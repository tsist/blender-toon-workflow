# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned, portable brush authors. No native source document is rewritten.

Angles are degrees, screen distances are output pixels, distance reductions
are scene world units. Raster images are immutable captured linear RGBA.
"""
import copy
import math

SCHEMA='toon.brush-effects.v1'
MAP_NAMES=('brush','size','alpha','color','distortion')

def texture_defaults():
    return dict(enabled=False,amount=1.,source_type='IMAGE',image=None,
        uv_source='STROKE',uv_selection_mode='INDEX',uv_name='',uv_index=0,
        object_color_selection_mode='INDEX',object_color_name='',object_color_index=0,
        offset=[0.,0.],tiling=[1.,1.],rotation=0.,wrap_mode_u='REPEAT',wrap_mode_v='REPEAT',filter_mode='LINEAR')

def reduction_defaults():
    return dict(enabled=False,refer_object_on=False,object_reference='',reduction_start=0.,
        reduction_end=10.,curve=[[0.,1.],[1.,0.]])

def defaults():
    return dict(shape=dict(type='SIMPLE',stretch=1.,stretch_random=0.,angle=0.,angle_random=0.,groove=0.,groove_number=1),
        stroke=dict(size=1.,size_random=0.,stroke_size_random=0.,extend=0.,extend_random=0.,copies=1,copy_random=0,
            normal_offset=0.,normal_offset_random=0.,x_offset=0.,x_offset_random=0.,y_offset=0.,y_offset_random=0.,
            horizontal_space=0.,horizontal_space_random=0.,vertical_space=0.,vertical_space_random=0.,
            distortion_enabled=False,distortion_amount=0.,distortion_random=0.,distortion_cycles=1.,
            distortion_cycles_random=0.,distortion_phase=0.,distortion_phase_random=0.,
            stroke_type='CONTINUOUS',line_type='FULL',length=8.,length_random=0.,space=4.,space_random=0.,seed=0),
        color=dict(mode='RGB',red=0.,green=0.,blue=0.,hue=0.,saturation=0.,value=0.),
        curves=dict(size_enabled=False,alpha_enabled=False,size=[[0.,1.],[1.,1.]],alpha=[[0.,1.],[1.,1.]],
            reduction_start=0.,reduction_end=1.),
        maps={key:texture_defaults() for key in MAP_NAMES},
        reduction={channel:{kind:reduction_defaults() for kind in ('size','alpha')} for channel in ('VISIBLE','HIDDEN')},
        raster=dict(pixel_aspect=[1.,1.],off_screen_distance=0.,antialiasing=None,samples_per_axis=None),
        chain=dict(weld_edges=None,closed_orientation=None,seam_tokens={},seam_anchor=None))

def merge(base,patch):
    if type(patch) is not dict:raise ValueError('Brush effect update must be an object')
    result=copy.deepcopy(base)
    for key,value in patch.items():
        if key not in result:raise ValueError('Unknown brush effect field: '+str(key))
        result[key]=merge(result[key],value) if type(result[key]) is dict and key not in ('seam_tokens','image') else copy.deepcopy(value)
    return result

def _number(v,label,lo=-65536.,hi=65536.):
    if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:
        raise ValueError(label+': bounded finite number required')
    return v

def curve(points):
    if type(points) is not list or not 2<=len(points)<=4096:raise ValueError('Curve requires 2..4096 control points')
    previous=-1.
    for pair in points:
        if type(pair) is not list or len(pair)!=2:raise ValueError('Curve points are [position,value]')
        _number(pair[0],'curve position',0,1);_number(pair[1],'curve value',0,64)
        if pair[0]<=previous:raise ValueError('Curve positions must strictly increase')
        previous=pair[0]
    if points[0][0]!=0 or points[-1][0]!=1:raise ValueError('Curve must span [0,1]')
    return copy.deepcopy(points)

def checked(row):
    # Merging against defaults also rejects unknown fields at every level.
    out=merge(defaults(),row)
    shape=out['shape'];stroke=out['stroke']
    enums=[(shape['type'],('SIMPLE','NORMAL','MULTIPLE')),
        (stroke['stroke_type'],('NORMAL','RAKE','RANDOM','CONTINUOUS','STAMP')),(stroke['line_type'],('FULL','DASHED')),(out['color']['mode'],('RGB','HSV'))]
    for value,allowed in enums:
        if value not in allowed:raise ValueError('Unknown brush enum: '+str(value))
    for group in (shape,stroke,out['color']):
        for key,value in group.items():
            if type(value) is bool:continue
            if type(value) is str:continue
            _number(value,key,0,2147483647) if key=='seed' else _number(value,key)
    for key in ('distortion_enabled',):
        if type(stroke[key]) is not bool:raise ValueError(key+': boolean required')
    for key,minimum,maximum in (('copies',1,64),('copy_random',0,63),('seed',0,2147483647)):
        if type(stroke[key]) is not int or not minimum<=stroke[key]<=maximum:raise ValueError(key+': integer outside limits')
    if not 0<shape['stretch']<=64 or not 0<=shape['groove']<=1:raise ValueError('Invalid brush shape extent')
    if type(shape['groove_number']) is not int or not 1<=shape['groove_number']<=64:raise ValueError('groove_number requires 1..64')
    if not 0<=stroke['size']<=64 or stroke['length']<=0 or stroke['space']<0:raise ValueError('Invalid stroke size/spacing')
    cv=out['curves']
    for key in ('size_enabled','alpha_enabled'):
        if type(cv[key]) is not bool:raise ValueError('Curve enable requires boolean')
    cv['size']=curve(cv['size']);cv['alpha']=curve(cv['alpha'])
    _number(cv['reduction_start'],'reduction_start',0,1);_number(cv['reduction_end'],'reduction_end',0,1)
    if cv['reduction_start']>=cv['reduction_end']:raise ValueError('Curve reduction interval must increase')
    for name,m in out['maps'].items():
        m['source_type']={'OBJECTCOLOR':'OBJECT_COLOR'}.get(m['source_type'],m['source_type'])
        m['uv_source']={'OBJECTUV':'UV'}.get(m['uv_source'],m['uv_source'])
        m['filter_mode']={'POINT':'NEAREST','BILINEAR':'LINEAR'}.get(m['filter_mode'],m['filter_mode'])
        if type(m['enabled']) is not bool:raise ValueError('Texture gate requires boolean')
        _number(m['amount'],'map amount',0,64)
        for key,allowed in [('source_type',('IMAGE','OBJECT_COLOR')),('uv_source',('STROKE','UV','SCREEN','WORLD')),
            ('uv_selection_mode',('INDEX','NAME')),('object_color_selection_mode',('INDEX','NAME')),
            ('wrap_mode_u',('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE')),('wrap_mode_v',('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE')),
            ('filter_mode',('NEAREST','LINEAR'))]:
            if m[key] not in allowed:raise ValueError('Invalid texture '+key)
        for key in ('offset','tiling'):
            if type(m[key]) is not list or len(m[key])!=2:raise ValueError('Texture '+key+' requires two coordinates')
            for v in m[key]:_number(v,key)
        _number(m['rotation'],'map rotation')
        for key in ('uv_index','object_color_index'):
            if type(m[key]) is not int or not 0<=m[key]<=255:raise ValueError('Map index requires 0..255')
        for key in ('uv_name','object_color_name'):
            if type(m[key]) is not str or len(m[key])>256:raise ValueError('Map name too long')
        image=m['image']
        if image is not None:
            if type(image) is not dict or set(image)!={'width','height','pixels'}:raise ValueError('Captured image fields differ')
            w,h=image['width'],image['height']
            if type(w) is not int or type(h) is not int or w<1 or h<1 or w*h>1048576:raise ValueError('Image capture limit is 1048576 pixels')
            if type(image['pixels']) is not list or len(image['pixels'])!=w*h*4:raise ValueError('Captured RGBA pixel count differs')
            for v in image['pixels']:_number(v,'captured pixel',-65536,65536)
        if m['enabled'] and m['source_type']=='IMAGE' and image is None:raise ValueError('Enabled image map requires captured RGBA')
    for channel in out['reduction'].values():
        for r in channel.values():
            if type(r['enabled']) is not bool or type(r['refer_object_on']) is not bool:raise ValueError('Reduction gate requires boolean')
            _number(r['reduction_start'],'distance start',0,1e12);_number(r['reduction_end'],'distance end',0,1e12)
            if r['reduction_end']<=r['reduction_start']:raise ValueError('Distance interval must increase')
            if type(r['object_reference']) is not str:raise ValueError('Reference object name required')
            if r['enabled'] and r['refer_object_on'] and not r['object_reference']:raise ValueError('Enabled reference reduction requires object')
            r['curve']=curve(r['curve'])
    r=out['raster'];_number(r['off_screen_distance'],'offscreen',0,65536)
    if type(r['pixel_aspect']) is not list or len(r['pixel_aspect'])!=2:raise ValueError('Pixel aspect requires two axes')
    for v in r['pixel_aspect']:_number(v,'pixel aspect',.01,100)
    if r['antialiasing'] is not None:_number(r['antialiasing'],'antialiasing',0,16)
    if r['samples_per_axis'] is not None and (type(r['samples_per_axis']) is not int or r['samples_per_axis'] not in (1,2,4)):raise ValueError('Sampling requires 1,2,4')
    ch=out['chain']
    if ch['weld_edges'] is not None and type(ch['weld_edges']) is not bool:raise ValueError('Owned weld override requires boolean')
    if ch['closed_orientation'] not in (None,'retained','clockwise','counterclockwise'):raise ValueError('Unknown loop direction')
    if type(ch['seam_tokens']) is not dict:raise ValueError('Seam tokens require an object')
    for token in ch['seam_tokens'].values():
        if type(token) is not dict or set(token)!={'source_id','endpoint'} or type(token['source_id']) is not str or token['endpoint'] not in (0,1):raise ValueError('Invalid seam token')
    anchor=ch['seam_anchor']
    if anchor is not None and (type(anchor) is not dict or set(anchor)!={'object','edge','endpoint'} or type(anchor['object']) is not str or type(anchor['edge']) is not int or anchor['edge']<0 or anchor['endpoint'] not in (0,1)):raise ValueError('Seam anchor requires object, edge and endpoint')
    return out

def records(document,actuals=None):
    payload=document.get('request',{}).get('brush_effects')
    if payload is None:return {}
    if type(payload) is not dict or set(payload)!={'schema','brushes'} or payload['schema']!=SCHEMA or type(payload['brushes']) is not dict or len(payload['brushes'])>512:raise ValueError('Invalid brush effects document')
    rows={}
    for actual,row in payload['brushes'].items():
        if type(actual) is not str or (actuals is not None and actual not in actuals):raise ValueError('Unknown brush effect actual identity')
        rows[actual]=checked(row)
    return rows

def snapshot_image(image):
    """Explicit capture from a user-owned loaded Blender Image, portable in JSON."""
    w,h=map(int,image.size)
    if w<1 or h<1 or w*h>1048576:raise ValueError('Image must have 1..1048576 loaded pixels')
    return dict(width=w,height=h,pixels=list(image.pixels[:]))

def effective_chains(document,bindings,controls):
    """Apply explicit owned policy after preserved native controls validation."""
    result=copy.deepcopy(controls)
    kinds={a:{r['kind'] for r in bindings['routes'] if r['actual_brush_id']==a} for a in bindings['actuals']}
    for actual,row in records(document,bindings['actuals']).items():
        ch=row['chain']
        if ch==defaults()['chain']:continue
        control=result['brushes'][actual]
        control['boundary_enabled']='boundary' in kinds[actual]
        control['intersection_enabled']='intersection' in kinds[actual]
        if ch['closed_orientation'] is not None:control['policy']['closed_orientation']=ch['closed_orientation']
        if ch['seam_tokens']:control['policy']['seam_tokens']=copy.deepcopy(ch['seam_tokens'])
    return result

def neutral(row):
    """Dormant authors and phase-only policies do not enter the raster branch."""
    r=copy.deepcopy(row);base=defaults()
    r['chain']=base['chain']
    r['raster']['off_screen_distance']=0.
    for key,m in r['maps'].items():
        if not m['enabled'] or m['amount']==0:r['maps'][key]=base['maps'][key]
    cv=r['curves']
    if not cv['size_enabled'] and not cv['alpha_enabled']:r['curves']=base['curves']
    for channel,values in r['reduction'].items():
        for kind,red in values.items():
            if not red['enabled']:r['reduction'][channel][kind]=base['reduction'][channel][kind]
    s=r['stroke']
    if s['stroke_type']=='NORMAL':s['stroke_type']='CONTINUOUS'
    if s['stroke_type']=='CONTINUOUS' and s['line_type']=='FULL':
        for key in ('length','length_random','space','space_random'):s[key]=base['stroke'][key]
    if not s['distortion_enabled']:
        for key in tuple(s):
            if key.startswith('distortion_'):s[key]=base['stroke'][key]
    if s['copies']==1 and s['copy_random']==0:
        for key in ('horizontal_space','horizontal_space_random','vertical_space','vertical_space_random'):s[key]=base['stroke'][key]
    s['seed']=0
    shape=r['shape']
    if shape['type']=='SIMPLE' and shape['groove']==0:shape['groove_number']=1
    if shape['stretch']==1 and shape['stretch_random']==0:shape['angle']=0.;shape['angle_random']=0.
    c=r['color']
    if c['mode']=='RGB':c['hue']=c['saturation']=c['value']=0.
    elif c['hue']%1==0 and c['saturation']==0 and c['value']==0:c.update(base['color'])
    return r==base
