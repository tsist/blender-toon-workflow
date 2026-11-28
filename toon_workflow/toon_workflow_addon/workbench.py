# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Blender workbench using the existing public job runner.

Imported only by the package's lazy register entry. The UI owns snapshots,
request/log directories and a local status-file reader; the public CLI owns
render processes, timeout, cancellation and final acceptance. Disabling this
workbench never terminates an active public job.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty)

from . import camera_correction, camera_shots, joint_ownership, joint_request, joint_sequence
from .backend import pencil as pencil_backend
from .._ui_kit import ensure_object_mode, get_theme

DOMAIN = 'toon'
THEME = get_theme(DOMAIN)

PROP = 'toon_workbench'
_registered = False
_PENCIL_LEASE = object()
_descriptor = None
_processes = {}
_classes_registered = []
_TERMINAL = {'succeeded', 'failed', 'cancelled', 'canceled', 'timed_out', 'abandoned'}


def _write(path, value):
    temporary=path.with_name('.w-'+uuid.uuid4().hex[:12]+'.tmp')
    try:
        with temporary.open('x',encoding='utf8') as stream:
            stream.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _project_root():
    settings = getattr(bpy.context.scene, PROP, None)
    explicit = getattr(settings, 'project_directory', '') if settings else ''
    if explicit:
        root = Path(bpy.path.abspath(explicit))
        if not root.is_absolute() or not (root/'blenderctl.ps1').is_file():
            raise ValueError('公共工作目录须包含 blenderctl.ps1')
        return root.resolve(strict=True)
    roots = (Path(__file__).resolve().parents[2], *Path(bpy.app.binary_path).resolve().parents)
    for root in roots:
        if (root/'blenderctl.ps1').is_file():
            return root
    raise ValueError('请在输出进阶设置中选择工程目录')


def _submission_root(record):
    # Older in-tree submissions did not store this field. New submissions bind
    # the execution directory so switching scenes cannot retarget their runner.
    if 'project_root' not in record:
        return _project_root()
    root = Path(record['project_root'])
    if not root.is_absolute() or not (root/'blenderctl.ps1').is_file():
        raise ValueError('提交绑定的公共工作目录已失效')
    return root.resolve(strict=True)


def collect_resources():
    """Explicitly collect native file dependencies; reject unsupported closure."""
    if bpy.data.libraries:
        raise ValueError('当前联合配置尚不支持链接库，请使用独立本地工作副本')
    resources = set()
    for image in bpy.data.images:
        if image.source in {'GENERATED', 'VIEWER'} or image.packed_file:
            continue
        if image.source != 'FILE':
            raise ValueError('尚不支持序列／UDIM外部图像：' + image.name)
        if not image.filepath:
            raise ValueError('外部图像没有文件路径：' + image.name)
    # Blender's native dependency path inventory covers images, sounds, fonts,
    # movie clips and native file-path fields. Directories/dynamic placeholders
    # are deliberately not advertised as a closed single-file dependency.
    from .joint_workflow import runtime_paths
    for path in runtime_paths():
        if not path.is_absolute() or not path.is_file():
            raise ValueError('依赖缺失或需要专用适配：' + str(path))
        resources.add(str(path.resolve(strict=True)))
    for image in bpy.data.images:
        if image.source == 'FILE' and not image.packed_file:
            if not bpy.data.filepath and image.filepath.startswith('//'):
                raise ValueError('未保存工程中的相对图像路径需要先明确为绝对路径')
            path = Path(bpy.path.abspath(image.filepath, library=image.library))
            if not path.is_absolute() or not path.is_file():
                raise ValueError('外部图像缺失：' + str(path))
            resources.add(str(path.resolve(strict=True)))
    return sorted(resources)


def _synchronize_author_curves(scene):
    """Fail before saving when the native editable curve cannot be committed."""
    settings = scene.toon_viewport_preview
    if settings.curve_tree is None:
        return
    if settings.curve_error:
        raise ValueError('作者曲线尚有错误：' + settings.curve_error)
    curve_editor=pencil_backend('curve_editor')
    curve_editor.synchronize(scene)
    if settings.curve_error:
        raise ValueError('作者曲线提交失败：' + settings.curve_error)


