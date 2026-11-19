# SPDX-License-Identifier: GPL-3.0-or-later
"""Stable named typed Scene tracks. Exact author JSON remains immutable."""
import contextlib,json,math,struct
import bpy
from bpy.props import StringProperty,BoolProperty,FloatProperty,IntProperty,EnumProperty
from . import amount_animation as A,parameter_overlay as O
ROOT='toon_viewport_preview.parameter_tracks';MAX_TRACKS=4096
SUFFIX={'FLOAT':'float_value','INT':'int_value','BOOL':'bool_value'}
class ParameterTrack(bpy.types.PropertyGroup):
    identity:StringProperty(options=set())
    kind:EnumProperty(items=[(k,k,'') for k in SUFFIX],options=set())
    enabled:BoolProperty(default=False,options=set())
    float_value:FloatProperty(name='动画值',default=0,min=-1e12,max=1e12,options={'ANIMATABLE'})
    int_value:IntProperty(name='动画整数',default=0,options={'ANIMATABLE'})
    bool_value:BoolProperty(name='动画开关',default=False,options={'ANIMATABLE'})
CLASSES=(ParameterTrack,)
def rows(scene):
    collection=scene.toon_viewport_preview.parameter_tracks;O.need(len(collection)<=MAX_TRACKS,'Too many parameter tracks');result={}
    for item in collection:
        try:t=json.loads(item.identity)
        except Exception as error:raise ValueError('Invalid parameter track metadata') from error
        t=O.target(t['scope'],t['owner'],t['path'],t['component'])
        O.need(item.name==O.token(t) and item.name not in result and item.identity==O.canonical(t),'Renamed, duplicate or redirected parameter track')
        result[item.name]=(item,t)
    return result
def key_path(t,kind):return ROOT+'["'+O.token(t)+'"].'+SUFFIX[kind]
def _graph(scene):
    action,slot,bag,unused=A._graph(scene);registry=rows(scene)
    paths={key_path(t,item.kind):(key,item) for key,(item,t) in registry.items()};found={}
    if bag is not None:
        for fc in bag.fcurves:
            if not fc.data_path.startswith(ROOT):continue
            O.need(fc.data_path in paths and fc.array_index==0,'Indexed, orphan or metadata parameter curve')
            key,item=paths[fc.data_path];O.need(key not in found,'Duplicate parameter curve')
            O.need(not fc.modifiers and not fc.sampled_points and not fc.mute and (fc.group is None or not fc.group.mute),'Parameter modifiers, samples and muted channels unsupported')
            O.need(fc.extrapolation=='CONSTANT','Parameter animation requires CONSTANT extrapolation')
            times=set()
            for point in fc.keyframe_points:
                x,y=point.co;O.need(math.isfinite(x) and math.isfinite(y) and x not in times,'Invalid parameter key');times.add(x)
                O.need(point.interpolation==('LINEAR' if item.kind=='FLOAT' else 'CONSTANT'),'Float keys require LINEAR; bool/int keys require CONSTANT')
                if item.kind=='BOOL':O.need(y in (0.,1.),'Boolean key must be exactly 0 or 1')
                if item.kind=='INT':O.need(y==int(y) and -2147483648<=y<=2147483647,'Integer key outside exact RNA range')
            found[key]=fc
    return action,slot,bag,found
def validate_registry(scene,document):
    for item,t in rows(scene).values():
        if item.enabled:O.need(O.describe(document,t)['kind']==item.kind,'Animated parameter type changed')
def resolve(scene,document,*,evaluated_scene=None):
    registry=rows(scene);enabled=[(key,item,t) for key,(item,t) in registry.items() if item.enabled]
    if not enabled:return document
    validate_registry(scene,document);_graph(scene);values=registry
    if evaluated_scene is not None:
        original=evaluated_scene.original if evaluated_scene.is_evaluated else evaluated_scene
        O.need(original==scene,'Evaluated parameter Scene differs from author Scene')
        values=rows(evaluated_scene);O.need(set(values)==set(registry),'Evaluated parameter identities differ')
        O.need(all(values[k][0].identity==r[0].identity and values[k][0].kind==r[0].kind and values[k][0].enabled==r[0].enabled for k,r in registry.items()),'Evaluated parameter metadata differs')
    return O.apply(document,[(t,getattr(values[key][0],SUFFIX[item.kind])) for key,item,t in enabled])
def signature(scene):
    registry=rows(scene)
    if any(item.enabled for item,t in registry.values()):_graph(scene)
    return tuple((key,item.identity,item.kind,bool(item.enabled),getattr(item,SUFFIX[item.kind])) for key,(item,t) in sorted(registry.items()))
def receipt(scene,*,evaluated_scene=None):
    source=scene if evaluated_scene is None else evaluated_scene
    return dict(frame=int(source.frame_current),subframe=float(source.frame_subframe),evaluation='DEPSGRAPH_SCENE_EVAL' if evaluated_scene is not None else 'SCENE',targets=[dict(**t,kind=item.kind,value=getattr(item,SUFFIX[item.kind])) for item,t in rows(source).values() if item.enabled])
def _static(scene):
    from .addon import _preset
    return _preset(scene)['raw']
