# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in scalar CEL3 boundary widths inside the existing owned shader core.

The original hard comparators, palette colors, core and output identities stay
in place. Zero widths disable the smooth branch; restore remains a surface API.
"""
import hashlib
import json
import math
import uuid

from . import materials as M
from . import surface_slots as S

KEY = 'toon_band_boundary'
PROFILE = 'toon-band-boundary.v1'
_WIDTHS = {'width_low': 'Width Low', 'width_high': 'Width High'}
_MODES = ('eevee_cel3', 'eevee_cel3_reflect')
_PREFIX = 'boundary_'


class BoundaryError(ValueError):
    """Invalid request or changed owned boundary state."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def defaults():
    return dict(width_low=0., width_high=0.)


def normalize(settings):
    """Accept exactly two finite, non-boolean widths; never clamp a request."""
    if type(settings) is not dict or set(settings) != set(_WIDTHS):
        raise BoundaryError('Boundary settings require exactly width_low and width_high')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0. <= v <= 1.
           for v in settings.values()):
        raise BoundaryError('Boundary widths must be finite non-boolean values in [0,1]')
    return {key: float(settings[key]) for key in _WIDTHS}


def validate_windows(settings, threshold_low, threshold_high):
    """Validate full window widths against actual/native threshold values."""
    desired = normalize(settings)
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0. <= v <= 1.
           for v in (threshold_low, threshold_high)) or threshold_low >= threshold_high:
        raise BoundaryError('CEL3 thresholds must satisfy 0 <= low < high <= 1')
    low_min = threshold_low - desired['width_low'] / 2.
    low_max = threshold_low + desired['width_low'] / 2.
    high_min = threshold_high - desired['width_high'] / 2.
    high_max = threshold_high + desired['width_high'] / 2.
    if low_min < 0. or high_max > 1.:
        raise BoundaryError('Boundary windows must remain inside [0,1]')
    if low_max > high_min:
        raise BoundaryError('Boundary windows must not overlap')
    return desired


def _socket(sockets, identifier):
    matches = [s for s in sockets if s.identifier == identifier]
    if len(matches) != 1:
        raise BoundaryError('Boundary socket identifier changed: ' + str(identifier))
    return matches[0]


def _nodes(group):
    result = {}
    for node in group.nodes:
        nid = node.get(M.NODE_ID)
        if not isinstance(nid, str) or nid in result:
            raise BoundaryError('Core node identities are missing or duplicated')
        result[nid] = node
    return result


def _resolve(material, wrapper=None, group=None):
    if wrapper is None:
        matches = [n for n in material.node_tree.nodes if n.get(M.ROLE) == 'core_instance']
        if len(matches) != 1:
            raise BoundaryError('Exactly one owned core wrapper is required')
        wrapper = matches[0]
    if wrapper.bl_idname != 'ShaderNodeGroup' or wrapper.node_tree is None:
        raise BoundaryError('Owned core wrapper changed')
    if group is not None and wrapper.node_tree != group:
        raise BoundaryError('Boundary core reference changed')
    return wrapper, wrapper.node_tree


def _mode(material):
    settings = M._stored_json(material, M.DECLARED)
    if type(settings) is not dict or settings.get('mode') not in _MODES:
        raise BoundaryError('Boundary widths require owned CEL3 or CEL3_reflect')
    return settings['mode']


def _thresholds(wrapper):
    values = []
    for name in ('Threshold Low', 'Threshold High'):
        socket = wrapper.inputs.get(name)
        if socket is None or socket.is_linked:
            raise BoundaryError('CEL3 threshold input is missing or linked')
        values.append(float(socket.default_value))
    return values


