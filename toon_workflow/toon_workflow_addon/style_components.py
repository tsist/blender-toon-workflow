"""Original, reusable GAME/MANGA shader components for the P5 style study.

This module only creates a new Material and its own node graphs.  It never
edits an existing production material, surface_slots identity, object, scene,
Action or render setting.  GAME reuses the existing Toon Workflow eevee_cel3
core and finish_nodes Blinn/rim kernel; MANGA is a screen-space regular
screentone/crosshatch graph whose visible Surface is a single Emission.

Public API
----------
normalize(settings) -> canonical deep-copied settings dict
build(bpy, settings, name) -> new Material
inspect(material) -> {status, settings, issues} plus rich diagnostics (read-only)
update(material, settings) -> inspect() result after a guarded parameter update

Schema: toon-style-components.v1.  Styles: GAME, MANGA.

Coordinate semantics are explicit:
* MANGA WINDOW uses Texture Coordinate.Window (0..1) and the declared render
  resolution.  Pixel coordinates are (window.x*res_x, window.y*res_y), so a
  regular dot cell has equal width and height in pixels on any aspect.
* MANGA UV uses the named UV Map node; MANGA OBJECT uses local Object
  coordinates.  Neither is automatically matched to the screen and neither is
  claimed to be UV-seam free.
* GAME light_direction is an explicit world-space art direction, not a live
  light binding.  Changing it in a settings document requires update().
* GAME reflection_enabled is false by default; a Mix Shader gate bypasses the
  GGX path until it is explicitly enabled.  Its strength is independently
  controlled and defaults to zero.
* Material ID properties outside the toon_style_component_ prefix are ignored
  for graph validity; node, interface and content edits still reject.
"""

import hashlib
import json
import math
import uuid

SCHEMA = 'toon-style-components.v1'
STYLES = ('GAME', 'MANGA')

PROFILE_KEY = 'toon_style_component_profile'
PROFILE_PREFIX = 'toon_style_component_'
OWNER_KEY = 'toon_style_component_owner'
STYLE_KEY = 'toon_style_component_style'
DECLARED_KEY = 'toon_style_component_declared'
SIGNATURE_KEY = 'toon_style_component_signature'
GENERATION_KEY = 'toon_style_component_generation'
NODE_KEY = 'toon_style_component_node'
NODE_OWNER_KEY = 'toon_style_component_node_owner'
NOTE_KEY = 'toon_style_component_note'

_FINISH_NODE_KEY = 'toon_finish_node_id'
_FINISH_PROFILE_KEY = 'toon_finish_node_profile'
_FINISH_PROFILE = 'toon-finish-node.v1'
_FINISH_HAIR_PROFILE = 'toon-finish-node.band.v1'
_MANAGED_NODE_KEY = '_toon_node_id'
_MANAGED_ROLE_KEY = '_toon_role'
_MANAGED_CORE_ID = '_toon_core_id'

_GAME_COLOR_FIELDS = (
    'base_color', 'shadow_color', 'mid_color', 'lit_color',
    'highlight_color', 'rim_color', 'reflection_color',
)
_MANGA_SCALAR_FIELDS = (
    'paper_tone', 'ink_tone', 'black_below', 'hatch_below', 'dot_below',
    'light_gain', 'light_bias', 'hatch_spacing', 'hatch_angle_deg',
    'hatch_width_deep', 'hatch_width_light', 'dot_spacing',
    'dot_radius_dark', 'dot_radius_light',
)
_GAME_DEFAULTS = {
    'base_color': [0.62, 0.60, 0.56, 1.0],
    'shadow_color': [0.045, 0.065, 0.115, 1.0],
    'mid_color': [0.22, 0.38, 0.48, 1.0],
    'lit_color': [0.82, 0.84, 0.80, 1.0],
    'threshold_low': 0.16,
    'threshold_high': 0.48,
    'highlight_color': [1.0, 0.94, 0.80, 1.0],
    'highlight_strength': 0.35,
    'highlight_threshold': 0.90,
    'highlight_softness': 0.03,
    'rim_color': [0.46, 0.66, 1.0, 1.0],
    'rim_strength': 0.40,
    'rim_power': 5.0,
    'rim_lift': 0.0,
    'light_direction': [-0.52, -0.52, 0.67],
    'reflection_color': [0.62, 0.70, 0.84, 1.0],
    'reflection_enabled': False,
    'reflection_strength': 0.0,
    'reflection_roughness': 0.42,
}
_MANGA_DEFAULTS = {
    'pattern_space': 'WINDOW',
    'uv_map': None,
    'resolution': [960, 540],
    'paper_tone': 0.94,
    'ink_tone': 0.04,
    'black_below': 0.10,
    'hatch_below': 0.32,
    'dot_below': 0.82,
    'light_gain': 0.15,
    'light_bias': 0.0,
    'hatch_spacing': 7.0,
    'hatch_angle_deg': 45.0,
    'hatch_width_deep': 0.42,
    'hatch_width_light': 0.16,
    'dot_spacing': 18.0,
    'dot_radius_dark': 0.38,
    'dot_radius_light': 0.10,
}
_GAME_FIELDS = tuple(_GAME_DEFAULTS)
_MANGA_FIELDS = tuple(_MANGA_DEFAULTS)

_GAME_BASE_SOCKETS = {
    'base_color': 'Base Color',
    'shadow_color': 'Shadow Color',
    'mid_color': 'Mid Color',
    'lit_color': 'Lit Color',
    'threshold_low': 'Threshold Low',
    'threshold_high': 'Threshold High',
}
_FINISH_FIELDS = (
    'highlight_strength', 'highlight_color', 'highlight_threshold',
    'highlight_softness', 'rim_strength', 'rim_color', 'rim_power',
    'rim_lift', 'light_direction',
)


class StyleComponentError(ValueError):
    """Invalid request, foreign material, or protected author edit."""


def _need(condition, message):
    if not condition:
        raise StyleComponentError(message)


def _number(value, name):
    _need(type(value) in (int, float) and math.isfinite(value),
          name + ' must be a finite number')
    return float(value)


def _integer(value, name):
    _need(type(value) is int, name + ' must be an integer')
    return int(value)


def _rgba(value, name):
    _need(type(value) in (list, tuple) and len(value) == 4,
          name + ' requires RGBA')
    result = [_number(item, name) for item in value]
    _need(all(0.0 <= item <= 1.0 for item in result) and result[3] == 1.0,
          name + ' channels require [0,1] and alpha exactly 1')
    return result


def _vector(value, name):
    _need(type(value) in (list, tuple) and len(value) == 3,
          name + ' requires three components')
    result = [_number(item, name) for item in value]
    _need(math.hypot(*result) > 0.0, name + ' must be nonzero')
    return result


