# SPDX-License-Identifier: GPL-3.0-or-later
"""Detached boundary source migration. Existing actual authors are never reset."""
import copy
import uuid

from . import edge_sources as sources, source_bindings


def _need(value, message):
    if not value:
        raise ValueError(message)


def migrate(document, recipe, updates, *, uid_factory=lambda: uuid.uuid4().hex):
    _need(type(updates) is list and 1 <= len(updates) <= 32 and all(type(u) is dict for u in updates), 'Edge updates require 1..32 groups')
    from . import group_lifecycle as G
    if 'group_lifecycle' in document['request'] and any(u.get('group') in G.owned(document) for u in updates):
        return G.migrate_edges(document, recipe, updates)
    from .style import compile_preset
    from .wire_brush_import import import_editor_brush, project_editor_brush
    compile_preset(document, recipe)
    previous = source_bindings.recover(document)
    candidate = copy.deepcopy(document)
    request = candidate['request']
    records = sources.records(candidate)
    for row in records.values():
        if len(row['routes'])==12:
            row['routes'].extend(dict(category='object',channel=ch,enabled=False,mode='DEFAULT',slot=None,
                include_open=False,min_angle=30.0,max_angle=180.0) for ch in sources.CHANNELS)
    groups = {g['id']: g for g in request['boundary']['groups']}
    projection = candidate['inputs']['projection']
    editor = projection['editor_document']
    editors = {g['uid']: g for g in editor['groups']}
    full = candidate['inputs']['full_preset']
    lines = {g['node']: g for g in full['line_sets']}
    old_authors = {}
    aliases = request['boundary_aliases']
    for alias in aliases:
        author = editors[alias['group']]['brushes'][alias['slot']]
        comparable = {k: v for k, v in author.items() if k != 'uid'}
        actual = alias['brush_id']
        _need(actual not in old_authors or old_authors[actual] == comparable,
              'Shared actual author values disagree before edge migration')
        old_authors[actual] = comparable
    all_uids = {b['uid'] for g in editors.values() for b in g['brushes'].values()}
    seen = set()
    imported = []
    for update in updates:
        _need(type(update) is dict and {'group', 'enabled', 'routes'} <= set(update)
              <= {'group', 'enabled', 'routes', 'visible_enabled', 'hidden_enabled'},
              'Edge update fields differ')
        gid = update['group']
        _need(type(gid) is str and gid in groups and gid in previous['groups'] and gid not in seen,
              'Edge update group is unknown or repeated')
        seen.add(gid)
        for gate in ('visible_enabled', 'hidden_enabled'):
            if gate in update:
                _need(type(update[gate]) is bool, 'Group channel gate must be boolean')
                groups[gid][gate] = editors[gid][gate] = update[gate]
        _need(type(update['enabled']) is bool and type(update['routes']) is list
              and len(update['routes']) <= 14, 'Invalid edge update gates/routes')
        row = records.get(gid)
        if row is None:
            _need(update['enabled'], 'First edge import requires explicit enabled action')
            row = dict(group=gid, enabled=True, authors={}, routes=[
                dict(category=cat, channel=ch, enabled=cat == 'outline', mode='DEFAULT', slot='',
                     include_open=cat!='object', min_angle=30.0, max_angle=180.0)
                for cat in sources.CATEGORIES for ch in sources.CHANNELS])
        by_pair = {(r['category'], r['channel']): r for r in row['routes']}
        pairs = set()
        for change in update['routes']:
            _need(type(change) is dict and {'category', 'channel'} <= set(change)
                  <= {'category', 'channel', 'enabled', 'mode', 'include_open', 'min_angle', 'max_angle'},
                  'Invalid edge route update fields')
            pair = change['category'], change['channel']
            _need(pair in by_pair and pair not in pairs, 'Unknown or repeated edge route update')
            pairs.add(pair)
            by_pair[pair].update(copy.deepcopy(change))
        line = lines[previous['groups'][gid]['line_set_node']]
        source_alias = next(a for a in projection['source_aliases'] if a['line_set'] == line['node'])
        for route in row['routes']:
            if route['category']=='object' and not route['enabled'] and not route['slot']:
                route['slot']=None
                continue
            name, _, _ = sources.resolve_source(full, line, route['channel'], route['category'], route['mode'])
            actual = source_bindings._identity(full['source'], name)
            slot = sources.slot_for(actual)
            route['slot'] = slot
            if slot in row['authors']:
                continue
            _need(slot not in groups[gid] and slot not in editors[gid]['brushes'],
                  'Edge import cannot overwrite an unregistered slot')
            uid = uid_factory()
            _need(type(uid) is str and len(uid) == 32 and all(c in '0123456789abcdef' for c in uid)
                  and uid not in all_uids, 'New edge author UID invalid or reused')
            all_uids.add(uid)
            if actual in old_authors:
                author = dict(uid=uid, **copy.deepcopy(old_authors[actual]))
            else:
                _need(actual not in previous['actuals'],
                      'Existing actual without boundary author requires complete author projection')
                author = import_editor_brush(full, name, uid)
                old_authors[actual] = {k: copy.deepcopy(v) for k, v in author.items() if k != 'uid'}
            editors[gid]['brushes'][slot] = author
            groups[gid][slot] = project_editor_brush(author)
            aliases.append(dict(group=gid, slot=slot, brush_id=actual))
            source_alias['destination_uids'].append(uid)
            source_alias['destination_brushes'].append(slot)
            row['authors'][slot] = dict(source_brush=name, channel=route['channel'],
                                        mode=route['mode'], category=route['category'])
            if not any(r['group_id'] == gid and r['brush_id'] == actual for r in request['layers']):
                order = max((r['order'] for r in request['layers'] if r['group_id'] == gid), default=-1)+1
                request['layers'].append(dict(group_id=gid, brush_id=actual, order=order,
                    priority=groups[gid]['priority'], color=list(author['color'])+[author['blend_amount']]))
            imported.append(dict(group=gid, slot=slot, actual=actual, source_brush=name))
        row['enabled'] = update['enabled']
        records[gid] = row
    request['edge_sources'] = dict(schema=sources.SCHEMA, groups=list(records.values()))
    editor['revision'] += 1
    plan = compile_preset(candidate, recipe)
    bindings = source_bindings.recover(candidate)
    _need(set(previous['actuals']) <= set(bindings['actuals']), 'Edge migration lost actual identities')
    return candidate, plan, bindings, dict(new_routes=imported,
        new_actuals=sorted(set(bindings['actuals'])-set(previous['actuals'])),
        retained_actuals=sorted(previous['actuals']), native_flags_changed=False, source_profile=sources.PROFILE)
