# SPDX-License-Identifier: GPL-3.0-or-later
"""Autonomous material line functions. Detached authors and triangle policies.

Color priority for a multi-category primitive follows the first enabled category
in CATEGORIES. Both support materials participate; lowest triangle ID wins color.
Every occluder participates: promotion never punches through an unrelated wall.
"""
import copy
import math
import numpy as np
from .selector_bindings import key, need, face_membership

SCHEMA='toon.viewport-line-functions.v1'
CATEGORIES=('outline','object','intersection','smooth','material','selected_edge','normal_angle','wireframe')
BITS=(1,2,4,8,16,32,64,128)
GATES=('disable_intersection','draw_hidden_lines','draw_hidden_lines_of_targets','mask_hidden_lines_of_targets')
LISTS=('materials','draw_objects','draw_materials','mask_objects','mask_materials')

def default(identifier):
    return dict(id=identifier,**{k:False for k in GATES},**{k:[] for k in LISTS},
        overrides={k:dict(on=False,color=[0.,0.,0.],amount=1.) for k in CATEGORIES})

def records(document):
    ext=document['request'].get('line_functions')
    if ext is None:return None
    need(type(ext) is dict and {'schema','containers','masks'}<=set(ext)<={'schema','containers','masks','self_intersections'} and ext['schema']==SCHEMA,'Invalid line functions schema')
    need(type(ext['containers']) is list and len(ext['containers'])<=256,'Invalid line functions containers')
    ids=set();bindings=set()
    for row in ext['containers']:
        need(type(row) is dict and set(row)=={'id','overrides',*GATES,*LISTS},'Line function fields differ')
        need(type(row['id']) is str and 0<len(row['id'])<=128 and row['id'] not in ids,'Invalid/duplicate line function ID');ids.add(row['id'])
        need(all(type(row[k]) is bool for k in GATES),'Line function switches must be boolean')
        for name in LISTS:
            need(type(row[name]) is list and len(row[name])<=4096,'Invalid line function identity list')
            values=[key(r) for r in row[name]]
            need(len(values)==len(set(values)),'Duplicate line function identity')
            if name=='materials':
                need(not bindings.intersection(values),'Material bound to multiple line function containers');bindings.update(values)
        need(type(row['overrides']) is dict and set(row['overrides'])==set(CATEGORIES),'Invalid line function categories')
        for override in row['overrides'].values():
            need(type(override) is dict and set(override)=={'on','color','amount'} and type(override['on']) is bool,'Invalid color override')
            need(type(override['color']) is list and len(override['color'])==3 and all(type(v) in (float,int) and math.isfinite(v) and 0<=v<=1 for v in override['color']),'Invalid replacement color')
            need(type(override['amount']) in (float,int) and math.isfinite(override['amount']) and 0<=override['amount']<=1,'Invalid replacement amount')
    known={g['id'] for g in document['request']['boundary']['groups']};seen=set()
    need(type(ext['masks']) is list and len(ext['masks'])<=32,'Invalid hidden mask groups')
    for row in ext['masks']:
        need(type(row) is dict and set(row)=={'group','enabled'} and row['group'] in known and row['group'] not in seen and type(row['enabled']) is bool,'Invalid hidden mask group');seen.add(row['group'])
    self_seen=set()
    for row in ext.get('self_intersections',[]):
        need(type(row) is dict and set(row)=={'group','VISIBLE','HIDDEN'} and row['group'] in known and row['group'] not in self_seen and all(type(row[ch]) is bool for ch in ('VISIBLE','HIDDEN')),'Invalid per-channel self intersection');self_seen.add(row['group'])
    return copy.deepcopy(ext)

def visibility_active(document):
    """Only enabled visibility authors request a new channel classification.

    Color replacement, intersection qualification and self-intersection authors
    must retain the original Object/Outline intervals and preclassified flags.
    Holdout is captured from live geometry and combined by the caller.
    """
    ext=records(document)
    if ext is None:return False
    if any(row['enabled'] for row in ext['masks']):return True
    return any(row['materials'] and (row['draw_hidden_lines']
        or row['draw_hidden_lines_of_targets'] and bool(row['draw_objects'] or row['draw_materials'])
        or row['mask_hidden_lines_of_targets'] and bool(row['mask_objects'] or row['mask_materials']))
        for row in ext['containers'])

