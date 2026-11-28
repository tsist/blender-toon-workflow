"""Bounded native Toon work unit; callers own disk protection and persistence.

SPDX-License-Identifier: GPL-3.0-or-later
No file opening/saving, source Text execution, handlers, preference changes or UI
registration. A plan and its original protection expectation precede mutation.
"""
import copy
import json
import math

import bpy

from . import contract, materials, regions, outline_bpy, outline_core, state_contract
from .contract import WorkflowError

SCOPE = '_toon_scope_id'
ROLE = '_toon_role'
STATE = '_toon_state'
STATE_SCHEMA = state_contract.STATE_SCHEMA


def fail(code, message, path='$'):
    raise WorkflowError(code, message, path)


def activate(scene):
    if bpy.context.window:
        bpy.context.window.scene = scene
        bpy.context.window.view_layer = scene.view_layers[0]
    if bpy.context.scene != scene:
        fail('UNSUPPORTED', 'Cannot activate the explicitly requested scene')
    bpy.context.view_layer.update()


def curves(action):
    if action:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    yield from bag.fcurves


def _record(item):
    # Reuse the project's full Mesh/UV/attribute/node/Action reader. Additional
    # Toon witnesses close slot, collection and shape-key metadata gaps.
    import inspection as reader
    result = reader.record(item)
    if isinstance(item, bpy.types.Object):
        result['object_configuration'] = reader.configuration(item)
        # These writable getters are evaluated/cache-derived views of authored
        # transforms and geometry. A setter changes the separately protected
        # location/rotation/scale/basis/parent state; their cached readback is
        # not a second persisted authoring value (e.g. a Mirror bounding box).
        for derived in ('dimensions', 'matrix_world', 'matrix_local'):
            result['object_configuration'].pop(derived, None)
        result['material_slots'] = [{'link': s.link, 'material': reader.identity(s.material) if s.material else None}
                                    for s in item.material_slots]
    if isinstance(item, bpy.types.Key):
        result['key_configuration'] = reader.configuration(item)
        result['key_blocks'] = [{'settings': reader.configuration(k),
                                 'relative': k.relative_key.name if k.relative_key else None}
                                for k in item.key_blocks]
    if isinstance(item, bpy.types.Collection):
        result['membership'] = {'objects': sorted(o.name for o in item.objects),
                                'children': sorted(c.name for c in item.children)}
    if isinstance(item, bpy.types.Scene):
        result['scene_render'] = reader.configuration(item.render)
        result['scene_cycles'] = reader.configuration(item.cycles)
        result['scene_eevee'] = reader.configuration(item.eevee)
        result['scene_children'] = sorted(c.name for c in item.collection.children)
    return result


def capture_records(excluded=()):
    import inspection as reader
    excluded = set(excluded)
    return {reader.key(reader.identity(item)): _record(item) for item in reader.all_ids()
            if reader.key(reader.identity(item)) not in excluded}


def _key(item):
    import inspection as reader
    return reader.key(reader.identity(item))


def _diff(a, b, path=''):
    if type(a) != type(b):
        return path + ':type'
    if isinstance(a, dict):
        if a.keys() != b.keys(): return path + ':keys'
        for k in a:
            found = _diff(a[k], b[k], path + '/' + str(k))
            if found: return found
    elif isinstance(a, list):
        if len(a) != len(b): return path + ':length'
        for i, (x, y) in enumerate(zip(a, b)):
            found = _diff(x, y, path + '/' + str(i))
            if found: return found
    elif a != b:
        return path + ':value'
    return None


