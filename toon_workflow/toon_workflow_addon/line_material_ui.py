# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit used-slot material ink controls in the single Toon workbench."""
import copy
import json
import uuid
import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from . import line_material_colors as core
from .backend import pencil

PROP='toon_line_material_ui'
_registered=False
_classes_registered=[]
_descriptor=None
_epoch=uuid.uuid4().hex


class TOONINK_Settings(bpy.types.PropertyGroup):
    target_name: StringProperty()
    target_uid: StringProperty(options={'HIDDEN'})
    material_name: StringProperty()
    slot: IntProperty(min=0)
    identity: StringProperty(options={'HIDDEN'})
    epoch: StringProperty(options={'HIDDEN'})
    policy_id: StringProperty(name='线色方案',default='')
    loaded_id: StringProperty(options={'HIDDEN'})
    policy_snapshot: StringProperty(options={'HIDDEN'})
    source_kind: EnumProperty(name='基色来源',items=[('AUTHOR_RGB','指定颜色','独立作者颜色'),('NODE_RGBA','指定常量节点','只读取明确常量颜色，不求值整个材质'),('UV_IMAGE','指定UV贴图','从实际支撑面读取指定UV图像颜色')])
    image_name: StringProperty(name='颜色贴图')
    uv_name: StringProperty(name='UV层')
    uv_scale: FloatVectorProperty(name='UV缩放/镜像',size=2,default=(1,1),min=-65536,max=65536)
    uv_offset: FloatVectorProperty(name='UV偏移',size=2,default=(0,0),min=-65536,max=65536)
    image_filter: EnumProperty(name='纹理过滤',items=[('LINEAR','线性','双线性采样'),('NEAREST','最近点','保持贴图像素边界')])
    image_extension: EnumProperty(name='UV边界',items=[('EXTEND','延伸边缘','读取边缘颜色'),('REPEAT','重复','按UV周期重复')])
    source_color: FloatVectorProperty(name='作者基色',subtype='COLOR',size=3,min=0,max=1,default=(.5,.2,.3))
    node: StringProperty(name='节点名称')
    direction: EnumProperty(name='节点接口',items=[('INPUT','输入','未连接颜色输入'),('OUTPUT','RGB输出','颜色节点输出')])
    socket: StringProperty(name='接口标识',default='Base Color')
    group: StringProperty(name='限定线组ID',description='留空作用于此材质的全部线组')
    categories: EnumProperty(name='边类别',options={'ENUM_FLAG'},items=[(c,c,'',b) for c,b in zip(core.CATEGORIES,core.BITS)],default=set(core.CATEGORIES))
    amount: FloatProperty(name='材质混色',min=0,max=1,default=0)
    gain: FloatProperty(name='基色明度系数',min=0,max=1,default=1)
    tint: FloatVectorProperty(name='基色乘色',subtype='COLOR',size=3,min=0,max=1,default=(1,1,1))
    message: StringProperty()


def _settings(context): return getattr(context.scene,PROP)


def _slot(context):
    obj=context.active_object
    if context.mode!='OBJECT' or obj is None or obj.type!='MESH' or context.scene.objects.get(obj.name)!=obj:
        raise ValueError('请选择场景中的本地网格和已用材质槽')
    index=obj.active_material_index
    if obj.library or obj.override_library or index>=len(obj.material_slots): raise ValueError('目标不是本地已用槽')
    material=obj.material_slots[index].material
    if material is None or material.library or material.override_library or not any(p.material_index==index for p in obj.data.polygons):
        raise ValueError('材质槽必须有本地材质且实际分配给面')
    if context.scene.toon_viewport_preview.style_mode!='PRESET': raise ValueError('材质线色需要已有完整描边样式')
    return obj,index,material


