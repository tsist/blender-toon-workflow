"""The single four-stage Toon sidebar, with views of the original pipelines."""

from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent

from . import workbench as wb
from .backend import pencil as pencil_backend
from .._ui_kit import (
    BackgroundJobRunner, HUDManager, IconPack, SidebarDock, draw_heavy_workspace_header,
    primary_button, progress_card, prop_column, section_panel, status_banner,
)


DOMAIN = 'toon'
_ICONS = IconPack(DOMAIN)
_RUNNER = BackgroundJobRunner(DOMAIN)
_MONITOR = None
_JOB_KEY = None
_JOB_SINCE = 0.0
_REGISTERED = []
_BUSY = {'submitting', 'submitted', 'queued', 'running'}
_STATE_LABELS = {
    'idle': '', 'submitting': '正在提交渲染', 'submitted': '已提交渲染', 'queued': '等待渲染',
    'running': '渲染进行中', 'succeeded': '渲染完成', 'failed': '作业失败',
    'submit_failed': '提交失败', 'cancelled': '作业已取消', 'canceled': '作业已取消',
    'timed_out': '作业超时', 'prepared': '已保存快照',
}


def _section(layout, name, title, icon, *, closed=True):
    return section_panel(layout, 'TOON_' + name, title, icon_value=_ICONS.action_id(icon),
                         default_closed=closed)[1]


def _props(layout, owner, *names):
    column = prop_column(layout, align=True)
    for name in names:
        column.prop(owner, name)
    return column


def _module_sections(layout, context, icon, sections):
    for name, title, module in sections:
        if hasattr(context.scene, module.PROP) and (body := _section(layout, name, title, icon)) is not None:
            module.draw_contents(body, context)


def _panel_draw(layout, context, panel):
    panel.draw(SimpleNamespace(layout=layout), context)


def _draw_shading(layout, context, _settings, advanced):
    from . import (cel_ui, component_ui, finish_ui, line_material_ui, media_render_ui,
                   shot_look_ui, style_family_ui, style_set_ui, surface_ui)

    material = getattr(context.object, 'active_material', None)
    if material:
        layout.label(text=material.name, icon='MATERIAL')
    surface = getattr(context.scene, surface_ui.PROP)
    column = _props(layout, surface, 'candidate', 'shadow_color')
    if surface.mode in {'eevee_cel3', 'eevee_cel3_reflect'}:
        column.prop(surface, 'mid_color')
    column.prop(surface, 'lit_color')
    primary_button(layout, 'toon_surface.apply', '应用色阶到活动材质槽',
                   icon_value=_ICONS.action_id('cel_shader'))
    row = layout.row(align=True)
    row.operator('toon_surface.update', text='更新色阶')
    row.operator('toon_surface.restore', text='还原原材质')
    if advanced:
        _module_sections(layout, context, 'cel_shader', (
            ('surface', '色阶、明暗羽化与材质库', surface_ui),
            ('components', '风格组件与混合 PBR', component_ui),
            ('finish', '高光、边缘光与强度动画', finish_ui),
            ('cel_recipe', '材质配方与显式分配', cel_ui),
            ('style_families', '材质风格家族', style_family_ui),
            ('style_sets', '材质样式集', style_set_ui),
            ('shot_look', '镜头风格与原生媒体', shot_look_ui),
            ('media_render', '原生媒体和艺术线', media_render_ui),
            ('line_material', '材质墨线颜色来源', line_material_ui),
        ))


def _draw_face(layout, context, _settings, advanced):
    from . import eye_overlay_ui, face_ui, hair_map_ui, hair_sdf_ui, head_ui, lighting_53_ui, normal_ui, sdf_ui

    if lighting_53_ui.TOON53_PT_lighting.poll(context):
        lighting = getattr(context.scene, lighting_53_ui.PROP)
        _props(layout, lighting, 'key', 'fill', 'rim')
        row = layout.row(align=True)
        row.operator('toon53.prepare', text='准备影形灯组', icon_value=_ICONS.action_id('face_sdf'))
        row.operator('toon53.bind', text='绑定活动材质槽')
    else:
        layout.label(text='使用 EEVEE 可启用面影灯光工具', icon='INFO')
    primary_button(layout, 'toon_head.add_selected', '登记所选部件的头部法向',
                   icon_value=_ICONS.action_id('face_sdf'))
    row = layout.row(align=True)
    row.operator('toon_head.apply', text='应用整头法向')
    row.operator('toon_face.inspect_field', text='检查面影')
    if advanced:
        if lighting_53_ui.TOON53_PT_lighting.poll(context):
            body = _section(layout, 'lighting', '主副光、眼睑与相机影形', 'face_sdf')
            if body is not None:
                _panel_draw(body, context, lighting_53_ui.TOON53_PT_lighting)
        _module_sections(layout, context, 'face_sdf', (
            ('sdf', '面部 SDF 贴图与影形', sdf_ui),
            ('direction_threshold', '方向阈值与面部字段', face_ui),
            ('head', '整头法向与部件管理', head_ui),
            ('normals', '局部法向连接', normal_ui),
            ('eye', '眼部层、瞳孔亮片与发影', eye_overlay_ui),
            ('hair_map', '发束高光与遮罩', hair_map_ui),
            ('hair_sdf', '发束方向影形', hair_sdf_ui),
        ))


