# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in affine ellipsoid shading fields, without changing mesh normals.

The GN stores world-space point attributes before camera correction. A separate
editable shader reader blends them with Geometry.Normal. No material is bound,
mesh attribute/weight authored, handler installed or native normal replaced.
"""
import hashlib
import json
import math
import uuid

PROFILE = 'toon-artistic-normal.v1'
MAPPED_PROFILE = 'toon-artistic-normal.v2'
SPACE = 'WORLD_PRE_CAMERA_CORRECTION'
OWNER = 'toon_artistic_normal_owner'
PROFILE_KEY = 'toon_artistic_normal_profile'
_TARGET = 'toon_artistic_normal_target'
_UID = 'toon_artistic_normal_modifier_uid'
_INTERFACE = 'toon_artistic_normal_interface'
_FIELDS = 'toon_artistic_normal_attributes'
_SIGNATURE = 'toon_artistic_normal_signature'
_NODE = 'toon_artistic_normal_node'
_SHADER = 'toon-artistic-normal-shader.v1'
_EPSILON = 1e-6
_UNCHANGED = object()


class NormalError(ValueError):
    """Unqualified affine field, changed managed graph or protected consumer."""


def _strength(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise NormalError('strength must be finite in [0,1], never bool')
    return float(value)


def _matrix(matrix):
    try:
        result = [[float(v) for v in row] for row in matrix]
    except (TypeError, ValueError, OverflowError) as error:
        raise NormalError('A finite affine 4x4 matrix is required') from error
    if (len(result) != 4 or any(len(row) != 4 for row in result)
            or not all(math.isfinite(v) for row in result for v in row)
            or result[3] != [0.0, 0.0, 0.0, 1.0]):
        raise NormalError('A finite affine 4x4 matrix is required')
    return result


def _inverse(matrix):
    source = _matrix(matrix)
    rows = [row[:] + [float(i == j) for j in range(4)] for i, row in enumerate(source)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda i: abs(rows[i][column]))
        if rows[pivot][column] == 0:
            raise NormalError('Affine matrices must be invertible')
        rows[pivot], rows[column] = rows[column], rows[pivot]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        for i in range(4):
            if i != column:
                amount = rows[i][column]
                rows[i] = [a - amount*b for a, b in zip(rows[i], rows[column])]
    result = [row[4:] for row in rows]
    if not all(math.isfinite(v) for row in result for v in row):
        raise NormalError('Affine inverse must be finite')
    return result


def reference_normal(point, target_matrix, reference_matrix):
    """Pure scalar oracle: normalize(B^-T * (B^-1 * (A * P))).

    Matrices are full affine 4x4 iterables. The returned XYZ is world space;
    inverse-transpose multiplication treats q as a direction (w=0).
    """
    if len(point) != 3 or not all(math.isfinite(v) for v in point):
        raise NormalError('A finite local XYZ point is required')
    a, inverse = _matrix(target_matrix), _inverse(reference_matrix)
    _inverse(a)
    world = [sum(a[i][j]*point[j] for j in range(3)) + a[i][3] for i in range(3)]
    q = [sum(inverse[i][j]*world[j] for j in range(3)) + inverse[i][3] for i in range(3)]
    if not all(math.isfinite(v) for v in q) or math.hypot(*q) <= _EPSILON:
        raise NormalError('Ellipsoid reference coordinate is at/near its center or nonfinite')
    normal = [sum(inverse[j][i]*q[j] for j in range(3)) for i in range(3)]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length <= 0.0:
        raise NormalError('Ellipsoid normal is degenerate or nonfinite')
    return tuple(v/length for v in normal)


def _socket(values, identifier):
    found = [value for value in values if value.identifier == identifier]
    if len(found) != 1:
        raise NormalError('Expected exactly one node socket: ' + identifier)
    return found[0]


def _signature(group):
    from .runtime_compat import signature_inputs
    def value(item):
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        if hasattr(item, 'name_full'):
            return ['ID', item.bl_rna.identifier, item.name_full]
        try:
            return [value(v) for v in item]
        except TypeError:
            return str(item)
    rows = []
    for node in group.nodes:
        props = {key:getattr(node, key) for key in ('operation','data_type','input_type',
            'domain','transform_space','attribute_name','attribute_type') if hasattr(node, key)}
        rows.append([node.get(_NODE), node.bl_idname, props,
            [(socket.identifier,value(socket.default_value)) for socket in signature_inputs(node) if hasattr(socket,'default_value')]])
    links = sorted(((link.from_node.get(_NODE),link.from_socket.identifier,
                    link.to_node.get(_NODE),link.to_socket.identifier) for link in group.links),key=repr)
    sockets = []
    for item in group.interface.items_tree:
        if item.item_type == 'SOCKET':
            sockets.append({key:value(getattr(item,key)) for key in
                ('identifier','name','in_out','socket_type','default_value','min_value','max_value')
                if hasattr(item,key)})
    payload = dict(nodes=sorted(rows,key=lambda row:str(row[0])),links=links,
                   interface=group.get(_INTERFACE),sockets=sockets,fields=group.get(_FIELDS))
    return hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _input(modifier, interface, key):
    return getattr(modifier.properties.inputs, interface[key])


def _animated_paths(target):
    animation = target.animation_data
    if animation is None:
        return set()
    paths = {curve.data_path for curve in animation.drivers}
    actions = [animation.action] if animation.action else []
    for track in animation.nla_tracks:
        actions.extend(strip.action for strip in track.strips if strip.action)
    for action in actions:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip,'channelbags',()):
                    paths.update(curve.data_path for curve in bag.fcurves)
    return paths


def _weights(mesh, name):
    if type(name) is not str or not name or len(name) > 63:
        raise NormalError('weight_attribute must be an existing named FLOAT POINT attribute')
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != 'POINT' or attribute.data_type != 'FLOAT':
        raise NormalError('weight_attribute must be an existing FLOAT POINT attribute')
    if any(not math.isfinite(value.value) or not 0 <= value.value <= 1 for value in attribute.data):
        raise NormalError('weight_attribute values must be finite in [0,1]')


def _mapping_values(mesh, name):
    if type(name) is not str or not name or len(name.encode('utf8'))>63 or '\x00' in name:
        raise NormalError('mapping_attribute must name an existing FLOAT_VECTOR POINT attribute')
    attribute=mesh.attributes.get(name)
    if (attribute is None or attribute.domain!='POINT' or attribute.data_type!='FLOAT_VECTOR'
            or len(attribute.data)!=len(mesh.vertices)):
        raise NormalError('mapping_attribute must be FLOAT_VECTOR POINT with one direction per vertex')
    if any(not all(math.isfinite(v) for v in item.vector) or math.hypot(*item.vector)<=_EPSILON for item in attribute.data):
        raise NormalError('mapping_attribute directions must be finite and nonzero')
    return attribute


def is_managed(modifier):
    """Recognize both supported profiles; ownership is checked by inspect/validate."""
    return (getattr(modifier,'type',None)=='NODES' and modifier.node_group is not None
            and modifier.node_group.get(PROFILE_KEY) in {PROFILE,MAPPED_PROFILE})


def _preflight(target, reference, weight, strength, depsgraph=None):
    import bpy
    strength = _strength(strength)
    if target is None or target.type != 'MESH' or target.library or target.override_library:
        raise NormalError('A local editable MESH target is required')
    if reference is None or reference == target or reference not in list(bpy.data.objects):
        raise NormalError('An existing independent reference Object is required')
    _weights(target.data, weight)
    graph = depsgraph or bpy.context.evaluated_depsgraph_get()
    _inverse(target.evaluated_get(graph).matrix_world)
    _inverse(reference.evaluated_get(graph).matrix_world)
    cursor, seen = reference, set()
    while cursor is not None:
        if cursor == target or cursor.as_pointer() in seen:
            raise NormalError('Reference parenting/feedback from the target is unsupported')
        seen.add(cursor.as_pointer())
        if any(getattr(constraint,'target',None) == target for constraint in cursor.constraints):
            raise NormalError('Reference constraint feedback from the target is unsupported')
        cursor = cursor.parent
    return strength, graph


def _is_correction(modifier):
    return (modifier.type == 'NODES' and modifier.node_group is not None
        and modifier.node_group.get('toon_camera_correction_profile') == 'toon-camera-xy-depth.v1')


def _downstream(target, start):
    for modifier in list(target.modifiers)[start:]:
        if modifier.show_viewport or modifier.show_render:
            if not _is_correction(modifier):
                raise NormalError('Only an owned camera correction may follow the artistic normal field')
            from . import camera_correction
            # Public inspection recognizes the managed graph; no private API coupling.
            camera_correction.inspect(target, modifier, evaluate=False)


def _owned(target, modifier):
    if modifier.id_data != target or modifier.type != 'NODES':
        raise NormalError('The artistic normal modifier does not belong to this target')
    group = modifier.node_group
    if (group is None or group.get(PROFILE_KEY) not in {PROFILE,MAPPED_PROFILE} or not group.get(OWNER)
            or group.get(_TARGET) != target or group.get(_UID) != modifier.persistent_uid
            or group.users != 1):
        raise NormalError('The artistic normal graph is unowned, shared or rebound')
    owner=group[OWNER]
    if type(owner) is not str or len(owner)!=32 or any(value not in '0123456789abcdef' for value in owner):
        raise NormalError('The artistic normal owner UUID changed or is invalid')
    try:
        fields=json.loads(group[_FIELDS])
    except (KeyError,TypeError,ValueError) as error:
        raise NormalError('The artistic normal field ownership mapping is invalid') from error
    if fields!={key:'toon_art_'+key+'_'+owner for key in ('normal','mix','valid')}:
        raise NormalError('The artistic normal owner UUID differs from its owned field names')
    if _signature(group) != group.get(_SIGNATURE):
        raise NormalError('The managed artistic normal graph was edited')
    index = next(i for i,value in enumerate(target.modifiers) if value.persistent_uid == modifier.persistent_uid)
    _downstream(target,index+1)
    return group, json.loads(group[_INTERFACE]), fields


def _build_group(owner):
    import bpy
    group = bpy.data.node_groups.new('Toon Artistic Normal Field','GeometryNodeTree')
    group[OWNER],group[PROFILE_KEY] = owner,PROFILE
    fields = {key:'toon_art_'+key+'_'+owner for key in ('normal','mix','valid')}
    group[_FIELDS] = json.dumps(fields,sort_keys=True)
    interface = {}
    for key,name,kind in [('geometry','Geometry','NodeSocketGeometry'),('reference','Ellipsoid Reference','NodeSocketObject'),
                          ('weight','Weight Attribute','NodeSocketString'),('strength','Strength','NodeSocketFloat')]:
        socket = group.interface.new_socket(name=name,in_out='INPUT',socket_type=kind)
        interface[key] = socket.identifier
        if key == 'strength':
            socket.default_value=0.0;socket.min_value=0.0;socket.max_value=1.0
    interface['output'] = group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry').identifier
    group[_INTERFACE] = json.dumps(interface,sort_keys=True)
    def node(key,kind,**props):
        value=group.nodes.new(kind);value[_NODE]=key;value.label=key
        for name,prop in props.items():setattr(value,name,prop)
        return value
    def link(source,dest):group.links.new(source,dest)
    def math_(key,operation,a,b):
        value=node(key,'ShaderNodeMath',operation=operation)
        for identifier,operand in [('Value',a),('Value_001',b)]:
            socket=_socket(value.inputs,identifier)
            if isinstance(operand,(int,float)):socket.default_value=operand
            else:link(operand,socket)
        return _socket(value.outputs,'Value')
    def boolean(key,operation,a,b=None):
        value=node(key,'FunctionNodeBooleanMath',operation=operation)
        link(a,_socket(value.inputs,'Boolean'))
        if b is not None:link(b,_socket(value.inputs,'Boolean_001'))
        return _socket(value.outputs,'Boolean')
    def transform(key,kind,vector,matrix):
        value=node(key,kind)
        link(vector,_socket(value.inputs,'Direction' if kind=='FunctionNodeTransformDirection' else 'Vector'))
        link(matrix,_socket(value.inputs,'Transform'))
        return _socket(value.outputs,'Direction' if kind=='FunctionNodeTransformDirection' else 'Vector')
    entry=node('input','NodeGroupInput');output=node('output','NodeGroupOutput')
    position=node('position','GeometryNodeInputPosition')
    self_node=node('self','GeometryNodeSelfObject')
    self_info=node('target_matrix','GeometryNodeObjectInfo',transform_space='ORIGINAL')
    link(_socket(self_node.outputs,'Self Object'),_socket(self_info.inputs,'Object'))
    ref_info=node('reference_matrix','GeometryNodeObjectInfo',transform_space='ORIGINAL')
    link(_socket(entry.outputs,interface['reference']),_socket(ref_info.inputs,'Object'))
    inv_ref=node('inverse_reference','FunctionNodeInvertMatrix')
    inv_self=node('inverse_target','FunctionNodeInvertMatrix')
    link(_socket(ref_info.outputs,'Transform'),_socket(inv_ref.inputs,'Matrix'))
    link(_socket(self_info.outputs,'Transform'),_socket(inv_self.inputs,'Matrix'))
    transpose=node('inverse_transpose','FunctionNodeTransposeMatrix')
    link(_socket(inv_ref.outputs,'Matrix'),_socket(transpose.inputs,'Matrix'))
    world=transform('local_to_world','FunctionNodeTransformPoint',_socket(position.outputs,'Position'),_socket(self_info.outputs,'Transform'))
    q=transform('reference_coordinate','FunctionNodeTransformPoint',world,_socket(inv_ref.outputs,'Matrix'))
    raw=transform('world_normal','FunctionNodeTransformDirection',q,_socket(transpose.outputs,'Matrix'))
    normalized=node('normalize_world','ShaderNodeVectorMath',operation='NORMALIZE')
    link(raw,_socket(normalized.inputs,'Vector'))
    length=node('reference_length','ShaderNodeVectorMath',operation='LENGTH')
    link(q,_socket(length.inputs,'Vector'))
    normal_length=node('world_normal_length','ShaderNodeVectorMath',operation='LENGTH')
    link(raw,_socket(normal_length.inputs,'Vector'))
    weight=node('weight','GeometryNodeInputNamedAttribute',data_type='FLOAT')
    link(_socket(entry.outputs,interface['weight']),_socket(weight.inputs,'Name'))
    w=_socket(weight.outputs,'Attribute');s=_socket(entry.outputs,interface['strength'])
    valid_items=[_socket(inv_ref.outputs,'Invertible'),_socket(inv_self.outputs,'Invertible'),_socket(weight.outputs,'Exists'),
        math_('noncentral','GREATER_THAN',_socket(length.outputs,'Value'),_EPSILON),
        math_('reference_finite','LESS_THAN',_socket(length.outputs,'Value'),3.402823e38),
        math_('normal_nondegenerate','GREATER_THAN',_socket(normal_length.outputs,'Value'),0.0),
        math_('normal_finite','LESS_THAN',_socket(normal_length.outputs,'Value'),3.402823e38),
        math_('weight_not_nan','GREATER_THAN',w,-1.0),
        boolean('weight_nonnegative','NOT',math_('weight_negative','LESS_THAN',w,0.0)),
        boolean('weight_at_most_one','NOT',math_('weight_over_one','LESS_THAN',1.0,w)),
        boolean('strength_nonnegative','NOT',math_('strength_negative','LESS_THAN',s,0.0)),
        math_('strength_not_nan','GREATER_THAN',s,-1.0),
        boolean('strength_at_most_one','NOT',math_('strength_over_one','LESS_THAN',1.0,s))]
    valid=valid_items[0]
    for i,predicate in enumerate(valid_items[1:]):valid=boolean('valid_'+str(i),'AND',valid,predicate)
    vector_switch=node('safe_world','GeometryNodeSwitch',input_type='VECTOR')
    link(valid,_socket(vector_switch.inputs,'Switch'))
    _socket(vector_switch.inputs,'False').default_value=(0.0,0.0,1.0)
    link(_socket(normalized.outputs,'Vector'),_socket(vector_switch.inputs,'True'))
    mix_switch=node('safe_mix','GeometryNodeSwitch',input_type='FLOAT')
    link(valid,_socket(mix_switch.inputs,'Switch'));_socket(mix_switch.inputs,'False').default_value=0.0
    link(math_('effective_mix','MULTIPLY',s,w),_socket(mix_switch.inputs,'True'))
    geometry=_socket(entry.outputs,interface['geometry'])
    for key,kind,field in [('normal','FLOAT_VECTOR',_socket(vector_switch.outputs,'Output')),
                           ('mix','FLOAT',_socket(mix_switch.outputs,'Output')),('valid','BOOLEAN',valid)]:
        store=node('store_'+key,'GeometryNodeStoreNamedAttribute',data_type=kind,domain='POINT')
        _socket(store.inputs,'Name').default_value=fields[key]
        link(geometry,_socket(store.inputs,'Geometry'));link(field,_socket(store.inputs,'Value'))
        geometry=_socket(store.outputs,'Geometry')
    link(geometry,_socket(output.inputs,interface['output']))
    for i,value in enumerate(group.nodes):value.location=((i%7)*220,-(i//7)*220)
    group[_SIGNATURE]=_signature(group)
    return group,interface


def _build_mapped_group(owner):
    """Reuse the unchanged v1 constructor, then replace only its direction source."""
    import bpy
    group,interface=_build_group(owner)
    try:
        group[PROFILE_KEY]=MAPPED_PROFILE
        socket=group.interface.new_socket(name='Mapping Attribute',in_out='INPUT',socket_type='NodeSocketString')
        interface['map']=socket.identifier;group[_INTERFACE]=json.dumps(interface,sort_keys=True)
        entry=next(node for node in group.nodes if node.get(_NODE)=='input')
        mapping=group.nodes.new('GeometryNodeInputNamedAttribute');mapping.data_type='FLOAT_VECTOR'
        mapping[_NODE]='mapping';mapping.label='mapping'
        group.links.new(_socket(entry.outputs,interface['map']),_socket(mapping.inputs,'Name'))
        for key,identifier in [('world_normal','Direction'),('reference_length','Vector')]:
            node=next(node for node in group.nodes if node.get(_NODE)==key)
            destination=_socket(node.inputs,identifier)
            for link in list(destination.links):group.links.remove(link)
            group.links.new(_socket(mapping.outputs,'Attribute'),destination)
        # The existing noncentral/finite guards now consume the authored vector.
        valid_node=next(node for node in group.nodes if node.get(_NODE)=='safe_world')
        valid_input=_socket(valid_node.inputs,'Switch');valid_source=valid_input.links[0].from_socket
        exists=group.nodes.new('FunctionNodeBooleanMath');exists.operation='AND'
        exists[_NODE]='mapping_exists';exists.label='mapping_exists'
        group.links.new(valid_source,_socket(exists.inputs,'Boolean'))
        group.links.new(_socket(mapping.outputs,'Exists'),_socket(exists.inputs,'Boolean_001'))
        combined=_socket(exists.outputs,'Boolean')
        for node_key,socket_key in [('safe_world','Switch'),('safe_mix','Switch'),('store_valid','Value')]:
            node=next(node for node in group.nodes if node.get(_NODE)==node_key)
            destination=_socket(node.inputs,socket_key)
            for link in list(destination.links):group.links.remove(link)
            group.links.new(combined,destination)
        mapping.location=(-440,-440);exists.location=(-220,-440)
        group[_SIGNATURE]=_signature(group)
        return group,interface
    except Exception:
        if group.users==0:bpy.data.node_groups.remove(group)
        raise


def build(target, reference, weight_attribute, *, strength=0.0):
    """Append after native deform, automatically before an owned camera correction."""
    import bpy
    _preflight(target,reference,weight_attribute,strength)
    for value in target.modifiers:
        marker=value.node_group.get(PROFILE_KEY) if value.type=='NODES' and value.node_group else None
        if type(marker) is str and marker.startswith('toon-artistic-normal.v') and marker not in {PROFILE,MAPPED_PROFILE}:
            raise NormalError('An unknown artistic normal profile already occupies this target; no implicit second field is allowed')
    if any(is_managed(value) for value in target.modifiers):
        raise NormalError('This target already has an artistic normal field')
    before=list(target.modifiers)
    insertion=next((i for i,value in enumerate(before) if _is_correction(value) and (value.show_viewport or value.show_render)),len(before))
    _downstream(target,insertion)
    group=modifier=None
    try:
        group,interface=_build_group(uuid.uuid4().hex)
        modifier=target.modifiers.new('Toon Artistic Normal Field','NODES');modifier.node_group=group
        group[_TARGET],group[_UID]=target,modifier.persistent_uid
        if insertion<len(before):
            with bpy.context.temp_override(object=target,active_object=target,selected_objects=[target],selected_editable_objects=[target]):
                if bpy.ops.object.modifier_move_to_index(modifier=modifier.name,index=insertion)!={'FINISHED'}:
                    raise NormalError('Could not place artistic normals before camera correction')
        configure(target,modifier,reference=reference,weight_attribute=weight_attribute,strength=strength)
        return modifier
    except Exception:
        if modifier is not None:target.modifiers.remove(modifier)
        if group is not None and group.users==0:bpy.data.node_groups.remove(group)
        raise


def configure(target, modifier, *, reference=_UNCHANGED,
              weight_attribute=_UNCHANGED, strength=_UNCHANGED,
              mapping_attribute=_UNCHANGED):
    group,interface,fields=_owned(target,modifier)
    if (group[PROFILE_KEY]==MAPPED_PROFILE and reference is not _UNCHANGED
            and reference!=_input(modifier,interface,'reference').value):
        raise NormalError('A mapped reference cannot be rebound to another Object; clear mapped mode and reinitialize explicitly')
    if reference is _UNCHANGED:reference=_input(modifier,interface,'reference').value
    if weight_attribute is _UNCHANGED:weight_attribute=_input(modifier,interface,'weight').value
    if strength is _UNCHANGED:strength=_input(modifier,interface,'strength').value
    strength,_=_preflight(target,reference,weight_attribute,strength)
    values=dict(reference=reference,weight=weight_attribute,strength=strength)
    if group[PROFILE_KEY]==PROFILE and mapping_attribute is not _UNCHANGED:
        raise NormalError('v1 has no mapping input; use set_mapping for an explicit mode switch')
    if group[PROFILE_KEY]==MAPPED_PROFILE:
        if mapping_attribute is _UNCHANGED:mapping_attribute=_input(modifier,interface,'map').value
        if mapping_attribute in fields.values():raise NormalError('mapping_attribute cannot alias an owned output field')
        _mapping_values(target.data,mapping_attribute);values['map']=mapping_attribute
    controlled=_animated_paths(target)
    for key,value in values.items():
        parameter=_input(modifier,interface,key)
        if parameter.value!=value and parameter.path_from_id('value') in controlled:
            raise NormalError('Artistic normal parameter is animated/driven: '+key)
    for key,value in values.items():_input(modifier,interface,key).value=value
    target.update_tag()
    result=dict(profile=group[PROFILE_KEY],owner=group[OWNER],attributes=fields,strength=strength)
    if group[PROFILE_KEY]==MAPPED_PROFILE:result.update(mapping_attribute=mapping_attribute,mapping_space='REFERENCE_COVECTOR')
    return result


def _switch_mapping(target,modifier,mapping_attribute):
    import bpy
    old,interface,fields=_owned(target,modifier)
    keys=('reference','weight','strength')+(('map',) if 'map' in interface else ())
    original={key:_input(modifier,interface,key).value for key in keys}
    paths={_input(modifier,interface,key).path_from_id('value') for key in keys}
    prefix=modifier.path_from_id()+'.properties.inputs'
    if any(path in paths or path.startswith(prefix) for path in _animated_paths(target)):
        raise NormalError('Mode switching cannot rebuild native animated/driven modifier inputs')
    _preflight(target,original['reference'],original['weight'],original['strength'])
    if mapping_attribute is not None:
        if mapping_attribute in fields.values():raise NormalError('mapping_attribute cannot alias an owned output field')
        _mapping_values(target.data,mapping_attribute)
    new=None;old_profile=old[PROFILE_KEY]
    try:
        new,new_interface=(_build_mapped_group(old[OWNER]) if mapping_attribute is not None else _build_group(old[OWNER]))
        new[_TARGET],new[_UID]=target,modifier.persistent_uid
        modifier.node_group=new
        # Initialize the new instance with the very same captured reference;
        # public v2 configure never permits rebinding an established map.
        if mapping_attribute is not None:_input(modifier,new_interface,'reference').value=original['reference']
        kwargs=dict(reference=original['reference'],weight_attribute=original['weight'],strength=original['strength'])
        if mapping_attribute is not None:kwargs['mapping_attribute']=mapping_attribute
        configure(target,modifier,**kwargs)
        report=validate(target,modifier)
        if report['attributes']!=fields or old.users!=0:
            raise NormalError('Mode switch changed field identity or the old graph acquired external users')
        report.update(previous_profile=old_profile,mode_change='set_mapping' if mapping_attribute is not None else 'clear_mapping')
        bpy.data.node_groups.remove(old)
        return report
    except Exception as error:
        failures=[]
        try:
            modifier.node_group=old
            for key,value in original.items():_input(modifier,interface,key).value=value
            target.update_tag()
        except Exception as rollback_error:failures.append(str(rollback_error))
        if new is not None and new.users==0:
            try:bpy.data.node_groups.remove(new)
            except Exception as rollback_error:failures.append(str(rollback_error))
        if failures:raise NormalError('Mode switch failed; rollback also failed: '+'; '.join(failures)) from error
        raise


def set_mapping(target,modifier,mapping_attribute):
    """Switch to a static reference-covector map without replacing modifier/readers."""
    group,_,_=_owned(target,modifier)
    if group[PROFILE_KEY]==MAPPED_PROFILE:
        interface=json.loads(group[_INTERFACE])
        original={key:_input(modifier,interface,key).value for key in ('reference','weight','strength','map')}
        try:
            configure(target,modifier,mapping_attribute=mapping_attribute)
            return validate(target,modifier)
        except Exception as error:
            failures=[]
            for key,value in original.items():
                try:_input(modifier,interface,key).value=value
                except Exception as rollback_error:failures.append(key+':'+str(rollback_error))
            try:target.update_tag()
            except Exception as rollback_error:failures.append(str(rollback_error))
            if failures:raise NormalError('Mapping change failed; input rollback also failed: '+'; '.join(failures)) from error
            raise
    return _switch_mapping(target,modifier,mapping_attribute)


def clear_mapping(target,modifier):
    """Restore the exact v1 field builder; the author's mesh map is retained."""
    group,_,_=_owned(target,modifier)
    if group[PROFILE_KEY]==PROFILE:return inspect(target,modifier)
    return _switch_mapping(target,modifier,None)


