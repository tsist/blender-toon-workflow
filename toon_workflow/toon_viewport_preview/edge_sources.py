# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned, explicit autonomous boundary sources and preserved author proof."""
import copy
import math
import re

LEGACY_SCHEMA = 'toon.viewport-edge-sources.v1'
SCHEMA = 'toon.viewport-edge-sources.v2'
PROFILE = 'autonomous_edge_groups.v1'
LEGACY_CATEGORIES = ('outline', 'smooth', 'material', 'selected', 'normal_angle', 'wireframe')
CATEGORIES = LEGACY_CATEGORIES+('object',)
CHANNELS = ('VISIBLE', 'HIDDEN')
_SLOT = re.compile(r'edge_[0-9a-f]{64}_brush\Z')


def _need(value, message):
    if not value:
        raise ValueError(message)


def slot_for(actual):
    _need(type(actual) is str and re.fullmatch(r'(native|authored)-brush-[0-9a-f]{64}', actual) is not None,
          'Edge author requires a canonical actual identity')
    return 'edge_' + actual.rsplit('-', 1)[1] + '_brush'


def records(document):
    if 'edge_sources' not in document['request']:
        return {}
    ext = document['request']['edge_sources']
    _need(type(ext) is dict and set(ext) == {'schema', 'groups'} and ext['schema'] in (SCHEMA,LEGACY_SCHEMA),
          'Unsupported edge source extension')
    categories=CATEGORIES if ext['schema']==SCHEMA else LEGACY_CATEGORIES
    rows = ext['groups']
    _need(type(rows) is list and 1 <= len(rows) <= 32, 'Edge source groups require 1..32 records')
    known = {g['id'] for g in document['request']['boundary']['groups']}
    result = {}
    for row in rows:
        _need(type(row) is dict and set(row) == {'group', 'enabled', 'authors', 'routes'},
              'Edge source record fields differ')
        gid = row['group']
        _need(type(gid) is str and gid in known and gid not in result, 'Unknown or duplicate edge group')
        _need(type(row['enabled']) is bool, 'Edge enabled must be boolean')
        authors = row['authors']
        _need(type(authors) is dict and 1 <= len(authors) <= 512, 'Edge author table is invalid')
        for slot, author in authors.items():
            _need(type(slot) is str and _SLOT.fullmatch(slot) is not None, 'Invalid edge author slot')
            _need(type(author) is dict and set(author) == {'source_brush', 'channel', 'mode', 'category'},
                  'Edge author descriptor fields differ')
            _need(type(author['source_brush']) is str and bool(author['source_brush'])
                  and author['channel'] in CHANNELS and author['mode'] in ('DEFAULT', 'SPECIFIC')
                  and author['category'] in categories, 'Invalid edge author descriptor')
        _need(type(row['routes']) is list and len(row['routes']) == 2*len(categories), 'Edge profile route count differs')
        pairs = set()
        for route in row['routes']:
            _need(type(route) is dict and set(route) == {'category', 'channel', 'enabled', 'mode',
                  'slot', 'include_open', 'min_angle', 'max_angle'}, 'Edge route fields differ')
            pair = route['category'], route['channel']
            _need(pair[0] in categories and pair[1] in CHANNELS and pair not in pairs,
                  'Invalid or duplicate edge category/channel')
            pairs.add(pair)
            _need(type(route['enabled']) is bool and type(route['include_open']) is bool,
                  'Edge route gates must be boolean')
            unbound=pair[0]=='object' and not route['enabled'] and route['slot'] is None
            _need(route['mode'] in ('DEFAULT', 'SPECIFIC') and (unbound or route['slot'] in authors),
                  'Edge route has no author')
            _need(all(type(route[k]) in (int, float) and math.isfinite(route[k])
                      for k in ('min_angle', 'max_angle'))
                  and 0 <= route['min_angle'] <= route['max_angle'] <= 180,
                  'Normal angle range must be finite degrees in 0..180')
        result[gid] = copy.deepcopy(row)
    return result


def resolve_source(full, line, channel, category, mode):
    _need(category in CATEGORIES, 'Invalid boundary source category')
    return resolve_captured_source(full, line, channel, category, mode)


