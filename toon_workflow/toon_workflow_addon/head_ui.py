# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded head-part normal author UI, with guarded additive preparation.

No standalone Panel, material edits, name-based role guesses, handlers or file
saves. The head core owns registry semantics; the normal core owns fields.
"""
import math

import bpy
from bpy.props import EnumProperty, PointerProperty, StringProperty

PROP = 'toon_head_normal_ui'
ROLES = (('FACE', '面部', ''), ('LASH', '睫毛', ''), ('BROW', '眉毛', ''),
         ('HAIR', '头发', ''), ('EYE', '眼部', ''), ('MOUTH', '口部', ''),
         ('ACCESSORY', '附属物', ''))
_LABELS = {key: label for key, label, _ in ROLES}
_registered = False
_descriptor = None
_classes_registered = []


def _cores():
    from . import artistic_normals, head_normals
    return artistic_normals, head_normals


def _scene(context):
    scene = context.scene
    if scene.library or scene.override_library or not scene.is_editable:
        raise ValueError('头部法向需要本地可编辑Scene')
    if context.mode != 'OBJECT':
        raise ValueError('请先切换对象模式')
    return scene


def _field(target, normal):
    matches = [m for m in target.modifiers if m.type == 'NODES' and m.node_group
               and normal.is_managed(m)]
    if len(matches) > 1:
        raise ValueError('目标含多个艺术法向字段，请先明确处理：' + target.name)
    return matches[0] if matches else None


def _reference(modifier):
    sockets = [item for item in modifier.node_group.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
        and item.socket_type == 'NodeSocketObject']
    if len(sockets) != 1:
        raise ValueError('受管法向缺少唯一Object参考输入')
    return getattr(modifier.properties.inputs, sockets[0].identifier).value


def _weight(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return None
    if attribute.domain != 'POINT' or attribute.data_type != 'FLOAT':
        raise ValueError('已有头部权重必须是FLOAT POINT，保持原属性不改')
    if any(not math.isfinite(item.value) or not 0 <= item.value <= 1 for item in attribute.data):
        raise ValueError('已有头部权重必须是有限[0,1]，保持原值不改')
    return attribute


def _rollback(created, normal):
    failures = []
    for row in reversed(created):
        target = row['target']
        modifier = row.get('modifier')
        if modifier is not None:
            try:
                normal.remove(target, modifier)
            except Exception as error:
                failures.append(target.name + ':modifier:' + str(error))
                # Do not remove its input Mesh/weight if modifier teardown
                # failed; report the recoverable partial preparation honestly.
                continue
        if row.get('copy') is not None:
            try:
                copy = row['copy']
                if target.data == copy:
                    target.data = row['original_mesh']
                if copy.users != 0:
                    raise ValueError('本次新Mesh出现外部消费者，保留副本')
                bpy.data.meshes.remove(copy)
            except Exception as error:
                failures.append(target.name + ':mesh:' + str(error))
        elif row.get('attribute') is not None:
            try:
                row['original_mesh'].attributes.remove(row['attribute'])
            except Exception as error:
                failures.append(target.name + ':weight:' + str(error))
        target.update_tag()
    if failures:
        raise RuntimeError('头部法向准备回滚未完整完成：' + '; '.join(failures))


def add_selected(context):
    scene = _scene(context)
    settings = getattr(scene, PROP)
    normal, head = _cores()
    existing = head.controls(scene)
    reference = settings.reference or (existing['reference'] if existing else None)
    if reference is None or reference not in list(scene.objects):
        raise ValueError('请选择当前Scene内的头部法向参考Object')
    if existing and reference != existing['reference']:
        raise ValueError('加入部件不更换已有头部公共参考，请保留原引用')
    selected = list(context.selected_objects)
    if not selected or any(obj.type != 'MESH' for obj in selected):
        raise ValueError('请选择一组明确的Mesh；选择中不可混入非网格对象')
    if len(selected) != len({obj.as_pointer() for obj in selected}):
        raise ValueError('选择包含重复目标')
    name = settings.weight_attribute
    if type(name) is not str or not 1 <= len(name) <= 63:
        raise ValueError('头部权重属性名须为1..63字符')
    if settings.role not in _LABELS:
        raise ValueError('请选择明确的新增部件角色')
    bindings = [dict(target=row['target'], role=row['role'], strength=row['strength'])
                for row in existing['bindings']] if existing else []
    members = {row['target'] for row in bindings}
    prepared = []
    # Resolve every selection/reference/old field/weight before the first edit.
    for target in selected:
        if target.library or target.override_library or not target.is_editable or target not in list(scene.objects):
            raise ValueError('所选目标须为本Scene本地可编辑Mesh：' + target.name)
        if target == reference:
            raise ValueError('目标不能同时作为自身法向参考')
        modifier = _field(target, normal)
        if modifier is not None:
            report = normal.inspect(target, modifier, evaluate=False)
            if report['status'] != 'valid':
                raise ValueError('已有艺术法向输入无效：' + target.name + '；' + '; '.join(report['issues']))
            if _reference(modifier) != reference:
                raise ValueError('已有艺术法向Reference不同，拒绝静默改绑：' + target.name)
            strength = report['strength']
        else:
            _weight(target.data, name)
            strength = 0.0
        if target not in members:
            prepared.append(dict(target=target, modifier=modifier, strength=strength))
    if not prepared:
        return dict(status='unchanged', added=0, members=len(bindings))
    if len(bindings) + len(prepared) > head.MAX_BINDINGS:
        raise ValueError('头部法向组最多%d个明确部件' % head.MAX_BINDINGS)
    created = []
    try:
        for item in prepared:
            target, modifier = item['target'], item['modifier']
            if modifier is None:
                row = dict(target=target, original_mesh=target.data)
                created.append(row)
                if target.data.attributes.get(name) is None:
                    if target.data.users > 1 or target.data.library is not None:
                        row['copy'] = target.data.copy()
                        target.data = row['copy']
                    attribute = target.data.attributes.new(name=name, type='FLOAT', domain='POINT')
                    row['attribute'] = attribute
                    if attribute.name != name:
                        raise ValueError('Blender改变了头部权重名，拒绝截断或自动重名')
                    attribute.data.foreach_set('value', [0.] * len(attribute.data))
                row['modifier'] = normal.build(target, reference, name, strength=0.0)
            bindings.append(dict(target=target, role=settings.role, strength=item['strength']))
        # define is the core's metadata transaction. It never configures the
        # pre-existing native parameters; old roles/request values stay exact.
        receipt = head.define(scene, reference, bindings, replace=existing is not None)
    except Exception as error:
        try:
            _rollback(created, normal)
        except Exception as rollback_error:
            raise RuntimeError(str(error) + '；' + str(rollback_error)) from error
        raise
    return dict(status='defined', added=len(prepared), members=len(bindings), receipt=receipt)


def apply_head(context):
    scene = _scene(context)
    receipt = _cores()[1].apply(scene)
    if receipt is None:
        raise ValueError('尚未定义头部法向组')
    context.view_layer.update()
    return receipt


def inspect_head(context):
    scene = _scene(context)
    report = _cores()[1].inspect(scene)
    if report is None:
        raise ValueError('尚未定义头部法向组')
    return report


def _selected_members(context):
    scene = _scene(context)
    head = _cores()[1]
    registry = head.controls(scene)
    if registry is None:
        raise ValueError('尚未定义头部法向组')
    selected = set(context.selected_objects)
    if not selected:
        raise ValueError('请明确选择要更新或移出的已登记部件')
    members = {row['target'] for row in registry['bindings']}
    if not selected <= members:
        raise ValueError('选择包含未登记对象，保持整个头部配置不变')
    return scene, head, registry, selected


def update_selected_role(context):
    scene, head, registry, selected = _selected_members(context)
    role = getattr(scene, PROP).role
    if role not in _LABELS:
        raise ValueError('请选择明确的部件角色')
    changed = sum(row['target'] in selected and row['role'] != role for row in registry['bindings'])
    if not changed:
        return dict(status='unchanged', updated=0)
    bindings = [dict(target=row['target'],
                     role=role if row['target'] in selected else row['role'],
                     strength=row['strength']) for row in registry['bindings']]
    receipt = head.define(scene, registry['reference'], bindings, replace=True)
    return dict(status='updated', updated=changed, receipt=receipt)


def remove_selected(context):
    scene, head, registry, selected = _selected_members(context)
    bindings = [dict(target=row['target'], role=row['role'], strength=row['strength'])
                for row in registry['bindings'] if row['target'] not in selected]
    if bindings:
        receipt = head.define(scene, registry['reference'], bindings, replace=True)
    else:
        receipt = head.clear(scene)
    return dict(status='removed', removed=len(selected), members=len(bindings), receipt=receipt)


class TOONHEAD_Settings(bpy.types.PropertyGroup):
    reference: PointerProperty(name='头部公共参考', type=bpy.types.Object)
    role: EnumProperty(name='新增/更新部件角色', items=ROLES, default='FACE')
    weight_attribute: StringProperty(name='新字段权重名', default='toon_head_weight')
    message: StringProperty(name='头部法向状态')


class _Action:
    @classmethod
    def poll(cls, context):
        try:
            _scene(context)
            return True
        except (ValueError, AttributeError):
            return False

    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            report = self.action(context)
            if isinstance(report, dict) and report.get('status') == 'invalid':
                raise ValueError('; '.join(report.get('issues', ['头部法向无效'])))
            issues = report.get('retirement_issues', report.get('receipt', {}).get('retirement_issues', []))
            settings.message = (self.bl_label + '已提交；旧metadata清理待处理：' + '; '.join(issues)
                                if issues else self.bl_label + '完成')
            self.report({'WARNING'} if issues else {'INFO'}, settings.message)
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error)
            self.report({'ERROR'}, settings.message)
            return {'CANCELLED'}


class TOONHEAD_OT_add(_Action, bpy.types.Operator):
    bl_idname = 'toon_head.add_selected'; bl_label = '加入所选网格（缺字段默认0）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(add_selected)


class TOONHEAD_OT_apply(_Action, bpy.types.Operator):
    bl_idname = 'toon_head.apply'; bl_label = '显式应用整头请求强度'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(apply_head)


class TOONHEAD_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_head.inspect'; bl_label = '检查整头法向组'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(inspect_head)


class TOONHEAD_OT_role(_Action, bpy.types.Operator):
    bl_idname = 'toon_head.update_selected_role'; bl_label = '更新所选已登记部件角色'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_selected_role)


class TOONHEAD_OT_remove(_Action, bpy.types.Operator):
    bl_idname = 'toon_head.remove_selected'; bl_label = '移出所选已登记部件'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(remove_selected)


def draw_contents(layout, context):
    settings = getattr(context.scene, PROP)
    layout.label(text='整头法向：按明确选择和角色加入，默认0')
    layout.prop(settings, 'reference')
    layout.prop(settings, 'role')
    layout.prop(settings, 'weight_attribute')
    layout.operator('toon_head.add_selected')
    layout.label(text='已有字段保原参考/权重/强度；已有成员保原角色')
    try:
        normal, head = _cores()
        registry = head.controls(context.scene)
        if registry:
            layout.label(text='已存公共参考：' + registry['reference'].name)
            for row in registry['bindings']:
                box = layout.box()
                box.label(text=_LABELS.get(row['role'], row['role']) + ' · ' + row['target'].name)
                report = normal.inspect(row['target'], _field(row['target'], normal), evaluate=False)
                box.label(text='权重：' + report['weight_attribute'])
                box.prop(row['control'], '["' + row['strength_property'] + '"]', text='请求强度')
            layout.operator('toon_head.apply')
            layout.operator('toon_head.inspect')
            layout.operator('toon_head.update_selected_role')
            layout.operator('toon_head.remove_selected')
            layout.label(text='角色/移出仅作用于明确选中的已登记部件')
            layout.label(text='移出保留独立法向字段、权重和材质')
        else:
            layout.label(text='尚未定义头部组；权重不自动设为1')
    except (ValueError, TypeError, KeyError) as error:
        layout.label(text=str(error), icon='ERROR')
    layout.label(text='不批量改材质；单对象绑定见高级艺术法向')
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONHEAD_Settings, TOONHEAD_OT_add, TOONHEAD_OT_apply, TOONHEAD_OT_inspect,
           TOONHEAD_OT_role, TOONHEAD_OT_remove)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Scene头部法向UI属性已被其它所有者注册')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONHEAD_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Scene头部法向UI属性已被替换，保留其它所有者')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
