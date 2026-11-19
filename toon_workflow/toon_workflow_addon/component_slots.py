# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit slot assignment for new style components, keeping original IDs.

This adapter does not infer character roles, rewrite UVs, modify source shader
graphs, or change camera bindings from names. Apply a shot explicitly before
rendering; no frame/camera handler is installed.
"""
import json
import hashlib
import uuid
from contextlib import contextmanager
from . import surface_slots,eye_overlay_nodes,component_catalog as components

PROFILE='toon-component-slots.v1'
KEY='toon_component_slots_profile'
OWNER='toon_component_slots_owner'
TARGET='toon_component_slots_target'
MESH='toon_component_slots_mesh'
SLOT='toon_component_slots_slot'
LINK='toon_component_slots_link'
ORIGINAL='toon_component_slots_original'
DATA='toon_component_slots_data_original'
HAD='toon_component_slots_had_original'
HAD_DATA='toon_component_slots_had_data_original'
SIGNATURE='toon_component_slots_signature'


def _signature(material):
    data={key:material.get(key) for key in (KEY,OWNER,MESH,SLOT,LINK,HAD,HAD_DATA)}
    data.update({key:surface_slots._identity(material.get(key)) for key in (TARGET,ORIGINAL,DATA)})
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode('utf8')).hexdigest()


def _slot(scene,target,index):
    import bpy
    if (not isinstance(target,bpy.types.Object) or target.type!='MESH'
            or scene.objects.get(target.name)!=target or target.library or target.override_library
            or target.data.library or target.data.override_library
            or not target.is_editable or not target.data.is_editable
            or target.mode!='OBJECT' or target.data.is_editmode):
        raise ValueError('请选择本场景可编辑的本地Mesh对象')
    if type(index) is not int or not 0<=index<len(target.material_slots):
        raise ValueError('必须明确已有材质槽')
    if not any(p.material_index==index for p in target.data.polygons):
        raise ValueError('材质槽未被实际面使用')
    slot=target.material_slots[index]
    if slot.link not in ('DATA','OBJECT'):raise ValueError('未知材质链接类型')
    if slot.link=='DATA' and target.data.users!=1:
        raise ValueError('共享Mesh的数据槽不能局部替换；请先显式选择OBJECT槽或独立工作副本')
    return slot


def _protect(material):
    surface_slots._protected(material)
    if eye_overlay_nodes.is_present(material):raise ValueError('眼部叠色/高光层必须保留')
    if material is not None and (KEY in material or surface_slots._PROFILE in material):
        raise ValueError('当前槽已有受管组件；请先恢复，再应用另一组件')


def _created(scene,target,index,settings):
    import bpy
    slot=_slot(scene,target,index);previous=slot.material;_protect(previous)
    desired=components.normalize(settings)
    _mapping(target,desired)
    if desired['style'] in ('GLASS','WATER') and not scene.eevee.use_raytracing:
        raise ValueError('玻璃与水面需要先显式启用本场景EEVEE光线追踪')
    material=components.build(bpy,desired,'Toon '+desired['style']+' '+target.name)
    try:
        result=components.inspect(material)
        if result['status']!='valid':raise ValueError('新组件的节点读回失败：'+str(result.get('issues')))
        material[KEY]=PROFILE;material[OWNER]=str(uuid.uuid4())
        # Native Mesh ID-property references themselves add Mesh.users. Keep
        # the existing surface-slot convention: target ID + signed Mesh name,
        # so ownership bookkeeping cannot masquerade as a shared geometry user.
        material[TARGET]=target;material[MESH]=target.data.name;material[SLOT]=index;material[LINK]=slot.link
        material[HAD]=previous is not None;material[HAD_DATA]=target.data.materials[index] is not None
        if previous is not None:material[ORIGINAL]=previous
        if target.data.materials[index] is not None:material[DATA]=target.data.materials[index]
        material[SIGNATURE]=_signature(material)
    except BaseException:
        if material.users==0:bpy.data.materials.remove(material)
        raise
    return dict(target=target,slot=index,link=slot.link,original=previous,material=material,settings=desired)


def _mapping(target,settings):
    if (settings['style']=='MANGA' and settings.get('pattern_space')=='UV'
            and target.data.uv_layers.get(settings['uv_map']) is None):
        raise ValueError('漫画图案需要目标真实存在的UV层：'+settings['uv_map'])


def _owned(scene,target,index):
    slot=_slot(scene,target,index);material=slot.material
    if (material is None or material.get(KEY)!=PROFILE or material.get(TARGET)!=target
            or material.get(MESH)!=target.data.name or material.get(SLOT)!=index
            or material.get(LINK)!=slot.link or type(material.get(HAD)) is not bool
            or type(material.get(HAD_DATA)) is not bool
            or (ORIGINAL in material)!=material[HAD]
            or (DATA in material)!=material[HAD_DATA] or _signature(material)!=material.get(SIGNATURE)):
        raise ValueError('当前槽没有一致的风格组件所有权')
    try:uuid.UUID(material.get(OWNER,''))
    except (TypeError,ValueError,AttributeError):raise ValueError('风格组件所有权标识已改变')
    if slot.link=='OBJECT' and target.data.materials[index]!=material.get(DATA):
        raise ValueError('OBJECT槽下方的数据材质已改变；保留后续编辑')
    state=components.inspect(material)
    if state['status']!='valid':raise ValueError('组件已被修改：'+str(state.get('issues',[])))
    return slot,material,state


def inspect(scene,target,index):
    try:
        slot,material,state=_owned(scene,target,index)
        return dict(status='valid',profile=PROFILE,target=target.name,slot=index,link=slot.link,
                    material=material.name,settings=state['settings'],issues=[],
                    original_material=material[ORIGINAL].name if material[HAD] else None)
    except (ValueError,TypeError,KeyError,AttributeError,ReferenceError) as error:
        return dict(status='conflict',profile=PROFILE,issues=[str(error)])


def apply(scene,target,index,settings):
    if scene.render.engine!='BLENDER_EEVEE':raise ValueError('新风格组件目前需要EEVEE')
    row=_created(scene,target,index,settings)
    slot=target.material_slots[index]
    try:
        slot.material=row['material']
        _owned(scene,target,index)
    except BaseException:
        slot.material=row['original']
        raise
    return inspect(scene,target,index)


def update(scene,target,index,settings):
    if scene.render.engine!='BLENDER_EEVEE':raise ValueError('新风格组件目前需要EEVEE')
    _,material,_=_owned(scene,target,index)
    desired=components.normalize(settings);_mapping(target,desired)
    result=components.update(material,desired)
    if result['status']!='valid':raise ValueError('组件更新读回失败')
    return inspect(scene,target,index)


def restore(scene,target,index):
    slot,material,_=_owned(scene,target,index)
    previous=material[ORIGINAL] if material[HAD] else None
    slot.material=previous
    return dict(status='restored',profile=PROFILE,target=target.name,slot=index,
                original_material=previous.name if previous else None,
                retained_material=material.name)


def snapshot(scene):
    rows=[]
    for obj in scene.objects:
        if obj.type!='MESH' or obj.hide_render:continue
        # Blender permits polygons with material_index == 0 on a Mesh with
        # no slots (rig widgets and helper geometry are common examples).
        # There is no material author to capture on such an object.
        if not obj.material_slots:continue
        for index in sorted({p.material_index for p in obj.data.polygons}):
            if not 0<=index<len(obj.material_slots):
                raise ValueError('材质面索引超出已有槽：'+obj.name+' / '+str(index))
            mat=obj.material_slots[index].material
            if mat is None or mat.get(KEY)!=PROFILE:continue
            state=inspect(scene,obj,index)
            if state['status']!='valid':raise ValueError('风格组件预检冲突：'+str(state['issues']))
            rows.append(state)
    return sorted(rows,key=lambda r:(r['target'],r['slot']))


@contextmanager
def render_values(scene,*,light_direction=None,resolution=None):
    """Temporary explicit preview values, restored without a camera handler.

    Pixel-space patterns use the tile's actual extent; GAME's artistic light
    snapshot follows the preview's explicitly chosen Sun for this render only.
    """
    import copy
    source=snapshot(scene);changed=[];applied=[]
    try:
        for row in source:
            desired=copy.deepcopy(row['settings'])
            if light_direction is not None and 'light_direction' in desired:
                desired['light_direction']=list(light_direction)
            if resolution is not None and desired.get('pattern_space')=='WINDOW':desired['resolution']=list(resolution)
            desired=components.normalize(desired)
            obj=scene.objects[row['target']]
            if desired!=row['settings']:
                material=obj.material_slots[row['slot']].material
                generation=material.get('toon_style_component_generation')
                update(scene,obj,row['slot'],desired);changed.append((row,generation))
            applied.append(dict(target=row['target'],slot=row['slot'],settings=desired))
        yield dict(profile='toon-component-preview-values.v1',applied=applied)
    finally:
        errors=[]
        for row,generation in reversed(changed):
            try:
                obj=scene.objects[row['target']]
                update(scene,obj,row['slot'],row['settings'])
                if generation is not None:obj.material_slots[row['slot']].material['toon_style_component_generation']=generation
            except Exception as error:errors.append(str(error))
        if errors:raise RuntimeError('预览组件参数未完整恢复：'+'; '.join(errors))
        if snapshot(scene)!=source:raise RuntimeError('预览组件参数恢复不一致')


def apply_set(context,document,bindings,*,camera_bindings=None):
    """Build every component first, then commit explicit slot pointers atomically.

    Bindings contain actual object identities, slot indices and role strings.
    Camera overrides are selected only from explicitly bound Camera IDs.
    """
    from . import style_set
    scene=context.scene
    if scene.render.engine!='BLENDER_EEVEE':raise ValueError('风格集合目前需要EEVEE')
    camera_role=None;seen=[]
    for role,camera in (camera_bindings or {}).items():
        if (type(role) is not str or camera.type!='CAMERA' or scene.objects.get(camera.name)!=camera
                or any(camera is old for old in seen)):
            raise ValueError('机位绑定必须是本场景独立真实相机身份')
        seen.append(camera)
        if camera is scene.camera:camera_role=role
    plan=style_set.bind(document,bindings,camera_role=camera_role,normalize_component=components.normalize)
    staged=[]
    for record in plan['records']:
        slot=_slot(scene,record['target'],record['slot'])
        if record['action']=='PRESERVE':continue
        _protect(slot.material)
    try:
        for record in plan['records']:
            if record['action']=='COMPONENT':
                staged.append(_created(scene,record['target'],record['slot'],record['settings']))
        for row in staged:row['target'].material_slots[row['slot']].material=row['material']
        for row in staged:_owned(scene,row['target'],row['slot'])
    except BaseException:
        for row in reversed(staged):
            slot=row['target'].material_slots[row['slot']]
            if slot.material==row['material']:slot.material=row['original']
        raise
    return dict(status='applied',profile=PROFILE,camera_role=camera_role,
                records=[dict(target=r['target'].name,slot=r['slot'],material=r['material'].name,
                              settings=r['settings']) for r in staged],
                preserved=[dict(target=r['target'].name,slot=r['slot']) for r in plan['records'] if r['action']=='PRESERVE'],
                unbound_roles=plan['unbound_roles'])
