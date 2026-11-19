# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure, persistent chain author state keyed by recovered actual brush IDs.

No file reads, phase execution, Blender access, or style/curve/map consumption.
Native pixel lengths are retained without a runtime resolution conversion.
"""
import math

from ._support.chain_policy import checked_policy

SCHEMA = 'toon.viewport-chain-controls.v2'
LEGACY_SCHEMA = 'toon.viewport-chain-controls.v1'
BINDINGS_SCHEMA = 'toon.viewport-native-bindings.v1'
MAX_ACTUALS = 512
MAX_ROUTES = 8192
MAX_LENGTH_PX = 65536
_LEGACY_ROW_FIELDS = {'boundary_enabled', 'intersection_enabled', 'max_gap_px', 'policy'}
_ROW_FIELDS = _LEGACY_ROW_FIELDS | {'intersection_max_gap_px'}
_POLICY_FIELDS = {'split_angle_degrees', 'line_direction_degrees',
                  'closed_orientation', 'seam_tokens', 'min_length_px', 'link_length_px'}
_NATIVE_FIELDS = {'line_link_length', 'line_split_angle', 'line_direction',
                  'min_line_length', 'loop_direction_type'}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _identifier(value, label):
    _need(type(value) is str and 0 < len(value) <= 256,
          label + ': nonempty actual ID of at most 256 characters required')
    try:
        value.encode('utf8')
    except UnicodeError as exc:
        raise ValueError(label + ': actual ID must encode as UTF-8') from exc
    return value


def _finite_number(value, label):
    _need(type(value) in (int, float), label + ': finite number required; bool is not numeric')
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(label + ': number exceeds binary64 range') from exc
    _need(math.isfinite(converted), label + ': finite number required')
    return value


def _length(value, label):
    value = _finite_number(value, label)
    _need(0 <= value <= MAX_LENGTH_PX, label + ': length must be in 0..65536')
    return value


def _binding_index(bindings):
    _need(type(bindings) is dict and bindings.get('schema') == BINDINGS_SCHEMA,
          'recover(document) bindings schema required')
    actuals = bindings.get('actuals')
    _need(type(actuals) is dict and 0 < len(actuals) <= MAX_ACTUALS,
          'bindings.actuals must contain 1..512 recovered actuals')
    eligible = {}
    for actual, row in actuals.items():
        _identifier(actual, 'bindings.actuals key')
        _need(type(row) is dict and row.get('actual_brush_id') == actual,
              'bindings actual row identity differs from its key')
        eligible[actual] = set()
    routes = bindings.get('routes')
    _need(type(routes) is list and len(routes) <= MAX_ROUTES,
          'bindings.routes must be a bounded list')
    for row in routes:
        _need(type(row) is dict, 'binding route must be an object')
        actual = _identifier(row.get('actual_brush_id'), 'route actual')
        kind = row.get('kind')
        _need(type(kind) is str and kind in ('boundary', 'intersection'),
              'binding route kind must be boundary or intersection')
        _need(actual in eligible, 'binding route references an unknown actual')
        eligible[actual].add(kind)
    return actuals, eligible


def _checked_row(row, *, legacy=False):
    fields = _LEGACY_ROW_FIELDS if legacy else _ROW_FIELDS
    _need(type(row) is dict and set(row) == fields,
          'chain brush row must have exactly the fields defined by its schema')
    for name in ('boundary_enabled', 'intersection_enabled'):
        _need(type(row[name]) is bool, name + ': boolean required')
    gap = _length(row['max_gap_px'], 'max_gap_px')
    policy = row['policy']
    _need(type(policy) is dict and set(policy) == _POLICY_FIELDS,
          'policy must have exactly the six v1 policy fields')
    _need(type(policy['seam_tokens']) is dict and not policy['seam_tokens'],
          'per-actual author state supports only empty seam_tokens')
    # The public v1 validator retains numeric values, rejects bool/NaN/huge
    # numbers and nonzero link, and returns a detached policy dictionary.
    normalized = checked_policy(policy)
    # Validate the complete old row first. Never inherit boundary gap into IX:
    # a v1 scene with IX already enabled must retain its previous gap-zero image.
    intersection_gap = 0 if legacy else _length(row['intersection_max_gap_px'], 'intersection_max_gap_px')
    return dict(boundary_enabled=row['boundary_enabled'],
                intersection_enabled=row['intersection_enabled'],
                max_gap_px=gap, intersection_max_gap_px=intersection_gap, policy=normalized)


def _checked_payload(payload):
    """Validate the payload itself, without guessing stale actual eligibility."""
    _need(type(payload) is dict and set(payload) == {'schema', 'brushes'},
          'chain controls must have exactly schema and brushes')
    _need(type(payload['schema']) is str and payload['schema'] in (LEGACY_SCHEMA, SCHEMA),
          'chain controls schema differs')
    brushes = payload['brushes']
    _need(type(brushes) is dict and len(brushes) <= MAX_ACTUALS,
          'brushes must be an object with at most 512 actuals')
    for actual in brushes:
        _identifier(actual, 'controls actual')
    legacy = payload['schema'] == LEGACY_SCHEMA
    return {actual: _checked_row(brushes[actual], legacy=legacy) for actual in sorted(brushes)}


def _check_kind(actual, row, eligible):
    _need(not row['boundary_enabled'] or 'boundary' in eligible[actual],
          'boundary enabled without a boundary route for actual ' + actual)
    _need(not row['intersection_enabled'] or 'intersection' in eligible[actual],
          'intersection enabled without an intersection route for actual ' + actual)
    _need(row['intersection_max_gap_px'] == 0 or 'intersection' in eligible[actual],
          'intersection gap without an intersection route for actual ' + actual)


def seed_controls(bindings):
    """Seed all actuals disabled from their original native chain scalars only.

    Only native CLOCKWISE is mapped by this slice. Split/direction are converted
    from radians with math.degrees without rounding. Link/min lengths retain
    their native pixel values; runtime scale belongs to the caller.
    """
    actuals, unused_eligible = _binding_index(bindings)
    brushes = {}
    for actual in sorted(actuals):
        parameters = actuals[actual].get('chain_parameters_native')
        _need(type(parameters) is dict and set(parameters) == _NATIVE_FIELDS,
              'actual native chain scalars must contain exactly five fields')
        orientation = parameters['loop_direction_type']
        _need(type(orientation) is str and orientation == 'CLOCKWISE',
              'unsupported native loop_direction_type; only CLOCKWISE is mapped')
        split = _finite_number(parameters['line_split_angle'], 'native line_split_angle')
        direction = _finite_number(parameters['line_direction'], 'native line_direction')
        gap = _length(parameters['line_link_length'], 'native line_link_length')
        minimum = _length(parameters['min_line_length'], 'native min_line_length')
        # math.degrees may overflow to infinity for a finite huge float;
        # checked_policy then rejects that invalid conversion clearly.
        brushes[actual] = _checked_row(dict(boundary_enabled=False,
            intersection_enabled=False, max_gap_px=gap, intersection_max_gap_px=0,
            policy=dict(split_angle_degrees=math.degrees(split),
                        line_direction_degrees=math.degrees(direction),
                        closed_orientation='clockwise', seam_tokens={},
                        min_length_px=minimum, link_length_px=0)))
    return dict(schema=SCHEMA, brushes=brushes)


def normalize_controls(bindings, payload):
    """Validate one complete current-actual author state; no partial updates."""
    actuals, eligible = _binding_index(bindings)
    brushes = _checked_payload(payload)
    _need(set(brushes) == set(actuals), 'controls actual set must exactly match current bindings')
    for actual, row in brushes.items():
        _check_kind(actual, row, eligible)
    return dict(schema=SCHEMA, brushes=brushes)


def merge_controls(bindings, previous=None, explicit=None):
    """Seed -> validate/migrate previous intersection -> explicit full override.

    Stale previous actuals are validated then dropped. New actuals stay off.
    Existing enabled flags that lose their route kind cause an explicit error,
    including when explicit is supplied; explicit never conceals bad previous
    state. To intentionally replace incompatible previous state, pass None.
    """
    result = seed_controls(bindings)
    actuals, eligible = _binding_index(bindings)
    if previous is not None:
        old = _checked_payload(previous)
        for actual in sorted(set(old) & set(actuals)):
            _check_kind(actual, old[actual], eligible)
            result['brushes'][actual] = old[actual]
    if explicit is not None:
        return normalize_controls(bindings, explicit)
    return result


__all__ = ['SCHEMA', 'seed_controls', 'normalize_controls', 'merge_controls']
