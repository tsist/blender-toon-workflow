# SPDX-License-Identifier: GPL-3.0-or-later
"""Flat cyclic hair-distance nodes and a live head-relative direction carrier.

No color interface or existing HM parameter is reinterpreted.  The optional
extension replaces only the two consumers of the original HM lit_raw value.
"""
import hashlib
import json
import math

from . import hair_map_nodes as N

PREFIX = 'cyclic_sdf:'
CARRIER_PROFILE = 'toon-hair-sdf-direction.v1'
CARRIER_PROFILE_V2 = 'toon-hair-sdf-direction.v2'
MODE_V1 = 'cyclic_v1'
MODE_V2 = 'omni_v2'
RING_ELEVATION = math.pi / 6.
CARRIER_NODE = 'toon_hair_sdf_carrier_node'
EPSILON = 1.e-6
VERTICAL_FADE = .1
KEY_PHASES = [i / 8. for i in range(8)]


def _need(value, message):
    if not value:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _unit(value):
    length = math.sqrt(sum(v * v for v in value))
    _need(math.isfinite(length) and length > 1.e-10,
          'Hair SDF direction frame contains a zero or nonfinite axis')
    return [v / length for v in value]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _matrix(value):
    rows = [[float(v) for v in row] for row in value]
    _need(len(rows) == 4 and all(len(row) == 4 for row in rows)
          and all(math.isfinite(v) for row in rows for v in row),
          'Hair SDF reference/Sun matrix must be finite affine 4x4')
    _need(all(abs(rows[3][i] - (1. if i == 3 else 0.)) <= 1.e-7
              for i in range(4)), 'Hair SDF does not admit projective object transforms')
    columns = [[rows[j][i] for j in range(3)] for i in range(3)]
    a, b, c = columns
    det = (a[0] * (b[1] * c[2] - b[2] * c[1])
           - b[0] * (a[1] * c[2] - a[2] * c[1])
           + c[0] * (a[1] * b[2] - a[2] * b[1]))
    denominator = math.prod(math.sqrt(_dot(v, v)) for v in columns)
    _need(denominator > 1.e-25 and abs(det) / denominator > 1.e-8,
          'Hair SDF reference/Sun transform is singular')
    return columns


def direction_frame(reference_matrix, sun_matrix, *, mode=MODE_V1):
    """Pure oracle matching the carrier: GS +X/right and -Y/forward.

    The reference may have affine scale/shear.  A rigid positive Sun basis is
    required by the parent HM lighting policy.  Translation never changes yaw.
    """
    _need(mode in (MODE_V1, MODE_V2), 'Unsupported Hair SDF direction mode')
    ref, sun = _matrix(reference_matrix), _matrix(sun_matrix)
    right = _unit(ref[0])
    f0 = [-v for v in ref[1]]
    along = _dot(f0, right)
    forward = _unit([f0[i] - right[i] * along for i in range(3)])
    sun_units = [_unit(v) for v in sun]
    _need(all(abs(_dot(sun_units[i], sun_units[j])) <= 1.e-5
              for i, j in ((0, 1), (0, 2), (1, 2))),
          'Hair SDF Sun axes must remain orthogonal')
    a, b, c = sun_units
    cross = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
             a[0]*b[1]-a[1]*b[0]]
    _need(_dot(cross, c) > 0., 'Hair SDF Sun basis must be right handed')
    light = sun_units[2]
    x, y = _dot(light, right), _dot(light, forward)
    horizontal = math.hypot(x, y)
    phase = (math.atan2(x, y) / (2. * math.pi)) % 1. if horizontal > EPSILON else 0.
    if mode == MODE_V2:
        up = _unit([forward[1]*right[2]-forward[2]*right[1],
                    forward[2]*right[0]-forward[0]*right[2],
                    forward[0]*right[1]-forward[1]*right[0]])
        vertical = max(-1., min(1., _dot(light, up)))
        elevation = max(-math.pi/2., min(math.pi/2., math.atan2(vertical, horizontal)))
        return dict(phase=phase, valid=1., horizontal=horizontal, elevation=elevation,
                    right=right, forward=forward, up=up, sun_direction=light,
                    policy='GS +X/right,-Y/forward,up=forward cross right; atan2(up,horizontal) radians')
    t = max(0., min(1., horizontal / VERTICAL_FADE))
    valid = t * t * (3. - 2. * t) if horizontal > EPSILON else 0.
    return dict(phase=phase, valid=valid, horizontal=horizontal,
                right=right, forward=forward, sun_direction=light,
                policy='GS +X/right,-Y/forward; atan2(right,forward)/2pi modulo 1')