def _uuid_text(value, name):
    _need(type(value) is str, name + ' must be a UUID string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise StyleComponentError(name + ' must be a UUID string') from error
    _need(parsed.int != 0, name + ' must be nonzero')
    return value


def defaults(style):
    """Return a complete settings document for one supported style."""
    _need(style in STYLES, 'Style must be GAME or MANGA')
    source = _GAME_DEFAULTS if style == 'GAME' else _MANGA_DEFAULTS
    result = {'schema': SCHEMA, 'style': style}
    for key, value in source.items():
        result[key] = list(value) if isinstance(value, list) else value
    return result


def normalize(settings):
    """Validate a closed settings document and return canonical values.

    Missing or unknown fields are rejected; every numeric value is finite.
    Defaults are available separately through defaults(style).
    """
    _need(type(settings) is dict, 'settings must be a dictionary')
    _need(settings.get('schema') == SCHEMA,
          'settings.schema must be ' + SCHEMA)
    style = settings.get('style')
    _need(style in STYLES, 'settings.style must be GAME or MANGA')
    expected = {'schema', 'style'} | set(_GAME_FIELDS if style == 'GAME' else _MANGA_FIELDS)
    unknown = sorted(set(settings) - expected)
    missing = sorted(expected - set(settings))
    _need(not unknown, 'Unknown settings fields: ' + ', '.join(unknown))
    _need(not missing, 'Missing settings fields: ' + ', '.join(missing))
    if style == 'GAME':
        return _normalize_game(settings)
    return _normalize_manga(settings)


def _normalize_game(settings):
    result = {'schema': SCHEMA, 'style': 'GAME'}
    for key in _GAME_COLOR_FIELDS:
        result[key] = _rgba(settings[key], key)
    for key in ('threshold_low', 'threshold_high'):
        result[key] = _number(settings[key], key)
        _need(0.0 <= result[key] <= 1.0, key + ' must be in [0,1]')
    _need(result['threshold_low'] < result['threshold_high'],
          'threshold_low must be less than threshold_high')
    ranges = {
        'highlight_strength': (0.0, 2.0),
        'highlight_threshold': (0.0, 1.0),
        'highlight_softness': (0.001, 1.0),
        'rim_strength': (0.0, 2.0),
        'rim_power': (0.1, 64.0),
        'rim_lift': (-1.0, 1.0),
        'reflection_strength': (0.0, 2.0),
        'reflection_roughness': (0.02, 1.0),
    }
    for key, (low, high) in ranges.items():
        result[key] = _number(settings[key], key)
        _need(low <= result[key] <= high,
              key + ' is outside [' + str(low) + ',' + str(high) + ']')
    _need(type(settings['reflection_enabled']) is bool,
          'reflection_enabled must be boolean')
    result['reflection_enabled'] = settings['reflection_enabled']
    result['light_direction'] = _vector(settings['light_direction'], 'light_direction')
    return {key: result[key] for key in ('schema', 'style') + _GAME_FIELDS}


def _normalize_manga(settings):
    result = {'schema': SCHEMA, 'style': 'MANGA'}
    space = settings['pattern_space']
    _need(space in ('WINDOW', 'UV', 'OBJECT'),
          'pattern_space must be WINDOW, UV or OBJECT')
    result['pattern_space'] = space
    uv_map = settings['uv_map']
    if space == 'UV':
        _need(type(uv_map) is str and bool(uv_map.strip()),
              'uv_map requires a nonempty layer name in UV space')
        result['uv_map'] = uv_map
    else:
        _need(uv_map is None, 'uv_map must be null outside UV space')
        result['uv_map'] = None
    resolution = settings['resolution']
    if space == 'WINDOW':
        _need(type(resolution) in (list, tuple) and len(resolution) == 2,
              'resolution requires [x,y] in WINDOW space')
        result['resolution'] = [_integer(resolution[0], 'resolution.x'),
                                _integer(resolution[1], 'resolution.y')]
        _need(all(16 <= item <= 16384 for item in result['resolution']),
              'WINDOW resolution components must be in [16,16384]')
    else:
        _need(resolution is None, 'resolution must be null outside WINDOW space')
        result['resolution'] = None
    for key in _MANGA_SCALAR_FIELDS:
        result[key] = _number(settings[key], key)
    _need(0.0 <= result['paper_tone'] <= 1.0, 'paper_tone must be in [0,1]')
    _need(0.0 <= result['ink_tone'] <= 1.0, 'ink_tone must be in [0,1]')
    _need(result['paper_tone'] >= result['ink_tone'],
          'paper_tone must be greater than or equal to ink_tone')
    for key in ('black_below', 'hatch_below', 'dot_below'):
        _need(0.0 <= result[key] <= 1.0, key + ' must be in [0,1]')
    _need(result['black_below'] < result['hatch_below'] < result['dot_below'],
          'thresholds require black_below < hatch_below < dot_below')
    _need(0.05 <= result['light_gain'] <= 8.0, 'light_gain must be in [0.05,8]')
    _need(-1.0 <= result['light_bias'] <= 1.0, 'light_bias must be in [-1,1]')
    spacing_range = (2.0, 256.0) if space == 'WINDOW' else (0.0005, 4.0)
    for key in ('hatch_spacing', 'dot_spacing'):
        _need(spacing_range[0] <= result[key] <= spacing_range[1],
              key + ' is outside its ' + space + ' range')
    _need(1.0 <= result['hatch_angle_deg'] <= 89.0,
          'hatch_angle_deg must be in [1,89]')
    for key in ('hatch_width_deep', 'hatch_width_light'):
        _need(0.01 <= result[key] <= 0.85, key + ' must be in [0.01,0.85]')
    _need(result['hatch_width_deep'] >= result['hatch_width_light'],
          'hatch_width_deep must be >= hatch_width_light')
    for key in ('dot_radius_dark', 'dot_radius_light'):
        _need(0.0 <= result[key] <= 0.5, key + ' must be in [0,0.5]')
    _need(result['dot_radius_dark'] >= result['dot_radius_light'],
          'dot_radius_dark must be >= dot_radius_light')
    return {key: result[key] for key in ('schema', 'style') + _MANGA_FIELDS}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, float):
        _need(math.isfinite(value), 'Nonfinite node value')
        return float(value)
    if hasattr(value, '__len__') and not isinstance(value, str):
        return [_plain(item) for item in value]
    raise StyleComponentError('Unsupported node value')


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _close(left, right):
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _close(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float):
        return abs(left - right) <= 1e-5 * max(1.0, abs(left), abs(right))
    return left == right


def _socket(sockets, identifier):
    found = [socket for socket in sockets if socket.identifier == identifier]
    _need(len(found) == 1, 'Expected one socket identifier: ' + identifier)
    return found[0]


def _link(tree, source, target):
    tree.links.new(source, target)


def _own_node(tree, owner, key, kind, location=(0.0, 0.0), **properties):
    node = tree.nodes.new(kind)
    node.name = 'ToonStyle/' + key
    node.label = key
    node[NODE_KEY] = key
    node[NODE_OWNER_KEY] = owner
    node.location = location
    for name, value in properties.items():
        setattr(node, name, value)
    return node


def _param_value(tree, owner, key, value, location=(0.0, 0.0)):
    node = _own_node(tree, owner, 'param:' + key, 'ShaderNodeValue', location)
    node.outputs[0].default_value = value
    return node


def _math(tree, owner, key, operation, a, b=None, location=(0.0, 0.0), clamp=False):
    node = _own_node(tree, owner, key, 'ShaderNodeMath',
                     location, operation=operation, use_clamp=clamp)
    for socket, source in ((node.inputs[0], a), (node.inputs[1], b)):
        if source is None:
            continue
        if type(source) in (int, float):
            socket.default_value = source
        else:
            _link(tree, source, socket)
    return node.outputs[0]


def _vector_math(tree, owner, key, operation, a, b=None, location=(0.0, 0.0)):
    node = _own_node(tree, owner, key, 'ShaderNodeVectorMath',
                     location, operation=operation)
    _link(tree, a, node.inputs['Vector'])
    if b is not None:
        socket = node.inputs['Vector_001'] if operation != 'SCALE' else node.inputs['Scale']
        if type(b) in (int, float):
            socket.default_value = b
        elif isinstance(b, (list, tuple)):
            socket.default_value = b
        else:
            _link(tree, b, socket)
    return node.outputs['Value' if operation in ('DOT_PRODUCT', 'LENGTH', 'DISTANCE') else 'Vector']


def _combine(tree, owner, key, x, y, z=0.0, location=(0.0, 0.0)):
    node = _own_node(tree, owner, key, 'ShaderNodeCombineXYZ', location)
    for socket, source in zip((node.inputs['X'], node.inputs['Y'], node.inputs['Z']),
                              (x, y, z)):
        if type(source) in (int, float):
            socket.default_value = source
        else:
            _link(tree, source, socket)
    return node.outputs['Vector']


def _separate(tree, owner, key, source, location=(0.0, 0.0)):
    node = _own_node(tree, owner, key, 'ShaderNodeSeparateXYZ', location)
    _link(tree, source, node.inputs['Vector'])
    return node


