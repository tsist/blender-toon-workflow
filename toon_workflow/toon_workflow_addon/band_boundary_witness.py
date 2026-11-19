# SPDX-License-Identifier: GPL-3.0-or-later
"""Static boundary author/frozen/actual-RENDER evidence; bpy-free JSON reader."""
import hashlib
import json
import math
from types import SimpleNamespace

SCHEMA = 'toon-band-boundary-freeze.v1'
PROFILE = 'toon-band-boundary.v1'


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encode(value).encode('utf8')).hexdigest()


def _time(graph):
    _need(graph.mode == 'RENDER', 'Boundary witness requires the actual RENDER graph')
    scene = graph.scene_eval
    return dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe)


def _tree(tree, *, core=False):
    from . import materials as M
    records, lookup = {}, {}
    for node in tree.nodes:
        key = node.get(M.NODE_ID) if core else node.name
        _need(type(key) is str and key not in records, 'Boundary witness node identity changed')
        semantics = M._node_semantics(node, include_distribution=True)
        for prop in ('data_type', 'interpolation_type', 'clamp'):
            if hasattr(node, prop):
                semantics[prop] = M._plain(getattr(node, prop))
        child = getattr(node, 'node_tree', None)
        records[key] = dict(semantics=semantics, scope=node.get(M.SCOPE), role=node.get(M.ROLE),
                           inputs=[s.identifier for s in node.inputs],
                           outputs=[s.identifier for s in node.outputs],
                           group=([child.name, child.get(M.GROUP_ID)] if child is not None else None))
        lookup[node] = key
    routes = sorted([lookup[x.from_node], x.from_socket.identifier,
                     lookup[x.to_node], x.to_socket.identifier] for x in tree.links)
    interface = []
    if core:
        for socket in tree.interface.items_tree:
            _need(socket.item_type == 'SOCKET', 'Boundary core gained an interface panel')
            interface.append(dict(identifier=socket.identifier, name=socket.name,
                direction=socket.in_out, type=socket.socket_type,
                default=M._plain(socket.default_value) if hasattr(socket, 'default_value') else None))
    return dict(nodes=records, routes=routes, interface=interface)


def _shader(material):
    from . import materials as M
    from . import band_boundary as B
    wrapper, group = B._resolve(material)
    return dict(material_id=material.get(M.IDENTITY), owner=material.get(M.SCOPE),
                group_id=group.get(M.GROUP_ID), wrapper_id=wrapper.get(M.NODE_ID),
                wrapper_name=wrapper.name,
                wrapper_inputs=[[s.identifier, s.name, M._plain(s.default_value), s.is_linked]
                                for s in wrapper.inputs if hasattr(s, 'default_value')],
                shader_outputs=[[s.identifier, s.name, s.bl_idname] for s in wrapper.outputs],
                core=_tree(group, core=True), outer=_tree(material.node_tree))


def _source(material):
    from . import band_boundary as B
    feature = B.snapshot(material)
    _need(feature is not None, 'Boundary material lost its feature binding')
    payload = dict(feature=feature, shader=_shader(material))
    return dict(payload, sha256=_digest(payload))


def prepare(scene, objects):
    """Strict author admission before JointCapture changes the render engine."""
    from . import band_boundary as B
    from . import surface_slots as S
    result = {}
    for obj in sorted(objects, key=lambda item: item.name):
        if obj.type != 'MESH' or obj.hide_render:
            continue
        used = {p.material_index for p in obj.data.polygons}
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if index not in used or material is None or B.KEY not in material:
                continue
            previous = obj.active_material_index
            try:
                obj.active_material_index = index
                context = SimpleNamespace(scene=scene, active_object=obj, mode='OBJECT')
                S._owned(context, eevee=True)
                report = B.inspect(context)
                _need(report['status'] == 'valid', 'Boundary author inspection is not valid')
                source = _source(material)
                _need({k: v for k, v in report.items() if k != 'status'} == source['feature'],
                      'Boundary strict inspection and author snapshot disagree')
                wrapper, group = B._resolve(material)
                result[(obj.name, index)] = dict(source=source, target=obj, material=material,
                                                 wrapper=wrapper, group=group, link=slot.link)
            finally:
                obj.active_material_index = previous
    return result


