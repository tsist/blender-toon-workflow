# SPDX-License-Identifier: GPL-3.0-or-later
"""Vector file output for resolved line layers.

The EPS/AIEPS writers consume the same styled, clipped stroke fragments as the
line renderer, then split each authored visible/hidden channel with a CPU
equivalent of the shader's triangle-BVH centerline visibility query.  EPS has
no per-stroke alpha, brush texture or coverage/AA representation; those losses
are stated in every report and in the EPS comments.

PLD is a proprietary Pencil+ format.  No public specification or writer was
found in the official GPL pyd source (the vector outputs are passed through to
the closed render application), so PLD is reported as blocked instead of
writing a renamed JSON/placeholder file.
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path
import uuid

import numpy as np

from . import render_elements

SCHEMA = 'toon.vector-output-request.v1'
REPORT_SCHEMA = 'toon.vector-output-report.v1'
EPS_SCHEMA = 'toon.vector.eps.v1'
VECTOR_PROP = 'toon_vector_outputs'
FILE_TYPES = ('AIEPS', 'EPS', 'PLD')
GROUPING_MODES = ('PRESERVE', 'LINE', 'LINE_SET', 'CHANNEL', 'NONE')
MAX_OUTPUTS = 32
SAMPLE_STEP_PX = 2.0
MAX_SAMPLES_PER_PRIMITIVE = 2048
_PLACEHOLDER = re.compile(r'_?<LineName>', re.IGNORECASE)
_HASH_RUN = re.compile(r'#+')
_INVALID_WINDOWS = set('<>:"|?*')
_LOSS = {
    'color': 'Scene-linear RGB is converted to sRGB device RGB for EPS; no ICC profile or soft-proof is embedded.',
    'color_gradient': 'Per-run RGB endpoint gradients are reduced to the mean linear color; no EPS shading or gradient fill is emitted.',
    'alpha': 'EPS/AIEPS has no per-stroke alpha; resolved alpha is discarded and every fill is opaque.',
    'texture': 'Brush maps, color-range textures and map opacity are not represented in EPS/AIEPS.',
    'coverage': 'Coverage profiles, feathering, supersampling and post-filtering are not AA-represented.',
    'shape': 'Brush stamp/stretch/angle/random shape and native stamp-derived envelopes are not represented; only the resolved centerline width envelope is written.',
    'brush_effects': 'Unknown brush_effects keys are not guessed; recognized rgb is consumed, unsupported keys are listed in source_data.unsupported_brush_effects. rgb_endpoints is preferred over rgb and brush_effects.rgb.',
    'brush_effect_rows': 'Mainline brush_effect_rows/pack (shape, stamp, UV/map/random data) are not serialized; layer.projected/world already contain source UV distortion, but native brush stamping is lost.',
    'width': 'Width is preserved as a filled per-sample envelope; joins use a local tangent estimate, not native stroke joins.',
    'zorder': 'Default PRESERVE keeps build_layers paint order. Explicit LINE/LINE_SET/CHANNEL/NONE grouping may reorder or flatten the stream and is reported per file.',
    'visibility': 'Visibility is sampled on the centerline (nominal 2 px, capped at 2048 samples per primitive, bisection-refined); width-dependent and exact rational edge occlusion are not claimed.',
    'hidden_promotion': 'Line-function table x>=0.5 promotion ignores that occluder during CPU lineState traversal; native line-function semantics are not claimed.',
    'holdout': 'Heldout sources are excluded by the builder; table y or zero width/alpha encode mask/holdout, and native holdout occlusion is not claimed.',
    'border': 'The full frame is projected; when both render border and crop-to-border are enabled, the EPS is clipped to the border rectangle.',
}
_PLD_SOURCES = [
    'https://docs.psoft.co.jp/plb400w/en/latest/reference/FileOutput.html',
    'https://github.com/psofthouse/Pencil-4-Line-for-Blender-pyd-source',
]
_PLD_REASON = ('PLD is a proprietary Pencil+ format. The official documentation '
               'describes only the UI and After Effects use, and the official GPL pyd '
               'source does not contain a PLD serializer (vector outputs are passed to '
               'the closed Render App). No public byte-level specification or sample '
               'reader was available, so no .pld file was fabricated.')


def loss_manifest():
    """Return the explicit, stable vector-loss statement."""
    return dict(_LOSS)


def pld_status():
    return dict(available=False, format='PLD', reason=_PLD_REASON, sources=list(_PLD_SOURCES))


class VectorOutputError(ValueError):
    """Invalid vector request or unsupported vector format."""


class VectorFormatUnavailable(VectorOutputError):
    """A recognized format exists in the UI but cannot be serialized here."""


def _bool(value, name):
    if type(value) is not bool:
        raise VectorOutputError(name + ' must be boolean')
    return value


def _string(value, name, *, maximum=1024):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VectorOutputError(name + ' must be a non-empty string of at most ' + str(maximum) + ' characters')
    if '\x00' in value:
        raise VectorOutputError(name + ' cannot contain NUL')
    return value


def _line_set_ids(value):
    if not isinstance(value, (list, tuple)) or len(value) != 8:
        raise VectorOutputError('line_set_ids requires eight booleans')
    return [_bool(item, 'line_set_ids') for item in value]


def _category_default(name):
    return True


def normalize_outputs(outputs):
    """Validate one public output list, independent of scene RNA."""
    if hasattr(outputs, 'vector_outputs') and not isinstance(outputs, (list, tuple)):
        outputs = outputs.vector_outputs
    if not isinstance(outputs, (list, tuple)):
        try:
            outputs = list(outputs)
        except TypeError as error:
            raise VectorOutputError('Vector outputs must be a sequence') from error
    if not 1 <= len(outputs) <= MAX_OUTPUTS:
        raise VectorOutputError('Vector outputs require 1..' + str(MAX_OUTPUTS) + ' entries')
    allowed = {'on', 'sub_path', 'file_type', 'visible_lines_on', 'hidden_lines_on',
               'line_set_ids', 'label', 'grouping', 'line_ids'} | {
                   name + '_on' for name in render_elements.CATEGORIES}
    result = []
    for index, raw in enumerate(outputs):
        if not isinstance(raw, dict) and hasattr(raw, 'file_type') and hasattr(raw, 'line_set_ids'):
            raw = dict(
                on=raw.on, sub_path=raw.sub_path, file_type=raw.file_type,
                visible_lines_on=raw.visible_lines_on,
                hidden_lines_on=raw.hidden_lines_on,
                line_set_ids=list(raw.line_set_ids), label=getattr(raw, 'label', ''),
                grouping=getattr(raw, 'grouping', 'PRESERVE'), line_ids=None,
                **{name + '_on': getattr(raw, name + '_on')
                   for name in render_elements.CATEGORIES})
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise VectorOutputError('Unknown vector output fields at index ' + str(index))
        file_type = raw.get('file_type', 'AIEPS')
        if file_type not in FILE_TYPES:
            raise VectorOutputError('Unknown vector file type at index ' + str(index))
        label = raw.get('label', '')
        if label != '':
            _string(label, 'label', maximum=256)
        sub_path = _string(raw.get('sub_path', 'Line'), 'sub_path', maximum=1024)
        on = _bool(raw.get('on', True), 'on')
        grouping = raw.get('grouping', 'PRESERVE')
        if grouping not in GROUPING_MODES:
            raise VectorOutputError('Unknown vector grouping mode at index ' + str(index))
        line_ids = raw.get('line_ids')
        if line_ids is not None:
            if not isinstance(line_ids, (list, tuple)) or not line_ids:
                raise VectorOutputError('line_ids must be a non-empty sequence')
            line_ids = list(line_ids)
            if any(type(value) not in (int, str) or value == '' for value in line_ids):
                raise VectorOutputError('line_ids must contain non-empty strings or integers')
            if len(set(line_ids)) != len(line_ids):
                raise VectorOutputError('line_ids must be unique')
        row = dict(
            index=index,
            label=label,
            on=on,
            sub_path=sub_path,
            file_type=file_type,
            visible_lines_on=_bool(raw.get('visible_lines_on', True), 'visible_lines_on'),
            hidden_lines_on=_bool(raw.get('hidden_lines_on', True), 'hidden_lines_on'),
            line_set_ids=_line_set_ids(raw.get('line_set_ids', [True] * 8)),
            grouping=grouping,
            line_ids=line_ids,
        )
        for name in render_elements.CATEGORIES:
            row[name + '_on'] = _bool(raw.get(name + '_on', _category_default(name)), name + '_on')
        result.append(row)
    return result


def outputs_from_state(state):
    """Convert the persisted RNA collection to the public plain-dict form."""
    result = []
    for index, item in enumerate(state.vector_outputs):
        row = dict(index=index, label=item.label, on=item.on, sub_path=item.sub_path,
                   file_type=item.file_type, visible_lines_on=item.visible_lines_on,
                   hidden_lines_on=item.hidden_lines_on, line_set_ids=list(item.line_set_ids),
                   grouping=getattr(item, 'grouping', 'PRESERVE'), line_ids=None)
        for name in render_elements.CATEGORIES:
            row[name + '_on'] = getattr(item, name + '_on')
        result.append(row)
    return result


def vector_state(scene,*,view_layer=None,node_ptr=''):
    """Resolve a real owner; omitted scope retains the legacy Scene API."""
    from . import vector_scope
    try:return vector_scope.resolve(scene,view_layer=view_layer,node_ptr=node_ptr)[0]
    except vector_scope.VectorScopeError as error:raise VectorOutputError(str(error)) from error


def _line_hierarchy(plan):
    """Read plan.raw.request.line_hierarchy.lines without inventing Line IDs."""
    if plan is None:
        return dict(present=False, source=None, lines=[], map={}, enabled=[])
    raw = plan.get('raw') if isinstance(plan, dict) else None
    request = raw.get('request') if isinstance(raw, dict) else None
    block = request.get('line_hierarchy') if isinstance(request, dict) else None
    if not isinstance(block, dict):
        return dict(present=False, source=None, lines=[], map={}, enabled=[])
    source = block.get('lines')
    if not isinstance(source, list):
        raise VectorOutputError('line_hierarchy.lines must be a list')
    lines = []
    seen = set()
    for index, row in enumerate(source):
        if not isinstance(row, dict):
            raise VectorOutputError('line_hierarchy line %d must be an object' % index)
        line_id = row.get('id')
        if type(line_id) not in (int, str) or line_id == '':
            raise VectorOutputError('line_hierarchy line %d has an invalid id' % index)
        if line_id in seen:
            raise VectorOutputError('duplicate line_hierarchy id: ' + str(line_id))
        seen.add(line_id)
        name = row.get('name')
        if not isinstance(name, str) or not name:
            raise VectorOutputError('line_hierarchy line %d has an invalid name' % index)
        priority = row.get('priority')
        if type(priority) not in (int, float) or not math.isfinite(float(priority)):
            raise VectorOutputError('line_hierarchy line %d has an invalid priority' % index)
        enabled = row.get('enabled')
        if type(enabled) is not bool:
            raise VectorOutputError('line_hierarchy line %d enabled must be boolean' % index)
        slots = row.get('slots')
        if not isinstance(slots, list) or any(
                slot is not None and (not isinstance(slot, str) or not slot) for slot in slots):
            raise VectorOutputError('line_hierarchy line %d has invalid slots' % index)
        lines.append(dict(id=line_id, name=name, priority=priority,
                          enabled=enabled, slots=list(slots)))
    return dict(present=True, source='plan.raw.request.line_hierarchy.lines',
                lines=lines, map={row['id']: row for row in lines},
                enabled=[row['id'] for row in lines if row['enabled']])


def _line_for_layer(layer, hierarchy):
    if not hierarchy['present']:
        if layer.get('line_id') is not None:
            return None
        return None
    line_id = layer.get('line_id')
    if line_id is None:
        return None
    line = hierarchy['map'].get(line_id)
    if line is None:
        raise VectorOutputError('layer line_id is not present in line_hierarchy: ' + str(line_id))
    return line


def _line_name_for_item(item, hierarchy):
    if not hierarchy['present']:
        raise VectorOutputError('<LineName> requires plan.raw.request.line_hierarchy; group or line_set_id cannot substitute')
    requested = item.get('line_ids')
    candidates = hierarchy['lines'] if requested is None else [
        hierarchy['map'][value] for value in requested if value in hierarchy['map']]
    if requested is not None and len(candidates) != len(requested):
        missing = sorted(str(value) for value in requested if value not in hierarchy['map'])
        raise VectorOutputError('line_ids not present in line_hierarchy: ' + ', '.join(missing))
    enabled = [row for row in candidates if row['enabled']]
    if len(enabled) != 1:
        raise VectorOutputError('<LineName> requires exactly one enabled selected Line, got ' + str(len(enabled)))
    return enabled[0]['name']


def _owning_scene(item):
    from . import vector_scope
    scene,state,index,_=vector_scope.owner_for_item(item)
    return scene,state,index


def _frame_subpath(sub_path, frame, file_type, *, line_name=None):
    text = sub_path
    if _PLACEHOLDER.search(text):
        if not line_name:
            raise VectorOutputError('sub_path uses <LineName> but no actual line name is available')
        text = _PLACEHOLDER.sub(line_name, text)
    text = _HASH_RUN.sub(lambda match: str(frame).zfill(len(match.group(0))), text)
    if '#' not in sub_path:
        text += str(frame).zfill(4)
    return text


def resolve_output_path(base, item, scene, *, line_name=None):
    """Resolve and validate one new output path below an existing base directory."""
    base = Path(base)
    if not base.is_absolute() or not base.is_dir():
        raise VectorOutputError('Vector base path must be an existing absolute directory')
    base = base.resolve()
    if not isinstance(item, dict):
        raise VectorOutputError('Invalid vector output entry')
    file_type = item.get('file_type', 'AIEPS')
    if file_type not in FILE_TYPES:
        raise VectorOutputError('Unknown vector file type')
    sub_path = _string(item.get('sub_path', 'Line'), 'sub_path', maximum=1024)
    sub_path = _frame_subpath(sub_path, int(scene.frame_current), file_type,
                              line_name=line_name)
    sub_path = sub_path.replace('\\', '/')
    if sub_path.startswith('/') or re.match(r'^[A-Za-z]:', sub_path):
        raise VectorOutputError('sub_path must be relative to the base directory')
    parts = [part for part in sub_path.split('/') if part not in ('', '.')]
    if not parts:
        raise VectorOutputError('sub_path resolves to an empty file name')
    if any(part == '..' for part in parts):
        raise VectorOutputError('sub_path cannot traverse outside the base directory')
    if any(any(character in _INVALID_WINDOWS for character in part) or
           any(ord(character) < 32 for character in part) for part in parts):
        raise VectorOutputError('sub_path contains characters invalid in a Windows file name')
    extension = '.pld' if file_type == 'PLD' else '.eps'
    name = parts[-1]
    if not name.lower().endswith(extension):
        name += extension
    parts[-1] = name
    path = base.joinpath(*parts)
    try:
        resolved = path.resolve(strict=False)
    except OSError as error:
        raise VectorOutputError('Cannot resolve vector output path: ' + str(error)) from error
    if not resolved.is_relative_to(base):
        raise VectorOutputError('sub_path resolves outside the base directory')
    return path


def _preflight(path):
    path = Path(path)
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise VectorOutputError('Vector output parent is not a directory: ' + str(path.parent))
    if path.exists() or path.is_symlink():
        raise FileExistsError('Vector file already exists; choose a new name: ' + str(path))
    return path


def _drain(steps):
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


class _SettingsVisibilityBoth:
    """Delegate every setting except the explicit output visibility channel."""

    def __init__(self, settings):
        self._settings = settings

    def __getattr__(self, name):
        if name == '_settings':
            raise AttributeError(name)
        return getattr(self._settings, name)

    @property
    def visibility(self):
        return 'BOTH'


def _capture(context):
    from . import camera
    depsgraph = context.evaluated_depsgraph_get()
    if depsgraph.mode == 'RENDER':
        from .render_geometry import capture as capture_geometry
        geometry = capture_geometry(depsgraph)
        frame = camera.capture_render(context.scene, depsgraph)
        evaluation = 'RENDER'
    else:
        from .geometry import Geometry
        geometry = Geometry(context)
        frame = camera.capture(context)
        evaluation = 'VIEWPORT'
    return geometry, frame, evaluation


def _plan_for_scene(scene,context=None):
    from . import addon
    settings = scene.toon_viewport_preview
    evaluated=None
    if context is not None:
        graph=context.evaluated_depsgraph_get()
        if graph.mode=='RENDER':evaluated=graph.scene_eval
    return addon.effective_preset(scene,evaluated_scene=evaluated) if settings.style_mode == 'PRESET' else None


def _build_layers(context, geometry, frame, plan=None):
    from . import addon
    from .strokes import build_layers_steps
    scene = context.scene
    settings = scene.toon_viewport_preview
    proxy = _SettingsVisibilityBoth(settings)
    width, height = frame['width'], frame['height']
    if settings.style_mode == 'PRESET':
        if plan is None:
            plan = addon._preset(scene)
        if plan is None:
            raise VectorOutputError('Preset style has no compiled line plan')
        map_amounts = addon._map_amounts(scene, plan)
        dash_patterns = addon._dash_patterns(scene, plan)
        from .intersection_strokes import _plan as intersection_plan
        if intersection_plan(plan) is not None and not hasattr(geometry, 'intersection_sources'):
            from .intersection_sources import capture_steps
            geometry.intersection_sources = _drain(capture_steps(geometry.meshes))
        chain_phase = None
        inputs = addon._chain_inputs(scene, plan)
        if inputs is not None:
            from . import chain_snapshot, chain_compute, dash_controls
            bindings, controls = inputs
            payload = _drain(chain_snapshot.snapshot_steps(
                geometry, plan, frame['view'], frame['mvp'], frame['perspective'],
                width, height, 1, proxy, bindings, controls))
            if dash_controls.needs_metric(dash_patterns):
                payload['include_dash_metric'] = True
            chain_phase = chain_compute.compute_snapshot(payload, lambda: None)
        layers, style = _drain(build_layers_steps(
            geometry, plan, frame['view'], frame['mvp'], frame['perspective'],
            width, height, 1, proxy, include_provenance=True,
            map_amounts=map_amounts, dash_patterns=dash_patterns, chain_phase=chain_phase))
        source_mode = 'PRESET'
    else:
        layers = render_elements.basic_layers(geometry, frame, proxy)
        style = dict(groups=0, layers=len(layers), chains=0, rows=[], authority='BASIC sources',
                     missing=['native chain/weld/phase', 'native curve evaluator',
                              'native coverage/AA/filter'])
        source_mode = 'BASIC'
    return layers, style, source_mode, plan


class _Occluder:
    """CPU equivalent of drawing.py's world-triangle centerline query."""

    def __init__(self, geometry):
        from .visibility import build_bvh
        self.nodes, self.triangles = build_bvh(geometry.meshes)
        self.count = len(self.triangles) // 3

    @staticmethod
    def _box_hit(origin, direction, low, high, limit):
        enter, leave = 0.0, limit
        for axis in range(3):
            if abs(direction[axis]) < 1e-20:
                if origin[axis] < low[axis] or origin[axis] > high[axis]:
                    return False
            else:
                a = (low[axis] - origin[axis]) / direction[axis]
                b = (high[axis] - origin[axis]) / direction[axis]
                enter = max(enter, min(a, b))
                leave = min(leave, max(a, b))
                if enter > leave:
                    return False
        return True

    def state(self, point, query, support, frame, inverse_mvp, eye, forward, perspective,
              policy_offset=-1.0, policy_table=None):
        """CPU lineState: 0 blocked/hidden, 1 visible, 2 masked (priority)."""
        if self.count == 0:
            return 1
        width, height = frame['width'], frame['height']
        x, y = query
        ndc_x, ndc_y = 2.0 * x / width - 1.0, 2.0 * y / height - 1.0
        near_h = inverse_mvp @ np.array((ndc_x, ndc_y, -1.0, 1.0), np.float64)
        if abs(near_h[3]) < 1e-20:
            return 1
        near_point = near_h[:3] / near_h[3]
        if perspective:
            direction = point - eye
            norm = float(np.linalg.norm(direction))
            if norm <= 1e-20:
                return 1
            direction /= norm
        else:
            direction = forward
        distance = float(np.dot(point - near_point, direction))
        if distance <= 0.0:
            return 1
        origin = point - direction * distance
        limit = distance - 1e-6 * max(1.0, float(np.linalg.norm(point - origin)))
        if limit < 0.0:
            return 1
        nodes, triangles = self.nodes, self.triangles
        stack = [0]
        support0, support1 = float(support[0]), float(support[1])
        blocked = False
        table = None
        if policy_table is not None:
            table = np.asarray(policy_table)
            if table.dtype.kind not in 'iuf' or table.ndim != 2 or table.shape[1] != 4 or not np.isfinite(table).all():
                raise VectorOutputError('line_function_table must be a (N,4) finite array')
        while stack:
            node = stack.pop()
            low, high = nodes[node * 2], nodes[node * 2 + 1]
            if not self._box_hit(origin, direction, low[:3], high[:3], limit):
                continue
            if high[3] >= 0.0:
                stack.append(int(low[3]))
                stack.append(int(high[3]))
                continue
            start, end = int(low[3]), int(low[3]) - int(high[3])
            for triangle in range(start, end):
                first = triangles[triangle * 3]
                if first[3] == support0 or first[3] == support1:
                    continue
                a = first[:3]
                e1 = triangles[triangle * 3 + 1, :3] - a
                e2 = triangles[triangle * 3 + 2, :3] - a
                p = np.cross(direction, e2)
                det = float(np.dot(e1, p))
                if abs(det) < 1e-20:
                    continue
                offset = origin - a
                u = float(np.dot(offset, p)) / det
                if u < 0.0 or u > 1.0:
                    continue
                qv = np.cross(offset, e1)
                v = float(np.dot(direction, qv)) / det
                if v < 0.0 or u + v > 1.0:
                    continue
                t = float(np.dot(e2, qv)) / det
                if 0.0 <= t < limit:
                    if table is None or float(policy_offset) < 0.0:
                        policy = np.zeros(4, np.float64)
                    else:
                        index = int(policy_offset) + int(round(float(first[3])))
                        if not 0 <= index < len(table):
                            raise VectorOutputError(
                                'line_function_policy index exceeds table: %d not in [0,%d)'
                                % (index, len(table)))
                        policy = table[index]
                    if float(policy[1]) > 0.5:
                        return 2
                    if float(policy[0]) < 0.5:
                        blocked = True
        return 0 if blocked else 1