def _mapping(tree, owner, key, source, location=(0.0, 0.0)):
    node = _own_node(tree, owner, key, 'ShaderNodeMapping',
                     location, vector_type='POINT')
    _link(tree, source, node.inputs['Vector'])
    return node


def _map_range(tree, owner, key, value, from_min, from_max, to_min, to_max,
               location=(0.0, 0.0)):
    node = _own_node(tree, owner, key, 'ShaderNodeMapRange',
                     location, clamp=True, interpolation_type='LINEAR')
    for socket, source in (
        (node.inputs['Value'], value),
        (node.inputs['From Min'], from_min),
        (node.inputs['From Max'], from_max),
        (node.inputs['To Min'], to_min),
        (node.inputs['To Max'], to_max),
    ):
        if type(source) in (int, float):
            socket.default_value = source
        else:
            _link(tree, source, socket)
    return node.outputs['Result']


def _build_manga(bpy, settings, name, owner, created):
    material = bpy.data.materials.new(name)
    created['materials'].append(material)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    p = {}
    for index, key in enumerate(_MANGA_SCALAR_FIELDS):
        p[key] = _param_value(tree, owner, key, settings[key],
                              (-1900.0, 1600.0 - index * 120.0)).outputs[0]
    space = settings['pattern_space']
    coordinates = None
    if space != 'UV':
        coordinates = _own_node(tree, owner, 'pattern:coordinates',
                                'ShaderNodeTexCoord', (-1800.0, 1200.0))
    if space == 'WINDOW':
        base_coordinate = coordinates.outputs['Window']
        resolution_x = _own_node(tree, owner, 'param:resolution_x', 'ShaderNodeValue')
        resolution_y = _own_node(tree, owner, 'param:resolution_y', 'ShaderNodeValue')
        resolution_x.outputs[0].default_value = settings['resolution'][0]
        resolution_y.outputs[0].default_value = settings['resolution'][1]
        pixel_scale = _combine(tree, owner, 'pattern:pixel_scale',
                               resolution_x.outputs[0], resolution_y.outputs[0], 1.0,
                               (-1550.0, 1150.0))
        pixel_coordinate = _vector_math(
            tree, owner, 'pattern:pixel_coordinate', 'MULTIPLY',
            base_coordinate, pixel_scale, (-1350.0, 1150.0))
        dot_mapping = _mapping(tree, owner, 'pattern:dot_mapping',
                               pixel_coordinate, (-1100.0, 850.0))
        hatch_coordinate = pixel_coordinate
    elif space == 'UV':
        uv_map = _own_node(tree, owner, 'pattern:uvmap', 'ShaderNodeUVMap',
                           (-1800.0, 1300.0))
        uv_map.uv_map = settings['uv_map']
        base_coordinate = uv_map.outputs['UV']
        hatch_coordinate = base_coordinate
        dot_mapping = _mapping(tree, owner, 'pattern:dot_mapping',
                               base_coordinate, (-1100.0, 850.0))
    else:
        base_coordinate = coordinates.outputs['Object']
        hatch_coordinate = base_coordinate
        dot_mapping = _mapping(tree, owner, 'pattern:dot_mapping',
                               base_coordinate, (-1100.0, 850.0))
    dot_cell = _math(tree, owner, 'pattern:dot_cell', 'DIVIDE',
                     1.0, p['dot_spacing'], (-1550.0, 800.0))
    dot_scale = _combine(tree, owner, 'pattern:dot_scale',
                         dot_cell, dot_cell, 1.0, (-1350.0, 800.0))
    _link(tree, dot_scale, dot_mapping.inputs['Scale'])
    if space != 'WINDOW':
        hatch_cell = _math(tree, owner, 'pattern:hatch_cell', 'DIVIDE',
                           1.0, p['hatch_spacing'], (-1550.0, 600.0))
        hatch_scale = _combine(tree, owner, 'pattern:hatch_scale',
                               hatch_cell, hatch_cell, 1.0, (-1350.0, 600.0))
    else:
        hatch_cell = _math(tree, owner, 'pattern:hatch_cell', 'DIVIDE',
                           1.0, p['hatch_spacing'], (-1550.0, 600.0))
        hatch_scale = _combine(tree, owner, 'pattern:hatch_scale',
                               hatch_cell, hatch_cell, 1.0, (-1350.0, 600.0))
    angle = _math(tree, owner, 'pattern:hatch_angle', 'RADIANS',
                  p['hatch_angle_deg'], None, (-1350.0, 450.0))
    angle_positive = _combine(tree, owner, 'pattern:hatch_angle_positive',
                              0.0, 0.0, angle, (-1150.0, 500.0))
    negative = _math(tree, owner, 'pattern:hatch_angle_negative', 'MULTIPLY',
                     angle, -1.0, (-1150.0, 350.0))
    angle_negative = _combine(tree, owner, 'pattern:hatch_angle_negative_xyz',
                              0.0, 0.0, negative, (-950.0, 350.0))
    hatch_a = _mapping(tree, owner, 'pattern:hatch_mapping_a',
                       hatch_coordinate, (-900.0, 600.0))
    hatch_b = _mapping(tree, owner, 'pattern:hatch_mapping_b',
                       hatch_coordinate, (-900.0, 400.0))
    for mapping, rotation in ((hatch_a, angle_positive), (hatch_b, angle_negative)):
        _link(tree, hatch_scale, mapping.inputs['Scale'])
        _link(tree, rotation, mapping.inputs['Rotation'])

    diffuse = _own_node(tree, owner, 'manga:diffuse',
                        'ShaderNodeBsdfDiffuse', (-1800.0, 600.0))
    _socket(diffuse.inputs, 'Color').default_value = (1.0, 1.0, 1.0, 1.0)
    _socket(diffuse.inputs, 'Roughness').default_value = 0.0
    shader_to_rgb = _own_node(tree, owner, 'manga:shader_to_rgb',
                              'ShaderNodeShaderToRGB', (-1600.0, 600.0))
    _link(tree, diffuse.outputs['BSDF'], shader_to_rgb.inputs['Shader'])
    luminance = _own_node(tree, owner, 'manga:luminance',
                          'ShaderNodeRGBToBW', (-1400.0, 600.0))
    _link(tree, shader_to_rgb.outputs['Color'], luminance.inputs['Color'])
    gain = _math(tree, owner, 'manga:light_gain', 'MULTIPLY',
                 luminance.outputs['Val'], p['light_gain'], (-1200.0, 600.0))
    biased = _math(tree, owner, 'manga:light_bias', 'ADD',
                   gain, p['light_bias'], (-1050.0, 600.0), clamp=True)

    below_black = _math(tree, owner, 'manga:below_black', 'LESS_THAN',
                        biased, p['black_below'], (-850.0, 750.0))
    below_hatch = _math(tree, owner, 'manga:below_hatch', 'LESS_THAN',
                        biased, p['hatch_below'], (-850.0, 600.0))
    below_dot = _math(tree, owner, 'manga:below_dot', 'LESS_THAN',
                      biased, p['dot_below'], (-850.0, 450.0))
    not_black = _math(tree, owner, 'manga:not_black', 'SUBTRACT',
                      1.0, below_black, (-700.0, 750.0))
    hatch_gate = _math(tree, owner, 'manga:hatch_gate', 'MULTIPLY',
                       below_hatch, not_black, (-550.0, 650.0))
    not_hatch = _math(tree, owner, 'manga:not_hatch', 'SUBTRACT',
                      1.0, below_hatch, (-700.0, 500.0))
    dot_gate = _math(tree, owner, 'manga:dot_gate', 'MULTIPLY',
                     below_dot, not_hatch, (-550.0, 450.0))

    hatch_width = _map_range(
        tree, owner, 'manga:hatch_width', biased,
        p['black_below'], p['hatch_below'],
        p['hatch_width_deep'], p['hatch_width_light'], (-1050.0, 250.0))
    hatch_half = _math(tree, owner, 'manga:hatch_half_width', 'MULTIPLY',
                       hatch_width, 0.5, (-850.0, 250.0))
    line_masks = []
    for suffix, mapping in (('a', hatch_a), ('b', hatch_b)):
        separate = _separate(tree, owner, 'manga:hatch_y_' + suffix,
                             mapping.outputs['Vector'], (-700.0, 100.0 if suffix == 'a' else -50.0))
        cell = _math(tree, owner, 'manga:hatch_fract_' + suffix, 'FRACT',
                     separate.outputs['Y'], None, (-550.0, 100.0 if suffix == 'a' else -50.0))
        offset = _math(tree, owner, 'manga:hatch_offset_' + suffix, 'SUBTRACT',
                       cell, 0.5, (-400.0, 100.0 if suffix == 'a' else -50.0))
        absolute = _math(tree, owner, 'manga:hatch_abs_' + suffix, 'ABSOLUTE',
                         offset, None, (-250.0, 100.0 if suffix == 'a' else -50.0))
        line_masks.append(_math(
            tree, owner, 'manga:hatch_line_' + suffix, 'LESS_THAN',
            absolute, hatch_half, (-100.0, 100.0 if suffix == 'a' else -50.0)))
    hatch_line = _math(tree, owner, 'manga:hatch_line', 'MAXIMUM',
                       line_masks[0], line_masks[1], (60.0, 50.0))
    hatch_ink = _math(tree, owner, 'manga:hatch_ink', 'MULTIPLY',
                      hatch_gate, hatch_line, (220.0, 300.0))

    dot_fraction = _vector_math(tree, owner, 'manga:dot_fraction', 'FRACTION',
                                dot_mapping.outputs['Vector'], None, (-700.0, -400.0))
    dot_xy = _separate(tree, owner, 'manga:dot_fraction_xy',
                       dot_fraction, (-550.0, -400.0))
    dot_dx = _math(tree, owner, 'manga:dot_dx', 'SUBTRACT',
                   dot_xy.outputs['X'], 0.5, (-400.0, -350.0))
    dot_dy = _math(tree, owner, 'manga:dot_dy', 'SUBTRACT',
                   dot_xy.outputs['Y'], 0.5, (-400.0, -500.0))
    dot_plane = _combine(tree, owner, 'manga:dot_plane',
                         dot_dx, dot_dy, 0.0, (-250.0, -425.0))
    dot_distance = _vector_math(tree, owner, 'manga:dot_distance', 'LENGTH',
                                dot_plane, None, (-100.0, -425.0))
    dot_radius = _map_range(
        tree, owner, 'manga:dot_radius', biased,
        p['hatch_below'], p['dot_below'],
        p['dot_radius_dark'], p['dot_radius_light'], (-500.0, -600.0))
    dot_mask = _math(tree, owner, 'manga:dot_mask', 'LESS_THAN',
                     dot_distance, dot_radius, (-100.0, -500.0))
    dot_ink = _math(tree, owner, 'manga:dot_ink', 'MULTIPLY',
                    dot_gate, dot_mask, (60.0, -400.0))
    ink_a = _math(tree, owner, 'manga:ink_a', 'MAXIMUM',
                  below_black, hatch_ink, (400.0, 250.0))
    ink = _math(tree, owner, 'manga:ink', 'MAXIMUM',
                ink_a, dot_ink, (550.0, 100.0))
    tone_delta = _math(tree, owner, 'manga:tone_delta', 'SUBTRACT',
                       p['ink_tone'], p['paper_tone'], (400.0, -100.0))
    tone_scaled = _math(tree, owner, 'manga:tone_scaled', 'MULTIPLY',
                        ink, tone_delta, (550.0, -100.0))
    tone = _math(tree, owner, 'manga:tone', 'ADD',
                 tone_scaled, p['paper_tone'], (700.0, -100.0))
    combine = _own_node(tree, owner, 'manga:combine', 'ShaderNodeCombineColor',
                        (850.0, -100.0))
    for name in ('Red', 'Green', 'Blue'):
        _link(tree, tone, combine.inputs[name])
    emission = _own_node(tree, owner, 'manga:emission', 'ShaderNodeEmission',
                         (1050.0, -100.0))
    _link(tree, combine.outputs['Color'], emission.inputs['Color'])
    emission.inputs['Strength'].default_value = 1.0
    output = _own_node(tree, owner, 'manga:output', 'ShaderNodeOutputMaterial',
                       (1250.0, -100.0), target='ALL')
    _link(tree, emission.outputs['Emission'], output.inputs['Surface'])
    material.use_fake_user = True
    return material


