# SPDX-License-Identifier: GPL-3.0-or-later
"""Pairwise broadcast of the affine float64 triangle interval kernel.

Each row is one already-selected edge/triangle pair. Query, topology-support
exclusion, budgets, owner grouping, and final domain resolution stay with the
caller. No E*T array, projection, BLAS operation, or alternate tolerance exists
here. The ORTHO adapter passes view depth; the PERSP adapter passes reciprocal
clip-w in the same affine screen domain. Numerical helpers are imported lazily.
"""
import numpy as np


def triangle_intervals_batch(index, ids, ends, depths, intervals, stats):
    """Return ids/lo/hi/g0/g1 and stable original pair_rows after filtering.

    ids[N], ends[N,2,2], depths[N,2], intervals[N,2] describe independent pairs.
    The expression order, canonical halfplanes, strict source-depth guard and
    exact fallbacks match the original per-edge kernel. The caller bounds N.
    """
    from .analytic_visibility import (
        _cross, _fcross, _exact_cross_points, _representable_fraction, _exact_gap,
        _clip_positive, _require, UnresolvedVisibility,
    )

    ids = np.asarray(ids)
    ends = np.asarray(ends, dtype=np.float64)
    depths = np.asarray(depths, dtype=np.float64)
    intervals = np.asarray(intervals, dtype=np.float64)
    _require(ids.ndim == 1 and ids.dtype.kind in 'iu', 'Batch triangle ids must be an integer vector')
    count = len(ids)
    _require(ends.shape == (count, 2, 2) and depths.shape == (count, 2)
             and intervals.shape == (count, 2), 'Invalid analytic pair array shapes')
    _require(bool(np.isfinite(ends).all()) and bool(np.isfinite(depths).all())
             and bool(np.isfinite(intervals).all()), 'Analytic pair inputs must be finite')
    _require(bool(((ids >= 0) & (ids < len(index.xy))).all()), 'Batch triangle id is out of range')
    _require(bool((intervals[:, 1] >= intervals[:, 0]).all()), 'Batch intervals have reversed bounds')
    pair_rows = np.arange(count, dtype=np.int64)
    if not count:
        return ids, np.empty(0), np.empty(0), np.empty(0), np.empty(0), pair_rows

    tri = index.xy[ids]
    ab, ac = tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]
    det = _cross(ab, ac)
    det_error = 64*np.finfo(float).eps*(np.abs(ab[:, 0]*ac[:, 1])+np.abs(ab[:, 1]*ac[:, 0]))
    uncertain = np.flatnonzero(np.abs(det) <= det_error)
    force_exact = np.zeros(len(ids), dtype=bool)
    force_exact[uncertain] = True
    for j in uncertain:
        det[j] = _representable_fraction(_exact_cross_points(tri[j, 0], tri[j, 1], tri[j, 2]))
        stats['exact_predicate_fallbacks'] += 1
    zero = det == 0
    for j in np.flatnonzero(zero):
        if all(_exact_cross_points(ends[j, 0], ends[j, 1], p) == 0 for p in tri[j]):
            active_ends = ends[j, 0]+np.asarray(intervals[j])[:, None]*(ends[j, 1]-ends[j, 0])
            axis = int(np.argmax(np.abs(ends[j, 1]-ends[j, 0])))
            low = max(float(tri[j, :, axis].min()), float(active_ends[:, axis].min()))
            high = min(float(tri[j, :, axis].max()), float(active_ends[:, axis].max()))
            if high > low:
                if stats.get('projected_tangent_policy','reject')!='nonblocking':
                    raise UnresolvedVisibility('Projected zero-area face has positive-length collinear overlap; depth interval unresolved')
                # The renderer's ray/triangle contract rejects parallel rays.
                # An exactly edge-on triangle has no positive projected area;
                # its source edges remain present, but it is not an occluder.
                stats['projected_tangent_nonblocking']=stats.get('projected_tangent_nonblocking',0)+1
    stats['projected_zero_area_events'] += int(zero.sum())
    keep = ~zero
    ids, tri, ab, ac, det, force_exact, ends, depths, intervals, pair_rows = (
        a[keep] for a in (ids, tri, ab, ac, det, force_exact, ends, depths, intervals, pair_rows))
    count = len(ids)
    lo, hi = intervals[:, 0].copy(), intervals[:, 1].copy()
    live = np.ones(count, dtype=bool)
    unresolved = np.zeros(count, dtype=bool)
    for k in range(3):
        forward = index.vertices[ids, k] < index.vertices[ids, (k+1) % 3]
        a = np.where(forward[:, None], tri[:, k], tri[:, (k+1) % 3])
        b = np.where(forward[:, None], tri[:, (k+1) % 3], tri[:, k])
        orientation = np.where(forward, 1, -1) * np.sign(det)
        e = b-a
        values = []
        for endpoint in range(2):
            point = ends[:, endpoint]
            q = point-a
            f = _cross(e, q)
            error = 64*np.finfo(float).eps*(np.abs(e[:, 0]*q[:, 1])+np.abs(e[:, 1]*q[:, 0]))
            for j in np.flatnonzero(live & ((np.abs(f) <= error) | force_exact)):
                f[j] = _representable_fraction(_exact_cross_points(a[j], b[j], point[j]))
                stats['exact_predicate_fallbacks'] += 1
            values.append(f*orientation)
        lo, hi, ok = _clip_positive(values[0], values[1], lo, hi, unresolved=unresolved)
        live = (live & ok) | unresolved
    if np.any(unresolved):
        # The fast float path can round two equal exact intersections to
        # opposite sides of an interval (ordinary tangent/collinear contact).
        # Replay the complete three-plane clip with exact rational endpoints
        # for only the flagged pairs. An exact zero-measure piece is empty;
        # an exact positive piece that float64 cannot represent still fails.
        for j in np.flatnonzero(unresolved):
            exact = _exact_clip_interval(tri[j], ends[j], intervals[j],
                                         index.vertices[ids[j]])
            if exact is None:
                live[j] = False
                stats['exact_clip_empty_pairs'] = stats.get('exact_clip_empty_pairs', 0) + 1
                continue
            left, right = exact
            left_f, right_f = float(left), float(right)
            if not (np.isfinite(left_f) and np.isfinite(right_f) and left_f < right_f):
                raise UnresolvedVisibility('Positive exact clipped piece is not representable in float64')
            lo[j], hi[j] = left_f, right_f
            live[j] = True
            stats['exact_clip_fallbacks'] = stats.get('exact_clip_fallbacks', 0) + 1
    ids, tri, ab, ac, det, lo, hi, force_exact, ends, depths, pair_rows = (
        a[live] for a in (ids, tri, ab, ac, det, lo, hi, force_exact, ends, depths, pair_rows))
    z = index.depth[ids]
    gaps = []
    for endpoint in range(2):
        point, source_depth = ends[:, endpoint], depths[:, endpoint]
        ap = point-tri[:, 0]
        u, v = _cross(ap, ac)/det, _cross(ab, ap)/det
        da, db = z[:, 1]-z[:, 0], z[:, 2]-z[:, 0]
        terms = np.column_stack((z[:, 0]-source_depth, u*da, v*db))
        gap = terms.sum(axis=1)
        error = 256*np.finfo(float).eps*(np.abs(terms).sum(axis=1)+np.abs(z).max(axis=1))
        for j in np.flatnonzero((np.abs(gap) <= error) | force_exact):
            gap[j] = _representable_fraction(_exact_gap(tri[j], z[j], point[j], source_depth[j]))
            stats['exact_predicate_fallbacks'] += 1
        gaps.append(gap)
    g0, g1 = gaps
    d0, d1 = g0+depths[:, 0], g1+depths[:, 1]
    lo, hi, ok_near = _clip_positive(d0-index.cp['near'], d1-index.cp['near'], lo, hi)
    lo, hi, ok_far = _clip_positive(index.cp['far']-d0, index.cp['far']-d1, lo, hi)
    live = ok_near & ok_far
    if np.any(live & (g0 == 0) & (g1 == 0)):
        raise UnresolvedVisibility('Non-support face is identically at source depth over a positive interval')
    return ids[live], lo[live], hi[live], g0[live], g1[live], pair_rows[live]