def distance_value(samples, phase, elevation=None, *, mode=MODE_V1):
    """Bpy-free interpolation oracle; smoothstep is applied once downstream."""
    _need(mode in (MODE_V1, MODE_V2), 'Unsupported Hair SDF distance mode')
    _need(len(samples) == (10 if mode == MODE_V2 else 8)
          and all(math.isfinite(float(v)) and 0. <= v <= 1. for v in samples)
          and math.isfinite(phase), 'Invalid Hair SDF distance samples/phase')
    p = phase % 1.
    ring = sum(value * max(1.-8.*abs(((p-i/8.+.5) % 1.)-.5), 0.)
               for i, value in enumerate(samples[:8]))
    if mode == MODE_V1:
        return ring
    _need(elevation is not None and math.isfinite(elevation), 'Omni SDF requires actual elevation')
    e = max(-math.pi/2., min(math.pi/2., elevation))
    if e <= RING_ELEVATION:
        weight = max(0., min(1., (e+math.pi/2.)/(2.*math.pi/3.)))
        return samples[9]*(1.-weight)+ring*weight
    weight = max(0., min(1., (e-RING_ELEVATION)/(math.pi/3.)))
    return ring*(1.-weight)+samples[8]*weight


def seam(group, *, extended=False):
    raw = N._socket(N._node(group, 'shadow_boundary:smooth').outputs, 'Value')
    destinations = [N._socket(N._node(group, 'lit_with_illumination').inputs, 'Value'),
                    N._socket(N._node(group, 'highlight_lit').inputs, 'Value_001')]
    if not extended:
        _need(len(raw.links) == 2 and all(len(s.links) == 1
              and s.links[0].from_socket == raw for s in destinations),
              'Hair SDF requires the two original exclusive lit_raw consumers')
    return raw, destinations


def extension_state(group):
    """Exact child footprint; parent HM parameters outside the seam may change."""
    state = N.structure(group)
    rows = [row for row in state['nodes'] if row['id'].startswith(PREFIX)]
    links = [row for row in state['links']
             if row[0].startswith(PREFIX) or row[2].startswith(PREFIX)]
    _need(rows, 'Hair SDF flat nodes are missing')
    return dict(nodes=rows, links=links)


def extension_signature(group):
    return hashlib.sha256(_json(extension_state(group)).encode()).hexdigest()


def shader_parameters(group):
    return {key: float(N._socket(N._node(group, PREFIX + key).outputs, 'Value').default_value)
            for key in ('strength', 'softness')}


def write_shader_parameters(group, strength, softness):
    for key, value in (('strength', strength), ('softness', softness)):
        N._socket(N._node(group, PREFIX + key).outputs, 'Value').default_value = value
    return shader_parameters(group)


