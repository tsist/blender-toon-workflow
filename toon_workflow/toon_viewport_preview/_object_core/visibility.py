# SPDX-License-Identifier: GPL-3.0-or-later
"""Original solid-surface visibility sampling using Blender's read-only BVH.

No Blender objects or line carriers are created. This first implementation is
spatial sampling, not exact analytic hidden-line removal or temporal AA.
"""
from __future__ import annotations

import numpy as np

from .contract import InputError
from .geometry import _NUMERIC_EPS, _camera_parameters, _require


def _screen_interval(xy, width, height, margin):
    lo, hi = 0.0, 1.0
    delta = xy[1] - xy[0]
    for axis, lower, upper in [(0, -margin, width + margin), (1, -margin, height + margin)]:
        if delta[axis] == 0:
            if not lower <= xy[0, axis] <= upper:
                return None
        else:
            a, b = (lower - xy[0, axis]) / delta[axis], (upper - xy[0, axis]) / delta[axis]
            lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
            if lo > hi:
                return None
    return lo, hi


def _triangle_culling(obj):
    arrays = obj['arrays']
    nt = len(arrays['triangles'])
    flags = np.asarray(obj['material_culling'], dtype=bool)
    if not len(flags):
        return np.zeros(nt, dtype=bool)
    # Only actually used material slots matter, but never guess triangle/face
    # membership when culling differs among those slots.
    used = flags[np.asarray(arrays['face_material'])]
    if not len(used) or bool(np.all(used == used[0])):
        return np.full(nt, bool(used[0]) if len(used) else False, dtype=bool)
    mapping = arrays.get('triangle_polygon')
    if mapping is None:
        raise InputError('Mixed material culling requires explicit triangle_polygon: ' + str(obj['id']))
    mapping = np.asarray(mapping)
    _require(mapping.shape == (nt,) and mapping.dtype.kind in 'iu'
             and bool(((mapping >= 0) & (mapping < len(used))).all()),
             'Invalid triangle_polygon for culling: ' + str(obj['id']))
    return used[mapping]