def _build_game(bpy, settings, name, owner, created):
    from . import materials as prod_materials
    from . import finish_nodes as prod_finish

    base = {'mode': 'eevee_cel3'}
    for key in ('base_color', 'shadow_color', 'mid_color', 'lit_color',
                'threshold_low', 'threshold_high'):
        base[key] = settings[key]
    temporary_materials = []
    core_groups = []
    temp = prod_materials._build(
        bpy, base, 'BLENDER_EEVEE', str(uuid.uuid4()),
        temporary_materials, core_groups)
    created['materials'].append(temp)
    created['groups'].extend(core_groups)
    _need(len(temporary_materials) == 1 and len(core_groups) == 1,
          'eevee_cel3 helper created an unexpected datablock count')
    wrapper = temp.node_tree.nodes.get('Toon/Managed Core - user wrapper outside')
    _need(wrapper is not None and wrapper.node_tree is not None,
          'eevee_cel3 helper did not expose its managed core')
    core = wrapper.node_tree
    core.name = name + '_GAME_Core'

    material = bpy.data.materials.new(name)
    created['materials'].append(material)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    core_node = _own_node(tree, owner, 'game:core', 'ShaderNodeGroup',
                          (-700.0, 200.0))
    core_node.node_tree = core
    for key, socket_name in _GAME_BASE_SOCKETS.items():
        core_node.inputs[socket_name].default_value = settings[key]
    output = _own_node(tree, owner, 'game:output', 'ShaderNodeOutputMaterial',
                       (700.0, 200.0), target='ALL')

    finish_settings = prod_finish.defaults()
    for key in _FINISH_FIELDS:
        finish_settings[key] = settings[key]
    finish_group = prod_finish.build(str(uuid.uuid4()), finish_settings)
    created['groups'].append(finish_group)
    finish_group.name = name + '_GAME_Finish'
    finish_node = _own_node(tree, owner, 'game:finish', 'ShaderNodeGroup',
                            (-200.0, 200.0))
    finish_node.node_tree = finish_group
    _link(tree, core_node.outputs['Shader'], finish_node.inputs['Base Shader'])

    reflection_color = _own_node(tree, owner, 'param:reflection_color',
                                 'ShaderNodeRGB', (-700.0, -250.0))
    reflection_color.outputs[0].default_value = settings['reflection_color']
    reflection_strength = _param_value(
        tree, owner, 'reflection_strength', settings['reflection_strength'],
        (-700.0, -400.0))
    reflection_enabled = _param_value(
        tree, owner, 'reflection_enabled',
        1.0 if settings['reflection_enabled'] else 0.0, (-900.0, -300.0))
    reflection_roughness = _param_value(
        tree, owner, 'reflection_roughness', settings['reflection_roughness'],
        (-700.0, -550.0))
    reflection_scale = _own_node(tree, owner, 'game:reflection_scale',
                                 'ShaderNodeMixRGB', (-450.0, -300.0))
    reflection_scale.blend_type = 'MULTIPLY'
    reflection_scale.use_clamp = False
    reflection_scale.inputs[0].default_value = 1.0
    _link(tree, reflection_color.outputs['Color'], reflection_scale.inputs['Color1'])
    _link(tree, reflection_strength.outputs[0], reflection_scale.inputs['Color2'])
    reflection = _own_node(tree, owner, 'game:reflection',
                           'ShaderNodeBsdfAnisotropic', (-150.0, -450.0),
                           distribution='GGX')
    _link(tree, reflection_scale.outputs['Color'], reflection.inputs['Color'])
    _link(tree, reflection_roughness.outputs[0], reflection.inputs['Roughness'])
    reflection.inputs['Anisotropy'].default_value = 0.0
    reflection.inputs['Rotation'].default_value = 0.0
    add = _own_node(tree, owner, 'game:add', 'ShaderNodeAddShader',
                    (300.0, 200.0))
    _link(tree, finish_node.outputs['Shader'], add.inputs[0])
    _link(tree, reflection.outputs['BSDF'], add.inputs[1])
    gate = _own_node(tree, owner, 'game:reflection_gate',
                     'ShaderNodeMixShader', (500.0, 200.0))
    _link(tree, reflection_enabled.outputs[0], _socket(gate.inputs, 'Fac'))
    _link(tree, finish_node.outputs['Shader'], _socket(gate.inputs, 'Shader'))
    _link(tree, add.outputs['Shader'], _socket(gate.inputs, 'Shader_001'))
    _link(tree, gate.outputs['Shader'], output.inputs['Surface'])

    bpy.data.materials.remove(temp)
    created['materials'].remove(temp)
    material.use_fake_user = True
    return material


