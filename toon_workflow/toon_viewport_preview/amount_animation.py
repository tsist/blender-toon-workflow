# SPDX-License-Identifier: GPL-3.0-or-later
"""Controlled named-actual amount animation. Static JSON is never written here."""
import contextlib
import math
import struct

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty

PROP = 'toon_viewport_preview'
ROOT = PROP + '.amount_tracks'
MAX_TRACKS = 512


class AmountTrack(bpy.types.PropertyGroup):
    actual_id: StringProperty(options=set())
    enabled: BoolProperty(name='启用影响量动画', default=False, options=set())
    amount: FloatProperty(name='动画影响量', default=1., min=0., max=1., options={'ANIMATABLE'})


CLASSES = (AmountTrack,)


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _identity(actual):
    _need(type(actual) is str and 0 < len(actual) <= 256 and '\x00' not in actual,
          'amount animation requires a bounded exact actual identity')
    actual.encode('utf-8')
    return actual


def _value(value):
    _need(type(value) in (int, float), 'animation amount must be a real scalar')
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError('animation amount is outside binary64 range') from exc
    _need(math.isfinite(converted) and 0 <= converted <= 1,
          'animation amount must be a finite real scalar in 0..1')
    return converted


def key_path(actual):
    return ROOT + '["' + bpy.utils.escape_identifier(_identity(actual)) + '"].amount'


def _settings(scene):
    _need(isinstance(scene, bpy.types.Scene), 'amount animation requires a Scene')
    return getattr(scene, PROP)


def _rows(scene):
    collection = _settings(scene).amount_tracks
    _need(len(collection) <= MAX_TRACKS, 'amount animation has too many tracks')
    rows = {}
    for item in collection:
        name = _identity(item.name)
        _need(item.actual_id == name, 'amount animation track name differs from its recovered identity')
        _need(name not in rows, 'duplicate amount animation actual identity')
        _value(float(item.amount))
        rows[name] = item
    return rows


def track(scene, actual):
    return _rows(scene).get(_identity(actual))


def _registry(actuals):
    _need(type(actuals) in (dict, set, frozenset) and len(actuals) <= MAX_TRACKS,
          'bounded recovered actual registry required for amount animation')
    for actual in actuals:
        _identity(actual)


def validate_registry(scene, actuals):
    _registry(actuals)
    for actual, item in _rows(scene).items():
        _need(not item.enabled or actual in actuals,
              'enabled amount animation actual is missing from recovered registry: ' + actual)


def _local(scene):
    _need(scene.library is None and scene.is_editable, 'linked/read-only Scene animation is unsupported')


def _token(slot):
    return (slot.handle, slot.identifier, slot.target_id_type)


def _matching_slot(action, token):
    matches = [s for s in action.slots if _token(s) == token]
    _need(len(matches) == 1 and token[2] == 'SCENE',
          'copied Action has no unique preserved matching SCENE slot')
    slot = matches[0]
    _need(any(strip.channelbag(slot) is not None for layer in action.layers for strip in layer.strips),
          'copied SCENE slot has no preserved channelbag; empty fallback is forbidden')
    return slot


def _graph(scene):
    """Read-only validation of the bound slot; other Action slots are not evaluated."""
    _local(scene)
    rows = _rows(scene)
    ad = scene.animation_data
    if ad is None:
        return None, None, None, {}
    _need(not ad.nla_tracks and not ad.drivers and not ad.use_tweak_mode,
          'amount animation does not support NLA entries, tweak mode or drivers')
    _need(ad.action_blend_type == 'REPLACE' and ad.action_influence == 1. and ad.action_extrapolation == 'HOLD',
          'amount animation requires full REPLACE Action influence and HOLD extrapolation')
    action = ad.action
    if action is None:
        return None, None, None, {}
    _need(action.library is None and action.is_editable, 'linked/read-only Action animation is unsupported')
    if not action.slots and not action.layers and action.is_empty:
        _need(ad.action_slot is None, 'empty Action has an unexpected slot binding')
        return action, None, None, {}
    _need(action.is_action_layered and len(action.layers) == 1,
          'amount animation requires one native Action layer')
    strips = action.layers[0].strips
    _need(len(strips) == 1 and strips[0].type == 'KEYFRAME',
          'amount animation requires one native KEYFRAME strip')
    slot = ad.action_slot
    _need(slot is not None and slot.target_id_type == 'SCENE' and
          any(s.as_pointer() == slot.as_pointer() for s in action.slots),
          'existing Action requires its current valid SCENE slot')
    bag = strips[0].channelbag(slot)
    _need(bag is not None and bag.slot_handle == slot.handle,
          'existing Action requires its preserved SCENE channelbag')
    paths = {key_path(actual): actual for actual in rows}
    amounts = {}
    for current in bag.fcurves:
        if not current.data_path.startswith(ROOT):
            continue
        _need(current.data_path in paths and current.array_index == 0,
              'indexed, orphan, metadata or malformed amount animation path is unsupported')
        actual = paths[current.data_path]
        _need(actual not in amounts, 'duplicate named amount animation curve')
        _need(not current.modifiers and not current.sampled_points and not current.mute and
              (current.group is None or not current.group.mute),
              'amount animation modifiers, samples or muted channels are unsupported')
        _need(current.extrapolation == 'CONSTANT', 'amount animation requires CONSTANT extrapolation')
        times = set()
        for key in current.keyframe_points:
            _need(key.interpolation == 'LINEAR', 'amount animation supports LINEAR keys only')
            x, y = key.co
            _need(math.isfinite(x) and math.isfinite(y) and 0 <= y <= 1 and x not in times,
                  'amount animation has invalid or duplicate key coordinates')
            times.add(x)
        amounts[actual] = current
    return action, slot, bag, amounts


