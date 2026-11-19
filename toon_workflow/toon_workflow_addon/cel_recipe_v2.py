# SPDX-License-Identifier: GPL-3.0-or-later
"""Static CEL v2: surface, optional band boundary, finish and Pencil.

This leaf module composes three already-existing native ownership layers:

* ``surface_slots`` (managed EEVEE surface in one explicit used slot),
* ``finish_core`` (static geometry-normal BLINN highlight/rim wrapper), and
* ``cel_lines`` (Pencil clean-BASIC to static PRESET, or no Pencil work).

The v1 ``cel_recipe`` module remains unchanged and is reused for its complete
surface preflight and slot rollback contract.  The v2 document is closed:
PRESERVE roles carry only ``action``; SURFACE roles carry the full v1 settings
and may additionally carry boundary widths and a static finish document.

All checks run before mutation. Mutation order is surface, boundary, finish, Pencil;
rollback runs in reverse order.  A finish that was created or updated and then
fails in an outer verification still has a recorded rollback intent, so this
layer can remove/restore it even when ``finish_core.apply``/``update`` itself
already succeeded.  Only data created by this call with zero users is removed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path


SCHEMA = 'toon-cel-recipe.v2'
PLAN_SCHEMA = 'toon-cel-recipe.plan.v2'
RECEIPT_SCHEMA = 'toon-cel-recipe.receipt.v2'
INSPECT_SCHEMA = 'toon-cel-recipe.inspect.v2'

MAX_JSON_BYTES = 1024 * 1024
MAX_ROLES = 32
MAX_LINES = 32
MAX_NAME_CHARS = 128
LINE_POLICIES = ('CREATE_FROM_BASIC', 'PRESERVE')

_ROLE_RE = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
_TOP_FIELDS = ('schema', 'recipe_id', 'name', 'revision', 'roles', 'lines')
_ROLE_FIELDS = {'action'}
_SURFACE_ROLE_FIELDS = {'action', 'settings'}
_LINE_FIELDS = ('role', 'width', 'color')
_BINDING_FIELDS = ('role', 'object', 'slot', 'link', 'material')
_PROTECTED_ROLE_TOKENS = {'face', 'head', 'sdf', 'normal', 'normals'}
_FINISH_REPORT_FIELDS = (
    'profile', 'status', 'owner', 'target', 'material', 'group',
    'normal_mode', 'highlight_source', 'settings', 'declared', 'animation',
)


class RecipeError(ValueError):
    """Invalid v2 recipe data, binding, native state or transactional failure."""


def _fail(message, path=None):
    if path:
        raise RecipeError(str(path) + ': ' + str(message))
    raise RecipeError(str(message))


def _cel_recipe():
    from . import cel_recipe

    return cel_recipe


def _materials():
    from . import materials

    return materials


def _surface_slots():
    from . import surface_slots

    return surface_slots


def _finish_core():
    from . import finish_core

    return finish_core


def _band_boundary():
    from . import band_boundary

    return band_boundary


def _finish_nodes():
    from . import finish_nodes

    return finish_nodes


def _cel_lines():
    from . import cel_lines

    return cel_lines


def _bpy():
    import bpy

    return bpy


def _exact_fields(value, fields, path):
    if not isinstance(value, dict):
        _fail('object required', path)
    if any(type(key) is not str for key in value):
        _fail('string keys required', path)
    actual = set(value)
    expected = set(fields)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        parts = []
        if missing:
            parts.append('missing ' + ', '.join(missing))
        if extra:
            parts.append('unknown ' + ', '.join(extra))
        _fail('exact fields required (' + '; '.join(parts) + ')', path)


def _check_name(value, path):
    if type(value) is not str:
        _fail('name must be a string', path)
    if not 1 <= len(value) <= MAX_NAME_CHARS:
        _fail('name must contain 1..%d characters' % MAX_NAME_CHARS, path)
    if any(ord(character) < 32 for character in value):
        _fail('name must not contain C0 control characters', path)
    return value


def _check_uuid(value, path):
    if type(value) is not str:
        _fail('canonical UUID string required', path)
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise RecipeError(str(path) + ': canonical UUID string required') from error
    if str(parsed) != value or parsed.int == 0:
        _fail('canonical nonzero UUID string required', path)
    return value


def _check_role_key(value, path):
    if type(value) is not str or _ROLE_RE.fullmatch(value) is None:
        _fail('role key must match [a-z][a-z0-9_]{0,31}', path)
    return value


def _protected_role_name(role):
    if role in _PROTECTED_ROLE_TOKENS:
        return True
    tokens = role.split('_')
    return (
        role.startswith('face_')
        or role.startswith('head_')
        or 'sdf' in tokens
        or 'normal' in tokens
        or 'normals' in tokens
        or role.endswith('_sdf')
        or role.endswith('_normal')
        or role.endswith('_normals')
    )


def _normalized_settings(value, path):
    materials = _materials()
    try:
        settings = materials._settings(value, 'BLENDER_EEVEE')
    except Exception as error:
        raise RecipeError(str(path) + ': invalid surface settings: ' + str(error)) from error
    if settings.get('mode') not in ('eevee_cel3', 'eevee_cel3_reflect'):
        _fail("settings.mode must be 'eevee_cel3' or 'eevee_cel3_reflect'", path + '.mode')
    return settings


def _normalized_finish(value, path):
    finish_nodes = _finish_nodes()
    try:
        return finish_nodes.normalize(value)
    except Exception as error:
        raise RecipeError(str(path) + ': invalid static finish: ' + str(error)) from error


def _normalized_color(value, path):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail('color must be a four-channel finite list', path)
    color = []
    for channel in value:
        if type(channel) not in (int, float) or not math.isfinite(channel):
            _fail('color channels must be finite numbers', path)
        if not 0.0 <= float(channel) <= 1.0:
            _fail('color channels must be in [0,1]', path)
        color.append(float(channel))
    if color[3] != 1.0:
        _fail('line color alpha must be exactly 1', path)
    return color


def normalize(document):
    """Return a fresh, closed, JSON-safe normalized v2 recipe document."""

    _exact_fields(document, _TOP_FIELDS, 'document')
    if document['schema'] != SCHEMA:
        _fail("schema must be %r" % SCHEMA, 'document.schema')

    recipe_id = _check_uuid(document['recipe_id'], 'document.recipe_id')
    name = _check_name(document['name'], 'document.name')
    revision = document['revision']
    if type(revision) is not int or revision <= 0:
        _fail('revision must be a positive integer', 'document.revision')

    roles_value = document['roles']
    if not isinstance(roles_value, dict):
        _fail('roles must be an object', 'document.roles')
    if not 1 <= len(roles_value) <= MAX_ROLES:
        _fail('roles must contain 1..%d entries' % MAX_ROLES, 'document.roles')

    roles = {}
    for role, spec in roles_value.items():
        role_path = 'document.roles.' + str(role)
        _check_role_key(role, role_path)
        if not isinstance(spec, dict):
            _fail('role entry must be an object', role_path)
        action = spec.get('action')
        if action == 'PRESERVE':
            _exact_fields(spec, _ROLE_FIELDS, role_path)
            roles[role] = {'action': 'PRESERVE'}
        elif action == 'SURFACE':
            allowed = _SURFACE_ROLE_FIELDS | {'finish', 'boundary'}
            if (not _SURFACE_ROLE_FIELDS.issubset(spec)
                    or not set(spec).issubset(allowed)):
                _exact_fields(spec, _SURFACE_ROLE_FIELDS, role_path)
            if _protected_role_name(role):
                _fail('protected face/head/SDF/normal role must be PRESERVE', role_path)
            normalized = {
                'action': 'SURFACE',
                'settings': _normalized_settings(
                    spec['settings'], role_path + '.settings'
                ),
            }
            if 'boundary' in spec:
                try:
                    normalized['boundary'] = _band_boundary().validate_windows(
                        spec['boundary'], normalized['settings']['threshold_low'],
                        normalized['settings']['threshold_high'])
                except Exception as error:
                    raise RecipeError(role_path + '.boundary: ' + str(error)) from error
            if 'finish' in spec:
                normalized['finish'] = _normalized_finish(
                    spec['finish'], role_path + '.finish'
                )
            roles[role] = normalized
        else:
            _fail("action must be 'PRESERVE' or 'SURFACE'", role_path + '.action')

    lines_value = document['lines']
    if not isinstance(lines_value, list):
        _fail('lines must be a list', 'document.lines')
    if len(lines_value) > MAX_LINES:
        _fail('lines must contain at most %d entries' % MAX_LINES, 'document.lines')

    lines = []
    seen_line_roles = set()
    for index, row in enumerate(lines_value):
        path = 'document.lines[%d]' % index
        _exact_fields(row, _LINE_FIELDS, path)
        role = _check_role_key(row['role'], path + '.role')
        if role in seen_line_roles:
            _fail('line roles must be unique', path + '.role')
        seen_line_roles.add(role)
        width = row['width']
        if type(width) not in (int, float) or not math.isfinite(width):
            _fail('width must be a finite number', path + '.width')
        width = float(width)
        if not 0.1 <= width <= 32.0:
            _fail('width must be in [0.1, 32]', path + '.width')
        lines.append({
            'role': role,
            'width': width,
            'color': _normalized_color(row['color'], path + '.color'),
        })

    return {
        'schema': SCHEMA,
        'recipe_id': recipe_id,
        'name': name,
        'revision': revision,
        'roles': roles,
        'lines': lines,
    }


def _cel3_settings():
    materials = _materials()
    raw = {'mode': 'eevee_cel3'}
    for key, value in materials.CEL3_DEFAULTS.items():
        raw[key] = list(value) if isinstance(value, list) else value
    return _normalized_settings(raw, 'new_document.settings')


def new_document(name='CEL recipe', *, include_finish=False):
    """Create the initial explicit composite recipe.

    ``include_finish`` is opt-in: a surface-only v2 document preserves any
    existing legal finish when applied, which is the safe migration default.
    When enabled, all SURFACE roles receive the exact nine static finish
    defaults (zero highlight/rim strength).
    """

    if type(include_finish) is not bool:
        _fail('include_finish must be a boolean')
    finish = _finish_nodes().defaults() if include_finish else None
    def surface_spec():
        spec = {'action': 'SURFACE', 'settings': _cel3_settings()}
        if finish is not None:
            spec['finish'] = {key: (list(value) if isinstance(value, list) else value)
                              for key, value in finish.items()}
        return spec

    roles = {
        'body': surface_spec(),
        'face': {'action': 'PRESERVE'},
        'head': {'action': 'PRESERVE'},
        'prop': surface_spec(),
        'background': surface_spec(),
    }
    document = {
        'schema': SCHEMA,
        'recipe_id': str(uuid.uuid4()),
        'name': name,
        'revision': 1,
        'roles': roles,
        'lines': [
            {'role': 'character', 'width': 2.2, 'color': [0.02, 0.02, 0.02, 1.0]},
            {'role': 'prop', 'width': 1.4, 'color': [0.02, 0.02, 0.02, 1.0]},
            {'role': 'background', 'width': 1.0, 'color': [0.02, 0.02, 0.02, 1.0]},
        ],
    }
    return normalize(document)


def export_document(document, absolute_json_path):
    """Create an absolute .json file with exclusive ``x`` semantics."""

    normalized = normalize(document)
    path = Path(absolute_json_path)
    if not path.is_absolute():
        _fail('export path must be absolute')
    if path.suffix.lower() != '.json':
        _fail('export path must end in .json')
    if not path.parent.is_dir():
        _fail('export parent directory must already exist')

    text = json.dumps(
        normalized,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + '\n'
    data = text.encode('utf-8')
    created = False
    stream = None
    try:
        stream = path.open('x', encoding='utf-8', newline='\n')
        created = True
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    except FileExistsError as error:
        raise RecipeError('export path already exists; export use is create-only') from error
    except Exception:
        if created:
            try:
                path.unlink()
            except Exception:
                pass
        raise
    finally:
        if stream is not None:
            stream.close()
    return {
        'status': 'exported',
        'schema': SCHEMA,
        'recipe_id': normalized['recipe_id'],
        'revision': normalized['revision'],
        'file': str(path),
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecipeError('duplicate JSON key: ' + repr(key)[:80])
        result[key] = value
    return result


def _reject_constant(value):
    raise RecipeError('nonfinite JSON number: ' + str(value))


def _check_finite_tree(value, depth=0):
    if depth > 64:
        _fail('JSON nesting exceeds 64 levels')
    if type(value) is float:
        if not math.isfinite(value):
            _fail('nonfinite JSON number')
        return
    if isinstance(value, list):
        for item in value:
            _check_finite_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                _fail('JSON object keys must be strings')
            _check_finite_tree(item, depth + 1)


def load_document(absolute_json_path):
    """Load a <=1 MiB absolute v2 recipe JSON, rejecting duplicates/nonfinite."""

    path = Path(absolute_json_path)
    if not path.is_absolute():
        _fail('load path must be absolute')
    if path.suffix.lower() != '.json':
        _fail('load path must end in .json')
    if not path.is_file():
        _fail('load path must be an existing JSON file')
    try:
        size = path.stat().st_size
    except OSError as error:
        raise RecipeError('cannot stat recipe file: ' + str(error)) from error
    if size > MAX_JSON_BYTES:
        _fail('recipe JSON exceeds 1 MiB')
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RecipeError('cannot read recipe file: ' + str(error)) from error
    if len(data) > MAX_JSON_BYTES:
        _fail('recipe JSON exceeds 1 MiB')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise RecipeError('recipe file is not UTF-8 JSON') from error
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except RecipeError:
        raise
    except json.JSONDecodeError as error:
        raise RecipeError('invalid recipe JSON: ' + str(error)) from error
    except (RecursionError, ValueError) as error:
        raise RecipeError('invalid recipe JSON: ' + str(error)) from error
    _check_finite_tree(parsed)
    return normalize(parsed)


def _validate_line_policy(line_policy):
    if type(line_policy) is not str or line_policy not in LINE_POLICIES:
        _fail("line_policy must be 'CREATE_FROM_BASIC' or 'PRESERVE'")
    return line_policy


def _surface_document(document):
    roles = {}
    for role, spec in document['roles'].items():
        if spec['action'] == 'PRESERVE':
            roles[role] = {'action': 'PRESERVE'}
        else:
            roles[role] = {'action': 'SURFACE', 'settings': spec['settings']}
    return {
        'schema': _cel_recipe().SCHEMA,
        'recipe_id': document['recipe_id'],
        'name': document['name'],
        'revision': document['revision'],
        'roles': roles,
        'lines': [],
    }


def _slot_material(row):
    return row['object'].material_slots[row['slot']].material


def _slot_call(scene, row, callback):
    return _cel_recipe()._with_active_index(
        scene, row['object'], row['slot'], callback
    )


def _finish_inspect_current(scene, row):
    finish_core = _finish_core()
    try:
        return _slot_call(
            scene, row, lambda fake: finish_core.inspect(fake)
        )
    except RecipeError:
        raise
    except Exception as error:
        raise RecipeError(
            'Existing finish is not valid: ' + str(error)
        ) from error


def _finish_summary(report):
    if report is None:
        return None
    return {key: report[key] for key in _FINISH_REPORT_FIELDS if key in report}


def _boundary_snapshot(material):
    if material is None:
        return None
    boundary = _band_boundary()
    if (material.get(_surface_slots()._PROFILE) != _surface_slots().PROFILE
            and boundary.KEY not in material):
        return None
    return boundary.snapshot(material)


def _boundary_inspect_current(scene, row):
    boundary = _band_boundary()
    material = _slot_material(row)
    if (row.get('boundary_settings') is None and material is not None
            and boundary.KEY not in material
            and material.get(_surface_slots()._PROFILE) == _surface_slots().PROFILE):
        declared = json.loads(material[_surface_slots()._DECLARED])
        if declared.get('mode') not in ('eevee_cel3', 'eevee_cel3_reflect'):
            # B.inspect admits only CEL3. An older owned native_cel surface
            # without this feature remains preservable, but a forged marker or
            # boundary residue must never be classified as harmless absence.
            _slot_call(scene, row, _surface_slots()._owned)
            if boundary.snapshot(material) is not None:
                _fail('Unexpected band boundary on a non-CEL3 surface')
            return dict(profile=boundary.PROFILE, status='absent', settings=boundary.defaults())
    report = _slot_call(scene, row, boundary.inspect)
    if report.get('status') not in ('valid', 'absent'):
        _fail('Existing band boundary is not valid: ' + str(report))
    return report


def _plan_boundary(scene, row, role_spec):
    boundary = _band_boundary()
    existing = _boundary_snapshot(_slot_material(row))
    row['boundary_report'] = None
    row['boundary_settings'] = role_spec.get('boundary')
    row['boundary_old_settings'] = None
    if row['action'] == 'create' and existing is not None:
        _fail('a surface replacement would discard an existing band boundary')
    if existing is not None:
        report = _boundary_inspect_current(scene, row)
        if report['status'] != 'valid':
            _fail('Existing band boundary disappeared during preflight')
        row['boundary_report'] = report
        row['boundary_old_settings'] = report['settings']
        # Surface updates precede boundary updates. The existing windows must
        # therefore remain legal at the new thresholds even when the document
        # requests narrower final windows. Use two explicit legal updates.
        try:
            boundary.validate_windows(report['settings'],
                role_spec['settings']['threshold_low'], role_spec['settings']['threshold_high'])
        except Exception as error:
            raise RecipeError('先缩小/归零现有边界宽度，再更新阈值：' + str(error)) from error
        # Omission preserves the feature, including when surface thresholds change.
        desired = role_spec.get('boundary', report['settings'])
        boundary.validate_windows(desired, role_spec['settings']['threshold_low'],
                                  role_spec['settings']['threshold_high'])
        row['boundary_action'] = ('preserve' if 'boundary' not in role_spec else
            'unchanged' if _finish_equal(report['settings'], desired) else 'update')
    else:
        row['boundary_action'] = 'create' if 'boundary' in role_spec else 'absent'


def _verify_boundary(scene, row):
    action = row['boundary_action']
    if action == 'not_applicable':
        return None
    report = _boundary_inspect_current(scene, row)
    if action == 'absent':
        if report['status'] != 'absent':
            _fail('An absent band boundary was unexpectedly created')
    else:
        expected = (row['boundary_settings'] if action in ('create', 'update', 'unchanged')
                    else row['boundary_old_settings'])
        if report['status'] != 'valid' or not _finish_equal(report['settings'], expected):
            _fail('Band boundary native readback differs from the document or preserved state')
    return report


def _capture_boundary_rollback(scene, row):
    if row['boundary_action'] == 'not_applicable':
        return
    existing = _boundary_snapshot(_slot_material(row))
    if row['boundary_action'] in ('create', 'absent'):
        if existing is not None:
            _fail('An unexpected band boundary appeared before apply')
    else:
        report = _boundary_inspect_current(scene, row)
        if (report['status'] != 'valid'
                or not _finish_equal(report['settings'], row['boundary_old_settings'])):
            _fail('Existing band boundary changed between preflight and apply')


def _boundary_apply_and_capture(scene, row):
    boundary = _band_boundary()
    # Capture the post-surface state; rollback restores it before surface rollback.
    row['boundary_token'] = _slot_call(scene, row, boundary.capture)
    callback = boundary.apply if row['boundary_action'] == 'create' else boundary.update
    _slot_call(scene, row, lambda fake: callback(fake, row['boundary_settings']))
    return _verify_boundary(scene, row)


def _rollback_boundary(scene, row):
    token = row.get('boundary_token')
    if token is not None:
        _slot_call(scene, row, lambda fake: _band_boundary().rollback(fake, token))


def _close_value(left, right):
    if isinstance(left, list):
        return (
            isinstance(right, list)
            and len(left) == len(right)
            and all(_close_value(a, b) for a, b in zip(left, right))
        )
    if type(left) in (int, float) and type(right) in (int, float):
        return abs(left - right) <= 4e-7 * max(1.0, abs(left), abs(right))
    return left == right


def _finish_equal(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return set(left) == set(right) and all(
        _close_value(left[key], right[key]) for key in left
    )


def _require_static_geometry_blinn(report, profile):
    finish_nodes = _finish_nodes()
    if report.get('normal_mode') != 'GEOMETRY':
        _fail('existing finish must use geometry normals; ARTISTIC requires explicit migration')
    source = report.get('highlight_source') or {}
    if source.get('mode') != 'BLINN':
        _fail('existing finish must use the BLINN highlight shape; HEAD_BAND requires explicit migration')
    animation = report.get('animation') or {}
    if animation.get('status') != 'valid' or animation.get('animated_keys'):
        _fail('existing finish Action/NLA/driver is outside the static v2 contract')
    if profile != finish_nodes.PROFILE:
        _fail('existing finish must use the static BLINN node profile')


def _preflight_finish_creation(scene, row, desired):
    finish_core = _finish_core()

    def callback(fake):
        target, material, tree = finish_core._context(fake, eevee=True)
        if finish_core.KEY in material:
            _fail('existing finish must be updated or explicitly removed first')
        finish_core._opaque(material)
        finish_core._output(tree)
        finish_core._single_hair_highlight(
            fake, target, material, desired['highlight_strength']
        )

    _slot_call(scene, row, callback)


def _plan_existing_finish(scene, row, desired):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()

    def callback(fake):
        report = finish_core.inspect(fake)
        target, material, _tree = finish_core._context(fake, eevee=True)
        group = material.get(finish_core.KEY)
        _require_static_geometry_blinn(
            report, group.get(finish_nodes.PROFILE_KEY)
        )
        finish_core._single_hair_highlight(
            fake, target, material, desired['highlight_strength']
        )
        return report

    try:
        return _slot_call(scene, row, callback)
    except RecipeError:
        raise
    except Exception as error:
        raise RecipeError(
            'Existing finish is not valid: ' + str(error)
        ) from error


def _plan_finish(scene, row, role_spec):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    material = _slot_material(row)
    row['finish_settings'] = None
    row['finish_old_group'] = None
    row['finish_old_settings'] = None
    row['finish_report'] = None

    if row['action'] == 'create' and material is not None and finish_core.KEY in material:
        _fail(
            'a surface replacement would discard an existing finish; '
            'remove or restore it explicitly first'
        )

    if 'finish' not in role_spec:
        if material is not None and finish_core.KEY in material:
            report = _finish_inspect_current(scene, row)
            group = material.get(finish_core.KEY)
            if group.get(finish_nodes.PROFILE_KEY) not in (
                finish_nodes.PROFILE, finish_nodes.HAIR_PROFILE
            ):
                _fail('existing finish has an unsupported node profile')
            row['finish_action'] = 'preserve'
            row['finish_old_group'] = group
            row['finish_old_settings'] = report['settings']
            row['finish_report'] = report
        else:
            row['finish_action'] = 'absent'
        return

    desired = role_spec['finish']
    row['finish_settings'] = desired
    if material is not None and finish_core.KEY in material:
        report = _plan_existing_finish(scene, row, desired)
        group = material.get(finish_core.KEY)
        row['finish_old_group'] = group
        row['finish_old_settings'] = report['settings']
        row['finish_report'] = report
        row['finish_action'] = (
            'unchanged' if _finish_equal(report['settings'], desired) else 'update'
        )
        return

    row['finish_action'] = 'create'
    if row['action'] != 'create':
        _preflight_finish_creation(scene, row, desired)


def _line_role_targets(scene, document, line_bindings):
    if type(line_bindings) is not dict:
        _fail('Line bindings must map roles to explicit Object lists')
    roles = [row['role'] for row in document['lines']]
    extras = set(line_bindings) - set(roles)
    if extras:
        _fail('Line bindings contain undeclared roles: ' + ', '.join(sorted(extras)))
    if not roles and line_bindings:
        _fail('Line bindings exist without line roles')
    selected = [role for role in roles if role in line_bindings]
    if not selected:
        return {}
    cel_lines = _cel_lines()
    return cel_lines._validate_bindings(
        scene,
        {role: line_bindings[role] for role in selected},
        selected,
    )


def _line_preflight(scene, document, line_bindings, line_policy):
    if line_policy == 'CREATE_FROM_BASIC':
        if not document['lines']:
            if line_bindings:
                _fail('Line bindings exist without line roles')
            return None, {}
        plan = _cel_lines().preflight(scene, document['lines'], line_bindings)
        return plan, plan['binding_witness']
    targets = _line_role_targets(scene, document, line_bindings)
    return None, targets


def preflight(
    context,
    document,
    bindings,
    line_bindings,
    *,
    line_policy='CREATE_FROM_BASIC',
):
    """Validate surface, boundary, finish and Pencil before mutation."""

    line_policy = _validate_line_policy(line_policy)
    doc = normalize(document)
    try:
        surface_plan = _cel_recipe().preflight(
            context, _surface_document(doc), bindings, {}
        )
    except RecipeError:
        raise
    except Exception as error:
        raise RecipeError('Surface preflight failed: ' + str(error)) from error
    scene = context.scene
    records = surface_plan['records']
    for row in records:
        role_spec = doc['roles'][row['role']]
        if role_spec['action'] == 'SURFACE':
            _plan_boundary(scene, row, role_spec)
            _plan_finish(scene, row, role_spec)
        else:
            row['boundary_action'] = 'not_applicable'
            row['boundary_settings'] = None
            row['boundary_report'] = None
            row['finish_action'] = 'not_applicable'
            row['finish_settings'] = None
            row['finish_old_group'] = None
            row['finish_old_settings'] = None
            row['finish_report'] = None
    try:
        line_plan, line_targets = _line_preflight(
            scene, doc, line_bindings, line_policy
        )
    except RecipeError:
        raise
    except Exception as error:
        raise RecipeError('Line preflight failed: ' + str(error)) from error
    plan = {
        'schema': PLAN_SCHEMA,
        'document': doc,
        'records': records,
        'unbound_roles': surface_plan['unbound_roles'],
        'line_policy': line_policy,
        'line_plan': line_plan,
        'line_targets': line_targets,
        'line_token': None,
        'line_inspection': None,
    }
    plan['unbound_line_roles'] = _unbound_line_roles(plan)
    return plan


def _plain_socket_value(value):
    return _materials()._plain(value)


def _capture_surface_rollback(scene, row):
    slot = row['object'].material_slots[row['slot']]
    if slot.link != row['link'] or slot.material != row['material']:
        _fail('Material slot changed between preflight and apply', row['object'].name)
    if row['action'] != 'update':
        return
    wrapper = _cel_recipe()._find_wrapper(row['material'])
    if wrapper is None:
        _fail('Owned surface wrapper is missing before update', row['object'].name)
    row['wrapper'] = wrapper
    row['old_values'] = {
        socket.name: _plain_socket_value(socket.default_value)
        for socket in wrapper.inputs
        if hasattr(socket, 'default_value')
    }
    row['old_metadata'] = {
        key: row['material'][key]
        for key in (
            _surface_slots()._BASELINE,
            _surface_slots()._DECLARED,
            _surface_slots()._SIGNATURE,
        )
    }


def _capture_finish_rollback(scene, row):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    material = _slot_material(row)
    action = row['finish_action']
    if action == 'create':
        if material is not None and finish_core.KEY in material:
            _fail('An unexpected finish appeared before apply', row['object'].name)
        return
    if action == 'absent':
        if material is not None and finish_core.KEY in material:
            _fail('An unexpected finish appeared before apply', row['object'].name)
        return
    if action == 'not_applicable':
        return
    if material is None:
        _fail('Finish target material disappeared before apply', row['object'].name)
    group = material.get(finish_core.KEY)
    if group is None or group != row['finish_old_group']:
        _fail('Existing finish identity changed between preflight and apply')
    report = _finish_inspect_current(scene, row)
    if not _finish_equal(report['settings'], row['finish_old_settings']):
        _fail('Existing finish parameters changed between preflight and apply')
    profile = group.get(finish_nodes.PROFILE_KEY)
    if action in ('update', 'unchanged'):
        _require_static_geometry_blinn(report, profile)


def _surface_apply_and_capture(scene, row):
    surface = _surface_slots()
    try:
        result = _slot_call(
            scene, row, lambda fake: surface.apply(fake, row['settings'])
        )
    finally:
        # A dependency wrapper can fail after its native creation succeeded.
        # Retain the exact new owned IDs before the transaction restores the slot.
        material = _slot_material(row)
        if (material is not None and material != row['material']
                and material.get(surface._PROFILE) == surface.PROFILE):
            row['created_material'] = material
            wrapper = _cel_recipe()._find_wrapper(material)
            row['created_group'] = wrapper.node_tree if wrapper is not None else None
    if material is None:
        _fail('Surface apply did not create a material', row['object'].name)
    row['created_material'] = material
    wrapper = _cel_recipe()._find_wrapper(material)
    row['created_group'] = wrapper.node_tree if wrapper is not None else None
    return result


def _surface_update_and_capture(scene, row):
    surface = _surface_slots()
    return _slot_call(
        scene, row, lambda fake: surface.update(fake, row['settings'])
    )


def _finish_apply_and_capture(scene, row):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    _slot_call(
        scene, row, lambda fake: finish_core.apply(fake, row['finish_settings'])
    )
    material = _slot_material(row)
    group = material.get(finish_core.KEY) if material is not None else None
    if group is None:
        _fail('Finish apply did not install an owned Group', row['object'].name)
    row['finish_created_group'] = group
    report = _finish_inspect_current(scene, row)
    _require_static_geometry_blinn(
        report, group.get(finish_nodes.PROFILE_KEY)
    )
    if not _finish_equal(report['settings'], row['finish_settings']):
        _fail('Finish apply native readback differs from the document')
    return report


def _finish_update_and_capture(scene, row):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    _slot_call(
        scene, row, lambda fake: finish_core.update(fake, row['finish_settings'])
    )
    material = _slot_material(row)
    group = material.get(finish_core.KEY) if material is not None else None
    if group is None or group != row['finish_old_group']:
        _fail('Finish update changed the owned Group identity')
    row['finish_updated_group'] = group
    report = _finish_inspect_current(scene, row)
    _require_static_geometry_blinn(
        report, group.get(finish_nodes.PROFILE_KEY)
    )
    if not _finish_equal(report['settings'], row['finish_settings']):
        _fail('Finish update native readback differs from the document')
    return report


def _finish_verify_preserved(scene, row):
    finish_core = _finish_core()
    material = _slot_material(row)
    if material is None:
        _fail('Finish material disappeared')
    group = material.get(finish_core.KEY)
    if group is None or group != row['finish_old_group']:
        _fail('A preserved finish changed identity')
    report = _finish_inspect_current(scene, row)
    if not _finish_equal(report['settings'], row['finish_old_settings']):
        _fail('A preserved finish changed its static parameters')
    return report


def _line_role_rows(plan):
    line_plan = plan['line_plan']
    rows = []
    if line_plan is not None:
        for spec in line_plan['lines']:
            role = spec['role']
            witnesses = line_plan['binding_witness'].get(role, [])
            rows.append({
                'role': role,
                'width': spec['width'],
                'color': list(spec['color']),
                'targets': [
                    {
                        'name': row['name'],
                        'library': row['library'],
                        'type': row['type'],
                    }
                    for row in witnesses
                ],
            })
        return rows
    for spec in plan['document']['lines']:
        role = spec['role']
        witnesses = plan['line_targets'].get(role, [])
        rows.append({
            'role': role,
            'width': spec['width'],
            'color': list(spec['color']),
            'targets': [
                {
                    'name': row['name'],
                    'library': row['library'],
                    'type': row['type'],
                }
                for row in witnesses
            ],
        })
    return rows


def _unbound_line_roles(plan):
    if plan['line_policy'] == 'CREATE_FROM_BASIC':
        return []
    roles = [row['role'] for row in plan['document']['lines']]
    return [role for role in roles if role not in plan['line_targets']]


def _line_inspect_report(plan):
    policy = plan['line_policy']
    if policy == 'CREATE_FROM_BASIC':
        return {
            'policy': policy,
            'status': 'ready',
            'applied': False,
            'roles': _line_role_rows(plan),
            'unbound_line_roles': [],
            'unapplied_line_roles': [],
        }
    roles = [row['role'] for row in plan['document']['lines']]
    return {
        'policy': policy,
        'status': 'ready',
        'applied': False,
        'roles': _line_role_rows(plan),
        'unbound_line_roles': _unbound_line_roles(plan),
        'unapplied_line_roles': list(roles),
        'note': (
            'Pencil was not migrated. Document line roles were not applied; '
            'bound targets are reported only as existing explicit objects.'
        ),
    }


def inspect(
    context,
    document,
    bindings,
    line_bindings,
    *,
    line_policy='CREATE_FROM_BASIC',
):
    """Return a read-only, JSONable readiness report; no native mutation."""

    plan = preflight(
        context,
        document,
        bindings,
        line_bindings,
        line_policy=line_policy,
    )
    slots = []
    for row in plan['records']:
        slot_report = {
            'role': row['role'],
            'object': row['object'].name,
            'slot': row['slot'],
            'action': row['action'],
            'finish': {
                'action': row['finish_action'],
                'state': _finish_summary(row.get('finish_report')),
            },
        }
        # Existing family receipts embed this complete readiness report. Keep
        # the legacy shape when neither document nor native state has a feature.
        if row['boundary_action'] not in ('absent', 'not_applicable'):
            slot_report['boundary'] = {
                'action': row['boundary_action'],
                'settings': row['boundary_settings'],
                'state': row.get('boundary_report'),
            }
        slots.append(slot_report)
    return {
        'schema': INSPECT_SCHEMA,
        'status': 'ready',
        'recipe_id': plan['document']['recipe_id'],
        'revision': plan['document']['revision'],
        'line_policy': plan['line_policy'],
        'unbound_roles': list(plan['unbound_roles']),
        'unbound_line_roles': _unbound_line_roles(plan),
        'slots': slots,
        'lines': _line_inspect_report(plan),
    }


def _line_apply_and_capture(scene, plan):
    policy = plan['line_policy']
    if policy == 'CREATE_FROM_BASIC':
        line_plan = plan['line_plan']
        if line_plan is None:
            return {
                'policy': policy,
                'status': 'empty',
                'applied': False,
                'roles': [],
                'unbound_line_roles': [],
                'unapplied_line_roles': [],
            }
        cel_lines = _cel_lines()
        token = cel_lines.apply(scene, line_plan)
        plan['line_token'] = token
        inspection = cel_lines.inspect(scene, token)
        plan['line_inspection'] = inspection
        if inspection.get('status') not in ('applied', 'valid'):
            _fail('Line recipe verification failed')
        return {
            'policy': policy,
            'status': inspection.get('status'),
            'applied': True,
            'state_sha256': inspection.get('state_sha256'),
            'document_sha256': inspection.get('document_sha256'),
            'recipe_sha256': inspection.get('recipe_sha256'),
            'roles': _line_role_rows(plan),
            'unbound_line_roles': [],
            'unapplied_line_roles': [],
        }
    roles = [row['role'] for row in plan['document']['lines']]
    return {
        'policy': policy,
        'status': 'preserved',
        'applied': False,
        'roles': _line_role_rows(plan),
        'unbound_line_roles': _unbound_line_roles(plan),
        'unapplied_line_roles': list(roles),
        'note': (
            'Pencil was preserved and not migrated. Document line roles '
            'were not applied; bound targets are explicit but unapplied.'
        ),
    }


def _rollback_finish(scene, row):
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    action = row['finish_action']
    if action not in ('create', 'update'):
        return
    material = _slot_material(row)
    if material is None:
        raise RecipeError('Finish rollback lost its material')
    group = material.get(finish_core.KEY)
    if action == 'create':
        if group is None:
            return
        expected = row.get('finish_created_group')
        if expected is not None and group != expected:
            raise RecipeError('Rollback refused an unowned finish Group')
        if group.get(finish_nodes.PROFILE_KEY) != finish_nodes.PROFILE:
            raise RecipeError('Rollback refused a non-BLINN finish Group')
        _slot_call(scene, row, lambda fake: finish_core.remove(fake))
        if material.get(finish_core.KEY) is not None:
            raise RecipeError('Finish removal did not restore the original output')
        return

    expected = row.get('finish_updated_group') or row.get('finish_old_group')
    if group is None or group != expected:
        raise RecipeError('Rollback refused a changed finish Group')
    _slot_call(
        scene, row, lambda fake: finish_core.update(fake, row['finish_old_settings'])
    )
    report = _finish_inspect_current(scene, row)
    if not _finish_equal(report['settings'], row['finish_old_settings']):
        raise RecipeError('Finish rollback did not restore earlier native values')


def _final_verify(scene, plan):
    surface = _surface_slots()
    finish_core = _finish_core()
    finish_nodes = _finish_nodes()
    slots = []
    for row in plan['records']:
        slot = row['object'].material_slots[row['slot']]
        if row['action'] == 'preserve':
            if slot.material != row['material']:
                raise RecipeError('PRESERVE material changed during recipe application')
            surface_state = None
        else:
            surface_state = _slot_call(scene, row, surface.inspect)
            if surface_state.get('status') != 'valid':
                raise RecipeError('; '.join(surface_state.get('issues', [])))

        action = row['finish_action']
        if action in ('create', 'update'):
            report = _finish_inspect_current(scene, row)
            _require_static_geometry_blinn(
                report,
                slot.material.get(finish_core.KEY).get(finish_nodes.PROFILE_KEY),
            )
            if not _finish_equal(report['settings'], row['finish_settings']):
                raise RecipeError('Finish final readback differs from the document')
        elif action in ('preserve', 'unchanged'):
            report = _finish_verify_preserved(scene, row)
        elif action == 'absent':
            if slot.material is not None and finish_core.KEY in slot.material:
                raise RecipeError('An absent finish was unexpectedly created')
            report = None
        else:
            report = None

        boundary_report = _verify_boundary(scene, row)
        slot_report = {
            'role': row['role'],
            'object': row['object'].name,
            'slot': row['slot'],
            'action': row['action'],
            'material': _cel_recipe()._material_label(slot.material),
            'surface': surface_state,
            'finish': {
                'action': action,
                'state': _finish_summary(report),
            },
        }
        if row['boundary_action'] not in ('absent', 'not_applicable'):
            slot_report['boundary'] = {
                'action': row['boundary_action'],
                'settings': row['boundary_settings'],
                'state': boundary_report,
            }
        slots.append(slot_report)
    return slots


def _apply(
    context,
    document,
    bindings,
    line_bindings,
    *,
    line_policy='CREATE_FROM_BASIC',
    _transaction=False,
    _capture=None,
):
    """Apply surface, boundary, finish, Pencil; rollback in reverse order."""

    plan = preflight(
        context,
        document,
        bindings,
        line_bindings,
        line_policy=line_policy,
    )
    scene = context.scene
    surface_changed = []
    boundary_changed = []
    finish_changed = []
    transaction_token = None
    try:
        # All remaining read-only snapshots precede the first mutation.
        for row in plan['records']:
            _capture_surface_rollback(scene, row)
            _capture_boundary_rollback(scene, row)
            _capture_finish_rollback(scene, row)

        for row in plan['records']:
            action = row['action']
            if action == 'create':
                surface_changed.append(row)
                _surface_apply_and_capture(scene, row)
            elif action == 'update':
                surface_changed.append(row)
                _surface_update_and_capture(scene, row)

        for row in plan['records']:
            if row['boundary_action'] in ('create', 'update'):
                # Record intent before API invocation, including partial failures.
                boundary_changed.append(row)
                row['boundary_report'] = _boundary_apply_and_capture(scene, row)
            elif row['boundary_action'] in ('preserve', 'unchanged'):
                row['boundary_report'] = _verify_boundary(scene, row)

        for row in plan['records']:
            action = row['finish_action']
            if action == 'create':
                finish_changed.append(row)
                row['finish_report'] = _finish_apply_and_capture(scene, row)
            elif action == 'update':
                finish_changed.append(row)
                row['finish_report'] = _finish_update_and_capture(scene, row)
            elif action in ('preserve', 'unchanged'):
                row['finish_report'] = _finish_verify_preserved(scene, row)

        line_receipt = _line_apply_and_capture(scene, plan)
        slots = _final_verify(scene, plan)
        result = {
            'schema': RECEIPT_SCHEMA,
            'status': 'applied',
            'recipe_id': plan['document']['recipe_id'],
            'revision': plan['document']['revision'],
            'line_policy': plan['line_policy'],
            'slots': slots,
            'lines': line_receipt,
            'unbound_roles': list(plan['unbound_roles']),
            'unbound_line_roles': _unbound_line_roles(plan),
        }
        if _transaction:
            token = dict(status='applied', scene=scene, plan=plan,
                surface_changed=surface_changed, finish_changed=finish_changed,
                boundary_changed=boundary_changed,
                after_slots=slots,
                after_ids=[(row['object'], row['slot'], row['object'].material_slots[row['slot']].material)
                    for row in plan['records']])
            transaction_token=token
            if _capture is not None:_capture(result,token)
            return result, token
        return result
    except BaseException as error:
        issues = []
        token = plan.get('line_token')
        if token is not None:
            try:
                _cel_lines().rollback(scene, token)
            except Exception as rollback_error:
                issues.append('Pencil: ' + str(rollback_error))
        for row in reversed(finish_changed):
            try:
                _rollback_finish(scene, row)
            except Exception as rollback_error:
                issues.append(
                    row['object'].name + ' finish: ' + str(rollback_error)
                )
        for row in reversed(boundary_changed):
            try:
                _rollback_boundary(scene, row)
            except Exception as rollback_error:
                issues.append(row['object'].name + ' boundary: ' + str(rollback_error))
        for row in reversed(surface_changed):
            try:
                _cel_recipe()._restore_record(scene, row)
            except Exception as rollback_error:
                issues.append(
                    row['object'].name + ' surface: ' + str(rollback_error)
                )
        if issues:
            if transaction_token is not None:transaction_token['status']='rollback_failed'
            raise RecipeError(
                'Recipe failed; rollback incomplete: ' + '; '.join(issues)
            ) from error
        if transaction_token is not None:transaction_token['status']='rolled_back'
        raise


def apply(context, document, bindings, line_bindings, *, line_policy='CREATE_FROM_BASIC'):
    """Existing v2 entry and JSON receipt remain unchanged."""
    return _apply(context, document, bindings, line_bindings, line_policy=line_policy)


def apply_transaction(context, document, bindings, line_bindings, *, line_policy='PRESERVE', capture=None):
    """Internal composition entry; runtime token never becomes a portable recipe."""
    return _apply(context, document, bindings, line_bindings,
        line_policy=line_policy, _transaction=True, _capture=capture)


def validate_transaction(context, token):
    """Read-only admission before a multi-domain rollback starts."""
    if token.get('status') != 'applied' or context.scene != token['scene']:
        raise RecipeError('CEL transaction is unavailable for this scene')
    scene=context.scene;plan=token['plan']
    for obj, index, material in token['after_ids']:
        if scene.objects.get(obj.name) != obj or obj.material_slots[index].material != material:
            raise RecipeError('CEL target identity changed; preserve later edits')
    current=_final_verify(scene,plan)
    encode=lambda value: json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False)
    if encode(current)!=encode(token['after_slots']):
        raise RecipeError('CEL result changed; preserve later edits')
    return dict(status='valid')


def rollback_transaction(context, token):
    """Revert this transaction only while its exact owned result is unchanged."""
    validate_transaction(context,token)
    scene=context.scene;plan=token['plan']
    if plan.get('line_token') is not None:
        _cel_lines().rollback(scene,plan['line_token'])
    for row in reversed(token['finish_changed']):
        _rollback_finish(scene,row)
    for row in reversed(token['boundary_changed']):
        _rollback_boundary(scene,row)
    for row in reversed(token['surface_changed']):
        _cel_recipe()._restore_record(scene,row)
    token['status']='rolled_back'
    return dict(status='rolled_back',recipe_id=plan['document']['recipe_id'])


__all__ = [
    'SCHEMA', 'PLAN_SCHEMA', 'RECEIPT_SCHEMA', 'INSPECT_SCHEMA',
    'LINE_POLICIES', 'RecipeError',
    'normalize', 'new_document', 'export_document', 'load_document',
    'preflight', 'inspect', 'apply', 'apply_transaction', 'validate_transaction', 'rollback_transaction',
]