def _shaders(group):
    import bpy
    return [value for value in bpy.data.node_groups if value.get(PROFILE_KEY)==_SHADER
            and value.get(OWNER)==group[OWNER] and value.get(_TARGET)==group[_TARGET]
            and value.get(_UID)==group[_UID]]


def _shader_edited(shader):
    try:
        return _signature(shader)!=shader.get(_SIGNATURE)
    except (TypeError,ValueError,ReferenceError,RuntimeError):
        # An author-editable reader can contain unserializable/nonfinite
        # settings. It remains an edited reader, never an excuse to delete it.
        return True


def inspect(target, modifier, *, depsgraph=None, evaluate=True):
    group,interface,fields=_owned(target,modifier)
    reference=_input(modifier,interface,'reference').value
    weight=_input(modifier,interface,'weight').value
    strength=_input(modifier,interface,'strength').value
    report=dict(profile=group[PROFILE_KEY],status='valid',space=SPACE,owner=group[OWNER],target=target.name,
        modifier_uid=modifier.persistent_uid,reference=reference.name if reference else None,
        weight_attribute=weight,strength=float(strength),attributes=fields,issues=[],
        shader_boundary='Shader readers are author-editable; material binding/link validation belongs to the caller',
        shader_groups=[dict(name=value.name,users=value.users,author_edited=_shader_edited(value)) for value in _shaders(group)])
    if group[PROFILE_KEY]==MAPPED_PROFILE:
        report.update(mapping_attribute=_input(modifier,interface,'map').value,mapping_space='REFERENCE_COVECTOR')
    try:
        _,graph=_preflight(target,reference,weight,strength,depsgraph)
        if group[PROFILE_KEY]==MAPPED_PROFILE:
            if report['mapping_attribute'] in fields.values():raise NormalError('mapping_attribute cannot alias an owned output field')
            _mapping_values(target.data,report['mapping_attribute'])
    except NormalError as error:
        report.update(status='invalid',issues=[str(error)]);return report
    if evaluate:
        evaluated=target.evaluated_get(graph);mesh=evaluated.to_mesh()
        try:
            _weights(mesh,weight)
            if group[PROFILE_KEY]==MAPPED_PROFILE:_mapping_values(mesh,report['mapping_attribute'])
            for key,kind in [('normal','FLOAT_VECTOR'),('mix','FLOAT'),('valid','BOOLEAN')]:
                attribute=mesh.attributes.get(fields[key])
                if attribute is None or attribute.domain!='POINT' or attribute.data_type!=kind:
                    raise NormalError('Artistic normal field missing or wrong domain/type: '+key)
            invalid=sum(not value.value for value in mesh.attributes[fields['valid']].data)
            for value in mesh.attributes[fields['normal']].data:
                if not all(math.isfinite(v) for v in value.vector) or abs(value.vector.length-1)>1e-4:
                    raise NormalError('Nonfinite or nonunit artistic world normal')
            if any(not math.isfinite(value.value) or not 0<=value.value<=1 for value in mesh.attributes[fields['mix']].data):
                raise NormalError('Invalid effective artistic normal strength')
            report.update(vertices=len(mesh.vertices),invalid_points=invalid,evaluation=graph.mode)
            if invalid:raise NormalError('Invalid artistic normal fields at %d points'%invalid)
        except NormalError as error:
            report.update(status='invalid',issues=[str(error)])
        finally:evaluated.to_mesh_clear()
    return report