def verify_protection(expectation, plan):
    """Only predeclared changes are canonicalized; expectations stay immutable."""
    current = capture_records()
    targets = {r['key']: r for r in plan['targets']}
    differences = []
    for key, expected in expectation['records'].items():
        if key not in current:
            differences.append({'id': key, 'difference': 'missing'})
            continue
        actual = copy.deepcopy(current[key])
        wanted = copy.deepcopy(expected)
        if key in targets:
            target = targets[key]
            # An explicit source Mesh copy and identity initialization are the
            # only Object changes. Native material replacement changes slots.
            if plan['mode'] == 'create':
                for row in (wanted['object'], wanted['object_configuration']):
                    row['data'] = {'type': 'Mesh', 'name': target['copy_name'], 'library': None}
                if '_toon_object_id' not in wanted['custom_properties']:
                    wanted['custom_properties']['_toon_object_id'] = target['object_id']
                wanted['object_configuration']['custom_properties'] = copy.deepcopy(wanted['custom_properties'])
            if plan['mode'] == 'create' and regions.is_region_mode(plan['manifest']['shading']):
                # Only explicitly selected pointer deltas are authorized; retain
                # all slot/link/index fields and the active material relation.
                obj = resolve_region_targets(plan)[list(targets).index(key)]
                selected = {a['slot_index'] for a in plan['manifest']['shading']['assignments']
                            if a['object_id'] == target['object_id']}
                for index in selected:
                    if index >= len(wanted['material_slots']) or index >= len(obj.material_slots):
                        differences.append({'id': key, 'difference': 'Region slot count changed'})
                        continue
                    wanted['material_slots'][index]['material'] = regions._identity(obj.material_slots[index].material)
                active_index = wanted['object_configuration']['active_material_index']
                if active_index in selected:
                    wanted['object_configuration']['active_material'] = wanted['material_slots'][active_index]['material']
            if plan['mode'] == 'create' and materials.is_managed_mode(plan['shading_mode']):
                # Validate exact slot count/link/ownership before canonicalizing
                # the explicitly requested new Material pointers.
                slots, old_slots = actual['material_slots'], wanted['material_slots']
                links = [s['link'] for s in old_slots] or ['DATA']
                obj = bpy.data.objects[target['name']]
                valid = (len(slots) == len(links) and [s['link'] for s in slots] == links and
                         all(s.material is not None and s.material.get(SCOPE) == plan['manifest']['scope_id'] and
                             s.material.get(ROLE) == 'shading_material' for s in obj.material_slots))
                if not valid:
                    differences.append({'id': key, 'difference': 'Unexpected managed material slot replacement'})
                actual.pop('material_slots', None); wanted.pop('material_slots', None)
                actual['object_configuration'].pop('active_material', None)
                wanted['object_configuration'].pop('active_material', None)
        if key == plan['scene_key']:
            # The requested rendering/time fields are checked separately.
            for field in ('scene', 'scene_render', 'scene_cycles'):
                a, w = actual[field], wanted[field]
                allowed = ({'camera', 'frames', 'fps', 'fps_base', 'engine', 'resolution'} if field == 'scene' else
                           {'engine', 'fps', 'fps_base', 'resolution_x', 'resolution_y', 'resolution_percentage',
                            'film_transparent', 'use_border', 'use_crop_to_border'} if field == 'scene_render' else
                           {'samples', 'device', 'use_adaptive_sampling', 'use_denoising', 'seed', 'use_animated_seed'})
                if field == 'scene_cycles' and plan['manifest']['engine'] != 'CYCLES': allowed = set()
                for name in allowed:
                    a.pop(name, None); w.pop(name, None)
                    if field == 'scene_cycles' and plan['manifest']['engine'] == 'CYCLES':
                        a.get('custom_properties', {}).pop(name, None)
                        w.get('custom_properties', {}).pop(name, None)
            if plan['manifest']['engine'] == 'BLENDER_EEVEE':
                actual['scene_eevee'].pop('taa_render_samples', None)
                wanted['scene_eevee'].pop('taa_render_samples', None)
            actual['scene_children'] = [n for n in actual['scene_children'] if n != plan['collection_name']]
            wanted['scene_children'] = [n for n in wanted['scene_children'] if n != plan['collection_name']]
        if key == plan.get('collection_key'):
            actual['custom_properties'].pop(STATE, None); wanted['custom_properties'].pop(STATE, None)
            actual['membership']['objects'] = []; wanted['membership']['objects'] = []
        mismatch = _diff(wanted, actual)
        if mismatch: differences.append({'id': key, 'difference': mismatch})
    result = {'status': 'passed' if not differences else 'failed', 'checked': len(expectation['records']),
              'differences': differences, 'coverage': expectation['coverage']}
    if regions.is_region_mode(plan['manifest']['shading']):
        # A successful new snapshot alone is insufficient: bind the complete
        # source slot map captured before this run to its actual saved result.
        objects = resolve_region_targets(plan)
        result['material_regions'] = regions.verify_source_mapping(objects, plan['targets'],
            plan['manifest']['shading'], plan['manifest']['engine'], plan['manifest']['scope_id'],
            plan['region_source_targets'], create=plan['mode'] == 'create',
            animation_manifest=plan['manifest'] if contract.is_animated(plan['manifest']) else None)
        allowed = set(expectation['records']) | set(_managed_exclusions(plan['manifest']['scope_id'], plan['manifest']))
        if plan['mode'] == 'create':
            allowed.add(_key(bpy.data.collections[plan['collection_name']]))
            allowed.update(_key(o.data) for o in objects)
        extras = set(current) - allowed
        if extras:
            differences.append({'id': sorted(extras)[0], 'difference': 'Unexpected external ID after sealed preflight'})
            result['status'] = 'failed'
    return result


def resolve_region_targets(plan):
    scene = bpy.data.scenes[plan['manifest']['scene']]
    result = []
    for target in plan['targets']:
        matches = [o for o in bpy.data.objects if o.get(outline_bpy.OBJECT_ID) == target['object_id']]
        if len(matches) != 1 or matches[0].name not in scene.objects:
            fail('CONFLICT', 'Regional target UUID is missing, duplicated or outside its scene')
        obj = matches[0]
        if len([s for s in bpy.data.scenes if obj.name in s.objects]) != 1:
            fail('CONFLICT', 'Regional target is shared across scenes')
        result.append(obj)
    return result


def _managed_exclusions(scope, manifest=None):
    import inspection as reader
    result = []
    for item in reader.all_ids():
        if item.get(SCOPE) == scope and (item.get(outline_bpy.GENERATED) is True or
                                         item.get(ROLE) in ('shading_material', 'shading_core', 'outline_action')):
            result.append(_key(item))
    if contract.is_animated(manifest):
        from . import animation_bpy
        native = animation_bpy.capture_scope(manifest)
        for binding in native['bindings']:
            if binding['action'] is not None:
                action = bpy.data.actions.get(binding['action']['name'])
                if action is None or action.get(animation_bpy.ACTION_ID) != binding['action_uuid']:
                    fail('CONFLICT', 'Validated shading Action identity disappeared')
                result.append(_key(action))
    return result


