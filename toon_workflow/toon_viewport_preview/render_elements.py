# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit autonomous source selection, retaining resolved brush composition.

These profiles describe this renderer's sources. They do not certify Pencil's
eight native category predicates or its unresolved RGBA depth semantics.
"""
import hashlib
import json
import math
import re

import numpy as np
from . import depth_output

SCHEMA = 'toon.render-elements-request.v1'
PROFILE = 'autonomous_resolved_source_kind.v1'
BASIC_PROFILE = 'autonomous_basic_eligible_sources.v1'
CATEGORIES = ('outline', 'object', 'intersection', 'smoothing', 'material_id',
              'selected_edges', 'normal_angle', 'wireframe')
BITS = {name: 1 << index for index, name in enumerate(CATEGORIES)}
SUPPORTED = {'PRESET': {'outline','object', 'intersection', 'smoothing', 'material_id', 'selected_edges', 'normal_angle', 'wireframe'},
             'BASIC': {'outline', 'material_id', 'selected_edges', 'normal_angle', 'wireframe'}}
MAX_ELEMENTS = 128  # Product admission bound, not a native format limit.
DEFAULT_BUDGET = 512 * 1024 * 1024
_ID = re.compile(r'[A-Za-z0-9_]{1,32}\Z')
PRIMITIVE_ARRAYS = ('projected', 'world', 'supports', 'widths', 'alphas',
                    'alpha_source_ids', 'source_indices', 'source_mesh_indices',
                    'source_kinds', 'category_bits','preclassified','brush_effect_rows','brush_phase','rgb_endpoints','line_function_policy')


def _bool(value, name):
    if type(value) is not bool: raise ValueError(name+' must be boolean')
    return value


def normalize(elements, *, style_mode, group_ids):
    """Validate the public request once, before saving or launching a worker."""
    if style_mode not in SUPPORTED: raise ValueError('Unknown element source mode')
    if not isinstance(elements, (list, tuple)) or not 1 <= len(elements) <= MAX_ELEMENTS:
        raise ValueError('Render elements require 1..'+str(MAX_ELEMENTS)+' entries')
    result=[]; seen=set(); known=set(group_ids)
    allowed={'id','label','type','group_uids','line_set_ids','visible_lines_on',
             'hidden_lines_on','background','depth','classification_profile'} | {n+'_on' for n in CATEGORIES}
    for raw in elements:
        if not isinstance(raw,dict) or set(raw)-allowed: raise ValueError('Unknown render element fields')
        uid=raw.get('id')
        if not isinstance(uid,str) or not _ID.fullmatch(uid) or uid in seen:
            raise ValueError('Element ID must be unique, safe ASCII, 1..32 characters')
        seen.add(uid)
        label=raw.get('label',uid)
        if not isinstance(label,str) or len(label)>256: raise ValueError('Invalid element label')
        kind=raw.get('type','COLOR')
        if kind not in ('COLOR','DEPTH'):
            raise ValueError('Unknown render element content type')
        if kind=='COLOR' and 'depth' in raw:
            raise ValueError('COLOR elements cannot contain depth settings')
        profile=PROFILE if style_mode=='PRESET' else BASIC_PROFILE
        if raw.get('classification_profile',profile)!=profile: raise ValueError('Unsupported source classification profile')
        groups=raw.get('group_uids')
        if groups is not None:
            if not isinstance(groups,list) or any(not isinstance(g,str) or g not in known for g in groups):
                raise ValueError('Unknown group UID in render element')
            if len(set(groups))!=len(groups): raise ValueError('Duplicate group UID')
        ids=raw.get('line_set_ids',[True]*8)
        if not isinstance(ids,list) or len(ids)!=8: raise ValueError('line_set_ids requires eight booleans')
        ids=[_bool(v,'line_set_ids') for v in ids]
        row=dict(id=uid,label=label,type=kind,group_uids=None if groups is None else list(groups),line_set_ids=ids,
                 classification_profile=profile,
                 visible_lines_on=_bool(raw.get('visible_lines_on',True),'visible_lines_on'),
                 hidden_lines_on=_bool(raw.get('hidden_lines_on',True),'hidden_lines_on'))
        if kind=='DEPTH':row['depth']=depth_output.normalize(raw.get('depth'))
        for name in CATEGORIES:
            default=name in SUPPORTED[style_mode] and not (style_mode=='PRESET' and name=='wireframe')
            enabled=_bool(raw.get(name+'_on',default),name+'_on')
            if enabled and name not in SUPPORTED[style_mode]:
                raise ValueError('Source category is not implemented for '+style_mode+': '+name)
            row[name+'_on']=enabled
        bg=raw.get('background',{'enabled':False,'rgba':[1.,1.,1.,1.]})
        if not isinstance(bg,dict) or set(bg)-{'enabled','rgba'}: raise ValueError('Invalid element background')
        rgba=bg.get('rgba',[1.,1.,1.,1.])
        if not isinstance(rgba,(list,tuple)) or len(rgba)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in rgba):
            raise ValueError('Background must contain four finite straight linear values')
        if not 0 <= rgba[3] <= 1: raise ValueError('Background alpha must be within 0..1')
        row['background']=dict(enabled=_bool(bg.get('enabled',False),'background.enabled'),rgba=list(map(float,rgba)))
        row['pass_name']='PCL_E_'+uid
        result.append(row)
    return dict(schema=SCHEMA,source_mode=style_mode,elements=result)


def config_sha(config):
    return hashlib.sha256(json.dumps(config,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()


def admit(width,height,count,budget=DEFAULT_BUDGET,*,depth=False):
    if any(type(v) is not int or v<1 for v in (width,height,count,budget)) or count>MAX_ELEMENTS:
        raise ValueError('Invalid element output extent, count or memory budget')
    if width>8192 or height>8192 or width*height>16777216:
        raise ValueError('Element frame exceeds 8192 pixels per side or 16777216 total pixels')
    # GPU base + sample scratch (60 B/px), readback (16), resident RGBA
    # arrays plus two native pass copies (48/pass). Geometry/driver use is
    # scene dependent; the reserve is admission accounting, not a hard cap.
    if type(depth) is not bool:raise ValueError('Depth precision admission flag must be boolean')
    # Depth promotes three resident RGBA surfaces from16F to32F (+24 B/px).
    estimate=width*height*(76+(24 if depth else 0)+48*(count+1))+64*1024*1024
    if estimate>budget: raise ValueError('分层输出预计缓冲超预算：'+str(estimate)+' > '+str(budget)+' 字节')
    return dict(estimated_buffer_bytes=estimate,budget_bytes=budget,geometry_and_driver_hard_cap=False)


def combined_layers(layers):
    return [row for row in layers if not row['elements_only']]


def select_layers(layers,element,*,return_masks=False):
    """Mask all primitive arrays together; never rechain or split brush buckets."""
    result=[];masks=[]
    bits=sum(BITS[n] for n in CATEGORIES if element[n+'_on'])
    visible=element['visible_lines_on']; hidden=element['hidden_lines_on']
    for layer in layers:
        if element['group_uids'] is not None and layer['group_id'] not in element['group_uids']: continue
        number=layer['line_set_id']
        if type(number) is not int or not 1<=number<=8: raise ValueError('Invalid layer numeric line-set ID')
        if not element['line_set_ids'][number-1]: continue
        channel=layer['visibility']
        if channel=='BOTH':
            if not (visible or hidden): continue
            selected_channel='BOTH' if visible and hidden else ('VISIBLE' if visible else 'HIDDEN')
        elif channel in ('VISIBLE','HIDDEN'):
            if not (visible if channel=='VISIBLE' else hidden): continue
            selected_channel=channel
        else: raise ValueError('Invalid source visibility channel')
        mask=(layer['category_bits'] & bits)!=0
        if not np.any(mask): continue
        selected=layer.copy(); selected['visibility']=selected_channel
        for name in PRIMITIVE_ARRAYS:
            if name in layer: selected[name]=layer[name][mask]
        result.append(selected);masks.append(mask)
    return (result,masks) if return_masks else result


def with_background(pixels,background):
    if not background['enabled']: return pixels
    result=pixels.copy(); rgba=np.asarray(background['rgba'],np.float32)
    rgba[:3]*=rgba[3]
    result+=rgba*(1-result[:,:,3,None])
    return result


def basic_layers(geometry,frame,settings):
    """Adapt the exact BASIC source order; enabled predicates may overlap."""
    from .geometry import project_segments
    from .coverage import options as coverage_options
    from .sampling import options as sampling_options
    segments=[];supports=[];mesh_ids=[];source_ids=[];categories=[]
    for mi,row in enumerate(geometry.meshes):
        ids=row.selected_indices(frame['view'],frame['perspective'],settings)
        segments.append(row.vertices[row.edges[ids]])
        supports.append(np.where(row.triangle_supports[ids]>=0,row.triangle_supports[ids]+row.face_offset,0).astype(np.float32))
        bits=np.zeros(len(row.edges),np.uint16)
        if settings.silhouette: bits[row.silhouettes(frame['view'],frame['perspective'])]|=BITS['outline']
        if settings.open_edges: bits[row.counts<=1]|=BITS['outline']
        if settings.marked_edges: bits[row.marks]|=BITS['selected_edges']
        if getattr(settings,'wireframe_edges',False):
            bits[:row.original_edge_count]|=BITS['wireframe']
        if len(row.normals):
            facing=row.facing(frame['view'],frame['perspective']); front=facing[row.faces0]|facing[row.faces1]
            if settings.material_edges: bits[row.material_boundary & front]|=BITS['material_id']
            if settings.crease_edges:
                bits[(row.counts==2)&front&(row.normal_dot<np.cos(np.deg2rad(settings.crease_angle))) ]|=BITS['normal_angle']
        # No source is added by element selection. This matches BASIC's original
        # empty-face/open-edge gate, including overlapping mark eligibility.
        categories.append(bits[ids]);mesh_ids.append(np.full(len(ids),mi,np.int32));source_ids.append(ids)
    segments=np.concatenate(segments) if segments else np.empty((0,2,3),np.float32)
    support=np.concatenate(supports) if supports else np.empty((0,2),np.float32)
    projected,indices,u=project_segments(segments,frame['mvp'],frame['width'],frame['height'],with_parameters=True)
    world=segments[indices,0,None,:]+u[:,:,None]*(segments[indices,1]-segments[indices,0])[:,None,:]
    raster={name:getattr(settings,name) for name in ('coverage_profile','feather_px','samples_per_axis','post_filter_sigma') if hasattr(settings,name)}
    concatenate=lambda rows,dtype:np.concatenate(rows).astype(dtype) if rows else np.empty(0,dtype)
    layer=dict(**coverage_options(raster),**sampling_options(raster),projected=projected,world=world,
        supports=support[indices],widths=np.full((len(indices),2),settings.line_width,np.float32),
        alphas=np.full((len(indices),2),settings.opacity,np.float32),color=settings.line_color,
        visibility=settings.visibility,group_id='__BASIC__',actual_brush='__BASIC__',priority=0,
        line_set_id=settings.basic_line_set_id,elements_only=settings.basic_elements_only,classification=BASIC_PROFILE,
        source_indices=concatenate(source_ids,np.int64)[indices],source_mesh_indices=concatenate(mesh_ids,np.int32)[indices],
        source_kinds=np.zeros(len(indices),np.int8),category_bits=concatenate(categories,np.uint16)[indices])
    return [layer]


def validate_file(path,config,width,height,pixel_aspect):
    from .exr_header import inspect_exr
    names=['Combined']+[row['pass_name'] for row in config['elements']]
    inspected=inspect_exr(path,max_parts=len(names))
    parts=inspected['parts']
    if not inspected['multipart'] or not inspected['chunks_checked'] or len(parts)!=len(names):
        raise ValueError('Incomplete multilayer EXR structure')
    by_name={part['name']:part for part in parts}
    if set(by_name)!={'ToonLines.'+name for name in names}: raise ValueError('Unexpected EXR pass identities')
    for name in names:
        prefix='ToonLines.'+name; part=by_name[prefix]
        if (part['data_window']!=[0,0,width-1,height-1] or part['display_window']!=[0,0,width-1,height-1]
                or part['compression']!=3 or part['color_interop_id']!='lin_rec709_scene'
                or abs(part['pixel_aspect']-pixel_aspect[0]/pixel_aspect[1])>1e-6):
            raise ValueError('EXR extent, encoding or aspect does not match request')
        if {c['name'] for c in part['channels']}!={prefix+'.'+c for c in 'RGBA'} or any(
                c['pixel_type']!=2 or c['x_sampling']!=1 or c['y_sampling']!=1 for c in part['channels']):
            raise ValueError('EXR channel layout does not match request')
    return inspected
