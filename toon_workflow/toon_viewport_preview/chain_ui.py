# SPDX-License-Identifier: GPL-3.0-or-later
"""Chain author UI: exact JSON authority, guarded RNA display/edit proxies.

This module edits only owned chain settings. It never edits preset Text,
curves/maps or source geometry, and never executes phase planning.
"""
import copy
import json
import math
from collections import OrderedDict
from contextlib import contextmanager

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .chain_controls import seed_controls, normalize_controls, merge_controls
from .source_bindings import recover

_PROP = 'toon_viewport_preview'
_MAX_JSON = 1024 * 1024
_GUARDS = {}
_BINDINGS = OrderedDict()
_SYNC = OrderedDict()
_SYNC_FAILURES = OrderedDict()
_PENDING_SYNC = {}
_TIMER_EPOCH = 0
_CACHE_LIMIT = 4
_EDIT_FIELDS = ('label', 'boundary_enabled', 'intersection_enabled', 'max_gap_px', 'intersection_max_gap_px',
                'split_enabled', 'split_angle', 'direction_enabled', 'direction_angle',
                'closed_orientation', 'min_length_px', 'split_value_json',
                'direction_value_json')


def _settings(scene):
    return getattr(scene, _PROP)


def _scene_key(scene):
    return scene.as_pointer()


@contextmanager
def _guard(scene):
    key = _scene_key(scene)
    _GUARDS[key] = _GUARDS.get(key, 0) + 1
    try:
        yield
    finally:
        remaining = _GUARDS[key] - 1
        if remaining:
            _GUARDS[key] = remaining
        else:
            _GUARDS.pop(key, None)


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_LIMIT:
        cache.popitem(last=False)


def clear_runtime_cache():
    """Cancel only this module's idle callbacks; never clear persisted data."""
    global _TIMER_EPOCH
    _TIMER_EPOCH += 1
    callbacks = list(_PENDING_SYNC.values())
    _PENDING_SYNC.clear()
    for callback in callbacks:
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _BINDINGS.clear()
    _SYNC.clear()
    _SYNC_FAILURES.clear()
    _GUARDS.clear()


def _plan(scene):
    from . import addon
    return addon._preset(scene)


def _redraw():
    from . import addon
    addon._redraw()


def _registered():
    from . import addon
    return bool(getattr(addon, '_registered', False)) and hasattr(bpy.types.Scene, _PROP)


def _schedule_resync(scene, plan=None):
    """Panel-safe admission: one owned timer per scene, no RNA writes here.

    The timer uses the latest live scene/plan/raw, not captured author data.
    A failed unchanged input is not retried on every draw; prepare or a changed
    input can retry. The RNA reference is only a deletion/reuse validity guard.
    """
    if not _registered():
        return False
    key = _scene_key(scene)
    if key in _PENDING_SYNC:
        return True
    stamp = _stamp(scene, plan, _settings(scene).chain_controls_json)
    failed = _SYNC_FAILURES.get(key)
    if failed is not None and failed[0] == stamp:
        return False
    original_scene, epoch = scene, _TIMER_EPOCH

    def callback():
        if _PENDING_SYNC.get(key) is not callback or epoch != _TIMER_EPOCH:
            return None
        current = current_plan = None
        try:
            if not _registered() or _scene_key(original_scene) != key:
                return None
            current = next((item for item in bpy.data.scenes if _scene_key(item) == key), None)
            if current is None or current != original_scene:
                return None
            settings = _settings(current)
            raw = settings.chain_controls_json
            if raw == '':
                _SYNC.pop(key, None)
                _SYNC_FAILURES.pop(key, None)
                settings.chain_error = ''
                return None
            current_plan = _plan(current)
            bindings, controls = read_controls(current)
            if controls is None:
                _SYNC.pop(key, None)
            elif (settings.chain_controls_json == raw
                  and _plan(current) is current_plan
                  and epoch == _TIMER_EPOCH and _registered()):
                _resync(current, current_plan, bindings, controls, rebuild=True)
            else:
                return None
            settings.chain_error = ''
            _SYNC_FAILURES.pop(key, None)
        except Exception as error:
            if current is not None and epoch == _TIMER_EPOCH and _registered():
                message = _message(error)
                _remember(_SYNC_FAILURES, key, (
                    _stamp(current, current_plan, _settings(current).chain_controls_json), message))
                try:
                    _settings(current).chain_error = message
                except (AttributeError, ReferenceError, RuntimeError):
                    pass
        finally:
            if _PENDING_SYNC.get(key) is callback:
                _PENDING_SYNC.pop(key, None)
            if current is not None and epoch == _TIMER_EPOCH and _registered():
                _redraw()
        return None

    _PENDING_SYNC[key] = callback
    try:
        bpy.app.timers.register(callback, first_interval=0.0)
    except Exception:
        _PENDING_SYNC.pop(key, None)
        raise
    return True


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate saved chain field')
        result[key] = value
    return result


