# SPDX-License-Identifier: GPL-3.0-or-later
"""Workstation entry for the Blender 5.3 native Toon controls."""
import bpy

from . import lighting_53 as lighting


_registered = False
_classes_registered = []
_descriptor = None
PROP = 'toon53_workbench'
LABELS = {
    'toon53_palette': '统一色板',
    'toon53_palette_mix': '色板影响',
    'toon53_exposure': '整体明度',
    'toon53_shadow': '脸部暗色',
    'toon53_shadow_mix': '暗色影响',
    'toon53_key_strength': '主光提亮',
    'toon53_fill_strength': '副光染色',
    'toon53_highlight': '发丝亮色',
    'toon53_highlight_strength': '亮带强度',
    'toon53_highlight_shift': '亮带移动上限',
    'toon53_face_threshold': '脸影界线',
    'toon53_face_feather': '脸影过渡',
}


class TOON53_PG_authoring(bpy.types.PropertyGroup):
    key: bpy.props.PointerProperty(name='主光', type=bpy.types.Object,
                                   poll=lambda self, obj: obj.type == 'LIGHT' and obj.data.type == 'SUN')
    fill: bpy.props.PointerProperty(name='副光', type=bpy.types.Object,
                                    poll=lambda self, obj: obj.type == 'LIGHT')
    role: bpy.props.EnumProperty(name='使用方式', items=[
        (lighting.ROLE_FACE, '面部', '复用现有 SDF 并加入可调整刘海影'),
        (lighting.ROLE_HAIR, '发束', '为已画遮罩添加随主光与视角响应的亮带'),
        (lighting.ROLE_COLOR, '校色', '仅继承项目色板和明暗'),
    ])
    mask: bpy.props.PointerProperty(name='亮带遮罩', type=bpy.types.Image)
    uv: bpy.props.StringProperty(name='遮罩 UV')


class TOON53_OT_save(bpy.types.Operator):
    bl_idname = 'toon53.save_upgrade'
    bl_label = '另存 5.3 升级工程'
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default='*.blend', options={'HIDDEN'})

    def invoke(self, context, event):
        if bpy.data.filepath:
            from pathlib import Path
            source = Path(bpy.data.filepath)
            self.filepath = str(source.with_name(source.stem + '-5.3.blend'))
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            lighting.save_upgrade(bpy.path.abspath(self.filepath))
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, '已另存 5.3 工作副本')
        return {'FINISHED'}


class TOON53_OT_prepare(bpy.types.Operator):
    bl_idname = 'toon53.prepare'
    bl_label = '建立项目灯光与影形控制'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        state = getattr(scene, PROP)
        try:
            lighting.prepare(scene, state.key, context.active_object, fill=state.fill)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class TOON53_OT_bind(bpy.types.Operator):
    bl_idname = 'toon53.bind'
    bl_label = '接入活动材质槽'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == 'MESH' and obj.active_material
                    and context.scene.get(lighting.PROJECT))

    def execute(self, context):
        scene = context.scene
        obj = context.active_object
        state = getattr(scene, PROP)
        try:
            lighting.bind(scene, obj, obj.active_material_index, state.role,
                          mask=state.mask, uv_name=state.uv)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class TOON53_OT_repair_normal(bpy.types.Operator):
    bl_idname = 'toon53.repair_face_normal'
    bl_label = '恢复面部艺术法向'
    bl_description = '在已登记相机的活动脸部槽中，让逐灯着色继续使用原材质法向'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        obj = context.active_object
        control = scene.get(lighting.CONTROLLER)
        if not (context.mode == 'OBJECT' and obj and obj == scene.get(lighting.FACE_OBJECT)
                and obj.type == 'MESH' and control and control.parent == scene.camera):
            return False
        material = obj.active_material
        if material and material.node_tree:
            stage = material.node_tree.nodes.get(material.get(lighting.STAGE, ''))
            return bool(stage and stage.get(lighting.ROLE) == lighting.ROLE_FACE)
        return False

    def execute(self, context):
        obj = context.active_object
        try:
            result = lighting.repair_face_normal(context.scene, obj, obj.active_material_index)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        message = '已恢复原面部法向' if result['status'] == 'repaired' else '面部法向已核验'
        self.report({'INFO'}, message)
        return {'FINISHED'}


