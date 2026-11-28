# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit intersection-request styles on cached world-space segment sources."""
import numpy as np
from .intersection_sources import capture
from .style import map_triangle_points,size_scale,_validate_mapping,_number,_boolean,_color
from .coverage import options as coverage_options
from .sampling import options as sampling_options
from . import map_amount_controls as map_amount

def _plan(plan):
    if 'viewport_intersection_plan' in plan:return plan['viewport_intersection_plan']
    raw=plan['raw']['request'].get('intersection')
    if raw is None:return None
    if raw.get('schema') not in ('toon-painted-intersection-image-request.v1','toon-painted-intersection-image-request.v3'):
        raise ValueError('Unsupported viewport intersection request schema')
    settings=raw['settings']
    if settings.get('point_policy','suppress')!='suppress':
        raise ValueError('Viewport intersection supports explicit point suppression only')
    _boolean(settings['self_intersection'],'intersection.self_intersection')
    policy=settings.get('support_policy','priority_front_side.v1')
    if policy not in ('priority_front_side.v1','priority_least_absolute_facing.v1'):
        raise ValueError('Unknown intersection support policy')
    controls={g['id']:g for g in plan['groups']}
    from . import ix_sources
    source_records=ix_sources.records(plan['raw'])
    aliases={x['group']:x['brush_id'] for x in plan['raw']['request'].get('intersection_aliases',[])}
    groups=[];ids=set();wire_filtered=False
    archived=plan['raw']['request'].get('group_lifecycle',{}).get('archived',{})
    sources=[g for g in raw['groups'] if g['id'] not in archived and g['id'] in controls]
    sources.extend(dict(id=gid) for gid,r in source_records.items()
                   if r['enabled'] and gid not in archived and gid in controls and gid not in {g['id'] for g in sources})
    for source in sources:
        gid=source['id']
        if gid in ids or gid not in controls:raise ValueError('Unbound or repeated intersection group')
        ids.add(gid)
        # Autonomous wire replaces this group's entire intersection source.
        # Retained disabled records restore the original intersection route.
        record=source_records.get(gid)
        explicit=record is not None and record['enabled']
        if (not explicit and controls[gid].get('wire_sources',{}).get('enabled') is True
                and controls[gid].get('edge_sources',{}).get('enabled') is not True):
            wire_filtered=True
            continue
        if explicit:
            bound=controls[gid]
            channels={ch:dict(enabled=route['enabled'],actual_brush=record['authors'][route['slot']]['actual'],
                brush=record['authors'][route['slot']]['brush'],slot=route['slot']) for ch,route in record['routes'].items()}
            groups.append(dict(id=gid,priority=bound['priority'],brush=channels['VISIBLE']['brush'],
                object_ids=bound['object_ids'],material_names=bound['material_names'],control=bound,
                visible_enabled=channels['VISIBLE']['enabled'],hidden_enabled=channels['HIDDEN']['enabled'],
                actual_brush=channels['VISIBLE']['actual_brush'],channels=channels,selection=bound.get('selection')))
            continue
        brush=source['brush']
        for name in ('width_map','alpha_map'):
            if brush[name] is not None:_validate_mapping(brush[name],'intersection.'+name)
        _number(brush['width_px'],'intersection.width_px',0)
        _number(brush['blend_amount'],'intersection.blend_amount',0,1)
        _color(brush['color'],'intersection.color')
        objects=[plan['bindings']['object'][x] for x in source['object_ids']]
        materials=[plan['bindings']['material'][x] for x in source['material_names']]
        bound=controls[gid]
        authored_order=('line_hierarchy' in plan['raw']['request'] or
            plan['raw']['request'].get('selector_bindings',{}).get('order') is not None)
        groups.append(dict(id=gid,priority=bound['priority'] if authored_order else source['priority'],brush=brush,
            object_ids=objects,material_names=materials,control=controls[gid],
            visible_enabled=_boolean(source['visible_enabled'],'intersection.visible'),
            hidden_enabled=_boolean(source['hidden_enabled'],'intersection.hidden'),
            actual_brush=aliases.get(gid,'intersection/'+gid),selection=bound.get('selection')))
    if wire_filtered and not groups:
        plan['viewport_intersection_plan']=None
        return None
    result=dict(groups=sorted(groups,key=lambda g:g['priority'],reverse=True),coverage=dict(**coverage_options(settings),
        **sampling_options(settings,sigma_key='postfilter_sigma')),
        self_intersection=settings['self_intersection'],policy=policy)
    from .line_functions import records,self_channels
    ext=records(plan['raw'])
    if ext:
        from .selector_bindings import key
        result['disabled_materials']=[key(ref) for row in ext['containers'] if row['disable_intersection'] for ref in row['materials']]
    if ext and ext.get('self_intersections'):
        result['channel_ownership']=True
        for group in result['groups']:group['self_channels']=self_channels(plan['raw'],group['id'],settings['self_intersection'])
    plan['viewport_intersection_plan']=result
    return result