def _cache_records(collection, manifest=None):
    result = {obj.name: {'record': _record(obj), 'data': _record(obj.data),
                       'action': _record(obj.animation_data.action) if obj.animation_data and obj.animation_data.action else None}
            for obj in collection.objects if obj.get(outline_bpy.GENERATED) is True}
    if manifest and manifest['schema_version'] in contract.GROUPED_VERSIONS:
        import inspection as reader
        for obj in collection.objects:
            if obj.name not in result: continue
            ink = []
            for slot in obj.material_slots:
                if slot.material is None: fail('CONFLICT', 'Native Ink slot is empty')
                record = _record(slot.material)
                record['material_configuration'] = reader.configuration(slot.material)
                ink.append(record)
            result[obj.name]['ink'] = ink
    return result


def _check_cache(collection, baseline):
    current = _cache_records(collection, baseline['manifest'])
    if current != baseline['cache_records']:
        fail('CONFLICT', 'Owned line geometry, visibility or animation was edited; explicit replacement is required')
    owned = set(collection.objects)
    users = bpy.data.user_map()
    scene = bpy.data.scenes.get(baseline['manifest']['scene'])
    # Blender 5.2's map includes the containing Scene as well as Collection for
    # ordinary object membership, although only Collection contributes users.
    # Require this exact owned mount; do not broadly allow arbitrary Scenes.
    if scene is None or collection not in list(scene.collection.children) or collection.users != 1 or set(users.get(collection, ())) != {scene}:
        fail('CONFLICT', 'Owned root has an outside mount or ID consumer')
    for obj in owned:
        if obj.get(SCOPE) != collection[SCOPE] or obj.get(outline_bpy.GENERATED) is not True:
            fail('CONFLICT', 'Foreign objects in owned output collection')
        if (set(obj.users_collection) != {collection} or obj.users != 1 or
                set(obj.users_scene) != {scene} or set(users.get(obj, ())) != {collection, scene}):
            fail('CONFLICT', 'Owned output has an outside consumer')
        if (obj.data.users != 1 or set(users.get(obj.data, ())) != {obj} or
                (obj.animation_data and obj.animation_data.action and
                 (obj.animation_data.action.users != 1 or set(users.get(obj.animation_data.action, ())) != {obj}))):
            fail('CONFLICT', 'Owned geometry or visibility action has an outside consumer')
        if baseline['manifest']['schema_version'] in contract.GROUPED_VERSIONS:
            if len(obj.material_slots) != 1:
                fail('CONFLICT', 'Native grouped carrier requires exactly one Ink material')
            material = obj.material_slots[0].material
            if (material is None or material.get(SCOPE) != collection[SCOPE]
                    or material.get(outline_bpy.GENERATED) is not True
                    or material.users != 1 or set(users.get(material, ())) != {obj.data}):
                fail('CONFLICT', 'Native Ink has a foreign identity or outside consumer')
    return current


def validate_group_styles(collection, manifest, frames):
    if manifest['schema_version'] not in contract.GROUPED_VERSIONS: return
    settings = manifest['lines']
    styles = {'global': settings}
    styles.update({g['group_id']: {**settings, **g['style']} for g in settings['groups'] if g['enabled']})
    for frame in frames:
        for group in frame['groups']:
            group_id = group['group_id']
            for name in group['objects']:
                obj = collection.objects.get(name)
                if obj is None or obj.type != 'MESH' or len(obj.material_slots) != 1:
                    fail('VALIDATION_FAILED', 'Grouped native carrier is missing or invalid')
                material = obj.material_slots[0].material
                if material is None or any(item.get('_toon_group_id') != group_id for item in (obj, obj.data, material)):
                    fail('VALIDATION_FAILED', 'Native group identity differs from frame ownership')
                if any(item.get('_toon_frame_tag') != str(frame['frame']) for item in (obj, obj.data, material)):
                    fail('VALIDATION_FAILED', 'Native group frame tag differs')
                tree = material.node_tree
                emit = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeEmission'] if tree else []
                outputs = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'] if tree else []
                if (not material.use_nodes or len(tree.nodes) != 2 or len(emit) != 1 or len(outputs) != 1
                        or len(tree.links) != 1 or tree.links[0].from_node != emit[0]
                        or tree.links[0].to_node != outputs[0]
                        or tree.links[0].from_socket != emit[0].outputs[0]
                        or tree.links[0].to_socket != outputs[0].inputs['Surface']
                        or emit[0].inputs['Strength'].default_value != 1):
                    fail('VALIDATION_FAILED', 'Native Ink node structure differs from opaque contract')
                actual = list(emit[0].inputs['Color'].default_value)
                if any(abs(a-b) > 2e-7 for a,b in zip(actual, styles[group_id]['color'])):
                    fail('VALIDATION_FAILED', 'Native Ink color differs from declared group style')


def _source_digest(mesh):
    # This comparison deliberately excludes only the new identity layers and
    # material binding. Positions, topology, UVs, weights and shape coordinates
    # still come from complete reads of the source and isolated copy.
    import inspection as reader
    data = reader.mesh_content(mesh)
    data.pop('materials')
    data['attributes'] = [a for a in data['attributes'] if a['name'] not in (outline_bpy.POINT_ID, outline_bpy.EDGE_ID)]
    if mesh.shape_keys:
        keys = _record(mesh.shape_keys)
        keys.pop('name', None)
        keys['key_configuration'].pop('name', None)
        data['shape_key_metadata'] = keys
    return data


