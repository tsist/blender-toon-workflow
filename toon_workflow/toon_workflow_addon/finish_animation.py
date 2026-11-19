# SPDX-License-Identifier: GPL-3.0-or-later
"""Native strength curves owned by one local finish ShaderNodeTree.

The outer Material and its existing SDF/normal readers are never animated here.
Only the Action actually assigned to this Group is inspected. Legal LINEAR
keyframe edits in Blender remain author-editable; no frozen point-list baseline
is used to reject them.
"""
import math
import struct
import uuid

from . import finish_nodes
from .joint_time import MIN_FRAME, MAX_FRAME

SCHEMA = 'toon-finish-animation.v1'
PROFILE = 'toon_finish_animation_profile'
ACTION_ID = 'toon_finish_animation_action_uuid'
KEYS = ('highlight_strength', 'rim_strength')
_AD_FIELDS = ('action_extrapolation', 'action_blend_type', 'action_influence')
_POINT_FIELDS = ('interpolation', 'easing', 'handle_left_type', 'handle_right_type',
                 'back', 'amplitude', 'period', 'type', 'select_control_point',
                 'select_left_handle', 'select_right_handle')


class FinishAnimationError(ValueError):
    pass


def _need(condition, message):
    if not condition:
        raise FinishAnimationError(message)


def _number(value, minimum, maximum, label):
    _need(type(value) in (int, float) and minimum <= value <= maximum
          and math.isfinite(value), label + ' is outside its finite range')
    return float(value)


