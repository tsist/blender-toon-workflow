# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, read-only recovery of preserved native brush/source identities.

Audit prototype only. No Blender imports, rendering, linking, welding, unit
conversion, file reads, or interpretation/execution of captured source text.
"""
import copy
import hashlib
import json
import math
import re
from . import wire_sources, edge_sources, ix_sources

SCHEMA = 'toon.viewport-native-bindings.v1'
MAX_NODES = 500_000
MAX_DEPTH = 48
MAX_TEXT_CHARS = 8 * 1024 * 1024
MAX_CONTAINER = 100_000
MAX_BRUSHES = 512
MAX_GROUPS = 128
MAX_ALIASES = 4096
CHAIN_KEYS = ('line_link_length', 'line_split_angle', 'line_direction',
              'min_line_length', 'loop_direction_type')
SHAPE_KEYS = ('brush_type', 'stretch', 'stretch_random', 'angle',
              'angle_random', 'antialiasing')
SLOT_ROUTES = {'brush': 'v_default', 'silhouette_brush': 'v_outline',
               'smoothing_brush': 'v_smooth', 'sharp_brush': 'v_smooth',
               'marked_brush': 'v_selected',
               'wireframe_visible_brush': 'v_wireframe',
               'wireframe_hidden_brush': 'h_wireframe'}
_ACTUAL = re.compile(r'native-brush-[0-9a-f]{64}\Z')


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, path, *, nonempty=False):
    _need(type(value) is dict and (not nonempty or bool(value)), path + ': object required')
    return value


def _array(value, path, limit):
    _need(type(value) is list and len(value) <= limit, path + ': bounded array required')
    return value


def _text(value, path):
    _need(type(value) is str and 0 < len(value) <= 256, path + ': nonempty identifier required')
    return value


def _pointer(*parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def _preflight(value):
    """Bound traversal before copying, serializing, or examining nested input."""
    stack = [(iter((value,)), 0, None)]
    active = set()
    nodes = chars = 0
    while stack:
        iterator, depth, container_id = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if container_id is not None:
                active.remove(container_id)
            continue
        nodes += 1
        _need(nodes <= MAX_NODES and depth <= MAX_DEPTH, 'input node/depth budget exceeded')
        kind = type(item)
        if kind in (dict, list):
            _need(len(item) <= MAX_CONTAINER, 'input container budget exceeded')
            _need(id(item) not in active, 'cyclic input is not JSON')
            active.add(id(item))
            if kind is dict:
                for key in item:
                    _need(type(key) is str, 'JSON object keys must be strings')
                    chars += len(key)
                    _need(chars <= MAX_TEXT_CHARS, 'input text budget exceeded')
                children = iter(item.values())
            else:
                children = iter(item)
            stack.append((children, depth + 1, id(item)))
        elif kind is str:
            chars += len(item)
        elif kind is float:
            _need(math.isfinite(item), 'nonfinite input is not canonical JSON')
        elif kind is int:
            _need(item.bit_length() <= 256, 'input integer budget exceeded')
        else:
            _need(item is None or kind is bool, 'only plain JSON values are accepted')
        _need(chars <= MAX_TEXT_CHARS, 'input text budget exceeded')


def _canonical(value):
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    except (UnicodeError, TypeError, ValueError) as exc:
        raise ValueError('source identity cannot be encoded as canonical UTF-8 JSON') from exc


def _identity(source, name):
    # Same explicit formula as curve_editor._intersection_author.digest.
    return 'native-brush-' + hashlib.sha256(_canonical([source, name])).hexdigest()


def _unique_id_rows(rows, path):
    result = {}
    for row in rows:
        row = _object(row, path)
        uid = _text(row.get('id'), path + '.id')
        _need(uid not in result, path + ': duplicate group ID')
        result[uid] = row
    return result


def recover(document):
    if type(document) is dict and type(document.get('request')) is dict and 'group_lifecycle' in document['request']:
        from . import group_lifecycle
        _preflight(document)
        return group_lifecycle.recover(document, lambda base: _recover_native(base, checked=True))
    return _recover_native(document)


def _recover_native(document, *, checked=False):
    """Recover one complete document, or raise ValueError without partial output.

    Input is the inner ``document`` object, not its persisted-style wrapper.
    ``routes`` contains only the explicit request aliases; enabled source routes
    that have no request alias are never added. Returned values are detached.
    JSON pointers are relative to the input document root.
    """
    if not checked:_preflight(document)
    root = _object(document, '$')
    inputs = _object(root.get('inputs'), '$.inputs')
    full = _object(inputs.get('full_preset'), '$.inputs.full_preset')
    source = _object(full.get('source'), 'full.source', nonempty=True)
    # Validate source identity encoding even if no alias happens to reference it.
    source_hash = hashlib.sha256(_canonical(source)).hexdigest()
    brushes = _object(full.get('brushes'), 'full.brushes', nonempty=True)
    _need(len(brushes) <= MAX_BRUSHES, 'source brush budget exceeded')
    details = _object(full.get('details'), 'full.details', nonempty=True)
    _need(len(details) <= MAX_BRUSHES, 'source detail budget exceeded')
    index = {}
    for name, brush in brushes.items():
        _text(name, 'source brush key')
        brush = _object(brush, 'source brush')
        detail = _text(brush.get('detail'), 'source brush.detail')
        _object(details.get(detail), 'source detail')
        actual = _identity(source, name)
        _need(actual not in index, 'source identity digest is not unique')
        index[actual] = name

    projection = _object(inputs.get('projection'), 'inputs.projection')
    editor = _object(projection.get('editor_document'), 'projection.editor_document')
    editor_rows = _array(editor.get('groups'), 'editor.groups', MAX_GROUPS)
    editor_groups, uid_owner = {}, {}
    for row in editor_rows:
        row = _object(row, 'editor group')
        gid = _text(row.get('uid'), 'editor group.uid')
        _need(gid not in editor_groups, 'duplicate editor group ID')
        editor_groups[gid] = row
        for slot, brush in _object(row.get('brushes'), 'editor group.brushes', nonempty=True).items():
            _text(slot, 'editor slot')
            uid = _text(_object(brush, 'editor brush').get('uid'), 'editor brush.uid')
            _need(uid not in uid_owner, 'duplicate editor brush UID')
            uid_owner[uid] = (gid, slot)

    line_sets = _array(full.get('line_sets'), 'full.line_sets', MAX_GROUPS)
    line_index = {}
    for i, line in enumerate(line_sets):
        line = _object(line, 'line set')
        node = _text(line.get('node'), 'line set.node')
        _text(line.get('line'), 'line set.line')
        _need(node not in line_index, 'duplicate native LineSet node ID')
        line_index[node] = (i, line)
    groups = {}
    used_nodes = set()
    source_aliases = _array(projection.get('source_aliases'), 'projection.source_aliases', MAX_GROUPS)
    for ai, row in enumerate(source_aliases):
        row = _object(row, 'source alias')
        node = _text(row.get('line_set'), 'source alias.line_set')
        _need(node in line_index and node not in used_nodes, 'missing or duplicate source LineSet join')
        uids = _array(row.get('destination_uids'), 'source alias.destination_uids', MAX_ALIASES)
        slots = _array(row.get('destination_brushes'), 'source alias.destination_brushes', MAX_ALIASES)
        _need(bool(uids) and len(uids) == len(slots), 'source alias UID/slot shape mismatch')
        owners = []
        seen_uids = set()
        for uid, slot in zip(uids, slots):
            _text(uid, 'destination UID'); _text(slot, 'destination slot')
            _need(uid not in seen_uids and uid in uid_owner, 'unknown or duplicate destination UID')
            seen_uids.add(uid)
            owner, real_slot = uid_owner[uid]
            _need(real_slot == slot, 'destination UID and editor slot conflict')
            owners.append(owner)
        _need(len(set(owners)) == 1, 'source alias spans multiple editor groups')
        gid = owners[0]
        _need(gid not in groups, 'multiple source aliases join one editor group')
        si, line = line_index[node]
        props = _object(line.get('properties'), 'line set.properties')
        line_id = line.get('lineset_id')
        _need(type(line_id) is int and type(props.get('lineset_id')) is int,
              'native lineset_id must retain integer type')
        _need(line_id == props['lineset_id'], 'native lineset_id fields conflict')
        _need(1 <= line_id <= 8, 'native lineset_id must be in 1..8')
        _need(type(props.get('is_weld_edges')) is bool, 'is_weld_edges must be boolean')
        native_routes = _object(line.get('routes'), 'line set.routes')
        default = _object(native_routes.get('v_default'), 'line set default route')
        alias_source = _text(row.get('source_brush'), 'source alias.source_brush')
        _need(alias_source in brushes and alias_source == default.get('brush'),
              'source alias brush conflicts with native default route')
        groups[gid] = dict(group_id=gid, line_set_node=node, line=line.get('line'),
                           lineset_id=line_id, is_weld_edges=props['is_weld_edges'],
                           native_line_set_pointer=_pointer('inputs', 'full_preset', 'line_sets', si),
                           source_alias_pointer=_pointer('inputs', 'projection', 'source_aliases', ai),
                           destination_uids=list(uids), destination_slots=list(slots),
                           source_default_brush_key=row['source_brush'])
        used_nodes.add(node)
    _need(set(editor_groups) == set(groups), 'editor group lacks unique native LineSet join')

    request = _object(root.get('request'), 'request')
    wire_records = wire_sources.records(root)
    edge_records = edge_sources.records(root)
    ix_records = ix_sources.records(root)
    boundary = _object(request.get('boundary'), 'request.boundary')
    intersection = _object(request.get('intersection'), 'request.intersection')
    boundary_groups = _unique_id_rows(_array(boundary.get('groups'), 'boundary.groups', MAX_GROUPS), 'boundary.groups')
    intersection_groups = _unique_id_rows(_array(intersection.get('groups'), 'intersection.groups', MAX_GROUPS), 'intersection.groups')
    _need(set(boundary_groups) <= set(groups) and set(intersection_groups) <= set(groups),
          'request group lacks recovered native source')
    resolved_rows = _array(projection.get('resolved_routes', []), 'resolved_routes', MAX_ALIASES)
    resolved_index = {}
    for ri, row in enumerate(resolved_rows):
        row = _object(row, 'resolved route')
        key = tuple(_text(row.get(k), 'resolved route.' + k) for k in ('line', 'line_set', 'route'))
        _need(key not in resolved_index, 'duplicate resolved fallback identity')
        resolved_index[key] = (ri, row)

    actuals, routes, seen = {}, [], set()
    for kind, alias_key in (('boundary', 'boundary_aliases'), ('intersection', 'intersection_aliases')):
        for ai, row in enumerate(_array(request.get(alias_key), alias_key, MAX_ALIASES)):
            row = _object(row, alias_key + ' entry')
            expected_keys = {'group', 'brush_id', 'slot'} if kind == 'boundary' else {'group', 'brush_id'}
            _need(set(row) == expected_keys, 'unexpected alias fields')
            gid = _text(row.get('group'), 'alias.group')
            required_groups = boundary_groups if kind == 'boundary' else intersection_groups
            _need(gid in required_groups and gid in groups, 'alias references unknown group')
            actual = row.get('brush_id')
            _need(type(actual) is str and _ACTUAL.fullmatch(actual) is not None and actual in index,
                  'unknown actual brush identity')
            slot = row.get('slot') if kind == 'boundary' else 'intersection_brush'
            if kind == 'boundary':
                _need(type(slot) is str and (slot in SLOT_ROUTES
                      or slot in edge_records.get(gid, {}).get('authors', {})), 'unknown boundary slot')
                _need(slot in boundary_groups[gid] and slot in editor_groups[gid]['brushes'],
                      'boundary alias slot absent from request/editor')
                flag = {'marked_brush': 'marked_enabled', 'silhouette_brush': 'silhouette_enabled',
                        'smoothing_brush': 'smoothing_enabled', 'sharp_brush': 'smoothing_enabled'}.get(slot)
                _need(flag is None or editor_groups[gid].get(flag) is True, 'alias enables disabled editor category')
            unique = (kind, gid, slot)
            _need(unique not in seen, 'duplicate request alias identity')
            seen.add(unique)
            node = groups[gid]['line_set_node']
            si, line = line_index[node]
            autonomous_edge = kind == 'boundary' and slot in edge_records.get(gid, {}).get('authors', {})
            route_name = SLOT_ROUTES.get(slot) if kind == 'boundary' else 'v_intersection'
            autonomous_wire = kind == 'boundary' and slot in wire_sources.SLOTS
            if autonomous_edge:
                descriptor = edge_records[gid]['authors'][slot]
                route_source, route_name, raw_route = edge_sources.resolve_source(full, line,
                    descriptor['channel'], descriptor['category'], descriptor['mode'])
                _need(route_source == descriptor['source_brush'] and slot == edge_sources.slot_for(actual),
                      'Edge author descriptor/actual disagrees')
            elif autonomous_wire:
                _need(gid in wire_records, 'wire alias has no retained explicit author record')
                route_source, raw_route = wire_sources.resolve_source(full, line, wire_sources.SLOTS[slot])
            else:
                raw_route = _object(line['routes'].get(route_name), 'native source route')
                _need(raw_route.get('enabled') is True, 'request alias native route disabled')
                route_source = raw_route.get('brush')
            fallback = None
            if route_source is None:
                _need(raw_route.get('status') == 'DISCONNECTED_SPECIFIC_UNVERIFIED', 'route lacks source brush')
                key = (line.get('line'), node, route_name)
                _need(key in resolved_index, 'route lacks explicit scoped fallback evidence')
                ri, resolved = resolved_index[key]
                reference = _object(resolved.get('reference'), 'fallback reference')
                _need(reference.get('verified') is True, 'fallback reference is unverified')
                route_source = resolved.get('resolved_brush')
                _text(route_source, 'fallback resolved_brush')
                _need(route_source == raw_route.get('default_candidate') and route_source in brushes,
                      'fallback candidate conflicts with native source')
                fallback = dict(pointer=_pointer('inputs', 'projection', 'resolved_routes', ri), evidence=resolved)
            name = index[actual]
            _need(route_source == name, 'actual brush and native route disagree')
            detail_key = brushes[name]['detail']
            if actual not in actuals:
                actuals[actual] = _actual_record(full, actual, name, source_hash)
            route_record = dict(kind=kind, group_id=gid, slot=slot, actual_brush_id=actual,
                               source_brush_key=name, source_detail_key=detail_key,
                               source_brush_pointer=actuals[actual]['source_brush_pointer'],
                               source_detail_pointer=actuals[actual]['source_detail_pointer'],
                               request_alias_pointer=_pointer('request', alias_key, ai),
                               native_route_name=route_name,
                               native_route_pointer=_pointer('inputs', 'full_preset', 'line_sets', si, 'routes', route_name),
                               original_native_route=raw_route, scoped_fallback=fallback)
            if autonomous_wire:
                route_record['autonomous_source_profile'] = wire_sources.PROFILE
            if autonomous_edge:
                route_record['autonomous_source_profile'] = edge_sources.PROFILE
            routes.append(route_record)
    for gid in wire_records:
        _need(all(('boundary', gid, slot) in seen for slot in wire_sources.SLOTS),
              'Retained wire record requires both explicit channel aliases')
    for gid, record in edge_records.items():
        _need(all(('boundary', gid, slot) in seen for slot in record['authors']),
              'Retained edge record requires all explicit author aliases')
        line = line_index[groups[gid]['line_set_node']][1]
        for route in record['routes']:
            if route['category']=='object' and not route['enabled'] and route['slot'] is None:
                continue
            name, _, _ = edge_sources.resolve_source(full, line, route['channel'], route['category'], route['mode'])
            _need(edge_sources.slot_for(_identity(source, name)) == route['slot'],
                  'Edge route and author actual disagree')
    for position, (gid, record) in enumerate(ix_records.items()):
        _need(gid in groups, 'Intersection source group lacks native LineSet identity')
        si, line = line_index[groups[gid]['line_set_node']]
        for slot, author in record['authors'].items():
            actual = author['actual']
            _need(type(actual) is str and _ACTUAL.fullmatch(actual) is not None and actual in index,
                  'Unknown intersection actual identity')
            name, route_name, raw_route = ix_sources.resolve(full, line, author['source_channel'], author['source_mode'])
            _need(name == index[actual] == author['source_brush'], 'Intersection author source and actual disagree')
            if actual not in actuals:
                actuals[actual] = _actual_record(full, actual, name, source_hash)
            routes.append(dict(kind='intersection', group_id=gid, slot=slot, actual_brush_id=actual,
                source_brush_key=name, source_detail_key=actuals[actual]['source_detail_key'],
                source_brush_pointer=actuals[actual]['source_brush_pointer'],
                source_detail_pointer=actuals[actual]['source_detail_pointer'],
                request_alias_pointer=_pointer('request', 'intersection_sources', 'groups', position, 'authors', slot),
                native_route_name=route_name,
                native_route_pointer=_pointer('inputs', 'full_preset', 'line_sets', si, 'routes', route_name),
                original_native_route=raw_route, scoped_fallback=None, autonomous_source_profile=ix_sources.PROFILE))
        for channel, route in record['routes'].items():
            name, _, _ = ix_sources.resolve(full, line, channel, route['mode'])
            _need(ix_sources.slot_for(_identity(source, name)) == route['slot'],
                  'Intersection channel and retained author disagree')
    _need(bool(routes), 'no explicit aliases to recover')
    return copy.deepcopy(dict(schema=SCHEMA, source_identity=source,
        source_identity_pointer='/inputs/full_preset/source', actuals=actuals, routes=routes, groups=groups,
        qualification=dict(native_equivalent=False, chain_rendering_enabled=False,
                           units='native values retained; no angle conversion or link scaling',
                           weld_executed=False, route_scope=('explicit aliases and retained intersection authors'
                               if ix_records else 'explicit request aliases only'))))


def _actual_record(full, actual, name, source_hash):
    detail_key = full['brushes'][name]['detail']
    detail = full['details'][detail_key]
    props = _object(detail.get('properties'), 'detail.properties', nonempty=True)
    for key in CHAIN_KEYS[:-1] + SHAPE_KEYS[1:]:
        _need(type(props.get(key)) in (int, float) and math.isfinite(props[key]),
              'missing or invalid native detail number: ' + key)
    _text(props.get('loop_direction_type'), 'detail.loop_direction_type')
    _need(props.get('brush_type') == 'SIMPLE', 'prototype supports preserved SIMPLE details only')
    curves = {key: _object(detail.get(key), 'detail.' + key) for key in ('size_curve', 'alpha_curve')}
    return dict(actual_brush_id=actual, source_brush_key=name, source_detail_key=detail_key,
        source_identity_sha256=source_hash,
        source_brush_pointer=_pointer('inputs', 'full_preset', 'brushes', name),
        source_detail_pointer=_pointer('inputs', 'full_preset', 'details', detail_key),
        detail_properties=props, curves=curves,
        chain_parameters_native={key: props[key] for key in CHAIN_KEYS},
        simple_shape_native={key: props[key] for key in SHAPE_KEYS})
