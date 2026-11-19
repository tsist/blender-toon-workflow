# SPDX-License-Identifier: GPL-3.0-or-later
"""Conditional region colour accumulation at each correlated sample."""
import json,math
import numpy as np
from . import manga_look

SCHEMA='toon-shot-look.regions.v2'

def normalize(look):
    if type(look) is not dict or set(look)!={'foreground','background','background_roles'}:raise ValueError('Closed regional look document required')
    fg=dict(look['foreground']);strength=fg.pop('line_ink_strength',None)
    if type(strength) not in (int,float) or not math.isfinite(strength) or not 0<=strength<=1:raise ValueError('Invalid regional ink strength')
    fg=manga_look.normalize_settings(fg)
    if fg['mode']!='MANGA':raise ValueError('Regional foreground requires MANGA')
    fg['line_ink_strength']=float(strength)
    bg=manga_look.normalize_settings(look['background']);roles=look['background_roles']
    if type(roles) is not list or len(set(roles))!=len(roles) or any(type(r) is not str or not r for r in roles):raise ValueError('Explicit unique background roles required')
    return json.loads(json.dumps(dict(foreground=fg,background=bg,background_roles=roles)))

class Accumulator:
    def __init__(self,shape):
        self.foreground=np.zeros(shape,np.float64);self.background=np.zeros(shape,np.float64);self.count=0;self.captures=[]
    def add(self,beauty,line,mask,capture):
        if mask.shape!=beauty.shape[:2] or not np.isfinite(mask).all() or not np.all((mask==0)|(mask==1)):
            raise ValueError('Regional Look requires binary same-position opaque coverage; mixed native coverage needs conditional passes')
        combined=line.astype(np.float64)+beauty.astype(np.float64)*(1-line[:,:,3:4].astype(np.float64))
        self.background+=combined*mask[:,:,None];self.foreground+=combined*(1-mask[:,:,None])
        self.count+=1;self.captures.append(capture)
    def resolve(self,count,combined):
        if self.count!=count or count<=0:raise ValueError('Incomplete regional sample set')
        fg=(self.foreground/count).astype(np.float32);bg=(self.background/count).astype(np.float32)
        error=float(abs(fg.astype(np.float64)+bg-combined).max())
        if error>8*np.finfo(np.float32).eps*max(1,float(abs(combined).max())):raise ValueError('Region contributions do not reconstruct Combined')
        return dict(Foreground=fg,Background=bg),dict(profile=SCHEMA,sample_count=count,captures=self.captures,reconstruction_max_error=error,
            semantics='conditional premultiplied Combined per region before resolve; no resolved-mask colour multiplication')

def apply(regions,settings,combined,line,*,offset=(0,0)):
    from .shot_look_output import _ink
    look=normalize(settings);fg=dict(look['foreground']);strength=fg.pop('line_ink_strength');bg=dict(look['background'])
    for item in (fg,bg):item['offset_px']=[item['offset_px'][0]+offset[0],item['offset_px'][1]+offset[1]]
    a=manga_look.apply(regions['Foreground'],fg);b=manga_look.apply(regions['Background'],bg)
    value=a+b;value[:,:,3]=combined[:,:,3]
    value=_ink(value,line,strength,fg['ink_rgb']);value[:,:,3]=combined[:,:,3]
    return value
