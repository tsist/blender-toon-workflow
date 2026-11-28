# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic VIEW_3D preview lifecycle. No historical backend is modified."""
import json
import time
import traceback
import os
import uuid
import hashlib
from pathlib import Path

import bpy
import numpy as np
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty,
                       StringProperty,CollectionProperty,IntProperty,BoolVectorProperty)

from .geometry import Geometry, project_segments
from .drawing import Renderer
from .style import compile_preset
from .strokes import build_layers_steps

from . import curve_editor, chain_ui, chain_snapshot, chain_compute, chain_controls, source_bindings
from . import map_amount_ui, map_amount_controls
from . import amount_animation, amount_animation_ui
from . import parameter_animation, parameter_animation_ui
from . import viewport_scope
from . import dash_ui, dash_controls
from .phase_jobs import LatestPhaseJobs
from .output import save_image_new
from . import render_output, sequence_output
from . import render_elements
from .compositor_scope import active_binding, attach_tree
from . import wire_ui, edge_ui, ix_ui,selector_ui,selector_picker,group_ui

PROP = 'toon_viewport_preview'
_registered = False
_handler = None
_epoch = 0
_reset_epoch = 0
_scene_epochs = {}
_states = {}
_captures = []
_errors = {}
_capture_failures = []
_recent_updates = []
_style_cache={}
_phase_jobs=None
_chain_input_cache={}
_output_jobs={}
_camera_runs={}
_IDLE_SECONDS=0.25
_OUTPUT_FORMATS=(('PNG','PNG · 透明线图','sRGB RGBA8，颜色限于0–1'),
                ('OPEN_EXR','OpenEXR · 线性 HDR','线性 Rec.709、预乘 Alpha、32位浮点 ZIP 无损'))
_NAVIGATION_MODALS=frozenset({
    'VIEW3D_OT_rotate','VIEW3D_OT_move','VIEW3D_OT_zoom','VIEW3D_OT_dolly',
    'VIEW3D_OT_view_roll','VIEW3D_OT_zoom_border','VIEW3D_OT_fly','VIEW3D_OT_walk',
})


def _navigation_active(window):
    # This public collection is window-wide. Also suspend other previews in
    # that window rather than starting expensive work during its interaction.
    return any(op.bl_idname in _NAVIGATION_MODALS or
               op.bl_idname.startswith(('VIEW3D_OT_ndof_','TRANSFORM_OT_'))
               for op in window.modal_operators)


def _geometry_epoch(scene):
    return max(_reset_epoch,_scene_epochs.get(scene.as_pointer(),0))


def _source_members(context):
    return tuple((obj.as_pointer(),obj.data.as_pointer(),obj.name)
        for obj in context.view_layer.objects if obj.type=='MESH'
        and obj.visible_get(view_layer=context.view_layer,viewport=context.space_data))


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _touch(self, context):
    _errors.clear()
    _redraw()



def _phase_manager():
    global _phase_jobs
    if _phase_jobs is None:_phase_jobs=LatestPhaseJobs()
    return _phase_jobs


def _stop_phase_jobs():
    global _phase_jobs
    manager=_phase_jobs
    _phase_jobs=None
    if manager is not None:manager.close_and_drain()


def _chain_inputs(scene,plan):
    settings=getattr(scene,PROP)
    raw=settings.chain_controls_json
    if not raw:return None
    pointer=scene.as_pointer();key=(id(plan),settings.preset_revision,raw)
    cached=_chain_input_cache.get(pointer)
    if cached is not None and cached[0]==key:return cached[1]
    bindings,controls=chain_ui.read_controls(scene)
    result=(bindings,controls) if controls is not None and any(
        row['boundary_enabled'] or row['intersection_enabled'] for row in controls['brushes'].values()) else None
    _chain_input_cache[pointer]=(key,result)
    return result


def _chain_camera_steps(owner,geometry,frame,settings,plan,*,dash_patterns=None):
    scene=settings.id_data
    inputs=_chain_inputs(scene,plan)
    if inputs is None:return None
    bindings,controls=inputs
    if settings.intersection_enabled and any(row['intersection_enabled'] for row in controls['brushes'].values()):
        from .intersection_strokes import _plan as intersection_plan
        if intersection_plan(plan) is not None and not hasattr(geometry,'intersection_sources'):
            from .intersection_sources import capture_steps
            geometry.intersection_sources=yield from capture_steps(geometry.meshes)
    payload=yield from chain_snapshot.snapshot_steps(geometry,plan,frame['view'],frame['mvp'],
        frame['perspective'],frame['width'],frame['height'],1,settings,bindings,controls)
    patterns=_dash_patterns(scene,plan) if dash_patterns is None else dash_patterns
    if dash_controls.needs_metric(patterns):payload['include_dash_metric']=True
    manager=_phase_manager();ticket=manager.request(owner,chain_compute.compute_snapshot,payload)
    try:
        while True:
            ready,value=manager.poll(owner,ticket)
            if ready:return value
            yield dict(stage='chain-wait')
    finally:
        manager.cancel(owner)


def _style_mode_changed(self,context):
    for element in self.elements:_sync_element_groups(element,self)
    _touch(self,context)


def _map_amounts(scene,plan,*,evaluated_scene=None):
    static=map_amount_ui._settings_copy(scene,plan)
    if not any(item.enabled for item in getattr(scene,PROP).amount_tracks):return static
    bindings=chain_ui._recover(scene,plan)
    return amount_animation.resolve(scene,static,bindings['actuals'],evaluated_scene=evaluated_scene)


def _dash_patterns(scene,plan):
    return dash_ui._settings_copy(scene,plan)


def _style_signature(settings):
    return (settings.line_width,tuple(settings.line_color),settings.opacity,
        settings.silhouette,settings.open_edges,settings.marked_edges,settings.material_edges,
        settings.crease_edges,settings.crease_angle,settings.visibility,settings.style_mode,
        settings.preset_revision,settings.curve_revision,settings.width_multiplier,settings.intersection_enabled,
        settings.chain_controls_json,settings.map_amounts_json,settings.dash_patterns_json,
        amount_animation.signature(settings.id_data) if settings.style_mode=='PRESET' else (),
        parameter_animation.signature(settings.id_data) if settings.style_mode=='PRESET' else (),
        settings.wireframe_edges if settings.style_mode=='BASIC' else False,
        tuple((g.name,g.enabled,g.width_scale,g.opacity) for g in settings.groups))


def _curve_target_changed(self,context):
    if group_ui.guarded(self.id_data):return
    try:curve_editor.target_changed(self.id_data)
    except Exception as error:self.curve_error=str(error)
    _redraw()


class PreviewGroup(bpy.types.PropertyGroup):
    label: StringProperty()
    enabled: BoolProperty(name='启用',default=True,update=_touch)
    width_scale: FloatProperty(name='线宽倍数',default=1,min=0,max=8,update=_touch)
    opacity: FloatProperty(name='不透明度',default=1,min=0,max=1,update=_touch)
    line_set_id: IntProperty(name='线集合 ID',default=1,min=1,max=8)
    elements_only: BoolProperty(name='在分层主图中排除',description='仅影响分层 EXR 的 Combined；普通预览与单图保持原样',default=False)


class ElementGroupFilter(bpy.types.PropertyGroup):
    label: StringProperty()
    enabled: BoolProperty(name='包含',default=True)


def _background_changed(self,context):
    rgba=list(self.background_color)
    alpha=max(0.,min(1.,rgba[3]))
    if rgba[3]!=alpha:
        rgba[3]=alpha;self.background_color=rgba


class PreviewElement(bpy.types.PropertyGroup):
    label: StringProperty(name='名称',default='线条元素')
    element_type: EnumProperty(name='内容',items=[('COLOR','线色',''),('DEPTH','深度','沿相机视轴近白远黑，保留线条透明度')],default='COLOR')
    depth_min: FloatProperty(name='近距离',description='此距离及更近的线条为白色',default=1.,min=.01,max=100000,options=set())
    depth_max: FloatProperty(name='远距离',description='此距离及更远的线条为黑色；需大于近距离',default=3.,min=.01,max=100000,options=set())
    visible_lines_on: BoolProperty(name='可见线',default=True)
    hidden_lines_on: BoolProperty(name='隐藏线',default=True)
    line_set_ids: BoolVectorProperty(name='线集合 ID',size=8,default=(True,)*8)
    use_group_filter: BoolProperty(name='仅选定样式组',default=False)
    group_filters: CollectionProperty(type=ElementGroupFilter)
    outline_on: BoolProperty(name='轮廓／开放边',default=True)
    object_on: BoolProperty(name='对象内线',default=False)
    intersection_on: BoolProperty(name='交线',default=False)
    smoothing_on: BoolProperty(name='平滑边',default=False)
    material_id_on: BoolProperty(name='材质边界',default=False)
    selected_edges_on: BoolProperty(name='标记边',default=True)
    normal_angle_on: BoolProperty(name='法线角边',default=False)
    wireframe_on: BoolProperty(name='线框',description='筛选基础模式中已启用的网格线框来源',default=False)
    background_enabled: BoolProperty(name='背景色',default=False)
    background_color: FloatVectorProperty(name='背景',subtype='COLOR',size=4,default=(1,1,1,1),min=0,soft_max=1,update=_background_changed)


