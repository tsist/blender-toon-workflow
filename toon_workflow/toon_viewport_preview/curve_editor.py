# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned viewport curve editing using the existing C4 state codec.

CurveMapping control points are restored, never reconstructed from samples.
Boundary and intersection authors share actual-brush identity. Preserved native
intersection controls seed a separate owned author record; provenance is immutable.
"""
import copy
import hashlib
import json
import uuid

_CODEC=None
_ENUMS={}
KEYS=('size_curve','alpha_curve')
ROLE='toon.viewport-curve-editor.v1'
INTERSECTION='intersection_brush'

def codec():
    global _CODEC
    if _CODEC is None:
        from .dependencies import load
        _CODEC=load('paint_editor_state')
    return _CODEC

def group_items(settings,context):
    items=[(g.name,g.label or g.name,'') for g in settings.groups]
    if not items:items=[('__none__','请先载入配置','')]
    token=tuple(items)
    # Keep dynamic EnumProperty strings alive, including previously returned items.
    return _ENUMS.setdefault(token,items)


def slot_items(settings,context):
    """Keep legacy saved enum numbers and expose retained source authors by stable ID."""
    items=[('brush','基础笔刷','',0),('silhouette_brush','轮廓笔刷','',1),
           ('smoothing_brush','平滑笔刷','',2),('marked_brush','标记笔刷','',3),
           ('intersection_brush','原交线笔刷','',4),('wireframe_visible_brush','原线框可见笔刷','',5),
           ('wireframe_hidden_brush','原线框隐藏笔刷','',6)]
    scene=settings.id_data
    if settings.preset_text is not None:
        from . import addon,ix_sources
        plan=addon._preset(scene);gid=settings.curve_group
        group=next((g for g in plan['groups'] if g['id']==gid),None)
        extra={}
        if group is not None:
            extra.update((s,'边界 · '+s[5:13]) for s in group['brush_slots'] if s.startswith('edge_'))
        record=ix_sources.records(plan['raw']).get(gid)
        if record is not None:
            extra.update((s,'交线 · '+a['source_brush']) for s,a in record['authors'].items())
        values={i[3] for i in items}
        for slot,label in sorted(extra.items()):
            number=1024+(int(hashlib.sha256(slot.encode('utf8')).hexdigest()[:8],16)&0x3fffffff)
            if number in values:raise ValueError('曲线选择身份冲突，请保留配置并重新选择来源')
            values.add(number);items.append((slot,label,'编辑此实际笔刷的共享曲线',number))
    token=tuple(items)
    return _ENUMS.setdefault(token,items)

def _identity(plan,gid,slot):
    from . import ix_sources
    record=ix_sources.records(plan['raw']).get(gid)
    if record is not None and slot in record['authors']:
        return record['authors'][slot]['actual']
    if slot==INTERSECTION:
        aliases={a['group']:a['brush_id'] for a in plan['raw']['request'].get('intersection_aliases',[])}
        if gid not in aliases:raise ValueError('交线笔刷缺少可追溯身份，不能反推作者控制点')
        return aliases[gid]
    aliases={(a['group'],a['slot']):a['brush_id']
        for a in plan['raw']['request'].get('boundary_aliases',[])}
    if (gid,slot) in aliases:return aliases[gid,slot]
    group=next(g for g in plan['groups'] if g['id']==gid)
    return 'editor/'+(group['brush_meta'][slot]['uid'] or gid+'/'+slot)

def targets(plan,gid,slot):
    identity=_identity(plan,gid,slot)
    rows=[(g['id'],s) for g in plan['groups'] for s in g['brush_slots']
        if _identity(plan,g['id'],s)==identity]
    # Archived consumers retain shared native actual authors. Editing an active
    # consumer must update those authors too, so restoring cannot revive stale curves.
    rows.extend((a['group'],a['slot']) for a in plan['raw']['request'].get('boundary_aliases',[])
                if a['brush_id']==identity and (a['group'],a['slot']) not in rows)
    from . import ix_sources
    rows.extend((gid,s) for gid,row in ix_sources.records(plan['raw']).items()
                for s,author in row['authors'].items() if author['actual']==identity)
    raw=plan['raw']['request'].get('intersection')
    if raw is not None:
        aliases={a['group']:a['brush_id'] for a in plan['raw']['request'].get('intersection_aliases',[])}
        rows.extend((g['id'],INTERSECTION) for g in raw['groups'] if aliases.get(g['id'])==identity)
    if not rows:raise ValueError('当前配置没有此笔刷')
    return identity,rows

def _intersection_author(plan,gid,wrapped):
    identity=_identity(plan,gid,INTERSECTION)
    raw=plan['raw']['request'].get('intersection')
    groups={g['id']:g for g in raw['groups']} if raw else {}
    if gid not in groups:raise ValueError('当前分组没有独立交线笔刷')
    numeric=groups[gid]['brush']
    saved=wrapped.get('intersection_curve_authors',{}).get(identity)
    if saved is not None:
        result=copy.deepcopy(saved)
    else:
        # Same identity formula as joint_document and joint_authoring_layer.
        full=plan['raw']['inputs']['full_preset']
        def digest(name):
            body=(json.dumps([full['source'],name],ensure_ascii=False,allow_nan=False,
                            sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
            return 'native-brush-'+hashlib.sha256(body).hexdigest()
        names=[name for name in full['brushes'] if digest(name)==identity]
        if len(names)!=1:raise ValueError('交线身份不能唯一绑定原笔刷，请提供完整作者配置')
        detail=full['details'].get(full['brushes'][names[0]]['detail'])
        if not isinstance(detail,dict):raise ValueError('交线笔刷缺少原细节控制点')
        result={}
        for key in KEYS:
            curve=detail.get(key)
            if not isinstance(curve,dict) or curve.get('evidence')!='CALLER_CAPTURE_POINTS_VERIFIED' or not curve.get('mapping') or not curve.get('points'):
                raise ValueError('交线笔刷没有已核验的原控制点，不能从采样表反推')
            result[key]=dict(enabled=numeric[key] is not None,points=copy.deepcopy(curve['points']),
                samples=copy.deepcopy(curve['samples']),**copy.deepcopy(curve['mapping']))
    for key in KEYS:
        codec()._curve(result[key])
        projected=result[key]['samples'] if result[key]['enabled'] else None
        if projected!=numeric[key]:raise ValueError('交线作者曲线与当前数值配置不一致，请重新载入完整配置')
    return result

def _author(plan,gid,slot,wrapped):
    if slot==INTERSECTION:return _intersection_author(plan,gid,wrapped)
    from . import ix_sources
    record=ix_sources.records(plan['raw']).get(gid)
    if record is not None and slot in record['authors']:
        return copy.deepcopy(record['authors'][slot]['curves'])
    editor={g['uid']:g for g in plan['raw']['inputs']['projection']['editor_document']['groups']}
    brush=editor.get(gid,{}).get('brushes',{}).get(slot)
    if brush is None:raise ValueError('此笔刷没有完整作者控制点')
    return {key:brush[key] for key in KEYS}

def _owned(settings):
    tree=settings.curve_tree
    if tree is None:return None
    if tree.library is not None:raise ValueError('链接曲线不可编辑，请在本地工作副本中重新载入')
    if tree.get('toon_viewport_curve_role')!=ROLE:raise ValueError('曲线辅助数据不属于本预览')
    if tree.users>1:raise ValueError('曲线被其他场景共享，请重新载入后独立编辑')
    if any(k not in tree.nodes or tree.nodes[k].bl_idname!='ShaderNodeFloatCurve' for k in KEYS):
        raise ValueError('曲线辅助数据缺失，请重新载入')
    return tree

def signature(tree,settings):
    result=[]
    for key in KEYS:
        m=tree.nodes[key].mapping
        result.append(dict(enabled=bool(getattr(settings,'curve_'+key+'_enabled')),
            use_clip=m.use_clip,clip=[m.clip_min_x,m.clip_min_y,m.clip_max_x,m.clip_max_y],
            extend=m.extend,tone=m.tone,black=list(m.black_level),white=list(m.white_level),
            points=[(float(p.location.x),float(p.location.y),p.handle_type) for p in m.curves[0].points]))
    return json.dumps(result,allow_nan=False,separators=(',',':'))

def detach(scene):
    import bpy
    settings=scene.toon_viewport_preview
    tree=settings.curve_tree
    settings.curve_tree=None;settings.curve_error=''
    if tree is not None and tree.get('toon_viewport_curve_role')==ROLE and tree.users==0:
        bpy.data.node_groups.remove(tree)

def commit_pending(scene):
    """Do not discard a user's edit when a load click beats the timer."""
    settings=scene.toon_viewport_preview;tree=settings.curve_tree
    if tree is not None and tree.get('toon_viewport_curve_role')==ROLE:
        if signature(tree,settings)!=tree.get('last_signature'):
            synchronize(scene)

