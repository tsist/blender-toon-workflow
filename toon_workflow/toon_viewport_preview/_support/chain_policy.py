# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic style-chain policy over caller-declared screen geometry.

The module is intentionally self-contained: it performs no file, bpy, NumPy,
raster, phase, visibility-clip, or native-operator work.  Source IDs, source
orientation semantics and ``seam_node`` manifest values are preserved.  The
only geometry authority is ``xy_by_source``, whose two points are the original
forward endpoints of each source.

Policy order is fixed: split -> orientation/seam -> minimum length.  The split
rule copies the established screen-chord, strict-greater semantics.  Direction
and area are autonomous screen-space rules; neither is presented as a native
Pencil/Paint algorithm.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Set
from fractions import Fraction

POLICY_SCHEMA = "toon.autonomous-style-chain-policy.v1"
PLAN_SCHEMA = "toon.autonomous-style-chain-policy-plan.v1"
MAX_SOURCES = 100_000
MAX_CHAINS = 100_000
MAX_IDENTIFIER_LENGTH = 256

POLICY_KEYS = (
    "split_angle_degrees",
    "line_direction_degrees",
    "closed_orientation",
    "seam_tokens",
    "min_length_px",
    "link_length_px",
)

DEFAULT_POLICY = {
    "split_angle_degrees": None,
    "line_direction_degrees": None,
    "closed_orientation": "retained",
    "seam_tokens": {},
    "min_length_px": 0,
    "link_length_px": 0,
}

_CHAIN_KEYS = frozenset(("chain_id", "closed", "ordered_sources", "seam_node"))
_STEP_KEYS = frozenset(("source_id", "orientation"))
_TOKEN_KEYS = frozenset(("source_id", "endpoint"))
_ORIENTATIONS = frozenset(("forward", "reverse"))
_CLOSED_ORIENTATIONS = frozenset(("retained", "clockwise", "counterclockwise"))
_SPLIT_POLICY = "screen_chord_turn_strict_greater.v1"
_AUTONOMOUS_POLICY = "toon.autonomous-style-chain-policy.v1"


class StyleChainPolicyError(ValueError):
    """Typed refusal for a policy, input, or an impossible token mapping."""

    def __init__(self, code, path, detail=None):
        self.code = code
        self.path = path
        self.detail = detail
        message = code + " at " + path
        if detail:
            message += ": " + detail
        super().__init__(message)


class UnsupportedStyleChainPolicy(StyleChainPolicyError):
    """A schema-valid policy asks for functionality not present in v1."""


def _fail(code, path, detail=None):
    raise StyleChainPolicyError(code, path, detail)


def _text(value, path):
    if type(value) is not str or not 1 <= len(value) <= MAX_IDENTIFIER_LENGTH:
        _fail("IDENTIFIER", path)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StyleChainPolicyError("UNICODE_SCALAR", path) from exc
    return value


def _policy_number(value, path, minimum, maximum):
    if type(value) not in (int, float):
        _fail("NUMBER_TYPE", path)
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise StyleChainPolicyError("NONFINITE_NUMBER", path) from exc
    if not math.isfinite(numeric):
        _fail("NONFINITE_NUMBER", path)
    if not minimum <= numeric <= maximum:
        _fail("NUMBER_RANGE", path)
    return value


def _finite_policy_number(value, path):
    if type(value) not in (int, float):
        _fail("NUMBER_TYPE", path)
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise StyleChainPolicyError("NONFINITE_NUMBER", path) from exc
    if not math.isfinite(numeric):
        _fail("NONFINITE_NUMBER", path)
    return value


def _periodic_checkpoint(index, checkpoint):
    if index % 256 == 0:
        checkpoint()


def _copy_steps(steps, checkpoint):
    result = []
    for index, step in enumerate(steps):
        _periodic_checkpoint(index, checkpoint)
        result.append(dict(step))
    return result


def _source_ids(rows, checkpoint):
    result = []
    for index, row in enumerate(rows):
        _periodic_checkpoint(index, checkpoint)
        result.append(row["source_id"])
    return result


def _copy_seam_tokens(tokens, checkpoint):
    result = {}
    for index, (key, value) in enumerate(tokens.items()):
        _periodic_checkpoint(index, checkpoint)
        result[key] = dict(value)
    return result


def checked_policy(policy):
    """Return a detached policy-v1 dictionary or raise a typed refusal.

    ``None`` selects ``DEFAULT_POLICY``.  A supplied mapping must contain exactly
    the six v1 keys: no omitted or unknown keys are accepted.
    """
    return _checked_policy(policy, lambda: None)


