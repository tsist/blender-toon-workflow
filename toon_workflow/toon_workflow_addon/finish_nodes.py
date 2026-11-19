# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable additive CEL highlight/rim kernel; no material or Action ownership.

The light vector is an explicit world-space art direction, not a live light
binding.  An optional independently created artistic-normal reader consumes
the existing fields without adding links to any prior reader.
"""
import json
import math
import uuid

PROFILE = 'toon-finish-node.v1'
HAIR_PROFILE = 'toon-finish-node.band.v1'
OWNER = 'toon_finish_owner'
PROFILE_KEY = 'toon_finish_node_profile'
ANIMATED_KEYS = ('highlight_strength', 'rim_strength')
NODE = 'toon_finish_node_id'
CHILD_OWNER = 'toon_finish_child_owner'
_INTERFACE = 'toon_finish_node_interface'
_NORMAL = 'toon_finish_node_normal'
_DEFAULTS = dict(highlight_strength=0., highlight_color=[1., .9, .75, 1.],
    highlight_threshold=.85, highlight_softness=.05, rim_strength=0.,
    rim_color=[.65, .75, 1., 1.], rim_power=5., rim_lift=0.,
    light_direction=[0., 0., 1.])
_RANGES = dict(highlight_strength=(0., 2.), highlight_threshold=(0., 1.),
    highlight_softness=(.001, 1.), rim_strength=(0., 2.), rim_power=(.1, 64.),
    rim_lift=(-1., 1.))
_COLORS = ('highlight_color', 'rim_color')


class FinishNodeError(ValueError):
    """Invalid parameters or changed owned node/reader identity."""


def _need(condition, message):
    if not condition:
        raise FinishNodeError(message)


def _number(value, name):
    _need(type(value) in (int, float) and math.isfinite(value),
          name + ' must be a finite number')
    return float(value)


def _owner(value):
    _need(type(value) is str, 'Finish owner must be a UUID string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise FinishNodeError('Finish owner must be a UUID string') from error
    _need(parsed.int != 0, 'Finish owner must be nonzero')
    return value


def defaults():
    return {key: list(value) if isinstance(value, list) else value
            for key, value in _DEFAULTS.items()}


def normalize(settings):
    """Normalize the closed nine-field, static parameter document."""
    _need(type(settings) is dict and set(settings) == set(_DEFAULTS),
          'Finish settings require exactly: ' + ', '.join(_DEFAULTS))
    result = {}
    for key, (low, high) in _RANGES.items():
        value = _number(settings[key], key)
        _need(low <= value <= high, key + ' is outside its supported range')
        result[key] = value
    for key in _COLORS:
        value = settings[key]
        _need(type(value) in (list, tuple) and len(value) == 4,
              key + ' requires RGBA')
        channels = [_number(item, key) for item in value]
        _need(all(0. <= item <= 1. for item in channels) and channels[3] == 1.,
              key + ' channels require [0,1] and alpha exactly 1')
        result[key] = channels
    vector = settings['light_direction']
    _need(type(vector) in (list, tuple) and len(vector) == 3,
          'light_direction requires three world-space components')
    vector = [_number(item, 'light_direction') for item in vector]
    length = math.hypot(*vector)
    _need(math.isfinite(length) and length > 0.,
          'light_direction must have a finite nonzero length')
    result['light_direction'] = vector
    return {key: result[key] for key in _DEFAULTS}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, float):
        _need(math.isfinite(value), 'Nonfinite owned node value')
        return value
    if hasattr(value, 'name_full'):
        return [value.bl_rna.identifier, value.name_full,
                value.library.filepath if value.library else None]
    try:
        return [_plain(item) for item in value]
    except TypeError as error:
        raise FinishNodeError('Unsupported owned node value') from error


def _socket(sockets, identifier):
    found = [socket for socket in sockets if socket.identifier == identifier]
    _need(len(found) == 1, 'Expected one socket identifier: ' + identifier)
    return found[0]


def _node(group, key):
    found = [node for node in group.nodes if node.get(NODE) == key]
    _need(len(found) == 1, 'Owned finish node missing or duplicated: ' + key)
    node = found[0]
    _need(node.get(OWNER) == group.get(OWNER), 'Finish node owner changed')
    return node


def _group(group):
    import bpy
    _need(isinstance(group, bpy.types.ShaderNodeTree)
          and group.get(PROFILE_KEY) in (PROFILE, HAIR_PROFILE), 'Expected owned finish Group')
    _need(not group.library and not group.override_library and group.is_editable,
          'Finish Group must be local and editable')
    _owner(group.get(OWNER))
    return group


def _parameter_sockets(group, key):
    _group(group)
    _need(key in _DEFAULTS, 'Unknown finish parameter')
    node = _node(group, 'param:' + key)
    if key in _RANGES:
        _need(node.bl_idname == 'ShaderNodeValue', 'Scalar parameter node changed')
        return [_socket(node.outputs, 'Value')]
    if key in _COLORS:
        _need(node.bl_idname == 'ShaderNodeRGB', 'Color parameter node changed')
        return [_socket(node.outputs, 'Color')]
    _need(node.bl_idname == 'ShaderNodeCombineXYZ', 'Direction parameter node changed')
    return [_socket(node.inputs, axis) for axis in ('X', 'Y', 'Z')]


def parameter_socket(group, key):
    """Only these scalar output sockets are part of the animation seam."""
    _need(key in ANIMATED_KEYS, 'This finish parameter is not animation-enabled')
    return _parameter_sockets(group, key)[0]


def parameters(group):
    """Actual native (float32) values; never returns a stale declaration."""
    result = {}
    for key in _DEFAULTS:
        sockets = _parameter_sockets(group, key)
        result[key] = ([float(socket.default_value) for socket in sockets]
                       if key == 'light_direction' else
                       _plain(sockets[0].default_value))
    return normalize(result)


def _close(left, right):
    if isinstance(left, list):
        return isinstance(right, list) and len(left) == len(right) and all(
            _close(a, b) for a, b in zip(left, right))
    return abs(left - right) <= 4e-7 * max(1., abs(left), abs(right))


def write_parameters(group, settings):
    """Write only the nine parameter constants with exact native rollback."""
    desired = normalize(settings)
    old = {key: [_plain(socket.default_value)
                 for socket in _parameter_sockets(group, key)] for key in _DEFAULTS}
    try:
        for key, value in desired.items():
            sockets = _parameter_sockets(group, key)
            values = value if key == 'light_direction' else [value]
            for socket, native in zip(sockets, values):
                socket.default_value = native
        actual = parameters(group)
        _need(all(_close(actual[key], desired[key]) for key in _DEFAULTS),
              'Finish parameter readback differs from requested native values')
        return actual
    except Exception:
        for key, values in old.items():
            for socket, value in zip(_parameter_sockets(group, key), values):
                socket.default_value = value
        raise


def owned_children(group):
    """Only the independent reader created by this Group may be reclaimed."""
    _group(group)
    try:
        expected = json.loads(group[_NORMAL])
    except (KeyError, ValueError, TypeError) as error:
        raise FinishNodeError('Finish normal declaration changed') from error
    instances = [node for node in group.nodes if node.bl_idname == 'ShaderNodeGroup']
    if expected == {'mode': 'GEOMETRY'}:
        _need(not instances, 'Unexpected nested finish Group')
        return []
    _need(type(expected) is dict and set(expected) ==
          {'mode', 'target', 'modifier_uid', 'normal_owner'}
          and expected['mode'] == 'ARTISTIC', 'Invalid artistic-normal declaration')
    _need(len(instances) == 1 and instances[0] == _node(group, 'normal_reader'),
          'Independent normal reader instance changed')
    from . import artistic_normals as art
    child = instances[0].node_tree
    _need(child is not None and child.get(CHILD_OWNER) == group[OWNER]
          and child.get(art.PROFILE_KEY) == art._SHADER,
          'Independent normal reader ownership changed')
    target = child.get(art._TARGET)
    actual = dict(mode='ARTISTIC', target=_plain(target),
        modifier_uid=child.get(art._UID), normal_owner=child.get(art.OWNER))
    _need(actual == expected, 'Independent normal reader source identity changed')
    _need(art._signature(child) == child.get(art._SIGNATURE),
          'Independent normal reader graph was edited')
    _need(child.library is None and child.animation_data is None,
          'Independent normal reader must remain static and local')
    return [child]


def _reader_shape(child):
    """Supplement the legacy field-reader signature with mute/default state."""
    from .runtime_compat import signature_inputs
    nodes = []
    for node in child.nodes:
        nodes.append(dict(name=node.name, type=node.bl_idname, mute=node.mute,
            properties={key: _plain(getattr(node, key)) for key in
                ('operation', 'use_clamp', 'attribute_name', 'attribute_type', 'is_active_output')
                if hasattr(node, key)},
            inputs=[(socket.identifier, _plain(socket.default_value))
                    for socket in signature_inputs(node) if hasattr(socket, 'default_value')],
            outputs=[(socket.identifier, _plain(socket.default_value))
                     for socket in node.outputs if hasattr(socket, 'default_value')]))
    return dict(nodes=sorted(nodes, key=lambda row: row['name']),
        links=sorted([[link.from_node.name, link.from_socket.identifier,
                       link.to_node.name, link.to_socket.identifier] for link in child.links]))


def structure(group):
    """JSON-safe semantic shape; only nine declared parameter values vary.

    Material binding and native Action validation are the caller's contracts.
    Cosmetic node layout is deliberately omitted. Names remain because native
    FCurve paths use them. The new reader is signed, not an old reader output.
    """
    _group(group)
    parameter_values = set()
    for key in _DEFAULTS:
        parameter_values.update(socket.as_pointer() for socket in _parameter_sockets(group, key))
    children = owned_children(group)
    from . import finish_band as hair
    band_shape = hair._structure(group) if group.get(PROFILE_KEY) == HAIR_PROFILE else None
    if band_shape is not None:
        parameter_values.update(socket.as_pointer() for socket in hair._parameter_sockets(group).values())
    nodes = []
    ids = set()
    for node in group.nodes:
        identity = node.get(NODE)
        _need(type(identity) is str and identity not in ids and node.get(OWNER) == group[OWNER],
              'Foreign, untagged or duplicate finish node')
        ids.add(identity)
        properties = {key: _plain(getattr(node, key)) for key in
            ('operation', 'use_clamp', 'clamp_type', 'blend_type', 'data_type',
             'distribution', 'attribute_name', 'attribute_type', 'is_active_output')
            if hasattr(node, key)}
        sockets = {}
        for side, values in (('inputs', node.inputs), ('outputs', node.outputs)):
            sockets[side] = [dict(identifier=socket.identifier, type=socket.bl_idname,
                value=('PARAMETER' if socket.as_pointer() in parameter_values else
                       _plain(socket.default_value)) if hasattr(socket, 'default_value') else None)
                for socket in values]
        nodes.append(dict(id=identity, name=node.name, type=node.bl_idname,
                          mute=node.mute, properties=properties, sockets=sockets))
    interface = []
    for item in group.interface.items_tree:
        _need(item.item_type == 'SOCKET', 'Unexpected finish interface panel')
        interface.append({key: _plain(getattr(item, key)) for key in
            ('identifier', 'name', 'in_out', 'socket_type', 'default_value', 'min_value', 'max_value')
            if hasattr(item, key)})
    from . import artistic_normals as art
    result = dict(profile=group[PROFILE_KEY], owner=group[OWNER],
        interface=interface, interface_map=group.get(_INTERFACE),
        normal=group[_NORMAL], nodes=sorted(nodes, key=lambda row: row['id']),
        links=sorted([[link.from_node.get(NODE), link.from_socket.identifier,
                       link.to_node.get(NODE), link.to_socket.identifier] for link in group.links]),
        children=[dict(name=child.name, owner=child[CHILD_OWNER],
                       signature=art._signature(child), shape=_reader_shape(child)) for child in children])
    if band_shape is not None:
        result['band'] = band_shape
    from . import finish_band_mask as mask
    if mask.present(group):
        result['band_mask']=mask._structure(group)
    return result


def build(owner, settings, normal_modifier=None, *, band=None, band_reference=None):
    """Create an unbound kernel; band mode temporarily owns reference UUID metadata.

    No original mesh, material, normal reader or Action edits. Reclaim band
    reference metadata only after deleting the final owning Group.
    """
    import bpy
    owner = _owner(owner)
    settings = normalize(settings)
    from . import finish_band as hair
    if band is not None:
        band = hair.normalize(band)
        band_reference = hair._check_reference(band_reference, getattr(normal_modifier, 'id_data', None))
    else:
        _need(band_reference is None, 'A band reference requires band settings')
    group = bpy.data.node_groups.new('Toon CEL Finish ' + owner[:8], 'ShaderNodeTree')
    children = []
    band_token, band_token_created = None, False
    try:
        if band is not None:
            band_token, band_token_created = hair._acquire_reference(band_reference)
            group[hair._REF_ID] = band_token
        group.use_fake_user = False
        group[PROFILE_KEY], group[OWNER] = (HAIR_PROFILE if band is not None else PROFILE), owner
        base = group.interface.new_socket(name='Base Shader', in_out='INPUT', socket_type='NodeSocketShader')
        result = group.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
        group[_INTERFACE] = _json(dict(base=base.identifier, shader=result.identifier))
        def node(key, kind, **properties):
            value = group.nodes.new(kind)
            value.name = 'ToonFinish/' + key
            value.label = key
            value[NODE], value[OWNER] = key, owner
            for name, prop in properties.items():
                setattr(value, name, prop)
            return value
        def link(source, destination):
            group.links.new(source, destination)
        def math_(key, operation, a, b=None):
            value = node(key, 'ShaderNodeMath', operation=operation, use_clamp=False)
            for identifier, source in (('Value', a), ('Value_001', b)):
                if source is None:
                    continue
                socket = _socket(value.inputs, identifier)
                if type(source) in (int, float):
                    socket.default_value = source
                else:
                    link(source, socket)
            return _socket(value.outputs, 'Value')
        def vector(key, operation, a, b=None):
            value = node(key, 'ShaderNodeVectorMath', operation=operation)
            link(a, _socket(value.inputs, 'Vector'))
            if b is not None:
                link(b, _socket(value.inputs, 'Scale' if operation == 'SCALE' else 'Vector_001'))
            return _socket(value.outputs, 'Value' if operation == 'DOT_PRODUCT' else 'Vector')
        def clamp(key, source):
            return math_(key + ':upper', 'MINIMUM', math_(key + ':lower', 'MAXIMUM', source, 0.), 1.)
        params = {}
        for key in _DEFAULTS:
            kind = 'ShaderNodeRGB' if key in _COLORS else 'ShaderNodeCombineXYZ' if key == 'light_direction' else 'ShaderNodeValue'
            value = node('param:' + key, kind)
            params[key] = _socket(value.outputs, 'Color' if key in _COLORS else 'Vector' if key == 'light_direction' else 'Value')
        incoming = node('input', 'NodeGroupInput')
        outgoing = node('output', 'NodeGroupOutput', is_active_output=True)
        geometry = node('geometry', 'ShaderNodeNewGeometry')
        if normal_modifier is None:
            group[_NORMAL] = _json(dict(mode='GEOMETRY'))
            source_normal = _socket(geometry.outputs, 'Normal')
        else:
            from . import artistic_normals as art
            child = art.shader_group(normal_modifier)
            children.append(child)
            child[CHILD_OWNER] = owner
            value = node('normal_reader', 'ShaderNodeGroup')
            value.node_tree = child
            reader_outputs = [item for item in child.interface.items_tree
                              if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT']
            _need(child.get(art._INTERFACE) == 'world-normal-output.v1'
                  and len(reader_outputs) == 1
                  and reader_outputs[0].socket_type == 'NodeSocketVector',
                  'Independent ART reader output interface changed')
            source_normal = _socket(value.outputs, reader_outputs[0].identifier)
            group[_NORMAL] = _json(dict(mode='ARTISTIC', target=_plain(child.get(art._TARGET)),
                modifier_uid=child.get(art._UID), normal_owner=child.get(art.OWNER)))
        n = vector('normal_unit', 'NORMALIZE', source_normal)
        v = vector('view_unit', 'NORMALIZE', _socket(geometry.outputs, 'Incoming'))
        light = vector('light_unit', 'NORMALIZE', params['light_direction'])
        half = vector('half_unit', 'NORMALIZE', vector('half_sum', 'ADD', light, v))
        dot = vector('half_dot', 'DOT_PRODUCT', n, half)
        linear = clamp('highlight_range', math_('highlight_width', 'DIVIDE',
            math_('highlight_shift', 'SUBTRACT', dot, params['highlight_threshold']), params['highlight_softness']))
        squared = math_('highlight_squared', 'MULTIPLY', linear, linear)
        smooth = math_('highlight_smooth', 'MULTIPLY', squared,
            math_('highlight_polynomial', 'SUBTRACT', 3., math_('highlight_double', 'MULTIPLY', linear, 2.)))
        front = math_('light_front', 'MAXIMUM', vector('light_dot', 'DOT_PRODUCT', n, light), 0.)
        highlight = math_('highlight_weight', 'MULTIPLY', params['highlight_strength'],
            math_('highlight_lit', 'MULTIPLY', smooth, front))
        rim_base = clamp('rim_range', math_('rim_lift', 'ADD',
            math_('rim_inverse_facing', 'SUBTRACT', 1., vector('view_dot', 'DOT_PRODUCT', n, v)), params['rim_lift']))
        rim = math_('rim_weight', 'MULTIPLY', params['rim_strength'],
            math_('rim_power', 'POWER', rim_base, params['rim_power']))
        color = vector('finish_color', 'ADD', vector('highlight_color', 'SCALE', params['highlight_color'], highlight),
                       vector('rim_color', 'SCALE', params['rim_color'], rim))
        emission = node('emission', 'ShaderNodeEmission')
        _socket(emission.inputs, 'Strength').default_value = 1.
        link(color, _socket(emission.inputs, 'Color'))
        add = node('add', 'ShaderNodeAddShader')
        # Both shader inputs share the same display name; identifiers distinguish them.
        link(_socket(incoming.outputs, base.identifier), _socket(add.inputs, 'Shader'))
        link(_socket(emission.outputs, 'Emission'), _socket(add.inputs, 'Shader_001'))
        link(_socket(add.outputs, 'Shader'), _socket(outgoing.inputs, result.identifier))
        write_parameters(group, settings)
        if band is not None:
            hair._build(group, band, band_reference)
        structure(group)
        for index, value in enumerate(group.nodes):
            value.location = ((index % 7) * 220., -(index // 7) * 180.)
        return group
    except Exception:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        for child in reversed(children):
            if child.users == 0:
                bpy.data.node_groups.remove(child)
        if band_token_created:
            hair._cleanup_reference(band_reference, band_token)
        raise
