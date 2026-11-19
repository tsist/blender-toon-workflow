# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned brush consumer: stable source randomness, strokes and GPU map records."""
import hashlib
import math
import numpy as np
from . import brush_effects as E

STRIDE=21
HEADER=32
PRIMITIVE_KEYS=('projected','world','supports','widths','alphas','alpha_source_ids','preclassified',
    'source_indices','source_mesh_indices','source_kinds','category_bits','brush_phase','rgb_endpoints','line_function_policy')

def _random(identity,seed,label):
    digest=hashlib.blake2b((str(seed)+'|'+identity+'|'+label).encode('utf8'),digest_size=8).digest()
    return int.from_bytes(digest,'little')/(2**64-1)*2-1

def _curve(points,t):
    return np.interp(t,np.asarray(points)[:,0],np.asarray(points)[:,1])

def _phase(layer,index):
    if 'brush_phase' in layer:return np.asarray(layer['brush_phase'][index],np.float64)
    projected=np.asarray(layer['projected'][index,:,:2])
    p=np.asarray(projected,np.float64)
    rid=layer.get('alpha_source_ids',np.full(len(layer['projected']),-1))[index]
    if rid<0:return np.array([0.,1.])
    ref=layer['alpha_references'][rid];xy=np.asarray(ref['xy']);d=xy[1]-xy[0];l2=np.dot(d,d)
    q=np.clip((p-xy[0])@d/l2,0,1) if l2 else np.zeros(2)
    # Projection is published in its input dtype while complete source Alpha
    # retains float64 parent coordinates. Recognize only exact representable
    # parent endpoints; never widen a tolerance over interior clip/curve cuts.
    quantized=np.asarray(xy,dtype=projected.dtype)
    at_first=np.all(projected==quantized[0],axis=1)
    at_last=np.all(projected==quantized[1],axis=1)
    # Coincident quantized parents cannot identify an endpoint unambiguously.
    q=np.where(at_first&~at_last,0.,np.where(at_last&~at_first,1.,q))
    endpoints=ref.get('function',{}).get('phase',[0.,1.])
    return endpoints[0]+q*(endpoints[1]-endpoints[0])

def _surface(geometry,world,supports,m):
    # Capture contains immutable arrays; interpolate through a supporting
    # evaluated triangle, retaining CORNER seams for UV/color attributes.
    uv=np.zeros((2,2));rgba=np.ones((2,4))
    for endpoint,point in enumerate(world):
        found=False
        for support in supports:
            tid=int(support)
            if tid<1:continue
            mesh=next((x for x in geometry.meshes if x.face_offset<=tid<x.face_offset+len(x.triangles)),None)
            if mesh is None:continue
            tri=tid-mesh.face_offset;vertices=mesh.triangles[tri];corners=mesh.triangle_loops[tri]
            xyz=np.asarray(mesh.vertices[vertices],np.float64);ab=xyz[1]-xyz[0];ac=xyz[2]-xyz[0]
            weights=np.linalg.lstsq(np.column_stack((ab,ac)),point-xyz[0],rcond=None)[0]
            bary=np.array([1-weights.sum(),*weights]);bary=np.maximum(bary,0);bary/=max(bary.sum(),1e-30)
            if m['source_type']=='OBJECT_COLOR':
                layers=list(mesh.layers)
                layer=(next((x for x in layers if x['name']==m['object_color_name']),None)
                    if m['object_color_selection_mode']=='NAME' else
                    (layers[m['object_color_index']] if m['object_color_index']<len(layers) else None))
                if layer is None:raise ValueError('Requested object color layer missing on '+mesh.name)
                rgba[endpoint]=bary@layer['linear_rgba'][vertices if layer['domain']=='POINT' else corners]
            if m['uv_source']=='UV' and m['source_type']=='IMAGE':
                layers=getattr(mesh,'uv_layers',[])
                layer=(next((x for x in layers if x['name']==m['uv_name']),None) if m['uv_selection_mode']=='NAME' else
                    (layers[m['uv_index']] if m['uv_index']<len(layers) else None))
                if layer is None:raise ValueError('Requested UV layer missing on '+mesh.name)
                uv[endpoint]=bary@layer['uv'][corners]
            found=True;break
        if not found and (m['source_type']=='OBJECT_COLOR' or m['uv_source']=='UV'):
            raise ValueError('Surface texture requires a supporting evaluated face')
    return uv,rgba

