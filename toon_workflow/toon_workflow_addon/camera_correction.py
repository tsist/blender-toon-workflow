# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable camera-space XY correction, with a separate managed identity.

This is an experimental geometry domain, not a shading or line backend.
``build`` appends a Geometry Nodes modifier and never applies transforms,
changes constraints, or edits the source mesh. ``configure`` preserves the
node graph; ``inspect`` reports invalid inputs and ``validate`` rejects them.
The render caller MUST validate every requested frame/subframe before using
the corrected RENDER geometry. No global handlers or drivers are installed.

Nodes use full matrices, including the target's shear. Invalid matrix/depth/
weight fields leave affected points unchanged and expose a Boolean diagnostic
attribute. External object-reference deletion/type changes additionally need
the mandatory Python admission check. A safe bypass is NOT success. Camera transforms
must be rigid (unit scale, no shear), matching the ordinary render camera.
Parent/constraint feedback from the corrected mesh is explicitly unsupported.
"""
import hashlib
import json
import math
import uuid

PROFILE = 'toon-camera-xy-depth.v1'
OWNER = 'toon_camera_correction_owner'
PROFILE_KEY = 'toon_camera_correction_profile'
_INTERFACE = 'toon_camera_correction_interface'
_SIGNATURE = 'toon_camera_correction_signature'
_VALID = 'toon_camera_correction_valid_attribute'
_NODE_ID = 'toon_camera_correction_node_id'
_TARGET = 'toon_camera_correction_target'
_UID = 'toon_camera_correction_modifier_uid'
_EPSILON = 1e-6


class CorrectionError(ValueError):
    """Invalid correction input or protected managed graph."""


def _strength(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CorrectionError('strength must be a finite number in [0,1]')
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise CorrectionError('strength must be a finite number in [0,1]')
    return value


def correct_camera_points(points, weights, reference_depth, strength=0.0):
    """Independent scalar reference; camera looks along negative Z.

    This helper consumes explicit camera-space coordinates. It does not
    silently clamp weights, change the projection anchor, or claim to
    preserve volume/normals. Invalid/behind-camera samples are rejected.
    """
    strength = _strength(strength)
    if not math.isfinite(reference_depth) or reference_depth <= _EPSILON:
        raise CorrectionError('reference_depth must be positive and finite')
    if len(points) != len(weights):
        raise CorrectionError('one weight per point is required')
    result = []
    for point, weight in zip(points, weights):
        if len(point) != 3 or not all(math.isfinite(v) for v in point):
            raise CorrectionError('camera points must be finite XYZ values')
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise CorrectionError('weights must be finite and within [0,1]')
        x, y, z = point
        if -z <= _EPSILON:
            raise CorrectionError('points at/behind the camera are unsupported')
        factor = 1 + strength * weight * (-z / reference_depth - 1)
        result.append((x * factor, y * factor, z))
    return result


def _socket(sockets, identifier):
    matches = [s for s in sockets if s.identifier == identifier]
    if len(matches) != 1:
        raise CorrectionError('expected one socket identifier: ' + identifier)
    return matches[0]


def _signature(group):
    def value(v):
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        if hasattr(v, 'name_full'):
            return ('ID', v.bl_rna.identifier, v.name_full)
        try:
            return [value(i) for i in v]
        except TypeError:
            return str(v)
    rows = []
    for node in group.nodes:
        row = [node.get(_NODE_ID), node.bl_idname]
        row.append({key: getattr(node, key) for key in
                    ('operation', 'data_type', 'input_type', 'domain',
                     'transform_space') if hasattr(node, key)})
        row.append([(s.identifier, value(s.default_value)) for s in node.inputs
                    if hasattr(s, 'default_value')])
        rows.append(row)
    links = [(l.from_node.get(_NODE_ID), l.from_socket.identifier,
              l.to_node.get(_NODE_ID), l.to_socket.identifier)
             for l in group.links]
    payload = dict(nodes=sorted(rows, key=lambda r: str(r[0])),
                   links=sorted(links), interface=json.loads(group[_INTERFACE]))
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode('utf8')).hexdigest()


def _owned(target, modifier):
    if modifier.id_data != target or modifier.type != 'NODES':
        raise CorrectionError('modifier does not belong to the target')
    group = modifier.node_group
    if (group is None or not group.get(OWNER) or
            group.get(PROFILE_KEY) != PROFILE or group.get(_TARGET) != target or
            group.get(_UID) != modifier.persistent_uid):
        raise CorrectionError('unrecognized correction owner/profile')
    if group.users != 1:
        raise CorrectionError('managed correction group has external users')
    if _signature(group) != group.get(_SIGNATURE):
        raise CorrectionError('managed correction graph was edited')
    modifiers=list(target.modifiers)
    index=next(i for i,m in enumerate(modifiers) if m.persistent_uid==modifier.persistent_uid)
    if any(m.show_viewport or m.show_render for m in modifiers[index+1:]):
        raise CorrectionError('camera correction must be the last enabled modifier; downstream deformation is unqualified')
    return group, json.loads(group[_INTERFACE])


def _input(modifier, identifiers, key):
    # Blender 5.2 modifier inputs are RNA properties, not legacy IDProperties.
    return getattr(modifier.properties.inputs, identifiers[key])


def _weight_values(target, name):
    attribute = target.data.attributes.get(name)
    if attribute is None or attribute.domain != 'POINT' or attribute.data_type != 'FLOAT':
        raise CorrectionError('weight_attribute must be an existing FLOAT POINT attribute')
    values = [d.value for d in attribute.data]
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
        raise CorrectionError('weight_attribute values must be finite within [0,1]')
    return values


def _preflight(target, camera, reference, weight_attribute, strength, depsgraph=None):
    import bpy
    strength = _strength(strength)
    if target is None or target.type != 'MESH' or target.library or target.override_library:
        raise CorrectionError('a local editable MESH target is required; library override not qualified')
    if camera is None or camera.type != 'CAMERA' or camera.data.type != 'PERSP':
        raise CorrectionError('an explicit perspective camera is required')
    if reference is None or reference == target:
        raise CorrectionError('an explicit independent reference object is required')
    if not isinstance(weight_attribute, str) or not weight_attribute or len(weight_attribute) > 63:
        raise CorrectionError('a nonempty weight_attribute of at most 63 characters is required')
    _weight_values(target, weight_attribute)
    _reject_geometry_feedback(target)
    graph = depsgraph or bpy.context.evaluated_depsgraph_get()
    evaluated = target.evaluated_get(graph)
    cam = camera.evaluated_get(graph)
    ref = reference.evaluated_get(graph)
    for label, matrix in [('target', evaluated.matrix_world), ('camera', cam.matrix_world)]:
        if (not all(math.isfinite(v) for row in matrix for v in row) or
                abs(matrix.determinant()) <= 1e-12):
            raise CorrectionError(label + ' matrix must be finite and invertible')
    basis = cam.matrix_world.to_3x3()
    gram = basis.transposed() @ basis
    if max(abs(gram[i][j] - float(i == j)) for i in range(3) for j in range(3)) > 1e-5 or basis.determinant() <= 0:
        raise CorrectionError('camera scale/shear/mirroring is unsupported; use a rigid camera transform')
    reference_depth = -(cam.matrix_world.inverted() @ ref.matrix_world.translation).z
    if not math.isfinite(reference_depth) or reference_depth <= _EPSILON:
        raise CorrectionError('reference is at/behind the camera or nonfinite')
    # Direct transform constraints/vertex parenting on the corrected geometry
    # can create a second deformation path. Do not rewrite them automatically.
    for obj in (camera, reference):
        cursor = obj
        visited = set()
        while cursor is not None:
            if cursor == target:
                raise CorrectionError('camera/reference parenting to corrected target is unsupported')
            if cursor.as_pointer() in visited:
                raise CorrectionError('cyclic camera/reference ancestry is unsupported')
            visited.add(cursor.as_pointer())
            if any(getattr(c, 'target', None) == target for c in cursor.constraints):
                raise CorrectionError('constraint feedback from corrected target is unsupported')
            cursor = cursor.parent
    return strength, float(reference_depth), graph


def _reject_geometry_feedback(target):
    """Reject common second paths through a corrected mesh; never retarget.

    Covers object parenting/constraints and native deformation modifiers. This
    is not a general depsgraph or arbitrary scripted-driver cycle solver.
    """
    def dependencies(obj):
        if obj.parent is not None:yield obj.parent
        for constraint in obj.constraints:
            if constraint.mute or constraint.influence==0:continue
            other=getattr(constraint,'target',None)
            if other is not None:yield other
        for modifier in obj.modifiers:
            if not(modifier.show_render or modifier.show_viewport):continue
            if modifier.type in {'ARMATURE','MESH_DEFORM','SURFACE_DEFORM','SHRINKWRAP','DATA_TRANSFER'}:
                other=getattr(modifier,'object',None) or getattr(modifier,'target',None)
                if other is not None:yield other
    queue=list(dependencies(target));seen=set()
    while queue:
        obj=queue.pop()
        if obj==target:
            raise CorrectionError('target geometry dependency feeds back to itself')
        if obj.as_pointer() in seen:continue
        seen.add(obj.as_pointer())
        if any(m.type=='NODES' and m.node_group and m.node_group.get(PROFILE_KEY)==PROFILE
               and (m.show_viewport or m.show_render) for m in obj.modifiers):
            raise CorrectionError('target depends on another corrected mesh: '+obj.name+
                '; use an explicitly verified uncorrected deformation reference')
        queue.extend(dependencies(obj))


def _animated_paths(target):
    """Read native control paths; static configure must not override animation."""
    animation = target.animation_data
    if animation is None:
        return set()
    paths = {curve.data_path for curve in animation.drivers}
    actions = []
    if animation.action:
        actions.append(animation.action)
    for track in animation.nla_tracks:
        for strip in track.strips:
            if strip.action:
                actions.append(strip.action)
    for action in actions:
        # Layered actions are the 5.2 path. Check every bag conservatively:
        # this operation has no contract for rewriting other slots or NLA.
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    paths.update(curve.data_path for curve in bag.fcurves)
    return paths


def _build_group(owner):
    import bpy
    group = bpy.data.node_groups.new('Toon Camera XY Correction', 'GeometryNodeTree')
    group[OWNER] = owner
    group[PROFILE_KEY] = PROFILE
    group[_VALID] = '.toon_camera_valid_' + owner[:12]
    identifiers = {}
    for key, name, kind in [('geometry', 'Geometry', 'NodeSocketGeometry'),
                            ('camera', 'Camera', 'NodeSocketObject'),
                            ('reference', 'Reference', 'NodeSocketObject'),
                            ('weight', 'Weight Attribute', 'NodeSocketString'),
                            ('strength', 'Strength', 'NodeSocketFloat')]:
        socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
        identifiers[key] = socket.identifier
        if key == 'strength':
            socket.default_value = 0.0
            socket.min_value = 0.0
            socket.max_value = 1.0
    identifiers['output'] = group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry').identifier
    group[_INTERFACE] = json.dumps(identifiers, sort_keys=True)

    def node(key, kind, **props):
        n = group.nodes.new(kind)
        n[_NODE_ID] = key
        n.label = key
        for k, v in props.items():
            setattr(n, k, v)
        return n
    def link(source, dest):
        group.links.new(source, dest)
    def transform(key, vector, matrix):
        n = node(key, 'FunctionNodeTransformPoint')
        link(vector, _socket(n.inputs, 'Vector'))
        link(matrix, _socket(n.inputs, 'Transform'))
        return _socket(n.outputs, 'Vector')
    def invert(key, matrix):
        n = node(key, 'FunctionNodeInvertMatrix')
        link(matrix, _socket(n.inputs, 'Matrix'))
        return _socket(n.outputs, 'Matrix'), _socket(n.outputs, 'Invertible')
    def math_node(key, op, a, b):
        n = node(key, 'ShaderNodeMath', operation=op)
        for identifier, operand in [('Value', a), ('Value_001', b)]:
            socket = _socket(n.inputs, identifier)
            if isinstance(operand, (int, float)):
                socket.default_value = operand
            else:
                link(operand, socket)
        return _socket(n.outputs, 'Value')
    def boolean(key, op, a, b=None):
        n = node(key, 'FunctionNodeBooleanMath', operation=op)
        link(a, _socket(n.inputs, 'Boolean'))
        if b is not None:
            link(b, _socket(n.inputs, 'Boolean_001'))
        return _socket(n.outputs, 'Boolean')

    entry = node('input', 'NodeGroupInput')
    output = node('output', 'NodeGroupOutput')
    self_obj = node('self', 'GeometryNodeSelfObject')
    self_info = node('self_matrix', 'GeometryNodeObjectInfo', transform_space='ORIGINAL')
    link(_socket(self_obj.outputs, 'Self Object'), _socket(self_info.inputs, 'Object'))
    camera_info = node('camera_matrix', 'GeometryNodeObjectInfo', transform_space='ORIGINAL')
    reference_info = node('reference_matrix', 'GeometryNodeObjectInfo', transform_space='ORIGINAL')
    link(_socket(entry.outputs, identifiers['camera']), _socket(camera_info.inputs, 'Object'))
    link(_socket(entry.outputs, identifiers['reference']), _socket(reference_info.inputs, 'Object'))
    camera_model = node('camera_model', 'GeometryNodeCameraInfo')
    link(_socket(entry.outputs, identifiers['camera']), _socket(camera_model.inputs, 'Camera'))
    inv_self, valid_self = invert('inverse_self', _socket(self_info.outputs, 'Transform'))
    inv_camera, valid_camera = invert('inverse_camera', _socket(camera_info.outputs, 'Transform'))
    position = node('position', 'GeometryNodeInputPosition')
    world = transform('local_to_world', _socket(position.outputs, 'Position'), _socket(self_info.outputs, 'Transform'))
    cam_point = transform('world_to_camera', world, inv_camera)
    ref_world_node = node('reference_origin', 'FunctionNodeTransformPoint')
    link(_socket(reference_info.outputs, 'Transform'), _socket(ref_world_node.inputs, 'Transform'))
    ref_camera = transform('reference_to_camera', _socket(ref_world_node.outputs, 'Vector'), inv_camera)
    xyz = node('camera_xyz', 'ShaderNodeSeparateXYZ')
    ref_xyz = node('reference_xyz', 'ShaderNodeSeparateXYZ')
    link(cam_point, _socket(xyz.inputs, 'Vector'))
    link(ref_camera, _socket(ref_xyz.inputs, 'Vector'))
    ratio = math_node('depth_ratio', 'DIVIDE', _socket(xyz.outputs, 'Z'), _socket(ref_xyz.outputs, 'Z'))
    offset = math_node('ratio_minus_one', 'SUBTRACT', ratio, 1.0)
    weight = node('weight', 'GeometryNodeInputNamedAttribute', data_type='FLOAT')
    link(_socket(entry.outputs, identifiers['weight']), _socket(weight.inputs, 'Name'))
    amount = math_node('amount', 'MULTIPLY', _socket(entry.outputs, identifiers['strength']), _socket(weight.outputs, 'Attribute'))
    factor = math_node('factor_offset', 'MULTIPLY', amount, offset)
    factor = math_node('factor', 'ADD', factor, 1.0)
    x = math_node('correct_x', 'MULTIPLY', _socket(xyz.outputs, 'X'), factor)
    y = math_node('correct_y', 'MULTIPLY', _socket(xyz.outputs, 'Y'), factor)
    combined = node('corrected_camera', 'ShaderNodeCombineXYZ')
    link(x, _socket(combined.inputs, 'X'))
    link(y, _socket(combined.inputs, 'Y'))
    link(_socket(xyz.outputs, 'Z'), _socket(combined.inputs, 'Z'))
    new_world = transform('camera_to_world', _socket(combined.outputs, 'Vector'), _socket(camera_info.outputs, 'Transform'))
    new_local = transform('world_to_local', new_world, inv_self)

    predicates = [valid_self, valid_camera, _socket(weight.outputs, 'Exists'),
        boolean('perspective', 'NOT', _socket(camera_model.outputs, 'Is Orthographic')),
        math_node('reference_front', 'LESS_THAN', _socket(ref_xyz.outputs, 'Z'), -_EPSILON),
        math_node('point_front', 'LESS_THAN', _socket(xyz.outputs, 'Z'), -_EPSILON),
        math_node('weight_nonnegative', 'GREATER_THAN', _socket(weight.outputs, 'Attribute'), -1e-8),
        math_node('weight_maximum', 'LESS_THAN', _socket(weight.outputs, 'Attribute'), 1.0000001),
        math_node('strength_nonnegative', 'GREATER_THAN', _socket(entry.outputs, identifiers['strength']), -1e-8),
        math_node('strength_maximum', 'LESS_THAN', _socket(entry.outputs, identifiers['strength']), 1.0000001)]
    valid = predicates[0]
    for i, predicate in enumerate(predicates[1:]):
        valid = boolean('valid_' + str(i), 'AND', valid, predicate)
    selected = boolean('selected', 'AND', valid,
        math_node('positive_amount', 'GREATER_THAN', amount, 0.0))
    set_position = node('set_position', 'GeometryNodeSetPosition')
    link(_socket(entry.outputs, identifiers['geometry']), _socket(set_position.inputs, 'Geometry'))
    link(selected, _socket(set_position.inputs, 'Selection'))
    link(new_local, _socket(set_position.inputs, 'Position'))
    diagnostic = node('diagnostic', 'GeometryNodeStoreNamedAttribute', data_type='BOOLEAN', domain='POINT')
    _socket(diagnostic.inputs, 'Name').default_value = group[_VALID]
    link(_socket(set_position.outputs, 'Geometry'), _socket(diagnostic.inputs, 'Geometry'))
    link(valid, _socket(diagnostic.inputs, 'Value'))
    link(_socket(diagnostic.outputs, 'Geometry'), _socket(output.inputs, identifiers['output']))
    for i, n in enumerate(group.nodes):
        n.location = ((i % 7) * 220, -(i // 7) * 220)
    group[_SIGNATURE] = _signature(group)
    return group, identifiers


def build(target, camera, reference, weight_attribute, *, strength=0.0):
    """Append an isolated managed node group. No mesh data is duplicated/edited."""
    import bpy
    _preflight(target, camera, reference, weight_attribute, strength)
    if any(m.type == 'NODES' and m.node_group and m.node_group.get(PROFILE_KEY) == PROFILE for m in target.modifiers):
        raise CorrectionError('target already has a managed camera correction')
    group = modifier = None
    try:
        owner = uuid.uuid4().hex
        group, identifiers = _build_group(owner)
        modifier = target.modifiers.new('Toon Camera XY Correction', 'NODES')
        modifier.node_group = group
        group[_TARGET] = target
        group[_UID] = modifier.persistent_uid
        configure(target, modifier, camera=camera, reference=reference,
                  weight_attribute=weight_attribute, strength=strength)
        return modifier
    except Exception:
        if modifier is not None:
            target.modifiers.remove(modifier)
        if group is not None and group.users == 0:
            bpy.data.node_groups.remove(group)
        raise


def configure(target, modifier, *, camera, reference, weight_attribute, strength=0.0):
    """Change public instance parameters; reject edited/shared graphs first."""
    group, identifiers = _owned(target, modifier)
    strength, _, _ = _preflight(target, camera, reference, weight_attribute, strength)
    requested = [('camera', camera), ('reference', reference),
                 ('weight', weight_attribute), ('strength', strength)]
    controlled = _animated_paths(target)
    for key, value in requested:
        parameter = _input(modifier, identifiers, key)
        if parameter.value != value and parameter.path_from_id('value') in controlled:
            raise CorrectionError('parameter is animated/driven; edit its native animation explicitly: ' + key)
    for key, value in requested:
        _input(modifier, identifiers, key).value = value
    target.update_tag()
    return dict(profile=PROFILE, owner=group[OWNER], strength=strength,
                diagnostic_attribute=group[_VALID])


def inspect(target, modifier, *, depsgraph=None, evaluate=True):
    """Read current references and actual evaluated diagnostic fields.

    ``status=invalid`` is a rejection, even though nodes safely bypass their
    invalid points. This operation never repairs graph, weight, or constraints.
    """
    group, identifiers = _owned(target, modifier)
    camera = _input(modifier, identifiers, 'camera').value
    reference = _input(modifier, identifiers, 'reference').value
    weight = _input(modifier, identifiers, 'weight').value
    strength = _input(modifier, identifiers, 'strength').value
    report = dict(profile=PROFILE, owner=group[OWNER], status='valid',
        modifier_uid=modifier.persistent_uid,
        strength=float(strength), camera=camera.name if camera else None,
        reference=reference.name if reference else None, weight_attribute=weight,
        diagnostic_attribute=group[_VALID], issues=[],
        modifier_viewport=bool(modifier.show_viewport), modifier_render=bool(modifier.show_render),
        matrix_policy='full_target_matrix; rigid_camera_only',
        invalid_policy='unchanged_points_with_diagnostic; caller_must_reject',
        qualification='experimental_geometry_only')
    report['animated_parameter_paths'] = sorted(path for path in _animated_paths(target)
        if any(path == _input(modifier, identifiers, key).path_from_id('value')
               for key in ('camera', 'reference', 'weight', 'strength')))
    try:
        _, reference_depth, graph = _preflight(target, camera, reference, weight, strength, depsgraph)
        report['reference_depth'] = reference_depth
    except CorrectionError as error:
        report.update(status='invalid', issues=[str(error)])
        return report
    if evaluate:
        evaluated = target.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        try:
            evaluated_weight=mesh.attributes.get(weight)
            if (evaluated_weight is None or evaluated_weight.data_type!='FLOAT'
                    or evaluated_weight.domain!='POINT'):
                report.update(status='invalid',issues=['evaluated weight must remain FLOAT POINT'])
                return report
            values=[d.value for d in evaluated_weight.data]
            if not all(math.isfinite(v) and 0<=v<=1 for v in values):
                report.update(status='invalid',issues=['evaluated weights are outside finite [0,1]'])
                return report
            report['positive_weight_points']=sum(v>0 for v in values)
            attribute = mesh.attributes.get(group[_VALID])
            if attribute is None:
                report.update(status='invalid', issues=['diagnostic attribute absent; modifier disabled or downstream removed it'])
            else:
                invalid = sum(not d.value for d in attribute.data)
                report.update(vertices=len(mesh.vertices), invalid_points=invalid,
                              evaluation=graph.mode)
                if invalid:
                    report.update(status='invalid', issues=['invalid correction fields at %d points' % invalid])
        finally:
            evaluated.to_mesh_clear()
    return report


def validate(target, modifier, *, depsgraph=None):
    """Mandatory per-frame render admission; raises for any invalid field."""
    report = inspect(target, modifier, depsgraph=depsgraph)
    if report['status'] != 'valid':
        raise CorrectionError('; '.join(report['issues']))
    return report
