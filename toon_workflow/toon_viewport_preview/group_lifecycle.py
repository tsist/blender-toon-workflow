# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned groups and brushes, distinct from immutable captured native authors."""
import copy
import hashlib
import math
import re
import uuid

from . import edge_sources as E, ix_sources as I, selector_bindings as S

SCHEMA = 'toon.viewport-group-lifecycle.v1'
KEY = 'group_lifecycle'
CHANNELS = E.CHANNELS


def need(value, message):
    if not value:
        raise ValueError(message)


def identity(gid, channel, role='DEFAULT'):
    return 'authored-brush-' + hashlib.sha256(('toon-owned-brush.v1\0'+gid+'\0'+channel+'\0'+role).encode('utf8')).hexdigest()


def records(document):
    ext = document['request'].get(KEY)
    if ext is None:
        return dict(schema=SCHEMA, authored=[], archived={})
    need(type(ext) is dict and set(ext) == {'schema', 'authored', 'archived'} and ext['schema'] == SCHEMA,
         'Unsupported group lifecycle')
    known = {g['id'] for g in document['request']['boundary']['groups']}
    need(type(ext['authored']) is list and len(ext['authored']) <= 32, 'Invalid owned groups')
    ids = set()
    for row in ext['authored']:
        need(type(row) is dict and set(row) == {'group', 'origin', 'brushes'} and row['origin'] == 'autonomous-defaults.v1',
             'Invalid owned group origin')
        gid = row['group']
        need(type(gid) is str and re.fullmatch('[0-9a-f]{32}', gid) and gid in known and gid not in ids, 'Invalid owned group ID')
        ids.add(gid)
        need(type(row['brushes']) is dict and 2 <= len(row['brushes']) <= 512, 'Invalid owned brush table')
        pairs = set()
        for actual, brush in row['brushes'].items():
            need(type(brush) is dict and set(brush) == {'channel', 'role', 'chain', 'shape', 'initial_amount'}, 'Invalid owned brush metadata')
            ch, role = brush['channel'], brush['role']
            need(ch in CHANNELS and role in ('DEFAULT', 'intersection')+E.CATEGORIES and (ch, role) not in pairs,
                 'Invalid owned brush channel/role')
            need(actual == identity(gid, ch, role), 'Owned actual ID and origin disagree')
            pairs.add((ch, role))
            from . import source_bindings as B, map_amount_controls as M
            need(type(brush['chain']) is dict and set(brush['chain']) == set(B.CHAIN_KEYS), 'Invalid owned chain defaults')
            need(brush['chain']['loop_direction_type'] == 'CLOCKWISE', 'Unsupported owned loop direction')
            need(all(type(brush['chain'][k]) in (int, float) and math.isfinite(brush['chain'][k])
                     for k in B.CHAIN_KEYS[:-1]), 'Invalid owned chain scalar')
            need(type(brush['shape']) is dict and set(brush['shape']) == set(B.SHAPE_KEYS)
                 and brush['shape']['brush_type'] == 'SIMPLE', 'Invalid owned SIMPLE defaults')
            need(all(type(brush['shape'][k]) in (int, float) and math.isfinite(brush['shape'][k]) for k in B.SHAPE_KEYS[1:]),
                 'Invalid owned SIMPLE scalar')
            M._amount(brush['initial_amount'])
        need(all((ch, 'DEFAULT') in pairs for ch in CHANNELS), 'Owned group requires independent V/H defaults')
    need(type(ext['archived']) is dict and set(ext['archived']) <= known, 'Unknown archived group')
    need(bool(known-set(ext['archived'])), '至少保留一个活动样式组')
    for gid, saved in ext['archived'].items():
        need(type(saved) is dict and set(saved) == {'group', 'index', 'elements'}, 'Invalid archived group snapshot')
        need(type(saved['index']) is int and saved['index'] >= 0 and type(saved['elements']) is dict, 'Invalid archive position/filters')
        row = saved['group']
        need(type(row) is dict and set(row) == {'name','label','enabled','width_scale','opacity','line_set_id','elements_only'}
             and row['name'] == gid and type(row['label']) is str and type(row['enabled']) is bool and type(row['elements_only']) is bool,
             'Invalid archived controls')
        need(type(row['line_set_id']) is int and 1 <= row['line_set_id'] <= 8, 'Invalid archived line ID')
        from .style import _number
        _number(row['width_scale'], 'archived width', 0, 8);_number(row['opacity'], 'archived opacity', 0, 1)
        need(all(type(k) is str and type(v) is bool for k,v in saved['elements'].items()), 'Invalid archived element selection')
    return copy.deepcopy(ext)


