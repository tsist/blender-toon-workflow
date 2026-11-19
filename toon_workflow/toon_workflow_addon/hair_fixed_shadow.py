# SPDX-License-Identifier: GPL-3.0-or-later
"""Static Blue-channel hair shadows with explicit legacy/version dispatch.

The caller owns Object/material exclusivity and UV validation.  This module
never changes Image pixels, a ramp, a normal reader, or an existing finish layer.
New v2 layers multiply the dynamic ramp COLOR. Legacy v1 Light gating remains
readable, editable and removable without automatic migration or re-signing.
An explicit HM v3 handshake may replace only the external v2 source edge.
"""
import hashlib
import json
import math
from pathlib import Path
import uuid

LEGACY_PROFILE = 'toon-hair-fixed-shadow.v1'
PROFILE = 'toon-hair-fixed-shadow.v2'
HM_PROFILE = 'toon-hair-fixed-shadow.hm.v3'
PROFILE_KEY = 'toon_hair_fixed_shadow_profile'
_PREFIX = 'toon_hair_fixed_shadow_'
_OWNER = _PREFIX + 'owner'
_BINDING = _PREFIX + 'binding'
_DECLARED = _PREFIX + 'declared'
_SIGNATURE = _PREFIX + 'signature'
_IMAGE = _PREFIX + 'image'
_NODE = _PREFIX + 'node'
_LEGACY_TYPES = dict(input='NodeGroupInput', output='NodeGroupOutput',
              uv='ShaderNodeUVMap', texture='ShaderNodeTexImage',
              blue='ShaderNodeSeparateColor', strength='ShaderNodeValue',
              weighted='ShaderNodeMath', factor='ShaderNodeMath', shade='ShaderNodeMath')
_TYPES = dict(input='NodeGroupInput', output='NodeGroupOutput',
              uv='ShaderNodeUVMap', texture='ShaderNodeTexImage',
              blue='ShaderNodeSeparateColor', strength='ShaderNodeValue',
              weighted='ShaderNodeMath', tint='ShaderNodeRGB', multiply='ShaderNodeMixRGB')


class HairFixedShadowError(ValueError):
    """Invalid source, altered owned graph, or changed material binding."""


def _need(condition, message):
    if not condition:
        raise HairFixedShadowError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _strength(value):
    _need(type(value) in (int, float) and math.isfinite(value) and 0. <= value <= 1.,
          'Fixed hair shadow strength requires a finite number in [0,1]')
    return float(value)


def _tint(value):
    _need(type(value) in (list, tuple) and len(value) == 3,
          'Fixed hair shadow tint requires RGB')
    result = []
    for channel in value:
        _need(type(channel) in (int, float) and math.isfinite(channel) and 0. <= channel <= 1.,
              'Fixed hair shadow tint requires finite RGB values in [0,1]')
        result.append(float(channel))
    return result


def _profile(block):
    profile = block.get(PROFILE_KEY)
    _need(profile in (LEGACY_PROFILE, PROFILE, HM_PROFILE), 'Unknown or missing fixed hair shadow profile')
    return profile


def _static(block):
    animation = block.animation_data
    _need(animation is None or (animation.action is None and not animation.drivers
                               and not animation.nla_tracks),
          'Fixed hair shadow requires static material and node data')


def _material(material, *, observation=False):
    import bpy
    _need(isinstance(material, bpy.types.Material) and material.use_nodes
          and material.node_tree is not None and not material.library
          and not material.override_library and material.is_editable,
          'Expected a local editable node Material')
    if not observation:
        _need(not material.use_extra_user
              and material.users - int(material.use_fake_user) <= 1,
              'Fixed hair shadow requires an exclusive Material')
    _static(material)
    _static(material.node_tree)
    return material.node_tree