def _anchors(group, wrapper):
    nodes = _nodes(group)
    for nid, kind in (('inputs', 'NodeGroupInput'), ('luminance', 'ShaderNodeRGBToBW'),
                      ('hard_threshold_low', 'ShaderNodeMath'), ('hard_threshold_high', 'ShaderNodeMath'),
                      ('hard_palette_shadow_mid', 'ShaderNodeMixRGB'), ('hard_palette_lit', 'ShaderNodeMixRGB')):
        if nid not in nodes or nodes[nid].bl_idname != kind or nodes[nid].mute:
            raise BoundaryError('Original CEL3 node changed: ' + nid)
    for side in ('low', 'high'):
        hard = nodes['hard_threshold_' + side]
        threshold_id = wrapper.inputs['Threshold ' + side.title()].identifier
        if hard.operation != 'GREATER_THAN' or hard.use_clamp:
            raise BoundaryError('Original CEL3 hard comparison changed')
        for socket, source in ((hard.inputs[0], nodes['luminance'].outputs[0]),
                               (hard.inputs[1], _socket(nodes['inputs'].outputs, threshold_id))):
            if len(socket.links) != 1 or socket.links[0].from_socket != source:
                raise BoundaryError('Original CEL3 scalar/threshold route changed')
    return nodes


def _route(source, target):
    return [source.node.get(M.NODE_ID), source.identifier,
            target.node.get(M.NODE_ID), target.identifier]


def _original_routes(nodes):
    return [_route(nodes['hard_threshold_' + side].outputs[0],
                   nodes['hard_palette_shadow_mid' if side == 'low' else 'hard_palette_lit'].inputs[0])
            for side in ('low', 'high')]


def _specs(side):
    prefix = _PREFIX + side + '_'
    # safe_max differs from t+w/2 only when w==0: the disabled Map Range then
    # receives a nonzero denominator, and its finite output is multiplied by 0.
    specs = [('half', 'MULTIPLY', {1: .5}), ('min', 'SUBTRACT', {}),
             ('max', 'ADD', {}), ('gate', 'GREATER_THAN', {1: 0.}),
             ('off', 'SUBTRACT', {0: 1.}), ('safe_max', 'ADD', {}),
             ('smooth', None, {}), ('hard_part', 'MULTIPLY', {}),
             ('smooth_part', 'MULTIPLY', {}), ('result', 'ADD', {})]
    return [(prefix + suffix, operation, constants) for suffix, operation, constants in specs]


def _edges(nodes, socket_ids, side):
    prefix = _PREFIX + side + '_'
    node = lambda suffix: nodes[prefix + suffix]
    incoming = nodes['inputs']
    width = _socket(incoming.outputs, socket_ids['width_' + side])
    threshold = incoming.outputs['Threshold ' + side.title()]
    hard = nodes['hard_threshold_' + side]
    palette = nodes['hard_palette_shadow_mid' if side == 'low' else 'hard_palette_lit']
    return [(width, node('half').inputs[0]), (threshold, node('min').inputs[0]),
            (node('half').outputs[0], node('min').inputs[1]),
            (threshold, node('max').inputs[0]), (node('half').outputs[0], node('max').inputs[1]),
            (width, node('gate').inputs[0]), (node('gate').outputs[0], node('off').inputs[1]),
            (node('max').outputs[0], node('safe_max').inputs[0]),
            (node('off').outputs[0], node('safe_max').inputs[1]),
            (nodes['luminance'].outputs[0], node('smooth').inputs['Value']),
            (node('min').outputs[0], node('smooth').inputs['From Min']),
            (node('safe_max').outputs[0], node('smooth').inputs['From Max']),
            (hard.outputs[0], node('hard_part').inputs[0]),
            (node('off').outputs[0], node('hard_part').inputs[1]),
            (node('smooth').outputs[0], node('smooth_part').inputs[0]),
            (node('gate').outputs[0], node('smooth_part').inputs[1]),
            (node('hard_part').outputs[0], node('result').inputs[0]),
            (node('smooth_part').outputs[0], node('result').inputs[1]),
            (node('result').outputs[0], palette.inputs[0])]


