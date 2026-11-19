"""Explicit placement of complete post-style screen ribbons; no bpy or I/O.

Only carrier vertex depths change. Source samples, source-u, depth fade,
screen coordinates, caps, joins and triangle topology stay with their caller.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math

from .contract import WorkflowError


def _fail(message, path='$.lines.placement.camera_depth'):
    raise WorkflowError('UNSUPPORTED', 'INVALID_PLACEMENT_DEPTH: ' + message, path)


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _margin(depth, near):
    return 1e-4 * max(1.0, depth, near)


def validate_plane_depth(placement, camera):
    """Return an admitted plane depth, or None for exact native placement.

    This also validates camera-plane declarations for empty style buckets.
    It does not establish that other scene geometry lies behind the plane.
    """
    if placement == {'mode': 'native_depth'}:
        return None
    if (not isinstance(placement, dict) or set(placement) != {'mode', 'camera_depth'}
            or placement['mode'] != 'camera_plane'):
        _fail('Expected native_depth or camera_plane with explicit camera_depth',
              '$.lines.placement')
    depth = placement['camera_depth']
    if not _finite_number(depth) or depth <= 0:
        _fail('camera_depth must be a finite positive number')
    if (not isinstance(camera, dict) or not _finite_number(camera.get('near'))
            or not _finite_number(camera.get('far'))):
        _fail('Actual camera near/far values must be finite numbers')
    near, far = camera['near'], camera['far']
    if not 0 < near < far:
        _fail('Actual camera near/far planes must be positive and ordered')
    margin = _margin(depth, near)
    if not (depth - near > margin and far - depth > margin):
        _fail('camera_depth must be strictly inside actual near/far planes by more than '
              '1e-4 * max(1, camera_depth, near); no clamping is performed')
    return depth


def place_spec(spec, placement, camera):
    """Place complete ribbon triangles after style/width/cap construction.

    Native placement returns the original spec object. Camera-plane placement
    returns a shallow spec copy with new vertex lists; all other fields keep
    their original values and references. No input object is mutated.
    """
    depth = validate_plane_depth(placement, camera)
    if depth is None:
        return spec
    if not isinstance(spec, dict) or not isinstance(spec.get('vertices'), (list, tuple)):
        _fail('Expected a screen ribbon spec with complete vertices')
    margin = _margin(depth, camera['near'])
    vertices = []
    for index, vertex in enumerate(spec['vertices']):
        if (not isinstance(vertex, (list, tuple)) or len(vertex) != 3
                or any(not _finite_number(value) for value in vertex)):
            _fail('Original ribbon vertex %d must contain finite x, y and depth' % index)
        if not depth + margin < vertex[2]:
            _fail('camera_depth plus its safety margin must be strictly less than every '
                  'original ribbon vertex depth; vertex %d would not move forward safely' % index)
        vertices.append([vertex[0], vertex[1], depth])
    return {**spec, 'vertices': vertices}