def _map_coordinates(geometry,layer,index,m,width,height):
    world=layer['world'][index]
    if m['uv_source']=='SCREEN':uv=layer['projected'][index,:,:2]/[width,height]
    elif m['uv_source']=='WORLD':uv=world[:,:2]
    else:uv=np.zeros((2,2))
    rgba=np.ones((2,4))
    if m['enabled'] and (m['source_type']=='OBJECT_COLOR' or m['uv_source']=='UV'):
        surface,rgba=_surface(geometry,world,layer['supports'][index],m)
        if m['uv_source']=='UV':uv=surface
    return uv,rgba

def apply_layers(geometry,plan,layers,view,width,height,scale=1.,projection=None):
    rows=E.records(plan['raw'])
    if not rows:return layers,dict(schema=E.SCHEMA,active_actuals=[])
    output=[];reports=[]
    for layer in layers:
        actual=layer.get('actual_brush');row=rows.get(actual)
        if row is None or E.neutral(row):output.append(layer);continue
        styled,report=_apply(geometry,layer,row,view,width,height,scale,projection=projection)
        output.append(styled);reports.append(dict(actual=actual,**report))
    return output,dict(schema=E.SCHEMA,active_actuals=[x['actual'] for x in reports],layers=reports,
        model='autonomous portable brush authors; native pixel equivalence not claimed')