def initialize(scene,*,discard_pending=False):
    import bpy
    from . import addon
    settings=scene.toon_viewport_preview
    if scene.library is not None or (settings.preset_text is not None and settings.preset_text.library is not None):
        raise ValueError('请在本地工作副本中编辑曲线')
    if not discard_pending:commit_pending(scene)
    plan=addon._preset(scene);gid=settings.curve_group;slot=settings.curve_slot
    group=next(g for g in plan['groups'] if g['id']==gid)
    identity,rows=targets(plan,gid,slot)
    wrapped=json.loads(settings.preset_text.as_string())
    original=_author(plan,gid,slot,wrapped)
    for key in KEYS:
        if original.get(key) is None or not original[key].get('points'):
            raise ValueError('此笔刷没有完整原控制点，不能从采样表反推曲线')
        for target_gid,target_slot in rows:
            if _author(plan,target_gid,target_slot,wrapped)[key]!=original[key]:
                raise ValueError('共享笔刷的原曲线不一致，须先修复配置')
    previous=settings.curve_tree
    previous_flags={key:bool(getattr(settings,'curve_'+key+'_enabled')) for key in KEYS}
    tree=bpy.data.node_groups.new('.toon_viewport_curves.'+uuid.uuid4().hex,'ShaderNodeTree')
    try:
        tree['toon_viewport_curve_role']=ROLE
        for key in KEYS:
            node=tree.nodes.new('ShaderNodeFloatCurve');node.name=key
            codec()._restore_curve(node.mapping,original[key])
        tree['identity']=identity
        tree['selected_group']=gid;tree['selected_slot']=slot
        tree['targets']=json.dumps(rows)
        tree['preset_text']=settings.preset_text.name
        tree['preset_revision']=settings.preset_revision
        tree['original']=json.dumps({key:original[key] for key in KEYS},allow_nan=False)
        tree['label']=(group['label'] or gid)+' / '+slot
        settings.curve_tree=tree
        for key in KEYS:setattr(settings,'curve_'+key+'_enabled',original[key]['enabled'])
        tree['last_signature']=signature(tree,settings)
        settings.curve_error=''
        if previous is not None and previous.get('toon_viewport_curve_role')==ROLE and previous.users==0:
            bpy.data.node_groups.remove(previous)
    except Exception:
        if settings.curve_tree==tree:settings.curve_tree=previous
        for key,value in previous_flags.items():setattr(settings,'curve_'+key+'_enabled',value)
        if tree.users==0:bpy.data.node_groups.remove(tree)
        raise
    return dict(identity=identity,targets=rows,control_points_restored_exact=True)

