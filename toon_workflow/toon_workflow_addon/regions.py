"""Existing material-slot regions and static templates; no file IO or UI.

SPDX-License-Identifier: GPL-3.0-or-later
Each assignment owns an independent Material/Core. Source mappings are sealed
before mutation and checked again from the saved candidate by the cold reader.
"""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import struct
import uuid

from . import contract, materials

SCHEMA = 'toon-region-materials-v1'
REGION = '_toon_region_id'
TEMPLATE = '_toon_template_id'
ORIGINAL = '_toon_region_original_slot'
_SHADING_KEYS = {'mode', 'unassigned', 'templates', 'assignments'}


def _fail(code, text, path='shading'):
    raise contract.WorkflowError(code, text, path)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value, path):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        _fail('INVALID_REQUEST', 'Canonical nonzero UUID required', path)
    if str(parsed) != value or parsed.int == 0:
        _fail('INVALID_REQUEST', 'Canonical nonzero UUID required', path)
    return value


def is_region_mode(shading):
    return isinstance(shading, dict) and shading.get('mode') == 'eevee_regions'


def normalize_shading(shading, targets, engine, reserved_ids=()):
    if engine != 'BLENDER_EEVEE':
        _fail('UNSUPPORTED', 'Regions currently require BLENDER_EEVEE', 'engine')
    if not isinstance(shading, dict) or set(shading) != _SHADING_KEYS:
        _fail('INVALID_REQUEST', 'Exact region shading fields required')
    if shading['mode'] != 'eevee_regions' or shading['unassigned'] != 'preserve':
        _fail('INVALID_REQUEST', 'eevee_regions with unassigned=preserve required')
    templates, assignments = shading['templates'], shading['assignments']
    if not isinstance(templates, list) or not 1 <= len(templates) <= 16:
        _fail('INVALID_REQUEST', '1..16 explicit templates required', 'shading.templates')
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 64:
        _fail('INVALID_REQUEST', '1..64 region assignments required', 'shading.assignments')
    target_ids = {t['object_id'] for t in targets}
    occupied_ids = set(reserved_ids) | target_ids | {t['mesh_id'] for t in targets}
    template_ids, used, occupied_slots = set(), set(), set()
    for i, template in enumerate(templates):
        path = 'shading.templates[%d]' % i
        if not isinstance(template, dict) or set(template) != {'template_id', 'settings'}:
            _fail('INVALID_REQUEST', 'Exact template_id/settings required', path)
        tid = _uuid(template['template_id'], path + '.template_id')
        if tid in occupied_ids:
            _fail('INVALID_REQUEST', 'Template UUID collision', path)
        occupied_ids.add(tid); template_ids.add(tid)
        settings = materials._settings(template['settings'], engine)
        if settings['mode'] not in ('eevee_cel3', 'eevee_cel3_reflect'):
            _fail('UNSUPPORTED', 'Template must use eevee_cel3 or eevee_cel3_reflect', path)
    for i, assignment in enumerate(assignments):
        path = 'shading.assignments[%d]' % i
        if not isinstance(assignment, dict) or set(assignment) != {'region_id', 'object_id', 'slot_index', 'template_id'}:
            _fail('INVALID_REQUEST', 'Exact region assignment fields required', path)
        rid = _uuid(assignment['region_id'], path + '.region_id')
        _uuid(assignment['object_id'], path + '.object_id')
        _uuid(assignment['template_id'], path + '.template_id')
        if rid in occupied_ids:
            _fail('INVALID_REQUEST', 'Region UUID collision', path)
        occupied_ids.add(rid)
        if assignment['object_id'] not in target_ids or assignment['template_id'] not in template_ids:
            _fail('INVALID_REQUEST', 'Assignment must reference declared target/template', path)
        slot = assignment['slot_index']
        if type(slot) is not int or slot < 0:
            _fail('INVALID_REQUEST', 'Nonnegative integer source slot required', path + '.slot_index')
        pair = (assignment['object_id'], slot)
        if pair in occupied_slots:
            _fail('CONFLICT', 'Overlapping source slot assignments', path)
        occupied_slots.add(pair); used.add(assignment['template_id'])
    if used != template_ids:
        _fail('INVALID_REQUEST', 'Every template needs an explicit consumer', 'shading.templates')
    return deepcopy(shading)