@contextlib.contextmanager
def _edit(scene,t,kind,*,action_edit):
    """Same slot/COW rules as amount, with typed target-row rollback."""
    collection=scene.toon_viewport_preview.parameter_tracks;key=O.token(t);item=collection.get(key)
    state=None if item is None else (item.identity,item.kind,item.enabled,item.float_value,item.int_value,item.bool_value)
    had_ad=scene.animation_data is not None;ad=scene.animation_data;old_action=None if ad is None else ad.action;old_slot=None if ad is None else ad.action_slot
    old_token=None if old_slot is None else A._token(old_slot);old_curve=None if not action_edit else _graph(scene)[3].get(key)
    old_keys=None if old_curve is None else [A._key_state(p) for p in old_curve.keyframe_points]
    fc_state=None if old_curve is None else {n:getattr(old_curve,n) for n in ('extrapolation','mute','lock','hide','select','color_mode','auto_smoothing')}
    old_color=None if old_curve is None else tuple(old_curve.color);old_group='' if old_curve is None or old_curve.group is None else old_curve.group.name;replacement=None
    try:
        if action_edit:
            ad=scene.animation_data_create()
            if old_action is None:replacement=bpy.data.actions.new('ParameterAnimation_'+scene.name)
            elif old_action.users>1 or old_slot is None:replacement=old_action.copy()
            if replacement is not None:
                matched=None if old_token is None else A._matching_slot(replacement,old_token);ad.action=replacement
                if matched is not None:ad.action_slot=matched;O.need(A._token(ad.action_slot)==old_token,'Parameter SCENE slot rebinding failed')
        yield
    except Exception:
        if replacement is not None:
            ad.action=old_action
            if old_token is not None:ad.action_slot=A._matching_slot(old_action,old_token)
            bpy.data.actions.remove(replacement)
            if not had_ad:scene.animation_data_clear()
        elif action_edit and old_action is not None:
            bag=old_action.layers[0].strips[0].channelbag(old_slot)
            current=next((fc for fc in bag.fcurves if fc.data_path==key_path(t,kind) and fc.array_index==0),None)
            if old_keys is None:
                if current is not None:bag.fcurves.remove(current)
            else:
                if current is None:current=old_action.fcurve_ensure_for_datablock(scene,data_path=key_path(t,kind),index=0,group_name=old_group)
                for n,v in fc_state.items():setattr(current,n,v)
                current.color=old_color;A._restore_keys(current,old_keys)
        item=collection.get(key)
        if state is None:
            if item is not None:collection.remove(next(i for i,r in enumerate(collection) if r.as_pointer()==item.as_pointer()))
        elif item is not None:item.identity,item.kind,item.enabled,item.float_value,item.int_value,item.bool_value=state
        raise
def _author(scene,t):
    A._local(scene);document=_static(scene);validate_registry(scene,document);spec=O.describe(document,t);_graph(scene)
    O.need(O.token(t) in rows(scene) or len(rows(scene))<MAX_TRACKS,'Too many parameter tracks')
    return document,spec
def _new(scene,t,kind,value):
    row=scene.toon_viewport_preview.parameter_tracks.add();row.name=O.token(t);row.identity=O.canonical(t);row.kind=kind;setattr(row,SUFFIX[kind],value);return row
def set_key(scene,t,value,*,frame=None):
    document,spec=_author(scene,t);value=O.value(spec,value)
    # Validate a complete simultaneous pose: preserve other enabled overrides.
    effective=resolve(scene,document);O.apply(effective,[(t,value)])
    if spec['kind']=='INT':O.need(int(struct.unpack('f',struct.pack('f',value))[0])==value,'Integer key is not exactly representable by Blender FCurve float32')
    time=A._frame(scene,frame);key=O.token(t);existing=_graph(scene)[3].get(key);O.need(existing is None or not existing.lock,'Parameter curve is locked')
    with _edit(scene,t,spec['kind'],action_edit=True):
        item=scene.toon_viewport_preview.parameter_tracks.get(key) or _new(scene,t,spec['kind'],value)
        item.enabled=True;setattr(item,SUFFIX[item.kind],value)
        fc=scene.animation_data.action.fcurve_ensure_for_datablock(scene,data_path=key_path(t,item.kind),index=0)
        untouched={float(p.co.x):A._key_state(p) for p in fc.keyframe_points if p.co.x!=time}
        point=next((p for p in fc.keyframe_points if p.co.x==time),None) or fc.keyframe_points.insert(time,value)
        point.co=(time,float(value));point.interpolation='LINEAR' if item.kind=='FLOAT' else 'CONSTANT';fc.extrapolation='CONSTANT';A._finish_keys(fc,untouched);_graph(scene);resolve(scene,document)
    return item
def set_enabled(scene,t,enabled):
    O.need(type(enabled) is bool,'Parameter enabled must be boolean');A._local(scene);key=O.token(t);item=rows(scene).get(key)
    if not enabled:
        if item is not None:item[0].enabled=False
        return
    document,spec=_author(scene,t)
    with _edit(scene,t,spec['kind'],action_edit=False):
        row=scene.toon_viewport_preview.parameter_tracks.get(key) or _new(scene,t,spec['kind'],spec['static']);row.enabled=True;resolve(scene,document)
def remove_key(scene,t,*,frame=None):
    document,spec=_author(scene,t);time=A._frame(scene,frame);key=O.token(t);fc=_graph(scene)[3].get(key)
    O.need(fc is not None and not fc.lock,'No editable parameter curve');O.need(any(p.co.x==time for p in fc.keyframe_points),'No parameter key at the exact current frame')
    with _edit(scene,t,spec['kind'],action_edit=True):
        fc=_graph(scene)[3][key];untouched={float(p.co.x):A._key_state(p) for p in fc.keyframe_points if p.co.x!=time};fc.keyframe_points.remove(next(p for p in fc.keyframe_points if p.co.x==time))
        if fc.keyframe_points:A._finish_keys(fc,untouched)
        else:_graph(scene)[2].fcurves.remove(fc)
        _graph(scene)
