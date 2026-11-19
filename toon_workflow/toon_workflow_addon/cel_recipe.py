# SPDX-License-Identifier: GPL-3.0-or-later
"""Static CEL recipe: explicit surface slots, PRESERVE roles and Pencil lines.

This is a leaf data/compiler module for the Toon workflow add-on.  It does not
register operators, draw UI, import a Pencil document itself, invoke the legacy
run/edit scheduler, write render settings, or modify source files.  The sibling
``cel_lines`` module owns the Pencil style mutation API; this module only
orchestrates it after all surface work is complete.

The surface layer deliberately reuses the existing ``surface_slots`` ownership
guards and ``materials`` graph builder.  A new material is created only for an
empty or demonstrably simple, opaque, static, texture-free native slot.  Any
unmanaged material with textures, transparency, native animation or unusual
nodes must be declared PRESERVE.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace


SCHEMA = 'toon-cel-recipe.v1'
PLAN_SCHEMA = 'toon-cel-recipe.plan.v1'
RECEIPT_SCHEMA = 'toon-cel-recipe.receipt.v1'
INSPECT_SCHEMA = 'toon-cel-recipe.inspect.v1'

MAX_JSON_BYTES = 1024 * 1024
MAX_ROLES = 32
MAX_LINES = 32
MAX_NAME_CHARS = 128

_ROLE_RE = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
_TOP_FIELDS = ('schema', 'recipe_id', 'name', 'revision', 'roles', 'lines')
_ROLE_FIELDS = {'action'}
_SURFACE_ROLE_FIELDS = {'action', 'settings'}
_LINE_FIELDS = ('role', 'width', 'color')
_BINDING_FIELDS = ('role', 'object', 'slot', 'link', 'material')
_PROTECTED_ROLE_TOKENS = {'face', 'head', 'sdf', 'normal', 'normals'}
_SIMPLE_BSDF_TYPES = {
    'ShaderNodeBsdfPrincipled',
    'ShaderNodeBsdfDiffuse',
    'ShaderNodeBsdfToon',
    'ShaderNodeBsdfGlossy',
    'ShaderNodeEmission',
}
_SIMPLE_NODE_TYPES = _SIMPLE_BSDF_TYPES | {'ShaderNodeOutputMaterial'}
_TRANSPARENCY_SCALARS = (
    'transmission', 'refraction', 'subsurface', 'coat', 'sheen',
)


class RecipeError(ValueError):
    """Invalid recipe data, binding, native source or transactional failure."""



def _fail(message, path=None):
    if path:
        raise RecipeError(str(path) + ': ' + str(message))
    raise RecipeError(str(message))


def _materials():
    from . import materials

    return materials


def _surface_slots():
    from . import surface_slots

    return surface_slots


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
    """Conventional protected roles can never become SURFACE targets.

    Actual material markers are checked again per slot.  This name guard is
    intentionally conservative for face/head/SDF/normal roles so a recipe
    cannot silently turn a known protected target into a replacement surface.
    """

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


def _normalized_color(value, path):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail('color must be a four-channel finite list', path)
    color = []
    for index, channel in enumerate(value):
        if type(channel) not in (int, float) or not math.isfinite(channel):
            _fail('color channels must be finite numbers', path)
        if not 0.0 <= float(channel) <= 1.0:
            _fail('color channels must be in [0,1]', path)
        color.append(float(channel))
    if color[3] != 1.0:
        _fail('line color alpha must be exactly 1', path)
    return color


def normalize(document):
    """Return a fresh, closed, JSON-safe normalized recipe document."""

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
            _exact_fields(spec, _SURFACE_ROLE_FIELDS, role_path)
            if _protected_role_name(role):
                _fail('protected face/head/SDF/normal role must be PRESERVE', role_path)
            roles[role] = {
                'action': 'SURFACE',
                'settings': _normalized_settings(spec['settings'], role_path + '.settings'),
            }
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
        color = _normalized_color(row['color'], path + '.color')
        lines.append({'role': role, 'width': width, 'color': color})

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


def new_document(name='CEL recipe'):
    """Create the initial explicit CEL recipe.

    body/prop/background use the existing CEL3 three-band candidate.  face and
    head are protected PRESERVE roles.  The default line roles are independent
    from surface role names; ``character`` is intentionally a line-only role.
    Widths and the near-black color are initial targets, not art-locked values.
    """

    document = {
        'schema': SCHEMA,
        'recipe_id': str(uuid.uuid4()),
        'name': name,
        'revision': 1,
        'roles': {
            'body': {'action': 'SURFACE', 'settings': _cel3_settings()},
            'face': {'action': 'PRESERVE'},
            'head': {'action': 'PRESERVE'},
            'prop': {'action': 'SURFACE', 'settings': _cel3_settings()},
            'background': {'action': 'SURFACE', 'settings': _cel3_settings()},
        },
        'lines': [
            {'role': 'character', 'width': 2.2, 'color': [0.02, 0.02, 0.02, 1.0]},
            {'role': 'prop', 'width': 1.4, 'color': [0.02, 0.02, 0.02, 1.0]},
            {'role': 'background', 'width': 1.0, 'color': [0.02, 0.02, 0.02, 1.0]},
        ],
    }
    return normalize(document)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _material_label(material):
    if material is None:
        return None
    name = getattr(material, 'name', None)
    if type(name) is not str:
        name = '<invalid material>'
    library = getattr(material, 'library', None)
    filepath = getattr(library, 'filepath', None)
    if filepath:
        return name + ' [library: ' + str(filepath) + ']'
    return name


def _find_wrapper(material):
    materials = _materials()
    tree = getattr(material, 'node_tree', None)
    if tree is None:
        return None
    try:
        for node in tree.nodes:
            if node.get(materials.ROLE) == 'core_instance':
                return node
    except Exception:
        return None
    return None


def _with_active_index(scene, obj, index, callback):
    original = obj.active_material_index
    if type(original) is not int:
        _fail('object.active_material_index is not an integer: ' + obj.name)
    try:
        obj.active_material_index = index
        fake_context = SimpleNamespace(scene=scene, active_object=obj, mode='OBJECT')
        return callback(fake_context)
    finally:
        try:
            obj.active_material_index = original
        finally:
            if obj.active_material_index != original:
                raise RecipeError(
                    'failed to restore active_material_index exactly on ' + obj.name
                )


def _owned_at(scene, obj, index):
    surface = _surface_slots()
    result = _with_active_index(
        scene,
        obj,
        index,
        lambda fake: surface._owned(fake, eevee=True),
    )
    if not isinstance(result, tuple) or len(result) != 7:
        raise RecipeError('surface_slots._owned returned an unexpected result')
    target, owned_index, slot, material, owner, baseline, declared = result
    try:
        baseline_settings = baseline['materials'][0]['settings']
    except Exception as error:
        raise RecipeError('surface_slots._owned returned an invalid baseline') from error
    return {
        'target': target,
        'index': owned_index,
        'slot': slot,
        'material': material,
        'owner': owner,
        'baseline': baseline,
        'baseline_settings': baseline_settings,
        'declared': declared,
    }


def _settings_equal(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get('mode') != right.get('mode'):
        return False
    materials = _materials()
    try:
        fields = materials._fields(left['mode'])
        return all(materials._close(left.get(field), right.get(field)) for field in fields[1:])
    except Exception:
        return False


def _socket_default_is_one(value):
    return type(value) in (int, float) and float(value) == 1.0


def _opaque_material_options(material):
    blend_method = getattr(material, 'blend_method', None)
    if not hasattr(material, 'surface_render_method') and blend_method is not None and blend_method != 'OPAQUE':
        return False, 'material blend_method is not OPAQUE'
    if getattr(material, 'surface_render_method', None) == 'BLENDED':
        return False, 'material surface_render_method is BLENDED'
    for name in (
        'use_screen_refraction',
        'use_raytrace_refraction',
    ):
        if getattr(material, name, False):
            return False, 'material transparency/refraction option is enabled: ' + name
    return True, 'opaque options'


def _material_is_simple_opaque_static(material):
    """Conservative gate for replacing an unmanaged native material.

    Only empty/constant Principled/Diffuse/Toon/Glossy/Emission graphs whose
    only possible link is a BSDF surface into one Material Output qualify.
    Textures, Mix nodes, transparency, normal/bump graphs, node groups,
    animation and library/override data are rejected and must be PRESERVE.
    """

    bpy = _bpy()
    if material is None:
        return True, 'empty slot'
    if not isinstance(material, bpy.types.Material):
        return False, 'not a Blender Material'
    if (
        getattr(material, 'library', None) is not None
        or getattr(material, 'override_library', None) is not None
        or not getattr(material, 'is_editable', True)
    ):
        return False, 'material is library/linked/override or not editable'
    if getattr(material, 'animation_data', None):
        return False, 'material has native animation/drivers'

    ok, reason = _opaque_material_options(material)
    if not ok:
        return False, reason

    use_nodes = bool(getattr(material, 'use_nodes', False))
    if not use_nodes:
        diffuse = getattr(material, 'diffuse_color', None)
        if diffuse is None or len(diffuse) < 4:
            return False, 'material diffuse color is unavailable'
        if float(diffuse[3]) != 1.0:
            return False, 'material diffuse alpha is not 1'
        return True, 'non-node native material'

    tree = getattr(material, 'node_tree', None)
    if tree is None:
        return False, 'material use_nodes is enabled but node_tree is missing'
    if (
        getattr(tree, 'library', None) is not None
        or getattr(tree, 'override_library', None) is not None
        or not getattr(tree, 'is_editable', True)
    ):
        return False, 'material node tree is library/override or not editable'
    if getattr(tree, 'animation_data', None):
        return False, 'material node tree has native animation/drivers'
    if getattr(tree, 'users', 1) > 1:
        return False, 'material node tree has multiple consumers'

    nodes = list(tree.nodes)
    if not nodes:
        return True, 'empty material node tree'
    for node in nodes:
        if node.bl_idname not in _SIMPLE_NODE_TYPES:
            return False, 'unmanaged node type is not simple: ' + str(node.bl_idname)
    outputs = [node for node in nodes if node.bl_idname == 'ShaderNodeOutputMaterial']
    if len(outputs) > 1:
        return False, 'multiple Material Output nodes'

    surface_links = []
    for link in tree.links:
        if (
            link.from_node.bl_idname in _SIMPLE_BSDF_TYPES
            and link.to_node.bl_idname == 'ShaderNodeOutputMaterial'
            and getattr(link.to_socket, 'name', None) == 'Surface'
        ):
            surface_links.append(link)
        else:
            return False, 'unmanaged node link is not a simple BSDF surface link'
    if len(surface_links) > 1:
        return False, 'multiple links into Material Output Surface'

    for node in nodes:
        for socket in getattr(node, 'inputs', ()):
            if getattr(socket, 'is_linked', False):
                if node.bl_idname == 'ShaderNodeOutputMaterial' and socket.name == 'Surface':
                    continue
                return False, 'linked input is not a simple surface link'
            value = getattr(socket, 'default_value', None)
            if type(value) not in (int, float):
                continue
            name = str(getattr(socket, 'name', '')).lower()
            numeric = float(value)
            if 'alpha' in name and not _socket_default_is_one(value):
                return False, 'material alpha input is not 1'
            if name in {'transmission', 'transmission weight'} and numeric > 0.0:
                return False, 'material has non-opaque ' + name
    return True, 'simple opaque static node material'


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
    """Load a <=1 MiB absolute recipe JSON, rejecting duplicates/nonfinite/extra."""

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


def preflight(context, document, bindings, line_bindings):
    """All input and ownership checks precede mutation; no implicit targets."""
    from . import cel_lines
    bpy = _bpy()
    surface = _surface_slots()
    doc = normalize(document)
    scene = context.scene
    if (not isinstance(scene, bpy.types.Scene) or scene.library or scene.override_library
            or not scene.is_editable or getattr(context, 'mode', 'OBJECT') != 'OBJECT'):
        raise RecipeError('Use a local editable Scene in Object Mode')
    if scene.render.engine != 'BLENDER_EEVEE':
        raise RecipeError('Static CEL recipe requires EEVEE')
    if type(bindings) is not list or not 1 <= len(bindings) <= 4096:
        raise RecipeError('Provide 1..4096 explicit material slot bindings')
    if type(line_bindings) is not dict:
        raise RecipeError('Line bindings must map roles to explicit Object lists')
    records = []
    seen = set()
    for index, row in enumerate(bindings):
        _exact_fields(row, _BINDING_FIELDS, 'bindings[%d]' % index)
        role, obj, slot_index = row['role'], row['object'], row['slot']
        if type(role) is not str or role not in doc['roles']:
            raise RecipeError('Material binding role is undeclared')
        if (not isinstance(obj, bpy.types.Object) or scene.objects.get(obj.name) != obj
                or type(slot_index) is not int or not 0 <= slot_index < len(obj.material_slots)):
            raise RecipeError('Choose an existing Scene Object and integer material slot')
        if row['material'] is not None and not isinstance(row['material'], bpy.types.Material):
            raise RecipeError('Expected material must be a Material or None')
        if (obj.as_pointer(), slot_index) in seen:
            raise RecipeError('Duplicate object/material slot binding')
        seen.add((obj.as_pointer(), slot_index))
        _with_active_index(scene, obj, slot_index, lambda fake: surface._context(fake, eevee=True))
        slot = obj.material_slots[slot_index]
        if slot.link != row['link'] or slot.material != row['material']:
            raise RecipeError('Expected material or slot link changed')
        record = dict(row, action='preserve', settings=None, owned=None)
        if doc['roles'][role]['action'] == 'SURFACE':
            desired = doc['roles'][role]['settings']
            material = slot.material
            surface._protected(material)
            if material is not None and surface._PROFILE in material:
                owned = _owned_at(scene, obj, slot_index)
                if desired['mode'] != owned['declared']['mode']:
                    raise RecipeError('Owned surface mode changes require explicit restore')
                record['owned'] = owned
                record['action'] = 'unchanged' if _settings_equal(desired, owned['declared']) else 'update'
            else:
                good, reason = _material_is_simple_opaque_static(material)
                if not good:
                    raise RecipeError('Complex native material requires PRESERVE: ' + reason)
                record['action'] = 'create'
            record['settings'] = desired
        records.append(record)
    if not doc['lines'] and line_bindings:
        raise RecipeError('Line bindings exist without line roles')
    line_plan = cel_lines.preflight(scene, doc['lines'], line_bindings) if doc['lines'] else None
    from .line_material_colors import protect_cel_sources
    protect_cel_sources(scene, records)
    return dict(document=doc, records=records, line_plan=line_plan,
        unbound_roles=sorted(set(doc['roles']) - {r['role'] for r in records}))


def _slot_call(scene, row, callback):
    return _with_active_index(scene, row['object'], row['slot'], callback)


def _restore_record(scene, row):
    surface = _surface_slots()
    material = row['material']
    slot = row['object'].material_slots[row['slot']]
    slot.link = row['link']
    slot.material = material
    if row['action'] == 'update':
        wrapper = row['wrapper']
        for name, value in row['old_values'].items():
            wrapper.inputs[name].default_value = value
        for key, value in row['old_metadata'].items():
            material[key] = value
        _slot_call(scene, row, lambda fake: surface._owned(fake))
    elif row['action'] == 'create' and row.get('created_material') is not None:
        surface._cleanup([row['created_material']], [row['created_group']] if row.get('created_group') else [])
    if slot.link != row['link'] or slot.material != material:
        raise RecipeError('Slot rollback did not restore original identity')


def apply(context, document, bindings, line_bindings):
    """Apply surfaces then Pencil with exact native parameter/metadata rollback."""
    from . import cel_lines
    surface = _surface_slots()
    plan = preflight(context, document, bindings, line_bindings)
    scene = context.scene
    changed = []
    line_token = None
    try:
        for row in plan['records']:
            action = row['action']
            if action not in {'create', 'update'}:
                continue
            if action == 'update':
                material = row['material']
                wrapper = _find_wrapper(material)
                row['wrapper'] = wrapper
                row['old_values'] = {socket.name: _materials()._plain(socket.default_value)
                    for socket in wrapper.inputs if hasattr(socket, 'default_value')}
                row['old_metadata'] = {key: material[key] for key in
                    (surface._BASELINE, surface._DECLARED, surface._SIGNATURE)}
            changed.append(row)
            if action == 'create':
                _slot_call(scene, row, lambda fake: surface.apply(fake, row['settings']))
                material = row['object'].material_slots[row['slot']].material
                row['created_material'] = material
                row['created_group'] = _find_wrapper(material).node_tree
            else:
                _slot_call(scene, row, lambda fake: surface.update(fake, row['settings']))
        if plan['line_plan'] is not None:
            line_token = cel_lines.apply(scene, plan['line_plan'])
            line_result = cel_lines.inspect(scene, line_token)
            if line_result.get('status') not in {'applied', 'valid'}:
                raise RecipeError('Line recipe verification failed')
        else:
            line_result = None
        states = []
        for row in plan['records']:
            if row['action'] == 'preserve':
                if row['object'].material_slots[row['slot']].material != row['material']:
                    raise RecipeError('PRESERVE material changed during recipe application')
                state = None
            else:
                state = _slot_call(scene, row, surface.inspect)
                if state['status'] != 'valid':
                    raise RecipeError('; '.join(state['issues']))
            states.append(dict(role=row['role'], object=row['object'].name, slot=row['slot'],
                action=row['action'], material=_material_label(row['object'].material_slots[row['slot']].material),
                surface=state))
        return dict(schema=RECEIPT_SCHEMA, status='applied', recipe_id=plan['document']['recipe_id'],
            revision=plan['document']['revision'], slots=states, lines=line_result,
            unbound_roles=plan['unbound_roles'])
    except Exception as error:
        issues = []
        if line_token is not None:
            try:
                cel_lines.rollback(scene, line_token)
            except Exception as rollback_error:
                issues.append('Pencil: ' + str(rollback_error))
        for row in reversed(changed):
            try:
                _restore_record(scene, row)
            except Exception as rollback_error:
                issues.append(row['object'].name + ': ' + str(rollback_error))
        if issues:
            raise RecipeError('Recipe failed; rollback incomplete: ' + '; '.join(issues)) from error
        raise


def inspect(context, document, bindings, line_bindings):
    plan = preflight(context, document, bindings, line_bindings)
    return dict(schema=INSPECT_SCHEMA, status='ready', recipe_id=plan['document']['recipe_id'],
        revision=plan['document']['revision'], unbound_roles=plan['unbound_roles'],
        slots=[dict(role=r['role'], object=r['object'].name, slot=r['slot'], action=r['action'])
            for r in plan['records']],
        lines=plan['line_plan']['groups'] if plan['line_plan'] else [])
