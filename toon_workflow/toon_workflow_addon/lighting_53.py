# SPDX-License-Identifier: GPL-3.0-or-later
"""Native EEVEE material lighting for an explicitly upgraded project."""
import hashlib
import json
from pathlib import Path
from copy import deepcopy
import uuid

import bpy
from mathutils import Vector


PROFILE = 'toon-eevee-lighting-5.3.v1'
PROJECT = 'toon53_project'
OWNER = 'toon53_owner'
ROLE = 'toon53_role'
PALETTE_REF = 'toon53_palette_ref'
SIGNATURE = 'toon53_graph_signature'
CONTROLLER = 'toon53_shadow_controller'
LOCAL_ANCHOR = 'toon53_shadow_anchor'
LOCAL_HANDLE = 'toon53_shadow_handle'
EYELID_CONTROL = 'toon53_eyelid_control'
FACE_OBJECT = 'toon53_face_object'
KEY_LIGHT = 'toon53_key_light'
FILL_LIGHT = 'toon53_fill_light'
MATERIAL = 'toon53_material'
PROJECT_REF = 'toon53_scene'
STAGE = 'toon53_stage'
ROLE_FACE = 'FACE'
ROLE_HAIR = 'HAIR'
ROLE_COLOR = 'COLOR'
FAMILY_ARCHIVE = 'toon53_previous_family'
FACE_NORMAL = 'Face Shader Normal'
ARTIST_NORMAL = 'Artist Normal'
NORMAL_MIX = 'toon53_face_normal_mix'
NORMAL_BLEND = '混合艺术与 Cast 法向'
NORMAL_UNIT = '归一化面部法向'
FILL_EVALUATION = '副光原几何求值'
FILL_NORMAL = '副光原几何因子'
KEY_DIFFUSE = 'toon53_key_diffuse'
KEY_GROUP_NAME = '5.3 主光色阶'
KEY_LABEL = '仅主光驱动旧色阶'
KEY_EXPRESSIONS = ('2*(x*z + w*y)', '2*(y*z - w*x)', '1-2*(x*x + y*y)')
_upgraded_paths = set()

COLORS = {
    'toon53_palette': (1., 1., 1.),
    'toon53_shadow': (.49, .38, .45),
    'toon53_highlight': (1., .81, .86),
}
NUMBERS = {
    'toon53_palette_mix': 0.,
    'toon53_exposure': 1.,
    'toon53_shadow_mix': .3,
    'toon53_key_strength': .16,
    'toon53_fill_strength': .25,
    'toon53_highlight_strength': .4,
    'toon53_highlight_shift': .028,
    'toon53_face_threshold': .2,
    'toon53_face_feather': .065,
}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _local(block):
    return block and block.is_editable and block.library is None and block.override_library is None


def _present(scene, obj, kind):
    return obj and obj.type == kind and scene.objects.get(obj.name) == obj and _local(obj)


def _available(scene):
    _need(bpy.app.version[:2] == (5, 3) and scene.render.engine == 'BLENDER_EEVEE',
          '原生逐灯着色需要 Blender 5.3 和 EEVEE')


def _check_copy():
    if bpy.data.filepath and bpy.data.version[:2] < (5, 3):
        _need(Path(bpy.data.filepath).resolve() in _upgraded_paths,
              '请先在工作台另存 5.3 升级工程')


def _id_property(block, name, value):
    block[name] = value
    ui = block.id_properties_ui(name)
    if isinstance(value, tuple):
        ui.update(subtype='COLOR', min=0., max=4., soft_min=0., soft_max=1.)
    elif isinstance(value, float):
        maximum = .075 if name.endswith('_shift') else 1.
        ui.update(min=0., max=maximum, soft_min=0., soft_max=maximum)


def _identity(scene):
    value = scene.get(PROJECT)
    _need(isinstance(value, str) and len(value) == 32
          and sum(s.get(PROJECT) == value for s in bpy.data.scenes) == 1,
          '请在此场景建立独立的 5.3 项目控制')
    return value


def _tag_lights(scene):
    owner = _identity(scene)
    return 'toon53_' + owner


def _light_available(scene, light):
    return _present(scene, light, 'LIGHT') and _local(light.data) and light.data.users == 1


def _fill_receivers(scene, fill):
    owner = _identity(scene)
    face = scene[FACE_OBJECT]
    current = fill.light_linking.receiver_collection
    if current is not None:
        _need(current.get(OWNER) == owner and set(current.all_objects) == {face},
              '副光已有收光分配。请选择一盏可给脸部使用的灯')
        return
    receivers = bpy.data.collections.new('三渲二 5.3 · 脸部收光')
    try:
        receivers[OWNER] = owner
        receivers.objects.link(face)
        fill.light_linking.receiver_collection = receivers
    except Exception:
        bpy.data.collections.remove(receivers)
        raise


def is_fill_excluded(scene, target, light):
    """Qualify an explicit 5.3 fill that cannot reach the old hair sensor."""
    if bpy.app.version[:2] != (5, 3) or target is None or scene.get(FILL_LIGHT) != light:
        return False
    receivers = light.light_linking.receiver_collection
    tag = _tag_lights(scene)
    return (light.get(tag) == -1. and light.data.get(tag) == -1.
            and receivers is not None and receivers.get(OWNER) == _identity(scene)
            and not receivers.children and set(receivers.objects) == {scene.get(FACE_OBJECT)}
            and target not in receivers.objects.values())


def _setup_lights(scene, key, fill=None):
    owner = _tag_lights(scene)
    _need(_light_available(scene, key) and key.data.type == 'SUN',
          '请选择此场景内独立使用灯光数据的本地太阳作主光')
    _need(fill is None or (_light_available(scene, fill) and fill != key),
          '副光须为本场景另一盏使用独立数据的本地灯光')
    next_roles = {key: 1.}
    if fill is not None:
        next_roles[fill] = -1.
    old_roles = [obj for obj in scene.objects if obj.type == 'LIGHT'
                 and (owner in obj or owner in obj.data)]
    for light in set(old_roles).union(next_roles):
        _need(_light_available(scene, light), '现有光源已共享或不可编辑')
    if fill is not None:
        _fill_receivers(scene, fill)
    for light in old_roles:
        for block in (light, light.data):
            if owner in block:
                del block[owner]
        if light not in next_roles:
            collection = light.light_linking.receiver_collection
            if collection is not None and collection.get(OWNER) == _identity(scene):
                light.light_linking.receiver_collection = None
                if collection.users == 0:
                    bpy.data.collections.remove(collection)
    for light, role in next_roles.items():
        for block in (light, light.data):
            block[owner] = role
            block.update_tag()
    scene[KEY_LIGHT] = key
    if fill is not None:
        scene[FILL_LIGHT] = fill
    elif FILL_LIGHT in scene:
        del scene[FILL_LIGHT]
    scene.update_tag()


def _new_empty(scene, parent, name, size, shape):
    obj = bpy.data.objects.new(name, None)
    try:
        scene.collection.objects.link(obj)
        obj.parent = parent
        obj.empty_display_type = shape
        obj.empty_display_size = size
        obj.show_in_front = True
        obj.hide_render = True
        obj.lock_location[2] = True
        obj.lock_scale[2] = True
        obj.lock_rotation = (True, True, True)
        obj[OWNER] = _identity(scene)
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise
    return obj


