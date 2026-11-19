# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase planning over caller-declared finite-f64 intersection topology.

This is a pure NumPy/standard-library audit adapter.  It canonicalizes by the
exact undirected endpoint-ID pair, extracts degree-two chains from the caller's
declared ``(group, actual, channel, mesh-pair, endpoint-ID)`` topology, delegates
split/direction/minimum decisions to policy v1, and maps phases back to every
original support instance.

It deliberately does **not** call the public ``build_chains`` API and does not
enforce a single-position xy/w gate on shared topology endpoint IDs.  Real
intersection source samples can differ in the last binary64 pixel bit at a
logical join.  Each canonical source keeps one deterministic original sample;
all other samples and source geometry remain untouched and are reported as
diagnostics. No coordinate is averaged, welded, snapped, or used to infer the
declared topology. A positive explicit gap radius adds an independent phase-only
endpoint-distance stage after that topology. The public linker is unchanged.

The module never reads a file, imports Blender, evaluates curves or maps,
generates geometry, writes a bucket, or modifies its inputs.  Canonical source
IDs are scoped to one call/capture; they are not a cross-frame identity claim.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
import struct

import numpy as np
from ._support.chain_policy import plan_style_chains
from .intersection_gap_linker import MAX_GAP_PX, PROFILE as GAP_PROFILE, link_declared_topology


SCHEMA = "toon.viewport-caller-declared-intersection-topology-phase.v1"
TOPOLOGY_SCHEMA = "toon.viewport-intersection-caller-declared-topology.v1"
PROFILE = "caller_declared_finite_f64_intersection_topology.v1"
POLICY_KEYS = frozenset(
    (
        "split_angle_degrees",
        "line_direction_degrees",
        "closed_orientation",
        "seam_tokens",
        "min_length_px",
        "link_length_px",
    )
)
MAX_INSTANCES = 100_000
MAX_CANONICAL_SOURCES = 100_000
MAX_CANDIDATE_CHECKS = 2_000_000
MAX_IDENTIFIER_LENGTH = 256
_SOURCE_FIELDS = frozenset(("source_id", "endpoint_ids", "xy", "w", "domain"))


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _identifier(value, name):
    _need(
        type(value) is str and 0 < len(value) <= MAX_IDENTIFIER_LENGTH,
        name + " must be a nonempty string of at most 256 characters",
    )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(name + " must encode as UTF-8") from exc
    return value


def _integer(value, name, *, minimum=None):
    _need(
        not isinstance(value, (bool, np.bool_))
        and isinstance(value, (int, np.integer)),
        name + " must be an integer",
    )
    result = int(value)
    if minimum is not None:
        _need(result >= minimum, name + " must be at least " + str(minimum))
    _need(result <= np.iinfo(np.int64).max, name + " exceeds int64")
    return result


def _positive_pixels(value, name):
    result = _integer(value, name, minimum=1)
    _need(result <= 2**31 - 1, name + " is unreasonably large")
    return result


def _finite_number(value, name):
    _need(
        not isinstance(value, (bool, np.bool_))
        and isinstance(value, numbers.Real),
        name + " must be a real number",
    )
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(name + " must be a finite real number") from exc
    _need(math.isfinite(result), name + " must be finite")
    return result


def _float_array(value, shape, name):
    try:
        raw = np.asarray(value)
        _need(raw.dtype.kind in "iuf", name + " must use a real numeric dtype")
        with np.errstate(over="raise", invalid="raise"):
            array = np.array(raw, dtype=np.float64, copy=True)
    except (TypeError, ValueError, OverflowError, FloatingPointError) as exc:
        raise ValueError(name + " must be a finite real numeric array") from exc
    if shape is not None:
        _need(array.shape == shape, name + " has an unexpected shape")
    _need(bool(np.isfinite(array).all()), name + " must be finite")
    return array


def _integer_array(value, shape, name, *, minimum=0):
    try:
        raw = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(name + " must be an integer array") from exc
    _need(raw.shape == shape, name + " has an unexpected shape")
    _need(raw.dtype.kind in "iu", name + " must use an integer dtype")
    array = np.array(raw, dtype=np.int64, copy=True)
    _need(bool((array >= minimum).all()), name + " contains an invalid index")
    return array


