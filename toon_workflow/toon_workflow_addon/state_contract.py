"""Pure, versioned witnesses for persisted Toon state; never imports bpy.

The digest covers the complete ordered chain provenance, without retaining it
inside a blend. Callers can persist the canonical chains as audit sidecars.
"""
import math
import re

from . import contract
from .contract import WorkflowError

STATE_SCHEMA = 'toon-state-v2'
FRAME_SCHEMA = 'toon-frame-report-v2'
CHAIN_SCHEMA = 'toon-chain-provenance-json-sha256-v1'
STATE_SCHEMA_V3 = 'toon-state-v3'
FRAME_SCHEMA_V3 = 'toon-frame-report-v3'
CHAIN_SCHEMA_V2 = 'toon-chain-provenance-json-sha256-v2'
STATE_SCHEMA_V4 = 'toon-state-v4'
FRAME_SCHEMA_V4 = 'toon-frame-report-v4'
CHAIN_SCHEMA_V3 = 'toon-chain-provenance-json-sha256-v3'
STATE_SCHEMA_V5 = 'toon-state-v5'
STATE_SCHEMA_V6 = 'toon-state-v6'
STATE_SCHEMA_V7 = 'toon-state-v7'
STATE_SCHEMA_V8 = 'toon-state-v8'
FRAME_SCHEMA_V5 = 'toon-frame-report-v5'
FRAME_SCHEMA_V6 = 'toon-frame-report-v6'
FRAME_SCHEMA_V7 = 'toon-frame-report-v7'
CANONICALIZATION = 'toon-contract-json-v1'
_COUNTS = ('chain_count', 'source_fragment_count', 'sample_count', 'closed_chain_count')
_OMITTED = ('sources', 'fragments', 'objects_geometry', 'joins', 'classified_edges', 'discarded', 'chains')


def _fail(message, path):
    raise WorkflowError('VALIDATION_FAILED', message, path)


def _integer(value, path, nonnegative=True):
    if type(value) is not int or (nonnegative and value < 0):
        _fail('Expected a nonnegative integer' if nonnegative else 'Expected an integer', path)


def _sha(value, path):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        _fail('Expected a lowercase SHA-256', path)


def state_schema(manifest):
    versions = {'0.1.0': STATE_SCHEMA, '0.2.0': STATE_SCHEMA_V3, '0.3.0': STATE_SCHEMA_V4,
                **dict.fromkeys(contract.PLACED_VERSIONS, STATE_SCHEMA_V5),
                contract.REGION_VERSION: STATE_SCHEMA_V6, contract.ANIMATION_VERSION: STATE_SCHEMA_V7,
                contract.JITTER_VERSION: STATE_SCHEMA_V8}
    version = manifest['schema_version']
    if version not in versions: _fail('Unsupported manifest version for state', '$.manifest.schema_version')
    return versions[version]


def chain_schema(manifest):
    if manifest is None: return CHAIN_SCHEMA
    versions = {'0.1.0': CHAIN_SCHEMA, '0.2.0': CHAIN_SCHEMA_V2, '0.3.0': CHAIN_SCHEMA_V3,
                **dict.fromkeys(contract.PLACED_VERSIONS, CHAIN_SCHEMA_V3)}
    version = manifest['schema_version']
    if version not in versions: _fail('Unsupported manifest version for provenance', '$.manifest.schema_version')
    return versions[version]


def frame_schema(manifest=None):
    if manifest is None: return FRAME_SCHEMA
    versions = {'0.1.0': FRAME_SCHEMA, '0.2.0': FRAME_SCHEMA_V3, '0.3.0': FRAME_SCHEMA_V4,
                **dict.fromkeys(contract.PLACED_VERSIONS, FRAME_SCHEMA_V5), contract.ANIMATION_VERSION: FRAME_SCHEMA_V6,
                contract.JITTER_VERSION: FRAME_SCHEMA_V7}
    version = manifest['schema_version']
    if version not in versions: _fail('Unsupported manifest version for frame report', '$.manifest.schema_version')
    return versions[version]


