# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned distance-key extensions of an existing HM lit_raw seam.

v1 retains its two-atlas horizontal contract. Explicit v2 adds a +30-degree
ring and unique top/bottom keys in a third atlas, using actual head elevation.
The old HM, fused highlight and fixed-shadow interfaces are preserved.
"""
import hashlib
import json
import math
import re
import struct
import uuid

from . import hair_map_nodes as N, hair_sdf_nodes as D

PROFILE = 'toon-hair-sdf.v1'
SCHEMA = 'toon-hair-sdf-descriptor.v1'
PROFILE_V2 = 'toon-hair-sdf.v2'
SCHEMA_V2 = 'toon-hair-sdf-descriptor.v2'
PREFIX = 'toon_hair_sdf_'
PROFILE_KEY = PREFIX + 'profile'
_OWNER = PREFIX + 'owner'
_TARGET = PREFIX + 'target'
_REFERENCE = PREFIX + 'reference'
_CARRIER = PREFIX + 'carrier'
_IMAGES = (PREFIX + 'image0', PREFIX + 'image1')
_DECLARED = PREFIX + 'declared'
_KEYS = {PROFILE_KEY, _OWNER, _TARGET, _REFERENCE, _CARRIER, _DECLARED, *_IMAGES}
_IMAGES_V2 = (*_IMAGES, PREFIX + 'image2')
_KEYS_V2 = _KEYS | {_IMAGES_V2[2]}
_SAMPLING = dict(interpolation='Linear', extension='EXTEND', projection='FLAT',
                 projection_blend=0., mode='RGB', alpha_mode='CHANNEL_PACKED')
_DESCRIPTOR_KEYS = {'schema', 'profile', 'owner', 'parent_hm_owner', 'target',
    'source_mesh', 'uv', 'images', 'reference', 'carrier', 'settings', 'sampling',
    'key_phases', 'formula', 'shader_signature'}
FORMULA = ('wi=max(1-8*abs(fract(phase-i/8+0.5)-0.5),0); '
           'shade=smoothstep(0.5-softness,0.5+softness,sum(wi*atlas_i)); '
           'effective=mix(old_lit_raw,shade,strength*valid)')
KEY_ELEVATIONS_V2 = [30.] * 8 + [90., -90.]
ATLAS_LAYOUT_V2 = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 'constant_1', 'constant_1']]
FORMULA_V2 = ('ring=sum(max(1-8*abs(fract(phase-i/8+0.5)-0.5),0)*atlas_i); '
    'e=clamp(elevation,-pi/2,pi/2); e<=pi/6:distance=mix(bottom,ring,clamp((e+pi/2)/(2*pi/3),0,1)); '
    'e>=pi/6:distance=mix(ring,top,clamp((e-pi/6)/(pi/3),0,1)); '
    'shade=smoothstep(0.5-softness,0.5+softness,distance); '
    'effective=mix(old_lit_raw,shade,strength*valid); valid=1 for admitted complete direction')


def _mode(value):
    return D.MODE_V2 if value['profile'] == PROFILE_V2 else D.MODE_V1


def _image_keys(value):
    return _IMAGES_V2 if value['profile'] == PROFILE_V2 else _IMAGES


def _metadata_keys(value):
    return _KEYS_V2 if value['profile'] == PROFILE_V2 else _KEYS


class HairSDFError(ValueError):
    """Unsupported cyclic SDF source, altered footprint or invalid live frame."""


def _need(value, message):
    if not value:
        raise HairSDFError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _original(value):
    return getattr(value, 'original', value)


def _sdf():
    from . import face_sdf
    return face_sdf


def _name(value):
    return type(value) is str and 0 < len(value) <= 1024 and '\x00' not in value


def _is_sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _is_owner(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None and int(value, 16) != 0


def _settings(strength, softness):
    _need(all(type(v) in (int, float) and math.isfinite(v) for v in (strength, softness))
          and 0. <= strength <= 1. and (1.e-5 <= softness <= .25 or softness == _f32(1.e-5)),
          'Hair SDF requires finite non-boolean strength [0,1] and softness [1e-5,.25]')
    result = dict(strength=_f32(strength), softness=_f32(softness))
    _need(_f32(.5 - result['softness']) < _f32(.5 + result['softness']),
          'Hair SDF transition collapses at native float32 precision')
    return result


def present(group):
    """Incomplete reserved state is present and must be rejected, not ignored."""
    return group is not None and (any(str(key).startswith(PREFIX) for key in group.keys())
        or any(str(n.get(N.NODE, '')).startswith(D.PREFIX) for n in group.nodes))


def validate_receipt_payload(value):
    """Bounded bpy-free reader for the child descriptor carried by HM v2 freeze."""
    from . import sdf_witness as W
    omni = type(value) is dict and value.get('profile') == PROFILE_V2
    expected = _DESCRIPTOR_KEYS | {'key_elevations', 'atlas_layout'} if omni else _DESCRIPTOR_KEYS
    _need(type(value) is dict and set(value) == expected,
          'Hair SDF descriptor fields are incomplete or unknown')
    _need((value['schema'], value['profile']) == ((SCHEMA_V2, PROFILE_V2) if omni else (SCHEMA, PROFILE))
          and _is_owner(value['owner']) and _is_owner(value['parent_hm_owner']),
          'Hair SDF descriptor profile/owner is invalid')
    target, mesh, uv = value['target'], value['source_mesh'], value['uv']
    _need(type(target) is dict and set(target) == {'name', 'type'}
          and _name(target['name']) and target['type'] == 'MESH', 'Invalid Hair SDF target')
    _need(type(mesh) is dict and set(mesh) == {'name', 'vertices', 'corners'}
          and _name(mesh['name']) and all(type(mesh[k]) is int and 0 < mesh[k] <= 100000000
            for k in ('vertices', 'corners')), 'Invalid Hair SDF source Mesh')
    _need(type(uv) is dict and _name(uv.get('uv_name')), 'Hair SDF UV name is missing')
    W._uv(uv, uv['uv_name'])
    _need(uv['vertices'] == mesh['vertices'] and uv['corners'] == mesh['corners']
          and uv['count'] == mesh['corners'], 'Hair SDF UV extent differs from source Mesh')
    images = value['images']
    _need(type(images) is list and len(images) == (3 if omni else 2), 'Hair SDF atlas count differs from profile')
    for image in images:
        _need(type(image) is dict and all(k in image for k in ('alpha_mode', 'rgba_min', 'rgba_max')),
              'Hair SDF atlas alpha/range descriptor is missing')
        clean = {k: v for k, v in image.items() if k not in {'alpha_mode', 'rgba_min', 'rgba_max'}}
        W._image(clean)
        _need(image['alpha_mode'] == 'CHANNEL_PACKED' and image['channels'] == 4
              and image['width'] == image['height'] and 2 <= image['width'] <= 2048
              and _name(image['name']) and _is_sha(image['rgba_sha256'])
              and all(type(image[k]) in (int, float) and math.isfinite(image[k]) for k in ('rgba_min', 'rgba_max'))
              and 0. <= image['rgba_min'] <= image['rgba_max'] <= 1.,
              'Hair SDF atlases require finite packed RGBA square data in [0,1]')
    _need(len({im['name'] for im in images}) == len(images) and len({im['width'] for im in images}) == 1,
          'Hair SDF atlases must be distinct IDs with matching dimensions')
    reference = value['reference']
    _need(type(reference) is dict and set(reference) == {'name', 'type'}
          and _name(reference['name']) and _name(reference['type']), 'Invalid Hair SDF head reference')
    carrier = value['carrier']
    _need(type(carrier) is dict and set(carrier) == {'group', 'profile', 'owner', 'uid', 'graph_signature', 'attributes'}
          and _name(carrier['group']) and carrier['profile'] == (D.CARRIER_PROFILE_V2 if omni else D.CARRIER_PROFILE)
          and carrier['owner'] == value['owner'] and type(carrier['uid']) is int and carrier['uid'] > 0
          and _is_sha(carrier['graph_signature']), 'Invalid Hair SDF direction carrier')
    fields = carrier['attributes']
    _need(type(fields) is dict and set(fields) == ({'phase', 'valid', 'elevation'} if omni else {'phase', 'valid'})
          and all(_name(v) for v in fields.values()) and len(set(fields.values())) == len(fields),
          'Invalid Hair SDF direction field names')
    settings = value['settings']
    _need(type(settings) is dict and set(settings) == {'strength', 'softness'}
          and settings == _settings(settings['strength'], settings['softness']),
          'Hair SDF native settings are invalid or unrounded')
    _need(value['sampling'] == _SAMPLING and value['key_phases'] == D.KEY_PHASES
          and value['formula'] == (FORMULA_V2 if omni else FORMULA) and _is_sha(value['shader_signature']),
          'Hair SDF sampling/keys/formula/shader declaration changed')
    if omni:
        _need(value['key_elevations'] == KEY_ELEVATIONS_V2 and value['atlas_layout'] == ATLAS_LAYOUT_V2,
              'Omni SDF ring elevation, pole order or padding contract changed')
    # Reject nonfinite values nested in foreign image/UV JSON as well.
    _need(len(_json(value)) <= 65536, 'Hair SDF descriptor exceeds its bounded JSON size')
    return value


def _declaration(group):
    omni = group.get(PROFILE_KEY) == PROFILE_V2
    _need(group.get(PROFILE_KEY) in (PROFILE, PROFILE_V2)
          and {str(k) for k in group.keys() if str(k).startswith(PREFIX)} == (_KEYS_V2 if omni else _KEYS),
          'Hair SDF reserved metadata is incomplete or unsupported')
    raw = group.get(_DECLARED)
    _need(type(raw) is str and len(raw) <= 65536, 'Hair SDF declaration is not bounded JSON')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, 'Duplicate Hair SDF declaration field')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError) as error:
        raise HairSDFError('Hair SDF declaration cannot be read') from error
    _need(type(value) is dict and value.get('profile') == group.get(PROFILE_KEY),
          'Hair SDF metadata/declaration profiles differ')
    return validate_receipt_payload(value)


def _image_descriptor(image, group=None, index=None, *, pole=False):
    import numpy as np
    from . import face_sdf as T
    result = T._image_content_descriptor(image, 'R')
    _need(image.channels == 4 and image.alpha_mode == 'CHANNEL_PACKED'
          and not getattr(image, 'animation_data', None) and image.size[0] == image.size[1]
          and 2 <= image.size[0] <= 2048, 'Hair SDF requires static packed RGBA square atlases <=2048')
    values = np.empty(len(image.pixels), dtype='<f4')
    image.pixels.foreach_get(values)
    _need(np.isfinite(values).all() and float(values.min()) >= 0. and float(values.max()) <= 1.,
          'Every Hair SDF RGBA channel must be finite in [0,1]')
    if pole or index == 2:
        _need(np.all(values.reshape(-1, 4)[:, 2:] == 1.), 'Omni pole atlas B/A must be constant one')
    allowed = set()
    expected = int(image.use_fake_user)
    if group is not None:
        allowed.add(group)
        textures = [n for n in group.nodes if n.bl_idname == 'ShaderNodeTexImage'
                    and _original(n.image) == _original(image)]
        image_keys = _IMAGES_V2 if group.get(PROFILE_KEY) == PROFILE_V2 else _IMAGES
        _need(index in range(len(image_keys)) and group.get(image_keys[index]) == image
              and len(textures) == 1 and textures[0].get(N.NODE) == D.PREFIX + 'atlas' + str(index),
              'Hair SDF atlas must own exactly one group pointer and one sampler')
        expected += 2
    _need(not image.use_extra_user and image.users == expected
          and not (T._image_consumers(image) - allowed),
          'Hair SDF requires dedicated Image IDs; do not reuse HM, B, mask or other consumers')
    return dict(result, alpha_mode=image.alpha_mode,
                rgba_min=float(values.min()), rgba_max=float(values.max()))


def _reference(reference, *, depsgraph=None):
    import bpy
    value = _original(reference)
    _need(isinstance(value, bpy.types.Object) and bpy.data.objects.get(value.name) == value
          and not value.library and not value.override_library and value.is_editable,
          'Hair SDF needs an explicit local head reference Object')
    matrix = value.evaluated_get(depsgraph).matrix_world if depsgraph is not None else value.matrix_world
    D._matrix(matrix)
    return value


def _target(group, explicit, declared, frozen):
    import bpy
    from . import hair_map as H
    obj = _original(group.get(_TARGET))
    _need(_original(explicit) == obj and isinstance(obj, bpy.types.Object)
          and bpy.data.objects.get(obj.name) == obj and obj.type == 'MESH'
          and not obj.library and not obj.override_library and obj.is_editable
          and _original(group.get(H._TARGET)) == obj,
          'Hair SDF target differs from its explicit parent HM Mesh')
    mesh = bpy.data.meshes.get(declared['source_mesh']['name'])
    _need(mesh is not None and not mesh.library and not mesh.override_library and mesh.is_editable,
          'Hair SDF retained original Mesh is missing or linked')
    if not frozen:
        _need(obj.data == mesh and mesh.users == 1, 'Hair SDF author target no longer has its exclusive source Mesh')
    _need(declared['target'] == dict(name=obj.name_full, type='MESH')
          and declared['source_mesh'] == dict(name=mesh.name_full, vertices=len(mesh.vertices), corners=len(mesh.loops)),
          'Hair SDF target or source Mesh identity changed')
    uv = _sdf()._uv_descriptor(mesh, declared['uv']['uv_name'])
    _sdf()._uv_shape(uv, len(mesh.vertices))
    _need(uv == declared['uv'], 'Hair SDF source UV bytes changed')
    return obj


def _carrier(group, target, declared, frozen):
    import bpy
    from . import hair_map as H
    carrier = _original(group.get(_CARRIER))
    reference = _reference(group.get(_REFERENCE))
    sun = _original(group.get(H._SUN))
    source = declared['carrier']
    _need(isinstance(carrier, bpy.types.GeometryNodeTree)
          and not carrier.library and not carrier.override_library and carrier.is_editable
          and carrier.animation_data is None and not carrier.use_fake_user and not carrier.use_extra_user,
          'Hair SDF direction carrier is missing, foreign or animated')
    _need(carrier.get('profile') == source['profile'] and carrier.get('owner') == declared['owner']
          and carrier.get('target') == target and carrier.get('reference') == reference
          and carrier.get('sun') == sun and carrier.get('uid') == source['uid']
          and json.loads(carrier.get('fields', '{}')) == source['attributes']
          and carrier.get('signature') == source['graph_signature'] == D.carrier_signature(carrier)
          and carrier.name == source['group'], 'Hair SDF direction carrier identity or graph changed')
    infos = {node.get(D.CARRIER_NODE): node for node in carrier.nodes if node.bl_idname == 'GeometryNodeObjectInfo'}
    _need(set(infos) == {'reference', 'sun'}
          and _original(N._socket(infos['reference'].inputs, 'Object').default_value) == reference
          and _original(N._socket(infos['sun'].inputs, 'Object').default_value) == sun,
          'Hair SDF carrier lost its live reference/Sun Object links')
    expected = 1 + (0 if frozen else 1)
    consumers = bpy.data.user_map(subset={carrier}).get(carrier, set())
    _need(carrier.users == expected and consumers.issubset({group, target}),
          'Hair SDF direction carrier acquired another consumer')
    if not frozen:
        mods = [m for m in target.modifiers if m.type == 'NODES' and m.node_group == carrier]
        _need(len(mods) == 1 and mods[0].persistent_uid == source['uid']
              and mods[0].show_render and mods[0].show_viewport,
              'Hair SDF carrier modifier was replaced or disabled')
        from . import camera_correction
        # The new modifier is inserted at zero; any correction must stay later.
        index = list(target.modifiers).index(mods[0])
        corrections = [i for i, m in enumerate(target.modifiers)
                       if m.type == 'NODES' and m.node_group
                       and m.node_group.get(camera_correction.PROFILE_KEY) == camera_correction.PROFILE]
        _need(all(index < i for i in corrections), 'Hair SDF carrier moved behind camera correction')
        _need(not any(name in target.data.attributes for name in source['attributes'].values()),
              'Hair SDF outputs conflict with raw Mesh attributes')
    _need(declared['reference'] == dict(name=reference.name_full, type=reference.type),
          'Hair SDF reference identity changed')
    D.direction_frame(reference.matrix_world, sun.matrix_world, mode=_mode(declared))
    return carrier, reference, sun


def _shader(group, value):
    _need(value['shader_signature'] == D.extension_signature(group),
          'Hair SDF flat node footprint or seam was edited')
    _need(D.shader_parameters(group) == value['settings'], 'Hair SDF settings differ from declared values')
    raw, destinations = D.seam(group, extended=True)
    effective = N._socket(N._node(group, D.PREFIX + 'effective').outputs, 'Value')
    _need(len(effective.links) == 2 and all(len(s.links) == 1
          and s.links[0].from_socket == effective for s in destinations),
          'Hair SDF no longer owns exactly the two HM lit_raw consumers')
    old = N._node(group, D.PREFIX + 'old')
    _need(len(raw.links) == 1 and len(N._socket(old.inputs, 'Value').links) == 1
          and N._socket(old.inputs, 'Value').links[0].from_socket == raw,
          'Original HM lit_raw branch was changed')
    uv = N._node(group, D.PREFIX + 'uv')
    _need(uv.bl_idname == 'ShaderNodeUVMap' and uv.uv_map == value['uv']['uv_name'] and not uv.from_instancer,
          'Hair SDF explicit UV sampler changed')
    for i, image_key in enumerate(_image_keys(value)):
        node = N._node(group, D.PREFIX + 'atlas' + str(i))
        _need(node.bl_idname == 'ShaderNodeTexImage' and _original(node.image) == _original(group.get(image_key))
              and all(getattr(node, k) == v for k, v in _SAMPLING.items() if k not in {'mode', 'alpha_mode'}),
              'Hair SDF atlas reference or sampler changed')
    for key, name in value['carrier']['attributes'].items():
        node = N._node(group, D.PREFIX + key)
        _need(node.bl_idname == 'ShaderNodeAttribute' and node.attribute_type == 'GEOMETRY'
              and node.attribute_name == name, 'Hair SDF direction reader changed')


def describe(group, target, *, frozen=False):
    """Validate child data without recursively calling parent H._owned."""
    if not present(group):
        return None
    N._group(group)
    value = _declaration(group)
    _need(group.get(_OWNER) == value['owner'] and value['parent_hm_owner'] == group[N.OWNER],
          'Hair SDF child/parent owner changed')
    target = _target(group, target, value, frozen)
    _carrier(group, target, value, frozen)
    _shader(group, value)
    images = []
    image_keys = _image_keys(value)
    for i, key in enumerate(image_keys):
        current = _image_descriptor(_original(group.get(key)), group, i)
        _need(current == value['images'][i], 'Hair SDF atlas source, pixels or channel interpretation changed')
        images.append(current)
    _need(len({_original(group.get(k)) for k in image_keys}) == len(image_keys), 'Hair SDF atlas IDs were aliased')
    return validate_receipt_payload(dict(value, images=images))


def observe(group, target, depsgraph, *, frozen=False):
    """Actual RENDER-only child payload; parent owns the outer HM source record."""
    import numpy as np
    _need(depsgraph.mode == 'RENDER', 'Hair SDF requires the actual RENDER dependency graph')
    value = describe(group, target, frozen=frozen)
    _need(value is not None, 'Hair SDF observation requires an attached extension')
    reference = _original(group[_REFERENCE])
    from . import hair_map as H
    sun = _original(group[H._SUN])
    _reference(reference, depsgraph=depsgraph)
    rm, lm = reference.evaluated_get(depsgraph).matrix_world, sun.evaluated_get(depsgraph).matrix_world
    oracle = D.direction_frame(rm, lm, mode=_mode(value))
    ev = target.evaluated_get(depsgraph)
    mesh = ev.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        uv = mesh.uv_layers.get(value['uv']['uv_name'])
        _need(uv is not None, 'Hair SDF actual RENDER UV is missing')
        coordinates = np.empty((len(mesh.loops), 2), dtype='<f4')
        uv.data.foreach_get('uv', coordinates.ravel())
        _need(np.isfinite(coordinates).all(), 'Hair SDF actual RENDER UV is nonfinite')
        result = dict(uv=dict(uv_name=uv.name, shape=list(coordinates.shape),
                             sha256=hashlib.sha256(coordinates.tobytes()).hexdigest()))
        maximum = {}
        for key, name in value['carrier']['attributes'].items():
            attr = mesh.attributes.get(name)
            _need(attr is not None and attr.domain == 'POINT' and attr.data_type == 'FLOAT'
                  and len(attr.data) == len(mesh.vertices) and len(attr.data) > 0,
                  'Hair SDF actual RENDER direction field is missing or has the wrong domain/type')
            data = np.empty((len(mesh.vertices), 1), dtype='<f4')
            attr.data.foreach_get('value', data.ravel())
            lo, hi = (-math.pi/2., math.pi/2.) if key == 'elevation' else (0., 1.)
            _need(np.isfinite(data).all() and float(data.min()) >= lo-2.e-6 and float(data.max()) <= hi+2.e-6,
                  'Hair SDF actual RENDER field is nonfinite or outside its profile range')
            delta = np.abs(data.astype(np.float64) - oracle[key])
            if key == 'phase':
                delta = np.minimum(delta, np.abs(1. - delta))
            maximum[key] = float(delta.max())
            _need(maximum[key] <= 2.e-5, 'Hair SDF actual RENDER field differs from its live head/Sun oracle')
            result[key] = dict(attribute=name, shape=list(data.shape),
                               sha256=hashlib.sha256(data.tobytes()).hexdigest())
        # Confirm actual evaluated constants, not only a main-database cache.
        candidates = [slot.material for slot in ev.material_slots if slot.material is not None
                      and _original(slot.material).get(H.KEY) == group]
        _need(candidates, 'Hair SDF evaluated HM material is missing')
        groups = [n.node_tree for n in candidates[0].node_tree.nodes
                  if n.bl_idname == 'ShaderNodeGroup' and _original(n.node_tree) == group]
        _need(len(groups) == 1 and D.shader_parameters(groups[0].evaluated_get(depsgraph)) == value['settings'],
              'Hair SDF main and actual evaluated constants disagree')
        result.update(reference_world=[list(row) for row in rm], sun_world=[list(row) for row in lm],
                      oracle={key: oracle[key] for key in value['carrier']['attributes']},
                      settings=value['settings'], oracle_max_error=maximum)
        return result
    finally:
        ev.to_mesh_clear()


def _context(context):
    obj = context.object
    _need(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT'
          and context.scene.objects.get(obj.name) == obj and obj.material_slots,
          'Choose an actual local Mesh material slot in Object Mode')
    return context.scene, obj, obj.active_material_index


def _sign(group):
    from . import hair_map as H
    group[H._SHAPE] = _sha(N.structure(group))


def _styles(group):
    return {n.get(N.NODE): {p: (tuple(getattr(n, p)) if p in {'location', 'color'} else getattr(n, p))
            for p in ('name', 'label', 'location', 'width', 'hide', 'select', 'use_custom_color', 'color')}
            for n in group.nodes if str(n.get(N.NODE, '')).startswith(D.PREFIX)}


def _remove_shader(group):
    raw, destinations = D.seam(group, extended=True)
    for destination in destinations:
        group.links.new(raw, destination)
    for node in list(group.nodes):
        if str(node.get(N.NODE, '')).startswith(D.PREFIX):
            group.nodes.remove(node)


def attach(context, images, uv_name, reference, strength=0., softness=.002, *, mode=D.MODE_V1, pole_image=None):
    """Attach disabled by default; no source Image/UV or HM/B setting is changed."""
    import bpy
    from . import hair_map as H
    scene, target, index = _context(context)
    _, _, group, _, _, _ = H._owned(scene, target, index)
    _need(not present(group), 'Hair SDF is already present or has damaged reserved state')
    _need(mode in (D.MODE_V1, D.MODE_V2), 'Choose explicit cyclic_v1 or omni_v2 mode')
    _need(type(images) in (list, tuple), 'Explicit Hair SDF atlas Image list required')
    images = list(images)
    if mode == D.MODE_V2 and pole_image is not None:
        _need(len(images) == 2, 'Provide two ring atlases plus pole_image, or three atlases, not both')
        images.append(pole_image)
    _need((mode == D.MODE_V2 or pole_image is None) and len(images) == (3 if mode == D.MODE_V2 else 2)
          and len(set(images)) == len(images), 'Atlas count/identity differs from explicit Hair SDF mode')
    omni = mode == D.MODE_V2
    profile, schema = (PROFILE_V2, SCHEMA_V2) if omni else (PROFILE, SCHEMA)
    image_keys, metadata_keys = (_IMAGES_V2, _KEYS_V2) if omni else (_IMAGES, _KEYS)
    settings = _settings(strength, softness)
    _need(target.data.users == 1, 'Hair SDF requires the exclusive original Mesh')
    reference = _reference(reference)
    _need(reference != target, 'Hair SDF head reference cannot be its target Mesh')
    sun = _original(group[H._SUN])
    graph = context.evaluated_depsgraph_get()
    D.direction_frame(reference.evaluated_get(graph).matrix_world, sun.evaluated_get(graph).matrix_world, mode=mode)
    image_values = [_image_descriptor(image, pole=(omni and i == 2)) for i, image in enumerate(images)]
    _need(len({im['width'] for im in image_values}) == 1, 'Hair SDF atlas dimensions differ')
    uv = _sdf()._uv_descriptor(target.data, uv_name)
    _sdf()._uv_shape(uv, len(target.data.vertices))
    D.seam(group)
    owner = uuid.uuid4().hex
    fields = dict(phase='toon_hair_sdf_phase_' + owner, valid='toon_hair_sdf_valid_' + owner)
    if omni:
        fields['elevation'] = 'toon_hair_sdf_elevation_' + owner
    _need(not any(name in target.data.attributes for name in fields.values()), 'Hair SDF output names already exist')
    previous = group[H._SHAPE]
    modifier, carrier = None, None
    try:
        modifier, carrier = D.build_carrier(target, reference, sun, owner, fields, mode=mode)
        group[PROFILE_KEY], group[_OWNER], group[_TARGET] = profile, owner, target
        group[_REFERENCE], group[_CARRIER] = reference, carrier
        for key, image in zip(image_keys, images):
            group[key] = image
        D.create_shader(group, images, uv_name, fields, mode=mode, **settings)
        value = dict(schema=schema, profile=profile, owner=owner, parent_hm_owner=group[N.OWNER],
            target=dict(name=target.name_full, type='MESH'),
            source_mesh=dict(name=target.data.name_full, vertices=len(target.data.vertices), corners=len(target.data.loops)),
            uv=uv, images=[_image_descriptor(image, group, i) for i, image in enumerate(images)],
            reference=dict(name=reference.name_full, type=reference.type),
            carrier=dict(group=carrier.name, profile=carrier['profile'], owner=owner,
                         uid=modifier.persistent_uid, graph_signature=carrier['signature'], attributes=fields),
            settings=D.shader_parameters(group), sampling=dict(_SAMPLING), key_phases=list(D.KEY_PHASES),
            formula=FORMULA_V2 if omni else FORMULA, shader_signature=D.extension_signature(group))
        if omni:
            value.update(key_elevations=list(KEY_ELEVATIONS_V2), atlas_layout=ATLAS_LAYOUT_V2)
        group[_DECLARED] = _json(validate_receipt_payload(value))
        _sign(group)
        return describe(group, target)
    except BaseException:
        _remove_shader(group)
        for key in metadata_keys:
            if key in group:
                del group[key]
        if modifier is not None:
            target.modifiers.remove(modifier)
        if carrier is not None and carrier.users == 0:
            bpy.data.node_groups.remove(carrier)
        group[H._SHAPE] = previous
        raise


def update(context, strength, softness):
    """Update this child's two native constants; all other authors stay intact."""
    from . import hair_map as H
    scene, target, index = _context(context)
    _, _, group, _, _, _ = H._owned(scene, target, index)
    before = describe(group, target)
    _need(before is not None, 'Current HM has no Hair SDF extension')
    desired = _settings(strength, softness)
    old_shape, old_raw = group[H._SHAPE], group[_DECLARED]
    try:
        native = D.write_shader_parameters(group, **desired)
        _need(native == desired, 'Hair SDF native parameter readback differs')
        value = dict(before, settings=native, shader_signature=D.extension_signature(group))
        group[_DECLARED] = _json(validate_receipt_payload(value))
        _sign(group)
        return describe(group, target)
    except BaseException:
        D.write_shader_parameters(group, **before['settings'])
        group[_DECLARED], group[H._SHAPE] = old_raw, old_shape
        raise


