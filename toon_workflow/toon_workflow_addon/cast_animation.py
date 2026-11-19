# SPDX-License-Identifier: GPL-3.0-or-later
"""Native strength animation on one opt-in cast-art ShaderNodeTree.

Only the internal strength Value is animated. The Material tree, its SDF
binding and all other acceptance controls remain outside this Action. Legal
native LINEAR point edits are inspected as current author data, never compared
with an obsolete baseline of point coordinates.
"""
import math
import struct
import uuid

from .joint_time import MIN_FRAME, MAX_FRAME

SCHEMA = 'toon-cast-animation.v1'
PROFILE = 'toon_cast_animation_profile'
ACTION_ID = 'toon_cast_animation_action_uuid'
_ROLE = 'strength_value'
_AD_FIELDS = ('action_extrapolation', 'action_blend_type', 'action_influence',
              'use_nla', 'use_pin')
_POINT_FIELDS = ('interpolation', 'easing', 'handle_left_type', 'handle_right_type',
                 'back', 'amplitude', 'period', 'type', 'select_control_point',
                 'select_left_handle', 'select_right_handle')


class CastAnimationError(ValueError):
    pass


def _need(condition, message):
    if not condition:
        raise CastAnimationError(message)


def _cast():
    # Avoid a module-import cycle with face_cast_art's v2 graph validation.
    from . import face_cast_art
    return face_cast_art


def _number(value, minimum, maximum, label):
    _need(type(value) in (int, float) and minimum <= value <= maximum
          and math.isfinite(value), label + ' is outside its finite range')
    return float(value)


