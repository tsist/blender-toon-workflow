# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded eye layer and fixed-hair-shadow controls; no standalone panel."""
import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import eye_overlay_nodes as eye_nodes, eye_overlays, hair_fixed_shadow

PROP = 'toon_eye_overlay_ui'
_registered = False
_classes_registered = []
_descriptor = None


class TOONEYE_Settings(bpy.types.PropertyGroup):
    kind: EnumProperty(name='眼层用途', items=[
        ('SHADOW', '眼部叠底阴影', '半透明着色，保留后方眼白与虹膜'),
        ('GLINT', '瞳孔柔边高光', '现有高光小片的柔边白色高光')],
        default='SHADOW', options=set())
    opacity: FloatProperty(name='不透明度', default=.35, min=0., max=1., options=set())
    tint: FloatVectorProperty(name='叠底颜色', subtype='COLOR', size=3,
        default=(.7, .7, .7), min=0., max=1., options=set())
    glint_color: FloatVectorProperty(name='高光颜色', subtype='COLOR', size=3,
        default=(1., .98, .96), min=0., max=1., options=set())
    feather: FloatProperty(name='高光边缘过渡', default=.18, min=.0001, max=1., options=set())
    uv_name: StringProperty(name='高光 UV', default='UV贴图', options=set())
    center: FloatVectorProperty(name='遮罩中心', size=2,
        default=(.1092628, .8924619), min=-1.e6, max=1.e6, options=set())
    radius: FloatVectorProperty(name='遮罩半径', size=2,
        default=(.0862841, .0830364), min=.000001, max=1.e6, options=set())
    hair_image: PointerProperty(name='固定阴影遮罩', type=bpy.types.Image, options=set())
    hair_uv_name: StringProperty(name='头发 UV', default='UV贴图', options=set())
    hair_strength: FloatProperty(name='固定阴影强度', default=0., min=0., max=1., options=set())
    hair_tint: FloatVectorProperty(name='固有阴影叠底色', subtype='COLOR', size=3,
        default=(.65, .55, .65), min=0., max=1., options=set())
    message: StringProperty(name='材质检查', options={'SKIP_SAVE'})


def _settings(context):
    return getattr(context.scene, PROP)


def _context(context):
    scene, obj = context.scene, context.active_object
    if (scene is None or scene.library or scene.override_library or not scene.is_editable
            or context.mode != 'OBJECT' or obj is None or obj.type != 'MESH'
            or scene.objects.get(obj.name) != obj or obj.library or obj.override_library
            or not obj.is_editable or obj.data is None or obj.data.library
            or obj.data.override_library or not obj.data.is_editable):
        raise ValueError('请在物体模式选择当前场景中的本地可编辑网格')
    if obj.data.users > 1:
        raise ValueError('此网格被多个对象共用，请先建立独立工作副本')
    index = obj.active_material_index
    if not 0 <= index < len(obj.material_slots):
        raise ValueError('请选择已有的活动材质槽')
    if not any(polygon.material_index == index for polygon in obj.data.polygons):
        raise ValueError('活动槽没有使用中的面，请明确选择目标槽')
    return scene, obj, index, obj.material_slots[index]


def _exclusive_material(context):
    scene, obj, index, slot = _context(context)
    material = slot.material
    if material is None or material.library or material.override_library or not material.is_editable:
        raise ValueError('当前槽需要本地可编辑材质')
    consumers = [(candidate, number) for candidate in bpy.data.objects if candidate.type == 'MESH'
                 for number, candidate_slot in enumerate(candidate.material_slots)
                 if candidate_slot.material == material]
    if consumers != [(obj, index)] or material.use_extra_user or material.users - int(material.use_fake_user) > 1:
        raise ValueError('此材质还被其它槽或对象共用，请先建立独立材质副本')
    return scene, obj, index, material


