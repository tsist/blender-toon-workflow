# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted in-tree adapter for existing public blenderctl extension.run.

The public runner owns job status, cancellation, timeout and final acceptance.
This script only writes its new job's output directory. It is not a new
toon.run contract or a replacement for the planned dedicated interface.
"""
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys

IMPLEMENTATION_FILES = ('__init__.py', 'runtime_compat.py', 'band_boundary.py','band_boundary_witness.py','render_capture.py','shot_regions.py',
                        'lighting_53.py', 'lighting_53_ui.py', 'directional_face_53.py', 'native_surface_witness.py',
                        'line_optics.py','media_render.py','media_render_ui.py','style_family_v2.py',
                        'joint_sampling.py', 'joint_line_adapter.py', 'pixel_coverage.py', 'line_material_colors.py', 'line_material_ui.py', 'line_image_source.py', 'line_image_affine.py',
                        'joint_render.py', 'camera_correction.py', 'joint_extension.py',
                        'eye_overlay_nodes.py', 'eye_overlay_lines.py', 'eye_overlay_beauty.py', 'eye_overlays.py', 'hair_fixed_shadow.py', 'eye_overlay_ui.py',
                        'hair_map.py', 'hair_map_nodes.py', 'hair_map_witness.py', 'hair_map_ui.py', 'hair_highlight_mask.py',
                        'hair_sdf.py', 'hair_sdf_nodes.py', 'hair_sdf_compile.py', 'hair_sdf_authoring.py', 'hair_sdf_ui.py',
                        'joint_request.py', 'joint_sequence.py', 'joint_time.py', 'joint_inspect.py',
                        'joint_workflow.py', 'joint_basic_style.py', 'workflow_contract.py', 'joint_recovery.py', 'camera_shots.py',
                        'author_receive.py','workbench_identity.py','workbench_jobs.py',
                        'artistic_normals.py', 'normal_authoring.py', 'normal_witness.py', 'head_normals.py',
                        'face_thresholds.py', 'face_material.py', 'face_witness.py',
                        'face_sdf.py', 'face_sdf_polar.py', 'face_sdf_direction.py', 'face_sdf_continuity.py', 'face_direction_keys.py',
                        'sdf_filter.py', 'sdf_cast.py', 'sdf_ui.py', 'sdf_key_authoring.py', 'sdf_keys_core.py', 'sdf_witness.py', 'face_cast_art.py', 'cast_art_witness.py', 'cast_animation.py',
                        'component_catalog.py','component_slots.py','component_ui.py','style_components.py','media_components.py','style_set.py','omni_preview.py','workbench.py', 'component_witness.py','style_set_ui.py', 'manga_look.py','shot_look_ui.py','shot_look_output.py', 'surface_slots.py', 'surface_ui.py', 'cel_recipe.py', 'cel_recipe_v2.py', 'line_template.py', 'line_template_ink.py', 'line_template_ui.py', 'cel_lines.py', 'cel_ui.py', 'style_family.py', 'style_family_ui.py',
                        'finish_nodes.py', 'finish_animation.py', 'finish_core.py', 'finish_ui.py', 'finish_witness.py', 'finish_band.py', 'finish_band_mask.py', 'materials.py', 'templates.py', 'contract.py', 'backend.py')


def code_inventory(tools):
    paths = [*(tools/'toon_workflow_addon'/name for name in IMPLEMENTATION_FILES),
             *(p for p in (tools/'toon_viewport_preview').rglob('*.py')
               if '__pycache__' not in p.parts)]
    paths.extend(tools/'toon_workflow_cli_v1'/name for name in
                 ('cli.py','protocol.py','runner.py','worker.py','toon_workflow_public.py','deployment.py'))
    if (tools/'bundle.json').is_file():
        paths.extend((tools/'__init__.py', tools/'bundle.json'))
    return {p.relative_to(tools).as_posix():digest(p) for p in paths}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _domain_package():
    """Resolve this file's real package even for extension.run's file loader."""
    root = Path(__file__).resolve().parent.parent
    bundled = (root/'bundle.json').is_file()
    parent = root.parent if bundled else root
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))
    name = (root.name + '.' if bundled else '') + 'toon_workflow_addon'
    package = importlib.import_module(name)
    if not Path(package.__file__).resolve().is_relative_to(root/'toon_workflow_addon'):
        raise ValueError('Joint domain import resolved outside this deployment')
    return package


def run(params, job):
    import bpy
    tools = Path(__file__).resolve().parent.parent
    sys.dont_write_bytecode = True
    if type(params) is not dict or params.get('mode') not in {'frame', 'sequence'}:
        raise ValueError('Joint extension requires an explicit frame/sequence mode')
    expected = {'mode', 'source', 'scene', 'view_layer', 'samples_per_axis',
                'memory_budget_bytes', 'lighting_samples', 'implementation', 'resources'}
    if params['mode'] == 'sequence':
        expected.add('times')
    if set(params) != expected:
        raise ValueError('Joint extension requires its exact parameter contract')
    source = Path(params['source']['file'])
    if not source.is_absolute() or digest(source) != params['source']['expected_sha256']:
        raise ValueError('Joint source fingerprint differs')
    # The pinned entry script establishes the entire dependency inventory
    # with stdlib reads before executing any other product module.
    actual = code_inventory(tools)
    if actual != params['implementation']:
        raise ValueError('Joint implementation inventory changed')
    # Implementation files are a separate fixed domain inventory, not user
    # scene resources. Hold every source with the public runner's existing
    # guard; retain the public resource-list bound for actual source/assets.
    from contextlib import ExitStack
    from filesystem import FileGuard
    if len(actual) > 256:
        raise ValueError('Joint implementation exceeds its fixed inventory bound')
    with ExitStack() as stack:
        for name, sha in actual.items():
            guard = stack.enter_context(FileGuard(tools/name))
            if guard.sha256() != sha:
                raise ValueError('Joint source changed before acquiring its guard')
        return _execute(params, job, tools, source, actual)


def _execute(params, job, tools, source, actual):
    import bpy
    resources = {}
    for row in params['resources']:
        path = Path(row['file']).resolve()
        if digest(path) != row['expected_sha256']:
            raise ValueError('Joint resource changed: '+str(path))
        resources[path] = row['expected_sha256']
    package = _domain_package()
    pencil = importlib.import_module(package.__name__+'.backend').pencil()
    pencil.register()
    # Saved PointerProperty data is hidden without its RNA schema, even though
    # user_map still reports the Scene as an image consumer. Restore the same
    # owned authoring schema before loading; do not waive image exclusivity.
    importlib.import_module(package.__name__+'.sdf_ui').register()
    for name in ('component_ui','style_set_ui','shot_look_ui','media_render_ui'):
        importlib.import_module(package.__name__+'.'+name).register()
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    scene = bpy.data.scenes[params['scene']]
    layer = scene.view_layers[params['view_layer']]
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    # Unknown external image dependencies cannot be advertised as closed.
    for image in bpy.data.images:
        if image.source in {'GENERATED', 'VIEWER'} or image.packed_file:
            continue
        if image.source != 'FILE':
            raise ValueError('Sequence/UDIM image dependency requires a dedicated adapter')
        if image.filepath:
            path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
            if path not in resources:
                raise ValueError('Undeclared external image dependency: '+str(path))
    if bpy.data.libraries:
        raise ValueError('Linked libraries require a separately verified joint dependency adapter')
    joint_render, joint_sequence, joint_sampling = (
        importlib.import_module(package.__name__+'.'+name)
        for name in ('joint_render', 'joint_sequence', 'joint_sampling'))
    joint_sampling.admit_lighting(params['lighting_samples'])
    if params['mode'] == 'sequence':
        report = joint_sequence.render_sequence(source, params['scene'], params['view_layer'],
            job/'sequence-output', params['times'], samples_per_axis=params['samples_per_axis'],
            memory_budget_bytes=params['memory_budget_bytes'],
            render_options=dict(lighting_samples=params['lighting_samples']),
            implementation=actual, expected_source_sha256=params['source']['expected_sha256'],
            resource_bindings=params['resources'])
        joint_sequence.inspect_sequence(job/'sequence-output')
        final_index = report['frame_count']-1
        preview = job/'sequence-output'/('frame-%03d' % final_index)/'joint-output'/'Combined.png'
        author = job/'sequence-output/frame-000/joint-output/author.blend'
        source_unchanged = True
    else:
        report = joint_render.render(scene, layer, job/'joint-output',
            samples_per_axis=params['samples_per_axis'], memory_budget_bytes=params['memory_budget_bytes'],
            lighting_samples=params['lighting_samples'])
        preview = job/'joint-output/Combined.png'
        author = job/'joint-output/author.blend'
        source_unchanged = report['source']['unchanged']
    if any(digest(tools/name) != sha for name, sha in actual.items()):
        raise ValueError('Implementation changed during joint rendering')
    report['implementation'] = actual
    report['resources'] = params['resources']
    report['public_entry'] = 'blenderctl extension.run / in-tree joint adapter'
    report['implementation_guard'] = 'fixed implementation inventory; every file held until render completes'
    report['mode'] = params['mode']
    report['preview_policy'] = 'last requested time' if params['mode'] == 'sequence' else 'requested frame'
    report['author_checkpoint_policy'] = 'first requested time' if params['mode'] == 'sequence' else 'requested frame'
    (job/'output-toon-joint.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    for source_path, name in ((preview, 'output-toon-joint.png'), (author, 'output-toon-author.blend')):
        with source_path.open('rb') as src, (job/name).open('xb') as dst:
            shutil.copyfileobj(src, dst)
    return dict(profile=report['profile'], experimental=True,
                report=str(job/'output-toon-joint.json'), output=str(job/'output-toon-joint.png'),
                source_unchanged=source_unchanged, mode=params['mode'])


if __name__ == '__main__':
    result = run(parameters, Path(job_directory))
