# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated per-pixel face-threshold texture reader.

This module adds a shader reader only.  It does not add another Geometry
Nodes profile and never converts the image into a point attribute.  The four
existing ``toon-face-threshold.v1`` outputs are reused unchanged:

* ``threshold`` is treated as the original kernel's side weight.  A dedicated
  carrier must therefore have ``left`` all 0.0 and ``right`` all 1.0, both as
  FLOAT/POINT attributes with distinct names, so the old kernel's selected
  threshold is exactly ``side_weight``.
* ``phase`` keeps its original azimuth meaning, ``mix`` keeps its original
  region/strength/fade meaning and ``valid`` keeps its original gate meaning.
* The old Geometry Nodes default strength remains 0.0; this module neither
  changes nor replaces that kernel.

The reader samples a dedicated local FILE or PACKED image through a named
FLOAT2/CORNER UV layer.  Left and right samples are taken separately; the
right sample uses ``U = 1 - U`` when ``mirror`` is true.  The two scalar
threshold samples are then mixed with the carrier side weight.  Only after
that comparison does the existing ADD-only DARKEN layer run.  Surface, Alpha
and Normal are never touched by the binding.

``filter_radius_texels=0`` is the original ``toon-face-sdf-shader.v1`` path
and is never migrated.  A positive author radius creates the explicit
``toon-face-sdf-shader.v2`` profile: each side uses a signed 3x3 binomial
image-space kernel before the unchanged side mix, phase comparison and
Softness.  The radius is static metadata, not a mutable group input.
"""
from array import array
import hashlib
import json
import math
import os
import struct
import sys
import uuid

import bpy

from . import sdf_filter

PROFILE = 'toon-face-sdf-shader.v1'
FILTER_PROFILE = 'toon-face-sdf-shader.v2'
POLAR_PROFILE = 'toon-face-sdf-shader.v3'
DIRECTION_PROFILE = 'toon-face-sdf-shader.v4'
CONTINUITY_PROFILE = 'toon-face-sdf-shader.v5'
AUTHORED_PROFILE = 'toon-face-sdf-shader.v6'
PROFILES = (PROFILE, FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE)
SUPPORTED_PROFILES = frozenset(PROFILES)
PROFILE_KEY = 'toon_face_sdf_profile'
OWNER = 'toon_face_sdf_owner'
CARRIER_CONTRACT = 'left0_right1_side.v1'
SCHEMA = 'toon-face-sdf-reader.v1'
DESCRIPTOR_SCHEMA = 'toon-face-sdf-reader-descriptor.v1'
POLAR_DESCRIPTOR_SCHEMA = 'toon-face-sdf-reader-descriptor.v2'
DIRECTION_DESCRIPTOR_SCHEMA = 'toon-face-sdf-reader-descriptor.v3'
CONTINUITY_DESCRIPTOR_SCHEMA = 'toon-face-sdf-reader-descriptor.v4'
AUTHORED_DESCRIPTOR_SCHEMA = 'toon-face-sdf-reader-descriptor.v5'
BINDING = 'toon-face-sdf-material-binding.v1'

_TARGET = 'toon_face_sdf_target'
_UID = 'toon_face_sdf_modifier_uid'
_TARGET_DESC = 'toon_face_sdf_target_descriptor'
_FIELDS = 'toon_face_sdf_fields'
_CARRIER = 'toon_face_sdf_carrier'
_CARRIER_CONTRACT = 'toon_face_sdf_carrier_contract'
_IMAGE_DESC = 'toon_face_sdf_image_descriptor'
_UV_DESC = 'toon_face_sdf_uv_descriptor'
_CHANNEL = 'toon_face_sdf_channel'
_MIRROR = 'toon_face_sdf_mirror'
_SAMPLING = 'toon_face_sdf_sampling'
_VERTICAL_SOURCE = 'toon_face_sdf_vertical_source'
_DIRECTION_SOURCE = 'toon_face_sdf_direction_source'
_DIRECTION_CORRECTION = 'toon_face_sdf_direction_correction'
_PAIR_SOURCE = 'toon_face_sdf_pair_source'
_CONTINUITY_KEYS = 'toon_face_sdf_continuity_keys'
_SIGNATURE = 'toon_face_sdf_signature'
_NODE = 'toon_face_sdf_node'

_MARK = 'toon_face_sdf_binding'
_TOKEN = 'toon_face_sdf_token'
_TREE = 'toon_face_sdf_reader_tree'
_DEST = 'toon_face_sdf_destination'
_SOURCE = 'toon_face_sdf_source'
_ORIGINAL = 'toon_face_sdf_original'
_SNAPSHOT = 'toon_face_sdf_snapshot'

_FIELD_KEYS = ('threshold', 'phase', 'mix', 'valid')
_CHANNELS = {'R': ('Red', 0), 'G': ('Green', 1), 'B': ('Blue', 2)}
_NODE_TYPES = {
    'inputs': 'NodeGroupInput',
    'output': 'NodeGroupOutput',
    'uv': 'ShaderNodeUVMap',
    'left_texture': 'ShaderNodeTexImage',
    'right_texture': 'ShaderNodeTexImage',
    'left_separate': 'ShaderNodeSeparateColor',
    'right_separate': 'ShaderNodeSeparateColor',
    'attr_threshold': 'ShaderNodeAttribute',
    'attr_phase': 'ShaderNodeAttribute',
    'attr_mix': 'ShaderNodeAttribute',
    'attr_valid': 'ShaderNodeAttribute',
    'side_max': 'ShaderNodeMath',
    'side_min': 'ShaderNodeMath',
    'left_weight': 'ShaderNodeMath',
    'left_part': 'ShaderNodeMath',
    'right_part': 'ShaderNodeMath',
    'texture_scalar': 'ShaderNodeMath',
    'softness_max': 'ShaderNodeMath',
    'softness_positive': 'ShaderNodeMath',
    'hard_lit': 'ShaderNodeMath',
    'soft_low': 'ShaderNodeMath',
    'soft_high': 'ShaderNodeMath',
    'smooth_lit': 'ShaderNodeMapRange',
    'hard_weight': 'ShaderNodeMath',
    'lit_hard': 'ShaderNodeMath',
    'lit_soft': 'ShaderNodeMath',
    'lit': 'ShaderNodeMath',
    'shadow': 'ShaderNodeMath',
    'mix_max': 'ShaderNodeMath',
    'mix_min': 'ShaderNodeMath',
    'valid_gate': 'ShaderNodeMath',
    'darkening_mix': 'ShaderNodeMath',
    'darkening': 'ShaderNodeMath',
    'rgb_minimum': 'ShaderNodeMixRGB',
    'final_darkening': 'ShaderNodeMixRGB',
}
_MIRROR_NODE_TYPES = {
    'uv_split': 'ShaderNodeSeparateXYZ',
    'mirror_x': 'ShaderNodeMath',
    'mirror_uv': 'ShaderNodeCombineXYZ',
}

_FILTER_SIDES = ('left', 'right')
_FILTER_TAPS = tuple(range(9))
_TEXTURE_COUNTS = {PROFILE: 2, FILTER_PROFILE: 18, POLAR_PROFILE: 27, DIRECTION_PROFILE: 36, CONTINUITY_PROFILE: 45, AUTHORED_PROFILE: 45}


def _texture_count(info):
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and not info['direction_correction']:
        return 36
    return _TEXTURE_COUNTS[info['profile']]


class FaceSDFError(ValueError):
    """Invalid SDF reader identity, source data or material binding."""


def _filter_radius(value):
    try:
        return sdf_filter.validate_radius(value)
    except ValueError as error:
        raise FaceSDFError(str(error)) from error


def _sampling_shape(value, *, profile, image_width=None, image_height=None):
    try:
        return sdf_filter.validate_sampling(
            value, profile=profile, image_width=image_width,
            image_height=image_height)
    except ValueError as error:
        raise FaceSDFError(str(error)) from error


def _filter_node_types():
    result = {}
    for side in _FILTER_SIDES:
        for index in _FILTER_TAPS:
            result[side + '_sample_weight_' + str(index)] = 'ShaderNodeMath'
            if index != 4:
                result[side + '_uv_offset_' + str(index)] = 'ShaderNodeVectorMath'
                result[side + '_texture_' + str(index)] = 'ShaderNodeTexImage'
                result[side + '_separate_' + str(index)] = 'ShaderNodeSeparateColor'
        for index in range(8):
            result[side + '_sum_' + str(index)] = 'ShaderNodeMath'
    return result


def _profile_fields(info):
    """Keep the old v1 report payload byte-for-byte compatible."""
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        return dict(profile=info['profile'], sampling=info['sampling'], direction_encoding=_continuity_encoding(info['profile']),
                    vertical_source=info['vertical_source'], direction_source=info['direction_source'],
                    direction_correction=info['direction_correction'], pair_source=info['pair_source'],
                    continuity_keys=info['continuity_keys'])
    if info['profile'] == DIRECTION_PROFILE:
        from .face_sdf_direction import ENCODING
        return dict(profile=DIRECTION_PROFILE, sampling=info['sampling'], direction_encoding=ENCODING,
                    vertical_source=info['vertical_source'], direction_source=info['direction_source'],
                    direction_correction=info['direction_correction'])
    if info['profile'] == POLAR_PROFILE:
        from .face_sdf_polar import ENCODING
        return dict(profile=POLAR_PROFILE, sampling=info['sampling'], direction_encoding=ENCODING,
                    vertical_source=info['vertical_source'])
    if info['profile'] in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        return dict(profile=info['profile'], sampling=info['sampling'])
    return dict(profile=PROFILE)


def _continuity_encoding(profile):
    from . import face_sdf_continuity
    return (face_sdf_continuity.AUTHORED_ENCODING if profile == AUTHORED_PROFILE
            else face_sdf_continuity.ENCODING)


def _core():
    from . import face_thresholds
    return face_thresholds


def _material_core():
    from . import face_material
    return face_material


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bpy.types.ID):
        return ['ID', value.bl_rna.identifier, value.name_full]
    try:
        return [_value(item) for item in value]
    except TypeError:
        return str(value)


def _is_sha(value):
    return (type(value) is str and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _is_owner(value):
    return (type(value) is str and len(value) == 32
            and all(c in '0123456789abcdef' for c in value))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(float(value))


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _float_sha(values):
    data = array('f', values)
    if sys.byteorder != 'little':
        data.byteswap()
    return hashlib.sha256(data.tobytes()).hexdigest()


def _constant_sha(value, count):
    word = struct.pack('<f', float(value))
    digest = hashlib.sha256()
    block = word * 4096
    whole, remainder = divmod(int(count), 4096)
    for _ in range(whole):
        digest.update(block)
    digest.update(word * remainder)
    return digest.hexdigest()


def _socket(sockets, identifier):
    matches = [socket for socket in sockets if socket.identifier == identifier]
    if len(matches) != 1:
        raise FaceSDFError('Expected exactly one native socket: ' + identifier)
    return matches[0]


def _target(target):
    if (not isinstance(target, bpy.types.Object) or target.type != 'MESH'
            or target.library is not None or target.override_library is not None
            or target not in list(bpy.data.objects)):
        raise FaceSDFError('An existing local editable MESH Object is required')
    mesh = target.data
    if mesh is None or mesh.library is not None or mesh.override_library is not None:
        raise FaceSDFError('The target Mesh must be local and readable')
    return mesh


def _explicit_modifier(target, modifier):
    _target(target)
    if (not isinstance(modifier, bpy.types.Modifier)
            or getattr(modifier, 'id_data', None) != target):
        raise FaceSDFError('The face modifier must belong to the explicit target')
    matches = [item for item in target.modifiers if _core().is_managed(item)]
    if len(matches) != 1 or matches[0] != modifier:
        raise FaceSDFError('The target must have exactly one explicit toon-face-threshold.v1 modifier')
    return modifier


def _managed(target):
    matches = [item for item in target.modifiers if _core().is_managed(item)]
    if len(matches) != 1:
        raise FaceSDFError('The target must have exactly one explicit toon-face-threshold.v1 modifier')
    return matches[0]


def _context_target(context):
    target = getattr(context, 'object', None)
    if getattr(context, 'mode', None) != 'OBJECT' or target is None or target.type != 'MESH':
        raise FaceSDFError('Select an active Mesh Object in Object Mode')
    if target.library is not None or target.override_library is not None or not target.is_editable:
        raise FaceSDFError('SDF binding only edits a local editable Mesh Object')
    return target


def _point_float(mesh, name, label):
    if type(name) is not str or not name:
        raise FaceSDFError(label + ' must be a nonempty attribute name')
    attribute = mesh.attributes.get(name)
    if (attribute is None or attribute.domain != 'POINT'
            or attribute.data_type != 'FLOAT'
            or len(attribute.data) != len(mesh.vertices)):
        raise FaceSDFError(label + ' must be an actual FLOAT/POINT attribute with one value per vertex: ' + name)
    values = array('f', [0.0]) * len(attribute.data)
    attribute.data.foreach_get('value', values)
    if any(not math.isfinite(float(value)) or value < 0.0 or value > 1.0 for value in values):
        raise FaceSDFError(label + ' values must be finite in [0,1]: ' + name)
    return values


def _target_descriptor(target):
    mesh = _target(target)
    return dict(name=target.name, mesh=mesh.name,
                vertices=len(mesh.vertices), loops=len(mesh.loops))


def _fields(value):
    polar_keys = set(_FIELD_KEYS) | {'vertical_side', 'vertical_weight'}
    direction_keys = polar_keys | {'direction_upper', 'direction_top'}
    continuity_keys = direction_keys | {'direction_pair'}
    if type(value) is not dict or set(value) not in (set(_FIELD_KEYS), polar_keys, direction_keys, continuity_keys):
        raise FaceSDFError('Carrier fields must contain exactly threshold/phase/mix/valid')
    names = list(value.values())
    if any(type(name) is not str or not name for name in names) or len(set(names)) != len(names):
        raise FaceSDFError('Carrier output field names must be nonempty and unique')
    return dict(value)


def _carrier_shape(carrier, fields=None, *, count=None):
    if type(carrier) is not dict:
        raise FaceSDFError('Carrier metadata must be an object')
    if carrier.get('contract') != CARRIER_CONTRACT:
        raise FaceSDFError('Carrier contract must be ' + CARRIER_CONTRACT)
    carrier_fields = _fields(carrier.get('fields'))
    if fields is not None and carrier_fields != fields:
        raise FaceSDFError('Carrier fields do not match the reader fields')
    left = carrier.get('left_attribute')
    right = carrier.get('right_attribute')
    weight = carrier.get('weight_attribute')
    for label, name in (('left', left), ('right', right), ('weight', weight)):
        if type(name) is not str or not name:
            raise FaceSDFError('Carrier ' + label + ' attribute name is missing')
    if left == right:
        raise FaceSDFError('Carrier left and right source attributes must have different names')
    if weight in (left, right):
        raise FaceSDFError('Carrier weight attribute must not alias the left/right constants')
    if any(name in carrier_fields.values() for name in (left, right, weight)):
        raise FaceSDFError('Carrier source attribute aliases an owned output field')
    source_count = carrier.get('count')
    if type(source_count) is not int or source_count <= 0:
        raise FaceSDFError('Carrier source count must be a positive integer')
    if count is not None and source_count != count:
        raise FaceSDFError('Carrier source count does not match the current mesh')
    for key in ('left_sha256', 'right_sha256', 'weight_sha256'):
        if not _is_sha(carrier.get(key)):
            raise FaceSDFError('Carrier is missing a valid source hash: ' + key)
    if carrier['left_sha256'] != _constant_sha(0.0, source_count):
        raise FaceSDFError('Carrier left source is not the required all-zero FLOAT/POINT field')
    if carrier['right_sha256'] != _constant_sha(1.0, source_count):
        raise FaceSDFError('Carrier right source is not the required all-one FLOAT/POINT field')
    if not _is_owner(carrier.get('core_owner')):
        raise FaceSDFError('Carrier core owner is invalid')
    polar = carrier.get('core_profile') in (_core().POLAR_PROFILE, _core().DIRECTION_PROFILE, _core().CONTINUITY_PROFILE)
    direction = carrier.get('core_profile') in (_core().DIRECTION_PROFILE, _core().CONTINUITY_PROFILE)
    continuity = carrier.get('core_profile') == _core().CONTINUITY_PROFILE
    if carrier.get('core_profile') not in (_core().PROFILE, _core().POLAR_PROFILE, _core().DIRECTION_PROFILE, _core().CONTINUITY_PROFILE):
        raise FaceSDFError('Carrier core profile is unsupported')
    expected_roles = set(_FIELD_KEYS) | ({'vertical_side', 'vertical_weight'} if polar else set())
    if direction:
        expected_roles |= {'direction_upper', 'direction_top'}
    if continuity:
        expected_roles.add('direction_pair')
        _continuity_keys(carrier.get('continuity_keys'))
    elif 'continuity_keys' in carrier:
        raise FaceSDFError('Legacy carrier must not carry a continuity identity')
    if set(carrier_fields) != expected_roles:
        raise FaceSDFError('Carrier fields differ from the explicit direction profile')
    if not _is_sha(carrier.get('core_signature')):
        raise FaceSDFError('Carrier core signature is invalid')
    return carrier_fields


def _carrier(target, modifier):
    """Re-preflight the exact left0/right1 side carrier on every live use."""
    mesh = _target(target)
    _explicit_modifier(target, modifier)
    core = _core()
    report = core.inspect(target, modifier, evaluate=False)
    if report.get('status') != 'valid':
        raise FaceSDFError('Face carrier is invalid: ' + '; '.join(report.get('issues', [])))
    fields = _fields(report.get('attributes'))
    left = report.get('left_attribute')
    right = report.get('right_attribute')
    weight = report.get('weight_attribute')
    if type(left) is not str or type(right) is not str or type(weight) is not str:
        raise FaceSDFError('Face carrier source names are missing')
    if left == right:
        raise FaceSDFError('Face carrier left and right source names must differ')
    if weight in (left, right):
        raise FaceSDFError('Face carrier weight must not alias the left/right constants')
    left_values = _point_float(mesh, left, 'Carrier left source')
    right_values = _point_float(mesh, right, 'Carrier right source')
    weight_values = _point_float(mesh, weight, 'Carrier weight source')
    if any(float(value) != 0.0 for value in left_values):
        raise FaceSDFError('Carrier left source must be all zero FLOAT/POINT (left0_right1_side.v1)')
    if any(float(value) != 1.0 for value in right_values):
        raise FaceSDFError('Carrier right source must be all one FLOAT/POINT (left0_right1_side.v1)')
    owner = report.get('owner')
    if not _is_owner(owner):
        raise FaceSDFError('Face carrier owner is invalid')
    count = len(mesh.vertices)
    if count <= 0:
        raise FaceSDFError('Face carrier requires at least one vertex')
    carrier = dict(
        contract=CARRIER_CONTRACT,
        fields=fields,
        left_attribute=left,
        right_attribute=right,
        weight_attribute=weight,
        left_sha256=_float_sha(left_values),
        right_sha256=_float_sha(right_values),
        weight_sha256=_float_sha(weight_values),
        count=count,
        core_profile=report['profile'],
        core_owner=owner,
        core_signature=core.graph_signature(modifier.node_group),
        side_semantics='threshold=side_weight: 0=reference -X side, 1=reference +X side',
    )
    if report['profile'] == core.CONTINUITY_PROFILE:
        carrier['continuity_keys'] = report['continuity_keys']
    _carrier_shape(carrier, fields, count=count)
    return carrier


def carrier(target, modifier):
    """Read-only carrier receipt for a live explicit face modifier."""
    return _carrier(target, modifier)


def _image_consumers(image):
    try:
        return set(bpy.data.user_map(subset={image}).get(image, set()))
    except (TypeError, RuntimeError, ReferenceError) as error:
        raise FaceSDFError('SDF image consumers could not be inspected; refusing an unverifiable binding') from error


def _image_content_descriptor(image, channel):
    import numpy as np
    if not isinstance(image, bpy.types.Image):
        raise FaceSDFError('An existing Image datablock is required')
    if image.library is not None or image.override_library is not None:
        raise FaceSDFError('SDF image must be a local datablock')
    if channel not in _CHANNELS:
        raise FaceSDFError('SDF channel must be R, G or B')
    if image.source != 'FILE' or image.source == 'TILED':
        raise FaceSDFError('SDF image must already be a local single FILE or PACKED image; got ' + str(image.source))
    if len(image.tiles) != 1:
        raise FaceSDFError('SDF image must have exactly one tile')
    if len(image.packed_files) > 1:
        raise FaceSDFError('SDF image must not contain multiple packed files')
    if image.colorspace_settings.name != 'Non-Color':
        raise FaceSDFError('SDF image must already be Non-Color; refusing to change shared color space: '
                           + str(image.colorspace_settings.name))
    packed = image.packed_file
    packed_sha256 = None
    file_sha256 = None
    filepath = None
    if packed is not None:
        try:
            data = bytes(packed.data)
        except (TypeError, RuntimeError) as error:
            raise FaceSDFError('Packed SDF image data could not be read') from error
        if not data:
            raise FaceSDFError('Packed SDF image is empty')
        packed_sha256 = _sha_bytes(data)
        source_kind = 'PACKED'
        if image.filepath:
            candidate = bpy.path.abspath(image.filepath)
            if os.path.isfile(candidate):
                file_sha256 = _file_sha(candidate)
                filepath = candidate
    else:
        candidate = bpy.path.abspath(image.filepath) if image.filepath else ''
        if not candidate or not os.path.isfile(candidate):
            raise FaceSDFError('SDF image FILE source is missing on local disk')
        if candidate.startswith('\\\\') or candidate.startswith('//'):
            raise FaceSDFError('SDF image FILE source must be a local path, not a UNC share')
        file_sha256 = _file_sha(candidate)
        filepath = candidate
        source_kind = 'FILE'
    width, height = int(image.size[0]), int(image.size[1])
    if width <= 0 or height <= 0:
        raise FaceSDFError('SDF image has invalid actual dimensions')
    pixel_count = width * height * 4
    try:
        actual_count = len(image.pixels)
    except (RuntimeError, ReferenceError) as error:
        raise FaceSDFError('SDF image pixels could not be read') from error
    if actual_count != pixel_count:
        raise FaceSDFError('SDF image pixel extent does not match width*height*RGBA')
    values = array('f', [0.0]) * pixel_count
    try:
        image.pixels.foreach_get(values)
    except (RuntimeError, TypeError) as error:
        raise FaceSDFError('SDF image pixel readback failed') from error
    # Read-only native-float view of the same buffer: validate every RGBA value,
    # then only the selected channel's range. Keep original bytes for all hashes.
    if not np.isfinite(np.frombuffer(values, dtype=np.float32)).all():
        raise FaceSDFError('SDF image contains nonfinite pixel data')
    channel_index = _CHANNELS[channel][1]
    channel_values = values[channel_index::4]
    if len(channel_values) != width * height:
        raise FaceSDFError('SDF image channel extent is invalid')
    channel_view = np.frombuffer(channel_values, dtype=np.float32)
    if np.any(channel_view < 0.0) or np.any(channel_view > 1.0):
        raise FaceSDFError('SDF image selected channel must be finite in [0,1]')
    precision = 'FLOAT32' if image.is_float else 'UINT8'
    descriptor = dict(
        name=image.name,
        source_kind=source_kind,
        packed=packed is not None,
        filepath=filepath,
        file_sha256=file_sha256,
        packed_sha256=packed_sha256,
        source_sha256=packed_sha256 if packed_sha256 is not None else file_sha256,
        width=width,
        height=height,
        channels=int(getattr(image, 'channels', 4)),
        depth=int(image.depth),
        is_float=bool(image.is_float),
        precision=precision,
        colorspace=image.colorspace_settings.name,
        single_tile=True,
        channel=channel,
        channel_index=channel_index,
        pixel_count=pixel_count,
        pixel_sha256=_float_sha(channel_values),
        rgba_sha256=_float_sha(values),
        min=float(min(channel_values)),
        max=float(max(channel_values)),
    )
    if not _is_sha(descriptor['source_sha256']):
        raise FaceSDFError('SDF image source hash is invalid')
    return descriptor


def _image_descriptor(image, channel, *, allowed_consumers=()):
    descriptor = _image_content_descriptor(image, channel)
    allowed = set(allowed_consumers)
    # user_map groups references by owning ID. Two pointers from the same Scene
    # are only one consumer there, so also account for every allowed reference.
    if image.use_extra_user:
        raise FaceSDFError('SDF image has an unqualified extra user; refusing an unverifiable binding')
    expected_users = int(image.use_fake_user)
    for tree in allowed:
        if not isinstance(tree, bpy.types.ShaderNodeTree):
            raise FaceSDFError('Only explicit SDF shader trees may be allowed image consumers')
        expected_users += sum(node.bl_idname == 'ShaderNodeTexImage' and node.image == image
                              for node in tree.nodes)
    from . import sdf_ui
    prop = bpy.types.Scene.bl_rna.properties.get(sdf_ui.PROP)
    schema_owned = (sdf_ui._registered and prop is not None
                    and prop.as_pointer() == sdf_ui._descriptor)
    for scene in bpy.data.scenes:
        settings = getattr(scene, sdf_ui.PROP, None) if schema_owned else None
        if not isinstance(settings, sdf_ui.TOONSDF_Settings):
            continue
        picker_count = sum(getattr(settings, key, None) == image for key in ('image', 'vertical_image', 'direction_image', 'pair_image'))
        if not picker_count:
            continue
        # Saved RNA picker storage is not scene.items() in Blender 5.2.
        # Custom properties are additional references, never substitutes for it.
        def no_custom_reference(value):
            if isinstance(value, bpy.types.Image):
                return value != image
            if isinstance(value, bpy.types.ID):
                return True
            if hasattr(value, 'items'):
                return all(no_custom_reference(item) for item in value.values())
            return True
        if all(no_custom_reference(item) for item in scene.values()):
            allowed.add(scene)
            expected_users += picker_count
    consumers = _image_consumers(image)
    if consumers - allowed:
        names = sorted(getattr(user, 'name', str(user)) for user in consumers - allowed)
        raise FaceSDFError('SDF image is shared with other consumers; a dedicated image is required: '
                           + ', '.join(names[:8]))
    if image.users != expected_users:
        raise FaceSDFError('SDF image has unexplained references; expected '
                           + str(expected_users) + ', observed ' + str(image.users))
    return descriptor


def _same_image_identity(current, stored):
    keys = ('width', 'height', 'channels', 'depth', 'is_float', 'precision',
            'colorspace', 'single_tile', 'source_kind', 'packed', 'source_sha256',
            'pixel_sha256', 'rgba_sha256', 'channel', 'channel_index', 'pixel_count')
    return all(current.get(key) == stored.get(key) for key in keys)


def _image_shape(value, channel=None):
    if type(value) is not dict:
        raise FaceSDFError('Image descriptor must be an object')
    for key in ('width', 'height', 'channels', 'depth', 'pixel_count'):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise FaceSDFError('Image descriptor has an invalid integer: ' + key)
    if value['pixel_count'] != value['width'] * value['height'] * 4:
        raise FaceSDFError('Image descriptor pixel extent is inconsistent')
    if type(value.get('channel_index')) is not int or not 0 <= value['channel_index'] <= 2:
        raise FaceSDFError('Image descriptor channel index must be 0, 1 or 2')
    if value.get('channel') not in _CHANNELS:
        raise FaceSDFError('Image descriptor channel must be R, G or B')
    if channel is not None and value.get('channel') != channel:
        raise FaceSDFError('Image descriptor channel differs from the reader channel')
    if value.get('channel_index') != _CHANNELS[value['channel']][1]:
        raise FaceSDFError('Image descriptor channel index is inconsistent')
    if value.get('colorspace') != 'Non-Color':
        raise FaceSDFError('Image descriptor must remain Non-Color')
    if value.get('source_kind') not in {'FILE', 'PACKED'}:
        raise FaceSDFError('Image descriptor source kind must be FILE or PACKED')
    if type(value.get('packed')) is not bool or value.get('single_tile') is not True:
        raise FaceSDFError('Image descriptor tile/packed flags are invalid')
    if type(value.get('is_float')) is not bool or type(value.get('precision')) is not str:
        raise FaceSDFError('Image descriptor precision flags are invalid')
    for key in ('source_sha256', 'pixel_sha256', 'rgba_sha256'):
        if not _is_sha(value.get(key)):
            raise FaceSDFError('Image descriptor hash is invalid: ' + key)
    if value['source_kind'] == 'PACKED':
        if not value['packed'] or not _is_sha(value.get('packed_sha256')):
            raise FaceSDFError('Packed image descriptor is missing its packed hash')
        if value['source_sha256'] != value['packed_sha256']:
            raise FaceSDFError('Packed image source hash is inconsistent')
    else:
        if value['packed'] or not _is_sha(value.get('file_sha256')):
            raise FaceSDFError('FILE image descriptor is missing its source hash')
        if value['source_sha256'] != value['file_sha256']:
            raise FaceSDFError('FILE image source hash is inconsistent')
    if (not _finite(value.get('min')) or not _finite(value.get('max'))
            or not 0.0 <= value['min'] <= value['max'] <= 1.0):
        raise FaceSDFError('Image descriptor channel range is invalid')
    return value


def _uv_descriptor(mesh, uv_name):
    if type(uv_name) is not str or not uv_name:
        raise FaceSDFError('An explicit UV name is required')
    layer = mesh.uv_layers.get(uv_name)
    attribute = mesh.attributes.get(uv_name)
    if layer is None or attribute is None:
        raise FaceSDFError('UV layer does not exist on the target Mesh: ' + uv_name)
    if (attribute.domain != 'CORNER' or attribute.data_type != 'FLOAT2'
            or len(attribute.data) != len(mesh.loops)):
        raise FaceSDFError('UV layer must be FLOAT2/CORNER with one value per loop: ' + uv_name)
    values = array('f', [0.0]) * (2 * len(mesh.loops))
    attribute.data.foreach_get('vector', values)
    if any(not math.isfinite(float(value)) or value < 0.0 or value > 1.0 for value in values):
        raise FaceSDFError('UV layer coordinates must be finite in [0,1]: ' + uv_name)
    pairs = [tuple(float(value) for value in values[i:i + 2])
             for i in range(0, len(values), 2)]
    extent = dict(min=[min(pair[0] for pair in pairs), min(pair[1] for pair in pairs)],
                  max=[max(pair[0] for pair in pairs), max(pair[1] for pair in pairs)]) if pairs else dict(min=None, max=None)
    return dict(uv_name=uv_name, domain='CORNER', data_type='FLOAT2',
                vertices=len(mesh.vertices), corners=len(mesh.loops),
                count=len(attribute.data), sha256=_float_sha(values), extent=extent)


def _uv_shape(value, target_vertices=None):
    if type(value) is not dict:
        raise FaceSDFError('UV descriptor must be an object')
    if type(value.get('uv_name')) is not str or not value['uv_name']:
        raise FaceSDFError('UV descriptor name is missing')
    if value.get('domain') != 'CORNER' or value.get('data_type') != 'FLOAT2':
        raise FaceSDFError('UV descriptor must be FLOAT2/CORNER')
    for key in ('vertices', 'corners', 'count'):
        if type(value.get(key)) is not int or value[key] < 0:
            raise FaceSDFError('UV descriptor has an invalid count: ' + key)
    if value['count'] != value['corners']:
        raise FaceSDFError('UV descriptor corner count is inconsistent')
    if target_vertices is not None and value['vertices'] != target_vertices:
        raise FaceSDFError('UV descriptor vertex count differs from the target')
    if not _is_sha(value.get('sha256')):
        raise FaceSDFError('UV descriptor hash is invalid')
    extent = value.get('extent')
    if type(extent) is not dict or set(extent) != {'min', 'max'}:
        raise FaceSDFError('UV descriptor extent is invalid')
    if value['count'] == 0:
        if extent != dict(min=None, max=None):
            raise FaceSDFError('Empty UV descriptor must have a null extent')
    else:
        for key in ('min', 'max'):
            row = extent[key]
            if (type(row) is not list or len(row) != 2
                    or any(not _finite(item) for item in row)
                    or any(item < 0.0 or item > 1.0 for item in row)):
                raise FaceSDFError('UV descriptor extent values are invalid')
        if any(a > b for a, b in zip(extent['min'], extent['max'])):
            raise FaceSDFError('UV descriptor extent is reversed')
    return value


def _target_shape(value, count):
    if type(value) is not dict:
        raise FaceSDFError('Target descriptor must be an object')
    if type(value.get('name')) is not str or not value['name']:
        raise FaceSDFError('Target descriptor name is missing')
    if type(value.get('mesh')) is not str or not value['mesh']:
        raise FaceSDFError('Target descriptor mesh name is missing')
    if type(value.get('vertices')) is not int or value['vertices'] != count:
        raise FaceSDFError('Target descriptor vertex count is invalid')
    if type(value.get('loops')) is not int or value['loops'] < 0:
        raise FaceSDFError('Target descriptor loop count is invalid')
    return value


def _vertical_source_shape(value, horizontal_image, horizontal_uv, horizontal_sampling):
    """Explicit second Image/UV data contract; no fallback to horizontal data."""
    if type(value) is not dict or set(value) != {'image', 'uv', 'sampling'}:
        raise FaceSDFError('Polar reader requires an explicit independent vertical source')
    image = _image_shape(value['image'], 'G')
    if (type(image.get('name')) is not str or not image['name']
            or image['name'] == horizontal_image.get('name') or image['channels'] not in (3, 4)):
        raise FaceSDFError('Vertical source must be an independent RGB Image with a signed G channel')
    uv = _uv_shape(value['uv'], horizontal_uv['vertices'])
    if uv['uv_name'] == horizontal_uv['uv_name'] or uv['corners'] != horizontal_uv['corners']:
        raise FaceSDFError('Vertical UV must be another layer on the same target/corner topology')
    sampling = _sampling_shape(value['sampling'], profile=POLAR_PROFILE,
        image_width=image['width'], image_height=image['height'])
    if sampling['radius_texels'] != horizontal_sampling['radius_texels']:
        raise FaceSDFError('Horizontal and vertical filters must use the same explicit radius')
    return dict(image=image, uv=uv, sampling=sampling)


def _vertical_author_source(target, horizontal_image, horizontal_descriptor, horizontal_uv, sampling,
                            vertical_image, vertical_uv):
    if (not isinstance(vertical_image, bpy.types.Image) or vertical_image == horizontal_image
            or type(vertical_uv) is not str or not vertical_uv or vertical_uv == horizontal_uv['uv_name']):
        raise FaceSDFError('POLAR_LRUD requires a distinct vertical_image and another explicit vertical_uv layer')
    image = _image_content_descriptor(vertical_image, 'G')
    _image_content_descriptor(vertical_image, 'B')
    uv = _uv_descriptor(target.data, vertical_uv)
    vertical_sampling = sdf_filter.sampling_descriptor(sampling['radius_texels'], image['width'], image['height'])
    return _vertical_source_shape(dict(image=image, uv=uv, sampling=vertical_sampling),
        horizontal_descriptor, horizontal_uv, sampling)


def _reader_carrier_profile(profile, carrier, channel):
    """Each explicit reader generation admits only its own direction carrier."""
    expected = (_core().CONTINUITY_PROFILE if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE) else
                _core().DIRECTION_PROFILE if profile == DIRECTION_PROFILE else
                _core().POLAR_PROFILE if profile == POLAR_PROFILE else _core().PROFILE)
    if carrier['core_profile'] != expected:
        raise FaceSDFError('Reader and carrier explicit direction profiles differ')
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and channel != 'R':
        raise FaceSDFError('Three-dimensional readers require horizontal R encoding')


def _direction_correction(value, *, direction):
    if type(value) is not bool:
        raise FaceSDFError('direction_correction must be an explicit boolean')
    if not direction and value is not True:
        raise FaceSDFError('Legacy readers do not accept an unused direction_correction override')
    return value


def _continuity_keys(value):
    from . import face_direction_keys
    if type(value) is not dict or value != face_direction_keys.continuity_identity():
        raise FaceSDFError('Packed continuity key/layout identity differs')
    return value


def _pair_arguments(continuity, enabled, image, uv_name, radius):
    if not continuity or not enabled:
        if image is not None or uv_name is not None or radius is not None:
            raise FaceSDFError('Pair arguments must all be None outside enabled DIRECTION_CONTINUITY')
        return
    if image is None or type(uv_name) is not str or not uv_name or radius is None:
        raise FaceSDFError('Enabled DIRECTION_CONTINUITY requires pair_image, pair_uv and pair_filter_radius_texels')
    if _filter_radius(radius) <= 0.:
        raise FaceSDFError('Continuity pair filter radius must be positive')


def _json_value(group, key, label):
    try:
        raw = group[key]
        if type(raw) is not str:
            raise TypeError('expected stored JSON text')
        return json.loads(raw)
    except (KeyError, TypeError, ValueError) as error:
        raise FaceSDFError('Invalid ' + label + ' metadata') from error


def _pair_source_shape(value, horizontal, vertical, direction, *, enabled):
    from . import face_sdf_continuity
    if not enabled:
        if value is not None:
            raise FaceSDFError('Disabled continuity reader must carry pair_source=None')
        return None
    if type(value) is not dict or set(value) != {
            'image','uv','sampling','encoding','decode_scale','continuity_keys','top_transport'}:
        raise FaceSDFError('Continuity reader requires its exact fourth-source contract')
    image = _image_shape(value['image'], 'R')
    if (type(image.get('name')) is not str or not image['name'] or image['channels'] not in (3,4)
            or image['name'] in (horizontal.get('name'),vertical['image'].get('name'),direction['image'].get('name'))):
        raise FaceSDFError('Continuity pair must be a fourth dedicated RGB Image')
    uv = _uv_shape(value['uv'],vertical['uv']['vertices'])
    if uv['corners'] != vertical['uv']['corners']:
        raise FaceSDFError('Continuity pair UV corner topology differs')
    sampling = _sampling_shape(value['sampling'], profile=CONTINUITY_PROFILE,
        image_width=image['width'],image_height=image['height'])
    if (value['encoding'] != face_sdf_continuity.SOURCE_ENCODING
            or type(value['decode_scale']) not in (int,float) or value['decode_scale'] != face_sdf_continuity.DECODE_SCALE
            or value['top_transport'] != face_sdf_continuity.top_transport()):
        raise FaceSDFError('Continuity pair encoding/decode/top transport differs')
    identity = _continuity_keys(value['continuity_keys'])
    return dict(image=image,uv=uv,sampling=sampling,encoding=face_sdf_continuity.SOURCE_ENCODING,
        decode_scale=face_sdf_continuity.DECODE_SCALE,continuity_keys=identity,top_transport=face_sdf_continuity.top_transport())


def _pair_author_source(target, horizontal, vertical, direction, image, uv_name, radius):
    from . import face_direction_keys, face_sdf_continuity
    if not isinstance(image,bpy.types.Image):
        raise FaceSDFError('Continuity pair_image must be an actual Image ID')
    descriptor = _image_content_descriptor(image,'R')
    for channel in ('G','B'):
        _image_content_descriptor(image,channel)
    value = dict(image=descriptor,uv=_uv_descriptor(target.data,uv_name),
        sampling=sdf_filter.sampling_descriptor(_filter_radius(radius),descriptor['width'],descriptor['height']),
        encoding=face_sdf_continuity.SOURCE_ENCODING,decode_scale=face_sdf_continuity.DECODE_SCALE,
        continuity_keys=face_direction_keys.continuity_identity(),top_transport=face_sdf_continuity.top_transport())
    return _pair_source_shape(value,horizontal,vertical,direction,enabled=True)


def _direction_source_shape(value, horizontal_image, vertical_source, sampling):
    """Third Image/actual UV/filter and signed-key encoding, without size aliases."""
    from . import face_direction_keys, face_sdf_direction
    keys = {'image', 'uv', 'sampling', 'encoding', 'decode_scale', 'key_identity'}
    if type(value) is not dict or set(value) != keys:
        raise FaceSDFError('Direction-key reader requires its exact third-source contract')
    image = _image_shape(value['image'], 'R')
    if (type(image.get('name')) is not str or not image['name']
            or image['name'] in (horizontal_image.get('name'), vertical_source['image'].get('name'))
            or image['channels'] not in (3, 4)):
        raise FaceSDFError('Direction-key source must be a third dedicated RGB Image')
    uv = _uv_shape(value['uv'], vertical_source['uv']['vertices'])
    if uv['corners'] != vertical_source['uv']['corners']:
        raise FaceSDFError('Direction source UV must use the same target/corner topology')
    actual_sampling = _sampling_shape(value['sampling'], profile=DIRECTION_PROFILE,
        image_width=image['width'], image_height=image['height'])
    if actual_sampling['radius_texels'] != sampling['radius_texels']:
        raise FaceSDFError('Direction source must use the same explicit filter radius')
    if (value['encoding'] != face_sdf_direction.SOURCE_ENCODING
            or type(value['decode_scale']) not in (int, float)
            or value['decode_scale'] != face_sdf_direction.DECODE_SCALE
            or value['key_identity'] != face_direction_keys.identity()):
        raise FaceSDFError('Direction-key encoding, decode scale or registered key identity differs')
    return dict(image=image, uv=uv, sampling=actual_sampling,
                encoding=face_sdf_direction.SOURCE_ENCODING,
                decode_scale=face_sdf_direction.DECODE_SCALE,
                key_identity=face_direction_keys.identity())


def _direction_author_source(target, horizontal_image, horizontal_descriptor,
                             vertical_image, vertical_source, sampling, image, uv_name):
    from . import face_direction_keys, face_sdf_direction
    if not isinstance(image, bpy.types.Image) or image in (horizontal_image, vertical_image):
        raise FaceSDFError('DIRECTION_KEYS requires a third explicit direction_image')
    descriptor = _image_content_descriptor(image, 'R')
    # Both decoded channels must be valid; the R descriptor also signs all RGBA.
    _image_content_descriptor(image, 'G')
    uv = _uv_descriptor(target.data, uv_name)
    source = dict(image=descriptor, uv=uv,
        sampling=sdf_filter.sampling_descriptor(sampling['radius_texels'], descriptor['width'], descriptor['height']),
        encoding=face_sdf_direction.SOURCE_ENCODING, decode_scale=face_sdf_direction.DECODE_SCALE,
        key_identity=face_direction_keys.identity())
    return _direction_source_shape(source, horizontal_descriptor, vertical_source, sampling)


def _vertical_texture(node):
    return str(node.get(_NODE, '')).startswith('vertical_texture_')


def _direction_texture(node):
    return str(node.get(_NODE, '')).startswith('direction_texture_')


def _pair_texture(node):
    return str(node.get(_NODE, '')).startswith('continuity_texture_')


def _node_state(node):
    from .runtime_compat import signature_inputs
    properties = {}
    for key in ('operation', 'blend_type', 'use_clamp', 'clamp',
                'interpolation_type', 'data_type', 'attribute_name',
                'attribute_type', 'uv_map', 'projection', 'interpolation',
                'extension', 'mode', 'mute', 'is_active_output'):
        if hasattr(node, key):
            properties[key] = _value(getattr(node, key))
    if hasattr(node, 'image'):
        image = node.image
        properties['image'] = None if image is None else [
            'IMAGE', image.source, [int(image.size[0]), int(image.size[1])],
            bool(image.is_float), image.colorspace_settings.name]
    return [node.bl_idname, properties,
            [(socket.identifier, _value(socket.default_value))
             for socket in signature_inputs(node) if hasattr(socket, 'default_value')]]


def _signature(group):
    nodes = [(node.get(_NODE), _node_state(node)) for node in group.nodes]
    links = [(link.from_node.get(_NODE), link.from_socket.identifier,
              link.to_node.get(_NODE), link.to_socket.identifier)
             for link in group.links]
    interface = [{key: _value(getattr(socket, key)) for key in
                  ('identifier', 'name', 'in_out', 'socket_type', 'default_value',
                   'min_value', 'max_value') if hasattr(socket, key)}
                 for socket in group.interface.items_tree if socket.item_type == 'SOCKET']
    payload = dict(nodes=sorted(nodes, key=repr), links=sorted(links, key=repr),
                   interface=interface, profile=group.get(PROFILE_KEY),
                   owner=group.get(OWNER), uid=group.get(_UID),
                   target=group.get(_TARGET_DESC), fields=group.get(_FIELDS),
                   carrier=group.get(_CARRIER),
                   carrier_contract=group.get(_CARRIER_CONTRACT),
                   image=group.get(_IMAGE_DESC), uv=group.get(_UV_DESC),
                   channel=group.get(_CHANNEL), mirror=group.get(_MIRROR))
    # The v2 sampling contract is an additional signed payload element.  A v1
    # reader keeps exactly its original signature input and metadata.
    if group.get(PROFILE_KEY) in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload['sampling'] = group.get(_SAMPLING)
    if group.get(PROFILE_KEY) in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload['vertical_source'] = group.get(_VERTICAL_SOURCE)
    if group.get(PROFILE_KEY) in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload['direction_source'] = group.get(_DIRECTION_SOURCE)
        payload['direction_correction'] = group.get(_DIRECTION_CORRECTION)
    if group.get(PROFILE_KEY) in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload['pair_source'] = group.get(_PAIR_SOURCE)
        payload['continuity_keys'] = group.get(_CONTINUITY_KEYS)
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _animated(tree):
    animation = tree.animation_data
    return animation is not None and bool(animation.action or animation.drivers or animation.nla_tracks)


def _json_object(group, key, label):
    value = group.get(key)
    if type(value) is not str:
        raise FaceSDFError('Reader is missing ' + label + ' metadata')
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise FaceSDFError('Reader ' + label + ' metadata is not valid JSON') from error
    if type(result) is not dict:
        raise FaceSDFError('Reader ' + label + ' metadata must be an object')
    return result


def _same_float(actual, expected):
    actual = float(actual)
    expected = float(expected)
    if not math.isfinite(actual) or not math.isfinite(expected):
        return False
    if expected == 0.0:
        return actual == 0.0
    return math.isclose(actual, expected, rel_tol=1e-6, abs_tol=0.0)


def _expect_link(socket, source_node, identifier, label):
    if len(socket.links) != 1:
        raise FaceSDFError(label + ' must have exactly one explicit link')
    link = socket.links[0]
    if link.from_node != source_node or link.from_socket.identifier != identifier:
        raise FaceSDFError(label + ' is not bound to its explicit channel source')


def _expect_default(socket, expected, label):
    actual = socket.default_value
    if isinstance(expected, (tuple, list)):
        try:
            actual = tuple(float(item) for item in actual)
        except (TypeError, ValueError) as error:
            raise FaceSDFError(label + ' is not a finite vector') from error
        expected = tuple(float(item) for item in expected)
        if (len(actual) != len(expected)
                or any(not _same_float(a, b) for a, b in zip(actual, expected))):
            raise FaceSDFError(label + ' differs from the authored filter')
        return
    if not _same_float(actual, expected):
        raise FaceSDFError(label + ' differs from the authored filter')


def _filter_contract(by_key, info):
    """Re-derive the v2 3x3 kernel; a stored signature alone is not enough."""
    sampling = info['sampling']
    if type(sampling) is not dict:
        raise FaceSDFError('Filter reader is missing its sampling contract')
    step_u, step_v = sampling['uv_step']
    image = by_key['left_texture'].image
    if image is None:
        raise FaceSDFError('Filter reader has no explicit image')
    channel_name = _CHANNELS[info['channel']][0]
    base_nodes = {'left': by_key['uv'],
                  'right': (by_key['mirror_uv'] if info['mirror'] else by_key['uv'])}
    base_sockets = {'left': 'UV',
                    'right': 'Vector' if info['mirror'] else 'UV'}
    centers = {'left': by_key['left_separate'],
               'right': by_key['right_separate']}
    for side in _FILTER_SIDES:
        for index in _FILTER_TAPS:
            dx, dy, numerator = sdf_filter.KERNEL_WEIGHTS[index]
            weight = by_key[side + '_sample_weight_' + str(index)]
            if weight.operation != 'MULTIPLY':
                raise FaceSDFError('Filter tap weight is not a multiply: ' + side)
            if index == 4:
                _expect_link(weight.inputs[0], centers[side], channel_name,
                             'Filter centre channel')
            else:
                offset = by_key[side + '_uv_offset_' + str(index)]
                if offset.operation != 'ADD':
                    raise FaceSDFError('Filter UV offset is not an add: ' + side)
                _expect_link(offset.inputs[0], base_nodes[side], base_sockets[side],
                             'Filter UV base')
                _expect_default(offset.inputs[1],
                                (dx * step_u, dy * step_v, 0.0),
                                'Filter UV offset')
                texture = by_key[side + '_texture_' + str(index)]
                if (texture.image is None or texture.image != image
                        or texture.interpolation != 'Linear'
                        or texture.extension != 'EXTEND'
                        or texture.projection != 'FLAT'):
                    raise FaceSDFError('Filter tap image sampling differs: ' + side)
                _expect_link(texture.inputs['Vector'], offset, 'Vector',
                             'Filter tap UV')
                separate = by_key[side + '_separate_' + str(index)]
                if separate.mode != 'RGB':
                    raise FaceSDFError('Filter tap SeparateColor must use RGB')
                _expect_link(separate.inputs['Color'], texture, 'Color',
                             'Filter tap color')
                _expect_link(weight.inputs[0], separate, channel_name,
                             'Filter tap channel')
            _expect_default(weight.inputs[1], numerator / 16.0,
                            'Filter tap weight')
        previous = (by_key[side + '_sample_weight_0'], 'Value')
        for index in range(1, 9):
            total = by_key[side + '_sum_' + str(index - 1)]
            if total.operation != 'ADD':
                raise FaceSDFError('Filter tap accumulation is not an add: ' + side)
            _expect_link(total.inputs[0], previous[0], previous[1],
                         'Filter accumulated taps')
            _expect_link(total.inputs[1],
                         by_key[side + '_sample_weight_' + str(index)], 'Value',
                         'Filter next tap')
            previous = (total, 'Value')
        part = by_key[side + '_part']
        _expect_link(part.inputs[0], previous[0], previous[1],
                     'Filter final tap into the old side blend')


def _graph_contract(group, info):
    expected = dict(_NODE_TYPES)
    if info['mirror']:
        expected.update(_MIRROR_NODE_TYPES)
    if info['profile'] in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        expected.update(_filter_node_types())
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        from . import face_sdf_polar
        expected.update(face_sdf_polar.node_types())
    if info['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        from . import face_sdf_direction
        expected.update(face_sdf_direction.node_types())
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        from . import face_sdf_continuity
        expected.update(face_sdf_continuity.node_types(info['direction_correction'],authored=info['profile']==AUTHORED_PROFILE))
    actual = [node.get(_NODE) for node in group.nodes]
    if any(type(key) is not str for key in actual) or len(actual) != len(set(actual)):
        raise FaceSDFError('Reader nodes must have unique explicit keys')
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise FaceSDFError('Reader node set differs: missing=' + repr(missing) + ' extra=' + repr(extra))
    by_key = {node.get(_NODE): node for node in group.nodes}
    for key, kind in expected.items():
        if by_key[key].bl_idname != kind:
            raise FaceSDFError('Reader node type differs: ' + key)
    uv_node = by_key['uv']
    if uv_node.uv_map != info['uv']['uv_name']:
        raise FaceSDFError('Reader UVMap node does not use the explicit UV name')
    left = by_key['left_texture']
    right = by_key['right_texture']
    if (left.bl_idname != 'ShaderNodeTexImage' or right.bl_idname != 'ShaderNodeTexImage'
            or left.image is None or left.image != right.image):
        raise FaceSDFError('Reader left/right texture nodes must sample one explicit image')
    textures = [node for node in group.nodes if node.bl_idname == 'ShaderNodeTexImage']
    if len(textures) != _texture_count(info):
        raise FaceSDFError('Reader image-node count differs from its explicit profile')
    vertical_image = None
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        vertical_image = by_key['vertical_texture_4'].image
        if vertical_image is None or vertical_image == left.image:
            raise FaceSDFError('Polar vertical and horizontal sources must be distinct actual Image IDs')
    for texture in textures:
        if (texture.interpolation != 'Linear' or texture.extension != 'EXTEND'
                or texture.projection != 'FLAT'):
            raise FaceSDFError('Reader texture must use Linear/EXTEND/FLAT')
        expected_image = vertical_image if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and _vertical_texture(texture) else left.image
        if info['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and _direction_texture(texture):
            expected_image = by_key['direction_texture_4'].image
        if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and _pair_texture(texture):
            expected_image = by_key['continuity_texture_4'].image
        if texture.image is None or texture.image != expected_image:
            raise FaceSDFError('Reader texture nodes differ from their explicit horizontal/vertical Image group')
    for key in ('left_separate', 'right_separate'):
        if by_key[key].mode != 'RGB':
            raise FaceSDFError('Reader SeparateColor must use RGB mode')
    for key in ('attr_threshold', 'attr_phase', 'attr_mix', 'attr_valid'):
        node = by_key[key]
        if node.attribute_type != 'GEOMETRY' or node.attribute_name != info['fields'][key[5:]]:
            raise FaceSDFError('Reader Attribute node does not match the owned field: ' + key)
    if by_key['rgb_minimum'].blend_type != 'DARKEN' or by_key['rgb_minimum'].use_clamp:
        raise FaceSDFError('Reader shadow cap must be an unclamped DARKEN')
    if by_key['final_darkening'].blend_type != 'MIX' or by_key['final_darkening'].use_clamp:
        raise FaceSDFError('Reader final mix must be an unclamped MIX')
    sockets = [socket for socket in group.interface.items_tree if socket.item_type == 'SOCKET']
    inputs = [socket for socket in sockets if socket.in_out == 'INPUT']
    outputs = [socket for socket in sockets if socket.in_out == 'OUTPUT']
    names = [socket.name for socket in inputs]
    if names != ['Original Color', 'Shadow Color', 'Softness']:
        raise FaceSDFError('Reader interface inputs are not the ADD-only contract')
    if [socket.socket_type for socket in inputs] != ['NodeSocketColor', 'NodeSocketColor', 'NodeSocketFloat']:
        raise FaceSDFError('Reader interface input types are invalid')
    if len(outputs) != 1 or outputs[0].name != 'Color' or outputs[0].socket_type != 'NodeSocketColor':
        raise FaceSDFError('Reader interface must have exactly one Color output')
    if info['profile'] in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _filter_contract(by_key, info)
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        face_sdf_polar.validate(group, by_key, info, comparison=info['profile'] == POLAR_PROFILE)
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        overrides = face_sdf_continuity.validate(group, by_key, info)
        face_sdf_direction.validate(group, by_key, info, **overrides)
    elif info['profile'] == DIRECTION_PROFILE:
        face_sdf_direction.validate(group, by_key, info)


def _intrinsic(group):
    if not isinstance(group, bpy.types.NodeTree) or group.bl_idname != 'ShaderNodeTree':
        raise FaceSDFError('Reader must be a ShaderNodeTree')
    profile = group.get(PROFILE_KEY)
    if type(profile) is not str or profile not in SUPPORTED_PROFILES:
        raise FaceSDFError('Reader profile is not a supported explicit face-SDF profile')
    owner = group.get(OWNER)
    if not _is_owner(owner):
        raise FaceSDFError('Reader owner is invalid')
    uid = group.get(_UID)
    if type(uid) is not int:
        raise FaceSDFError('Reader modifier_uid is invalid')
    target = group.get(_TARGET)
    if target is not None and not isinstance(target, bpy.types.Object):
        raise FaceSDFError('Reader target is not an Object reference')
    fields = _fields(_json_object(group, _FIELDS, 'fields'))
    carrier = _json_object(group, _CARRIER, 'carrier')
    _carrier_shape(carrier, fields)
    if group.get(_CARRIER_CONTRACT) != CARRIER_CONTRACT:
        raise FaceSDFError('Reader carrier contract key is not ' + CARRIER_CONTRACT)
    image = _image_shape(_json_object(group, _IMAGE_DESC, 'image'))
    uv = _uv_shape(_json_object(group, _UV_DESC, 'UV'))
    target_desc = _target_shape(_json_object(group, _TARGET_DESC, 'target'), carrier['count'])
    if profile == PROFILE:
        if _SAMPLING in group:
            raise FaceSDFError('Shader v1 must not accept a v2 sampling descriptor')
        sampling = None
    else:
        sampling = _sampling_shape(
            _json_object(group, _SAMPLING, 'sampling'),
            profile=profile,
            image_width=image['width'], image_height=image['height'])
        if sampling['radius_texels'] <= 0.0:
            raise FaceSDFError('Shader v2 sampling radius must be positive')
    channel = group.get(_CHANNEL)
    mirror = group.get(_MIRROR)
    if channel not in _CHANNELS or type(mirror) is not bool:
        raise FaceSDFError('Reader channel/mirror metadata is invalid')
    if image['channel'] != channel:
        raise FaceSDFError('Reader image channel differs from its channel metadata')
    if uv['vertices'] != target_desc['vertices']:
        raise FaceSDFError('Reader UV and target vertex counts differ')
    if _animated(group):
        raise FaceSDFError('Reader graph has native animation; refusing to treat it as authored data')
    signature = group.get(_SIGNATURE)
    if not _is_sha(signature) or signature != _signature(group):
        raise FaceSDFError('Reader nodes, links, interface or metadata were edited')
    info = dict(profile=profile, owner=owner, uid=uid, fields=fields, carrier=carrier,
                image=image, uv=uv, target_desc=target_desc, sampling=sampling,
                channel=channel, mirror=mirror, signature=signature)
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        info['vertical_source'] = _vertical_source_shape(
            _json_object(group, _VERTICAL_SOURCE, 'vertical source'), image, uv, sampling)
    elif _VERTICAL_SOURCE in group:
        raise FaceSDFError('Legacy reader must not carry polar vertical-source metadata')
    _reader_carrier_profile(profile, carrier, channel)
    if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        info['direction_correction'] = _direction_correction(group.get(_DIRECTION_CORRECTION), direction=True)
        info['direction_source'] = _direction_source_shape(
            _json_object(group, _DIRECTION_SOURCE, 'direction source'), image, info['vertical_source'], sampling)
    elif _DIRECTION_SOURCE in group or _DIRECTION_CORRECTION in group:
        raise FaceSDFError('Legacy reader must not carry direction-key metadata')
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        info['continuity_keys'] = _continuity_keys(_json_object(group, _CONTINUITY_KEYS, 'continuity keys'))
        info['pair_source'] = _pair_source_shape(_json_value(group, _PAIR_SOURCE, 'pair source'),
            image, info['vertical_source'], info['direction_source'], enabled=info['direction_correction'])
    elif _PAIR_SOURCE in group or _CONTINUITY_KEYS in group:
        raise FaceSDFError('Legacy reader must not carry packed-pair metadata')
    _graph_contract(group, info)
    return info


def _binding_nodes(group):
    result = []
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == group:
                result.append((tree, node))
    return result


def _binding_node_state(node):
    return [node.bl_idname, {'mute': bool(node.mute)},
            [(socket.identifier, _value(socket.default_value))
             for socket in node.inputs if hasattr(socket, 'default_value')]]


def _binding_state(tree, token):
    def identity(node):
        if node.get(_TOKEN) == token:
            return ['reader', token]
        if node.get(_DEST) == token:
            return ['destination', token]
        if node.get(_SOURCE) == token:
            return ['source', token]
        return ['node', node.name]

    readers = [node for node in tree.nodes if node.get(_TOKEN) == token]
    destinations = [node for node in tree.nodes if node.get(_DEST) == token]
    if len(readers) != 1 or len(destinations) != 1:
        return None
    reader = readers[0]
    destination = destinations[0]
    links = [(identity(link.from_node), link.from_socket.identifier,
              identity(link.to_node), link.to_socket.identifier)
             for link in tree.links if link.from_node == reader or link.to_node == reader]
    try:
        destination_color = _value(_socket(destination.inputs, 'Color').default_value)
    except FaceSDFError:
        return None
    return json.dumps(dict(reader=_binding_node_state(reader),
        links=sorted(links, key=repr), destination_color=destination_color,
        original=reader.get(_ORIGINAL)), sort_keys=True, allow_nan=False)


def _related_animation(tree, sockets, nodes=()):
    from .face_material import _related_animation as helper
    return helper(tree, sockets, nodes)


def _binding_info(group):
    nodes = _binding_nodes(group)
    if group.users == 0:
        if nodes:
            raise FaceSDFError('Reader has consumers but no live node-tree user')
        return dict(state='unbound', nodes=[])
    if group.users != 2 or len(nodes) != 1:
        raise FaceSDFError('Reader is shared or has multiple material instances')
    tree, node = nodes[0]
    target = group.get(_TARGET)
    uid = group.get(_UID)
    if (node.get(_MARK) != BINDING
            or node.get(_TREE) != group
            or node.node_tree is not group
            or node.get(_TARGET) != target
            or node.get(_UID) != uid
            or node.get(OWNER) != group.get(OWNER)):
        raise FaceSDFError('Reader material instance markers/owner were changed')
    token = node.get(_TOKEN)
    if (type(token) is not str or len(token) != 32
            or any(c not in '0123456789abcdef' for c in token)):
        raise FaceSDFError('Reader material token is invalid')
    snapshot = node.get(_SNAPSHOT)
    if type(snapshot) is not str or _binding_state(tree, token) != snapshot:
        raise FaceSDFError('Reader material instance or links were edited')
    destinations = [item for item in tree.nodes if item.get(_DEST) == token]
    if len(destinations) != 1:
        raise FaceSDFError('Reader destination marker is missing or ambiguous')
    destination = destinations[0]
    try:
        color = _socket(destination.inputs, 'Color')
    except FaceSDFError as error:
        raise FaceSDFError('Reader destination has no unique Color input') from error
    if _related_animation(tree, list(node.inputs) + [color], nodes=[node]):
        raise FaceSDFError('Reader instance or destination has native animation')
    original_value = node.get(_ORIGINAL)
    if type(original_value) is not str:
        raise FaceSDFError('Reader original-source snapshot is missing')
    try:
        original = json.loads(original_value)
    except (TypeError, ValueError) as error:
        raise FaceSDFError('Reader original-source snapshot is invalid') from error
    if (type(original) is not dict or type(original.get('linked')) is not bool
            or type(original.get('source_socket')) not in (str, type(None))
            or type(original.get('color')) is not list or len(original['color']) != 4
            or any(not _finite(item) for item in original['color'])):
        raise FaceSDFError('Reader original-source snapshot has invalid fields')
    sources = [item for item in tree.nodes if item.get(_SOURCE) == token]
    if len(sources) != int(original['linked']):
        raise FaceSDFError('Reader original source identity changed')
    source = sources[0] if sources else None
    source_socket = None
    if source is not None:
        try:
            source_socket = _socket(source.outputs, original['source_socket'])
        except FaceSDFError as error:
            raise FaceSDFError('Reader original source socket changed') from error
    return dict(state='bound', tree=tree, node=node, destination=destination,
                color=color, source=source, source_socket=source_socket,
                original=original, token=token, snapshot=snapshot)


def _binding_report(info):
    if info['state'] == 'unbound':
        return dict(state='unbound')
    materials = [material for material in bpy.data.materials
                 if material.node_tree == info['tree']]
    if len(materials) != 1:
        raise FaceSDFError('SDF material node tree has no unique actual Material owner')
    return dict(state='bound', material=materials[0].name,
                node=info['node'].name, destination=info['destination'].name,
                destination_socket='Color',
                source=info['source'].name if info['source'] is not None else None,
                source_socket=info['original'].get('source_socket'),
                token=info['token'])


def _remove_instance(tree, node):
    for key in (_TREE, _TARGET):
        if key in node:
            del node[key]
    tree.nodes.remove(node)


def _clean_marker(node, key, token):
    if node.get(key) == token:
        del node[key]


def _unbound_readers(target, modifier, image_descriptor, uv_descriptor, channel, mirror):
    matches = []
    for group in bpy.data.node_groups:
        if group.bl_idname != 'ShaderNodeTree':
            continue
        if group.get(_TARGET) != target or group.get(_UID) != modifier.persistent_uid:
            continue
        if group.get(_CHANNEL) != channel or group.get(_MIRROR) is not mirror:
            continue
        if group.users != 0:
            continue
        try:
            info = _intrinsic(group)
        except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError):
            continue
        if not _same_image_identity(info['image'], image_descriptor):
            continue
        if info['uv'] != uv_descriptor:
            continue
        matches.append((group, info))
    return matches


def _unbound_reader(target, modifier, image_descriptor, uv_descriptor, channel, mirror,
                    profile, sampling, *, image, vertical_source=None, vertical_image=None,
                    direction_source=None, direction_image=None, direction_correction=True,
                    pair_source=None, pair_image=None):
    matches = [group for group, info in
               _unbound_readers(target, modifier, image_descriptor, uv_descriptor,
                                channel, mirror)
               if info['profile'] == profile
               and info['sampling'] == sampling
                and info.get('vertical_source') == vertical_source
                and info.get('direction_source') == direction_source
                and info.get('pair_source') == pair_source
                and (profile not in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) or info.get('direction_correction') == direction_correction)
               # Content equivalence cannot substitute a different explicit ID.
                and all(node.image == (pair_image if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and _pair_texture(node) else
                        direction_image if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and _direction_texture(node) else
                        vertical_image if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and _vertical_texture(node) else image)
                        for node in group.nodes if node.bl_idname == 'ShaderNodeTexImage')]
    if len(matches) > 1:
        raise FaceSDFError('Multiple unbound SDF readers match this target/image/UV; refusing ambiguity')
    return matches[0] if matches else None


def _add_interface(group):
    def add_input(name, kind, default):
        socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
        socket.default_value = default
        if kind == 'NodeSocketFloat':
            socket.min_value, socket.max_value = 0.0, 1.0
        return socket

    add_input('Original Color', 'NodeSocketColor', (1.0, 1.0, 1.0, 1.0))
    add_input('Shadow Color', 'NodeSocketColor', (.45, .34, .31, 1.0))
    add_input('Softness', 'NodeSocketFloat', .02)
    group.interface.new_socket(name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')


def _build_reader_group(target, modifier, image, uv_name, channel, mirror,
                        carrier, image_descriptor, uv_descriptor, target_descriptor,
                        *, sampling=None, polar=False, vertical_source=None, vertical_image=None,
                        direction=False, direction_source=None, direction_image=None, direction_correction=True,
                        continuity=False, pair_source=None, pair_image=None, authored=False):
    if type(authored) is not bool or (authored and not (continuity and direction and polar)):
        raise FaceSDFError('Authored reader requires its explicit polar/direction/continuity construction')
    profile = AUTHORED_PROFILE if authored else CONTINUITY_PROFILE if continuity else DIRECTION_PROFILE if direction else (POLAR_PROFILE if polar else (PROFILE if sampling is None else FILTER_PROFILE))
    direction_correction = _direction_correction(direction_correction, direction=direction)
    if sampling is not None:
        sampling = _sampling_shape(sampling, profile=profile,
                                   image_width=image_descriptor['width'],
                                   image_height=image_descriptor['height'])
    if polar:
        vertical_source = _vertical_source_shape(vertical_source, image_descriptor, uv_descriptor, sampling)
        if not isinstance(vertical_image, bpy.types.Image) or vertical_image == image:
            raise FaceSDFError('Polar builder requires a distinct explicit vertical Image ID')
    elif vertical_source is not None or vertical_image is not None:
        raise FaceSDFError('Legacy reader must not receive a vertical source')
    if direction:
        direction_source = _direction_source_shape(direction_source, image_descriptor, vertical_source, sampling)
        if not isinstance(direction_image, bpy.types.Image) or direction_image in (image, vertical_image):
            raise FaceSDFError('Direction builder requires a third explicit Image ID')
    elif direction_source is not None or direction_image is not None:
        raise FaceSDFError('Legacy reader must not receive a direction-key source')
    if continuity:
        pair_source = _pair_source_shape(pair_source, image_descriptor, vertical_source, direction_source, enabled=direction_correction)
        if direction_correction and (not isinstance(pair_image,bpy.types.Image) or pair_image in (image,vertical_image,direction_image)):
            raise FaceSDFError('Continuity builder requires a fourth actual Image ID')
    elif pair_source is not None or pair_image is not None:
        raise FaceSDFError('Legacy builder must not receive pair data')
    owner = uuid.uuid4().hex
    group = bpy.data.node_groups.new('Toon Face SDF Reader', 'ShaderNodeTree')
    try:
        group[PROFILE_KEY] = profile
        group[OWNER] = owner
        group[_TARGET] = target
        group[_UID] = modifier.persistent_uid
        group[_TARGET_DESC] = _canonical(target_descriptor)
        group[_FIELDS] = _canonical(_fields(carrier['fields']))
        group[_CARRIER] = _canonical(carrier)
        group[_CARRIER_CONTRACT] = CARRIER_CONTRACT
        group[_IMAGE_DESC] = _canonical(image_descriptor)
        group[_UV_DESC] = _canonical(uv_descriptor)
        group[_CHANNEL] = channel
        group[_MIRROR] = mirror
        if sampling is not None:
            group[_SAMPLING] = _canonical(sampling)
        if polar:
            group[_VERTICAL_SOURCE] = _canonical(vertical_source)
        if direction:
            group[_DIRECTION_SOURCE] = _canonical(direction_source)
            group[_DIRECTION_CORRECTION] = direction_correction
        if continuity:
            group[_PAIR_SOURCE] = _canonical(pair_source)
            from . import face_direction_keys
            group[_CONTINUITY_KEYS] = _canonical(face_direction_keys.continuity_identity())
        _add_interface(group)

        def node(key, kind, **properties):
            result = group.nodes.new(kind)
            result[_NODE] = key
            for name, value in properties.items():
                setattr(result, name, value)
            return result

        def link(source, destination):
            group.links.new(source, destination)

        def math_node(key, operation, a=None, b=None, c=None):
            result = node(key, 'ShaderNodeMath', operation=operation)
            for index, value in enumerate((a, b, c)):
                if value is None:
                    continue
                if type(value) in (int, float):
                    result.inputs[index].default_value = value
                else:
                    link(value, result.inputs[index])
            return result.outputs[0]

        entry = node('inputs', 'NodeGroupInput')
        output = node('output', 'NodeGroupOutput')
        uv = node('uv', 'ShaderNodeUVMap', uv_map=uv_descriptor['uv_name'])
        left_texture = node('left_texture', 'ShaderNodeTexImage', image=image,
                            interpolation='Linear', extension='EXTEND', projection='FLAT')
        right_texture = node('right_texture', 'ShaderNodeTexImage', image=image,
                             interpolation='Linear', extension='EXTEND', projection='FLAT')
        link(uv.outputs['UV'], left_texture.inputs['Vector'])
        right_base = uv.outputs['UV']
        if mirror:
            split = node('uv_split', 'ShaderNodeSeparateXYZ')
            link(uv.outputs['UV'], split.inputs['Vector'])
            mirror_x = node('mirror_x', 'ShaderNodeMath', operation='SUBTRACT')
            mirror_x.inputs[0].default_value = 1.0
            link(split.outputs['X'], mirror_x.inputs[1])
            combine = node('mirror_uv', 'ShaderNodeCombineXYZ')
            link(mirror_x.outputs[0], combine.inputs['X'])
            link(split.outputs['Y'], combine.inputs['Y'])
            combine.inputs['Z'].default_value = 0.0
            right_base = combine.outputs['Vector']
        link(right_base, right_texture.inputs['Vector'])
        left_separate = node('left_separate', 'ShaderNodeSeparateColor', mode='RGB')
        right_separate = node('right_separate', 'ShaderNodeSeparateColor', mode='RGB')
        link(left_texture.outputs['Color'], left_separate.inputs['Color'])
        link(right_texture.outputs['Color'], right_separate.inputs['Color'])
        channel_name = _CHANNELS[channel][0]
        left_channel = _socket(left_separate.outputs, channel_name)
        right_channel = _socket(right_separate.outputs, channel_name)
        left_scalar_source = left_channel
        right_scalar_source = right_channel

        if sampling is not None:
            step_u, step_v = sampling['uv_step']
            bases = {'left': uv.outputs['UV'], 'right': right_base}
            centers = {'left': left_separate, 'right': right_separate}
            for side in _FILTER_SIDES:
                outputs = {}
                for index in _FILTER_TAPS:
                    dx, dy, numerator = sdf_filter.KERNEL_WEIGHTS[index]
                    weight = node(side + '_sample_weight_' + str(index),
                                  'ShaderNodeMath', operation='MULTIPLY')
                    weight.inputs[1].default_value = numerator / 16.0
                    if index == 4:
                        link(_socket(centers[side].outputs, channel_name),
                             weight.inputs[0])
                    else:
                        offset = node(side + '_uv_offset_' + str(index),
                                      'ShaderNodeVectorMath', operation='ADD')
                        link(bases[side], offset.inputs[0])
                        offset.inputs[1].default_value = (
                            dx * step_u, dy * step_v, 0.0)
                        texture = node(side + '_texture_' + str(index),
                                       'ShaderNodeTexImage', image=image,
                                       interpolation='Linear',
                                       extension='EXTEND', projection='FLAT')
                        link(offset.outputs['Vector'], texture.inputs['Vector'])
                        separate = node(side + '_separate_' + str(index),
                                        'ShaderNodeSeparateColor', mode='RGB')
                        link(texture.outputs['Color'], separate.inputs['Color'])
                        link(_socket(separate.outputs, channel_name),
                             weight.inputs[0])
                    outputs[index] = weight.outputs['Value']
                previous = outputs[0]
                for index in range(1, 9):
                    total = node(side + '_sum_' + str(index - 1),
                                 'ShaderNodeMath', operation='ADD')
                    link(previous, total.inputs[0])
                    link(outputs[index], total.inputs[1])
                    previous = total.outputs['Value']
                if side == 'left':
                    left_scalar_source = previous
                else:
                    right_scalar_source = previous

        attributes = {}
        for key, role in (('attr_threshold', 'threshold'), ('attr_phase', 'phase'),
                          ('attr_mix', 'mix'), ('attr_valid', 'valid')):
            node_item = node(key, 'ShaderNodeAttribute', attribute_type='GEOMETRY',
                             attribute_name=carrier['fields'][role])
            attributes[role] = node_item.outputs['Fac']

        side_max = math_node('side_max', 'MINIMUM', attributes['threshold'], 1.0)
        side = math_node('side_min', 'MAXIMUM', side_max, 0.0)
        left_weight = math_node('left_weight', 'SUBTRACT', 1.0, side)
        left_part = math_node('left_part', 'MULTIPLY', left_scalar_source, left_weight)
        right_part = math_node('right_part', 'MULTIPLY', right_scalar_source, side)
        scalar = math_node('texture_scalar', 'ADD', left_part, right_part)

        softness = math_node('softness_max', 'MAXIMUM', entry.outputs['Softness'], 0.0)
        positive = math_node('softness_positive', 'GREATER_THAN', softness, 0.0)
        hard = math_node('hard_lit', 'GREATER_THAN', scalar, attributes['phase'])
        low = math_node('soft_low', 'SUBTRACT', attributes['phase'], softness)
        high = math_node('soft_high', 'ADD', attributes['phase'], softness)
        smooth = node('smooth_lit', 'ShaderNodeMapRange',
                      interpolation_type='SMOOTHSTEP', clamp=True)
        link(scalar, smooth.inputs['Value'])
        link(low, smooth.inputs['From Min'])
        link(high, smooth.inputs['From Max'])
        smooth.inputs['To Min'].default_value = 0.0
        smooth.inputs['To Max'].default_value = 1.0
        hard_weight = math_node('hard_weight', 'SUBTRACT', 1.0, positive)
        lit_hard = math_node('lit_hard', 'MULTIPLY', hard, hard_weight)
        lit_soft = math_node('lit_soft', 'MULTIPLY', smooth.outputs['Result'], positive)
        lit = math_node('lit', 'ADD', lit_hard, lit_soft)
        shadow = math_node('shadow', 'SUBTRACT', 1.0, lit)
        mix_max = math_node('mix_max', 'MINIMUM', attributes['mix'], 1.0)
        mix = math_node('mix_min', 'MAXIMUM', mix_max, 0.0)
        valid = math_node('valid_gate', 'GREATER_THAN', attributes['valid'], .5)
        darkening_mix = math_node('darkening_mix', 'MULTIPLY', shadow, mix)
        darkening = math_node('darkening', 'MULTIPLY', darkening_mix, valid)

        minimum = node('rgb_minimum', 'ShaderNodeMixRGB', blend_type='DARKEN', use_clamp=False)
        minimum.inputs[0].default_value = 1.0
        link(entry.outputs['Original Color'], minimum.inputs[1])
        link(entry.outputs['Shadow Color'], minimum.inputs[2])
        blend = node('final_darkening', 'ShaderNodeMixRGB', blend_type='MIX', use_clamp=False)
        link(darkening, blend.inputs[0])
        link(entry.outputs['Original Color'], blend.inputs[1])
        link(minimum.outputs[0], blend.inputs[2])
        link(blend.outputs[0], output.inputs['Color'])

        if polar:
            from . import face_sdf_polar
            face_sdf_polar.build(group, carrier['fields'], vertical_image, vertical_source)
        if direction:
            from . import face_sdf_direction
            face_sdf_direction.build(group, carrier['fields'], direction_image, direction_source,
                                     direction_correction=direction_correction)
        if continuity:
            from . import face_sdf_continuity
            face_sdf_continuity.build(group, carrier['fields'], pair_image, pair_source, enabled=direction_correction,authored=authored)
        for index, item in enumerate(group.nodes):
            item.location = ((index % 9) * 220, -(index // 9) * 200)
        group[_SIGNATURE] = _signature(group)
        _intrinsic(group)
        return group
    except Exception:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        raise


def shader_group(target, modifier, image, uv_name, *, channel='R', mirror=True,
                 filter_radius_texels=0.0, direction_mode='AZIMUTH', vertical_image=None, vertical_uv=None,
                 direction_image=None, direction_uv=None, direction_correction=True,
                 pair_image=None, pair_uv=None, pair_filter_radius_texels=None):
    """Create a validated, unbound SDF shader reader group.

    The image must already be a dedicated local FILE/PACKED Non-Color image.
    The UV must already exist as FLOAT2/CORNER with finite values in [0,1].
    No attribute or UV layer is created or changed here.

    ``filter_radius_texels=0`` reuses the original v1 reader exactly.  A
    positive value creates the explicit v2 3x3 image-space filter.  The radius
    is fixed by this construction; it is not a mutable managed-group input.
    POLAR_LRUD v3 requires a separate vertical_image/vertical_uv; its G/B data
    uses nine unmirrored taps and its own dimensions at the same radius.
    DIRECTION_KEYS v4 additionally requires a third direction_image and its
    explicit direction_uv. R/G signed distances use that Image's dimensions.
    Its static direction_correction=False keeps the LRUD response for a
    separate reader sharing the same new carrier (for example, a nose plane).
    DIRECTION_CONTINUITY v5 adds the explicit pair Image/UV and its independent
    positive sampling radius. A disabled v5 reader requires all pair arguments
    to be None and does not create pair texture/vector Attribute consumers.
    DIRECTION_AUTHORED v6 uses only the independently authored key8/key11 pair
    for the upper correction; other upper directions retain the LRUD residual.
    """
    mesh = _target(target)
    _explicit_modifier(target, modifier)
    radius = _filter_radius(filter_radius_texels)
    if type(channel) is not str or channel not in _CHANNELS:
        raise FaceSDFError('SDF channel must be R, G or B')
    if type(mirror) is not bool:
        raise FaceSDFError('mirror must be an explicit boolean')
    carrier_value = _carrier(target, modifier)
    polar = _direction_mode(direction_mode, carrier_value, channel, radius)
    direction = direction_mode in ('DIRECTION_KEYS', 'DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED')
    continuity = direction_mode in ('DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED')
    authored = direction_mode == 'DIRECTION_AUTHORED'
    direction_correction = _direction_correction(direction_correction, direction=direction)
    _pair_arguments(continuity, direction_correction, pair_image, pair_uv, pair_filter_radius_texels)
    if not polar and (vertical_image is not None or vertical_uv is not None):
        raise FaceSDFError('AZIMUTH does not accept an unused vertical Image/UV')
    if not direction and (direction_image is not None or direction_uv is not None):
        raise FaceSDFError('Legacy direction modes do not accept a direction Image/UV')
    image_value = _image_descriptor(image, channel)
    uv_value = _uv_descriptor(mesh, uv_name)
    target_value = _target_descriptor(target)
    profile, sampling = sdf_filter.sampling_for_radius(
        radius, image_value['width'], image_value['height'])
    vertical_source = None
    if polar:
        vertical_source = _vertical_author_source(target, image, image_value, uv_value, sampling, vertical_image, vertical_uv)
        _image_descriptor(vertical_image, 'G')
    direction_source = (_direction_author_source(target, image, image_value, vertical_image, vertical_source,
        sampling, direction_image, direction_uv) if direction else None)
    if direction:
        _image_descriptor(direction_image, 'R')
    pair_source = (_pair_author_source(target, image_value, vertical_source, direction_source,
        pair_image, pair_uv, pair_filter_radius_texels) if continuity and direction_correction else None)
    if pair_source is not None:
        _image_descriptor(pair_image, 'R')
    return _build_reader_group(target, modifier, image, uv_name, channel, mirror,
                               carrier_value, image_value, uv_value, target_value,
                               sampling=sampling, polar=polar,
                               vertical_source=vertical_source, vertical_image=vertical_image,
                               direction=direction, direction_source=direction_source, direction_image=direction_image,
                               direction_correction=direction_correction, continuity=continuity,
                               pair_source=pair_source, pair_image=pair_image,authored=authored)


def _direction_mode(mode, carrier_value, channel, radius):
    if mode not in ('AZIMUTH', 'POLAR_LRUD', 'DIRECTION_KEYS', 'DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED'):
        raise FaceSDFError('direction_mode must be AZIMUTH, POLAR_LRUD, DIRECTION_KEYS, DIRECTION_CONTINUITY or DIRECTION_AUTHORED')
    expected = {'AZIMUTH': _core().PROFILE, 'POLAR_LRUD': _core().POLAR_PROFILE,
                'DIRECTION_KEYS': _core().DIRECTION_PROFILE,
                'DIRECTION_CONTINUITY': _core().CONTINUITY_PROFILE,
                'DIRECTION_AUTHORED': _core().CONTINUITY_PROFILE}[mode]
    polar = mode in ('POLAR_LRUD', 'DIRECTION_KEYS', 'DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED')
    if carrier_value['core_profile'] != expected:
        raise FaceSDFError('Shader direction mode and carrier profile differ')
    if polar and (channel != 'R' or radius <= 0):
        raise FaceSDFError('POLAR_LRUD requires horizontal R, independent vertical G/B and a positive filter radius')
    return polar


def _resolve_existing_reader(target, modifier, image, uv_name, channel, mirror,
                             profile, sampling, *, vertical_source=None, vertical_image=None,
                             direction_source=None, direction_image=None, direction_correction=True,
                             pair_source=None, pair_image=None):
    image_value = _image_content_descriptor(image, channel)
    uv_value = _uv_descriptor(target.data, uv_name)
    reader = _unbound_reader(target, modifier, image_value, uv_value, channel, mirror,
                             profile, sampling, image=image,
                             vertical_source=vertical_source, vertical_image=vertical_image,
                             direction_source=direction_source, direction_image=direction_image,
                             direction_correction=direction_correction, pair_source=pair_source, pair_image=pair_image)
    if reader is not None:
        _image_descriptor(image, channel, allowed_consumers={reader})
        if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            _image_descriptor(vertical_image, 'G', allowed_consumers={reader})
        if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            _image_descriptor(direction_image, 'R', allowed_consumers={reader})
        if pair_source is not None:
            _image_descriptor(pair_image,'R',allowed_consumers={reader})
        return reader
    _image_descriptor(image, channel)
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _image_descriptor(vertical_image, 'G')
    if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _image_descriptor(direction_image, 'R')
    if pair_source is not None:
        _image_descriptor(pair_image,'R')
    return None


def _shadow_value(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(not _finite(item) for item in value)):
        raise FaceSDFError('shadow_color must be four finite RGBA values')
    return tuple(float(item) for item in value)


def _softness_value(value):
    if type(value) not in (int, float) or not math.isfinite(float(value)) or not 0.0 <= value <= 1.0:
        raise FaceSDFError('softness must be finite in [0,1], never bool')
    return float(value)


def _old_markers(tree):
    material_module = _material_core()
    keys = tuple(getattr(material_module, name) for name in ('_MARK', '_DEST', '_SOURCE'))
    return any(any(key in node for key in keys) for node in tree.nodes)


def _own_markers(tree):
    return any(any(key in node for key in (_MARK, _DEST, _SOURCE)) for node in tree.nodes)


def bind(context, image, uv_name, *, channel='R', mirror=True,
         shadow_color=(.45, .34, .31, 1.0), softness=.02,
         filter_radius_texels=0.0, direction_mode='AZIMUTH', vertical_image=None, vertical_uv=None,
         direction_image=None, direction_uv=None, direction_correction=True,
         pair_image=None, pair_uv=None, pair_filter_radius_texels=None):
    """Bind only the unique active exclusive material's unique Emission.Color.

    The original Emission.Color source (link or constant) is recorded and is
    restored exactly by :func:`unbind`.  An existing old ``face_material``
    binding is deliberately refused; unbind it explicitly first.

    ``filter_radius_texels=0`` keeps the original v1 reader.  A positive radius
    creates the explicit v2 3x3 filter and is fixed for this binding; changing
    it requires an explicit unbind/rebind.
    The explicit POLAR_LRUD mode additionally requires a distinct vertical_image
    and vertical_uv layer. No source Image, UV or pixel is created or changed.
    DIRECTION_KEYS also requires a third dedicated direction_image and its
    actual direction_uv; its registered key encoding/decode are fixed.
    direction_correction is an explicit static bool owned by that v4 reader.
    DIRECTION_CONTINUITY uses its own v5 reader with a separately sampled pair
    source. False requires pair_image/pair_uv/pair_filter_radius_texels=None.
    DIRECTION_AUTHORED v6 retains that source contract but restricts the upper
    correction to the independent key8/key11 author set.
    """
    target = _context_target(context)
    modifier = _managed(target)
    radius = _filter_radius(filter_radius_texels)
    polar = _direction_mode(direction_mode, _carrier(target, modifier), channel, radius)
    direction = direction_mode in ('DIRECTION_KEYS', 'DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED')
    continuity = direction_mode in ('DIRECTION_CONTINUITY', 'DIRECTION_AUTHORED')
    authored = direction_mode == 'DIRECTION_AUTHORED'
    direction_correction = _direction_correction(direction_correction, direction=direction)
    _pair_arguments(continuity, direction_correction, pair_image, pair_uv, pair_filter_radius_texels)
    if not polar and (vertical_image is not None or vertical_uv is not None):
        raise FaceSDFError('AZIMUTH does not accept an unused vertical Image/UV')
    if not direction and (direction_image is not None or direction_uv is not None):
        raise FaceSDFError('Legacy direction modes do not accept a direction Image/UV')
    color_value = _shadow_value(shadow_color)
    softness_value = _softness_value(softness)
    if type(channel) is not str or channel not in _CHANNELS:
        raise FaceSDFError('SDF channel must be R, G or B')
    if type(mirror) is not bool:
        raise FaceSDFError('mirror must be an explicit boolean')
    from .normal_ui import _material
    material = _material(target)
    tree = material.node_tree
    if _old_markers(tree):
        raise FaceSDFError('Activity material already has an old face_material binding; '
                           'explicitly unbind it before binding SDF')
    if _own_markers(tree):
        raise FaceSDFError('Activity material already has an SDF binding or residual markers')
    destination, color = _material_core()._emission(tree)
    if _related_animation(tree, [color]):
        raise FaceSDFError('Emission.Color has native animation; preserving it unchanged')
    if len(color.links) > 1:
        raise FaceSDFError('Emission.Color input is not unique')
    old = color.links[0] if color.links else None
    source, source_socket = (old.from_node, old.from_socket) if old else (None, None)
    if source == destination:
        raise FaceSDFError('Emission self-link cannot be safely restored')
    constant = tuple(float(item) for item in color.default_value)
    image_value = _image_content_descriptor(image, channel)
    profile, sampling = sdf_filter.sampling_for_radius(
        radius, image_value['width'], image_value['height'])
    if polar:
        profile = AUTHORED_PROFILE if authored else CONTINUITY_PROFILE if continuity else DIRECTION_PROFILE if direction else POLAR_PROFILE
    vertical_source = (_vertical_author_source(target, image, image_value, _uv_descriptor(target.data, uv_name),
        sampling, vertical_image, vertical_uv) if polar else None)
    direction_source = (_direction_author_source(target, image, image_value, vertical_image, vertical_source,
        sampling, direction_image, direction_uv) if direction else None)
    pair_source = (_pair_author_source(target, image_value, vertical_source, direction_source,
        pair_image, pair_uv, pair_filter_radius_texels) if continuity and direction_correction else None)
    reader = _resolve_existing_reader(target, modifier, image, uv_name, channel, mirror,
        profile, sampling, vertical_source=vertical_source, vertical_image=vertical_image,
        direction_source=direction_source, direction_image=direction_image, direction_correction=direction_correction,
        pair_source=pair_source, pair_image=pair_image)
    if reader is None:
        reader = shader_group(target, modifier, image, uv_name, channel=channel, mirror=mirror,
                              filter_radius_texels=radius, direction_mode=direction_mode,
                              vertical_image=vertical_image, vertical_uv=vertical_uv,
                              direction_image=direction_image, direction_uv=direction_uv,
                              direction_correction=direction_correction, pair_image=pair_image, pair_uv=pair_uv,
                              pair_filter_radius_texels=pair_filter_radius_texels)
    token = uuid.uuid4().hex
    node = None
    try:
        node = tree.nodes.new('ShaderNodeGroup')
        node.node_tree = reader
        node.label = 'SDF threshold reader (ADD-only)'
        node.location = (destination.location.x - 240, destination.location.y - 160)
        node[_MARK], node[_TOKEN], node[_TREE] = BINDING, token, reader
        node[_TARGET], node[_UID], node[OWNER] = target, modifier.persistent_uid, reader[OWNER]
        destination[_DEST] = token
        if source is not None:
            source[_SOURCE] = token
            tree.links.new(source_socket, node.inputs['Original Color'])
        else:
            node.inputs['Original Color'].default_value = constant
        node.inputs['Shadow Color'].default_value = color_value
        node.inputs['Softness'].default_value = softness_value
        node[_ORIGINAL] = _canonical(dict(linked=source is not None,
            source_socket=source_socket.identifier if source_socket else None,
            color=list(constant)))
        tree.links.new(node.outputs['Color'], color)
        node[_SNAPSHOT] = _binding_state(tree, token)
        if node[_SNAPSHOT] is None:
            raise FaceSDFError('Could not witness the new material binding')
    except Exception:
        if node is not None:
            _remove_instance(tree, node)
        _clean_marker(destination, _DEST, token)
        if source is not None:
            _clean_marker(source, _SOURCE, token)
            if not color.links:
                tree.links.new(source_socket, color)
        else:
            color.default_value = constant
        if reader.users == 0:
            bpy.data.node_groups.remove(reader)
        raise
    info = _intrinsic(reader)
    report = dict(profile=info['profile'], status='bound', material=material.name,
                  node=node.name, reader=reader.name, owner=info['owner'],
                  modifier_uid=modifier.persistent_uid,
                  carrier_contract=CARRIER_CONTRACT,
                  carrier=info['carrier'], image=info['image'], uv=info['uv'],
                  channel=info['channel'], mirror=info['mirror'],
                  original_source=node.get(_ORIGINAL), token=token)
    if info['profile'] in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report['sampling'] = info['sampling']
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report.update(_profile_fields(info))
    return report


def unbind(context):
    """Restore the exact original Emission.Color input without deleting image/UV."""
    target = _context_target(context)
    modifier = _managed(target)
    try:
        core_group, _interface, core_fields = _core()._owned(target, modifier)
    except (ValueError, TypeError, AttributeError, RuntimeError) as error:
        raise FaceSDFError('Face carrier ownership changed; refusing SDF unbind: ' + str(error)) from error
    from .normal_ui import _material
    material = _material(target)
    tree = material.node_tree
    from . import face_cast_art
    if face_cast_art.is_present(material):
        raise FaceSDFError('请先移除活动材质艺术投影接受层，再解绑SDF；保留当前作者状态')
    if _old_markers(tree):
        raise FaceSDFError('Old face_material markers are present; resolve the old binding explicitly first')
    marked = [node for node in tree.nodes if _MARK in node]
    if len(marked) != 1:
        raise FaceSDFError('No unique SDF material instance; refusing to delete author nodes')
    node = marked[0]
    token = node.get(_TOKEN)
    reader = node.node_tree if node.bl_idname == 'ShaderNodeGroup' else None
    if (node.get(_MARK) != BINDING or not isinstance(reader, bpy.types.NodeTree)
            or node.get(_TREE) != reader
            or type(reader.get(PROFILE_KEY)) is not str
            or reader.get(PROFILE_KEY) not in SUPPORTED_PROFILES
            or node.get(_TARGET) != target
            or node.get(_UID) != modifier.persistent_uid
            or node.get(OWNER) != reader.get(OWNER)
            or reader.get(_TARGET) != target
            or reader.get(_UID) != modifier.persistent_uid):
        raise FaceSDFError('SDF material instance, reader identity or owner was changed')
    if (not isinstance(token, str) or len(token) != 32
            or any(c not in '0123456789abcdef' for c in token)):
        raise FaceSDFError('SDF material token is invalid')
    if reader.users != 2:
        raise FaceSDFError('SDF reader has other consumers; refusing to remove its instance')
    try:
        intrinsic = _intrinsic(reader)
        info = _binding_info(reader)
    except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError) as error:
        raise FaceSDFError('SDF reader or material binding was edited; refusing unbind: ' + str(error)) from error
    if info['state'] != 'bound' or info['node'] != node or info['tree'] != tree:
        raise FaceSDFError('SDF binding identity changed; refusing unbind')
    destination, color = _material_core()._emission(tree)
    if info['destination'] != destination:
        raise FaceSDFError('SDF destination changed; refusing unbind')
    if core_fields != info['original'] if False else False:
        raise FaceSDFError('unreachable')
    core_carrier = info['carrier'] if 'carrier' in info else None
    del core_carrier
    original = info['original']
    source = info['source']
    source_socket = info['source_socket']
    if source_socket is not None:
        try:
            tree.links.new(source_socket, color)
        except Exception:
            tree.links.new(node.outputs['Color'], color)
            raise
    color.default_value = tuple(original['color'])
    name = node.name
    reader_name = reader.name
    _remove_instance(tree, node)
    _clean_marker(destination, _DEST, token)
    if source is not None:
        _clean_marker(source, _SOURCE, token)
    report = dict(profile=intrinsic['profile'], status='unbound', material=material.name,
                  removed_node=name, reader_retained=reader_name,
                  image_retained=intrinsic['image']['name'],
                  uv_retained=intrinsic['uv']['uv_name'],
                  carrier_contract=CARRIER_CONTRACT)
    if intrinsic['profile'] == FILTER_PROFILE:
        report['sampling'] = intrinsic['sampling']
    if intrinsic['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report.update(_profile_fields(intrinsic))
    return report


def reader_edited(group):
    """Conservative structural check, including the exact live owner binding."""
    try:
        info = _intrinsic(group)
        target = group.get(_TARGET)
        uid = group.get(_UID)
        if not isinstance(target, bpy.types.Object) or type(uid) is not int:
            return True
        matches = [modifier for modifier in target.modifiers
                   if modifier.persistent_uid == uid and modifier.type == 'NODES'
                   and _core().is_managed(modifier)]
        if len(matches) != 1:
            return True
        modifier = matches[0]
        try:
            core_group, _interface, core_fields = _core()._owned(target, modifier)
        except (ValueError, TypeError, AttributeError, RuntimeError):
            return True
        if core_fields != info['fields']:
            return True
        if core_group.get(_core().OWNER) != info['carrier']['core_owner']:
            return True
        if _core().graph_signature(core_group) != info['carrier']['core_signature']:
            return True
        _binding_info(group)
        return False
    except (FaceSDFError, ValueError, TypeError, KeyError, AttributeError,
            RuntimeError, ReferenceError):
        return True


def reader_descriptor(group):
    """Return a JSON-serialisable frozen descriptor; no live Object references."""
    info = _intrinsic(group)
    textures = [node for node in group.nodes if node.bl_idname == 'ShaderNodeTexImage']
    expected_textures = _texture_count(info)
    horizontal = ([node for node in textures if not _vertical_texture(node) and not _direction_texture(node) and not _pair_texture(node)]
                  if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) else textures)
    if (len(textures) != expected_textures or not horizontal or horizontal[0].image is None
            or any(texture.image != horizontal[0].image for texture in horizontal)):
        raise FaceSDFError('Reader has no unique actual image source')
    observed = _image_content_descriptor(horizontal[0].image, info['channel'])
    if not _same_image_identity(observed, info['image']):
        raise FaceSDFError('Actual image source or pixels changed from the authored descriptor')
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _vertical_live_source(group, info)
    if info['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _direction_live_source(group, info)
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and info['pair_source'] is not None:
        _pair_live_source(group, info)
    payload = dict(
        schema=(AUTHORED_DESCRIPTOR_SCHEMA if info['profile']==AUTHORED_PROFILE else
                CONTINUITY_DESCRIPTOR_SCHEMA if info['profile']==CONTINUITY_PROFILE else
                DIRECTION_DESCRIPTOR_SCHEMA if info['profile'] == DIRECTION_PROFILE else
                POLAR_DESCRIPTOR_SCHEMA if info['profile'] == POLAR_PROFILE else DESCRIPTOR_SCHEMA),
        profile=info['profile'],
        owner=info['owner'],
        target=info['target_desc'],
        modifier_uid=info['uid'],
        carrier_contract=CARRIER_CONTRACT,
        carrier=info['carrier'],
        image=info['image'],
        uv=info['uv'],
        channel=info['channel'],
        mirror=info['mirror'],
        shader=dict(name=group.name, signature=info['signature']),
    )
    if info['profile'] in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload['sampling'] = info['sampling']
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        if info['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            from .face_sdf_direction import ENCODING
            payload['direction_source'] = info['direction_source']
            payload['direction_correction'] = info['direction_correction']
        else:
            from .face_sdf_polar import ENCODING
        payload['direction_encoding'] = ENCODING
        payload['vertical_source'] = info['vertical_source']
    if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        payload.update(direction_encoding=_continuity_encoding(info['profile']), continuity_keys=info['continuity_keys'],pair_source=info['pair_source'])
    payload['descriptor_sha256'] = _descriptor_sha(payload)
    return payload


def _vertical_live_source(group, info, target=None, *, check_consumers=False):
    """Read actual vertical content; a source UV is checked only for live data.

    Frozen groups keep raw author descriptors while their copied Mesh may be
    evaluated/subdivided, so reader_descriptor alone must not infer live UVs.
    """
    node = next(item for item in group.nodes if item.get(_NODE) == 'vertical_texture_4')
    if node.image is None:
        raise FaceSDFError('Vertical actual Image source is unavailable')
    current = (_image_descriptor(node.image, 'G', allowed_consumers={group})
        if check_consumers else _image_content_descriptor(node.image, 'G'))
    stored = info['vertical_source']
    if (not _same_image_identity(current, stored['image'])
            or current['name'] != stored['image']['name']):
        raise FaceSDFError('Vertical actual G/RGBA content or Image identity changed')
    if target is not None and _uv_descriptor(target.data, stored['uv']['uv_name']) != stored['uv']:
        raise FaceSDFError('Vertical actual source UV changed')
    return stored


def _direction_live_source(group, info, target=None, *, check_consumers=False):
    node = next(item for item in group.nodes if item.get(_NODE) == 'direction_texture_4')
    if node.image is None:
        raise FaceSDFError('Direction-key actual Image source is unavailable')
    current = (_image_descriptor(node.image, 'R', allowed_consumers={group})
        if check_consumers else _image_content_descriptor(node.image, 'R'))
    _image_content_descriptor(node.image, 'G')
    stored = info['direction_source']
    if (not _same_image_identity(current, stored['image'])
            or current['name'] != stored['image']['name']):
        raise FaceSDFError('Direction actual R/G/RGBA content or Image identity changed')
    if target is not None and _uv_descriptor(target.data, stored['uv']['uv_name']) != stored['uv']:
        raise FaceSDFError('Direction actual source UV changed')
    return stored


def _pair_live_source(group, info, target=None, *, check_consumers=False):
    if info['pair_source'] is None:
        return None
    node = next(n for n in group.nodes if n.get(_NODE) == 'continuity_texture_4')
    if node.image is None:
        raise FaceSDFError('Continuity pair actual Image is unavailable')
    current = (_image_descriptor(node.image,'R',allowed_consumers={group}) if check_consumers
               else _image_content_descriptor(node.image,'R'))
    for channel in ('G','B'):
        _image_content_descriptor(node.image,channel)
    stored = info['pair_source']
    if not _same_image_identity(current,stored['image']) or current['name'] != stored['image']['name']:
        raise FaceSDFError('Continuity actual pair RGB/RGBA or Image identity changed')
    if target is not None and _uv_descriptor(target.data,stored['uv']['uv_name']) != stored['uv']:
        raise FaceSDFError('Continuity actual source UV changed')
    return stored


def _descriptor_sha(value):
    payload = dict(value)
    payload.pop('descriptor_sha256', None)
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _inspect_descriptor(value):
    if type(value) is not dict:
        raise FaceSDFError('Frozen reader descriptor must be an object')
    if 'descriptor' in value and type(value['descriptor']) is dict:
        value = value['descriptor']
    profile = value.get('profile')
    schema = (AUTHORED_DESCRIPTOR_SCHEMA if profile==AUTHORED_PROFILE else
              CONTINUITY_DESCRIPTOR_SCHEMA if profile==CONTINUITY_PROFILE else
              DIRECTION_DESCRIPTOR_SCHEMA if profile == DIRECTION_PROFILE else
              POLAR_DESCRIPTOR_SCHEMA if profile == POLAR_PROFILE else DESCRIPTOR_SCHEMA)
    if (value.get('schema') != schema or type(profile) is not str
            or profile not in SUPPORTED_PROFILES):
        raise FaceSDFError('Frozen reader descriptor schema/profile is invalid')
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        ENCODING = _continuity_encoding(profile)
    elif profile == DIRECTION_PROFILE:
        from .face_sdf_direction import ENCODING
    else:
        from .face_sdf_polar import ENCODING
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        if value.get('direction_encoding') != ENCODING:
            raise FaceSDFError('Frozen polar direction encoding differs')
    elif 'direction_encoding' in value or 'vertical_source' in value or 'direction_source' in value or 'direction_correction' in value:
        raise FaceSDFError('Legacy shader must not carry polar direction metadata')
    if not _is_owner(value.get('owner')):
        raise FaceSDFError('Frozen reader descriptor owner is invalid')
    if type(value.get('modifier_uid')) is not int:
        raise FaceSDFError('Frozen reader descriptor modifier_uid is invalid')
    if value.get('carrier_contract') != CARRIER_CONTRACT:
        raise FaceSDFError('Frozen reader descriptor carrier contract is invalid')
    # Frozen descriptor payloads contain a JSON object, not the IDProperty
    # JSON string used on live node groups. Validate that object directly.
    carrier = value.get('carrier')
    fields = _carrier_shape(carrier)
    _reader_carrier_profile(profile, carrier, value.get('channel'))
    image = _image_shape(value.get('image'))
    uv = _uv_shape(value.get('uv'), carrier['count'])
    target_desc = _target_shape(value.get('target'), carrier['count'])
    channel = value.get('channel')
    mirror = value.get('mirror')
    if channel not in _CHANNELS or type(mirror) is not bool:
        raise FaceSDFError('Frozen reader channel/mirror is invalid')
    if image['channel'] != channel:
        raise FaceSDFError('Frozen reader image/channel binding differs')
    if profile == PROFILE:
        if 'sampling' in value:
            raise FaceSDFError('Frozen shader v1 descriptor must not carry v2 sampling')
        sampling = None
    else:
        if 'sampling' not in value:
            raise FaceSDFError('Frozen shader v2 descriptor is missing its sampling contract')
        sampling = _sampling_shape(value['sampling'], profile=profile,
                                   image_width=image['width'],
                                   image_height=image['height'])
    vertical_source = None
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        vertical_source = _vertical_source_shape(value.get('vertical_source'), image, uv, sampling)
    direction_source = None
    if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        direction_correction = _direction_correction(value.get('direction_correction'), direction=True)
        direction_source = _direction_source_shape(value.get('direction_source'), image, vertical_source, sampling)
    elif 'direction_source' in value or 'direction_correction' in value:
        raise FaceSDFError('Legacy frozen shader must not carry direction-key metadata')
    pair_source = None
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        continuity_keys = _continuity_keys(value.get('continuity_keys'))
        pair_source = _pair_source_shape(value.get('pair_source'),image,vertical_source,direction_source,enabled=direction_correction)
    elif 'pair_source' in value or 'continuity_keys' in value:
        raise FaceSDFError('Legacy descriptor must not carry continuity metadata')
    shader = value.get('shader')
    if (type(shader) is not dict or type(shader.get('name')) is not str
            or not _is_sha(shader.get('signature'))):
        raise FaceSDFError('Frozen reader shader identity is invalid')
    if not _is_sha(value.get('descriptor_sha256')):
        raise FaceSDFError('Frozen reader descriptor hash is missing')
    if value['descriptor_sha256'] != _descriptor_sha(value):
        raise FaceSDFError('Frozen reader descriptor hash does not match its content')
    report = dict(schema=SCHEMA, profile=profile, status='frozen',
                  descriptor_state='frozen', live=False,
                  owner=value['owner'], target=target_desc['name'],
                  modifier_uid=value['modifier_uid'],
                  carrier_contract=CARRIER_CONTRACT, carrier=carrier,
                  image=image, uv=uv, channel=channel, mirror=mirror,
                  shader=shader, binding=None, issues=[],
                  note='Frozen descriptor only; no live modifier/material binding is asserted.',
                  descriptor=value)
    if sampling is not None:
        report['sampling'] = sampling
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report['direction_encoding'] = ENCODING
        report['vertical_source'] = vertical_source
    if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report['direction_source'] = direction_source
        report['direction_correction'] = direction_correction
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        report.update(pair_source=pair_source,continuity_keys=continuity_keys)
    return report


def _invalid(error, *, group=None):
    profile = PROFILE
    if isinstance(group, bpy.types.NodeTree):
        candidate = group.get(PROFILE_KEY)
        if type(candidate) is str and candidate in SUPPORTED_PROFILES:
            profile = candidate
    report = dict(schema=SCHEMA, profile=profile, status='invalid',
                  live=False, issues=[str(error)], binding=None)
    if isinstance(group, bpy.types.NodeTree):
        if profile in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            try:
                report['sampling'] = _sampling_shape(
                    _json_object(group, _SAMPLING, 'sampling'), profile=profile)
            except (FaceSDFError, ValueError, TypeError, KeyError):
                pass
        try:
            report['descriptor'] = reader_descriptor(group)
        except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError):
            pass
    return report


def _same_carrier_identity(current, stored):
    keys = ('contract', 'fields', 'left_attribute', 'right_attribute', 'weight_attribute',
            'left_sha256', 'right_sha256', 'core_profile', 'core_owner',
            'core_signature', 'count')
    if current.get('core_profile') == _core().CONTINUITY_PROFILE or stored.get('core_profile') == _core().CONTINUITY_PROFILE:
        keys += ('continuity_keys',)
    return all(current.get(key) == stored.get(key) for key in keys)


def inspect_reader(reader, *, target=None, modifier=None):
    """Read-only validation of a live reader, or of a frozen descriptor."""
    if type(reader) is dict:
        try:
            return _inspect_descriptor(reader)
        except (FaceSDFError, ValueError, TypeError, KeyError) as error:
            return _invalid(error)
    group = reader
    try:
        info = _intrinsic(group)
    except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError) as error:
        return _invalid(error, group=group)
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        try:
            descriptor = reader_descriptor(group)
        except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError) as error:
            return _invalid(error, group=group)
    else:
        descriptor = reader_descriptor(group)
    group_target = group.get(_TARGET)
    uid = group.get(_UID)
    live_target = target if isinstance(target, bpy.types.Object) else (
        group_target if isinstance(group_target, bpy.types.Object) else None)
    if target is not None and live_target != target:
        return _invalid('Explicit target differs from the reader target', group=group)
    if live_target is None:
        return dict(schema=SCHEMA, **_profile_fields(info), status='frozen',
                    descriptor_state='frozen', live=False,
                    owner=info['owner'], target=info['target_desc']['name'],
                    modifier_uid=uid, carrier_contract=CARRIER_CONTRACT,
                    carrier=info['carrier'], image=info['image'], uv=info['uv'],
                    channel=info['channel'], mirror=info['mirror'],
                    shader=dict(name=group.name, signature=info['signature']),
                    binding=None, issues=[],
                    note='Target Object is unavailable; frozen descriptor only.',
                    descriptor=descriptor)
    matches = [item for item in live_target.modifiers if item.persistent_uid == uid]
    if modifier is not None and modifier not in matches:
        return _invalid('Explicit modifier does not match the reader modifier_uid', group=group)
    if modifier is None:
        modifier = matches[0] if len(matches) == 1 else None
    if modifier is None:
        binding = None
        try:
            binding = _binding_report(_binding_info(group))
        except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError):
            pass
        return dict(schema=SCHEMA, **_profile_fields(info), status='frozen',
                    descriptor_state='frozen', live=False,
                    owner=info['owner'], target=live_target.name,
                    modifier_uid=uid, carrier_contract=CARRIER_CONTRACT,
                    carrier=info['carrier'], image=info['image'], uv=info['uv'],
                    channel=info['channel'], mirror=info['mirror'],
                    shader=dict(name=group.name, signature=info['signature']),
                    binding=binding, issues=[],
                    note='Live modifier is unavailable; frozen descriptor only.',
                    descriptor=descriptor)
    if modifier.type != 'NODES':
        return _invalid('Modifier with the reader UID is not a Geometry Nodes modifier', group=group)
    if modifier.node_group is None:
        return dict(schema=SCHEMA, **_profile_fields(info), status='frozen',
                    descriptor_state='frozen', live=False,
                    owner=info['owner'], target=live_target.name,
                    modifier_uid=uid, carrier_contract=CARRIER_CONTRACT,
                    carrier=info['carrier'], image=info['image'], uv=info['uv'],
                    channel=info['channel'], mirror=info['mirror'],
                    shader=dict(name=group.name, signature=info['signature']),
                    binding=None, issues=[],
                    note='Modifier node group is cleared; frozen descriptor only.',
                    descriptor=descriptor)
    if not _core().is_managed(modifier):
        return _invalid('Modifier is not the managed toon-face-threshold.v1 profile', group=group)
    try:
        current_carrier = _carrier(live_target, modifier)
    except (FaceSDFError, ValueError, TypeError, RuntimeError) as error:
        return _invalid(error, group=group)
    if not _same_carrier_identity(current_carrier, info['carrier']):
        return _invalid('Live carrier identity differs from the reader (left0_right1_side.v1)', group=group)
    left_texture = next((node for node in group.nodes if node.get(_NODE) == 'left_texture'), None)
    right_texture = next((node for node in group.nodes if node.get(_NODE) == 'right_texture'), None)
    if left_texture is None or right_texture is None or left_texture.image is None:
        return _invalid('Reader texture nodes are missing', group=group)
    try:
        current_image = _image_descriptor(left_texture.image, info['channel'], allowed_consumers={group})
    except (FaceSDFError, ValueError, TypeError, RuntimeError) as error:
        return _invalid(error, group=group)
    if not _same_image_identity(current_image, info['image']):
        return _invalid('Live image content/identity differs from the reader descriptor', group=group)
    try:
        current_uv = _uv_descriptor(live_target.data, info['uv']['uv_name'])
    except (FaceSDFError, ValueError, TypeError, RuntimeError) as error:
        return _invalid(error, group=group)
    if current_uv != info['uv']:
        return _invalid('Live UV content/identity differs from the reader descriptor', group=group)
    if info['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        try:
            _vertical_live_source(group, info, live_target, check_consumers=True)
            if info['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
                _direction_live_source(group, info, live_target, check_consumers=True)
            if info['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and info['pair_source'] is not None:
                _pair_live_source(group,info,live_target,check_consumers=True)
        except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError) as error:
            return _invalid(error, group=group)
    if (len(live_target.data.vertices) != info['target_desc']['vertices']
            or len(live_target.data.loops) != info['target_desc']['loops']):
        return _invalid('Live target topology extent differs from the reader descriptor', group=group)
    try:
        binding = _binding_report(_binding_info(group))
    except (FaceSDFError, ValueError, TypeError, KeyError, RuntimeError, ReferenceError) as error:
        return _invalid('Live material binding is invalid: ' + str(error), group=group)
    return dict(schema=SCHEMA, **_profile_fields(info), status='valid',
                descriptor_state='live', live=True,
                owner=info['owner'], target=live_target.name, modifier_uid=uid,
                carrier_contract=CARRIER_CONTRACT, carrier=current_carrier,
                image=current_image, uv=current_uv, channel=info['channel'],
                mirror=info['mirror'],
                shader=dict(name=group.name, signature=info['signature']),
                binding=binding, issues=[], descriptor=descriptor)


def validate_reader(reader, *, target=None, modifier=None, require_live=False):
    report = inspect_reader(reader, target=target, modifier=modifier)
    if report.get('status') == 'invalid':
        raise FaceSDFError('; '.join(report.get('issues', ['SDF reader is invalid'])))
    if require_live and report.get('status') != 'valid':
        raise FaceSDFError('SDF reader is not live: ' + str(report.get('status')))
    return report
