# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit key authoring for detached production parameters."""
import bpy
from bpy.props import StringProperty,EnumProperty
from . import parameter_animation as P,parameter_overlay as O
_ITEMS={}
_ITEM_LIMIT=16
_EMPTY_ROWS=[('__NONE__','无可动画数值参数','结构、引用和继承值保持静态')]
_ERROR_ROWS=[('__NONE__','请先载入可编辑样式','')]
def _entry(scene):
    """Keep dynamic enum strings alive across callbacks for one compiled plan."""
    from .addon import _preset
    pointer=scene.as_pointer()
    try:
        s=scene.toon_viewport_preview;plan=_preset(scene)
        key=(s.preset_revision,s.parameter_scope,s.parameter_owner)
        cached=_ITEMS.get(pointer)
        if cached is not None and cached['plan'] is plan and cached['key']==key:
            return cached
        _ITEMS.pop(pointer,None)
        specs=tuple(O.targets(plan['raw'],scope=s.parameter_scope,owner=s.parameter_owner,fallback_owner=True));rows=[]
        from .brush_ui import _LABELS
        for spec in specs:
            t=spec['target'];label=_LABELS.get(t['path'][-1],t['path'][-1])
            prefix='/'.join(t['path'][:-1]);component='' if t['component']<0 else ' '+('XYZ'[t['component']])
            rows.append((spec['token'],t['owner'][-8:]+' · '+prefix+' · '+label+component,spec['kind']))
        entry=dict(plan=plan,key=key,specs=specs,rows=rows or _EMPTY_ROWS)
        _ITEMS[pointer]=entry
        while len(_ITEMS)>_ITEM_LIMIT:_ITEMS.pop(next(iter(_ITEMS)))
        return entry
    except Exception:
        _ITEMS.pop(pointer,None)
        raise
def choices(scene):return list(_entry(scene)['specs'])
def items(self,context):
    try:return _entry(self.id_data)['rows']
    except Exception:return _ERROR_ROWS
def selected(scene):
    s=scene.toon_viewport_preview
    return next((r for r in choices(scene) if r['token']==s.parameter_choice),None)
class TOONPARAM_OT_select(bpy.types.Operator):
    bl_idname='toon_viewport.select_parameter_animation';bl_label='选择参数动画';bl_options={'REGISTER','UNDO'}
    scope:EnumProperty(items=[(k,k,'') for k in O.SCOPES]);owner:StringProperty()
    def execute(self,context):
        s=context.scene.toon_viewport_preview;s.parameter_scope=self.scope;s.parameter_owner=self.owner
        rows=choices(context.scene)
        if rows:s.parameter_choice=rows[0]['token']
        self.report({'INFO'},'已选择；在“新参数动画”面板写入关键帧');return {'FINISHED'}
class TOONPARAM_OT_edit(bpy.types.Operator):
    bl_idname='toon_viewport.parameter_animation';bl_label='编辑参数动画';bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[('KEY','插入／更新关键帧',''),('REMOVE','删除当前帧',''),('TOGGLE','启用／停用',''),('SAMPLE','取当前值','')])
    @classmethod
    def poll(cls,context):return context.scene is not None and context.scene.library is None and context.scene.is_editable and context.scene.toon_viewport_preview.style_mode=='PRESET'
    def execute(self,context):
        try:
            scene=context.scene;s=scene.toon_viewport_preview;spec=selected(scene)
            if spec is None:raise ValueError('请选择一个可动画参数')
            t=spec['target'];row=s.parameter_tracks.get(spec['token']);suffix=P.SUFFIX[spec['kind']]
            if self.action=='KEY':P.set_key(scene,t,getattr(s,'parameter_key_'+spec['kind'].lower()))
            elif self.action=='REMOVE':P.remove_key(scene,t)
            elif self.action=='TOGGLE':P.set_enabled(scene,t,not(row is not None and row.enabled))
            else:setattr(s,'parameter_key_'+spec['kind'].lower(),getattr(row,suffix) if row is not None and row.enabled else spec['static'])
            scene.frame_set(scene.frame_current,subframe=scene.frame_subframe);context.view_layer.update();s.parameter_animation_error=''
        except Exception as error:
            context.scene.toon_viewport_preview.parameter_animation_error=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}
class TOONPARAM_PT_animation(bpy.types.Panel):
    bl_label='新参数动画';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='二维描边';bl_options={'DEFAULT_CLOSED'}
    def draw(self,context):
        layout=self.layout;s=context.scene.toon_viewport_preview;layout.prop(s,'parameter_scope');layout.prop(s,'parameter_choice')
        try:spec=selected(context.scene)
        except Exception:spec=None
        if spec is None:layout.label(text='请从笔刷、Line或线条功能选择作者');return
        row=s.parameter_tracks.get(spec['token']);enabled=row is not None and row.enabled
        layout.label(text=('当前动画值：' if enabled else '当前静态值：')+str(getattr(row,P.SUFFIX[spec['kind']]) if enabled else spec['static']))
        layout.prop(s,'parameter_key_'+spec['kind'].lower(),text='待写关键帧')
        line=layout.row(align=True)
        for action,label in [('SAMPLE','取当前值'),('KEY','插入/更新'),('REMOVE','删当前帧')]:line.operator(TOONPARAM_OT_edit.bl_idname,text=label).action=action
        layout.operator(TOONPARAM_OT_edit.bl_idname,text='停用动画 · 恢复静态值' if enabled else '启用参数动画').action='TOGGLE'
        layout.label(text='浮点线性；整数/开关保持前键；静态作者不逐帧改写')
        if s.parameter_animation_error:layout.label(text=s.parameter_animation_error,icon='ERROR')
CLASSES=(TOONPARAM_OT_select,TOONPARAM_OT_edit,TOONPARAM_PT_animation)