def _shape(group, nodes):
    records = {}
    for nid, node in nodes.items():
        if nid.startswith(_PREFIX):
            data = M._node_semantics(node)
            if node.bl_idname == 'ShaderNodeMapRange':
                data.update(data_type=node.data_type, interpolation_type=node.interpolation_type,
                            clamp=node.clamp)
            records[nid] = data
    # Include both replaced destination factors and all original comparator
    # output consumers, so a second hidden branch cannot bypass the contract.
    anchors = {nodes['hard_threshold_low'], nodes['hard_threshold_high']}
    factors = {nodes['hard_palette_shadow_mid'].inputs[0], nodes['hard_palette_lit'].inputs[0]}
    links = sorted(_route(x.from_socket, x.to_socket) for x in group.links
                   if x.from_node.get(M.NODE_ID, '').startswith(_PREFIX)
                   or x.to_node.get(M.NODE_ID, '').startswith(_PREFIX)
                   or x.from_node in anchors or x.to_socket in factors)
    return dict(nodes=records, routes=links)


def snapshot(material, wrapper=None, group=None):
    """Pure read-only feature admission, safe to call from materials._capture."""
    wrapper, group = _resolve(material, wrapper, group)
    present = KEY in material
    residue = [n for n in group.nodes if str(n.get(M.NODE_ID, '')).startswith(_PREFIX)
               or n.name.startswith('Toon/' + _PREFIX)]
    width_interface = [s for s in group.interface.items_tree
                       if s.item_type == 'SOCKET' and s.name in _WIDTHS.values()]
    if not present:
        if residue or width_interface or any(wrapper.inputs.get(name) for name in _WIDTHS.values()):
            raise BoundaryError('Boundary metadata is absent but managed nodes or width sockets remain')
        return None
    raw = material.get(KEY)
    if type(raw) is not str:
        raise BoundaryError('Boundary metadata must be JSON text')
    try:
        state = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise BoundaryError('Boundary metadata is invalid JSON') from error
    fields = {'profile', 'group_id', 'owner', 'generation', 'settings', 'socket_ids',
              'nodes', 'original_routes', 'shape', 'shape_sha256'}
    if type(state) is not dict or set(state) != fields or state['profile'] != PROFILE:
        raise BoundaryError('Boundary metadata profile or fields changed')
    owner = material.get(M.SCOPE)
    if (material.get(S._PROFILE) != S.PROFILE or material.get(S._OWNER) != owner
            or state['owner'] != owner or group.get(M.SCOPE) != owner
            or group.get(M.ROLE) != 'shading_core' or wrapper.get(M.SCOPE) != owner
            or state['group_id'] != group.get(M.GROUP_ID)
            or state['group_id'] != material.get(M.GROUP_ID)):
        raise BoundaryError('Boundary owner or original core identity changed')
    M._scope(owner)
    _mode(material)
    for item in (material, material.node_tree, group):
        M._local_static(item, 'Boundary managed state')
    if group.users != 1:
        raise BoundaryError('Boundary core has additional consumers')
    generation = state['generation']
    if type(generation) is not str or len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
        raise BoundaryError('Boundary generation identity changed')
    ids = state['socket_ids']
    if type(ids) is not dict or set(ids) != set(_WIDTHS) or any(type(v) is not str for v in ids.values()) or len(set(ids.values())) != 2:
        raise BoundaryError('Boundary socket identifiers changed')
    if len(width_interface) != 2:
        raise BoundaryError('Boundary width interface changed')
    actual = {}
    for key, name in _WIDTHS.items():
        interface = [s for s in width_interface if s.identifier == ids[key] and s.name == name
                     and s.in_out == 'INPUT' and s.socket_type == 'NodeSocketFloat']
        if (len(interface) != 1 or interface[0].default_value != 0.
                or interface[0].min_value != 0. or interface[0].max_value != 1.):
            raise BoundaryError('Boundary width interface identity/default changed')
        socket = _socket(wrapper.inputs, ids[key])
        if socket.name != name or socket.is_linked:
            raise BoundaryError('Boundary width input is missing, renamed or linked')
        actual[key] = float(socket.default_value)
    declared = normalize(state['settings'])
    if actual != declared:
        raise BoundaryError('Boundary widths were changed outside the declared state')
    validate_windows(actual, *_thresholds(wrapper))
    nodes = _anchors(group, wrapper)
    expected = {nid for side in ('low', 'high') for nid, _, _ in _specs(side)}
    if set(n.get(M.NODE_ID) for n in residue) != expected or set(state['nodes']) != expected or len(residue) != len(expected):
        raise BoundaryError('Boundary managed node set changed')
    for side in ('low', 'high'):
        for nid, operation, constants in _specs(side):
            node = nodes[nid]
            kind = 'ShaderNodeMath' if operation else 'ShaderNodeMapRange'
            if (node.bl_idname != kind or node.mute or node.get(M.SCOPE) != owner
                    or node.get(M.ROLE) != 'managed_core_node'):
                raise BoundaryError('Boundary node type, activation or owner changed: ' + nid)
            if operation:
                if node.operation != operation or node.use_clamp:
                    raise BoundaryError('Boundary math semantics changed: ' + nid)
            elif (node.data_type != 'FLOAT' or node.interpolation_type != 'LINEAR' or not node.clamp
                  or node.inputs['To Min'].default_value != 0. or node.inputs['To Max'].default_value != 1.):
                raise BoundaryError('Boundary Map Range semantics changed')
            for index, value in constants.items():
                if node.inputs[index].is_linked or node.inputs[index].default_value != value:
                    raise BoundaryError('Boundary math constant changed: ' + nid)
    if state['original_routes'] != _original_routes(nodes):
        raise BoundaryError('Original CEL3 palette routes changed')
    shape = _shape(group, nodes)
    expected_routes = sorted(_route(a, b) for side in ('low', 'high') for a, b in _edges(nodes, ids, side))
    if shape['routes'] != expected_routes:
        raise BoundaryError('Boundary scalar graph routes changed')
    if (state['shape'] != shape or state['shape_sha256'] != _digest(shape)
            or state['nodes'] != sorted(expected)):
        raise BoundaryError('Boundary graph shape/signature changed')
    return state