def _checked_policy(policy, checkpoint):
    if policy is None:
        raw = dict(DEFAULT_POLICY)
    else:
        if type(policy) is not dict:
            _fail("POLICY_TYPE", "$.policy")
        keys = set(policy)
        if keys != set(POLICY_KEYS):
            unknown = sorted(str(key) for key in keys - set(POLICY_KEYS))
            missing = sorted(set(POLICY_KEYS) - keys)
            detail = "unknown=" + repr(unknown) + ", missing=" + repr(missing)
            _fail("POLICY_FIELDS", "$.policy", detail)
        raw = policy

    split = raw["split_angle_degrees"]
    if split is not None:
        split = _policy_number(split, "$.policy.split_angle_degrees", 0, 180)

    line = raw["line_direction_degrees"]
    if line is not None:
        line = _policy_number(line, "$.policy.line_direction_degrees", -360, 360)

    orientation = raw["closed_orientation"]
    if type(orientation) is not str or orientation not in _CLOSED_ORIENTATIONS:
        _fail("CLOSED_ORIENTATION", "$.policy.closed_orientation")

    tokens = raw["seam_tokens"]
    if type(tokens) is not dict:
        _fail("SEAM_TOKENS_TYPE", "$.policy.seam_tokens")
    if len(tokens) > MAX_CHAINS:
        _fail("SEAM_TOKENS_LIMIT", "$.policy.seam_tokens")
    normalized_tokens = {}
    for index, (parent_id, token) in enumerate(tokens.items()):
        _periodic_checkpoint(index, checkpoint)
        parent_path = "$.policy.seam_tokens[" + str(parent_id) + "]"
        _text(parent_id, parent_path + "<key>")
        if parent_id in normalized_tokens:
            _fail("DUPLICATE_SEAM_TOKEN", parent_path)
        if type(token) is not dict or set(token) != _TOKEN_KEYS:
            _fail("SEAM_TOKEN_FIELDS", parent_path)
        source_id = _text(token["source_id"], parent_path + ".source_id")
        endpoint = token["endpoint"]
        if type(endpoint) is not int or endpoint not in (0, 1):
            _fail("SEAM_TOKEN_ENDPOINT", parent_path + ".endpoint")
        normalized_tokens[parent_id] = {
            "source_id": source_id,
            "endpoint": endpoint,
        }

    minimum = _policy_number(
        raw["min_length_px"], "$.policy.min_length_px", 0, 65_536
    )

    link = _finite_policy_number(raw["link_length_px"], "$.policy.link_length_px")
    if link != 0:
        raise UnsupportedStyleChainPolicy(
            "UNSUPPORTED_LINK_LENGTH",
            "$.policy.link_length_px",
            "v1 defines only link_length_px=0; no link algorithm is implied",
        )

    ordered_tokens = {}
    for index, key in enumerate(sorted(normalized_tokens)):
        _periodic_checkpoint(index, checkpoint)
        ordered_tokens[key] = dict(normalized_tokens[key])
    return {
        "split_angle_degrees": split,
        "line_direction_degrees": line,
        "closed_orientation": orientation,
        "seam_tokens": ordered_tokens,
        "min_length_px": minimum,
        "link_length_px": 0,
    }


def _coordinate(value, path):
    # NumPy scalars and other real-like values are accepted; text and bools are
    # not geometry and must never be coerced implicitly.
    if isinstance(value, (bool, str, bytes, bytearray)):
        _fail("COORDINATE_TYPE", path)
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise StyleChainPolicyError("COORDINATE_TYPE", path) from exc
    if not math.isfinite(numeric):
        _fail("NONFINITE_COORDINATE", path)
    return numeric


def _point(value, path):
    if isinstance(value, (str, bytes, bytearray, Mapping, Set)) or not hasattr(value, "__len__"):
        _fail("POINT_SHAPE", path)
    try:
        if len(value) != 2:
            _fail("POINT_SHAPE", path)
        first, second = value[0], value[1]
    except (TypeError, KeyError, IndexError) as exc:
        raise StyleChainPolicyError("POINT_SHAPE", path) from exc
    return (
        _coordinate(first, path + "[0]"),
        _coordinate(second, path + "[1]"),
    )


def _source_xy(value, path):
    if isinstance(value, (str, bytes, bytearray, Mapping, Set)) or not hasattr(value, "__len__"):
        _fail("SOURCE_XY_SHAPE", path)
    try:
        if len(value) != 2:
            _fail("SOURCE_XY_SHAPE", path)
        first, second = value[0], value[1]
    except (TypeError, KeyError, IndexError) as exc:
        raise StyleChainPolicyError("SOURCE_XY_SHAPE", path) from exc
    return (
        _point(first, path + "[0]"),
        _point(second, path + "[1]"),
    )


