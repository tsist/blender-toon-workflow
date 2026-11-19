# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned-array closed AABB dual-tree traversal for viewport intersections.

Vectorizes tree levels and bounded leaf blocks. Same partition principle as the
project paged_candidates core; this is a preview-only in-memory implementation.
No triangle-geometry or adjacency filter is used in candidate enumeration.
"""
import numpy as np

def candidate_pages(bounds,stats,page_size=65536):
    bounds=np.asarray(bounds,np.float64)
    if bounds.ndim!=2 or bounds.shape[1]!=6 or not np.isfinite(bounds).all() or np.any(bounds[:,:3]>bounds[:,3:]):
        raise ValueError('Finite ordered [N,6] AABBs required')
    n=len(bounds)
    if n>1048576:raise ValueError('Viewport intersection triangle limit')
    stats.update(completed=False,node_pair_visits=0,leaf_pair_tests=0,candidate_pairs=0)
    if not n:stats['completed']=True;return
    # Breadth-wise construction; rows in each partition stay mutually disjoint.
    # Keep leaf partitions in each level so a single lexsort handles all active nodes.
    capacity=8;perm=np.arange(n,dtype=np.int32)
    lo=np.array([0]);hi=np.array([n]);ids=np.array([0])
    children=np.full((2*n-1,2),-1,np.int32)
    spans=np.zeros((2*n-1,2),np.int32);boxes=np.empty((2*n-1,6),np.float64)
    counts=np.zeros(2*n-1,np.int32);used=1;levels=[];depth=0
    while True:
        lengths=hi-lo;spans[ids]=np.column_stack((lo,hi));counts[ids]=lengths
        active=lengths>capacity
        if not active.any():break
        groups=np.repeat(np.arange(len(lo)),lengths)
        act=active[groups];axis=depth%3
        order=np.lexsort((perm,np.where(act,bounds[perm,axis+3],0.),
                              np.where(act,bounds[perm,axis],0.),groups))
        perm=perm[order]
        take=np.flatnonzero(active);parent=ids[take]
        left=np.arange(used,used+2*len(take),2);right=left+1;used+=2*len(take)
        children[parent,0]=left;children[parent,1]=right;levels.append(parent)
        middle=lo[take]+lengths[take]//2
        # Preserve partition order, inserting left/right children at active slots.
        positions=np.cumsum(np.where(active,2,1))-np.where(active,2,1)
        new_n=len(lo)+len(take)
        new_lo=np.empty(new_n,int);new_hi=new_lo.copy();new_ids=new_lo.copy()
        new_lo[positions]=lo;new_hi[positions]=hi;new_ids[positions]=ids
        new_lo[positions[take]]=lo[take];new_hi[positions[take]]=middle;new_ids[positions[take]]=left
        new_lo[positions[take]+1]=middle;new_hi[positions[take]+1]=hi[take];new_ids[positions[take]+1]=right
        lo,hi,ids=new_lo,new_hi,new_ids;depth+=1
    # Every final partition is one leaf, so reduceat is exactly its closed bounds.
    values=bounds[perm]
    boxes[ids,:3]=np.minimum.reduceat(values[:,:3],lo,axis=0)
    boxes[ids,3:]=np.maximum.reduceat(values[:,3:],lo,axis=0)
    for parent in reversed(levels):
        a,b=children[parent].T
        boxes[parent,:3]=np.minimum(boxes[a,:3],boxes[b,:3])
        boxes[parent,3:]=np.maximum(boxes[a,3:],boxes[b,3:])
    stats.update(tree_nodes=used,tree_depth=depth,tree_storage_bytes=children.nbytes+spans.nbytes+boxes.nbytes+counts.nbytes+perm.nbytes)
    todo=[np.array([[0,0]],np.int32)];grid0,grid1=np.indices((capacity,capacity))
    ix,jx=grid0.ravel(),grid1.ravel()
    while todo:
        pairs=todo.pop();u,v=pairs.T
        stats['node_pair_visits']+=len(pairs)
        if stats['node_pair_visits']>100000000:raise ValueError('Viewport node-pair limit')
        overlap=np.all((boxes[u,:3]<=boxes[v,3:])&(boxes[v,:3]<=boxes[u,3:]),axis=1)
        u,v=u[overlap],v[overlap]
        if not len(u):continue
        ul=children[u,0]<0;vl=children[v,0]<0
        same=(u==v)&~ul
        tasks=[]
        if same.any():
            a,b=children[u[same]].T
            tasks.append(np.concatenate((np.column_stack((a,a)),np.column_stack((a,b)),np.column_stack((b,b)))))
        branch=~same&~(ul&vl)
        if branch.any():
            a,b=u[branch],v[branch]
            split=~ul[branch]&(vl[branch]|(counts[a]>counts[b])|((counts[a]==counts[b])&(a<b)))
            first=np.where(split,children[a,0],a);second=np.where(split,b,children[b,0])
            third=np.where(split,children[a,1],a);fourth=np.where(split,b,children[b,1])
            tasks.append(np.concatenate((np.column_stack((first,second)),np.column_stack((third,fourth)))))
        for task in tasks:
            for start in range(0,len(task),4096):todo.append(task[start:start+4096])
        leaf=ul&vl
        if not leaf.any():continue
        a,b=u[leaf],v[leaf]
        ai=spans[a,0,None]+ix;bi=spans[b,0,None]+jx
        valid=(ai<spans[a,1,None])&(bi<spans[b,1,None])&((a[:,None]!=b[:,None])|(ai<bi))
        aa=perm[np.minimum(ai,n-1)];bb=perm[np.minimum(bi,n-1)]
        aa,bb=aa[valid],bb[valid];stats['leaf_pair_tests']+=len(aa)
        if stats['leaf_pair_tests']>100000000:raise ValueError('Viewport leaf-pair limit')
        valid=np.all((bounds[aa,:3]<=bounds[bb,3:])&(bounds[bb,:3]<=bounds[aa,3:]),axis=1)
        aa,bb=aa[valid],bb[valid]
        page=np.column_stack((np.minimum(aa,bb),np.maximum(aa,bb)))
        stats['candidate_pairs']+=len(page)
        if stats['candidate_pairs']>20000000:raise ValueError('Viewport candidate-pair limit')
        for start in range(0,len(page),page_size):yield page[start:start+page_size]
    stats['completed']=True