def _prepare(context, operation='run', *, activate=True):
    scene = context.scene
    settings = getattr(scene, PROP)
    refresh_scene(scene)
    if settings.job_state in {'submitting', 'submitted', 'queued', 'running'}:
        raise ValueError('当前工作台已有进行中的公共作业，请先完成或取消')
    output = Path(bpy.path.abspath(settings.output_directory))
    if not output.is_absolute() or not output.is_dir():
        raise ValueError('请选择一个已经存在的绝对输出目录')
    if context.mode != 'OBJECT':
        raise ValueError('请先退出编辑／雕刻模式，再保存联合渲染快照')
    if scene.render.engine != 'BLENDER_EEVEE':
            raise ValueError('线色联合渲染需要 EEVEE')
    if scene.camera is None:
        raise ValueError('请指定场景相机')
    mode = settings.render_mode
    if mode not in {'FRAME', 'SEQUENCE'}:
        raise ValueError('请选择单帧或序列渲染模式')
    times = None
    if mode == 'SEQUENCE':
        try:
            times = json.loads(settings.sequence_times)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError('时间数组应为有效 JSON，例如 [1, 6.5, 12]') from error
        if type(times) is not list:
            raise ValueError('时间数组应为非空 JSON 数字数组，例如 [1, 6.5, 12]')
    else:
        times = [scene.frame_current + scene.frame_subframe]
    try:
        effective_times = joint_sequence.normalize_times(times)
    except ValueError as error:
        raise ValueError('时间数组无效：' + str(error)) from error
    _synchronize_author_curves(scene)
    from . import style_family_ui
    if hasattr(scene,style_family_ui.PROP) and style_family_ui.settings(context).receipt:
        style_family_ui.inspect_current(context)
    from .joint_workflow import admit_legacy, capture_author
    from .workflow_contract import fingerprint
    admit_legacy(scene)
    if operation not in {'run','edit','inspect'}:
        raise ValueError('未知工作台操作')
    style_path = None
    if operation == 'edit':
        style_path = Path(bpy.path.abspath(settings.style_file))
        if not style_path.is_absolute() or not style_path.is_file() or style_path.suffix.lower() != '.json':
            raise ValueError('请选择已有的完整作者样式 JSON 文件')
    author_fingerprint = fingerprint(capture_author(scene))
    resources = collect_resources()
    project_root = _project_root()
    token = uuid.uuid4().hex
    directory = output/('toon-joint-' + token)
    directory.mkdir()
    jobs = directory/'jobs'
    jobs.mkdir()
    before_path = bpy.data.filepath
    before_dirty = bool(bpy.data.is_dirty)
    before_sha = _sha(Path(before_path)) if before_path and Path(before_path).is_file() else None
    snapshot = directory/'snapshot.blend'
    result = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True,
        check_existing=False, relative_remap=True)
    if result != {'FINISHED'} or bpy.data.filepath != before_path:
        raise RuntimeError('保存快照未完成，或当前工程路径意外改变')
    if before_sha is not None and _sha(Path(before_path)) != before_sha:
        raise RuntimeError('当前源工程文件在快照期间发生变化')
    request_options = dict(resources=resources,
        samples_per_axis=int(settings.samples_per_axis),
        lighting_samples=int(settings.lighting_samples),
        memory_budget_bytes=settings.memory_budget_mb*1024*1024)
    from .workflow_contract import request as domain_request
    request = domain_request(snapshot, scene.name, context.view_layer.name, times, **request_options)
    if operation == 'edit':
        request['command'] = 'toon-workflow.edit'
        request['params'].update(style=joint_request._descriptor(style_path),
                                 expected_author_sha256=author_fingerprint)
    elif operation == 'inspect':
        request['command'] = 'toon-workflow.inspect'
    _write(directory/'request.json', request)
    record = dict(schema='toon-workbench-submission.v1', owner=token,
        directory=str(directory), jobs_directory=str(jobs), snapshot=str(snapshot),
        project_root=str(project_root),
        request=str(directory/'request.json'), source_filepath=before_path,
        source_sha256=before_sha, source_unchanged=True,
        source_dirty_before=before_dirty, source_dirty_after=bool(bpy.data.is_dirty),
        resources=resources, scene=scene.name, view_layer=context.view_layer.name,
        mode=mode, times=times, effective_times=effective_times,
        operation=operation, author_sha256=author_fingerprint,
        snapshot_sha256=_sha(snapshot), lighting_samples=int(settings.lighting_samples),
        job_id='', state='prepared', artifact='')
    from . import workbench_identity
    record['gui_identity']=workbench_identity.identity(scene)
    record['author_animation_sha256']=workbench_identity.animation_signature(scene)
    _write(directory/'submission.json', record)
    if activate:
        _activate_record(context,record)
    return record