def validate(target, modifier, *, depsgraph=None):
    report=inspect(target,modifier,depsgraph=depsgraph)
    if report['status']!='valid':raise NormalError('; '.join(report['issues']))
    return report


def shader_group(modifier):
    """Create an editable, unbound ShaderNodeTree with a world Normal output."""
    import bpy
    target=modifier.id_data;group,_,fields=_owned(target,modifier)
    shader=bpy.data.node_groups.new('Toon Artistic Normal Reader','ShaderNodeTree')
    try:
        shader[PROFILE_KEY],shader[OWNER],shader[_TARGET],shader[_UID]=_SHADER,group[OWNER],target,modifier.persistent_uid
        shader[_FIELDS]=json.dumps(fields,sort_keys=True);shader[_INTERFACE]='world-normal-output.v1'
        output_socket=shader.interface.new_socket(name='Normal',in_out='OUTPUT',socket_type='NodeSocketVector')
        def node(key,kind,**props):
            value=shader.nodes.new(kind);value[_NODE]=key
            for name,prop in props.items():setattr(value,name,prop)
            return value
        def link(source,dest):shader.links.new(source,dest)
        base=node('base','ShaderNodeNewGeometry')
        normal=node('world_attribute','ShaderNodeAttribute',attribute_type='GEOMETRY',attribute_name=fields['normal'])
        weight=node('mix_attribute','ShaderNodeAttribute',attribute_type='GEOMETRY',attribute_name=fields['mix'])
        inverse=node('one_minus_mix','ShaderNodeMath',operation='SUBTRACT')
        inverse.inputs[0].default_value=1.0;link(_socket(weight.outputs,'Fac'),inverse.inputs[1])
        base_scaled=node('base_scaled','ShaderNodeVectorMath',operation='SCALE')
        artist_scaled=node('artist_scaled','ShaderNodeVectorMath',operation='SCALE')
        link(_socket(base.outputs,'Normal'),_socket(base_scaled.inputs,'Vector'))
        link(_socket(inverse.outputs,'Value'),_socket(base_scaled.inputs,'Scale'))
        link(_socket(normal.outputs,'Vector'),_socket(artist_scaled.inputs,'Vector'))
        link(_socket(weight.outputs,'Fac'),_socket(artist_scaled.inputs,'Scale'))
        combined=node('blend','ShaderNodeVectorMath',operation='ADD')
        link(_socket(base_scaled.outputs,'Vector'),combined.inputs[0]);link(_socket(artist_scaled.outputs,'Vector'),combined.inputs[1])
        normalize=node('normalize','ShaderNodeVectorMath',operation='NORMALIZE')
        link(_socket(combined.outputs,'Vector'),_socket(normalize.inputs,'Vector'))
        length=node('blend_length','ShaderNodeVectorMath',operation='LENGTH')
        link(_socket(combined.outputs,'Vector'),_socket(length.inputs,'Vector'))
        valid=node('blend_nondegenerate','ShaderNodeMath',operation='GREATER_THAN')
        link(_socket(length.outputs,'Value'),valid.inputs[0]);valid.inputs[1].default_value=_EPSILON
        inverse_valid=node('blend_degenerate','ShaderNodeMath',operation='SUBTRACT')
        inverse_valid.inputs[0].default_value=1.0;link(_socket(valid.outputs,'Value'),inverse_valid.inputs[1])
        safe_art=node('safe_normalized_blend','ShaderNodeVectorMath',operation='SCALE')
        safe_base=node('safe_base_normal','ShaderNodeVectorMath',operation='SCALE')
        link(_socket(normalize.outputs,'Vector'),_socket(safe_art.inputs,'Vector'))
        link(_socket(valid.outputs,'Value'),_socket(safe_art.inputs,'Scale'))
        link(_socket(base.outputs,'Normal'),_socket(safe_base.inputs,'Vector'))
        link(_socket(inverse_valid.outputs,'Value'),_socket(safe_base.inputs,'Scale'))
        safe=node('safe_output','ShaderNodeVectorMath',operation='ADD')
        link(_socket(safe_art.outputs,'Vector'),safe.inputs[0]);link(_socket(safe_base.outputs,'Vector'),safe.inputs[1])
        output=node('output','NodeGroupOutput')
        link(_socket(safe.outputs,'Vector'),_socket(output.inputs,output_socket.identifier))
        for i,value in enumerate(shader.nodes):value.location=((i%5)*220,-(i//5)*220)
        shader[_SIGNATURE]=_signature(shader)
        return shader
    except Exception:
        bpy.data.node_groups.remove(shader);raise


def remove(target, modifier):
    """Remove only the owned unedited GN and unbound shader readers; protect users."""
    import bpy
    group,_,fields=_owned(target,modifier)
    shaders=_shaders(group)
    if any(_shader_edited(value) for value in shaders):
        raise NormalError('Artistic normal shader readers contain author edits; removal would discard authored work')
    if any(value.users for value in shaders):
        raise NormalError('Artistic normal shader readers have consumers; unbind them explicitly first')
    controlled=_animated_paths(target)
    _,interface,_=_owned(target,modifier)
    if any(_input(modifier,interface,key).path_from_id('value') in controlled for key in ('reference','weight','strength')+(('map',) if 'map' in interface else ())):
        raise NormalError('Removing the artistic normal modifier would leave native animation paths')
    owner=group[OWNER];profile=group[PROFILE_KEY]
    target.modifiers.remove(modifier)
    for value in shaders:bpy.data.node_groups.remove(value)
    if group.users==0:bpy.data.node_groups.remove(group)
    return dict(profile=profile,status='removed',owner=owner,attributes=fields)