def validate_render_contract(scene, manifest):
    actual = {'scene': scene.name, 'camera': scene.camera.name if scene.camera else None,
              'engine': scene.render.engine, 'frames': [scene.frame_start, scene.frame_end],
              'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
              'resolution': [scene.render.resolution_x, scene.render.resolution_y],
              'resolution_percentage': scene.render.resolution_percentage,
              'transparent': scene.render.film_transparent,
              'use_border': scene.render.use_border, 'use_crop_to_border': scene.render.use_crop_to_border,
              'motion_blur': scene.render.use_motion_blur,
              'dof': scene.camera.data.dof.use_dof if scene.camera else None,
              'samples': scene.eevee.taa_render_samples if manifest['engine'] == 'BLENDER_EEVEE' else scene.cycles.samples,
              'device': 'GPU' if manifest['engine'] == 'BLENDER_EEVEE' else scene.cycles.device}
    expected = {'scene': manifest['scene'], 'camera': manifest['render']['camera'],
                'engine': manifest['engine'], 'frames': [manifest['time']['start'], manifest['time']['end']],
                'fps': 24, 'fps_base': 1., 'resolution': manifest['render']['resolution'],
                'resolution_percentage': 100, 'transparent': manifest['render']['transparent'],
                'use_border': False, 'use_crop_to_border': False, 'motion_blur': False, 'dof': False,
                'samples': manifest['render']['samples'], 'device': manifest['render']['device']}
    if manifest['engine'] == 'CYCLES':
        actual['sampling_policy'] = {'seed': scene.cycles.seed, 'animated_seed': scene.cycles.use_animated_seed,
                                     'adaptive_sampling': scene.cycles.use_adaptive_sampling,
                                     'denoising': scene.cycles.use_denoising}
        expected['sampling_policy'] = {'seed': 0, 'animated_seed': False, 'adaptive_sampling': False, 'denoising': False}
    if actual != expected:
        fail('VALIDATION_FAILED', 'Actual rendering contract differs: ' + str({k: {'expected': expected[k], 'actual': actual[k]} for k in expected if expected[k] != actual[k]}))
    return {'status': 'passed', 'actual': actual,
            'device_scope': 'Host graphics context for EEVEE; CPU for Cycles. No physical GPU selection guarantee.'}


def validate_evaluation_inputs(scene, objects):
    """Reject known VIEWPORT/RENDER divergence; never rewrite user settings.

    This is a settings gate, not a claim of universal modifier equivalence.
    Call it again after frame evaluation because modifier switches can animate.
    """
    if scene.render.use_simplify:
        fail('UNSUPPORTED', 'Simplify requires a render-equivalent outline evaluation workflow')
    for obj in objects:
        for modifier in obj.modifiers:
            if modifier.show_viewport != modifier.show_render:
                fail('UNSUPPORTED', 'Outline viewport/render modifier switches differ: ' + obj.name + '/' + modifier.name)
            if not modifier.show_viewport:
                continue
            if modifier.type in {'SUBSURF', 'MULTIRES'} and modifier.levels != modifier.render_levels:
                fail('UNSUPPORTED', 'Outline viewport/render subdivision levels differ: ' + obj.name + '/' + modifier.name)
            if modifier.type == 'NODES' and modifier.node_group:
                pending, seen = [modifier.node_group], set()
                while pending:
                    group = pending.pop()
                    if group.as_pointer() in seen:
                        continue
                    seen.add(group.as_pointer())
                    for node in group.nodes:
                        if node.bl_idname == 'GeometryNodeIsViewport':
                            fail('UNSUPPORTED', 'Viewport-dependent Geometry Nodes require render-equivalent outline capture: ' + obj.name)
                        child = getattr(node, 'node_tree', None)
                        if child is not None:
                            pending.append(child)


