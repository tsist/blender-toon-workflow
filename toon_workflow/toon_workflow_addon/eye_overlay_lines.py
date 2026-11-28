# SPDX-License-Identifier: GPL-3.0-or-later
"""Finite, non-refractive eye layers for the joint renderer.

The source capture is never changed. A detached line capture omits eye-layer
triangles from opaque visibility, while source-edge provenance suppresses the
layers' own strokes. A private Renderer constructor injects the same analytic
UV transfer into COLOR fragments, leaving the shipped Pencil backend intact.

This is deliberately not a general transparent-material interpreter: at most
eight declared material layers and 16384 triangles are admitted. Render-level
subdivision is retained; triangles are uploaded in a finite RGBA32F texture,
not expanded into shader constants. Each material
contributes its nearest positive ray hit once, even at triangulation seams.
"""
import copy
import hashlib
import json
import math
import types

from .backend import pencil

SCHEMA = 'toon-eye-overlay-line-snapshot.v1'
MAX_MATERIALS = 8
MAX_TRIANGLES = 16384
_SETTING_KEYS = {'tint', 'opacity', 'uv_name', 'center', 'radius', 'feather', 'color'}


def _number(value, label, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(label+' must be a finite scalar')
    value = float(value)
    if low is not None and value < low or high is not None and value > high:
        raise ValueError(label+' is outside the managed eye-layer range')
    return value


def _vector(value, size, label, low=None, high=None):
    if type(value) not in (list, tuple) or len(value) != size:
        raise ValueError(label+' requires '+str(size)+' components')
    return [_number(v, label, low, high) for v in value]


def _settings(kind, settings):
    if kind not in {'SHADOW', 'GLINT'} or type(settings) is not dict:
        raise ValueError('A declared SHADOW or GLINT eye layer is required')
    if set(settings)-_SETTING_KEYS:
        raise ValueError('Unknown analytic eye-layer settings')
    result = dict(opacity=_number(settings.get('opacity'), 'Eye opacity', 0., 1.))
    if kind == 'SHADOW':
        result['tint'] = _vector(settings.get('tint'), 3, 'Eye transmission tint', 0., 1.)
    else:
        uv_name = settings.get('uv_name')
        if type(uv_name) is not str or not uv_name:
            raise ValueError('Glint requires an explicit UV layer')
        result['uv_name'] = uv_name
        result['center'] = _vector(settings.get('center'), 2, 'Glint UV center')
        radius = settings.get('radius')
        if type(radius) in (int, float):
            radius = [radius, radius]
        result['radius'] = _vector(radius, 2, 'Glint UV radius')
        if min(result['radius']) <= 0.:
            raise ValueError('Glint UV radius must be positive')
        result['feather'] = _number(settings.get('feather'), 'Glint UV feather', 0., 1.)
        result['color'] = _vector(settings.get('color'), 3, 'Glint linear color', 0., 1.)
    return result


def _triangle_incidence(mesh, triangles):
    """Rebuild triangle-side support without altering source polygon/edge IDs."""
    import numpy as np
    incidence = {}
    for ti, triangle in enumerate(triangles):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            key = tuple(sorted((int(triangle[a]), int(triangle[b]))))
            incidence.setdefault(key, []).append(ti)
    supports = np.full((len(mesh.edges), 2), -1, np.int32)
    counts = np.zeros(len(mesh.edges), np.int64)
    extra = []
    for ei, edge in enumerate(mesh.edges):
        hits = incidence.get(tuple(sorted(map(int, edge))), ())
        counts[ei] = len(hits)
        if hits:
            supports[ei] = (hits[0], hits[-1])
        if len(hits) > 2:
            extra.append((ei, np.asarray(hits, np.int32)))
    return supports, counts, extra


def prepare(geometry, rows):
    """Return ``(detached_line_geometry, JSON_snapshot)`` for explicit bindings.

    Each row has exactly target/material/slot/kind/settings. target/material
    are captured Main names; the caller must validate the live material graph
    and its owner binding before supplying these rows. No naming heuristics
    infer an eye layer. Polygon, loop, UV and source-edge indices stay intact;
    render_steps suppresses every edge incident to a removed eye polygon.
    """
    import numpy as np
    if type(rows) is not list or len(rows) > 64:
        raise ValueError('Managed eye bindings must be a list of at most 64 rows')
    by_name = {}
    for mi, mesh in enumerate(geometry.meshes):
        by_name.setdefault(mesh.name, []).append(mi)
    materials = []
    material_ids = {}
    bindings = set()
    removals = {}
    triangles = []
    for row in rows:
        if type(row) is not dict or set(row) != {'target', 'material', 'slot', 'kind', 'settings'}:
            raise ValueError('Managed eye binding fields differ')
        target, material, slot = (row[k] for k in ('target', 'material', 'slot'))
        if (type(target) is not str or type(material) is not str or not target or not material
                or type(slot) is not int or slot < 0):
            raise ValueError('Explicit eye object, material and nonnegative slot required')
        matches = by_name.get(target, ())
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous captured eye object: '+target)
        mi = matches[0]
        mesh = geometry.meshes[mi]
        if slot >= len(mesh.material_names) or mesh.material_names[slot] != material:
            raise ValueError('Eye material slot differs from the declared capture')
        key = (target, slot)
        if key in bindings:
            raise ValueError('Duplicate managed eye binding')
        bindings.add(key)
        kind = row['kind']
        settings = _settings(kind, row['settings'])
        descriptor = dict(material=material, kind=kind, settings=settings)
        if material in material_ids:
            mid = material_ids[material]
            if materials[mid] != descriptor:
                raise ValueError('One eye material has conflicting analytic descriptors')
        else:
            mid = len(materials)
            if mid >= MAX_MATERIALS:
                raise ValueError('Managed eye material limit exceeded')
            material_ids[material] = mid
            materials.append(descriptor)
        face_mask = np.asarray(mesh.face_material) == slot
        if not np.any(face_mask):
            raise ValueError('Managed eye material slot has no captured faces')
        removals.setdefault(mi, np.zeros(len(mesh.face_material), bool))[:] |= face_mask
        tids = np.flatnonzero(face_mask[mesh.triangle_polygons])
        if not len(tids):
            raise ValueError('Managed eye binding has no captured triangles')
        if len(triangles)+len(tids) > MAX_TRIANGLES:
            raise ValueError('Managed eye triangle limit exceeded')
        uv = None
        if kind == 'GLINT':
            matches = [layer for layer in mesh.uv_layers if layer['name'] == settings['uv_name']]
            if len(matches) != 1:
                raise ValueError('Missing or ambiguous glint UV layer')
            uv = np.asarray(matches[0]['uv'], np.float64)
        for ti in tids:
            points = np.asarray(mesh.vertices[mesh.triangles[ti]], np.float64)
            first, second = points[1]-points[0], points[2]-points[0]
            area = np.linalg.norm(np.cross(first, second))
            scale = np.linalg.norm(first)*np.linalg.norm(second)
            if (not np.isfinite(points).all() or not math.isfinite(float(area))
                    or not scale > 0. or area <= 1.e-12*scale):
                raise ValueError('Degenerate/nonfinite managed eye triangle')
            corners = np.zeros((3, 2), np.float64) if uv is None else uv[mesh.triangle_loops[ti]]
            if not np.isfinite(corners).all():
                raise ValueError('Nonfinite managed eye triangle UV')
            if uv is not None:
                a, b = corners[1]-corners[0], corners[2]-corners[0]
                determinant = abs(float(a[0]*b[1]-a[1]*b[0]))
                if determinant <= 1.e-12*np.linalg.norm(a)*np.linalg.norm(b):
                    raise ValueError('Degenerate managed glint UV triangle')
            triangles.append(dict(material_index=mid, world=points.tolist(), uv=corners.tolist()))
    result = copy.copy(geometry)
    result.meshes = []
    for cache in ('intersection_sources', 'object_partition'):
        if hasattr(result, cache):
            delattr(result, cache)
    suppressed = []
    offset = 1
    for mi, source in enumerate(geometry.meshes):
        mesh = copy.copy(source)
        removed = removals.get(mi)
        edge_ids = []
        if removed is not None:
            # Shared eye/receiver boundary edges are also not outline sources.
            edge_mask = ((source.counts > 0) & removed[source.faces0]
                         | (source.counts > 1) & removed[source.faces1])
            for ei, faces in source.extra_faces:
                if np.any(removed[faces]):
                    edge_mask[ei] = True
            edge_ids = np.flatnonzero(edge_mask).tolist()
            keep = ~removed[source.triangle_polygons]
            for name in ('triangles', 'triangle_loops', 'triangle_polygons',
                         'triangle_normals', 'triangle_centers'):
                setattr(mesh, name, getattr(source, name)[keep].copy())
            mesh.triangle_supports, mesh.triangle_edge_count, mesh.extra_triangles = _triangle_incidence(mesh, mesh.triangles)
            mesh.depth_batch = None
        mesh.face_offset = offset
        offset += len(mesh.triangles)
        result.meshes.append(mesh)
        suppressed.append(dict(mesh_index=mi, target=source.name, source_edges=edge_ids))
    result.triangle_count = sum(len(mesh.triangles) for mesh in result.meshes)
    if offset >= 2**24:
        raise ValueError('Managed eye line triangle identity exceeds float32 range')
    snapshot = dict(schema=SCHEMA, materials=materials, triangles=triangles,
                    suppressed_edges=suppressed,
                    policy='nearest positive ray hit per material; COLOR transfer only; raw DEPTH',
                    source_geometry_changed=False)
    snapshot['sha256'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':'),
                                                   allow_nan=False).encode('utf8')).hexdigest()
    return result, snapshot


