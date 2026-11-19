# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit autonomous wire records; preserved native flags are not authority.

Reading never imports brushes, creates routes or prunes retained author state.
Only the separate author action may add an extension to a legacy document.
"""
SCHEMA = 'toon.viewport-wire-sources.v1'
PROFILE = 'autonomous_wire_groups.v1'
SLOTS = {'wireframe_visible_brush': 'v', 'wireframe_hidden_brush': 'h'}


def _need(value, message):
    if not value:
        raise ValueError(message)


def records(document):
    request = document['request']
    if 'wire_sources' not in request:
        _need(request.get('profile') != PROFILE, 'Wire profile requires versioned wire_sources')
        return {}
    extension = request['wire_sources']
    _need(type(extension) is dict and set(extension) == {'schema', 'legacy_profile', 'groups'},
          'Wire source extension fields differ')
    _need(extension['schema'] == SCHEMA and type(extension['schema']) is str,
          'Unsupported wire source version')
    _need(request.get('profile') == PROFILE, 'Wire source extension requires its explicit profile')
    legacy = extension['legacy_profile']
    _need(type(legacy) is str and 0 < len(legacy) <= 256 and legacy != PROFILE,
          'Wire source legacy profile must be retained')
    groups = extension['groups']
    _need(type(groups) is list and 1 <= len(groups) <= 32, 'Wire source groups require 1..32 records')
    known = {g['id'] for g in request['boundary']['groups']}
    result = {}
    for row in groups:
        _need(type(row) is dict and set(row) == {'group', 'enabled', 'visible', 'hidden'},
              'Wire source record fields differ')
        gid = row['group']
        _need(type(gid) is str and gid in known and gid not in result,
              'Wire source group is unknown or duplicated')
        _need(all(type(row[key]) is bool for key in ('enabled', 'visible', 'hidden')),
              'Wire source gates must be booleans')
        result[gid] = dict(row)
    return result


def resolve_source(full, line, channel):
    """Resolve the captured default or an active connected specific brush.

    Default import remains an autonomous opt-in when native wire is off.
    A specific import requires its captured category/socket to be active;
    disconnected specifics never silently fall back to the default brush.
    """
    _need(channel in ('v', 'h'), 'Unknown wire source channel')
    props = line['properties']
    route = line['routes'][channel+'_wireframe']
    _need(type(route) is dict and type(route.get('specific_on')) is bool,
          'Captured wire route lacks a boolean specific flag')
    specific = route['specific_on']
    _need(props.get(channel+'_wireframe_specific_on') is specific,
          'Wire specific flag conflicts with captured properties')
    base = line['routes'][channel+'_default']
    _need(route.get('default_candidate') == base.get('brush'),
          'Wire captured default candidate disagrees')
    socket = channel+'_brush'
    if specific:
        socket = channel+'_wireframe_specific'
        _need(route.get('enabled') is True and props.get(channel+'_wireframe_on') is True
              and props.get(channel+'_wireframe_brush_settings') == socket,
              'Wire specific import requires an active captured category/socket')
        name = route.get('connected_brush')
        _need(type(name) is str and name in full['brushes']
              and route.get('brush') == name and route.get('status') == 'CONNECTED_SPECIFIC',
              'Wire specific brush is disconnected or conflicts; no implicit default fallback')
    else:
        name = base.get('brush')
        _need(type(name) is str and name in full['brushes'], 'Wire channel base brush missing')
        _need(route.get('brush') == name, 'Wire default route conflicts with captured base')
    tree = full['raw_tree']
    links = [link for link in tree['links'] if link.get('to_node') == line['node']
             and link.get('to_socket') == socket]
    _need(len(links) == 1 and links[0].get('is_valid') is True
          and links[0].get('is_muted') is False and links[0].get('from_node') == name,
          'Wire source raw link is absent, ambiguous, invalid or muted')
    nodes = {node['name']: node for node in tree['nodes']}
    _need(name in nodes and line['node'] in nodes, 'Wire raw source nodes missing')
    if specific:
        target = nodes[line['node']]
        sockets = [row for row in target['inputs'] if row.get('identifier') == socket]
        _need(target.get('type') == 'Pencil4LineSetNodeType'
              and nodes[name].get('type') == 'Pencil4BrushSettingsNodeType',
              'Wire specific source has an unsupported raw node type')
        _need(len(sockets) == 1 and sockets[0].get('enabled') is True,
              'Wire specific raw socket is missing or disabled')
        _need(any(row.get('identifier') == links[0].get('from_socket')
                  and row.get('enabled') is True for row in nodes[name]['outputs']),
              'Wire specific raw output socket is missing or disabled')
        _need(target['properties'].get(channel+'_wireframe_on') is True
              and target['properties'].get(channel+'_wireframe_specific_on') is True
              and target['properties'].get(channel+'_wireframe_brush_settings') == socket,
              'Wire specific raw properties disagree with captured route')
    _need(nodes[name]['properties'].get('mute') is False
          and nodes[line['node']]['properties'].get('mute') is False,
          'Wire raw source node is muted')
    brush = full['brushes'][name]
    detail = full['details'].get(brush['detail'])
    _need(type(detail) is dict and detail['properties'].get('brush_type') == 'SIMPLE',
          'Wire import requires captured SIMPLE detail')
    return name, route


def attach_plan(document, groups):
    """Validate opt-in format and attach detached per-group gates to a new plan."""
    rows = records(document)
    if not rows:
        _need(not any(slot in group['brushes'] for group in groups for slot in SLOTS),
              'Wire author brushes require a retained versioned source record')
        return False
    by_group = {g['id']: g for g in groups}
    _need(all(group['id'] in rows for group in groups if any(slot in group['brushes'] for slot in SLOTS)),
          'Wire author brushes have no group source record')
    for gid, record in rows.items():
        group = by_group[gid]
        _need(all(slot in group['brushes'] for slot in SLOTS),
              'Retained wire profile requires both channel author brushes')
        group['wire_sources'] = dict(record)
    return any(row['enabled'] for row in rows.values())
