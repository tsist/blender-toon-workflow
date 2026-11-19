# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python request construction for the public job runner; no bpy import."""
import hashlib
from pathlib import Path
from .joint_sampling import offsets, admit_lighting


def _descriptor(path):
    path = Path(path).resolve(strict=True)
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(file=str(path), expected_sha256=sha)


def code_inventory():
    from .joint_extension import code_inventory as inventory
    tools = Path(__file__).resolve().parent.parent
    return inventory(tools)


def build(source, scene, view_layer, *, resources, samples_per_axis=2,
          memory_budget_bytes=512*1024*1024, lighting_samples=64):
    """Use only for this user-authorized in-tree implementation.

    Resources are explicit even when the caller has established none exist.
    The adapter validates image closure again after opening its saved source.
    """
    offsets(samples_per_axis)
    admit_lighting(lighting_samples)
    if not isinstance(scene, str) or not scene or not isinstance(view_layer, str) or not view_layer:
        raise ValueError('Explicit scene and view layer names are required')
    if type(memory_budget_bytes) is not int or memory_budget_bytes <= 0:
        raise ValueError('A positive image memory budget is required')
    tools = Path(__file__).resolve().parent.parent
    implementation = code_inventory()
    declared = [_descriptor(path) for path in resources]
    source_ref = _descriptor(source)
    manifest = dict(script=_descriptor(Path(__file__).with_name('joint_extension.py')),
        trust='execute_local_python_without_sandbox',
        params=dict(mode='frame', source=source_ref, scene=scene, view_layer=view_layer,
            samples_per_axis=samples_per_axis, memory_budget_bytes=memory_budget_bytes,
            lighting_samples=lighting_samples,
            implementation=implementation, resources=declared),
        outputs=['output-toon-joint.json', 'output-toon-joint.png', 'output-toon-author.blend'])
    # Product code is held separately by joint_extension's fixed inventory.
    # User resources keep the unchanged public runner's resource contract.
    guarded = [source_ref, *declared]
    return dict(schema_version='1.0', command='extension.run', params=dict(manifest=manifest, resources=guarded))


def build_sequence(source, scene, view_layer, *, times, resources,
                   samples_per_axis=2, lighting_samples=64,
                   memory_budget_bytes=512*1024*1024):
    """One public job, ordered explicit times; no inferred frame range/resume."""
    from .joint_sequence import normalize_times
    normalize_times(times)
    request = build(source, scene, view_layer, resources=resources,
        samples_per_axis=samples_per_axis, lighting_samples=lighting_samples,
        memory_budget_bytes=memory_budget_bytes)
    params = request['params']['manifest']['params']
    params['mode'] = 'sequence'
    params['times'] = list(times)
    return request
