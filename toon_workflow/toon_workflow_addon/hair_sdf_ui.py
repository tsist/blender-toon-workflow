# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit cyclic hair-shadow authoring in the existing Toon workbench."""
import bpy
from bpy.props import StringProperty,FloatProperty,PointerProperty,EnumProperty
from . import hair_map,hair_map_ui,hair_sdf,hair_sdf_authoring

PROP='toon_hair_sdf_ui'
_registered=False
_classes_registered=[]
_descriptor=None

class TOONHSDF_Settings(bpy.types.PropertyGroup):
    mode:EnumProperty(name='方向表示',items=(('cyclic_v1','水平循环 v1','旧八方向；竖直极点回退原明暗'),
        ('omni_v2','全方向 v2','八个+30°环键与唯一顶底键；真实高程距离插值')),default='cyclic_v1',options=set())
    atlas0_name:StringProperty(name='方向图 0–135°',options=set())
    atlas1_name:StringProperty(name='方向图 180–315°',options=set())
    pole_atlas_name:StringProperty(name='极点图 R顶 / G底',options=set())
    uv_name:StringProperty(name='SDF 实际 UV',default='ToonHairHighlightUVFit',options=set())
    reference:PointerProperty(name='头部方向参考',type=bpy.types.Object,options=set())
    strength:FloatProperty(name='SDF 明暗强度',default=0.,min=0.,max=1.,options=set())
    softness:FloatProperty(name='SDF 影界过渡',default=.002,min=.00001,max=.25,options=set())
    plan_path:StringProperty(name='8键 / 10键影形计划',subtype='FILE_PATH',options=set())
    output_dir:StringProperty(name='新的编译目录',subtype='DIR_PATH',options=set())
    loaded_owner:StringProperty(options={'SKIP_SAVE'})
    loaded_profile:StringProperty(options={'SKIP_SAVE'})
    message:StringProperty(options={'SKIP_SAVE'})

def settings(context):return getattr(context.scene,PROP)

def _load(context,report):
    u=settings(context)
    names=[v['name'] for v in report['images']]
    u.atlas0_name,u.atlas1_name=names[:2]
    u.mode='omni_v2' if report['profile']==hair_sdf.PROFILE_V2 else 'cyclic_v1'
    u.pole_atlas_name=names[2] if len(names)==3 else ''
    u.uv_name=report['uv']['uv_name']
    u.reference=context.scene.objects.get(report['reference']['name'])
    u.strength=report['settings']['strength'];u.softness=report['settings']['softness']
    u.loaded_owner=report['owner']
    u.loaded_profile=report['profile']
    return report

def read_current(context):
    scene,obj,index=hair_map_ui._context(context)
    report=hair_map.describe(scene,obj,index).get('sdf')
    if report is None:raise ValueError('当前 HM 未连接头发 SDF')
    _load(context,report);settings(context).message='已读取当前头发 SDF；R 高光与 B 固定影结构保留'
    return report

def attach_current(context):
    hair_map_ui._context(context);u=settings(context)
    images=[bpy.data.images.get(u.atlas0_name),bpy.data.images.get(u.atlas1_name)]
    if None in images or u.reference is None:raise ValueError('请明确选择两张独立 RGBA 方向图和头部参考')
    pole=bpy.data.images.get(u.pole_atlas_name) if u.mode=='omni_v2' else None
    if u.mode=='omni_v2' and pole is None:raise ValueError('全方向 v2 必须选择独立极点图：R顶/G底/BA恒1')
    report=hair_sdf.attach(context,images,u.uv_name,u.reference,strength=0.,softness=u.softness,mode=u.mode,pole_image=pole)
    _load(context,report);u.message='已连接零强度 SDF；读取、调整并应用后再看实际效果'
    return report

def update_current(context):
    scene,obj,index=hair_map_ui._context(context);u=settings(context)
    report=hair_map.describe(scene,obj,index).get('sdf')
    if report is None or u.loaded_owner!=report['owner']:raise ValueError('请先读取当前槽的头发 SDF')
    mode='omni_v2' if report['profile']==hair_sdf.PROFILE_V2 else 'cyclic_v1'
    names=[u.atlas0_name,u.atlas1_name]+([u.pole_atlas_name] if mode=='omni_v2' else [])
    if (u.mode!=mode or names!=[v['name'] for v in report['images']]
            or u.uv_name!=report['uv']['uv_name'] or u.reference!=scene.objects.get(report['reference']['name'])):
        raise ValueError('更换模式、方向图、UV 或参考前先移除旧 SDF；读取可恢复当前绑定')
    report=hair_sdf.update(context,u.strength,u.softness)
    _load(context,report);u.message='已应用头发 SDF；请核对完整材质和必要光向'
    return report

