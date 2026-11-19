# SPDX-License-Identifier: GPL-3.0-or-later
"""Camera-ray resolve of declared static, non-refractive eye layers.

The caller supplies an EEVEE base rendered with every declared eye material
replaced by white Transparent BSDF. This module neither swaps materials nor
renders. Native object-origin transparency sorting is not used for the eye
layers: the same finite analytic shaders as the line adapter are applied in
physical ray-distance order, against captured opaque receiver triangles.
"""
import math

from . import eye_overlay_lines

SCHEMA = 'toon-eye-overlay-beauty-resolve.v1'
_TILE_SIDE = 128
_RAY_EPSILON = 1.e-6


def _camera(frame, base):
    import numpy as np
    width, height = frame.get('width'), frame.get('height')
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 2048 or not 1 <= height <= 2048):
        raise ValueError('Eye beauty requires a complete frame of at most 2048 per side')
    if (not isinstance(base, np.ndarray) or base.dtype != np.float32
            or base.shape != (height, width, 4) or not np.isfinite(base).all()
            or np.any(base[:, :, 3] < 0.) or np.any(base[:, :, 3] > 1.)):
        raise ValueError('Eye base must be finite complete-frame premultiplied float32 RGBA')
    if frame.get('metadata', {}).get('evaluation') != 'RENDER':
        raise ValueError('Eye beauty requires the actual RENDER camera capture')
    if type(frame.get('perspective')) is not bool:
        raise ValueError('Eye beauty requires an explicit perspective/orthographic flag')
    mvp = np.asarray(frame['mvp'], np.float64)
    view = np.asarray(frame['view'], np.float64)
    if (mvp.shape != (4, 4) or view.shape != (4, 4)
            or not np.isfinite(mvp).all() or not np.isfinite(view).all()):
        raise ValueError('Finite 4 by 4 eye camera matrices required')
    try:
        inverse_mvp = np.linalg.inv(mvp)
        inverse_view = np.linalg.inv(view)
    except np.linalg.LinAlgError as error:
        raise ValueError('Eye camera matrices must be invertible') from error
    if not np.isfinite(inverse_mvp).all() or not np.isfinite(inverse_view).all():
        raise ValueError('Eye camera inverse is nonfinite')
    return width, height, mvp, inverse_mvp, inverse_view


def _projected_triangles(snapshot, mvp, width, height):
    """Conservative pixel-center boxes; ray tests perform the exact far cut."""
    import numpy as np
    result = []
    for row in snapshot['triangles']:
        points = np.asarray(row['world'], np.float64)
        homogeneous = np.concatenate((points, np.ones((3, 1), np.float64)), axis=1)
        clip = homogeneous @ mvp.T
        if not np.isfinite(clip).all():
            raise ValueError('Nonfinite projected eye triangle')
        # A triangle wholly beyond any one clip plane cannot cover the frame.
        if any(np.all(sign*clip[:, axis] > clip[:, 3])
               for axis in range(3) for sign in (-1., 1.)):
            continue
        # Mixed near-plane clipping needs another adapter. Never project its
        # vertices across w=0 and silently lose the clipped polygon coverage.
        if np.any(clip[:, 3] <= 0.) or np.any(clip[:, 2]+clip[:, 3] <= 0.):
            raise ValueError('Eye triangles crossing/touching the near plane are not qualified')
        ndc = clip[:, :2]/clip[:, 3:4]
        screen = (ndc*.5+.5)*np.asarray([width, height], np.float64)
        # x+.5/y+.5 are the complete-frame pixel centers. The small arithmetic
        # envelope only enlarges this candidate box; it does not create a hit.
        lo = np.ceil(screen.min(axis=0)-.5-1.e-7)
        hi = np.floor(screen.max(axis=0)-.5+1.e-7)+1
        x0, y0 = max(0, int(lo[0])), max(0, int(lo[1]))
        x1, y1 = min(width, int(hi[0])), min(height, int(hi[1]))
        if x1 <= x0 or y1 <= y0:
            continue
        result.append(dict(material_index=row['material_index'], world=points,
                           uv=np.asarray(row['uv'], np.float64),
                           bbox=(x0, y0, x1, y1)))
    return result


def _rays(xx, yy, width, height, inverse_mvp, inverse_view, perspective):
    import numpy as np
    ndc = np.column_stack(((xx+.5)/width*2.-1., (yy+.5)/height*2.-1.,
                           np.ones(len(xx)), np.ones(len(xx))))
    far_h = ndc @ inverse_mvp.T
    ndc[:, 2] = -1.
    near_h = ndc @ inverse_mvp.T
    if np.any(abs(near_h[:, 3]) < 1.e-20) or np.any(abs(far_h[:, 3]) < 1.e-20):
        raise ValueError('Eye camera near/far unprojection is singular')
    near = near_h[:, :3]/near_h[:, 3:4]
    far = far_h[:, :3]/far_h[:, 3:4]
    if perspective:
        eye_h = inverse_view @ np.asarray([0., 0., 0., 1.])
        if abs(eye_h[3]) < 1.e-20:
            raise ValueError('Eye camera origin is singular')
        eye = eye_h[:3]/eye_h[3]
        origin = np.broadcast_to(eye, near.shape)
        direction = near-origin
    else:
        origin = near
        direction = far-near
    length = np.linalg.norm(direction, axis=1)
    if np.any(length <= 0.) or not np.isfinite(length).all():
        raise ValueError('Invalid eye camera rays')
    direction = direction/length[:, None]
    near_distance = np.einsum('ij,ij->i', near-origin, direction)
    far_distance = np.einsum('ij,ij->i', far-origin, direction)
    if (not np.isfinite(near).all() or not np.isfinite(far).all()
            or np.any(far_distance <= near_distance)):
        raise ValueError('Invalid eye ray clipping interval')
    return origin, direction, near, near_distance, far_distance


