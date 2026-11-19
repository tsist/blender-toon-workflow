# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent conservative phase-only links over declared intersection chains.

The adapter supplies its validated owned topology and canonical source samples.
Logical endpoint identity remains exact domain/ID, including unequal f64 sample
positions at an existing join.  Coordinates are used only to consider new links
between original degree-one endpoints; no coordinates or source IDs are changed.

The project's GPL linker.py is the design reference for a bounded spatial grid,
mutually unique pairs and rejecting new cycle components.  Its public
single-position build_chains API is deliberately not called or relaxed here.
"""
import copy
import hashlib
import json
import math

SCHEMA = 'toon.intersection-independent-gap-phase-linker.v1'
PROFILE = 'caller_declared_intersection_unique_degree1_gap.v1'
MAX_GAP_PX = 65_536.0


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _cell(value, radius):
    # Exact integer floor of the binary64 ratio: finite coordinates divided by
    # a positive subnormal radius must not overflow merely to form a grid key.
    numerator, denominator = value.as_integer_ratio()
    rn, rd = radius.as_integer_ratio()
    return (numerator * rd) // (denominator * rn)


def _reverse(steps, cooperate):
    result = []
    for step in reversed(steps):
        cooperate('gap_reverse')
        result.append(dict(source_id=step['source_id'],
            orientation='reverse' if step['orientation'] == 'forward' else 'forward'))
    return result


def _signature(steps, rows, cooperate):
    signature = []
    for step in steps:
        cooperate('gap_signature')
        signature.append((tuple(rows[step['source_id']]['endpoint_ids']), step['orientation']))
    return tuple(signature)


def _chain_id(domain, steps):
    payload = json.dumps([domain, steps, False, None], sort_keys=True,
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return 'chain-' + hashlib.sha256(payload).hexdigest()


def link_declared_topology(topology, sources, *, max_gap_px, candidate_budget, cooperation):
    """Link an adapter-owned, validated topology; never parse external soup here.

    Positive max_gap_px only.  Competing neighbors, a near endpoint of the same
    original chain, branch ends, collapsed endpoint chords and new cycles retain
    their original chains.  This is an explicit conservative autonomous profile,
    not a nearest-neighbor or native Pencil rule.
    """
    cooperation('gap_start', force=True)
    _need(0 < max_gap_px <= MAX_GAP_PX, 'independent intersection gap must be in (0,65536] pixels')
    rows = {}
    degrees = {}
    for source in sources:
        cooperation('gap_source_rows')
        sid = source['source_id']
        _need(sid not in rows, 'gap source identity repeated')
        rows[sid] = source
        for node in source['endpoint_ids']:
            key = (source['domain'], node)
            degrees[key] = degrees.get(key, 0) + 1
    # Detached stage result.  Existing declared topology is returned separately
    # by the adapter, so its zero-gap evidence remains its own unchanged proof.
    cooperation('gap_copy', force=True)
    result = copy.deepcopy(topology)
    cooperation('gap_copy', force=True)
    chains = result['chains']
    domains = result['chain_domains']
    eligible = []
    branch_ends = collapsed_ends = 0
    for ci, chain in enumerate(chains):
        cooperation('gap_eligible_chains')
        if chain['closed']:
            continue
        steps = chain['ordered_sources']
        for end in (0, 1):
            step = steps[0 if end == 0 else -1]
            source = rows[step['source_id']]
            raw_end = end if step['orientation'] == 'forward' else 1 - end
            node = source['endpoint_ids'][raw_end]
            key = (source['domain'], node)
            _need(source['domain'] == domains[chain['chain_id']], 'gap chain crosses a declared domain')
            if degrees[key] != 1:
                branch_ends += 1
                continue
            xy = source['xy']
            if math.hypot(xy[1][0] - xy[0][0], xy[1][1] - xy[0][1]) == 0:
                collapsed_ends += 1
                continue
            eligible.append(dict(chain=ci, end=end, key=key, sid=source['source_id'],
                                 raw_end=raw_end, xy=tuple(source['xy'][raw_end])))
    cooperation('gap_sort', force=True)
    eligible.sort(key=lambda ep: (ep['key'], chains[ep['chain']]['chain_id'], ep['end']))
    cooperation('gap_sort', force=True)
    neighbors = [0] * len(eligible)
    sole = [None] * len(eligible)
    self_blocked = set()
    checks = candidate_pairs = self_checks = self_pairs = 0
    grid = {}
    for i, endpoint in enumerate(eligible):
        cooperation('gap_candidate_endpoints')
        x, y = endpoint['xy']
        gx, gy = _cell(x, max_gap_px), _cell(y, max_gap_px)
        domain = endpoint['key'][0]
        # The final predicate uses rounded binary64 subtraction/hypot.
        # A pair such as nextafter(1,0) and 2 can round to distance 1 while
        # exact-ratio cells differ by 2. One extra cell per axis is a
        # conservative broadphase (exact coordinate difference <2*radius);
        # all extra checks still consume budget and use the unchanged <= gate.
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                for j in grid.get((domain, gx + dx, gy + dy), ()):
                    _need(checks < candidate_budget, 'intersection gap candidate budget exceeded; no partial result')
                    checks += 1
                    cooperation('gap_candidate_checks')
                    other = eligible[j]
                    distance = math.hypot(x - other['xy'][0], y - other['xy'][1])
                    if endpoint['chain'] == other['chain']:
                        self_checks += 1
                        if distance <= max_gap_px:
                            self_pairs += 1
                            self_blocked.update((i, j))
                        continue
                    if distance <= max_gap_px:
                        candidate_pairs += 1
                        neighbors[i] += 1
                        neighbors[j] += 1
                        sole[i], sole[j] = j, i
        grid.setdefault((domain, gx, gy), []).append(i)
    selected = []
    for i, count in enumerate(neighbors):
        cooperation('gap_select')
        j = sole[i]
        if (count == 1 and neighbors[j] == 1 and i < j
                and i not in self_blocked and j not in self_blocked):
            selected.append((i, j))

    # Selected chain graph degree <=2.  Preserve original loops and reject all
    # links in a new cycle, including parallel pairs between two open chains.
    parent = list(range(len(chains)))
    def find(index):
        while parent[index] != index:
            cooperation('gap_union_find')
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for i, j in selected:
        cooperation('gap_union')
        left, right = find(eligible[i]['chain']), find(eligible[j]['chain'])
        if left != right:
            parent[max(left, right)] = min(left, right)
    components = {}
    for pair in selected:
        cooperation('gap_components')
        components.setdefault(find(eligible[pair[0]]['chain']), []).append(pair)
    accepted = []
    rejected_cycles = []
    for pairs in components.values():
        cooperation('gap_component')
        members = set()
        for pair in pairs:
            cooperation('gap_component_members')
            members.update(eligible[index]['chain'] for index in pair)
        if len(pairs) >= len(members):
            rejected_cycles.append(dict(reason='new_gap_cycle_not_supported',
                topology_chain_ids=sorted(chains[index]['chain_id'] for index in members),
                rejected_pair_count=len(pairs)))
        else:
            accepted.extend(pairs)
    links = {}
    for i, j in accepted:
        cooperation('gap_link_table')
        left, right = eligible[i], eligible[j]
        links[(left['chain'], left['end'])] = (right['chain'], right['end'])
        links[(right['chain'], right['end'])] = (left['chain'], left['end'])
    completed = set()
    output = []
    for ci, chain in enumerate(chains):
        cooperation('gap_merge_components')
        if ci in completed:
            continue
        domain = domains[chain['chain_id']]
        if not any((ci, end) in links for end in (0, 1)):
            output.append((chain, domain))
            completed.add(ci)
            continue
        current, entering = ci, 0
        searching = set()
        while (current, entering) in links:
            cooperation('gap_merge_search')
            _need(current not in searching, 'internal intersection gap cycle escaped rejection')
            searching.add(current)
            current, remote_end = links[(current, entering)]
            entering = 1 - remote_end
        steps = []
        while True:
            cooperation('gap_merge_chains')
            _need(current not in completed, 'intersection gap component visited twice')
            completed.add(current)
            original = chains[current]['ordered_sources']
            for step in original if entering == 0 else _reverse(original, cooperation):
                cooperation('gap_merge_sources')
                _need(rows[step['source_id']]['domain'] == domain, 'gap merge crossed a declared domain')
                steps.append(dict(step))
            remote = links.get((current, 1 - entering))
            if remote is None:
                break
            current, entering = remote
        backward = _reverse(steps, cooperation)
        if _signature(backward, rows, cooperation) < _signature(steps, rows, cooperation):
            steps = backward
        cooperation('gap_chain_hash', force=True)
        cid = _chain_id(domain, steps)
        cooperation('gap_chain_hash', force=True)
        output.append((dict(chain_id=cid, closed=False, ordered_sources=steps, seam_node=None), domain))
    output.sort(key=lambda item: item[0]['chain_id'])
    final_sources = set()
    chain_domains = {}
    for chain, domain in output:
        cooperation('gap_conservation_chains')
        _need(chain['chain_id'] not in chain_domains, 'gap output chain ID collision')
        chain_domains[chain['chain_id']] = domain
        for step in chain['ordered_sources']:
            cooperation('gap_conservation_sources')
            sid = step['source_id']
            _need(sid not in final_sources, 'gap output repeated a canonical source')
            final_sources.add(sid)
    _need(final_sources == set(rows), 'gap output lost a canonical source')
    def endpoint_record(index):
        endpoint = eligible[index]
        return dict(source_id=endpoint['sid'], source_endpoint=endpoint['raw_end'],
                    topology_endpoint_id=endpoint['key'][1], domain=endpoint['key'][0],
                    xy=list(endpoint['xy']))
    applied = []
    for i, j in sorted(accepted):
        cooperation('gap_link_evidence')
        left, right = eligible[i], eligible[j]
        applied.append(dict(a=endpoint_record(i), b=endpoint_record(j),
            gap_distance_px=math.hypot(left['xy'][0] - right['xy'][0], left['xy'][1] - right['xy'][1])))
    result['schema'] = SCHEMA
    result['chains'] = [chain for chain, domain in output]
    result['chain_domains'] = chain_domains
    closed_count = sum(chain['closed'] for chain, domain in output)
    result['evidence'].update(max_gap_screen_px=max_gap_px, candidate_budget=candidate_budget,
        candidate_budget_remaining=candidate_budget - checks, candidate_distance_checks=checks,
        candidate_pairs=candidate_pairs, eligible_endpoint_count=len(eligible),
        same_chain_neighbor_checks_excluded=self_checks, self_candidate_pairs=self_pairs,
        self_ambiguous_endpoint_count=len(self_blocked),
        competing_endpoint_count=sum(count > 1 for count in neighbors),
        ambiguous_endpoint_count=sum(count > 1 or index in self_blocked for index, count in enumerate(neighbors)),
        branch_endpoints_excluded=branch_ends, degenerate_endpoints_excluded=collapsed_ends,
        applied_link_count=len(accepted), applied_links=applied, rejected_cycle_components=rejected_cycles,
        output_chain_count=len(output), open_chain_count=len(output)-closed_count,
        closed_chain_count=closed_count, generated_source_count=0, gap_length_added_px=0.0,
        sequencing='declared exact-ID topology -> independent mutual-unique degree1 gap phase links -> policy v1 link_length_px=0')
    result['qualification'].update(profile=PROFILE, native_equivalent=False,
        new_gap_link_implemented=True, public_build_chains_used=False,
        gap_units='explicit screen pixels; caller owns radius scaling',
        competition_policy='every endpoint must have exactly one cross-chain candidate; competing/tied/self-near endpoints stay unlinked',
        self_ambiguity='same-original-chain endpoints within radius block both endpoints; no self-closing link',
        new_cycles='reject all links in that component; original declared loops retained',
        gap_length_counted=False, generated_source_count=0,
        endpoint_identity='original exact domain/ID; shared logical samples are not equalized',
        coordinate_averaging=False, coordinate_weld=False, weld_executed=False,
        cancellation='cooperative stage/work-unit checks; no GUI latency guarantee')
    cooperation('gap_finish', force=True)
    return result
