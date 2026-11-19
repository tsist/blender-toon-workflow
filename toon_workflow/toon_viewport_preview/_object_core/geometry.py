# SPDX-License-Identifier: GPL-3.0-or-later
"""Original NumPy geometry preparation for the bounded orthographic line pass.

This module creates no Blender datablocks. Actual input triangles supply the
geometric winding normals and adjacency used for contour classification, matching
the surface consumed by visibility. Authored polygon/corner normals are retained.
"""
from __future__ import annotations

import numpy as np

from .contract import InputError


_NUMERIC_EPS = 64 * np.finfo(np.float64).eps


def _require(condition, message):
    if not condition:
        raise InputError(message)


def _finite(value, shape, label, integer=False):
    a = np.asarray(value)
    _require(a.shape == shape, label + ' has an invalid shape')
    _require(a.dtype.kind in ('iu' if integer else 'biuf'), label + ' has an invalid dtype')
    _require(bool(np.isfinite(a).all()), label + ' contains nonfinite values')
    return a


def _camera_parameters(camera):
    """Validate the supported camera and return its explicit mathematical data."""
    _require(camera.get('kind') == 'ORTHO', 'Only ORTHO cameras are supported')
    width, height = camera.get('width'), camera.get('height')
    _require(type(width) is int and type(height) is int and width > 0 and height > 0,
             'Camera dimensions must be positive integers')
    world = _finite(camera['world'], (4, 4), 'camera.world').astype(np.float64, copy=False)
    projection = _finite(camera['projection'], (4, 4), 'camera.projection').astype(np.float64, copy=False)
    _require(np.array_equal(world[3], [0, 0, 0, 1]), 'Camera world matrix must be affine')
    rotation = world[:3, :3]
    # Blender stores its poses in float precision. This bound validates a rigid
    # pose; it is not an image-matching or geometry-error acceptance threshold.
    pose_eps = 64 * np.finfo(np.float32).eps
    _require(bool(np.allclose(rotation.T @ rotation, np.eye(3), atol=pose_eps, rtol=0))
             and np.linalg.det(rotation) > 0, 'Scaled, mirrored or sheared cameras are unsupported')
    _require(abs(projection[3, 3]) > _NUMERIC_EPS
             and bool(np.all(np.abs(projection[3, :3]) <= _NUMERIC_EPS)),
             'Camera projection is not orthographic')
    projection = projection / projection[3, 3]
    _require(abs(projection[2, 2]) > _NUMERIC_EPS, 'Camera depth projection is singular')
    near = float(camera.get('clip_start', (projection[2, 3] + 1) / projection[2, 2]))
    far = float(camera.get('clip_end', (projection[2, 3] - 1) / projection[2, 2]))
    # A Blender orthographic viewport uses a signed view-depth slab around
    # its view origin. Keep explicit camera inputs strict unless the adapter
    # declares that convention; do not move the origin or clamp the near plane.
    policy = camera.get('clip_depth_policy', 'nonnegative_near')
    _require(type(policy) is str and policy in ('nonnegative_near', 'signed_ortho_view'),
             'Unknown orthographic depth policy')
    _require(np.isfinite(near) and np.isfinite(far) and near < far
             and (near >= 0 or policy == 'signed_ortho_view')
             and (policy != 'signed_ortho_view' or far > 0),
             'Camera clipping range is invalid')
    toward_camera = rotation[:, 2].copy()
    toward_camera /= np.linalg.norm(toward_camera)
    view = np.linalg.inv(world)
    return {'world': world, 'view': view, 'projection': projection,
            'view_projection': projection @ view, 'toward_camera': toward_camera,
            'near': near, 'far': far, 'width': width, 'height': height}


