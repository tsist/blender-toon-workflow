# SPDX-License-Identifier: GPL-3.0-or-later
"""Nonlinear look sidecar; never changes the four correlated source layers."""
import json,zipfile,math
from pathlib import Path
import numpy as np
from . import manga_look

SCHEMA='toon-shot-look.v1'

def _ink(pixels,line,strength,color):
    if type(strength) not in (int,float) or not math.isfinite(strength) or not 0<=strength<=1:raise ValueError('Invalid original-line ink strength')
    if line.shape!=pixels.shape:raise ValueError('Line and Look extent differ')
    result=pixels.copy()
    weight=np.divide(line[:,:,3:4],pixels[:,:,3:4],out=np.zeros_like(pixels[:,:,3:4]),where=pixels[:,:,3:4]>0)
    weight=np.clip(weight,0,1)*strength
    result[:,:,:3]=pixels[:,:,:3]*(1-weight)+np.asarray(color,dtype=pixels.dtype)*pixels[:,:,3:4]*weight
    return result

def build(full_combined,frame,settings,*,line):
    from .backend import pencil
    settings=dict(settings);strength=settings.pop('line_ink_strength')
    normalized=manga_look.normalize_settings(settings)
    if normalized['mode']!='MANGA':raise ValueError('Only explicit MANGA creates a Look sidecar')
    camera=pencil('camera')
    pixels=camera.extract_pixels(frame,_ink(manga_look.apply(full_combined,normalized),line,strength,normalized['ink_rgb']))
    replay=dict(normalized)
    info=frame['metadata']
    crop=info['use_border'] and info['use_crop_to_border']
    x,y=info['border_rect_px'][:2] if crop else (0,0)
    replay['offset_px']=[normalized['offset_px'][0]+x,normalized['offset_px'][1]+y]
    receipt=dict(schema=SCHEMA,settings=json.loads(json.dumps(normalized)),replay_settings=json.loads(json.dumps(replay)),line_ink_strength=strength,
        source='resolved-full-frame-Combined-before-border-crop',alpha_exact=True,
        semantics='nonlinear-sidecar; raw Beauty/Line/Interaction/Combined remain correlated originals',
        limitations=['luminance does not distinguish albedo from shadow','screen patterns do not follow surface curvature'])
    return pixels,receipt

def validate(report,layers,width,height):
    has='shot_look' in report
    keys={'Look','look_preview','look_arrays'}
    actual=keys & set(report['artifacts'])
    if has!=bool(actual) or has and actual!=keys:raise ValueError('Look metadata/artifact coverage differs')
    if 'look_region_arrays' in report['artifacts'] and (not has or report['shot_look'].get('schema')!='toon-shot-look.regions.v2'):
        raise ValueError('Regional arrays require their explicit v2 look receipt')
    if not has:return None
    receipt=report['shot_look']
    if receipt.get('schema')=='toon-shot-look.regions.v2':return validate_regions(report,layers,width,height)
    required={'schema','settings','replay_settings','line_ink_strength','source','alpha_exact','semantics','limitations'}
    if type(receipt) is not dict or set(receipt)!=required or receipt['schema']!=SCHEMA or receipt['alpha_exact'] is not True:raise ValueError('Invalid Look sidecar schema')
    normalized=manga_look.normalize_settings(receipt['settings']);replay=manga_look.normalize_settings(receipt['replay_settings'])
    if normalized['mode']!='MANGA':raise ValueError('Look sidecar requires MANGA')
    expected=dict(normalized);info=report['frame']
    crop=info['use_border'] and info['use_crop_to_border']
    x,y=info['border_rect_px'][:2] if crop else (0,0)
    expected['offset_px']=(normalized['offset_px'][0]+x,normalized['offset_px'][1]+y)
    if replay!=expected or receipt['source']!='resolved-full-frame-Combined-before-border-crop':raise ValueError('Look crop/pixel-space replay differs')
    path=Path(report['artifacts']['look_arrays']['path'])
    with zipfile.ZipFile(path) as archive:
        if archive.namelist()!=['Look.npy'] or archive.infolist()[0].file_size>width*height*4*4+1024:raise ValueError('Look array exceeds declared extent')
    with np.load(path,allow_pickle=False) as archive:pixels=archive['Look']
    if pixels.shape!=(height,width,4) or pixels.dtype!=np.float32 or not np.isfinite(pixels).all():raise ValueError('Invalid Look array')
    if not np.array_equal(pixels[:,:,3],layers['Combined'][:,:,3]):raise ValueError('Look Alpha changed')
    expected_pixels=_ink(manga_look.apply(layers['Combined'],replay),layers['Line'],receipt['line_ink_strength'],normalized['ink_rgb'])
    if not np.array_equal(pixels,expected_pixels):raise ValueError('Look nonlinear replay differs')
    return dict(schema=SCHEMA,mode='MANGA',alpha_exact=True,nonlinear_replay_exact=True)