def visibility_effective(geometry,plan,enabled_groups=None):
    """Resolve author targets against this Line's selected captured sources.

    Use the producer's existing selection predicate and canonical capture,
    including original objects and instance parents. Mere presence of an
    enabled rule for a different object must not replace CPU classification.
    This does not change any geometry, source predicate or visibility math.
    """
    if plan is None or not visibility_active(plan['raw']):return False
    ext=records(plan['raw']);source_objects=set();source_materials=set();source_groups=set();occluder_materials=set()
    groups=[g for g in plan['groups'] if enabled_groups is None or g['id'] in enabled_groups]
    for mesh in geometry.meshes:
        meta=getattr(mesh,'selector_meta',None)
        need(meta is not None,'Visibility rules require captured selector identities')
        if len(mesh.triangles):occluder_materials.update(meta['materials'])
        if getattr(mesh,'holdout',False):continue
        for group in groups:
            if not (group.get('visible_enabled',True) or group.get('hidden_enabled',True)):continue
            whole,faces=face_membership(mesh.name,mesh.face_material_names,group,meta)
            if not whole and not np.any(faces):continue
            source_groups.add(group['id']);source_objects.update(meta['objects'])
            source_materials.update(meta['materials'] if whole else meta['materials'][faces])
    if not source_groups:return False
    def target(row,prefix):
        return bool(source_objects.intersection(key(ref) for ref in row[prefix+'_objects'])
            or source_materials.intersection(key(ref) for ref in row[prefix+'_materials']))
    for row in ext['containers']:
        if not occluder_materials.intersection(key(ref) for ref in row['materials']):continue
        if row['draw_hidden_lines']:return True
        if row['draw_hidden_lines_of_targets'] and target(row,'draw'):return True
        if row['mask_hidden_lines_of_targets'] and target(row,'mask'):return True
    all_groups={g['id']:g for g in plan.get('_all_groups',plan['groups'])}
    for row in ext['masks']:
        gid=row['group']
        if not row['enabled'] or gid not in all_groups or enabled_groups is not None and gid not in enabled_groups:continue
        if not source_groups-{gid}:continue
        for mesh in geometry.meshes:
            if not len(mesh.triangles):continue
            whole,faces=face_membership(mesh.name,mesh.face_material_names,all_groups[gid],mesh.selector_meta)
            if whole or np.any(faces):return True
    return False

def migrate(document,recipe,updates):
    from .style import compile_preset
    from .source_bindings import recover
    compile_preset(document,recipe)
    candidate=copy.deepcopy(document)
    ext=records(candidate) or dict(schema=SCHEMA,containers=[],masks=[])
    need(type(updates) is dict and set(updates)<={'containers','masks','remove','self_intersections'},'Invalid line function update')
    rows={r['id']:r for r in ext['containers']}
    for identifier in updates.get('remove',[]):
        need(identifier in rows,'Unknown line function container');del rows[identifier]
    for change in updates.get('containers',[]):
        need(type(change) is dict and 'id' in change,'Line function ID required')
        row=rows.get(change['id'],default(change['id']));row.update(copy.deepcopy(change));rows[change['id']]=row
    ext['containers']=list(rows.values())
    masks={r['group']:r for r in ext['masks']}
    for row in updates.get('masks',[]):masks[row['group']]=copy.deepcopy(row)
    ext['masks']=list(masks.values());candidate['request']['line_functions']=ext
    if 'self_intersections' in updates:
        self_rows={r['group']:r for r in ext.get('self_intersections',[])}
        for r in updates['self_intersections']:self_rows[r['group']]=copy.deepcopy(r)
        ext['self_intersections']=list(self_rows.values())
    records(candidate)
    plan=compile_preset(candidate,recipe)
    return candidate,plan,recover(candidate),dict(containers=len(rows),masks=len(masks),native_flags_changed=False)

def self_channels(document,gid,fallback):
    ext=records(document)
    row=next((r for r in ext.get('self_intersections',[]) if r['group']==gid),None) if ext else None
    return {ch:(row[ch] if row else bool(fallback)) for ch in ('VISIBLE','HIDDEN')}

def intersection_eligible(data,meshes,disabled):
    result=np.ones(len(data['world']),bool)
    for mi,mesh in enumerate(meshes):
        get=mesh.get if isinstance(mesh,dict) else lambda k:getattr(mesh,k)
        held=mesh.get('holdout',False) if isinstance(mesh,dict) else getattr(mesh,'holdout',False)
        if not disabled and not held:continue
        meta=get('selector_meta')
        for side in (0,1):
            ids=np.flatnonzero(data['references'][:,side,0]==mi)
            local=data['references'][ids,side,1]
            if held:result[ids]=False
            elif disabled:result[ids]&=~np.isin(meta['materials'][get('triangle_polygons')[local]],disabled)
    return result

