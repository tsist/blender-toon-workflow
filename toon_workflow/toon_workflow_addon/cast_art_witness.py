# SPDX-License-Identifier: GPL-3.0-or-later
"""Bpy-free integrity receipt for the optional face cast-art layer.

The accepting layer reuses the existing face SDF UV and mix/valid fields. Its
receipt therefore joins a separately validated SDF receipt instead of inventing
another geometry or UV identity. It certifies recorded data preservation, not
physical self-shadow separation, artistic quality or continuous animation.
"""
import hashlib
import json
import math
import re
import uuid

from . import sdf_witness
from .joint_time import MIN_FRAME, MAX_FRAME

LEGACY_SCHEMA = 'toon-face-cast-art-freeze.v1'
SCHEMA = 'toon-face-cast-art-freeze.v2'
PROFILE = 'toon-face-cast-art.v1'
PROFILE_V2 = 'toon-face-cast-art.v2'
DESCRIPTOR_SCHEMA = 'toon-face-cast-art-descriptor.v1'
DESCRIPTOR_SCHEMA_V2 = 'toon-face-cast-art-descriptor.v2'
ANIMATION_SCHEMA = 'toon-cast-animation.v1'
FORMULA = 'clamp(strength*mix*valid*clamp((white-channel)/(white-black),0,1),0,1)'

_IMAGE_KEYS = {
    'name', 'source_kind', 'packed', 'filepath', 'file_sha256',
    'packed_sha256', 'source_sha256', 'width', 'height', 'channels', 'depth',
    'is_float', 'precision', 'colorspace', 'single_tile', 'channel',
    'channel_index', 'pixel_count', 'pixel_sha256', 'rgba_sha256', 'min', 'max',
}
_IDENTITY_KEYS = {
    'profile', 'owner', 'target', 'modifier_uid', 'sdf_owner', 'material',
    'uv_name', 'fields', 'channel', 'image', 'settings',
}
_DESCRIPTOR_KEYS = _IDENTITY_KEYS | {
    'schema', 'interpolation', 'extension', 'mirror', 'formula',
    'graph_sha256', 'binding_sha256', 'descriptor_sha256', 'node',
    'output_socket',
}
_ROW_KEYS = _IDENTITY_KEYS | {
    'status', 'descriptor_before', 'descriptor_frozen', 'data_exact',
}
_ANIMATION_KEYS = {'schema', 'status', 'group', 'owner', 'animated_keys', 'action', 'curves'}
_ACTION_KEYS = {'name', 'uuid', 'profile', 'slot', 'layer', 'strip_type', 'channelbag_slot_handle'}
_CURVE_KEYS = {'key', 'data_path', 'array_index', 'extrapolation', 'mute', 'modifiers', 'keyframes'}
_POINT_KEYS = {
    'co', 'handle_left', 'handle_right', 'interpolation', 'easing',
    'handle_left_type', 'handle_right_type', 'back', 'amplitude', 'period',
    'type', 'select_control_point', 'select_left_handle', 'select_right_handle',
}


def _closed(value, keys, label):
    if type(value) is not dict or set(value) != keys:
        raise ValueError('Invalid or incomplete cast-art '+label+' fields')


def _name(value):
    return type(value) is str and bool(value) and '\x00' not in value


