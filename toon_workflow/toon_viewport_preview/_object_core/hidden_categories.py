# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage-only opaque same-object/same-domain H/O/L hidden-category partition.

All ray/triangle/depth predicates come from the unchanged analytic kernel.
Intervals describe a.e. positive-measure interiors with closure endpoints;
there is no new isolated-point reject rule, epsilon or short-piece removal.
The adapter may supply ORTHO view depth or PERSP reciprocal clip-w.
"""
from __future__ import annotations
import copy
import time
import numpy as np
from .geometry import _camera_parameters, _require
from .visibility import _screen_interval
from .analytic_visibility import _ProjectedIndex, _triangle_intervals, _clip_positive
from .occlusion import _validate_sources, _source_span
from .intervals import union_intervals, subtract_intervals, UnresolvedVisibility


def _intersection(left, right, limit):
    """Intersect existing disjoint float64 spans without new numerical roots."""
    result=[];i=j=0
    while i<len(left) and j<len(right):
        a,b=max(left[i][0],right[j][0]),min(left[i][1],right[j][1])
        if b>a:
            result.append((a,b))
            _require(len(result)<=limit,'Hidden category intersection exceeds max_pieces')
        if left[i][1]<=right[j][1]:i+=1
        else:j+=1
    return result


def _witness_cover(ids,lefts,rights,a,b):
    """Cover a proven union component with actual triangle subintervals.

    The geometric union has already been evaluated exhaustively. Keeping one
    covering proof avoids charging the render output budget for every redundant
    overlapping triangle. No source span, predicate or category is removed.
    """
    candidates=sorted((max(a,float(lo)),min(b,float(hi)),int(identifier))
        for identifier,lo,hi in zip(ids,lefts,rights) if min(b,float(hi))>max(a,float(lo)))
    cursor=a;position=0;result=[]
    while cursor<b:
        best=None
        while position<len(candidates) and candidates[position][0]<=cursor:
            left,right,identifier=candidates[position];position+=1
            if right>cursor and (best is None or (right,-identifier)>(best[0],-best[1])):
                best=(right,identifier)
        if best is None:raise UnresolvedVisibility('Hidden category covering witness has a gap')
        end=min(b,best[0]);result.append((best[1],cursor,end));cursor=end
    return result


def partition_hidden_categories(objects, edge_records, camera, *,
        surface_model='opaque_solid', identity_policy='object_id_only', merge=False,
        candidate_scope='current_m1_silhouette_open', index_objects=None, grid_px=4,
        max_grid_refs=20_000_000, max_pairs=10_000_000, max_pieces=100_000,witness_profile='all'):
    """Return (complete records, stats), or raise without returning a partial.

    Caller supplies complete prepared geometry and current-M1 source records,
    including its actual local_surface_domain_both_sides and side-count proof.
    H=all non-support strict-front coverage; O=all non-support same-object or
    explicit same-domain
    camera-clipped coverage on either depth side. If local L then Hobject=H,
    otherwise Hobject=H intersect O. Houtline=H minus Hobject.

    Each hidden record has front occluder_witnesses proving H. An object record
    with L=False additionally has same_object_witnesses whose union proves O
    over that record. L=True uses the retained upstream local proof, never a
    fabricated non-support O witness. All emitted witness entries count toward
    the same cumulative record+witness budget. Visible records have category=None.
    """
    _validate_sources(objects,edge_records)
    _require(witness_profile in ('all','cover'),'Unknown hidden-category witness profile')
    _require(surface_model=='opaque_solid' and type(merge) is bool
             and candidate_scope=='current_m1_silhouette_open'
             and ((merge and identity_policy=='explicit_domain')
                  or (not merge and identity_policy=='object_id_only')),
             'Unsupported hidden category scope; only opaque current-M1 object or explicit domain identity')
    _require(type(grid_px) is int and 1<=grid_px<=256 and
             all(type(v) is int and v>0 for v in (max_grid_refs,max_pairs,max_pieces)),
             'Invalid hidden category budgets')
    _require(max_grid_refs<=20_000_000 and max_pairs<=10_000_000 and max_pieces<=100_000,
             'Hidden category budgets exceed v1 maximums')
    for edge in edge_records:
        local=edge.get('local_surface_domain_both_sides');counts=edge.get('local_surface_domain_side_counts')
        _require(type(local) is bool and isinstance(counts,dict),
                 'Current-M1 local projected support proof is required')
        _require(all(type(counts.get(k)) is int and counts[k]>=0 for k in ('positive','negative'))
                 and local==bool(counts['positive']>0 and counts['negative']>0),
                 'Local projected support flag and side counts disagree')
    cp=_camera_parameters(camera);margin=float(camera.get('margin_px',0))
    _require(np.isfinite(margin) and margin>=0,'Invalid hidden category viewport margin')
    stats=dict(method='projected_affine_H_O_L_hidden_categories_experimental',
        projected_tangent_policy=camera.get('projected_tangent_policy','reject'),
        policy=('same_domain_either_depth_with_local_projected_support' if merge
                else 'same_object_either_depth_with_local_projected_support'),edge_count=len(edge_records),
        triangle_count=0,grid_px=grid_px,grid_references=0,grid_bytes=0,pairs=0,
        support_pairs_excluded=0,world_degenerate_triangles=0,projected_zero_area_events=0,
        culled_triangles=0,exact_predicate_fallbacks=0,offscreen_rejected=0,zero_measure_sources=0,
        visible_records=0,hidden_records=0,hidden_object_records=0,hidden_outline_records=0,
        front_witness_intervals=0,same_object_witness_intervals=0,returned_piece_budget_used=0,
        index_seconds=0.,coverage_seconds=0.,partition_seconds=0.,no_physical_ray_offset=True,
        reference_kernel_equivalence=False,feature_acceptance='not_decided',
        witness_profile=witness_profile,
        endpoint_semantics='positive-measure a.e. interiors with closure endpoints',
        limitations=['Opaque current-M1 silhouette/open only',
                     'Explicit same-domain identity; merge=True' if merge else 'Exact object_id; merge=False',
                     'Local L is an upstream classifier proof, not recomputed here',
                     'No optical/layer-count/priority/style/RGBA or general feature qualification'])
    tick=time.perf_counter();index=_ProjectedIndex(objects if index_objects is None else index_objects,
        cp,margin,merge,grid_px,max_grid_refs,stats)
    stats['index_seconds']=time.perf_counter()-tick
    stats['identity_columns_bytes']=int(index.source_object_index.nbytes+index.source_triangle_index.nbytes)
    records=[];used=0
    for edge in edge_records:
        if edge['u1']==edge['u0']:
            stats['zero_measure_sources']+=1;continue
        xy,world,depth=(np.asarray(edge[k],dtype=np.float64) for k in ('xy','world','depth'))
        interval=_screen_interval(xy,cp['width'],cp['height'],margin)
        if interval is None or interval[1]<=interval[0]:
            stats['offscreen_rejected']+=1;continue
        tick=time.perf_counter();clipped_xy=xy[0]+np.asarray(interval)[:,None]*(xy[1]-xy[0])
        ids=index.query(clipped_xy);stats['pairs']+=len(ids)
        _require(stats['pairs']<=max_pairs,'Hidden category pair budget exceeded; no partial result accepted')
        pair=np.asarray(edge['vertex_ids'])+index.offsets[edge['object_id']]
        support=(np.any(index.support_vertices[ids]==pair[0],axis=1)
                 &np.any(index.support_vertices[ids]==pair[1],axis=1))
        stats['support_pairs_excluded']+=int(support.sum());ids=ids[~support]
        try:
            ids,lo,hi,g0,g1=_triangle_intervals(index,ids,xy,depth,interval,stats)
            flo,fhi,front=_clip_positive(-g0,-g1,lo,hi,strict=True)
        except UnresolvedVisibility as error:
            error.visibility_context=dict(object_id=edge['object_id'],edge_id=edge['edge_id'],
                stage='hidden_category_raw_coverage',interval=list(interval));raise
        same=index.domain[ids]==index.domains[edge['object_id']]
        front_ids,front_lo,front_hi=ids[front],flo[front],fhi[front]
        same_ids,same_lo,same_hi=ids[same],lo[same],hi[same]
        H=union_intervals(zip(front_lo,front_hi),max_pieces=max_pieces)
        O=union_intervals(zip(same_lo,same_hi),max_pieces=max_pieces)
        stats['coverage_seconds']+=time.perf_counter()-tick;tick=time.perf_counter()
        L=edge['local_surface_domain_both_sides']
        object_spans=H if L else _intersection(H,O,max_pieces)
        outline_spans=[span for h in H for span in subtract_intervals(h,object_spans,max_pieces=max_pieces)]
        visible=subtract_intervals(interval,H,max_pieces=max_pieces)
        pieces=[(a,b,None) for a,b in visible]+[(a,b,'object') for a,b in object_spans]+[(a,b,'outline') for a,b in outline_spans]
        pieces.sort(key=lambda value:(value[0],value[1],value[2] or ''))
        for a,b,category in pieces:
            start,end=_source_span(edge,a,b);front_witnesses=[];same_witnesses=[]
            def collect(effective_ids,lefts,rights,target,role):
                witnesses=(_witness_cover(effective_ids,lefts,rights,a,b) if witness_profile=='cover'
                    else zip(effective_ids,lefts,rights))
                for effective_id,wa,wb in witnesses:
                    left,right=max(a,float(wa)),min(b,float(wb))
                    if right<=left:continue
                    wu0,wu1=_source_span(edge,left,right)
                    oi=int(index.source_object_index[effective_id]);ti=int(index.source_triangle_index[effective_id])
                    witness=dict(object_id=objects[oi]['id'],source_object_index=oi,
                        source_triangle_index=ti,u0=wu0,u1=wu1,role=role)
                    mapping=objects[oi]['arrays'].get('triangle_polygon')
                    if mapping is not None:witness['source_polygon_index']=int(mapping[ti])
                    target.append(witness)
                    _require(used+1+len(front_witnesses)+len(same_witnesses)<=max_pieces,
                             'Hidden category output/witness budget exceeded; no partial result accepted')
                target.sort(key=lambda w:(w['source_object_index'],w['source_triangle_index'],w['u0'],w['u1']))
            if category is not None:
                collect(front_ids,front_lo,front_hi,front_witnesses,'strict_front_occluder')
                _require(bool(front_witnesses),'Hidden category span lacks a front witness')
                if category=='object' and not L:
                    collect(same_ids,same_lo,same_hi,same_witnesses,'same_object_either_depth_coverage')
                    _require(bool(same_witnesses),'Hidden object span lacks same-object coverage proof')
            used+=1+len(front_witnesses)+len(same_witnesses)
            _require(used<=max_pieces,'Hidden category output/witness budget exceeded; no partial result accepted')
            row={k:copy.deepcopy(edge[k]) for k in ('source','object_id','edge_id','original_edge_id','vertex_ids','kind',
                'local_surface_domain_both_sides','local_surface_domain_side_counts') if k in edge}
            ts=np.asarray([a,b]);visibility='visible' if category is None else 'hidden'
            row.update(u0=start,u1=end,world=world[0]+ts[:,None]*(world[1]-world[0]),
                xy=xy[0]+ts[:,None]*(xy[1]-xy[0]),depth=depth[0]+ts*(depth[1]-depth[0]),
                visibility=visibility,category=category,occluder_witnesses=front_witnesses,
                same_object_witnesses=same_witnesses,
                category_basis=None if category is None else 'local_projected_both_sides' if L else 'non_support_same_object_coverage' if category=='object' else 'absence_of_local_both_sides_and_same_object_coverage',
                visibility_method=stats['method'])
            records.append(row);stats[visibility+'_records']+=1
            if category is not None:stats['hidden_'+category+'_records']+=1
            stats['front_witness_intervals']+=len(front_witnesses);stats['same_object_witness_intervals']+=len(same_witnesses)
        stats['partition_seconds']+=time.perf_counter()-tick
    stats.update(records=len(records),returned_piece_budget_used=used)
    return records,stats
