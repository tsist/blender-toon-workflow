# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-array recovery of unclipped boundary source records.

This is an audit prototype, not a product integration.  It recovers the
explicit boundary source domain from captured evaluated mesh arrays, a captured
frame, the compiled style plan and the current runtime group controls.  It does
not link chains, solve phase, build shapes, weld meshes, rasterise, or call
Blender/GPU/native drawing.

The main builder never reads a file.  ``capture_loader.load_capture`` is the
separate explicit reader for this capture and always uses ``allow_pickle=False``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import OrderedDict

import numpy as np
from . import source_routes


SCHEMA = 'toon.boundary-source-builder.v1'
SOURCE_SCHEMA = 'toon.boundary-source-table.v1'
DOMAIN_SCHEMA = 'toon.boundary-source-domain-table.v1'
LINKER_ADAPTER_SCHEMA = 'toon.boundary-source-linker-adapter.v1'
LINKER_SCHEMA = 'toon.viewport-source-chain-linker.v1'
LINKER_SOURCE_FIELDS = ('source_id', 'endpoint_ids', 'xy', 'w', 'domain')
LINKER_MAX_SOURCES = 100_000
LINKER_MAX_IDENTIFIER_LENGTH = 256
CLASSIFICATION = 'autonomous_resolved_source_kind.v1'
CHANNELS = ('VISIBLE', 'HIDDEN')
SOURCE_CATEGORIES = ('selected', 'smoothing')


class BoundarySourceError(ValueError):
    """A source table cannot be built without changing the declared contract."""


def _need(condition, message):
    if not condition:
        raise BoundarySourceError(message)


def _int(value, name, minimum=None):
    _need(type(value) is int and not isinstance(value, bool), name + ' must be an integer')
    if minimum is not None:
        _need(value >= minimum, name + ' must be >= ' + str(minimum))
    return value


def _text(value, name):
    _need(type(value) is str and value, name + ' must be a nonempty string')
    return value


def _mapping(value, name):
    _need(type(value) is dict, name + ' must be an object')
    return value


def _array(value, name, shape, kinds, *, finite=False):
    array = np.asarray(value)
    _need(array.ndim == len(shape), name + ' has the wrong rank')
    for actual, expected in zip(array.shape, shape):
        _need(expected is None or actual == expected,
              name + ' has shape ' + repr(array.shape) + ', expected ' + repr(shape))
    _need(array.dtype.kind in kinds, name + ' has unsupported dtype ' + str(array.dtype))
    if finite:
        _need(bool(np.isfinite(array).all()), name + ' contains non-finite values')
    return array


def _string_array(values):
    values = [str(value) for value in values]
    if not values:
        return np.empty(0, dtype='<U1')
    return np.asarray(values, dtype=str).copy()


def _canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _digest(prefix, value):
    return prefix + hashlib.sha256(_canonical(value)).hexdigest()


def linker_domain_key(group_id, actual_brush_id, channel, mesh_index):
    """Encode the caller-owned linker domain (group/actual/channel/mesh).

    The linker treats this string as opaque.  The compact canonical-JSON
    payload keeps the identity unambiguous if identifiers ever contain
    punctuation; the full source/domain tables retain the separate fields.
    """
    _text(group_id, 'linker domain group_id')
    _text(actual_brush_id, 'linker domain actual_brush_id')
    _need(channel in CHANNELS, 'linker domain channel is unsupported')
    mesh_index = _int(mesh_index, 'linker domain mesh_index', 0)
    payload = json.dumps(
        [group_id, actual_brush_id, channel, mesh_index],
        ensure_ascii=False, allow_nan=False, separators=(',', ':'),
    )
    value = 'bnddom-v1:' + payload
    _need(len(value) <= LINKER_MAX_IDENTIFIER_LENGTH,
          'linker domain exceeds 256 characters')
    return value


def make_capture_id(mesh_fingerprints, frame, style_sha256):
    """Stable capture scope for source IDs, without reading or hashing arrays.

    ``mesh_fingerprints`` is a sequence of the capture's recorded mesh hashes
    (one per captured mesh, in captured mesh-index order).  This identity is
    deliberately capture-scoped.  It is not a cross-frame or instance identity.
    """
    rows = []
    for row in mesh_fingerprints:
        row = _mapping(row, 'mesh fingerprint')
        rows.append({
            'mesh_index': _int(row.get('mesh_index'), 'mesh fingerprint.mesh_index', 0),
            'sha256': _text(row.get('sha256'), 'mesh fingerprint.sha256'),
        })
    rows.sort(key=lambda row: row['mesh_index'])
    _need(bool(rows), 'at least one mesh fingerprint is required')
    _need(len({row['mesh_index'] for row in rows}) == len(rows),
          'mesh fingerprint indices must be unique')
    frame = _normalize_frame(frame)
    value = {
        'schema': 'toon.boundary-source-capture-id.v1',
        'mesh_sha256': rows,
        'mvp': frame['mvp'].tolist(),
        'view': frame['view'].tolist(),
        'width': frame['width'],
        'height': frame['height'],
        'perspective': frame['perspective'],
        'style_sha256': _text(style_sha256, 'style_sha256'),
    }
    return 'capture-sha256:' + hashlib.sha256(_canonical(value)).hexdigest()


def _normalize_frame(frame):
    frame = _mapping(frame, 'frame')
    width = _int(frame.get('width'), 'frame.width', 1)
    height = _int(frame.get('height'), 'frame.height', 1)
    mvp = _array(frame.get('mvp'), 'frame.mvp', (4, 4), 'iuf', finite=True)
    view = _array(frame.get('view'), 'frame.view', (4, 4), 'iuf', finite=True)
    perspective = frame.get('perspective')
    _need(type(perspective) is bool, 'frame.perspective must be boolean')
    return {
        'width': width,
        'height': height,
        'mvp': np.array(mvp, dtype=np.float64, copy=True),
        'view': np.array(view, dtype=np.float64, copy=True),
        'perspective': perspective,
    }


