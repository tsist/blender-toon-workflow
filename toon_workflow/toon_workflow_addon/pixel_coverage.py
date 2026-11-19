# SPDX-License-Identifier: GPL-3.0-or-later
"""Mother-owned screen-pixel footprint, without changing the Pencil backend.

The circular-source distance gets a normalized one-pixel box cross-section.
Long straight bands conserve Alpha-equivalent width, including widths below
one pixel. Capsule endpoints are an autonomous distance approximation, not an
exact two-dimensional area integral or a commercial Pencil coverage claim.
"""
import math
import types
import inspect
import textwrap

PROFILE = 'pixel_box.v1'
_BACKEND_PROFILES = ('round_capsule.v1', 'extent_falloff.v1')
_MARKER = 'if(strokeCoverage.x>0.5){\n                    // Outer-extent diameter:'
_REPLACEMENT = '''if(strokeCoverage.x>1.5){
                    float footprint=strokeCoverage.y;
                    float halfFootprint=footprint*0.5;
                    coverage=clamp((radius+halfFootprint-distance)/footprint,0.0,1.0)
                            -clamp((halfFootprint-radius-distance)/footprint,0.0,1.0);
                    // Visibility cuts truncate the original stroke; they do
                    // not create new circular caps. Integrate each artificial
                    // longitudinal boundary over the same pixel footprint.
                    if(intervalClip.x>0.5 || intervalClip.y>0.5){
                        vec2 runDelta=intervalBounds.zw-intervalBounds.xy;
                        float runLength=length(runDelta);
                        if(runLength==0.0){coverage=0.0;}
                        else{
                            float along=dot(samplePosition-intervalBounds.xy,runDelta)/runLength;
                            float firstBox=clamp((along+halfFootprint)/footprint,0.0,1.0);
                            float lastBox=clamp((along-runLength+halfFootprint)/footprint,0.0,1.0);
                            float axial=intervalClip.x>0.5 && intervalClip.y>0.5
                                ?firstBox-lastBox:intervalClip.x>0.5?firstBox:1.0-lastBox;
                            coverage*=axial;
                        }
                    }
                }else if(strokeCoverage.x>0.5){
                    // Outer-extent diameter:'''


