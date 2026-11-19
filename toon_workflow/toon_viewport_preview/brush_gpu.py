# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU shader helpers for the same portable authored brush records."""
COMMON='''
vec4 brushRecord(int index){
    int width=textureSize(brushEffectRecords,0).x;
    return texelFetch(brushEffectRecords,ivec2(index%width,index/width),0);
}
vec4 brushPrimitive(int row){return brushRecord(32+int(brushEffectId)*21+row);}
'''
FRAGMENT=COMMON+'''
int brushWrapped(int p,int extentSize,int mode){
    if(mode==0)return ((p%extentSize)+extentSize)%extentSize;
    if(mode==1){int q=((p%(2*extentSize))+2*extentSize)%(2*extentSize);return q<extentSize?q:2*extentSize-q-1;}
    if(mode==4)return clamp(p<0?-p-1:p,0,extentSize-1);
    return clamp(p,0,extentSize-1);
}
vec4 brushTexel(ivec2 p,vec4 image,ivec2 wrapping){
    ivec2 extent=ivec2(image.yz);
    if((wrapping.x==3 && (p.x<0 || p.x>=extent.x)) || (wrapping.y==3 && (p.y<0 || p.y>=extent.y)))return vec4(0.0);
    p=ivec2(brushWrapped(p.x,extent.x,wrapping.x),brushWrapped(p.y,extent.y,wrapping.y));
    int index=int(image.x)+p.y*extent.x+p.x,width=textureSize(brushEffectPixels,0).x;
    return texelFetch(brushEffectPixels,ivec2(index%width,index/width),0);
}
vec4 brushMap(int mapId,float t,vec2 samplePosition){
    int base=mapId*6;vec4 meta=brushRecord(base);
    if(meta.x<0.5)return vec4(1.0);
    float surfaceT=(t/wb.w)/((1.0-t)/wa.w+t/wb.w);
    if(meta.z>0.5)return mix(brushPrimitive(8+mapId*2),brushPrimitive(9+mapId*2),surfaceT);
    vec4 endpoints=brushPrimitive(3+mapId);vec2 uv=mix(endpoints.xy,endpoints.zw,surfaceT);
    if(meta.w<0.5){
        vec2 d=b.xy-a.xy;float len=length(d);vec2 normal=len>0.0?vec2(-d.y,d.x)/len:vec2(0,1);
        vec2 phase=brushPrimitive(2).xy;
        uv=vec2(mix(phase.x,phase.y,t),dot(samplePosition-mix(a.xy,b.xy,t),normal)/max(mix(widths.x,widths.y,t),1e-8)+0.5);
    }else if(meta.w>1.5 && meta.w<2.5){uv=samplePosition/viewport.xy;}
    vec4 transform=brushRecord(base+2),options=brushRecord(base+3),image=brushRecord(base+1);
    uv=uv*transform.xy;float c=cos(options.x),s=sin(options.x);
    uv=mat2(c,s,-s,c)*(uv-0.5)+0.5+transform.zw;
    vec2 p=uv*image.yz-0.5;ivec2 lo=ivec2(floor(p));ivec2 wrapping=ivec2(options.yz);
    if(image.w<0.5)return brushTexel(ivec2(floor(p+0.5)),image,wrapping);
    vec2 fraction=fract(p);
    return mix(mix(brushTexel(lo,image,wrapping),brushTexel(lo+ivec2(1,0),image,wrapping),fraction.x),
        mix(brushTexel(lo+ivec2(0,1),image,wrapping),brushTexel(lo+ivec2(1,1),image,wrapping),fraction.x),fraction.y);
}
vec2 brushLocal(vec2 p){
    vec4 shape=brushPrimitive(0),extent=brushPrimitive(1);
    vec2 d=b.xy-a.xy;float angle=atan(d.y,d.x)+shape.y,c=cos(angle),s=sin(angle);
    return mat2(c,-s,s,c)*p/vec2(shape.x*extent.x,extent.y);
}
float authoredBrushDistance(vec2 samplePosition,float t,out float radius,out float attenuation){
    vec2 delta=brushLocal(b.xy-a.xy),relative=brushLocal(samplePosition-a.xy);
    vec4 distortion=brushRecord(24);
    if(distortion.x>0.5){
        vec2 normal=length(delta)>0.0?vec2(-delta.y,delta.x)/length(delta):vec2(0,1);
        relative-=normal*(brushMap(4,t,samplePosition).r-0.5)*distortion.y;
    }
    float l2=dot(delta,delta),u=l2>0.0?clamp(dot(relative,delta)/l2,0.0,1.0):0.0;
    float sizeAmount=brushRecord(6).y;
    float sizeFactor=brushRecord(6).x>0.5?max(0.0,mix(1.0,brushMap(1,t,samplePosition).r,sizeAmount)):1.0;
    float r0=widths.x*0.5*sizeFactor,r1=widths.y*0.5*sizeFactor,dr=r1-r0;
    if(l2==0.0)u=dr>0.0?1.0:0.0;
    else if(dr!=0.0){
        float len=sqrt(l2),slope=abs(dr);
        if(slope>=len)u=dr>0.0?1.0:0.0;
        else{
            float perpendicular=abs(relative.x*delta.y-relative.y*delta.x)/len;
            u=clamp((dot(relative,delta)+dr*perpendicular*len/sqrt((len-slope)*(len+slope)))/l2,0.0,1.0);
        }
    }
    radius=mix(r0,r1,u);vec2 displacement=relative-delta*u;
    vec4 shape=brushPrimitive(0),extent=brushPrimitive(1);
    float normalized=radius>0.0?length(displacement)/radius:1e20;
    attenuation=1.0;
    if(extent.w>0.5 && extent.w<1.5)attenuation=exp(-2.0*normalized*normalized);
    if(extent.w>1.5 || shape.z>0.0){
        float across=radius>0.0?displacement.y/radius:0.0;
        float amount=extent.w>1.5?max(shape.z,0.65):shape.z;
        attenuation*=mix(1.0,0.5+0.5*cos((across+1.0)*3.14159265359*shape.w),amount);
    }
    return length(displacement);
}
vec3 brushRgbToHsv(vec3 c){
    vec4 K=vec4(0.0,-1.0/3.0,2.0/3.0,-1.0);
    vec4 p=mix(vec4(c.bg,K.wz),vec4(c.gb,K.xy),step(c.b,c.g));
    vec4 q=mix(vec4(p.xyw,c.r),vec4(c.r,p.yzx),step(p.x,c.r));
    float d=q.x-min(q.w,q.y),e=1e-10;
    return vec3(abs(q.z+(q.w-q.y)/(6.0*d+e)),d/(q.x+e),q.x);
}
vec3 brushHsvToRgb(vec3 c){
    vec3 p=abs(fract(c.xxx+vec3(0,2.0/3.0,1.0/3.0))*6.0-3.0);
    return c.z*mix(vec3(1),clamp(p-1.0,0.0,1.0),c.y);
}
vec3 authoredBrushColor(vec3 rgb,float t,vec2 p){
    vec4 mapMeta=brushRecord(18);
    if(mapMeta.x>0.5)rgb=mix(rgb,brushMap(3,t,p).rgb,clamp(mapMeta.y,0.0,1.0));
    vec4 mode=brushRecord(30),adjust=brushRecord(31);
    if(mode.x<0.5)return clamp(rgb+mode.yzw,0.0,1.0);
    vec3 hsv=brushRgbToHsv(max(rgb,vec3(0)));
    hsv.x=fract(hsv.x+adjust.x);hsv.yz=clamp(hsv.yz+adjust.yz,0.0,1.0);
    return brushHsvToRgb(hsv);
}
'''