def remove_current(context):
    result=hair_sdf.remove(context);u=settings(context);u.loaded_owner='';u.loaded_profile=''
    u.message='已移除头发 SDF；原 HM 明暗、统一高光与固定影保持'
    return result

def compile_current(context):
    hair_map_ui._context(context);u=settings(context)
    result=hair_sdf_authoring.compile_plan(context,u.plan_path,u.output_dir)
    omni=result['report']['schema']==hair_sdf_authoring.OUTPUT_SCHEMA_V2
    if len(result['images'])!=(3 if omni else 2):raise RuntimeError('编译方向图数量与计划模式不符')
    names=[im.name for im in result['images']]
    u.atlas0_name,u.atlas1_name=names[:2];u.pole_atlas_name=names[2] if omni else ''
    u.mode='omni_v2' if omni else 'cyclic_v1'
    u.message=('10键全方向' if omni else '八方向')+'影形已编译并实际重载核验；尚未自动连接材质'
    return result['report']

class _Action:
    bl_options={'REGISTER','UNDO'}
    operation=None
    def execute(self,context):
        try:self.operation(context)
        except Exception as error:
            settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}
        self.report({'INFO'},settings(context).message);return {'FINISHED'}

class TOONHSDF_OT_attach(_Action,bpy.types.Operator):
    bl_idname='toon_hair_sdf.attach';bl_label='连接零层';operation=staticmethod(attach_current)
class TOONHSDF_OT_read(_Action,bpy.types.Operator):
    bl_idname='toon_hair_sdf.read';bl_label='读取';operation=staticmethod(read_current)
class TOONHSDF_OT_update(_Action,bpy.types.Operator):
    bl_idname='toon_hair_sdf.update';bl_label='应用';operation=staticmethod(update_current)
class TOONHSDF_OT_remove(_Action,bpy.types.Operator):
    bl_idname='toon_hair_sdf.remove';bl_label='移除';operation=staticmethod(remove_current)
class TOONHSDF_OT_compile(_Action,bpy.types.Operator):
    bl_idname='toon_hair_sdf.compile';bl_label='编译影形计划';operation=staticmethod(compile_current)

def draw_contents(layout,context):
    u=settings(context);obj=context.active_object
    box=layout.box();box.label(text='头发 SDF · 方向影形')
    box.label(text='影形控制明暗；沿用同一受光高光和 B 固定影')
    box.prop(u,'mode')
    box.prop_search(u,'atlas0_name',bpy.data,'images');box.prop_search(u,'atlas1_name',bpy.data,'images')
    if u.mode=='omni_v2':
        box.prop_search(u,'pole_atlas_name',bpy.data,'images')
        box.label(text='环键高程 +30°；极点图 R顶/G底/BA恒1')
    if obj and obj.type=='MESH':box.prop_search(u,'uv_name',obj.data,'uv_layers')
    else:box.prop(u,'uv_name')
    box.prop(u,'reference');box.prop(u,'strength');box.prop(u,'softness')
    row=box.row(align=True)
    for key in ('attach','read','update','remove'):row.operator('toon_hair_sdf.'+key)
    box.label(text='0°=参考前方(-Y)，正向转至右方(+X)；不镜像 UV')
    box.label(text='单白 Sun / 零 World；'+('顶底按真实高程插值，不回退 HM' if u.mode=='omni_v2' else '极端竖光回退原明暗'))
    if u.loaded_owner:box.label(text='当前读回：'+u.loaded_profile+' · '+u.loaded_owner[:8])
    box.prop(u,'plan_path');box.prop(u,'output_dir');box.operator('toon_hair_sdf.compile')
    if u.message:box.label(text=u.message)

CLASSES=(TOONHSDF_Settings,TOONHSDF_OT_attach,TOONHSDF_OT_read,TOONHSDF_OT_update,TOONHSDF_OT_remove,TOONHSDF_OT_compile)
def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Hair SDF UI property belongs to another registration')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONHSDF_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise
def unregister():
    global _registered,_descriptor
    prop=bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None and prop.as_pointer()!=_descriptor:raise RuntimeError('Hair SDF UI property was replaced; preserve foreign owner')
    if prop is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
