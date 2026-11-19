"""Small native bpy media graphs; Blender 5.2.1. No scene/geometry/UV edits.

Generated coordinates are object bounding-box coordinates. Smoke is a static
procedural ellipsoid inside closed geometry, not a fluid simulation. Water is a
surface treatment, not depth/shoreline/foam. Single Z + alpha cannot reconstruct
refracted lines or volume transmittance; this module never grants joint admission.
"""
import hashlib
import json
import math
import uuid

SCHEMA = 'toon-media-components.v1'
_META, _SEAL, _OWNER, _ROLE = ('toon_media_' + x for x in ('metadata', 'seal', 'owner', 'role'))
_ID = 'toon_media_node_uuid'
_DEFAULTS = {
    'ALPHA_SHEET': dict(color=[.7, .85, 1., 1.], roughness=.35, alpha=.35),
    'GLASS': dict(color=[1., 1., 1., 1.], roughness=.02, ior=1.45, thin_wall=False,thickness_mode='SPHERE'),
    'WATER': dict(color=[.015, .19, .22, 1.], edge_color=[.09, .43, .46, 1.],
                  roughness=.08, ripple_scale=7., ripple_strength=.12),
    'SMOKE': dict(color=[.65, .69, .74, 1.], density=3., anisotropy=.2,
                  noise_scale=3., threshold=.35),
    'EMISSION': dict(color=[1., .5, .1, 1.], strength=2.),
}
_RANGES = dict(roughness=(0., 1.), alpha=(0., 1.), ior=(1., 3.),
               ripple_scale=(.01, 100.), ripple_strength=(0., .3),
               density=(0., 20.), anisotropy=(-.9, .9), noise_scale=(.01, 100.),
               threshold=(0., .8), strength=(0., 100.))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def defaults(style):
    """Return an independent full settings dictionary for one exact style."""
    if not isinstance(style, str) or style not in _DEFAULTS:
        raise ValueError('Unknown media style')
    return dict(schema=SCHEMA, style=style, **json.loads(_json(_DEFAULTS[style])))


def normalize(settings):
    """Strict partial input -> full settings. Reject unknown keys, bool numerics,
    nonfinite values, RGB colors and color alpha other than exactly 1."""
    if not isinstance(settings, dict):
        raise ValueError('settings must be a dictionary')
    result = defaults(settings.get('style'))
    if set(settings) - set(result) or settings.get('schema', SCHEMA) != SCHEMA:
        raise ValueError('Unknown field or schema')
    for key, value in settings.items():
        if key in ('schema', 'style'):
            continue
        if key in ('color', 'edge_color'):
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise ValueError(key + ' must be RGBA')
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                   not 0. <= x <= 1. or not math.isfinite(x) for x in value) or value[3] != 1.:
                raise ValueError(key + ' requires finite [0,1] channels and alpha 1')
            value = [float(x) for x in value]
        elif key == 'thickness_mode':
            if type(value) is not str or value not in ('SPHERE','SLAB'):
                raise ValueError('thickness_mode must be SPHERE or SLAB')
        elif key == 'thin_wall':
            if type(value) is not bool:
                raise ValueError('thin_wall must be bool')
        else:
            lo, hi = _RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(key + ' must be finite numeric')
            if not lo <= value <= hi or not math.isfinite(value):
                raise ValueError(key + ' outside allowed range')
            value = float(value)
        result[key] = value
    return result


def _profile(style):
    return dict(profile='native-' + style.lower(), coordinates='Generated' if style in ('WATER', 'SMOKE') else 'none',
                alpha_semantics='surface_opacity' if style == 'ALPHA_SHEET' else
                'volume_integrated_not_transmittance' if style == 'SMOKE' else 'coverage_not_transmission',
                depth_semantics='volume_not_in_single_z' if style == 'SMOKE' else
                'surface_z_not_refracted_path' if style in ('GLASS', 'WATER') else 'renderer_surface_z',
                joint_admission=False, required_render_settings={
                    'eevee.use_raytracing': True} if style in ('GLASS', 'WATER') else {})


def _new(mat, role, kind, **options):
    node = mat.node_tree.nodes.new(kind)
    node[_OWNER], node[_ROLE] = mat[_OWNER], role
    node[_ID] = str(uuid.uuid4())
    node.name, node.label = 'Media ' + role, role
    for key, value in options.items():
        setattr(node, key, value)
    return node


def _nodes(mat):
    return {n.get(_ROLE): n for n in mat.node_tree.nodes}


