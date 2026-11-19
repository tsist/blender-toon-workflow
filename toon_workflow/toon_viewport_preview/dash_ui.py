# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded nonanimated RNA proxies for exact autonomous dash author JSON."""
import copy
import struct
from collections import OrderedDict
from contextlib import contextmanager
import bpy
from bpy.props import EnumProperty,FloatProperty,StringProperty
from . import chain_ui
from . import dash_controls as controls

_GUARDS={}
_SYNC=OrderedDict()
_RUNTIME=OrderedDict()
_PENDING={}
_FAILED=OrderedDict()
_EPOCH=0
_FIELDS=('mode','dash_length_px','gap_length_px')

def _settings(scene):return scene.toon_viewport_preview
def _authorable(scene):return scene is not None and scene.library is None and scene.is_editable
def _require_authorable(scene):
    if not _authorable(scene):raise ValueError('linked Scene dash controls are read-only')
def _plan(scene):
    from . import addon
    return addon._preset(scene)
def _remember(cache,key,value):
    cache[key]=value;cache.move_to_end(key)
    while len(cache)>4:cache.popitem(last=False)

def clear_runtime_cache():
    global _EPOCH
    _EPOCH+=1;callbacks=list(_PENDING.values());_PENDING.clear()
    for callback in callbacks:
        if bpy.app.timers.is_registered(callback):bpy.app.timers.unregister(callback)
    _SYNC.clear();_RUNTIME.clear();_FAILED.clear();_GUARDS.clear()

@contextmanager
def _guard(scene):
    key=scene.as_pointer();_GUARDS[key]=_GUARDS.get(key,0)+1
    try:yield
    finally:
        _GUARDS[key]-=1
        if not _GUARDS[key]:_GUARDS.pop(key)

def read_controls(scene,plan=None):
    raw=_settings(scene).dash_patterns_json
    if not raw:return None,None
    plan=_plan(scene) if plan is None else plan
    bindings=chain_ui._recover(scene,plan)
    return copy.deepcopy(bindings),controls.normalize(bindings,chain_ui._decode(raw))

def _settings_copy(scene,plan):
    settings=_settings(scene);raw=settings.dash_patterns_json
    if not raw:return {}
    key=scene.as_pointer();stamp=(id(plan),settings.preset_revision,raw)
    cached=_RUNTIME.get(key)
    if cached is None or cached[0]!=stamp:
        unused,payload=read_controls(scene,plan);_remember(_RUNTIME,key,(stamp,payload['patterns']))
    return copy.deepcopy(_RUNTIME[key][1])

def _rows(settings):
    return [dict(name=item.name,label=item.label,**{field:getattr(item,field) for field in _FIELDS}) for item in settings.dashes]
def _display(bindings,payload):
    return [dict(name=actual,label=bindings['actuals'][actual]['source_brush_key'],**row) for actual,row in payload['patterns'].items()]
def _apply(settings,rows):
    if {item.name for item in settings.dashes}!={row['name'] for row in rows} or len(settings.dashes)!=len(rows):
        settings.dashes.clear()
        for row in rows:
            item=settings.dashes.add();item.name=row['name']
    for row in rows:
        item=settings.dashes[row['name']];item.label=row['label']
        for field in _FIELDS:
            if getattr(item,field)!=row[field]:setattr(item,field,row[field])
def _stamp(scene,plan):
    s=_settings(scene)
    return (id(plan),s.preset_revision,s.dash_patterns_json,tuple(item.name for item in s.dashes))
def _resync(scene,plan,bindings,payload):
    _require_authorable(scene)
    with _guard(scene):_apply(_settings(scene),_display(bindings,payload))
    _remember(_SYNC,scene.as_pointer(),_stamp(scene,plan))
def _commit(scene,plan,bindings,payload):
    _require_authorable(scene);payload=controls.normalize(bindings,payload)
    raw=chain_ui._canonical(payload);s=_settings(scene);old_raw=s.dash_patterns_json;old_rows=_rows(s)
    try:
        with _guard(scene):
            _apply(s,_display(bindings,payload))
            if s.dash_patterns_json!=raw:s.dash_patterns_json=raw
    except Exception:
        with _guard(scene):
            if s.dash_patterns_json!=old_raw:s.dash_patterns_json=old_raw
            _apply(s,old_rows)
        _SYNC.pop(scene.as_pointer(),None);raise
    s.dash_error='';_FAILED.pop(scene.as_pointer(),None)
    _remember(_SYNC,scene.as_pointer(),_stamp(scene,plan));_RUNTIME.pop(scene.as_pointer(),None)
    return payload
def initialize(scene,plan,*,previous=None,explicit=None):
    bindings=chain_ui._recover(scene,plan)
    payload=controls.normalize(bindings,explicit) if explicit is not None else controls.merge(bindings,previous)
    return copy.deepcopy(bindings),_commit(scene,plan,bindings,payload)
def _message(error):
    if 'read-only' in str(error):return '链接场景的虚线参数只读。'
    if 'actual' in str(error) or 'traceable' in str(error):return '当前笔刷缺少完整配置身份，请重新载入虚线参数。'
    return '线段长度须为0.25到65536像素，间隔须为0到65536像素；原值已保留。'
def _error(scene,error):
    message=_message(error)
    if _authorable(scene):_settings(scene).dash_error=message
    return message