def _activate_record(context,record):
    settings=getattr(context.scene,PROP)
    settings.owner_token = record['owner']
    settings.submission_directory = record['directory']
    settings.job_id = ''
    settings.job_state = 'prepared'
    settings.artifact_path = ''
    settings.message = '快照与请求已保存；源工程路径保持不变'


def _command(record, *arguments):
    python = Path(sys.prefix)/'bin'/('python.exe' if os.name=='nt' else 'python3')
    entry = Path(__file__).resolve().parent.parent/'toon_workflow_cli_v1'/'cli.py'
    if not python.is_file() or not entry.is_file():
        raise ValueError('当前 Blender Python 或随包公共 CLI 入口缺失')
    return [str(python), '-B', str(entry), '--blender', bpy.app.binary_path,
        '--jobs-dir', record['jobs_directory'],
        '--transactions-dir', str(Path(record['directory'])/'transactions'), *arguments]


def _launch(record, command, kind):
    """Launch only the public CLI; no shell string or private render worker."""
    directory = Path(record['directory'])
    # A failed cancel may be retried for this same submission. Keep every
    # attempt's exclusive logs rather than overwriting or colliding with them.
    stem = kind + ('-' + uuid.uuid4().hex if kind == 'cancel' else '')
    stdout_path = directory/(stem+'-stdout.log')
    stderr_path = directory/(stem+'-stderr.log')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    startup = None
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    _write(directory/(stem+'-command.json'), dict(argv=command, shell=False))
    environment=os.environ.copy()
    environment['TOON_WORKFLOW_CLI_CORE_ROOT']=str(_submission_root(record)/'tools'/'blenderctl')
    with stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr:
        process = subprocess.Popen(command, cwd=str(_submission_root(record)), shell=False,
            stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            creationflags=flags, startupinfo=startup, env=environment)
    _processes[(record['owner'], kind)] = process
    return process


def _record(settings):
    if not settings.owner_token or not settings.submission_directory:
        raise ValueError('当前工作台没有拥有的提交任务')
    directory = Path(settings.submission_directory)
    if not directory.is_absolute():
        raise ValueError('工作台提交路径无效')
    record = _read(directory/'submission.json')
    if record.get('owner') != settings.owner_token or record.get('directory') != str(directory):
        raise ValueError('提交所有权不匹配')
    jobs = Path(record['jobs_directory'])
    if jobs.parent != directory or jobs.name != 'jobs':
        raise ValueError('任务目录超出当前提交范围')
    from . import workbench_identity
    workbench_identity.check_owner(settings.id_data,record)
    return record


def refresh_scene(scene):
    """Read only local public status/results; never spawn a status command."""
    settings = getattr(scene, PROP)
    if not settings.owner_token:
        return
    try:
        record = _record(settings)
        directory = Path(record['directory'])
        cancel_key = (record['owner'], 'cancel')
        cancel_process = _processes.get(cancel_key)
        if cancel_process is not None:
            cancel_code = cancel_process.poll()
            if cancel_code is not None:
                if cancel_code == 0:
                    record['cancel_sent'] = True
                    _write(directory/'submission.json', record)
                    _processes.pop(cancel_key, None)
                else:
                    _processes.pop(cancel_key, None)
                    settings.message = '取消请求进程失败，可重试当前作业：' + str(cancel_code)
        process = _processes.get((record['owner'], 'submit'))
        if not record.get('job_id') and settings.job_state == 'submitting':
            if process is not None and process.poll() is None:
                return
            log = directory/'submit-stdout.log'
            if not log.is_file():
                return
            result = _read(log)
            job_id = result.get('job_id')
            if result.get('ok') is False or not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
                settings.job_state = 'submit_failed'
                settings.message = '公共 CLI 提交失败：' + str(result.get('error', result))[:900]
                _processes.pop((record['owner'], 'submit'), None)
                return
            if process is not None and process.returncode != 0:
                settings.job_state = 'submit_failed'
                settings.message = '公共 CLI 提交进程失败：' + str(process.returncode)
                _processes.pop((record['owner'], 'submit'), None)
                return
            record['job_id'] = job_id
            record['state'] = 'submitted'
            _write(directory/'submission.json', record)
            settings.job_id = job_id
            settings.job_state = 'submitted'
            _processes.pop((record['owner'], 'submit'), None)
        if record.get('job_id'):
            status_path = Path(record['jobs_directory'])/record['job_id']/'status.json'
            if not status_path.is_file():
                return
            status = _read(status_path)
            if status.get('job_id') != record['job_id']:
                raise ValueError('公共状态文件的作业编号不匹配')
            settings.job_id = record['job_id']
            settings.job_state = status.get('state', 'unknown')
            result_path = status_path.with_name('result.json')
            if settings.job_state in _TERMINAL and result_path.is_file():
                result = _read(result_path)
                if result.get('job_id') != record['job_id']:
                    raise ValueError('公共结果文件的作业编号不匹配')
                if result.get('ok'):
                    artifact = result.get('data', {}).get('output', '') or result.get('data', {}).get('result', {}).get('output', '')
                    if not artifact:
                        artifact = result.get('artifacts', {}).get('output-toon-joint.png', '')
                    settings.artifact_path = artifact
                    messages={'edit':'作者候选已验收；当前GUI未应用，请显式接收',
                        'inspect':'公共检查已完成（本操作不生成图片）',
                        'recover':'原快照作业恢复完成；当前GUI编辑保持',
                        'run':'联合渲染已完成'}
                    if record.get('operation')=='edit' and (directory/'received.json').is_file():
                        messages['edit']='本候选已显式接收；后续GUI编辑独立保留'
                    settings.message = messages.get(record.get('operation'),'公共作业已完成') if settings.job_state == 'succeeded' else settings.job_state
                else:
                    settings.message = str(result.get('error', settings.job_state))[:1000]
    except (OSError, ValueError, TypeError, KeyError) as error:
        # A public worker may atomically replace a status file between reads.
        # Preserve the last useful state and report an explicit diagnostic.
        settings.message = str(error)[:1000]


