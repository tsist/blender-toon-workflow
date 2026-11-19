# SPDX-License-Identifier: GPL-3.0-or-later
"""Editor boundary styles on real source edges, before clip/visibility cuts.

Chains are deterministic degree-two mesh paths. Native Pencil connection,
cross-object weld, intersection styling and exact AA are not claimed here.
"""
import numpy as np
from .geometry import project_segments
from .style import map_endpoints,curve_values,size_scale
from .coverage import options as coverage_options
from .sampling import options as sampling_options
from . import source_alpha
from . import map_amount_controls as map_amount
from . import dash_splitter
from . import source_routes


_PROVENANCE_DTYPES = {
    'source_indices': np.dtype(np.int64),
    'source_mesh_indices': np.dtype(np.int32),
    'source_kinds': np.dtype(np.int8),
    'category_bits': np.dtype(np.uint16),
}
_PROVENANCE_METADATA = ('group_id', 'actual_brush', 'priority', 'line_set_id',
                        'elements_only', 'classification')


def _validate_provenance(provenance, count):
    """Validate once at the optional source-array input boundary."""
    if provenance is None:return
    if not isinstance(provenance,dict) or set(provenance)!=set(_PROVENANCE_DTYPES):
        raise ValueError('Provenance must contain the four source arrays')
    for name,dtype in _PROVENANCE_DTYPES.items():
        value=provenance[name]
        if not isinstance(value,np.ndarray) or value.dtype!=dtype or value.shape!=(count,):
            raise ValueError('Provenance '+name+' must be a '+str(dtype)+' source-length array')


def _layer_provenance(group_id,actual_brush,priority,control,*,classification='autonomous_resolved_source_kind.v1'):
    return dict(group_id=group_id,actual_brush=actual_brush,priority=priority,
                line_set_id=getattr(control,'line_set_id',1),
                elements_only=getattr(control,'elements_only',False),
                classification=classification)


def _merge_provenance(rows,result,*,metadata=False):
    if not any(any(name in row for name in _PROVENANCE_DTYPES) for row in rows):return
    if any(not all(name in row for name in _PROVENANCE_DTYPES) for row in rows):
        raise ValueError('Cannot merge mixed or incomplete source provenance')
    if metadata:
        if any(any(name not in row or row[name]!=result.get(name)
                   for name in _PROVENANCE_METADATA) for row in rows):
            raise ValueError('Same actual brush layer has conflicting provenance metadata')
    for name in _PROVENANCE_DTYPES:
        result[name]=np.concatenate([row[name] for row in rows])


def chain_parameters(pairs,lengths,*,with_arc=False,arc_lengths=None):
    """Full uncut path arclength; branch vertices end a path, loops use IDs."""
    if with_arc and len(pairs)>dash_splitter.Budget().max_sources:
        raise dash_splitter.DashBudgetError('legacy dash traversal source budget')
    links={}
    for edge,(a,b) in enumerate(pairs):
        links.setdefault(int(a),[]).append(edge);links.setdefault(int(b),[]).append(edge)
    remaining=set(range(len(pairs)));result=np.zeros((len(pairs),2),np.float64)
    chains=[];arc=np.zeros((len(pairs),2),np.float64) if with_arc else None
    def follow(vertex,edge):
        rows=[]
        while edge in remaining:
            remaining.remove(edge)
            a,b=map(int,pairs[edge]);forward=a==vertex
            rows.append((edge,forward));vertex=b if forward else a
            if len(links[vertex])!=2:break
            candidates=[i for i in links[vertex] if i in remaining]
            if not candidates:break
            edge=min(candidates)
        chains.append(rows)
    for vertex in sorted(links):
        if len(links[vertex])==2:continue
        for edge in links[vertex]:
            if edge in remaining:follow(vertex,edge)
    while remaining:
        edge=min(remaining);follow(int(min(pairs[edge])),edge)
    for rows in chains:
        total=sum(lengths[e] for e,_ in rows);position=0.0;arc_position=0.0
        for edge,forward in rows:
            span=(position/total,(position+lengths[edge])/total) if total>0 else (0.0,0.0)
            result[edge]=span if forward else span[::-1]
            if with_arc:
                length=lengths[edge] if arc_lengths is None else arc_lengths[edge]
                end=arc_position+length
                if not np.isfinite(end) or length>0 and end==arc_position:
                    raise dash_splitter.DashPrecisionError('legacy chain arc cannot advance finite prefix')
                arc[edge]=(arc_position,end) if forward else (end,arc_position)
                arc_position=end
            position+=lengths[edge]
    if with_arc:return result,len(chains),arc
    return result,len(chains)


