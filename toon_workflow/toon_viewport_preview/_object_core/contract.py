# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated, hash-bound evaluated geometry and explicit camera inputs."""
import hashlib
import json
from pathlib import Path
import numpy as np

SCHEMA = 'toon-image-input.v1'
REQUIRED = ('co','edges','loop_vertex','loop_edge','face_start','face_total','face_material',
            'face_smooth','face_normal','corner_normals','triangles','edge_sharp')

class InputError(ValueError):
    pass

def digest_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def fail(condition, message):
    if not condition:
        raise InputError(message)

def matrix(value, label):
    m=np.asarray(value,dtype=np.float64)
    fail(m.shape==(4,4) and np.isfinite(m).all(),label+' requires a finite 4x4 matrix')
    fail(abs(np.linalg.det(m))>1e-14,label+' is singular')
    return m

def load_input(path):
    path=Path(path).resolve(strict=True)
    document=json.loads(path.read_text(encoding='utf-8-sig'),parse_constant=lambda s: (_ for _ in ()).throw(InputError('Nonfinite JSON value')))
    fail(document.get('schema') in (SCHEMA,'toon-image-input.v2'),'Unsupported input schema')
    painted=document['schema']=='toon-image-input.v2'
    paint_bytes=path.stat().st_size
    camera=dict(document['camera'])
    fail(camera.get('kind')=='ORTHO','Initial research backend supports only explicit ORTHO cameras')
    for key in ('width','height'):
        fail(type(camera[key]) is int and 0<camera[key]<=8192,'Invalid image '+key)
    fail(camera['width']*camera['height']<=16777216,'v1 image allocation budget is 16 megapixels')
    camera['world']=matrix(camera['world'],'camera.world')
    camera['projection']=matrix(camera['projection'],'camera.projection')
    fail(np.allclose(camera['world'][3],[0,0,0,1],atol=1e-12,rtol=0),'Invalid camera affine matrix')
    camera['clip_end']=float(camera.get('clip_end',100))
    camera['clip_start']=float(camera.get('clip_start',.01))
    fail(0<camera['clip_start']<camera['clip_end'],'Invalid clip range')
    ids=set()
    objects=[]
    hashes={str(path):digest_file(path)}
    for entry in document['objects']:
        fail(isinstance(entry['id'],str) and entry['id'] not in ids,'Object IDs must be unique strings')
        ids.add(entry['id'])
        source=Path(entry['file'])
        fail(source.is_absolute(),'Geometry files must be explicit absolute paths')
        actual=digest_file(source)
        fail(actual==entry['sha256'],'Geometry SHA mismatch: '+str(source))
        hashes[str(source)]=actual
        with np.load(source,allow_pickle=False) as archive:
            if painted:
                from .paint_attributes import metadata,archive_preflight,validate_arrays
                paint,paint_keys=metadata(entry)
                paint_bytes+=archive_preflight(archive)+source.stat().st_size
                fail(paint_bytes<=512*1024**2,'Paint input aggregate byte budget')
                fail(paint_keys<=set(archive.files),'Missing declared paint arrays')
            fail(all(k in archive for k in REQUIRED),'Missing geometry arrays: '+entry['id'])
            arrays={k:np.array(archive[k],copy=True) for k in REQUIRED}
            if 'triangle_polygon' in archive:
                arrays['triangle_polygon']=np.array(archive['triangle_polygon'],copy=True)
            if painted:
                arrays.update({k:np.array(archive[k],copy=True) for k in paint_keys})
        for k,a in arrays.items():
            fail(a.dtype.kind in 'biuf' and np.isfinite(a).all(),'Invalid numeric array '+k)
        for key in ('face_start','face_total','face_material'):
            fail(arrays[key].ndim==1 and arrays[key].dtype.kind in 'iu',
                 'Face topology arrays require one-dimensional integers: '+key)
        nv=len(arrays['co']);nf=len(arrays['face_start']);ne=len(arrays['edges']);nl=len(arrays['loop_vertex'])
        fail(arrays['co'].shape==(nv,3) and arrays['edges'].shape==(ne,2),'Invalid vertex/edge shapes')
        fail(arrays['triangles'].ndim==2 and arrays['triangles'].shape[1]==3,'Invalid triangles')
        fail(arrays['face_normal'].shape==(nf,3) and arrays['corner_normals'].shape==(nl,3),'Invalid normal shapes')
        fail(arrays['loop_edge'].shape==(nl,) and arrays['face_total'].shape==(nf,),'Invalid loop/face shapes')
        fail(arrays['face_smooth'].shape==arrays['face_material'].shape==(nf,),'Invalid face attributes')
        fail(arrays['edge_sharp'].shape==(ne,),'Invalid sharp edge count')
        if painted:validate_arrays(paint,arrays)
        fail(((arrays['face_start']>=0)&(arrays['face_start']<=nl)).all(),'Invalid face loop starts')
        fail(((arrays['face_total']>=3)&(arrays['face_total']<=nl)).all(),'Invalid face corner counts')
        fail(np.array_equal(arrays['face_start'],np.cumsum(np.r_[0,arrays['face_total'][:-1]])) and int(arrays['face_total'].sum())==nl,'Noncontiguous face loops')
        material_count=max(1,len(entry.get('material_culling',[])))
        fail(((arrays['face_material']>=0)&(arrays['face_material']<material_count)).all(),'Face material index outside declared slots')
        for key,bound in (('edges',nv),('loop_vertex',nv),('triangles',nv),('loop_edge',ne)):
            a=arrays[key]
            fail(a.dtype.kind in 'iu' and (a>=0).all() and (a<bound).all(),'Invalid indices: '+key)
        m=matrix(entry['matrix_world'],'object.matrix_world')
        fail(np.array_equal(m[3],[0,0,0,1]),'Object matrix is not affine')
        group=entry.get('outline_group')
        fail(group is None or (isinstance(group,str) and bool(group)), 'outline_group must be a nonempty string')
        loaded_object={'id':entry['id'],'name':entry.get('name',entry['id']),'arrays':arrays,'matrix_world':m,
                        'material_culling':entry.get('material_culling',[]),'source_sha256':actual,'outline_group':group}
        if painted:loaded_object['paint']=paint
        objects.append(loaded_object)
    fail(bool(objects),'No target geometry')
    settings=dict(document['settings'])
    fail(not(set(settings)-{'width_px','feather_px','visibility_step_px','ray_epsilon','color','samples_per_axis','include_open',
                           'line_categories','merge_outline_groups'}),'Unknown settings are not silently ignored')
    for key in ('width_px','feather_px','visibility_step_px','ray_epsilon'):
        settings[key]=float(settings[key])
        fail(np.isfinite(settings[key]) and settings[key]>=0,'Invalid '+key)
    fail(0<settings['width_px']<=256 and settings['visibility_step_px']>0 and settings['ray_epsilon']>0,'Invalid positive rendering parameter')
    color=np.asarray(settings['color'],dtype=np.float32)
    fail(color.shape==(4,) and np.isfinite(color).all() and ((color>=0)&(color<=1)).all(),'v1 color is straight linear RGBA in [0,1]')
    settings['color']=color
    fail(type(settings.get('samples_per_axis')) is int and settings['samples_per_axis'] in (1,2,4),'Sampling must be integer 1, 2 or 4 per axis')
    fail('include_open' not in settings or type(settings['include_open']) is bool,'include_open must be a boolean')
    categories=settings.get('line_categories')
    fail(categories is None or (isinstance(categories,list) and bool(categories)
         and all(x in ('outline','object') for x in categories) and len(set(categories))==len(categories)),
         'line_categories must be a unique nonempty list of outline/object')
    fail(type(settings.get('merge_outline_groups',False)) is bool,'merge_outline_groups must be boolean')
    return {'document':document,'objects':objects,'camera':camera,'settings':settings,'input_hashes':hashes}
