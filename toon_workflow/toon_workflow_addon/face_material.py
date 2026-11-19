# SPDX-License-Identifier: GPL-3.0-or-later
"""Native, editable face-threshold reader and explicit Emission.Color binding.

Comparison takes place in the shader after scalar-attribute interpolation.
The added layer can only darken RGB. Surface, Alpha and Normal are untouched.
"""
import hashlib
import json
import math
import uuid

import bpy

PROFILE = 'toon-face-threshold-shader.v1'
PROFILE_KEY = 'toon_face_threshold_profile'
OWNER = 'toon_face_threshold_owner'
_TARGET = 'toon_face_threshold_target'
_UID = 'toon_face_threshold_modifier_uid'
_FIELDS = 'toon_face_threshold_attributes'
_SIGNATURE = 'toon_face_threshold_signature'
_NODE = 'toon_face_threshold_node'
CORE_SIGNATURE = 'toon_face_threshold_core_signature'
_MARK = 'toon_face_material_binding'
_TOKEN = 'toon_face_material_token'
_TREE = 'toon_face_material_reader'
_DEST = 'toon_face_material_destination'
_SOURCE = 'toon_face_material_source'
_ORIGINAL = 'toon_face_material_original'
_SNAPSHOT = 'toon_face_material_snapshot'
_BINDING = 'toon-face-material-binding.v1'


def _core():
    from . import face_thresholds
    return face_thresholds


def _value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bpy.types.ID):
        return ['ID', value.bl_rna.identifier, value.name_full]
    try:
        return [_value(item) for item in value]
    except TypeError:
        return str(value)


def _socket(sockets, identifier):
    matches = [s for s in sockets if s.identifier == identifier]
    if len(matches) != 1:
        raise ValueError('节点缺少唯一插槽：' + identifier)
    return matches[0]


def _animated(tree):
    animation = tree.animation_data
    return animation is not None and bool(animation.action or animation.drivers or animation.nla_tracks)


def _related_animation(tree, sockets, nodes=()):
    from .artistic_normals import _animated_paths
    paths = _animated_paths(tree)
    if any(s.path_from_id('default_value') in paths for s in sockets):
        return True
    prefixes = [node.path_from_id() for node in nodes]
    return any(path == base or path.startswith(base + '.') or path.startswith(base + '[')
               for path in paths for base in prefixes)


def _node_state(node):
    properties = ('operation', 'blend_type', 'use_clamp', 'use_alpha', 'clamp',
                  'interpolation_type', 'data_type', 'attribute_name', 'attribute_type', 'mute', 'is_active_output')
    return [node.bl_idname,
            {key: _value(getattr(node, key)) for key in properties if hasattr(node, key)},
            [(s.identifier, _value(s.default_value)) for s in node.inputs if hasattr(s, 'default_value')]]


def _signature(group):
    rows = [(n.get(_NODE), _node_state(n)) for n in group.nodes]
    links = [(l.from_node.get(_NODE), l.from_socket.identifier,
              l.to_node.get(_NODE), l.to_socket.identifier) for l in group.links]
    interface = [{key: _value(getattr(s, key)) for key in
                  ('identifier', 'name', 'in_out', 'socket_type', 'default_value', 'min_value', 'max_value')
                  if hasattr(s, key)} for s in group.interface.items_tree if s.item_type == 'SOCKET']
    payload = dict(nodes=sorted(rows, key=repr), links=sorted(links, key=repr),
                   interface=interface, fields=group.get(_FIELDS))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def reader_edited(group):
    """Conservative protection check, including its exact live GN owner binding."""
    try:
        if (group.bl_idname != 'ShaderNodeTree' or group.get(PROFILE_KEY) != PROFILE
                or _animated(group) or _signature(group) != group.get(_SIGNATURE)):
            return True
        target = group.get(_TARGET)
        if not isinstance(target, bpy.types.Object):
            return True
        matches = [m for m in target.modifiers if m.persistent_uid == group.get(_UID)
                   and m.type == 'NODES' and _core().is_managed(m)]
        if len(matches) != 1 or not _core().is_managed(matches[0]):
            return True
        core_group = matches[0].node_group
        return (core_group.get(OWNER) != group.get(OWNER)
                or core_group.get(_TARGET) != target or core_group.get(_UID) != group.get(_UID)
                or core_group.get(_FIELDS) != group.get(_FIELDS)
                or _core().graph_signature(core_group) != group.get(CORE_SIGNATURE))
    except (AttributeError, KeyError, TypeError, ValueError, ReferenceError, RuntimeError):
        return True


