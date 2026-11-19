# SPDX-License-Identifier: GPL-3.0-or-later
"""Omni preview controls embedded by the unified Toon workbench.

Operators submit an isolated copy snapshot; GPU rendering never runs in the UI
process. Explicit refresh/cancel keep observation separate from the background job.
"""
import json
from pathlib import Path
import uuid

import bpy
from bpy.props import EnumProperty,FloatProperty,FloatVectorProperty,IntProperty,PointerProperty,StringProperty
from . import omni_preview

PROP='toon_omni_preview_ui'
_registered=False
_classes_registered=[]
_descriptor=None


def _preset_changed(self,context):
    if self.preset=='CUSTOM':return
    value=omni_preview.preset(self.preset)
    for name in ('light_yaws','light_elevations','camera_yaws','camera_elevations'):
        setattr(self,name,','.join(format(v,'g') for v in value[name]))
    self.camera_mode=value['camera_mode'];self.page_size=str(value['page_size'])


def _camera_poll(self,obj):return obj.type=='CAMERA'
def _sun_poll(self,obj):return obj.type=='LIGHT' and obj.data.type=='SUN'


class TOONOMNI_Settings(bpy.types.PropertyGroup):
    preset:EnumProperty(name='预览组合',items=[('LIGHT_9','9 格环绕光','3光向 × 3高低角'),('LIGHT_16','16 格环绕光','4光向 × 4高低角'),
        ('LIGHT_36','36 格环绕光','6光向 × 6高低角'),('LIGHT_64','64 格环绕光','8光向 × 8高低角'),
        ('MULTI_16','16 格光向与机位','4光向 × 4机位'),('CUSTOM','自定义角表','按四张角表组合，最多256格')],default='LIGHT_9',update=_preset_changed,options=set())
    reference:PointerProperty(name='头部方向参考',type=bpy.types.Object,options=set())
    sun:PointerProperty(name='单一 Sun',type=bpy.types.Object,poll=_sun_poll,options=set())
    camera:PointerProperty(name='当前源相机',type=bpy.types.Object,poll=_camera_poll,options=set())
    camera_mode:EnumProperty(name='机位',items=[('SOURCE','保留当前机位','灯光环绕，使用原场景相机'),('ORBIT','环绕头部参考','同一源相机按角表临时环绕')],default='SOURCE',options=set())
    light_yaws:StringProperty(name='光方位角',default='0,120,240',options=set())
    light_elevations:StringProperty(name='光高低角',default='-60,0,60',options=set())
    camera_yaws:StringProperty(name='机位方位角',default='0',options=set())
    camera_elevations:StringProperty(name='机位高低角',default='0',options=set())
    tile_size:IntProperty(name='单格像素',default=256,min=64,max=1024,options=set())
    samples:IntProperty(name='原生采样',default=64,min=1,max=512,options=set())
    page_size:EnumProperty(name='每页格数',items=[(str(n),str(n),'每页最多'+str(n)+'格') for n in (9,16,36,64)],default='9',options=set())
    orbit_distance:FloatProperty(name='机位距离',description='0沿用源相机到参考焦点距离；保留原镜头参数',default=0.,min=0.,options=set())
    focus_offset:FloatVectorProperty(name='焦点偏移',description='参考 right/forward/up 方向上的世界单位偏移',size=3,default=(0,0,0),options=set())
    output_parent:StringProperty(name='输出父目录',subtype='DIR_PATH',options=set())
    job_directory:StringProperty(name='本次预览',subtype='DIR_PATH',options={'SKIP_SAVE'})
    message:StringProperty(options={'SKIP_SAVE'})
    job_status:StringProperty(default='idle',options={'SKIP_SAVE'})
    page_index:IntProperty(name='图集页码',default=1,min=1,max=256,options={'SKIP_SAVE'})


def settings(context):return getattr(context.scene,PROP)


def _document(context,directory):
    u=settings(context)
    if None in (u.reference,u.sun,u.camera):raise ValueError('请明确选择头部参考、Sun 和当前场景源相机')
    document=dict(schema=omni_preview.SCHEMA,reference=u.reference.name,sun=u.sun.name,camera=u.camera.name,
        output_directory=str(directory),camera_mode=u.camera_mode,tile_size=u.tile_size,samples=u.samples,
        page_size=int(u.page_size),orbit_distance=u.orbit_distance,focus_offset=list(u.focus_offset))
    fields=('light_yaws','light_elevations') if u.camera_mode=='SOURCE' else ('light_yaws','light_elevations','camera_yaws','camera_elevations')
    for name in fields:
        try:
            text=getattr(u,name).strip()
            document[name]=json.loads(text) if text.startswith('[') else [float(v.strip()) for v in text.replace('，',',').split(',')]
        except (ValueError,TypeError) as exc:raise ValueError('角度请用逗号分隔，例如 0,90,180,270') from exc
    if u.camera_mode=='SOURCE':document.update(camera_yaws=[0],camera_elevations=[0])
    return document


def read_current(context):
    from . import hair_map,hair_sdf
    u=settings(context);u.camera=context.scene.camera
    suns=[o for o in context.scene.objects if o.type=='LIGHT' and o.data.type=='SUN' and not o.hide_render and o.data.energy>0]
    if len(suns)==1:u.sun=suns[0]
    references=set()
    for obj in context.scene.objects:
        if obj.type!='MESH':continue
        for slot in obj.material_slots:
            if slot.material is not None and hair_map.KEY in slot.material:
                group=slot.material[hair_map.KEY]
                if hair_sdf.present(group):
                    reference=group.get(hair_sdf._REFERENCE)
                    if reference is not None:references.add(reference)
    if len(references)==1:u.reference=next(iter(references))
    u.message='已读取明确的源引用；请核对参考物体。预览不自动隐藏环境或改变材质。'