def footprint(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value != 1.0:
        raise ValueError('Pixel box uses a fixed one-output-pixel footprint')
    return float(value)


def validate_brushes(plan, group_ids):
    """The new distance footprint is qualified for circular simple brushes."""
    aliases = plan['raw']['request'].get('boundary_aliases', []) + plan['raw']['request'].get('intersection_aliases', [])
    active = {row['brush_id'] for row in aliases if row['group'] in group_ids}
    effects = plan['raw']['request'].get('brush_effects', {}).get('brushes', {})
    for actual in active:
        row = effects.get(actual)
        if row is None:
            continue  # The unmodified backend's plain circular stroke.
        shape, stroke = row['shape'], row['stroke']
        if (shape['type'] != 'SIMPLE' or shape['stretch'] != 1.0 or shape['stretch_random'] != 0.0
                or stroke['size'] != 1.0 or stroke['size_random'] != 0.0
                or stroke['stroke_size_random'] != 0.0 or stroke['distortion_enabled']):
            raise ValueError('Pixel box footprint requires an unscaled circular SIMPLE brush: '+actual)


def validate_dashes(plan, group_ids, patterns):
    """Dash endpoints need their own provenance before interval clipping."""
    aliases=plan['raw']['request'].get('boundary_aliases',[])+plan['raw']['request'].get('intersection_aliases',[])
    active={row['brush_id'] for row in aliases if row['group'] in group_ids}
    if any(patterns[actual]['mode']=='DASHED' and patterns[actual]['gap_length_px']>0
           for actual in active if actual in patterns):
        raise ValueError('Pixel box interval caps do not yet support dashed brush endpoints')


def interval_layers(geometry, layers):
    """Mark artificial Object/Outline cut ends without modifying source rows.

    Union touching intervals of the same source edge before computing caps:
    fragments share a run footprint, so maximum-Alpha arbitration does not
    attenuate dense style subdivisions. Recompute after element selection;
    never bridge actual hidden gaps or borrow another category's support.
    Original captured Mesh-edge ends retain round joins even for short edges.
    This is continuous coverage, not a short-line or pixel-length rejection.
    Named paint, source alpha, width and category arrays are never rewritten.
    """
    import numpy as np
    result=[]
    for layer in layers:
        count=len(layer['world'])
        clips=np.zeros((count,2),np.float32)
        bounds=np.zeros((count,4),np.float32)
        if layer.get('coverage_profile') == PROFILE and 'preclassified' in layer:
            ids=np.flatnonzero(layer['preclassified'])
            if len(ids):
                meshes=np.asarray(layer['source_mesh_indices'])
                sources=np.asarray(layer['source_indices'])
                for mi in np.unique(meshes[ids]):
                    if not 0<=mi<len(geometry.meshes):
                        raise ValueError('Pixel interval needs captured source-mesh identity')
                    mesh=geometry.meshes[int(mi)]
                    selected=ids[meshes[ids]==mi]
                    edge_ids=sources[selected]
                    if np.any(edge_ids<0) or np.any(edge_ids>=len(mesh.edges)):
                        raise ValueError('Pixel interval source edge is outside captured mesh')
                    parent=np.asarray(mesh.vertices[mesh.edges[edge_ids]],np.float64)
                    ends=np.asarray(layer['world'][selected],np.float64)
                    delta=parent[:,1]-parent[:,0]
                    length2=np.einsum('ij,ij->i',delta,delta)
                    if np.any(length2==0):
                        raise ValueError('Pixel visibility interval requires a nonzero captured source edge')
                    u=np.einsum('ijk,ik->ij',ends-parent[:,0,None,:],delta)/length2[:,None]
                    forward=u[:,0]<=u[:,1]
                    lo=np.min(u,axis=1);hi=np.max(u,axis=1)
                    p=np.asarray(layer['projected'][selected,:,:2],np.float32)
                    first=np.where(forward[:,None],p[:,0],p[:,1])
                    last=np.where(forward[:,None],p[:,1],p[:,0])
                    # Bound arithmetic roundoff in source-u. There is no
                    # world-distance, output-pixel or short-line threshold.
                    scale=np.max(np.abs(parent),axis=(1,2))
                    epsilon=64*np.finfo(np.float64).eps*(1.+scale/np.sqrt(length2))
                    if (not np.isfinite(u).all() or not np.isfinite(epsilon).all()
                            or np.any(epsilon>=.5) or np.any(lo < -epsilon) or np.any(hi > 1.+epsilon)):
                        raise ValueError('Pixel interval source coordinates are nonfinite, out of range or numerically unresolved')
                    order=np.lexsort((lo,edge_ids))
                    members=[];edge=None;run_lo=run_hi=0.;start=end=0
                    def flush():
                        if not members:return
                        rows=selected[members]
                        bounds[rows]=np.r_[first[start],last[end]]
                        clips[rows]=(abs(run_lo)>epsilon[start],abs(run_hi-1.)>epsilon[end])
                    for index in order:
                        if edge!=edge_ids[index] or lo[index]>run_hi+max(epsilon[index],epsilon[end]):
                            flush();members=[index];edge=edge_ids[index]
                            run_lo,run_hi=lo[index],hi[index];start=end=index
                        else:
                            members.append(index)
                            if hi[index]>run_hi:run_hi=hi[index];end=index
                    flush()
        result.append(dict(layer,_toon_interval_clip=clips,_toon_interval_bounds=bounds))
    return result


def renderer_class(base):
    """Privately compose the existing Renderer, including managed eye shaders.

    Function globals are copied per owner. No backend function, enum, shader
    factory or module global is patched. Legacy profiles retain their branches.
    """
    native_init, native_render = base.__init__, base.render_steps
    init_globals, render_globals = dict(native_init.__globals__), dict(native_render.__globals__)
    if tuple(render_globals['PROFILES']) != _BACKEND_PROFILES:
        raise ValueError('Pixel coverage needs review for this backend profile inventory')
    original_shader = init_globals['_shader']
    original_options = render_globals['coverage_options']
    original_batch = render_globals['_line_batch']
    injected = [0]

    def shader(*args, **kwargs):
        args = list(args)
        if len(args) >= 4 and isinstance(args[3], str) and _MARKER in args[3]:
            if args[3].count(_MARKER) != 1:
                raise ValueError('Unexpected line shader footprint marker count')
            args[3] = args[3].replace(_MARKER, _REPLACEMENT, 1)
            args[0]=list(args[0])+[('VEC2','source_interval_clip'),('VEC4','source_interval_bounds')]
            vertex_marker='preclassifiedFlag=source_preclassified;'
            if args[2].count(vertex_marker)!=1:
                raise ValueError('Unexpected pixel interval vertex seam')
            args[2]=args[2].replace(vertex_marker,vertex_marker+'intervalClip=source_interval_clip;intervalBounds=source_interval_bounds;',1)
            if len(args)>4:
                args[4]=list(args[4])+[('VEC2','intervalClip'),('VEC4','intervalBounds')]
            else:
                kwargs['interface']=list(kwargs['interface'])+[('VEC2','intervalClip'),('VEC4','intervalBounds')]
            injected[0] += 1
        return original_shader(*args, **kwargs)

    def options(layer):
        if layer.get('coverage_profile') != PROFILE:
            return original_options(layer)
        return dict(coverage_profile=PROFILE, feather_px=footprint(layer.get('feather_px', 1.0)))

    init_globals['_shader'] = shader
    render_globals['coverage_options'] = options
    render_globals['PROFILES'] = (*_BACKEND_PROFILES, PROFILE)
    render_globals['_toon_interval_layers'] = interval_layers

    def line_batch(shader, layer, alpha_ids, matrix, first, end):
        import numpy as np
        namespace=dict(original_batch.__globals__)
        native_batch=namespace['batch_for_shader']
        def batch(real_shader, primitive, attributes):
            attributes=dict(attributes,source_interval_clip=np.repeat(layer['_toon_interval_clip'][first:end],6,axis=0),
                source_interval_bounds=np.repeat(layer['_toon_interval_bounds'][first:end],6,axis=0))
            return native_batch(real_shader,primitive,attributes)
        namespace['batch_for_shader']=batch
        function=types.FunctionType(original_batch.__code__,namespace,original_batch.__name__,
            original_batch.__defaults__,original_batch.__closure__)
        return function(shader,layer,alpha_ids,matrix,first,end)

    render_globals['_line_batch']=line_batch
    initialize = types.FunctionType(native_init.__code__, init_globals, native_init.__name__,
                                    native_init.__defaults__, native_init.__closure__)
    initialize.__kwdefaults__ = native_init.__kwdefaults__
    source=textwrap.dedent(inspect.getsource(native_render))
    seam='checked=[(layer,dict(**coverage_options(layer),**sampling_options(layer))) for layer in layers]'
    if source.count(seam)!=1:
        raise ValueError('Pixel interval layer seam changed; adapter needs review')
    source=source.replace(seam,'layers=_toon_interval_layers(geometry,layers)\n    '+seam,1)
    exec(compile(source,'<trusted-toon-pixel-renderer>','exec'),render_globals)
    render=render_globals[native_render.__name__]
    return type('ToonPixelBoxRenderer', (base,), dict(__init__=initialize, render_steps=render,
        _toon_pixel_injected=injected, _toon_pixel_profile=PROFILE))


def verify_renderer(renderer):
    if getattr(renderer, '_toon_pixel_injected', None) != [1]:
        raise ValueError('Expected exactly one private pixel coverage shader injection')
