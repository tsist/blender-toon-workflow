# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded inert JSON editor state. No source loading, preview job or autoexec.

Validation and JSON decoding do not import bpy. Explicit restore creates only
owned curve helpers. Save adapters embed the document in their single inert
metadata Text; they must not append helpers to the offline four-image closure.
"""
import copy
import json
import math
import re

SCHEMA = 'toon-painted-editor-state.v1'
TEXT_NAME = 'ToonSessionC4EditorState.json'
MAX_BYTES = 4*1024**2
MAX_GROUPS = 32
MAX_SELECTORS = 1024
MAX_CURVE_POINTS = 64
SAMPLES = 256
BRUSH_KEYS = ('brush','silhouette_brush','sharp_brush','marked_brush')
SETTINGS_KEYS = ('source_mode','include_open','feather_px','samples_per_axis',
    'coverage_profile','post_filter_sigma','source_selected','source_sharp',
    'source_marked','force_marked_visible')
MAP_KEYS = ('enabled','selection_kind','layer_name','layer_index','channel','encoding','missing')

def _cpu():
    if __package__:
        from . import cpu_editor_state
    else:
        import cpu_editor_state
    return cpu_editor_state

def brush_keys(document):
    return _cpu().BRUSH_KEYS if document.get('schema')=='toon-cpu-painted-editor-state.v1' else BRUSH_KEYS

def settings_keys(document):
    return _cpu().SETTINGS_KEYS if document.get('schema')=='toon-cpu-painted-editor-state.v1' else SETTINGS_KEYS


def need(value, message):
    if not value:
        raise ValueError(message)


def exact(value, keys, label):
    need(type(value) is dict and set(value)==set(keys),label+' fields differ')


def string(value, maximum, label, *, empty=False):
    need(type(value) is str and (0 if empty else 1)<=len(value)<=maximum and '\x00' not in value,label)


def uid(value):
    need(type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None,'32hex editor identity required')


def integer(value, lo, hi, label):
    need(type(value) is int and lo<=value<=hi,label)


def scalar(value, lo, hi, label):
    need(type(value) in (int,float) and lo<=value<=hi and math.isfinite(value),label)


def _basic_json(value, depth=0, counter=None):
    if counter is None:counter=[0]
    counter[0]+=1
    need(depth<=12 and counter[0]<=250000,'JSON structural budget')
    kind=type(value)
    need(kind in (dict,list,str,int,float,bool,type(None)),'Only basic JSON types are accepted')
    if kind is dict:
        need(len(value)<=64 and all(type(k) is str for k in value),'JSON object budget/keys')
        for item in value.values():_basic_json(item,depth+1,counter)
    elif kind is list:
        need(len(value)<=1024,'JSON list budget')
        for item in value:_basic_json(item,depth+1,counter)
    elif kind is str:
        need(len(value)<=1024,'JSON string budget')
    elif kind is float:
        need(math.isfinite(value),'Finite JSON floats required')


def boolean(value, label):
    need(type(value) is bool,label)


def vector(value, count, lo, hi, label):
    need(type(value) is list and len(value)==count,label)
    for x in value:
        scalar(x,lo,hi,label)


def _map(value):
    exact(value,MAP_KEYS,'map')
    boolean(value['enabled'],'Map enabled boolean')
    need(value['selection_kind'] in ('name','index'),'Map selection kind')
    string(value['layer_name'],1024,'Color attribute name',empty=not(value['enabled'] and value['selection_kind']=='name'))
    integer(value['layer_index'],0,15,'Color attribute index')
    need(value['channel'] in ('mean_rgb','alpha','red','max_rgb'),'Map channel')
    need(value['encoding'] in ('linear_rgba','srgb_rgba'),'Map encoding')
    need(value['missing'] in ('error','one','zero'),'Map missing policy')


def _curve(value):
    exact(value,('enabled','use_clip','clip','extend','tone','black_level','white_level','points','samples'),'curve')
    boolean(value['enabled'],'Curve enabled boolean')
    boolean(value['use_clip'],'Curve clipping boolean')
    vector(value['clip'],4,-16,16,'Curve clip bounds')
    a,b,c,d=value['clip']
    need(a<c and b<d,'Curve clip must have positive dimensions')
    need(value['extend'] in ('HORIZONTAL','EXTRAPOLATED'),'Curve extend')
    need(value['tone'] in ('STANDARD','FILMLIKE'),'Curve tone')
    for key in ('black_level','white_level'):
        vector(value[key],3,-16,16,'Curve levels')
    need(type(value['points']) is list and 2<=len(value['points'])<=MAX_CURVE_POINTS,'2..64 curve control points')
    last=-math.inf
    for point in value['points']:
        exact(point,('x','y','handle'),'curve control point')
        scalar(point['x'],-16,16,'Curve x');scalar(point['y'],-16,16,'Curve y')
        need(point['x']>last,'Curve x coordinates must strictly increase')
        last=point['x']
        need(point['handle'] in ('AUTO','AUTO_CLAMPED','VECTOR'),'Curve handle')
    vector(value['samples'],SAMPLES,0,1,'256 clamped uniform curve samples')


def validate(document):
    """Strict pure-JSON validation; returns an independent inert document."""
    if type(document) is dict and document.get('schema')=='toon-cpu-painted-editor-state.v1':
        return _cpu().validate(document)
    _basic_json(document)
    exact(document,('schema','draft_uid','revision','active_group','settings','groups'),'editor state')
    need(document['schema']==SCHEMA,'Unsupported editor state schema')
    uid(document['draft_uid'])
    integer(document['revision'],0,2**31-1,'Editor revision')
    need(type(document['groups']) is list and len(document['groups'])<=MAX_GROUPS,'At most32 groups')
    integer(document['active_group'],0,max(0,len(document['groups'])-1),'Active group index')
    config=document['settings'];exact(config,SETTINGS_KEYS,'editor settings')
    need(config['source_mode'] in ('authored','selected','sharp','selected_and_sharp'),'Source mode')
    for name in ('include_open','source_selected','source_sharp','source_marked','force_marked_visible'):
        boolean(config[name],'Settings boolean '+name)
    scalar(config['feather_px'],0,256,'Feather range')
    scalar(config['post_filter_sigma'],0,4,'Postfilter range')
    need(config['samples_per_axis'] in ('1','2','4'),'Sample rate')
    need(config['coverage_profile'] in ('round_capsule.v1','extent_falloff.v1'),'Coverage profile')
    group_ids=set();brush_ids=set();priorities=set()
    for group in document['groups']:
        exact(group,('uid','label','priority','object_ids','material_names','visible_enabled',
            'hidden_enabled','silhouette_enabled','sharp_enabled','marked_enabled','brushes'),'group')
        uid(group['uid']);need(group['uid'] not in group_ids,'Duplicate group identity');group_ids.add(group['uid'])
        string(group['label'],256,'Group label',empty=True)
        integer(group['priority'],-100000,100000,'Group priority')
        need(group['priority'] not in priorities,'Duplicate group priority');priorities.add(group['priority'])
        for key in ('object_ids','material_names'):
            rows=group[key]
            need(type(rows) is list and len(rows)<=MAX_SELECTORS,'Selector limit')
            for row in rows:string(row,1024,'Selector name')
            need(len(set(rows))==len(rows),'Duplicate selector')
        for key in ('visible_enabled','hidden_enabled','silhouette_enabled','sharp_enabled','marked_enabled'):
            boolean(group[key],'Group flag '+key)
        exact(group['brushes'],BRUSH_KEYS,'four independent brushes')
        for brush in group['brushes'].values():
            exact(brush,('uid','width_px','blend_amount','color','width_map','alpha_map','size_curve','alpha_curve'),'brush')
            uid(brush['uid']);need(brush['uid'] not in brush_ids,'Duplicate brush identity');brush_ids.add(brush['uid'])
            scalar(brush['width_px'],0,256,'Brush width');scalar(brush['blend_amount'],0,1,'Brush Blend')
            vector(brush['color'],3,0,1,'Linear brush RGB')
            _map(brush['width_map']);_map(brush['alpha_map'])
            _curve(brush['size_curve']);_curve(brush['alpha_curve'])
    try:
        raw=json.dumps(document,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    except (TypeError,ValueError,UnicodeError) as error:
        raise ValueError('Editor state must be bounded UTF-8 JSON') from error
    need(len(raw)+1<=MAX_BYTES,'Editor state exceeds4MiB')
    return copy.deepcopy(document)


def encode(document):
    return (json.dumps(validate(document),ensure_ascii=False,sort_keys=True,
                       allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')


def decode(data):
    need(type(data) in (bytes,str),'JSON bytes or text required')
    raw=data.encode('utf-8') if type(data) is str else data
    need(0<len(raw)<=MAX_BYTES,'Editor JSON size bound')
    def pairs(rows):
        result={}
        for key,value in rows:
            need(key not in result,'Duplicate JSON key')
            result[key]=value
        return result
    def bad_constant(value):
        raise ValueError('Non-finite JSON constant '+value)
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=bad_constant)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as error:
        raise ValueError('Invalid bounded editor JSON') from error
    return validate(value)


def recipe(document):
    """Pure JSON projection for binding accepted editor state to its recipe.

    This does not authenticate CurveMapping evaluation: explicit restore checks
    control points against samples in the current Blender before mutation.
    """
    if type(document) is dict and document.get('schema')=='toon-cpu-painted-editor-state.v1':
        return _cpu().recipe(document)
    document=validate(document)
    config=document['settings'];groups=[]
    need(bool(document['groups']),'An executable recipe requires at least one group')
    def mapping(value):
        if not value['enabled']:return None
        return dict(selection=({'name':value['layer_name']} if value['selection_kind']=='name' else {'index':value['layer_index']}),
            channel=value['channel'],encoding=value['encoding'],missing=value['missing'])
    def brush(value):
        return dict(width_px=value['width_px'],blend_amount=value['blend_amount'],color=list(value['color']),
            width_map=mapping(value['width_map']),alpha_map=mapping(value['alpha_map']),
            **{key:(list(value[key]['samples']) if value[key]['enabled'] else None) for key in ('size_curve','alpha_curve')})
    for group in document['groups']:
        need(bool(group['object_ids'] or group['material_names']),'Executable group requires selectors')
        row=dict(id=group['uid'],priority=group['priority'],object_ids=list(group['object_ids']),
            material_names=list(group['material_names']),visible_enabled=group['visible_enabled'],
            hidden_enabled=group['hidden_enabled'],brush=brush(group['brushes']['brush']))
        for key in ('silhouette','sharp','marked'):
            if group[key+'_enabled']:row[key+'_brush']=brush(group['brushes'][key+'_brush'])
        groups.append(row)
    schemas=dict(authored='toon-painted-authored-image-request.v1',selected='toon-painted-selected-image-request.v2',
        sharp='toon-painted-sharp-image-request.v1',selected_and_sharp='toon-painted-selected-sharp-image-request.v1')
    settings=dict(include_open=config['include_open'],feather_px=config['feather_px'],
        samples_per_axis=int(config['samples_per_axis']),coverage_profile=config['coverage_profile'],
        post_filter_sigma=config['post_filter_sigma'])
    if config['source_mode']=='authored':
        source_types=[key for key in ('selected','sharp','marked') if config['source_'+key]]
        need(bool(source_types),'Authored recipe requires source types')
        settings.update(source_types=source_types,force_marked_visible=config['force_marked_visible'])
    return dict(schema=schemas[config['source_mode']],groups=groups,settings=settings)


def _api():
    if __package__:
        from . import paint_settings
    else:
        import paint_settings
    paint_settings.main_thread()
    return paint_settings


def _samples(mapping):
    mapping.update()
    values=[float(mapping.evaluate(mapping.curves[0],i/(SAMPLES-1))) for i in range(SAMPLES)]
    need(all(math.isfinite(v) for v in values),'Non-finite CurveMapping output')
    return [max(0.,min(1.,v)) for v in values]


def _capture_curve(mapping, enabled):
    need(len(mapping.curves)==1,'Expected actual ShaderNodeFloatCurve one-curve RNA')
    return dict(enabled=enabled,use_clip=mapping.use_clip,
        clip=[mapping.clip_min_x,mapping.clip_min_y,mapping.clip_max_x,mapping.clip_max_y],
        extend=mapping.extend,tone=mapping.tone,black_level=list(mapping.black_level),white_level=list(mapping.white_level),
        points=[dict(x=float(p.location.x),y=float(p.location.y),handle=p.handle_type) for p in mapping.curves[0].points],
        samples=_samples(mapping))


def capture(draft):
    """Capture editor data only, with no source path/binding and no execution."""
    if draft.source_mode=='cpu_boundary':return _cpu().capture(draft)
    api=_api()
    if not draft.uid:
        import uuid
        draft.uid=uuid.uuid4().hex
    api.synchronize_revision(draft)
    groups=[];seen_trees=set()
    for group in draft.groups:
        brushes={}
        for key in BRUSH_KEYS:
            brush=getattr(group,key);tree=api.owned_tree(draft,brush)
            need(tree.as_pointer() not in seen_trees,'Editor brushes must own independent helper trees')
            seen_trees.add(tree.as_pointer())
            brushes[key]=dict(uid=brush.uid,width_px=float(brush.width_px),blend_amount=float(brush.blend_amount),
                color=list(brush.color),width_map={k:getattr(brush.width_map,k) for k in MAP_KEYS},
                alpha_map={k:getattr(brush.alpha_map,k) for k in MAP_KEYS},
                **{name:_capture_curve(tree.nodes[name].mapping,getattr(brush,name+'_enabled')) for name in ('size_curve','alpha_curve')})
        groups.append(dict(uid=group.uid,label=group.label,priority=group.priority,
            object_ids=[r.object.name if r.object else r.snapshot_id for r in group.objects],
            material_names=[r.material.name if r.material else r.snapshot_name for r in group.materials],
            visible_enabled=group.visible_enabled,hidden_enabled=group.hidden_enabled,
            silhouette_enabled=group.silhouette_enabled,sharp_enabled=group.sharp_enabled,
            marked_enabled=group.marked_enabled,brushes=brushes))
    return validate(dict(schema=SCHEMA,draft_uid=draft.uid,revision=draft.revision,
        active_group=draft.active_group,settings={k:getattr(draft,k) for k in SETTINGS_KEYS},groups=groups))


def _restore_curve(mapping, value):
    mapping.initialize()
    mapping.use_clip=False
    mapping.clip_min_x,mapping.clip_min_y,mapping.clip_max_x,mapping.clip_max_y=value['clip']
    mapping.extend=value['extend'];mapping.tone=value['tone']
    mapping.black_level=value['black_level'];mapping.white_level=value['white_level']
    curve=mapping.curves[0]
    # New owned helper has exactly two endpoints. Add remaining points only here.
    points=value['points']
    curve.points[0].location=(points[0]['x'],points[0]['y'])
    curve.points[-1].location=(points[-1]['x'],points[-1]['y'])
    for point in points[1:-1]:
        curve.points.new(point['x'],point['y'])
    mapping.update()
    for point,stored in zip(curve.points,points):
        point.handle_type=stored['handle']
    mapping.use_clip=value['use_clip'];mapping.update()
    need(_samples(mapping)==value['samples'],'Stored control points/samples differ in current Blender; explicit migration required')


def _populate(api, draft, document, *, trees=None):
    if document.get('schema')=='toon-cpu-painted-editor-state.v1':
        return _cpu().populate(api,draft,document,trees=trees)
    draft.uid=document['draft_uid']
    for name,value in document['settings'].items():setattr(draft,name,value)
    for stored in document['groups']:
        group=draft.groups.add();group.uid=stored['uid'];group.label=stored['label'];group.priority=stored['priority']
        for key in ('visible_enabled','hidden_enabled','silhouette_enabled','sharp_enabled','marked_enabled'):
            setattr(group,key,stored[key])
        for value in stored['object_ids']:group.objects.add().snapshot_id=value
        for value in stored['material_names']:group.materials.add().snapshot_name=value
        for key in BRUSH_KEYS:
            value=stored['brushes'][key];brush=getattr(group,key)
            brush.uid=value['uid']
            for field in ('width_px','blend_amount','color'):setattr(brush,field,value[field])
            for field in ('width_map','alpha_map'):
                for name,item in value[field].items():setattr(getattr(brush,field),name,item)
            for name in ('size_curve','alpha_curve'):setattr(brush,name+'_enabled',value[name]['enabled'])
            if trees is None:
                tree=api.initialize_brush(draft,brush,uid=brush.uid)
                for name in ('size_curve','alpha_curve'):_restore_curve(tree.nodes[name].mapping,value[name])
            else:
                brush.curve_tree=trees[brush.uid]
    draft.active_group=document['active_group']
    api.synchronize_revision(draft)
    draft.revision=document['revision']


def _rollback_snapshot(api,draft):
    """Internal RNA transaction snapshot, including inactive mode data/ID refs.

    This is not serializable editor state. Existing objects and helper trees
    remain untouched while a replacement is checked in its temporary Scene.
    """
    groups=[]
    group_fields=('uid','label','priority','visible_enabled','hidden_enabled',
        'silhouette_enabled','sharp_enabled','smoothing_enabled','marked_enabled',
        'size_mode','initial_scale_ex','coverage_extent_factor')
    for group in draft.groups:
        brushes={}
        for key in api.ALL_BRUSH_KEYS:
            b=getattr(group,key)
            brushes[key]=dict(uid=b.uid,width_px=b.width_px,native_size=b.native_size,
                blend_amount=b.blend_amount,color=tuple(b.color),curve_tree=b.curve_tree,
                size_curve_enabled=b.size_curve_enabled,alpha_curve_enabled=b.alpha_curve_enabled,
                width_map={k:getattr(b.width_map,k) for k in MAP_KEYS},
                alpha_map={k:getattr(b.alpha_map,k) for k in MAP_KEYS})
        groups.append(dict(values={k:getattr(group,k) for k in group_fields},brushes=brushes,
            objects=[(r.object,r.snapshot_id) for r in group.objects],
            materials=[(r.material,r.snapshot_name) for r in group.materials]))
    return dict(meta={k:getattr(draft,k) for k in ('uid','revision','curve_signature','active_group')},
        settings={k:getattr(draft,k) for k in api.ALL_SETTINGS_KEYS},groups=groups)


def _rollback_restore(draft,snapshot):
    for key,value in snapshot['settings'].items():setattr(draft,key,value)
    for stored in snapshot['groups']:
        g=draft.groups.add()
        for key,value in stored['values'].items():setattr(g,key,value)
        for pointer,fallback in stored['objects']:
            row=g.objects.add();row.object=pointer;row.snapshot_id=fallback
        for pointer,fallback in stored['materials']:
            row=g.materials.add();row.material=pointer;row.snapshot_name=fallback
        for key,values in stored['brushes'].items():
            b=getattr(g,key)
            for name,value in values.items():
                if name in ('width_map','alpha_map'):
                    for field,item in value.items():setattr(getattr(b,name),field,item)
                else:setattr(b,name,value)
    for key,value in snapshot['meta'].items():setattr(draft,key,value)


def restore(scene, document, *, replace=False):
    """Explicit inert restore; no object lookup, source read, render or preview.

    The caller owns stale/live-owner handling. By default a nonempty draft is
    protected. replace=True is explicit replacement of editor data only; old
    helpers are left intact for consumers/undo and are not automatically purged.
    """
    document=validate(document)
    api=_api();draft=getattr(scene,api.PROP)
    need(type(replace) is bool,'Literal replace flag')
    need(not len(draft.groups) or replace,'Target draft is not empty; explicit replace=True required')
    import bpy
    stage=bpy.data.scenes.new('ToonPaint Inert Restore Check')
    created=[]
    try:
        with api.suspend_notifications():
            staged=getattr(stage,api.PROP)
            _populate(api,staged,document)
            trees={b.uid:b.curve_tree for g in staged.groups for b in (getattr(g,k) for k in brush_keys(document))}
            created=list(trees.values())
            # Exact round trip before target mutation includes disabled editor data.
            need(capture(staged)==document,'Editor restoration differs before target mutation')
            rollback=_rollback_snapshot(api,draft)
            if len(draft.groups):
                try:
                    capture(draft)
                finally:
                    # capture may synchronize a curve signature; unsuccessful
                    # replacement must retain the exact pre-call metadata.
                    draft.curve_signature=rollback['meta']['curve_signature']
            try:
                draft.groups.clear()
                _populate(api,draft,document,trees=trees)
            except BaseException:
                draft.groups.clear()
                _rollback_restore(draft,rollback)
                raise
        return draft
    finally:
        # Remove only this explicit restore's temporary scene and unused helpers.
        if not created:
            staged=getattr(stage,api.PROP)
            created=[b.curve_tree for g in staged.groups for b in (getattr(g,k) for k in api.ALL_BRUSH_KEYS) if b.curve_tree is not None]
        bpy.data.scenes.remove(stage)
        for tree in created:
            if tree.users==0 and tree.get('role')==api.ROLE:
                bpy.data.node_groups.remove(tree)


def write_text(draft, *, name=TEXT_NAME):
    """Create a new inert Text; never replace a Text or save the Main."""
    string(name,256,'Text name')
    api=_api();raw=encode(capture(draft))
    import bpy
    text=bpy.data.texts.new(name)
    try:
        text.use_module=False;text.use_fake_user=True;text.write(raw.decode('utf-8'))
    except BaseException:
        bpy.data.texts.remove(text)
        raise
    return text


def read_text(text):
    need(not text.use_module,'Autorun Text cannot be an editor-state source')
    return decode(text.as_string())


def _json_path(filepath):
    from pathlib import Path
    path=Path(filepath)
    need(path.is_absolute() and path.suffix.lower()=='.json','请选择绝对路径的 JSON 设置文件')
    return path


def write_file(document, filepath):
    """Explicit new JSON export; never mutate the Main or overwrite a file."""
    raw=encode(document)
    path=_json_path(filepath)
    with path.open('xb') as stream:
        need(stream.write(raw)==len(raw),'Incomplete editor-state JSON write')
    return str(path)


def import_file(scene, filepath):
    """Explicit bounded JSON import into an empty draft; no source/preview IO."""
    need(scene.get('qualification')!='offline_derivative_not_revalidated',
         '请先打开源工程，再导入描边设置；离线四图副本保持只读')
    need(not scene.get('public_session_schema'),
         '请在源工程中导入设置；离线四图副本保持只读')
    path=_json_path(filepath)
    with path.open('rb') as stream:
        document=decode(stream.read(MAX_BYTES+1))
    need(document['revision']<2**31-1,'草稿修订号已耗尽，请先建立新的草稿')
    draft=restore(scene,document)
    # Import is an explicit edit and must invalidate any live preview, including
    # a recipe whose saved revision happens to match the previous generation.
    _api().mark_changed(draft)
    return draft
