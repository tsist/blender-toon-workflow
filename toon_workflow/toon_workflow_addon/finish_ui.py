# SPDX-License-Identifier: GPL-3.0-or-later
"""Single-workbench CEL finish controls; staging fields do not own animation."""
import math

import bpy
from bpy.props import BoolProperty,EnumProperty,FloatProperty,FloatVectorProperty,PointerProperty,StringProperty
from mathutils import Vector

from . import finish_core as finish, finish_nodes, finish_band

PROP='toon_finish_ui'
_registered=False
_classes_registered=[]
_descriptor=None


class TOONFINISH_Settings(bpy.types.PropertyGroup):
    band_mask_enabled: BoolProperty(name='发束高光遮罩',default=False,options=set())
    band_mask_image: StringProperty(name='遮罩图像',default='',options=set())
    band_mask_uv: StringProperty(name='遮罩UV',default='',options=set())
    highlight_mode: EnumProperty(name='高光形状',items=[('BLINN','半角高光','随光向和视角变化'),('HEAD_BAND','头部参考光带','跟随指定参考的局部X/Z艺术光带')],default='BLINN',options=set())
    band_reference: PointerProperty(name='光带参考',type=bpy.types.Object,options=set())
    band_center: FloatProperty(name='光带高度',default=.3,min=-8,max=8,options=set())
    band_half_width: FloatProperty(name='光带半宽',default=.07,min=.0001,max=8,options=set())
    band_softness: FloatProperty(name='光带边缘过渡',default=.025,min=.0001,max=8,options=set())
    band_curvature: FloatProperty(name='光带两侧抬升',default=.12,min=-4,max=4,options=set())
    highlight_strength: FloatProperty(name='高光强度',default=0,min=0,max=2,options=set())
    highlight_color: FloatVectorProperty(name='高光颜色',subtype='COLOR',size=4,default=(1,.9,.75,1),min=0,max=1,options=set())
    highlight_threshold: FloatProperty(name='高光收紧',default=.85,min=0,max=1,options=set())
    highlight_softness: FloatProperty(name='高光过渡',default=.05,min=.001,max=1,options=set())
    rim_strength: FloatProperty(name='边缘光强度',default=0,min=0,max=2,options=set())
    rim_color: FloatVectorProperty(name='边缘光颜色',subtype='COLOR',size=4,default=(.65,.75,1,1),min=0,max=1,options=set())
    rim_power: FloatProperty(name='边缘光收紧',default=5,min=.1,max=64,options=set())
    rim_lift: FloatProperty(name='边缘光扩展',default=0,min=-1,max=1,options=set())
    light_direction: FloatVectorProperty(name='高光方向（世界）',subtype='DIRECTION',size=3,default=(0,0,1),options=set())
    normal_mode: EnumProperty(name='形体法向',items=[('GEOMETRY','原生表面法向',''),('ARTISTIC','已有艺术法向','独立读取现有头部字段，保留原材质Normal连接')],default='GEOMETRY',options=set())
    normal_modifier: StringProperty(name='艺术法向修改器',options=set())
    sun: PointerProperty(name='方向参考 Sun',type=bpy.types.Object)
    key: EnumProperty(name='强度动画',items=[('rim_strength','边缘光强度',''),('highlight_strength','高光强度','')],default='rim_strength',options=set())
    key_value: FloatProperty(name='关键帧值',default=0,min=0,max=2,options=set())
    message: StringProperty(options={'SKIP_SAVE'})


def _settings(context):return getattr(context.scene,PROP)


def _values(context):
    settings=_settings(context)
    return {key:list(getattr(settings,key)) if isinstance(default,list) else getattr(settings,key)
            for key,default in finish_nodes.defaults().items()}


def _band_values(context):
    settings=_settings(context)
    return {key:getattr(settings,'band_'+key) for key in finish_band.defaults()}


def _mask_values(context):
    settings=_settings(context)
    if not settings.band_mask_enabled:return None
    if settings.highlight_mode!='HEAD_BAND':raise ValueError('发束遮罩仅用于头部参考光带')
    image=bpy.data.images.get(settings.band_mask_image)
    if image is None:raise ValueError('请选择已有的独立Non-Color遮罩图像')
    return dict(image=image,uv_name=settings.band_mask_uv)