def apply(geometry,plan,layers):
    ext=records(plan['raw']) if plan is not None else None
    from .holdout import active
    if ext is None and not active(geometry):return layers
    resolved=(plan['_line_function_visibility'] if plan is not None and '_line_function_visibility' in plan
        else visibility_effective(geometry,plan))
    query_visibility=bool(active(geometry) or resolved)
    if ext is None:ext=dict(containers=[],masks=[])
    bound={key(ref):r for r in ext['containers'] for ref in r['materials']}
    masks={r['group'] for r in ext['masks'] if r['enabled']}
    groups={g['id']:g for g in plan.get('_all_groups',plan['groups'])} if plan is not None else {}
    masks.intersection_update(groups)
    # IDs follow the visibility triangle tags, including instance-specific offsets.
    triangles={}
    for mesh in geometry.meshes:
        meta=getattr(mesh,'selector_meta',None)
        need(meta is not None,'Line functions require owning-thread identity capture')
        group_faces={}
        for gid in masks:
            whole,faces=face_membership(mesh.name,mesh.face_material_names,groups[gid],meta)
            group_faces[gid]=np.ones(len(mesh.face_material_names),bool) if whole else faces
        for ti,polygon in enumerate(mesh.triangle_polygons):
            material=str(meta['materials'][polygon])
            triangles[ti+mesh.face_offset]=(tuple(meta['objects']),material,bound.get(material),
                frozenset(gid for gid,faces in group_faces.items() if faces[polygon]),getattr(mesh,'holdout',False))
    stride=max(triangles,default=0)+1
    def targets(row,prefix,objects,materials):
        return bool(set(objects).intersection(key(r) for r in row[prefix+'_objects']) or set(materials).intersection(key(r) for r in row[prefix+'_materials']))
    policies={};table=[]
    for layer in layers:
        count=len(layer['world']);rgb=np.tile(np.asarray(layer['color'],np.float32),(count,1))
        ids=np.full(count,-1,np.float32);disabled=np.zeros(count,bool)
        for i,support in enumerate(layer['supports']):
            source=[triangles[int(t)] for t in sorted(set(support)) if int(t) in triangles]
            source_objects={o for s in source for o in s[0]}
            mesh_index=int(layer['source_mesh_indices'][i]) if 'source_mesh_indices' in layer else -1
            if 0<=mesh_index<len(geometry.meshes):source_objects.update(geometry.meshes[mesh_index].selector_meta['objects'])
            objects=tuple(sorted(source_objects));materials=tuple(sorted({s[1] for s in source}))
            policy=(objects,materials,layer.get('group_id'))
            if query_visibility and policy not in policies:
                # flags: x=ignore occluder, y=mask hidden. Mask wins conflicts.
                flags=np.zeros((stride,4),np.float32)
                for tag,(obs,mat,row,mask_groups,held) in triangles.items():
                    promoted=bool(row and (row['draw_hidden_lines'] or row['draw_hidden_lines_of_targets'] and targets(row,'draw',objects,materials)))
                    masked=bool(mask_groups-{policy[2]} or row and not row['draw_hidden_lines'] and row['mask_hidden_lines_of_targets'] and targets(row,'mask',objects,materials))
                    flags[tag,:2]=(promoted,masked or held)
                policies[policy]=len(table)*stride;table.append(flags)
            if query_visibility:ids[i]=policies[policy]
            bits=int(layer['category_bits'][i]) if 'category_bits' in layer else 0
            disabled[i]=bool(any(s[4] for s in source) or 0<=mesh_index<len(geometry.meshes) and getattr(geometry.meshes[mesh_index],'holdout',False) or bits&4 and any(s[2] and s[2]['disable_intersection'] for s in source))
            chosen=False
            for s in source:
                if not s[2]:continue
                for category,bit in zip(CATEGORIES,BITS):
                    override=s[2]['overrides'][category]
                    if bits&bit and override['on']:
                        rgb[i]=rgb[i]*(1-override['amount'])+np.asarray(override['color'])*override['amount'];chosen=True;break
                if chosen:break
        # Zero Alpha preserves all source/phase identity and array alignment.
        layer['alphas']=layer['alphas'].copy();layer['alphas'][disabled]=0
        layer['widths']=layer['widths'].copy();layer['widths'][disabled]=0
        layer['rgb_endpoints']=np.repeat(rgb[:,None,:],2,axis=1);layer['line_function_policy']=ids
    packed=np.concatenate(table) if table else np.zeros((1,4),np.float32)
    need(len(packed)<2**24,'Line function GPU policy address exceeds exact float32 range')
    for layer in layers:layer['line_function_table']=packed
    return layers
