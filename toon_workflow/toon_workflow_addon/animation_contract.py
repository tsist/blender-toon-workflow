# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure data layer for EEVEE region parameter animation.

This module validates and samples declared parameter tracks.  It does not
create Blender Actions, evaluate native F-curves, start Blender, touch a scene,
or certify render-time/subframe/float32 behaviour.
"""
from bisect import bisect_left, bisect_right
from copy import deepcopy
import math
import uuid

from . import contract, materials


MAX_TRACKS = 64
MAX_KEYS = 32
MAX_FRAMES = 72
INTERPOLATIONS = ('LINEAR', 'CONSTANT')
THRESHOLD_FIELDS = frozenset(('threshold_low', 'threshold_high'))
TRACK_FIELDS = frozenset(('template_id', 'field', 'interpolation', 'keys'))
KEY_FIELDS = frozenset(('frame', 'value'))
SHADING_FIELDS = frozenset(('mode', 'unassigned', 'templates', 'assignments'))


def _error(code, message, path='$'):
    raise contract.WorkflowError(code, message, path)


def _canonical_uuid(value, path):
    if type(value) is not str:
        _error('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _error('INVALID_REQUEST', 'Explicit UUID required', path)
    if str(parsed) != value or parsed.int == 0:
        _error('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
    return value


def _finite_number(value, path, *, bounded=False):
    if type(value) not in (int, float):
        _error('INVALID_REQUEST', 'Finite number required', path)
    if bounded and not 0 <= value <= 1:
        _error('INVALID_REQUEST', 'Finite [0,1] value required', path)
    if type(value) is float:
        if not math.isfinite(value):
            _error('INVALID_REQUEST', 'Finite number required', path)
        return value
    try:
        converted = float(value)
    except OverflowError:
        _error('INVALID_REQUEST', 'Finite number required', path)
    if not math.isfinite(converted):
        _error('INVALID_REQUEST', 'Finite number required', path)
    return converted


def _frame_time(value, path):
    if type(value) not in (int, float):
        _error('INVALID_REQUEST', 'Finite int/float frame required', path)
    if type(value) is float and not math.isfinite(value):
        _error('INVALID_REQUEST', 'Finite int/float frame required', path)
    if type(value) is int:
        try:
            converted = float(value)
        except OverflowError:
            _error('INVALID_REQUEST', 'Finite int/float frame required', path)
        if not math.isfinite(converted):
            _error('INVALID_REQUEST', 'Finite int/float frame required', path)
    return value


def _parse_keys(keys, path, *, field=None, normalized=False):
    if type(keys) is not list:
        _error('INVALID_REQUEST', 'keys must be a list', path)
    if normalized:
        if not 2 <= len(keys) <= MAX_KEYS:
            _error('INVALID_REQUEST', 'Track requires 2..32 keys', path)
    elif not keys:
        _error('INVALID_REQUEST', 'Track requires at least one key', path)

    expected_color = None if field is None else field.endswith('_color')
    actual_color = None
    result = []
    previous_frame = None
    for index, key in enumerate(keys):
        location = path + '[' + str(index) + ']'
        if type(key) is not dict:
            _error('INVALID_REQUEST', 'Each key must be an object', location)
        extras = set(key) - KEY_FIELDS
        if extras:
            _error('UNSUPPORTED', 'Unsupported key fields: ' +
                   ', '.join(sorted(map(str, extras))), location)
        missing = KEY_FIELDS - set(key)
        if missing:
            _error('INVALID_REQUEST', 'Missing key fields: ' +
                   ', '.join(sorted(missing)), location)
        frame = key['frame']
        if type(frame) is not int:
            _error('INVALID_REQUEST', 'Key frame must be an integer', location + '.frame')
        if previous_frame is not None and frame <= previous_frame:
            _error('INVALID_REQUEST', 'Key frames must be strictly increasing',
                   location + '.frame')
        previous_frame = frame

        raw_value = key['value']
        is_color = type(raw_value) in (list, tuple)
        if expected_color is not None and is_color != expected_color:
            _error('INVALID_REQUEST', 'Color field requires four channels and '
                   'scalar field requires one value', location + '.value')
        if actual_color is not None and is_color != actual_color:
            _error('INVALID_REQUEST', 'Key value shape must be consistent',
                   location + '.value')
        actual_color = is_color
        if is_color:
            if len(raw_value) != 4:
                _error('INVALID_REQUEST', 'Color value requires exactly four channels',
                       location + '.value')
            value = [
                _finite_number(channel, location + '.value[' + str(channel_index) + ']',
                               bounded=normalized)
                for channel_index, channel in enumerate(raw_value)
            ]
            if normalized and value[3] != 1.0:
                _error('UNSUPPORTED', 'Opaque animation color requires alpha=1',
                       location + '.value[3]')
        else:
            value = _finite_number(raw_value, location + '.value', bounded=normalized)
        result.append({'frame': frame, 'value': value})
    return result


def _parse_track(track, path):
    if type(track) is not dict:
        _error('INVALID_REQUEST', 'Track must be an object', path)
    extras = set(track) - TRACK_FIELDS
    if extras:
        _error('UNSUPPORTED', 'Unknown or unsupported track fields/version: ' +
               ', '.join(sorted(map(str, extras))), path)
    missing = TRACK_FIELDS - set(track)
    if missing:
        _error('INVALID_REQUEST', 'Missing track fields: ' +
               ', '.join(sorted(missing)), path)
    template_id = _canonical_uuid(track['template_id'], path + '.template_id')
    field = track['field']
    if type(field) is not str or not field:
        _error('INVALID_REQUEST', 'Nonempty field name required', path + '.field')
    interpolation = track['interpolation']
    if type(interpolation) is not str:
        _error('INVALID_REQUEST', 'Interpolation must be LINEAR or CONSTANT',
               path + '.interpolation')
    if interpolation not in INTERPOLATIONS:
        _error('UNSUPPORTED', 'Unsupported interpolation: ' + interpolation,
               path + '.interpolation')
    keys = _parse_keys(track['keys'], path + '.keys', field=field, normalized=True)
    return {'template_id': template_id, 'field': field,
            'interpolation': interpolation, 'keys': keys}


def _parse_basic_track(track, path):
    """Read the minimal evaluator structure without inventing defaults."""
    if type(track) is not dict:
        _error('INVALID_REQUEST', 'Track must be an object', path)
    if 'interpolation' not in track or 'keys' not in track:
        _error('INVALID_REQUEST', 'Track requires interpolation and keys', path)
    interpolation = track['interpolation']
    if type(interpolation) is not str:
        _error('INVALID_REQUEST', 'Interpolation must be LINEAR or CONSTANT',
               path + '.interpolation')
    if interpolation not in INTERPOLATIONS:
        _error('UNSUPPORTED', 'Unsupported interpolation: ' + interpolation,
               path + '.interpolation')
    if 'field' in track:
        field = track['field']
        if type(field) is not str or not field:
            _error('INVALID_REQUEST', 'Nonempty field name required',
                   path + '.field')
    else:
        field = None
    keys = _parse_keys(track['keys'], path + '.keys', field=field, normalized=False)
    return {'interpolation': interpolation, 'keys': keys}


def _copy_value(value):
    return list(value) if type(value) is list else value


def _evaluate_parsed(track, frame):
    frames = [key['frame'] for key in track['keys']]
    if frame <= frames[0]:
        return _copy_value(track['keys'][0]['value'])
    if frame >= frames[-1]:
        return _copy_value(track['keys'][-1]['value'])

    left_index = bisect_right(frames, frame) - 1
    left = track['keys'][left_index]
    if frame == left['frame'] or track['interpolation'] == 'CONSTANT':
        return _copy_value(left['value'])
    right = track['keys'][left_index + 1]
    fraction = (frame - left['frame']) / (right['frame'] - left['frame'])
    if type(left['value']) is list:
        return [a + (b - a) * fraction for a, b in zip(left['value'], right['value'])]
    return left['value'] + (right['value'] - left['value']) * fraction


def _shading_templates(shading, engine):
    if engine != 'BLENDER_EEVEE':
        _error('UNSUPPORTED', 'EEVEE region animation requires BLENDER_EEVEE',
               '$.engine')
    if type(shading) is not dict:
        _error('INVALID_REQUEST', 'EEVEE regions shading object required', '$.shading')
    if set(shading) != SHADING_FIELDS:
        _error('INVALID_REQUEST', 'Exact eevee_regions four-field shading required',
               '$.shading')
    contract.validate(shading, contract.REGIONS_SHADING_SCHEMA, '$.shading')

    templates = {}
    for index, template in enumerate(shading['templates']):
        location = '$.shading.templates[' + str(index) + ']'
        template_id = _canonical_uuid(template['template_id'],
                                      location + '.template_id')
        if template_id in templates:
            _error('CONFLICT', 'Duplicate template_id', location + '.template_id')
        settings = materials._settings(template['settings'], engine)
        templates[template_id] = {
            'settings': settings,
            'active': tuple(materials._active(engine, settings['mode'])),
        }
    for index, assignment in enumerate(shading['assignments']):
        if assignment['template_id'] not in templates:
            _error('INVALID_REQUEST', 'Assignment must reference an existing template',
                   '$.shading.assignments[' + str(index) + '].template_id')
    return templates


def _validate_time(frame_start, frame_end):
    if type(frame_start) is not int or type(frame_end) is not int:
        _error('INVALID_REQUEST', 'frame_start and frame_end must be integers',
               '$.time')
    if frame_end < frame_start:
        _error('INVALID_REQUEST', 'frame_end must not precede frame_start', '$.time')
    if frame_end - frame_start + 1 > MAX_FRAMES:
        _error('UNSUPPORTED', 'Ordered integer grid of at most72 frames required',
               '$.time')


def _merged_at(templates, by_template, frame, engine):
    for template_id, tracked in by_template.items():
        merged = deepcopy(templates[template_id]['settings'])
        for track in tracked:
            merged[track['field']] = _evaluate_parsed(track, frame)
        try:
            materials._settings(merged, engine)
        except contract.WorkflowError as exc:
            raise contract.WorkflowError(
                exc.code,
                'Invalid merged shading at frame ' + str(frame) +
                ' for template ' + template_id + ': ' + exc.message,
                '$.tracks',
            ) from exc


def _track_limit(track, frame, side):
    """Return a symbolic one-sided limit without epsilon sampling."""
    if side not in ('left', 'right'):
        raise AssertionError('side must be left or right')
    keys = track['keys']
    frames = [key['frame'] for key in keys]
    if frame <= frames[0]:
        return _copy_value(keys[0]['value'])
    if frame >= frames[-1]:
        if frame == frames[-1] and side == 'left' and \
                track['interpolation'] == 'CONSTANT':
            return _copy_value(keys[-2]['value'])
        return _copy_value(keys[-1]['value'])

    right_index = bisect_left(frames, frame)
    if frames[right_index] == frame:
        if side == 'right' or track['interpolation'] == 'LINEAR':
            return _copy_value(keys[right_index]['value'])
        return _copy_value(keys[right_index - 1]['value'])

    left = keys[right_index - 1]
    if track['interpolation'] == 'CONSTANT':
        return _copy_value(left['value'])
    right = keys[right_index]
    fraction = (frame - left['frame']) / (right['frame'] - left['frame'])
    if type(left['value']) is list:
        return [a + (b - a) * fraction
                for a, b in zip(left['value'], right['value'])]
    return left['value'] + (right['value'] - left['value']) * fraction


def _merged_limit_at(templates, template_id, tracked, frame, side,
                     lower, upper, engine):
    merged = deepcopy(templates[template_id]['settings'])
    for track in tracked:
        merged[track['field']] = _track_limit(track, frame, side)
    try:
        materials._settings(merged, engine)
    except contract.WorkflowError as exc:
        raise contract.WorkflowError(
            exc.code,
            'Invalid merged shading for template ' + template_id +
            ' in open interval (' + str(lower) + ', ' + str(upper) + '), ' +
            side + ' limit at frame ' + str(frame) + ': ' + exc.message,
            '$.tracks',
        ) from exc


def _validate_threshold_continuum(templates, by_template, frame_start,
                                  frame_end, engine):
    """Check both open-interval limits of every piecewise threshold segment."""
    for template_id, tracked in by_template.items():
        threshold_tracks = [
            track for track in tracked if track['field'] in THRESHOLD_FIELDS
        ]
        if not threshold_tracks:
            continue
        critical = {frame_start, frame_end}
        for track in threshold_tracks:
            critical.update(key['frame'] for key in track['keys'])
        ordered = sorted(critical)
        # Exact critical values, including CONSTANT right-key jumps, were
        # checked by the complete-settings point pass above.  This pass proves
        # the open intervals from their symbolic left/right limits.
        for lower, upper in zip(ordered, ordered[1:]):
            _merged_limit_at(templates, template_id, tracked, lower, 'right',
                             lower, upper, engine)
            _merged_limit_at(templates, template_id, tracked, upper, 'left',
                             lower, upper, engine)


def normalize_tracks(tracks, shading, frame_start, frame_end,
                     engine='BLENDER_EEVEE'):
    """Validate and copy parameter tracks rooted on the inclusive frame grid."""
    _validate_time(frame_start, frame_end)
    if type(tracks) is not list:
        _error('INVALID_REQUEST', 'tracks must be a list', '$.tracks')
    if len(tracks) > MAX_TRACKS:
        _error('INVALID_REQUEST', 'At most 64 tracks are allowed', '$.tracks')
    templates = _shading_templates(shading, engine)

    normalized = []
    selectors = set()
    by_template = {}
    for index, raw_track in enumerate(tracks):
        path = '$.tracks[' + str(index) + ']'
        track = _parse_track(raw_track, path)
        template_id = track['template_id']
        if template_id not in templates:
            _error('UNSUPPORTED', 'Track references an unknown template',
                   path + '.template_id')
        if track['field'] not in templates[template_id]['active']:
            _error('UNSUPPORTED', 'Field is not active for template mode: ' +
                   track['field'], path + '.field')
        selector = (template_id, track['field'])
        if selector in selectors:
            _error('CONFLICT', 'Duplicate (template_id, field) selector', path)
        selectors.add(selector)
        keys = track['keys']
        if keys[0]['frame'] != frame_start or keys[-1]['frame'] != frame_end:
            _error('INVALID_REQUEST',
                   'First/last key must equal frame_start/frame_end',
                   path + '.keys')
        normalized.append(track)
        by_template.setdefault(template_id, []).append(track)

    # Key-time union plus every integer frame.  Key times are integers in this
    # contract, but the explicit union documents the complete declared graph.
    points = set(range(frame_start, frame_end + 1))
    for track in normalized:
        points.update(key['frame'] for key in track['keys'])
    for frame in sorted(points):
        _merged_at(templates, by_template, frame, engine)
    _validate_threshold_continuum(
        templates, by_template, frame_start, frame_end, engine)
    return normalized


def evaluate_track(track, frame):
    """Evaluate one declared track without a Blender Action."""
    moment = _frame_time(frame, '$.frame')
    parsed = _parse_basic_track(track, '$.track')
    return _evaluate_parsed(parsed, moment)


def sample_shading(shading, tracks, frame):
    """Return a complete shading copy with matching fields sampled."""
    moment = _frame_time(frame, '$.frame')
    templates = _shading_templates(shading, 'BLENDER_EEVEE')
    if type(tracks) is not list:
        _error('INVALID_REQUEST', 'tracks must be a list', '$.tracks')
    if len(tracks) > MAX_TRACKS:
        _error('INVALID_REQUEST', 'At most 64 tracks are allowed', '$.tracks')

    parsed_tracks = []
    selectors = set()
    for index, raw_track in enumerate(tracks):
        path = '$.tracks[' + str(index) + ']'
        track = _parse_track(raw_track, path)
        if track['template_id'] not in templates:
            _error('UNSUPPORTED', 'Track references an unknown template',
                   path + '.template_id')
        if track['field'] not in templates[track['template_id']]['active']:
            _error('UNSUPPORTED', 'Field is not active for template mode: ' +
                   track['field'], path + '.field')
        selector = (track['template_id'], track['field'])
        if selector in selectors:
            _error('CONFLICT', 'Duplicate (template_id, field) selector', path)
        selectors.add(selector)
        parsed_tracks.append(track)

    index_by_template = {
        template['template_id']: index
        for index, template in enumerate(shading['templates'])
    }
    result = deepcopy(shading)
    for track in parsed_tracks:
        template_index = index_by_template[track['template_id']]
        result['templates'][template_index]['settings'][track['field']] = \
            _evaluate_parsed(track, moment)

    for template in result['templates']:
        try:
            materials._settings(template['settings'], 'BLENDER_EEVEE')
        except contract.WorkflowError as exc:
            raise contract.WorkflowError(
                exc.code,
                'Invalid sampled shading for template ' +
                template['template_id'] + ': ' + exc.message,
                '$.shading',
            ) from exc
    return result


def _selectors(tracks, name):
    if type(tracks) is not list:
        _error('INVALID_REQUEST', name + ' must be a list', '$.' + name)
    if len(tracks) > MAX_TRACKS:
        _error('INVALID_REQUEST', 'At most 64 tracks are allowed', '$.' + name)
    result = []
    seen = set()
    for index, raw_track in enumerate(tracks):
        path = '$.' + name + '[' + str(index) + ']'
        track = _parse_track(raw_track, path)
        selector = (track['template_id'], track['field'])
        if selector in seen:
            _error('CONFLICT', 'Duplicate (template_id, field) selector', path)
        seen.add(selector)
        result.append(selector)
    return result


def validate_update_selectors(previous_tracks, desired_tracks):
    """Require an identical ordered selector list while allowing new keys."""
    previous = _selectors(previous_tracks, 'previous_tracks')
    desired = _selectors(desired_tracks, 'desired_tracks')
    if previous != desired:
        _error('UNSUPPORTED',
               'Update selectors must remain identical and ordered',
               '$.desired_tracks')
    return True