def curve(scene, actual):
    return _graph(scene)[3].get(_identity(actual))


def resolve(scene, static_amounts, actuals, *, evaluated_scene=None):
    validate_registry(scene, actuals)
    rows = _rows(scene)
    enabled = [actual for actual, item in rows.items() if item.enabled]
    if not enabled:
        return static_amounts
    _graph(scene)
    values = rows
    if evaluated_scene is not None:
        _need(isinstance(evaluated_scene, bpy.types.Scene), 'evaluated amount source must be a Scene')
        original = evaluated_scene.original if evaluated_scene.is_evaluated else evaluated_scene
        _need(original == scene, 'evaluated amount Scene differs from requested Scene')
        values = _rows(evaluated_scene)
        _need(set(values) == set(rows) and all(values[a].enabled == rows[a].enabled for a in rows),
              'evaluated amount track identities differ from author Scene')
    result = dict(static_amounts)
    for actual in enabled:
        result[actual] = _value(float(values[actual].amount))
    return result


def signature(scene):
    rows = _rows(scene)
    if any(item.enabled for item in rows.values()):
        _graph(scene)
    return tuple((actual, bool(item.enabled), float(item.amount))
                 for actual, item in sorted(rows.items()))


def _frame(scene, value):
    value = scene.frame_current_final if value is None else value
    _need(type(value) in (int, float) and abs(value) <= 1048574 and math.isfinite(value),
          'animation frame must be finite and inside Blender Scene frame bounds')
    # Exact comparison in native key-coordinate precision, without an epsilon window.
    return struct.unpack('f', struct.pack('f', float(value)))[0]


def _key_state(key):
    return dict(co=tuple(key.co), interpolation=key.interpolation, easing=key.easing,
                handle_left=tuple(key.handle_left), handle_right=tuple(key.handle_right),
                handle_left_type=key.handle_left_type, handle_right_type=key.handle_right_type,
                amplitude=key.amplitude, back=key.back, period=key.period,
                type=key.type, select_control_point=key.select_control_point,
                select_left_handle=key.select_left_handle, select_right_handle=key.select_right_handle)


def _assign_key(key, state):
    for name in ('co', 'interpolation', 'easing', 'handle_left_type', 'handle_right_type',
                 'amplitude', 'back', 'period', 'type', 'select_control_point',
                 'select_left_handle', 'select_right_handle', 'handle_left', 'handle_right'):
        setattr(key, name, state[name])


def _finish_keys(current, untouched):
    current.update()
    # Blender recalculates even unused LINEAR handles. Restore all other keys exactly.
    for key in current.keyframe_points:
        state = untouched.get(float(key.co.x))
        if state is not None:
            _assign_key(key, state)
    for key in current.keyframe_points:
        if float(key.co.x) in untouched:
            _need(_key_state(key) == untouched[float(key.co.x)], 'unrelated amount key changed')


def _restore_keys(current, keys):
    current.keyframe_points.clear()
    current.keyframe_points.add(len(keys))
    for key, state in zip(current.keyframe_points, keys):
        _assign_key(key, state)
    current.update()
    for key, state in zip(current.keyframe_points, keys):
        _assign_key(key, state)