def _literal(value):
    # Avoid GLSL integer literals in float constructors; no user text is emitted.
    value = _number(value, 'Eye shader constant')
    text = format(value, '.9g')
    return text if '.' in text or 'e' in text.lower() else text+'.0'


def _vec(value, size):
    return 'vec%d(%s)' % (size, ','.join(_literal(v) for v in value))


def _glsl(snapshot):
    if (type(snapshot) is not dict or snapshot.get('schema') != SCHEMA
            or type(snapshot.get('materials')) is not list
            or type(snapshot.get('triangles')) is not list
            or not 1 <= len(snapshot['materials']) <= MAX_MATERIALS
            or not 1 <= len(snapshot['triangles']) <= MAX_TRIANGLES):
        raise ValueError('Invalid finite managed eye snapshot')
    materials = snapshot['materials']
    for row in materials:
        if _settings(row['kind'], row['settings']) != row['settings']:
            raise ValueError('Noncanonical managed eye settings')
    points = []
    for row in snapshot['triangles']:
        if (type(row) is not dict or set(row) != {'material_index', 'world', 'uv'}
                or type(row['material_index']) is not int
                or not 0 <= row['material_index'] < len(materials)):
            raise ValueError('Invalid managed eye triangle material')
        if len(row['world']) != 3 or len(row['uv']) != 3:
            raise ValueError('Invalid managed eye triangle shape')
        points.append([_vector(p, 3, 'Eye world corner') for p in row['world']])
        [_vector(p, 2, 'Eye UV corner') for p in row['uv']]
    count, nmat = len(points), len(materials)
    arrays = ['const int managedEyeTriangleCount=%d;' % count,
              'const int managedEyeMaterialCount=%d;' % nmat]
    # Only the global optical bound is embedded. The full evaluated triangle
    # and UV data remain in the owning texture, avoiding shader-code growth.
    lo = [min(p[j] for triangle in points for p in triangle)-1.e-6 for j in range(3)]
    hi = [max(p[j] for triangle in points for p in triangle)+1.e-6 for j in range(3)]
    arrays += ['const vec3 managedEyeBoundsMin='+_vec(lo, 3)+';',
               'const vec3 managedEyeBoundsMax='+_vec(hi, 3)+';']
    transfers = []
    for mid, row in enumerate(materials):
        settings = row['settings']
        if row['kind'] == 'SHADOW':
            code = 'rgb*=vec3(1.0)-%s*(vec3(1.0)-%s);' % (
                _literal(settings['opacity']), _vec(settings['tint'], 3))
        else:
            code = 'float radial=length((uv-%s)/%s);' % (_vec(settings['center'], 2), _vec(settings['radius'], 2))
            if settings['feather'] == 0.:
                code += 'float mask=radial<1.0?1.0:0.0;'
            else:
                code += 'float mask=1.0-smoothstep(%s,1.0,radial);' % _literal(1.-settings['feather'])
            code += 'rgb=mix(rgb,%s,mask*%s);' % (_vec(settings['color'], 3), _literal(settings['opacity']))
        transfers.append(('if' if mid == 0 else 'else if')+'(material==%d){%s}' % (mid, code))
    return '\n'.join(arrays)+'''
vec4 managedEyeTriangleRow(int triangle,int row){
    int index=triangle*6+row;
    return texelFetch(managedEyeTriangles,ivec2(index%2048,index/2048),0);
}
vec3 managedEyeTransfer(vec3 rgb,int material,vec2 uv){
'''+''.join(transfers)+'''
    return rgb;
}
vec3 managedEye(vec3 rgb,vec3 linePoint){
    // The line's optical position is its source axial plane, sampled at this
    // actual fragment. Coverage/visibility remain the original Pencil rules.
    vec2 ndc=(gl_FragCoord.xy+strokeCoverage.zw)/size*2.0-1.0;
    vec4 nearH=inverseMvp*vec4(ndc,-1.0,1.0);
    if(abs(nearH.w)<1e-20)return rgb;
    vec3 nearPoint=nearH.xyz/nearH.w;
    vec3 forward=normalize(rayForward.xyz);
    vec3 endPoint,direction;
    float limit;
    if(rayCamera.w>0.0){
        vec3 intoScene=normalize(nearPoint-rayCamera.xyz);
        float denominator=dot(intoScene,forward);
        float axial=dot(linePoint-rayCamera.xyz,forward);
        if(denominator<=1e-8 || axial<=0.0)return rgb;
        endPoint=rayCamera.xyz+intoScene*(axial/denominator);
        direction=-intoScene;
        limit=length(endPoint-rayCamera.xyz);
    }else{
        vec3 delta=nearPoint-linePoint;
        endPoint=linePoint+delta-forward*dot(delta,forward);
        direction=-forward;
        limit=dot(endPoint-nearPoint,forward);
    }
    if(limit<=1e-6)return rgb;
    if(!boxHit(endPoint,direction,managedEyeBoundsMin,managedEyeBoundsMax,limit))return rgb;
    float hitDistance[8];
    vec2 hitUV[8];
    for(int material=0;material<8;material++){
        hitDistance[material]=1e30;hitUV[material]=vec2(0.0);
    }
    for(int triangle=0;triangle<managedEyeTriangleCount;triangle++){
        vec4 first=managedEyeTriangleRow(triangle,0);
        vec3 a=first.xyz;
        vec3 e1=managedEyeTriangleRow(triangle,1).xyz-a;
        vec3 e2=managedEyeTriangleRow(triangle,2).xyz-a;
        vec3 p=cross(direction,e2);
        float determinant=dot(e1,p);
        if(abs(determinant)<1e-20)continue;
        vec3 offset=endPoint-a;
        float u=dot(offset,p)/determinant;
        if(u< -1e-6 || u>1.000001)continue;
        vec3 q=cross(offset,e1);
        float v=dot(direction,q)/determinant;
        if(v< -1e-6 || u+v>1.000001)continue;
        float distance=dot(e2,q)/determinant;
        int material=int(first.w);
        // One thin layer per material: shared triangle edges cannot double it.
        if(distance>1e-6 && distance<limit-1e-6 && distance<hitDistance[material]){
            hitDistance[material]=distance;
            hitUV[material]=managedEyeTriangleRow(triangle,3).xy*(1.0-u-v)
                +managedEyeTriangleRow(triangle,4).xy*u+managedEyeTriangleRow(triangle,5).xy*v;
        }
    }
    // Starting at the line point walks physical far-to-near toward the camera.
    for(int ordinal=0;ordinal<managedEyeMaterialCount;ordinal++){
        int closest=-1;float best=1e29;
        for(int material=0;material<managedEyeMaterialCount;material++){
            if(hitDistance[material]>0.0 && hitDistance[material]<best){
                closest=material;best=hitDistance[material];
            }
        }
        if(closest<0)break;
        rgb=managedEyeTransfer(rgb,closest,hitUV[closest]);
        hitDistance[closest]=-1.0;
    }
    return rgb;
}
'''


