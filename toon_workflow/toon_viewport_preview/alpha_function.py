# SPDX-License-Identifier: GPL-3.0-or-later
"""Source Alpha = perspective attribute times the original sampled curve.

The function is independent of geometry/width tessellation. Curve samples are
uniform in chain phase; the attribute is linear in the source's world parameter.
"""
import numpy as np


def evaluate(function, q):
    q=np.clip(np.asarray(q,np.float64),0,1)
    a,b=function['map'];w0,w1=function['weights']
    numerator=a*w1*(1-q)+b*w0*q
    denominator=w1*(1-q)+w0*q
    attribute=np.divide(numerator,denominator,out=np.zeros_like(q),where=denominator>0)
    attribute=np.where(q<=0,a,np.where(q>=1,b,attribute))
    curve=function['curve'];c0,c1=function['phase']
    value=1. if curve is None else np.interp(c0+q*(c1-c0),np.linspace(0,1,len(curve)),curve)
    return np.clip(attribute*value*function['scale'],0,1)


def maximum(function):
    """Exact candidates of each rational-times-affine piece, in float64.

    Local interval coordinates avoid large curve intercepts on short pieces.
    With N(r)=n0+n1*r+n2*r*r and D(r)=c+d*r, the derivative
    numerator is (n1*c-n0*d)+2*n2*c*r+n2*d*r*r.
    """
    c0,c1=function['phase'];curve=function['curve'];cuts=np.array([0.,1.])
    if curve is not None and len(curve)>1 and c0!=c1:
        grid=(np.linspace(0,1,len(curve))-c0)/(c1-c0)
        cuts=np.union1d(cuts,grid[(grid>0)&(grid<1)])
    values=evaluate(function,cuts);best=float(values.max())
    if curve is None or len(curve)==1 or c0==c1:return best
    lo,hi=cuts[:-1],cuts[1:]
    a,b=function['map'];w0,w1=function['weights']
    dl=w1*(1-lo)+w0*lo;dr=w1*(1-hi)+w0*hi
    ml=(a*w1*(1-lo)+b*w0*lo)/dl
    mr=(a*w1*(1-hi)+b*w0*hi)/dr
    cl=np.interp(c0+lo*(c1-c0),np.linspace(0,1,len(curve)),curve)
    cr=np.interp(c0+hi*(c1-c0),np.linspace(0,1,len(curve)),curve)
    normal=np.maximum(dl,dr);dl=dl/normal;dr=dr/normal
    n0=ml*dl*cl
    n1=(mr*dr-ml*dl)*cl+ml*dl*(cr-cl)
    n2=(mr*dr-ml*dl)*(cr-cl);d=dr-dl
    p0=n1*dl-n0*d;p1=2*n2*dl;p2=n2*d
    candidates=[]
    linear=(p2==0)&(p1!=0)
    root=np.divide(-p0,p1,out=np.zeros_like(p0),where=linear)
    valid=linear&(root>0)&(root<1)
    candidates.extend((lo[valid]+root[valid]*(hi-lo)[valid]).tolist())
    discriminant=p1*p1-4*p2*p0
    quadratic=(p2!=0)&(discriminant>=0)
    stable=-.5*(p1+np.copysign(np.sqrt(np.maximum(discriminant,0)),p1))
    for root in (np.divide(stable,p2,out=np.zeros_like(p2),where=quadratic),
                 np.divide(p0,stable,out=np.zeros_like(p0),where=quadratic&(stable!=0))):
        valid=quadratic&(root>0)&(root<1)
        candidates.extend((lo[valid]+root[valid]*(hi-lo)[valid]).tolist())
    if candidates:best=max(best,float(evaluate(function,np.asarray(candidates)).max()))
    return best


def checked(function,curve_cache=None):
    if not isinstance(function,dict) or set(function)!={'map','weights','phase','curve','scale'}:
        raise ValueError('Source Alpha function fields are incomplete')
    result={}
    for key in ('map','weights','phase'):
        value=np.asarray(function[key])
        if value.shape!=(2,) or value.dtype.kind not in 'iuf' or not np.isfinite(value).all():
            raise ValueError('Finite source Alpha '+key+' pair required')
        result[key]=value.astype(np.float64,copy=False)
    if np.any(result['map']<0) or np.any(result['map']>1):
        raise ValueError('Source Alpha map must be in [0,1]')
    if np.any(result['weights']<=0) or np.max(result['weights'])!=1:
        raise ValueError('Positive normalized source Alpha perspective weights required')
    scale=np.asarray(function['scale'])
    if scale.shape!=() or scale.dtype.kind not in 'iuf' or not np.isfinite(scale) or not 0<=scale<=1:
        raise ValueError('Source Alpha scale must be in [0,1]')
    result['scale']=float(scale)
    curve=function['curve']
    if curve is not None and (curve_cache is None or id(curve) not in curve_cache):
        values=np.asarray(curve)
        if values.ndim!=1 or not 1<=len(values)<=1024 or values.dtype.kind not in 'iuf' or not np.isfinite(values).all() or np.any(values<0) or np.any(values>1):
            raise ValueError('Source Alpha curve requires 1..1024 finite samples in [0,1]')
        if curve_cache is not None:curve_cache[id(curve)]=values
    result['curve']=curve
    return result