def _sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _owner(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _animation(value, owner):
    """Validate a native observation, without inferring shader evaluation.

    The JSON can attest structural closure and the before/frozen observation
    equality below. It cannot establish a Blender dependency graph or prove
    that a curve was sampled by the renderer.
    """
    _closed(value, _ANIMATION_KEYS, 'animation')
    if (value['schema'] != ANIMATION_SCHEMA or value['status'] != 'valid'
            or not _name(value['group']) or value['owner'] != owner
            or type(value['animated_keys']) is not list
            or type(value['curves']) is not list):
        raise ValueError('Invalid cast-art animation identity')
    action = value['action']
    if action is None:
        if value['animated_keys'] != [] or value['curves'] != []:
            raise ValueError('Static cast-art animation has curves or animated keys')
        return
    _closed(action, _ACTION_KEYS, 'animation Action')
    if (action['profile'] != ANIMATION_SCHEMA
            or not _name(action['name']) or not _name(action['layer'])
            or action['strip_type'] != 'KEYFRAME'
            or type(action['uuid']) is not str
            or type(action['channelbag_slot_handle']) is not int
            or action['channelbag_slot_handle'] < 1):
        raise ValueError('Invalid cast-art animation Action identity')
    try:
        action_id = uuid.UUID(action['uuid'])
    except (ValueError, AttributeError) as error:
        raise ValueError('Invalid cast-art animation Action UUID') from error
    if action_id.int == 0 or str(action_id) != action['uuid']:
        raise ValueError('Cast-art animation Action UUID is not canonical')
    slot = action['slot']
    _closed(slot, {'identifier', 'handle', 'target_id_type'}, 'animation slot')
    if (not _name(slot['identifier']) or type(slot['handle']) is not int
            or slot['handle'] < 1 or slot['handle'] != action['channelbag_slot_handle']
            or slot['target_id_type'] != 'NODETREE'):
        raise ValueError('Cast-art animation slot/channelbag identity differs')
    if value['animated_keys'] != ['strength'] or len(value['curves']) != 1:
        raise ValueError('Cast-art animation must have one strength curve')
    curve = value['curves'][0]
    _closed(curve, _CURVE_KEYS, 'animation curve')
    if (curve['key'] != 'strength' or not _name(curve['data_path'])
            or type(curve['array_index']) is not int or curve['array_index'] != 0
            or curve['extrapolation'] != 'CONSTANT' or curve['mute'] is not False
            or type(curve['modifiers']) is not list or curve['modifiers'] != []
            or type(curve['keyframes']) is not list or not curve['keyframes']):
        raise ValueError('Invalid cast-art animation strength curve')
    last = None
    for point in curve['keyframes']:
        _closed(point, _POINT_KEYS, 'animation keyframe')
        for key in ('co', 'handle_left', 'handle_right'):
            coordinate = point[key]
            if (type(coordinate) is not list or len(coordinate) != 2
                    or any(not _number(v) for v in coordinate)):
                raise ValueError('Cast-art keyframe coordinates must be finite pairs')
        frame, strength = point['co']
        if (not MIN_FRAME <= frame <= MAX_FRAME or not 0 <= strength <= 1
                or (last is not None and frame <= last)
                or point['interpolation'] != 'LINEAR'
                or any(not _name(point[key]) for key in
                       ('easing', 'handle_left_type', 'handle_right_type', 'type'))
                or any(not _number(point[key]) for key in ('back', 'amplitude', 'period'))
                or any(type(point[key]) is not bool for key in
                       ('select_control_point', 'select_left_handle', 'select_right_handle'))):
            raise ValueError('Invalid cast-art keyframe range, order or interpolation')
        last = frame


def _render_time(value):
    _closed(value, {'scene', 'frame', 'subframe'}, 'RENDER time')
    if (not _name(value['scene']) or type(value['frame']) is not int
            or not MIN_FRAME <= value['frame'] <= MAX_FRAME
            or type(value['subframe']) is not float or not _number(value['subframe'])
            or not -1 < value['subframe'] < 1):
        raise ValueError('Invalid cast-art RENDER time')


def _settings(value):
    _closed(value, {'strength', 'lit_color', 'black_level', 'white_level'},
            'settings')
    if not _number(value['strength']) or not 0 <= value['strength'] <= 1:
        raise ValueError('Cast-art strength must be finite in [0,1]')
    color = value['lit_color']
    if (type(color) is not list or len(color) != 4
            or any(not _number(v) or not 0 <= v <= 1 for v in color)):
        raise ValueError('Cast-art lit color must be four finite JSON values in [0,1]')
    black, white = value['black_level'], value['white_level']
    if (not _number(black) or not _number(white)
            or not 0 <= black < white <= 1):
        raise ValueError('Cast-art levels require finite 0 <= black < white <= 1')


def _image(value):
    _closed(value, _IMAGE_KEYS, 'image descriptor')
    if not _number(value['min']) or not _number(value['max']):
        raise ValueError('Cast-art image scalar bounds must be finite numbers')
    # Reuse the existing scalar/RGBA/source-byte and precision validation.
    # The additional closure below covers nullable local-file evidence emitted
    # by face_sdf._image_content_descriptor; no pixels are inferred here.
    sdf_witness._image(value)
    if value['source_kind'] == 'FILE':
        if value['packed_sha256'] is not None:
            raise ValueError('Cast-art FILE image unexpectedly has packed bytes')
    elif ((value['filepath'] is None) != (value['file_sha256'] is None)
          or (value['filepath'] is not None and not _name(value['filepath']))):
        raise ValueError('Cast-art packed image local-file evidence is inconsistent')


def _identity(value):
    if (value['profile'] not in (PROFILE, PROFILE_V2) or not _owner(value['owner'])
            or not _owner(value['sdf_owner'])
            or type(value['modifier_uid']) is not int
            or any(not _name(value[key]) for key in
                   ('target', 'material', 'uv_name'))
            or type(value['channel']) is not str
            or value['channel'] not in {'R', 'G', 'B'}):
        raise ValueError('Invalid cast-art layer/source identity')
    fields = value['fields']
    _closed(fields, {'mix', 'valid'}, 'field roles')
    if (any(not _name(name) for name in fields.values())
            or fields['mix'] == fields['valid']):
        raise ValueError('Invalid cast-art mix/valid field roles')
    _settings(value['settings'])
    _image(value['image'])
    if value['image']['channel'] != value['channel']:
        raise ValueError('Cast-art image and layer channels differ')


def _descriptor(value):
    v2 = type(value) is dict and value.get('profile') == PROFILE_V2
    _closed(value, _DESCRIPTOR_KEYS | ({'animation'} if v2 else set()), 'layer descriptor')
    _identity(value)
    if (value['schema'] != (DESCRIPTOR_SCHEMA_V2 if v2 else DESCRIPTOR_SCHEMA)
            or not _name(value['node']) or not _name(value['output_socket'])
            or value['interpolation'] != 'Linear'
            or value['extension'] != 'EXTEND'
            or value['mirror'] is not False
            or value['formula'] != FORMULA
            or not all(_sha(value[key]) for key in
                       ('graph_sha256', 'binding_sha256', 'descriptor_sha256'))):
        raise ValueError('Invalid cast-art sampling, formula or shader digest')
    if v2:
        _animation(value['animation'], value['owner'])
    payload = dict(value)
    payload.pop('descriptor_sha256')
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf8')
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid cast-art descriptor JSON') from error
    if hashlib.sha256(encoded).hexdigest() != value['descriptor_sha256']:
        raise ValueError('Cast-art descriptor content hash differs')


def validate_receipt(feature, sdf_feature=None, face_feature=None, *, expected_render_time=None):
    """Validate owned art data and join its exact existing SDF dependency.

    Descriptors come from actual before/frozen RENDER sampling. The SDF receipt
    supplies independent source/before/copied/frozen UV and carrier evidence;
    hashes in this receipt alone cannot establish that relationship. Missing
    optional feature keys belong to caller compatibility logic. An explicit
    empty feature is valid with no SDF dependency.
    """
    v2 = type(feature) is dict and feature.get('schema') == SCHEMA
    _closed(feature, {'schema', 'targets'} | ({'render_time'} if v2 else set()), 'feature')
    if (feature['schema'] not in (LEGACY_SCHEMA, SCHEMA) or type(feature['targets']) is not list
            or len(feature['targets']) > 256):
        raise ValueError('Unsupported cast-art freeze receipt')
    if v2:
        _render_time(feature['render_time'])
        if expected_render_time is not None:
            _render_time(expected_render_time)
            if feature['render_time'] != expected_render_time:
                raise ValueError('Cast-art RENDER time differs from the joint observation')
    if sdf_feature is None:
        if feature['targets']:
            raise ValueError('A nonempty cast-art receipt requires its SDF receipt')
        return 0
    sdf_witness.validate_receipt(sdf_feature, face_feature)
    sdf_rows = {row['owner']: row for row in sdf_feature['targets']}
    owners, materials = set(), set()
    for row in feature['targets']:
        _closed(row, _ROW_KEYS, 'target row')
        _identity(row)
        if not v2 and row['profile'] != PROFILE:
            raise ValueError('Legacy cast-art freeze cannot carry an animated layer')
        identity = (row['target'], row['material'])
        if (row['status'] != 'valid' or row['data_exact'] is not True
                or row['owner'] in owners or identity in materials):
            raise ValueError('Invalid, changed or duplicate cast-art target')
        before, frozen = row['descriptor_before'], row['descriptor_frozen']
        _descriptor(before)
        _descriptor(frozen)
        if before != frozen:
            raise ValueError('Cast-art shader, image or settings changed during freezing')
        if any(before[key] != row[key] for key in _IDENTITY_KEYS):
            raise ValueError('Cast-art descriptor and target row differ')
        source = sdf_rows.get(row['sdf_owner'])
        if (source is None or source['target'] != row['target']
                or source['modifier_uid'] != row['modifier_uid']
                or source['uv_name'] != row['uv_name']
                or any(source['attributes'][key] != row['fields'][key]
                       for key in ('mix', 'valid'))):
            raise ValueError('Cast-art source differs from its actual SDF receipt')
        binding_before = source.get('binding_before')
        binding_frozen = source.get('binding_frozen')
        binding_keys = {'state', 'material', 'node', 'destination',
                        'destination_socket', 'source', 'source_socket', 'token'}
        for binding in (binding_before, binding_frozen):
            _closed(binding, binding_keys, 'source SDF binding')
            if (binding['state'] != 'bound'
                    or binding['material'] != row['material']
                    or binding['source'] != before['node']
                    or binding['source_socket'] != before['output_socket']
                    or not _name(binding['node'])
                    or not _name(binding['destination'])
                    or binding['destination_socket'] != 'Color'
                    or not _owner(binding['token'])):
                raise ValueError('Cast-art instance is not the recorded SDF material source')
        if binding_before != binding_frozen:
            raise ValueError('Cast-art source SDF binding changed during freezing')
        owners.add(row['owner'])
        materials.add(identity)
    return len(owners)
