# SPDX-License-Identifier: GPL-3.0-or-later
"""Nineteen original autonomous designs; no vendor preset code is imported."""
import json
import math
from pathlib import Path

BRUSHES=('Bubble','Circle','Flat','Flower','Rough','Soft')
STROKES=('Dashed_Line1','Dashed_Line2','Dashed_Line3','Fuzzy1','Fuzzy2','Fuzzy3','Fuzzy4',
         'Pen1','Pen2','Pen3','Spray','Wave_Large','Wave_Small')
NAMES=BRUSHES+STROKES


def _stamp(name):
    pixels=[];size=32
    for y in range(size):
        for x in range(size):
            u=(x+.5)/size*2-1;v=(y+.5)/size*2-1;r=math.hypot(u,v);theta=math.atan2(v,u)
            if name=='Bubble':alpha=max(0.,1-abs(r-.68)/.14)
            elif name=='Circle':alpha=max(0.,min(1.,(.83-r)*16))
            elif name=='Flat':alpha=max(0.,min(1.,(.85-abs(u))*16,(.2-abs(v))*16))
            elif name=='Flower':alpha=max(0.,min(1.,(.58+.23*math.cos(5*theta)-r)*16))
            elif name=='Rough':alpha=max(0.,1-r)*(.25+.75*((x*73+y*137+x*y*19)%101)/100)
            else:alpha=math.exp(-r*r*5)*max(0.,1-r)
            pixels.extend((1.,1.,1.,alpha))
    return dict(width=size,height=size,pixels=pixels)


def payload(name):
    from . import brush_effects as B
    if name not in NAMES:raise ValueError('Unknown autonomous preset: '+str(name))
    row=B.defaults()
    if name in BRUSHES:
        row['shape'].update(type='NORMAL',stretch=3. if name=='Flat' else 1.)
        row['stroke'].update(stroke_type='STAMP',length=1.4 if name=='Soft' else 5.,space=0.)
        row['maps']['brush'].update(enabled=True,image=_stamp(name),wrap_mode_u='CLAMP',wrap_mode_v='CLAMP')
    elif name.startswith('Dashed_Line'):
        index=int(name[-1]);row['stroke'].update(stroke_type='STAMP',length=[6.,12.,24.][index-1],space=[12.,18.,28.][index-1])
    elif name.startswith('Fuzzy'):
        index=int(name[-1]);row['shape'].update(type='MULTIPLE',stretch=1.,angle_random=15.)
        row['stroke'].update(copies=index+1,normal_offset=.45*index,normal_offset_random=.5,
            distortion_enabled=True,distortion_amount=.35*index,distortion_cycles=3.+index,seed=19+index)
    elif name.startswith('Pen'):
        index=int(name[-1]);row['shape'].update(type='NORMAL',stretch=1.+index*.6,angle=20.+index*10.)
        row['curves'].update(size_enabled=True,size=[[0.,.1],[.18,1.],[.72,.75],[1.,.08]],
            alpha_enabled=True,alpha=[[0.,.5],[.15,1.],[.85,1.],[1.,.5]])
        row['stroke']['size_random']=.04*index
    elif name=='Spray':
        row['shape'].update(type='MULTIPLE');row['stroke'].update(stroke_type='STAMP',length=.7,space=2.,
            copies=7,normal_offset_random=4.,x_offset_random=3.,y_offset_random=3.,size_random=.6,seed=731)
    else:
        row['stroke'].update(distortion_enabled=True,distortion_amount=4. if name=='Wave_Large' else 1.5,
            distortion_cycles=3. if name=='Wave_Large' else 9.)
    return B.checked(row)


def apply(scene,actual,name):
    from . import brush_ui
    return brush_ui.set_effects(scene,[dict(actual=actual,effects=payload(name))])


def reset(scene,actual,*,kind='ALL'):
    from . import brush_ui,brush_effects as B
    row=B.defaults()
    if kind=='BRUSH':row={'shape':row['shape'],'maps':{'brush':row['maps']['brush']}}
    elif kind=='STROKE':row={k:row[k] for k in ('stroke','curves')}
    elif kind!='ALL':raise ValueError('Unknown preset reset kind')
    return brush_ui.set_effects(scene,[dict(actual=actual,effects=row)])


def save(path,effects,*,label):
    from . import brush_effects as B
    value=dict(schema='toon.brush-preset.v1',label=label,effects=B.checked(effects))
    with Path(path).open('x',encoding='utf8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
    return value


def load(scene,actual,path):
    from . import brush_ui,brush_effects as B
    path=Path(path)
    if not path.is_file() or path.stat().st_size>16*1024*1024:raise ValueError('Invalid brush preset file')
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if type(value) is not dict or set(value)!={'schema','label','effects'} or value['schema']!='toon.brush-preset.v1':raise ValueError('Unsupported brush preset schema')
    return brush_ui.set_effects(scene,[dict(actual=actual,effects=B.checked(value['effects']))])