def _camera_rays(frame):
    mvp = np.asarray(frame['mvp'], np.float64)
    inverse_mvp = np.linalg.inv(mvp)
    forward = np.cross(mvp[0, :3], mvp[1, :3])
    norm = float(np.linalg.norm(forward))
    if norm <= 1e-20:
        raise VectorOutputError('Camera basis is degenerate')
    forward /= norm
    # drawing.py derives the direction from MVP rows and orients it with the
    # inverse-MVP camera column; use the same convention for parity.
    if float(np.dot(forward, inverse_mvp[:3, 2])) < 0.0:
        forward = -forward
    eye = np.zeros(3, np.float64)
    perspective = bool(frame['perspective'])
    if perspective:
        homogeneous = inverse_mvp[:, 2]
        if abs(homogeneous[3]) <= 1e-20:
            raise VectorOutputError('Perspective camera eye is not finite')
        eye = homogeneous[:3] / homogeneous[3]
    return inverse_mvp, eye, forward, perspective


def _sample(primitive, count):
    world = primitive['world']
    projected = primitive['projected']
    widths = primitive['widths']
    alphas = primitive['alphas']
    t = np.linspace(0.0, 1.0, count)[:, None]
    points = world[0] + t * (world[1] - world[0])
    screen = projected[0] + t * (projected[1] - projected[0])
    width = widths[0] + t[:, 0] * (widths[1] - widths[0])
    alpha = alphas[0] + t[:, 0] * (alphas[1] - alphas[0])
    return points, screen, width, alpha