def _normalize_mesh_rows(meshes):
    _need(type(meshes) in (list, tuple), 'meshes must be a sequence')
    rows = []
    for position, mesh in enumerate(meshes):
        mesh = _mapping(mesh, 'meshes[%d]' % position)
        mesh_index = _int(mesh.get('mesh_index'), 'meshes[%d].mesh_index' % position, 0)
        name = _text(mesh.get('name'), 'meshes[%d].name' % position)
        face_offset = _int(mesh.get('face_offset'), 'meshes[%d].face_offset' % position, 0)
        vertices = _array(mesh.get('vertices'), 'meshes[%d].vertices' % position,
                          (None, 3), 'iuf', finite=True)
        edges = _array(mesh.get('edges'), 'meshes[%d].edges' % position,
                       (None, 2), 'iu')
        counts = _array(mesh.get('counts'), 'meshes[%d].counts' % position,
                        (len(edges),), 'iu')
        normals = _array(mesh.get('normals'), 'meshes[%d].normals' % position,
                         (None, 3), 'iuf', finite=True)
        centers = _array(mesh.get('centers'), 'meshes[%d].centers' % position,
                         (None, 3), 'iuf', finite=True)
        _need(len(normals) == len(centers),
              'meshes[%d] normals and centers have different polygon counts' % position)
        faces0 = _array(mesh.get('faces0'), 'meshes[%d].faces0' % position,
                        (len(edges),), 'iu')
        faces1 = _array(mesh.get('faces1'), 'meshes[%d].faces1' % position,
                        (len(edges),), 'iu')
        corners0 = _array(mesh.get('corners0'), 'meshes[%d].corners0' % position,
                          (len(edges), 2), 'iu')
        corners1 = _array(mesh.get('corners1'), 'meshes[%d].corners1' % position,
                          (len(edges), 2), 'iu')
        triangle_supports = _array(mesh.get('triangle_supports'),
                                   'meshes[%d].triangle_supports' % position,
                                   (len(edges), 2), 'iu')
        virtual_edges = np.asarray(mesh.get('virtual_edges'))
        _need(virtual_edges.shape == (len(edges),) and virtual_edges.dtype == np.bool_,
              'meshes[%d].virtual_edges must be a source-length boolean array' % position)
        resolved_kinds = _array(mesh.get('resolved_kinds'),
                                'meshes[%d].resolved_kinds' % position,
                                (len(edges),), 'iu')
        face_material_ids = _array(mesh.get('face_material_ids'),
                                   'meshes[%d].face_material_ids' % position,
                                   (None,), 'iu')
        _need(len(face_material_ids) == len(normals),
              'meshes[%d] face_material_ids and polygon arrays differ' % position)
        palette = mesh.get('face_material_palette')
        _need(type(palette) in (list, tuple)
              and all(type(item) is str for item in palette),
              'meshes[%d].face_material_palette must be a string sequence' % position)
        _need(len(set(palette)) == len(palette),
              'meshes[%d].face_material_palette must be unique' % position)
        if len(face_material_ids):
            _need(int(face_material_ids.min()) >= 0
                  and int(face_material_ids.max()) < len(palette),
                  'meshes[%d] face material ID is outside the palette' % position)
        if len(edges):
            _need(int(edges.min()) >= 0 and int(edges.max()) < len(vertices),
                  'meshes[%d] edge references a missing vertex' % position)
        _need(int(np.min(counts, initial=0)) >= 0,
              'meshes[%d] counts must be nonnegative' % position)
        _need(int(np.min(resolved_kinds, initial=0)) >= 0
              and int(np.max(resolved_kinds, initial=0)) <= 4,
              'meshes[%d] resolved_kinds must be in 0..4' % position)
        if len(normals):
            for field, values in (('faces0', faces0), ('faces1', faces1)):
                _need(int(values.min()) >= 0 and int(values.max()) < len(normals),
                      'meshes[%d] %s references a missing polygon' % (position, field))
        else:
            _need(not bool(np.any(counts > 0)),
                  'meshes[%d] has faces but no polygon normals/centers' % position)
        for field, values in (('corners0', corners0), ('corners1', corners1)):
            _need(int(values.min(initial=-1)) >= -1,
                  'meshes[%d] %s contains an invalid corner' % (position, field))
        _need(int(np.min(triangle_supports, initial=-1)) >= -1,
              'meshes[%d] triangle_supports contains an invalid support' % position)
        triangles = mesh.get('triangles')
        triangle_count = None
        if triangles is not None:
            triangle_array = _array(triangles, 'meshes[%d].triangles' % position,
                                    (None, 3), 'iu')
            if len(triangle_array):
                _need(int(triangle_array.min()) >= 0
                      and int(triangle_array.max()) < len(vertices),
                      'meshes[%d] triangles reference a missing vertex' % position)
            triangle_count = len(triangle_array)
        rows.append({
            'mesh_index': mesh_index,
            'name': name,
            'face_offset': face_offset,
            'vertices': vertices,
            'edges': edges,
            'counts': counts,
            'normals': normals,
            'centers': centers,
            'faces0': faces0,
            'faces1': faces1,
            'corners0': corners0,
            'corners1': corners1,
            'triangle_supports': triangle_supports,
            'virtual_edges': virtual_edges,
            'resolved_kinds': resolved_kinds,
            'face_material_ids': face_material_ids,
            'face_material_palette': tuple(palette),
            'triangle_count': triangle_count,
            'sha256': mesh.get('sha256'),
            'input_position': position,
        })
        if 'original_edge_count' in mesh:
            rows[-1]['original_edge_count'] = _int(mesh['original_edge_count'], 'original_edge_count', 0)
        if 'selector_meta' in mesh:
            from .selector_bindings import checked_meta
            rows[-1]['selector_meta']=checked_meta(mesh['selector_meta'],len(normals))
        if 'edge_predicates' in mesh:
            predicates = _mapping(mesh['edge_predicates'], 'edge_predicates')
            rows[-1]['edge_predicates'] = {}
            for name in ('silhouette', 'smoothing', 'material', 'selected', 'normal_angle'):
                value = np.asarray(predicates.get(name))
                _need(value.shape == (len(edges),) and (value.dtype == np.bool_ if name != 'normal_angle'
                      else value.dtype.kind == 'f' and bool(np.isfinite(value).all())),
                      'Invalid independent edge predicate: '+name)
                rows[-1]['edge_predicates'][name] = value
            for name in ('object_parent','object_open'):
                if name in predicates:
                    value=np.asarray(predicates[name])
                    _need(value.shape==(len(edges),) and value.dtype==np.bool_, 'Invalid Object parent predicate')
                    rows[-1]['edge_predicates'][name]=value
    _need(bool(rows), 'at least one captured mesh is required')
    rows.sort(key=lambda row: row['mesh_index'])
    _need(len({row['mesh_index'] for row in rows}) == len(rows),
          'captured mesh_index values must be unique')
    return rows


def _normalize_groups(plan):
    plan = _mapping(plan, 'plan')
    groups = plan.get('groups')
    _need(type(groups) in (list, tuple) and groups, 'plan.groups must be a nonempty sequence')
    result = []
    seen_ids = set()
    seen_priorities = set()
    for position, group in enumerate(groups):
        group = _mapping(group, 'plan.groups[%d]' % position)
        group_id = _text(group.get('id'), 'plan.groups[%d].id' % position)
        _need(group_id not in seen_ids, 'duplicate plan group id: ' + group_id)
        seen_ids.add(group_id)
        priority = _int(group.get('priority'), 'plan.groups[%d].priority' % position)
        _need(priority not in seen_priorities,
              'duplicate plan group priority: ' + str(priority))
        seen_priorities.add(priority)
        object_ids = group.get('object_ids')
        material_names = group.get('material_names')
        for value, field in ((object_ids, 'object_ids'), (material_names, 'material_names')):
            _need(type(value) in (list, tuple)
                  and all(type(item) is str for item in value),
                  'plan group %s.%s must be a string sequence' % (group_id, field))
        _need(len(set(object_ids)) == len(object_ids),
              'plan group %s has duplicate object selectors' % group_id)
        _need(len(set(material_names)) == len(material_names),
              'plan group %s has duplicate material selectors' % group_id)
        for field in ('visible_enabled', 'hidden_enabled'):
            _need(type(group.get(field)) is bool,
                  'plan group %s.%s must be boolean' % (group_id, field))
        for field in ('silhouette_enabled', 'marked_enabled', 'smoothing_enabled'):
            value = group.get(field)
            _need(value is None or type(value) is bool,
                  'plan group %s.%s must be boolean or null' % (group_id, field))
        brushes = _mapping(group.get('brushes'), 'plan group %s.brushes' % group_id)
        brush_meta = _mapping(group.get('brush_meta'), 'plan group %s.brush_meta' % group_id)
        slots = group.get('brush_slots')
        _need(type(slots) in (list, tuple) and slots,
              'plan group %s.brush_slots must be nonempty' % group_id)
        _need(all(type(slot) is str and slot for slot in slots),
              'plan group %s.brush_slots must contain strings' % group_id)
        _need(len(set(slots)) == len(slots),
              'plan group %s.brush_slots contains duplicates' % group_id)
        _need('brush' in slots, 'plan group %s must contain the brush slot' % group_id)
        for slot in slots:
            _need(slot in brushes and slot in brush_meta,
                  'plan group %s slot %s lacks compiled brush/meta' % (group_id, slot))
            _need(type(brush_meta[slot]) is dict,
                  'plan group %s slot %s brush_meta must be an object' % (group_id, slot))
        result.append({
            'id': group_id,
            'priority': priority,
            'object_ids': tuple(object_ids),
            'material_names': tuple(material_names),
            'visible_enabled': group['visible_enabled'],
            'hidden_enabled': group['hidden_enabled'],
            'silhouette_enabled': group.get('silhouette_enabled'),
            'marked_enabled': group.get('marked_enabled'),
            'smoothing_enabled': group.get('smoothing_enabled'),
            'brush_slots': tuple(slots),
            'brushes': brushes,
            'brush_meta': brush_meta,
        })
        if 'wire_sources' in group:
            result[-1]['wire_sources'] = dict(group['wire_sources'])
        if 'selection' in group:result[-1]['selection']=copy.deepcopy(group['selection'])
        if 'object_partition' in group:result[-1]['object_partition']=group['object_partition']
        if 'edge_sources' in group:
            result[-1]['edge_sources'] = copy.deepcopy(group['edge_sources'])
    result.sort(key=lambda group: group['priority'], reverse=True)
    return result, plan


