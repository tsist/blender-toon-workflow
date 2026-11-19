# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit candidate: explicit screen-unit source linkage and v1 phase planning.

Does not evaluate curves, generate geometry, weld, or claim native equivalence.
"""
import math
from .linker import build_chains
from ._support.chain_policy import plan_style_chains

NATIVE_KEYS = {'line_link_length', 'line_split_angle', 'line_direction',
               'min_line_length', 'loop_direction_type'}


def _finite_number(value, name):
    if type(value) not in (float, int):
        raise ValueError('finite numeric value required: ' + name)
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError('number exceeds binary64 range: ' + name) from exc
    if not math.isfinite(number):
        raise ValueError('finite numeric value required: ' + name)
    return number


def native_policy(parameters, *, pixel_scale):
    """Translate preserved radians and explicit caller-supplied pixel scale.

    Scaling is a declared autonomous contract: native pixel lengths multiplied
    by pixel_scale. It is not an inferred Pencil resolution/size-mode rule.
    Only the recovered CLOCKWISE value is supported by this audit slice.
    """
    if type(parameters) is not dict or set(parameters) != NATIVE_KEYS:
        raise ValueError('exact preserved chain parameter fields required')
    pixel_scale = _finite_number(pixel_scale, 'pixel_scale')
    if pixel_scale <= 0:
        raise ValueError('positive finite explicit pixel_scale required')
    numbers = {key: _finite_number(parameters[key], key)
               for key in NATIVE_KEYS - {'loop_direction_type'}}
    link = numbers['line_link_length'] * pixel_scale
    minimum = numbers['min_line_length'] * pixel_scale
    if not math.isfinite(link) or not math.isfinite(minimum) or link < 0 or minimum < 0:
        raise ValueError('nonnegative finite screen link/min lengths required')
    if parameters['loop_direction_type'] != 'CLOCKWISE':
        raise ValueError('this audit slice only maps recovered CLOCKWISE')
    policy = dict(split_angle_degrees=math.degrees(numbers['line_split_angle']),
                  line_direction_degrees=math.degrees(numbers['line_direction']),
                  closed_orientation='clockwise', seam_tokens={},
                  min_length_px=minimum, link_length_px=0)
    return dict(max_gap_px=link, policy=policy, pixel_scale=float(pixel_scale),
                qualification='autonomous explicit pixel scaling; not native equivalence')


def plan_sources(sources, *, policy, max_gap_px=0, candidate_budget=1_000_000,
                 checkpoint=lambda: None, include_arc=False,seam_anchor=None):
    """Return original-order phase/suppression plus full independent evidence.

    Linker owns nonzero gap pairing. Policy v1 continues receiving and enforcing
    link_length_px=0. Gap distances never contribute to source chord lengths.
    Suppressed sources remain represented; caller excludes only their drawing.
    """
    if type(include_arc) is not bool:
        raise ValueError('include_arc must be a boolean')
    linked = build_chains(sources, max_gap_px=max_gap_px,
                          candidate_budget=candidate_budget, checkpoint=checkpoint)
    if seam_anchor is not None:
        import copy
        policy=copy.deepcopy(policy)
        for chain in linked['chains']:
            if chain['closed'] and any(step['source_id']==seam_anchor['source_id'] for step in chain['ordered_sources']):
                policy['seam_tokens'][chain['chain_id']]=dict(seam_anchor)
    planned = plan_style_chains(linked['chains'], linked['xy_by_source'], policy,
                                checkpoint=checkpoint)
    spans = {row['source_id']: row for row in planned['source_spans']}
    suppressed = set(planned['suppressed_sources'])
    if include_arc:
        from .dash_splitter import arc_spans_from_policy
        arc_by_source, unused_totals = arc_spans_from_policy(planned, checkpoint=checkpoint)
        arcs = []
    phases, active = [], []
    for index, source in enumerate(sources):
        if index % 256 == 0:
            checkpoint()
        row = spans[source['source_id']]
        phases.append(list(row['chain_parameters']))
        active.append(source['source_id'] not in suppressed)
        if include_arc:arcs.append(arc_by_source[source['source_id']])
    checkpoint()
    result = dict(schema='toon.viewport-source-phase-adapter.v1',
                source_ids=[s['source_id'] for s in sources],
                parameters=phases, active=active, linker=linked, policy_plan=planned,
                qualification=dict(native_equivalent=False, weld_executed=False,
                    geometry_generated=False, source_order_preserved=True,
                    current_authored_curves_overwritten=False,
                    clip_restarts_phase=False, link_length_implemented_by='independent linker'))
    if include_arc:
        import numpy as np
        result['arc_px'] = np.asarray(arcs, np.float64).reshape(-1, 2)
    return result
