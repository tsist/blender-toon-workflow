# SPDX-License-Identifier: GPL-3.0-or-later
"""CEL family authoring in the single workbench.

Portable documents keep settings and explicit roles. Runtime rollback tokens
remain in this module; saved receipts only witness current native state.
"""
import json
import math
import uuid
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, PointerProperty, StringProperty
from mathutils import Vector

from . import cel_ui, line_template_ui

PROP = 'toon_style_family_ui'
_registered = False
_classes_registered = []
_descriptor = None
_tokens = {}


def _core(document=None):
    if document is not None and document.get('schema') == 'toon-style-family.v2':
        from . import style_family_v2
        return style_family_v2
    from . import style_family
    return style_family


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def settings(context):
    return getattr(context.scene, PROP)


class TOONFAMILY_Settings(bpy.types.PropertyGroup):
    name: StringProperty(name='家族名称', default='CEL 风格家族')
    family_id: StringProperty(options={'HIDDEN'})
    family_schema: StringProperty(default='toon-style-family.v1', options={'HIDDEN'})
    family_kind: EnumProperty(name='新家族机制', items=[
        ('CEL', 'CEL 硬边', '显式零宽色阶边界，保留现有色表与修饰'),
        ('GAME', 'GAME 柔和边界', '普通 CEL3/reflect 的合法非零边界，保留现有修饰'),
        ('MANGA', 'MANGA 整镜头', '保留原材质，前景和背景分别处理黑白网点与排线')], default='CEL')
    background_roles: StringProperty(name='背景材质角色', default='[]',
        description='明确输入配方角色，以逗号分隔；没有背景目标时输入 []；不按物体名推测')
    look_document: StringProperty(options={'HIDDEN'})
    line_mode: EnumProperty(name='描边组件', items=[
        ('PRESERVE', '保留当前完整描边', '保留原作者及 INDEX 绘制，不作为便携运输'),
        ('TEMPLATE', '使用完整描边模板', '使用已显式绑定的完整模板，整体替换并保留旧记录')],
        default='PRESERVE')
    sun: PointerProperty(name='方向参考 Sun', type=bpy.types.Object)
    path: StringProperty(name='家族 JSON', subtype='FILE_PATH')
    receipt: StringProperty(options={'HIDDEN'})
    message: StringProperty()


_CEL_FIELDS = ('recipe_schema', 'recipe_id', 'recipe_name', 'revision',
               'line_policy', 'bind_role', 'line_role', 'file_path', 'receipt', 'message')
_SLOT_FIELDS = ('role', 'target', 'slot', 'link', 'expected_name', 'expected_uid', 'binding_epoch')
_FAMILY_FIELDS = ('name', 'family_id', 'line_mode', 'sun', 'path', 'receipt', 'message',
                  'family_schema', 'family_kind', 'background_roles', 'look_document')
_LINE_FIELDS = ('name', 'path', 'document', 'mode', 'conflict', 'receipt', 'message')
_LINE_BINDING_FIELDS = ('kind', 'role', 'target_name', 'target_uid', 'epoch')


def _capture_cel(context):
    u = cel_ui._settings(context)
    doc = cel_ui.document(context) if u.recipe_id else None
    return dict(fields={key: getattr(u, key) for key in _CEL_FIELDS}, document=doc,
                slots=[{key: getattr(row, key) for key in _SLOT_FIELDS} for row in u.slots],
                line_targets=[dict(role=row.role, target=row.target) for row in u.line_targets])


def _restore_cel(context, saved):
    u = cel_ui._settings(context)
    if saved['document'] is None:
        u.roles.clear()
        u.lines.clear()
    else:
        cel_ui._load_document(u, saved['document'])
    u.slots.clear()
    for values in saved['slots']:
        row = u.slots.add()
        for key, value in values.items():
            setattr(row, key, value)
    u.line_targets.clear()
    for values in saved['line_targets']:
        row = u.line_targets.add()
        row.role, row.target = values['role'], values['target']
    for key, value in saved['fields'].items():
        setattr(u, key, value)


