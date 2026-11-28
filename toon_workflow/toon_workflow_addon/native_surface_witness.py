# SPDX-License-Identifier: GPL-3.0-or-later
"""Semantic witness for a Blender 5.3 per-light surface left in its original material tree."""

import hashlib
import json
from pathlib import Path
import re

import bpy

from . import lighting_53


KEY = 'toon53_native_opaque_surface'
TERMS = {'Diffuse Light', 'Diffuse Color', 'Glossy Light', 'Glossy Color'}
OTHER_INPUTS = {
    'LightIndex': ('INT', 0),
    'Transmission Light': ('RGBA', (0., 0., 0., 1.)),
    'Transmission Color': ('RGBA', (1., 1., 1., 1.)),
    'Weight': ('VALUE', 0.),
}
ACCUMULATION = 'ShaderNodeLightAccumulation'
NODE_PROPERTIES = {
    'NodeFrame': (), 'NodeGroupInput': (), 'NodeGroupOutput': ('is_active_output',),
    'ShaderNodeMath': ('operation', 'use_clamp'),
    'ShaderNodeVectorMath': ('operation',),
    'ShaderNodeMapRange': ('data_type', 'interpolation_type', 'clamp'),
    'ShaderNodeMix': ('data_type', 'factor_mode', 'blend_type', 'clamp_factor', 'clamp_result'),
    'ShaderNodeMixRGB': ('blend_type', 'use_alpha', 'use_clamp'),
    'ShaderNodeClamp': ('clamp_type',),
    'ShaderNodeCombineXYZ': (), 'ShaderNodeSeparateXYZ': (),
    'ShaderNodeSeparateColor': ('mode',), 'ShaderNodeCombineColor': ('mode',),
    'ShaderNodeVectorTransform': ('vector_type', 'convert_from', 'convert_to'),
    'ShaderNodeNewGeometry': (),
    'ShaderNodeTexCoord': ('object', 'from_instancer'),
    'ShaderNodeTexImage': ('image', 'extension', 'interpolation', 'projection', 'projection_blend'),
    'ShaderNodeUVMap': ('uv_map', 'from_instancer'),
    'ShaderNodeAttribute': ('attribute_name', 'attribute_type'),
    'ShaderNodeRGB': (), 'ShaderNodeValue': (),
    'ShaderNodeLightInfo': (), 'ShaderNodeLightEvaluation': ('mode',),
    'ShaderNodeShadowRaycast': (),
}
MATRIX_PATH = re.compile(r'matrix_world\[[0-3]\]\[[0-3]\]')


def _need(condition, detail):
    if not condition:
        raise ValueError(detail)


def _value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bpy.types.Image):
        _need(value.source == 'FILE' and value.library is None, '原生表面只支持本地静态图像')
        if value.packed_file:
            source = ('packed', value.packed_file.size)
        else:
            path = Path(bpy.path.abspath(value.filepath))
            _need(path.is_absolute() and path.is_file(), '原生表面使用缺失的外部图像')
            source = ('file', str(path.resolve()))
        return ('Image', value.name_full, source, value.colorspace_settings.name, value.alpha_mode)
    if isinstance(value, bpy.types.ID):
        _need(value.library is None and value.override_library is None, '原生表面使用外链数据')
        return (value.bl_rna.identifier, value.name_full)
    return list(value)


def _socket(socket, driven):
    if hasattr(socket, 'default_value') and socket.path_from_id('default_value') in driven:
        return (socket.identifier, socket.type, 'driven')
    return (socket.identifier, socket.type, _value(getattr(socket, 'default_value', None)))


def _drivers(tree, nodes):
    animation = tree.animation_data
    if animation is None:
        return []
    _need(animation.action is None and not animation.nla_tracks, '原生表面图含未支持的节点动作')
    paths = {socket.path_from_id('default_value'): socket for node in nodes
             for socket in (*node.inputs, *node.outputs) if hasattr(socket, 'default_value')}
    result = []
    for curve in animation.drivers:
        socket = paths.get(curve.data_path)
        if socket is None:
            continue
        _need(socket.type == 'VALUE' and curve.array_index == 0
              and (socket.node.bl_idname != ACCUMULATION or socket.identifier in TERMS),
              '原生表面只支持单值方向驱动')
        driver = curve.driver
        _need(driver.type in {'AVERAGE', 'SUM'} and not curve.mute and not curve.modifiers
              and len(driver.variables) == 1, '原生表面驱动必须是可核对的简单矩阵读取')
        variable = driver.variables[0]
        target = variable.targets[0]
        _need(variable.type == 'SINGLE_PROP' and isinstance(target.id, bpy.types.Object)
              and MATRIX_PATH.fullmatch(target.data_path), '原生表面驱动超出头轴与灯方向范围')
        result.append((curve.data_path, curve.array_index, driver.type, _value(target.id), target.data_path))
    return sorted(result)