def resolve_captured_source(full, line, channel, category, mode):
    """Choose an explicit base or a captured active, directly linked SIMPLE brush.

    DEFAULT is an autonomous choice even when native specific was enabled.
    SPECIFIC never invents a fallback for a disconnected captured socket.
    """
    _need(channel in CHANNELS and category in CATEGORIES+('intersection',) and mode in ('DEFAULT', 'SPECIFIC'),
          'Invalid edge source choice')
    ch = 'v' if channel == 'VISIBLE' else 'h'
    route_name = ch + '_' + (category if mode == 'SPECIFIC' else 'default')
    route = line['routes'].get(route_name)
    _need(type(route) is dict, 'Captured edge route is missing: ' + route_name)
    socket = ch + '_brush' if mode == 'DEFAULT' else ch + '_' + category + '_specific'
    name = route.get('brush')
    if mode == 'SPECIFIC':
        props = line['properties']
        _need(route.get('enabled') is True and route.get('specific_on') is True
              and props.get(ch+'_'+category+'_on') is True
              and props.get(ch+'_'+category+'_specific_on') is True
              and props.get(ch+'_'+category+'_brush_settings') == socket
              and route.get('status') == 'CONNECTED_SPECIFIC'
              and route.get('connected_brush') == name,
              'Specific edge source requires an active captured connection; no implicit fallback')
    _need(type(name) is str and name in full['brushes'], 'Captured edge brush is missing')
    tree = full['raw_tree']
    links = [l for l in tree['links'] if l.get('to_node') == line['node'] and l.get('to_socket') == socket]
    _need(len(links) == 1 and links[0].get('is_valid') is True and links[0].get('is_muted') is False
          and links[0].get('from_node') == name, 'Edge source raw link is invalid, muted or ambiguous')
    nodes = {n['name']: n for n in tree['nodes']}
    source, target = nodes.get(name), nodes.get(line['node'])
    _need(source is not None and target is not None
          and source.get('type') == 'Pencil4BrushSettingsNodeType'
          and target.get('type') == 'Pencil4LineSetNodeType'
          and source['properties'].get('mute') is False and target['properties'].get('mute') is False,
          'Unsupported or muted raw edge source nodes')
    _need(any(s.get('identifier') == links[0].get('from_socket') and s.get('enabled') is True
              for s in source['outputs'])
          and sum(s.get('identifier') == socket and s.get('enabled') is True for s in target['inputs']) == 1,
          'Edge source raw sockets are disabled or missing')
    if mode == 'SPECIFIC':
        props = target['properties']
        _need(props.get(ch+'_'+category+'_on') is True and props.get(ch+'_'+category+'_specific_on') is True
              and props.get(ch+'_'+category+'_brush_settings') == socket,
              'Raw specific edge source flags disagree')
    detail = full['details'].get(full['brushes'][name]['detail'])
    _need(type(detail) is dict and detail['properties'].get('brush_type') == 'SIMPLE',
          'Edge source import currently requires captured SIMPLE detail')
    return name, route_name, route


def attach_plan(document, groups):
    rows = records(document)
    for group in groups:
        row = rows.get(group['id'])
        slots = set(row['authors']) if row else set()
        _need({s for s in group['brushes'] if _SLOT.fullmatch(s)} == slots,
              'Edge author slots require their retained source descriptors')
        if row:
            group['edge_sources'] = row
    return any(row['enabled'] for row in rows.values())


def predicates(mesh, view, perspective):
    """Detach independent predicates; legacy single-valued kinds cannot supply them."""
    import numpy as np
    return dict(silhouette=np.array(mesh.silhouettes(view, perspective), dtype=bool, copy=True),
                smoothing=np.array(mesh.smoothing, dtype=bool, copy=True),
                material=np.array(mesh.material_boundary, dtype=bool, copy=True),
                selected=np.array(mesh.marks, dtype=bool, copy=True),
                normal_angle=np.rad2deg(np.arccos(mesh.normal_dot)))


def object_active(group):
    row=group.get('edge_sources')
    return row is not None and row['enabled'] and any(r['enabled'] and
        (r['category']=='object' or r['category']=='outline' and group.get('object_partition',False)) for r in row['routes'])