def _capture_line(context):
    u = line_template_ui.settings(context)
    return dict(fields={key: getattr(u, key) for key in _LINE_FIELDS},
                bindings=[{key: getattr(row, key) for key in _LINE_BINDING_FIELDS}
                          for row in u.bindings],
                ink_bindings=[line_template_ui._ink_row(row) for row in u.ink_bindings])


def _restore_line(context, saved):
    u = line_template_ui.settings(context)
    for key, value in saved['fields'].items():
        setattr(u, key, value)
    u.bindings.clear()
    for values in saved['bindings']:
        row = u.bindings.add()
        for key, value in values.items():
            setattr(row, key, value)
    u.ink_bindings.clear()
    for values in saved['ink_bindings']:
        row = u.ink_bindings.add()
        for key, value in values.items():
            setattr(row, key, value)


def _family_fields(context):
    u = settings(context)
    return {key: getattr(u, key) for key in _FAMILY_FIELDS}


def _restore_family(context, saved):
    u = settings(context)
    for key, value in saved.items():
        setattr(u, key, value)


def _selection(context, targets=()):
    objects = set(targets)
    if context.view_layer.objects.active is not None:
        objects.add(context.view_layer.objects.active)
    return dict(active=context.view_layer.objects.active,
                selected=list(context.selected_objects),
                indices={obj: obj.active_material_index for obj in objects
                         if obj is not None and obj.type == 'MESH'})


def _restore_selection(context, saved):
    for obj in context.view_layer.objects:
        obj.select_set(obj in saved['selected'])
    for obj, index in saved['indices'].items():
        obj.active_material_index = index
    context.view_layer.objects.active = saved['active']


def _kwargs(context, doc):
    if doc['line']['mode'] != 'TEMPLATE':
        return {}
    u = line_template_ui.settings(context)
    if u.mode != 'REPLACE' or u.conflict != 'ERROR':
        raise ValueError('家族的完整描边模板须选择整体替换及同名停止')
    return dict(template_bindings=line_template_ui.bindings(context),
                ink_bindings=line_template_ui.ink_bindings(context))


def document(context):
    u = settings(context)
    if not u.family_id:
        raise ValueError('请先从当前配方建立家族，或导入家族文件')
    c = cel_ui._settings(context)
    if c.line_policy != 'PRESERVE':
        raise ValueError('家族单独处理描边，请将 CEL 配方描边处理设为保留现有描边')
    line = dict(mode=u.line_mode)
    if u.line_mode == 'TEMPLATE':
        lu = line_template_ui.settings(context)
        if not lu.document:
            raise ValueError('请先在完整描边模板区导入并显式绑定模板')
        line['document'] = json.loads(lu.document)
    value = dict(schema=u.family_schema, family_id=u.family_id,
        name=u.name, family='CEL', recipe=cel_ui.document(context), line=line)
    if u.family_schema == 'toon-style-family.v2':
        value['family'] = u.family_kind
        value['look'] = json.loads(u.look_document) if u.look_document else None
        if u.family_kind == 'MANGA':
            if value['look'] is None:
                raise ValueError('MANGA 家族缺少明确前景/背景 Look 设置，请重新建立或导入')
            value['look']['background_roles'] = _background_role_names(u.background_roles)
    return _core(value).normalize(value)


def _background_role_names(text):
    if text.strip() == '[]':
        return []
    names = [name.strip() for name in text.split(',')]
    if not names or any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError('背景角色须明确用逗号分隔，不重复；没有背景目标时填写 []')
    return names


def _load_family_document(u, doc):
    """Store the selected schema without ever upgrading a historical v1."""
    u.family_id, u.name, u.line_mode = doc['family_id'], doc['name'], doc['line']['mode']
    u.family_schema = doc['schema']
    u.family_kind = doc['family']
    look = doc.get('look')
    u.look_document = _json(look) if look is not None else ''
    u.background_roles = ','.join(look['background_roles']) or '[]' if look is not None else '[]'


def _remove_new_archive(text, body):
    if text is None:
        return
    if (text.get('toon_cel_author_archive') == 'toon-cel-author-archive.v1'
            and text.as_string() == body and text.users <= int(text.use_fake_user)):
        text.use_fake_user = False
        if text.users == 0:
            bpy.data.texts.remove(text)