def prepare_plan(manifest, baseline=None):
    manifest = contract.normalize_manifest(manifest)
    if not regions.is_region_mode(manifest['shading']):
        materials._settings(manifest['shading'], manifest['engine'])
    if manifest['engine'] == 'BLENDER_EEVEE' and manifest['render']['device'] != 'GPU':
        fail('UNSUPPORTED', 'EEVEE uses the host graphics context; device must explicitly be GPU')
    if manifest['engine'] == 'CYCLES' and manifest['render']['device'] != 'CPU':
        fail('UNSUPPORTED', 'This slice accepts Cycles CPU; exact GPU backend/device selection requires an extended contract')
    scene = bpy.data.scenes.get(manifest['scene'])
    if scene is None: fail('NOT_FOUND', 'Exact scene does not exist', '$.manifest.scene')
    activate(scene)
    if contract.is_animated(manifest) and scene.frame_subframe != 0:
        fail('UNSUPPORTED', 'Managed parameter animation requires an integer reference frame')
    if len(scene.view_layers) != 1:
        fail('UNSUPPORTED', 'This slice requires one explicit view layer; multilayer rendering is pending')
    camera = scene.objects.get(manifest['render']['camera'])
    if camera is None or camera.type != 'CAMERA': fail('NOT_FOUND', 'Exact camera is not in the requested scene')
    if scene.render.use_motion_blur or camera.data.dof.use_dof:
        fail('UNSUPPORTED', 'Integer-frame caches do not qualify MB or DOF')
    if scene.compositing_node_group:
        fail('UNSUPPORTED', 'Existing compositor needs an explicit separate output workflow')
    if scene.render.use_multiview or scene.render.use_sequencer and scene.sequence_editor:
        fail('UNSUPPORTED', 'Multiview or active VSE input is outside this slice')
    # Disk/cache evaluation has a separate immutable-receipt contract in the
    # public CLI. This slice never silently claims it or invokes a simulation.
    for obj in bpy.data.objects:
        if obj.particle_systems or any(m.type in {'FLUID', 'CLOTH', 'SOFT_BODY', 'DYNAMIC_PAINT', 'MESH_SEQUENCE_CACHE'} for m in obj.modifiers):
            fail('UNSUPPORTED', 'Simulation/cache input requires its future Toon receipt workflow: ' + obj.name)
    for group in bpy.data.node_groups:
        if group.bl_idname == 'GeometryNodeTree' and any(n.bl_idname in {'GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake'} for n in group.nodes):
            fail('UNSUPPORTED', 'Simulation/bake nodes require an explicit immutable cache workflow')
    scope = manifest['scope_id']
    collections = [c for c in bpy.data.collections if c.get(SCOPE) == scope and c.get(ROLE) == 'scope_root']
    if manifest['mode'] == 'create' and (collections or any(o.get(SCOPE) == scope for o in bpy.data.objects)):
        fail('CONFLICT', 'Scope already exists; create cannot overwrite it')
    collection = None
    if manifest['mode'] == 'update':
        if not isinstance(baseline, dict) or baseline.get('schema') != state_contract.state_schema(manifest) or len(collections) != 1:
            fail('CONFLICT', 'An original state baseline and exactly one owned scope are required')
        collection = collections[0]
        if collection not in list(scene.collection.children): fail('CONFLICT', 'Owned root moved outside the expected scene')
        embedded = json.loads(collection.get(STATE, '{}'))
        state_contract.validate_embedded_state(embedded)
        if embedded != baseline.get('embedded_state'):
            fail('CONFLICT', 'State baseline and persistent scope state differ')
        previous = embedded['manifest']
        for field in ('schema_version', 'scope_id', 'scene', 'engine', 'time', 'render'):
            if manifest[field] != previous[field]: fail('UNSUPPORTED', 'Update cannot replace ' + field + ' in this slice')
        if [{k: v for k, v in t.items() if k != 'initialize_identity'} for t in manifest['targets']] != [{k: v for k, v in t.items() if k != 'initialize_identity'} for t in previous['targets']]:
            fail('CONFLICT', 'Update target identities/order must remain unchanged')
        if any(t['initialize_identity'] for t in manifest['targets']): fail('CONFLICT', 'Update cannot initialize source identity')
        if manifest['shading']['mode'] != previous['shading']['mode']:
            fail('UNSUPPORTED', 'Shading mode replacement needs a separate explicit operation')
        if contract.is_animated(manifest):
            from . import animation_bpy, animation_contract
            animation_contract.validate_update_selectors(previous['animations'], manifest['animations'])
            if animation_bpy.capture_scope(previous) != embedded['shading_animation']['native']:
                fail('CONFLICT', 'Owned shading Action/slot/curve witness differs from the original baseline')
            old_templates = {t['template_id']:t['settings'] for t in previous['shading']['templates']}
            new_templates = {t['template_id']:t['settings'] for t in manifest['shading']['templates']}
            for track in previous['animations']:
                tid, field = track['template_id'], track['field']
                if tid not in new_templates or not materials._close(new_templates[tid][field], old_templates[tid][field]):
                    fail('UNSUPPORTED', 'An animated field base value cannot be changed by a static parameter update: '+field)
        _check_cache(collection, embedded)
        if materials.is_managed_mode(manifest['shading']['mode']):
            current = materials.managed_snapshot(scope)
            materials._plan_update(scope, embedded['materials'], current, manifest['shading'],
                                   previous['shading'] if manifest['schema_version'] in contract.GROUPED_VERSIONS else None)
    targets = []
    for item in manifest['targets']:
        obj = scene.objects.get(item['object_name']) if manifest['mode'] == 'create' else next((o for o in scene.objects if o.get(outline_bpy.OBJECT_ID) == item['object_id']), None)
        if obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.override_library or obj.data.library or obj.data.override_library:
            fail('UNSUPPORTED', 'Local, non-override Object Mode Mesh target required: ' + item['object_name'])
        if len([s for s in bpy.data.scenes if obj.name in s.objects]) != 1:
            fail('CONFLICT', 'Target is shared across scenes: ' + obj.name)
        collisions = [o for o in bpy.data.objects if o != obj and o.get(outline_bpy.OBJECT_ID) == item['object_id']]
        if collisions: fail('CONFLICT', 'Duplicate explicit Object UUID')
        if any(m != obj.data and m.get(outline_bpy.MESH_ID) == item['mesh_id'] for m in bpy.data.meshes):
            fail('CONFLICT', 'Mesh UUID belongs to a different datablock')
        if obj.get(outline_bpy.OBJECT_ID) not in (None, item['object_id']) or obj.data.get(outline_bpy.MESH_ID) not in (None, item['mesh_id']):
            fail('CONFLICT', 'Existing source identity cannot be reassigned')
        if manifest['mode'] == 'update':
            outline_bpy.initialize_source(obj, item['object_id'], item['mesh_id'], False)
        copy_name = 'ToonSource_' + item['mesh_id']
        if manifest['mode'] == 'create' and bpy.data.meshes.get(copy_name): fail('CONFLICT', 'Planned source copy name already exists')
        targets.append({**item, 'name': obj.name, 'key': _key(obj), 'original_mesh': obj.data.name,
                        'copy_name': copy_name, 'source_digest': _source_digest(obj.data)})
    if regions.is_region_mode(manifest['shading']):
        objects = [bpy.data.objects[t['name']] for t in targets]
        region_source_targets = regions.source_witness(objects, targets, manifest['shading'])
        if manifest['mode'] == 'update':
            preparation = regions.plan_update(objects, targets, manifest['shading'], manifest['engine'], scope,
                                embedded['materials'], previous['shading'],
                                animation_manifest=previous if contract.is_animated(manifest) else None,
                                desired_animation_manifest=manifest if contract.is_animated(manifest) else None)
    outline_bpy.validate_edge_overrides([(bpy.data.objects[t['name']], t) for t in targets], manifest['lines'])
    if manifest['lines']['enabled']:
        validate_evaluation_inputs(scene, [bpy.data.objects[t['name']] for t in targets])
    if contract.is_placed(manifest):
        from . import placement_core
        clip_camera = {'near': camera.data.clip_start, 'far': camera.data.clip_end}
        for effective in contract.effective_placements(manifest['lines']):
            placement_core.validate_plane_depth(effective['placement'], clip_camera)
        if collection is not None:
            original_frame, original_subframe = scene.frame_current, scene.frame_subframe
            try:
                for previous_frame in embedded['frames']:
                    scene.frame_set(previous_frame['frame']); bpy.context.view_layer.update()
                    validate_frame_placement(scene, collection, previous, previous_frame)
            except contract.WorkflowError as exc:
                fail('CONFLICT', 'Baseline native placement no longer matches original sources: '+exc.message)
            finally:
                scene.frame_set(original_frame, subframe=original_subframe); bpy.context.view_layer.update()
    excluded = _managed_exclusions(scope, previous if contract.is_animated(manifest) else None) if collection else []
    plan = {'schema': 'toon-plan-v1', 'mode': manifest['mode'], 'manifest': manifest,
            'scene_key': _key(scene), 'reference_frame': scene.frame_current,
            'collection_name': collection.name if collection else 'ToonScope_' + scope,
            'collection_key': _key(collection) if collection else None, 'targets': targets,
            'shading_mode': manifest['shading']['mode'], 'excluded_owned': excluded}
    if regions.is_region_mode(manifest['shading']):
        plan['region_source_targets'] = region_source_targets
        if manifest['mode']=='update' and contract.is_animated(manifest):
            plan['region_preparation']=preparation
    if collection is None and bpy.data.collections.get(plan['collection_name']): fail('CONFLICT', 'Planned scope collection name exists')
    expectation = {'schema': 'toon-expectation-v1', 'reference_frame': scene.frame_current,
                   'records': capture_records(excluded),
                   'coverage': 'Project versioned authoring RNA Mesh/UV/attributes/weights/shape coordinates/nodes/Action reader plus authored Object configuration, material slots, shape-key metadata and collection memberships. Object dimensions/world/local matrices are derived observations, not separate authoring values; their authored transform inputs remain exact. Target evaluated line geometry and rendered replay have separate witnesses. Opaque plugin state and unenumerated external runtime state are not certified.'}
    return plan, expectation