def _edited(item,context,field):
    scene=item.id_data
    if not _authorable(scene) or _GUARDS.get(scene.as_pointer(),0):return
    payload=bindings=plan=None
    try:
        plan=_plan(scene);bindings,payload=read_controls(scene,plan)
        if payload is None:raise ValueError('dash actual author state not initialized')
        if item.name not in payload['patterns']:raise ValueError('unknown dash actual')
        proposed=copy.deepcopy(payload);row=proposed['patterns'][item.name]
        value=getattr(item,field)
        displayed=row[field] if field=='mode' else struct.unpack('f',struct.pack('f',row[field]))[0]
        if value==displayed:return
        row[field]=value if field=='mode' else float(value)
        _commit(scene,plan,bindings,proposed)
    except Exception as error:
        if payload is not None:
            try:_resync(scene,plan,bindings,payload)
            except Exception:_SYNC.pop(scene.as_pointer(),None)
        _error(scene,error)
    chain_ui._redraw()
def _update(field):return lambda item,context:_edited(item,context,field)

class DashBrush(bpy.types.PropertyGroup):
    label:StringProperty(default='',options=set())
    mode:EnumProperty(name='样式',items=[('SOLID','实线',''),('DASHED','虚线','')],default='SOLID',options=set(),update=_update('mode'))
    dash_length_px:FloatProperty(name='长度（像素）',default=12,min=.25,max=65536,options=set(),update=_update('dash_length_px'))
    gap_length_px:FloatProperty(name='间隔（像素）',default=6,min=0,max=65536,options=set(),update=_update('gap_length_px'))

class TOONVIEWPORT_OT_prepare_dashes(bpy.types.Operator):
    bl_idname='toon_viewport.prepare_dashes';bl_label='载入虚线参数';bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _authorable(context.scene)
    def execute(self,context):
        try:
            raw=_settings(context.scene).dash_patterns_json
            initialize(context.scene,_plan(context.scene),previous=chain_ui._decode(raw) if raw else None)
            chain_ui._redraw();return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},_error(context.scene,error));return {'CANCELLED'}

class TOONVIEWPORT_OT_restore_dash(bpy.types.Operator):
    bl_idname='toon_viewport.restore_dash';bl_label='恢复此笔刷虚线参数';bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):return _authorable(context.scene)
    def execute(self,context):
        try:
            scene=context.scene;plan=_plan(scene);bindings,payload=read_controls(scene,plan)
            if payload is None:raise ValueError('dash actual author state not initialized')
            actual=chain_ui._selected_actual(scene,plan,bindings);proposed=copy.deepcopy(payload)
            proposed['patterns'][actual]=controls.default_pattern()
            _commit(scene,plan,bindings,proposed);chain_ui._redraw();return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},_error(context.scene,error));return {'CANCELLED'}

def _schedule(scene,plan):
    if not _authorable(scene):return
    key=scene.as_pointer();stamp=_stamp(scene,plan)
    if _FAILED.get(key)==stamp:return 'FAILED'
    if key in _PENDING:return 'PENDING'
    original_scene,epoch=scene,_EPOCH
    def callback():
        current=current_plan=None
        try:
            if _PENDING.get(key) is not callback or epoch!=_EPOCH:return None
            if original_scene.as_pointer()!=key:return None
            current=next((s for s in bpy.data.scenes if s.as_pointer()==key),None)
            if current is None or current!=original_scene or not _authorable(current):return None
            current_plan=_plan(current);bindings,payload=read_controls(current,current_plan)
            if payload is not None:_resync(current,current_plan,bindings,payload)
            _settings(current).dash_error='';_FAILED.pop(key,None)
        except Exception as error:
            if current is not None and epoch==_EPOCH:
                _remember(_FAILED,key,_stamp(current,current_plan));_error(current,error)
        finally:
            if _PENDING.get(key) is callback:_PENDING.pop(key,None)
            if current is not None and epoch==_EPOCH:chain_ui._redraw()
        return None
    _PENDING[key]=callback
    try:bpy.app.timers.register(callback,first_interval=0.)
    except Exception:_PENDING.pop(key,None);raise
    return 'PENDING'

def draw(layout,scene):
    s=_settings(scene);header=layout.row()
    header.prop(s,'show_dashes',text='虚线',emboss=False,icon='TRIA_DOWN' if s.show_dashes else 'TRIA_RIGHT')
    if not s.show_dashes:return
    box=layout.box()
    if not _authorable(scene):box.label(text='链接场景的虚线参数只读。');return
    box.prop(s,'curve_group',text='分组');box.prop(s,'curve_slot',text='笔刷')
    if not s.dash_patterns_json:
        box.operator(TOONVIEWPORT_OT_prepare_dashes.bl_idname);box.label(text='默认实线；仅调整所选笔刷');return
    try:
        plan=_plan(scene);bindings,payload=read_controls(scene,plan)
        if _SYNC.get(scene.as_pointer())!=_stamp(scene,plan):
            if _schedule(scene,plan)=='FAILED':
                box.label(text=s.dash_error or '虚线参数载入失败，请重新载入。',icon='ERROR')
                box.operator(TOONVIEWPORT_OT_prepare_dashes.bl_idname)
            else:box.label(text='正在载入虚线参数…')
            return
        actual=chain_ui._selected_actual(scene,plan,bindings);item=s.dashes[actual]
        box.label(text=item.label);box.prop(item,'mode')
        values=box.column();values.enabled=item.mode=='DASHED'
        values.prop(item,'dash_length_px');values.prop(item,'gap_length_px')
        box.operator(TOONVIEWPORT_OT_restore_dash.bl_idname)
    except Exception as error:
        box.label(text=_message(error));box.operator(TOONVIEWPORT_OT_prepare_dashes.bl_idname)
    if s.dash_error:box.label(text=s.dash_error,icon='ERROR')

CLASSES=(DashBrush,TOONVIEWPORT_OT_prepare_dashes,TOONVIEWPORT_OT_restore_dash)
