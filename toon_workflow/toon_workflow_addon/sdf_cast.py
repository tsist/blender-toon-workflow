# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit single-Sun cast sensor, separated from an SDF face form layer.

The carrier stores a world direction only. Explicit material binding redirects
one active Diffuse.Normal through a face-mix-limited reader. Native light shadow
settings remain untouched. No handlers or Python-per-frame values are used.
"""
import hashlib
import json
import math
import uuid

from . import face_thresholds as face
from .host_compat import is_supported_host

PROFILE = 'toon-face-sdf-cast.v1'
READER_PROFILE = 'toon-face-sdf-cast-reader.v1'
BINDING = 'toon-face-sdf-cast-binding.v1'
_PROFILE = 'toon_sdf_cast_profile'
_OWNER = 'toon_sdf_cast_owner'
_TARGET = 'toon_sdf_cast_target'
_UID = 'toon_sdf_cast_uid'
_SUN = 'toon_sdf_cast_sun'
_ATTRIBUTE = 'toon_sdf_cast_attribute'
_FACE = 'toon_sdf_cast_face'
_SIGNATURE = 'toon_sdf_cast_signature'
_CORE_SIGNATURE = 'toon_sdf_cast_core_signature'
_INTERFACE = 'toon_sdf_cast_interface'
_NODE = 'toon_sdf_cast_node'
_MARK = 'toon_sdf_cast_binding'
_TOKEN = 'toon_sdf_cast_token'
_DEST = 'toon_sdf_cast_destination'
_SOURCE = 'toon_sdf_cast_source'
_ORIGINAL = 'toon_sdf_cast_original'
_SNAPSHOT = 'toon_sdf_cast_snapshot'


class CastError(ValueError):
    """Invalid owner, native source, material binding or protected consumer."""


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _value(value):
    if value is None or type(value) in (str, bool, int, float):return value
    if hasattr(value, 'name_full'):return ['ID', value.bl_rna.identifier, value.name_full]
    try:return [_value(item) for item in value]
    except TypeError:return str(value)


def _socket(sockets, identifier):
    result = [s for s in sockets if s.identifier == identifier]
    if len(result) != 1:raise CastError('Expected one native socket: ' + identifier)
    return result[0]


def _animated(tree):
    animation = tree.animation_data
    return animation is not None and bool(animation.action or animation.drivers or animation.nla_tracks)


def _node_state(node):
    from .runtime_compat import signature_inputs
    props = ('operation', 'data_type', 'input_type', 'domain', 'transform_space',
             'attribute_name', 'attribute_type', 'mute', 'blend_type', 'use_clamp',
             'is_active_output', 'vector_type', 'convert_from', 'convert_to')
    return [node.bl_idname, {key:_value(getattr(node,key)) for key in props if hasattr(node,key)},
            [(s.identifier,_value(s.default_value)) for s in signature_inputs(node) if hasattr(s,'default_value')],
            _value(node.node_tree) if node.bl_idname == 'ShaderNodeGroup' else None]


def graph_signature(group):
    """Sign native graph, sockets and every cast-specific identity field."""
    payload = dict(nodes=sorted([(n.get(_NODE),_node_state(n)) for n in group.nodes],key=repr),
        links=sorted([(l.from_node.get(_NODE),l.from_socket.identifier,l.to_node.get(_NODE),l.to_socket.identifier) for l in group.links],key=repr),
        interface=[{key:_value(getattr(s,key)) for key in ('identifier','name','in_out','socket_type','default_value','min_value','max_value') if hasattr(s,key)}
                   for s in group.interface.items_tree if s.item_type=='SOCKET'],
        metadata={key:_value(group.get(key)) for key in (_PROFILE,_OWNER,_TARGET,_UID,_SUN,_ATTRIBUTE,_FACE,_CORE_SIGNATURE,_INTERFACE)})
    return hashlib.sha256(_json(payload).encode('utf8')).hexdigest()


def is_managed(modifier):
    return getattr(modifier,'type',None)=='NODES' and modifier.node_group is not None and modifier.node_group.get(_PROFILE)==PROFILE


def _face_state(target, face_modifier):
    mesh = face._target(target,write=True)
    group, interface, fields = face._owned(target,face_modifier)
    values={key:face._input(face_modifier,interface,key).value for key in
            ('reference','light','left','right','weight','strength','side_blend','vertical_fade')}
    face._preflight(target,values['reference'],values['light'],values['left'],values['right'],values['weight'],
                    values['strength'],side_blend=values['side_blend'],vertical_fade=values['vertical_fade'],
                    fields=fields,profile=group[face.PROFILE_KEY])
    sun = values['light']
    state = dict(profile=group[face.PROFILE_KEY],owner=group[face.OWNER],
                 modifier_uid=face_modifier.persistent_uid,fields=fields,
                 mix_attribute=fields['mix'],valid_attribute=fields['valid'],
                 graph_signature=face.graph_signature(group))
    if group[face.PROFILE_KEY] in (face.DIRECTION_PROFILE,face.CONTINUITY_PROFILE):
        from . import face_direction_keys
        state['direction_keys'] = face_direction_keys.identity()
    if group[face.PROFILE_KEY] == face.CONTINUITY_PROFILE:
        state['continuity_keys'] = face_direction_keys.continuity_identity()
    return mesh, sun, state


def _owned(target, face_modifier, cast_modifier):
    mesh,sun,state = _face_state(target,face_modifier)
    if getattr(cast_modifier,'id_data',None)!=target or not is_managed(cast_modifier):
        raise CastError('Cast modifier/profile does not belong to this target')
    group=cast_modifier.node_group;owner=group.get(_OWNER)
    if (type(owner) is not str or len(owner)!=32 or any(c not in '0123456789abcdef' for c in owner)
            or group.get(_TARGET)!=target or group.get(_UID)!=cast_modifier.persistent_uid
            or group.get(_SUN)!=sun or group.get(_ATTRIBUTE)!='toon_sdf_sun_'+owner
            or group.get(_FACE)!=_json(state) or group.users!=1 or _animated(group)):
        raise CastError('Cast carrier is rebound, shared, animated or has invalid source identity')
    if graph_signature(group)!=group.get(_SIGNATURE):raise CastError('Cast carrier graph was edited')
    modifiers=list(target.modifiers);index=modifiers.index(cast_modifier)
    if index+1>=len(modifiers) or modifiers[index+1]!=face_modifier:
        raise CastError('Cast carrier must be immediately before its explicit face modifier')
    if sum(m.type=='NODES' and m.node_group is not None and _PROFILE in m.node_group for m in modifiers)!=1:
        raise CastError('Cast carrier must be unique on its target')
    if group[_ATTRIBUTE] in mesh.attributes:raise CastError('Source Mesh already contains the owned cast output name')
    return group,state,sun


def _node(group, key, kind, **properties):
    node=group.nodes.new(kind);node[_NODE]=key;node.label=key
    for name,value in properties.items():setattr(node,name,value)
    return node


def _identity(group, profile, owner, target, uid, sun, attribute, state):
    for key,value in ((_PROFILE,profile),(_OWNER,owner),(_TARGET,target),(_UID,uid),
                      (_SUN,sun),(_ATTRIBUTE,attribute),(_FACE,_json(state))):group[key]=value


def create(target, face_modifier):
    """Add an isolated native carrier, immediately before the given face field."""
    import bpy
    mesh,sun,state=_face_state(target,face_modifier)
    if any(m.type=='NODES' and m.node_group and _PROFILE in m.node_group for m in target.modifiers):
        raise CastError('An existing cast profile occupies this target; no implicit replacement')
    owner=uuid.uuid4().hex;attribute='toon_sdf_sun_'+owner
    if mesh.attributes.get(attribute) is not None:raise CastError('Cast output name already exists')
    group=modifier=None
    try:
        group=bpy.data.node_groups.new('Toon Face SDF Cast Direction','GeometryNodeTree')
        geometry_in=group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        geometry_out=group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
        entry=_node(group,'input','NodeGroupInput');output=_node(group,'output','NodeGroupOutput')
        info=_node(group,'sun','GeometryNodeObjectInfo',transform_space='ORIGINAL')
        _socket(info.inputs,'Object').default_value=sun
        direction=_node(group,'sun_world_z','FunctionNodeTransformDirection');_socket(direction.inputs,'Direction').default_value=(0.,0.,1.)
        normalize=_node(group,'unit_sun','ShaderNodeVectorMath',operation='NORMALIZE')
        store=_node(group,'store_sun','GeometryNodeStoreNamedAttribute',data_type='FLOAT_VECTOR',domain='POINT')
        _socket(store.inputs,'Name').default_value=attribute
        for a,b in [(_socket(info.outputs,'Transform'),_socket(direction.inputs,'Transform')),
                    (_socket(direction.outputs,'Direction'),normalize.inputs[0]),
                    (_socket(normalize.outputs,'Vector'),_socket(store.inputs,'Value')),
                    (_socket(entry.outputs,geometry_in.identifier),_socket(store.inputs,'Geometry')),
                    (_socket(store.outputs,'Geometry'),_socket(output.inputs,geometry_out.identifier))]:group.links.new(a,b)
        for index,node in enumerate(group.nodes):node.location=(index*210,0)
        insertion=list(target.modifiers).index(face_modifier)
        modifier=target.modifiers.new('Toon Face SDF Cast Direction','NODES');modifier.node_group=group
        _identity(group,PROFILE,owner,target,modifier.persistent_uid,sun,attribute,state)
        group[_SIGNATURE]=graph_signature(group)
        with bpy.context.temp_override(object=target,active_object=target,selected_objects=[target],selected_editable_objects=[target]):
            if bpy.ops.object.modifier_move_to_index(modifier=modifier.name,index=insertion)!={'FINISHED'}:
                raise CastError('Could not insert the cast carrier before the explicit face field')
        target.update_tag();validate(target,face_modifier,modifier)
        return modifier
    except Exception:
        if modifier is not None:target.modifiers.remove(modifier)
        if group is not None and group.users==0:bpy.data.node_groups.remove(group)
        raise


def _resolve_reader(reader):
    target=reader.get(_TARGET)
    if target is None:raise CastError('Reader target missing')
    candidates=[m for m in target.modifiers if m.persistent_uid==reader.get(_UID) and is_managed(m)]
    state=json.loads(reader[_FACE])
    faces=[m for m in target.modifiers if m.persistent_uid==state['modifier_uid'] and face.is_managed(m)]
    if len(candidates)!=1 or len(faces)!=1:raise CastError('Reader source modifier missing')
    return target,faces[0],candidates[0]


def reader_edited(reader):
    try:
        if reader.bl_idname!='ShaderNodeTree' or reader.get(_PROFILE)!=READER_PROFILE or _animated(reader):return True
        if graph_signature(reader)!=reader.get(_SIGNATURE):return True
        target,face_modifier,cast_modifier=_resolve_reader(reader)
        group,_,_=_owned(target,face_modifier,cast_modifier)
        return (reader.get(_CORE_SIGNATURE)!=group.get(_SIGNATURE)
                or any(reader.get(key)!=group.get(key) for key in (_OWNER,_TARGET,_UID,_SUN,_ATTRIBUTE,_FACE)))
    except (ValueError,TypeError,KeyError,AttributeError,ReferenceError,RuntimeError):return True


def _readers(group):
    import bpy
    result=[]
    for reader in bpy.data.node_groups:
        if reader.bl_idname!='ShaderNodeTree':continue
        if (reader.get(_OWNER)==group[_OWNER] or reader.get(_ATTRIBUTE)==group[_ATTRIBUTE]
                or (reader.get(_TARGET)==group[_TARGET] and reader.get(_UID)==group[_UID])):result.append(reader)
    return result


def inspect(target, face_modifier, cast_modifier, *, evaluate=True, depsgraph=None):
    import bpy
    group,state,sun=_owned(target,face_modifier,cast_modifier)
    report=dict(profile=PROFILE,status='valid',owner=group[_OWNER],target=target.name,
        modifier_uid=cast_modifier.persistent_uid,normal_attribute=group[_ATTRIBUTE],sun=sun.name,
        face=state,carrier=dict(graph_signature=group[_SIGNATURE],position='immediately_before_face'),
        readers=[dict(name=r.name,users=r.users,author_edited=reader_edited(r)) for r in _readers(group)],
        source_identities=dict(face_graph_signature=state['graph_signature'],cast_graph_signature=group[_SIGNATURE]),
        invalid_points=None,issues=[],binding_effect='Explicit bind redirects only active material Diffuse.Normal through face mix/valid; UV independent')
    report['bindings']=binding_descriptors(target,cast_modifier,live=True)
    if any(row['author_edited'] for row in report['readers']):
        report.update(status='invalid',issues=['Cast reader source identity or author graph changed'])
    if evaluate:
        graph=depsgraph or bpy.context.evaluated_depsgraph_get();ev=target.evaluated_get(graph);mesh=ev.to_mesh()
        try:
            attribute=mesh.attributes.get(group[_ATTRIBUTE])
            if attribute is None or attribute.domain!='POINT' or attribute.data_type!='FLOAT_VECTOR' or len(attribute.data)!=len(mesh.vertices):
                raise CastError('Cast direction output missing or has wrong schema')
            direction=sun.evaluated_get(graph).matrix_world.to_3x3() @ __import__('mathutils').Vector((0.,0.,1.));direction.normalize()
            invalid=sum(not all(math.isfinite(v) for v in item.vector) or (item.vector-direction).length>2e-5 for item in attribute.data)
            report.update(invalid_points=invalid,vertices=len(mesh.vertices),evaluation=graph.mode,light_direction=list(direction))
            from .normal_witness import snapshot
            report['declared_extra_fields']=[group[_ATTRIBUTE]]
            report['field_snapshot']=snapshot(mesh,[group[_ATTRIBUTE]])
            if invalid:raise CastError('Cast direction differs from current evaluated Sun +Z')
        except CastError as error:report.update(status='invalid',issues=report['issues']+[str(error)])
        finally:ev.to_mesh_clear()
    return report


def validate(target, face_modifier, cast_modifier, *, evaluate=True, depsgraph=None):
    report=inspect(target,face_modifier,cast_modifier,evaluate=evaluate,depsgraph=depsgraph)
    if report['status']!='valid':raise CastError('; '.join(report['issues']))
    return report


def _reader(group,state,sun):
    import bpy
    reader=bpy.data.node_groups.new('Toon Face SDF Cast Reader','ShaderNodeTree')
    try:
        _identity(reader,READER_PROFILE,group[_OWNER],group[_TARGET],group[_UID],sun,group[_ATTRIBUTE],state)
        reader[_CORE_SIGNATURE]=group[_SIGNATURE]
        source=reader.interface.new_socket(name='Original Normal',in_out='INPUT',socket_type='NodeSocketVector')
        result=reader.interface.new_socket(name='Normal',in_out='OUTPUT',socket_type='NodeSocketVector')
        reader[_INTERFACE]=_json(dict(original=source.identifier,normal=result.identifier))
        entry=_node(reader,'input','NodeGroupInput');output=_node(reader,'output','NodeGroupOutput')
        attributes={key:_node(reader,'attribute_'+key,'ShaderNodeAttribute',attribute_type='GEOMETRY',attribute_name=name)
                    for key,name in [('sun',group[_ATTRIBUTE]),('mix',state['mix_attribute']),('valid',state['valid_attribute'])]}
        def math_(key,operation,a,b):
            node=_node(reader,key,'ShaderNodeMath',operation=operation)
            for socket,value in zip(node.inputs[:2],(a,b)):
                if type(value) in (int,float):socket.default_value=value
                else:reader.links.new(value,socket)
            return node.outputs[0]
        mix=math_('mix_max','MINIMUM',math_('mix_min','MAXIMUM',attributes['mix'].outputs['Fac'],0.),1.)
        valid=math_('valid_gate','GREATER_THAN',attributes['valid'].outputs['Fac'],.5)
        influence=math_('face_influence','MULTIPLY',mix,valid)
        inverse=math_('original_influence','SUBTRACT',1.,influence)
        original=_node(reader,'original_scaled','ShaderNodeVectorMath',operation='SCALE')
        light=_node(reader,'sun_scaled','ShaderNodeVectorMath',operation='SCALE')
        add=_node(reader,'mixed_normal','ShaderNodeVectorMath',operation='ADD')
        unit=_node(reader,'unit_normal','ShaderNodeVectorMath',operation='NORMALIZE')
        for a,b in [(_socket(entry.outputs,source.identifier),original.inputs[0]),(inverse,_socket(original.inputs,'Scale')),
                    (attributes['sun'].outputs['Vector'],light.inputs[0]),(influence,_socket(light.inputs,'Scale')),
                    (original.outputs['Vector'],add.inputs[0]),(light.outputs['Vector'],add.inputs[1]),
                    (add.outputs['Vector'],unit.inputs[0]),(unit.outputs['Vector'],_socket(output.inputs,result.identifier))]:reader.links.new(a,b)
        for index,node in enumerate(reader.nodes):node.location=((index%6)*220,-(index//6)*220)
        reader[_SIGNATURE]=graph_signature(reader);return reader
    except Exception:
        if reader.users==0:bpy.data.node_groups.remove(reader)
        raise


def _material(target):
    from .normal_ui import _material as exclusive_material, _diffuse
    material=exclusive_material(target);destination,normal=_diffuse(material.node_tree)
    return material,destination,normal


def _reader_socket(node,key,*,output=False):
    interface=json.loads(node.node_tree[_INTERFACE])
    return _socket(node.outputs if output else node.inputs,interface[key])


def _related_animation(tree,sockets,nodes=()):
    from .artistic_normals import _animated_paths
    paths=_animated_paths(tree)
    if any(socket.path_from_id('default_value') in paths for socket in sockets):return True
    prefixes=[node.path_from_id() for node in nodes]
    return any(path==base or path.startswith(base+'.') or path.startswith(base+'[') for path in paths for base in prefixes)


def _lighting_normal_links(tree, output, normal):
    """Accept only explicitly validated 5.3 consumers of the existing cast output."""
    import bpy
    if not is_supported_host(bpy.app.version):
        return ()
    from . import lighting_53
    try:
        return lighting_53._cast_normal_consumers(tree, output, normal)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise CastError('Incompatible 5.3 cast Normal consumers: ' + str(error)) from error


def _binding_state(tree,token):
    reader=[n for n in tree.nodes if n.get(_TOKEN)==token]
    destination=[n for n in tree.nodes if n.get(_DEST)==token]
    source=[n for n in tree.nodes if n.get(_SOURCE)==token]
    if len(reader)!=1 or len(destination)!=1 or len(source)>1:return None
    node=reader[0];normal=_socket(destination[0].inputs,'Normal')
    extras=_lighting_normal_links(tree,_reader_socket(node,'normal',output=True),normal)
    def identity(n):
        for key,label in ((_TOKEN,'reader'),(_DEST,'destination'),(_SOURCE,'source')):
            if n.get(key)==token:return [label,token]
        return ['node',n.name]
    links=sorted([(identity(l.from_node),l.from_socket.identifier,identity(l.to_node),l.to_socket.identifier)
                  for l in tree.links if (l.from_node==node or l.to_node==node) and l not in extras],key=repr)
    fallbacks=[n for n in tree.nodes if n.get(_MARK)==token]
    return _json(dict(reader=_node_state(node),links=links,normal_default=_value(normal.default_value),
        destination_mute=destination[0].mute,source=_node_state(source[0]) if source else None,
        fallback=[_node_state(n) for n in fallbacks],original=node.get(_ORIGINAL)))


def _clean(node,key,token):
    if node.get(key)==token:del node[key]


def _remove_node(tree,node):
    if _TARGET in node:del node[_TARGET]
    tree.nodes.remove(node)


def binding_descriptors(target, cast_modifier, *, live=True):
    """Validate explicit target-material instances; return primitive witnesses.

    live=False still requires the explicit modifier identity, checks material
    links/snapshots/reader graph, and skips recursive live source validation.
    After deleting modifiers a freeze caller must compare its saved primitive
    state with _binding_state(tree, token); no deleted RNA reference is needed.
    """
    import bpy
    if type(live) is not bool:raise CastError('live must be explicit bool')
    if getattr(cast_modifier,'id_data',None)!=target or not is_managed(cast_modifier):
        raise CastError('Expected the explicit cast modifier on this target')
    group=cast_modifier.node_group
    materials=[]
    for slot in target.material_slots:
        material=slot.material
        if material is not None and material not in materials:materials.append(material)
    results=[];used_readers=[];tokens=set()
    from .normal_ui import _diffuse
    for material in materials:
        tree=material.node_tree
        if tree is None:continue
        instances=[n for n in tree.nodes if _TOKEN in n]
        marks=[n for n in tree.nodes if any(key in n for key in (_DEST,_SOURCE,_MARK))]
        if not instances:
            if marks:raise CastError('Orphan cast source/destination marks in explicit target material')
            continue
        if len(instances)!=1:raise CastError('Target material has ambiguous cast reader instances')
        if (material.library or material.override_library or not material.is_editable
                or tree.library or not tree.is_editable or not material.use_nodes):
            raise CastError('Bound cast material is no longer local/editable')
        consumers=bpy.data.user_map(subset={material}).get(material,set())
        if any(user not in {target,target.data} for user in consumers):
            raise CastError('Bound cast material has foreign consumers')
        node=instances[0];token=node.get(_TOKEN);reader=node.node_tree if node.bl_idname=='ShaderNodeGroup' else None
        if (type(token) is not str or len(token)!=32 or any(c not in '0123456789abcdef' for c in token) or token in tokens
                or node.get(_PROFILE)!=BINDING or node.get(_TARGET)!=target
                or node.get(_UID)!=cast_modifier.persistent_uid or node.get(_OWNER)!=group.get(_OWNER)
                or reader is None or reader.users!=1 or reader.get(_PROFILE)!=READER_PROFILE
                or graph_signature(reader)!=reader.get(_SIGNATURE) or _animated(reader)
                or reader.get(_CORE_SIGNATURE)!=group.get(_SIGNATURE)
                or any(reader.get(key)!=group.get(key) for key in (_OWNER,_TARGET,_UID,_SUN,_ATTRIBUTE,_FACE))
                or (live and reader_edited(reader))):
            raise CastError('Bound cast instance/reader/source identity is edited, shared or foreign')
        tokens.add(token);used_readers.append(reader)
        destination,normal=_diffuse(tree)
        if [n for n in tree.nodes if n.get(_DEST)==token]!=[destination]:
            raise CastError('Bound cast destination identity changed')
        if _related_animation(tree,[normal],nodes=[node]):raise CastError('Bound cast input/instance has native animation')
        output=_reader_socket(node,'normal',output=True)
        extras=_lighting_normal_links(tree,output,normal)
        if (len(output.links)!=1+len(extras) or len(normal.links)!=1
                or normal.links[0].from_socket!=output):
            raise CastError('Cast reader does not feed exactly the explicit Diffuse.Normal')
        original=json.loads(node[_ORIGINAL])
        if (type(original) is not dict or set(original)!={'linked','source_socket','normal'}
                or type(original['linked']) is not bool or type(original['normal']) is not list or len(original['normal'])!=3
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in original['normal'])):
            raise CastError('Cast original Normal restoration record is invalid')
        sources=[n for n in tree.nodes if n.get(_SOURCE)==token]
        fallback=[n for n in tree.nodes if n.get(_MARK)==token]
        incoming=_reader_socket(node,'original')
        if len(incoming.links)!=1:raise CastError('Cast Original Normal source link is ambiguous/missing')
        if original['linked']:
            if len(sources)!=1 or fallback or type(original['source_socket']) is not str:
                raise CastError('Cast original linked source identity changed')
            source=sources[0];source_socket=_socket(source.outputs,original['source_socket'])
            if incoming.links[0].from_socket!=source_socket or _related_animation(tree,[],nodes=[source]):
                raise CastError('Cast Original Normal no longer reads the witnessed original source')
            source_desc=dict(kind='linked',node=source.name,node_type=source.bl_idname,socket=source_socket.identifier)
        else:
            if sources or len(fallback)!=1 or original['source_socket'] is not None:
                raise CastError('Cast Geometry.Normal fallback identity changed')
            source=fallback[0]
            if (source.bl_idname!='ShaderNodeNewGeometry' or incoming.links[0].from_socket!=_socket(source.outputs,'Normal')
                    or any(l.to_node!=node for socket in source.outputs for l in socket.links)
                    or _related_animation(tree,[],nodes=[source])):
                raise CastError('Cast Geometry.Normal fallback changed or gained consumers')
            source_desc=dict(kind='Geometry.Normal fallback',node=source.name,node_type=source.bl_idname,socket='Normal')
        if any(any(n.get(key)!=token for key in (_DEST,_SOURCE,_MARK) if key in n) for n in marks):
            raise CastError('Foreign/orphan cast marks remain in the bound material')
        state=_binding_state(tree,token)
        if type(node.get(_SNAPSHOT)) is not str or state!=node.get(_SNAPSHOT):
            raise CastError('Cast material binding links/values differ from their explicit witness')
        results.append(dict(profile=BINDING,material=material.name,node=node.name,token=token,
            source=source_desc,destination=dict(node=destination.name,node_type=destination.bl_idname,socket=normal.identifier),
            original_default=original['normal'],reader=reader.name,reader_graph_signature=reader[_SIGNATURE],
            binding_sha256=hashlib.sha256(state.encode('utf8')).hexdigest(),
            normal_attribute=reader[_ATTRIBUTE],target=target.name,modifier_uid=cast_modifier.persistent_uid,
            source_live_validated=live,changes_link='Diffuse.Normal explicitly redirected'))
    for reader in _readers(group):
        if reader.users and reader not in used_readers:
            raise CastError('Used cast reader has no witnessed instance on an explicit target material')
    return results


def bind(target, face_modifier, cast_modifier):
    """Explicitly redirect the active exclusive material's Diffuse.Normal."""
    group,state,sun=_owned(target,face_modifier,cast_modifier)
    material,destination,normal=_material(target);tree=material.node_tree
    if any(any(key in n for key in (_TOKEN,_DEST,_SOURCE,_MARK)) for n in tree.nodes):
        raise CastError('Existing cast binding or orphan identity marks; no implicit replacement')
    if len(normal.links)>1 or _related_animation(tree,[normal]):
        raise CastError('Diffuse.Normal has ambiguous links or native animation')
    old=normal.links[0] if normal.links else None
    source,source_socket=(old.from_node,old.from_socket) if old else (None,None)
    if source==destination:raise CastError('Self-linked Diffuse.Normal cannot be restored safely')
    if source is not None and _related_animation(tree,[],nodes=[source]):raise CastError('Original normal source has native animation')
    constant=tuple(normal.default_value);_json(list(constant))
    reader=_reader(group,state,sun);token=uuid.uuid4().hex;node=fallback=None
    try:
        node=tree.nodes.new('ShaderNodeGroup');node.node_tree=reader;node.label='SDF face cast sensor (explicit Normal redirect)'
        node[_TOKEN],node[_TARGET],node[_UID],node[_OWNER]=token,target,cast_modifier.persistent_uid,group[_OWNER]
        node[_PROFILE]=BINDING
        destination[_DEST]=token
        if source is not None:
            source[_SOURCE]=token;tree.links.new(source_socket,_reader_socket(node,'original'))
        else:
            fallback=tree.nodes.new('ShaderNodeNewGeometry');fallback[_MARK]=token
            tree.links.new(_socket(fallback.outputs,'Normal'),_reader_socket(node,'original'))
        node[_ORIGINAL]=_json(dict(linked=source is not None,source_socket=source_socket.identifier if source_socket else None,normal=list(constant)))
        tree.links.new(_reader_socket(node,'normal',output=True),normal)
        node[_SNAPSHOT]=_binding_state(tree,token)
        binding_descriptors(target,cast_modifier,live=True)
    except Exception:
        if node is not None:_remove_node(tree,node)
        if fallback is not None:tree.nodes.remove(fallback)
        _clean(destination,_DEST,token)
        if source is not None:_clean(source,_SOURCE,token);tree.links.new(source_socket,normal)
        normal.default_value=constant
        if reader.users==0:__import__('bpy').data.node_groups.remove(reader)
        raise
    return dict(profile=BINDING,status='bound',material=material.name,reader=reader.name,token=token,
                destination='Diffuse.Normal',original_linked=source is not None,
                normal_attribute=group[_ATTRIBUTE],effect='Explicitly redirected Normal; face mix/valid limits sensor to covered points')