def _visibility(obj, frame):
    for prop in ('hide_render', 'hide_viewport'):
        for moment, value in ((frame - 1, True), (frame, False), (frame + 1, True)):
            setattr(obj, prop, value)
            obj.keyframe_insert(data_path=prop, frame=moment)
    action = obj.animation_data.action
    action[SCOPE] = obj[SCOPE]; action[ROLE] = 'outline_action'
    for curve in curves(action):
        for point in curve.keyframe_points: point.interpolation = 'CONSTANT'


def _reuse_visibility_action(obj, binding, scope):
    """Bind a pre-clear witnessed 0.3 Action/slot without rewriting its data."""
    action, slot, name = binding
    if (bpy.data.actions.get(name) != action or action.name != name
            or action.get(SCOPE) != scope or action.get(ROLE) != 'outline_action'
            or action.library or action.override_library or action.users != 0
            or slot is None or not any(item == slot for item in action.slots) or slot.target_id_type != 'OBJECT'):
        fail('CONFLICT', 'Visibility Action is not an isolated witnessed baseline binding')
    if obj.animation_data is not None:
        fail('CONFLICT', 'New carrier already has animation data')
    obj.hide_render = True; obj.hide_viewport = True
    animation = obj.animation_data_create()
    animation.action = action
    animation.action_slot = slot
    if animation.action != action or animation.action_slot != slot:
        fail('CONFLICT', 'Witnessed visibility Action/slot binding was not retained')


def _frame_report(row, frame, manifest=None):
    return state_contract.frame_report(row, frame, manifest)


def validate_frame_placement(scene, collection, manifest, frame_row):
    from . import placement_bpy
    return placement_bpy.validate_frame(scene, collection, manifest, frame_row)


