# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact independent amount authority and guarded RNA display for traced actuals."""
import copy
import struct
from collections import OrderedDict
from contextlib import contextmanager
import bpy
from bpy.props import FloatProperty, StringProperty
from . import chain_ui
from . import map_amount_controls as controls

_GUARDS = {}
_SYNC = OrderedDict()
_RUNTIME = OrderedDict()
_PENDING = {}
_FAILED = OrderedDict()
_EPOCH = 0


def _settings(scene):
    return scene.toon_viewport_preview


def _authorable(scene):
    return scene is not None and scene.library is None and scene.is_editable


def _require_authorable(scene):
    if not _authorable(scene):
        raise ValueError('linked Scene amount controls are read-only')


def _plan(scene):
    from . import addon
    return addon._preset(scene)


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > 4:
        cache.popitem(last=False)


def clear_runtime_cache():
    global _EPOCH
    _EPOCH += 1
    callbacks = list(_PENDING.values());_PENDING.clear()
    for callback in callbacks:
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _SYNC.clear();_RUNTIME.clear();_FAILED.clear();_GUARDS.clear()


@contextmanager
def _guard(scene):
    key = scene.as_pointer();_GUARDS[key] = _GUARDS.get(key, 0) + 1
    try:
        yield
    finally:
        _GUARDS[key] -= 1
        if not _GUARDS[key]:_GUARDS.pop(key)


def read_controls(scene, plan=None):
    """No reads/draws write raw. Empty author state means legacy effect1."""
    raw = _settings(scene).map_amounts_json
    if not raw:return None, None
    plan = _plan(scene) if plan is None else plan
    bindings = chain_ui._recover(scene, plan)
    return bindings, controls.normalize(bindings, chain_ui._decode(raw))


def _settings_copy(scene, plan):
    """Owned runtime table, distinct from style._settings_copy's author data."""
    settings = _settings(scene);raw = settings.map_amounts_json
    if not raw:return {}
    key = scene.as_pointer();stamp = (id(plan), settings.preset_revision, raw)
    cached = _RUNTIME.get(key)
    if cached is None or cached[0] != stamp:
        unused, normalized = read_controls(scene, plan)
        _remember(_RUNTIME, key, (stamp, normalized['amounts']))
    return copy.deepcopy(_RUNTIME[key][1])


def _rows(settings):
    return [dict(name=item.name, label=item.label, amount=item.amount) for item in settings.map_amounts]


def _apply(settings, rows):
    if {item.name for item in settings.map_amounts} != {row['name'] for row in rows} or len(settings.map_amounts) != len(rows):
        settings.map_amounts.clear()
        for row in rows:
            item = settings.map_amounts.add();item.name = row['name']
    for row in rows:
        item = settings.map_amounts[row['name']]
        item.label = row['label']
        if item.amount != row['amount']:item.amount = row['amount']


def _display(bindings, normalized):
    return [dict(name=actual, label=bindings['actuals'][actual]['source_brush_key'], amount=amount)
            for actual, amount in normalized['amounts'].items()]


def _stamp(scene, plan):
    settings = _settings(scene)
    return (id(plan), settings.preset_revision, settings.map_amounts_json, tuple(item.name for item in settings.map_amounts))


def _resync(scene, plan, bindings, normalized):
    _require_authorable(scene)
    with _guard(scene):_apply(_settings(scene), _display(bindings, normalized))
    _remember(_SYNC, scene.as_pointer(), _stamp(scene, plan))


def _commit(scene, plan, bindings, payload):
    _require_authorable(scene)
    normalized = controls.normalize(bindings, payload)
    raw = chain_ui._canonical(normalized)
    settings = _settings(scene);old_raw = settings.map_amounts_json;old_rows = _rows(settings)
    try:
        with _guard(scene):
            _apply(settings, _display(bindings, normalized))
            if settings.map_amounts_json != raw:settings.map_amounts_json = raw
    except Exception:
        with _guard(scene):
            if settings.map_amounts_json != old_raw:settings.map_amounts_json = old_raw
            _apply(settings, old_rows)
        _SYNC.pop(scene.as_pointer(), None)
        raise
    settings.map_amount_error = ''
    _FAILED.pop(scene.as_pointer(), None)
    _remember(_SYNC, scene.as_pointer(), _stamp(scene, plan))
    _RUNTIME.pop(scene.as_pointer(), None)
    return normalized


def initialize(scene, plan, *, previous=None, explicit=None):
    bindings = chain_ui._recover(scene, plan)
    if explicit is not None:
        normalized = controls.normalize(bindings, explicit)
    elif previous is not None:
        normalized = controls.merge(bindings, previous, plan['raw'])
    else:
        normalized = controls.seed_native(bindings, plan['raw'])
    return bindings, _commit(scene, plan, bindings, normalized)


def _message(error):
    if 'read-only' in str(error):
        return '链接场景的尺寸贴图参数只读。'
    if 'actual' in str(error) or 'traceable' in str(error):
        return '当前笔刷缺少对应的完整配置身份，请重新载入尺寸贴图参数。'
    return '尺寸贴图影响量须为0到1，原有设置已保留。'


def _error(scene, error):
    message = _message(error)
    if _authorable(scene):_settings(scene).map_amount_error = message
    return message