def _draw_correction(layout, context, settings):
    obj = context.object
    if obj is not None and obj.type == 'MESH':
        try:
            modifier = wb._correction(obj)
            if modifier is not None:
                ids = json.loads(modifier.node_group[wb.camera_correction._INTERFACE])
                for key, label in (('camera', '修形相机'), ('reference', '深度参考'), ('weight', '权重属性')):
                    row = layout.row()
                    row.enabled = False
                    row.prop(getattr(modifier.properties.inputs, ids[key]), 'value', text=label)
                layout.prop(getattr(modifier.properties.inputs, ids['strength']), 'value', text='修形强度')
                _props(layout, modifier, 'show_viewport', 'show_render')
            else:
                _props(layout, settings, 'correction_camera', 'correction_reference', 'weight_attribute')
                row = layout.row(align=True)
                row.operator('toon_workbench.zero_weight')
                row.operator('toon_workbench.create_correction')
            layout.operator('toon_workbench.inspect_correction')
        except (ValueError, KeyError, AttributeError) as error:
            layout.label(text=str(error)[:100], icon='ERROR')
    else:
        layout.label(text='选中本地网格以编辑镜头修形', icon='INFO')
    layout.separator()
    _props(layout, settings, 'correction_camera', 'correction_reference', 'shot_strength', 'shot_replace')
    layout.operator('toon_workbench.define_shot')
    root = context.scene.get(wb.camera_shots.KEY)
    if isinstance(root, bpy.types.Collection):
        for shot in root.children:
            camera = shot.get('toon_shots_camera')
            if camera is not None and 'toon_shots_strength' in shot:
                layout.prop(shot, '["toon_shots_strength"]', text=camera.name)
        layout.operator('toon_workbench.apply_shot')


def _draw_lines(layout, context, settings, advanced):
    from . import line_template_ui
    pencil = context.scene.toon_viewport_preview
    layout.prop(pencil, 'enabled', text='开启视口描边', toggle=True, icon_value=_ICONS.action_id('line_viewport'))
    _props(layout, pencil, 'style_mode')
    if pencil.style_mode == 'BASIC':
        _props(layout, pencil, 'line_width', 'line_color', 'opacity')
    else:
        _props(layout, pencil, 'width_multiplier')
    operator = layout.operator('toon_viewport.author_file', text='为所选网格新建线组', icon='ADD')
    operator.action = 'NEW'
    if advanced:
        addon, files, lines, animation, functions = (pencil_backend(name) for name in (
            'addon', 'file_ui', 'line_ui', 'parameter_animation_ui', 'line_function_ui'))
        for name, title, panel in (
            ('pencil', '描边模式、边源、笔刷与防抖', addon.TOONVIEWPORT_PT_preview),
            ('author', '自主作者图与文件', files.TOONVIEWPORT_PT_author_files),
            ('line_groups', '线层级、集合与合并', lines.TOONVIEWPORT_PT_lines),
            ('line_animation', '描边参数动画', animation.TOONPARAM_PT_animation),
            ('line_functions', '活动材质的线条功能', functions.TOONVIEWPORT_PT_line_functions),
        ):
            if (body := _section(layout, name, title, 'line_viewport')) is not None:
                _panel_draw(body, context, panel)
        _module_sections(layout, context, 'line_viewport', (('line_template', '完整描边模板', line_template_ui),))
        if (body := _section(layout, 'correction', '镜头修形与逐机位方案', 'line_viewport')) is not None:
            _draw_correction(body, context, settings)


