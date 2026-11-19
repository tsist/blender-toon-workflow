# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional, static R-channel mask on the existing HEAD_BAND highlight.

The original band formula and rim path remain unchanged.  Runtime validation
reuses the SDF image/UV readers; receipt validation stays importable without
bpy.  The mask owns two references to its dedicated Image: one explicit Group
ID pointer and one Image Texture node, plus the Image's optional fake user.
It never writes image pixels, color space, packing, UVs or the finish shape
signature.  The caller signs a newly built Group only after this layer exists.
"""
import json

from . import finish_nodes as N

PROFILE = 'toon-finish-band-mask.v1'
SCHEMA = 'toon-finish-band-mask-descriptor.v1'
BINDING = 'toon_finish_band_mask'
_TARGET = BINDING + '_target'
_IMAGE = BINDING + '_image'
_DECLARED = BINDING + '_declared'
_KEYS = {BINDING, _TARGET, _IMAGE, _DECLARED}
_PREFIX = 'mask:'
_KINDS = {'mask:uv': 'ShaderNodeUVMap',
          'mask:texture': 'ShaderNodeTexImage',
          'mask:separate': 'ShaderNodeSeparateColor',
          'mask:multiply': 'ShaderNodeMath'}
_SAMPLING = dict(interpolation='Linear', extension='EXTEND',
                 projection='FLAT', mode='RGB')
_IMAGE_KEYS = {
    'name', 'source_kind', 'packed', 'filepath', 'file_sha256',
    'packed_sha256', 'source_sha256', 'width', 'height', 'channels', 'depth',
    'is_float', 'precision', 'colorspace', 'single_tile', 'channel',
    'channel_index', 'pixel_count', 'pixel_sha256', 'rgba_sha256', 'min', 'max',
}
_UV_KEYS = {'uv_name', 'domain', 'data_type', 'vertices', 'corners',
            'count', 'sha256', 'extent'}
_DESCRIPTOR_KEYS = {'schema', 'profile', 'owner', 'target', 'image', 'uv',
                    'channel', 'sampling'}


class BandMaskError(ValueError):
    """Invalid explicit input, altered owned mask, or unqualified consumer."""


def _need(condition, message):
    if not condition:
        raise BandMaskError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _sdf():
    # face_sdf imports bpy.  Keep this lazy for the public JSON reader.
    from . import face_sdf
    return face_sdf


def _original(value):
    return getattr(value, 'original', value)


def present(group):
    """Damaged reserved markers count as present and must not be ignored."""
    return (any(str(key).startswith(BINDING) for key in group.keys())
            or any(str(node.get(N.NODE, '')).startswith(_PREFIX)
                   for node in group.nodes))


def _target(group, explicit=None):
    import bpy
    value = _original(group.get(_TARGET))
    if explicit is not None:
        _need(_original(explicit) == value, 'Band mask target differs from explicit target')
    _need(isinstance(value, bpy.types.Object) and value.type == 'MESH'
          and bpy.data.objects.get(value.name) == value and not value.library
          and not value.override_library and value.is_editable,
          'Band mask requires its original local editable Mesh Object')
    _need(value.data is not None and not value.data.library
          and not value.data.override_library and value.data.is_editable,
          'Band mask source Mesh must remain local and editable')
    return value


def _target_descriptor(target, mesh=None):
    mesh = target.data if mesh is None else mesh
    return dict(name=target.name_full, mesh=mesh.name_full,
                vertices=len(mesh.vertices), corners=len(mesh.loops))


def _image_descriptor(image, group=None):
    T = _sdf()
    value = T._image_content_descriptor(image, 'R')
    _need(not image.use_extra_user, 'Band mask Image has an unqualified extra user')
    expected = int(image.use_fake_user)
    allowed = set()
    if group is not None:
        allowed.add(group)
        textures = [node for node in group.nodes
                    if node.bl_idname == 'ShaderNodeTexImage' and node.image == image]
        _need(group.get(_IMAGE) == image and len(textures) == 1
              and textures[0].get(N.NODE) == 'mask:texture',
              'Band mask must own one Image ID pointer and exactly one Texture reference')
        expected += 2
    _need(not (T._image_consumers(image) - allowed) and image.users == expected,
          'Band mask needs a dedicated Image; shared or unexplained references found')
    return value


def _declaration(group):
    _need(group.get(BINDING) == PROFILE, 'Band mask profile is missing or unsupported')
    _need({str(k) for k in group.keys() if str(k).startswith(BINDING)} == _KEYS,
          'Band mask reserved metadata changed')
    raw = group.get(_DECLARED)
    _need(type(raw) is str, 'Band mask declaration is not JSON')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                _need(key not in result, 'Duplicate band mask declaration key')
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError) as error:
        raise BandMaskError('Band mask declaration cannot be read') from error
    validate_receipt_payload(value)
    _need(value['owner'] == group.get(N.OWNER), 'Band mask finish owner changed')
    return value


def _nodes(group):
    result = {}
    for node in group.nodes:
        key = node.get(N.NODE)
        if not str(key).startswith(_PREFIX):
            continue
        _need(key in _KINDS and key not in result
              and node.bl_idname == _KINDS[key] and not node.mute
              and node.get(N.OWNER) == group.get(N.OWNER),
              'Band mask node inventory, type or owner changed')
        result[key] = node
    _need(set(result) == set(_KINDS), 'Band mask nodes are missing')
    return result


def _links():
    return sorted([
        ('mask:uv', 'UV', 'mask:texture', 'Vector'),
        ('mask:texture', 'Color', 'mask:separate', 'Color'),
        ('mask:separate', 'Red', 'mask:multiply', 'Value_001'),
        ('highlight_lit', 'Value', 'mask:multiply', 'Value'),
        ('mask:multiply', 'Value', 'highlight_weight', 'Value_001'),
    ])


def _graph_contract(group, uv_name, image):
    nodes = _nodes(group)
    uv_node, tex = nodes['mask:uv'], nodes['mask:texture']
    _need(_original(tex.image) == _original(image),
          'Band mask Image Texture no longer references its declared Image ID')
    _need(uv_node.uv_map == uv_name and not uv_node.from_instancer,
          'Band mask must sample its explicit target UV')
    _need(all(getattr(tex, key) == value for key, value in _SAMPLING.items()
              if key != 'mode') and tex.projection_blend == 0.,
          'Band mask Image sampling changed')
    _need(nodes['mask:separate'].mode == 'RGB'
          and nodes['mask:multiply'].operation == 'MULTIPLY'
          and not nodes['mask:multiply'].use_clamp,
          'Band mask channel/math settings changed')
    connected = set(nodes.values())
    actual = sorted((link.from_node.get(N.NODE), link.from_socket.identifier,
                     link.to_node.get(N.NODE), link.to_socket.identifier)
                    for link in group.links
                    if link.from_node in connected or link.to_node in connected)
    _need(actual == _links(), 'Band mask links or external consumers changed')
    destination = N._socket(N._node(group, 'highlight_weight').inputs, 'Value_001')
    _need(len(destination.links) == 1 and destination.links[0].from_node == nodes['mask:multiply'],
          'Band mask no longer exclusively feeds highlight weight')
    return nodes


def _validate_source(group, target, *, frozen):
    if not present(group):
        return None
    N._group(group)
    _need(group.get(N.PROFILE_KEY) == N.HAIR_PROFILE,
          'A band mask is only supported on HEAD_BAND')
    from . import finish_band
    finish_band.validate(group)
    declared = _declaration(group)
    source = _target(group, target)
    import bpy
    mesh = bpy.data.meshes.get(declared['target']['mesh'])
    _need(mesh is not None and not mesh.library and not mesh.override_library
          and mesh.is_editable, 'Band mask original source Mesh is missing or not local/editable')
    if not frozen:
        _need(source.data == mesh, 'Band mask author target no longer uses its declared source Mesh')
    _need(_target_descriptor(source, mesh) == declared['target'],
          'Band mask target name, Mesh or extent changed')
    image = group.get(_IMAGE)
    _graph_contract(group, declared['uv']['uv_name'], image)
    content = _image_descriptor(image, group)
    T = _sdf()
    T._image_shape(content, 'R')
    _need(T._same_image_identity(content, declared['image']),
          'Band mask Image source or pixels changed')
    uv = T._uv_descriptor(mesh, declared['uv']['uv_name'])
    T._uv_shape(uv, len(mesh.vertices))
    _need(uv == declared['uv'], 'Band mask original UV bytes changed')
    return dict(declared, image=content, uv=uv)


def validate(group, target=None):
    """Full author validation: Object.data must still be the declared source Mesh."""
    return _validate_source(group, target, frozen=False)


def frozen_source_descriptor(group, target=None):
    """Check the retained author source after explicit joint RENDER mesh replacement.

    No Mesh ID pointer is added: the declared local Mesh name, extent and UV
    bytes remain mandatory, as do the strong Object/Image IDs and graph.
    The current Object.data UV is observed separately by the RENDER witness.
    """
    return _validate_source(group, target, frozen=True)


def descriptor(group, target=None):
    return validate(group, target)


def report(group):
    value = validate(group)
    if value is None:
        return None
    image = group[_IMAGE]
    return dict(profile=PROFILE, image_name=image.name_full,
                uv_name=value['uv']['uv_name'], channel='R',
                image_references_owned=2, image_fake_user=bool(image.use_fake_user),
                image_policy='dedicated to one finish Group; Group ID pointer + Texture node')


def _structure(group):
    # Shape signs the retained author source, not a transient frozen RENDER
    # mesh. Author operations separately call the strict default validate.
    value = frozen_source_descriptor(group)
    if value is None:
        return None
    # An external FILE may disappear after a packed project is moved.  Pixel
    # and packed-source identity stay signed; incidental disk path does not.
    image_keys = ('source_kind', 'packed', 'source_sha256', 'pixel_sha256',
                  'rgba_sha256', 'width', 'height', 'channels', 'depth',
                  'is_float', 'precision', 'colorspace', 'single_tile',
                  'channel', 'channel_index', 'pixel_count')
    return dict(profile=PROFILE, target=N._plain(group[_TARGET]),
                image_id=N._plain(group[_IMAGE]),
                image={key: value['image'][key] for key in image_keys},
                uv=value['uv'], sampling=value['sampling'], channel='R')


def evaluate(group, target, evaluated_mesh, *, frozen=False):
    """Observe the actual RENDER Group/Image and its separately evaluated UV.

    The caller supplies a mesh from its current RENDER depsgraph. Subdivision
    may change UV loop count, so source and evaluated descriptors are kept as
    different fields; they are compared across the two RENDER observations.
    """
    source_group = _original(group)
    reader = frozen_source_descriptor if frozen else validate
    value = reader(source_group, _original(target))
    if value is None:
        return None
    _need(group.get(BINDING) == PROFILE and group.get(N.OWNER) == source_group.get(N.OWNER)
          and _original(group.get(_TARGET)) == _original(source_group.get(_TARGET))
          and _original(group.get(_IMAGE)) == _original(source_group.get(_IMAGE)),
          'Evaluated mask source identity changed')
    nodes = _graph_contract(group, value['uv']['uv_name'], source_group[_IMAGE])
    image = nodes['mask:texture'].image
    content = _sdf()._image_content_descriptor(image, 'R')
    _need(_sdf()._same_image_identity(content, value['image']),
          'Evaluated mask Image bytes differ from its source')
    uv_name = nodes['mask:uv'].uv_map
    _need(uv_name == value['uv']['uv_name'], 'Evaluated mask UV node changed')
    evaluated_uv = _sdf()._uv_descriptor(evaluated_mesh, uv_name)
    _sdf()._uv_shape(evaluated_uv)
    result = dict(value, image=content)
    validate_receipt_payload(result)
    return dict(source=result, evaluated_uv=evaluated_uv)


def validate_receipt_payload(value):
    """Closed JSON validation, deliberately without importing bpy/face_sdf."""
    from . import sdf_witness as W
    _need(type(value) is dict and set(value) == _DESCRIPTOR_KEYS,
          'Band mask descriptor fields are invalid or incomplete')
    _need(value.get('schema') == SCHEMA and value.get('profile') == PROFILE,
          'Band mask descriptor identity is invalid')
    N._owner(value.get('owner'))
    target = value['target']
    _need(type(target) is dict and set(target) == {'name', 'mesh', 'vertices', 'corners'}
          and all(type(target[k]) is str and target[k] and '\x00' not in target[k]
                  for k in ('name', 'mesh'))
          and all(type(target[k]) is int and target[k] > 0 for k in ('vertices', 'corners')),
          'Band mask target descriptor is invalid')
    image, uv = value['image'], value['uv']
    _need(type(image) is dict and set(image) == _IMAGE_KEYS,
          'Band mask Image descriptor fields changed')
    W._image(image)
    _need(image['filepath'] is None or
          (type(image['filepath']) is str and image['filepath'] and '\x00' not in image['filepath']),
          'Band mask Image path must be a string or null')
    _need(image['channel'] == value['channel'] == 'R' and image['channel_index'] == 0,
          'Band mask must sample Non-Color R')
    _need(type(uv) is dict and set(uv) == _UV_KEYS,
          'Band mask UV descriptor fields changed')
    W._uv(uv, uv.get('uv_name'))
    _need(uv['vertices'] == target['vertices'] and uv['corners'] == target['corners'],
          'Band mask original UV and target extents differ')
    _need(value['sampling'] == _SAMPLING, 'Band mask sampling descriptor changed')
    _need(uv['count'] > 0 and all(0. <= item <= 1.
          for key in ('min', 'max') for item in uv['extent'][key]),
          'Band mask UV coordinates must remain in [0,1]')
    return value


def build(group, target, image, uv_name):
    """Append only the optional mask seam to a newly constructed band Group."""
    N._group(group)
    _need(group.get(N.PROFILE_KEY) == N.HAIR_PROFILE and not present(group),
          'Band mask requires an unmasked HEAD_BAND Group')
    from . import finish_band
    finish_band.validate(group)
    # Validate target through the same exact policy before creating its ID ref.
    import bpy
    _need(isinstance(target, bpy.types.Object) and target.type == 'MESH'
          and bpy.data.objects.get(target.name) == target and not target.library
          and not target.override_library and target.is_editable
          and not target.data.library and not target.data.override_library
          and target.data.is_editable, 'Band mask target must be a local editable Mesh')
    image_value = _image_descriptor(image)
    uv = _sdf()._uv_descriptor(target.data, uv_name)
    _sdf()._uv_shape(uv, len(target.data.vertices))
    value = dict(schema=SCHEMA, profile=PROFILE, owner=group[N.OWNER],
                 target=_target_descriptor(target), image=image_value,
                 uv=uv, channel='R', sampling=dict(_SAMPLING))
    validate_receipt_payload(value)
    source = N._socket(N._node(group, 'highlight_lit').outputs, 'Value')
    destination = N._socket(N._node(group, 'highlight_weight').inputs, 'Value_001')
    _need(len(destination.links) == 1 and destination.links[0].from_socket == source
          and len(source.links) == 1, 'Original highlight seam changed')
    created = []
    try:
        for key, kind in _KINDS.items():
            node = group.nodes.new(kind)
            created.append(node)
            node.name, node.label = 'ToonFinish/' + key, key
            node[N.NODE], node[N.OWNER] = key, group[N.OWNER]
        nodes = {node[N.NODE]: node for node in created}
        nodes['mask:uv'].uv_map = uv_name
        tex = nodes['mask:texture']
        tex.image, tex.interpolation, tex.extension, tex.projection = image, 'Linear', 'EXTEND', 'FLAT'
        tex.projection_blend = 0.
        nodes['mask:separate'].mode = 'RGB'
        nodes['mask:multiply'].operation = 'MULTIPLY'
        nodes['mask:multiply'].use_clamp = False
        group[BINDING], group[_TARGET], group[_IMAGE] = PROFILE, target, image
        group[_DECLARED] = _json(value)
        group.links.remove(destination.links[0])
        for a, a_socket, b, b_socket in _links():
            group.links.new(N._socket(N._node(group, a).outputs, a_socket),
                            N._socket(N._node(group, b).inputs, b_socket))
        return validate(group, target)
    except BaseException:
        for node in created:
            group.nodes.remove(node)
        for key in _KEYS:
            if key in group:
                del group[key]
        if not destination.links:
            group.links.new(source, destination)
        raise
