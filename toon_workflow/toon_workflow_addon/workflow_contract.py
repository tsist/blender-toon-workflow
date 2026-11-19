# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed disk-only contract for the mother's explicit 2D workflow domain."""
import copy
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'toon-workflow-manifest.v2'
LEGACY_SCHEMA = 'toon-workflow-manifest.v1'
STATE = 'toon-workflow-state.v1'
OPERATIONS = ('run', 'inspect', 'edit', 'recover')


def manifest_time_policy(value):
    """The manifest identity selects a clock; historical requests keep theirs."""
    from .joint_time import LEGACY_TIME_POLICY, NATIVE_TIME_POLICY
    schema = value.get('schema') if type(value) is dict else None
    if schema == SCHEMA:
        return NATIVE_TIME_POLICY
    if schema == LEGACY_SCHEMA:
        return LEGACY_TIME_POLICY
    raise ValueError('Unknown explicit 2D workflow manifest time contract')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def descriptor(value):
    if (type(value) is not dict or set(value) != {'file', 'expected_sha256'}
            or type(value['file']) is not str or not Path(value['file']).is_absolute()
            or not re.fullmatch('[0-9a-f]{64}', str(value['expected_sha256']))):
        raise ValueError('Expected an absolute file/expected_sha256 descriptor')
    return dict(value)


def manifest(value):
    from .joint_sampling import offsets, admit_lighting
    from .joint_sequence import normalize_times
    expected = {'schema', 'scene', 'view_layer', 'times', 'samples_per_axis',
                'lighting_samples', 'memory_budget_bytes'}
    if (type(value) is not dict or set(value) != expected
            or value['schema'] not in {SCHEMA, LEGACY_SCHEMA}):
        raise ValueError('Expected the explicit 2D toon-workflow manifest; legacy geometry states are not migrated')
    for key in ('scene', 'view_layer'):
        if type(value[key]) is not str or not 1 <= len(value[key]) <= 256:
            raise ValueError('Explicit scene/view_layer names are required')
    normalize_times(value['times'], time_policy=manifest_time_policy(value))
    offsets(value['samples_per_axis']); admit_lighting(value['lighting_samples'])
    if type(value['memory_budget_bytes']) is not int or value['memory_budget_bytes'] <= 0:
        raise ValueError('A positive image memory budget is required')
    return copy.deepcopy(value)


def normalize(operation, value):
    if operation not in OPERATIONS or type(value) is not dict:
        raise ValueError('Unknown toon-workflow operation or non-object parameters')
    if operation == 'inspect' and 'state' in value:
        if set(value) != {'state', 'implementation'}:
            raise ValueError('State inspection requires state and implementation')
        result = dict(state=descriptor(value['state']), implementation=value['implementation'])
    elif operation == 'recover':
        if set(value) != {'recovery', 'implementation'}:
            raise ValueError('Recovery requires the original persisted request and implementation')
        result = dict(recovery=descriptor(value['recovery']), implementation=value['implementation'])
    else:
        common = {'file', 'expected_sha256', 'resources', 'implementation', 'manifest'}
        required = common | ({'style', 'expected_author_sha256'} if operation == 'edit' else set())
        if set(value) != required:
            raise ValueError('Unknown or missing toon-workflow parameters')
        descriptor({k: value[k] for k in ('file', 'expected_sha256')})
        if type(value['resources']) is not list or len(value['resources']) > 100:
            raise ValueError('Declare at most100 user resource descriptors')
        resources = [descriptor(row) for row in value['resources']]
        names = [str(Path(row['file']).resolve()).casefold() for row in resources]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate user resources')
        result = copy.deepcopy(value)
        result['resources'] = resources; result['manifest'] = manifest(value['manifest'])
        if operation == 'edit':
            result['style'] = descriptor(value['style'])
            if not re.fullmatch('[0-9a-f]{64}', str(value['expected_author_sha256'])):
                raise ValueError('An explicit captured author fingerprint is required for editing')
    inventory = result['implementation']
    if (type(inventory) is not dict or not 1 <= len(inventory) <= 256
            or any(type(key) is not str or Path(key).is_absolute() or '..' in Path(key).parts
                   or not re.fullmatch('[0-9a-f]{64}', str(sha)) for key, sha in inventory.items())):
        raise ValueError('Invalid fixed implementation inventory')
    return result


def request(source, scene, view_layer, times, *, resources, samples_per_axis=2,
            lighting_samples=64, memory_budget_bytes=512*1024*1024):
    from .joint_request import _descriptor, code_inventory
    return dict(schema_version='1.0', command='toon-workflow.run', params=dict(
        **_descriptor(source), resources=[_descriptor(p) for p in resources],
        implementation=code_inventory(), manifest=dict(schema=SCHEMA,scene=scene,
        view_layer=view_layer,times=list(times),samples_per_axis=samples_per_axis,
        lighting_samples=lighting_samples,memory_budget_bytes=memory_budget_bytes)))