class PreviewSettings(bpy.types.PropertyGroup):
    binding_object:PointerProperty(type=bpy.types.Object)
    binding_collection:PointerProperty(type=bpy.types.Collection)
    binding_material:PointerProperty(type=bpy.types.Material)
    enabled: BoolProperty(name='自动描边', default=False, update=_touch)
    display_mode: EnumProperty(name='显示', items=[
        ('OVERLAY','叠加','叠加到当前视图'), ('LINES','仅描边','棋盘格底，只看线条'),
        ('WHITE','白底线稿','纯白背景，只看线条')], default='WHITE', update=_touch)
    line_width: FloatProperty(name='线宽（像素）', default=1.4, min=0.1, max=32, update=_touch)
    line_color: FloatVectorProperty(name='线色', subtype='COLOR', size=3, default=(0.02,0.015,0.025), min=0, max=1, update=_touch)
    opacity: FloatProperty(name='不透明度', default=1, min=0, max=1, update=_touch)
    style_mode: EnumProperty(name='样式',items=[('BASIC','基础几何','统一线宽和线色'),
        ('PRESET','原配置预览','根据已载入配置生成边界线与交线')],default='BASIC',update=_style_mode_changed)
    intersection_enabled: BoolProperty(name='交线/自交',default=True,update=_touch)
    width_multiplier: FloatProperty(name='整体线宽倍数',default=1,min=0,max=8,update=_touch)
    document_path: StringProperty(name='样式文档',subtype='FILE_PATH')
    recipe_path: StringProperty(name='对象绑定',subtype='FILE_PATH')
    show_import: BoolProperty(name='载入另一份配置',default=False)
    show_visible_lines: BoolProperty(name='编辑可见线',default=True)
    preset_text: PointerProperty(type=bpy.types.Text)
    preset_revision: IntProperty(default=0)
    groups: CollectionProperty(type=PreviewGroup)
    basic_line_set_id: IntProperty(name='线集合 ID',default=1,min=1,max=8)
    basic_elements_only: BoolProperty(name='在分层主图中排除',description='仅影响分层 EXR 的 Combined；普通预览与单图保持原样',default=False)
    elements: CollectionProperty(type=PreviewElement)
    active_element: IntProperty(default=0,min=0)
    element_budget_mb: IntProperty(name='缓冲预算（MiB）',default=512,min=65,max=65536)
    sequence_use_scene_range: BoolProperty(name='使用场景帧范围',default=True)
    sequence_start: IntProperty(name='起始帧',default=1,min=-1048574,max=1048574)
    sequence_end: IntProperty(name='结束帧',default=250,min=-1048574,max=1048574)
    sequence_step: IntProperty(name='帧步长',default=1,min=1)
    sequence_format: EnumProperty(name='序列格式',items=_OUTPUT_FORMATS,default='PNG')
    sequence_elements: BoolProperty(name='多层 EXR',description='使用分层线图面板的元素设置，导出时固定为 EXR',default=False)
    show_map_amounts: BoolProperty(default=False)
    map_amounts_json: StringProperty(default='',update=_touch)
    map_amount_error: StringProperty()
    map_amounts: CollectionProperty(type=map_amount_ui.MapAmountBrush)
    amount_tracks: CollectionProperty(type=amount_animation.AmountTrack,options=set())
    parameter_tracks: CollectionProperty(type=parameter_animation.ParameterTrack,options=set())
    parameter_scope: EnumProperty(name='参数类型',items=[('BRUSH','笔刷',''),('LINE','Line',''),('MERGE','合并开关',''),('LF','材质线条功能',''),('MASK','组遮罩',''),('SELF','自交开关','')],options=set())
    parameter_owner: StringProperty(options=set())
    parameter_choice: EnumProperty(name='动画参数',items=parameter_animation_ui.items,options=set())
    parameter_key_float: FloatProperty(name='待写值',min=-1e12,max=1e12,options=set())
    parameter_key_int: IntProperty(name='待写整数',options=set())
    parameter_key_bool: BoolProperty(name='待写开关',options=set())
    parameter_animation_error: StringProperty(options={'SKIP_SAVE'})
    amount_key_value: FloatProperty(name='关键帧值',description='使用下方按钮写入当前帧；不会改写静态影响量',
        default=1.,min=0.,max=1.,precision=3,options=set())
    amount_animation_error: StringProperty(options={'SKIP_SAVE'})
    show_dashes: BoolProperty(default=False,options=set())
    dash_patterns_json: StringProperty(default='',options=set(),update=_touch)
    dash_error: StringProperty(options={'SKIP_SAVE'})
    dashes: CollectionProperty(type=dash_ui.DashBrush,options=set())
    show_chains: BoolProperty(default=False)
    chain_controls_json: StringProperty(default='',update=_touch)
    chain_error: StringProperty()
    chains: CollectionProperty(type=chain_ui.ChainBrush)
    show_curves: BoolProperty(default=False)
    curve_group: EnumProperty(name='分组',items=curve_editor.group_items,update=_curve_target_changed)
    curve_slot: EnumProperty(name='笔刷',items=curve_editor.slot_items,default=1,update=_curve_target_changed)
    curve_tree: PointerProperty(type=bpy.types.NodeTree)
    curve_size_curve_enabled: BoolProperty(default=False)
    curve_alpha_curve_enabled: BoolProperty(default=False)
    curve_revision: IntProperty(default=0)
    curve_error: StringProperty()
    silhouette: BoolProperty(name='轮廓线', default=True, update=_touch)
    open_edges: BoolProperty(name='开放边', default=True, update=_touch)
    marked_edges: BoolProperty(name='标记边', default=True, update=_touch)
    wireframe_edges: BoolProperty(name='线框',description='绘制求值网格的全部原始边，含松散边；仍按可见／隐藏选项判断遮挡',default=False,update=_touch)
    material_edges: BoolProperty(name='材质边界', default=False, update=_touch)
    crease_edges: BoolProperty(name='法线角边', default=False, update=_touch)
    crease_angle: FloatProperty(name='最小夹角', default=45, min=0, max=180, update=_touch)
    visibility: EnumProperty(name='遮挡', items=[('VISIBLE','可见线','只画可见部分'),
        ('HIDDEN','隐藏线','只画被遮住的部分'),('BOTH','全部','显示两者')],default='VISIBLE',update=_touch)
    output_message: StringProperty(options={'SKIP_SAVE'})
    output_error: BoolProperty(options={'SKIP_SAVE'})
    element_output_message: StringProperty(options={'SKIP_SAVE'})
    element_output_error: BoolProperty(options={'SKIP_SAVE'})
    sequence_message: StringProperty(options={'SKIP_SAVE'})
    sequence_error: BoolProperty(options={'SKIP_SAVE'})
    sequence_message_layer: StringProperty(options={'SKIP_SAVE'})
    last_output_path: StringProperty(subtype='FILE_PATH')
    last_output_sha256: StringProperty()
    last_output_elements: StringProperty()


class State:
    def __init__(self, context):
        self.scene=context.scene.as_pointer();self.layer=context.view_layer.name
        self.geometry_epoch=-1;self.geometry=None;self.geometry_members=None
        self.renderer=None;self.key=None;self.change_time=time.perf_counter()
        self.refined=False;self.error=None;self.metrics={};self.generation=0;self.request=0
        self.history=[];self.status='WAITING';self.preparation=None
        self.preparation_progress=None;self.preparation_max_step=0.
        self.geometry_preparation=None;self.geometry_request=None
        self.style_preparation=None;self.render_preparation=None
        self.pending_layers=None;self.pending_style_report=None;self.pending_projected=None
        self.work_progress=None;self.work_compute_seconds=0.;self.classify_compute_seconds=0.
        self.work_max_step=0.;self.work_started=self.change_time
        self.phase_owner=uuid.uuid4().hex;self.phase_ticket=None;self.phase_token=None
        self.phase_snapshot=None;self.chain_phase=None
        self.map_amounts={}
        self.dash_patterns={}

    def cancel_preparation(self):
        if self.preparation is not None:self.preparation.close();self.preparation=None
        self.preparation_progress=None

    def cancel_view_work(self):
        # Every generator yield is outside all GPU binding/state contexts.
        for name in ('style_preparation','render_preparation'):
            job=getattr(self,name)
            if job is not None:job.close();setattr(self,name,None)
        self.pending_layers=self.pending_style_report=self.pending_projected=None
        self.work_progress=None
        if self.phase_snapshot is not None:self.phase_snapshot.close();self.phase_snapshot=None
        if self.phase_ticket is not None and _phase_jobs is not None:_phase_jobs.cancel(self.phase_owner)
        self.phase_ticket=self.phase_token=self.chain_phase=None

    def cancel_geometry_preparation(self):
        if self.geometry_preparation is not None:self.geometry_preparation.close();self.geometry_preparation=None
        self.geometry_request=None

    def close(self):
        self.cancel_view_work();self.cancel_preparation();self.cancel_geometry_preparation()
        self.geometry=None;self.renderer=None

    def _advance(self,name,context):
        deadline=time.perf_counter()+.012
        while True:
            started=time.perf_counter()
            try:self.work_progress=next(getattr(self,name))
            except StopIteration as done:
                setattr(self,name,None);self.work_progress=None
                return True,done.value
            except Exception:
                getattr(self,name).close();setattr(self,name,None);self.work_progress=None
                raise
            finally:
                elapsed=time.perf_counter()-started
                self.work_compute_seconds+=elapsed;self.work_max_step=max(self.work_max_step,elapsed)
                if name=='style_preparation':self.classify_compute_seconds+=elapsed
            if time.perf_counter()>=deadline:
                self.status='PREPARING';context.area.tag_redraw()
                return False,None

    def update(self,context,members=None):
        settings=getattr(context.scene,PROP);rv3d=context.region_data
        width,height=context.region.width,context.region.height
        members=_source_members(context) if members is None else members
        epoch=_geometry_epoch(context.scene)
        view=tuple(value for row in rv3d.view_matrix for value in row)
        projection=tuple(value for row in rv3d.perspective_matrix for value in row)
        style=_style_signature(settings)
        key=(epoch,members,width,height,view,projection,style);now=time.perf_counter()
        if key!=self.key:
            self.cancel_view_work();self.refined=False
            plan=effective_preset(context.scene) if settings.style_mode=='PRESET' else None
            self.effective_plan=plan
            amounts=_map_amounts(context.scene,plan) if plan is not None else {}
            patterns=_dash_patterns(context.scene,plan) if plan is not None else {}
            self.key=key;self.map_amounts=amounts;self.dash_patterns=patterns;self.change_time=now
            self.work_started=now;self.work_compute_seconds=self.classify_compute_seconds=0.;self.work_max_step=0.
            self.refined=False;self.error=None;self.request+=1;self.status='WAITING'
        if _navigation_active(context.window):
            self.cancel_view_work()
            self.change_time=now;self.refined=False;self.status='MOVING';return False
        if now-self.change_time<_IDLE_SECONDS:
            self.status='WAITING';return False
        if self.refined:return True
        if self.geometry is None or self.geometry_epoch!=epoch or self.geometry_members!=members:
            self.cancel_view_work();self.cancel_preparation()
            geometry_request=(epoch,members)
            if self.geometry_preparation is not None and self.geometry_request!=geometry_request:
                self.geometry_preparation.close();self.geometry_preparation=None
            if self.geometry_preparation is None:
                self.geometry_preparation=Geometry.capture_steps(context);self.geometry_request=geometry_request
            ready,result=self._advance('geometry_preparation',context)
            if not ready:return False
            self.geometry=result;self.geometry_epoch=epoch;self.geometry_members=members
        plan=self.effective_plan if settings.style_mode=='PRESET' else None
        from .intersection_strokes import _plan as intersection_plan
        need_intersections=plan is not None and settings.intersection_enabled and intersection_plan(plan) is not None
        if need_intersections and not hasattr(self.geometry,'intersection_sources'):
            from .intersection_sources import capture_steps
            if self.preparation is None:self.preparation=capture_steps(self.geometry.meshes);self.preparation_max_step=0.
            deadline=time.perf_counter()+.012
            while True:
                started=time.perf_counter()
                try:self.preparation_progress=next(self.preparation)
                except StopIteration as done:
                    self.geometry.intersection_sources=done.value;self.preparation=None;self.preparation_progress=None;break
                finally:
                    elapsed=time.perf_counter()-started;self.preparation_max_step=max(self.preparation_max_step,elapsed)
                    self.work_compute_seconds+=elapsed;self.work_max_step=max(self.work_max_step,elapsed)
                if time.perf_counter()>=deadline:
                    self.status='PREPARING';context.area.tag_redraw();return False
        elif not need_intersections:self.cancel_preparation()
        scale=min(1.,2048/max(width,height));rw,rh=max(1,round(width*scale)),max(1,round(height*scale))
        matrix=rv3d.perspective_matrix.copy()
        if plan is not None and self.chain_phase is None:
            inputs=_chain_inputs(context.scene,plan)
            if inputs is not None:
                token=(_reset_epoch,self.key,self.request)
                if self.phase_ticket is None:
                    if self.phase_snapshot is None:
                        bindings,controls=inputs
                        self.phase_snapshot=chain_snapshot.snapshot_steps(self.geometry,plan,rv3d.view_matrix.copy(),matrix,
                            rv3d.is_perspective,rw,rh,scale,settings,bindings,controls)
                    ready,payload=self._advance('phase_snapshot',context)
                    if not ready:return False
                    if dash_controls.needs_metric(self.dash_patterns):payload['include_dash_metric']=True
                    self.phase_token=token
                    self.phase_ticket=_phase_manager().request(self.phase_owner,chain_compute.compute_snapshot,payload)
                ready,value=_phase_manager().poll(self.phase_owner,self.phase_ticket)
                if not ready:
                    self.status='PREPARING';context.area.tag_redraw();return False
                if self.phase_token!=token:
                    self.cancel_view_work();return False
                self.chain_phase=value
                _phase_manager().cancel(self.phase_owner);self.phase_ticket=None
        if self.render_preparation is None:
            if settings.style_mode=='PRESET':
                if self.style_preparation is None:
                    self.style_preparation=build_layers_steps(self.geometry,plan,rv3d.view_matrix.copy(),matrix,
                        rv3d.is_perspective,rw,rh,scale,settings,allow_intersection_capture=True,chain_phase=self.chain_phase,
                        map_amounts=self.map_amounts,dash_patterns=self.dash_patterns)
                ready,result=self._advance('style_preparation',context)
                if not ready:return False
                self.pending_layers,self.pending_style_report=result
                self.pending_projected=np.empty((sum(len(layer['projected']) for layer in self.pending_layers),2,3),np.float32)
                world=None;supports=np.empty((0,2),np.float32)
            else:
                started=time.perf_counter()
                segments,supports=self.geometry.segment_records(rv3d.view_matrix,rv3d.is_perspective,settings)
                projected,indices,source_u=project_segments(segments,matrix,rw,rh,with_parameters=True)
                world=segments[indices,0,None,:]+source_u[:,:,None]*(segments[indices,1]-segments[indices,0])[:,None,:]
                supports=supports[indices];self.pending_projected=projected
                elapsed=time.perf_counter()-started;self.classify_compute_seconds+=elapsed;self.work_compute_seconds+=elapsed;self.work_max_step=max(self.work_max_step,elapsed)
            if self.renderer is None:
                started=time.perf_counter();self.renderer=Renderer();elapsed=time.perf_counter()-started
                self.work_compute_seconds+=elapsed;self.work_max_step=max(self.work_max_step,elapsed)
            self.render_preparation=self.renderer.render_steps(self.geometry,matrix,self.pending_projected,settings,
                rw,rh,scale,supports,world=world,layers=self.pending_layers)
        ready,_=self._advance('render_preparation',context)
        if not ready:return False
        # Only a completed job for this still-current key can be published.
        if self.phase_token is not None and self.phase_token!=(_reset_epoch,self.key,self.request):
            self.cancel_view_work();return False
        self.generation+=1;self.refined=True;self.status='READY';self.error=None
        self.metrics=dict(generation=self.generation,request=self.request,phase='细化',
            width=rw,height=rh,source_names=self.geometry.names,mesh_count=len(self.geometry.meshes),
            triangles=self.geometry.triangle_count,segments=len(self.pending_projected),nonmanifold_edges=self.geometry.nonmanifold_count,
            capture_seconds=self.geometry.capture_seconds,update_seconds=self.work_compute_seconds,
            classify_seconds=self.classify_compute_seconds,geometry_epoch=self.geometry_epoch,perspective=rv3d.is_perspective,
            preparation_max_step_seconds=self.preparation_max_step,work_max_step_seconds=self.work_max_step,
            work_wall_seconds=time.perf_counter()-self.work_started,source_alpha=self.renderer.alpha_report,
            settings=dict(line_width=settings.line_width,color=list(settings.line_color),opacity=settings.opacity,
                visibility=settings.visibility,style_mode=settings.style_mode),style_report=self.pending_style_report,
            settled_seconds=time.perf_counter()-self.change_time)
        self.history.append(self.metrics.copy());self.history=self.history[-32:]
        self.pending_layers=self.pending_style_report=self.pending_projected=None
        context.area.tag_redraw();return True