def allowed_source_intervals(manifest, key, group_id):
    """Normalized0.3 physical-u mask/dash domain, never a geometry rebuild.

    Dash is ((u + phase_u) / period_u) % 1 < duty. Closed geometric interval
    endpoints may lie exactly on a dash transition; open interiors must remain
    inside one allowed band. Depth, clipping and zero width may remove more.
    """
    settings = manifest['lines']
    stroke = settings['stroke']
    if group_id != 'global':
        group = next((g for g in settings['groups'] if g['group_id'] == group_id and g['enabled']), None)
        if group is None: _fail('Unknown enabled source group', '$.chains.sources.group_id')
        stroke = group['style'].get('stroke', stroke)
    edit = next((e for e in settings['edge_overrides']
                 if [e['object_id'], e['mesh_id'], e['edge_id'], *e['point_ids']] == list(key)), None)
    if edit is not None and not edit['enabled']: return []
    masks = [(r['start'], r['end']) for r in edit['visible_intervals']] if edit is not None else [(0, 1)]
    dash = stroke['dash']
    if not dash['enabled'] or dash['duty'] == 1: return [list(r) for r in masks]
    if dash['duty'] == 0: return []
    period, phase, duty = dash['period_u'], dash['phase_u'], dash['duty']
    bands = []
    first = math.floor(phase / period) - 1
    last = math.ceil((1 + phase) / period) + 1
    for n in range(first, last + 1):
        start, end = n * period - phase, (n + duty) * period - phase
        for lo, hi in masks:
            left, right = max(0, lo, start), min(1, hi, end)
            if left < right: bands.append([left, right])
    return bands


def _validate_group_sources(chains, manifest):
    """Recompute class ownership from complete physical classes, never names."""
    settings = manifest['lines']
    selectors = {(oid, kind): group for group in settings['groups']
                 for oid in group['object_ids'] for kind in group['kinds']}
    targets = {t['object_id']: t['mesh_id'] for t in manifest['targets']}
    styled = manifest['schema_version'] in contract.STYLED_VERSIONS
    source_version = 'v3' if styled else 'v2'
    seen = {}
    allowed_cache = {}
    all_kinds = {'silhouette', 'crease', 'material_boundary', 'marked', 'boundary'}
    for chain in chains:
        if not chain['sources'] or chain['samples'] < (3 if chain['closed'] else 2):
            _fail('Nonempty ' + source_version + ' chain requires complete source and sample witnesses', '$.chains')
        owner = None
        for source in chain['sources']:
            fields = {'key', 'source_interval', 'reversed', 'classes', 'group_id', 'primary_kind', 'physical_classes'}
            if not isinstance(source, dict) or set(source) != fields:
                _fail('Invalid ' + source_version + ' source ownership record', '$.chains.sources')
            key = source['key']
            if (not isinstance(key, list) or len(key) != 5 or key[0] not in targets
                    or key[1] != targets[key[0]] or any(type(v) is not int or v <= 0 for v in key[2:])
                    or key[3] >= key[4] or (styled and any(v > 2147483647 for v in key[2:]))
                    or (not styled and tuple(key) in seen)):
                _fail('Invalid or duplicated physical source edge', '$.chains.sources.key')
            physical = source['physical_classes']
            if (not isinstance(physical, list) or not physical or any(k not in all_kinds for k in physical)
                    or len(set(physical)) != len(physical)):
                _fail('Invalid physical line classes', '$.chains.sources.physical_classes')
            remaining = [k for k in settings['kind_order'] if k in physical
                         and ((key[0], k) not in selectors or selectors[key[0], k]['enabled'])]
            if not remaining:
                _fail('Excluded edge was drawn', '$.chains.sources')
            primary = remaining[0]
            selected = selectors.get((key[0], primary))
            group_id = selected['group_id'] if selected else 'global'
            if (source['primary_kind'] != primary or source['classes'] != [primary]
                    or source['group_id'] != group_id or (owner is not None and owner != group_id)):
                _fail('Source class/group ownership differs from manifest', '$.chains.sources')
            owner = group_id
            interval = source['source_interval']
            if (type(source['reversed']) is not bool or not isinstance(interval, list) or len(interval) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in interval)
                    or not 0 <= interval[0] < interval[1] <= 1):
                _fail('Invalid canonical source interval', '$.chains.sources')
            prior = seen.setdefault(tuple(key), [])
            if styled:
                identity = {k: source[k] for k in ('classes', 'group_id', 'primary_kind', 'physical_classes')}
                if any(identity != old_identity or (interval[0] < old[1] and old[0] < interval[1])
                       for old, old_identity in prior):
                    _fail('Repeated physical source intervals overlap or change ownership', '$.chains.sources.source_interval')
                cache_key = (tuple(key), group_id)
                if cache_key not in allowed_cache:
                    allowed_cache[cache_key] = allowed_source_intervals(manifest, key, group_id)
                allowed = allowed_cache[cache_key]
                if not any(lo <= interval[0] < interval[1] <= hi for lo, hi in allowed):
                    _fail('Source interval is excluded by local override, mask or dash', '$.chains.sources.source_interval')
                prior.append((interval, identity))


