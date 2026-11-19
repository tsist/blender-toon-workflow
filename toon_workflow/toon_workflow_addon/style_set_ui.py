# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded explicit-role template authoring; no automatic shot handlers."""
import json
import uuid
import bpy
from bpy.props import CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from . import style_set as T, component_slots as S, component_catalog as C

PROP = 'toon_style_set_ui'
_registered = False
_classes_registered = []
_descriptor = None

def _mesh_poll(self, obj):
    return obj.type == 'MESH'

def _camera_poll(self, obj):
    return obj.type == 'CAMERA'

class TOONSTYLESET_Binding(bpy.types.PropertyGroup):
    role: StringProperty(name='角色')
    target: PointerProperty(name='网格对象', type=bpy.types.Object, poll=_mesh_poll)
    slot: IntProperty(name='材质槽', default=0, min=0)


class TOONSTYLESET_CameraBinding(bpy.types.PropertyGroup):
    camera_role: StringProperty(name='机位角色')
    camera: PointerProperty(name='相机对象', type=bpy.types.Object, poll=_camera_poll)


class TOONSTYLESET_Settings(bpy.types.PropertyGroup):
    template_name: StringProperty(name='模板名', default='新风格模板')
    json_path: StringProperty(name='模板 JSON 绝对路径', subtype='FILE_PATH')
    document: StringProperty(options={'HIDDEN'})
    role_name: StringProperty(name='明确角色名')
    role_action: EnumProperty(name='角色用途', items=[
        ('PRESERVE', '保留原材质', '保护头脸、眼层等现有材质'),
        ('COMPONENT', '风格组件', '记录活动槽已验证的组件参数')], default='PRESERVE')
    bindings: CollectionProperty(type=TOONSTYLESET_Binding)
    camera_bindings: CollectionProperty(type=TOONSTYLESET_CameraBinding)
    camera_role: StringProperty(name='明确机位角色名')
    camera: PointerProperty(name='作者相机', type=bpy.types.Object, poll=_camera_poll)
    message: StringProperty(name='模板结果')


def _settings(context):
    return getattr(context.scene, PROP)

def _doc(context):
    settings = _settings(context)
    if not settings.document:
        raise ValueError('请先新建或导入模板')
    document = json.loads(settings.document)
    if document['name'] != settings.template_name:
        document['name'] = settings.template_name
        document['revision'] += 1
    return T.normalize(document, C.normalize)

def _save(context, document):
    normalized = T.normalize(document, C.normalize)
    settings = _settings(context)
    settings.document = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    settings.template_name = normalized['name']

def snapshot(scene):
    settings=getattr(scene,PROP,None)
    if settings is None or not settings.document:
        return None
    from types import SimpleNamespace
    return dict(schema='toon-style-set-author.v1',document=_doc(SimpleNamespace(scene=scene)),
        bindings=[dict(role=row.role,target=row.target.name if row.target else None,slot=row.slot) for row in settings.bindings],
        cameras=[dict(camera_role=row.camera_role,camera=row.camera.name if row.camera else None) for row in settings.camera_bindings])

def _text(value, label):
    if not value.strip():
        raise ValueError('请明确填写' + label)
    return value

def _active(context):
    target = context.active_object
    if target is None:
        raise ValueError('请选择本场景网格及已用活动槽')
    index = target.active_material_index
    S._slot(context.scene, target, index)
    return target, index

def _capture(context):
    target, index = _active(context)
    state = S.inspect(context.scene, target, index)
    if state['status'] != 'valid':
        raise ValueError('活动槽组件无效：' + '; '.join(state.get('issues', [])))
    return target, index, C.normalize(state['settings'])

def _camera(scene, camera):
    if (camera is None or camera.type != 'CAMERA'
            or scene.objects.get(camera.name) != camera):
        raise ValueError('请明确选择当前场景的真实Camera对象')
    return camera

def _bind(settings, role, target, index):
    for row in settings.bindings:
        if row.target == target and row.slot == index:
            row.role = role
            return
    row = settings.bindings.add()
    row.role, row.target, row.slot = role, target, index

def new_current(context):
    settings = _settings(context)
    document = dict(schema=T.SCHEMA, id=str(uuid.uuid4()), name=settings.template_name,
                    revision=1, roles={}, camera_overrides=[])
    _save(context, document)
    settings.bindings.clear()
    settings.camera_bindings.clear()
    return '已新建空模板，请明确记录角色'

