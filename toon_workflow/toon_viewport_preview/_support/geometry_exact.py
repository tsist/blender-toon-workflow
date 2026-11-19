# SPDX-License-Identifier: GPL-3.0-or-later
"""Original exact, closed-set triangle/triangle intersection research core.

Input: exactly 3 x 3 list/tuple or NumPy ndarray. Coordinates accept Python
int (unbounded), fractions.Fraction, finite Python float; NumPy signed/unsigned
integer scalars up to64 bits and float32/float64. Arrays accept integer <=64-bit
or float32/64 dtype only. bool, complex, Decimal, strings, object arrays,
float16 and longdouble are explicitly rejected. No input is mutated/coerced
through a lower-precision float. Fractions preserve exact input IEEE values.

Result keys: kind, dimension (-1/0/1/2), vertices (tuple of Fraction triples),
vertices_exact (JSON list of triples of [numerator,denominator] pairs). No float
display copy is supplied. Geometry is canonical under swapping/permutations;
source identities belong to the caller's separate pair record, never this key.

No candidate enumeration, visibility, rendering, commercial modules, network,
epsilon, snapping or length filtering. Dependencies: stdlib; optional existing
NumPy for explicit ndarray/scalar input support. Not a Blender integration.
"""
from fractions import Fraction
import math
try:
    import numpy as np
except ImportError:
    np=None

__all__=['triangle_intersection_exact']

def _coordinate(value):
    t=type(value)
    if t is Fraction:return value
    if t is int:return Fraction(value)
    if t is float:
        if not math.isfinite(value):raise ValueError('Coordinates must be finite')
        return Fraction.from_float(value)
    if np is not None:
        if isinstance(value,np.integer) and value.dtype.kind in 'iu' and value.dtype.itemsize<=8:
            return Fraction(int(value))
        if t is np.float32 or t is np.float64:
            v=float(value)  # Exact widening of binary32, identity for binary64.
            if not math.isfinite(v):raise ValueError('Coordinates must be finite')
            return Fraction.from_float(v)
    raise TypeError('Coordinate type must be int, Fraction, float or explicit NumPy integer/float32/float64; bool is excluded')

def _triangle(value):
    if np is not None and isinstance(value,np.ndarray):
        if value.shape!=(3,3):raise ValueError('Triangle ndarray shape must be (3,3)')
        dt=value.dtype
        if not ((dt.kind in 'iu' and dt.itemsize<=8) or (dt.kind=='f' and dt.itemsize in (4,8))):
            raise TypeError('Triangle ndarray dtype must be <=64-bit integer or float32/float64')
    elif type(value) in (list,tuple):
        if len(value)!=3:raise ValueError('Triangle must have three rows')
    else:raise TypeError('Triangle must be a list, tuple or NumPy ndarray')
    out=[]
    for row in value:
        if type(row) not in (list,tuple) and not (np is not None and isinstance(row,np.ndarray)):
            raise TypeError('Triangle rows must be list, tuple or ndarray')
        if len(row)!=3:raise ValueError('Triangle rows must have three coordinates')
        out.append(tuple(_coordinate(v) for v in row))
    t=tuple(out);normal=_cross(_sub(t[1],t[0]),_sub(t[2],t[0]))
    if not any(normal):raise ValueError('Degenerate zero-area triangle')
    return t,normal

def _sub(a,b):return tuple(x-y for x,y in zip(a,b))
def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _lerp(a,b,t):return tuple(x+t*(y-x) for x,y in zip(a,b))

def _plane_slice(triangle,normal,origin):
    values=[_dot(normal,_sub(p,origin)) for p in triangle]
    points={p for p,d in zip(triangle,values) if d==0}
    for i in range(3):
        j=(i+1)%3;di,dj=values[i],values[j]
        if di*dj<0:points.add(_lerp(triangle[i],triangle[j],di/(di-dj)))
    return tuple(sorted(points))

def _orient2(a,b,c,axes):
    i,j=axes
    return (b[i]-a[i])*(c[j]-a[j])-(b[j]-a[j])*(c[i]-a[i])

def _unique_in_order(points):
    # Complete exact duplicate removal, never a distance/rounding test.
    result=[];seen=set()
    for p in points:
        if p not in seen:seen.add(p);result.append(p)
    return result