def _classify_primitive(primitive, occluder, frame, inverse_mvp, eye, forward, perspective,
                        want_visible, *, policy_offset=-1.0, policy_table=None):
    q0, q1 = primitive['projected']
    length = float(np.linalg.norm(q1 - q0))
    count = max(2, int(math.ceil(length / SAMPLE_STEP_PX)) + 1)
    count = min(count, MAX_SAMPLES_PER_PRIMITIVE)
    points, screen, widths, alphas = _sample(primitive, count)
    support = primitive['supports']
    states = np.zeros(count, np.int8)
    for index in range(count):
        states[index] = occluder.state(
            points[index], (float(screen[index, 0]), float(screen[index, 1])),
            support, frame, inverse_mvp, eye, forward, perspective,
            policy_offset=policy_offset, policy_table=policy_table)
    target = 1 if want_visible else 0
    keep = states == target
    return points, screen, widths, alphas, keep, int(np.count_nonzero(states == 2))


def _bisect_transition(primitive, occluder, frame, inverse_mvp, eye, forward, perspective,
                       target_state, policy_offset, policy_table, low, high, low_keep, high_keep):
    for _ in range(12):
        mid = (low + high) * 0.5
        world = primitive['world']
        projected = primitive['projected']
        point = world[0] + mid * (world[1] - world[0])
        screen = projected[0] + mid * (projected[1] - projected[0])
        state = occluder.state(point, (float(screen[0]), float(screen[1])),
                               primitive['supports'], frame, inverse_mvp,
                               eye, forward, perspective, policy_offset=policy_offset,
                               policy_table=policy_table)
        keep = state == target_state
        if keep == low_keep:
            low = mid
        else:
            high = mid
    return (low + high) * 0.5