def prepare_objects(raw_objects):
    """Return new dictionaries with frozen-input world geometry and diagnostics.

    Original ``arrays`` and IDs are retained. No input array or source transform
    is modified. Invalid indexing/connectivity is rejected before adjacency work.
    Derived triangle topology is cached separately from the preserved arrays.
    """
    _require(isinstance(raw_objects, (list, tuple)), 'Objects must be a list')
    result, ids = [], set()
    for raw in raw_objects:
        oid = raw['id']
        _require(isinstance(oid, (str, int)) and not isinstance(oid, bool) and oid not in ids,
                 'Object IDs must be unique strings or integers')
        ids.add(oid)
        arrays = raw['arrays']
        co = np.asarray(arrays['co'])
        _require(co.ndim == 2 and co.shape[1] == 3, 'co must be Nx3')
        co = _finite(co, co.shape, 'co')
        nv = len(co)
        edges = np.asarray(arrays['edges'])
        _require(edges.ndim == 2 and edges.shape[1] == 2, 'edges must be Nx2')
        edges = _finite(edges, edges.shape, 'edges', integer=True)
        ne = len(edges)
        starts = np.asarray(arrays['face_start'])
        _require(starts.ndim == 1, 'face_start must be one dimensional')
        nf = len(starts)
        starts = _finite(starts, (nf,), 'face_start', integer=True)
        totals = _finite(arrays['face_total'], (nf,), 'face_total', integer=True)
        _require(bool((totals >= 3).all()), 'Faces require at least three corners')
        nl = int(totals.sum(dtype=np.int64))
        _require(np.array_equal(starts, np.cumsum(np.r_[0, totals[:-1]], dtype=np.int64))
                 if nf else nl == 0, 'Face loop ranges are not contiguous')
        loop_vertex = _finite(arrays['loop_vertex'], (nl,), 'loop_vertex', integer=True)
        loop_edge = _finite(arrays['loop_edge'], (nl,), 'loop_edge', integer=True)
        for label, index, limit in [('edges', edges, nv), ('loop_vertex', loop_vertex, nv),
                                    ('loop_edge', loop_edge, ne)]:
            _require(bool(((index >= 0) & (index < limit)).all()), 'Out of range ' + label)
        face_normal = _finite(arrays['face_normal'], (nf, 3), 'face_normal')
        _finite(arrays['face_smooth'], (nf,), 'face_smooth')
        face_material = _finite(arrays['face_material'], (nf,), 'face_material', integer=True)
        _require(bool((face_material >= 0).all()), 'Negative material indices are unsupported')
        _finite(arrays['edge_sharp'], (ne,), 'edge_sharp')
        _finite(arrays['corner_normals'], (nl, 3), 'corner_normals')
        triangles = np.asarray(arrays['triangles'])
        _require(triangles.ndim == 2 and triangles.shape[1] == 3, 'triangles must be Nx3')
        triangles = _finite(triangles, triangles.shape, 'triangles', integer=True)
        _require(bool(((triangles >= 0) & (triangles < nv)).all()), 'Out of range triangles')
        if nl:
            following = np.arange(nl, dtype=np.int64) + 1
            following[starts + totals - 1] = starts
            a, b = loop_vertex, loop_vertex[following]
            pair = edges[loop_edge]
            _require(bool((((pair[:, 0] == a) & (pair[:, 1] == b))
                           | ((pair[:, 1] == a) & (pair[:, 0] == b))).all()),
                     'loop_edge does not describe its polygon boundary')
        matrix = _finite(raw['matrix_world'], (4, 4), 'object.matrix_world').astype(np.float64, copy=False)
        _require(np.array_equal(matrix[3], [0, 0, 0, 1]), 'Object matrix must be affine')
        linear = matrix[:3, :3]
        determinant = float(np.linalg.det(linear))
        _require(np.isfinite(determinant) and determinant != 0, 'Object matrix is singular')
        culling = list(raw.get('material_culling', []))
        _require(all(type(x) in (bool, np.bool_) for x in culling), 'material_culling must contain booleans')
        _require(not len(face_material) or int(face_material.max()) < max(1, len(culling)),
                 'Material culling list does not cover face material slots')
        world_vertices = co.astype(np.float64) @ linear.T + matrix[:3, 3]
        # The determinant sign preserves *geometric winding* under reflection.
        normals = face_normal.astype(np.float64) @ np.linalg.inv(linear)
        normals *= np.sign(determinant)
        lengths = np.linalg.norm(normals, axis=1)
        nonzero = lengths > 0
        normals[nonzero] /= lengths[nonzero, None]
        _require(bool(np.isfinite(world_vertices).all()) and bool(np.isfinite(normals).all()),
                 'World geometry overflowed')
        edge_delta = world_vertices[edges[:, 1]] - world_vertices[edges[:, 0]]
        row = dict(raw)
        row.update(world_vertices=world_vertices, world_face_normals=normals,
                   material_culling=[bool(x) for x in culling])
        row['_triangle_topology'] = _triangle_topology(world_vertices, triangles)
        row['_geometry_diagnostics'] = {
            'zero_length_edge_ids': np.flatnonzero(np.all(edge_delta == 0, axis=1)).tolist(),
            'zero_normal_face_ids': np.flatnonzero(~nonzero).tolist(),
            'negative_transform_determinant': determinant < 0,
        }
        result.append(row)
    return result