def read_current(context, policy_id=None, *, new=False):
    obj,index,material=_slot(context);u=_settings(context)
    payload=core.records(pencil('addon').effective_preset(context.scene)) or dict(schema=core.SCHEMA,policies=[])
    row=None if new else next((p for p in payload['policies'] if p['id']==policy_id),None) if policy_id else next(
        (p for p in payload['policies'] if p['material']==dict(name=material.name_full,library=None)),None)
    if policy_id and row is None:raise ValueError('线色方案已失联，请重新读取')
    if row is not None and row['material']!=dict(name=material.name_full,library=None):
        raise ValueError('先选择该方案对应的材质槽')
    if row is not None and len(row['groups'])>1:
        raise ValueError('本界面仅编辑全部或一个线组；多组方案请在完整作者JSON中编辑')
    u.target_name=obj.name_full;u.target_uid=str(obj.session_uid);u.slot=index
    u.material_name=material.name_full;u.identity=str(material.session_uid);u.epoch=_epoch
    if row is None:
        u.image_name='';u.uv_name='';u.uv_scale=(1,1);u.uv_offset=(0,0);u.image_filter='LINEAR';u.image_extension='EXTEND'
        u.policy_id='material-ink-'+uuid.uuid4().hex;u.source_kind='AUTHOR_RGB';u.amount=0;u.gain=1;u.tint=(1,1,1)
        u.group='';u.categories=set(core.CATEGORIES);u.node='';u.socket='Base Color';u.direction='INPUT'
    else:
        u.policy_id=row['id'];u.amount=row['amount'];u.gain=row['gain'];u.tint=row['tint'];u.categories=set(row['categories'])
        u.group=row['groups'][0] if row['groups'] else '';u.source_kind=row['source']['kind']
        if u.source_kind=='AUTHOR_RGB':u.source_color=row['source']['color']
        elif u.source_kind=='UV_IMAGE':
            src=row['source'];a=src['uv_transform']
            if a[1]!=0 or a[3]!=0:raise ValueError('旋转/剪切UV变换请在完整作者JSON中编辑，界面不改写')
            u.image_name=src['image'];u.uv_name=src['uv_name'];u.uv_scale=(a[0],a[4]);u.uv_offset=(a[2],a[5])
            u.image_filter=src['filter'];u.image_extension=src['extension']
        else:
            for k in ('node','direction','socket'):setattr(u,k,row['source'][k])
    u.loaded_id=u.policy_id;u.policy_snapshot=core._json(row)
    u.message='已明确绑定 '+obj.name+' / '+str(index)+' / '+material.name


def set_payload(scene, payload):
    """Replace only this mother extension, preserving native controls/texts."""
    settings=scene.toon_viewport_preview
    if settings.style_mode!='PRESET' or settings.preset_text is None:raise ValueError('需要完整PRESET描边作者')
    pencil('curve_editor').commit_pending(scene)
    old=settings.preset_text;before=json.loads(old.as_string());candidate=copy.deepcopy(before)
    if payload is None:candidate['document']['request'].pop(core.KEY,None)
    else:candidate['document']['request'][core.KEY]=copy.deepcopy(payload)
    pencil('file_authoring').checked(candidate)
    plan=pencil('style').compile_preset(candidate['document'],candidate['recipe'])
    resolved=core.resolve(scene,plan)
    text=None;old_fake=old.use_fake_user;revision=settings.preset_revision
    pins={}
    try:
        text=bpy.data.texts.new('.toon_material_ink.'+uuid.uuid4().hex)
        text['toon_viewport_preview_owned']=True;text['toon_viewport_preview_reclaimable']=True
        text.from_string(json.dumps(candidate,ensure_ascii=False,indent=2,allow_nan=False))
        if json.loads(text.as_string())!=candidate:raise ValueError('新描边作者回读不一致')
        for policy in (payload or {}).get('policies',[]):
            if policy['source']['kind']=='UV_IMAGE':
                image=bpy.data.images[policy['source']['image']]
                if image not in pins:pins[image]=image.use_fake_user
                image.use_fake_user=True
        old.use_fake_user=True
        settings.preset_text=text;settings.preset_revision+=1
        from .cel_lines import _clear_preview_caches
        _clear_preview_caches(scene)
        return resolved
    except BaseException:
        settings.preset_text=old;settings.preset_revision=revision;old.use_fake_user=old_fake
        for image,value in pins.items():image.use_fake_user=value
        if text is not None and text.users==0:bpy.data.texts.remove(text)
        raise