def visible_segments(objects, edge_records, camera, sample_step_px=.25, ray_epsilon=1e-6,
                     line_categories=None, merge_outline_groups=False):
    """Sample solid visibility at <=half the requested screen step.

    Every visible/hidden transition is bisected twelve times. Features narrower
    than the actual sampling interval can still be missed; no continuous-domain
    or near-coplanar guarantee is claimed. Back-facing hit triangles are skipped
    only when their declared material enables backface culling.
    """
    cp = _camera_parameters(camera)
    _require(line_categories is None or (isinstance(line_categories,(list,tuple)) and len(line_categories)>0
             and all(x in ('outline','object') for x in line_categories)), 'Invalid line categories')
    _require(type(merge_outline_groups) is bool,'merge_outline_groups must be boolean')
    semantic_categories = line_categories is not None
    # Float32 BVH coordinates must not spend their precision on a large scene
    # translation. Keep the actual double-precision world geometry for identity
    # and projection, but shift the spatial query domain to the camera origin.
    bvh_origin=cp['world'][:3,3].copy()
    step, epsilon = float(sample_step_px), float(ray_epsilon)
    _require(np.isfinite(step) and step > 0 and np.isfinite(epsilon) and epsilon > 0,
             'Visibility sampling step and ray epsilon must be finite and positive')
    margin = camera.get('margin_px')
    if margin is not None:
        margin = float(margin)
        _require(np.isfinite(margin) and margin >= 0, 'margin_px must be finite and nonnegative')
    budget = camera.get('max_visibility_samples', 10_000_000)
    skip_budget = camera.get('max_culling_skips_per_ray', 4096)
    _require(type(budget) is int and budget > 0 and type(skip_budget) is int and skip_budget > 0,
             'Visibility budgets must be positive integers')
    stats = {'edge_count': len(edge_records), 'triangle_count': 0, 'ray_casts': 0,
             'sample_points': 0, 'transition_bisections': 0, 'culled_backface_hits': 0,
             'adjacent_triangle_hits_skipped': 0,
             'other_domain_nearest_hits': 0, 'outline_points': 0, 'object_points': 0,
             'local_both_side_object_points': 0,
             'line_categories': line_categories, 'merge_outline_groups': merge_outline_groups,
             'domain_classification': 'local incident support on both projected sides, otherwise nearest non-culled non-adjacent surface behind visible contour',
             'bvh_coordinate_origin': bvh_origin.tolist(),
             'offscreen_rejected': 0, 'offscreen_clipped': 0, 'fully_hidden_edges': 0,
             'segments': 0, 'margin_px': margin, 'requested_step_px': step,
             'maximum_sampling_interval_px': step / 2,
             'transition_bisection_steps': 12,
             'maximum_transition_bracket_px': step / 8192,
             'material_culling': 'per triangle when mapped; uniform-used-material fallback otherwise',
             'front_facing_min_normalized_dot': float(_NUMERIC_EPS),
             'degenerate_triangles': [],
             'limitations': ['Solid occluders only; alpha/transmission is not implemented',
                            'Spatial samples can miss sub-sample occluders or visible islands',
                            'Explicit ray_epsilon displaces the ray origin; near-coplanar surfaces remain limited',
                            'BVHTree uses its native numeric precision; no exact-predicate claim',
                            'No temporal AA, hidden-line output or stroke-identity implementation']}
    vertices, triangles, triangle_cull, facing = [], [], [], []
    object_offsets = {}
    object_domains = {o['id']: ('group',o['outline_group'])
                      if merge_outline_groups and o.get('outline_group') else ('object',o['id']) for o in objects}
    triangle_domains=[]
    offset = 0
    for obj in objects:
        object_offsets[obj['id']] = offset
        world = obj['world_vertices']
        tri = np.asarray(obj['arrays']['triangles'])
        cull = _triangle_culling(obj)
        if len(tri):
            tripoints = world[tri]
            normal = np.cross(tripoints[:, 1] - tripoints[:, 0], tripoints[:, 2] - tripoints[:, 0])
            length = np.linalg.norm(normal, axis=1)
            good = length > 0
            stats['degenerate_triangles'].append({'object_id': obj['id'],
                                                'triangle_ids': np.flatnonzero(~good).tolist()})
            if bool(good.any()):
                triangles.extend((tri[good] + offset).tolist())
                triangle_cull.extend(cull[good].tolist())
                triangle_domains.extend([object_domains[obj['id']]]*int(good.sum()))
                # The source-winding normal is kept independently of the normal
                # returned by BVH, so reverse rays do not change culling meaning.
                # Use the same normalized tangent band as contour/support
                # classification; positive unnormalized dot alone would let a
                # culled near-tangent surface wrongly occlude another edge.
                unit_normal = normal[good] / length[good, None]
                facing.extend(((unit_normal @ cp['toward_camera']) > _NUMERIC_EPS).tolist())
        vertices.extend((world-bvh_origin).tolist())
        offset += len(world)
    stats['triangle_count'] = len(triangles)
    bvh = None
    if triangles:
        try:
            from mathutils import Vector
            from mathutils.bvhtree import BVHTree
        except ImportError as error:
            raise InputError('mathutils.bvhtree is required for real occlusion; run inside Blender') from error
        bvh = BVHTree.FromPolygons(vertices, triangles, all_triangles=True, epsilon=0.0)
        direction = Vector(cp['toward_camera'].tolist())
        reverse_direction = -direction
        triangle_cull = np.asarray(triangle_cull, dtype=bool)
        facing = np.asarray(facing, dtype=bool)
        triangle_vertices = triangles
    # The BVH owns its copied data; drop temporary Python geometry lists.
    del vertices, triangles

    def has_occluder(point, depth, edge_vertices, domain=None, reverse=False):
        distance = float(cp['far']-depth-epsilon if reverse else depth-cp['near']-epsilon)
        if bvh is None or distance <= 0:
            return False
        ray_direction = reverse_direction if reverse else direction
        origin = Vector((point-bvh_origin + cp['toward_camera'] * (-epsilon if reverse else epsilon)).tolist())
        for _ in range(skip_budget):
            stats['ray_casts'] += 1
            hit, _, index, hit_distance = bvh.ray_cast(origin, ray_direction, distance)
            if hit is None:
                return False
            # A candidate point is on this topological edge. Its incident
            # triangles cannot occlude it along the camera ray. Native float32
            # BVH arithmetic can nevertheless return tiny positive self-hits,
            # particularly near grazing silhouettes. Exclude only triangles
            # containing both edge vertices; nearby unrelated geometry remains
            # a valid occluder regardless of its distance.
            hit_vertices = triangle_vertices[index]
            adjacent = edge_vertices[0] in hit_vertices and edge_vertices[1] in hit_vertices
            if adjacent:
                stats['adjacent_triangle_hits_skipped'] += 1
            elif not triangle_cull[index] or facing[index]:
                # The nearest effective surface determines the visible domain
                # across this contour. A nearer other-domain surface must not
                # be skipped to find a farther same-domain surface.
                if domain is not None and triangle_domains[index]!=domain:
                    stats['other_domain_nearest_hits'] += 1
                    return False
                return True
            else:
                stats['culled_backface_hits'] += 1
            advance = float(hit_distance) + epsilon
            origin += ray_direction * advance
            distance -= advance
            if distance <= 0:
                return False
        raise InputError('Visibility skip budget exceeded; visibility is unresolved')

    segments = []
    by_id = {obj['id']: obj for obj in objects}
    for edge in edge_records:
        source_object = by_id[edge['object_id']]
        local_edge_vertices = (edge['vertex_ids'] if 'vertex_ids' in edge else
                               source_object['arrays']['edges'][edge['edge_id']])
        edge_vertices = tuple(int(v) + object_offsets[edge['object_id']] for v in local_edge_vertices)
        xy = np.asarray(edge['xy'], dtype=np.float64)
        world = np.asarray(edge['world'], dtype=np.float64)
        depth = np.asarray(edge['depth'], dtype=np.float64)
        _require(xy.shape == (2, 2) and world.shape == (2, 3) and depth.shape == (2,)
                 and bool(np.isfinite(xy).all()) and bool(np.isfinite(world).all())
                 and bool(np.isfinite(depth).all()), 'Malformed projected edge')
        interval = _screen_interval(xy, cp['width'], cp['height'], margin) if margin is not None else (0., 1.)
        if interval is None:
            stats['offscreen_rejected'] += 1
            continue
        lo, hi = interval
        if lo != 0 or hi != 1:
            stats['offscreen_clipped'] += 1
        pixel_length = float(np.linalg.norm(xy[1] - xy[0]))
        count = max(1, int(np.ceil(pixel_length * (hi - lo) / (step / 2))))
        _require(count + stats['sample_points'] + 1 <= budget,
                 'Visibility sample budget would be exceeded; no partial result is accepted')
        parameters = np.linspace(lo, hi, count + 1)
        delta_world, delta_depth = world[1] - world[0], depth[1] - depth[0]

        def at(t):
            stats['sample_points'] += 1
            if stats['sample_points'] > budget:
                raise InputError('Visibility sample budget exceeded; no partial result is accepted')
            point=world[0] + t * delta_world
            point_depth=depth[0] + t * delta_depth
            if has_occluder(point,point_depth,edge_vertices):return 0
            if not semantic_categories:return 1
            # Incident triangles are excluded from ray self-hits, but their
            # local projected support still distinguishes an internal domain
            # edge from its outer boundary. In particular, four-face edges
            # cannot be classified solely by any-front/any-back winding.
            interior=bool(edge.get('local_surface_domain_both_sides',False))
            if interior:
                stats['local_both_side_object_points']+=1
            else:
                interior=has_occluder(point,point_depth,edge_vertices,domain=object_domains[edge['object_id']],reverse=True)
            stats['object_points' if interior else 'outline_points']+=1
            return 2 if interior else 1

        states = np.fromiter((at(t) for t in parameters), dtype=np.uint8, count=len(parameters))
        if not bool(states.any()):
            stats['fully_hidden_edges'] += 1
            continue
        boundaries = {}
        for j in np.flatnonzero(states[1:] != states[:-1]):
            left, right, left_state = float(parameters[j]), float(parameters[j + 1]), int(states[j])
            for _ in range(12):
                middle = (left + right) / 2
                if at(middle) == left_state:
                    left = middle
                else:
                    right = middle
                stats['transition_bisections'] += 1
            boundaries[int(j)] = (left + right) / 2
        base_u0, base_u1 = float(edge.get('u0', 0)), float(edge.get('u1', 1))
        for code in (1,2) if semantic_categories else (1,):
            line_type='outline' if code==1 else 'object'
            if semantic_categories and line_type not in line_categories:continue
            selected=states==code
            starts = np.flatnonzero(selected & np.r_[True, ~selected[:-1]])
            ends = np.flatnonzero(selected & np.r_[~selected[1:], True])
            for first, last in zip(starts, ends):
                a = float(parameters[first]) if first == 0 else boundaries[int(first - 1)]
                b = float(parameters[last]) if last == len(states) - 1 else boundaries[int(last)]
                ts = np.array([a, b])
                row = {k: edge[k] for k in ('object_id', 'edge_id', 'kind')}
                for key in ('vertex_ids','original_edge_id','local_surface_domain_both_sides',
                            'local_surface_domain_side_counts'):
                    if key in edge:row[key]=edge[key]
                if semantic_categories:row['line_type']=line_type
                row.update(source=edge.get('source', (edge['object_id'], edge['edge_id'], edge['kind'])),
                           xy=xy[0] + ts[:, None] * (xy[1] - xy[0]),
                           depth=depth[0] + ts * delta_depth,
                           world=world[0] + ts[:, None] * delta_world,
                           u0=base_u0 + a * (base_u1 - base_u0),
                           u1=base_u0 + b * (base_u1 - base_u0))
                segments.append(row)
    stats['segments'] = len(segments)
    return segments, stats
