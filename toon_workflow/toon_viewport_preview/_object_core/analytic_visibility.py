# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental affine screen/depth solid intervals in projected float64 space.

Original implementation. The bounded CSR broad phase and affine half-plane
design were compared with PENumbra; no upstream code or empirical filters are
included. Frozen projected float64 coordinates define the numeric input domain.
The ORTHO adapter supplies view-space depth directly; the PERSP adapter supplies
screen NDC plus a monotone affine function of reciprocal clip-w.
"""
from fractions import Fraction
import numpy as np

from .geometry import _NUMERIC_EPS, _camera_parameters, _require
from .visibility import _screen_interval, _triangle_culling
from .intervals import UnresolvedVisibility, union_intervals, subtract_intervals, domain_envelope


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _fcross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _fraction_point(p):
    return tuple(Fraction.from_float(float(v)) for v in p)


def _representable_fraction(value):
    try:
        result = float(value)
    except OverflowError as error:
        raise UnresolvedVisibility('Exact geometric coefficient exceeds float64') from error
    if not np.isfinite(result) or (value != 0 and result == 0):
        raise UnresolvedVisibility('Nonzero exact geometric coefficient is not representable in float64')
    return result


def _exact_cross_points(a, b, p):
    # These coordinate equalities prove collinearity in the frozen input
    # domain, without evaluating or relaxing an orientation tolerance.
    if ((a[0] == b[0] == p[0]) or (a[1] == b[1] == p[1])
            or (p[0] == a[0] and p[1] == a[1])
            or (p[0] == b[0] and p[1] == b[1])):
        return Fraction(0)
    a, b, p = map(_fraction_point, (a, b, p))
    return _fcross((b[0]-a[0], b[1]-a[1]), (p[0]-a[0], p[1]-a[1]))


def _exact_gap(tri_xy, tri_depth, point, source_depth):
    a, b, c = map(_fraction_point, tri_xy)
    p = _fraction_point(point)
    ab = (b[0]-a[0], b[1]-a[1])
    ac = (c[0]-a[0], c[1]-a[1])
    ap = (p[0]-a[0], p[1]-a[1])
    det = _fcross(ab, ac)
    if not det:
        raise UnresolvedVisibility('Zero projected area has no affine depth plane')
    z = [Fraction.from_float(float(v)) for v in tri_depth]
    return z[0]-Fraction.from_float(float(source_depth)) + _fcross(ap, ac)/det*(z[1]-z[0]) + _fcross(ab, ap)/det*(z[2]-z[0])


def _clip_positive(values0, values1, lo, hi, strict=False, unresolved=None):
    """Vectorized affine >=0 clipping; strict removes identically zero functions.

    Only endpoints of a positive measure interval are represented, so the open
    versus closed endpoint distinction does not create a line segment by itself.
    When ``unresolved`` is supplied, exact-positive pieces that collapse in
    float64 are marked there instead of raising, so the caller can replay the
    complete clipping with exact rational endpoints for only those pairs.
    """
    f0, f1 = np.asarray(values0), np.asarray(values1)
    alive = ~((f0 < 0) & (f1 < 0))
    if strict:
        alive &= ~((f0 == 0) & (f1 == 0))
    crossing = (f0 < 0) != (f1 < 0)
    root = np.zeros_like(f0, dtype=np.float64)
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        divisor = f0-f1
        np.divide(f0, divisor, out=root, where=crossing)
    # If an affine endpoint is exactly zero, its root is exactly 0 or 1.
    # Rational conversion cannot add information for that identity.
    uncertain_root = crossing & (f0 != 0) & (f1 != 0)
    for j in np.flatnonzero(uncertain_root & (~np.isfinite(divisor) | ~np.isfinite(root) | (root<=0) | (root>=1))):
        exact = Fraction.from_float(float(f0[j])) / (Fraction.from_float(float(f0[j]))-Fraction.from_float(float(f1[j])))
        root[j] = float(exact)
        if 0 < exact < 1 and not 0 < root[j] < 1:
            raise UnresolvedVisibility('Positive clipping interval cannot be represented in float64')
    new_lo = np.where(crossing & (f0 < 0), np.maximum(lo, root), lo)
    new_hi = np.where(crossing & (f1 < 0), np.minimum(hi, root), hi)
    bad_positive = np.zeros(len(f0), dtype=bool)
    for j in np.flatnonzero(uncertain_root & alive & (new_hi<=new_lo)):
        exact = Fraction.from_float(float(f0[j])) / (Fraction.from_float(float(f0[j]))-Fraction.from_float(float(f1[j])))
        left, right = Fraction.from_float(float(lo[j])), Fraction.from_float(float(hi[j]))
        if f0[j]<0: left=max(left,exact)
        else: right=min(right,exact)
        if right>left:
            if unresolved is None:
                raise UnresolvedVisibility('Positive clipped piece collapsed in float64')
            bad_positive[j] = True
    if unresolved is not None:
        unresolved |= bad_positive
    lo, hi = new_lo, new_hi
    alive &= hi > lo
    if unresolved is not None:
        # Keep flagged pairs in the caller's live set until the exact replay
        # either restores a representable positive interval or proves it empty.
        alive |= unresolved
    return lo, hi, alive


class _ProjectedIndex:
    def __init__(self, objects, cp, margin, merge, grid_px, max_refs, stats):
        self.cp, self.grid_px = cp, grid_px
        self._query_cache = {}
        self._query_cache_bytes = 0
        self._query_cache_limit = 32 * 1024 * 1024
        self._query_cache_entry_limit = 4096
        self._stats = stats
        stats.update(query_cache_hits=0, query_cache_misses=0, query_cache_peak_array_bytes=0,
                     query_cache_peak_entries=0)
        self.xmin, self.ymin = -margin, -margin
        self.xmax, self.ymax = cp['width']+margin, cp['height']+margin
        self.nx = max(1, int(np.ceil((self.xmax-self.xmin)/grid_px)))
        self.ny = max(1, int(np.ceil((self.ymax-self.ymin)/grid_px)))
        _require(self.nx*self.ny <= max_refs, 'Analytic grid cell budget exceeded')
        all_xy, all_depth, all_vertices, all_domains = [], [], [], []
        all_support_vertices, all_source_objects, all_source_triangles = [], [], []
        self.offsets, self.domains, domain_ids = {}, {}, {}
        offset = 0
        for source_object_index, obj in enumerate(objects):
            oid = obj['id']
            key = ('group', obj['outline_group']) if merge and obj.get('outline_group') else ('object', oid)
            self.domains[oid] = domain_ids.setdefault(key, len(domain_ids))
            self.offsets[oid] = offset
            world = obj['world_vertices']
            homogeneous = np.column_stack((world, np.ones(len(world))))
            clip = homogeneous @ cp['view_projection'].T
            xy = (clip[:, :2]/clip[:, 3:4]+1)*[cp['width']/2, cp['height']/2]
            depth = -(homogeneous @ cp['view'].T)[:, 2]
            tri = np.asarray(obj['arrays']['triangles'])
            if len(tri):
                source_triangles = np.asarray(obj.get('index_source_triangles',
                                       np.arange(len(tri),dtype=np.int64)),dtype=np.int64)
                support_vertices = np.asarray(obj.get('index_support_vertices',tri),dtype=np.int64)
                _require(source_triangles.shape==(len(tri),) and support_vertices.shape==(len(tri),3)
                         and bool(np.all(source_triangles>=0)),
                         'Invalid index triangle/support mapping: '+str(oid))
                p = world[tri]
                n = np.cross(p[:, 1]-p[:, 0], p[:, 2]-p[:, 0])
                length = np.linalg.norm(n, axis=1)
                valid = length > 0
                front = np.zeros(len(tri), dtype=bool)
                front[valid] = (n[valid]/length[valid, None] @ cp['toward_camera']) > _NUMERIC_EPS
                culled = _triangle_culling(obj) & ~front
                stats['culled_triangles'] += int((valid & culled).sum())
                stats['world_degenerate_triangles'] += int((~valid).sum())
                keep = valid & ~culled
                all_xy.append(xy[tri[keep]])
                all_depth.append(depth[tri[keep]])
                all_vertices.append(tri[keep]+offset)
                all_support_vertices.append(support_vertices[keep]+offset)
                all_domains.append(np.full(int(keep.sum()), self.domains[oid], dtype=np.int32))
                all_source_objects.append(np.full(int(keep.sum()), source_object_index, dtype=np.int32))
                all_source_triangles.append(source_triangles[keep].astype(np.int32))
            offset += len(world)
        self.xy = np.concatenate(all_xy) if all_xy else np.empty((0, 3, 2))
        self.depth = np.concatenate(all_depth) if all_depth else np.empty((0, 3))
        self.vertices = np.concatenate(all_vertices) if all_vertices else np.empty((0, 3), dtype=np.int64)
        self.support_vertices = (np.concatenate(all_support_vertices) if all_support_vertices
                                 else np.empty((0, 3), dtype=np.int64))
        self.domain = np.concatenate(all_domains) if all_domains else np.empty(0, dtype=np.int32)
        self.source_object_index = np.concatenate(all_source_objects) if all_source_objects else np.empty(0, dtype=np.int32)
        self.source_triangle_index = np.concatenate(all_source_triangles) if all_source_triangles else np.empty(0, dtype=np.int32)
        stats['triangle_count'] = len(self.xy)
        if not len(self.xy):
            self.bounds = np.empty((0, 4))
            self.starts = np.zeros(self.nx*self.ny+1, dtype=np.int64)
            self.items = np.empty(0, dtype=np.int64)
            return
        lower = np.nextafter(self.xy.min(axis=1), -np.inf)
        upper = np.nextafter(self.xy.max(axis=1), np.inf)
        self.bounds = np.column_stack((lower, upper))
        selected = np.flatnonzero((upper[:, 0] >= self.xmin) & (lower[:, 0] <= self.xmax)
                                  & (upper[:, 1] >= self.ymin) & (lower[:, 1] <= self.ymax))
        x0 = np.clip(np.floor((lower[selected, 0]-self.xmin)/grid_px), 0, self.nx-1).astype(np.int64)
        x1 = np.clip(np.floor((upper[selected, 0]-self.xmin)/grid_px), 0, self.nx-1).astype(np.int64)
        y0 = np.clip(np.floor((lower[selected, 1]-self.ymin)/grid_px), 0, self.ny-1).astype(np.int64)
        y1 = np.clip(np.floor((upper[selected, 1]-self.ymin)/grid_px), 0, self.ny-1).astype(np.int64)
        widths = x1-x0+1
        counts = widths*(y1-y0+1)
        total = int(counts.sum())
        _require(total <= max_refs, 'Analytic triangle/grid-reference budget exceeded')
        repeated = np.repeat(np.arange(len(selected)), counts)
        relative = np.arange(total)-np.repeat(np.cumsum(counts)-counts, counts)
        cells = (y0[repeated]+relative//widths[repeated])*self.nx+x0[repeated]+relative%widths[repeated]
        order = np.argsort(cells, kind='stable')
        self.items = selected[repeated[order]]
        self.starts = np.r_[0, np.cumsum(np.bincount(cells, minlength=self.nx*self.ny))]
        stats['grid_references'] = total
        stats['grid_bytes'] = int(self.items.nbytes+self.starts.nbytes)

    def query(self, ends):
        low = np.nextafter(ends.min(axis=0), -np.inf)
        high = np.nextafter(ends.max(axis=0), np.inf)
        x0 = int(np.clip(np.floor((low[0]-self.xmin)/self.grid_px), 0, self.nx-1))
        x1 = int(np.clip(np.floor((high[0]-self.xmin)/self.grid_px), 0, self.nx-1))
        y0 = int(np.clip(np.floor((low[1]-self.ymin)/self.grid_px), 0, self.ny-1))
        y1 = int(np.clip(np.floor((high[1]-self.ymin)/self.grid_px), 0, self.ny-1))
        key = (x0, x1, y0, y1)
        ids = self._query_cache.get(key)
        if ids is None:
            self._stats['query_cache_misses'] += 1
            chunks = [self.items[self.starts[y*self.nx+x]:self.starts[y*self.nx+x+1]]
                      for y in range(y0, y1+1) for x in range(x0, x1+1)]
            ids = np.unique(np.concatenate(chunks)) if chunks else np.empty(0, dtype=np.int64)
            # Cache only sorted candidates for a cell rectangle, never an
            # edge-specific exact AABB result or support exclusion.
            if ids.nbytes <= self._query_cache_limit:
                while self._query_cache and (self._query_cache_bytes+ids.nbytes > self._query_cache_limit
                        or len(self._query_cache) >= self._query_cache_entry_limit):
                    oldest = next(iter(self._query_cache))
                    self._query_cache_bytes -= self._query_cache.pop(oldest).nbytes
                self._query_cache[key] = ids
                self._query_cache_bytes += ids.nbytes
                self._stats['query_cache_peak_array_bytes'] = max(self._stats['query_cache_peak_array_bytes'], self._query_cache_bytes)
                self._stats['query_cache_peak_entries'] = max(self._stats['query_cache_peak_entries'], len(self._query_cache))
        else:
            self._stats['query_cache_hits'] += 1
        b = self.bounds[ids]
        return ids[(b[:, 2] >= low[0]) & (b[:, 0] <= high[0])
                   & (b[:, 3] >= low[1]) & (b[:, 1] <= high[1])]


def _triangle_intervals(index, ids, ends, depths, interval, stats):
    """Single-edge adapter to the shared pair kernel, preserving result order."""
    from .analytic_batches import triangle_intervals_batch
    count = len(ids)
    result = triangle_intervals_batch(
        index, ids,
        np.broadcast_to(np.asarray(ends, dtype=np.float64), (count, 2, 2)),
        np.broadcast_to(np.asarray(depths, dtype=np.float64), (count, 2)),
        np.broadcast_to(np.asarray(interval, dtype=np.float64), (count, 2)),
        stats,
    )
    return result[:5]


def _edge_interval_batches(index, edge_records, cp, margin, max_pairs, batch_edges, stats):
    """Bounded pair batches, source order and scalar geometry-error diagnosis.

    Failures never produce accepted partial output. Query/budget lookahead and
    domain evaluation can change which error is encountered first overall.
    """
    from .analytic_batches import triangle_intervals_batch
    pending, pair_count = [], 0
    pair_limit = 250_000

    def solve(rows):
        counts = np.asarray([len(row[2]) for row in rows], dtype=np.int64)
        owners = np.repeat(np.arange(len(rows)), counts)
        ids = np.concatenate([row[2] for row in rows])
        xy = np.asarray([row[0]['xy'] for row in rows], dtype=np.float64)
        depths = np.asarray([row[0]['depth'] for row in rows], dtype=np.float64)
        intervals = np.asarray([row[1] for row in rows], dtype=np.float64)
        before = dict(stats)
        try:
            result = triangle_intervals_batch(index, ids, xy[owners], depths[owners], intervals[owners], stats)
        except UnresolvedVisibility:
            # Batched stages can encounter a later source's failure first.
            # Replay in original source order only to locate the first error;
            # no partial successful result is returned or substituted.
            stats.clear(); stats.update(before)
            for i, (edge, interval, selected_ids) in enumerate(rows):
                try:
                    _triangle_intervals(index, selected_ids, xy[i], depths[i], interval, stats)
                except UnresolvedVisibility as error:
                    error.visibility_context = dict(object_id=edge['object_id'], edge_id=edge['edge_id'],
                        stage='batched_geometry_scalar_failure_replay', interval=list(interval))
                    raise
            raise
        filtered_ids, lo, hi, g0, g1, pair_rows = result
        _require(bool(np.all(np.diff(pair_rows) > 0)), 'Pair kernel changed source order')
        result_counts = np.bincount(owners[pair_rows], minlength=len(rows))
        starts = np.r_[0, np.cumsum(result_counts)]
        try:
            flo, fhi, front = _clip_positive(-g0, -g1, lo, hi, strict=True)
        except UnresolvedVisibility:
            for i, (edge, interval, _) in enumerate(rows):
                selection = slice(int(starts[i]), int(starts[i+1]))
                try:
                    _clip_positive(-g0[selection], -g1[selection], lo[selection], hi[selection], strict=True)
                except UnresolvedVisibility as error:
                    error.visibility_context = dict(object_id=edge['object_id'], edge_id=edge['edge_id'],
                        stage='batched_forward_scalar_failure_replay', interval=list(interval))
                    raise
            raise
        stats['triangle_pair_batches'] += 1
        stats['largest_pair_batch'] = max(stats['largest_pair_batch'], len(ids))
        for i, (edge, interval, _) in enumerate(rows):
            selection = slice(int(starts[i]), int(starts[i+1]))
            yield (edge, interval, *(a[selection] for a in (filtered_ids, lo, hi, g0, g1, flo, fhi, front)))

    for edge in edge_records:
        xy = np.asarray(edge['xy'], dtype=np.float64)
        interval = _screen_interval(xy, cp['width'], cp['height'], margin)
        if interval is None or interval[1] <= interval[0]:
            stats['offscreen_rejected'] += 1
            continue
        clipped_xy = xy[0]+np.asarray(interval)[:,None]*(xy[1]-xy[0])
        ids = index.query(clipped_xy)
        stats['pairs'] += len(ids)
        _require(stats['pairs'] <= max_pairs, 'Analytic pair budget exceeded; no partial result accepted')
        pair = np.asarray(edge['vertex_ids'])+index.offsets[edge['object_id']]
        adjacent = (np.any(index.support_vertices[ids]==pair[0], axis=1)
                    & np.any(index.support_vertices[ids]==pair[1], axis=1))
        stats['support_pairs_excluded'] += int(adjacent.sum())
        ids = ids[~adjacent]
        if pending and (len(pending) >= batch_edges or pair_count+len(ids) > pair_limit):
            yield from solve(pending)
            pending, pair_count = [], 0
        if len(ids) > pair_limit:
            # Preserve the previously supported large-single-edge path instead
            # of creating a new input rejection for a batching resource limit.
            stats['oversized_scalar_edges'] += 1
            result = _triangle_intervals(index, ids, xy,
                     np.asarray(edge['depth'], dtype=np.float64), interval, stats)
            _, lo, hi, g0, g1 = result
            yield (edge, interval, *result, *_clip_positive(-g0, -g1, lo, hi, strict=True))
        else:
            pending.append((edge, interval, ids)); pair_count += len(ids)
    if pending:
        yield from solve(pending)


def visible_segments(objects, edge_records, camera, sample_step_px=.25, ray_epsilon=1e-6,
                     line_categories=None, merge_outline_groups=False, *, index_objects=None,
                     grid_px=4, max_grid_refs=20_000_000, max_pairs=10_000_000,
                     max_pieces=100_000, batch_edges=256):
    cp = _camera_parameters(camera)
    _require(line_categories is None or (isinstance(line_categories, (list, tuple)) and len(line_categories)>0
             and all(x in ('outline', 'object') for x in line_categories)), 'Invalid analytic line categories')
    _require(type(merge_outline_groups) is bool, 'merge_outline_groups must be boolean')
    _require(type(grid_px) is int and grid_px>0 and all(type(v) is int and v>0 for v in (max_grid_refs,max_pairs,max_pieces)), 'Invalid analytic budgets')
    _require(type(batch_edges) is int and batch_edges > 0, 'batch_edges must be a positive integer')
    margin = float(camera.get('margin_px', 0))
    _require(np.isfinite(margin) and margin>=0, 'Invalid analytic viewport margin')
    stats = dict(method='projected_affine_intervals_experimental', edge_count=len(edge_records),
                 projected_tangent_policy=camera.get('projected_tangent_policy','reject'),
                 triangle_count=0, grid_px=grid_px, grid_references=0, grid_bytes=0,
                 pairs=0, support_pairs_excluded=0, world_degenerate_triangles=0,
                 projected_zero_area_events=0, culled_triangles=0, exact_predicate_fallbacks=0,
                 single_domain_unions=0,
                 batch_edges=batch_edges, triangle_pair_batches=0, largest_pair_batch=0, oversized_scalar_edges=0,
                 offscreen_rejected=0, segments=0, no_physical_ray_offset=True,
                 compatibility_arguments_unused=dict(sample_step_px=sample_step_px,ray_epsilon=ray_epsilon),
                 reference_kernel_equivalence=False,
                 limitations=['Orthographic solid surfaces only',
                              'Projected float64 input; filtered rational predicates do not recover source precision',
                              'Projected collinear faces and unresolved source-depth/domain ties fail closed',
                              'No temporal AA or optical transparency'])
    index = _ProjectedIndex(objects if index_objects is None else index_objects,
                            cp, margin, merge_outline_groups, grid_px, max_grid_refs, stats)
    output = []
    for edge, interval, ids, lo, hi, g0, g1, flo, fhi, front in _edge_interval_batches(index, edge_records, cp, margin, max_pairs, batch_edges, stats):
        xy, world, depth = (np.asarray(edge[k], dtype=np.float64) for k in ('xy','world','depth'))
        blocked = union_intervals(zip(flo[front], fhi[front]), max_pieces=max_pieces)
        visible = subtract_intervals(interval, blocked, max_pieces=max_pieces)
        if not visible:
            continue
        semantic = line_categories is not None
        same_domain = index.domains[edge['object_id']]
        regions = []
        if not semantic:
            regions = [(a,b,None) for a,b in visible]
        elif edge.get('local_surface_domain_both_sides',False):
            regions = [(a,b,'object') for a,b in visible]
        else:
            blo, bhi, back = _clip_positive(g0, g1, lo, hi, strict=True)
            # The gap and hit depth have the same ordering for this source edge.
            # Only visible domains need resolving; obscured ambiguous ties do
            # not invalidate an otherwise well-defined foreground result.
            for start, end in visible:
                candidates = []
                for j in np.flatnonzero(back & (bhi>start) & (blo<end)):
                    candidates.append(dict(lo=max(start,float(blo[j])),hi=min(end,float(bhi[j])),
                                           a=float(g0[j]),b=float(g1[j]-g0[j]),id=int(ids[j]),domain=int(index.domain[ids[j]])))
                try:
                    if candidates and all(c['domain'] == candidates[0]['domain'] for c in candidates):
                        # Every possible nearest surface has the same domain.
                        # Identity crossings are irrelevant to this caller's
                        # categorical output. Do not apply this to mixed foreign
                        # domains: their surviving depth ties must still fail.
                        _require(all(np.isfinite(c['a']) and np.isfinite(c['b']) for c in candidates),
                                 'Nonfinite affine depth in single-domain coverage')
                        covered = union_intervals(((c['lo'], c['hi']) for c in candidates), max_pieces=max_pieces)
                        envelope = [dict(lo=a, hi=b, domain=candidates[0]['domain']) for a,b in covered]
                        stats['single_domain_unions'] += 1
                    else:
                        envelope = domain_envelope(candidates, max_pieces=max_pieces)
                except UnresolvedVisibility as error:
                    error.visibility_context = dict(object_id=edge['object_id'],edge_id=edge['edge_id'],
                        source_vertices=list(map(int,edge['vertex_ids'])),visible_interval=[start,end],
                        source_domain=same_domain,candidates=candidates)
                    raise
                cursor = start
                for piece in envelope:
                    if piece['lo']>cursor:
                        regions.append((cursor,piece['lo'],'outline'))
                    regions.append((piece['lo'],piece['hi'],'object' if piece['domain']==same_domain else 'outline'))
                    cursor = piece['hi']
                if cursor<end:
                    regions.append((cursor,end,'outline'))
        selected = []
        for a,b,category in regions:
            if semantic and category not in line_categories:
                continue
            if selected and selected[-1][2]==category and selected[-1][1]==a:
                selected[-1]=(selected[-1][0],b,category)
            else:
                selected.append((a,b,category))
        for a,b,category in selected:
            ts=np.asarray([a,b])
            row={k:edge[k] for k in ('object_id','edge_id','kind','vertex_ids','original_edge_id',
                                     'local_surface_domain_both_sides','local_surface_domain_side_counts') if k in edge}
            if semantic:
                row['line_type']=category
            u0,u1=float(edge.get('u0',0)),float(edge.get('u1',1))
            row.update(source=edge.get('source',(edge['object_id'],edge['edge_id'],edge['kind'])),
                       xy=xy[0]+ts[:,None]*(xy[1]-xy[0]),world=world[0]+ts[:,None]*(world[1]-world[0]),
                       depth=depth[0]+ts*(depth[1]-depth[0]),u0=u0+a*(u1-u0),u1=u0+b*(u1-u0),
                       visibility_method='projected_affine_intervals_experimental')
            output.append(row)
    stats['segments']=len(output)
    return output, stats
