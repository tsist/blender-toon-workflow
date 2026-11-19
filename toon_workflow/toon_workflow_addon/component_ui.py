# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded style/media controls; no Panel, handlers or scene guessing.

Static fields remain owned by style_components. This UI exposes defaults,
validated native readback, and exact component JSON rather than a second schema.
"""
from copy import deepcopy
import json
import math
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty
from mathutils import Vector

from . import component_slots as slots, component_catalog as components

PROP = 'toon_component_surface_ui'
_registered = False
_classes_registered = []
_descriptor = None
_MAX_BYTES = 16 * 1024 * 1024
_PUBLIC_FIELDS = {
    'GAME': ('base_color', 'shadow_color', 'mid_color', 'lit_color',
        'threshold_low', 'threshold_high', 'highlight_strength', 'highlight_threshold',
        'highlight_softness', 'rim_strength','reflection_enabled', 'reflection_strength', 'reflection_roughness'),
    'MANGA': ('pattern_space', 'uv_map', 'black_below', 'hatch_below', 'dot_below',
        'paper_tone', 'ink_tone','light_gain', 'dot_spacing', 'dot_radius_dark', 'dot_radius_light',
        'hatch_spacing', 'hatch_angle_deg', 'hatch_width_deep', 'hatch_width_light'),
    'ALPHA_SHEET': ('color', 'roughness', 'alpha'),
    'GLASS': ('color', 'roughness', 'ior', 'thin_wall','thickness_mode'),
    'WATER': ('color', 'edge_color', 'roughness', 'ripple_scale', 'ripple_strength'),
    'SMOKE': ('color', 'density', 'anisotropy', 'noise_scale', 'threshold'),
    'EMISSION': ('color', 'strength'),
}
_COLOR_FIELDS = {'base_color', 'shadow_color', 'mid_color', 'lit_color', 'color', 'edge_color'}


def _load(settings, values, set_style=True):
    """Only public controls are copied; opaque component settings stay owned."""
    if set_style and settings.style != values['style']:
        settings.style = values['style']
    for key in _PUBLIC_FIELDS[values['style']]:
        if key in values:
            value = values[key]
            if key in _COLOR_FIELDS:
                value = value[:3]  # Component colors have a fixed opaque alpha.
            elif key == 'uv_map' and value is None:
                value = ''
            setattr(settings, key, value)


def _style_changed(settings, context):
    try:
        _load(settings, components.defaults(settings.style), set_style=False)
        settings.message = '已载入此类型默认参数；尚未改动活动槽'
    except Exception as error:
        settings.message = str(error)


def _sun_poll(settings, obj):
    return obj.type == 'LIGHT' and obj.data.type == 'SUN'


class TOONCOMPONENT_Settings(bpy.types.PropertyGroup):
    style: EnumProperty(name='表面类型', items=[
        ('GAME', '游戏表面', '独立游戏表面组件'),
        ('MANGA', '漫画表面', '独立漫画表面组件'),
        ('ALPHA_SHEET', '半透明薄片', '以透明度控制表面'),
        ('GLASS', '玻璃', '原生透射表面'),
        ('WATER', '水面', '表面波纹与透射，不是流体模拟'),
        ('SMOKE', '烟雾', '封闭网格内的静态程序体积'),
        ('EMISSION', '自发光', '原生发光表面')], default='GAME', update=_style_changed)
    base_color: FloatVectorProperty(name='光照基色', subtype='COLOR', size=3, default=(.62,.60,.56), min=0, max=1)
    shadow_color: FloatVectorProperty(name='暗部颜色', subtype='COLOR', size=3, default=(.045,.065,.115), min=0, max=1)
    mid_color: FloatVectorProperty(name='中间色', subtype='COLOR', size=3, default=(.22,.38,.48), min=0, max=1)
    lit_color: FloatVectorProperty(name='亮部颜色', subtype='COLOR', size=3, default=(.82,.84,.80), min=0, max=1)
    threshold_low: FloatProperty(name='暗部阈值', default=.16, min=0, max=1)
    threshold_high: FloatProperty(name='亮部阈值', default=.48, min=0, max=1)
    highlight_strength: FloatProperty(name='高光强度', default=.35, min=0, max=2)
    highlight_threshold: FloatProperty(name='高光阈值', default=.90, min=0, max=1)
    highlight_softness: FloatProperty(name='高光柔度', default=.03, min=.001, max=1)
    rim_strength: FloatProperty(name='边缘光强度', default=.40, min=0, max=2)
    reflection_strength: FloatProperty(name='反射强度', default=0, min=0, max=2)
    reflection_enabled: BoolProperty(name='启用反射',default=False)
    reflection_roughness: FloatProperty(name='反射粗糙度', default=.42, min=.02, max=1)
    pattern_space: EnumProperty(name='网点映射', items=[
        ('WINDOW', '画幅', '按实际渲染像素匹配网点'),
        ('UV', 'UV贴图', '使用明确命名的UV层'),
        ('OBJECT', '对象坐标', '使用对象局部坐标')], default='WINDOW')
    uv_map: StringProperty(name='UV层名称', description='仅UV映射使用；必须是目标上已有的UV层')
    black_below: FloatProperty(name='纯黑阈值', default=.10, min=0, max=1)
    hatch_below: FloatProperty(name='排线阈值', default=.32, min=0, max=1)
    dot_below: FloatProperty(name='网点阈值', default=.82, min=0, max=1)
    paper_tone: FloatProperty(name='纸张明度', default=.94, min=0, max=1)
    light_gain: FloatProperty(name='采光增益',default=1.,min=.05,max=8.)
    ink_tone: FloatProperty(name='墨色明度', default=.04, min=0, max=1)
    dot_spacing: FloatProperty(name='网点间距', default=18, min=.0005, max=256,
        description='画幅模式2–256像素；UV或对象模式0.0005–4坐标单位')
    dot_radius_dark: FloatProperty(name='暗部网点半径', default=.38, min=0, max=.5)
    dot_radius_light: FloatProperty(name='亮部网点半径', default=.10, min=0, max=.5)
    hatch_spacing: FloatProperty(name='排线间距', default=7, min=.0005, max=256,
        description='画幅模式2–256像素；UV或对象模式0.0005–4坐标单位')
    hatch_angle_deg: FloatProperty(name='排线角度（度）', default=45, min=1, max=89)
    hatch_width_deep: FloatProperty(name='深色排线宽度', default=.42, min=.01, max=.85)
    hatch_width_light: FloatProperty(name='浅色排线宽度', default=.16, min=.01, max=.85)
    color: FloatVectorProperty(name='颜色', subtype='COLOR', size=3, default=(.7,.85,1), min=0, max=1)
    edge_color: FloatVectorProperty(name='掠射边缘色', subtype='COLOR', size=3, default=(.09,.43,.46), min=0, max=1)
    roughness: FloatProperty(name='粗糙度', default=.35, min=0, max=1)
    alpha: FloatProperty(name='不透明度', default=.35, min=0, max=1)
    ior: FloatProperty(name='折射率', default=1.45, min=1, max=3)
    thin_wall: BoolProperty(name='薄壁', default=False)
    thickness_mode: EnumProperty(name='厚度近似',items=[('SPHERE','球体','适用于有厚度的圆润实体'),('SLAB','薄板','适用于有厚度的板状几何')],default='SPHERE')
    ripple_scale: FloatProperty(name='波纹密度', default=7, min=.01, max=100)
    ripple_strength: FloatProperty(name='波纹强度', default=.12, min=0, max=.3)
    density: FloatProperty(name='烟雾密度', default=3, min=0, max=20)
    anisotropy: FloatProperty(name='散射方向性', default=.2, min=-.9, max=.9)
    noise_scale: FloatProperty(name='烟雾细节密度', default=3, min=.01, max=100)
    threshold: FloatProperty(name='烟雾裁切阈值', default=.35, min=0, max=.8)
    strength: FloatProperty(name='发光强度', default=2, min=0, max=100)
    sun: PointerProperty(name='光向快照 Sun（可选）', type=bpy.types.Object,
        poll=_sun_poll, description='应用或更新时读取Sun；留空保留组件显式光向，不自动跟随')
    json_path: StringProperty(name='组件参数 JSON', subtype='FILE_PATH',
        description='填写绝对.json路径；导出只创建新文件，不覆盖已有文件')
    message: StringProperty(name='组件结果')


def _settings(context):
    return getattr(context.scene, PROP)


def _target(context):
    target = context.active_object
    if target is None:
        raise ValueError('请选择当前场景的网格和已用活动材质槽')
    index = target.active_material_index
    slots._slot(context.scene, target, index)
    return target, index


def _current(context):
    target, index = _target(context)
    state = slots.inspect(context.scene, target, index)
    if state['status'] != 'valid':
        raise ValueError('活动槽组件冲突：' + '; '.join(state.get('issues', [])))
    return target, index, state


def _finite(value, depth=0):
    if depth > 64:
        raise ValueError('组件JSON嵌套层级超过64')
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return
    if kind is float and math.isfinite(value):
        return
    if kind is list or (kind is dict and all(type(k) is str for k in value)):
        for child in (value.values() if kind is dict else value):
            _finite(child, depth + 1)
        return
    raise ValueError('组件参数必须是有限静态JSON')


def _normalized(settings):
    _finite(settings)
    result = components.normalize(deepcopy(settings))
    if type(result) is not dict:
        raise ValueError('组件normalize必须返回规范dict')
    _finite(result)
    return deepcopy(result)


def _values(context, source):
    """Overlay public controls only, preserving every unexposed source field."""
    settings = _settings(context)
    if settings.style != source['style']:
        raise ValueError('当前槽类型与所选类型不同；更换类型请先恢复，再应用')
    desired = deepcopy(source)
    for key in _PUBLIC_FIELDS[source['style']]:
        if key in desired:
            value = getattr(settings, key)
            desired[key] = list(value) + [1.] if key in _COLOR_FIELDS else value
    if source['style'] == 'MANGA':
        if desired['pattern_space'] != 'UV':
            desired['uv_map'] = None
        if desired['pattern_space'] != 'WINDOW':
            desired['resolution'] = None
    return desired


def _snapshot(context, settings):
    """Explicit one-time evaluated Sun direction and effective render pixels."""
    # Mapping edits may require resolution/uv_map to be adapted before normalize.
    desired = deepcopy(settings)
    _finite(desired)
    sun = _settings(context).sun
    if 'light_direction' in desired and sun is not None:
        if (sun.type != 'LIGHT' or sun.data.type != 'SUN'
                or context.scene.objects.get(sun.name) != sun):
            raise ValueError('光向快照需要本场景的真实Sun对象')
        evaluated = sun.evaluated_get(context.evaluated_depsgraph_get())
        to_light = -(evaluated.matrix_world.to_3x3() @ Vector((0., 0., -1.)))
        if not all(math.isfinite(v) for v in to_light) or to_light.length <= 1e-12:
            raise ValueError('Sun变换不能提供有限非零光向')
        desired['light_direction'] = list(to_light.normalized())
    if 'resolution' in desired and desired.get('pattern_space') == 'WINDOW':
        render = context.scene.render
        desired['resolution'] = [round(render.resolution_x * render.resolution_percentage / 100),
                                 round(render.resolution_y * render.resolution_percentage / 100)]
    return _normalized(desired)


def _result(context, result, note=''):
    settings = _settings(context)
    status = result.get('status')
    if status == 'valid':
        _load(settings, result['settings'])
    labels = {'valid': '组件有效', 'conflict': '检查冲突',
              'restored': '已恢复原槽材质', 'exported': '已导出组件参数',
              'enabled': '已启用本场景透射'}
    detail = '; '.join(result.get('issues', [])) or result.get('material', '')
    settings.message = labels.get(status, str(status)) + (('：' + detail) if detail else '')
    if note:
        settings.message += '；' + note
    return result


_SNAPSHOT_NOTE = '光向与有效输出尺寸为本次快照；灯光变化不会自动跟随'


def apply_current(context):
    target, index = _target(context)
    desired = _snapshot(context, _values(context, components.defaults(_settings(context).style)))
    return _result(context, slots.apply(context.scene, target, index, desired), _SNAPSHOT_NOTE)


def inspect_current(context):
    target, index = _target(context)
    return _result(context, slots.inspect(context.scene, target, index))


def update_current(context):
    target, index, state = _current(context)
    desired = _snapshot(context, _values(context, state['settings']))
    return _result(context, slots.update(context.scene, target, index, desired), _SNAPSHOT_NOTE)


def restore_current(context):
    target, index = _target(context)
    return _result(context, slots.restore(context.scene, target, index))


def enable_transmission_current(context):
    """User-invoked, current-Scene opt-in only; never change global preferences."""
    if context.scene.render.engine != 'BLENDER_EEVEE':
        raise ValueError('本场景透射开关需要EEVEE')
    context.scene.eevee.use_raytracing = True
    return _result(context, dict(status='enabled'), '仅修改当前场景')


def _path(context, existing=False):
    path = Path(_settings(context).json_path)
    if not path.is_absolute() or path.suffix.lower() != '.json' or not path.parent.is_dir():
        raise ValueError('请选择已有父目录内的绝对.json路径')
    if existing and (not path.is_file() or path.stat().st_size > _MAX_BYTES):
        raise ValueError('请选择已存在且不超过16MiB的组件JSON')
    return path


def _unique_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('组件JSON含重复字段：' + key)
        value[key] = child
    return value


def import_current(context):
    path = _path(context, existing=True)
    with path.open('rb') as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('组件JSON超过16MiB')
    def reject(value):
        raise ValueError('组件JSON不能含非有限常量：' + value)
    incoming = _normalized(json.loads(raw.decode('utf-8-sig'), parse_constant=reject,
                                     object_pairs_hook=_unique_object))
    adapted = 'resolution' in incoming and incoming.get('pattern_space') == 'WINDOW'
    if adapted:
        render = context.scene.render
        incoming['resolution'] = [round(render.resolution_x * render.resolution_percentage / 100),
                                  round(render.resolution_y * render.resolution_percentage / 100)]
        incoming = _normalized(incoming)
    target, index = _target(context)
    state = slots.inspect(context.scene, target, index)
    if state['status'] == 'valid':
        if incoming['style'] != state['settings']['style']:
            raise ValueError('更换表面类型前请明确恢复原槽材质，再从JSON应用')
        result = slots.update(context.scene, target, index, incoming)
    else:
        # Slot adapter rejects managed conflicts and protected head/eye/hair
        # bindings; an invalid readback never authorizes their replacement.
        result = slots.apply(context.scene, target, index, incoming)
    note = '已读取文件中的规范静态参数；保留文件光向'
    if adapted:
        note += '；已按当前画幅适配网点'
    return _result(context, result, note)


def export_current(context):
    _, _, state = _current(context)
    desired = _normalized(state['settings'])
    path = _path(context)
    encoded = json.dumps(desired, ensure_ascii=False, indent=2, allow_nan=False).encode('utf8')
    if len(encoded) > _MAX_BYTES:
        raise ValueError('组件JSON超过16MiB')
    with path.open('xb') as stream:
        stream.write(encoded)
    return _result(context, dict(status='exported'), '新文件：' + path.name)


class _Action:
    @classmethod
    def poll(cls, context):
        try:
            _target(context)
            return True
        except (ValueError, AttributeError, TypeError, ReferenceError):
            return False

    def execute(self, context):
        try:
            result = self.action(context)
            if result.get('status') == 'conflict':
                self.report({'WARNING'}, _settings(context).message)
            return {'FINISHED'}
        except Exception as error:
            if hasattr(context.scene, PROP):
                _settings(context).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONCOMPONENT_OT_apply(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.apply'; bl_label = '应用参数到活动槽'; bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(apply_current)


class TOONCOMPONENT_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.inspect'; bl_label = '检查当前组件'; bl_options = {'REGISTER'}
    action = staticmethod(inspect_current)


class TOONCOMPONENT_OT_update(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.update'; bl_label = '更新当前组件'; bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_current)


class TOONCOMPONENT_OT_restore(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.restore'; bl_label = '恢复原槽材质'; bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(restore_current)


class TOONCOMPONENT_OT_import(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.import_json'; bl_label = '从JSON应用／更新'; bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(import_current)


class TOONCOMPONENT_OT_export(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.export_json'; bl_label = '导出当前参数'; bl_options = {'REGISTER'}
    action = staticmethod(export_current)


class TOONCOMPONENT_OT_enable_transmission(_Action, bpy.types.Operator):
    bl_idname = 'toon_component.enable_transmission'; bl_label = '启用本场景透射'; bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(enable_transmission_current)

    @classmethod
    def poll(cls, context):
        scene = getattr(context, 'scene', None)
        return scene is not None and scene.render.engine == 'BLENDER_EEVEE'


def draw_contents(layout, context):
    settings = _settings(context)
    layout.label(text='游戏／漫画表面与介质')
    layout.label(text='仅当前已用槽；保留头脸、眼层与发影绑定')
    target = context.active_object
    if target is not None and target.type == 'MESH':
        layout.label(text=target.name + ' / 槽 ' + str(target.active_material_index))
    layout.prop(settings, 'style')
    if settings.style == 'GAME':
        for key in ('base_color', 'shadow_color', 'mid_color', 'lit_color',
                    'threshold_low', 'threshold_high'):
            layout.prop(settings, key)
        layout.label(text='高光与反射')
        for key in ('highlight_strength', 'highlight_threshold', 'highlight_softness',
                    'rim_strength','reflection_enabled', 'reflection_strength', 'reflection_roughness'):
            layout.prop(settings, key)
        layout.prop(settings, 'sun')
        layout.label(text='Sun留空保留显式光向；点应用／更新才读取')
    elif settings.style == 'MANGA':
        layout.prop(settings, 'pattern_space')
        if settings.pattern_space == 'UV':
            layout.prop(settings, 'uv_map')
        if settings.pattern_space == 'WINDOW':
            layout.label(text='间距单位为像素（2～256）；尺寸自动匹配画幅')
        else:
            layout.label(text='间距使用坐标单位（0.0005～4），请相应调小')
        layout.label(text='换映射需先恢复原槽，再应用')
        for key in ('light_gain','black_below', 'hatch_below', 'dot_below', 'paper_tone', 'ink_tone'):
            layout.prop(settings, key)
        layout.label(text='网点')
        for key in ('dot_spacing', 'dot_radius_dark', 'dot_radius_light'):
            layout.prop(settings, key)
        layout.label(text='排线')
        for key in ('hatch_spacing', 'hatch_angle_deg', 'hatch_width_deep', 'hatch_width_light'):
            layout.prop(settings, key)
    else:
        for key in _PUBLIC_FIELDS[settings.style]:
            layout.prop(settings, key)
        if settings.style in ('GLASS', 'WATER'):
            layout.label(text='玻璃／水请先启用本场景透射')
            layout.operator('toon_component.enable_transmission')
        if settings.style == 'SMOKE':
            layout.label(text='用于封闭网格内的静态体积；非流体模拟')
    row = layout.row(align=True)
    row.operator('toon_component.apply'); row.operator('toon_component.inspect')
    row = layout.row(align=True)
    row.operator('toon_component.update'); row.operator('toon_component.restore')
    layout.label(text='更新保留其余已读参数；光向和画幅为静态快照')
    layout.label(text='原生节点编辑受检查保护；冲突不强覆盖')
    layout.prop(settings, 'json_path')
    row = layout.row(align=True)
    row.operator('toon_component.import_json'); row.operator('toon_component.export_json')
    layout.label(text='导出仅新文件；更换类型请先恢复原槽')
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONCOMPONENT_Settings, TOONCOMPONENT_OT_apply, TOONCOMPONENT_OT_inspect,
    TOONCOMPONENT_OT_update, TOONCOMPONENT_OT_restore, TOONCOMPONENT_OT_import,
    TOONCOMPONENT_OT_export, TOONCOMPONENT_OT_enable_transmission)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Component UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONCOMPONENT_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Component UI property was replaced; preserve its foreign owner')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