def _identity(material):
    if material is None:
        return None
    return {'type': 'Material', 'name': material.name,
            'library': material.library.filepath if material.library else None}


def _objects(objects, targets):
    objects = list(objects)
    if len(objects) != len(targets) or len({id(o) for o in objects}) != len(objects):
        _fail('INVALID_REQUEST', 'Exact unique target object list required')
    result = {}
    for obj, target in zip(objects, targets):
        if (obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.override_library
                or obj.data.library or obj.data.override_library or obj.data.users != 1):
            _fail('UNSUPPORTED', 'Isolated local Object Mode Mesh required', target['object_name'])
        if (obj.name != target['object_name'] or obj.get('_toon_object_id') != target['object_id']
                or obj.data.get('_toon_mesh_id') != target['mesh_id']):
            _fail('CONFLICT', 'Region target UUID/name/Mesh identity changed', target['object_name'])
        result[target['object_id']] = obj
    return result


def _target_witness(obj, target):
    values = [p.material_index for p in obj.data.polygons]
    counts = Counter(values)
    if len(obj.material_slots) and any(index >= len(obj.material_slots) for index in counts):
        _fail('UNSUPPORTED', 'Source polygon refers outside its material slot domain')
    payload = b''.join(struct.pack('<I', value) for value in values)
    # Slot indices alone do not identify which actual faces consume a slot.
    # Persist vertex/edge/face order and connectivity independently of positions
    # so legal deformation can keep a fixed topology while rebinding is caught.
    topology = {'vertex_count': len(obj.data.vertices),
                'edges': [list(edge.vertices) for edge in obj.data.edges],
                'faces': [list(polygon.vertices) for polygon in obj.data.polygons]}
    slots = [{'slot_index': i, 'link': slot.link, 'material': _identity(slot.material),
              'data_material': _identity(obj.data.materials[i]), 'polygon_count': counts[i]}
             for i, slot in enumerate(obj.material_slots)]
    return {'object_id': target['object_id'], 'mesh_id': target['mesh_id'],
            'object_name': obj.name, 'mesh_name': obj.data.name, 'slot_count': len(slots),
            'topology_sha256': hashlib.sha256(_json(topology).encode('utf-8')).hexdigest(),
            'polygon_count': len(values), 'material_indices_sha256': hashlib.sha256(payload).hexdigest(),
            'slots': slots}


def source_witness(objects, targets, shading):
    """Read existing source slots before Mesh isolation/identity initialization."""
    objects = list(objects)
    if len(objects) != len(targets):
        _fail('INVALID_REQUEST', 'Exact source target order required')
    witnesses = [_target_witness(obj, target) for obj, target in zip(objects, targets)]
    by_id = {w['object_id']: w for w in witnesses}
    for assignment in shading['assignments']:
        target = by_id[assignment['object_id']]
        index = assignment['slot_index']
        if index >= target['slot_count']:
            _fail('INVALID_REQUEST', 'Assigned source slot does not exist', 'shading.assignments')
        if target['slots'][index]['polygon_count'] == 0:
            _fail('UNSUPPORTED', 'Assigned source slot has no polygon consumer', 'shading.assignments')
    return witnesses


def preflight(objects, targets, shading, engine, scope_id):
    materials._scope(scope_id)
    normalize_shading(shading, targets, engine, [scope_id])
    lookup = _objects(objects, targets)
    return source_witness([lookup[t['object_id']] for t in targets], targets, shading)


