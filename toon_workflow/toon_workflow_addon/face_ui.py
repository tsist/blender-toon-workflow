# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit face-threshold authoring UI; embedded only, with native UNDO."""
import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

PROP = 'toon_face_threshold_ui'
_registered = False
_descriptor = None
_classes_registered = []


def _core():
    from . import face_thresholds
    return face_thresholds


def _target(context):
    target = context.object
    if context.mode != 'OBJECT' or target is None or target.type != 'MESH':
        raise ValueError('请选择对象模式下的活动网格')
    if target.library or target.override_library or not target.is_editable:
        raise ValueError('面影只修改本地可编辑网格对象')
    return target


def _edit_target(context):
    target = context.object
    if context.mode != 'EDIT_MESH' or target is None or target.type != 'MESH':
        raise ValueError('阈值写入仅支持活动网格的编辑模式')
    if target.library or target.override_library or not target.is_editable:
        raise ValueError('面影图只编辑本地可编辑网格')
    if target.data.library or target.data.users != 1 or len(context.objects_in_mode_unique_data) != 1:
        raise ValueError('请仅编辑一个独占Mesh；共享Mesh请先在对象模式初始化')
    return target


def _managed(target):
    matches = [m for m in target.modifiers if _core().is_managed(m)]
    if len(matches) != 1:
        raise ValueError('目标须有且仅有一个受管面影字段')
    return matches[0]


def create_attributes(context):
    target = _target(context); settings = getattr(context.scene, PROP)
    names = (settings.left_attribute, settings.right_attribute, settings.weight_attribute)
    original = target.data
    copied = original.users > 1 or original.library is not None
    existing = set(original.attributes.keys())
    mesh = original.copy() if copied else original
    try:
        if copied:
            target.data = mesh
        result = _core().initialize(target, *names)
    except Exception:
        if copied:
            target.data = original
            if mesh.users != 0:
                raise RuntimeError('本次副本出现其它消费者，保留副本并停止')
            bpy.data.meshes.remove(mesh)
        else:
            for name in set(names) - existing:
                attr = mesh.attributes.get(name)
                if attr is not None:
                    mesh.attributes.remove(attr)
        raise
    return dict(result, copied_mesh=copied)


def create_field(context):
    target = _target(context); settings = getattr(context.scene, PROP)
    return _core().build(target, settings.reference, settings.light, settings.left_attribute,
                         settings.right_attribute, settings.weight_attribute, strength=0.,
                         side_blend=settings.side_blend, vertical_fade=settings.vertical_fade)


def apply_configuration(context):
    target = _target(context); settings = getattr(context.scene, PROP)
    core = _core(); modifier = _managed(target)
    fade = 0. if modifier.node_group.get(core.PROFILE_KEY) in (core.POLAR_PROFILE,core.DIRECTION_PROFILE,core.CONTINUITY_PROFILE) else settings.vertical_fade
    return core.configure(target, modifier, reference=settings.reference, light=settings.light,
        left_attribute=settings.left_attribute, right_attribute=settings.right_attribute,
        weight_attribute=settings.weight_attribute, strength=settings.strength,
        side_blend=settings.side_blend, vertical_fade=fade)


def inspect_field(context):
    target = _target(context)
    return _core().inspect(target, _managed(target), depsgraph=context.evaluated_depsgraph_get())


def write_selected(context):
    import bmesh
    target = _edit_target(context)
    mesh = bmesh.from_edit_mesh(target.data); mesh.verts.index_update()
    indices = [v.index for v in mesh.verts if v.select]
    if not indices:
        raise ValueError('没有选中顶点；不会写入整个网格')
    settings = getattr(context.scene, PROP)
    name = {'LEFT': settings.left_attribute, 'RIGHT': settings.right_attribute,
            'WEIGHT': settings.weight_attribute}[settings.paint_field]
    value, blend = settings.paint_value, settings.paint_blend
    bpy.ops.object.mode_set(mode='OBJECT')
    try:
        return _core().write_values(target, name, indices, value, blend=blend)
    finally:
        context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode='EDIT')


def bind_material(context):
    from . import face_material
    target = _target(context); settings = getattr(context.scene, PROP)
    return face_material.bind(target, _managed(target), shadow_color=tuple(settings.shadow_color), softness=settings.softness)


def unbind_material(context):
    from . import face_material
    target = _target(context)
    return face_material.unbind(target, _managed(target))


