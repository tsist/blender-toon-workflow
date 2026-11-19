# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure captured-Mesh adapter to the affine Object/Outline interval core.

ORTHO is passed directly. PERSP is mapped to an affine screen/reciprocal-w
envelope so the same frozen-float64 interval kernel can be reused. An optional
``domain`` callable groups captured instances into one explicit object domain;
the default keeps every instance independent.
"""
from fractions import Fraction
import json
import numpy as np
from ._object_core.geometry import (_camera_parameters, _triangle_topology, classify_project,
                                    _depth_interval, _NUMERIC_EPS)
from ._object_core.analytic_visibility import visible_segments
from ._object_core.hidden_categories import partition_hidden_categories
from ._object_core.intervals import union_intervals


def _need(value, message):
    if not value:
        raise ValueError(message)


def _array(value, shape, label, integer=False):
    value=np.asarray(value)
    _need(value.shape==shape and value.dtype.kind in ('iu' if integer else 'iuf')
          and bool(np.isfinite(value).all()), 'Invalid Object '+label)
    return value


def _prepared(mesh, index, *, topology=None, world_face_normals=None, outline_group=None):
    vertices=np.asarray(mesh['vertices']);edges=np.asarray(mesh['edges']);triangles=np.asarray(mesh['triangles'])
    _need(vertices.ndim==2 and vertices.shape[1]==3 and edges.ndim==2 and edges.shape[1]==2
          and triangles.ndim==2 and triangles.shape[1]==3, 'Invalid Object geometry shapes')
    vertices=_array(vertices,vertices.shape,'vertices');edges=_array(edges,edges.shape,'edges',True)
    triangles=_array(triangles,triangles.shape,'triangles',True)
    nv=len(vertices);original=mesh['original_edge_count']
    _need(type(original) is int and 0<=original<=len(edges), 'Invalid Object original edge prefix')
    for name,values in (('edges',edges),('triangles',triangles)):
        _need(bool(np.all((values>=0)&(values<nv))), 'Object '+name+' index outside vertices')
    starts=np.asarray(mesh['face_start']);normals=np.asarray(mesh['normals'])
    _need(starts.ndim==1, 'Invalid Object polygon starts')
    starts=_array(starts,starts.shape,'face_start',True);nf=len(starts)
    normals=_array(normals,(nf,3),'polygon normals')
    if world_face_normals is not None:
        normals=_array(world_face_normals,(nf,3),'polygon normals')
    loop=np.asarray(mesh['loop_vertex']);_need(loop.ndim==1,'Invalid Object loops')
    loop=_array(loop,loop.shape,'loop_vertex',True)
    totals=np.diff(np.r_[starts,len(loop)]) if nf else np.empty(0,np.int64)
    _need((bool(np.all(totals>=3)) and starts[0]==0) if nf else len(loop)==0,
          'Invalid Object polygon loop ranges')
    _need(bool(np.all((loop>=0)&(loop<nv))), 'Object loop index outside vertices')
    materials=_array(mesh['face_material'],(nf,),'face_material',True)
    _need(bool(np.all(materials>=0)), 'Negative Object material ID')
    mapping=_array(mesh['triangle_polygons'],(len(triangles),),'triangle_polygons',True)
    _need(bool(np.all((mapping>=0)&(mapping<nf))), 'Object triangle polygon outside mesh')
    following=np.arange(len(loop))+1
    if nf:following[starts+totals-1]=starts
    if 'loop_edge' in mesh:
        loop_edge=_array(mesh['loop_edge'],(len(loop),),'loop_edge',True)
        _need(bool(np.all((loop_edge>=0)&(loop_edge<original))), 'Object loop edge outside original prefix')
        if len(loop):
            expected=np.sort(np.column_stack((loop,loop[following])),axis=1)
            _need(np.array_equal(np.sort(edges[loop_edge],axis=1),expected),'Object loop edge ownership differs')
    else:
        lookup={}
        for ei,pair in enumerate(edges[:original]):
            key=tuple(sorted(map(int,pair)))
            _need(key not in lookup,'Duplicate original edge needs explicit loop_edge')
            lookup[key]=ei
        keys=[tuple(sorted((int(a),int(b)))) for a,b in zip(loop,loop[following])]
        _need(all(k in lookup for k in keys),'Object polygon boundary absent from original edges')
        loop_edge=np.asarray([lookup[k] for k in keys],np.int64)
    if topology is None:
        topology=_triangle_topology(vertices,triangles)
    delta=vertices[edges[:original,1]]-vertices[edges[:original,0]]
    # All positions/normals are already captured in world space. No invented
    # authored corner normals or reconstructed raw Blender attributes are used.
    arrays=dict(co=vertices,edges=edges[:original],triangles=triangles,triangle_polygon=mapping,
                face_start=starts,loop_edge=loop_edge,face_material=materials)
    key=mesh.get('instance_key')
    object_id='mesh:'+str(index) if key is None else 'instance:'+json.dumps(key,separators=(',',':'),allow_nan=False)
    row=dict(id=object_id,arrays=arrays,world_vertices=vertices,world_face_normals=normals,
                material_culling=[False]*max(1,int(materials.max(initial=0))+1),
                _triangle_topology=topology,_geometry_diagnostics=dict(
                    zero_length_edge_ids=np.flatnonzero(np.all(delta==0,axis=1)).tolist(),
                    zero_normal_face_ids=np.flatnonzero(np.linalg.norm(normals,axis=1)==0).tolist(),
                    negative_transform_determinant=False))
    if outline_group is not None:
        row['outline_group']=outline_group
    return row


def _parents(obj, mesh, toward):
    topology=obj['_triangle_topology'];keys=topology['keys'];inverse=topology['inverse']
    valid=topology['valid'];facing=topology['normals']@toward
    front=(facing>_NUMERIC_EPS)&valid;back=~front&valid
    fc=np.bincount(inverse,weights=np.repeat(front,3),minlength=len(keys))
    bc=np.bincount(inverse,weights=np.repeat(back,3),minlength=len(keys))
    valid_count=np.bincount(inverse,weights=np.repeat(valid,3),minlength=len(keys))
    silhouette=(fc>0)&(bc>0)
    opened=(topology['counts']==1)&(valid_count==1)
    candidate=silhouette|opened
    edges=np.asarray(mesh['edges']);nv=len(mesh['vertices'])
    pairs=np.sort(edges,axis=1);edge_keys=pairs[:,0].astype(np.int64)*nv+pairs[:,1]
    rank=np.searchsorted(keys,edge_keys);present=rank<len(keys)
    if len(keys):present&=keys[np.minimum(rank,len(keys)-1)]==edge_keys
    parent=np.zeros(len(edges),bool);is_open=parent.copy()
    parent[present]=candidate[rank[present]]
    is_open[present]=opened[rank[present]]&~silhouette[rank[present]]
    _need(set(map(int,keys[candidate]))<=set(map(int,edge_keys)), 'Object candidate missing from captured virtual edges')
    return parent,is_open


def _selector_key(ref):
    _need(type(ref) is dict and set(ref)=={'name','library'},
          'Merge domain collection requires name and library identity')
    _need(type(ref['name']) is str and 0<len(ref['name'])<=1024,
          'Invalid merge domain collection name')
    _need(ref['library'] is None or type(ref['library']) is str and 0<len(ref['library'])<=8192,
          'Invalid merge domain library')
    return json.dumps(ref,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def _merge_declaration(merge_domains):
    if merge_domains is None:
        return None,None
    _need(type(merge_domains) in (list,tuple),'merge_domains must be a list or tuple')
    declared=[];refs={}
    for spec in merge_domains:
        _need(type(spec) is dict and set(spec)=={'id','collections'},
              'Merge domain requires id and collections')
        did=spec['id']
        _need(type(did) is str and bool(did) and did not in declared,
              'Merge domain ids must be unique nonempty strings')
        _need(type(spec['collections']) in (list,tuple),
              'Merge domain collections must be a list or tuple')
        for ref in spec['collections']:
            key=_selector_key(ref)
            _need(key not in refs,'Collection belongs to more than one merge domain')
            refs[key]=did
        declared.append(did)
    return tuple(declared),refs


def _mesh_merge_domain(mesh,index,declared,refs):
    value=mesh.get('merge_domain')
    if value is not None:
        _need(type(value) is str and bool(value),
              'mesh.merge_domain must be a nonempty string or None')
        if declared is not None:
            _need(value in declared,'mesh.merge_domain is not declared')
        return value
    if refs is None:
        return None
    meta=mesh.get('selector_meta')
    if not isinstance(meta,dict) or 'collections' not in meta:
        return None
    collections=meta['collections']
    if isinstance(collections,str):
        # Accept either one canonical selector JSON string or a JSON list of them.
        text=collections.strip()
        if text.startswith('['):
            try:
                collections=json.loads(text)
            except ValueError as error:
                raise ValueError('Invalid captured collection list JSON') from error
        else:
            collections=(collections,)
    _need(type(collections) in (list,tuple),
          'mesh.selector_meta.collections must be a list or tuple')
    matched=set()
    for raw in collections:
        _need(type(raw) is str,'Captured collection identity must be a JSON string')
        try:
            ref=json.loads(raw)
        except ValueError as error:
            raise ValueError('Invalid captured collection identity JSON') from error
        did=refs.get(_selector_key(ref))
        if did is not None:
            matched.add(did)
    _need(len(matched)<=1,'Mesh collections resolve to multiple merge domains')
    return next(iter(matched)) if matched else None


def _perspective_parameters(view,mvp):
    """Validate a standard perspective camera and return its view depth range."""
    world=np.linalg.inv(view)
    projection=mvp@world
    _need(bool(np.isfinite(projection).all()),'Object perspective projection is not finite')
    _need(np.array_equal(world[3],[0,0,0,1]),'Object camera world matrix must be affine')
    rotation=world[:3,:3]
    pose_eps=64*np.finfo(np.float32).eps
    _need(bool(np.allclose(rotation.T@rotation,np.eye(3),atol=pose_eps,rtol=0))
          and np.linalg.det(rotation)>0,'Object perspective camera pose must be rigid')
    eps=_NUMERIC_EPS
    _need(abs(projection[3,0])<=eps and abs(projection[3,1])<=eps
          and abs(projection[3,3])<=eps and projection[3,2]<-eps,
          'Object perspective projection is not a standard camera')
    _need(abs(projection[2,2])>eps,'Object perspective depth projection is singular')
    near_denominator=projection[2,2]+projection[3,2]
    far_denominator=projection[2,2]-projection[3,2]
    _need(abs(near_denominator)>eps and abs(far_denominator)>eps,
          'Object perspective clip range is not finite')
    z_near=(-projection[2,3]-projection[3,3])/near_denominator
    z_far=(projection[3,3]-projection[2,3])/far_denominator
    near,far=-z_near,-z_far
    _need(bool(np.isfinite([near,far]).all()) and 0<near<far,
          'Object perspective clip range is invalid')
    w_near=projection[3,2]*z_near+projection[3,3]
    w_far=projection[3,2]*z_far+projection[3,3]
    _need(bool(np.isfinite([w_near,w_far]).all()) and 0<w_near<=w_far,
          'Object perspective eye depth range is invalid')
    return dict(world=world,projection=projection,near=near,far=far,
                w_near=w_near,w_far=w_far,camera_origin=world[:3,3].copy())


def _perspective_topology(mesh,synthetic_vertices,camera_origin):
    triangles=np.asarray(mesh['triangles'])
    topology=_triangle_topology(synthetic_vertices,triangles)
    if not len(triangles):
        return topology
    vertices=np.asarray(mesh['vertices'],dtype=np.float64)
    points=vertices[triangles]
    normals=np.cross(points[:,1]-points[:,0],points[:,2]-points[:,0])
    lengths=np.linalg.norm(normals,axis=1)
    valid=lengths>0
    facing=np.zeros(len(triangles),dtype=np.float64)
    if bool(valid.any()):
        unit=np.zeros_like(normals)
        unit[valid]=normals[valid]/lengths[valid,None]
        to_camera=camera_origin-points[valid].mean(axis=1)
        distance=np.linalg.norm(to_camera,axis=1)
        good=distance>0
        direction=np.zeros_like(to_camera)
        direction[good]=to_camera[good]/distance[good,None]
        facing[valid]=np.einsum('ij,ij->i',unit[valid],direction)
    topology=dict(topology)
    topology['normals']=np.column_stack((np.zeros(len(triangles)),np.zeros(len(triangles)),facing))
    topology['valid']=valid
    topology['zero_area_triangle_ids']=np.flatnonzero(~valid).tolist()
    return topology


def _perspective_face_normals(mesh,camera_origin):
    starts=np.asarray(mesh['face_start'],dtype=np.int64)
    loops=np.asarray(mesh['loop_vertex'],dtype=np.int64)
    normals=np.asarray(mesh['normals'],dtype=np.float64)
    if not len(starts):
        return np.empty((0,3),dtype=np.float64)
    totals=np.diff(np.r_[starts,len(loops)])
    vertices=np.asarray(mesh['vertices'],dtype=np.float64)
    centers=np.add.reduceat(vertices[loops],starts,axis=0)/totals[:,None]
    to_camera=camera_origin-centers
    distance=np.linalg.norm(to_camera,axis=1)
    direction=np.zeros_like(to_camera)
    good=distance>0
    direction[good]=to_camera[good]/distance[good,None]
    length=np.linalg.norm(normals,axis=1)
    facing=np.zeros(len(starts),dtype=np.float64)
    good&=length>0
    facing[good]=np.einsum('ij,ij->i',normals[good]/length[good,None],direction[good])
    return np.column_stack((np.zeros(len(starts)),np.zeros(len(starts)),facing))


def _clip_polygon_depth(points,depths,bound,keep_above):
    """Sutherland-Hodgman clip of a 3D polygon against one view-depth plane."""
    result=[]
    count=len(points)
    for index in range(count):
        p0,p1=points[index],points[(index+1)%count]
        d0,d1=depths[index],depths[(index+1)%count]
        inside0=(d0>=bound) if keep_above else (d0<=bound)
        inside1=(d1>=bound) if keep_above else (d1<=bound)
        if inside0:
            result.append((p0,d0))
        if inside0 != inside1:
            if d0==d1:
                continue
            exact=((Fraction.from_float(float(bound))-Fraction.from_float(float(d0)))
                   /(Fraction.from_float(float(d1))-Fraction.from_float(float(d0))))
            t=float(exact)
            t=min(1.0,max(0.0,t))
            point=p0+t*(p1-p0)
            result.append((point,bound))
    deduplicated=[]
    for point,depth in result:
        if not deduplicated or not np.array_equal(point,deduplicated[-1][0]):
            deduplicated.append((point,depth))
    if len(deduplicated)>1 and np.array_equal(deduplicated[0][0],deduplicated[-1][0]):
        deduplicated.pop()
    return deduplicated


def _clip_triangle_depth(points,depths,near,far):
    if bool(np.all(depths>far)) or bool(np.all(depths<near)):
        return []
    polygon=[(points[i],depths[i]) for i in range(3)]
    polygon=_clip_polygon_depth([p for p,d in polygon],[d for p,d in polygon],near,True)
    if len(polygon)<3:
        return []
    polygon=_clip_polygon_depth([p for p,d in polygon],[d for p,d in polygon],far,False)
    return [p for p,d in polygon] if len(polygon)>=3 else []


def _perspective_index_objects(meshes,prepared,parameters,w_scale,mvp):
    """Build near/far-clipped occluder geometry with original support identities."""
    index_objects=[]
    for prepared_object,mesh in zip(prepared,meshes):
        vertices=np.asarray(mesh['vertices'],dtype=np.float64)
        homogeneous=np.column_stack((vertices,np.ones(len(vertices))))
        clip=homogeneous@mvp.T
        w=clip[:,3]
        triangles=np.asarray(mesh['triangles'],dtype=np.int64)
        original_count=len(vertices)
        clipped_positions=[]
        clipped_triangles=[]
        clipped_support=[]
        clipped_source_triangles=[]
        for triangle_index,triangle in enumerate(triangles):
            points=vertices[triangle]
            depths=w[triangle]
            polygon=_clip_triangle_depth(points,depths,
                                         float(parameters['w_near']),float(parameters['w_far']))
            if len(polygon)<3:
                continue
            for fan in range(1,len(polygon)-1):
                fan_points=[polygon[0],polygon[fan],polygon[fan+1]]
                homogeneous_fan=np.column_stack((np.asarray(fan_points),np.ones(3)))
                fan_clip=homogeneous_fan@mvp.T
                fan_w=fan_clip[:,3]
                _need(bool(np.isfinite(fan_clip).all()) and bool(np.all(fan_w>0)),
                      'Object perspective clipped occluder has nonpositive homogeneous w')
                ndc=fan_clip[:,:2]/fan_w[:,None]
                synthetic_z=w_scale/fan_w-2.0
                base=original_count+len(clipped_positions)
                clipped_positions.extend(np.column_stack((ndc,synthetic_z)))
                clipped_triangles.append([base,base+1,base+2])
                clipped_support.append(triangle.tolist())
                clipped_source_triangles.append(triangle_index)
        world_vertices=np.concatenate((np.asarray(prepared_object['world_vertices']),
                                       np.asarray(clipped_positions).reshape(-1,3)))
        row=dict(id=prepared_object['id'],
                 arrays=dict(triangles=np.asarray(clipped_triangles,dtype=np.int64).reshape(-1,3),
                             face_material=np.zeros(len(clipped_triangles),dtype=np.int64),
                             triangle_polygon=np.zeros(len(clipped_triangles),dtype=np.int64),
                             edges=np.asarray(mesh['edges'],dtype=np.int64)),
                 world_vertices=world_vertices,
                 material_culling=[False],
                 index_support_vertices=np.asarray(clipped_support,dtype=np.int64).reshape(-1,3),
                 index_source_triangles=np.asarray(clipped_source_triangles,dtype=np.int64))
        if prepared_object.get('outline_group') is not None:
            row['outline_group']=prepared_object['outline_group']
        index_objects.append(row)
    return index_objects


def _perspective_prepared(meshes,view,mvp,width,height,domain_keys):
    """Map captured perspective geometry to the affine screen-depth kernel.

    The kernel receives NDC x/y and a monotone affine function of reciprocal
    clip-w. Along any projected line both coordinates and 1/w are affine in the
    screen parameter, so triangle halfplanes and depth gaps remain exact in the
    frozen float64 input domain. Source edges and occluder triangles are clipped
    against near/far. Unreferenced behind-eye vertices are placeholders only.
    """
    parameters=_perspective_parameters(view,mvp)
    clips=[]
    for mesh in meshes:
        vertices=np.asarray(mesh['vertices'],dtype=np.float64)
        homogeneous=np.column_stack((vertices,np.ones(len(vertices))))
        clip=homogeneous@mvp.T
        _need(bool(np.isfinite(clip).all()),'Object perspective clip coordinates are not finite')
        clips.append(clip)
    w_min=float(parameters['w_near'])
    prepared=[]
    for index,(mesh,clip) in enumerate(zip(meshes,clips)):
        w=clip[:,3]
        safe_w=np.where(w>0,w,1.0)
        ndc=clip[:,:2]/safe_w[:,None]
        ndc=np.where((w>0)[:,None],ndc,0.0)
        synthetic_z=np.where(w>0,w_min/safe_w-2.0,-2.0)
        synthetic_vertices=np.column_stack((ndc,synthetic_z))
        topology=_perspective_topology(mesh,synthetic_vertices,parameters['camera_origin'])
        face_normals=_perspective_face_normals(mesh,parameters['camera_origin'])
        synthetic_mesh=dict(mesh)
        synthetic_mesh['vertices']=synthetic_vertices
        prepared.append(_prepared(
            synthetic_mesh,index,topology=topology,world_face_normals=face_normals,
            outline_group=domain_keys[index]))
    index_objects=_perspective_index_objects(meshes,prepared,parameters,w_min,mvp)
    camera=dict(kind='ORTHO',world=np.eye(4),projection=np.eye(4),width=width,height=height,
                margin_px=0,clip_start=0.0,clip_end=3.0)
    return prepared,camera,parameters,w_min,index_objects


def _perspective_sources(records,meshes,object_mesh,mvp,parameters,w_min,width,height):
    """Clip each source against near/far and install reciprocal-w screen data.

    ``window`` supplies the clipped absolute source-u interval and endpoint
    clip-w values, so phase and paint receive the same source-u spans.
    Core records and their witnesses remain in this internal screen-parameter
    domain. Only the final adapter intervals are remapped; no screen-domain
    witness is returned as a captured-source proof.
    """
    window={};filtered=[];stats=dict(clipped=0,rejected=0)
    for record in records:
        mi=object_mesh[record['object_id']]
        vertices=np.asarray(meshes[mi]['vertices'],dtype=np.float64)
        v0,v1=map(int,record['vertex_ids'])
        endpoints=vertices[[v0,v1]]
        homogeneous=np.column_stack((endpoints,np.ones(2)))
        clip=homogeneous@mvp.T
        depths=clip[:,3]
        interval=_depth_interval(depths,float(parameters['w_near']),float(parameters['w_far']))
        if interval is None or interval[1]<=interval[0]:
            stats['rejected']+=1
            continue
        t0,t1=interval
        if t0!=0.0 or t1!=1.0:
            stats['clipped']+=1
        clipped=endpoints[0]+np.asarray([t0,t1])[:,None]*(endpoints[1]-endpoints[0])
        clipped_homogeneous=np.column_stack((clipped,np.ones(2)))
        clipped_clip=clipped_homogeneous@mvp.T
        w=clipped_clip[:,3]
        _need(bool(np.isfinite(clipped_clip).all()) and bool(np.all(w>0)),
              'Object perspective clipped source has nonpositive homogeneous w')
        ndc=clipped_clip[:,:2]/w[:,None]
        record['world']=np.column_stack((ndc,w_min/w-2.0))
        record['xy']=(ndc+1.0)*[width/2,height/2]
        record['depth']=2.0-w_min/w
        record['u0'],record['u1']=0.0,1.0
        window[(record['object_id'],record['edge_id'])]=(float(t0),float(t1),float(w[0]),float(w[1]))
        filtered.append(record)
    return filtered,window,stats


def _perspective_source_u(value,u0,u1,w0,w1):
    """Map a screen-parameter interval back to absolute original source-u."""
    _need(np.isfinite(value) and 0<=value<=1,'Invalid Object screen parameter')
    # t = s*w0 / (w0 + (1-s)*(w1-w0)); s=0 -> t=0, s=1 -> t=1.
    denominator=w0+(1-value)*(w1-w0)
    _need(np.isfinite(denominator) and denominator>0,
          'Object perspective reciprocal-w interpolation is singular')
    t=value*w0/denominator
    return u0+t*(u1-u0)


def _perspective_source_span(a,b,u0,u1,w0,w1):
    """Keep ordered screen endpoints ordered without a geometric tolerance.

    Independently rounded rational evaluations can reverse adjacent float64
    endpoints by one ulp. Preserve the historical values on the ordinary path;
    only an invalid mapped span is recomputed exactly from its captured floats.
    Correct rounding of that monotone rational map cannot reverse the interval.
    A genuinely reversed input remains an error, never a request to sort it.
    """
    _need(a<=b,'Invalid Object screen interval order')
    lo=_perspective_source_u(a,u0,u1,w0,w1)
    hi=_perspective_source_u(b,u0,u1,w0,w1)
    if u0<=lo<=hi<=u1:
        return lo,hi
    _need(np.isfinite([u0,u1,w0,w1]).all() and 0<=u0<=u1<=1
          and w0>0 and w1>0,'Invalid Object perspective source window')
    start,end,x,y=(Fraction(float(v)) for v in (u0,u1,w0,w1))
    def exact(value):
        s=Fraction(float(value))
        return start+(end-start)*s*x/(s*x+(1-s)*y)
    lo,hi=exact(a),exact(b)
    _need(start<=lo<=hi<=end,'Invalid exact Object source-u span')
    return float(lo),float(hi)


def classify(meshes,view,mvp,width,height,perspective,merge_domains=None,merge_outline=False,margin_px=0.):
    """Full-parent masks plus V/H category spans in captured edge source-u.

    ``merge_domains`` is the explicit line-hierarchy declaration
    ``[{id,collections:[{name,library}]}]``; per-mesh ``merge_domain`` or the
    canonical ``selector_meta.collections`` identities resolve the domain.
    ``merge_outline=True`` consumes those domains for Visible and Hidden
    Outline/Object classification; False keeps independent object instances.
    """
    _need(type(perspective) is bool,'Object perspective must be boolean')
    _need(type(merge_outline) is bool,'merge_outline must be boolean')
    _need(type(margin_px) in (int,float) and np.isfinite(margin_px) and margin_px>=0,'Invalid Object off-screen margin')
    _need(type(meshes) in (list,tuple),'Object meshes must be a sequence')
    view=_array(view,(4,4),'view').astype(np.float64,copy=False)
    mvp=_array(mvp,(4,4),'mvp').astype(np.float64,copy=False)
    declared,refs=_merge_declaration(merge_domains)
    domain_keys=[_mesh_merge_domain(mesh,i,declared,refs) for i,mesh in enumerate(meshes)]
    merge=bool(merge_outline and any(key is not None for key in domain_keys))
    prepared_groups=[key if merge else None for key in domain_keys]
    index_objects=None
    if perspective:
        prepared,camera,perspective_parameters,w_min,index_objects=_perspective_prepared(
            meshes,view,mvp,width,height,prepared_groups)
    else:
        world=np.linalg.inv(view)
        camera=dict(kind='ORTHO',world=world,projection=mvp@world,width=width,height=height,margin_px=0)
        camera['clip_depth_policy']='signed_ortho_view'
        perspective_parameters=None;w_min=None
        prepared=[_prepared(mesh,i,outline_group=prepared_groups[i]) for i,mesh in enumerate(meshes)]
    camera['margin_px']=float(margin_px)
    camera['projected_tangent_policy']='nonblocking'
    cp=_camera_parameters(camera)
    domain_indices={obj['id']:i for i,obj in enumerate(prepared)}
    _need(len(domain_indices)==len(prepared),'Duplicate Object instance identity in captured geometry')
    parents=[];opened=[]
    for obj,mesh in zip(prepared,meshes):
        a,b=_parents(obj,mesh,cp['toward_camera']);parents.append(a);opened.append(b)
    sources,source_report=classify_project(prepared,camera,include_open=True)
    perspective_window={}
    perspective_source_stats=None
    if perspective:
        sources,perspective_window,perspective_source_stats=_perspective_sources(
            sources,meshes,domain_indices,mvp,perspective_parameters,w_min,width,height)
    visible,v_report=visible_segments(prepared,sources,camera,line_categories=['outline','object'],
                                      merge_outline_groups=merge,index_objects=index_objects)
    hidden,h_report=partition_hidden_categories(
        prepared,sources,camera,merge=merge,
        identity_policy='explicit_domain' if merge else 'object_id_only',
        index_objects=index_objects,witness_profile='cover')
    virtual=[]
    for mesh in meshes:
        count=mesh['original_edge_count'];rows={}
        for ei,pair in enumerate(np.asarray(mesh['edges'])[count:],start=count):
            key=tuple(sorted(map(int,pair)))
            _need(key not in rows,'Duplicate captured virtual edge')
            rows[key]=ei
        virtual.append(rows)
    intervals={}
    for row,channel in [(r,'VISIBLE') for r in visible]+[(r,'HIDDEN') for r in hidden if r['visibility']=='hidden']:
        mi=domain_indices[row['object_id']];source_eid=row['edge_id'];eid=row['original_edge_id']
        if eid is None:
            pair=tuple(sorted(map(int,row['vertex_ids'])))
            _need(pair in virtual[mi],'Object virtual source has no captured edge')
            eid=virtual[mi][pair]
        pair=tuple(map(int,np.asarray(meshes[mi]['edges'])[eid]));source_pair=tuple(map(int,row['vertex_ids']))
        _need(pair==source_pair or pair==source_pair[::-1],'Object source vertex orientation differs')
        a,b=float(row['u0']),float(row['u1'])
        if perspective:
            key=(row['object_id'],source_eid)
            _need(key in perspective_window,'Object perspective source is missing its window')
            u0,u1,w0,w1=perspective_window[key]
            a,b=_perspective_source_span(a,b,u0,u1,w0,w1)
        if pair!=source_pair:a,b=1-b,1-a
        _need(0<=a<=b<=1 and np.isfinite([a,b]).all(),
              'Invalid Object source-u span '+repr((a,b,row['u0'],row['u1'],source_eid,perspective)))
        if a==b:continue
        entry=intervals.setdefault((mi,int(eid)),{ch:{c:[] for c in ('outline','object')} for ch in ('VISIBLE','HIDDEN')})
        category=row['line_type'] if channel=='VISIBLE' else row['category']
        entry[channel][category].append((a,b))
    for entry in intervals.values():
        for values in entry.values():
            for category,spans in values.items():values[category]=union_intervals(spans,max_pieces=100000)
    scope=('autonomous PERSP reciprocal-w opaque double-sided; merge='+str(merge) if perspective else
           'autonomous ORTHO opaque double-sided '+
           ('explicit_domain' if merge else 'object_id_only')+'; merge='+str(merge))
    domains=[]
    for i,obj in enumerate(prepared):
        row=dict(mesh_index=i,object_id=obj['id'],instance_key=meshes[i].get('instance_key'))
        if domain_keys[i] is not None:row['merge_domain']=domain_keys[i]
        domains.append(row)
    report=dict(
        scope=scope,native_equivalent=False,parent_counts=[int(a.sum()) for a in parents],
        domains=domains,
        prepared_domain=('captured perspective screen NDC and reciprocal clip-w; '
                         'source edges and occluder triangles clipped against near/far' if perspective else
                         'captured world geometry; identity transform, original transform diagnostic not reconstructed'),
        candidate_scope='current_m1_silhouette_open; full parent masks before clipping',
        source=source_report,visible=v_report,hidden=h_report)
    if perspective:
        report['perspective']=dict(
            domain='screen-space affine x/y and reciprocal clip-w',
            near=float(perspective_parameters['near']),far=float(perspective_parameters['far']),
            w_near=float(perspective_parameters['w_near']),w_far=float(perspective_parameters['w_far']),
            w_scale=float(w_min),u_map='projective t=s*w0/(w0+(1-s)*(w1-w0))',
            source_clipping=perspective_source_stats,
            limitation='source edges and occluder triangles are clipped against near/far; unreferenced behind-eye vertices are ignored')
    if merge_domains is not None or merge_outline:
        report['merge']=dict(declared=None if declared is None else list(declared),
                             merge_outline=merge_outline,applied=merge,
                             domains=[dict(mesh_index=i,object_id=obj['id'],merge_domain=domain_keys[i])
                                      for i,obj in enumerate(prepared)])
    return dict(parents=parents,opened=opened,intervals=intervals,report=report)