def chain_fingerprint(chains, manifest=None):
    """Hash every field and order using contract.canonical, never a projection."""
    if not isinstance(chains, list):
        _fail('Expected ordered chain list', '$.chains')
    fragments = samples = closed = 0
    for index, chain in enumerate(chains):
        path = '$.chains[%d]' % index
        if not isinstance(chain, dict) or set(chain) != {'sources', 'closed', 'samples'}:
            _fail('Expected complete chain provenance record', path)
        if not isinstance(chain['sources'], list) or type(chain['closed']) is not bool:
            _fail('Expected ordered sources and boolean closed', path)
        _integer(chain['samples'], path + '.samples')
        fragments += len(chain['sources'])
        samples += chain['samples']
        closed += int(chain['closed'])
    try:
        digest = contract.signature(chains)
    except (TypeError, ValueError) as exc:
        _fail('Chain provenance is not canonical JSON: ' + str(exc), '$.chains')
    if contract.is_grouped(manifest):
        _validate_group_sources(chains, manifest)
    return {'schema': chain_schema(manifest), 'canonicalization': CANONICALIZATION,
            'sha256': digest, 'chain_count': len(chains),
            'source_fragment_count': fragments, 'sample_count': samples,
            'closed_chain_count': closed}


def validate_chain_fingerprint(value, path='$.chain_fingerprint', manifest=None):
    if not isinstance(value, dict) or set(value) != {'schema', 'canonicalization', 'sha256', *_COUNTS}:
        _fail('Invalid chain fingerprint fields', path)
    if value['schema'] != chain_schema(manifest) or value['canonicalization'] != CANONICALIZATION:
        _fail('Unsupported chain fingerprint schema or canonicalization', path)
    _sha(value['sha256'], path + '.sha256')
    for key in _COUNTS:
        _integer(value[key], path + '.' + key)
    if value['closed_chain_count'] > value['chain_count']:
        _fail('Closed chain count exceeds total chains', path)
    if value['chain_count'] == 0 and any(value[k] for k in _COUNTS):
        _fail('Empty chain fingerprint has nonzero counts', path)
    if value['chain_count'] == 0 and value['sha256'] != contract.signature([]):
        _fail('Empty chain fingerprint does not hash the empty list', path)


def frame_report(row, frame, manifest=None):
    """Return a small report without mutating or retaining raw chain lists."""
    report = {k: v for k, v in row.items() if k not in _OMITTED}
    report.update(frame_schema=frame_schema(manifest),
                  frame=frame, chain_fingerprint=chain_fingerprint(row.get('chains', []), manifest))
    validate_frame_report(report, frame, manifest)
    return report