def _observe(target, index, material, graph):
    from . import band_boundary as B
    evaluated = target.evaluated_get(graph)
    _need(0 <= index < len(evaluated.material_slots), 'Boundary RENDER slot disappeared')
    _need(any(p.material_index == index for p in evaluated.data.polygons), 'Boundary slot is not used by RENDER polygons')
    actual = evaluated.material_slots[index].material
    _need(actual is not None and actual.original == material, 'Boundary RENDER Material identity changed')
    # Observe the material carried by the evaluated object, not a copy of the
    # author descriptor. id_eval_get resolves shader evaluation when the slot
    # itself retains its original Material ID.
    actual = graph.id_eval_get(actual.original)
    _need(actual.original == material, 'Boundary evaluated Material reference changed')
    _, source_group = B._resolve(material)
    _, evaluated_group = B._resolve(actual)
    _need(actual.node_tree.original == material.node_tree and evaluated_group.original == source_group,
          'Boundary actual RENDER core or Material tree identity changed')
    return _shader(actual)


def capture(scene, objects, *, depsgraph, authors):
    from . import band_boundary as B
    stamp = _time(depsgraph)
    _need(stamp == dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe),
          'Boundary author and actual RENDER times disagree')
    rows, refs = [], {}
    for target in sorted(objects, key=lambda item: item.name):
        evaluated = target.evaluated_get(depsgraph)
        used = {p.material_index for p in evaluated.data.polygons}
        for index, slot in enumerate(evaluated.material_slots):
            if index not in used or slot.material is None or B.KEY not in slot.material.original:
                continue
            key = (target.name, index)
            _need(key in authors, 'Boundary RENDER target was not strictly admitted as an author slot')
            ref = authors[key]
            material = ref['material']
            _need(target == ref['target'] and target.material_slots[index].material == material,
                  'Boundary author slot changed before actual RENDER capture')
            before = _source(material)
            _need(before == ref['source'], 'Boundary author shader changed before actual RENDER capture')
            observed = _observe(target, index, material, depsgraph)
            _need(observed == before['shader'], 'Boundary actual RENDER shader differs from admitted author state')
            rows.append(dict(target=target.name, slot=index, slot_link=ref['link'], material=material.name,
                             before=before, render_before=observed))
            refs[key] = ref
    return dict(schema=SCHEMA, render_time=stamp, targets=rows, _refs=refs)


def _current(receipt, row):
    import bpy
    from . import band_boundary as B
    from . import materials as M
    from . import surface_slots as S
    key = (row['target'], row['slot'])
    _need('_refs' in receipt and key in receipt['_refs'], 'Boundary runtime identity references are missing')
    ref = receipt['_refs'][key]
    target, material = ref['target'], ref['material']
    _need(bpy.data.objects.get(row['target']) == target and bpy.data.materials.get(row['material']) == material,
          'Boundary frozen Object or Material identity changed')
    _need(row['slot'] < len(target.material_slots), 'Boundary frozen slot disappeared')
    slot = target.material_slots[row['slot']]
    _need(slot.material == material and slot.link == row['slot_link'] == ref['link'],
          'Boundary frozen Material/slot/link changed')
    _need(material.get(S._TARGET) == target and material.get(S._SLOT) == row['slot']
          and material.get(S._OWNER) == row['before']['feature']['owner'],
          'Boundary frozen target-slot ownership changed')
    wrapper, group = B._resolve(material)
    _need(wrapper == ref['wrapper'] and group == ref['group']
          and material.get(M.IDENTITY) == row['before']['shader']['material_id'],
          'Boundary frozen core/instance/Material identity changed')
    # Mesh datablocks and binding mesh names intentionally change in freezing.
    # The pure feature admission and full shader/output graph must not change.
    current = _source(material)
    _need(current == row['before'], 'Boundary frozen shader/socket/width/output graph changed')
    return target, material, current


