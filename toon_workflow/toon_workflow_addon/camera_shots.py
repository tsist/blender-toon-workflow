# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit camera-shot presets for existing independent camera corrections.

No handlers, timers, object copies or implicit weight/animation edits. Scene ID
references retain an otherwise unlinked Collection tree; object references are
ID properties, never collection object links. Importing this module is bpy-free.
"""
import json
import re
import uuid

from . import camera_correction as correction

PROFILE = 'toon-camera-shots.v1'
KEY = '_toon_camera_shots'
_SCHEMA = 'toon_shots_schema'
_OWNER = 'toon_shots_owner'
_ROLE = 'toon_shots_role'
_ID = 'toon_shots_id'
_CAMERA = 'toon_shots_camera'
_REFERENCE = 'toon_shots_reference'
_STRENGTH = 'toon_shots_strength'
_TARGET = 'toon_shots_target'
_MODIFIER = 'toon_shots_modifier_uid'
_TARGET_OWNER = 'toon_shots_target_owner'
_WEIGHT = 'toon_shots_weight_attribute'
_COUNT = 'toon_shots_count'
_MEMBERS = 'toon_shots_members'
_TARGETS = 'toon_shots_targets'
MAX_SHOTS = 32
MAX_TARGETS = 256


class ShotError(ValueError):
    """Missing, damaged or conflicting explicit camera-shot bindings."""


def _bpy():
    import bpy
    return bpy


def _uuid(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ShotError('Expected a stable camera-shot owner/identifier UUID')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _stored(value, key):
    text = value.get(key)
    if type(text) is not str or len(text) > 256 * 256:
        raise ShotError('Missing or oversized camera-shot membership manifest')
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ShotError('Invalid camera-shot membership manifest') from error
    if type(parsed) is not list:
        raise ShotError('Camera-shot membership must be an explicit list')
    return parsed


def _same(left, right):
    return left == right


def _contains(values, value):
    return any(_same(item, value) for item in values)


def _scene(scene):
    bpy = _bpy()
    if (not isinstance(scene, bpy.types.Scene)
            or scene.library or scene.override_library):
        raise ShotError('An existing local editable Scene is required')
    if not _contains(bpy.data.scenes, scene):
        raise ShotError('The shot Scene no longer exists')


def _object(scene, value, label, *, local=False):
    bpy = _bpy()
    if (not isinstance(value, bpy.types.Object)
            or not _contains(bpy.data.objects, value)
            or not _contains(scene.objects, value)):
        raise ShotError(label + ' must be an existing Object in the requested Scene')
    if local and (value.library or value.override_library):
        raise ShotError(label + ' must be local and editable; overrides are not qualified')
    return value


def _camera_reference(scene, camera, reference):
    _object(scene, camera, 'camera')
    _object(scene, reference, 'reference')
    if camera.type != 'CAMERA' or camera.data.type != 'PERSP':
        raise ShotError('A perspective camera Object is required')


def _managed(target):
    candidates = [modifier for modifier in target.modifiers
        if modifier.type == 'NODES' and modifier.node_group is not None
        and modifier.node_group.get(correction.PROFILE_KEY) == correction.PROFILE]
    if len(candidates) != 1:
        raise ShotError('Each target must have exactly one existing managed camera correction')
    modifier = candidates[0]
    group, identifiers = correction._owned(target, modifier)
    owner = _uuid(group.get(correction.OWNER))
    uid = modifier.persistent_uid
    if type(uid) is not int or uid < 0:
        raise ShotError('A valid modifier persistent_uid is required')
    weight = correction._input(modifier, identifiers, 'weight').value
    correction._weight_values(target, weight)
    return dict(target=target, modifier=modifier, identifiers=identifiers,
                owner=owner, uid=uid, weight=weight)


def _parents(collection):
    bpy = _bpy()
    values = list(bpy.data.collections) + [scene.collection for scene in bpy.data.scenes]
    return [value for value in values if _contains(value.children, collection)]


def _collection(value, owner, role, parent):
    bpy = _bpy()
    if (not isinstance(value, bpy.types.Collection)
            or not _contains(bpy.data.collections, value)
            or value.library or value.override_library
            or value.get(_SCHEMA) != PROFILE or value.get(_ROLE) != role
            or value.get(_OWNER) != owner):
        raise ShotError('Missing or unowned camera-shot ' + role + ' Collection')
    if len(value.objects):
        raise ShotError('Managed camera-shot Collections must not contain linked Objects')
    parents = _parents(value)
    if (parent is None and parents) or (parent is not None and
            (len(parents) != 1 or not _same(parents[0], parent))):
        raise ShotError('Camera-shot Collection was externally linked, moved or shared')
    if getattr(value, 'use_fake_user', False) or value.users != 1:
        raise ShotError('Camera-shot Collection must have exactly its declared owner reference')
    animation = getattr(value, 'animation_data', None)
    if animation is not None and (animation.action or animation.drivers or animation.nla_tracks):
        raise ShotError('Managed shot metadata cannot override native animation')


def _members(value, expected):
    if (type(value.get(_COUNT)) is not int or value[_COUNT] != len(expected)
            or _stored(value, _MEMBERS) != expected):
        raise ShotError('Camera-shot child count/owner UID manifest differs; a binding was lost or added')


def _read(scene):
    """Structural inspection only; the selected shot's numeric preflight is apply-owned."""
    _scene(scene)
    if KEY not in scene:
        return None
    root = scene.get(KEY)
    bpy = _bpy()
    if not isinstance(root, bpy.types.Collection):
        raise ShotError('Scene camera-shot root reference is missing or invalid')
    owner = _uuid(root.get(_OWNER))
    _collection(root, owner, 'root', None)
    if any(not _same(other, scene) and _same(other.get(KEY), root) for other in bpy.data.scenes):
        raise ShotError('The camera-shot root is shared by another Scene')
    children = list(root.children)
    if not 1 <= len(children) <= MAX_SHOTS:
        raise ShotError('Expected 1..32 explicitly owned camera shots')
    shots, shot_ids, seen_cameras, target_set, target_ids = [], set(), [], None, None
    for shot in children:
        _collection(shot, owner, 'shot', root)
        shot_id = _uuid(shot.get(_ID))
        if shot_id in shot_ids:
            raise ShotError('Duplicate camera-shot UUID')
        shot_ids.add(shot_id)
        camera, reference = shot.get(_CAMERA), shot.get(_REFERENCE)
        _camera_reference(scene, camera, reference)
        if _contains(seen_cameras, camera):
            raise ShotError('A camera Object has multiple shot definitions')
        seen_cameras.append(camera)
        strength = correction._strength(shot.get(_STRENGTH))
        bindings = list(shot.children)
        if not 1 <= len(bindings) <= MAX_TARGETS:
            raise ShotError('Expected 1..256 explicitly bound targets per shot')
        rows, identities, binding_ids, owners = [], [], set(), set()
        for binding in bindings:
            _collection(binding, owner, 'binding', shot)
            if len(binding.children):
                raise ShotError('Target bindings must not have child Collections')
            binding_id = _uuid(binding.get(_ID))
            if binding_id in binding_ids:
                raise ShotError('Duplicate target binding UUID')
            binding_ids.add(binding_id)
            target = _object(scene, binding.get(_TARGET), 'target', local=True)
            if target.type != 'MESH':
                raise ShotError('A shot target must remain a MESH')
            row = _managed(target)
            if (binding.get(_TARGET_OWNER) != row['owner']
                    or type(binding.get(_MODIFIER)) is not int
                    or binding[_MODIFIER] != row['uid'] or binding.get(_WEIGHT) != row['weight']):
                raise ShotError('Target correction owner/modifier UID/weight binding changed')
            if row['owner'] in owners:
                raise ShotError('A shot has duplicate target owner UUIDs')
            owners.add(row['owner'])
            identities.append(dict(owner=row['owner'], modifier_uid=row['uid'], binding_id=binding_id))
            rows.append(row)
        _members(shot, sorted(identities, key=lambda row: row['owner']))
        if target_set is None:
            target_set = sorted(owners)
            target_ids = {row['owner']: (row['target'], row['uid']) for row in rows}
        elif sorted(owners) != target_set:
            raise ShotError('All shots must bind the same target owner UUID set')
        elif any(not _same(row['target'], target_ids[row['owner']][0])
                 or row['uid'] != target_ids[row['owner']][1] for row in rows):
            raise ShotError('A target owner UUID was reused for a different Object/modifier identity')
        shots.append(dict(collection=shot, id=shot_id, camera=camera,
                          reference=reference, strength=strength, targets=rows))
    _members(root, sorted(shot_ids))
    if _stored(root, _TARGETS) != target_set:
        raise ShotError('The root target owner UUID manifest changed')
    return dict(root=root, owner=owner, shots=shots, target_set=target_set)