def unbind(target, face_modifier, cast_modifier):
    """Restore the exact original link/default; retain the original normal reader."""
    group,_,_=_owned(target,face_modifier,cast_modifier)
    binding_descriptors(target,cast_modifier,live=True)
    material,destination,normal=_material(target);tree=material.node_tree
    instances=[n for n in tree.nodes if _TOKEN in n]
    if len(instances)!=1:raise CastError('Expected exactly one explicit cast reader instance')
    node=instances[0];token=node.get(_TOKEN);reader=node.node_tree if node.bl_idname=='ShaderNodeGroup' else None
    if reader is not None and len(_reader_socket(node,'normal',output=True).links)!=1:
        raise CastError('Cast Normal is still used by the 5.3 face-lighting controls')
    if (type(token) is not str or len(token)!=32 or node.get(_PROFILE)!=BINDING or node.get(_TARGET)!=target or node.get(_UID)!=cast_modifier.persistent_uid
            or node.get(_OWNER)!=group[_OWNER] or reader is None or reader_edited(reader)
            or reader.users!=1 or node.get(_SNAPSHOT)!=_binding_state(tree,token)):
        raise CastError('Cast binding identity, graph, links or consumers changed; preserve author work')
    if [n for n in tree.nodes if n.get(_DEST)==token]!=[destination] or _related_animation(tree,[normal],nodes=[node]):
        raise CastError('Destination or binding native animation changed')
    original=json.loads(node[_ORIGINAL]);sources=[n for n in tree.nodes if n.get(_SOURCE)==token]
    if len(sources)!=int(original['linked']):raise CastError('Original normal source identity changed')
    source=sources[0] if sources else None
    source_socket=_socket(source.outputs,original['source_socket']) if source is not None else None
    fallback=[n for n in tree.nodes if n.get(_MARK)==token]
    if (len(fallback)!=int(not original['linked']) or any(n.bl_idname!='ShaderNodeNewGeometry' or any(l.to_node!=node for socket in n.outputs for l in socket.links) for n in fallback)
            or _related_animation(tree,[],nodes=([source] if source else [])+fallback)):
        raise CastError('Original/fallback source changed or gained consumers/animation')
    if source_socket is not None:
        try:tree.links.new(source_socket,normal)
        except Exception:tree.links.new(_reader_socket(node,'normal',output=True),normal);raise
    normal.default_value=original['normal'];name=node.name;_remove_node(tree,node)
    for n in fallback:tree.nodes.remove(n)
    _clean(destination,_DEST,token)
    if source is not None:_clean(source,_SOURCE,token)
    return dict(profile=BINDING,status='unbound',material=material.name,removed_node=name,
                restored_original_link=source_socket is not None,reader_retained=reader.name,original_reader_untouched=True)