def _timer():
    if not _registered:
        return None
    changed=False
    for scene in bpy.data.scenes:
        settings=getattr(scene,PROP)
        before=(settings.job_id,settings.job_state,settings.artifact_path,settings.message)
        refresh_scene(scene)
        changed|=before!=(settings.job_id,settings.job_state,settings.artifact_path,settings.message)
    if changed:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D','PROPERTIES','NODE_EDITOR'}:area.tag_redraw()
    return .5


@persistent
def _load_post(_dummy):
    if _registered and not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=.5, persistent=True)


def _mesh(context):
    obj = context.object
    if obj is None or obj.type != 'MESH' or context.mode != 'OBJECT':
        raise ValueError('请选择对象模式下的本地网格')
    if obj.library or obj.override_library:
        raise ValueError('当前镜头修形工作台只处理本地可编辑网格')
    return obj


def _correction(obj):
    matches = [m for m in obj.modifiers if m.type == 'NODES' and m.node_group
        and m.node_group.get(camera_correction.PROFILE_KEY) == camera_correction.PROFILE]
    if len(matches) > 1:
        raise ValueError('目标存在多个镜头修形修改器，请先明确作者意图')
    return matches[0] if matches else None


def _camera_poll(_self, obj):
    return obj.type == 'CAMERA'


