# SPDX-License-Identifier: GPL-3.0-or-later
"""Unbound, static HM color kernel for the unified Toon Workflow.

G changes the directional light/shadow boundary and R changes the highlight
threshold.  A separately lit Diffuse closure supplies an illumination gate;
that gate is an approximation under the caller's admitted lighting conditions,
not a general per-light visibility buffer.  B and existing finish layers are
outside this module.  The caller owns the material seam, Normal input, Image
consumers, Sun carrier and their render-time validation.
"""
import json
import math
import struct
import uuid

PROFILE = 'toon-hair-map-node.v1'
PROFILE_KEY = 'toon_hm_node_profile'
OWNER = 'toon_hm_owner'
NODE = 'toon_hm_node'
INTERFACE = 'toon_hm_node_interface'
_INTERFACE = INTERFACE

_DEFAULTS = dict(
    strength=0., map_strength=1., neutral=.5,
    shadow_bias=0., shadow_softness=.05,
    highlight_strength=0., highlight_threshold=.85,
    highlight_map_strength=.2, highlight_softness=.05,
    highlight_color=[1., .85, .9, 1.],
    shadow_color=[.1, .05, .1, 1.], lit_color=[.8, .4, .6, 1.],
    irradiance_reference=1.)
_RANGES = dict(
    strength=(0., 1.), map_strength=(0., 4.), neutral=(0., 1.),
    shadow_bias=(-1., 1.), shadow_softness=(.001, 1.),
    highlight_strength=(0., 2.), highlight_threshold=(0., 1.),
    highlight_map_strength=(0., 1.), highlight_softness=(.001, 1.),
    irradiance_reference=(1.e-6, 1.e6))
_COLORS = ('highlight_color', 'shadow_color', 'lit_color')


class HairMapNodeError(ValueError):
    """Invalid HM constants or an altered owned kernel identity."""


def _need(condition, message):
    if not condition:
        raise HairMapNodeError(message)


def _number(value, name):
    _need(type(value) in (int, float) and math.isfinite(value),
          name + ' must be a finite number')
    return float(value)


def _owner(value):
    _need(type(value) is str, 'HM owner must be a UUID string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise HairMapNodeError('HM owner must be a UUID string') from error
    _need(parsed.int != 0, 'HM owner must be nonzero')
    return value


def _name(value, label):
    _need(type(value) is str and bool(value.strip()) and '\x00' not in value,
          label + ' must be an explicit nonempty string')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def defaults():
    """Fresh values; callers may edit color lists without altering defaults."""
    return {key: list(value) if isinstance(value, list) else value
            for key, value in _DEFAULTS.items()}


def normalize(settings):
    """Validate the closed, opaque, finite static parameter document."""
    _need(type(settings) is dict and set(settings) == set(_DEFAULTS),
          'HM settings require exactly: ' + ', '.join(_DEFAULTS))
    result = {}
    for key, (low, high) in _RANGES.items():
        value = _number(settings[key], key)
        # RNA stores float32.  Admit each exact native endpoint as well as the
        # mathematical interval, so a requested 1e-6 denominator can be read
        # back without treating its one-ULP rounding as a malformed value.
        endpoints = (struct.unpack('<f', struct.pack('<f', edge))[0]
                     for edge in (low, high))
        _need(low <= value <= high or value in endpoints,
              key + ' is outside its supported range')
        result[key] = value
    for key in _COLORS:
        value = settings[key]
        _need(type(value) in (list, tuple) and len(value) == 4,
              key + ' requires RGBA')
        channels = [_number(item, key) for item in value]
        _need(all(0. <= item <= 1. for item in channels) and channels[3] == 1.,
              key + ' requires [0,1] channels and alpha exactly 1')
        result[key] = channels
    return {key: result[key] for key in _DEFAULTS}


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, float):
        _need(math.isfinite(value), 'Nonfinite owned HM value')
        return float(value)
    if hasattr(value, 'name_full'):
        return [value.bl_rna.identifier, value.name_full,
                value.library.filepath if value.library else None]
    try:
        return [_plain(item) for item in value]
    except TypeError as error:
        raise HairMapNodeError('Unsupported owned HM value') from error


def _socket(sockets, identifier):
    found = [socket for socket in sockets if socket.identifier == identifier]
    _need(len(found) == 1, 'Expected one HM socket identifier: ' + identifier)
    return found[0]