class TOONFACE_Settings(bpy.types.PropertyGroup):
    reference: PointerProperty(name='头部方向参考', type=bpy.types.Object)
    light: PointerProperty(name='面影方向太阳', type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'LIGHT' and obj.data.type == 'SUN')
    left_attribute: StringProperty(name='左向阈值图', default='toon_face_left_threshold')
    right_attribute: StringProperty(name='右向阈值图', default='toon_face_right_threshold')
    weight_attribute: StringProperty(name='面影区域权重', default='toon_face_shadow_weight')
    strength: FloatProperty(name='待应用强度', default=0., min=0., max=1.)
    side_blend: FloatProperty(name='左右过渡范围', default=.05, min=0., max=1.,
        description='控制方向转过正面时左右图的过渡；左右按参考+X/-X分侧，不代表解剖左右；0为硬选侧')
    vertical_fade: FloatProperty(name='顶光淡出范围', default=.1, min=0., max=1.,
        description='控制方向接近顶光时额外面影的淡出；0仅保留竖直方向安全回退')
    shadow_color: FloatVectorProperty(name='阴影颜色', size=4, subtype='COLOR', default=(.45, .34, .31, 1.), min=0., max=1.)
    softness: FloatProperty(name='阈值柔和度', default=.02, min=0., max=1.)
    paint_field: EnumProperty(name='所选写入图', items=(('LEFT', '左向阈值', ''), ('RIGHT', '右向阈值', ''), ('WEIGHT', '区域权重', '')), default='LEFT')
    paint_value: FloatProperty(name='写入数值', default=.5, min=0., max=1.)
    paint_blend: FloatProperty(name='写入混合', default=1., min=0., max=1.)
    message: StringProperty(name='检查结果')


class _Action:
    @classmethod
    def poll(cls, context):
        try:
            _target(context)
            return True
        except (ValueError, AttributeError):
            return False

    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            result = self.action(context)
            if isinstance(result, dict) and result.get('status') == 'invalid':
                raise ValueError('; '.join(result.get('issues', ['面影字段无效'])))
            settings.message = self.bl_label + '完成'
            if isinstance(result, dict) and 'attributes' in result and 'strength' in result:
                settings.message += '；强度 %.6g' % result['strength']
            self.report({'INFO'}, settings.message)
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error); self.report({'ERROR'}, settings.message)
            return {'CANCELLED'}


class TOONFACE_OT_attributes(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.create_attributes'; bl_label = '创建缺失阈值图与零权重（保留已有值）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_attributes)


class TOONFACE_OT_build(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.create_field'; bl_label = '创建面影字段（默认0）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_field)


class TOONFACE_OT_apply(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.apply_configuration'; bl_label = '显式应用面影配置'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(apply_configuration)


class TOONFACE_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.inspect_field'; bl_label = '检查面影字段'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(inspect_field)


class TOONFACE_OT_write(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.write_selected'; bl_label = '仅写入所选顶点'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(write_selected)

    @classmethod
    def poll(cls, context):
        try:
            _edit_target(context)
            return True
        except (ValueError, AttributeError):
            return False


class TOONFACE_OT_bind(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.bind_material'; bl_label = '绑定活动材质Emission.Color'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(bind_material)


class TOONFACE_OT_unbind(_Action, bpy.types.Operator):
    bl_idname = 'toon_face.unbind_material'; bl_label = '解绑面影（精确恢复原输入）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(unbind_material)


CLASSES = (TOONFACE_Settings, TOONFACE_OT_attributes, TOONFACE_OT_build, TOONFACE_OT_apply,
           TOONFACE_OT_inspect, TOONFACE_OT_write, TOONFACE_OT_bind, TOONFACE_OT_unbind)


def draw_contents(layout, context):
    settings = getattr(context.scene, PROP)
    layout.label(text='方向阈值图是额外压暗层，保留原生较暗发影')
    layout.label(text='不能提亮原暗面；选择太阳仅控制面影方向')
    layout.label(text='不是多灯解算或完整SDF；默认强度0')
    for name in ('reference', 'light', 'left_attribute', 'right_attribute', 'weight_attribute'):
        layout.prop(settings, name)
    layout.operator('toon_face.create_attributes'); layout.operator('toon_face.create_field')
    polar = False
    try:
        from . import face_thresholds
        target = context.active_object
        polar = target is not None and any(face_thresholds.is_managed(m) and
            m.node_group.get(face_thresholds.PROFILE_KEY) in (face_thresholds.POLAR_PROFILE,face_thresholds.DIRECTION_PROFILE,face_thresholds.CONTINUITY_PROFILE) for m in target.modifiers)
    except (AttributeError, TypeError):
        pass
    for name in ('strength', 'side_blend') + (() if polar else ('vertical_fade',)):
        layout.prop(settings, name)
    if polar:
        layout.label(text='三维面影保留上下光响应，无顶底淡出')
    layout.label(text='左右按参考+X/-X分侧，不代表解剖左右')
    layout.operator('toon_face.apply_configuration')
    layout.operator('toon_face.inspect_field')
    box = layout.box()
    for name in ('paint_field', 'paint_value', 'paint_blend'):
        box.prop(settings, name)
    box.operator('toon_face.write_selected')
    box.label(text='仅单网格编辑模式的明确选区；不自动铺满脸部')
    for name in ('shadow_color', 'softness'):
        layout.prop(settings, name)
    layout.operator('toon_face.bind_material'); layout.operator('toon_face.unbind_material')
    layout.label(text='逐个活动独占材质绑定；保留Surface、Alpha和Normal链接')
    if settings.message:
        layout.label(text=settings.message)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Scene面影UI属性已被其它注册拥有')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls); _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONFACE_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer(); _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Scene面影UI属性已被替换，保留其它所有者')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear(); _registered = False