def _state_key(context):
    return (context.window.as_pointer(),context.area.as_pointer(),context.region.as_pointer(),
            context.scene.as_pointer(),context.view_layer.as_pointer())


def _save_capture(state, path, key, mode):
    path = Path(path)
    white=path.with_name(path.stem+'-white.png')
    receipt=Path(str(path)+'.json')
    if not path.is_absolute() or not path.parent.is_dir() or any(p.exists() for p in (path,white,receipt)):
        raise ValueError('Capture requires three new absolute paths in an existing directory')
    token=uuid.uuid4().hex
    staged=path.with_name('.'+token+'-'+path.name)
    staged_white=white.with_name('.'+token+'-'+white.name)
    staged_receipt=receipt.with_name('.'+token+'-'+receipt.name)
    renderer = state.renderer
    pixels = np.asarray(renderer.color.read(),dtype=np.float32).reshape(-1)
    image = bpy.data.images.new('ToonViewportCapture',width=renderer.width,height=renderer.height,
                                alpha=True,float_buffer=True)
    try:
        # GPU output is premultiplied. Blender's image pixel API takes straight
        # RGBA; unassociate only nonzero alpha for a portable transparent PNG.
        values=pixels.reshape((-1,4)).copy()
        values[:,:3]=np.divide(values[:,:3],values[:,3,None],out=np.zeros_like(values[:,:3]),where=values[:,3,None]>0)
        image.pixels.foreach_set(values.reshape(-1))
        image.alpha_mode='STRAIGHT'
        image.filepath_raw=str(staged)
        image.file_format='PNG'
        image.save()
        white_values=pixels.reshape((-1,4)).copy()
        white_values[:,:3]+=1.0-white_values[:,3,None]
        white_values[:,3]=1.0
        image.pixels.foreach_set(white_values.reshape(-1))
        image.filepath_raw=str(staged_white)
        image.save()
    finally:
        bpy.data.images.remove(image)
    result=dict(path=str(path),white_path=str(white),width=renderer.width,height=renderer.height,
        contract='transparent_line_layer_and_white_QA.v1',display_mode_at_capture=mode,
        state_key=list(key),metrics=state.metrics)
    with staged_receipt.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows rename is create-only. A late conflict leaves diagnostics and
    # never overwrites another file; the JSON is promoted last as completion.
    os.rename(staged,path)
    os.rename(staged_white,white)
    os.rename(staged_receipt,receipt)
    return result


def request_capture(path, context=None):
    """Queue a transparent line layer and white QA view for one explicit viewport.

    The last JSON is the completion marker, not a screenshot/beauty contract.
    Timer callers must have exactly one eligible view or supply a temp override.
    Captures the next ready current view, including navigation after this call.
    """
    context=context or bpy.context
    if context.area is not None and context.area.type=='VIEW_3D' and context.region is not None and context.region.type=='WINDOW':
        target_area=context.area
        key=_state_key(context)
    else:
        areas=[a for a in context.window.screen.areas if a.type=='VIEW_3D']
        if len(areas)!=1:
            raise ValueError('Capture requires an explicit single VIEW_3D target')
        target_area=areas[0]
        region=next(r for r in areas[0].regions if r.type=='WINDOW')
        key=(context.window.as_pointer(),areas[0].as_pointer(),region.as_pointer(),
             context.scene.as_pointer(),context.view_layer.as_pointer())
    if not getattr(context.scene,PROP).enabled or not viewport_scope.enabled(context.window.screen,target_area,target_area.spaces.active):
        raise ValueError('Capture target preview is disabled')
    if key in _errors:
        # An explicit new capture is one new attempt, not a timer retry loop.
        _errors.pop(key)
        state=_states.get(key)
        if state is not None:
            state.key=None;state.refined=False;state.error=None;state.status='WAITING'
    _captures.append((key,str(path)))
    _redraw()
    return dict(queued=str(path),state_key=list(key))


def _output_key(context):
    area=context.area
    if area is None or area.type!='VIEW_3D':
        areas=[a for a in context.window.screen.areas if a.type=='VIEW_3D']
        if len(areas)!=1:raise ValueError('请选择一个明确的三维视口')
        area=areas[0]
    region=next(r for r in area.regions if r.type=='WINDOW')
    return (context.window.as_pointer(),area.as_pointer(),region.as_pointer(),
            context.scene.as_pointer(),context.view_layer.as_pointer())


def _finish_output(job,status,message,scene=None,result=None):
    job.update(status=status,message=message)
    if result is not None:job['result']=result
    if status!='QUEUED':
        warning=_close_camera_run(job['id'])
        if warning:job['cleanup_warning']=warning
    try:
        if scene is not None:
            settings=getattr(scene,PROP)
            settings.output_message=message;settings.output_error=status=='FAILED'
            if 'element_config' in job:
                settings.element_output_message=message;settings.element_output_error=status=='FAILED'
            if status=='COMPLETE':
                settings.last_output_path=result['path'];settings.last_output_sha256=result['sha256']
                passes=result.get('element_passes',[])
                settings.last_output_elements=json.dumps(passes,ensure_ascii=False) if passes else ''
        _redraw()
    except Exception as error:
        # UI notification is separate from the durable file/job outcome.
        job['ui_warning']=str(error)


def request_output(path,context=None):
    """Export the next complete view as straight sRGB PNG or linear EXR.

    This is a line layer, independent of the display background. Navigation
    keeps it queued until the target's current complete result is ready.
    """
    return _request_output(path,context,'VIEW')


def request_camera_output(path,context=None):
    """Queue the current scene camera at render extent, using viewport meshes.

    Frame/camera/style changes restart unfinished work for the latest state.
    The host viewport supplies visibility and a GPU context, never the view.
    """
    return _request_output(path,context,'CAMERA')


def request_render_output(path,context=None):
    """Export a submission-time scene snapshot using an isolated RENDER graph."""
    context=context or bpy.context
    normalized=os.path.normcase(str(Path(bpy.path.abspath(str(path))).resolve()))
    if any(j['status']=='QUEUED' and j['normalized_path']==normalized for j in _output_jobs.values()):
        raise ValueError('此文件名已有待完成的导出')
    result=render_output.request(path,context)
    settings=getattr(context.scene,PROP)
    settings.output_message='后台渲染线图：使用提交时场景，可继续操作或取消';settings.output_error=False
    _redraw()
    return result


def element_configuration(settings):
    rows=[]
    for item in settings.elements:
        row=dict(id=item.name,label=item.label,type=item.element_type,
            group_uids=[g.name for g in item.group_filters if g.enabled] if item.use_group_filter else None,
            line_set_ids=list(item.line_set_ids),visible_lines_on=item.visible_lines_on,hidden_lines_on=item.hidden_lines_on,
            background=dict(enabled=item.background_enabled,rgba=list(item.background_color)))
        row.update({name+'_on':getattr(item,name+'_on') for name in render_elements.CATEGORIES})
        if item.element_type=='DEPTH':row['depth']=dict(z_min=item.depth_min,z_max=item.depth_max)
        rows.append(row)
    return rows


def request_render_elements(path,context=None,*,elements=None,memory_budget_bytes=None):
    """Freeze selected COLOR/DEPTH elements and export one multilayer EXR."""
    context=context or bpy.context;settings=getattr(context.scene,PROP)
    normalized=os.path.normcase(str(Path(bpy.path.abspath(str(path))).resolve()))
    if any(j['status']=='QUEUED' and j['normalized_path']==normalized for j in _output_jobs.values()):
        raise ValueError('此文件名已有待完成的导出')
    result=render_output.request(path,context,elements=element_configuration(settings) if elements is None else elements,
        memory_budget_bytes=settings.element_budget_mb*1024*1024 if memory_budget_bytes is None else memory_budget_bytes)
    settings.output_message='后台分层线图：使用提交时场景与元素设置';settings.output_error=False
    settings.element_output_message=settings.output_message;settings.element_output_error=False
    _redraw();return result


def _sequence_frames(scene,settings):
    start,end=(scene.frame_start,scene.frame_end) if settings.sequence_use_scene_range else (settings.sequence_start,settings.sequence_end)
    if start>end:raise ValueError('序列起始帧不能大于结束帧')
    return list(range(start,end+1,settings.sequence_step))


def request_sequence_output(path,context=None,*,frames=None,elements=None,memory_budget_bytes=None):
    """Freeze the active camera scene and queue numbered complete RENDER frames.

    The filename is a prefix template: lines.png becomes lines_000001.png.
    No preview enablement or change to the current scene frame is required.
    """
    context=context or bpy.context;settings=getattr(context.scene,PROP)
    path=Path(bpy.path.abspath(str(path)))
    requested=_sequence_frames(context.scene,settings) if frames is None else frames
    if elements is None and settings.sequence_elements:elements=element_configuration(settings)
    suffix='.exr' if elements is not None else path.suffix.lower()
    # The backend validates all frame/path identities. Admission here also
    # protects preview exports, whose jobs are owned by this module.
    ordered=sequence_output.protocol.frames(requested)
    paths={os.path.normcase(str((path.parent/sequence_output.protocol.frame_name(path.stem,frame,suffix)).resolve())) for frame in ordered}
    if any(j['status']=='QUEUED' and j['normalized_path'] in paths for j in _output_jobs.values()):
        raise ValueError('已有线图导出正在使用相同的序列文件名')
    result=sequence_output.request(path.parent,path.stem,ordered,suffix,context,elements=elements,
        memory_budget_bytes=settings.element_budget_mb*1024*1024 if memory_budget_bytes is None else memory_budget_bytes)
    settings.sequence_message='序列已排队，使用提交时的场景，可继续操作或取消'
    settings.sequence_error=False;settings.sequence_message_layer=context.view_layer.name
    _redraw();return result


