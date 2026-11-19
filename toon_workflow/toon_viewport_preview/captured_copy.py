# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy a captured document's executable authors into independent owned authors.

No native identity is forged. The complete input is retained as inert provenance
and remains exportable. Shared source authors become independent per owned slot.
"""
import copy
import hashlib
import uuid


def convert(wrapped):
    from . import file_authoring as F,group_lifecycle as G,source_bindings as B,edge_sources as E,ix_sources as I
    from . import wire_sources as W,wire_brush_import as WI,selector_bindings as S
    from .style import compile_preset,size_scale
    from . import curve_editor
    source=F.checked(wrapped);src=source['document'];full=src['inputs'].get('full_preset')
    if src['inputs'].get('autonomous_origin')=='toon.autonomous-style.v1':return source
    B._need(type(full) is dict,'Captured author copy requires complete native provenance')
    plan=compile_preset(src,source['recipe']);bindings=B.recover(src)
    groups=src['request']['boundary']['groups'];editors={g['uid']:g for g in src['inputs']['projection']['editor_document']['groups']}
    result=F.new_document(width=src['inputs']['width'],height=src['inputs']['height'],label=editors[groups[0]['id']]['label'] or 'Copied')
    result['recipe']=copy.deepcopy(source['recipe']);dst=result['document'];mapping={};clones={};clone_info=[]
    live=[dst['request']['boundary']['groups'][0]['id']]
    for row in groups[1:]:
        dst,_,_,report=G.migrate(dst,result['recipe'],dict(action='NEW',label=editors[row['id']]['label'] or 'Copied'))
        live.append(report['group'])
    result['document']=dst
    dst['request']['boundary']['settings']=copy.deepcopy(src['request']['boundary']['settings'])
    dst['request']['boundary']['units']=copy.deepcopy(src['request']['boundary']['units'])
    if src['request'].get('intersection',{}).get('settings'):
        dst['request']['intersection']['settings']=copy.deepcopy(src['request']['intersection']['settings'])
    dst['inputs']['projection']['editor_document']['settings']=copy.deepcopy(src['inputs']['projection']['editor_document']['settings'])
    alias={(r['group'],r['slot']):r['brush_id'] for r in src['request']['boundary_aliases']}
    raw_lines={r['node']:r for r in full['line_sets']};edge_records=E.records(src);ix_records=I.records(src);wire_records=W.records(src)
    selectors,selector_order=S.records(src)

    def source_editor(actual,*,allow_unmapped=False):
        origin=next((r for r in src['request']['boundary_aliases'] if r['brush_id']==actual and r['slot']!='brush'),None) or next((r for r in src['request']['boundary_aliases'] if r['brush_id']==actual),None)
        if origin:return copy.deepcopy(editors[origin['group']]['brushes'][origin['slot']])
        record=bindings['actuals'].get(actual)
        name=record['source_brush_key'] if record else next((name for name in full['brushes'] if B._identity(full['source'],name)==actual),None)
        B._need(name in full['brushes'],'Captured brush has no complete source author')
        raw=full['brushes'][name];props=raw['properties'];detail=full['details'][raw['detail']]
        B._need(allow_unmapped or not raw['maps']['size']['enabled'] and not raw['maps']['color']['enabled'],
            'Raw native map is active but its executable editor mapping was not captured; provide a captured numeric/editor map')
        value=dict(uid=uuid.uuid4().hex,native_size=props['size'],blend_amount=props['blend_amount'],color=copy.deepcopy(props['brush_color']),
            width_map=WI._disabled_map(),alpha_map=WI._disabled_map(),size_curve=WI._import_curve(detail,'size_curve'),alpha_curve=WI._import_curve(detail,'alpha_curve'))
        WI.project_editor_brush(value);return value

    def source_metadata(actual):
        if actual in bindings['actuals']:return bindings['actuals'][actual]
        name=next((name for name in full['brushes'] if B._identity(full['source'],name)==actual),None)
        B._need(name is not None,'Unknown native source brush')
        return B._actual_record(full,actual,name,hashlib.sha256(B._canonical(full['source'])).hexdigest())

    def intersection_editor(actual,numeric,curves):
        brush=source_editor(actual,allow_unmapped=True);brush.update(copy.deepcopy(curves))
        for name in ('blend_amount','color'):brush[name]=copy.deepcopy(numeric[name])
        for name in ('width_map','alpha_map'):
            compact=numeric[name]
            if compact is None:brush[name]['enabled']=False
            else:
                selection=compact['selection'];brush[name]=dict(enabled=True,selection_kind='name' if 'name' in selection else 'index',layer_name=selection.get('name',''),layer_index=selection.get('index',0),channel=compact['channel'],encoding=compact['encoding'],missing=compact['missing'])
        return brush

    def put(old_gid,new_gid,channel,role,actual,*,author=None,reference_width=None):
        row=next(r for r in dst['request']['group_lifecycle']['authored'] if r['group']==new_gid)
        new=G._add_brush(dst,row,channel,role);slot=E.slot_for(new)
        target=next(g for g in dst['inputs']['projection']['editor_document']['groups'] if g['uid']==new_gid)
        boundary=next(g for g in dst['request']['boundary']['groups'] if g['id']==new_gid)
        brush=copy.deepcopy(author) if author is not None else source_editor(actual)
        brush['uid']=target['brushes'][slot]['uid']
        if reference_width is not None:
            units=size_scale(target['size_mode'],dst['inputs']['width'],dst['inputs']['height'],dst['request']['boundary']['units'],target['initial_scale_ex'])
            extent=units*target['coverage_extent_factor'];B._need(extent>0,'Cannot copy legacy intersection width onto zero-size units')
            brush['native_size']=reference_width/extent
        target['brushes'][slot]=brush;boundary[slot]=WI.project_editor_brush(brush)
        if channel=='VISIBLE' and role=='DEFAULT':
            uid=target['brushes']['brush']['uid'];target['brushes']['brush']=copy.deepcopy(brush);target['brushes']['brush']['uid']=uid;boundary['brush']=WI.project_editor_brush(brush)
        meta=source_metadata(actual);row['brushes'][new].update(chain=copy.deepcopy(meta['chain_parameters_native']),shape=copy.deepcopy(meta['simple_shape_native']),initial_amount=meta.get('initial_amount',1.))
        clones.setdefault(actual,[])
        if new not in clones[actual]:clones[actual].append(new)
        clone_info.append(dict(source_group=old_gid,source_actual=actual,target_group=new_gid,target_actual=new,channel=channel,role=role))
        effects=src['request'].get('brush_effects',{}).get('brushes',{})
        if actual in effects:dst['request'].setdefault('brush_effects',dict(schema='toon.brush-effects.v1',brushes={}))['brushes'][new]=copy.deepcopy(effects[actual])
        return new

    for source_group,new_gid in zip(groups,live):
        old_gid=source_group['id'];mapping[old_gid]=new_gid;original=editors[old_gid]
        target=next(g for g in dst['inputs']['projection']['editor_document']['groups'] if g['uid']==new_gid)
        boundary=next(g for g in dst['request']['boundary']['groups'] if g['id']==new_gid)
        for name in ('priority','object_ids','material_names','visible_enabled','hidden_enabled','size_mode','initial_scale_ex','coverage_extent_factor','line_set_id','elements_only'):
            if name in source_group:target[name]=boundary[name]=copy.deepcopy(source_group[name])
        target['label']=original['label'];target['display_color']=copy.deepcopy(original.get('display_color',[.25,.35,.45]))
        selection=next(r for r in dst['request']['selector_bindings']['groups'] if r['group']==new_gid)
        selection.update(copy.deepcopy(selectors.get(old_gid,dict(group=old_gid,enabled=False,objects=[],collections=[],materials=[]))));selection['group']=new_gid
        edge=next(r for r in dst['request']['edge_sources']['groups'] if r['group']==new_gid)
        native=raw_lines.get(bindings['groups'][old_gid].get('line_set_node'))
        source_edge=edge_records.get(old_gid);source_wire=wire_records.get(old_gid)
        # Default V/H authors are copied independently, even when their source
        # actual is shared. Only explicit routed sources become new actuals.
        base=alias.get((old_gid,'brush'))
        B._need(base is not None,'Captured group lacks a complete default brush')
        for channel in E.CHANNELS:
            actual=base
            if source_edge and source_edge['enabled']:
                routes=[r for r in source_edge['routes'] if r['channel']==channel and r['mode']=='DEFAULT' and r['slot'] in source_edge['authors']]
                if routes:actual=alias[old_gid,routes[0]['slot']]
            put(old_gid,new_gid,channel,'DEFAULT',actual)
        if source_edge and source_edge['enabled']:
            for route in edge['routes']:
                old=next((r for r in source_edge['routes'] if (r['category'],r['channel'])==(route['category'],route['channel'])),None)
                if old is None:route['enabled']=False;continue
                route.update({k:copy.deepcopy(old[k]) for k in ('enabled','mode','include_open','min_angle','max_angle')})
                if old['slot'] is None:
                    route['slot']=None;continue
                actual=alias[old_gid,old['slot']]
                new=put(old_gid,new_gid,route['channel'],route['category'] if route['mode']=='SPECIFIC' else 'DEFAULT',actual)
                route['slot']=E.slot_for(new)
        elif source_wire and source_wire['enabled']:
            for route in edge['routes']:
                route['enabled']=route['category']=='wireframe' and source_wire['visible' if route['channel']=='VISIBLE' else 'hidden']
                route['mode']='SPECIFIC' if route['category']=='wireframe' else 'DEFAULT'
                if route['category']=='wireframe':
                    oldslot='wireframe_visible_brush' if route['channel']=='VISIBLE' else 'wireframe_hidden_brush'
                    actual=alias[old_gid,oldslot];route['slot']=E.slot_for(put(old_gid,new_gid,route['channel'],'wireframe',actual))
        else:
            categories=src['request']['boundary']['settings'].get('source_categories',['selected','smoothing'])
            enabled=dict(outline='selected' in categories and original.get('silhouette_enabled') is not False,
                selected='selected' in categories and original.get('marked_enabled') is not False,
                smooth='smoothing' in categories and original.get('smoothing_enabled') is not False)
            for route in edge['routes']:
                cat=route['category'];route['enabled']=enabled.get(cat,False)
                oldslot={'outline':'silhouette_brush','smooth':'smoothing_brush','selected':'marked_brush'}.get(cat,'brush')
                route['include_open']=src['request']['boundary']['settings'].get('include_open',True)
                if cat in enabled and (old_gid,oldslot) in alias and oldslot!='brush':
                    route['mode']='SPECIFIC';route['slot']=E.slot_for(put(old_gid,new_gid,route['channel'],cat,alias[old_gid,oldslot]))
        ix=next(r for r in dst['request']['intersection_sources']['groups'] if r['group']==new_gid)
        source_ix=ix_records.get(old_gid)
        legacy=next((g for g in src['request'].get('intersection',{}).get('groups',[]) if g['id']==old_gid),None)
        if source_ix and source_ix['enabled']:
            ix['enabled']=True
            for channel,route in source_ix['routes'].items():
                a=source_ix['authors'][route['slot']]
                brush=intersection_editor(a['actual'],a['brush'],a['curves'])
                # Separate IX copies preserve the current numeric width/maps
                # even if a captured default actual is shared with a boundary
                # consumer whose explicit projection differs.
                new=put(old_gid,new_gid,channel,'intersection',a['actual'],author=brush,reference_width=a['brush']['width_px'])
                slot=I.slot_for(new);ix['routes'][channel]=dict(enabled=route['enabled'],mode='SPECIFIC',slot=slot)
                ix['authors'][slot]=dict(actual=new,source_brush=new,source_channel=channel,source_mode='SPECIFIC',brush={},curves={},reference_units=1.)
        elif legacy is not None and not(source_wire and source_wire['enabled']):
            actual=next(r['brush_id'] for r in src['request']['intersection_aliases'] if r['group']==old_gid)
            author=curve_editor._intersection_author(plan,old_gid,source)
            brush=intersection_editor(actual,legacy['brush'],author)
            ix['enabled']=True
            for channel in E.CHANNELS:
                new=put(old_gid,new_gid,channel,'intersection',actual,author=brush,reference_width=legacy['brush']['width_px']);slot=I.slot_for(new)
                ix['authors'][slot]=dict(actual=new,source_brush=new,source_channel=channel,source_mode='SPECIFIC',brush={},curves={},reference_units=1.)
                ix['routes'][channel]=dict(enabled=legacy['visible_enabled'] if channel=='VISIBLE' else legacy['hidden_enabled'],mode='SPECIFIC',slot=slot)
        G._sync_ix(dst,new_gid)
    dst['request']['selector_bindings']['order']=[mapping[g] for g in selector_order] if selector_order is not None else [mapping[g['id']] for g in sorted(groups,key=lambda g:g['priority'],reverse=True)]
    dst['request']['group_lifecycle']['archived']=F._remap(src['request'].get('group_lifecycle',{}).get('archived',{}),mapping)
    # Copy hierarchy/LF, grouping references and scene controls after the complete
    # group/actual map exists. Shared source actual controls fan out to every clone.
    for name in ('line_hierarchy','line_functions'):
        if name in src['request']:dst['request'][name]=F._remap(src['request'][name],mapping)
    controls=F._remap(source.get('scene_controls',{}),mapping)
    for name,field in (('chain_controls','brushes'),('map_amounts','amounts'),('dash_patterns','patterns')):
        if controls.get(name):controls[name][field]={new:copy.deepcopy(value) for old,value in source['scene_controls'][name][field].items() for new in clones.get(old,[])}
    if controls:result['scene_controls']=controls
    archive=dict(schema='toon.inert-captured-source.v1',sha256=hashlib.sha256(B._canonical(source)).hexdigest(),source_identity=copy.deepcopy(full['source']),
        document=copy.deepcopy(src),recipe=copy.deepcopy(source['recipe']),wrapper_metadata={k:copy.deepcopy(v) for k,v in source.items() if k not in ('document','recipe')},copy_routes=clone_info,
        qualification='Independent per-slot owned copy; original native sharing retained only in this inert source archive')
    dst['provenance']=dict(captured_import_archives=[archive])
    return F.checked(result)