def _patch(wrapped,rows,payload,identity):
    document=wrapped['document']
    editor=document['inputs']['projection']['editor_document']
    groups={g['uid']:g for g in editor['groups']}
    boundary={g['id']:g for g in document['request']['boundary']['groups']}
    intersection=document['request'].get('intersection')
    intersection_groups={g['id']:g for g in intersection['groups']} if intersection else {}
    boundary_changed=False
    for gid,slot in rows:
        records=document['request'].get('intersection_sources',{}).get('groups',[])
        record=next((r for r in records if r['group']==gid and slot in r['authors']),None)
        if record is not None:
            author=record['authors'][slot]
            author['curves']=copy.deepcopy(payload)
            for key in KEYS:
                author['brush'][key]=copy.deepcopy(payload[key]['samples']) if payload[key]['enabled'] else None
            continue
        if slot==INTERSECTION:
            for key in KEYS:
                intersection_groups[gid]['brush'][key]=copy.deepcopy(payload[key]['samples']) if payload[key]['enabled'] else None
            wrapped.setdefault('intersection_curve_authors',{})[identity]=copy.deepcopy(payload)
            continue
        boundary_changed=True
        brush=groups[gid]['brushes'][slot]
        for key in KEYS:
            value=copy.deepcopy(brush[key]);value.update(copy.deepcopy(payload[key]))
            brush[key]=value
            if gid in boundary and isinstance(boundary[gid].get(slot),dict):
                boundary[gid][slot][key]=copy.deepcopy(value['samples']) if value['enabled'] else None
    if boundary_changed:editor['revision']=int(editor['revision'])+1