class TOON53_OT_select_control(bpy.types.Operator):
    bl_idname = 'toon53.select_shadow_control'
    bl_label = '在相机视图选中影形控制点'
    bl_options = {'REGISTER', 'UNDO'}

    control: bpy.props.EnumProperty(items=[
        (lighting.CONTROLLER, '整体', '平移或拉伸整个刘海投影'),
        (lighting.LOCAL_ANCHOR, '局部落点', '把作用范围移到压眼的影形附近'),
        (lighting.LOCAL_HANDLE, '局部拖动', '拖动局部轮廓。远离落点的区域保持原位'),
        (lighting.EYELID_CONTROL, '上睑窄区', '移动主光减影区域的中心'),
    ])

    @classmethod
    def poll(cls, context):
        control = context.scene.get(lighting.CONTROLLER)
        return bool(control and context.area and context.area.type == 'VIEW_3D'
                    and context.mode == 'OBJECT' and context.scene.camera == control.parent
                    and context.view_layer.objects.get(control.name) == control)

    def execute(self, context):
        control = context.scene.get(self.control)
        if not control or context.view_layer.objects.get(control.name) != control:
            return {'CANCELLED'}
        for obj in context.selected_objects:
            obj.select_set(False)
        control.select_set(True)
        context.view_layer.objects.active = control
        context.space_data.region_3d.view_perspective = 'CAMERA'
        context.space_data.show_gizmo_object_translate = True
        context.space_data.overlay.show_extras = True
        return {'FINISHED'}


class TOON53_OT_eyelid(bpy.types.Operator):
    bl_idname = 'toon53.add_eyelid_control'
    bl_label = '为活动脸部槽添加上睑控光'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(context.scene.get(lighting.LOCAL_ANCHOR) and not context.scene.get(lighting.EYELID_CONTROL)
                    and obj and obj.type == 'MESH' and obj.active_material)

    def execute(self, context):
        try:
            lighting.add_eyelid_control(context.scene, context.active_object.active_material)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class TOON53_OT_key_control(bpy.types.Operator):
    bl_idname = 'toon53.key_shadow_control'
    bl_label = '给相机影形控制打关键帧'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return isinstance(context.scene.get(lighting.CONTROLLER), bpy.types.Object)

    def execute(self, context):
        scene = context.scene
        frame = scene.frame_current + scene.frame_subframe
        targets = {
            lighting.CONTROLLER: ('location', 'scale', '["toon53_density"]'),
            lighting.LOCAL_ANCHOR: ('location',),
            lighting.LOCAL_HANDLE: ('location', '["toon53_radius"]', '["toon53_face_bias"]'),
            lighting.EYELID_CONTROL: ('location', '["toon53_eyelid_width"]', '["toon53_eyelid_height"]',
                                      '["toon53_eyelid_relief"]'),
        }
        for name, paths in targets.items():
            control = scene.get(name)
            if control is None:
                continue
            for path in paths:
                if not control.keyframe_insert(data_path=path, frame=frame, group='三渲二 5.3 影形'):
                    self.report({'ERROR'}, '未能插入 ' + path)
                    return {'CANCELLED'}
        return {'FINISHED'}


class TOON53_OT_toggle_override(bpy.types.Operator):
    bl_idname = 'toon53.toggle_palette_override'
    bl_label = '切换跟随项目或使用材质例外'
    bl_options = {'REGISTER', 'UNDO'}

    field: bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self, context):
        obj = context.active_object
        material = obj.active_material if obj and obj.type == 'MESH' else None
        field = self.field
        if field not in LABELS or material is None or not material.get(lighting.PALETTE_REF):
            return {'CANCELLED'}
        scene = context.scene
        path = lighting.palette_path(material[lighting.PALETTE_REF], field)
        flag = path + '_override'
        enabled = bool(scene[flag])
        if not enabled:
            inherited = scene[field]
            scene[path] = inherited if isinstance(inherited, float) else tuple(inherited)
        scene[flag] = not enabled
        scene.update_tag()
        return {'FINISHED'}