def _result(context,report):
    settings=_settings(context)
    if report.get('status')=='valid':
        for key,value in report['settings'].items():setattr(settings,key,value)
        settings.normal_mode=report['normal_mode']
        source=report['highlight_source'];settings.highlight_mode=source['mode']
        mask=report.get('band_mask')
        settings.band_mask_enabled=mask is not None
        settings.band_mask_image=mask['image_name'] if mask else ''
        settings.band_mask_uv=mask['uv_name'] if mask else ''
        if source['mode']=='HEAD_BAND':
            settings.band_reference=finish_band.reference(context.active_object.active_material[finish.KEY])
            for key,value in source['settings'].items():setattr(settings,'band_'+key,value)
    settings.message=report['status']+' / '+report.get('material','')
    return report


def apply_current(context):
    settings=_settings(context)
    modifier=None
    if settings.normal_mode=='ARTISTIC':
        modifier=context.active_object.modifiers.get(settings.normal_modifier)
        if modifier is None:raise ValueError('请选择此对象已有的艺术法向修改器')
    is_band=settings.highlight_mode=='HEAD_BAND'
    mask=_mask_values(context)
    if mask is not None:
        return _result(context,finish.apply_with_band_mask(context,_values(context),normal_modifier=modifier,
            band_settings=_band_values(context),band_reference=settings.band_reference,band_mask=mask))
    return _result(context,finish.apply(context,_values(context),normal_modifier=modifier,
        band_settings=_band_values(context) if is_band else None,
        band_reference=settings.band_reference if is_band else None))


def update_current(context):
    settings=_settings(context);report=finish.inspect(context);current=report['highlight_source']
    actual_mask=report.get('band_mask');requested=_mask_values(context)
    if ((actual_mask is None)!=(requested is None) or
        (actual_mask is not None and (actual_mask['image_name']!=requested['image'].name or actual_mask['uv_name']!=requested['uv_name']))):
        raise ValueError('更换发束遮罩、图像或UV前请先移除效果，再重新添加')
    if settings.highlight_mode!=current['mode']:
        raise ValueError('切换高光形状前请先移除当前效果，再明确添加')
    is_band=current['mode']=='HEAD_BAND'
    if is_band and settings.band_reference!=finish_band.reference(context.active_object.active_material[finish.KEY]):
        raise ValueError('更换光带参考前请先移除当前效果，再明确添加')
    return _result(context,finish.update(context,_values(context),band_settings=_band_values(context) if is_band else None))
def inspect_current(context):return _result(context,finish.inspect(context))
def remove_current(context):return _result(context,finish.remove(context))


def key_current(context):
    settings=_settings(context)
    frame=float(context.scene.frame_current)+float(context.scene.frame_subframe)
    return _result(context,finish.insert_key(context,settings.key,frame,settings.key_value))


def clear_current(context):return _result(context,finish.clear_keys(context))


def snapshot_sun(context):
    settings=_settings(context);sun=settings.sun
    if sun is None or sun.type!='LIGHT' or sun.data.type!='SUN' or context.scene.objects.get(sun.name)!=sun:
        raise ValueError('请选择当前场景中的明确 Sun')
    value=sun.matrix_world.to_3x3()@Vector((0,0,1))
    if not all(math.isfinite(v) for v in value) or value.length==0:
        raise ValueError('Sun方向不是有限非零向量')
    settings.light_direction=value.normalized()
    settings.message='已读取当前Sun方向；灯光动画不会自动更新此艺术方向'


class _Action:
    @classmethod
    def poll(cls,context):
        try:finish._context(context);return True
        except (ValueError,AttributeError,ReferenceError):return False
    def execute(self,context):
        try:self.action(context);return {'FINISHED'}
        except Exception as error:
            _settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONFINISH_OT_apply(_Action,bpy.types.Operator):
    bl_idname='toon_finish.apply';bl_label='添加高光与边缘光';bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_current)
class TOONFINISH_OT_update(_Action,bpy.types.Operator):
    bl_idname='toon_finish.update';bl_label='更新效果参数';bl_options={'REGISTER','UNDO'}
    action=staticmethod(update_current)