def _f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _uuid(value):
    _need(type(value) is str, 'Animation UUID must be a string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise FinishAnimationError('Animation UUID is invalid') from error
    _need(parsed.int != 0 and str(parsed) == value, 'Animation UUID is not canonical')
    return value


def _incoming_drivers(group):
    import bpy
    seen = set()
    result = []
    for block in bpy.data.user_map():
        # Embedded material/compositor trees are also checked if their owner is
        # the ID exposed by user_map, without scanning unrelated Actions.
        for item in (block, getattr(block, 'node_tree', None)):
            if item is None or item.as_pointer() in seen:
                continue
            seen.add(item.as_pointer())
            ad = getattr(item, 'animation_data', None)
            if ad is None:
                continue
            for curve in ad.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id == group:
                            result.append(dict(owner=item.name, path=target.data_path))
    return result


def _group(group):
    import bpy
    _need(isinstance(group, bpy.types.NodeTree) and group.bl_idname == 'ShaderNodeTree'
          and group in list(bpy.data.node_groups) and not group.is_embedded_data
          and not group.library and not group.override_library and group.is_editable,
          'Expected a local editable standalone finish ShaderNodeTree')
    _need(group.get(finish_nodes.PROFILE_KEY) in {finish_nodes.PROFILE,finish_nodes.HAIR_PROFILE},
          'Group has no supported finish node profile')
    owner = group.get(finish_nodes.OWNER)
    _need(type(owner) is str and bool(owner), 'Finish Group owner is missing')
    binding_profile = group.get('toon_finish_binding_profile')
    if binding_profile is None:
        _need(group.users <= 1, 'Unbound finish Group has additional consumers')
    else:
        _need(binding_profile == 'toon-cel-finish.v1', 'Finish binding profile is unsupported')
        owners = [material for material in bpy.data.materials
                  if material.get('toon_finish_binding') == group]
        _need(len(owners) == 1, 'Bound finish Group has no unique Material owner')
        material = owners[0]
        incoming = set(bpy.data.user_map(subset={group}).get(group, ()))
        _need(group.users == 2 and bool(incoming)
              and incoming <= {material, material.node_tree},
              'Bound finish Group has additional or foreign consumers')
    finish_nodes.parameters(group)
    _need(not _incoming_drivers(group), 'An external driver references this finish Group')
    return owner


def _paths(group):
    paths = {}
    for key in KEYS:
        socket = finish_nodes.parameter_socket(group, key)
        _need(socket.id_data == group and type(socket.default_value) in (int, float),
              'Finish parameter is not a scalar socket on this Group: ' + key)
        path = socket.path_from_id('default_value')
        _need(type(path) is str and path and path not in paths,
              'Finish parameter path is missing or duplicated')
        paths[path] = key
    return paths


def _point(point):
    result = {key: getattr(point, key) for key in _POINT_FIELDS}
    result.update(co=list(point.co), handle_left=list(point.handle_left),
                  handle_right=list(point.handle_right))
    return result


def _curve(curve, key):
    _need(curve.array_index == 0 and not curve.mute and curve.group is None
          and not curve.modifiers and not curve.sampled_points
          and curve.extrapolation == 'CONSTANT',
          'Finish F-Curve has unsupported channels, groups, modifiers or evaluation state')
    points = [_point(point) for point in curve.keyframe_points]
    _need(bool(points), 'Finish F-Curve has no keyframes')
    last = None
    for point in points:
        frame, value = point['co']
        _number(frame, MIN_FRAME, MAX_FRAME, 'Keyframe time')
        _number(value, 0, 2, 'Strength key value')
        _need(last is None or frame > last, 'Finish keyframe times must be unique and increasing')
        _need(point['interpolation'] == 'LINEAR', 'Finish keyframes must use LINEAR interpolation')
        for handle in ('handle_left', 'handle_right'):
            _need(all(math.isfinite(v) for v in point[handle]), 'Finish keyframe handle is nonfinite')
        for name in ('back', 'amplitude', 'period'):
            _need(math.isfinite(point[name]), 'Finish keyframe property is nonfinite')
        last = frame
    return dict(key=key, data_path=curve.data_path, array_index=0,
                extrapolation=curve.extrapolation, mute=False, modifiers=[],
                keyframes=points)


def _inspect(group):
    import bpy
    owner = _group(group)
    paths = _paths(group)
    ad = group.animation_data
    if ad is not None:
        _need(not ad.drivers and not ad.nla_tracks and not ad.use_tweak_mode,
              'Finish Group drivers, NLA or tweak mode are not supported')
    action = ad.action if ad is not None else None
    report = dict(schema=SCHEMA, status='valid', group=group.name, owner=owner,
                  animated_keys=[], action=None, curves=[])
    if action is None:
        _need(PROFILE not in group and ACTION_ID not in group,
              'Finish animation metadata exists without its Action')
        return report, None, None
    _need(not action.library and not action.override_library and action.is_editable,
          'Finish Action is not local and editable')
    _need(group.get(PROFILE) == SCHEMA and action.get(PROFILE) == SCHEMA
          and action.get(finish_nodes.OWNER) == owner,
          'Foreign or unowned Action is assigned to finish Group')
    action_id = _uuid(group.get(ACTION_ID))
    _need(action.get(ACTION_ID) == action_id, 'Finish Action UUID changed')
    _need([a for a in bpy.data.actions if a.get(PROFILE) == SCHEMA
           and a.get(ACTION_ID) == action_id] == [action],
          'Finish Action UUID is duplicated')
    _need(not action.use_fake_user and action.users == 1
          and set(bpy.data.user_map(subset={action}).get(action, ())) == {group},
          'Finish Action has an extra or foreign native consumer')
    _need(not action.use_frame_range and not action.use_cyclic,
          'Finish Action manual/cyclic frame range is not supported')
    _need(ad.action_extrapolation == 'HOLD' and ad.action_blend_type == 'REPLACE'
          and ad.action_influence == 1.0, 'Finish Action evaluation context changed')
    slots = list(action.slots)
    _need(len(slots) == 1 and ad.action_slot == slots[0]
          and slots[0].target_id_type == 'NODETREE',
          'Finish Action must have one selected NODETREE slot')
    slot = slots[0]
    layers = list(action.layers)
    _need(len(layers) == 1 and len(layers[0].strips) == 1,
          'Finish Action must have one layer and one strip')
    strip = layers[0].strips[0]
    _need(strip.type == 'KEYFRAME' and len(strip.channelbags) == 1,
          'Finish Action must have one KEYFRAME strip/channelbag')
    bag = strip.channelbags[0]
    _need(slot.handle == ad.action_slot_handle == bag.slot_handle,
          'Finish Action selected slot/channelbag handles disagree')
    _need(not bag.groups, 'Finish Action groups are not supported')
    rows = []
    seen = set()
    for curve in bag.fcurves:
        path = curve.data_path
        _need(path in paths and path not in seen, 'Finish Action has an unknown or duplicate parameter path')
        seen.add(path)
        rows.append(_curve(curve, paths[path]))
    _need(bool(rows), 'Owned finish Action has no parameter curves')
    rows.sort(key=lambda row: row['key'])
    report.update(animated_keys=[row['key'] for row in rows], curves=rows,
        action=dict(name=action.name, uuid=action_id, profile=SCHEMA,
                    slot=dict(identifier=slot.identifier, handle=slot.handle,
                              target_id_type=slot.target_id_type),
                    layer=layers[0].name, strip_type=strip.type,
                    channelbag_slot_handle=bag.slot_handle))
    return report, action, bag


def inspect(group):
    """Read a legal static/animated finish Group; conflicts raise ValueError."""
    return _inspect(group)[0]


def _metadata(group):
    return {key: group[key] for key in (PROFILE, ACTION_ID) if key in group}


def _restore_metadata(group, values):
    for key in (PROFILE, ACTION_ID):
        if key in values:
            group[key] = values[key]
        elif key in group:
            del group[key]


def _values(group):
    return {key: float(finish_nodes.parameter_socket(group, key).default_value) for key in KEYS}


def _restore_values(group, values):
    for key, value in values.items():
        finish_nodes.parameter_socket(group, key).default_value = value


def _ad_state(group):
    ad = group.animation_data
    if ad is None:
        return None
    return dict(action=ad.action, slot=ad.action_slot,
                fields={key: getattr(ad, key) for key in _AD_FIELDS})


def _restore_ad(group, previous):
    if previous is None:
        group.animation_data_clear()
        return
    ad = group.animation_data_create()
    ad.action = previous['action']
    if previous['action'] is not None:
        ad.action_slot = previous['slot']
    for key, value in previous['fields'].items():
        setattr(ad, key, value)


def _restore_points(curve, rows):
    points = curve.keyframe_points
    points.clear()
    points.add(len(rows))
    points.foreach_set('co', [value for row in rows for value in row['co']])
    for point, row in zip(points, rows):
        for key in _POINT_FIELDS:
            setattr(point, key, row[key])
    curve.update()
    # update() can solve AUTO handles. Restore their exact stored coordinates
    # last; these curves are LINEAR, so handle edits do not change evaluation.
    for point, row in zip(points, rows):
        point.handle_left = row['handle_left']
        point.handle_right = row['handle_right']


def _configure_action(group, action, owner):
    action[PROFILE], action[finish_nodes.OWNER] = SCHEMA, owner
    action[ACTION_ID] = str(uuid.uuid4())
    action.use_fake_user = False
    slot = action.slots.new(id_type='NODETREE', name=group.name)
    strip = action.layers.new('Finish').strips.new(type='KEYFRAME')
    bag = strip.channelbag(slot, ensure=True)
    ad = group.animation_data_create()
    ad.action, ad.action_slot = action, slot
    ad.action_extrapolation, ad.action_blend_type, ad.action_influence = 'HOLD', 'REPLACE', 1.0
    group[PROFILE], group[ACTION_ID] = SCHEMA, action[ACTION_ID]
    return bag


def insert_key(group, key, frame, value):
    """Insert/update one native LINEAR strength key after full author preflight."""
    import bpy
    _need(type(key) is str and key in KEYS, 'Only highlight_strength/rim_strength may be animated')
    frame = _f32(_number(frame, MIN_FRAME, MAX_FRAME, 'Keyframe time'))
    value = _f32(_number(value, 0, 2, 'Strength key value'))
    before, action, bag = _inspect(group)
    previous_ad, previous_metadata, previous_values = _ad_state(group), _metadata(group), _values(group)
    socket = finish_nodes.parameter_socket(group, key)
    path = socket.path_from_id('default_value')
    curve = next((row for row in bag.fcurves if row.data_path == path), None) if bag else None
    points = [_point(point) for point in curve.keyframe_points] if curve else None
    created_action = None
    created_curve = False
    try:
        if action is None:
            # Capture the exact newly allocated ID before configuring/binding
            # it, so even a configuration failure cannot orphan an untracked ID.
            owner = group[finish_nodes.OWNER]
            created_action = bpy.data.actions.new('Toon Finish Animation ' + owner[:8])
            bag = _configure_action(group, created_action, owner)
        if curve is None:
            curve = bag.fcurves.new(data_path=path, index=0, group_name='')
            curve.extrapolation = 'CONSTANT'
            created_curve = True
        socket.default_value = value
        point = next((p for p in curve.keyframe_points if float(p.co.x) == frame), None)
        if point is None:
            point = curve.keyframe_points.insert(frame, value, options={'FAST'})
        else:
            point.co.y = value
        point.interpolation = 'LINEAR'
        curve.update()
        return inspect(group)
    except Exception as error:
        try:
            if created_action is None:
                if created_curve:
                    bag.fcurves.remove(curve)
                elif points is not None:
                    _restore_points(curve, points)
            _restore_ad(group, previous_ad)
            _restore_metadata(group, previous_metadata)
            _restore_values(group, previous_values)
            if created_action is not None and created_action.users == 0:
                bpy.data.actions.remove(created_action)
            _need(inspect(group) == before, 'Finish key insertion rollback witness differs')
        except Exception as rollback_error:
            raise FinishAnimationError('Finish key insertion failed; rollback incomplete: ' + str(rollback_error)) from error
        raise


def clear(group):
    """Detach a valid owned Action, retain it with a fake user, keep current values."""
    before, action, _bag = _inspect(group)
    if action is None:
        return before
    previous_ad, previous_metadata, previous_values = _ad_state(group), _metadata(group), _values(group)
    previous_fake = action.use_fake_user
    try:
        action.use_fake_user = True
        group.animation_data_clear()
        _restore_metadata(group, {})
        _restore_values(group, previous_values)
        return inspect(group)
    except Exception as error:
        try:
            action.use_fake_user = previous_fake
            _restore_ad(group, previous_ad)
            _restore_metadata(group, previous_metadata)
            _restore_values(group, previous_values)
            _need(inspect(group) == before, 'Finish clear rollback witness differs')
        except Exception as rollback_error:
            raise FinishAnimationError('Finish animation clear failed; rollback incomplete: ' + str(rollback_error)) from error
        raise