def _weights(points,triangle):
    e=triangle[:,1]-triangle[:,0];f=triangle[:,2]-triangle[:,0]
    r=points-triangle[:,None,0]
    ee=np.einsum('ni,ni->n',e,e);ff=np.einsum('ni,ni->n',f,f);ef=np.einsum('ni,ni->n',e,f)
    er=np.einsum('nei,ni->ne',r,e);fr=np.einsum('nei,ni->ne',r,f)
    den=ee*ff-ef*ef
    u=(ff[:,None]*er-ef[:,None]*fr)/den[:,None]
    v=(ee[:,None]*fr-ef[:,None]*er)/den[:,None]
    weights=np.stack((1-u-v,u,v),axis=2)
    if not np.isfinite(weights).all():raise ValueError('Nonfinite intersection barycentrics')
    violation=float(max(0,-weights.min(),weights.max()-1)) if len(weights) else 0.
    # Construction is finite-f64. Project roundoff back into the support's closed attribute domain.
    weights=np.clip(weights,0,1);weights/=weights.sum(axis=2)[:,:,None]
    return weights,violation

def intersection_parameters(edges,lengths,*,with_arc=False,arc_lengths=None):
    """Coincident topology-edge instances share phase; every source/support is retained."""
    from .strokes import chain_parameters
    canonical=np.sort(edges,axis=1)
    unique,first,inverse=np.unique(canonical,axis=0,return_index=True,return_inverse=True)
    if with_arc:cp,chains,arc=chain_parameters(unique,lengths[first],with_arc=True,
        arc_lengths=None if arc_lengths is None else arc_lengths[first])
    else:cp,chains=chain_parameters(unique,lengths[first])
    values=cp[inverse].copy()
    reverse=edges[:,0]!=canonical[:,0]
    values[reverse]=values[reverse,::-1]
    if with_arc:
        arcs=arc[inverse].copy();arcs[reverse]=arcs[reverse,::-1]
        return values,chains,len(edges)-len(unique),arcs
    return values,chains,len(edges)-len(unique)


def add_layers(*args,**kwargs):
    steps=add_layers_steps(*args,**kwargs)
    while True:
        try:next(steps)
        except StopIteration as done:return done.value