def _cleanup(bpy, created):
    for material in reversed(created['materials']):
        if material.users == 1 and material.use_fake_user:
            material.use_fake_user = False
        if material.users == 0:
            bpy.data.materials.remove(material)
    for group in reversed(created['groups']):
        if group.users == 0:
            bpy.data.node_groups.remove(group)


def _material(material):
    import bpy
    _need(isinstance(material, bpy.types.Material),
          'Expected a Material')
    _need(material.get(PROFILE_KEY) == SCHEMA,
          'Material is not owned by ' + SCHEMA)
    return material


def build(bpy, settings, name):
    """Create an unbound Material with its own node graphs.

    No existing material, object, scene, slot or render setting is changed.
    """
    desired = normalize(settings)
    _need(type(name) is str and bool(name.strip()), 'name must be a nonempty string')
    owner = str(uuid.uuid4())
    created = {'materials': [], 'groups': []}
    try:
        if desired['style'] == 'GAME':
            material = _build_game(bpy, desired, name.strip(), owner, created)
        else:
            material = _build_manga(bpy, desired, name.strip(), owner, created)
        material[PROFILE_KEY] = SCHEMA
        material[OWNER_KEY] = owner
        material[STYLE_KEY] = desired['style']
        material[DECLARED_KEY] = _json(desired)
        material[GENERATION_KEY] = 1
        material[NOTE_KEY] = (
            'New reusable component material; not a surface_slots material. '
            'GAME reuses eevee_cel3 + finish_nodes; MANGA is Window/UV/Object '
            'screentone with a pure Emission Surface. GAME light_direction is '
            'an explicit art direction and requires update(); it is not a live '
            'light binding. GAME GGX is bypassed unless reflection_enabled '
            'is true.')
        material[SIGNATURE_KEY] = _structure_signature(material)
        result = inspect(material)
        _need(result['valid'],
              'Built component failed its own inspection: ' + _json(result['conflicts']))
        return material
    except Exception:
        _cleanup(bpy, created)
        raise


def _find_node(tree, key, owner=None):
    rows = [node for node in tree.nodes if node.get(NODE_KEY) == key]
    _need(len(rows) == 1, 'Owned node missing or duplicated: ' + key)
    node = rows[0]
    if owner is not None:
        _need(node.get(NODE_OWNER_KEY) == owner, 'Owned node owner changed: ' + key)
    return node


def _param(tree, key, owner):
    node = _find_node(tree, 'param:' + key, owner)
    _need(node.bl_idname == 'ShaderNodeValue',
          'Parameter node changed type: ' + key)
    return node.outputs[0].default_value


def _node_key(node):
    for key in (NODE_KEY, _FINISH_NODE_KEY, _MANAGED_NODE_KEY):
        value = node.get(key)
        if type(value) is str:
            return key + ':' + value
    return 'name:' + node.name


def _is_parameter_node(node):
    for key in (NODE_KEY, _FINISH_NODE_KEY):
        value = node.get(key)
        if type(value) is str and value.startswith('param:'):
            return True
    return node.get(NODE_KEY) == 'game:core'


def _tree_role(tree):
    if tree.get(_FINISH_PROFILE_KEY) in (_FINISH_PROFILE, _FINISH_HAIR_PROFILE):
        return 'finish'
    if tree.get(_MANAGED_ROLE_KEY) == 'shading_core' and tree.get(_MANAGED_CORE_ID):
        return 'managed_core'
    return 'group:' + tree.name


def _node_record(node):
    properties = {}
    for name in ('operation', 'use_clamp', 'blend_type', 'data_type',
                 'distribution', 'component', 'vector_type', 'interpolation',
                 'extension', 'clamp', 'is_active_output', 'target',
                 'attribute_name', 'attribute_type', 'space'):
        if hasattr(node, name):
            properties[name] = _plain(getattr(node, name))
    if node.bl_idname == 'ShaderNodeUVMap' and node.get(NODE_KEY) != 'pattern:uvmap':
        properties['uv_map'] = node.uv_map
    if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None:
        properties['node_tree_role'] = _tree_role(node.node_tree)
    parameter = _is_parameter_node(node)
    inputs = []
    for socket in node.inputs:
        if not hasattr(socket, 'default_value'):
            continue
        inputs.append({
            'identifier': socket.identifier,
            'type': socket.bl_idname,
            'value': 'PARAMETER' if parameter else _plain(socket.default_value),
        })
    outputs = []
    for socket in node.outputs:
        if not hasattr(socket, 'default_value'):
            continue
        outputs.append({
            'identifier': socket.identifier,
            'type': socket.bl_idname,
            'value': 'PARAMETER' if parameter else _plain(socket.default_value),
        })
    return {
        'id': _node_key(node),
        'name': node.name,
        'type': node.bl_idname,
        'mute': bool(node.mute),
        'properties': properties,
        'inputs': inputs,
        'outputs': outputs,
    }


def _structure_signature(material):
    trees = []
    seen = set()

    def collect(tree, role):
        identity = tree.as_pointer()
        if identity in seen:
            return
        seen.add(identity)
        nodes = [_node_record(node) for node in tree.nodes]
        ids = [row['id'] for row in nodes]
        _need(len(ids) == len(set(ids)), 'Duplicate node identity in ' + tree.name)
        links = sorted([
            [_node_key(link.from_node), link.from_socket.identifier,
             _node_key(link.to_node), link.to_socket.identifier]
            for link in tree.links
        ])
        interface = []
        for item in tree.interface.items_tree:
            row = {'item_type': item.item_type}
            for name in ('identifier', 'name', 'in_out', 'socket_type',
                         'default_value', 'min_value', 'max_value'):
                if hasattr(item, name):
                    try:
                        row[name] = _plain(getattr(item, name))
                    except StyleComponentError:
                        row[name] = None
            interface.append(row)
        interface.sort(key=lambda row: (row.get('identifier') or '',
                                        row.get('name') or ''))
        trees.append({'role': role, 'interface': interface,
                      'nodes': sorted(nodes, key=lambda row: row['id']),
                      'links': links})
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None:
                collect(node.node_tree, _tree_role(node.node_tree))

    collect(material.node_tree, 'material')
    return _digest(trees)


def _tree_summary(tree):
    counts = {}
    for node in tree.nodes:
        counts[node.bl_idname] = counts.get(node.bl_idname, 0) + 1
    return {
        'name': tree.name,
        'nodes': len(tree.nodes),
        'links': len(tree.links),
        'counts': dict(sorted(counts.items())),
    }


def _linked_node(socket):
    return socket.links[0].from_node if socket.links else None


def _trace_coordinate(socket):
    seen = set()
    while socket is not None and socket.as_pointer() not in seen:
        seen.add(socket.as_pointer())
        node = _linked_node(socket)
        if node is None:
            return None, None
        if node.bl_idname in ('ShaderNodeTexCoord', 'ShaderNodeUVMap'):
            return node, socket.links[0].from_socket
        candidates = [item for item in node.inputs
                      if item.type == 'VECTOR' and item.links]
        if not candidates:
            return None, None
        socket = candidates[0]
    return None, None