def _normalize_plan_settings(plan):
    settings = _mapping(plan.get('settings'), 'plan.settings')
    categories = settings.get('source_categories', ['selected', 'smoothing'])
    _need(type(categories) in (list, tuple)
          and all(type(item) is str for item in categories),
          'plan.settings.source_categories must be a string sequence')
    _need(len(set(categories)) == len(categories),
          'plan.settings.source_categories contains duplicates')
    unknown = sorted(set(categories) - set(SOURCE_CATEGORIES))
    _need(not unknown, 'unknown source categories are not implemented: ' + repr(unknown))
    include_open = settings.get('include_open', True)
    _need(type(include_open) is bool, 'plan.settings.include_open must be boolean')
    return {'source_categories': tuple(categories), 'include_open': include_open}


def _normalize_runtime_settings(settings):
    settings = _mapping(settings, 'settings')
    visibility = settings.get('visibility')
    _need(visibility in CHANNELS + ('BOTH',),
          'settings.visibility must be VISIBLE, HIDDEN or BOTH')
    controls = settings.get('groups', [])
    _need(type(controls) in (list, tuple), 'settings.groups must be a sequence')
    result = {}
    for position, control in enumerate(controls):
        control = _mapping(control, 'settings.groups[%d]' % position)
        group_id = _text(control.get('id'), 'settings.groups[%d].id' % position)
        _need(group_id not in result, 'duplicate runtime group control: ' + group_id)
        enabled = control.get('enabled')
        _need(type(enabled) is bool,
              'settings.groups[%d].enabled must be boolean' % position)
        result[group_id] = {'id': group_id, 'enabled': enabled}
    return {'visibility': visibility, 'groups': result}


def _normalize_aliases(plan, groups):
    raw = plan.get('raw', {})
    _need(type(raw) is dict, 'plan.raw must be an object')
    request = raw.get('request', {})
    _need(type(request) is dict, 'plan.raw.request must be an object')
    rows = request.get('boundary_aliases', [])
    _need(type(rows) in (list, tuple), 'boundary_aliases must be a sequence')
    group_ids = {group['id'] for group in groups}
    result = {}
    for position, row in enumerate(rows):
        row = _mapping(row, 'boundary_aliases[%d]' % position)
        _need(set(row) == {'group', 'slot', 'brush_id'},
              'boundary_aliases[%d] has unexpected fields' % position)
        group_id = _text(row.get('group'), 'boundary_aliases[%d].group' % position)
        slot = _text(row.get('slot'), 'boundary_aliases[%d].slot' % position)
        actual = _text(row.get('brush_id'), 'boundary_aliases[%d].brush_id' % position)
        _need(group_id in group_ids,
              'boundary alias references an unknown group: ' + group_id)
        key = (group_id, slot)
        _need(key not in result, 'duplicate boundary alias: ' + repr(key))
        result[key] = actual
    return result


def _actual_for(group, slot, aliases):
    actual = aliases.get((group['id'], slot))
    if actual is not None:
        return actual
    meta = group['brush_meta'][slot]
    uid = meta.get('uid')
    if uid is None:
        return slot
    _need(type(uid) is str and uid,
          'plan group %s slot %s has an invalid editor brush uid'
          % (group['id'], slot))
    return uid


def _normalize_actual_registry(actual_registry):
    if actual_registry is None:
        return None
    actual_registry = _mapping(actual_registry, 'actual_registry')
    result = {}
    for actual, row in actual_registry.items():
        _text(actual, 'actual_registry key')
        row = _mapping(row, 'actual_registry[%s]' % actual)
        brush = row.get('source_brush_key', '')
        detail = row.get('source_detail_key', '')
        _need(type(brush) is str and type(detail) is str,
              'actual_registry[%s] source brush/detail keys must be strings' % actual)
        result[actual] = {'source_brush_key': brush, 'source_detail_key': detail}
    return result


def _normalize_weld_requests(weld_requests):
    if weld_requests is None:
        return None
    if isinstance(weld_requests, dict):
        rows = []
        for group_id, row in weld_requests.items():
            row = dict(_mapping(row, 'weld request'))
            row.setdefault('group_id', group_id)
            rows.append(row)
    else:
        _need(type(weld_requests) in (list, tuple),
              'weld_requests must be a mapping or sequence')
        rows = [_mapping(row, 'weld request') for row in weld_requests]
    result = []
    for position, row in enumerate(rows):
        group_id = row.get('group_id')
        if group_id is not None:
            group_id = _text(group_id, 'weld_requests[%d].group_id' % position)
        requested = row.get('is_weld_edges')
        _need(type(requested) is bool,
              'weld_requests[%d].is_weld_edges must be boolean' % position)
        result.append({
            'group_id': group_id,
            'requested': requested,
            'line_set_node': row.get('line_set_node'),
            'line': row.get('line'),
            'lineset_id': row.get('lineset_id'),
            'native_line_set_pointer': row.get('native_line_set_pointer'),
            'source_alias_pointer': row.get('source_alias_pointer'),
            'destination_uids': list(row.get('destination_uids') or []),
            'destination_slots': list(row.get('destination_slots') or []),
            'source_default_brush_key': row.get('source_default_brush_key'),
            'status': 'NOT_EXECUTED' if requested else 'NOT_REQUESTED',
        })
    return result


def _material_indices(palette, names):
    names = set(names)
    return [index for index, name in enumerate(palette) if name in names]