def apply_plan(plan, expectation, baseline=None, progress=None, frame_evidence=None):
    manifest, scope = plan['manifest'], plan['manifest']['scope_id']
    if contract.is_animated(manifest):
        from . import animation_bpy, animation_state
    scene = bpy.data.scenes[manifest['scene']]; activate(scene)
    if capture_records(plan['excluded_owned']) != expectation['records']:
        fail('CONFLICT', 'Scene changed after the sealed preflight expectation')
    objects = [bpy.data.objects[t['name']] for t in plan['targets']]
    ink_pool = None
    action_pool = None
    if plan['mode'] == 'create':
        collection = bpy.data.collections.new(plan['collection_name'])
        from .._ui_kit import apply_collection_theme

        apply_collection_theme(collection, 'toon')
        collection[SCOPE] = scope; collection[ROLE] = 'scope_root'
        scene.collection.children.link(collection)
        # Retain former Mesh and already orphaned input IDs by references on
        # the new owned root, without changing any source fake-user flag.
        import inspection as reader
        retained = {i for i in reader.all_ids() if i.users == 0 and i != collection and not i.library}
        retained.update(obj.data for obj in objects)
        for index, item in enumerate(sorted(retained, key=lambda x: _key(x))):
            collection['_toon_retained_%06d' % index] = item
        for obj, target in zip(objects, plan['targets']):
            source = obj.data
            duplicate = source.copy(); duplicate.name = target['copy_name']; obj.data = duplicate
            if duplicate.name != target['copy_name'] or _source_digest(duplicate) != target['source_digest']:
                fail('VALIDATION_FAILED', 'Isolated Mesh differs from sealed source content')
            outline_bpy.initialize_source(obj, target['object_id'], target['mesh_id'], target['initialize_identity'])
        shading = (regions.apply_regions(objects, plan['targets'], manifest['shading'], manifest['engine'], scope)
                   if regions.is_region_mode(manifest['shading']) else
                   materials.apply_materials(objects, manifest['shading'], manifest['engine'], scope))
        if contract.is_animated(manifest):
            from . import animation_bpy, animation_state
            animation_state.validate_effective(manifest, shading['snapshot'])
            animation_change = animation_bpy.create(manifest)
    else:
        collection = bpy.data.collections[plan['collection_name']]
        _check_cache(collection, baseline['embedded_state'])
        if manifest['schema_version'] in contract.STYLED_VERSIONS:
            ink_pool = {}
            action_pool = {}
            for obj in collection.objects:
                frame_tag, group_id = obj.get('_toon_frame_tag'), obj.get('_toon_group_id')
                material = obj.material_slots[0].material
                if (not isinstance(frame_tag, str) or not isinstance(group_id, str)
                        or any(item.get('_toon_frame_tag') != frame_tag or item.get('_toon_group_id') != group_id
                               for item in (obj.data, material))
                        or (frame_tag, group_id) in ink_pool):
                    fail('CONFLICT', 'Witnessed grouped Ink requires one exact carrier per frame and group')
                ink_pool[(frame_tag, group_id)] = material
                animation = obj.animation_data
                action, slot = (animation.action, animation.action_slot) if animation else (None, None)
                if action is None or slot is None or not any(item == slot for item in action.slots) or slot.target_id_type != 'OBJECT':
                    fail('CONFLICT', 'Witnessed carrier requires an explicit OBJECT visibility Action slot')
                action_pool[(frame_tag, group_id)] = (action, slot, action.name)
        if contract.is_animated(manifest):
            shading=regions.update_animated_regions(objects,plan['targets'],manifest,baseline['embedded_state'],
                    baseline['embedded_state']['manifest'],plan['region_preparation'],plan['reference_frame'])
            animation_change=shading['animation']
        else:
            shading = (regions.update_regions(objects, plan['targets'], manifest['shading'], manifest['engine'], scope,
                    baseline['embedded_state']['materials'], baseline['embedded_state']['manifest']['shading'],
                    animation_manifest=None)
                   if regions.is_region_mode(manifest['shading']) else
                   materials.update_parameters(scope, baseline['embedded_state']['materials'], manifest['shading'],
                    baseline['embedded_state']['manifest']['shading'] if manifest['schema_version'] in contract.GROUPED_VERSIONS else None)
                   if materials.is_managed_mode(manifest['shading']['mode']) else
                   materials.apply_materials(objects, manifest['shading'], manifest['engine'], scope))
        outline_bpy.clear_owned(collection, scope)
    scene.camera = scene.objects[manifest['render']['camera']]
    scene.render.engine = manifest['engine']
    scene.frame_start, scene.frame_end = manifest['time']['start'], manifest['time']['end']
    scene.render.fps, scene.render.fps_base = 24, 1.
    scene.render.resolution_x, scene.render.resolution_y = manifest['render']['resolution']
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = manifest['render']['transparent']
    scene.render.use_border = False; scene.render.use_crop_to_border = False
    if manifest['engine'] == 'CYCLES':
        scene.cycles.samples = manifest['render']['samples']; scene.cycles.device = manifest['render']['device']
        scene.cycles.use_adaptive_sampling = False; scene.cycles.use_denoising = False
        scene.cycles.seed = 0; scene.cycles.use_animated_seed = False
    # EEVEE 5.2 uses taa_render_samples when present. A missing setter is an
    # explicit runtime refusal, never a hidden lower quality default.
    if manifest['engine'] == 'BLENDER_EEVEE':
        if not hasattr(scene, 'eevee') or not hasattr(scene.eevee, 'taa_render_samples'):
            fail('UNSUPPORTED', 'Installed EEVEE sampling API needs version-specific support')
        scene.eevee.taa_render_samples = manifest['render']['samples']
    validate_render_contract(scene, manifest)
    frame_reports = []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame); bpy.context.view_layer.update()
        validate_render_contract(scene, manifest)
        if manifest['lines']['enabled']:
            validate_evaluation_inputs(scene, objects)
        if manifest['schema_version'] in contract.STYLED_VERSIONS:
            row = outline_bpy.build_frame(scene, objects, collection, manifest['lines'], frame, str(frame), ink_pool=ink_pool,
                                          allow_styled_jitter=contract.allows_styled_jitter(manifest))
        else:
            row = outline_bpy.build_frame(scene, objects, collection, manifest['lines'], frame, str(frame))
        if manifest['schema_version'] in contract.STYLED_VERSIONS:
            for name in row['objects']:
                obj = bpy.data.objects[name]
                binding = action_pool.get((str(frame), obj.get('_toon_group_id'))) if action_pool is not None else None
                if binding is None:
                    _visibility(obj, frame)
                else:
                    _reuse_visibility_action(obj, binding, scope)
            scene.frame_set(frame); bpy.context.view_layer.update()
        else:
            for name in row['objects']: _visibility(bpy.data.objects[name], frame)
        if contract.is_animated(manifest):
            row['shading_sample'] = animation_bpy.sample_scope(manifest, frame)
        frame_reports.append(_frame_report(row, frame, manifest))
        if contract.is_placed(manifest):
            validate_frame_placement(scene, collection, manifest, frame_reports[-1])
        if frame_evidence:
            frame_evidence(frame, row.get('chains', []), frame_reports[-1]['chain_fingerprint'])
        del row
        if progress: progress({'stage': 'line_cache', 'frame': frame, 'completed': len(frame_reports), 'total': scene.frame_end - scene.frame_start + 1})
    scene.frame_set(plan['reference_frame']); bpy.context.view_layer.update()
    protection = verify_protection(expectation, plan)
    if protection['status'] != 'passed': fail('VALIDATION_FAILED', 'Source/non-target content changed: ' + str(protection['differences'][:3]))
    for obj, target in zip(objects, plan['targets']):
        if _source_digest(obj.data) != target['source_digest']:
            fail('VALIDATION_FAILED', 'Target geometry/UV/weights/shape content changed')
    validate_group_styles(collection, manifest, frame_reports)
    snapshot = (regions.snapshot(objects, plan['targets'], manifest['shading'], manifest['engine'], scope,
                                animation_manifest=manifest if contract.is_animated(manifest) else None)
                if regions.is_region_mode(manifest['shading']) else materials.managed_snapshot(scope))
    embedded = {'schema': state_contract.state_schema(manifest), 'manifest': manifest, 'materials': snapshot,
                'cache_records': _cache_records(collection, manifest), 'frames': frame_reports,
                'reference_frame': plan['reference_frame']}
    if contract.is_animated(manifest):
        embedded['shading_animation'] = {'schema': animation_state.STATE_SCHEMA,
            'native': animation_bpy.capture_scope(manifest),
            'reference': animation_bpy.sample_scope(manifest, plan['reference_frame'])}
        shading['animation'] = animation_change
        shading['changed'] = shading['changed'] or animation_change['changed']
        shading['snapshot'] = snapshot
    state_contract.validate_embedded_state(embedded, manifest)
    collection[STATE] = contract.canonical(embedded)
    return {'embedded_state': embedded, 'shading': shading, 'protection': protection,
            'frames': frame_reports, 'target_content': 'passed'}


