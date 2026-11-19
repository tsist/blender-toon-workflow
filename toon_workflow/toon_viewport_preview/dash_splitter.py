# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded source interval descriptors for autonomous screen-pixel dashes.

Pure NumPy/math only. No bpy, I/O, raster, attribute resampling or topology
inference. Solid returns the original complete records object by identity.
"""
from dataclasses import dataclass
import math
import numpy as np


class DashInputError(ValueError):
    pass


class DashBudgetError(DashInputError):
    pass


class DashPrecisionError(DashInputError):
    pass


@dataclass(frozen=True)
class Budget:
    max_sources: int = 100_000
    max_periods_per_source: int = 4_096
    max_periods_total: int = 1_000_000
    max_fragments_total: int = 1_000_000
    max_period_index: int = 2**40


@dataclass(frozen=True)
class DashSlices:
    original: object
    row_indices: np.ndarray
    source_u: np.ndarray
    screen_q: np.ndarray
    arc_px: np.ndarray


def _need(condition, message):
    if not condition:
        raise DashInputError(message)


def _scalar(value, name):
    _need(not isinstance(value, (bool, np.bool_)) and
          isinstance(value, (int, float, np.integer, np.floating)), name+' must be numeric')
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise DashInputError(name+' is outside binary64') from error
    _need(math.isfinite(result), name+' must be finite')
    return result


def _budget(value):
    value = Budget() if value is None else value
    _need(type(value) is Budget, 'Budget required')
    for name, upper in vars(Budget()).items():
        here = getattr(value, name)
        _need(type(here) is int and 1 <= here <= upper, 'budget may only tighten '+name)
    return value


def _pattern(value):
    if value is None:
        return None
    _need(type(value) is dict, 'explicit pattern dictionary required')
    if value == {'mode': 'SOLID'}:
        return None
    _need(set(value) == {'mode', 'dash_length_px', 'gap_length_px'} and value['mode'] == 'DASHED',
          'pattern must be SOLID or exact DASHED fields')
    dash = _scalar(value['dash_length_px'], 'dash_length_px')
    gap = _scalar(value['gap_length_px'], 'gap_length_px')
    _need(dash > 0 and gap >= 0, 'positive dash and nonnegative gap required')
    period = dash + gap
    _need(math.isfinite(period), 'finite dash period required')
    if gap == 0:
        return None
    if period <= dash:
        raise DashPrecisionError('positive gap vanished from binary64 period')
    return dash, period


def _array(value, shape, name):
    _need(isinstance(value, np.ndarray) and value.dtype.kind in 'iuf' and value.shape == shape,
          name+' must be a real NumPy array of shape '+str(shape))
    _need(bool(np.isfinite(value).all()), name+' must be finite')
    return value.astype(np.float64, copy=False)


def arc_spans_from_policy(policy_plan, *, budget=None, checkpoint=lambda: None):
    """Reaccumulate existing measured lengths in final policy traversal order.

    No normalized phase is used, no coordinate is welded, no gap is measured.
    Returns {source_id: [s(endpoint0), s(endpoint1)]}, and totals by chain ID.
    """
    limit = _budget(budget)
    _need(callable(checkpoint), 'callable checkpoint required')
    _need(type(policy_plan) is dict, 'policy plan required')
    chains, spans = policy_plan.get('chains'), policy_plan.get('source_spans')
    _need(type(chains) is list and type(spans) is list, 'policy chains and source spans required')
    if len(spans) > limit.max_sources or len(chains) > limit.max_sources:
        raise DashBudgetError('policy source/chain budget')
    lengths = {}
    for index, row in enumerate(spans):
        if index % 256 == 0:checkpoint()
        sid = row['source_id']
        _need(type(sid) is str and sid not in lengths, 'unique policy source IDs required')
        value = _scalar(row['length'], 'policy source length')
        _need(value >= 0, 'nonnegative policy source length required')
        lengths[sid] = value
    result, totals, seen = {}, {}, set()
    for chain in chains:
        checkpoint()
        cid = chain['chain_id'];steps = chain['ordered_sources']
        _need(type(cid) is str and cid not in totals and type(steps) is list, 'unique chains required')
        prefix = 0.
        for index, step in enumerate(steps):
            if index % 256 == 0:checkpoint()
            sid, orientation = step['source_id'], step['orientation']
            _need(sid in lengths and sid not in seen and orientation in ('forward', 'reverse'),
                  'exact conserved source traversal required')
            seen.add(sid);length = lengths[sid];end = prefix + length
            if not math.isfinite(end) or length > 0 and end == prefix:
                raise DashPrecisionError('positive source length cannot advance finite prefix')
            result[sid] = np.array([prefix, end] if orientation == 'forward' else [end, prefix], np.float64)
            prefix = end
        totals[cid] = prefix
    _need(seen == set(lengths), 'policy source set differs from traversal')
    checkpoint()
    return result, totals


def split_records(records, arc_px, clip_w, pattern=None, *, budget=None):
    """Intersect existing original-source intervals with full-chain dash cells.

    ``records['source_u']`` is N x 2 in original, unrenormalized source u.
    arc_px and clip_w are full-source endpoint values, never clipped values.
    Other records/arrays are opaque and retained through DashSlices.original.
    """
    parsed = _pattern(pattern)
    if parsed is None:
        return records
    limit = _budget(budget);dash, period = parsed
    _need(type(records) is dict and isinstance(records.get('source_u'), np.ndarray), 'source_u records required')
    n = len(records['source_u'])
    if n > limit.max_sources:
        raise DashBudgetError('source count budget')
    intervals = _array(records['source_u'], (n, 2), 'source_u')
    arc = _array(arc_px, (n, 2), 'arc_px')
    weights = _array(clip_w, (n, 2), 'clip_w')
    _need(bool(np.all((intervals >= 0) & (intervals <= 1))) and
          bool(np.all(intervals[:, 0] <= intervals[:, 1])), 'ordered original source_u in [0,1] required')
    _need(bool(np.all(weights > 0)), 'complete positive-w source required')
    rows, out_u, out_q, out_s = [], [], [], []
    periods_used = 0

    def emit(row, us, qs, ss):
        if len(rows) >= limit.max_fragments_total:
            raise DashBudgetError('fragment output budget')
        rows.append(row);out_u.append(us);out_q.append(qs);out_s.append(ss)

    for row in range(n):
        ulo, uhi = map(float, intervals[row]);s0, s1 = map(float, arc[row]);w0, w1 = map(float, weights[row])
        ds = s1 - s0
        if not math.isfinite(ds):
            raise DashPrecisionError('full-source arc difference overflow')

        def q_of_u(u):
            if u == 0 or u == 1:
                return float(u)
            value = u*w1/((1-u)*w0+u*w1)
            if not 0 < value < 1:
                raise DashPrecisionError('interior source parameter collapsed in screen mapping')
            return value

        qlo, qhi = q_of_u(ulo), q_of_u(uhi)
        va = s0 if qlo == 0 else s1 if qlo == 1 else s0+ds*qlo
        vb = s0 if qhi == 0 else s1 if qhi == 1 else s0+ds*qhi
        if ds != 0 and ulo < uhi and va == vb:
            raise DashPrecisionError('positive visible arc interval collapsed')
        left, right = min(va, vb), max(va, vb)
        ratio0, ratio1 = left/period, right/period
        if not math.isfinite(ratio0) or not math.isfinite(ratio1):
            raise DashBudgetError('period index outside finite range')
        first, last = math.floor(ratio0), math.floor(ratio1)
        count = last-first+1
        if max(abs(first), abs(last)) > limit.max_period_index:
            raise DashBudgetError('period index magnitude budget')
        if count > limit.max_periods_per_source or count > limit.max_periods_total-periods_used:
            raise DashBudgetError('period enumeration budget before generation')
        periods_used += count
        if left == right:
            if left % period < dash:
                emit(row, (ulo, uhi), (qlo, qhi), (va, vb))
            continue

        def from_arc(value):
            # Preserve existing interval endpoints exactly; only new cuts use
            # inverse perspective interpolation on the original full source.
            if value == va:
                return float(ulo), float(qlo)
            if value == vb:
                return float(uhi), float(qhi)
            q = (value-s0)/ds
            if not 0 <= q <= 1:
                raise DashPrecisionError('dash boundary outside full source')
            u = q*w0/((1-q)*w1+q*w0)
            if not math.isfinite(u) or not ulo <= u <= uhi:
                raise DashPrecisionError('dash boundary outside original retained interval')
            return u, q

        fragments = []
        for ordinal in range(first, last+1):
            dash_lo = ordinal*period;dash_hi = dash_lo+dash
            if not math.isfinite(dash_lo) or not math.isfinite(dash_hi) or dash_hi <= dash_lo:
                raise DashPrecisionError('dash boundaries cannot be represented')
            lo, hi = max(left, dash_lo), min(right, dash_hi)
            if hi <= lo:
                continue
            ua, qa = from_arc(lo);ub, qb = from_arc(hi)
            if ua == ub:
                raise DashPrecisionError('positive dash interval collapsed in original source_u')
            if ua > ub:
                ua, ub, qa, qb, lo, hi = ub, ua, qb, qa, hi, lo
            fragments.append(((ua, ub), (qa, qb), (lo, hi)))
        for us, qs, ss in sorted(fragments, key=lambda value: value[0][0]):
            emit(row, us, qs, ss)
    return DashSlices(records, np.asarray(rows, np.int64), np.asarray(out_u, np.float64).reshape(-1, 2),
                      np.asarray(out_q, np.float64).reshape(-1, 2), np.asarray(out_s, np.float64).reshape(-1, 2))