def new_current(context):
    return _new_current(context, v2=False)


def new_current_v2(context):
    """Explicit opt-in; the historical new operator continues to make v1."""
    return _new_current(context, v2=True)


def _new_current(context, *, v2):
    """Start explicit new authoring while retaining native shader/ink data."""
    u = settings(context)
    if u.receipt:
        raise ValueError('已有家族应用回执；请先留存家族记录再继续作者')
    c = cel_ui._settings(context)
    if not c.recipe_id:
        raise ValueError('请先建立 CEL 组合配方并明确绑定目标材质槽')
    before = _capture_cel(context)
    family_before = _family_fields(context)
    slots, _ = cel_ui.bindings(context)
    recipe = dict(before['document'], schema='toon-cel-recipe.v2', lines=[])
    line_doc = None
    if u.line_mode == 'TEMPLATE':
        lu = line_template_ui.settings(context)
        if not lu.document:
            raise ValueError('请选择已有完整描边模板，或先保留原描边')
        line_doc = json.loads(lu.document)
    if v2:
        from . import style_family_v2
        kwargs = dict(background_roles=_background_role_names(u.background_roles)) if u.family_kind == 'MANGA' else {}
        doc = style_family_v2.new_document(recipe, u.family_kind, u.name, line_doc, **kwargs)
    else:
        doc = _core().new_document(recipe, u.name, line_doc)
    selection = _selection(context, [row['object'] for row in slots])
    archive = None
    archive_body = None
    try:
        created = cel_ui.fork_composite(context)
        if created.get('archive'):
            archive = bpy.data.texts.get(created['archive'])
            archive_body = archive.as_string() if archive is not None else None
        cel_ui._load_document(c, doc['recipe'])
        c.slots.clear()
        c.line_targets.clear()
        c.line_policy = 'PRESERVE'
        for row in slots:
            obj = row['object']
            context.view_layer.objects.active = obj
            obj.active_material_index = row['slot']
            c.bind_role = row['role']
            cel_ui.bind_slot(context)
        _load_family_document(u, doc)
        u.receipt = ''
        _tokens.pop(context.scene.as_pointer(), None)
        u.message = ('已建立 ' + doc['family'] + ' 家族 v2；请明确绑定全部已用槽并预检') if v2 else '已留存旧配方并建立 CEL 家族；请补全每个目标全部已用槽，再预检'
        return dict(status='created', family_id=u.family_id,
                    archive=created.get('archive'))
    except BaseException:
        _restore_cel(context, before)
        _restore_family(context, family_before)
        _remove_new_archive(archive, archive_body)
        raise
    finally:
        _restore_selection(context, selection)


def _refresh_bindings(context):
    c = cel_ui._settings(context)
    for row in c.slots:
        if row.target is None or not 0 <= row.slot < len(row.target.material_slots):
            raise ValueError('家族应用后的材质槽目标失联')
        slot = row.target.material_slots[row.slot]
        if slot.link != row.link:
            raise ValueError('家族应用改变了材质槽链接方式')
        row.expected_name = cel_ui._material_name(slot.material)
        row.expected_uid = str(slot.material.session_uid) if slot.material else ''
        row.binding_epoch = cel_ui._binding_epoch


def inspect_current(context):
    u = settings(context)
    doc = document(context)
    slots, _ = cel_ui.bindings(context)
    kwargs = _kwargs(context, doc)
    if u.receipt:
        saved = json.loads(u.receipt)
        observation = _core(doc).observe(context, doc, slots, **kwargs)
        if (_json(saved['document']) != _json(doc)
                or _json(saved['observation']) != _json(observation)):
            raise ValueError('家族、绑定或原生作者已有修改；保留修改并停止覆盖')
        u.message = '家族原样核验通过；当前图像的艺术采用范围另行判断'
        return dict(status='unchanged', result=saved['result'])
    result = _core(doc).preflight(context, doc, slots, **kwargs)
    u.message = '家族预检通过；覆盖仅限显式绑定目标，不代表全场景或新角色验收'
    return result


