# SPDX-License-Identifier: GPL-3.0-or-later
"""Detached explicit author migration. No bpy, file writes or read-time upgrade."""
import copy
import uuid

from . import source_bindings, wire_sources


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def migrate(document, recipe, updates, *, uid_factory=lambda: uuid.uuid4().hex):
    from . import group_lifecycle as G
    if 'group_lifecycle' in document['request']:
        _need(not any(u.get('group') in G.owned(document) for u in updates), '自主组请使用统一边界来源中的原始边线框')
    """Prepare a complete candidate; caller commits document and controls once.

    ``updates`` is a bounded list of exact wire records. New records require an
    explicit enabled action. Disabling an existing record retains both aliases,
    all imported authors and actual identities. Inputs are never mutated.
    """
    from .style import compile_preset
    from .wire_brush_import import import_editor_brush, project_editor_brush
    compile_preset(document, recipe)
    previous = source_bindings.recover(document)
    _need(type(updates) is list and 1 <= len(updates) <= 32, 'Wire author updates require 1..32 records')
    candidate = copy.deepcopy(document)
    request = candidate['request']
    existing = wire_sources.records(candidate)
    groups = {g['id']: g for g in request['boundary']['groups']}
    editor = candidate['inputs']['projection']['editor_document']
    editors = {g['uid']: g for g in editor['groups']}
    projection = candidate['inputs']['projection']
    full = candidate['inputs']['full_preset']
    lines = {g['node']: g for g in full['line_sets']}
    aliases = request['boundary_aliases']
    before_actuals = set(previous['actuals'])
    old_editors = {}
    for row in aliases:
        brush = editors[row['group']]['brushes'][row['slot']]
        comparable = {k: v for k, v in brush.items() if k != 'uid'}
        actual = row['brush_id']
        _need(actual not in old_editors or old_editors[actual] == comparable,
              'Shared actual author values disagree before wire migration')
        old_editors[actual] = comparable
    all_uids = {brush['uid'] for group in editors.values() for brush in group['brushes'].values()}
    seen = set()
    new_routes = []
    for update in updates:
        _need(type(update) is dict and set(update) == {'group', 'enabled', 'visible', 'hidden'},
              'Wire author update fields differ')
        gid = update['group']
        _need(type(gid) is str and gid in groups and gid in previous['groups'] and gid not in seen,
              'Wire author update group is unknown or duplicated')
        seen.add(gid)
        _need(all(type(update[key]) is bool for key in ('enabled', 'visible', 'hidden')),
              'Wire author update gates must be booleans')
        if gid in existing:
            existing[gid] = dict(update)
            continue
        _need(update['enabled'], 'First wire import requires explicit enabled action')
        source = previous['groups'][gid]
        line = lines[source['line_set_node']]
        source_alias = next(row for row in projection['source_aliases'] if row['line_set'] == line['node'])
        for slot, channel in wire_sources.SLOTS.items():
            name, unused = wire_sources.resolve_source(full, line, channel)
            actual = source_bindings._identity(full['source'], name)
            uid = uid_factory()
            _need(type(uid) is str and len(uid) == 32 and all(c in '0123456789abcdef' for c in uid)
                  and uid not in all_uids, 'New wire editor UID is invalid or reused')
            all_uids.add(uid)
            if actual in old_editors:
                author = dict(uid=uid, **copy.deepcopy(old_editors[actual]))
            else:
                _need(actual not in before_actuals,
                      'Existing actual without boundary author requires explicit complete author projection')
                author = import_editor_brush(full, name, uid)
            numeric = project_editor_brush(author)
            _need(slot not in editors[gid]['brushes'] and slot not in groups[gid],
                  'Wire import cannot overwrite an unregistered author slot')
            editors[gid]['brushes'][slot] = author
            groups[gid][slot] = numeric
            aliases.append(dict(group=gid, slot=slot, brush_id=actual))
            source_alias['destination_uids'].append(uid)
            source_alias['destination_brushes'].append(slot)
            if not any(row['group_id'] == gid and row['brush_id'] == actual for row in request['layers']):
                order = max((row['order'] for row in request['layers'] if row['group_id'] == gid), default=-1)+1
                request['layers'].append(dict(group_id=gid, brush_id=actual, order=order,
                    priority=groups[gid]['priority'], color=list(author['color'])+[author['blend_amount']]))
            new_routes.append(dict(group=gid, slot=slot, actual=actual, source_brush=name))
        existing[gid] = dict(update)
    if 'wire_sources' not in request:
        legacy = request.get('profile')
        _need(type(legacy) is str and legacy != wire_sources.PROFILE, 'Legacy request profile missing')
        request['wire_sources'] = dict(schema=wire_sources.SCHEMA, legacy_profile=legacy, groups=[])
    request['profile'] = wire_sources.PROFILE
    request['wire_sources']['groups'] = list(existing.values())
    editor['revision'] += 1
    plan = compile_preset(candidate, recipe)
    bindings = source_bindings.recover(candidate)
    _need(before_actuals <= set(bindings['actuals']), 'Wire migration lost a previous actual identity')
    return candidate, plan, bindings, dict(new_routes=new_routes,
        new_actuals=sorted(set(bindings['actuals'])-before_actuals),
        retained_actuals=sorted(before_actuals), native_flags_changed=False,
        source_profile=wire_sources.PROFILE)