def remove(target, face_modifier, cast_modifier):
    """Remove only a verified unused carrier/readers, never unknown consumers."""
    import bpy
    group,_,_=_owned(target,face_modifier,cast_modifier);readers=_readers(group)
    if any(reader_edited(reader) for reader in readers):raise CastError('Cast reader was edited or lost source identity')
    if any(reader.users for reader in readers):raise CastError('Cast reader still has consumers; explicitly unbind first')
    trees=[tree for tree in bpy.data.node_groups if tree.bl_idname=='ShaderNodeTree']
    trees.extend(mat.node_tree for mat in bpy.data.materials if mat.node_tree is not None)
    for tree in trees:
        if tree in readers:continue
        if any(n.bl_idname=='ShaderNodeAttribute' and n.attribute_name==group[_ATTRIBUTE] for n in tree.nodes):
            raise CastError('Unverified Shader Attribute consumer reads the cast direction')
    from .artistic_normals import _animated_paths
    prefix=cast_modifier.path_from_id()
    if any(path==prefix or path.startswith(prefix+'.') or path.startswith(prefix+'[') for path in _animated_paths(target)):
        raise CastError('Removing cast carrier would discard native modifier animation')
    owner=group[_OWNER];attribute=group[_ATTRIBUTE];target.modifiers.remove(cast_modifier)
    for reader in readers:bpy.data.node_groups.remove(reader)
    if group.users==0:bpy.data.node_groups.remove(group)
    return dict(profile=PROFILE,status='removed',owner=owner,normal_attribute=attribute)
