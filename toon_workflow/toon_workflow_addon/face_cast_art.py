# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in artistic shadow acceptance before a live face SDF reader.

White keeps upstream shading. Black blends toward an explicitly authored lit
color, restricted by the existing face mix/valid fields. This also suppresses
external contact shadows in that region; it is not selective self-shadow removal.
The legacy v1 stays static. Explicit v2 owns one internal strength Action;
geometry, normals, UVs, lights and Surface links are never authored here.
"""
import hashlib
import json
import math
import uuid

import bpy

PROFILE = 'toon-face-cast-art.v1'
PROFILE_V2 = 'toon-face-cast-art.v2'
PROFILE_KEY = 'toon_face_cast_art_profile'
OWNER = 'toon_face_cast_art_owner'
SCHEMA = 'toon-face-cast-art-descriptor.v1'
SCHEMA_V2 = 'toon-face-cast-art-descriptor.v2'
FORMULA = 'clamp(strength*mix*valid*clamp((white-channel)/(white-black),0,1),0,1)'

_PREFIX = 'toon_face_cast_art_'
_MARK = _PREFIX + 'binding'
_TARGET = _PREFIX + 'target'
_UID = _PREFIX + 'modifier_uid'
_SDF_OWNER = _PREFIX + 'sdf_owner'
_SDF_TOKEN = _PREFIX + 'sdf_token'
_UV = _PREFIX + 'uv_name'
_FIELDS = _PREFIX + 'fields'
_CHANNEL = _PREFIX + 'channel'
_IMAGE = _PREFIX + 'image_descriptor'
_GRAPH = _PREFIX + 'graph_sha256'
_NODE = _PREFIX + 'node'
_SOURCE = _PREFIX + 'source'
_OLD_ORIGINAL = _PREFIX + 'old_sdf_original'
_OLD_SNAPSHOT = _PREFIX + 'old_sdf_snapshot'
_DECLARED = _PREFIX + 'declared_settings'
_BINDING_SHA = _PREFIX + 'binding_sha256'

_INPUTS = ('Original Color', 'Strength', 'Lit Color', 'Black Level', 'White Level')
_CHANNEL_NAMES = {'R': 'Red', 'G': 'Green', 'B': 'Blue'}
_NODE_KINDS = {
    'input': 'NodeGroupInput', 'output': 'NodeGroupOutput',
    'uv': 'ShaderNodeUVMap', 'image': 'ShaderNodeTexImage',
    'channel': 'ShaderNodeSeparateColor', 'suppression': 'ShaderNodeMapRange',
    'mix': 'ShaderNodeAttribute', 'valid': 'ShaderNodeAttribute',
    'region': 'ShaderNodeMath', 'gate': 'ShaderNodeMath',
    'strength': 'ShaderNodeMath', 'color': 'ShaderNodeMixRGB',
}


class CastArtError(ValueError):
    """Invalid owned data, unsupported consumer, or author conflict."""


def _sdf():
    from . import face_sdf
    return face_sdf


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf8')).hexdigest()


def _json(value, label):
    if not isinstance(value, str):
        raise CastArtError(label + ' is missing')
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as error:
        raise CastArtError(label + ' is invalid JSON') from error
    if type(result) is not dict:
        raise CastArtError(label + ' must be a JSON object')
    return result


def _owner(value):
    return (type(value) is str and len(value) == 32
            and all(c in '0123456789abcdef' for c in value))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _settings(strength, lit_color, black_level, white_level):
    if not _finite(strength) or not 0. <= strength <= 1.:
        raise CastArtError('Strength must be finite in [0,1]')
    if (not isinstance(lit_color, (tuple, list)) or len(lit_color) != 4
            or any(not _finite(v) or not 0. <= v <= 1. for v in lit_color)):
        raise CastArtError('Lit color must be four finite RGBA values in [0,1]')
    if (not _finite(black_level) or not _finite(white_level)
            or not 0. <= black_level < white_level <= 1.):
        raise CastArtError('Levels require finite 0 <= black < white <= 1')
    return dict(strength=float(strength), lit_color=[float(v) for v in lit_color],
                black_level=float(black_level), white_level=float(white_level))


def _actual_settings(node):
    group = node.node_tree
    strength = (_strength_socket(group).default_value
                if group.get(PROFILE_KEY) == PROFILE_V2
                else node.inputs['Strength'].default_value)
    return _settings(float(strength),
                     [float(v) for v in node.inputs['Lit Color'].default_value],
                     float(node.inputs['Black Level'].default_value),
                     float(node.inputs['White Level'].default_value))


def _write_settings(node, values):
    if node.node_tree.get(PROFILE_KEY) == PROFILE_V2:
        _strength_socket(node.node_tree).default_value = values['strength']
    else:
        node.inputs['Strength'].default_value = values['strength']
    node.inputs['Lit Color'].default_value = values['lit_color']
    node.inputs['Black Level'].default_value = values['black_level']
    node.inputs['White Level'].default_value = values['white_level']
    # Sign the actual native float32 values rather than the input Python doubles.
    node[_DECLARED] = _canonical(_actual_settings(node))


def _strength_socket(group):
    # No author inspection recursion: shape/ownership are checked by _state.
    nodes = [n for n in group.nodes if n.get(_NODE) == 'strength_value']
    if len(nodes) != 1 or nodes[0].bl_idname != 'ShaderNodeValue':
        raise CastArtError('Acceptance has no unique internal strength Value')
    return nodes[0].outputs[0]


def _context(context):
    target = _sdf()._context_target(context)
    from .normal_ui import _material
    material = _material(target)
    return target, material


def _local_material(material):
    if (not isinstance(material, bpy.types.Material) or material.library
            or material.override_library or not material.is_editable
            or not material.use_nodes or material.node_tree is None
            or material.node_tree.library or not material.node_tree.is_editable):
        raise CastArtError('An editable local node Material is required')
    return material.node_tree


def is_present(material):
    """Also detect damaged reserved markers; never silently overwrite them."""
    if material is None:
        return False
    if any(str(k).startswith(_PREFIX) for k in material.keys()):
        return True
    tree = getattr(material, 'node_tree', None)
    if tree is None:
        return False
    if any(str(k).startswith(_PREFIX) for k in tree.keys()):
        return True
    return any(any(str(k).startswith(_PREFIX) for k in n.keys())
               or (n.bl_idname == 'ShaderNodeGroup' and n.node_tree is not None
                   and any(str(k).startswith(_PREFIX) for k in n.node_tree.keys()))
               for n in tree.nodes)


def _image_descriptor(image, channel, group=None):
    """Separate image ownership contract; no change to legacy SDF rules."""
    T = _sdf()
    value = T._image_content_descriptor(image, channel)
    if image.use_extra_user:
        raise CastArtError('Acceptance image has an unqualified extra user')
    allowed = set()
    expected = int(image.use_fake_user)
    if group is not None:
        allowed.add(group)
        expected += sum(n.bl_idname == 'ShaderNodeTexImage' and n.image == image
                        for n in group.nodes)
    from . import sdf_ui
    prop = bpy.types.Scene.bl_rna.properties.get(sdf_ui.PROP)
    schema_owned = (sdf_ui._registered and prop is not None
                    and prop.as_pointer() == sdf_ui._descriptor)

    def no_custom_reference(value):
        if isinstance(value, bpy.types.Image):
            return value != image
        if isinstance(value, bpy.types.ID):
            return True
        if hasattr(value, 'items'):
            return all(no_custom_reference(v) for v in value.values())
        return True

    for scene in bpy.data.scenes:
        settings = getattr(scene, sdf_ui.PROP, None) if schema_owned else None
        if (not isinstance(settings, sdf_ui.TOONSDF_Settings)
                or getattr(settings, 'cast_art_image', None) != image):
            continue
        # user_map deduplicates owner IDs. Count the real known picker once;
        # another pointer (even on this Scene) must remain unexplained.
        if all(no_custom_reference(v) for v in scene.values()):
            allowed.add(scene)
            expected += 1
    consumers = T._image_consumers(image)
    if consumers - allowed or image.users != expected:
        raise CastArtError('Acceptance image has shared or unexplained consumers; '
                           'a dedicated image is required')
    return value


def _sdf_instance(material, target=None, *, live=True):
    T = _sdf()
    tree = _local_material(material)
    nodes = [n for n in tree.nodes if T._MARK in n]
    if len(nodes) != 1 or nodes[0].bl_idname != 'ShaderNodeGroup':
        raise CastArtError('The material must have one explicit live SDF binding')
    node = nodes[0]
    group = node.node_tree
    profile = group.get(T.PROFILE_KEY) if group is not None else None
    if type(profile) is not str or profile not in T.SUPPORTED_PROFILES:
        raise CastArtError('SDF profile identity changed')
    if target is None:
        target = group.get(T._TARGET)
    if target != group.get(T._TARGET):
        raise CastArtError('SDF target differs from the explicit target')
    if live:
        from .normal_ui import _material
        # Author operations already use _material through _context. Read-only
        # public capture must also support a non-active material without ever
        # changing selection or the active material index.
        if target.active_material == material:
            if _material(target) != material:
                raise CastArtError('SDF Material is not exclusive')
        else:
            if material not in [slot.material for slot in target.material_slots]:
                raise CastArtError('Explicit SDF Material is not on its target')
            if any(obj != target and any(slot.material == material
                   for slot in getattr(obj, 'material_slots', ()))
                   for obj in bpy.data.objects):
                raise CastArtError('Explicit SDF Material is used by another Object')
            consumers = bpy.data.user_map(subset={material}).get(material, set())
            if any(user not in {target, target.data} for user in consumers):
                raise CastArtError('Explicit SDF Material has another datablock consumer')
        T.validate_reader(group, target=target, require_live=True)
    else:
        T._intrinsic(group)
    binding = T._binding_info(group)
    if binding['state'] != 'bound' or binding['node'] != node:
        raise CastArtError('SDF material binding is not intact')
    return target, node, group, binding


def _node(group, role, kind, **properties):
    node = group.nodes.new(kind)
    node[_NODE] = role
    node.label = role
    for key, value in properties.items():
        setattr(node, key, value)
    return node


def _interface(group):
    return [{k: _sdf()._value(getattr(s, k)) for k in
             ('identifier', 'name', 'in_out', 'socket_type', 'default_value',
              'min_value', 'max_value') if hasattr(s, k)}
            for s in group.interface.items_tree if s.item_type == 'SOCKET']


def graph_signature(group):
    """Sign owned shader semantics, not native ID runtime/lifetime flags."""
    T = _sdf()
    payload = dict(
        nodes=sorted([(n.get(_NODE), T._node_state(n),
                       {k: T._value(v) for k, v in n.items()})
                      for n in group.nodes], key=repr),
        links=sorted([(l.from_node.get(_NODE), l.from_socket.identifier,
                       l.to_node.get(_NODE), l.to_socket.identifier)
                      for l in group.links], key=repr),
        interface=_interface(group),
        metadata={k: T._value(v) for k, v in group.items() if k != _GRAPH
                  and not (group.get(PROFILE_KEY) == PROFILE_V2 and k in
                           {'toon_cast_animation_profile', 'toon_cast_animation_action_uuid'})})
    return _digest(payload)


def _build_group(target, sdf_group, image, image_desc, channel, *, animate_strength=False):
    T = _sdf()
    sdf_info = T._intrinsic(sdf_group)
    fields = {k: sdf_info['fields'][k] for k in ('mix', 'valid')}
    owner = uuid.uuid4().hex
    group = bpy.data.node_groups.new('Toon Face Cast Art', 'ShaderNodeTree')
    try:
        for key, value in ((PROFILE_KEY, PROFILE_V2 if animate_strength else PROFILE), (OWNER, owner),
                           (_TARGET, target), (_UID, sdf_info['uid']),
                           (_SDF_OWNER, sdf_info['owner']),
                           (_UV, sdf_info['uv']['uv_name']),
                           (_FIELDS, _canonical(fields)), (_CHANNEL, channel),
                           (_IMAGE, _canonical(image_desc))):
            group[key] = value
        specs = [('Original Color', 'INPUT', 'NodeSocketColor', (1., 1., 1., 1.)),
                 ('Strength', 'INPUT', 'NodeSocketFloat', 0.),
                 ('Lit Color', 'INPUT', 'NodeSocketColor', (1., 1., 1., 1.)),
                 ('Black Level', 'INPUT', 'NodeSocketFloat', 0.),
                 ('White Level', 'INPUT', 'NodeSocketFloat', 1.),
                 ('Color', 'OUTPUT', 'NodeSocketColor', None)]
        for name, direction, kind, default in specs:
            if animate_strength and name == 'Strength':
                continue
            socket = group.interface.new_socket(name=name, in_out=direction,
                                                socket_type=kind)
            if default is not None:
                socket.default_value = default
            if kind == 'NodeSocketFloat':
                socket.min_value = 0.
                socket.max_value = 1.
        entry = _node(group, 'input', 'NodeGroupInput')
        output = _node(group, 'output', 'NodeGroupOutput')
        uv = _node(group, 'uv', 'ShaderNodeUVMap', uv_map=group[_UV])
        texture = _node(group, 'image', 'ShaderNodeTexImage', image=image,
                        interpolation='Linear', extension='EXTEND', projection='FLAT')
        separate = _node(group, 'channel', 'ShaderNodeSeparateColor', mode='RGB')
        suppress = _node(group, 'suppression', 'ShaderNodeMapRange',
                         interpolation_type='LINEAR', clamp=True)
        suppress.inputs['To Min'].default_value = 1.
        suppress.inputs['To Max'].default_value = 0.
        mix = _node(group, 'mix', 'ShaderNodeAttribute',
                    attribute_type='GEOMETRY', attribute_name=fields['mix'])
        valid = _node(group, 'valid', 'ShaderNodeAttribute',
                      attribute_type='GEOMETRY', attribute_name=fields['valid'])
        region = _node(group, 'region', 'ShaderNodeMath', operation='MULTIPLY')
        gate = _node(group, 'gate', 'ShaderNodeMath', operation='MULTIPLY')
        strength = _node(group, 'strength', 'ShaderNodeMath',
                         operation='MULTIPLY', use_clamp=True)
        color = _node(group, 'color', 'ShaderNodeMixRGB',
                      blend_type='MIX', use_clamp=False)
        if animate_strength:
            scalar = _node(group, 'strength_value', 'ShaderNodeValue')
            scalar.outputs[0].default_value = 0.
            strength_source = scalar.outputs[0]
        else:
            strength_source = entry.outputs['Strength']
        pairs = [(uv.outputs['UV'], texture.inputs['Vector']),
                 (texture.outputs['Color'], separate.inputs['Color']),
                 (separate.outputs[_CHANNEL_NAMES[channel]], suppress.inputs['Value']),
                 (entry.outputs['Black Level'], suppress.inputs['From Min']),
                 (entry.outputs['White Level'], suppress.inputs['From Max']),
                 (mix.outputs['Fac'], region.inputs[0]),
                 (valid.outputs['Fac'], region.inputs[1]),
                 (region.outputs[0], gate.inputs[0]),
                 (suppress.outputs['Result'], gate.inputs[1]),
                 (gate.outputs[0], strength.inputs[0]),
                 (strength_source, strength.inputs[1]),
                 (strength.outputs[0], color.inputs[0]),
                 (entry.outputs['Original Color'], color.inputs[1]),
                 (entry.outputs['Lit Color'], color.inputs[2]),
                 (color.outputs['Color'], output.inputs['Color'])]
        for source, destination in pairs:
            group.links.new(source, destination)
        for index, node in enumerate(group.nodes):
            node.location = ((index % 6) * 230, -(index // 6) * 210)
        group[_GRAPH] = graph_signature(group)
        return group
    except Exception:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        raise


def _roles(group):
    expected = dict(_NODE_KINDS)
    if group.get(PROFILE_KEY) == PROFILE_V2:
        expected['strength_value'] = 'ShaderNodeValue'
    result = {}
    for node in group.nodes:
        role = node.get(_NODE)
        if role not in expected or role in result or node.bl_idname != expected[role]:
            raise CastArtError('Owned acceptance node inventory was edited')
        result[role] = node
    if set(result) != set(expected):
        raise CastArtError('Owned acceptance shader nodes are missing')
    return result


def _group_intrinsic(group):
    T = _sdf()
    if (not isinstance(group, bpy.types.ShaderNodeTree) or group.library
            or group.override_library or not group.is_editable
            or group.get(PROFILE_KEY) not in {PROFILE, PROFILE_V2} or not _owner(group.get(OWNER))
            or group.use_fake_user or group.use_extra_user
            or (group.get(PROFILE_KEY) == PROFILE and T._animated(group))):
        raise CastArtError('Acceptance group identity, editability or animation changed')
    if (not isinstance(group.get(_TARGET), bpy.types.Object)
            or type(group.get(_UID)) is not int or not _owner(group.get(_SDF_OWNER))
            or group.get(_CHANNEL) not in _CHANNEL_NAMES):
        raise CastArtError('Acceptance source identity is invalid')
    if sum(g.get(OWNER) == group[OWNER] for g in bpy.data.node_groups) != 1:
        raise CastArtError('Duplicate acceptance owner identity')
    if group.get(_GRAPH) != graph_signature(group):
        raise CastArtError('Acceptance nodes, links, interface or metadata were edited')
    nodes = _roles(group)
    fields = _json(group.get(_FIELDS), 'Acceptance fields')
    if (set(fields) != {'mix', 'valid'} or any(type(v) is not str or not v for v in fields.values())
            or len(set(fields.values())) != 2 or nodes['mix'].attribute_name != fields['mix']
            or nodes['valid'].attribute_name != fields['valid']
            or nodes['uv'].uv_map != group.get(_UV)):
        raise CastArtError('Acceptance UV/field source identity changed')
    image = nodes['image'].image
    content = _image_descriptor(image, group[_CHANNEL], group)
    stored = _json(group.get(_IMAGE), 'Acceptance image')
    T._image_shape(stored, group[_CHANNEL])
    if not T._same_image_identity(content, stored):
        raise CastArtError('Acceptance image bytes or pixels changed; explicitly rebind')
    return nodes, fields, content


def _animation_guard(tree, node):
    T = _sdf()
    if T._related_animation(tree, list(node.inputs), nodes=[node]):
        raise CastArtError('Acceptance instance has native animation/driver/NLA')
    # An external driver may reference an input without animating this tree.
    prefix = node.path_from_id()
    try:
        consumers = bpy.data.user_map(subset={tree}).get(tree, set())
    except (RuntimeError, TypeError, ReferenceError) as error:
        raise CastArtError('Cannot inspect acceptance animation consumers') from error
    for consumer in consumers:
        animation = getattr(consumer, 'animation_data', None)
        for curve in getattr(animation, 'drivers', ()) if animation is not None else ():
            for variable in curve.driver.variables:
                for target in variable.targets:
                    path = getattr(target, 'data_path', '')
                    if target.id == tree and (path == prefix or path.startswith(prefix + '.')
                                             or path.startswith(prefix + '[')):
                        raise CastArtError('Acceptance instance has an external driver consumer')


def _binding_payload(tree, node, sdf_node):
    """Only the owned instance and its exact boundary, not arbitrary upstream art."""
    T = _sdf()
    owner = node.get(OWNER)

    def identity(item):
        if item == node:
            return ['acceptance', owner]
        if item == sdf_node:
            return ['sdf', node.get(_SDF_OWNER), node.get(_SDF_TOKEN)]
        if item.get(_SOURCE) == owner:
            return ['original_source', owner]
        return ['node', item.name]

    return dict(
        instance=T._binding_node_state(node),
        metadata={k: T._value(v) for k, v in node.items() if k != _BINDING_SHA},
        links=sorted([(identity(l.from_node), l.from_socket.identifier,
                       identity(l.to_node), l.to_socket.identifier)
                      for l in tree.links if l.from_node == node or l.to_node == node], key=repr),
        sdf_original=sdf_node.get(T._ORIGINAL),
        sdf_snapshot=sdf_node.get(T._SNAPSHOT))


def _state(material, target=None, *, live=True):
    T = _sdf()
    tree = _local_material(material)
    if any(str(k).startswith(_PREFIX) for k in material.keys()) or any(
            str(k).startswith(_PREFIX) for k in tree.keys()):
        raise CastArtError('Unexpected reserved acceptance metadata on Material/tree')
    marked = [n for n in tree.nodes if any(str(k).startswith(_PREFIX) for k in n.keys())]
    instances = [n for n in marked if _MARK in n]
    if len(instances) != 1 or instances[0].bl_idname != 'ShaderNodeGroup':
        raise CastArtError('No unique acceptance instance; reserved markers are damaged')
    node = instances[0]
    if any(n != node and n.bl_idname == 'ShaderNodeGroup' and n.node_tree is not None
           and any(str(k).startswith(_PREFIX) for k in n.node_tree.keys())
           for n in tree.nodes):
        raise CastArtError('Unexpected additional acceptance group or damaged group markers')
    group = node.node_tree
    if (group is None or node.get(_MARK) not in {PROFILE, PROFILE_V2}
            or node.get(_MARK) != group.get(PROFILE_KEY)
            or node.get(OWNER) != group.get(OWNER)):
        raise CastArtError('Acceptance instance/group identity changed')
    nodes, fields, image_desc = _group_intrinsic(group)
    target, sdf_node, sdf_group, sdf_binding = _sdf_instance(material, target, live=live)
    info = T._intrinsic(sdf_group)
    if (group.get(_TARGET) != target or group.get(_UID) != info['uid']
            or group.get(_SDF_OWNER) != info['owner']
            or group.get(_UV) != info['uv']['uv_name']
            or fields != {k: info['fields'][k] for k in ('mix', 'valid')}
            or node.get(_SDF_OWNER) != info['owner']
            or node.get(_SDF_TOKEN) != sdf_binding['token']
            or sdf_binding['source'] != node
            or sdf_binding['source_socket'] != node.outputs['Color']):
        raise CastArtError('Acceptance was detached or rebound to a different SDF source')
    users = [(m.node_tree, n) for m in bpy.data.materials if m.node_tree
             for n in m.node_tree.nodes if n.bl_idname == 'ShaderNodeGroup' and n.node_tree == group]
    consumers = set(bpy.data.user_map(subset={group}).get(group, set()))
    if group.users != 1 or users != [(tree, node)] or consumers != {material}:
        raise CastArtError('Acceptance group has shared or unknown consumers')
    if sum(n.get(_MARK) in {PROFILE, PROFILE_V2} and n.get(OWNER) == group[OWNER]
           for m in bpy.data.materials if m.node_tree for n in m.node_tree.nodes) != 1:
        raise CastArtError('Duplicate acceptance material instance identity')
    _animation_guard(tree, node)
    values = _actual_settings(node)
    declared = _json(node.get(_DECLARED), 'Acceptance declared settings')
    if group.get(PROFILE_KEY) == PROFILE_V2:
        from . import cast_animation
        cast_animation.inspect(group)
        # v2 deliberately exposes its internal native scalar for author edits.
        # Other instance settings and every link remain signed and static.
        _settings(**declared)
        declared = dict(declared, strength=values['strength'])
    if values != declared:
        raise CastArtError('Acceptance settings were changed outside the author update')
    old_original = _json(node.get(_OLD_ORIGINAL), 'Original SDF source')
    if (set(old_original) != {'linked', 'source_socket', 'color'}
            or type(old_original['linked']) is not bool
            or not isinstance(node.get(_OLD_SNAPSHOT), str)):
        raise CastArtError('Acceptance restoration record is invalid')
    sources = [n for n in tree.nodes if n.get(_SOURCE) == group[OWNER]]
    if len(sources) != int(old_original['linked']):
        raise CastArtError('Original source identity is missing or duplicated')
    source = sources[0] if sources else None
    if set(marked) != {node} | ({source} if source is not None else set()):
        raise CastArtError('Unexpected residual acceptance node markers')
    if source is not None and any(str(k).startswith(_PREFIX) and k != _SOURCE
                                  for k in source.keys()):
        raise CastArtError('Unexpected acceptance metadata on the original source')
    source_socket = T._socket(source.outputs, old_original['source_socket']) if source else None
    original_input = node.inputs['Original Color']
    if source_socket is None:
        if original_input.links:
            raise CastArtError('The original constant source was replaced by a link')
    elif (len(original_input.links) != 1
          or original_input.links[0].from_socket != source_socket):
        raise CastArtError('The original upstream link was changed')
    expected_links = int(source_socket is not None) + 1
    connected = [l for l in tree.links if l.from_node == node or l.to_node == node]
    if (len(connected) != expected_links
            or len(node.outputs['Color'].links) != 1
            or node.outputs['Color'].links[0].to_socket != sdf_node.inputs['Original Color']):
        raise CastArtError('Acceptance input/output has an unexpected consumer')
    binding_sha = _digest(_binding_payload(tree, node, sdf_node))
    if node.get(_BINDING_SHA) != binding_sha:
        raise CastArtError('Acceptance boundary, restoration data or instance was edited')
    return dict(material=material, tree=tree, node=node, group=group,
                target=target, sdf_node=sdf_node, sdf_group=sdf_group,
                sdf_binding=sdf_binding, fields=fields, nodes=nodes,
                image=image_desc, settings=values, source=source,
                source_socket=source_socket, old_original=old_original)


def _descriptor(state, *, tree=None, node=None, group=None, sdf_node=None, image=None):
    tree = state['tree'] if tree is None else tree
    node = state['node'] if node is None else node
    group = state['group'] if group is None else group
    sdf_node = state['sdf_node'] if sdf_node is None else sdf_node
    settings = _actual_settings(node)
    profile = group.get(PROFILE_KEY)
    result = dict(schema=SCHEMA_V2 if profile == PROFILE_V2 else SCHEMA,
                  profile=profile, owner=group[OWNER],
                  target=state['target'].name, modifier_uid=group[_UID],
                  sdf_owner=group[_SDF_OWNER], material=state['material'].name,
                  node=node.name, output_socket=node.outputs['Color'].identifier,
                  uv_name=group[_UV], fields=_json(group[_FIELDS], 'Acceptance fields'),
                  channel=group[_CHANNEL], image=state['image'] if image is None else image,
                  settings=settings, graph_sha256=graph_signature(group),
                  binding_sha256=_digest(_binding_payload(tree, node, sdf_node)),
                  interpolation='Linear', extension='EXTEND', mirror=False,
                  formula=FORMULA)
    if profile == PROFILE_V2:
        from . import cast_animation
        result['animation'] = cast_animation.inspect(state['group'])
    result['descriptor_sha256'] = _digest(result)
    return result


def validate_material(material, *, target=None):
    """Complete live author validation; returns None when not present."""
    if not is_present(material):
        return None
    return _descriptor(_state(material, target, live=True))


def reader_edited(group):
    """Conservative reader-inventory guard, matching legacy static guards."""
    try:
        bindings = [(material, node) for material in bpy.data.materials if material.node_tree
                    for node in material.node_tree.nodes
                    if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == group]
        if len(bindings) != 1:
            return True
        state = _state(bindings[0][0], live=False)
        return state['group'] != group or state['node'] != bindings[0][1]
    except (ValueError, TypeError, RuntimeError, ReferenceError, KeyError, AttributeError):
        return True


def _evaluated_snapshot(state, depsgraph):
    if depsgraph.mode != 'RENDER':
        raise CastArtError('Acceptance snapshot requires an actual RENDER dependency graph')
    material = depsgraph.id_eval_get(state['material'])
    if not material.is_evaluated or material.node_tree is None:
        raise CastArtError('Acceptance snapshot did not resolve an evaluated Material')
    tree = material.node_tree
    own = [n for n in tree.nodes if n.get(_MARK) == state['group'].get(PROFILE_KEY)
           and n.get(OWNER) == state['group'][OWNER]]
    sdf = [n for n in tree.nodes if n.get(_sdf()._MARK) == _sdf().BINDING
           and n.get(_sdf().OWNER) == state['group'][_SDF_OWNER]]
    if len(own) != 1 or len(sdf) != 1 or own[0].node_tree is None:
        raise CastArtError('Evaluated acceptance/SDF binding inventory differs')
    node, sdf_node = own[0], sdf[0]
    # The evaluated material instance may retain its original group pointer.
    # Read the group explicitly from this same actual RENDER graph.
    group = node.node_tree.evaluated_get(depsgraph)
    roles = _roles(group)
    image = _sdf()._image_content_descriptor(roles['image'].image, group[_CHANNEL])
    current = _descriptor(state, tree=tree, node=node, group=group,
                          sdf_node=sdf_node, image=image)
    if group.get(PROFILE_KEY) == PROFILE_V2:
        current['settings']['strength'] = float(_strength_socket(group).default_value)
        _settings(**current['settings'])
        current.pop('descriptor_sha256')
        current['descriptor_sha256'] = _digest(current)
    return current


def snapshot(material, depsgraph=None):
    """Live author snapshot, or its matching evaluated shader data."""
    state = _state(material, live=True)
    before = _descriptor(state)
    if depsgraph is None:
        return before
    current = _evaluated_snapshot(state, depsgraph)
    if current != before:
        raise CastArtError('Actual RENDER acceptance shader/image differs from live author data')
    return current


def snapshot_frozen(material, depsgraph, expected_descriptor, *, expected_render_time=None):
    """Restricted post-freeze read: requires the previously verified descriptor.

    The joint worker has already replaced evaluated Meshes and removed source
    modifiers. Do not rerun original-Mesh UV/extent or exclusive-Mesh tests here.
    Intrinsic owned graphs, SDF/acceptance boundaries, current image content and
    the actual evaluated Material must still match the complete before record.
    """
    if type(expected_descriptor) is not dict:
        raise CastArtError('Frozen acceptance snapshot requires its verified before descriptor')
    if expected_descriptor.get('profile') == PROFILE_V2:
        from .cast_art_witness import _render_time
        _render_time(expected_render_time)
        if depsgraph.mode != 'RENDER':
            raise CastArtError('Frozen acceptance requires an actual RENDER graph')
        scene = depsgraph.scene_eval
        observed = dict(scene=scene.name, frame=scene.frame_current,
                        subframe=scene.frame_subframe)
        if observed != expected_render_time:
            raise CastArtError('Frozen acceptance RENDER time differs from its before observation')
    state = _state(material, live=False)
    current = _evaluated_snapshot(state, depsgraph)
    if current != expected_descriptor:
        raise CastArtError('Acceptance shader/image changed after geometry freezing')
    return current


def inspect(context):
    target, material = _context(context)
    value = validate_material(material, target=target)
    return dict(status='valid' if value is not None else 'absent',
                profile=value['profile'] if value is not None else PROFILE, descriptor=value)


def insert_strength_key(context, *, frame, value):
    target, material = _context(context)
    state = _state(material, target, live=True)
    if state['group'].get(PROFILE_KEY) != PROFILE_V2:
        raise CastArtError('Legacy static layer: explicitly remove and rebind in strength animation mode')
    from . import cast_animation
    result = cast_animation.insert_key(state['group'], frame, value)
    return dict(status='keyed', animation=result,
                descriptor=validate_material(material, target=target))


def clear_strength_animation(context):
    target, material = _context(context)
    state = _state(material, target, live=True)
    if state['group'].get(PROFILE_KEY) != PROFILE_V2:
        raise CastArtError('Legacy static layer has no owned strength animation')
    from . import cast_animation
    result = cast_animation.clear(state['group'])
    return dict(status='detached', animation=result,
                descriptor=validate_material(material, target=target))


def _restore_sdf(tree, sdf_node, token, source, source_socket, original, snapshot):
    T = _sdf()
    color = sdf_node.inputs['Original Color']
    for link in list(color.links):
        tree.links.remove(link)
    if source_socket is not None:
        tree.links.new(source_socket, color)
        source[T._SOURCE] = token
    sdf_node[T._ORIGINAL] = original
    sdf_node[T._SNAPSHOT] = snapshot


def bind(context, image, *, strength=0., lit_color=(1., 1., 1., 1.),
         black_level=0., white_level=1., channel='R', animate_strength=False):
    T = _sdf()
    target, material = _context(context)
    if is_present(material):
        raise CastArtError('Acceptance or damaged reserved markers already exist')
    if type(channel) is not str or channel not in _CHANNEL_NAMES:
        raise CastArtError('Acceptance channel must be R, G or B')
    if type(animate_strength) is not bool:
        raise CastArtError('Animation mode must be an explicit bool')
    values = _settings(strength, lit_color, black_level, white_level)
    image_desc = _image_descriptor(image, channel)
    target, sdf_node, sdf_group, binding = _sdf_instance(material, target, live=True)
    tree = material.node_tree
    source, source_socket, token = binding['source'], binding['source_socket'], binding['token']
    old_original = sdf_node[T._ORIGINAL]
    old_snapshot = sdf_node[T._SNAPSHOT]
    old_input = tuple(float(v) for v in sdf_node.inputs['Original Color'].default_value)
    if source is not None and any(str(k).startswith(_PREFIX) for k in source.keys()):
        raise CastArtError('Original source has conflicting acceptance markers')
    group = node = None
    try:
        group = _build_group(target, sdf_group, image, image_desc, channel,
                             animate_strength=animate_strength)
        node = tree.nodes.new('ShaderNodeGroup')
        node.node_tree = group
        node.label = 'Face cast acceptance (artistic; external shadows affected)'
        node.location = (sdf_node.location.x - 260, sdf_node.location.y - 220)
        for key, value in ((_MARK, group[PROFILE_KEY]), (OWNER, group[OWNER]),
                           (_SDF_OWNER, sdf_group[T.OWNER]), (_SDF_TOKEN, token),
                           (_OLD_ORIGINAL, old_original), (_OLD_SNAPSHOT, old_snapshot)):
            node[key] = value
        node.inputs['Original Color'].default_value = old_input
        _write_settings(node, values)
        if source is not None:
            del source[T._SOURCE]
            source[_SOURCE] = group[OWNER]
            tree.links.new(source_socket, node.inputs['Original Color'])
        node[T._SOURCE] = token
        tree.links.new(node.outputs['Color'], sdf_node.inputs['Original Color'])
        replacement = dict(binding['original'])
        replacement['linked'] = True
        replacement['source_socket'] = node.outputs['Color'].identifier
        sdf_node[T._ORIGINAL] = T._canonical(replacement)
        sdf_node[T._SNAPSHOT] = T._binding_state(tree, token)
        if sdf_node[T._SNAPSHOT] is None:
            raise CastArtError('Could not witness the explicit SDF source transition')
        node[_BINDING_SHA] = _digest(_binding_payload(tree, node, sdf_node))
        descriptor = validate_material(material, target=target)
        return dict(status='bound', profile=group[PROFILE_KEY], descriptor=descriptor)
    except Exception as error:
        failures = []
        try:
            if node is not None:
                tree.nodes.remove(node)
            if source is not None and source.get(_SOURCE) == (group.get(OWNER) if group else None):
                del source[_SOURCE]
            _restore_sdf(tree, sdf_node, token, source, source_socket,
                         old_original, old_snapshot)
            if T._binding_state(tree, token) != old_snapshot:
                raise CastArtError('SDF snapshot did not restore exactly')
            T.validate_reader(sdf_group, target=target, require_live=True)
        except Exception as restore_error:
            failures.append(str(restore_error))
        try:
            if group is not None and group.users == 0:
                bpy.data.node_groups.remove(group)
        except Exception as cleanup_error:
            failures.append(str(cleanup_error))
        if failures:
            raise RuntimeError('Acceptance bind failed and rollback was incomplete: '
                               + '; '.join(failures)) from error
        raise


def update(context, *, strength, lit_color, black_level, white_level):
    target, material = _context(context)
    state = _state(material, target, live=True)
    values = _settings(strength, lit_color, black_level, white_level)
    node = state['node']
    old_values = state['settings']
    if state['group'].get(PROFILE_KEY) == PROFILE_V2:
        from . import cast_animation
        if (cast_animation.inspect(state['group'])['animated_keys']
                and values['strength'] != old_values['strength']):
            raise CastArtError('Animated strength must be changed with an owned keyframe')
    old_declared, old_sha = node[_DECLARED], node[_BINDING_SHA]
    try:
        _write_settings(node, values)
        node[_BINDING_SHA] = _digest(_binding_payload(state['tree'], node, state['sdf_node']))
        return dict(status='updated', profile=state['group'][PROFILE_KEY],
                    descriptor=validate_material(material, target=target))
    except Exception as error:
        try:
            _write_settings(node, old_values)
            node[_DECLARED], node[_BINDING_SHA] = old_declared, old_sha
            validate_material(material, target=target)
        except Exception as restore_error:
            raise RuntimeError('Acceptance update failed and rollback was incomplete: '
                               + str(restore_error)) from error
        raise


def unbind(context):
    T = _sdf()
    target, material = _context(context)
    state = _state(material, target, live=True)
    tree, node, group = state['tree'], state['node'], state['group']
    if group.get(PROFILE_KEY) == PROFILE_V2:
        from . import cast_animation
        if cast_animation.inspect(group)['action'] is not None:
            raise CastArtError('Detach and retain the owned strength Action before removing this layer')
    sdf_node = state['sdf_node']
    token = state['sdf_binding']['token']
    old_original, old_snapshot = node[_OLD_ORIGINAL], node[_OLD_SNAPSHOT]
    current_original = sdf_node[T._ORIGINAL]
    current_snapshot = sdf_node[T._SNAPSHOT]
    source, source_socket = state['source'], state['source_socket']
    try:
        del node[T._SOURCE]
        _restore_sdf(tree, sdf_node, token, source, source_socket,
                     old_original, old_snapshot)
        if T._binding_state(tree, token) != old_snapshot:
            raise CastArtError('Original SDF token/source/defaults did not restore exactly')
        T.validate_reader(state['sdf_group'], target=target, require_live=True)
    except Exception as error:
        try:
            if source is not None and source.get(T._SOURCE) == token:
                del source[T._SOURCE]
            node[T._SOURCE] = token
            tree.links.new(node.outputs['Color'], sdf_node.inputs['Original Color'])
            sdf_node[T._ORIGINAL], sdf_node[T._SNAPSHOT] = current_original, current_snapshot
            validate_material(material, target=target)
        except Exception as restore_error:
            raise RuntimeError('Acceptance remove failed and rollback was incomplete: '
                               + str(restore_error)) from error
        raise
    # All protected original state has passed before deleting only our own IDs.
    if source is not None:
        del source[_SOURCE]
    name, owner, profile = group.name, group[OWNER], group[PROFILE_KEY]
    tree.nodes.remove(node)
    if group.users != 0:
        raise RuntimeError('Acceptance detached, but group gained a consumer during removal')
    bpy.data.node_groups.remove(group)
    if T._binding_state(tree, token) != old_snapshot:
        raise RuntimeError('SDF binding changed during owned acceptance cleanup')
    return dict(status='unbound', profile=profile, owner=owner, removed_group=name,
                sdf_owner=state['sdf_group'][T.OWNER], sdf_token=token,
                restored_snapshot_sha256=hashlib.sha256(old_snapshot.encode('utf8')).hexdigest())