def _receipt(owner, shot, status):
    return dict(profile=PROFILE, status=status, owner=owner, shot_id=shot['id'],
        camera=shot['camera'].name, reference=shot['reference'].name,
        strength=shot['strength'], targets=[dict(target=row['target'].name,
            owner=row['owner'], modifier_uid=row['uid'], weight_attribute=row['weight'])
            for row in shot['targets']])


def inspect(scene):
    """Read and validate the complete ID-reference tree without changing it."""
    value = _read(scene)
    if value is None:
        return None
    active = next((shot for shot in value['shots'] if _same(shot['camera'], scene.camera)), None)
    return dict(profile=PROFILE, owner=value['owner'], shot_count=len(value['shots']),
        target_count=len(value['target_set']), active_camera=scene.camera.name if scene.camera else None,
        active_shot_id=active['id'] if active else None,
        shots=[_receipt(value['owner'], shot, 'defined') for shot in value['shots']])


def _new(name, owner, role, created):
    from .._ui_kit import apply_collection_theme

    value = _bpy().data.collections.new(name)
    created.append(value)
    apply_collection_theme(value, 'toon')
    value[_SCHEMA], value[_OWNER], value[_ROLE] = PROFILE, owner, role
    return value


def _remove_created(created):
    # Only IDs allocated by this call are eligible for cleanup.
    failures = []
    for value in reversed(created):
        try:
            _bpy().data.collections.remove(value, do_unlink=True)
        except Exception as error:
            failures.append(str(error))
    if failures:
        raise ShotError('Created camera-shot Collection cleanup failed: ' + '; '.join(failures))