def _original(material):
    try:
        value = json.loads(material[ORIGINAL])
    except (KeyError, TypeError, ValueError):
        _fail('CONFLICT', 'Original region source slot metadata is invalid')
    if set(value) != {'slot_index', 'link', 'material', 'data_material'}:
        _fail('CONFLICT', 'Original region source slot metadata fields changed')
    for label, key in [('material', '_toon_previous_material_effective'),
                       ('data_material', '_toon_previous_material_data')]:
        expected = value[label]
        if expected is None:
            if key in material:
                _fail('CONFLICT', 'Unexpected original material reference')
        else:
            import bpy
            previous = material.get(key)
            if not isinstance(previous, bpy.types.Material) or _identity(previous) != expected:
                _fail('CONFLICT', 'Original material reference changed')
    return value


def snapshot(objects, targets, shading, engine, scope_id, animation_manifest=None):
    target_rows = preflight(objects, targets, shading, engine, scope_id)
    lookup = _objects(objects, targets)
    managed = materials.managed_snapshot(scope_id, animation_manifest=animation_manifest)
    captured = {m['material_id']: m for m in managed['materials']}
    rows, seen = [], set()
    for assignment in shading['assignments']:
        obj = lookup[assignment['object_id']];index = assignment['slot_index']
        material = obj.material_slots[index].material
        if (material is None or material.get(materials.SCOPE) != scope_id
                or material.get(materials.ROLE) != 'shading_material'
                or material.get(REGION) != assignment['region_id']
                or material.get(TEMPLATE) != assignment['template_id']):
            _fail('CONFLICT', 'Region material ownership or selector changed')
        mid = material.get(materials.IDENTITY)
        if mid not in captured or mid in seen:
            _fail('CONFLICT', 'Region material identity is missing or shared')
        seen.add(mid)
        expected_binding = [{'object': obj.name, 'mesh': obj.data.name, 'slot': index,
                             'link': obj.material_slots[index].link}]
        if captured[mid]['bindings'] != expected_binding:
            _fail('CONFLICT', 'Region must have exactly its declared source-slot consumer')
        origin = _original(material)
        if origin['slot_index'] != index or origin['link'] != obj.material_slots[index].link:
            _fail('CONFLICT', 'Original source slot identity/link changed')
        rows.append({**deepcopy(assignment), 'material_id': mid, 'original_slot': origin})
    if seen != set(captured):
        _fail('CONFLICT', 'Unexpected managed material outside region assignments')
    return {'schema': SCHEMA, 'scope_id': scope_id, 'managed': managed,
            'regions': rows, 'targets': target_rows}


