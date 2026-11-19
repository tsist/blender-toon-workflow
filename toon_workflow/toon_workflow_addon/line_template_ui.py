# SPDX-License-Identifier: GPL-3.0-or-later
"""Static line templates in the single Toon workbench."""
import hashlib,json,uuid
import bpy
from bpy.props import CollectionProperty,EnumProperty,FloatVectorProperty,IntProperty,PointerProperty,StringProperty
from . import line_template as core,cel_ui
PROP='toon_line_template_ui'
_epoch=uuid.uuid4().hex
_registered=False
_classes_registered=[]
_descriptor=None


class TOONLINE_Binding(bpy.types.PropertyGroup):
    kind:StringProperty()
    role:StringProperty()
    target_name:StringProperty()
    target_uid:StringProperty()
    epoch:StringProperty()


class TOONLINE_InkBinding(bpy.types.PropertyGroup):
    policy_id:StringProperty()
    kind:StringProperty()
    image_name:StringProperty(name='目标图像')
    image_uid:StringProperty(options={'HIDDEN'})
    node:StringProperty(name='目标颜色节点')
    direction:EnumProperty(name='接口方向',items=[('INPUT','输入','未连接的颜色输入'),('OUTPUT','输出','RGB 节点输出')])
    socket:StringProperty(name='接口标识')
    node_uid:StringProperty(options={'HIDDEN'})
    uv_name:StringProperty(name='目标 UV')
    uv_transform:FloatVectorProperty(name='UV 仿射',size=6,default=(1,0,0,0,1,0),min=-65536,max=65536)
    confirmed:StringProperty(options={'HIDDEN'})
    epoch:StringProperty(options={'HIDDEN'})


class TOONLINE_Settings(bpy.types.PropertyGroup):
    name:StringProperty(name='模板名',default='描边模板')
    path:StringProperty(name='描边模板 JSON',subtype='FILE_PATH')
    document:StringProperty(options={'HIDDEN'})
    bindings:CollectionProperty(type=TOONLINE_Binding)
    ink_bindings:CollectionProperty(type=TOONLINE_InkBinding)
    mode:EnumProperty(name='应用方式',items=[('APPEND','追加线组','保留现有线组'),('REPLACE','替换描边并留存旧记录','明确开始新的静态描边作者')],default='APPEND')
    conflict:EnumProperty(name='同名线组',items=[('ERROR','停止并报告','保留现有作者'),('RENAME','新组自动改名','明确追加新组')],default='ERROR')
    receipt:StringProperty(options={'HIDDEN'})
    message:StringProperty()


def settings(context):return getattr(context.scene,PROP)


def _load(u,doc):
    if u.receipt or u.document:raise ValueError('请先留存当前模板记录，再开始新模板')
    u.document=core._json(core.normalize(doc));u.name=doc['name'];u.bindings.clear()
    for kind,rows in doc['roles'].items():
        for row in rows:
            item=u.bindings.add();item.kind=kind;item.role=row['role']
    u.ink_bindings.clear()
    if doc['schema']=='toon-line-template.v2':
        u.mode='REPLACE'
        for row in doc['ink_sources']['policies']:
            item=u.ink_bindings.add();item.policy_id=row['policy_id'];item.kind=row['kind']


def new_current(context):
    u=settings(context)
    if not u.document:return
    fields={key:getattr(u,key) for key in ('document','name','path','mode','conflict','receipt','message')}
    rows=[{key:getattr(r,key) for key in ('kind','role','target_name','target_uid','epoch')} for r in u.bindings]
    inks=[_ink_row(r) for r in u.ink_bindings]
    text=None
    try:
        from .cel_ui import _archive_text
        text=_archive_text('Toon previous template record '+uuid.uuid4().hex,
            core._json(dict(settings=fields,bindings=rows,ink_bindings=inks,note='Historical UI record; bindings require explicit validation before reuse.')))
        text['toon_line_template_record']=json.loads(u.document)['schema'];text.use_fake_user=True
        u.document='';u.receipt='';u.bindings.clear();u.ink_bindings.clear();u.path='';u.mode='APPEND';u.conflict='ERROR'
        u.message='旧模板记录已留存，可读取当前描边或导入新模板'
    except BaseException:
        for key,value in fields.items():setattr(u,key,value)
        u.bindings.clear()
        for values in rows:
            r=u.bindings.add()
            for key,value in values.items():setattr(r,key,value)
        u.ink_bindings.clear()
        for values in inks:
            r=u.ink_bindings.add()
            for key,value in values.items():setattr(r,key,value)
        if text is not None:bpy.data.texts.remove(text)
        raise


