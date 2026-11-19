# SPDX-License-Identifier: GPL-3.0-or-later
"""Cold integrity checks for the optional directional face-threshold fields.

Only the explicitly profiled four/six/eight/nine shading outputs are certified. This is not a material,
cast-shadow-isolation, complete SDF, animation or artistic-quality certificate.
"""
import math
import re
from .normal_witness import snapshot

SCHEMA = 'toon-face-threshold-freeze.v1'
SCHEMA_V2 = 'toon-face-threshold-freeze.v2'
SCHEMA_V3 = 'toon-face-threshold-freeze.v3'
SCHEMA_V4 = 'toon-face-threshold-freeze.v4'
PROFILE = 'toon-face-threshold.v1'
PROFILE_V2 = 'toon-face-threshold.v2'
PROFILE_V3 = DIRECTION_PROFILE = 'toon-face-threshold.v3'
PROFILE_V4 = CONTINUITY_PROFILE = 'toon-face-threshold.v4'
SPACE = 'HEAD_DIRECTION_AZIMUTH'
SPACE_V2 = 'HEAD_DIRECTION_POLAR_LRUD'
SPACE_DIRECTION = 'HEAD_DIRECTION_POLAR_KEYS_14'
SPACE_CONTINUITY = 'HEAD_DIRECTION_POLAR_KEYS_14_PAIR'
KINDS = {'threshold':'FLOAT','phase':'FLOAT','mix':'FLOAT','valid':'BOOLEAN'}
KINDS_V2 = dict(KINDS, vertical_side='FLOAT', vertical_weight='FLOAT')
KINDS_V3 = dict(KINDS_V2, direction_upper='FLOAT', direction_top='FLOAT')
KINDS_V4 = dict(KINDS_V3, direction_pair='FLOAT_VECTOR')


def schema_for_targets(rows):
    """Keep legacy/empty output v1; require the v2 identity for any polar row."""
    profiles = [row.get('profile') if type(row) is dict else None for row in rows]
    if any(profile not in (PROFILE, PROFILE_V2, PROFILE_V3, PROFILE_V4) for profile in profiles):
        raise ValueError('Unsupported face-threshold target profile')
    if PROFILE_V4 in profiles:return SCHEMA_V4
    if PROFILE_V3 in profiles:return SCHEMA_V3
    return SCHEMA_V2 if PROFILE_V2 in profiles else SCHEMA


def validate_receipt(feature):
    if (type(feature) is not dict or feature.get('schema') not in (SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4)
            or type(feature.get('targets')) is not list or len(feature['targets'])>256):
        raise ValueError('Unsupported face-threshold freeze witness')
    if feature['schema'] != schema_for_targets(feature['targets']):
        raise ValueError('Face-threshold freeze schema differs from its target profiles')
    targets=set();owners=set()
    for row in feature['targets']:
        profile = row.get('profile')
        kinds = KINDS_V4 if profile == PROFILE_V4 else KINDS_V3 if profile == PROFILE_V3 else KINDS_V2 if profile == PROFILE_V2 else KINDS
        space = SPACE_CONTINUITY if profile == PROFILE_V4 else SPACE_DIRECTION if profile == PROFILE_V3 else SPACE_V2 if profile == PROFILE_V2 else SPACE
        owner=row.get('owner');target=row.get('target')
        if (profile not in (PROFILE, PROFILE_V2, PROFILE_V3, PROFILE_V4) or row.get('space')!=space
                or row.get('status')!='valid' or row.get('data_exact') is not True
                or not isinstance(target,str) or not target or target in targets
                or not isinstance(owner,str) or not re.fullmatch('[0-9a-f]{32}',owner) or owner in owners
                or type(row.get('modifier_uid')) is not int
                or any(type(row.get(key)) not in (int,float) or not math.isfinite(row[key])
                    or not 0<=row[key]<=1 for key in ('strength','side_blend','vertical_fade'))):
            raise ValueError('Invalid face-threshold target identity or state')
        if profile in (PROFILE_V2, PROFILE_V3, PROFILE_V4) and row['vertical_fade'] != 0.:
            raise ValueError('Polar face-threshold v2 must not apply legacy vertical fade')
        if profile in (PROFILE_V3, PROFILE_V4):
            from . import face_direction_keys
            if row.get('direction_keys') != face_direction_keys.identity():
                raise ValueError('Directional face-threshold key document identity differs')
        elif 'direction_keys' in row:
            raise ValueError('Legacy face-threshold must not carry direction-key identity')
        if profile == PROFILE_V4:
            if row.get('continuity_keys') != face_direction_keys.continuity_identity():
                raise ValueError('Continuity face-threshold packed-key identity differs')
        elif 'continuity_keys' in row:
            raise ValueError('Legacy face-threshold must not carry continuity-key identity')
        for key in ('reference','light','left_attribute','right_attribute','weight_attribute'):
            if not isinstance(row.get(key),str) or not row[key]:
                raise ValueError('Missing face-threshold source binding: '+key)
        fields={key:'toon_face_'+key+'_'+owner for key in kinds}
        if row.get('attributes')!=fields:
            raise ValueError('Face-threshold owner/field role binding differs')
        if any(row[key] in fields.values() for key in ('left_attribute','right_attribute','weight_attribute')):
            raise ValueError('Face-threshold source aliases its owned output')
        before=row.get('before')
        if (type(before) is not dict or before!=row.get('copied') or before!=row.get('frozen')
                or type(before.get('vertices')) is not int or before['vertices']<0
                or type(before.get('corners')) is not int or before['corners']<0
                or type(before.get('custom_normals')) is not bool
                or not re.fullmatch('[0-9a-f]{64}',str(before.get('corner_normals_sha256')))
                or type(before.get('fields')) is not dict or set(before['fields'])!=set(fields.values())):
            raise ValueError('Face-threshold data changed during frame freezing')
        for role,name in fields.items():
            data=before['fields'][name]
            if (data.get('domain')!='POINT' or data.get('data_type')!=kinds[role]
                    or type(data.get('count')) is not int or data['count']!=before['vertices']
                    or not re.fullmatch('[0-9a-f]{64}',str(data.get('sha256')))):
                raise ValueError('Face-threshold field role/type/extent differs')
        targets.add(target);owners.add(owner)
    return len(targets)
