# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-array original-style mapping for the 2D viewport preview.

This module deliberately has no ``bpy`` import.  It maps the persisted
JOINT-DOCUMENT editor state and the explicit authoring recipe into a small
plan plus the numeric kernels used by the viewport drawing layer.

Important boundaries of this module:

* Native curve control points are copied losslessly into ``plan['raw']`` and
  each group's ``editor_raw``.  Only the original uniform ``samples`` are
  evaluated here; this is not a native CurveMapping evaluator.
* Native brush size is never converted to a viewport width here.  ``width_px``
  is an explicit Root input.
* Chain parameters and phase are Root inputs.  This module neither solves nor
  invents them.
* ``size_scale`` calls the existing project module
  ``toon_image_cpu/relative_size.py::resolve_size_scale`` explicitly.  The old
  package is not changed or vendored into this package.
"""
from __future__ import annotations

import copy
import math
import numbers

import numpy as np

SCHEMA = 'toon.viewport-preview-style-plan.v1'
CHECK_SCHEMA = 'toon.viewport-preview-style-check.v1'

DOMAINS = ('POINT', 'CORNER')
CHANNELS = ('mean_rgb', 'alpha', 'red', 'max_rgb')
ENCODINGS = ('linear_rgba', 'srgb_rgba')
MISSING_POLICIES = ('error', 'one', 'zero')
SIZE_MODES = ('RELATIVE', 'ABSOLUTE')

COMPILED_BRUSH_FIELDS = (
    'blend_amount', 'color', 'width_map', 'alpha_map', 'size_curve', 'alpha_curve',
)

# The real C4 editor state uses these four.  ``sharp_brush`` is kept in the
# known set because the historical group contract accepts it.
SPECIAL_BRUSH_SLOTS = (
    'brush', 'silhouette_brush', 'sharp_brush', 'marked_brush', 'smoothing_brush',
    'wireframe_visible_brush', 'wireframe_hidden_brush',
)

_DOCUMENT_FIELDS = {'inputs', 'provenance', 'request', 'schema'}
_INPUTS_FIELDS = {
    'full_preset', 'height', 'intersection_recipe', 'projection',
    'source_blend_descriptor', 'source_descriptor', 'width', 'autonomous_origin',
}
_REQUEST_FIELDS = {
    'boundary', 'boundary_aliases', 'input', 'intersection',
    'intersection_aliases', 'layers', 'profile', 'schema', 'wire_sources', 'edge_sources', 'intersection_sources', 'selector_bindings', 'group_lifecycle', 'line_hierarchy', 'line_functions', 'brush_effects',
}
_PROJECTION_FIELDS = {
    'editor_document', 'field_matrix', 'historical_raw_tree_sha256', 'losses',
    'native_equivalent', 'omitted_sets', 'partial', 'policy', 'recipe',
    'reference_descriptor', 'represented_sets', 'resolved_routes', 'schema',
    'source_aliases',
}
_EDITOR_DOCUMENT_FIELDS = {'active_group', 'draft_uid', 'groups', 'revision', 'schema', 'settings'}
_BOUNDARY_FIELDS = {'groups', 'schema', 'settings', 'units'}
_BOUNDARY_GROUP_FIELDS = {
    'id', 'priority', 'object_ids', 'material_names', 'visible_enabled',
    'hidden_enabled', 'brush', 'silhouette_brush', 'sharp_brush',
    'marked_brush', 'smoothing_brush', 'size_mode', 'initial_scale_ex',
    'coverage_extent_factor', 'line_set_id', 'elements_only',
    'wireframe_visible_brush', 'wireframe_hidden_brush',
}
_EDITOR_GROUP_FIELDS = {
    'display_color',
    'uid', 'label', 'object_ids', 'material_names', 'priority',
    'visible_enabled', 'hidden_enabled', 'silhouette_enabled',
    'marked_enabled', 'smoothing_enabled', 'size_mode', 'initial_scale_ex',
    'coverage_extent_factor', 'brushes', 'line_set_id', 'elements_only',
}
_BOUNDARY_BRUSH_FIELDS = {
    'native_size', 'blend_amount', 'color', 'width_map', 'alpha_map',
    'size_curve', 'alpha_curve',
}
_EDITOR_BRUSH_FIELDS = set(_BOUNDARY_BRUSH_FIELDS) | {'uid'}
_BOUNDARY_MAP_FIELDS = {'channel', 'encoding', 'missing', 'selection'}
_EDITOR_MAP_FIELDS = {
    'channel', 'enabled', 'encoding', 'layer_index', 'layer_name', 'missing',
    'selection_kind',
}
_CURVE_FIELDS = {
    'black_level', 'clip', 'enabled', 'extend', 'points', 'samples', 'tone',
    'use_clip', 'white_level',
}
_CURVE_POINT_FIELDS = {'handle', 'x', 'y'}
_BOUNDARY_SETTINGS_FIELDS = {
    'coverage_profile', 'feather_px', 'force_marked_visible', 'include_open',
    'post_filter_sigma', 'samples_per_axis', 'source_categories',
}
_EDITOR_SETTINGS_FIELDS = {
    'absolute_scale', 'coverage_profile', 'feather_px', 'force_marked_visible',
    'include_open', 'post_filter_sigma', 'relative_target_height',
    'relative_target_width', 'samples_per_axis', 'sensor_fit', 'source_marked',
    'source_mode', 'source_selected', 'source_smoothing',
}
_UNITS_FIELDS = {'absolute_scale', 'relative_target_height', 'relative_target_width', 'sensor_fit'}
_OBJECT_BINDING_FIELDS = {'logical_id', 'source_name'}
_MATERIAL_BINDING_FIELDS = {'logical_name', 'source_name'}

SIZE_SCALE_DEPENDENCY = {
    'module': 'toon_image_cpu.relative_size',
    'function': 'resolve_size_scale',
    'schema': 'toon.line-size-resolution.v1',
    'reason': 'Explicit original bridge unit transform; this package does not copy or alter it.',
    'load_order': [
        'packaged toon_viewport_preview/_support/relative_size.py',
        'development sibling tools/toon_image_cpu/relative_size.py loaded by path',
    ],
}

_RESOLVE_SIZE_SCALE = None


class StyleMappingError(ValueError):
    """Input cannot be mapped without inventing style semantics."""


def _need(condition, message):
    if not condition:
        raise StyleMappingError(message)


def _object(value, label):
    _need(type(value) is dict, label + ' must be an object')
    return value


def _string(value, label):
    _need(type(value) is str and 0 < len(value) <= 1024, label + ' must be a non-empty string')
    value.encode('utf-8', errors='strict')
    return value


def _boolean(value, label):
    _need(type(value) is bool, label + ' must be boolean')
    return value


def _optional_boolean(value, label):
    if value is None:
        return None
    return _boolean(value, label)


def _integer(value, label, minimum=None, maximum=None):
    _need(
        isinstance(value, numbers.Integral) and not isinstance(value, (bool, np.bool_)),
        label + ' must be an integer',
    )
    value = int(value)
    if minimum is not None:
        _need(value >= minimum, label + ' is below its minimum')
    if maximum is not None:
        _need(value <= maximum, label + ' is above its maximum')
    return value


def _number(value, label, minimum=None, maximum=None):
    _need(
        isinstance(value, numbers.Real) and not isinstance(value, (bool, np.bool_))
        and math.isfinite(float(value)),
        label + ' must be a finite number',
    )
    value = float(value)
    if minimum is not None:
        _need(value >= minimum, label + ' is below its minimum')
    if maximum is not None:
        _need(value <= maximum, label + ' is above its maximum')
    return value


def _optional_number(value, label, minimum=None, maximum=None):
    if value is None:
        return None
    return _number(value, label, minimum, maximum)


def _string_list(value, label, allow_empty=True):
    _need(type(value) is list, label + ' must be a list')
    _need(len(value) <= 1024, label + ' has too many entries')
    result = [_string(item, label + ' entry') for item in value]
    _need(allow_empty or bool(result), label + ' cannot be empty')
    _need(len(set(result)) == len(result), label + ' must not contain duplicates')
    return result


def _warning(warnings, seen, code, path, reason, severity='warning', **details):
    key = (code, path)
    if key in seen:
        return
    seen.add(key)
    row = {'code': code, 'path': path, 'reason': reason, 'severity': severity}
    row.update(details)
    warnings.append(row)


def _warn_unknown(value, allowed, path, warnings, seen):
    if type(value) is not dict:
        return []
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _warning(
            warnings, seen, 'unknown-field', path,
            'Field(s) are preserved in raw but are not interpreted by this module.',
            severity='unverified', keys=unknown,
        )
    return unknown


def _validate_mapping(mapping, label='mapping'):
    _need(type(mapping) is dict, label + ' must be an object')
    unknown = set(mapping) - set(_BOUNDARY_MAP_FIELDS)
    _need(not unknown, label + ' has unknown field(s): ' + ', '.join(sorted(unknown)))
    for key in _BOUNDARY_MAP_FIELDS:
        _need(key in mapping, label + ' is missing ' + key)
    selection = mapping['selection']
    _need(type(selection) is dict, label + '.selection must be an object')
    _need(set(selection) in ({'index'}, {'name'}), label + '.selection must be exactly {index} or {name}')
    if 'index' in selection:
        index = selection['index']
        _need(
            isinstance(index, numbers.Integral) and not isinstance(index, (bool, np.bool_))
            and int(index) >= 0,
            label + '.selection.index must be a nonnegative integer',
        )
    else:
        _string(selection['name'], label + '.selection.name')
    _need(mapping['channel'] in CHANNELS, label + '.channel is unsupported')
    _need(mapping['encoding'] in ENCODINGS, label + '.encoding is unsupported')
    _need(mapping['missing'] in MISSING_POLICIES, label + '.missing is unsupported')
    return mapping


def _rgba_array(value, label):
    array = np.asarray(value)
    _need(array.ndim == 2 and array.shape[1] == 4, label + ' must have shape (N, 4)')
    _need(array.dtype.kind == 'f', label + ' must be normalized floating-point RGBA')
    _need(array.size == 0 or bool(np.isfinite(array).all()), label + ' must contain finite values')
    return array


class PreparedLayers(tuple):
    """Owned capture-time validation; reused by brush endpoint reads."""
    def __new__(cls,layers):
        return super().__new__(cls,_validate_layers(layers))


def _validate_layers(layers):
    if isinstance(layers,PreparedLayers):return layers
    _need(type(layers) is list, 'layers must be a list')
    _need(len(layers) <= 16, 'layers must contain at most 16 entries')
    names = set()
    checked = []
    for index, layer in enumerate(layers):
        label = 'layers[%d]' % index
        _need(type(layer) is dict, label + ' must be an object')
        for key in ('name', 'domain', 'linear_rgba', 'srgb_rgba'):
            _need(key in layer, label + ' is missing ' + key)
        name = _string(layer['name'], label + '.name')
        _need(name not in names, 'duplicate layer name: ' + name)
        names.add(name)
        domain = layer['domain']
        _need(domain in DOMAINS, label + ' has invalid domain %r; expected POINT or CORNER' % (domain,))
        linear = _rgba_array(layer['linear_rgba'], label + '.linear_rgba')
        srgb = _rgba_array(layer['srgb_rgba'], label + '.srgb_rgba')
        _need(len(linear) == len(srgb), label + ' linear/sRGB sample counts differ')
        _need(np.array_equal(linear[:,3],srgb[:,3]),label + ' alpha must be encoding-independent')
        checked.append({
            'name': name, 'domain': domain,
            'linear_rgba': linear, 'srgb_rgba': srgb,
        })
    return checked


def _pair_array(pairs, label):
    _need(pairs is not None, label + ' is required for this layer domain')
    array = np.asarray(pairs)
    if array.size == 0 and array.ndim == 1:
        array = array.reshape((0, 2))
    _need(array.ndim == 2 and array.shape[1] == 2, label + ' must have shape (N, 2)')
    _need(array.dtype.kind in 'iu', label + ' must contain integer indices')
    if len(array):
        _need(int(array.min()) >= 0, label + ' must contain nonnegative indices')
    return array.astype(np.int64, copy=False)


def _check_pair_bounds(pairs, length, label):
    if len(pairs):
        _need(
            int(pairs.max()) < length,
            '%s index %d is outside the layer domain of %d entries' % (label, int(pairs.max()), length),
        )


def _resolve_layer(mapping, layers, label):
    selection = mapping['selection']
    if 'index' in selection:
        index = selection['index']
        if index >= len(layers):
            return None, index
        return layers[index], index
    name = selection['name']
    matches = [layer for layer in layers if layer['name'] == name]
    _need(len(matches) <= 1, label + ': layer name is duplicated: ' + name)
    return (matches[0] if matches else None), None


def _fallback_count(vertex_pairs, corner_pairs, label):
    # Missing/disabled maps do not read CORNER support. A loose mesh edge may
    # legitimately have no corners while still supplying its vertex pair.
    if vertex_pairs is not None:
        vertices=_pair_array(vertex_pairs,'vertex_pairs')
        if corner_pairs is not None:
            corners=np.asarray(corner_pairs)
            _need(corners.shape==(len(vertices),2),label+': endpoint counts differ')
        return len(vertices)
    arrays = []
    for pairs, name in ((vertex_pairs, 'vertex_pairs'), (corner_pairs, 'corner_pairs')):
        if pairs is not None:
            arrays.append(_pair_array(pairs, name))
    _need(bool(arrays), label + ': missing layer fallback requires vertex_pairs or corner_pairs')
    if len(arrays) == 2:
        _need(len(arrays[0]) == len(arrays[1]), label + ': vertex_pairs and corner_pairs lengths differ')
    return len(arrays[0])


def _map_endpoints_checked(mapping, layers, vertex_pairs, corner_pairs):
    if mapping is None:
        count = _fallback_count(vertex_pairs, corner_pairs, 'map_endpoints')
        return np.ones((count, 2), dtype=np.float64)

    _validate_mapping(mapping)
    layer, _ = _resolve_layer(mapping, layers, 'map_endpoints')
    if layer is None:
        policy = mapping['missing']
        if policy == 'error':
            selection = mapping['selection']
            if 'index' in selection:
                detail = 'index %d' % selection['index']
            else:
                detail = 'name %r' % selection['name']
            raise StyleMappingError(
                'map_endpoints: requested paint layer is missing (%s, %d layers)' % (detail, len(layers))
            )
        count = _fallback_count(vertex_pairs, corner_pairs, 'map_endpoints')
        value = 1.0 if policy == 'one' else 0.0
        return np.full((count, 2), value, dtype=np.float64)

    domain = layer['domain']
    if domain == 'POINT':
        pairs = _pair_array(vertex_pairs, 'vertex_pairs')
    elif domain == 'CORNER':
        pairs = _pair_array(corner_pairs, 'corner_pairs')
    else:
        raise StyleMappingError(
            'map_endpoints: layer %r has invalid domain %r; expected POINT or CORNER'
            % (layer['name'], domain)
        )

    key = 'linear_rgba' if mapping['encoding'] == 'linear_rgba' else 'srgb_rgba'
    source = layer[key]
    _check_pair_bounds(pairs, len(source), key)
    values = np.asarray(source[pairs], dtype=np.float64)
    channel = mapping['channel']
    if channel == 'mean_rgb':
        raw = values[:, :, :3].mean(axis=2)
    elif channel == 'alpha':
        raw = values[:, :, 3]
    elif channel == 'red':
        raw = values[:, :, 0]
    elif channel == 'max_rgb':
        raw = values[:, :, :3].max(axis=2)
    else:
        raise StyleMappingError('map_endpoints: unsupported channel ' + repr(channel))
    return np.asarray(np.clip(raw, 0.0, 1.0), dtype=np.float64)


def map_endpoints(mapping, layers, vertex_pairs, corner_pairs):
    """Sample a paint layer at explicit endpoint pairs.

    Returns a new ``(N, 2)`` float64 array.  POINT layers read
    ``vertex_pairs``; CORNER layers read ``corner_pairs`` and fail clearly
    when that support is absent.  The source layer arrays are never modified;
    only the derived mapping result is clamped to 0..1.
    """
    return _map_endpoints_checked(mapping, _validate_layers(layers), vertex_pairs, corner_pairs)


def map_triangle_points(mapping,layers,vertices,corners,weights):
    """Read one selected support at each intersection endpoint, never average sides."""
    if mapping is None:return np.ones(weights.shape[:2],np.float64)
    _validate_mapping(mapping)
    checked=_validate_layers(layers)
    layer,_=_resolve_layer(mapping,checked,'intersection mapping')
    if layer is None:
        if mapping['missing']=='error':raise StyleMappingError('Missing intersection color layer')
        return np.full(weights.shape[:2],1.0 if mapping['missing']=='one' else 0.0)
    indices=vertices if layer['domain']=='POINT' else corners
    data=layer[mapping['encoding']]
    if len(indices) and (indices.min()<0 or indices.max()>=len(data)):
        raise StyleMappingError('Intersection support attribute index outside domain')
    rgba=np.einsum('nei,nic->nec',weights,data[indices])
    channel=mapping['channel']
    if channel=='alpha':return rgba[:,:,3]
    if channel=='red':return rgba[:,:,0]
    if channel=='max_rgb':return rgba[:,:,:3].max(axis=2)
    return rgba[:,:,:3].mean(axis=2)

def curve_values(samples, parameters):
    """Piecewise-linear uniform sampling; ``None`` means a constant one."""
    parameters = np.asarray(parameters, dtype=np.float64)
    _need(parameters.size == 0 or bool(np.isfinite(parameters).all()), 'curve parameters must be finite')
    if samples is None:
        return np.ones(parameters.shape, dtype=np.float64)
    values = np.asarray(samples, dtype=np.float64)
    _need(values.ndim == 1 and 1 <= len(values) <= 1024, 'curve samples must be a 1D list of 1..1024 entries')
    _need(bool(np.isfinite(values).all()), 'curve samples must be finite')
    positions = np.linspace(0.0, 1.0, len(values))
    return np.asarray(np.interp(parameters, positions, values), dtype=np.float64)


def _endpoint_parameters(chain_parameters, count):
    """Normalize the explicit Root chain parameters.

    Preferred form is (N, 2), one value per endpoint.  A scalar is constant;
    (2,) is one shared endpoint pair (and takes precedence for N=2); (N, 1)
    is one value per segment; (N,) is the same convenience for N != 2.
    """
    values = np.asarray(chain_parameters, dtype=np.float64)
    _need(values.size == 0 or bool(np.isfinite(values).all()), 'chain_parameters must be finite')
    if values.ndim == 0:
        return np.full((count, 2), float(values), dtype=np.float64)
    if values.shape == (count, 2):
        return values
    if values.shape == (2,):
        return values[None, :]
    if values.shape == (count, 1):
        return values
    if values.shape == (count,):
        return values[:, None]
    raise StyleMappingError(
        'chain_parameters shape must be scalar, (N,), (2,), or (N, 2); got %r for N=%d'
        % (values.shape, count)
    )


def endpoint_style(brush, layers, vertex_pairs, corner_pairs, chain_parameters, width_px):
    """Return ``(widths[N,2], alphas[N,2], color[3])`` for one brush.

    ``width_px`` is the Root-supplied viewport width.  This function does not
    interpret ``native_size`` and does not add the historical ``+1``.
    Width and alpha products are computed from the current inputs only, never
    accumulated from a prior call.
    """
    _need(type(brush) is dict, 'brush must be a compiled brush object')
    unknown = sorted(set(brush) - set(COMPILED_BRUSH_FIELDS))
    _need(not unknown, 'compiled brush has unsupported field(s): ' + ', '.join(unknown))
    for key in COMPILED_BRUSH_FIELDS:
        _need(key in brush, 'compiled brush is missing ' + key)

    checked_layers = _validate_layers(layers)
    blend_amount = _number(brush['blend_amount'], 'brush.blend_amount', 0.0, 1.0)
    width = _number(width_px, 'width_px', 0.0)

    color = np.asarray(brush['color'], dtype=np.float64)
    _need(color.shape == (3,), 'brush.color must contain exactly 3 numbers')
    _need(bool(np.isfinite(color).all()), 'brush.color must be finite')
    _need(bool(((color >= 0.0) & (color <= 1.0)).all()), 'brush.color components must be in 0..1')

    width_factors = _map_endpoints_checked(
        brush['width_map'], checked_layers, vertex_pairs, corner_pairs,
    )
    alpha_factors = _map_endpoints_checked(
        brush['alpha_map'], checked_layers, vertex_pairs, corner_pairs,
    )
    _need(width_factors.shape[0] == alpha_factors.shape[0], 'width/alpha endpoint counts differ')
    count = width_factors.shape[0]

    parameters = _endpoint_parameters(chain_parameters, count)
    size_curve = np.broadcast_to(
        np.asarray(curve_values(brush['size_curve'], parameters), dtype=np.float64),
        (count, 2),
    )
    alpha_curve = np.broadcast_to(
        np.asarray(curve_values(brush['alpha_curve'], parameters), dtype=np.float64),
        (count, 2),
    )
    widths = width_factors * size_curve * width
    alphas = alpha_factors * alpha_curve * blend_amount
    return (
        np.asarray(widths, dtype=np.float64),
        np.asarray(alphas, dtype=np.float64),
        color.copy(),
    )


def _load_resolve_size_scale():
    global _RESOLVE_SIZE_SCALE
    if _RESOLVE_SIZE_SCALE is not None:
        return _RESOLVE_SIZE_SCALE

    from .dependencies import load
    _RESOLVE_SIZE_SCALE = load('relative_size').resolve_size_scale
    return _RESOLVE_SIZE_SCALE


def size_scale(mode, width, height, units, initial_scale_ex):
    """Explicitly resolve ScaleEx through the existing project module."""
    units = _normalize_units(units, 'units')
    if isinstance(mode, str):
        mode = str(mode)
    if isinstance(width, numbers.Integral) and not isinstance(width, (bool, np.bool_)):
        width = int(width)
    if isinstance(height, numbers.Integral) and not isinstance(height, (bool, np.bool_)):
        height = int(height)
    initial_scale_ex = _number(initial_scale_ex, 'initial_scale_ex', 0.0)
    resolve_size_scale = _load_resolve_size_scale()
    result = resolve_size_scale(
        mode=mode,
        width=width,
        height=height,
        sensor_fit=units['sensor_fit'],
        initial_scale_ex=initial_scale_ex,
        absolute_scale=units['absolute_scale'],
        relative_target_width=units['relative_target_width'],
        relative_target_height=units['relative_target_height'],
    )
    return float(result['scale_ex'])


def _binding_map(recipe, key, logical_key, source_key, allowed_fields, warnings, seen):
    rows = recipe.get(key)
    _need(type(rows) is list, 'recipe.' + key + ' must be a list')
    _need(len(rows) <= 4096, 'recipe.' + key + ' has too many entries')
    result = {}
    for index, row in enumerate(rows):
        label = 'recipe.%s[%d]' % (key, index)
        _need(type(row) is dict, label + ' must be an object')
        _warn_unknown(row, allowed_fields, label, warnings, seen)
        logical = _string(row.get(logical_key), label + '.' + logical_key)
        source = _string(row.get(source_key), label + '.' + source_key)
        _need(logical not in result, 'recipe.' + key + ' has duplicate logical entry: ' + logical)
        result[logical] = source
    return result


def _editor_group_map(editor):
    groups = editor.get('groups')
    _need(type(groups) is list, 'editor_document.groups must be a list')
    _need(len(groups) <= 32, 'editor_document.groups has too many entries')
    result = {}
    order = []
    for index, group in enumerate(groups):
        label = 'editor_document.groups[%d]' % index
        _need(type(group) is dict, label + ' must be an object')
        uid = _string(group.get('uid'), label + '.uid')
        _need(uid not in result, 'duplicate editor group uid: ' + uid)
        result[uid] = group
        order.append(uid)
    return result, order


def _boundary_groups(boundary):
    groups = boundary.get('groups')
    _need(type(groups) is list and 0 < len(groups) <= 32, 'request.boundary.groups must contain 1..32 groups')
    return groups


def _compile_map(editor_map, boundary_map, path, warnings, seen):
    if editor_map is None:
        if boundary_map is None:
            return None, False
        _validate_mapping(boundary_map, path + '/boundary')
        return copy.deepcopy(boundary_map), True

    _need(type(editor_map) is dict, path + ' must be an object')
    _warn_unknown(editor_map, _EDITOR_MAP_FIELDS, path, warnings, seen)
    enabled = editor_map.get('enabled')
    _need(type(enabled) is bool, path + '.enabled must be boolean')
    if not enabled:
        if boundary_map is not None:
            _warning(
                warnings, seen, 'map-boundary-mismatch', path,
                'Editor map is disabled but the boundary request contains a map; editor disabled state wins.',
            )
        return None, False

    kind = editor_map.get('selection_kind')
    _need(kind in ('index', 'name'), path + '.selection_kind is unsupported')
    if kind == 'index':
        layer_index = _integer(editor_map.get('layer_index'), path + '.layer_index', 0)
        selection = {'index': layer_index}
    else:
        layer_name = _string(editor_map.get('layer_name'), path + '.layer_name')
        selection = {'name': layer_name}

    channel = editor_map.get('channel')
    encoding = editor_map.get('encoding')
    missing = editor_map.get('missing')
    _need(channel in CHANNELS, path + '.channel is unsupported')
    _need(encoding in ENCODINGS, path + '.encoding is unsupported')
    _need(missing in MISSING_POLICIES, path + '.missing is unsupported')
    compiled = {
        'selection': selection,
        'channel': channel,
        'encoding': encoding,
        'missing': missing,
    }
    if boundary_map is not None:
        _validate_mapping(boundary_map, path + '/boundary')
        if compiled != boundary_map:
            _warning(
                warnings, seen, 'map-boundary-mismatch', path,
                'Editor and boundary maps differ; the editor map is used.',
                editor=compiled, boundary=boundary_map,
            )
    return compiled, True


def _curve_samples(curve, boundary_samples, path, warnings, seen):
    if curve is None:
        if boundary_samples is None:
            return None, False
        values = _number_list(boundary_samples, path + '/boundary_samples')
        return values, True

    _need(type(curve) is dict, path + ' must be an object')
    _warn_unknown(curve, _CURVE_FIELDS, path, warnings, seen)
    _warning(warnings,seen,'sampled-curve-scope',path,
        'Only enabled/uniform samples are evaluated; native points, handles, extend, clip, tone and levels are preserved but not evaluated.',
        severity='unverified')
    enabled = curve.get('enabled')
    _need(type(enabled) is bool, path + '.enabled must be boolean')
    samples = curve.get('samples')
    points = curve.get('points')
    if points is not None:
        _need(type(points) is list, path + '.points must be a list')
        for index, point in enumerate(points):
            _need(type(point) is dict, '%s.points[%d] must be an object' % (path, index))
            _warn_unknown(point, _CURVE_POINT_FIELDS, '%s.points[%d]' % (path, index), warnings, seen)

    if not enabled:
        if boundary_samples is not None:
            _warning(
                warnings, seen, 'curve-boundary-mismatch', path,
                'Editor curve is disabled but the boundary request contains samples; editor state wins.',
            )
        return None, False

    if samples is None:
        if boundary_samples is None:
            raise StyleMappingError(path + ' is enabled but has no uniform samples')
        values = _number_list(boundary_samples, path + '/boundary_samples')
        _warning(
            warnings, seen, 'curve-samples-from-boundary', path,
            'Editor curve has no samples; the boundary samples are used for evaluation.',
        )
        return values, True

    values = _number_list(samples, path + '/samples')
    if boundary_samples is not None and not _same_numbers(values, boundary_samples):
        _warning(
            warnings, seen, 'curve-boundary-mismatch', path,
            'Editor and boundary curve samples differ; the editor samples are used.',
        )
    return values, True


def _number_list(value, label):
    _need(type(value) is list, label + ' must be a list')
    _need(1 <= len(value) <= 1024, label + ' must contain 1..1024 entries')
    result = []
    for item in value:
        result.append(_number(item, label + ' entry',0.0,1.0))
    return result


def _same_numbers(left, right):
    if type(right) is not list or len(left) != len(right):
        return False
    return all(abs(a - b) <= 1e-12 for a, b in zip(left, right))


def _compile_brush(editor_brush, boundary_brush, path, slot, warnings, seen):
    if editor_brush is None and boundary_brush is None:
        return None, None

    if editor_brush is not None:
        _need(type(editor_brush) is dict, path + ' must be an object')
        _warn_unknown(editor_brush, _EDITOR_BRUSH_FIELDS, path, warnings, seen)
        blend_amount = _number(editor_brush.get('blend_amount'), path + '.blend_amount', 0.0, 1.0)
        color = _color(editor_brush.get('color'), path + '.color')
        width_map, width_enabled = _compile_map(
            editor_brush.get('width_map'),
            boundary_brush.get('width_map') if type(boundary_brush) is dict else None,
            path + '/width_map', warnings, seen,
        )
        alpha_map, alpha_enabled = _compile_map(
            editor_brush.get('alpha_map'),
            boundary_brush.get('alpha_map') if type(boundary_brush) is dict else None,
            path + '/alpha_map', warnings, seen,
        )
        size_curve, size_enabled = _curve_samples(
            editor_brush.get('size_curve'),
            boundary_brush.get('size_curve') if type(boundary_brush) is dict else None,
            path + '/size_curve', warnings, seen,
        )
        alpha_curve, alpha_enabled_curve = _curve_samples(
            editor_brush.get('alpha_curve'),
            boundary_brush.get('alpha_curve') if type(boundary_brush) is dict else None,
            path + '/alpha_curve', warnings, seen,
        )
        compiled = {
            'blend_amount': blend_amount,
            'color': color,
            'width_map': width_map,
            'alpha_map': alpha_map,
            'size_curve': size_curve,
            'alpha_curve': alpha_curve,
        }
        meta = {
            'slot': slot,
            'source': 'editor',
            'uid': editor_brush.get('uid'),
            'native_size': _optional_number(editor_brush.get('native_size'), path + '.native_size', 0.0),
            'width_map_enabled': width_enabled,
            'alpha_map_enabled': alpha_enabled,
            'size_curve_enabled': size_enabled,
            'alpha_curve_enabled': alpha_enabled_curve,
            'native_curve_control_points_in_editor_raw': _has_curve_points(editor_brush),
        }
        if boundary_brush is None:
            _warning(
                warnings, seen, 'brush-slot-editor-only', path,
                'Special brush slot exists in the editor state but not in the boundary request; it is preserved.',
                slot=slot,
            )
        else:
            _need(type(boundary_brush) is dict, path + '/boundary must be an object')
            _warn_unknown(boundary_brush, _BOUNDARY_BRUSH_FIELDS, path + '/boundary', warnings, seen)
            boundary_compiled = _boundary_brush_values(boundary_brush, path + '/boundary')
            if boundary_compiled != compiled:
                _warning(
                    warnings, seen, 'brush-boundary-mismatch', path,
                    'Editor and boundary brush values differ; the editor values are used.',
                    slot=slot,
                )
        return compiled, meta

    _need(type(boundary_brush) is dict, path + ' must be an object')
    _warn_unknown(boundary_brush, _BOUNDARY_BRUSH_FIELDS, path, warnings, seen)
    compiled = _boundary_brush_values(boundary_brush, path)
    meta = {
        'slot': slot,
        'source': 'boundary',
        'uid': None,
        'native_size': _optional_number(boundary_brush.get('native_size'), path + '.native_size', 0.0),
        'width_map_enabled': boundary_brush.get('width_map') is not None,
        'alpha_map_enabled': boundary_brush.get('alpha_map') is not None,
        'size_curve_enabled': boundary_brush.get('size_curve') is not None,
        'alpha_curve_enabled': boundary_brush.get('alpha_curve') is not None,
        'native_curve_control_points_in_editor_raw': False,
    }
    _warning(
        warnings, seen, 'brush-slot-boundary-only', path,
        'Special brush slot exists only in the boundary request; no editor native curve state is available.',
        slot=slot,
    )
    return compiled, meta


def _boundary_brush_values(brush, path):
    color = _color(brush.get('color'), path + '.color')
    blend_amount = _number(brush.get('blend_amount'), path + '.blend_amount', 0.0, 1.0)
    maps = {}
    for key in ('width_map', 'alpha_map'):
        value = brush.get(key)
        if value is None:
            maps[key] = None
        else:
            _validate_mapping(value, path + '.' + key)
            maps[key] = copy.deepcopy(value)
    curves = {}
    for key in ('size_curve', 'alpha_curve'):
        value = brush.get(key)
        curves[key] = None if value is None else _number_list(value, path + '.' + key)
    return {
        'blend_amount': blend_amount,
        'color': color,
        'width_map': maps['width_map'],
        'alpha_map': maps['alpha_map'],
        'size_curve': curves['size_curve'],
        'alpha_curve': curves['alpha_curve'],
    }


def _color(value, label):
    _need(type(value) is list and len(value) == 3, label + ' must be a 3-item list')
    result = [_number(item, label + ' component', 0.0, 1.0) for item in value]
    return result


def _has_curve_points(brush):
    for key in ('size_curve', 'alpha_curve'):
        curve = brush.get(key)
        if type(curve) is dict and type(curve.get('points')) is list and curve['points']:
            return True
    return False


def _optional_size_mode(value, label):
    if value is None:
        return None
    _need(type(value) is str and value in SIZE_MODES, label + ' must be RELATIVE or ABSOLUTE')
    return value


def _settings_copy(boundary, editor):
    settings = boundary.get('settings')
    _need(type(settings) is dict, 'request.boundary.settings must be an object')
    editor_settings = editor.get('settings')
    _need(type(editor_settings) is dict, 'editor_document.settings must be an object')
    return copy.deepcopy(settings), copy.deepcopy(editor_settings)


def _normalize_units(units, label):
    units = _object(units, label)
    for key in _UNITS_FIELDS:
        _need(key in units, label + ' is missing ' + key)
    result = copy.deepcopy(units)
    result['absolute_scale'] = _number(units['absolute_scale'], label + '.absolute_scale', 0.0)
    for key in ('relative_target_width', 'relative_target_height'):
        value = units[key]
        _need(
            value is None or (
                isinstance(value, numbers.Integral)
                and not isinstance(value, (bool, np.bool_))
                and int(value) >= 0
            ),
            '%s.%s must be a nonnegative integer or None' % (label, key),
        )
        result[key] = None if value is None else int(value)
    _need(units['sensor_fit'] in ('AUTO', 'HORIZONTAL', 'VERTICAL'), label + '.sensor_fit is unsupported')
    result['sensor_fit'] = str(units['sensor_fit'])
    return result


def _units_copy(boundary):
    units = boundary.get('units')
    _need(type(units) is dict, 'request.boundary.units must be an object')
    return _normalize_units(units, 'request.boundary.units')


def _native_curve_summary(editor_group_map):
    curve_count = 0
    point_count = 0
    enabled_count = 0
    for group in editor_group_map.values():
        brushes = group.get('brushes')
        if type(brushes) is not dict:
            continue
        for brush in brushes.values():
            if type(brush) is not dict:
                continue
            for key in ('size_curve', 'alpha_curve'):
                curve = brush.get(key)
                if type(curve) is not dict:
                    continue
                curve_count += 1
                if curve.get('enabled') is True:
                    enabled_count += 1
                points = curve.get('points')
                if type(points) is list:
                    point_count += len(points)
    return {
        'curve_count': curve_count,
        'point_count': point_count,
        'enabled_curve_count': enabled_count,
        'control_points_preserved_in_raw': True,
        'evaluation': 'enabled original uniform samples only',
        'native_control_point_evaluation': False,
    }


def compile_preset(document, recipe):
    """Compile the real C4 editor/boundary state into a style plan.

    The returned ``raw`` is a deep copy of the complete input document.  Each
    group also retains ``raw`` (boundary group) and ``editor_raw`` (full editor
    group, including native curve control points).

    Unbound object/material selectors and malformed core fields raise
    :class:`StyleMappingError`.  Unknown extra schema fields are preserved in
    raw and reported as ``unverified`` warnings; they are never silently
    marked as understood.  The compact map contract itself remains strict.
    """
    warnings = []
    seen = set()

    document = _object(document, 'document')
    recipe = _object(recipe, 'recipe')
    _warn_unknown(document, _DOCUMENT_FIELDS, 'document', warnings, seen)
    _warn_unknown(recipe, {
        'C4_complete', 'authority', 'diagnostic_object_map', 'geometry_payloads_modified',
        'material_bindings', 'native_capture', 'object_bindings',
        'original_engineering_accepted', 'prior_logical_input', 'raw_input', 'revision',
        'schema', 'slot_witnesses', 'source_current', 'source_sha256',
    }, 'recipe', warnings, seen)

    inputs = _object(document.get('inputs'), 'document.inputs')
    request = _object(document.get('request'), 'document.request')
    from . import brush_effects
    brush_effects.records(document)
    _warn_unknown(inputs, _INPUTS_FIELDS, 'document.inputs', warnings, seen)
    _warn_unknown(request, _REQUEST_FIELDS | {'brush_effects'}, 'document.request', warnings, seen)

    projection = _object(inputs.get('projection'), 'document.inputs.projection')
    editor = _object(projection.get('editor_document'), 'document.inputs.projection.editor_document')
    boundary = _object(request.get('boundary'), 'document.request.boundary')
    _warn_unknown(projection, _PROJECTION_FIELDS, 'document.inputs.projection', warnings, seen)
    _warn_unknown(editor, _EDITOR_DOCUMENT_FIELDS, 'document.inputs.projection.editor_document', warnings, seen)
    _warn_unknown(boundary, _BOUNDARY_FIELDS, 'document.request.boundary', warnings, seen)

    settings, editor_settings = _settings_copy(boundary, editor)
    _warn_unknown(settings, _BOUNDARY_SETTINGS_FIELDS, 'request.boundary.settings', warnings, seen)
    _warn_unknown(editor_settings, _EDITOR_SETTINGS_FIELDS, 'editor_document.settings', warnings, seen)
    units = _units_copy(boundary)
    _warn_unknown(units, _UNITS_FIELDS, 'request.boundary.units', warnings, seen)

    editor_group_map, editor_order = _editor_group_map(editor)
    boundary_groups = _boundary_groups(boundary)
    object_bindings = _binding_map(
        recipe, 'object_bindings', 'logical_id', 'source_name', _OBJECT_BINDING_FIELDS,
        warnings, seen,
    )
    material_bindings = _binding_map(
        recipe, 'material_bindings', 'logical_name', 'source_name', _MATERIAL_BINDING_FIELDS,
        warnings, seen,
    )

    groups = []
    binding_rows = []
    used_objects = []
    used_materials = []
    native_size_paths = []
    native_size_values = {}
    size_resolution_paths = []
    visibility_paths = []
    curve_point_paths = []
    label_paths = []
    seen_ids = set()
    seen_priorities = set()

    from . import edge_sources
    edge_records = edge_sources.records(document)
    from . import group_lifecycle
    owned_group_ids = set(group_lifecycle.owned(document)) if 'group_lifecycle' in request else set()
    for index, boundary_group in enumerate(boundary_groups):
        _need(type(boundary_group) is dict, 'request.boundary.groups[%d] must be an object' % index)
        group_id = _string(boundary_group.get('id'), 'request.boundary.groups[%d].id' % index)
        _need(group_id not in seen_ids, 'duplicate boundary group id: ' + group_id)
        seen_ids.add(group_id)
        path = 'groups/' + group_id
        _warn_unknown(
            boundary_group, _BOUNDARY_GROUP_FIELDS | set(edge_records.get(group_id, {}).get('authors', {})),
            'request.boundary.groups[%d]' % index, warnings, seen,
        )

        priority = _integer(boundary_group.get('priority'), path + '.priority', -100000, 100000)
        _need(priority not in seen_priorities, 'duplicate boundary group priority: %d' % priority)
        seen_priorities.add(priority)
        object_ids_logical = _string_list(boundary_group.get('object_ids'), path + '.object_ids')
        material_names_logical = _string_list(boundary_group.get('material_names'), path + '.material_names')
        _need(
            bool(object_ids_logical or material_names_logical) or group_id in owned_group_ids,
            path + ': group must select at least one object or material',
        )

        object_ids = []
        for logical in object_ids_logical:
            _need(
                logical in object_bindings,
                path + ': object selector %r has no recipe.object_bindings entry' % logical,
            )
            object_ids.append(object_bindings[logical])
        material_names = []
        for logical in material_names_logical:
            _need(
                logical in material_bindings,
                path + ': material selector %r has no recipe.material_bindings entry' % logical,
            )
            material_names.append(material_bindings[logical])
        _need(len(set(object_ids)) == len(object_ids), path + ': translated object names are not unique')
        _need(len(set(material_names)) == len(material_names), path + ': translated material names are not unique')
        used_objects.extend(object_ids_logical)
        used_materials.extend(material_names_logical)

        visible_enabled = _boolean(boundary_group.get('visible_enabled'), path + '.visible_enabled')
        hidden_enabled = _boolean(boundary_group.get('hidden_enabled'), path + '.hidden_enabled')
        size_mode = _optional_size_mode(boundary_group.get('size_mode'), path + '.size_mode')
        initial_scale_ex = _optional_number(
            boundary_group.get('initial_scale_ex'), path + '.initial_scale_ex', 0.0,
        )
        coverage_extent_factor = _optional_number(
            boundary_group.get('coverage_extent_factor'), path + '.coverage_extent_factor', 0.0,
        )

        editor_group = editor_group_map.get(group_id)
        if editor_group is not None:
            _warn_unknown(editor_group, _EDITOR_GROUP_FIELDS, path + '/editor', warnings, seen)
            editor_priority = _integer(editor_group.get('priority'), path + '/editor.priority', -100000, 100000)
            if editor_priority != priority:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.priority',
                         'Editor and boundary priorities differ; boundary priority is used.',
                         editor=editor_priority, boundary=priority)
            editor_objects = _string_list(editor_group.get('object_ids'), path + '/editor.object_ids')
            editor_materials = _string_list(editor_group.get('material_names'), path + '/editor.material_names')
            if editor_objects != object_ids_logical:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.object_ids',
                         'Editor and boundary object selectors differ; boundary selectors are used.')
            if editor_materials != material_names_logical:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.material_names',
                         'Editor and boundary material selectors differ; boundary selectors are used.')
            for field, boundary_value in (('visible_enabled', visible_enabled), ('hidden_enabled', hidden_enabled)):
                editor_value = _boolean(editor_group.get(field), path + '/editor.' + field)
                if editor_value != boundary_value:
                    _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor/' + field,
                             'Editor and boundary boolean values differ; boundary value is used.')
            silhouettes = _optional_boolean(editor_group.get('silhouette_enabled'), path + '/editor.silhouette_enabled')
            marked = _optional_boolean(editor_group.get('marked_enabled'), path + '/editor.marked_enabled')
            smoothing = _optional_boolean(editor_group.get('smoothing_enabled'), path + '/editor.smoothing_enabled')
            editor_size_mode = _optional_size_mode(editor_group.get('size_mode'), path + '/editor.size_mode')
            if editor_size_mode is not None and size_mode is not None and editor_size_mode != size_mode:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.size_mode',
                         'Editor and boundary size modes differ; boundary mode is used.')
            editor_initial = _optional_number(editor_group.get('initial_scale_ex'), path + '/editor.initial_scale_ex', 0.0)
            if editor_initial is not None and initial_scale_ex is not None and editor_initial != initial_scale_ex:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.initial_scale_ex',
                         'Editor and boundary initial ScaleEx differ; boundary value is used.')
            editor_factor = _optional_number(
                editor_group.get('coverage_extent_factor'), path + '/editor.coverage_extent_factor', 0.0,
            )
            if editor_factor is not None and coverage_extent_factor is not None and editor_factor != coverage_extent_factor:
                _warning(warnings, seen, 'editor-boundary-mismatch', path + '/editor.coverage_extent_factor',
                         'Editor and boundary coverage factors differ; boundary value is used.')
            label = editor_group.get('label')
            if label is not None:
                label = _string(label, path + '/editor.label')
            editor_brushes = editor_group.get('brushes')
            _need(type(editor_brushes) is dict, path + '/editor.brushes must be an object')
        else:
            silhouettes = marked = smoothing = None
            label = None
            editor_brushes = {}
            _warning(warnings, seen, 'missing-editor-group', path,
                     'Boundary group has no matching editor group; native curve control points are unavailable.')

        element_mapping = {}
        element_mapping_explicit = {}
        for field, default in (('line_set_id', 1), ('elements_only', False)):
            supplied = []
            for source, source_path in ((boundary_group, path), (editor_group, path + '/editor')):
                if source is None or field not in source:
                    continue
                value = (_integer(source[field], source_path + '.' + field, 1, 8)
                         if field == 'line_set_id' else _boolean(source[field], source_path + '.' + field))
                supplied.append(value)
            _need(len(supplied) < 2 or supplied[0] == supplied[1],
                  path + ': editor and boundary ' + field + ' values differ')
            element_mapping[field] = supplied[0] if supplied else default
            # False identifies the default source; an explicit 1/False is True.
            element_mapping_explicit[field] = bool(supplied)

        known_slots = SPECIAL_BRUSH_SLOTS + tuple(sorted(edge_records.get(group_id, {}).get('authors', {})))
        unknown_editor_slots = sorted(set(editor_brushes) - set(known_slots))
        unknown_boundary_slots = sorted(
            {key for key in boundary_group if key.endswith('_brush') and key not in known_slots}
        )
        for slot in unknown_editor_slots + unknown_boundary_slots:
            _warning(
                warnings, seen, 'unknown-brush-slot', path + '/brushes/' + slot,
                'Unknown special brush slot is preserved in raw but is not compiled.',
                severity='unverified', slot=slot,
            )

        brushes = {}
        brush_meta = {}
        for slot in known_slots:
            editor_brush = editor_brushes.get(slot)
            boundary_brush = boundary_group.get(slot)
            compiled, meta = _compile_brush(
                editor_brush, boundary_brush, path + '/brushes/' + slot, slot, warnings, seen,
            )
            if compiled is not None:
                brushes[slot] = compiled
                brush_meta[slot] = meta

        _need('brush' in brushes, path + ': required brush slot is missing')
        brush_slots = list(brushes)
        uncompiled_slots = sorted(set(unknown_editor_slots) | set(unknown_boundary_slots))

        for slot, meta in brush_meta.items():
            if meta['native_size'] is not None:
                native_size_paths.append(path + '/brushes/' + slot + '/native_size')
                native_size_values.setdefault(group_id, {})[slot] = meta['native_size']
        if size_mode is not None or initial_scale_ex is not None:
            size_resolution_paths.extend([path + '/size_mode', path + '/initial_scale_ex'])
        if coverage_extent_factor is not None:
            size_resolution_paths.append(path + '/coverage_extent_factor')
        visibility_paths.extend([
            path + '/visible_enabled', path + '/hidden_enabled',
            path + '/silhouette_enabled', path + '/marked_enabled', path + '/smoothing_enabled',
        ])
        if editor_brushes:
            curve_point_paths.append(path + '/editor_raw/brushes/*/size_curve/points')
            curve_point_paths.append(path + '/editor_raw/brushes/*/alpha_curve/points')
        if label is not None:
            label_paths.append(path + '/label')

        unconsumed = []
        if any(meta['native_size'] is not None for meta in brush_meta.values()):
            unconsumed.append('native_size')
        if size_mode is not None or initial_scale_ex is not None or coverage_extent_factor is not None:
            unconsumed.append('size_resolution')
        unconsumed.append('visibility_selection')
        if editor_brushes:
            unconsumed.append('native_curve_control_points')
        if label is not None:
            unconsumed.append('group_label')

        group_plan = {
            'id': group_id,
            'priority': priority,
            'object_ids': object_ids,
            'material_names': material_names,
            'object_ids_logical': object_ids_logical,
            'material_names_logical': material_names_logical,
            'visible_enabled': visible_enabled,
            'hidden_enabled': hidden_enabled,
            'silhouette_enabled': silhouettes,
            'marked_enabled': marked,
            'smoothing_enabled': smoothing,
            'size_mode': size_mode,
            'initial_scale_ex': initial_scale_ex,
            'coverage_extent_factor': coverage_extent_factor,
            'source_line_set_id': element_mapping['line_set_id'],
            'output_to_elements_only': element_mapping['elements_only'],
            'element_mapping_explicit': element_mapping_explicit,
            'label': label,
            'brushes': brushes,
            'brush': brushes['brush'],
            'brush_slots': brush_slots,
            'uncompiled_brush_slots': uncompiled_slots,
            'brush_meta': brush_meta,
            'raw': copy.deepcopy(boundary_group),
            'editor_raw': copy.deepcopy(editor_group) if editor_group is not None else None,
            'unconsumed': unconsumed,
        }
        for slot, compiled in brushes.items():
            group_plan[slot] = compiled
        groups.append(group_plan)
        binding_rows.append({
            'id': group_id,
            'object_logical': list(object_ids_logical),
            'object_real': list(object_ids),
            'material_logical': list(material_names_logical),
            'material_real': list(material_names),
        })

    from . import wire_sources
    wire_sources.attach_plan(document, groups)
    edge_sources.attach_plan(document, groups)
    from . import ix_sources
    ix_sources.attach_plan(document, groups)
    if any(key in request for key in ('wire_sources', 'edge_sources', 'intersection_sources', 'group_lifecycle')):
        # Resolve retained aliases even when wire is disabled: author identities
        # survive switching back to legacy sources, with no read-time migration.
        from .source_bindings import recover
        recover(document)

    from . import selector_bindings
    selector_bindings.attach(document,groups)
    from . import line_functions
    line_functions.records(document)
    if 'group_lifecycle' in request:
        from . import group_lifecycle
        group_lifecycle.active(document, groups)
    from . import line_hierarchy
    line_hierarchy.attach(document, groups)
    for group_id in editor_order:
        if group_id not in seen_ids:
            _warning(
                warnings, seen, 'unrepresented-editor-group', 'editor_document.groups/' + group_id,
                'Editor group has no boundary group and is preserved only in raw.',
            )

    partial = projection.get('partial', True)
    if type(partial) is not bool:
        _warning(warnings, seen, 'invalid-projection-status', 'inputs.projection.partial',
                 'Projection partial flag is not boolean; treated as partial.', severity='unverified')
        partial = True
    native_equivalent = projection.get('native_equivalent', False)
    if any(key in request for key in ('wire_sources', 'edge_sources', 'intersection_sources', 'selector_bindings', 'group_lifecycle', 'line_functions', 'line_hierarchy', 'brush_effects')):
        native_equivalent = False
    if type(native_equivalent) is not bool:
        _warning(warnings, seen, 'invalid-projection-status', 'inputs.projection.native_equivalent',
                 'Projection native-equivalence flag is not boolean; treated as non-native.', severity='unverified')
        native_equivalent = False
    losses = projection.get('losses', [])
    _need(type(losses) is list, 'inputs.projection.losses must be a list')
    if partial or not native_equivalent:
        loss_codes = sorted({
            str(row.get('code')) for row in losses if type(row) is dict and row.get('code') is not None
        })
        _warning(
            warnings, seen, 'projection-not-native-equivalent', 'inputs.projection',
            'The persisted projection is partial and/or declared non-native; this plan does not claim full native reconstruction.',
            severity='warning', partial=partial, native_equivalent=native_equivalent,
            loss_count=len(losses), loss_codes=loss_codes,
        )

    native_curves = _native_curve_summary(editor_group_map)
    unconsumed = []
    if native_size_paths:
        unconsumed.append({
            'capability': 'native_size',
            'paths': native_size_paths,
            'values': native_size_values,
            'reason': 'Kept for audit only. Root supplies width_px; no native size+1 conversion is inferred.',
        })
    if size_resolution_paths or units:
        unconsumed.append({
            'capability': 'size_resolution',
            'paths': size_resolution_paths + ['request.boundary.units.*'],
            'reason': 'Call size_scale explicitly. compile_preset does not apply units or size_mode.',
        })
    if visibility_paths:
        unconsumed.append({
            'capability': 'visibility_selection',
            'paths': visibility_paths,
            'reason': 'Root selects the brush slot and visibility; this module only compiles style values.',
        })
    if curve_point_paths:
        unconsumed.append({
            'capability': 'native_curve_control_points',
            'paths': curve_point_paths,
            'reason': 'Control points are preserved in raw/editor_raw; only original uniform samples are evaluated.',
        })
    if label_paths:
        unconsumed.append({
            'capability': 'group_label',
            'paths': label_paths,
            'reason': 'Display metadata only.',
        })
    unconsumed.append({
        'capability': 'geometry_settings',
        'paths': ['request.boundary.settings.*'],
        'keys': sorted(settings),
        'reason': 'Raster/geometry sampling and coverage settings are owned by Root, not this style mapping.',
    })
    unconsumed.append({
        'capability': 'full_preset',
        'paths': ['inputs.full_preset'],
        'reason': 'The complete native preset remains in raw; this module maps the editor/boundary style subset only.',
    })
    unconsumed.append({
        'capability': 'non_boundary_request_sections',
        'paths': [
            'request.intersection', 'request.intersection_aliases',
            'request.layers', 'request.boundary_aliases',
        ],
        'reason': 'Outside this style mapping module; preserved in raw.',
    })
    unconsumed.append({
        'capability': 'projection_native_equivalence',
        'paths': ['inputs.projection.losses', 'inputs.projection.field_matrix'],
        'reason': 'Projection losses and field matrix are retained for audit, not reproduced.',
    })
    unconsumed.append({
        'capability': 'recipe_metadata',
        'paths': [
            'recipe.C4_complete', 'recipe.authority', 'recipe.diagnostic_object_map',
            'recipe.geometry_payloads_modified', 'recipe.native_capture',
            'recipe.original_engineering_accepted', 'recipe.prior_logical_input',
            'recipe.raw_input', 'recipe.slot_witnesses', 'recipe.source_current',
            'recipe.source_sha256',
        ],
        'reason': 'Audit/provenance metadata; only object and material bindings are consumed.',
    })

    for item in unconsumed:
        _warning(
            warnings, seen, 'unconsumed-capability', item['capability'], item['reason'],
            severity='warning', paths=item['paths'][:64], path_count=len(item['paths']),
        )

    unknown_field_count = sum(1 for row in warnings if row['code'] == 'unknown-field')
    unknown_slot_count = sum(1 for row in warnings if row['code'] == 'unknown-brush-slot')

    binding_report = {
        'group_count': len(groups),
        'object_binding_count': len(object_bindings),
        'material_binding_count': len(material_bindings),
        'object_selector_count': sum(len(row['object_logical']) for row in binding_rows),
        'unique_object_logical_count': len(set(used_objects)),
        'unique_object_real_count': len({name for row in binding_rows for name in row['object_real']}),
        'material_selector_count': sum(len(row['material_logical']) for row in binding_rows),
        'unique_material_logical_count': len(set(used_materials)),
        'unique_material_real_count': len({name for row in binding_rows for name in row['material_real']}),
        'unused_object_bindings': sorted(set(object_bindings) - set(used_objects)),
        'unused_material_bindings': sorted(set(material_bindings) - set(used_materials)),
        'unbound_object_selectors': [],
        'unbound_material_selectors': [],
        'groups': binding_rows,
    }

    return {
        'schema': SCHEMA,
        'status': {
            'style_plan': 'compiled',
            'native_equivalent': native_equivalent,
            'projection_partial': partial,
            'full_native_reconstruction': False,
            'known_fields_only': unknown_field_count == 0 and unknown_slot_count == 0,
            'unknown_field_count': unknown_field_count,
            'unknown_brush_slot_count': unknown_slot_count,
            'unconsumed_capability_count': len(unconsumed),
            'all_capabilities_consumed': len(unconsumed) == 0,
            'unknown_or_unconsumed': bool(unknown_field_count or unknown_slot_count or unconsumed),
            'warning_count': len(warnings),
            'notes': [
                'Explicit style fields are mapped; native C4 equivalence is not claimed.',
                'Curve control points are preserved in raw/editor_raw, not reconstructed from samples.',
            ],
        },
        'source': {
            'joint_schema': document.get('schema'),
            'projection_schema': projection.get('schema'),
            'editor_schema': editor.get('schema'),
            'editor_revision': editor.get('revision'),
            'editor_draft_uid': editor.get('draft_uid'),
            'boundary_schema': boundary.get('schema'),
            'recipe_schema': recipe.get('schema'),
            'recipe_revision': recipe.get('revision'),
        },
        'raw': copy.deepcopy(document),
        'recipe_raw': copy.deepcopy(recipe),
        'bindings': {
            'object': copy.deepcopy(object_bindings),
            'material': copy.deepcopy(material_bindings),
        },
        'binding_report': binding_report,
        'settings': settings,
        'editor_settings': editor_settings,
        'units': units,
        'groups': groups,
        'native_curves': native_curves,
        'consumed': [
            'recipe.object_bindings: logical_id -> source_name',
            'recipe.material_bindings: logical_name -> source_name',
            'request.boundary.groups: id, priority, selectors, visibility flags',
            'request.boundary.groups[].brushes: blend_amount, color, width_map, alpha_map, size_curve, alpha_curve',
            'inputs.projection.editor_document.groups[].brushes: all special slots, enabled flags, uniform samples, control points copied to raw/editor_raw',
        ],
        'unconsumed': unconsumed,
        'warnings': warnings,
        'dependencies': {'size_scale': copy.deepcopy(SIZE_SCALE_DEPENDENCY)},
    }


__all__ = [
    'CHECK_SCHEMA',
    'COMPILED_BRUSH_FIELDS',
    'SCHEMA',
    'SIZE_SCALE_DEPENDENCY',
    'SPECIAL_BRUSH_SLOTS',
    'StyleMappingError',
    'compile_preset',
    'curve_values',
    'endpoint_style',
    'map_endpoints',
    'size_scale',
]