class TOON53_PT_lighting(bpy.types.Panel):
    bl_idname = 'TOON53_PT_lighting'
    bl_label = '5.3 主光、发影与统一校色'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '三渲二'
    bl_parent_id = 'TOONWORKBENCH_PT_workbench'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return bpy.app.version[:2] == (5, 3) and context.scene.render.engine == 'BLENDER_EEVEE'

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        state = getattr(scene, PROP)
        layout.operator(TOON53_OT_save.bl_idname, icon='FILE_BLEND')
        lights = layout.column(align=True)
        lights.prop(state, 'key')
        lights.prop(state, 'fill')
        lights.operator(TOON53_OT_prepare.bl_idname)
        control = scene.get(lighting.CONTROLLER)
        if not control:
            lights.label(text='先选择使用脸部材质槽的目标')
            return
        if scene.get(lighting.KEY_LIGHT) != state.key and scene.get(lighting.KEY_LIGHT):
            lights.label(text='当前主光 · ' + scene[lighting.KEY_LIGHT].name)
        if scene.get(lighting.FILL_LIGHT) != state.fill and scene.get(lighting.FILL_LIGHT):
            lights.label(text='当前副光 · ' + scene[lighting.FILL_LIGHT].name)
        panel = layout.box()
        panel.label(text='相机影形控制')
        if control.parent != scene.camera:
            panel.label(text='当前镜头与影形控制的相机不同', icon='ERROR')
        panel.operator(TOON53_OT_select_control.bl_idname, text='选中整体控制').control = lighting.CONTROLLER
        panel.prop(control, 'location', text='影形位置')
        panel.prop(control, 'scale', text='影形拉伸')
        panel.prop(control, '["toon53_density"]', text='刘海影浓淡')
        anchor = scene.get(lighting.LOCAL_ANCHOR)
        handle = scene.get(lighting.LOCAL_HANDLE)
        if anchor and handle:
            panel.operator(TOON53_OT_select_control.bl_idname, text='选中局部落点').control = lighting.LOCAL_ANCHOR
            panel.operator(TOON53_OT_select_control.bl_idname, text='选中局部拖动点').control = lighting.LOCAL_HANDLE
            panel.prop(handle, 'location', text='局部投影偏移')
            panel.prop(handle, '["toon53_radius"]', text='局部半径')
            panel.prop(handle, '["toon53_face_bias"]', text='局部脸影偏移')
        eyelid = scene.get(lighting.EYELID_CONTROL)
        if eyelid:
            panel.operator(TOON53_OT_select_control.bl_idname, text='选中上睑窄区').control = lighting.EYELID_CONTROL
            panel.prop(eyelid, '["toon53_eyelid_width"]', text='水平半宽')
            panel.prop(eyelid, '["toon53_eyelid_height"]', text='垂直半高')
            panel.prop(eyelid, '["toon53_eyelid_relief"]', text='仅在此处补主光')
        else:
            panel.operator(TOON53_OT_eyelid.bl_idname)
        panel.operator(TOON53_OT_key_control.bl_idname, icon='KEY_HLT')
        palette = layout.box()
        palette.label(text='项目色板与明暗')
        for name, label in LABELS.items():
            palette.prop(scene, f'["{name}"]', text=label)
        target = layout.box()
        target.label(text='活动材质槽')
        target.prop(state, 'role')
        obj = context.active_object
        if state.role == lighting.ROLE_HAIR:
            target.prop(state, 'mask')
            if obj and obj.type == 'MESH':
                target.prop_search(state, 'uv', obj.data, 'uv_layers')
            else:
                target.prop(state, 'uv')
        target.operator(TOON53_OT_bind.bl_idname)
        mat = obj.active_material if obj and obj.type == 'MESH' else None
        if mat and mat.get(lighting.PALETTE_REF):
            stage = mat.node_tree.nodes.get(mat.get(lighting.STAGE))
            if stage is None or stage.bl_idname != 'ShaderNodeGroup' or stage.node_tree is None:
                target.label(text='材质的 5.3 控制节点需要检查', icon='ERROR')
                return
            if stage.get(lighting.ROLE) == lighting.ROLE_FACE:
                target.operator(TOON53_OT_repair_normal.bl_idname)
                if lighting.NORMAL_MIX in stage.node_tree.nodes:
                    path = lighting.face_normal_mix_path(mat)
                    target.prop(scene, f'["{path}"]', text='面部法向混合')
            for name, label in LABELS.items():
                if name not in stage.node_tree.nodes:
                    continue
                row = target.row()
                path = lighting.palette_path(mat[lighting.PALETTE_REF], name)
                flag = path + '_override'
                button = row.operator(TOON53_OT_toggle_override.bl_idname, text='',
                                      icon='LOCKED' if scene[flag] else 'UNLOCKED')
                button.field = name
                value = row.row()
                value.enabled = bool(scene[flag])
                value.prop(scene, f'["{path}"]', text=label)


_CLASSES = (TOON53_PG_authoring, TOON53_OT_save, TOON53_OT_prepare,
            TOON53_OT_bind, TOON53_OT_repair_normal, TOON53_OT_select_control, TOON53_OT_eyelid, TOON53_OT_key_control,
            TOON53_OT_toggle_override, TOON53_PT_lighting)


def register():
    global _registered, _descriptor
    if _registered:
        return
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
        _classes_registered.append(cls)
    setattr(bpy.types.Scene, PROP, bpy.props.PointerProperty(type=TOON53_PG_authoring))
    _descriptor = getattr(bpy.types.Scene, PROP)
    _registered = True


def unregister():
    global _registered, _descriptor
    if _descriptor is not None:
        if getattr(bpy.types.Scene, PROP, None) != _descriptor:
            raise RuntimeError('5.3 工作台 Scene 属性已改变')
        delattr(bpy.types.Scene, PROP)
        _descriptor = None
    while _classes_registered:
        cls = _classes_registered[-1]
        bpy.utils.unregister_class(cls)
        _classes_registered.pop()
    _registered = False
