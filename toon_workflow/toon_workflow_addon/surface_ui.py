# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded single-slot EEVEE author controls. No standalone Panel or handler."""
import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import surface_slots as surface
from . import band_boundary as boundary

PROP='toon_surface_slot_ui'
_registered=False
_classes_registered=[]
_descriptor=None


def _load(settings,values):
    settings.mode=values['mode']
    for key,value in values.items():
        if key!='mode':setattr(settings,key,value)


def _candidate_changed(settings,context):
    _load(settings,surface.candidate_settings(settings.candidate))


class TOONSURFACE_Settings(bpy.types.PropertyGroup):
    candidate: EnumProperty(name='表面候选',items=[('CEL3','CEL3 三色','不透明三色基础填充'),('GAME_REFLECT','GAME_REFLECT 三色+反射','美术叠加反射，不是完整PBR'),('INK_FLAT','INK_FLAT 墨线平涂','单色平涂；不生成排线或完整漫画风格')],default='CEL3',update=_candidate_changed)
    mode: EnumProperty(name='实际设置模式',items=[('eevee_cel3','eevee_cel3',''),('eevee_cel3_reflect','eevee_cel3_reflect',''),('native_cel','native_cel','')],default='eevee_cel3')
    base_color: FloatVectorProperty(name='光照采样基色',subtype='COLOR',size=4,default=(.8,.8,.8,1.),min=0,max=1)
    shadow_color: FloatVectorProperty(name='暗部色',subtype='COLOR',size=4,default=(.035,.065,.14,1.),min=0,max=1)
    mid_color: FloatVectorProperty(name='中间色',subtype='COLOR',size=4,default=(.28,.46,.68,1.),min=0,max=1)
    lit_color: FloatVectorProperty(name='亮部色',subtype='COLOR',size=4,default=(.72,.87,1.,1.),min=0,max=1)
    threshold_low: FloatProperty(name='低阈值',default=.18,min=0,max=1)
    threshold_high: FloatProperty(name='高阈值',default=.5,min=0,max=1)
    threshold: FloatProperty(name='二色阈值',default=.18,min=0,max=1)
    toon_size: FloatProperty(name='库内Toon Size',default=.5,min=0,max=1)
    toon_smooth: FloatProperty(name='库内Toon Smooth',default=0,min=0,max=1)
    reflection_color: FloatVectorProperty(name='反射色',subtype='COLOR',size=4,default=(.65,.7,.8,1.),min=0,max=1)
    reflection_strength: FloatProperty(name='反射强度',default=.35,min=0,max=1)
    reflection_roughness: FloatProperty(name='反射粗糙度',default=.26,min=0,max=1)
    boundary_width_low: FloatProperty(name='低边界完整照度区间宽度',
        description='完整照度区间宽度，0 为硬边；不是像素宽度',default=0,min=0,max=1)
    boundary_width_high: FloatProperty(name='高边界完整照度区间宽度',
        description='完整照度区间宽度，0 为硬边；不是像素宽度',default=0,min=0,max=1)
    library_path: StringProperty(name='材质参数库 JSON',subtype='FILE_PATH')
    template_id: StringProperty(name='库条目 UUID',description='只有一个条目时可留空；多个条目必须明确UUID')
    message: StringProperty(name='表面检查')


def _settings(context):return getattr(context.scene,PROP)


def _values(context):
    settings=_settings(context)
    return {key:settings.mode if key=='mode' else list(getattr(settings,key)) if key.endswith('_color') else getattr(settings,key)
            for key in surface.materials._fields(settings.mode)}


def _result(context,result):
    settings=_settings(context)
    if result.get('status')=='valid':_load(settings,result['settings'])
    settings.message=result.get('status','')+': '+('; '.join(result.get('issues',[])) or result.get('material',result.get('file',result.get('original_material') or '')))
    return result


def apply_current(context):return _result(context,surface.apply(context,_values(context)))
def update_current(context):return _result(context,surface.update(context,_values(context)))
def restore_current(context):return _result(context,surface.restore(context))
def inspect_current(context):
    result=surface.inspect(context)
    if result.get('status')=='valid':
        material=context.active_object.material_slots[result['slot_index']].material
        report=(boundary.inspect(context)
            if result['settings']['mode'] in ('eevee_cel3','eevee_cel3_reflect')
                or boundary.KEY in material else dict(status='absent'))
        result=dict(result,boundary=report)
        if report.get('status')=='valid':
            settings=_settings(context)
            settings.boundary_width_low=report['settings']['width_low']
            settings.boundary_width_high=report['settings']['width_high']
        elif report.get('status')=='absent':
            settings=_settings(context)
            settings.boundary_width_low=settings.boundary_width_high=0
        else:
            raise ValueError('明暗羽化检查失败：'+str(report))
    return _result(context,result)


def _boundary_values(context):
    settings=_settings(context)
    return dict(width_low=settings.boundary_width_low,width_high=settings.boundary_width_high)


def apply_boundary_current(context):
    boundary.apply(context,_boundary_values(context))
    return inspect_current(context)


def update_boundary_current(context):
    boundary.update(context,_boundary_values(context))
    return inspect_current(context)


def close_boundary_current(context):
    # Closing retains the owned feature and makes both boundaries hard again.
    boundary.update(context,boundary.defaults())
    return inspect_current(context)
def import_current(context):
    settings=_settings(context)
    return _result(context,surface.apply_library(context,bpy.path.abspath(settings.library_path),settings.template_id or None))