def define(scene, camera, reference, targets, *, strength=0.0, replace=False):
    """Define a preset from existing valid independent managed modifiers.

    Defining or explicitly replacing a preset never configures a modifier.
    Replacing the same camera retains its stable shot UUID and Collection.
    """
    _scene(scene)
    strength = correction._strength(strength)
    if type(replace) is not bool:
        raise ShotError('replace must be explicit Boolean')
    _camera_reference(scene, camera, reference)
    if type(targets) not in (list, tuple) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ShotError('Provide an explicit list of 1..256 targets')
    rows, seen_targets, owners = [], [], set()
    for target in targets:
        _object(scene, target, 'target', local=True)
        if target.type != 'MESH' or _contains(seen_targets, target):
            raise ShotError('Targets must be distinct local MESH Objects')
        seen_targets.append(target)
        row = _managed(target)
        if row['owner'] in owners:
            raise ShotError('Targets must have independent managed owner UUIDs')
        owners.add(row['owner'])
        current = correction.inspect(target, row['modifier'], evaluate=False)
        if current.get('status') != 'valid':
            raise ShotError('Existing target correction is invalid: ' + target.name)
        correction._preflight(target, camera, reference, row['weight'], strength)
        rows.append(row)
    existing = _read(scene)
    if existing is not None and sorted(owners) != existing['target_set']:
        raise ShotError('A new shot must retain the existing target owner UUID set')
    if existing is not None:
        identity = {row['owner']: (row['target'], row['uid']) for row in existing['shots'][0]['targets']}
        if any(not _same(row['target'], identity[row['owner']][0])
               or row['uid'] != identity[row['owner']][1] for row in rows):
            raise ShotError('A shot must retain the same Object/modifier identities, not cloned owner UUIDs')
    previous = next((shot for shot in existing['shots'] if _same(shot['camera'], camera)), None) if existing else None
    if previous is not None:
        if not replace:
            raise ShotError('This camera already has a shot; explicit replace=True is required')
        collection = previous['collection']
        old = {key: collection[key] for key in (_CAMERA, _REFERENCE, _STRENGTH)}
        try:
            collection[_CAMERA], collection[_REFERENCE], collection[_STRENGTH] = camera, reference, strength
        except Exception:
            for key, value in old.items():
                collection[key] = value
            raise
        shot = dict(previous, camera=camera, reference=reference, strength=strength)
        return _receipt(existing['owner'], shot, 'replaced')
    if existing is not None and len(existing['shots']) >= MAX_SHOTS:
        raise ShotError('At most 32 camera shots are supported')
    created, root = [], existing['root'] if existing else None
    owner = existing['owner'] if existing else uuid.uuid4().hex
    shot_id = uuid.uuid4().hex
    old_members = list(existing['shots']) if existing else []
    try:
        if root is None:
            root = _new('Toon Camera Shots', owner, 'root', created)
            root[_TARGETS] = _json(sorted(owners))
        shot = _new('Toon Shot ' + camera.name, owner, 'shot', created)
        shot[_ID], shot[_CAMERA], shot[_REFERENCE], shot[_STRENGTH] = shot_id, camera, reference, strength
        identities = []
        for row in rows:
            binding = _new('Toon Shot Target ' + row['target'].name, owner, 'binding', created)
            binding_id = uuid.uuid4().hex
            binding[_ID], binding[_TARGET] = binding_id, row['target']
            binding[_TARGET_OWNER], binding[_MODIFIER], binding[_WEIGHT] = row['owner'], row['uid'], row['weight']
            shot.children.link(binding)
            identities.append(dict(owner=row['owner'], modifier_uid=row['uid'], binding_id=binding_id))
        shot[_COUNT], shot[_MEMBERS] = len(rows), _json(sorted(identities, key=lambda row: row['owner']))
        root.children.link(shot)
        root[_COUNT] = len(old_members) + 1
        root[_MEMBERS] = _json(sorted([value['id'] for value in old_members] + [shot_id]))
        if existing is None:
            scene[KEY] = root
    except Exception:
        if existing is not None:
            root[_COUNT], root[_MEMBERS] = len(old_members), _json(sorted(value['id'] for value in old_members))
        elif _same(scene.get(KEY), root) and KEY in scene:
            del scene[KEY]
        _remove_created(created)
        raise
    return _receipt(owner, dict(id=shot_id, camera=camera, reference=reference,
                                strength=strength, targets=rows), 'defined')


