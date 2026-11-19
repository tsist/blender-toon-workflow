# SPDX-License-Identifier: GPL-3.0-or-later
"""Static CEL line recipe slice for the Pencil viewport style engine.

This leaf module converts one clean BASIC author state into an explicit
autonomous PRESET document.  It deliberately does not integrate with the
legacy run/edit workers, does not write files, and does not modify the Pencil
source package.

Public API
----------
preflight(scene, lines, bindings) -> plan
apply(scene, plan) -> runtime rollback token
rollback(scene, token)
inspect(scene, token) -> JSONable receipt

``lines`` is a sequence of ``{"role": str, "width": float,
"color": [r, g, b, 1.0]}``.  ``bindings`` maps each role to an explicit list
of real ``bpy.types.Object`` values.  No name lookup, selection set, or
implicit object discovery is performed.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re


PLAN_SCHEMA = 'toon.cel-lines.plan.v1'
TOKEN_SCHEMA = 'toon.cel-lines.rollback-token.v1'
STATE_SCHEMA = 'toon.cel-lines.basic-state.v1'
INSPECT_SCHEMA = 'toon.cel-lines.inspection.v1'

MAX_GROUPS = 32
MAX_OBJECTS_PER_ROLE = 4096
MAX_OBJECTS = 4096
PENCIL_ROOTS = ('toon_viewport_preview', 'toon_author_files')
MAX_PRESET_REVISION = 2147483647

__all__ = [
    'PLAN_SCHEMA', 'TOKEN_SCHEMA', 'STATE_SCHEMA', 'INSPECT_SCHEMA',
    'preflight', 'apply', 'rollback', 'inspect',
]


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _bpy():
    import bpy
    return bpy


def _pencil(name=None):
    """Resolve the sibling Pencil package from either add-on layout.

    In ``toon_workflow_addon`` the existing ``backend.pencil`` seam is used.
    When this leaf is installed directly in ``toon_viewport_preview`` the
    relative import is used.  A top-level import is retained for offline
    schema tests.
    """
    package = __package__
    if package:
        try:
            backend = importlib.import_module(package + '.backend')
        except ImportError:
            backend = None
        if backend is not None and hasattr(backend, 'pencil'):
            return backend.pencil(name)
        return importlib.import_module(
            package + ('.' + name if name else ''))
    return importlib.import_module(
        'toon_viewport_preview' + ('.' + name if name else ''))


def _is_number(value):
    return type(value) in (int, float) and not isinstance(value, bool)


def _finite(value, label):
    _need(_is_number(value), label + ' must be a real number; bool is not numeric')
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(label + ' is outside binary64 range') from error
    _need(math.isfinite(result), label + ' must be finite')
    return result


def _role(value, label):
    _need(type(value) is str, label + ' must be a string')
    _need(1 <= len(value) <= 64, label + ' must contain 1..64 characters')
    _need(value == value.strip(), label + ' must not have leading/trailing whitespace')
    _need('\x00' not in value and not any(ord(char) < 32 for char in value),
          label + ' contains a control character')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError(label + ' must be UTF-8') from error
    return value


def _normalize_lines(lines):
    _need(type(lines) in (list, tuple), 'lines must be a list')
    _need(1 <= len(lines) <= MAX_GROUPS,
          'lines must contain 1..%d roles' % MAX_GROUPS)
    result = []
    seen = set()
    for index, row in enumerate(lines):
        _need(type(row) is dict, 'lines[%d] must be an object' % index)
        _need(set(row) == {'role', 'width', 'color'},
              'lines[%d] must contain exactly role, width and color' % index)
        role = _role(row['role'], 'lines[%d].role' % index)
        _need(role not in seen, 'duplicate line role: ' + role)
        seen.add(role)
        width = _finite(row['width'], 'lines[%d].width' % index)
        _need(0.1 <= width <= 32.0,
              'lines[%d].width must be in 0.1..32 pixels' % index)
        color = row['color']
        _need(type(color) in (list, tuple) and len(color) == 4,
              'lines[%d].color must have four RGBA components' % index)
        rgba = []
        for component, value in enumerate(color):
            _need(_is_number(value),
                  'lines[%d].color[%d] must be a real number' % (index, component))
            value = _finite(value,
                            'lines[%d].color[%d]' % (index, component))
            _need(0.0 <= value <= 1.0,
                  'lines[%d].color[%d] must be in 0..1' % (index, component))
            rgba.append(value)
        _need(rgba[3] == 1.0,
              'lines[%d].color alpha must be exactly 1' % index)
        result.append(dict(role=role, width=width, color=rgba))
    return result


def _selector_key(ref):
    _need(type(ref) is dict and set(ref) == {'name', 'library'},
          'selector identity must contain exactly name and library')
    _need(type(ref['name']) is str and 1 <= len(ref['name']) <= 1024,
          'selector object name is invalid')
    _need(ref['library'] is None or
          (type(ref['library']) is str and 1 <= len(ref['library']) <= 8192),
          'selector library identity is invalid')
    return json.dumps(ref, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def _witness(options, role):
    _need(type(options) in (list, tuple) and 1 <= len(options) <= MAX_OBJECTS_PER_ROLE,
          'bindings[%r] must be an explicit non-empty object list' % role)
    bpy = _bpy()
    result = []
    local = set()
    for index, obj in enumerate(options):
        _need(isinstance(obj, bpy.types.Object),
              'bindings[%r][%d] is not a bpy.types.Object' % (role, index))
        _need(obj.type == 'MESH',
              'bindings[%r][%d] must be a MESH object' % (role, index))
        _need(1 <= len(obj.name) <= 1024,
              'bindings[%r][%d] has an invalid object name' % (role, index))
        pointer = int(obj.as_pointer())
        _need(pointer > 0,
              'bindings[%r][%d] has an invalid Object pointer' % (role, index))
        _need(pointer not in local,
              'bindings[%r] contains the same Object twice' % role)
        local.add(pointer)
        library = obj.library.filepath if obj.library is not None else None
        if obj.library is not None:
            _need(type(library) is str and 1 <= len(library) <= 8192,
                  'bindings[%r][%d] has an invalid library path' % (role, index))
        _selector_key(dict(name=obj.name, library=library))
        result.append(dict(pointer=pointer, name=obj.name,
                           library=library, type=obj.type))
    return result


def _validate_bindings(scene, bindings, roles):
    _need(type(bindings) is dict, 'bindings must be an object keyed by role')
    role_set = set(roles)
    _need(set(bindings) == role_set,
          'bindings keys must exactly match line roles; missing=%r extra=%r'
          % (sorted(role_set - set(bindings)),
             sorted(set(bindings) - role_set)))
    result = {}
    claimed = {}
    total = 0
    for role in roles:
        options = bindings[role]
        _need(type(options) in (list, tuple) and
              1 <= len(options) <= MAX_OBJECTS_PER_ROLE,
              'bindings[%r] must be an explicit non-empty object list' % role)
        witnesses = _witness(options, role)
        for row, obj in zip(witnesses, options):
            pointer = row['pointer']
            if pointer in claimed:
                raise ValueError(
                    'Object %r is bound to both %r and %r'
                    % (row['name'], claimed[pointer], role))
            claimed[pointer] = role
            _need(obj.name in scene.objects and
                  int(scene.objects[obj.name].as_pointer()) == pointer,
                  'Object %r is not a live member of Scene %r'
                  % (row['name'], scene.name))
            _need((obj.library.filepath if obj.library is not None else None)
                  == row['library'],
                  'Object %r library identity changed during preflight'
                  % row['name'])
        total += len(witnesses)
        _need(total <= MAX_OBJECTS,
              'total explicit object bindings exceed %d' % MAX_OBJECTS)
        result[role] = witnesses
    return result


def _scene_ref(scene):
    library = scene.library.filepath if scene.library is not None else None
    return dict(pointer=int(scene.as_pointer()), name=scene.name,
                library=library)


def _id_ref(value):
    if value is None:
        return None
    library = getattr(value, 'library', None)
    return dict(pointer=int(value.as_pointer()),
                name=getattr(value, 'name', None),
                type=value.bl_rna.identifier,
                library=library.filepath if library is not None else None)


def _same_id(left, right):
    try:
        return int(left.as_pointer()) == int(right.as_pointer())
    except Exception:
        return left is right


def _plain_value(value):
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) in (list, tuple):
        return [_plain_value(item) for item in value]
    if type(value) is dict:
        return {str(key): _plain_value(item) for key, item in value.items()}
    if not isinstance(value, (str, bytes)):
        try:
            return [_plain_value(item) for item in value]
        except TypeError:
            pass
    raise ValueError('unsupported RNA value type: ' + type(value).__name__)


_SIMPLE_PROP_TYPES = {
    'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM',
    'BOOLEAN_ARRAY', 'INT_ARRAY', 'FLOAT_ARRAY',
}


def _capture_propgroup(target):
    result = dict(values={}, collections={}, pointers={}, readonly=[])
    for prop in target.bl_rna.properties:
        identifier = prop.identifier
        if identifier == 'rna_type':
            continue
        if prop.is_readonly:
            result['readonly'].append(identifier)
        if prop.type == 'COLLECTION':
            rows = []
            for item in getattr(target, identifier):
                rows.append(_capture_propgroup(item))
            result['collections'][identifier] = rows
        elif prop.type == 'POINTER':
            live = getattr(target, identifier)
            result['pointers'][identifier] = dict(live=live, ref=_id_ref(live))
        elif prop.type in _SIMPLE_PROP_TYPES:
            result['values'][identifier] = _plain_value(
                getattr(target, identifier))
        else:
            raise ValueError(
                'unsupported Pencil RNA property type %r for %s'
                % (prop.type, identifier))
    return result


def _witness_propgroup(captured):
    return dict(
        values=copy.deepcopy(captured['values']),
        collections={
            name: [_witness_propgroup(row) for row in rows]
            for name, rows in captured['collections'].items()
        },
        pointers={
            name: copy.deepcopy(row['ref'])
            for name, row in captured['pointers'].items()
        })


def _restore_propgroup(target, captured):
    readonly = set(captured.get('readonly') or ())
    for name, rows in captured['collections'].items():
        collection = getattr(target, name)
        collection.clear()
        for row in rows:
            item = collection.add()
            _restore_propgroup(item, row)
    for name, value in captured['values'].items():
        if name in readonly:
            continue
        setattr(target, name, copy.deepcopy(value))
    for name, row in captured['pointers'].items():
        if name in readonly:
            continue
        setattr(target, name, row['live'])


def _is_pencil_path(path):
    if type(path) is not str:
        return False
    for root in PENCIL_ROOTS:
        if path == root:
            return True
        if path.startswith(root + '.') or path.startswith(root + '['):
            return True
        if path.startswith('["' + root + '"]') or path.startswith("['" + root + "']"):
            return True
    return False


def _pencil_animation_paths(scene):
    """Use the existing Scene/Action-slot/NLA and incoming-driver scope."""
    return sorted({path for path in _pencil('group_ui')._animation_paths(scene)
                   if _is_pencil_path(path)})


def _capture_texts():
    bpy = _bpy()
    rows = []
    for text in bpy.data.texts:
        library = text.library.filepath if text.library is not None else None
        rows.append(dict(
            live=text,
            pointer=int(text.as_pointer()),
            name=text.name,
            library=library,
            users=int(text.users),
            use_fake_user=bool(text.use_fake_user),
            owned=bool(text.get('toon_viewport_preview_owned')),
            reclaimable=bool(text.get('toon_viewport_preview_reclaimable')),
        ))
    rows.sort(key=lambda row: row['pointer'])
    return rows


def _text_witness(rows):
    return [{key: copy.deepcopy(value) for key, value in row.items()
             if key != 'live'} for row in rows]


def _capture_state(scene):
    settings, _prop = _settings(scene)
    author = getattr(scene, 'toon_author_files', None)
    return dict(
        schema=STATE_SCHEMA,
        scene=_scene_ref(scene),
        settings=_capture_propgroup(settings),
        author_settings_present=author is not None,
        author_settings=(_capture_propgroup(author)
                         if author is not None else None),
        preset_text=_id_ref(settings.preset_text),
        curve_tree=_id_ref(settings.curve_tree),
        author_tree=(_id_ref(author.author_tree)
                      if author is not None else None),
        texts=_capture_texts(),
        animation_paths=_pencil_animation_paths(scene),
    )


def _state_witness(state):
    return dict(
        schema=state['schema'],
        scene=copy.deepcopy(state['scene']),
        settings=_witness_propgroup(state['settings']),
        author_settings_present=state['author_settings_present'],
        author_settings=(_witness_propgroup(state['author_settings'])
                         if state['author_settings'] is not None else None),
        trees=dict(
            preset_text=copy.deepcopy(state['preset_text']),
            curve_tree=copy.deepcopy(state['curve_tree']),
            author_tree=copy.deepcopy(state['author_tree']),
        ),
        texts=_text_witness(state['texts']),
        animation_paths=list(state['animation_paths']),
    )


def _canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError('value is not canonical JSON: ' + str(error)) from error


def _canonical_sha256(value):
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _state_sha256(state):
    return _canonical_sha256(_state_witness(state))


def _settings(scene):
    bpy = _bpy()
    _need(isinstance(scene, bpy.types.Scene), 'scene must be bpy.types.Scene')
    prop = getattr(_pencil('addon'), 'PROP', 'toon_viewport_preview')
    settings = getattr(scene, prop, None)
    _need(settings is not None,
          'Pencil Scene.%s property is not registered' % prop)
    return settings, prop


def _validate_scene(scene):
    settings, _prop = _settings(scene)
    _need(scene.library is None,
          'linked Scene is read-only for this static CEL slice')
    _need(bool(scene.is_editable), 'Scene must be editable')
    for field in ('resolution_x', 'resolution_y'):
        value = getattr(scene.render, field)
        _need(type(value) is int and 1 <= value <= 16384,
              'Scene.render.%s must be an integer in 1..16384' % field)
    return settings


def _validate_clean_state(scene, state, settings):
    _need(settings.preset_text is None,
          'clean BASIC requires Scene.toon_viewport_preview.preset_text is None')
    _need(settings.style_mode == 'BASIC',
          'clean BASIC requires style_mode == "BASIC"')
    for name in ('groups', 'elements', 'amount_tracks', 'parameter_tracks',
                 'chains', 'dashes', 'map_amounts'):
        collection = getattr(settings, name)
        _need(len(collection) == 0,
              'clean BASIC requires empty %s collection' % name)
    for name in ('chain_controls_json', 'map_amounts_json',
                 'dash_patterns_json'):
        _need(not getattr(settings, name),
              'clean BASIC requires empty %s' % name)
    _need(settings.curve_tree is None,
          'clean BASIC requires no curve_tree or pending curve author')
    _need((getattr(settings, 'curve_error', '') or '') == '',
          'clean BASIC requires an empty curve_error')
    author = getattr(scene, 'toon_author_files', None)
    if author is not None:
        _need(author.author_tree is None,
              'clean BASIC requires no author graph (toon_author_files.author_tree)')
    _need(not state['animation_paths'],
          'Pencil RNA is referenced by Action/NLA/driver: '
          + '; '.join(state['animation_paths'][:8]))
    _need(int(settings.preset_revision) < MAX_PRESET_REVISION,
          'preset_revision is exhausted; start a fresh clean BASIC state')


def _build_document(scene, specs, witnesses):
    file_authoring = _pencil('file_authoring')
    group_lifecycle = _pencil('group_lifecycle')
    selectors = _pencil('selector_bindings')

    width = int(scene.render.resolution_x)
    height = int(scene.render.resolution_y)
    wrapped = file_authoring.new_document(
        width=width, height=height, label=specs[0]['role'])
    document = wrapped['document']
    recipe = wrapped['recipe']

    owned = group_lifecycle.owned(document)
    _need(len(owned) == 1,
          'file_authoring.new_document must create exactly one base group')
    groups = [dict(role=specs[0]['role'],
                   group_id=next(iter(owned)))]
    for spec in specs[1:]:
        document, _plan, _bindings, report = group_lifecycle.migrate(
            document, recipe, dict(action='NEW', label=spec['role']))
        groups.append(dict(role=spec['role'], group_id=report['group']))

    for spec, row in zip(specs, groups):
        owned = group_lifecycle.owned(document)
        _need(row['group_id'] in owned,
              'group_lifecycle.migrate did not retain a new group')
        actuals = sorted(
            actual for actual, meta in owned[row['group_id']]['brushes'].items()
            if meta['role'] == 'DEFAULT')
        _need(len(actuals) == 2,
              'owned group must have independent VISIBLE/HIDDEN defaults')
        for actual in actuals:
            document, _plan, _bindings, _report = group_lifecycle.migrate(
                document, recipe,
                dict(action='BRUSH', group=row['group_id'], actual=actual,
                     values=dict(native_size=float(spec['width']),
                                 blend_amount=1.0,
                                 color=list(spec['color'][:3]))))

    document, _plan, _bindings, _report = group_lifecycle.migrate_edges(
        document, recipe, [dict(group=row['group_id'], enabled=True, routes=[],
            visible_enabled=True, hidden_enabled=False) for row in groups])
    rows, order = selectors.records(document)
    known = {row['group_id'] for row in groups}
    _need(set(rows) == known,
          'selector bindings contain an unknown or missing style group')
    _need(order is not None and set(order) == known,
          'selector priority order must contain every style group once')
    for spec, row in zip(specs, groups):
        binding_row = rows[row['group_id']]
        binding_row['enabled'] = True
        binding_row['objects'] = [
            dict(name=item['name'], library=item['library'])
            for item in witnesses[spec['role']]
        ]
        _need(binding_row['collections'] == [] and
              binding_row['materials'] == [],
              'this slice binds MESH Object values only')
    document['request']['selector_bindings'] = dict(
        schema=selectors.SCHEMA,
        groups=[rows[group_id] for group_id in rows],
        order=list(order))
    document['inputs']['projection']['editor_document']['active_group'] = \
        groups[0]['group_id']

    checked = file_authoring.checked(dict(
        schema=file_authoring.SCHEMA, document=document, recipe=recipe))
    return checked['document'], checked['recipe'], groups


def _compile_document(document, recipe):
    file_authoring = _pencil('file_authoring')
    style = _pencil('style')
    source_bindings = _pencil('source_bindings')
    checked = file_authoring.checked(dict(
        schema=file_authoring.SCHEMA, document=document, recipe=recipe))
    document = checked['document']
    recipe = checked['recipe']
    compiled = style.compile_preset(document, recipe)
    recovered = source_bindings.recover(document)
    _need(compiled['status']['unknown_field_count'] == 0,
          'checked compile reported unknown fields')
    _need(compiled['status']['unknown_brush_slot_count'] == 0,
          'checked compile reported unknown brush slots')
    return document, recipe, compiled, recovered


def _group_rows(document):
    group_lifecycle = _pencil('group_lifecycle')
    records = group_lifecycle.records(document)
    _need(records['archived'] == {},
          'this slice requires no archived style groups')
    return {row['group']: row for row in records['authored']}


def _verify_brush_values(brush, spec, label, *, editor):
    _need(abs(float(brush['native_size']) - spec['width']) <= 0.0,
          '%s width differs from requested %r' % (label, spec['width']))
    _need([float(value) for value in brush['color']] ==
          list(spec['color'][:3]),
          '%s color differs from requested RGB' % label)
    _need(float(brush['blend_amount']) == 1.0,
          '%s blend_amount must be 1 for alpha=1' % label)
    if editor:
        for field in ('width_map', 'alpha_map'):
            mapping = brush[field]
            _need(type(mapping) is dict and mapping.get('enabled') is False,
                  '%s must have disabled %s' % (label, field))
        for field in ('size_curve', 'alpha_curve'):
            curve = brush[field]
            _need(type(curve) is dict and curve.get('enabled') is False,
                  '%s must have disabled %s' % (label, field))
    else:
        for field in ('width_map', 'alpha_map', 'size_curve', 'alpha_curve'):
            _need(brush[field] is None,
                  '%s must not contain active %s data' % (label, field))


def _verify_document(document, recipe, specs, witnesses, groups):
    _need(document.get('inputs', {}).get('full_preset') is None,
          'autonomous slice must not retain a captured native full_preset')
    _need(document['request'].get('intersection', {}).get('groups') == [],
          'clean BASIC slice must not contain legacy intersection authors')
    _need(recipe.get('object_bindings') == [] and
          recipe.get('material_bindings') == [],
          'explicit selector bindings must not add recipe name bindings')
    owned = _group_rows(document)
    groups_by_role = {row['role']: row['group_id'] for row in groups}
    _need(len(groups_by_role) == len(specs),
          'style group roles must be unique')
    boundary_groups = {
        row['id']: row for row in document['request']['boundary']['groups']}
    editor_groups = {
        row['uid']: row
        for row in document['inputs']['projection']['editor_document']['groups']}
    boundary_aliases = document['request'].get('boundary_aliases', [])
    boundary_by_slot = {
        (row['group'], row['slot']): row for row in boundary_aliases}
    units = document['request']['boundary']['units']
    _need(float(units['absolute_scale']) == 1.0,
          'boundary units absolute_scale must be 1 for exact brush width')
    _need(units['relative_target_height'] is None and
          units['relative_target_width'] is None,
          'boundary units relative targets must be unused')

    document, recipe, compiled, recovered = _compile_document(document, recipe)
    blocking = {
        'brush-boundary-mismatch', 'editor-boundary-mismatch',
        'missing-editor-group', 'unrepresented-editor-group',
    }
    _need(not any(row.get('code') in blocking for row in compiled['warnings']),
          'checked compile reported inconsistent editor/boundary data')
    compiled_by_id = {row['id']: row for row in compiled['groups']}
    _need(set(compiled_by_id) == set(groups_by_role.values()),
          'compiled groups differ from planned style groups')
    _need(set(boundary_groups) == set(groups_by_role.values()),
          'boundary groups differ from planned style groups')
    _need(set(editor_groups) == set(groups_by_role.values()),
          'editor groups differ from planned style groups')
    _need(set(owned) == set(groups_by_role.values()),
          'owned group lifecycle records differ from planned style groups')
    _need(set(recovered['groups']) == set(groups_by_role.values()),
          'recovered author groups differ from planned style groups')

    selector_rows, selector_order = _pencil('selector_bindings').records(document)
    _need(set(selector_rows) == set(groups_by_role.values()),
          'selector rows differ from planned style groups')
    _need(selector_order is not None and
          set(selector_order) == set(groups_by_role.values()),
          'selector order must contain every style group once')
    seen_objects = {}

    for spec in specs:
        gid = groups_by_role[spec['role']]
        group = compiled_by_id[gid]
        _need(group['label'] == spec['role'],
              'style group label must equal its role')
        _need(group['brush']['color'] == list(spec['color'][:3]),
              'compiled group brush color differs from role line')
        _need(float(group['brush']['blend_amount']) == 1.0,
              'compiled group brush blend_amount must be 1')
        _need(float(group['brush_meta']['brush']['native_size']) ==
              spec['width'],
              'compiled group brush native_size must equal line width')
        for field in ('width_map', 'alpha_map', 'size_curve', 'alpha_curve'):
            _need(group['brush'][field] is None,
                  'compiled brush must not contain active %s' % field)
        _need(group['size_mode'] == 'ABSOLUTE',
              'owned group size_mode must be ABSOLUTE')
        _need(float(group['initial_scale_ex']) == 1.0,
              'owned group initial_scale_ex must be 1')
        _need(float(group['coverage_extent_factor']) == 1.0,
              'owned group coverage_extent_factor must be 1')

        boundary = boundary_groups[gid]
        editor = editor_groups[gid]
        _need(boundary.get('object_ids') == [] and
              boundary.get('material_names') == [],
              'this slice must use explicit selector bindings, not name lists')
        for field in ('size_mode', 'initial_scale_ex',
                      'coverage_extent_factor'):
            _need(boundary[field] == editor[field],
                  'boundary/editor %s must agree' % field)
        _need(boundary['size_mode'] == 'ABSOLUTE',
              'boundary group size_mode must be ABSOLUTE')
        _need(float(boundary['initial_scale_ex']) == 1.0,
              'boundary group initial_scale_ex must be 1')
        _need(float(boundary['coverage_extent_factor']) == 1.0,
              'boundary group coverage_extent_factor must be 1')

        row = owned[gid]
        _need(len(row['brushes']) >= 2,
              'owned group must contain independent V/H authors')
        for actual, meta in row['brushes'].items():
            if meta['role'] != 'DEFAULT':
                continue
            slot = _pencil('edge_sources').slot_for(actual)
            _need(slot in editor['brushes'] and slot in boundary,
                  'owned default author is missing from editor/boundary')
            _verify_brush_values(editor['brushes'][slot], spec,
                                 '%s/%s editor' % (gid, slot), editor=True)
            _verify_brush_values(boundary[slot], spec,
                                 '%s/%s boundary' % (gid, slot), editor=False)
            _need(boundary_by_slot.get((gid, slot), {}).get('brush_id') ==
                  actual,
                  'boundary alias must retain the exact owned actual')
        _need('brush' in editor['brushes'] and 'brush' in boundary,
              'owned group must expose the visible default brush clone')
        _verify_brush_values(editor['brushes']['brush'], spec,
                             '%s/brush editor' % gid, editor=True)
        _verify_brush_values(boundary['brush'], spec,
                             '%s/brush boundary' % gid, editor=False)

        selector = selector_rows[gid]
        expected = witnesses[spec['role']]
        _need(selector['enabled'] is True,
              'explicit selector row must be enabled')
        _need(group['source_line_set_id'] == 1 and
              group['output_to_elements_only'] is False,
              'owned groups must keep neutral line-set/element defaults')
        actual_keys = [_selector_key(item) for item in selector['objects']]
        expected_keys = [_selector_key(dict(name=item['name'],
                                            library=item['library']))
                         for item in expected]
        _need(actual_keys == expected_keys,
              'selector object list/order differs from explicit bindings')
        _need(selector['collections'] == [] and selector['materials'] == [],
              'this slice must not add collection/material selectors')
        for item in selector['objects']:
            key = _selector_key(item)
            if key in seen_objects:
                raise ValueError(
                    'selector object %r is rebound across style groups'
                    % item['name'])
            seen_objects[key] = gid

    return compiled


def _group_summaries(document, compiled, specs, witnesses, groups):
    boundary = {
        row['id']: row for row in document['request']['boundary']['groups']}
    selectors = _pencil('selector_bindings').records(document)[0]
    compiled_by_id = {row['id']: row for row in compiled['groups']}
    result = []
    for spec, group in zip(specs, groups):
        gid = group['group_id']
        row = compiled_by_id[gid]
        result.append(dict(
            role=spec['role'],
            group_id=gid,
            label=row['label'],
            priority=row['priority'],
            line_set_id=row['source_line_set_id'],
            elements_only=row['output_to_elements_only'],
            brush=dict(native_size=spec['width'],
                       color=list(spec['color'][:3]),
                       blend_amount=1.0),
            objects=copy.deepcopy(witnesses[spec['role']]),
            selector=copy.deepcopy(selectors[gid]),
            boundary_visibility=dict(
                visible=boundary[gid]['visible_enabled'],
                hidden=boundary[gid]['hidden_enabled']),
        ))
    return result


def _compiled_digest(compiled, recovered):
    return dict(
        schema=compiled['schema'],
        status=copy.deepcopy(compiled['status']),
        binding_report=copy.deepcopy(compiled['binding_report']),
        warning_count=len(compiled['warnings']),
        warning_codes=sorted({row.get('code')
                              for row in compiled['warnings']
                              if row.get('code') is not None}),
        group_count=len(compiled['groups']),
        actual_count=len(recovered['actuals']),
        route_count=len(recovered['routes']),
    )


def _plan_core(plan):
    return dict(
        schema=plan['schema'],
        scene=copy.deepcopy(plan['scene']),
        canvas=copy.deepcopy(plan['canvas']),
        lines=copy.deepcopy(plan['lines']),
        groups=copy.deepcopy(plan['groups']),
        binding_witness=copy.deepcopy(plan['binding_witness']),
        bindings_sha256=plan['bindings_sha256'],
        document=copy.deepcopy(plan['document']),
        recipe=copy.deepcopy(plan['recipe']),
        document_sha256=plan['document_sha256'],
        recipe_sha256=plan['recipe_sha256'],
        state_sha256=plan['state_sha256'],
    )


def preflight(scene, lines, bindings):
    """Validate a clean BASIC state and compile one static PRESET plan."""
    specs = _normalize_lines(lines)
    settings = _validate_scene(scene)
    witnesses = _validate_bindings(
        scene, bindings, [spec['role'] for spec in specs])
    state = _capture_state(scene)
    _validate_clean_state(scene, state, settings)
    state_sha = _state_sha256(state)

    document, recipe, groups = _build_document(scene, specs, witnesses)
    document, recipe, compiled, recovered = _compile_document(document, recipe)
    verified_compiled = _verify_document(
        document, recipe, specs, witnesses, groups)

    document_sha = _canonical_sha256(document)
    recipe_sha = _canonical_sha256(recipe)
    binding_sha = _canonical_sha256(witnesses)
    plan = dict(
        schema=PLAN_SCHEMA,
        scene=_scene_ref(scene),
        canvas=dict(width=int(scene.render.resolution_x),
                    height=int(scene.render.resolution_y)),
        lines=copy.deepcopy(specs),
        groups=_group_summaries(
            document, compiled, specs, witnesses, groups),
        binding_witness=copy.deepcopy(witnesses),
        bindings_sha256=binding_sha,
        document=copy.deepcopy(document),
        recipe=copy.deepcopy(recipe),
        document_sha256=document_sha,
        recipe_sha256=recipe_sha,
        state_sha256=state_sha,
        state_witness=_state_witness(state),
        compiled=copy.deepcopy(verified_compiled),
        compiled_summary=_compiled_digest(compiled, recovered),
        limitations=[
            'clean BASIC only: no pre-existing preset/groups/elements/tracks/'
            'chains/dashes/map-amounts/curve tree/author graph',
            'MESH objects only; every object must be unique across roles',
            'width is exact only through the owned ABSOLUTE unit profile '
            '(absolute_scale=1, initial_scale_ex=1, coverage=1, '
            'width_multiplier=1, group width_scale=1)',
            'no intersections, curves, maps, brush effects, animation or '
            'native full_preset are authored',
            'RGBA is passed to Pencil linear brush RGB without color-space '
            'conversion; alpha must be exactly 1',
        ],
    )
    plan['content_sha256'] = _canonical_sha256(_plan_core(plan))
    return plan


def _validate_witness_list(role, rows):
    _need(type(rows) is list and 1 <= len(rows) <= MAX_OBJECTS_PER_ROLE,
          'binding witness for %r must be a non-empty list' % role)
    for index, row in enumerate(rows):
        _need(type(row) is dict and
              set(row) == {'pointer', 'name', 'library', 'type'},
              'binding witness %r[%d] has invalid fields' % (role, index))
        _need(type(row['pointer']) is int and row['pointer'] > 0,
              'binding witness %r[%d].pointer is invalid' % (role, index))
        _need(row['type'] == 'MESH',
              'binding witness %r[%d] must be MESH' % (role, index))
        _selector_key(dict(name=row['name'], library=row['library']))


def _validate_plan(plan, scene):
    _need(type(plan) is dict, 'plan must be an object')
    required = {
        'schema', 'scene', 'canvas', 'lines', 'groups', 'binding_witness',
        'bindings_sha256', 'document', 'recipe', 'document_sha256',
        'recipe_sha256', 'state_sha256', 'state_witness', 'compiled',
        'compiled_summary',
        'limitations', 'content_sha256',
    }
    _need(set(plan) == required, 'plan fields differ from the fixed API')
    _need(plan['schema'] == PLAN_SCHEMA, 'unsupported CEL line plan schema')
    _need(type(plan['scene']) is dict and
          plan['scene'] == _scene_ref(scene),
          'plan is bound to a different Scene')
    _need(plan['canvas'] ==
          dict(width=int(scene.render.resolution_x),
               height=int(scene.render.resolution_y)),
          'Scene render resolution changed since preflight')
    specs = _normalize_lines(plan['lines'])
    _need(specs == plan['lines'], 'plan lines are not normalized')
    _need(isinstance(plan['document'], dict) and
          isinstance(plan['recipe'], dict),
          'plan must contain a compiled document and recipe')
    _need(_canonical_sha256(plan['document']) == plan['document_sha256'],
          'plan document digest differs')
    _need(_canonical_sha256(plan['recipe']) == plan['recipe_sha256'],
          'plan recipe digest differs')
    _need(type(plan['state_sha256']) is str and
          len(plan['state_sha256']) == 64,
          'plan state digest is invalid')
    _need(type(plan['binding_witness']) is dict,
          'plan binding witness must be an object')
    _need(set(plan['binding_witness']) ==
          {spec['role'] for spec in specs},
          'plan binding witness roles differ from lines')
    for role, rows in plan['binding_witness'].items():
        _validate_witness_list(role, rows)
    _need(_canonical_sha256(plan['binding_witness']) ==
          plan['bindings_sha256'],
          'plan binding witness digest differs')

    groups = plan['groups']
    _need(type(groups) is list and len(groups) == len(specs),
          'plan group count differs from lines')
    roles = [spec['role'] for spec in specs]
    group_roles = [row.get('role') for row in groups]
    _need(group_roles == roles,
          'plan group role order differs from lines')
    group_ids = [row.get('group_id') for row in groups]
    _need(all(type(gid) is str and re.fullmatch('[0-9a-f]{32}', gid)
              for gid in group_ids) and len(set(group_ids)) == len(group_ids),
          'plan group IDs must be unique 32-hex identities')
    for row, role in zip(groups, roles):
        _need(row.get('objects') == plan['binding_witness'][role],
              'plan group object witness differs from binding witness')
        _need(type(row.get('selector')) is dict and
              row['selector'].get('objects') ==
              [dict(name=item['name'], library=item['library'])
               for item in plan['binding_witness'][role]],
              'plan selector witness differs from explicit bindings')
    _need(_canonical_sha256(_plan_core(plan)) == plan['content_sha256'],
          'plan content digest differs')
    return specs


def _resolve_plan_objects(scene, plan, specs):
    bpy = _bpy()
    by_pointer = {}
    for obj in scene.objects:
        by_pointer[int(obj.as_pointer())] = obj
    result = {}
    claimed = {}
    for spec in specs:
        role = spec['role']
        resolved = []
        for witness in plan['binding_witness'][role]:
            pointer = witness['pointer']
            obj = by_pointer.get(pointer)
            if obj is None or not isinstance(obj, bpy.types.Object):
                raise ValueError(
                    'bound Object %r no longer exists in Scene %r'
                    % (witness['name'], scene.name))
            _need(obj.type == 'MESH',
                  'bound Object %r is no longer MESH' % witness['name'])
            _need(obj.name == witness['name'] and
                  (obj.library.filepath if obj.library is not None else None)
                  == witness['library'],
                  'bound Object identity changed since preflight: '
                  + witness['name'])
            if pointer in claimed:
                raise ValueError(
                    'Object %r is bound to both %r and %r'
                    % (witness['name'], claimed[pointer], role))
            claimed[pointer] = role
            resolved.append(obj)
        result[role] = resolved
    return result


def _new_texts(before_state):
    existing = {row['pointer'] for row in before_state['texts']}
    return [row for row in _capture_texts()
            if row['pointer'] not in existing]


def _created_preset_texts(before, document, recipe):
    """Identify only the single Text produced by the checked set_preset call."""
    expected = dict(schema='toon.viewport-persisted-style.v1',
                    document=document, recipe=recipe)
    matches = []
    for row in _new_texts(before):
        text = row['live']
        if (not row['owned'] or not row['reclaimable'] or
                re.fullmatch(r'\.toon_viewport_style\.[0-9a-f]{32}', text.name) is None):
            continue
        try:
            if json.loads(text.as_string()) == expected:
                matches.append(text)
        except (ValueError, TypeError):
            continue
    _need(len(matches) <= 1, 'Ambiguous new preset Text ownership; preserve all candidates')
    return matches


def _cleanup_created_texts(created_texts):
    bpy = _bpy()
    removed = []
    for text in created_texts:
        if text.library or text.use_fake_user or text.users != 0:
            continue
        if not text.get('toon_viewport_preview_owned'):
            continue
        removed.append(dict(pointer=int(text.as_pointer()), name=text.name))
        bpy.data.texts.remove(text)
    return removed


def _protect_text(text):
    if text is None or text.library is not None:
        return None
    previous = bool(text.use_fake_user)
    if not previous:
        text.use_fake_user = True
    return previous


def _restore_text_protection(text, previous):
    if text is None or previous is None or text.library is not None:
        return
    text.use_fake_user = bool(previous)


def _clear_preview_caches(scene):
    try:
        addon = _pencil('addon')
    except Exception:
        return
    pointer = int(scene.as_pointer())
    cache = getattr(addon, '_style_cache', None)
    if isinstance(cache, dict):
        for key in list(cache):
            try:
                if key and key[0] == pointer:
                    cache.pop(key, None)
            except Exception:
                continue
    chain_cache = getattr(addon, '_chain_input_cache', None)
    if isinstance(chain_cache, dict):
        chain_cache.pop(pointer, None)


def _state_values(state):
    return state['settings']['values']


def _compare_preserved_state(before, after):
    old_values = _state_values(before)
    new_values = _state_values(after)
    # Enum menu identifiers are derived from the newly installed document;
    # actual animation tracks and curve data remain separately protected.
    allowed = {'preset_revision', 'style_mode', 'width_multiplier', 'opacity',
               'parameter_choice', 'curve_group', 'curve_slot'}
    missing = set(old_values) - set(new_values)
    _need(not missing, 'Pencil settings disappeared: %r' % sorted(missing))
    for name, value in old_values.items():
        if name in allowed:
            continue
        _need(new_values.get(name) == value,
              'non-target BASIC setting changed: ' + name)
    for name in ('groups',):
        _need(name in before['settings']['collections'],
              'missing groups collection in before-state')
    for name, rows in before['settings']['collections'].items():
        if name == 'groups':
            continue
        _need(after['settings']['collections'].get(name) == rows,
              'non-target collection changed: ' + name)
    for name, row in before['settings']['pointers'].items():
        if name == 'preset_text':
            continue
        _need(after['settings']['pointers'].get(name, {}).get('ref') ==
              row['ref'],
              'non-target pointer changed: ' + name)
    if before['author_settings'] is None:
        _need(after['author_settings'] is None,
              'Pencil author file settings appeared unexpectedly')
    else:
        _need(after['author_settings'] is not None and
              _witness_propgroup(after['author_settings']) ==
              _witness_propgroup(before['author_settings']),
              'Pencil author file settings changed unexpectedly')
    _need(after['curve_tree'] == before['curve_tree'],
          'curve tree changed unexpectedly')
    _need(after['author_tree'] == before['author_tree'],
          'author graph changed unexpectedly')
    _need(after['animation_paths'] == before['animation_paths'],
          'Pencil RNA animation references changed unexpectedly')


def _preexisting_texts_intact(before, after):
    old = {row['pointer']: row for row in before['texts']}
    new = {row['pointer']: row for row in after['texts']}
    for pointer, row in old.items():
        current = new.get(pointer)
        _need(current is not None,
              'pre-existing Text disappeared: ' + row['name'])
        for field in ('name', 'library', 'users', 'use_fake_user',
                      'owned', 'reclaimable'):
            _need(current[field] == row[field],
                  'pre-existing Text %r changed %s'
                  % (row['name'], field))


def _verify_post_apply(scene, plan, before, document, recipe,
                       specs, witnesses, groups):
    settings = _validate_scene(scene)
    author = getattr(scene, 'toon_author_files', None)
    _need(settings.preset_text is not None,
          'addon.set_preset did not install a preset Text')
    text = settings.preset_text
    _need(text.library is None,
          'addon.set_preset installed a linked Text')
    _need(bool(text.get('toon_viewport_preview_owned')),
          'installed preset Text is not owned by Pencil')
    _need(bool(text.get('toon_viewport_preview_reclaimable')),
          'installed preset Text is not marked reclaimable')
    _need(text.users >= 1,
          'installed preset Text has no Scene consumer')
    _need(settings.style_mode == 'PRESET',
          'addon.set_preset did not switch style_mode to PRESET')
    _need(float(settings.width_multiplier) == 1.0,
          'width_multiplier must be exactly 1 in PRESET')
    _need(float(settings.opacity) == 1.0,
          'global opacity must be exactly 1 in PRESET')
    _need(settings.curve_tree is None,
          'apply created an unexpected curve tree')
    if author is not None:
        _need(author.author_tree is None,
              'apply created an unexpected author graph')

    old_revision = int(
        before['settings']['values'].get('preset_revision', -1))
    _need(int(settings.preset_revision) == old_revision + 1,
          'preset_revision did not advance exactly once')

    compiled = _verify_document(
        document, recipe, specs, witnesses, groups)
    expected_order = [
        row['id'] for row in sorted(
            compiled['groups'], key=lambda item: item['priority'])]
    actual_order = [item.name for item in settings.groups]
    _need(actual_order == expected_order,
          'Scene group controls do not follow compiled priority order')
    _need(len(settings.groups) == len(specs),
          'Scene group control count differs from lines')
    for item in settings.groups:
        _need(bool(item.enabled), 'new group control must be enabled')
        _need(float(item.width_scale) == 1.0,
              'new group width_scale must be 1')
        _need(float(item.opacity) == 1.0,
              'new group opacity must be 1')
        _need(int(item.line_set_id) == 1,
              'new group line_set_id must preserve the neutral default 1')
        _need(bool(item.elements_only) is False,
              'new group elements_only must be False')

    body = text.as_string()
    _need(len(body.encode('utf-8')) <= 16 * 1024 * 1024,
          'installed preset Text exceeds the 16 MiB static slice limit')
    wrapped = json.loads(body)
    _need(type(wrapped) is dict and
          wrapped.get('schema') == 'toon.viewport-persisted-style.v1',
          'installed preset Text has an unexpected wrapper schema')
    _need(_canonical_sha256(wrapped['document']) ==
          plan['document_sha256'],
          'installed preset document differs from the plan')
    _need(_canonical_sha256(wrapped['recipe']) ==
          plan['recipe_sha256'],
          'installed preset recipe differs from the plan')
    _verify_document(wrapped['document'], wrapped['recipe'],
                     specs, witnesses, groups)

    after = _capture_state(scene)
    _preexisting_texts_intact(before, after)
    _compare_preserved_state(before, after)
    new_texts = _new_texts(before)
    _need(len(new_texts) == 1,
          'apply must create exactly one owned preset Text')
    _need(new_texts[0]['pointer'] == int(text.as_pointer()),
          'created Text is not the installed preset Text')
    _need(new_texts[0]['owned'],
          'created Text is missing the Pencil owned marker')
    _need(not new_texts[0]['use_fake_user'],
          'created preset Text must not use a fake user')
    return text, after


def apply(scene, plan):
    """Apply one preflighted plan atomically and return a runtime token."""
    _validate_scene(scene)
    specs = _validate_plan(plan, scene)
    settings = _validate_scene(scene)
    before = _capture_state(scene)
    _validate_clean_state(scene, before, settings)
    _need(_state_sha256(before) == plan['state_sha256'],
          'Pencil BASIC state changed since preflight; rerun preflight')
    _resolve_plan_objects(scene, plan, specs)
    _need(_canonical_sha256(plan['document']) ==
          plan['document_sha256'], 'plan document digest changed')
    _need(_canonical_sha256(plan['recipe']) ==
          plan['recipe_sha256'], 'plan recipe digest changed')
    document = copy.deepcopy(plan['document'])
    recipe = copy.deepcopy(plan['recipe'])
    document, recipe, compiled, recovered = _compile_document(
        document, recipe)
    _need(_canonical_sha256(document) == plan['document_sha256'],
          'checked plan document differs from its digest')
    _need(_canonical_sha256(recipe) == plan['recipe_sha256'],
          'checked plan recipe differs from its digest')
    groups = [
        dict(role=row['role'], group_id=row['group_id'])
        for row in plan['groups']]
    witnesses = copy.deepcopy(plan['binding_witness'])
    _verify_document(document, recipe, specs, witnesses, groups)

    # Final state check immediately before the first mutation.
    _need(_state_sha256(_capture_state(scene)) == plan['state_sha256'],
          'Pencil BASIC state changed while compiling; rerun preflight')

    old_text = settings.preset_text
    protected = _protect_text(old_text)
    created = None
    try:
        settings.width_multiplier = 1.0
        settings.opacity = 1.0
        _pencil('addon').set_preset(scene, document, recipe)
        _restore_text_protection(old_text, protected)
        protected = None
        created, after = _verify_post_apply(
            scene, plan, before, document, recipe,
            specs, witnesses, groups)
        created_texts = [created]
        token = dict(
            schema=TOKEN_SCHEMA,
            status='applied',
            scene=_scene_ref(scene),
            plan_content_sha256=plan['content_sha256'],
            document_sha256=plan['document_sha256'],
            recipe_sha256=plan['recipe_sha256'],
            state_before_sha256=plan['state_sha256'],
            state_after_sha256=_state_sha256(after),
            lines=copy.deepcopy(specs),
            groups=copy.deepcopy(groups),
            before=before,
            before_witness=_state_witness(before),
            after_witness=_state_witness(after),
            applied_text=created,
            applied_text_pointer=int(created.as_pointer()),
            applied_text_name=created.name,
            applied_revision=int(settings.preset_revision),
            created_texts=created_texts,
            limitations=copy.deepcopy(plan['limitations']),
        )
        return token
    except BaseException as error:
        _restore_text_protection(old_text, protected)
        failures = []
        try:
            if settings.preset_text is not None:
                settings.preset_text = old_text
        except Exception as cleanup:
            failures.append('preset pointer: ' + str(cleanup))
        try:
            _restore_propgroup(settings, before['settings'])
            author = getattr(scene, 'toon_author_files', None)
            if author is not None and before['author_settings'] is not None:
                _restore_propgroup(author, before['author_settings'])
        except Exception as cleanup:
            failures.append('settings/author restore: ' + str(cleanup))
        try:
            _cleanup_created_texts(_created_preset_texts(before, document, recipe))
        except Exception as cleanup:
            failures.append('created Text cleanup: ' + str(cleanup))
        _clear_preview_caches(scene)
        try:
            restored_sha = _state_sha256(_capture_state(scene))
        except Exception as cleanup:
            restored_sha = None
            failures.append('restored state capture: ' + str(cleanup))
        if failures or restored_sha != plan['state_sha256']:
            detail = '; '.join(failures) if failures else 'digest mismatch'
            raise RuntimeError(
                'CEL lines apply failed and rollback was incomplete: '
                + detail) from error
        raise
    finally:
        if protected is not None:
            _restore_text_protection(old_text, protected)


def _validate_token(token, scene):
    _need(type(token) is dict, 'token must be the runtime object returned by apply')
    _need(token.get('schema') == TOKEN_SCHEMA,
          'unsupported CEL lines rollback token')
    _need(token.get('status') in ('applied', 'rolled_back'),
          'unsupported CEL lines token status')
    _need(token.get('scene') == _scene_ref(scene),
          'token belongs to a different Scene')
    for field in ('state_before_sha256', 'state_after_sha256'):
        _need(type(token.get(field)) is str and len(token[field]) == 64,
              'token ' + field + ' is invalid')
    _need(type(token.get('before')) is dict,
          'token is missing its runtime before-state')
    _need(type(token.get('groups')) is list,
          'token is missing its group witnesses')
    _need(type(token.get('lines')) is list,
          'token is missing its line specifications')


def _restore_before(scene, token):
    before = token['before']
    settings = _validate_scene(scene)
    old_text = before['settings']['pointers'].get(
        'preset_text', {}).get('live')
    try:
        settings.preset_text = old_text
    except Exception:
        pass
    _restore_propgroup(settings, before['settings'])
    author = getattr(scene, 'toon_author_files', None)
    if author is not None and before['author_settings'] is not None:
        _restore_propgroup(author, before['author_settings'])
    removed = _cleanup_created_texts(token['created_texts'])
    _clear_preview_caches(scene)
    return removed


def rollback(scene, token):
    """Restore the complete pre-apply Pencil author state from a token."""
    _validate_scene(scene)
    _validate_token(token, scene)
    settings, _prop = _settings(scene)
    current_sha = _state_sha256(_capture_state(scene))
    if token.get('status') == 'rolled_back':
        _need(current_sha == token['state_before_sha256'],
              'token was already rolled back but the Scene no longer '
              'matches the original state')
        return dict(schema=TOKEN_SCHEMA, status='already_rolled_back',
                    scene=token['scene'],
                    state_sha256=current_sha,
                    drift_detected=False, texts_removed=[])

    drift = current_sha != token['state_after_sha256']
    _need(not drift, 'Pencil author state changed after apply; preserve edits and refuse rollback')
    failures = []
    removed = []
    try:
        removed = _restore_before(scene, token)
    except Exception as error:
        failures.append(str(error))
    try:
        final_sha = _state_sha256(_capture_state(scene))
    except Exception as error:
        final_sha = None
        failures.append('final state capture: ' + str(error))
    if failures or final_sha != token['state_before_sha256']:
        detail = '; '.join(failures) if failures else 'digest mismatch'
        raise RuntimeError(
            'CEL lines rollback did not restore the original Pencil state: '
            + detail)
    token['status'] = 'rolled_back'
    token['rolled_back_state_sha256'] = final_sha
    token['drift_detected'] = bool(drift)
    return dict(schema=TOKEN_SCHEMA, status='rolled_back',
                scene=token['scene'], state_sha256=final_sha,
                drift_detected=bool(drift),
                applied_text_pointer=token.get('applied_text_pointer'),
                texts_removed=removed)


def _safe_selector_groups(document):
    if type(document) is not dict:
        return [], None
    try:
        rows, order = _pencil('selector_bindings').records(document)
        return [copy.deepcopy(rows[key]) for key in rows], copy.deepcopy(order)
    except Exception:
        return [], None


def _live_matches(scene, refs):
    result = []
    for ref in refs:
        matches = []
        for obj in scene.objects:
            library = obj.library.filepath if obj.library is not None else None
            if obj.name == ref['name'] and library == ref['library']:
                matches.append(dict(pointer=int(obj.as_pointer()),
                                    name=obj.name, library=library,
                                    type=obj.type))
        result.append(dict(selector=copy.deepcopy(ref),
                           live_matches=matches,
                           unique=len(matches) == 1))
    return result


def inspect(scene, token):
    """Return actual document, scene controls, and binding identities."""
    _validate_scene(scene)
    _validate_token(token, scene)
    settings, _prop = _settings(scene)
    state = _capture_state(scene)
    current_sha = _state_sha256(state)
    text = settings.preset_text
    wrapper = None
    document = None
    recipe = None
    wrapper_error = None
    if text is not None:
        try:
            _need(text.library is None,
                  'actual preset Text is linked and cannot be inspected')
            body = text.as_string()
            _need(len(body.encode('utf-8')) <= 16 * 1024 * 1024,
                  'actual preset Text exceeds 16 MiB')
            wrapper = json.loads(body)
            _need(type(wrapper) is dict and
                  'document' in wrapper and 'recipe' in wrapper,
                  'actual preset Text lacks document/recipe')
            document = wrapper['document']
            recipe = wrapper['recipe']
        except Exception as error:
            wrapper_error = str(error)
            document = None
            recipe = None

    controls = None
    controls_error = None
    try:
        controls = _pencil('file_authoring').capture_controls(scene)
    except Exception as error:
        controls_error = str(error)

    selector_groups, selector_order = _safe_selector_groups(document)
    binding_sha = _canonical_sha256(selector_groups)
    boundary = {}
    if type(document) is dict:
        boundary = {
            row.get('id'): row
            for row in document.get('request', {}).get(
                'boundary', {}).get('groups', [])
            if type(row) is dict
        }
    control_groups = {}
    if type(controls) is dict:
        control_groups = {
            row.get('name'): row
            for row in controls.get('groups', [])
            if type(row) is dict
        }
    role_by_group = {
        row.get('group_id'): row.get('role')
        for row in token.get('groups', [])
        if type(row) is dict
    }
    group_rows = []
    role_bindings = {}
    for row in selector_groups:
        gid = row.get('group')
        doc_group = boundary.get(gid, {})
        group_row = dict(
            group_id=gid,
            role=role_by_group.get(gid),
            label=doc_group.get('label'),
            priority=doc_group.get('priority'),
            line_set_id=doc_group.get('line_set_id'),
            elements_only=doc_group.get('elements_only'),
            selector=copy.deepcopy(row),
            control=copy.deepcopy(control_groups.get(gid)),
            bindings=_live_matches(scene, row.get('objects', [])),
        )
        group_rows.append(group_row)
        if group_row['role'] is not None:
            role_bindings[group_row['role']] = [
                dict(name=item.get('name'), library=item.get('library'))
                for item in row.get('objects', [])
                if type(item) is dict]

    actual_document_sha = (_canonical_sha256(document)
                           if document is not None else None)
    actual_recipe_sha = (_canonical_sha256(recipe)
                         if recipe is not None else None)
    text_pointer = int(text.as_pointer()) if text is not None else None
    expected_text_pointer = (token.get('applied_text_pointer')
                             if token.get('status') == 'applied' else None)
    applied_text_matches = (
        text_pointer == expected_text_pointer and
        (text is None) == (expected_text_pointer is None))
    return dict(
        schema=INSPECT_SCHEMA,
        status=token.get('status'),
        scene=_scene_ref(scene),
        state_sha256=current_sha,
        scene_controls=controls,
        scene_controls_error=controls_error,
        document=document,
        recipe=recipe,
        document_sha256=actual_document_sha,
        recipe_sha256=actual_recipe_sha,
        plan_document_sha256=token.get('document_sha256'),
        document_matches_plan=(actual_document_sha ==
                               token.get('document_sha256')),
        selector_order=selector_order,
        bindings=dict(
            schema=(document.get('request', {}).get(
                'selector_bindings', {}).get('schema')
                if type(document) is dict else None),
            groups=selector_groups,
            identity_sha256=binding_sha,
            role_objects=role_bindings,
        ),
        group_bindings=group_rows,
        lines=copy.deepcopy(token.get('lines', [])),
        text=dict(
            present=text is not None,
            name=(text.name if text is not None else None),
            pointer=text_pointer,
            library=(text.library.filepath
                     if text is not None and text.library is not None
                     else None),
            users=(int(text.users) if text is not None else None),
            use_fake_user=(bool(text.use_fake_user)
                           if text is not None else None),
            owned=(bool(text.get('toon_viewport_preview_owned'))
                   if text is not None else None),
            reclaimable=(bool(text.get('toon_viewport_preview_reclaimable'))
                         if text is not None else None),
            preset_revision=int(settings.preset_revision),
            curve_revision=int(settings.curve_revision),
            style_mode=settings.style_mode,
            curve_tree=_id_ref(settings.curve_tree),
            author_tree=(_id_ref(getattr(
                getattr(scene, 'toon_author_files', None),
                'author_tree', None))),
            wrapper_error=wrapper_error,
        ),
        token=dict(
            schema=token.get('schema'),
            status=token.get('status'),
            plan_content_sha256=token.get('plan_content_sha256'),
            state_before_sha256=token.get('state_before_sha256'),
            state_after_sha256=token.get('state_after_sha256'),
            applied_text_pointer=token.get('applied_text_pointer'),
            applied_revision=token.get('applied_revision'),
        ),
        text_matches_applied=bool(applied_text_matches),
        state_matches_applied=(current_sha ==
                               token.get('state_after_sha256')
                               if token.get('status') == 'applied' else None),
        limitations=list(token.get('limitations') or ()),
    )
