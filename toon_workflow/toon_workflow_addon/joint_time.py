# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure sequence clocks: historical receipts and native Blender 5.2 time.

Native output uses Blender's signed modf representation. Setter arguments use
floor plus a nonnegative float32 fraction; those representations are distinct.
No bpy, handlers, tolerance comparisons or implicit migration is involved.
"""
import json
import math
import struct

LEGACY_TIME_POLICY = 'floor + float32 subframe; preserve order'
NATIVE_TIME_POLICY = 'blender-frame-set-f32-total.v1'
LEGACY_PROFILE = 'eevee_joint_sequence.v1'
PROFILE = 'eevee_joint_sequence.v2'
MAX_TIMES = 96
MIN_FRAME = -1048574
MAX_FRAME = 1048574


def profile_for_policy(time_policy):
    if type(time_policy) is not str:
        raise ValueError('Unknown sequence time policy')
    if time_policy == LEGACY_TIME_POLICY:
        return LEGACY_PROFILE
    if time_policy == NATIVE_TIME_POLICY:
        return PROFILE
    raise ValueError('Unknown sequence time policy')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def _f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _zero(value):
    return 0.0 if value == 0.0 else value


def _legacy_normalize_times(times):
    # Keep the historical algorithm, including bounds and collision semantics.
    if type(times) not in (list, tuple) or not 1 <= len(times) <= MAX_TIMES:
        raise ValueError('times must be an explicit ordered list of 1..96 numbers')
    rows, seen = [], set()
    for value in times:
        if type(value) not in (int, float):
            raise ValueError('Time must be a finite int/float, never bool')
        try:
            number = float(value)
        except OverflowError as error:
            raise ValueError('Time must be finite and representable') from error
        if not math.isfinite(number) or (type(value) is int and int(number) != value):
            raise ValueError('Time must be finite and exactly representable')
        frame = math.floor(number)
        subframe = struct.unpack('f', struct.pack('f', number - frame))[0]
        if subframe == 1.0:
            frame, subframe = frame + 1, 0.0
        key = (frame, subframe)
        if key in seen:
            raise ValueError('Duplicate time after Blender subframe normalization')
        seen.add(key)
        rows.append(dict(frame=frame, subframe=subframe))
    return rows


def _legacy_validate_normalized(rows):
    if type(rows) is not list or not 1 <= len(rows) <= MAX_TIMES:
        raise ValueError('Invalid normalized time list')
    seen = set()
    for row in rows:
        if (type(row) is not dict or set(row) != {'frame', 'subframe'}
                or type(row['frame']) is not int
                or type(row['subframe']) not in (int, float)
                or not math.isfinite(row['subframe']) or not 0 <= row['subframe'] < 1
                or struct.unpack('f', struct.pack('f', row['subframe']))[0] != row['subframe']):
            raise ValueError('Invalid normalized frame/subframe')
        key = (row['frame'], row['subframe'])
        if key in seen:
            raise ValueError('Duplicate normalized time')
        seen.add(key)


def _floor_arguments(total):
    frame = math.floor(total)
    if not MIN_FRAME <= frame <= MAX_FRAME:
        raise ValueError('Sequence setter frame exceeds native Blender bounds')
    subframe = _f32(total - frame)
    if subframe == 1.0:
        frame, subframe = frame + 1, 0.0
    if not MIN_FRAME <= frame <= MAX_FRAME:
        raise ValueError('Sequence carried frame exceeds native Blender bounds')
    return dict(frame=frame, subframe=_zero(subframe))


def _native_observed(arguments):
    # Public preflight excludes RNA argument clamps and the native total clamp.
    # The double sum is narrowed at BKE_scene_frame_set(Scene*, float), then
    # modf truncates toward zero. Canonical zero has one JSON representation.
    total = _f32(float(arguments['frame']) + float(arguments['subframe']))
    fraction, whole = math.modf(total)
    return dict(frame=int(whole), subframe=_zero(_f32(fraction)))


def _native_arguments(row):
    if (type(row) is not dict or set(row) != {'frame', 'subframe'}
            or type(row['frame']) is not int or type(row['subframe']) is not float
            or not MIN_FRAME <= row['frame'] <= MAX_FRAME
            or not math.isfinite(row['subframe']) or not -1.0 < row['subframe'] < 1.0):
        raise ValueError('Invalid native observed frame/subframe')
    total = float(row['frame']) + row['subframe']
    if not MIN_FRAME <= total <= MAX_FRAME or _f32(total) != total:
        raise ValueError('Native observed total is outside bounds or not float32')
    fraction, whole = math.modf(total)
    canonical = dict(frame=int(whole), subframe=_zero(_f32(fraction)))
    if _json_bytes(row) != _json_bytes(canonical):
        raise ValueError('Native observed frame/subframe is not canonical')
    arguments = _floor_arguments(total)
    if _json_bytes(_native_observed(arguments)) != _json_bytes(row):
        raise ValueError('Native effective time is not a setter fixed point')
    return arguments, _zero(total)


def normalize_times(times, *, time_policy=NATIVE_TIME_POLICY):
    """Preserve order and reject collisions under the selected exact clock."""
    profile_for_policy(time_policy)
    if time_policy == LEGACY_TIME_POLICY:
        return _legacy_normalize_times(times)
    if type(times) not in (list, tuple) or not 1 <= len(times) <= MAX_TIMES:
        raise ValueError('times must be an explicit ordered list of 1..96 numbers')
    rows, seen = [], set()
    for value in times:
        if type(value) not in (int, float):
            raise ValueError('Time must be a finite int/float, never bool')
        try:
            number = float(value)
        except OverflowError as error:
            raise ValueError('Time must be finite and representable') from error
        if not math.isfinite(number) or (type(value) is int and int(number) != value):
            raise ValueError('Time must be finite and exactly representable')
        if not MIN_FRAME <= number <= MAX_FRAME:
            raise ValueError('Sequence raw time exceeds native Blender bounds')
        row = _native_observed(_floor_arguments(number))
        _, total = _native_arguments(row)
        if total in seen:
            raise ValueError('Duplicate time after native Blender effective-time normalization')
        seen.add(total)
        rows.append(row)
    return rows


def validate_normalized(rows, *, time_policy):
    """Validate stored rows without rewriting them or choosing another policy."""
    profile_for_policy(time_policy)
    if time_policy == LEGACY_TIME_POLICY:
        return _legacy_validate_normalized(rows)
    if type(rows) is not list or not 1 <= len(rows) <= MAX_TIMES:
        raise ValueError('Invalid normalized time list')
    seen = set()
    for row in rows:
        _, total = _native_arguments(row)
        if total in seen:
            raise ValueError('Duplicate native effective time')
        seen.add(total)


def setter_arguments(row, *, time_policy):
    """Return a plain frame/subframe dict accepted by Scene.frame_set."""
    profile_for_policy(time_policy)
    if time_policy == LEGACY_TIME_POLICY:
        _legacy_validate_normalized([row])
        return dict(row)
    arguments, _ = _native_arguments(row)
    return arguments


def validate_settings_times(settings, *, allow_legacy_missing=False):
    """Return the clock only after exact profile/policy/nominal binding checks.

    Only a cold caller that has already identified a legacy profile may opt in
    to its historical missing-policy receipt. Native receipts always carry the
    original requested_times list, bound through canonical JSON to native rows.
    """
    if type(settings) is not dict:
        raise ValueError('Sequence settings must be a plain object')
    profile = settings.get('profile')
    if type(profile) is not str or profile not in {LEGACY_PROFILE, PROFILE}:
        raise ValueError('Unknown sequence settings profile')
    if 'time_policy' not in settings:
        if not (allow_legacy_missing is True and profile == LEGACY_PROFILE):
            raise ValueError('Sequence settings are missing their time policy')
        policy = LEGACY_TIME_POLICY
    else:
        policy = settings['time_policy']
    if profile_for_policy(policy) != profile:
        raise ValueError('Sequence profile/time policy mismatch')
    if 'times' not in settings:
        raise ValueError('Sequence settings are missing normalized times')
    validate_normalized(settings['times'], time_policy=policy)
    if policy == NATIVE_TIME_POLICY:
        if type(settings.get('requested_times')) is not list:
            raise ValueError('Native settings require an explicit requested_times list')
        canonical = normalize_times(settings['requested_times'], time_policy=policy)
        if _json_bytes(canonical) != _json_bytes(settings['times']):
            raise ValueError('Sequence requested_times differ from canonical native times')
    elif 'requested_times' in settings:
        raise ValueError('Legacy settings cannot carry native requested_times')
    return policy