def _edited(item, context):
    scene = item.id_data
    if not _authorable(scene) or _GUARDS.get(scene.as_pointer(), 0):return
    normalized = bindings = plan = None
    try:
        plan = _plan(scene);bindings, normalized = read_controls(scene, plan)
        if normalized is None:raise ValueError('amount actual author state not initialized')
        proposed = copy.deepcopy(normalized)
        if item.name not in proposed['amounts']:raise ValueError('unknown amount actual')
        value = float(item.amount)
        displayed = struct.unpack('f', struct.pack('f', proposed['amounts'][item.name]))[0]
        if value == displayed:return
        proposed['amounts'][item.name] = value
        _commit(scene, plan, bindings, proposed)
    except Exception as error:
        if normalized is not None:
            _resync(scene, plan, bindings, normalized)
        _error(scene, error)
    chain_ui._redraw()


class MapAmountBrush(bpy.types.PropertyGroup):
    label: StringProperty(default='')
    amount: FloatProperty(name='影响量', description='0无影响，1完整应用已启用的尺寸贴图',
                          default=1., min=0., max=1., precision=3, options=set(), update=_edited)


class TOONVIEWPORT_OT_prepare_map_amounts(bpy.types.Operator):
    bl_idname = 'toon_viewport.prepare_map_amounts';bl_label = '载入尺寸贴图参数';bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):return _authorable(context.scene)
    def execute(self, context):
        try:
            raw = _settings(context.scene).map_amounts_json
            initialize(context.scene, _plan(context.scene), previous=chain_ui._decode(raw) if raw else None)
            chain_ui._redraw();return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, _error(context.scene, error));return {'CANCELLED'}


class TOONVIEWPORT_OT_restore_map_amount(bpy.types.Operator):
    bl_idname = 'toon_viewport.restore_map_amount';bl_label = '恢复此笔刷影响量';bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context):return _authorable(context.scene)
    def execute(self, context):
        try:
            scene = context.scene;plan = _plan(scene);bindings, normalized = read_controls(scene, plan)
            if normalized is None:raise ValueError('amount actual author state not initialized')
            actual = chain_ui._selected_actual(scene, plan, bindings)
            proposed = copy.deepcopy(normalized)
            proposed['amounts'][actual] = controls.native_value(bindings, plan['raw'], actual)
            _commit(scene, plan, bindings, proposed);chain_ui._redraw();return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, _error(context.scene, error));return {'CANCELLED'}


def _schedule(scene, plan):
    if not _authorable(scene):return
    key = scene.as_pointer();stamp = _stamp(scene, plan)
    if _FAILED.get(key) == stamp:return 'FAILED'
    if key in _PENDING:return 'PENDING'
    original_scene, epoch = scene, _EPOCH
    def callback():
        current = None
        try:
            if _PENDING.get(key) is not callback or epoch != _EPOCH:return None
            if original_scene.as_pointer() != key:return None
            current = next((value for value in bpy.data.scenes if value.as_pointer() == key), None)
            if current is None or current != original_scene or not _authorable(current):return None
            current_plan = _plan(current);bindings, normalized = read_controls(current, current_plan)
            if normalized is not None:_resync(current, current_plan, bindings, normalized)
            _settings(current).map_amount_error = '';_FAILED.pop(key, None)
        except Exception as error:
            if current is not None and epoch == _EPOCH:
                _remember(_FAILED, key, stamp);_error(current, error)
        finally:
            if _PENDING.get(key) is callback:_PENDING.pop(key, None)
            if current is not None and epoch == _EPOCH:chain_ui._redraw()
        return None
    _PENDING[key] = callback
    bpy.app.timers.register(callback, first_interval=0.)
    return 'PENDING'


def draw(layout, scene):
    settings = _settings(scene);header = layout.row()
    header.prop(settings, 'show_map_amounts', text='尺寸贴图', emboss=False,
                icon='TRIA_DOWN' if settings.show_map_amounts else 'TRIA_RIGHT')
    if not settings.show_map_amounts:return
    box = layout.box()
    if not _authorable(scene):
        box.label(text='链接场景的尺寸贴图参数只读。');return
    box.prop(settings, 'curve_group', text='分组');box.prop(settings, 'curve_slot', text='笔刷')
    if not settings.map_amounts_json:
        box.operator(TOONVIEWPORT_OT_prepare_map_amounts.bl_idname);box.label(text='0无影响，1完整应用已启用的贴图');return
    try:
        plan = _plan(scene);bindings, normalized = read_controls(scene, plan)
        if _SYNC.get(scene.as_pointer()) != _stamp(scene, plan):
            if _schedule(scene, plan) == 'FAILED':
                box.label(text=settings.map_amount_error or '影响量载入失败，请重新载入。', icon='ERROR')
                box.operator(TOONVIEWPORT_OT_prepare_map_amounts.bl_idname)
            else:box.label(text='正在载入影响量…')
            return
        actual = chain_ui._selected_actual(scene, plan, bindings);item = settings.map_amounts[actual]
        from . import amount_animation, amount_animation_ui
        target=amount_animation.track(scene,actual)
        animated=target is not None and target.enabled
        box.label(text=item.label)
        static_row=box.row();static_row.enabled=not animated
        static_row.prop(item, 'amount', slider=True)
        box.label(text='0无影响，1完整；不改变贴图开关')
        static_row=box.row();static_row.enabled=not animated
        static_row.operator(TOONVIEWPORT_OT_restore_map_amount.bl_idname)
        amount_animation_ui.draw(box,scene,actual,plan,bindings,normalized)
    except Exception as error:
        box.label(text=_message(error));box.operator(TOONVIEWPORT_OT_prepare_map_amounts.bl_idname)
    if settings.map_amount_error:box.label(text=settings.map_amount_error, icon='ERROR')


CLASSES = (MapAmountBrush, TOONVIEWPORT_OT_prepare_map_amounts, TOONVIEWPORT_OT_restore_map_amount)