def _graph(mat, style):
    tree = mat.node_tree
    tree.nodes.clear()  # Only a freshly created material is passed here.
    new = lambda role, kind, **kw: _new(mat, role, kind, **kw)
    out = new('output', 'ShaderNodeOutputMaterial', target='ALL')
    shader = new('shader', 'ShaderNodeVolumePrincipled' if style == 'SMOKE' else
                 'ShaderNodeEmission' if style == 'EMISSION' else 'ShaderNodeBsdfPrincipled')
    tree.links.new(shader.outputs[0], out.inputs['Volume' if style == 'SMOKE' else 'Surface'])
    if style not in ('WATER', 'SMOKE'):
        return
    coord = new('coordinates', 'ShaderNodeTexCoord')
    noise = new('noise', 'ShaderNodeTexNoise', noise_dimensions='3D', normalize=True)
    tree.links.new(coord.outputs['Generated'], noise.inputs['Vector'])
    noise.inputs['Detail'].default_value = 2.
    noise.inputs['Roughness'].default_value = .55
    if style == 'WATER':
        wave = new('wave', 'ShaderNodeTexWave', wave_type='BANDS', bands_direction='X', wave_profile='SIN')
        tree.links.new(coord.outputs['Generated'], wave.inputs['Vector'])
        wave.inputs['Distortion'].default_value = 2.
        add = new('ripple_sum', 'ShaderNodeMath', operation='ADD')
        tree.links.new(wave.outputs['Fac'], add.inputs[0])
        tree.links.new(noise.outputs['Fac'], add.inputs[1])
        bump = new('bump', 'ShaderNodeBump', invert=False)
        bump.inputs['Distance'].default_value = .03
        tree.links.new(add.outputs[0], bump.inputs['Height'])
        tree.links.new(bump.outputs['Normal'], shader.inputs['Normal'])
        fresnel = new('fresnel', 'ShaderNodeFresnel')
        fresnel.inputs['IOR'].default_value = 1.333
        tree.links.new(bump.outputs['Normal'], fresnel.inputs['Normal'])
        tint = new('view_tint', 'ShaderNodeMixRGB', blend_type='MIX', use_clamp=False)
        tree.links.new(fresnel.outputs[0], tint.inputs[0])
        tree.links.new(tint.outputs[0], shader.inputs['Base Color'])
    else:
        shader.inputs['Color Attribute'].default_value = ''
        shader.inputs['Density Attribute'].default_value = ''
        center = new('center', 'ShaderNodeVectorMath', operation='SUBTRACT')
        center.inputs[1].default_value = (.5, .5, .5)
        tree.links.new(coord.outputs['Generated'], center.inputs[0])
        length = new('radius', 'ShaderNodeVectorMath', operation='LENGTH')
        tree.links.new(center.outputs['Vector'], length.inputs[0])
        radial = new('radial_scale', 'ShaderNodeMath', operation='MULTIPLY')
        radial.inputs[1].default_value = 2.
        tree.links.new(length.outputs['Value'], radial.inputs[0])
        falloff = new('falloff', 'ShaderNodeMath', operation='SUBTRACT', use_clamp=True)
        falloff.inputs[0].default_value = 1.
        tree.links.new(radial.outputs[0], falloff.inputs[1])
        cut = new('threshold', 'ShaderNodeMath', operation='SUBTRACT', use_clamp=True)
        tree.links.new(noise.outputs['Fac'], cut.inputs[0])
        scale = new('noise_gain', 'ShaderNodeMath', operation='MULTIPLY')
        tree.links.new(cut.outputs[0], scale.inputs[0])
        floor = new('noise_floor', 'ShaderNodeMath', operation='ADD')
        floor.inputs[1].default_value = .1
        tree.links.new(scale.outputs[0], floor.inputs[0])
        mask = new('density_mask', 'ShaderNodeMath', operation='MULTIPLY')
        tree.links.new(falloff.outputs[0], mask.inputs[0])
        tree.links.new(floor.outputs[0], mask.inputs[1])
        density = new('density', 'ShaderNodeMath', operation='MULTIPLY')
        tree.links.new(mask.outputs[0], density.inputs[0])
        tree.links.new(density.outputs[0], shader.inputs['Density'])


def _apply(mat, s):
    n, style = _nodes(mat), s['style']
    shader = n['shader']
    shader.inputs['Color' if style in ('SMOKE', 'EMISSION') else 'Base Color'].default_value = s['color']
    if style == 'EMISSION':
        shader.inputs['Strength'].default_value = s['strength']
    elif style == 'SMOKE':
        shader.inputs['Anisotropy'].default_value = s['anisotropy']
        n['noise'].inputs['Scale'].default_value = s['noise_scale']
        n['threshold'].inputs[1].default_value = s['threshold']
        n['noise_gain'].inputs[1].default_value = .9 / (1. - s['threshold'])
        n['density'].inputs[1].default_value = s['density']
    else:
        shader.inputs['Roughness'].default_value = s['roughness']
        shader.inputs['Alpha'].default_value = s.get('alpha', 1.)
        shader.inputs['Transmission Weight'].default_value = 1. if style in ('GLASS', 'WATER') else 0.
        shader.inputs['IOR'].default_value = s.get('ior', 1.333 if style == 'WATER' else 1.45)
        shader.inputs['Thin Wall'].default_value = s.get('thin_wall', False)
        if style=='GLASS':mat.thickness_mode=s['thickness_mode']
        if style in ('GLASS','WATER'):mat.use_thickness_from_shadow=False
        if style == 'WATER':
            n['view_tint'].inputs[1].default_value = s['color']
            n['view_tint'].inputs[2].default_value = s['edge_color']
            n['wave'].inputs['Scale'].default_value = s['ripple_scale']
            n['noise'].inputs['Scale'].default_value = s['ripple_scale'] * 1.7
            n['bump'].inputs['Strength'].default_value = s['ripple_strength']