def owned(document):
    return {r['group']: r for r in records(document)['authored']}


def active(document, groups):
    ext = records(document)
    own = {r['group'] for r in ext['authored']}
    for group in groups:
        if group['id'] in own:
            group['authored'] = True
            if not group['edge_sources']['enabled']:
                group['visible_enabled'] = group['hidden_enabled'] = False
    groups[:] = [g for g in groups if g['id'] not in ext['archived']]


def native_view(document, ids):
    result = copy.deepcopy(document)
    request = result['request'];request.pop(KEY)
    editor = result['inputs']['projection']['editor_document']
    editor['groups'] = [g for g in editor['groups'] if g['uid'] not in ids]
    for name in ('boundary', 'intersection'):
        request[name]['groups'] = [g for g in request[name]['groups'] if g['id'] not in ids]
    for name in ('boundary_aliases','intersection_aliases'):
        request[name] = [r for r in request[name] if r['group'] not in ids]
    request['layers'] = [r for r in request['layers'] if r['group_id'] not in ids]
    for name in ('edge_sources','intersection_sources','wire_sources'):
        if name in request:
            request[name]['groups'] = [r for r in request[name]['groups'] if r['group'] not in ids]
            if not request[name]['groups']:request.pop(name)
    if 'selector_bindings' in request:
        request['selector_bindings']['groups'] = [r for r in request['selector_bindings']['groups'] if r['group'] not in ids]
        order = request['selector_bindings']['order']
        if order is not None:request['selector_bindings']['order'] = [g for g in order if g not in ids]
    return result