def _conflict(info, code, message, severity='error'):
    info['conflicts'].append({'code': code, 'severity': severity,
                              'message': message})


def _inspect_game(material, info, owner):
    actual = {}
    try:
        from . import finish_nodes as prod_finish
        from . import materials as prod_materials
    except Exception as error:
        _conflict(info, 'dependency_missing',
                  'GAME inspection requires toon_workflow_addon: ' + str(error))
        return actual
    tree = material.node_tree
    try:
        core_node = _find_node(tree, 'game:core', owner)
        finish_node = _find_node(tree, 'game:finish', owner)
        output = _find_node(tree, 'game:output', owner)
        add = _find_node(tree, 'game:add', owner)
        gate = _find_node(tree, 'game:reflection_gate', owner)
        reflection = _find_node(tree, 'game:reflection', owner)
        reflection_color = _find_node(tree, 'param:reflection_color', owner)
        reflection_strength = _find_node(tree, 'param:reflection_strength', owner)
        reflection_enabled = _find_node(tree, 'param:reflection_enabled', owner)
        reflection_roughness = _find_node(tree, 'param:reflection_roughness', owner)
    except StyleComponentError as error:
        _conflict(info, 'missing_owned_node', str(error))
        return actual
    if core_node.node_tree is None:
        _conflict(info, 'core_missing', 'GAME core group is unset')
    else:
        core = core_node.node_tree
        if (core.get(_MANAGED_ROLE_KEY) != 'shading_core'
                or not core.get(_MANAGED_CORE_ID)):
            _conflict(info, 'core_reuse_mismatch',
                      'GAME core is not a reused eevee_cel3 managed core')
        if any(node.bl_idname in ('ShaderNodeBsdfAnisotropic',
                                  'ShaderNodeBsdfGlossy',
                                  'ShaderNodeBsdfPrincipled')
               for node in core.nodes):
            _conflict(info, 'double_highlight',
                      'GAME core contains a second specular closure')
    for key, socket_name in _GAME_BASE_SOCKETS.items():
        try:
            actual[key] = _plain(core_node.inputs[socket_name].default_value)
        except (KeyError, TypeError):
            _conflict(info, 'core_input_missing', 'Missing core input: ' + socket_name)
    if finish_node.node_tree is None:
        _conflict(info, 'finish_missing', 'GAME finish group is unset')
    else:
        finish = finish_node.node_tree
        if finish.get(_FINISH_PROFILE_KEY) not in (_FINISH_PROFILE,
                                                   _FINISH_HAIR_PROFILE):
            _conflict(info, 'finish_reuse_mismatch',
                      'GAME finish group is not a finish_nodes kernel')
        try:
            actual.update(prod_finish.parameters(finish))
        except Exception as error:
            _conflict(info, 'finish_parameter_error', str(error))
        if any(node.bl_idname in ('ShaderNodeBsdfAnisotropic',
                                  'ShaderNodeBsdfGlossy',
                                  'ShaderNodeBsdfPrincipled')
               for node in finish.nodes):
            _conflict(info, 'double_highlight',
                      'GAME finish group contains an extra specular closure')
    if reflection.bl_idname != 'ShaderNodeBsdfAnisotropic':
        _conflict(info, 'reflection_type', 'GAME reflection is not Anisotropic BSDF')
    else:
        if reflection.distribution != 'GGX':
            _conflict(info, 'reflection_distribution',
                      'GAME reflection must use GGX')
        actual['reflection_color'] = _plain(
            reflection_color.outputs['Color'].default_value)
        actual['reflection_strength'] = _plain(
            reflection_strength.outputs['Value'].default_value)
        raw_enabled = _plain(reflection_enabled.outputs['Value'].default_value)
        if not (abs(raw_enabled) <= 1e-6 or abs(raw_enabled - 1.0) <= 1e-6):
            _conflict(info, 'reflection_enabled_invalid',
                      'GAME reflection_enabled must be exactly 0 or 1')
        actual['reflection_enabled'] = raw_enabled >= 0.5
        actual['reflection_roughness'] = _plain(
            reflection_roughness.outputs['Value'].default_value)
        info['reflection_default_low'] = actual['reflection_strength'] <= 0.5
        info['reflection_default_disabled'] = actual['reflection_enabled'] is False
    if len([node for node in tree.nodes
            if node.bl_idname == 'ShaderNodeBsdfAnisotropic']) != 1:
        _conflict(info, 'double_highlight',
                  'GAME material needs exactly one GGX reflection closure')
    if gate.bl_idname != 'ShaderNodeMixShader':
        _conflict(info, 'reflection_gate_type',
                  'GAME reflection gate is not a Mix Shader')
    surface = output.inputs['Surface']
    if len(surface.links) != 1 or surface.links[0].from_node != gate:
        _conflict(info, 'wrong_output',
                  'GAME Surface is not fed by its reflection gate')
    gate_finish = _socket(gate.inputs, 'Shader')
    gate_add = _socket(gate.inputs, 'Shader_001')
    gate_fac = _socket(gate.inputs, 'Fac')
    if (len(gate_finish.links) != 1
            or gate_finish.links[0].from_node != finish_node):
        _conflict(info, 'wrong_output',
                  'GAME reflection gate is not fed by the finish shader')
    if len(gate_add.links) != 1 or gate_add.links[0].from_node != add:
        _conflict(info, 'wrong_output',
                  'GAME reflection gate is not fed by the Add Shader')
    if len(gate_fac.links) != 1 or gate_fac.links[0].from_node != reflection_enabled:
        _conflict(info, 'wrong_output',
                  'GAME reflection gate factor is not the enable parameter')
    finish_links = [link for link in add.inputs[0].links
                    if link.from_node == finish_node]
    reflection_links = [link for link in add.inputs[1].links
                        if link.from_node == reflection]
    if len(finish_links) != 1 or len(reflection_links) != 1:
        _conflict(info, 'wrong_output',
                  'GAME Add Shader does not combine finish and reflection')
    base_input = finish_node.inputs.get('Base Shader') if finish_node.node_tree else None
    if base_input is not None:
        if len(base_input.links) != 1 or base_input.links[0].from_node != core_node:
            _conflict(info, 'wrong_output',
                      'GAME finish Base Shader is not fed by the core')
    info['graph'] = {
        'material': _tree_summary(tree),
        'core': _tree_summary(core_node.node_tree) if core_node.node_tree else None,
        'finish': _tree_summary(finish_node.node_tree) if finish_node.node_tree else None,
        'core_reused': bool(core_node.node_tree and core_node.node_tree.get(_MANAGED_CORE_ID)),
        'finish_reused': bool(finish_node.node_tree and finish_node.node_tree.get(_FINISH_PROFILE_KEY)),
        'light_binding': 'explicit world-space art direction; update() required after changes',
        'reflection_gate': {
            'node': gate.name,
            'enabled': actual.get('reflection_enabled'),
            'semantics': 'Mix Shader: 0=finish only, 1=finish+GGX; '
                         'GGX does not participate unless explicitly enabled',
        },
        'output': 'Emission core + Emission finish; GGX only when reflection_enabled is true',
    }
    scene = getattr(__import__('bpy'), 'context').scene
    info['scene'] = {
        'engine': scene.render.engine,
        'resolution': [int(scene.render.resolution_x), int(scene.render.resolution_y)],
    }
    if scene.render.engine != 'BLENDER_EEVEE':
        _conflict(info, 'engine_not_eevee',
                  'GAME ShaderToRGB core requires EEVEE; current engine is '
                  + str(scene.render.engine), 'warning')
    return actual


