"""Strict data-only contract for the first executable Toon slice.

This module never reads a blend, initializes an identity, imports bpy or claims
that normalized requests are runtime-qualified. The unregistered P2 draft has
no implicit migration into this contract.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import uuid

REGION_VERSION = '0.7.0'
ANIMATION_VERSION = '0.8.0'
JITTER_VERSION = '0.9.0'
VERSION = JITTER_VERSION
ANIMATION_VERSIONS = (ANIMATION_VERSION, JITTER_VERSION)
REGION_VERSIONS = (REGION_VERSION, *ANIMATION_VERSIONS)
REFLECT_VERSION = '0.6.0'
LEGACY_VERSION = '0.1.0'
GROUPED_VERSION = '0.2.0'
STYLED_VERSION = '0.3.0'
PLACED_VERSION = '0.4.0'
CEL3_VERSION = '0.5.0'
PLACED_VERSIONS = (PLACED_VERSION, CEL3_VERSION, REFLECT_VERSION, *REGION_VERSIONS)
STYLED_VERSIONS = (STYLED_VERSION, *PLACED_VERSIONS)
GROUPED_VERSIONS = (GROUPED_VERSION, *STYLED_VERSIONS)
MANIFEST_VERSIONS = (LEGACY_VERSION, *GROUPED_VERSIONS)
STATE_SOURCE = 'SAVED_DISK_V1'


def is_grouped(manifest):
    return manifest is not None and manifest.get('schema_version') in GROUPED_VERSIONS


def is_placed(manifest):
    return manifest is not None and manifest.get('schema_version') in PLACED_VERSIONS


def is_animated(manifest):
    return manifest is not None and manifest.get('schema_version') in ANIMATION_VERSIONS


def allows_styled_jitter(manifest):
    return manifest is not None and manifest.get('schema_version') == JITTER_VERSION


class WorkflowError(Exception):
    def __init__(self, code, message, path='$'):
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def _object(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def _number(minimum=None, maximum=None, exclusive_minimum=None):
    result = {'type': 'number'}
    if minimum is not None: result['minimum'] = minimum
    if maximum is not None: result['maximum'] = maximum
    if exclusive_minimum is not None: result['exclusiveMinimum'] = exclusive_minimum
    return result


NAME_SCHEMA = {'type': 'string', 'minLength': 1}
UUID_SCHEMA = {'type': 'string', 'format': 'uuid'}
SHA_SCHEMA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
PATH_SCHEMA = {'type': 'string', 'minLength': 1, 'format': 'absolute-path'}
FILE_SCHEMA = _object({'file': PATH_SCHEMA, 'expected_sha256': SHA_SCHEMA})
COLOR_SCHEMA = {'type': 'array', 'items': _number(0, 1), 'minItems': 4, 'maxItems': 4}
TARGET_SCHEMA = _object({'object_name': NAME_SCHEMA, 'object_id': UUID_SCHEMA,
                         'mesh_id': UUID_SCHEMA, 'initialize_identity': {'type': 'boolean'}})
LEGACY_LINES_SCHEMA = _object({
    'enabled': {'type': 'boolean'},
    'kinds': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
              'items': {'enum': ['silhouette', 'crease', 'material_boundary', 'marked', 'boundary']}},
    'crease_angle_degrees': _number(0, 180),
    'backend': {'enum': ['screen_ribbon', 'tube_world']},
    'width_mode': {'enum': ['pixel', 'frame_fraction', 'world']},
    'width': _number(exclusive_minimum=0), 'color': COLOR_SCHEMA,
    'opacity': _number(0, 1), 'depth_bias': _number(0),
    'cadence_frames': {'type': 'integer', 'enum': [1, 2, 3]},
    'jitter_px': _number(0), 'seed': {'type': 'integer'},
})
LINE_STYLE_SCHEMA_V2 = _object({
    'width_mode': {'enum': ['pixel', 'frame_fraction']},
    'width': _number(exclusive_minimum=0), 'color': COLOR_SCHEMA,
    'jitter_px': _number(0), 'cadence_frames': {'type': 'integer', 'enum': [1, 2, 3]},
    'seed': {'type': 'integer'},
}, [])
LINE_GROUP_SCHEMA_V2 = _object({
    'group_id': UUID_SCHEMA,
    'object_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': UUID_SCHEMA},
    'kinds': deepcopy(LEGACY_LINES_SCHEMA['properties']['kinds']),
    'enabled': {'type': 'boolean'}, 'style': LINE_STYLE_SCHEMA_V2,
})
LINES_SCHEMA_V2 = _object({
    **deepcopy(LEGACY_LINES_SCHEMA['properties']),
    'backend': {'enum': ['screen_ribbon']},
    'width_mode': {'enum': ['pixel', 'frame_fraction']},
    'opacity': _number(1, 1),
    'groups': {'type': 'array', 'minItems': 0, 'maxItems': 64, 'items': LINE_GROUP_SCHEMA_V2},
    'kind_order': deepcopy(LEGACY_LINES_SCHEMA['properties']['kinds']),
})
WIDTH_PROFILE_SCHEMA = {'type': 'array', 'minItems': 2, 'maxItems': 32,
    'items': _object({'u': _number(0, 1), 'scale': _number(0, 4)})}
STROKE_SCHEMA = _object({
    'dash': _object({'enabled': {'type': 'boolean'}, 'period_u': _number(1 / 128, 1),
                     'duty': _number(0, 1), 'phase_u': _number(-1, 1)}),
    'taper': _object({'start_u': _number(0, .5), 'end_u': _number(0, .5)}),
    'cap': {'enum': ['butt', 'square', 'round']},
    'width_profile': WIDTH_PROFILE_SCHEMA,
    'depth_width_fade': _object({'enabled': {'type': 'boolean'},
                                'start_depth': _number(0), 'end_depth': _number(0)}),
})
EDGE_OVERRIDE_SCHEMA = _object({
    'object_id': UUID_SCHEMA, 'mesh_id': UUID_SCHEMA,
    'edge_id': {'type': 'integer', 'minimum': 1, 'maximum': 2147483647},
    'point_ids': {'type': 'array', 'minItems': 2, 'maxItems': 2,
                  'items': {'type': 'integer', 'minimum': 1, 'maximum': 2147483647}},
    'enabled': {'type': 'boolean'}, 'width_profile': WIDTH_PROFILE_SCHEMA,
    'visible_intervals': {'type': 'array', 'minItems': 0, 'maxItems': 64,
                          'items': _object({'start': _number(0, 1), 'end': _number(0, 1)})},
})
LINE_STYLE_SCHEMA = deepcopy(LINE_STYLE_SCHEMA_V2)
LINE_STYLE_SCHEMA['properties'].update(stroke=STROKE_SCHEMA, jitter_px=_number(0, 0))
LINE_GROUP_SCHEMA = deepcopy(LINE_GROUP_SCHEMA_V2)
LINE_GROUP_SCHEMA['properties']['style'] = LINE_STYLE_SCHEMA
LINES_SCHEMA = deepcopy(LINES_SCHEMA_V2)
LINES_SCHEMA['properties'].update(stroke=STROKE_SCHEMA,
    edge_overrides={'type': 'array', 'minItems': 0, 'maxItems': 256, 'items': EDGE_OVERRIDE_SCHEMA},
    jitter_px=_number(0, 0),
    groups={'type': 'array', 'minItems': 0, 'maxItems': 64, 'items': LINE_GROUP_SCHEMA})
LINES_SCHEMA['required'] += ['stroke', 'edge_overrides']
PLACEMENT_SCHEMA = _object({'mode': {'enum': ['native_depth', 'camera_plane']},
                            'camera_depth': _number(exclusive_minimum=0)}, ['mode'])
LINE_STYLE_SCHEMA_V4 = deepcopy(LINE_STYLE_SCHEMA)
LINE_STYLE_SCHEMA_V4['properties']['placement'] = PLACEMENT_SCHEMA
LINE_GROUP_SCHEMA_V4 = deepcopy(LINE_GROUP_SCHEMA)
LINE_GROUP_SCHEMA_V4['properties']['style'] = LINE_STYLE_SCHEMA_V4
LINES_SCHEMA_V4 = deepcopy(LINES_SCHEMA)
LINES_SCHEMA_V4['properties'].update(placement=PLACEMENT_SCHEMA,
    groups={'type': 'array', 'minItems': 0, 'maxItems': 64, 'items': LINE_GROUP_SCHEMA_V4})
LINES_SCHEMA_V4['required'].append('placement')
LINE_STYLE_SCHEMA_V9 = deepcopy(LINE_STYLE_SCHEMA_V4)
LINE_STYLE_SCHEMA_V9['properties']['jitter_px'] = _number(0, 64)
LINE_GROUP_SCHEMA_V9 = deepcopy(LINE_GROUP_SCHEMA_V4)
LINE_GROUP_SCHEMA_V9['properties']['style'] = LINE_STYLE_SCHEMA_V9
LINES_SCHEMA_V9 = deepcopy(LINES_SCHEMA_V4)
LINES_SCHEMA_V9['properties'].update(jitter_px=_number(0, 64),
    groups={'type': 'array', 'minItems': 0, 'maxItems': 64, 'items': LINE_GROUP_SCHEMA_V9})
SHADING_SCHEMA = _object({
    'mode': {'enum': ['preserve', 'native_cel']},
    'base_color': COLOR_SCHEMA, 'shadow_color': COLOR_SCHEMA, 'lit_color': COLOR_SCHEMA,
    'threshold': _number(0, 1), 'toon_size': _number(0, 1), 'toon_smooth': _number(0, 1),
})
EEVEE_CEL3_SHADING_SCHEMA = _object({
    'mode': {'const': 'eevee_cel3'},
    'base_color': COLOR_SCHEMA, 'shadow_color': COLOR_SCHEMA,
    'mid_color': COLOR_SCHEMA, 'lit_color': COLOR_SCHEMA,
    'threshold_low': _number(0, 1), 'threshold_high': _number(0, 1),
})
SHADING_SCHEMA_V5 = {'oneOf': [deepcopy(SHADING_SCHEMA), EEVEE_CEL3_SHADING_SCHEMA]}
EEVEE_CEL3_REFLECT_SHADING_SCHEMA = _object({
    'mode': {'const': 'eevee_cel3_reflect'},
    'base_color': COLOR_SCHEMA, 'shadow_color': COLOR_SCHEMA,
    'mid_color': COLOR_SCHEMA, 'lit_color': COLOR_SCHEMA,
    'threshold_low': _number(0, 1), 'threshold_high': _number(0, 1),
    'reflection_color': COLOR_SCHEMA,
    'reflection_strength': _number(0, 1), 'reflection_roughness': _number(0, 1),
})
SHADING_SCHEMA_V6 = {'oneOf': [deepcopy(SHADING_SCHEMA), deepcopy(EEVEE_CEL3_SHADING_SCHEMA),
                             EEVEE_CEL3_REFLECT_SHADING_SCHEMA]}
REGIONS_SHADING_SCHEMA = _object({
    'mode': {'const': 'eevee_regions'}, 'unassigned': {'const': 'preserve'},
    'templates': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': _object({
        'template_id': UUID_SCHEMA,
        'settings': {'oneOf': [deepcopy(EEVEE_CEL3_SHADING_SCHEMA), deepcopy(EEVEE_CEL3_REFLECT_SHADING_SCHEMA)]}})},
    'assignments': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': _object({
        'region_id': UUID_SCHEMA, 'object_id': UUID_SCHEMA,
        'slot_index': {'type': 'integer', 'minimum': 0}, 'template_id': UUID_SCHEMA})},
})
TIME_SCHEMA = _object({'start': {'type': 'integer'}, 'end': {'type': 'integer'},
                       'fps': {'type': 'integer', 'const': 24}})
RENDER_SCHEMA = _object({
    'enabled': {'type': 'boolean'},
    'resolution': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 2048},
                   'minItems': 2, 'maxItems': 2},
    'samples': {'type': 'integer', 'minimum': 1},
    'device': {'enum': ['CPU', 'GPU']}, 'camera': NAME_SCHEMA,
    'transparent': {'type': 'boolean'},
})
LEGACY_MANIFEST_SCHEMA = _object({
    'schema_version': {'const': LEGACY_VERSION}, 'scope_id': UUID_SCHEMA,
    'mode': {'enum': ['create', 'update']}, 'scene': NAME_SCHEMA,
    'engine': {'enum': ['BLENDER_EEVEE', 'CYCLES']},
    'targets': {'type': 'array', 'minItems': 1, 'items': TARGET_SCHEMA},
    'lines': LEGACY_LINES_SCHEMA, 'shading': SHADING_SCHEMA,
    'time': TIME_SCHEMA, 'render': RENDER_SCHEMA,
})
GROUPED_MANIFEST_SCHEMA = deepcopy(LEGACY_MANIFEST_SCHEMA)
GROUPED_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': GROUPED_VERSION}
GROUPED_MANIFEST_SCHEMA['properties']['lines'] = LINES_SCHEMA_V2
STYLED_MANIFEST_SCHEMA = deepcopy(LEGACY_MANIFEST_SCHEMA)
STYLED_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': STYLED_VERSION}
STYLED_MANIFEST_SCHEMA['properties']['lines'] = LINES_SCHEMA
PLACED_MANIFEST_SCHEMA = deepcopy(LEGACY_MANIFEST_SCHEMA)
PLACED_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': PLACED_VERSION}
PLACED_MANIFEST_SCHEMA['properties']['lines'] = LINES_SCHEMA_V4
CEL3_MANIFEST_SCHEMA = deepcopy(PLACED_MANIFEST_SCHEMA)
CEL3_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': CEL3_VERSION}
CEL3_MANIFEST_SCHEMA['properties']['shading'] = SHADING_SCHEMA_V5
REFLECT_MANIFEST_SCHEMA = deepcopy(CEL3_MANIFEST_SCHEMA)
REFLECT_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': REFLECT_VERSION}
REFLECT_MANIFEST_SCHEMA['properties']['shading'] = SHADING_SCHEMA_V6
REGION_MANIFEST_SCHEMA = deepcopy(REFLECT_MANIFEST_SCHEMA)
REGION_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': REGION_VERSION}
REGION_MANIFEST_SCHEMA['properties']['engine'] = {'const': 'BLENDER_EEVEE'}
REGION_MANIFEST_SCHEMA['properties']['shading'] = REGIONS_SHADING_SCHEMA
# The pure animation normalizer checks the anyOf value and continuous interval
# semantics after this closed structural traversal; external JSON Schema also
# validates the scalar/color union directly.
ANIMATION_VALUE_SCHEMA = {'anyOf': [_number(0, 1), deepcopy(COLOR_SCHEMA)]}
ANIMATION_TRACK_SCHEMA = _object({'template_id': UUID_SCHEMA,
    'field': {'enum': ['base_color', 'shadow_color', 'mid_color', 'lit_color',
                      'threshold_low', 'threshold_high', 'reflection_color',
                      'reflection_strength', 'reflection_roughness']},
    'interpolation': {'enum': ['LINEAR', 'CONSTANT']},
    'keys': {'type': 'array', 'minItems': 2, 'maxItems': 32,
             'items': _object({'frame': {'type': 'integer'}, 'value': ANIMATION_VALUE_SCHEMA})}})
ANIMATIONS_SCHEMA = {'type': 'array', 'minItems': 0, 'maxItems': 64, 'items': ANIMATION_TRACK_SCHEMA}
CURRENT_MANIFEST_SCHEMA = deepcopy(REGION_MANIFEST_SCHEMA)
CURRENT_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': ANIMATION_VERSION}
CURRENT_MANIFEST_SCHEMA['properties']['animations'] = ANIMATIONS_SCHEMA
CURRENT_MANIFEST_SCHEMA['required'].append('animations')
ANIMATION_MANIFEST_SCHEMA = CURRENT_MANIFEST_SCHEMA
CURRENT_MANIFEST_SCHEMA = deepcopy(ANIMATION_MANIFEST_SCHEMA)
CURRENT_MANIFEST_SCHEMA['properties']['schema_version'] = {'const': JITTER_VERSION}
CURRENT_MANIFEST_SCHEMA['properties']['lines'] = LINES_SCHEMA_V9
MANIFEST_SCHEMAS = {LEGACY_VERSION: LEGACY_MANIFEST_SCHEMA,
                    GROUPED_VERSION: GROUPED_MANIFEST_SCHEMA, STYLED_VERSION: STYLED_MANIFEST_SCHEMA,
                    PLACED_VERSION: PLACED_MANIFEST_SCHEMA, CEL3_VERSION: CEL3_MANIFEST_SCHEMA,
                    REFLECT_VERSION: REFLECT_MANIFEST_SCHEMA, REGION_VERSION: REGION_MANIFEST_SCHEMA,
                    ANIMATION_VERSION: ANIMATION_MANIFEST_SCHEMA, JITTER_VERSION: CURRENT_MANIFEST_SCHEMA}
MANIFEST_SCHEMA = {'oneOf': list(MANIFEST_SCHEMAS.values())}
_SOURCE_PARAMS = {'file': PATH_SCHEMA, 'expected_sha256': SHA_SCHEMA,
                  'resources': {'type': 'array', 'items': FILE_SCHEMA},
                  'implementation_sha256': SHA_SCHEMA,
                  'state_source': {'const': STATE_SOURCE}}
RUN_PARAMS_SCHEMA = _object({**_SOURCE_PARAMS, 'manifest': MANIFEST_SCHEMA, 'baseline': FILE_SCHEMA},
                            [*_SOURCE_PARAMS, 'manifest'])
INSPECT_PARAMS_SCHEMA = _object({**_SOURCE_PARAMS, 'scene': NAME_SCHEMA,
    'object_names': {'type': 'array', 'items': NAME_SCHEMA, 'uniqueItems': True}}, list(_SOURCE_PARAMS))


def _fail(code, message, path):
    raise WorkflowError(code, message, path)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowError('INVALID_REQUEST', 'Canonical JSON required: ' + str(exc)) from exc


def signature(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def validate(value, schema, path='$'):
    """Validate the deliberately closed subset used by registered schemas."""
    if 'oneOf' in schema:
        # These are explicitly discriminated unions: manifest schema_version
        # and shading mode. Select before traversal to retain strict field
        # rejection and legacy error codes/paths, rather than trying branches.
        branches = schema['oneOf']
        if all('schema_version' in branch.get('properties', {}) for branch in branches):
            discriminator = 'schema_version'
        elif all('mode' in branch.get('properties', {}) for branch in branches):
            discriminator = 'mode'
        else:
            _fail('INVALID_REQUEST', 'Unsupported contract union', path)
        for branch in branches:
            tag_schema = branch['properties'][discriminator]
            tags = [tag_schema['const']] if 'const' in tag_schema else tag_schema['enum']
            if (isinstance(value, dict) and type(value.get(discriminator)) is str
                    and value[discriminator] in tags):
                return validate(value, branch, path)
        if discriminator == 'mode' and isinstance(value, dict):
            if 'mode' not in value:
                _fail('INVALID_REQUEST', 'Missing fields: mode', path)
            _fail('INVALID_REQUEST', 'Unsupported shading mode', path + '.mode')
        return validate(value, branches[0], path)
    kind = schema.get('type')
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str), 'boolean': lambda: type(value) is bool,
             'integer': lambda: type(value) is int,
             'number': lambda: type(value) is int or (type(value) is float and math.isfinite(value))}
    if kind and not valid[kind](): _fail('INVALID_REQUEST', 'Expected ' + kind, path)
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        _fail('INVALID_REQUEST', 'Expected constant ' + repr(schema['const']), path)
    if 'enum' in schema and not any(type(value) is type(v) and value == v for v in schema['enum']):
        _fail('INVALID_REQUEST', 'Unsupported value', path)
    if kind == 'object':
        extras = set(value) - set(schema['properties'])
        if extras: _fail('UNSUPPORTED', 'Unsupported fields: ' + ', '.join(sorted(map(str, extras))), path)
        missing = set(schema['required']) - set(value)
        if missing: _fail('INVALID_REQUEST', 'Missing fields: ' + ', '.join(sorted(missing)), path)
        for key, item in value.items(): validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', float('inf')):
            _fail('INVALID_REQUEST', 'Array length out of range', path)
        if schema.get('uniqueItems') and len({canonical(v) for v in value}) != len(value):
            _fail('INVALID_REQUEST', 'Duplicate array items', path)
        for index, item in enumerate(value): validate(item, schema['items'], path + '[' + str(index) + ']')
    elif kind == 'string':
        if len(value) < schema.get('minLength', 0) or not value.strip() or '\x00' in value:
            _fail('INVALID_REQUEST', 'Nonempty string without NUL required', path)
        if schema.get('pattern'):
            import re
            if not re.fullmatch(schema['pattern'], value): _fail('INVALID_REQUEST', 'Invalid SHA256', path)
        if schema.get('format') == 'uuid':
            try: parsed = uuid.UUID(value)
            except (ValueError, AttributeError): _fail('INVALID_REQUEST', 'Explicit UUID required', path)
            if str(parsed) != value or parsed.int == 0:
                _fail('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
        if schema.get('format') == 'absolute-path' and not Path(value).is_absolute():
            _fail('INVALID_REQUEST', 'Absolute path required', path)
    elif kind in ('number', 'integer'):
        if type(value) is float and not math.isfinite(value): _fail('INVALID_REQUEST', 'Finite number required', path)
        if value < schema.get('minimum', -float('inf')) or value > schema.get('maximum', float('inf')):
            _fail('INVALID_REQUEST', 'Number out of range', path)
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            _fail('INVALID_REQUEST', 'Number must be greater than minimum', path)
    return value


def normalize_manifest(value):
    if isinstance(value, dict) and value.get('schema_version') == '0.1.0-draft.2':
        _fail('UNSUPPORTED', 'P2 draft.2 was never registered; no implicit migration', '$.manifest.schema_version')
    validate(value, MANIFEST_SCHEMA, '$.manifest')
    result = deepcopy(value)
    targets = result['targets']
    for key in ('object_name', 'object_id', 'mesh_id'):
        if len({t[key] for t in targets}) != len(targets):
            _fail('INVALID_REQUEST', 'Duplicate target ' + key, '$.manifest.targets')
    identities = [result['scope_id']] + [t[k] for t in targets for k in ('object_id', 'mesh_id')]
    if len(set(identities)) != len(identities):
        _fail('INVALID_REQUEST', 'Scope/object/mesh identities must be distinct', '$.manifest.targets')
    if result['mode'] == 'update' and any(t['initialize_identity'] for t in targets):
        _fail('UNSUPPORTED', 'Update cannot initialize or reassign target identities', '$.manifest.targets')
    lines = result['lines']
    if ((lines['backend'] == 'screen_ribbon' and lines['width_mode'] not in ('pixel', 'frame_fraction'))
            or (lines['backend'] == 'tube_world' and lines['width_mode'] != 'world')):
        _fail('UNSUPPORTED', 'Carrier and width domain are incompatible', '$.manifest.lines.width_mode')
    if lines['width_mode'] == 'frame_fraction' and lines['width'] > 1:
        _fail('INVALID_REQUEST', 'Frame fraction must be in (0,1]', '$.manifest.lines.width')
    if result['time']['end'] < result['time']['start'] or result['time']['end'] - result['time']['start'] + 1 > 72:
        _fail('UNSUPPORTED', 'Ordered integer grid of at most72 frames required', '$.manifest.time')
    if is_grouped(result):
        _validate_line_groups(lines, targets, identities, result['schema_version'])
    if result['schema_version'] in STYLED_VERSIONS:
        _validate_styled_lines(lines, targets)
    if is_placed(result):
        _validate_placement_lines(lines)
    shading = result['shading']
    if shading['mode'] == 'eevee_regions':
        from . import regions
        reserved = identities + [g['group_id'] for g in lines['groups']]
        result['shading'] = regions.normalize_shading(shading, targets, result['engine'], reserved)
    elif shading['mode'] == 'eevee_cel3':
        if result['engine'] != 'BLENDER_EEVEE':
            _fail('UNSUPPORTED', 'eevee_cel3 requires BLENDER_EEVEE', '$.manifest.engine')
        for field in ('base_color', 'shadow_color', 'mid_color', 'lit_color'):
            if shading[field][3] != 1:
                _fail('UNSUPPORTED', 'eevee_cel3 requires opaque color alpha=1', '$.manifest.shading.' + field)
        if shading['threshold_low'] >= shading['threshold_high']:
            _fail('INVALID_REQUEST', 'eevee_cel3 requires threshold_low < threshold_high', '$.manifest.shading.threshold_low')
    elif shading['mode'] == 'eevee_cel3_reflect':
        if result['engine'] != 'BLENDER_EEVEE':
            _fail('UNSUPPORTED', 'eevee_cel3_reflect requires BLENDER_EEVEE', '$.manifest.engine')
        for field in ('base_color', 'shadow_color', 'mid_color', 'lit_color', 'reflection_color'):
            if shading[field][3] != 1:
                _fail('UNSUPPORTED', 'eevee_cel3_reflect requires opaque color alpha=1', '$.manifest.shading.' + field)
        if shading['threshold_low'] >= shading['threshold_high']:
            _fail('INVALID_REQUEST', 'eevee_cel3_reflect requires threshold_low < threshold_high', '$.manifest.shading.threshold_low')
    if is_animated(result):
        from . import animation_contract
        result['animations'] = animation_contract.normalize_tracks(result['animations'], result['shading'],
            result['time']['start'], result['time']['end'], result['engine'])
    return result


def _validate_line_groups(lines, targets, identities, version=GROUPED_VERSION):
    """Versioned membership/ownership rules; never inject legacy fields."""
    path = '$.manifest.lines'
    if set(lines['kind_order']) != set(lines['kinds']):
        _fail('INVALID_REQUEST', 'kind_order must be an exact permutation of global kinds', path + '.kind_order')
    if lines['color'][3] != 1:
        _fail('UNSUPPORTED', 'Grouped lines require opaque color alpha=1', path + '.color')
    target_ids = {target['object_id'] for target in targets}
    used_ids, occupied = set(identities), set()
    style_fields = tuple((LINE_STYLE_SCHEMA_V2 if version == GROUPED_VERSION else
                          LINE_STYLE_SCHEMA_V4 if version in PLACED_VERSIONS else LINE_STYLE_SCHEMA)['properties'])
    for index, group in enumerate(lines['groups']):
        location = path + '.groups[' + str(index) + ']'
        if group['group_id'] in used_ids:
            _fail('INVALID_REQUEST', 'Group UUID must be distinct from scope, targets, meshes and other groups', location + '.group_id')
        used_ids.add(group['group_id'])
        if not set(group['object_ids']) <= target_ids:
            _fail('INVALID_REQUEST', 'Group object_ids must refer to declared target UUIDs', location + '.object_ids')
        if not set(group['kinds']) <= set(lines['kinds']):
            _fail('INVALID_REQUEST', 'Group kinds must be a subset of global kinds', location + '.kinds')
        pairs = {(object_id, kind) for object_id in group['object_ids'] for kind in group['kinds']}
        if occupied & pairs:
            _fail('CONFLICT', 'Line groups overlap in object UUID and line kind, including disabled groups', location)
        occupied.update(pairs)
        effective = {field: lines[field] for field in style_fields}
        effective.update(group['style'])
        if effective['color'][3] != 1:
            _fail('UNSUPPORTED', 'Grouped lines require opaque color alpha=1', location + '.style.color')
        if effective['width_mode'] == 'frame_fraction' and effective['width'] > 1:
            _fail('INVALID_REQUEST', 'Effective frame fraction must be in (0,1]', location + '.style.width')


def validate_placement(value, path='$.manifest.lines.placement'):
    """Closed mode-dependent placement object with explicit field rules."""
    validate(value, PLACEMENT_SCHEMA, path)
    if value['mode'] == 'native_depth' and set(value) != {'mode'}:
        _fail('UNSUPPORTED', 'Native depth placement cannot declare camera_depth', path)
    if value['mode'] == 'camera_plane' and set(value) != {'mode', 'camera_depth'}:
        _fail('INVALID_REQUEST', 'Camera plane placement requires camera_depth', path)
    return value


def effective_placements(lines):
    """Ordered global and enabled group placements, including empty buckets."""
    rows = [{'group_id': 'global', 'placement': lines['placement'], 'color': lines['color']}]
    rows.extend({'group_id': group['group_id'], 'placement': group['style'].get('placement', lines['placement']),
                 'color': group['style'].get('color', lines['color'])}
                for group in lines['groups'] if group['enabled'])
    return rows


def _validate_placement_lines(lines):
    validate_placement(lines['placement'])
    for index, group in enumerate(lines['groups']):
        if 'placement' in group['style']:
            validate_placement(group['style']['placement'], '$.manifest.lines.groups[%d].style.placement' % index)
    planes = [row for row in effective_placements(lines) if row['placement']['mode'] == 'camera_plane']
    for index, row in enumerate(planes):
        depth = row['placement']['camera_depth']
        for other in planes[:index]:
            other_depth = other['placement']['camera_depth']
            if row['color'] != other['color'] and abs(depth - other_depth) < 1e-4 * max(1, depth, other_depth):
                _fail('CONFLICT', 'Different-color camera planes require explicit depth separation', '$.manifest.lines.placement')


def _validate_width_profile(profile, path):
    if profile[0]['u'] != 0 or profile[-1]['u'] != 1:
        _fail('INVALID_REQUEST', 'Width profile must include canonical u=0 and u=1', path)
    if any(a['u'] >= b['u'] for a, b in zip(profile, profile[1:])):
        _fail('INVALID_REQUEST', 'Width profile u values must be strictly increasing', path)


def _validate_stroke(stroke, path):
    _validate_width_profile(stroke['width_profile'], path + '.width_profile')
    fade = stroke['depth_width_fade']
    if fade['start_depth'] >= fade['end_depth']:
        _fail('INVALID_REQUEST', 'Width fade requires start_depth < end_depth even when disabled', path + '.depth_width_fade')


def _validate_styled_lines(lines, targets):
    path = '$.manifest.lines'
    _validate_stroke(lines['stroke'], path + '.stroke')
    for i, group in enumerate(lines['groups']):
        if 'stroke' in group['style']:
            _validate_stroke(group['style']['stroke'], path + '.groups[%d].style.stroke' % i)
    meshes = {target['object_id']: target['mesh_id'] for target in targets}
    seen = set()
    for i, edit in enumerate(lines['edge_overrides']):
        location = path + '.edge_overrides[%d]' % i
        if edit['object_id'] not in meshes or meshes[edit['object_id']] != edit['mesh_id']:
            _fail('INVALID_REQUEST', 'Edge override must match a declared target Object/Mesh UUID pair', location)
        if edit['point_ids'][0] >= edit['point_ids'][1]:
            _fail('INVALID_REQUEST', 'Canonical point_ids require strictly ascending distinct IDs', location + '.point_ids')
        key = (edit['object_id'], edit['mesh_id'], edit['edge_id'], *edit['point_ids'])
        if key in seen:
            _fail('CONFLICT', 'Only one edge override may select a canonical physical key', location)
        seen.add(key)
        _validate_width_profile(edit['width_profile'], location + '.width_profile')
        prior_end = None
        for j, interval in enumerate(edit['visible_intervals']):
            if interval['start'] >= interval['end'] or (prior_end is not None and interval['start'] <= prior_end):
                _fail('INVALID_REQUEST', 'Visible intervals must be positive, sorted and strictly separated', location + '.visible_intervals[%d]' % j)
            prior_end = interval['end']


def _source(value, schema):
    validate(value, schema, '$.params')
    result = deepcopy(value)
    result['file'] = str(Path(result['file']).resolve())
    if Path(result['file']).suffix.lower() != '.blend':
        _fail('INVALID_REQUEST', 'Saved .blend source required', '$.params.file')
    seen = {str(Path(result['file']).resolve()).casefold()}
    for i, document in enumerate(result['resources']):
        document['file'] = str(Path(document['file']).resolve())
        key = document['file'].casefold()
        if key in seen: _fail('INVALID_REQUEST', 'Duplicate/source resource path', '$.params.resources[' + str(i) + ']')
        seen.add(key)
    return result


def normalize_params(value):
    # Check the rejected former version before ordinary strict field traversal.
    if isinstance(value, dict) and isinstance(value.get('manifest'), dict):
        if value['manifest'].get('schema_version') == '0.1.0-draft.2':
            _fail('UNSUPPORTED', 'P2 draft.2 was never registered; no implicit migration', '$.manifest.schema_version')
    result = _source(value, RUN_PARAMS_SCHEMA)
    result['manifest'] = normalize_manifest(result['manifest'])
    if (result['manifest']['mode'] == 'update') != ('baseline' in result):
        _fail('INVALID_REQUEST', 'Only update requires the original state baseline', '$.params.baseline')
    if 'baseline' in result:
        result['baseline']['file'] = str(Path(result['baseline']['file']).resolve())
        if result['baseline']['file'].casefold() in {result['file'].casefold(), *(r['file'].casefold() for r in result['resources'])}:
            _fail('INVALID_REQUEST', 'Baseline must be a separate immutable document', '$.params.baseline')
    return result


def normalize_inspect_params(value):
    return _source(value, INSPECT_PARAMS_SCHEMA)


def normalize_request(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'command', 'params'}:
        _fail('INVALID_REQUEST', 'Expected schema_version/command/params envelope', '$')
    if value['schema_version'] != '1.0': _fail('INVALID_REQUEST', 'Envelope schema_version must be1.0', '$.schema_version')
    if value['command'] not in ('toon.run', 'toon.inspect'): _fail('UNSUPPORTED', 'Only run/inspect are implemented', '$.command')
    fn = normalize_params if value['command'] == 'toon.run' else normalize_inspect_params
    return {'schema_version': '1.0', 'command': value['command'], 'params': fn(value['params'])}


def describe(operation=None, include_schema=False, section=None):
    if operation not in (None, 'run', 'inspect'):
        _fail('UNSUPPORTED', 'Only run/inspect discovery is available', '$.operation')
    if (include_schema or section) and operation is None:
        _fail('INVALID_REQUEST', 'Schema/section requires operation', '$.operation')
    if include_schema and section: _fail('INVALID_REQUEST', 'Choose schema or section', '$.section')
    if section and (operation != 'run' or section not in ('targets', 'lines', 'shading', 'time', 'render', 'animations')):
        _fail('UNSUPPORTED', 'Unknown manifest section', '$.section')
    rows = []
    for name in ([operation] if operation else ['run', 'inspect']):
        row = {'command': 'toon.' + name, 'execution': 'standalone_blender_worker',
               'required_params': list((RUN_PARAMS_SCHEMA if name == 'run' else INSPECT_PARAMS_SCHEMA)['required'])}
        if include_schema: row['params_schema'] = deepcopy(RUN_PARAMS_SCHEMA if name == 'run' else INSPECT_PARAMS_SCHEMA)
        if name == 'run': row['manifest_versions'] = list(MANIFEST_VERSIONS)
        if section:
            row['section'] = section
            row['manifest_schema_version'] = VERSION
            row['schema'] = deepcopy(CURRENT_MANIFEST_SCHEMA['properties'][section])
            row['legacy_section_schemas'] = {version: deepcopy(schema['properties'][section])
                for version, schema in MANIFEST_SCHEMAS.items() if version != VERSION and section in schema['properties']}
        rows.append(row)
    return {'contract_version': VERSION, 'manifest_versions': list(MANIFEST_VERSIONS),
        'default_discovery_manifest_version': VERSION,
        'operations': rows, 'state_source': STATE_SOURCE,
        'bpy_imported': False, 'capabilities_are_runtime_certified': False,
        'limits': {'fps': 24, 'max_frames': 72, 'max_dimension': 2048},
        'unsupported': ['P2 draft.2 migration', 'resume', 'xray', 'face_pair', 'brush', 'optical_layer_separation',
                        'motion_blur', 'depth_of_field', 'subframes', 'template_versions_other_than_v1', 'study', 'GUI_apply', 'DAG']}
