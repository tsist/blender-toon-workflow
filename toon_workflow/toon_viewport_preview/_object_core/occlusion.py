# SPDX-License-Identifier: GPL-3.0-or-later
"""Raw source-u solid visibility partitions and original-triangle witnesses.

No Pencil hidden-category, layer-count, priority, style or RGBA semantics.
All geometry predicates are reused from the same-package analytic core.
"""
from __future__ import annotations

import re
import time
import numpy as np

from .contract import InputError
from .geometry import _camera_parameters, _require
from .visibility import _screen_interval
from .analytic_visibility import _ProjectedIndex, _triangle_intervals, _clip_positive
from .intervals import UnresolvedVisibility, union_intervals, subtract_intervals


def _edge_id(value):
    return ((type(value) is int and value >= 0) or
            (isinstance(value, str) and re.fullmatch(r'triangle-diagonal:[0-9]+:[0-9]+', value) is not None))


class _TriangleMembership:
    """Original vertex-to-triangle incidence; no visibility/culling filtering."""
    def __init__(self, triangles, vertex_count):
        self.triangles = np.asarray(triangles)
        vertices = self.triangles.reshape(-1)
        # The prepared geometry contract validates these original indices.
        order = np.argsort(vertices, kind='stable')
        self.rows = order // 3
        self.starts = np.r_[0, np.cumsum(np.bincount(vertices, minlength=vertex_count))]

    def contains(self, a, b):
        if self.starts[a+1]-self.starts[a] > self.starts[b+1]-self.starts[b]:
            a, b = b, a
        rows = self.rows[self.starts[a]:self.starts[a+1]]
        return bool(np.any(self.triangles[rows] == b))


def _validate_sources(objects, edges):
    _require(isinstance(objects, (list, tuple)) and isinstance(edges, (list, tuple)),
             'Objects and edge_records must be lists or tuples')
    lookup = {}
    memberships = {}
    for obj in objects:
        _require(isinstance(obj, dict), 'Prepared object must be a dictionary')
        oid = obj.get('id')
        _require(isinstance(oid, str) and bool(oid) and oid not in lookup,
                 'Occlusion object IDs must be unique nonempty strings')
        lookup[oid] = obj
        _require('world_vertices' in obj and 'arrays' in obj, 'Prepared geometry required')
        tri = np.asarray(obj['arrays']['triangles'])
        _require(len(tri) <= np.iinfo(np.int32).max, 'Original triangle ID exceeds packed int32 capacity')
        mapping = obj['arrays'].get('triangle_polygon')
        if mapping is not None:
            mapping = np.asarray(mapping)
            _require(mapping.shape == (len(tri),) and mapping.dtype.kind in 'iu' and
                     bool(((mapping >= 0) & (mapping < len(obj['arrays']['face_start']))).all()),
                     'Invalid original triangle_polygon ownership: ' + oid)
    _require(len(objects) <= np.iinfo(np.int32).max, 'Original object index exceeds packed int32 capacity')
    seen = set()
    for edge in edges:
        _require(isinstance(edge, dict), 'Source edge must be a dictionary')
        _require(all(k in edge for k in ('object_id','edge_id','original_edge_id','source','kind','vertex_ids','world','xy','depth','u0','u1')),
                 'Missing source identity or geometry')
        oid, eid = edge['object_id'], edge['edge_id']
        _require(isinstance(oid, str) and oid in lookup and _edge_id(eid), 'Unknown or invalid source object/edge ID')
        key = oid, eid
        _require(key not in seen, 'Duplicate source ID')
        seen.add(key)
        _require(edge['kind'] in ('silhouette','open'), 'Unsupported source candidate kind')
        _require(isinstance(edge['source'], (list, tuple)) and tuple(edge['source']) == (oid,eid,edge['kind']),
                 'Inconsistent source identity')
        vertices = np.asarray(edge['vertex_ids'])
        nv = len(lookup[oid]['world_vertices'])
        _require(vertices.shape == (2,) and vertices.dtype.kind in 'iu' and
                 bool(((vertices >= 0) & (vertices < nv)).all()) and vertices[0] != vertices[1],
                 'Invalid source vertex IDs')
        pair = tuple(sorted(map(int, vertices)))
        if type(eid) is int:
            original = np.asarray(lookup[oid]['arrays']['edges'])
            _require(eid < len(original) and tuple(sorted(map(int, original[eid]))) == pair,
                     'Source edge ID does not own source vertices')
            _require(edge.get('original_edge_id',eid) == eid, 'Inconsistent original_edge_id')
        else:
            _require(eid == 'triangle-diagonal:%d:%d' % pair and edge.get('original_edge_id') is None,
                     'Invalid virtual source identity')
            if oid not in memberships:
                memberships[oid] = _TriangleMembership(lookup[oid]['arrays']['triangles'], nv)
            _require(memberships[oid].contains(*pair),
                     'Virtual source pair is absent from original triangles')
        for field, shape in (('world',(2,3)),('xy',(2,2)),('depth',(2,))):
            value = np.asarray(edge[field])
            _require(value.shape == shape and value.dtype.kind in 'biuf' and bool(np.isfinite(value).all()),
                     'Invalid source ' + field)
        u0, u1 = edge['u0'], edge['u1']
        _require(not isinstance(u0,(bool,np.bool_)) and not isinstance(u1,(bool,np.bool_)) and
                 np.isscalar(u0) and np.isscalar(u1), 'Invalid original source parameter')
        try:
            finite = np.isfinite(u0) and np.isfinite(u1) and 0 <= u0 <= u1 <= 1
        except TypeError as error:
            raise InputError('Invalid original source parameter') from error
        _require(bool(finite), 'Invalid original source parameter')


