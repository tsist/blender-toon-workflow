# SPDX-License-Identifier: GPL-3.0-or-later
"""Balanced world-triangle BVH for direct line-center visibility queries.

Median splits bound traversal depth. This is a float32 preview query, not the
historical exact-rational visibility contract. No external code is copied.
"""
import numpy as np


def build_bvh(meshes, leaf_size=8):
    job=build_bvh_steps(meshes,leaf_size)
    while True:
        try:next(job)
        except StopIteration as done:return done.value

def build_bvh_steps(meshes, leaf_size=8):
    triangles=[];tags=[]
    for mesh in meshes:
        triangles.append(mesh.vertices[mesh.triangles])
        tags.append(np.arange(len(mesh.triangles),dtype=np.int32)+mesh.face_offset)
        yield dict(stage='visibility-input',meshes_done=len(triangles))
    vertices=np.concatenate(triangles) if triangles else np.empty((0,3,3),np.float32)
    tags=np.concatenate(tags) if tags else np.empty(0,np.int32)
    low=vertices.min(axis=1) if len(vertices) else np.empty((0,3))
    high=vertices.max(axis=1) if len(vertices) else np.empty((0,3))
    centers=(low+high)*.5
    nodes=[];order=[];visits=0

    def split(indices,depth):
        nonlocal visits
        visits+=1
        if visits%128==0:yield dict(stage='visibility-tree',nodes=visits)
        if depth>=31:raise ValueError('Visibility BVH exceeds traversal stack')
        index=len(nodes);node=np.zeros((2,4),np.float32);nodes.append(node)
        node[0,:3]=low[indices].min(axis=0);node[1,:3]=high[indices].max(axis=0)
        if len(indices)<=leaf_size:
            node[0,3]=len(order);node[1,3]=-len(indices);order.extend(indices)
        else:
            axis=np.ptp(centers[indices],axis=0).argmax()
            pivot=len(indices)//2
            ranked=indices[np.argpartition(centers[indices,axis],pivot)]
            node[0,3]=yield from split(ranked[:pivot],depth+1)
            node[1,3]=yield from split(ranked[pivot:],depth+1)
        return index

    if len(vertices):yield from split(np.arange(len(vertices)),0)
    order=np.asarray(order,np.int32)
    packed=np.ones((len(order),3,4),np.float32)
    packed[:,:,:3]=vertices[order]
    packed[:,0,3]=tags[order]
    return np.asarray(nodes,np.float32).reshape((-1,4)),packed.reshape((-1,4))


GLSL = '''
vec4 tableValue(sampler2D tableImage,int index){
    int width=textureSize(tableImage,0).x;
    return texelFetch(tableImage,ivec2(index%width,index/width),0);
}
bool boxHit(vec3 origin,vec3 direction,vec3 lo,vec3 hi,float limit){
    float enter=0.0;float leave=limit;
    for(int axis=0;axis<3;axis++){
        if(abs(direction[axis])<1e-20){
            if(origin[axis]<lo[axis] || origin[axis]>hi[axis])return false;
        }else{
            float a=(lo[axis]-origin[axis])/direction[axis];
            float b=(hi[axis]-origin[axis])/direction[axis];
            enter=max(enter,min(a,b));leave=min(leave,max(a,b));
            if(enter>leave)return false;
        }
    }
    return true;
}
int lineState(vec2 center,vec3 point,vec2 support){
    if(nodeCount==0)return 1;
    vec2 ndc=center/size*2.0-1.0;
    vec4 nearH=inverseMvp*vec4(ndc,-1.0,1.0);
    vec3 nearPoint=nearH.xyz/nearH.w;
    vec3 direction=rayCamera.w>0.0?normalize(point-rayCamera.xyz):rayForward.xyz;
    vec3 lo=tableValue(bvhNodes,0).xyz;
    vec3 hi=tableValue(bvhNodes,1).xyz;
    // Anchor the ray at the source, then move only to the geometry front.
    // A distant orthographic clip plane must not magnify query error/bias.
    float travel=dot(point-(lo+hi)*0.5,direction)+dot(abs(direction),(hi-lo)*0.5);
    if(travel<0.0)return 1;
    float margin=1e-5*max(1.0,length(hi-lo));
    float distance=min(dot(point-nearPoint,direction),travel+margin);
    vec3 origin=point-direction*distance;
    // Small world-space rounding envelope, independent of triangle slope.
    float limit=distance-1e-6*max(1.0,length(point-origin));
    bool blocked=false;
    int stack[32];int count=1;stack[0]=0;
    while(count>0){
        int node=stack[--count];
        vec4 lo=tableValue(bvhNodes,node*2);
        vec4 hi=tableValue(bvhNodes,node*2+1);
        if(!boxHit(origin,direction,lo.xyz,hi.xyz,limit))continue;
        if(hi.w>=0.0){
            stack[count++]=int(lo.w);stack[count++]=int(hi.w);
        }else{
            int start=int(lo.w);int end=start-int(hi.w);
            for(int triangle=start;triangle<end;triangle++){
                vec4 first=tableValue(triangleWorld,triangle*3);
                if(first.w==support.x || first.w==support.y)continue;
                vec3 a=first.xyz;
                vec3 e1=tableValue(triangleWorld,triangle*3+1).xyz-a;
                vec3 e2=tableValue(triangleWorld,triangle*3+2).xyz-a;
                vec3 p=cross(direction,e2);float det=dot(e1,p);
                if(abs(det)<1e-20)continue;
                vec3 offset=origin-a;float u=dot(offset,p)/det;
                if(u<0.0 || u>1.0)continue;
                vec3 q=cross(offset,e1);float v=dot(direction,q)/det;
                if(v<0.0 || u+v>1.0)continue;
                float t=dot(e2,q)/det;
                if(t>=0.0 && t<limit){
                    vec4 policy=alphaPolicy<0.0?vec4(0.0):tableValue(lineFunctionPolicies,int(alphaPolicy)+int(first.w));
                    if(policy.y>0.5)return 2;
                    if(policy.x<0.5)blocked=true;
                }
            }
        }
    }
    return blocked?0:1;
}
'''
