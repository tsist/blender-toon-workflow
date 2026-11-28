# SPDX-License-Identifier: GPL-3.0-or-later
"""UV/image/shader integrity witness for the optional face SDF sampler.

The geometry identity deliberately retains its existing meaning. This witness
has its own loop-UV bytes and explicit source bindings. It certifies neither a
mathematical signed-distance field nor the artistic or temporal appearance.
Only ``snapshot`` reads Blender data; receipt validation has no bpy dependency.

``toon-face-sdf-freeze.v2`` remains the multi-material freeze container and is
unrelated to the ``toon-face-sdf-shader.v2`` image-filter profile.
``toon-face-sdf-freeze.v3`` admits the polar/LRUD shader and material bindings;
the old freeze identities never silently accept a polar carrier or shader.
``toon-face-sdf-freeze.v4`` adds the explicit third-image directional-key source
and its actual loop-UV snapshots. Cast freeze.v3 signs its nested FTv3 identity.
Freeze.v5 retains the full upper-key continuity scope; freeze.v6 additionally
admits the explicitly selected authored-key shader without changing FT/cast.
"""
import hashlib
import json
import math
import re
import struct

try:  # Blender package import
    from . import sdf_filter
except ImportError:  # Standalone CPU validation
    import sdf_filter

SCHEMA = 'toon-face-sdf-freeze.v1'
MATERIAL_SCHEMA = 'toon-face-sdf-freeze.v2'
POLAR_SCHEMA = SCHEMA_V3 = 'toon-face-sdf-freeze.v3'
DIRECTION_SCHEMA = SCHEMA_V4 = 'toon-face-sdf-freeze.v4'
CONTINUITY_SCHEMA = SCHEMA_V5 = 'toon-face-sdf-freeze.v5'
AUTHORED_SCHEMA = SCHEMA_V6 = 'toon-face-sdf-freeze.v6'
PROFILE = sdf_filter.PROFILE_V1
FILTER_PROFILE = sdf_filter.PROFILE_V2
POLAR_PROFILE = sdf_filter.PROFILE_V3
DIRECTION_PROFILE = 'toon-face-sdf-shader.v4'
CONTINUITY_PROFILE = 'toon-face-sdf-shader.v5'
AUTHORED_PROFILE = 'toon-face-sdf-shader.v6'
POLAR_ENCODING = 'R=horizontal;vertical.GB=top_bottom;independent_UV;polar_angle/pi.v2'
DIRECTION_ENCODING = 'LRUD+signed_RG_keys;top_source_guard;polar_angle/pi.v1'
DIRECTION_SOURCE_ENCODING = 'R=upper;G=top;signed_model_distance;E=.5+.5*d/640'
PROFILES = sdf_filter.PROFILES
SUPPORTED_PROFILES = sdf_filter.SUPPORTED_PROFILES
CARRIER_CONTRACT = 'left0_right1_side.v1'
CAST_SCHEMA = 'toon-face-sdf-cast-freeze.v1'
CAST_SCHEMA_V2 = 'toon-face-sdf-cast-freeze.v2'
CAST_SCHEMA_V3 = 'toon-face-sdf-cast-freeze.v3'
CAST_SCHEMA_V4 = 'toon-face-sdf-cast-freeze.v4'
CAST_PROFILE = 'toon-face-sdf-cast.v1'
FACE_PROFILE = 'toon-face-threshold.v1'
FACE_PROFILE_V2 = 'toon-face-threshold.v2'
FACE_PROFILE_V3 = 'toon-face-threshold.v3'
FACE_PROFILE_V4 = 'toon-face-threshold.v4'
_OLD_FIELD_ROLES = ('threshold', 'phase', 'mix', 'valid')
_POLAR_FIELD_ROLES = _OLD_FIELD_ROLES + ('vertical_side', 'vertical_weight')
_DIRECTION_FIELD_ROLES = _POLAR_FIELD_ROLES + ('direction_upper', 'direction_top')
_CONTINUITY_FIELD_ROLES = _DIRECTION_FIELD_ROLES + ('direction_pair',)


def _encoding(profile):
    if profile == AUTHORED_PROFILE:
        from .face_sdf_continuity import AUTHORED_ENCODING
        return AUTHORED_ENCODING
    if profile == CONTINUITY_PROFILE:
        from .face_sdf_continuity import ENCODING
        return ENCODING
    return DIRECTION_ENCODING if profile == DIRECTION_PROFILE else POLAR_ENCODING


def _continuity_identity(value):
    from . import face_direction_keys
    if type(value) is not dict or value != face_direction_keys.continuity_identity():
        raise ValueError('Continuity packed key/layout identity differs')
    return value


def _face_profile(shader_profile):
    if shader_profile not in (PROFILE, FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        raise ValueError('Unsupported face-SDF shader profile')
    if shader_profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):return FACE_PROFILE_V4
    if shader_profile == DIRECTION_PROFILE:return FACE_PROFILE_V3
    return FACE_PROFILE_V2 if shader_profile == POLAR_PROFILE else FACE_PROFILE


def _field_roles(face_profile):
    if face_profile not in (FACE_PROFILE, FACE_PROFILE_V2, FACE_PROFILE_V3, FACE_PROFILE_V4):
        raise ValueError('Unsupported face-threshold carrier profile')
    if face_profile == FACE_PROFILE_V4:return _CONTINUITY_FIELD_ROLES
    if face_profile == FACE_PROFILE_V3:return _DIRECTION_FIELD_ROLES
    return _POLAR_FIELD_ROLES if face_profile == FACE_PROFILE_V2 else _OLD_FIELD_ROLES