def _check_target(settings,tree,allow_previous_target=False):
    if not allow_previous_target and (tree.get('selected_group')!=settings.curve_group or tree.get('selected_slot')!=settings.curve_slot):
        raise ValueError('当前选择已改变，请重新载入曲线后编辑')
    if settings.preset_text is None or settings.preset_text.name!=tree['preset_text'] or settings.preset_revision!=tree['preset_revision']:
        raise ValueError('预设已改变，请重新载入曲线后编辑')
    if not settings.preset_text.get('toon_viewport_preview_owned'):
        raise ValueError('配置不是本预览的独立工作副本')

def synchronize(scene,*,allow_previous_target=False):
    from . import addon
    settings=scene.toon_viewport_preview
    if scene.library is not None or (settings.preset_text is not None and settings.preset_text.library is not None):
        raise ValueError('链接场景或配置不可写，请先建立本地工作副本')
    tree=_owned(settings)
    if tree is None:return False
    sig=signature(tree,settings)
    if sig==tree['last_signature']:return False
    _check_target(settings,tree,allow_previous_target)
    text=settings.preset_text
    old_body=text.as_string();wrapped=json.loads(old_body)
    rows=json.loads(tree['targets'])
    payload={key:codec()._capture_curve(tree.nodes[key].mapping,
        bool(getattr(settings,'curve_'+key+'_enabled'))) for key in KEYS}
    # Reuse codec validation for finite points/sample range and supported handles.
    for value in payload.values():codec()._curve(value)
    _patch(wrapped,rows,payload,tree['identity'])
    plan=addon.compile_preset(wrapped['document'],wrapped['recipe'])
    body=json.dumps(wrapped,ensure_ascii=False,allow_nan=False,indent=2)
    if settings.preset_revision>=2**31-1 or settings.curve_revision>=2**31-1:
        raise ValueError('曲线修订计数已用尽，请载入新配置')
    old_text=text
    old_revision=settings.preset_revision;old_curve_revision=settings.curve_revision
    old_meta={key:tree[key] for key in ('preset_text','preset_revision','last_signature')}
    detached=None;body_written=False
    try:
        if text.users-int(text.use_fake_user)>1:
            import bpy
            detached=bpy.data.texts.new('.toon_viewport_style.'+uuid.uuid4().hex)
            detached.use_fake_user=False;detached['toon_viewport_preview_reclaimable']=True
            detached.from_string(body);detached['toon_viewport_preview_owned']=True
            settings.preset_text=text=detached
        else:
            body_written=True
            text.from_string(body)
        settings.preset_revision=old_revision+1;settings.curve_revision=old_curve_revision+1
        tree['preset_text']=text.name;tree['preset_revision']=settings.preset_revision
        tree['last_signature']=sig
    except Exception:
        settings.preset_text=old_text
        if body_written:old_text.from_string(old_body)
        settings.preset_revision=old_revision;settings.curve_revision=old_curve_revision
        for key,value in old_meta.items():tree[key]=value
        if detached is not None and detached.users==0:
            import bpy
            bpy.data.texts.remove(detached)
        raise
    addon._style_cache.clear()
    addon._style_cache[(scene.as_pointer(),text.as_pointer(),settings.preset_revision)]=plan
    settings.curve_error=''
    addon._touch(settings,None)
    return True