def apply(scene):
    """Apply the exact active camera's preset as an all-target transaction."""
    value = _read(scene)
    if value is None:
        return None
    selected = [shot for shot in value['shots'] if _same(shot['camera'], scene.camera)]
    if len(selected) != 1:
        raise ShotError('The effective scene.camera Object has no unique declared shot')
    shot = selected[0]
    snapshots = []
    for row in shot['targets']:
        target, modifier, identifiers = row['target'], row['modifier'], row['identifiers']
        # Complete all expensive/animated-input admission before the first write.
        correction._owned(target, modifier)
        correction._preflight(target, shot['camera'], shot['reference'], row['weight'], shot['strength'])
        parameters = {key: correction._input(modifier, identifiers, key)
                      for key in ('camera', 'reference', 'weight', 'strength')}
        if correction._animated_paths(target).intersection(
                parameter.path_from_id('value') for parameter in parameters.values()):
            raise ShotError('Shot apply conflicts with native parameter FCurves/drivers: ' + target.name)
        snapshots.append((target, parameters, {key: parameter.value for key, parameter in parameters.items()}))
    try:
        for row in shot['targets']:
            correction.configure(row['target'], row['modifier'], camera=shot['camera'],
                reference=shot['reference'], weight_attribute=row['weight'], strength=shot['strength'])
    except Exception as error:
        failures = []
        for target, parameters, original in reversed(snapshots):
            for key, parameter in parameters.items():
                try:
                    parameter.value = original[key]
                except Exception as rollback_error:
                    failures.append(target.name + ':' + key + ':' + str(rollback_error))
            try:
                target.update_tag()
            except Exception as rollback_error:
                failures.append(target.name + ':update:' + str(rollback_error))
        if failures:
            raise ShotError('Shot apply failed; instance rollback also failed: ' + '; '.join(failures)) from error
        raise
    receipt=_receipt(value['owner'], shot, 'applied')
    # ID-property author values are doubles; modifier RNA is native float32.
    # Keep both identities instead of testing the requested decimal as if it
    # were the exact value actually evaluated by Geometry Nodes.
    receipt['requested_strength']=receipt['strength']
    row=shot['targets'][0]
    receipt['strength']=float(correction._input(row['modifier'],row['identifiers'],'strength').value)
    receipt['strength_precision']='native modifier RNA'
    return receipt