def record_role_current(context):
    settings = _settings(context)
    document = _doc(context)
    role = _text(settings.role_name, '角色名')
    target, index = _active(context)
    entry = {'action': settings.role_action}
    if settings.role_action == 'COMPONENT':
        target, index, entry['settings'] = _capture(context)
    document['roles'][role] = entry
    document['revision'] += 1
    _save(context, document)
    _bind(settings, role, target, index)
    return '已记录角色参数并绑定活动槽'

def bind_current(context):
    settings = _settings(context)
    document = _doc(context)
    role = _text(settings.role_name, '角色名')
    if role not in document['roles']:
        raise ValueError('角色尚未记录；请先记录活动槽角色')
    target, index = _active(context)
    _bind(settings, role, target, index)
    return '已绑定活动槽；角色参数保持不变'

def record_camera_current(context):
    settings = _settings(context)
    document = _doc(context)
    role = _text(settings.role_name, '已有组件角色名')
    camera_role = _text(settings.camera_role, '机位角色名')
    camera = _camera(context.scene, settings.camera)
    if document['roles'].get(role, {}).get('action') != 'COMPONENT':
        raise ValueError('机位覆盖需要已存在的风格组件角色')
    _, _, captured = _capture(context)
    entry = dict(camera_role=camera_role, role=role, settings=captured)
    overrides = document['camera_overrides']
    old = next((i for i, row in enumerate(overrides)
                if (row['camera_role'], row['role']) == (camera_role, role)), None)
    if old is None:
        overrides.append(entry)
    else:
        overrides[old] = entry
    document['revision'] += 1
    # Validate the entire prospective document before changing authoring state.
    document = T.normalize(document, C.normalize)
    for row in settings.camera_bindings:
        if row.camera == camera and row.camera_role != camera_role:
            raise ValueError('同一真实相机不能绑定多个机位角色')
    _save(context, document)
    row = next((row for row in settings.camera_bindings
                if row.camera_role == camera_role), None)
    if row is None:
        row = settings.camera_bindings.add()
    row.camera_role, row.camera = camera_role, camera
    return '已记录机位覆盖；切换相机后请明确恢复再应用'


def import_current(context):
    settings = _settings(context)
    document = T.load_file(settings.json_path, C.normalize)
    _save(context, document)
    settings.bindings.clear()
    settings.camera_bindings.clear()
    for role in document['roles']:
        settings.bindings.add().role = role
    for role in sorted({row['camera_role'] for row in document['camera_overrides']}):
        settings.camera_bindings.add().camera_role = role
    return '已导入；请为每个占位明确选择真实对象、槽与相机'


def export_current(context):
    document = T.export_file(_doc(context), _settings(context).json_path, C.normalize)
    _save(context, document)
    return '已导出新JSON文件'


def _bindings(context, document):
    rows, seen, bound = [], set(), set()
    for row in _settings(context).bindings:
        if row.role not in document['roles']:
            raise ValueError('绑定含未声明角色：' + row.role)
        S._slot(context.scene, row.target, row.slot)
        identity = (row.target.as_pointer(), row.slot)
        if identity in seen:
            raise ValueError('同一真实对象和槽不能重复绑定')
        seen.add(identity)
        bound.add(row.role)
        rows.append(dict(role=row.role, target=row.target, slot=row.slot))
    if not document['roles'] or set(document['roles']) - bound:
        raise ValueError('必须为每个角色明确绑定对象与已用槽')
    return rows


def _cameras(context, document):
    result, seen = {}, set()
    required = {row['camera_role'] for row in document['camera_overrides']}
    for row in _settings(context).camera_bindings:
        role = _text(row.camera_role, '机位角色名')
        camera = _camera(context.scene, row.camera)
        if role in result or camera.as_pointer() in seen:
            raise ValueError('机位角色或真实相机重复绑定')
        result[role] = camera
        seen.add(camera.as_pointer())
    if required - set(result):
        raise ValueError('每个机位覆盖都需要明确绑定当前场景相机')
    return result


def apply_current(context):
    document = _doc(context)
    bindings = _bindings(context, document)
    cameras = _cameras(context, document)
    for row in bindings:
        if document['roles'][row['role']]['action'] == 'COMPONENT':
            material = S._slot(context.scene, row['target'], row['slot']).material
            if material is not None and S.KEY in material:
                raise ValueError('绑定槽已有组件，请先点击“恢复绑定组件”再应用')
    S.apply_set(context, document, bindings, camera_bindings=cameras)
    _save(context, document)
    return '已按当前真实相机应用模板；保留角色原槽不变'