def apply_current(context):
    u = settings(context)
    if u.receipt:
        return inspect_current(context)
    if cel_ui._settings(context).receipt:
        raise ValueError('CEL 配方已被单独应用；请先留存家族记录，再从当前配方明确建立新家族')
    doc = document(context)
    slots, _ = cel_ui.bindings(context)
    kwargs = _kwargs(context, doc)
    cel_before = _capture_cel(context)
    family_before = _family_fields(context)
    line_before = _capture_line(context) if doc['line']['mode'] == 'TEMPLATE' else None
    core = _core(doc)
    token = core.apply(context, doc, slots, **kwargs)
    try:
        _refresh_bindings(context)
        current, lines = cel_ui.bindings(context)
        result = core.receipt(token)
        c = cel_ui._settings(context)
        c.receipt = _json(dict(schema='toon-cel-ui-receipt.v1', result=result['cel'],
            observation=cel_ui._observation(context, doc['recipe'], current, lines)))
        if line_before is not None:
            lu = line_template_ui.settings(context)
            lu.receipt = _json(dict(result=result['line'],
                observation=line_template_ui._observation(context)))
        observation = core.observe(context, doc, current, **kwargs)
        if _json(observation) != _json(token['observation']):
            raise ValueError('UI 提交前家族实际状态已改变')
        u.receipt = _json(dict(document=doc, observation=observation, result=result))
        u.message = ('已应用 ' + doc['family'] + ' 家族 v2；请看完整渲染决定采用') if doc['schema'] == 'toon-style-family.v2' else '已应用 CEL 家族；保留项与显式模板分别记录，需看完整渲染决定采用'
        _tokens[context.scene.as_pointer()] = dict(token=token, cel=cel_before,
            family=family_before, line=line_before)
        return result
    except BaseException as error:
        # Restore UI first: the core observes native readers, not these receipts.
        _restore_cel(context, cel_before)
        if line_before is not None:
            _restore_line(context, line_before)
        _restore_family(context, family_before)
        try:
            core.rollback(context, token)
        except Exception as rollback_error:
            raise RuntimeError('家族 UI 提交失败且原生回退未完成：' + str(rollback_error)) from error
        raise


def restore_current(context):
    u = settings(context)
    saved = _tokens.get(context.scene.as_pointer())
    if saved is None:
        raise ValueError('当前会话没有家族回退记录；保存重开后请使用原工程恢复，不自动重建旧绑定')
    inspect_current(context)
    result = _core(saved['token']['document']).rollback(context, saved['token'])
    _restore_cel(context, saved['cel'])
    if saved['line'] is not None:
        _restore_line(context, saved['line'])
    _restore_family(context, saved['family'])
    _tokens.pop(context.scene.as_pointer(), None)
    cel_ui._settings(context).receipt = ''
    u.receipt = ''
    _refresh_bindings(context)
    u.message = '已回退本次原生家族应用；当前目标仍显式绑定，可重新作者'
    return result


def archive_current(context):
    """Keep historical records before explicitly starting another author pass."""
    u = settings(context)
    if not u.family_id:
        raise ValueError('当前没有家族作者记录')
    doc = document(context)
    value = dict(schema='toon-style-family-author-archive.v2' if doc['schema'] == 'toon-style-family.v2' else 'toon-style-family-author-archive.v1',
                 document=doc, receipt=u.receipt,
                 note='Historical record only; not an automatic native-state or binding restore.')
    text = cel_ui._archive_text('Toon previous family ' + uuid.uuid4().hex, _json(value))
    text['toon_style_family_archive'] = value['schema']
    text.use_fake_user = True
    u.family_id = ''
    u.receipt = ''
    u.family_schema = 'toon-style-family.v1'
    u.look_document = ''
    _tokens.pop(context.scene.as_pointer(), None)
    u.message = '已留存家族记录，原生材质和描边保持；可从当前配方建立新家族'
    return dict(status='archived', archive=text.name)