def create_shader(group, images, uv_name, fields, strength, softness, *, styles=None, mode=MODE_V1):
    """Create only owned flat nodes; caller restores the original seam on error."""
    _need(mode in (MODE_V1, MODE_V2) and len(images) == (3 if mode == MODE_V2 else 2),
          'Hair SDF mode and atlas count differ')
    parent_owner = group[N.OWNER]
    raw, destinations = seam(group)
    created = []

    def node(key, kind, **properties):
        v = group.nodes.new(kind)
        created.append(v)
        v[N.NODE], v[N.OWNER] = PREFIX + key, parent_owner
        v.name, v.label = 'ToonHM/' + PREFIX + key, PREFIX + key
        for name, value in properties.items():
            setattr(v, name, value)
        return v

    def link(a, b):
        group.links.new(a, b)

    def math_(key, operation, a, b=None):
        v = node(key, 'ShaderNodeMath', operation=operation, use_clamp=False)
        for identifier, value in (('Value', a), ('Value_001', b)):
            if value is not None:
                if type(value) in (int, float):
                    N._socket(v.inputs, identifier).default_value = value
                else:
                    link(value, N._socket(v.inputs, identifier))
        return N._socket(v.outputs, 'Value')

    try:
        uv = node('uv', 'ShaderNodeUVMap', uv_map=uv_name, from_instancer=False)
        samples = []
        for i, image in enumerate(images):
            tex = node('atlas' + str(i), 'ShaderNodeTexImage', interpolation='Linear',
                       extension='EXTEND', projection='FLAT', projection_blend=0.)
            tex.image = image
            split = node('channels' + str(i), 'ShaderNodeSeparateColor', mode='RGB')
            link(N._socket(uv.outputs, 'UV'), N._socket(tex.inputs, 'Vector'))
            link(N._socket(tex.outputs, 'Color'), N._socket(split.inputs, 'Color'))
            samples.extend([N._socket(split.outputs, key) for key in ('Red', 'Green', 'Blue')])
            samples.append(N._socket(tex.outputs, 'Alpha'))
        phase = node('phase', 'ShaderNodeAttribute', attribute_type='GEOMETRY',
                     attribute_name=fields['phase'])
        validity = node('valid', 'ShaderNodeAttribute', attribute_type='GEOMETRY',
                        attribute_name=fields['valid'])
        params = {}
        for key, value in (('strength', strength), ('softness', softness)):
            v = node(key, 'ShaderNodeValue')
            N._socket(v.outputs, 'Value').default_value = value
            params[key] = N._socket(v.outputs, 'Value')
        contributions = []
        for i, sample in enumerate(samples[:8]):
            prefix = 'key' + str(i)
            delta = math_(prefix + ':delta', 'SUBTRACT', N._socket(phase.outputs, 'Fac'), i / 8.)
            shifted = math_(prefix + ':shift', 'ADD', delta, .5)
            wrapped = math_(prefix + ':wrap', 'FRACT', shifted)
            centered = math_(prefix + ':center', 'SUBTRACT', wrapped, .5)
            distance = math_(prefix + ':distance', 'ABSOLUTE', centered)
            scale = math_(prefix + ':scale', 'MULTIPLY', distance, 8.)
            weight = math_(prefix + ':weight', 'MAXIMUM',
                           math_(prefix + ':one_minus', 'SUBTRACT', 1., scale), 0.)
            contributions.append(math_(prefix + ':sample', 'MULTIPLY', sample, weight))
        distance = contributions[0]
        for i, value in enumerate(contributions[1:], 1):
            distance = math_('sum' + str(i), 'ADD', distance, value)
        if mode == MODE_V2:
            elevation = node('elevation', 'ShaderNodeAttribute', attribute_type='GEOMETRY',
                             attribute_name=fields['elevation'])
            e = math_('e_max', 'MINIMUM', N._socket(elevation.outputs, 'Fac'), math.pi/2.)
            e = math_('e_min', 'MAXIMUM', e, -math.pi/2.)
            lower = math_('lower_clamp', 'MINIMUM', math_('lower_max', 'MAXIMUM',
                math_('lower_unit', 'DIVIDE', math_('lower_shift', 'ADD', e, math.pi/2.), 2.*math.pi/3.), 0.), 1.)
            upper = math_('upper_clamp', 'MINIMUM', math_('upper_max', 'MAXIMUM',
                math_('upper_unit', 'DIVIDE', math_('upper_shift', 'SUBTRACT', e, RING_ELEVATION), math.pi/3.), 0.), 1.)
            ring_weight = math_('ring_weight', 'MINIMUM', lower,
                                math_('ring_upper', 'SUBTRACT', 1., upper))
            ring_part = math_('ring_part', 'MULTIPLY', distance, ring_weight)
            bottom_part = math_('bottom_part', 'MULTIPLY', samples[9],
                                math_('bottom_weight', 'SUBTRACT', 1., lower))
            top_part = math_('top_part', 'MULTIPLY', samples[8], upper)
            distance = math_('omni_distance', 'ADD',
                math_('ring_bottom', 'ADD', ring_part, bottom_part), top_part)
        low = math_('low', 'SUBTRACT', .5, params['softness'])
        high = math_('high', 'ADD', .5, params['softness'])
        width = math_('width', 'SUBTRACT', high, low)
        shifted = math_('shift', 'SUBTRACT', distance, low)
        unit = math_('divide', 'DIVIDE', shifted, width)
        unit = math_('unit_min', 'MAXIMUM', unit, 0.)
        unit = math_('unit_max', 'MINIMUM', unit, 1.)
        square = math_('square', 'MULTIPLY', unit, unit)
        poly = math_('polynomial', 'SUBTRACT', 3., math_('double', 'MULTIPLY', unit, 2.))
        shade = math_('shade', 'MULTIPLY', square, poly)
        mix = math_('mix', 'MULTIPLY', params['strength'], N._socket(validity.outputs, 'Fac'))
        original = math_('old', 'MULTIPLY', raw, math_('one_minus_mix', 'SUBTRACT', 1., mix))
        authored = math_('authored', 'MULTIPLY', shade, mix)
        effective = math_('effective', 'ADD', original, authored)
        for destination in destinations:
            link(effective, destination)
        _need(len(group.nodes) <= 256, 'Hair SDF would exceed the HM 256-node contract')
        for i, v in enumerate(created):
            v.location = ((i % 8) * 220., -2900. - (i // 8) * 180.)
            if styles and v[N.NODE] in styles:
                for key, value in styles[v[N.NODE]].items():
                    setattr(v, key, value)
        extension_state(group)
        return created
    except BaseException:
        for v in reversed(created):
            group.nodes.remove(v)
        for destination in destinations:
            group.links.new(raw, destination)
        raise


def carrier_signature(group):
    from . import hair_map as H
    value = H._carrier_shape(group)
    value['mutes'] = [(n.name, bool(n.mute), n.get(CARRIER_NODE)) for n in group.nodes]
    return hashlib.sha256(_json(value).encode()).hexdigest()


def build_carrier(target, reference, sun, owner, fields, *, mode=MODE_V1):
    """An affine-reference GN carrier; no source attribute or geometry is edited."""
    import bpy
    _need(mode in (MODE_V1, MODE_V2), 'Unsupported Hair SDF carrier mode')
    group = bpy.data.node_groups.new('Toon Hair SDF Direction ' + owner[:8], 'GeometryNodeTree')
    modifier = None
    try:
        a = group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
        b = group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

        def node(key, kind, **props):
            v = group.nodes.new(kind)
            v.name, v.label, v[CARRIER_NODE] = 'HairSDF/' + key, key, key
            for name, value in props.items():
                setattr(v, name, value)
            return v

        def link(x, y):
            group.links.new(x, y)

        def math_(key, op, x, y=None):
            v = node(key, 'ShaderNodeMath', operation=op, use_clamp=False)
            for socket, value in zip(v.inputs[:2], (x, y)):
                if value is None:
                    continue
                if type(value) in (int, float):
                    socket.default_value = value
                else:
                    link(value, socket)
            return N._socket(v.outputs, 'Value')

        def vector(key, op, x, y=None):
            v = node(key, 'ShaderNodeVectorMath', operation=op)
            link(x, N._socket(v.inputs, 'Vector'))
            if y is not None:
                link(y, N._socket(v.inputs, 'Scale' if op == 'SCALE' else 'Vector_001'))
            return N._socket(v.outputs, 'Value' if op in {'DOT_PRODUCT', 'LENGTH'} else 'Vector')

        def direction(key, transform, axis):
            v = node(key, 'FunctionNodeTransformDirection')
            N._socket(v.inputs, 'Direction').default_value = axis
            link(transform, N._socket(v.inputs, 'Transform'))
            return N._socket(v.outputs, 'Direction')

        entry = node('input', 'NodeGroupInput')
        output = node('output', 'NodeGroupOutput')
        rinfo = node('reference', 'GeometryNodeObjectInfo', transform_space='ORIGINAL')
        linfo = node('sun', 'GeometryNodeObjectInfo', transform_space='ORIGINAL')
        N._socket(rinfo.inputs, 'Object').default_value = reference
        N._socket(linfo.inputs, 'Object').default_value = sun
        right = vector('right', 'NORMALIZE',
                       direction('right_raw', N._socket(rinfo.outputs, 'Transform'), (1., 0., 0.)))
        f0 = direction('forward_raw', N._socket(rinfo.outputs, 'Transform'), (0., -1., 0.))
        along = vector('forward_right', 'DOT_PRODUCT', f0, right)
        forward = vector('forward', 'NORMALIZE',
                         vector('orthogonal_forward', 'SUBTRACT', f0,
                                vector('right_projection', 'SCALE', right, along)))
        light = vector('light', 'NORMALIZE',
                       direction('light_raw', N._socket(linfo.outputs, 'Transform'), (0., 0., 1.)))
        x = vector('right_dot', 'DOT_PRODUCT', light, right)
        y = vector('forward_dot', 'DOT_PRODUCT', light, forward)
        h = math_('horizontal', 'SQRT', math_('h2', 'ADD',
                  math_('x2', 'MULTIPLY', x, x), math_('y2', 'MULTIPLY', y, y)))
        usable = math_('usable', 'GREATER_THAN', h, EPSILON)
        angle = math_('angle', 'ARCTAN2', x, y)
        raw = math_('phase_raw', 'FRACT', math_('angle_unit', 'DIVIDE', angle, 2. * math.pi))
        phase = math_('phase', 'MULTIPLY', raw, usable)
        if mode == MODE_V2:
            up = vector('up', 'NORMALIZE', vector('up_cross', 'CROSS_PRODUCT', forward, right))
            z = vector('up_dot', 'DOT_PRODUCT', light, up)
            z = math_('z_min', 'MAXIMUM', math_('z_max', 'MINIMUM', z, 1.), -1.)
            elevation = math_('elevation', 'ARCTAN2', z, h)
            elevation = math_('elevation_min', 'MAXIMUM',
                math_('elevation_max', 'MINIMUM', elevation, math.pi/2.), -math.pi/2.)
            valid = math_('valid', 'ADD', 1., 0.)
            stores = (('phase', phase), ('valid', valid), ('elevation', elevation))
        else:
            t = math_('vertical_unit', 'MINIMUM',
                      math_('vertical_min', 'MAXIMUM', math_('vertical_divide', 'DIVIDE', h, VERTICAL_FADE), 0.), 1.)
            valid = math_('valid', 'MULTIPLY', usable,
                         math_('valid_smooth', 'MULTIPLY', math_('valid_square', 'MULTIPLY', t, t),
                               math_('valid_poly', 'SUBTRACT', 3., math_('valid_double', 'MULTIPLY', t, 2.))))
            stores = (('phase', phase), ('valid', valid))
        geometry = N._socket(entry.outputs, a.identifier)
        for key, value in stores:
            store = node('store_' + key, 'GeometryNodeStoreNamedAttribute', data_type='FLOAT', domain='POINT')
            N._socket(store.inputs, 'Name').default_value = fields[key]
            link(geometry, N._socket(store.inputs, 'Geometry'))
            link(value, N._socket(store.inputs, 'Value'))
            geometry = N._socket(store.outputs, 'Geometry')
        link(geometry, N._socket(output.inputs, b.identifier))
        group['profile'], group['owner'] = (CARRIER_PROFILE_V2 if mode == MODE_V2 else CARRIER_PROFILE), owner
        group['target'], group['reference'], group['sun'] = target, reference, sun
        group['fields'] = _json(fields)
        modifier = target.modifiers.new('Toon Hair SDF Direction', 'NODES')
        modifier.node_group = group
        with bpy.context.temp_override(object=target, active_object=target,
               selected_objects=[target], selected_editable_objects=[target]):
            _need(bpy.ops.object.modifier_move_to_index(modifier=modifier.name, index=0) == {'FINISHED'},
                  'Could not place Hair SDF direction carrier before correction')
        group['uid'] = modifier.persistent_uid
        group['signature'] = carrier_signature(group)
        return modifier, group
    except BaseException:
        if modifier is not None:
            target.modifiers.remove(modifier)
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        raise