def _request_output(path,context,mode):
    context=context or bpy.context
    key=_output_key(context);path=Path(bpy.path.abspath(str(path)))
    if not getattr(context.scene,PROP).enabled:raise ValueError('请先启用自动描边')
    target_area=next(area for area in context.window.screen.areas if area.as_pointer()==key[1])
    if not viewport_scope.enabled(context.window.screen,target_area,target_area.spaces.active):raise ValueError('请先开启此视口描边')
    if not path.is_absolute() or path.suffix.lower() not in {'.png','.exr'} or not path.parent.is_dir():
        raise ValueError('请选择已有目录中的新 PNG 或 EXR 文件名')
    if path.exists() or path.is_symlink():raise FileExistsError('文件已存在，请选择新文件名')
    normalized=os.path.normcase(str(path.resolve()))
    if any(j['status']=='QUEUED' and j['normalized_path']==normalized for j in render_output.jobs()):
        raise ValueError('此文件名已有待完成的渲染线图')
    if any(j['status']=='QUEUED' and normalized in j.get('normalized_paths',()) for j in sequence_output.jobs()):
        raise ValueError('此文件名已有待完成的序列线图')
    for job in _output_jobs.values():
        if job['status']=='QUEUED' and (job['key']==key or job['normalized_path']==normalized):
            raise ValueError('此视口或文件名已有待完成的导出')
    for token in list(_output_jobs):
        if len(_output_jobs)<20:break
        if _output_jobs[token]['status']!='QUEUED':del _output_jobs[token]
    if len(_output_jobs)>=20:raise ValueError('等待导出的视口过多')
    if mode=='CAMERA':
        from .camera import capture
        capture(context)
    if key in _errors:
        # One explicit export is one new attempt after a transient preview
        # error. Do not enqueue behind the unchanged error signature forever.
        _errors.pop(key)
        state=_states.get(key)
        if state is not None:
            state.cancel_view_work();state.cancel_preparation();state.cancel_geometry_preparation()
            state.key=None;state.refined=False;state.error=None;state.status='WAITING'
    token=uuid.uuid4().hex
    job=dict(id=token,key=key,path=str(path),normalized_path=normalized,status='QUEUED',mode=mode)
    _output_jobs[token]=job
    message='等待相机线图，可继续移动或取消' if mode=='CAMERA' else '等待当前完整线图，可继续移动或取消'
    _finish_output(job,'QUEUED',message,context.scene)
    _redraw()
    return dict(id=token,status='QUEUED',path=str(path),state_key=list(key))


def output_jobs():
    """Detached job reports; completion includes frame, view and file identity."""
    import copy
    return copy.deepcopy(list(_output_jobs.values()))+render_output.jobs()


def cancel_output(token,scene=None):
    job=_output_jobs.get(token)
    if job is None:return render_output.cancel(token)
    if job['status']!='QUEUED':return False
    _finish_output(job,'CANCELLED','已取消线图导出',scene)
    return True


def _save_outputs(state,key,context):
    for job in list(_output_jobs.values()):
        if job['key']!=key or job['status']!='QUEUED':continue
        if job.get('mode','VIEW')!='VIEW':continue
        try:
            metadata=dict(generation=state.generation,frame=context.scene.frame_current,
                scene=context.scene.name,view_layer=context.view_layer.name,
                projection=[list(row) for row in context.region_data.perspective_matrix],
                contract='viewport_resolved_line_'+Path(job['path']).suffix.lower()[1:]+'.v1')
            pixels=np.asarray(state.renderer.color.read(),dtype=np.float32).reshape(
                state.renderer.height,state.renderer.width,4)
            result=save_image_new(job['path'],pixels)
        except Exception as error:
            _finish_output(job,'FAILED','导出失败：'+str(error),context.scene)
            if getattr(error,'__notes__',None):job['error_notes']=list(error.__notes__)
            continue
        # Publication has completed. Preserve the file identity even if a
        # later metadata/UI operation fails; never delete the published PNG.
        job.update(status='COMPLETE',result=result)
        try:
            result.update(metadata)
            _finish_output(job,'COMPLETE','已导出透明线图：'+Path(job['path']).name,context.scene,result)
        except Exception as error:
            job.update(status='COMPLETE',result=result,message='透明线图已导出',ui_warning=str(error))


class CameraRun:
    def __init__(self,geometry,frame,settings,plan,signature):
        self.geometry=geometry;self.frame=frame;self.signature=signature
        self.phase_owner=uuid.uuid4().hex
        self.map_amounts=_map_amounts(settings.id_data,plan) if plan is not None else {}
        self.dash_patterns=_dash_patterns(settings.id_data,plan) if plan is not None else {}
        self.renderer=None;self.steps=self._work(settings,plan)

    def _work(self,settings,plan):
        f=self.frame;w,h=f['width'],f['height'];style=None;layers=None
        if plan is not None:
            chain_phase=yield from _chain_camera_steps(self.phase_owner,self.geometry,f,settings,plan,dash_patterns=self.dash_patterns)
            layers,style=yield from build_layers_steps(self.geometry,plan,f['view'],f['mvp'],
                f['perspective'],w,h,1,settings,allow_intersection_capture=False,chain_phase=chain_phase,
                map_amounts=self.map_amounts,dash_patterns=self.dash_patterns)
            projected=np.empty((0,2,3),np.float32);supports=np.empty((0,2),np.float32);world=None
        else:
            segments,supports=self.geometry.segment_records(f['view'],f['perspective'],settings)
            projected,indices,source_u=project_segments(segments,f['mvp'],w,h,with_parameters=True)
            world=segments[indices,0,None,:]+source_u[:,:,None]*(segments[indices,1]-segments[indices,0])[:,None,:]
            supports=supports[indices]
            yield dict(stage='camera-projection')
        self.renderer=Renderer()
        yield from self.renderer.render_steps(self.geometry,f['mvp'],projected,settings,
            w,h,1,supports,world=world,layers=layers)
        return style

    def close(self):
        if self.steps is not None:self.steps.close();self.steps=None
        if _phase_jobs is not None:_phase_jobs.cancel(self.phase_owner)
        self.renderer=None;self.geometry=None


def _close_camera_run(token):
    run=_camera_runs.pop(token,None)
    if run is not None:
        try:run.close()
        except Exception as error:return str(error)
    return None


def _camera_signature(frame,state,settings):
    return (frame['signature'],id(state.geometry),state.geometry_epoch,
            state.geometry_members,_style_signature(settings))


def _advance_camera_outputs(state,key,context):
    if _navigation_active(context.window):return
    for job in list(_output_jobs.values()):
        if job['key']!=key or job['status']!='QUEUED' or job.get('mode')!='CAMERA':continue
        try:
            from .camera import capture,extract_pixels
            settings=getattr(context.scene,PROP);frame=capture(context)
            signature=_camera_signature(frame,state,settings)
            run=_camera_runs.get(job['id'])
            if run is None or run.signature!=signature:
                warning=_close_camera_run(job['id'])
                if warning:job['cleanup_warning']=warning
                plan=effective_preset(context.scene) if settings.style_mode=='PRESET' else None
                run=CameraRun(state.geometry,frame,settings,plan,signature)
                _camera_runs[job['id']]=run
                job['restarts']=job.get('restarts',-1)+1
                job['binding']=frame['metadata']
            deadline=time.perf_counter()+.012
            while True:
                started=time.perf_counter()
                try:progress=next(run.steps)
                except StopIteration as done:
                    metadata=dict(frame['metadata'],contract='camera_resolved_line_'+Path(job['path']).suffix.lower()[1:]+'.v1',mode='CAMERA',
                        scene=context.scene.name,view_layer=context.view_layer.name,
                        preview_generation=state.generation,triangles=state.geometry.triangle_count,
                        source_names=state.geometry.names,style_report=done.value)
                    pixels=np.asarray(run.renderer.color.read(),np.float32).reshape(frame['height'],frame['width'],4)
                    pixels=extract_pixels(frame,pixels)
                    if _camera_signature(capture(context),state,settings)!=signature:
                        _close_camera_run(job['id']);break
                    result=save_image_new(job['path'],pixels,pixel_aspect=frame['metadata']['pixel_aspect'])
                    # The durable output precedes all UI and cleanup work.
                    job.update(status='COMPLETE',result=result)
                    try:
                        result.update(metadata)
                        _finish_output(job,'COMPLETE','已导出相机线图：'+Path(job['path']).name,context.scene,result)
                    except Exception as error:
                        job['ui_warning']=str(error)
                        warning=_close_camera_run(job['id'])
                        if warning:job['cleanup_warning']=warning
                    break
                elapsed=time.perf_counter()-started
                job['max_compute_step']=max(job.get('max_compute_step',0.),elapsed)
                job['progress']=str(progress.get('stage',progress.get('phase','camera'))) if isinstance(progress,dict) else 'camera'
                if isinstance(progress,dict) and progress.get('stage')=='chain-wait':break
                if time.perf_counter()>=deadline:break
        except Exception as error:
            if job['status']=='COMPLETE':job['ui_warning']=str(error)
            else:_finish_output(job,'FAILED','相机线图导出失败：'+str(error),context.scene)
            warning=_close_camera_run(job['id'])
            if warning:job['cleanup_warning']=warning


def inspect():
    return [dict(key=list(key),error=state.error,ready=state.refined,status=state.status,
                 preparation=state.preparation_progress,work=state.work_progress,
                 history=state.history.copy(),**state.metrics) for key,state in _states.items()]


def _draw():
    context = bpy.context
    if context.area is None or context.area.type!='VIEW_3D' or context.region_data is None:
        return
    if not getattr(context.scene,PROP).enabled or not viewport_scope.enabled(context.screen,context.area,context.space_data):
        return
    key = _state_key(context)
    members=_source_members(context)
    signature=(_geometry_epoch(context.scene),members,context.region.width,context.region.height,
               tuple(v for row in context.region_data.perspective_matrix for v in row))
    if key in _errors:
        if _errors[key]['signature']==signature:
            return
        # New view/source input is a new attempt. Do not let an old failure
        # suppress the timer wake-up while this new request waits for idle.
        _errors.pop(key)
    state = _states.get(key)
    try:
        if state is None:
            state = _states[key] = State(context)
        if not state.update(context,members):
            # Leave Blender's current solid/material/rendered view untouched.
            # In particular do not draw a stale line texture or its white fill.
            return
        _errors.pop(key,None)
        from . import output_scope
        state.renderer.display(context.region.width,context.region.height,getattr(context.scene,PROP).display_mode,
            output_scope.background(context.scene,context.view_layer.name))
        if state.refined:
            _save_outputs(state,key,context)
            _advance_camera_outputs(state,key,context)
            for target,path in _captures.copy():
                if target!=key:continue
                _captures.remove((target,path))
                try:
                    _save_capture(state,path,key,getattr(context.scene,PROP).display_mode)
                except Exception:
                    _capture_failures.append(dict(path=path,error=traceback.format_exc()))
                    del _capture_failures[:-20]
                    print('TOON_CAPTURE_ERROR',_capture_failures[-1]['error'],flush=True)
    except Exception:
        error=traceback.format_exc()
        _errors[key]=dict(signature=signature,error=error)
        for job in list(_output_jobs.values()):
            if job['key']==key and job['status']=='QUEUED':
                _finish_output(job,'FAILED','预览生成失败，未导出线图',context.scene)
        for target,path in _captures.copy():
            if target==key:
                _captures.remove((target,path))
                _capture_failures.append(dict(path=path,error='Preview failed before capture: '+error))
        del _capture_failures[:-20]
        print('TOON_VIEWPORT_ERROR',error,flush=True)
        if state is not None:
            state.cancel_view_work();state.cancel_preparation();state.cancel_geometry_preparation()
            state.error=error


@persistent
def _changed(scene, graph):
    global _epoch
    updates=[dict(type=type(update.id).__name__,name=update.id.name,
        geometry=update.is_updated_geometry,transform=update.is_updated_transform) for update in graph.updates]
    if updates:
        _recent_updates.append(updates)
        del _recent_updates[:-8]
        # UI settings update the Scene and collection visibility bookkeeping,
        # even when every source mesh is unchanged. Final evaluated geometry
        # and transforms carry their own depsgraph updates; target membership
        # is also checked on every draw. Frame/undo/redo have separate hooks.
        data_types={'Mesh','Armature','Key','Lattice','Curve','Curves','MetaBall','Volume','GeometryNodeTree','Action','Material'}
        object_types={'MESH','ARMATURE','LATTICE','CURVE','CURVES','SURFACE','META','VOLUME','GREASEPENCIL'}
        relevant=any(type(update.id).__name__ in data_types or
            (isinstance(update.id,bpy.types.Object) and update.id.type in object_types and
             (update.is_updated_geometry or update.is_updated_transform)) for update in graph.updates)
        # Explicit selectors also depend on collection membership and ID names.
        if not relevant and selector_ui.has_bindings(scene):
            relevant=any(isinstance(update.id,(bpy.types.Collection,bpy.types.Object,bpy.types.Material)) for update in graph.updates)
        if relevant:
            _epoch += 1
            _scene_epochs[scene.as_pointer()]=_epoch
        _redraw()


@persistent
def _frame_changed(scene=None,*unused):
    global _epoch,_reset_epoch
    _epoch += 1
    if isinstance(scene,bpy.types.Scene):_scene_epochs[scene.as_pointer()]=_epoch
    else:_reset_epoch=_epoch
    _redraw()