def recover(document, native_recover):
    from .wire_brush_import import project_editor_brush
    from . import source_bindings as B, wire_sources
    ext = records(document);own = {r['group']: r for r in ext['authored']}
    base = native_view(document, own)
    if document['inputs'].get('autonomous_origin') == 'toon.autonomous-style.v1':
        need(document['inputs'].get('full_preset') is None, 'Autonomous style cannot retain captured native payload')
        need(not base['request']['boundary']['groups'] and not base['inputs']['projection']['editor_document']['groups']
             and not base['request']['intersection']['groups'] and not base['request']['boundary_aliases']
             and not base['request']['intersection_aliases'] and not base['inputs']['projection'].get('source_aliases'),
             'Autonomous style contains unregistered/native authors')
        result = dict(schema=B.SCHEMA, source_identity={'origin':'toon.autonomous-style.v1'},
            source_identity_pointer='/inputs/autonomous_origin', actuals={}, routes=[], groups={},
            qualification=dict(native_equivalent=False, chain_rendering_enabled=False, weld_executed=False,
                units='independent autonomous author units', route_scope='explicit autonomous authors'))
    else:
        result = native_recover(base)
    # Filtered native validation must still report pointers into the original
    # document, including native authors appended after owned records.
    for route in result['routes']:
        gid,slot=route['group_id'],route['slot']
        if route.get('autonomous_source_profile')==I.PROFILE:
            index=next(i for i,r in enumerate(document['request']['intersection_sources']['groups']) if r['group']==gid)
            route['request_alias_pointer']=B._pointer('request','intersection_sources','groups',index,'authors',slot)
        else:
            name='boundary_aliases' if route['kind']=='boundary' else 'intersection_aliases'
            index=next(i for i,r in enumerate(document['request'][name]) if r['group']==gid and
                (route['kind']!='boundary' or r['slot']==slot))
            route['request_alias_pointer']=B._pointer('request',name,index)
    editors = {g['uid']:g for g in document['inputs']['projection']['editor_document']['groups']}
    boundaries = {g['id']:g for g in document['request']['boundary']['groups']}
    need(len(editors)==len(document['inputs']['projection']['editor_document']['groups'])
         and len(boundaries)==len(document['request']['boundary']['groups']), 'Duplicate owned/native group ID')
    # Global editor UIDs remain unique across native and owned groups.
    uids = [b['uid'] for g in editors.values() for b in g['brushes'].values()]
    need(len(uids) == len(set(uids)), 'Duplicate editor brush UID')
    edges, intersections = E.records(document), I.records(document)
    aliases = document['request']['boundary_aliases']
    for position, (gid, row) in enumerate(own.items()):
        need(gid in editors and gid not in result['groups'] and gid in edges and gid in intersections, 'Owned group lacks complete authors')
        need(not any(a['group']==gid for a in document['request']['intersection_aliases']), 'Owned groups cannot forge native IX aliases')
        need(not any(g['id']==gid for g in document['request']['intersection']['groups']), 'Owned groups cannot forge legacy IX authors')
        need(gid not in wire_sources.records(document), 'Owned groups use explicit edge wireframe routes')
        editor, boundary = editors[gid], boundaries[gid]
        expected = {E.slot_for(a):a for a in row['brushes']}
        expected['brush'] = identity(gid,'VISIBLE')
        need(set(editor['brushes'])==set(expected) and set(edges[gid]['authors'])==set(expected)-{'brush'}, 'Owned brush slots differ')
        own_aliases = [a for a in aliases if a['group']==gid]
        need(len(own_aliases)==len(expected) and all(set(a)=={'group','slot','brush_id'} and expected.get(a['slot'])==a['brush_id'] for a in own_aliases)
             and len({a['slot'] for a in own_aliases})==len(expected), 'Owned boundary aliases disagree')
        result['groups'][gid] = dict(group_id=gid, origin='authored', line_set_node=None, line=None,
            lineset_id=boundary.get('line_set_id',1), is_weld_edges=False, native_line_set_pointer=None, source_alias_pointer=None)
        for actual, meta in row['brushes'].items():
            slot = E.slot_for(actual);brush = editor['brushes'][slot]
            descriptor=edges[gid]['authors'][slot]
            need(descriptor==dict(source_brush=actual,channel=meta['channel'],mode='DEFAULT' if meta['role']=='DEFAULT' else 'SPECIFIC',
                 category='outline' if meta['role'] in ('DEFAULT','intersection') else meta['role']), 'Owned edge descriptor disagrees')
            numeric = project_editor_brush(brush)
            need(boundary.get(slot)==numeric, 'Owned numeric brush differs from editor author')
            if meta['channel']=='VISIBLE' and meta['role']=='DEFAULT':
                need({k:v for k,v in editor['brushes']['brush'].items() if k!='uid'}=={k:v for k,v in brush.items() if k!='uid'}
                     and boundary['brush']==numeric, 'Owned V default clone differs')
            pointer = B._pointer('request',KEY,'authored',position,'brushes',actual)
            props = dict(meta['chain'], **meta['shape'])
            result['actuals'][actual] = dict(actual_brush_id=actual,origin='authored',
                source_brush_key=(editor['label'] or gid)+' · '+meta['channel']+' · '+meta['role'],source_detail_key=actual,
                source_identity_sha256=hashlib.sha256(B._canonical(['autonomous-defaults.v1',gid,meta['channel'],meta['role']])).hexdigest(),
                source_brush_pointer=pointer,source_detail_pointer=pointer,detail_properties=props,
                curves={k:copy.deepcopy(brush[k]) for k in ('size_curve','alpha_curve')},
                chain_parameters_native=copy.deepcopy(meta['chain']),simple_shape_native=copy.deepcopy(meta['shape']),
                initial_amount=meta['initial_amount'])
        for slot, actual in expected.items():
            ai = next(i for i,a in enumerate(aliases) if a['group']==gid and a['slot']==slot)
            result['routes'].append(dict(kind='boundary',group_id=gid,slot=slot,actual_brush_id=actual,origin='authored',
                source_brush_key=result['actuals'][actual]['source_brush_key'],source_detail_key=actual,
                request_alias_pointer=B._pointer('request','boundary_aliases',ai),native_route_pointer=None))
        for route in edges[gid]['routes']:
            need(route['slot']==E.slot_for(identity(gid,route['channel'],route['category'] if route['mode']=='SPECIFIC' else 'DEFAULT')),
                 'Owned edge route origin disagrees')
        for slot, author in intersections[gid]['authors'].items():
            actual=author['actual'];need(actual in row['brushes'], 'Owned IX borrowed an external actual')
            meta=row['brushes'][actual]
            need(author['source_brush']==actual and author['source_channel']==meta['channel']
                 and author['source_mode']==('DEFAULT' if meta['role']=='DEFAULT' else 'SPECIFIC')
                 and meta['role'] in ('DEFAULT','intersection'), 'Owned IX descriptor disagrees')
            brush=editor['brushes'][E.slot_for(actual)]
            numeric=project_editor_brush(brush);numeric.pop('native_size');numeric['width_px']=brush['native_size']*author['reference_units']*editor['coverage_extent_factor']
            need(author['brush']==numeric and author['curves']=={k:brush[k] for k in ('size_curve','alpha_curve')}, 'Owned IX author differs')
            result['routes'].append(dict(kind='intersection',group_id=gid,slot=slot,actual_brush_id=actual,origin='authored',
                source_brush_key=result['actuals'][actual]['source_brush_key'],source_detail_key=actual,native_route_pointer=None))
        for ch, route in intersections[gid]['routes'].items():
            need(route['slot']==I.slot_for(identity(gid,ch,'intersection' if route['mode']=='SPECIFIC' else 'DEFAULT')), 'Owned IX route origin disagrees')
    need(len(result['actuals'])<=512, 'Actual registry exceeds 512 brushes')
    return result