def snapshot(mesh, uv_name):
    """Hash one explicitly named FLOAT2/CORNER UV map in loop order.

    Selection metadata and active-UV state are not touched or inferred. Values
    are finite little-endian float32 pairs; extent is descriptive, never a
    substitute for their full byte identity.
    """
    import numpy as np
    if not isinstance(uv_name, str) or not uv_name:
        raise ValueError('An explicit face-SDF UV name is required')
    attribute = mesh.attributes.get(uv_name)
    if (attribute is None or attribute.domain != 'CORNER'
            or attribute.data_type != 'FLOAT2'
            or len(attribute.data) != len(mesh.loops)
            or mesh.uv_layers.get(uv_name) is None):
        raise ValueError('Face-SDF UV map must be FLOAT2/CORNER: '+uv_name)
    values = np.empty(len(attribute.data)*2, dtype=np.dtype('<f4'))
    attribute.data.foreach_get('vector', values)
    if not np.isfinite(values).all():
        raise ValueError('Nonfinite face-SDF UV coordinate: '+uv_name)
    pairs = values.reshape(-1, 2)
    extent = dict(min=pairs.min(axis=0).tolist(), max=pairs.max(axis=0).tolist()) if len(pairs) else dict(min=None, max=None)
    return dict(uv_name=uv_name, domain='CORNER', data_type='FLOAT2',
                vertices=len(mesh.vertices), corners=len(mesh.loops),
                count=len(attribute.data), sha256=hashlib.sha256(values.tobytes()).hexdigest(),
                extent=extent)


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _name(value):
    return isinstance(value, str) and bool(value) and '\x00' not in value


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _uv(value, name):
    if (type(value) is not dict or value.get('uv_name') != name
            or value.get('domain') != 'CORNER' or value.get('data_type') != 'FLOAT2'
            or not _integer(value.get('vertices')) or not _integer(value.get('corners'))
            or type(value.get('count')) is not int or value['count'] != value['corners']
            or not _sha(value.get('sha256')) or type(value.get('extent')) is not dict
            or set(value['extent']) != {'min', 'max'}):
        raise ValueError('Invalid face-SDF loop-UV witness')
    extent = value['extent']
    if value['count'] == 0:
        if extent != dict(min=None, max=None):
            raise ValueError('Empty face-SDF UV witness has an extent')
        if value['sha256'] != hashlib.sha256(b'').hexdigest():
            raise ValueError('Empty face-SDF UV hash differs')
    else:
        for bound in ('min', 'max'):
            if (type(extent[bound]) is not list or len(extent[bound]) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in extent[bound])):
                raise ValueError('Invalid face-SDF UV extent')
        if any(a > b for a, b in zip(extent['min'], extent['max'])):
            raise ValueError('Reversed face-SDF UV extent')


def _constant_sha(count, value):
    """Bound memory while independently checking full float32 constant bytes."""
    word = struct.pack('<f', value)
    digest = hashlib.sha256()
    block = word*4096
    whole, remainder = divmod(count, 4096)
    for _ in range(whole):
        digest.update(block)
    digest.update(word*remainder)
    return digest.hexdigest()


def _carrier(value, row, vertices):
    """Check recorded actual FLOAT/POINT constants, not an inferred threshold."""
    if type(value) is not dict:
        raise ValueError('Missing face-SDF constant carrier witness')
    for role, constant in (('left', 0.0), ('right', 1.0)):
        field = value.get(role)
        if (type(field) is not dict or field.get('attribute') != row[role+'_attribute']
                or field.get('domain') != 'POINT' or field.get('data_type') != 'FLOAT'
                or type(field.get('count')) is not int or field['count'] != vertices
                or type(field.get('min')) not in (int, float) or field['min'] != constant
                or type(field.get('max')) not in (int, float) or field['max'] != constant
                or not _sha(field.get('sha256'))
                or field['sha256'] != _constant_sha(vertices, constant)):
            raise ValueError('Face-SDF carrier is not actual '+role+' constant '+str(constant))


def _image(value):
    # Selected scalar pixels and complete RGBA have distinct byte identities.
    if (type(value) is not dict or not _name(value.get('name'))
            or not _sha(value.get('pixel_sha256')) or not _sha(value.get('rgba_sha256'))
            or not _sha(value.get('source_sha256'))):
        raise ValueError('Missing face-SDF image pixel/source byte identity')
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError('Invalid face-SDF image descriptor key')
        if key.endswith('_sha256') and item is not None and not _sha(item):
            raise ValueError('Invalid face-SDF image digest: '+key)
    for key in ('width', 'height', 'channels', 'depth', 'pixel_count'):
        if not _integer(value.get(key), 1):
            raise ValueError('Invalid face-SDF image extent: '+key)
    if (not isinstance(value.get('channel'), str) or value['channel'] not in {'R', 'G', 'B'}
            or type(value.get('channel_index')) is not int
            or value['channel_index'] != {'R': 0, 'G': 1, 'B': 2}[value['channel']]
            or value['pixel_count'] != value['width']*value['height']*4
            or value.get('colorspace') != 'Non-Color' or value.get('single_tile') is not True
            or type(value.get('is_float')) is not bool
            or value.get('precision') != ('FLOAT32' if value['is_float'] else 'UINT8')
            or type(value.get('packed')) is not bool):
        raise ValueError('Invalid face-SDF image sampling/precision contract')
    if value.get('source_kind') == 'PACKED':
        if not value['packed'] or value['source_sha256'] != value.get('packed_sha256'):
            raise ValueError('Face-SDF packed source hash differs')
    elif value.get('source_kind') == 'FILE':
        if (value['packed'] or not _name(value.get('filepath'))
                or value['source_sha256'] != value.get('file_sha256')):
            raise ValueError('Face-SDF file source hash differs')
    else:
        raise ValueError('Unsupported face-SDF image source kind')
    if (type(value.get('min')) not in (int, float) or type(value.get('max')) not in (int, float)
            or not math.isfinite(value['min']) or not math.isfinite(value['max'])
            or not 0 <= value['min'] <= value['max'] <= 1):
        raise ValueError('Invalid face-SDF scalar pixel range')