def _triangle_rows(snapshot):
    """Six RGBA32F rows per triangle; Renderer owns the padded 2048-wide table."""
    import numpy as np
    count = len(snapshot['triangles'])
    if not 1 <= count <= MAX_TRIANGLES:
        raise ValueError('Invalid managed eye triangle texture size')
    values = np.zeros((count, 6, 4), np.float32)
    for index, triangle in enumerate(snapshot['triangles']):
        values[index, :3, :3] = triangle['world']
        values[index, 0, 3] = triangle['material_index']
        values[index, 3:, :2] = triangle['uv']
    if not np.isfinite(values).all():
        raise ValueError('Managed eye triangle data exceed finite GPU float32')
    return values.reshape(-1, 4)


def _source_layers(layers, snapshot):
    """Mask aligned primitive arrays, retaining source/phase tables unchanged."""
    import numpy as np
    if layers is None:
        raise ValueError('Managed eye lines require explicit source-provenance layers')
    arrays = pencil('render_elements').PRIMITIVE_ARRAYS
    suppressed = {row['mesh_index']: np.asarray(row['source_edges'], np.int64)
                  for row in snapshot['suppressed_edges'] if row['source_edges']}
    result = []
    for layer in layers:
        count = len(layer['world'])
        if not count:
            result.append(layer)
            continue
        for name in ('source_mesh_indices', 'source_indices', 'source_kinds'):
            value = layer.get(name)
            if not isinstance(value, np.ndarray) or value.shape != (count,) or value.dtype.kind not in 'iu':
                raise ValueError('Managed eye lines require integer source provenance: '+name)
        keep = np.ones(count, bool)
        for mi, edges in suppressed.items():
            keep &= ~((layer['source_mesh_indices'] == mi) & np.isin(layer['source_kinds'],[0,1,2,3,4,6,7])
                      & np.isin(layer['source_indices'], edges))
        if bool(np.all(keep)):
            result.append(layer)
            continue
        selected = layer.copy()
        for name in arrays:
            if name not in layer:
                continue
            if len(layer[name]) != count:
                raise ValueError('Managed eye primitive array alignment differs: '+name)
            selected[name] = layer[name][keep]
        result.append(selected)
    return result