def _owned(context):
    owned = S._owned(context, eevee=True)
    material = owned[3]
    if 'toon_finish_binding' in material:
        from . import finish_core
        finish_core._owned(context)
    _mode(material)
    wrapper, group = _resolve(material)
    _anchors(group, wrapper)
    return owned, wrapper, group


def _metadata(material):
    return {key: material[key] for key in (S._BASELINE, S._DECLARED, S._SIGNATURE)}


def _reconcile(context, material):
    # This is an explicitly authorized feature edit, following finish_core's
    # attach handshake. Never remove markers or rewrite core declared settings.
    material[S._BASELINE] = S._json(M.managed_snapshot(material[S._OWNER]))
    material[S._SIGNATURE] = S._signature(material)
    S._owned(context, eevee=True)
    if 'toon_finish_binding' in material:
        from . import finish_core
        finish_core._owned(context)


def _read_widths(wrapper, socket_ids):
    return {key: float(_socket(wrapper.inputs, identifier).default_value)
            for key, identifier in socket_ids.items()}


def _write_widths(wrapper, socket_ids, settings):
    for key, identifier in socket_ids.items():
        _socket(wrapper.inputs, identifier).default_value = settings[key]
    return _read_widths(wrapper, socket_ids)


def apply(context, settings):
    """Attach once to a strictly unchanged ordinary owned CEL3 surface."""
    owned, wrapper, group = _owned(context)
    material, owner = owned[3], owned[4]
    if snapshot(material, wrapper, group) is not None:
        raise BoundaryError('Boundary feature already exists; use update')
    desired = validate_windows(settings, *_thresholds(wrapper))
    old_metadata = _metadata(material)
    nodes = _anchors(group, wrapper)
    routes = _original_routes(nodes)
    original_links = [(nodes[row[0]].outputs[0], nodes[row[2]].inputs[0]) for row in routes]
    created_nodes, created_sockets = [], []
    socket_ids = {}
    try:
        for key, name in _WIDTHS.items():
            socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat')
            created_sockets.append(socket)
            socket.default_value = 0.
            socket.min_value, socket.max_value = 0., 1.
            socket_ids[key] = socket.identifier
        actual = _write_widths(wrapper, socket_ids, desired)
        validate_windows(actual, *_thresholds(wrapper))
        for side in ('low', 'high'):
            for index, (nid, operation, constants) in enumerate(_specs(side)):
                node = group.nodes.new('ShaderNodeMath' if operation else 'ShaderNodeMapRange')
                created_nodes.append(node)
                M._tag(node, owner, 'managed_core_node')
                node[M.NODE_ID] = nid
                node.name, node.label = 'Toon/' + nid, nid
                node.location = (-100 + index * 160, -650 if side == 'low' else -1100)
                if operation:
                    node.operation, node.use_clamp = operation, False
                    for socket in node.inputs:
                        socket.default_value = 0.
                    for position, value in constants.items():
                        node.inputs[position].default_value = value
                else:
                    node.data_type, node.interpolation_type, node.clamp = 'FLOAT', 'LINEAR', True
                    node.inputs['To Min'].default_value = 0.
                    node.inputs['To Max'].default_value = 1.
                nodes[nid] = node
        for side in ('low', 'high'):
            for source, target in _edges(nodes, socket_ids, side):
                group.links.new(source, target)
        shape = _shape(group, nodes)
        state = dict(profile=PROFILE, group_id=group[M.GROUP_ID], owner=owner,
                     generation=uuid.uuid4().hex, settings=actual, socket_ids=socket_ids,
                     nodes=sorted(shape['nodes']), original_routes=routes,
                     shape=shape, shape_sha256=_digest(shape))
        material[KEY] = _json(state)
        snapshot(material, wrapper, group)
        _reconcile(context, material)
        return inspect(context)
    except BaseException as error:
        try:
            if KEY in material:
                del material[KEY]
            for node in reversed(created_nodes):
                group.nodes.remove(node)
            for socket in reversed(created_sockets):
                group.interface.remove(socket)
            for source, target in original_links:
                group.links.new(source, target)
            for key, value in old_metadata.items():
                material[key] = value
        except BaseException as rollback_error:
            raise BoundaryError('Boundary apply failed; rollback incomplete: ' + str(rollback_error)) from error
        raise


