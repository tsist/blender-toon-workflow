# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent Blender >=4.1 smoothing-boundary preprocessing.

The arithmetic/topology behavior is informed by the public v4.1.5 bridge, not
copied from its C++ source. This module does not reproduce the closed drawing
engine, angle-based old Blender smoothing, wire or Freestyle categories.
"""
import numpy as np

SCHEMA = 'toon.blender41-smoothing-flags.v1'
MAX_EDGES = 2_000_000
MAX_LOOPS = 8_000_000
MAX_TRIANGLES = 4_000_000


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _array(arrays, name, shape, *, boolean=False):
    value = arrays[name]
    _need(isinstance(value, np.ndarray) and value.shape == shape, name+' shape/type')
    _need(value.dtype == np.dtype('?') if boolean else value.dtype.kind in 'iu',
          name+' requires '+('Boolean' if boolean else 'integer')+' dtype')
    if not boolean:
        _need(bool((value >= 0).all()), name+' requires nonnegative indices')
    return value


def compute_flags(arrays, *, checkpoint=lambda: None):
    """Return fresh read-only flags and original-edge transfer identities.

    Required arrays are listed in REQUIRED below; extra arrays are ignored.
    Polygon loops must be contiguous, and triangle corners must belong to their
    declared polygon. The existing exporter supplies this contract. A triangle
    side is an original edge only when its corner's declared loop_edge has the
    exact unordered pair of triangle endpoints; virtual diagonals remain -1.

    For flat polygons, the first polygon encountered at an edge is retained.
    A later loop encounter adds the boundary if the current or first polygon
    is flat. This also intentionally retains the source's repeated-loop behavior
    for degenerate polygons; it is not a two-distinct-polygons test.

    checkpoint is called before work, every 4096 polygons / 65536 triangles and
    at exit. Input arrays are never modified. Resource limits are logical array
    bounds, not OS memory or hard cancellation guarantees.
    """
    required = ('edges','edge_sharp','face_smooth','face_start','face_total',
                'loop_vertex','loop_edge','triangles','triangle_loop','triangle_polygon')
    _need(type(arrays) is dict and all(k in arrays for k in required), 'Missing smoothing arrays')
    _need(callable(checkpoint), 'checkpoint must be callable')
    checkpoint()
    for name in ('edges','face_start','loop_vertex','triangles'):
        _need(isinstance(arrays[name], np.ndarray) and arrays[name].ndim > 0, name+' array required')
    ne, nf, nl, nt = (len(arrays[k]) for k in ('edges','face_start','loop_vertex','triangles'))
    _need(ne <= MAX_EDGES and nl <= MAX_LOOPS and nt <= MAX_TRIANGLES and nf <= nl,
          'Smoothing array budget exceeded')
    edges = _array(arrays,'edges',(ne,2))
    sharp = _array(arrays,'edge_sharp',(ne,),boolean=True)
    smooth = _array(arrays,'face_smooth',(nf,),boolean=True)
    start = _array(arrays,'face_start',(nf,))
    total = _array(arrays,'face_total',(nf,))
    lv = _array(arrays,'loop_vertex',(nl,))
    le = _array(arrays,'loop_edge',(nl,))
    tri = _array(arrays,'triangles',(nt,3))
    tl = _array(arrays,'triangle_loop',(nt,3))
    tp = _array(arrays,'triangle_polygon',(nt,))
    _need(bool((start <= nl).all()) and bool(((total >= 3) & (total <= nl)).all()),
          'Polygon loop range')
    _need(int(total.sum(dtype=np.uint64)) == nl, 'Polygon loops must cover input exactly')
    expected_start = np.cumsum(np.r_[np.int64(0), total[:-1]], dtype=np.int64) if nf else np.empty(0,dtype=np.int64)
    _need(np.array_equal(start, expected_start), 'Polygon loops must be contiguous')
    _need(bool((le < ne).all()) and bool((tl < nl).all()) and bool((tp < nf).all()),
          'Smoothing indices out of range')
    if 'co' in arrays:
        _need(isinstance(arrays['co'],np.ndarray) and arrays['co'].ndim == 2 and arrays['co'].shape[1] == 3,
              'Optional co shape')
        nv = len(arrays['co'])
        _need(bool((edges < nv).all()) and bool((lv < nv).all()) and bool((tri < nv).all()),
              'Vertex index out of range')

    edge_flags = np.array(sharp,dtype=bool,copy=True)
    flat_cause = np.zeros(ne,dtype=bool)
    flat = ~smooth
    if bool(flat.any()):
        first_polygon = np.full(ne,-1,dtype=np.int64)
        for pi in range(nf):
            if pi % 4096 == 0:
                checkpoint()
            begin, end = int(start[pi]), int(start[pi])+int(total[pi])
            for edge_value in le[begin:end]:
                eid = int(edge_value)
                first = int(first_polygon[eid])
                if first < 0:
                    first_polygon[eid] = pi
                elif bool(flat[pi]) or bool(flat[first]):
                    flat_cause[eid] = True
        edge_flags |= flat_cause

    triangle_edge_ids = np.full((nt,3),-1,dtype=np.int64)
    triangle_flags = np.zeros((nt,3),dtype=bool)
    for begin in range(0,nt,65536):
        checkpoint()
        end = min(begin+65536,nt)
        loops = tl[begin:end]
        polygons = tp[begin:end]
        _need(np.array_equal(lv[loops],tri[begin:end]), 'Triangle corner/vertex mismatch')
        lo = start[polygons,None].astype(np.int64,copy=False)
        hi = lo + total[polygons,None].astype(np.int64,copy=False)
        _need(bool(((loops >= lo) & (loops < hi)).all()), 'Triangle corner outside polygon')
        candidates = le[loops].astype(np.int64,copy=False)
        pairs = edges[candidates]
        source = tri[begin:end]
        following = source[:,(1,2,0)]
        same = ((pairs[:,:,0] == source) & (pairs[:,:,1] == following)) | \
               ((pairs[:,:,1] == source) & (pairs[:,:,0] == following))
        triangle_edge_ids[begin:end] = np.where(same,candidates,-1)
        triangle_flags[begin:end] = same & edge_flags[candidates]
    eligible = np.unique(triangle_edge_ids[triangle_flags])
    eligible = np.asarray(eligible,dtype=np.int64)
    stats = dict(schema=SCHEMA,edges=ne,polygons=nf,loops=nl,triangles=nt,
        authored_sharp_edges=int(sharp.sum()),flat_polygons=int(flat.sum()),
        flat_shared_loop_boundary_edges=int(flat_cause.sum()),
        added_flat_boundary_edges=int((flat_cause & ~sharp).sum()),
        edge_smoothing_count=int(edge_flags.sum()),
        transferred_smoothing_corners=int(triangle_flags.sum()),
        eligible_edge_count=len(eligible),virtual_or_mismatched_triangle_sides=int((triangle_edge_ids < 0).sum()),
        model='public_blender_ge_4_1_sharp_and_flat_first_polygon_loop_flags',
        closed_drawing_equivalence=False,wire_and_freestyle_included=False)
    for value in (edge_flags,triangle_flags,triangle_edge_ids,eligible):
        value.flags.writeable = False
    checkpoint()
    return dict(edge_smoothing=edge_flags,triangle_smoothing=triangle_flags,
        triangle_edge_ids=triangle_edge_ids,eligible_edge_ids=eligible,stats=stats)