class TOONWORKBENCH_Settings(bpy.types.PropertyGroup):
    stage: EnumProperty(name='工序', items=(
        ('SHADING', '着色', '色阶着色、材质混合与配方'),
        ('FACE', '面影', '面部 SDF、头部法向、眼部与发影'),
        ('LINES', '描边', '视口描边、笔刷、参数动画与镜头修形'),
        ('OUTPUT', '输出', '分层渲染、矢量线稿和环绕预览'),
    ), default='SHADING')
    level: EnumProperty(name='操控档位', items=(
        ('DAILY', '日常', '显示当前工序的高频控制'),
        ('ADVANCED', '进阶', '展开当前工序的完整选项'),
    ), default='DAILY')
    last_report: StringProperty(options={'SKIP_SAVE'})
    last_report_kind: StringProperty(default='INFO', options={'SKIP_SAVE'})
    range_start: FloatProperty(name='起始帧', default=1.0, min=-1048574.0, max=1048574.0)
    range_end: FloatProperty(name='结束帧', default=12.0, min=-1048574.0, max=1048574.0)
    range_step: FloatProperty(name='步长', default=1, min=0.001)
    project_directory: StringProperty(name='公共工作目录', subtype='DIR_PATH',
        description='包含 blenderctl.ps1 的工程目录，用于现有公共作业监管')
    show_head_normals: BoolProperty(name='头部整体法向', default=True)
    show_face_threshold: BoolProperty(name='方向阈值面影', default=True)
    show_face_sdf: BoolProperty(name='专用SDF贴图面影', default=True)
    show_finish: BoolProperty(name='CEL 高光、边缘光与强度动画', default=False)
    show_eye_layers: BoolProperty(name='眼部叠底、高光与固定发影', default=False)
    show_hair_map: BoolProperty(name='HM 逐发束明暗与高光', default=False)
    show_line_material: BoolProperty(name='材质与描边线色',default=False)
    show_line_template: BoolProperty(name='完整描边模板与角色绑定', default=False)
    show_cel_recipe: BoolProperty(name='CEL 配方与显式分配', default=False)
    show_surface_slots: BoolProperty(name='活动槽表面与材质模板', default=False)
    show_normal_advanced: BoolProperty(name='单部件法向与材质连接', default=False)
    style_file: StringProperty(name='作者样式 JSON（完整样式或BASIC线宽）', subtype='FILE_PATH')
    show_pencil_preset: BoolProperty(name='PRESET 描边与笔刷', default=False)
    show_pencil_author: BoolProperty(name='作者管理与文件', default=False)
    show_pencil_lines: BoolProperty(name='Line 层级与合并', default=False)
    show_pencil_animation: BoolProperty(name='描边参数动画', default=False)
    show_pencil_elements: BoolProperty(name='独立线图分层设置', default=False)
    show_pencil_output: BoolProperty(name='独立线图输出（非联合成图）', default=False)
    output_directory: StringProperty(name='输出目录', subtype='DIR_PATH')
    render_mode: EnumProperty(name='渲染模式', items=[
        ('FRAME', '单帧', '渲染当前帧'),
        ('SEQUENCE', '序列', '按时间数组顺序渲染一组帧')], default='FRAME')
    sequence_times: StringProperty(name='时间数组', default='[1, 6.5, 12]',
        description='非空 JSON 数字数组，允许子帧，按输入顺序渲染')
    samples_per_axis: EnumProperty(name='每轴采样', items=[(str(n), str(n), str(n*n)+'个联合空间采样') for n in (1, 2, 4, 8)], default='2')
    lighting_samples: IntProperty(name='光照采样', default=64, min=1, max=256)
    memory_budget_mb: IntProperty(name='图像内存预算 MiB', default=512, min=32, max=65536)
    timeout_seconds: IntProperty(name='作业超时（秒）', default=600, min=10, max=86400)
    correction_camera: PointerProperty(name='修形相机', type=bpy.types.Object, poll=_camera_poll)
    correction_reference: PointerProperty(name='深度参考', type=bpy.types.Object)
    weight_attribute: StringProperty(name='权重属性', default='toon_perspective_weight')
    shot_strength: FloatProperty(name='镜头方案强度', default=0., min=0., max=1.)
    shot_replace: BoolProperty(name='更新已有镜头方案', default=False)
    owner_token: StringProperty(options={'HIDDEN'})
    submission_directory: StringProperty(name='提交目录', subtype='DIR_PATH')
    job_id: StringProperty(name='公共作业编号')
    job_state: StringProperty(name='状态', default='idle')
    artifact_path: StringProperty(name='结果图', subtype='FILE_PATH')
    message: StringProperty(name='消息')