def _dash_for(patterns,actual,scale):
    """Only caller-resolved explicit aliases receive a pattern."""
    row=None if patterns is None or actual is None else patterns.get(actual)
    if row is None or row['mode']=='SOLID' or row['gap_length_px']==0:return None
    return dict(mode=row['mode'],dash_length_px=row['dash_length_px']*scale,
                gap_length_px=row['gap_length_px']*scale)


def _empty_fragments(provenance=None):
    result=dict(world=np.empty((0,2,3)),projected=np.empty((0,2,3),np.float32),
        supports=np.empty((0,2),np.float32),widths=np.empty((0,2),np.float32),
        alphas=np.empty((0,2),np.float32),alpha_references=[],alpha_source_ids=np.empty(0,np.int64))
    if provenance is not None:
        result.update({name:np.empty(0,dtype) for name,dtype in _PROVENANCE_DTYPES.items()})
    return result


def _dash_metric_lengths(clip,width,height):
    """Validate complete sources before they can contribute any chain prefix."""
    if not np.isfinite(clip).all() or not np.all(clip[:,:,3]>0):
        raise ValueError('Dashed chains require finite complete positive-w sources')
    with np.errstate(over='raise',invalid='raise',divide='raise'):
        xy=clip[:,:,:2]/clip[:,:,3,None]*np.array([width,height])*.5
        delta=xy[:,1]-xy[:,0]
        lengths=np.hypot(delta[:,0],delta[:,1])
    if not np.isfinite(lengths).all():
        raise dash_splitter.DashPrecisionError('nonfinite full-source dash metric')
    return lengths


def _admit_dash_layer(fragments,pattern,budget):
    if pattern is None:return
    count=budget.get('layer_fragments',0)+len(fragments['projected'])
    if count>dash_splitter.Budget().max_fragments_total:
        raise dash_splitter.DashBudgetError('frame final dash layer fragment budget')
    budget['layer_fragments']=count


def _dash_spans(segments,clip,arc,projection,width,height,pattern,budget,padding=0.):
    if arc is None:raise ValueError('Full-source pixel arc required for dashed sources')
    arc=np.asarray(arc)
    if arc.shape!=(len(segments),2) or arc.dtype.kind not in 'iuf' or not np.isfinite(arc).all():
        raise ValueError('Finite full-source dash arc endpoints required')
    if not np.isfinite(clip).all() or not np.all(clip[:,:,3]>0):
        raise ValueError('Dashed chains require finite complete positive-w sources')
    budget['sources']+=len(segments)
    if budget['sources']>dash_splitter.Budget().max_sources:
        raise dash_splitter.DashBudgetError('frame dash source budget')
    # Limit enumeration to the existing clip interval without resetting the
    # full-source chain metric. Fully clipped sources cannot contribute ink.
    unused,kept,source_u=project_segments(segments,projection,width,height,with_parameters=True,padding=padding)
    if not len(kept):return {}
    remaining=dash_splitter.Budget().max_fragments_total-budget['intervals']
    if remaining<=0:raise dash_splitter.DashBudgetError('frame dash interval budget')
    slices=dash_splitter.split_records(dict(source_u=source_u),arc[kept],clip[kept,:,3],pattern,
        budget=dash_splitter.Budget(max_fragments_total=remaining))
    if not isinstance(slices,dash_splitter.DashSlices):raise ValueError('Active dash pattern required')
    budget['intervals']+=len(slices.row_indices)
    result={}
    for row,q in zip(slices.row_indices,slices.screen_q):
        result.setdefault(int(kept[row]),[]).append(q)
    return result


def styled_segments(segments,parameters,width_map,alpha_map,brush,width_px,alpha_scale,supports,
                    projection,width,height,*,provenance=None,dash_pattern=None,dash_arc=None):
    """Shared boundary/intersection curve, clip and screen-parameter consumer."""
    _validate_provenance(provenance,len(segments))
    return _styled_segments(segments,parameters,width_map,alpha_map,brush,width_px,alpha_scale,
                            supports,projection,width,height,provenance=provenance,
                            dash_pattern=dash_pattern,dash_arc=dash_arc)