def _draw_output(layout, context, settings, advanced):
    from . import omni_preview_ui
    addon, vector = (pencil_backend(name) for name in ('addon', 'vector_ui'))
    _props(layout, settings, 'output_directory', 'render_mode')
    if settings.render_mode == 'SEQUENCE':
        _props(layout, settings, 'range_start', 'range_end', 'range_step')
    primary_button(layout, 'toon_workbench.submit_workspace', '后台渲染分离线色通道',
                   icon_value=_ICONS.action_id('toon_output'),
                   enabled=settings.job_state not in _BUSY)
    row = layout.row(align=True)
    row.operator('toon_viewport.export_camera_png', text='保存相机线稿')
    row.operator('toon_omni.submit', text='生成环绕预览')
    if not advanced:
        if (body := _section(layout, 'omni_quick', '环绕预览设置', 'toon_output')) is not None:
            omni_preview_ui.draw_contents(body, context)
    else:
        for name, title, panel in (
            ('elements', '线稿分层与颜色元素', addon.TOONVIEWPORT_PT_elements),
            ('sequence', '独立线稿序列输出', addon.TOONVIEWPORT_PT_sequence),
            ('vector', '矢量线稿和输出列表', vector.TOONVECTOR_PT_output),
        ):
            if (body := _section(layout, name, title, 'toon_output')) is not None:
                _panel_draw(body, context, panel)
        if (body := _section(layout, 'author_output', '作者线稿范围与交接', 'toon_output')) is not None:
            author = context.scene.toon_author_files
            _props(body, author, 'path', 'scope', 'keyed_only')
            row = body.row(align=True)
            op = row.operator('toon_viewport.author_file', text='用当前参数覆盖范围')
            op.action, op.use_current_scope_values = 'OVERRIDE', True
            row.operator('toon_viewport.author_file', text='解除覆盖').action = 'REMOVE_OVERRIDE'
            body.operator('toon_viewport.author_file', text='后台按范围导出').action = 'OUTPUT'
        _module_sections(layout, context, 'toon_output', (('omni', '环绕光照与多机位检查', omni_preview_ui),))
        if (body := _section(layout, 'jobs', '采样、历史作业与作者候选', 'toon_output')) is not None:
            _props(body, settings, 'project_directory', 'samples_per_axis', 'lighting_samples',
                   'memory_budget_mb', 'timeout_seconds')
            row = body.row(align=True)
            row.operator('toon_workbench.prepare_joint', text='保存快照')
            row.operator('toon_workbench.joint_render', text='按已存配置提交')
            row = body.row(align=True)
            row.operator('toon_workbench.inspect_job', text='核对作业')
            row.operator('toon_workbench.recover_job', text='恢复历史序列')
            _props(body, settings, 'style_file')
            row = body.row(align=True)
            row.operator('toon_workbench.author_candidate', text='制作作者候选')
            row.operator('toon_workbench.receive_author', text='接收作者候选')
            if settings.job_id:
                body.label(text=f'作业编号 {settings.job_id}')
    if settings.artifact_path:
        layout.prop(settings, 'artifact_path', text='已完成结果')


_PAGES = {'SHADING': _draw_shading, 'FACE': _draw_face, 'LINES': _draw_lines, 'OUTPUT': _draw_output}


def draw(layout, context):
    settings = getattr(context.scene, wb.PROP)
    draw_heavy_workspace_header(layout, DOMAIN, _ICONS, settings, stage_prop='stage', level_prop='level')
    busy = _busy(context)
    if busy:
        progress_card(layout, DOMAIN, _ICONS, text=busy['detail'], factor=busy['progress'],
                      cancel_operator='toon_workbench.cancel_workspace')
    _PAGES[settings.stage](layout, context, settings, settings.level == 'ADVANCED')
    report = settings.last_report
    kind = settings.last_report_kind
    if settings.stage == 'OUTPUT' and settings.message:
        report = settings.message
        kind = 'RUNNING' if busy else ('ERROR' if 'failed' in settings.job_state else 'INFO')
    status_banner(layout, context, report, kind=kind, icon_pack=_ICONS)


