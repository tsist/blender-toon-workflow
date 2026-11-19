# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional R-mask fusion inside an existing HM color layer.

The mask shapes the already illuminated/view-dependent HM highlight weight.
It does not add another light, band, Emission or highlight response.  Existing
HM nodes remain intact; detaching restores their exact original output edge.
"""
import json
import math
import re
import struct

from . import hair_map_nodes as N

PROFILE = 'toon-hair-highlight-mask.v1'
SCHEMA = 'toon-hair-highlight-mask-descriptor.v1'
PREFIX = 'toon_hm_highlight_mask_'
PROFILE_KEY = PREFIX + 'profile'
BINDING = PROFILE_KEY
_TARGET = PREFIX + 'target'
_IMAGE = PREFIX + 'image'
_DECLARED = PREFIX + 'declared'
_KEYS = {PROFILE_KEY, _TARGET, _IMAGE, _DECLARED}
_PREFIX = 'fused_mask:'
_KINDS = {
    'fused_mask:uv': 'ShaderNodeUVMap',
    'fused_mask:texture': 'ShaderNodeTexImage',
    'fused_mask:channels': 'ShaderNodeSeparateColor',
    'fused_mask:shift': 'ShaderNodeMath',
    'fused_mask:coverage_scale': 'ShaderNodeMath',
    'fused_mask:coverage_lower': 'ShaderNodeMath',
    'fused_mask:coverage_upper': 'ShaderNodeMath',
    'fused_mask:weight': 'ShaderNodeMath',
    'fused_mask:lower': 'ShaderNodeMath',
    'fused_mask:upper': 'ShaderNodeMath',
    'fused_mask:color': 'ShaderNodeMixRGB',
}
_SAMPLING = dict(interpolation='Linear', extension='EXTEND', projection='FLAT',
                 projection_blend=0., mode='RGB')
FORMULA = ('mask_coverage = clamp((mask.R - black) / (white - black), 0, 1); '
           'mix(palette, highlight_color, clamp(mask_coverage * existing_HM_highlight_weight, 0, 1))')
_DESCRIPTOR_KEYS = {'schema', 'profile', 'owner', 'target', 'source_mesh',
                    'uv', 'image', 'sampling', 'channel', 'formula', 'levels'}
_IMAGE_KEYS = {
    'name', 'source_kind', 'packed', 'filepath', 'file_sha256',
    'packed_sha256', 'source_sha256', 'width', 'height', 'channels', 'depth',
    'is_float', 'precision', 'colorspace', 'single_tile', 'channel',
    'channel_index', 'pixel_count', 'pixel_sha256', 'rgba_sha256', 'min', 'max',
}
_UV_KEYS = {'uv_name', 'domain', 'data_type', 'vertices', 'corners',
            'count', 'sha256', 'extent'}


class HairHighlightMaskError(ValueError):
    """Invalid owner/source, altered fusion graph, or incompatible consumers."""


def _need(condition, message):
    if not condition:
        raise HairHighlightMaskError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _sdf():
    from . import face_sdf
    return face_sdf


def _original(value):
    return getattr(value, 'original', value)


def _name(value, *, path=False):
    return (type(value) is str and bool(value) and '\x00' not in value
            and len(value) <= (32768 if path else 1024))


def _count(value, *, positive=True):
    return type(value) is int and (1 if positive else 0) <= value <= 2147483647


def _float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _levels(black, white):
    _need(all(type(item) in (int, float) and math.isfinite(item) for item in (black, white))
          and 0. <= black < white <= 1.,
          'HM highlight-mask levels require finite non-boolean 0 <= black < white <= 1')
    # RNA stores constants as float32. A window whose endpoints collapse in
    # that representation cannot implement the declared author calibration.
    _need(_float32(black) < _float32(white) and _float32(white - black) > 0.,
          'HM highlight-mask levels collapse at native float32 precision')
    return dict(black=float(black), white=float(white))


def present(group):
    """Incomplete/unknown reserved markers are present and must be rejected."""
    return group is not None and (
        any(str(key).startswith(PREFIX) for key in group.keys())
        or any(str(node.get(N.NODE, '')).startswith(_PREFIX) for node in group.nodes))


def validate_receipt_payload(value):
    """Validate a bounded closed descriptor without Blender or filesystem I/O."""
    from . import sdf_witness as W
    _need(type(value) is dict and set(value) == _DESCRIPTOR_KEYS,
          'HM highlight-mask descriptor fields changed')
    _need(value['schema'] == SCHEMA and value['profile'] == PROFILE,
          'HM highlight-mask descriptor profile is unsupported')
    levels = value['levels']
    _need(type(levels) is dict and set(levels) == {'black', 'white'},
          'HM highlight-mask level fields changed')
    _levels(levels['black'], levels['white'])
    _need(type(value['owner']) is str and re.fullmatch('[0-9a-f]{32}', value['owner']) is not None
          and int(value['owner'], 16) != 0, 'HM highlight-mask owner must match a nonzero HM UUID')
    target, mesh = value['target'], value['source_mesh']
    _need(type(target) is dict and set(target) == {'name', 'type'}
          and _name(target['name']) and target['type'] == 'MESH',
          'HM highlight-mask target descriptor changed')
    _need(type(mesh) is dict and set(mesh) == {'name', 'vertices', 'corners'}
          and _name(mesh['name']) and _count(mesh['vertices']) and _count(mesh['corners']),
          'HM highlight-mask original Mesh descriptor changed')
    image, uv = value['image'], value['uv']
    _need(type(image) is dict and set(image) == _IMAGE_KEYS,
          'HM highlight-mask Image descriptor fields changed')
    W._image(image)
    _need(_name(image['name']) and (image['filepath'] is None or _name(image['filepath'], path=True))
          and all(_count(image[key]) for key in ('width', 'height', 'channels', 'depth'))
          and type(image['pixel_count']) is int and 1 <= image['pixel_count'] <= 9223372036854775807,
          'HM highlight-mask Image dimensions/name/path are invalid')
    _need(value['channel'] == image['channel'] == 'R' and image['channel_index'] == 0,
          'HM highlight-mask must sample Non-Color R')
    _need(type(uv) is dict and set(uv) == _UV_KEYS and _name(uv['uv_name']),
          'HM highlight-mask UV descriptor fields changed')
    W._uv(uv, uv['uv_name'])
    _need(uv['vertices'] == mesh['vertices'] and uv['corners'] == mesh['corners']
          and _count(uv['count']) and all(0. <= item <= 1.
              for bound in ('min', 'max') for item in uv['extent'][bound]),
          'HM highlight-mask original UV extent/count changed')
    sampling = value['sampling']
    _need(type(sampling) is dict and set(sampling) == set(_SAMPLING)
          and all(type(sampling[key]) is str for key in ('interpolation', 'extension', 'projection', 'mode'))
          and type(sampling['projection_blend']) in (int, float)
          and math.isfinite(sampling['projection_blend']) and sampling == _SAMPLING
          and type(value['formula']) is str and value['formula'] == FORMULA,
          'HM highlight-mask sampling/formula contract changed')
    return value


def _declaration(group):
    _need(group.get(PROFILE_KEY) == PROFILE, 'HM highlight-mask profile is missing or unsupported')
    _need({str(key) for key in group.keys() if str(key).startswith(PREFIX)} == _KEYS,
          'HM highlight-mask reserved metadata is incomplete or unknown')
    raw = group.get(_DECLARED)
    _need(type(raw) is str and len(raw) <= 65536, 'HM highlight-mask declaration is not bounded JSON')
    try:
        def unique(pairs):
            result = {}
            for key, item in pairs:
                _need(key not in result, 'Duplicate HM highlight-mask declaration key')
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError) as error:
        raise HairHighlightMaskError('HM highlight-mask declaration cannot be read') from error
    validate_receipt_payload(value)
    _need(value['owner'] == group.get(N.OWNER), 'HM highlight-mask owner differs from its HM Group')
    return value


def _target(group, explicit):
    import bpy
    from . import hair_map as H
    target = _original(group.get(_TARGET))
    _need(_original(explicit) == target and isinstance(target, bpy.types.Object)
          and target.type == 'MESH' and bpy.data.objects.get(target.name) == target
          and not target.library and not target.override_library and target.is_editable
          and target.data is not None and not target.data.library
          and not target.data.override_library and target.data.is_editable,
          'HM highlight-mask target reference is missing or differs from its explicit Mesh')
    _need(_original(group.get(H._TARGET)) == target,
          'HM highlight-mask target differs from its parent HM target')
    return target


def _image_descriptor(image, group=None):
    T = _sdf()
    content = T._image_content_descriptor(image, 'R')
    _need(not image.use_extra_user, 'HM highlight-mask Image has an extra user')
    expected, allowed = int(image.use_fake_user), set()
    if group is not None:
        allowed.add(group)
        textures = [node for node in group.nodes
                    if node.bl_idname == 'ShaderNodeTexImage' and node.image == image]
        _need(group.get(_IMAGE) == image and len(textures) == 1
              and textures[0].get(N.NODE) == 'fused_mask:texture',
              'HM highlight-mask must own one explicit Image pointer and one Texture reference')
        expected += 2
    _need(image.users == expected and not (T._image_consumers(image) - allowed),
          'HM highlight-mask requires a dedicated Non-Color Image ID without other consumers')
    return content


def _nodes(group):
    result = {}
    for node in group.nodes:
        key = node.get(N.NODE)
        if not str(key).startswith(_PREFIX):
            continue
        _need(key in _KINDS and key not in result and node.bl_idname == _KINDS[key]
              and node.get(N.OWNER) == group.get(N.OWNER) and not node.mute,
              'HM highlight-mask node inventory/type/owner changed')
        result[key] = node
    _need(set(result) == set(_KINDS), 'HM highlight-mask nodes are missing')
    return result


def _links():
    return sorted([
        ('fused_mask:uv', 'UV', 'fused_mask:texture', 'Vector'),
        ('fused_mask:texture', 'Color', 'fused_mask:channels', 'Color'),
        ('fused_mask:channels', 'Red', 'fused_mask:shift', 'Value'),
        ('fused_mask:shift', 'Value', 'fused_mask:coverage_scale', 'Value'),
        ('fused_mask:coverage_scale', 'Value', 'fused_mask:coverage_lower', 'Value'),
        ('fused_mask:coverage_lower', 'Value', 'fused_mask:coverage_upper', 'Value'),
        ('fused_mask:coverage_upper', 'Value', 'fused_mask:weight', 'Value_001'),
        ('highlight_strength', 'Value', 'fused_mask:weight', 'Value'),
        ('fused_mask:weight', 'Value', 'fused_mask:lower', 'Value'),
        ('fused_mask:lower', 'Value', 'fused_mask:upper', 'Value'),
        ('fused_mask:upper', 'Value', 'fused_mask:color', 'Fac'),
        ('palette', 'Color', 'fused_mask:color', 'Color1'),
        ('param:highlight_color', 'Color', 'fused_mask:color', 'Color2'),
        ('fused_mask:color', 'Color', 'whole_effect', 'Color2'),
    ])


def _graph_contract(group, uv_name, image, levels):
    nodes = _nodes(group)
    tex, uv = nodes['fused_mask:texture'], nodes['fused_mask:uv']
    _need(_original(tex.image) == _original(image) and uv.uv_map == uv_name
          and not uv.from_instancer, 'HM highlight-mask Image/UV reference changed')
    _need(all(getattr(tex, key) == val for key, val in _SAMPLING.items() if key != 'mode'),
          'HM highlight-mask texture sampling changed')
    _need(nodes['fused_mask:channels'].mode == 'RGB', 'HM highlight-mask channel mode changed')
    for key, operation in (('shift', 'SUBTRACT'), ('coverage_scale', 'DIVIDE'),
                           ('coverage_lower', 'MAXIMUM'), ('coverage_upper', 'MINIMUM'),
                           ('weight', 'MULTIPLY'), ('lower', 'MAXIMUM'), ('upper', 'MINIMUM')):
        node = nodes['fused_mask:' + key]
        _need(node.operation == operation and not node.use_clamp,
              'HM highlight-mask weight/clamp operation changed')
    _need(N._socket(nodes['fused_mask:shift'].inputs, 'Value_001').default_value == _float32(levels['black'])
          and N._socket(nodes['fused_mask:coverage_scale'].inputs, 'Value_001').default_value ==
              _float32(levels['white'] - levels['black']),
          'HM highlight-mask native black/white calibration changed')
    _need(N._socket(nodes['fused_mask:coverage_lower'].inputs, 'Value_001').default_value == 0.
          and N._socket(nodes['fused_mask:coverage_upper'].inputs, 'Value_001').default_value == 1.,
          'HM highlight-mask coverage clamp endpoints changed')
    _need(N._socket(nodes['fused_mask:lower'].inputs, 'Value_001').default_value == 0.
          and N._socket(nodes['fused_mask:upper'].inputs, 'Value_001').default_value == 1.,
          'HM highlight-mask clamp endpoints changed')
    color = nodes['fused_mask:color']
    _need(color.blend_type == 'MIX' and not color.use_alpha and not color.use_clamp,
          'HM highlight-mask must use one opaque palette/highlight mix')
    connected = set(nodes.values())
    actual = sorted((link.from_node.get(N.NODE), link.from_socket.identifier,
                     link.to_node.get(N.NODE), link.to_socket.identifier)
                    for link in group.links
                    if link.from_node in connected or link.to_node in connected)
    _need(actual == _links(), 'HM highlight-mask links or external consumers changed')
    destination = N._socket(N._node(group, 'whole_effect').inputs, 'Color2')
    _need(len(destination.links) == 1
          and destination.links[0].from_socket == N._socket(color.outputs, 'Color'),
          'HM highlight-mask no longer exclusively owns its output seam')
    return nodes


def describe(group, target, *, frozen=False):
    """Validate retained source data; actual evaluated UV is witnessed outside.

    Frozen mode permits Object.data to be the explicit evaluated replacement.
    The original local Mesh name, extent and original UV bytes remain required.
    It adds no source Mesh reference and does not call parent H._owned.
    """
    if not present(group):
        return None
    N._group(group)
    declared = _declaration(group)
    source = _target(group, target)
    import bpy
    mesh = bpy.data.meshes.get(declared['source_mesh']['name'])
    _need(mesh is not None and not mesh.library and not mesh.override_library and mesh.is_editable,
          'HM highlight-mask retained original Mesh is missing or not local')
    if not frozen:
        _need(source.data == mesh and mesh.users == 1,
              'HM highlight-mask author target no longer uses its exclusive original Mesh')
    _need(dict(name=source.name_full, type='MESH') == declared['target']
          and dict(name=mesh.name_full, vertices=len(mesh.vertices), corners=len(mesh.loops)) == declared['source_mesh'],
          'HM highlight-mask target/Mesh identity or extent changed')
    image = group.get(_IMAGE)
    _graph_contract(group, declared['uv']['uv_name'], image, declared['levels'])
    content = _image_descriptor(image, group)
    _need(content['name'] == declared['image']['name']
          and _sdf()._same_image_identity(content, declared['image']),
          'HM highlight-mask Image source, name or pixels changed')
    uv = _sdf()._uv_descriptor(mesh, declared['uv']['uv_name'])
    _sdf()._uv_shape(uv, len(mesh.vertices))
    _need(uv == declared['uv'], 'HM highlight-mask original UV bytes changed')
    result = dict(declared, image=content, uv=uv)
    validate_receipt_payload(result)
    return result


def _new_nodes(group, image, uv_name, levels, *, styles=None):
    created = []
    try:
        for index, (key, kind) in enumerate(_KINDS.items()):
            node = group.nodes.new(kind)
            created.append(node)
            node[N.NODE], node[N.OWNER] = key, group[N.OWNER]
            node.name, node.label = 'ToonHM/' + key, key
            node.location = ((index % 4) * 240., -2200. - (index // 4) * 220.)
        nodes = {node[N.NODE]: node for node in created}
        nodes['fused_mask:uv'].uv_map = uv_name
        nodes['fused_mask:uv'].from_instancer = False
        tex = nodes['fused_mask:texture']
        tex.image, tex.interpolation, tex.extension, tex.projection = image, 'Linear', 'EXTEND', 'FLAT'
        tex.projection_blend = 0.
        nodes['fused_mask:channels'].mode = 'RGB'
        for key, operation in (('shift', 'SUBTRACT'), ('coverage_scale', 'DIVIDE'),
                               ('coverage_lower', 'MAXIMUM'), ('coverage_upper', 'MINIMUM'),
                               ('weight', 'MULTIPLY'), ('lower', 'MAXIMUM'), ('upper', 'MINIMUM')):
            nodes['fused_mask:' + key].operation = operation
            nodes['fused_mask:' + key].use_clamp = False
        N._socket(nodes['fused_mask:shift'].inputs, 'Value_001').default_value = levels['black']
        N._socket(nodes['fused_mask:coverage_scale'].inputs, 'Value_001').default_value = levels['white'] - levels['black']
        N._socket(nodes['fused_mask:coverage_lower'].inputs, 'Value_001').default_value = 0.
        N._socket(nodes['fused_mask:coverage_upper'].inputs, 'Value_001').default_value = 1.
        N._socket(nodes['fused_mask:lower'].inputs, 'Value_001').default_value = 0.
        N._socket(nodes['fused_mask:upper'].inputs, 'Value_001').default_value = 1.
        color = nodes['fused_mask:color']
        color.blend_type, color.use_alpha, color.use_clamp = 'MIX', False, False
        for a, a_socket, b, b_socket in _links():
            group.links.new(N._socket(N._node(group, a).outputs, a_socket),
                            N._socket(N._node(group, b).inputs, b_socket))
        if styles is not None:
            for key, values in styles.items():
                node = nodes[key]
                for prop, val in values.items():
                    setattr(node, prop, val)
        return created
    except BaseException:
        for node in created:
            group.nodes.remove(node)
        raise


def _sign(group):
    from . import hair_map as H
    group[H._SHAPE] = H._sha(N.structure(group))


def _context(context):
    scene, target = context.scene, context.active_object
    _need(scene is not None and context.mode == 'OBJECT' and target is not None,
          'Choose an explicit local HM target in Object mode')
    return scene, target, target.active_material_index


def attach(context, image, uv_name, *, black_level=0., white_level=1.):
    """Explicitly add the masked single-highlight mix; never change image/UVs."""
    from . import hair_map as H
    scene, target, index = _context(context)
    _, _, group, _, _, _ = H._owned(scene, target, index)
    _need(not present(group), 'HM highlight-mask is already present or damaged')
    levels = _levels(black_level, white_level)
    _need(target.data.users == 1, 'HM highlight-mask requires an exclusive local source Mesh')
    image_value = _image_descriptor(image)
    uv = _sdf()._uv_descriptor(target.data, uv_name)
    _sdf()._uv_shape(uv, len(target.data.vertices))
    value = dict(schema=SCHEMA, profile=PROFILE, owner=group[N.OWNER],
                 target=dict(name=target.name_full, type='MESH'),
                 source_mesh=dict(name=target.data.name_full, vertices=len(target.data.vertices), corners=len(target.data.loops)),
                 image=image_value, uv=uv, sampling=dict(_SAMPLING), channel='R', formula=FORMULA,
                 levels=levels)
    validate_receipt_payload(value)
    source = N._socket(N._node(group, 'hm_color').outputs, 'Vector')
    destination = N._socket(N._node(group, 'whole_effect').inputs, 'Color2')
    _need(len(destination.links) == 1 and destination.links[0].from_socket == source
          and len(source.links) == 1, 'Original HM color output seam changed')
    previous_shape = group[H._SHAPE]
    created = []
    try:
        group[PROFILE_KEY], group[_TARGET], group[_IMAGE] = PROFILE, target, image
        group[_DECLARED] = _json(value)
        created = _new_nodes(group, image, uv_name, levels)
        _sign(group)
        return describe(group, target)
    except BaseException:
        for node in created:
            group.nodes.remove(node)
        for key in _KEYS:
            if key in group:
                del group[key]
        group.links.new(source, destination)
        group[H._SHAPE] = previous_shape
        raise


def _styles(nodes):
    return {key: {prop: (tuple(getattr(node, prop)) if prop in ('location', 'color')
                              else getattr(node, prop))
                  for prop in ('name', 'label', 'location', 'width', 'hide',
                               'select', 'use_custom_color', 'color')}
            for key, node in nodes.items()}


def remove(context):
    """Preflight fully, then restore the original edge and only reclaim our nodes."""
    from . import hair_map as H
    scene, target, index = _context(context)
    _, _, group, _, _, _ = H._owned(scene, target, index)
    descriptor = describe(group, target)
    _need(descriptor is not None, 'Current HM has no highlight-mask layer')
    nodes = _nodes(group)
    styles = _styles(nodes)
    metadata = {key: group[key] for key in _KEYS}
    previous_shape = group[H._SHAPE]
    image, uv_name = group[_IMAGE], descriptor['uv']['uv_name']
    source = N._socket(N._node(group, 'hm_color').outputs, 'Vector')
    destination = N._socket(N._node(group, 'whole_effect').inputs, 'Color2')
    try:
        group.links.new(source, destination)
        for node in nodes.values():
            group.nodes.remove(node)
        for key in _KEYS:
            del group[key]
        _sign(group)
        _need(not present(group), 'HM highlight-mask cleanup left reserved state')
        return dict(profile=PROFILE, owner=descriptor['owner'], removed=True, restored=True)
    except BaseException as error:
        # Nodes are owned, deterministic and disjoint from the original HM
        # graph. Recreate only this layer if a later removal step fails.
        try:
            for node in list(group.nodes):
                if str(node.get(N.NODE, '')).startswith(_PREFIX):
                    group.nodes.remove(node)
            for key, item in metadata.items():
                group[key] = item
            _new_nodes(group, image, uv_name, descriptor['levels'], styles=styles)
            group[H._SHAPE] = previous_shape
            describe(group, target)
        except BaseException as rollback_error:
            raise RuntimeError('HM highlight-mask removal failed and rollback is incomplete: '
                               + str(rollback_error)) from error
        raise