def sync_sun(context):
    u = settings(context)
    if u.receipt or cel_ui._settings(context).receipt:
        raise ValueError('已应用记录保持不变；请先明确开始新的家族作者')
    sun = u.sun
    if (sun is None or sun.type != 'LIGHT' or sun.data.type != 'SUN'
            or context.scene.objects.get(sun.name) != sun):
        raise ValueError('请选择当前场景中的明确 Sun')
    value = sun.matrix_world.to_3x3() @ Vector((0, 0, 1))
    if not all(math.isfinite(x) for x in value) or value.length == 0:
        raise ValueError('Sun 方向不是有限非零向量')
    direction = value.normalized()
    changed = 0
    for row in cel_ui._settings(context).roles:
        if row.action == 'SURFACE' and row.use_finish:
            row.finish.light_direction = direction
            changed += 1
    if not changed:
        raise ValueError('当前没有显式携带高光与边缘光的 SURFACE 角色')
    u.message = '已给 ' + str(changed) + ' 个材质角色读取 Sun 方向快照；换灯后须显式更新'
    return dict(status='snapshotted', roles=changed, direction=list(direction))


def _path(context, *, existing=False):
    value = settings(context).path
    if not value:
        raise ValueError('请选择明确的家族 JSON 文件路径')
    path = Path(bpy.path.abspath(value))
    if not path.is_absolute() or path.suffix.lower() != '.json':
        raise ValueError('家族文件须为绝对 JSON 路径')
    return path.resolve(strict=existing)


def export_current(context):
    path = _path(context)
    data = _json(document(context))
    with path.open('x', encoding='utf8', newline='\n') as stream:
        stream.write(data)
    settings(context).message = '已导出家族设置；原材质、图片、UV 和 INDEX 数据不随文件运输'
    return dict(status='exported', file=str(path))


def import_current(context):
    u = settings(context)
    c = cel_ui._settings(context)
    if u.family_id or u.receipt:
        raise ValueError('请先留存当前家族记录，再导入新家族')
    if c.receipt or c.slots or c.line_targets or c.roles or c.lines:
        raise ValueError('导入需要空的 CEL 作者区；请先留存已有作者，在新工作副本导入')
    value = json.loads(_path(context, existing=True).read_text('utf-8-sig'))
    doc = _core(value).normalize(value)
    lu = line_template_ui.settings(context)
    if doc['line']['mode'] == 'TEMPLATE' and (lu.document or lu.receipt or lu.bindings or lu.ink_bindings):
        raise ValueError('请先留存并清空当前完整描边模板记录')
    cel_before, family_before = _capture_cel(context), _family_fields(context)
    line_before = _capture_line(context) if doc['line']['mode'] == 'TEMPLATE' else None
    try:
        cel_ui._load_document(c, doc['recipe'])
        c.line_policy = 'PRESERVE'
        if line_before is not None:
            line_template_ui._load(lu, doc['line']['document'])
            lu.mode, lu.conflict = 'REPLACE', 'ERROR'
        _load_family_document(u, doc)
        u.receipt = ''
        _tokens.pop(context.scene.as_pointer(), None)
        u.message = '已导入家族；所有材质槽及完整模板角色都须显式绑定，不按名称推测'
        return dict(status='imported', family_id=u.family_id)
    except BaseException:
        _restore_cel(context, cel_before)
        if line_before is not None:
            _restore_line(context, line_before)
        _restore_family(context, family_before)
        raise