def _triangle_topology(world_vertices, triangles):
    """Build unoriented triangle-edge IDs without touching source topology."""
    nt, nv = len(triangles), len(world_vertices)
    if not nt:
        return {'keys': np.empty(0, dtype=np.int64), 'inverse': np.empty(0, dtype=np.int64),
                'counts': np.empty(0, dtype=np.int64), 'normals': np.empty((0, 3)),
                'valid': np.empty(0, dtype=bool), 'direction_sign': np.empty(0, dtype=np.int8),
                'zero_area_triangle_ids': []}
    _require(nv <= 3_037_000_499, 'Vertex-pair identity exceeds int64 capacity')
    points = world_vertices[triangles]
    normals = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    valid = lengths > 0
    normals[valid] /= lengths[valid, None]
    _require(bool(np.isfinite(normals).all()), 'Triangle normal calculation overflowed')
    pairs = np.asarray(triangles[:, [[0, 1], [1, 2], [2, 0]]], dtype=np.int64).reshape(-1, 2)
    direction_sign = np.where(pairs[:, 0] <= pairs[:, 1], 1, -1).astype(np.int8)
    pairs.sort(axis=1)
    keys = pairs[:, 0] * nv + pairs[:, 1]
    unique, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    return {'keys': unique, 'inverse': inverse, 'counts': counts, 'normals': normals,
            'valid': valid, 'direction_sign': direction_sign,
            'zero_area_triangle_ids': np.flatnonzero(~valid).tolist()}


def _triangle_culling_for_classification(obj):
    """Resolve culling exactly, refusing ambiguous triangle/material mapping."""
    a = obj['arrays']
    nt = len(a['triangles'])
    flags = np.asarray(obj['material_culling'], dtype=bool)
    if not len(flags):
        return np.zeros(nt, dtype=bool)
    used = flags[np.asarray(a['face_material'])]
    if not len(used) or bool(np.all(used == used[0])):
        return np.full(nt, bool(used[0]) if len(used) else False, dtype=bool)
    mapping = a.get('triangle_polygon')
    if mapping is None:
        raise InputError('Mixed material culling requires explicit triangle_polygon: ' + str(obj['id']))
    mapping = np.asarray(mapping)
    _require(mapping.shape == (nt,) and mapping.dtype.kind in 'iu'
             and bool(((mapping >= 0) & (mapping < len(used))).all()),
             'Invalid triangle_polygon for culling: ' + str(obj['id']))
    return used[mapping]


def _depth_interval(depth, near, far):
    lo, hi = 0.0, 1.0
    d0, change = float(depth[0]), float(depth[1] - depth[0])
    if change == 0:
        return (lo, hi) if near <= d0 <= far else None
    a, b = (near - d0) / change, (far - d0) / change
    lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
    return (lo, hi) if lo <= hi else None


