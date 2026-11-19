# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete projected source Alpha references, independent of stroke cuts.

References follow the existing full-source xy/knots contract. The sampler is
float32; original knots remain float64 until the upload boundary. Equal knots
created by float32 rounding use the right-hand value, without deleting rows.
"""
import numpy as np
from . import alpha_function

LIMIT=2**24

def reference(clip,positions,alphas,width,height):
    """Return a finite positive-w source reference, or the explicit old fallback."""
    if not np.all(clip[:,3]>0):return None
    xy=(clip[:,:2]/clip[:,3,None]+1)*np.array([width,height])*.5
    if not np.isfinite(xy).all() or np.any(abs(xy)>np.finfo(np.float32).max):return None
    return dict(xy=xy,knots=np.column_stack((positions,np.clip(alphas,0,1))))

def function_reference(clip,phase,attribute,curve,scale,width,height):
    """Preserve the original Alpha function, independent of width cuts."""
    base=reference(clip,np.array([0.,1.]),np.zeros(2),width,height)
    if base is None:return None
    weights=np.asarray(clip[:,3],np.float64);weights=weights/weights.max()
    return dict(xy=base['xy'],function=dict(map=attribute,weights=weights,
                phase=phase,curve=curve,scale=scale))

def merge(rows):
    references=[];ids=[]
    for row in rows:
        refs=row.get('alpha_references',[])
        if not isinstance(refs,(list,tuple)):raise ValueError('Source Alpha references must be a sequence')
        local=row.get('alpha_source_ids')
        if (local is None)!=('alpha_references' not in row):raise ValueError('Incomplete source Alpha reference fields')
        if local is None:local=np.full(len(row['projected']),-1,np.int64)
        local=np.asarray(local)
        if local.dtype.kind not in 'iu' or local.shape!=(len(row['projected']),):raise ValueError('Integer source Alpha IDs required')
        if np.any(local < -1) or np.any(local >= len(refs)):raise ValueError('Source Alpha reference ID out of range')
        ids.append(np.where(local>=0,local+len(references),-1))
        references.extend(refs)
    return dict(alpha_references=references,alpha_source_ids=np.concatenate(ids) if ids else np.empty(0,np.int64))

def pack(layers):
    job=pack_steps(layers)
    while True:
        try:next(job)
        except StopIteration as done:return done.value

def pack_steps(layers):
    """Validate and pack one frame in bounded reference batches."""
    merged=merge(layers);refs=merged['alpha_references'];ids=merged['alpha_source_ids']
    if 4*len(refs)>=LIMIT:raise ValueError('Source Alpha descriptor exceeds exact float32 indexing')
    descriptors=np.zeros((4*len(refs),4),np.float32);knots=[];offset=0
    curve_cache={};curve_tables={};function_count=point_count=0
    for index,ref in enumerate(refs):
        if index and index%128==0:yield dict(stage='alpha-pack',references_done=index,references_total=len(refs))
        if not isinstance(ref,dict) or set(ref) not in ({'xy','knots'},{'xy','function'}):raise ValueError('Source Alpha requires xy and complete knots or function')
        xy=np.asarray(ref['xy'])
        if xy.dtype.kind not in 'iuf' or xy.shape!=(2,2) or not np.isfinite(xy).all():raise ValueError('Finite source Alpha xy required')
        if np.any(abs(xy)>np.finfo(np.float32).max):raise ValueError('Source Alpha xy exceeds float32')
        descriptors[4*index]=xy.reshape(4)
        if 'function' in ref:
            function_count+=1;function=alpha_function.checked(ref['function'],curve_cache)
            curve=function['curve'];values=np.ones(1) if curve is None else curve_cache[id(curve)]
            constant_curve=float(values[0]) if np.all(values==values[0]) else None
            if function['phase'][0]==function['phase'][1]:
                constant_curve=float(np.interp(function['phase'][0],np.linspace(0,1,len(values)),values))
            uniform=-1.
            if function['scale']==0 or np.all(function['map']==0):uniform=0.
            elif function['map'][0]==function['map'][1] and constant_curve is not None:
                uniform=float(function['map'][0])*constant_curve*function['scale']
            gpu_xy=xy.astype(np.float32).astype(np.float64)
            delta=gpu_xy[1]-gpu_xy[0]
            point=bool(np.max(abs(delta))<=np.finfo(np.float32).tiny)
            maximum=uniform if uniform>=0 else (alpha_function.maximum(function) if point else 1.)
            point_count+=int(point)
            start=count=0
            if uniform<0:
                packed_values=values.astype(np.float32,copy=False)
                key=packed_values.tobytes()
                if key not in curve_tables:
                    count=len(values)
                    if offset+count>=LIMIT:raise ValueError('Source Alpha table exceeds exact float32 indexing')
                    packed=np.zeros((count,4),np.float32);packed[:,1]=packed_values
                    curve_tables[key]=(offset,count);knots.append(packed);offset+=count
                start,count=curve_tables[key]
            descriptors[4*index+1]=(start,count,maximum,uniform)
            descriptors[4*index+2]=(*function['map'],*function['weights'])
            descriptors[4*index+3]=(*function['phase'],function['scale'],1.)
            continue
        values=np.asarray(ref['knots'])
        if values.dtype.kind not in 'iuf' or values.ndim!=2 or values.shape[1]!=2 or len(values)<2 or not np.isfinite(values).all():raise ValueError('Finite complete source Alpha knots required')
        if values[0,0]!=0 or values[-1,0]!=1 or np.any(np.diff(values[:,0])<=0) or np.any(values[:,1]<0) or np.any(values[:,1]>1):raise ValueError('Source Alpha knots must increase over [0,1] with Alpha in [0,1]')
        maximum=float(values[:,1].max());uniform=maximum if np.all(values[:,1]==maximum) else -1.
        # Constant sources need no knot texture rows, including point references.
        count=len(values) if uniform<0 else 0
        if offset+count>=LIMIT:raise ValueError('Source Alpha knots exceed exact float32 indexing')
        descriptors[4*index+1]=(offset,count,maximum,uniform)
        if count:
            packed=np.zeros((count,4),np.float32);packed[:,:2]=values
            knots.append(packed);offset+=count
    ids_by_layer=[];start=0
    for layer in layers:
        end=start+len(layer['projected']);ids_by_layer.append(ids[start:end].astype(np.float32));start=end
    return (descriptors,np.concatenate(knots) if knots else np.empty((0,4),np.float32),ids_by_layer,
            dict(references=len(refs),knots=offset,referenced_primitives=int(np.count_nonzero(ids>=0)),
                 legacy_primitives=int(np.count_nonzero(ids<0)),model='full_source_alpha_function_and_knots.v2',
                 function_references=function_count,knots_references=len(refs)-function_count,
                 shared_curve_tables=len(curve_tables),point_function_references=point_count,
                 cross_eye_policy='explicit_legacy_fragment_fallback'))

GLSL='''
vec4 alphaRecord(int index){
    int width=textureSize(sourceAlphaReferences,0).x;
    return texelFetch(sourceAlphaReferences,ivec2(index%width,index/width),0);
}
vec2 alphaKnot(int index){
    int width=textureSize(sourceAlphaKnots,0).x;
    return texelFetch(sourceAlphaKnots,ivec2(index%width,index/width),0).xy;
}
float completeSourceAlpha(vec2 samplePosition,float fallback){
    if(alphaSourceId<0.0)return fallback;
    int index=int(alphaSourceId)*4;
    vec4 xy=alphaRecord(index),range=alphaRecord(index+1);
    if(range.w>=0.0)return range.w;
    // Finite endpoints can still overflow when subtracted in float32.
    // Scale before that subtraction only in the extreme range, preserving
    // the ordinary viewport path's precision.
    float scale=max(max(abs(xy.x),abs(xy.y)),max(abs(xy.z),abs(xy.w)));
    // A dynamic reciprocal near float32 max can itself flush to zero.
    // This fixed normal factor bounds the largest possible difference while
    // keeping every subsequent reciprocal in the normal float32 range.
    if(scale>1e18){xy*=1e-18;samplePosition*=1e-18;}
    vec2 delta=xy.zw-xy.xy;float span=max(abs(delta.x),abs(delta.y));
    if(span<=1.17549435e-38)return range.z;
    vec2 unit=delta/span;
    float u=clamp(dot((samplePosition-xy.xy)/span,unit)/dot(unit,unit),0.0,1.0);
    vec4 function=alphaRecord(index+3);
    if(function.w>0.5){
        vec4 source=alphaRecord(index+2);
        float mapValue;
        if(u<=0.0)mapValue=source.x;
        else if(u>=1.0)mapValue=source.y;
        else{
            float first=source.w*(1.0-u),last=source.z*u;
            mapValue=(source.x*first+source.y*last)/(first+last);
        }
        float phase=clamp(mix(function.x,function.y,u),0.0,1.0);
        float position=phase*(range.y-1.0);
        int first=int(floor(position)),last=min(first+1,int(range.y)-1);
        float curve=mix(alphaKnot(int(range.x)+first).y,alphaKnot(int(range.x)+last).y,position-float(first));
        return clamp(mapValue*curve*function.z,0.0,1.0);
    }
    int offset=int(range.x),last=int(range.y)-1;
    if(u>=1.0)return alphaKnot(offset+last).y;
    int lower=0,upper=last;
    // upper_bound handles knots that become equal only after float32 upload.
    for(int step=0;step<24 && lower+1<upper;step++){
        int mid=(lower+upper)/2;
        if(alphaKnot(offset+mid).x<=u)lower=mid;else upper=mid;
    }
    vec2 a=alphaKnot(offset+lower),b=alphaKnot(offset+upper);
    return b.x>a.x?mix(a.y,b.y,clamp((u-a.x)/(b.x-a.x),0.0,1.0)):b.y;
}
'''