@persistent
def _save_pre(*unused):
    for scene in bpy.data.scenes:
        settings=getattr(scene,PROP,None)
        if settings is None or settings.curve_tree is None or scene.library is not None:continue
        try:curve_editor.synchronize(scene)
        except Exception as error:
            settings.curve_error=str(error)
            print('TOON_CURVE_SAVE_ERROR',scene.name,str(error),flush=True)

@persistent
def _exit_pre(*unused):
    # Normal Blender exit does not call addon.unregister or load_pre.
    # Reap only the Popen instances created by this add-on.
    _cancel_background_exports()
    _stop_phase_jobs()


def _cancel_background_exports():
    # Each export owner must get its own cleanup opportunity on load/exit.
    for owner in (render_output, sequence_output):
        try:
            owner.cancel_all()
        except Exception as error:
            print('TOON_EXPORT_CLEANUP_ERROR', owner.__name__, str(error), flush=True)


@persistent
def _load_pre(*unused):
    global _epoch,_reset_epoch
    selector_picker.clear_all()
    _cancel_background_exports()
    for state in _states.values():
        state.close()
    _states.clear()
    _captures.clear()
    _errors.clear()
    _style_cache.clear()
    for job in list(_output_jobs.values()):
        if job['status']=='QUEUED':_finish_output(job,'CANCELLED','工程已关闭，导出已取消')
    for token in list(_camera_runs):_close_camera_run(token)
    _stop_phase_jobs();chain_ui.clear_runtime_cache();map_amount_ui.clear_runtime_cache();dash_ui.clear_runtime_cache();_chain_input_cache.clear()
    _scene_epochs.clear()
    _epoch += 1
    _reset_epoch=_epoch


def _tick():
    if not _registered:
        return None
    scenes_by_pointer={scene.as_pointer():scene for scene in bpy.data.scenes}
    for job in sequence_output.jobs():
        if job['status']!='QUEUED':continue
        scene=scenes_by_pointer.get(job['scene_key'][0])
        if scene is None or not any(layer.as_pointer()==job['scene_key'][1] for layer in scene.view_layers):
            sequence_output.cancel(job['id'])
    sequence_output.poll()
    latest_sequence_by_scene={}
    for job in sequence_output.jobs():
        scene=scenes_by_pointer.get(job['scene_key'][0])
        if scene is None:continue
        latest_sequence_by_scene[scene.as_pointer()]=job
    for pointer,job in latest_sequence_by_scene.items():
        scene=scenes_by_pointer[pointer]
        settings=getattr(scene,PROP)
        status=job['status'];completed=job.get('completed_count',0);total=job.get('total_count',len(job.get('frames',())))
        message={'QUEUED':'序列处理中','COMPLETE':'序列导出完成','CANCELLED':'序列已取消，已发布的完整帧保留',
                 'FAILED':'序列导出失败，请检查输出目录和设置'}.get(status,'序列处理中')
        message+=' · 已发布 '+str(completed)+' / '+str(total)+' 帧'
        if settings.sequence_message!=message:settings.sequence_message=message;_redraw()
        if settings.sequence_error!=(status=='FAILED'):settings.sequence_error=status=='FAILED'
        if settings.sequence_message_layer!=job['view_layer']:settings.sequence_message_layer=job['view_layer']
    for job in render_output.poll():
        scene=next((s for s in bpy.data.scenes if s.as_pointer()==job['scene_key'][0]),None)
        _finish_output(job,job['status'],job['message'],scene,job.get('result'))
        render_output._receipt(job)
    live=set()
    curve_scenes=set()
    for window in bpy.context.window_manager.windows:
        navigating=_navigation_active(window)
        if window.scene.as_pointer() not in curve_scenes:
            curve_scenes.add(window.scene.as_pointer())
            settings=getattr(window.scene,PROP)
            try:
                if window.scene.library is None and settings.curve_tree is not None:curve_editor.synchronize(window.scene)
            except Exception as error:
                if settings.curve_error!=str(error):
                    settings.curve_error=str(error);_redraw()
        for area in window.screen.areas:
            if area.type!='VIEW_3D':
                continue
            enabled=getattr(window.scene,PROP).enabled and viewport_scope.enabled(window.screen,area,area.spaces.active)
            needs_redraw=False
            for region in area.regions:
                if region.type=='WINDOW' and enabled:
                    key=(window.as_pointer(),area.as_pointer(),region.as_pointer(),
                         window.scene.as_pointer(),window.view_layer.as_pointer())
                    live.add(key)
                    state=_states.get(key)
                    needs_redraw|=(key not in _errors and (state is None or not state.refined))
                    needs_redraw|=any(target==key for target,path in _captures)
                    needs_redraw|=any(job['key']==key and job['status']=='QUEUED' for job in _output_jobs.values())
                    needs_redraw|=navigating
            if needs_redraw:
                area.tag_redraw()
    for key in list(_states):
        if key not in live:
            _states.pop(key).close()
    for key in list(_errors):
        if key not in live:_errors.pop(key)
    for key,path in _captures.copy():
        if key not in live:
            _captures.remove((key,path))
            _capture_failures.append(dict(path=path,error='Capture cancelled: target viewport/scene closed or preview disabled'))
    del _capture_failures[:-20]
    for job in list(_output_jobs.values()):
        if job['status']=='QUEUED' and job['key'] not in live:
            scene=next((s for s in bpy.data.scenes if s.as_pointer()==job['key'][3]),None)
            _finish_output(job,'CANCELLED','视口已关闭或描边已停用，导出已取消',scene)
    scenes={scene.as_pointer() for scene in bpy.data.scenes}
    for pointer in list(_scene_epochs):
        if pointer not in scenes:_scene_epochs.pop(pointer)
    for cache_key in list(_style_cache):
        if cache_key[0] not in scenes:_style_cache.pop(cache_key)
    for pointer in list(_chain_input_cache):
        if pointer not in scenes:_chain_input_cache.pop(pointer)
    return 0.016 if _camera_runs or any(s.geometry_preparation is not None or s.preparation is not None or s.style_preparation is not None or s.render_preparation is not None or s.phase_snapshot is not None or s.phase_ticket is not None for s in _states.values()) else 0.1


def _preset(scene):
    settings=getattr(scene,PROP)
    if settings.preset_text is None:raise ValueError('请先载入样式文档和对象绑定')
    key=(scene.as_pointer(),settings.preset_text.as_pointer(),settings.preset_revision)
    if key not in _style_cache:
        wrapped=json.loads(settings.preset_text.as_string())
        compiled=compile_preset(wrapped['document'],wrapped['recipe'])
        for stale in list(_style_cache):
            if stale[0]==key[0]:_style_cache.pop(stale)
        _style_cache[key]=compiled
    return _style_cache[key]


def effective_preset(scene,*,static_plan=None,evaluated_scene=None):
    """Freeze one detached pose; author operations keep using _preset."""
    plan=_preset(scene) if static_plan is None else static_plan
    document=parameter_animation.resolve(scene,plan['raw'],evaluated_scene=evaluated_scene)
    if document is plan['raw']:return plan
    effective=compile_preset(document,plan['recipe_raw'])
    effective['parameter_animation']=parameter_animation.receipt(scene,evaluated_scene=evaluated_scene)
    return effective


def set_preset(scene,document,recipe,*,chain_controls_payload=None,map_amounts_payload=None,dash_patterns_payload=None):
    """Persist complete documents in a new owned Text; never rewrite inputs."""
    plan=compile_preset(document,recipe)
    settings=getattr(scene,PROP)
    parameter_animation.validate_registry(scene,document)
    pending_chain_controls=None;pending_map_amounts=None;pending_dash_patterns=None
    bindings=None
    if (settings.chain_controls_json or chain_controls_payload is not None or settings.map_amounts_json or map_amounts_payload is not None
            or settings.dash_patterns_json or dash_patterns_payload is not None or len(settings.amount_tracks)):
        bindings=source_bindings.recover(plan['raw'])
    if len(settings.amount_tracks):amount_animation.validate_registry(scene,bindings['actuals'])
    if settings.chain_controls_json or chain_controls_payload is not None:
        previous=chain_ui._decode(settings.chain_controls_json) if settings.chain_controls_json else None
        pending_chain_controls=chain_controls.merge_controls(bindings,previous=previous,explicit=chain_controls_payload)
    if settings.map_amounts_json or map_amounts_payload is not None:
        previous_amounts=chain_ui._decode(settings.map_amounts_json) if settings.map_amounts_json else None
        pending_map_amounts=(map_amount_controls.normalize(bindings,map_amounts_payload) if map_amounts_payload is not None else
                            map_amount_controls.merge(bindings,previous_amounts,plan['raw']))
    if settings.dash_patterns_json or dash_patterns_payload is not None:
        previous_dashes=chain_ui._decode(settings.dash_patterns_json) if settings.dash_patterns_json else None
        pending_dash_patterns=(dash_controls.normalize(bindings,dash_patterns_payload) if dash_patterns_payload is not None else
                               dash_controls.merge(bindings,previous_dashes))
    for group in plan['groups']:
        identities={}
        for slot,brush in group['brushes'].items():
            meta=group['brush_meta'][slot];uid=meta['uid'] or slot
            signature=(brush,meta['native_size'])
            if uid in identities and identities[uid]!=signature:
                raise ValueError('Actual brush identity has conflicting style: '+uid)
            identities[uid]=signature
    # Huge single-line insertion stalled in the target Blender build.
    # Pretty JSON retains every value while keeping lines short.
    curve_editor.commit_pending(scene)
    body=json.dumps(dict(schema='toon.viewport-persisted-style.v1',document=document,recipe=recipe),ensure_ascii=False,indent=2)
    settings=getattr(scene,PROP);previous_text=settings.preset_text
    previous_element_controls={g.name:(g.line_set_id,g.elements_only) for g in settings.groups}
    text=bpy.data.texts.new('.toon_viewport_style.'+uuid.uuid4().hex)
    text.use_fake_user=False;text['toon_viewport_preview_reclaimable']=True
    text['toon_viewport_preview_owned']=True
    text.from_string(body)
    curve_editor.detach(scene)
    settings.preset_text=text;settings.preset_revision+=1
    settings.groups.clear()
    for group in sorted(plan['groups'],key=lambda g:g['priority']):
        item=settings.groups.add();item.name=group['id'];item.label=group['label'] or group['id']
        explicit=group.get('element_mapping_explicit',{})
        old=previous_element_controls.get(group['id'],(1,False))
        item.line_set_id=group['source_line_set_id'] if explicit.get('line_set_id') else old[0]
        item.elements_only=group['output_to_elements_only'] if explicit.get('elements_only') else old[1]
    if settings.style_mode!='PRESET':settings.style_mode='PRESET'
    else:
        for element in settings.elements:_sync_element_groups(element,settings)
    _style_cache.clear()
    _style_cache[(scene.as_pointer(),text.as_pointer(),settings.preset_revision)]=plan
    _chain_input_cache.pop(scene.as_pointer(),None)
    if pending_chain_controls is not None:
        chain_ui.initialize(scene,plan,explicit=pending_chain_controls)
    if pending_map_amounts is not None:
        map_amount_ui.initialize(scene,plan,explicit=pending_map_amounts)
    if pending_dash_patterns is not None:
        dash_ui.initialize(scene,plan,explicit=pending_dash_patterns)
    _touch(settings,bpy.context)
    if (previous_text is not None and previous_text.library is None and previous_text.users==0
            and not previous_text.use_fake_user and previous_text.get('toon_viewport_preview_reclaimable')):
        bpy.data.texts.remove(previous_text)
    return plan['binding_report']


