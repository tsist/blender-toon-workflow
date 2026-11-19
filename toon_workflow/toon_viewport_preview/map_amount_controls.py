# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent exact size-map author values; autonomous preview, not native transfer."""
import math

SCHEMA = 'toon.viewport-size-map-amounts.v1'
MAX_ACTUALS = 512


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _identifier(value):
    _need(type(value) is str and 0 < len(value) <= 256, 'amount actual identity must be a bounded string')
    value.encode('utf-8')
    return value


def _amount(value):
    _need(type(value) in (int, float), 'size map amount must be a finite real scalar; bool is not numeric')
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError) as exc:
        raise ValueError('size map amount is outside binary64 range') from exc
    _need(finite and 0 <= value <= 1, 'size map amount must be in 0..1')
    return value


def _actuals(bindings):
    _need(type(bindings) is dict and bindings.get('schema') == 'toon.viewport-native-bindings.v1', 'recovered actual bindings required')
    actuals = bindings.get('actuals')
    _need(type(actuals) is dict and 0 < len(actuals) <= MAX_ACTUALS, 'bounded recovered actual registry required')
    for actual, row in actuals.items():
        _identifier(actual)
        _need(type(row) is dict and row.get('actual_brush_id') == actual, 'amount actual identity differs from its registry key')
    return actuals


def checked_payload(payload):
    _need(type(payload) is dict and set(payload) == {'schema', 'amounts'}, 'size map author table has unexpected fields')
    _need(payload['schema'] == SCHEMA and type(payload['schema']) is str, 'size map author schema differs')
    values = payload['amounts']
    _need(type(values) is dict and len(values) <= MAX_ACTUALS, 'bounded amount table required')
    for actual in values:_identifier(actual)
    return {actual: _amount(values[actual]) for actual in sorted(values)}


def defaults(bindings):
    """A scene without an author table keeps the previous complete map effect."""
    return dict(schema=SCHEMA, amounts={actual: 1 for actual in sorted(_actuals(bindings))})


def normalize(bindings, payload):
    actuals = _actuals(bindings)
    values = checked_payload(payload)
    _need(set(values) == set(actuals), 'amount actual set must exactly match current recovered identities')
    return dict(schema=SCHEMA, amounts=values)


def _native_amount(row, document):
    if row.get('origin') == 'authored':
        return _amount(row['initial_amount'])
    full = document['inputs']['full_preset']
    brushes = full['brushes']
    key = row['source_brush_key']
    _need(key in brushes and type(brushes[key].get('properties')) is dict, 'traced source brush properties missing')
    props = brushes[key]['properties']
    _need('size_map_amount' in props, 'original size_map_amount missing')
    return _amount(props['size_map_amount'])


def native_value(bindings, document, actual):
    """Restore one traced actual without validating unrelated original values."""
    actuals = _actuals(bindings)
    _need(actual in actuals, 'unknown amount actual')
    return _native_amount(actuals[actual], document)


def seed_native(bindings, document):
    """Read only traced original brush properties; do not infer a transfer law."""
    result = {actual: _native_amount(row, document) for actual, row in _actuals(bindings).items()}
    return normalize(bindings, dict(schema=SCHEMA, amounts=result))


def merge(bindings, previous, document):
    """Author action: retain exact old identities, seed newly traced originals."""
    actuals = _actuals(bindings)
    old = {} if previous is None else checked_payload(previous)
    values = {actual: old[actual] if actual in old else _native_amount(actuals[actual], document)
              for actual in sorted(actuals)}
    return dict(schema=SCHEMA, amounts=values)


def factor(amounts, actual):
    # This is a lookup in a validated owned runtime snapshot, not identity
    # inference. Untraced old sources have no editable row and retain legacy 1.
    return 1 if amounts is None else amounts.get(actual, 1)


def mix(factors, amount):
    """Amount1 returns the original array, avoiding any rounding/reallocation."""
    if amount == 1:
        return factors
    return 1 + amount * (factors - 1)


def consumer_brush(brush, amount):
    """a0 follows existing map-off sampling without changing the author object."""
    return dict(brush, width_map=None) if amount == 0 else brush
