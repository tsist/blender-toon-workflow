# SPDX-License-Identifier: GPL-3.0-or-later
"""Static reference-local curved hair band, with native parameter readback.

Matrices are evaluated evidence, never part of the static ownership shape.
There is deliberately no public graph construction/signing or re-sign API.
An unbound build temporarily owns toon_finish_band_reference_id metadata on its
explicit reference EMPTY; callers reclaim it only after the last Group deletion.
"""
import json
import math
import struct
import uuid
from . import finish_nodes as N

_REF = 'toon_finish_band_reference'
_REF_ID = 'toon_finish_band_reference_id'
_DECLARED = 'toon_finish_band_declared'
_DEFAULTS = dict(center=.3, half_width=.07, softness=.025, curvature=.12)
_PREFIX = 'band:'


def defaults():
    return dict(_DEFAULTS)


def normalize(settings):
    N._need(type(settings) is dict and set(settings) == set(_DEFAULTS),
            'Band settings require exactly center, half_width, softness, curvature')
    values = {key: N._number(settings[key], key) for key in _DEFAULTS}
    # RNA stores FloatProperty/Value sockets as float32. Only this exact lower
    # endpoint representation is canonicalized, never an epsilon neighbourhood.
    native_min = struct.unpack('f', struct.pack('f', .0001))[0]
    for key in ('half_width', 'softness'):
        if values[key] == native_min:
            values[key] = .0001
    N._need(-8. <= values['center'] <= 8., 'Band center outside [-8,8]')
    N._need(.0001 <= values['half_width'] <= 8., 'Band half_width outside [.0001,8]')
    N._need(.0001 <= values['softness'] <= values['half_width'],
            'Band softness requires [.0001,half_width]')
    N._need(-4. <= values['curvature'] <= 4., 'Band curvature outside [-4,4]')
    return values


def _active(group):
    N._group(group)
    return group.get(N.PROFILE_KEY) == N.HAIR_PROFILE


def _matrix(matrix):
    rows = [[float(matrix[r][c]) for c in range(4)] for r in range(4)]
    N._need(all(math.isfinite(v) for row in rows for v in row),
            'Band reference matrix must be finite')
    try:
        inverse = matrix.inverted()
    except (ValueError, ZeroDivisionError) as error:
        raise N.FinishNodeError('Band reference matrix must be invertible') from error
    N._need(all(math.isfinite(float(inverse[r][c])) for r in range(4) for c in range(4)),
            'Band reference inverse matrix must be finite')
    return rows


def _check_reference(value, target=None):
    import bpy
    N._need(isinstance(value, bpy.types.Object) and value.type == 'EMPTY'
            and bpy.data.objects.get(value.name) == value
            and not value.library and not value.override_library and value.is_editable
            and value != target, 'Band reference requires an independent local editable EMPTY')
    _matrix(value.matrix_world)
    return value


def reference(group):
    if not _active(group):
        return None
    return _check_reference(group.get(_REF))


def reference_id(group):
    """Persistent reference anchor; retrieve before deleting the owning Group."""
    if not _active(group):
        return None
    return N._owner(group.get(_REF_ID))


def _matching_groups(ref, token):
    import bpy
    return [group for group in bpy.data.node_groups
            if group.get(N.PROFILE_KEY) == N.HAIR_PROFILE
            and group.get(_REF) == ref and group.get(_REF_ID) == token]


def _unique_reference(ref, token):
    import bpy
    N._need(ref.get(_REF_ID) == token, 'Band reference persistent identity changed')
    def same_uuid(value):
        try:
            return type(value) is str and uuid.UUID(value) == uuid.UUID(token)
        except (ValueError, AttributeError):
            return False
    N._need(not any(obj != ref and same_uuid(obj.get(_REF_ID)) for obj in bpy.data.objects),
            'Band reference UUID is duplicated on another Object')


def _acquire_reference(ref):
    """An unbound build temporarily owns metadata on its explicit reference."""
    if _REF_ID not in ref:
        token = str(uuid.uuid4())
        ref[_REF_ID] = token
        return token, True
    token = N._owner(ref[_REF_ID])
    _unique_reference(ref, token)
    N._need(bool(_matching_groups(ref, token)),
            'Band reference UUID is foreign or stale; no automatic adoption')
    return token, False