def _new_editor():
    from .wire_brush_import import _disabled_map
    curve=dict(enabled=False,use_clip=True,clip=[0.,0.,1.,1.],extend='HORIZONTAL',tone='STANDARD',
        black_level=[0.,0.,0.],white_level=[1.,1.,1.],points=[dict(x=0.,y=1.,handle='VECTOR'),dict(x=1.,y=1.,handle='VECTOR')],samples=[1.]*256)
    return dict(uid=uuid.uuid4().hex,native_size=1.,blend_amount=1.,color=[0.,0.,0.],width_map=_disabled_map(),alpha_map=_disabled_map(),
        size_curve=copy.deepcopy(curve),alpha_curve=copy.deepcopy(curve))


def _add_brush(document,row,channel,role='DEFAULT'):
    from .wire_brush_import import project_editor_brush
    gid=row['group'];actual=identity(gid,channel,role);slot=E.slot_for(actual)
    if actual in row['brushes']:return actual
    editor=next(g for g in document['inputs']['projection']['editor_document']['groups'] if g['uid']==gid)
    boundary=next(g for g in document['request']['boundary']['groups'] if g['id']==gid)
    if role=='DEFAULT':
        brush=_new_editor();meta=dict(channel=channel,role=role,initial_amount=1.,
            chain=dict(line_link_length=0.,line_split_angle=math.pi,line_direction=0.,min_line_length=0.,loop_direction_type='CLOCKWISE'),
            shape=dict(brush_type='SIMPLE',stretch=0.,stretch_random=0.,angle=0.,angle_random=0.,antialiasing=.5))
    else:
        base=identity(gid,channel);brush=copy.deepcopy(editor['brushes'][E.slot_for(base)]);brush['uid']=uuid.uuid4().hex
        meta=copy.deepcopy(row['brushes'][base]);meta['role']=role
    row['brushes'][actual]=meta;editor['brushes'][slot]=brush;boundary[slot]=project_editor_brush(brush)
    document['request']['boundary_aliases'].append(dict(group=gid,slot=slot,brush_id=actual))
    edge=next(r for r in document['request']['edge_sources']['groups'] if r['group']==gid)
    edge['authors'][slot]=dict(source_brush=actual,channel=channel,mode='DEFAULT' if role=='DEFAULT' else 'SPECIFIC',category='outline' if role in ('DEFAULT','intersection') else role)
    document['request']['layers'].append(dict(group_id=gid,brush_id=actual,order=len(row['brushes'])-1,priority=boundary['priority'],color=list(brush['color'])+[brush['blend_amount']]))
    return actual


