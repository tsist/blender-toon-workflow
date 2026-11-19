# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned Line hierarchy, socket connections and collection merge domains.

Line ordering and LineSet ownership are separate: every Line is drawn and
resolved independently, while its connected groups compete only within it.
Captured native nodes and source brush identities are never rewritten.
"""
import copy
import math
import uuid
from .selector_bindings import key, need

SCHEMA = 'toon.viewport-line-hierarchy.v1'
LINE_OPTIONS=dict(size_mode=None,antialiasing=None,oversampling=None,off_screen_distance=None,elements_only=False)


def defaults(document):
    groups = sorted(document['request']['boundary']['groups'], key=lambda g:g['priority'], reverse=True)
    order=document['request'].get('selector_bindings',{}).get('order')
    if order is not None:
        groups=sorted(groups,key=lambda g:order.index(g['id']))
    return dict(schema=SCHEMA, lines=[dict(id='line-default', name='Line', priority=0,
        enabled=True, slots=[g['id'] for g in groups])], merge_domains=[], merge_groups=[])


def records(document):
    value = copy.deepcopy(document['request'].get('line_hierarchy', defaults(document)))
    need(type(value) is dict and set(value)=={'schema','lines','merge_domains','merge_groups'}
         and value['schema']==SCHEMA, 'Unsupported Line hierarchy')
    known={g['id'] for g in document['request']['boundary']['groups']}
    lines=value['lines']; seen=set(); connected=[]
    need(type(lines) is list and 1<=len(lines)<=32, 'Expected 1..32 Lines')
    for row in lines:
        required={'id','name','priority','enabled','slots'}
        need(type(row) is dict and required<=set(row)<=required|set(LINE_OPTIONS), 'Invalid Line fields')
        for name,default_value in LINE_OPTIONS.items():row.setdefault(name,default_value)
        need(row['size_mode'] in (None,'ABSOLUTE','RELATIVE') and type(row['elements_only']) is bool,'Invalid Line size/output mode')
        need(row['oversampling'] is None or type(row['oversampling']) is int and row['oversampling'] in (1,2,4),'Invalid Line sampling')
        for name,limit in [('antialiasing',16.),('off_screen_distance',65536.)]:
            number=row[name]
            need(number is None or type(number) in (int,float) and math.isfinite(number) and 0<=number<=limit,'Invalid Line '+name)
        need(type(row['id']) is str and 0<len(row['id'])<=128 and row['id'] not in seen, 'Duplicate/invalid Line id')
        seen.add(row['id'])
        need(type(row['name']) is str and 0<len(row['name'])<=128, 'Invalid Line name')
        need(type(row['priority']) is int and -1000000<=row['priority']<=1000000
             and type(row['enabled']) is bool, 'Invalid Line order or enabled state')
        need(type(row['slots']) is list and len(row['slots'])<=64, 'Too many Line sockets')
        for gid in row['slots']:
            need(gid is None or type(gid) is str and gid in known, 'Unknown connected LineSet')
            if gid is not None:connected.append(gid)
    need(len(set(connected))==len(connected), 'A LineSet may be connected to only one Line')
    # Groups created later are connected to the first Line without mutating the
    # persisted document. Explicit disconnect is represented by a disabled Line.
    missing=known-set(connected)
    for group in sorted(document['request']['boundary']['groups'],key=lambda g:g['priority'],reverse=True):
        if group['id'] not in missing:continue
        if None in lines[0]['slots']:lines[0]['slots'][lines[0]['slots'].index(None)]=group['id']
        else:
            need(len(lines[0]['slots'])<64,'No free socket for the new LineSet')
            lines[0]['slots'].append(group['id'])
    domains=value['merge_domains']; domain_ids=set(); collections=set()
    need(type(domains) is list and len(domains)<=128, 'Invalid merge domains')
    for row in domains:
        need(type(row) is dict and set(row)=={'id','collections'} and type(row['id']) is str
             and 0<len(row['id'])<=128 and row['id'] not in domain_ids, 'Invalid merge-domain identity')
        domain_ids.add(row['id'])
        need(type(row['collections']) is list and 1<=len(row['collections'])<=256, 'Empty/invalid merge domain')
        for ref in row['collections']:
            token=key(ref);need(token not in collections, 'Collection belongs to more than one merge domain')
            collections.add(token)
    flags=value['merge_groups']; flag_ids=set()
    need(type(flags) is list and len(flags)<=32, 'Invalid merge group flags')
    for row in flags:
        need(type(row) is dict and set(row)=={'group','visible','hidden'} and row['group'] in known
             and row['group'] not in flag_ids and type(row['visible']) is bool and type(row['hidden']) is bool,
             'Invalid per-channel outline merge flag')
        flag_ids.add(row['group'])
    return value


def attach(document, groups):
    if 'line_hierarchy' not in document['request']:return
    value=records(document); by_id={g['id']:g for g in groups}; batches=[]
    partition_ids={r['group'] for r in value['merge_groups'] if r['visible'] or r['hidden']} if value['merge_domains'] else set()
    # Low priority resolves first; lexical names break ties deterministically.
    for line in sorted(value['lines'], key=lambda r:(r['priority'], r['name'], r['id'])):
        selected=[]
        for slot,gid in enumerate(line['slots']):
            if gid in by_id:
                group=by_id[gid];group['line_id']=line['id'];group['line_slot']=slot
                group['line_options']={k:line[k] for k in LINE_OPTIONS}
                group['object_partition']=gid in partition_ids
                if line['size_mode'] is not None:group['size_mode']=line['size_mode']
                # Keep disabled Line authors available to RNA, portable files
                # and the editor. Only runtime batches exclude disabled Lines.
                group['priority']=len(line['slots'])-slot-1
                selected.append(group)
        batches.append(dict(id=line['id'],name=line['name'],priority=line['priority'],groups=selected))
    groups[:]=[group for line in batches for group in line['groups']]
    return batches


def batches(plan):
    value=plan['raw']['request'].get('line_hierarchy')
    if value is None or plan.get('_line_slice'):return None
    rows=records(plan['raw']); by_id={g['id']:g for g in plan['groups']}; result=[]
    for line in sorted(rows['lines'],key=lambda r:(r['priority'],r['name'],r['id'])):
        ids=[gid for gid in line['slots'] if gid in by_id] if line['enabled'] else []
        if ids:result.append((line,ids))
    return result


def active_groups(plan):
    rows=batches(plan)
    if rows is None:return plan['groups']
    ids={gid for line,gids in rows for gid in gids}
    return [g for g in plan['groups'] if g['id'] in ids]


def slice_plan(plan, ids, *, scale=1.):
    """Filter every group consumer while retaining its protected source archive."""
    ids=set(ids); result=dict(plan,groups=copy.deepcopy([g for g in plan['groups'] if g['id'] in ids]),_line_slice=True)
    result['_all_groups']=plan.get('_all_groups',active_groups(plan))
    options=result['groups'][0].get('line_options',LINE_OPTIONS) if result['groups'] else LINE_OPTIONS
    result['line_options']=copy.deepcopy(options)
    result['settings']=dict(plan['settings'])
    if options['antialiasing'] is not None:
        result['settings'].update(coverage_profile='round_capsule.v1',feather_px=options['antialiasing'])
    if options['oversampling'] is not None:result['settings']['samples_per_axis']=options['oversampling']
    padding=options['off_screen_distance']
    if padding is not None:
        for group in result['groups']:
            for brush in group['brushes'].values():brush['_projection_padding']=padding*scale
    result.pop('viewport_intersection_plan',None)
    raw=dict(plan['raw']); request=dict(raw['request']); raw['request']=request;result['raw']=raw
    for name in ('boundary_aliases','intersection_aliases'):
        request[name]=[a for a in request.get(name,[]) if a['group'] in ids]
    if request.get('intersection') is not None:
        request['intersection']=dict(request['intersection'],groups=[g for g in request['intersection']['groups'] if g['id'] in ids])
        request['intersection']['settings']=dict(request['intersection']['settings'])
        if options['antialiasing'] is not None:
            request['intersection']['settings'].update(coverage_profile='round_capsule.v1',feather_px=options['antialiasing'])
        if options['oversampling'] is not None:request['intersection']['settings']['samples_per_axis']=options['oversampling']
    # ix_sources validates its records against the full raw boundary archive.
    # Disabled slices must not synthesize additional groups from that archive.
    if 'intersection_sources' in request:
        table=request['intersection_sources'];rows=[r for r in table['groups'] if r['group'] in ids]
        if rows:request['intersection_sources']=dict(table,groups=rows)
        else:request.pop('intersection_sources')
    return result


def migrate(document, recipe, updates):
    from .style import compile_preset
    from .source_bindings import recover
    compile_preset(document,recipe);before=recover(document)
    need(type(updates) is dict and set(updates)<={'hierarchy','action','line','group','index','direction',
         'name','priority','enabled','collection','domain','visible','hidden',*LINE_OPTIONS}, 'Invalid Line update')
    candidate=copy.deepcopy(document);value=records(candidate)
    action=updates.get('action','replace')
    if action=='replace':value=copy.deepcopy(updates['hierarchy'])
    elif action=='new':
        value['lines'].append(dict(id='line-'+uuid.uuid4().hex,name=updates.get('name','Line'),
            priority=updates.get('priority',0),enabled=True,slots=[None],
            **{k:updates.get(k,v) for k,v in LINE_OPTIONS.items()}))
    elif action in ('set','socket','shrink','move','connect','remove'):
        row=next((r for r in value['lines'] if r['id']==updates.get('line')),None)
        need(row is not None,'Unknown Line')
        if action=='set':
            for field in ('name','priority','enabled',*LINE_OPTIONS):
                if field in updates:row[field]=updates[field]
        elif action=='socket':row['slots'].append(None)
        elif action=='shrink':row['slots']=[gid for gid in row['slots'] if gid is not None]
        elif action=='move':
            index=updates['index'];target=index+updates['direction']
            need(type(index) is int and type(target) is int and 0<=index<len(row['slots']) and 0<=target<len(row['slots']), 'Invalid socket move')
            row['slots'][index],row['slots'][target]=row['slots'][target],row['slots'][index]
        elif action=='connect':
            gid=updates['group'];index=updates['index']
            need(type(index) is int and 0<=index<len(row['slots']) and row['slots'][index] is None, 'Connect requires an empty socket')
            for other in value['lines']:other['slots']=[None if item==gid else item for item in other['slots']]
            row['slots'][index]=gid
        else:
            need(len(value['lines'])>1 and all(gid is None for gid in row['slots']), 'Only an empty extra Line can be removed')
            value['lines'].remove(row)
    elif action=='merge_collection':
        token=key(updates['collection']);domain=updates.get('domain')
        for row in value['merge_domains']:row['collections']=[r for r in row['collections'] if key(r)!=token]
        value['merge_domains']=[r for r in value['merge_domains'] if r['collections']]
        if domain is not None:
            row=next((r for r in value['merge_domains'] if r['id']==domain),None)
            if row is None:row=dict(id=domain,collections=[]);value['merge_domains'].append(row)
            row['collections'].append(copy.deepcopy(updates['collection']))
    elif action=='merge_group':
        gid=updates['group'];value['merge_groups']=[r for r in value['merge_groups'] if r['group']!=gid]
        value['merge_groups'].append(dict(group=gid,visible=updates['visible'],hidden=updates['hidden']))
    else:raise ValueError('Unknown Line action')
    candidate['request']['line_hierarchy']=value
    records(candidate);candidate['inputs']['projection']['editor_document']['revision']+=1
    plan=compile_preset(candidate,recipe);after=recover(candidate)
    need(before==after,'Line hierarchy changed protected brush/source identities')
    return candidate,plan,after,dict(action=action,lines=len(value['lines']),protected_source_identity_exact=True)


def mesh_domains(plan, meshes):
    value=records(plan['raw']);refs={key(ref):r['id'] for r in value['merge_domains'] for ref in r['collections']}
    result=[]
    for mesh in meshes:
        matched={refs[token] for token in getattr(mesh,'selector_meta',{}).get('collections',[]) if token in refs}
        need(len(matched)<=1,'Object is inside conflicting collection merge domains')
        # Collection instances retain their instancer namespace: unrelated
        # placements of the same collection must not merge accidentally.
        domain=next(iter(matched),None)
        instance=getattr(mesh,'instance_key',None)
        if domain is not None and instance is not None and instance[2]:
            domain=repr((domain,instance[0],instance[1]))
        result.append(domain)
    return result
