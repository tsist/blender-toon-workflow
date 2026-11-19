# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit static image color on captured RENDER CORNER UV surfaces.

One private RGBA32F table contains policy headers, support-tag directories,
world-to-UV planes and scene-linear image pixels. No stroke array is rewritten.
"""
import hashlib
import json
import math
import numpy as np

KIND='UV_IMAGE'
MAX_BYTES=128*1024*1024
MAX_SIDE=2048
SCHEMA='toon.line-uv-source-render.v1'


def need(ok,message):
    if not ok:raise ValueError(message)


def sha(array):return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
def json_sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()


def normalize(src):
    fields={'kind','image','uv_name','uv_transform','filter','extension','support'}
    need(type(src) is dict and set(src)==fields and src['kind']==KIND,'Invalid explicit UV image source')
    need(all(type(src[k]) is str and 0<len(src[k])<=1024 for k in ('image','uv_name')),'Specify exact image and UV names')
    need(src['filter'] in ('NEAREST','LINEAR') and src['extension'] in ('EXTEND','REPEAT'),'Unsupported UV image filter/wrap')
    need(src['support']=='LOWEST_MATCHING_TRIANGLE','Choose the explicit captured support policy')
    a=src['uv_transform']
    need(type(a) is list and len(a)==6 and all(type(v) in (int,float) and math.isfinite(v) and abs(v)<=65536 for v in a),
         'UV transform must be six bounded finite affine coefficients')
    return src


def image_data(name):
    import bpy
    image=bpy.data.images.get(name)
    need(image is not None and not image.library and not image.override_library,'UV image is missing or nonlocal: '+name)
    need(image.source=='FILE' and len(image.tiles)==1 and image.channels==4,'Use one saved/packed static RGBA image, not an unsaved generated image, UDIM or sequence')
    need(not image.is_dirty,'Save/pack image pixel edits before binding them as a static line-color source')
    w,h=map(int,image.size)
    need(1<=w<=MAX_SIDE and 1<=h<=MAX_SIDE,'UV line image exceeds the 2048-per-side profile')
    need(image.alpha_mode in ('STRAIGHT','CHANNEL_PACKED','NONE'),'Premultiplied image sources require an explicit conversion')
    cs=image.colorspace_settings.name;floating=bool(image.is_float)
    need(cs in ('sRGB','Linear Rec.709','Non-Color'),'Unsupported explicit image color space: '+cs)
    need(not (floating and cs=='sRGB'),'Use Linear Rec.709 for a floating color image; no ambiguous sRGB float interpretation')
    pixels=np.empty(w*h*4,np.float32);image.pixels.foreach_get(pixels);pixels=pixels.reshape(h*w,4)
    need(np.isfinite(pixels).all() and np.all((pixels>=0)&(pixels<=1)),'UV image must contain finite LDR RGBA in [0,1]')
    raw=sha(pixels)
    if cs=='sRGB':
        rgb=pixels[:,:3]
        pixels[:,:3]=np.where(rgb<=np.float32(.04045),rgb/np.float32(12.92),
            ((rgb+np.float32(.055))/np.float32(1.055))**np.float32(2.4))
    descriptor=dict(kind=KIND,image=name,width=w,height=h,colorspace=cs,is_float=floating,
        alpha_mode=image.alpha_mode,alpha_policy='ignored; line coverage unchanged',raw_rgba_sha256=raw,
        linear_rgba_sha256=sha(pixels),pixel_space='scene_linear_Rec709',source=image.source)
    return descriptor,pixels


def validate_descriptor(d):
    keys={'kind','image','width','height','colorspace','is_float','alpha_mode','alpha_policy','raw_rgba_sha256',
          'linear_rgba_sha256','pixel_space','source'}
    need(type(d) is dict and set(d)==keys and d['kind']==KIND,'Invalid resolved UV image descriptor')
    need(type(d['image']) is str and d['image'] and all(type(d[k]) is int and 1<=d[k]<=MAX_SIDE for k in ('width','height')),'Invalid image extent')
    need(d['colorspace'] in ('sRGB','Linear Rec.709','Non-Color') and type(d['is_float']) is bool
         and not (d['is_float'] and d['colorspace']=='sRGB'),'Invalid captured image color interpretation')
    need(d['source']=='FILE' and d['alpha_mode'] in ('STRAIGHT','CHANNEL_PACKED','NONE')
         and d['alpha_policy']=='ignored; line coverage unchanged' and d['pixel_space']=='scene_linear_Rec709','Invalid image alpha/profile')
    for k in ('raw_rgba_sha256','linear_rgba_sha256'):
        need(type(d[k]) is str and len(d[k])==64 and all(c in '0123456789abcdef' for c in d[k]),'Invalid image hash')


def source(material,src,scene):
    normalize(src);d,p=image_data(src['image'])
    for obj in scene.objects:
        if obj.type!='MESH':continue
        slots={i for i,s in enumerate(obj.material_slots) if s.material==material}
        if slots and any(p.material_index in slots for p in obj.data.polygons):
            need(obj.data.uv_layers.get(src['uv_name']) is not None,'Specified UV is missing on '+obj.name)
    return None,d


def _texture_image_cache(resolved):
    cache={}
    for row in resolved['policies']:
        src=row['policy']['source']
        if src['kind']!=KIND:continue
        if src['image'] not in cache:cache[src['image']]=image_data(src['image'])
        need(cache[src['image']][0]==row['source_descriptor'],'UV image changed during render capture')
    return cache


def build(geometry,resolved):
    from .line_image_affine import affines
    active=[(i,r) for i,r in enumerate(resolved['policies']) if r['policy']['source']['kind']==KIND]
    if not active:return None
    cache=_texture_image_cache(resolved)
    maximum=max((m.face_offset+len(m.triangles)-1 for m in geometry.meshes),default=0)
    need(0<=maximum<2**24,'Source triangle tags exceed exact GPU integer range')
    header=np.zeros((max(1,len(resolved['policies'])*3),4),np.float32);blocks=[header];count=len(header);images={};records=[]
    def append(rows):
        nonlocal count
        rows=np.asarray(rows,np.float32).reshape(-1,4);start=count;count+=len(rows)
        need(count*16<=MAX_BYTES and count<2**24,'UV line source table exceeds its 128 MiB admission bound')
        blocks.append(rows);return start
    for i,row in active:
        p=row['policy'];src=p['source'];ref=json.dumps(p['material'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        d,pixels=cache[src['image']]
        if src['image'] not in images:images[src['image']]=append(pixels)
        directory=np.zeros((maximum+1,4),np.float32);base=append(directory);surfaces=[]
        transform=np.asarray(src['uv_transform'],np.float64).reshape(2,3)
        for mesh in geometry.meshes:
            ids=np.flatnonzero(np.asarray(mesh.selector_meta['materials'])[mesh.triangle_polygons]==ref)
            if not len(ids):continue
            uv=next((u for u in mesh.uv_layers if u['name']==src['uv_name']),None)
            need(uv is not None,'Captured RENDER UV missing on '+mesh.name+': '+src['uv_name'])
            corners=np.asarray(uv['uv'])[mesh.triangle_loops[ids]]
            xyz=np.asarray(mesh.vertices)[mesh.triangles[ids]]
            need(np.isfinite(corners).all(),'Captured RENDER UV contains nonfinite values')
            mapped=corners@transform[:,:2].T+transform[:,2]
            planes=affines(xyz,mapped)
            # The legacy GPU point is float32. Bound stored coefficients,
            # coordinate rounding and a conservative dot/interpolation error
            # in image texels; reject large-origin/tiny-triangle cancellation.
            xyz32=np.asarray(xyz,np.float32).astype(np.float64)
            p64=planes.astype(np.float64)
            observed=np.einsum('nij,nkj->nki',p64[:,:,:3],xyz32)+p64[:,None,:,3]
            corner_error=np.max(abs(observed-mapped),axis=1)
            scale=np.max(abs(xyz32),axis=1)
            arithmetic=16*np.finfo(np.float32).eps*(np.einsum('nij,nj->ni',abs(p64[:,:,:3]),scale)+abs(p64[:,:,3]))
            bound=float(np.max((corner_error+arithmetic)*np.array([d['width'],d['height']])))
            need(math.isfinite(bound) and bound<=.25,'UV line mapping exceeds the quarter-texel float32 profile on '+mesh.name)
            start=append(planes.reshape(-1,4))
            directory[mesh.face_offset+ids,0]=start+np.arange(len(ids),dtype=np.float32)*2
            surfaces.append(dict(mesh=mesh.name,triangles=len(ids),tag_sha256=sha(np.asarray(mesh.face_offset+ids,np.int64)),
                uv_name=src['uv_name'],uv_sha256=sha(corners),world_sha256=sha(xyz),planes_sha256=sha(planes),error_bound_texels=bound))
        need(bool(surfaces),'UV material has no captured RENDER support surfaces: '+p['material']['name'])
        header[i*3]=(images[src['image']],d['width'],d['height'],float(src['filter']=='LINEAR'))
        header[i*3+1]=(base,float(src['extension']=='REPEAT'),maximum+1,0)
        header[i*3+2,:3]=np.asarray(p['tint'])*p['gain']
        records.append(dict(policy_id=p['id'],source=src,image=d,surfaces=surfaces))
    table=np.concatenate(blocks)
    receipt=dict(schema=SCHEMA,policies=records,table_rows=len(table),table_sha256=sha(table),
        support='LOWEST_MATCHING_TRIANGLE',mapping='RENDER_CORNER_UV_world_affine_at_source_point',
        alpha_width_geometry='unchanged')
    receipt['sha256']=json_sha(receipt)
    return dict(table=table,receipt=receipt)


GLSL='''
vec4 toonInkRead(int index){int w=textureSize(toonInkUV,0).x;return texelFetch(toonInkUV,ivec2(index%w,index/w),0);}
vec3 toonInkTexel(ivec2 p,vec4 image,bool repeatUV){
    ivec2 e=ivec2(image.yz);
    if(repeatUV)p=((p%e)+e)%e;else p=clamp(p,ivec2(0),e-1);
    return toonInkRead(int(image.x)+p.y*e.x+p.x).rgb;
}
vec3 toonInkImageColor(int policy,vec3 point,vec2 supports){
    vec4 image=toonInkRead(policy*3),directory=toonInkRead(policy*3+1);
    int a=int(min(supports.x,supports.y)),b=int(max(supports.x,supports.y)),address=0;
    if(a>0 && a<int(directory.z))address=int(toonInkRead(int(directory.x)+a).x);
    if(address==0 && b>0 && b<int(directory.z))address=int(toonInkRead(int(directory.x)+b).x);
    if(address==0)return vec3(0); // CPU admission guarantees a matching source support.
    vec4 world=vec4(point,1.0);
    vec2 uv=vec2(dot(toonInkRead(address),world),dot(toonInkRead(address+1),world));
    vec2 p=uv*image.yz-0.5;ivec2 lo=ivec2(floor(p));bool repeatUV=directory.y>0.5;
    vec3 color;
    if(image.w<0.5)color=toonInkTexel(ivec2(floor(p+0.5)),image,repeatUV);
    else{vec2 f=fract(p);color=mix(mix(toonInkTexel(lo,image,repeatUV),toonInkTexel(lo+ivec2(1,0),image,repeatUV),f.x),
         mix(toonInkTexel(lo+ivec2(0,1),image,repeatUV),toonInkTexel(lo+ivec2(1,1),image,repeatUV),f.x),f.y);}
    return color*toonInkRead(policy*3+2).rgb;
}
'''


def validate_receipt(receipt):
    need(type(receipt) is dict and set(receipt)=={'schema','policies','table_rows','table_sha256','support','mapping','alpha_width_geometry','sha256'}
         and receipt['schema']==SCHEMA,'Invalid UV RENDER receipt')
    need(json_sha({k:v for k,v in receipt.items() if k!='sha256'})==receipt['sha256'],'UV RENDER receipt hash differs')
    need(type(receipt['table_rows']) is int and 0<receipt['table_rows']*16<=MAX_BYTES
         and receipt['support']=='LOWEST_MATCHING_TRIANGLE' and receipt['mapping']=='RENDER_CORNER_UV_world_affine_at_source_point'
         and receipt['alpha_width_geometry']=='unchanged','Invalid UV RENDER contract')
    value=receipt['table_sha256']
    need(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'Invalid UV table hash')
    need(type(receipt['policies']) is list and 1<=len(receipt['policies'])<=256,'Invalid UV RENDER policies')
    seen=set()
    for row in receipt['policies']:
        need(type(row) is dict and set(row)=={'policy_id','source','image','surfaces'},'Invalid UV RENDER policy fields')
        normalize(row['source']);validate_descriptor(row['image'])
        need(row['source']['image']==row['image']['image'] and type(row['policy_id']) is str and row['policy_id']
             and type(row['surfaces']) is list and row['surfaces'],'Invalid UV RENDER source binding')
        need(row['policy_id'] not in seen,'Duplicate UV RENDER policy ID')
        seen.add(row['policy_id'])
        for mesh in row['surfaces']:
            need(type(mesh) is dict and set(mesh)=={'mesh','triangles','tag_sha256','uv_name','uv_sha256','world_sha256','planes_sha256','error_bound_texels'}
                 and mesh['uv_name']==row['source']['uv_name'] and type(mesh['triangles']) is int and mesh['triangles']>0,'Invalid UV RENDER surface')
            need(type(mesh['error_bound_texels']) in (int,float) and math.isfinite(mesh['error_bound_texels']) and 0<=mesh['error_bound_texels']<=.25,'UV numerical profile differs')
            for field in ('tag_sha256','uv_sha256','world_sha256','planes_sha256'):
                value=mesh[field];need(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'Invalid UV surface hash')
    return len(receipt['policies'])
