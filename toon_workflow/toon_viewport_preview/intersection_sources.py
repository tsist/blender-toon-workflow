# SPDX-License-Identifier: GPL-3.0-or-later
"""World-space intersection segments for the finite-f64 viewport preview.

Reuses the project's closed-AABB dual-tree enumerator. Construction is float64,
not the exact backend. No coordinate rounding, screen bridge or length cutoff.
Point/coplanar contacts and complete coincident triangle edges are not strokes.
"""
import time
import numpy as np

_EXACT=None
def _exact_module():
    global _EXACT
    if _EXACT is None:
        from .dependencies import load
        _EXACT=load('geometry_exact')
    return _EXACT

def _exact_mask(point,triangle):
    from fractions import Fraction
    t=[tuple(Fraction.from_float(float(x)) for x in p) for p in triangle]
    for i,p in enumerate(t):
        if point==p:return 1<<i
    for i,j in ((0,1),(1,2),(2,0)):
        a=[point[k]-t[i][k] for k in range(3)]
        b=[t[j][k]-t[i][k] for k in range(3)]
        if all(a[k]*b[l]-a[l]*b[k]==0 for k,l in ((0,1),(1,2),(2,0))):
            return (1<<i)|(1<<j)
    return 7

def triangle_segments(A,B,diagnostics=None):
    """Return input row ordinals, finite segments, endpoint simplex masks.

    Masks have shape [segment, endpoint, support], bits name support vertices.
    They preserve topology for conservative degree-two chains, without welding.
    """
    A=np.asarray(A,np.float64);B=np.asarray(B,np.float64)
    if A.shape!=B.shape or A.ndim!=3 or A.shape[1:]!=(3,3):
        raise ValueError('Matching [N,3,3] triangles required')
    if not np.isfinite(A).all() or not np.isfinite(B).all():
        raise ValueError('Finite intersection triangles required')
    nA=np.cross(A[:,1]-A[:,0],A[:,2]-A[:,0])
    nB=np.cross(B[:,1]-B[:,0],B[:,2]-B[:,0])
    direction=np.cross(nA,nB)
    da=np.einsum('ni,nji->nj',nB,A-B[:,0,None,:])
    db=np.einsum('ni,nji->nj',nA,B-A[:,0,None,:])
    shared=np.all(A[:,:,None,:]==B[:,None,:,:],axis=-1)
    # Identity gives exact zero for a shared input corner, independent of arithmetic.
    da=np.where(shared.any(axis=2),0.,da)
    db=np.where(shared.any(axis=1),0.,db)
    shared_edge=(shared.any(axis=2).sum(axis=1)>=2)&(shared.any(axis=1).sum(axis=1)>=2)
    live=(np.any(direction!=0,axis=1)&~shared_edge&
          ~((da>0).all(axis=1)|(da<0).all(axis=1)|(db>0).all(axis=1)|(db<0).all(axis=1)))
    rows=np.flatnonzero(live)
    if not len(rows):return rows,np.empty((0,2,3)),np.empty((0,2,2),np.uint8)
    a,b=A[rows],B[rows];da,db=da[rows],db[rows]
    axis=np.abs(direction[rows]).argmax(axis=1)
    def cut(t,d):
        edge0=np.array([0,1,2]);edge1=np.array([1,2,0])
        x,y=d[:,edge0],d[:,edge1]
        cross=((x<0)&(y>0))|((x>0)&(y<0))
        u=np.divide(x,x-y,out=np.zeros_like(x),where=cross)
        points=np.concatenate((t,t[:,edge0]+u[:,:,None]*(t[:,edge1]-t[:,edge0])),axis=1)
        valid=np.concatenate((d==0,cross),axis=1)
        masks=np.broadcast_to(np.array([1,2,4,3,6,5],np.uint8),(len(t),6))
        value=np.take_along_axis(points,axis[:,None,None],axis=2)[:,:,0]
        low_i=np.where(valid,value,np.inf).argmin(axis=1)
        high_i=np.where(valid,value,-np.inf).argmax(axis=1)
        idx=np.arange(len(t))
        return (valid.any(axis=1),points[idx,low_i],points[idx,high_i],
                value[idx,low_i],value[idx,high_i],masks[idx,low_i],masks[idx,high_i])
    ca,cb=cut(a,da),cut(b,db)
    lo=np.maximum(ca[3],cb[3]);hi=np.minimum(ca[4],cb[4])
    keep=ca[0]&cb[0]&(hi>lo)&(ca[4]>ca[3])
    u=np.divide(np.stack((lo,hi),axis=1)-ca[3][:,None],(ca[4]-ca[3])[:,None],
                out=np.zeros((len(rows),2)),where=(ca[4]>ca[3])[:,None])
    points=ca[1][:,None,:]+u[:,:,None]*(ca[2]-ca[1])[:,None,:]
    endpoints=np.stack((lo,hi),axis=1)
    masks=np.full((len(rows),2,2),7,np.uint8)
    for side,c in enumerate((ca,cb)):
        masks[:,:,side]=np.where(endpoints==c[3][:,None],c[5][:,None],
                           np.where(endpoints==c[4][:,None],c[6][:,None],(c[5]|c[6])[:,None]))
    # Near-coincident interval boundaries only select an exact fallback; they never
    # snap endpoints, assign topology by distance, or delete a positive short line.
    magnitude=np.maximum(1.,np.max(np.abs(np.stack((ca[3],ca[4],cb[3],cb[4]),axis=1)),axis=1))
    guard=64*np.spacing(magnitude)
    near=np.zeros(len(rows),bool)
    for ai in (0,1):
        for bi in (0,1):
            x,y=ca[3+ai],cb[3+bi]
            ma,mb=ca[5+ai],cb[5+bi]
            # A common original corner already has exact input identity and masks.
            common_corner=np.all(ca[1+ai]==cb[1+bi],axis=1)&np.isin(ma,(1,2,4))&np.isin(mb,(1,2,4))
            near|=(abs(x-y)<=guard)&~common_corner
    shared_point=shared[rows].any(axis=(1,2))&((ca[3]==ca[4])|(cb[3]==cb[4]))
    ambiguous=ca[0]&cb[0]&near&~shared_point
    for j in np.flatnonzero(ambiguous):
        ex=_exact_module().triangle_intersection_exact(a[j],b[j])
        keep[j]=ex['kind']=='segment'
        if keep[j]:
            q=sorted(ex['vertices'],key=lambda p:p[int(axis[j])])
            points[j]=np.array([[float(x) for x in p] for p in q])
            if not np.any(points[j,0]!=points[j,1]):
                raise ArithmeticError('Positive exact intersection collapsed in viewport float64')
            for end,p in enumerate(q):
                masks[j,end]=[_exact_mask(p,a[j]),_exact_mask(p,b[j])]
    keep&=np.any(points[:,0]!=points[:,1],axis=1)
    if diagnostics is not None:diagnostics['exact_boundary_fallbacks']=int(np.count_nonzero(ambiguous))
    if not np.isfinite(points[keep]).all():raise ValueError('Nonfinite intersection construction')
    return rows[keep],points[keep],masks[keep]