@contextlib.contextmanager
def _edit(scene, actual, *, action_edit):
    """Synchronous transaction: restore original binding/target on any author failure."""
    collection = _settings(scene).amount_tracks
    old_row = track(scene, actual)
    row_state = None if old_row is None else (bool(old_row.enabled), float(old_row.amount), old_row.actual_id)
    had_ad = scene.animation_data is not None
    ad = scene.animation_data
    old_action = None if ad is None else ad.action
    old_slot = None if ad is None else ad.action_slot
    old_token = None if old_slot is None else _token(old_slot)
    old_curve = None if not action_edit else _graph(scene)[3].get(actual)
    old_keys = None if old_curve is None else [_key_state(k) for k in old_curve.keyframe_points]
    curve_state = None if old_curve is None else {name: getattr(old_curve, name) for name in
        ('extrapolation', 'mute', 'lock', 'hide', 'select', 'color_mode', 'auto_smoothing')}
    old_color = None if old_curve is None else tuple(old_curve.color)
    old_group = '' if old_curve is None or old_curve.group is None else old_curve.group.name
    replacement = None
    try:
        if action_edit:
            ad = scene.animation_data_create()
            if old_action is None:
                replacement = bpy.data.actions.new('AmountAnimation_' + scene.name)
            elif old_action.users > 1 or old_slot is None:
                replacement = old_action.copy()
            if replacement is not None:
                matched = None if old_token is None else _matching_slot(replacement, old_token)
                ad.action = replacement
                if matched is not None:
                    ad.action_slot = matched
                    _need(_token(ad.action_slot) == old_token, 'copied SCENE slot rebinding failed')
        yield
    except Exception:
        # Restore binding before deleting only the duplicate Action owned by this transaction.
        if replacement is not None:
            ad.action = old_action
            if old_token is not None:
                ad.action_slot = _matching_slot(old_action, old_token)
            # The duplicate belongs only to this synchronous transaction. Remove
            # it even when a copied fake-user contributes to its users count.
            bpy.data.actions.remove(replacement)
            if not had_ad:
                scene.animation_data_clear()
        elif action_edit and old_action is not None:
            # Rollback must not validate the partially edited graph.
            bag = old_action.layers[0].strips[0].channelbag(old_slot)
            current = None if bag is None else next((c for c in bag.fcurves if c.data_path == key_path(actual) and c.array_index == 0), None)
            if old_keys is None:
                if current is not None:
                    bag.fcurves.remove(current)
            else:
                if current is None:
                    current = old_action.fcurve_ensure_for_datablock(
                        scene, data_path=key_path(actual), index=0, group_name=old_group)
                for name, value in curve_state.items():
                    setattr(current, name, value)
                current.color = old_color
                _restore_keys(current, old_keys)
        item = collection.get(actual)
        if row_state is None:
            if item is not None:
                collection.remove(next(i for i, r in enumerate(collection) if r.as_pointer() == item.as_pointer()))
        elif item is not None:
            item.enabled, item.amount, item.actual_id = row_state
        raise


def _author(scene, actual, actuals):
    _identity(actual)
    validate_registry(scene, actuals)
    _need(actual in actuals, 'amount animation requires a recovered actual identity')
    _graph(scene)
    item = track(scene, actual)
    _need(item is not None or len(_settings(scene).amount_tracks) < MAX_TRACKS, 'amount animation has too many tracks')
    return item


def _new_track(scene, actual, value):
    item = _settings(scene).amount_tracks.add()
    item.name = actual
    item.actual_id = actual
    _need(item.name == actual, 'RNA changed the exact actual identity')
    item.amount = value
    return item


def set_enabled(scene, actual, enabled, actuals, *, initial_value=1.):
    _need(type(enabled) is bool, 'amount animation enabled must be boolean')
    value = _value(initial_value)
    if enabled:
        item = _author(scene, actual, actuals)
    else:
        # Safe recovery: stop consuming an unsupported graph without editing it.
        _local(scene)
        _registry(actuals)
        item = track(scene, actual)
        if item is None:
            return None
    with _edit(scene, actual, action_edit=False):
        if item is None:
            item = _new_track(scene, actual, value)
        item.enabled = enabled
    return item


def set_key(scene, actual, value, actuals, *, frame=None):
    value = _value(value)
    item = _author(scene, actual, actuals)
    time = _frame(scene, frame)
    existing = curve(scene, actual)
    _need(existing is None or not existing.lock, 'amount animation curve is locked')
    with _edit(scene, actual, action_edit=True):
        if item is None:
            item = _new_track(scene, actual, value)
        item.enabled = True
        item.amount = value
        action = scene.animation_data.action
        current = action.fcurve_ensure_for_datablock(scene, data_path=key_path(actual), index=0)
        _need(current.data_path == key_path(actual) and current.array_index == 0,
              'amount animation insertion did not preserve the named actual path')
        untouched = {float(k.co.x): _key_state(k) for k in current.keyframe_points if k.co.x != time}
        selected = next((k for k in current.keyframe_points if k.co.x == time), None)
        if selected is None:
            selected = current.keyframe_points.insert(time, value)
        selected.co = (time, value)
        selected.interpolation = 'LINEAR'
        current.extrapolation = 'CONSTANT'
        _finish_keys(current, untouched)
        _graph(scene)
    return item


def remove_key(scene, actual, actuals, *, frame=None):
    _author(scene, actual, actuals)
    time = _frame(scene, frame)
    current = curve(scene, actual)
    _need(current is not None and not current.lock, 'no editable named amount curve at current frame')
    selected = next((k for k in current.keyframe_points if k.co.x == time), None)
    _need(selected is not None, 'no amount key at the exact current frame')
    with _edit(scene, actual, action_edit=True):
        current = curve(scene, actual)
        selected = next(k for k in current.keyframe_points if k.co.x == time)
        untouched = {float(k.co.x): _key_state(k) for k in current.keyframe_points if k.co.x != time}
        current.keyframe_points.remove(selected)
        if current.keyframe_points:
            _finish_keys(current, untouched)
        else:
            _graph(scene)[2].fcurves.remove(current)
        _graph(scene)
