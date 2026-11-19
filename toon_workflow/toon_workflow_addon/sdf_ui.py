# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded opt-in SDF authoring UI. Native UVs are never repurposed."""
from array import array
import json
import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

PROP = 'toon_face_sdf_ui'
_registered = False
_descriptor = None
_classes_registered = []


def _target(context):
    target = context.object
    if context.mode != 'OBJECT' or target is None or target.type != 'MESH':
        raise ValueError('请在对象模式明确选择一个网格')
    if target.library or target.override_library or not target.is_editable:
        raise ValueError('SDF作者操作只支持本地可编辑对象')
    mesh = target.data
    if mesh.library or mesh.override_library or not mesh.is_editable or mesh.users != 1:
        raise ValueError('SDF作者操作要求独占的本地可编辑Mesh')
    return target


def _face(target):
    from . import face_thresholds
    matches = [m for m in target.modifiers if face_thresholds.is_managed(m)]
    if len(matches) != 1:
        raise ValueError('目标须有且仅有一个已有受管面影字段以确定真实区域权重')
    modifier = matches[0]
    report = face_thresholds.inspect(target, modifier, evaluate=False)
    if report['status'] != 'valid':
        raise ValueError('已有面影字段无效：' + '; '.join(report.get('issues', [])))
    return modifier, report


def _name(name):
    if type(name) is not str or not name or len(name.encode('utf8')) > 63 or '\x00' in name:
        raise ValueError('UV层名须为1..63个UTF-8字节，不能含空字符')
    return name


def _slots(text, target):
    try:
        values = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ValueError('请明确填写皮肤材质槽JSON数组，例如[0,4,6]；不会猜测材质') from error
    if (type(values) is not list or not values or any(type(v) is not int for v in values)
            or len(values) != len(set(values))
            or any(v < 0 or v >= len(target.material_slots) or target.material_slots[v].material is None for v in values)):
        raise ValueError('皮肤材质槽须为明确、唯一且存在的非空整数数组')
    return set(values)


def _selection_snapshot(mesh):
    attributes = {}
    for attr in mesh.attributes:
        if attr.name.startswith(('.select_', '.uv_select_')):
            if attr.data_type != 'BOOLEAN':
                raise ValueError('未知选择属性类型，拒绝改变：' + attr.name)
            attributes[attr.name] = dict(domain=attr.domain, values=[bool(v.value) for v in attr.data])
    return dict(attributes=attributes,
        vertices=[bool(v.select) for v in mesh.vertices], edges=[bool(e.select) for e in mesh.edges],
        polygons=[bool(p.select) for p in mesh.polygons])


def _restore_selection(mesh, state):
    for collection, values in ((mesh.vertices, state['vertices']), (mesh.edges, state['edges']), (mesh.polygons, state['polygons'])):
        for item, value in zip(collection, values):
            if item.select != value:
                item.select = value
    for name, row in state['attributes'].items():
        attr = mesh.attributes.get(name)
        if attr is None:
            attr = mesh.attributes.new(name, 'BOOLEAN', row['domain'])
        if attr.data_type != 'BOOLEAN' or attr.domain != row['domain'] or len(attr.data) != len(row['values']):
            raise RuntimeError('原选择属性结构发生变化：' + name)
        if [bool(v.value) for v in attr.data] != row['values']:
            attr.data.foreach_set('value', row['values'])
    for attr in list(mesh.attributes):
        if attr.name.startswith(('.select_', '.uv_select_')) and attr.name not in state['attributes']:
            mesh.attributes.remove(attr)


def _uv_state(mesh):
    selection = _selection_snapshot(mesh)
    layers = []
    for layer in mesh.uv_layers:
        values = array('f', [0.]) * (2 * len(mesh.loops))
        layer.data.foreach_get('uv', values)
        layers.append(dict(name=layer.name, uv=values,
            flags={key: bool(getattr(layer, key)) for key in ('active_render', 'active_clone') if hasattr(layer, key)}))
    return dict(layers=layers, active=mesh.uv_layers.active.name if mesh.uv_layers.active else None,
        selection=selection)


def _restore_old_uv_state(mesh, state):
    for row in state['layers']:
        layer = mesh.uv_layers.get(row['name'])
        if layer is None:
            raise RuntimeError('原UV层丢失：' + row['name'])
        values = array('f', [0.]) * len(row['uv'])
        layer.data.foreach_get('uv', values)
        if values != row['uv']:
            layer.data.foreach_set('uv', row['uv'])
    # Restore false flags before true flags because these are exclusive roles.
    for key in ('active_render', 'active_clone'):
        for layer in mesh.uv_layers:
            if hasattr(layer, key) and getattr(layer, key):
                setattr(layer, key, False)
        for row in state['layers']:
            if row['flags'].get(key):
                setattr(mesh.uv_layers[row['name']], key, True)
    if state['active'] is not None:
        mesh.uv_layers.active = mesh.uv_layers[state['active']]
    _restore_selection(mesh, state['selection'])


def _sdf():
    from . import face_sdf
    return face_sdf


def _cast():
    from . import sdf_cast
    return sdf_cast


def _uv_exists(target, name):
    _name(name)
    layer = target.data.uv_layers.get(name)
    if layer is None or len(layer.data) != len(target.data.loops):
        raise ValueError('请先新增或明确选择完整的专用UV层：' + name)
    if any(not math.isfinite(float(v)) for item in layer.data for v in item.uv):
        raise ValueError('专用UV坐标含非有限值')
    return layer