def inspect_scene(scene_name=None, object_names=None):
    scenes = (list(bpy.data.scenes) if scene_name is None else
              [bpy.data.scenes[scene_name]] if scene_name in bpy.data.scenes else [])
    if not scenes: fail('NOT_FOUND', 'Exact scene does not exist')
    requested = set(object_names or [])
    if requested - {o.name for s in scenes for o in s.objects}: fail('NOT_FOUND', 'Object filter contains absent names')
    return {'scenes': [{'name': s.name, 'engine': s.render.engine, 'camera': s.camera.name if s.camera else None,
                        'frames': [s.frame_start, s.frame_end], 'fps': s.render.fps} for s in scenes],
            'objects': [{'name': o.name, 'type': o.type, 'object_id': o.get(outline_bpy.OBJECT_ID),
                         'mesh': o.data.name if o.type == 'MESH' else None,
                         'mesh_id': o.data.get(outline_bpy.MESH_ID) if o.type == 'MESH' else None,
                         'local': not bool(o.library or o.override_library),
                         'materials': [s.material.name if s.material else None for s in o.material_slots],
                         'modifiers': [{'name': m.name, 'type': m.type} for m in o.modifiers]}
                        for o in sorted(set(o for s in scenes for o in s.objects), key=lambda o: o.name)
                        if not requested or o.name in requested],
            'scopes': [{'collection': c.name, 'scope_id': c.get(SCOPE), 'has_state': STATE in c}
                       for c in bpy.data.collections if c.get(ROLE) == 'scope_root'],
            'not_run': ['render', 'animation_quality', 'licensing', 'GUI', 'installation']}