def target_changed(scene):
    settings=scene.toon_viewport_preview
    tree=_owned(settings)
    if tree is None:return
    if tree.get('selected_group')==settings.curve_group and tree.get('selected_slot')==settings.curve_slot:return
    synchronize(scene,allow_previous_target=True)
    detach(scene)

def restore(scene):
    settings=scene.toon_viewport_preview
    if scene.library is not None or (settings.preset_text is not None and settings.preset_text.library is not None):
        raise ValueError('链接场景或配置不可写，请先建立本地工作副本')
    tree=_owned(settings)
    if tree is None:raise ValueError('请先载入曲线')
    _check_target(settings,tree)
    original=json.loads(tree['original'])
    previous={key:codec()._capture_curve(tree.nodes[key].mapping,
        bool(getattr(settings,'curve_'+key+'_enabled'))) for key in KEYS}
    def apply(payload):
        # The codec requires two initial endpoints; fresh nodes preserve handles.
        for key in KEYS:
            old=tree.nodes[key];tree.nodes.remove(old)
            node=tree.nodes.new('ShaderNodeFloatCurve');node.name=key
            codec()._restore_curve(node.mapping,payload[key])
            setattr(settings,'curve_'+key+'_enabled',payload[key]['enabled'])
    try:
        for value in original.values():codec()._curve(value)
        apply(original)
        return synchronize(scene)
    except Exception:
        apply(previous)
        raise

def draw(layout,scene):
    settings=scene.toon_viewport_preview
    box=layout.box()
    box.prop(settings,'show_curves',text='笔刷曲线')
    if not settings.show_curves:return
    box.prop(settings,'curve_group',text='分组');box.prop(settings,'curve_slot',text='笔刷')
    box.operator('toon_viewport.edit_curves',text='载入原控制点')
    if settings.curve_error:
        box.label(text=settings.curve_error,icon='ERROR')
        if settings.curve_tree is not None:box.operator('toon_viewport.discard_curves',text='放弃未应用修改并重载')
    try:tree=_owned(settings)
    except ValueError as error:
        box.label(text=str(error),icon='ERROR');return
    if tree is None:return
    if tree.get('selected_group')!=settings.curve_group or tree.get('selected_slot')!=settings.curve_slot:
        box.label(text='请载入当前选择的原控制点');return
    box.label(text=tree.get('label','当前笔刷'))
    for key,label in (('size_curve','线宽变化曲线'),('alpha_curve','不透明度变化曲线')):
        box.prop(settings,'curve_'+key+'_enabled',text=label)
        if getattr(settings,'curve_'+key+'_enabled'):
            box.template_curve_mapping(tree.nodes[key],'mapping',type='NONE')
    box.operator('toon_viewport.restore_curves',text='恢复载入时曲线')
    box.label(text='自动预览 · 修订 '+str(settings.curve_revision))
    box.label(text='按实际笔刷共享曲线；交线可独立编辑')