def build_regions(full_combined,frame,settings,*,line,regions,capture):
    from . import shot_regions
    from .backend import pencil
    document=dict(foreground=settings['foreground'],background=settings['background'],background_roles=[])
    document=shot_regions.normalize(document)
    pixels=shot_regions.apply(regions,document,full_combined,line)
    extract=pencil('camera').extract_pixels
    info=frame['metadata'];crop=info['use_border'] and info['use_crop_to_border']
    offset=info['border_rect_px'][:2] if crop else [0,0]
    receipt=dict(schema=shot_regions.SCHEMA,settings=document,background_objects=settings['background_objects'],
        replay_offset=list(offset),source='same-render-binary-coverage-conditional-Combined',alpha_exact=True,
        semantics='conditional-premultiplied-regions-resolved-before-nonlinear-look',capture=capture,
        limitations=['luminance does not distinguish albedo from shadow','screen patterns do not follow surface curvature',
                     'binary opaque coverage only; native mixed coverage requires conditional colour passes'])
    return extract(frame,pixels),receipt,{k:extract(frame,v) for k,v in regions.items()}


def validate_regions(report,layers,width,height):
    from . import shot_regions
    receipt=report['shot_look']
    fields={'schema','settings','background_objects','replay_offset','source','alpha_exact','semantics','capture','limitations'}
    if type(receipt) is not dict or set(receipt)!=fields or receipt['schema']!=shot_regions.SCHEMA or receipt['alpha_exact'] is not True:
        raise ValueError('Invalid regional look receipt')
    settings=shot_regions.normalize(receipt['settings'])
    if receipt['source']!='same-render-binary-coverage-conditional-Combined':raise ValueError('Invalid region source semantics')
    info=report['frame'];crop=info['use_border'] and info['use_crop_to_border']
    offset=info['border_rect_px'][:2] if crop else [0,0]
    if receipt['replay_offset']!=list(offset):raise ValueError('Regional crop origin differs')
    capture=receipt['capture'];count=len(report['samples'])
    if capture.get('profile')!=shot_regions.SCHEMA or capture.get('sample_count')!=count or len(capture.get('captures',[]))!=count:
        raise ValueError('Regional sampling coverage is incomplete')
    objects=receipt['background_objects']
    if type(objects) is not list or objects!=sorted(set(objects)):raise ValueError('Region Object identities differ')
    for index,row in enumerate(capture['captures']):
        if (row.get('same_render') is not True or row.get('background_objects')!=objects
                or row.get('filter_size')!=0. or row.get('extent')!=info['full_extent']
                or row.get('samples')!=report['lighting']['samples_per_position']):
            raise ValueError('Regional capture is not aligned to the declared fixed-position render')
        if row.get('frame')!=info['frame'] or row.get('subframe')!=info['frame_subframe']:
            raise ValueError('Regional capture time differs')
        # Each capture file is an independent immutable native pass artifact.
        for file in row.get('files',[]):
            p=Path(file['file'])
            if not p.resolve(strict=True).is_relative_to(Path(report['author_checkpoint']).parent.resolve()):
                raise ValueError('Regional native output left its bundle')
            import hashlib
            if hashlib.sha256(p.read_bytes()).hexdigest()!=file['sha256']:raise ValueError('Regional native output changed')
    expected={'Foreground','Background'}
    path=Path(report['artifacts']['look_region_arrays']['path'])
    with zipfile.ZipFile(path) as z:
        if set(z.namelist())!={k+'.npy' for k in expected} or any(f.file_size>width*height*4*4+1024 for f in z.infolist()):
            raise ValueError('Regional arrays exceed the declared extent')
    with np.load(path,allow_pickle=False) as z:regions={k:z[k] for k in expected}
    for a in regions.values():
        if a.dtype!=np.float32 or a.shape!=(height,width,4) or not np.isfinite(a).all():raise ValueError('Invalid conditional region pixels')
    source=regions['Foreground'].astype(np.float64)+regions['Background']
    if np.max(abs(source-layers['Combined']))>8*np.finfo(np.float32).eps*max(1,float(abs(layers['Combined']).max())):
        raise ValueError('Conditional regions no longer reconstruct Combined')
    expected_pixels=shot_regions.apply(regions,settings,layers['Combined'],layers['Line'],offset=offset)
    with np.load(report['artifacts']['look_arrays']['path'],allow_pickle=False) as z:
        if set(z.files)!={'Look'}:raise ValueError('Invalid Look archive')
        actual=z['Look']
    if not np.array_equal(actual,expected_pixels) or not np.array_equal(actual[:,:,3],layers['Combined'][:,:,3]):
        raise ValueError('Regional nonlinear replay/Alpha differs')
    return dict(schema=shot_regions.SCHEMA,mode='MANGA',alpha_exact=True,conditional_replay_exact=True,
                background_objects=objects,sample_count=count)
