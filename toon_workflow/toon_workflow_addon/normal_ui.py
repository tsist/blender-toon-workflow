# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in artistic-normal UI. No handlers, file saves or material guessing.

Only an explicitly requested, target-exclusive Diffuse.Normal input is wired.
The binding's exact ownership and direct links are witnessed for safe removal.
The artistic field and shader-group implementations remain in the core module.
"""
import uuid

import bpy
from bpy.props import FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

PROP = 'toon_artistic_normal_ui'
_PROFILE = 'toon-artistic-normal-ui-binding.v1'
_MARK = 'toon_art_normal_ui_binding'
_TOKEN = 'toon_art_normal_ui_token'
_TARGET = 'toon_art_normal_ui_target'
_UID = 'toon_art_normal_ui_modifier_uid'
_TREE = 'toon_art_normal_ui_shader_tree'
_OUTPUT = 'toon_art_normal_ui_output'
_DESTINATION = 'toon_art_normal_ui_destination'
_registered = False
_descriptor = None
_classes_registered = []


def _core():
    from . import artistic_normals
    return artistic_normals


def _authoring():
    from . import normal_authoring
    return normal_authoring


def _target(context):
    target = context.object
    if context.mode != 'OBJECT' or target is None or target.type != 'MESH':
        raise ValueError('请选择对象模式下的活动网格')
    if target.library or target.override_library or not target.is_editable:
        raise ValueError('艺术法向UI只修改本地可编辑网格对象')
    return target


def _managed(target):
    core = _core()
    matches = [modifier for modifier in target.modifiers if core.is_managed(modifier)]
    if len(matches) != 1:
        raise ValueError('目标须有且仅有一个受管艺术法向字段')
    return matches[0]


def _edit_target(context):
    target = context.object
    if context.mode != 'EDIT_MESH' or target is None or target.type != 'MESH':
        raise ValueError('写入方向仅支持活动网格的编辑模式')
    if target.library or target.override_library or not target.is_editable:
        raise ValueError('方向图只编辑本地可编辑网格')
    if target.data.library is not None or target.data.users != 1:
        raise ValueError('方向图写入需要独占Mesh，请先在对象模式初始化独立副本')
    if len(context.objects_in_mode_unique_data) != 1:
        raise ValueError('请仅编辑一个网格；不写入其它头部部件')
    return target


def _valid(target, modifier):
    report = _core().inspect(target, modifier, evaluate=False)
    if report['status'] != 'valid':
        raise ValueError('艺术法向字段无效：' + '; '.join(report.get('issues', [])))
    return report


def _material(target):
    material = target.active_material
    if (material is None or material.library or material.override_library
            or not material.is_editable or not material.use_nodes or material.node_tree is None):
        raise ValueError('请明确选择本地、可编辑且已启用节点的活动材质')
    if material.node_tree.library or not material.node_tree.is_editable:
        raise ValueError('活动材质节点树不可编辑')
    others = [obj.name for obj in bpy.data.objects if obj != target
              and any(slot.material == material for slot in getattr(obj, 'material_slots', ()))]
    if others:
        raise ValueError('活动材质被其它对象使用，拒绝修改共享数据：' + ', '.join(others[:8]))
    # Also reject hidden consumers such as an orphan Mesh or a GN material
    # reference; object-slot scanning alone does not establish exclusive use.
    consumers = bpy.data.user_map(subset={material}).get(material, set())
    if any(user not in {target, target.data} for user in consumers):
        raise ValueError('活动材质还有本目标之外的数据块消费者，拒绝共享修改')
    return material


def _diffuse(tree):
    matches = [node for node in tree.nodes if node.bl_idname == 'ShaderNodeBsdfDiffuse']
    if len(matches) != 1:
        raise ValueError('活动材质必须恰好包含一个原生Diffuse BSDF')
    node = matches[0]
    sockets = [socket for socket in node.inputs if socket.identifier == 'Normal']
    if len(sockets) != 1:
        raise ValueError('Diffuse缺少唯一Normal输入')
    return node, sockets[0]


def create_zero_weight(context):
    target = _target(context)
    name = getattr(context.scene, PROP).weight_attribute
    if type(name) is not str or not 1 <= len(name) <= 63:
        raise ValueError('请输入1..63字符的权重属性名')
    original = target.data
    if original.attributes.get(name) is not None:
        raise ValueError('权重属性已存在，保持原值不覆盖：' + name)
    copied = original.users > 1 or original.library is not None
    mesh = original.copy() if copied else original
    attribute = None
    try:
        attribute = mesh.attributes.new(name=name, type='FLOAT', domain='POINT')
        if attribute.name != name:
            raise ValueError('Blender改变了权重属性名（截断或重名），已拒绝创建')
        attribute.data.foreach_set('value', [0.0] * len(attribute.data))
        if copied:
            target.data = mesh
    except Exception:
        if copied and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        elif not copied and attribute is not None:
            mesh.attributes.remove(attribute)
        raise
    target.update_tag()
    return dict(attribute=name, copied_mesh=copied, vertices=len(mesh.vertices))


def create_field(context):
    target = _target(context)
    settings = getattr(context.scene, PROP)
    return _core().build(target, settings.reference, settings.weight_attribute, strength=0.0)


def apply_strength(context):
    target = _target(context)
    # Omitted reference/weight retain their actual modifier bindings. Native
    # animation protection belongs to configure, not to Scene UI proxy values.
    return _core().configure(target, _managed(target),
                             strength=getattr(context.scene, PROP).strength)


def initialize_mapping(context):
    target = _target(context)
    modifier = _managed(target)
    core = _core()
    if modifier.node_group.get(core.PROFILE_KEY) != core.PROFILE:
        raise ValueError('初始化新图前请先恢复椭球模式；已有方向图保持不变')
    name = getattr(context.scene, PROP).mapping_attribute
    if type(name) is not str or not name or len(name.encode('utf8')) > 63 or '\x00' in name:
        raise ValueError('作者方向属性名须为1..63个UTF-8字节，不能含空字符')
    original = target.data
    if original.attributes.get(name) is not None:
        raise ValueError('作者方向属性已存在，保持原值不覆盖：' + name)
    copied = original.users > 1 or original.library is not None
    mesh = original.copy() if copied else original
    try:
        if copied:
            target.data = mesh
        result = _authoring().initialize(target, modifier, name)
        attribute = mesh.attributes.get(name)
        if attribute is None or attribute.name != name:
            raise ValueError('初始化未保留确切作者属性名，拒绝截断或自动重名')
    except Exception:
        if copied:
            target.data = original
            if mesh.users != 0:
                raise RuntimeError('本次新Mesh出现外部消费者，保留副本并拒绝继续')
            bpy.data.meshes.remove(mesh)
        else:
            # No such attribute existed before this synchronous author call.
            attribute = mesh.attributes.get(name)
            if attribute is not None:
                mesh.attributes.remove(attribute)
        raise
    target.update_tag()
    return dict(result, copied_mesh=copied)


def enable_mapping(context):
    target = _target(context)
    return _core().set_mapping(target, _managed(target),
                              getattr(context.scene, PROP).mapping_attribute)


def restore_ellipsoid(context):
    target = _target(context)
    return _core().clear_mapping(target, _managed(target))


def write_selected_direction(context):
    target = _edit_target(context)
    _managed(target)
    import bmesh
    mesh = bmesh.from_edit_mesh(target.data)
    mesh.verts.ensure_lookup_table()
    mesh.verts.index_update()
    indices = [vertex.index for vertex in mesh.verts if vertex.select]
    if not indices:
        raise ValueError('没有选中顶点；不会改写整个网格')
    settings = getattr(context.scene, PROP)
    name = settings.mapping_attribute
    direction = tuple(settings.direction)
    blend = settings.blend
    # Flush the actual EditMesh once, then reuse the same atomic core author
    # operation as object-mode callers. Restore Edit Mode on success or error.
    bpy.ops.object.mode_set(mode='OBJECT')
    try:
        return _authoring().write_direction(target, name, indices, direction, blend=blend)
    finally:
        context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode='EDIT')


def write_selected_weight(context):
    target = _edit_target(context)
    modifier = _managed(target)
    import bmesh
    mesh = bmesh.from_edit_mesh(target.data)
    mesh.verts.ensure_lookup_table()
    mesh.verts.index_update()
    indices = [vertex.index for vertex in mesh.verts if vertex.select]
    if not indices:
        raise ValueError('没有选中顶点；不会改写整个权重图')
    settings = getattr(context.scene, PROP)
    value, blend = settings.weight_value, settings.weight_blend
    # The core resolves the actual owned modifier input after EditMesh flush.
    # The setup-only UI attribute name must never retarget an existing field.
    bpy.ops.object.mode_set(mode='OBJECT')
    try:
        return _authoring().write_weight(target, modifier, indices, value, blend=blend)
    finally:
        context.view_layer.objects.active = target
        bpy.ops.object.mode_set(mode='EDIT')


def bind_diffuse(context):
    target = _target(context)
    modifier = _managed(target)
    _valid(target, modifier)
    material = _material(target)
    tree = material.node_tree
    diffuse, normal = _diffuse(tree)
    if normal.is_linked:
        raise ValueError('Diffuse.Normal已有连接，保持作者链接不变')
    if _DESTINATION in diffuse or any(_MARK in node for node in tree.nodes):
        raise ValueError('材质已有UI绑定或残留标记，请先检查现有作者状态')
    shader = _core().shader_group(modifier)
    outputs = [item for item in shader.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'
        and item.name == 'Normal' and item.socket_type == 'NodeSocketVector']
    all_outputs = [item for item in shader.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT']
    if shader.bl_idname != 'ShaderNodeTree' or len(outputs) != 1 or len(all_outputs) != 1:
        raise ValueError('核心shader group须有唯一原生Vector Normal输出')
    token = uuid.uuid4().hex
    node = tree.nodes.new('ShaderNodeGroup')
    try:
        node.node_tree = shader
        node.label = '艺术法向（UI绑定）'
        node.location = (diffuse.location.x - 230, diffuse.location.y - 180)
        socket = next(s for s in node.outputs if s.identifier == outputs[0].identifier)
        node[_MARK], node[_TOKEN], node[_TARGET] = _PROFILE, token, target
        node[_UID], node[_TREE], node[_OUTPUT] = modifier.persistent_uid, shader, socket.identifier
        tree.links.new(socket, normal)
        diffuse[_DESTINATION] = token
    except Exception:
        if diffuse.get(_DESTINATION) == token:
            del diffuse[_DESTINATION]
        tree.nodes.remove(node)
        raise
    return dict(material=material.name, node=node.name, diffuse=diffuse.name)


def disconnect_diffuse(context):
    target = _target(context)
    modifier = _managed(target)
    report = _valid(target, modifier)
    material = _material(target)
    tree = material.node_tree
    marked = [node for node in tree.nodes if _MARK in node]
    if len(marked) != 1:
        raise ValueError('材质没有唯一由此UI标记的节点，不删除作者节点')
    node = marked[0]
    if (node.get(_MARK) != _PROFILE or node.bl_idname != 'ShaderNodeGroup'
            or node.get(_TARGET) != target or node.get(_UID) != modifier.persistent_uid
            or node.get(_TREE) != node.node_tree or node.node_tree is None):
        raise ValueError('UI节点类型、目标或核心shader引用已被修改，拒绝断开')
    token = node.get(_TOKEN)
    diffuse, normal = _diffuse(tree)
    destinations = [n for n in tree.nodes if n.get(_DESTINATION) == token]
    if not isinstance(token, str) or len(token) != 32 or destinations != [diffuse]:
        raise ValueError('原Diffuse目标身份已改变，拒绝断开')
    links = [link for link in tree.links if link.from_node == node or link.to_node == node]
    if (len(links) != 1 or links[0].from_node != node or links[0].to_node != diffuse
            or links[0].to_socket != normal or links[0].from_socket.identifier != node.get(_OUTPUT)
            or len(normal.links) != 1):
        raise ValueError('UI节点链接已被作者改变，保留当前材质')
    shaders = [row for row in report.get('shader_groups', []) if row['name'] == node.node_tree.name]
    if len(shaders) != 1 or shaders[0]['author_edited']:
        raise ValueError('shader group已经过作者编辑，拒绝删除其UI实例')
    # Node removal removes only the one witnessed link. Diffuse's unlinked
    # Normal default and every other material node/socket remain untouched.
    name = node.name
    tree.nodes.remove(node)
    del diffuse[_DESTINATION]
    return dict(material=material.name, removed_node=name)


def inspect_field(context):
    target = _target(context)
    return _core().inspect(target, _managed(target), depsgraph=context.evaluated_depsgraph_get())


class TOONNORMAL_Settings(bpy.types.PropertyGroup):
    reference: PointerProperty(name='法向参考', type=bpy.types.Object)
    weight_attribute: StringProperty(name='权重属性', default='toon_art_normal_weight')
    strength: FloatProperty(name='待应用强度', default=0., min=0., max=1.)
    mapping_attribute: StringProperty(name='作者方向属性', default='toon_authored_normal')
    direction: FloatVectorProperty(name='参考空间方向', size=3, default=(0., 0., 1.))
    blend: FloatProperty(name='选中方向混合', default=1., min=0., max=1.)
    weight_value: FloatProperty(name='选中权重值', default=1., min=0., max=1.)
    weight_blend: FloatProperty(name='权重混合', default=1., min=0., max=1.)
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
                raise ValueError('; '.join(result.get('issues', ['艺术法向字段无效'])))
            if isinstance(result, dict) and 'attributes' in result and 'strength' in result:
                attrs = result['attributes']
                settings.message = '有效；强度 %.6g；normal=%s；mix=%s；valid=%s' % (
                    result['strength'], attrs['normal'], attrs['mix'], attrs['valid'])
            else:
                settings.message = self.bl_label + '完成'
            self.report({'INFO'}, settings.message)
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error)
            self.report({'ERROR'}, settings.message)
            return {'CANCELLED'}


class TOONNORMAL_OT_zero(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.zero_weight'; bl_label = '创建全零权重（不覆盖）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_zero_weight)


class TOONNORMAL_OT_build(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.create_field'; bl_label = '创建艺术法向字段（默认0）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_field)


class TOONNORMAL_OT_strength(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.apply_strength'; bl_label = '应用强度（保留现有参考）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(apply_strength)


class TOONNORMAL_OT_bind(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.bind_diffuse'; bl_label = '绑定活动材质Diffuse.Normal'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(bind_diffuse)


class TOONNORMAL_OT_disconnect(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.disconnect_diffuse'; bl_label = '断开本UI绑定（保护手改）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(disconnect_diffuse)


class TOONNORMAL_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.inspect_field'; bl_label = '检查艺术法向字段'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(inspect_field)


class TOONNORMAL_OT_initialize_mapping(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.initialize_mapping'; bl_label = '初始化静态作者方向图（不启用）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(initialize_mapping)


class TOONNORMAL_OT_enable_mapping(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.enable_mapping'; bl_label = '显式启用作者映射模式'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(enable_mapping)


class TOONNORMAL_OT_clear_mapping(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.restore_ellipsoid'; bl_label = '恢复椭球模式（保留方向图）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(restore_ellipsoid)


class TOONNORMAL_OT_write_direction(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.write_selected_direction'; bl_label = '仅写入所选顶点方向'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(write_selected_direction)

    @classmethod
    def poll(cls, context):
        try:
            _edit_target(context)
            return True
        except (ValueError, AttributeError):
            return False


class TOONNORMAL_OT_write_weight(_Action, bpy.types.Operator):
    bl_idname = 'toon_normal.write_selected_weight'; bl_label = '仅写入所选顶点权重'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(write_selected_weight)

    @classmethod
    def poll(cls, context):
        try:
            _managed(_edit_target(context))
            return True
        except (ValueError, AttributeError):
            return False


class TOONNORMAL_PT_panel(bpy.types.Panel):
    bl_idname = 'TOONNORMAL_PT_panel'; bl_label = '艺术法向（实验）'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = '三渲二'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_contents(self.layout, context)


def draw_contents(layout, context):
    try:
        if context.mode == 'EDIT_MESH':
            _edit_target(context)
        else:
            _target(context)
    except ValueError as error:
        layout.label(text=str(error), icon='INFO')
        return
    settings = getattr(context.scene, PROP)
    layout.prop(settings, 'reference')
    layout.prop(settings, 'weight_attribute')
    layout.operator('toon_normal.zero_weight')
    layout.operator('toon_normal.create_field')
    layout.prop(settings, 'strength')
    layout.operator('toon_normal.apply_strength')
    layout.label(text='现有参考/权重与原生动画保持；可在修改器中编辑')
    layout.operator('toon_normal.bind_diffuse')
    layout.operator('toon_normal.disconnect_diffuse')
    layout.operator('toon_normal.inspect_field')
    box = layout.box()
    box.label(text='局部作者法向：静态头部参考空间方向模板')
    box.label(text='非切线RGB Normal；初始化默认不生效')
    box.prop(settings, 'mapping_attribute')
    box.operator('toon_normal.initialize_mapping')
    box.operator('toon_normal.enable_mapping')
    box.operator('toon_normal.restore_ellipsoid')
    box.prop(settings, 'direction')
    box.prop(settings, 'blend')
    box.operator('toon_normal.write_selected_direction')
    box.label(text='仅单网格编辑模式下所选顶点；不猜区域或改其它部件')
    weights = layout.box()
    weights.label(text='局部权重：只写当前法向修改器的实际绑定图')
    weights.prop(settings, 'weight_value')
    weights.prop(settings, 'weight_blend')
    weights.operator('toon_normal.write_selected_weight')
    weights.label(text='权重乘目标当前强度；写权重不会改变或启用强度')
    weights.label(text='上方权重属性名用于新建，不改变此处的写入目标')
    if settings.message:
        layout.label(text=settings.message)


CLASSES = (TOONNORMAL_Settings, TOONNORMAL_OT_zero, TOONNORMAL_OT_build,
    TOONNORMAL_OT_strength, TOONNORMAL_OT_bind, TOONNORMAL_OT_disconnect,
    TOONNORMAL_OT_inspect, TOONNORMAL_OT_initialize_mapping,
    TOONNORMAL_OT_enable_mapping, TOONNORMAL_OT_clear_mapping, TOONNORMAL_OT_write_direction,
    TOONNORMAL_OT_write_weight)


def register(include_panel=True):
    global _registered, _descriptor
    if _registered:
        return
    if type(include_panel) is not bool:
        raise ValueError('include_panel须为显式布尔值')
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Scene艺术法向UI属性已被其它注册拥有')
    try:
        for cls in CLASSES + ((TOONNORMAL_PT_panel,) if include_panel else ()):
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONNORMAL_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    descriptor = bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer() != _descriptor:
        raise RuntimeError('Scene艺术法向UI属性已被替换，保留其它所有者及RNA类型')
    if descriptor is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