def _validate_chains(chains, xy_by_source, checkpoint):
    if type(chains) is not list:
        _fail("CHAINS_TYPE", "$.chains")
    if len(chains) > MAX_CHAINS:
        _fail("CHAIN_LIMIT", "$.chains")
    if type(xy_by_source) is not dict:
        _fail("XY_MAP_TYPE", "$.xy_by_source")
    if len(xy_by_source) > MAX_SOURCES:
        _fail("SOURCE_LIMIT", "$.xy_by_source")

    normalized = []
    chain_by_id = {}
    source_owner = {}
    total_sources = 0

    for chain_index, raw_chain in enumerate(chains):
        _periodic_checkpoint(chain_index, checkpoint)
        chain_path = "$.chains[" + str(chain_index) + "]"
        if type(raw_chain) is not dict or set(raw_chain) != _CHAIN_KEYS:
            _fail("CHAIN_FIELDS", chain_path)
        chain_id = _text(raw_chain["chain_id"], chain_path + ".chain_id")
        if chain_id in chain_by_id:
            _fail("DUPLICATE_CHAIN_ID", chain_path + ".chain_id")
        closed = raw_chain["closed"]
        if type(closed) is not bool:
            _fail("CLOSED_BOOL", chain_path + ".closed")
        seam_node = raw_chain["seam_node"]
        if seam_node is not None:
            seam_node = _text(seam_node, chain_path + ".seam_node")
        raw_steps = raw_chain["ordered_sources"]
        if type(raw_steps) is not list or not raw_steps:
            _fail("ORDERED_SOURCES", chain_path + ".ordered_sources")
        chain_sources = []
        for step_index, raw_step in enumerate(raw_steps):
            _periodic_checkpoint(step_index, checkpoint)
            step_path = (
                chain_path + ".ordered_sources[" + str(step_index) + "]"
            )
            if type(raw_step) is not dict or set(raw_step) != _STEP_KEYS:
                _fail("SOURCE_STEP_FIELDS", step_path)
            source_id = _text(raw_step["source_id"], step_path + ".source_id")
            orientation = raw_step["orientation"]
            if type(orientation) is not str or orientation not in _ORIENTATIONS:
                _fail("SOURCE_ORIENTATION", step_path + ".orientation")
            if source_id in source_owner:
                _fail("DUPLICATE_SOURCE_ID", step_path + ".source_id")
            source_owner[source_id] = (chain_id, step_index)
            chain_sources.append(source_id)
            total_sources += 1
            if total_sources > MAX_SOURCES:
                _fail("SOURCE_LIMIT", "$.chains")
        chain = {
            "chain_id": chain_id,
            "closed": closed,
            "ordered_sources": _copy_steps(raw_steps, checkpoint),
            "seam_node": seam_node,
        }
        normalized.append(chain)
        chain_by_id[chain_id] = {
            "chain": chain,
            "source_ids": set(chain_sources),
        }

    for index, key in enumerate(xy_by_source):
        _periodic_checkpoint(index, checkpoint)
        _text(key, "$.xy_by_source<key>")
    known_sources = set(source_owner)
    xy_sources = set(xy_by_source)
    if xy_sources - known_sources:
        extra = sorted(xy_sources - known_sources)[0]
        _fail(
            "UNKNOWN_SOURCE_GEOMETRY",
            "$.xy_by_source[" + extra + "]",
            "geometry is supplied for a source absent from all chains",
        )
    if known_sources - xy_sources:
        missing = sorted(known_sources - xy_sources)[0]
        _fail(
            "MISSING_SOURCE_GEOMETRY",
            "$.xy_by_source[" + missing + "]",
        )
    normalized_xy = {}
    for chain in normalized:
        for index, step in enumerate(chain["ordered_sources"]):
            _periodic_checkpoint(index, checkpoint)
            source_id = step["source_id"]
            normalized_xy[source_id] = _source_xy(
                xy_by_source[source_id], "$.xy_by_source[" + source_id + "]"
            )

    return (
        normalized,
        normalized_xy,
        chain_by_id,
        source_owner,
    )


