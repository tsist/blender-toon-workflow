# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit direction-key face reader extension (v4).

LRUD stays the base scalar. Two model-authored signed-distance channels are
combined before the existing single comparison. The top contribution is
guarded by the *same* filtered top source that supplies the LRUD reader.
"""

# d is signed distance in the common 640-pixel model projection. The shader
# consumes a phase offset d/80, not recovered pixel distance d. Thus
# (2E-1)*8 = (d/640)*8 = d/80. The two scales are intentional.
ENCODING = 'LRUD+signed_RG_keys;top_source_guard;polar_angle/pi.v1'
SOURCE_ENCODING = 'R=upper;G=top;signed_model_distance;E=.5+.5*d/640'
DECODE_SCALE = 8.0


def node_types():
    result = {'direction_uv': 'ShaderNodeUVMap',
              'direction_guard_smooth': 'ShaderNodeMapRange'}
    for role in ('direction_upper', 'direction_top'):
        result['direction_attr_' + role] = 'ShaderNodeAttribute'
    for i in range(9):
        result['direction_texture_' + str(i)] = 'ShaderNodeTexImage'
        result['direction_separate_' + str(i)] = 'ShaderNodeSeparateColor'
        if i != 4:
            result['direction_uv_offset_' + str(i)] = 'ShaderNodeVectorMath'
    for side in ('upper', 'top'):
        result.update({'direction_' + side + '_weight_' + str(i): 'ShaderNodeMath' for i in range(9)})
        result.update({'direction_' + side + '_sum_' + str(i): 'ShaderNodeMath' for i in range(8)})
        result.update({'direction_' + side + '_' + k: 'ShaderNodeMath' for k in ('twice', 'signed', 'decoded')})
    result.update({'direction_' + key: 'ShaderNodeMath' for key in (
        'guard_min', 'guard_twice_softness', 'guard_max', 'guard_hard',
        'guard_hard_weight', 'guard_hard_part', 'guard_soft_part', 'guard',
        'enabled_upper', 'enabled_top', 'sum_weights', 'denominator', 'upper_weight', 'guarded_top', 'top_weight',
        'combined_weight', 'base_weight', 'base_part', 'phase_part',
        'upper_part', 'top_part', 'base_phase', 'distance_part', 'scalar')})
    return result


def build(group, fields, image, source, *, direction_correction=True):
    from . import face_sdf as S, sdf_filter
    direction_correction = S._direction_correction(direction_correction, direction=True)
    by = {n.get(S._NODE): n for n in group.nodes}

    def node(key, kind, **props):
        value = group.nodes.new(kind)
        value[S._NODE] = key
        for name, item in props.items():
            setattr(value, name, item)
        by[key] = value
        return value

    def math_(key, operation, a, b):
        value = node('direction_' + key, 'ShaderNodeMath', operation=operation)
        for socket, item in zip(value.inputs, (a, b)):
            if type(item) in (int, float):
                socket.default_value = item
            else:
                group.links.new(item, socket)
        return value.outputs['Value']

    uv = node('direction_uv', 'ShaderNodeUVMap', uv_map=source['uv']['uv_name'])
    step_u, step_v = source['sampling']['uv_step']
    for i, (dx, dy, _n) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        vector = uv.outputs['UV']
        if i != 4:
            offset = node('direction_uv_offset_' + str(i), 'ShaderNodeVectorMath', operation='ADD')
            group.links.new(vector, offset.inputs[0])
            offset.inputs[1].default_value = (dx * step_u, dy * step_v, 0.)
            vector = offset.outputs['Vector']
        texture = node('direction_texture_' + str(i), 'ShaderNodeTexImage', image=image,
                       interpolation='Linear', extension='EXTEND', projection='FLAT')
        group.links.new(vector, texture.inputs['Vector'])
        separate = node('direction_separate_' + str(i), 'ShaderNodeSeparateColor', mode='RGB')
        group.links.new(texture.outputs['Color'], separate.inputs['Color'])

    distances = {}
    for side, channel in (('upper', 'Red'), ('top', 'Green')):
        weights = [math_(side + '_weight_' + str(i), 'MULTIPLY',
                        by['direction_separate_' + str(i)].outputs[channel], n / 16.)
                   for i, (_dx, _dy, n) in enumerate(sdf_filter.KERNEL_WEIGHTS)]
        total = weights[0]
        for i in range(1, 9):
            total = math_(side + '_sum_' + str(i - 1), 'ADD', total, weights[i])
        distances[side] = math_(side + '_decoded', 'MULTIPLY',
            math_(side + '_signed', 'SUBTRACT', math_(side + '_twice', 'MULTIPLY', total, 2.), 1.),
            source['decode_scale'])
    attrs = {}
    for role in ('direction_upper', 'direction_top'):
        value = node('direction_attr_' + role, 'ShaderNodeAttribute',
                     attribute_type='GEOMETRY', attribute_name=fields[role])
        attrs[role] = value.outputs['Fac']
    softness = by['softness_max'].outputs['Value']
    positive = by['softness_positive'].outputs['Value']
    top = by['polar_top_sum_7'].outputs['Value']
    low = math_('guard_min', 'ADD', .5, softness)
    high = math_('guard_max', 'ADD', .5, math_('guard_twice_softness', 'MULTIPLY', 2., softness))
    smooth = node('direction_guard_smooth', 'ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
    group.links.new(top, smooth.inputs['Value'])
    group.links.new(low, smooth.inputs['From Min'])
    group.links.new(high, smooth.inputs['From Max'])
    smooth.inputs['To Min'].default_value = 0.
    smooth.inputs['To Max'].default_value = 1.
    hard = math_('guard_hard', 'GREATER_THAN', top, .5)
    guard = math_('guard', 'ADD',
        math_('guard_soft_part', 'MULTIPLY', smooth.outputs['Result'], positive),
        math_('guard_hard_part', 'MULTIPLY', hard,
              math_('guard_hard_weight', 'SUBTRACT', 1., positive)))
    # A shared carrier can have explicitly independent readers: disabled nose
    # correction retains the exact LRUD scalar rather than changing the carrier.
    enabled = float(direction_correction)
    upper = math_('enabled_upper', 'MULTIPLY', attrs['direction_upper'], enabled)
    top_weight = math_('enabled_top', 'MULTIPLY', attrs['direction_top'], enabled)
    denominator = math_('denominator', 'MAXIMUM', 1.,
        math_('sum_weights', 'ADD', upper, top_weight))
    u = math_('upper_weight', 'DIVIDE', upper, denominator)
    v = math_('top_weight', 'DIVIDE',
        math_('guarded_top', 'MULTIPLY', top_weight, guard), denominator)
    weight = math_('combined_weight', 'ADD', u, v)
    base = math_('base_part', 'MULTIPLY', by['polar_scalar'].outputs['Value'],
                 math_('base_weight', 'SUBTRACT', 1., weight))
    phase = math_('phase_part', 'MULTIPLY', weight, by['attr_phase'].outputs['Fac'])
    distance = math_('distance_part', 'ADD',
        math_('upper_part', 'MULTIPLY', u, distances['upper']),
        math_('top_part', 'MULTIPLY', v, distances['top']))
    scalar = math_('scalar', 'ADD', math_('base_phase', 'ADD', base, phase), distance)
    group.links.new(scalar, by['hard_lit'].inputs[0])
    group.links.new(scalar, by['smooth_lit'].inputs['Value'])


def validate(group, by, info, *, top_source=None, distance_source=None, upper_source=None):
    from . import face_sdf as S, sdf_filter
    source = info['direction_source']
    uv = by['direction_uv']
    if uv.uv_map != source['uv']['uv_name']:
        raise S.FaceSDFError('Direction source actual UV differs')
    image = by['direction_texture_4'].image
    if image is None or image in (by['left_texture'].image, by['vertical_texture_4'].image):
        raise S.FaceSDFError('Direction distance source must be a third actual Image ID')
    step_u, step_v = source['sampling']['uv_step']
    for i, (dx, dy, _n) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        texture = by['direction_texture_' + str(i)]
        if (texture.image != image or texture.interpolation != 'Linear'
                or texture.extension != 'EXTEND' or texture.projection != 'FLAT'):
            raise S.FaceSDFError('Direction source actual Image/sampling differs')
        if i == 4:
            S._expect_link(texture.inputs['Vector'], uv, 'UV', 'Direction center UV')
        else:
            offset = by['direction_uv_offset_' + str(i)]
            if offset.operation != 'ADD':
                raise S.FaceSDFError('Direction UV offset must be ADD')
            S._expect_link(offset.inputs[0], uv, 'UV', 'Direction UV base')
            S._expect_default(offset.inputs[1], (dx * step_u, dy * step_v, 0.), 'Direction actual texel step')
            S._expect_link(texture.inputs['Vector'], offset, 'Vector', 'Direction UV tap')
        separate = by['direction_separate_' + str(i)]
        if separate.mode != 'RGB':
            raise S.FaceSDFError('Direction channels must be RGB')
        S._expect_link(separate.inputs['Color'], texture, 'Color', 'Direction tap color')
    for role in ('direction_upper', 'direction_top'):
        node = by['direction_attr_' + role]
        if node.attribute_type != 'GEOMETRY' or node.attribute_name != info['fields'][role]:
            raise S.FaceSDFError('Direction field identity differs: ' + role)

    def edge(key, operation, a, b):
        node = by['direction_' + key]
        if node.operation != operation:
            raise S.FaceSDFError('Direction operation differs: ' + key)
        for i, item in enumerate((a, b)):
            if type(item) in (int, float):
                S._expect_default(node.inputs[i], item, 'Direction ' + key)
            else:
                S._expect_link(node.inputs[i], by[item[0]], item[1], 'Direction ' + key)
        return ('direction_' + key, 'Value')

    distances = {}
    for side, channel in (('upper', 'Red'), ('top', 'Green')):
        weights = [edge(side + '_weight_' + str(i), 'MULTIPLY',
                        ('direction_separate_' + str(i), channel), n / 16.)
                   for i, (_dx, _dy, n) in enumerate(sdf_filter.KERNEL_WEIGHTS)]
        total = weights[0]
        for i in range(1, 9):
            total = edge(side + '_sum_' + str(i - 1), 'ADD', total, weights[i])
        distances[side] = edge(side + '_decoded', 'MULTIPLY',
            edge(side + '_signed', 'SUBTRACT', edge(side + '_twice', 'MULTIPLY', total, 2.), 1.),
            source['decode_scale'])
    softness = ('softness_max', 'Value')
    positive = ('softness_positive', 'Value')
    top = ('polar_top_sum_7', 'Value')
    low = edge('guard_min', 'ADD', .5, softness)
    high = edge('guard_max', 'ADD', .5, edge('guard_twice_softness', 'MULTIPLY', 2., softness))
    smooth = by['direction_guard_smooth']
    if smooth.interpolation_type != 'SMOOTHSTEP' or not smooth.clamp:
        raise S.FaceSDFError('Direction top guard must be clamped SMOOTHSTEP')
    for socket, item in (('Value', top), ('From Min', low), ('From Max', high)):
        S._expect_link(smooth.inputs[socket], by[item[0]], item[1], 'Direction top guard')
    S._expect_default(smooth.inputs['To Min'], 0., 'Direction guard output min')
    S._expect_default(smooth.inputs['To Max'], 1., 'Direction guard output max')
    hard = edge('guard_hard', 'GREATER_THAN', top, .5)
    guard = edge('guard', 'ADD',
        edge('guard_soft_part', 'MULTIPLY', ('direction_guard_smooth', 'Result'), positive),
        edge('guard_hard_part', 'MULTIPLY', hard, edge('guard_hard_weight', 'SUBTRACT', 1., positive)))
    enabled = float(info['direction_correction'])
    upper = edge('enabled_upper', 'MULTIPLY', ('direction_attr_direction_upper', 'Fac') if upper_source is None else upper_source, enabled)
    top_weight = edge('enabled_top', 'MULTIPLY', ('direction_attr_direction_top', 'Fac'), enabled)
    denominator = edge('denominator', 'MAXIMUM', 1., edge('sum_weights', 'ADD', upper, top_weight))
    u = edge('upper_weight', 'DIVIDE', upper, denominator)
    v = edge('top_weight', 'DIVIDE', edge('guarded_top', 'MULTIPLY', top_weight, guard), denominator)
    weight = edge('combined_weight', 'ADD', u, v)
    base = edge('base_part', 'MULTIPLY', ('polar_scalar', 'Value'), edge('base_weight', 'SUBTRACT', 1., weight))
    phase = edge('phase_part', 'MULTIPLY', weight, ('attr_phase', 'Fac'))
    distance = edge('distance_part', 'ADD', edge('upper_part', 'MULTIPLY', u, distances['upper']),
                    edge('top_part', 'MULTIPLY', v, distances['top'] if top_source is None else top_source))
    scalar = edge('scalar', 'ADD', edge('base_phase', 'ADD', base, phase),
                  distance if distance_source is None else distance_source)
    S._expect_link(by['hard_lit'].inputs[0], by[scalar[0]], scalar[1], 'Direction single hard comparison')
    S._expect_link(by['smooth_lit'].inputs['Value'], by[scalar[0]], scalar[1], 'Direction single soft comparison')
