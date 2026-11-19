# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated autonomous source-selection prototype. No style, projection or phase.

Callers keep their original legacy producer whenever select_routes returns None.
All mesh arrays must be detached from Blender before calling this module.
"""
from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


WIRE_VISIBLE_SLOT = 'wireframe_visible_brush'
WIRE_HIDDEN_SLOT = 'wireframe_hidden_brush'
CHANNELS = ('VISIBLE', 'HIDDEN')
PROFILE = 'autonomous_wire_groups.v1'
EDGE_CATEGORIES = ('outline', 'smooth', 'material', 'selected', 'normal_angle', 'wireframe','object')
EDGE_CATEGORY_BITS = {
    'object': 2,
    'outline': 1,
    'smooth': 8,
    'material': 16,
    'selected': 32,
    'normal_angle': 64,
    'wireframe': 128,
}
EDGE_SOURCE_KIND = 7
PREDICATE_KEYS = ('silhouette', 'smoothing', 'material', 'selected', 'normal_angle')


def active(groups):
    """No retained-but-disabled record activates a new producer path."""
    return any(
        _record_enabled(_group_value(group, 'wire_sources'))
        or _record_enabled(_group_value(group, 'edge_sources'))
        for group in groups
    )


def _group_value(group, name, default=None):
    if isinstance(group, MappingABC):
        return group.get(name, default)
    return getattr(group, name, default)


def _record_enabled(record):
    return isinstance(record, MappingABC) and record.get('enabled') is True


def _edge_enabled(group):
    return _record_enabled(_group_value(group, 'edge_sources'))


@dataclass(frozen=True)
class MeshArrays:
    name: str
    raw_kinds: np.ndarray
    counts: np.ndarray
    faces0: np.ndarray
    faces1: np.ndarray
    face_material_names: Sequence[str]
    facing: np.ndarray
    original_edge_count: int
    virtual_edges: np.ndarray
    predicates: Mapping[str, np.ndarray] | None = None
    selector_meta: Mapping | None = None


@dataclass(frozen=True)
class GroupRoute:
    id: str
    priority: int
    object_ids: Sequence[str]
    material_names: Sequence[str]
    brush_slots: Sequence[str]
    available_slots: Sequence[str]
    actuals_by_slot: Mapping[str, str]
    visible_enabled: bool = True
    hidden_enabled: bool = True
    runtime_enabled: bool = True
    silhouette_enabled: bool | None = None
    marked_enabled: bool | None = None
    smoothing_enabled: bool | None = None
    edge_sources: Mapping | None = None
    selection: Mapping | None = None
    object_partition: bool = False


@dataclass(frozen=True)
class _EdgeRoute:
    category: str
    channel: str
    enabled: bool
    include_open: bool
    min_angle: float
    max_angle: float
    slot: str
    actual: str
    partition: bool = False


@dataclass(frozen=True)
class WireRecord:
    group_id: str
    enabled: bool
    visible: bool
    hidden: bool


@dataclass(frozen=True)
class SourceSelection:
    group_index: int
    group_id: str
    slot: str
    actual: str
    channel: str
    ids: np.ndarray
    sides: np.ndarray
    source_kinds: np.ndarray
    category_bits: np.ndarray


@dataclass(frozen=True)
class SelectionResult:
    kind_gate: np.ndarray
    owner: np.ndarray
    sides: np.ndarray
    selections: tuple[SourceSelection, ...]


def select_plan(mesh, groups, actuals, runtime_enabled, *, source_categories=('selected', 'smoothing'),
                include_open=True, visibility='BOTH'):
    """Adapt compiled/minimal plan rows without copying geometry or brush values.

    ``actuals`` is the caller's existing resolved (group, slot) mapping.  This
    adapter never replaces its explicit identity with a label or another channel.
    """
    routes = [GroupRoute(group['id'], group['priority'], group['object_ids'], group['material_names'],
              group['brush_slots'], tuple(group['brushes']),
              {slot: actuals[(group['id'], slot)] for slot in group['brush_slots']},
              group['visible_enabled'], group['hidden_enabled'], runtime_enabled.get(group['id'], True),
              group['silhouette_enabled'], group['marked_enabled'], group['smoothing_enabled'],
              edge_sources=group.get('edge_sources'),selection=group.get('selection'),object_partition=group.get('object_partition',False))
              for group in groups]
    wires = [WireRecord(row['group'], row['enabled'], row['visible'], row['hidden'])
             for group in groups if (row := group.get('wire_sources')) is not None]
    return select_routes(mesh, routes, wires, source_categories=source_categories,
                         include_open=include_open, visibility=visibility)


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _bool(value: bool, label: str) -> None:
    _need(type(value) is bool, label + ' must be boolean')


def _integer_array(value: np.ndarray, size: int, label: str) -> None:
    _need(isinstance(value, np.ndarray) and value.shape == (size,)
          and value.dtype.kind in 'iu', label + ' must be an integer edge array')


def _predicate_arrays(mesh: MeshArrays) -> dict[str, np.ndarray] | None:
    """Validate independent edge predicates without deriving them from raw_kinds."""
    if mesh.predicates is None:
        return None
    _need(isinstance(mesh.predicates, MappingABC), 'predicates must be a mapping or None')
    edge_count = len(mesh.raw_kinds)
    result: dict[str, np.ndarray] = {}
    for label in PREDICATE_KEYS[:-1]:
        value = mesh.predicates.get(label)
        _need(isinstance(value, np.ndarray) and value.shape == (edge_count,)
              and value.dtype == np.bool_, 'predicates.' + label + ' must be a boolean edge array')
        result[label] = value
    normal_angle = mesh.predicates.get('normal_angle')
    _need(isinstance(normal_angle, np.ndarray) and normal_angle.shape == (edge_count,)
          and normal_angle.dtype.kind == 'f'
          and bool(np.all(np.isfinite(normal_angle))),
          'predicates.normal_angle must be a finite float edge array')
    result['normal_angle'] = normal_angle
    if 'object_parent' in mesh.predicates or 'object_open' in mesh.predicates:
        for label in ('object_parent','object_open'):
            value=mesh.predicates.get(label)
            _need(isinstance(value,np.ndarray) and value.shape==(edge_count,) and value.dtype==np.bool_,
                  'Object parent predicates must be boolean edge arrays')
            result[label]=value
    return result


def _edge_record(group: GroupRoute) -> Mapping | None:
    record = group.edge_sources
    if record is None:
        return None
    _need(isinstance(record, MappingABC), 'edge_sources must be a mapping or None')
    _bool(record.get('enabled'), 'edge_sources.enabled')
    if not record['enabled']:
        return None
    _need(record.get('group') == group.id, 'edge_sources group does not match its route group')
    return record


def _edge_profiles(groups: Sequence[GroupRoute],
                   predicates: Mapping[str, np.ndarray] | None) -> dict[int, tuple[_EdgeRoute, ...]]:
    """Normalize only the fields needed by selection; schema parsing stays in edge_sources."""
    profiles: dict[int, tuple[_EdgeRoute, ...]] = {}
    for gi, group in enumerate(groups):
        record = _edge_record(group)
        if record is None:
            continue
        _need(predicates is not None, 'active edge_sources require independent edge predicates')
        routes = record.get('routes')
        _need(isinstance(routes, SequenceABC) and not isinstance(routes, (str, bytes)),
              'edge_sources.routes must be a sequence')
        slots_by_actual: dict[str, str] = {}
        partition=group.object_partition or any(r.get('category')=='object' and r.get('enabled') is True for r in routes)
        if partition:
            _need('object_parent' in predicates and 'object_open' in predicates,
                  'Object routes require current full-parent interval predicates')
        normalized: list[_EdgeRoute] = []
        for route in routes:
            _need(isinstance(route, MappingABC), 'edge route must be a mapping')
            category = route.get('category')
            channel = route.get('channel')
            _need(category in EDGE_CATEGORIES, 'invalid edge category')
            _need(channel in CHANNELS, 'invalid edge channel')
            enabled = route.get('enabled')
            _bool(enabled, 'edge route enabled')
            include_open = route.get('include_open')
            if category in ('outline','object'):
                _bool(include_open, 'edge route include_open')
            else:
                include_open = False
            min_angle = max_angle = 0.0
            if category == 'normal_angle':
                min_angle = route.get('min_angle')
                max_angle = route.get('max_angle')
                _need(isinstance(min_angle, (int, float, np.integer, np.floating))
                      and not isinstance(min_angle, (bool, np.bool_))
                      and isinstance(max_angle, (int, float, np.integer, np.floating))
                      and not isinstance(max_angle, (bool, np.bool_))
                      and math.isfinite(min_angle) and math.isfinite(max_angle)
                      and 0 <= min_angle <= max_angle <= 180,
                      'normal_angle range must be finite degrees in 0..180')
                min_angle, max_angle = float(min_angle), float(max_angle)
            slot = route.get('slot')
            if category=='object' and not enabled and slot is None:
                continue
            _need(type(slot) is str and bool(slot), 'edge route must name a canonical slot')
            actual = group.actuals_by_slot.get(slot)
            _need(type(actual) is str and bool(actual), 'missing exact actual for ' + group.id + '/' + slot)
            previous = slots_by_actual.setdefault(actual, slot)
            _need(previous == slot, 'edge routes for one actual must share one canonical slot')
            normalized.append(_EdgeRoute(category, channel, enabled, include_open,
                                         min_angle, max_angle, slot, actual,partition))
        profiles[gi] = tuple(normalized)
    return profiles


def _edge_mask(mesh: MeshArrays, predicates: Mapping[str, np.ndarray], route: _EdgeRoute,
               original: np.ndarray, include_open: bool) -> np.ndarray:
    if route.partition and route.category in ('outline','object'):
        return predicates['object_parent'] & (route.include_open | ~predicates['object_open'])
    if route.category == 'outline':
        # An explicit profile owns its open boundary gate independently of legacy.
        opened = (mesh.counts == 1) if route.include_open else False
        return predicates['silhouette'] | opened
    if route.category == 'smooth':
        return predicates['smoothing']
    if route.category == 'material':
        return predicates['material']
    if route.category == 'selected':
        return predicates['selected']
    if route.category == 'normal_angle':
        return (original & (mesh.counts == 2)
                & (predicates['normal_angle'] >= route.min_angle)
                & (predicates['normal_angle'] <= route.max_angle))
    return original


def _channel_enabled(group: GroupRoute, channel: str) -> bool:
    return group.visible_enabled if channel == 'VISIBLE' else group.hidden_enabled


def _validate(mesh: MeshArrays, groups: Sequence[GroupRoute], wires: Sequence[WireRecord]) -> dict[str, WireRecord]:
    edge_count = len(mesh.raw_kinds)
    _integer_array(mesh.raw_kinds, edge_count, 'raw_kinds')
    _need(bool(np.all((mesh.raw_kinds >= 0) & (mesh.raw_kinds <= 4))), 'raw_kinds must remain 0..4')
    for label in ('counts', 'faces0', 'faces1'):
        _integer_array(getattr(mesh, label), edge_count, label)
    _need(bool(np.all(mesh.counts >= 0)), 'negative counts')
    _need(isinstance(mesh.facing, np.ndarray) and mesh.facing.ndim == 1
          and mesh.facing.dtype == np.bool_, 'facing must be a boolean polygon array')
    polygon_count = len(mesh.facing)
    _need(len(mesh.face_material_names) == polygon_count, 'material/facing polygon count differs')
    _need(all(isinstance(name, str) for name in mesh.face_material_names), 'material names must be strings')
    if polygon_count:
        _need(bool(np.all((mesh.faces0 >= 0) & (mesh.faces0 < polygon_count)))
              and bool(np.all((mesh.faces1 >= 0) & (mesh.faces1 < polygon_count))), 'missing polygon')
    else:
        _need(not bool(np.any(mesh.counts)), 'face counts without polygons')
    _need(type(mesh.original_edge_count) is int
          and 0 <= mesh.original_edge_count <= edge_count, 'invalid original_edge_count')
    _need(isinstance(mesh.virtual_edges, np.ndarray) and mesh.virtual_edges.shape == (edge_count,)
          and mesh.virtual_edges.dtype == np.bool_, 'invalid virtual_edges')
    _need(not bool(np.any(mesh.virtual_edges[:mesh.original_edge_count]))
          and bool(np.all(mesh.virtual_edges[mesh.original_edge_count:])), 'original/virtual prefix mismatch')
    ids = [group.id for group in groups]
    _need(len(set(ids)) == len(ids), 'duplicate group ID')
    priorities = [group.priority for group in groups]
    _need(all(type(priority) is int for priority in priorities)
          and len(set(priorities)) == len(priorities)
          and priorities == sorted(priorities, reverse=True), 'groups must have unique descending priorities')
    for group in groups:
        _bool(group.object_partition,'object_partition')
        for label in ('visible_enabled', 'hidden_enabled', 'runtime_enabled'):
            _bool(getattr(group, label), label)
        for label in ('silhouette_enabled', 'marked_enabled', 'smoothing_enabled'):
            _need(getattr(group, label) is None or type(getattr(group, label)) is bool, label + ' must be boolean/null')
        _need(len(set(group.brush_slots)) == len(group.brush_slots), 'duplicate brush slot')
        if group.edge_sources is not None:
            _need(isinstance(group.edge_sources, MappingABC), 'edge_sources must be a mapping or None')
            _bool(group.edge_sources.get('enabled'), 'edge_sources.enabled')
            if group.edge_sources['enabled']:
                _need(group.edge_sources.get('group') == group.id,
                      'edge_sources group does not match its route group')
    by_group: dict[str, WireRecord] = {}
    for record in wires:
        _need(record.group_id in ids, 'wire record references unknown group')
        _need(record.group_id not in by_group, 'duplicate wire group')
        for label in ('enabled', 'visible', 'hidden'):
            _bool(getattr(record, label), 'wire.' + label)
        by_group[record.group_id] = record
    _need(not any(_edge_enabled(group) for group in groups) or mesh.predicates is not None,
          'active edge_sources require independent edge predicates')
    return by_group


def select_routes(mesh: MeshArrays, groups: Sequence[GroupRoute], wires: Sequence[WireRecord], *,
                  source_categories: Sequence[str] = ('selected', 'smoothing'),
                  include_open: bool = True, visibility: str = 'BOTH') -> SelectionResult | None:
    """Select only when an enabled wire or explicit edge record activates the new path.

    Native SpecOn rejection and full alias/source proof are author/compiler work.
    actuals_by_slot is the caller's exact resolved registry binding, never a label.
    Disabled records restore the next retained source (wire, then legacy).  The new
    explicit routes consume independent predicates and replace that group's old
    boundary/wire selection without mutating either retained record.
    """
    for record in wires:
        _bool(record.enabled, 'wire.enabled')
    if not any(record.enabled for record in wires) and not any(_edge_enabled(group) for group in groups):
        return None
    _bool(include_open, 'include_open')
    _need(visibility in CHANNELS + ('BOTH',), 'invalid visibility')
    by_group = _validate(mesh, groups, wires)
    predicates = _predicate_arrays(mesh)
    edge_profiles = _edge_profiles(groups, predicates)
    edge_count = len(mesh.raw_kinds)
    kind_gate = np.array(mesh.raw_kinds, dtype=np.int8, copy=True)
    if 'smoothing' not in source_categories:
        kind_gate[kind_gate == 3] = 0
    if 'selected' not in source_categories:
        kind_gate[np.isin(kind_gate, [1, 2, 4])] = 0
    if not include_open:
        kind_gate[kind_gate == 2] = 0
    original = np.arange(edge_count) < mesh.original_edge_count
    owner = np.full(edge_count, -1, np.int32)
    sides = np.zeros(edge_count, np.int8)
    names = np.asarray(mesh.face_material_names)
    from .selector_bindings import face_membership
    for gi, group in enumerate(groups):
        whole,faces=face_membership(mesh.name,names,group,mesh.selector_meta)
        if len(names):
            m0 = faces[mesh.faces0] & (mesh.counts > 0)
            m1 = faces[mesh.faces1] & (mesh.counts > 0)
        else:
            m0 = m1 = np.zeros(edge_count, bool)
        if whole:
            m0 = m1 = np.ones(edge_count, bool)
        record = by_group.get(group.id)
        wire_enabled = record is not None and record.enabled
        profile = edge_profiles.get(gi)
        if profile is not None:
            eligible = np.zeros(edge_count, bool)
            for route in profile:
                if route.enabled and _channel_enabled(group, route.channel):
                    eligible |= _edge_mask(mesh, predicates, route, original, include_open)
        elif wire_enabled:
            eligible = original
        else:
            eligible = kind_gate > 0
        match = (owner < 0) & (m0 | m1) & eligible
        owner[match] = gi
        prefer1 = (~m0) & m1
        if len(mesh.facing):
            prefer1 |= m1 & mesh.facing[mesh.faces1] & (~mesh.facing[mesh.faces0])
        sides[match & prefer1] = 1
    selections: list[SourceSelection] = []

    def emit(gi: int, slot: str, channel: str, selected: np.ndarray, wire: bool) -> None:
        group = groups[gi]
        if not len(selected):
            return
        actual = group.actuals_by_slot.get(slot)
        _need(type(actual) is str and bool(actual), 'missing exact actual for ' + group.id + '/' + slot)
        kinds = (np.full(len(selected), 6, np.int8) if wire
                 else np.array(kind_gate[selected], dtype=np.int8, copy=True))
        bits = (np.full(len(selected), 128, np.uint16) if wire else
                np.select([np.isin(kinds, [1, 2]), kinds == 3, kinds == 4],
                          [1, 8, 32], default=0).astype(np.uint16))
        selections.append(SourceSelection(gi, group.id, slot, actual, channel,
                          np.array(selected, dtype=np.int64, copy=True),
                          np.array(sides[selected], dtype=np.int8, copy=True), kinds, bits))

    def emit_edge(gi: int, profile: Sequence[_EdgeRoute]) -> None:
        group = groups[gi]
        base = owner == gi
        for channel in CHANNELS:
            if visibility not in (channel, 'BOTH') or not _channel_enabled(group, channel):
                continue
            merged: dict[str, list] = {}
            for route in profile:
                if route.channel != channel or not route.enabled:
                    continue
                selected = base & _edge_mask(mesh, predicates, route, original, include_open)
                entry = merged.get(route.actual)
                if entry is None:
                    entry = [route.slot, np.zeros(edge_count, np.uint16),
                             np.zeros(edge_count, bool)]
                    merged[route.actual] = entry
                _need(entry[0] == route.slot, 'edge routes for one actual must share one canonical slot')
                entry[2] |= selected
                entry[1][selected] |= EDGE_CATEGORY_BITS[route.category]
            for actual, (slot, bits, union) in merged.items():
                ids = np.flatnonzero(union)
                if not len(ids):
                    continue
                _need(type(actual) is str and bool(actual),
                      'missing exact actual for ' + group.id + '/' + slot)
                selections.append(SourceSelection(
                    gi, group.id, slot, actual, channel,
                    np.array(ids, dtype=np.int64, copy=True),
                    np.array(sides[ids], dtype=np.int8, copy=True),
                    np.full(len(ids), EDGE_SOURCE_KIND, np.int8),
                    np.array(bits[ids], dtype=np.uint16, copy=True),
                ))

    for gi, group in enumerate(groups):
        if not group.runtime_enabled:
            continue
        profile = edge_profiles.get(gi)
        if profile is not None:
            # The explicit record owns this group's routing; wire is retained but not read.
            emit_edge(gi, profile)
            continue
        record = by_group.get(group.id)
        if record is not None and record.enabled:
            # Complete replacement: even a false wire channel never restores legacy.
            selected = np.flatnonzero(owner == gi)
            for channel, source_on, slot in (
                ('VISIBLE', record.visible, WIRE_VISIBLE_SLOT),
                ('HIDDEN', record.hidden, WIRE_HIDDEN_SLOT),
            ):
                if source_on and visibility in (channel, 'BOTH'):
                    emit(gi, slot, channel, selected, True)
            continue
        mask = owner == gi
        if group.silhouette_enabled is False:
            mask &= kind_gate != 1
        if group.marked_enabled is False:
            mask &= kind_gate != 4
        if group.smoothing_enabled is False:
            mask &= kind_gate != 3
        slots = np.full(edge_count, 'brush', dtype=object)
        for kind, slot in ((1, 'silhouette_brush'), (3, 'smoothing_brush'), (4, 'marked_brush')):
            if slot in group.available_slots:
                slots[kind_gate == kind] = slot
        for slot in group.brush_slots:
            selected = np.flatnonzero(mask & (slots == slot))
            for channel, enabled in (('VISIBLE', group.visible_enabled), ('HIDDEN', group.hidden_enabled)):
                if enabled and visibility in (channel, 'BOTH'):
                    emit(gi, slot, channel, selected, False)
    return SelectionResult(kind_gate, owner, sides, tuple(selections))