def _refuse_bound_readers(target, modifier, report):
    """Never reconfigure a live material consumer, including damaged markers."""
    fields = set(report['attributes'].values())
    markers = ('toon_face_material_', 'toon_face_sdf_', 'toon_sdf_cast_', 'toon_face_cast_art_')
    for material in {s.material for s in target.material_slots if s.material}:
        if material.node_tree is None:
            continue
        if any(any(str(key).startswith(markers) for key in node.keys()) for node in material.node_tree.nodes):
            raise ValueError('目标材质仍有面影/SDF/投影读者标记；请先明确解绑后再转换carrier')
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'ShaderNodeTree' or tree.users == 0:
            continue
        if any(n.bl_idname == 'ShaderNodeAttribute' and n.attribute_name in fields for n in tree.nodes):
            raise ValueError('实际面影输出仍有Shader消费者；请先明确解绑')
        bound = ((tree.get('toon_face_threshold_target') == target and tree.get('toon_face_threshold_modifier_uid') == modifier.persistent_uid)
            or (tree.get('toon_face_sdf_target') == target and tree.get('toon_face_sdf_modifier_uid') == modifier.persistent_uid)
            or tree.get('toon_sdf_cast_target') == target)
        if bound:
            raise ValueError('仍有本目标面影/SDF/投影读者消费者；请先明确解绑')
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree and any(n.bl_idname == 'ShaderNodeAttribute' and n.attribute_name in fields for n in tree.nodes):
            raise ValueError('实际面影输出有直接材质消费者；请先明确解除')


def create_carrier(context):
    """Explicitly convert one existing unbound face field, retaining its ID."""
    from . import face_thresholds
    target = _target(context)
    settings = getattr(context.scene, PROP)
    modifier, report = _face(target)
    _uv_exists(target, settings.uv_map)
    _refuse_bound_readers(target, modifier, report)
    zero, one = 'toon_sdf_side_zero', 'toon_sdf_side_one'
    weight = _name(settings.weight_attribute)
    names = (zero, one, weight)
    if len(set(names)) != 3 or any(name == 'position' or name in report['attributes'].values() for name in names):
        raise ValueError('SDF新源属性名须互异且不能使用原位置或受管输出名')
    if any(target.data.attributes.get(name) is not None for name in names):
        raise ValueError('SDF源属性名已存在；保持原值，拒绝覆盖或自动复用')
    mesh = target.data
    source = mesh.attributes.get(report['weight_attribute'])
    if (source is None or source.data_type != 'FLOAT' or source.domain != 'POINT'
            or len(source.data) != len(mesh.vertices) or source.is_internal):
        raise ValueError('实际旧面影权重须为可读非内部FLOAT POINT源图')
    values = [float(v.value) for v in source.data]
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError('实际旧权重须全部有限且位于0..1')
    if not any(v > 0 for v in values):
        raise ValueError('实际旧面影权重全零，请先明确作者区域；不自动铺满脸部')
    group, interface, _ = face_thresholds._owned(target, modifier)
    keys = ('left', 'right', 'weight', 'strength')
    slots = {key: face_thresholds._input(modifier, interface, key) for key in keys}
    previous = {key: slot.value for key, slot in slots.items()}
    from .artistic_normals import _animated_paths
    paths = _animated_paths(target)
    if any(slot.path_from_id('value') in paths for slot in slots.values()):
        raise ValueError('待转换面影输入有原生动画；请先明确处理，保持原状态')
    created = []
    try:
        for name, data in ((zero, [0.] * len(values)), (one, [1.] * len(values)), (weight, values)):
            attr = mesh.attributes.new(name, 'FLOAT', 'POINT')
            created.append(attr.name)
            if attr.name != name:
                raise ValueError('Blender更改了请求属性名；拒绝自动重名')
            attr.data.foreach_set('value', data)
        mesh.update(); target.update_tag()
        face_thresholds.configure(target, modifier, left_attribute=zero, right_attribute=one,
            weight_attribute=weight, strength=settings.strength)
        receipt = _sdf().carrier(target, modifier)
    except Exception as error:
        failures = []
        for key, value in previous.items():
            try:
                slots[key].value = value
            except Exception as restore_error:
                failures.append(key + ': ' + str(restore_error))
        for name in reversed(created):
            try:
                attr = mesh.attributes.get(name)
                if attr is not None:
                    mesh.attributes.remove(attr)
            except Exception as restore_error:
                failures.append(name + ': ' + str(restore_error))
        mesh.update(); target.update_tag()
        if failures:
            raise RuntimeError('carrier创建失败且回滚受阻：' + '; '.join(failures)) from error
        raise
    return dict(schema='toon-sdf-carrier-ui.v1', status='created', target=target.name,
        owner=report['owner'], modifier_uid=modifier.persistent_uid, old_weight_attribute=report['weight_attribute'],
        attributes_created=list(names), cloned_weight_count=len(values), strength=settings.strength,
        carrier=receipt, core_group=group.name, uv_map=settings.uv_map)


