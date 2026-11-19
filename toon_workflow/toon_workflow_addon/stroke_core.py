"""Canonical source-u stroke math for the opt-in 0.3 slice; no bpy or I/O.

Each styled source edge is an independent open stroke. Width is measured in
vertical pixels, depth fade uses unbiased positive camera depth, and the
legacy helper remains the exact neutral implementation.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math
from fractions import Fraction

from . import outline_core as core

WIDTH_ERROR_PX = 0.01
CAP_ERROR_PX = 0.01
MAX_SAMPLES = 2048
MAX_VERTICES = 2000000
_MAX_REFERENCE_STEPS = 2 ** 52


def _clamp(value):
    return min(1.0, max(0.0, value))


def profile_value(profile, u):
    for left, right in zip(profile, profile[1:]):
        if u <= right['u']:
            t = (u - left['u']) / (right['u'] - left['u'])
            return left['scale'] + t * (right['scale'] - left['scale'])
    return profile[-1]['scale']


def width_factors(stroke, profile, u, depth):
    taper = stroke['taper']
    start, end = taper['start_u'], taper['end_u']
    fade = stroke['depth_width_fade']
    return (profile_value(profile, u),
            _clamp(u / start) if start else 1.0,
            _clamp((1.0 - u) / end) if end else 1.0,
            _clamp((fade['end_depth'] - depth) /
                   (fade['end_depth'] - fade['start_depth'])) if fade['enabled'] else 1.0)


def width_at(stroke, profile, u, depth, base_width):
    return base_width * math.prod(width_factors(stroke, profile, u, depth))


def is_neutral(stroke, profile=None):
    profile = profile if profile is not None else stroke['width_profile']
    dash = stroke['dash']
    return ((not dash['enabled'] or dash['duty'] == 1.0)
            and stroke['taper']['start_u'] == 0.0
            and stroke['taper']['end_u'] == 0.0
            and stroke['cap'] == 'butt'
            and all(point['scale'] == 1.0 for point in profile)
            and not stroke['depth_width_fade']['enabled'])


def dash_intervals(dash):
    """Return (lo,hi,ordinal); PLUS phase is anchored before clipping."""
    if not dash['enabled'] or dash['duty'] == 1.0:
        return [(0.0, 1.0, None)]
    if dash['duty'] == 0.0:
        return []
    p, phase, duty = dash['period_u'], dash['phase_u'], dash['duty']
    first = math.floor(phase / p) - 1
    last = math.ceil((1.0 + phase) / p) + 1
    result = []
    for ordinal in range(first, last + 1):
        lo = max(0.0, ordinal * p - phase)
        hi = min(1.0, (ordinal + duty) * p - phase)
        if hi > lo:
            result.append((lo, hi, ordinal))
    return result


def screen_to_u(t, a, b, da, db, perspective):
    """Inverse screen-linear interpolation on a positive-depth subsegment."""
    local = (t * da / ((1.0 - t) * db + t * da)
             if perspective and da != db else t)
    return a + (b - a) * local


def _exact_fraction(value, label):
    """Lift a binary float (or integer) without decimal/rounding reinterpretation."""
    if type(value) not in (int, float) or not math.isfinite(value):
        core.fail('OUTLINE_SAMPLE_NUMERICS', label + ' must be finite')
    return Fraction(value) if type(value) is int else Fraction.from_float(value)


def _validate_jitter_camera(camera):
    """Reject camera metrics that would make the reference projection undefined."""
    if camera.get('type') not in ('PERSP', 'ORTHO'):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference requires a PERSP or ORTHO camera')
    frame = camera.get('frame')
    resolution = camera.get('resolution')
    ratio = camera.get('pixel_ratio')
    if (not isinstance(frame, (list, tuple)) or len(frame) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in frame)
            or frame[1] == frame[0] or frame[3] == frame[2]
            or not isinstance(resolution, (list, tuple)) or len(resolution) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or value <= 0.0 for value in resolution)
            or type(ratio) not in (int, float) or not math.isfinite(ratio)
            or ratio <= 0.0):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference camera metrics are invalid')
    if camera['type'] == 'PERSP':
        frame_depth = camera.get('frame_depth')
        if (type(frame_depth) not in (int, float)
                or not math.isfinite(frame_depth) or frame_depth == 0.0):
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference perspective frame depth is invalid')


def _positive_depth_reference(a, b, camera):
    """Return the source-u intersection with the positive near/far depth slab."""
    _validate_jitter_camera(camera)
    near, far = camera.get('near'), camera.get('far')
    if (type(near) not in (int, float) or type(far) not in (int, float)
            or not math.isfinite(near) or not math.isfinite(far)
            or near <= 0.0 or far <= near):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference requires finite 0 < near < far')
    d0, d1 = -core.to_camera(a, camera)[2], -core.to_camera(b, camera)[2]
    if not math.isfinite(d0) or not math.isfinite(d1):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference endpoint depth is nonfinite')
    if d1 == d0:
        if near <= d0 <= far:
            r0, r1 = 0.0, 1.0
        else:
            return None
    else:
        an, af = (near - d0) / (d1 - d0), (far - d0) / (d1 - d0)
        r0, r1 = max(0.0, min(an, af)), min(1.0, max(an, af))
        if not r1 > r0:
            return None
    ra, rb = core.lerp(a, b, r0), core.lerp(a, b, r1)
    dr0, dr1 = -core.to_camera(ra, camera)[2], -core.to_camera(rb, camera)[2]
    if (not math.isfinite(dr0) or not math.isfinite(dr1)
            or dr0 <= 0.0 or dr1 <= 0.0):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference requires positive finite endpoint depths')
    return {'r0': r0, 'r1': r1, 'd_r0': dr0, 'd_r1': dr1,
            'perspective': camera['type'] == 'PERSP'}


def _mandatory_knots(stroke, profile, d0, d1):
    result = {0.0, 1.0, *(point['u'] for point in profile)}
    result.update((stroke['taper']['start_u'], 1.0 - stroke['taper']['end_u']))
    fade = stroke['depth_width_fade']
    if fade['enabled'] and d1 != d0:
        for depth in (fade['start_depth'], fade['end_depth']):
            u = (depth - d0) / (d1 - d0)
            if 0.0 < u < 1.0:
                result.add(u)
    return result


def _width_error_bound(stroke, profile, a, b, da, db, base, perspective):
    """Conservative full-width interpolation error: sup|w''(screen t)|/8.

    Mandatory factor knots must already partition this interval. Every factor
    is then affine in source-u. Product differentiation bounds all cross terms;
    the inverse perspective map contributes its first and second derivatives.
    """
    fa = width_factors(stroke, profile, a, da)
    fb = width_factors(stroke, profile, b, db)
    span = b - a
    maxima = [max(x, y) for x, y in zip(fa, fb)]
    slopes = [abs(y - x) / span for x, y in zip(fa, fb)]
    first = sum(slopes[i] * math.prod(maxima[j] for j in range(4) if j != i)
                for i in range(4))
    second = sum(slopes[i] * slopes[j] * math.prod(maxima[k] for k in range(4)
                 if k != i and k != j) for i in range(4) for j in range(4) if i != j)
    if perspective:
        minimum = min(da, db)
        u1 = span * da * db / (minimum * minimum)
        u2 = 2.0 * span * da * db * abs(db - da) / (minimum * minimum * minimum)
    else:
        u1, u2 = span, 0.0
    return base * (second * u1 * u1 + first * u2) / 8.0


def _jitter_error_bound(a, b, da, db, jitter_px, perspective):
    """Worst-case offset chord error over one positive-depth screen span."""
    if not math.isfinite(jitter_px) or jitter_px < 0.0:
        core.fail('OUTLINE_JITTER_NUMERICS',
                  'Jitter amplitude must be finite and nonnegative')
    if jitter_px == 0.0:
        return 0.0
    span = b - a
    if not math.isfinite(span) or span <= 0.0:
        core.fail('OUTLINE_JITTER_NUMERICS',
                  'Jitter interpolation span must be finite and positive')
    if perspective:
        if (not math.isfinite(da) or not math.isfinite(db)
                or da <= 0.0 or db <= 0.0):
            core.fail('OUTLINE_JITTER_NUMERICS',
                      'Jitter offset bound requires positive finite depths')
        minimum = min(da, db)
        u1 = span * da * db / (minimum * minimum)
        u2 = 2.0 * span * da * db * abs(db - da) / (minimum ** 3)
    else:
        u1, u2 = span, 0.0
    bound = (5.0 * math.pi * math.pi * jitter_px * u1 * u1
             + 2.0 * math.pi * jitter_px * u2) / 8.0
    if not math.isfinite(bound):
        core.fail('OUTLINE_JITTER_NUMERICS',
                  'Jitter interpolation error bound is nonfinite')
    return bound


def _sample_us(stroke, profile, lo, hi, d0, d1, base, perspective, budget, *,
               extra_us=None, jitter_px=0.0):
    if not math.isfinite(jitter_px) or jitter_px < 0.0:
        core.fail('OUTLINE_JITTER_NUMERICS',
                  'Jitter amplitude must be finite and nonnegative')
    knots = sorted({lo, hi} | {u for u in _mandatory_knots(stroke, profile, d0, d1)
                              if lo < u < hi}
                   | {u for u in (extra_us or ()) if lo < u < hi})
    if len(knots) > budget:
        core.fail('OUTLINE_SAMPLE_LIMIT', 'One source edge exceeds 2048 samples')
    output = [knots[0]]
    def depth(u):
        return d0 + u * (d1 - d0)
    for left, right in zip(knots, knots[1:]):
        stack = [(left, right, 0)]
        while stack:
            a, b, level = stack.pop()
            da, db = depth(a), depth(b)
            bound = _width_error_bound(stroke, profile, a, b, da, db, base, perspective)
            if not math.isfinite(bound):
                core.fail('OUTLINE_WIDTH_NUMERICS', 'Width error bound is nonfinite')
            offset_bound = (_jitter_error_bound(a, b, da, db, jitter_px, perspective)
                            if jitter_px else 0.0)
            if bound <= WIDTH_ERROR_PX and offset_bound <= WIDTH_ERROR_PX:
                output.append(b)
                if len(output) > budget:
                    core.fail('OUTLINE_SAMPLE_LIMIT', 'One source edge exceeds 2048 samples')
            else:
                mid = screen_to_u(0.5, a, b, da, db, perspective)
                if level >= 64 or not a < mid < b:
                    if (jitter_px and offset_bound > WIDTH_ERROR_PX
                            and bound <= WIDTH_ERROR_PX):
                        core.fail('OUTLINE_JITTER_NUMERICS',
                                  'Cannot meet jitter interpolation error bound')
                    core.fail('OUTLINE_WIDTH_NUMERICS', 'Cannot meet width interpolation error bound')
                stack.append((mid, b, level + 1))
                stack.append((a, mid, level + 1))
                if len(stack) + len(output) > budget:
                    core.fail('OUTLINE_SAMPLE_LIMIT', 'One source edge exceeds 2048 samples')
    return output


def _jitter_edge_frame(a, b, camera):
    """Positive-depth reference frame and its screen-linear grid definition.

    The reference is the full physical edge intersected with the positive
    near/far depth slab. Its screen-linear grid has ``steps + 1`` nodes, but
    callers enumerate only the integer cells covering a visible interval.
    """
    reference = _positive_depth_reference(a, b, camera)
    if reference is None:
        return None
    try:
        pa = core.project(core.lerp(a, b, reference['r0']), camera)
        pb = core.project(core.lerp(a, b, reference['r1']), camera)
    except (ArithmeticError, ValueError):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference projection is undefined')
    if any(not math.isfinite(component) for component in (*pa, *pb)):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference projection is nonfinite')
    tangent = core.sub(pb[:2], pa[:2])
    screen_length = core.length(tangent)
    if not math.isfinite(screen_length):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference screen length is nonfinite')
    if screen_length == 0.0:
        return None
    if screen_length < 1e-7:
        return None
    direction = core.mul(tangent, 1.0 / screen_length)
    normal = (-direction[1], direction[0])
    steps = max(8, int(math.ceil(screen_length / 8.0)))
    if steps > _MAX_REFERENCE_STEPS:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference screen grid exceeds the reliable index domain')
    span = reference['r1'] - reference['r0']
    depth_ratio = (min(reference['d_r0'], reference['d_r1'])
                   / max(reference['d_r0'], reference['d_r1'])
                   if reference['perspective'] else 1.0)
    source_step = span * depth_ratio / steps
    resolvable = 2.0 * max(math.ulp(reference['r0']), math.ulp(reference['r1']),
                           math.ulp(span))
    if not math.isfinite(source_step) or source_step <= resolvable:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference grid is not numerically distinguishable')
    reference['steps'] = steps
    reference['screen_length_px'] = screen_length
    return normal, reference


def _reference_t_exact(value, r0, r1, d_r0, d_r1, perspective):
    """Exact rational reference t from binary-float inputs."""
    u = _exact_fraction(value, 'Jitter reference source-u')
    if perspective:
        before, after = u - r0, r1 - u
        denominator = after * d_r0 + before * d_r1
        if denominator <= 0:
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference perspective denominator is invalid')
        return before * d_r1 / denominator
    return (u - r0) / (r1 - r0)


def _reference_t_fraction(u, reference):
    """Return exact rational t without float lambda or 1-lambda cancellation."""
    r0 = _exact_fraction(reference['r0'], 'Jitter reference r0')
    r1 = _exact_fraction(reference['r1'], 'Jitter reference r1')
    d_r0 = _exact_fraction(reference['d_r0'], 'Jitter reference d_r0')
    d_r1 = _exact_fraction(reference['d_r1'], 'Jitter reference d_r1')
    if not r1 > r0 or d_r0 <= 0 or d_r1 <= 0:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference interval or depths are invalid')
    return _reference_t_exact(u, r0, r1, d_r0, d_r1,
                              reference['perspective'])


def _reference_t(u, reference):
    """Diagnostic float view of the exact reference t."""
    try:
        value = float(_reference_t_fraction(u, reference))
    except OverflowError:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference screen parameter is nonfinite')
    if not math.isfinite(value):
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference screen parameter is nonfinite')
    return value


def _reference_scaled_t(u, reference):
    """Exact N*t, used for ceil/floor without an epsilon window."""
    steps = reference.get('steps')
    if type(steps) is not int or steps <= 0:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference steps must be a positive integer')
    return _reference_t_fraction(u, reference) * steps


def _floor_fraction(value):
    return value.numerator // value.denominator


def _ceil_fraction(value):
    return -((-value.numerator) // value.denominator)


def _visible_reference_us(reference, lo, hi, budget):
    """Enumerate only exact reference-grid cells covering [lo, hi]."""
    steps = reference.get('steps')
    if type(steps) is not int or steps <= 0:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference steps must be a positive integer')
    scaled_lo = _reference_scaled_t(lo, reference)
    scaled_hi = _reference_scaled_t(hi, reference)
    low, high = sorted((scaled_lo, scaled_hi))
    # Strict source-u filtering maps to strict integer bounds on i/N. Endpoints
    # are supplied separately by _sample_us, so no boundary cell is pre-counted.
    first = max(0, _floor_fraction(low) + 1)
    last = min(steps, _ceil_fraction(high) - 1)
    if first > last:
        return []
    candidate_count = last - first + 1
    if candidate_count > budget:
        core.fail('OUTLINE_SAMPLE_LIMIT',
                  'Visible jitter reference grid exceeds 2048 samples')

    r0 = _exact_fraction(reference['r0'], 'Jitter reference r0')
    r1 = _exact_fraction(reference['r1'], 'Jitter reference r1')
    d_r0 = _exact_fraction(reference['d_r0'], 'Jitter reference d_r0')
    d_r1 = _exact_fraction(reference['d_r1'], 'Jitter reference d_r1')
    if not r1 > r0 or d_r0 <= 0 or d_r1 <= 0:
        core.fail('OUTLINE_SAMPLE_NUMERICS',
                  'Jitter reference interval or depths are invalid')
    perspective = reference['perspective']
    lo_exact = _exact_fraction(lo, 'Jitter visible lo')
    hi_exact = _exact_fraction(hi, 'Jitter visible hi')
    result, previous_exact, previous_float = [], None, None
    for index in range(first, last + 1):
        if perspective:
            denominator = (steps - index) * d_r1 + index * d_r0
            if denominator <= 0:
                core.fail('OUTLINE_SAMPLE_NUMERICS',
                          'Jitter reference grid denominator is invalid')
            local = (index * d_r0) / denominator
        else:
            local = Fraction(index, steps)
        exact_u = r0 + (r1 - r0) * local
        if previous_exact is not None and not exact_u > previous_exact:
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid nodes are not ordered')
        try:
            u = float(exact_u)
        except OverflowError:
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid maps to nonfinite source-u')
        if not math.isfinite(u):
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid maps to nonfinite source-u')
        if previous_float is not None and not u > previous_float:
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid nodes are not distinguishable')

        # The rounded sample must still be unambiguously nearest to its cell.
        back = _reference_t_exact(u, r0, r1, d_r0, d_r1, perspective) * steps
        if index > 0 and abs(back - (index - 1)) <= abs(back - index):
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid maps back to the wrong cell')
        if index < steps and abs(back - (index + 1)) <= abs(back - index):
            core.fail('OUTLINE_SAMPLE_NUMERICS',
                      'Jitter reference grid maps back to the wrong cell')
        if lo_exact < exact_u < hi_exact:
            result.append(u)
        previous_exact, previous_float = exact_u, u
    return result


def fragment_reference_frame(fragment, camera):
    """Return the fixed unjittered screen direction/normal for one fragment.

    Positive-jitter plans carry the complete positive-depth edge reference,
    shared by all mask/dash/width runs. Prefer that validated metadata. The
    source_world endpoint fallback remains for older manually built fragments.
    The helper never consults jittered ``screen``/``world`` data.
    """
    samples = fragment.get('samples') if isinstance(fragment, dict) else None
    if not isinstance(samples, list) or len(samples) < 2:
        core.fail('INVALID_RIBBON_INPUT', 'A fragment with at least two samples is required')
    if 'jitter_reference_frame' in fragment:
        reference = fragment['jitter_reference_frame']
        if (not isinstance(reference, dict)
                or set(reference) != {'direction', 'normal'}):
            core.fail('INVALID_RIBBON_INPUT', 'Jitter reference frame fields are invalid')
        vectors = {}
        for name in ('direction', 'normal'):
            value = reference[name]
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or any(type(component) not in (int, float)
                           or not math.isfinite(component) for component in value)
                    or abs(math.hypot(*value) - 1.0) > 1e-9):
                core.fail('INVALID_RIBBON_INPUT',
                          'Jitter reference ' + name + ' must be a finite unit 2D vector')
            vectors[name] = tuple(value)
        direction, normal = vectors['direction'], vectors['normal']
        if (abs(core.dot(direction, normal)) > 1e-9
                or direction[0] * normal[1] - direction[1] * normal[0] <= 0.0):
            core.fail('INVALID_RIBBON_INPUT',
                      'Jitter reference frame must be an orthogonal right-handed pair')
        return vectors
    endpoints = (samples[0], samples[-1])
    if any(not isinstance(item, dict) or 'source_world' not in item for item in endpoints):
        core.fail('INVALID_RIBBON_INPUT', 'Fragment endpoints require unjittered source_world')
    pa = core.project(endpoints[0]['source_world'], camera)
    pb = core.project(endpoints[1]['source_world'], camera)
    tangent = core.sub(pb[:2], pa[:2])
    if core.length(tangent) < 1e-7:
        core.fail('INVALID_RIBBON_INPUT', 'Fragment source endpoints have zero screen length')
    direction = core.unit(tangent)
    return {'direction': direction, 'normal': (-direction[1], direction[0])}


def _local_overrides(source, settings):
    by_id = {edge['edge_id']: edge for edge in source['edges']}
    result = {}
    for override in settings.get('edge_overrides', []):
        if override['object_id'] != source['object_id']:
            continue
        edge_id = override['edge_id']
        edge = by_id.get(edge_id)
        if (override['mesh_id'] != source['mesh_id'] or edge is None
                or override['point_ids'] != sorted(edge['vertices'])):
            core.fail('OUTLINE_OVERRIDE_IDENTITY', 'Override differs from captured source identity',
                      '$.lines.edge_overrides')
        if edge_id in result:
            core.fail('OUTLINE_OVERRIDE_DUPLICATE', 'Repeated physical source edge override',
                      '$.lines.edge_overrides')
        previous = -1.0
        for item in override['visible_intervals']:
            if not (0 <= item['start'] < item['end'] <= 1 and item['start'] > previous):
                core.fail('OUTLINE_OVERRIDE_INTERVAL', 'Masks must be positive and strictly separated',
                          '$.lines.edge_overrides')
            previous = item['end']
        result[edge_id] = override
    return result


def _mask_neutral(override):
    return (override is None or (override['enabled'] and override['visible_intervals'] ==
                                [{'start': 0.0, 'end': 1.0}]))


def build_plan_classified(source, camera, settings, time_value, classified, *,
                          allow_styled_jitter=False):
    """Return legacy-shaped plan, or None for exact legacy fallback.

    Selected classes have already been assigned to their sole group owner.
    Local overrides never re-enable an unselected edge. Unaffected neutral edges
    retain legacy joins, while every styled edge/run carries stroke_style.
    """
    stroke = settings.get('stroke')
    if stroke is None:
        return None
    if settings['jitter_px'] != 0 and not allow_styled_jitter:
        core.fail('OUTLINE_STROKE_JITTER_UNQUALIFIED', 'The 0.3 stroke slice requires jitter_px=0')
    if not math.isfinite(settings['jitter_px']) or settings['jitter_px'] < 0.0:
        core.fail('OUTLINE_JITTER_NUMERICS',
                  'Jitter amplitude must be finite and nonnegative')
    overrides = _local_overrides(source, settings)
    styled, neutral = [], []
    for edge in classified:
        override = overrides.get(edge['key'][2])
        profile = override['width_profile'] if override else stroke['width_profile']
        (neutral if settings['jitter_px'] == 0 and is_neutral(stroke, profile)
         and _mask_neutral(override) else styled).append(edge)
    if not styled:
        return None
    if settings['backend'] != 'screen_ribbon':
        core.fail('UNSUPPORTED_OUTLINE_BACKEND', 'Styled strokes require screen_ribbon')
    jittered = settings['jitter_px'] > 0
    legacy_settings = {key: value for key, value in settings.items()
                       if key not in ('stroke', 'edge_overrides')}
    plan = core._build_plan_classified(source, camera, legacy_settings, time_value, neutral,
                                       allow_styled_jitter=allow_styled_jitter)
    plan['classified_edges'] = classified
    width = plan['width']
    if not math.isfinite(width):
        core.fail('OUTLINE_WIDTH_NUMERICS', 'Resolved pixel width is nonfinite')
    for edge in styled:
        if not settings['enabled'] or not edge['selected_classes']:
            continue
        override = overrides.get(edge['key'][2])
        profile = override['width_profile'] if override else stroke['width_profile']
        if override and not override['enabled']:
            plan['discarded'].append({'key': edge['key'], 'reason': 'edge_override_disabled'})
            continue
        masks = override['visible_intervals'] if override else [{'start': 0.0, 'end': 1.0}]
        a, b = (source['positions'][str(pid)] for pid in sorted(edge['vertices']))
        d0, d1 = -core.to_camera(a, camera)[2], -core.to_camera(b, camera)[2]
        guard = width * max(point['scale'] for point in profile) + (
            settings['jitter_px'] + 1.0 if jittered else 1.0)
        if not math.isfinite(guard):
            core.fail('OUTLINE_WIDTH_NUMERICS', 'Stroke guard is nonfinite')
        clip = core.clip_segment(a, b, camera, guard)
        if clip is None:
            plan['discarded'].append({'key': edge['key'], 'reason': 'outside_guarded_camera_frustum'})
            continue
        jitter_frame = _jitter_edge_frame(a, b, camera) if jittered else None
        if jittered and jitter_frame is None:
            plan['discarded'].append({'key': edge['key'], 'reason': 'zero_projected_length'})
            continue
        jitter_normal, jitter_reference = (
            jitter_frame if jitter_frame is not None else (None, None))
        native_reference_frame = (
            {'direction': (jitter_normal[1], -jitter_normal[0]), 'normal': jitter_normal}
            if jittered else None)
        sample_count, produced = 0, 0
        for mask_index, mask in enumerate(masks):
            for dash_lo, dash_hi, ordinal in dash_intervals(stroke['dash']):
                lo, hi = max(clip[0], mask['start'], dash_lo), min(clip[1], mask['end'], dash_hi)
                if hi <= lo:
                    continue
                remaining = MAX_SAMPLES - sample_count
                extra_us = (_visible_reference_us(jitter_reference, lo, hi, remaining)
                            if jittered else None)
                us = _sample_us(stroke, profile, lo, hi, d0, d1, width,
                                camera['type'] == 'PERSP', remaining,
                                extra_us=extra_us,
                                jitter_px=settings['jitter_px'] if jittered else 0.0)
                sample_count += len(us)
                samples = []
                for u in us:
                    original = core.lerp(a, b, u)
                    px, py, depth = core.project(original, camera)
                    biased = depth - settings['depth_bias']
                    if biased < camera['near'] - 1e-8:
                        core.fail('OUTLINE_BIAS_CROSSES_NEAR', 'Depth bias crosses actual near plane')
                    if jittered:
                        offset = core.style_offset(edge['key'], u, time_value, settings)
                        screen = core.add((px, py), core.mul(jitter_normal, offset))
                        world = core.unproject(screen[0], screen[1], biased, camera)
                        samples.append({'source_u': u, 'world': list(world),
                                        'source_world': list(original), 'screen': list(screen),
                                        'depth': biased, 'offset_px': offset,
                                        'half_width_px': width_at(stroke, profile, u, depth, width) / 2.0})
                        if any(not math.isfinite(value) for value in (
                                *screen, *world, biased, offset,
                                samples[-1]['half_width_px'])):
                            core.fail('OUTLINE_JITTER_NUMERICS',
                                      'Jitter sample contains a nonfinite value')
                    else:
                        samples.append({'source_u': u, 'world': list(core.unproject(px, py, biased, camera)),
                                        'source_world': list(original), 'screen': [px, py],
                                        'depth': biased, 'offset_px': 0.0,
                                        'half_width_px': width_at(stroke, profile, u, depth, width) / 2.0})
                    # A computed fade threshold is an exact semantic zero even
                    # if world interpolation rounds depth by one floating ULP.
                    fade = stroke['depth_width_fade']
                    if (fade['enabled'] and d1 != d0
                            and u == (fade['end_depth'] - d0) / (d1 - d0)):
                        samples[-1]['half_width_px'] = 0.0
                if core.length(core.sub(samples[-1]['screen'], samples[0]['screen'])) < 1e-7:
                    plan['discarded'].append({'key': edge['key'], 'source_interval': [lo, hi],
                                              'reason': 'zero_projected_length'})
                    continue
                # Split at zero-width samples and platforms; no caps or bridges at a tip.
                runs, active = [], []
                for left, right in zip(samples, samples[1:]):
                    if left['half_width_px'] == right['half_width_px'] == 0.0:
                        if active:
                            runs.append(active)
                            active = []
                        continue
                    if not active:
                        active = [left]
                    active.append(right)
                    if right['half_width_px'] == 0.0:
                        runs.append(active)
                        active = []
                if active:
                    runs.append(active)
                if not runs:
                    plan['discarded'].append({'key': edge['key'], 'source_interval': [lo, hi],
                                              'reason': 'zero_width_interval'})
                for run_index, values in enumerate(runs):
                    start, end = values[0]['source_u'], values[-1]['source_u']
                    start_reason = ('zero_width' if values[0]['half_width_px'] == 0.0 else
                                    'frustum' if start == clip[0] and clip[0] > 0.0 else
                                    'dash' if start == dash_lo and dash_lo > 0.0 else
                                    'mask' if start == mask['start'] and mask['start'] > 0.0 else 'source')
                    end_reason = ('zero_width' if values[-1]['half_width_px'] == 0.0 else
                                  'frustum' if end == clip[1] and clip[1] < 1.0 else
                                  'dash' if end == dash_hi and dash_hi < 1.0 else
                                  'mask' if end == mask['end'] and mask['end'] < 1.0 else 'source')
                    style = {'cap': stroke['cap'],
                             'start_cap': 'butt' if start_reason in ('frustum', 'zero_width') else stroke['cap'],
                             'end_cap': 'butt' if end_reason in ('frustum', 'zero_width') else stroke['cap']}
                    fragment_id = {'dash_ordinal': ordinal, 'mask_ordinal': mask_index,
                                   'run_ordinal': run_index}
                    fragment = {'source_key': edge['key'], 'source_interval': [start, end],
                                'classes': edge['selected_classes'], 'samples': values,
                                'fragment_id': fragment_id, 'endpoint_reasons': [start_reason, end_reason]}
                    if jittered:
                        fragment['jitter_reference_frame'] = dict(native_reference_frame)
                    plan['fragments'].append(fragment)
                    item = {'key': edge['key'], 'source_interval': [start, end], 'reversed': False,
                            'classes': edge['selected_classes']}
                    chain = {'samples': values, 'sources': [item], 'closed': False,
                             'stroke_style': style}
                    if jittered:
                        chain['jitter_reference_frame'] = dict(native_reference_frame)
                    plan['chains'].append(chain)
                    produced += 1
        if not produced and not masks:
            plan['discarded'].append({'key': edge['key'], 'reason': 'empty_visible_intervals'})
        elif not produced and not dash_intervals(stroke['dash']):
            plan['discarded'].append({'key': edge['key'], 'reason': 'dash_duty_zero'})
    return plan


def ribbon_mesh(samples, cap='butt', start_cap=None, end_cap=None,
                reference_direction=None, reference_normal=None):
    """Variable-width straight-edge strip and endpoint caps, legacy mesh shape.

    All samples belong to one projected source edge. Square/round caps hold
    endpoint depth and never extrapolate source-u. Zero radius caps add no
    geometry. Round chord error is at most CAP_ERROR_PX. When the optional
    unjittered reference frame is supplied, both width and caps use it instead
    of the jittered sample chord; omitting both preserves the legacy result.
    """
    if len(samples) < 2 or cap not in ('butt', 'square', 'round'):
        core.fail('INVALID_RIBBON_INPUT', 'Invalid styled stroke samples or cap')
    caps = (start_cap or cap, end_cap or cap)
    if any(value not in ('butt', 'square', 'round') for value in caps):
        core.fail('INVALID_RIBBON_INPUT', 'Invalid styled endpoint cap')
    vertices, triangles = [], []
    def triangle(a, b, c, depths):
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if cross == 0.0:
            return
        if len(vertices) + 3 > MAX_VERTICES:
            core.fail('OUTLINE_VERTEX_LIMIT', 'Styled stroke exceeds the vertex budget')
        base = len(vertices)
        vertices.extend([[p[0], p[1], depth] for p, depth in zip((a, b, c), depths)])
        triangles.append([base, base + 1, base + 2])
    if reference_direction is None and reference_normal is None:
        direction = core.unit(core.sub(samples[-1]['screen'], samples[0]['screen']))
        normal = (-direction[1], direction[0])
    else:
        def reference_unit(value, label):
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or any(type(component) not in (int, float)
                           or not math.isfinite(component) for component in value)):
                core.fail('INVALID_RIBBON_INPUT', label + ' must be a finite 2D vector')
            if math.hypot(value[0], value[1]) < 1e-12:
                core.fail('INVALID_RIBBON_INPUT', label + ' must be nonzero')
            return core.unit(tuple(value))
        direction = (reference_unit(reference_direction, 'reference_direction')
                     if reference_direction is not None else None)
        normal = (reference_unit(reference_normal, 'reference_normal')
                  if reference_normal is not None else None)
        if normal is None:
            normal = (-direction[1], direction[0])
        if direction is None:
            direction = (normal[1], -normal[0])
        if (abs(core.dot(direction, normal)) > 1e-9
                or direction[0] * normal[1] - direction[1] * normal[0] <= 0):
            core.fail('INVALID_RIBBON_INPUT',
                      'reference_direction and reference_normal must be a right-handed pair')
    for sample in samples:
        if not math.isfinite(sample['half_width_px']) or sample['half_width_px'] < 0:
            core.fail('INVALID_RIBBON_INPUT', 'Half widths must be finite and nonnegative')
    for a, b in zip(samples, samples[1:]):
        if core.length(core.sub(b['screen'], a['screen'])) < 1e-12:
            core.fail('DEGENERATE_SCREEN_SEGMENT', 'Styled segment projects to zero length')
        al = core.add(a['screen'], core.mul(normal, a['half_width_px']))
        ar = core.sub(a['screen'], core.mul(normal, a['half_width_px']))
        bl = core.add(b['screen'], core.mul(normal, b['half_width_px']))
        br = core.sub(b['screen'], core.mul(normal, b['half_width_px']))
        prior_triangles = len(triangles)
        triangle(al, ar, br, (a['depth'], a['depth'], b['depth']))
        triangle(al, br, bl, (a['depth'], b['depth'], b['depth']))
        if len(triangles) == prior_triangles and max(a['half_width_px'], b['half_width_px']) > 0.0:
            core.fail('DEGENERATE_STROKE_GEOMETRY', 'A positive-width span has no representable triangle')
    for index, endpoint in enumerate((samples[0], samples[-1])):
        radius, center, depth = endpoint['half_width_px'], endpoint['screen'], endpoint['depth']
        if radius == 0.0 or caps[index] == 'butt':
            continue
        outward = core.mul(direction, -1.0 if index == 0 else 1.0)
        left, right = core.add(center, core.mul(normal, radius)), core.sub(center, core.mul(normal, radius))
        if caps[index] == 'square':
            outer_left = core.add(left, core.mul(outward, radius))
            outer_right = core.add(right, core.mul(outward, radius))
            triangle(left, right, outer_right, (depth,) * 3)
            triangle(left, outer_right, outer_left, (depth,) * 3)
        else:
            # asin avoids catastrophic cancellation of acos(1-epsilon/r).
            angle = 2.0 * math.asin(math.sqrt(min(1.0, CAP_ERROR_PX / (2.0 * radius))))
            count = max(2, math.ceil(math.pi / (2.0 * angle)))
            if count > MAX_SAMPLES:
                core.fail('OUTLINE_CAP_SAMPLE_LIMIT', 'Round cap exceeds 2048 arcs at .01px error')
            for i in range(count):
                def arc(t):
                    return core.add(center, core.mul(core.add(core.mul(normal, math.cos(t)),
                                        core.mul(outward, math.sin(t))), radius))
                triangle(center, arc(math.pi * i / count), arc(math.pi * (i + 1) / count), (depth,) * 3)
    return {'vertices': vertices, 'triangles': triangles, 'joins': [], 'closed': False,
            'caps': cap if caps[0] == caps[1] else list(caps)}