def _bad_constant(unused):
    raise ValueError('nonfinite saved chain value')


def _decode(raw):
    if type(raw) is not str or len(raw) > _MAX_JSON:
        raise ValueError('saved chain text exceeds limit')
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_constant)


def _recover(scene, plan, *, fresh=False):
    if type(plan) is not dict or type(plan.get('raw')) is not dict:
        raise ValueError('complete preset source required')
    settings = _settings(scene)
    key = _scene_key(scene)
    old = _BINDINGS.get(key)
    if not fresh and old is not None and old[0] == settings.preset_revision and old[1] is plan:
        _BINDINGS.move_to_end(key)
        return old[2]
    bindings = recover(plan['raw'])
    _remember(_BINDINGS, key, (settings.preset_revision, plan, bindings))
    return bindings


def read_controls(scene, plan=None):
    """Return detached (bindings, controls); empty raw means (None, None).

    An explicit plan is freshly recovered. Default-plan reads cache only by
    current plan object plus preset revision; callers never receive that cache.
    """
    raw = _settings(scene).chain_controls_json
    if raw == '':
        return None, None
    supplied = plan is not None
    plan = _plan(scene) if plan is None else plan
    bindings = _recover(scene, plan, fresh=supplied)
    controls = normalize_controls(bindings, _decode(raw))
    from . import brush_effects
    controls=brush_effects.effective_chains(plan['raw'],bindings,controls)
    return copy.deepcopy(bindings), controls


def _eligibility(bindings):
    result = {actual: set() for actual in bindings['actuals']}
    for route in bindings['routes']:
        result[route['actual_brush_id']].add(route['kind'])
    return result


def _cache_scalar(text, default, *, direction=False):
    try:
        value = default if not text else _decode(text)
    except (ValueError, TypeError) as exc:
        raise ValueError('saved angle cache is invalid') from exc
    if type(value) not in (int, float):
        raise ValueError('saved angle cache is invalid')
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError) as exc:
        raise ValueError('saved angle cache is invalid') from exc
    lower, upper = (-360, 360) if direction else (0, 180)
    if not finite or not lower <= value <= upper:
        raise ValueError('saved angle cache is invalid')
    return value


def _snapshot(settings):
    return [dict(name=item.name,
                 boundary_available=item.boundary_available,
                 intersection_available=item.intersection_available,
                 **{key: getattr(item, key) for key in _EDIT_FIELDS})
            for item in settings.chains]


def _set_row(item, data):
    item.name = data['name']
    item['_boundary_available'] = data['boundary_available']
    item['_intersection_available'] = data['intersection_available']
    for key in _EDIT_FIELDS:
        value = data[key]
        if getattr(item, key) != value:
            setattr(item, key, value)


def _apply_rows(settings, rows, *, rebuild):
    names = [item.name for item in settings.chains]
    expected = [row['name'] for row in rows]
    same = len(names) == len(set(names)) and set(names) == set(expected)
    if not same:
        if not rebuild:
            raise ValueError('chain display needs reload')
        settings.chains.clear()
        for row in rows:
            _set_row(settings.chains.add(), row)
    else:
        for row in rows:
            _set_row(settings.chains[row['name']], row)


