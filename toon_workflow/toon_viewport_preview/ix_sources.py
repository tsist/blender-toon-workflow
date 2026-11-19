# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned, retained V/H intersection authors; geometry stays in the existing producer."""
import copy
import re
from . import edge_sources

SCHEMA = 'toon.viewport-intersection-sources.v1'
PROFILE = 'autonomous_intersection_channels.v1'
CHANNELS = ('VISIBLE', 'HIDDEN')
_SLOT = re.compile(r'ix_[0-9a-f]{64}_brush\Z')
BRUSH_FIELDS = {'width_px', 'blend_amount', 'color', 'width_map', 'alpha_map', 'size_curve', 'alpha_curve'}


def _need(value, message):
    if not value:
        raise ValueError(message)


def slot_for(actual):
    return 'ix_' + edge_sources.slot_for(actual)[5:]


def check_author(author):
    from .style import _number, _color, _validate_mapping
    from ._support.paint_editor_state import _curve
    _need(type(author) is dict and set(author) == {'actual', 'source_brush', 'source_channel',
          'source_mode', 'brush', 'curves', 'reference_units'}, 'Intersection author fields differ')
    _need(type(author['source_brush']) is str and bool(author['source_brush'])
          and author['source_channel'] in CHANNELS and author['source_mode'] in ('DEFAULT', 'SPECIFIC'),
          'Invalid intersection source descriptor')
    brush = author['brush']
    _need(type(brush) is dict and set(brush) == BRUSH_FIELDS, 'Intersection numeric brush fields differ')
    _number(brush['width_px'], 'intersection author reference width', 0)
    _need(_number(author['reference_units'], 'intersection reference units', 0) > 0,
          'Positive intersection reference units required')
    _number(brush['blend_amount'], 'intersection author blend', 0, 1)
    _color(brush['color'], 'intersection author color')
    for key in ('width_map', 'alpha_map'):
        if brush[key] is not None:
            _validate_mapping(brush[key], 'intersection author '+key)
    curves = author['curves']
    _need(type(curves) is dict and set(curves) == {'size_curve', 'alpha_curve'}, 'Intersection curve authors missing')
    for key, curve in curves.items():
        _curve(curve)
        _need((curve['samples'] if curve['enabled'] else None) == brush[key],
              'Intersection curve author and numeric samples disagree')


def records(document):
    request = document['request']
    if 'intersection_sources' not in request:
        return {}
    ext = request['intersection_sources']
    _need(type(ext) is dict and set(ext) == {'schema', 'groups'} and ext['schema'] == SCHEMA,
          'Unsupported intersection source version')
    rows = ext['groups']
    _need(type(rows) is list and 1 <= len(rows) <= 32, 'Intersection sources require 1..32 groups')
    known = {g['id'] for g in request['boundary']['groups']}
    result = {}
    for row in rows:
        _need(type(row) is dict and set(row) == {'group', 'enabled', 'authors', 'routes'},
              'Intersection source record fields differ')
        gid = row['group']
        _need(type(gid) is str and gid in known and gid not in result, 'Unknown or duplicate intersection group')
        _need(type(row['enabled']) is bool, 'Intersection profile enabled must be boolean')
        authors = row['authors']
        _need(type(authors) is dict and 1 <= len(authors) <= 512, 'Invalid intersection author table')
        for slot, author in authors.items():
            _need(type(slot) is str and _SLOT.fullmatch(slot) is not None, 'Invalid intersection author slot')
            check_author(author)
            _need(slot_for(author['actual']) == slot, 'Intersection slot and actual disagree')
        _need(type(row['routes']) is dict and set(row['routes']) == set(CHANNELS),
              'Intersection sources require both channel routes')
        for route in row['routes'].values():
            _need(type(route) is dict and set(route) == {'enabled', 'mode', 'slot'}
                  and type(route['enabled']) is bool and route['mode'] in ('DEFAULT', 'SPECIFIC')
                  and route['slot'] in authors, 'Invalid intersection channel route')
        result[gid] = copy.deepcopy(row)
    return result


def resolve(full, line, channel, mode):
    return edge_sources.resolve_captured_source(full, line, channel, 'intersection', mode)


def attach_plan(document, groups):
    rows = records(document)
    for group in groups:
        if group['id'] in rows:
            group['intersection_sources'] = rows[group['id']]


def channel_rows(group):
    """Shared consumer/phase channel view; legacy groups retain their original brush."""
    channels = group.get('channels')
    if channels is not None:
        return [(channel, channels[channel]['enabled'], channels[channel]['actual_brush'],
                 channels[channel].get('brush')) for channel in CHANNELS]
    return [(channel, group['visible_enabled'] if channel == 'VISIBLE' else group['hidden_enabled'],
             group['actual_brush'], group.get('brush')) for channel in CHANNELS]