def export_current(context):return _result(context,surface.export_library(context,bpy.path.abspath(_settings(context).library_path)))


class _Action:
    @classmethod
    def poll(cls,context):
        try:surface._context(context);return True
        except (ValueError,AttributeError,ReferenceError):return False
    def execute(self,context):
        try:self.action(context);return {'FINISHED'}
        except Exception as error:
            if hasattr(context.scene,PROP):_settings(context).message=str(error)
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONSURFACE_OT_apply(_Action,bpy.types.Operator):
    bl_idname='toon_surface.apply';bl_label='应用到活动槽';bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_current)
class TOONSURFACE_OT_update(_Action,bpy.types.Operator):
    bl_idname='toon_surface.update';bl_label='更新当前受管参数';bl_options={'REGISTER','UNDO'}
    action=staticmethod(update_current)
class TOONSURFACE_OT_restore(_Action,bpy.types.Operator):
    bl_idname='toon_surface.restore';bl_label='恢复原槽材质';bl_options={'REGISTER','UNDO'}
    action=staticmethod(restore_current)
class TOONSURFACE_OT_inspect(_Action,bpy.types.Operator):
    bl_idname='toon_surface.inspect';bl_label='检查活动槽';bl_options={'REGISTER'}
    action=staticmethod(inspect_current)
class TOONSURFACE_OT_import(_Action,bpy.types.Operator):
    bl_idname='toon_surface.import_library';bl_label='从材质库应用';bl_options={'REGISTER','UNDO'}
    action=staticmethod(import_current)
class TOONSURFACE_OT_export(_Action,bpy.types.Operator):
    bl_idname='toon_surface.export_library';bl_label='导出活动槽参数';bl_options={'REGISTER'}
    action=staticmethod(export_current)
class TOONSURFACE_OT_boundary_apply(_Action,bpy.types.Operator):
    bl_idname='toon_surface.boundary_apply';bl_label='应用明暗羽化';bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_boundary_current)
class TOONSURFACE_OT_boundary_update(_Action,bpy.types.Operator):
    bl_idname='toon_surface.boundary_update';bl_label='更新明暗羽化';bl_options={'REGISTER','UNDO'}
    action=staticmethod(update_boundary_current)
class TOONSURFACE_OT_boundary_close(_Action,bpy.types.Operator):
    bl_idname='toon_surface.boundary_close';bl_label='关闭明暗羽化（归零）';bl_options={'REGISTER','UNDO'}
    action=staticmethod(close_boundary_current)


def draw_contents(layout,context):
    settings=_settings(context);obj=context.active_object
    layout.label(text='活动槽表面作者：不修改脸部受管绑定')
    if obj is not None:
        layout.label(text=obj.name+' / slot '+str(obj.active_material_index))
        if 0<=obj.active_material_index<len(obj.material_slots):
            slot=obj.material_slots[obj.active_material_index]
            layout.label(text=slot.link+' / '+(slot.material.name if slot.material else '(空材质)'))
    layout.prop(settings,'candidate');layout.label(text='实际模式：'+settings.mode)
    if settings.candidate=='GAME_REFLECT':layout.label(text='三色+总反射候选；非完整游戏PBR')
    if settings.candidate=='INK_FLAT':layout.label(text='墨线平涂；不生成排线/完整漫画风格')
    for name in ('base_color','shadow_color','lit_color'):layout.prop(settings,name)
    if settings.mode in {'eevee_cel3','eevee_cel3_reflect'}:
        for name in ('mid_color','threshold_low','threshold_high'):layout.prop(settings,name)
    else:layout.prop(settings,'threshold')
    if settings.mode=='eevee_cel3_reflect':
        for name in ('reflection_color','reflection_strength','reflection_roughness'):layout.prop(settings,name)
    row=layout.row(align=True);row.operator('toon_surface.apply');row.operator('toon_surface.update')
    row=layout.row(align=True);row.operator('toon_surface.restore');row.operator('toon_surface.inspect')
    layout.label(text='换模式先恢复；共享/作者修改冲突不强覆盖')
    box=layout.box();box.label(text='可选明暗边界羽化：仅 CEL3 / 三色+反射')
    box.prop(settings,'boundary_width_low');box.prop(settings,'boundary_width_high')
    box.label(text='完整照度区间宽度；0 为硬边，不是像素')
    row=box.row(align=True);row.operator('toon_surface.boundary_apply');row.operator('toon_surface.boundary_update')
    box.operator('toon_surface.boundary_close')
    layout.prop(settings,'library_path');layout.prop(settings,'template_id')
    row=layout.row(align=True);row.operator('toon_surface.import_library');row.operator('toon_surface.export_library')
    layout.label(text='此JSON仅基础静态材质参数；不运输明暗羽化')
    layout.label(text='不是 Pencil style_file；羽化随 CEL v2 配方运输')
    if settings.message:layout.label(text=settings.message)


CLASSES=(TOONSURFACE_Settings,TOONSURFACE_OT_apply,TOONSURFACE_OT_update,TOONSURFACE_OT_restore,
         TOONSURFACE_OT_inspect,TOONSURFACE_OT_import,TOONSURFACE_OT_export,
         TOONSURFACE_OT_boundary_apply,TOONSURFACE_OT_boundary_update,TOONSURFACE_OT_boundary_close)


def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Surface UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONSURFACE_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise


def unregister():
    global _registered,_descriptor
    descriptor=bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer()!=_descriptor:
        raise RuntimeError('Surface UI property was replaced; preserve its foreign owner')
    if descriptor is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