def _sync_ix(document,gid):
    from .wire_brush_import import project_editor_brush
    from .style import size_scale
    editor=next(g for g in document['inputs']['projection']['editor_document']['groups'] if g['uid']==gid)
    record=next(r for r in document['request']['intersection_sources']['groups'] if r['group']==gid)
    units=size_scale(editor['size_mode'],document['inputs']['width'],document['inputs']['height'],document['request']['boundary']['units'],editor['initial_scale_ex'])
    for author in record['authors'].values():
        brush=editor['brushes'][E.slot_for(author['actual'])];numeric=project_editor_brush(brush);numeric.pop('native_size')
        numeric['width_px']=brush['native_size']*units*editor['coverage_extent_factor']
        author['brush']=numeric;author['reference_units']=units
        author['curves']={k:copy.deepcopy(brush[k]) for k in ('size_curve','alpha_curve')}


def migrate(document,recipe,change,*,_bootstrap=False):
    from .style import compile_preset
    from .source_bindings import recover as bindings
    if _bootstrap:
        need(document['inputs'].get('autonomous_origin')=='toon.autonomous-style.v1' and document['inputs'].get('full_preset') is None
             and document['request']['boundary']['groups']==[] and document['inputs']['projection']['editor_document']['groups']==[]
             and change.get('action')=='NEW', 'Invalid autonomous bootstrap')
    else:
        compile_preset(document,recipe)
    need(type(change) is dict and change.get('action') in ('NEW','ARCHIVE','RESTORE','BRUSH'), 'Invalid group action')
    candidate=copy.deepcopy(document);request=candidate['request'];ext=(dict(schema=SCHEMA,authored=[],archived={}) if _bootstrap else records(candidate));request[KEY]=ext
    groups=request['boundary']['groups'];editor=candidate['inputs']['projection']['editor_document'];own={r['group']:r for r in ext['authored']}
    action=change['action'];gid=change.get('group');report=dict(action=action)
    if action=='NEW':
        need(len(groups)<32,'最多32个样式组（包含可恢复组）')
        label=change.get('label','独立样式组');need(type(label) is str and 0<len(label)<=256,'Invalid group label')
        gid=uuid.uuid4().hex;priority=max((g['priority'] for g in groups),default=-1)+1;need(priority<=100000,'Group priority exhausted')
        g=dict(id=gid,priority=priority,object_ids=[],material_names=[],visible_enabled=True,hidden_enabled=True,
            size_mode='ABSOLUTE',initial_scale_ex=1.,coverage_extent_factor=1.,line_set_id=1,elements_only=False)
        groups.append(g);eg=dict(g,uid=gid,label=label,silhouette_enabled=True,smoothing_enabled=False,marked_enabled=False,brushes={});eg.pop('id');editor['groups'].append(eg)
        row=dict(group=gid,origin='autonomous-defaults.v1',brushes={});ext['authored'].append(row)
        if 'edge_sources' not in request:request['edge_sources']=dict(schema=E.SCHEMA,groups=[])
        # Expand old native v1 routes without assigning a source for disabled Object.
        request['edge_sources']['schema']=E.SCHEMA
        for old in request['edge_sources']['groups']:
            if len(old['routes'])==12:old['routes'].extend(dict(category='object',channel=ch,enabled=False,mode='DEFAULT',slot=None,include_open=False,min_angle=30.,max_angle=180.) for ch in CHANNELS)
        edge=dict(group=gid,enabled=True,authors={},routes=[]);request['edge_sources']['groups'].append(edge)
        for ch in CHANNELS:_add_brush(candidate,row,ch)
        for cat in E.CATEGORIES:
            for ch in CHANNELS:edge['routes'].append(dict(category=cat,channel=ch,enabled=cat=='outline',mode='DEFAULT',slot=E.slot_for(identity(gid,ch)),include_open=cat!='object',min_angle=30.,max_angle=180.))
        eg['brushes']['brush']=copy.deepcopy(eg['brushes'][E.slot_for(identity(gid,'VISIBLE'))]);eg['brushes']['brush']['uid']=uuid.uuid4().hex
        g['brush']=copy.deepcopy(g[E.slot_for(identity(gid,'VISIBLE'))]);request['boundary_aliases'].append(dict(group=gid,slot='brush',brush_id=identity(gid,'VISIBLE')))
        if 'intersection_sources' not in request:request['intersection_sources']=dict(schema=I.SCHEMA,groups=[])
        ix=dict(group=gid,enabled=False,authors={},routes={})
        for ch in CHANNELS:
            actual=identity(gid,ch);slot=I.slot_for(actual)
            ix['authors'][slot]=dict(actual=actual,source_brush=actual,source_channel=ch,source_mode='DEFAULT',brush={},curves={},reference_units=1.)
            ix['routes'][ch]=dict(enabled=True,mode='DEFAULT',slot=slot)
        request['intersection_sources']['groups'].append(ix);_sync_ix(candidate,gid)
        rows,order=S.records(document)
        rows[gid]=dict(group=gid,enabled=True,objects=[],collections=[],materials=[])
        if order is None:order=[g['id'] for g in sorted(document['request']['boundary']['groups'],key=lambda g:g['priority'],reverse=True)]
        request['selector_bindings']=dict(schema=S.SCHEMA,groups=list(rows.values()),order=[gid]+order)
        report['select_group']=gid
    elif action=='ARCHIVE':
        need(gid in {g['id'] for g in groups} and gid not in ext['archived'],'Group already removed or unknown')
        need(len(groups)-len(ext['archived'])>1,'至少保留一个活动样式组')
        ext['archived'][gid]=copy.deepcopy(change['snapshot'])
    elif action=='RESTORE':
        need(gid in ext['archived'],'Group is not recoverable')
        report['restore']=ext['archived'].pop(gid);report['select_group']=gid
    else:
        need(gid in own,'只能在此入口编辑自主组笔刷')
        actual=change['actual'];need(actual in own[gid]['brushes'],'Unknown owned brush')
        from .wire_brush_import import project_editor_brush
        eg=next(g for g in editor['groups'] if g['uid']==gid);g=next(g for g in groups if g['id']==gid)
        slots=[E.slot_for(actual)]+(['brush'] if actual==identity(gid,'VISIBLE') else [])
        need(set(change['values'])<={'native_size','blend_amount','color'},'Invalid brush fields')
        for slot in slots:
            eg['brushes'][slot].update(copy.deepcopy(change['values']));g[slot]=project_editor_brush(eg['brushes'][slot])
        _sync_ix(candidate,gid)
    editor['revision']+=1
    plan=compile_preset(candidate,recipe);result=bindings(candidate)
    report['group']=gid
    return candidate,plan,result,report