def update(context, settings):
    """Change only existing unlinked width values, with full rollback."""
    owned, wrapper, group = _owned(context)
    material = owned[3]
    state = snapshot(material, wrapper, group)
    if state is None:
        raise BoundaryError('Boundary feature is absent; use apply')
    desired = validate_windows(settings, *_thresholds(wrapper))
    old_json, old_metadata = material[KEY], _metadata(material)
    old_widths = dict(state['settings'])
    try:
        actual = _write_widths(wrapper, state['socket_ids'], desired)
        validate_windows(actual, *_thresholds(wrapper))
        state['settings'] = actual
        material[KEY] = _json(state)
        snapshot(material, wrapper, group)
        _reconcile(context, material)
        return inspect(context)
    except BaseException as error:
        try:
            _write_widths(wrapper, state['socket_ids'], old_widths)
            material[KEY] = old_json
            for key, value in old_metadata.items():
                material[key] = value
        except BaseException as rollback_error:
            raise BoundaryError('Boundary update failed; rollback incomplete: ' + str(rollback_error)) from error
        raise


def inspect(context):
    """Read-only strict admission; absent feature keeps old snapshots untouched."""
    owned, wrapper, group = _owned(context)
    state = snapshot(owned[3], wrapper, group)
    if state is None:
        return dict(profile=PROFILE, status='absent', settings=defaults())
    return dict(state, status='valid')


