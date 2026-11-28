# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit LRUD threshold selection for face shader v3.

The v2 direction carrier supplies the polar angle and meridian weight. R is
the existing horizontal threshold (mirrored only on its right branch). A
distinct vertical Image/UV supplies G/top and B/bottom without mirroring.
All three scalar fields are filtered in texture space, mixed BEFORE one
comparison, and feed the existing single ADD-only darkening stage.
"""

ENCODING = 'R=horizontal;vertical.GB=top_bottom;independent_UV;polar_angle/pi.v2'


def node_types():
    result = {'polar_attr_' + role: 'ShaderNodeAttribute'
              for role in ('vertical_side', 'vertical_weight')}
    result['vertical_uv'] = 'ShaderNodeUVMap'
    for i in range(9):
        result['vertical_texture_' + str(i)] = 'ShaderNodeTexImage'
        result['vertical_separate_' + str(i)] = 'ShaderNodeSeparateColor'
        if i != 4:
            result['vertical_uv_offset_' + str(i)] = 'ShaderNodeVectorMath'
    for side in ('top', 'bottom'):
        result.update({'polar_' + side + '_weight_' + str(i): 'ShaderNodeMath' for i in range(9)})
        result.update({'polar_' + side + '_sum_' + str(i): 'ShaderNodeMath' for i in range(8)})
    result.update({'polar_' + key: 'ShaderNodeMath' for key in (
        'bottom_weight', 'top_part', 'bottom_part', 'vertical_scalar',
        'horizontal_weight', 'horizontal_part', 'vertical_part', 'scalar')})
    return result


def build(group, fields, vertical_image, vertical_source):
    from . import face_sdf as S, sdf_filter
    by = {node.get(S._NODE): node for node in group.nodes}

    def node_(key, kind, **properties):
        value = group.nodes.new(kind)
        value[S._NODE] = key
        for name, item in properties.items():
            setattr(value, name, item)
        by[key] = value
        return value

    uv = node_('vertical_uv', 'ShaderNodeUVMap', uv_map=vertical_source['uv']['uv_name'])
    step_u, step_v = vertical_source['sampling']['uv_step']
    for i, (dx, dy, _numerator) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        source = uv.outputs['UV']
        if i != 4:
            offset = node_('vertical_uv_offset_' + str(i), 'ShaderNodeVectorMath', operation='ADD')
            group.links.new(source, offset.inputs[0])
            offset.inputs[1].default_value = (dx * step_u, dy * step_v, 0.)
            source = offset.outputs['Vector']
        texture = node_('vertical_texture_' + str(i), 'ShaderNodeTexImage', image=vertical_image,
            interpolation='Linear', extension='EXTEND', projection='FLAT')
        group.links.new(source, texture.inputs['Vector'])
        separate = node_('vertical_separate_' + str(i), 'ShaderNodeSeparateColor', mode='RGB')
        group.links.new(texture.outputs['Color'], separate.inputs['Color'])

    def math_(key, operation, a, b):
        node = group.nodes.new('ShaderNodeMath')
        node[S._NODE] = 'polar_' + key
        node.operation = operation
        by[node[S._NODE]] = node
        for socket, value in zip(node.inputs, (a, b)):
            if type(value) in (int, float):
                socket.default_value = value
            else:
                group.links.new(value, socket)
        return node.outputs['Value']

    attrs = {}
    for role in ('vertical_side', 'vertical_weight'):
        node = group.nodes.new('ShaderNodeAttribute')
        node[S._NODE] = 'polar_attr_' + role
        node.attribute_type = 'GEOMETRY'
        node.attribute_name = fields[role]
        by[node[S._NODE]] = node
        attrs[role] = node.outputs['Fac']
    vertical = {}
    for side, channel in (('top', 'Green'), ('bottom', 'Blue')):
        weights = []
        for i, (_dx, _dy, numerator) in enumerate(sdf_filter.KERNEL_WEIGHTS):
            source = by['vertical_separate_' + str(i)].outputs[channel]
            weights.append(math_(side + '_weight_' + str(i), 'MULTIPLY', source, numerator / 16.0))
        total = weights[0]
        for i in range(1, 9):
            total = math_(side + '_sum_' + str(i - 1), 'ADD', total, weights[i])
        vertical[side] = total
    bottom_weight = math_('bottom_weight', 'SUBTRACT', 1., attrs['vertical_side'])
    top = math_('top_part', 'MULTIPLY', vertical['top'], attrs['vertical_side'])
    bottom = math_('bottom_part', 'MULTIPLY', vertical['bottom'], bottom_weight)
    selected = math_('vertical_scalar', 'ADD', top, bottom)
    horizontal_weight = math_('horizontal_weight', 'SUBTRACT', 1., attrs['vertical_weight'])
    horizontal = math_('horizontal_part', 'MULTIPLY', by['texture_scalar'].outputs['Value'], horizontal_weight)
    vertical_part = math_('vertical_part', 'MULTIPLY', selected, attrs['vertical_weight'])
    scalar = math_('scalar', 'ADD', horizontal, vertical_part)
    group.links.new(scalar, by['hard_lit'].inputs[0])
    group.links.new(scalar, by['smooth_lit'].inputs['Value'])


def validate(group, by, info, *, comparison=True):
    from . import face_sdf as S, sdf_filter
    source = info['vertical_source']
    uv = by['vertical_uv']
    if uv.uv_map != source['uv']['uv_name']:
        raise S.FaceSDFError('Polar vertical UVMap differs from the explicit independent layer')
    image = by['vertical_texture_4'].image
    if image is None or image == by['left_texture'].image:
        raise S.FaceSDFError('Polar vertical source must have its own Image ID')
    step_u, step_v = source['sampling']['uv_step']
    for i, (dx, dy, _numerator) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        texture = by['vertical_texture_' + str(i)]
        if (texture.image != image or texture.interpolation != 'Linear'
                or texture.extension != 'EXTEND' or texture.projection != 'FLAT'):
            raise S.FaceSDFError('Polar vertical tap Image/sampling differs')
        if i == 4:
            S._expect_link(texture.inputs['Vector'], uv, 'UV', 'Polar vertical center UV')
        else:
            offset = by['vertical_uv_offset_' + str(i)]
            if offset.operation != 'ADD':
                raise S.FaceSDFError('Polar vertical UV offset must be ADD')
            S._expect_link(offset.inputs[0], uv, 'UV', 'Polar unmirrored vertical UV')
            S._expect_default(offset.inputs[1], (dx * step_u, dy * step_v, 0.), 'Polar vertical UV step')
            S._expect_link(texture.inputs['Vector'], offset, 'Vector', 'Polar vertical tap UV')
        separate = by['vertical_separate_' + str(i)]
        if separate.mode != 'RGB':
            raise S.FaceSDFError('Polar vertical SeparateColor must be RGB')
        S._expect_link(separate.inputs['Color'], texture, 'Color', 'Polar vertical tap color')
    for role in ('vertical_side', 'vertical_weight'):
        node = by['polar_attr_' + role]
        if node.attribute_type != 'GEOMETRY' or node.attribute_name != info['fields'][role]:
            raise S.FaceSDFError('Polar attribute role differs: ' + role)

    def edge(key, op, a, b):
        node = by['polar_' + key]
        if node.operation != op:
            raise S.FaceSDFError('Polar scalar operation differs: ' + key)
        for index, value in enumerate((a, b)):
            if type(value) in (int, float):
                S._expect_default(node.inputs[index], value, 'Polar ' + key)
            else:
                S._expect_link(node.inputs[index], by[value[0]], value[1], 'Polar ' + key)
        return ('polar_' + key, 'Value')

    vertical = {}
    for side, channel in (('top', 'Green'), ('bottom', 'Blue')):
        weights = []
        for i, (_dx, _dy, numerator) in enumerate(sdf_filter.KERNEL_WEIGHTS):
            source = ('vertical_separate_' + str(i), channel)
            weights.append(edge(side + '_weight_' + str(i), 'MULTIPLY', source, numerator / 16.))
        total = weights[0]
        for i in range(1, 9):
            total = edge(side + '_sum_' + str(i - 1), 'ADD', total, weights[i])
        vertical[side] = total
    up = ('polar_attr_vertical_side', 'Fac')
    weight = ('polar_attr_vertical_weight', 'Fac')
    bottom_weight = edge('bottom_weight', 'SUBTRACT', 1., up)
    top = edge('top_part', 'MULTIPLY', vertical['top'], up)
    bottom = edge('bottom_part', 'MULTIPLY', vertical['bottom'], bottom_weight)
    selected = edge('vertical_scalar', 'ADD', top, bottom)
    horizontal_weight = edge('horizontal_weight', 'SUBTRACT', 1., weight)
    horizontal = edge('horizontal_part', 'MULTIPLY', ('texture_scalar', 'Value'), horizontal_weight)
    vertical_part = edge('vertical_part', 'MULTIPLY', selected, weight)
    result = edge('scalar', 'ADD', horizontal, vertical_part)
    if comparison:
        S._expect_link(by['hard_lit'].inputs[0], by[result[0]], result[1], 'Polar one comparison')
        S._expect_link(by['smooth_lit'].inputs['Value'], by[result[0]], result[1], 'Polar soft comparison')