def validate_frame_report(report, frame=None, manifest=None):
    path = '$.frame'
    required = {'frame_schema', 'frame', 'chain_fingerprint', 'enabled', 'objects',
                'object_names', 'geometry_sha256', 'counts', 'visibility', 'seconds', 'limits'}
    optional = {'camera', 'cpu_visibility', 'depth_bias', 'time_value'}
    grouped = contract.is_grouped(manifest)
    if grouped: required.add('groups')
    if contract.is_animated(manifest): required.add('shading_sample')
    if not isinstance(report, dict) or not required <= set(report) or set(report) - required - optional:
        _fail('Invalid frame report fields; raw provenance is not embedded', path)
    if report['frame_schema'] != frame_schema(manifest):
        _fail('Unsupported frame report schema', path + '.frame_schema')
    _integer(report['frame'], path + '.frame', False)
    if frame is not None and (type(frame) is not int or report['frame'] != frame):
        _fail('Frame report is outside the expected ordered frame domain', path + '.frame')
    if contract.is_animated(manifest):
        from . import animation_state
        animation_state.validate_sample(report['shading_sample'], report['frame'], manifest)
    if type(report['enabled']) is not bool:
        _fail('Expected boolean enabled', path + '.enabled')
    names = report['objects']
    if (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names)
            or len(set(names)) != len(names) or report['object_names'] != names):
        _fail('Frame object names must be unique matching ordered lists', path + '.objects')
    _sha(report['geometry_sha256'], path + '.geometry_sha256')
    placed = contract.is_placed(manifest)
    if report['visibility'] != ('grouped_native_placement' if placed else 'native_depth'):
        _fail('Unsupported frame visibility contract', path + '.visibility')
    seconds = report['seconds']
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        _fail('Expected finite nonnegative seconds', path + '.seconds')
    if not isinstance(report['limits'], list) or any(not isinstance(v, str) for v in report['limits']):
        _fail('Expected a list of limit descriptions', path + '.limits')
    counts = report['counts']
    keys = {'objects', 'vertices', 'faces', 'samples'}
    if report['enabled']:
        keys |= {'curve_points', 'splines', 'source_vertices', 'source_edges', 'fragments', 'chains'}
        if (set(report) != required | optional or not isinstance(report['camera'], dict)
                or report['cpu_visibility'] != 'not_called'
                or type(report['time_value']) not in (int, float)
                or report['time_value'] != report['frame']
                or type(report['depth_bias']) not in (int, float)
                or not math.isfinite(report['depth_bias']) or report['depth_bias'] < 0):
            _fail('Enabled frame lacks its native evaluation witnesses', path)
    elif set(report) != required:
        _fail('Disabled frame has unexpected evaluation fields', path)
    if not isinstance(counts, dict) or set(counts) != keys:
        _fail('Invalid frame count fields', path + '.counts')
    for key, value in counts.items():
        _integer(value, path + '.counts.' + key)
    if counts['objects'] != len(names):
        _fail('Object count differs from frame objects', path + '.counts.objects')
    if grouped:
        groups = report['groups']
        allowed = ['global'] + [g['group_id'] for g in manifest['lines']['groups'] if g['enabled']]
        group_fields = {'group_id', 'objects', 'placement'} if placed else {'group_id', 'objects'}
        if (not isinstance(groups, list) or any(not isinstance(g, dict) or set(g) != group_fields
                or g['group_id'] not in allowed or not isinstance(g['objects'], list)
                or len(g['objects']) > 1 for g in groups)):
            _fail('Invalid native group records', path + '.groups')
        if (len({g['group_id'] for g in groups}) != len(groups)
                or (report['enabled'] and [g['group_id'] for g in groups] != allowed)
                or [allowed.index(g['group_id']) for g in groups] != sorted(allowed.index(g['group_id']) for g in groups)
                or [n for g in groups for n in g['objects']] != names
                or (not report['enabled'] and groups)):
            _fail('Group/object ownership or order differs', path + '.groups')
        if placed:
            placements = {row['group_id']: row['placement'] for row in contract.effective_placements(manifest['lines'])}
            for index, group in enumerate(groups):
                contract.validate_placement(group['placement'], path + '.groups[%d].placement' % index)
                if group['placement'] != placements[group['group_id']]:
                    _fail('Native group placement differs from the effective manifest', path + '.groups[%d].placement' % index)
    fingerprint = report['chain_fingerprint']
    validate_chain_fingerprint(fingerprint, path + '.chain_fingerprint', manifest)
    for key, count in (('samples', 'sample_count'), ('chains', 'chain_count'), ('fragments', 'source_fragment_count')):
        if key in counts and counts[key] != fingerprint[count]:
            _fail('Frame counts differ from chain fingerprint', path + '.counts.' + key)
    if not report['enabled'] and (any(counts.values()) or any(fingerprint[k] for k in _COUNTS)):
        _fail('Disabled frame has nonempty geometry/provenance counts', path)


