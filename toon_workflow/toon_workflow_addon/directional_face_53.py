# SPDX-License-Identifier: GPL-3.0-or-later
"""Bind an authored directional distance field to EEVEE's per-light evaluation."""

import bpy

from . import lighting_53 as lighting


AXES = ('X', 'Y', 'Z', '-X', '-Y', '-Z')
CONTROLS = (
    ('Enabled', 'NodeSocketFloat', 1.),
    ('Lit Color', 'NodeSocketColor', (.949, .815, .753, 1.)),
    ('Shadow Color', 'NodeSocketColor', (.807, .485, .533, 1.)),
    ('Key Gain', 'NodeSocketFloat', 1.256637),
    ('Cast Weight', 'NodeSocketFloat', .21),
    ('Fill Gain', 'NodeSocketFloat', .105),
    ('Rim Gain', 'NodeSocketFloat', .08),
    ('UV Scale', 'NodeSocketVector', (1., 1., 1.)),
    ('UV Offset', 'NodeSocketVector', (0., 0., 0.)),
    ('Base Tint', 'NodeSocketColor', (1., 1., 1., 1.)),
)


def valid_field(group):
    if not (lighting._local(group) and group.bl_idname == 'ShaderNodeTree'):
        return False
    sockets = [(item.in_out, item.name, item.socket_type)
               for item in group.interface.items_tree if item.item_type == 'SOCKET']
    return (sockets.count(('INPUT', 'Light', 'NodeSocketVector')) == 1
            and sockets.count(('INPUT', 'Face UV', 'NodeSocketVector')) == 1
            and sockets.count(('OUTPUT', 'Lit', 'NodeSocketFloat')) == 1)


def _validate_images(field):
    pending = [field]
    seen = set()
    while pending:
        tree = pending.pop()
        if tree in seen:
            continue
        seen.add(tree)
        lighting._need(lighting._local(tree), '距离场及其子组须为本地节点组')
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree:
                pending.append(node.node_tree)
            if node.bl_idname == 'ShaderNodeTexImage':
                image = node.image
                lighting._need(image and image.source == 'FILE' and image.packed_file
                               and image.colorspace_settings.name == 'Non-Color',
                               '距离场图像须打包为 Non-Color 数据贴图')


