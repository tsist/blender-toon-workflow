# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit static material-to-ink RGB, owned by the mother joint renderer.

This stage follows legacy brush/maps and precedes Eye/DEPTH. It never expands
strokes or touches positions, source-u, widths, alpha or visibility policies.
"""
import copy
import hashlib
import json
import math

KEY = 'toon_line_material_colors'
SCHEMA = 'toon.line-material-colors.v1'
RECEIPT = 'toon.line-material-colors-resolved.v1'
SCHEMA_V2='toon.line-material-colors.v2'
RECEIPT_V2='toon.line-material-colors-resolved.v2'
CATEGORIES = ('outline','object','intersection','smooth','material','selected_edge','normal_angle','wireframe')
BITS = (1,2,4,8,16,32,64,128)


def _need(value, message):
    if not value: raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value): return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _rgb(value):
    return (type(value) is list and len(value)==3 and all(type(v) in (int,float)
        and math.isfinite(v) and 0<=v<=1 for v in value))


def normalize(payload, known_groups=None):
    _need(type(payload) is dict and set(payload)=={'schema','policies'} and payload['schema'] in (SCHEMA,SCHEMA_V2),
          'Invalid material line-color schema')
    _need(type(payload['policies']) is list and len(payload['policies'])<=256, 'Invalid line-color policies')
    ids=set()
    for row in payload['policies']:
        _need(type(row) is dict and set(row)=={'id','material','groups','categories','source','amount','gain','tint'},
              'Invalid material line-color policy fields')
        _need(type(row['id']) is str and 0<len(row['id'])<=128 and row['id'] not in ids,
              'Invalid or duplicate line-color policy ID'); ids.add(row['id'])
        ref=row['material']
        _need(type(ref) is dict and set(ref)=={'name','library'} and type(ref['name']) is str
              and 0<len(ref['name'])<=1024 and ref['library'] is None, 'Choose an explicit local material')
        groups=row['groups']; cats=row['categories']
        _need(type(groups) is list and len(groups)<=32 and all(type(g) is str and g for g in groups)
              and len(set(groups))==len(groups), 'Invalid line-color groups')
        if known_groups is not None: _need(set(groups)<=set(known_groups), 'Unknown line-color group')
        _need(type(cats) is list and cats and all(type(c) is str and c in CATEGORIES for c in cats)
              and len(set(cats))==len(cats), 'Invalid line-color categories')
        for field in ('amount','gain'):
            v=row[field]
            _need(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1, 'Invalid line-color '+field)
        _need(_rgb(row['tint']), 'Invalid linear RGB tint')
        src=row['source']; _need(type(src) is dict, 'Invalid material color source')
        if src.get('kind')=='UV_IMAGE':
            _need(payload['schema']==SCHEMA_V2,'UV image sources require the explicit v2 author schema')
            from . import line_image_source
            line_image_source.normalize(src)
        elif src.get('kind')=='AUTHOR_RGB':
            _need(set(src)=={'kind','color'} and _rgb(src['color']), 'Invalid author RGB source')
        else:
            _need(set(src)=={'kind','node','direction','socket'} and src['kind']=='NODE_RGBA'
                  and src['direction'] in ('INPUT','OUTPUT') and all(type(src[k]) is str and 0<len(src[k])<=1024
                  for k in ('node','socket')), 'Specify an explicit constant RGBA node socket')
    return copy.deepcopy(payload)


def records(plan):
    payload=plan['raw']['request'].get(KEY) if plan is not None else None
    return None if payload is None else normalize(payload, [g['id'] for g in plan['groups']])


def protect_cel_sources(scene, records):
    """Protect source dependencies before CEL mutations, not after commit.

    A CEL update keeps the owned Material and its used-slot identity. An
    AUTHOR_RGB policy reads its own constant, so that update has no color
    dependency. Creation replaces the slot Material; node/image sources have
    additional graph or sampling dependencies and remain protected here.
    The CEL caller already validates ownership, hand edits and slot identity.
    """
    settings=getattr(scene,'toon_viewport_preview',None)
    if settings is None or settings.style_mode!='PRESET' or settings.preset_text is None:return
    document=json.loads(settings.preset_text.as_string())['document']
    payload=document['request'].get(KEY)
    if payload is None:return
    bound={}
    for policy in normalize(payload)['policies']:
        bound.setdefault(policy['material']['name'], []).append(policy)
    for row in records:
        material=row['material']
        policies=bound.get(material.name_full, []) if material is not None else []
        if row['action'] in ('create','update') and policies:
            if row['action']=='update' and all(p['source']['kind']=='AUTHOR_RGB' for p in policies):
                continue
            raise ValueError('CEL would change a material ink source; remove/rebind its explicit ink policy first: '+material.name_full)


def _source(material, src, scene=None):
    if src['kind']=='UV_IMAGE':
        from . import line_image_source
        return line_image_source.source(material,src,scene)
    if src['kind']=='AUTHOR_RGB': return list(src['color']),dict(kind='AUTHOR_RGB')
    tree=material.node_tree
    _need(material.use_nodes and tree is not None, 'Material has no node tree: '+material.name)
    animation=tree.animation_data
    _need(not animation or (not animation.action and not animation.drivers and not animation.nla_tracks),
          'Animated node trees require a separate material-color time contract')
    node=tree.nodes.get(src['node'])
    _need(node is not None and not node.mute, 'Explicit source node is missing or muted: '+src['node'])
    inputs=src['direction']=='INPUT'
    _need((inputs and node.bl_idname in ('ShaderNodeBsdfPrincipled','ShaderNodeEmission','ShaderNodeGroup'))
          or (not inputs and node.bl_idname=='ShaderNodeRGB'), 'Unsupported constant color node')
    sockets=node.inputs if inputs else node.outputs
    matches=[s for s in sockets if s.identifier==src['socket']]
    _need(len(matches)==1, 'Specify the unique socket identifier, not its label')
    socket=matches[0]
    _need(socket.type=='RGBA' and (not inputs or not socket.is_linked), 'Color source must be an unlinked RGBA input or RGB output')
    color=list(socket.default_value)[:3]
    _need(_rgb(color), 'Source RGB must be finite scene-linear values in [0,1]')
    return color,dict(kind='NODE_RGBA',node_type=node.bl_idname,socket_type=socket.type)


def resolve(scene, plan):
    payload=records(plan)
    if payload is None: return None
    import bpy
    rows=[]
    for row in payload['policies']:
        material=bpy.data.materials.get(row['material']['name'])
        _need(material is not None and not material.library and not material.override_library,
              'Explicit material is missing or nonlocal: '+row['material']['name'])
        used=any(obj.type=='MESH' and any(p.material_index==i for p in obj.data.polygons)
                 for obj in scene.objects for i,s in enumerate(obj.material_slots) if s.material==material)
        _need(used, 'Line-color material must occupy a used Scene Mesh slot: '+material.name)
        rgb,descriptor=_source(material,row['source'],scene)
        rows.append(dict(policy=copy.deepcopy(row), source_rgb=rgb,source_descriptor=descriptor,
                         target_rgb=None if rgb is None else [rgb[i]*row['gain']*row['tint'][i] for i in range(3)]))
    data=dict(schema=RECEIPT_V2 if payload['schema']==SCHEMA_V2 else RECEIPT, policy_sha256=_sha(payload), policies=rows,
              stage='after-legacy-brush-maps-before-eye-depth',
              multi_support='first matching policy in explicit author order',
              color_space='scene_linear_Rec709', alpha_width_geometry='unchanged')
    data['sha256']=_sha(data)
    return data


def validate_receipt(data):
    _need(type(data) is dict and set(data)=={'schema','policy_sha256','policies','stage','multi_support',
          'color_space','alpha_width_geometry','sha256'} and data['schema'] in (RECEIPT,RECEIPT_V2), 'Invalid line-color receipt')
    base={k:v for k,v in data.items() if k!='sha256'}
    _need(_sha(base)==data['sha256'], 'Resolved material-color identity changed')
    _need(data['stage']=='after-legacy-brush-maps-before-eye-depth'
          and data['multi_support']=='first matching policy in explicit author order'
          and data['color_space']=='scene_linear_Rec709' and data['alpha_width_geometry']=='unchanged',
          'Unsupported material-color stage')
    _need(type(data['policies']) is list and len(data['policies'])<=256, 'Invalid resolved policy list')
    _need(all(type(r) is dict and set(r)=={'policy','source_rgb','target_rgb','source_descriptor'} for r in data['policies']),
          'Invalid resolved policy fields')
    payload=normalize(dict(schema=SCHEMA_V2 if data['schema']==RECEIPT_V2 else SCHEMA,policies=[r['policy'] for r in data['policies']]))
    for row in data['policies']:
        if row['policy']['source']['kind']=='UV_IMAGE':
            from . import line_image_source
            line_image_source.validate_descriptor(row['source_descriptor'])
            _need(row['source_rgb'] is None and row['target_rgb'] is None
                  and row['policy']['source']['image']==row['source_descriptor']['image'],'UV source must retain per-fragment color semantics')
            continue
        _need(_rgb(row['source_rgb'])
              and _rgb(row['target_rgb']), 'Invalid resolved material RGB')
        p=row['policy']
        descriptor=row['source_descriptor']
        if p['source']['kind']=='AUTHOR_RGB':_need(descriptor==dict(kind='AUTHOR_RGB'),'Author source descriptor differs')
        else:
            allowed=('ShaderNodeRGB',) if p['source']['direction']=='OUTPUT' else ('ShaderNodeBsdfPrincipled','ShaderNodeEmission','ShaderNodeGroup')
            _need(type(descriptor) is dict and set(descriptor)=={'kind','node_type','socket_type'}
                  and descriptor['kind']=='NODE_RGBA' and descriptor['node_type'] in allowed
                  and descriptor['socket_type']=='RGBA','Constant source descriptor differs')
        _need(row['target_rgb']==[row['source_rgb'][i]*p['gain']*p['tint'][i] for i in range(3)], 'Derived line target differs')
        if p['source']['kind']=='AUTHOR_RGB': _need(row['source_rgb']==p['source']['color'], 'Author RGB differs')
    _need(_sha(payload)==data['policy_sha256'], 'Line-color policy identity differs')
    return len(data['policies'])


def _target_rows(layer, by_material, policies, first=0, end=None):
    import numpy as np
    end=len(layer['world']) if end is None else end
    count=end-first;used=np.zeros(count,bool);rows=np.zeros((count,4),np.float32);counts={}
    for policy_index,row in enumerate(policies):
        p=row['policy'];counts[p['id']]=0
        if p['groups'] and layer.get('group_id') not in p['groups']:continue
        if p['amount']==0:continue
        bits=sum(BITS[CATEGORIES.index(c)] for c in p['categories'])
        mask=(~used & ((layer['category_bits'][first:end].astype(np.int64)&bits)!=0)
              & np.isin(layer['supports'][first:end],by_material.get(_json(p['material']),[])).any(axis=1))
        rows[mask,:3]=[-float(policy_index+1),0.,0.] if p['source']['kind']=='UV_IMAGE' else row['target_rgb']
        rows[mask,3]=p['amount']
        counts[p['id']]=int(np.count_nonzero(mask));used|=mask
    return rows,counts


def apply(geometry, layers, resolved):
    if resolved is None: return layers, None
    import numpy as np
    validate_receipt(resolved)
    tags={}
    for mesh in geometry.meshes:
        meta=getattr(mesh,'selector_meta',None)
        _need(meta is not None, 'Material line colors require captured material identities')
        for ti,polygon in enumerate(mesh.triangle_polygons):
            tags[ti+mesh.face_offset]=str(meta['materials'][polygon])
    by_material={}
    for tag,material in tags.items(): by_material.setdefault(material,[]).append(tag)
    from . import line_image_source
    runtime=line_image_source.build(geometry,resolved)
    counts={r['policy']['id']:0 for r in resolved['policies']}
    for layer in layers:
        count=len(layer['world'])
        if not count: continue
        _need('supports' in layer and 'category_bits' in layer, 'Material line colors require PRESET source provenance')
        _,matched=_target_rows(layer,by_material,resolved['policies'])
        for key,n in matched.items():counts[key]+=n
        # Dictionaries survive native category selection unchanged. Resolve
        # against that selection's actual supports at batch time; no new
        # per-primitive array can become misaligned after element filtering.
        layer['_toon_material_color_binding']=(by_material,resolved['policies'])
        if runtime is not None:layer['_toon_material_uv_runtime']=runtime
    report=dict(resolved=resolved,matched_primitives=counts)
    if runtime is not None:report['uv_sources']=runtime['receipt']
    return layers,report


def renderer_class(base, *, uv_runtime=None):
    """Private uniform stage after GPU brush color, without extra samplers.

    Consecutive equal-color runs retain primitive order and the same layer
    maximum-alpha buffer. They do not split/reclassify geometric source rows.
    """
    import types
    import numpy as np
    native_init,native_render=base.__init__,base.render_steps
    init_globals=dict(native_init.__globals__);render_globals=dict(native_render.__globals__)
    original_shader=init_globals['_shader'];original_batch=render_globals['_line_batch'];injected=[0]
    marker='if(brushEffectId>=0.0)rgb=authoredBrushColor(rgb,t,samplePosition);'
    texture=None
    if uv_runtime is not None:
        from .backend import pencil
        texture=pencil('drawing').Renderer._alpha_texture(uv_runtime['table'])
    def shader(*args,**kwargs):
        args=list(args)
        if len(args)>=4 and marker in args[3]:
            _need(args[3].count(marker)==1,'Material color fragment seam changed')
            args[1]=list(args[1])+[('VEC4','toonMaterialColor')]
            blend='if(depthRange.z<0.5 && toonMaterialColor.a>0.0)rgb=mix(rgb,toonMaterialColor.rgb,toonMaterialColor.a);'
            if texture is not None:
                from .line_image_source import GLSL
                args[3]=args[3].replace('void main(){',GLSL+'\nvoid main(){',1)
                blend='if(depthRange.z<0.5 && toonMaterialColor.a>0.0){vec3 target=toonMaterialColor.r<0.0?toonInkImageColor(int(-toonMaterialColor.r)-1,point,supports):toonMaterialColor.rgb;rgb=mix(rgb,target,toonMaterialColor.a);}'
                samplers=list(args[5] if len(args)>5 else kwargs.get('samplers',()))
                native=['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels']
                _need(samplers in (native,native+['managedEyeTriangles']),'UV source sampler layout changed')
                samplers.insert(7,'toonInkUV')
                if len(args)>5:args[5]=samplers
                else:kwargs['samplers']=samplers
            args[3]=args[3].replace(marker,marker+'\n                '+blend,1)
            injected[0]+=1
        return original_shader(*args,**kwargs)
    class Batch:
        def __init__(self,items):self.items=items
        def draw(self,real_shader):
            if texture is not None:real_shader.uniform_sampler('toonInkUV',texture)
            for color,batch in self.items:
                real_shader.uniform_float('toonMaterialColor',color)
                batch.draw(real_shader)
    def line_batch(real_shader,layer,alpha_ids,matrix,first,end):
        binding=layer.get('_toon_material_color_binding')
        rows=(_target_rows(layer,*binding,first,end)[0] if binding is not None
              else np.zeros((end-first,4),np.float32))
        cuts=np.r_[0,np.flatnonzero(np.any(rows[1:]!=rows[:-1],axis=1))+1,len(rows)]
        return Batch([(tuple(rows[a]),original_batch(real_shader,layer,alpha_ids,matrix,first+int(a),first+int(b)))
                      for a,b in zip(cuts[:-1],cuts[1:]) if b>a])
    init_globals['_shader']=shader;render_globals['_line_batch']=line_batch
    initialize=types.FunctionType(native_init.__code__,init_globals,native_init.__name__,native_init.__defaults__,native_init.__closure__)
    initialize.__kwdefaults__=native_init.__kwdefaults__
    render=types.FunctionType(native_render.__code__,render_globals,native_render.__name__,native_render.__defaults__,native_render.__closure__)
    render.__kwdefaults__=native_render.__kwdefaults__
    return type('ToonMaterialInkRenderer',(base,),dict(__init__=initialize,render_steps=render,_toon_material_injected=injected,_toon_uv_texture=texture))


def verify_renderer(renderer):
    _need(getattr(renderer,'_toon_material_injected',None)==[1], 'Expected one material color shader stage')
