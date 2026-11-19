# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent autonomous selectors; captured native author identities stay intact."""
import copy
import json
import numpy as np

SCHEMA='toon.viewport-selector-bindings.v1'
KINDS=('objects','collections','materials')

def need(value,message):
    if not value:raise ValueError(message)

def key(ref):
    need(type(ref) is dict and set(ref)=={'name','library'},'Selector requires name and library identity')
    need(type(ref['name']) is str and 0<len(ref['name'])<=1024,'Invalid selector name')
    need(ref['library'] is None or type(ref['library']) is str and 0<len(ref['library'])<=8192,'Invalid selector library')
    return json.dumps(ref,sort_keys=True,separators=(',',':'),ensure_ascii=False)

def records(document):
    value=document['request'].get('selector_bindings')
    if value is None:return {},None
    need(type(value) is dict and set(value)=={'schema','groups','order'} and value['schema']==SCHEMA,'Unsupported selector bindings')
    known={g['id'] for g in document['request']['boundary']['groups']}
    need(type(value['groups']) is list and len(value['groups'])<=32,'Invalid selector groups')
    rows={}
    for row in value['groups']:
        need(type(row) is dict and set(row)=={'group','enabled',*KINDS},'Invalid selector group fields')
        need(type(row['group']) is str and row['group'] in known and row['group'] not in rows and type(row['enabled']) is bool,'Unknown/duplicate selector group or gate')
        for kind in KINDS:
            need(type(row[kind]) is list and len(row[kind])<=4096,'Invalid selector list')
            keys=[key(ref) for ref in row[kind]]
            need(len(set(keys))==len(keys),'Duplicate selector identity')
        rows[row['group']]=copy.deepcopy(row)
    order=value['order']
    need(order is None or type(order) is list and all(type(gid) is str for gid in order) and len(order)==len(known) and set(order)==known,'Priority order must contain every group exactly once')
    return rows,copy.deepcopy(order)

def attach(document,groups):
    rows,order=records(document)
    for group in groups:
        row=rows.get(group['id'])
        if row and row['enabled']:group['selection']={kind:[key(ref) for ref in row[kind]] for kind in KINDS}
        if order is not None:group['priority']=len(order)-1-order.index(group['id'])
    return bool(rows) or order is not None

def migrate(document,recipe,updates):
    from .style import compile_preset
    from .source_bindings import recover
    compile_preset(document,recipe);before=recover(document)
    need(type(updates) is dict and set(updates)<={'groups','order'},'Invalid selector update')
    candidate=copy.deepcopy(document);rows,order=records(candidate)
    known={g['id'] for g in candidate['request']['boundary']['groups']};seen=set()
    changes=updates.get('groups',[])
    need(type(changes) is list and len(changes)<=32,'Invalid selector changes')
    for change in changes:
        need(type(change) is dict and {'group'}<=set(change)<={'group','enabled',*KINDS},'Invalid selector change fields')
        gid=change['group'];need(type(gid) is str and gid in known and gid not in seen,'Unknown/repeated selector group');seen.add(gid)
        row=rows.get(gid,dict(group=gid,enabled=True,objects=[],collections=[],materials=[]))
        row.update(copy.deepcopy(change));rows[gid]=row
    if 'order' in updates:order=copy.deepcopy(updates['order'])
    candidate['request']['selector_bindings']=dict(schema=SCHEMA,groups=list(rows.values()),order=order)
    records(candidate)
    candidate['inputs']['projection']['editor_document']['revision']+=1
    plan=compile_preset(candidate,recipe);bindings=recover(candidate)
    need(before==bindings,'Selector edit changed protected brush/source identities')
    return candidate,plan,bindings,dict(groups=list(seen),order=order,protected_source_identity_exact=True)

def checked_meta(meta,count):
    need(type(meta) is dict and set(meta)=={'objects','collections','materials'},'Missing captured selector identities')
    for kind in ('objects','collections'):
        need(type(meta[kind]) in (list,tuple) and all(type(x) is str for x in meta[kind]),'Invalid captured selector identities')
    values=np.asarray(meta['materials'])
    need(values.shape==(count,) and values.dtype.kind in 'US','Invalid captured material selector identities')
    return dict(objects=list(meta['objects']),collections=list(meta['collections']),materials=values.copy())

def face_membership(name,face_names,group,meta=None):
    """One predicate used by boundary, phase and both intersection consumers."""
    get=group.get if isinstance(group,dict) else lambda k,default=None:getattr(group,k,default)
    selection=get('selection')
    if selection is None:
        return name in get('object_ids'),np.isin(face_names,get('material_names'))
    need(meta is not None,'Explicit selectors require captured datablock identities')
    whole=bool(set(selection['objects'])&set(meta['objects']) or set(selection['collections'])&set(meta['collections']))
    return whole,np.isin(meta['materials'],selection['materials'])
