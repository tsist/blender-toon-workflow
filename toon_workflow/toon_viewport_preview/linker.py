# SPDX-License-Identifier: GPL-3.0-or-later
"""Autonomous, conservative source-phase linker; no geometry is generated.

Caller supplies explicit screen pixels and domain identity. This module does not
import or modify the v1 policy, perform welding, clipping, or infer native rules.
"""
import copy
import hashlib
import json
import math

SCHEMA = 'toon.viewport-source-chain-linker.v1'
PROFILE = 'autonomous_unique_pair_link.v1'
MAX_SOURCES = 100_000
MAX_IDENTIFIER_LENGTH = 256
MAX_CANDIDATE_CHECKS = 2_000_000
_FIELDS = {'source_id', 'endpoint_ids', 'xy', 'w', 'domain'}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    _need(type(value) is str and 0 < len(value) <= MAX_IDENTIFIER_LENGTH,
          'identifier must be a nonempty string of at most 256 characters')
    try:
        value.encode('utf8')
    except UnicodeError as exc:
        raise ValueError('identifier must encode as UTF-8') from exc
    return value


def _number(value):
    _need(type(value) in (int, float), 'finite numeric value required')
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError('number is outside binary64 range') from exc
    _need(math.isfinite(result), 'finite binary64 value required')
    return result


def _chain_id(domain, steps, closed, seam):
    payload = json.dumps([domain, steps, closed, seam], ensure_ascii=False,
                         separators=(',', ':'), sort_keys=True).encode('utf8')
    return 'chain-' + hashlib.sha256(payload).hexdigest()


def _reverse(steps, cooperate):
    result = []
    for step in reversed(steps):
        cooperate('reverse_sources')
        result.append(dict(source_id=step['source_id'],
                      orientation='reverse' if step['orientation'] == 'forward' else 'forward'))
    return result


def _cell(value, radius):
    # Exact integer floor of the binary64 ratio avoids x/radius overflowing
    # for otherwise finite coordinates and positive subnormal radii.
    xn, xd = value.as_integer_ratio()
    rn, rd = radius.as_integer_ratio()
    return (xn * rd) // (xd * rn)