def _inspect_manga(material, info, owner):
    tree = material.node_tree
    actual = {}
    try:
        for key in _MANGA_SCALAR_FIELDS:
            actual[key] = _param(tree, key, owner)
        output = _find_node(tree, 'manga:output', owner)
        emission = _find_node(tree, 'manga:emission', owner)
        combine = _find_node(tree, 'manga:combine', owner)
        tone = _find_node(tree, 'manga:tone', owner)
        dot_mapping = _find_node(tree, 'pattern:dot_mapping', owner)
        hatch_a = _find_node(tree, 'pattern:hatch_mapping_a', owner)
        hatch_b = _find_node(tree, 'pattern:hatch_mapping_b', owner)
    except StyleComponentError as error:
        _conflict(info, 'missing_owned_node', str(error))
        return actual
    source, source_socket = _trace_coordinate(dot_mapping.inputs['Vector'])
    if source is None:
        _conflict(info, 'coordinate_source', 'MANGA dot coordinate source is missing')
        space = None
    elif source.bl_idname == 'ShaderNodeUVMap':
        space = 'UV'
        actual['uv_map'] = source.uv_map
    elif source_socket.name == 'Window':
        space = 'WINDOW'
    elif source_socket.name == 'Object':
        space = 'OBJECT'
    else:
        _conflict(info, 'coordinate_source',
                  'MANGA coordinate source is not Window, UV or Object')
        space = None
    actual['pattern_space'] = space
    if space == 'WINDOW':
        try:
            raw_resolution = [
                _param(tree, 'resolution_x', owner),
                _param(tree, 'resolution_y', owner),
            ]
            actual['resolution'] = [int(round(value)) for value in raw_resolution]
            if any(abs(value - rounded) > 1e-4
                   for value, rounded in zip(raw_resolution, actual['resolution'])):
                _conflict(info, 'parameter_mismatch',
                          'WINDOW resolution nodes are not integral pixels')
        except (StyleComponentError, ValueError) as error:
            _conflict(info, 'resolution_missing', str(error))
        actual['uv_map'] = None
    else:
        actual['resolution'] = None
        actual['uv_map'] = actual.get('uv_map')
    for node in (hatch_a, hatch_b):
        if node.bl_idname != 'ShaderNodeMapping':
            _conflict(info, 'hatch_mapping_type', 'Hatch mapping node changed type')
    random_types = ('ShaderNodeTexVoronoi', 'ShaderNodeTexWave', 'ShaderNodeTexNoise')
    if any(node.bl_idname in random_types for node in tree.nodes):
        _conflict(info, 'random_pattern',
                  'MANGA contains Voronoi/Wave/Noise; default screen must be regular')
    if output.inputs['Surface'].links:
        source_node = output.inputs['Surface'].links[0].from_node
        if source_node != emission:
            _conflict(info, 'wrong_output',
                      'MANGA Surface is not a single Emission')
    else:
        _conflict(info, 'wrong_output', 'MANGA Surface is unlinked')
    if emission.bl_idname != 'ShaderNodeEmission':
        _conflict(info, 'wrong_output', 'MANGA output node is not Emission')
    if emission.inputs['Strength'].default_value != 1.0:
        _conflict(info, 'wrong_output', 'MANGA Emission strength must be 1')
    color_source = _linked_node(emission.inputs['Color'])
    if color_source != combine:
        _conflict(info, 'wrong_output', 'MANGA Emission color is not the grayscale combine')
    grayscale = True
    for name in ('Red', 'Green', 'Blue'):
        link = combine.inputs[name].links
        if len(link) != 1 or link[0].from_node != tone:
            grayscale = False
    if not grayscale:
        _conflict(info, 'not_grayscale',
                  'MANGA R/G/B are not fed by the same scalar tone')
    counts = {}
    for node in tree.nodes:
        counts[node.bl_idname] = counts.get(node.bl_idname, 0) + 1
    info['graph'] = {
        'material': _tree_summary(tree),
        'counts': dict(sorted(counts.items())),
        'coordinate_space': space,
        'dot_source': {
            'WINDOW': 'regular screen-space grid via Vector Math FRACTION',
            'UV': 'regular UV-space grid via Vector Math FRACTION; named UV Map, not screen-matched',
            'OBJECT': 'regular local Object-space grid via Vector Math FRACTION; not screen-matched',
            None: 'unknown coordinate source',
        }[space],
        'pixel_formula': (
            'pixel=(window.x*resolution_x, window.y*resolution_y); '
            'dot_cell=(pixel.x/dot_spacing, pixel.y/dot_spacing)'
            if space == 'WINDOW' else
            'no screen-pixel formula outside WINDOW space'
        ),
        'threshold_driver': (
            'ShaderToRGB luminance -> light_gain -> light_bias(clamped) -> '
            'black/hatch/dot thresholds'
        ),
        'has_voronoi': counts.get('ShaderNodeTexVoronoi', 0) > 0,
        'has_wave': counts.get('ShaderNodeTexWave', 0) > 0,
        'surface': 'Emission -> OutputMaterial.Surface',
        'gamma': 'scalar tone through CombineColor with identical R/G/B',
    }
    if space == 'WINDOW' and 'resolution' in actual:
        scene = getattr(__import__('bpy'), 'context').scene
        current = [int(scene.render.resolution_x), int(scene.render.resolution_y)]
        percentage = int(scene.render.resolution_percentage)
        effective = [max(1, int(round(value * percentage / 100.0)))
                     for value in current]
        info['scene'] = {
            'engine': scene.render.engine,
            'resolution': current,
            'resolution_percentage': percentage,
            'effective_resolution': effective,
            'declared_resolution': actual['resolution'],
            'resolution_matches': effective == actual['resolution'],
            'aspect': effective[0] / effective[1] if effective[1] else None,
        }
        if percentage != 100:
            _conflict(info, 'resolution_percentage_not_100',
                      'WINDOW resolution must be the effective pixel resolution; '
                      'resolution_percentage is ' + str(percentage),
                      'warning')
        if effective != actual['resolution']:
            _conflict(info, 'scene_resolution_mismatch',
                      'Scene resolution differs from baked WINDOW resolution; '
                      'call update() after changing render resolution',
                      'warning')
    return actual


def _finalize_info(info, style):
    errors = [row for row in info['conflicts'] if row['severity'] == 'error']
    warnings = [row for row in info['conflicts'] if row['severity'] != 'error']
    info['valid'] = not errors
    info['status'] = 'valid' if info['valid'] else 'conflict'
    info['settings'] = None
    info['issues'] = [row['code'] + ': ' + row['message'] for row in errors]
    info['warnings'] = [row['code'] + ': ' + row['message'] for row in warnings]
    if info['valid'] and style in STYLES:
        candidate = {'schema': SCHEMA, 'style': style}
        candidate.update(info.get('actual') or {})
        try:
            info['settings'] = normalize(candidate)
        except StyleComponentError as error:
            _conflict(info, 'settings_not_canonical', str(error))
            errors = [row for row in info['conflicts'] if row['severity'] == 'error']
            info['valid'] = False
            info['status'] = 'conflict'
            info['settings'] = None
            info['issues'] = [row['code'] + ': ' + row['message']
                              for row in errors]
    return info