def _display_rows(scene, bindings, controls, overrides=None):
    old = {row['name']: row for row in _snapshot(_settings(scene))}
    seeded = seed_controls(bindings)
    eligible = _eligibility(bindings)
    rows = []
    for actual, value in controls['brushes'].items():
        policy = value['policy']
        saved = dict(old.get(actual, {}))
        saved.update((overrides or {}).get(actual, {}))
        default = seeded['brushes'][actual]['policy']
        split = policy['split_angle_degrees']
        direction = policy['line_direction_degrees']
        split_cache = split if split is not None else _cache_scalar(
            saved.get('split_value_json', ''), default['split_angle_degrees'])
        direction_cache = direction if direction is not None else _cache_scalar(
            saved.get('direction_value_json', ''), default['line_direction_degrees'], direction=True)
        rows.append(dict(name=actual,
            label=bindings['actuals'][actual]['source_brush_key'],
            boundary_available='boundary' in eligible[actual],
            intersection_available='intersection' in eligible[actual],
            boundary_enabled=value['boundary_enabled'],
            intersection_enabled=value['intersection_enabled'], max_gap_px=value['max_gap_px'],
            intersection_max_gap_px=value['intersection_max_gap_px'],
            split_enabled=split is not None, split_angle=math.radians(split_cache),
            direction_enabled=direction is not None, direction_angle=math.radians(direction_cache),
            closed_orientation=policy['closed_orientation'], min_length_px=policy['min_length_px'],
            split_value_json=_canonical(split_cache), direction_value_json=_canonical(direction_cache)))
    return rows


def _stamp(scene, plan, raw):
    return (id(plan), _settings(scene).preset_revision, raw,
            tuple(item.name for item in _settings(scene).chains))


def _commit(scene, plan, bindings, controls, *, overrides=None, rebuild=True):
    """All validation precedes RNA writes; failed application rolls back raw/UI."""
    controls = normalize_controls(bindings, controls)
    raw = _canonical(controls)
    rows = _display_rows(scene, bindings, controls, overrides)
    settings = _settings(scene)
    old_raw, old_rows = settings.chain_controls_json, _snapshot(settings)
    try:
        with _guard(scene):
            _apply_rows(settings, rows, rebuild=rebuild)
            if settings.chain_controls_json != raw:
                settings.chain_controls_json = raw
    except Exception:
        with _guard(scene):
            if settings.chain_controls_json != old_raw:
                settings.chain_controls_json = old_raw
            _apply_rows(settings, old_rows, rebuild=rebuild)
        _SYNC.pop(_scene_key(scene), None)
        raise
    settings.chain_error = ''
    _SYNC_FAILURES.pop(_scene_key(scene), None)
    _remember(_SYNC, _scene_key(scene), _stamp(scene, plan, raw))
    return controls


def initialize(scene, plan, previous=None, explicit=None):
    """Validate recover+merge first, then atomically populate owned RNA/JSON.

    Returns detached (bindings, controls). No preset Text or curve is written.
    previous=None means seed; the prepare operator explicitly supplies saved raw.
    """
    bindings = _recover(scene, plan, fresh=True)
    controls = merge_controls(bindings, previous=previous, explicit=explicit)
    controls = _commit(scene, plan, bindings, controls)
    return copy.deepcopy(bindings), controls


def _selected_actual(scene, plan, bindings):
    from . import curve_editor
    settings = _settings(scene)
    group, slot = settings.curve_group, settings.curve_slot
    request = plan['raw'].get('request', {})
    if slot == 'intersection_brush':
        traced = any(row.get('group') == group for row in request.get('intersection_aliases', []))
    else:
        traced = any(row.get('group') == group and row.get('slot') == slot
                     for row in request.get('boundary_aliases', []))
    if not traced:
        raise ValueError('selected brush has no traceable native identity')
    actual = curve_editor._identity(plan, group, slot)
    if actual not in bindings['actuals']:
        raise ValueError('selected brush has no traceable native identity')
    return actual


