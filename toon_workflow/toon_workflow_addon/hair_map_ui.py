# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded opt-in HM controls, owned by the single Toon workbench."""
import bpy
from bpy.props import FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import hair_map, hair_map_nodes

PROP = 'toon_hair_map_ui'
_registered = False
_classes_registered = []
_descriptor = None


def _sun_poll(_self, obj):
    return obj.type == 'LIGHT' and obj.data is not None and obj.data.type == 'SUN'


class TOONHM_Settings(bpy.types.PropertyGroup):
    # A Scene ID pointer would add an Image consumer and defeat the dedicated
    # R/G owner contract. Resolve the searchable name to its actual ID at each
    # operation, as the existing finish-mask author UI does.
    image_name: StringProperty(name='HM 数据贴图', default='', options=set(),
        description='已有的独立 Non-Color 图像；R控制高光阈值，G控制发束明暗，不更改旧B图像')
    sun: PointerProperty(name='受光参考 Sun', type=bpy.types.Object,
        poll=_sun_poll, options=set(), description='当前场景中唯一受支持的白色 Sun')
    uv_name: StringProperty(name='头发 UV', default='UV贴图', options=set())
    strength: FloatProperty(name='HM 总强度', default=0., min=0., max=1., options=set(),
        description='零时完整旁路HM，恢复原色阶和既有固定发影')
    map_strength: FloatProperty(name='G 明暗塑形', default=1., min=0., max=4., options=set())
    neutral: FloatProperty(name='G 中性灰', default=.5, min=0., max=1., options=set())
    shadow_bias: FloatProperty(name='影界偏移', default=0., min=-1., max=1., options=set())
    shadow_softness: FloatProperty(name='影界过渡', default=.05, min=.001, max=1., options=set())
    highlight_strength: FloatProperty(name='HM 高光强度', default=0., min=0., max=2., options=set())
    highlight_threshold: FloatProperty(name='高光收紧', default=.85, min=0., max=1., options=set())
    highlight_map_strength: FloatProperty(name='R 阈值塑形', default=.2, min=0., max=1., options=set())
    highlight_softness: FloatProperty(name='高光过渡', default=.05, min=.001, max=1., options=set())
    # Alpha is fixed by the opaque kernel; show only editable RGB channels.
    highlight_color: FloatVectorProperty(name='HM 高光颜色', subtype='COLOR', size=3,
        default=(1., .85, .9), min=0., max=1., options=set())
    shadow_color: FloatVectorProperty(name='HM 阴影颜色', subtype='COLOR', size=3,
        default=(.1, .05, .1), min=0., max=1., options=set())
    lit_color: FloatVectorProperty(name='HM 受光颜色', subtype='COLOR', size=3,
        default=(.8, .4, .6), min=0., max=1., options=set())
    highlight_mask_image_name: StringProperty(name='高光遮罩', default='', options=set(),
        description='已有的独立 Non-Color R 遮罩；塑形同一套受光高光，不新增固定发光层')
    highlight_mask_uv_name: StringProperty(name='遮罩UV', default='ToonHairHighlight', options=set())
    mask_black_level: FloatProperty(name='遮罩黑位', default=0., min=0., max=1., options=set())
    mask_white_level: FloatProperty(name='遮罩白位', default=1., min=0., max=1., options=set())
    irradiance_reference: FloatProperty(name='受光基准（随创建派生）', default=1.,
        min=1.e-6, max=1.e6, options=set())
    loaded_owner: StringProperty(options={'SKIP_SAVE'})
    loaded_material: StringProperty(options={'SKIP_SAVE'})
    message: StringProperty(options={'SKIP_SAVE'})


def _settings(context):
    return getattr(context.scene, PROP)


def _context(context):
    scene, obj = context.scene, context.active_object
    if (scene is None or context.mode != 'OBJECT' or obj is None or obj.type != 'MESH'
            or scene.objects.get(obj.name) != obj or obj.library or obj.override_library
            or not obj.is_editable or obj.data is None or obj.data.library
            or obj.data.override_library or not obj.data.is_editable):
        raise ValueError('请在物体模式选择当前场景中的本地可编辑头发网格')
    index = obj.active_material_index
    if (not 0 <= index < len(obj.material_slots)
            or obj.material_slots[index].material is None
            or not any(p.material_index == index for p in obj.data.polygons)):
        raise ValueError('请选择已有面使用的活动头发材质槽')
    return scene, obj, index