def _apply(geometry,layer,row,view,width,height,scale,projection=None):
    count=len(layer['projected']);s=row['stroke'];shape=row['shape'];cv=row['curves']
    records=[];origins=[];positions=[];worlds=[];widths=[];alphas=[];parents=[];fractions=[];rgb_rows=[]
    surface_data=[[_map_coordinates(geometry,layer,i,row['maps'][key],width,height) for key in E.MAP_NAMES] for i in range(count)]
    eye=np.linalg.inv(np.asarray(view,np.float64))[:3,3]
    for i in range(count):
        original=np.asarray(layer['projected'][i],np.float64);delta=original[1,:2]-original[0,:2];length=np.linalg.norm(delta)
        tangent=delta/length if length else np.array([1.,0.]);normal=np.array([-tangent[1],tangent[0]])
        mesh_index=int(layer.get('source_mesh_indices',np.zeros(count,np.int32))[i])
        mesh=geometry.meshes[mesh_index] if 0<=mesh_index<len(geometry.meshes) else None
        identity=str((getattr(mesh,'name',''),getattr(mesh,'instance_key',None)))+'|'+str(layer.get('source_indices',np.arange(count))[i])+'|'+str(layer.get('source_kinds',np.zeros(count,np.int32))[i])
        identity=str(layer.get('actual_brush',''))+'|'+identity
        rand=lambda label:_random(identity,s['seed'],label)
        phases=_phase(layer,i)
        copies=max(1,min(64,s['copies']+round(rand('copies')*s['copy_random'])))
        if s['stroke_type'] in ('RAKE','RANDOM'):copies=max(3,copies)
        reduction_active=any(r['enabled'] for ch in row['reduction'].values() for r in ch.values())
        segments=max(1,int(math.ceil(length/4))) if s['distortion_enabled'] or reduction_active else 1
        if s['distortion_enabled']:
            max_cycles=abs(s['distortion_cycles'])+abs(s['distortion_cycles_random'])
            segments=max(segments,int(math.ceil(max_cycles*abs(phases[1]-phases[0])*8)))
        cuts=np.linspace(0,1,segments+1)
        # Add every authored curve knot before interpolation: piecewise-linear
        # tapers remain independent of source tessellation.
        if phases[1]!=phases[0]:
            for name in ('size','alpha'):
                if cv[name+'_enabled']:
                    target=np.asarray(cv[name])[:,0]*(cv['reduction_end']-cv['reduction_start'])+cv['reduction_start']
                    cuts=np.concatenate((cuts,(target-phases[0])/(phases[1]-phases[0])))
        cuts=np.unique(np.clip(cuts,0,1))
        # STAMP is a deterministic source-local repeated mark/space author.
        if (s['stroke_type']=='STAMP' or s['line_type']=='DASHED') and length:
            # Period derives from actual+seed, while absolute arclength comes
            # from complete chain phase. Mesh cuts never restart a mark.
            mark=max(.01,s['length']+s['length_random']*_random(str(layer.get('actual_brush','')),s['seed'],'mark'))*scale
            gap=max(0.,s['space']+s['space_random']*_random(str(layer.get('actual_brush','')),s['seed'],'gap'))*scale
            span=phases[1]-phases[0]
            total=length/abs(span) if span else length
            arc=phases*total if span else np.array([0.,length])
            low,high=min(arc),max(arc);spans=[]
            for k in range(math.floor(low/(mark+gap))-1,math.floor(high/(mark+gap))+1):
                a=max(low,k*(mark+gap));b=min(high,k*(mark+gap)+mark)
                if b>a:
                    q=np.sort((np.array([a,b])-arc[0])/(arc[1]-arc[0]))
                    spans.append((max(0.,q[0]),min(1.,q[1])))
        else:spans=[(0.,1.)]
        for copy_index in range(copies):
            cr=lambda name:_random(identity,s['seed'],name+'|'+str(copy_index))
            offset=(tangent*(s['horizontal_space']+s['horizontal_space_random']*cr('horizontal'))*copy_index+
                normal*(s['normal_offset']+s['normal_offset_random']*cr('normal')+
                        (s['vertical_space']+s['vertical_space_random']*cr('vertical'))*copy_index)+
                np.array([s['x_offset']+s['x_offset_random']*cr('x'),s['y_offset']+s['y_offset_random']*cr('y')]))*scale
            source_width=float(np.max(layer['widths'][i]))
            if s['stroke_type']=='RAKE':offset+=normal*(copy_index-(copies-1)*.5)*source_width/copies
            elif s['stroke_type']=='RANDOM':offset+=(tangent*cr('scatter_tangent')+normal*cr('scatter_normal'))*source_width
            extend=(s['extend']+s['extend_random']*cr('extend'))*scale
            size=max(0.,s['size']+s['size_random']*rand('size')+s['stroke_size_random']*cr('stroke_size'))
            if s['stroke_type']=='RAKE':size/=copies*1.25
            elif s['stroke_type']=='RANDOM':size*=.25+.5*(cr('scatter_size')+1.)
            stretch=max(.01,shape['stretch']+shape['stretch_random']*cr('stretch'))
            angle=math.radians(shape['angle']+shape['angle_random']*cr('angle'))
            for lo,hi in spans:
                samples=np.unique(np.concatenate(([lo],cuts[(cuts>lo)&(cuts<hi)],[hi])))
                for q in np.column_stack((samples[:-1],samples[1:])):
                    phase=phases[0]+q*(phases[1]-phases[0]);xy=original[0]+q[:,None]*(original[1]-original[0]);original_xy=xy[:,:2].copy()
                    xy[:,:2]+=offset
                    # Only physical primitive ends that are complete chain
                    # ends extend. Reversed source phase has the same two ends.
                    first_end=q[0]==0. and phase[0] in (0.,1.)
                    last_end=q[1]==1. and phase[1] in (0.,1.)
                    xy[0,:2]-=tangent*extend*first_end;xy[1,:2]+=tangent*extend*last_end
                    if s['distortion_enabled']:
                        cycles=s['distortion_cycles']+s['distortion_cycles_random']*cr('cycles')
                        shift=s['distortion_phase']+s['distortion_phase_random']*cr('phase')
                        amount=(s['distortion_amount']+s['distortion_random']*cr('distortion'))*scale
                        xy[:,:2]+=normal[None,:]*np.sin((phase*cycles+shift)*math.tau)[:,None]*amount
                    source_q=q
                    if projection is not None:
                        clip_w=np.asarray(layer['world'][i])@np.asarray(projection)[3,:3]+np.asarray(projection)[3,3]
                        if np.all(clip_w>0):source_q=(q/clip_w[1])/((1-q)/clip_w[0]+q/clip_w[1])
                    world=layer['world'][i,0]+source_q[:,None]*(layer['world'][i,1]-layer['world'][i,0])
                    taper=np.clip((phase-cv['reduction_start'])/(cv['reduction_end']-cv['reduction_start']),0,1)
                    wf=np.ones(2)*size;af=np.ones(2)
                    if cv['size_enabled']:wf*=_curve(cv['size'],taper)
                    if cv['alpha_enabled']:af*=_curve(cv['alpha'],taper)
                    for kind,factors in (('size',wf),('alpha',af)):
                        red=row['reduction'][layer['visibility'] if layer['visibility']!='BOTH' else 'VISIBLE'][kind]
                        if red['enabled']:
                            if red['refer_object_on']:
                                refs=getattr(geometry,'reference_points',{})
                                if red['object_reference'] not in refs:raise ValueError('Distance reference object missing: '+red['object_reference'])
                                origin=np.asarray(refs[red['object_reference']])
                            else:origin=eye
                            distance=np.linalg.norm(world-origin,axis=1)
                            u=np.clip((distance-red['reduction_start'])/(red['reduction_end']-red['reduction_start']),0,1)
                            factors*=_curve(red['curve'],u)
                    rec=np.zeros((STRIDE,4),np.float32)
                    rec[0]=(stretch,angle,shape['groove'],shape['groove_number'])
                    size_map=row['maps']['size'];image=size_map['image']
                    maxmap=(max(image['pixels'][::4]) if image else float(np.max(surface_data[i][1][1][:,0]))) if size_map['enabled'] else 1.
                    bound=max(1.,1-size_map['amount']+size_map['amount']*maxmap) if size_map['enabled'] else 1.
                    rec[1]=(*row['raster']['pixel_aspect'],bound,('SIMPLE','NORMAL','MULTIPLE').index(shape['type']))
                    rec[2]=(*phase,0.,float(length))
                    for mi,(uv,rgba) in enumerate(surface_data[i]):
                        rec[3+mi]=(uv[0]+source_q[0]*(uv[1]-uv[0])).tolist()+(uv[0]+source_q[1]*(uv[1]-uv[0])).tolist()
                        rec[8+mi*2]=rgba[0]+source_q[0]*(rgba[1]-rgba[0]);rec[9+mi*2]=rgba[0]+source_q[1]*(rgba[1]-rgba[0])
                    rec[18]=(*af,0.,0.)
                    rec[19]=(*np.mean(surface_data[i][4][1],axis=0)[:3],0.)
                    rec[20]=original_xy.reshape(4)
                    records.append(rec);positions.append(xy);worlds.append(world);widths.append((layer['widths'][i,0]+q*(layer['widths'][i,1]-layer['widths'][i,0]))*wf)
                    alphas.append(layer['alphas'][i,0]+q*(layer['alphas'][i,1]-layer['alphas'][i,0]));parents.append(i);fractions.append(q)
                    if 'rgb_endpoints' in layer:rgb_rows.append(layer['rgb_endpoints'][i,0]+source_q[:,None]*(layer['rgb_endpoints'][i,1]-layer['rgb_endpoints'][i,0]))
                    if len(records)>1000000:raise ValueError('Authored brush primitive budget exceeded; no partial output')
    result=layer.copy();ids=np.asarray(parents,np.int64)
    for key in PRIMITIVE_KEYS:
        if key in layer:
            value=np.asarray(layer[key])
            if value.ndim<1 or len(value)!=count:raise ValueError('Brush primitive array length differs: '+key)
            result[key]=value[ids]
    if rgb_rows:result['rgb_endpoints']=np.asarray(rgb_rows,np.float32)
    for key,values in [('projected',positions),('world',worlds),('widths',widths),('alphas',alphas)]:
        result[key]=np.asarray(values,np.float32).reshape((-1,2,3 if key in ('projected','world') else 1)) if key in ('projected','world') else np.asarray(values,np.float32).reshape((-1,2))
    result['brush_effect_rows']=np.asarray(records,np.float32).reshape((-1,STRIDE,4))
    result['brush_effect_author']=row
    if row['raster']['antialiasing'] is not None:result['coverage_profile']='round_capsule.v1';result['feather_px']=row['raster']['antialiasing']
    if row['raster']['samples_per_axis'] is not None:result['samples_per_axis']=row['raster']['samples_per_axis']
    return result,dict(input_primitives=count,output_primitives=len(records),shape=shape['type'],copies=s['copies'],maps=[k for k,v in row['maps'].items() if v['enabled']],source_identity_retained=True)