def copy_image(context):
    """Make an explicit independent author copy without changing any binding."""
    settings = getattr(context.scene, PROP)
    original = settings.image
    if original is None:
        raise ValueError('请先选择一张SDF贴图')
    sdf = _sdf()
    before = sdf._image_content_descriptor(original, settings.channel)
    candidate = original.copy()
    try:
        candidate.name = original.name + ' 独立副本'
        candidate.use_fake_user = False
        after = sdf._image_content_descriptor(candidate, settings.channel)
        if not sdf._same_image_identity(before, after):
            raise ValueError('独立贴图副本的内容与原图不一致，已保留原选择')
        settings.image = candidate
    except Exception:
        if candidate.users == 0:
            bpy.data.images.remove(candidate)
        raise
    return dict(status='copied', image=candidate.name, source=original.name,
                source_sha256=after['source_sha256'])


def bind_reader(context):
    target = _target(context)
    settings = getattr(context.scene, PROP)
    modifier, _ = _face(target)
    _uv_exists(target, settings.uv_map)
    _sdf().carrier(target, modifier)
    vertical = (dict(vertical_image=settings.vertical_image, vertical_uv=settings.vertical_uv_map)
                if settings.direction_mode in ('POLAR_LRUD','DIRECTION_KEYS','DIRECTION_CONTINUITY','DIRECTION_AUTHORED') else {})
    if settings.direction_mode in ('DIRECTION_KEYS','DIRECTION_CONTINUITY','DIRECTION_AUTHORED'):
        vertical.update(direction_image=settings.direction_image, direction_uv=settings.direction_uv_map)
        vertical['direction_correction'] = settings.direction_correction
    if settings.direction_mode in ('DIRECTION_CONTINUITY','DIRECTION_AUTHORED') and settings.direction_correction:
        vertical.update(pair_image=settings.pair_image, pair_uv=settings.pair_uv_map,
                        pair_filter_radius_texels=settings.pair_filter_radius_texels)
    return _sdf().bind(context, settings.image, settings.uv_map, channel=settings.channel,
        mirror=settings.mirror, shadow_color=tuple(settings.shadow_color), softness=settings.softness,
        filter_radius_texels=settings.filter_radius_texels, direction_mode=settings.direction_mode, **vertical)


def switch_direction(context):
    from . import face_thresholds
    target = _target(context)
    modifier, _ = _face(target)
    settings = getattr(context.scene, PROP)
    profile = {'AZIMUTH':face_thresholds.PROFILE, 'POLAR_LRUD':face_thresholds.POLAR_PROFILE,
               'DIRECTION_KEYS':face_thresholds.DIRECTION_PROFILE,
               'DIRECTION_CONTINUITY':face_thresholds.CONTINUITY_PROFILE,
               'DIRECTION_AUTHORED':face_thresholds.CONTINUITY_PROFILE}[settings.direction_mode]
    return face_thresholds.switch_profile(target, modifier, profile)


def _retire_reader_identity(reader, target, modifier, core):
    """Only the known managed definitions for this explicit face carrier."""
    from . import face_thresholds as F, face_material as M, face_sdf as S, sdf_cast as C, face_cast_art as A
    tags = [(key, reader.get(key)) for key in (M.PROFILE_KEY, S.PROFILE_KEY, C._PROFILE, A.PROFILE_KEY)
            if key in reader]
    if len(tags) != 1:
        raise ValueError('未使用读者的受管身份不明确，保留全部数据：' + reader.name)
    key, profile = tags[0]
    if key == M.PROFILE_KEY and profile == M.PROFILE:
        identity = (reader.get(M._TARGET) == target and reader.get(M._UID) == modifier.persistent_uid
                    and reader.get(M.OWNER) == core[F.OWNER])
    elif key == S.PROFILE_KEY and profile in S.SUPPORTED_PROFILES:
        info = S._intrinsic(reader)
        identity = (reader.get(S._TARGET) == target and info['uid'] == modifier.persistent_uid
                    and info['carrier']['core_owner'] == core[F.OWNER])
    elif key == C._PROFILE and profile == C.READER_PROFILE:
        face = json.loads(reader.get(C._FACE, ''))
        identity = (reader.get(C._TARGET) == target and type(face) is dict
                    and face.get('owner') == core[F.OWNER]
                    and face.get('modifier_uid') == modifier.persistent_uid)
    elif key == A.PROFILE_KEY and profile in (A.PROFILE, A.PROFILE_V2):
        identity = reader.get(A._TARGET) == target and reader.get(A._UID) == modifier.persistent_uid
    else:
        raise ValueError('不清理未知面影读者profile：' + str(profile))
    if not identity or F._reader_edited(reader):
        raise ValueError('未使用读者已改动或不属于当前面影，保留全部数据：' + reader.name)
    return profile


