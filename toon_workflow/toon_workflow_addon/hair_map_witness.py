# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent HM material, source and evaluated-data freezing evidence.

The JSON reader is bpy-free.  Authoring and actual RENDER observations are
delegated to hair_map; their exact source and evaluated payloads are compared
across every subsequent line sample, rather than accepted once per job.
"""
import math
import re

SCHEMA = 'toon-hair-map-freeze.v1'
SCHEMA_SDF = 'toon-hair-map-freeze.v2'
PROFILE = 'toon-hair-map.v1'
_OWNER = re.compile(r'[0-9a-f]{32}')
_SHA = re.compile(r'[0-9a-f]{64}')


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _render_time(depsgraph):
    _need(depsgraph.mode == 'RENDER', 'HM requires the actual RENDER dependency graph')
    scene = depsgraph.scene_eval
    return dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe)


def _marked(material):
    """Reserved remnants must not silently become an ordinary material."""
    prefixes = ('toon_hm', 'toon_hair_map')
    if any(str(key).startswith(prefixes) for key in material.keys()):
        return True
    tree = material.node_tree
    if tree is None:
        return False
    for node in tree.nodes:
        if any(str(key).startswith(prefixes) for key in node.keys()):
            return True
        group = getattr(node, 'node_tree', None)
        if group is not None and any(str(key).startswith(prefixes) for key in group.keys()):
            return True
    return False


def capture(scene, objects, *, depsgraph):
    import bpy
    from . import hair_map
    stamp = _render_time(depsgraph)
    _need(stamp == dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe),
          'HM Main and actual RENDER times disagree')
    rows = []
    owners = {}
    for obj in sorted(objects, key=lambda item: item.name):
        evaluated = obj.evaluated_get(depsgraph)
        used = {polygon.material_index for polygon in evaluated.data.polygons}
        for index, slot in enumerate(evaluated.material_slots):
            material = slot.material
            if index not in used or material is None:
                continue
            material = material.original
            present = hair_map.present(material)
            if not present:
                _need(not _marked(material), 'HM shader has lost its explicit material binding')
                continue
            consumers = {candidate for candidate in bpy.data.objects
                         if candidate.type == 'MESH' and any(
                             item.material == material for item in candidate.material_slots)}
            _need(consumers == {obj}, 'HM material is shared by another Object')
            observed = hair_map.observe(scene, obj, index, depsgraph, frozen=False)
            _need(type(observed) is dict and set(observed) == {'source', 'evaluated'},
                  'HM observer returned an unsupported payload')
            source = observed['source']
            owner = source.get('owner') if type(source) is dict else None
            key = (obj.name, material.name)
            row = dict(profile=PROFILE, target=obj.name, slot=index, material=material.name,
                       owner=owner, before=source, render_before=observed['evaluated'])
            _source(source, row)
            _need(owner not in owners or owners[owner] == key, 'HM owner is reused by another material')
            owners[owner] = key
            _evaluated(observed['evaluated'], source)
            rows.append(row)
    return dict(schema=SCHEMA_SDF if any('sdf' in row['before'] for row in rows) else SCHEMA,
                render_time=stamp, targets=rows)


def verify_frozen(receipt):
    import bpy
    from . import hair_map
    scene = bpy.data.scenes.get(receipt['render_time']['scene'])
    _need(scene is not None, 'Frozen HM Scene is missing')
    for row in receipt['targets']:
        target = bpy.data.objects.get(row['target'])
        _need(target is not None, 'Frozen HM target is missing')
        row['frozen'] = hair_map.describe(scene, target, row['slot'], frozen=True)
        row['data_exact'] = row['before'] == row['frozen']
    validate_receipt(receipt, require_render=False)


def verify_render(receipt, depsgraph):
    import bpy
    from . import hair_map
    _need(_render_time(depsgraph) == receipt['render_time'], 'HM actual RENDER time changed')
    scene = bpy.data.scenes.get(receipt['render_time']['scene'])
    _need(scene is not None, 'Frozen HM Scene is missing')
    for row in receipt['targets']:
        target = bpy.data.objects.get(row['target'])
        _need(target is not None, 'Frozen HM target is missing')
        observed = hair_map.observe(scene, target, row['slot'], depsgraph, frozen=True)
        _need(type(observed) is dict and set(observed) == {'source', 'evaluated'},
              'Frozen HM observer returned an unsupported payload')
        _need(observed['source'] == row['before'],
              'HM source/shader/image/binding changed between actual RENDER graphs')
        row['render_frozen'] = observed['evaluated']
    validate_receipt(receipt)


def _json(value, *, depth=0):
    """Bound JSON types and every nonfinite value before accepting equality."""
    _need(depth <= 24, 'HM witness nesting exceeds its reader budget')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _need(math.isfinite(value), 'HM witness contains a nonfinite number')
        return
    if type(value) is list:
        _need(len(value) <= 1024, 'HM witness list exceeds its reader budget')
        for item in value:
            _json(item, depth=depth + 1)
        return
    _need(type(value) is dict and len(value) <= 256,
          'HM witness contains unsupported JSON or excessive fields')
    for key, item in value.items():
        _need(type(key) is str, 'HM witness object key must be a string')
        if key.endswith(('sha256', 'signature')) and item is not None:
            _need(type(item) is str and _SHA.fullmatch(item) is not None,
                  'HM witness contains an invalid content/graph SHA256')
        _json(item, depth=depth + 1)


def _text(value, message):
    _need(type(value) is str and value.strip() and '\x00' not in value, message)


def _source(source, row):
    _need(type(source) is dict and {'profile', 'owner', 'target', 'material', 'group',
          'slots', 'settings', 'kernel_sha256', 'uv_name', 'binding', 'image', 'carrier', 'sun'} <= set(source),
          'HM source descriptor is incomplete')
    _json(source)
    if 'sdf' in source:
        from .hair_sdf import validate_receipt_payload
        validate_receipt_payload(source['sdf'])
        _need(source['sdf']['parent_hm_owner']==source['owner']
              and source['sdf']['target']['name']==row['target'],
              'Hair SDF child differs from its parent HM or target')
    _need(source['profile'] == PROFILE and source['owner'] == row['owner']
          and type(source['owner']) is str and _OWNER.fullmatch(source['owner']) is not None,
          'HM source profile/owner differs from its freeze target')
    _need(source['target'] == row['target'] and source['material'] == row['material'],
          'HM material or target differs from its freeze target')
    for key in ('target', 'material', 'group'):
        _text(source[key], 'HM source identity is empty: ' + key)
    slots = source['slots']
    _need(type(slots) is list and 1 <= len(slots) <= 256,
          'HM source has no bounded material-slot binding')
    seen = set()
    for slot in slots:
        _need(type(slot) is dict and set(slot) == {'index', 'link'}
              and type(slot['index']) is int and 0 <= slot['index'] <= 32767
              and slot['link'] in ('DATA', 'OBJECT') and slot['index'] not in seen,
              'Invalid or duplicate HM material-slot binding')
        seen.add(slot['index'])
    _need(row['slot'] in seen, 'HM freeze slot is outside its explicit binding')
    from .hair_map_nodes import normalize
    normalize(source['settings'])
    _need(type(source['kernel_sha256']) is str and _SHA.fullmatch(source['kernel_sha256']) is not None,
          'HM source has no valid shader graph SHA256')
    _text(source['uv_name'], 'HM source has no explicit UV sampling name')
    for key in ('binding', 'image', 'carrier', 'sun'):
        _need(type(source[key]) is dict and bool(source[key]),
              'HM source descriptor has no ' + key + ' evidence')
    if 'highlight_mask' in source:
        from .hair_highlight_mask import validate_receipt_payload
        mask=source['highlight_mask']
        validate_receipt_payload(mask)
        _need(mask['owner']==source['owner'] and mask['target']['name']==source['target'],
              'Unified highlight mask owner/target differs from its actual HM binding')
    image = source['image']
    _need(set(image) == {'R', 'G', 'sampling', 'channels'}
          and image['sampling'] == 'Linear/EXTEND/FLAT'
          and image['channels'] == 'R threshold, G directional shadow',
          'HM Image channel or sampling contract differs')
    from .sdf_witness import _image as validate_image
    for channel, index in (('R', 0), ('G', 1)):
        value = image[channel]
        validate_image(value)
        _need(value['channel'] == channel and value['channel_index'] == index,
              'HM Image channels are not the explicit Non-Color R/G pair')
    separate = {'channel', 'channel_index', 'pixel_sha256', 'min', 'max'}
    _need({k: v for k, v in image['R'].items() if k not in separate}
          == {k: v for k, v in image['G'].items() if k not in separate},
          'HM R/G descriptors do not identify the same source Image')
    carrier = source['carrier']
    _need(set(carrier) == {'group', 'owner', 'uid', 'attribute', 'graph_signature'}
          and carrier['owner'] == source['owner'] and type(carrier['uid']) is int
          and carrier['uid'] >= 0 and carrier['attribute'] == 'toon_hm_sun_' + source['owner']
          and type(carrier['graph_signature']) is str and _SHA.fullmatch(carrier['graph_signature']) is not None,
          'HM carrier owner, attribute, UID or graph identity differs')
    _text(carrier['group'], 'HM carrier Group identity is empty')
    sun = source['sun']
    _need({'object', 'data', 'type', 'energy', 'color', 'angle', 'world', 'policy'} <= set(sun)
          and sun['type'] == 'SUN' and sun['color'] == [1., 1., 1.]
          and type(sun['energy']) in (int, float) and math.isfinite(sun['energy'])
          and 0. <= sun['energy'] <= 1.e6 and type(sun['angle']) in (int, float)
          and math.isfinite(sun['angle']) and 0. <= sun['angle'] <= math.pi,
          'HM source is not its bounded static white Sun')
    for key in ('object', 'data', 'policy'):
        _text(sun[key], 'HM Sun identity or policy is empty')
    _need(type(sun['world']) is dict and set(sun['world']) == {'name', 'policy'}
          and sun['world']['policy'] == 'single constant Background, strength=0',
          'HM source does not record its zero-World policy')
    _text(sun['world']['name'], 'HM World identity is empty')
    reference = max(sun['energy'] / math.pi, 1.e-6)
    _need(abs(source['settings']['irradiance_reference'] - reference) <= max(4.e-7, reference * 2.e-7),
          'HM source illumination reference differs from its static Sun energy')
    binding = source['binding']
    _need({'ramp', 'ramp_state', 'destination', 'destination_type', 'destination_socket',
          'normal_node', 'normal_type', 'normal_socket', 'fixed_shadow', 'created_normal',
          'instance', 'fixed_descriptor'} <= set(binding)
          and type(binding['fixed_shadow']) is bool and type(binding['created_normal']) is bool,
          'HM shader seam declaration is incomplete')
    for key in ('ramp', 'destination', 'destination_type', 'destination_socket',
                'normal_node', 'normal_type', 'normal_socket', 'instance'):
        _text(binding[key], 'HM shader endpoint is empty: ' + key)
    ramp = binding['ramp_state']
    _need(type(ramp) is dict and set(ramp) == {'node', 'interpolation', 'color_mode', 'elements'}
          and ramp['node'] == binding['ramp'] and ramp['interpolation'] == 'CONSTANT'
          and ramp['color_mode'] == 'RGB' and type(ramp['elements']) is list
          and len(ramp['elements']) == 2, 'HM original palette is not a two-color CONSTANT RGB ramp')
    positions = []
    for element in ramp['elements']:
        _need(type(element) is dict and set(element) == {'position', 'color'}
              and type(element['position']) in (int, float) and math.isfinite(element['position'])
              and 0. <= element['position'] <= 1. and type(element['color']) is list
              and len(element['color']) == 4 and all(type(v) in (int, float) and math.isfinite(v)
                  for v in element['color']) and element['color'][3] == 1.,
              'HM original ramp endpoint is invalid or transparent')
        positions.append(element['position'])
    _need(positions[0] <= positions[1], 'HM original ramp order is reversed')
    fixed = binding['fixed_descriptor']
    if binding['fixed_shadow']:
        _need(type(fixed) is dict and fixed.get('profile') == 'toon-hair-fixed-shadow.hm.v3'
              and fixed.get('hm_owner') == source['owner'] and fixed.get('hm_instance') == binding['instance']
              and fixed.get('material') == source['material'] and fixed.get('channel') == 'B'
              and fixed.get('sampling') == 'Linear/REPEAT/FLAT'
              and fixed.get('source_mode') == 'hm-wrapper' and binding['destination_type'] == 'ShaderNodeGroup'
              and type(fixed.get('signature')) is str and _SHA.fullmatch(fixed['signature']) is not None,
              'HM does not record its explicit v3 fixed-shadow seam')
    else:
        _need(fixed is None and binding['destination_type'] == 'ShaderNodeEmission',
              'HM direct Color seam has an unexpected fixed-shadow layer')


def _array(value, columns, message):
    _need(type(value) is dict and {'sha256', 'shape'} <= set(value), message)
    shape = value['shape']
    _need(type(shape) is list and len(shape) == 2
          and type(shape[0]) is int and 0 < shape[0] <= 100000000
          and type(shape[1]) is int and shape[1] == columns
          and type(value['sha256']) is str and _SHA.fullmatch(value['sha256']) is not None,
          message)


def _evaluated(evaluated, source):
    _need(type(evaluated) is dict and {'uv', 'direction', 'sun_direction', 'sun_world', 'settings'} <= set(evaluated),
          'HM actual RENDER descriptor is incomplete')
    _json(evaluated)
    _array(evaluated['uv'], 2, 'Invalid HM actual RENDER UV snapshot')
    _array(evaluated['direction'], 3, 'Invalid HM actual RENDER direction snapshot')
    _need(evaluated['uv'].get('uv_name') == source['uv_name']
          and evaluated['direction'].get('attribute') == source['carrier']['attribute'],
          'HM actual RENDER UV or direction does not match its declared reader')
    vector = evaluated['sun_direction']
    _need(type(vector) is list and len(vector) == 3 and all(
        type(item) in (int, float) and math.isfinite(item) for item in vector)
        and abs(math.hypot(*vector) - 1.) <= 2.e-5,
        'HM actual RENDER Sun direction is not finite unit world-space data')
    matrix = evaluated['sun_world']
    _need(type(matrix) is list and len(matrix) == 4 and all(type(row) is list and len(row) == 4
          and all(type(v) in (int, float) and math.isfinite(v) for v in row) for row in matrix)
          and matrix[3] == [0., 0., 0., 1.], 'Invalid HM actual RENDER Sun world matrix')
    axis = [matrix[index][2] for index in range(3)]
    length = math.hypot(*axis)
    _need(length > 1.e-8 and all(abs(axis[index] / length - vector[index]) <= 2.e-5 for index in range(3)),
          'HM actual RENDER direction differs from evaluated world +Z')
    _need(evaluated['settings'] == source['settings'],
          'HM actual RENDER parameters differ from the source descriptor')
    if 'sdf' in source:
        child=evaluated.get('sdf');declared=source['sdf']
        from . import hair_sdf as S, hair_sdf_nodes as D
        omni=declared['profile']==S.PROFILE_V2
        fields=('phase','valid','elevation') if omni else ('phase','valid')
        keys={'uv','phase','valid','reference_world','sun_world','oracle','settings','oracle_max_error'}
        if omni:keys.add('elevation')
        _need(type(child) is dict and set(child)==keys,
              'Hair SDF actual RENDER child evidence is missing or unknown')
        _array(child['uv'],2,'Hair SDF actual RENDER UV is invalid')
        _need(child['uv'].get('uv_name')==declared['uv']['uv_name']
              and child['uv']['shape'][0]==evaluated['uv']['shape'][0],
              'Hair SDF actual RENDER UV differs from its reader')
        for key in fields:
            _array(child[key],1,'Hair SDF actual RENDER field is invalid')
            _need(child[key].get('attribute')==declared['carrier']['attributes'][key]
                  and child[key]['shape'][0]==evaluated['direction']['shape'][0],
                  'Hair SDF actual RENDER field differs from its carrier')
        for key in ('reference_world','sun_world'):
            value=child[key]
            _need(type(value) is list and len(value)==4 and all(type(r) is list and len(r)==4
                  and all(type(x) in (int,float) and math.isfinite(x) for x in r) for r in value)
                  and value[3]==[0.,0.,0.,1.],'Hair SDF actual RENDER transform is invalid')
        oracle=D.direction_frame(child['reference_world'],child['sun_world'],mode=D.MODE_V2) if omni else D.direction_frame(child['reference_world'],child['sun_world'])
        _need(child['sun_world']==evaluated['sun_world'] and child['settings']==declared['settings']
              and type(child['oracle']) is dict and set(child['oracle'])==set(fields)
              and all(type(child['oracle'][k]) in (int,float) and math.isfinite(child['oracle'][k])
                      and abs(child['oracle'][k]-oracle[k])<=2.e-5 for k in fields),
              'Hair SDF oracle, source settings or current Sun differs')
        errors=child['oracle_max_error']
        _need(type(errors) is dict and set(errors)==set(fields) and all(
              type(x) in (int,float) and math.isfinite(x) and 0.<=x<=2.e-5 for x in errors.values()),
              'Hair SDF actual RENDER carrier failed its live oracle')
    else:
        _need('sdf' not in evaluated,'Unexpected hair SDF RENDER evidence without a declared child')
    if 'highlight_mask' in source:
        mask=evaluated.get('highlight_mask_uv')
        _array(mask,2,'Unified highlight mask actual RENDER UV evidence is missing or invalid')
        _need(mask.get('uv_name')==source['highlight_mask']['uv']['uv_name']
              and mask['shape'][0]==evaluated['uv']['shape'][0],
              'Unified highlight mask actual RENDER UV differs from its declared reader')
    else:
        _need('highlight_mask_uv' not in evaluated,
              'Unexpected unified highlight mask UV without a declared mask')


def validate_receipt(receipt, *, require_render=True, expected_render_time=None):
    """Validate a saved HM feature without importing Blender or trusting equality."""
    _need(type(receipt) is dict and set(receipt) == {'schema', 'render_time', 'targets'}
          and receipt['schema'] in {SCHEMA,SCHEMA_SDF}, 'Invalid HM freeze receipt')
    stamp = receipt['render_time']
    _need(type(stamp) is dict and set(stamp) == {'scene', 'frame', 'subframe'}
          and type(stamp['scene']) is str and bool(stamp['scene'])
          and type(stamp['frame']) is int and type(stamp['subframe']) in (int, float)
          and math.isfinite(stamp['subframe']) and 0. <= stamp['subframe'] < 1.,
          'Invalid HM actual RENDER time witness')
    if expected_render_time is not None:
        _need(stamp == expected_render_time, 'HM time differs from the independent joint RENDER time')
    rows = receipt['targets']
    _need(type(rows) is list and len(rows) <= 256, 'Invalid or excessive HM freeze targets')
    _need((receipt['schema']==SCHEMA_SDF)==any(type(r) is dict and 'sdf' in r.get('before',{}) for r in rows),
          'HM SDF child requires its explicit v2 freeze schema')
    seen = set()
    owners = {}
    for row in rows:
        _need(type(row) is dict and {'profile', 'target', 'slot', 'material', 'owner',
              'before', 'frozen', 'data_exact', 'render_before'} <= set(row),
              'Incomplete HM freeze target')
        _text(row['target'], 'HM target name is empty')
        _text(row['material'], 'HM material name is empty')
        _need(type(row['slot']) is int and 0 <= row['slot'] <= 32767
              and row['profile'] == PROFILE, 'Invalid HM target profile or slot')
        key = (row['target'], row['slot'])
        _need(key not in seen, 'Duplicate HM freeze target slot')
        seen.add(key)
        _source(row['before'], row)
        _source(row['frozen'], row)
        binding = (row['target'], row['material'])
        _need(row['owner'] not in owners or owners[row['owner']] == binding,
              'HM owner is reused by another freeze target')
        owners[row['owner']] = binding
        _need(row['data_exact'] is True and row['before'] == row['frozen'],
              'HM source, shader, image or binding did not freeze exactly')
        _evaluated(row['render_before'], row['before'])
        if require_render:
            _need('render_frozen' in row, 'HM was not reread in the frozen RENDER graph')
            _evaluated(row['render_frozen'], row['frozen'])
            _need(row['render_before'] == row['render_frozen'],
                  'HM evaluated parameters, UV, direction or Sun changed during freezing')
    return len(seen)
