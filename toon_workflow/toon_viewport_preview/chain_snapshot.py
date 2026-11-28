# SPDX-License-Identifier: GPL-3.0-or-later
"""Main-thread snapshot producer. Returned payload contains only owned arrays/JSON."""
import copy
import uuid
import numpy as np

_ARRAYS=('vertices','edges','counts','normals','centers','faces0','faces1',
         'corners0','corners1','triangle_supports','virtual_edges','triangles','triangle_polygons')
_IX_GROUP_FIELDS=('id','priority','object_ids','material_names','visible_enabled','hidden_enabled','actual_brush')
_IX_ARRAYS=('world','edges','pair','references','supports','triangles')
_GROUP_FIELDS=('id','priority','object_ids','material_names','visible_enabled','hidden_enabled',
               'silhouette_enabled','marked_enabled','smoothing_enabled','brush_slots')

def snapshot_steps(geometry, plan, view, mvp, perspective, width, height, scale, settings, bindings, controls):
    """Call only on the Blender owning thread; yield between independent copies.

    No current brush curves/maps/width/color are sent to the phase worker.
    Geometry/RNA/Mathutils objects are read here and never enter the payload.
    """
    frame=dict(view=np.asarray(view,np.float64).copy(),mvp=np.asarray(mvp,np.float64).copy(),
               perspective=bool(perspective),width=int(width),height=int(height))
    from . import object_view
    object_partition=yield from object_view.prepare_steps(geometry,plan,view,mvp,width,height,perspective,pixel_scale=scale)
    groups=[]
    wire_present=any('wire_sources' in group for group in plan['groups'])
    edge_active=any(group.get('edge_sources',{}).get('enabled') for group in plan['groups'])
    for group in plan['groups']:
        row={key:copy.deepcopy(group[key]) for key in _GROUP_FIELDS}
        row['brushes']={slot:{} for slot in group['brush_slots']}
        row['brush_meta']={slot:dict(uid=group['brush_meta'][slot]['uid']) for slot in group['brush_slots']}
        if 'wire_sources' in group:row['wire_sources']=copy.deepcopy(group['wire_sources'])
        if 'edge_sources' in group:row['edge_sources']=copy.deepcopy(group['edge_sources'])
        if 'selection' in group:row['selection']=copy.deepcopy(group['selection'])
        if 'object_partition' in group:row['object_partition']=group['object_partition']
        groups.append(row)
    active_aliases=[a for a in plan['raw']['request'].get('boundary_aliases',[])
        if a['group'] not in plan['raw']['request'].get('group_lifecycle',{}).get('archived',{})]
    minimal_plan=dict(groups=groups,settings=dict(source_categories=copy.deepcopy(plan['settings'].get('source_categories',['selected','smoothing'])),
        include_open=plan['settings'].get('include_open',True)),
        raw=dict(request=dict(boundary_aliases=copy.deepcopy(active_aliases))))
    if 'line_hierarchy' in plan['raw']['request']:
        minimal_plan['line_batches']=[dict(id=line['id'],groups=ids) for line,ids in __import__(
            __package__+'.line_hierarchy',fromlist=['batches']).batches(plan)]
    runtime=dict(visibility=str(settings.visibility),groups=[dict(id=str(g.name),enabled=bool(g.enabled)) for g in settings.groups])
    boundary_enabled=any(row['boundary_enabled'] for row in controls['brushes'].values())
    ix_requested=any(row['intersection_enabled'] for row in controls['brushes'].values())
    intersection=dict(global_enabled=bool(getattr(settings,'intersection_enabled',True)),
                      plan=None,sources=None,endpoint_count=None)
    if ix_requested and intersection['global_enabled']:
        from .intersection_strokes import _plan
        compiled=_plan(plan)
        if compiled is not None:
            source_groups=[]
            for group in compiled['groups']:
                row={key:copy.deepcopy(group[key]) for key in _IX_GROUP_FIELDS}
                if 'selection' in group:row['selection']=copy.deepcopy(group['selection'])
                if 'self_channels' in group:row['self_channels']=copy.deepcopy(group['self_channels'])
                if 'channels' in group:
                    row['channels']={ch:{key:copy.deepcopy(value[key]) for key in ('enabled','actual_brush')}
                                     for ch,value in group['channels'].items()}
                source_groups.append(row)
            intersection['plan']=dict(
                groups=source_groups,
                self_intersection=bool(compiled['self_intersection']),policy=str(compiled['policy']))
            if compiled.get('channel_ownership'):intersection['plan']['channel_ownership']=True
            if 'disabled_materials' in compiled:intersection['plan']['disabled_materials']=copy.deepcopy(compiled['disabled_materials'])
            if hasattr(geometry,'intersection_sources'):
                source=geometry.intersection_sources
                intersection['sources']={}
                for key in _IX_ARRAYS:
                    intersection['sources'][key]=np.array(source[key],copy=True)
                    yield dict(stage='chain-snapshot-intersection',field=key)
                declared=source.get('stats',{}).get('topology_endpoints')
                if declared is not None:intersection['endpoint_count']=int(declared)
    ix_owned=intersection['sources'] is not None
    meshes=[]
    for index,mesh in enumerate(geometry.meshes if boundary_enabled or ix_owned else ()):

        row=dict(mesh_index=index,name=str(mesh.name),face_offset=int(mesh.face_offset))
        row['holdout']=bool(getattr(mesh,'holdout',False))
        if hasattr(mesh,'selector_meta'):row['selector_meta']=copy.deepcopy(mesh.selector_meta)
        for key in (_ARRAYS if boundary_enabled else ('triangles','triangle_polygons')):
            row[key]=np.array(getattr(mesh,key),copy=True)
            yield dict(stage='chain-snapshot',object=mesh.name,field=key)
        if boundary_enabled:
            if wire_present or edge_active:row['original_edge_count']=int(mesh.original_edge_count)
            if edge_active:
                row['edge_predicates']=object_view.predicates(mesh,index,view,perspective,object_partition)
            row['resolved_kinds']=np.array(mesh.categories(view,perspective),copy=True)
            yield dict(stage='chain-snapshot',object=mesh.name,field='resolved_kinds')
        row['face_material_names']=np.array(mesh.face_material_names,copy=True)
        yield dict(stage='chain-snapshot',object=mesh.name,field='face_material_names')
        meshes.append(row)
    registry={actual:dict(source_brush_key=row['source_brush_key'],source_detail_key=row['source_detail_key'])
              for actual,row in bindings['actuals'].items()}
    return dict(meshes=meshes,frame=frame,plan=minimal_plan,settings=runtime,
        intersection=intersection,capture_id='viewport-phase:'+uuid.uuid4().hex,actual_registry=registry,
        weld_requests=copy.deepcopy(bindings['groups']),controls=copy.deepcopy(controls),pixel_scale=float(scale),
        owned_brush_chains={a:copy.deepcopy(r['chain']) for a,r in __import__(__package__+'.brush_effects',fromlist=['records']).records(plan['raw']).items()})