def build_chains(sources, *, max_gap_px=0, candidate_budget=1_000_000,
                 checkpoint=lambda: None):
    """Return detached v1-compatible chains and original xy_by_source.

    Each plain JSON source has exactly source_id, endpoint_ids:[id0,id1],
    xy:[[x0,y0],[x1,y1]], w:[positive_w0,positive_w1], and domain:string.
    domain is caller-encoded group/actual/channel/mesh identity, not interpreted
    here. Endpoint IDs are local to that domain; source IDs are globally unique.
    max_gap_px=0 disables all new links, including zero-gap new links.
    Any malformed input or exhausted candidate budget rejects the whole call.
    A repeated (domain, endpoint_id) must have exactly equal received numeric
    xy and w values. No tolerance or implicit welding is used.
    """
    _need(callable(checkpoint), 'checkpoint must be callable')
    checkpoint()
    _need(type(sources) is list and len(sources) <= MAX_SOURCES, 'source count/type limit')
    radius = _number(max_gap_px)
    _need(radius >= 0, 'max_gap_px must be nonnegative screen pixels')
    _need(type(candidate_budget) is int and 0 <= candidate_budget <= MAX_CANDIDATE_CHECKS,
          'candidate budget must be an integer in 0..2000000')
    work = {}
    def cooperate(phase, *, force=False):
        count = work.get(phase, 0)
        if force or count % 256 == 0:
            checkpoint()
        work[phase] = count + 1

    rows, xy_map, adjacency, lengths, endpoint_geometry = {}, {}, {}, [], {}
    for i, source in enumerate(sources):
        cooperate('input_sources')
        _need(type(source) is dict and set(source) == _FIELDS, 'source fields differ')
        sid = _text(source['source_id'])
        domain = _text(source['domain'])
        _need(sid not in rows, 'duplicate source_id')
        ends = source['endpoint_ids']
        _need(type(ends) is list and len(ends) == 2, 'two endpoint IDs required')
        ends = [_text(value) for value in ends]
        xy, w = source['xy'], source['w']
        _need(type(xy) is list and len(xy) == 2, 'two original projected points required')
        points = []
        for point in xy:
            _need(type(point) is list and len(point) == 2, '2D point required')
            points.append(tuple(_number(value) for value in point))
        _need(type(w) is list and len(w) == 2 and all(_number(value) > 0 for value in w),
              'both original projection w values must be positive')
        for end, node in enumerate(ends):
            key = (domain, node)
            # Compare received values before binary64 coercion: large unequal
            # integers must not become equal merely through float rounding.
            geometry = (tuple(xy[end]), w[end])
            _need(key not in endpoint_geometry or endpoint_geometry[key] == geometry,
                  'conflicting xy/w for the same (domain, endpoint_id)')
            endpoint_geometry[key] = geometry
        length = math.hypot(points[1][0]-points[0][0], points[1][1]-points[0][1])
        _need(math.isfinite(length), 'source chord length is outside binary64 range')
        lengths.append(length)
        row = dict(sid=sid, domain=domain, ends=ends, points=points, length=length)
        rows[sid] = row
        xy_map[sid] = copy.deepcopy(xy)
        for end, node in enumerate(ends):
            adjacency.setdefault((domain, node), []).append((sid, end))
    for neighbors in adjacency.values():
        cooperate('adjacency_lists')
        if len(neighbors) > 256:
            cooperate('adjacency_sort', force=True)
        neighbors.sort(key=lambda half: (rows[half[0]]['ends'][1-half[1]], half[1], half[0]))
        if len(neighbors) > 256:
            cooperate('adjacency_sort', force=True)
    cooperate('length_sum', force=True)
    try:
        total_length = math.fsum(lengths)
    except OverflowError as exc:
        raise ValueError('total source length is outside binary64 range') from exc
    _need(math.isfinite(total_length), 'nonfinite total source length')
    cooperate('length_sum', force=True)

    visited, topo = set(), []

    def follow(start_key, first_sid, first_end):
        sid, entering = first_sid, first_end
        steps = []
        while True:
            cooperate('topology_sources')
            _need(sid not in visited, 'internal topology traversal repeated a source')
            visited.add(sid)
            row = rows[sid]
            steps.append(dict(source_id=sid, orientation='forward' if entering == 0 else 'reverse'))
            exit_key = (row['domain'], row['ends'][1-entering])
            if len(adjacency[exit_key]) != 2:
                break
            candidates = [half for half in adjacency[exit_key] if half[0] not in visited]
            if not candidates:
                break
            _need(len(candidates) == 1, 'invalid degree-two topology')
            sid, entering = candidates[0]
        closed = exit_key == start_key and len(adjacency[start_key]) == 2
        seam = start_key[1] if closed else None
        cooperate('chain_hash', force=True)
        chain = dict(chain_id=_chain_id(start_key[0], steps, closed, seam), closed=closed,
                     ordered_sources=steps, seam_node=seam)
        cooperate('chain_hash', force=True)
        topo.append(dict(chain=chain, domain=start_key[0], ends=(start_key, exit_key)))

    # Start at all degree != 2 vertices first. Starting arbitrarily inside an
    # open chain would incorrectly divide that chain in two.
    cooperate('vertex_sort', force=True)
    vertices = sorted(adjacency)
    cooperate('vertex_sort', force=True)
    for vertex in vertices:
        cooperate('topology_vertices')
        if len(adjacency[vertex]) != 2:
            for sid, end in adjacency[vertex]:
                cooperate('topology_half_edges')
                if sid not in visited:
                    follow(vertex, sid, end)
    cooperate('source_sort', force=True)
    source_ids = sorted(rows)
    cooperate('source_sort', force=True)
    # Remaining pure degree-two components are loops. Choose the smallest
    # physical vertex, not an opaque source ID that can change per capture or
    # channel. The far endpoint determines the outgoing half-edge first.
    for vertex in vertices:
        cooperate('remaining_topology_vertices')
        if len(adjacency[vertex]) != 2:
            continue
        candidates = [half for half in adjacency[vertex] if half[0] not in visited]
        if candidates:
            sid, end = min(candidates, key=lambda half:
                           (rows[half[0]]['ends'][1-half[1]], half[0], half[1]))
            follow(vertex, sid, end)
    _need(len(visited) == len(rows), 'topology lost a source')
    cooperate('topology_sort', force=True)
    topo.sort(key=lambda item: item['chain']['chain_id'])
    cooperate('topology_sort', force=True)

    eligible = []
    branch_ends = degenerate_ends = 0
    for ci, item in enumerate(topo):
        cooperate('eligible_chains')
        if item['chain']['closed']:
            continue
        steps = item['chain']['ordered_sources']
        for end, key in enumerate(item['ends']):
            if len(adjacency[key]) != 1:
                branch_ends += 1
                continue
            step = steps[0 if end == 0 else -1]
            sid = step['source_id']
            raw_end = end if step['orientation'] == 'forward' else 1-end
            if rows[sid]['length'] == 0:
                degenerate_ends += 1
                continue
            eligible.append(dict(chain=ci, end=end, key=key, sid=sid,
                                 raw_end=raw_end, xy=rows[sid]['points'][raw_end]))
    cooperate('eligible_sort', force=True)
    eligible.sort(key=lambda ep: (ep['key'], topo[ep['chain']]['chain']['chain_id'], ep['end']))
    cooperate('eligible_sort', force=True)
    neighbor_count = [0] * len(eligible)
    sole_neighbor = [None] * len(eligible)
    checks = candidates = same_chain_checks = 0
    grid = {}
    if radius > 0:
        for i, endpoint in enumerate(eligible):
            cooperate('candidate_endpoints')
            x, y = endpoint['xy']
            gx, gy = _cell(x, radius), _cell(y, radius)
            domain = endpoint['key'][0]
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    for j in grid.get((domain, gx+dx, gy+dy), ()):
                        checks += 1
                        _need(checks <= candidate_budget, 'candidate budget exceeded; no partial result')
                        cooperate('candidate_checks')
                        other = eligible[j]
                        if endpoint['chain'] == other['chain']:
                            same_chain_checks += 1
                            continue
                        distance = math.hypot(x-other['xy'][0], y-other['xy'][1])
                        if distance <= radius:
                            candidates += 1
                            neighbor_count[i] += 1
                            neighbor_count[j] += 1
                            sole_neighbor[i] = j
                            sole_neighbor[j] = i
            grid.setdefault((domain, gx, gy), []).append(i)
    selected = []
    for i in range(len(eligible)):
        cooperate('select_pairs')
        if neighbor_count[i] == 1 and neighbor_count[sole_neighbor[i]] == 1 and i < sole_neighbor[i]:
            selected.append((i, sole_neighbor[i]))

    # Components in the selected chain graph have degree <= 2. Reject every
    # link in a new cycle, including two chains joined by two parallel pairs.
    parent = list(range(len(topo)))
    def find(a):
        while parent[a] != a:
            cooperate('union_find')
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i, j in selected:
        cooperate('union_pairs')
        a, b = find(eligible[i]['chain']), find(eligible[j]['chain'])
        if a != b:
            parent[max(a, b)] = min(a, b)
    components = {}
    for pair in selected:
        cooperate('component_pairs')
        components.setdefault(find(eligible[pair[0]]['chain']), []).append(pair)
    accepted, rejected_cycles = [], []
    for pairs in components.values():
        cooperate('components')
        members = set()
        for pair in pairs:
            cooperate('component_members')
            members.update(eligible[index]['chain'] for index in pair)
        if len(pairs) >= len(members):
            cooperate('cycle_sort', force=True)
            rejected_cycles.append(dict(reason='new_link_cycle_unsupported',
                topology_chain_ids=sorted(topo[ci]['chain']['chain_id'] for ci in members),
                rejected_pair_count=len(pairs)))
            cooperate('cycle_sort', force=True)
        else:
            for pair in pairs:
                cooperate('accept_pairs')
                accepted.append(pair)
    links = {}
    for i, j in accepted:
        cooperate('link_table')
        a, b = eligible[i], eligible[j]
        links[(a['chain'], a['end'])] = (b['chain'], b['end'])
        links[(b['chain'], b['end'])] = (a['chain'], a['end'])

    completed, output = set(), []
    for ci, item in enumerate(topo):
        cooperate('merge_components')
        if ci in completed:
            continue
        if not any((ci, end) in links for end in (0, 1)):
            output.append((item['chain'], item['domain']))
            completed.add(ci)
            continue
        # Find this component's free outer endpoint without depending on input
        # source order. Walking one direction is bounded by its source count.
        current, entering = ci, 0
        searching = set()
        while (current, entering) in links:
            cooperate('merge_search')
            _need(current not in searching, 'internal cycle escaped rejection')
            searching.add(current)
            current, remote_end = links[(current, entering)]
            entering = 1-remote_end
        start = (current, entering)
        steps = []
        while True:
            cooperate('merge_chains')
            _need(current not in completed, 'chain component visited twice')
            completed.add(current)
            original = topo[current]['chain']['ordered_sources']
            for step in original if entering == 0 else _reverse(original, cooperate):
                cooperate('merge_sources')
                steps.append(step)
            remote = links.get((current, 1-entering))
            if remote is None:
                break
            current, entering = remote
        domain = topo[start[0]]['domain']
        # Physical traversal tokens determine direction before opaque IDs.
        backward = _reverse(steps, cooperate)
        def signature(seq):
            physical, opaque = [], []
            for step in seq:
                cooperate('merge_signature')
                ends = rows[step['source_id']]['ends']
                physical.append(tuple(ends) if step['orientation'] == 'forward' else tuple(reversed(ends)))
                opaque.append((step['source_id'], step['orientation']))
            return physical, opaque
        if signature(backward) < signature(steps):
            steps = backward
        cooperate('chain_hash', force=True)
        chain = dict(chain_id=_chain_id(domain, steps, False, None), closed=False,
                     ordered_sources=steps, seam_node=None)
        cooperate('chain_hash', force=True)
        output.append((chain, domain))
    cooperate('output_sort', force=True)
    output.sort(key=lambda item: item[0]['chain_id'])
    cooperate('output_sort', force=True)
    chains, chain_domains, final_sources = [], {}, set()
    final_count = 0
    for chain, domain in output:
        cooperate('output_chains')
        chains.append(chain)
        chain_domains[chain['chain_id']] = domain
        for step in chain['ordered_sources']:
            cooperate('conservation_sources')
            _need(step['source_id'] not in final_sources, 'final chain repeated a source')
            final_sources.add(step['source_id'])
            final_count += 1
    _need(final_count == len(rows), 'final chain source conservation failed')

    def endpoint_record(index):
        ep = eligible[index]
        return dict(source_id=ep['sid'], source_endpoint=ep['raw_end'],
                    topology_endpoint_id=ep['key'][1], domain=ep['key'][0])
    ordered_xy = {}
    for sid in source_ids:
        cooperate('output_geometry')
        ordered_xy[sid] = xy_map[sid]
    cooperate('accepted_sort', force=True)
    ordered_accepted = sorted(accepted)
    cooperate('accepted_sort', force=True)
    applied_links = []
    for i, j in ordered_accepted:
        cooperate('link_evidence')
        applied_links.append(dict(a=endpoint_record(i), b=endpoint_record(j)))
    ambiguous_count = 0
    for count in neighbor_count:
        cooperate('ambiguity_evidence')
        ambiguous_count += count > 1
    result = dict(schema=SCHEMA, chains=chains,
        xy_by_source=ordered_xy, chain_domains=chain_domains,
        evidence=dict(source_count=len(rows), original_source_length_sum=total_length,
            topology_chain_count=len(topo), output_chain_count=len(chains),
            max_gap_screen_px=radius, eligible_endpoint_count=len(eligible),
            candidate_distance_checks=checks, candidate_pairs=candidates,
            same_chain_neighbor_checks_excluded=same_chain_checks,
            ambiguous_endpoint_count=ambiguous_count,
            branch_endpoints_excluded=branch_ends, degenerate_endpoints_excluded=degenerate_ends,
            applied_link_count=len(accepted),
            applied_links=applied_links,
            rejected_cycle_components=rejected_cycles, generated_source_count=0,
            sequencing='original topology -> unique-pair link -> caller invokes v1 split/orient/min with link_length_px=0'),
        qualification=dict(profile=PROFILE, native_equivalent=False, weld_executed=False,
            geometry_generated=False, coordinates='caller-supplied positive-w original screen pixels',
            gap_units='explicit screen pixels; caller owns native radius scaling',
            endpoint_identity='same domain/endpoint ID requires exactly equal received numeric xy and w',
            cancellation='cooperative <=256 Python work units per phase; sorts, hashing, fsum and allocations have stage checks only; no GUI latency guarantee',
            competition_policy='retain unlinked; no nearest or tie-break inference',
            new_cycles='reject all links in that component; original topology loops retained'))
    checkpoint()
    # All containers were allocated here; the only copied input container is a
    # fixed 2x2 xy list above. No shared mutable input or global state is exposed.
    return result