def capture(context):
    """Capture an in-process composition rollback token; never serialize it."""
    owned, wrapper, group = _owned(context)
    material, owner = owned[3], owned[4]
    state = snapshot(material, wrapper, group)
    return dict(profile=PROFILE, status='captured', scene=context.scene,
                target=owned[0], slot_index=owned[1], material=material,
                material_id=material.get(M.IDENTITY), owner=owner,
                wrapper=wrapper, group=group, group_id=group.get(M.GROUP_ID),
                had_key=state is not None, old_json=material.get(KEY),
                old_widths=dict(state['settings']) if state is not None else None,
                generation=state['generation'] if state is not None else None,
                metadata=_metadata(material),
                before=json.loads(_json(owned[5])))


def rollback(context, token):
    """Internal transaction undo, including detach of a newly attached feature.

    The caller must roll back any later finish changes first. This is not an
    author-facing removal API and refuses foreign core/material identities.
    """
    if (type(token) is not dict or token.get('profile') != PROFILE
            or token.get('status') != 'captured' or token.get('scene') != context.scene):
        raise BoundaryError('Boundary rollback token is unavailable for this scene')
    owned, wrapper, group = _owned(context)
    material = owned[3]
    if (owned[0] != token['target'] or owned[1] != token['slot_index']
            or material != token['material'] or material.get(M.IDENTITY) != token['material_id']
            or owned[4] != token['owner'] or wrapper != token['wrapper']
            or group != token['group'] or group.get(M.GROUP_ID) != token['group_id']):
        raise BoundaryError('Boundary rollback material/core/slot identity changed')
    state = snapshot(material, wrapper, group)
    # External wrapper/finish nodes and original CEL settings belong to their
    # own transactions, so require those to be restored before touching core.
    before_row = token['before']['materials'][0]
    current_row = owned[5]['materials'][0]
    if (_json(before_row['wrapper']) != _json(current_row['wrapper'])
            or _json(before_row['settings']) != _json(current_row['settings'])):
        raise BoundaryError('Restore later finish/core setting changes before boundary rollback')
    if token['had_key']:
        if state is None or state['generation'] != token['generation']:
            raise BoundaryError('Boundary rollback generation changed')
        if token['old_json'] != material[KEY]:
            _write_widths(wrapper, state['socket_ids'], token['old_widths'])
            material[KEY] = token['old_json']
    elif state is not None:
        nodes = _anchors(group, wrapper)
        routes = state['original_routes']
        interfaces = [_socket([s for s in group.interface.items_tree if s.item_type == 'SOCKET'], identifier)
                      for identifier in state['socket_ids'].values()]
        for nid in state['nodes']:
            group.nodes.remove(nodes[nid])
        for interface in reversed(interfaces):
            group.interface.remove(interface)
        for from_id, from_socket, to_id, to_socket in routes:
            group.links.new(_socket(nodes[from_id].outputs, from_socket),
                            _socket(nodes[to_id].inputs, to_socket))
        del material[KEY]
    restored = M.managed_snapshot(token['owner'])
    if _json(restored) != _json(token['before']):
        raise BoundaryError('Boundary rollback did not exactly restore the captured managed snapshot')
    for key, value in token['metadata'].items():
        material[key] = value
    S._owned(context, eevee=True)
    token['status'] = 'rolled_back'
    return dict(profile=PROFILE, status='rolled_back')


__all__ = ['KEY', 'PROFILE', 'BoundaryError', 'defaults', 'normalize',
           'validate_windows', 'snapshot', 'apply', 'update', 'inspect',
           'capture', 'rollback']