class TOONVIEWPORT_OT_load_style(bpy.types.Operator):
    bl_idname='toon_viewport.load_style'
    bl_label='载入样式与绑定'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        settings=getattr(context.scene,PROP)
        try:
            paths=[Path(bpy.path.abspath(p)) for p in (settings.document_path,settings.recipe_path)]
            if any(not p.is_file() or p.stat().st_size>16*1024*1024 for p in paths):
                raise ValueError('需要两个有效且不超过 16 MiB 的 JSON 文件')
            document,recipe=[json.loads(p.read_text(encoding='utf-8-sig')) for p in paths]
            report=set_preset(context.scene,document,recipe)
            self.report({'INFO'},f"已载入 {report['group_count']} 个样式组")
            return {'FINISHED'}
        except Exception as error:
            settings.curve_error=str(error);_redraw()
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_PT_preview(bpy.types.Panel):
    bl_label='二维描边 · 自动预览'
    bl_idname='TOONVIEWPORT_PT_preview'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='二维描边'

    def draw(self,context):
        layout=self.layout
        settings=getattr(context.scene,PROP)
        layout.prop(settings,'enabled')
        local_enabled=viewport_scope.enabled(context.screen,context.area,context.space_data)
        layout.operator('toon_viewport.local_visibility',text='此视口显示描边',depress=local_enabled,icon='HIDE_OFF' if local_enabled else 'HIDE_ON')
        layout.prop(settings,'display_mode')
        layout.separator()
        row=layout.row(align=True);row.enabled=settings.enabled and local_enabled
        row.operator('toon_viewport.export_png',text='导出透明线图',icon='EXPORT')
        row.operator('toon_viewport.export_camera_png',text='相机线图',icon='CAMERA_DATA')
        layout.operator('toon_viewport.export_render_png',text='渲染线图（后台）',icon='RENDER_STILL')
        key=_output_key(context)
        pending=[j for j in output_jobs() if j['status']=='QUEUED' and (
            j.get('key')==key or tuple(j.get('scene_key',()))==(context.scene.as_pointer(),context.view_layer.as_pointer()))]
        if pending:row.operator('toon_viewport.cancel_output',text='',icon='CANCEL')
        if settings.output_message:
            layout.label(text=settings.output_message,icon='ERROR' if settings.output_error else 'INFO')
        if settings.last_output_path:
            row=layout.row(align=True)
            row.operator('toon_viewport.add_output_node',text='添加主图到合成',icon='NODE_COMPOSITING')
            if settings.last_output_elements:row.menu('TOONVIEWPORT_MT_output_passes',text='',icon='DOWNARROW_HLT')
        layout.label(text='相机输出沿用视口可见性与视口级细分')
        layout.separator()
        layout.prop(settings,'style_mode')
        if settings.style_mode=='PRESET':
            if settings.preset_text is not None:
                layout.label(text=f'已载入 {len(settings.groups)} 个样式组')
                layout.prop(settings,'show_import')
            if settings.preset_text is None or settings.show_import:
                layout.prop(settings,'document_path');layout.prop(settings,'recipe_path')
                layout.operator('toon_viewport.load_style')
            author_channels.draw(layout,settings)
            layout.prop(settings,'width_multiplier')
            layout.prop(settings,'intersection_enabled');layout.prop(settings,'opacity')
            if settings.preset_text is not None:
                try:group_ui.draw_header(layout,_preset(context.scene))
                except Exception:layout.label(text='当前样式暂不能编辑组',icon='ERROR')
            for item in selector_ui.ordered(context.scene,settings.groups):
                box=layout.box();box.prop(item,'enabled',text=item.label)
                row=box.row(align=True);row.enabled=item.enabled
                row.prop(item,'width_scale');row.prop(item,'opacity')
                if settings.preset_text is not None:
                    try:
                        plan=_preset(context.scene)
                        group_ui.draw_group(box,plan,item.name)
                        brush_ui.draw(box,plan,item.name,channel='VISIBLE' if settings.show_visible_lines else 'HIDDEN')
                        line_function_ui.draw_mask(box,plan,item.name)
                        selector_ui.draw(box,plan,item.name)
                        edge_active=edge_ui.draw(box,plan,item.name,channel='VISIBLE' if settings.show_visible_lines else 'HIDDEN')
                        wire_box=box.column();wire_box.enabled=not edge_active
                        wire_ui.draw(wire_box,plan,item.name,channel='VISIBLE' if settings.show_visible_lines else 'HIDDEN')
                        ix_ui.draw(box,plan,item.name,channel='VISIBLE' if settings.show_visible_lines else 'HIDDEN')
                    except Exception:box.label(text='当前样式暂不能设置线框来源',icon='ERROR')
        else:
            for name in ('line_width','line_color','opacity'):layout.prop(settings,name)
            row=layout.row(align=True);row.prop(settings,'silhouette');row.prop(settings,'open_edges')
            row=layout.row(align=True);row.prop(settings,'marked_edges');row.prop(settings,'material_edges')
            layout.prop(settings,'wireframe_edges')
            layout.prop(settings,'crease_edges')
            if settings.crease_edges:layout.prop(settings,'crease_angle')
        if settings.curve_error:layout.label(text=settings.curve_error,icon='ERROR')
        if settings.style_mode=='PRESET':
            curve_editor.draw(layout,context.scene);chain_ui.draw(layout,context.scene);map_amount_ui.draw(layout,context.scene);dash_ui.draw(layout,context.scene)
        layout.prop(settings,'visibility')
        layout.separator()
        layout.label(text='移动时显示原视图，停止后自动更新')
        states=[s for k,s in _states.items() if k[1]==context.area.as_pointer() and k[3]==context.scene.as_pointer()]
        for state in states[:1]:
            if state.error:
                layout.label(text='预览出错，详见控制台',icon='ERROR')
            else:
                layout.label(text={'READY':'预览已更新','MOVING':'移动中，显示原视图',
                                   'PREPARING':'正在生成预览，仍可移动视图',
                                   'WAITING':'停止移动后更新预览'}[state.status])



class TOONVIEWPORT_OT_edit_curves(bpy.types.Operator):
    bl_idname='toon_viewport.edit_curves';bl_label='载入原控制点';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:curve_editor.initialize(context.scene)
        except Exception as error:
            context.scene.toon_viewport_preview.curve_error=str(error);_redraw()
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}

class TOONVIEWPORT_OT_restore_curves(bpy.types.Operator):
    bl_idname='toon_viewport.restore_curves';bl_label='恢复载入时曲线';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:curve_editor.restore(context.scene)
        except Exception as error:
            context.scene.toon_viewport_preview.curve_error=str(error);_redraw()
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}

class TOONVIEWPORT_OT_discard_curves(bpy.types.Operator):
    bl_idname='toon_viewport.discard_curves';bl_label='放弃未应用修改并重载';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:curve_editor.initialize(context.scene,discard_pending=True)
        except Exception as error:
            context.scene.toon_viewport_preview.curve_error=str(error);_redraw()
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}


def _check_output_operator(op,context):
    op.filename_ext='.exr' if op.file_format=='OPEN_EXR' else '.png'
    return ExportHelper.check(op,context)


def _invoke_output_operator(op,context,event,mode):
    op._target_key=_output_key(context)
    op.filename_ext='.exr' if op.file_format=='OPEN_EXR' else '.png'
    if not op.filepath:
        parent=Path(bpy.data.filepath).parent if bpy.data.filepath else Path(bpy.app.tempdir)
        stem=({'CAMERA':'toon-camera-','RENDER':'toon-render-','ELEMENTS':'toon-elements-'}.get(mode,'toon-lines-'))+str(context.scene.frame_current).zfill(4)
        path=parent/(stem+op.filename_ext);index=1
        while path.exists():path=parent/(stem+'-'+str(index)+op.filename_ext);index+=1
        op.filepath=str(path)
    return ExportHelper.invoke(op,context,event)


def _execute_output_operator(op,context,mode):
    request={'CAMERA':request_camera_output,'RENDER':request_render_output,'VIEW':request_output,'ELEMENTS':request_render_elements}[mode]
    try:
        if op.properties.is_property_set('file_format'):
            expected='.exr' if op.file_format=='OPEN_EXR' else '.png'
            if Path(op.filepath).suffix.lower()!=expected:
                raise ValueError('输出格式与文件扩展名不一致')
        key=getattr(op,'_target_key',None)
        if key is None:request(op.filepath,context)
        else:
            window=next((w for w in context.window_manager.windows if w.as_pointer()==key[0]),None)
            area=next((a for a in window.screen.areas if a.as_pointer()==key[1] and a.type=='VIEW_3D'),None) if window else None
            region=next((r for r in area.regions if r.as_pointer()==key[2]),None) if area else None
            if (region is None or window.scene.as_pointer()!=key[3]
                    or window.view_layer.as_pointer()!=key[4]):raise ValueError('原导出视口已关闭或更换场景')
            with context.temp_override(window=window,area=area,region=region,scene=window.scene,view_layer=window.view_layer):
                request(op.filepath,context)
    except Exception as error:
        settings=getattr(context.scene,PROP)
        settings.output_message=str(error);settings.output_error=True
        if mode=='ELEMENTS':settings.element_output_message=str(error);settings.element_output_error=True
        op.report({'ERROR'},str(error));return {'CANCELLED'}
    op.report({'INFO'},'线图导出已排队');return {'FINISHED'}


class TOONVIEWPORT_OT_export_png(bpy.types.Operator,ExportHelper):
    bl_idname='toon_viewport.export_png';bl_label='导出透明线图'
    filename_ext='.png'
    filter_glob: StringProperty(default='*.png;*.exr',options={'HIDDEN'})
    file_format: EnumProperty(name='输出格式',items=_OUTPUT_FORMATS,default='PNG')
    check_existing: BoolProperty(default=False,options={'HIDDEN'})

    @classmethod
    def poll(cls,context):
        return context.window is not None and getattr(context.scene,PROP).enabled

    def invoke(self,context,event):return _invoke_output_operator(self,context,event,'VIEW')
    def check(self,context):return _check_output_operator(self,context)
    def execute(self,context):return _execute_output_operator(self,context,'VIEW')


class TOONVIEWPORT_OT_export_camera_png(bpy.types.Operator,ExportHelper):
    bl_idname='toon_viewport.export_camera_png';bl_label='导出相机线图'
    bl_description='按场景相机和渲染尺寸导出，使用当前视口可见性与视口级细分，不启用渲染级细分'
    filename_ext='.png'
    filter_glob: StringProperty(default='*.png;*.exr',options={'HIDDEN'})
    file_format: EnumProperty(name='输出格式',items=_OUTPUT_FORMATS,default='PNG')
    check_existing: BoolProperty(default=False,options={'HIDDEN'})

    @classmethod
    def poll(cls,context):
        return context.window is not None and getattr(context.scene,PROP).enabled

    def invoke(self,context,event):return _invoke_output_operator(self,context,event,'CAMERA')
    def check(self,context):return _check_output_operator(self,context)
    def execute(self,context):return _execute_output_operator(self,context,'CAMERA')


class TOONVIEWPORT_OT_export_render_png(bpy.types.Operator,ExportHelper):
    bl_idname='toon_viewport.export_render_png';bl_label='导出渲染线图'
    bl_description='按提交时场景和渲染级几何在后台导出透明线图，可继续操作或取消'
    filename_ext='.png'
    filter_glob: StringProperty(default='*.png;*.exr',options={'HIDDEN'})
    file_format: EnumProperty(name='输出格式',items=_OUTPUT_FORMATS,default='PNG')
    check_existing: BoolProperty(default=False,options={'HIDDEN'})

    @classmethod
    def poll(cls,context):return context.window is not None

    def invoke(self,context,event):return _invoke_output_operator(self,context,event,'RENDER')
    def check(self,context):return _check_output_operator(self,context)
    def execute(self,context):return _execute_output_operator(self,context,'RENDER')


class TOONVIEWPORT_OT_export_elements(bpy.types.Operator,ExportHelper):
    bl_idname='toon_viewport.export_elements';bl_label='导出分层 EXR'
    filename_ext='.exr'
    filter_glob: StringProperty(default='*.exr',options={'HIDDEN'})
    file_format: EnumProperty(name='格式',items=[('OPEN_EXR','多层 EXR','32位线性、预乘 Alpha')],default='OPEN_EXR')
    @classmethod
    def poll(cls,context):return context.window is not None and len(context.scene.toon_viewport_preview.elements)>0
    def invoke(self,context,event):return _invoke_output_operator(self,context,event,'ELEMENTS')
    def check(self,context):return _check_output_operator(self,context)
    def execute(self,context):return _execute_output_operator(self,context,'ELEMENTS')