def _owner_and_sides(row, groups, kind_gate, frame):
    edge_count = len(row['edges'])
    owner = np.full(edge_count, -1, dtype=np.int32)
    sides = np.zeros(edge_count, dtype=np.int8)
    polygon_count = len(row['normals'])
    if polygon_count:
        try:
            inverse_view = np.linalg.inv(frame['view'])
        except np.linalg.LinAlgError as exc:
            raise BoundarySourceError('frame.view is singular; facing is undefined') from exc
        if frame['perspective']:
            facing = (np.einsum('ij,ij->i', row['normals'],
                                inverse_view[:3, 3] - row['centers']) >= 0.0)
        else:
            facing = row['normals'] @ inverse_view[:3, 2] >= 0.0
    else:
        facing = np.empty(0, dtype=np.bool_)
    polygon_faces0 = row['face_material_ids'][row['faces0']] if polygon_count else None
    polygon_faces1 = row['face_material_ids'][row['faces1']] if polygon_count else None
    for group_index, group in enumerate(groups):
        from .selector_bindings import face_membership
        names=np.asarray(row['face_material_palette'])[row['face_material_ids']]
        object_match,faces=face_membership(row['name'],names,group,row.get('selector_meta'))
        if polygon_count:
            m0 = faces[row['faces0']] & (row['counts'] > 0)
            m1 = faces[row['faces1']] & (row['counts'] > 0)
        else:
            m0 = np.zeros(edge_count, dtype=np.bool_)
            m1 = np.zeros(edge_count, dtype=np.bool_)
        if object_match:
            m0 = np.ones(edge_count, dtype=np.bool_)
            m1 = np.ones(edge_count, dtype=np.bool_)
        match = (owner < 0) & (m0 | m1) & (kind_gate > 0)
        owner[match] = group_index
        prefer1 = (~m0) & m1
        if len(facing):
            prefer1 |= m1 & facing[row['faces1']] & (~facing[row['faces0']])
        sides[match & prefer1] = 1
    return owner, sides


def _project_strict(world, mvp, width, height, location, *, projection_profile='strict'):
    """Full-source finite positive-w projection; strict keeps its legacy near gate."""
    _need(bool(np.isfinite(world).all()),
          'non-finite source world endpoint at ' + location)
    homogeneous = np.concatenate(
        (world, np.ones((*world.shape[:2], 1), dtype=np.float64)), axis=2)
    clip = homogeneous @ np.asarray(mvp, dtype=np.float64).T
    _need(bool(np.isfinite(clip).all()),
          'non-finite full-MVP clip coordinate at ' + location)
    w = clip[:, :, 3]
    bad_w = np.flatnonzero(~(w > 0.0).all(axis=1))
    if len(bad_w):
        index = int(bad_w[0])
        raise BoundarySourceError(
            'strict projection rejected non-positive/non-finite w at %s '
            '(chord row %d, w=%r)' % (location, index, w[index].tolist()))
    # Keep the historical near-plane restriction for existing callers.  The
    # explicit runtime profile measures the full positive-w source; the existing
    # consumer later clips geometry while retaining the original source phase.
    near_margin = clip[:, :, 2] + w
    bad_near = np.flatnonzero((near_margin < 0.0).any(axis=1))
    if projection_profile == 'strict' and len(bad_near):
        index = int(bad_near[0])
        raise BoundarySourceError(
            'strict projection rejected near-plane crossing with positive w at %s '
            '(chord row %d, z_clip+w=%r)' % (location, index, near_margin[index].tolist()))
    ndc = clip[:, :, :2] / w[:, :, None]
    pixel = (ndc + 1.0) * np.array([width, height], dtype=np.float64) * 0.5
    _need(bool(np.isfinite(pixel).all()),
          'non-finite bottom-left pixel coordinate at ' + location)
    legacy_xy = ndc * np.array([width, height], dtype=np.float64) * 0.5
    return pixel, w, near_margin, legacy_xy


def _legacy_parameters(chain_parameters, pairs, clip_xy, location):
    lengths = np.linalg.norm(clip_xy[:, 1] - clip_xy[:, 0], axis=1)
    _need(bool(np.isfinite(lengths).all()),
          'non-finite legacy chain length at ' + location)
    try:
        parameters, chains = chain_parameters(pairs, lengths)
    except Exception as exc:
        raise BoundarySourceError(
            'legacy chain_parameters failed at ' + location) from exc
    parameters = np.asarray(parameters)
    _need(parameters.shape == (len(pairs), 2)
          and parameters.dtype.kind in 'iuf'
          and bool(np.isfinite(parameters).all()),
          'legacy chain_parameters returned invalid phase at ' + location)
    _need(type(chains) is int and chains >= 0,
          'legacy chain_parameters returned an invalid chain count at ' + location)
    return np.array(parameters, dtype=np.float64, copy=True), chains


def _category_bits(kinds):
    return np.select(
        [np.isin(kinds, [1, 2]), kinds == 3, kinds == 4],
        [1, 8, 32],
        default=0,
    ).astype(np.uint16)


def _new_domain(group, actual, channel, mesh, domain_id):
    return {
        'domain_id': domain_id,
        'linker_domain': linker_domain_key(
            group['id'], actual, channel, mesh['mesh_index']),
        'group_id': group['id'],
        'actual_brush_id': actual,
        'channel': channel,
        'mesh_index': mesh['mesh_index'],
        'mesh_name': mesh['name'],
        'priority': group['priority'],
        'slots': [],
        '_ranges': [],
        'source_count': 0,
        'virtual_source_edges': 0,
    }