def _eye_values(settings):
    if settings.kind == 'SHADOW':
        return dict(opacity=settings.opacity, tint=list(settings.tint))
    return dict(opacity=settings.opacity, uv_name=settings.uv_name,
                center=list(settings.center), radius=list(settings.radius),
                feather=settings.feather, color=list(settings.glint_color))


def _check_uv(obj, name):
    if not name or not obj.data.uv_layers.get(name):
        raise ValueError('目标网格没有所选 UV，请明确选择已有 UV 层')


def _load_eye(context, report):
    settings = _settings(context)
    settings.kind = report['kind']
    for key, value in report['settings'].items():
        setattr(settings, 'glint_color' if key == 'color' else key, value)
    settings.message = '眼层材质已检查：' + report['material']
    return report


def create_current(context):
    """Replace only the active used slot and atomically extend its declaration."""
    scene, obj, index, slot = _context(context)
    settings = _settings(context)
    rows = eye_overlays.collect(scene)
    if any(row['target'] == obj.name and row['slot'] == index for row in rows):
        raise ValueError('当前槽已登记眼层，请更新现有材质')
    if len(rows) >= 8:
        raise ValueError('当前场景已登记八个眼层槽')
    if settings.kind == 'GLINT':
        _check_uv(obj, settings.uv_name)
    bindings = [dict(object=scene.objects[row['target']], slot=row['slot']) for row in rows]
    bindings.append(dict(object=obj, slot=index))
    old_material = slot.material
    had_declaration = eye_overlays.KEY in scene
    old_declaration = scene.get(eye_overlays.KEY)
    targets = list(dict.fromkeys(binding['object'] for binding in bindings))
    object_metadata = [(target, eye_overlays.OBJECT_ID in target,
                        target.get(eye_overlays.OBJECT_ID)) for target in targets]
    material = None
    try:
        material = eye_nodes.create(settings.kind,
            ('眼部叠底阴影' if settings.kind == 'SHADOW' else '瞳孔柔边高光') + ' / ' + obj.name,
            **_eye_values(settings))
        # Retain the old ID through the new material instead of mutating its
        # fake-user flag; an otherwise zero-user old material survives saving.
        if old_material is not None:
            material['toon_eye_overlay_original_material'] = old_material
        eye_overlays.remove(scene)
        slot.material = material
        eye_overlays.configure(scene, bindings)
        report = _load_eye(context, eye_nodes.validate(material))
        settings.message = '已替换活动槽并开启受管眼层：' + material.name
        return report
    except Exception as error:
        rollback_errors = []
        try:
            slot.material = old_material
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        try:
            if had_declaration:
                scene[eye_overlays.KEY] = old_declaration
            elif eye_overlays.KEY in scene:
                del scene[eye_overlays.KEY]
            for target, existed, value in object_metadata:
                if existed:
                    target[eye_overlays.OBJECT_ID] = value
                elif eye_overlays.OBJECT_ID in target:
                    del target[eye_overlays.OBJECT_ID]
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if material is not None and material.users == 0:
            try:
                bpy.data.materials.remove(material)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError('创建失败，部分恢复未完成：' + '; '.join(rollback_errors)) from error
        raise


def update_current(context):
    scene, obj, index, material = _exclusive_material(context)
    rows = eye_overlays.collect(scene)
    if not any(row['target'] == obj.name and row['slot'] == index for row in rows):
        raise ValueError('当前槽没有有效的受管眼层登记')
    before = eye_nodes.validate(material)
    settings = _settings(context)
    if before['kind'] != settings.kind:
        raise ValueError('眼层用途与当前材质不同，请先读取当前参数')
    if settings.kind == 'GLINT':
        _check_uv(obj, settings.uv_name)
    try:
        report = eye_nodes.update(material, **_eye_values(settings))
        eye_overlays.collect(scene)
    except Exception:
        eye_nodes.update(material, **before['settings'])
        raise
    return _load_eye(context, report)