def _exact_clip_interval(tri, ends, interval, vertex_ids):
    """Return exact rational clipped interval, or None when it is empty.

    ``interval`` is the already screen-clipped source interval. The three
    halfplanes use the same canonical edge orientation as the float kernel.
    This is a correctness replay for collapsed float pieces, not an alternate
    tolerance or a filter.
    """
    from fractions import Fraction
    from .analytic_visibility import _fcross

    def point(value):
        return (Fraction.from_float(float(value[0])),
                Fraction.from_float(float(value[1])))

    a, b, c = (point(value) for value in tri)
    e0, e1 = point(ends[0]), point(ends[1])
    ab = (b[0] - a[0], b[1] - a[1])
    ac = (c[0] - a[0], c[1] - a[1])
    det = _fcross(ab, ac)
    if det == 0:
        raise UnresolvedVisibility('Exact clipped pair has zero projected area')
    lo = Fraction.from_float(float(interval[0]))
    hi = Fraction.from_float(float(interval[1]))
    for k in range(3):
        forward = vertex_ids[k] < vertex_ids[(k + 1) % 3]
        left = point(tri[k]) if forward else point(tri[(k + 1) % 3])
        right = point(tri[(k + 1) % 3]) if forward else point(tri[k])
        edge = (right[0] - left[0], right[1] - left[1])
        orientation = (1 if forward else -1) * (1 if det > 0 else -1)
        values = []
        for endpoint in (e0, e1):
            q = (endpoint[0] - left[0], endpoint[1] - left[1])
            values.append(_fcross(edge, q) * orientation)
        f0, f1 = values
        if f0 < 0 and f1 < 0:
            return None
        if (f0 < 0) != (f1 < 0):
            root = f0 / (f0 - f1)
            if f0 < 0:
                lo = max(lo, root)
            else:
                hi = min(hi, root)
            if hi <= lo:
                return None
    return (lo, hi) if hi > lo else None