def retire_unused_readers(context):
    """Preflight all readers/images, then retire unused managed definitions.

    Run as the dedicated UNDO operator for native-ID deletion. A rare error
    after native deletion is reported as partial/Undo-required, never CANCELLED
    with hidden removals. No image, UV or geometry carrier is removed.
    """
    from . import face_thresholds as F
    target = _target(context)
    modifier, _ = _face(target)
    core, _, _ = F._owned(target, modifier)
    readers = list(F._readers(core))
    report = dict(schema='toon-sdf-retire-unused-readers.v1', status='unchanged', target=target.name,
        modifier_uid=modifier.persistent_uid, removed_names=[], removed_profiles=[], skipped_users=[],
        retained_image_count=0, retained_images=[], undo_required=False)
    if not readers:
        return report
    # Native references, including custom ID properties, are counted separately
    # from visible group instances and fake-user storage.
    users = bpy.data.user_map(subset=set(readers))
    instances = {reader: [] for reader in readers}
    trees = [('Material:' + mat.name, mat.node_tree) for mat in bpy.data.materials if mat.node_tree]
    trees.extend(('NodeGroup:' + tree.name, tree) for tree in bpy.data.node_groups)
    for name, tree in trees:
        for node in tree.nodes:
            group = getattr(node, 'node_tree', None)
            if group in instances:
                instances[group].append(name + '/' + node.name)
    candidates = []
    for reader in readers:
        fake, count = bool(reader.use_fake_user), int(reader.users)
        if count < int(fake):
            raise ValueError('读者用户计数不一致，未清理：' + reader.name)
        consumers = users.get(reader, set())
        if count != int(fake) or instances[reader] or consumers:
            report['skipped_users'].append(dict(name=reader.name, users=count, fake_user=fake,
                instances=instances[reader], consumers=sorted(value.name for value in consumers)))
            continue
        if reader.library or reader.override_library or not reader.is_editable:
            raise ValueError('未使用读者不是可编辑本地定义，未清理：' + reader.name)
        profile = _retire_reader_identity(reader, target, modifier, core)
        candidates.append(dict(group=reader, pointer=reader.as_pointer(), name=reader.name,
            profile=profile, fake_before=fake))
    # user_of_id counts all native Image pointers, not only Texture nodes.
    # Resolve every affected count/protection before the first fake flag write.
    image_plan = []
    for image in bpy.data.images:
        references = sum(item['group'].user_of_id(image) for item in candidates)
        if not references:
            continue
        remaining = int(image.users) - int(image.use_fake_user) - references
        if remaining < 0:
            raise ValueError('图像引用计数不明确，未清理：' + image.name)
        preserve = remaining == 0 and not image.use_fake_user
        if preserve and (image.library or image.override_library or not image.is_editable):
            raise ValueError('无法保留即将孤立的图像，未清理：' + image.name)
        image_plan.append(dict(image=image, name=image.name, fake_before=bool(image.use_fake_user),
            references_removed=references, predicted_remaining_users=remaining, preserve=preserve))

    removed = []
    try:
        for item in image_plan:
            if item['preserve']:
                item['image'].use_fake_user = True
        for item in candidates:
            reader = item['group']
            if item['fake_before']:
                reader.use_fake_user = False
            bpy.data.node_groups.remove(reader, do_unlink=False)
            removed.append(item)
    except Exception as error:
        present = {group.as_pointer(): group for group in bpy.data.node_groups}
        removed = [item for item in candidates if item['pointer'] not in present]
        restoration_errors = []
        for item in candidates:
            group = present.get(item['pointer'])
            if group is not None:
                try:
                    group.use_fake_user = item['fake_before']
                except Exception as restore_error:
                    restoration_errors.append(item['name'] + ': ' + str(restore_error))
        for item in image_plan:
            image = item['image']
            try:
                # Preserve truly orphaned Images after a partial delete until
                # the user invokes this operator's Undo; do not lose the pixels.
                image.use_fake_user = item['fake_before'] or (bool(removed) and image.users == int(image.use_fake_user))
            except Exception as restore_error:
                restoration_errors.append(item['name'] + ': ' + str(restore_error))
        if not removed and not restoration_errors:
            raise RuntimeError('清理未完成，已恢复原标记：' + str(error)) from error
        report.update(status='partial_undo_required', undo_required=True, error=str(error),
            restoration_errors=restoration_errors)
    else:
        report['status'] = 'retired' if removed else 'unchanged'
    report['removed_names'] = [item['name'] for item in removed]
    report['removed_profiles'] = [item['profile'] for item in removed]
    report['retained_images'] = [dict(name=item['name'], references_removed=item['references_removed'],
        predicted_remaining_users=item['predicted_remaining_users'], fake_before=item['fake_before'],
        fake_after=bool(item['image'].use_fake_user)) for item in image_plan]
    report['retained_image_count'] = len(report['retained_images'])
    return report


def unbind_reader(context):
    _target(context)
    return _sdf().unbind(context)


def _cast_modifier(target):
    matches = [m for m in target.modifiers if _cast().is_managed(m)]
    if len(matches) != 1:
        raise ValueError('目标须有且仅有一个专用原生投影carrier')
    return matches[0]


def create_cast(context):
    target = _target(context); modifier, _ = _face(target)
    _sdf().carrier(target, modifier)
    created = _cast().create(target, modifier)
    return dict(status='created', modifier=created.name, modifier_uid=created.persistent_uid)


def bind_cast(context):
    target = _target(context); modifier, _ = _face(target)
    return _cast().bind(target, modifier, _cast_modifier(target))


def unbind_cast(context):
    target = _target(context); modifier, _ = _face(target)
    return _cast().unbind(target, modifier, _cast_modifier(target))


def _cast_art():
    from . import face_cast_art
    return face_cast_art


def _cast_art_values(settings):
    return dict(strength=settings.cast_art_strength, lit_color=tuple(settings.cast_art_lit_color),
                black_level=settings.cast_art_black_level, white_level=settings.cast_art_white_level)


def bind_cast_art(context):
    _target(context)
    settings = getattr(context.scene, PROP)
    return _cast_art().bind(context, settings.cast_art_image, channel=settings.cast_art_channel,
                            animate_strength=settings.cast_art_animate_strength,
                            **_cast_art_values(settings))


