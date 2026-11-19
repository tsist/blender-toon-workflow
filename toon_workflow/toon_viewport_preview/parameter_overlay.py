# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed allowlist and detached overrides for numeric production authors."""
import copy
import hashlib
import json
import math
from . import brush_effects as B,line_hierarchy as H,line_functions as LF,source_bindings

SCOPES=('BRUSH','LINE','MERGE','LF','MASK','SELF')
LINE_FIELDS={'enabled','elements_only','priority','antialiasing','off_screen_distance'}
def need(ok,message):
    if not ok:raise ValueError(message)
def canonical(target):return json.dumps(target,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def token(target):return hashlib.sha256(canonical(target).encode('utf8')).hexdigest()
def target(scope,owner,path,component=-1):
    need(scope in SCOPES and type(owner) is str and 0<len(owner)<=256,'Unknown parameter scope or owner')
    need(type(path) in (tuple,list) and 1<=len(path)<=5 and all(type(p) is str and p for p in path),'Invalid parameter field')
    need(type(component) is int and -1<=component<=2,'Invalid parameter component')
    return dict(scope=scope,owner=owner,path=list(path),component=component)
def _records(document,scope,*,bindings=None):
    if scope=='BRUSH':
        bindings=source_bindings.recover(document) if bindings is None else bindings
        actuals=bindings['actuals'];records=B.records(document,actuals)
        return {a:records[a] if a in records else B.defaults() for a in actuals}
    if scope in ('LINE','MERGE'):
        h=H.records(document)
        return {r['id' if scope=='LINE' else 'group']:r for r in h['lines' if scope=='LINE' else 'merge_groups']}
    ext=LF.records(document)
    if ext is None:return {}
    field={'LF':'containers','MASK':'masks','SELF':'self_intersections'}[scope]
    return {r['id' if scope=='LF' else 'group']:r for r in ext.get(field,[])}
def _get(row,path):
    for p in path:row=row[p]
    return row
def _put(row,path,value):
    for p in path[:-1]:row=row[p]
    row[path[-1]]=value
def _allowed(scope,path):
    if scope=='BRUSH':
        if path[0] in ('shape','stroke','color'):return len(path)==2
        if path[0]=='maps':return len(path)==3 and path[1] in B.MAP_NAMES and path[2] in ('enabled','amount','offset','tiling','rotation')
        if path[0]=='curves':return len(path)==2 and path[1] in ('size_enabled','alpha_enabled','reduction_start','reduction_end')
        if path[0]=='reduction':return len(path)==4 and path[1] in ('VISIBLE','HIDDEN') and path[2] in ('size','alpha') and path[3] in ('enabled','refer_object_on','reduction_start','reduction_end')
        if path[0]=='raster':return len(path)==2 and path[1] in ('pixel_aspect','off_screen_distance','antialiasing')
        if path[0]=='chain':return path==['chain','weld_edges']
        return False
    if scope=='LINE':return len(path)==1 and path[0] in LINE_FIELDS
    if scope=='MERGE':return len(path)==1 and path[0] in ('visible','hidden')
    if scope=='LF':return len(path)==1 and path[0] in LF.GATES or len(path)==3 and path[0]=='overrides' and path[1] in LF.CATEGORIES and path[2] in ('on','color','amount')
    if scope=='MASK':return path==['enabled']
    if scope=='SELF':return len(path)==1 and path[0] in ('VISIBLE','HIDDEN')
    return False
def _active_owner(scope,owner,archived,groups):
    if scope in ('MERGE','MASK','SELF'):return owner not in archived
    if scope=='BRUSH':return bool(groups-archived)
    return True
def owner_active(document,scope,owner,*,bindings=None):
    """Archive retains source records; it does not retain authoring eligibility."""
    from . import group_lifecycle
    archived=set(group_lifecycle.records(document)['archived'])
    groups=set()
    if scope=='BRUSH':
        bindings=source_bindings.recover(document) if bindings is None else bindings
        groups={row['group_id'] for row in bindings['routes'] if row['actual_brush_id']==owner}
    return _active_owner(scope,owner,archived,groups)
def describe(document,t):
    checked=target(t['scope'],t['owner'],t['path'],t['component']);need(checked==t,'Parameter target fields differ')
    scope,owner,path,component=t['scope'],t['owner'],t['path'],t['component']
    need(_allowed(scope,path),'This parameter is structural, referenced or not animatable')
    rows=_records(document,scope);need(owner in rows,'Animated author identity is missing: '+owner)
    need(owner_active(document,scope,owner),'Animated author owner is archived; disable its tracks before removal: '+owner)
    return _descriptor(rows[owner],t)
def _descriptor(row,t,*,brush_defaults=None):
    """Describe an already-located validated row without recovering its owner."""
    checked=target(t['scope'],t['owner'],t['path'],t['component']);need(checked==t,'Parameter target fields differ')
    scope,path,component=t['scope'],t['path'],t['component']
    need(_allowed(scope,path),'This parameter is structural, referenced or not animatable')
    try:value=_get(row,path)
    except (KeyError,TypeError) as error:raise ValueError('Unknown parameter field') from error
    vector=type(value) is list
    if vector:
        need(len(value) in (2,3) and component in range(len(value)),'Vector parameter requires its exact component')
        value=value[component]
    else:need(component==-1,'Scalar parameter cannot have a component')
    need(type(value) in (bool,int,float),'Enum, reference, inherited None and structural values remain static')
    # JSON numeric spelling is not a parameter type declaration: angle=0 and
    # angle=0.0 must create the same persistent RNA path.
    if scope=='BRUSH':
        declared=_get(B.defaults() if brush_defaults is None else brush_defaults,path)
        kind=('BOOL' if path==['chain','weld_edges'] and type(value) is bool else 'FLOAT') if vector or declared is None else {bool:'BOOL',int:'INT',float:'FLOAT'}[type(declared)]
    elif scope=='LINE':kind='INT' if path==['priority'] else 'BOOL' if path[0] in ('enabled','elements_only') else 'FLOAT'
    elif scope in ('MERGE','MASK','SELF'):kind='BOOL'
    else:kind='BOOL' if len(path)==1 or path[-1]=='on' else 'FLOAT'
    return dict(target=t,token=token(t),kind=kind,static=value)
def value(spec,v):
    kind=spec['kind']
    if kind=='BOOL':need(type(v) is bool,'Boolean animation requires an exact bool')
    elif kind=='INT':need(type(v) is int and -2147483648<=v<=2147483647,'Integer animation requires a signed 32-bit int')
    else:need(type(v) in (int,float) and math.isfinite(v) and abs(v)<=1e12,'Float animation requires a bounded finite scalar');v=float(v)
    return v
def apply(document,overrides):
    """Validate targets on static authors, then validate all changed rows together."""
    if not overrides:return document
    result=copy.deepcopy(document);changed={};seen=set()
    for t,v in overrides:
        spec=describe(document,t);key=spec['token'];need(key not in seen,'Duplicate parameter target');seen.add(key)
        v=value(spec,v);scope=t['scope'];rows=changed.setdefault(scope,_records(result,scope));row=rows[t['owner']]
        if t['component']>=0:
            vector=list(_get(row,t['path']));vector[t['component']]=v;_put(row,t['path'],vector)
        else:_put(row,t['path'],v)
    req=result['request']
    for scope,rows in changed.items():
        if scope=='BRUSH':
            prior=B.records(result,source_bindings.recover(result)['actuals'])
            prior.update({owner:B.checked(row) for owner,row in rows.items() if any(t['scope']==scope and t['owner']==owner for t,v in overrides)})
            req['brush_effects']=dict(schema=B.SCHEMA,brushes=prior)
        elif scope in ('LINE','MERGE'):
            ext=H.records(result);ext['lines' if scope=='LINE' else 'merge_groups']=list(rows.values());req['line_hierarchy']=ext;H.records(result)
        else:
            ext=LF.records(result);ext[{'LF':'containers','MASK':'masks','SELF':'self_intersections'}[scope]]=list(rows.values());req['line_functions']=ext;LF.records(result)
    return result
def targets(document,*,scope=None,owner=None,fallback_owner=False):
    """Enumerate validated numeric leaves once, optionally for an exact owner.

    Every source table is local to this call. A missing/inactive/empty selected
    owner may fall back to its scope without recovering the document again.
    """
    need(scope is None or scope in SCOPES,'Unknown parameter scope')
    need(owner is None or type(owner) is str,'Invalid parameter owner filter')
    need(type(fallback_owner) is bool,'Invalid parameter owner fallback')
    need(scope is not None or owner is None,'Owner filter requires a parameter scope')
    def walk(row,path=()):
        for k,v in row.items():
            p=path+(k,)
            if type(v) is dict:yield from walk(v,p)
            elif type(v) in (bool,int,float):yield p,-1
            elif type(v) is list and len(v) in (2,3) and all(type(x) in (float,int) for x in v):
                for c in range(len(v)):yield p,c
    scopes=SCOPES if scope is None else (scope,)
    bindings=source_bindings.recover(document) if 'BRUSH' in scopes else None
    from . import group_lifecycle
    archived=set(group_lifecycle.records(document)['archived'])
    groups={}
    if bindings is not None:
        for route in bindings['routes']:groups.setdefault(route['actual_brush_id'],set()).add(route['group_id'])
    brush_defaults=B.defaults() if bindings is not None else None
    def descriptors(current_scope,current_owner,row):
        if not _active_owner(current_scope,current_owner,archived,groups.get(current_owner,set())):return
        for path,component in walk(row):
            t=target(current_scope,current_owner,path,component)
            if _allowed(current_scope,t['path']):yield _descriptor(row,t,brush_defaults=brush_defaults)
    for current_scope in scopes:
        rows=_records(document,current_scope,bindings=bindings)
        if owner is not None:
            selected=list(descriptors(current_scope,owner,rows[owner])) if owner in rows else []
            if selected or not fallback_owner:
                yield from selected
                continue
        for current_owner,row in rows.items():
            yield from descriptors(current_scope,current_owner,row)