def _interpolate(primitive, index):
    world = primitive['world']
    projected = primitive['projected']
    return (world[0] + index * (world[1] - world[0]),
            projected[0] + index * (projected[1] - projected[0]),
            primitive['widths'][0] + index * (primitive['widths'][1] - primitive['widths'][0]),
            primitive['alphas'][0] + index * (primitive['alphas'][1] - primitive['alphas'][0]))


def _runs(primitive, occluder, frame, inverse_mvp, eye, forward, perspective, want_visible,
          *, preclassified=False, policy_offset=-1.0, policy_table=None):
    if preclassified:
        # Object-partition fragments are already cut into the authored
        # visibility channel by object_fragments.cut; the shader deliberately
        # skips lineVisible for these rows.
        first = (float(primitive['projected'][0][0]), float(primitive['projected'][0][1]),
                 float(primitive['widths'][0]), float(primitive['alphas'][0]))
        last = (float(primitive['projected'][1][0]), float(primitive['projected'][1][1]),
                float(primitive['widths'][1]), float(primitive['alphas'][1]))
        if math.dist(first[:2], last[:2]) <= 1e-12:
            return [[first]], 0
        return [[first, last]], 0
    points, screen, widths, alphas, keep, masked = _classify_primitive(
        primitive, occluder, frame, inverse_mvp, eye, forward, perspective,
        want_visible, policy_offset=policy_offset, policy_table=policy_table)
    count = len(keep)
    if not np.any(keep):
        return [], masked
    target_state = 1 if want_visible else 0
    runs = []
    index = 0
    while index < count:
        if not keep[index]:
            index += 1
            continue
        first = index
        while index + 1 < count and keep[index + 1]:
            index += 1
        last = index
        start_t = (first / (count - 1)) if first > 0 else 0.0
        end_t = (last / (count - 1)) if last + 1 < count else 1.0
        if first > 0:
            start_t = _bisect_transition(
                primitive, occluder, frame, inverse_mvp, eye, forward, perspective,
                target_state, policy_offset, policy_table,
                (first - 1) / (count - 1), first / (count - 1),
                bool(keep[first - 1]), True)
        if last + 1 < count:
            end_t = _bisect_transition(
                primitive, occluder, frame, inverse_mvp, eye, forward, perspective,
                target_state, policy_offset, policy_table,
                last / (count - 1), (last + 1) / (count - 1),
                True, bool(keep[last + 1]))
        ts = [start_t] + [idx / (count - 1) for idx in range(first, last + 1)] + [end_t]
        ts = sorted(set(round(value, 12) for value in ts))
        run_points = []
        for value in ts:
            _, q, width, alpha = _interpolate(primitive, value)
            run_points.append((float(q[0]), float(q[1]), float(width), float(alpha)))
        if run_points:
            runs.append(run_points)
        index += 1
    return runs, masked


def _polygon(run, *, output_width, output_height, crop_x, crop_y):
    def output_point(x, y):
        # Source projected coordinates are bottom-origin pixels.  EPS content
        # uses top-origin page coordinates.
        return x - crop_x, output_height - (y - crop_y)

    if not run:
        return []
    if len(run) == 1:
        x, y, width, _ = run[0]
        radius = max(width * 0.5, 0.05)
        return [output_point(x + dx, y + dy) for dx, dy in
                ((-radius, 0.0), (0.0, -radius), (radius, 0.0), (0.0, radius))]
    points = [(point[0], point[1]) for point in run]
    left = []
    right = []
    for index, point in enumerate(points):
        if index == 0:
            tangent = (points[1][0] - point[0], points[1][1] - point[1])
        elif index == len(points) - 1:
            tangent = (point[0] - points[index - 1][0], point[1] - points[index - 1][1])
        else:
            tangent = (points[index + 1][0] - points[index - 1][0],
                       points[index + 1][1] - points[index - 1][1])
        length = math.hypot(*tangent)
        if length <= 1e-12:
            tangent = (1.0, 0.0)
        else:
            tangent = (tangent[0] / length, tangent[1] / length)
        normal = (-tangent[1], tangent[0])
        radius = max(run[index][2] * 0.5, 0.02)
        left.append(output_point(point[0] + normal[0] * radius,
                                 point[1] + normal[1] * radius))
        right.append(output_point(point[0] - normal[0] * radius,
                                  point[1] - normal[1] * radius))
    polygon = left + list(reversed(right))
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    if max(xs) < 0.0 or max(ys) < 0.0 or min(xs) > output_width or min(ys) > output_height:
        return []
    return polygon


def _srgb_component(value):
    value = min(1.0, max(0.0, float(value)))
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1.0 / 2.4) - 0.055


def _color(layer):
    color = tuple(layer.get('color', (0.0, 0.0, 0.0)))[:3]
    if len(color) != 3 or not all(math.isfinite(float(value)) for value in color):
        raise VectorOutputError('Layer color must contain three finite values')
    return tuple(round(_srgb_component(value), 6) for value in color)


def _layer_rgb(layer):
    """Consume an optional B/LF endpoint RGB array.

    Supported shapes are (N,2,3), (N,2,4), (N,3) and (N,4), where N is the
    per-layer primitive count.  The fourth channel is recorded as ignored
    because EPS has no per-stroke alpha; the resolved ``alphas`` array remains
    the source of truth for alpha loss reporting.
    """
    value = layer.get('rgb_endpoints')
    source = 'rgb_endpoints'
    if value is None:
        value = layer.get('rgb')
        source = 'layer_rgb'
    effects = layer.get('brush_effects')
    effects_rgb = None
    if isinstance(effects, dict) and 'rgb' in effects:
        effects_rgb = effects['rgb']
    if value is None:
        value = effects_rgb
        source = 'brush_effects.rgb'
    if value is None:
        return None, False, 'uniform_layer_color'
    array = np.asarray(value)
    if array.dtype.kind not in 'iuf' or not np.isfinite(array).all():
        raise VectorOutputError('Layer rgb must contain finite numeric values')
    count = len(layer['projected'])
    alpha_ignored = False
    if array.shape == (count, 2, 3):
        pass
    elif array.shape == (count, 2, 4):
        array = array[:, :, :3]
        alpha_ignored = True
    elif array.shape == (count, 3):
        array = np.repeat(array[:, None, :], 2, axis=1)
    elif array.shape == (count, 4):
        alpha_ignored = True
        array = np.repeat(array[:, None, :3], 2, axis=1)
    else:
        raise VectorOutputError('Layer rgb shape must be (N,2,3), (N,2,4), (N,3) or (N,4)')
    if layer.get('rgb_endpoints') is not None:
        source = 'rgb_endpoints'
    elif layer.get('rgb') is not None:
        source = 'layer_rgb'
    else:
        source = 'brush_effects.rgb'
    return np.asarray(array, np.float64), alpha_ignored, source