def validate_snapshot(value, targets, shading, engine, scope_id):
    """Pure shape/binding validation; native re-observation is still required."""
    normalize_shading(shading, targets, engine, [scope_id])
    if (not isinstance(value, dict) or set(value) != {'schema', 'scope_id', 'managed', 'regions', 'targets'}
            or value['schema'] != SCHEMA or value['scope_id'] != scope_id):
        _fail('CONFLICT', 'Exact regional snapshot shape required')
    managed = value['managed']
    if (not isinstance(managed, dict) or set(managed) != {'schema', 'scope_id', 'materials'}
            or managed['schema'] != materials.SCHEMA or managed['scope_id'] != scope_id
            or not isinstance(managed['materials'], list) or not isinstance(value['regions'], list)
            or not isinstance(value['targets'], list) or len(value['targets']) != len(targets)
            or len(value['regions']) != len(shading['assignments'])):
        _fail('CONFLICT', 'Regional snapshot collections differ')
    ids = [row.get('material_id') for row in managed['materials'] if isinstance(row, dict)]
    if len(ids) != len(managed['materials']) or len(set(ids)) != len(ids):
        _fail('CONFLICT', 'Regional managed material identities differ')
    for material_id in ids:
        _uuid(material_id, 'materials.material_id')
    region_ids = []
    for row, assignment in zip(value['regions'], shading['assignments']):
        if (not isinstance(row, dict) or set(row) != set(assignment) | {'material_id', 'original_slot'}
                or {k: row[k] for k in assignment} != assignment or row['material_id'] not in ids):
            _fail('CONFLICT', 'Regional assignment snapshot differs')
        original = row['original_slot']
        if (not isinstance(original, dict) or set(original) != {'slot_index', 'link', 'material', 'data_material'}
                or original['slot_index'] != assignment['slot_index']):
            _fail('CONFLICT', 'Regional original slot snapshot differs')
        region_ids.append(row['material_id'])
    if len(set(region_ids)) != len(region_ids) or set(region_ids) != set(ids):
        _fail('CONFLICT', 'Regional material coverage differs')
    for row, target in zip(value['targets'], targets):
        keys = {'object_id', 'mesh_id', 'object_name', 'mesh_name', 'slot_count', 'polygon_count',
                'material_indices_sha256', 'topology_sha256', 'slots'}
        if (not isinstance(row, dict) or set(row) != keys or row['object_id'] != target['object_id']
                or row['mesh_id'] != target['mesh_id'] or row['object_name'] != target['object_name']
                or type(row['slot_count']) is not int or row['slot_count'] < 0
                or type(row['polygon_count']) is not int or row['polygon_count'] < 0
                or not isinstance(row['slots'], list) or len(row['slots']) != row['slot_count']):
            _fail('CONFLICT', 'Regional target witness shape differs')
        contract.validate(row['material_indices_sha256'], contract.SHA_SCHEMA, 'materials.material_indices_sha256')
        contract.validate(row['topology_sha256'], contract.SHA_SCHEMA, 'materials.topology_sha256')
        for index, slot in enumerate(row['slots']):
            if (not isinstance(slot, dict) or set(slot) != {'slot_index', 'link', 'material', 'data_material', 'polygon_count'}
                    or type(slot['slot_index']) is not int or slot['slot_index'] != index
                    or slot['link'] not in ('DATA', 'OBJECT') or type(slot['polygon_count']) is not int
                    or slot['polygon_count'] < 0):
                _fail('CONFLICT', 'Regional slot witness shape differs')
    return value


def verify_source_mapping(objects, targets, shading, engine, scope_id, source_targets, create=False, animation_manifest=None):
    """Compare original sealed slots/domain with actual region bindings."""
    actual = snapshot(objects, targets, shading, engine, scope_id, animation_manifest=animation_manifest)
    expected = deepcopy(source_targets)
    if len(expected) != len(targets):
        _fail('CONFLICT', 'Sealed source target count differs')
    if create:
        originals = {(r['object_id'], r['slot_index']): r for r in actual['regions']}
        for row, target in zip(expected, targets):
            if row['object_id'] != target['object_id'] or row['mesh_id'] != target['mesh_id']:
                _fail('CONFLICT', 'Sealed source target UUID/order differs')
            row['mesh_name'] = target['copy_name']
            observed = next(w for w in actual['targets'] if w['object_id'] == row['object_id'])
            for slot in row['slots']:
                region = originals.get((row['object_id'], slot['slot_index']))
                if region is None:
                    continue
                origin = {k: slot[k] for k in ('slot_index', 'link', 'material', 'data_material')}
                if region['original_slot'] != origin:
                    _fail('CONFLICT', 'Original material retention differs from sealed source slot')
                replacement = observed['slots'][slot['slot_index']]['material']
                slot['material'] = replacement
                if slot['link'] == 'DATA':
                    slot['data_material'] = replacement
    if actual['targets'] != expected:
        _fail('CONFLICT', 'Actual region slots/links/polygon domain differ from sealed source mapping')
    return {'status': 'passed', 'schema': SCHEMA, 'target_count': len(targets),
            'assignment_count': len(shading['assignments']),
            'source_mapping': 'exact_preflight_slots_links_and_polygon_indices'}