def _build(group, scene, head, axes, uv_name, field, scope_name, valid_name):
    def node(kind, label=None, **props):
        result = group.nodes.new(kind)
        if label:
            result.label = label
        for name, value in props.items():
            setattr(result, name, value)
        return result

    def send(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            group.links.new(value, socket)
        else:
            socket.default_value = value

    def math(operation, a, b=0., label=None):
        result = node('ShaderNodeMath', label, operation=operation)
        send(a, result.inputs[0])
        send(b, result.inputs[1])
        if operation == 'COMPARE':
            result.inputs[2].default_value = .001
        return result.outputs[0]

    def vec(operation, a, b=None, label=None):
        result = node('ShaderNodeVectorMath', label, operation=operation)
        send(a, result.inputs[0])
        if b is not None:
            send(b, result.inputs['Scale'] if operation == 'SCALE' else result.inputs[1])
        return result.outputs['Value' if operation == 'DOT_PRODUCT' else 'Vector']

    def ramp(value, low, high):
        result = node('ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        for name, setting in (('Value', value), ('From Min', low), ('From Max', high)):
            send(setting, result.inputs[name])
        return result.outputs['Result']

    def emission(color):
        result = node('ShaderNodeEmission')
        send(color, result.inputs['Color'])
        return result.outputs[0]

    def accumulate(color, label):
        result = node('ShaderNodeLightAccumulation', label)
        send(color, result.inputs['Diffuse Light'])
        return result.outputs[0]

    def mix_surface(weight, a, b):
        result = node('ShaderNodeMixShader')
        for value, socket in zip((weight, a, b), result.inputs):
            send(value, socket)
        return result.outputs[0]

    def attribute(name, kind='GEOMETRY'):
        return node('ShaderNodeAttribute', attribute_type=kind, attribute_name=name).outputs['Fac']

    group.interface.new_socket(name='Original Surface', in_out='INPUT', socket_type='NodeSocketShader')
    for name, kind, value in CONTROLS:
        item = group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
        item.default_value = value
        if kind == 'NodeSocketFloat':
            item.min_value = 0.
            item.max_value = 20. if name == 'Key Gain' else 1.
    for name in ('Surface', 'Main Shape', 'Face Scope'):
        group.interface.new_socket(name=name, in_out='OUTPUT', socket_type='NodeSocketShader')
    inputs = node('NodeGroupInput').outputs
    output = node('NodeGroupOutput').inputs

    evaluation = node('ShaderNodeLightEvaluation', '逐灯方向和漫反射', mode='DIFFUSE').outputs
    head_direction = node('ShaderNodeCombineXYZ', '头部空间 · 右、上、前')
    for axis, destination in zip(axes, head_direction.inputs):
        column = 'XYZ'.index(axis[-1])
        coordinates = node('ShaderNodeCombineXYZ', axis + ' 世界轴')
        for row, coordinate in enumerate(coordinates.inputs):
            lighting._set_source(coordinate, None, (('axis', head, f'matrix_world[{column}][{row}]'),))
        unit = vec('NORMALIZE', coordinates.outputs[0])
        dot = vec('DOT_PRODUCT', evaluation['Direction'], unit)
        send(math('MULTIPLY', dot, -1.) if axis.startswith('-') else dot, destination)

    uv = node('ShaderNodeUVMap', '实际消费 UV', uv_map=uv_name).outputs['UV']
    uv = vec('ADD', vec('MULTIPLY', uv, inputs['UV Scale']), inputs['UV Offset'])
    uv = vec('MINIMUM', vec('MAXIMUM', uv, (0., 0., 0.)), (1., 1., 1.))
    lookup = node('ShaderNodeGroup', '角色方向距离场', node_tree=field)
    send(uv, lookup.inputs['Face UV'])
    send(head_direction.outputs[0], lookup.inputs['Light'])

    role = attribute(lighting._tag_directional_lights(scene), 'LIGHT')
    is_key = math('COMPARE', role, 1., '主光 · 控制形体')
    is_fill = math('COMPARE', role, 2., '副光 · 只染色')
    is_rim = math('COMPARE', role, 3., '轮廓光 · 只提边')
    info = node('ShaderNodeLightInfo').outputs
    power = math('MINIMUM', math('MULTIPLY', info['Power'], inputs['Key Gain']), 1.)
    lit = math('MULTIPLY', is_key, lookup.outputs['Lit'], '仅主光形体，排除投影')
    cast = node('ShaderNodeShadowRaycast', '刘海与衣领的实时投影')
    cast.inputs['Softness'].default_value = .26
    split = node('ShaderNodeSeparateColor')
    send(cast.outputs['Color'], split.inputs['Color'])
    visible = math('SUBTRACT', 1., math('MULTIPLY', inputs['Cast Weight'],
                                       math('SUBTRACT', 1., split.outputs[0])))
    lift = vec('SUBTRACT', inputs['Lit Color'], inputs['Shadow Color'])
    key = vec('SCALE', lift, math('MULTIPLY', lit, visible))
    diffuse = ramp(evaluation['Factor'], .02, .72)
    fill = math('MULTIPLY', is_fill, inputs['Fill Gain'])
    geometry = node('ShaderNodeNewGeometry').outputs
    facing = math('ABSOLUTE', vec('DOT_PRODUCT', geometry['Normal'], geometry['Incoming']))
    rim = math('MULTIPLY', math('MULTIPLY', is_rim, inputs['Rim Gain']),
               math('SUBTRACT', 1., ramp(facing, .12, .55)))
    extra = math('MULTIPLY', diffuse, math('ADD', fill, rim))
    color = vec('MULTIPLY', vec('ADD', key, vec('SCALE', (1., 1., 1.), extra)), info['Color'])
    contribution = accumulate(vec('SCALE', vec('MULTIPLY', color, inputs['Base Tint']), power),
                              '逐盏采样后相加')
    surface = node('ShaderNodeAddShader')
    send(emission(vec('MULTIPLY', inputs['Shadow Color'], inputs['Base Tint'])), surface.inputs[0])
    send(contribution, surface.inputs[1])

    scope = ramp(attribute(scope_name), .12, .96) if scope_name else 1.
    if valid_name:
        scope = math('MULTIPLY', scope, attribute(valid_name))
    black = emission((0., 0., 0., 1.))
    send(mix_surface(math('MULTIPLY', scope, inputs['Enabled']), inputs['Original Surface'],
                     surface.outputs[0]), output['Surface'])
    send(mix_surface(scope, black, accumulate(lit, '诊断 · 主光形体')), output['Main Shape'])
    send(emission(scope if isinstance(scope, bpy.types.NodeSocket) else (1., 1., 1., 1.)),
         output['Face Scope'])
    for index, item in enumerate(group.nodes):
        item.location = ((index % 8) * 220, -(index // 8) * 230)


def bind(scene, target, index, *, head, axes, uv_name, field, scope_name='', valid_name=''):
    """Replace one EEVEE Surface link, retaining its exact source for restore."""
    lighting._available(scene)
    lighting._check_copy()
    owner = lighting._identity(scene)
    lighting._need(target == scene.get(lighting.FACE_OBJECT), '请选择此场景已登记的面部对象')
    material = lighting._material_context(scene, target, index)
    lighting._need(lighting.DIRECTIONAL_STAGE not in material, '此材质已绑定连续面影')
    lighting._need(scope_name or sum(slot.material == material for slot in target.material_slots) == 1,
                   '同一材质用于多个槽时，请指定明确的脸域权重属性')
    lighting._need(lighting._present(scene, head, 'EMPTY'), '请选择本场景内随头运动的本地空物体')
    lighting._need(len(axes) == 3 and all(axis in AXES for axis in axes)
                   and len({axis[-1] for axis in axes}) == 3, '右、上、前须使用互不重复的坐标轴')
    lighting._need(uv_name in target.data.uv_layers, '请选择当前网格实际存在的脸部 UV')
    lighting._need(valid_field(field), '距离场须有向量输入 Light、Face UV 和浮点输出 Lit')
    _validate_images(field)
    if scope_name or valid_name:
        evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.attributes
        if scope_name:
            scope = evaluated.get(scope_name)
            lighting._need(scope and scope.data_type == 'FLOAT', '缺少浮点脸域属性 ' + scope_name)
        if valid_name:
            valid = evaluated.get(valid_name)
            lighting._need(valid and valid.data_type in {'BOOLEAN', 'FLOAT'},
                           '缺少布尔或浮点脸域有效性属性 ' + valid_name)
    tree = material.node_tree
    output = tree.get_output_node('EEVEE')
    lighting._need(output and len(output.inputs['Surface'].links) == 1
                   and not output.inputs['Volume'].is_linked, '材质须有明确的 EEVEE 表面输出')
    destination = output.inputs['Surface']
    original = destination.links[0].from_socket
    group = bpy.data.node_groups.new('三渲二 5.3 · 连续面影 · ' + material.name, 'ShaderNodeTree')
    stage = None
    try:
        from .._ui_kit import apply_node_theme

        _build(group, scene, head, axes, uv_name, field, scope_name, valid_name)
        for node in group.nodes:
            apply_node_theme(node, 'toon')
        group[lighting.STAGE] = lighting.PROFILE
        group[lighting.ROLE] = lighting.ROLE_DIRECTIONAL_FACE
        group[lighting.OWNER] = owner
        group[lighting.PROJECT_REF] = scene
        group[lighting.SIGNATURE] = lighting._signature(group)
        stage = tree.nodes.new('ShaderNodeGroup')
        stage.node_tree = group
        apply_node_theme(stage, 'toon')
        stage.label = '5.3 连续面影'
        stage.location = (output.location.x - 240, output.location.y)
        stage[lighting.STAGE] = lighting.PROFILE
        stage[lighting.OWNER] = owner
        stage[lighting.ROLE] = lighting.ROLE_DIRECTIONAL_FACE
        tree.links.new(original, stage.inputs['Original Surface'])
        tree.links.new(stage.outputs['Surface'], destination)
        material[lighting.DIRECTIONAL_STAGE] = stage.name
    except Exception:
        if stage is not None:
            tree.nodes.remove(stage)
        if not destination.is_linked:
            tree.links.new(original, destination)
        bpy.data.node_groups.remove(group)
        raise
    return stage


def active_stage(material):
    if material and material.node_tree:
        node = material.node_tree.nodes.get(material.get(lighting.DIRECTIONAL_STAGE, ''))
        if node and node.get(lighting.ROLE) == lighting.ROLE_DIRECTIONAL_FACE:
            return node
    return None


def unbind(scene, target, index):
    material = lighting._material_context(scene, target, index)
    stage = active_stage(material)
    owner = lighting._identity(scene)
    lighting._need(stage and stage.get(lighting.OWNER) == owner
                   and lighting.is_owned_group(stage.node_tree), '连续面影节点已变化，请先核对接线')
    tree = material.node_tree
    output = tree.get_output_node('EEVEE')
    lighting._need(output, '当前材质已无 EEVEE 输出')
    surface = output.inputs['Surface']
    prior = stage.inputs['Original Surface'].links
    lighting._need(len(prior) == 1 and len(surface.links) == 1
                   and surface.links[0].from_socket == stage.outputs['Surface']
                   and sum(len(socket.links) for socket in stage.outputs) == 1,
                   '连续面影入口或出口已改接，请先核对接线')
    group = stage.node_tree
    tree.links.new(prior[0].from_socket, surface)
    tree.nodes.remove(stage)
    del material[lighting.DIRECTIONAL_STAGE]
    if group.users == 0:
        bpy.data.node_groups.remove(group)