def validate_embedded_state(embedded, manifest=None):
    """Validate explicit versioned structure and frame/cache domain; no migration.

    This checks digest format and count consistency. Only the complete sidecar
    chains can independently reproduce a nonempty provenance digest.
    """
    path = '$.embedded_state'
    required = {'schema', 'manifest', 'materials', 'cache_records', 'frames', 'reference_frame'}
    if isinstance(embedded, dict) and contract.is_animated(embedded.get('manifest')):
        required.add('shading_animation')
    if not isinstance(embedded, dict) or set(embedded) != required:
        _fail('Invalid embedded state fields', path)
    stored_manifest = embedded['manifest']
    contract.normalize_manifest(stored_manifest)
    if embedded['schema'] != state_schema(stored_manifest):
        _fail('State schema does not match manifest; no implicit migration', path + '.schema')
    if manifest is not None and stored_manifest != manifest:
        _fail('Embedded manifest differs from the requested manifest', path + '.manifest')
    _integer(embedded['reference_frame'], path + '.reference_frame', False)
    if not isinstance(embedded['materials'], dict) or not isinstance(embedded['cache_records'], dict):
        _fail('Expected material and native cache record objects', path)
    if stored_manifest['schema_version'] in contract.REGION_VERSIONS:
        from . import regions
        regions.validate_snapshot(embedded['materials'], stored_manifest['targets'],
                                  stored_manifest['shading'], stored_manifest['engine'], stored_manifest['scope_id'])
    frames = embedded['frames']
    start, end = stored_manifest['time']['start'], stored_manifest['time']['end']
    if not isinstance(frames, list) or len(frames) != end - start + 1:
        _fail('Frame report count differs from manifest frame domain', path + '.frames')
    names = []
    for frame, report in zip(range(start, end + 1), frames):
        validate_frame_report(report, frame, stored_manifest)
        if report['enabled'] != stored_manifest['lines']['enabled']:
            _fail('Frame enablement differs from manifest', path + '.frames')
        names.extend(report['objects'])
    if len(set(names)) != len(names) or set(names) != set(embedded['cache_records']):
        _fail('Frame objects and native cache records differ', path + '.cache_records')
    if contract.is_animated(stored_manifest):
        from . import animation_state
        animation_state.validate_state(embedded['shading_animation'], stored_manifest, embedded['reference_frame'], frames, embedded['materials'])
    if contract.is_grouped(stored_manifest):
        version = ('v5' if contract.is_placed(stored_manifest) else
                   'v4' if stored_manifest['schema_version'] == '0.3.0' else 'v3')
        for name, record in embedded['cache_records'].items():
            if not isinstance(record, dict) or set(record) != {'record', 'data', 'action', 'ink'}:
                _fail(version + ' requires complete native Ink witness', path + '.cache_records.' + name)
            if not isinstance(record['ink'], list) or len(record['ink']) != 1 or not isinstance(record['ink'][0], dict):
                _fail(version + ' native carrier requires one complete Ink record', path + '.cache_records.' + name)
            ink = record['ink'][0]
            if (ink.get('type') != 'Material' or not {'name', 'nodes', 'material', 'custom_properties', 'material_configuration'} <= set(ink)
                    or any(not isinstance(ink[k], dict) for k in ('nodes', 'material', 'custom_properties', 'material_configuration'))):
                _fail(version + ' Ink record lacks its full identity, nodes or configuration', path + '.cache_records.' + name)