_OPTIONS = ('operation', 'use_clamp', 'blend_type', 'invert', 'target', 'is_active_output',
            'noise_dimensions', 'noise_type', 'normalize', 'wave_type', 'wave_profile',
            'bands_direction', 'rings_direction', 'from_instancer', 'distribution',
            'subsurface_method')
_MAT_OPTIONS = ('surface_render_method', 'use_raytrace_refraction', 'use_transparent_shadow',
                'use_transparency_overlap', 'use_backface_culling', 'thickness_mode',
                'use_thickness_from_shadow', 'refraction_depth', 'volume_intersection_method')


def _sockets(sockets):
    result = []
    for socket in sockets:
        value = getattr(socket, 'default_value', None)
        if value is not None and not isinstance(value, (str, bool, int, float)):
            value = list(value)
        result.append([socket.identifier, value])
    return result


def _snapshot(mat):
    rows = []
    for node in mat.node_tree.nodes:
        row = dict(role=node.get(_ROLE), owner=node.get(_OWNER), id=node.get(_ID), type=node.bl_idname,
                   mute=node.mute, inputs=_sockets(node.inputs), outputs=_sockets(node.outputs))
        row['options'] = {x: getattr(node, x) for x in _OPTIONS if hasattr(node, x)}
        if hasattr(node, 'object'):
            row['object'] = None if node.object is None else node.object.name_full
        rows.append(row)
    links = sorted([l.from_node.get(_ROLE, ''), list(l.from_node.outputs).index(l.from_socket),
                    l.to_node.get(_ROLE, ''), list(l.to_node.inputs).index(l.to_socket)] for l in mat.node_tree.links)
    return dict(nodes=sorted(rows, key=lambda x: str(x['role'])), links=links,
                material={x: getattr(mat, x) for x in _MAT_OPTIONS if hasattr(mat, x)})


def _stamp(mat, settings):
    data = dict(schema=SCHEMA, owner=mat[_OWNER], settings=settings, **_profile(settings['style']),
                graph=_snapshot(mat))
    mat[_META], mat[_SEAL] = _json(data), _hash(data)


def build(bpy, settings, name='Toon Media'):
    """Create an unassigned material only; caller owns geometry and render setup."""
    s = normalize(settings)
    if not isinstance(name, str) or not name.strip():
        raise ValueError('name must be nonempty')
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True
        mat[_OWNER] = str(uuid.uuid4())
        mat.surface_render_method = 'BLENDED' if s['style'] == 'ALPHA_SHEET' else 'DITHERED'
        mat.use_raytrace_refraction = s['style'] in ('GLASS', 'WATER')
        mat.use_transparent_shadow = False
        _graph(mat, s['style'])
        _apply(mat, s)
        _stamp(mat, s)
    except Exception:
        bpy.data.materials.remove(mat)
        raise
    return mat


def inspect(material):
    """Reject altered graph, animation or owned metadata; ignore unrelated ID props."""
    settings, issues = None, []
    try:
        data = json.loads(material[_META])
        settings = normalize(data['settings'])
        if data['settings'] != settings or data['schema'] != SCHEMA:
            issues.append('Invalid settings/schema metadata')
        if data['owner'] != material[_OWNER] or str(uuid.UUID(data['owner'])) != data['owner']:
            issues.append('Invalid owner')
        expected_keys = {'schema', 'owner', 'settings', 'graph'} | set(_profile(settings['style']))
        if set(data) != expected_keys or any(data.get(k) != v for k, v in _profile(settings['style']).items()):
            issues.append('Altered profile metadata')
        if _hash(data) != material[_SEAL]:
            issues.append('Altered owned metadata')
        if not material.use_nodes or not material.node_tree or _snapshot(material) != data['graph']:
            issues.append('Altered graph or material options')
        nodes = list(material.node_tree.nodes)
        roles = [n.get(_ROLE) for n in nodes]
        if len(set(roles)) != len(roles) or None in roles or any(n.get(_OWNER) != data['owner'] for n in nodes):
            issues.append('Invalid node ownership')
        ids = [n.get(_ID) for n in nodes]
        if len(set(ids)) != len(ids) or any(str(uuid.UUID(x)) != x for x in ids):
            issues.append('Invalid node UUIDs')
        if material.animation_data or material.node_tree.animation_data:
            issues.append('Animated material is outside editable contract')
    except Exception as exc:
        issues.append('Invalid media material: ' + type(exc).__name__)
    return dict(status='conflict' if issues else 'valid', settings=settings, issues=issues)


def update(material, settings):
    """Same-style settings edit, retaining all node identities; refuse hand edits."""
    report = inspect(material)
    if report['status'] != 'valid':
        return report
    s = normalize(settings)
    if s['style'] != report['settings']['style']:
        raise ValueError('Style changes require a new material')
    _apply(material, s)
    _stamp(material, s)
    return inspect(material)