def pack(layer):
    row=layer.get('brush_effect_author')
    if row is None:return np.zeros((1,4),np.float32),np.ones((1,4),np.float32)
    header=np.zeros((HEADER,4),np.float32);pixels=[];offset=0
    for i,key in enumerate(E.MAP_NAMES):
        m=row['maps'][key];base=i*6
        header[base]=(float(m['enabled']),m['amount'],float(m['source_type']=='OBJECT_COLOR'),('STROKE','UV','SCREEN','WORLD').index(m['uv_source']))
        image=m['image'];data=np.asarray(image['pixels'],np.float32).reshape(-1,4) if image else np.ones((1,4),np.float32)
        header[base+1]=(offset,image['width'] if image else 1,image['height'] if image else 1,float(m['filter_mode']=='LINEAR'))
        header[base+2]=(*m['tiling'],*m['offset'])
        header[base+3]=(math.radians(m['rotation']),('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE').index(m['wrap_mode_u']),('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE').index(m['wrap_mode_v']),0.)
        pixels.append(data);offset+=len(data)
    c=row['color'];header[30]=(float(c['mode']=='HSV'),c['red'],c['green'],c['blue']);header[31]=(c['hue'],c['saturation'],c['value'],0.)
    return np.concatenate((header,layer['brush_effect_rows'].reshape(-1,4))),np.concatenate(pixels)