def read_current(context):
    scene, obj, index, material = _exclusive_material(context)
    rows = eye_overlays.collect(scene)
    if not any(row['target'] == obj.name and row['slot'] == index for row in rows):
        raise ValueError('当前槽没有有效的受管眼层登记')
    return _load_eye(context, eye_nodes.validate(material))


def _load_hair(context, report):
    settings = _settings(context)
    if 'strength' in report:
        settings.hair_strength = report['strength']
        settings.hair_uv_name = report['uv_name']
        settings.hair_image = bpy.data.images.get(report['image']['name'])
        if 'tint' in report:
            settings.hair_tint = report['tint']
    settings.message = ('固定阴影已移除：' if report.get('restored') else '固定阴影已检查：') + report['material']
    if report['profile'] == hair_fixed_shadow.LEGACY_PROFILE and not report.get('restored'):
        settings.message += '；旧 v1 光照门控，需先移除再明确添加 v2 叠底'
    return report


def attach_hair_current(context):
    _, obj, _, material = _exclusive_material(context)
    settings = _settings(context)
    if settings.hair_image is None:
        raise ValueError('请选择已有的固定阴影遮罩图像')
    _check_uv(obj, settings.hair_uv_name)
    return _load_hair(context, hair_fixed_shadow.attach(material, settings.hair_image,
        uv_name=settings.hair_uv_name, strength=settings.hair_strength, tint=list(settings.hair_tint)))


def update_hair_current(context):
    _, obj, _, material = _exclusive_material(context)
    before = hair_fixed_shadow.validate(material)
    settings = _settings(context)
    _check_uv(obj, before['uv_name'])
    if (settings.hair_image != bpy.data.images.get(before['image']['name'])
            or settings.hair_uv_name != before['uv_name']):
        if before['profile'] == hair_fixed_shadow.LEGACY_PROFILE:
            raise ValueError('旧v1更换遮罩或 UV 前请先移除效果；读取按钮可载入当前参数')
        raise ValueError('更换遮罩或 UV 请使用“替换固定影图像 / UV”；读取按钮可载入当前参数')
    if before['profile'] == hair_fixed_shadow.LEGACY_PROFILE:
        report = hair_fixed_shadow.update_strength(material, settings.hair_strength)
    else:
        report = hair_fixed_shadow.update(material, strength=settings.hair_strength,
                                         tint=list(settings.hair_tint))
    return _load_hair(context, report)


def read_hair_current(context):
    _, obj, _, material = _exclusive_material(context)
    report = hair_fixed_shadow.validate(material)
    _check_uv(obj, report['uv_name'])
    return _load_hair(context, report)


def replace_hair_source(context):
    _, obj, _, material = _exclusive_material(context)
    settings = _settings(context)
    _check_uv(obj, settings.hair_uv_name)
    if settings.hair_image is None:
        raise ValueError('请先选择已保存或打包的独立固定影贴图')
    report = hair_fixed_shadow.replace_source(material, image=settings.hair_image,
                                               uv_name=settings.hair_uv_name)
    return _load_hair(context, report)


def remove_hair_current(context):
    _, _, _, material = _exclusive_material(context)
    return _load_hair(context, hair_fixed_shadow.remove(material))


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


class TOONEYE_OT_create(_Action, bpy.types.Operator):
    bl_idname = 'toon_eye_overlay.create'
    bl_label = '新建并替换活动槽'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_current)


class TOONEYE_OT_update(_Action, bpy.types.Operator):
    bl_idname = 'toon_eye_overlay.update'
    bl_label = '更新当前眼层参数'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_current)


class TOONEYE_OT_read(_Action, bpy.types.Operator):
    bl_idname = 'toon_eye_overlay.read'
    bl_label = '读取当前眼层参数'
    bl_options = {'REGISTER'}
    action = staticmethod(read_current)


