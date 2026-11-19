# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure owned-array phase operation; no bpy/RNA/GPU imports or closures."""
import numpy as np
from .boundary_sources import build_boundary_sources
from .phase_worker import compute_boundary_phases
from .intersection_chain_compute import compute_intersection_phases

def compute_snapshot(payload,checkpoint):
    batches=payload['plan'].get('line_batches')
    if batches is not None:
        results=[]
        for batch in batches:
            ids=set(batch['groups']);sub=dict(payload);plan=dict(payload['plan']);sub['plan']=plan
            plan.pop('line_batches',None);plan['groups']=[g for g in plan['groups'] if g['id'] in ids]
            raw=dict(plan['raw']);req=dict(raw['request']);raw['request']=req;plan['raw']=raw
            req['boundary_aliases']=[a for a in req.get('boundary_aliases',[]) if a['group'] in ids]
            weld=payload['weld_requests']
            sub['weld_requests']=({gid:r for gid,r in weld.items() if gid in ids} if isinstance(weld,dict)
                else [r for r in weld if r['group_id'] in ids])
            ix=dict(payload['intersection']);sub['intersection']=ix
            if ix.get('plan') is not None:
                ix['plan']=dict(ix['plan'],groups=[g for g in ix['plan']['groups'] if g['id'] in ids])
            results.append(compute_snapshot(sub,checkpoint))
        result=dict(boundary={},intersection={},enabled_actuals=[],intersection_enabled_actuals=[],
            intersection_requested_actuals=[],reports=[],source_summary=dict(lines=len(results)),weld=None,
            intersection_status=dict(state='ready',reason='independent Line phase domains'),
            qualification='autonomous independent Lines')
        if payload.get('include_dash_metric',False):result.update(boundary_arc={},intersection_arc={})
        for row in results:
            for name in ('boundary','intersection','boundary_arc','intersection_arc'):
                if name in row:result[name].update(row[name])
            for name in ('enabled_actuals','intersection_enabled_actuals','intersection_requested_actuals'):
                result[name]=sorted(set(result[name])|set(row.get(name,[])))
            result['reports'].extend(row.get('reports',[]))
            if row['intersection_status']['state']=='pending':result['intersection_status']=row['intersection_status']
        return result
    include_arc=payload.get('include_dash_metric',False)
    if type(include_arc) is not bool:raise ValueError('include_dash_metric must be a boolean')
    controls=payload['controls']['brushes']
    checkpoint()
    if any(row['boundary_enabled'] for row in controls.values()):
        result=_compute_boundary_snapshot(payload,checkpoint,include_arc=include_arc)
    else:
        result=dict(boundary={},enabled_actuals=[],reports=[],
                    source_summary=dict(boundary='not requested; source builder skipped'),weld=None)
        if include_arc:result['boundary_arc']={}
    intersections=compute_intersection_phases(payload,checkpoint)
    result.update(intersection=intersections['lookup'],
        intersection_enabled_actuals=intersections['enabled_actuals'],
        intersection_requested_actuals=intersections['requested_actuals'],
        intersection_status=intersections['status'],intersection_reports=intersections['reports'])
    if include_arc:result['intersection_arc']=intersections['arc_lookup']
    result['reports']=result['reports']+intersections['reports']
    result['qualification']='autonomous enabled source phase only; current authors remain on owning thread'
    checkpoint()
    return result


def _compute_boundary_snapshot(payload,checkpoint,*,include_arc=False):
    controls=payload['controls']['brushes']
    meshes=[]
    for source in payload['meshes']:
        checkpoint()
        if source.get('holdout',False):continue
        mesh=dict(source)
        names=mesh.pop('face_material_names')
        palette,indices=np.unique(names,return_inverse=True)
        mesh['face_material_palette']=palette.tolist();mesh['face_material_ids']=indices.astype(np.int32)
        meshes.append(mesh)
    if not payload['meshes']:
        result=dict(boundary={},reports=[],qualification='empty owned geometry; no source phase')
        if include_arc:result['boundary_arc']={}
        return result
    built=build_boundary_sources(meshes=meshes,frame=payload['frame'],plan=payload['plan'],
        settings=payload['settings'],capture_id=payload['capture_id'],actual_registry=payload['actual_registry'],
        weld_requests=payload['weld_requests'],include_linker_sources=True,checkpoint=checkpoint,
        enabled_actuals={actual for actual,row in controls.items() if row['boundary_enabled']},
        projection_profile='positive_w_unclipped')
    table=built['sources'];rows=built['linker_sources'];indices=[];rules={}
    for i,(row,actual) in enumerate(zip(rows,table['actual_brush_id'])):
        if i%256==0:checkpoint()
        control=controls.get(str(actual))
        if control is None or not control['boundary_enabled']:continue
        indices.append(i)
        policy=dict(control['policy']);policy['min_length_px']*=payload['pixel_scale']
        owned=payload.get('owned_brush_chains',{}).get(str(actual),{})
        if owned.get('weld_edges') is True:
            # Exact world-position weld is an owned phase topology policy.
            # Mesh arrays, native weld flags and source IDs stay immutable.
            row['domain']='owned-weld|'+str(table['group_id'][i])+'|'+str(actual)+'|'+str(table['channel'][i])
            row['endpoint_ids']=['owned-world:'+','.join(float(v).hex() for v in p) for p in table['world'][i]]
        rules.setdefault(row['domain'],dict(policy=policy,max_gap_px=control['max_gap_px']*payload['pixel_scale']))
        anchor=owned.get('seam_anchor')
        if anchor is not None:
            mesh=next((m for m in payload['meshes'] if m['mesh_index']==int(table['mesh_index'][i])),None)
            if mesh is not None and mesh['name']==anchor['object'] and int(table['edge_index'][i])==anchor['edge']:
                rules[row['domain']]['seam_anchor']=dict(source_id=row['source_id'],endpoint=anchor['endpoint'])
    selected=[rows[i] for i in indices]
    planned=compute_boundary_phases(dict(sources=selected,domain_rules=rules),checkpoint,include_arc=include_arc)
    lookup={}
    if include_arc:arc_lookup={}
    for ordinal,i in enumerate(indices):
        if ordinal%256==0:checkpoint()
        key=(int(table['mesh_index'][i]),str(table['group_id'][i]),str(table['slot'][i]),str(table['channel'][i]),int(table['edge_index'][i]))
        lookup[key]=(tuple(float(v) for v in planned['parameters'][ordinal]),bool(planned['active'][ordinal]))
        if include_arc:arc_lookup[key]=tuple(float(v) for v in planned['arc_px'][ordinal])
    checkpoint()
    result=dict(boundary=lookup,enabled_actuals=[actual for actual,row in controls.items() if row['boundary_enabled']],reports=planned['reports'],source_summary=built['summary']['counts'],
        weld=dict(native=built['qualification']['weld'],owned_exact_world_actuals=[a for a,r in payload.get('owned_brush_chains',{}).items() if r.get('weld_edges') is True]),projection=built['qualification']['projection'],
        source_selection=built['qualification']['source_selection'],
        qualification='autonomous enabled boundary phase only; current authors remain on owning thread')
    if include_arc:result['boundary_arc']=arc_lookup
    return result