def _message(error):
    text = str(error)
    path = getattr(error, 'path', '')
    if 'traceable' in text:
        return '此笔刷缺少可追溯的原配置身份，不能编辑链参数。'
    if 'not initialized' in text:
        return '请先载入链参数。'
    if 'without a boundary route' in text:
        return '该笔刷没有边界线来源，不能启用边界链参数。'
    if 'intersection gap without an intersection route' in text:
        return '该笔刷没有交线来源，交线连接间距必须为零。'
    if 'without an intersection route' in text:
        return '该笔刷没有交线来源，不能启用交线链参数。'
    if 'angle cache' in text:
        return '保存的角度记忆值无效，请恢复所选笔刷的原值。'
    if 'loop_direction_type' in text:
        return '原配置的闭环方向暂不支持，链参数保持原值。'
    if 'intersection_max_gap_px' in text:
        return '交线连接间距须为 0 到 65536 像素的有限数值。'
    if 'max_gap_px' in text or 'line_link_length' in text:
        return '边界连接间距须为 0 到 65536 像素的有限数值。'
    if 'min_length_px' in path or 'min_line_length' in text:
        return '最短链长须为 0 到 65536 像素的有限数值。'
    if 'split_angle_degrees' in path or 'line_split_angle' in text or 'edited split' in text:
        return '分割角度须为 0° 到 180°的有限数值。'
    if 'line_direction_degrees' in path or 'native line_direction' in text or 'edited direction' in text:
        return '线方向须为 -360° 到 360°的有限数值。'
    if 'actual set' in text:
        return '链参数笔刷集合与当前配置不一致，请重新载入链参数。'
    if 'display needs reload' in text:
        return '链参数面板需要重新载入，已保留原有保存值。'
    if isinstance(error, json.JSONDecodeError) or 'saved chain' in text or 'chain controls' in text:
        return '保存的链参数内容无效，未覆盖原有设置。'
    return '无法更新链参数，已保留原有设置；请重新载入完整配置。'


def _resync(scene, plan, bindings, controls, *, rebuild):
    with _guard(scene):
        _apply_rows(_settings(scene), _display_rows(scene, bindings, controls), rebuild=rebuild)
    _remember(_SYNC, _scene_key(scene), _stamp(scene, plan, _settings(scene).chain_controls_json))


def _edited(item, context, field):
    scene = item.id_data
    if scene is None or _GUARDS.get(_scene_key(scene), 0):
        return
    settings = _settings(scene)
    plan = None
    bindings = controls = None
    try:
        plan = _plan(scene)
        bindings, controls = read_controls(scene)
        if controls is None:
            raise ValueError('chain controls not initialized')
        actual = item.name
        if actual not in controls['brushes']:
            raise ValueError('selected brush has no traceable native identity')
        proposed = copy.deepcopy(controls)
        row, policy = proposed['brushes'][actual], proposed['brushes'][actual]['policy']
        cache = {}
        if field in ('boundary_enabled', 'intersection_enabled'):
            row[field] = bool(getattr(item, field))
        elif field in ('max_gap_px', 'intersection_max_gap_px'):
            row[field] = float(getattr(item, field))
        elif field == 'closed_orientation':
            policy[field] = item.closed_orientation
        elif field == 'min_length_px':
            policy[field] = float(item.min_length_px)
        else:
            split = field.startswith('split_')
            name = 'split_angle_degrees' if split else 'line_direction_degrees'
            cache_name = 'split_value_json' if split else 'direction_value_json'
            default = seed_controls(bindings)['brushes'][actual]['policy'][name]
            if field.endswith('_enabled'):
                enabled = bool(getattr(item, field))
                if enabled:
                    policy[name] = _cache_scalar(getattr(item, cache_name), default, direction=not split)
                else:
                    if policy[name] is not None:
                        cache[cache_name] = _canonical(policy[name])
                    policy[name] = None
            else:
                # RNA enforces the angle property's hard range. Clamp only the
                # conversion of its float endpoint representation, never JSON.
                degrees = math.degrees(float(getattr(item, field)))
                if not math.isfinite(degrees):
                    raise ValueError('edited split angle is nonfinite' if split
                                     else 'edited direction angle is nonfinite')
                lower, upper = (0., 180.) if split else (-360., 360.)
                degrees = min(upper, max(lower, degrees))
                cache[cache_name] = _canonical(degrees)
                if policy[name] is not None:
                    policy[name] = degrees
        _commit(scene, plan, bindings, proposed,
                overrides={actual: cache}, rebuild=False)
    except Exception as error:
        # Rehydrate from authoritative raw; never read all RNA floats back into
        # JSON. In particular an invalid edit cannot quantize other fields.
        try:
            if controls is not None and bindings is not None:
                _resync(scene, plan, bindings, controls, rebuild=False)
        except Exception:
            _SYNC.pop(_scene_key(scene), None)
        settings.chain_error = _message(error)
    _redraw()