def apply_current(context, remove=False):
    u=_settings(context);obj,index,material=_slot(context)
    if (u.epoch!=_epoch or u.target_name!=obj.name_full or u.target_uid!=str(obj.session_uid)
            or u.slot!=index or u.material_name!=material.name_full
            or u.identity!=str(material.session_uid)):raise ValueError('选择或会话已改变，请先重新读取活动槽')
    payload=core.records(pencil('addon').effective_preset(context.scene)) or dict(schema=core.SCHEMA,policies=[])
    rows=payload['policies'];found=next((i for i,p in enumerate(rows) if p['id']==u.policy_id),None)
    if u.policy_id!=u.loaded_id or core._json(rows[found] if found is not None else None)!=u.policy_snapshot:
        raise ValueError('线色方案已经改变，请保留修改并重新读取活动槽')
    if remove:
        if found is None:raise ValueError('此方案尚未写入')
        if rows[found]['material']!=dict(name=material.name_full,library=None):raise ValueError('方案材质已改变')
        rows.pop(found)
    else:
        source=dict(kind='AUTHOR_RGB',color=list(u.source_color)) if u.source_kind=='AUTHOR_RGB' else dict(
            kind='NODE_RGBA',node=u.node,direction=u.direction,socket=u.socket)
        if u.source_kind=='UV_IMAGE':
            source=dict(kind='UV_IMAGE',image=u.image_name,uv_name=u.uv_name,uv_transform=[u.uv_scale[0],0.,u.uv_offset[0],0.,u.uv_scale[1],u.uv_offset[1]],
                filter=u.image_filter,extension=u.image_extension,support='LOWEST_MATCHING_TRIANGLE')
        row=dict(id=u.policy_id,material=dict(name=material.name_full,library=None),groups=[u.group] if u.group else [],
                 categories=[c for c in core.CATEGORIES if c in u.categories],source=source,amount=u.amount,gain=u.gain,tint=list(u.tint))
        if found is None:rows.append(row)
        else:
            if rows[found]['material']!=row['material']:raise ValueError('不能借同ID覆盖其它材质方案')
            rows[found]=row
    if any(p['source']['kind']=='UV_IMAGE' for p in rows):payload['schema']=core.SCHEMA_V2
    result=set_payload(context.scene,payload if rows else None)
    u.policy_snapshot=core._json(None if remove else row)
    u.message='已写入新描边作者，旧作者留存；旧CEL或模板回执需重新核验'
    return result


class _Action:
    def execute(self,context):
        try:self.action(context);return {'FINISHED'}
        except Exception as error:_settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}
class TOONINK_OT_read(_Action,bpy.types.Operator):
    bl_idname='toon_ink.read_slot';bl_label='读取活动槽线色';bl_options={'REGISTER','UNDO'}
    policy_id: StringProperty()
    def action(self,context):return read_current(context,self.policy_id or None)
class TOONINK_OT_new(_Action,bpy.types.Operator):
    bl_idname='toon_ink.new_policy';bl_label='为活动槽新增方案';bl_options={'REGISTER','UNDO'}
    action=staticmethod(lambda c:read_current(c,new=True))
class TOONINK_OT_apply(_Action,bpy.types.Operator):
    bl_idname='toon_ink.apply';bl_label='应用材质线色';bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_current)
class TOONINK_OT_remove(_Action,bpy.types.Operator):
    bl_idname='toon_ink.remove';bl_label='移除此线色方案';bl_options={'REGISTER','UNDO'}
    action=staticmethod(lambda c:apply_current(c,remove=True))


def draw_contents(layout,context):
    u=_settings(context)
    row=layout.row(align=True);row.operator('toon_ink.read_slot');row.operator('toon_ink.new_policy')
    try:
        _,_,material=_slot(context)
        payload=core.records(pencil('addon').effective_preset(context.scene))
        for policy in payload['policies'] if payload else []:
            if policy['material']==dict(name=material.name_full,library=None):
                op=layout.operator('toon_ink.read_slot',text='方案 '+str(payload['policies'].index(policy)+1)+' · '+{'AUTHOR_RGB':'指定色','NODE_RGBA':'节点色','UV_IMAGE':'UV贴图'}[policy['source']['kind']]);op.policy_id=policy['id']
    except ValueError:pass
    if u.material_name:
        layout.label(text=u.material_name+' / 槽 '+str(u.slot));layout.prop(u,'source_kind')
        if u.source_kind=='AUTHOR_RGB':layout.prop(u,'source_color')
        elif u.source_kind=='UV_IMAGE':
            layout.prop_search(u,'image_name',bpy.data,'images');layout.prop(u,'uv_name')
            layout.prop(u,'uv_scale');layout.prop(u,'uv_offset');layout.prop(u,'image_filter');layout.prop(u,'image_extension')
            layout.label(text='UV接缝使用首个匹配支撑面；不读取背景颜色')
        else:
            layout.prop(u,'node');layout.prop(u,'direction');layout.prop(u,'socket')
        layout.prop(u,'group');layout.prop(u,'categories');layout.prop(u,'amount');layout.prop(u,'gain');layout.prop(u,'tint')
        row=layout.row(align=True);row.operator('toon_ink.apply');row.operator('toon_ink.remove')
        layout.label(text='同材质多方案按作者顺序优先；仅联合渲染生效')
    if u.message:layout.label(text=u.message[:110])


CLASSES=(TOONINK_Settings,TOONINK_OT_read,TOONINK_OT_new,TOONINK_OT_apply,TOONINK_OT_remove)
def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Material ink UI has another owner')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONINK_Settings));_descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise
def unregister():
    global _registered,_descriptor
    prop=bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None:
        if prop.as_pointer()!=_descriptor:raise RuntimeError('Preserve foreign material ink UI')
        delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
