# SPDX-License-Identifier: GPL-3.0-or-later
"""Cut already styled full-parent primitives; retain source Alpha and dash phase."""
import numpy as np

ARRAYS=('projected','world','supports','widths','alphas','alpha_source_ids',
        'source_indices','source_mesh_indices','source_kinds','category_bits')


def cut(fragments,mesh,mesh_index,channel,partition,mvp,width,height,*,line_functions=False):
    indices=[];parameters=[];bits=[];classified=[]
    for ordinal,(edge,mask) in enumerate(zip(fragments['source_indices'],fragments['category_bits'])):
        regular=int(mask)&~3
        if regular:
            indices.append(ordinal);parameters.append((0.,1.));bits.append(regular);classified.append(False)
        categories=int(mask)&3
        if not categories:continue
        edge=int(edge);parent=mesh.vertices[mesh.edges[edge]].astype(np.float64)
        delta=parent[1]-parent[0];length2=float(delta@delta)
        if length2==0:continue
        u=(fragments['world'][ordinal]-parent[0])@delta/length2
        change=float(u[1]-u[0])
        if change<=0:continue
        channels=partition['intervals'].get((mesh_index,edge),{})
        entry=channels.get(channel,{})
        for name,bit in (('outline',1),('object',2)):
            if not categories&bit:continue
            spans=list(entry.get(name,()))
            if line_functions:
                spans=sorted(span for ch in ('VISIBLE','HIDDEN') for span in channels.get(ch,{}).get(name,()))
                merged=[]
                for lo,hi in spans:
                    if merged and lo<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],hi))
                    else:merged.append((lo,hi))
                spans=merged
            for lo,hi in spans:
                a=max(float(u[0]),float(lo));b=min(float(u[1]),float(hi))
                if b<=a:continue
                indices.append(ordinal);parameters.append(((a-u[0])/change,(b-u[0])/change))
                bits.append(bit);classified.append(True)
    ids=np.asarray(indices,np.int64);t=np.asarray(parameters,np.float64).reshape(-1,2)
    result=fragments.copy()
    for name in ARRAYS:
        if name in fragments:result[name]=fragments[name][ids].copy()
    result['preclassified']=np.asarray(classified,np.bool_)
    result['category_bits']=np.asarray(bits,np.uint16)
    if not len(ids):return result
    # Ordinary predicates retain their exact old primitive arrays. Only the
    # interval rows get new endpoints, all evaluated on their full styled parent.
    selected=np.flatnonzero(result['preclassified'])
    local=t[selected];old=fragments['world'][ids[selected]]
    world=old[:,0,None,:]+local[:,:,None]*(old[:,1]-old[:,0])[:,None,:]
    result['world'][selected]=world
    clip=np.concatenate((world,np.ones((*world.shape[:2],1))),axis=2)@np.asarray(mvp,np.float64).T
    if not np.isfinite(clip).all() or np.any(clip[:,:,3]<=0):raise ValueError('Object fragment requires finite positive-w geometry')
    xyz=clip[:,:,:3]/clip[:,:,3,None];xyz[:,:,:2]=(xyz[:,:,:2]+1)*np.array([width,height])*.5
    result['projected'][selected]=xyz.astype(np.float32)
    for name in ('widths','alphas'):
        values=fragments[name][ids[selected]]
        result[name][selected]=values[:,0,None]+local*(values[:,1]-values[:,0])[:,None]
    if line_functions:result['preclassified'][:]=False
    return result