def capture_steps(meshes):
    """Geometry-only capture with cooperative boundaries, never partial results.

    Every yielded value is progress only. The complete source set is returned
    once, after the same closed-AABB enumeration and final canonical sort.
    Closing this generator releases its private arrays without publishing them.
    """
    started=time.perf_counter();active_start=started;active_seconds=0.
    triangles=[];refs=[];vertices=[];global_tri=[];vo=0
    for mi,m in enumerate(meshes):
        triangles.append(m.vertices[m.triangles].astype(np.float64))
        refs.append(np.column_stack((np.full(len(m.triangles),mi),np.arange(len(m.triangles)))))
        vertices.append(m.triangles+vo);vo+=len(m.vertices)
        global_tri.append(np.arange(len(m.triangles))+m.face_offset)
    tris=np.concatenate(triangles) if triangles else np.empty((0,3,3))
    ref=np.concatenate(refs) if refs else np.empty((0,2),np.int64)
    vids=np.concatenate(vertices) if vertices else np.empty((0,3),np.int64)
    tags=np.concatenate(global_tri) if global_tri else np.empty(0,np.int64)
    bounds=np.concatenate((tris.min(axis=1),tris.max(axis=1)),axis=1)
    from .intersection_pairs import candidate_pages
    pair_stats={}
    stream=candidate_pages(bounds,pair_stats,page_size=8192)
    world=[];pairs=[];masks=[];candidate_count=0;fallbacks=0;segment_count=0
    active_seconds+=time.perf_counter()-active_start
    yield dict(phase='prepared',candidates=0,segments=0)
    active_start=time.perf_counter()
    for page in stream:
        p=page.astype(np.int64);candidate_count+=len(p)
        diagnostic={}
        rows,points,features=triangle_segments(tris[p[:,0]],tris[p[:,1]],diagnostic)
        fallbacks+=diagnostic.get('exact_boundary_fallbacks',0)
        if len(rows):
            world.append(points);pairs.append(p[rows]);masks.append(features)
            segment_count+=len(rows)
        active_seconds+=time.perf_counter()-active_start
        yield dict(phase='intersections',candidates=candidate_count,segments=segment_count)
        active_start=time.perf_counter()
    if not pair_stats['completed']:raise RuntimeError('Incomplete intersection candidate stream')
    pair=np.concatenate(pairs) if pairs else np.empty((0,2),np.int64)
    masks=np.concatenate(masks) if masks else np.empty((0,2,2),np.uint8)
    world=np.concatenate(world) if world else np.empty((0,2,3))
    order=np.lexsort((pair[:,1],pair[:,0]))
    pair,world,masks=pair[order],world[order],masks[order]
    tokens=[]
    for i,(a,b) in enumerate(pair):
        for end in range(2):
            feature=[]
            for side,tid in enumerate((a,b)):
                mask=masks[i,end,side]
                feature.append(tuple(sorted(int(v) for j,v in enumerate(vids[tid]) if mask&(1<<j))))
            token=tuple(sorted(feature))
            tokens.append(token)
    key_ids={token:i for i,token in enumerate(sorted(set(tokens)))}
    edges=np.array([key_ids[t] for t in tokens],np.int64).reshape((-1,2))
    return dict(world=world,pair=pair,simplex_masks=masks,supports=tags[pair].astype(np.float32),
        references=ref[pair],edges=edges,triangles=tris,triangle_references=ref,
        stats=dict(seconds=active_seconds+time.perf_counter()-active_start,
            wall_seconds=time.perf_counter()-started,triangles=len(tris),
            aabb_candidates=candidate_count,exact_boundary_fallbacks=fallbacks,pair_stats=pair_stats,segments=len(world),topology_endpoints=len(key_ids),
            candidate_complete=True,numeric='finite-f64 transverse segment preview',
            point_policy='suppress',coplanar_policy='suppress',full_edge_contact_policy='suppress',
            exact_backend_equivalent=False))

def capture(meshes):
    """Synchronous compatibility consumer of the identical source enumerator."""
    stream=capture_steps(meshes)
    while True:
        try:next(stream)
        except StopIteration as done:return done.value