def update_cast_art(context):
    _target(context)
    return _cast_art().update(context, **_cast_art_values(getattr(context.scene, PROP)))


def unbind_cast_art(context):
    _target(context)
    return _cast_art().unbind(context)


def key_cast_art(context):
    _target(context)
    settings = getattr(context.scene, PROP)
    return _cast_art().insert_strength_key(context,
        frame=context.scene.frame_current + context.scene.frame_subframe,
        value=settings.cast_art_strength)


def clear_cast_art_animation(context):
    _target(context)
    return _cast_art().clear_strength_animation(context)


def inspect_cast_art(context):
    _target(context)
    report = _cast_art().inspect(context)
    descriptor = report.get('descriptor')
    if descriptor is not None:
        settings = getattr(context.scene, PROP)
        for name, value in descriptor['settings'].items():
            setattr(settings, 'cast_art_'+name, value)
        settings.cast_art_channel = descriptor['channel']
        settings.cast_art_image = bpy.data.images.get(descriptor['image']['name'])
        settings.cast_art_animate_strength = descriptor['profile'] == _cast_art().PROFILE_V2
    return report


def inspect_sdf(context):
    target = _target(context); modifier, report = _face(target)
    carrier = _sdf().carrier(target, modifier)
    readers = []
    for material in {s.material for s in target.material_slots if s.material}:
        if material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if (node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None
                    and (_sdf()._MARK in node or node.node_tree.get(_sdf()._TARGET) == target)):
                readers.append(_sdf().validate_reader(node.node_tree, target=target, modifier=modifier, require_live=True))
    casts = [m for m in target.modifiers if _cast().is_managed(m)]
    if len(casts) > 1:
        raise ValueError('发现多个投影carrier，拒绝猜测')
    cast = _cast().inspect(target, modifier, casts[0], evaluate=False) if casts else None
    if cast and cast.get('status') != 'valid':
        raise ValueError('投影carrier无效：' + '; '.join(cast.get('issues', [])))
    art = [_cast_art().validate_material(material, target=target)
           for material in {s.material for s in target.material_slots if s.material}]
    return dict(status='valid', target=target.name, carrier=carrier,
        strength=report['strength'], bound_readers=readers, cast=cast,
        cast_art=[value for value in art if value is not None],
        qualification='原生结构与身份检查；不是可见点击、图像或艺术资格')


def create_front_uv(context):
    """Add a dedicated raw-reference XZ chart for explicitly weighted polygons.

    The complete selected polygons define bounds. A partial positive vertex
    admits the polygon; no material, front/back classifier or anatomical region
    is inferred. Outside loops of the new layer are zero, old layers are exact.
    """
    target = _target(context)
    settings = getattr(context.scene, PROP)
    name = _name(settings.uv_map)
    mesh = target.data
    if mesh.uv_layers.get(name) is not None or mesh.attributes.get(name) is not None:
        raise ValueError('专用UV名已存在，保留原值不覆盖：' + name)
    reference = settings.reference
    if (reference is None or reference == target or reference.library or reference.override_library
            or reference not in list(bpy.data.objects)):
        raise ValueError('请明确选择独立本地头部方向参考')
    _, report = _face(target)
    if reference.name != report['reference']:
        raise ValueError('投影参考必须与已有面影字段实际绑定参考相同')
    weight_name = report['weight_attribute']
    attr = mesh.attributes.get(weight_name)
    if attr is None or attr.data_type != 'FLOAT' or attr.domain != 'POINT' or len(attr.data) != len(mesh.vertices):
        raise ValueError('实际面影区域权重须为原Mesh上的FLOAT POINT')
    weights = [float(v.value) for v in attr.data]
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in weights):
        raise ValueError('实际面影区域权重须全部有限且位于0..1')
    slots = _slots(settings.material_slots, target)
    polygons = [p for p in mesh.polygons if p.material_index in slots and any(weights[i] > 0 for i in p.vertices)]
    if not polygons:
        raise ValueError('明确材质槽中没有实际面影权重非零的面；不自动扩大范围')
    matrix = reference.matrix_world.inverted() @ target.matrix_world
    if not all(math.isfinite(float(v)) for row in matrix for v in row):
        raise ValueError('投影参考变换不可逆或含非有限值')
    indices = sorted({i for p in polygons for i in p.vertices})
    coordinates = {i: tuple(float(v) for v in matrix @ mesh.vertices[i].co) for i in indices}
    if any(not math.isfinite(v) for q in coordinates.values() for v in q):
        raise ValueError('原Mesh参考坐标含非有限值')
    xmin, xmax = min(q[0] for q in coordinates.values()), max(q[0] for q in coordinates.values())
    zmin, zmax = min(q[2] for q in coordinates.values()), max(q[2] for q in coordinates.values())
    if xmax - xmin <= 1e-12 or zmax - zmin <= 1e-12:
        raise ValueError('投影范围在参考X/Z方向退化，无法创建面部UV')
    uv = array('f', [0.]) * (2 * len(mesh.loops))
    written = []
    for poly in polygons:
        for loop in poly.loop_indices:
            q = coordinates[mesh.loops[loop].vertex_index]
            uv[loop*2] = .05 + .90 * (q[0] - xmin) / (xmax - xmin)
            uv[loop*2+1] = .05 + .90 * (q[2] - zmin) / (zmax - zmin)
            written.append(loop)
    before = _uv_state(mesh)
    layer = None
    try:
        layer = mesh.uv_layers.new(name=name, do_init=False)
        if layer.name != name:
            raise ValueError('Blender更改了请求UV名，拒绝截断或自动重名')
        layer.data.foreach_set('uv', uv)
        _restore_old_uv_state(mesh, before)
        mesh.update(); target.update_tag()
    except Exception:
        if layer is not None and mesh.uv_layers.get(layer.name) == layer:
            mesh.uv_layers.remove(layer)
        _restore_old_uv_state(mesh, before)
        mesh.update(); target.update_tag()
        raise
    return dict(schema='toon-sdf-front-uv.v1', status='created', target=target.name,
        uv_map=name, reference=reference.name, weight_attribute=weight_name,
        material_slots=sorted(slots), polygon_indices=[p.index for p in polygons],
        polygons=len(polygons), vertices=len(indices), written_loops=len(written),
        q_bounds=dict(x=[xmin, xmax], z=[zmin, zmax]), uv_bounds=[.05, .05, .95, .95],
        coordinate_space='RAW_REFERENCE_XZ', margin=.05,
        scope='explicit material slots AND any source vertex actual face weight >0; entire admitted polygons',
        outside_new_uv=[0., 0.], old_uvs_preserved=[row['name'] for row in before['layers']],
        limitation='Raw source reference projection; front/back may overlap. No automatic UV unwrap, live deformation or artistic qualification.')