def _group(group):
    import bpy
    _need(isinstance(group, bpy.types.ShaderNodeTree)
          and group.get(PROFILE_KEY) == PROFILE, 'Expected owned HM Shader Group')
    _need(not group.library and not group.override_library and group.is_editable
          and group.animation_data is None, 'HM Group must remain static, local and editable')
    _owner(group.get(OWNER))
    return group


def _node(group, key):
    found = [node for node in group.nodes if node.get(NODE) == key]
    _need(len(found) == 1 and found[0].get(OWNER) == group.get(OWNER),
          'Owned HM node is missing, duplicated or has a different owner: ' + key)
    return found[0]


def _parameter_socket(group, key):
    _group(group)
    _need(key in _DEFAULTS, 'Unknown HM parameter')
    node = _node(group, 'param:' + key)
    expected = 'ShaderNodeRGB' if key in _COLORS else 'ShaderNodeValue'
    _need(node.bl_idname == expected, 'HM parameter node type changed: ' + key)
    return _socket(node.outputs, 'Color' if key in _COLORS else 'Value')


def parameters(group):
    """Read actual native constants, rather than a cached declaration."""
    return normalize({key: _plain(_parameter_socket(group, key).default_value)
                      for key in _DEFAULTS})


def _close(left, right):
    if isinstance(left, list):
        return isinstance(right, list) and len(left) == len(right) and all(
            _close(a, b) for a, b in zip(left, right))
    return abs(left - right) <= 4.e-7 * max(1., abs(left), abs(right))


def write_parameters(group, settings):
    """Write this kernel's constants only, with native-value rollback."""
    desired = normalize(settings)
    sockets = {key: _parameter_socket(group, key) for key in _DEFAULTS}
    before = {key: _plain(socket.default_value) for key, socket in sockets.items()}
    try:
        for key, value in desired.items():
            sockets[key].default_value = value
        actual = parameters(group)
        _need(all(_close(actual[key], desired[key]) for key in _DEFAULTS),
              'HM native parameter readback differs from the request')
        return actual
    except Exception:
        for key, value in before.items():
            sockets[key].default_value = value
        raise


def _mapping(value, fields):
    return {key: _plain(getattr(value, key)) for key in fields if hasattr(value, key)}


def structure(group):
    """JSON-safe semantic graph, including current constants and references.

    Layout and labels are cosmetic.  Constants are deliberately included, so
    an authorized parameter update must refresh the caller's declaration and
    shape together; ordinary native hand edits cannot silently pass a hash.
    """
    from .runtime_compat import signature_inputs
    _group(group)
    parameters(group)
    nodes, seen = [], set()
    _need(len(group.nodes) <= 256, 'HM Group exceeds its bounded node inventory')
    for node in group.nodes:
        identity = node.get(NODE)
        _need(type(identity) is str and identity not in seen
              and node.get(OWNER) == group[OWNER],
              'Foreign, untagged or duplicate HM node')
        _need(node.bl_idname != 'ShaderNodeGroup', 'Nested HM Groups are outside this kernel')
        seen.add(identity)
        properties = _mapping(node, (
            'operation', 'use_clamp', 'clamp_type', 'blend_type', 'use_alpha',
            'distribution', 'attribute_name', 'attribute_type', 'uv_map',
            'from_instancer', 'interpolation', 'extension', 'projection',
            'projection_blend', 'is_active_output', 'mode'))
        row = dict(id=identity, name=node.name, type=node.bl_idname,
                   mute=node.mute, properties=properties)
        for side, sockets in (('inputs', signature_inputs(node)), ('outputs', node.outputs)):
            row[side] = [dict(identifier=socket.identifier, type=socket.bl_idname,
                             value=_plain(socket.default_value)
                             if hasattr(socket, 'default_value') else None)
                         for socket in sockets]
        if hasattr(node, 'image'):
            row['image'] = _plain(node.image)
            if hasattr(node, 'image_user'):
                row['image_user'] = _mapping(node.image_user, (
                    'frame_duration', 'frame_start', 'frame_offset',
                    'use_cyclic', 'use_auto_refresh', 'tile'))
            if hasattr(node, 'texture_mapping'):
                row['texture_mapping'] = _mapping(node.texture_mapping, (
                    'vector_type', 'translation', 'rotation', 'scale',
                    'min', 'max', 'use_min', 'use_max',
                    'mapping_x', 'mapping_y', 'mapping_z'))
            if hasattr(node, 'color_mapping'):
                row['color_mapping'] = _mapping(node.color_mapping, (
                    'brightness', 'contrast', 'saturation', 'blend_type',
                    'blend_factor', 'blend_color', 'use_color_ramp'))
                ramp = getattr(node.color_mapping, 'color_ramp', None)
                if ramp is not None:
                    row['color_mapping']['ramp'] = dict(
                        interpolation=ramp.interpolation, color_mode=ramp.color_mode,
                        hue_interpolation=ramp.hue_interpolation,
                        elements=[dict(position=element.position, color=list(element.color))
                                  for element in ramp.elements])
        nodes.append(row)
    interface = []
    for item in group.interface.items_tree:
        _need(item.item_type == 'SOCKET', 'Unexpected HM interface panel')
        interface.append(_mapping(item, (
            'identifier', 'name', 'in_out', 'socket_type',
            'default_value', 'min_value', 'max_value', 'hide_value')))
    return dict(profile=group[PROFILE_KEY], owner=group[OWNER],
                interface=interface, interface_map=group.get(INTERFACE),
                nodes=sorted(nodes, key=lambda row: row['id']),
                links=sorted([[link.from_node.get(NODE), link.from_socket.identifier,
                               link.to_node.get(NODE), link.to_socket.identifier]
                              for link in group.links]))