class TOONVIEWPORT_OT_export_sequence(bpy.types.Operator,ExportHelper):
    bl_idname='toon_viewport.export_sequence';bl_label='导出序列线图'
    bl_description='按活动相机与提交时的场景逐帧后台渲染，不改变当前帧，可继续操作或取消'
    filename_ext='.png'
    filter_glob: StringProperty(default='*.png;*.exr',options={'HIDDEN'})
    file_format: EnumProperty(name='输出格式',items=_OUTPUT_FORMATS,default='PNG')
    check_existing: BoolProperty(default=False,options={'HIDDEN'})

    @classmethod
    def poll(cls,context):return context.scene is not None and context.view_layer is not None

    def invoke(self,context,event):
        settings=getattr(context.scene,PROP)
        self._sequence_target=(context.scene.as_pointer(),context.view_layer.as_pointer())
        self.file_format='OPEN_EXR' if settings.sequence_elements else settings.sequence_format
        self.filename_ext='.exr' if self.file_format=='OPEN_EXR' else '.png'
        if not self.filepath:
            parent=Path(bpy.data.filepath).parent if bpy.data.filepath else Path(bpy.app.tempdir)
            self.filepath=str(parent/('toon-sequence'+self.filename_ext))
        return ExportHelper.invoke(self,context,event)

    def check(self,context):
        if getattr(context.scene,PROP).sequence_elements:self.file_format='OPEN_EXR'
        return _check_output_operator(self,context)

    def execute(self,context):
        settings=getattr(context.scene,PROP)
        try:
            target=getattr(self,'_sequence_target',None)
            if target is not None and target!=(context.scene.as_pointer(),context.view_layer.as_pointer()):
                raise ValueError('原序列导出场景或视图层已更换，请重新选择输出')
            path=Path(self.filepath)
            if settings.sequence_elements:path=path.with_suffix('.exr')
            else:
                selected=self.file_format if self.properties.is_property_set('file_format') else settings.sequence_format
                expected='.exr' if selected=='OPEN_EXR' else '.png'
                if path.suffix.lower()!=expected:raise ValueError('输出格式与文件扩展名不一致')
            request_sequence_output(path,context)
        except Exception as error:
            settings.sequence_message=str(error);settings.sequence_error=True
            settings.sequence_message_layer=context.view_layer.name
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        self.report({'INFO'},'序列线图已排队');return {'FINISHED'}


class TOONVIEWPORT_OT_cancel_sequence(bpy.types.Operator):
    bl_idname='toon_viewport.cancel_sequence';bl_label='取消此序列'
    job_id: StringProperty(default='')

    def execute(self,context):
        key=(context.scene.as_pointer(),context.view_layer.as_pointer())
        job=next((j for j in sequence_output.jobs() if j['id']==self.job_id),None)
        if job is None or job['status']!='QUEUED' or tuple(job['scene_key'])!=key:
            self.report({'ERROR'},'该序列已结束或不属于当前场景视图层');return {'CANCELLED'}
        if not sequence_output.cancel(self.job_id):
            self.report({'ERROR'},'该序列已结束');return {'CANCELLED'}
        _redraw();return {'FINISHED'}


def _sync_element_groups(item,settings):
    previous={g.name:g.enabled for g in item.group_filters}
    item.group_filters.clear()
    groups=[(g.name,g.label) for g in settings.groups] if settings.style_mode=='PRESET' else [('__BASIC__','基础几何')]
    for uid,label in groups:
        group=item.group_filters.add();group.name=uid;group.label=label
        group.enabled=previous.get(uid,not item.use_group_filter)


class TOONVIEWPORT_OT_manage_element(bpy.types.Operator):
    bl_idname='toon_viewport.manage_element';bl_label='编辑线条元素';bl_options={'REGISTER','UNDO'}
    action: EnumProperty(items=[(s,s,'') for s in ('ADD','REMOVE','COPY','UP','DOWN','SYNC_GROUPS')])
    def execute(self,context):
        settings=context.scene.toon_viewport_preview;items=settings.elements
        index=min(settings.active_element,max(0,len(items)-1))
        if self.action=='ADD':
            if len(items)>=render_elements.MAX_ELEMENTS:
                self.report({'ERROR'},'最多 '+str(render_elements.MAX_ELEMENTS)+' 个元素');return {'CANCELLED'}
            item=items.add();item.name=uuid.uuid4().hex;item.label='线条元素 '+str(len(items))
            for name in render_elements.CATEGORIES:setattr(item,name+'_on',name in render_elements.SUPPORTED[settings.style_mode])
            _sync_element_groups(item,settings);settings.active_element=len(items)-1
        elif not items:return {'CANCELLED'}
        elif self.action=='REMOVE':items.remove(index);settings.active_element=max(0,min(index,len(items)-1))
        elif self.action=='COPY':
            if len(items)>=render_elements.MAX_ELEMENTS:return {'CANCELLED'}
            old=items[index];item=items.add();item.name=uuid.uuid4().hex
            for name in ('label','element_type','depth_min','depth_max','visible_lines_on','hidden_lines_on','use_group_filter','background_enabled'):
                setattr(item,name,getattr(old,name))
            item.label=old.label+' 副本';item.line_set_ids=old.line_set_ids[:];item.background_color=old.background_color[:]
            for name in render_elements.CATEGORIES:setattr(item,name+'_on',getattr(old,name+'_on'))
            for old_group in old.group_filters:
                group=item.group_filters.add();group.name=old_group.name;group.label=old_group.label;group.enabled=old_group.enabled
            settings.active_element=len(items)-1
        elif self.action=='SYNC_GROUPS':_sync_element_groups(items[index],settings)
        else:
            target=index+(-1 if self.action=='UP' else 1)
            if 0<=target<len(items):items.move(index,target);settings.active_element=target
        return {'FINISHED'}


class TOONVIEWPORT_UL_elements(bpy.types.UIList):
    def draw_item(self,context,layout,data,item,icon,active_data,active_propname,index):
        layout.prop(item,'label',text='',emboss=False,icon='RENDERLAYERS')


class TOONVIEWPORT_PT_elements(bpy.types.Panel):
    bl_label='分层线图输出';bl_idname='TOONVIEWPORT_PT_elements'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='二维描边'
    bl_options={'DEFAULT_CLOSED'}
    def draw(self,context):
        layout=self.layout;settings=context.scene.toon_viewport_preview
        layout.label(text='按当前自主来源分类筛选')
        layout.label(text='仅筛选已启用的来源与遮挡通道')
        layout.label(text='深度及原版八类等价性仍待完成')
        if settings.style_mode=='PRESET':
            for group in settings.groups:
                box=layout.box();box.label(text=group.label)
                box.prop(group,'line_set_id');box.prop(group,'elements_only')
        else:
            layout.prop(settings,'basic_line_set_id');layout.prop(settings,'basic_elements_only')
        row=layout.row();row.template_list('TOONVIEWPORT_UL_elements','',settings,'elements',settings,'active_element',rows=3)
        col=row.column(align=True)
        for action,icon in (('ADD','ADD'),('REMOVE','REMOVE'),('COPY','DUPLICATE'),('UP','TRIA_UP'),('DOWN','TRIA_DOWN')):
            col.operator('toon_viewport.manage_element',text='',icon=icon).action=action
        if settings.elements:
            item=settings.elements[min(settings.active_element,len(settings.elements)-1)]
            box=layout.box();box.prop(item,'label');box.prop(item,'element_type')
            if item.element_type=='DEPTH':
                row=box.row(align=True);row.prop(item,'depth_min');row.prop(item,'depth_max')
                if item.depth_min>=item.depth_max:box.label(text='远距离须大于近距离',icon='ERROR')
            row=box.row(align=True);row.prop(item,'visible_lines_on');row.prop(item,'hidden_lines_on')
            row=box.row(align=True)
            for i in range(8):row.prop(item,'line_set_ids',index=i,text=str(i+1),toggle=True)
            box.prop(item,'use_group_filter')
            if item.use_group_filter:
                for group in item.group_filters:box.prop(group,'enabled',text=group.label)
                box.operator('toon_viewport.manage_element',text='刷新样式组列表',icon='FILE_REFRESH').action='SYNC_GROUPS'
            for name in render_elements.CATEGORIES:
                supported=name in render_elements.SUPPORTED[settings.style_mode]
                row=box.row();row.enabled=supported or getattr(item,name+'_on')
                row.prop(item,name+'_on')
                if not supported and getattr(item,name+'_on'):box.label(text='当前模式不可用，请取消此项',icon='INFO')
            box.prop(item,'background_enabled')
            if item.background_enabled:box.prop(item,'background_color')
        layout.prop(settings,'element_budget_mb')
        layout.operator('toon_viewport.export_elements',icon='EXPORT')
        pending=[j for j in render_output.jobs() if j['status']=='QUEUED' and 'element_config' in j and
            tuple(j['scene_key'])==(context.scene.as_pointer(),context.view_layer.as_pointer())]
        if pending:
            names={'LOADING':'载入快照','CAPTURING':'读取渲染网格','STYLING':'整理笔画','DRAWING':'绘制主图',
                   'ELEMENT':'绘制元素','ELEMENT_DRAWING':'绘制元素','PACKING_ELEMENTS':'写入多层图像'}
            row=layout.row(align=True)
            row.label(text=names.get(pending[0].get('worker_stage'),'后台处理中'))
            row.operator('toon_viewport.cancel_output',text='取消',icon='CANCEL').job_id=pending[0]['id']
        if settings.element_output_message:layout.label(text=settings.element_output_message,icon='ERROR' if settings.element_output_error else 'INFO')
        if settings.last_output_path and settings.last_output_elements:
            row=layout.row(align=True)
            row.operator('toon_viewport.add_output_node',text='添加主图到合成',icon='NODE_COMPOSITING')
            if settings.last_output_elements:row.menu('TOONVIEWPORT_MT_output_passes',text='',icon='DOWNARROW_HLT')


class TOONVIEWPORT_PT_sequence(bpy.types.Panel):
    bl_label='序列线图输出';bl_idname='TOONVIEWPORT_PT_sequence'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='二维描边'
    bl_options={'DEFAULT_CLOSED'}

    def draw(self,context):
        layout=self.layout;scene=context.scene;settings=getattr(scene,PROP)
        layout.label(text='按活动相机逐帧渲染，可继续操作或取消')
        layout.prop(settings,'sequence_use_scene_range')
        row=layout.row(align=True)
        if settings.sequence_use_scene_range:
            row.prop(scene,'frame_start',text='起始帧');row.prop(scene,'frame_end',text='结束帧')
        else:
            row.prop(settings,'sequence_start');row.prop(settings,'sequence_end')
        layout.prop(settings,'sequence_step')
        layout.prop(settings,'sequence_elements')
        row=layout.row();row.enabled=not settings.sequence_elements;row.prop(settings,'sequence_format')
        if settings.sequence_elements:layout.label(text='多层输出固定为 EXR，沿用分层元素设置')
        suffix='.exr' if settings.sequence_elements or settings.sequence_format=='OPEN_EXR' else '.png'
        layout.label(text='文件名前缀逐帧编号：线稿_000001'+suffix)
        if scene.camera is None:layout.label(text='请先设置场景活动相机',icon='INFO')
        layout.operator('toon_viewport.export_sequence',icon='EXPORT')
        key=(scene.as_pointer(),context.view_layer.as_pointer())
        matching=[job for job in sequence_output.jobs() if tuple(job['scene_key'])==key]
        names={'QUEUED':'处理中','COMPLETE':'完成','FAILED':'失败','CANCELLED':'已取消'}
        for job in matching:
            box=layout.box();box.label(text=job.get('prefix','序列')+' · '+names.get(job['status'],'处理中'))
            box.label(text='已发布 '+str(job.get('completed_count',0))+' / '+str(job.get('total_count',len(job.get('frames',()))))+' 帧')
            published=job.get('published_frames',[])
            if published:box.label(text='完整文件 '+str(len(published))+' 个 · 最近帧 '+str(published[-1]['frame']))
            progress=job.get('worker_progress',{})
            if not isinstance(progress,dict):progress={}
            if job['status']=='QUEUED':
                if type(progress.get('frame')) is int:box.label(text='当前渲染帧 '+str(progress['frame']))
                else:box.label(text='等待开始渲染')
                row=box.row();row.enabled=not job.get('cancel_requested',False)
                row.operator('toon_viewport.cancel_sequence',text='取消中' if job.get('cancel_requested') else '取消此序列',icon='CANCEL').job_id=job['id']
        if settings.sequence_message and settings.sequence_message_layer==context.view_layer.name:
            layout.label(text=settings.sequence_message,icon='ERROR' if settings.sequence_error else 'INFO')