def restore_current(context):
    document = _doc(context)
    records = []
    for row in _bindings(context, document):
        if document['roles'][row['role']]['action'] == 'COMPONENT':
            slot, _, _ = S._owned(context.scene, row['target'], row['slot'])
            records.append((row, slot, slot.material, slot.link))
    attempted = []
    try:
        for record in records:
            row = record[0]
            attempted.append(record)
            S.restore(context.scene, row['target'], row['slot'])
    except BaseException as error:
        failures = []
        for _, slot, material, link in reversed(attempted):
            try:
                slot.link = link
                slot.material = material
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise RuntimeError('恢复失败：' + str(error) + '；回滚未完成：' + '; '.join(failures)) from error
        raise
    return '已恢复所有绑定组件；现在可切换机位再应用'

class TOONSTYLESET_OT_action(bpy.types.Operator):
    bl_idname = 'toon_style_set.action'
    bl_label = '风格模板操作'
    bl_options = {'REGISTER', 'UNDO'}
    operation: EnumProperty(items=[(key, label, '') for key, label in (
        ('NEW', '新建模板'), ('RECORD', '记录角色'), ('BIND', '绑定活动槽'),
        ('CAMERA', '记录机位覆盖'), ('IMPORT', '导入模板'), ('EXPORT', '导出模板'),
        ('APPLY', '应用模板'), ('RESTORE', '恢复绑定组件'),
        ('REMOVE_BINDING', '删除槽绑定'), ('REMOVE_CAMERA', '删除机位绑定'))])
    index: IntProperty(default=-1)

    def execute(self, context):
        try:
            settings = _settings(context)
            if self.operation in ('REMOVE_BINDING', 'REMOVE_CAMERA'):
                rows = settings.bindings if self.operation == 'REMOVE_BINDING' else settings.camera_bindings
                if not 0 <= self.index < len(rows):
                    raise ValueError('绑定行已改变，请重新选择')
                rows.remove(self.index)
                message = '已删除绑定；模板角色参数保持不变'
            else:
                actions = dict(NEW=new_current, RECORD=record_role_current, BIND=bind_current,
                    CAMERA=record_camera_current, IMPORT=import_current, EXPORT=export_current,
                    APPLY=apply_current, RESTORE=restore_current)
                message = actions[self.operation](context)
            settings.message = message
            return {'FINISHED'}
        except Exception as error:
            _settings(context).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def _button(layout, operation, text, index=-1):
    operator = layout.operator('toon_style_set.action', text=text)
    operator.operation, operator.index = operation, index


def draw_contents(layout, context):
    settings = _settings(context)
    layout.label(text='风格模板：角色与机位明确绑定')
    layout.prop(settings, 'template_name')
    _button(layout, 'NEW', '新建空模板')
    layout.prop(settings, 'json_path')
    row = layout.row(align=True)
    _button(row, 'IMPORT', '导入模板'); _button(row, 'EXPORT', '导出新文件')
    layout.prop(settings, 'role_name'); layout.prop(settings, 'role_action')
    _button(layout, 'RECORD', '记录活动槽角色并绑定')
    _button(layout, 'BIND', '绑定活动槽（保留角色参数）')
    for index, binding in enumerate(settings.bindings):
        box = layout.box()
        for key in ('role', 'target', 'slot'):
            box.prop(binding, key)
        _button(box, 'REMOVE_BINDING', '删除此槽绑定', index)
    layout.prop(settings, 'camera_role'); layout.prop(settings, 'camera')
    _button(layout, 'CAMERA', '记录活动槽参数为机位覆盖')
    for index, binding in enumerate(settings.camera_bindings):
        box = layout.box()
        box.prop(binding, 'camera_role'); box.prop(binding, 'camera')
        _button(box, 'REMOVE_CAMERA', '删除此机位绑定', index)
    _button(layout, 'RESTORE', '恢复绑定组件')
    layout.prop(context.scene, 'camera', text='当前渲染机位')
    _button(layout, 'APPLY', '按当前机位应用模板')
    layout.label(text='切机位：先恢复绑定组件，再明确应用；无自动跟随')
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONSTYLESET_Binding, TOONSTYLESET_CameraBinding,
           TOONSTYLESET_Settings, TOONSTYLESET_OT_action)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Style-set UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONSTYLESET_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Style-set UI property was replaced; preserve its foreign owner')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
