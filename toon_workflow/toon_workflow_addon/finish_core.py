# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit additive CEL finish around an existing opaque material Surface.

Old SDF/normal/color readers remain connected to their original destinations.
Only this effect's nodes, identity tags and surface-wrapper adoption are owned.
"""
import hashlib
import json
import uuid

from . import finish_nodes as nodes, finish_animation as animation, surface_slots as surface, finish_band as band

PROFILE = 'toon-cel-finish.v1'
KEY = 'toon_finish_binding'
_PROFILE = 'toon_finish_binding_profile'
_TARGET = 'toon_finish_target'
_SLOTS = 'toon_finish_slots'
_OWNER = 'toon_finish_owner'
_MARK = 'toon_finish_instance'
_SOURCE = 'toon_finish_source'
_OUTPUT = 'toon_finish_output'
_SOCKET = 'toon_finish_source_socket'
_DECLARED = 'toon_finish_declared'
_SHAPE = 'toon_finish_shape'
_NORMAL = 'toon_finish_normal_mode'
_SURFACE = 'toon_finish_surface_owner'


class FinishError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _socket(values, identifier):
    matches = [s for s in values if s.identifier == identifier]
    if len(matches) != 1:
        raise FinishError('Expected one socket: ' + identifier)
    return matches[0]


def _effect_socket(node, key):
    interface=json.loads(node.node_tree['toon_finish_node_interface'])
    return _socket(node.outputs if key=='shader' else node.inputs,interface[key])


def _slots(target, material):
    return [dict(index=i, link=s.link) for i,s in enumerate(target.material_slots) if s.material == material]


def _context(context, *, eevee=False):
    import bpy
    target, index, slot = surface._context(context, eevee=eevee)
    if context.scene.objects.get(target.name)!=target:
        raise FinishError('Finish target must belong to the explicit Scene')
    material = slot.material
    if (material is None or material.library or material.override_library or not material.is_editable
            or not material.use_nodes or material.node_tree is None
            or material.node_tree.library or material.node_tree.override_library or not material.node_tree.is_editable):
        raise FinishError('Choose an existing local editable node material')
    users = bpy.data.user_map(subset={material}).get(material, set())
    if any(user not in {target, target.data} for user in users):
        raise FinishError('Finish requires a material used only by this target')
    if any(obj != target and any(s.material == material for s in getattr(obj,'material_slots',()))
           for obj in bpy.data.objects):
        raise FinishError('Material is shared by another Object; preserve shared author data')
    if material.animation_data or material.node_tree.animation_data:
        raise FinishError('Existing material/tree animation requires a separate contract')
    return target, material, material.node_tree


def _opaque(material):
    if material.surface_render_method == 'BLENDED':
        raise FinishError('Initial finish scope is opaque EEVEE surfaces')
    pending = [material.node_tree]
    seen = set()
    count = 0
    forbidden = {'ShaderNodeBsdfTransparent','ShaderNodeBsdfGlass','ShaderNodeBsdfRefraction',
        'ShaderNodeBsdfHair','ShaderNodeBsdfHairPrincipled','ShaderNodeVolumePrincipled',
        'ShaderNodeVolumeAbsorption','ShaderNodeVolumeScatter','ShaderNodeHoldout'}
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        count += len(tree.nodes)
        if len(seen)>64 or count>4096:
            raise FinishError('Material exceeds bounded opaque-finish graph inspection')
        if tree.library or tree.override_library or tree.animation_data:
            raise FinishError('Linked/animated nested shader requires explicit support')
        for node in tree.nodes:
            if node.bl_idname in forbidden:
                raise FinishError('Transparent, hair and volume closures require explicit support')
            if node.bl_idname == 'ShaderNodeBsdfPrincipled':
                for name, expected in (('Alpha',1.),('Transmission Weight',0.)):
                    socket = _socket(node.inputs,name)
                    if socket.is_linked or socket.default_value != expected:
                        raise FinishError('Principled alpha/transmission must be explicitly opaque')
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree:
                pending.append(node.node_tree)


def _band_context(scene, reference):
    import bpy
    if (not isinstance(reference,bpy.types.Object) or reference.type!='EMPTY'
            or scene.objects.get(reference.name)!=reference or reference.library
            or reference.override_library or not reference.is_editable):
        raise FinishError('Choose an explicit local editable Empty reference in this Scene')


def _output(tree):
    outputs = [n for n in tree.nodes if n.bl_idname=='ShaderNodeOutputMaterial' and n.is_active_output]
    if len(outputs)!=1 or outputs[0].target!='ALL' or outputs[0].mute:
        raise FinishError('Choose one active ALL Material Output')
    output=outputs[0]
    if _socket(output.inputs,'Volume').is_linked:
        raise FinishError('Volume output is outside the opaque finish contract')
    incoming=_socket(output.inputs,'Surface')
    if len(incoming.links)!=1:
        raise FinishError('Surface must have one explicit existing source')
    if incoming.links[0].from_node.get('toon53_stage'):
        from .lighting_53 import original_surface_socket
        incoming = original_surface_socket(incoming)
    return output,incoming,incoming.links[0].from_socket


def _tagged(tree,key,owner):
    rows=[n for n in tree.nodes if n.get(key)==owner]
    if len(rows)!=1:
        raise FinishError('Finish source/output/instance identity changed')
    return rows[0]


def _adopt_surface(context, material):
    """Private, explicit attach/detach handshake; never bless arbitrary edits."""
    if surface._PROFILE not in material:
        return
    baseline=surface.materials.managed_snapshot(material[surface._OWNER])
    material[surface._BASELINE]=surface._json(baseline)
    material[surface._SIGNATURE]=surface._signature(material)
    surface._owned(context)


def _owned(context):
    import bpy
    target,material,tree=_context(context)
    group=material.get(KEY)
    if (not isinstance(group,bpy.types.NodeTree) or group.get(_PROFILE)!=PROFILE
            or group.library or group.override_library or not group.is_editable
            or group.get(_TARGET)!=target or group.use_fake_user):
        raise FinishError('Active material has no valid owned finish')
    owner=group.get(_OWNER)
    if type(owner) is not str or len(owner)!=32 or any(c not in '0123456789abcdef' for c in owner):
        raise FinishError('Finish owner identity changed')
    if _json(_slots(target,material))!=group.get(_SLOTS):
        raise FinishError('Finish material slot/link consumers changed')
    incoming=bpy.data.user_map(subset={group}).get(group,set())
    if group.users!=2 or not incoming or not incoming.issubset({material,tree}):
        raise FinishError('Finish Group gained a foreign consumer')
    node=_tagged(tree,_MARK,owner)
    source=_tagged(tree,_SOURCE,owner)
    output=_tagged(tree,_OUTPUT,owner)
    if node.bl_idname!='ShaderNodeGroup' or node.node_tree!=group or node.mute:
        raise FinishError('Finish instance changed type/reference/activation')
    actual_output, destination, _ = _output(tree)
    if actual_output!=output:
        raise FinishError('Finish Material Output changed')
    original=_socket(source.outputs,group[_SOCKET])
    base=_effect_socket(node,'base')
    result=_effect_socket(node,'shader')
    if (len(base.links)!=1 or base.links[0].from_socket!=original
            or len(result.links)!=1 or result.links[0].to_socket!=destination
            or destination.links[0].from_socket!=result):
        raise FinishError('Finish passthrough links changed; preserve author edits')
    from . import finish_band_mask as mask
    if mask.present(group):mask.validate(group,target)
    if _digest(nodes.structure(group))!=group.get(_SHAPE):
        raise FinishError('Finish Group topology/reference changed; preserve author edits')
    animated=animation.inspect(group)
    if animated.get('status')!='valid':
        raise FinishError('Finish animation conflict: '+str(animated.get('issues',animated)))
    actual=nodes.parameters(group)
    nodes.normalize(actual)
    band.validate(group)
    reference=band.reference(group)
    if reference is not None:_band_context(context.scene,reference)
    declared=json.loads(group[_DECLARED])
    keys=set(animated['animated_keys'])
    for key,value in declared.items():
        if key not in keys and _json(actual[key])!=_json(value):
            raise FinishError('Finish parameter was edited outside its declared state: '+key)
    if surface._PROFILE in material:
        if group.get(_SURFACE)!=material.get(surface._OWNER):
            raise FinishError('Base surface identity changed')
        surface._owned(context)
    return target,material,tree,group,node,source,output,actual,animated


def inspect(context):
    target,material,_,group,_,source,_,actual,animated=_owned(context)
    source_mode=band.validate(group)
    report=dict(profile=PROFILE,status='valid',owner=group[_OWNER],target=target.name,
        material=material.name,slots=json.loads(group[_SLOTS]),group=group.name,
        normal_mode=group[_NORMAL],settings=actual,declared=json.loads(group[_DECLARED]),
        animation=animated,highlight_source=({'mode':'HEAD_BAND',**source_mode} if source_mode else {'mode':'BLINN'}),
        original_source=dict(node=source.name,type=source.bl_idname,socket=group[_SOCKET]),
        light_direction_policy='explicit world-space artist direction; optional Sun snapshot, no live light binding')
    from . import finish_band_mask as mask
    if mask.present(group):report['band_mask']=mask.report(group)
    return report


def apply(context, settings, *, normal_modifier=None, band_settings=None, band_reference=None):
    return _apply(context,settings,normal_modifier=normal_modifier,
        band_settings=band_settings,band_reference=band_reference,band_mask=None)


def apply_with_band_mask(context, settings, *, normal_modifier=None,
                         band_settings, band_reference, band_mask):
    """Explicit opt-in; existing apply/old Groups keep their original shape."""
    if type(band_mask) is not dict or set(band_mask)!={'image','uv_name'}:
        raise FinishError('Band mask requires exactly image and uv_name')
    if band_settings is None:raise FinishError('Band mask requires HEAD_BAND settings')
    return _apply(context,settings,normal_modifier=normal_modifier,
        band_settings=band_settings,band_reference=band_reference,band_mask=band_mask)


def _single_hair_highlight(context, target, material, strength):
    """Reject a second producer at the author boundary, before any writes."""
    if type(strength) not in (int, float) or strength <= 0.:
        return
    from .lighting_53 import has_hair_stage
    if has_hair_stage(material):
        raise FinishError('此材质由 5.3 亮带控制；请在新工作台调节亮带强度')
    from . import hair_map
    if not hair_map.present(material):
        return
    report=hair_map.describe(context.scene,target,target.active_material_index)
    active=report['settings']
    if active['strength']>0. and active['highlight_strength']>0.:
        raise FinishError('当前头发已使用 HM 受光高光；请先关闭该高光，再切换到 CEL 光带，避免两套高光叠加')


def _apply(context, settings, *, normal_modifier=None, band_settings=None,
           band_reference=None, band_mask=None):
    import bpy
    target,material,tree=_context(context,eevee=True)
    desired=nodes.normalize(settings)
    _single_hair_highlight(context,target,material,desired['highlight_strength'])
    if band_settings is not None:
        band_settings=band.normalize(band_settings)
        _band_context(context.scene,band_reference)
    elif band_reference is not None:
        raise FinishError('A band reference requires explicit band settings')
    if KEY in material:
        raise FinishError('An existing finish must be updated or explicitly removed')
    _opaque(material)
    output,destination,original=_output(tree)
    source=original.node
    if any(key in node for node,key in ((source,_SOURCE),(output,_OUTPUT))):
        raise FinishError('Surface source/output already has a finish identity')
    old_surface=None
    if surface._PROFILE in material:
        surface._owned(context,eevee=True)
        if normal_modifier is not None:
            raise FinishError('Managed surface finish currently uses geometry normals; keep protected native ART materials separate')
        old_surface={key:material[key] for key in (surface._BASELINE,surface._SIGNATURE)}
    if normal_modifier is not None and normal_modifier.id_data!=target:
        raise FinishError('Artistic normal modifier must belong to the explicit target')
    owner=uuid.uuid4().hex
    group=None;node=None;children=[];band_target=None;band_token=None
    try:
        group=nodes.build(owner,desired,normal_modifier=normal_modifier,band=band_settings,band_reference=band_reference)
        children=nodes.owned_children(group)
        band_target=band.reference(group);band_token=band.reference_id(group)
        if band_mask is not None:
            from . import finish_band_mask as mask
            mask.build(group,target,band_mask['image'],band_mask['uv_name'])
        group[_PROFILE]=PROFILE;group[_TARGET]=target;group[_SLOTS]=_json(_slots(target,material))
        group[_SOCKET]=original.identifier;group[_NORMAL]='ARTISTIC' if normal_modifier else 'GEOMETRY'
        group[_DECLARED]=_json(nodes.parameters(group))
        group[_SHAPE]=_digest(nodes.structure(group))
        if old_surface is not None:group[_SURFACE]=material[surface._OWNER]
        source[_SOURCE]=owner;output[_OUTPUT]=owner
        node=tree.nodes.new('ShaderNodeGroup');node.node_tree=group;node[_MARK]=owner
        node.label='CEL 高光与边缘光';node.location=(output.location.x-230,output.location.y)
        tree.links.new(original,_effect_socket(node,'base'))
        tree.links.new(_effect_socket(node,'shader'),destination)
        material[KEY]=group
        _adopt_surface(context,material)
        return inspect(context)
    except BaseException:
        if KEY in material and material[KEY]==group:del material[KEY]
        if node is not None:tree.nodes.remove(node)
        tree.links.new(original,destination)
        for block,key in ((source,_SOURCE),(output,_OUTPUT)):
            if block.get(key)==owner:del block[key]
        if old_surface:
            for key,value in old_surface.items():material[key]=value
        if group is not None and group.users==0:
            bpy.data.node_groups.remove(group)
            if band_target is not None:band._cleanup_reference(band_target,band_token)
        for child in children:
            if child.users==0 and child.get('toon_finish_child_owner')==owner:bpy.data.node_groups.remove(child)
        raise


def update(context, settings, *, band_settings=None):
    target,material,tree,group,node,source,output,actual,animated=_owned(context)
    desired=nodes.normalize(settings)
    _single_hair_highlight(context,target,material,desired['highlight_strength'])
    old_band=band.parameters(group)
    if band_settings is not None:
        if old_band is None:raise FinishError('Changing the highlight mode requires explicit removal and reattachment')
        band_settings=band.normalize(band_settings)
    for key in animated['animated_keys']:
        if _json(desired[key])!=_json(actual[key]):
            raise FinishError('Keyed parameter requires an explicit keyframe: '+key)
    old=group[_DECLARED]
    try:
        nodes.write_parameters(group,desired)
        if band_settings is not None:band.write_parameters(group,band_settings)
        group[_DECLARED]=_json(nodes.parameters(group))
        return inspect(context)
    except BaseException:
        nodes.write_parameters(group,actual);group[_DECLARED]=old
        if old_band is not None:band.write_parameters(group,old_band)
        raise


def insert_key(context,key,frame,value):
    owned=_owned(context)
    group=owned[3]
    if key=='highlight_strength':
        _single_hair_highlight(context,owned[0],owned[1],value)
    animation.insert_key(group,key,frame,value)
    context.scene.frame_set(context.scene.frame_current,subframe=context.scene.frame_subframe)
    return inspect(context)


def clear_keys(context):
    group=_owned(context)[3]
    animation.clear(group)
    group[_DECLARED]=_json(nodes.parameters(group))
    return inspect(context)


def _node_state(node):
    """Retain editable instance state if detach must recreate the RNA node."""
    def custom(value):
        if hasattr(value,'to_dict'):return {k:custom(v) for k,v in value.items()}
        if hasattr(value,'to_list'):return value.to_list()
        return value
    properties={}
    for prop in node.bl_rna.properties:
        key=prop.identifier
        if prop.is_readonly or key.startswith('bl_') or prop.type=='COLLECTION':continue
        value=getattr(node,key)
        properties[key]=tuple(value) if getattr(prop,'is_array',False) else value
    return properties,{key:custom(value) for key,value in node.items()}


def _restore_node(tree,state):
    properties,custom=state
    node=tree.nodes.new('ShaderNodeGroup')
    # Parent assignment can change relative layout; restore location after it.
    for key in ('parent','node_tree','name'):
        if key in properties:setattr(node,key,properties[key])
    for key,value in properties.items():
        if key not in {'parent','node_tree','name'}:setattr(node,key,value)
    for key,value in custom.items():node[key]=value
    return node


def remove(context):
    import bpy
    target,material,tree,group,node,source,output,actual,animated=_owned(context)
    if animated['animated_keys']:
        raise FinishError('Explicitly clear this finish animation before removing the effect')
    owner=group[_OWNER];children=nodes.owned_children(group)
    band_target=band.reference(group);band_token=band.reference_id(group)
    original=_socket(source.outputs,group[_SOCKET]);_,destination,_=_output(tree)
    instance_state=_node_state(node)
    old_baseline={key:material[key] for key in (surface._BASELINE,surface._SIGNATURE)} if surface._PROFILE in material else None
    try:
        tree.links.new(original,destination)
        del material[KEY]
        del source[_SOURCE];del output[_OUTPUT]
        # The unlinked owned node must be excluded from the adopted wrapper.
        tree.nodes.remove(node)
        node=None
        _adopt_surface(context,material)
    except BaseException:
        if node is None:
            node=_restore_node(tree,instance_state)
        source[_SOURCE]=owner;output[_OUTPUT]=owner;material[KEY]=group
        tree.links.new(original,_effect_socket(node,'base'))
        tree.links.new(_effect_socket(node,'shader'),destination)
        if old_baseline:
            for key,value in old_baseline.items():material[key]=value
        raise
    if group.users==0:
        bpy.data.node_groups.remove(group)
        if band_target is not None:band._cleanup_reference(band_target,band_token)
    for child in children:
        if child.users==0 and child.get('toon_finish_child_owner')==owner:bpy.data.node_groups.remove(child)
    return dict(profile=PROFILE,status='removed',target=target.name,material=material.name,
        original_surface_restored=True,material_pointer_preserved=True)
