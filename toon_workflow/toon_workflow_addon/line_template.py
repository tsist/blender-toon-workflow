# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete static Pencil styles with explicit semantic reference bindings.

v1 preserves the historical native style and append flow. v2 adds explicit,
ordered AUTHOR_RGB/NODE_RGBA/UV_IMAGE dependencies and is replacement-only.
"""
import copy
import hashlib
import json
import math
import uuid
from pathlib import Path

SCHEMA='toon-line-template.v1'
SCHEMA_V2='toon-line-template.v2'
KINDS=('objects','materials','collections')
MAX_BYTES=16*1024*1024
MATERIAL_KEY='toon_line_material_colors'


def _need(value,message):
    if not value:raise ValueError(message)


def _native():
    from .backend import pencil
    return pencil('file_authoring')


def _state():
    from . import cel_lines
    return cel_lines


def _ink():
    from . import line_template_ink
    return line_template_ink


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _fields(value,keys,label):
    _need(type(value) is dict and set(value)==set(keys),label+' fields changed')


def _ref(value):
    return dict(name=value.name_full,library=value.library.filepath if value.library else None)


def _key(ref):
    from .backend import pencil
    return pencil('selector_bindings').key(ref)


def _material_policies(style):
    """Normalized native material policies plus their exact request payload."""
    from . import line_material_colors
    request=style['document']['request'];payload=request.get(MATERIAL_KEY)
    if payload is None:return None,[]
    groups=[row['id'] for row in request['boundary']['groups']]
    payload=line_material_colors.normalize(payload,groups)
    return payload,payload['policies']


def _plan(style):
    return dict(raw=style['document'],groups=style['document']['request']['boundary']['groups'])


def _uid(value):
    value=getattr(value,'session_uid',None)
    return value if type(value) is int else None


def _reference_rows(style,ink_sources=None):
    """Only paths supported by the native repair_names contract are portable."""
    request=style['document']['request'];result=[]
    if ink_sources is None:
        _need(not request.get(MATERIAL_KEY,{}).get('policies'),
              'Material ink colors require explicit material/node transport; retain the complete author blend')
    else:
        _,policies=_material_policies(style)
        for i,row in enumerate(policies):
            ref=row['material']
            _need(type(ref) is dict and ref.get('library') is None,
                  'Material ink references must be local: '+str(ref.get('name')))
            result.append(('materials',ref,f'/document/request/{MATERIAL_KEY}/policies/{i}/material'))
        for i,row in enumerate(ink_sources['policies']):
            if row['kind']!='UV_IMAGE':continue
            for j,ref in enumerate(row['consumers']):
                _need(ref['library'] is None,'UV ink consumer references must be local: '+ref['name'])
                result.append(('objects',ref,f'/ink_sources/policies/{i}/consumers/{j}'))
    for i,row in enumerate(request.get('selector_bindings',{}).get('groups',[])):
        for kind in KINDS:
            for j,ref in enumerate(row[kind]):result.append((kind,ref,f'/document/request/selector_bindings/groups/{i}/{kind}/{j}'))
    for i,row in enumerate(request.get('line_functions',{}).get('containers',[])):
        for field in ('materials','draw_materials','mask_materials','draw_objects','mask_objects'):
            for j,ref in enumerate(row.get(field,[])):
                result.append(('objects' if field.endswith('objects') else 'materials',ref,f'/document/request/line_functions/containers/{i}/{field}/{j}'))
    for i,row in enumerate(request.get('line_hierarchy',{}).get('merge_domains',[])):
        for j,ref in enumerate(row['collections']):result.append(('collections',ref,f'/document/request/line_hierarchy/merge_domains/{i}/collections/{j}'))
    allowed={path for _,_,path in result}
    def walk(value,path=''):
        if type(value) is dict:
            for key in ('object_ids','material_names'):
                _need(not value.get(key),'Legacy name lists require explicit migration: '+path+'/'+key)
            _need(not value.get('refer_object_on'),'Object-reference reduction requires explicit migration: '+path)
            _need(not value.get('seam_tokens') and value.get('seam_anchor') is None,'Topology seam anchors require explicit migration: '+path)
            if set(value)=={'name','library'}:
                _need(path in allowed,'Unmapped native reference: '+path)
            for key,item in value.items():walk(item,path+'/'+str(key))
        elif type(value) is list:
            for i,item in enumerate(value):walk(item,path+'/'+str(i))
    walk(style)
    return result


def _inventory(style,ink_sources=None):
    result={kind:{} for kind in KINDS}
    for kind,ref,_ in _reference_rows(style,ink_sources):
        _key(ref);prior=result[kind].get(ref['name'])
        _need(prior is None or prior==ref,'Ambiguous same-name library reference: '+ref['name'])
        result[kind][ref['name']]=copy.deepcopy(ref)
    return {kind:sorted(rows.values(),key=lambda r:(r['name'],r['library'] or '')) for kind,rows in result.items()}


def normalize(document):
    schema=document.get('schema') if type(document) is dict else None
    if schema==SCHEMA_V2:
        _fields(document,('schema','template_id','name','revision','style','roles','ink_sources'),'Template')
    else:
        _fields(document,('schema','template_id','name','revision','style','roles'),'Template')
        _need(document['schema']==SCHEMA,'Unsupported line template schema')
    try:ident=uuid.UUID(document['template_id'])
    except Exception as error:raise ValueError('Template identity must be a UUID') from error
    _need(ident.int and str(ident)==document['template_id'],'Template identity must be canonical')
    _need(type(document['name']) is str and 1<=len(document['name'])<=128 and not any(ord(c)<32 for c in document['name']),'Invalid template name')
    _need(type(document['revision']) is int and document['revision']>0,'Invalid template revision')
    style=copy.deepcopy(document['style']);_fields(style,('schema','document','recipe','scene_controls'),'Portable style')
    _need(style['schema']==_native().SCHEMA,'Unsupported native portable style')
    style=_native().checked(style)
    _need(style['document']['inputs'].get('autonomous_origin')=='toon.autonomous-style.v1','Only autonomous static styles are supported')
    _need(style['document']['inputs'].get('full_preset') is None,'Captured native payload requires separate conversion')
    _need(not style['recipe']['object_bindings'] and not style['recipe']['material_bindings'],'Logical source bindings require a separate migration contract')
    gids={r['id'] for r in style['document']['request']['boundary']['groups']}
    selectors=style['document']['request'].get('selector_bindings',{}).get('groups',[])
    _need(len(selectors)==len(gids) and {r['group'] for r in selectors}==gids,'Every line group needs one explicit selector record')
    controls=style['scene_controls'];_fields(controls,('settings','groups','elements','chain_controls','map_amounts','dash_patterns'),'Portable controls')
    from .backend import pencil
    recovered=pencil('source_bindings').recover(style['document'])
    for key,module,function in [('chain_controls','chain_controls','normalize_controls'),('map_amounts','map_amount_controls','normalize'),('dash_patterns','dash_controls','normalize')]:
        _need(controls[key] is not None,'Portable controls must explicitly carry '+key)
        getattr(pencil(module),function)(recovered,controls[key])
    ink_sources=None
    if schema==SCHEMA_V2:
        _,policies=_material_policies(style)
        ink_sources=_ink().normalize_sources(document['ink_sources'],policies)
    inventory=_inventory(style,ink_sources);roles=copy.deepcopy(document['roles']);_fields(roles,KINDS,'Role table')
    for kind in KINDS:
        _need(type(roles[kind]) is list and len(roles[kind])<=4096,'Invalid role list')
        seen=set();refs=[]
        for row in roles[kind]:
            _fields(row,('role','source'),'Role')
            role=row['role'];_need(type(role) is str and 1<=len(role)<=128 and not any(ord(c)<32 for c in role),'Invalid role name')
            _need(role not in seen,'Duplicate role: '+role);seen.add(role)
            _key(row['source']);refs.append(row['source'])
        _need(sorted(refs,key=lambda r:(r['name'],r['library'] or ''))==inventory[kind],kind+' role references must exactly cover the native style')
    if schema==SCHEMA_V2:
        result=dict(document,schema=SCHEMA_V2,style=style,roles=roles,ink_sources=ink_sources)
    else:
        result=dict(document,schema=SCHEMA,style=style,roles=roles)
    _need(len(_json(result).encode('utf8'))<=MAX_BYTES,'Line template exceeds 16 MiB')
    return result


def _static(scene):
    C=_state();settings=C._validate_scene(scene)
    _need(not C._pencil_animation_paths(scene),'Pencil Action/NLA/driver requires explicit preservation')
    _need(not settings.amount_tracks and not settings.parameter_tracks,'Pencil animation tracks are not portable static state')
    author=getattr(scene,'toon_author_files',None)
    _need(settings.curve_tree is None and (author is None or author.author_tree is None),'Commit and close pending native editor trees before using a static template')
    return settings


def _capture_ink_sources(scene,style,policies):
    from . import line_material_colors
    import bpy
    resolved=line_material_colors.resolve(scene,_plan(style))
    _need(resolved is not None and len(resolved['policies'])==len(policies),
          'Resolved material ink policies do not match the captured style')
    line_material_colors.validate_receipt(resolved)
    rows=[]
    for policy,actual in zip(policies,resolved['policies']):
        _need(actual['policy']['id']==policy['id'],'Material ink policy order changed during capture')
        kind=policy['source']['kind'];row=dict(policy_id=policy['id'],kind=kind)
        if kind=='NODE_RGBA':
            row['source_descriptor']=_ink().normalize_source_descriptor(
                actual['source_descriptor'],direction=policy['source']['direction'])
            row['source_rgb']=list(actual['source_rgb'])
        elif kind=='UV_IMAGE':
            row['image']=_ink().normalize_image_descriptor(actual['source_descriptor'])
            material=bpy.data.materials.get(actual['policy']['material']['name'])
            _need(material is not None and not material.library and not material.override_library,
                  'Captured UV material is missing or nonlocal')
            consumers=[_ref(obj) for obj in _ink().consumer_objects(scene,material)]
            _need(bool(consumers),'Captured UV material has no actual Scene Mesh consumers')
            row['consumers']=consumers
        elif kind!='AUTHOR_RGB':
            _need(False,'Unsupported material ink source kind')
        rows.append(row)
    return dict(schema=_ink().SOURCE_SCHEMA,policies=rows)


def capture(scene,name='描边模板'):
    _static(scene)
    style=_native().checked(_native().read_scene(scene));style['schema']=_native().SCHEMA
    from .backend import pencil
    recovered=pencil('source_bindings').recover(style['document'])
    for key,module,function in [('chain_controls','chain_controls','seed_controls'),('map_amounts','map_amount_controls','defaults'),('dash_patterns','dash_controls','defaults')]:
        if style['scene_controls'][key] is None:style['scene_controls'][key]=getattr(pencil(module),function)(recovered)
    _map_dependencies(scene,style)
    raw=style['document']['request'].get(MATERIAL_KEY)
    if type(raw) is dict and raw.get('policies'):
        _,policies=_material_policies(style)
        ink_sources=_capture_ink_sources(scene,style,policies)
        inventory=_inventory(style,ink_sources)
        return normalize(dict(schema=SCHEMA_V2,template_id=str(uuid.uuid4()),name=name,revision=1,
            style=style,ink_sources=ink_sources,
            roles={kind:[dict(role=r['name'],source=r) for r in refs] for kind,refs in inventory.items()}))
    inventory=_inventory(style)
    doc=normalize(dict(schema=SCHEMA,template_id=str(uuid.uuid4()),name=name,revision=1,style=style,
        roles={kind:[dict(role=r['name'],source=r) for r in refs] for kind,refs in inventory.items()}))
    return doc


def export_document(document,path):
    doc=normalize(document);path=Path(path)
    _need(path.is_absolute() and path.suffix.lower()=='.json','Use an absolute JSON path')
    data=json.dumps(doc,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    with path.open('x',encoding='utf8',newline='\n') as stream:stream.write(data)
    return dict(status='exported',file=str(path),sha256=hashlib.sha256(data.encode('utf8')).hexdigest())


def load_document(path):
    path=Path(path);_need(path.is_absolute() and path.is_file() and path.stat().st_size<=MAX_BYTES,'Use a line template JSON up to 16 MiB')
    def pairs(rows):
        result={}
        for key,value in rows:
            _need(key not in result,'Duplicate JSON key: '+key);result[key]=value
        return result
    def constant(value):raise ValueError('Nonfinite JSON number: '+value)
    return normalize(json.loads(path.read_text('utf-8-sig'),object_pairs_hook=pairs,parse_constant=constant))


def _resolve(scene,doc,bindings):
    import bpy
    _fields(bindings,KINDS,'Bindings');maps={kind:{} for kind in KINDS};targets={kind:{} for kind in KINDS}
    collections={scene.collection,*scene.collection.children_recursive}
    materials={s.material for o in scene.objects for s in o.material_slots if s.material}
    for kind in KINDS:
        expected={r['role'] for r in doc['roles'][kind]}
        _need(type(bindings[kind]) is dict and set(bindings[kind])==expected,kind+' roles must all be explicitly bound')
        seen=set()
        for row in doc['roles'][kind]:
            value=bindings[kind][row['role']]
            valid=(isinstance(value,bpy.types.Object) and scene.objects.get(value.name)==value and value.type=='MESH') if kind=='objects' else (isinstance(value,bpy.types.Material) and value in materials) if kind=='materials' else (isinstance(value,bpy.types.Collection) and value in collections)
            _need(valid and not value.library and not value.override_library,'Choose a local Scene '+kind+' target: '+row['role'])
            _need(value.as_pointer() not in seen,'Distinct source roles require distinct targets: '+row['role']);seen.add(value.as_pointer())
            maps[kind][row['source']['name']]=_ref(value);targets[kind][row['role']]=value
    return maps,targets


def _map_dependencies(scene,style):
    from .backend import pencil
    bindings=pencil('source_bindings').recover(style['document']);req=style['document']['request']
    selections={r['group']:r for r in req.get('selector_bindings',{}).get('groups',[])}
    report=[]
    for actual,brush in req.get('brush_effects',{}).get('brushes',{}).items():
        gids={r['group_id'] for r in bindings['routes'] if r['actual_brush_id']==actual}
        for slot,m in brush['maps'].items():
            if not m['enabled']:continue
            _need(m['source_type']=='OBJECT_COLOR','Enabled image maps require an explicit image/UV transport contract')
            _need(m['object_color_selection_mode']=='NAME' and bool(m['object_color_name']),'Use named object-color layers for portable maps')
            objects=set()
            for gid in gids:
                selection=selections[gid]
                for ref in selection['objects']:objects.add(scene.objects[ref['name']])
                for ref in selection['collections']:
                    col=next(c for c in [scene.collection,*scene.collection.children_recursive] if c.name==ref['name']);objects.update(o for o in col.all_objects if o.name in scene.objects)
                mats={r['name'] for r in selection['materials']}
                objects.update(o for o in scene.objects if any(s.material and s.material.name in mats for s in o.material_slots))
            _need(bool(objects),'Object-color map has no explicit supporting objects')
            for obj in objects:
                _need(obj.type=='MESH','Object-color map needs Mesh targets')
                layer=obj.data.color_attributes.get(m['object_color_name'])
                _need(layer is not None and layer.domain in ('POINT','CORNER') and layer.data_type in ('FLOAT_COLOR','BYTE_COLOR'),'Required color layer missing or unsupported on '+obj.name+': '+m['object_color_name'])
                _need(all(math.isfinite(v) for x in layer.data for v in x.color),'Nonfinite color attribute on '+obj.name)
                report.append(dict(object=obj.name,layer=layer.name,domain=layer.domain,type=layer.data_type,actual=actual,map=slot,checked_on='original Mesh; evaluated RENDER support requires joint run'))
    # Legacy enabled brush maps also need an explicit named layer. Their
    # portable semantics differ from brush_effects and need separate support.
    for group in style['document']['inputs']['projection']['editor_document']['groups']:
        for brush in group['brushes'].values():
            for key in ('width_map','alpha_map'):
                _need(not (brush.get(key) or {}).get('enabled'),'Legacy enabled maps require explicit layer migration')
    return report


def _bind_target_images(ink_bindings,policies):
    import bpy
    for policy in policies:
        if policy['source']['kind']!='UV_IMAGE':continue
        image=ink_bindings['images'][policy['id']]
        _need(isinstance(image,bpy.types.Image) and bpy.data.images.get(image.name)==image
              and not image.library and not image.override_library,
              'Bind a local Image datablock for UV policy '+policy['id'])
    return ink_bindings


def _targets_by_ref(targets):
    return {kind:{_key(_ref(value)):value for value in rows.values()} for kind,rows in targets.items()}


def _role_refs(doc,targets):
    roles={}
    for kind in KINDS:
        source={row['role']:row['source'] for row in doc['roles'][kind]}
        roles[kind]=[dict(role=role,source=copy.deepcopy(source[role]),target=_ref(targets[kind][role]),
            target_uid=_uid(targets[kind][role])) for role in sorted(targets[kind])]
    return roles


def _binding_observation(ink_bindings,policies):
    image_names=_ink().target_image_names(ink_bindings,policies)
    images={}
    for policy in policies:
        if policy['source']['kind']!='UV_IMAGE':continue
        image=ink_bindings['images'][policy['id']]
        images[policy['id']]=dict(name=image.name,uid=_uid(image),fake_user=bool(image.use_fake_user),
            source=getattr(image,'source',None))
    return dict(nodes={key:copy.deepcopy(value) for key,value in ink_bindings['nodes'].items()},
        images=images,uv_names={key:value for key,value in ink_bindings['uv_names'].items()},
        uv_transforms={key:list(value) for key,value in ink_bindings['uv_transforms'].items()},
        target_image_names=image_names)


def _consumer_observation(scene,policy,material,uv_name):
    consumers=[]
    for obj in _ink().consumer_objects(scene,material):
        uv=_ink().uv_witness(obj,uv_name)
        consumers.append(dict(object=_ref(obj),uv=uv))
    _need(bool(consumers),'UV target material has no actual Scene Mesh consumers: '+policy['id'])
    return consumers


def _verify_ink(scene,doc,style,maps,targets,ink_bindings,policies,resolved):
    from . import line_material_colors
    if not policies:
        _need(_ink().normalize_bindings(ink_bindings,policies) is ink_bindings,
              'Empty ink bindings changed')
        _need(resolved is None or (type(resolved) is dict
              and type(resolved.get('policies')) is list and not resolved['policies']),
              'Unexpected resolved material ink policies')
        return dict(policies=[])
    _need(resolved is not None,'Resolved material ink policies are missing')
    line_material_colors.validate_receipt(resolved)
    actual_rows={row['policy']['id']:row for row in resolved['policies']}
    _need(len(actual_rows)==len(policies),'Resolved material ink policy count differs')
    by_ref=_targets_by_ref(targets);image_names=_ink().target_image_names(ink_bindings,policies)
    observations=[]
    for policy,source in zip(policies,doc['ink_sources']['policies']):
        pid=policy['id'];kind=policy['source']['kind'];actual=actual_rows.get(pid)
        _need(actual is not None and actual['policy']['id']==pid
              and actual['policy']['source']['kind']==kind,'Resolved material ink identity differs: '+pid)
        source_ref=policy['material'];target_ref=maps['materials'].get(source_ref['name'])
        _need(target_ref is not None and actual['policy']['material']==target_ref,
              'Resolved material target differs from the explicit role binding: '+pid)
        material=by_ref['materials'].get(_key(target_ref))
        _need(material is not None,'Resolved material target is not the bound Scene material')
        row=dict(policy_id=pid,kind=kind,material=copy.deepcopy(target_ref),
            source=copy.deepcopy(source),resolved=copy.deepcopy(actual))
        if kind=='AUTHOR_RGB':
            _need(actual['source_descriptor']==dict(kind='AUTHOR_RGB')
                  and actual['source_rgb']==policy['source']['color']
                  and actual['target_rgb'] is not None,'Resolved AUTHOR_RGB source differs')
        elif kind=='NODE_RGBA':
            binding=ink_bindings['nodes'][pid]
            _need(actual['policy']['source']['node']==binding['node']
                  and actual['policy']['source']['direction']==binding['direction']
                  and actual['policy']['source']['socket']==binding['socket'],
                  'Resolved NODE_RGBA target differs from its explicit binding: '+pid)
            _ink().normalize_source_descriptor(actual['source_descriptor'],direction=binding['direction'])
            _need(actual['source_rgb'] is not None and actual['target_rgb'] is not None,
                  'Resolved NODE_RGBA source RGB is missing')
        elif kind=='UV_IMAGE':
            target_name=image_names[pid];binding=ink_bindings
            _need(actual['source_rgb'] is None and actual['target_rgb'] is None,
                  'UV_IMAGE must retain per-fragment color semantics')
            _need(actual['source_descriptor']['image']==target_name
                  and _ink().image_descriptor_matches(source['image'],actual['source_descriptor']),
                  'UV target image descriptor differs from the source except for image name: '+pid)
            _need(actual['policy']['source']['image']==target_name
                  and actual['policy']['source']['uv_name']==binding['uv_names'][pid]
                  and actual['policy']['source']['uv_transform']==list(binding['uv_transforms'][pid]),
                  'Resolved UV target differs from its explicit binding: '+pid)
            mapped=_ink().map_consumers(source,maps['objects'])
            actual_consumers=[_ref(obj) for obj in _ink().consumer_objects(scene,material)]
            _need(sorted(mapped,key=_ink().ref_key)==sorted(actual_consumers,key=_ink().ref_key),
                  'UV target consumers do not exactly cover the mapped source consumers: '+pid)
            uv_name=binding['uv_names'][pid]
            row['consumers']=_consumer_observation(scene,policy,material,uv_name)
        else:_need(False,'Unsupported material ink source kind')
        observations.append(row)
    return dict(policies=observations)


def _observation(scene,doc,maps,targets,ink_bindings,policies,resolved,verified):
    rows={row['policy_id']:row for row in verified['policies']}
    bindings=_binding_observation(ink_bindings,policies)
    cold_images=[]
    for policy,source in zip(policies,doc['ink_sources']['policies']):
        if policy['source']['kind']!='UV_IMAGE':continue
        verified_row=rows[policy['id']]
        cold_images.append(dict(policy_id=policy['id'],name=bindings['images'][policy['id']]['name'],
            uid=bindings['images'][policy['id']]['uid'],descriptor=copy.deepcopy(source['image']),
            target_descriptor=copy.deepcopy(verified_row['resolved']['source_descriptor']),
            consumers=copy.deepcopy(source['consumers']),
            target_consumers=copy.deepcopy(verified_row.get('consumers',[])),
            uv_name=bindings['uv_names'][policy['id']],
            uv_transform=copy.deepcopy(bindings['uv_transforms'][policy['id']])))
    data=dict(schema=_ink().OBSERVATION_SCHEMA,template_schema=SCHEMA_V2,
        template_id=doc['template_id'],revision=doc['revision'],
        document_sha256=hashlib.sha256(_json(doc).encode('utf8')).hexdigest(),
        ink_sources=copy.deepcopy(doc['ink_sources']),roles=_role_refs(doc,targets),
        bindings=bindings,policies=[rows[row['id']] for row in policies if row['id'] in rows],
        resolved=copy.deepcopy(resolved),
        cold_open=dict(images=cold_images,objects=[copy.deepcopy(r) for r in maps['objects'].values()],
            materials=[copy.deepcopy(r) for r in maps['materials'].values()],
            uv_names=dict(bindings['uv_names']),uv_transforms=copy.deepcopy(bindings['uv_transforms'])),
        authority='actual line_material_colors.resolve',
        render_validation='not performed; joint RENDER/numeric acceptance remains required')
    return _ink().sealed(data)


def _no_ink_observation(doc,targets):
    data=dict(schema=_ink().OBSERVATION_SCHEMA,template_schema=SCHEMA,
        template_id=doc['template_id'],revision=doc['revision'],
        document_sha256=hashlib.sha256(_json(doc).encode('utf8')).hexdigest(),
        ink_sources=None,roles=_role_refs(doc,targets),bindings=None,policies=[],resolved=None,
        cold_open=dict(images=[],objects=[],materials=[],uv_names={},uv_transforms={}),
        authority='not applicable; v1 carries no ink dependencies',
        render_validation='not performed')
    return _ink().sealed(data)


def ink_observation(scene,document,bindings,ink_bindings=None):
    """Return the live JSON-safe source snapshot for apply/preflight/UI.

    `uid`/`target_uid` are session identities used by runtime rollback checks,
    and `fake_user` is live datablock state. Persist the apply receipt's
    post-retention observation for cold-open comparison, then drop only UID
    fields and reseal. Preflight/inspect snapshots are pre-commit and may still
    show fake_user false.
    """
    doc=normalize(document)
    if doc['schema']==SCHEMA_V2:
        _need(ink_bindings is not None,'v2 ink observation requires ink_bindings')
        maps,targets,style,resolved,verified,policies=_v2_prepare(
            scene,doc,bindings,ink_bindings,verify=True)
        return _observation(scene,doc,maps,targets,ink_bindings,policies,resolved,verified)
    _need(ink_bindings is None,'v1 templates do not carry ink_bindings')
    maps,targets=_resolve(scene,doc,bindings)
    return _no_ink_observation(doc,targets)


def _v2_prepare(scene,doc,bindings,ink_bindings,*,verify=True):
    from . import line_material_colors
    _,policies=_material_policies(doc['style'])
    _ink().normalize_bindings(ink_bindings,policies)
    _bind_target_images(ink_bindings,policies)
    maps,targets=_resolve(scene,doc,bindings)
    style=_native().repair_names(doc['style'],**maps)
    style=_ink().remap_style(style,maps['materials'],ink_bindings,policies)
    style=_native().checked(style)
    resolved=line_material_colors.resolve(scene,_plan(style))
    verified=_verify_ink(scene,doc,style,maps,targets,ink_bindings,policies,resolved) if verify else dict(policies=[])
    return maps,targets,style,resolved,verified,policies


def preflight(scene,document,bindings,*,mode='APPEND',conflict='ERROR',ink_bindings=None):
    if type(document) is dict and document.get('schema')==SCHEMA_V2 and mode!='REPLACE':
        raise ValueError('toon-line-template.v2 supports REPLACE only; APPEND is not supported')
    settings=_static(scene);doc=normalize(document)
    _need(mode in ('APPEND','REPLACE') and conflict in ('ERROR','RENAME'),'Choose append or explicit replacement and a supported conflict policy')
    if doc['schema']==SCHEMA_V2:
        _need(mode=='REPLACE','toon-line-template.v2 supports REPLACE only; APPEND is not supported')
        _need(ink_bindings is not None,'v2 templates require ink_bindings')
        maps,targets,style,resolved,verified,policies=_v2_prepare(scene,doc,bindings,ink_bindings)
        deps=_map_dependencies(scene,style)
        old=_native().checked(_native().read_scene(scene)) if settings.preset_text else None
        _native()._validate_controls(scene,style,style['scene_controls'])
        observation=_observation(scene,doc,maps,targets,ink_bindings,policies,resolved,verified)
        return dict(document=doc,style=style,targets=targets,target_maps=maps,
            dependencies=_v2_dependencies(deps,observation),brush_dependencies=deps,old=old,
            mode=mode,conflict=conflict,ink_observation=observation,ink_bindings=ink_bindings,
            ink_images={policy['id']:ink_bindings['images'][policy['id']] for policy in policies
                if policy['source']['kind']=='UV_IMAGE'})
    _need(ink_bindings is None,'v1 templates do not carry ink_bindings')
    maps,targets=_resolve(scene,doc,bindings)
    style=_native().repair_names(doc['style'],**maps)
    deps=_map_dependencies(scene,style)
    old=_native().checked(_native().read_scene(scene)) if settings.preset_text else None
    if mode=='APPEND':
        _need(old is not None and settings.style_mode=='PRESET','Append requires an existing static PRESET')
        style=_native().append_style(old,style,conflict=conflict)
    _native()._validate_controls(scene,style,style['scene_controls'])
    return dict(document=doc,style=style,targets=targets,dependencies=deps,old=old,mode=mode,conflict=conflict)


def inspect(scene,document,bindings,*,mode='APPEND',conflict='ERROR',ink_bindings=None):
    p=preflight(scene,document,bindings,mode=mode,conflict=conflict,ink_bindings=ink_bindings)
    if p['document']['schema']==SCHEMA_V2:
        return dict(status='ready',mode=mode,conflict=conflict,
            groups=len(p['style']['document']['request']['boundary']['groups']),
            dependencies=p['dependencies'],ink=p['dependencies']['ink'],
            roles={kind:{role:_ref(value) for role,value in rows.items()} for kind,rows in p['targets'].items()},
            limitations=['v2 ink sources are explicitly rebound; pixels, UV layers, slots and material nodes are not written',
                'UV consumers are checked against actual target material use; actual RENDER/numeric admission requires joint acceptance',
                'Static named native references; animation is not transported'])
    return dict(status='ready',mode=mode,conflict=conflict,groups=len(p['style']['document']['request']['boundary']['groups']),dependencies=p['dependencies'],
        roles={kind:{role:_ref(value) for role,value in rows.items()} for kind,rows in p['targets'].items()},
        limitations=['Static named native references; no image/UV assets or animation transported','Attribute availability is not artistic mask alignment or evaluated RENDER support'])


def _install(scene,style):
    """Native set_preset plus its portable RNA controls, without a temp file."""
    from .backend import pencil
    addon=pencil('addon');controls=style['scene_controls'];settings=scene.toon_viewport_preview
    addon.set_preset(scene,style['document'],style['recipe'],chain_controls_payload=controls.get('chain_controls'),map_amounts_payload=controls.get('map_amounts'),dash_patterns_payload=controls.get('dash_patterns'))
    for key,value in controls.get('settings',{}).items():setattr(settings,key,value)
    for row in controls.get('groups',[]):
        item=settings.groups.get(row['name'])
        if item:
            for key,value in row.items():
                if key!='name':setattr(item,key,value)
    if 'elements' in controls:
        E=pencil('render_elements');rows=E.normalize(controls['elements'],style_mode='PRESET',group_ids=[g.name for g in settings.groups])['elements'] if controls['elements'] else []
        settings.elements.clear()
        for row in rows:
            item=settings.elements.add();item.name=row['id'];item.label=row['label'];item.element_type=row['type']
            for key in ('visible_lines_on','hidden_lines_on','line_set_ids'):setattr(item,key,row[key])
            for key in E.CATEGORIES:setattr(item,key+'_on',row[key+'_on'])
            item.background_enabled=row['background']['enabled'];item.background_color=row['background']['rgba']
            if row['type']=='DEPTH':item.depth_min=row['depth']['z_min'];item.depth_max=row['depth']['z_max']
            item.use_group_filter=row['group_uids'] is not None;addon._sync_element_groups(item,settings)
            if row['group_uids'] is not None:
                for g in item.group_filters:g.enabled=g.name in row['group_uids']


def _close(a,b):
    if type(a) is dict:return type(b) is dict and set(a)==set(b) and all(_close(a[k],b[k]) for k in a)
    if type(a) is list:return type(b) is list and len(a)==len(b) and all(_close(x,y) for x,y in zip(a,b))
    if type(a) in (int,float) and type(b) in (int,float):return abs(a-b)<=4e-7*max(1.,abs(a),abs(b))
    return a==b


def _unique_images(images):
    result=[];seen=set()
    for image in images:
        pointer=image.as_pointer()
        if pointer not in seen:seen.add(pointer);result.append(image)
    return result


def _retain_images(images,entries):
    for image in _unique_images(images):
        old=bool(image.use_fake_user)
        entries.append(dict(image=image,name=image.name,old=old))
        if not old:
            image.use_fake_user=True
            _need(bool(image.use_fake_user),'Could not retain bound UV Image: '+image.name)


def _live_image(entry):
    import bpy
    try:
        image=entry['image']
        if image.name!=entry['name']:return None
    except (ReferenceError,RuntimeError):return None
    if bpy.data.images.get(entry['name']) is not image:return None
    return image


def _restore_images(entries):
    issues=[]
    for entry in entries:
        image=_live_image(entry)
        if image is None:
            issues.append('bound Image is missing or renamed: '+entry['name'])
            continue
        image.use_fake_user=bool(entry['old'])
    if issues:raise RuntimeError('; '.join(issues))


def _check_images(entries):
    for entry in entries:
        image=_live_image(entry)
        _need(image is not None,'Bound UV Image was removed or renamed; preserve subsequent edits')
        _need(bool(image.use_fake_user),'Bound UV Image fake-user changed; preserve subsequent edits')


def _binding_snapshot(bindings,ink_bindings):
    return ({kind:dict(rows) for kind,rows in bindings.items()},
        dict(nodes=copy.deepcopy(ink_bindings['nodes']),images=dict(ink_bindings['images']),
            uv_names=dict(ink_bindings['uv_names']),
            uv_transforms={key:list(value) for key,value in ink_bindings['uv_transforms'].items()}))


def _v2_dependencies(brush_dependencies,observation):
    """Receipt dependency envelope; ink carries declared and actual source values."""
    return dict(schema='toon-line-template-dependencies.v2',
        brush_maps=copy.deepcopy(brush_dependencies),ink=copy.deepcopy(observation))


def _restore_settings(scene,captured):
    """Restore native read-only chain flags backed by ID properties."""
    _state()._restore_propgroup(scene.toon_viewport_preview,captured)
    rows=captured['collections'].get('chains',[]);live=scene.toon_viewport_preview.chains
    _need(len(rows)==len(live),'Restored chain display row count differs')
    for item,row in zip(live,rows):
        values=row['values']
        _need(item.name==values['name'],'Restored chain display identity differs')
        for name in ('boundary_available','intersection_available'):
            if name in values:item['_'+name]=values[name]


def apply(scene,document,bindings,*,mode='APPEND',conflict='ERROR',ink_bindings=None,capture=None):
    import bpy
    C=_state();p=preflight(scene,document,bindings,mode=mode,conflict=conflict,ink_bindings=ink_bindings)
    is_v2=p['document']['schema']==SCHEMA_V2
    before=C._capture_state(scene);before_sha=C._state_sha256(before);old_text=scene.toon_viewport_preview.preset_text
    old_body=old_text.as_string() if old_text is not None else None
    protected=C._protect_text(old_text);archive=None;created=[];fake=[];committed_token=None
    try:
        if is_v2:_retain_images(p['ink_images'].values(),fake)
        _install(scene,p['style'])
        actual=_native().checked(_native().read_scene(scene))
        _need(actual['document']==p['style']['document'] and actual['recipe']==p['style']['recipe'],'Installed native document readback differs')
        _need(_close(actual['scene_controls'],p['style']['scene_controls']),'Installed native controls readback differs')
        _need(old_text is None or old_text.as_string()==old_body,'Previous preset text changed during installation')
        created=C._created_preset_texts(before,p['style']['document'],p['style']['recipe'])
        _need(len(created)==1 and created[0]==scene.toon_viewport_preview.preset_text,'Expected one new owned preset Text')
        C._restore_text_protection(old_text,protected);protected=None
        if p['old'] is not None:
            archive=bpy.data.texts.new('Toon previous line style '+uuid.uuid4().hex)
            archive['toon_line_template_archive']=SCHEMA;archive.use_fake_user=True
            archive.write(_json(p['old']))
        after=C._capture_state(scene)
        observation=ink_observation(scene,p['document'],bindings,ink_bindings) if is_v2 else None
        dependencies=(_v2_dependencies(p['brush_dependencies'],observation)
            if is_v2 else p['dependencies'])
        result=dict(status='applied',mode=mode,template_id=p['document']['template_id'],groups=len(actual['document']['request']['boundary']['groups']),
            dependencies=dependencies,archive=archive.name if archive else None,before=before,before_sha=before_sha,after_sha=C._state_sha256(after),created=created,archive_id=archive,old_text=old_text,old_body=old_body,after_body=scene.toon_viewport_preview.preset_text.as_string())
        if is_v2:
            scene_bindings,ink_snapshot=_binding_snapshot(bindings,ink_bindings)
            result.update(template_schema=SCHEMA_V2,ink=dependencies['ink'],_ink_document=p['document'],
                _ink_scene_bindings=scene_bindings,_ink_bindings=ink_snapshot,
                _ink_after_sha256=observation['sha256'],_ink_images=fake)
        committed_token=result
        if capture is not None:capture(result)
        return result
    except BaseException as error:
        C._restore_text_protection(old_text,protected);protected=None
        issues=[]
        try:_restore_images(fake)
        except Exception as restore:issues.append(str(restore))
        try:
            if not created:created=C._created_preset_texts(before,p['style']['document'],p['style']['recipe'])
            _restore_settings(scene,before['settings'])
            if old_text is not None and old_text.as_string()!=old_body:old_text.from_string(old_body)
            author=getattr(scene,'toon_author_files',None)
            if author is not None and before['author_settings'] is not None:C._restore_propgroup(author,before['author_settings'])
            C._cleanup_created_texts(created)
            if archive is not None:
                archive.use_fake_user=False
                if archive.users==0 and archive.get('toon_line_template_archive')==SCHEMA:bpy.data.texts.remove(archive)
            C._clear_preview_caches(scene)
            _need(C._state_sha256(C._capture_state(scene))==before_sha,'Complete Pencil state was not restored')
        except Exception as rollback:issues.append(str(rollback))
        if committed_token is not None:committed_token['status']='rollback_failed' if issues else 'rolled_back'
        if issues:raise RuntimeError('Line template failed; rollback incomplete: '+'; '.join(issues)) from error
        raise
    finally:
        if protected is not None:C._restore_text_protection(old_text,protected)


def rollback(scene,token):
    import bpy
    C=_state();_need(token['status']=='applied' and C._state_sha256(C._capture_state(scene))==token['after_sha'],'Pencil author changed; preserve subsequent edits')
    _need(scene.toon_viewport_preview.preset_text.as_string()==token['after_body'],'Current preset body changed; preserve subsequent edits')
    _need(token['old_text'] is None or token['old_text'].as_string()==token['old_body'],'Previous preset body changed; preserve it')
    if token.get('template_schema')==SCHEMA_V2:
        _need(all(key in token for key in ('_ink_document','_ink_scene_bindings','_ink_bindings',
            '_ink_after_sha256','_ink_images')),'v2 rollback token is incomplete')
        try:
            live=ink_observation(scene,token['_ink_document'],token['_ink_scene_bindings'],token['_ink_bindings'])
        except Exception as error:
            raise ValueError('Ink sources changed; preserve subsequent edits') from error
        _need(live['sha256']==token['_ink_after_sha256'],'Ink sources changed; preserve subsequent edits')
        _check_images(token['_ink_images'])
    before=token['before'];_restore_settings(scene,before['settings'])
    author=getattr(scene,'toon_author_files',None)
    if author is not None and before['author_settings'] is not None:C._restore_propgroup(author,before['author_settings'])
    C._cleanup_created_texts(token['created']);archive=token['archive_id']
    if archive is not None:
        archive.use_fake_user=False
        if archive.users==0 and archive.get('toon_line_template_archive')==SCHEMA:bpy.data.texts.remove(archive)
    if token.get('template_schema')==SCHEMA_V2:_restore_images(token['_ink_images'])
    C._clear_preview_caches(scene)
    _need(C._state_sha256(C._capture_state(scene))==token['before_sha'],'Pencil rollback differs from original')
    token['status']='rolled_back'
    return dict(status='rolled_back')


def receipt(token):
    result={key:token[key] for key in ('status','mode','template_id','groups','dependencies','archive')}
    if token.get('template_schema')==SCHEMA_V2:
        result['template_schema']=SCHEMA_V2;result['ink']=token['ink']
    return result
