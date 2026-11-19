# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit native-media Beauty plus artistic line overlay controls."""
import json
import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty
from . import line_optics

PROP = 'toon_native_media'
SCHEMA = 'toon-native-media-settings.v1'
_registered = False
_classes_registered = []
_descriptor = None


def normalize_snapshot(value):
    from .media_render import normalize_settings
    return normalize_settings(value)


class TOONMEDIA_Object(bpy.types.PropertyGroup):
    object: PointerProperty(name='媒体对象', type=bpy.types.Object,
                            poll=lambda self, obj: obj.type in ('MESH', 'VOLUME'))


class TOONMEDIA_Optics(bpy.types.PropertyGroup):
    mode: EnumProperty(name='艺术线光学', items=[('SHARP','清晰','保持原线'),('DEFOCUS','景深','统一艺术深度'),
                      ('VECTOR','运动模糊','显式原生四通道像素速度'),('BOTH','两者','运动模糊后景深')], default='SHARP')
    depth: FloatProperty(name='艺术线统一深度', min=.000001, default=5.)
    focus_distance: FloatProperty(name='艺术线焦距距离', min=.000001, default=5.)
    fstop: FloatProperty(name='艺术线光圈', min=.000001, default=2.8)
    max_blur_px: FloatProperty(name='Defocus最大模糊（像素）', min=0, max=256, default=16.)
    velocity_px: FloatVectorProperty(name='原生四通道像素速度', size=4, min=-512, max=512, default=(0,0,0,0))
    shutter: FloatProperty(name='艺术线快门', min=0, max=2, default=.5)
    samples: IntProperty(name='艺术线运动采样', min=1, max=256, default=32)


class TOONMEDIA_Settings(bpy.types.PropertyGroup):
    enabled: BoolProperty(name='原生媒体与艺术线输出', default=False)
    line_mode: EnumProperty(name='艺术线参与', items=[('OVERLAY','艺术叠层','媒体对象不参与线源与不透明遮挡'),
                           ('EXCLUDE','排除艺术线','仅使用原生Beauty')], default='OVERLAY')
    line_optics: PointerProperty(type=TOONMEDIA_Optics)
    media_objects: CollectionProperty(type=TOONMEDIA_Object)


def snapshot(scene):
    settings = getattr(scene, PROP, None)
    if settings is None or not settings.enabled:
        return None
    objects = []
    pointers = set()
    for item in settings.media_objects:
        obj = item.object
        if (obj is None or not obj.as_pointer() or scene.objects.get(obj.name) != obj
                or obj.type not in ('MESH', 'VOLUME') or obj.library or obj.override_library):
            raise ValueError('媒体绑定必须是本Scene的真实本地Mesh/Volume Object')
        if obj.as_pointer() in pointers:
            raise ValueError('媒体Object绑定重复')
        pointers.add(obj.as_pointer())
        objects.append(obj.name)
    optics = {key: list(getattr(settings.line_optics, key)) if key == 'velocity_px'
              else getattr(settings.line_optics, key) for key in line_optics.DEFAULTS}
    return normalize_snapshot(dict(schema=SCHEMA, enabled=True, line_mode=settings.line_mode,
                                   line_optics=optics, media_objects=sorted(objects)))


class TOONMEDIA_OT_add(bpy.types.Operator):
    bl_idname = 'toon_workflow.native_media_add'
    bl_label = '将活动媒体对象加入列表'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.active_object is not None and context.active_object.type in ('MESH','VOLUME')

    def execute(self, context):
        obj = context.active_object
        if obj.library or obj.override_library or context.scene.objects.get(obj.name) != obj:
            self.report({'ERROR'}, '需要本Scene真实本地媒体对象'); return {'CANCELLED'}
        settings = getattr(context.scene, PROP)
        if any(item.object == obj for item in settings.media_objects):
            self.report({'WARNING'}, '媒体对象已绑定'); return {'CANCELLED'}
        settings.media_objects.add().object = obj
        return {'FINISHED'}


class TOONMEDIA_OT_remove(bpy.types.Operator):
    bl_idname = 'toon_workflow.native_media_remove'
    bl_label = '移除媒体对象绑定'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty(default=-1)

    def execute(self, context):
        objects = getattr(context.scene, PROP).media_objects
        if not 0 <= self.index < len(objects): return {'CANCELLED'}
        objects.remove(self.index)
        return {'FINISHED'}


def draw_contents(layout, context):
    settings = getattr(context.scene, PROP)
    layout.prop(settings, 'enabled')
    if not settings.enabled: return
    layout.label(text='Beauty使用原生玻璃、水、烟、透明、发光及相机景深/运动模糊')
    layout.label(text='Line为艺术叠层，不计算物理透射、折射或体积传输')
    layout.prop(settings, 'line_mode')
    if settings.line_mode == 'OVERLAY':
        box = layout.box()
        box.label(text='采Line时仅隐藏下列真实媒体对象；Beauty保留它们')
        for index, item in enumerate(settings.media_objects):
            row = box.row(align=True); row.prop(item, 'object', text='')
            row.operator(TOONMEDIA_OT_remove.bl_idname, text='', icon='X').index = index
        box.operator(TOONMEDIA_OT_add.bl_idname, icon='ADD')
        for key in line_optics.DEFAULTS: layout.prop(settings.line_optics, key)
        layout.label(text='Line使用统一艺术深度/速度，不自动跟随物体或相机运动')


def register():
    global _registered, _descriptor
    if _registered: return
    if hasattr(bpy.types.Scene, PROP): raise RuntimeError('Native-media Scene property has another owner')
    try:
        for cls in (TOONMEDIA_Object, TOONMEDIA_Optics, TOONMEDIA_Settings, TOONMEDIA_OT_add, TOONMEDIA_OT_remove):
            bpy.utils.register_class(cls); _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONMEDIA_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister(); raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Native-media Scene property owner changed')
    if descriptor is not None: delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered): bpy.utils.unregister_class(cls)
    _classes_registered.clear(); _registered = False