def _opaque_bvh(geometry):
    import numpy as np
    from mathutils.bvhtree import BVHTree
    vertices = []
    triangles = []
    offset = 0
    for mesh in geometry.meshes:
        if not len(mesh.triangles):
            continue
        points = np.asarray(mesh.vertices, np.float64)
        faces = np.asarray(mesh.triangles, np.int64)
        if not np.isfinite(points).all():
            raise ValueError('Nonfinite opaque receiver geometry')
        vertices.extend(points.tolist())
        triangles.extend((faces+offset).tolist())
        offset += len(points)
    if not triangles:
        raise ValueError('Managed eye beauty requires opaque receiver triangles')
    return BVHTree.FromPolygons(vertices, triangles, all_triangles=True, epsilon=0.)


def _hits(row, ids, origins, directions, near_distance, far_distance):
    """Vectorized two-sided Moller-Trumbore on one triangle's candidate pixels."""
    import numpy as np
    a, b, c = row['world']
    e1, e2 = b-a, c-a
    direction = directions[ids]
    p = np.cross(direction, e2)
    determinant = p @ e1
    usable = abs(determinant) >= 1.e-20
    inverse = np.divide(1., determinant, out=np.zeros_like(determinant), where=usable)
    offset = origins[ids]-a
    u = np.einsum('ij,ij->i', offset, p)*inverse
    q = np.cross(offset, e1)
    v = np.einsum('ij,ij->i', direction, q)*inverse
    distance = (q @ e2)*inverse
    usable &= ((u >= -1.e-7) & (v >= -1.e-7) & (u+v <= 1.+1.e-7)
               & (distance > _RAY_EPSILON)
               & (distance >= near_distance[ids]-_RAY_EPSILON)
               & (distance <= far_distance[ids]+_RAY_EPSILON))
    selected = ids[usable]
    uv = row['uv'][0]*(1.-u[usable, None]-v[usable, None])
    uv += row['uv'][1]*u[usable, None]+row['uv'][2]*v[usable, None]
    return selected, distance[usable], uv


def _weights(descriptor, uv):
    import numpy as np
    settings = descriptor['settings']
    if descriptor['kind'] == 'SHADOW':
        # A white transmission layer is also an exact no-op.
        value = settings['opacity'] if any(v != 1. for v in settings['tint']) else 0.
        return np.full(len(uv), value, np.float64)
    radial = np.linalg.norm((uv-np.asarray(settings['center']))/np.asarray(settings['radius']), axis=1)
    feather = settings['feather']
    if feather == 0.:
        return (radial < 1.).astype(np.float64)*settings['opacity']
    t = np.clip((1.-radial)/feather, 0., 1.)
    return settings['opacity']*t*t*(3.-2.*t)