def _canonical_digest(value, *, ensure_ascii=False, checkpoint=lambda: None):
    encoder = json.JSONEncoder(
        ensure_ascii=ensure_ascii,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256()
    for index, chunk in enumerate(encoder.iterencode(value)):
        _periodic_checkpoint(index, checkpoint)
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _turn(left, right):
    """Copy of the established screen-chord turn rule; None means collapsed."""
    a = [float(left[1][i]) - float(left[0][i]) for i in range(2)]
    b = [float(right[1][i]) - float(right[0][i]) for i in range(2)]
    na, nb = math.hypot(*a), math.hypot(*b)
    if not all(math.isfinite(value) for value in (*a, *b, na, nb)):
        _fail("NONFINITE_SCREEN_CHORD", "$.xy_by_source")
    if na == 0 or nb == 0:
        return None
    a = [value / na for value in a]
    b = [value / nb for value in b]
    return math.degrees(
        math.atan2(
            abs(a[0] * b[1] - a[1] * b[0]),
            a[0] * b[0] + a[1] * b[1],
        )
    )


def _split_chain(chain, xy_by_source, angle, checkpoint):
    """Reuse the v2 style-domain split and its stable split-ID construction."""
    original = {
        "chain_id": chain["chain_id"],
        "closed": chain["closed"],
        "ordered_sources": _copy_steps(chain["ordered_sources"], checkpoint),
        "seam_node": chain["seam_node"],
    }
    steps = chain["ordered_sources"]
    evidence = {
        "parent_chain_id": chain["chain_id"],
        "split_angle_degrees": angle,
        "policy": _SPLIT_POLICY,
        "cuts": [],
        "applied": False,
        "closed_parent_cut_to_open": False,
        "style_chain_ids": [chain["chain_id"]],
        "geometry_manifest_changed": False,
    }
    if angle is None:
        return [original], evidence

    oriented = []
    for index, step in enumerate(steps):
        _periodic_checkpoint(index, checkpoint)
        xy = xy_by_source[step["source_id"]]
        oriented.append(xy if step["orientation"] == "forward" else xy[::-1])

    count = len(steps)
    cuts = []
    for right in range(0 if chain["closed"] else 1, count):
        _periodic_checkpoint(right, checkpoint)
        left = (right - 1) % count
        degrees = _turn(oriented[left], oriented[right])
        cut = angle is not None and (
            degrees is None or angle == 0 or degrees > angle
        )
        cuts.append(
            {
                "left_source": steps[left]["source_id"],
                "right_source": steps[right]["source_id"],
                "before_ordinal": right,
                "turn_degrees": degrees,
                "cut": cut,
                "reason": (
                    "collapsed_screen_chord"
                    if degrees is None
                    else "split_zero_each_source"
                    if angle == 0
                    else "turn_exceeds_threshold"
                    if cut
                    else "retained_topology_join"
                ),
            }
        )
    evidence["cuts"] = cuts

    if not any(row["cut"] for row in cuts):
        return [original], evidence

    cut_positions = set()
    for index, row in enumerate(cuts):
        _periodic_checkpoint(index, checkpoint)
        if row["cut"]:
            cut_positions.add(row["before_ordinal"])
    begin = min(cut_positions) if chain["closed"] else 0
    groups = []
    current = []
    for index in range(count):
        _periodic_checkpoint(index, checkpoint)
        ordinal = (begin + index) % count
        if current and ordinal in cut_positions:
            groups.append(current)
            current = []
        current.append(dict(steps[ordinal]))
    groups.append(current)

    result = []
    for index, group in enumerate(groups):
        _periodic_checkpoint(index, checkpoint)
        payload = {
            "parent_chain": chain["chain_id"],
            "split_angle_degrees": angle,
            "ordered_sources": group,
            "policy": _SPLIT_POLICY,
        }
        # This specific legacy identity must retain boundary_phase's default
        # ASCII escaping. New autonomous policy IDs use UTF-8 canonical JSON.
        digest = _canonical_digest(payload, ensure_ascii=True, checkpoint=checkpoint)
        result.append(
            {
                "chain_id": "style-" + digest,
                "closed": False,
                "ordered_sources": group,
                "seam_node": None,
            }
        )

    evidence["applied"] = True
    evidence["closed_parent_cut_to_open"] = bool(chain["closed"])
    evidence["style_chain_ids"] = []
    for index, row in enumerate(result):
        _periodic_checkpoint(index, checkpoint)
        evidence["style_chain_ids"].append(row["chain_id"])
    return result, evidence


def _traversal_points(step, xy_by_source):
    xy = xy_by_source[step["source_id"]]
    if step["orientation"] == "forward":
        return xy[0], xy[1]
    return xy[1], xy[0]


def _signed_area(chain, xy_by_source, checkpoint):
    """Exact sign/area of admitted binary64 endpoints, not exact projection.

    Local-origin differences avoid unnecessarily large products. Fractions
    preserve all bits even for translated subpixel loops or extreme offsets.
    The endpoint-list polygon includes the same declared joins as v1 did.
    """
    first = _traversal_points(chain["ordered_sources"][0], xy_by_source)[0]
    origin = tuple(Fraction.from_float(value) for value in first)
    previous = (Fraction(0), Fraction(0))
    twice_area = Fraction(0)
    for index, step in enumerate(chain["ordered_sources"]):
        for point_index, point in enumerate(_traversal_points(step, xy_by_source)):
            _periodic_checkpoint(index * 2 + point_index, checkpoint)
            current = tuple(Fraction.from_float(value) - base for value, base in zip(point, origin))
            twice_area += previous[0] * current[1] - previous[1] * current[0]
            previous = current
    # The final join to the local origin contributes exactly zero.
    return twice_area / 2


def _finite_fraction_float(value):
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _reverse_steps(steps, checkpoint):
    result = []
    for index, step in enumerate(reversed(steps)):
        _periodic_checkpoint(index, checkpoint)
        result.append({
            "source_id": step["source_id"],
            "orientation": (
                "reverse" if step["orientation"] == "forward" else "forward"
            ),
        })
    return result


def _line_chord(chain, xy_by_source):
    first = chain["ordered_sources"][0]
    last = chain["ordered_sources"][-1]
    start = _traversal_points(first, xy_by_source)[0]
    end = _traversal_points(last, xy_by_source)[1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if not math.isfinite(dx) or not math.isfinite(dy):
        _fail("NONFINITE_LINE_CHORD", "$.xy_by_source")
    return start, end, dx, dy


def _apply_closed_orientation(chain, xy_by_source, target, checkpoint):
    if target == "retained":
        return False, {
            "policy": target, "signed_area_px2": None, "signed_area_exact": None,
            "observed": "not_evaluated", "action": "retained",
            "reason": "closed_orientation_retained",
            "signed_area_after_reversal_px2": None,
            "area_evaluation": "not_required_retained_policy",
            "seam_rule": "preserve_original_physical_join",
        }
    before = _signed_area(chain, xy_by_source, checkpoint)
    if before > 0:
        observed = "counterclockwise"
    elif before < 0:
        observed = "clockwise"
    else:
        observed = "zero"
    action = "retained"
    reason = "closed_orientation_retained"
    reversed_chain = False
    if target != "retained" and observed != "zero" and observed != target:
        chain["ordered_sources"] = _reverse_steps(chain["ordered_sources"], checkpoint)
        action = "reversed"
        reversed_chain = True
        reason = "closed_orientation_target_required_reversal"
    elif observed == "zero":
        reason = "zero_signed_area_retained"
    elif target == "retained":
        reason = "closed_orientation_retained"
    else:
        reason = "closed_orientation_already_target"
    after = -before if reversed_chain else before
    return reversed_chain, {
        "policy": target,
        "signed_area_px2": _finite_fraction_float(before),
        "signed_area_exact": [before.numerator, before.denominator],
        "observed": observed,
        "action": action,
        "reason": reason,
        "signed_area_after_reversal_px2": _finite_fraction_float(after),
        "signed_area_after_reversal_exact": [after.numerator, after.denominator],
        "area_evaluation": "exact_binary64_endpoints_local_origin_fraction.v1",
        "seam_rule": "preserve_original_physical_join",
    }


def _apply_line_direction(chain, xy_by_source, degrees, checkpoint):
    if degrees is None:
        return False, {
            "policy_degrees": None,
            "action": "retained",
            "reason": "line_direction_null",
        }
    start, end, dx, dy = _line_chord(chain, xy_by_source)
    if dx == 0 and dy == 0:
        return False, {
            "policy_degrees": degrees,
            "chord_px": [dx, dy],
            "action": "retained",
            "reason": "zero_first_last_chord_retained",
        }
    # Canonical modulo is exact in the admitted binary64 degree value, so a
    # tiny negative angle is not rounded to360 by floating modulo addition.
    normalized = Fraction.from_float(float(degrees)) % 360
    axes = {0: [1.0, 0.0], 90: [0.0, 1.0],
            180: [-1.0, 0.0], 270: [0.0, -1.0]}
    if normalized in axes:
        unit = axes[normalized]
    else:
        representative = normalized if normalized <= 180 else normalized - 360
        radians = math.radians(float(representative))
        unit = [math.cos(radians), math.sin(radians)]
    dot = (Fraction.from_float(dx) * Fraction.from_float(unit[0]) +
           Fraction.from_float(dy) * Fraction.from_float(unit[1]))
    if dot < 0:
        chain["ordered_sources"] = _reverse_steps(chain["ordered_sources"], checkpoint)
        action = "reversed"
        reason = "negative_direction_dot"
    elif dot == 0:
        action = "retained"
        reason = "zero_direction_dot_retained"
    else:
        action = "retained"
        reason = "positive_direction_dot"
    return action == "reversed", {
        "policy_degrees": degrees,
        "first_screen_point": [start[0], start[1]],
        "last_screen_point": [end[0], end[1]],
        "chord_px": [dx, dy],
        "unit_direction": unit,
        "normalized_policy_degrees": float(normalized),
        "normalized_policy_degrees_exact": [normalized.numerator, normalized.denominator],
        "dot": _finite_fraction_float(dot),
        "dot_exact": [dot.numerator, dot.denominator],
        "direction_evaluation": "binary64_trig_components_exact_fraction_dot.v1",
        "action": action,
        "reason": reason,
    }


def _rotate_closed_to_token(chain, token, checkpoint):
    target_source = token["source_id"]
    positions = {}
    for index, step in enumerate(chain["ordered_sources"]):
        _periodic_checkpoint(index, checkpoint)
        positions[step["source_id"]] = index
    if target_source not in positions:
        _fail(
            "SEAM_TOKEN_SOURCE",
            "$.policy.seam_tokens",
            "token source is not in its parent chain",
        )
    index = positions[target_source]
    step = chain["ordered_sources"][index]
    start_endpoint = 0 if step["orientation"] == "forward" else 1
    end_endpoint = 1 - start_endpoint
    if token["endpoint"] == start_endpoint:
        target_index = index
    elif token["endpoint"] == end_endpoint:
        target_index = (index + 1) % len(chain["ordered_sources"])
    else:
        _fail("SEAM_TOKEN_ENDPOINT", "$.policy.seam_tokens")
    if target_index:
        steps = chain["ordered_sources"]
        chain["ordered_sources"] = steps[target_index:] + steps[:target_index]
    return target_index != 0, target_index


def _source_length(xy):
    dx = xy[1][0] - xy[0][0]
    dy = xy[1][1] - xy[0][1]
    if not math.isfinite(dx) or not math.isfinite(dy):
        _fail("NONFINITE_SOURCE_DELTA", "$.xy_by_source")
    # This is the 2D norm used by the established float length accumulation.
    # It is kept in pure Python and deliberately not replaced by an inferred
    # native curve metric.
    try:
        length = math.sqrt(dx * dx + dy * dy)
    except (OverflowError, ValueError) as exc:
        raise StyleChainPolicyError(
            "NONFINITE_SOURCE_LENGTH", "$.xy_by_source"
        ) from exc
    if not math.isfinite(length):
        _fail("NONFINITE_SOURCE_LENGTH", "$.xy_by_source")
    return length


def _measure_chain(chain, xy_by_source, checkpoint):
    rows = []
    total = 0.0
    for index, step in enumerate(chain["ordered_sources"]):
        _periodic_checkpoint(index, checkpoint)
        length = _source_length(xy_by_source[step["source_id"]])
        rows.append((step, length))
        total += length
        if not math.isfinite(total):
            _fail("NONFINITE_CHAIN_LENGTH", "$.xy_by_source")

    prefix = 0.0
    measured = []
    for index, (step, length) in enumerate(rows):
        _periodic_checkpoint(index, checkpoint)
        if total:
            c0 = prefix / total
            c1 = (prefix + length) / total
        else:
            c0, c1 = 0.0, 0.0
        if step["orientation"] == "reverse":
            c0, c1 = c1, c0
        measured.append(
            {
                "source_id": step["source_id"],
                "orientation": step["orientation"],
                "chain_parameters": [c0, c1],
                "length": length,
            }
        )
        prefix += length
    return total, measured


def _policy_fingerprint(policy, checkpoint):
    return _canonical_digest(policy, checkpoint=checkpoint)


def _stable_changed_id(parent_id, base_id, chain, changes, policy_sha256, checkpoint):
    payload = {
        "schema": "toon.autonomous-style-chain-policy-id.v1",
        "parent_chain_id": parent_id,
        "base_chain_id": base_id,
        "autonomous_policy": _AUTONOMOUS_POLICY,
        "policy_sha256": policy_sha256,
        "closed": chain["closed"],
        "ordered_sources": chain["ordered_sources"],
        "changes": list(changes),
    }
    return "style-" + _canonical_digest(payload, checkpoint=checkpoint)


def _finalize_chain(
    chain,
    parent_id,
    base_id,
    changes,
    suppressed,
    chain_length,
    minimum,
    token,
    policy_sha256,
    checkpoint,
):
    """Return a detached chain; add a stable policy ID only when changed."""
    if not changes:
        return {
            "chain_id": chain["chain_id"],
            "closed": chain["closed"],
            "ordered_sources": _copy_steps(chain["ordered_sources"], checkpoint),
            "seam_node": chain["seam_node"],
        }

    result = {
        "chain_id": chain["chain_id"],
        "closed": chain["closed"],
        "ordered_sources": _copy_steps(chain["ordered_sources"], checkpoint),
        "seam_node": chain["seam_node"],
    }
    needs_new_id = any(
        change != "split"
        for change in changes
    )
    if needs_new_id:
        result["chain_id"] = _stable_changed_id(
            parent_id, base_id, result, changes, policy_sha256, checkpoint
        )
    result["parent_chain_id"] = parent_id
    result["policy"] = _AUTONOMOUS_POLICY
    result["changes"] = list(changes)
    if token is not None:
        result["seam_token"] = {
            "source_id": token["source_id"],
            "endpoint": token["endpoint"],
        }
    if suppressed:
        result["suppressed"] = True
        result["chain_length_px"] = chain_length
        result["min_length_px"] = minimum
    return result


def plan_style_chains(
    chains,
    xy_by_source,
    policy,
    *,
    checkpoint=lambda: None,
):
    """Plan autonomous style chains from caller-declared source geometry.

    Returns exactly ``{schema, chains, source_spans, suppressed_sources,
    evidence}``.  All inputs are treated as immutable and all returned containers
    are detached.  ``source_u`` remains the original-source domain ``[0, 1]``
    for every span; no visibility clip is consulted.
    """
    if not callable(checkpoint):
        _fail("CHECKPOINT_NOT_CALLABLE", "$.checkpoint")
    checkpoint()
    normalized_policy = _checked_policy(policy, checkpoint)
    (
        input_chains,
        source_xy,
        chain_by_id,
        source_owner,
    ) = _validate_chains(chains, xy_by_source, checkpoint)

    token_map = normalized_policy["seam_tokens"]
    for index, (parent_id, token) in enumerate(token_map.items()):
        _periodic_checkpoint(index, checkpoint)
        if parent_id not in chain_by_id:
            _fail(
                "UNKNOWN_SEAM_PARENT",
                "$.policy.seam_tokens[" + parent_id + "]",
            )
        parent = chain_by_id[parent_id]["chain"]
        if not parent["closed"]:
            _fail(
                "SEAM_TOKEN_ON_OPEN_CHAIN",
                "$.policy.seam_tokens[" + parent_id + "]",
            )
        if token["source_id"] not in chain_by_id[parent_id]["source_ids"]:
            _fail(
                "SEAM_TOKEN_SOURCE",
                "$.policy.seam_tokens[" + parent_id + "]",
                "token source is not in its parent chain",
            )

    policy_sha256 = _policy_fingerprint(normalized_policy, checkpoint)
    output_chains = []
    output_chain_ids = set()
    source_spans = []
    suppressed_sources = []
    parent_evidence = []
    split_output_count = 0
    reversed_chain_count = 0
    seam_rotation_count = 0

    for input_chain in input_chains:
        checkpoint()
        parent_id = input_chain["chain_id"]
        split_chains, split_evidence = _split_chain(
            input_chain, source_xy, normalized_policy["split_angle_degrees"], checkpoint
        )
        token = token_map.get(parent_id)
        if token is not None and (
            len(split_chains) != 1 or not split_chains[0]["closed"]
        ):
            _fail(
                "SEAM_TOKEN_ON_SPLIT_OPEN_CHAIN",
                "$.policy.seam_tokens[" + parent_id + "]",
                "a closed parent split into open fragments cannot retain a seam token",
            )

        output_evidence = []
        for split_index, split_chain in enumerate(split_chains):
            _periodic_checkpoint(split_index, checkpoint)
            chain = {
                "chain_id": split_chain["chain_id"],
                "closed": split_chain["closed"],
                "ordered_sources": _copy_steps(split_chain["ordered_sources"], checkpoint),
                "seam_node": split_chain["seam_node"],
            }
            base_id = chain["chain_id"]
            changes = ["split"] if split_evidence["applied"] else []
            if split_evidence["applied"]:
                split_output_count += 1

            orientation_evidence = None
            if chain["closed"]:
                reversed_chain, orientation_evidence = _apply_closed_orientation(
                    chain,
                    source_xy,
                    normalized_policy["closed_orientation"],
                    checkpoint,
                )
                if reversed_chain:
                    changes.append("closed_orientation_reversed")
                    reversed_chain_count += 1
                line_evidence = {
                    "policy_degrees": normalized_policy["line_direction_degrees"],
                    "action": "not_applicable",
                    "reason": "closed_chain_uses_closed_orientation",
                }
            else:
                reversed_chain, line_evidence = _apply_line_direction(
                    chain,
                    source_xy,
                    normalized_policy["line_direction_degrees"],
                    checkpoint,
                )
                if reversed_chain:
                    changes.append("line_direction_reversed")
                    reversed_chain_count += 1
                orientation_evidence = {
                    "policy": normalized_policy["closed_orientation"],
                    "action": "not_applicable",
                    "reason": "open_chain_uses_line_direction",
                }

            seam_evidence = {
                "explicit_token": token is not None,
                "action": "not_applicable_open_chain",
                "reason": "open_chain",
            }
            if chain["closed"]:
                if token is None:
                    seam_evidence = {
                        "explicit_token": False,
                        "action": "retained",
                        "reason": (
                            "closed_orientation_reversal_preserved_original_physical_join"
                            if "closed_orientation_reversed" in changes
                            else "original_physical_join_retained"
                        ),
                    }
                else:
                    rotated, target_index = _rotate_closed_to_token(chain, token, checkpoint)
                    if rotated:
                        changes.append("seam_token_rotated")
                        seam_rotation_count += 1
                    seam_evidence = {
                        "explicit_token": True,
                        "token": {
                            "source_id": token["source_id"],
                            "endpoint": token["endpoint"],
                        },
                        "target_ordinal": target_index,
                        "action": "rotated" if rotated else "already_at_join",
                        "reason": (
                            "explicit_token_rotated_to_physical_join"
                            if rotated
                            else "explicit_token_already_at_physical_join"
                        ),
                    }

            chain_length, measured = _measure_chain(chain, source_xy, checkpoint)
            minimum = normalized_policy["min_length_px"]
            suppressed = chain_length < minimum
            if suppressed:
                changes.append("min_length_suppressed")

            final_chain = _finalize_chain(
                chain,
                parent_id,
                base_id,
                changes,
                suppressed,
                chain_length,
                minimum,
                token,
                policy_sha256,
                checkpoint,
            )
            final_id = final_chain["chain_id"]
            if final_id in output_chain_ids:
                _fail("OUTPUT_CHAIN_ID_COLLISION", "$.chains", final_id)
            output_chain_ids.add(final_id)
            output_chains.append(final_chain)

            for index, row in enumerate(measured):
                _periodic_checkpoint(index, checkpoint)
                source_spans.append(
                    {
                        "source_id": row["source_id"],
                        "style_chain_id": final_id,
                        "orientation": row["orientation"],
                        "chain_parameters": list(row["chain_parameters"]),
                        "source_u": [0.0, 1.0],
                        "length": row["length"],
                    }
                )
                if suppressed:
                    suppressed_sources.append(row["source_id"])

            output_evidence.append(
                {
                    "style_chain_id": final_id,
                    "source_ids": _source_ids(measured, checkpoint),
                    "closed": final_chain["closed"],
                    "changes": list(changes),
                    "closed_orientation": orientation_evidence,
                    "line_direction": line_evidence,
                    "seam": seam_evidence,
                    "chain_length_px": chain_length,
                    "min_length_px": minimum,
                    "suppressed": suppressed,
                }
            )

        parent_evidence.append(
            {
                "parent_chain_id": parent_id,
                "split": split_evidence,
                "outputs": output_evidence,
            }
        )

    span_sources = []
    for index, row in enumerate(source_spans):
        _periodic_checkpoint(index, checkpoint)
        span_sources.append(row["source_id"])
    if (
        len(span_sources) != len(source_owner)
        or len(set(span_sources)) != len(span_sources)
        or set(span_sources) != set(source_owner)
    ):
        raise AssertionError("internal source disposition invariant failed")

    active_chains = 0
    for index, row in enumerate(output_chains):
        _periodic_checkpoint(index, checkpoint)
        active_chains += not row.get("suppressed", False)
    evidence = {
        "schema": "toon.autonomous-style-chain-policy-evidence.v1",
        "policy_schema": POLICY_SCHEMA,
        "policy": {
            "split_angle_degrees": normalized_policy["split_angle_degrees"],
            "line_direction_degrees": normalized_policy[
                "line_direction_degrees"
            ],
            "closed_orientation": normalized_policy["closed_orientation"],
            "seam_tokens": _copy_seam_tokens(normalized_policy["seam_tokens"], checkpoint),
            "min_length_px": normalized_policy["min_length_px"],
            "link_length_px": 0,
        },
        "policy_sha256": policy_sha256,
        "parents": parent_evidence,
        "counts": {
            "input_sources": len(source_owner),
            "input_chains": len(input_chains),
            "output_chains": len(output_chains),
            "active_chains": active_chains,
            "suppressed_chains": len(output_chains) - active_chains,
            "split_output_chains": split_output_count,
            "reversed_chains": reversed_chain_count,
            "seam_token_rotations": seam_rotation_count,
            "source_spans": len(source_spans),
            "suppressed_sources": len(suppressed_sources),
        },
        "source_u": {
            "domain": [0.0, 1.0],
            "visibility_clips_restart_source_u": False,
            "per_source_full_domain": True,
        },
        "length_model": "full_source_screen_chord_sum_and_count.v1",
        "qualification": {
            "scope": "autonomous_caller_declared_style_chain_policy",
            "native_equivalence": False,
            "native_seam_inferred": False,
            "source_ids_changed": False,
            "geometry_manifest_changed": False,
            "closed_direction_from_screen_signed_area": True,
            "line_direction_from_first_last_screen_chord": True,
            "link_length_implemented": False,
            "f1_completion_claimed": False,
            "raster_or_visibility_run": False,
        },
    }

    return {
        "schema": PLAN_SCHEMA,
        "chains": output_chains,
        "source_spans": source_spans,
        "suppressed_sources": suppressed_sources,
        "evidence": evidence,
    }