def _image_descriptor(image):
    """Small FILE/packed snapshot; sRGB is recorded, never converted."""
    import bpy
    import numpy as np
    _need(isinstance(image, bpy.types.Image) and image.source == 'FILE'
          and not image.library and not image.override_library,
          'Fixed hair shadow requires an existing local FILE Image')
    _need(len(image.tiles) == 1 and len(image.packed_files) <= 1 and not image.is_dirty,
          'Fixed hair shadow requires one valid unchanged image tile')
    packed = image.packed_file
    if packed is not None:
        data = bytes(packed.data)
        _need(bool(data), 'Packed hair mask is empty')
        source_sha = hashlib.sha256(data).hexdigest()
        source_kind, filepath = 'PACKED', None
    else:
        candidate = bpy.path.abspath(image.filepath) if image.filepath else ''
        _need(candidate and not candidate.startswith(('\\\\', '//')),
              'Hair mask must have a local FILE path')
        source = Path(candidate)
        _need(source.is_file(), 'Hair mask FILE is missing')
        with source.open('rb') as stream:
            source_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        source_kind, filepath = 'FILE', str(source.resolve())
    width, height = map(int, image.size)
    count = width * height * 4
    _need(width > 0 and height > 0 and len(image.pixels) == count,
          'Hair mask actual RGBA extent is invalid')
    values = np.empty(count, dtype='<f4')
    image.pixels.foreach_get(values)
    _need(bool(np.isfinite(values).all()), 'Hair mask contains nonfinite pixels')
    blue = values[2::4].copy()
    _need(bool(((blue >= 0.) & (blue <= 1.)).all()), 'Hair mask Blue must be in [0,1]')
    return dict(name=image.name, source_kind=source_kind, filepath=filepath,
                source_sha256=source_sha, width=width, height=height,
                channels=int(image.channels), depth=int(image.depth), is_float=bool(image.is_float),
                colorspace=image.colorspace_settings.name, alpha_mode=image.alpha_mode,
                rgba_sha256=hashlib.sha256(values.tobytes()).hexdigest(),
                blue_sha256=hashlib.sha256(blue.tobytes()).hexdigest(),
                blue_min=float(blue.min()), blue_max=float(blue.max()), channel='B')