class _Action:
    def execute(self, context):
        try:
            self.action(context)
            return {'FINISHED'}
        except Exception as error:
            settings(context).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONFAMILY_OT_new(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.new'
    bl_label = '从当前配方建立 CEL 家族'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(new_current)


class TOONFAMILY_OT_new_v2(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.new_v2'
    bl_label = '新建三风格家族 v2'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(new_current_v2)


class TOONFAMILY_OT_inspect(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.inspect'
    bl_label = '预检家族覆盖与状态'
    bl_options = {'REGISTER'}
    action = staticmethod(inspect_current)


class TOONFAMILY_OT_apply(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.apply'
    bl_label = '应用 CEL 家族'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(apply_current)


class TOONFAMILY_OT_restore(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.restore'
    bl_label = '回退本会话家族应用'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(restore_current)


class TOONFAMILY_OT_archive(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.archive'
    bl_label = '留存家族记录并继续作者'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(archive_current)


class TOONFAMILY_OT_sync_sun(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.sync_sun'
    bl_label = '读取所选 Sun 的修饰方向'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(sync_sun)


class TOONFAMILY_OT_export(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.export_family'
    bl_label = '导出家族设置'
    bl_options = {'REGISTER'}
    action = staticmethod(export_current)


class TOONFAMILY_OT_import(_Action, bpy.types.Operator):
    bl_idname = 'toon_style_family.import_family'
    bl_label = '导入家族设置'
    bl_options = {'REGISTER', 'UNDO'}
    action = staticmethod(import_current)


def draw_contents(layout, context):
    u = settings(context)
    layout.label(text='风格家族：材质、修饰与完整描边')
    edit = layout.column()
    edit.enabled = not bool(u.receipt)
    edit.prop(u, 'name')
    edit.prop(u, 'line_mode')
    if not u.family_id:
        edit.operator('toon_style_family.new')
        edit.prop(u, 'family_kind')
        if u.family_kind == 'MANGA':
            edit.prop(u, 'background_roles')
        edit.operator('toon_style_family.new_v2')
    else:
        if u.family_schema == 'toon-style-family.v2':
            layout.label(text=u.family_kind + ' 家族 v2')
            if u.family_kind == 'MANGA':
                edit.prop(u, 'background_roles')
                layout.label(text='保留全部原材质；前景与背景分别输出黑白附加图')
        layout.label(text='材质参数与显式槽绑定在 CEL 组合配方区编辑')
        if u.line_mode == 'TEMPLATE':
            layout.label(text='模板角色及线色来源在完整描边模板区绑定')
        else:
            layout.label(text='保留完整原作者与 INDEX；不是便携描边运输')
        edit.prop(u, 'sun')
        edit.operator('toon_style_family.sync_sun')
        row = layout.row(align=True)
        row.operator('toon_style_family.inspect')
        if not u.receipt:
            row.operator('toon_style_family.apply')
        if u.receipt:
            row = layout.row()
            row.enabled = context.scene.as_pointer() in _tokens
            row.operator('toon_style_family.restore')
            if context.scene.as_pointer() not in _tokens:
                layout.label(text='重开后可核验；旧状态请从原工程恢复')
        layout.operator('toon_style_family.archive')
    layout.prop(u, 'path')
    row = layout.row(align=True)
    row.operator('toon_style_family.import_family')
    if u.family_id:
        row.operator('toon_style_family.export_family')
    layout.label(text='覆盖限于显式绑定；风格机制仍需实际渲染验收')
    if u.message:
        layout.label(text=u.message[:140])


@persistent
def _load_post(_dummy):
    _tokens.clear()


CLASSES = (TOONFAMILY_Settings, TOONFAMILY_OT_new, TOONFAMILY_OT_new_v2, TOONFAMILY_OT_inspect,
           TOONFAMILY_OT_apply, TOONFAMILY_OT_restore, TOONFAMILY_OT_archive,
           TOONFAMILY_OT_sync_sun, TOONFAMILY_OT_export, TOONFAMILY_OT_import)


def register():
    global _registered, _descriptor
    if _registered:
        return
    if hasattr(bpy.types.Scene, PROP):
        raise RuntimeError('风格家族 UI 属性已由其它注册实例拥有')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _classes_registered.append(cls)
        setattr(bpy.types.Scene, PROP, PointerProperty(type=TOONFAMILY_Settings))
        _descriptor = bpy.types.Scene.bl_rna.properties[PROP].as_pointer()
        if _load_post not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_load_post)
        _registered = True
    except Exception:
        unregister()
        raise


def unregister():
    global _registered, _descriptor
    prop = bpy.types.Scene.bl_rna.properties.get(PROP)
    if prop is not None and prop.as_pointer() != _descriptor:
        raise RuntimeError('风格家族 UI 属性已被替换；保留其它实例所有权')
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if prop is not None:
        delattr(bpy.types.Scene, PROP)
    _descriptor = None
    _tokens.clear()
    for cls in reversed(_classes_registered):
        bpy.utils.unregister_class(cls)
    _classes_registered.clear()
    _registered = False