def submit_current(context):
    u=settings(context)
    if u.job_directory:
        previous=omni_preview.status(bpy.path.abspath(u.job_directory))
        if previous['status'] not in ('completed','cancelled','failed'):
            raise ValueError('本面板的上一预览仍在运行；请刷新或申请取消')
    parent=Path(bpy.path.abspath(u.output_parent))
    if not parent.is_absolute() or not parent.is_dir():raise ValueError('请选择已存在的绝对输出父目录')
    directory=parent/('toon-omni-'+uuid.uuid4().hex)
    record=omni_preview.submit(context,_document(context,directory))
    u.job_directory=str(directory);u.job_status=record['status']
    u.message='预览正在后台生成，可继续编辑。'
    return record


def refresh_current(context):
    u=settings(context)
    if not u.job_directory:raise ValueError('尚未提交预览')
    record=omni_preview.status(bpy.path.abspath(u.job_directory));u.job_status=record['status']
    completed,total=record.get('completed',0),record.get('total','?')
    name={'submitted':'已提交','running':'生成中','finalizing':'整理中','completed':'已完成','cancelled':'已取消','failed':'失败'}.get(record['status'],record['status'])
    pages=record.get('pages',[])
    u.message=name+' '+str(completed)+'/'+str(total)+((' · '+pages[0]['file']) if pages else '')
    return record


def cancel_current(context):
    u=settings(context)
    if not u.job_directory:raise ValueError('尚未提交预览')
    result=omni_preview.request_cancel(bpy.path.abspath(u.job_directory));u.message='已申请取消；当前单格结束后停止并保留结果。'
    return result


def open_current(context):
    record=refresh_current(context);pages=record.get('pages',[])
    if not pages:raise ValueError('尚无完整图集页；已完成的单格保留在本次目录中')
    index=settings(context).page_index-1
    if index>=len(pages):raise ValueError('当前已有 '+str(len(pages))+' 页，请选择已生成的页码')
    page=Path(pages[index]['file']);root=Path(bpy.path.abspath(settings(context).job_directory)).resolve(strict=True)
    if not page.is_file() or not page.resolve().is_relative_to(root) or omni_preview._sha(page)!=pages[index]['sha256']:
        raise ValueError('图集路径或内容已改变，请检查本次输出目录')
    bpy.ops.wm.path_open(filepath=str(page))
    return dict(file=str(page))


class _Action:
    bl_options={'REGISTER'}
    action=None
    def execute(self,context):
        try:self.action(context)
        except Exception as exc:
            settings(context).message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}
        self.report({'INFO'},settings(context).message);return {'FINISHED'}


class TOONOMNI_OT_read(_Action,bpy.types.Operator):
    bl_idname='toon_omni.read';bl_label='读取当前源';action=staticmethod(read_current)
class TOONOMNI_OT_submit(_Action,bpy.types.Operator):
    bl_idname='toon_omni.submit';bl_label='后台生成预览';action=staticmethod(submit_current)
class TOONOMNI_OT_refresh(_Action,bpy.types.Operator):
    bl_idname='toon_omni.refresh';bl_label='刷新进度';action=staticmethod(refresh_current)
class TOONOMNI_OT_cancel(_Action,bpy.types.Operator):
    bl_idname='toon_omni.cancel';bl_label='申请取消';action=staticmethod(cancel_current)
class TOONOMNI_OT_open(_Action,bpy.types.Operator):
    bl_idname='toon_omni.open';bl_label='打开图集';action=staticmethod(open_current)


def draw_contents(layout,context):
    u=settings(context);box=layout.box();box.label(text='环绕光照与多机位预览')
    box.label(text='后台生成 · 保留原材质与真实遮挡')
    box.label(text='使用单一白色Sun和零环境光；不自动隐藏场景。')
    box.prop(u,'preset');box.operator('toon_omni.read')
    box.prop(u,'reference');box.prop(u,'sun');box.prop(u,'camera')
    box.prop(u,'light_yaws');box.prop(u,'light_elevations');box.prop(u,'camera_mode')
    if u.camera_mode=='ORBIT':
        box.prop(u,'camera_yaws');box.prop(u,'camera_elevations');box.prop(u,'orbit_distance');box.prop(u,'focus_offset')
    row=box.row(align=True);row.prop(u,'tile_size');row.prop(u,'samples');box.prop(u,'page_size')
    box.label(text='0°为参考前方；正方位转向右方。纯顶/底可填 ±90°。')
    box.label(text='极点重复方向会合并；最多256格，每页最多64格。')
    try:
        _,plan=omni_preview.normalize(_document(context,Path.cwd()/'toon-omni-plan-only'))
        box.label(text='本次 '+str(plan['tiles'])+' 格 / '+str(plan['pages'])+' 页')
    except ValueError:pass
    box.prop(u,'output_parent');box.operator('toon_omni.submit')
    row=box.row(align=True);row.operator('toon_omni.refresh');row.operator('toon_omni.cancel');row.operator('toon_omni.open')
    if u.job_directory:box.prop(u,'page_index');box.prop(u,'job_directory')
    if u.message:box.label(text=u.message)


CLASSES=(TOONOMNI_Settings,TOONOMNI_OT_read,TOONOMNI_OT_submit,TOONOMNI_OT_refresh,TOONOMNI_OT_cancel,TOONOMNI_OT_open)


def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Omni UI property is owned by another registration')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONOMNI_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise


def unregister():
    global _registered,_descriptor
    prop=bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None and prop.as_pointer()!=_descriptor:raise RuntimeError('Omni UI property changed owner; preserve foreign registration')
    if prop is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