def _load(context, report):
    settings = _settings(context)
    for key, value in report['settings'].items():
        setattr(settings, key, value[:3] if key.endswith('_color') else value)
    settings.image_name = report['image']['R']['name']
    settings.sun = context.scene.objects.get(report['sun']['object'])
    settings.uv_name = report['uv_name']
    settings.loaded_owner = report['owner']
    settings.loaded_material = report['material']
    highlight_mask = report.get('highlight_mask')
    if highlight_mask is not None:
        settings.highlight_mask_image_name = highlight_mask['image']['name']
        settings.highlight_mask_uv_name = highlight_mask['uv']['uv_name']
        settings.mask_black_level = highlight_mask['levels']['black']
        settings.mask_white_level = highlight_mask['levels']['white']
    settings.message = '已读取 HM：' + report['material']
    return report


def _values(settings, reference):
    # The readonly reference comes from the validated current layer.  Stale UI
    # values cannot silently retarget a different material or light energy.
    result = {key: list(getattr(settings, key)) + [1.] if key.endswith('_color')
              else getattr(settings, key) for key in hair_map_nodes.defaults()}
    result['irradiance_reference'] = reference
    return hair_map_nodes.normalize(result)


def attach_current(context):
    _context(context)
    settings = _settings(context)
    image = bpy.data.images.get(settings.image_name)
    if image is None:
        raise ValueError('请选择已有的独立 Non-Color HM 数据图像')
    if settings.sun is None:
        raise ValueError('请选择当前场景中的白色 Sun')
    # No settings override: derive both colors from the actual source ramp and
    # keep total/highlight strengths at zero until an explicit later update.
    report = hair_map.attach(context, image, settings.sun, settings.uv_name, settings=None)
    _load(context, report)
    settings.message = '已创建零强度 HM；原配色已读取，编辑后点击应用'
    return report


def read_current(context):
    scene, obj, index = _context(context)
    return _load(context, hair_map.describe(scene, obj, index))


def update_current(context):
    scene, obj, index = _context(context)
    before = hair_map.describe(scene, obj, index)
    settings = _settings(context)
    if settings.loaded_owner != before['owner']:
        raise ValueError('请先读取当前槽的 HM 参数，再编辑并应用')
    image = bpy.data.images.get(settings.image_name)
    if (image is None or image.name != before['image']['R']['name']
            or settings.sun != scene.objects.get(before['sun']['object'])
            or settings.uv_name != before['uv_name']):
        raise ValueError('更换 HM 图像、UV 或 Sun 前请先移除旧层；读取可恢复当前绑定')
    values = _values(settings, before['settings']['irradiance_reference'])
    report = hair_map.update(context, values)
    _load(context, report)
    settings.message = '已应用 HM：' + report['material']
    return report


def remove_current(context):
    _context(context)
    report = hair_map.remove(context)
    settings = _settings(context)
    settings.loaded_owner = ''
    settings.loaded_material = ''
    settings.message = '已移除 HM，恢复原色阶与固定发影接线'
    return report


def mask_attach_current(context):
    scene, obj, index = _context(context)
    hair_map.describe(scene, obj, index)
    settings = _settings(context)
    image = bpy.data.images.get(settings.highlight_mask_image_name)
    if image is None:
        raise ValueError('请选择已有的独立 Non-Color 高光遮罩')
    from . import hair_highlight_mask
    hair_highlight_mask.attach(context, image, settings.highlight_mask_uv_name,
        black_level=settings.mask_black_level, white_level=settings.mask_white_level)
    report = read_current(context)
    settings.message = '已连接统一高光遮罩；遮罩塑形，光照控制显隐'
    return report


def mask_remove_current(context):
    scene, obj, index = _context(context)
    hair_map.describe(scene, obj, index)
    from . import hair_highlight_mask
    hair_highlight_mask.remove(context)
    report = read_current(context)
    _settings(context).message = '已移除统一高光遮罩，保留原 HM 参数'
    return report