def apply_regions(objects, targets, shading, engine, scope_id):
    import bpy
    objects = list(objects)
    before = preflight(objects, targets, shading, engine, scope_id)
    lookup = _objects(objects, targets)
    if any(m.get(materials.SCOPE) == scope_id and m.get(materials.ROLE) == 'shading_material'
           for m in bpy.data.materials):
        _fail('CONFLICT', 'Region scope already has materials; use update')
    templates = {t['template_id']: t['settings'] for t in shading['templates']}
    new_materials, new_groups, rollback, replacements = [], [], [], {}
    try:
        for assignment in shading['assignments']:
            obj = lookup[assignment['object_id']];index = assignment['slot_index']
            slot = obj.material_slots[index]
            old_effective, old_data = slot.material, obj.data.materials[index]
            original = {'slot_index': index, 'link': slot.link, 'material': _identity(old_effective),
                        'data_material': _identity(old_data)}
            material = materials._build(bpy, materials._settings(templates[assignment['template_id']], engine),
                                        engine, scope_id, new_materials, new_groups)
            material[REGION] = assignment['region_id'];material[TEMPLATE] = assignment['template_id']
            material[ORIGINAL] = _json(original)
            if old_effective is not None:
                material['_toon_previous_material_effective'] = old_effective
            if old_data is not None:
                material['_toon_previous_material_data'] = old_data
            rollback.append((obj, index, slot.link, old_effective))
            slot.material = material
            binding = [{'object': obj.name, 'mesh': obj.data.name, 'slot': index, 'link': slot.link}]
            material[materials.BINDINGS] = materials._json(binding)
            replacements[(assignment['object_id'], index)] = _identity(material)
        after = snapshot(objects, targets, shading, engine, scope_id)
        expected = deepcopy(before)
        for target in expected:
            for slot in target['slots']:
                replacement = replacements.get((target['object_id'], slot['slot_index']))
                if replacement is not None:
                    slot['material'] = replacement
                    if slot['link'] == 'DATA':
                        slot['data_material'] = replacement
        if after['targets'] != expected:
            _fail('VALIDATION_FAILED', 'Region binding modified undeclared slot/link/polygon identity')
    except Exception:
        for obj, index, link, previous in reversed(rollback):
            if obj.material_slots[index].link != link:
                _fail('VALIDATION_FAILED', 'Unexpected slot link mutation during region rollback')
            obj.material_slots[index].material = previous
        for material in reversed(new_materials):
            if material.users == 0 and material.get(materials.SCOPE) == scope_id:
                bpy.data.materials.remove(material)
        for group in reversed(new_groups):
            if group.users == 0 and group.get(materials.SCOPE) == scope_id:
                bpy.data.node_groups.remove(group)
        raise
    return {'scope_id': scope_id, 'mode': 'eevee_regions', 'changed': True,
            'binding_policy': 'Exact existing used slots only; unassigned slots and polygon indices preserved.',
            'before_targets': before, 'snapshot': after,
            'template_consumption': {t['template_id']: materials._consumption(engine, t['settings']['mode'])
                                     for t in shading['templates']}}