def _primitive_color(layer, rgb, index):
    if rgb is None:
        return _color(layer), False
    first, last = rgb[index]
    if np.max(np.abs(first - last)) <= 1e-9:
        linear = first
        gradient = False
    else:
        linear = (first + last) * 0.5
        gradient = True
    if not np.isfinite(linear).all():
        raise VectorOutputError('Layer rgb contains a non-finite primitive color')
    return tuple(round(_srgb_component(value), 6) for value in linear[:3]), gradient


def _selected_layer_masks(layers, item, hierarchy=None):
    base = dict(group_uids=None, line_set_ids=list(item['line_set_ids']))
    for name in render_elements.CATEGORIES:
        base[name + '_on'] = item[name + '_on']
    selected = []
    filtered = []
    requested = item.get('line_ids')
    if requested is not None and not (hierarchy and hierarchy['present']):
        raise VectorOutputError('line_ids requires plan.raw.request.line_hierarchy')
    for layer in layers:
        if hierarchy and hierarchy['present']:
            line_id = layer.get('line_id')
            if line_id is None:
                if requested is not None:
                    continue
            else:
                line = hierarchy['map'].get(line_id)
                if line is None:
                    raise VectorOutputError('layer line_id is not present in line_hierarchy: ' + str(line_id))
                if not line['enabled']:
                    continue
                if requested is not None and line_id not in requested:
                    continue
        elif requested is not None:
            raise VectorOutputError('line_ids requires plan.raw.request.line_hierarchy')
        filtered.append(layer)
    # Match Renderer: each Line paints H then V, with ordered layers in each
    # channel. A BOTH stream is split without moving it across another Line.
    for line_id in dict.fromkeys(layer.get('line_id') for layer in filtered):
        for source_channel in ('HIDDEN', 'VISIBLE', 'BOTH'):
            stream=[layer for layer in filtered if layer.get('line_id')==line_id
                    and layer['visibility']==source_channel]
            for visible,hidden in ((False,True),(True,False)):
                if source_channel!='BOTH' and source_channel!=('VISIBLE' if visible else 'HIDDEN'):
                    continue
                if (visible and not item['visible_lines_on']) or (hidden and not item['hidden_lines_on']):
                    continue
                element=dict(base,visible_lines_on=visible,hidden_lines_on=hidden)
                rows,masks=render_elements.select_layers(stream,element,return_masks=True)
                selected.extend(zip(rows,masks))
    return selected


def _selected_layer_sets(layers, item, hierarchy=None):
    """Compatibility view used by filter tests; optional arrays need masks."""
    return [layer for layer, _ in _selected_layer_masks(layers, item, hierarchy)]


def _mask_optional(layer, mask):
    rgb = layer.get('rgb')
    if rgb is not None:
        value = np.asarray(rgb)
        if len(value) != len(mask):
            raise VectorOutputError('Layer rgb must have one entry per styled primitive')
        layer['rgb'] = value[mask]
    effects = layer.get('brush_effects')
    if isinstance(effects, dict) and 'rgb' in effects:
        value = np.asarray(effects['rgb'])
        if len(value) != len(mask):
            raise VectorOutputError('brush_effects.rgb must have one entry per styled primitive')
        effects = dict(effects)
        effects['rgb'] = value[mask]
        layer['brush_effects'] = effects


def _line_function_context(plan):
    raw = plan.get('raw') if isinstance(plan, dict) else None
    request = raw.get('request') if isinstance(raw, dict) else None
    functions = request.get('line_functions') if isinstance(request, dict) else None
    return dict(
        present=isinstance(functions, dict),
        schema=functions.get('schema') if isinstance(functions, dict) else None,
        containers=len(functions.get('containers', [])) if isinstance(functions, dict) else 0,
        masks=len(functions.get('masks', [])) if isinstance(functions, dict) else 0,
        source='plan.raw.request.line_functions' if isinstance(functions, dict) else None,
        per_primitive_source='layer.line_function_policy + layer.line_function_table',
    )


def _line_function_transform(layers, line_function):
    """Validate, but never reinterpret, the real per-primitive policy table.

    Mainline semantics: policy is a float block offset per primitive; the
    shared table is (rows,4), indexed by int(policy_offset) + triangle_global_id.
    x<0.5 occludes, x>=0.5 ignores that occluder (promotion), and y>0.5 masks
    (priority).  No-policy is -1.
    """
    policy_layers = 0
    table_shapes = []
    no_policy = 0
    policy_min = None
    policy_max = None
    for layer in layers:
        policy = layer.get('line_function_policy')
        table = layer.get('line_function_table')
        if policy is None and table is None:
            continue
        count = len(layer['projected'])
        if table is not None:
            values = np.asarray(table)
            if values.dtype.kind not in 'iuf' or values.ndim != 2 or values.shape[1] != 4 or not np.isfinite(values).all():
                raise VectorOutputError('layer.line_function_table must be a finite (N,4) array')
            table_shapes.append([int(values.shape[0]), 4])
        if policy is not None:
            offsets = np.asarray(policy)
            if offsets.dtype.kind not in 'iuf' or offsets.shape != (count,) or not np.isfinite(offsets).all():
                raise VectorOutputError('layer.line_function_policy must be one finite float per primitive')
            if np.any(offsets < -1.0):
                raise VectorOutputError('layer.line_function_policy values must be -1 or non-negative')
            if table is None and np.any(offsets >= 0.0):
                raise VectorOutputError('layer.line_function_policy requires layer.line_function_table')
            policy_layers += 1
            no_policy += int(np.count_nonzero(offsets < 0.0))
            current_min = float(np.min(offsets))
            current_max = float(np.max(offsets))
            policy_min = current_min if policy_min is None else min(policy_min, current_min)
            policy_max = current_max if policy_max is None else max(policy_max, current_max)
    return layers, dict(
        present=bool(line_function.get('present') or policy_layers),
        source=line_function.get('source'),
        plan_schema=line_function.get('schema'),
        plan_containers=line_function.get('containers', 0),
        plan_masks=line_function.get('masks', 0),
        per_primitive_source='layer.line_function_policy + layer.line_function_table',
        policy_layers=policy_layers,
        table_shapes=table_shapes,
        no_policy_primitives=no_policy,
        policy_offset_min=policy_min,
        policy_offset_max=policy_max,
        holdout_source='heldout sources excluded by builder; table y/zero width-alpha encode holdout/mask',
        visibility_authority='lineState table (x=promotion, y=mask priority, -1=no policy)',
    )


def _record_key(record, grouping):
    if grouping == 'LINE':
        return ('LINE', record['line_id'], record['line_name'], record['color'])
    if grouping == 'LINE_SET':
        return ('LINE_SET', record['line_set_id'], record['channel'], record['color'])
    if grouping == 'CHANNEL':
        return ('CHANNEL', record['channel'], record['color'])
    if grouping == 'NONE':
        return ('NONE', record['color'])
    return ('PRESERVE', record['line_id'], record['line_name'],
            record['line_set_id'], record['channel'], record['color'])


def _append_record(records, record, grouping):
    key = _record_key(record, grouping)
    record['key'] = key
    if grouping == 'PRESERVE' and records and records[-1]['key'] == key:
        records[-1]['polygons'].extend(record['polygons'])
    else:
        records.append(record)


def _merge_records(records, grouping):
    if grouping == 'PRESERVE':
        return records
    result = []
    by_key = {}
    for record in records:
        key = _record_key(record, grouping)
        if key in by_key:
            by_key[key]['polygons'].extend(record['polygons'])
            continue
        copy = dict(record)
        copy['key'] = key
        copy['polygons'] = list(record['polygons'])
        by_key[key] = copy
        result.append(copy)
    return result


def _ai_layer_groups(records, grouping):
    if grouping == 'PRESERVE':
        return [dict(name=_record_layer_name(record), records=[record]) for record in records]
    result = []
    by_key = {}
    for record in records:
        if grouping == 'LINE':
            key = ('LINE', record['line_id'], record['line_name'])
        elif grouping == 'LINE_SET':
            key = ('LINE_SET', record['line_set_id'], record['channel'])
        elif grouping == 'CHANNEL':
            key = ('CHANNEL', record['channel'])
        else:
            key = ('NONE',)
        if key not in by_key:
            group = dict(name=_group_layer_name(key), records=[])
            by_key[key] = group
            result.append(group)
        by_key[key]['records'].append(record)
    return result


def _record_layer_name(record):
    if record.get('line_name'):
        base = record['line_name']
    else:
        base = 'Line Set ' + str(record['line_set_id'])
    return base + (' Visible' if record['channel'] == 'VISIBLE' else ' Hidden')