def remove(context):
    """Restore original lit_raw edges, then reclaim only this child's carrier."""
    import bpy
    from . import hair_map as H
    scene, target, index = _context(context)
    _, _, group, _, _, _ = H._owned(scene, target, index)
    value = describe(group, target)
    _need(value is not None, 'Current HM has no Hair SDF extension')
    carrier, _, _ = _carrier(group, target, value, False)
    mods = [m for m in target.modifiers if m.type == 'NODES' and m.node_group == carrier]
    _need(len(mods) == 1, 'Hair SDF carrier modifier is unavailable')
    modifier = mods[0]
    metadata_keys = _metadata_keys(value)
    styles, metadata = _styles(group), {key: group[key] for key in metadata_keys}
    previous = group[H._SHAPE]
    images = [group[key] for key in _image_keys(value)]
    try:
        _remove_shader(group)
        for key in metadata_keys:
            del group[key]
        _sign(group)
        _need(not present(group), 'Hair SDF cleanup left reserved shader state')
        D.seam(group)
        # Native geometry/data stays in place. Only an owned no-geometry-change
        # modifier and its exclusively retained Group are reclaimed last.
        target.modifiers.remove(modifier)
        _need(carrier.users == 0, 'Hair SDF carrier remains referenced after detach')
        bpy.data.node_groups.remove(carrier)
        return dict(profile=value['profile'], owner=value['owner'], removed=True, restored=True)
    except BaseException as error:
        try:
            # A deleted native ID cannot be re-created with its former identity.
            # Report that boundary honestly rather than forging an old UID.
            _need(carrier.name in bpy.data.node_groups, 'Hair SDF carrier ID already deleted')
            _need(any(m == modifier for m in target.modifiers), 'Hair SDF modifier already removed')
            _remove_shader(group)
            for key, item in metadata.items():
                group[key] = item
            D.create_shader(group, images, value['uv']['uv_name'], value['carrier']['attributes'],
                            styles=styles, mode=_mode(value), **value['settings'])
            group[H._SHAPE] = previous
            describe(group, target)
        except BaseException as rollback_error:
            raise RuntimeError('Hair SDF removal failed and rollback is incomplete: ' + str(rollback_error)) from error
        raise