def _sampling(row):
    """Keep shader v2 sampling explicit and keep shader v1 sampling-free."""
    profile = row.get('profile')
    if profile == PROFILE:
        if 'sampling' in row:
            raise ValueError('Face-SDF shader v1 receipt must not carry v2 sampling')
        return None
    if profile not in (FILTER_PROFILE, POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        raise ValueError('Unsupported face-SDF shader profile')
    value = row.get('sampling')
    image = row.get('image')
    if (type(value) is not dict or type(image) is not dict
            or type(image.get('width')) is not int
            or type(image.get('height')) is not int):
        raise ValueError('Face-SDF shader v2 receipt is missing its sampling/image extent')
    try:
        return sdf_filter.validate_sampling(
            value, profile=profile,
            image_width=image['width'], image_height=image['height'])
    except ValueError as error:
        raise ValueError('Invalid face-SDF shader v2 sampling: ' + str(error)) from error


def _vertical_uv(value, name, geometry):
    """Strict independent-layer range and the SAME raw/evaluated Mesh extent."""
    if not _name(name):raise ValueError('Polar face-SDF vertical UV name is missing')
    _uv(value, name)
    if (type(geometry) is not dict
            or any(value[key] != geometry.get(key) for key in ('vertices', 'corners', 'count'))):
        raise ValueError('Polar face-SDF vertical UV differs from its actual Mesh extent')
    if value['count'] and any(not 0 <= number <= 1 for bound in ('min', 'max') for number in value['extent'][bound]):
        raise ValueError('Polar face-SDF vertical UV coordinates must stay in [0,1]')


def _vertical_source(value, row):
    if type(value) is not dict or set(value) != {'image', 'uv', 'sampling'}:
        raise ValueError('Polar face-SDF requires its explicit independent vertical source')
    image=value['image'];uv=value['uv']
    _image(image)
    if (image['channel'] != 'G' or image['channels'] not in (3, 4)
            or image['name'] == row['image']['name']):
        raise ValueError('Polar face-SDF vertical source must be a distinct GB image described at G')
    if type(uv) is not dict or uv.get('uv_name') == row['uv_name']:
        raise ValueError('Polar face-SDF vertical source requires an independent UV layer')
    _vertical_uv(uv, uv.get('uv_name'), row.get('source_uv'))
    if uv != row.get('source_vertical_uv'):
        raise ValueError('Polar face-SDF vertical descriptor differs from the raw independent UV snapshot')
    try:
        sampling=sdf_filter.validate_sampling(value['sampling'],profile=POLAR_PROFILE,
            image_width=image['width'],image_height=image['height'])
    except ValueError as error:
        raise ValueError('Invalid polar vertical-source sampling: '+str(error)) from error
    if sampling['radius_texels'] != row['sampling']['radius_texels']:
        raise ValueError('Polar horizontal/vertical sampling radii differ')
    return value


def _direction_source(value, row):
    """Third image uses its own dimensions; full RGBA attests both R and G."""
    from . import face_direction_keys
    if type(value) is not dict or set(value) != {
            'image', 'uv', 'sampling', 'encoding', 'decode_scale', 'key_identity'}:
        raise ValueError('Directional face-SDF requires its explicit third source')
    image, uv = value['image'], value['uv']
    _image(image)
    if (image['channel'] != 'R' or image['channels'] not in (3, 4)
            or image['name'] in (row['image']['name'], row['vertical_source']['image']['name'])
            or value['encoding'] != DIRECTION_SOURCE_ENCODING
            or type(value['decode_scale']) not in (int, float) or value['decode_scale'] != 8.0
            or value['key_identity'] != face_direction_keys.identity()):
        raise ValueError('Directional face-SDF third image, encoding or key identity differs')
    if type(uv) is not dict:
        raise ValueError('Directional face-SDF third source UV is missing')
    _vertical_uv(uv, uv.get('uv_name'), row.get('source_uv'))
    if uv != row.get('source_direction_uv'):
        raise ValueError('Directional face-SDF descriptor differs from raw third-source UV')
    try:
        sampling = sdf_filter.validate_sampling(value['sampling'], profile=DIRECTION_PROFILE,
            image_width=image['width'], image_height=image['height'])
    except ValueError as error:
        raise ValueError('Invalid directional third-source sampling: '+str(error)) from error
    if sampling['radius_texels'] != row['sampling']['radius_texels']:
        raise ValueError('Directional third-source filter radius differs')
    return value


def _pair_source(value, row):
    from . import face_sdf_continuity
    enabled = row.get('direction_correction')
    if type(enabled) is not bool:
        raise ValueError('Continuity correction must be an explicit bool')
    if not enabled:
        if value is not None:
            raise ValueError('Disabled continuity must have pair_source=None')
        return None
    if type(value) is not dict or set(value) != {
            'image','uv','sampling','encoding','decode_scale','continuity_keys','top_transport'}:
        raise ValueError('Continuity fourth-source fields differ')
    image,uv = value['image'],value['uv']
    _image(image)
    if (image['channel'] != 'R' or image['channels'] not in (3,4)
            or image['name'] in (row['image']['name'],row['vertical_source']['image']['name'],row['direction_source']['image']['name'])
            or value['encoding'] != face_sdf_continuity.SOURCE_ENCODING
            or type(value['decode_scale']) not in (int,float) or value['decode_scale'] != face_sdf_continuity.DECODE_SCALE
            or value['top_transport'] != face_sdf_continuity.top_transport()):
        raise ValueError('Continuity fourth-source image/encoding/transport differs')
    if _continuity_identity(value['continuity_keys']) != row.get('continuity_keys'):
        raise ValueError('Continuity fourth-source key identity differs')
    if type(uv) is not dict:
        raise ValueError('Continuity fourth-source UV is missing')
    _vertical_uv(uv,uv.get('uv_name'),row.get('source_uv'))
    if uv != row.get('source_pair_uv'):
        raise ValueError('Continuity fourth-source raw UV differs')
    try:
        sdf_filter.validate_sampling(value['sampling'],profile=row['profile'],
            image_width=image['width'],image_height=image['height'])
    except ValueError as error:
        raise ValueError('Continuity fourth-source sampling differs: '+str(error)) from error
    return value


def _descriptor(value, row):
    profile = row.get('profile')
    if (type(value) is not dict
            or value.get('schema') != ('toon-face-sdf-reader-descriptor.v5'
                if profile == AUTHORED_PROFILE else 'toon-face-sdf-reader-descriptor.v4'
                if profile == CONTINUITY_PROFILE else 'toon-face-sdf-reader-descriptor.v3'
                if profile == DIRECTION_PROFILE else 'toon-face-sdf-reader-descriptor.v2'
                if profile == POLAR_PROFILE else 'toon-face-sdf-reader-descriptor.v1')
            or value.get('profile') != profile
            or profile not in PROFILES
            or not _sha(value.get('descriptor_sha256'))):
        raise ValueError('Missing face-SDF shader/image descriptor')
    payload = dict(value)
    payload.pop('descriptor_sha256')
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf8')
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid face-SDF descriptor JSON payload') from error
    if hashlib.sha256(encoded).hexdigest() != value['descriptor_sha256']:
        raise ValueError('Face-SDF descriptor content hash differs')
    for key in ('profile', 'owner', 'modifier_uid', 'carrier_contract', 'channel', 'mirror', 'image'):
        if value.get(key) != row[key]:
            raise ValueError('Face-SDF descriptor binding differs: '+key)
    if profile in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        encoding = _encoding(profile)
        if value.get('direction_encoding') != encoding or row.get('direction_encoding') != encoding:
            raise ValueError('Polar face-SDF descriptor/receipt direction encoding differs')
        if _vertical_source(value.get('vertical_source'),row) != row.get('vertical_source'):
            raise ValueError('Polar face-SDF descriptor/receipt vertical source differs')
    elif 'direction_encoding' in value or 'vertical_source' in value:
        raise ValueError('Legacy face-SDF descriptor must not carry polar source metadata')
    if profile in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
        if (type(value.get('direction_correction')) is not bool
                or value['direction_correction'] != row.get('direction_correction')):
            raise ValueError('Directional face-SDF correction mode differs from its receipt')
        if _direction_source(value.get('direction_source'),row) != row.get('direction_source'):
            raise ValueError('Directional face-SDF descriptor/receipt third source differs')
    elif 'direction_source' in value or 'direction_correction' in value:
        raise ValueError('Legacy face-SDF descriptor must not carry a third source')
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        _continuity_identity(value.get('continuity_keys'))
        if value.get('continuity_keys') != row.get('continuity_keys') or 'pair_source' not in value:
            raise ValueError('Continuity descriptor key identity/pair presence differs')
        if _pair_source(value['pair_source'],row) != row.get('pair_source'):
            raise ValueError('Continuity descriptor fourth source differs')
    elif 'pair_source' in value or 'continuity_keys' in value:
        raise ValueError('Legacy descriptor must not carry continuity metadata')
    target = value.get('target')
    if (type(target) is not dict or target.get('name') != row['target']
            or not _name(target.get('mesh'))
            or type(target.get('vertices')) is not int or target['vertices'] != row['source_uv']['vertices']
            or type(target.get('loops')) is not int or target['loops'] != row['source_uv']['corners']
            or value.get('uv') != row['source_uv']):
        raise ValueError('Face-SDF descriptor original target/UV binding differs')
    carrier = value.get('carrier')
    if (type(carrier) is not dict or carrier.get('contract') != CARRIER_CONTRACT
            or carrier.get('fields') != row['attributes'] or carrier.get('core_profile') != _face_profile(profile)
            or not isinstance(carrier.get('core_owner'), str)
            or re.fullmatch('[0-9a-f]{32}', carrier['core_owner']) is None
            or not _sha(carrier.get('core_signature')) or not _sha(carrier.get('weight_sha256'))
            or not _integer(carrier.get('count'), 1) or carrier['count'] != target['vertices']
            or carrier.get('side_semantics') != 'threshold=side_weight: 0=reference -X side, 1=reference +X side'):
        raise ValueError('Face-SDF descriptor carrier identity differs')
    if profile in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
        if carrier.get('continuity_keys') != value['continuity_keys']:
            raise ValueError('Continuity descriptor carrier layout identity differs')
    elif 'continuity_keys' in carrier:
        raise ValueError('Legacy carrier must not carry packed continuity identity')
    fields = {role: 'toon_face_'+role+'_'+carrier['core_owner'] for role in _field_roles(_face_profile(profile))}
    if carrier['fields'] != fields:
        raise ValueError('Face-SDF fields are not owned by their explicit face carrier')
    for role in ('left', 'right', 'weight'):
        if carrier.get(role+'_attribute') != row[role+'_attribute']:
            raise ValueError('Face-SDF descriptor source binding differs: '+role)
    for role in ('left', 'right'):
        if carrier.get(role+'_sha256') != row['source_carrier'][role]['sha256']:
            raise ValueError('Face-SDF descriptor actual source constant hash differs: '+role)
    if profile == PROFILE:
        if 'sampling' in value:
            raise ValueError('Face-SDF v1 descriptor must not carry v2 sampling')
    else:
        if 'sampling' not in value:
            raise ValueError('Face-SDF v2 descriptor is missing its sampling contract')
        image = value.get('image')
        if type(image) is not dict:
            raise ValueError('Face-SDF v2 descriptor image is missing')
        try:
            sampling = sdf_filter.validate_sampling(
                value['sampling'], profile=profile,
                image_width=image.get('width'), image_height=image.get('height'))
        except ValueError as error:
            raise ValueError('Invalid face-SDF v2 descriptor sampling: ' + str(error)) from error
        if sampling != row.get('sampling'):
            raise ValueError('Face-SDF v2 descriptor sampling differs from the receipt')
    shader = value.get('shader')
    if (type(shader) is not dict or not _name(shader.get('name'))
            or shader.get('signature') != row['reader_signature']):
        raise ValueError('Face-SDF descriptor shader graph signature differs')
    return carrier['core_owner']


def schema_for_targets(rows):
    """Keep the v1 identity for single-reader objects, including old outputs."""
    rows = tuple(rows)
    if any(type(row) is not dict or row.get('profile') not in PROFILES for row in rows):
        raise ValueError('Unsupported face-SDF target profile')
    if any(row['profile'] == AUTHORED_PROFILE for row in rows):
        return AUTHORED_SCHEMA
    if any(row['profile'] == CONTINUITY_PROFILE for row in rows):
        return CONTINUITY_SCHEMA
    if any(row['profile'] == DIRECTION_PROFILE for row in rows):
        return DIRECTION_SCHEMA
    if any(row['profile'] == POLAR_PROFILE for row in rows):
        return POLAR_SCHEMA
    names = [row['target'] for row in rows]
    return MATERIAL_SCHEMA if len(set(names)) != len(names) else SCHEMA


def cast_schema_for_targets(rows):
    """Version nested face-source identities without changing sensor semantics."""
    profiles = [row.get('face', {}).get('profile') if type(row) is dict
        and type(row.get('face')) is dict else None for row in rows]
    if any(profile not in (FACE_PROFILE, FACE_PROFILE_V2, FACE_PROFILE_V3, FACE_PROFILE_V4) for profile in profiles):
        raise ValueError('Unsupported face-SDF cast face-source profile')
    if FACE_PROFILE_V4 in profiles:return CAST_SCHEMA_V4
    if FACE_PROFILE_V3 in profiles:return CAST_SCHEMA_V3
    return CAST_SCHEMA_V2 if FACE_PROFILE_V2 in profiles else CAST_SCHEMA


def _material_binding(row):
    value = row.get('material_binding_before')
    if (type(value) is not dict or set(value) != {'target', 'material', 'slots'}
            or value.get('target') != row['target'] or not _name(value.get('material'))
            or type(value.get('slots')) is not list or not value['slots']
            or any(type(i) is not int or not 0 <= i < 4096 for i in value['slots'])
            or value['slots'] != sorted(set(value['slots']))
            or value != row.get('material_binding_frozen')):
        raise ValueError('Invalid or changed face-SDF material/slot identity')
    for key in ('binding_before', 'binding_frozen'):
        binding = row.get(key)
        if (type(binding) is not dict or set(binding) != {'state', 'material', 'node',
                'destination', 'destination_socket', 'source', 'source_socket', 'token'}
                or binding.get('state') != 'bound' or binding.get('material') != value['material']
                or any(not _name(binding.get(name)) for name in ('node', 'destination'))
                or binding.get('destination_socket') != 'Color'
                or not isinstance(binding.get('token'), str)
                or re.fullmatch('[0-9a-f]{32}', binding['token']) is None
                or (binding.get('source') is None) != (binding.get('source_socket') is None)
                or (binding.get('source') is not None and
                    (not _name(binding['source']) or not _name(binding['source_socket'])))):
            raise ValueError('Invalid face-SDF material binding witness')
    return value


def validate_receipt(feature, face_feature=None):
    """Validate frozen data/descriptors without bpy or a surviving modifier.

    The live ``face_sdf`` validator must run before freezing. ``source_uv`` and
    ``source_carrier`` describe author data; subdivision may change their extent.
    Only actual RENDER ``before/copied/frozen`` UV and descriptors must be exact.
    Supplying the independently validated face-v1 feature additionally binds each
    SDF target to the actual carrier owner, modifier and generated attribute roles.
    """
    if (type(feature) is not dict or feature.get('schema') not in {SCHEMA, MATERIAL_SCHEMA, POLAR_SCHEMA, DIRECTION_SCHEMA, CONTINUITY_SCHEMA, AUTHORED_SCHEMA}
            or type(feature.get('targets')) is not list or len(feature['targets']) > 256):
        raise ValueError('Unsupported face-SDF freeze witness')
    has_polar = any(type(row) is dict and row.get('profile') == POLAR_PROFILE for row in feature['targets'])
    has_direction = any(type(row) is dict and row.get('profile') == DIRECTION_PROFILE for row in feature['targets'])
    has_continuity = any(type(row) is dict and row.get('profile') == CONTINUITY_PROFILE for row in feature['targets'])
    has_authored = any(type(row) is dict and row.get('profile') == AUTHORED_PROFILE for row in feature['targets'])
    if has_authored != (feature['schema'] == AUTHORED_SCHEMA):
        raise ValueError('Selected authored-key shader requires its explicit freeze.v6 identity')
    if not has_authored and has_continuity != (feature['schema'] == CONTINUITY_SCHEMA):
        raise ValueError('Continuity shader requires its explicit freeze.v5 identity')
    if not has_authored and not has_continuity and has_direction != (feature['schema'] == DIRECTION_SCHEMA):
        raise ValueError('Directional face-SDF shader requires its explicit freeze.v4 identity')
    if not has_authored and not has_continuity and not has_direction and has_polar != (feature['schema'] == POLAR_SCHEMA):
        raise ValueError('Face-SDF polar shader requires its explicit freeze.v3 identity')
    face_rows = None
    if face_feature is not None:
        from .face_witness import validate_receipt as validate_face
        validate_face(face_feature)
        face_rows = {row['owner']: row for row in face_feature['targets']}
    targets = set()
    owners = set()
    materials, readers, occupied, carrier_rows = set(), set(), set(), {}
    multi = feature['schema'] in (MATERIAL_SCHEMA, POLAR_SCHEMA, DIRECTION_SCHEMA, CONTINUITY_SCHEMA, AUTHORED_SCHEMA)
    for row in feature['targets']:
        if type(row) is not dict:
            raise ValueError('Invalid face-SDF target row')
        owner, target = row.get('owner'), row.get('target')
        if (row.get('profile') not in PROFILES or row.get('status') != 'valid'
                or row.get('data_exact') is not True
                or not _name(target) or (not multi and target in targets)
                or not isinstance(owner, str) or re.fullmatch('[0-9a-f]{32}', owner) is None
                or owner in owners or type(row.get('modifier_uid')) is not int
                or row.get('carrier_contract') != CARRIER_CONTRACT
                or not _name(row.get('uv_name')) or not isinstance(row.get('channel'), str)
                or row['channel'] not in {'R', 'G', 'B'}
                or type(row.get('mirror')) is not bool or row.get('interpolation') != 'Linear'
                or row.get('extension') != 'EXTEND' or not _sha(row.get('reader_signature'))):
            raise ValueError('Invalid face-SDF target identity or sampling contract')
        names = [row.get(key) for key in ('left_attribute', 'right_attribute', 'weight_attribute')]
        if any(not _name(name) for name in names) or len(set(names)) != 3:
            raise ValueError('Face-SDF source names must be explicit and distinct')
        fields = row.get('attributes')
        roles = _field_roles(_face_profile(row['profile']))
        if (type(fields) is not dict or set(fields) != set(roles)
                or any(not _name(name) for name in fields.values()) or len(set(fields.values())) != len(roles)
                or any(name in fields.values() for name in names)):
            raise ValueError('Face-SDF source/owned carrier role alias')
        _image(row.get('image'))
        if row['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE) and (row['channel'] != 'R' or row['image']['channels'] not in (3, 4)):
            raise ValueError('Polar face-SDF shader requires RGB LRUD data with horizontal R')
        if row['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            encoding = _encoding(row['profile'])
            if row.get('direction_encoding') != encoding:
                raise ValueError('Polar face-SDF receipt direction encoding differs')
        elif any(key in row for key in ('direction_encoding','vertical_source','source_vertical_uv',
                                       'vertical_before','vertical_copied','vertical_frozen')):
            raise ValueError('Legacy face-SDF receipt must not carry polar independent-source fields')
        if row['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            if type(row.get('direction_correction')) is not bool:
                raise ValueError('Directional face-SDF correction mode must be an explicit bool')
        elif any(key in row for key in (
                'direction_source', 'source_direction_uv', 'direction_before',
                'direction_copied', 'direction_frozen', 'direction_correction')):
            raise ValueError('Legacy face-SDF receipt must not carry directional third-source fields')
        if row['image']['channel'] != row['channel']:
            raise ValueError('Face-SDF image selected channel differs from shader')
        _sampling(row)
        for key in ('source_uv', 'before', 'copied', 'frozen'):
            _uv(row.get(key), row['uv_name'])
        if not row['before'] == row['copied'] == row['frozen']:
            raise ValueError('Face-SDF RENDER loop UV changed during freezing')
        if row['profile'] in (POLAR_PROFILE, DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            vertical=_vertical_source(row.get('vertical_source'),row)
            name=vertical['uv']['uv_name']
            _vertical_uv(row.get('source_vertical_uv'),name,row['source_uv'])
            for key in ('vertical_before','vertical_copied','vertical_frozen'):
                _vertical_uv(row.get(key),name,row['before'])
            if not row['vertical_before'] == row['vertical_copied'] == row['vertical_frozen']:
                raise ValueError('Polar face-SDF independent RENDER UV changed during freezing')
        if row['profile'] in (DIRECTION_PROFILE, CONTINUITY_PROFILE, AUTHORED_PROFILE):
            direction = _direction_source(row.get('direction_source'), row)
            name = direction['uv']['uv_name']
            _vertical_uv(row.get('source_direction_uv'), name, row['source_uv'])
            for key in ('direction_before', 'direction_copied', 'direction_frozen'):
                _vertical_uv(row.get(key), name, row['before'])
            if not row['direction_before'] == row['direction_copied'] == row['direction_frozen']:
                raise ValueError('Directional face-SDF third-source RENDER UV changed during freezing')
        if row['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE):
            _continuity_identity(row.get('continuity_keys'))
            if 'pair_source' not in row:
                raise ValueError('Continuity receipt must explicitly contain pair_source')
            pair = _pair_source(row['pair_source'],row)
            if pair is not None:
                name = pair['uv']['uv_name']
                _vertical_uv(row.get('source_pair_uv'),name,row['source_uv'])
                for key in ('pair_before','pair_copied','pair_frozen'):
                    _vertical_uv(row.get(key),name,row['before'])
                if not row['pair_before'] == row['pair_copied'] == row['pair_frozen']:
                    raise ValueError('Continuity pair RENDER UV changed during freezing')
            elif any(key in row for key in ('source_pair_uv','pair_before','pair_copied','pair_frozen')):
                raise ValueError('Disabled continuity must not carry unused pair UV witnesses')
        elif any(key in row for key in ('continuity_keys','pair_source','source_pair_uv','pair_before','pair_copied','pair_frozen')):
            raise ValueError('Legacy SDF receipt must not carry continuity metadata')
        _carrier(row.get('source_carrier'), row, row['source_uv']['vertices'])
        _carrier(row.get('render_carrier'), row, row['before']['vertices'])
        face_owner = _descriptor(row.get('descriptor_before'), row)
        if _descriptor(row.get('descriptor_frozen'), row) != face_owner:
            raise ValueError('Face-SDF descriptor face carrier owner changed')
        if row['descriptor_before'] != row['descriptor_frozen']:
            raise ValueError('Face-SDF shader or image descriptor changed during freezing')
        if ('binding_before' in row or 'binding_frozen' in row) and row.get('binding_before') != row.get('binding_frozen'):
            raise ValueError('Face-SDF material binding changed during freezing')
        if face_rows is not None:
            carrier = face_rows.get(face_owner)
            if carrier is None or carrier.get('profile') != _face_profile(row['profile']) or any(row[key] != carrier.get(key) for key in
                    ('target', 'modifier_uid', 'attributes', 'left_attribute',
                     'right_attribute', 'weight_attribute')):
                raise ValueError('Face-SDF receipt differs from actual face-v1 carrier')
            if row['profile'] in (CONTINUITY_PROFILE, AUTHORED_PROFILE) and carrier.get('continuity_keys') != row['continuity_keys']:
                raise ValueError('Continuity reader and frozen carrier layout identities differ')
            if carrier['before']['vertices'] != row['before']['vertices']:
                raise ValueError('Face-SDF UV and face-v1 carrier RENDER extent differ')
        if multi:
            binding = _material_binding(row)
            material = binding['material']
            reader = row['descriptor_before']['shader']['name']
            slots = {(target, i) for i in binding['slots']}
            if material in materials or reader in readers or occupied.intersection(slots):
                raise ValueError('Repeated face-SDF material/reader or overlapping target slots')
            # The new scope is several materials on one existing face carrier,
            # using the same UV. A new carrier/UV system is a different feature.
            carrier_identity = dict(face_owner=face_owner,
                **{key: row[key] for key in ('modifier_uid', 'attributes', 'left_attribute',
                    'right_attribute', 'weight_attribute', 'uv_name', 'source_uv',
                    'before', 'copied', 'frozen', 'source_carrier', 'render_carrier')})
            if target in carrier_rows and carrier_rows[target] != carrier_identity:
                raise ValueError('Materials on one face-SDF target disagree on carrier/UV identity')
            carrier_rows[target] = carrier_identity
            materials.add(material); readers.add(reader); occupied.update(slots)
        targets.add(target)
        owners.add(owner)
    return len(owners)


def _normal_snapshot(value, attribute):
    if (type(value) is not dict or not _integer(value.get('vertices'))
            or not _integer(value.get('corners')) or type(value.get('custom_normals')) is not bool
            or not _sha(value.get('corner_normals_sha256'))
            or type(value.get('fields')) is not dict or set(value['fields']) != {attribute}):
        raise ValueError('Invalid face-SDF cast normal-data snapshot')
    field = value['fields'][attribute]
    if (type(field) is not dict or field.get('domain') != 'POINT'
            or field.get('data_type') != 'FLOAT_VECTOR'
            or type(field.get('count')) is not int or field['count'] != value['vertices']
            or not _sha(field.get('sha256'))):
        raise ValueError('Face-SDF cast field type/domain/extent differs')


def _reader_signatures(value, expected_names):
    if type(value) is not list or len(value) != len(expected_names):
        raise ValueError('Missing face-SDF cast shader reader signatures')
    actual = set()
    for row in value:
        if (type(row) is not dict or not _name(row.get('name')) or row['name'] in actual
                or not _sha(row.get('sha256'))):
            raise ValueError('Invalid face-SDF cast shader reader signature')
        actual.add(row['name'])
    if actual != expected_names:
        raise ValueError('Face-SDF cast shader signature reader inventory differs')


def validate_cast_receipt(feature, face_feature=None):
    """Bind a frozen single-Sun sensor field to its actual face-v1 carrier.

    Direction finiteness/unit/current-Sun agreement belongs to live validation.
    Hash equality here proves preservation only; frozen meshes need no original
    modifiers and no live reader resolution is attempted.
    """
    if (type(feature) is not dict or feature.get('schema') not in (CAST_SCHEMA, CAST_SCHEMA_V2, CAST_SCHEMA_V3, CAST_SCHEMA_V4)
            or type(feature.get('targets')) is not list or len(feature['targets']) > 256):
        raise ValueError('Unsupported face-SDF cast freeze witness')
    if feature['schema'] != cast_schema_for_targets(feature['targets']):
        raise ValueError('Face-SDF cast freeze schema differs from its nested face profiles')
    faces = None
    if face_feature is not None:
        from .face_witness import validate_receipt as validate_face
        validate_face(face_feature)
        faces = {row['owner']: row for row in face_feature['targets']}
    targets, owners = set(), set()
    for row in feature['targets']:
        if type(row) is not dict:
            raise ValueError('Invalid face-SDF cast target row')
        owner, target = row.get('owner'), row.get('target')
        if (row.get('profile') != CAST_PROFILE or row.get('status') != 'valid'
                or row.get('data_exact') is not True or not _name(target) or target in targets
                or not isinstance(owner, str) or re.fullmatch('[0-9a-f]{32}', owner) is None
                or owner in owners or type(row.get('modifier_uid')) is not int
                or row.get('normal_attribute') != 'toon_sdf_sun_'+owner or not _name(row.get('sun'))):
            raise ValueError('Invalid face-SDF cast identity')
        face = row.get('face')
        if (type(face) is not dict or face.get('profile') not in (FACE_PROFILE, FACE_PROFILE_V2, FACE_PROFILE_V3, FACE_PROFILE_V4)
                or not isinstance(face.get('owner'), str)
                or re.fullmatch('[0-9a-f]{32}', face['owner']) is None
                or type(face.get('modifier_uid')) is not int or not _sha(face.get('graph_signature'))):
            raise ValueError('Invalid face-SDF cast face-source identity')
        if face['profile'] in (FACE_PROFILE_V3, FACE_PROFILE_V4):
            from . import face_direction_keys
            if face.get('direction_keys') != face_direction_keys.identity():
                raise ValueError('Cast direction-key source document identity differs')
        elif 'direction_keys' in face:
            raise ValueError('Legacy cast face source must not carry direction-key identity')
        if face['profile'] == FACE_PROFILE_V4:
            _continuity_identity(face.get('continuity_keys'))
        elif 'continuity_keys' in face:
            raise ValueError('Legacy cast face source must not carry continuity identity')
        fields = {role: 'toon_face_'+role+'_'+face['owner'] for role in _field_roles(face['profile'])}
        if (face.get('fields') != fields or face.get('mix_attribute') != fields['mix']
                or face.get('valid_attribute') != fields['valid']
                or row['normal_attribute'] in fields.values()):
            raise ValueError('Face-SDF cast mix/valid source role binding differs')
        carrier = row.get('carrier')
        source_ids = row.get('source_identities')
        if (type(carrier) is not dict or not _sha(carrier.get('graph_signature'))
                or carrier.get('position') != 'immediately_before_face'
                or type(source_ids) is not dict
                or source_ids.get('face_graph_signature') != face['graph_signature']
                or source_ids.get('cast_graph_signature') != carrier['graph_signature']):
            raise ValueError('Face-SDF cast carrier source signature differs')
        readers = row.get('readers')
        if type(readers) is not list or len(readers) > 256:
            raise ValueError('Invalid face-SDF cast reader inventory')
        reader_names = set()
        for reader in readers:
            if (type(reader) is not dict or not _name(reader.get('name')) or reader['name'] in reader_names
                    or not _integer(reader.get('users')) or reader.get('author_edited') is not False):
                raise ValueError('Face-SDF cast reader is edited or has invalid identity')
            reader_names.add(reader['name'])
        _reader_signatures(row.get('shader_before'), reader_names)
        _reader_signatures(row.get('shader_frozen'), reader_names)
        if row['shader_before'] != row['shader_frozen']:
            raise ValueError('Face-SDF cast shader graph changed during freezing')
        if ('binding_before' in row or 'binding_frozen' in row) and row.get('binding_before') != row.get('binding_frozen'):
            raise ValueError('Face-SDF cast material binding changed during freezing')
        for key in ('before', 'copied', 'frozen'):
            _normal_snapshot(row.get(key), row['normal_attribute'])
        if not row['before'] == row['copied'] == row['frozen']:
            raise ValueError('Face-SDF cast field/corner normals changed during freezing')
        if faces is not None:
            actual = faces.get(face['owner'])
            if (actual is None or actual.get('profile') != face['profile'] or actual['target'] != target
                    or actual['modifier_uid'] != face['modifier_uid']
                    or actual['attributes'] != face['fields'] or actual['light'] != row['sun']
                    or actual['before']['vertices'] != row['before']['vertices']
                    or actual['before']['corners'] != row['before']['corners']
                    or actual['before']['corner_normals_sha256'] != row['before']['corner_normals_sha256']):
                raise ValueError('Face-SDF cast differs from actual face-v1 freeze witness')
            if face['profile'] == FACE_PROFILE_V4 and actual.get('continuity_keys') != face['continuity_keys']:
                raise ValueError('Cast continuity source differs from actual carrier layout')
        targets.add(target)
        owners.add(owner)
    return len(targets)