class TOONWORKBENCH_OT_prepare_joint(bpy.types.Operator):
    bl_idname = 'toon_workbench.prepare_joint'
    bl_label = '保存联合渲染快照'
    bl_description = '保存可编辑副本与公共CLI请求，保持当前工程路径不变'
    def execute(self, context):
        try:
            with ensure_object_mode(context):
                _prepare(context)
            self.report({'INFO'}, '联合渲染快照与请求已保存')
            return {'FINISHED'}
        except Exception as error:
            getattr(context.scene, PROP).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_joint_render(bpy.types.Operator):
    bl_idname = 'toon_workbench.joint_render'
    bl_label = '后台联合渲染'
    bl_description = '保存当前快照，按显式配置提交公共blenderctl联合渲染'
    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            if settings.job_state in {'submitting', 'submitted', 'queued', 'running'}:
                raise ValueError('当前工作台已有进行中的公共作业')
            with ensure_object_mode(context):
                record = _prepare(context,activate=False)
            command = _command(record, '--timeout', str(settings.timeout_seconds),
                '--async', 'request', record['request'])
            _launch(record, command, 'submit')
            _activate_record(context,record)
            settings.job_state = 'submitting'
            settings.message = '已启动公共 CLI 提交；任务由公共作业系统管理'
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_author_candidate(bpy.types.Operator):
    bl_idname = 'toon_workbench.author_candidate'
    bl_label = '后台制作作者候选'
    bl_description = '保存当前快照，按完整样式或BASIC线宽JSON生成独立候选；完成后显式接收'
    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            with ensure_object_mode(context):
                record = _prepare(context, operation='edit',activate=False)
            _launch(record, _command(record, '--timeout', str(settings.timeout_seconds),
                '--async', 'request', record['request']), 'submit')
            _activate_record(context,record)
            settings.job_state = 'submitting'
            settings.message = '已提交作者候选；完成后可显式接收'
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def receive_author(context):
    """Apply only the accepted own-job style, rejecting later author changes."""
    settings = getattr(context.scene, PROP)
    refresh_scene(context.scene)
    record = _record(settings)
    from . import workbench_identity
    if record.get('operation') != 'edit' or settings.job_state != 'succeeded':
        raise ValueError('当前提交没有已接受的作者候选')
    job = Path(record['jobs_directory']) / record['job_id']
    result = _read(job/'result.json')
    if not result.get('ok') or result.get('data', {}).get('candidate_accepted') is not True:
        raise ValueError('公共父进程尚未接受此候选')
    acceptance = _read(job/'toon-workflow-acceptance.json')
    report_path = job/'toon-workflow-report.json'
    if acceptance.get('status') != 'accepted' or acceptance['report'] != dict(
            file=str(report_path), expected_sha256=_sha(report_path)):
        raise ValueError('候选验收报告已改变')
    report = _read(report_path)
    public_request=_read(job/'request.json')
    original_request=_read(Path(record['request']))
    from .workflow_contract import fingerprint
    if (result.get('job_id')!=record['job_id'] or acceptance.get('operation')!='edit'
            or acceptance.get('request_sha256')!=fingerprint(public_request)
            or fingerprint(public_request)!=fingerprint(original_request)
            or report.get('implementation')!=public_request.get('params',{}).get('implementation')
            or public_request.get('command')!='toon-workflow.edit'):
        raise ValueError('候选结果、验收、请求或实现身份不匹配')
    if (report.get('operation') != 'edit' or report.get('source') != dict(
            file=record['snapshot'],expected_sha256=record['snapshot_sha256'])):
        raise ValueError('候选不属于当前工作台快照')
    _synchronize_author_curves(context.scene)
    from .joint_workflow import capture_author, non_author_signature
    from .workflow_contract import fingerprint
    received=Path(record['directory'])/'received.json'
    if received.is_file():
        receipt=_read(received)
        workbench_identity.check_owner(context.scene,record)
        if (receipt.get('job_id')!=record['job_id'] or receipt.get('report_sha256')!=_sha(report_path)
                or receipt.get('pid')!=os.getpid() or receipt.get('scene_uid')!=context.scene.session_uid
                or receipt.get('author_sha256')!=report['result']['author_sha256']
                or workbench_identity.animation_signature(context.scene)!=record.get('author_animation_sha256')
                or fingerprint(capture_author(context.scene))!=receipt.get('author_sha256')):
            raise ValueError('已接收记录与当前作者或会话不同；未重复写入')
        settings.message='此候选已接收，作者保持不变'
        return receipt['author_sha256']
    workbench_identity.check_owner(context.scene,record,receive=True)
    if fingerprint(capture_author(context.scene)) != record['author_sha256']:
        raise ValueError('提交后作者设置已改变；未覆盖当前编辑，请从新快照制作候选')
    if workbench_identity.animation_signature(context.scene)!=record.get('author_animation_sha256'):
        raise ValueError('提交后作者动画或驱动已改变；未覆盖当前编辑')
    style = report['result']['style']
    path = Path(style['file'])
    if path.is_symlink() or not path.resolve().is_relative_to(job.resolve()) or _sha(path) != style['expected_sha256']:
        raise ValueError('候选样式路径或指纹不匹配')
    # The same author baseline and native importer validate animation/refs.
    # Only style data are received; current geometry and later object edits stay.
    preserve = non_author_signature(context.scene)
    from . import author_receive
    def commit():
        _write(received,dict(schema='toon-workbench-received.v1',job_id=record['job_id'],
            report_sha256=_sha(report_path),author_sha256=report['result']['author_sha256'],
            pid=os.getpid(),scene_uid=context.scene.session_uid))
    author_receive.apply(context.scene,path,report['result']['author_sha256'],preserve,commit=commit)
    settings.message = '已接收本次候选作者样式；几何与后续对象编辑保留'
    return report['result']['author_sha256']