def preview_fixture(points,author,*,width=256,height=128,line_width=12.,color=(.15,.15,.15)):
    """Preview uses the production author consumer and Renderer, never a proxy.

    Returns (owned Geometry-like capture, layer). Caller supplies identity MVP
    inside a GPU-enabled RenderEngine callback and reads the resulting texture.
    The generated plane supplies UV and POINT/CORNER color preview coordinates.
    """
    from types import SimpleNamespace
    p=np.asarray(points,np.float64)
    if p.ndim!=2 or p.shape[1]!=2 or len(p)<2 or not np.isfinite(p).all():raise ValueError('Preview requires finite Nx2 pixel path')
    vertices=np.array([[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0]],np.float32)
    tri=np.array([[0,1,2],[0,2,3]],np.int32);loops=np.arange(6).reshape(2,3)
    uv=(vertices[tri,:2]+1)*.5
    mesh=SimpleNamespace(name='OwnedBrushPreview',instance_key=None,face_offset=1,vertices=vertices,triangles=tri,
        triangle_loops=loops,uv_layers=[dict(name='UVMap',uv=uv.reshape(-1,2))],
        layers=[dict(name='Color',domain='POINT',linear_rgba=np.column_stack(((vertices[:,:2]+1)*.5,np.ones((4,2)))) )])
    geometry=SimpleNamespace(meshes=[mesh],reference_points={})
    xy=np.stack((p[:-1],p[1:]),axis=1);projected=np.concatenate((xy,np.zeros((*xy.shape[:2],1))),axis=2)
    world=projected.copy();world[:,:,:2]=world[:,:,:2]/[width,height]*2-1
    n=len(xy);arc=np.concatenate(([0.],np.cumsum(np.linalg.norm(p[1:]-p[:-1],axis=1))));phase=arc/max(arc[-1],1e-30)
    layer=dict(projected=projected.astype(np.float32),world=world.astype(np.float32),supports=np.ones((n,2),np.float32),brush_phase=np.column_stack((phase[:-1],phase[1:])),
        widths=np.full((n,2),line_width,np.float32),alphas=np.ones((n,2),np.float32),color=color,visibility='BOTH',
        source_indices=np.arange(n,dtype=np.int64),source_mesh_indices=np.zeros(n,np.int32),source_kinds=np.ones(n,np.int8),category_bits=np.ones(n,np.uint16),
        actual_brush='owned-brush-preview',coverage_profile='round_capsule.v1',feather_px=1.,samples_per_axis=1,post_filter_sigma=0.)
    checked=E.checked(author)
    if not E.neutral(checked):layer,_=_apply(geometry,layer,checked,np.eye(4),width,height,1.)
    return geometry,layer
