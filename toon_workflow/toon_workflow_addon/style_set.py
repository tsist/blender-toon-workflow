# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable explicit-role style sets; no bpy imports or scene mutations.

normalize without a component callback checks finite static JSON only: its
document is planning data, never evidence that GAME/MANGA settings are valid.
resolve exposes this distinction; bind always requires a component callback.
The caller owns Blender target/slot eligibility, application and rollback.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

SCHEMA = 'toon-style-set.v1'
_FIELDS = {'schema', 'id', 'name', 'revision', 'roles', 'camera_overrides'}
_MAX_BYTES = 16 * 1024 * 1024


def _closed(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(label + ': unexpected or missing fields')


def _text(value, label):
    if (type(value) is not str or not value.strip() or len(value) > 128
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError(label + ': nonempty control-free string required')
    return value


def _static(value, depth=0):
    """Accept JSON types only, rejecting nonfinite values and cycles/depth."""
    if depth > 64:
        raise ValueError('Static JSON nesting exceeds 64 levels')
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return
    if kind is float and math.isfinite(value):
        return
    if kind is list:
        for item in value:
            _static(item, depth + 1)
        return
    if kind is dict and all(type(key) is str for key in value):
        for item in value.values():
            _static(item, depth + 1)
        return
    raise ValueError('Finite static JSON values and string keys required')


def _settings(settings, callback):
    if type(settings) is not dict:
        raise ValueError('Component settings must be a dict')
    _static(settings)
    result = deepcopy(settings)
    if callback is not None:
        result = callback(result)
        if type(result) is not dict:
            raise ValueError('Component normalizer must return a dict')
        _static(result)
    return deepcopy(result)


def normalize(document, normalize_component=None):
    """Return an independent strict document; no callback means planning only."""
    if normalize_component is not None and not callable(normalize_component):
        raise ValueError('normalize_component must be callable')
    _closed(document, _FIELDS, 'Style set')
    if document['schema'] != SCHEMA:
        raise ValueError('Unsupported style-set schema')
    identity = document['id']
    try:
        parsed = uuid.UUID(identity) if type(identity) is str else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.int == 0 or str(parsed) != identity:
        raise ValueError('Canonical nonzero UUID required')
    _text(document['name'], 'name')
    if type(document['revision']) is not int or document['revision'] < 1:
        raise ValueError('Positive integer revision required')
    roles = document['roles']
    if type(roles) is not dict:
        raise ValueError('roles must be a dict')
    normalized = {}
    for role, entry in roles.items():
        _text(role, 'role')
        if type(entry) is not dict:
            raise ValueError('Role entry must be a dict')
        action = entry.get('action')
        if action == 'PRESERVE':
            _closed(entry, {'action'}, 'Preserve role')
            normalized[role] = {'action': action}
        elif action == 'COMPONENT':
            _closed(entry, {'action', 'settings'}, 'Component role')
            normalized[role] = dict(action=action, settings=_settings(
                entry['settings'], normalize_component))
        else:
            raise ValueError('Unknown role action')
    overrides = document['camera_overrides']
    if type(overrides) is not list:
        raise ValueError('camera_overrides must be a list')
    result_overrides, seen = [], set()
    for entry in overrides:
        _closed(entry, {'camera_role', 'role', 'settings'}, 'Camera override')
        camera = _text(entry['camera_role'], 'camera_role')
        role = _text(entry['role'], 'override role')
        if role not in normalized or normalized[role]['action'] != 'COMPONENT':
            raise ValueError('Camera overrides require an existing COMPONENT role')
        if (camera, role) in seen:
            raise ValueError('Duplicate camera_role/role override')
        seen.add((camera, role))
        result_overrides.append(dict(camera_role=camera, role=role,
            settings=_settings(entry['settings'], normalize_component)))
    return dict(schema=SCHEMA, id=identity, name=document['name'],
                revision=document['revision'], roles=normalized,
                camera_overrides=result_overrides)


def resolve(document, camera_role=None, normalize_component=None):
    """Resolve only the explicitly supplied camera role, never object names."""
    doc = normalize(document, normalize_component)
    if camera_role is not None:
        _text(camera_role, 'camera_role')
    roles = doc['roles']
    applied = []
    for entry in doc['camera_overrides']:
        if camera_role is not None and entry['camera_role'] == camera_role:
            roles[entry['role']]['settings'] = deepcopy(entry['settings'])
            applied.append(entry['role'])
    return dict(style_id=doc['id'], revision=doc['revision'],
                camera_role=camera_role, roles=roles, applied_overrides=applied,
                component_validation=('component_validated' if
                    normalize_component is not None else 'planning_only'))


def bind(document, bindings, camera_role=None, normalize_component=None):
    """Return a read-only plan for explicit identities/slots; never apply it.

    Opaque targets retain identity. Eligibility and actual slot existence are
    Blender adapter duties; this module checks duplicate identities and roles.
    """
    if not callable(normalize_component):
        raise ValueError('bind requires a component normalizer; planning is insufficient')
    resolved = resolve(document, camera_role, normalize_component)
    if type(bindings) is not list:
        raise ValueError('bindings must be a list')
    records, seen, bound = [], set(), set()
    for row in bindings:
        _closed(row, {'target', 'slot', 'role'}, 'Binding')
        target, slot, role = row['target'], row['slot'], row['role']
        if target is None or type(slot) is not int or slot < 0:
            raise ValueError('Binding requires a target and integer slot >= 0')
        _text(role, 'binding role')
        if role not in resolved['roles']:
            raise ValueError('Binding role is not declared by this style set')
        identity = (id(target), slot)
        if identity in seen:
            raise ValueError('Duplicate target identity/slot binding')
        seen.add(identity)
        bound.add(role)
        records.append(dict(target=target, slot=slot, role=role,
                            **deepcopy(resolved['roles'][role])))
    return dict(status='ready', style_id=resolved['style_id'],
                revision=resolved['revision'], camera_role=camera_role,
                component_validation=resolved['component_validation'],
                records=records, unbound_roles=sorted(set(resolved['roles']) - bound))


def _path(path, existing=False):
    result = Path(path)
    if (not result.is_absolute() or result.suffix.lower() != '.json'
            or not result.parent.is_dir()):
        raise ValueError('Absolute .json path in an existing directory required')
    if existing and (not result.is_file() or result.stat().st_size > _MAX_BYTES):
        raise ValueError('Existing JSON file up to 16MiB required')
    return result


def export_file(document, path, normalize_component=None):
    """Create a new JSON file exclusively; absent callback exports planning data."""
    doc = normalize(document, normalize_component)
    target = _path(path)
    data = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)
    if len(data.encode('utf8')) > _MAX_BYTES:
        raise ValueError('Style-set JSON exceeds 16MiB')
    with target.open('x', encoding='utf8', newline='\n') as stream:
        stream.write(data)
    return doc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key: ' + key)
        result[key] = value
    return result


def load_file(path, normalize_component=None):
    """Load bounded strict JSON; explicitly recognized v0 data is migrated."""
    source = _path(path, existing=True)
    with source.open('rb') as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('Style-set JSON exceeds 16MiB')
    def reject(value):
        raise ValueError('Nonfinite JSON constant: ' + value)
    doc = json.loads(raw.decode('utf-8-sig'), parse_constant=reject,
                     object_pairs_hook=_unique_object)
    if type(doc) is dict and doc.get('schema') == 'toon-style-set.v0':
        return migrate_v0(doc, normalize_component)
    return normalize(doc, normalize_component)


def migrate_v0(document, normalize_component=None):
    """Only the explicit initial style-set v0 is supported, never CEL families."""
    _closed(document, {'schema', 'id', 'name', 'roles'}, 'Style set v0')
    if document['schema'] != 'toon-style-set.v0':
        raise ValueError('Unsupported migration schema')
    return normalize(dict(schema=SCHEMA, id=document['id'], name=document['name'],
        revision=1, roles=deepcopy(document['roles']), camera_overrides=[]),
        normalize_component)
