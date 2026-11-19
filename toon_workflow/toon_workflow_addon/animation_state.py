"""Pure validation of bound native parameter-animation evidence.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from copy import deepcopy
import math
from . import contract, materials, animation_contract

STATE_SCHEMA = 'toon-shading-state-v1'
NATIVE_SCHEMA = 'toon-shading-animation-v1'
SAMPLE_SCHEMA = 'toon-shading-animation-sample-v1'
TOLERANCE = 1e-6

def fail(text):
    raise contract.WorkflowError('CONFLICT', text, '$.shading_animation')

def _exact(value, fields, text):
    if type(value) is not dict or set(value) != set(fields):fail(text)

def _number(value):
    return type(value) in (int, float) and math.isfinite(value)

def _uuid(value):
    contract.validate(value, contract.UUID_SCHEMA, '$.shading_animation.uuid')

def _digest(value):
    if value.get('sha256') != contract.signature({k:v for k,v in value.items() if k!='sha256'}):
        fail('Native animation canonical witness digest differs')

def validate_sample(value, frame, manifest, native=None):
    _exact(value, {'schema','scope_id','frame','scene','scene_frame','rows','settings','tolerance','passed'},
           'Exact shading sample fields required')
    if (value['schema']!=SAMPLE_SCHEMA or value['scope_id']!=manifest['scope_id']
            or not _number(value['frame']) or value['frame']!=frame
            or value['scene']!=manifest['scene'] or value['scene_frame']!=frame
            or value['tolerance']!=TOLERANCE or value['passed'] is not True
            or type(value['rows']) is not list or type(value['settings']) is not list):
        fail('Shading sample identity, time or tolerance differs')
    tracks=manifest['animations'];assignments=manifest['shading']['assignments']
    templates={t['template_id']:t['settings'] for t in manifest['shading']['templates']}
    by_region={r['region_id']:r for r in assignments}
    settings={};order=[]
    for row in value['settings']:
        _exact(row,{'material_id','template_id','settings'},'Exact actual material settings required')
        mid,tid=row['material_id'],row['template_id'];_uuid(mid)
        if mid in settings or tid not in templates:fail('Duplicate or unknown shading settings identity')
        if row['settings'].get('mode')!=templates[tid]['mode']:fail('Actual shading mode differs')
        materials._settings(row['settings'],manifest['engine'])
        settings[mid]=row;order.append(mid)
    if order!=sorted(order) or len(order)!=len(assignments):fail('Actual settings material domain differs')
    for tid in templates:
        if sum(r['template_id']==tid for r in value['settings'])!=sum(r['template_id']==tid for r in assignments):
            fail('Actual settings template consumer count differs')
    if native is not None:
        bindings=native['bindings'];native_map={b['material_id']:b for b in bindings}
        if order!=[b['material_id'] for b in bindings]:fail('Sample material IDs differ from native ownership')
        for mid,row in settings.items():
            if row['template_id']!=native_map[mid]['template_id']:fail('Sample template differs from native binding')
    else:native_map={}
    expected_count=sum(sum(t['template_id']==r['template_id'] for t in tracks) for r in assignments)
    if len(value['rows'])!=expected_count:fail('Tracked sample row count differs')
    seen=set();actual_order=[]
    for row in value['rows']:
        _exact(row,{'material_id','region_id','template_id','object_id','slot_index','field','socket_name',
                    'path','action','actual','expected','errors','error'},'Exact tracked sample row required')
        mid=row['material_id'];region=by_region.get(row['region_id'])
        if (mid not in settings or region is None
                or any(row[k]!=region[k] for k in ('template_id','object_id','slot_index'))
                or settings[mid]['template_id']!=row['template_id']):fail('Sample region binding differs')
        selector=(row['template_id'],row['field'])
        selected=[t for t in tracks if (t['template_id'],t['field'])==selector]
        if len(selected)!=1 or (mid,row['field']) in seen:fail('Unknown or duplicate tracked sample selector')
        seen.add((mid,row['field']));actual_order.append((mid,tracks.index(selected[0])))
        expected=animation_contract.evaluate_track(selected[0],frame)
        actual=row['actual'];native_setting=settings[mid]['settings'][row['field']]
        if actual!=native_setting or row['expected']!=expected:fail('Sample actual/expected values are not independently bound')
        av=actual if isinstance(actual,list) else [actual];ev=expected if isinstance(expected,list) else [expected]
        if len(av)!=len(ev) or any(not _number(v) for v in av):fail('Invalid actual shading value shape')
        errors=[abs(a-b) for a,b in zip(av,ev)];maximum=max(errors)
        if (row['errors']!=errors or row['error']!=maximum or maximum>TOLERANCE
                or row['socket_name']!=materials._sockets(templates[row['template_id']]['mode'])[row['field']]
                or not isinstance(row['path'],str) or not row['path']):fail('Tracked sample path/error evidence differs')
        if native is not None:
            binding=native_map[mid];action=binding['action']
            if (binding['region_id']!=row['region_id'] or action is None or row['action']!={
                    'name':action['name'],'uuid':action['uuid'],'slot':binding['slot']['identifier']}):
                fail('Sample Action identity differs from native witness')
    if actual_order!=sorted(actual_order):fail('Tracked sample ordering differs')
    if native is not None:
        for binding in native['bindings']:
            for track in tracks:
                if track['template_id']==binding['template_id'] and (binding['material_id'],track['field']) not in seen:
                    fail('Native binding lacks its tracked sample')
    return value

def validate_native(native, manifest, reference):
    _exact(native,{'schema','scope_id','frame_start','frame_end','tracks','bindings'},'Exact native animation witness required')
    if (native['schema']!=NATIVE_SCHEMA or native['scope_id']!=manifest['scope_id']
            or native['frame_start']!=manifest['time']['start'] or native['frame_end']!=manifest['time']['end']
            or native['tracks']!=manifest['animations'] or type(native['bindings']) is not list):
        fail('Native witness declaration/domain differs')
    expected={r['region_id']:r for r in manifest['shading']['assignments']};seen_regions=set();mids=[];action_ids=set()
    for binding in native['bindings']:
        if type(binding) is not dict or not {'material_id','material_name','region_id','template_id','object_id',
                'slot_index','object_name','mesh_name','link','node_tree_name','action_uuid','slot','tracks',
                'action','curves','static','sha256'}==set(binding):fail('Native binding fields differ')
        _digest(binding);mid=binding['material_id'];_uuid(mid);mids.append(mid)
        region=expected.get(binding['region_id'])
        if region is None or any(binding[k]!=region[k] for k in region):fail('Native region assignment differs')
        seen_regions.add(binding['region_id']);tracks=[t for t in manifest['animations'] if t['template_id']==binding['template_id']]
        if binding['tracks']!=tracks:fail('Native binding track selectors differ')
        action=binding['action']
        if binding['static']['node_tree']!={'name':binding['node_tree_name'],'library':None,
                'is_embedded_data':True,'owner_material_id':mid,'owner_material_name':binding['material_name']}:
            fail('Embedded material NodeTree ownership witness differs')
        if not tracks:
            if action is not None or binding['action_uuid'] is not None or binding['slot'] is not None or binding['curves']!=[]:
                fail('Unanimated region gained owned animation')
            continue
        if type(action) is not dict:fail('Animated region lacks complete Action witness')
        _digest(action);_uuid(action['uuid'])
        if action['uuid'] in action_ids:fail('Shared or duplicate owned Action UUID')
        action_ids.add(action['uuid'])
        if (binding['action_uuid']!=action['uuid'] or action['schema']!=NATIVE_SCHEMA
                or action['tags']!={'scope_id':manifest['scope_id'],'role':'shading_action','material_id':mid}
                or action['users']!=1 or action['use_fake_user'] is not False or action['use_cyclic'] is not False
                or action['use_frame_range'] is not False or action['slot_count']!=1
                or action['slots']!=[binding['slot']] or action['slot']!=binding['slot']
                or binding['slot']['target_id_type']!='NODETREE' or action['curves']!=binding['curves']
                or action['curve_count']!=len(binding['curves'])
                or action['consumers']!=[{'type':'Material','name':binding['material_name'],'library':None}]
                or action['node_tree']!={'name':binding['node_tree_name'],'library':None}):
            fail('Native Action ownership/slot/consumer proof differs')
        ad=action['animation_data']
        if (ad['exists'] is not True or ad['action']!=action['name']
                or ad['action_slot']!=binding['slot']['identifier'] or ad['drivers']!=0 or ad['nla_tracks']!=0
                or ad['action_extrapolation']!='HOLD' or ad['action_blend_type']!='REPLACE'
                or ad['action_influence']!=1):fail('Native Action evaluation context differs')
        layers=action['layers']
        if (type(layers) is not list or len(layers)!=1 or len(layers[0]['strips'])!=1
                or len(layers[0]['strips'][0]['channelbags'])!=1):fail('Native Action layer/strip domain differs')
        bag=layers[0]['strips'][0]['channelbags'][0]
        if bag!=action['channelbag'] or bag['curves']!=binding['curves'] or bag['groups']!=[] or action['groups']!=[]:
            fail('Native channelbag/flat curve witness is contradictory')
        slot=binding['slot'];handle=slot['handle']
        if type(handle) is not int or not slot['handle_available'] or any(v!=handle for v in slot['handle_sources'].values()):
            fail('Native Action slot handle proof differs')
        paths={r['field']:r['path'] for r in reference['rows'] if r['material_id']==mid}
        specs={}
        for track in tracks:
            if track['field'] not in paths:fail('Reference does not bind tracked native path')
            for channel in range(4 if track['field'].endswith('_color') else 1):
                specs[(paths[track['field']],channel)]=(track,channel)
        curves={(c['data_path'],c['array_index']):c for c in binding['curves']}
        if len(curves)!=len(binding['curves']) or set(curves)!=set(specs):fail('Native curve paths/channels differ')
        for key,curve in curves.items():
            track,channel=specs[key]
            if (curve['modifiers']!=[] or curve['group'] is not None or curve['mute'] is not False
                    or curve['extrapolation']!='CONSTANT' or len(curve['keyframe_points'])!=len(track['keys'])):
                fail('Native curve structure differs')
            for point,declared in zip(curve['keyframe_points'],track['keys']):
                expected_value=declared['value'][channel] if isinstance(declared['value'],list) else declared['value']
                if (point['co'][0]!=declared['frame'] or abs(point['co'][1]-expected_value)>TOLERANCE
                        or point['interpolation']!=track['interpolation']):fail('Native keyframe values/interpolation differ')
    if mids!=sorted(mids) or len(set(mids))!=len(mids) or len(mids)!=len(expected) or seen_regions!=set(expected):
        fail('Native material/region ownership domain differs')
    validate_sample(reference,reference['frame'],manifest,native)
    return native

def validate_managed_binding(value, manifest, snapshot, frames):
    """Bind independent native/sample witnesses to the actual regional snapshot."""
    managed={r['material_id']:r for r in snapshot['managed']['materials']}
    regional={r['region_id']:r for r in snapshot['regions']}
    targets={r['object_id']:r for r in snapshot['targets']}
    native=value['native'];bindings={b['material_id']:b for b in native['bindings']}
    if set(bindings)!=set(managed):fail('Native material IDs differ from managed regional materials')
    for b in native['bindings']:
        row=managed[b['material_id']];region=regional.get(b['region_id']);target=targets.get(b['object_id'])
        if region is None or target is None or region['material_id']!=b['material_id'] or row['name']!=b['material_name']:
            fail('Native region/material identity differs from managed snapshot')
        binding={'object':target['object_name'],'mesh':target['mesh_name'],'slot':region['slot_index'],'link':region['original_slot']['link']}
        selected=[slot for slot in target['slots'] if slot['slot_index']==region['slot_index']]
        material_identity={'type':'Material','name':row['name'],'library':None}
        if (len(selected)!=1 or selected[0]['link']!=b['link']
                or selected[0]['material']!=material_identity
                or selected[0]['data_material']!=(material_identity if b['link']=='DATA' else region['original_slot']['data_material'])):
            fail('Actual target material slot contradicts managed native ownership')
        if (b['object_name']!=binding['object'] or b['mesh_name']!=binding['mesh'] or b['link']!=binding['link']
                or row['bindings']!=[binding]):fail('Native actual source slot binding differs')
        static=b['static'];_digest(static)
        if (static['material']['id']!=b['material_id'] or static['material']['name']!=row['name']
                or static['material']['engine']!=row['engine'] or static['material']['library'] is not None
                or static['object_binding']!=binding
                or any(static[k]!=b[k] for k in ('object_id','region_id','template_id'))):
            fail('Native static ownership differs from managed regional snapshot')
    for sample in [value['reference'],*[f['shading_sample'] for f in frames]]:
        if {r['material_id'] for r in sample['settings']}!=set(managed):fail('Sample material coverage differs from managed snapshot')
        for row in sample['settings']:
            mid=row['material_id'];b=bindings[mid]
            tracked={t['field'] for t in manifest['animations'] if t['template_id']==b['template_id']}
            if row['template_id']!=b['template_id']:fail('Sample template differs from managed region')
            if set(row['settings'])!=set(managed[mid]['settings']):fail('Sample settings fields differ from managed snapshot')
            for field,actual in row['settings'].items():
                if field not in tracked and actual!=managed[mid]['settings'][field]:
                    fail('Untracked sample setting differs from managed reference snapshot')
            if sample is value['reference'] and row['settings']!=managed[mid]['settings']:
                fail('Reference actual settings differ from managed reference snapshot')
    return True


def validate_state(value, manifest, reference_frame, frames, materials_snapshot=None):
    _exact(value,{'schema','native','reference'},'Exact shading animation state required')
    if value['schema']!=STATE_SCHEMA:fail('Unknown shading animation state version')
    validate_sample(value['reference'],reference_frame,manifest)
    validate_native(value['native'],manifest,value['reference'])
    for row in frames:validate_sample(row['shading_sample'],row['frame'],manifest,value['native'])
    if materials_snapshot is not None:validate_managed_binding(value,manifest,materials_snapshot,frames)
    return value

def validate_effective(manifest, snapshot, plans=()):
    """Preflight each region after static three-way merging, before native writes."""
    changed={p['material_id']:p['changes'] for p in plans}
    current={m['material_id']:m['settings'] for m in snapshot['managed']['materials']}
    for region in snapshot['regions']:
        effective={**deepcopy(current[region['material_id']]),**deepcopy(changed.get(region['material_id'],{}))}
        # Current tracked sockets contain the old reference-time evaluation,
        # not retained static values. Seed those fields from the new first key
        # before validating the complete continuous domain below.
        for track in manifest['animations']:
            if track['template_id']==region['template_id']:
                effective[track['field']]=animation_contract.evaluate_track(track,manifest['time']['start'])
        shading=deepcopy(manifest['shading'])
        for template in shading['templates']:
            if template['template_id']==region['template_id']:template['settings']=effective
        animation_contract.normalize_tracks(manifest['animations'],shading,manifest['time']['start'],manifest['time']['end'],manifest['engine'])
    return True