class TOONHAIRSHADOW_OT_attach(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_fixed_shadow.attach'
    bl_label = '添加固定阴影'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(attach_hair_current)


class TOONHAIRSHADOW_OT_update(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_fixed_shadow.update'
    bl_label = '更新固定阴影参数'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_hair_current)


class TOONHAIRSHADOW_OT_read(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_fixed_shadow.read'
    bl_label = '读取固定阴影参数'
    bl_options = {'REGISTER'}
    action = staticmethod(read_hair_current)


class TOONHAIRSHADOW_OT_remove(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_fixed_shadow.remove'
    bl_label = '移除固定阴影'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(remove_hair_current)


class TOONHAIRSHADOW_OT_replace_source(_Action, bpy.types.Operator):
    bl_idname = 'toon_hair_fixed_shadow.replace_source'
    bl_label = '替换固定影图像 / UV'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(replace_hair_source)


def draw_contents(layout, context):
    settings = _settings(context)
    obj = context.active_object
    material = None
    layout.label(text='当前活动槽：眼层与头发固定阴影')
    if obj is not None and obj.type == 'MESH':
        layout.label(text=obj.name + ' / 槽 ' + str(obj.active_material_index))
        if 0 <= obj.active_material_index < len(obj.material_slots):
            material = obj.material_slots[obj.active_material_index].material
            layout.label(text=material.name if material else '空材质槽')
    layout.label(text='透明仅用于受管眼层，无折射')
    try:
        rows = eye_overlays.collect(context.scene)
        if rows:
            layout.label(text='受管眼层已开启：' + str(len(rows)) + ' 个槽')
    except (ValueError, TypeError, AttributeError) as error:
        layout.label(text='眼层登记需修复：' + str(error))
    box = layout.box()
    box.prop(settings, 'kind')
    box.prop(settings, 'opacity')
    if settings.kind == 'SHADOW':
        box.prop(settings, 'tint')
    else:
        box.prop(settings, 'glint_color')
        box.prop(settings, 'feather')
        if obj is not None and obj.type == 'MESH':
            box.prop_search(settings, 'uv_name', obj.data, 'uv_layers')
        else:
            box.prop(settings, 'uv_name')
        box.prop(settings, 'center')
        box.prop(settings, 'radius')
    box.label(text='新建会替换此槽；原材质保留')
    box.operator('toon_eye_overlay.create')
    row = box.row(align=True)
    row.operator('toon_eye_overlay.update')
    row.operator('toon_eye_overlay.read')
    hair = layout.box()
    hair.label(text='头发固有阴影：与动态阴影正片叠底')
    hair.prop(settings, 'hair_image')
    if obj is not None and obj.type == 'MESH':
        hair.prop_search(settings, 'hair_uv_name', obj.data, 'uv_layers')
    else:
        hair.prop(settings, 'hair_uv_name')
    hair.prop(settings, 'hair_strength')
    if material is not None and material.get(hair_fixed_shadow.PROFILE_KEY) == hair_fixed_shadow.LEGACY_PROFILE:
        hair.label(text='当前为旧 v1 光照门控，仅更新强度')
        hair.label(text='使用叠底前，先移除旧层，再添加 v2')
    else:
        hair.prop(settings, 'hair_tint')
    hair.label(text='保留原法向、色阶与高光；默认强度为零')
    row = hair.row(align=True)
    row.operator('toon_hair_fixed_shadow.attach')
    row.operator('toon_hair_fixed_shadow.update')
    row = hair.row(align=True)
    row.operator('toon_hair_fixed_shadow.read')
    row.operator('toon_hair_fixed_shadow.remove')
    hair.operator('toon_hair_fixed_shadow.replace_source')
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONEYE_Settings, TOONEYE_OT_create, TOONEYE_OT_update, TOONEYE_OT_read,
           TOONHAIRSHADOW_OT_attach, TOONHAIRSHADOW_OT_update, TOONHAIRSHADOW_OT_read,
           TOONHAIRSHADOW_OT_remove, TOONHAIRSHADOW_OT_replace_source)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Eye overlay UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONEYE_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Eye overlay UI property was replaced; preserve its foreign owner')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
