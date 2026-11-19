"""Owned native surface materials and bounded three-way parameter updates.

This module never saves a file, changes render settings, or edits source materials.
The caller must first isolate target Mesh datablocks in its working candidate.
"""
import hashlib
import json
import math
import struct
import uuid

from .contract import WorkflowError
from . import contract

SCHEMA = 'toon-materials-snapshot-v1'
SCOPE = '_toon_scope_id'
ROLE = '_toon_role'
IDENTITY = '_toon_material_id'
NODE_ID = '_toon_node_id'
GROUP_ID = '_toon_core_id'
DECLARED = '_toon_declared_shading'
BINDINGS = '_toon_material_bindings'
FIELDS = ('mode', 'base_color', 'shadow_color', 'lit_color', 'threshold', 'toon_size', 'toon_smooth')
ACTIVE = {'BLENDER_EEVEE': ('base_color', 'shadow_color', 'lit_color', 'threshold'),
          'CYCLES': ('base_color', 'toon_size', 'toon_smooth')}
SOCKETS = {'base_color': 'Base Color', 'shadow_color': 'Shadow Color',
           'lit_color': 'Lit Color', 'threshold': 'Threshold',
           'toon_size': 'Toon Size', 'toon_smooth': 'Toon Smooth'}
DEFAULTS = {'base_color': [.8, .8, .8, 1.], 'shadow_color': [.035, .065, .14, 1.],
            'lit_color': [.72, .87, 1., 1.], 'threshold': .18,
            'toon_size': .5, 'toon_smooth': 0.}
CEL3_FIELDS = ('mode', 'base_color', 'shadow_color', 'mid_color', 'lit_color',
               'threshold_low', 'threshold_high')
CEL3_ACTIVE = CEL3_FIELDS[1:]
CEL3_SOCKETS = {'base_color': 'Base Color', 'shadow_color': 'Shadow Color',
                'mid_color': 'Mid Color', 'lit_color': 'Lit Color',
                'threshold_low': 'Threshold Low', 'threshold_high': 'Threshold High'}
CEL3_DEFAULTS = {'base_color': [.8, .8, .8, 1.], 'shadow_color': [.035, .065, .14, 1.],
                 'mid_color': [.28, .46, .68, 1.], 'lit_color': [.72, .87, 1., 1.],
                 'threshold_low': .18, 'threshold_high': .5}
CEL3_REFLECT_FIELDS = CEL3_FIELDS + ('reflection_color', 'reflection_strength', 'reflection_roughness')
CEL3_REFLECT_ACTIVE = CEL3_REFLECT_FIELDS[1:]
CEL3_REFLECT_SOCKETS = {**CEL3_SOCKETS, 'reflection_color': 'Reflection Color',
                       'reflection_strength': 'Reflection Strength',
                       'reflection_roughness': 'Reflection Roughness'}
CEL3_REFLECT_DEFAULTS = {**CEL3_DEFAULTS, 'reflection_color': [.65, .7, .8, 1.],
                        'reflection_strength': .35, 'reflection_roughness': .26}


def is_managed_mode(mode):
    return mode in ('native_cel', 'eevee_cel3', 'eevee_cel3_reflect')


def _fields(mode):
    if mode == 'eevee_cel3_reflect':
        return CEL3_REFLECT_FIELDS
    return CEL3_FIELDS if mode == 'eevee_cel3' else FIELDS


def _active(engine, mode):
    if mode == 'eevee_cel3_reflect':
        return CEL3_REFLECT_ACTIVE
    return CEL3_ACTIVE if mode == 'eevee_cel3' else ACTIVE[engine]


def _sockets(mode):
    if mode == 'eevee_cel3_reflect':
        return CEL3_REFLECT_SOCKETS
    return CEL3_SOCKETS if mode == 'eevee_cel3' else SOCKETS