def build_boundary_sources(*, meshes, frame, plan, settings, capture_id,
                           actual_registry=None, weld_requests=None,
                           legacy_chain_parameters=None,
                           include_linker_sources=True, checkpoint=lambda: None,
                           enabled_actuals=None, projection_profile='strict'):
    """Return unclipped source/domain tables; never link or solve new phase.

    ``legacy_chain_parameters`` is an injected callable with the original
    ``strokes.chain_parameters(pairs, lengths)`` contract.  It is diagnostic
    only and is evaluated on each original mesh/group/slot source selection.

    When ``include_linker_sources`` is true, the result also carries a detached
    ``linker_sources`` list whose records have exactly the frozen linker's five
    fields.  Building this adapter does not call the linker, link chains or
    solve phase; it only checks positive-w/finite geometry and exact
    same-domain endpoint xy/w consistency.

    ``enabled_actuals=None`` retains historical selection and qualification.
    An explicit actual set filters only after all groups have assigned ownership,
    and avoids projecting slots without a currently consumed visibility channel.
    ``positive_w_unclipped`` keeps finite/strict-positive-w validation but records
    near margins without rejecting near-plane crossings; it never clips sources.
    """
    _text(capture_id, 'capture_id')
    _need(type(include_linker_sources) is bool,
          'include_linker_sources must be boolean')
    _need(type(projection_profile) is str
          and projection_profile in ('strict', 'positive_w_unclipped'),
          'unknown boundary source projection_profile')
    if enabled_actuals is not None:
        _need(type(enabled_actuals) in (set, frozenset, list, tuple),
              'enabled_actuals must be an explicit actual-ID collection or None')
        enabled_actuals = frozenset(_text(actual, 'enabled actual') for actual in enabled_actuals)
    frame = _normalize_frame(frame)
    mesh_rows = _normalize_mesh_rows(meshes)
    groups, plan = _normalize_groups(plan)
    plan_settings = _normalize_plan_settings(plan)
    runtime_settings = _normalize_runtime_settings(settings)
    aliases = _normalize_aliases(plan, groups)
    actual_registry = _normalize_actual_registry(actual_registry)
    weld_requests = _normalize_weld_requests(weld_requests)

    if actual_registry is not None:
        for (group_id, slot), actual in aliases.items():
            _need(actual in actual_registry,
                  'boundary alias actual is absent from source_bindings: '
                  + actual)

    actual_usage = {}
    source_parts = {
        'source_id': [],
        'mesh_index': [],
        'edge_index': [],
        'vertex_a_index': [],
        'vertex_b_index': [],
        'endpoint_a_id': [],
        'endpoint_b_id': [],
        'group_id': [],
        'slot': [],
        'actual_brush_id': [],
        'channel': [],
        'source_kind': [],
        'category_bits': [],
        'side': [],
        'virtual_edge': [],
        'domain_id': [],
        'linker_domain': [],
        'world': [],
        'projected_pixel': [],
        'clip_w': [],
        'near_margin': [],
        'support': [],
        'corner': [],
        'legacy_chain_phase': [],
    }
    domains = OrderedDict()
    summary_rows = []
    summary_by_key = {}
    total_legacy_chains = 0
    legacy_computed = legacy_chain_parameters is not None
    wire_active = source_routes.active(groups)
    route_actuals = ({(group['id'], slot): _actual_for(group, slot, aliases)
                     for group in groups for slot in group['brush_slots']} if wire_active else None)

    for mesh in mesh_rows:
        checkpoint()
        routed = None
        if wire_active:
            if len(mesh['normals']):
                inverse_view = np.linalg.inv(frame['view'])
                facing = (np.einsum('ij,ij->i', mesh['normals'], inverse_view[:3, 3] - mesh['centers']) >= 0.0
                          if frame['perspective'] else mesh['normals'] @ inverse_view[:3, 2] >= 0.0)
            else:
                facing = np.empty(0, dtype=np.bool_)
            names = np.asarray(mesh['face_material_palette'])[mesh['face_material_ids']]
            route_mesh = source_routes.MeshArrays(mesh['name'], mesh['resolved_kinds'], mesh['counts'],
                mesh['faces0'], mesh['faces1'], names, facing, mesh.get('original_edge_count'), mesh['virtual_edges'],
                mesh.get('edge_predicates'),mesh.get('selector_meta'))
            routed = source_routes.select_plan(route_mesh, groups, route_actuals,
                {gid: control['enabled'] for gid, control in runtime_settings['groups'].items()},
                source_categories=plan_settings['source_categories'], include_open=plan_settings['include_open'],
                visibility=runtime_settings['visibility'])
            kind_gate = routed.kind_gate
            owner, sides = routed.owner, routed.sides
        else:
            kind_gate = np.array(mesh['resolved_kinds'], dtype=np.int8, copy=True)
            if 'smoothing' not in plan_settings['source_categories']:
                kind_gate[kind_gate == 3] = 0
            if 'selected' not in plan_settings['source_categories']:
                kind_gate[np.isin(kind_gate, [1, 2, 4])] = 0
            if not plan_settings['include_open']:
                kind_gate[kind_gate == 2] = 0
            owner, sides = _owner_and_sides(mesh, groups, kind_gate, frame)
        for group_index, group in enumerate(groups):
            control = runtime_settings['groups'].get(group['id'])
            if control is not None and not control['enabled']:
                continue
            if routed is None:
                mask = owner == group_index
                if group['silhouette_enabled'] is False:
                    mask &= kind_gate != 1
                if group['marked_enabled'] is False:
                    mask &= kind_gate != 4
                if group['smoothing_enabled'] is False:
                    mask &= kind_gate != 3
                slots = np.full(len(kind_gate), 'brush', dtype=object)
                for kind, slot in ((1, 'silhouette_brush'),
                                   (3, 'smoothing_brush'),
                                   (4, 'marked_brush')):
                    if slot in group['brushes']:
                        slots[kind_gate == kind] = slot
                slot_selections = ((slot, None) for slot in group['brush_slots'])
            else:
                slot_selections = ((selection.slot, selection) for selection in routed.selections
                                   if selection.group_index == group_index)
            for slot, selection in slot_selections:
                ids = np.flatnonzero(mask & (slots == slot)) if selection is None else selection.ids
                if not len(ids):
                    continue
                actual = _actual_for(group, slot, aliases) if selection is None else selection.actual
                if enabled_actuals is not None:
                    if actual not in enabled_actuals:
                        continue
                    if selection is None and not any(
                            (group['visible_enabled'] if channel == 'VISIBLE'
                             else group['hidden_enabled'])
                            and runtime_settings['visibility'] in (channel, 'BOTH')
                            for channel in CHANNELS):
                        continue
                if actual_registry is not None:
                    _need(actual in actual_registry,
                          'source actual is absent from source_bindings: ' + actual)
                pairs = np.asarray(mesh['edges'][ids], dtype=np.int64)
                world = np.array(mesh['vertices'][pairs], dtype=np.float64, copy=True)
                location = 'mesh_index=%d edge_index=%d group=%s slot=%s' % (
                    mesh['mesh_index'], int(ids[0]), group['id'], slot)
                pixel, clip_w, near_margin, legacy_xy = _project_strict(
                    world, frame['mvp'], frame['width'], frame['height'], location,
                    projection_profile=projection_profile)
                supports = np.where(
                    mesh['triangle_supports'][ids] >= 0,
                    mesh['triangle_supports'][ids] + mesh['face_offset'],
                    0,
                ).astype(np.float32)
                corners = np.where(
                    sides[ids, None] == 0,
                    mesh['corners0'][ids],
                    mesh['corners1'][ids],
                ).astype(np.int32, copy=True)
                if legacy_computed:
                    chain_phase, chain_count = _legacy_parameters(
                        legacy_chain_parameters, pairs, legacy_xy, location)
                    total_legacy_chains += chain_count
                else:
                    chain_phase = np.empty((0, 2), dtype=np.float64)
                    chain_count = None
                source_kinds = (np.asarray(mesh['resolved_kinds'][ids], dtype=np.int8)
                                if selection is None else selection.source_kinds)
                category_bits = _category_bits(source_kinds) if selection is None else selection.category_bits
                virtual = np.array(mesh['virtual_edges'][ids], dtype=np.bool_, copy=True)
                for channel in (CHANNELS if selection is None else (selection.channel,)):
                    enabled = (group['visible_enabled'] if channel == 'VISIBLE'
                               else group['hidden_enabled'])
                    if ((selection is None and not enabled)
                            or runtime_settings['visibility'] not in (channel, 'BOTH')):
                        continue
                    domain_id = _digest('bnddom1-', [
                        capture_id, mesh['mesh_index'], group['id'], actual, channel,
                    ])
                    domain_key = (mesh['mesh_index'], group['id'], actual, channel)
                    domain = domains.get(domain_key)
                    if domain is None:
                        domain = _new_domain(group, actual, channel, mesh, domain_id)
                        domains[domain_key] = domain
                    _need(domain['domain_id'] == domain_id,
                          'internal domain identity collision')
                    start = len(source_parts['source_id'])
                    count = len(ids)
                    for local, edge_id in enumerate(ids):
                        if local % 256 == 0:checkpoint()
                        source_id = _digest('bndsrc1-', [
                            capture_id, mesh['mesh_index'], int(edge_id),
                            group['id'], actual, channel,
                        ])
                        source_parts['source_id'].append(source_id)
                        source_parts['mesh_index'].append(mesh['mesh_index'])
                        source_parts['edge_index'].append(int(edge_id))
                        source_parts['vertex_a_index'].append(int(pairs[local, 0]))
                        source_parts['vertex_b_index'].append(int(pairs[local, 1]))
                        source_parts['endpoint_a_id'].append(
                            'mesh:%d/vertex:%d' % (mesh['mesh_index'], int(pairs[local, 0])))
                        source_parts['endpoint_b_id'].append(
                            'mesh:%d/vertex:%d' % (mesh['mesh_index'], int(pairs[local, 1])))
                        source_parts['group_id'].append(group['id'])
                        source_parts['slot'].append(slot)
                        source_parts['actual_brush_id'].append(actual)
                        source_parts['channel'].append(channel)
                        source_parts['source_kind'].append(int(source_kinds[local]))
                        source_parts['category_bits'].append(int(category_bits[local]))
                        source_parts['side'].append(int(sides[edge_id]))
                        source_parts['virtual_edge'].append(bool(virtual[local]))
                        source_parts['domain_id'].append(domain_id)
                        source_parts['linker_domain'].append(domain['linker_domain'])
                    source_parts['world'].append(world)
                    source_parts['projected_pixel'].append(pixel)
                    source_parts['clip_w'].append(clip_w)
                    source_parts['near_margin'].append(near_margin)
                    source_parts['support'].append(supports)
                    source_parts['corner'].append(corners)
                    source_parts['legacy_chain_phase'].append(
                        chain_phase if legacy_computed else np.empty((0, 2)))
                    stop = start + count
                    domain['_ranges'].append((start, stop))
                    domain['source_count'] += count
                    domain['virtual_source_edges'] += int(np.count_nonzero(virtual))
                    if slot not in domain['slots']:
                        domain['slots'].append(slot)
                    key = (mesh['name'], group['id'], slot, actual, channel)
                    row = summary_by_key.get(key)
                    if row is None:
                        row = {
                            'object': mesh['name'],
                            'mesh_index': mesh['mesh_index'],
                            'group': group['id'],
                            'slot': slot,
                            'actual_brush': actual,
                            'channel': channel,
                            'source_edges': 0,
                            'virtual_source_edges': 0,
                            'source_records': 0,
                            'domain_ids': [],
                        }
                        summary_by_key[key] = row
                        summary_rows.append(row)
                    if domain_id not in row['domain_ids']:
                        row['domain_ids'].append(domain_id)
                    row['source_edges'] += count
                    row['virtual_source_edges'] += int(np.count_nonzero(virtual))
                    row['source_records'] += count
                    usage = actual_usage.setdefault(actual, {
                        'actual_brush_id': actual,
                        'source_brush_key': (actual_registry or {}).get(actual, {}).get(
                            'source_brush_key', ''),
                        'source_detail_key': (actual_registry or {}).get(actual, {}).get(
                            'source_detail_key', ''),
                        'groups': [],
                        'slots': [],
                        'meshes': [],
                        'source_records': 0,
                    })
                    for field, value in (('groups', group['id']),
                                         ('slots', slot),
                                         ('meshes', mesh['mesh_index'])):
                        if value not in usage[field]:
                            usage[field].append(value)
                    usage['source_records'] += count

    def concat(name, dtype=None):
        parts = source_parts[name]
        if not parts:
            shape = (0,) if name not in (
                'world', 'projected_pixel', 'clip_w', 'near_margin',
                'support', 'corner', 'legacy_chain_phase') else {
                    'world': (0, 2, 3),
                    'projected_pixel': (0, 2, 2),
                    'clip_w': (0, 2),
                    'near_margin': (0, 2),
                    'support': (0, 2),
                    'corner': (0, 2),
                    'legacy_chain_phase': (0, 2),
                }[name]
            if name in ('source_id', 'endpoint_a_id', 'endpoint_b_id',
                        'group_id', 'slot', 'actual_brush_id', 'channel', 'domain_id',
                        'linker_domain'):
                return np.empty(0, dtype='<U1')
            return np.empty(shape, dtype=dtype)
        if name in ('source_id', 'endpoint_a_id', 'endpoint_b_id',
                    'group_id', 'slot', 'actual_brush_id', 'channel', 'domain_id',
                    'linker_domain'):
            return _string_array(parts)
        if name == 'legacy_chain_phase' and not legacy_computed:
            return np.empty((0, 2), dtype=np.float64)
        return np.concatenate(parts, axis=0)

    sources = {
        'source_id': concat('source_id'),
        'mesh_index': np.asarray(source_parts['mesh_index'], dtype=np.int64),
        'edge_index': np.asarray(source_parts['edge_index'], dtype=np.int64),
        'vertex_a_index': np.asarray(source_parts['vertex_a_index'], dtype=np.int64),
        'vertex_b_index': np.asarray(source_parts['vertex_b_index'], dtype=np.int64),
        'endpoint_a_id': concat('endpoint_a_id'),
        'endpoint_b_id': concat('endpoint_b_id'),
        'group_id': concat('group_id'),
        'slot': concat('slot'),
        'actual_brush_id': concat('actual_brush_id'),
        'channel': concat('channel'),
        'source_kind': np.asarray(source_parts['source_kind'], dtype=np.int8),
        'category_bits': np.asarray(source_parts['category_bits'], dtype=np.uint16),
        'side': np.asarray(source_parts['side'], dtype=np.int8),
        'virtual_edge': np.asarray(source_parts['virtual_edge'], dtype=np.bool_),
        'domain_id': concat('domain_id'),
        'linker_domain': concat('linker_domain'),
        'world': concat('world', np.float64),
        'projected_pixel': concat('projected_pixel', np.float64),
        'clip_w': concat('clip_w', np.float64),
        'near_margin': concat('near_margin', np.float64),
        'support': concat('support', np.float32),
        'corner': concat('corner', np.int32),
    }
    if legacy_computed:
        sources['legacy_chain_phase'] = concat('legacy_chain_phase', np.float64)
    count = len(sources['source_id'])
    for name, values in sources.items():
        _need(len(values) == count,
              'source table field %s has %d rows, expected %d'
              % (name, len(values), count))
    _need(len(set(sources['source_id'].tolist())) == count,
          'source_id values are not unique')

    domain_rows = []
    for domain in domains.values():
        indices = np.concatenate(
            [np.arange(start, stop, dtype=np.int64) for start, stop in domain['_ranges']]
        ) if domain['_ranges'] else np.empty(0, dtype=np.int64)
        _need(len(indices) == domain['source_count'],
              'domain source range count mismatch')
        _need(bool(np.all(np.diff(indices) > 0)),
              'domain source indices were reordered')
        domain_rows.append({
            'domain_id': domain['domain_id'],
            'linker_domain': domain['linker_domain'],
            'group_id': domain['group_id'],
            'actual_brush_id': domain['actual_brush_id'],
            'channel': domain['channel'],
            'mesh_index': domain['mesh_index'],
            'mesh_name': domain['mesh_name'],
            'priority': domain['priority'],
            'slots': tuple(domain['slots']),
            'source_indices': indices,
            'source_count': domain['source_count'],
            'virtual_source_edges': domain['virtual_source_edges'],
        })
    domain_rows.sort(key=lambda row: (
        row['mesh_index'], -row['priority'], row['group_id'],
        row['actual_brush_id'], row['channel'],
    ))
    _need(len({row['domain_id'] for row in domain_rows}) == len(domain_rows),
          'domain_id values are not unique')
    _need(sum(row['source_count'] for row in domain_rows) == count,
          'domain source counts do not cover the source table')
    _need(sum(row['source_edges'] for row in summary_rows) == count,
          'summary source counts do not cover the source table')

    linker_meta = {
        'schema': LINKER_ADAPTER_SCHEMA,
        'target_schema': LINKER_SCHEMA,
        'target_entry': 'build_chains',
        'target_signature': (
            'build_chains(sources, *, max_gap_px=0, '
            'candidate_budget=1000000, checkpoint=...)'),
        'source_fields_exact': list(LINKER_SOURCE_FIELDS),
        'source_limit': LINKER_MAX_SOURCES,
        'identifier_limit': LINKER_MAX_IDENTIFIER_LENGTH,
        'domain_encoding': (
            'bnddom-v1:' + json.dumps(
                ['<group_id>', '<actual_brush_id>', '<channel>', '<mesh_index>'],
                ensure_ascii=False, separators=(',', ':'),
            )),
        'domain_key_field': 'linker_domain',
        'xy': 'projected_pixel, bottom-left full-MVP pixels, original unclipped chord',
        'w': 'clip_w, positive full-MVP homogeneous w',
        'endpoint_scope': (
            'same (domain, endpoint_id) must have exactly equal received xy/w'),
        'link_performed': False,
        'phase_computed': False,
        'checkpoint_executed': False,
        'adapter_present': include_linker_sources,
        'source_count': count if include_linker_sources else None,
        'domain_count': len(domain_rows),
    }
    style_reference = {
        'authority': 'current compiled plan',
        'lookup': "plan['groups'] entry where id == source.group_id; brushes[source.slot]",
        'fields': [
            'size_curve', 'alpha_curve', 'width_map', 'alpha_map',
            'blend_amount', 'color', 'native_size',
        ],
        'full_preset_curve_override': False,
        'source_group_field': 'group_id',
        'source_slot_field': 'slot',
        'note': (
            'The source/domain/linker tables do not replace current authored '
            'curve, map, width or alpha with full-preset source values.'),
    }
    linker_sources = None
    if include_linker_sources:
        _need(count <= LINKER_MAX_SOURCES,
              'linker source limit exceeded; build full records without adapter')
        domain_by_linker = {row['linker_domain']: row for row in domain_rows}
        _need(len(domain_by_linker) == len(domain_rows),
              'linker domain encoding is not unique')
        endpoint_geometry = {}
        linker_sources = []
        for index in range(count):
            source_id = str(sources['source_id'][index])
            endpoint_ids = [
                str(sources['endpoint_a_id'][index]),
                str(sources['endpoint_b_id'][index]),
            ]
            xy = [
                [float(sources['projected_pixel'][index, end, 0]),
                 float(sources['projected_pixel'][index, end, 1])]
                for end in (0, 1)
            ]
            w = [float(sources['clip_w'][index, 0]),
                 float(sources['clip_w'][index, 1])]
            domain = str(sources['linker_domain'][index])
            _text(source_id, 'linker source source_id')
            _text(domain, 'linker source domain')
            _need(len(source_id) <= LINKER_MAX_IDENTIFIER_LENGTH
                  and len(domain) <= LINKER_MAX_IDENTIFIER_LENGTH,
                  'linker source identifier exceeds 256 characters')
            for endpoint_id in endpoint_ids:
                _text(endpoint_id, 'linker source endpoint_id')
                _need(len(endpoint_id) <= LINKER_MAX_IDENTIFIER_LENGTH,
                      'linker endpoint identifier exceeds 256 characters')
            _need(all(math.isfinite(value) for point in xy for value in point),
                  'linker source xy must be finite')
            _need(all(math.isfinite(value) and value > 0.0 for value in w),
                  'linker source w must be finite and positive')
            _need(domain in domain_by_linker,
                  'linker source domain is absent from the domain table')
            domain_row = domain_by_linker[domain]
            _need(
                domain_row['mesh_index'] == int(sources['mesh_index'][index])
                and domain_row['group_id'] == str(sources['group_id'][index])
                and domain_row['actual_brush_id']
                == str(sources['actual_brush_id'][index])
                and domain_row['channel'] == str(sources['channel'][index]),
                'linker domain fields disagree with its full source record',
            )
            for end, endpoint_id in enumerate(endpoint_ids):
                key = (domain, endpoint_id)
                geometry = (tuple(xy[end]), w[end])
                _need(key not in endpoint_geometry
                      or endpoint_geometry[key] == geometry,
                      'same-domain linker endpoint xy/w is not exactly equal')
                endpoint_geometry[key] = geometry
            linker_source = {
                'source_id': source_id,
                'endpoint_ids': endpoint_ids,
                'xy': xy,
                'w': w,
                'domain': domain,
            }
            _need(set(linker_source) == set(LINKER_SOURCE_FIELDS),
                  'linker source fields differ from the frozen contract')
            linker_sources.append(linker_source)
        linker_meta['adapter_source_count'] = len(linker_sources)
        linker_meta['same_domain_endpoint_geometry_exact'] = True
    else:
        linker_meta['same_domain_endpoint_geometry_exact'] = None

    used_actuals = set(actual_usage)
    actual_rows = []
    registry_keys = sorted(actual_registry) if actual_registry is not None else sorted(used_actuals)
    for actual in registry_keys:
        usage = actual_usage.get(actual)
        row = {
            'actual_brush_id': actual,
            'source_brush_key': (actual_registry or {}).get(actual, {}).get(
                'source_brush_key', ''),
            'source_detail_key': (actual_registry or {}).get(actual, {}).get(
                'source_detail_key', ''),
            'used': usage is not None,
            'groups': list(usage['groups']) if usage else [],
            'slots': list(usage['slots']) if usage else [],
            'meshes': list(usage['meshes']) if usage else [],
            'source_records': usage['source_records'] if usage else 0,
        }
        actual_rows.append(row)

    slot_source_counts = {}
    for row in summary_rows:
        key = (row['group'], row['slot'])
        slot_source_counts[key] = slot_source_counts.get(key, 0) + row['source_records']
    actual_source_by_slot = {}
    actual_registered_by_slot = {}
    unregistered_slot_fallbacks = []
    for group in groups:
        for slot in group['brush_slots']:
            actual = _actual_for(group, slot, aliases)
            has_alias = (group['id'], slot) in aliases
            registered = (actual in actual_registry) if actual_registry is not None else None
            actual_source_by_slot[(group['id'], slot)] = (
                'boundary_alias' if has_alias
                else 'editor_uid_fallback' if group['brush_meta'][slot].get('uid')
                else 'slot_name_fallback')
            actual_registered_by_slot[(group['id'], slot)] = registered
            if actual_registry is not None and not registered:
                unregistered_slot_fallbacks.append({
                    'group_id': group['id'],
                    'slot': slot,
                    'actual_brush_id': actual,
                    'source_records': slot_source_counts.get((group['id'], slot), 0),
                    'reason': (
                        'disabled/no emitted source; original editor UID fallback'
                        if not slot_source_counts.get((group['id'], slot), 0)
                        else 'emitted source actual is not registered'),
                })
    emitted_actuals = sorted(used_actuals)
    emitted_actuals_all_registered = (
        actual_registry is not None and used_actuals <= set(actual_registry))
    if actual_registry is not None:
        _need(emitted_actuals_all_registered,
              'an emitted source actual is absent from source_bindings')
        _need(all(row['source_records'] == 0
                  for row in unregistered_slot_fallbacks),
              'unregistered slot fallback emitted a source record')

    by_channel = {channel: int(np.count_nonzero(sources['channel'] == channel))
                  for channel in CHANNELS}
    by_slot = {}
    for slot in sorted(set(sources['slot'].tolist())):
        by_slot[slot] = int(np.count_nonzero(sources['slot'] == slot))
    by_mesh = {}
    for mesh in mesh_rows:
        by_mesh[str(mesh['mesh_index'])] = int(
            np.count_nonzero(sources['mesh_index'] == mesh['mesh_index']))
    mesh_rows_out = [{
        'mesh_index': mesh['mesh_index'],
        'name': mesh['name'],
        'face_offset': mesh['face_offset'],
        'vertices': len(mesh['vertices']),
        'edges': len(mesh['edges']),
        'triangles': mesh['triangle_count'],
        'sha256': mesh['sha256'],
    } for mesh in mesh_rows]
    group_rows = []
    for group in groups:
        control = runtime_settings['groups'].get(group['id'])
        weld = None
        if weld_requests is not None:
            matches = [row for row in weld_requests if row['group_id'] == group['id']]
            if len(matches) == 1:
                weld = bool(matches[0]['requested'])
            elif len(matches) > 1:
                raise BoundarySourceError(
                    'multiple weld requests for group ' + group['id'])
        group_rows.append({
            'group_id': group['id'],
            'priority': group['priority'],
            'runtime_enabled': True if control is None else control['enabled'],
            'visible_enabled': group['visible_enabled'],
            'hidden_enabled': group['hidden_enabled'],
            'silhouette_enabled': group['silhouette_enabled'],
            'marked_enabled': group['marked_enabled'],
            'smoothing_enabled': group['smoothing_enabled'],
            'object_ids': tuple(group['object_ids']),
            'material_names': tuple(group['material_names']),
            'brush_slots': tuple(group['brush_slots']),
            'actuals_by_slot': {slot: _actual_for(group, slot, aliases)
                                for slot in group['brush_slots']},
            'actual_source_by_slot': {
                slot: actual_source_by_slot[(group['id'], slot)]
                for slot in group['brush_slots']
            },
            'actual_registered_by_slot': {
                slot: actual_registered_by_slot[(group['id'], slot)]
                for slot in group['brush_slots']
            },
            'source_records_by_slot': {
                slot: slot_source_counts.get((group['id'], slot), 0)
                for slot in group['brush_slots']
            },
            'native_weld_requested': weld,
        })

    summary = {
        'schema': SCHEMA,
        'capture_id': capture_id,
        'counts': {
            'meshes': len(mesh_rows),
            'groups': len(groups),
            'domains': len(domain_rows),
            'sources': count,
            'by_channel': by_channel,
            'by_slot': by_slot,
            'by_mesh': by_mesh,
            'legacy_chains': total_legacy_chains if legacy_computed else None,
            'linker_sources': len(linker_sources) if linker_sources is not None else None,
        },
        'rows': summary_rows,
        'total_source_edges': sum(row['source_edges'] for row in summary_rows),
        'total_virtual_source_edges': sum(
            row['virtual_source_edges'] for row in summary_rows),
        'style_report_comparable_fields': [
            'object', 'group', 'slot', 'actual_brush', 'channel',
            'source_edges', 'virtual_source_edges',
        ],
        'not_claimed': [
            'native category equivalence',
            'link or chain connectivity',
            'new chain phase',
            'shape or stamp changes',
            'native weld execution',
            'raster max-bucket merge',
        ],
        'actual_identity': {
            'known_actuals': len(actual_registry) if actual_registry is not None else None,
            'emitted_actuals': emitted_actuals,
            'emitted_actuals_all_registered': emitted_actuals_all_registered,
            'registry_unused_actuals': sorted(
                set(actual_registry) - used_actuals) if actual_registry is not None else [],
            'unregistered_disabled_slot_fallbacks': unregistered_slot_fallbacks,
        },
        'linker': linker_meta,
        'style_reference': style_reference,
    }
    if weld_requests is None:
        weld_block = {
            'status': 'UNKNOWN_NOT_SUPPLIED',
            'executed': False,
            'cross_mesh_merge': False,
            'requests': [],
        }
    else:
        requested = [row for row in weld_requests if row['requested']]
        weld_block = {
            'status': 'NOT_EXECUTED',
            'executed': False,
            'cross_mesh_merge': False,
            'requested_count': len(requested),
            'requests': weld_requests,
        }
    qualification = {
        'native_equivalent': False,
        'scope': 'captured unclipped boundary source records only',
        'classification_profile': source_routes.PROFILE if wire_active else CLASSIFICATION,
        'category_source': ('explicit autonomous original-edge wire routes and legacy resolved_kinds; color layers are not read'
                            if wire_active else 'captured resolved_kinds; color layers are not read'),
        'emitted_actuals_are_source_binding_registered': emitted_actuals_all_registered,
        'source_identity_scope': (
            'capture-bound source_id; not a cross-frame or production instance identity'),
        'mesh_identity_caveat': (
            'captured names are labels in this sample; production must use instance identity'),
        'phase_solved': False,
        'link_performed': False,
        'shape_or_stamp_applied': False,
        'raster_max_bucket_merged': False,
        'source_text_executed': False,
        'blender_or_gpu_called': False,
        'projection': {
            'matrix': 'full MVP',
            'pixel_origin': 'bottom-left',
            'clipping_applied': False,
            'strict_nonfinite_or_nonpositive_w': True,
            'strict_positive_w_near_plane': projection_profile == 'strict',
            'phase_not_patched_by_clip_length': True,
        },
        'legacy_chain_phase': {
            'computed': legacy_computed,
            'function': 'injected strokes.chain_parameters' if legacy_computed
            else 'not supplied',
            'role': 'diagnostic only; not a new phase or link result',
        },
        'weld': weld_block,
        'meshes_never_cross_welded': True,
        'linker_adapter': {
            'present': include_linker_sources,
            'target_schema': LINKER_SCHEMA,
            'source_count': len(linker_sources) if linker_sources is not None else None,
            'source_limit': LINKER_MAX_SOURCES,
            'same_domain_endpoint_geometry_exact': (
                True if include_linker_sources else None),
            'link_performed': False,
            'phase_computed': False,
            'checkpoint_executed': False,
        },
        'style_reference': style_reference,
    }
    if projection_profile != 'strict':
        qualification['projection']['profile'] = projection_profile
        qualification['projection']['near_margin_recorded'] = True
        qualification['projection']['near_clip_deferred_to_original_source_consumer'] = True
    if enabled_actuals is not None:
        qualification['source_selection'] = {
            'enabled_actuals': sorted(enabled_actuals),
            'ownership_assigned_before_actual_filter': True,
            'only_currently_consumed_channels_projected': True,
        }
    result = {
        'schema': SCHEMA,
        'source_schema': SOURCE_SCHEMA,
        'domain_schema': DOMAIN_SCHEMA,
        'linker_adapter_schema': LINKER_ADAPTER_SCHEMA,
        'linker_target_schema': LINKER_SCHEMA,
        'capture_id': capture_id,
        'sources': sources,
        'domains': domain_rows,
        'summary': summary,
        'meshes': mesh_rows_out,
        'groups': group_rows,
        'actuals': actual_rows,
        'linker': linker_meta,
        'style_reference': style_reference,
        'qualification': qualification,
    }
    if linker_sources is not None:
        result['linker_sources'] = linker_sources
    return result