def _cleanup_reference(ref, token):
    """Call after actual Group deletion; never replace or remove changed metadata."""
    import bpy
    token = N._owner(token)
    N._need(isinstance(ref, bpy.types.Object) and bpy.data.objects.get(ref.name) == ref
            and ref.type == 'EMPTY' and not ref.library and not ref.override_library
            and ref.is_editable, 'Band cleanup requires its original local editable EMPTY')
    N._need(ref.get(_REF_ID) == token,
            'Band reference metadata changed; cleanup refused')
    if any(group.get(_REF) == ref or group.get(_REF_ID) == token
           for group in bpy.data.node_groups):
        return False
    del ref[_REF_ID]
    return True


def _parameter_sockets(group):
    values = {}
    for key in _DEFAULTS:
        node = N._node(group, _PREFIX + 'param:' + key)
        N._need(node.bl_idname == 'ShaderNodeValue' and not node.mute,
                'Band parameter node changed')
        values[key] = N._socket(node.outputs, 'Value')
    return values


def parameters(group):
    if not _active(group):
        return None
    values = {key: float(socket.default_value) for key, socket in _parameter_sockets(group).items()}
    # Float32 conversion of .0001 can round slightly below its declared decimal limit.
    # Legal stored values are accepted only if they equal a representable boundary.
    return _native_normalize(values)


def _native_normalize(values):
    normalize(values)  # Validate public input semantics, retain actual native values.
    return values


def declared(group):
    if not _active(group):
        return None
    try:
        values = json.loads(group[_DECLARED])
    except (KeyError, TypeError, ValueError) as error:
        raise N.FinishNodeError('Band parameter declaration changed') from error
    N._need(type(values) is dict and set(values) == set(_DEFAULTS), 'Band declaration fields changed')
    values = {key: N._number(values[key], key) for key in _DEFAULTS}
    return _native_normalize(values)


def _spec():
    # source references are (role, output identifier); numbers are static constants.
    p = lambda key: (_PREFIX + 'param:' + key, 'Value')
    s = lambda key: (_PREFIX + key, 'Value')
    return [
        ('x_squared', 'MULTIPLY', (_PREFIX+'axes','X'), (_PREFIX+'axes','X')),
        ('curve', 'MULTIPLY', s('x_squared'), p('curvature')),
        ('z_center', 'SUBTRACT', (_PREFIX+'axes','Z'), p('center')),
        ('offset', 'SUBTRACT', s('z_center'), s('curve')),
        ('distance', 'ABSOLUTE', s('offset'), None),
        ('edge', 'SUBTRACT', p('half_width'), s('distance')),
        ('width', 'DIVIDE', s('edge'), p('softness')),
        ('lower', 'MAXIMUM', s('width'), 0.),
        ('upper', 'MINIMUM', s('lower'), 1.),
        ('squared', 'MULTIPLY', s('upper'), s('upper')),
        ('double', 'MULTIPLY', s('upper'), 2.),
        ('polynomial', 'SUBTRACT', 3., s('double')),
        ('smooth', 'MULTIPLY', s('squared'), s('polynomial'))]


def _expected_links():
    edges = [(_PREFIX+'coordinates','Object',_PREFIX+'axes','Vector'),
             (_PREFIX+'smooth','Value','highlight_lit','Value')]
    for role, operation, a, b in _spec():
        for identifier, source in (('Value', a), ('Value_001', b)):
            if isinstance(source, tuple):
                edges.append((source[0],source[1],_PREFIX+role,identifier))
    return sorted(edges)