def build(owner, settings, image, uv_name, direction_attribute, normal_reader=None):
    """Create an unbound HM color Group without editing any source data.

    The explicit Normal input must be linked by the caller to Geometry.Normal
    or an already qualified independent world-normal reader.  Image content,
    Non-Color sampling, UV existence and illumination-reference admission are
    validated by that caller.  This version does not create nested readers.
    """
    import bpy
    owner = _owner(owner)
    settings = normalize(settings)
    _name(uv_name, 'HM UV name')
    _name(direction_attribute, 'HM direction attribute')
    _need(normal_reader is None, 'This HM kernel requires its explicit external Normal input')
    _need(isinstance(image, bpy.types.Image), 'HM requires an explicit Image')
    group = bpy.data.node_groups.new('Toon Hair Map ' + owner[:8], 'ShaderNodeTree')
    try:
        group.use_fake_user = False
        group[PROFILE_KEY], group[OWNER] = PROFILE, owner
        original = group.interface.new_socket(
            name='Original Color', in_out='INPUT', socket_type='NodeSocketColor')
        normal_input = group.interface.new_socket(
            name='Normal', in_out='INPUT', socket_type='NodeSocketVector')
        normal_input.hide_value = True
        output = group.interface.new_socket(
            name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')
        group[INTERFACE] = _json(dict(original=original.identifier,
                                    normal=normal_input.identifier, color=output.identifier))

        def node(key, kind, **properties):
            value = group.nodes.new(kind)
            value.name, value.label = 'ToonHM/' + key, key
            value[NODE], value[OWNER] = key, owner
            for key_, prop in properties.items():
                setattr(value, key_, prop)
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
            return math_(key + ':upper', 'MINIMUM',
                         math_(key + ':lower', 'MAXIMUM', source, 0.), 1.)

        def smoothstep(key, low, high, source):
            width = math_(key + ':width', 'SUBTRACT', high, low)
            unit = clamp(key + ':unit', math_(key + ':divide', 'DIVIDE',
                         math_(key + ':shift', 'SUBTRACT', source, low), width))
            squared = math_(key + ':square', 'MULTIPLY', unit, unit)
            polynomial = math_(key + ':polynomial', 'SUBTRACT', 3.,
                               math_(key + ':double', 'MULTIPLY', unit, 2.))
            return math_(key + ':smooth', 'MULTIPLY', squared, polynomial)

        def mix(key, factor, a, b):
            value = node(key, 'ShaderNodeMixRGB', blend_type='MIX',
                         use_clamp=False, use_alpha=False)
            link(factor, _socket(value.inputs, 'Fac'))
            link(a, _socket(value.inputs, 'Color1'))
            link(b, _socket(value.inputs, 'Color2'))
            return _socket(value.outputs, 'Color')

        params = {}
        for key in _DEFAULTS:
            value = node('param:' + key,
                         'ShaderNodeRGB' if key in _COLORS else 'ShaderNodeValue')
            params[key] = _socket(value.outputs, 'Color' if key in _COLORS else 'Value')
        incoming = node('input', 'NodeGroupInput')
        outgoing = node('output', 'NodeGroupOutput', is_active_output=True)
        geometry = node('geometry', 'ShaderNodeNewGeometry')
        direction = node('sun_direction', 'ShaderNodeAttribute',
                         attribute_type='GEOMETRY', attribute_name=direction_attribute)
        uv = node('uv', 'ShaderNodeUVMap', uv_map=uv_name, from_instancer=False)
        texture = node('texture', 'ShaderNodeTexImage', interpolation='Linear',
                       extension='EXTEND', projection='FLAT', projection_blend=0.)
        texture.image = image
        separate = node('channels', 'ShaderNodeSeparateColor', mode='RGB')
        link(_socket(uv.outputs, 'UV'), _socket(texture.inputs, 'Vector'))
        link(_socket(texture.outputs, 'Color'), _socket(separate.inputs, 'Color'))
        n = vector('normal_unit', 'NORMALIZE', _socket(incoming.outputs, normal_input.identifier))
        light = vector('light_unit', 'NORMALIZE', _socket(direction.outputs, 'Vector'))
        view = vector('view_unit', 'NORMALIZE', _socket(geometry.outputs, 'Incoming'))
        half = vector('half_unit', 'NORMALIZE', vector('half_sum', 'ADD', light, view))
        dot = vector('light_dot', 'DOT_PRODUCT', n, light)
        green_shift = math_('green_shift', 'MULTIPLY',
                           math_('green_neutral', 'SUBTRACT',
                                 _socket(separate.outputs, 'Green'), params['neutral']),
                           params['map_strength'])
        lit_raw = smoothstep('shadow_boundary',
                             math_('shadow_low', 'SUBTRACT', params['shadow_bias'], params['shadow_softness']),
                             math_('shadow_high', 'ADD', params['shadow_bias'], params['shadow_softness']),
                             math_('shadow_response', 'ADD', dot, green_shift))

        sensor = node('illumination_sensor', 'ShaderNodeBsdfDiffuse')
        _socket(sensor.inputs, 'Color').default_value = (1., 1., 1., 1.)
        _socket(sensor.inputs, 'Roughness').default_value = 0.
        link(light, _socket(sensor.inputs, 'Normal'))
        sensor_rgb = node('illumination_rgb', 'ShaderNodeShaderToRGB')
        sensor_bw = node('illumination_luminance', 'ShaderNodeRGBToBW')
        link(_socket(sensor.outputs, 'BSDF'), _socket(sensor_rgb.inputs, 'Shader'))
        link(_socket(sensor_rgb.outputs, 'Color'), _socket(sensor_bw.inputs, 'Color'))
        gate = clamp('illumination_gate', math_('illumination_reference', 'DIVIDE',
                     _socket(sensor_bw.outputs, 'Val'), params['irradiance_reference']))
        lit = math_('lit_with_illumination', 'MULTIPLY', lit_raw, gate)
        palette = mix('palette', lit, params['shadow_color'], params['lit_color'])
        threshold = math_('red_threshold', 'ADD', params['highlight_threshold'],
                          math_('red_threshold_shift', 'MULTIPLY',
                                math_('red_inverse', 'SUBTRACT', 1.,
                                      _socket(separate.outputs, 'Red')),
                                params['highlight_map_strength']))
        highlight = smoothstep('highlight_boundary', threshold,
                               math_('highlight_high', 'ADD', threshold, params['highlight_softness']),
                               vector('half_dot', 'DOT_PRODUCT', n, half))
        highlight = math_('highlight_front', 'MULTIPLY', highlight,
                          math_('front', 'MAXIMUM', dot, 0.))
        highlight = math_('highlight_illumination', 'MULTIPLY', highlight, gate)
        highlight = math_('highlight_lit', 'MULTIPLY', highlight, lit_raw)
        highlight = math_('highlight_strength', 'MULTIPLY', highlight, params['highlight_strength'])
        highlight_color = vector('highlight_color', 'SCALE', params['highlight_color'], highlight)
        hm_color = vector('hm_color', 'ADD', palette, highlight_color)
        result = mix('whole_effect', params['strength'],
                     _socket(incoming.outputs, original.identifier), hm_color)
        link(result, _socket(outgoing.inputs, output.identifier))
        write_parameters(group, settings)
        structure(group)
        for index, value in enumerate(group.nodes):
            value.location = ((index % 8) * 220., -(index // 8) * 190.)
        return group
    except BaseException:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        raise
