# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit autonomous axial source RGBA depth; never read GPU arbitration Z."""
import math
import struct

PROFILE = 'autonomous_axial_source_rgba_depth.v1'


def _float32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def normalize(value=None):
    value = {'z_min': 1., 'z_max': 3.} if value is None else value
    if type(value) is not dict or set(value)-{'z_min', 'z_max', 'profile'}:
        raise ValueError('Unknown depth output fields')
    if value.get('profile', PROFILE) != PROFILE:
        raise ValueError('Unsupported depth output profile')
    result = {}
    for key, default in [('z_min', 1.), ('z_max', 3.)]:
        number = value.get(key, default)
        if type(number) not in (int, float):
            raise ValueError('Depth '+key+' must be a finite scalar')
        try:
            valid = math.isfinite(number) and .01 <= number <= 100000
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('Depth '+key+' must be within .01..100000')
        result[key] = float(number)
    if not result['z_min'] < result['z_max']:
        raise ValueError('Depth z_min must be smaller than z_max')
    if not _float32(result['z_min']) < _float32(result['z_max']):
        raise ValueError('Depth range collapses in GPU float32 precision')
    result['profile'] = PROFILE
    return result


def parameters(element, frame):
    """Build one camera-space plane from an explicit captured view matrix."""
    if element['type'] == 'COLOR':
        return None
    if element['type'] != 'DEPTH':
        raise ValueError('Unknown element depth mode')
    depth = normalize(element['depth'])
    row = frame['view'][2]
    axis = tuple(-float(row[i]) for i in range(4))
    if not all(math.isfinite(v) and abs(v) <= 3.4028234663852886e38 for v in axis):
        raise ValueError('Depth camera-space axis must fit GPU float32')
    return dict(axis=axis, range=(depth['z_min'], depth['z_max'], 1., 0.))


def shader_parameters(value):
    """Validate the optional renderer boundary once, before any GPU work."""
    if value is None:
        return (0., 0., 0., 0.), (1., 3., 0., 0.)
    if type(value) is not dict or set(value) != {'axis', 'range'}:
        raise ValueError('Explicit depth shader parameters required')
    axis, bounds = value['axis'], value['range']
    if (type(axis) not in (list, tuple) or len(axis) != 4
            or type(bounds) not in (list, tuple) or len(bounds) != 4):
        raise ValueError('Depth shader vectors must have four values')
    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 3.4028234663852886e38
           for v in list(axis)+list(bounds)):
        raise ValueError('Depth shader parameters must fit GPU float32')
    normalize(dict(z_min=bounds[0], z_max=bounds[1]))
    if bounds[2] != 1 or bounds[3] != 0:
        raise ValueError('Invalid depth shader mode')
    return tuple(axis), tuple(bounds)