def _group_layer_name(key):
    if key[0] == 'LINE':
        return key[2] or ('Line ' + str(key[1]))
    if key[0] == 'LINE_SET':
        return 'Line Set ' + str(key[1]) + (' Visible' if key[2] == 'VISIBLE' else ' Hidden')
    if key[0] == 'CHANNEL':
        return 'Visible' if key[1] == 'VISIBLE' else 'Hidden'
    return 'Toon Vector'


def _collect(layers, item, frame, occluder, inverse_mvp, eye, forward, perspective,
             *, hierarchy=None, line_function=None):
    hierarchy = hierarchy or dict(present=False, source=None, lines=[], map={}, enabled=[])
    line_function = line_function or dict(present=False, policy=None, table=None, source=None)
    layers, line_function_report = _line_function_transform(layers, line_function)
    grouping = item.get('grouping', 'PRESERVE')
    output_width = int(frame['output_width'])
    output_height = int(frame['output_height'])
    crop_x = crop_y = 0
    if frame['metadata'].get('use_border') and frame['metadata'].get('use_crop_to_border'):
        crop_x, crop_y = frame['metadata']['border_rect_px'][:2]
    groups = []
    stats = dict(visible_runs=0, hidden_runs=0, visible_polygons=0, hidden_polygons=0,
                 source_primitives=0, dropped_nonpositive_alpha=0, vertices=0,
                 width_min=None, width_max=None, alpha_min=None, alpha_max=None,
                 rgb_sources=[], rgb_gradient_primitives=0,
                 rgb_alpha_ignored=False, brush_effects_present=False,
                 brush_effect_rows_present=False, brush_effect_author_present=False,
                 brush_phase_present=False,
                 unsupported_brush_effects=[], consumed_brush_effects=[],
                 superseded_brush_effects=[], superseded_rgb_fields=[], layers_with_line_id=0,
                 layers_without_line_id=0, line_ids=[], grouping=grouping,
                 masked_suppressed_samples=0)
    for layer, mask in _selected_layer_masks(layers, item, hierarchy):
        _mask_optional(layer, mask)
        channel = layer['visibility']
        want_visible = channel == 'VISIBLE'
        line = _line_for_layer(layer, hierarchy)
        line_id = None if line is None else line['id']
        line_name = None if line is None else line['name']
        if line_id is None:
            stats['layers_without_line_id'] += 1
        else:
            stats['layers_with_line_id'] += 1
            if line_id not in stats['line_ids']:
                stats['line_ids'].append(line_id)
        rgb, rgb_alpha_ignored, rgb_source = _layer_rgb(layer)
        policy = layer.get('line_function_policy')
        policy_table = layer.get('line_function_table')
        if policy is not None:
            policy = np.asarray(policy)
            if policy.dtype.kind not in 'iuf' or policy.shape != (len(layer['projected']),):
                raise VectorOutputError('Filtered line_function_policy length differs from primitives')
        if policy_table is not None:
            policy_table = np.asarray(policy_table)
        effects = layer.get('brush_effects')
        stats['brush_effect_rows_present'] |= 'brush_effect_rows' in layer
        stats['brush_effect_author_present'] |= 'brush_effect_author' in layer
        stats['brush_phase_present'] |= 'brush_phase' in layer
        if effects is not None and not isinstance(effects, dict):
            raise VectorOutputError('Layer brush_effects must be a mapping')
        effects_keys = set(effects) if isinstance(effects, dict) else set()
        top_rgb = layer.get('rgb_endpoints') if layer.get('rgb_endpoints') is not None else layer.get('rgb')
        superseded = sorted(effects_keys & {'rgb'}) if top_rgb is not None else []
        superseded_rgb = []
        if layer.get('rgb_endpoints') is not None:
            if layer.get('rgb') is not None:
                superseded_rgb.append('rgb')
            if 'rgb' in effects_keys:
                superseded_rgb.append('brush_effects.rgb')
        stats['superseded_rgb_fields'] = sorted(set(stats['superseded_rgb_fields']) | set(superseded_rgb))
        consumed = sorted((effects_keys & {'rgb'}) - set(superseded))
        unsupported = sorted(effects_keys - {'rgb'})
        stats['brush_effects_present'] |= bool(effects_keys)
        if rgb_source not in stats['rgb_sources']:
            stats['rgb_sources'].append(rgb_source)
        stats['rgb_alpha_ignored'] |= rgb_alpha_ignored
        stats['consumed_brush_effects'] = sorted(set(stats['consumed_brush_effects']) | set(consumed))
        stats['unsupported_brush_effects'] = sorted(set(stats['unsupported_brush_effects']) | set(unsupported))
        stats['superseded_brush_effects'] = sorted(set(stats['superseded_brush_effects']) | set(superseded))
        line_set_id = int(layer['line_set_id'])
        if type(layer['line_set_id']) is not int or not 1 <= line_set_id <= 8:
            raise VectorOutputError('Invalid layer numeric line-set ID')
        for index in range(len(layer['projected'])):
            color, gradient = _primitive_color(layer, rgb, index)
            if gradient:
                stats['rgb_gradient_primitives'] += 1
            primitive = {name: layer[name][index] for name in
                         ('world', 'projected', 'widths', 'alphas', 'supports')}
            stats['source_primitives'] += 1
            stats['width_min'] = min([v for v in (stats['width_min'], float(np.min(primitive['widths']))) if v is not None])
            stats['width_max'] = max([v for v in (stats['width_max'], float(np.max(primitive['widths']))) if v is not None])
            stats['alpha_min'] = min([v for v in (stats['alpha_min'], float(np.min(primitive['alphas']))) if v is not None])
            stats['alpha_max'] = max([v for v in (stats['alpha_max'], float(np.max(primitive['alphas']))) if v is not None])
            if float(np.max(primitive['alphas'])) <= 0.0:
                stats['dropped_nonpositive_alpha'] += 1
                continue
            preclassified = (bool(layer['preclassified'][index])
                             if 'preclassified' in layer else False)
            policy_offset = float(policy[index]) if policy is not None else -1.0
            runs, masked = _runs(primitive, occluder, frame, inverse_mvp, eye, forward,
                                 perspective, want_visible, preclassified=preclassified,
                                 policy_offset=policy_offset, policy_table=policy_table)
            stats['masked_suppressed_samples'] += masked
            for run in runs:
                polygon = _polygon(run, output_width=output_width,
                                   output_height=output_height,
                                   crop_x=crop_x, crop_y=crop_y)
                if not polygon:
                    continue
                record = dict(line_id=line_id, line_name=line_name,
                              line_set_id=line_set_id, channel=channel, color=color,
                              polygons=[polygon])
                _append_record(groups, record, grouping)
                stats['vertices'] += len(polygon)
                if want_visible:
                    stats['visible_runs'] += 1
                    stats['visible_polygons'] += 1
                else:
                    stats['hidden_runs'] += 1
                    stats['hidden_polygons'] += 1
    stats['line_function'] = line_function_report
    stats['line_function']['masked_suppressed_samples'] = stats['masked_suppressed_samples']
    stats['record_count'] = len(groups)
    stats['order_preserved'] = grouping == 'PRESERVE'
    groups = _merge_records(groups, grouping)
    stats['group_count'] = len(_ai_layer_groups(groups, grouping))
    return groups, stats


def _number(value):
    text = format(float(value), '.6f').rstrip('0').rstrip('.')
    return text if text not in ('', '-0') else '0'


def _ps_string(value):
    value = ''.join(character if 32 <= ord(character) < 127 else '?' for character in value)
    return '(' + value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ')'


def _write_paths(lines, records):
    for record in records:
        color = record['color']
        polygons = record['polygons']
        lines.append(_number(color[0]) + ' ' + _number(color[1]) + ' ' +
                     _number(color[2]) + ' setrgbcolor')
        lines.append('newpath')
        for polygon in polygons:
            first = polygon[0]
            lines.append(_number(first[0]) + ' ' + _number(first[1]) + ' moveto')
            for point in polygon[1:]:
                lines.append(_number(point[0]) + ' ' + _number(point[1]) + ' lineto')
            lines.append('closepath')
        lines.append('fill')