class TOONSDF_Settings(bpy.types.PropertyGroup):
    direction_mode: EnumProperty(name='面影方向模式', items=(
        ('AZIMUTH', '水平左右', '保留原水平影形和顶底淡出'),
        ('POLAR_LRUD', '三维上下左右', '真实光向；RGB分别为水平、上方、下方阈值，上下不镜像'),
        ('DIRECTION_KEYS', '三维关键方向修正', '保留水平/上下图，以第三张RG距离图修正上后方影形并保护原顶光；14键限定模式'),
        ('DIRECTION_CONTINUITY', '连续方向面影', '在关键方向模式上增加两方向独立影形和顶光细节保护；需要适配实际UV的独立图像'),
        ('DIRECTION_AUTHORED', '定向影形与自然受光', '已制作的方向采用专用影形，其余方向保留原面部光影；保护顶光细节')),
        default='AZIMUTH')
    image: PointerProperty(name='SDF方向阈值图像', type=bpy.types.Image)
    vertical_image: PointerProperty(name='上下光独立阈值图像', type=bpy.types.Image,
        description='独立Non-Color图像；G上方、B下方，与水平图和UV分别核验')
    vertical_uv_map: StringProperty(name='上下光独立UV层', default='ToonFaceVertical')
    direction_image: PointerProperty(name='关键方向距离图像', type=bpy.types.Image,
        description='独立Non-Color图像，R上后方/G顶光；必须适配实际消费UV')
    direction_uv_map: StringProperty(name='关键方向实际UV层', default='ToonFaceVertical')
    direction_correction: BoolProperty(name='启用关键方向修正', default=True,
        description='关闭时显式保留原上下左右响应，供共用新载体的鼻影等独立材质使用')
    pair_image: PointerProperty(name='连续影形图像', type=bpy.types.Image,
        description='独立Non-Color图像；R/G为两个方向影形，B为原顶光作者域的下颌运输距离，须匹配真实UV')
    pair_uv_map: StringProperty(name='连续影形实际UV层', default='ToonFaceVertical')
    pair_filter_radius_texels: FloatProperty(name='连续影形采样半径(像素)', default=1., min=.001, max=64.,
        description='此图独立的绑定时采样半径；不会替换水平、上下及原方向图的采样设置')
    key_recipe_path: StringProperty(name='关键光角计划', subtype='FILE_PATH', default='',
        description='明确各光角的二值明暗图与当前真实UV绑定；白影黑亮')
    key_output_directory: StringProperty(name='新的阈值图目录', subtype='DIR_PATH', default='',
        description='新目录保存PNG16、来源计划与检查报告；已有目录不覆盖')
    uv_map: StringProperty(name='专用UV层', default='ToonFaceSDF')
    material_slots: StringProperty(name='明确皮肤材质槽JSON', default='',
        description='明确填写材质槽整数数组，例如[0,4,6]；不按名称猜测皮肤')
    weight_attribute: StringProperty(name='SDF区域权重属性', default='toon_sdf_weight')
    reference: PointerProperty(name='头部方向参考', type=bpy.types.Object)
    light: PointerProperty(name='方向太阳', type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'LIGHT' and obj.data.type == 'SUN')
    strength: FloatProperty(name='待应用强度', default=0., min=0., max=1.)
    channel: EnumProperty(name='阈值通道', items=(('R','R',''),('G','G',''),('B','B','')), default='R')
    mirror: BoolProperty(name='左右镜像采样', default=True)
    shadow_color: FloatVectorProperty(name='阴影颜色', size=4, subtype='COLOR', default=(.45,.34,.31,1.), min=0., max=1.)
    softness: FloatProperty(name='阈值柔和度', default=.02, min=0., max=1.)
    filter_radius_texels: FloatProperty(name='阴影图采样平滑(像素)', default=0., min=0., max=64.,
        description='在阴影比较前平滑贴图采样，0关闭。更换此数值时先建立独立贴图副本，再移除旧绑定并重新绑定；单位为原图像素')
    cast_art_image: PointerProperty(name='投影接受图', type=bpy.types.Image,
        description='独立Non-Color数据图；白保留上游，黑按强度使用亮面底色')
    cast_art_channel: EnumProperty(name='接受图通道', items=(('R','R',''),('G','G',''),('B','B','')), default='R')
    cast_art_strength: FloatProperty(name='接受层强度', default=0., min=0., max=1.,
        description='0保留原来源；动画模式用强度关键帧，非零会抑制黑区的上游阴影')
    cast_art_animate_strength: BoolProperty(name='新层启用强度动画', default=False,
        description='仅新增时选择；旧静态层须明确移除再绑定，保留原SDF来源')
    cast_art_lit_color: FloatVectorProperty(name='亮面底色', size=4, subtype='COLOR',
        default=(1.,1.,1.,1.), min=0., max=1., description='黑区混向此明确底色；可能覆盖原贴图细节')
    cast_art_black_level: FloatProperty(name='黑位', default=0., min=0., max=1.)
    cast_art_white_level: FloatProperty(name='白位', default=1., min=0., max=1.)
    message: StringProperty(name='操作结果')


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
            report = self.action(context)
            if isinstance(report, dict) and report.get('status') == 'invalid':
                raise ValueError('; '.join(report.get('issues', ['SDF状态无效'])))
            settings.message = self.bl_label + '完成'
            self.report({'INFO'}, settings.message)
            return {'FINISHED'}
        except Exception as error:
            settings.message = str(error); self.report({'ERROR'}, settings.message)
            return {'CANCELLED'}


