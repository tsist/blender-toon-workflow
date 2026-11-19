# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent complete style files, from-zero authors and explicit rebinding.

No executable preset files or commercial payload are read or generated.
"""
import copy
import json
import os
from pathlib import Path
import uuid

SCHEMA='toon.autonomous-style-file.v1'
CONTROL_FIELDS=('enabled','display_mode','line_width','line_color','opacity','width_multiplier','intersection_enabled','basic_line_set_id','basic_elements_only')


def new_document(*,width=1920,height=1080,label='自主线条'):
    from . import group_lifecycle as G
    if any(type(v) is not int or not 1<=v<=16384 for v in (width,height)):
        raise ValueError('Invalid author canvas dimensions')
    settings=dict(coverage_profile='round_capsule.v1',feather_px=1.,force_marked_visible=False,include_open=True,
        post_filter_sigma=0.,samples_per_axis=1,source_categories=['selected','smoothing'])
    units=dict(absolute_scale=1.,relative_target_height=None,relative_target_width=None,sensor_fit='AUTO')
    editor=dict(schema='toon.autonomous-editor.v1',active_group=None,draft_uid=uuid.uuid4().hex,groups=[],revision=0,settings={})
    document=dict(schema='toon.autonomous-author-document.v1',inputs=dict(autonomous_origin='toon.autonomous-style.v1',
        full_preset=None,width=width,height=height,projection=dict(editor_document=editor,source_aliases=[],partial=False,
        native_equivalent=False,losses=[])),request=dict(boundary=dict(schema='toon-painted-boundary-units-request.v1',groups=[],settings=settings,units=units),
        intersection=dict(schema='toon-painted-intersection-image-request.v3',groups=[],settings=dict(coverage_profile='round_capsule.v1',
            feather_px=1.,point_policy='suppress',postfilter_sigma=0.,samples_per_axis=1,self_intersection=True)),
        boundary_aliases=[],intersection_aliases=[],layers=[]))
    recipe=dict(schema='toon.autonomous-bindings.v1',object_bindings=[],material_bindings=[])
    document,plan,bindings,report=G.migrate(document,recipe,dict(action='NEW',label=label),_bootstrap=True)
    document['inputs']['projection']['editor_document']['active_group']=report['group']
    return dict(schema=SCHEMA,document=document,recipe=recipe)


def read_scene(scene):
    settings=scene.toon_viewport_preview
    if settings.preset_text is None:raise ValueError('请先新建或载入样式')
    wrapped=json.loads(settings.preset_text.as_string());wrapped['scene_controls']=capture_controls(scene)
    return wrapped


def capture_controls(scene):
    settings=scene.toon_viewport_preview
    values={name:getattr(settings,name) for name in CONTROL_FIELDS}
    values={name:list(value) if hasattr(value,'__len__') and not isinstance(value,str) else value for name,value in values.items()}
    from . import addon
    return dict(settings=values,
        groups=[{k:getattr(g,k) for k in ('name','label','enabled','width_scale','opacity','line_set_id','elements_only')} for g in settings.groups],
        elements=addon.element_configuration(settings),
        chain_controls=json.loads(settings.chain_controls_json) if settings.chain_controls_json else None,
        map_amounts=json.loads(settings.map_amounts_json) if settings.map_amounts_json else None,
        dash_patterns=json.loads(settings.dash_patterns_json) if settings.dash_patterns_json else None)


def new_scene_style(scene,*,label='自主线条'):
    from . import addon
    wrapped=new_document(width=scene.render.resolution_x,height=scene.render.resolution_y,label=label)
    objects=[obj for obj in scene.objects if obj.type=='MESH' and obj.select_get()]
    if not objects:objects=[obj for obj in scene.objects if obj.type=='MESH']
    row=wrapped['document']['request']['selector_bindings']['groups'][0]
    row['objects']=[dict(name=obj.name,library=obj.library.filepath if obj.library else None) for obj in objects]
    addon.set_preset(scene,wrapped['document'],wrapped['recipe'])
    scene.toon_viewport_preview.curve_group=row['group'];scene.toon_viewport_preview.curve_slot='brush'
    return wrapped


def checked(wrapped):
    from .style import compile_preset
    from .source_bindings import _preflight,recover
    _preflight(wrapped)
    if type(wrapped) is not dict or not {'document','recipe'}<=set(wrapped):raise ValueError('Expected complete style document and recipe')
    compile_preset(wrapped['document'],wrapped['recipe']);recover(wrapped['document'])
    return copy.deepcopy(wrapped)


def export_style(scene,path):
    from . import curve_editor
    curve_editor.commit_pending(scene)
    wrapped=checked(read_scene(scene));wrapped['schema']=SCHEMA
    path=Path(path)
    if not path.is_absolute() or path.suffix.lower()!='.json':raise ValueError('请选择绝对路径 JSON 文件名')
    data=json.dumps(wrapped,ensure_ascii=False,indent=2,allow_nan=False)
    with path.open('x',encoding='utf8') as stream:stream.write(data)
    return dict(path=str(path),groups=len(wrapped['document']['request']['boundary']['groups']))


def repair_names(wrapped,*,objects=None,materials=None,collections=None):
    """Explicit old-name -> {name,library} identities; never guess library joins."""
    result=checked(wrapped)
    for kind,mapping in (('objects',objects or {}),('materials',materials or {}),('collections',collections or {})):
        from .selector_bindings import key
        for value in mapping.values():key(value)
        for row in result['document']['request'].get('selector_bindings',{}).get('groups',[]):
            row[kind]=[copy.deepcopy(mapping.get(ref['name'],ref)) for ref in row[kind]]
        if kind!='collections':
            name='object_bindings' if kind=='objects' else 'material_bindings'
            for row in result['recipe'][name]:
                if row['source_name'] in mapping:row['source_name']=mapping[row['source_name']]['name']
        fields={'objects':('draw_objects','mask_objects'),'materials':('materials','draw_materials','mask_materials')}.get(kind,())
        for row in result['document']['request'].get('line_functions',{}).get('containers',[]):
            for field in fields:row[field]=[copy.deepcopy(mapping.get(ref['name'],ref)) for ref in row[field]]
        if kind=='collections':
            for row in result['document']['request'].get('line_hierarchy',{}).get('merge_domains',[]):
                row['collections']=[copy.deepcopy(mapping.get(ref['name'],ref)) for ref in row['collections']]
    return checked(result)


def append_style(target,incoming,*,conflict='ERROR'):
    """Append autonomous groups with regenerated IDs and exact author remapping."""
    from . import group_lifecycle as G,edge_sources as E,ix_sources as I
    target=checked(target);incoming=checked(incoming)
    if conflict not in ('ERROR','RENAME','REPLACE_LABEL'):raise ValueError('Invalid same-name policy')
    if incoming['document']['inputs'].get('autonomous_origin')!='toon.autonomous-style.v1':
        from . import captured_copy
        incoming=captured_copy.convert(incoming)
    src=incoming['document'];dst=target['document']
    labels={g['label']:g['uid'] for g in dst['inputs']['projection']['editor_document']['groups']}
    all_mapping={};replaced_actuals=set();original_gids={g['id'] for g in dst['request']['boundary']['groups']}
    for kind,key in (('object_bindings','logical_id'),('material_bindings','logical_name')):
        rows={r[key]:r for r in target['recipe'][kind]}
        for binding in incoming['recipe'][kind]:
            if binding[key] in rows and rows[binding[key]]!=binding:raise ValueError('Conflicting logical binding: '+binding[key])
            rows[binding[key]]=copy.deepcopy(binding)
        target['recipe'][kind]=list(rows.values())
    for eg in src['inputs']['projection']['editor_document']['groups']:
        label=eg['label'];old=eg['uid']
        current_replaced=set()
        replacing=label in labels and conflict=='REPLACE_LABEL'
        if label in labels:
            if conflict=='ERROR':raise ValueError('Same group label: '+label)
            if not replacing:
                n=2
                while label+' '+str(n) in labels:n+=1
                label=label+' '+str(n)
        if replacing:
            new=labels[label]
            if new not in G.owned(dst):raise ValueError('不能覆盖captured原始作者；请改名追加')
            if new in G.records(dst)['archived']:raise ValueError('请先恢复待替换的归档组')
            current_replaced=set(G.owned(dst)[new]['brushes']);replaced_actuals.update(current_replaced)
        else:
            dst,_,_,report=G.migrate(dst,target['recipe'],dict(action='NEW',label=label))
            new=report['group'];labels[label]=new
        source_row=next(r for r in G.records(src)['authored'] if r['group']==old)
        row=next(r for r in dst['request']['group_lifecycle']['authored'] if r['group']==new)
        for meta in source_row['brushes'].values():G._add_brush(dst,row,meta['channel'],meta['role'])
        # Translate all identifiers (actuals, slots and UID references) together.
        mapping={old:new}
        for actual,meta in source_row['brushes'].items():
            actual_new=G.identity(new,meta['channel'],meta['role']);mapping[actual]=actual_new
            mapping[E.slot_for(actual)]=E.slot_for(actual_new);mapping[I.slot_for(actual)]=I.slot_for(actual_new)
        all_mapping.update(mapping)
        def remap(value):
            if type(value) is str:return mapping.get(value,value)
            if type(value) is list:return [remap(v) for v in value]
            if type(value) is dict:return {mapping.get(k,k):remap(v) for k,v in value.items()}
            return value
        bg=remap(next(g for g in src['request']['boundary']['groups'] if g['id']==old))
        dest_bg=next(g for g in dst['request']['boundary']['groups'] if g['id']==new)
        bg['priority']=dest_bg['priority'];dest_bg.clear();dest_bg.update(bg)
        dest_eg=next(g for g in dst['inputs']['projection']['editor_document']['groups'] if g['uid']==new)
        clone=remap(eg);clone.update(label=label,priority=bg['priority'])
        for brush in clone['brushes'].values():brush['uid']=uuid.uuid4().hex
        dest_eg.clear();dest_eg.update(clone)
        row.clear();row.update(remap(source_row))
        for kind in ('edge_sources','intersection_sources'):
            rows=dst['request'][kind]['groups'];index=next(i for i,r in enumerate(rows) if r['group']==new)
            rows[index]=remap(next(r for r in src['request'][kind]['groups'] if r['group']==old))
        dst['request']['boundary_aliases']=[a for a in dst['request']['boundary_aliases'] if a['group']!=new]+remap([a for a in src['request']['boundary_aliases'] if a['group']==old])
        dst['request']['layers']=[a for a in dst['request']['layers'] if a['group_id']!=new]+remap([a for a in src['request']['layers'] if a['group_id']==old])
        if 'brush_effects' in dst['request']:
            for actual in current_replaced:dst['request']['brush_effects']['brushes'].pop(actual,None)
        if 'brush_effects' in src['request']:
            ext=dst['request'].setdefault('brush_effects',dict(schema='toon.brush-effects.v1',brushes={}))
            for actual in source_row['brushes']:
                if actual in src['request']['brush_effects']['brushes']:ext['brushes'][mapping[actual]]=remap(src['request']['brush_effects']['brushes'][actual])
        selection=next((r for r in src['request']['selector_bindings']['groups'] if r['group']==old),None)
        if selection:
            rows=dst['request']['selector_bindings']['groups'];rows[next(i for i,r in enumerate(rows) if r['group']==new)]=remap(selection)
        if old in src['request'].get('group_lifecycle',{}).get('archived',{}):
            dst['request']['group_lifecycle']['archived'][new]=remap(src['request']['group_lifecycle']['archived'][old])
        G._sync_ix(dst,new)
    target['document']=dst
    _append_extensions(target,incoming,all_mapping,original_gids,replaced_actuals)
    archives=src.get('provenance',{}).get('captured_import_archives',[])
    if archives:
        destination=dst.setdefault('provenance',{}).setdefault('captured_import_archives',[])
        known={r['sha256'] for r in destination}
        destination.extend(_remap(r,all_mapping) for r in archives if r['sha256'] not in known)
    return checked(target)


def _remap(value,mapping):
    if type(value) is str:return mapping.get(value,value)
    if type(value) is list:return [_remap(v,mapping) for v in value]
    if type(value) is dict:return {mapping.get(k,k):_remap(v,mapping) for k,v in value.items()}
    return copy.deepcopy(value)


def _append_extensions(target,incoming,mapping,original_gids,replaced_actuals):
    from . import source_bindings as B,chain_controls as C,map_amount_controls as M,dash_controls as D
    from .selector_bindings import key
    dst=target['document']['request'];src=incoming['document']['request'];new_gids=set(mapping[g['id']] for g in src['boundary']['groups'])
    if 'line_hierarchy' in src:
        hierarchy=dst.setdefault('line_hierarchy',dict(schema='toon.viewport-line-hierarchy.v1',lines=[dict(id='line-default',name='Line',priority=0,enabled=True,slots=list(original_gids))],merge_domains=[],merge_groups=[]))
        reusable={gid:line for line in hierarchy['lines'] for gid in line['slots'] if gid in new_gids and {g for g in line['slots'] if g is not None}<=new_gids}
        for line in hierarchy['lines']:line['slots']=[None if gid in new_gids else gid for gid in line['slots']]
        for line in src['line_hierarchy']['lines']:
            clone=_remap(line,mapping);existing=next((reusable[g] for g in clone['slots'] if g in reusable),None)
            clone['id']=existing['id'] if existing else 'line-'+uuid.uuid4().hex
            if existing:existing.clear();existing.update(clone)
            else:hierarchy['lines'].append(clone)
        for row in src['line_hierarchy']['merge_domains']:
            clone=_remap(row,mapping);clone['id']='domain-'+uuid.uuid4().hex
            tokens={key(ref) for ref in clone['collections']}
            if any({key(ref) for ref in domain['collections']}==tokens for domain in hierarchy['merge_domains']):continue
            existing={key(ref) for domain in hierarchy['merge_domains'] for ref in domain['collections']}
            if existing.intersection(key(ref) for ref in clone['collections']):raise ValueError('Merge collection already belongs to a target domain')
            hierarchy['merge_domains'].append(clone)
        hierarchy['merge_groups']=[r for r in hierarchy['merge_groups'] if r['group'] not in new_gids]+_remap(src['line_hierarchy']['merge_groups'],mapping)
    if 'line_functions' in src:
        value=dst.setdefault('line_functions',dict(schema='toon.viewport-line-functions.v1',containers=[],masks=[],self_intersections=[]))
        for row in src['line_functions']['containers']:
            clone=_remap(row,mapping);clone['id']='lf-'+uuid.uuid4().hex
            materials={key(ref) for ref in clone['materials']}
            for existing in value['containers']:
                overlap=materials.intersection(key(ref) for ref in existing['materials'])
                if overlap:
                    same={k:v for k,v in existing.items() if k not in ('id','materials')}=={k:v for k,v in clone.items() if k not in ('id','materials')}
                    if not same:raise ValueError('Material has a conflicting target Line Functions author')
                    clone['materials']=[ref for ref in clone['materials'] if key(ref) not in overlap]
            if clone['materials'] or not row['materials']:value['containers'].append(clone)
        for field in ('masks','self_intersections'):
            value[field]=[r for r in value.get(field,[]) if r['group'] not in new_gids]+_remap(src['line_functions'].get(field,[]),mapping)
    controls=target.setdefault('scene_controls',{});incoming_controls=_remap(incoming.get('scene_controls',{}),mapping)
    controls['groups']=[r for r in controls.get('groups',[]) if r['name'] not in new_gids]+incoming_controls.get('groups',[])
    labels={g['uid']:g['label'] for g in target['document']['inputs']['projection']['editor_document']['groups']}
    for row in controls['groups']:row['label']=labels[row['name']]
    existing_elements=controls.setdefault('elements',[]);known={r['id'] for r in existing_elements}
    for row in incoming_controls.get('elements',[]):
        if row['id'] in known:row['id']='E_'+uuid.uuid4().hex[:24]
        known.add(row['id']);existing_elements.append(row)
    bindings=B.recover(target['document'])
    for name,field,seed,normalize in (('chain_controls','brushes',C.seed_controls,C.normalize_controls),('map_amounts','amounts',M.defaults,M.normalize),('dash_patterns','patterns',D.defaults,D.normalize)):
        base=controls.get(name);source=incoming_controls.get(name)
        if base is None and source is None:continue
        combined=seed(bindings)
        for payload in (base,source):
            if payload:
                for actual,row in payload[field].items():
                    if actual in combined[field]:combined[field][actual]=copy.deepcopy(row)
        controls[name]=normalize(bindings,combined)


def import_style(scene,path,*,mode='REPLACE',conflict='ERROR',objects=None,materials=None,collections=None):
    from . import addon
    path=Path(path)
    if not path.is_file() or path.stat().st_size>16*1024*1024:raise ValueError('需要不超过16MiB的JSON样式文件')
    wrapped=repair_names(json.loads(path.read_text(encoding='utf-8-sig')),objects=objects,materials=materials,collections=collections)
    if mode=='APPEND':wrapped=append_style(read_scene(scene),wrapped,conflict=conflict)
    elif mode!='REPLACE':raise ValueError('Unknown import mode')
    controls=wrapped.get('scene_controls',{})
    _validate_controls(scene,wrapped,controls)
    report=addon.set_preset(scene,wrapped['document'],wrapped['recipe'],chain_controls_payload=controls.get('chain_controls'),
        map_amounts_payload=controls.get('map_amounts'),dash_patterns_payload=controls.get('dash_patterns'))
    settings=scene.toon_viewport_preview
    for name,value in controls.get('settings',{}).items():
        if name not in CONTROL_FIELDS:raise ValueError('Unknown portable setting')
        setattr(settings,name,value)
    for row in controls.get('groups',[]):
        item=settings.groups.get(row['name'])
        if item:
            for name,value in row.items():
                if name!='name':setattr(item,name,value)
    if 'elements' in controls:
        from . import render_elements
        elements=render_elements.normalize(controls['elements'],style_mode='PRESET',group_ids=[g.name for g in settings.groups])['elements'] if controls['elements'] else []
        settings.elements.clear()
        for row in elements:
            item=settings.elements.add();item.name=row['id'];item.label=row['label'];item.element_type=row['type']
            for name in ('visible_lines_on','hidden_lines_on','line_set_ids'):setattr(item,name,row[name])
            for name in render_elements.CATEGORIES:setattr(item,name+'_on',row[name+'_on'])
            item.background_enabled=row['background']['enabled'];item.background_color=row['background']['rgba']
            if row['type']=='DEPTH':item.depth_min=row['depth']['z_min'];item.depth_max=row['depth']['z_max']
            item.use_group_filter=row['group_uids'] is not None
            addon._sync_element_groups(item,settings)
            if row['group_uids'] is not None:
                for group in item.group_filters:group.enabled=group.name in row['group_uids']
    return report


def _validate_controls(scene,wrapped,controls):
    from . import render_elements
    from .style import _number
    if type(controls) is not dict or set(controls)-{'settings','groups','elements','chain_controls','map_amounts','dash_patterns'}:
        raise ValueError('Unknown portable scene controls')
    settings=scene.toon_viewport_preview
    values=controls.get('settings',{})
    if type(values) is not dict or set(values)-set(CONTROL_FIELDS):raise ValueError('Unknown portable settings')
    for name,value in values.items():
        prop=settings.bl_rna.properties[name]
        if prop.type=='BOOLEAN' and type(value) is not bool:raise ValueError('Invalid portable boolean')
        if prop.type=='ENUM' and value not in {i.identifier for i in prop.enum_items}:raise ValueError('Invalid portable enum')
        if prop.type in ('INT','FLOAT'):
            numbers=value if prop.is_array else [value]
            if prop.is_array and (type(value) is not list or len(value)!=prop.array_length):raise ValueError('Invalid portable vector')
            for scalar in numbers:_number(scalar,name,prop.hard_min,prop.hard_max)
    known={g['id'] for g in wrapped['document']['request']['boundary']['groups']}
    rows=controls.get('groups',[])
    if type(rows) is not list or len(rows)>32:raise ValueError('Invalid portable group controls')
    seen=set()
    for row in rows:
        if type(row) is not dict or set(row)!={'name','label','enabled','width_scale','opacity','line_set_id','elements_only'} or row['name'] not in known or row['name'] in seen:raise ValueError('Invalid portable group identity')
        seen.add(row['name'])
        if type(row['label']) is not str or len(row['label'])>256 or type(row['enabled']) is not bool or type(row['elements_only']) is not bool:raise ValueError('Invalid portable group values')
        _number(row['width_scale'],'group width',0,8);_number(row['opacity'],'group opacity',0,1)
        if type(row['line_set_id']) is not int or not 1<=row['line_set_id']<=8:raise ValueError('Invalid portable line ID')
    if controls.get('elements'):render_elements.normalize(controls['elements'],style_mode='PRESET',group_ids=known)


def state(scene):
    from .source_bindings import recover
    from . import addon
    settings=scene.toon_viewport_preview
    wrapped=read_scene(scene) if settings.preset_text else None
    return dict(scene=scene.name,enabled=settings.enabled,style_mode=settings.style_mode,
        revision=settings.preset_revision,document=wrapped,bindings=recover(wrapped['document']) if wrapped else None,
        jobs=addon.output_jobs())


def cleanup_references(scene,*,remove_missing=True,match_names=False,replacements=None):
    """Clean author selectors only. Never delete objects/materials/collections."""
    import bpy
    import re
    from . import addon,selector_bindings as S
    wrapped=checked(read_scene(scene));document=wrapped['document'];rows,_=S.records(document)
    replacements=replacements or {};changes=[]
    for kind,blocks in (('objects',bpy.data.objects),('collections',bpy.data.collections),('materials',bpy.data.materials)):
        index={S.key(dict(name=b.name,library=b.library.filepath if b.library else None)):b for b in blocks}
        mapping=replacements.get(kind,{})
        for ref in mapping.values():S.key(ref)
        for row in rows.values():
            updated=[]
            for ref in row[kind]:
                old=copy.deepcopy(ref)
                ref=copy.deepcopy(mapping.get(S.key(ref),mapping.get(ref['name'],ref)))
                if S.key(ref) not in index and match_names:
                    base=re.sub(r'\.\d{3}$','',ref['name'])
                    candidates=[b for b in blocks if re.sub(r'\.\d{3}$','',b.name)==base and b.library is None]
                    if len(candidates)==1:ref=dict(name=candidates[0].name,library=None)
                if S.key(ref) not in index and remove_missing:
                    changes.append(dict(group=row['group'],kind=kind,before=old,after=None));continue
                if ref!=old:changes.append(dict(group=row['group'],kind=kind,before=old,after=ref))
                if ref not in updated:updated.append(ref)
            row[kind]=updated
    ext=document['request'].get('selector_bindings')
    if ext is not None:ext['groups']=list(rows.values())
    # Name repair of historical recipe bindings is explicit and does not mutate
    # the captured source author. Unused recipe rows can be pruned safely.
    for kind,logical,field in (('object_bindings','logical_id','object_ids'),('material_bindings','logical_name','material_names')):
        used={v for g in document['request']['boundary']['groups'] for v in g[field]}
        wrapped['recipe'][kind]=[r for r in wrapped['recipe'][kind] if r[logical] in used]
    document['inputs']['projection']['editor_document']['revision']+=1
    checked(wrapped);addon.set_preset(scene,document,wrapped['recipe'])
    return dict(changes=changes,data_blocks_deleted=0,protected_native_payload_unchanged=True)