def encode_eps(document):
    """Serialize a validated document as EPSF-3.0 / optional AI5 layer comments."""
    width = float(document['width'])
    height = float(document['height'])
    file_type = document['file_type']
    lines = [
        '%!PS-Adobe-3.0 EPSF-3.0',
        '%%Creator: Toon Vector Output (autonomous)',
        '%%Title: ' + document['title'],
        '%%BoundingBox: 0 0 ' + str(int(math.ceil(width))) + ' ' + str(int(math.ceil(height))),
        '%%HiResBoundingBox: 0 0 ' + _number(width) + ' ' + _number(height),
        '%%DocumentData: Clean7Bit',
        '%%LanguageLevel: 2',
        '%%Pages: 1',
        '%%ToonVectorSchema: ' + EPS_SCHEMA,
        '%%ToonVectorFileType: ' + file_type,
        '%%ToonVectorFrame: ' + str(document['frame']),
        '%%ToonVectorSourceMode: ' + document['source_mode'],
        '%%ToonVectorLayers: ' + str(document['layer_count']),
        '%%ToonVectorVisibleRuns: ' + str(document['visible_runs']),
        '%%ToonVectorHiddenRuns: ' + str(document['hidden_runs']),
        '%%ToonVectorVisiblePolygons: ' + str(document['visible_polygons']),
        '%%ToonVectorHiddenPolygons: ' + str(document['hidden_polygons']),
        '%%ToonVectorAlpha: discarded; EPS fills are opaque',
        '%%ToonVectorTexture: discarded; no brush-map texture in EPS',
        '%%ToonVectorCoverage: discarded; no AA/feather representation',
    ]
    if file_type == 'AIEPS':
        lines.append('%%AI8_CreatorVersion: 8.0')
    lines.extend([
        '%%EndComments',
        '%%BeginProlog',
        '/ToonVectorSave save def',
        '/Lb { pop pop pop pop pop pop pop pop pop pop } bind def',
        '/Ln { pop } bind def',
        '/LB { } bind def',
        '%%EndProlog',
        '%%BeginSetup',
        'ToonVectorSave',
        '0 0 translate',
        '%%EndSetup',
        '%%Page: 1 1',
        'gsave',
        '0 0 ' + _number(width) + ' ' + _number(height) + ' rectclip',
    ])
    if file_type == 'AIEPS':
        layer_index = 0
        for layer in _ai_layer_groups(document['groups'], document['grouping']):
            lines.extend([
                '%AI5_BeginLayer',
                '1 1 1 1 0 0 ' + str(layer_index % 27) + ' 0 0 0 Lb',
                _ps_string(layer['name']) + ' Ln',
            ])
            _write_paths(lines, layer['records'])
            lines.extend(['LB', '%AI5_EndLayer--'])
            layer_index += 1
    else:
        _write_paths(lines, document['groups'])
    lines.extend([
        'grestore',
        'showpage',
        '%%PageTrailer',
        '%%Trailer',
        'ToonVectorSave restore',
        '%%EOF',
        '',
    ])
    return ('\n'.join(lines)).encode('ascii', 'replace')


