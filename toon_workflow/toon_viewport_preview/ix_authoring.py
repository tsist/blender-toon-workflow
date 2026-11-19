# SPDX-License-Identifier: GPL-3.0-or-later
"""Detached intersection author migration, retaining current edits by actual identity."""
import copy
import uuid
from . import ix_sources, source_bindings


def _need(value, message):
    if not value:
        raise ValueError(message)


def migrate(wrapped, updates):
    _need(type(updates) is list and 1 <= len(updates) <= 32 and all(type(u) is dict for u in updates), 'Intersection updates require 1..32 groups')
    from . import group_lifecycle as G
    if 'group_lifecycle' in wrapped['document']['request'] and any(u.get('group') in G.owned(wrapped['document']) for u in updates):
        return G.migrate_ix(wrapped, updates)
    from .style import compile_preset, size_scale
    from .wire_brush_import import import_editor_brush, project_editor_brush
    from .curve_editor import _intersection_author
    document, recipe = wrapped['document'], wrapped['recipe']
    old_plan = compile_preset(document, recipe)
    before = source_bindings.recover(document)
    result = copy.deepcopy(wrapped)
    candidate = result['document']
    request = candidate['request']
    rows = ix_sources.records(candidate)
    groups = {g['id']: g for g in old_plan['groups']}
    full = candidate['inputs']['full_preset']
    lines = {g['node']: g for g in full['line_sets']}
    editors = {g['uid']: g for g in candidate['inputs']['projection']['editor_document']['groups']}
    boundary_authors = {}
    for alias in request['boundary_aliases']:
        brush = editors[alias['group']]['brushes'][alias['slot']]
        comparable = {k: v for k, v in brush.items() if k != 'uid'}
        actual = alias['brush_id']
        _need(actual not in boundary_authors or boundary_authors[actual] == comparable,
              'Shared boundary actual authors disagree before intersection migration')
        boundary_authors[actual] = comparable
    # Existing editable IX curves are read from the current wrapper, never reset
    # to captured native samples when an actual first joins this channel profile.
    legacy = {g['id']: g for g in request['intersection']['groups']}
    legacy_aliases = {a['group']: a['brush_id'] for a in request['intersection_aliases']}
    existing_by_actual = {}
    for record in rows.values():
        for author in record['authors'].values():
            existing_by_actual.setdefault(author['actual'], author)
    seen = set()
    imported = []
    for update in updates:
        _need(type(update) is dict and set(update) == {'group', 'enabled', 'routes'},
              'Intersection update fields differ')
        gid = update['group']
        _need(type(gid) is str and gid in groups and gid in before['groups'] and gid not in seen,
              'Unknown or repeated intersection update group')
        seen.add(gid)
        _need(type(update['enabled']) is bool and type(update['routes']) is dict
              and set(update['routes']) <= set(ix_sources.CHANNELS), 'Invalid intersection update gates')
        row = rows.get(gid)
        if row is None:
            _need(update['enabled'], 'First intersection import requires explicit enabled action')
            old = legacy.get(gid, {})
            row = dict(group=gid, enabled=True, authors={}, routes={ch: dict(
                enabled=old.get('visible_enabled' if ch == 'VISIBLE' else 'hidden_enabled', True),
                mode='DEFAULT', slot='') for ch in ix_sources.CHANNELS})
        for channel, change in update['routes'].items():
            _need(type(change) is dict and set(change) <= {'enabled', 'mode'}, 'Invalid intersection route update')
            row['routes'][channel].update(copy.deepcopy(change))
        line = lines[before['groups'][gid]['line_set_node']]
        group = groups[gid]
        reference_units = size_scale(group['size_mode'], candidate['inputs']['width'], candidate['inputs']['height'],
                                     old_plan['units'], group['initial_scale_ex'])
        _need(reference_units > 0, 'Positive intersection reference size required')
        for channel, route in row['routes'].items():
            _need(type(route['enabled']) is bool, 'Intersection channel enabled must be boolean')
            name, _, _ = ix_sources.resolve(full, line, channel, route['mode'])
            actual = source_bindings._identity(full['source'], name)
            slot = ix_sources.slot_for(actual)
            route['slot'] = slot
            if slot in row['authors']:
                continue
            # A legacy IX numeric author is the authoritative current width/maps.
            own_legacy = gid if legacy_aliases.get(gid) == actual else None
            legacy_gid = own_legacy or next((g for g, a in legacy_aliases.items() if a == actual), None)
            if legacy_gid is not None:
                brush = copy.deepcopy(legacy[legacy_gid]['brush'])
                curves = _intersection_author(old_plan, legacy_gid, wrapped)
                if legacy_gid != gid:
                    other = groups.get(legacy_gid)
                    if other is None:
                        raw_group=next(g for g in request['boundary']['groups'] if g['id']==legacy_gid)
                        other={key:raw_group.get(key) for key in ('size_mode','initial_scale_ex')}
                    other_units = size_scale(other['size_mode'], candidate['inputs']['width'], candidate['inputs']['height'],
                                             old_plan['units'], other['initial_scale_ex'])
                    _need(other_units > 0, 'Positive source intersection reference size required')
                    brush['width_px'] *= reference_units / other_units
            else:
                existing = existing_by_actual.get(actual)
                if actual in boundary_authors:
                    editor = dict(uid=uuid.uuid4().hex, **copy.deepcopy(boundary_authors[actual]))
                    numeric = project_editor_brush(editor)
                    brush = {k: v for k, v in numeric.items() if k != 'native_size'}
                    brush['width_px'] = editor['native_size']*reference_units*group['coverage_extent_factor']
                    curves = {k: copy.deepcopy(editor[k]) for k in ('size_curve', 'alpha_curve')}
                elif existing is not None:
                    brush = copy.deepcopy(existing['brush'])
                    brush['width_px'] *= reference_units / existing['reference_units']
                    curves = copy.deepcopy(existing['curves'])
                else:
                    _need(actual not in before['actuals'], 'Existing actual has no complete current intersection author')
                    editor = import_editor_brush(full, name, uuid.uuid4().hex)
                    numeric = project_editor_brush(editor)
                    brush = {k: v for k, v in numeric.items() if k != 'native_size'}
                    brush['width_px'] = editor['native_size']*reference_units*group['coverage_extent_factor']
                    curves = {k: copy.deepcopy(editor[k]) for k in ('size_curve', 'alpha_curve')}
            author = dict(actual=actual, source_brush=name, source_channel=channel,
                          source_mode=route['mode'], brush=brush, curves=copy.deepcopy(curves),
                          reference_units=reference_units)
            ix_sources.check_author(author)
            row['authors'][slot] = author
            imported.append(dict(group=gid, slot=slot, actual=actual, source_brush=name))
            if not any(r['group_id'] == gid and r['brush_id'] == actual for r in request['layers']):
                order = max((r['order'] for r in request['layers'] if r['group_id'] == gid), default=-1)+1
                request['layers'].append(dict(group_id=gid, brush_id=actual, order=order,
                    priority=group['priority'], color=list(brush['color'])+[brush['blend_amount']]))
        row['enabled'] = update['enabled']
        rows[gid] = row
    request['intersection_sources'] = dict(schema=ix_sources.SCHEMA, groups=list(rows.values()))
    plan = compile_preset(candidate, recipe)
    bindings = source_bindings.recover(candidate)
    _need(set(before['actuals']) <= set(bindings['actuals']), 'Intersection migration lost actual identities')
    return result, plan, bindings, dict(source_profile=ix_sources.PROFILE, new_routes=imported,
        new_actuals=sorted(set(bindings['actuals'])-set(before['actuals'])), legacy_authors_preserved=True)
