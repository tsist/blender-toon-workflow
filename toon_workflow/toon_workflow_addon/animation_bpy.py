# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed EEVEE region parameter animation on material wrapper NodeTrees.

This is a candidate native layer for manifest 0.8.  It deliberately sits
outside ``materials._local_static``: the existing static-material guard is not
changed or temporarily bypassed.  A versioned managed context may call
``validate_tree`` to establish that one NodeTree-owned Action is the only
animation attached to one managed region material.

The module imports ``bpy`` only inside functions.  It never starts Blender,
sets the scene frame, renders, saves a file, installs handlers, creates
drivers/NLA tracks, or changes the legacy static material policy.
"""
from copy import deepcopy
import hashlib
import json
import math
import uuid

from . import animation_contract, contract, materials, regions


SCHEMA = 'toon-shading-animation-v1'
SAMPLE_SCHEMA = 'toon-shading-animation-sample-v1'
ROLE = 'shading_action'
ACTION_ID = '_toon_action_id'
ACTION_SCHEMA = '_toon_action_schema'
TOLERANCE = 1e-6

__all__ = ('capture_scope', 'create', 'update', 'sample_scope', 'validate_tree')


def _error(code, message, path='$'):
    raise contract.WorkflowError(code, message, path)


def _invalid(message, path='$'):
    _error('INVALID_REQUEST', message, path)


def _conflict(message, path='$'):
    _error('CONFLICT', message, path)


def _unsupported(message, path='$'):
    _error('UNSUPPORTED', message, path)


def _uuid(value, path):
    if type(value) is not str:
        _invalid('Canonical nonzero lower-case UUID required', path)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _invalid('Canonical nonzero lower-case UUID required', path)
    if str(parsed) != value or parsed.int == 0:
        _invalid('Canonical nonzero lower-case UUID required', path)
    return value


def _native_uuid(value, path):
    try:
        return _uuid(value, path)
    except contract.WorkflowError:
        _conflict('Managed native UUID is missing or invalid', path)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _conflict('Animation witness is not canonical JSON: ' + str(exc))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _first_difference(left, right, path='$'):
    if type(left) is not type(right):
        return path + ':type'
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return path + ':keys'
        for key in left:
            found = _first_difference(left[key], right[key], path + '.' + str(key))
            if found:
                return found
        return None
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return path + ':length'
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_difference(a, b, path + '[' + str(index) + ']')
            if found:
                return found
        return None
    if left != right:
        return path + ':value'
    return None


def _plain(value, path='$'):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _conflict('Nonfinite value in native witness', path)
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item, path + '[' + str(index) + ']')
                for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {str(key): _plain(item, path + '.' + str(key))
                for key, item in value.items()}
    to_list = getattr(value, 'to_list', None)
    if callable(to_list):
        return _plain(to_list(), path)
    if hasattr(value, '__len__') and hasattr(value, '__getitem__'):
        try:
            return [_plain(value[index], path + '[' + str(index) + ']')
                    for index in range(len(value))]
        except (TypeError, IndexError):
            pass
    _unsupported('Unsupported non-RNA witness value', path)


def _action_name(material_id):
    return 'ToonShadingAction_' + material_id


def _stored_json(item, key, label):
    raw = item.get(key)
    if not isinstance(raw, str):
        _conflict(label + ' metadata is missing or not a string')
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        _conflict(label + ' metadata is not valid JSON')


def _local(item, label, path):
    if (item is None or getattr(item, 'library', None)
            or getattr(item, 'override_library', None)):
        _unsupported(label + ' must be local and non-override', path)


def _id_identity(item):
    return {
        'type': item.bl_rna.identifier,
        'name': item.name,
        'library': getattr(getattr(item, 'library', None), 'filepath', None),
    }


def _tracks_for(context, template_id):
    return deepcopy(context['tracks_by_template'].get(template_id, []))


def _manifest_context(manifest):
    if type(manifest) is not dict:
        _invalid('Manifest object required', '$')
    scope_id = _uuid(manifest.get('scope_id'), '$.scope_id')
    engine = manifest.get('engine')
    if engine != 'BLENDER_EEVEE':
        _unsupported('Managed region animation requires BLENDER_EEVEE', '$.engine')

    targets = manifest.get('targets')
    if type(targets) is not list or not targets:
        _invalid('Nonempty targets list required', '$.targets')
    for index, target in enumerate(targets):
        path = '$.targets[' + str(index) + ']'
        if type(target) is not dict:
            _invalid('Target object required', path)
        name = target.get('object_name')
        if type(name) is not str or not name.strip() or '\x00' in name:
            _invalid('Nonempty object_name required', path + '.object_name')
        _uuid(target.get('object_id'), path + '.object_id')
        _uuid(target.get('mesh_id'), path + '.mesh_id')
        if 'initialize_identity' in target and type(target['initialize_identity']) is not bool:
            _invalid('initialize_identity must be boolean',
                     path + '.initialize_identity')

    shading = manifest.get('shading')
    if not regions.is_region_mode(shading):
        _unsupported('Manifest shading.mode must be eevee_regions', '$.shading')
    shading = regions.normalize_shading(shading, targets, engine, [scope_id])

    time = manifest.get('time')
    contract.validate(time, contract.TIME_SCHEMA, '$.time')
    frame_start, frame_end = time['start'], time['end']

    if not contract.is_animated(manifest):
        _unsupported('Explicit manifest0.8 required', '$.schema_version')
    tracks = animation_contract.normalize_tracks(
        manifest.get('animations'), shading, frame_start, frame_end, engine)

    templates = {}
    for index, template in enumerate(shading['templates']):
        settings = materials._settings(template['settings'], engine)
        templates[template['template_id']] = {
            'index': index,
            'settings': settings,
            'active': tuple(materials._active(engine, settings['mode'])),
            'sockets': dict(materials._sockets(settings['mode'])),
        }
    tracks_by_template = {template_id: [] for template_id in templates}
    for track in tracks:
        tracks_by_template[track['template_id']].append(track)

    scene = manifest.get('scene')
    if scene is not None and (type(scene) is not str or not scene.strip()):
        _invalid('scene must be a nonempty string when present', '$.scene')
    schema_version = manifest.get('schema_version')
    return {
        'manifest': manifest,
        'scope_id': scope_id,
        'engine': engine,
        'targets': deepcopy(targets),
        'shading': shading,
        'time': deepcopy(time),
        'frame_start': frame_start,
        'frame_end': frame_end,
        'scene': scene,
        'schema_version': schema_version,
        'tracks': tracks,
        'tracks_by_template': tracks_by_template,
        'templates': templates,
    }


def _static_manifest_view(context):
    return {
        'schema_version': context['schema_version'],
        'scope_id': context['scope_id'],
        'scene': context['scene'],
        'engine': context['engine'],
        'targets': [{k:v for k,v in target.items() if k!='initialize_identity'} for target in context['targets']],
        'time': context['time'],
        'shading': {**context['shading'], 'templates':[{'template_id':t['template_id'],'settings':{'mode':t['settings']['mode']}} for t in context['shading']['templates']]},
    }


def _target_object(bpy, target):
    matches = [obj for obj in bpy.data.objects
               if obj.get('_toon_object_id') == target['object_id']]
    if len(matches) != 1:
        _conflict('Target object UUID must resolve to exactly one Object',
                  '$.targets')
    obj = matches[0]
    if obj.name != target['object_name']:
        _conflict('Target object name differs from Object UUID binding',
                  '$.targets')
    if obj.type != 'MESH' or obj.mode != 'OBJECT':
        _unsupported('Target must be an Object Mode Mesh', '$.targets')
    if (obj.library or obj.override_library
            or obj.data.library or obj.data.override_library):
        _unsupported('Target Object/Mesh must be local and non-override',
                     '$.targets')
    if obj.data.get('_toon_mesh_id') != target['mesh_id']:
        _conflict('Target Mesh UUID differs from manifest', '$.targets')
    if obj.data.users != 1:
        _unsupported('Target Mesh must be isolated before animation',
                     '$.targets')
    return obj


def _locate_bindings(context, deep=False):
    import bpy

    objects = {}
    for target in context['targets']:
        objects[target['object_id']] = _target_object(bpy, target)

    template_ids = set(context['templates'])
    rows = []
    material_ids = {}
    for index, assignment in enumerate(context['shading']['assignments']):
        path = '$.shading.assignments[' + str(index) + ']'
        obj = objects[assignment['object_id']]
        slot_index = assignment['slot_index']
        if slot_index >= len(obj.material_slots):
            _conflict('Assigned Material slot does not exist', path)
        slot = obj.material_slots[slot_index]
        material = slot.material
        if material is None:
            _conflict('Assigned region slot has no Material', path)
        _local(material, 'Region Material', path)
        if (material.get(materials.SCOPE) != context['scope_id']
                or material.get(materials.ROLE) != 'shading_material'):
            _conflict('Region Material scope/role changed', path)
        if material.get(regions.REGION) != assignment['region_id']:
            _conflict('Region Material UUID differs from assignment', path)
        if material.get(regions.TEMPLATE) != assignment['template_id']:
            _conflict('Region Material template UUID differs from assignment',
                      path)
        if assignment['template_id'] not in template_ids:
            _conflict('Assignment template is not declared', path)
        material_id = _native_uuid(material.get(materials.IDENTITY),
                                   path + '.material_id')
        if material_id in material_ids:
            _conflict('Two regions share one Material UUID', path)
        material_ids[material_id] = material
        if not getattr(material, 'use_nodes', False) or material.node_tree is None:
            _conflict('Region Material has no owned NodeTree', path)
        _local(material.node_tree, 'Region Material NodeTree', path)
        tree = material.node_tree
        wrappers = [node for node in tree.nodes
                    if node.get(materials.ROLE) == 'core_instance']
        outputs = [node for node in tree.nodes
                   if node.get(materials.ROLE) == 'managed_output']
        if len(wrappers) != 1 or len(outputs) != 1:
            _conflict('Exactly one managed wrapper and output are required',
                      path)
        wrapper, output = wrappers[0], outputs[0]
        if wrapper.bl_idname != 'ShaderNodeGroup':
            _conflict('Managed wrapper is not a ShaderNodeGroup', path)
        if wrapper.get(materials.NODE_ID) != 'core_instance':
            _conflict('Managed wrapper node identity changed', path)
        if output.bl_idname != 'ShaderNodeOutputMaterial':
            _conflict('Managed output node type changed', path)
        if output.get(materials.NODE_ID) != 'material_output':
            _conflict('Managed output node identity changed', path)
        core = wrapper.node_tree
        if core is None:
            _conflict('Managed wrapper has no core NodeTree', path)
        rows.append({
            'assignment': deepcopy(assignment),
            'object': obj,
            'slot': slot,
            'material': material,
            'material_id': material_id,
            'region_id': assignment['region_id'],
            'template_id': assignment['template_id'],
            'tree': tree,
            'wrapper': wrapper,
            'output': output,
            'core': core,
            'link': slot.link,
        })

    rows.sort(key=lambda row: row['material_id'])
    if len(rows) != len(material_ids):
        _conflict('Region material coverage is not one-to-one')
    managed = [material for material in bpy.data.materials
               if material.get(materials.SCOPE) == context['scope_id']
               and material.get(materials.ROLE) == 'shading_material']
    managed_ids = {material.get(materials.IDENTITY) for material in managed}
    if (len(managed) != len(managed_ids)
            or managed_ids != set(material_ids)
            or len(managed) != len(rows)):
        _conflict('Unexpected managed Material outside region assignments')

    if deep:
        user_map = bpy.data.user_map()
        for row in rows:
            row['static'] = _static_binding(row, context, user_map)
    return rows


def _material_consumers(bpy, material, path, user_map=None):
    actual_bindings = []
    for obj in bpy.data.objects:
        for index, slot in enumerate(getattr(obj, 'material_slots', ())):
            if slot.material == material:
                actual_bindings.append({
                    'object': obj.name,
                    'mesh': obj.data.name,
                    'slot': index,
                    'link': slot.link,
                })
    actual_bindings.sort(key=lambda row: (row['object'], row['slot']))
    allowed = set()
    for row in actual_bindings:
        obj = bpy.data.objects.get(row['object'])
        if obj is None:
            _conflict('Material consumer Object disappeared', path)
        allowed.add(obj.data if row['link'] == 'DATA' else obj)
    if user_map is None:
        user_map = bpy.data.user_map()
    incoming = set(user_map.get(material, ()))
    if incoming != allowed:
        _conflict('Material has a non-slot or external ID consumer', path)
    incoming_rows = sorted([_id_identity(item) for item in incoming],
                           key=lambda row: (row['type'], row['name'],
                                            row['library'] or ''))
    return actual_bindings, incoming_rows


def _core_structure(group, context, mode, path):
    try:
        return materials._core_structure(group, context['scope_id'], mode)
    except contract.WorkflowError as exc:
        code = ('UNSUPPORTED'
                if exc.code in ('unsupported_material_animation',
                                'shared_material_core', 'protected_material')
                else 'CONFLICT')
        _error(code, exc.message, path)


def _static_binding(row, context, user_map=None):
    import bpy

    if user_map is None:
        user_map = bpy.data.user_map()
    material, tree, wrapper = row['material'], row['tree'], row['wrapper']
    path = '$.bindings[' + row['material_id'] + ']'
    # Embedded ID.users is transient (new tree=1, disk reopen=0 in 5.2.1).
    # Prove the persistent owner relation directly instead of sealing that count.
    owners=[m for m in bpy.data.materials if m.node_tree==tree]
    if not getattr(tree,'is_embedded_data',False) or owners!=[material]:
        _conflict('Material NodeTree must be embedded with exactly its declared owner',path)
    if getattr(material, 'animation_data', None) is not None:
        _unsupported('Material datablock animation is not allowed', path)
    if getattr(row['core'], 'animation_data', None) is not None:
        _unsupported('Managed core NodeTree animation is not allowed', path)
    if wrapper.mute or row['output'].mute:
        _unsupported('Managed wrapper/output must not be muted', path)
    if (not row['output'].is_active_output
            or getattr(row['output'], 'target', None) != 'ALL'):
        _conflict('Managed material output activation changed', path)
    if wrapper.get(materials.SCOPE) != context['scope_id']:
        _conflict('Managed wrapper scope changed', path)
    if row['output'].get(materials.SCOPE) != context['scope_id']:
        _conflict('Managed output scope changed', path)
    if row['core'].get(materials.GROUP_ID) != material.get(materials.GROUP_ID):
        _conflict('Managed core reference changed', path)

    engine = material.get('_toon_engine')
    if engine != context['engine']:
        _conflict('Material engine metadata differs from manifest', path)
    declared = _stored_json(material, materials.DECLARED,
                            'Material declared shading')
    try:
        declared = materials._settings(declared, engine)
    except contract.WorkflowError as exc:
        _conflict('Material declared shading is invalid: ' + exc.message, path)
    template_settings = context['templates'][row['template_id']]['settings']
    if declared['mode'] != template_settings['mode']:
        _conflict('Material mode differs from its template', path)
    active = materials._active(engine, declared['mode'])
    sockets = materials._sockets(declared['mode'])

    inputs = []
    for field in active:
        socket = wrapper.inputs.get(sockets[field])
        if socket is None:
            _conflict('Managed active input is missing: ' + field, path)
        muted = bool(getattr(socket, 'mute', False))
        if socket.is_linked:
            _unsupported('Managed active input is linked: ' + field, path)
        if muted:
            _unsupported('Managed active input is muted: ' + field, path)
        inputs.append({
            'field': field,
            'socket_name': sockets[field],
            'identifier': socket.identifier,
            'linked': bool(socket.is_linked),
            'muted': muted,
        })

    for node in row['core'].nodes:
        if node.mute:
            _unsupported('Managed core Node is muted: ' + node.name, path)
    core_record = _core_structure(row['core'], context, declared['mode'], path)
    if core_record['engine'] != engine:
        _conflict('Managed core engine metadata differs from Material', path)
    try:
        wrapper_record = materials._wrapper_state(material, wrapper)
    except contract.WorkflowError as exc:
        _conflict('User wrapper witness failed: ' + exc.message, path)

    expected_binding = [{
        'object': row['object'].name,
        'mesh': row['object'].data.name,
        'slot': row['assignment']['slot_index'],
        'link': row['link'],
    }]
    stored_binding = _stored_json(material, materials.BINDINGS,
                                  'Material binding')
    if stored_binding != expected_binding:
        _conflict('Material binding metadata differs from actual slot', path)
    actual_bindings, incoming_rows = _material_consumers(
        bpy, material, path, user_map)
    if actual_bindings != expected_binding:
        _conflict('Region Material must have exactly its declared slot consumer',
                  path)

    static = {
        'material': {
            'id': row['material_id'],
            'name': material.name,
            'engine': engine,
            'declared_settings': declared,
            'library': getattr(getattr(material, 'library', None),
                               'filepath', None),
        },
        'object_binding': expected_binding[0],
        'object_id': row['assignment']['object_id'],
        'region_id': row['region_id'],
        'template_id': row['template_id'],
        'node_tree': {
            'name': tree.name,
            'library': getattr(getattr(tree, 'library', None), 'filepath', None),
            'is_embedded_data': True,
            'owner_material_id': row['material_id'],
            'owner_material_name': material.name,
        },
        'core': {
            'name': row['core'].name,
            'uuid': row['core'].get(materials.GROUP_ID),
            'users': int(row['core'].users),
            'structure': core_record,
            'animation_data': None,
        },
        'wrapper': {
            'name': wrapper.name,
            'node_id': wrapper.get(materials.NODE_ID),
            'core_name': row['core'].name,
            'state': {'policy':'user wrapper is recorded and preserved by the material layer'},
            'active_inputs': inputs,
        },
        'material_consumers': incoming_rows,
        'material_animation_data': None,
    }
    static['sha256'] = _digest(static)
    return static


def _rna(item, names):
    result = {}
    for name in names:
        if hasattr(item, name):
            value = getattr(item, name)
            if not callable(value):
                result[name] = _plain(value, '$.' + name)
    return result


def _handle_candidates(slot, animation_data, strip, bag):
    sources = {}
    for label, item, names in (
            ('ActionSlot', slot, ('handle', 'handle_int')),
            ('AnimData', animation_data, ('action_slot_handle',)),
            ('ActionStrip', strip, ('action_slot_handle',)),
            ('ActionChannelbag', bag, ('slot_handle', 'action_slot_handle'))):
        if item is None:
            continue
        for name in names:
            if hasattr(item, name):
                value = getattr(item, name)
                if value is not None:
                    sources[label + '.' + name] = value
    values = []
    for value in sources.values():
        if value not in values:
            values.append(value)
    if len(values) > 1:
        _conflict('Action slot handle RNA disagrees across structures')
    return (values[0] if values else None), sources


def _slot_record(slot, animation_data, strip, bag):
    identifier = getattr(slot, 'identifier', None)
    if type(identifier) is not str or not identifier:
        _conflict('Action slot identifier is missing')
    target = getattr(slot, 'target_id_type', None)
    if target != 'NODETREE':
        _conflict('Action slot target must be NODETREE')
    handle, sources = _handle_candidates(slot, animation_data, strip, bag)
    return {
        'identifier': identifier,
        'handle': handle,
        'handle_available': handle is not None,
        'handle_sources': sources,
        'target_id_type': target,
        'name_display': getattr(slot, 'name_display', None),
        'users': None if callable(getattr(slot, 'users', None)) else getattr(slot, 'users', None),
        'rna': _rna(slot, ('identifier', 'name_display',
                           'target_id_type', 'handle', 'handle_int', 'users')),
    }


def _vec3(value):
    return [float(value[0]), float(value[1])]


def _keyframe_record(point):
    record = {
        'co': _vec3(point.co),
        'interpolation': point.interpolation,
        'easing': getattr(point, 'easing', None),
        'handle_left': _vec3(point.handle_left),
        'handle_right': _vec3(point.handle_right),
        'handle_left_type': getattr(point, 'handle_left_type', None),
        'handle_right_type': getattr(point, 'handle_right_type', None),
        'back': getattr(point, 'back', None),
        'amplitude': getattr(point, 'amplitude', None),
        'period': getattr(point, 'period', None),
        'mute': getattr(point, 'mute', None),
    }
    return _plain(record)


def _fcurve_record(fcurve):
    modifiers = []
    for modifier in getattr(fcurve, 'modifiers', ()) or ():
        modifiers.append({
            'type': getattr(modifier, 'type', modifier.bl_rna.identifier),
            'rna': _rna(modifier, ('type', 'show_viewport', 'show_render')),
        })
    group = getattr(fcurve, 'group', None)
    record = {
        'data_path': fcurve.data_path,
        'array_index': int(fcurve.array_index),
        'group': group.name if group is not None else None,
        'mute': bool(fcurve.mute),
        'hide': bool(getattr(fcurve, 'hide', False)),
        'extrapolation': fcurve.extrapolation,
        'is_valid': getattr(fcurve, 'is_valid', None),
        'modifiers': modifiers,
        'keyframe_points': [_keyframe_record(point)
                            for point in fcurve.keyframe_points],
        'rna': _rna(fcurve, ('data_path', 'array_index', 'extrapolation',
                             'mute', 'hide', 'is_valid', 'auto_smoothing')),
    }
    return _plain(record)


def _group_record(group):
    return _plain({
        'name': group.name,
        'color_set': getattr(group, 'color_set', None),
        'is_custom': getattr(group, 'is_custom', None),
        'rna': _rna(group, ('name', 'color_set', 'is_custom')),
    })


def _action_structure(action):
    layers = list(action.layers)
    if len(layers) != 1:
        _unsupported('Owned Action must contain exactly one layer')
    strips = list(layers[0].strips)
    if len(strips) != 1:
        _unsupported('Owned Action layer must contain exactly one strip')
    strip = strips[0]
    bags = list(getattr(strip, 'channelbags', ()) or ())
    if len(bags) != 1:
        _unsupported('Owned Action strip must contain exactly one channelbag')
    return layers[0], strip, bags[0]


def _iter_fcurves(bag):
    return list(getattr(bag, 'fcurves', ()) or ())


def _layer_records(action):
    layer, strip, bag = _action_structure(action)
    bag_groups = list(getattr(bag, 'groups', ()) or ())
    curves = [_fcurve_record(fcurve) for fcurve in _iter_fcurves(bag)]
    curves.sort(key=lambda row: (row['data_path'], row['array_index']))
    bag_record = {
        'slot_handle': getattr(bag, 'slot_handle', None),
        'groups': [_group_record(group) for group in bag_groups],
        'curves': curves,
        'rna': _rna(bag, ('slot_handle',)),
    }
    strip_record = {
        'name': getattr(strip, 'name', None),
        'type': getattr(strip, 'type', None),
        'frame_start': getattr(strip, 'frame_start', None),
        'frame_end': getattr(strip, 'frame_end', None),
        'action_slot_handle': getattr(strip, 'action_slot_handle', None),
        'channelbags': [bag_record],
        'rna': _rna(strip, ('name', 'type', 'frame_start', 'frame_end',
                            'action_slot_handle')),
    }
    layer_record = {
        'name': layer.name,
        'strips': [strip_record],
        'rna': _rna(layer, ('name',)),
    }
    return (layer_record, strip_record, bag_record, curves,
            bag_record['groups'])


def _action_consumers(action, tree, user_map=None):
    import bpy
    if bool(action.use_fake_user):
        _unsupported('Owned Action must not use a fake user')
    if user_map is None:
        user_map = bpy.data.user_map()
    users = list(user_map.get(action, ()))
    if int(action.users) != 1 or len(users) != 1:
        _conflict('Owned Action must have exactly one native user')
    owners = [m for m in bpy.data.materials if m.node_tree == tree]
    if len(owners) != 1 or users[0] != owners[0] or tree.animation_data.action != action:
        _conflict('Owned embedded-tree Action must have its unique Material owner')
    return sorted([_id_identity(item) for item in users],
                  key=lambda row: (row['type'], row['name'], row['library'] or ''))


def _expected_curve_specs(tracks, wrapper, mode):
    sockets = materials._sockets(mode)
    specs = {}
    for index, track in enumerate(tracks):
        field = track['field']
        socket_name = sockets[field]
        socket = wrapper.inputs.get(socket_name)
        if socket is None:
            _conflict('Tracked wrapper input is missing: ' + field)
        if socket.is_linked:
            _unsupported('Tracked wrapper input is linked: ' + field)
        try:
            path = socket.path_from_id('default_value')
        except (AttributeError, TypeError):
            _unsupported('Actual socket path_from_id(default_value) is unavailable')
        if type(path) is not str or not path:
            _conflict('Socket default_value path is empty')
        channels = 4 if field.endswith('_color') else 1
        for channel in range(channels):
            key = (path, channel)
            if key in specs:
                _conflict('Two tracks resolve to one F-Curve path/index')
            points = []
            for point in track['keys']:
                value = point['value']
                scalar = value[channel] if channels == 4 else value
                points.append({
                    'frame': point['frame'],
                    'value': scalar,
                    'interpolation': track['interpolation'],
                })
            specs[key] = {
                'track_index': index,
                'field': field,
                'path': path,
                'channel': channel,
                'points': points,
                'interpolation': track['interpolation'],
            }
    return specs


def _validate_curves(action, specs, path):
    _, _, bag = _action_structure(action)
    groups = list(getattr(bag, 'groups', ()) or ())
    if groups:
        _unsupported('Owned Action must not contain Action groups', path)
    actual = {}
    flat = []
    for fcurve in _iter_fcurves(bag):
        key = (fcurve.data_path, int(fcurve.array_index))
        if key in actual:
            _conflict('Duplicate F-Curve path/index in owned Action', path)
        actual[key] = fcurve
        flat.append(_fcurve_record(fcurve))
    if set(actual) != set(specs):
        missing = sorted(set(specs) - set(actual))
        extra = sorted(set(actual) - set(specs))
        _conflict('Action curve set differs; missing=' + repr(missing) +
                  ' extra=' + repr(extra), path)

    for key, spec in specs.items():
        fcurve = actual[key]
        if fcurve.extrapolation != 'CONSTANT':
            _conflict('F-Curve extrapolation must be CONSTANT: ' + repr(key),
                      path)
        if bool(fcurve.mute):
            _unsupported('F-Curve must not be muted: ' + repr(key), path)
        if len(getattr(fcurve, 'modifiers', ()) or ()):
            _unsupported('F-Curve modifiers are not supported: ' + repr(key),
                         path)
        if getattr(fcurve, 'group', None) is not None:
            _unsupported('F-Curve groups are not supported: ' + repr(key), path)
        points = list(fcurve.keyframe_points)
        if len(points) != len(spec['points']):
            _conflict('F-Curve key count differs: ' + repr(key), path)
        for index, (point, expected) in enumerate(zip(points, spec['points'])):
            if float(point.co.x) != float(expected['frame']):
                _conflict('F-Curve key frame differs at ' + repr(key) +
                          '[' + str(index) + ']', path)
            if not materials._close(float(point.co.y), expected['value']):
                _conflict('F-Curve key value differs at ' + repr(key) +
                          '[' + str(index) + ']', path)
            if point.interpolation != spec['interpolation']:
                _conflict('F-Curve interpolation differs at ' + repr(key) +
                          '[' + str(index) + ']', path)
            if getattr(point, 'mute', False):
                _unsupported('Keyframe must not be muted at ' + repr(key),
                             path)
    flat.sort(key=lambda row: (row['data_path'], row['array_index']))
    return flat


def _validate_action(action, row, context, tracks, user_map=None):
    path = '$.bindings[' + row['material_id'] + '].action'
    if action is None:
        _conflict('Action is required for a nonempty track set', path)
    _local(action, 'Owned Action', path)
    if action.get(materials.SCOPE) != context['scope_id']:
        _conflict('Action scope tag differs from manifest', path)
    if action.get(materials.ROLE) != ROLE:
        _conflict('Action role tag must be ' + ROLE, path)
    if action.get(materials.IDENTITY) != row['material_id']:
        _conflict('Action Material UUID tag differs from binding', path)
    if action.get(ACTION_SCHEMA) != SCHEMA:
        _conflict('Action animation schema tag differs', path)
    action_id = _native_uuid(action.get(ACTION_ID), path + '.uuid')
    if action.name != _action_name(row['material_id']):
        _conflict('Action name is not the deterministic Material-based name',
                  path)
    if bool(action.use_fake_user):
        _unsupported('Owned Action must not use a fake user', path)
    if getattr(action, 'use_frame_range', False):
        _unsupported('Action manual frame range is not supported', path)
    if getattr(action, 'use_cyclic', False):
        _unsupported('Cyclic Action is not supported', path)
    animation_data = row['tree'].animation_data
    if animation_data is None or animation_data.action != action:
        _conflict('NodeTree animation_data does not reference the owned Action',
                  path)
    if len(getattr(animation_data, 'drivers', ()) or ()):
        _unsupported('NodeTree drivers are not supported', path)
    if len(getattr(animation_data, 'nla_tracks', ()) or ()):
        _unsupported('NLA tracks are not supported', path)
    if getattr(animation_data, 'use_tweak_mode', False):
        _unsupported('NLA tweak mode is not supported', path)
    extrapolation = getattr(animation_data, 'action_extrapolation', None)
    if extrapolation is not None and extrapolation != 'HOLD':
        _unsupported('AnimData action extrapolation must be HOLD', path)
    blend_type = getattr(animation_data, 'action_blend_type', None)
    if blend_type is not None and blend_type != 'REPLACE':
        _unsupported('AnimData action blend type must be REPLACE', path)
    influence = getattr(animation_data, 'action_influence', None)
    if influence is not None and not materials._close(float(influence), 1.0):
        _unsupported('AnimData action influence must be 1.0', path)

    slots = list(action.slots)
    if len(slots) != 1:
        _unsupported('Owned Action must contain exactly one slot', path)
    _, strip, bag = _action_structure(action)
    for label, item in (('Action', action), ('AnimData', animation_data),
                        ('ActionStrip', strip), ('ActionChannelbag', bag)):
        modifiers = getattr(item, 'modifiers', None)
        if modifiers is not None and len(modifiers):
            _unsupported(label + ' modifiers are not supported', path)
    slot = slots[0]
    if hasattr(animation_data, 'action_slot'):
        selected = getattr(animation_data, 'action_slot', None)
        if selected is None:
            _conflict('NodeTree animation_data has no selected Action slot', path)
        if selected != slot:
            _conflict('NodeTree selected Action slot differs from owned slot', path)
    slot_record = _slot_record(slot, animation_data, strip, bag)
    if slot_record['target_id_type'] != 'NODETREE':
        _conflict('Action slot must target NODETREE', path)
    if slot_record['handle'] is None:
        # Some ActionSlot RNA builds expose the handle only through AnimData.
        # The full probe record below makes that uncertainty explicit.
        pass

    mode = context['templates'][row['template_id']]['settings']['mode']
    specs = _expected_curve_specs(tracks, row['wrapper'], mode)
    curves = _validate_curves(action, specs, path)
    consumers = _action_consumers(action, row['tree'], user_map)
    layer_record, strip_record, bag_record, curve_records, group_records = \
        _layer_records(action)
    if curve_records != curves:
        _conflict('Action curve witness is not stable within one capture', path)

    record = {
        'name': action.name,
        'uuid': action_id,
        'schema': action.get(ACTION_SCHEMA),
        'library': getattr(getattr(action, 'library', None),
                           'filepath', None),
        'tags': {
            'scope_id': action.get(materials.SCOPE),
            'role': action.get(materials.ROLE),
            'material_id': action.get(materials.IDENTITY),
        },
        'users': int(action.users),
        'use_fake_user': bool(action.use_fake_user),
        'use_frame_range': bool(getattr(action, 'use_frame_range', False)),
        'use_cyclic': bool(getattr(action, 'use_cyclic', False)),
        'frame_range': _plain(list(action.frame_range)),
        'rna': _rna(action, ('name', 'users', 'use_fake_user',
                             'use_frame_range', 'frame_start', 'frame_end',
                             'use_cyclic', 'frame_range', 'is_action_layered',
                             'id_root')),
        'slot_count': len(slots),
        'slots': [slot_record],
        'slot': slot_record,
        'layers': [layer_record],
        'channelbag': bag_record,
        'groups': group_records,
        'curves': curve_records,
        'curve_count': len(curve_records),
        'consumers': consumers,
        'node_tree': {
            'name': row['tree'].name,
            'library': getattr(getattr(row['tree'], 'library', None),
                               'filepath', None),
        },
        'animation_data': {
            'exists': True,
            'action': action.name,
            'action_slot': slot.identifier,
            'action_slot_handle': getattr(animation_data,
                                          'action_slot_handle', None),
            'action_extrapolation': getattr(animation_data,
                                            'action_extrapolation', None),
            'action_blend_type': getattr(animation_data,
                                         'action_blend_type', None),
            'action_influence': getattr(animation_data,
                                        'action_influence', None),
            'drivers': len(getattr(animation_data, 'drivers', ()) or ()),
            'nla_tracks': len(getattr(animation_data, 'nla_tracks', ()) or ()),
            'use_nla': getattr(animation_data, 'use_nla', None),
            'rna': _rna(animation_data,
                        ('action_slot_handle', 'action_extrapolation',
                         'action_blend_type', 'action_influence',
                         'use_nla', 'use_tweak_mode')),
        },
        # The complete curve record above includes layers, strips,
        # channelbags, groups, per-curve RNA, all key points, interpolation,
        # handles and modifiers.  It is deliberately more than a point list.
    }
    record['sha256'] = _digest(record)
    return record


def _validate_no_animation(row, path):
    if getattr(row['tree'], 'animation_data', None) is not None:
        _conflict('NodeTree must have no animation_data when tracks are empty',
                  path)
    if getattr(row['material'], 'animation_data', None) is not None:
        _unsupported('Material datablock animation is not allowed', path)
    if getattr(row['core'], 'animation_data', None) is not None:
        _unsupported('Managed core NodeTree animation is not allowed', path)


def _validate_binding_animation(row, context, user_map=None):
    tracks = _tracks_for(context, row['template_id'])
    path = '$.bindings[' + row['material_id'] + ']'
    if not tracks:
        _validate_no_animation(row, path)
        return None
    animation_data = row['tree'].animation_data
    if animation_data is None or animation_data.action is None:
        _conflict('NodeTree animation_data/Action is missing', path)
    return _validate_action(animation_data.action, row, context, tracks,
                            user_map=user_map)


def _managed_actions(scope_id):
    import bpy
    return [action for action in bpy.data.actions
            if action.get(materials.SCOPE) == scope_id
            and action.get(materials.ROLE) == ROLE]


def _validate_action_uuid_uniqueness(actions):
    seen = {}
    for action in actions:
        raw = action.get(ACTION_ID)
        if raw is None:
            continue
        parsed = _native_uuid(raw, '$.actions')
        if parsed in seen and seen[parsed] != action:
            _conflict('Duplicate Action UUID')
        seen[parsed] = action


def capture_scope(manifest):
    """Validate and return the complete native animation witness for a scope."""
    import bpy

    context = _manifest_context(manifest)
    rows = _locate_bindings(context, deep=True)
    user_map = bpy.data.user_map()
    bindings = []
    expected_actions = set()
    animated_regions = 0
    for row in rows:
        tracks = _tracks_for(context, row['template_id'])
        action_record = _validate_binding_animation(row, context, user_map)
        if action_record is not None:
            animated_regions += 1
            action = row['tree'].animation_data.action
            expected_actions.add(action)
        binding = {
            'material_id': row['material_id'],
            'material_name': row['material'].name,
            'region_id': row['region_id'],
            'template_id': row['template_id'],
            'object_id': row['assignment']['object_id'],
            'slot_index': row['assignment']['slot_index'],
            'object_name': row['object'].name,
            'mesh_name': row['object'].data.name,
            'link': row['link'],
            'node_tree_name': row['tree'].name,
            'action_uuid': (action_record['uuid']
                            if action_record is not None else None),
            'slot': (deepcopy(action_record['slot'])
                     if action_record is not None else None),
            'tracks': tracks,
            'action': action_record,
            'curves': (deepcopy(action_record['curves'])
                       if action_record is not None else []),
            'static': row['static'],
        }
        binding['sha256'] = _digest(binding)
        bindings.append(binding)

    managed = _managed_actions(context['scope_id'])
    if len(expected_actions) != animated_regions:
        _conflict('Animated regions do not own distinct Action objects')
    if set(managed) != expected_actions:
        _conflict('Managed Action set differs from animated region set')
    if len({action.name for action in managed}) != len(managed):
        _conflict('Managed Action names are not unique')
    _validate_action_uuid_uniqueness(managed)
    return {
        'schema': SCHEMA,
        'scope_id': context['scope_id'],
        'frame_start': context['frame_start'],
        'frame_end': context['frame_end'],
        'tracks': deepcopy(context['tracks']),
        'bindings': bindings,
    }


def _new_action_uuid():
    import bpy
    used = set()
    for action in bpy.data.actions:
        raw = action.get(ACTION_ID)
        if isinstance(raw, str):
            used.add(raw)
    while True:
        candidate = str(uuid.uuid4())
        if candidate not in used:
            return candidate


def _clear_keyframe_points(fcurve):
    points = fcurve.keyframe_points
    clear = getattr(points, 'clear', None)
    if callable(clear):
        clear()
        return
    while len(points):
        points.remove(points[-1])


def _capture_points(fcurve):
    rows = []
    for point in fcurve.keyframe_points:
        row = {
            'co': [float(point.co.x), float(point.co.y)],
            'interpolation': point.interpolation,
            'easing': getattr(point, 'easing', None),
            'handle_left': [float(point.handle_left.x),
                            float(point.handle_left.y)],
            'handle_right': [float(point.handle_right.x),
                             float(point.handle_right.y)],
            'handle_left_type': getattr(point, 'handle_left_type', None),
            'handle_right_type': getattr(point, 'handle_right_type', None),
            'back': getattr(point, 'back', None),
            'amplitude': getattr(point, 'amplitude', None),
            'period': getattr(point, 'period', None),
            'mute': getattr(point, 'mute', None),
        }
        rows.append(row)
    return rows


def _write_points(fcurve, rows):
    _clear_keyframe_points(fcurve)
    for row in rows:
        point = fcurve.keyframe_points.insert(
            float(row['co'][0]), float(row['co'][1]))
        for name in ('handle_left_type', 'handle_right_type'):
            value = row.get(name)
            if value is not None and hasattr(point, name):
                setattr(point, name, value)
        point.co = (float(row['co'][0]), float(row['co'][1]))
        point.interpolation = row['interpolation']
        for name in ('easing', 'back', 'amplitude', 'period', 'mute'):
            value = row.get(name)
            if value is not None and hasattr(point, name):
                setattr(point, name, value)
        if row.get('handle_left') is not None:
            point.handle_left = tuple(row['handle_left'])
        if row.get('handle_right') is not None:
            point.handle_right = tuple(row['handle_right'])
    fcurve.extrapolation = 'CONSTANT'
    fcurve.update()


def _drop_action_groups(action, path):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', ()) or ():
                groups = list(getattr(bag, 'groups', ()) or ())
                if not groups:
                    continue
                for fcurve in _iter_fcurves(bag):
                    if getattr(fcurve, 'group', None) is not None:
                        try:
                            fcurve.group = None
                        except (AttributeError, TypeError):
                            pass
                for group in groups:
                    remove = getattr(getattr(bag, 'groups', None), 'remove', None)
                    if not callable(remove):
                        _unsupported(
                            'Action groups exist and this Blender build exposes '
                            'no group removal RNA; probe is required', path)
                    try:
                        remove(group)
                    except (RuntimeError, TypeError) as exc:
                        _unsupported(
                            'Unable to remove auto-created Action group: ' +
                            str(exc), path)
                if list(getattr(bag, 'groups', ()) or ()):
                    _unsupported('Action groups remain after cleanup', path)


def _configure_created_action(action, row, tracks, path):
    slot = list(action.slots)
    if len(slot) != 1:
        _unsupported('keyframe_insert did not create exactly one Action slot',
                     path)
    animation_data = row['tree'].animation_data
    selected = getattr(animation_data, 'action_slot', None)
    if selected is None and hasattr(animation_data, 'action_slot'):
        animation_data.action_slot = slot[0]
    slot = list(action.slots)
    if len(slot) != 1:
        _unsupported('Action slot changed during creation', path)
    if hasattr(animation_data, 'action_slot'):
        if getattr(animation_data, 'action_slot', None) != slot[0]:
            _conflict('Created Action slot is not selected by NodeTree', path)

    mode = _stored_json(row['material'], materials.DECLARED,
                        'Material declared shading')['mode']
    specs = _expected_curve_specs(tracks, row['wrapper'], mode)
    _, strip, bag = _action_structure(action)
    fcurves = _iter_fcurves(bag)
    actual = {(fcurve.data_path, int(fcurve.array_index)): fcurve
              for fcurve in fcurves}
    if set(actual) != set(specs):
        _conflict('keyframe_insert created an unexpected F-Curve set', path)
    for key, spec in specs.items():
        fcurve = actual[key]
        fcurve.extrapolation = 'CONSTANT'
        fcurve.mute = False
        points = list(fcurve.keyframe_points)
        if len(points) != len(spec['points']):
            _conflict('keyframe_insert produced an unexpected key count', path)
        for point, expected in zip(points, spec['points']):
            if float(point.co.x) != float(expected['frame']):
                _conflict('keyframe_insert produced an unexpected frame', path)
            if not materials._close(float(point.co.y), expected['value']):
                _conflict('keyframe_insert readback differs', path)
            point.interpolation = spec['interpolation']
    _drop_action_groups(action, path)
    for fcurve in _iter_fcurves(bag):
        fcurve.update()


def _rollback_created(trees, previous_actions):
    import bpy
    for tree in trees:
        try:
            if getattr(tree, 'animation_data', None) is not None:
                tree.animation_data_clear()
        except (AttributeError, RuntimeError):
            pass
    created = [action for action in bpy.data.actions
               if action not in previous_actions]
    for action in reversed(created):
        try:
            if action.users == 0:
                bpy.data.actions.remove(action)
        except (AttributeError, RuntimeError):
            pass


def create(manifest):
    """Create one owned Action per animated region on new region materials."""
    import bpy

    context = _manifest_context(manifest)
    rows = _locate_bindings(context, deep=True)
    path = '$.animations.tracks'
    for row in rows:
        if getattr(row['tree'], 'animation_data', None) is not None:
            _unsupported('New region NodeTree already has animation_data', path)
        if getattr(row['material'], 'animation_data', None) is not None:
            _unsupported('Material datablock already has animation_data', path)
        if getattr(row['core'], 'animation_data', None) is not None:
            _unsupported('Managed core has animation_data', path)
    existing_managed = _managed_actions(context['scope_id'])
    if existing_managed:
        _conflict('Scope already contains managed shading Actions; use update',
                  path)
    for row in rows:
        if not _tracks_for(context, row['template_id']):
            continue
        name = _action_name(row['material_id'])
        if bpy.data.actions.get(name) is not None:
            _conflict('Deterministic Action name already exists: ' + name, path)

    previous_actions = set(bpy.data.actions)
    touched_trees = []
    created = []
    try:
        for row in rows:
            tracks = _tracks_for(context, row['template_id'])
            if not tracks:
                continue
            tree = row['tree']
            touched_trees.append(tree)
            for track in tracks:
                field = track['field']
                socket_name = materials._sockets(
                    context['templates'][row['template_id']]['settings']['mode']
                )[field]
                socket = row['wrapper'].inputs.get(socket_name)
                if socket is None:
                    _conflict('Tracked wrapper input is missing: ' + field, path)
                for point in track['keys']:
                    value = point['value']
                    socket.default_value = (list(value)
                                            if field.endswith('_color')
                                            else float(value))
                    inserted = socket.keyframe_insert(
                        data_path='default_value',
                        frame=int(point['frame']),
                        group='')
                    if inserted is False:
                        _unsupported('keyframe_insert failed for ' + field, path)
            action = tree.animation_data.action
            if action is None:
                _conflict('keyframe_insert did not create an Action', path)
            created.append(action)
            action.name = _action_name(row['material_id'])
            if action.name != _action_name(row['material_id']):
                _conflict('Blender changed the deterministic Action name', path)
            action[materials.SCOPE] = context['scope_id']
            action[materials.ROLE] = ROLE
            action[materials.IDENTITY] = row['material_id']
            action[ACTION_ID] = _new_action_uuid()
            action[ACTION_SCHEMA] = SCHEMA
            action.use_fake_user = False
            _configure_created_action(action, row, tracks,
                                      '$.bindings[' + row['material_id'] + ']')
        captured = capture_scope(manifest)
    except Exception:
        _rollback_created(touched_trees, previous_actions)
        raise
    return {
        'schema': SCHEMA,
        'scope_id': context['scope_id'],
        'changed': bool(created),
        'created_actions': [{
            'material_id': action.get(materials.IDENTITY),
            'name': action.name,
            'uuid': action.get(ACTION_ID),
        } for action in created],
        'capture': captured,
    }


def _curve_map(action):
    _, _, bag = _action_structure(action)
    result = {}
    for fcurve in _iter_fcurves(bag):
        result[(fcurve.data_path, int(fcurve.array_index))] = fcurve
    return result


def _new_points_for_spec(spec):
    return [{
        'co': [float(point['frame']), float(point['value'])],
        'interpolation': spec['interpolation'],
    } for point in spec['points']]


def _points_same(left, right):
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if float(a['co'][0]) != float(b['co'][0]):
            return False
        if not materials._close(float(a['co'][1]), float(b['co'][1])):
            return False
        if a['interpolation'] != b['interpolation']:
            return False
    return True


def _action_shell(record):
    if record is None:
        return None
    layers = []
    for layer in record['layers']:
        strips = []
        for strip in layer['strips']:
            bags = []
            for bag in strip['channelbags']:
                bags.append({
                    'slot_handle': bag['slot_handle'],
                    'groups': deepcopy(bag['groups']),
                    'curves': [{
                        key: deepcopy(value)
                        for key, value in curve.items()
                        if key != 'keyframe_points'
                    } for curve in bag['curves']],
                    'rna': deepcopy(bag['rna']),
                })
            strips.append({
                'name': strip['name'],
                'type': strip['type'],
                'action_slot_handle': strip['action_slot_handle'],
                'channelbags': bags,
                'rna': {key: deepcopy(value)
                        for key, value in strip['rna'].items()
                        if key not in ('frame_start', 'frame_end')},
            })
        layers.append({
            'name': layer['name'],
            'strips': strips,
            'rna': deepcopy(layer['rna']),
        })
    rna = {key: deepcopy(value) for key, value in record['rna'].items()
           if key not in ('frame_range', 'frame_start', 'frame_end')}
    return {
        'name': record['name'],
        'uuid': record['uuid'],
        'schema': record['schema'],
        'tags': deepcopy(record['tags']),
        'users': record['users'],
        'use_fake_user': record['use_fake_user'],
        'use_frame_range': record['use_frame_range'],
        'use_cyclic': record['use_cyclic'],
        'slot_count': record['slot_count'],
        'slots': deepcopy(record['slots']),
        'layers': layers,
        'consumers': deepcopy(record['consumers']),
        'node_tree': deepcopy(record['node_tree']),
        'animation_data': deepcopy(record['animation_data']),
        'rna': rna,
    }


def _verify_update_only_changed_keys(before, after):
    before_map = {binding['material_id']: binding
                  for binding in before['bindings']}
    after_map = {binding['material_id']: binding
                 for binding in after['bindings']}
    if set(before_map) != set(after_map):
        _conflict('Material identity set changed during animation update')
    for material_id in sorted(before_map):
        old, new = before_map[material_id], after_map[material_id]
        if old['static'] != new['static']:
            _conflict('Static material/tree structure changed during animation '
                      'update', '$.bindings[' + material_id + ']')
        if old['region_id'] != new['region_id']:
            _conflict('Region UUID changed during animation update')
        if old['template_id'] != new['template_id']:
            _conflict('Template UUID changed during animation update')
        if old['node_tree_name'] != new['node_tree_name']:
            _conflict('NodeTree name changed during animation update')
        if _action_shell(old['action']) != _action_shell(new['action']):
            _conflict('Action shell changed beyond keys/interpolation',
                      '$.bindings[' + material_id + ']')
    return True


def update(manifest, previous_manifest, baseline_animation):
    """Update only declared keys/interpolation while retaining owned Actions."""
    before = capture_scope(previous_manifest)
    if _canonical(before) != _canonical(baseline_animation):
        difference = _first_difference(baseline_animation, before,
                                       '$.baseline_animation')
        _conflict('Baseline animation is not an exact capture_scope result: ' +
                  str(difference), '$.baseline_animation')

    previous_context = _manifest_context(previous_manifest)
    context = _manifest_context(manifest)
    if _canonical(_static_manifest_view(previous_context)) != \
            _canonical(_static_manifest_view(context)):
        _conflict('Animation update cannot change static manifest sections',
                  '$.animations')
    animation_contract.validate_update_selectors(
        previous_context['tracks'], context['tracks'])

    rows = _locate_bindings(previous_context, deep=False)
    rows_by_material = {row['material_id']: row for row in rows}
    before_map = {binding['material_id']: binding
                  for binding in before['bindings']}

    plans = []
    updated_tracks = set()
    for material_id in sorted(before_map):
        binding = before_map[material_id]
        row = rows_by_material.get(material_id)
        if row is None:
            _conflict('Animated region Material disappeared before update')
        old_tracks = _tracks_for(previous_context, binding['template_id'])
        new_tracks = _tracks_for(context, binding['template_id'])
        if _canonical(old_tracks) == _canonical(new_tracks):
            continue
        if row['tree'].animation_data is None or \
                row['tree'].animation_data.action is None:
            _conflict('Owned Action disappeared before update',
                      '$.bindings[' + material_id + ']')
        action = row['tree'].animation_data.action
        mode = previous_context['templates'][binding['template_id']]['settings']['mode']
        old_specs = _expected_curve_specs(old_tracks, row['wrapper'], mode)
        new_specs = _expected_curve_specs(new_tracks, row['wrapper'], mode)
        if set(old_specs) != set(new_specs):
            _unsupported('Selectors changed F-Curve paths during update')
        curves = _curve_map(action)
        if set(curves) != set(new_specs):
            _conflict('Current Action curve set differs before update',
                      '$.bindings[' + material_id + ']')
        for key in sorted(new_specs):
            spec = new_specs[key]
            old_spec = old_specs[key]
            fcurve = curves[key]
            old_points = _capture_points(fcurve)
            old_expected = [{
                'co': [float(point['frame']), float(point['value'])],
                'interpolation': old_spec['interpolation'],
            } for point in old_spec['points']]
            if not _points_same(old_points, old_expected):
                _conflict('Current Action keys differ from previous manifest',
                          '$.bindings[' + material_id + ']')
            new_points = _new_points_for_spec(spec)
            if _points_same(old_points, new_points):
                continue
            only_interpolation = (
                len(old_points) == len(new_points)
                and all(float(a['co'][0]) == float(b['co'][0])
                        and materials._close(float(a['co'][1]),
                                             float(b['co'][1]))
                        for a, b in zip(old_points, new_points)))
            plans.append({
                'material_id': material_id,
                'path': key[0],
                'index': key[1],
                'fcurve': fcurve,
                'old_points': old_points,
                'new_points': new_points,
                'only_interpolation': only_interpolation,
            })
        for old_track, new_track in zip(old_tracks, new_tracks):
            if _canonical(old_track) != _canonical(new_track):
                updated_tracks.add((binding['template_id'],
                                    old_track['field']))

    applied = []
    try:
        for plan in plans:
            applied.append(plan)
            if plan['only_interpolation']:
                points = list(plan['fcurve'].keyframe_points)
                if len(points) != len(plan['new_points']):
                    _conflict('Interpolation-only update key count changed')
                for point, expected in zip(points, plan['new_points']):
                    point.interpolation = expected['interpolation']
                plan['fcurve'].update()
            else:
                _write_points(plan['fcurve'], plan['new_points'])
        after = capture_scope(manifest)
        _verify_update_only_changed_keys(before, after)
    except Exception:
        try:
            for plan in reversed(applied):
                _write_points(plan['fcurve'], plan['old_points'])
        except Exception as rollback_error:
            raise contract.WorkflowError(
                'VALIDATION_FAILED',
                'Animation update failed and Action point rollback also '
                'failed: ' + str(rollback_error),
                '$.animations.tracks') from rollback_error
        raise

    return {
        'schema': SCHEMA,
        'scope_id': context['scope_id'],
        'changed': bool(plans),
        'updated_track_count': len(updated_tracks),
        'updated_curve_count': len(plans),
        'selectors': [[track['template_id'], track['field']]
                      for track in context['tracks']],
        'previous_capture_sha256': _digest(before),
        'capture': after,
    }


def _frame_number(value):
    if type(value) not in (int, float):
        _invalid('Finite int/float frame required', '$.frame')
    if type(value) is float and not math.isfinite(value):
        _invalid('Finite int/float frame required', '$.frame')
    try:
        converted = float(value)
    except OverflowError:
        _invalid('Finite int/float frame required', '$.frame')
    if not math.isfinite(converted):
        _invalid('Finite int/float frame required', '$.frame')
    return converted


def _scene_time(scene):
    frame_float = getattr(scene, 'frame_float', None)
    if frame_float is not None:
        return float(frame_float)
    return float(scene.frame_current) + float(getattr(scene, 'frame_subframe', 0.0))


def _read_socket(socket, field, path):
    value = socket.default_value
    if field.endswith('_color'):
        if not hasattr(value, '__len__') or len(value) != 4:
            _conflict('Animated color input is not RGBA', path)
        result = [float(channel) for channel in value]
    else:
        result = float(value)
    values = result if isinstance(result, list) else [result]
    if any(not math.isfinite(item) for item in values):
        _conflict('Animated input is nonfinite', path)
    return result


def _action_identity_only(row, context, tracks, path):
    if not tracks:
        if getattr(row['tree'], 'animation_data', None) is not None:
            _conflict('Static region NodeTree gained animation_data', path)
        return None
    animation_data = getattr(row['tree'], 'animation_data', None)
    if animation_data is None or animation_data.action is None:
        _conflict('Animated region Action is missing', path)
    action = animation_data.action
    if action.get(materials.SCOPE) != context['scope_id'] or \
            action.get(materials.ROLE) != ROLE or \
            action.get(materials.IDENTITY) != row['material_id'] or \
            action.get(ACTION_SCHEMA) != SCHEMA:
        _conflict('Animated region Action ownership tags differ', path)
    _native_uuid(action.get(ACTION_ID), path + '.uuid')
    if action.name != _action_name(row['material_id']):
        _conflict('Animated region Action name differs', path)
    if len(list(action.slots)) != 1:
        _conflict('Animated region Action slot count differs', path)
    return {
        'name': action.name,
        'uuid': action.get(ACTION_ID),
        'slot': getattr(action.slots[0], 'identifier', None),
    }


def sample_scope(manifest, frame):
    """Read actual tracked inputs at an already-set scene frame and verify them."""
    import bpy

    context = _manifest_context(manifest)
    moment = _frame_number(frame)
    if context['scene'] is None:
        scene = bpy.context.scene
    else:
        scene = bpy.data.scenes.get(context['scene'])
        if scene is None:
            _conflict('Manifest scene does not exist', '$.scene')
    if scene is None:
        _conflict('An active scene is required for sample_scope', '$.scene')
    actual_scene_frame = _scene_time(scene)
    if abs(actual_scene_frame - moment) > 1e-9:
        _conflict('Root must scene.frame_set before sample_scope; scene=%.12g '
                  'requested=%.12g' % (actual_scene_frame, moment),
                  '$.frame')

    rows = _locate_bindings(context, deep=False)
    evidence_rows = []
    settings_rows = []
    failed = []
    for row in rows:
        tracks = _tracks_for(context, row['template_id'])
        path = '$.bindings[' + row['material_id'] + ']'
        action_identity = _action_identity_only(row, context, tracks, path)
        declared = _stored_json(row['material'], materials.DECLARED,
                                'Material declared shading')
        engine = row['material'].get('_toon_engine')
        try:
            declared = materials._settings(declared, engine)
        except contract.WorkflowError as exc:
            _conflict('Material declared shading is invalid: ' + exc.message,
                      path)
        if declared['mode'] != context['templates'][row['template_id']][
                'settings']['mode']:
            _conflict('Material mode differs from template', path)
        fields = materials._fields(declared['mode'])
        sockets = materials._sockets(declared['mode'])
        actual_settings = {'mode': declared['mode']}
        for field in fields[1:]:
            socket = row['wrapper'].inputs.get(sockets[field])
            if socket is None:
                _conflict('Native wrapper input is missing: ' + field, path)
            if socket.is_linked:
                _unsupported('Native wrapper input is linked: ' + field, path)
            actual_settings[field] = _read_socket(
                socket, field, path + '.' + field)
        try:
            checked = materials._settings(actual_settings, engine)
        except contract.WorkflowError as exc:
            _conflict('Actual float32 inputs are invalid: ' + exc.message, path)
        settings_rows.append({
            'material_id': row['material_id'],
            'template_id': row['template_id'],
            'settings': checked,
        })

        for track in tracks:
            field = track['field']
            socket = row['wrapper'].inputs.get(sockets[field])
            actual = actual_settings[field]
            expected = animation_contract.evaluate_track(track, moment)
            if isinstance(actual, list):
                if not isinstance(expected, list) or len(expected) != len(actual):
                    _conflict('Expected color shape differs from actual', path)
                errors = [abs(float(a) - float(b))
                          for a, b in zip(actual, expected)]
                max_error = max(errors)
            else:
                if isinstance(expected, list):
                    _conflict('Expected scalar shape differs from actual', path)
                errors = [abs(float(actual) - float(expected))]
                max_error = errors[0]
            try:
                socket_path = socket.path_from_id('default_value')
            except (AttributeError, TypeError):
                _unsupported(
                    'Actual socket path_from_id(default_value) is unavailable',
                    path)
            evidence = {
                'material_id': row['material_id'],
                'region_id': row['region_id'],
                'template_id': row['template_id'],
                'object_id': row['assignment']['object_id'],
                'slot_index': row['assignment']['slot_index'],
                'field': field,
                'socket_name': sockets[field],
                'path': socket_path,
                'action': action_identity,
                'actual': actual,
                'expected': expected,
                'errors': errors,
                'error': max_error,
            }
            evidence_rows.append(evidence)
            if max_error > TOLERANCE:
                failed.append(evidence)

    result = {
        'schema': SAMPLE_SCHEMA,
        'scope_id': context['scope_id'],
        'frame': frame,
        'scene': scene.name,
        'scene_frame': actual_scene_frame,
        'rows': evidence_rows,
        'settings': settings_rows,
        'tolerance': TOLERANCE,
        'passed': not failed,
    }
    if failed:
        detail = _canonical(failed[0])
        error = contract.WorkflowError(
            'CONFLICT',
            'Native animation sample exceeds 1e-6: ' + detail,
            '$.bindings[' + failed[0]['material_id'] + '].' +
            failed[0]['field'])
        error.evidence = result
        raise error
    return result


def validate_tree(material, manifest):
    """Validate one unique managed NodeTree-owned Action in a new context."""
    import bpy

    if not isinstance(material, bpy.types.Material):
        _unsupported('A local managed Material datablock is required')
    context = _manifest_context(manifest)
    rows = _locate_bindings(context, deep=False)
    matches = [row for row in rows if row['material'] == material]
    if len(matches) != 1:
        _unsupported('Material is not exactly one declared animated region')
    row = matches[0]
    user_map = bpy.data.user_map()
    _static_binding(row, context, user_map)
    tracks = _tracks_for(context, row['template_id'])
    _validate_binding_animation(row, context, user_map)

    managed = _managed_actions(context['scope_id'])
    expected = set()
    animated_regions = 0
    for candidate in rows:
        animation_data = getattr(candidate['tree'], 'animation_data', None)
        if animation_data is not None and animation_data.action is not None:
            animated_regions += 1
            expected.add(animation_data.action)
    if len(expected) != animated_regions:
        _conflict('Animated region NodeTrees share one Action')
    if set(managed) != expected:
        _conflict('Managed Action set differs from region NodeTree set')
    _validate_action_uuid_uniqueness(managed)
    if not tracks:
        _validate_no_animation(row, '$.bindings[' + row['material_id'] + ']')
    return True