def resolve(base_rgba, geometry, frame, rows):
    """Resolve declared eye layers over a neutralized EEVEE opaque base.

    Returns a new premultiplied float32 RGBA array and a compact execution
    receipt. The alpha channel is preserved exactly. A visible effective eye
    fragment must have a ray-confirmed opaque receiver and base alpha near
    one; offscreen, fully occluded and zero-effect fragments do not modify it.
    The routine does not establish that the caller neutralized its base or
    independently qualify a native Blender render/visual result.
    """
    import numpy as np
    from mathutils import Vector
    width, height, mvp, inverse_mvp, inverse_view = _camera(frame, base_rgba)
    opaque, snapshot = eye_overlay_lines.prepare(geometry, rows)
    material_count = len(snapshot['materials'])
    output = base_rgba.copy()
    receipt = dict(schema=SCHEMA, ordering='physical-ray-distance',
        triangles=len(snapshot['triangles']), materials=material_count,
        shader_snapshot_sha256=snapshot['sha256'], complete_extent=[width, height],
        actual_pixels=0, changed_pixels=0, occluded_layer_hits=0, receiver_queries=0,
        opaque_receiver_required=True, alpha_preserved_exact=True,
        clipping='full-frame pixel centers; near-crossing triangles rejected; far ray interval applied',
        material_hit_policy='nearest camera hit per declared material; no folded or stacked same-material sheets',
        base_contract='caller supplied neutral white-Transparent eye layers over opaque EEVEE receivers',
        independent_blender_comparison='not_run', independent_visual_acceptance='not_run')
    if not material_count:
        return output, receipt
    projected = _projected_triangles(snapshot, mvp, width, height)
    if not projected:
        return output, receipt
    tiles = set()
    for row in projected:
        x0, y0, x1, y1 = row['bbox']
        tiles.update((tx, ty) for ty in range(y0//_TILE_SIDE, (y1-1)//_TILE_SIDE+1)
                     for tx in range(x0//_TILE_SIDE, (x1-1)//_TILE_SIDE+1))
    bvh = None
    alpha_tolerance = 4.*np.finfo(np.float32).eps
    receipt['opaque_alpha_tolerance'] = float(alpha_tolerance)
    for tx, ty in sorted(tiles, key=lambda pair: (pair[1], pair[0])):
        x0, y0 = tx*_TILE_SIDE, ty*_TILE_SIDE
        x1, y1 = min(width, x0+_TILE_SIDE), min(height, y0+_TILE_SIDE)
        xx, yy = np.meshgrid(np.arange(x0, x1, dtype=np.float64),
                             np.arange(y0, y1, dtype=np.float64))
        xx, yy = xx.ravel(), yy.ravel()
        origins, directions, near, near_distance, far_distance = _rays(
            xx, yy, width, height, inverse_mvp, inverse_view, frame['perspective'])
        count = len(xx)
        distances = np.full((material_count, count), np.inf, np.float64)
        uv = np.zeros((material_count, count, 2), np.float64)
        for row in projected:
            bx0, by0, bx1, by1 = row['bbox']
            if bx1 <= x0 or by1 <= y0 or bx0 >= x1 or by0 >= y1:
                continue
            ids = np.flatnonzero((xx >= bx0) & (xx < bx1) & (yy >= by0) & (yy < by1))
            selected, depth, hit_uv = _hits(row, ids, origins, directions, near_distance, far_distance)
            mid = row['material_index']
            closer = depth < distances[mid, selected]
            selected = selected[closer]
            distances[mid, selected] = depth[closer]
            uv[mid, selected] = hit_uv[closer]
        weights = np.zeros_like(distances)
        for mid, descriptor in enumerate(snapshot['materials']):
            weights[mid] = _weights(descriptor, uv[mid])
            distances[mid, weights[mid] <= 0.] = np.inf
        candidate = np.flatnonzero(np.isfinite(distances).any(axis=0))
        if not len(candidate):
            continue
        if bvh is None:
            bvh = _opaque_bvh(opaque)
        receiver = np.full(count, np.inf, np.float64)
        for index in candidate:
            limit = float(far_distance[index]-near_distance[index])
            hit, _normal, _face, distance = bvh.ray_cast(
                Vector(near[index].tolist()), Vector(directions[index].tolist()), limit)
            receipt['receiver_queries'] += 1
            if hit is None or distance is None:
                raise ValueError('Visible managed eye fragment has no opaque receiver')
            distance = float(distance)
            if not math.isfinite(distance) or distance < 0.:
                raise ValueError('Invalid opaque receiver ray distance')
            receiver[index] = near_distance[index]+distance
        visible = np.isfinite(distances) & (distances < receiver[None, :]-_RAY_EPSILON)
        receipt['occluded_layer_hits'] += int((np.isfinite(distances) & ~visible).sum())
        active = visible.any(axis=0)
        if not np.any(active):
            continue
        tile_base = base_rgba[y0:y1, x0:x1]
        alpha = tile_base[:, :, 3].reshape(-1).astype(np.float64)
        if np.any(abs(alpha[active]-1.) > alpha_tolerance):
            raise ValueError('Managed eye fragment requires opaque receiver alpha near one')
        rgb = tile_base[:, :, :3].reshape(-1, 3).astype(np.float64)
        # Invisible entries sort last but are masked at every ordinal. Stable
        # ties are declaration order; a single material has only one hit.
        order = np.argsort(-distances, axis=0, kind='stable')
        for ordinal in range(material_count):
            material_at_pixel = order[ordinal]
            for mid, descriptor in enumerate(snapshot['materials']):
                ids = np.flatnonzero((material_at_pixel == mid) & visible[mid])
                if not len(ids):
                    continue
                settings = descriptor['settings']
                if descriptor['kind'] == 'SHADOW':
                    transmission = 1.-settings['opacity']*(1.-np.asarray(settings['tint']))
                    rgb[ids] *= transmission
                else:
                    amount = weights[mid, ids, None]
                    # The admitted receiver alpha is essentially one. Keeping
                    # this factor also preserves strict premultiplication at
                    # its finite arithmetic boundary without changing alpha.
                    rgb[ids] = rgb[ids]*(1.-amount)+np.asarray(settings['color'])*amount*alpha[ids, None]
        new_rgb = rgb.reshape(y1-y0, x1-x0, 3).astype(np.float32)
        receipt['actual_pixels'] += int(active.sum())
        receipt['changed_pixels'] += int(np.any(new_rgb != tile_base[:, :, :3], axis=2).sum())
        output[y0:y1, x0:x1, :3] = new_rgb
    # This is a producer invariant, not a substitute for a render comparison.
    if not np.array_equal(output[:, :, 3], base_rgba[:, :, 3]):
        raise ValueError('Managed eye resolve unexpectedly changed alpha')
    return output, receipt