def _plain(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        _need(math.isfinite(value), 'Nonfinite owned hair shadow socket')
        return float(value)
    return [_plain(item) for item in value]


def _nodes(group):
    inventory = _LEGACY_TYPES if _profile(group) == LEGACY_PROFILE else _TYPES
    nodes = {}
    for node in group.nodes:
        key = node.get(_NODE)
        _need(key in inventory and key not in nodes and node.bl_idname == inventory[key]
              and node.get(_OWNER) == group.get(_OWNER) and not node.mute,
              'Owned fixed hair shadow node inventory changed')
        nodes[key] = node
    _need(set(nodes) == set(inventory), 'Owned fixed hair shadow nodes are missing')
    return nodes


def _shape(group):
    nodes = _nodes(group)
    legacy = _profile(group) == LEGACY_PROFILE
    socket_name, socket_type = ('Light', 'NodeSocketFloat') if legacy else ('Color', 'NodeSocketColor')
    interface = []
    for item in group.interface.items_tree:
        _need(item.item_type == 'SOCKET', 'Fixed hair shadow interface changed')
        interface.append(dict(name=item.name, identifier=item.identifier,
            in_out=item.in_out, socket_type=item.socket_type,
            default=_plain(item.default_value) if hasattr(item, 'default_value') else None))
    _need(len(interface) == 2 and {(x['name'], x['in_out'], x['socket_type']) for x in interface}
          == {(socket_name, 'INPUT', socket_type), (socket_name, 'OUTPUT', socket_type)},
          'Fixed hair shadow versioned input/output interface changed')
    rows = []
    for key, node in sorted(nodes.items()):
        row = dict(id=key, type=node.bl_idname, name=node.name,
            inputs=[(socket.identifier, _plain(socket.default_value))
                    for socket in node.inputs if hasattr(socket, 'default_value')],
            outputs=[(socket.identifier, _plain(socket.default_value))
                     for socket in node.outputs if hasattr(socket, 'default_value')])
        row['properties'] = {name: getattr(node, name) for name in
            ('operation', 'use_clamp', 'mode', 'uv_map', 'from_instancer',
             'interpolation', 'extension', 'projection', 'projection_blend', 'is_active_output')
            if hasattr(node, name)}
        # Keep the exact legacy v1 shape/signature unchanged. Only v2 adds a
        # MixRGB node whose blend/alpha options are shader-affecting state.
        if not legacy and node.bl_idname == 'ShaderNodeMixRGB':
            row['properties'].update(blend_type=node.blend_type, use_alpha=node.use_alpha)
        rows.append(row)
    links = sorted((link.from_node.get(_NODE), link.from_socket.identifier,
                    link.to_node.get(_NODE), link.to_socket.identifier) for link in group.links)
    return dict(nodes=rows, interface=interface, links=links)


def _signature(group):
    return hashlib.sha256(_json(_shape(group)).encode('utf8')).hexdigest()


def is_present(material):
    return material is not None and any(str(key).startswith(_PREFIX) for key in material.keys())


def _hm_source(material, instance, *, owner=None):
    """Leaf identity check only; never recurse through the HM public validator."""
    import bpy
    tree = material.node_tree
    _need(instance in tuple(tree.nodes) and instance.bl_idname == 'ShaderNodeGroup'
          and not instance.mute, 'HM source is not an active instance in this material')
    group = instance.node_tree
    _need(group is not None and group.get('toon_hm_node_profile') == 'toon-hair-map-node.v1'
          and material.get('toon_hm_binding') == group
          and not group.library and not group.override_library and group.is_editable
          and not group.use_fake_user and not group.use_extra_user,
          'HM source Group reference, profile or locality changed')
    actual_owner = group.get('toon_hm_owner')
    _need(type(actual_owner) is str and len(actual_owner) == 32
          and all(c in '0123456789abcdef' for c in actual_owner)
          and (owner is None or actual_owner == owner)
          and instance.get('toon_hm_owner') == actual_owner,
          'HM source owner identity changed')
    instances = [node for node in tree.nodes if node.get('toon_hm_owner') == actual_owner]
    incoming = bpy.data.user_map(subset={group}).get(group, set())
    _need(instances == [instance] and group.users == 2 and incoming
          and incoming.issubset({material, tree}), 'HM source Group gained a foreign consumer')
    try:
        interface = json.loads(group['toon_hm_node_interface'])
    except (KeyError, TypeError, ValueError) as error:
        raise HairFixedShadowError('HM source interface identity cannot be read') from error
    _need(type(interface) is dict and set(interface) == {'original', 'normal', 'color'}
          and all(type(value) is str and value for value in interface.values())
          and len(set(interface.values())) == 3,
          'HM source interface identity changed')
    original = [socket for socket in instance.inputs if socket.identifier == interface['original']]
    normal = [socket for socket in instance.inputs if socket.identifier == interface['normal']]
    color = [socket for socket in instance.outputs if socket.identifier == interface['color']]
    _need(len(instance.inputs) == 2 and len(instance.outputs) == 1
          and len(original) == len(normal) == len(color) == 1
          and original[0].name == 'Original Color' and original[0].type == 'RGBA'
          and normal[0].name == 'Normal' and normal[0].type == 'VECTOR'
          and color[0].name == 'Color' and color[0].type == 'RGBA',
          'HM source Color/Normal interface changed')
    _static(group)
    return actual_owner, original[0], color[0]


def _binding(material, *, observation=False):
    tree = _material(material,observation=observation)
    profile = _profile(material)
    legacy = profile == LEGACY_PROFILE
    owner = material.get(_OWNER)
    try:
        _need(type(owner) is str and uuid.UUID(owner).int != 0, 'Invalid fixed hair shadow owner')
    except (ValueError, TypeError, AttributeError) as error:
        raise HairFixedShadowError('Invalid fixed hair shadow owner') from error
    try:
        binding = json.loads(material[_BINDING])
    except (KeyError, TypeError, ValueError) as error:
        raise HairFixedShadowError('Fixed hair shadow binding cannot be read') from error
    fields = {'source', 'destination'}
    if profile == HM_PROFILE:
        fields |= {'hm_instance', 'hm_owner'}
    _need(type(binding) is dict and set(binding) == fields,
          'Fixed hair shadow binding fields changed')
    if profile == HM_PROFILE:
        _need(type(binding['hm_instance']) is str and bool(binding['hm_instance'])
              and type(binding['hm_owner']) is str,
              'Fixed hair shadow HM binding identity is invalid')
    source, destination = tree.nodes.get(binding['source']), tree.nodes.get(binding['destination'])
    source_type, destination_type = (('ShaderNodeShaderToRGB', 'ShaderNodeValToRGB') if legacy
                                     else ('ShaderNodeValToRGB', 'ShaderNodeEmission'))
    _need(source is not None and source.bl_idname == source_type
          and destination is not None and destination.bl_idname == destination_type,
          'Original fixed hair shadow source or ramp changed')
    instances = [node for node in tree.nodes if node.get(_OWNER) == owner]
    _need(len(instances) == 1 and instances[0].bl_idname == 'ShaderNodeGroup'
          and not instances[0].mute, 'Fixed hair shadow material instance changed')
    instance = instances[0]
    group = instance.node_tree
    _need(group is not None and group.get(PROFILE_KEY) == profile and group.get(_OWNER) == owner
          and not group.library and not group.override_library and group.is_editable
          and not group.use_fake_user and not group.use_extra_user and group.users == 1,
          'Fixed hair shadow Group identity, locality or exclusivity changed')
    _static(group)
    source_socket = source.outputs['Color']
    if profile == HM_PROFILE:
        hm_instance = tree.nodes.get(binding['hm_instance'])
        _need(hm_instance is not None, 'Fixed hair shadow HM source is missing')
        _, hm_input, hm_output = _hm_source(material, hm_instance, owner=binding['hm_owner'])
        _need(len(hm_input.links) == 1 and hm_input.links[0].from_socket == source_socket
              and len(hm_output.links) == 1 and hm_output.links[0].to_socket == instance.inputs[0],
              'Fixed hair shadow HM source no longer exclusively occupies its declared seam')
        source_socket = hm_output
    destination_socket = destination.inputs['Fac' if legacy else 'Color']
    socket_name = 'Light' if legacy else 'Color'
    _need(len(instance.inputs) == len(instance.outputs) == 1
          and instance.inputs[0].name == instance.outputs[0].name == socket_name
          and len(instance.inputs[0].links) == len(instance.outputs[0].links) == 1
          and instance.inputs[0].links[0].from_socket == source_socket
          and len(destination_socket.links) == 1
          and destination_socket.links[0].from_socket == instance.outputs[0],
          'Fixed hair shadow no longer exclusively occupies its original seam')
    return tree, instance, group, source_socket, destination_socket


def validate(material, *, observation=False):
    """Validate only this owned layer and seam, preserving other author layers."""
    _, _, group, _, _ = _binding(material,observation=observation)
    profile = _profile(group)
    legacy = profile == LEGACY_PROFILE
    _need(group.get(_SIGNATURE) == _signature(group), 'Owned fixed hair shadow graph was edited')
    try:
        declared = json.loads(group[_DECLARED])
    except (KeyError, TypeError, ValueError) as error:
        raise HairFixedShadowError('Fixed hair shadow declaration cannot be read') from error
    expected = {'uv_name', 'strength', 'image'} if legacy else {'uv_name', 'strength', 'tint', 'image'}
    _need(type(declared) is dict and set(declared) == expected,
          'Fixed hair shadow declaration fields changed')
    strength = _strength(declared['strength'])
    uv_name = declared['uv_name']
    _need(type(uv_name) is str and uv_name.strip() and '\x00' not in uv_name,
          'Fixed hair shadow UV name is invalid')
    nodes = _nodes(group)
    tint = None if legacy else _tint(declared['tint'])
    if not legacy:
        _need(list(nodes['tint'].outputs[0].default_value) == tint + [1.]
              and nodes['multiply'].blend_type == 'MULTIPLY'
              and not nodes['multiply'].use_alpha and not nodes['multiply'].use_clamp,
              'Fixed hair shadow multiply tint or blend settings changed')
    image = group.get(_IMAGE)
    _need(nodes['texture'].image == image and nodes['uv'].uv_map == uv_name
          and not nodes['uv'].from_instancer
          and nodes['texture'].interpolation == 'Linear' and nodes['texture'].extension == 'REPEAT'
          and nodes['texture'].projection == 'FLAT' and nodes['texture'].projection_blend == 0.
          and nodes['blue'].mode == 'RGB'
          and nodes['strength'].outputs[0].default_value == strength,
          'Fixed hair shadow sampling, image reference or strength changed')
    image_value = _image_descriptor(image)
    _need(image_value == declared['image'], 'Fixed hair shadow image source or pixels changed')
    result = dict(profile=profile, owner=group[_OWNER], material=material.name, group=group.name,
                strength=strength, uv_name=uv_name, image=image_value,
                signature=group[_SIGNATURE], channel='B', sampling='Linear/REPEAT/FLAT',
                formula=('Light_out = Light_in * (1 - strength * Blue)' if legacy else
                         'Color_out = Color_dynamic * (1 - strength * Blue * (1 - tint))'),
                limitations=[('static ShaderToRGB Color -> scalar ramp Fac seam only' if legacy else
                              'static ramp Color -> Emission Color multiply seam only'),
                             'caller validates Object consumers and explicit UV existence'])
    if not legacy:
        result['tint'] = tint
    if profile == HM_PROFILE:
        binding = json.loads(material[_BINDING])
        result.update(source_mode='hm-wrapper', hm_instance=binding['hm_instance'],
                      hm_owner=binding['hm_owner'])
    return result


def attach(material, image, uv_name='UV贴图', strength=0., tint=(.65, .55, .65)):
    """Insert a v2 COLOR multiply layer, disabled unless explicitly enabled."""
    import bpy
    tree = _material(material)
    _need(not is_present(material), 'Fixed hair shadow is already present or damaged')
    _need(type(uv_name) is str and bool(uv_name.strip()) and '\x00' not in uv_name,
          'An explicit nonempty hair UV name is required')
    strength = _strength(strength)
    tint = _tint(tint)
    image_value = _image_descriptor(image)
    seams = [link for link in tree.links if link.from_node.bl_idname == 'ShaderNodeValToRGB'
             and link.from_socket == link.from_node.outputs['Color']
             and link.to_node.bl_idname == 'ShaderNodeEmission'
             and link.to_socket == link.to_node.inputs['Color']]
    _need(len(seams) == 1 and len(seams[0].to_socket.links) == 1,
          'Expected exactly one direct ramp Color -> Emission Color seam')
    seam = seams[0]
    source, destination = seam.from_socket, seam.to_socket
    binding = dict(source=seam.from_node.name, destination=seam.to_node.name)
    owner = str(uuid.uuid4())
    group = bpy.data.node_groups.new('Toon Hair Fixed Shadow ' + owner[:8], 'ShaderNodeTree')
    instance = None
    try:
        group[PROFILE_KEY], group[_OWNER], group[_IMAGE] = PROFILE, owner, image
        group.interface.new_socket(name='Color', in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')
        nodes = {}
        for key, node_type in _TYPES.items():
            node = group.nodes.new(node_type)
            node.name, node.label = 'Hair Fixed Shadow/' + key, key
            node[_NODE], node[_OWNER] = key, owner
            nodes[key] = node
        nodes['uv'].uv_map = uv_name
        texture = nodes['texture']
        texture.image, texture.interpolation, texture.extension = image, 'Linear', 'REPEAT'
        texture.projection, texture.projection_blend = 'FLAT', 0.
        nodes['blue'].mode = 'RGB'
        nodes['strength'].outputs[0].default_value = strength
        nodes['weighted'].operation, nodes['weighted'].use_clamp = 'MULTIPLY', False
        nodes['tint'].outputs[0].default_value = (*tint, 1.)
        nodes['multiply'].blend_type = 'MULTIPLY'
        nodes['multiply'].use_clamp, nodes['multiply'].use_alpha = False, False
        for a, a_index, b, b_index in (
                ('uv', 0, 'texture', 0), ('texture', 0, 'blue', 0),
                ('blue', 2, 'weighted', 0), ('strength', 0, 'weighted', 1),
                ('weighted', 0, 'multiply', 0), ('input', 0, 'multiply', 1),
                ('tint', 0, 'multiply', 2), ('multiply', 0, 'output', 0)):
            group.links.new(nodes[a].outputs[a_index], nodes[b].inputs[b_index])
        actual_strength = float(nodes['strength'].outputs[0].default_value)
        actual_tint = list(nodes['tint'].outputs[0].default_value[:3])
        group[_DECLARED] = _json(dict(uv_name=uv_name, strength=actual_strength,
                                     tint=actual_tint, image=image_value))
        group[_SIGNATURE] = _signature(group)
        instance = tree.nodes.new('ShaderNodeGroup')
        instance.node_tree = group
        instance.name, instance.label = 'Toon Hair Fixed Shadow', '头发固有阴影（正片叠底）'
        instance[_OWNER] = owner
        instance.location = ((seam.from_node.location.x + seam.to_node.location.x) / 2.,
                             seam.to_node.location.y - 180.)
        tree.links.remove(seam)
        tree.links.new(source, instance.inputs[0])
        tree.links.new(instance.outputs[0], destination)
        material[PROFILE_KEY], material[_OWNER], material[_BINDING] = PROFILE, owner, _json(binding)
        return validate(material)
    except BaseException:
        if instance is not None:
            tree.nodes.remove(instance)
        if not destination.links:
            tree.links.new(source, destination)
        for key in (PROFILE_KEY, _OWNER, _BINDING):
            if key in material:
                del material[key]
        if group.users == 0:
            bpy.data.node_groups.remove(group)
        raise


def bind_hm(material, instance):
    """Explicitly upgrade a valid v2 seam; leave its owned shader untouched."""
    before = validate(material)
    _need(before['profile'] == PROFILE, 'HM binding requires a valid unmigrated v2 fixed shadow')
    tree, fixed, group, source, _ = _binding(material)
    owner, hm_input, hm_output = _hm_source(material, instance)
    _need(not hm_input.is_linked and not hm_output.is_linked,
          'A new HM source must have unlinked Original Color and Color sockets')
    old_binding = material[_BINDING]
    binding = json.loads(old_binding)
    binding.update(hm_instance=instance.name, hm_owner=owner)
    try:
        tree.links.new(source, hm_input)
        tree.links.new(hm_output, fixed.inputs[0])
        material[PROFILE_KEY], group[PROFILE_KEY] = HM_PROFILE, HM_PROFILE
        material[_BINDING] = _json(binding)
        return validate(material)
    except BaseException:
        for socket in (hm_input, hm_output):
            for link in tuple(socket.links):
                tree.links.remove(link)
        tree.links.new(source, fixed.inputs[0])
        material[PROFILE_KEY], group[PROFILE_KEY] = PROFILE, PROFILE
        material[_BINDING] = old_binding
        raise


def unbind_hm(material):
    """Restore the original v2 seam; the caller removes its own HM instance."""
    before = validate(material)
    _need(before['profile'] == HM_PROFILE, 'HM unbinding requires a valid HM v3 fixed shadow')
    tree, fixed, group, _, _ = _binding(material)
    old_binding = material[_BINDING]
    binding = json.loads(old_binding)
    instance = tree.nodes[binding['hm_instance']]
    _, hm_input, hm_output = _hm_source(material, instance, owner=binding['hm_owner'])
    source = tree.nodes[binding['source']].outputs['Color']
    try:
        tree.links.new(source, fixed.inputs[0])
        for link in tuple(hm_input.links):
            tree.links.remove(link)
        material[PROFILE_KEY], group[PROFILE_KEY] = PROFILE, PROFILE
        material[_BINDING] = _json({key: binding[key] for key in ('source', 'destination')})
        return validate(material)
    except BaseException:
        tree.links.new(source, hm_input)
        tree.links.new(hm_output, fixed.inputs[0])
        material[PROFILE_KEY], group[PROFILE_KEY] = HM_PROFILE, HM_PROFILE
        material[_BINDING] = old_binding
        raise


def update(material, *, strength=None, tint=None):
    """Update static constants in place; never migrate an existing v1 layer."""
    before = validate(material)
    legacy = before['profile'] == LEGACY_PROFILE
    _need(strength is not None or tint is not None, 'An explicit strength or tint update is required')
    _need(not legacy or tint is None,
          'Legacy v1 is a Light gate and has no multiply tint; remove it before explicitly attaching v2')
    requested = before['strength'] if strength is None else _strength(strength)
    requested_tint = None if legacy else (before['tint'] if tint is None else _tint(tint))
    _, _, group, _, _ = _binding(material)
    nodes = _nodes(group)
    socket = nodes['strength'].outputs[0]
    old_value, old_declaration, old_signature = socket.default_value, group[_DECLARED], group[_SIGNATURE]
    tint_socket = None if legacy else nodes['tint'].outputs[0]
    old_tint = None if legacy else list(tint_socket.default_value)
    try:
        socket.default_value = requested
        actual = float(socket.default_value)
        _need(abs(actual-requested) <= 4.e-7, 'Fixed hair shadow strength readback failed')
        declared = dict(uv_name=before['uv_name'], strength=actual, image=before['image'])
        if not legacy:
            tint_socket.default_value = (*requested_tint, 1.)
            actual_tint = list(tint_socket.default_value[:3])
            _need(all(abs(a-b) <= 4.e-7 for a, b in zip(actual_tint, requested_tint)),
                  'Fixed hair shadow tint readback failed')
            declared['tint'] = actual_tint
        group[_DECLARED] = _json(declared)
        group[_SIGNATURE] = _signature(group)
        return validate(material)
    except Exception:
        socket.default_value = old_value
        if tint_socket is not None:
            tint_socket.default_value = old_tint
        group[_DECLARED], group[_SIGNATURE] = old_declaration, old_signature
        raise


def replace_source(material, *, image=None, uv_name=None):
    """Replace only v2/v3 B input, retaining the owner and all external seams.

    The caller validates target consumers and actual UV existence. The old
    Image is retained by fake-user for saved-file recovery; pixels are never
    edited. Legacy Light gates keep their explicit remove/attach route.
    """
    before = validate(material)
    _need(before['profile'] in (PROFILE, HM_PROFILE),
          'Legacy v1 source replacement requires explicit removal and v2 attachment')
    _need(image is not None or uv_name is not None, 'Specify a B image or UV source')
    tree, _, group, _, _ = _binding(material)
    nodes = _nodes(group)
    old_image = group[_IMAGE]
    requested_image = old_image if image is None else image
    requested_uv = before['uv_name'] if uv_name is None else uv_name
    _need(type(requested_uv) is str and requested_uv.strip() and '\x00' not in requested_uv,
          'Fixed hair shadow UV name is invalid')
    if before['profile'] == HM_PROFILE:
        hm = tree.nodes[before['hm_instance']].node_tree
        _need(requested_image not in (hm.get('toon_hm_image'), hm.get('toon_hm_highlight_mask_image')),
              'Use a dedicated B image, separate from HM and highlight-mask inputs')
    descriptor = _image_descriptor(requested_image)
    old_uv, old_declaration, old_signature = nodes['uv'].uv_map, group[_DECLARED], group[_SIGNATURE]
    old_fake = old_image.use_fake_user
    try:
        nodes['texture'].image = requested_image
        group[_IMAGE] = requested_image
        nodes['uv'].uv_map = requested_uv
        _need(nodes['texture'].image == requested_image and group[_IMAGE] == requested_image
              and nodes['uv'].uv_map == requested_uv, 'Fixed shadow source readback differs')
        group[_DECLARED] = _json(dict(uv_name=requested_uv, strength=before['strength'],
                                      tint=before['tint'], image=descriptor))
        group[_SIGNATURE] = _signature(group)
        if requested_image != old_image:
            old_image.use_fake_user = True
        return validate(material)
    except BaseException:
        nodes['texture'].image = old_image
        group[_IMAGE] = old_image
        nodes['uv'].uv_map = old_uv
        group[_DECLARED], group[_SIGNATURE] = old_declaration, old_signature
        old_image.use_fake_user = old_fake
        raise


def update_strength(material, strength):
    """Compatibility entry; v1 stays v1 and v2 preserves its multiply tint."""
    return update(material, strength=strength)


def remove(material):
    """Restore the exact old edge and reclaim only this exclusive owned Group."""
    before = validate(material)
    _need(before['profile'] != HM_PROFILE, 'Unbind the HM layer before removing its fixed hair shadow')
    tree, instance, group, source, destination = _binding(material)
    tree.links.new(source, destination)
    tree.nodes.remove(instance)
    for key in (PROFILE_KEY, _OWNER, _BINDING):
        del material[key]
    import bpy
    _need(group.users == 0, 'Owned fixed hair shadow Group unexpectedly acquired a consumer')
    bpy.data.node_groups.remove(group)
    return dict(profile=before['profile'], material=material.name, removed_owner=before['owner'], restored=True)


def render_snapshot(scene, geometry):
    """Small evidence from the actual captured RENDER UV and live shader."""
    import numpy as np
    result=[]; seen=set()
    for mesh in geometry.meshes:
        obj=scene.objects.get(mesh.name)
        if obj is None:continue
        for slot in obj.material_slots:
            material=slot.material
            if not is_present(material):continue
            if material in seen:raise ValueError('Fixed hair shadow needs an exclusive render target')
            seen.add(material)
            # Frozen Object.data is a new evaluated Mesh. The protected old
            # author Mesh remains in Main and legitimately adds a material
            # user. This is read-only observation; mutating APIs retain the
            # original exclusive-user admission.
            descriptor=validate(material,observation=True)
            matches=[u for u in mesh.uv_layers if u['name']==descriptor['uv_name']]
            if len(matches)!=1:raise ValueError('Fixed hair shadow RENDER UV is missing')
            uv=np.ascontiguousarray(matches[0]['uv'],dtype='<f4')
            if not np.isfinite(uv).all():raise ValueError('Fixed hair shadow RENDER UV is nonfinite')
            result.append(dict(target=obj.name,descriptor=descriptor,
                uv_sha256=hashlib.sha256(uv.tobytes()).hexdigest(),uv_shape=list(uv.shape)))
    return result