def migrate_edges(document,recipe,updates):
    from . import edge_authoring
    from .style import compile_preset
    from .source_bindings import recover as bindings
    ids=owned(document);native=[u for u in updates if u.get('group') not in ids]
    candidate=(edge_authoring.migrate(document,recipe,native)[0] if native else copy.deepcopy(document))
    ext=records(candidate);candidate['request'][KEY]=ext;ids={r['group']:r for r in ext['authored']}
    seen=set()
    for update in updates:
        gid=update['group']
        if gid not in ids:continue
        need(gid not in seen and set(update)<={'group','enabled','routes','visible_enabled','hidden_enabled'},'Invalid owned edge update');seen.add(gid)
        row=next(r for r in candidate['request']['edge_sources']['groups'] if r['group']==gid)
        row['enabled']=update['enabled'];need(type(row['enabled']) is bool,'Invalid edge gate')
        eg=next(g for g in candidate['inputs']['projection']['editor_document']['groups'] if g['uid']==gid)
        bg=next(g for g in candidate['request']['boundary']['groups'] if g['id']==gid)
        for key in ('visible_enabled','hidden_enabled'):
            if key in update:eg[key]=bg[key]=update[key]
        pairs=set()
        for change in update['routes']:
            pair=change['category'],change['channel'];need(pair not in pairs,'Repeated owned route');pairs.add(pair)
            route=next(r for r in row['routes'] if (r['category'],r['channel'])==pair)
            need(set(change)<={'category','channel','enabled','mode','include_open','min_angle','max_angle'},'Invalid owned route fields')
            route.update(copy.deepcopy(change));actual=_add_brush(candidate,ids[gid],route['channel'],route['category'] if route['mode']=='SPECIFIC' else 'DEFAULT')
            route['slot']=E.slot_for(actual)
        _sync_ix(candidate,gid)
    candidate['inputs']['projection']['editor_document']['revision']+=1
    return candidate,compile_preset(candidate,recipe),bindings(candidate),dict(source_profile='owned_edge_groups.v1')


