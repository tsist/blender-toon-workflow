# SPDX-License-Identifier: GPL-3.0-or-later
"""World triangle to CORNER-UV affine planes, without Blender dependencies.

Rows are ``(u.xyz, u.offset), (v.xyz, v.offset)`` in float32. The
minimum-norm gradient extends UV off the triangle plane by Euclidean
orthogonal projection. It neither clamps barycentrics nor merges UV charts.

Numerical admission is relative to the float64 SVD: s_min must exceed
64 * eps64 * s_max. This is a rank guard, not a float32/GPU accuracy promise.
Finite float32 coefficients (including subnormals) are accepted; overflow
and a nonzero coefficient rounding to zero are rejected. Large world origins
can still cause cancellation: inspect diagnostics or validate actual sampling.
"""
import numpy as np

_RANK_FACTOR = 64.0


def _real64(value, label):
    try:
        source = np.asarray(value)
        if source.dtype.kind not in 'fiu':
            raise ValueError(label + ' must contain real numeric values')
        result = np.asarray(source, dtype=np.float64)
    except (TypeError, OverflowError, ValueError) as error:
        raise ValueError(label + ' must contain real numeric values') from error
    if not np.isfinite(result).all():
        raise ValueError(label + ' must be finite')
    return result


def _inputs(xyz, uv):
    xyz = _real64(xyz, 'xyz')
    uv = _real64(uv, 'uv')
    if xyz.ndim != 3 or xyz.shape[1:] != (3, 3):
        raise ValueError('xyz must have shape (N, 3, 3)')
    if uv.shape != (len(xyz), 3, 2):
        raise ValueError('uv must have shape (N, 3, 2) matching xyz')
    return xyz, uv


def _edge_svd(xyz):
    with np.errstate(over='ignore', invalid='ignore'):
        edges = (xyz[:, 1:] - xyz[:, :1]).transpose(0, 2, 1)
    if not np.isfinite(edges).all():
        raise ValueError('Triangle edge differences exceed float64 range')
    scale = np.max(np.abs(edges), axis=(1, 2))
    if np.any(scale == 0):
        raise ValueError('Degenerate triangle has zero edge scale')
    try:
        u, singular, vh = np.linalg.svd(edges / scale[:, None, None], full_matrices=False)
    except np.linalg.LinAlgError as error:
        raise ValueError('Triangle SVD did not converge') from error
    if np.any(singular[:, 1] <= _RANK_FACTOR * np.finfo(np.float64).eps * singular[:, 0]):
        raise ValueError('Degenerate or numerically rank-deficient triangle')
    return scale, u, singular, vh


def affines(xyz, uv):
    """Return independent float32 (N, 2, 4) world-to-UV planes.

    Source arrays are only read. UV may be mirrored, outside [0, 1], or
    constant; only the world triangle must have numerical rank two.
    """
    xyz, uv = _inputs(xyz, uv)
    if not len(xyz):
        return np.empty((0, 2, 4), dtype=np.float32)
    scale, u, singular, vh = _edge_svd(xyz)
    # Scale before SVD avoids the squared condition number of a Gram solve.
    inverse = ((vh.transpose(0, 2, 1) / singular[:, None, :]) @ u.transpose(0, 2, 1))
    with np.errstate(over='ignore', under='ignore', invalid='ignore', divide='ignore'):
        gradient = ((uv[:, 1:] - uv[:, :1]).transpose(0, 2, 1) @ inverse) / scale[:, None, None]
        offset = uv[:, 0] - np.einsum('nij,nj->ni', gradient, xyz[:, 0])
        result64 = np.concatenate((gradient, offset[:, :, None]), axis=2)
        result = result64.astype(np.float32)
    if not np.isfinite(result64).all() or not np.isfinite(result).all():
        raise ValueError('UV affine coefficients exceed finite float32 range')
    if np.any((result64 != 0) & (result == 0)):
        raise ValueError('Nonzero UV affine coefficient underflows float32')
    return result


def apply_affines(rows, points):
    """CPU storage oracle: evaluate stored planes with float64 arithmetic.

    points has shape (N, ..., 3); output has shape (N, ..., 2). This deliberately
    does not claim to emulate a GPU's float32 input, dot order, FMA or sampling.
    """
    rows = _real64(rows, 'rows')
    points = _real64(points, 'points')
    if rows.ndim != 3 or rows.shape[1:] != (2, 4):
        raise ValueError('rows must have shape (N, 2, 4)')
    if points.ndim < 2 or points.shape[0] != len(rows) or points.shape[-1] != 3:
        raise ValueError('points must have shape (N, ..., 3) matching rows')
    shape = (len(rows),) + (1,) * (points.ndim - 2) + (2,)
    with np.errstate(over='ignore', invalid='ignore'):
        result = np.einsum('nij,n...j->n...i', rows[:, :, :3], points) + rows[:, :, 3].reshape(shape)
    if not np.isfinite(result).all():
        raise ValueError('UV affine evaluation is nonfinite')
    return result


def diagnostics(xyz, uv, rows=None):
    """Report conditioning and stored-plane corner error; do not grade accuracy."""
    xyz, uv = _inputs(xyz, uv)
    rows = affines(xyz, uv) if rows is None else rows
    observed = apply_affines(rows, xyz)
    if not len(xyz):
        return dict(triangles=0, rank_relative_cutoff=_RANK_FACTOR * np.finfo(np.float64).eps,
                    maximum_edge_condition=0.0, maximum_corner_storage_error=0.0)
    _, _, singular, _ = _edge_svd(xyz)
    return dict(triangles=len(xyz), rank_relative_cutoff=_RANK_FACTOR * np.finfo(np.float64).eps,
                maximum_edge_condition=float(np.max(singular[:, 0] / singular[:, 1])),
                maximum_corner_storage_error=float(np.max(np.abs(observed - uv))))