def _update(field):
    return lambda item, context: _edited(item, context, field)


class ChainBrush(bpy.types.PropertyGroup):
    label: StringProperty(default='')
    boundary_available: BoolProperty(get=lambda item: bool(item.get('_boundary_available', False)))
    intersection_available: BoolProperty(get=lambda item: bool(item.get('_intersection_available', False)))
    boundary_enabled: BoolProperty(name='启用边界链参数', default=False, update=_update('boundary_enabled'))
    intersection_enabled: BoolProperty(name='启用交线链参数', default=False, update=_update('intersection_enabled'))
    max_gap_px: FloatProperty(name='边界连接间距（像素）', default=0, min=0, max=65536,
                              update=_update('max_gap_px'))
    intersection_max_gap_px: FloatProperty(name='交线连接间距', description='仅连接交线的相位，不补画间隙',
        default=0, min=0, max=65536, update=_update('intersection_max_gap_px'))
    split_enabled: BoolProperty(name='按角度分割', default=True, update=_update('split_enabled'))
    split_angle: FloatProperty(name='分割角度', subtype='ANGLE', unit='ROTATION',
                               default=math.pi/2, min=0, max=math.pi, update=_update('split_angle'))
    direction_enabled: BoolProperty(name='指定线方向', default=True, update=_update('direction_enabled'))
    direction_angle: FloatProperty(name='线方向', subtype='ANGLE', unit='ROTATION',
                                   default=0, min=-2*math.pi, max=2*math.pi,
                                   update=_update('direction_angle'))
    closed_orientation: EnumProperty(name='闭环方向', default='clockwise', items=[
        ('retained', '保留原方向', ''), ('clockwise', '顺时针', ''),
        ('counterclockwise', '逆时针', '')], update=_update('closed_orientation'))
    min_length_px: FloatProperty(name='最短链长（像素）', default=0, min=0, max=65536,
                                 update=_update('min_length_px'))
    # Persistent degree-valued JSON scalars. They remember the exact value from
    # before None; neither is SKIP_SAVE and neither is added to author schema.
    split_value_json: StringProperty(default='')
    direction_value_json: StringProperty(default='')


class TOONVIEWPORT_OT_prepare_chains(bpy.types.Operator):
    bl_idname = 'toon_viewport.prepare_chains'
    bl_label = '载入链参数'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = _settings(context.scene)
        try:
            previous = _decode(settings.chain_controls_json) if settings.chain_controls_json else None
            initialize(context.scene, _plan(context.scene), previous=previous)
            _redraw()
            self.report({'INFO'}, '已载入链参数，新笔刷默认关闭')
            return {'FINISHED'}
        except Exception as error:
            settings.chain_error = _message(error)
            _redraw()
            self.report({'ERROR'}, settings.chain_error)
            return {'CANCELLED'}


class TOONVIEWPORT_OT_restore_selected_chain(bpy.types.Operator):
    bl_idname = 'toon_viewport.restore_selected_chain'
    bl_label = '恢复此笔刷链参数原值'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        settings = _settings(scene)
        try:
            plan = _plan(scene)
            bindings, controls = read_controls(scene)
            if controls is None:
                raise ValueError('chain controls not initialized')
            actual = _selected_actual(scene, plan, bindings)
            restored = seed_controls(bindings)['brushes'][actual]
            old = controls['brushes'][actual]
            restored['boundary_enabled'] = old['boundary_enabled']
            restored['intersection_enabled'] = old['intersection_enabled']
            proposed = copy.deepcopy(controls)
            proposed['brushes'][actual] = restored
            _commit(scene, plan, bindings, proposed)
            _redraw()
            self.report({'INFO'}, '已恢复所选笔刷链参数，启用状态保持不变')
            return {'FINISHED'}
        except Exception as error:
            settings.chain_error = _message(error)
            _redraw()
            self.report({'ERROR'}, settings.chain_error)
            return {'CANCELLED'}