class TOONVIEWPORT_OT_cancel_output(bpy.types.Operator):
    bl_idname='toon_viewport.cancel_output';bl_label='取消线图导出'
    job_id: StringProperty(default='')
    def execute(self,context):
        if self.job_id:
            job=next((j for j in render_output.jobs() if j['id']==self.job_id),None)
            if (job is None or 'element_config' not in job or job['status']!='QUEUED' or
                    tuple(job['scene_key'])!=(context.scene.as_pointer(),context.view_layer.as_pointer())):
                self.report({'ERROR'},'该元素任务已结束或不属于当前场景视图层');return {'CANCELLED'}
            render_output.cancel(self.job_id);_redraw();return {'FINISHED'}
        key=_output_key(context)
        for job in list(_output_jobs.values()):
            if job['key']==key:cancel_output(job['id'],context.scene)
        for job in render_output.jobs():
            if tuple(job['scene_key'])==(context.scene.as_pointer(),context.view_layer.as_pointer()):
                cancel_output(job['id'],context.scene)
        return {'FINISHED'}


class TOONVIEWPORT_OT_add_output_node(bpy.types.Operator):
    bl_idname='toon_viewport.add_output_node';bl_label='添加线图合成节点'
    bl_options={'REGISTER','UNDO'}
    pass_name: StringProperty(default='Combined')

    def execute(self,context):
        settings=getattr(context.scene,PROP)
        effect,tree=active_binding(context.scene)
        image=None;node=None;copy=None;selected=None
        try:
            if tree is None:raise ValueError('请先在合成器中建立合成节点组')
            if context.scene.library is not None:raise ValueError('请在可编辑的本地场景中添加节点')
            path=Path(settings.last_output_path)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=settings.last_output_sha256:
                raise ValueError('最近导出的线图已移动或改变，请重新导出')
            if tree.library is not None or tree.users>1:copy=tree.copy();copy.use_fake_user=False
            target=copy or tree
            image=bpy.data.images.load(str(path),check_existing=False)
            # Assigning the Image node resolves Blender's lazy multilayer
            # image type and pass sockets. Before this it may report IMAGE /
            # TARGA even though the file is a valid multipart OpenEXR.
            node=target.nodes.new('CompositorNodeImage');node.image=image
            from .._ui_kit import apply_node_theme

            apply_node_theme(node, 'toon')
            mapping=json.loads(settings.last_output_elements or '[]')
            multi=image.type=='MULTILAYER'
            if multi:
                from .exr_header import inspect_exr
                header=inspect_exr(path,max_parts=len(mapping)+1)
                expected={'ToonLines.Combined'}|{'ToonLines.'+e['pass_name'] for e in mapping}
                if not header['chunks_checked'] or {p['name'] for p in header['parts']}!=expected:
                    raise ValueError('多层线图与已保存的元素身份不匹配')
                if self.pass_name not in {'Combined'}|{e['pass_name'] for e in mapping}:
                    raise ValueError('该元素不属于最近一次导出的线图')
                image.colorspace_settings.name='Linear Rec.709';image.alpha_mode='PREMUL'
            elif image.file_format=='OPEN_EXR':
                image.colorspace_settings.name='Linear Rec.709';image.alpha_mode='PREMUL'
            elif image.file_format=='PNG':
                image.colorspace_settings.name='sRGB';image.alpha_mode='STRAIGHT'
            else:raise ValueError('最近导出不是受支持的 PNG 或 EXR 线图')
            image.pack()
            if hashlib.sha256(bytes(image.packed_file.data)).hexdigest()!=settings.last_output_sha256:
                raise ValueError('载入期间线图发生改变，请重新导出')
            if multi:
                node.layer='ToonLines'
                socket=next((o for o in node.outputs if o.identifier==self.pass_name),None)
                if socket is None or socket.bl_idname!='NodeSocketColor':raise ValueError('找不到对应的线图元素插槽')
                label='主图' if self.pass_name=='Combined' else next(e['label'] for e in mapping if e['pass_name']==self.pass_name)
                selected=target.nodes.new('NodeReroute');selected.label=label
                target.links.new(socket,selected.inputs[0])
                selected['toon_element_pass']=self.pass_name
                node['toon_element_passes']=json.dumps(mapping,ensure_ascii=False)
            node.label='二维描边 · '+path.stem
            node.location=(max((n.location.x for n in target.nodes if n!=node),default=0)+240,0)
            if selected is not None:selected.location=(node.location.x+240,node.location.y)
            node['toon_viewport_export_sha256']=settings.last_output_sha256
            if copy is not None:attach_tree(context.scene,effect,copy)
        except Exception as error:
            if selected is not None:(copy or tree).nodes.remove(selected)
            if node is not None:(copy or tree).nodes.remove(node)
            if image is not None and image.users==0:bpy.data.images.remove(image)
            if copy is not None and copy.users==0:bpy.data.node_groups.remove(copy)
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        self.report({'INFO'},'已添加打包线图节点，可连接到 Alpha Over 前景')
        return {'FINISHED'}


class TOONVIEWPORT_MT_output_passes(bpy.types.Menu):
    bl_label='选择线图元素';bl_idname='TOONVIEWPORT_MT_output_passes'
    def draw(self,context):
        settings=context.scene.toon_viewport_preview
        try:mapping=json.loads(settings.last_output_elements or '[]')
        except ValueError:mapping=[]
        for entry in mapping:
            op=self.layout.operator('toon_viewport.add_output_node',text=entry['label'],icon='NODE_COMPOSITING')
            op.pass_name=entry['pass_name']


_CLASSES=(PreviewGroup,ElementGroupFilter,PreviewElement,*chain_ui.CLASSES,*map_amount_ui.CLASSES,*dash_ui.CLASSES,*amount_animation.CLASSES,*amount_animation_ui.CLASSES,*wire_ui.CLASSES,*edge_ui.CLASSES,*ix_ui.CLASSES,PreviewSettings,TOONVIEWPORT_OT_load_style,TOONVIEWPORT_OT_edit_curves,TOONVIEWPORT_OT_restore_curves,TOONVIEWPORT_OT_discard_curves,TOONVIEWPORT_OT_export_png,TOONVIEWPORT_OT_export_camera_png,TOONVIEWPORT_OT_export_render_png,TOONVIEWPORT_OT_export_elements,TOONVIEWPORT_OT_export_sequence,TOONVIEWPORT_OT_cancel_sequence,TOONVIEWPORT_OT_manage_element,TOONVIEWPORT_UL_elements,TOONVIEWPORT_OT_cancel_output,TOONVIEWPORT_OT_add_output_node,TOONVIEWPORT_MT_output_passes,TOONVIEWPORT_PT_preview,TOONVIEWPORT_PT_elements,TOONVIEWPORT_PT_sequence)


from . import line_ui, vector_ui, vector_scope, author_channels
from . import file_ui, brush_preview, brush_ui, runtime_preferences, line_function_ui
_CLASSES = viewport_scope.CLASSES + parameter_animation.CLASSES + _CLASSES + parameter_animation_ui.CLASSES + selector_ui.CLASSES + selector_picker.CLASSES + group_ui.CLASSES + line_ui.CLASSES + brush_ui.CLASSES + line_function_ui.CLASSES + (brush_preview.TOONVIEWPORT_PreviewEngine,) + file_ui.CLASSES + vector_ui.CLASSES + author_channels.CLASSES
_vector_scene_descriptor=None

def register():
    global _registered,_handler,_vector_scene_descriptor
    if _registered:
        return
    registered=[];installed=False;author_installed=False;vector_installed=False;scope_installed=False;viewport_scope_installed=False;picker_installed=False;brush_installed=False;graph_menu_installed=False;graph_header_installed=False;callbacks=[]
    try:
        if any(hasattr(bpy.types.Scene,name) for name in (PROP,'toon_author_files',vector_ui.VECTOR_PROP)):
            raise RuntimeError('Preview property already owned by another registration')
        vector_scope.preflight()
        for cls in _CLASSES:
            bpy.utils.register_class(cls);registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=PreviewSettings));installed=True
        bpy.types.Scene.toon_author_files=PointerProperty(type=file_ui.AuthorSettings);author_installed=True
        setattr(bpy.types.Scene,vector_ui.VECTOR_PROP,PointerProperty(type=vector_ui.ToonVectorOutputState))
        vector_installed=True;_vector_scene_descriptor=bpy.types.Scene.bl_rna.properties[vector_ui.VECTOR_PROP].as_pointer()
        vector_scope.register(vector_ui.ToonVectorOutputState);scope_installed=True
        brush_ui.register();brush_installed=True
        bpy.types.NODE_MT_add.append(file_ui.author_graph.draw_add);graph_menu_installed=True
        bpy.types.NODE_HT_header.append(file_ui.author_graph.draw_header);graph_header_installed=True
        file_ui.author_graph.ensure_runtime()
        viewport_scope.install();viewport_scope_installed=True
        selector_picker.register();picker_installed=True
        _handler=bpy.types.SpaceView3D.draw_handler_add(_draw,(),'WINDOW','POST_PIXEL')
        for handlers,callback in ((bpy.app.handlers.depsgraph_update_post,_changed),
            (bpy.app.handlers.frame_change_post,_frame_changed),(bpy.app.handlers.load_pre,_load_pre),
            (bpy.app.handlers.save_pre,_save_pre),(bpy.app.handlers.exit_pre,_exit_pre),
            (bpy.app.handlers.undo_post,_frame_changed),(bpy.app.handlers.redo_post,_frame_changed)):
            handlers.append(callback);callbacks.append((handlers,callback))
        _registered=True
        bpy.app.timers.register(_tick,first_interval=0.1,persistent=True)
    except Exception:
        _registered=False
        if bpy.app.timers.is_registered(_tick):bpy.app.timers.unregister(_tick)
        for handlers,callback in callbacks:
            if callback in handlers:handlers.remove(callback)
        if _handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(_handler,'WINDOW');_handler=None
        if viewport_scope_installed:viewport_scope.uninstall()
        if picker_installed:selector_picker.unregister()
        if brush_installed:brush_ui.unregister()
        if graph_menu_installed:bpy.types.NODE_MT_add.remove(file_ui.author_graph.draw_add)
        if graph_header_installed:bpy.types.NODE_HT_header.remove(file_ui.author_graph.draw_header)
        if graph_menu_installed:file_ui.author_graph.clear_runtime()
        if scope_installed:vector_scope.unregister()
        descriptor=bpy.types.Scene.bl_rna.properties.get(vector_ui.VECTOR_PROP)
        if vector_installed and descriptor is not None and descriptor.as_pointer()==_vector_scene_descriptor:delattr(bpy.types.Scene,vector_ui.VECTOR_PROP)
        _vector_scene_descriptor=None
        if author_installed:del bpy.types.Scene.toon_author_files
        if installed:delattr(bpy.types.Scene,PROP)
        for cls in reversed(registered):bpy.utils.unregister_class(cls)
        raise


def unregister():
    global _registered,_handler,_vector_scene_descriptor
    if not _registered:
        return
    _registered=False
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    bpy.types.SpaceView3D.draw_handler_remove(_handler,'WINDOW')
    _handler=None
    _load_pre()
    for handlers,callback in ((bpy.app.handlers.depsgraph_update_post,_changed),
        (bpy.app.handlers.frame_change_post,_frame_changed),(bpy.app.handlers.load_pre,_load_pre),
            (bpy.app.handlers.save_pre,_save_pre),(bpy.app.handlers.exit_pre,_exit_pre),
        (bpy.app.handlers.undo_post,_frame_changed),(bpy.app.handlers.redo_post,_frame_changed)):
        if callback in handlers:
            handlers.remove(callback)
    viewport_scope.uninstall(persist=True)
    selector_picker.unregister()
    brush_ui.unregister()
    runtime_preferences.clear()
    bpy.types.NODE_MT_add.remove(file_ui.author_graph.draw_add)
    bpy.types.NODE_HT_header.remove(file_ui.author_graph.draw_header)
    file_ui.author_graph.clear_runtime()
    vector_scope.unregister()
    descriptor=bpy.types.Scene.bl_rna.properties.get(vector_ui.VECTOR_PROP)
    if descriptor is not None and descriptor.as_pointer()==_vector_scene_descriptor:delattr(bpy.types.Scene,vector_ui.VECTOR_PROP)
    _vector_scene_descriptor=None
    del bpy.types.Scene.toon_author_files
    delattr(bpy.types.Scene,PROP)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    # Dynamic enum strings must outlive their RNA properties.
    curve_editor._ENUMS.clear()
