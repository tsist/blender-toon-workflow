# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable Toon template-library data.

This module is deliberately data-only.  It never imports ``bpy``, creates a
Blender material, or reads/writes any external resource.  Runtime export and
the eventual creation of a shading draft remain the caller's responsibility.
"""
from copy import deepcopy
import hashlib
import json
import uuid

from . import contract, materials


SCHEMA_VERSION = 'toon-template-library-v1'
ENGINES = ('BLENDER_EEVEE', 'CYCLES')
REGION_MODES = ('eevee_cel3', 'eevee_cel3_reflect')

_DOCUMENT_FIELDS = frozenset((
    'schema_version', 'library_id', 'revision', 'name', 'templates',
))
_TEMPLATE_FIELDS = frozenset(('template_id', 'name', 'tags', 'engine', 'settings'))
_EDIT_FIELDS = frozenset(('template_id', 'settings'))
_BINDING_FIELDS = frozenset(('library_template_id', 'target_template_id'))


def _fail(code, message, path):
    raise contract.WorkflowError(code, message, path)


def _closed_object(value, fields, path):
    if not isinstance(value, dict):
        _fail('INVALID_REQUEST', 'Expected object', path)
    extras = set(value) - set(fields)
    if extras:
        _fail(
            'UNSUPPORTED',
            'Unsupported fields: ' + ', '.join(sorted(map(str, extras))),
            path,
        )
    missing = set(fields) - set(value)
    if missing:
        _fail(
            'INVALID_REQUEST',
            'Missing fields: ' + ', '.join(sorted(missing)),
            path,
        )
    return value


def _array(value, minimum, maximum, path):
    if not isinstance(value, list):
        _fail('INVALID_REQUEST', 'Expected array', path)
    if not minimum <= len(value) <= maximum:
        _fail('INVALID_REQUEST', 'Array length out of range', path)
    return value


def _uuid(value, path):
    if not isinstance(value, str):
        _fail('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        _fail('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
    if str(parsed) != value or parsed.int == 0:
        _fail('INVALID_REQUEST', 'Canonical nonzero lower-case UUID required', path)
    return value


def _has_control(value):
    return any(ord(character) < 0x20 or 0x7f <= ord(character) <= 0x9f
               for character in value)


def _name(value, path, maximum_length):
    if not isinstance(value, str):
        _fail('INVALID_REQUEST', 'Nonempty string required', path)
    if (not value.strip() or len(value) > maximum_length or _has_control(value)):
        _fail(
            'INVALID_REQUEST',
            'Nonempty control-free string of at most %d characters required'
            % maximum_length,
            path,
        )
    return value


def _revision(value, path):
    if type(value) is not int or value < 1:
        _fail('INVALID_REQUEST', 'Integer revision >= 1 required', path)
    return value


def _engine(value, path):
    if not isinstance(value, str) or value not in ENGINES:
        _fail('INVALID_REQUEST', 'Engine must be BLENDER_EEVEE or CYCLES', path)
    return value


def _tags(value, path):
    tags = _array(value, 0, 16, path)
    seen = set()
    for index, tag in enumerate(tags):
        location = path + '[' + str(index) + ']'
        _name(tag, location, 64)
        if tag in seen:
            _fail('INVALID_REQUEST', 'Duplicate tag', location)
        seen.add(tag)
    return tags


def _settings(value, engine, path):
    """Validate through the owned materials contract and reject preserve."""
    if isinstance(value, dict) and value.get('mode') == 'preserve':
        _fail(
            'UNSUPPORTED',
            'preserve is not a portable library template mode',
            path + '.mode',
        )
    try:
        normalized = materials._settings(value, engine)
    except contract.WorkflowError as exc:
        if exc.path == 'shading':
            detail_path = path
        elif exc.path.startswith('shading.'):
            detail_path = path + exc.path[len('shading'):]
        elif exc.path == 'engine':
            detail_path = path + '.engine'
        else:
            detail_path = path + '.' + exc.path
        _fail(exc.code, exc.message, detail_path)
    return normalized


def normalize_document(value):
    """Validate the closed v1 document and return an independent deep copy."""
    _closed_object(value, _DOCUMENT_FIELDS, '$')
    version = value['schema_version']
    if not isinstance(version, str) or version != SCHEMA_VERSION:
        _fail(
            'UNSUPPORTED',
            'Unsupported template-library schema_version: ' + repr(version),
            '$.schema_version',
        )
    library_id = _uuid(value['library_id'], '$.library_id')
    _revision(value['revision'], '$.revision')
    _name(value['name'], '$.name', 128)
    templates = _array(value['templates'], 1, 64, '$.templates')

    seen_ids = {library_id}
    for index, entry in enumerate(templates):
        path = '$.templates[' + str(index) + ']'
        _closed_object(entry, _TEMPLATE_FIELDS, path)
        template_id = _uuid(entry['template_id'], path + '.template_id')
        if template_id in seen_ids:
            _fail(
                'INVALID_REQUEST',
                'Template IDs must be distinct from each other and library_id',
                path + '.template_id',
            )
        seen_ids.add(template_id)
        _name(entry['name'], path + '.name', 128)
        engine = _engine(entry['engine'], path + '.engine')
        _tags(entry['tags'], path + '.tags')
        _settings(entry['settings'], engine, path + '.settings')

    return deepcopy(value)


def document_from_entries(library_id, name, revision, entries):
    """Build a portable document from complete, explicit template entries.

    The caller extracts values and identities from real Materials.  Source
    paths and scene bindings are intentionally outside this document.
    """
    _uuid(library_id, '$.library_id')
    _name(name, '$.name', 128)
    _revision(revision, '$.revision')
    _array(entries, 1, 64, '$.templates')
    document = {
        'schema_version': SCHEMA_VERSION,
        'library_id': library_id,
        'revision': revision,
        'name': name,
        'templates': deepcopy(entries),
    }
    return normalize_document(document)


def edit_document(document, expected_revision, edits):
    """Replace complete settings for existing IDs after full preflight."""
    current = normalize_document(document)
    if type(expected_revision) is not int:
        _fail('INVALID_REQUEST', 'Integer expected_revision required',
              '$.expected_revision')
    if expected_revision != current['revision']:
        _fail(
            'CONFLICT',
            'expected_revision does not match the current revision',
            '$.expected_revision',
        )
    edit_rows = _array(edits, 1, 64, '$.edits')

    indices = {entry['template_id']: index
               for index, entry in enumerate(current['templates'])}
    prepared = []
    seen_ids = set()
    for index, edit in enumerate(edit_rows):
        path = '$.edits[' + str(index) + ']'
        _closed_object(edit, _EDIT_FIELDS, path)
        template_id = _uuid(edit['template_id'], path + '.template_id')
        if template_id in seen_ids:
            _fail('INVALID_REQUEST', 'Duplicate edit template_id',
                  path + '.template_id')
        seen_ids.add(template_id)
        if template_id not in indices:
            _fail('INVALID_REQUEST', 'Unknown template_id',
                  path + '.template_id')
        target_index = indices[template_id]
        target = current['templates'][target_index]
        normalized = _settings(
            edit['settings'], target['engine'], path + '.settings'
        )
        if normalized['mode'] != target['settings']['mode']:
            _fail(
                'UNSUPPORTED',
                'Editing cannot replace a template settings mode',
                path + '.settings.mode',
            )
        prepared.append((target_index, deepcopy(edit['settings'])))

    result = deepcopy(current)
    for target_index, settings in prepared:
        result['templates'][target_index]['settings'] = settings
    result['revision'] = current['revision'] + 1
    return normalize_document(result)


def _normalize_shading(shading):
    """Validate the closed existing ``eevee_regions`` shading object."""
    contract.validate(
        shading, contract.REGIONS_SHADING_SCHEMA, '$.shading'
    )
    result = deepcopy(shading)
    template_ids = set()
    for index, template in enumerate(result['templates']):
        path = '$.shading.templates[' + str(index) + ']'
        template_id = template['template_id']
        if template_id in template_ids:
            _fail('INVALID_REQUEST', 'Duplicate shading template_id',
                  path + '.template_id')
        template_ids.add(template_id)
        normalized = _settings(
            template['settings'], 'BLENDER_EEVEE', path + '.settings'
        )
        if normalized['mode'] not in REGION_MODES:
            _fail(
                'UNSUPPORTED',
                'eevee_regions accepts only eevee_cel3 or '
                'eevee_cel3_reflect settings',
                path + '.settings.mode',
            )
    for index, assignment in enumerate(result['assignments']):
        if assignment['template_id'] not in template_ids:
            _fail(
                'INVALID_REQUEST',
                'Assignment template_id is not declared by shading templates',
                '$.shading.assignments[' + str(index) + '].template_id',
            )
    return result


def apply_document(shading, document, bindings, engine):
    """Replace selected settings in an existing EEVEE regions shading draft.

    Only template settings change.  Template IDs and order, assignments,
    ``unassigned`` and every other existing shading field are preserved.
    """
    library = normalize_document(document)
    selected_engine = _engine(engine, '$.engine')
    if selected_engine != 'BLENDER_EEVEE':
        _fail(
            'UNSUPPORTED',
            'eevee_regions requires BLENDER_EEVEE',
            '$.engine',
        )
    current = _normalize_shading(shading)
    binding_rows = _array(bindings, 1, 16, '$.bindings')

    library_by_id = {entry['template_id']: entry
                     for entry in library['templates']}
    target_indices = {entry['template_id']: index
                      for index, entry in enumerate(current['templates'])}
    prepared = []
    seen_targets = set()
    for index, binding in enumerate(binding_rows):
        path = '$.bindings[' + str(index) + ']'
        _closed_object(binding, _BINDING_FIELDS, path)
        source_id = _uuid(
            binding['library_template_id'], path + '.library_template_id'
        )
        target_id = _uuid(
            binding['target_template_id'], path + '.target_template_id'
        )
        if source_id not in library_by_id:
            _fail('INVALID_REQUEST', 'Unknown library_template_id',
                  path + '.library_template_id')
        if target_id not in target_indices:
            _fail('INVALID_REQUEST', 'Unknown target_template_id',
                  path + '.target_template_id')
        if target_id in seen_targets:
            _fail('INVALID_REQUEST', 'Duplicate target_template_id',
                  path + '.target_template_id')
        seen_targets.add(target_id)

        source = library_by_id[source_id]
        if source['engine'] != selected_engine:
            _fail(
                'UNSUPPORTED',
                'Selected library template engine does not match shading engine',
                path + '.library_template_id',
            )
        if source['settings']['mode'] not in REGION_MODES:
            _fail(
                'UNSUPPORTED',
                'eevee_regions cannot consume this template settings mode',
                path + '.library_template_id',
            )
        prepared.append((
            target_indices[target_id],
            deepcopy(source['settings']),
        ))

    result = deepcopy(current)
    for target_index, settings in prepared:
        result['templates'][target_index]['settings'] = settings
    return _normalize_shading(result)


def canonical_document(document):
    """Return canonical compact UTF-8-oriented JSON text for the document."""
    normalized = normalize_document(document)
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _fail(
            'INVALID_REQUEST',
            'Canonical JSON required: ' + str(exc),
            '$',
        )


def document_sha256(document):
    """SHA-256 of the canonical document's UTF-8 bytes."""
    return hashlib.sha256(canonical_document(document).encode('utf-8')).hexdigest()


__all__ = [
    'SCHEMA_VERSION',
    'ENGINES',
    'normalize_document',
    'document_from_entries',
    'edit_document',
    'apply_document',
    'canonical_document',
    'document_sha256',
]
