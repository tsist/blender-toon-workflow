# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact autonomous centerline-pixel dash author state; no native inference."""
import copy
import math

SCHEMA = 'toon.viewport-dash-patterns.v1'
MAX_ACTUALS = 512
_FIELDS = {'mode', 'dash_length_px', 'gap_length_px'}

def _need(condition, message):
    if not condition:raise ValueError(message)

def _identifier(value):
    _need(type(value) is str and 0 < len(value) <= 256, 'dash actual identity must be a bounded string')
    value.encode('utf8')
    return value

def _length(value, minimum, label):
    _need(type(value) in (int,float), label+' must be a finite real scalar; bool is not numeric')
    try:finite=math.isfinite(float(value))
    except (OverflowError,ValueError) as error:raise ValueError(label+' exceeds binary64 range') from error
    _need(finite and minimum <= value <= 65536, label+' is outside its pixel range')
    return value

def _actuals(bindings):
    _need(type(bindings) is dict and bindings.get('schema')=='toon.viewport-native-bindings.v1', 'recovered dash actual bindings required')
    actuals=bindings.get('actuals')
    _need(type(actuals) is dict and 0 < len(actuals) <= MAX_ACTUALS, 'bounded recovered dash actual registry required')
    for actual,row in actuals.items():
        _identifier(actual)
        _need(type(row) is dict and row.get('actual_brush_id')==actual, 'dash actual identity differs from its registry key')
    return actuals

def default_pattern():
    return dict(mode='SOLID',dash_length_px=12,gap_length_px=6)

def checked_payload(payload):
    _need(type(payload) is dict and set(payload)=={'schema','patterns'}, 'dash author table has unexpected fields')
    _need(type(payload['schema']) is str and payload['schema']==SCHEMA, 'dash author schema differs')
    patterns=payload['patterns']
    _need(type(patterns) is dict and len(patterns)<=MAX_ACTUALS, 'bounded dash pattern table required')
    result={}
    for actual in sorted(patterns):
        _identifier(actual);row=patterns[actual]
        _need(type(row) is dict and set(row)==_FIELDS, 'dash pattern fields differ')
        _need(type(row['mode']) is str and row['mode'] in {'SOLID','DASHED'}, 'dash mode must be SOLID or DASHED')
        result[actual]=dict(mode=row['mode'],dash_length_px=_length(row['dash_length_px'],.25,'dash_length_px'),
            gap_length_px=_length(row['gap_length_px'],0,'gap_length_px'))
    return result

def defaults(bindings):
    return dict(schema=SCHEMA,patterns={actual:default_pattern() for actual in sorted(_actuals(bindings))})

def normalize(bindings,payload):
    actuals=_actuals(bindings);patterns=checked_payload(payload)
    _need(set(patterns)==set(actuals), 'dash actual set must exactly match current recovered identities')
    return dict(schema=SCHEMA,patterns=patterns)

def merge(bindings,previous=None):
    result=defaults(bindings)
    old={} if previous is None else checked_payload(previous)
    for actual in set(old)&set(result['patterns']):result['patterns'][actual]=copy.deepcopy(old[actual])
    return result

def needs_metric(patterns):
    """Only validated owned runtime rows enter this lookup."""
    return any(row['mode']=='DASHED' and row['gap_length_px']>0 for row in patterns.values())