def _new_controls(scene, target, slot):
    camera = scene.camera
    _need(_present(scene, camera, 'CAMERA'), '请指定本场景的活动相机')
    evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
    indices = {index for face in evaluated.data.polygons if face.material_index == slot
               for index in face.vertices}
    _need(indices, '面部材质槽须包含实际渲染的面')
    points = [evaluated.matrix_world @ evaluated.data.vertices[i].co for i in indices]
    from . import face_thresholds
    refs = [face_thresholds.inspect(target, mod, evaluate=False).get('reference')
            for mod in target.modifiers if mod.type == 'NODES' and face_thresholds.is_managed(mod)]
    ref = scene.objects.get(refs[0]) if len(refs) == 1 and refs[0] else None
    pivot = ref.matrix_world.translation if ref else sum(points, Vector((0., 0., 0.))) / len(points)
    origin = camera.matrix_world.inverted_safe() @ pivot
    _need(origin.z < camera.data.clip_start * -2, '目标须在活动相机前方')
    controls = {}
    try:
        controller = controls[CONTROLLER] = _new_empty(
            scene, camera, '5.3 影形整体', abs(origin.z) * .065, 'CIRCLE')
        # The transforms are offsets around the initial on-screen center.
        controller.matrix_parent_inverse.translation = origin
        _id_property(controller, 'toon53_density', .72)
        controller['toon53_anchor'] = (origin.x, origin.y, -origin.z)
        anchor = controls[LOCAL_ANCHOR] = _new_empty(
            scene, camera, '5.3 局部影形 · 落点', abs(origin.z) * .026, 'CIRCLE')
        anchor.location = origin
        anchor.lock_scale = (True, True, True)
        handle = controls[LOCAL_HANDLE] = _new_empty(
            scene, anchor, '5.3 局部影形 · 拖动', abs(origin.z) * .018, 'SPHERE')
        handle.lock_scale = (True, True, True)
        handle['toon53_radius'] = .045
        handle.id_properties_ui('toon53_radius').update(min=.002, max=.3, soft_max=.12)
        handle['toon53_face_bias'] = 0.
        handle.id_properties_ui('toon53_face_bias').update(min=-1., max=1., soft_min=-.3, soft_max=.3)
    except Exception:
        for obj in reversed(controls.values()):
            bpy.data.objects.remove(obj, do_unlink=True)
        raise
    return controls


def prepare(scene, key, target, *, fill=None, face_slot=None):
    """Author visible, persistent shot controls in the open 5.3 working file."""
    _available(scene)
    _check_copy()
    _need(_present(scene, target, 'MESH'), '请指定此场景内的本地面部对象')
    if scene.get(PROJECT):
        _identity(scene)
        _need(scene.get(KEY_LIGHT) == key, '主光已绑定。保留当前键与面影引用')
        _need(scene.get(FACE_OBJECT) == target, '请先选回已绑定的脸部对象')
        _setup_lights(scene, key, fill)
        return scene[CONTROLLER]
    _need(_light_available(scene, key) and key.data.type == 'SUN'
          and (fill is None or (_light_available(scene, fill) and fill != key)),
          '请指定本地独占太阳主光与可选的独占副光')
    _need(not any(name in scene for name in (*COLORS, *NUMBERS, CONTROLLER, LOCAL_ANCHOR,
                                            LOCAL_HANDLE, KEY_LIGHT, FILL_LIGHT, FACE_OBJECT)),
          '场景已有未识别的 5.3 参数')
    scene[PROJECT] = uuid.uuid4().hex
    controls = {}
    try:
        slot = target.active_material_index if face_slot is None else face_slot
        controls = _new_controls(scene, target, slot)
        for name, value in {**COLORS, **NUMBERS}.items():
            _id_property(scene, name, value)
        for name, obj in controls.items():
            scene[name] = obj
        scene[FACE_OBJECT] = target
        _setup_lights(scene, key, fill)
    except Exception:
        for obj in reversed(controls.values()):
            bpy.data.objects.remove(obj, do_unlink=True)
        for name in (*COLORS, *NUMBERS, PROJECT, CONTROLLER, LOCAL_ANCHOR, LOCAL_HANDLE, FACE_OBJECT):
            if name in scene:
                del scene[name]
        raise
    return controls[CONTROLLER]


def _set_source(node_socket, index, variables, expression=None):
    curve = node_socket.driver_add('default_value', index) if index is not None else node_socket.driver_add('default_value')
    driver = curve.driver
    driver.type = 'SUM' if expression is None else 'SCRIPTED'
    if expression is not None:
        driver.expression = expression
    for name, block, data_path in variables:
        var = driver.variables.new()
        var.name = name
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = block.bl_rna.identifier.upper()
        var.targets[0].id = block
        var.targets[0].data_path = data_path
    if expression is not None and not driver.is_simple_expression:
        raise ValueError('无法生成 Blender 简单表达式驱动')


def palette_path(token, name):
    return token + '_' + name.removeprefix('toon53_')


def _parameter_driver(group, scene, token, name, default):
    color = isinstance(default, tuple)
    node = group.nodes.new('ShaderNodeRGB' if color else 'ShaderNodeValue')
    node.name = node.label = name
    socket = node.outputs[0]
    local_property = palette_path(token, name)
    for index in range(3) if color else (None,):
        component = f'[{index}]' if color else ''
        path = f'["{name}"]' + component
        local = f'["{local_property}"]' + component
        flag = f'["{local_property}_override"]'
        _set_source(socket, index, (('shared', scene, path), ('local', scene, local),
                                   ('override', scene, flag)),
                    'local if override else shared')
    return socket


def _socket_record(socket, driven):
    if not hasattr(socket, 'default_value'):
        return socket.identifier, None
    if socket.path_from_id('default_value') in driven:
        return socket.identifier, 'driver'
    value = socket.default_value
    if type(value) not in (str, bool, float, int, type(None)):
        value = list(value)
    return socket.identifier, value


def _signature(group):
    animation = group.animation_data
    _need(not animation or (animation.action is None and not animation.nla_tracks),
          '受管光照组包含未声明的原生动作')
    drivers = sorted([
        (curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression,
         tuple((var.name, var.type, var.targets[0].id_type,
                var.targets[0].id.name_full if var.targets[0].id else None, var.targets[0].data_path,
                var.targets[0].transform_space, var.targets[0].transform_type, var.targets[0].rotation_mode)
               for var in curve.driver.variables))
        for curve in (animation.drivers if animation else ())
    ])
    paths = {row[0] for row in drivers}
    props = ('operation', 'blend_type', 'vector_type', 'convert_from', 'convert_to',
             'attribute_type', 'attribute_name', 'mode', 'uv_map', 'extension',
             'projection', 'interpolation', 'mute')
    nodes = [
        (node.name, node.bl_idname, {prop: getattr(node, prop) for prop in props if hasattr(node, prop)},
                (node.image.name_full if node.bl_idname == 'ShaderNodeTexImage' and node.image else None),
         [_socket_record(value, paths) for value in (*node.inputs, *node.outputs)])
        for node in group.nodes
    ]
    links = sorted((item.from_node.name, item.from_socket.identifier,
                    item.to_node.name, item.to_socket.identifier) for item in group.links)
    return hashlib.sha256(json.dumps((nodes, links, drivers), allow_nan=False).encode()).hexdigest()


def is_owned_group(group):
    """Full local graph witness, used by the background opaque-material reader."""
    try:
        return (_local(group) and group.get(STAGE) == PROFILE
                and group.get(ROLE) in (ROLE_FACE, ROLE_HAIR, ROLE_COLOR)
                and group.get(SIGNATURE) == _signature(group))
    except (AttributeError, ValueError, TypeError, ReferenceError):
        return False


def _key_direction(node, key):
    node.label = '指定主光方向'
    for index, socket in enumerate(node.inputs):
        driver = socket.driver_add('default_value').driver
        driver.type = 'SCRIPTED'
        driver.expression = KEY_EXPRESSIONS[index]
        for channel in ('w', 'x', 'y', 'z'):
            var = driver.variables.new()
            var.name = channel
            var.type = 'TRANSFORMS'
            var.targets[0].id = key
            var.targets[0].rotation_mode = 'QUATERNION'
            var.targets[0].transform_space = 'WORLD_SPACE'
            var.targets[0].transform_type = 'ROT_' + channel.upper()
        _need(driver.is_simple_expression, '主光方向驱动必须能在后台运行')
    return node.outputs[0]