def verify_frozen(receipt):
    for row in receipt['targets']:
        _, _, row['frozen'] = _current(receipt, row)
        row['data_exact'] = row['frozen'] == row['before']
    if receipt['targets']:
        validate_receipt(portable(receipt), require_render=False)


def verify_render(receipt, depsgraph):
    _need(_time(depsgraph) == receipt['render_time'], 'Boundary actual RENDER time changed')
    for row in receipt['targets']:
        target, material, _ = _current(receipt, row)
        observed = _observe(target, row['slot'], material, depsgraph)
        _need(observed == row['render_before'] == row['before']['shader'],
              'Boundary actual RENDER shader changed between line samples')
        row['render_frozen'] = observed
        row['render_checks'] = row.get('render_checks', 0) + 1
    if receipt['targets']:
        validate_receipt(portable(receipt))


def portable(receipt):
    return {key: value for key, value in receipt.items() if key != '_refs'}


def _json(value, depth=0):
    _need(depth <= 32, 'Boundary receipt exceeds its nesting budget')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _need(math.isfinite(value), 'Boundary receipt contains a nonfinite number')
        return
    _need(type(value) in (list, dict) and len(value) <= 4096, 'Boundary receipt contains unsupported or excessive data')
    if type(value) is dict:
        _need(all(type(k) is str for k in value), 'Boundary receipt keys must be strings')
        value = value.values()
    for item in value:
        _json(item, depth + 1)


def _graph(graph):
    _need(type(graph) is dict and set(graph) == {'nodes', 'routes', 'interface'}
          and type(graph['nodes']) is dict and graph['nodes'] and type(graph['routes']) is list,
          'Boundary shader graph is incomplete')
    for node in graph['nodes'].values():
        _need(type(node) is dict and set(node) == {'semantics', 'scope', 'role', 'inputs', 'outputs', 'group'}
              and type(node['inputs']) is list and type(node['outputs']) is list,
              'Boundary shader node descriptor is incomplete')
    for route in graph['routes']:
        _need(type(route) is list and len(route) == 4 and all(type(s) is str for s in route),
              'Boundary route descriptor is invalid')
        a, output, b, input_ = route
        _need(a in graph['nodes'] and b in graph['nodes']
              and output in graph['nodes'][a]['outputs'] and input_ in graph['nodes'][b]['inputs'],
              'Boundary graph route is not closed')