def _styled_segments(segments,parameters,width_map,alpha_map,brush,width_px,alpha_scale,supports,
                     projection,width,height,*,provenance=None,dash_pattern=None,dash_arc=None,dash_budget=None):
    clip=np.concatenate((segments,np.ones((*segments.shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
    dash_on=dash_splitter._pattern(dash_pattern) is not None
    if dash_on:
        if dash_budget is None:dash_budget=dict(sources=0,intervals=0,styled=0)
        dash_spans=_dash_spans(segments,clip,dash_arc,projection,width,height,dash_pattern,dash_budget,padding=brush.get('_projection_padding',0.))
    world_rows=[];width_rows=[];alpha_rows=[];support_rows=[];alpha_references=[];alpha_ids=[]
    source_rows=[] if provenance is not None else None
    for j in range(len(segments)):
        if dash_on and j not in dash_spans:continue
        c0,c1=parameters[j];cuts=[0.0,1.0]
        if c0!=c1:
            for key in ('size_curve','alpha_curve'):
                samples=brush[key]
                if samples is not None:
                    q=(np.linspace(0,1,len(samples))-c0)/(c1-c0)
                    cuts.extend(q[(q>0)&(q<1)].tolist())
        q=np.unique(cuts)
        if ((brush['size_curve'] is not None and brush['width_map'] is not None) or
            (brush['alpha_curve'] is not None and brush['alpha_map'] is not None)):
            q=np.unique(np.concatenate((q,(q[:-1]+q[1:])*.5)))
        if dash_on:
            pairs=[]
            for lo,hi in dash_spans[j]:
                cuts=np.concatenate(([lo],q[(q>lo)&(q<hi)],[hi]))
                pairs.append(np.column_stack((cuts[:-1],cuts[1:])))
            pairs=np.concatenate(pairs)
            q=pairs.reshape(-1)
        w0,w1=clip[j,:,3]
        u=(q/w1)/((1-q)/w0+q/w1) if w0>0 and w1>0 else q
        phase=c0+q*(c1-c0)
        widths=(width_map[j,0]+u*(width_map[j,1]-width_map[j,0]))*curve_values(brush['size_curve'],phase)*width_px
        alphas=(alpha_map[j,0]+u*(alpha_map[j,1]-alpha_map[j,0]))*curve_values(brush['alpha_curve'],phase)*alpha_scale
        ref=source_alpha.function_reference(clip[j],parameters[j],alpha_map[j],
                    brush['alpha_curve'],alpha_scale,width,height)
        alpha_id=-1
        if ref is not None:
            alpha_id=len(alpha_references);alpha_references.append(ref)
        count=len(pairs) if dash_on else len(q)-1
        alpha_ids.append(np.full(count,alpha_id,np.int64))
        points=segments[j,0]+u[:,None]*(segments[j,1]-segments[j,0])
        if dash_on:
            dash_budget['styled']+=count
            if dash_budget['styled']>dash_splitter.Budget().max_fragments_total:
                raise dash_splitter.DashBudgetError('frame styled dash fragment budget')
            world_rows.append(points.reshape(-1,2,3));width_rows.append(widths.reshape(-1,2));alpha_rows.append(alphas.reshape(-1,2))
        else:
            world_rows.append(np.stack((points[:-1],points[1:]),axis=1))
            width_rows.append(np.stack((widths[:-1],widths[1:]),axis=1))
            alpha_rows.append(np.stack((alphas[:-1],alphas[1:]),axis=1))
        support_rows.append(np.tile(supports[j],(count,1)))
        if source_rows is not None:source_rows.append(np.full(count,j,np.int64))
    if not world_rows:return _empty_fragments(provenance)
    full=np.concatenate(world_rows);ws=np.concatenate(width_rows);aa=np.concatenate(alpha_rows)
    supports=np.concatenate(support_rows).astype(np.float32)
    projected,kept,source_u=project_segments(full,projection,width,height,with_parameters=True,padding=brush.get('_projection_padding',0.))
    clipped=full[kept,0,None,:]+source_u[:,:,None]*(full[kept,1]-full[kept,0])[:,None,:]
    subclip=np.concatenate((full[kept],np.ones((*full[kept].shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
    denom=(1-source_u)*subclip[:,0,3,None]+source_u*subclip[:,1,3,None]
    screen_u=np.divide(source_u*subclip[:,1,3,None],denom,out=source_u.copy(),where=denom!=0)
    widths=ws[kept,0,None]+screen_u*(ws[kept,1]-ws[kept,0])[:,None]
    alphas=aa[kept,0,None]+screen_u*(aa[kept,1]-aa[kept,0])[:,None]
    result=dict(projected=projected,world=clipped,supports=supports[kept],
                alpha_references=alpha_references,alpha_source_ids=np.concatenate(alpha_ids)[kept],
                widths=widths.astype(np.float32),alphas=alphas.astype(np.float32))
    if source_rows is not None:
        sources=np.concatenate(source_rows)[kept]
        result.update({name:values[sources] for name,values in provenance.items()})
    return result

def _merge(rows):
    result=rows[0].copy()
    if any(tuple(row['color'])!=tuple(result['color']) or row['visibility']!=result['visibility'] for row in rows[1:]):
        raise ValueError('Same actual brush layer has conflicting color or visibility')
    coverage=dict(**coverage_options(result),**sampling_options(result))
    if any(dict(**coverage_options(row),**sampling_options(row))!=coverage for row in rows[1:]):
        raise ValueError('Same actual brush layer has conflicting coverage or sampling settings')
    for name in ('projected','world','supports','widths','alphas'):
        result[name]=np.concatenate([r[name] for r in rows])
    if any('preclassified' in row for row in rows):
        result['preclassified']=np.concatenate([row.get('preclassified',np.zeros(len(row['projected']),bool)) for row in rows])
    result.update(source_alpha.merge(rows))
    _merge_provenance(rows,result,metadata=True)
    return result


def _styled_segments_steps(segments,parameters,width_map,alpha_map,brush,width_px,alpha_scale,
                           supports,projection,width,height,*,provenance=None,dash_pattern=None,dash_arc=None,dash_budget=None):
    """Keep complete chain phases; yield only between independent source batches."""
    _validate_provenance(provenance,len(segments))
    rows=[]
    if dash_budget is None:dash_budget=dict(sources=0,intervals=0,styled=0)
    for first in range(0,len(segments),128):
        end=min(first+128,len(segments))
        batch_provenance=None if provenance is None else {name:values[first:end] for name,values in provenance.items()}
        rows.append(_styled_segments(segments[first:end],parameters[first:end],
            width_map[first:end],alpha_map[first:end],brush,width_px,alpha_scale,
            supports[first:end],projection,width,height,provenance=batch_provenance,
            dash_pattern=dash_pattern,dash_arc=None if dash_arc is None else dash_arc[first:end],dash_budget=dash_budget))
        yield dict(stage='style',sources_done=end,sources_total=len(segments))
    if not rows:
        result=dict(world=np.empty((0,2,3)),projected=np.empty((0,2,3),np.float32),
                    supports=np.empty((0,2),np.float32),widths=np.empty((0,2),np.float32),
                    alphas=np.empty((0,2),np.float32),alpha_references=[],
                    alpha_source_ids=np.empty(0,np.int64))
        if provenance is not None:
            result.update({name:np.empty(0,dtype) for name,dtype in _PROVENANCE_DTYPES.items()})
        return result
    result={name:np.concatenate([row[name] for row in rows])
            for name in ('world','projected','supports','widths','alphas')}
    result.update(source_alpha.merge(rows))
    _merge_provenance(rows,result)
    return result


def build_layers(*args,**kwargs):
    steps=build_layers_steps(*args,**kwargs)
    while True:
        try:next(steps)
        except StopIteration as done:return done.value


def _routed_mesh_steps(row,mesh_index,groups,aliases,overrides,plan,view,projection,perspective,width,height,scale,settings,
                       buckets,report,coverage,dash_budget,include_provenance,chain_phase,map_amounts,dash_patterns,object_partition,function_visibility=False):
    """Active autonomous wire groups plus unchanged legacy source semantics.

    This consumer keeps full source metrics and every existing author path;
    the default legacy producer below never enters it.
    """
    facing=row.facing(view,perspective) if len(row.normals) else np.empty(0,bool)
    from . import edge_sources
    from . import object_view
    mesh=source_routes.MeshArrays(row.name,row.categories(view,perspective),row.counts,row.faces0,row.faces1,
        row.face_material_names,facing,row.original_edge_count,row.virtual_edges,
        object_view.predicates(row,mesh_index,view,perspective,object_partition)
            if any(g.get('edge_sources',{}).get('enabled') for g in groups) else None,
        getattr(row,'selector_meta',None))
    actuals={(group['id'],slot):aliases.get((group['id'],slot),group['brush_meta'][slot]['uid'] or slot)
             for group in groups for slot in group['brush_slots']}
    routed=source_routes.select_plan(mesh,groups,actuals,
        {gid:control.enabled for gid,control in overrides.items()},
        source_categories=plan['settings'].get('source_categories',['selected','smoothing']),
        include_open=plan['settings'].get('include_open',True),visibility=settings.visibility)
    yield dict(stage='classify',object=row.name)
    chain_count=0;cross_eye=False
    for selection in routed.selections:
        group=groups[selection.group_index];control=overrides.get(group['id'])
        slot=selection.slot;ids=selection.ids;channel=selection.channel;actual=selection.actual
        brush=group['brushes'][slot];pairs=row.edges[ids]
        traced_actual=aliases.get((group['id'],slot))
        amount=map_amount.factor(map_amounts,traced_actual)
        sample_brush=map_amount.consumer_brush(brush,amount)
        dash_pattern=_dash_for(dash_patterns,traced_actual,scale)
        corners=np.where(selection.sides[:,None]==0,row.corners0[ids],row.corners1[ids])
        segments=row.vertices[pairs].astype(np.float64)
        clip=np.concatenate((segments,np.ones((*segments.shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
        valid=np.abs(clip[:,:,3])>1e-12
        cross_eye|=not bool(np.all(clip[:,:,3]>0))
        xy=np.divide(clip[:,:,:2],clip[:,:,3,None],out=np.zeros_like(clip[:,:,:2]),where=valid[:,:,None])
        xy*=np.array([width,height])*.5
        lengths=np.linalg.norm(xy[:,1]-xy[:,0],axis=1)
        dash_arc=None
        if dash_pattern is None:cp,chains=chain_parameters(pairs,lengths)
        else:cp,chains,dash_arc=chain_parameters(pairs,lengths,with_arc=True,
            arc_lengths=_dash_metric_lengths(clip,width,height))
        chain_count+=chains
        wm=(np.ones((len(ids),2),np.float64) if amount==0 else
            map_amount.mix(map_endpoints(brush['width_map'],row.layers,pairs,corners),amount))
        am=map_endpoints(brush['alpha_map'],row.layers,pairs,corners)
        units=size_scale(group['size_mode'],round(width/scale),round(height/scale),
            plan['units'],group['initial_scale_ex'])*scale
        native=group['brush_meta'][slot]['native_size']
        px=native*units*group['coverage_extent_factor']*settings.width_multiplier
        alpha_scale=settings.opacity*brush['blend_amount']
        if control is not None:px*=control.width_scale;alpha_scale*=control.opacity
        tags=np.where(row.triangle_supports[ids]>=0,row.triangle_supports[ids]+row.face_offset,0)
        provenance=None
        partition_on=edge_sources.object_active(group)
        if include_provenance or partition_on:
            provenance=dict(source_indices=ids.astype(np.int64),source_mesh_indices=np.full(len(ids),mesh_index,np.int32),
                source_kinds=selection.source_kinds,category_bits=selection.category_bits)
        policy_enabled=chain_phase is not None and actual in chain_phase.get('enabled_actuals',())
        if not policy_enabled:
            fragments=yield from _styled_segments_steps(segments,cp,wm,am,sample_brush,px,alpha_scale,tags,projection,width,height,
                provenance=provenance,dash_pattern=dash_pattern,dash_arc=dash_arc,dash_budget=dash_budget)
        else:
            keys=[(mesh_index,group['id'],slot,channel,int(edge)) for edge in ids]
            lookup=chain_phase['boundary']
            if any(key not in lookup for key in keys):
                raise ValueError('Current source phase result is incomplete for the captured boundary')
            entries=[lookup[key] for key in keys]
            parameters=np.asarray([entry[0] for entry in entries],np.float64)
            active=np.asarray([entry[1] for entry in entries],bool)
            if parameters.shape!=(len(ids),2) or not np.isfinite(parameters).all() or np.any((parameters<0)|(parameters>1)):
                raise ValueError('Invalid full-source boundary phase')
            source_tags=None if provenance is None else {name:values[active] for name,values in provenance.items()}
            arcs=None
            if dash_pattern is not None:
                arc_lookup=chain_phase.get('boundary_arc',{})
                if any(key not in arc_lookup for key in keys):
                    raise ValueError('Full-source dash metric missing for captured boundary')
                arcs=np.asarray([arc_lookup[key] for key in keys],np.float64)[active]
            fragments=yield from _styled_segments_steps(segments[active],parameters[active],wm[active],am[active],sample_brush,
                px,alpha_scale,tags[active],projection,width,height,provenance=source_tags,dash_pattern=dash_pattern,
                dash_arc=arcs,dash_budget=dash_budget)
        if partition_on:
            from .object_fragments import cut
            fragments=cut(fragments,row,mesh_index,channel,object_view.partition_for(plan,group,channel,object_partition),projection,width,height,
                line_functions=function_visibility)
            if not include_provenance:
                for name in _PROVENANCE_DTYPES:fragments.pop(name,None)
        _admit_dash_layer(fragments,dash_pattern,dash_budget)
        layer=dict(**fragments,**coverage,color=brush['color'],visibility=channel)
        if include_provenance:
            layer.update(_layer_provenance(group['id'],actual,group['priority'],control))
        buckets.setdefault((group['priority'],group['id'],actual,channel),[]).append(layer)
        details=dict(object=row.name,group=group['id'],slot=slot,actual_brush=actual,channel=channel,
            source_edges=len(ids),virtual_source_edges=int(np.count_nonzero(row.virtual_edges[ids])),
            primitives=len(fragments['projected']),chains=chains,width_px=float(px),
            source_profile=edge_sources.PROFILE if group.get('edge_sources',{}).get('enabled') else source_routes.PROFILE)
        if policy_enabled:
            details.update(styled_source_edges=int(np.count_nonzero(active)),phase_policy='autonomous full-source split/orientation/min')
        report.append(details)
    return chain_count,cross_eye


def build_layers_steps(geometry,plan,view,projection,perspective,width,height,scale,settings,*,allow_intersection_capture=True,include_provenance=False,chain_phase=None,map_amounts=None,dash_patterns=None):
    from . import line_hierarchy
    batches=line_hierarchy.batches(plan)
    if batches is not None:
        layers=[]; reports=[]
        for line,ids in batches:
            sub=line_hierarchy.slice_plan(plan,ids,scale=scale)
            parts,report=yield from build_layers_steps(geometry,sub,view,projection,perspective,width,height,scale,settings,
                allow_intersection_capture=allow_intersection_capture,include_provenance=include_provenance,
                chain_phase=chain_phase,map_amounts=map_amounts,dash_patterns=dash_patterns)
            for layer in parts:
                layer['line_id']=line['id']
                layer['elements_only']=bool(layer.get('elements_only',False) or line.get('elements_only',False))
            layers.extend(parts);reports.append(dict(line=line['id'],name=line['name'],priority=line['priority'],report=report))
        return layers,dict(lines=reports,layers=len(layers),native_equivalent=False,
            authority='independent resolved Lines; connected LineSets compete only within each Line')
    from . import brush_effects,brush_engine
    effects=brush_effects.records(plan['raw'])
    if effects:
        include_provenance=True
        # Compile-only copies carry the clip margin; source authors stay exact.
        import copy
        plan=copy.deepcopy(plan)
        aliases={(a['group'],a['slot']):a['brush_id'] for a in plan['raw']['request'].get('boundary_aliases',[])}
        for group in plan['groups']:
            for slot,brush in group['brushes'].items():
                effect=effects.get(aliases.get((group['id'],slot)))
                if effect:brush['_projection_padding']=max(brush.get('_projection_padding',0.),effect['raster']['off_screen_distance']*scale)
    from .holdout import active as holdout_active
    from .line_functions import visibility_effective
    function_visibility=visibility_effective(geometry,plan,{g.name for g in settings.groups if g.enabled}) or holdout_active(geometry)
    # Private per-call plan: painting consumes the same runtime source decision
    # without mutating the cached author plan or consulting Blender in workers.
    plan=dict(plan,_line_function_visibility=function_visibility)
    if function_visibility or 'line_functions' in plan['raw']['request']:include_provenance=True
    buckets={};report=[];chain_count=0;cross_eye=False
    dash_budget=dict(sources=0,intervals=0,styled=0)
    coverage=dict(**coverage_options(plan['settings']),**sampling_options(plan['settings']))
    overrides={item.name:item for item in settings.groups}
    groups=sorted(plan['groups'],key=lambda g:g['priority'],reverse=True)
    from . import object_view
    object_partition=yield from object_view.prepare_steps(geometry,plan,view,projection,width,height,perspective,pixel_scale=scale)
    categories=set(plan['settings'].get('source_categories',['selected','smoothing']))
    aliases={(x['group'],x['slot']):x['brush_id'] for x in plan['raw']['request'].get('boundary_aliases',[])}
    wire_active=source_routes.active(groups)
    for mesh_index,row in enumerate(geometry.meshes):
        if getattr(row,'holdout',False):continue
        if wire_active:
            mesh_chains,mesh_cross_eye=yield from _routed_mesh_steps(row,mesh_index,groups,aliases,overrides,plan,view,projection,
                perspective,width,height,scale,settings,buckets,report,coverage,dash_budget,include_provenance,chain_phase,
                map_amounts,dash_patterns,object_partition,function_visibility)
            chain_count+=mesh_chains;cross_eye|=mesh_cross_eye
            continue
        kinds=row.categories(view,perspective)
        yield dict(stage='classify',object=row.name)
        if 'smoothing' not in categories:kinds[kinds==3]=0
        if 'selected' not in categories:kinds[np.isin(kinds,[1,2,4])]=0
        if not plan['settings'].get('include_open',True):kinds[kinds==2]=0
        owner=np.full(len(row.edges),-1,np.int32)
        sides=np.zeros(len(row.edges),np.int8)
        facing=row.facing(view,perspective) if len(row.normals) else np.empty(0,bool)
        for gi,group in enumerate(groups):
            from .selector_bindings import face_membership
            object_match,faces=face_membership(row.name,row.face_material_names,group,getattr(row,'selector_meta',None))
            if len(row.face_material_names):
                m0=faces[row.faces0]&(row.counts>0)
                m1=faces[row.faces1]&(row.counts>0)
            else:m0=m1=np.zeros(len(row.edges),bool)
            if object_match:m0=m1=np.ones(len(row.edges),bool)
            match=(owner<0)&(m0|m1)&(kinds>0)
            owner[match]=gi
            # Existing autonomous policy: eligible front side then lowest face.
            prefer1=(~m0)&m1
            if len(facing):
                prefer1|=m1&facing[row.faces1]&(~facing[row.faces0])
            sides[match&prefer1]=1
        for gi,group in enumerate(groups):
            control=overrides.get(group['id'])
            if control is not None and not control.enabled:continue
            mask=owner==gi
            # A disabled source stays disabled; never fall back to base brush.
            if group['silhouette_enabled'] is False:mask&=kinds!=1
            if group['marked_enabled'] is False:mask&=kinds!=4
            if group['smoothing_enabled'] is False:mask&=kinds!=3
            slots=np.full(len(kinds),'brush',dtype=object)
            for kind,slot in ((1,'silhouette_brush'),(3,'smoothing_brush'),(4,'marked_brush')):
                if slot in group['brushes']:slots[kinds==kind]=slot
            for slot in group['brush_slots']:
                ids=np.flatnonzero(mask&(slots==slot))
                if not len(ids):continue
                brush=group['brushes'][slot];pairs=row.edges[ids]
                traced_actual=aliases.get((group['id'],slot))
                actual=traced_actual if traced_actual is not None else group['brush_meta'][slot]['uid'] or slot
                amount=map_amount.factor(map_amounts,traced_actual)
                sample_brush=map_amount.consumer_brush(brush,amount)
                dash_pattern=_dash_for(dash_patterns,traced_actual,scale)
                corners=np.where(sides[ids,None]==0,row.corners0[ids],row.corners1[ids])
                segments=row.vertices[pairs].astype(np.float64)
                clip=np.concatenate((segments,np.ones((*segments.shape[:2],1))),axis=2)@np.asarray(projection,np.float64).T
                valid=np.abs(clip[:,:,3])>1e-12
                cross_eye|=not bool(np.all(clip[:,:,3]>0))
                xy=np.divide(clip[:,:,:2],clip[:,:,3,None],out=np.zeros_like(clip[:,:,:2]),where=valid[:,:,None])
                xy*=np.array([width,height])*.5
                lengths=np.linalg.norm(xy[:,1]-xy[:,0],axis=1)
                dash_arc=None
                if dash_pattern is None:cp,chains=chain_parameters(pairs,lengths)
                else:cp,chains,dash_arc=chain_parameters(pairs,lengths,with_arc=True,
                    arc_lengths=_dash_metric_lengths(clip,width,height))
                chain_count+=chains
                wm=(np.ones((len(ids),2),np.float64) if amount==0 else
                    map_amount.mix(map_endpoints(brush['width_map'],row.layers,pairs,corners),amount))
                am=map_endpoints(brush['alpha_map'],row.layers,pairs,corners)
                units=size_scale(group['size_mode'],round(width/scale),round(height/scale),
                    plan['units'],group['initial_scale_ex'])*scale
                native=group['brush_meta'][slot]['native_size']
                px=native*units*group['coverage_extent_factor']*settings.width_multiplier
                alpha_scale=settings.opacity*brush['blend_amount']
                if control is not None:px*=control.width_scale;alpha_scale*=control.opacity
                tags=np.where(row.triangle_supports[ids]>=0,row.triangle_supports[ids]+row.face_offset,0)
                provenance=None
                if include_provenance:
                    source_kinds=kinds[ids].astype(np.int8)
                    provenance=dict(source_indices=ids.astype(np.int64),
                        source_mesh_indices=np.full(len(ids),mesh_index,np.int32),source_kinds=source_kinds,
                        category_bits=np.select([np.isin(source_kinds,[1,2]),source_kinds==3,source_kinds==4],
                                                [1,8,32],default=0).astype(np.uint16))
                if chain_phase is None or actual not in chain_phase.get('enabled_actuals',()):
                    fragments=yield from _styled_segments_steps(segments,cp,wm,am,sample_brush,px,alpha_scale,tags,projection,width,height,provenance=provenance,
                        dash_pattern=dash_pattern,dash_arc=dash_arc,dash_budget=dash_budget)
                    for channel,enabled in (('VISIBLE',group['visible_enabled']),('HIDDEN',group['hidden_enabled'])):
                        if not enabled or settings.visibility not in (channel,'BOTH'):continue
                        _admit_dash_layer(fragments,dash_pattern,dash_budget)
                        key=(group['priority'],group['id'],actual,channel)
                        layer=dict(**fragments,**coverage,color=brush['color'],visibility=channel)
                        if include_provenance:layer.update(_layer_provenance(group['id'],actual,group['priority'],control))
                        buckets.setdefault(key,[]).append(layer)
                        report.append(dict(object=row.name,group=group['id'],slot=slot,actual_brush=actual,
                            channel=channel,source_edges=len(ids),virtual_source_edges=int(np.count_nonzero(row.virtual_edges[ids])),
                            primitives=len(fragments['projected']),chains=chains,width_px=float(px)))
                else:
                    lookup=chain_phase['boundary']
                    for channel,enabled in (('VISIBLE',group['visible_enabled']),('HIDDEN',group['hidden_enabled'])):
                        if not enabled or settings.visibility not in (channel,'BOTH'):continue
                        keys=[(mesh_index,group['id'],slot,channel,int(edge)) for edge in ids]
                        if any(key not in lookup for key in keys):
                            raise ValueError('Current source phase result is incomplete for the captured boundary')
                        entries=[lookup[key] for key in keys]
                        parameters=np.asarray([entry[0] for entry in entries],np.float64)
                        active=np.asarray([entry[1] for entry in entries],bool)
                        if parameters.shape!=(len(ids),2) or not np.isfinite(parameters).all() or np.any((parameters<0)|(parameters>1)):
                            raise ValueError('Invalid full-source boundary phase')
                        source_tags=None if provenance is None else {name:values[active] for name,values in provenance.items()}
                        arcs=None
                        if dash_pattern is not None:
                            arc_lookup=chain_phase.get('boundary_arc',{})
                            if any(key not in arc_lookup for key in keys):
                                raise ValueError('Full-source dash metric missing for captured boundary')
                            arcs=np.asarray([arc_lookup[key] for key in keys],np.float64)[active]
                        fragments=yield from _styled_segments_steps(segments[active],parameters[active],wm[active],am[active],
                            sample_brush,px,alpha_scale,tags[active],projection,width,height,provenance=source_tags,
                            dash_pattern=dash_pattern,dash_arc=arcs,dash_budget=dash_budget)
                        _admit_dash_layer(fragments,dash_pattern,dash_budget)
                        key=(group['priority'],group['id'],actual,channel)
                        layer=dict(**fragments,**coverage,color=brush['color'],visibility=channel)
                        if include_provenance:layer.update(_layer_provenance(group['id'],actual,group['priority'],control))
                        buckets.setdefault(key,[]).append(layer)
                        report.append(dict(object=row.name,group=group['id'],slot=slot,actual_brush=actual,
                            channel=channel,source_edges=len(ids),virtual_source_edges=int(np.count_nonzero(row.virtual_edges[ids])),
                            styled_source_edges=int(np.count_nonzero(active)),primitives=len(fragments['projected']),
                            chains=chains,width_px=float(px),phase_policy='autonomous full-source split/orientation/min'))
    from .intersection_strokes import add_layers_steps
    intersection_report=yield from add_layers_steps(geometry,plan,view,projection,perspective,width,height,scale,settings,buckets,allow_capture=allow_intersection_capture,include_provenance=include_provenance,chain_phase=chain_phase,map_amounts=map_amounts,
        dash_patterns=dash_patterns,dash_budget=dash_budget)
    # Same actual brush identity shares a max-alpha layer across mesh sources.
    order={(r['group_id'],r['brush_id']):r['order'] for r in plan['raw']['request'].get('layers',[])}
    def layer_order(key):
        priority,gid,actual,channel=key
        return (priority,order.get((gid,actual),1000),actual,channel=='VISIBLE')
    layers=[_merge(buckets[key]) for key in sorted(buckets,key=layer_order)]
    from . import line_functions
    layers=line_functions.apply(geometry,plan,layers)
    layers,brush_report=brush_engine.apply_layers(geometry,plan,layers,view,width,height,scale,projection=projection)
    return layers,dict(groups=len(groups),layers=len(layers),chains=chain_count,rows=report,
        authority='editor/boundary and explicit intersection request; autonomous viewport preview',
        intersections=intersection_report,brush_effects=brush_report,
        coverage_profiles=sorted({layer['coverage_profile'] for layer in layers}),
        coverage_sampling='per-sample actual-brush maximum, box mean, ordered channel over, Gaussian, visible over hidden',
        samples_per_axis=sorted({layer['samples_per_axis'] for layer in layers}),
        post_filter_sigma=sorted({layer['post_filter_sigma'] for layer in layers}),
        chain_policy='preview degree-two source mesh paths; unclipped projected arclength',
        eye_plane_chain_unverified=cross_eye,native_equivalent=False,
        autonomous_chain_phase=None if chain_phase is None else dict(reports=chain_phase.get('reports',[]),weld=chain_phase.get('weld'),
            count_scope='chains remains legacy source-slot diagnostic; enabled policy counts are in reports'),
        missing=['native intersection contacts/qualification','native chain/weld/phase','native curve evaluator','native coverage/AA/filter'])