class _Action:
    @classmethod
    def poll(cls, context):
        try:
            _context(context)
            return True
        except (ValueError, AttributeError, ReferenceError):
            return False

    def execute(self, context):
        try:
            self.action(context)
            return {'FINISHED'}
        except Exception as error:
            if context.scene is not None and hasattr(context.scene, PROP):
                _settings(context).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONHM_OT_attach(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.attach'
    bl_label = '创建零强度 HM'
    bl_description = '为活动槽明确创建独立HM层，读取原配色；已有B固定发影接缝将显式迁移，移除时恢复'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(attach_current)


class TOONHM_OT_read(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.read'
    bl_label = '读取当前 HM'
    bl_options = {'REGISTER'}
    action = staticmethod(read_current)


class TOONHM_OT_update(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.update'
    bl_label = '应用 HM 参数'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_current)


class TOONHM_OT_remove(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.remove'
    bl_label = '移除并恢复'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(remove_current)


class TOONHM_OT_mask_attach(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.mask_attach'
    bl_label = '连接高光遮罩'
    bl_description = '用独立遮罩塑形同一套受光高光，保留当前强度、颜色与旧 CEL finish'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(mask_attach_current)


class TOONHM_OT_mask_remove(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_map.mask_remove'
    bl_label = '移除高光遮罩'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(mask_remove_current)


def draw_contents(layout, context):
    settings = _settings(context)
    obj = context.active_object
    layout.label(text='活动头发槽：HM 逐发束明暗与高光')
    layout.label(text='EEVEE；单白色 Sun、零环境光、固定能量')
    layout.label(text='先创建零层或读取，再编辑并应用')
    layout.prop_search(settings, 'image_name', bpy.data, 'images')
    layout.prop(settings, 'sun')
    if obj is not None and obj.type == 'MESH' and obj.data is not None:
        layout.prop_search(settings, 'uv_name', obj.data, 'uv_layers')
    else:
        layout.prop(settings, 'uv_name')
    layout.label(text='使用独立 Non-Color R/G 图；保留旧 B 图色彩设置')
    row = layout.row(align=True)
    row.operator('toon_hair_map.attach')
    row.operator('toon_hair_map.read')
    layout.prop(settings, 'strength')
    colors = layout.column(align=True)
    colors.prop(settings, 'shadow_color')
    colors.prop(settings, 'lit_color')
    shadow = layout.box()
    shadow.label(text='G：发束明暗形状')
    for key in ('map_strength', 'neutral', 'shadow_bias', 'shadow_softness'):
        shadow.prop(settings, key)
    highlight = layout.box()
    highlight.label(text='R：高光阈值形状')
    for key in ('highlight_strength', 'highlight_threshold', 'highlight_map_strength',
                'highlight_softness', 'highlight_color'):
        highlight.prop(settings, key)
    highlight.label(text='启用 HM 高光前，关闭原 CEL 高光')
    mask = layout.box()
    mask.label(text='统一高光遮罩')
    mask.prop_search(settings, 'highlight_mask_image_name', bpy.data, 'images')
    if obj is not None and obj.type == 'MESH' and obj.data is not None:
        mask.prop_search(settings, 'highlight_mask_uv_name', obj.data, 'uv_layers')
    else:
        mask.prop(settings, 'highlight_mask_uv_name')
    mask_levels = mask.row(align=True)
    mask_levels.prop(settings, 'mask_black_level')
    mask_levels.prop(settings, 'mask_white_level')
    mask.label(text='遮罩塑形，光照控制显隐；只使用一套高光')
    mask.label(text='更换图像、UV 或黑白位前，先移除遮罩')
    mask_row = mask.row(align=True)
    mask_row.operator('toon_hair_map.mask_attach')
    mask_row.operator('toon_hair_map.mask_remove')
    reference = layout.row()
    reference.enabled = False
    reference.prop(settings, 'irradiance_reference')
    layout.label(text='更换贴图、UV、Sun 或能量时先移除再创建')
    layout.label(text='旧 B 接缝随创建明确迁移；移除 HM 时恢复')
    row = layout.row(align=True)
    row.operator('toon_hair_map.update')
    row.operator('toon_hair_map.remove')
    if settings.loaded_material:
        layout.label(text='已读取：' + settings.loaded_material)
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONHM_Settings, TOONHM_OT_attach, TOONHM_OT_read,
           TOONHM_OT_update, TOONHM_OT_remove, TOONHM_OT_mask_attach,
           TOONHM_OT_mask_remove)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('HM UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONHM_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('HM UI property was replaced; preserve its foreign owner')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
