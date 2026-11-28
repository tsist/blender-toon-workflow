# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental ordered, fresh-source sequence inside one public worker.

The public extension.run supervisor owns cancellation, timeout and job state.
This module starts no process, registers no Pencil classes and supports no
cross-job resume. Callers register Pencil and guard declared source/resources
and the implementation closure before entering this API. A frame marker is
an integrity receipt, not independent temporal or artistic qualification.
"""
import hashlib
import inspect as _inspect
import json
import math
from pathlib import Path
from . import joint_time

LEGACY_TIME_POLICY = joint_time.LEGACY_TIME_POLICY
NATIVE_TIME_POLICY = joint_time.NATIVE_TIME_POLICY
LEGACY_PROFILE = joint_time.LEGACY_PROFILE
PROFILE = joint_time.PROFILE
MAX_TIMES = joint_time.MAX_TIMES
_MAX_JSON = 8 * 1024 * 1024


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def _fingerprint(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def normalize_times(times, *, time_policy=NATIVE_TIME_POLICY):
    """Compatibility entry point for the centralized exact time policies."""
    return joint_time.normalize_times(times, time_policy=time_policy)


def _validate_normalized(rows, *, time_policy=NATIVE_TIME_POLICY):
    return joint_time.validate_normalized(rows, time_policy=time_policy)


def _write_new(path, value):
    """Publish a complete JSON file; never replace an existing receipt."""
    path = Path(path)
    if path.exists():
        raise ValueError('Refusing to overwrite sequence output: ' + str(path))
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(_json_bytes(value))
        stream.flush()
    temporary.rename(path)


def _reference(path):
    return dict(path=str(path), sha256=_digest(path))


def _inventory():
    from .joint_extension import code_inventory
    tools = Path(__file__).resolve().parent.parent
    rows = code_inventory(tools)
    for name in ('joint_sequence.py', 'joint_inspect.py'):
        path = Path(__file__).with_name(name)
        rows[path.relative_to(tools).as_posix()] = _digest(path)
    return rows


def _options(renderer, render_options):
    if render_options is None:
        return {}
    if type(render_options) is not dict:
        raise ValueError('render_options must be a plain JSON object')
    parameters = _inspect.signature(renderer).parameters
    allowed = {name for name, parameter in parameters.items()
               if parameter.kind == parameter.KEYWORD_ONLY
               and name not in {'samples_per_axis', 'memory_budget_bytes', 'progress'}}
    if set(render_options) - allowed:
        raise ValueError('Unsupported sequence render_options: ' +
                         ', '.join(sorted(set(render_options) - allowed)))
    # Copy JSON values, reject objects/non-finite numbers before any rendering.
    return json.loads(_json_bytes(render_options))


def _joint_time_camera(report, requested, camera_name, source):
    frame = report['frame']
    if (report.get('status') != 'completed'
            or report.get('schema') not in {'toon-joint-report.v1', 'toon-joint-report.v2', 'toon-joint-report.v3', 'toon-joint-report.v4', 'toon-joint-report.v5', 'toon-joint-report.v6', 'toon-native-media-artline-report.v1'}
            or frame.get('evaluation') != 'RENDER'
            or type(frame.get('frame')) is not int
            or frame['frame'] != requested['frame']
            or type(frame.get('frame_subframe')) not in (int, float)
            or not math.isfinite(frame['frame_subframe'])
            or frame.get('frame_subframe') != requested['subframe']
            or frame.get('camera_name') != camera_name
            or report.get('source', {}).get('sha256') != source['sha256']
            or not report.get('source', {}).get('unchanged')
            or Path(report['source']['file']).resolve() != Path(source['file']).resolve()):
        raise ValueError('Joint report time/camera/source differs from sequence admission')
    for correction in report.get('camera_corrections', []):
        if (correction.get('status') != 'valid'
                or (correction.get('strength', 1.0) != 0.0 and correction.get('camera') != camera_name)):
            raise ValueError('Correction differs from the effective source shot camera')


def _joint_settings(report, settings):
    if 'blender' in settings and report.get('blender') != settings['blender']:
        raise ValueError('Joint frame was rendered by a different Blender build')
    admission = report['admission']
    axis = settings['samples_per_axis']
    if (admission['samples_per_axis'] != axis
            or admission['sample_count'] != axis * axis
            or admission['budget_bytes'] != settings['memory_budget_bytes']):
        raise ValueError('Joint sampling/budget differs from the sequence request')
    if report['schema'] in {'toon-joint-report.v2','toon-joint-report.v3','toon-joint-report.v4','toon-joint-report.v5','toon-joint-report.v6','toon-native-media-artline-report.v1'}:
        expected = settings['render_options'].get('lighting_samples', 64)
        if (report['lighting']['samples_per_position'] != expected
                or admission['beauty_samples_per_position'] != expected):
            raise ValueError('Joint lighting differs from the sequence request')
    elif 'lighting_samples' in settings['render_options']:
        raise ValueError('Legacy joint reports cannot qualify explicit lighting samples')


def render_sequence(source_path, scene_name, view_layer_name, output_directory,
                    times, *, samples_per_axis=2,
                    memory_budget_bytes=512*1024*1024, render_options=None,
                    implementation=None, expected_source_sha256=None, progress=None,
                    reuse_from=None, resource_bindings=None,
                    time_policy=NATIVE_TIME_POLICY):
    """Render ordered times, reopening the same saved author before each frame.

    Must be called in the existing isolated extension.run Blender worker.
    ``render_options`` only admits actual explicit keyword-only parameters of
    joint_render.render (e.g. lighting_samples when supplied by that version).
    Output is a new directory; partial output has no sequence complete marker.
    Cancellation propagates to the public supervisor. Resume is unsupported.
    """
    import bpy
    from . import joint_render, joint_inspect, joint_sampling
    if not bpy.app.background:
        raise ValueError('Sequence rendering requires an isolated background worker')
    if (not isinstance(scene_name, str) or not scene_name
            or not isinstance(view_layer_name, str) or not view_layer_name):
        raise ValueError('Explicit scene/view-layer names are required')
    normalized = normalize_times(times, time_policy=time_policy)
    profile = joint_time.profile_for_policy(time_policy)
    joint_sampling.offsets(samples_per_axis)
    if type(memory_budget_bytes) is not int or memory_budget_bytes <= 0:
        raise ValueError('A positive image memory budget is required')
    options = _options(joint_render.render, render_options)
    source = Path(source_path)
    root = Path(output_directory)
    if not source.is_absolute() or not source.is_file():
        raise ValueError('An absolute saved author source is required')
    source = source.resolve(strict=True)
    if not root.is_absolute() or root.exists() or not root.parent.is_dir():
        raise ValueError('Choose a new absolute sequence directory in an existing parent')
    root = root.resolve()
    source_ref = dict(file=str(source), sha256=_digest(source))
    if expected_source_sha256 is not None and source_ref['sha256'] != expected_source_sha256:
        raise ValueError('Sequence source fingerprint differs')
    actual_implementation = _inventory()
    if implementation is not None and implementation != actual_implementation:
        raise ValueError('Sequence implementation inventory differs')
    settings = dict(profile=profile, source=source_ref, scene=scene_name,
        blender=dict(version=bpy.app.version_string, build_hash=bpy.app.build_hash.decode()),
        view_layer=view_layer_name, times=normalized, samples_per_axis=samples_per_axis,
        memory_budget_bytes=memory_budget_bytes, render_options=options,
        implementation=actual_implementation, time_policy=time_policy,
        camera_policy='actual RENDER shot; reject nonzero active correction mismatch', resume_supported=False)
    if time_policy == NATIVE_TIME_POLICY:
        settings['requested_times'] = json.loads(_json_bytes(list(times)))
    joint_time.validate_settings_times(settings)
    settings['resource_bindings'] = json.loads(_json_bytes(resource_bindings or []))
    fingerprint = _fingerprint(settings)
    root.mkdir()
    _write_new(root/'request.json', settings)
    completed = []
    progress = progress or (lambda *args, **kwargs: None)
    try:
        if reuse_from is not None:
            from .joint_recovery import copy_completed_frames
            completed = copy_completed_frames(reuse_from, root, settings)
        reused_frames = len(completed)
        for index, requested in enumerate(normalized):
            if index < reused_frames:
                progress('REUSED_FRAME', index=index, total=len(normalized), time=requested)
                continue
            if _digest(source) != source_ref['sha256'] or _inventory() != actual_implementation:
                raise ValueError('Sequence source or implementation changed before fresh open')
            if bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False) != {'FINISHED'}:
                raise RuntimeError('Fresh sequence source open did not finish')
            scene = bpy.data.scenes[scene_name]
            layer = scene.view_layers[view_layer_name]
            window = bpy.context.window
            if window is None:
                windows = list(bpy.context.window_manager.windows)
                if len(windows) != 1:
                    raise ValueError('Cannot identify one owned background window')
                window = windows[0]
            window.scene, window.view_layer = scene, layer
            bound = scene.bl_rna.properties['frame_current']
            arguments = joint_time.setter_arguments(requested, time_policy=time_policy)
            if not bound.hard_min <= arguments['frame'] <= bound.hard_max:
                raise ValueError('Time exceeds the installed Blender frame bounds')
            frame_root = root/('frame-%03d' % index)
            frame_root.mkdir()
            with bpy.context.temp_override(window=window, scene=scene, view_layer=layer):
                scene.frame_set(arguments['frame'], subframe=arguments['subframe'])
                layer.update()
                observed = dict(frame=int(scene.frame_current), subframe=float(scene.frame_subframe))
                if observed != requested:
                    raise ValueError('Blender did not set the normalized sequence time exactly')
                if scene.camera is None:
                    raise ValueError('Source shot has no effective camera at sequence time')
                camera_name = scene.camera.name
                # Actual RENDER admission owns correction checks. A scene-wide
                # precheck would reject disabled or excluded modifiers and use
                # the wrong evaluation context for animated fields.
                markers = [dict(name=marker.name, frame=int(marker.frame), camera=marker.camera.name)
                           for marker in scene.timeline_markers if marker.camera is not None]
                progress('SEQUENCE_FRAME', index=index, total=len(normalized), time=requested)
                def frame_progress(stage, **detail):
                    progress('SEQUENCE_RENDER', frame_index=index, frame_total=len(normalized),
                             render_stage=stage, detail=detail)
                report = joint_render.render(scene, layer, frame_root/'joint-output',
                    samples_per_axis=samples_per_axis, memory_budget_bytes=memory_budget_bytes,
                    progress=frame_progress, **options)
            _joint_time_camera(report, requested, camera_name, source_ref)
            _joint_settings(report, settings)
            report_path = frame_root/'joint-output'/'report.json'
            if _json_bytes(json.loads(report_path.read_text(encoding='utf8'))) != _json_bytes(report):
                raise ValueError('Returned joint report differs from its saved report')
            checked = joint_inspect.inspect(report_path)
            if checked.get('status') != 'pass':
                raise ValueError('Joint output did not pass integrity inspection')
            if _digest(source) != source_ref['sha256'] or _inventory() != actual_implementation:
                raise ValueError('Sequence source or implementation changed during frame rendering')
            frame_report = dict(schema='toon-sequence-frame-report.v1', profile=profile,
                experimental=True, status='completed', index=index, time=requested,
                camera=camera_name, source=source_ref, settings_sha256=fingerprint,
                implementation_sha256=_fingerprint(actual_implementation), source_camera_markers=markers,
                joint_report=_reference(report_path), joint_inspection=checked,
                artifacts=report['artifacts'], author=dict(path=report['author_checkpoint'],
                                                          sha256=report['author_sha256']))
            _write_new(frame_root/'frame-report.json', frame_report)
            marker = dict(schema='toon-sequence-frame-complete.v1', status='completed',
                index=index, time=requested, camera=camera_name, source_sha256=source_ref['sha256'],
                settings_sha256=fingerprint, implementation_sha256=_fingerprint(actual_implementation),
                frame_report=_reference(frame_root/'frame-report.json'))
            _write_new(frame_root/'complete.json', marker)
            completed.append(_reference(frame_root/'complete.json'))
        sequence_report = dict(schema='toon-sequence-report.v1', profile=profile,
            experimental=True, status='completed', settings=settings, settings_sha256=fingerprint,
            frames=completed, frame_count=len(completed), resume_supported=False,
            claim='ordered output integrity only; temporal and artistic acceptance are separate')
        sequence_report['reused_frames'] = reused_frames
        _write_new(root/'sequence-report.json', sequence_report)
        _write_new(root/'complete.json', dict(schema='toon-sequence-complete.v1', status='completed',
            settings_sha256=fingerprint, source_sha256=source_ref['sha256'], frame_count=len(completed),
            sequence_report=_reference(root/'sequence-report.json')))
        return sequence_report
    except Exception as error:
        _write_new(root/'failure.json', dict(schema='toon-sequence-failure.v1', status='failed',
            completed_frames=len(completed), requested_frames=len(normalized),
            settings_sha256=fingerprint, error_type=type(error).__name__, error=str(error)))
        raise


def _read(path):
    path = Path(path)
    if path.stat().st_size > _MAX_JSON:
        raise ValueError('Sequence JSON exceeds the cold-reader budget')
    def reject(value):
        raise ValueError('Non-finite JSON value: ' + value)
    return json.loads(path.read_text(encoding='utf8'), parse_constant=reject)


def _checked_reference(reference, root):
    if type(reference) is not dict or set(reference) != {'path', 'sha256'}:
        raise ValueError('Invalid sequence file reference')
    path = Path(reference['path'])
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('Sequence reference must be an absolute ordinary file')
    path = path.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or _digest(path) != reference['sha256']:
        raise ValueError('Sequence referenced file escaped or changed')
    return path


def inspect_sequence(output_directory):
    """Cold disk-only read; invoke joint_inspect independently for every frame.

    Requires the final complete marker. Does not resume, rewrite, render or
    infer missing success. Source files need not still exist; their historical
    SHA bindings are checked against all frame receipts and joint reports.
    """
    from . import joint_inspect
    root = Path(output_directory).resolve(strict=True)
    marker = _read(root/'complete.json')
    if marker.get('schema') != 'toon-sequence-complete.v1' or marker.get('status') != 'completed':
        raise ValueError('Expected a completed sequence marker')
    report_path = _checked_reference(marker['sequence_report'], root)
    if report_path != root/'sequence-report.json':
        raise ValueError('Unexpected sequence report location')
    report = _read(report_path)
    if (report.get('schema') != 'toon-sequence-report.v1' or report.get('profile') not in {LEGACY_PROFILE, PROFILE}
            or report.get('status') != 'completed' or report.get('resume_supported') is not False):
        raise ValueError('Expected a completed experimental sequence report')
    settings = report['settings']
    profile = report['profile']
    time_policy = joint_time.validate_settings_times(settings, allow_legacy_missing=True)
    fingerprint = _fingerprint(settings)
    if (fingerprint != report['settings_sha256'] or fingerprint != marker['settings_sha256']
            or settings['profile'] != profile or settings['resume_supported'] is not False
            or settings['source']['sha256'] != marker['source_sha256']
            or _json_bytes(_read(root/'request.json')) != _json_bytes(settings)):
        raise ValueError('Sequence settings/source binding differs')
    times = settings['times']
    _validate_normalized(times, time_policy=time_policy)
    if (type(report.get('frame_count')) is not int or type(marker.get('frame_count')) is not int
            or not len(report['frames']) == len(times) == report['frame_count'] == marker['frame_count']):
        raise ValueError('Sequence is incomplete or frame counts differ')
    implementation_sha = _fingerprint(settings['implementation'])
    inspected = []
    for index, (reference, requested) in enumerate(zip(report['frames'], times)):
        frame_root = root/('frame-%03d' % index)
        frame_marker_path = _checked_reference(reference, frame_root)
        if frame_marker_path != frame_root/'complete.json':
            raise ValueError('Frame order/path differs')
        frame_marker = _read(frame_marker_path)
        if frame_marker.get('schema') != 'toon-sequence-frame-complete.v1':
            raise ValueError('Invalid frame complete marker')
        frame_report_path = _checked_reference(frame_marker['frame_report'], frame_root)
        if frame_report_path != frame_root/'frame-report.json':
            raise ValueError('Unexpected frame report path')
        frame_report = _read(frame_report_path)
        if frame_report.get('schema') != 'toon-sequence-frame-report.v1' or frame_report.get('profile') != profile:
            raise ValueError('Invalid frame report')
        for record in (frame_marker, frame_report):
            if (record.get('status') != 'completed' or type(record.get('index')) is not int
                    or record.get('index') != index
                    or _json_bytes(record.get('time')) != _json_bytes(requested)
                    or record.get('settings_sha256') != fingerprint
                    or record.get('implementation_sha256') != implementation_sha):
                raise ValueError('Frame time/order/settings/implementation binding differs')
        if (frame_marker['camera'] != frame_report['camera']
                or frame_marker['source_sha256'] != settings['source']['sha256']
                or frame_report['source'] != settings['source']):
            raise ValueError('Frame camera/source binding differs')
        joint_path = _checked_reference(frame_report['joint_report'], frame_root)
        if joint_path != frame_root/'joint-output'/'report.json':
            raise ValueError('Unexpected joint report path')
        joint = _read(joint_path)
        _joint_time_camera(joint, requested, frame_report['camera'], settings['source'])
        _joint_settings(joint, settings)
        if frame_report['artifacts'] != joint['artifacts'] or frame_report['author'] != dict(
                path=joint['author_checkpoint'], sha256=joint['author_sha256']):
            raise ValueError('Frame artifact bindings differ from joint output')
        for artifact in [frame_report['author'], *frame_report['artifacts'].values()]:
            _checked_reference(dict(path=artifact['path'], sha256=artifact['sha256']), frame_root)
        checked = joint_inspect.inspect(joint_path)
        if checked.get('status') != 'pass':
            raise ValueError('Cold joint integrity inspection failed')
        inspected.append(dict(index=index, time=requested, camera=frame_report['camera'], inspection=checked))
    return dict(status='pass', profile=profile, experimental=True, frame_count=len(inspected),
        settings_sha256=fingerprint, frames=inspected, resume_supported=False,
        claim='cold output integrity and time/camera/order bindings only; temporal appearance is unqualified')