class TOONSDF_OT_front_uv(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.create_front_uv'; bl_label = '新增专用正面UV（保留原UV）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_front_uv)


class TOONSDF_OT_carrier(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.create_carrier'; bl_label = '明确转换已有面影为SDF carrier'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_carrier)


class TOONSDF_OT_copy_image(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.copy_image'; bl_label = '建立独立贴图副本'
    bl_description = '复制当前贴图供新的采样设置使用，保留原贴图与现有材质'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(copy_image)


class TOONSDF_OT_compile_keys(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.compile_keys'; bl_label = '编译关键光角阈值图'
    bl_description = '按当前真实UV和已审查的关键光角图生成PNG16，并放入待绑定贴图；材质保持当前绑定'
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def action(context):
        from .sdf_key_authoring import compile_from_ui
        return compile_from_ui(context)


class TOONSDF_OT_bind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.bind_reader'; bl_label = '绑定活动材质SDF图像读者'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(bind_reader)


class TOONSDF_OT_unbind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.unbind_reader'; bl_label = '解绑活动材质SDF读者'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(unbind_reader)


class TOONSDF_OT_direction(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.switch_direction'; bl_label = '应用面影方向模式'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(switch_direction)


class TOONSDF_OT_retire_readers(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.retire_unused_readers'; bl_label = '清理未使用的旧面影读者'
    bl_description = '先保检查点；仅移除当前面影未改动、无实例的旧读者定义，保留图像与UV，可撤销'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(retire_unused_readers)

    def execute(self, context):
        settings = getattr(context.scene, PROP)
        try:
            result = self.action(context)
        except Exception as error:
            settings.message = str(error); self.report({'ERROR'}, settings.message)
            return {'CANCELLED'}
        if result['undo_required']:
            settings.message = '部分清理遇到异常；图像已保留，请立即撤销本次清理。' + result.get('error', '')
            self.report({'WARNING'}, settings.message)
        else:
            settings.message = ('已清理' + str(len(result['removed_names'])) + '个旧读者，保留'
                + str(result['retained_image_count']) + '张图像；跳过'
                + str(len(result['skipped_users'])) + '个仍被使用的读者')
            self.report({'INFO'}, settings.message)
        # A partial native-ID commit must still receive its single Undo step.
        return {'FINISHED'}


class TOONSDF_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.inspect'; bl_label = '检查SDF实际carrier与读者'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(inspect_sdf)


class TOONSDF_OT_cast_create(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.create_cast'; bl_label = '新增单太阳投影carrier（可选）'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(create_cast)


class TOONSDF_OT_cast_bind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.bind_cast'; bl_label = '明确重定向活动材质Normal投影传感'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(bind_cast)


class TOONSDF_OT_cast_unbind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.unbind_cast'; bl_label = '解绑投影传感并恢复原Normal'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(unbind_cast)


class TOONSDF_OT_cast_art_bind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.bind_cast_art'; bl_label = '新增活动材质艺术接受层'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(bind_cast_art)


class TOONSDF_OT_cast_art_update(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.update_cast_art'; bl_label = '更新艺术接受层参数'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(update_cast_art)


class TOONSDF_OT_cast_art_unbind(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.unbind_cast_art'; bl_label = '移除接受层并恢复原来源'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(unbind_cast_art)


class TOONSDF_OT_cast_art_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.inspect_cast_art'; bl_label = '读取实际艺术接受层'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(inspect_cast_art)


class TOONSDF_OT_cast_art_key(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.key_cast_art_strength'; bl_label = '当前时刻插入接受强度关键帧'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(key_cast_art)


class TOONSDF_OT_cast_art_clear(_Action, bpy.types.Operator):
    bl_idname = 'toon_sdf.clear_cast_art_animation'; bl_label = '解除强度动画并保留Action'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(clear_cast_art_animation)


CLASSES = (TOONSDF_Settings, TOONSDF_OT_front_uv, TOONSDF_OT_carrier,
    TOONSDF_OT_copy_image, TOONSDF_OT_compile_keys, TOONSDF_OT_bind, TOONSDF_OT_retire_readers, TOONSDF_OT_direction, TOONSDF_OT_unbind, TOONSDF_OT_inspect,
    TOONSDF_OT_cast_create, TOONSDF_OT_cast_bind, TOONSDF_OT_cast_unbind,
    TOONSDF_OT_cast_art_bind, TOONSDF_OT_cast_art_update, TOONSDF_OT_cast_art_unbind, TOONSDF_OT_cast_art_inspect,
    TOONSDF_OT_cast_art_key, TOONSDF_OT_cast_art_clear)


def draw_contents(layout, context):
    settings = getattr(context.scene, PROP)
    layout.label(text='图像逐像素方向阈值；额外压暗层，不提亮原暗面')
    for key in ('image','uv_map','material_slots','weight_attribute','reference','light'):
        layout.prop(settings, key)
    layout.operator('toon_sdf.copy_image', icon='DUPLICATE')
    keys = layout.box()
    keys.label(text='关键光角形状 → 方向阈值图')
    keys.prop(settings, 'key_recipe_path')
    keys.prop(settings, 'key_output_directory')
    keys.operator('toon_sdf.compile_keys')
    keys.label(text='先审查图像在实际UV上的明暗形状，再使用生成图')
    layout.operator('toon_sdf.create_front_uv')
    for key in ('strength','channel','mirror','shadow_color','softness','filter_radius_texels'):
        layout.prop(settings, key)
    layout.label(text='采样平滑在绑定时应用；0关闭')
    layout.label(text='更换平滑数值前，先建立独立贴图副本')
    layout.operator('toon_sdf.create_carrier')
    layout.label(text='仅转换已有唯一面影；先明确解绑旧面影/SDF/投影读者')
    layout.operator('toon_sdf.retire_unused_readers')
    layout.prop(settings, 'direction_mode')
    layout.operator('toon_sdf.switch_direction')
    if settings.direction_mode in ('POLAR_LRUD','DIRECTION_KEYS','DIRECTION_CONTINUITY','DIRECTION_AUTHORED'):
        layout.prop(settings, 'vertical_image')
        layout.prop(settings, 'vertical_uv_map')
        layout.label(text='水平图R左右；独立上下图G上/B下，不镜像')
        layout.label(text='先解绑读者并移除投影carrier，再切换；需正采样半径')
    if settings.direction_mode in ('DIRECTION_KEYS','DIRECTION_CONTINUITY','DIRECTION_AUTHORED'):
        layout.prop(settings, 'direction_image')
        layout.prop(settings, 'direction_uv_map')
        layout.prop(settings, 'direction_correction')
        layout.label(text='第三图R上后方/G顶光；按实际UV与各自图像尺寸采样')
        layout.label(text='顶光保护沿用原上方阈值；并非任意方向独立作者图')
    if settings.direction_mode in ('DIRECTION_CONTINUITY','DIRECTION_AUTHORED') and settings.direction_correction:
        layout.prop(settings, 'pair_image')
        layout.prop(settings, 'pair_uv_map')
        layout.prop(settings, 'pair_filter_radius_texels')
        layout.label(text='连续图与原图分别采样；先检查真实UV与中间光角')
    layout.operator('toon_sdf.bind_reader')
    layout.operator('toon_sdf.unbind_reader')
    layout.operator('toon_sdf.inspect')
    cast = layout.box()
    cast.label(text='可选单太阳投影传感；会明确重定向Diffuse.Normal')
    cast.operator('toon_sdf.create_cast')
    cast.operator('toon_sdf.bind_cast')
    cast.operator('toon_sdf.unbind_cast')
    layout.label(text='新增独立UV；明确材质与非零面影权重限定范围')
    layout.label(text='初始化强度0；不自动覆盖原UV或原面影材质')
    art = layout.box()
    art.label(text='可选艺术投影接受层（初始强度0）')
    art.label(text='白保留上游；黑区会抑制阴影，包括外物投影')
    art.label(text='使用已有SDF区域和UV；亮面底色可能覆盖贴图细节')
    for key in ('cast_art_image', 'cast_art_channel', 'cast_art_strength', 'cast_art_lit_color',
                'cast_art_black_level', 'cast_art_white_level'):
        art.prop(settings, key)
    art.prop(settings, 'cast_art_animate_strength')
    art.operator('toon_sdf.bind_cast_art')
    art.operator('toon_sdf.update_cast_art')
    art.operator('toon_sdf.inspect_cast_art')
    art.operator('toon_sdf.key_cast_art_strength')
    art.operator('toon_sdf.clear_cast_art_animation')
    art.operator('toon_sdf.unbind_cast_art')
    if settings.message:
        layout.label(text=settings.message)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('Scene SDF UI属性已由其它注册拥有')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls); _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONSDF_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer(); _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    prop = bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None and prop.as_pointer() != _descriptor:
        raise RuntimeError('Scene SDF属性已被替换，保留其它所有者')
    if prop is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear(); _registered = False