def plan_update(objects, targets, shading, engine, scope_id, baseline, previous_shading, animation_manifest=None, desired_animation_manifest=None):
    normalize_shading(previous_shading, targets, engine, [scope_id])
    normalize_shading(shading, targets, engine, [scope_id])
    if (not isinstance(baseline, dict) or set(baseline) != {'schema', 'scope_id', 'managed', 'regions', 'targets'}
            or baseline['schema'] != SCHEMA or baseline['scope_id'] != scope_id):
        _fail('CONFLICT', 'Exact original regional snapshot baseline required')
    if shading['assignments'] != previous_shading['assignments']:
        _fail('UNSUPPORTED', 'Region re-assignment requires a separate explicit operation')
    previous = {t['template_id']: t['settings'] for t in previous_shading['templates']}
    desired = {t['template_id']: t['settings'] for t in shading['templates']}
    if list(desired) != list(previous):
        _fail('UNSUPPORTED', 'Template identities/order are fixed during parameter update')
    if desired_animation_manifest is not None:
        for track in desired_animation_manifest['animations']:
            tid,field=track['template_id'],track['field']
            if not materials._close(desired[tid][field],previous[tid][field]):
                _fail('UNSUPPORTED','Tracked base input cannot change; update its explicit keys: '+field)
    current = snapshot(objects, targets, shading, engine, scope_id, animation_manifest=animation_manifest)
    if current['regions'] != baseline['regions'] or current['targets'] != baseline['targets']:
        _fail('CONFLICT', 'Region/slot/link/polygon identity differs from the original baseline')
    base_managed = baseline['managed'];now_managed = current['managed']
    if (not isinstance(base_managed, dict) or set(base_managed) != {'schema', 'scope_id', 'materials'}
            or base_managed['schema'] != materials.SCHEMA or base_managed['scope_id'] != scope_id):
        _fail('CONFLICT', 'Original managed regional baseline shape differs')
    bases = {m['material_id']: m for m in base_managed['materials']}
    now = {m['material_id']: m for m in now_managed['materials']}
    if len(bases) != len(base_managed['materials']) or set(bases) != set(now):
        _fail('CONFLICT', 'Regional material set differs')
    plans = []
    for region in current['regions']:
        mid, tid = region['material_id'], region['template_id']
        old = {'schema': materials.SCHEMA, 'scope_id': scope_id, 'materials': [bases[mid]]}
        live = {'schema': materials.SCHEMA, 'scope_id': scope_id, 'materials': [now[mid]]}
        child = materials._plan_update(scope_id, old, live, desired[tid], previous[tid],
                                     defer_joint_validation=desired_animation_manifest is not None)
        plans.append({**child[0], 'region_id': region['region_id'], 'template_id': tid})
    if desired_animation_manifest is not None:
        if (not contract.is_animated(desired_animation_manifest)
                or desired_animation_manifest['shading']!=shading
                or desired_animation_manifest['scope_id']!=scope_id
                or desired_animation_manifest['engine']!=engine):
            _fail('CONFLICT', 'Explicit matching final animation context required')
        from . import animation_state
        animation_state.validate_effective(desired_animation_manifest, current, plans)
    return {'current': current, 'plans': plans}