def _source_json(source):
    _need(type(source) is dict and set(source) == {'feature', 'shader', 'sha256'}, 'Boundary source descriptor is incomplete')
    feature, shader = source['feature'], source['shader']
    _need(type(feature) is dict and feature.get('profile') == PROFILE, 'Boundary feature profile changed')
    _need(type(shader) is dict and set(shader) == {'material_id', 'owner', 'group_id', 'wrapper_id',
          'wrapper_name', 'wrapper_inputs', 'shader_outputs', 'core', 'outer'}, 'Boundary shader descriptor is incomplete')
    for key in ('owner', 'group_id', 'material_id'):
        _need(type(shader[key]) is str and bool(shader[key]), 'Boundary identity is empty')
    _need(feature.get('owner') == shader['owner'] and feature.get('group_id') == shader['group_id'],
          'Boundary feature/core identities disagree')
    widths = feature.get('settings')
    _need(type(widths) is dict and set(widths) == {'width_low', 'width_high'}
          and all(type(v) in (int, float) and 0 <= v <= 1 for v in widths.values()), 'Boundary declared widths are invalid')
    _need(feature.get('shape_sha256') == _digest(feature.get('shape')), 'Boundary feature shape digest is invalid')
    _need(source['sha256'] == _digest(dict(feature=feature, shader=shader)), 'Boundary source digest is invalid')
    _graph(shader['core']); _graph(shader['outer'])
    _need(type(shader['shader_outputs']) is list and len(shader['shader_outputs']) == 1
          and shader['shader_outputs'][0][1:] == ['Shader', 'NodeSocketShader']
          and shader['wrapper_name'] in shader['outer']['nodes'], 'Boundary Shader output identity is missing')
    outer = shader['outer']
    _need(shader['wrapper_id'] == 'core_instance'
          and outer['nodes'][shader['wrapper_name']]['role'] == 'core_instance',
          'Boundary original core instance identity changed')
    outputs = [key for key, node in outer['nodes'].items() if node['role'] == 'managed_output'
               and node['semantics'].get('type') == 'ShaderNodeOutputMaterial'
               and node['semantics'].get('is_active_output') is True
               and node['semantics'].get('target') == 'ALL' and node['semantics'].get('mute') is False]
    _need(len(outputs) == 1, 'Boundary active managed Material Output is missing')
    reachable, pending = set(), [shader['wrapper_name']]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(route[2] for route in outer['routes'] if route[0] == current)
    _need(outputs[0] in reachable, 'Boundary Shader is disconnected from its managed Material Output')
    shader_ids = [s['identifier'] for s in shader['core']['interface']
                  if type(s) is dict and s.get('direction') == 'OUTPUT' and s.get('name') == 'Shader'
                  and s.get('type') == 'NodeSocketShader']
    _need(shader_ids == [shader['shader_outputs'][0][0]], 'Boundary original Shader socket identifiers disagree')
    ids = feature.get('socket_ids')
    _need(type(ids) is dict and set(ids) == set(widths) and len(set(ids.values())) == 2, 'Boundary width socket IDs are invalid')
    for key, identifier in ids.items():
        _need(type(identifier) is str and bool(identifier), 'Boundary socket identifier is empty')
        matches = [r for r in shader['wrapper_inputs'] if type(r) is list and len(r) == 4 and r[0] == identifier]
        _need(len(matches) == 1 and matches[0][2] == widths[key] and matches[0][3] is False,
              'Boundary native width input does not match its declaration')
    expected_nodes = {'boundary_' + side + '_' + name for side in ('low', 'high')
                      for name in ('half', 'min', 'max', 'gate', 'off', 'safe_max',
                                   'smooth', 'hard_part', 'smooth_part', 'result')}
    shape = feature.get('shape')
    _need(type(shape) is dict and set(shape) == {'nodes', 'routes'}
          and type(feature.get('nodes')) is list and set(feature['nodes']) == expected_nodes
          and type(shape['nodes']) is dict and set(shape['nodes']) == expected_nodes,
          'Boundary managed graph descriptor is incomplete')
    core = shader['core']
    _need(all(nid in core['nodes'] and core['nodes'][nid]['semantics'] == shape['nodes'][nid]
              for nid in expected_nodes), 'Boundary feature signature differs from actual core nodes')
    originals = feature.get('original_routes')
    _need(type(originals) is list and len(originals) == 2
          and all(type(r) is list and len(r) == 4 for r in originals), 'Boundary original palette routes are missing')
    factors = {(r[2], r[3]) for r in originals}
    observed_routes = [r for r in core['routes'] if r[0].startswith('boundary_')
                       or r[2].startswith('boundary_') or r[0] in ('hard_threshold_low', 'hard_threshold_high')
                       or (r[2], r[3]) in factors]
    _need(observed_routes == shape['routes'], 'Boundary feature routes differ from actual core routes')
    expected_routes = []
    def edge(a, ai, b, bi):
        expected_routes.append([a, core['nodes'][a]['outputs'][ai],
                                b, core['nodes'][b]['inputs'][bi]])
    for side in ('low', 'high'):
        prefix = 'boundary_' + side + '_'
        name = lambda suffix: prefix + suffix
        width_id = ids['width_' + side]
        threshold_inputs = [r for r in shader['wrapper_inputs'] if r[1] == 'Threshold ' + side.title()]
        _need(len(threshold_inputs) == 1, 'Boundary native threshold input is missing')
        threshold_id = threshold_inputs[0][0]
        for source, destination, position in ((width_id, 'half', 0), (threshold_id, 'min', 0),
                                               (threshold_id, 'max', 0), (width_id, 'gate', 0)):
            expected_routes.append(['inputs', source, name(destination), core['nodes'][name(destination)]['inputs'][position]])
        for a, b, bi in (('half', 'min', 1), ('half', 'max', 1), ('gate', 'off', 1),
                          ('max', 'safe_max', 0), ('off', 'safe_max', 1),
                          ('min', 'smooth', 1), ('safe_max', 'smooth', 2),
                          ('off', 'hard_part', 1), ('smooth', 'smooth_part', 0),
                          ('gate', 'smooth_part', 1), ('hard_part', 'result', 0),
                          ('smooth_part', 'result', 1)):
            edge(name(a), 0, name(b), bi)
        edge('luminance', 0, name('smooth'), 0)
        edge('hard_threshold_' + side, 0, name('hard_part'), 0)
        edge(name('result'), 0, 'hard_palette_shadow_mid' if side == 'low' else 'hard_palette_lit', 0)
    _need(sorted(expected_routes) == shape['routes'], 'Boundary receipt does not contain the closed scalar boundary graph')
    thresholds = []
    for name in ('Threshold Low', 'Threshold High'):
        values = [r for r in shader['wrapper_inputs'] if type(r) is list and len(r) == 4 and r[1] == name]
        _need(len(values) == 1 and type(values[0][2]) in (int, float) and values[0][3] is False,
              'Boundary threshold witness is missing or linked')
        thresholds.append(values[0][2])
    low, high = thresholds
    _need(0 <= low < high <= 1 and low - widths['width_low']/2 >= 0
          and high + widths['width_high']/2 <= 1
          and low + widths['width_low']/2 <= high - widths['width_high']/2,
          'Boundary witnessed windows overlap or leave [0,1]')