def _visit(tree, roots, data_only, cache, active):
    key = (tree, data_only)
    if data_only and key in cache:
        return cache[key]
    _need(tree not in active and len(active) < 12 and tree.library is None, '原生表面组嵌套不可核对')
    active.add(tree)
    try:
        nodes, links = set(), set()
        pending = list(roots)
        while pending:
            node = pending.pop()
            if node in nodes:
                continue
            nodes.add(node)
            _need(len(nodes) <= 512 and not node.mute, '原生表面图包含过多或静音节点')
            if node.bl_idname == ACCUMULATION:
                _need(not data_only and node in roots, '数据支路中不允许着色闭包')
                _need({socket.identifier for socket in node.inputs} == TERMS | OTHER_INPUTS.keys(),
                      '原生表面的逐灯输入布局已变更')
                for socket in node.inputs:
                    if socket.identifier in TERMS:
                        continue
                    _need(not socket.is_linked, '原生表面只能使用漫反射与光泽项')
                    socket_type, default = OTHER_INPUTS[socket.identifier]
                    value = socket.default_value
                    _need(socket.type == socket_type and (tuple(value) if socket_type == 'RGBA' else value)
                          == default, '原生不透明表面的其它逐灯输入须保留默认值')
                _need(all(socket.name == 'Shader' or not socket.is_linked for socket in node.outputs),
                      '原生表面含未支持的输出')
            else:
                _need(node.bl_idname in NODE_PROPERTIES or node.bl_idname == 'ShaderNodeGroup',
                      '原生表面含未支持节点 ' + node.bl_idname)
                _need(not any(socket.type == 'SHADER' for socket in (*node.inputs, *node.outputs)),
                      '上游数据节点含着色闭包')
                if node.bl_idname == 'ShaderNodeLightEvaluation':
                    _need(node.mode in {'DIFFUSE', 'GLOSSY'}, '原生表面含透射光求值')
            for socket in node.inputs:
                for link in socket.links:
                    _need(link.is_valid and not link.is_muted, '原生表面含无效或静音连线')
                    links.add((link.from_node.name, link.from_socket.identifier, node.name, socket.identifier))
                    pending.append(link.from_node)
        rows = []
        drivers = _drivers(tree, nodes)
        driven = {row[0] for row in drivers}
        for node in sorted(nodes, key=lambda item: item.name):
            props = [(name, _value(getattr(node, name))) for name in NODE_PROPERTIES.get(node.bl_idname, ())]
            if node.bl_idname == 'ShaderNodeGroup':
                child = node.node_tree
                _need(child is not None, '原生表面含空节点组')
                props.append(('child', _visit(child, list(child.nodes), True, cache, active)))
            rows.append((node.name, node.bl_idname, props,
                         [_socket(socket, driven) for socket in (*node.inputs, *node.outputs)]))
        data = (rows, sorted(links), drivers)
        signed = hashlib.sha256(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if data_only:
            cache[key] = signed
        return signed
    finally:
        active.remove(tree)


def signature(tree):
    roots = [node for node in tree.nodes if node.bl_idname == ACCUMULATION]
    _need(roots and tree.type == 'SHADER', '请选择包含原生逐灯累积的着色材质')
    return _visit(tree, roots, False, {}, set())


def qualify(scene, material):
    lighting_53._available(scene)
    lighting_53._check_copy()
    _need(lighting_53._local(material) and material.node_tree, '请选择本地节点材质')
    tree = material.node_tree
    tree[KEY] = signature(tree)
    return sum(node.bl_idname == ACCUMULATION for node in tree.nodes)


def valid(tree):
    try:
        return KEY in tree and tree[KEY] == signature(tree)
    except (ValueError, TypeError, AttributeError, ReferenceError, RuntimeError):
        return False