def migrate_ix(wrapped,updates):
    from . import ix_authoring
    from .style import compile_preset
    from .source_bindings import recover as bindings
    ids=owned(wrapped['document']);native=[u for u in updates if u.get('group') not in ids]
    result=ix_authoring.migrate(wrapped,native)[0] if native else copy.deepcopy(wrapped)
    document=result['document'];ext=records(document);document['request'][KEY]=ext;ids={r['group']:r for r in ext['authored']}
    seen=set()
    for update in updates:
        gid=update['group']
        if gid not in ids:continue
        need(gid not in seen and set(update)=={'group','enabled','routes'},'Invalid owned IX update');seen.add(gid)
        row=next(r for r in document['request']['intersection_sources']['groups'] if r['group']==gid)
        row['enabled']=update['enabled'];need(type(row['enabled']) is bool,'Invalid owned IX gate')
        for ch,change in update['routes'].items():
            need(ch in CHANNELS and set(change)<={'enabled','mode'},'Invalid owned IX channel')
            route=row['routes'][ch];route.update(copy.deepcopy(change))
            actual=_add_brush(document,ids[gid],ch,'intersection' if route['mode']=='SPECIFIC' else 'DEFAULT');slot=I.slot_for(actual);route['slot']=slot
            if slot not in row['authors']:row['authors'][slot]=dict(actual=actual,source_brush=actual,source_channel=ch,source_mode=route['mode'],brush={},curves={},reference_units=1.)
        _sync_ix(document,gid)
    document['inputs']['projection']['editor_document']['revision']+=1
    return result,compile_preset(document,result['recipe']),bindings(document),dict(source_profile='owned_intersection_groups.v1')
