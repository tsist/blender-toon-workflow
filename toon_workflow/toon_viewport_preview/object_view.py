# SPDX-License-Identifier: GPL-3.0-or-later
"""One owned immutable geometry/frame partition shared by phase and painting."""
import numpy as np
import copy
import json
from .edge_sources import object_active


def prepare_steps(geometry,plan,view,mvp,width,height,perspective,*,pixel_scale=1.):
    from .line_hierarchy import active_groups
    if not any(object_active(g) for g in active_groups(plan)):
        return None
    hierarchy=plan['raw']['request'].get('line_hierarchy',{})
    domains=hierarchy.get('merge_domains',[])
    active_ids={g['id'] for g in active_groups(plan)}
    margin=max([g.get('line_options',{}).get('off_screen_distance') or 0. for g in active_groups(plan)]+[0.])
    active_actuals={a['brush_id'] for a in plan['raw']['request'].get('boundary_aliases',[]) if a['group'] in active_ids}
    effect_rows=plan['raw']['request'].get('brush_effects',{}).get('brushes',{})
    margin=max([margin]+[row.get('raster',{}).get('off_screen_distance',0.) for actual,row in effect_rows.items() if actual in active_actuals])*pixel_scale
    merged=bool(domains and any(r['group'] in active_ids and (r['visible'] or r['hidden']) for r in hierarchy.get('merge_groups',[])))
    stamp=(np.asarray(view,np.float64).tobytes(),np.asarray(mvp,np.float64).tobytes(),width,height,bool(perspective),
        json.dumps(domains,sort_keys=True),merged,margin,
        tuple((getattr(m,'merge_domain',None),tuple(getattr(m,'selector_meta',{}).get('collections',()))) for m in geometry.meshes))
    cached=getattr(geometry,'object_partition',None)
    if cached is not None and cached[0]==stamp:
        return cached[1]
    snapshots=[]
    for mesh in geometry.meshes:
        row={name:getattr(mesh,name) for name in ('vertices','edges','original_edge_count','triangles',
             'triangle_polygons','normals','face_start','loop_vertex','face_material')}
        # Distinct instances of one source object remain distinct domains.
        # Direct viewport objects have unique names but no depsgraph instance key.
        key=getattr(mesh,'instance_key',None)
        row['instance_key']=key if key is not None else ('viewport-object',mesh.name)
        if hasattr(mesh,'loop_edge'):row['loop_edge']=mesh.loop_edge
        if hasattr(mesh,'selector_meta'):row['selector_meta']=copy.deepcopy(mesh.selector_meta)
        if hasattr(mesh,'merge_domain'):row['merge_domain']=mesh.merge_domain
        snapshots.append(row)
        yield dict(stage='object-snapshot',object=mesh.name)
    from .object_adapter import classify
    result=classify(snapshots,view,mvp,width,height,bool(perspective),margin_px=margin)
    if merged:
        result['merge_partition']=classify(snapshots,view,mvp,width,height,bool(perspective),
            merge_domains=domains,merge_outline=True,margin_px=margin)
    geometry.object_partition=(stamp,result)
    yield dict(stage='object-classify',parents=sum(result['report']['parent_counts']))
    return result


def partition_for(plan,group,channel,partition):
    if partition is None:return None
    row=next((r for r in plan['raw']['request'].get('line_hierarchy',{}).get('merge_groups',[])
        if r['group']==group['id']),None)
    if row and row['visible' if channel=='VISIBLE' else 'hidden']:
        return partition.get('merge_partition',partition)
    return partition


def predicates(mesh,index,view,perspective,prepared):
    from .edge_sources import predicates as base
    result=base(mesh,view,perspective)
    if prepared is not None:
        result.update(object_parent=prepared['parents'][index],object_open=prepared['opened'][index])
    return result