def _f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _uuid(value):
    _need(type(value) is str, 'Cast animation UUID must be a string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise CastAnimationError('Cast animation UUID is invalid') from error
    _need(parsed.int != 0 and str(parsed) == value,
          'Cast animation UUID is not canonical')
    return value


def parameter_socket(group):
    """Return the sole internal v2 strength Value socket, without recursion."""
    import bpy
    T = _cast()
    _need(isinstance(group, bpy.types.ShaderNodeTree)
          and group.get(T.PROFILE_KEY) == T.PROFILE_V2,
          'Internal strength animation requires an opt-in cast-art v2 Group')
    rows = [node for node in group.nodes if node.get(T._NODE) == _ROLE]
    _need(len(rows) == 1 and rows[0].bl_idname == 'ShaderNodeValue',
          'Cast Group has no unique internal strength Value')
    node = rows[0]
    _need(len(node.outputs) == 1 and not node.mute,
          'Cast strength Value socket inventory or mute state changed')
    socket = node.outputs[0]
    _need(socket.id_data == group and type(socket.default_value) in (int, float),
          'Cast strength parameter is not a scalar socket on its Group')
    _number(float(socket.default_value), 0, 1, 'Cast strength value')
    return socket


def _incoming_drivers(group):
    import bpy
    seen = set()
    result = []
    for block in bpy.data.user_map():
        # Embedded trees may be represented by their owning ID in user_map.
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
    T = _cast()
    _need(isinstance(group, bpy.types.ShaderNodeTree)
          and group in list(bpy.data.node_groups) and not group.is_embedded_data
          and not group.library and not group.override_library and group.is_editable
          and not group.use_fake_user and not group.use_extra_user,
          'Expected a local editable standalone cast-art ShaderNodeTree')
    _need(group.get(T.PROFILE_KEY) == T.PROFILE_V2,
          'Group has no supported cast-art animation profile')
    owner = group.get(T.OWNER)
    _need(T._owner(owner), 'Cast Group owner is invalid')
    _need(sum(item.get(T.OWNER) == owner for item in bpy.data.node_groups) == 1,
          'Cast Group owner identity is duplicated')
    instances = [(material, node) for material in bpy.data.materials
                 if material.node_tree for node in material.node_tree.nodes
                 if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == group]
    _need(len(instances) == 1, 'Cast Group must have one Material instance')
    material, instance = instances[0]
    _need(not material.library and not material.override_library and material.is_editable
          and material.use_nodes and material.node_tree.is_editable
          and not material.node_tree.library,
          'Cast Material instance is not local and editable')
    _need(instance.get(T.OWNER) == owner and instance.get(T._MARK) == T.PROFILE_V2,
          'Cast Material instance owner/profile changed')
    consumers = set(bpy.data.user_map(subset={group}).get(group, ()))
    _need(group.users == 1 and consumers == {material},
          'Cast Group has an additional or foreign consumer')
    parameter_socket(group)
    _need(not _incoming_drivers(group),
          'An external driver references this cast-art Group')
    return owner


def _path(group):
    socket = parameter_socket(group)
    path = socket.path_from_id('default_value')
    _need(type(path) is str and bool(path), 'Cast strength parameter path is missing')
    return path


def _point(point):
    result = {key: getattr(point, key) for key in _POINT_FIELDS}
    result.update(co=list(point.co), handle_left=list(point.handle_left),
                  handle_right=list(point.handle_right))
    return result


def _curve(curve):
    _need(curve.array_index == 0 and not curve.mute and curve.group is None
          and not curve.modifiers and not curve.sampled_points
          and curve.extrapolation == 'CONSTANT',
          'Cast F-Curve has unsupported channels, groups, modifiers or evaluation state')
    points = [_point(point) for point in curve.keyframe_points]
    _need(bool(points), 'Cast F-Curve has no keyframes')
    last = None
    for point in points:
        frame, value = point['co']
        _number(frame, MIN_FRAME, MAX_FRAME, 'Cast keyframe time')
        _number(value, 0, 1, 'Cast strength key value')
        _need(last is None or frame > last,
              'Cast keyframe times must be unique and increasing')
        _need(point['interpolation'] == 'LINEAR',
              'Cast strength keyframes must use LINEAR interpolation')
        for handle in ('handle_left', 'handle_right'):
            _need(all(math.isfinite(value) for value in point[handle]),
                  'Cast keyframe handle is nonfinite')
        for name in ('back', 'amplitude', 'period'):
            _need(math.isfinite(point[name]), 'Cast keyframe property is nonfinite')
        last = frame
    return dict(key='strength', data_path=curve.data_path, array_index=0,
                extrapolation=curve.extrapolation, mute=False, modifiers=[],
                keyframes=points)


def _inspect(group):
    import bpy
    T = _cast()
    owner = _group(group)
    path = _path(group)
    ad = group.animation_data
    if ad is not None:
        _need(not ad.drivers and not ad.nla_tracks and not ad.use_tweak_mode,
              'Cast Group drivers, NLA or tweak mode are not supported')
    action = ad.action if ad is not None else None
    report = dict(schema=SCHEMA, status='valid', group=group.name, owner=owner,
                  animated_keys=[], action=None, curves=[])
    if action is None:
        _need(PROFILE not in group and ACTION_ID not in group,
              'Cast animation metadata exists without its Action')
        return report, None, None
    _need(not action.library and not action.override_library and action.is_editable,
          'Cast Action is not local and editable')
    _need(group.get(PROFILE) == SCHEMA and action.get(PROFILE) == SCHEMA
          and action.get(T.OWNER) == owner,
          'Foreign or unowned Action is assigned to cast Group')
    action_id = _uuid(group.get(ACTION_ID))
    _need(action.get(ACTION_ID) == action_id, 'Cast Action UUID changed')
    _need([item for item in bpy.data.actions if item.get(PROFILE) == SCHEMA
           and item.get(ACTION_ID) == action_id] == [action],
          'Cast Action UUID is duplicated')
    _need(not action.use_fake_user and not action.use_extra_user and action.users == 1
          and set(bpy.data.user_map(subset={action}).get(action, ())) == {group},
          'Cast Action has an extra or foreign native consumer')
    _need(not action.use_frame_range and not action.use_cyclic,
          'Cast Action manual/cyclic frame range is not supported')
    _need(ad.action_extrapolation == 'HOLD' and ad.action_blend_type == 'REPLACE'
          and ad.action_influence == 1.0,
          'Cast Action evaluation context changed')
    slots = list(action.slots)
    _need(len(slots) == 1 and ad.action_slot == slots[0]
          and slots[0].target_id_type == 'NODETREE',
          'Cast Action must have one selected NODETREE slot')
    slot = slots[0]
    layers = list(action.layers)
    _need(len(layers) == 1 and len(layers[0].strips) == 1,
          'Cast Action must have one layer and one strip')
    strip = layers[0].strips[0]
    _need(strip.type == 'KEYFRAME' and len(strip.channelbags) == 1,
          'Cast Action must have one KEYFRAME strip/channelbag')
    bag = strip.channelbags[0]
    _need(slot.handle == ad.action_slot_handle == bag.slot_handle,
          'Cast Action selected slot/channelbag handles disagree')
    _need(not bag.groups and len(bag.fcurves) == 1,
          'Cast Action must have one ungrouped strength curve')
    curve = bag.fcurves[0]
    _need(curve.data_path == path, 'Cast Action has an unknown strength parameter path')
    row = _curve(curve)
    report.update(animated_keys=['strength'], curves=[row],
        action=dict(name=action.name, uuid=action_id, profile=SCHEMA,
                    slot=dict(identifier=slot.identifier, handle=slot.handle,
                              target_id_type=slot.target_id_type),
                    layer=layers[0].name, strip_type=strip.type,
                    channelbag_slot_handle=bag.slot_handle))
    return report, action, bag


def inspect(group):
    """Inspect current legal native author data; never inspect an old baseline."""
    return _inspect(group)[0]


def _metadata(group):
    return {key: group[key] for key in (PROFILE, ACTION_ID) if key in group}


def _restore_metadata(group, values):
    for key in (PROFILE, ACTION_ID):
        if key in values:
            group[key] = values[key]
        elif key in group:
            del group[key]


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
    # update() can solve AUTO handles; restore exact saved handle coordinates
    # last. For LINEAR curves they do not affect the evaluated value.
    for point, row in zip(points, rows):
        point.handle_left = row['handle_left']
        point.handle_right = row['handle_right']


def _configure_action(group, action, owner):
    T = _cast()
    action[PROFILE], action[T.OWNER] = SCHEMA, owner
    action[ACTION_ID] = str(uuid.uuid4())
    action.use_fake_user = False
    slot = action.slots.new(id_type='NODETREE', name=group.name)
    strip = action.layers.new('Cast strength').strips.new(type='KEYFRAME')
    bag = strip.channelbag(slot, ensure=True)
    ad = group.animation_data_create()
    ad.action, ad.action_slot = action, slot
    ad.action_extrapolation, ad.action_blend_type, ad.action_influence = 'HOLD', 'REPLACE', 1.0
    group[PROFILE], group[ACTION_ID] = SCHEMA, action[ACTION_ID]
    return bag


def insert_key(group, frame, value):
    """Insert/update the sole strength curve with complete native rollback."""
    import bpy
    frame = _f32(_number(frame, MIN_FRAME, MAX_FRAME, 'Cast keyframe time'))
    value = _f32(_number(value, 0, 1, 'Cast strength key value'))
    before, action, bag = _inspect(group)
    previous_ad, previous_metadata = _ad_state(group), _metadata(group)
    socket = parameter_socket(group)
    previous_value = float(socket.default_value)
    path = _path(group)
    curve = bag.fcurves[0] if bag is not None else None
    points = [_point(point) for point in curve.keyframe_points] if curve else None
    created_action = None
    created_curve = False
    try:
        if action is None:
            # Track the actual ID before setup, including partial setup failure.
            created_action = bpy.data.actions.new('Toon Cast Animation ' + before['owner'][:8])
            bag = _configure_action(group, created_action, before['owner'])
        if curve is None:
            curve = bag.fcurves.new(data_path=path, index=0, group_name='')
            curve.extrapolation = 'CONSTANT'
            created_curve = True
        socket.default_value = value
        point = next((item for item in curve.keyframe_points if float(item.co.x) == frame), None)
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
            socket.default_value = previous_value
            if created_action is not None:
                _need(created_action.users == 0,
                      'Failed cast Action acquired an unexpected consumer')
                bpy.data.actions.remove(created_action)
            _need(inspect(group) == before
                  and float(socket.default_value) == previous_value,
                  'Cast key insertion rollback witness differs')
        except Exception as rollback_error:
            raise CastAnimationError('Cast key insertion failed; rollback incomplete: '
                                     + str(rollback_error)) from error
        raise


def clear(group):
    """Detach an owned Action, retain it with a fake user and keep current value."""
    before, action, _bag = _inspect(group)
    if action is None:
        return before
    previous_ad, previous_metadata = _ad_state(group), _metadata(group)
    socket = parameter_socket(group)
    previous_value = float(socket.default_value)
    previous_fake = action.use_fake_user
    try:
        action.use_fake_user = True
        group.animation_data_clear()
        _restore_metadata(group, {})
        socket.default_value = previous_value
        return inspect(group)
    except Exception as error:
        try:
            action.use_fake_user = previous_fake
            _restore_ad(group, previous_ad)
            _restore_metadata(group, previous_metadata)
            socket.default_value = previous_value
            _need(inspect(group) == before
                  and float(socket.default_value) == previous_value,
                  'Cast clear rollback witness differs')
        except Exception as rollback_error:
            raise CastAnimationError('Cast animation clear failed; rollback incomplete: '
                                     + str(rollback_error)) from error
        raise