class TOONFINISH_OT_inspect(_Action,bpy.types.Operator):
    bl_idname='toon_finish.inspect';bl_label='读取与检查效果';bl_options={'REGISTER'}
    action=staticmethod(inspect_current)
class TOONFINISH_OT_remove(_Action,bpy.types.Operator):
    bl_idname='toon_finish.remove';bl_label='移除效果并恢复原输出';bl_options={'REGISTER','UNDO'}
    action=staticmethod(remove_current)
class TOONFINISH_OT_key(_Action,bpy.types.Operator):
    bl_idname='toon_finish.insert_key';bl_label='在当前帧写入强度';bl_options={'REGISTER','UNDO'}
    action=staticmethod(key_current)
class TOONFINISH_OT_clear(_Action,bpy.types.Operator):
    bl_idname='toon_finish.clear_keys';bl_label='保留动作并撤下动画';bl_options={'REGISTER','UNDO'}
    action=staticmethod(clear_current)
class TOONFINISH_OT_sun(_Action,bpy.types.Operator):
    bl_idname='toon_finish.snapshot_sun';bl_label='读取当前Sun方向';bl_options={'REGISTER','UNDO'}
    action=staticmethod(snapshot_sun)


def draw_contents(layout,context):
    settings=_settings(context);target=context.active_object
    layout.label(text='原材质输出上的独立高光与边缘光')
    if target and target.type=='MESH' and target.active_material:
        material=target.active_material
        count=sum(slot.material==material for slot in target.material_slots)
        layout.label(text=material.name+' / 本对象'+str(count)+'个槽使用此材质')
    layout.prop(settings,'normal_mode')
    if settings.normal_mode=='ARTISTIC' and target:
        layout.prop_search(settings,'normal_modifier',target,'modifiers')
    layout.prop(settings,'highlight_mode')
    for key in ('highlight_strength','highlight_color'):
        layout.prop(settings,key)
    if settings.highlight_mode=='HEAD_BAND':
        layout.prop(settings,'band_reference')
        for key in finish_band.defaults():layout.prop(settings,'band_'+key)
        layout.label(text='高度、半宽和抬升使用参考局部坐标；保持参考跟随头部')
        layout.label(text='光带位置由参考控制；光向只控制正面亮度')
        layout.prop(settings,'band_mask_enabled')
        if settings.band_mask_enabled:
            layout.prop_search(settings,'band_mask_image',bpy.data,'images')
            layout.prop(settings,'band_mask_uv')
            layout.label(text='Non-Color R / 明确UV；换图或UV须移除后添加')
    else:
        layout.prop(settings,'highlight_threshold');layout.prop(settings,'highlight_softness')
    for key in ('rim_strength','rim_color','rim_power','rim_lift','light_direction'):
        layout.prop(settings,key)
    layout.prop(settings,'sun');layout.operator('toon_finish.snapshot_sun')
    layout.label(text='方向是艺术控制；读取Sun是当前时刻快照')
    row=layout.row(align=True);row.operator('toon_finish.apply');row.operator('toon_finish.update')
    row=layout.row(align=True);row.operator('toon_finish.inspect');row.operator('toon_finish.remove')
    layout.prop(settings,'key');layout.prop(settings,'key_value')
    layout.operator('toon_finish.insert_key');layout.operator('toon_finish.clear_keys')
    layout.label(text='强度关键帧写入效果节点组；保留原材质和角色动作')
    if settings.message:layout.label(text=settings.message[:120])


CLASSES=(TOONFINISH_Settings,TOONFINISH_OT_apply,TOONFINISH_OT_update,TOONFINISH_OT_inspect,
    TOONFINISH_OT_remove,TOONFINISH_OT_key,TOONFINISH_OT_clear,TOONFINISH_OT_sun)


def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Finish UI Scene property belongs to another owner')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONFINISH_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise


def unregister():
    global _registered,_descriptor
    descriptor=bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer()!=_descriptor:
        raise RuntimeError('Finish UI property was replaced; preserve foreign owner')
    if descriptor is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