def _source_span(edge, a, b):
    u0, u1 = float(edge['u0']), float(edge['u1'])
    start, end = u0+a*(u1-u0), u0+b*(u1-u0)
    if b > a and end <= start:
        raise UnresolvedVisibility('Positive original-u output interval collapsed in float64')
    return start, end


def partition_source_visibility(objects, edge_records, camera, *, grid_px=4,
                                max_grid_refs=20_000_000, max_pairs=10_000_000,
                                max_pieces=100_000):
    """Partition each current-M1 source; witnesses cover their own original-u.

    max_pieces bounds the combined returned records and witness intervals over
    the entire call. Budget/geometry failure raises without a partial result.
    The caller supplies prepared geometry and near/far-clipped source records.
    """
    _validate_sources(objects, edge_records)
    _require(type(grid_px) is int and 1 <= grid_px <= 256 and
             all(type(v) is int and v > 0 for v in (max_grid_refs,max_pairs,max_pieces)),
             'Invalid occlusion budgets')
    _require(max_grid_refs <= 20_000_000 and max_pairs <= 10_000_000 and max_pieces <= 100_000,
             'Occlusion budgets exceed v1 maximums')
    cp = _camera_parameters(camera)
    margin = float(camera.get('margin_px',0))
    _require(np.isfinite(margin) and margin >= 0, 'Invalid occlusion viewport margin')
    stats = dict(method='projected_affine_source_partition_experimental',policy='solid_binary_any_front',
        edge_count=len(edge_records),triangle_count=0,grid_px=grid_px,grid_references=0,grid_bytes=0,
        pairs=0,support_pairs_excluded=0,world_degenerate_triangles=0,projected_zero_area_events=0,
        culled_triangles=0,exact_predicate_fallbacks=0,offscreen_rejected=0,zero_measure_sources=0,
        visible_records=0,hidden_records=0,witness_intervals=0,returned_piece_budget_used=0,
        index_seconds=0.,coverage_seconds=0.,partition_seconds=0.,
        no_physical_ray_offset=True,reference_kernel_equivalence=False,
        limitations=['ORTHO opaque solid geometry assumption','No hidden category or physical layer count',
                     'Frozen float64 predicates; unresolved geometry fails closed','No optical or temporal behavior'])
    tick = time.perf_counter()
    index = _ProjectedIndex(objects,cp,margin,False,grid_px,max_grid_refs,stats)
    stats['index_seconds'] = time.perf_counter()-tick
    stats['identity_columns_bytes'] = int(index.source_object_index.nbytes+index.source_triangle_index.nbytes)
    records = []
    used = 0
    for edge in edge_records:
        if edge['u1'] == edge['u0']:
            stats['zero_measure_sources'] += 1
            continue
        xy, world, depth = (np.asarray(edge[k],dtype=np.float64) for k in ('xy','world','depth'))
        interval = _screen_interval(xy,cp['width'],cp['height'],margin)
        if interval is None or interval[1] <= interval[0]:
            stats['offscreen_rejected'] += 1
            continue
        tick = time.perf_counter()
        clipped_xy = xy[0]+np.asarray(interval)[:,None]*(xy[1]-xy[0])
        ids = index.query(clipped_xy)
        stats['pairs'] += len(ids)
        _require(stats['pairs'] <= max_pairs, 'Occlusion pair budget exceeded; no partial result accepted')
        pair = np.asarray(edge['vertex_ids'])+index.offsets[edge['object_id']]
        support = np.any(index.vertices[ids] == pair[0],axis=1) & np.any(index.vertices[ids] == pair[1],axis=1)
        stats['support_pairs_excluded'] += int(support.sum())
        ids = ids[~support]
        try:
            ids, lo, hi, g0, g1 = _triangle_intervals(index,ids,xy,depth,interval,stats)
            flo, fhi, front = _clip_positive(-g0,-g1,lo,hi,strict=True)
        except UnresolvedVisibility as error:
            error.visibility_context = dict(object_id=edge['object_id'],edge_id=edge['edge_id'],stage='raw_forward_partition',interval=list(interval))
            raise
        ids, flo, fhi = ids[front], flo[front], fhi[front]
        stats['coverage_seconds'] += time.perf_counter()-tick
        tick = time.perf_counter()
        hidden = union_intervals(zip(flo,fhi),max_pieces=max_pieces)
        visible = subtract_intervals(interval,hidden,max_pieces=max_pieces)
        pieces = [(a,b,'visible') for a,b in visible]+[(a,b,'hidden') for a,b in hidden]
        pieces.sort(key=lambda p:(p[0],p[1],p[2]))
        for a,b,visibility in pieces:
            start,end = _source_span(edge,a,b)
            witnesses = []
            if visibility == 'hidden':
                for effective_id, wa, wb in zip(ids,flo,fhi):
                    left,right = max(a,float(wa)),min(b,float(wb))
                    if right <= left:
                        continue
                    wu0,wu1 = _source_span(edge,left,right)
                    oi = int(index.source_object_index[effective_id])
                    ti = int(index.source_triangle_index[effective_id])
                    witness = dict(object_id=objects[oi]['id'],source_object_index=oi,
                                   source_triangle_index=ti,u0=wu0,u1=wu1)
                    mapping = objects[oi]['arrays'].get('triangle_polygon')
                    if mapping is not None:
                        witness['source_polygon_index'] = int(mapping[ti])
                    witnesses.append(witness)
                    _require(used+1+len(witnesses) <= max_pieces,
                             'Occlusion output/witness budget exceeded; no partial result accepted')
                witnesses.sort(key=lambda w:(w['source_object_index'],w['source_triangle_index'],w['u0'],w['u1']))
                _require(bool(witnesses), 'Hidden interval has no original-triangle witness')
            used += 1+len(witnesses)
            _require(used <= max_pieces, 'Occlusion output/witness budget exceeded; no partial result accepted')
            row = {k:edge[k] for k in ('source','object_id','edge_id','original_edge_id','vertex_ids','kind',
                'local_surface_domain_both_sides','local_surface_domain_side_counts') if k in edge}
            ts = np.asarray([a,b])
            row.update(u0=start,u1=end,world=world[0]+ts[:,None]*(world[1]-world[0]),
                xy=xy[0]+ts[:,None]*(xy[1]-xy[0]),depth=depth[0]+ts*(depth[1]-depth[0]),
                visibility=visibility,occluder_witnesses=witnesses,
                visibility_method='projected_affine_source_partition_experimental')
            records.append(row)
            stats[visibility+'_records'] += 1
            stats['witness_intervals'] += len(witnesses)
        stats['partition_seconds'] += time.perf_counter()-tick
    stats['returned_piece_budget_used'] = used
    stats['records'] = len(records)
    return records,stats