def validate(group):
    if not _active(group):
        return None
    ref = reference(group)
    _unique_reference(ref, reference_id(group))
    tex = N._node(group, _PREFIX+'coordinates')
    axes = N._node(group, _PREFIX+'axes')
    N._need(tex.bl_idname == 'ShaderNodeTexCoord' and tex.object == ref
            and not tex.from_instancer and not tex.mute, 'Band coordinate reference changed')
    N._need(axes.bl_idname == 'ShaderNodeSeparateXYZ' and not axes.mute,
            'Band local-axis separation changed')
    actual, expected = parameters(group), declared(group)
    N._need(actual == expected, 'Band native parameters differ from declaration')
    roles = {_PREFIX+'coordinates', _PREFIX+'axes'} | {_PREFIX+'param:'+key for key in _DEFAULTS}
    for role, operation, a, b in _spec():
        identity = _PREFIX+role
        roles.add(identity)
        node = N._node(group, identity)
        N._need(node.bl_idname == 'ShaderNodeMath' and node.operation == operation
                and not node.use_clamp and not node.mute, 'Band math operation changed: '+role)
        for identifier, source in (('Value',a), ('Value_001',b)):
            socket = N._socket(node.inputs, identifier)
            if isinstance(source, (int,float)):
                N._need(not socket.is_linked and socket.default_value == source,
                        'Band fixed constant changed: '+role)
            elif source is None:
                N._need(not socket.is_linked and socket.default_value == .5,
                        'Band unused input changed: '+role)
    nodes = [n for n in group.nodes if str(n.get(N.NODE,'')).startswith(_PREFIX)]
    N._need(len(nodes) == len(roles) and {n.get(N.NODE) for n in nodes} == roles
            and all(n.get(N.OWNER) == group.get(N.OWNER) for n in nodes),
            'Band node inventory or ownership changed')
    links = sorted((link.from_node.get(N.NODE),link.from_socket.identifier,
                    link.to_node.get(N.NODE),link.to_socket.identifier) for link in group.links
                   if link.from_node in nodes or link.to_node in nodes
                   or (link.to_node == N._node(group,'highlight_lit') and link.to_socket.identifier == 'Value'))
    N._need(links == _expected_links(), 'Band local-coordinate formula wiring changed')
    return dict(reference=ref.name_full, settings=actual)


def write_parameters(group, settings):
    if not _active(group):
        return None
    desired = normalize(settings)
    validate(group)
    sockets = _parameter_sockets(group)
    old = {key: float(socket.default_value) for key,socket in sockets.items()}
    old_declaration = group[_DECLARED]
    try:
        for key, value in desired.items():
            sockets[key].default_value = value
        actual = parameters(group)
        N._need(all(N._close(actual[key], desired[key]) for key in desired),
                'Band native parameter readback differs')
        group[_DECLARED] = N._json(actual)
        validate(group)
        return actual
    except Exception:
        for key, value in old.items():
            sockets[key].default_value = value
        group[_DECLARED] = old_declaration
        raise


def evaluate(group, depsgraph):
    config = validate(group)
    if config is None:
        return None
    N._need(depsgraph is not None and depsgraph.mode == 'RENDER',
            'Band evaluation requires a RENDER depsgraph')
    matrix = reference(group).evaluated_get(depsgraph).matrix_world
    return dict(config, matrix_world=_matrix(matrix))


def _build(group, settings, ref):
    """Internal construction only, called once by finish_nodes.build."""
    group[_REF] = ref
    def node(role, kind, **properties):
        value = group.nodes.new(kind)
        value.name = 'ToonFinish/'+_PREFIX+role
        value.label = _PREFIX+role
        value[N.NODE], value[N.OWNER] = _PREFIX+role, group[N.OWNER]
        for name, prop in properties.items():
            setattr(value,name,prop)
        return value
    node('coordinates','ShaderNodeTexCoord',object=ref,from_instancer=False)
    node('axes','ShaderNodeSeparateXYZ')
    for key in _DEFAULTS:
        value=node('param:'+key,'ShaderNodeValue')
        N._socket(value.outputs,'Value').default_value=settings[key]
    for role, operation, a, b in _spec():
        value=node(role,'ShaderNodeMath',operation=operation,use_clamp=False)
        for identifier,source in (('Value',a),('Value_001',b)):
            if type(source) in (int,float):N._socket(value.inputs,identifier).default_value=source
    destination=N._socket(N._node(group,'highlight_lit').inputs,'Value')
    N._need(len(destination.links)==1 and destination.links[0].from_node==N._node(group,'highlight_smooth'),
            'Band original highlight seam changed')
    group.links.remove(destination.links[0])
    for source,output,target,input_ in _expected_links():
        group.links.new(N._socket(N._node(group,source).outputs,output),
                        N._socket(N._node(group,target).inputs,input_))
    group[_DECLARED]=N._json(parameters(group))
    # finish_nodes.build validates the complete final graph at its structure boundary.


def _structure(group):
    config=validate(group)
    if config is None:return None
    tex=N._node(group,_PREFIX+'coordinates')
    return dict(reference=N._plain(reference(group)), reference_id=reference_id(group),
                coordinate_object=N._plain(tex.object),
                from_instancer=tex.from_instancer, parameters='STATIC_BAND_PARAMETERS',
                coordinate_space='REFERENCE_LOCAL_X_Z.v1')