class TOONWORKBENCH_OT_receive_author(bpy.types.Operator):
    bl_idname = 'toon_workbench.receive_author'
    bl_label = '接收已验证作者候选'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            receive_author(context)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_inspect_job(bpy.types.Operator):
    bl_idname='toon_workbench.inspect_job'
    bl_label='公共检查快照或完成结果'
    bl_description='检查自己的完成state；无完成state时检查当前快照，不生成图片'
    def execute(self,context):
        try:
            from .workbench_jobs import start_inspect
            start_inspect(context)
            return {'FINISHED'}
        except Exception as error:
            getattr(context.scene,PROP).message=str(error)
            self.report({'ERROR'},str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_recover_job(bpy.types.Operator):
    bl_idname='toon_workbench.recover_job'
    bl_label='恢复原快照作业'
    bl_description='沿原持久请求和原指纹创建新公共作业；不捕获或应用当前GUI编辑'
    def execute(self,context):
        try:
            from .workbench_jobs import start_recover
            start_recover(context)
            return {'FINISHED'}
        except Exception as error:
            getattr(context.scene,PROP).message=str(error)
            self.report({'ERROR'},str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_cancel_job(bpy.types.Operator):
    bl_idname = 'toon_workbench.cancel_job'
    bl_label = '取消当前公共作业'
    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            refresh_scene(context.scene)
            record = _record(settings)
            if not record.get('job_id') or record['job_id'] != settings.job_id:
                raise ValueError('当前工作台尚无可取消的已确认作业编号')
            if settings.job_state in _TERMINAL:
                raise ValueError('当前公共作业已结束')
            if record.get('cancel_sent') or (record['owner'], 'cancel') in _processes:
                raise ValueError('已发送此作业的取消请求')
            _launch(record, _command(record, 'job', 'cancel', record['job_id']), 'cancel')
            settings.message = '已向公共作业系统发送取消请求'
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_refresh(bpy.types.Operator):
    bl_idname = 'toon_workbench.refresh'
    bl_label = '刷新作业状态'
    def execute(self, context):
        refresh_scene(context.scene)
        return {'FINISHED'}


class TOONWORKBENCH_OT_zero_weight(bpy.types.Operator):
    bl_idname = 'toon_workbench.zero_weight'
    bl_label = '创建全零权重'
    bl_description = '仅创建不存在的FLOAT/POINT权重；共享网格先隔离，不会自动产生形变'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            obj = _mesh(context)
            name = getattr(context.scene, PROP).weight_attribute
            if not name or len(name) > 63:
                raise ValueError('权重属性名应为1至63个字符')
            if obj.data.attributes.get(name):
                raise ValueError('已有同名属性，未覆盖作者权重')
            if obj.data.users > 1 or obj.data.library:
                obj.data = obj.data.copy()
            attribute = obj.data.attributes.new(name, 'FLOAT', 'POINT')
            attribute.data.foreach_set('value', [0.] * len(obj.data.vertices))
            obj.data.update()
            self.report({'INFO'}, '全零权重已建立；请按需要绘制局部权重')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_create_correction(bpy.types.Operator):
    bl_idname = 'toon_workbench.create_correction'
    bl_label = '创建镜头修形'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            obj = _mesh(context)
            settings = getattr(context.scene, PROP)
            camera_correction.build(obj, settings.correction_camera,
                settings.correction_reference, settings.weight_attribute, strength=0.)
            self.report({'INFO'}, '已建立默认零强度的可编辑镜头修形')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_inspect_correction(bpy.types.Operator):
    bl_idname = 'toon_workbench.inspect_correction'
    bl_label = '检查镜头修形'
    def execute(self, context):
        try:
            obj = _mesh(context)
            modifier = _correction(obj)
            if modifier is None:
                raise ValueError('当前网格没有受管镜头修形')
            report = camera_correction.inspect(obj, modifier)
            getattr(context.scene, PROP).message = ('修形输入有效' if report['status'] == 'valid' else '; '.join(report['issues']))
            self.report({'INFO'} if report['status'] == 'valid' else {'WARNING'}, getattr(context.scene, PROP).message)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONWORKBENCH_OT_define_shot(bpy.types.Operator):
    bl_idname = 'toon_workbench.define_shot'
    bl_label = '保存所选网格的镜头方案'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            settings=getattr(context.scene, PROP)
            camera_shots.define(context.scene, settings.correction_camera,
                settings.correction_reference,
                [obj for obj in context.selected_objects if obj.type=='MESH'],
                strength=settings.shot_strength, replace=settings.shot_replace)
            self.report({'INFO'}, '镜头方案已保存；尚未改变网格修形参数')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error));return {'CANCELLED'}