def classify_project(objects, camera, include_open=True):
    """Classify the actual triangle surface and preserve original/virtual IDs.

    Original mesh edges retain their integer IDs and orientation. A triangulation
    diagonal is explicitly named ``triangle-diagonal:min:max`` with
    ``original_edge_id=None``; every record supplies original vertex_ids for
    support-triangle lookup. Screen-border clipping remains deferred.
    """
    cp = _camera_parameters(camera)
    records = []
    stats = {'object_count': len(objects), 'candidate_edges': 0, 'projected_edges': 0,
             'near_far_rejected': 0, 'near_far_clipped': 0,
             'screen_border_clipping': 'deferred; complete offscreen extent retained',
             'normal_domain': 'actual input triangle geometric winding normals',
             'tangent_policy': 'non-front; explicitly counted', 'objects': []}
    for obj in objects:
        arrays, vertices = obj['arrays'], obj['world_vertices']
        edges, nv = np.asarray(arrays['edges']), len(vertices)
        ne = len(edges)
        topology = obj.get('_triangle_topology')
        if topology is None or 'direction_sign' not in topology:
            topology = _triangle_topology(vertices, np.asarray(arrays['triangles']))
        keys, inverse, counts = topology['keys'], topology['inverse'], topology['counts']
        facing = topology['normals'] @ cp['toward_camera']
        valid = topology['valid']
        front = (facing > _NUMERIC_EPS) & valid
        nonfront = ~front & valid
        fc = np.bincount(inverse, weights=np.repeat(front, 3), minlength=len(keys))
        bc = np.bincount(inverse, weights=np.repeat(nonfront, 3), minlength=len(keys))
        silhouette = (fc > 0) & (bc > 0)
        valid_count = np.bincount(inverse, weights=np.repeat(valid, 3), minlength=len(keys))
        opened = (counts == 1) & (valid_count == 1) if include_open else np.zeros(len(keys), dtype=bool)
        cull = _triangle_culling_for_classification(obj)
        # For a directed edge following triangle winding, n.dot(camera +Z)
        # has the sign of its third vertex's projected side. Converting the
        # directed pair to sorted vertex order multiplies that sign once.
        # This measures support occupancy; it never deletes a contour candidate.
        side_value = np.repeat(facing, 3) * topology['direction_sign']
        support = np.repeat(valid & (~cull | front), 3)
        positive_support = np.bincount(inverse, weights=support & (side_value > _NUMERIC_EPS), minlength=len(keys))
        negative_support = np.bincount(inverse, weights=support & (side_value < -_NUMERIC_EPS), minlength=len(keys))
        tangent_support = np.bincount(inverse, weights=support & (np.abs(side_value) <= _NUMERIC_EPS), minlength=len(keys))
        culled_support = np.bincount(inverse, weights=np.repeat(valid & cull & ~front, 3), minlength=len(keys))
        degenerate_support = np.bincount(inverse, weights=np.repeat(~valid, 3), minlength=len(keys))
        both_sides = (positive_support > 0) & (negative_support > 0)
        culled_open = opened & (np.bincount(inverse, weights=np.repeat(cull & ~front, 3), minlength=len(keys)) > 0)
        opened &= ~culled_open
        candidates = silhouette | opened

        sorted_pairs = np.sort(edges.astype(np.int64, copy=False), axis=1)
        original_keys = sorted_pairs[:, 0] * nv + sorted_pairs[:, 1]
        ranks = np.searchsorted(keys, original_keys)
        present = ranks < len(keys)
        if len(keys):
            present &= keys[np.minimum(ranks, len(keys) - 1)] == original_keys
        else:
            present[:] = False
        selected_original = np.zeros(ne, dtype=bool)
        if present.any():
            selected_original[present] = candidates[ranks[present]]
        original_ids = np.flatnonzero(selected_original)
        virtual_ranks = np.flatnonzero(candidates & ~np.isin(keys, original_keys))
        # Duplicate original edge rows are retained as distinct original IDs;
        # they are diagnosed instead of being silently folded into virtual IDs.
        _, original_inverse, original_counts = np.unique(original_keys, return_inverse=True, return_counts=True)
        nonmanifold_original = np.flatnonzero(present & (counts[np.minimum(ranks, max(0, len(keys) - 1))] > 2)) if len(keys) else np.empty(0, dtype=np.int64)
        polygon_counts = np.bincount(np.asarray(arrays['loop_edge']), minlength=ne)
        polygon_facing = obj['world_face_normals'] @ cp['toward_camera']
        polygon_valid = np.linalg.norm(obj['world_face_normals'], axis=1) > 0
        detail = dict(obj['_geometry_diagnostics'])
        detail.update(object_id=obj['id'], edge_count=ne, triangle_edge_count=len(keys),
                      candidate_count=len(original_ids) + len(virtual_ranks),
                      silhouette_count=int(silhouette.sum()), open_count=int(opened.sum()),
                      original_edge_candidate_count=len(original_ids),
                      internal_diagonal_candidate_count=len(virtual_ranks),
                      local_both_side_candidate_count=int(both_sides[ranks[original_ids]].sum() + both_sides[virtual_ranks].sum()),
                      local_surface_domain_method='culling-aware incident triangle projected side; no candidate removal',
                      loose_edge_ids=np.flatnonzero(polygon_counts == 0).tolist(),
                      nonmanifold_edge_ids=nonmanifold_original.tolist(),
                      nonmanifold_triangle_edge_vertex_ids=[[int(k // nv), int(k % nv)] for k in keys[counts > 2]],
                      duplicate_original_edge_ids=np.flatnonzero(original_counts[original_inverse] > 1).tolist(),
                      zero_area_triangle_ids=topology['zero_area_triangle_ids'],
                      tangent_triangle_ids=np.flatnonzero(valid & (np.abs(facing) <= _NUMERIC_EPS)).tolist(),
                      tangent_face_ids=np.flatnonzero(polygon_valid & (np.abs(polygon_facing) <= _NUMERIC_EPS)).tolist(),
                      tangent_face_ids_scope='preserved polygon-normal diagnostic only; not used for classification',
                      culled_open_edge_ids=np.flatnonzero(present & culled_open[np.minimum(ranks, len(keys) - 1)]).tolist() if len(keys) else [])
        stats['objects'].append(detail)
        stats['candidate_edges'] += detail['candidate_count']
        selected = [(int(i), int(i), int(edges[i, 0]), int(edges[i, 1]), int(ranks[i])) for i in original_ids]
        selected += [(f'triangle-diagonal:{int(keys[r] // nv)}:{int(keys[r] % nv)}', None,
                      int(keys[r] // nv), int(keys[r] % nv), int(r)) for r in virtual_ranks]
        for edge_id, original_edge_id, v0, v1, rank in selected:
            ends = vertices[[v0, v1]]
            homogeneous = np.column_stack((ends, np.ones(2)))
            depths = -(homogeneous @ cp['view'].T)[:, 2]
            interval = _depth_interval(depths, cp['near'], cp['far'])
            if interval is None:
                stats['near_far_rejected'] += 1
                continue
            u0, u1 = interval
            if u0 != 0 or u1 != 1:
                stats['near_far_clipped'] += 1
            world = ends[0] + np.array([u0, u1])[:, None] * (ends[1] - ends[0])
            clip = np.column_stack((world, np.ones(2))) @ cp['view_projection'].T
            _require(bool((np.abs(clip[:, 3]) > _NUMERIC_EPS).all()), 'Projection has zero homogeneous w')
            xy = (clip[:, :2] / clip[:, 3:4] + 1) * [cp['width'] / 2, cp['height'] / 2]
            depth = depths[0] + np.array([u0, u1]) * (depths[1] - depths[0])
            kind = 'silhouette' if silhouette[rank] else 'open'
            positive, negative = int(positive_support[rank]), int(negative_support[rank])
            if v0 > v1:
                positive, negative = negative, positive
            support_counts = {'positive': positive, 'negative': negative,
                              'tangent': int(tangent_support[rank]), 'culled': int(culled_support[rank]),
                              'degenerate': int(degenerate_support[rank]),
                              'incident_triangle_edge_occurrences': int(counts[rank]),
                              'orientation': 'record.vertex_ids; signed projected cross(edge, third vertex)'}
            records.append({'object_id': obj['id'], 'edge_id': edge_id,
                            'original_edge_id': original_edge_id, 'vertex_ids': (v0, v1),
                            'local_surface_domain_both_sides': bool(both_sides[rank]),
                            'local_surface_domain_side_counts': support_counts,
                            'kind': kind, 'source': (obj['id'], edge_id, kind),
                            'world': world, 'xy': xy, 'depth': depth, 'u0': u0, 'u1': u1})
    stats['projected_edges'] = len(records)
    return records, stats