prepare_chains_operator = TOONVIEWPORT_OT_prepare_chains
restore_selected_operator = TOONVIEWPORT_OT_restore_selected_chain
CLASSES = (ChainBrush, prepare_chains_operator, restore_selected_operator)


def draw(layout, scene):
    """Draw reads Scene/RNA only; deferred idle callbacks perform resync writes."""
    settings = _settings(scene)
    header = layout.row()
    header.prop(settings, 'show_chains', text='链参数', emboss=False,
                icon='TRIA_DOWN' if settings.show_chains else 'TRIA_RIGHT')
    if not settings.show_chains:
        return
    box = layout.box()
    box.prop(settings, 'curve_group', text='分组')
    box.prop(settings, 'curve_slot', text='笔刷')
    if not settings.chain_controls_json:
        box.operator(prepare_chains_operator.bl_idname)
        box.label(text='默认保持原描边，载入后可选择启用')
        if settings.chain_error:
            box.label(text=settings.chain_error, icon='ERROR')
        return
    plan = None
    try:
        plan = _plan(scene)
        bindings, controls = read_controls(scene)
    except Exception as error:
        message = _message(error)
        try:
            _schedule_resync(scene, plan)
        except Exception:
            pass
        box.label(text=message, icon='ERROR')
        box.operator(prepare_chains_operator.bl_idname)
        return
    if _SYNC.get(_scene_key(scene)) != _stamp(scene, plan, settings.chain_controls_json):
        try:
            waiting = _schedule_resync(scene, plan)
        except Exception as error:
            box.label(text=_message(error), icon='ERROR')
            waiting = False
        if waiting:
            box.label(text='正在恢复链参数…')
        else:
            failed = _SYNC_FAILURES.get(_scene_key(scene))
            box.label(text=failed[1] if failed else '请重新载入链参数', icon='ERROR')
        box.operator(prepare_chains_operator.bl_idname)
        return
    try:
        actual = _selected_actual(scene, plan, bindings)
        item = settings.chains[actual]
    except Exception as error:
        # Missing aliases are a local selection hint, not a Scene write.
        box.label(text=_message(error), icon='ERROR')
        return
    box.label(text=item.label)
    enabled = box.row()
    enabled.enabled = item.boundary_available
    enabled.prop(item, 'boundary_enabled')
    gap = box.row()
    gap.enabled = item.boundary_available
    gap.prop(item, 'max_gap_px')
    pending = box.row()
    pending.enabled = item.intersection_available
    pending.prop(item, 'intersection_enabled')
    intersection_gap = box.row()
    intersection_gap.enabled = item.intersection_available
    intersection_gap.prop(item, 'intersection_max_gap_px')
    if item.intersection_available:
        box.label(text='连接交线相位，不补画间隙')
    policy = box.column()
    policy.enabled = item.boundary_available or item.intersection_available
    split = policy.row(align=True)
    split.prop(item, 'split_enabled')
    value = split.row(align=True)
    value.enabled = item.split_enabled
    value.prop(item, 'split_angle', text='')
    direction = policy.row(align=True)
    direction.prop(item, 'direction_enabled')
    value = direction.row(align=True)
    value.enabled = item.direction_enabled
    value.prop(item, 'direction_angle', text='')
    policy.prop(item, 'closed_orientation')
    policy.prop(item, 'min_length_px')
    box.operator(restore_selected_operator.bl_idname)
    if not item.boundary_available and not item.intersection_available:
        box.label(text='此笔刷没有可编辑的链参数')
    if settings.chain_error:
        box.label(text=settings.chain_error, icon='ERROR')


__all__ = ['ChainBrush', 'prepare_chains_operator', 'restore_selected_operator',
           'CLASSES', 'draw', 'read_controls', 'initialize', 'clear_runtime_cache']