def capture_current(context):
    u=settings(context);_load(u,core.capture(context.scene,u.name))
    # Export creation does not bind an imported template by names. The donor's
    # IDs are explicitly selected here while capturing its own author state.
    for row in u.bindings:
        pool=context.scene.objects if row.kind=='objects' else bpy.data.materials if row.kind=='materials' else bpy.data.collections
        value=pool.get(row.role)
        if value is not None:_bind_value(row,value)
    policies={p['id']:p for p in json.loads(u.document)['style']['document']['request'].get('toon_line_material_colors',{}).get('policies',[])}
    for index,row in enumerate(u.ink_bindings):
        src=policies[row.policy_id]['source']
        if row.kind=='UV_IMAGE':
            row.image_name=src['image'];row.uv_name=src['uv_name'];row.uv_transform=src['uv_transform']
        elif row.kind=='NODE_RGBA':
            row.node=src['node'];row.direction=src['direction'];row.socket=src['socket']
        if row.kind!='AUTHOR_RGB':bind_ink_current(context,index)
    u.message='已读取完整静态描边；导入其它工程后须逐角色绑定'


def import_current(context):
    u=settings(context);_load(u,core.load_document(bpy.path.abspath(u.path)))
    u.message='已导入；所有角色等待显式绑定，不按同名推测'


def export_current(context):
    u=settings(context);doc=json.loads(u.document);doc['name']=u.name
    result=core.export_document(doc,bpy.path.abspath(u.path));u.message='已导出完整静态描边模板'
    return result


def _bind_value(row,value):
    row.target_name=value.name_full;row.target_uid=str(value.session_uid);row.epoch=_epoch


def bind_current(context,index):
    u=settings(context)
    if u.receipt:raise ValueError('已应用模板保持原绑定；请保留现有作者')
    if not 0<=index<len(u.bindings):raise ValueError('选择模板中的明确角色')
    row=u.bindings[index]
    value=context.active_object if row.kind=='objects' else context.active_object.active_material if row.kind=='materials' and context.active_object else context.collection if row.kind=='collections' else None
    if value is None:raise ValueError('请先选择对应对象、活动材质或集合')
    _bind_value(row,value);u.message='已绑定：'+row.role+' → '+value.name


def bindings(context):
    u=settings(context);result={kind:{} for kind in core.KINDS}
    for row in u.bindings:
        pool=context.scene.objects if row.kind=='objects' else bpy.data.materials if row.kind=='materials' else bpy.data.collections
        value=pool.get(row.target_name)
        if value is None:raise ValueError('角色尚未绑定或目标失联：'+row.role)
        if row.epoch==_epoch:
            if row.target_uid!=str(value.session_uid):raise ValueError('目标身份已改变，请保留替换并重新绑定：'+row.role)
        elif not u.receipt:raise ValueError('未应用的绑定来自旧会话，请显式重新绑定：'+row.role)
        result[row.kind][row.role]=value
    return result


_INK_FIELDS=('policy_id','kind','image_name','image_uid','node','direction','socket','node_uid','uv_name','uv_transform','confirmed','epoch')


def _ink_row(row):
    return {key:list(row.uv_transform) if key=='uv_transform' else getattr(row,key) for key in _INK_FIELDS}


def _ink_config(row):
    if row.kind=='NODE_RGBA':return dict(node=row.node,direction=row.direction,socket=row.socket)
    if row.kind=='UV_IMAGE':return dict(image=row.image_name,uv_name=row.uv_name,uv_transform=list(row.uv_transform))
    return {}


def _ink_material(context,row):
    doc=json.loads(settings(context).document)
    policy=next(p for p in doc['style']['document']['request']['toon_line_material_colors']['policies'] if p['id']==row.policy_id)
    role=next(r['role'] for r in doc['roles']['materials'] if r['source']==policy['material'])
    return bindings(context)['materials'][role]