def validate_receipt(receipt, *, require_render=True, expected_render_time=None):
    """Validate a nonempty, closed portable receipt without importing bpy."""
    _json(receipt)
    _need(type(receipt) is dict and set(receipt) == {'schema', 'render_time', 'targets'}
          and receipt['schema'] == SCHEMA, 'Invalid boundary freeze receipt')
    stamp = receipt['render_time']
    _need(type(stamp) is dict and set(stamp) == {'scene', 'frame', 'subframe'}
          and type(stamp['scene']) is str and bool(stamp['scene']) and type(stamp['frame']) is int
          and type(stamp['subframe']) in (int, float) and 0 <= stamp['subframe'] < 1,
          'Invalid boundary actual RENDER time')
    if expected_render_time is not None:
        _need(stamp == expected_render_time, 'Boundary and joint actual RENDER times disagree')
    rows = receipt['targets']
    _need(type(rows) is list and 1 <= len(rows) <= 1024, 'Boundary receipt requires explicit targets')
    seen = set()
    required = {'target', 'slot', 'slot_link', 'material', 'before', 'render_before', 'frozen', 'data_exact'}
    for row in rows:
        _need(type(row) is dict and required <= set(row)
              and set(row) <= required | {'render_frozen', 'render_checks'}, 'Boundary target descriptor is incomplete')
        _need(type(row['target']) is str and bool(row['target']) and type(row['material']) is str and bool(row['material'])
              and type(row['slot']) is int and row['slot'] >= 0 and row['slot_link'] in ('DATA', 'OBJECT'),
              'Boundary target-slot identity is invalid')
        key = (row['target'], row['slot'])
        _need(key not in seen, 'Boundary receipt contains duplicate target slots')
        seen.add(key)
        _source_json(row['before'])
        _need(row['data_exact'] is True and row['before'] == row['frozen']
              and row['render_before'] == row['before']['shader'], 'Boundary author/RENDER/frozen evidence differs')
        if require_render:
            _need(row.get('render_frozen') == row['render_before']
                  and type(row.get('render_checks')) is int and row['render_checks'] >= 1,
                  'Boundary actual frozen RENDER evidence is missing')
    return len(rows)