def shader_group(modifier):
    """Create an unbound native reader. Alpha preservation is not promised."""
    core = _core()
    target = modifier.id_data
    report = core.inspect(target, modifier, evaluate=False)
    if report['status'] != 'valid':
        raise ValueError('面影字段无效：' + '; '.join(report.get('issues', [])))
    fields = report['attributes']
    shader = bpy.data.node_groups.new('Toon Face Threshold Reader', 'ShaderNodeTree')
    try:
        shader[PROFILE_KEY], shader[OWNER] = PROFILE, report['owner']
        shader[_TARGET], shader[_UID] = target, report['modifier_uid']
        shader[_FIELDS] = json.dumps(fields, sort_keys=True)
        shader[CORE_SIGNATURE] = core.graph_signature(modifier.node_group)
        for name, kind, default in (('Original Color', 'NodeSocketColor', (1., 1., 1., 1.)),
                                    ('Shadow Color', 'NodeSocketColor', (.45, .34, .31, 1.)),
                                    ('Softness', 'NodeSocketFloat', .02)):
            item = shader.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
            item.default_value = default
            if kind == 'NodeSocketFloat':
                item.min_value, item.max_value = 0., 1.
        shader.interface.new_socket(name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')
        def node(key, kind, **props):
            result = shader.nodes.new(kind)
            result[_NODE] = key
            for name, value in props.items():
                setattr(result, name, value)
            return result
        def link(source, destination):
            shader.links.new(source, destination)
        def math_node(key, operation, a=None, b=None):
            result = node(key, 'ShaderNodeMath', operation=operation)
            for i, value in enumerate((a, b)):
                if value is not None:
                    if isinstance(value, (int, float)):
                        result.inputs[i].default_value = value
                    else:
                        link(value, result.inputs[i])
            return result.outputs[0]
        entry = node('inputs', 'NodeGroupInput')
        output = node('output', 'NodeGroupOutput')
        attrs = {name: node('attribute_' + name, 'ShaderNodeAttribute',
                           attribute_type='GEOMETRY', attribute_name=fields[name]).outputs['Fac']
                 for name in ('threshold', 'phase', 'mix', 'valid')}
        threshold, phase = attrs['threshold'], attrs['phase']
        soft = math_node('nonnegative_softness', 'MAXIMUM', entry.outputs['Softness'], 0.)
        positive = math_node('softness_positive', 'GREATER_THAN', soft, 0.)
        hard = math_node('strict_lit', 'GREATER_THAN', threshold, phase)
        low = math_node('soft_low', 'SUBTRACT', phase, soft)
        high = math_node('soft_high', 'ADD', phase, soft)
        smooth = node('smooth_lit', 'ShaderNodeMapRange', interpolation_type='SMOOTHSTEP', clamp=True)
        link(threshold, smooth.inputs['Value'])
        link(low, smooth.inputs['From Min']); link(high, smooth.inputs['From Max'])
        smooth.inputs['To Min'].default_value = 0.; smooth.inputs['To Max'].default_value = 1.
        hard_weight = math_node('hard_weight', 'SUBTRACT', 1., positive)
        lit = math_node('lit', 'ADD', math_node('hard_lit', 'MULTIPLY', hard, hard_weight),
                        math_node('soft_lit', 'MULTIPLY', smooth.outputs['Result'], positive))
        shadow = math_node('shadow', 'SUBTRACT', 1., lit)
        mix = math_node('mix_max', 'MINIMUM', math_node('mix_min', 'MAXIMUM', attrs['mix'], 0.), 1.)
        valid = math_node('valid_gate', 'GREATER_THAN', attrs['valid'], .5)
        dark = math_node('darkening', 'MULTIPLY', math_node('shadow_mix', 'MULTIPLY', shadow, mix), valid)
        minimum = node('rgb_minimum', 'ShaderNodeMixRGB', blend_type='DARKEN', use_clamp=False)
        minimum.inputs[0].default_value = 1.
        link(entry.outputs['Original Color'], minimum.inputs[1])
        link(entry.outputs['Shadow Color'], minimum.inputs[2])
        blend = node('final_darkening', 'ShaderNodeMixRGB', blend_type='MIX', use_clamp=False)
        link(dark, blend.inputs[0]); link(entry.outputs['Original Color'], blend.inputs[1])
        link(minimum.outputs[0], blend.inputs[2]); link(blend.outputs[0], output.inputs['Color'])
        for i, item in enumerate(shader.nodes):
            item.location = ((i % 6) * 220, -(i // 6) * 200)
        shader[_SIGNATURE] = _signature(shader)
        return shader
    except Exception:
        bpy.data.node_groups.remove(shader)
        raise


def _emission(tree):
    matches = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeEmission']
    if len(matches) != 1:
        raise ValueError('活动材质须恰有一个原生Emission；不猜测脸部材质')
    return matches[0], _socket(matches[0].inputs, 'Color')


def _binding_state(tree, token):
    def identity(node):
        if node.get(_TOKEN) == token:
            return ['reader', token]
        if node.get(_DEST) == token:
            return ['destination', token]
        if node.get(_SOURCE) == token:
            return ['source', token]
        return ['node', node.name]
    readers = [n for n in tree.nodes if n.get(_TOKEN) == token]
    destinations = [n for n in tree.nodes if n.get(_DEST) == token]
    if len(readers) != 1 or len(destinations) != 1:
        return None
    reader, destination = readers[0], destinations[0]
    links = [(identity(l.from_node), l.from_socket.identifier,
              identity(l.to_node), l.to_socket.identifier) for l in tree.links
             if l.from_node == reader or l.to_node == reader]
    return json.dumps(dict(reader=_node_state(reader), links=sorted(links, key=repr),
        destination_color=_value(_socket(destination.inputs, 'Color').default_value),
        original=reader.get(_ORIGINAL)),
                      sort_keys=True, allow_nan=False)


def _clean_marker(node, key, token):
    if node.get(key) == token:
        del node[key]


def _remove_instance(tree, node):
    # Blender 5.2.1 does not release nested node IDProperty user counts when
    # removing the node. Release only our two witnessed strong references first.
    for key in (_TREE, _TARGET):
        if key in node:
            del node[key]
    tree.nodes.remove(node)


def bind(target, modifier, *, shadow_color=(.45, .34, .31, 1.), softness=.02):
    """Bind only the explicitly active, target-exclusive material's Emission.Color."""
    if not isinstance(target, bpy.types.Object) or getattr(modifier, 'id_data', None) != target:
        raise ValueError('面影修改器必须属于明确指定的目标对象')
    _core()._owned(target, modifier)
    from .normal_ui import _material
    material = _material(target); tree = material.node_tree
    destination, color = _emission(tree)
    if _related_animation(tree, [color]):
        raise ValueError('Emission.Color有原生动画，保持输入动画不变')
    if any(any(key in n for key in (_MARK, _DEST, _SOURCE)) for n in tree.nodes):
        raise ValueError('已有面影绑定或残留标记，保持当前材质')
    if len(color.links) > 1:
        raise ValueError('Emission.Color输入不唯一')
    if (len(shadow_color) != 4 or not all(math.isfinite(float(v)) for v in shadow_color)
            or type(softness) not in (float, int) or not math.isfinite(softness) or not 0 <= softness <= 1):
        raise ValueError('阴影颜色须为有限RGBA，柔和度须在0..1')
    old = color.links[0] if color.links else None
    source, source_socket = (old.from_node, old.from_socket) if old else (None, None)
    if source == destination:
        raise ValueError('Emission自连接不支持安全恢复')
    constant = tuple(color.default_value)
    shader = shader_group(modifier)
    token = uuid.uuid4().hex
    node = None
    try:
        node = tree.nodes.new('ShaderNodeGroup'); node.node_tree = shader
        node.label = '面影附加压暗层'
        node.location = (destination.location.x - 240, destination.location.y - 160)
        node[_MARK], node[_TOKEN], node[_TREE] = _BINDING, token, shader
        node[_TARGET], node[_UID], node[OWNER] = target, modifier.persistent_uid, shader[OWNER]
        destination[_DEST] = token
        if source is not None:
            source[_SOURCE] = token
            tree.links.new(source_socket, node.inputs['Original Color'])
        else:
            node.inputs['Original Color'].default_value = constant
        node.inputs['Shadow Color'].default_value = shadow_color
        node.inputs['Softness'].default_value = softness
        node[_ORIGINAL] = json.dumps(dict(linked=source is not None,
            source_socket=source_socket.identifier if source_socket else None,
            color=list(constant)), allow_nan=False)
        tree.links.new(node.outputs['Color'], color)
        node[_SNAPSHOT] = _binding_state(tree, token)
    except Exception:
        if node is not None:
            _remove_instance(tree, node)
        _clean_marker(destination, _DEST, token)
        if source is not None:
            _clean_marker(source, _SOURCE, token)
            tree.links.new(source_socket, color)
        color.default_value = constant
        if shader.users == 0:
            bpy.data.node_groups.remove(shader)
        raise
    return dict(material=material.name, node=node.name, reader=shader.name)


def unbind(target, modifier):
    """Preflight every witness before restoring the exact original input."""
    if not isinstance(target, bpy.types.Object) or getattr(modifier, 'id_data', None) != target:
        raise ValueError('面影修改器必须属于明确指定的目标对象')
    _core()._owned(target, modifier)
    from .normal_ui import _material
    material = _material(target); tree = material.node_tree
    marked = [n for n in tree.nodes if _MARK in n]
    if len(marked) != 1:
        raise ValueError('没有唯一面影UI实例；不删除作者节点')
    node = marked[0]; token = node.get(_TOKEN)
    shader = node.node_tree if node.bl_idname == 'ShaderNodeGroup' else None
    if (node.get(_MARK) != _BINDING or not isinstance(token, str) or len(token) != 32
            or node.get(_TARGET) != target or node.get(_UID) != modifier.persistent_uid
            or shader is None or node.get(_TREE) != shader
            or shader.get(_TARGET) != target or shader.get(_UID) != modifier.persistent_uid
            or node.get(OWNER) != shader.get(OWNER) or reader_edited(shader)):
        raise ValueError('面影实例、核心身份或读者已被修改；拒绝解绑')
    destination, color = _emission(tree)
    if _related_animation(tree, [color], nodes=[node]) or shader.users != 2:
        raise ValueError('相关输入有动画或读者有其它消费者；保留当前状态')
    destinations = [n for n in tree.nodes if n.get(_DEST) == token]
    if destinations != [destination] or _binding_state(tree, token) != node.get(_SNAPSHOT):
        raise ValueError('原节点身份、相关输入值或读者链接已改变；保留作者编辑')
    original = json.loads(node[_ORIGINAL])
    sources = [n for n in tree.nodes if n.get(_SOURCE) == token]
    if len(sources) != int(original['linked']):
        raise ValueError('原颜色来源身份已改变；拒绝恢复')
    source = sources[0] if sources else None
    source_socket = _socket(source.outputs, original['source_socket']) if source else None
    # Exact restoration is synchronous. No graph changes precede all checks.
    # Create the replacement link before deleting our instance, so a failed
    # native link creation leaves the original managed binding intact.
    if source_socket is not None:
        try:
            tree.links.new(source_socket, color)
        except Exception:
            tree.links.new(node.outputs['Color'], color)
            raise
    color.default_value = original['color']
    name = node.name
    _remove_instance(tree, node)
    _clean_marker(destination, _DEST, token)
    if source is not None:
        _clean_marker(source, _SOURCE, token)
    return dict(material=material.name, removed_node=name, reader_retained=shader.name)