def save_eps_new(path, payload, *, file_type):
    """Publish one complete EPS create-only with a hard-link commit."""
    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != '.eps':
        raise VectorOutputError('Choose a new absolute .eps path')
    if path.exists() or path.is_symlink():
        raise FileExistsError('Vector file already exists; choose a new name: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name('.v-' + uuid.uuid4().hex[:12] + '.tmp')
    created = False
    published = False
    cleanup_warning = None
    try:
        with staged.open('xb') as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(staged, path)
        published = True
    finally:
        if created:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                if published:
                    cleanup_warning = 'Vector file published but temporary cleanup failed: ' + str(error)
                else:
                    raise
    result = dict(path=str(path), file_type=file_type, bytes=len(payload),
                  sha256=hashlib.sha256(payload).hexdigest())
    if cleanup_warning:
        result['cleanup_warning'] = cleanup_warning
    return result


def _document(path, item, groups, stats, frame, source_mode):
    grouping = item.get('grouping', 'PRESERVE')
    layer_keys = _ai_layer_groups(groups, grouping)
    return dict(
        width=int(frame['output_width']),
        height=int(frame['output_height']),
        file_type=item['file_type'],
        title=path.name,
        frame=int(frame['metadata'].get('frame', 0)),
        source_mode=source_mode,
        groups=groups,
        grouping=grouping,
        layer_count=len(layer_keys),
        record_count=len(groups),
        visible_runs=stats['visible_runs'],
        hidden_runs=stats['hidden_runs'],
        visible_polygons=stats['visible_polygons'],
        hidden_polygons=stats['hidden_polygons'],
    )


def _source_style_summary(style, layer_count):
    # The hierarchy producer returns one complete report per resolved Line.
    # Preserve the leaf diagnostic meaning instead of reporting absent totals
    # as zero when the real per-Line sources contain chains.
    reports=[row['report'] for row in style['lines']] if 'lines' in style else [style]
    return dict(groups=sum(row.get('groups',0) for row in reports),
                layers=layer_count,chains=sum(row.get('chains',0) for row in reports),
                count_scope='sum of per-Line source-slot diagnostics')


def request_vector_output(path,context=None,outputs=None,*,view_layer=None,node_ptr=''):
    """Export the requested owner using the actual requested ViewLayer graph."""
    import bpy
    from . import vector_scope
    context=context or bpy.context;scene=getattr(context,'scene',None)
    try:state,layer,scope=vector_scope.resolve(scene,view_layer=view_layer,node_ptr=node_ptr,create_token=True)
    except vector_scope.VectorScopeError as error:raise VectorOutputError(str(error)) from error
    if layer is not None and getattr(context,'view_layer',None)!=layer:
        with bpy.context.temp_override(scene=scene,view_layer=layer):
            if bpy.context.scene!=scene or bpy.context.view_layer!=layer:raise VectorOutputError('ViewLayer override did not select the requested scope')
            report=_request_vector_output_current(path,bpy.context,outputs,_state=state)
            captured_layer=bpy.context.view_layer.name
    else:
        report=_request_vector_output_current(path,context,outputs,_state=state)
        captured_layer=context.view_layer.name
    report['output_scope']=dict(scope,captured_view_layer=captured_layer)
    state.last_vector_report=json.dumps(report,ensure_ascii=False,sort_keys=True)
    return report


def _request_vector_output_current(path, context=None, outputs=None,*,_state=None):
    """Export enabled vector outputs from the current resolved line sources.

    ``path`` is an existing base directory.  ``outputs`` is a list of plain
    dictionaries; when omitted, the scene's VectorOutputSettings collection is
    used.  EPS/AIEPS are written synchronously.  PLD is reported in ``blocked``
    and never replaced by a fake file.
    """
    import bpy
    context = context or bpy.context
    scene = getattr(context, 'scene', None)
    if scene is None:
        raise VectorOutputError('Vector output requires a Scene context')
    base = Path(bpy.path.abspath(str(path)))
    if not base.is_absolute() or not base.is_dir():
        raise VectorOutputError('Vector base path must be an existing absolute directory')
    base = base.resolve()
    state = _state if _state is not None else vector_state(scene)
    items = outputs_from_state(state) if outputs is None else normalize_outputs(outputs)
    if not items:
        raise VectorOutputError('No vector outputs are configured')
    enabled = [item for item in items if item['on']]
    if not enabled:
        raise VectorOutputError('No enabled vector outputs')
    plan = _plan_for_scene(scene,context)
    hierarchy = _line_hierarchy(plan)
    line_function = _line_function_context(plan)

    planned = []
    blocked = []
    seen = set()
    for item in enabled:
        if item['file_type'] == 'PLD':
            blocked.append(dict(index=item['index'], file_type='PLD', reason=_PLD_REASON,
                                sources=list(_PLD_SOURCES)))
            continue
        line_name = _line_name_for_item(item, hierarchy) if _PLACEHOLDER.search(item['sub_path']) else None
        target = resolve_output_path(base, item, scene, line_name=line_name)
        _preflight(target)
        normalized = os.path.normcase(str(target.resolve()))
        if normalized in seen:
            raise VectorOutputError('Multiple vector outputs resolve to the same file: ' + str(target))
        seen.add(normalized)
        planned.append((item, target))

    if not planned:
        report = dict(
            schema=REPORT_SCHEMA, ok=False, complete=False, base_path=str(base),
            scene=scene.name, frame=int(scene.frame_current), files=[], blocked=blocked,
            skipped=[item for item in items if not item['on']],
            loss=loss_manifest(),
            source_data=dict(available=False, reason='No supported vector output was captured.'),
            line_hierarchy=dict(present=hierarchy['present'], source=hierarchy['source'],
                                lines=hierarchy['lines']),
            line_function=line_function,
            error='No supported vector output was requested; PLD is blocked.',
        )
        state.vector_output_message = 'PLD blocked: no public format specification'
        state.vector_output_error = True
        state.last_vector_report = json.dumps(report, ensure_ascii=False, sort_keys=True)
        return report

    geometry, frame, evaluation = _capture(context)
    layers, style, source_mode, plan = _build_layers(context, geometry, frame, plan)
    source_line_ids = sorted({str(layer.get('line_id')) for layer in layers
                              if layer.get('line_id') is not None})
    line_hierarchy_report = dict(
        present=hierarchy['present'], source=hierarchy['source'],
        lines=hierarchy['lines'],
        layers_with_line_id=sum(1 for layer in layers if layer.get('line_id') is not None),
        layers_without_line_id=sum(1 for layer in layers if layer.get('line_id') is None),
        line_ids=source_line_ids,
        group_or_line_set_used_as_line=False,
    )
    occluder = _Occluder(geometry)
    inverse_mvp, eye, forward, perspective = _camera_rays(frame)

    files = []
    for item, target in planned:
        groups, stats = _collect(layers, item, frame, occluder, inverse_mvp, eye,
                                 forward, perspective, hierarchy=hierarchy,
                                 line_function=line_function)
        document = _document(target, item, groups, stats, frame, source_mode)
        payload = encode_eps(document)
        result = save_eps_new(target, payload, file_type=item['file_type'])
        result.update(
            index=item['index'], sub_path=item['sub_path'], on=item['on'],
            visible_lines_on=item['visible_lines_on'],
            hidden_lines_on=item['hidden_lines_on'],
            line_set_ids=list(item['line_set_ids']),
            category_filters={name + '_on': item[name + '_on'] for name in render_elements.CATEGORIES},
            layer_count=document['layer_count'],
            visible_runs=stats['visible_runs'], hidden_runs=stats['hidden_runs'],
            visible_polygons=stats['visible_polygons'],
            hidden_polygons=stats['hidden_polygons'],
            source_primitives=stats['source_primitives'],
            dropped_nonpositive_alpha=stats['dropped_nonpositive_alpha'],
            vertices=stats['vertices'],
            width_px=[stats['width_min'], stats['width_max']],
            alpha=[stats['alpha_min'], stats['alpha_max']],
            rgb_sources=list(stats['rgb_sources']),
            rgb_gradient_primitives=stats['rgb_gradient_primitives'],
            rgb_alpha_ignored=stats['rgb_alpha_ignored'],
            brush_effects_present=stats['brush_effects_present'],
            brush_effect_rows_present=stats['brush_effect_rows_present'],
            brush_effect_author_present=stats['brush_effect_author_present'],
            brush_phase_present=stats['brush_phase_present'],
            consumed_brush_effects=list(stats['consumed_brush_effects']),
            unsupported_brush_effects=list(stats['unsupported_brush_effects']),
            superseded_brush_effects=list(stats['superseded_brush_effects']),
            superseded_rgb_fields=list(stats['superseded_rgb_fields']),
            grouping=stats['grouping'],
            record_count=stats['record_count'],
            group_count=stats['group_count'],
            order_preserved=stats['order_preserved'],
            layers_with_line_id=stats['layers_with_line_id'],
            layers_without_line_id=stats['layers_without_line_id'],
            line_ids=list(stats['line_ids']),
            line_function=dict(stats['line_function']),
            masked_suppressed_samples=stats['masked_suppressed_samples'],
        )
        files.append(result)

    source_data = dict(
        rgb_sources=sorted({source for row in files for source in row['rgb_sources']}),
        rgb_gradient_primitives=sum(row['rgb_gradient_primitives'] for row in files),
        rgb_alpha_ignored=any(row['rgb_alpha_ignored'] for row in files),
        brush_effects_present=any(row['brush_effects_present'] for row in files),
        brush_effect_rows_present=any(row['brush_effect_rows_present'] for row in files),
        brush_effect_author_present=any(row['brush_effect_author_present'] for row in files),
        brush_phase_present=any(row['brush_phase_present'] for row in files),
        consumed_brush_effects=sorted({item for row in files for item in row['consumed_brush_effects']}),
        unsupported_brush_effects=sorted({item for row in files for item in row['unsupported_brush_effects']}),
        superseded_brush_effects=sorted({item for row in files for item in row['superseded_brush_effects']}),
        superseded_rgb_fields=sorted({item for row in files for item in row['superseded_rgb_fields']}),
        masked_suppressed_samples=sum(row['masked_suppressed_samples'] for row in files),
        layers_with_line_id=sum(row['layers_with_line_id'] for row in files),
        layers_without_line_id=sum(row['layers_without_line_id'] for row in files),
        line_ids=sorted({str(line_id) for row in files for line_id in row['line_ids']}),
        line_hierarchy_present=hierarchy['present'],
        line_assignment_source=(
            'legacy_no_line_id' if not hierarchy['present'] else
            ('mixed' if line_hierarchy_report['layers_with_line_id'] and
             line_hierarchy_report['layers_without_line_id'] else 'layer.line_id')),
        group_or_line_set_used_as_line=False,
        source_uv_distortion='consumed directly from layer.projected/world',
    )
    report = dict(
        schema=REPORT_SCHEMA,
        ok=True,
        complete=not blocked,
        base_path=str(base),
        scene=scene.name,
        frame=int(scene.frame_current),
        evaluation=evaluation,
        frame_extent=[int(frame['width']), int(frame['height'])],
        output_extent=[int(frame['output_width']), int(frame['output_height'])],
        output_pixel_aspect=list(frame['metadata'].get('pixel_aspect', (1.0, 1.0))),
        source_mode=source_mode,
        layers=len(layers),
        source_style=_source_style_summary(style, len(layers)),
        parameter_animation=plan.get('parameter_animation',{}) if plan is not None else {},
        files=files,
        source_data=source_data,
        line_hierarchy=line_hierarchy_report,
        line_function=line_function,
        grouping=dict(default='PRESERVE',
                      modes=list(GROUPING_MODES),
                      per_file=[dict(index=row['index'], grouping=row['grouping'],
                                     order_preserved=row['order_preserved'],
                                     record_count=row['record_count'],
                                     group_count=row['group_count']) for row in files]),
        blocked=blocked,
        skipped=[item for item in items if not item['on']],
        loss=loss_manifest(),
        source_limitations=dict(
            chain_phase='Enabled chain controls use the production snapshot/compute pipeline before build_layers; native Pencil+ phase equivalence is not claimed.',
            native_curve='Native Pencil+ curve evaluation is not claimed.',
            object_partition='Preclassified object-partition fragments are trusted as already cut into their authored channel.',
            render_equivalence='VIEWPORT evaluation is not render-equivalent; RENDER evaluation is reported when available.',
        ),
        ai8_layer_ingestion_verified=False,
        adobe_illustrator_tested=False,
        classification='centerline triangle-BVH visibility split; resolved brush color/width/alpha',
        native_equivalent=False,
    )
    state.vector_output_message = ('Exported ' + str(len(files)) + ' vector file(s)' +
                                   ('; PLD blocked' if blocked else ''))
    state.vector_output_error = False
    state.last_vector_report = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return report


def output_path_for_item(item):
    """Return the computed path for an RNA output item, or an empty string."""
    scene, state, _ = _owning_scene(item)
    if scene is None or state is None or not state.vector_output_base_path:
        return ''
    try:
        base = Path(state.vector_output_base_path)
        line_name = None
        if _PLACEHOLDER.search(item.sub_path):
            plan = _plan_for_scene(scene)
            hierarchy = _line_hierarchy(plan)
            line_name = _line_name_for_item(dict(line_ids=None), hierarchy)
        return str(resolve_output_path(base, dict(
            file_type=item.file_type, sub_path=item.sub_path), scene, line_name=line_name))
    except (OSError, ValueError):
        return ''