def _error(code, message, path='shading'):
    raise WorkflowError(code, message, path)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _plain(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _error('invalid_material_state', 'Nonfinite material socket value')
        return value
    try:
        return [_plain(v) for v in value]
    except TypeError:
        _error('invalid_material_state', 'Unsupported material socket value')


def _scope(scope_id):
    try:
        parsed = str(uuid.UUID(scope_id))
    except (ValueError, TypeError, AttributeError):
        _error('invalid_scope', 'scope_id must be a canonical UUID', 'scope_id')
    if parsed != scope_id:
        _error('invalid_scope', 'scope_id must be a canonical UUID', 'scope_id')
    return parsed


def _settings(settings, engine):
    if engine not in ACTIVE:
        _error('unsupported_engine', 'Native material engine must be BLENDER_EEVEE or CYCLES', 'engine')
    if not isinstance(settings, dict):
        _error('invalid_shading', 'Exact shading fields are required')
    mode = settings.get('mode')
    fields = _fields(mode)
    if set(settings) != set(fields):
        _error('invalid_shading', 'Exact shading fields are required')
    if mode not in ('preserve', 'native_cel', 'eevee_cel3', 'eevee_cel3_reflect'):
        _error('invalid_shading', 'Unsupported shading mode', 'shading.mode')
    if mode in ('eevee_cel3', 'eevee_cel3_reflect') and engine != 'BLENDER_EEVEE':
        _error('unsupported_shading', mode + ' requires BLENDER_EEVEE', 'shading.mode')
    result = {'mode': settings['mode']}
    for key in fields[1:]:
        value = settings[key]
        if key.endswith('_color'):
            if not isinstance(value, (tuple, list)) or len(value) != 4:
                _error('invalid_shading', 'Color must have four finite channels', 'shading.' + key)
            values = value
        else:
            values = [value]
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            _error('invalid_shading', 'Finite [0,1] values required', 'shading.' + key)
        result[key] = [float(v) for v in values] if key.endswith('_color') else float(value)
        if settings['mode'] == 'native_cel' and key.endswith('_color') and result[key][3] != 1.:
            _error('unsupported_shading', 'This opaque native_cel slice requires color alpha=1', 'shading.' + key)
        if mode in ('eevee_cel3', 'eevee_cel3_reflect') and key.endswith('_color') and result[key][3] != 1.:
            _error('unsupported_shading', 'This opaque ' + mode + ' slice requires color alpha=1', 'shading.' + key)
    if mode in ('eevee_cel3', 'eevee_cel3_reflect') and result['threshold_low'] >= result['threshold_high']:
        _error('invalid_shading', 'threshold_low must be less than threshold_high', 'shading.threshold_low')
    return result


def _close(a, b):
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if type(a) in (int, float) and type(b) in (int, float):
        # Compare the actual RNA float32 representation, so a representable
        # small user request is not silently discarded by an epsilon gate.
        return struct.pack('<f', a) == struct.pack('<f', b)
    return a == b


def _consumption(engine, mode='native_cel'):
    if mode == 'eevee_cel3_reflect':
        return {'consumed_parameters': list(CEL3_REFLECT_ACTIVE), 'unconsumed_parameters': [],
                'color_semantics': 'Linear RGB, opaque alpha=1. No transparency shader.',
                'backend_semantics': 'Real Diffuse -> ShaderToRGB -> RGBToBW -> two GREATER_THAN thresholds -> shadow/mid/lit Emission; Add Shader with a native GGX Glossy closure whose Color is reflection_color times reflection_strength. Equality belongs to the darker diffuse band; total-light diffuse quantization per sample before pixel filtering. Reflection remains a BSDF, not ShaderToRGB.',
                'limits': ['EEVEE-only opaque three-band base fill plus total specular reflection from lamps and World/scene probes; direct highlights and World reflection are not independently controlled. Reflection roughness affects both sources.',
                           'Add Shader is an art-directed, non-energy-conserving sum; not complete PBR metallic/IOR, real-metal BRDF, SDF face, or production face/skin/hair/eye shader acceptance.',
                           'No image-domain recoloring or scene light/filter/sample/device mutation.',
                           'Static managed inputs only; animation/drivers or incoming links to managed inputs reject parameter updates.']}
    if mode == 'eevee_cel3':
        return {'consumed_parameters': list(CEL3_ACTIVE), 'unconsumed_parameters': [],
                'color_semantics': 'Linear RGB, opaque alpha=1. No transparency shader.',
                'backend_semantics': 'Real Diffuse -> ShaderToRGB -> RGBToBW -> two GREATER_THAN thresholds -> shadow/mid/lit Emission. Equality belongs to the darker band; total-light quantization per sample before pixel filtering.',
                'limits': ['EEVEE-only opaque three-band base fill; not per-light BRDF or production PBR/face/skin/hair/eye shader acceptance.',
                           'No image-domain recoloring or scene light/filter/sample/device mutation.',
                           'Static managed inputs only; animation/drivers or incoming links to managed inputs reject parameter updates.']}
    active = list(ACTIVE[engine])
    return {'consumed_parameters': active,
            'unconsumed_parameters': [key for key in FIELDS[1:] if key not in active],
            'color_semantics': 'Linear RGB, opaque alpha=1. No transparency shader.',
            'backend_semantics': ('Real Diffuse -> ShaderToRGB -> RGBToBW -> hard threshold -> shadow/lit Emission; quantization before pixel filtering.'
                                  if engine == 'BLENDER_EEVEE' else
                                  'Native Toon DIFFUSE closure. Per-light renderer response; not total-light readback or arbitrary shadow/lit palette quantization.'),
            'limits': ['No production three-family/face/skin/hair/eye shader acceptance.',
                       'No image-domain recoloring or scene light/filter/sample/device mutation.',
                       'Static managed inputs only; animation/drivers or incoming links to managed inputs reject parameter updates.']}


def _tag(item, scope_id, role):
    item[SCOPE] = scope_id
    item[ROLE] = role


def _new_node(tree, scope_id, node_id, kind, x, y=0):
    node = tree.nodes.new(kind)
    _tag(node, scope_id, 'managed_core_node')
    node[NODE_ID] = node_id
    node.name = 'Toon/' + node_id
    node.label = node_id
    node.location = (x, y)
    return node


def _build(bpy, settings, engine, scope_id, created_materials, created_groups):
    mode = settings['mode']
    active, sockets = _active(engine, mode), _sockets(mode)
    defaults = (CEL3_REFLECT_DEFAULTS if mode == 'eevee_cel3_reflect' else
                CEL3_DEFAULTS if mode == 'eevee_cel3' else DEFAULTS)
    mid, gid = str(uuid.uuid4()), str(uuid.uuid4())
    group = bpy.data.node_groups.new('ToonCore_' + gid, 'ShaderNodeTree')
    created_groups.append(group)
    _tag(group, scope_id, 'shading_core')
    group[GROUP_ID] = gid
    group['_toon_engine'] = engine
    for key in active:
        socket = group.interface.new_socket(name=sockets[key], in_out='INPUT',
                                            socket_type='NodeSocketColor' if key.endswith('_color') else 'NodeSocketFloat')
        socket.default_value = defaults[key]
    group.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    incoming = _new_node(group, scope_id, 'inputs', 'NodeGroupInput', -800)
    outgoing = _new_node(group, scope_id, 'output', 'NodeGroupOutput', 650)
    if mode in ('eevee_cel3', 'eevee_cel3_reflect'):
        diffuse = _new_node(group, scope_id, 'diffuse', 'ShaderNodeBsdfDiffuse', -600)
        diffuse.inputs['Roughness'].default_value = 0.
        rgb = _new_node(group, scope_id, 'shader_to_rgb', 'ShaderNodeShaderToRGB', -400)
        bw = _new_node(group, scope_id, 'luminance', 'ShaderNodeRGBToBW', -200)
        low = _new_node(group, scope_id, 'hard_threshold_low', 'ShaderNodeMath', 0, -100)
        high = _new_node(group, scope_id, 'hard_threshold_high', 'ShaderNodeMath', 0, 100)
        low.operation = high.operation = 'GREATER_THAN'
        shadow_mid = _new_node(group, scope_id, 'hard_palette_shadow_mid', 'ShaderNodeMixRGB', 200, -100)
        lit = _new_node(group, scope_id, 'hard_palette_lit', 'ShaderNodeMixRGB', 400)
        for mix in (shadow_mid, lit):
            mix.blend_type = 'MIX'
            mix.use_clamp = False
        emission = _new_node(group, scope_id, 'emission', 'ShaderNodeEmission', 600)
        emission.inputs['Strength'].default_value = 1.
        outgoing.location = (850, 0)
        links = [(incoming.outputs['Base Color'], diffuse.inputs['Color']),
                 (diffuse.outputs['BSDF'], rgb.inputs['Shader']),
                 (rgb.outputs['Color'], bw.inputs['Color']),
                 (bw.outputs['Val'], low.inputs[0]),
                 (incoming.outputs['Threshold Low'], low.inputs[1]),
                 (bw.outputs['Val'], high.inputs[0]),
                 (incoming.outputs['Threshold High'], high.inputs[1]),
                 (low.outputs[0], shadow_mid.inputs[0]),
                 (incoming.outputs['Shadow Color'], shadow_mid.inputs[1]),
                 (incoming.outputs['Mid Color'], shadow_mid.inputs[2]),
                 (high.outputs[0], lit.inputs[0]),
                 (shadow_mid.outputs['Color'], lit.inputs[1]),
                 (incoming.outputs['Lit Color'], lit.inputs[2]),
                 (lit.outputs['Color'], emission.inputs['Color']),
                 (emission.outputs['Emission'], outgoing.inputs['Shader'])]
        if mode == 'eevee_cel3_reflect':
            reflection_color_scale = _new_node(group, scope_id, 'reflection_color_scale',
                                              'ShaderNodeMixRGB', 200, -350)
            reflection_color_scale.blend_type = 'MULTIPLY'
            reflection_color_scale.use_clamp = False
            reflection_color_scale.inputs[0].default_value = 1.
            reflection = _new_node(group, scope_id, 'reflection', 'ShaderNodeBsdfAnisotropic', 600, -300)
            reflection.distribution = 'GGX'
            reflection.inputs['Anisotropy'].default_value = 0.
            reflection.inputs['Rotation'].default_value = 0.
            combined = _new_node(group, scope_id, 'diffuse_plus_reflection', 'ShaderNodeAddShader', 850)
            outgoing.location = (1050, 0)
            links[-1:] = [(incoming.outputs['Reflection Color'], reflection_color_scale.inputs[1]),
                          (incoming.outputs['Reflection Strength'], reflection_color_scale.inputs[2]),
                          (reflection_color_scale.outputs['Color'], reflection.inputs['Color']),
                          (incoming.outputs['Reflection Roughness'], reflection.inputs['Roughness']),
                          (emission.outputs['Emission'], combined.inputs[0]),
                          (reflection.outputs['BSDF'], combined.inputs[1]),
                          (combined.outputs['Shader'], outgoing.inputs['Shader'])]
    elif engine == 'BLENDER_EEVEE':
        diffuse = _new_node(group, scope_id, 'diffuse', 'ShaderNodeBsdfDiffuse', -600)
        diffuse.inputs['Roughness'].default_value = 0.
        rgb = _new_node(group, scope_id, 'shader_to_rgb', 'ShaderNodeShaderToRGB', -400)
        bw = _new_node(group, scope_id, 'luminance', 'ShaderNodeRGBToBW', -200)
        step = _new_node(group, scope_id, 'hard_threshold', 'ShaderNodeMath', 0)
        step.operation = 'GREATER_THAN'
        mix = _new_node(group, scope_id, 'hard_palette', 'ShaderNodeMixRGB', 200)
        mix.blend_type = 'MIX'
        mix.use_clamp = False
        emission = _new_node(group, scope_id, 'emission', 'ShaderNodeEmission', 430)
        emission.inputs['Strength'].default_value = 1.
        links = [(incoming.outputs['Base Color'], diffuse.inputs['Color']),
                 (diffuse.outputs['BSDF'], rgb.inputs['Shader']),
                 (rgb.outputs['Color'], bw.inputs['Color']),
                 (bw.outputs['Val'], step.inputs[0]),
                 (incoming.outputs['Threshold'], step.inputs[1]),
                 (step.outputs[0], mix.inputs[0]),
                 (incoming.outputs['Shadow Color'], mix.inputs[1]),
                 (incoming.outputs['Lit Color'], mix.inputs[2]),
                 (mix.outputs['Color'], emission.inputs['Color']),
                 (emission.outputs['Emission'], outgoing.inputs['Shader'])]
    else:
        toon = _new_node(group, scope_id, 'toon_diffuse', 'ShaderNodeBsdfToon', -200)
        toon.component = 'DIFFUSE'
        links = [(incoming.outputs['Base Color'], toon.inputs['Color']),
                 (incoming.outputs['Toon Size'], toon.inputs['Size']),
                 (incoming.outputs['Toon Smooth'], toon.inputs['Smooth']),
                 (toon.outputs['BSDF'], outgoing.inputs['Shader'])]
    for source, target in links:
        group.links.new(source, target)
    material = bpy.data.materials.new('ToonMaterial_' + mid)
    created_materials.append(material)
    _tag(material, scope_id, 'shading_material')
    material[IDENTITY] = mid
    material[GROUP_ID] = gid
    material['_toon_engine'] = engine
    material[DECLARED] = _json(settings)
    material.use_nodes = True
    material.node_tree.nodes.clear()  # Only this newly allocated owned material.
    wrapper = material.node_tree.nodes.new('ShaderNodeGroup')
    _tag(wrapper, scope_id, 'core_instance')
    wrapper[NODE_ID] = 'core_instance'
    wrapper.name = 'Toon/Managed Core - user wrapper outside'
    wrapper.node_tree = group
    wrapper.location = (0, 0)
    for key in active:
        wrapper.inputs[sockets[key]].default_value = settings[key]
    output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
    _tag(output, scope_id, 'managed_output')
    output[NODE_ID] = 'material_output'
    output.name = 'Toon/Output'
    output.target = 'ALL'
    output.location = (400, 0)
    material.node_tree.links.new(wrapper.outputs['Shader'], output.inputs['Surface'])
    return material


def _local_static(item, description):
    if item is None or getattr(item, 'library', None) or getattr(item, 'override_library', None):
        _error('protected_material', description + ' must be local and non-override')
    if getattr(item, 'animation_data', None):
        _error('unsupported_material_animation', description + ' must have static managed state')


def _stored_json(item, key):
    value = item.get(key)
    if not isinstance(value, str):
        _error('invalid_material_state', 'Missing owned material metadata: ' + key)
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        _error('invalid_material_state', 'Invalid owned material metadata: ' + key)


def _node_semantics(node, include_distribution=False):
    data = {'type': node.bl_idname, 'mute': node.mute,
            'inputs': [[s.identifier, _plain(s.default_value)] for s in node.inputs if hasattr(s, 'default_value')]}
    for prop in ('operation', 'component', 'blend_type', 'use_clamp', 'is_active_output', 'target'):
        if hasattr(node, prop):
            data[prop] = _plain(getattr(node, prop))
    if include_distribution and hasattr(node, 'distribution'):
        data['distribution'] = _plain(node.distribution)
    return data


def _core_structure(group, scope_id, mode=None):
    _local_static(group, 'Managed shader core')
    if group.get(SCOPE) != scope_id or group.get(ROLE) != 'shading_core' or not group.get(GROUP_ID):
        _error('managed_structure_conflict', 'Managed core ownership changed')
    if group.users != 1:
        _error('shared_material_core', 'Managed core has additional consumers; explicit isolation required')
    records, lookup = {}, {}
    for node in group.nodes:
        nid = node.get(NODE_ID)
        if not isinstance(nid, str) or nid in records or node.get(SCOPE) != scope_id or node.get(ROLE) != 'managed_core_node':
            _error('managed_structure_conflict', 'Missing/duplicate/foreign core node identity')
        lookup[node] = nid
        records[nid] = _node_semantics(node, include_distribution=(
            mode == 'eevee_cel3_reflect' and node.bl_idname == 'ShaderNodeBsdfAnisotropic'))
    interface = []
    for item in group.interface.items_tree:
        if item.item_type != 'SOCKET':
            _error('managed_structure_conflict', 'Unexpected core interface panel')
        interface.append({'identifier': item.identifier, 'name': item.name, 'direction': item.in_out,
                          'type': item.socket_type,
                          'default': _plain(item.default_value) if hasattr(item, 'default_value') else None})
    links = sorted([lookup[x.from_node], x.from_socket.identifier, lookup[x.to_node], x.to_socket.identifier] for x in group.links)
    return {'group_id': group[GROUP_ID], 'engine': group.get('_toon_engine'), 'interface': interface, 'nodes': records, 'links': links}


def _wrapper_state(material, wrapper):
    """Observe but never reconcile arbitrary user-wrapper nodes or connections."""
    nodes = {}
    for node in material.node_tree.nodes:
        if node == wrapper:
            continue
        data = _node_semantics(node)
        if hasattr(node, 'node_tree') and node.node_tree:
            data['group_reference'] = [node.node_tree.name, node.node_tree.get(GROUP_ID),
                                       getattr(getattr(node.node_tree, 'library', None), 'filepath', None)]
        if hasattr(node, 'image') and node.image:
            data['image_reference'] = [node.image.name, node.image.filepath]
        if hasattr(node, 'color_ramp'):
            ramp = node.color_ramp
            data['ramp'] = [ramp.interpolation, ramp.color_mode, ramp.hue_interpolation,
                            [[e.position, list(e.color)] for e in ramp.elements]]
        nodes[node.name] = data
    links = sorted([x.from_node.name, x.from_socket.identifier, x.to_node.name, x.to_socket.identifier] for x in material.node_tree.links)
    return {'nodes': nodes, 'links': links}


def _capture(bpy, material, scope_id, animation_manifest=None):
    _local_static(material, 'Managed material')
    if animation_manifest is None:
        _local_static(material.node_tree, 'Managed material tree')
    else:
        if not contract.is_animated(animation_manifest):
            _error('unsupported_material_animation', 'Explicit animated manifest required')
        from . import animation_bpy
        animation_bpy.validate_tree(material, animation_manifest)
    engine = material.get('_toon_engine')
    if engine not in ACTIVE or material.get(ROLE) != 'shading_material' or material.get(SCOPE) != scope_id:
        _error('managed_structure_conflict', 'Material engine/ownership changed')
    candidates = [n for n in material.node_tree.nodes if n.get(ROLE) == 'core_instance']
    outputs = [n for n in material.node_tree.nodes if n.get(ROLE) == 'managed_output']
    if len(candidates) != 1 or len(outputs) != 1:
        _error('managed_structure_conflict', 'Exactly one managed core instance and output required')
    wrapper, output = candidates[0], outputs[0]
    if wrapper.bl_idname != 'ShaderNodeGroup' or output.bl_idname != 'ShaderNodeOutputMaterial':
        _error('managed_structure_conflict', 'Managed wrapper node types changed')
    if any(n.get(SCOPE) != scope_id for n in (wrapper, output)):
        _error('managed_structure_conflict', 'Managed wrapper ownership changed')
    if wrapper.mute or output.mute or not output.is_active_output or output.target != 'ALL':
        _error('managed_structure_conflict', 'Managed core/output activation changed')
    group = wrapper.node_tree
    if not group or group.get(GROUP_ID) != material.get(GROUP_ID):
        _error('managed_structure_conflict', 'Managed core reference changed')
    settings = _settings(_stored_json(material, DECLARED), engine)
    active, sockets = _active(engine, settings['mode']), _sockets(settings['mode'])
    for key in active:
        socket = wrapper.inputs.get(sockets[key])
        if socket is None or socket.is_linked:
            _error('managed_parameter_linked', 'Managed parameter input missing or linked: ' + key)
        settings[key] = _plain(socket.default_value)
    # Revalidate hand-edited ranges as well as declared inactive fields.
    settings = _settings(settings, engine)
    actual_bindings = []
    for obj in bpy.data.objects:
        for index, slot in enumerate(getattr(obj, 'material_slots', ())):
            if slot.material == material:
                actual_bindings.append({'object': obj.name, 'mesh': obj.data.name, 'slot': index, 'link': slot.link})
    actual_bindings.sort(key=lambda x: (x['object'], x['slot']))
    if actual_bindings != _stored_json(material, BINDINGS):
        _error('shared_material_consumer', 'Managed material consumer map changed')
    allowed_users = {bpy.data.objects[row['object']].data if row['link'] == 'DATA' else bpy.data.objects[row['object']]
                     for row in actual_bindings}
    incoming = set(bpy.data.user_map().get(material, ()))
    if incoming != allowed_users:
        _error('shared_material_consumer', 'Managed material has a non-slot or foreign incoming ID consumer')
    incoming_identities = sorted([{'type': user.bl_rna.identifier, 'name': user.name,
                                   'library': getattr(getattr(user, 'library', None), 'filepath', None)}
                                  for user in incoming], key=lambda row: (row['type'], row['name'], row['library'] or ''))
    previous_refs = {}
    for key in sorted(material.keys()):
        if key.startswith('_toon_previous_material_'):
            previous = material[key]
            if not isinstance(previous, bpy.types.Material):
                _error('managed_structure_conflict', 'Original material retention reference changed type')
            previous_refs[key] = {'name': previous.name,
                                  'library': getattr(getattr(previous, 'library', None), 'filepath', None)}
    structure = {'material_id': material.get(IDENTITY), 'engine': engine,
                 'core_instance_id': wrapper.get(NODE_ID), 'output_id': output.get(NODE_ID),
                 'core': _core_structure(group, scope_id, settings['mode']), 'bindings': actual_bindings,
                 'incoming_id_consumers': incoming_identities,
                 'previous_material_references': previous_refs}
    # Optional native feature values belong to the structural admission digest.
    # Leave legacy snapshots byte-for-byte unchanged when the feature is absent.
    from . import band_boundary
    boundary = band_boundary.snapshot(material, wrapper=wrapper, group=group)
    if boundary is not None:
        structure['band_boundary'] = boundary
    return {'material_id': material[IDENTITY], 'name': material.name, 'engine': engine,
            'settings': settings, 'active_fields': list(active),
            'structure': structure, 'structure_sha256': _digest(structure),
            'bindings': actual_bindings, 'wrapper': _wrapper_state(material, wrapper)}


def managed_snapshot(scope_id, animation_manifest=None):
    """Return JSON-safe baseline. User-wrapper state is observed, not owned."""
    import bpy
    _scope(scope_id)
    materials = [m for m in bpy.data.materials if m.get(SCOPE) == scope_id and m.get(ROLE) == 'shading_material']
    rows = [_capture(bpy, m, scope_id, animation_manifest=animation_manifest) for m in materials]
    ids = [x['material_id'] for x in rows]
    if len(ids) != len(set(ids)) or any(not isinstance(x, str) for x in ids):
        _error('managed_structure_conflict', 'Duplicate or missing material identity')
    return {'schema': SCHEMA, 'scope_id': scope_id, 'materials': sorted(rows, key=lambda x: x['material_id'])}


def apply_materials(objects, settings, engine, scope_id):
    """Build new owned materials, bind isolated targets, never modify old graphs."""
    normalized = _settings(settings, engine)
    _scope(scope_id)
    if normalized['mode'] == 'preserve':
        return {'scope_id': scope_id, 'mode': 'preserve', 'changed': False,
                'consumed_parameters': [], 'unconsumed_parameters': list(FIELDS[1:]), 'materials': []}
    import bpy
    objects = list(objects)
    if not objects or len({id(obj) for obj in objects}) != len(objects):
        _error('invalid_material_target', 'Unique nonempty target object list required')
    if any(m.get(SCOPE) == scope_id and m.get(ROLE) == 'shading_material' for m in bpy.data.materials):
        _error('managed_conflict', 'Scope already contains shading materials; use update')
    for obj in objects:
        if obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.override_library or obj.data.library or obj.data.override_library:
            _error('protected_material_target', 'Local non-override Object Mode Mesh required')
        if obj.data.users != 1:
            _error('shared_mesh', 'Caller must isolate target Mesh before material binding')
    created_materials, created_groups, old_slots = [], [], []
    receipts = []
    try:
        for obj in objects:
            original = [slot.material for slot in obj.material_slots]
            old_slots.append((obj, original))
            material = _build(bpy, normalized, engine, scope_id, created_materials, created_groups)
            # Datablock ID custom properties keep the previous slot materials
            # referenced across save/reopen without editing their fake_user or
            # shader graphs. The parent verifies these references on cold read.
            for index, previous in enumerate(original):
                if previous is not None:
                    material['_toon_previous_material_%04d' % index] = previous
            if not original:
                obj.data.materials.append(material)
            else:
                for slot in obj.material_slots:
                    slot.material = material
            bindings = [{'object': obj.name, 'mesh': obj.data.name, 'slot': i, 'link': slot.link}
                        for i, slot in enumerate(obj.material_slots)]
            material[BINDINGS] = _json(sorted(bindings, key=lambda x: (x['object'], x['slot'])))
            receipts.append({'object': obj.name, 'material_id': material[IDENTITY], 'material': material.name,
                             'core_group': material.node_tree.nodes.get('Toon/Managed Core - user wrapper outside').node_tree.name,
                             'old_materials': [m.name if m else None for m in original], 'bindings': bindings})
        snapshot = managed_snapshot(scope_id)
    except Exception:
        for obj, original in reversed(old_slots):
            if not original:
                if len(obj.data.materials):
                    obj.data.materials.pop(index=len(obj.data.materials) - 1)
            else:
                for index, material in enumerate(original):
                    obj.material_slots[index].material = material
        for material in reversed(created_materials):
            if material.get(SCOPE) == scope_id and material.get(ROLE) == 'shading_material':
                bpy.data.materials.remove(material)
        for group in reversed(created_groups):
            if group.get(SCOPE) == scope_id and group.get(ROLE) == 'shading_core' and group.users == 0:
                bpy.data.node_groups.remove(group)
        raise
    return {'scope_id': scope_id, 'mode': normalized['mode'], 'changed': True, 'engine': engine,
            **_consumption(engine, normalized['mode']), 'materials': receipts, 'snapshot': snapshot}


def _plan_update(scope_id, baseline, current, desired, baseline_declared=None, *, defer_joint_validation=False):
    """Pure preflight: desired is a complete manifest.shading dictionary."""
    _scope(scope_id)
    if not isinstance(baseline, dict) or set(baseline) != {'schema', 'scope_id', 'materials'} or baseline['schema'] != SCHEMA or baseline['scope_id'] != scope_id:
        _error('invalid_material_baseline', 'Exact managed_snapshot baseline shape and scope required')
    if current['schema'] != SCHEMA or current['scope_id'] != scope_id:
        _error('invalid_material_baseline', 'Current scope identity differs')
    if not isinstance(baseline['materials'], list):
        _error('invalid_material_baseline', 'materials must be a list')
    row_fields = {'material_id', 'name', 'engine', 'settings', 'active_fields',
                  'structure', 'structure_sha256', 'bindings', 'wrapper'}
    for row in baseline['materials']:
        if not isinstance(row, dict) or set(row) != row_fields:
            _error('invalid_material_baseline', 'Exact material snapshot row fields required')
        if not isinstance(row['material_id'], str) or row['engine'] not in ACTIVE:
            _error('invalid_material_baseline', 'Invalid material snapshot identity/engine')
    base = {x['material_id']: x for x in baseline['materials']}
    now = {x['material_id']: x for x in current['materials']}
    if len(base) != len(baseline['materials']) or set(base) != set(now) or not base:
        _error('managed_structure_conflict', 'Material identities were added, removed or duplicated')
    plans = []
    for mid in sorted(base):
        b, c = base[mid], now[mid]
        if b['structure_sha256'] != _digest(b['structure']) or c['structure_sha256'] != _digest(c['structure']):
            _error('invalid_material_baseline', 'Snapshot structure digest mismatch')
        if b['structure'] != c['structure'] or b['engine'] != c['engine']:
            _error('managed_structure_conflict', 'Managed core or binding structure changed')
        engine = c['engine']
        d = _settings(desired, engine)
        bsettings, csettings = _settings(b['settings'], engine), _settings(c['settings'], engine)
        declared = _settings(baseline_declared, engine) if baseline_declared is not None else bsettings
        mode = d['mode']
        if not is_managed_mode(mode) or any(s['mode'] != mode for s in (bsettings, csettings, declared)):
            _error('unsupported_material_update', 'Mode changes require a separate explicit replacement operation')
        fields, active = _fields(mode), _active(engine, mode)
        changes = {}
        for field in fields[1:]:
            if _close(d[field], declared[field]):
                continue  # User did not request this field: retain current.
            if field not in active:
                _error('unconsumed_parameter', 'Requested parameter has no native effect in ' + engine + ': ' + field, 'shading.' + field)
            if not _close(csettings[field], bsettings[field]) and not _close(csettings[field], d[field]):
                _error('managed_parameter_conflict', 'Same-field user edit conflicts: ' + field, 'shading.' + field)
            if not _close(csettings[field], d[field]):
                changes[field] = d[field]
        # Retained user values and requested changes must jointly satisfy the
        # complete mode contract before update_parameters writes any material.
        if not defer_joint_validation:
            _settings({**csettings, **changes}, engine)
        plans.append({'material_id': mid, 'engine': engine, 'changes': changes,
                      'preserved_parameters': {k: csettings[k] for k in fields[1:] if k not in changes}})
    return plans


def update_parameters(scope_id, baseline, desired, baseline_declared=None):
    """Preflight every material, then touch only requested nonconflicting inputs."""
    import bpy
    current = managed_snapshot(scope_id)
    plans = _plan_update(scope_id, baseline, current, desired, baseline_declared)
    lookup = {m[IDENTITY]: m for m in bpy.data.materials
              if m.get(SCOPE) == scope_id and m.get(ROLE) == 'shading_material'}
    rollback = []
    try:
        for plan in plans:
            material = lookup[plan['material_id']]
            wrapper = next(n for n in material.node_tree.nodes if n.get(ROLE) == 'core_instance')
            sockets = _sockets(desired['mode'])
            for field, value in plan['changes'].items():
                socket = wrapper.inputs[sockets[field]]
                rollback.append((socket, _plain(socket.default_value)))
                socket.default_value = value
        after = managed_snapshot(scope_id)
        before_by_id = {r['material_id']: r for r in current['materials']}
        for row in after['materials']:
            before = before_by_id[row['material_id']]
            if row['structure'] != before['structure'] or row['wrapper'] != before['wrapper']:
                _error('managed_structure_conflict', 'Unexpected core/wrapper mutation during parameter update')
            plan = next(p for p in plans if p['material_id'] == row['material_id'])
            for field, value in plan['changes'].items():
                if not _close(row['settings'][field], value):
                    _error('material_parameter_not_applied', 'RNA parameter set/get differs: ' + field)
    except Exception:
        for socket, value in reversed(rollback):
            socket.default_value = value
        raise
    return {'scope_id': scope_id, 'changed': any(p['changes'] for p in plans),
            'updates': plans, 'consumption': {e: _consumption(e, desired['mode']) for e in sorted({p['engine'] for p in plans})},
            'snapshot': after, 'wrapper_policy': 'Current user wrapper retained; only managed static input values changed.'}