def bind_ink_current(context,index):
    u=settings(context)
    if u.receipt:raise ValueError('已应用模板保持来源绑定；请先留存记录')
    if not 0<=index<len(u.ink_bindings):raise ValueError('请选择明确的线色来源')
    row=u.ink_bindings[index]
    if row.kind=='AUTHOR_RGB':return
    material=_ink_material(context,row)
    # Validate the exact chosen source without changing any material, image or UV.
    if row.kind=='NODE_RGBA':
        from . import line_material_colors
        line_material_colors._source(material,dict(kind='NODE_RGBA',**_ink_config(row)),context.scene)
        node=material.node_tree.nodes[row.node]
        row.node_uid=str(node.as_pointer())
    else:
        from . import line_image_source
        image=bpy.data.images.get(row.image_name)
        if image is None:raise ValueError('请选择目标工程已存在的图像')
        line_image_source.image_data(image.name)
        if not row.uv_name:raise ValueError('请明确指定目标 UV 名称')
        row.image_uid=str(image.session_uid)
    row.confirmed=core._json(_ink_config(row));row.epoch=_epoch
    u.message='已确认线色来源：'+row.policy_id+'；应用前仍需完整预检'


def ink_bindings(context):
    u=settings(context);doc=json.loads(u.document)
    if doc['schema']!='toon-line-template.v2':return None
    result=dict(nodes={},images={},uv_names={},uv_transforms={})
    expected=[(r['policy_id'],r['kind']) for r in doc['ink_sources']['policies']]
    if [(r.policy_id,r.kind) for r in u.ink_bindings]!=expected:raise ValueError('线色来源表已改变')
    for row in u.ink_bindings:
        if row.kind=='AUTHOR_RGB':continue
        if row.confirmed!=core._json(_ink_config(row)):raise ValueError('线色来源待明确确认：'+row.policy_id)
        if row.epoch!=_epoch and not u.receipt:raise ValueError('来源绑定来自旧会话，请重新确认：'+row.policy_id)
        if row.kind=='NODE_RGBA':
            material=_ink_material(context,row);tree=material.node_tree
            node=tree.nodes.get(row.node) if tree else None
            if node is None or (row.epoch==_epoch and str(node.as_pointer())!=row.node_uid):raise ValueError('颜色节点身份已改变：'+row.policy_id)
            result['nodes'][row.policy_id]=_ink_config(row)
        else:
            image=bpy.data.images.get(row.image_name)
            if image is None or (row.epoch==_epoch and str(image.session_uid)!=row.image_uid):raise ValueError('图像身份已改变：'+row.policy_id)
            result['images'][row.policy_id]=image;result['uv_names'][row.policy_id]=row.uv_name
            result['uv_transforms'][row.policy_id]=list(row.uv_transform)
    return result


def _persistent_ink(value):
    """Session IDs guard live choices, not the persisted content witness."""
    def stable(item):
        if type(item) is dict:return {k:stable(v) for k,v in item.items() if k not in ('uid','target_uid')}
        if type(item) is list:return [stable(v) for v in item]
        return item
    result=stable(value);result.pop('sha256',None)
    result['sha256']=hashlib.sha256(core._json(result).encode('utf8')).hexdigest()
    return result


def _observation(context):
    u=settings(context)
    result=dict(document=json.loads(u.document),name=u.name,mode=u.mode,conflict=u.conflict,
        bindings={k:{r:core._ref(v) for r,v in rows.items()} for k,rows in bindings(context).items()},pencil=cel_ui._pencil_state(context.scene))
    if result['document']['schema']=='toon-line-template.v2':
        result['ink']=_persistent_ink(core.ink_observation(context.scene,result['document'],bindings(context),ink_bindings(context)))
    return result


def inspect_current(context):
    u=settings(context)
    if u.receipt:
        saved=json.loads(u.receipt)
        if core._json(saved['observation'])!=core._json(_observation(context)):raise ValueError('模板、引用或描边作者已改变；保留修改并停止覆盖')
        u.message='原样核验通过';return dict(status='unchanged',result=saved['result'])
    result=core.inspect(context.scene,json.loads(u.document),bindings(context),mode=u.mode,conflict=u.conflict,ink_bindings=ink_bindings(context))
    u.message='预检通过：'+str(result['groups'])+'个线组；属性合法性不代表新输入的美术落点'
    return result


def apply_current(context):
    u=settings(context)
    if u.receipt:return inspect_current(context)
    _observation(context)
    token=core.apply(context.scene,json.loads(u.document),bindings(context),mode=u.mode,conflict=u.conflict,ink_bindings=ink_bindings(context))
    try:
        result=core.receipt(token)
        u.receipt=core._json(dict(result=result,observation=_observation(context)))
    except BaseException:
        core.rollback(context.scene,token);raise
    u.message='已应用完整静态描边；旧记录留存，已有CEL回执的变更需按新作者处理'
    return result