def inspect(material):
    """Read declarations, parameter nodes and graph conflicts without edits.

    Minimal stable keys: status ('valid'|'conflict'), settings (canonical
    settings or None) and issues (list[str]).  Rich diagnostics are retained
    as additional keys for local tests and the parent adapter.
    """
    import bpy
    _need(isinstance(material, bpy.types.Material), 'Expected a Material')
    if material.get(PROFILE_KEY) != SCHEMA:
        info = {
            'schema': SCHEMA,
            'material': material.name,
            'style': None,
            'owner': None,
            'generation': None,
            'valid': False,
            'status': 'conflict',
            'settings': None,
            'issues': [],
            'warnings': [],
            'conflicts': [],
            'declaration': None,
            'actual': {},
            'surface_slot_managed': False,
            'legacy_managed_identity': False,
            'external_metadata_keys': sorted(
                str(key) for key in material.keys()
                if not str(key).startswith(PROFILE_PREFIX)),
        }
        _conflict(info, 'not_owned', 'Material is not owned by ' + SCHEMA)
        return _finalize_info(info, None)
    owner = material.get(OWNER_KEY)
    style = material.get(STYLE_KEY)
    info = {
        'schema': SCHEMA,
        'material': material.name,
        'style': style,
        'owner': owner,
        'generation': material.get(GENERATION_KEY),
        'valid': False,
        'status': 'conflict',
        'settings': None,
        'issues': [],
        'warnings': [],
        'conflicts': [],
        'declaration': None,
        'actual': {},
        'surface_slot_managed': False,
        'legacy_managed_identity': False,
        'external_metadata_keys': [],
    }

    def conflict(code, message, severity='error'):
        _conflict(info, code, message, severity)

    try:
        _uuid_text(owner, 'owner')
    except StyleComponentError as error:
        conflict('invalid_owner', str(error))
    if style not in STYLES:
        conflict('invalid_style', 'Unknown component style')
    if material.library is not None or material.override_library is not None:
        conflict('not_local', 'Component material must be local and non-override')
    if not material.is_editable:
        conflict('not_editable', 'Component material must be editable')
    tree = material.node_tree
    if tree is None:
        conflict('missing_nodes', 'Component material has no node tree')
        info['valid'] = False
        return _finalize_info(info, style)
    own_nodes = []
    for node in tree.nodes:
        key = node.get(NODE_KEY)
        if type(key) is not str:
            conflict('foreign_node', 'Untagged node in module material: ' + node.name)
            continue
        own_nodes.append(node)
        if node.get(NODE_OWNER_KEY) != owner:
            conflict('foreign_node', 'Node owner changed: ' + node.name)
    keys = [node.get(NODE_KEY) for node in own_nodes]
    if len(keys) != len(set(keys)):
        conflict('duplicate_node', 'Duplicate module node identity')
    slot_keys = [key for key in material.keys()
                 if str(key).startswith('toon_surface_slot_')]
    info['surface_slot_managed'] = bool(slot_keys)
    legacy_keys = [key for key in material.keys() if str(key) in (
        '_toon_scope_id', '_toon_role', '_toon_material_id', '_toon_core_id')]
    info['legacy_managed_identity'] = bool(legacy_keys)
    info['external_metadata_keys'] = sorted(
        str(key) for key in material.keys()
        if not str(key).startswith(PROFILE_PREFIX))
    declaration = None
    try:
        declaration = json.loads(material[DECLARED_KEY])
        info['declaration'] = declaration
    except (KeyError, ValueError, TypeError) as error:
        conflict('declaration_invalid', 'Stored declaration is missing or invalid')
    if isinstance(declaration, dict):
        if declaration.get('schema') != SCHEMA or declaration.get('style') != style:
            conflict('declaration_invalid', 'Declaration schema/style does not match markers')
        else:
            try:
                normalized = normalize(declaration)
                if normalized != declaration:
                    conflict('declaration_not_canonical',
                             'Stored declaration is not canonical')
            except StyleComponentError as error:
                conflict('declaration_invalid', str(error))
    actual = {}
    if style == 'GAME':
        try:
            actual = _inspect_game(material, info, owner)
        except Exception as error:
            conflict('inspect_error', type(error).__name__ + ': ' + str(error))
    elif style == 'MANGA':
        try:
            actual = _inspect_manga(material, info, owner)
        except Exception as error:
            conflict('inspect_error', type(error).__name__ + ': ' + str(error))
    info['actual'] = actual
    if isinstance(declaration, dict) and actual:
        fields = _GAME_FIELDS if style == 'GAME' else _MANGA_FIELDS
        for key in fields:
            if key not in actual:
                conflict('missing_parameter', 'Actual parameter missing: ' + key)
            elif key in declaration and not _close(declaration[key], actual[key]):
                conflict('parameter_mismatch',
                         'Declared ' + key + ' differs from the native node value')
    try:
        actual_signature = _structure_signature(material)
        stored_signature = material.get(SIGNATURE_KEY)
        info['signature'] = {
            'stored': stored_signature,
            'actual': actual_signature,
            'match': stored_signature == actual_signature,
        }
        if stored_signature != actual_signature:
            conflict('graph_modified',
                     'Node graph differs from the build-time signature')
    except StyleComponentError as error:
        conflict('graph_unreadable', str(error))
    if style == 'MANGA':
        scene = info.get('scene')
        engine = scene['engine'] if scene else getattr(__import__('bpy'), 'context').scene.render.engine
        if engine != 'BLENDER_EEVEE':
            conflict('engine_not_eevee',
                     'ShaderToRGB MANGA requires EEVEE; current engine is ' + str(engine),
                     'warning')
    return _finalize_info(info, style)


def _write_settings(material, settings):
    style = settings['style']
    tree = material.node_tree
    owner = material[OWNER_KEY]
    if style == 'MANGA':
        for key in _MANGA_SCALAR_FIELDS:
            _find_node(tree, 'param:' + key, owner).outputs[0].default_value = settings[key]
        if settings['pattern_space'] == 'WINDOW':
            _find_node(tree, 'param:resolution_x', owner).outputs[0].default_value = settings['resolution'][0]
            _find_node(tree, 'param:resolution_y', owner).outputs[0].default_value = settings['resolution'][1]
        if settings['pattern_space'] == 'UV':
            _find_node(tree, 'pattern:uvmap', owner).uv_map = settings['uv_map']
        return
    from . import finish_nodes as prod_finish
    core_node = _find_node(tree, 'game:core', owner)
    for key, socket_name in _GAME_BASE_SOCKETS.items():
        core_node.inputs[socket_name].default_value = settings[key]
    finish_node = _find_node(tree, 'game:finish', owner)
    finish_settings = prod_finish.defaults()
    for key in _FINISH_FIELDS:
        finish_settings[key] = settings[key]
    prod_finish.write_parameters(finish_node.node_tree, finish_settings)
    _find_node(tree, 'param:reflection_color', owner).outputs[0].default_value = settings['reflection_color']
    _find_node(tree, 'param:reflection_strength', owner).outputs[0].default_value = settings['reflection_strength']
    _find_node(tree, 'param:reflection_enabled', owner).outputs[0].default_value = 1.0 if settings['reflection_enabled'] else 0.0
    _find_node(tree, 'param:reflection_roughness', owner).outputs[0].default_value = settings['reflection_roughness']


def update(material, settings):
    """Guarded parameter update; author-edited graphs and unknown fields reject.

    The graph is never rebuilt, deleted or adopted.  If any write or readback
    fails, all parameter constants and the declaration are restored.
    """
    material = _material(material)
    current = inspect(material)
    if not current['valid']:
        codes = ', '.join(row['code'] for row in current['conflicts']
                          if row['severity'] == 'error')
        raise StyleComponentError(
            'Refusing update on a modified or invalid component: ' + codes)
    desired = normalize(settings)
    if desired['style'] != current['style']:
        raise StyleComponentError('update cannot switch component style')
    if (current['style'] == 'MANGA'
            and desired['pattern_space'] != current['declaration']['pattern_space']):
        raise StyleComponentError(
            'update cannot switch pattern_space; build a new component')
    old = current['declaration']
    old_generation = material.get(GENERATION_KEY, 0)
    old_declared = material[DECLARED_KEY]
    old_signature = material.get(SIGNATURE_KEY)
    try:
        _write_settings(material, desired)
        material[DECLARED_KEY] = _json(desired)
        material[GENERATION_KEY] = int(old_generation) + 1
        signature = _structure_signature(material)
        if signature != old_signature:
            raise StyleComponentError(
                'update changed graph structure; author graph is protected')
        result = inspect(material)
        if not result['valid']:
            raise StyleComponentError(
                'update readback failed: ' + _json(result['conflicts']))
        return result
    except Exception:
        try:
            _write_settings(material, old)
        finally:
            material[DECLARED_KEY] = old_declared
            material[GENERATION_KEY] = old_generation
        raise
