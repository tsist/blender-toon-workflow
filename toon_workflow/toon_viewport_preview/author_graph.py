# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable native Blender author graph backed by the production Line contract.

Links, including reroutes, are parsed on explicit commit. Full source provenance
is immutable; cross-actual links copy onto independent owned targets.
"""
import copy
import json
import uuid
import bpy
from bpy.props import StringProperty,BoolProperty,IntProperty,FloatProperty,FloatVectorProperty,EnumProperty,PointerProperty

from . import author_channels

TREE='TOON_AuthorTree'
_ENUMS={}
_SELECTION={}


def need(value,message):
    if not value:raise ValueError(message)


class TOONAUTHOR_Tree(bpy.types.NodeTree):
    bl_idname=TREE;bl_label='自主线条作者图';bl_icon='NODETREE'
    show_visible_lines:BoolProperty(name='编辑可见线',default=True,update=author_channels.update_tree)


class TOONAUTHOR_Socket(bpy.types.NodeSocket):
    bl_idname='TOON_AuthorSocket';bl_label='作者连接'
    kind:EnumProperty(items=[('GROUP','LineSet',''),('BRUSH','Brush','')],default='GROUP')
    target_actual:StringProperty()
    author_channel:EnumProperty(items=[(c,c,'') for c in ('BOTH','VISIBLE','HIDDEN')],default='BOTH')
    def draw(self,context,layout,node,text):layout.label(text=text)
    def draw_color(self,context,node):return (.25,.65,.9,1.) if self.kind=='GROUP' else (.95,.55,.15,1.)


class AuthorNode:
    @classmethod
    def poll(cls,tree):return tree.bl_idname==TREE


def _display_color(node,context):node.use_custom_color=True;node.color=node.display_color


class TOONAUTHOR_Line(AuthorNode,bpy.types.Node):
    bl_idname='TOON_AuthorLine';bl_label='Line'
    line_id:StringProperty()
    author_name:StringProperty(name='Line名称',default='Line')
    priority:IntProperty(name='优先级',default=0,min=-1000000,max=1000000)
    enabled:BoolProperty(name='启用',default=True)
    size_mode:EnumProperty(name='线宽模式',items=[('INHERIT','继承组',''),('ABSOLUTE','绝对',''),('RELATIVE','相对','')])
    override_antialiasing:BoolProperty(name='独立抗锯齿',default=False)
    antialiasing:FloatProperty(name='抗锯齿',default=1.,min=0,max=16)
    oversampling:EnumProperty(name='过采样',items=[(n,n,'') for n in ('INHERIT','1','2','4')])
    override_off_screen:BoolProperty(name='独立屏外距离',default=False)
    off_screen_distance:FloatProperty(name='屏外距离',default=0.,min=0,max=65536)
    elements_only:BoolProperty(name='仅输出分层元素',default=False)
    def init(self,context):
        self.line_id='line-'+uuid.uuid4().hex;self.inputs.new(TOONAUTHOR_Socket.bl_idname,'LineSet 1')
    def draw_buttons(self,context,layout):
        layout.prop(self,'author_name');layout.prop(self,'priority');layout.prop(self,'enabled')
        layout.prop(self,'size_mode');layout.prop(self,'oversampling');layout.prop(self,'elements_only')
        layout.prop(self,'override_antialiasing')
        if self.override_antialiasing:layout.prop(self,'antialiasing')
        layout.prop(self,'override_off_screen')
        if self.override_off_screen:layout.prop(self,'off_screen_distance')
        row=layout.row(align=True)
        for action,label in [('SOCKET','加空槽'),('SHRINK','收起空槽')]:
            op=row.operator('toon_viewport.author_graph',text=label);op.action=action;op.node=self.name


class TOONAUTHOR_LineSet(AuthorNode,bpy.types.Node):
    bl_idname='TOON_AuthorLineSet';bl_label='LineSet'
    group_id:StringProperty(name='组身份')
    author_name:StringProperty(name='组名称')
    display_color:FloatVectorProperty(name='作者显示色',subtype='COLOR',size=3,default=(.25,.35,.45),min=0,max=1,update=_display_color)
    def init(self,context):self.outputs.new(TOONAUTHOR_Socket.bl_idname,'LineSet')
    def draw_buttons(self,context,layout):
        layout.prop_search(self,'group_id',context.scene.toon_viewport_preview,'groups',text='样式组')
        layout.prop(self,'author_name');layout.prop(self,'display_color')
        op=layout.operator('toon_viewport.author_graph',text='选择组及绑定入口');op.action='SELECT';op.node=self.name
        row=layout.row(align=True)
        for kind,label in [('objects','对象'),('collections','集合'),('materials','材质')]:
            op=row.operator('toon_viewport.selector_add',text=label);op.group=self.group_id;op.kind=kind


class TOONAUTHOR_Brush(AuthorNode,bpy.types.Node):
    bl_idname='TOON_AuthorBrush';bl_label='Brush'
    actual_id:StringProperty(name='Actual身份')
    author_channel:EnumProperty(items=[(c,c,'') for c in ('BOTH','VISIBLE','HIDDEN')],default='BOTH')
    editor_mode:EnumProperty(name='编辑入口',items=[('DETAILS','笔刷细节',''),('CURVES','曲线','')])
    def init(self,context):
        socket=self.outputs.new(TOONAUTHOR_Socket.bl_idname,'Brush');socket.kind='BRUSH'
    def draw_buttons(self,context,layout):
        channel='VISIBLE' if self.id_data.show_visible_lines else 'HIDDEN'
        if self.author_channel not in ('BOTH',channel):
            layout.label(text='另一频道笔刷');return
        layout.label(text=self.actual_id[-16:]);layout.prop(self,'editor_mode')
        if self.editor_mode=='DETAILS':
            op=layout.operator('toon_viewport.edit_brush_effects',text='打开笔刷细节');op.actual=self.actual_id
        else:
            op=layout.operator('toon_viewport.author_graph',text='选择并编辑曲线');op.action='CURVES';op.node=self.name
        op=layout.operator('toon_viewport.author_graph',text='选择对应笔刷');op.action='SELECT';op.node=self.name


def owner(scene):
    if 'toon_author_graph_owner' not in scene:scene['toon_author_graph_owner']=uuid.uuid4().hex
    return scene['toon_author_graph_owner']


def owned(scene,tree=None):
    tree=tree or scene.toon_author_files.author_tree
    need(tree is not None and tree.bl_idname==TREE and tree.get('owner')==owner(scene),'请选择此场景拥有的作者图')
    need(tree.library is None and tree.users<=1,'共享或链接作者图不可直接修改，请新建独立图')
    return tree


def create(scene):
    from . import file_authoring as F,line_hierarchy as H,source_bindings as B
    from . import curve_editor
    curve_editor.commit_pending(scene)
    wrapped=F.read_scene(scene);document=wrapped['document'];bindings=B.recover(document)
    tree=bpy.data.node_groups.new('自主线条作者图',TREE);tree['owner']=owner(scene)
    tree['preset_text']=scene.toon_viewport_preview.preset_text.name;tree['revision']=scene.toon_viewport_preview.preset_revision
    try:
        groups={g['uid']:g for g in document['inputs']['projection']['editor_document']['groups']};group_nodes={};brush_nodes={}
        for index,(gid,row) in enumerate(groups.items()):
            node=tree.nodes.new(TOONAUTHOR_LineSet.bl_idname);node.name='LineSet '+gid;node.label=row['label'];node.group_id=gid;node.author_name=row['label']
            node.display_color=row.get('display_color',(.25,.35,.45));node.location=(-100,-index*280);node.width=250;group_nodes[gid]=node
            routes=[r for r in bindings['routes'] if r['kind']=='boundary' and r['group_id']==gid and r['slot']!='brush']
            if not routes:routes=[r for r in bindings['routes'] if r['kind']=='boundary' and r['group_id']==gid]
            routes.extend(r for r in bindings['routes'] if r['kind']=='intersection' and r['group_id']==gid)
            seen=set()
            for route in routes:
                actual=route['actual_brush_id']
                if actual in seen:continue
                seen.add(actual)
                if actual not in brush_nodes:
                    brush=tree.nodes.new(TOONAUTHOR_Brush.bl_idname);brush.name='Brush '+actual;brush.label=bindings['actuals'][actual]['source_brush_key'];brush.actual_id=actual
                    brush.location=(-520,-len(brush_nodes)*180);brush.width=250;brush_nodes[actual]=brush
                socket=node.inputs.new(TOONAUTHOR_Socket.bl_idname,route['slot']);socket.kind='BRUSH';socket.target_actual=actual
                channels=author_channels.actual_channels(document,gid,actual,bindings)
                socket.author_channel=next(iter(channels)) if len(channels)==1 else 'BOTH'
                tree.links.new(brush_nodes[actual].outputs[0],socket)
        for index,line in enumerate(H.records(document)['lines']):
            node=tree.nodes.new(TOONAUTHOR_Line.bl_idname);node.name='Line '+line['id'];node.line_id=line['id'];node.author_name=line['name'];node.priority=line['priority'];node.enabled=line['enabled']
            node.size_mode=line.get('size_mode') or 'INHERIT';node.oversampling=str(line['oversampling']) if line.get('oversampling') else 'INHERIT';node.elements_only=line.get('elements_only',False)
            node.override_antialiasing=line.get('antialiasing') is not None;node.antialiasing=line.get('antialiasing') or 0.
            node.override_off_screen=line.get('off_screen_distance') is not None;node.off_screen_distance=line.get('off_screen_distance') or 0.
            node.location=(320,-index*260);node.width=230;node.inputs.clear()
            for slot,gid in enumerate(line['slots']):
                socket=node.inputs.new(TOONAUTHOR_Socket.bl_idname,'LineSet '+str(slot+1))
                if gid is not None:tree.links.new(group_nodes[gid].outputs[0],socket)
        for actual,brush in brush_nodes.items():
            channels=set().union(*(author_channels.actual_channels(document,gid,actual,bindings) for gid in groups
                if any(r['group_id']==gid and r['actual_brush_id']==actual for r in bindings['routes'])))
            brush.author_channel=next(iter(channels)) if len(channels)==1 else 'BOTH'
        tree.show_visible_lines=scene.toon_viewport_preview.show_visible_lines
        author_channels.update_tree(tree,None)
        previous=scene.toon_author_files.author_tree;scene.toon_author_files.author_tree=tree
        if previous is not None and previous.library is None and previous.get('owner')==owner(scene) and previous.users==0:bpy.data.node_groups.remove(previous)
        ensure_runtime()
        return tree
    except Exception:
        if tree.users==0:bpy.data.node_groups.remove(tree)
        raise


def resolve(socket,expected):
    """Resolve a single typed author through native reroutes, rejecting cycles."""
    if not socket.is_linked:return None
    seen=set();current=socket
    while True:
        need(len(current.links)==1,'作者输入必须只有一条连接')
        link=current.links[0];node=link.from_node
        need(node.as_pointer() not in seen,'作者Reroute存在循环');seen.add(node.as_pointer())
        if node.bl_idname=='NodeReroute':
            need(len(node.inputs)==1 and node.inputs[0].is_linked,'Reroute未连接有效作者');current=node.inputs[0];continue
        need(node.bl_idname==expected,'作者连接类型不匹配：'+node.bl_idname)
        return node


def _copy_brush(document,source,target,bindings):
    from . import group_lifecycle as G,edge_sources as E,wire_brush_import
    need(source in bindings['actuals'] and target in bindings['actuals'],'Unknown graph actual identity')
    if source==target:return
    own=G.owned(document);row=next((r for r in own.values() if target in r['brushes']),None)
    need(row is not None,'跨actual连接只能复制到自主笔刷；captured原始作者保持不变')
    aliases=document['request']['boundary_aliases']
    origin=next((r for r in aliases if r['brush_id']==source and r['slot']!='brush'),None) or next((r for r in aliases if r['brush_id']==source),None)
    editors={g['uid']:g for g in document['inputs']['projection']['editor_document']['groups']}
    if origin is not None:brush=copy.deepcopy(editors[origin['group']]['brushes'][origin['slot']])
    else:
        from . import ix_sources
        record=bindings['actuals'][source];full=document['inputs']['full_preset'];name=record['source_brush_key']
        props=full['brushes'][name]['properties'];detail=full['details'][full['brushes'][name]['detail']]
        brush=G._new_editor();brush.update(native_size=props['size'],blend_amount=props['blend_amount'],color=copy.deepcopy(props['brush_color']),
            size_curve=wire_brush_import._import_curve(detail,'size_curve'),alpha_curve=wire_brush_import._import_curve(detail,'alpha_curve'))
        ix=next((a for record in ix_sources.records(document).values() for a in record['authors'].values() if a['actual']==source),None)
        if ix is not None:
            brush.update(copy.deepcopy(ix['curves']));numeric=ix['brush']
        else:
            alias=next((r for r in document['request']['intersection_aliases'] if r['brush_id']==source),None)
            need(alias is not None,'Source brush has no complete executable author')
            numeric=next(g['brush'] for g in document['request']['intersection']['groups'] if g['id']==alias['group'])
        brush.update(blend_amount=numeric['blend_amount'],color=copy.deepcopy(numeric['color']))
        for field in ('width_map','alpha_map'):
            compact=numeric[field]
            if compact is None:brush[field]['enabled']=False
            else:
                selection=compact['selection'];brush[field]=dict(enabled=True,selection_kind='name' if 'name' in selection else 'index',layer_name=selection.get('name',''),layer_index=selection.get('index',0),channel=compact['channel'],encoding=compact['encoding'],missing=compact['missing'])
    gid=row['group']
    target_slots=[r['slot'] for r in aliases if r['brush_id']==target and r['group']==gid]
    boundary=next(g for g in document['request']['boundary']['groups'] if g['id']==gid)
    for slot in target_slots:
        value=copy.deepcopy(brush);value['uid']=editors[gid]['brushes'][slot]['uid'];editors[gid]['brushes'][slot]=value
        boundary[slot]=wire_brush_import.project_editor_brush(value)
    metadata=row['brushes'][target];record=bindings['actuals'][source]
    metadata['chain']=copy.deepcopy(record['chain_parameters_native']);metadata['shape']=copy.deepcopy(record['simple_shape_native'])
    metadata['initial_amount']=record.get('initial_amount',1.)
    effects=document['request'].get('brush_effects',{}).get('brushes',{})
    if source in effects:effects[target]=copy.deepcopy(effects[source])
    else:effects.pop(target,None)
    G._sync_ix(document,gid)


def migrate(document,recipe,updates):
    from . import line_hierarchy as H,source_bindings as B
    from .style import compile_preset
    candidate=copy.deepcopy(document);bindings=B.recover(candidate)
    # All sources read from the immutable pre-commit state; swapping connections
    # must not turn a simultaneous copy into an accidental sequential overwrite.
    original=copy.deepcopy(candidate)
    for source,target in updates['copies']:
        if source==target:continue
        origin=copy.deepcopy(original)
        _copy_brush(origin,source,target,bindings)
        target_group=next(r['group'] for r in origin['request']['group_lifecycle']['authored'] if target in r['brushes'])
        for key in ('boundary','edge_sources','intersection_sources'):
            source_row=next(r for r in origin['request'][key]['groups'] if r.get('id',r.get('group'))==target_group)
            dest=next(r for r in candidate['request'][key]['groups'] if r.get('id',r.get('group'))==target_group)
            # Only target brush slots/numeric authors are transferred below.
            if key=='boundary':
                slots=[r['slot'] for r in candidate['request']['boundary_aliases'] if r['brush_id']==target]
                for slot in slots:dest[slot]=copy.deepcopy(source_row[slot])
        src_editor=next(g for g in origin['inputs']['projection']['editor_document']['groups'] if g['uid']==target_group)
        dst_editor=next(g for g in candidate['inputs']['projection']['editor_document']['groups'] if g['uid']==target_group)
        for slot in [r['slot'] for r in candidate['request']['boundary_aliases'] if r['brush_id']==target]:dst_editor['brushes'][slot]=copy.deepcopy(src_editor['brushes'][slot])
        a=next(r for r in origin['request']['group_lifecycle']['authored'] if r['group']==target_group)
        b=next(r for r in candidate['request']['group_lifecycle']['authored'] if r['group']==target_group);b['brushes'][target]=copy.deepcopy(a['brushes'][target])
        effects=origin['request'].get('brush_effects',{}).get('brushes',{})
        own_effects=candidate['request'].setdefault('brush_effects',dict(schema='toon.brush-effects.v1',brushes={}))['brushes']
        if target in effects:own_effects[target]=copy.deepcopy(effects[target])
        else:own_effects.pop(target,None)
        from . import group_lifecycle as G
        G._sync_ix(candidate,target_group)
    for row in updates['groups']:
        editor=next(g for g in candidate['inputs']['projection']['editor_document']['groups'] if g['uid']==row['group'])
        editor['label']=row['name'];editor['display_color']=row['display_color']
    candidate['inputs']['projection']['editor_document']['revision']+=1
    return H.migrate(candidate,recipe,dict(hierarchy=updates['hierarchy']))


def apply(scene,tree=None):
    from . import file_authoring as F,line_hierarchy as H,source_bindings as B,wire_ui,chain_ui
    tree=owned(scene,tree);settings=scene.toon_viewport_preview
    need(tree.get('preset_text')==settings.preset_text.name and tree.get('revision')==settings.preset_revision,'作者图已过期，请保留当前图并从最新文档创建新图')
    wrapped=F.read_scene(scene);document=wrapped['document'];bindings=B.recover(document);known=set(bindings['groups'])
    lines=[];groups=[];copies=[];seen_groups=set();connected=set();line_ids=set();targets={}
    for node in tree.nodes:
        if node.bl_idname==TOONAUTHOR_Line.bl_idname:
            need(node.line_id and node.line_id not in line_ids,'重复Line身份');line_ids.add(node.line_id)
            slots=[]
            for socket in node.inputs:
                linked=resolve(socket,TOONAUTHOR_LineSet.bl_idname);gid=linked.group_id if linked else None
                need(gid is None or gid in known and gid not in connected,'同一LineSet只能连接一次')
                if gid is not None:connected.add(gid)
                slots.append(gid)
            lines.append(dict(id=node.line_id,name=node.author_name,priority=node.priority,enabled=node.enabled,slots=slots,
                size_mode=None if node.size_mode=='INHERIT' else node.size_mode,oversampling=None if node.oversampling=='INHERIT' else int(node.oversampling),
                antialiasing=node.antialiasing if node.override_antialiasing else None,
                off_screen_distance=node.off_screen_distance if node.override_off_screen else None,elements_only=node.elements_only))
        elif node.bl_idname==TOONAUTHOR_LineSet.bl_idname:
            need(node.group_id in known and node.group_id not in seen_groups,'Unknown/duplicate LineSet node')
            need(0<len(node.author_name)<=256,'作者组名称需要1..256字符')
            seen_groups.add(node.group_id)
            groups.append(dict(group=node.group_id,name=node.author_name,display_color=list(node.display_color)))
            for socket in node.inputs:
                need(socket.target_actual in bindings['actuals'],'Unknown target brush socket')
                need(any(r['group_id']==node.group_id and r['actual_brush_id']==socket.target_actual for r in bindings['routes']),'笔刷目标槽不属于此LineSet')
                linked=resolve(socket,TOONAUTHOR_Brush.bl_idname)
                need(linked is not None and linked.actual_id in bindings['actuals'],'LineSet笔刷输入不可悬空')
                need(socket.target_actual not in targets or targets[socket.target_actual]==linked.actual_id,'共享actual的连接来源互相矛盾')
                if socket.target_actual not in targets:copies.append((linked.actual_id,socket.target_actual))
                targets[socket.target_actual]=linked.actual_id
        elif node.bl_idname not in (TOONAUTHOR_Brush.bl_idname,'NodeReroute','NodeFrame'):raise ValueError('Unsupported author node')
    need(seen_groups==known,'作者图必须保留所有作者组；归档或移除请使用现有组生命周期入口')
    need(lines,'至少保留一个Line')
    if known-connected:lines.append(dict(id='line-unconnected-'+uuid.uuid4().hex,name='Unconnected',priority=-1000000,enabled=False,slots=sorted(known-connected)))
    hierarchy=H.records(document);hierarchy['lines']=lines
    report=wire_ui.apply_migration(scene,dict(groups=groups,copies=copies,hierarchy=hierarchy),migrate)
    # Static per-actual chain/map/dash settings copy while preserving target
    # animation identity and every unrelated author row.
    controls=F.read_scene(scene)['scene_controls'];before=wrapped.get('scene_controls',{})
    for name,field in (('chain_controls','brushes'),('map_amounts','amounts'),('dash_patterns','patterns')):
        if controls.get(name) and before.get(name):
            for source,target in copies:
                if source!=target and source in before[name][field]:controls[name][field][target]=copy.deepcopy(before[name][field][source])
    if any(a!=b for a,b in copies):
        from . import addon
        current=F.read_scene(scene)
        addon.set_preset(scene,current['document'],current['recipe'],chain_controls_payload=controls.get('chain_controls'),map_amounts_payload=controls.get('map_amounts'),dash_patterns_payload=controls.get('dash_patterns'))
    for row in groups:
        if settings.groups.get(row['group']):settings.groups[row['group']].label=row['name']
        for element in settings.elements:
            if element.group_filters.get(row['group']):element.group_filters[row['group']].label=row['name']
    tree['preset_text']=settings.preset_text.name;tree['revision']=settings.preset_revision
    report.update(lines=len(lines),brush_connections=len(copies),actual_identity_preserved=True)
    return report


def select(scene,node,*,curves=False):
    from . import source_bindings as B,file_authoring as F,curve_editor
    tree=owned(scene);settings=scene.toon_viewport_preview
    if isinstance(node,str):node=tree.nodes.get(node)
    need(node is not None,'Unknown author node')
    for item in tree.nodes:item.select=False
    node.select=True;tree.nodes.active=node
    _target_from_node(scene,node)
    _SELECTION[scene.as_pointer()]=(node.as_pointer(),settings.curve_group,settings.curve_slot)
    if curves:curve_editor.initialize(scene)
    return dict(group=settings.curve_group,slot=settings.curve_slot,active_node=node.name)


def _target_from_node(scene,node):
    from . import source_bindings as B,file_authoring as F
    settings=scene.toon_viewport_preview
    if node.bl_idname==TOONAUTHOR_LineSet.bl_idname:settings.curve_group=node.group_id;settings.curve_slot='brush'
    elif node.bl_idname==TOONAUTHOR_Brush.bl_idname:
        bindings=B.recover(F.read_scene(scene)['document'])
        eligible=[r for r in bindings['routes'] if r['actual_brush_id']==node.actual_id and r['group_id'] in {g.name for g in settings.groups}]
        route=next((r for r in eligible if r['kind']=='boundary'),eligible[0] if eligible else None)
        need(route is not None,'No active group for brush node');settings.curve_group=route['group_id'];settings.curve_slot=route['slot']
    elif node.bl_idname==TOONAUTHOR_Line.bl_idname:
        linked=next((resolve(s,TOONAUTHOR_LineSet.bl_idname) for s in node.inputs if s.is_linked),None)
        if linked:settings.curve_group=linked.group_id;settings.curve_slot='brush'


def sync_selection(scene):
    tree=owned(scene);settings=scene.toon_viewport_preview
    node=next((n for n in tree.nodes if n.bl_idname==TOONAUTHOR_LineSet.bl_idname and n.group_id==settings.curve_group),None)
    if settings.curve_slot!='brush':
        from . import source_bindings as B,file_authoring as F
        bindings=B.recover(F.read_scene(scene)['document'])
        route=next((r for r in bindings['routes'] if r['group_id']==settings.curve_group and r['slot']==settings.curve_slot),None)
        if route:node=next((n for n in tree.nodes if n.bl_idname==TOONAUTHOR_Brush.bl_idname and n.actual_id==route['actual_brush_id']),node)
        # Dynamic EnumProperty values are group-scoped. Switching group can
        # invalidate the previous group's slot before a user chooses a new one.
        else:settings.curve_slot='brush'
    need(node is not None,'Author graph has no current group node')
    return select(scene,node)


def _selection_tick():
    live=set()
    for window in bpy.context.window_manager.windows:
        scene=window.scene;pointer=scene.as_pointer();live.add(pointer)
        author=getattr(scene,'toon_author_files',None)
        if author is None or author.author_tree is None:continue
        tree=author.author_tree
        if tree.bl_idname!=TREE or tree.get('owner')!=scene.get('toon_author_graph_owner'):continue
        settings=scene.toon_viewport_preview;active=tree.nodes.active
        token=(active.as_pointer() if active else 0,settings.curve_group,settings.curve_slot)
        previous=_SELECTION.get(pointer)
        if previous is not None and token!=previous:
            try:
                visible=any(a.type=='NODE_EDITOR' and a.spaces.active.node_tree==tree for a in window.screen.areas)
                if token[0]!=previous[0] and active is not None and visible:_target_from_node(scene,active)
                elif token[1:]!=previous[1:]:sync_selection(scene)
                tree.pop('selection_error',None)
            except Exception as error:tree['selection_error']=str(error)
        active=tree.nodes.active
        _SELECTION[pointer]=(active.as_pointer() if active else 0,settings.curve_group,settings.curve_slot)
    for pointer in set(_SELECTION)-live:_SELECTION.pop(pointer,None)
    return .15


def ensure_runtime():
    if not bpy.app.timers.is_registered(_selection_tick):bpy.app.timers.register(_selection_tick,first_interval=.15,persistent=True)


def clear_runtime():
    _SELECTION.clear()
    if bpy.app.timers.is_registered(_selection_tick):bpy.app.timers.unregister(_selection_tick)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='NODE_EDITOR' and area.spaces.active.tree_type==TREE:
                area.spaces.active.node_tree=None;area.spaces.active.tree_type='ShaderNodeTree'


def show(context,tree=None):
    tree=owned(context.scene,tree)
    area=context.area if context.area and context.area.type=='NODE_EDITOR' else next((a for a in context.screen.areas if a.type=='NODE_EDITOR'),None)
    if area is None:
        area=context.area or max(context.screen.areas,key=lambda a:a.width*a.height);area.type='NODE_EDITOR'
    space=area.spaces.active;space.tree_type=TREE;space.pin=True;space.node_tree=tree
    space.show_region_ui=False
    return tree


def set_enum(scene,node_name,prop,item):
    node=owned(scene).nodes.get(node_name);need(node is not None,'Unknown node')
    need(prop=='editor_mode' and node.bl_idname==TOONAUTHOR_Brush.bl_idname,'Unsupported typed enum target')
    definition=node.bl_rna.properties[prop];need(definition.type=='ENUM' and item in {i.identifier for i in definition.enum_items},'Invalid enum item')
    setattr(node,prop,item)


class TOONAUTHOR_OT_graph(bpy.types.Operator):
    bl_idname='toon_viewport.author_graph';bl_label='自主作者图';bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[(n,n,'') for n in ('NEW','SHOW','APPLY','SELECT','CURVES','SYNC','RESET','SOCKET','SHRINK','MOVE','NEW_LINE','ENUM')])
    node:StringProperty();index:IntProperty(default=0);direction:IntProperty(default=1,min=-1,max=1)
    prop_name:StringProperty(default='editor_mode');item:StringProperty(default='DETAILS')
    def execute(self,context):
        scene=context.scene
        try:
            if self.action=='NEW':create(scene)
            elif self.action=='SHOW':show(context)
            elif self.action=='APPLY':apply(scene)
            elif self.action in ('SELECT','CURVES'):
                tree=owned(scene);select(scene,self.node or tree.nodes.active.name,curves=self.action=='CURVES')
            elif self.action=='SYNC':sync_selection(scene)
            elif self.action=='RESET':
                tree=owned(scene)
                for node in tree.nodes:node.select=False
                tree.nodes.active=None
                _SELECTION[scene.as_pointer()]=(0,scene.toon_viewport_preview.curve_group,scene.toon_viewport_preview.curve_slot)
            elif self.action=='ENUM':set_enum(scene,self.node,self.prop_name,self.item)
            elif self.action=='NEW_LINE':owned(scene).nodes.new(TOONAUTHOR_Line.bl_idname)
            else:
                line=owned(scene).nodes.get(self.node);need(line and line.bl_idname==TOONAUTHOR_Line.bl_idname,'Select a Line node')
                if self.action=='SOCKET':line.inputs.new(TOONAUTHOR_Socket.bl_idname,'LineSet '+str(len(line.inputs)+1))
                elif self.action=='SHRINK':
                    for socket in list(line.inputs):
                        if not socket.is_linked:line.inputs.remove(socket)
                elif self.action=='MOVE':
                    target=self.index+self.direction;need(0<=self.index<len(line.inputs) and 0<=target<len(line.inputs),'Invalid socket order');line.inputs.move(self.index,target)
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONAUTHOR_PT_graph(bpy.types.Panel):
    bl_label='自主线条作者';bl_space_type='NODE_EDITOR';bl_region_type='UI';bl_category='作者'
    @classmethod
    def poll(cls,context):return context.space_data.tree_type==TREE
    def draw(self,context):
        layout=self.layout;layout.template_ID(context.scene.toon_author_files,'author_tree')
        row=layout.row(align=True)
        for action,label in [('NEW','从文档新建'),('SHOW','选择默认图'),('APPLY','应用作者图')]:row.operator('toon_viewport.author_graph',text=label).action=action
        row=layout.row(align=True)
        for action,label in [('SELECT','激活节点'),('SYNC','同步组选中'),('RESET','清除选择')]:row.operator('toon_viewport.author_graph',text=label).action=action
        layout.operator('toon_viewport.author_graph',text='增加Line').action='NEW_LINE'
        layout.label(text='连线与Reroute在应用后参与绘线')


def draw_add(self,context):
    if context.space_data and context.space_data.type=='NODE_EDITOR' and context.space_data.tree_type==TREE:
        for cls in (TOONAUTHOR_Line,TOONAUTHOR_LineSet,TOONAUTHOR_Brush):
            op=self.layout.operator('node.add_node',text=cls.bl_label);op.type=cls.bl_idname;op.use_transform=True
        op=self.layout.operator('node.add_node',text='Reroute');op.type='NodeReroute';op.use_transform=True


def draw_header(self,context):
    if context.space_data and context.space_data.type=='NODE_EDITOR' and context.space_data.tree_type==TREE:
        author_channels.draw(self.layout,context.scene.toon_viewport_preview)
        self.layout.operator('toon_viewport.author_graph',text='应用作者图',icon='CHECKMARK').action='APPLY'
        self.layout.operator('toon_viewport.author_graph',text='同步选择',icon='RESTRICT_SELECT_OFF').action='SYNC'


CLASSES=(TOONAUTHOR_Tree,TOONAUTHOR_Socket,TOONAUTHOR_Line,TOONAUTHOR_LineSet,TOONAUTHOR_Brush,TOONAUTHOR_OT_graph,TOONAUTHOR_PT_graph)
def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.NODE_MT_add.append(draw_add)
    bpy.types.NODE_HT_header.append(draw_header)
    ensure_runtime()
def unregister():
    clear_runtime()
    bpy.types.NODE_MT_add.remove(draw_add)
    bpy.types.NODE_HT_header.remove(draw_header)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