def _set_range(settings):
    start, end, step = (Decimal(str(round(getattr(settings, name), 6))) for name in (
        'range_start', 'range_end', 'range_step'))
    if end < start:
        raise ValueError('结束帧须大于或等于起始帧')
    count = int((end - start) // step) + 1
    if count > wb.joint_sequence.MAX_TIMES:
        raise ValueError(f'单次渲染最多 {wb.joint_sequence.MAX_TIMES} 个时刻')
    times = [float(start + index * step) for index in range(count)]
    wb.joint_sequence.normalize_times(times)
    settings.sequence_times = json.dumps(times)


class TOONWORKBENCH_OT_submit_workspace(bpy.types.Operator):
    bl_idname = 'toon_workbench.submit_workspace'
    bl_label = '提交后台线色渲染'

    def execute(self, context):
        settings = getattr(context.scene, wb.PROP)
        try:
            if settings.render_mode == 'SEQUENCE':
                _set_range(settings)
            return bpy.ops.toon_workbench.joint_render()
        except (ValueError, RuntimeError) as error:
            settings.last_report, settings.last_report_kind = str(error), 'ERROR'
            return {'CANCELLED'}


def _busy(context):
    from . import omni_preview, omni_preview_ui
    if not getattr(context, 'scene', None) or not getattr(context, 'view_layer', None):
        return None
    settings = getattr(context.scene, wb.PROP, None)
    if settings and settings.job_state in _BUSY:
        detail, fraction = _STATE_LABELS[settings.job_state], 0.12
        if settings.job_id and settings.submission_directory:
            root = Path(settings.submission_directory) / 'jobs' / settings.job_id
            try:
                progress = wb._read(root / 'toon-progress.json')
                total = progress.get('frame_total', progress.get('total'))
                index = progress.get('frame_index', progress.get('index'))
                if isinstance(total, int) and total > 0 and isinstance(index, int):
                    detail = f'正在渲染序列，第 {min(total, index + 1)}/{total} 个时刻'
                    fraction = min(.98, max(.12, index / total))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return {'kind': 'joint', 'id': settings.owner_token,
                'detail': detail, 'progress': fraction}
    for kind, label in (('sequence_output', '正在导出线稿序列'), ('render_output', '正在生成渲染线稿')):
        for job in pencil_backend(kind).jobs():
            if tuple(job['scene_key']) != (context.scene.as_pointer(), context.view_layer.as_pointer()):
                continue
            if job['status'] != 'QUEUED':
                continue
            fraction = job.get('completed_count', 0) / max(1, job.get('total_count', 1))
            return {'kind': kind, 'id': job['id'], 'detail': label, 'progress': max(0.1, fraction)}
    omni = getattr(context.scene, omni_preview_ui.PROP, None)
    if omni and omni.job_directory:
        try:
            status = omni_preview.status(bpy.path.abspath(omni.job_directory), include_report=False)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if status['status'] in {'submitted', 'running', 'finalizing'}:
            completed, total = status.get('completed', 0), status.get('total', 1)
            return {'kind': 'omni', 'id': omni.job_directory,
                    'detail': f'环绕预览 · {completed}/{total} 个视角',
                    'progress': max(0.1, completed / max(1, total))}
    return None


class TOONWORKBENCH_OT_cancel_workspace(bpy.types.Operator):
    bl_idname = 'toon_workbench.cancel_workspace'
    bl_label = '取消当前三渲二作业'

    def execute(self, context):
        job = _busy(context)
        if not job:
            return {'CANCELLED'}
        if job['kind'] == 'joint':
            return bpy.ops.toon_workbench.cancel_job()
        if job['kind'] == 'sequence_output':
            return bpy.ops.toon_viewport.cancel_sequence(job_id=job['id'])
        if job['kind'] == 'render_output':
            return bpy.ops.toon_viewport.cancel_output()
        return bpy.ops.toon_omni.cancel()


def _stop_monitor():
    global _MONITOR
    if _MONITOR is not None:
        _MONITOR.cleanup()
        _MONITOR = None


def _tick():
    global _MONITOR, _JOB_KEY, _JOB_SINCE
    job = _busy(bpy.context)
    if not job:
        if _MONITOR is not None and _RUNNER.running:
            wb._write(Path(_MONITOR.name) / 'end.json', {'ok': True})
        _JOB_KEY = None
        return .5
    key = (job['kind'], job['id'])
    if key != _JOB_KEY:
        _JOB_KEY, _JOB_SINCE = key, monotonic()
    if _MONITOR is None and monotonic() - _JOB_SINCE >= 2:
        python = Path(sys.exec_prefix) / ('bin/python.exe' if sys.platform == 'win32' else 'bin/python3')
        if not python.is_file():
            return .5
        _MONITOR = TemporaryDirectory(prefix='toon_status_', ignore_cleanup_errors=True)
        directory = Path(_MONITOR.name)
        status, result = directory / 'status.json', directory / 'end.json'
        wb._write(status, job)
        try:
            _RUNNER.start([str(python), '-B', str(Path(__file__).with_name('progress_worker.py')), str(status), str(result)],
                          label='三渲二渲染', status_file=status, result_file=result,
                          on_complete=lambda _ok, _payload: _stop_monitor())
        except (RuntimeError, OSError):
            _stop_monitor()
    elif _MONITOR is not None and _RUNNER.running:
        wb._write(Path(_MONITOR.name) / 'status.json', job)
    return .5


@persistent
def _reset(_unused):
    global _JOB_KEY
    _RUNNER.cleanup()
    _stop_monitor()
    _JOB_KEY = None


def register():
    _ICONS.register()
    try:
        for cls in (TOONWORKBENCH_OT_submit_workspace, TOONWORKBENCH_OT_cancel_workspace):
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
        if not bpy.app.background:
            bpy.app.handlers.load_pre.append(_reset)
            bpy.app.timers.register(_tick, first_interval=.5, persistent=True)
    except Exception:
        unregister()
        raise


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _reset in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_reset)
    _reset(None)
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED.pop())
    _ICONS.unregister()