def _build(scene, material, role, token, *, mask, uv_name):
    group = bpy.data.node_groups.new('三渲二 5.3 · ' + material.name, 'ShaderNodeTree')
    group[STAGE], group[ROLE], group[OWNER] = PROFILE, role, _identity(scene)
    group[PROJECT_REF], group[MATERIAL] = scene, material.name_full

    def node(kind, **props):
        result = group.nodes.new(kind)
        for name, value in props.items():
            setattr(result, name, value)
        return result

    def assign(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            group.links.new(value, socket)
        else:
            socket.default_value = value

    def mono(operation, first, second):
        n = node('ShaderNodeMath', operation=operation)
        assign(first, n.inputs[0])
        assign(second, n.inputs[1])
        return n.outputs[0]

    def vector(operation, first, second):
        n = node('ShaderNodeVectorMath', operation=operation)
        assign(first, n.inputs[0])
        assign(second, n.inputs['Scale'] if operation == 'SCALE' else n.inputs[1])
        return n.outputs['Value' if operation == 'DOT_PRODUCT' else 'Vector']

    def combine(x, y, z):
        n = node('ShaderNodeCombineXYZ')
        for i, value in enumerate((x, y, z)):
            assign(value, n.inputs[i])
        return n.outputs[0]

    def transform(value, space_from, space_to, kind):
        n = node('ShaderNodeVectorTransform', vector_type=kind,
                 convert_from=space_from, convert_to=space_to)
        assign(value, n.inputs['Vector'])
        return n.outputs[0]

    def native_transform(controller, path):
        n = node('ShaderNodeCombineXYZ')
        for index, coordinate in enumerate(n.inputs):
            _set_source(coordinate, None,
                        (('control', controller, f'{path}[{index}]'),))
        return n.outputs[0]

    def native_property(controller, name):
        scalar = node('ShaderNodeValue').outputs[0]
        _set_source(scalar, None, (('control', controller, f'["{name}"]'),))
        return scalar

    def ramp(value, threshold, feather):
        n = node('ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        assign(value, n.inputs['Value'])
        assign(threshold, n.inputs['From Min'])
        assign(mono('ADD', threshold, feather), n.inputs['From Max'])
        return n.outputs['Result']

    def local_deformation(camera_point):
        anchor = scene[LOCAL_ANCHOR]
        handle = scene[LOCAL_HANDLE]
        origin = native_transform(anchor, 'location')
        offset = vector('SUBTRACT', camera_point, origin)
        planar = vector('MULTIPLY', offset, (1., 1., 0.))
        distance = node('ShaderNodeVectorMath', operation='LENGTH')
        assign(planar, distance.inputs[0])
        radius = mono('MAXIMUM', native_property(handle, 'toon53_radius'), .002)
        weight = mono('SUBTRACT', 1., ramp(distance.outputs['Value'], 0., radius))
        moved = vector('SCALE', native_transform(handle, 'location'), weight)
        face_bias = mono('MULTIPLY', native_property(handle, 'toon53_face_bias'), weight)
        return moved, face_bias

    def emission(color, strength=1.):
        n = node('ShaderNodeEmission')
        assign(color, n.inputs['Color'])
        assign(strength, n.inputs['Strength'])
        return n.outputs[0]

    try:
        for name, direction in (('Original Surface', 'INPUT'), ('Surface', 'OUTPUT')):
            group.interface.new_socket(name=name, in_out=direction, socket_type='NodeSocketShader')
        entry, output = node('NodeGroupInput'), node('NodeGroupOutput')
        defaults = {**COLORS, **NUMBERS}
        used = ['toon53_palette', 'toon53_palette_mix', 'toon53_exposure']
        if role == ROLE_FACE:
            used.extend(('toon53_shadow', 'toon53_shadow_mix', 'toon53_key_strength',
                         'toon53_fill_strength', 'toon53_face_threshold', 'toon53_face_feather'))
        if role == ROLE_HAIR:
            used.extend(('toon53_highlight', 'toon53_highlight_strength', 'toon53_highlight_shift'))
        p = {name: _parameter_driver(group, scene, token, name, defaults[name]) for name in used}
        original = entry.outputs['Original Surface']
        grade = node('ShaderNodeMixShader')
        assign(p['toon53_palette_mix'], grade.inputs[0])
        assign(original, grade.inputs[1])
        assign(emission(p['toon53_palette']), grade.inputs[2])
        exposure = node('ShaderNodeMixShader')
        assign(p['toon53_exposure'], exposure.inputs[0])
        assign(emission((0., 0., 0., 1.)), exposure.inputs[1])
        assign(grade.outputs[0], exposure.inputs[2])
        surface = exposure.outputs[0]

        if role in {ROLE_FACE, ROLE_HAIR}:
            attr = node('ShaderNodeAttribute', attribute_type='LIGHT', attribute_name=_tag_lights(scene))
            key = mono('GREATER_THAN', attr.outputs['Fac'], .5)
            info = node('ShaderNodeLightInfo')
            form = node('ShaderNodeLightEvaluation', mode='DIFFUSE')
            normal = mono('MAXIMUM', form.outputs['Factor'], 0.)
            power = mono('MINIMUM', info.outputs['Power'], 1.)
            key_power = mono('MULTIPLY', key, power)
            acc = node('ShaderNodeLightAccumulation')
            if role == ROLE_FACE:
                controller = scene[CONTROLLER]
                geom = node('ShaderNodeNewGeometry')
                authored = native_transform(controller, 'location')
                scale = native_transform(controller, 'scale')
                anchor = tuple(controller['toon53_anchor'])
                adjust = transform(geom.outputs['Position'], 'WORLD', 'CAMERA', 'POINT')
                adjusted = vector('ADD', vector('ADD', anchor, vector('MULTIPLY',
                                                                      vector('SUBTRACT', adjust, anchor), scale)),
                                  authored)
                moved, face_bias = local_deformation(adjust)
                delta = vector('SUBTRACT', vector('SUBTRACT', adjusted, adjust), moved)
                change = transform(delta, 'CAMERA', 'WORLD', 'VECTOR')
                ray = node('ShaderNodeShadowRaycast')
                assign(vector('ADD', geom.outputs['Position'], change), ray.inputs['Position'])
                assign(.32, ray.inputs['Softness'])
                cast = node('ShaderNodeMixRGB', blend_type='MIX')
                assign(native_property(controller, 'toon53_density'), cast.inputs[0])
                assign((1., 1., 1., 1.), cast.inputs[1])
                assign(ray.outputs['Color'], cast.inputs[2])
                threshold = mono('ADD', p['toon53_face_threshold'], face_bias)
                band = ramp(mono('MULTIPLY', normal, cast.outputs[0]), threshold,
                            p['toon53_face_feather'])
                key_gain = mono('MULTIPLY', mono('MULTIPLY', key_power, band), p['toon53_key_strength'])
                key_color = vector('SCALE', p['toon53_palette'], key_gain)
                fill = mono('LESS_THAN', attr.outputs['Fac'], -.5)
                fill_power = mono('MULTIPLY', fill, power)
                key_direction = _key_direction(node('ShaderNodeCombineXYZ'), scene[KEY_LIGHT])
                key_dot = vector('DOT_PRODUCT', geom.outputs['Normal'], key_direction)
                shadow_side = mono('SUBTRACT', 1., ramp(key_dot, threshold,
                                                       p['toon53_face_feather']))
                fill_amount = mono('MULTIPLY', mono('MULTIPLY', fill_power, normal),
                                   mono('MULTIPLY', p['toon53_fill_strength'], shadow_side))
                fill_color = vector('SCALE', info.outputs['Color'], fill_amount)
                assign(vector('ADD', key_color, fill_color), acc.inputs['Diffuse Light'])

                # Preserve the original surface and weight its contribution toward
                # the project shadow color. The native key shader then lifts
                # areas visible to the designated key only.
                shadow_mix = node('ShaderNodeMixShader')
                assign(p['toon53_shadow_mix'], shadow_mix.inputs[0])
                assign(surface, shadow_mix.inputs[1])
                assign(emission(p['toon53_shadow']), shadow_mix.inputs[2])
                surface = shadow_mix.outputs[0]
            else:
                uv = node('ShaderNodeUVMap', uv_map=uv_name).outputs['UV']
                lighting_direction = transform(form.outputs['Direction'], 'WORLD', 'CAMERA', 'VECTOR')
                split = node('ShaderNodeSeparateXYZ')
                assign(lighting_direction, split.inputs[0])
                shift = mono('MULTIPLY', split.outputs['X'], p['toon53_highlight_shift'])
                shifted_uv = vector('ADD', uv, combine(0., shift, 0.))

                def painted(coordinate):
                    texture = node('ShaderNodeTexImage', image=mask, extension='CLIP', interpolation='Linear')
                    assign(coordinate, texture.inputs['Vector'])
                    return texture.outputs['Color']

                weight = mono('MULTIPLY', painted(uv), painted(shifted_uv))
                glossy = node('ShaderNodeLightEvaluation', mode='GLOSSY')
                assign(.26, glossy.inputs['Roughness'])
                gloss_lobe = mono('MULTIPLY', .67, ramp(glossy.outputs['Factor'], .015, .16))
                lit = mono('MULTIPLY', ramp(normal, .03, .18), mono('ADD', .33, gloss_lobe))
                gloss = mono('MULTIPLY', mono('MULTIPLY', key_power, lit),
                             mono('MULTIPLY', weight, p['toon53_highlight_strength']))
                assign(vector('SCALE', p['toon53_highlight'], gloss), acc.inputs['Glossy Light'])
            add = node('ShaderNodeAddShader')
            assign(surface, add.inputs[0])
            assign(acc.outputs[0], add.inputs[1])
            surface = add.outputs[0]
        assign(surface, output.inputs['Surface'])
        for index, n in enumerate(group.nodes):
            n.location = ((index % 8) * 230, -(index // 8) * 220)
        group[SIGNATURE] = _signature(group)
        return group
    except Exception:
        bpy.data.node_groups.remove(group)
        raise


def _material_context(scene, target, index):
    _need(_present(scene, target, 'MESH') and 0 <= index < len(target.material_slots),
          '请选择当前场景实际使用的网格材质槽')
    material = target.material_slots[index].material
    _need(_local(material) and material.node_tree and
          any(poly.material_index == index for poly in target.data.polygons),
          '材质槽须有本地原生节点且实际用于面')
    consumers = [obj for obj in bpy.data.objects if obj.type == 'MESH'
                 and any(slot.material == material for slot in obj.material_slots)]
    _need(consumers == [target], '材质有其它对象使用，须先确认独占角色材质')
    return material


def _direct_face_path(material):
    """Find the authored Diffuse->ShaderToRGB input without changing its SDF readers."""
    from . import face_sdf
    tree = material.node_tree
    readers = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeGroup' and n.node_tree
               and n.node_tree.get(face_sdf.PROFILE_KEY) in face_sdf.SUPPORTED_PROFILES]
    _need(len(readers) == 1, '脸部槽须有一个已明确绑定的面部 SDF 读者')
    reader = readers[0]
    binding = face_sdf._binding_info(reader.node_tree)
    _need(binding['state'] == 'bound', '脸部 SDF 尚未绑定')
    paths = [n.inputs['Shader'] for n in tree.nodes if n.bl_idname == 'ShaderNodeShaderToRGB'
             and len(n.inputs['Shader'].links) == 1
             and n.inputs['Shader'].links[0].from_node.bl_idname == 'ShaderNodeBsdfDiffuse']
    _need(len(paths) <= 1, '脸部材质有多个明暗入口，无法明确隔离副光')
    return reader, paths[0] if paths else None


def _key_only_shader(tree, destination, key):
    """Keep the authored ramp and SDF ports. Feed its scalar from the selected Sun."""
    group = bpy.data.node_groups.new(KEY_GROUP_NAME, 'ShaderNodeTree')
    instance = None
    try:
        group.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
        direction = _key_direction(group.nodes.new('ShaderNodeCombineXYZ'), key)
        geom = group.nodes.new('ShaderNodeNewGeometry')
        cosine = group.nodes.new('ShaderNodeVectorMath')
        cosine.operation = 'DOT_PRODUCT'
        group.links.new(geom.outputs['Normal'], cosine.inputs[0])
        group.links.new(direction, cosine.inputs[1])
        value = group.nodes.new('ShaderNodeMath')
        value.operation = 'MAXIMUM'
        value.inputs[1].default_value = 0.
        group.links.new(cosine.outputs['Value'], value.inputs[0])
        passthrough = group.nodes.new('ShaderNodeEmission')
        group.links.new(value.outputs[0], passthrough.inputs['Color'])
        group.links.new(passthrough.outputs[0], group.nodes.new('NodeGroupOutput').inputs['Shader'])
        for index, n in enumerate(group.nodes):
            n.location = (index * 218, 0.)
        instance = tree.nodes.new('ShaderNodeGroup')
        instance.node_tree = group
        instance.label = KEY_LABEL
        instance.location = (destination.node.location.x - 245., destination.node.location.y + 150.)
        tree.links.new(instance.outputs['Shader'], destination)
    except Exception:
        if instance is not None:
            tree.nodes.remove(instance)
        bpy.data.node_groups.remove(group)
        raise
    return instance, group


def _input_source(socket):
    links = socket.links
    _need(len(links) <= 1 and (not links or links[0].is_valid), '法向入口连接失效或有多来源')
    return links[0].from_socket if links else None


def _single_node(group, kind, **properties):
    matches = [node for node in group.nodes if node.bl_idname == kind
               and all(getattr(node, key) == value for key, value in properties.items())]
    _need(len(matches) == 1, '逐灯着色有多个候选入口或原节点已改变：' + kind)
    return matches[0]


def _exclusive_group(instance, material):
    group = instance.node_tree
    _need(_local(group) and group.users == 1 and not group.use_fake_user and not group.use_extra_user
          and bpy.data.user_map(subset={group}).get(group, set()) <= {material, material.node_tree},
          '逐灯着色组已被其它使用者引用')
    return group


def _face_groups(scene, material):
    tree = material.node_tree
    owner = _identity(scene)
    stage = tree.nodes.get(material.get(STAGE, ''))
    _need(_local(tree) and stage is not None and stage.bl_idname == 'ShaderNodeGroup'
          and stage.get(STAGE) == PROFILE and stage.get(ROLE) == ROLE_FACE
          and stage.get(OWNER) == owner and not stage.mute,
          '请选择当前项目已接入的面部材质槽')
    group = _exclusive_group(stage, material)
    _need(is_owned_group(group) and group.get(ROLE) == ROLE_FACE and group.get(OWNER) == owner
          and group.get(PROJECT_REF) == scene and group.get(MATERIAL) == material.name_full,
          '面部逐灯组的作者、材质或签名已经改变')
    output = tree.get_output_node('EEVEE')
    _need(output and _input_source(output.inputs['Surface']) == stage.outputs['Surface']
          and _input_source(stage.inputs['Original Surface']) is not None,
          '面部逐灯组的表面连接已经改变')
    keys = [node for node in tree.nodes if node.bl_idname == 'ShaderNodeGroup' and node != stage
            and (node.get(OWNER) == owner or KEY_DIFFUSE in node or node.label == KEY_LABEL
                 or (node.node_tree and node.node_tree.name.startswith(KEY_GROUP_NAME)))]
    _need(len(keys) <= 1, '面部材质有多个主光色阶组')
    if not keys:
        return stage, None
    key = keys[0]
    _need(key.get(OWNER) == owner and not key.mute, '主光色阶组的作者已改变')
    key_group = _exclusive_group(key, material)
    links = list(key.outputs['Shader'].links)
    _need(len(links) == 1 and links[0].to_node.bl_idname == 'ShaderNodeShaderToRGB'
          and links[0].to_socket == links[0].to_node.inputs['Shader']
          and _input_source(links[0].to_socket) == key.outputs['Shader'],
          '主光色阶与原 Shader to RGB 的连接已经改变')
    if SIGNATURE in key_group:
        _need(key_group[SIGNATURE] == _signature(key_group), '主光色阶组已被手动改动')
    return stage, key


def _key_normal_port(group, key):
    direction = _single_node(group, 'ShaderNodeCombineXYZ')
    geom = _single_node(group, 'ShaderNodeNewGeometry')
    cosine = _single_node(group, 'ShaderNodeVectorMath', operation='DOT_PRODUCT')
    value = _single_node(group, 'ShaderNodeMath', operation='MAXIMUM')
    emission = _single_node(group, 'ShaderNodeEmission')
    output = _single_node(group, 'NodeGroupOutput')
    has_port = any(s.item_type == 'SOCKET' and s.in_out == 'INPUT' for s in group.interface.items_tree)
    _need(len(group.nodes) == 6 + int(has_port) and len(group.links) == 5
          and len(group.interface.items_tree) == 1 + int(has_port)
          and not any(n.mute for n in group.nodes) and not value.use_clamp
          and value.inputs[1].default_value == 0. and emission.inputs['Strength'].default_value == 1.
          and _input_source(cosine.inputs[1]) == direction.outputs[0]
          and _input_source(value.inputs[0]) == cosine.outputs['Value']
          and _input_source(emission.inputs['Color']) == value.outputs['Value']
          and _input_source(output.inputs['Shader']) == emission.outputs['Emission'],
          '旧主光色阶组的内部结构已经改变')
    animation = group.animation_data
    _need(animation and not animation.action and not animation.nla_tracks and len(animation.drivers) == 3,
          '主光色阶的驱动已改变')
    for socket, expression in zip(direction.inputs, KEY_EXPRESSIONS):
        curve = animation.drivers.find(socket.path_from_id('default_value'))
        _need(curve and not curve.mute and not curve.modifiers and curve.driver.type == 'SCRIPTED'
              and curve.driver.is_simple_expression and curve.driver.expression == expression
              and len(curve.driver.variables) == 4,
              '主光色阶的方向表达式已经改变')
        for channel, var in zip('wxyz', curve.driver.variables):
            dest = var.targets[0]
            _need(var.name == channel and var.type == 'TRANSFORMS' and dest.id == key
                  and dest.rotation_mode == 'QUATERNION' and dest.transform_space == 'WORLD_SPACE'
                  and dest.transform_type == 'ROT_' + channel.upper(), '主光色阶引用了不同的灯光')
    return cosine.inputs[0], geom.outputs['Normal']


def _forward_math(socket, index):
    links = socket.links
    _need(len(links) == 1, '副光因子存在多个去向或已经断开')
    node = links[0].to_node
    _need(node.bl_idname == 'ShaderNodeMath' and node.operation == 'MULTIPLY'
          and links[0].to_socket == node.inputs[index], '旧副光强度结构已经改变')
    return node


def _face_normal_sockets(group):
    forms = [node for node in group.nodes if node.bl_idname == 'ShaderNodeLightEvaluation'
             and node.mode == 'DIFFUSE' and node.name != FILL_EVALUATION]
    _need(len(forms) == 1, '需要一个明确的原主光 Diffuse 求值节点')
    form = forms[0]
    fill_strength = group.nodes['toon53_fill_strength'].outputs[0]
    fill_side = _forward_math(fill_strength, 0)
    fill_gain = _forward_math(fill_side.outputs[0], 1)
    weight = _input_source(fill_gain.inputs[0])
    _need(weight and weight.node.bl_idname == 'ShaderNodeMath' and weight.node.operation == 'MULTIPLY'
          and weight == weight.node.outputs['Value'], '旧副光的入射权重结构已经改变')
    return form.inputs['Normal'], weight.node.inputs[1]


def _socket_id(sockets, identifier):
    return next(socket for socket in sockets if socket.identifier == identifier)


def face_normal_mix_path(material):
    """Scene property for the face slot's keyframeable Cast Normal mix."""
    return palette_path(material[PALETTE_REF], NORMAL_MIX)


def _check_normal_bridge(scene, material, stage, normal, artist, *, cast):
    face_socket = stage.inputs.get(FACE_NORMAL)
    artist_socket = stage.inputs.get(ARTIST_NORMAL)
    group = stage.node_tree
    main, fill = _face_normal_sockets(group)
    form = main.node
    if face_socket is None:
        _need(artist_socket is None and _input_source(main) is None
              and all(name not in group.nodes for name in (FILL_EVALUATION, FILL_NORMAL, NORMAL_BLEND, NORMAL_UNIT, NORMAL_MIX)),
              '原主光法向入口已经手动改动')
        factor = _input_source(fill)
        _need(factor and factor.node.bl_idname == 'ShaderNodeMath' and factor.node.operation == 'MAXIMUM'
              and factor == factor.node.outputs['Value'] and _input_source(factor.node.inputs[0]) == form.outputs['Factor']
              and factor.node.inputs[1].default_value == 0., '原副光法向入口已经手动改动')
        return False
    _need(normal is not None and face_socket.type == 'VECTOR'
          and _input_source(face_socket) == normal, '原面部法向已断开或被更换')
    entry = _single_node(group, 'NodeGroupInput')
    face_signal = _socket_id(entry.outputs, face_socket.identifier)
    fill_eval = group.nodes.get(FILL_EVALUATION)
    fill_weight = group.nodes.get(FILL_NORMAL)
    _need(fill_eval is not None and fill_eval.bl_idname == 'ShaderNodeLightEvaluation'
          and fill_eval.mode == 'DIFFUSE' and _input_source(fill_eval.inputs['Normal']) is None
          and fill_weight is not None and fill_weight.bl_idname == 'ShaderNodeMath'
          and fill_weight.operation == 'MAXIMUM' and fill_weight.inputs[1].default_value == 0.
          and _input_source(fill_weight.inputs[0]) == fill_eval.outputs['Factor']
          and _input_source(fill) == fill_weight.outputs['Value'],
          '副光独立的原几何法向已经改变')
    if not cast:
        _need(artist_socket is None and NORMAL_MIX not in group.nodes
              and _input_source(main) == face_signal and len(face_signal.links) == 1,
              '作者法向与主光求值的连接已改变')
        return True
    mix = group.nodes.get(NORMAL_BLEND)
    unit = group.nodes.get(NORMAL_UNIT)
    amount = group.nodes.get(NORMAL_MIX)
    path = face_normal_mix_path(material)
    value = scene.get(path)
    _need(mix is not None and mix.bl_idname == 'ShaderNodeMix' and mix.data_type == 'VECTOR'
          and mix.factor_mode == 'UNIFORM' and mix.clamp_factor
          and unit is not None and unit.bl_idname == 'ShaderNodeVectorMath' and unit.operation == 'NORMALIZE'
          and amount is not None and amount.bl_idname == 'ShaderNodeValue'
          and type(value) is float and 0. <= value <= 1.,
          '作者与 Cast 法向的混合设置已改变')
    amount_socket = amount.outputs[0]
    driver = group.animation_data.drivers.find(amount_socket.path_from_id('default_value'))
    _need(driver and driver.driver.type == 'SUM' and len(driver.driver.variables) == 1
          and driver.driver.variables[0].type == 'SINGLE_PROP'
          and driver.driver.variables[0].targets[0].id == scene
          and driver.driver.variables[0].targets[0].data_path == f'["{path}"]',
          '法向混合的镜头参数链接已改变')
    mix_artist = _input_source(_socket_id(mix.inputs, 'A_Vector'))
    if artist is None:
        _need(artist_socket is None and mix_artist and mix_artist.node.bl_idname == 'ShaderNodeNewGeometry'
              and mix_artist.identifier == 'Normal', '作者缺省的几何法向已更改')
    else:
        _need(artist_socket and artist_socket.type == 'VECTOR' and _input_source(artist_socket) == artist
              and mix_artist == _socket_id(entry.outputs, artist_socket.identifier)
              and len(mix_artist.links) == 1, '原艺术法向已断开或被更换')
    _need(_input_source(_socket_id(mix.inputs, 'B_Vector')) == face_signal and len(face_signal.links) == 1
          and _input_source(_socket_id(mix.inputs, 'Factor_Float')) == amount_socket
          and _input_source(unit.inputs[0]) == _socket_id(mix.outputs, 'Result_Vector')
          and _input_source(main) == unit.outputs['Vector'],
          '法向混合与主光求值的连接已改变')
    return True


def _cast_normal_consumers(tree, cast_output, diffuse_normal):
    """Return the exact signed side link compatible with the original Cast witness."""
    stages = [node for node in tree.nodes if node.get(STAGE) == PROFILE and node.get(ROLE) == ROLE_FACE]
    if not any(node.inputs.get(FACE_NORMAL) is not None for node in stages):
        return ()
    _need(len(stages) == 1, '面部逐灯组有多个作者')
    from . import sdf_cast

    stage = stages[0]
    group = stage.node_tree
    scene = group.get(PROJECT_REF)
    material = bpy.data.materials.get(group.get(MATERIAL, ''))
    _need(isinstance(scene, bpy.types.Scene) and material and material.node_tree == tree
          and _input_source(diffuse_normal) == cast_output,
          '逐灯法向与原面部材质没有共同所有权')
    _, key = _face_groups(scene, material)
    _need(key is not None and key.get(KEY_DIFFUSE, diffuse_normal.node.name) == diffuse_normal.node.name,
          '主光色阶来自不同的原 Diffuse')
    original = json.loads(cast_output.node[sdf_cast._ORIGINAL])
    cast_input = sdf_cast._reader_socket(cast_output.node, 'original')
    artist = _input_source(cast_input) if original['linked'] else None
    _need(_check_normal_bridge(scene, material, stage, cast_output, artist, cast=True),
          '面部法向的受管旁路不完整')
    return (stage.inputs[FACE_NORMAL].links[0],)


def _wire_face_normal(scene, target, material, *, diffuse=None):
    stage, key = _face_groups(scene, material)
    info = dict(status='unchanged', material=material.name_full, source='Geometry.Normal')
    if key is None:
        _need(stage.inputs.get(FACE_NORMAL) is None, '主光色阶组失联')
        return info
    from . import normal_ui, sdf_cast
    tree = material.node_tree
    if diffuse is None:
        diffuse = tree.nodes.get(key.get(KEY_DIFFUSE, ''))
        if diffuse is None and KEY_DIFFUSE not in key:
            diffuse, _ = normal_ui._diffuse(tree)
    _need(diffuse is not None and diffuse.bl_idname == 'ShaderNodeBsdfDiffuse' and not diffuse.mute,
          '原脸部 Diffuse 失联或有多个来源')
    _key_normal_port(key.node_tree, scene[KEY_LIGHT])
    normal = _input_source(diffuse.inputs['Normal'])
    artist = normal
    cast_modifier = None
    if normal is not None and normal.node.get(sdf_cast._PROFILE) == sdf_cast.BINDING:
        cast = normal.node
        matches = [mod for mod in target.modifiers if mod.persistent_uid == cast.get(sdf_cast._UID)
                   and sdf_cast.is_managed(mod)]
        _need(len(matches) == 1, '原面部 Cast 法向的修改器失联')
        cast_modifier = matches[0]
        descriptors = sdf_cast.binding_descriptors(target, cast_modifier)
        _need(any(row['material'] == material.name and row['node'] == cast.name for row in descriptors),
              '原面部 Cast 法向的所属材质已经改变')
        original = json.loads(cast[sdf_cast._ORIGINAL])
        artist = _input_source(sdf_cast._reader_socket(cast, 'original')) if original['linked'] else None
    for source in (normal, artist):
        _need(source is None or (source.type == 'VECTOR' and not source.node.mute and source.node != diffuse),
              '原法向须来自明确且启用的向量输出')
    group = stage.node_tree
    applied = _check_normal_bridge(scene, material, stage, normal, artist, cast=cast_modifier is not None)
    if normal is None:
        return info
    info['source'] = f'{normal.node.name}.{normal.identifier}'
    info['artist_source'] = f'{artist.node.name}.{artist.identifier}' if artist else 'Geometry.Normal'
    mix_path = face_normal_mix_path(material) if cast_modifier is not None else None
    if mix_path is not None:
        info['mix_path'] = mix_path
    if applied:
        return info
    _need(mix_path is None or mix_path not in scene, '原有法向参数冲突，保留作者数据')
    previous = group[SIGNATURE]
    main, fill = _face_normal_sockets(group)
    old_fill = _input_source(fill)
    entry = _single_node(group, 'NodeGroupInput')
    nodes, ports = [], []
    amount = None

    def make(kind, name, near, **properties):
        node = group.nodes.new(kind)
        nodes.append(node)
        node.name = node.label = name
        node.location = near
        for field, value in properties.items():
            setattr(node, field, value)
        return node

    def expose(name, source):
        port = group.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketVector')
        ports.append(port)
        port.hide_value = True
        tree.links.new(source, stage.inputs[name])
        return entry.outputs[name]

    try:
        face_signal = expose(FACE_NORMAL, normal)
        form_node = main.node
        near = form_node.location.x - 270., form_node.location.y + 400.
        if mix_path is not None:
            if artist is None:
                geom = make('ShaderNodeNewGeometry', '原几何法向', (near[0] - 270., near[1]))
                artist_signal = geom.outputs['Normal']
            else:
                artist_signal = expose(ARTIST_NORMAL, artist)
            _id_property(scene, mix_path, .5)
            amount = make('ShaderNodeValue', NORMAL_MIX, (near[0] - 270., near[1] - 230.)).outputs[0]
            _set_source(amount, None, (('mix', scene, f'["{mix_path}"]'),))
            mix = make('ShaderNodeMix', NORMAL_BLEND, near, data_type='VECTOR', factor_mode='UNIFORM', clamp_factor=True)
            unit = make('ShaderNodeVectorMath', NORMAL_UNIT, (near[0] + 235., near[1]), operation='NORMALIZE')
            for source, sink in ((artist_signal, _socket_id(mix.inputs, 'A_Vector')),
                                 (face_signal, _socket_id(mix.inputs, 'B_Vector')),
                                 (amount, _socket_id(mix.inputs, 'Factor_Float')),
                                 (_socket_id(mix.outputs, 'Result_Vector'), unit.inputs[0])):
                group.links.new(source, sink)
            face_signal = unit.outputs['Vector']
        group.links.new(face_signal, main)
        fill_eval = make('ShaderNodeLightEvaluation', FILL_EVALUATION,
                         (fill.node.location.x - 470., fill.node.location.y + 250.), mode='DIFFUSE')
        fill_factor = make('ShaderNodeMath', FILL_NORMAL,
                           (fill.node.location.x - 235., fill.node.location.y + 250.), operation='MAXIMUM')
        fill_factor.inputs[1].default_value = 0.
        group.links.new(fill_eval.outputs['Factor'], fill_factor.inputs[0])
        group.links.new(fill_factor.outputs[0], fill)
        group[SIGNATURE] = _signature(group)
        _need(_check_normal_bridge(scene, material, stage, normal, artist, cast=cast_modifier is not None),
              '面部法向接入后未通过检查')
        if cast_modifier is not None:
            sdf_cast.binding_descriptors(target, cast_modifier)
    except Exception:
        if amount is not None:
            amount.driver_remove('default_value')
        for node in reversed(nodes):
            group.nodes.remove(node)
        for port in reversed(ports):
            group.interface.remove(port)
        if _input_source(main) is not None:
            group.links.remove(main.links[0])
        group.links.new(old_fill, fill)
        if mix_path is not None and mix_path in scene:
            del scene[mix_path]
        group[SIGNATURE] = previous
        raise
    scene.update_tag()
    return dict(info, status='repaired')


def repair_face_normal(scene, target, index):
    """Restore the original Normal readers on the active camera's assigned face slot."""
    _available(scene)
    _check_copy()
    controller = scene.get(CONTROLLER)
    _need(scene.camera and controller and controller.parent == scene.camera
          and scene.get(FACE_OBJECT) == target, '请切换到本项目登记的相机和脸部对象')
    return _wire_face_normal(scene, target, _material_context(scene, target, index))


def _family_before(scene):
    from . import style_family_ui

    state = getattr(scene, style_family_ui.PROP, None)
    if state is None:
        saved = scene.get(style_family_ui.PROP)
        _need(saved is None or not saved.get('receipt'), '请从已注册的三渲二工作台升级家族工程')
        return None
    if not state.receipt:
        return None
    _need(bpy.context.scene == scene, '家族迁移须在当前场景执行')
    style_family_ui.inspect_current(bpy.context)
    return state.receipt


def _family_after(scene, previous, changed_highlights):
    if previous is None:
        return
    from . import style_family_ui
    from . import cel_ui

    context = bpy.context
    settings = style_family_ui.settings(context)
    doc = style_family_ui.document(context)
    saved = json.loads(previous)
    slots, _ = cel_ui.bindings(context)
    observe = style_family_ui._core(doc).observe
    current = observe(context, doc, slots, **style_family_ui._kwargs(context, doc))
    old = saved['observation']
    if style_family_ui._json(current) == style_family_ui._json(old):
        return
    normalized = deepcopy(current)
    for name, old_hm in changed_highlights.items():
        if not old_hm or old_hm['highlight_strength'] <= 0.:
            continue
        source = [row for row in old['protected']['hair'] if row['material'] == name]
        updated = [row for row in normalized['protected']['hair'] if row['material'] == name]
        if len(source) == len(updated) == 1:
            _need(updated[0]['settings'] == dict(source[0]['settings'], highlight_strength=0.),
                  '升级后原 HM 只允许交出旧高光')
            updated[0]['settings'] = source[0]['settings']
            updated[0]['kernel_sha256'] = source[0]['kernel_sha256']
    _need(style_family_ui._json(normalized) == style_family_ui._json(old),
          '家族回执检测到本次材质以外的变化，保留原作者记录')
    updated_receipt = style_family_ui._json(dict(saved, observation=current))
    archive = None
    try:
        if FAMILY_ARCHIVE not in scene:
            archive = bpy.data.texts.new('三渲二 5.3 前的原家族回执')
            archive.write(previous)
            scene[FAMILY_ARCHIVE] = archive
        settings.receipt = updated_receipt
    except Exception:
        settings.receipt = previous
        if archive is not None:
            if FAMILY_ARCHIVE in scene:
                del scene[FAMILY_ARCHIVE]
            bpy.data.texts.remove(archive)
        raise


def bind(scene, target, index, role, *, mask=None, uv_name=''):
    """Wrap one explicitly assigned original Surface. The old graph stays editable."""
    _available(scene)
    _check_copy()
    owner = _identity(scene)
    _need(role in (ROLE_FACE, ROLE_HAIR, ROLE_COLOR), '未知逐灯着色角色')
    material = _material_context(scene, target, index)
    tree = material.node_tree
    _need(not material.get(STAGE), '此材质已有 5.3 光照')
    _need(PALETTE_REF not in material,
          '材质已有未识别的 5.3 调色属性')
    output = tree.get_output_node('EEVEE')
    _need(output is not None and len(output.inputs['Surface'].links) == 1
          and not output.inputs['Volume'].is_linked,
          '材质需一个明确的不透明表面输出')
    destination = output.inputs['Surface']
    original = destination.links[0].from_socket
    face_path = old_face_source = old_hm = None
    if role == ROLE_FACE:
        reader, face_path = _direct_face_path(material)
        from . import face_thresholds, face_sdf
        owner_obj = reader.node_tree.get(face_sdf._TARGET)
        modifier = next((m for m in target.modifiers if m.persistent_uid == reader.node_tree.get(
            face_sdf._UID)), None)
        _need(owner_obj == target and modifier is not None, '面影载体不属于当前对象')
        if face_thresholds.inspect(target, modifier, evaluate=False)['light'] != scene[KEY_LIGHT].name:
            raise ValueError('原面影指定的太阳须与 5.3 主光相同，请先在面影作者中确认绑定')
        if face_path is not None:
            old_face_source = face_path.links[0].from_socket
    if role == ROLE_HAIR:
        _need(_local(mask) and mask.source == 'FILE' and mask.packed_file
              and mask.colorspace_settings.name == 'Non-Color' and uv_name in target.data.uv_layers,
              '发丝亮带须绑定已打包的非颜色遮罩和实际存在的 UV')
        from . import finish_nodes, finish_core, hair_map
        legacy_finish = material.get(finish_core.KEY)
        _need(legacy_finish is None or finish_nodes.parameters(legacy_finish)['highlight_strength'] == 0.,
              '请在原作者中关闭已有 CEL 亮带，统一亮带接入当前材质')
        if hair_map.present(material):
            old_hm = hair_map.describe(scene, target, index)['settings']
    previous_family = _family_before(scene)
    stage = node = owned_mask = None
    key_instance = key_group = None
    hm_updated = False
    token = 'toon53m_' + uuid.uuid4().hex[:12]
    try:
        if role == ROLE_HAIR:
            owned_mask = mask.copy()
            owned_mask.name = '三渲二 5.3 亮带 · ' + material.name
        for name, value in {**COLORS, **NUMBERS}.items():
            local_name = palette_path(token, name)
            _id_property(scene, local_name, value)
            scene[local_name + '_override'] = False
        stage = _build(scene, material, role, token, mask=owned_mask, uv_name=uv_name)
        node = tree.nodes.new('ShaderNodeGroup')
        node.node_tree = stage
        node.label = '三渲二 5.3 逐灯控制'
        node.location = (output.location.x - 215, output.location.y)
        node[STAGE], node[OWNER], node[ROLE] = PROFILE, owner, role
        tree.links.new(original, node.inputs['Original Surface'])
        tree.links.new(node.outputs['Surface'], destination)
        if face_path is not None:
            key_instance, key_group = _key_only_shader(tree, face_path, scene[KEY_LIGHT])
            key_instance[OWNER] = owner
        if old_hm and old_hm['highlight_strength'] > 0.:
            hair_map._update(scene, target, index, dict(old_hm, highlight_strength=0.))
            hm_updated = True
            node['toon53_previous_hm_highlight'] = old_hm['highlight_strength']
        material[STAGE] = node.name
        material[PALETTE_REF] = token
        if key_instance is not None:
            key_instance[KEY_DIFFUSE] = old_face_source.node.name
            _wire_face_normal(scene, target, material, diffuse=old_face_source.node)
        scene.update_tag()
        _family_after(scene, previous_family, {material.name: old_hm})
    except Exception:
        if old_face_source is not None:
            tree.links.new(old_face_source, face_path)
        if key_instance is not None:
            tree.nodes.remove(key_instance)
        if key_group is not None and key_group.users == 0:
            bpy.data.node_groups.remove(key_group)
        if node is not None:
            tree.nodes.remove(node)
        tree.links.new(original, destination)
        if stage is not None and stage.users == 0:
            bpy.data.node_groups.remove(stage)
        if owned_mask is not None and owned_mask.users == 0:
            bpy.data.images.remove(owned_mask)
        for name in (*COLORS, *NUMBERS):
            local_name = palette_path(token, name)
            for prop in (local_name, local_name + '_override'):
                if prop in scene:
                    del scene[prop]
        for prop in (STAGE, PALETTE_REF):
            if prop in material:
                del material[prop]
        if hm_updated:
            hair_map._update(scene, target, index, old_hm)
        raise
    return node


def has_hair_stage(material):
    if bpy.app.version[:2] != (5, 3) or not material.get(STAGE):
        return False
    stage = material.node_tree.nodes.get(material[STAGE])
    return stage is not None and stage.get(STAGE) == PROFILE and stage.get(ROLE) == ROLE_HAIR


def add_eyelid_control(scene, material):
    """Add a narrow, independently keyed key-light opening on the bound face."""
    _available(scene)
    _check_copy()
    _need(EYELID_CONTROL not in scene and scene.objects.get(scene[LOCAL_ANCHOR].name) == scene[LOCAL_ANCHOR],
          '场景已有上睑控光，或原局部锚点失联')
    tree = material.node_tree
    instance = tree.nodes.get(material.get(STAGE, ''))
    _need(instance is not None and instance.get(ROLE) == ROLE_FACE
          and instance.get(OWNER) == _identity(scene) and is_owned_group(instance.node_tree),
          '请选择本工作台已绑定的脸部材质槽')
    group = instance.node_tree
    owner = group.nodes['toon53_key_strength'].outputs[0]
    _need(len(owner.links) == 1, '主光形状入口失联')
    gain = owner.links[0].to_node
    _need(gain.bl_idname == 'ShaderNodeMath' and gain.operation == 'MULTIPLY'
          and owner.links[0].to_socket == gain.inputs[1] and len(gain.inputs[0].links) == 1,
          '主光增益结构已改变')
    product = gain.inputs[0].links[0].from_node
    _need(product.bl_idname == 'ShaderNodeMath' and product.operation == 'MULTIPLY'
          and len(product.inputs[1].links) == 1, '主光色带结构已改变')
    destination = product.inputs[1]
    incoming = destination.links[0].from_socket
    _need(incoming.node.bl_idname == 'ShaderNodeMapRange'
          and incoming.node.interpolation_type == 'SMOOTHSTEP', '原主光色带无效')
    nodes = []
    handle = None
    before = group[SIGNATURE]

    def make(kind, **properties):
        node = group.nodes.new(kind)
        nodes.append(node)
        for name, value in properties.items():
            setattr(node, name, value)
        return node

    def put(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            group.links.new(value, socket)
        else:
            socket.default_value = value

    def arithmetic(operation, a, b):
        node = make('ShaderNodeMath', operation=operation)
        put(a, node.inputs[0]); put(b, node.inputs[1])
        return node.outputs[0]

    def native_value(block, path):
        socket = make('ShaderNodeValue').outputs[0]
        _set_source(socket, None, (('control', block, path),))
        return socket

    try:
        anchor = scene[LOCAL_ANCHOR]
        handle = _new_empty(scene, anchor, '5.3 上睑窄区控光', .012, 'SPHERE')
        handle.lock_scale = (True, True, True)
        for name, value, low, high in (
                ('toon53_eyelid_width', .024, .002, .2),
                ('toon53_eyelid_height', .007, .002, .05),
                ('toon53_eyelid_relief', 0., 0., 1.)):
            handle[name] = value
            handle.id_properties_ui(name).update(min=low, max=high)

        point = make('ShaderNodeVectorTransform', vector_type='POINT', convert_from='WORLD',
                     convert_to='CAMERA')
        put(make('ShaderNodeNewGeometry').outputs['Position'], point.inputs['Vector'])
        split = make('ShaderNodeSeparateXYZ')
        put(point.outputs[0], split.inputs[0])
        distances = []
        for index, control in enumerate(('width', 'height')):
            path = f'location[{index}]'
            pivot = arithmetic('ADD', native_value(anchor, path), native_value(handle, path))
            difference = arithmetic('SUBTRACT', split.outputs[index], pivot)
            radius = arithmetic('MAXIMUM', native_value(handle, f'["toon53_eyelid_{control}"]'), .002)
            normalized = arithmetic('DIVIDE', difference, radius)
            distances.append(arithmetic('MULTIPLY', normalized, normalized))
        mask = make('ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        put(arithmetic('ADD', *distances), mask.inputs['Value'])
        put(.24, mask.inputs['From Min']); put(1., mask.inputs['From Max'])
        put(1., mask.inputs['To Min']); put(0., mask.inputs['To Max'])
        lift = arithmetic('MULTIPLY', mask.outputs['Result'], native_value(handle, '["toon53_eyelid_relief"]'))
        put(arithmetic('MAXIMUM', incoming, lift), destination)
        for index, node in enumerate(nodes):
            node.location = ((index % 8) * 230, -1550 - (index // 8) * 220)
        group[SIGNATURE] = _signature(group)
        scene[EYELID_CONTROL] = handle
        return handle
    except Exception:
        group.links.new(incoming, destination)
        for node in reversed(nodes):
            group.nodes.remove(node)
        group[SIGNATURE] = before
        if handle is not None:
            bpy.data.objects.remove(handle, do_unlink=True)
        raise


def original_surface_socket(destination):
    """Resolve the original Surface ingress of a signed wrapper for old authors."""
    if len(destination.links) != 1:
        return destination
    node = destination.links[0].from_node
    if node.get(STAGE) != PROFILE:
        return destination
    original = node.inputs.get('Original Surface')
    _need(node.bl_idname == 'ShaderNodeGroup' and is_owned_group(node.node_tree)
          and original is not None and len(original.links) == 1,
          '5.3 光照包裹节点的原材质入口已变化')
    return original


def save_upgrade(destination):
    """Make a new named 5.3 working copy before editing an existing project."""
    source = Path(bpy.data.filepath)
    destination = Path(destination)
    _available(bpy.context.scene)
    _need(source.is_absolute() and source.is_file() and destination.is_absolute()
          and destination.suffix.lower() == '.blend' and destination.parent.is_dir(),
          '请选择现有工程与有效的新 .blend 路径')
    _need(source.resolve() != destination.resolve() and not destination.exists(),
          '请选择新的另存路径以保留现有文件')
    result = bpy.ops.wm.save_as_mainfile(filepath=str(destination), check_existing=False,
                                        relative_remap=True, copy=False)
    _need('FINISHED' in result and Path(bpy.data.filepath).resolve() == destination.resolve(),
          '未能切换到 5.3 工程副本')
    _upgraded_paths.add(destination.resolve())
    return destination