def _coplanar_clip(A,B,normal):
    drop=next(i for i,v in enumerate(normal) if v);axes=tuple(i for i in range(3) if i!=drop)
    orientation=_orient2(*B,axes);sign=1 if orientation>0 else -1
    polygon=list(A)
    for i in range(3):
        if not polygon:break
        start,end=B[i],B[(i+1)%3];clipped=[]
        previous=polygon[-1];dp=sign*_orient2(start,end,previous,axes)
        for current in polygon:
            dc=sign*_orient2(start,end,current,axes)
            if dc>=0:
                if dp<0:clipped.append(_lerp(previous,current,dp/(dp-dc)))
                clipped.append(current)
            elif dp>=0:clipped.append(_lerp(previous,current,dp/(dp-dc)))
            previous,dp=current,dc
        polygon=_unique_in_order(clipped)
    return polygon

def _canonical_geometry(points):
    points=_unique_in_order(points)
    if not points:return 'empty',()
    if len(points)==1:return 'point',(points[0],)
    first=points[0];edge=_sub(points[1],first)
    if all(not any(_cross(edge,_sub(p,first))) for p in points[2:]):
        a,b=min(points),max(points)
        return ('point',(a,)) if a==b else ('segment',(a,b))
    # For a nondegenerate convex boundary remove only a middle point of an
    # exactly straight edge; positive short edges and nonzero corners survive.
    changed=True
    while changed:
        changed=False
        for i,p in enumerate(points):
            previous,following=points[i-1],points[(i+1)%len(points)]
            if not any(_cross(_sub(p,previous),_sub(following,p))) and _dot(_sub(p,previous),_sub(p,following))<=0:
                del points[i];changed=True;break
    ring=tuple(points);reverse=tuple(reversed(ring))
    canonical=min(seq[i:]+seq[:i] for seq in (ring,reverse) for i in range(len(ring)))
    return 'coplanar_area',canonical

def _membership_gram(point,triangle,normal):
    # Independent construction guard: Gram-system barycentrics, not slicing or
    # clipping predicates. Exact plane membership and closed triangle weights.
    e,f=_sub(triangle[1],triangle[0]),_sub(triangle[2],triangle[0]);r=_sub(point,triangle[0])
    if _dot(normal,r)!=0:return False
    ee,ef,ff,er,fr=_dot(e,e),_dot(e,f),_dot(f,f),_dot(e,r),_dot(f,r)
    determinant=ee*ff-ef*ef
    if determinant<=0:raise ArithmeticError('Internal invalid nondegenerate triangle')
    u=(ff*er-ef*fr)/determinant;v=(ee*fr-ef*er)/determinant
    return u>=0 and v>=0 and u+v<=1

def triangle_intersection_exact(triangle_a,triangle_b):
    """Return exact canonical intersection, or reject illegal/degenerate inputs.

    TypeError means an unsupported container/scalar/dtype; ValueError means
    invalid shape, nonfinite coordinate or zero-area triangle. ArithmeticError
    is reserved for an internal construction/membership inconsistency.
    """
    A,nA=_triangle(triangle_a);B,nB=_triangle(triangle_b)
    direction=_cross(nA,nB)
    if not any(direction):
        points=[] if _dot(nA,_sub(B[0],A[0]))!=0 else _coplanar_clip(A,B,nA)
    else:
        sa=_plane_slice(A,nB,B[0]);sb=_plane_slice(B,nA,A[0]);points=[]
        if sa and sb:
            axis=next(i for i,x in enumerate(direction) if x)
            alo,ahi=min(p[axis] for p in sa),max(p[axis] for p in sa)
            blo,bhi=min(p[axis] for p in sb),max(p[axis] for p in sb)
            left,right=max(alo,blo),min(ahi,bhi)
            if left<=right:
                if alo==ahi:
                    points=[sa[0]]
                else:
                    p=min(sa,key=lambda p:p[axis]);q=max(sa,key=lambda p:p[axis])
                    points=[_lerp(p,q,(value-p[axis])/(q[axis]-p[axis])) for value in (left,right)]
    kind,vertices=_canonical_geometry(points)
    for p in vertices:
        if not (_membership_gram(p,A,nA) and _membership_gram(p,B,nB)):
            raise ArithmeticError('Constructed intersection vertex fails independent exact membership')
    return dict(kind=kind,dimension={'empty':-1,'point':0,'segment':1,'coplanar_area':2}[kind],vertices=vertices,vertices_exact=[[[x.numerator,x.denominator] for x in p] for p in vertices])