def layer_builder(snapshot):
    """Filter source edges before surface brush sampling, on a private copy.

    PRESET's UV/color brushes require a supporting opaque face. Eye-only edges
    deliberately have none after the visibility capture drops transparent
    triangles. Suppress these primitives before those brushes consume them;
    retaining original source-edge IDs protects all parent topology contracts.
    The trusted, pinned backend function and its module globals stay intact.
    """
    import inspect
    strokes=pencil('strokes')
    source=inspect.getsource(strokes.build_layers_steps)
    seam='    layers=line_functions.apply(geometry,plan,layers)'
    if source.count(seam)!=1:
        raise ValueError('Pencil layer-builder seam changed; eye adapter needs review')
    source=source.replace(seam,'    layers=_eye_filter(layers,_eye_snapshot)\n'+seam)
    namespace=dict(strokes.__dict__,_eye_filter=_source_layers,_eye_snapshot=snapshot)
    exec(compile(source,'<trusted-toon-eye-layer-builder>','exec'),namespace)
    return namespace['build_layers_steps']


def renderer(snapshot, *, base_renderer=None):
    """Create a private optical COLOR renderer; never patch backend globals."""
    drawing = pencil('drawing')
    base_renderer = drawing.Renderer if base_renderer is None else base_renderer
    source = _glsl(snapshot)
    # This existing static uploader creates an owning RGBA32F texture with
    # width 2048. No Pencil global state or source data is modified.
    texture = drawing.Renderer._alpha_texture(_triangle_rows(snapshot))
    original_shader = base_renderer.__init__.__globals__['_shader']
    marker = 'fragColor=vec4(rgb*alpha,alpha);'
    injected = [0]

    def shader(*args, **kwargs):
        args = list(args)
        if len(args) >= 4 and marker in args[3]:
            fragment = args[3]
            if fragment.count(marker) != 1 or fragment.count('void main(){') != 1:
                raise ValueError('Pencil line fragment shape changed; eye adapter needs review')
            fragment = fragment.replace('void main(){', source+'\nvoid main(){', 1)
            args[3] = fragment.replace(marker,
                'if(depthRange.z<0.5){rgb=managedEye(rgb,point);}\n'+marker, 1)
            if len(args) > 5:
                samplers = list(args[5])
                if samplers not in (['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels'], ['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels','toonInkUV']):
                    raise ValueError('Pencil line sampler layout changed; eye adapter needs review')
                args[5] = samplers+['managedEyeTriangles']
            else:
                samplers = list(kwargs.get('samplers', ()))
                if samplers not in (['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels'], ['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels','toonInkUV']):
                    raise ValueError('Pencil line sampler layout changed; eye adapter needs review')
                kwargs['samplers'] = samplers+['managedEyeTriangles']
            injected[0] += 1
        return original_shader(*args, **kwargs)

    original = base_renderer.__init__
    globals_copy = dict(original.__globals__)
    globals_copy['_shader'] = shader
    initialize = types.FunctionType(original.__code__, globals_copy, original.__name__,
                                    original.__defaults__, original.__closure__)

    original_render = base_renderer.render_steps
    render_globals = dict(original_render.__globals__)
    original_batch = render_globals['_line_batch']

    def line_batch(real_shader, *args, **kwargs):
        # A real GPUShader must reach batch_for_shader. Bind its private table
        # only after binding that actual shader, immediately before batching.
        real_shader.bind()
        real_shader.uniform_sampler('managedEyeTriangles', texture)
        return original_batch(real_shader, *args, **kwargs)

    render_globals['_line_batch'] = line_batch
    render_original = types.FunctionType(original_render.__code__, render_globals,
        original_render.__name__, original_render.__defaults__, original_render.__closure__)
    render_original.__kwdefaults__ = original_render.__kwdefaults__

    def render_steps(self, geometry, matrix, projected, settings, width, height, scale, supports,
                     *, world=None, layers=None, depth=None):
        selected = _source_layers(layers, snapshot)
        yield from render_original(self, geometry, matrix, projected, settings,
            width, height, scale, supports, world=world, layers=selected, depth=depth)

    cls = type('ManagedEyeRenderer', (base_renderer,),
               {'__init__': initialize, 'render_steps': render_steps})
    result = cls()
    if injected[0] != 1:
        raise ValueError('Expected exactly one managed eye COLOR fragment injection')
    result._managed_eye_triangle_texture = texture
    return result