def update_animated_regions(objects, targets, manifest, baseline, previous_manifest, preparation, reference_frame):
    """Apply a preflighted joint change without observing a half-written state."""
    import bpy
    from . import animation_bpy, animation_state
    scope=manifest['scope_id'];engine=manifest['engine'];shading=manifest['shading']
    objects=list(objects)
    verified=plan_update(objects,targets,shading,engine,scope,baseline['materials'],previous_manifest['shading'],
                         animation_manifest=previous_manifest,desired_animation_manifest=manifest)
    if verified!=preparation:
        _fail('CONFLICT','Joint preparation no longer equals independently recomputed requested plans')
    current=verified['current']
    plans=preparation['plans']
    animation_state.validate_effective(manifest,current,plans)
    lookup={m[materials.IDENTITY]:m for m in bpy.data.materials
            if m.get(materials.SCOPE)==scope and m.get(materials.ROLE)=='shading_material'}
    desired={t['template_id']:t['settings'] for t in shading['templates']}
    # Resolve and read all sockets before either subsystem writes anything.
    writes=[]
    expected=deepcopy(current)
    rows={r['material_id']:r for r in expected['managed']['materials']}
    for plan in plans:
        material=lookup[plan['material_id']]
        wrapper=next(n for n in material.node_tree.nodes if n.get(materials.ROLE)=='core_instance')
        sockets=materials._sockets(desired[plan['template_id']]['mode'])
        for field,value in plan['changes'].items():
            socket=wrapper.inputs[sockets[field]]
            writes.append((socket,materials._plain(socket.default_value),value,plan['material_id'],field))
    animation_change=None
    scene=bpy.data.scenes[manifest['scene']]
    original_frame,original_subframe=scene.frame_current,scene.frame_subframe
    try:
        animation_change=animation_bpy.update(manifest,previous_manifest,baseline['shading_animation']['native'])
        for socket,old,value,mid,field in writes:
            socket.default_value=value
            actual=materials._plain(socket.default_value)
            if not materials._close(actual,value):
                _fail('VALIDATION_FAILED','Joint regional RNA readback differs')
            rows[mid]['settings'][field]=actual
        scene=bpy.data.scenes[manifest['scene']]
        scene.frame_set(reference_frame);bpy.context.view_layer.update()
        sample=animation_bpy.sample_scope(manifest,reference_frame)
        native=animation_bpy.capture_scope(manifest)
        animation_state.validate_sample(sample,reference_frame,manifest,native)
        for row in sample['rows']:
            rows[row['material_id']]['settings'][row['field']]=row['actual']
        after=snapshot(objects,targets,shading,engine,scope,animation_manifest=manifest)
        if after!=expected:
            _fail('VALIDATION_FAILED','Joint update changed unrequested regional content')
    except Exception as original_error:
        try:
            for socket,old,_,_,_ in reversed(writes):socket.default_value=old
            if animation_change is not None:
                inverse=animation_bpy.update(previous_manifest,manifest,animation_change['capture'])
                if inverse['capture']!=baseline['shading_animation']['native']:
                    _fail('VALIDATION_FAILED','Inverse Action witness differs from original baseline')
            scene.frame_set(original_frame,subframe=original_subframe);bpy.context.view_layer.update()
            restored=snapshot(objects,targets,previous_manifest['shading'],engine,scope,animation_manifest=previous_manifest)
            if restored!=current:_fail('VALIDATION_FAILED','Joint rollback snapshot differs from original current values')
        except Exception as rollback_error:
            raise contract.WorkflowError('VALIDATION_FAILED','Joint update failed: '+str(original_error)+
                    '; rollback verification failed: '+str(rollback_error)) from original_error
        raise
    return {'scope_id':scope,'mode':'eevee_regions','changed':any(p['changes'] for p in plans),
            'updates':plans,'snapshot':after,'animation':animation_change,
            'wrapper_policy':'Joint final state verified; retained static inputs and wrapper are exact.'}


def update_regions(objects, targets, shading, engine, scope_id, baseline, previous_shading, animation_manifest=None):
    import bpy
    objects = list(objects)
    preparation = plan_update(objects, targets, shading, engine, scope_id, baseline, previous_shading, animation_manifest=animation_manifest)
    before, plans = preparation['current'], preparation['plans']
    lookup = {m[materials.IDENTITY]: m for m in bpy.data.materials
              if m.get(materials.SCOPE) == scope_id and m.get(materials.ROLE) == 'shading_material'}
    desired = {t['template_id']: t['settings'] for t in shading['templates']}
    rollback = []
    try:
        expected = deepcopy(before)
        rows = {m['material_id']: m for m in expected['managed']['materials']}
        for plan in plans:
            material = lookup[plan['material_id']]
            wrapper = next(n for n in material.node_tree.nodes if n.get(materials.ROLE) == 'core_instance')
            sockets = materials._sockets(desired[plan['template_id']]['mode'])
            for field, value in plan['changes'].items():
                socket = wrapper.inputs[sockets[field]]
                rollback.append((socket, materials._plain(socket.default_value)))
                socket.default_value = value
                actual = materials._plain(socket.default_value)
                if not materials._close(actual, value):
                    _fail('VALIDATION_FAILED', 'Regional parameter native readback differs')
                rows[plan['material_id']]['settings'][field] = actual
        after = snapshot(objects, targets, shading, engine, scope_id, animation_manifest=animation_manifest)
        if after != expected:
            _fail('VALIDATION_FAILED', 'Unexpected regional snapshot mutation during update')
    except Exception:
        for socket, value in reversed(rollback):
            socket.default_value = value
        raise
    return {'scope_id': scope_id, 'mode': 'eevee_regions', 'changed': any(p['changes'] for p in plans),
            'updates': plans, 'snapshot': after,
            'wrapper_policy': 'Current static user wrapper and unrequested legal inputs retained.'}