class _Action:
    def execute(self,context):
        try:self.action(context);return {'FINISHED'}
        except Exception as error:
            settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONLINE_OT_capture(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.capture';bl_label='读取当前完整描边';bl_options={'REGISTER','UNDO'}
    action=staticmethod(capture_current)
class TOONLINE_OT_import(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.import_template';bl_label='导入描边模板';bl_options={'REGISTER','UNDO'}
    action=staticmethod(import_current)
class TOONLINE_OT_export(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.export_template';bl_label='导出描边模板';bl_options={'REGISTER'}
    action=staticmethod(export_current)
class TOONLINE_OT_inspect(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.inspect';bl_label='预检描边模板';bl_options={'REGISTER'}
    action=staticmethod(inspect_current)
class TOONLINE_OT_apply(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.apply';bl_label='应用描边模板';bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_current)
class TOONLINE_OT_bind(bpy.types.Operator):
    bl_idname='toon_line_template.bind';bl_label='绑定当前选择';bl_options={'REGISTER','UNDO'}
    index:IntProperty(min=0)
    def execute(self,context):
        try:bind_current(context,self.index);return {'FINISHED'}
        except Exception as error:settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}
class TOONLINE_OT_new(_Action,bpy.types.Operator):
    bl_idname='toon_line_template.new';bl_label='留存记录并开始新模板';bl_options={'REGISTER','UNDO'}
    action=staticmethod(new_current)


class TOONLINE_OT_bind_ink(bpy.types.Operator):
    bl_idname='toon_line_template.bind_ink';bl_label='确认线色来源';bl_options={'REGISTER','UNDO'}
    index:IntProperty(min=0)
    def execute(self,context):
        try:bind_ink_current(context,self.index);return {'FINISHED'}
        except Exception as error:settings(context).message=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}


def draw_contents(layout,context):
    u=settings(context);layout.prop(u,'name');layout.prop(u,'path')
    row=layout.row(align=True);row.operator('toon_line_template.capture');row.operator('toon_line_template.import_template')
    if u.document:
        layout.operator('toon_line_template.new')
        v2=json.loads(u.document)['schema']=='toon-line-template.v2'
        row=layout.row();row.enabled=not v2 and not bool(u.receipt);row.prop(u,'mode')
        if v2:layout.label(text='含线色来源：整体替换，旧作者记录留存')
        if u.mode=='APPEND':layout.prop(u,'conflict')
        for i,b in enumerate(u.bindings):
            row=layout.row(align=True)
            row.label(text={'objects':'对象','materials':'材质','collections':'集合'}[b.kind]+'：'+b.role)
            row.label(text=b.target_name or '未绑定')
            if not u.receipt:row.operator('toon_line_template.bind',text='绑定当前').index=i
        for i,b in enumerate(u.ink_bindings):
            box=layout.box();box.label(text=b.policy_id+' / '+{'AUTHOR_RGB':'作者颜色','NODE_RGBA':'目标节点颜色','UV_IMAGE':'图像线色'}[b.kind])
            if b.kind=='AUTHOR_RGB':continue
            fields=box.column();fields.enabled=not bool(u.receipt)
            if b.kind=='NODE_RGBA':
                fields.prop(b,'node');fields.prop(b,'direction');fields.prop(b,'socket')
            else:
                fields.prop_search(b,'image_name',bpy.data,'images');fields.prop(b,'uv_name')
                fields.prop(b,'uv_transform',text='UV 变换 [a b tx c d ty]')
            if not u.receipt:fields.operator('toon_line_template.bind_ink').index=i
        layout.label(text='UV 须适配实际表面；预检不代替最终渲染落点确认')
        layout.label(text='不传输图像文件、UV 数据、动画或旧 INDEX 绘制')
        row=layout.row(align=True);row.operator('toon_line_template.inspect');row.operator('toon_line_template.apply')
        layout.operator('toon_line_template.export_template')
    if u.message:layout.label(text=u.message[:120])


CLASSES=(TOONLINE_Binding,TOONLINE_InkBinding,TOONLINE_Settings,TOONLINE_OT_capture,TOONLINE_OT_import,TOONLINE_OT_export,TOONLINE_OT_inspect,TOONLINE_OT_apply,TOONLINE_OT_bind,TOONLINE_OT_bind_ink,TOONLINE_OT_new)
def register():
    global _descriptor,_registered
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Line template UI is owned by another registration')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONLINE_Settings));_descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:unregister();raise
def unregister():
    global _descriptor,_registered
    prop=bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None:
        if prop.as_pointer()!=_descriptor:raise RuntimeError('Preserve foreign line template descriptor')
        delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