def _digest(value):
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _domain(group_id, actual_brush_id, channel, mesh_pair):
    mesh_pair = [int(value) for value in mesh_pair]
    value = "intersection:" + json.dumps(
        [group_id, actual_brush_id, channel, mesh_pair],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _need(
        len(value) <= MAX_IDENTIFIER_LENGTH,
        "combined group/actual/channel/mesh domain exceeds 256 characters",
    )
    return value


def _source_id(group_id, actual_brush_id, channel, mesh_pair, endpoint_ids):
    mesh_pair = [int(value) for value in mesh_pair]
    return "intersection-source-" + _digest(
        [
            "toon.viewport-intersection-source-id.v1",
            group_id,
            actual_brush_id,
            channel,
            mesh_pair,
            list(endpoint_ids),
        ]
    )


def _xy_copy(xy):
    return [[float(xy[0][0]), float(xy[0][1])], [float(xy[1][0]), float(xy[1][1])]]


REPRESENTATIVE_RULE = (
    "lexicographic sorted original global triangle pair, then complete original "
    "references, then complete original world coordinates, then canonical-direction "
    "xy/w; numeric ties use complete original world and canonical xy/w IEEE754 "
    "binary64 bit patterns in big-endian unsigned order; only bit-identical ties "
    "use the smallest current input instance index"
)


def _sample_key(index, canonical_xy, canonical_w, world, references, pair):
    # Numeric priority remains unchanged.  Explicit binary64 bits resolve signed
    # zero ties without normalizing, averaging or rewriting any source sample.
    original_floats = tuple(float(value) for value in world[index].reshape(-1))
    canonical_floats = (
        canonical_xy[0][0], canonical_xy[0][1],
        canonical_xy[1][0], canonical_xy[1][1],
        canonical_w[0], canonical_w[1],
    )
    bit_tiebreak = tuple(
        struct.unpack(">Q", struct.pack(">d", value))[0]
        for value in original_floats + canonical_floats
    )
    return (
        tuple(sorted((int(pair[index, 0]), int(pair[index, 1])))),
        tuple(int(value) for value in references[index].reshape(-1)),
        original_floats,
        canonical_xy[0][0],
        canonical_xy[0][1],
        canonical_xy[1][0],
        canonical_xy[1][1],
        canonical_w[0],
        canonical_w[1],
        bit_tiebreak,
    )


def _xy_delta(left, right):
    return max(
        abs(float(left[0][0]) - float(right[0][0])),
        abs(float(left[0][1]) - float(right[0][1])),
        abs(float(left[1][0]) - float(right[1][0])),
        abs(float(left[1][1]) - float(right[1][1])),
    )


def _w_delta(left, right):
    return max(
        abs(float(left[0]) - float(right[0])),
        abs(float(left[1]) - float(right[1])),
    )


def _checked_policy_header(policy, max_gap_px):
    _need(type(policy) is dict, "policy must be a dict with exactly six v1 fields")
    _need(set(policy) == POLICY_KEYS, "policy must contain exactly the six v1 fields")
    _need(
        type(policy["seam_tokens"]) is dict,
        "policy.seam_tokens must be a dict",
    )
    _need(
        _finite_number(policy["link_length_px"], "policy.link_length_px") == 0.0,
        "policy v1 accepts only link_length_px=0; positive link is not implemented",
    )
    _need(
        0.0 <= _finite_number(max_gap_px, "max_gap_px") <= MAX_GAP_PX,
        "independent intersection max_gap_px must be in 0..65536 screen pixels",
    )
    _need(
        _finite_number(policy["min_length_px"], "policy.min_length_px") >= 0,
        "policy.min_length_px must be nonnegative",
    )
    _need(
        type(policy["closed_orientation"]) is str,
        "policy.closed_orientation must be a string",
    )
    for key in ("split_angle_degrees", "line_direction_degrees"):
        if policy[key] is not None:
            _finite_number(policy[key], "policy." + key)
    return policy


def _check_candidate_budget(value):
    _need(
        not isinstance(value, (bool, np.bool_))
        and isinstance(value, (int, np.integer)),
        "candidate_budget must be an integer",
    )
    result = int(value)
    _need(
        0 <= result <= MAX_CANDIDATE_CHECKS,
        "candidate_budget must be in 0..2000000",
    )
    return result


class _Cooperation:
    def __init__(self, checkpoint):
        _need(checkpoint is None or callable(checkpoint), "checkpoint must be callable")
        self._callback = (lambda: None) if checkpoint is None else checkpoint
        self._counts = {}

    def __call__(self, stage, *, force=False):
        count = self._counts.get(stage, 0)
        if force or count % 256 == 0:
            self._callback()
        self._counts[stage] = count + 1


def _project(world, full_mvp, width, height):
    """Return bottom-left full-MVP pixels and positive clip w for both ends."""
    _need(world.shape[1:] == (2, 3), "world must have shape (N,2,3)")
    _need(full_mvp.shape == (4, 4), "full_mvp must have shape (4,4)")
    homogeneous = np.concatenate(
        (world.astype(np.float64, copy=True), np.ones((len(world), 2, 1))), axis=2
    )
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        clip = homogeneous @ full_mvp.T
        w = np.array(clip[:, :, 3], dtype=np.float64, copy=True)
        _need(bool(np.isfinite(clip).all()), "full-MVP clip coordinates must be finite")
        _need(bool((w > 0).all()), "both original full-MVP w values must be finite and positive")
        ndc = clip[:, :, :2] / w[:, :, None]
        # Identical handedness to the boundary/source-Alpha full-source path:
        # bottom-left pixels, no vertical flip.
        xy = (ndc + 1.0) * np.array([width, height], dtype=np.float64) * 0.5
    _need(bool(np.isfinite(xy).all()), "projected source pixels must be finite")
    return xy, w


def _validate_mesh_references(
    pair,
    references,
    mesh_count,
    mesh_triangle_counts,
):
    _need(mesh_count >= 1, "mesh_count must be positive")
    _need(
        bool((references[:, :, 0] < mesh_count).all()),
        "reference mesh index is outside mesh_count",
    )
    # pair contains global triangle indices, not mesh indices and not a mesh
    # pair.  The domain mesh pair comes only from references.
    mesh_pair = np.sort(references[:, :, 0], axis=1)
    if mesh_triangle_counts is not None:
        _need(
            isinstance(mesh_triangle_counts, (list, tuple, np.ndarray)),
            "mesh_triangle_counts must be a sequence",
        )
        counts = [
            _integer(value, "mesh_triangle_counts entry", minimum=0)
            for value in mesh_triangle_counts
        ]
        _need(
            len(counts) == mesh_count,
            "mesh_triangle_counts must have one entry per mesh",
        )
        _need(
            sum(counts) <= np.iinfo(np.int64).max,
            "total mesh triangle count exceeds int64",
        )
        limits = np.asarray(counts, dtype=np.int64)
        _need(
            bool((references[:, :, 1] < limits[references[:, :, 0]]).all()),
            "reference triangle index is outside its mesh",
        )
        prefixes = np.concatenate(
            (
                np.zeros(1, dtype=np.int64),
                np.cumsum(limits[:-1], dtype=np.int64),
            )
        )
        expected_global = prefixes[references[:, :, 0]] + references[:, :, 1]
        _need(
            bool(np.array_equal(pair, expected_global)),
            "pair must equal prefix[mesh] + local triangle index from references",
        )
    return mesh_pair


def _reverse_steps(steps):
    return [
        {
            "source_id": step["source_id"],
            "orientation": "reverse" if step["orientation"] == "forward" else "forward",
        }
        for step in reversed(steps)
    ]


def _signature(steps, rows):
    return tuple(
        (rows[step["source_id"]]["endpoint_pair"], step["orientation"])
        for step in steps
    )


def _rotate_to_smallest(steps, rows):
    index = min(
        range(len(steps)),
        key=lambda i: rows[steps[i]["source_id"]]["endpoint_pair"],
    )
    return steps[index:] + steps[:index]


def _canonical_chain(steps, closed, rows):
    flipped = _reverse_steps(steps)
    if closed:
        first = _rotate_to_smallest(steps, rows)
        second = _rotate_to_smallest(flipped, rows)
        chosen = first if _signature(first, rows) <= _signature(second, rows) else second
        start = chosen[0]
        row = rows[start["source_id"]]
        endpoint = row["endpoint_ids"][0 if start["orientation"] == "forward" else 1]
        return chosen, True, str(endpoint)
    chosen = min((steps, flipped), key=lambda value: _signature(value, rows))
    return chosen, False, None


def _chain_id(domain, steps, closed, seam_node):
    return "chain-" + _digest([domain, steps, closed, seam_node])


def _build_declared_topology(sources, *, candidate_budget, cooperation):
    """Extract degree-two chains from exact endpoint IDs; no coordinate topology."""
    _need(type(sources) is list, "topology sources must be a list")
    _need(len(sources) <= MAX_CANONICAL_SOURCES, "canonical source count limit exceeded")
    rows = {}
    adjacency = {}
    lengths = []
    for index, source in enumerate(sources):
        cooperation("topology_sources")
        _need(
            type(source) is dict and set(source) == _SOURCE_FIELDS,
            "source must have exactly five fields",
        )
        sid = _identifier(source["source_id"], "source_id")
        domain = _identifier(source["domain"], "domain")
        _need(sid not in rows, "duplicate canonical source_id")
        endpoints = source["endpoint_ids"]
        _need(type(endpoints) is list and len(endpoints) == 2, "two endpoint IDs required")
        endpoints = [_integer(value, "endpoint ID", minimum=0) for value in endpoints]
        _need(
            endpoints[0] != endpoints[1],
            "a topology source cannot have identical endpoint IDs",
        )
        _need(
            endpoints[0] < endpoints[1],
            "topology endpoint_ids must be in canonical ascending order",
        )
        xy = source["xy"]
        _need(type(xy) is list and len(xy) == 2, "two projected points required")
        points = []
        for point in xy:
            _need(type(point) is list and len(point) == 2, "2D point required")
            points.append(
                (
                    _finite_number(point[0], "projected x"),
                    _finite_number(point[1], "projected y"),
                )
            )
        w = source["w"]
        _need(type(w) is list and len(w) == 2, "two w values required")
        weights = [_finite_number(value, "projected w") for value in w]
        _need(all(value > 0 for value in weights), "topology w values must be positive")
        length = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
        _need(math.isfinite(length), "canonical source length is outside binary64 range")
        lengths.append(length)
        rows[sid] = {
            "source_id": sid,
            "domain": domain,
            "endpoint_ids": endpoints,
            "endpoint_pair": tuple(endpoints),
            "xy": points,
            "w": weights,
            "length": length,
        }
        for end, endpoint in enumerate(endpoints):
            adjacency.setdefault((domain, endpoint), []).append((sid, end))
    for neighbors in adjacency.values():
        neighbors.sort()
    topology_endpoint_count = len(adjacency)
    branch_endpoint_count = sum(
        len(neighbors) != 2 for neighbors in adjacency.values()
    )
    degenerate_source_count = sum(length == 0 for length in lengths)
    canonical_domain_count = len({row["domain"] for row in rows.values()})
    try:
        total_length = math.fsum(lengths)
    except OverflowError as exc:
        raise ValueError("canonical source length sum is outside binary64 range") from exc
    _need(math.isfinite(total_length), "canonical source length sum is not finite")

    visited = set()
    topology = []

    def follow(start_key, first_sid, entering):
        sid = first_sid
        steps = []
        while True:
            cooperation("topology_follow")
            _need(sid not in visited, "internal topology traversal repeated a source")
            visited.add(sid)
            row = rows[sid]
            orientation = "forward" if entering == 0 else "reverse"
            steps.append({"source_id": sid, "orientation": orientation})
            exit_key = (row["domain"], row["endpoint_ids"][1 - entering])
            if len(adjacency[exit_key]) != 2:
                break
            candidates = [half for half in adjacency[exit_key] if half[0] not in visited]
            if not candidates:
                break
            _need(len(candidates) == 1, "invalid degree-two topology")
            sid, entering = candidates[0]
        closed = exit_key == start_key and len(adjacency[start_key]) == 2
        topology.append((start_key[0], steps, closed))

    vertices = sorted(adjacency)
    for vertex in vertices:
        cooperation("topology_vertices")
        if len(adjacency[vertex]) != 2:
            for sid, end in adjacency[vertex]:
                if sid not in visited:
                    follow(vertex, sid, end)
    for vertex in vertices:
        cooperation("topology_loops")
        if len(adjacency[vertex]) != 2:
            continue
        for sid, end in adjacency[vertex]:
            if sid not in visited:
                follow(vertex, sid, end)
                break
    _need(len(visited) == len(rows), "topology lost a canonical source")

    chains = []
    chain_domains = {}
    seen_sources = set()
    for domain, steps, closed in topology:
        cooperation("topology_chain_canonicalization")
        ordered, is_closed, seam_node = _canonical_chain(steps, closed, rows)
        cid = _chain_id(domain, ordered, is_closed, seam_node)
        _need(cid not in chain_domains, "canonical chain ID collision")
        chains.append(
            {
                "chain_id": cid,
                "closed": is_closed,
                "ordered_sources": ordered,
                "seam_node": seam_node,
            }
        )
        chain_domains[cid] = domain
        for step in ordered:
            _need(step["source_id"] not in seen_sources, "chain output repeated a source")
            seen_sources.add(step["source_id"])
    _need(len(seen_sources) == len(rows), "chain output lost a canonical source")
    chains.sort(key=lambda chain: chain["chain_id"])
    closed_chain_count = sum(chain["closed"] for chain in chains)
    chain_domains = {
        chain["chain_id"]: chain_domains[chain["chain_id"]] for chain in chains
    }

    xy_by_source = {}
    for sid in sorted(rows):
        cooperation("topology_geometry")
        xy_by_source[sid] = [
            [float(rows[sid]["xy"][0][0]), float(rows[sid]["xy"][0][1])],
            [float(rows[sid]["xy"][1][0]), float(rows[sid]["xy"][1][1])],
        ]
    evidence = {
        "source_count": len(rows),
        "domain_count": canonical_domain_count,
        "original_source_length_sum": total_length,
        "topology_endpoint_count": topology_endpoint_count,
        "branch_endpoint_count": branch_endpoint_count,
        "degenerate_source_count": degenerate_source_count,
        "topology_chain_count": len(chains),
        "output_chain_count": len(chains),
        "open_chain_count": len(chains) - closed_chain_count,
        "closed_chain_count": closed_chain_count,
        "max_gap_screen_px": 0.0,
        "candidate_budget": candidate_budget,
        "candidate_budget_remaining": candidate_budget,
        "candidate_distance_checks": 0,
        "candidate_pairs": 0,
        "eligible_endpoint_count": 0,
        "same_chain_neighbor_checks_excluded": 0,
        "ambiguous_endpoint_count": 0,
        "applied_link_count": 0,
        "applied_links": [],
        "rejected_cycle_components": [],
        "generated_source_count": 0,
        "sequencing": (
            "caller-declared exact endpoint topology at max_gap_px=0 "
            "-> policy v1 with link_length_px=0"
        ),
    }
    return {
        "schema": TOPOLOGY_SCHEMA,
        "chains": chains,
        "xy_by_source": xy_by_source,
        "chain_domains": chain_domains,
        "evidence": evidence,
        "qualification": {
            "profile": PROFILE,
            "native_equivalent": False,
            "public_build_chains_used": False,
            "geometry_generated": False,
            "weld_executed": False,
            "coordinate_averaging": False,
            "new_gap_link_implemented": False,
            "endpoint_identity": "exact domain/endpoint-ID adjacency; coordinates never link",
            "shared_endpoint_xy_gate": False,
            "coordinate_weld": False,
        },
    }


def adapt_intersection_phase(
    data,
    *,
    group_id,
    actual_brush_id,
    channel,
    full_mvp,
    width,
    height,
    mesh_count,
    policy,
    max_gap_px,
    candidate_budget=1_000_000,
    endpoint_count=None,
    mesh_triangle_counts=None,
    checkpoint=None,
    include_arc=False,
):
    """Return original-instance phase/active plus canonical, topology and policy proof.

    Required ``data`` keys are ``world`` (N,2,3), ``edges`` (N,2) exact
    endpoint IDs, ``pair`` (N,2) nonnegative global triangle indices,
    ``references`` (N,2,2) as ``[mesh_index, local_triangle_index]`` per support
    side, and ``supports`` with N rows.  Domain mesh pairs come only from
    ``references``.  With explicit ``mesh_triangle_counts``, ``pair`` is also
    checked against ``prefix[mesh] + local_triangle``.  Extra soup keys are
    ignored and never modified.

    ``group_id``, ``actual_brush_id`` and ``channel`` are completed caller
    selections.  ``full_mvp`` is the full world-to-clip matrix and ``width`` /
    ``height`` are the final bottom-left pixel dimensions.  Positive clip w is
    required.  ``policy`` and ``max_gap_px`` are explicit. Policy v1 still accepts
    only ``link_length_px=0``. Independent ``max_gap_px`` is bounded to 0..65536
    screen pixels. Zero preserves the frozen complete return; positive values
    add a separately named conservative degree-one gap phase-link stage.

    Topology is the exact undirected endpoint-ID graph declared by ``edges``,
    isolated by the canonical mesh-pair domain.  Degree-two chains are extracted
    directly within each domain; an equal integer endpoint ID in different
    mesh-pair domains is not connected.  The public single-position
    ``build_chains`` API is not called.  The same canonical endpoint pair in two
    mesh-pair domains remains in those separate domains rather than being
    merged or rejected.  Shared topology endpoint samples may differ in the last
    binary64 pixel bits: each canonical source uses one deterministic original
    sample, all other samples and their original geometry remain untouched, and
    the numeric spread is reported in ``shared_endpoint_diagnostics`` /
    ``duplicate_sample_diagnostics``.

    The result contains ``phase`` (N,2) in original data order, ``active`` (N,)
    bool, ``canonical_ids`` (N,), ``canonical_map`` keyed by capture-local
    canonical ID, the full ``topology`` evidence, the full v1 ``policy_plan``,
    and the two geometry diagnostics.  Duplicate instances share one canonical
    topology source/length, but their original order and reversed phase
    direction are retained.  No coordinate is averaged, welded, snapped, or
    used to infer the declared topology. Any malformed or contradictory domain/index,
    nonfinite geometry, nonpositive w, exhausted budget or cancellation rejects
    the whole call without returning partial output.
    """
    _need(type(include_arc) is bool, 'include_arc must be a boolean')
    cooperation = _Cooperation(checkpoint)
    cooperation("start", force=True)
    _need(type(data) is dict, "data must be a dict")
    missing = [
        key
        for key in ("world", "edges", "pair", "references", "supports")
        if key not in data
    ]
    _need(not missing, "data is missing required source arrays: " + ", ".join(missing))

    group_id = _identifier(group_id, "group_id")
    actual_brush_id = _identifier(actual_brush_id, "actual_brush_id")
    channel = _identifier(channel, "channel")
    width = _positive_pixels(width, "width")
    height = _positive_pixels(height, "height")
    mesh_count = _positive_pixels(mesh_count, "mesh_count")
    candidate_budget = _check_candidate_budget(candidate_budget)
    policy = _checked_policy_header(policy, max_gap_px)
    gap_radius = _finite_number(max_gap_px, "max_gap_px")

    try:
        world_shape = np.shape(data["world"])
    except (TypeError, ValueError) as exc:
        raise ValueError("world must have shape (N,2,3)") from exc
    _need(
        len(world_shape) == 3 and world_shape[1:] == (2, 3),
        "world must have shape (N,2,3)",
    )
    count = world_shape[0]
    _need(count <= MAX_INSTANCES, "input support instance count limit exceeded")
    world = _float_array(data["world"], world_shape, "world")
    edges = _integer_array(data["edges"], (count, 2), "edges")
    pair = _integer_array(data["pair"], (count, 2), "pair")
    references = _integer_array(data["references"], (count, 2, 2), "references")
    try:
        supports = np.asarray(data["supports"])
    except (TypeError, ValueError) as exc:
        raise ValueError("supports must expose one row per source instance") from exc
    _need(supports.shape == (count, 2), "supports must have shape (N,2)")
    if endpoint_count is not None:
        endpoint_count = _integer(endpoint_count, "endpoint_count", minimum=0)
        _need(
            bool((edges < endpoint_count).all()),
            "endpoint ID is outside endpoint_count",
        )
    full_mvp = _float_array(full_mvp, (4, 4), "full_mvp")
    mesh_pair = _validate_mesh_references(
        pair, references, mesh_count, mesh_triangle_counts
    )
    xy, weights = _project(world, full_mvp, width, height)

    canonical = {}
    endpoint_samples = {}
    instance_to_canonical = [None] * count
    instance_forward = [False] * count
    for index in range(count):
        cooperation("canonical_instances")
        endpoint_a = int(edges[index, 0])
        endpoint_b = int(edges[index, 1])
        _need(
            endpoint_a != endpoint_b,
            "an intersection source cannot have identical endpoint IDs",
        )
        forward = endpoint_a < endpoint_b
        endpoint_ids = (
            (endpoint_a, endpoint_b) if forward else (endpoint_b, endpoint_a)
        )
        canonical_xy = [
            [float(xy[index, 0, 0]), float(xy[index, 0, 1])],
            [float(xy[index, 1, 0]), float(xy[index, 1, 1])],
        ]
        canonical_w = [float(weights[index, 0]), float(weights[index, 1])]
        if not forward:
            canonical_xy.reverse()
            canonical_w.reverse()
        domain = _domain(group_id, actual_brush_id, channel, mesh_pair[index])
        sid = _source_id(
            group_id,
            actual_brush_id,
            channel,
            mesh_pair[index],
            endpoint_ids,
        )
        key = (domain, endpoint_ids)
        record = canonical.get(key)
        if record is None:
            record = {
                "source_id": sid,
                "domain": domain,
                "endpoint_ids": [endpoint_ids[0], endpoint_ids[1]],
                "samples": [],
            }
            canonical[key] = record
        record["samples"].append(
            (
                index,
                canonical_xy,
                canonical_w,
                _sample_key(
                    index, canonical_xy, canonical_w, world, references, pair
                ),
            )
        )
        instance_to_canonical[index] = sid
        instance_forward[index] = forward

        # Geometry is diagnostic only.  Exact endpoint IDs, not pixels or a
        # tolerance, establish topology.  Last-bit projection differences are
        # retained in their original support instances.
        for end, endpoint_id in enumerate((endpoint_a, endpoint_b)):
            endpoint_key = (domain, endpoint_id)
            stats = endpoint_samples.get(endpoint_key)
            x = float(xy[index, end, 0])
            y = float(xy[index, end, 1])
            w = float(weights[index, end])
            if stats is None:
                endpoint_samples[endpoint_key] = {
                    "count": 1,
                    "min_x": x,
                    "max_x": x,
                    "min_y": y,
                    "max_y": y,
                    "min_w": w,
                    "max_w": w,
                }
            else:
                stats["count"] += 1
                stats["min_x"] = min(stats["min_x"], x)
                stats["max_x"] = max(stats["max_x"], x)
                stats["min_y"] = min(stats["min_y"], y)
                stats["max_y"] = max(stats["max_y"], y)
                stats["min_w"] = min(stats["min_w"], w)
                stats["max_w"] = max(stats["max_w"], w)

    _need(
        len(canonical) <= MAX_CANONICAL_SOURCES,
        "canonical source count limit exceeded",
    )
    if canonical:
        _need(
            len({record["source_id"] for record in canonical.values()}) == len(canonical),
            "canonical source ID collision",
        )

    shared_endpoint_key_count = 0
    shared_endpoint_sample_count = 0
    max_shared_xy = 0.0
    max_shared_w = 0.0
    max_shared_record = None
    max_shared_w_record = None
    for (domain, endpoint_id), stats in endpoint_samples.items():
        cooperation("shared_endpoint_diagnostics")
        if stats["count"] <= 1:
            continue
        shared_endpoint_key_count += 1
        shared_endpoint_sample_count += stats["count"]
        delta_xy = max(
            stats["max_x"] - stats["min_x"],
            stats["max_y"] - stats["min_y"],
        )
        delta_w = stats["max_w"] - stats["min_w"]
        if max_shared_record is None or delta_xy > max_shared_xy:
            max_shared_xy = delta_xy
            max_shared_record = {
                "domain": domain,
                "endpoint_id": int(endpoint_id),
                "sample_count": stats["count"],
                "max_abs_delta_xy_px": float(delta_xy),
                "max_abs_delta_w": float(delta_w),
            }
        if max_shared_w_record is None or delta_w > max_shared_w:
            max_shared_w = delta_w
            max_shared_w_record = {
                "domain": domain,
                "endpoint_id": int(endpoint_id),
                "sample_count": stats["count"],
                "max_abs_delta_xy_px": float(delta_xy),
                "max_abs_delta_w": float(delta_w),
            }
    _need(
        sum(stats["count"] for stats in endpoint_samples.values()) == 2 * count,
        "shared-endpoint diagnostic lost an endpoint sample",
    )
    shared_endpoint_diagnostics = {
        "schema": "toon.viewport-intersection-shared-endpoint-diagnostic.v1",
        "endpoint_sample_count": 2 * count,
        "shared_endpoint_key_count": shared_endpoint_key_count,
        "shared_endpoint_sample_count": shared_endpoint_sample_count,
        "max_abs_delta_xy_px": float(max_shared_xy),
        "max_abs_delta_w": float(max_shared_w),
        "max_abs_delta_xy_endpoint": max_shared_record,
        "max_abs_delta_w_endpoint": max_shared_w_record,
        "epsilon_gate": None,
        "topology_uses_coordinates": False,
        "coordinate_averaging": False,
        "coordinate_welding": False,
        "method": (
            "per caller topology scope exact endpoint-ID samples; coordinate/w range is "
            "reported, never used to accept, reject, merge or position topology"
        ),
    }

    topology_sources = []
    canonical_map = {}
    duplicate_sample_diagnostics = []
    for key in sorted(canonical):
        cooperation("canonical_records")
        record = canonical[key]
        samples = record["samples"]
        representative = min(samples, key=lambda sample: (sample[3], sample[0]))
        ordered_samples = sorted(samples, key=lambda sample: sample[0])
        representative_index = representative[0]
        representative_xy = representative[1]
        representative_w = representative[2]
        representative_sample_sha256 = _digest(
            ["representative-sample.v2", representative[3]]
        )
        length = math.hypot(
            representative_xy[1][0] - representative_xy[0][0],
            representative_xy[1][1] - representative_xy[0][1],
        )
        _need(math.isfinite(length), "canonical source length is not finite")
        max_duplicate_xy = 0.0
        max_duplicate_w = 0.0
        pairwise_xy = 0.0
        pairwise_w = 0.0
        for endpoint in (0, 1):
            xs = [sample[1][endpoint][0] for sample in samples]
            ys = [sample[1][endpoint][1] for sample in samples]
            ws = [sample[2][endpoint] for sample in samples]
            pairwise_xy = max(
                pairwise_xy,
                max(xs) - min(xs),
                max(ys) - min(ys),
            )
            pairwise_w = max(pairwise_w, max(ws) - min(ws))
        sample_rows = []
        for sample_index, sample_xy, sample_w, _sample_key_value in ordered_samples:
            delta_xy = _xy_delta(sample_xy, representative_xy)
            delta_w = _w_delta(sample_w, representative_w)
            max_duplicate_xy = max(max_duplicate_xy, delta_xy)
            max_duplicate_w = max(max_duplicate_w, delta_w)
            sample_rows.append(
                {
                    "instance_index": sample_index,
                    "canonical_xy": _xy_copy(sample_xy),
                    "w": [float(sample_w[0]), float(sample_w[1])],
                    "max_abs_delta_xy_vs_representative_px": float(delta_xy),
                    "max_abs_delta_w_vs_representative": float(delta_w),
                    "sample_sha256": _digest(
                        ["canonical-duplicate-sample.v2", _sample_key_value]
                    ),
                    "is_representative": sample_index == representative_index,
                }
            )
        instance_indices = [sample[0] for sample in ordered_samples]
        if len(samples) > 1:
            duplicate_sample_diagnostics.append(
                {
                    "source_id": record["source_id"],
                    "domain": record["domain"],
                    "endpoint_ids": list(record["endpoint_ids"]),
                    "instance_indices": list(instance_indices),
                    "instance_count": len(samples),
                    "representative_instance_index": representative_index,
                    "representative_selection": REPRESENTATIVE_RULE,
                    "representative_sample_sha256": representative_sample_sha256,
                    "max_abs_delta_xy_vs_representative_px": float(max_duplicate_xy),
                    "max_abs_delta_w_vs_representative": float(max_duplicate_w),
                    "max_abs_pairwise_delta_xy_px": float(pairwise_xy),
                    "max_abs_pairwise_delta_w": float(pairwise_w),
                    "samples": sample_rows,
                }
            )
        source = {
            "source_id": record["source_id"],
            "endpoint_ids": list(record["endpoint_ids"]),
            "xy": _xy_copy(representative_xy),
            "w": [float(representative_w[0]), float(representative_w[1])],
            "domain": record["domain"],
        }
        topology_sources.append(source)
        canonical_map[record["source_id"]] = {
            "domain": record["domain"],
            "endpoint_ids": list(record["endpoint_ids"]),
            "xy": _xy_copy(representative_xy),
            "w": [float(representative_w[0]), float(representative_w[1])],
            "length_px": float(length),
            "instance_indices": list(instance_indices),
            "sample_count": len(samples),
            "representative_instance_index": representative_index,
            "representative_selection": REPRESENTATIVE_RULE,
            "representative_sample_sha256": representative_sample_sha256,
            "max_abs_delta_xy_vs_representative_px": float(max_duplicate_xy),
            "max_abs_delta_w_vs_representative": float(max_duplicate_w),
            "max_abs_pairwise_delta_xy_px": float(pairwise_xy),
            "max_abs_pairwise_delta_w": float(pairwise_w),
            "source_u": [0.0, 1.0],
        }
    topology = _build_declared_topology(
        topology_sources,
        candidate_budget=candidate_budget,
        cooperation=cooperation,
    )
    phase_topology = topology
    if gap_radius > 0:
        phase_topology = link_declared_topology(
            topology, topology_sources, max_gap_px=gap_radius,
            candidate_budget=candidate_budget, cooperation=cooperation)
    policy_plan = plan_style_chains(
        phase_topology["chains"],
        phase_topology["xy_by_source"],
        policy,
        checkpoint=lambda: cooperation("policy", force=True),
    )
    spans = {row["source_id"]: row for row in policy_plan["source_spans"]}
    _need(
        set(spans) == set(canonical_map),
        "policy source spans do not conserve every canonical source exactly once",
    )
    _need(
        len(policy_plan["source_spans"]) == len(canonical_map),
        "policy emitted a duplicate source span",
    )
    suppressed = set(policy_plan["suppressed_sources"])
    _need(
        len(suppressed) == len(policy_plan["suppressed_sources"]),
        "policy suppressed a canonical source more than once",
    )
    _need(suppressed <= set(canonical_map), "policy suppressed an unknown source")

    phase = np.empty((count, 2), dtype=np.float64)
    active = np.empty(count, dtype=np.bool_)
    if include_arc:
        from .dash_splitter import arc_spans_from_policy
        arc_by_source, unused_totals = arc_spans_from_policy(policy_plan,
            checkpoint=lambda: cooperation('arc_metric', force=True))
        arc = np.empty((count, 2), dtype=np.float64)
    for index in range(count):
        cooperation("phase_instances")
        sid = instance_to_canonical[index]
        span = spans[sid]
        values = span["chain_parameters"]
        _need(
            type(values) is list and len(values) == 2,
            "policy source span must contain two phase values",
        )
        c0 = _finite_number(values[0], "chain parameter")
        c1 = _finite_number(values[1], "chain parameter")
        _need(
            0.0 <= c0 <= 1.0 and 0.0 <= c1 <= 1.0,
            "policy phase must remain in the original source-u domain [0,1]",
        )
        if not instance_forward[index]:
            c0, c1 = c1, c0
        phase[index] = (c0, c1)
        active[index] = sid not in suppressed
        if include_arc:
            ends = arc_by_source[sid]
            arc[index] = ends if instance_forward[index] else ends[::-1]

    canonical_ids = [instance_to_canonical[index] for index in range(count)]
    _need(len(canonical_ids) == count, "canonical instance mapping is incomplete")
    try:
        canonical_length_sum = math.fsum(
            record["length_px"] for record in canonical_map.values()
        )
    except OverflowError as exc:
        raise ValueError("canonical source length sum is outside binary64 range") from exc
    _need(
        math.isfinite(canonical_length_sum),
        "canonical source length sum is not finite",
    )
    cooperation("finish", force=True)
    result = {
        "schema": SCHEMA,
        "phase": phase,
        "active": active,
        "canonical_ids": canonical_ids,
        "canonical_map": canonical_map,
        "topology": topology,
        "policy_plan": policy_plan,
        "shared_endpoint_diagnostics": shared_endpoint_diagnostics,
        "duplicate_sample_diagnostics": duplicate_sample_diagnostics,
        "evidence": {
            "instance_count": count,
            "canonical_source_count": len(canonical_map),
            "duplicate_instance_count": count - len(canonical_map),
            "duplicate_canonical_count": len(duplicate_sample_diagnostics),
            "active_instance_count": int(active.sum()),
            "suppressed_instance_count": count - int(active.sum()),
            "original_order_preserved": True,
            "canonical_length_sum_px": canonical_length_sum,
            "suppressed_canonical_count": len(suppressed),
            "max_gap_px": 0.0,
            "policy_link_length_px": 0.0,
            "intersection_gap_linking": "unimplemented_this_slice",
            "canonicalization": "exact undirected endpoint-ID pair within declared domain",
            "domain": "group_id/actual_brush_id/channel/sorted reference mesh-index pair",
            "mesh_pair_source": "references",
            "pair_semantics": (
                "nonnegative global triangle indices; never mesh-pair identity"
            ),
            "pair_global_validation": (
                "optional mesh_triangle_counts: pair == prefix[mesh] + local_triangle"
            ),
            "support_tags_independent": True,
            "topology_scope": "caller-declared finite-f64 intersection topology",
            "topology_domain_scope": (
                "group_id/actual_brush_id/channel/sorted reference mesh-pair; "
                "topology adjacency is domain-isolated"
            ),
            "public_build_chains_used": False,
            "shared_endpoint_epsilon_gate": False,
            "duplicate_representative_selection": REPRESENTATIVE_RULE,
            "phase_backfill": "swap the two policy phase values for a reversed original endpoint order",
            "original_endpoint_direction_preserved": True,
            "source_u_domain": [0.0, 1.0],
            "generated_source_count": 0,
        },
        "qualification": {
            "profile": PROFILE,
            "api_identity": (
                "caller-declared finite-f64 intersection topology phase adapter; "
                "not the public single-position build_chains API"
            ),
            "native_equivalent": False,
            "public_linker_contract_unchanged": True,
            "geometry_generated": False,
            "curves_consumed": False,
            "maps_consumed": False,
            "brush_width_or_color_consumed": False,
            "bucket_modified": False,
            "source_geometry_modified": False,
            "coordinate_averaging_or_welding": False,
            "topology_identity_independent_of_sample_xy": True,
            "shared_endpoint_samples_preserved": True,
            "support_instances_retained": True,
            "support_tags_independent": True,
            "source_ids_scope": "capture-local canonical IDs; not a cross-frame identity claim",
            "positive_gap_linker": (
                "not implemented or qualified in this slice; a separately validated "
                "independent positive-gap linker with policy link_length_px=0 would "
                "not by itself violate policy v1"
            ),
            "blender_or_gpu": False,
        },
    }
    if gap_radius > 0:
        result["gap_linker"] = phase_topology
        result["evidence"].update(
            max_gap_px=gap_radius, intersection_gap_linking=GAP_PROFILE,
            applied_gap_link_count=phase_topology["evidence"]["applied_link_count"],
            gap_length_added_px=0.0)
        result["qualification"].update(
            profile="caller_declared_finite_f64_intersection_topology_with_unique_gap.v1",
            positive_gap_linker=GAP_PROFILE, native_equivalent=False,
            policy_link_length_px=0.0, gap_geometry_generated=False,
            gap_length_counted=False, public_build_chains_used=False)
    if include_arc:result['arc_px'] = arc
    return result


__all__ = ["adapt_intersection_phase"]
