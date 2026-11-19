# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed, static EEVEE eye overlay shaders.

This module only owns newly created materials.  It does not assign slots,
change meshes, opt a scene into an optical render profile, or qualify lines.
Colored transparency requires the separately qualified BLENDED render path.
"""
import hashlib
import json
import math
import uuid


PROFILE = 'toon-eye-overlay.v1'
PROFILE_KEY = 'toon_eye_overlay_profile'
_OWNER = 'toon_eye_overlay_owner'
_KIND = 'toon_eye_overlay_kind'
_SETTINGS = 'toon_eye_overlay_settings'
_SIGNATURE = 'toon_eye_overlay_signature'
_NODE = 'toon_eye_overlay_node'
_KINDS = ('SHADOW', 'GLINT')
_KEYS = dict(SHADOW={'tint', 'opacity'},
             GLINT={'opacity', 'uv_name', 'center', 'radius', 'feather', 'color'})


class EyeOverlayError(ValueError):
    """Invalid overlay input, unsupported profile, or changed owned graph."""


def _need(condition, message):
    if not condition:
        raise EyeOverlayError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _number(value, name, low, high):
    _need(type(value) in (int, float) and math.isfinite(value),
          name + ' requires a finite number')
    value = float(value)
    _need(low <= value <= high, name + ' is outside its supported range')
    return value


def _vector(value, name, count, low, high):
    _need(type(value) in (list, tuple) and len(value) == count,
          name + ' requires ' + str(count) + ' components')
    return [_number(item, name, low, high) for item in value]


def _normalize(kind, settings):
    _need(kind in _KINDS, 'Eye overlay kind must be SHADOW or GLINT')
    _need(type(settings) is dict and set(settings) == _KEYS[kind],
          'Incorrect eye overlay settings for ' + kind)
    result = {'opacity': _number(settings['opacity'], 'opacity', 0., 1.)}
    if kind == 'SHADOW':
        result['tint'] = _vector(settings['tint'], 'tint', 3, 0., 1.)
    else:
        name = settings['uv_name']
        _need(type(name) is str and bool(name.strip()) and '\x00' not in name,
              'An explicit nonempty UV layer name is required')
        result.update(uv_name=name,
            center=_vector(settings['center'], 'center', 2, -1.e6, 1.e6),
            radius=_vector(settings['radius'], 'radius', 2, 1.e-6, 1.e6),
            feather=_number(settings['feather'], 'feather', 1.e-4, 1.),
            color=_vector(settings['color'], 'color', 3, 0., 1.))
    return result


def _static(block):
    animation = block.animation_data
    _need(animation is None or (animation.action is None
          and not animation.drivers and not animation.nla_tracks),
          'Static eye overlay does not admit animation or drivers')


def _local(material):
    import bpy
    _need(isinstance(material, bpy.types.Material), 'Expected a Material')
    _need(not material.library and not material.override_library
          and material.is_editable, 'Eye overlay must be local and editable')
    _need(material.use_nodes and material.node_tree is not None,
          'Eye overlay node tree is missing')
    _static(material)
    _static(material.node_tree)


def _nodes(material):
    result = {}
    for node in material.node_tree.nodes:
        key = node.get(_NODE)
        _need(type(key) is str and key not in result,
              'Missing or duplicated owned eye overlay node')
        _need(node.get(_OWNER) == material.get(_OWNER),
              'Eye overlay node owner changed')
        result[key] = node
    return result


def _plain(value):
    if type(value) in (str, bool, int):
        return value
    if isinstance(value, float):
        _need(math.isfinite(value), 'Nonfinite eye overlay socket')
        return float(value)
    try:
        return [_plain(item) for item in value]
    except TypeError as error:
        raise EyeOverlayError('Unsupported eye overlay socket') from error


def _socket_snapshot(sockets):
    return [dict(index=index, identifier=socket.identifier,
                 value=_plain(socket.default_value) if hasattr(socket, 'default_value') else None)
            for index, socket in enumerate(sockets)]


def _snapshot(material):
    """Every admitted node socket and shader-affecting node option is pinned."""
    nodes = _nodes(material)
    rows = []
    for key, node in sorted(nodes.items()):
        row = dict(id=key, type=node.bl_idname, mute=node.mute,
                   inputs=_socket_snapshot(node.inputs),
                   outputs=_socket_snapshot(node.outputs))
        for name in ('operation', 'use_clamp', 'uv_map', 'target', 'is_active_output'):
            if hasattr(node, name):
                row[name] = getattr(node, name)
        rows.append(row)
    links = sorted((link.from_node.get(_NODE),
                    list(link.from_node.outputs).index(link.from_socket),
                    link.to_node.get(_NODE),
                    list(link.to_node.inputs).index(link.to_socket))
                   for link in material.node_tree.links)
    overlap = {name: getattr(material, name) for name in
               ('show_transparent_back', 'use_transparency_overlap') if hasattr(material, name)}
    return dict(nodes=rows, links=links, render_method=material.surface_render_method,
                overlap=overlap, backface_culling=material.use_backface_culling)


def _signature(material):
    return hashlib.sha256(_json(_snapshot(material)).encode('utf8')).hexdigest()


def _new(material, key, node_type, **properties):
    node = material.node_tree.nodes.new(node_type)
    node[_NODE] = key
    node[_OWNER] = material[_OWNER]
    node.name = 'Eye ' + key
    node.label = key
    for name, value in properties.items():
        setattr(node, name, value)
    return node


def _build(material, kind):
    tree = material.node_tree
    tree.nodes.clear()
    output = _new(material, 'output', 'ShaderNodeOutputMaterial', target='ALL')
    transparent = _new(material, 'transparent', 'ShaderNodeBsdfTransparent')
    transparent.inputs['Color'].default_value = (1., 1., 1., 1.)
    if kind == 'SHADOW':
        tree.links.new(transparent.outputs['BSDF'], output.inputs['Surface'])
        return
    uv = _new(material, 'uv', 'ShaderNodeUVMap')
    sub = _new(material, 'center', 'ShaderNodeVectorMath', operation='SUBTRACT')
    div = _new(material, 'radius', 'ShaderNodeVectorMath', operation='DIVIDE')
    length = _new(material, 'distance', 'ShaderNodeVectorMath', operation='LENGTH')
    inverse = _new(material, 'one_minus_r', 'ShaderNodeMath', operation='SUBTRACT')
    inverse.inputs[0].default_value = 1.
    feather = _new(material, 'feather', 'ShaderNodeMath', operation='DIVIDE', use_clamp=True)
    squared = _new(material, 't_squared', 'ShaderNodeMath', operation='MULTIPLY')
    twice = _new(material, 'two_t', 'ShaderNodeMath', operation='MULTIPLY')
    twice.inputs[1].default_value = 2.
    subtract = _new(material, 'three_minus_two_t', 'ShaderNodeMath', operation='SUBTRACT')
    subtract.inputs[0].default_value = 3.
    smooth = _new(material, 'smoothstep', 'ShaderNodeMath', operation='MULTIPLY')
    alpha = _new(material, 'opacity', 'ShaderNodeMath', operation='MULTIPLY')
    emission = _new(material, 'emission', 'ShaderNodeEmission')
    emission.inputs['Strength'].default_value = 1.
    mix = _new(material, 'mix', 'ShaderNodeMixShader')
    for source, source_index, target, target_index in (
            (uv, 0, sub, 0), (sub, 0, div, 0), (div, 0, length, 0),
            (length, 1, inverse, 1), (inverse, 0, feather, 0),
            (feather, 0, squared, 0), (feather, 0, squared, 1),
            (feather, 0, twice, 0), (twice, 0, subtract, 1),
            (squared, 0, smooth, 0), (subtract, 0, smooth, 1),
            (smooth, 0, alpha, 0), (alpha, 0, mix, 0),
            (transparent, 0, mix, 1), (emission, 0, mix, 2), (mix, 0, output, 0)):
        tree.links.new(source.outputs[source_index], target.inputs[target_index])


def _apply(material, kind, settings):
    nodes = _nodes(material)
    if kind == 'SHADOW':
        nodes['transparent'].inputs['Color'].default_value = (
            *[1. - settings['opacity'] * (1. - channel) for channel in settings['tint']], 1.)
    else:
        nodes['uv'].uv_map = settings['uv_name']
        nodes['center'].inputs[1].default_value = (*settings['center'], 0.)
        nodes['radius'].inputs[1].default_value = (*settings['radius'], 1.)
        nodes['feather'].inputs[1].default_value = settings['feather']
        nodes['opacity'].inputs[1].default_value = settings['opacity']
        nodes['emission'].inputs['Color'].default_value = (*settings['color'], 1.)


def _close(actual, expected):
    if isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(_close(a, e) for a, e in zip(actual, expected))
    return math.isclose(float(actual), float(expected), rel_tol=2.e-7, abs_tol=2.e-8)


def _verify_settings(material, kind, settings):
    nodes = _nodes(material)
    if kind == 'SHADOW':
        expected = [1.-settings['opacity']*(1.-channel) for channel in settings['tint']] + [1.]
        _need(_close(nodes['transparent'].inputs['Color'].default_value, expected),
              'Eye shadow settings disagree with the actual shader')
    else:
        _need(nodes['uv'].uv_map == settings['uv_name'], 'Eye glint UV binding changed')
        for key, actual, expected in (
                ('center', nodes['center'].inputs[1].default_value, settings['center'] + [0.]),
                ('radius', nodes['radius'].inputs[1].default_value, settings['radius'] + [1.]),
                ('feather', nodes['feather'].inputs[1].default_value, settings['feather']),
                ('opacity', nodes['opacity'].inputs[1].default_value, settings['opacity']),
                ('color', nodes['emission'].inputs['Color'].default_value, settings['color'] + [1.])):
            _need(_close(actual, expected), 'Eye glint parameter changed: ' + key)


def create(kind, name, *, tint=(.7, .7, .7), opacity=.35, uv_name='UV贴图',
           center=(.1092628, .8924619), radius=(.0862841, .0830364),
           feather=.18, color=(1., .98, .96)):
    """Create one independent material; all colors are scene-linear RGB."""
    import bpy
    _need(type(kind) is str and kind in _KINDS, 'Eye overlay kind must be SHADOW or GLINT')
    _need(type(name) is str and bool(name.strip()) and '\x00' not in name,
          'A nonempty material name is required')
    arguments = dict(tint=tint, opacity=opacity, uv_name=uv_name, center=center,
                     radius=radius, feather=feather, color=color)
    # Reject invalid supplied values even when a particular kind does not use
    # them; callers never get silent acceptance of NaN or bool parameters.
    normalized = {mode: _normalize(mode, {key: arguments[key] for key in _KEYS[mode]})
                  for mode in _KINDS}
    settings = normalized[kind]
    material = bpy.data.materials.new(name)
    try:
        material.use_nodes = True
        material[PROFILE_KEY] = PROFILE
        material[_OWNER] = str(uuid.uuid4())
        material[_KIND] = kind
        material.surface_render_method = 'BLENDED'
        # Names differ across Blender versions. The parent runtime must qualify
        # the actual RNA semantics before admitting this material for rendering.
        for attribute in ('show_transparent_back', 'use_transparency_overlap'):
            if hasattr(material, attribute):
                setattr(material, attribute, False)
        material.use_backface_culling = False
        _build(material, kind)
        _apply(material, kind, settings)
        material[_SETTINGS] = _json(settings)
        material[_SIGNATURE] = _signature(material)
        validate(material)
        return material
    except Exception:
        bpy.data.materials.remove(material)
        raise


def is_present(material):
    return material is not None and PROFILE_KEY in material


def validate(material):
    """Reject changed graphs and return a JSON-native optical descriptor."""
    _local(material)
    _need(material.get(PROFILE_KEY) == PROFILE, 'Unknown or missing eye overlay profile')
    owner = material.get(_OWNER)
    try:
        _need(type(owner) is str and uuid.UUID(owner).int != 0, 'Invalid eye overlay owner')
    except (ValueError, AttributeError, TypeError) as error:
        raise EyeOverlayError('Invalid eye overlay owner') from error
    import bpy
    _need([m for m in bpy.data.materials if m.get(_OWNER)==owner] == [material],
          'Duplicated eye overlay material identity')
    kind = material.get(_KIND)
    _need(type(kind) is str and kind in _KINDS, 'Invalid eye overlay kind')
    try:
        settings = _normalize(kind, json.loads(material[_SETTINGS]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise EyeOverlayError('Invalid eye overlay settings document') from error
    _need(material.surface_render_method == 'BLENDED', 'Colored eye layers require BLENDED')
    signature = _signature(material)
    _need(signature == material.get(_SIGNATURE), 'Owned eye overlay shader was edited')
    _verify_settings(material, kind, settings)
    return dict(profile=PROFILE, owner=owner, material=material.name, kind=kind,
                settings=settings, signature=signature, color_space='scene_linear_Rec709',
                render_method='BLENDED',
                overlap={key: getattr(material, key) for key in
                         ('show_transparent_back', 'use_transparency_overlap') if hasattr(material, key)},
                formula=('T=1-opacity*(1-tint); E=0; result=T*background' if kind == 'SHADOW' else
                         'r=length((uv-center)/radius); t=clamp((1-r)/feather,0,1); '
                         'a=opacity*t*t*(3-2*t); E=a*color; T=1-a; result=E+T*background'),
                limitations=['static, unlit, nonrefractive eye layers only',
                             'material validation does not qualify line depth or scene transparency',
                             'caller must validate UV existence, polygon binding and render ordering'])


def update(material, **settings):
    """Update only admitted constants, preserving graph and material identity."""
    previous = validate(material)
    _need(bool(settings) and set(settings) <= _KEYS[previous['kind']],
          'Unknown or empty eye overlay update')
    updated = _normalize(previous['kind'], {**previous['settings'], **settings})
    old_settings, old_signature = material[_SETTINGS], material[_SIGNATURE]
    try:
        _apply(material, previous['kind'], updated)
        material[_SETTINGS] = _json(updated)
        material[_SIGNATURE] = _signature(material)
        return validate(material)
    except Exception:
        _apply(material, previous['kind'], previous['settings'])
        material[_SETTINGS], material[_SIGNATURE] = old_settings, old_signature
        raise