class TOONWORKBENCH_OT_apply_shot(bpy.types.Operator):
    bl_idname = 'toon_workbench.apply_shot'
    bl_label = '预览当前镜头方案'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            report=camera_shots.apply(context.scene)
            if report is None:raise ValueError('当前场景没有镜头方案')
            context.view_layer.update()
            self.report({'INFO'}, '已应用镜头：'+report['camera'])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error));return {'CANCELLED'}


def _draw_pencil_sections(layout, context, settings):
    """Delegate native author UI without maintaining a second implementation."""
    addon,file_ui,line_ui,parameter_animation_ui,vector_ui=(pencil_backend(name)
        for name in ('addon','file_ui','line_ui','parameter_animation_ui','vector_ui'))
    sections = (
        ('show_pencil_preset', (addon.TOONVIEWPORT_PT_preview.draw,)),
        ('show_pencil_author', (file_ui.TOONVIEWPORT_PT_author_files.draw,)),
        ('show_pencil_lines', (line_ui.TOONVIEWPORT_PT_lines.draw,)),
        ('show_pencil_animation', (parameter_animation_ui.TOONPARAM_PT_animation.draw,)),
        ('show_pencil_elements', (addon.TOONVIEWPORT_PT_elements.draw,)),
        ('show_pencil_output', (addon.TOONVIEWPORT_PT_sequence.draw,
                                vector_ui.TOONVECTOR_PT_output.draw)),
    )
    for name, delegates in sections:
        box = layout.box()
        expanded = getattr(settings, name)
        box.prop(settings, name, icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT', emboss=False)
        if expanded:
            if name in {'show_pencil_preset', 'show_pencil_author',
                        'show_pencil_elements', 'show_pencil_output'}:
                box.label(text='以下导出独立线图；联合成图请使用联合渲染区')
            for draw in delegates:
                draw(SimpleNamespace(layout=box), context)


def draw_contents(layout, context):
    """Compatibility draw entry for the unified phase workspace."""
    from . import workspace_ui

    workspace_ui.draw(layout, context)


class TOONWORKBENCH_PT_workbench(bpy.types.Panel):
    bl_label = '三渲二工作台'
    bl_idname = 'TOONWORKBENCH_PT_workbench'
    bl_space_type = THEME.editor_space
    bl_region_type = 'UI'
    bl_category = THEME.tab_label
    bl_options = {'HIDE_HEADER'}
    bl_order = -20
    def draw(self, context):
        draw_contents(self.layout, context)


_CLASSES = (TOONWORKBENCH_Settings, TOONWORKBENCH_OT_prepare_joint,
    TOONWORKBENCH_OT_author_candidate, TOONWORKBENCH_OT_receive_author,
    TOONWORKBENCH_OT_joint_render, TOONWORKBENCH_OT_cancel_job,
    TOONWORKBENCH_OT_inspect_job, TOONWORKBENCH_OT_recover_job,
    TOONWORKBENCH_OT_refresh, TOONWORKBENCH_OT_zero_weight,
    TOONWORKBENCH_OT_create_correction, TOONWORKBENCH_OT_inspect_correction,
    TOONWORKBENCH_OT_define_shot, TOONWORKBENCH_OT_apply_shot,
    TOONWORKBENCH_PT_workbench)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Scene.toon_workbench 已被另一注册实例拥有')
    try:
        if hasattr(bpy.types.Scene, 'toon_viewport_preview'):
            pencil_core=pencil_backend('addon')
            prop = bpy.types.Scene.bl_rna.properties['toon_viewport_preview']
            if (not pencil_core._registered or not hasattr(bpy.types.Scene, 'toon_author_files')
                    or prop.fixed_type.as_pointer() != pencil_core.PreviewSettings.bl_rna.as_pointer()):
                raise RuntimeError('已有描边 RNA 不属于当前完整后端实例')
        joint_ownership.acquire(_PENCIL_LEASE)
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONWORKBENCH_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
        bpy.app.handlers.load_post.append(_load_post)
        bpy.app.timers.register(_timer, first_interval=.5, persistent=True)
        from . import workspace_ui
        workspace_ui.register()
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    _registered = False
    from . import workspace_ui
    workspace_ui.unregister()
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() == _descriptor:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    joint_ownership.release(_PENCIL_LEASE)
    # Public supervisors keep running. Discard only local Popen references;
    # no terminate/kill, no job mutation, and no other add-on teardown.
    _processes.clear()