def add_layers_steps(geometry,plan,view,projection,perspective,width,height,scale,settings,buckets,*,allow_capture=True,include_provenance=False,chain_phase=None,map_amounts=None,dash_patterns=None,dash_budget=None,_ownership_channel=None):
    from .strokes import _styled_segments_steps,chain_parameters,_layer_provenance,_dash_for,_dash_metric_lengths,_admit_dash_layer
    if dash_budget is None:dash_budget=dict(sources=0,intervals=0,styled=0)
    compiled=_plan(plan)
    if compiled is None:return dict(enabled=False,reason='no intersection request')
    if compiled.get('channel_ownership') and _ownership_channel is None:
        reports=[]
        for channel in ('VISIBLE','HIDDEN'):
            reports.append((yield from add_layers_steps(geometry,plan,view,projection,perspective,width,height,scale,settings,buckets,
                allow_capture=allow_capture,include_provenance=include_provenance,chain_phase=chain_phase,map_amounts=map_amounts,
                dash_patterns=dash_patterns,dash_budget=dash_budget,_ownership_channel=channel)))
        result=dict(reports[0]);result['rows']=[r for report in reports for r in report.get('rows',[])];result['channel_ownership']=True
        return result
    if _ownership_channel is not None:
        compiled=dict(compiled,groups=[dict(g) for g in compiled['groups']])
        for group in compiled['groups']:
            for ch,gate in (('VISIBLE','visible_enabled'),('HIDDEN','hidden_enabled')):
                if ch!=_ownership_channel:group[gate]=False
            if 'channels' in group:group['channels']={ch:dict(row,enabled=row['enabled'] and ch==_ownership_channel) for ch,row in group['channels'].items()}
    if not compiled['groups']:return dict(enabled=False,reason='wire groups replace intersection sources')
    amount_aliases={row['group']:row['brush_id'] for row in plan['raw']['request'].get('intersection_aliases',[])}
    if not getattr(settings,'intersection_enabled',True):return dict(enabled=False,reason='viewport switch')
    if chain_phase is not None and chain_phase.get('intersection_status',{}).get('state')=='pending':
        return dict(enabled=True,pending_geometry=True,
            reason=chain_phase['intersection_status']['reason'],phase_computed=False)
    reused=hasattr(geometry,'intersection_sources')
    if not reused and not allow_capture:
        return dict(enabled=True,pending_geometry=True,reason='following boundary preview; intersections refine after settling')
    if not reused:geometry.intersection_sources=capture(geometry.meshes)
    data=geometry.intersection_sources
    result=dict(data['stats'],enabled=True,cache_reused=reused,rows=[],
        support_policy=compiled['policy'],native_equivalent=False,attribute_weight_violation=0.)
    n=len(data['world'])
    if not n:return result
    inv=np.linalg.inv(np.asarray(view,np.float64))
    tris=data['triangles'][data['pair']]
    normals=np.cross(tris[:,:,1]-tris[:,:,0],tris[:,:,2]-tris[:,:,0])
    norms=np.linalg.norm(normals,axis=2)
    normals=np.divide(normals,norms[:,:,None],out=np.zeros_like(normals),where=norms[:,:,None]>0)
    toward=inv[:3,3]-tris.mean(axis=2) if perspective else np.broadcast_to(inv[:3,2],normals.shape)
    if perspective:toward/=np.linalg.norm(toward,axis=2)[:,:,None]
    facing=np.einsum('nsi,nsi->ns',normals,toward)
    owner=np.full(n,-1,np.int32);side=np.zeros(n,np.int8)
    from .line_functions import intersection_eligible
    refs=data['references'];eligible=intersection_eligible(data,geometry.meshes,compiled.get('disabled_materials',[]))
    if _ownership_channel is None and not compiled['self_intersection']:eligible&=refs[:,0,0]!=refs[:,1,0]
    controls={g.name:g for g in settings.groups}
    for gi,g in enumerate(compiled['groups']):
        matches=np.zeros((n,2),bool)
        for mi,m in enumerate(geometry.meshes):
            for s in (0,1):
                ids=np.flatnonzero(refs[:,s,0]==mi)
                local=refs[ids,s,1]
                from .selector_bindings import face_membership
                whole,faces=face_membership(m.name,m.face_material_names,g,getattr(m,'selector_meta',None))
                match=faces[m.triangle_polygons[local]]
                if whole:match[:]=True
                matches[ids,s]=match
        group_eligible=eligible
        if _ownership_channel is not None and not g['self_channels'][_ownership_channel]:group_eligible=eligible&(refs[:,0,0]!=refs[:,1,0])
        keep=(owner<0)&group_eligible&matches.any(axis=1)
        if compiled['policy']=='priority_front_side.v1':
            prefer1=~matches[:,0]|(matches[:,1]&(facing[:,1]>0)&~(facing[:,0]>0))
        else:
            prefer1=~matches[:,0]|(matches[:,1]&(abs(facing[:,1])<abs(facing[:,0])))
        owner[keep]=gi;side[keep&prefer1]=1
    for gi,g in enumerate(compiled['groups']):
        control=controls.get(g['id'])
        if control is not None and not control.enabled:continue
        ids=np.flatnonzero(owner==gi)
        if not len(ids):continue
        segments=data['world'][ids]
        selected=refs[ids,side[ids]]
        weights,violation=_weights(segments,tris[ids,side[ids]])
        result['attribute_weight_violation']=max(result['attribute_weight_violation'],violation)
        if 'channels' in g:
            rows=yield from _channel_layers_steps(geometry,plan,g,ids,segments,selected,weights,data,
                projection,width,height,scale,settings,control,buckets,compiled['coverage'],
                include_provenance,chain_phase,map_amounts,dash_patterns,dash_budget)
            result['rows'].extend(rows)
            continue
        wm=np.ones((len(ids),2));am=wm.copy()
        brush=g['brush']
        amount=map_amount.factor(map_amounts,amount_aliases.get(g['id']))
        sample_brush=map_amount.consumer_brush(brush,amount)
        from . import brush_effects
        effect=brush_effects.records(plan['raw']).get(g['actual_brush'])
        line_padding=plan.get('line_options',{}).get('off_screen_distance') or 0.
        sample_brush=dict(sample_brush,_projection_padding=max(line_padding,
            effect['raster']['off_screen_distance'] if effect else 0.)*scale)
        dash_pattern=_dash_for(dash_patterns,amount_aliases.get(g['id']),scale)
        for mi,m in enumerate(geometry.meshes):
            pick=np.flatnonzero(selected[:,0]==mi);local=selected[pick,1]
            if not len(pick):continue
            for name,target in (('width_map',wm),('alpha_map',am)):
                if name=='width_map' and amount==0:continue
                factors=map_triangle_points(brush[name],m.layers,m.triangles[local],
                    m.triangle_loops[local],weights[pick])
                target[pick]=map_amount.mix(factors,amount) if name=='width_map' else factors
        clip=np.concatenate((segments,np.ones((*segments.shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
        valid=clip[:,:,3]!=0
        xy=np.divide(clip[:,:,:2],clip[:,:,3,None],out=np.zeros_like(clip[:,:,:2]),where=valid[:,:,None])
        lengths=np.linalg.norm((xy[:,1]-xy[:,0])*np.array([width,height])*.5,axis=1)
        dash_arc=None
        if dash_pattern is None:cp,chains,duplicate_instances=intersection_parameters(data['edges'][ids],lengths)
        else:cp,chains,duplicate_instances,dash_arc=intersection_parameters(data['edges'][ids],lengths,with_arc=True,
            arc_lengths=_dash_metric_lengths(clip,width,height))
        bound=g['control'];units=plan['units']
        reference=plan['raw']['inputs']
        current_units=size_scale(bound['size_mode'],round(width/scale),round(height/scale),units,bound['initial_scale_ex'])
        reference_units=size_scale(bound['size_mode'],reference['width'],reference['height'],units,bound['initial_scale_ex'])
        if reference_units<=0:raise ValueError('Positive intersection reference unit scale required')
        px=brush['width_px']*current_units/reference_units*scale*settings.width_multiplier
        alpha=settings.opacity*brush['blend_amount']
        if control is not None:px*=control.width_scale;alpha*=control.opacity
        provenance=None
        if include_provenance:
            provenance=dict(source_indices=ids.astype(np.int64),
                source_mesh_indices=np.full(len(ids),-1,np.int32),
                source_kinds=np.full(len(ids),5,np.int8),category_bits=np.full(len(ids),4,np.uint16))
        if chain_phase is None or g['actual_brush'] not in chain_phase.get('intersection_enabled_actuals',()):
            fragments=yield from _styled_segments_steps(segments,cp,wm,am,sample_brush,px,alpha,data['supports'][ids],projection,width,height,provenance=provenance,
                dash_pattern=dash_pattern,dash_arc=dash_arc,dash_budget=dash_budget)
            for channel,enabled in (('VISIBLE',g['visible_enabled']),('HIDDEN',g['hidden_enabled'])):
                if not enabled or settings.visibility not in (channel,'BOTH'):continue
                _admit_dash_layer(fragments,dash_pattern,dash_budget)
                key=(g['priority'],g['id'],g['actual_brush'],channel)
                layer=dict(**fragments,**compiled['coverage'],color=brush['color'],visibility=channel)
                if include_provenance:layer.update(_layer_provenance(g['id'],g['actual_brush'],g['priority'],control))
                buckets.setdefault(key,[]).append(layer)
                result['rows'].append(dict(group=g['id'],actual_brush=g['actual_brush'],channel=channel,
                    source_segments=len(ids),primitives=len(fragments['projected']),chains=chains,duplicate_topology_instances=duplicate_instances,width_px=float(px)))
        else:
            lookup=chain_phase['intersection']
            for channel,enabled in (('VISIBLE',g['visible_enabled']),('HIDDEN',g['hidden_enabled'])):
                if not enabled or settings.visibility not in (channel,'BOTH'):continue
                keys=[(g['id'],g['actual_brush'],channel,int(index)) for index in ids]
                if any(key not in lookup for key in keys):
                    raise ValueError('Current source phase result is incomplete for the captured intersection')
                entries=[lookup[key] for key in keys]
                parameters=np.asarray([entry[0] for entry in entries],np.float64)
                active=np.asarray([entry[1] for entry in entries],bool)
                if parameters.shape!=(len(ids),2) or not np.isfinite(parameters).all() or np.any((parameters<0)|(parameters>1)):
                    raise ValueError('Invalid full-source intersection phase')
                source_tags=None if provenance is None else {name:values[active] for name,values in provenance.items()}
                arcs=None
                if dash_pattern is not None:
                    arc_lookup=chain_phase.get('intersection_arc',{})
                    if any(key not in arc_lookup for key in keys):
                        raise ValueError('Full-source dash metric missing for captured intersection')
                    arcs=np.asarray([arc_lookup[key] for key in keys],np.float64)[active]
                fragments=yield from _styled_segments_steps(segments[active],parameters[active],wm[active],am[active],
                    sample_brush,px,alpha,data['supports'][ids][active],projection,width,height,provenance=source_tags,
                    dash_pattern=dash_pattern,dash_arc=arcs,dash_budget=dash_budget)
                _admit_dash_layer(fragments,dash_pattern,dash_budget)
                key=(g['priority'],g['id'],g['actual_brush'],channel)
                layer=dict(**fragments,**compiled['coverage'],color=brush['color'],visibility=channel)
                if include_provenance:layer.update(_layer_provenance(g['id'],g['actual_brush'],g['priority'],control))
                buckets.setdefault(key,[]).append(layer)
                result['rows'].append(dict(group=g['id'],actual_brush=g['actual_brush'],channel=channel,
                    source_segments=len(ids),styled_source_segments=int(np.count_nonzero(active)),
                    primitives=len(fragments['projected']),chains=chains,
                    duplicate_topology_instances=duplicate_instances,width_px=float(px),
                    phase_policy='caller-declared intersection topology; split/orientation/min; link0'))
    return result


def _channel_layers_steps(geometry,plan,group,ids,segments,selected,weights,data,projection,width,height,
                         scale,settings,control,buckets,coverage,include_provenance,chain_phase,
                         map_amounts,dash_patterns,dash_budget):
    """New explicit channels share the original geometry and style kernels."""
    from . import ix_sources
    from .strokes import _styled_segments_steps,_layer_provenance,_dash_for,_dash_metric_lengths,_admit_dash_layer
    clip=np.concatenate((segments,np.ones((*segments.shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
    valid=clip[:,:,3]!=0
    xy=np.divide(clip[:,:,:2],clip[:,:,3,None],out=np.zeros_like(clip[:,:,:2]),where=valid[:,:,None])
    lengths=np.linalg.norm((xy[:,1]-xy[:,0])*np.array([width,height])*.5,axis=1)
    bound=group['control'];reference=plan['raw']['inputs']
    current_units=size_scale(bound['size_mode'],round(width/scale),round(height/scale),plan['units'],bound['initial_scale_ex'])
    reference_units=size_scale(bound['size_mode'],reference['width'],reference['height'],plan['units'],bound['initial_scale_ex'])
    if reference_units<=0:raise ValueError('Positive intersection reference size required')
    rows=[]
    for channel,enabled,actual,brush in ix_sources.channel_rows(group):
        if not enabled or settings.visibility not in (channel,'BOTH'):continue
        amount=map_amount.factor(map_amounts,actual)
        sample_brush=map_amount.consumer_brush(brush,amount)
        from . import brush_effects
        effect=brush_effects.records(plan['raw']).get(actual)
        line_padding=plan.get('line_options',{}).get('off_screen_distance') or 0.
        sample_brush=dict(sample_brush,_projection_padding=max(line_padding,
            effect['raster']['off_screen_distance'] if effect else 0.)*scale)
        pattern=_dash_for(dash_patterns,actual,scale)
        wm=np.ones((len(ids),2));am=wm.copy()
        for mi,mesh in enumerate(geometry.meshes):
            pick=np.flatnonzero(selected[:,0]==mi);local=selected[pick,1]
            if not len(pick):continue
            for name,target in (('width_map',wm),('alpha_map',am)):
                if name=='width_map' and amount==0:continue
                values=map_triangle_points(brush[name],mesh.layers,mesh.triangles[local],mesh.triangle_loops[local],weights[pick])
                target[pick]=map_amount.mix(values,amount) if name=='width_map' else values
        arc=None
        if pattern is None:cp,chains,duplicates=intersection_parameters(data['edges'][ids],lengths)
        else:cp,chains,duplicates,arc=intersection_parameters(data['edges'][ids],lengths,with_arc=True,
            arc_lengths=_dash_metric_lengths(clip,width,height))
        px=brush['width_px']*current_units/reference_units*scale*settings.width_multiplier
        alpha=settings.opacity*brush['blend_amount']
        if control is not None:px*=control.width_scale;alpha*=control.opacity
        provenance=None
        if include_provenance:provenance=dict(source_indices=ids.astype(np.int64),source_mesh_indices=np.full(len(ids),-1,np.int32),
            source_kinds=np.full(len(ids),5,np.int8),category_bits=np.full(len(ids),4,np.uint16))
        policy_on=chain_phase is not None and actual in chain_phase.get('intersection_enabled_actuals',())
        active=np.ones(len(ids),bool)
        if policy_on:
            keys=[(group['id'],actual,channel,int(index)) for index in ids]
            lookup=chain_phase['intersection']
            if any(key not in lookup for key in keys):raise ValueError('Incomplete independent intersection channel phase')
            entries=[lookup[key] for key in keys]
            cp=np.asarray([entry[0] for entry in entries],np.float64);active=np.asarray([entry[1] for entry in entries],bool)
            if cp.shape!=(len(ids),2) or not np.isfinite(cp).all() or np.any((cp<0)|(cp>1)):
                raise ValueError('Invalid independent intersection source phase')
            if pattern is not None:
                lookup=chain_phase.get('intersection_arc',{})
                if any(key not in lookup for key in keys):raise ValueError('Incomplete independent intersection dash arc')
                arc=np.asarray([lookup[key] for key in keys],np.float64)
        tags=None if provenance is None else {name:values[active] for name,values in provenance.items()}
        fragments=yield from _styled_segments_steps(segments[active],cp[active],wm[active],am[active],sample_brush,px,alpha,
            data['supports'][ids][active],projection,width,height,provenance=tags,dash_pattern=pattern,
            dash_arc=None if arc is None else arc[active],dash_budget=dash_budget)
        _admit_dash_layer(fragments,pattern,dash_budget)
        layer=dict(**fragments,**coverage,color=brush['color'],visibility=channel)
        if include_provenance:layer.update(_layer_provenance(group['id'],actual,group['priority'],control))
        buckets.setdefault((group['priority'],group['id'],actual,channel),[]).append(layer)
        row=dict(group=group['id'],slot=group['channels'][channel]['slot'],actual_brush=actual,channel=channel,
            source_segments=len(ids),styled_source_segments=int(np.count_nonzero(active)),primitives=len(fragments['projected']),
            chains=chains,duplicate_topology_instances=duplicates,width_px=float(px),source_profile=ix_sources.PROFILE)
        if policy_on:row['phase_policy']='caller-declared intersection topology; independent actual/channel split/orientation/min/gap'
        rows.append(row)
    return rows
