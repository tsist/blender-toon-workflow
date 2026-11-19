# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit static CEL recipe authoring, embedded in the single workbench."""
import json
import uuid
from types import SimpleNamespace

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    FloatVectorProperty, IntProperty, PointerProperty, StringProperty)

from . import surface_ui, surface_slots as surface

PROP = 'toon_cel_recipe_ui'
_registered = False
_classes_registered = []
_descriptor = None
_binding_epoch = uuid.uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _settings(context):
    return getattr(context.scene, PROP)


def _material_name(material):
    return material.name_full if material else ''


def _core(schema):
    if schema == 'toon-cel-recipe.v1':
        from . import cel_recipe
        return cel_recipe
    if schema == 'toon-cel-recipe.v2':
        from . import cel_recipe_v2
        return cel_recipe_v2
    raise ValueError('不支持的 CEL 配方格式')


class TOONCEL_Finish(bpy.types.PropertyGroup):
    highlight_strength: FloatProperty(name='高光强度', default=0, min=0, max=2)
    highlight_color: FloatVectorProperty(name='高光颜色', subtype='COLOR', size=4,
        default=(1,.9,.75,1), min=0, max=1)
    highlight_threshold: FloatProperty(name='高光阈值', default=.85, min=0, max=1)
    highlight_softness: FloatProperty(name='高光过渡', default=.05, min=.001, max=1)
    rim_strength: FloatProperty(name='边缘光强度', default=0, min=0, max=2)
    rim_color: FloatVectorProperty(name='边缘光颜色', subtype='COLOR', size=4,
        default=(.65,.75,1,1), min=0, max=1)
    rim_power: FloatProperty(name='边缘光指数', default=5, min=.1, max=64)
    rim_lift: FloatProperty(name='边缘光偏移', default=0, min=-1, max=1)
    light_direction: FloatVectorProperty(name='世界光向快照', size=3, default=(0,0,1))


class TOONCEL_Role(bpy.types.PropertyGroup):
    role: StringProperty(name='材质角色')
    action: EnumProperty(name='处理', items=[('PRESERVE', '保留原材质', ''),
        ('SURFACE', 'CEL 表面', '')], default='PRESERVE')
    surface: PointerProperty(type=surface_ui.TOONSURFACE_Settings)
    use_boundary: BoolProperty(name='携带明暗边界羽化', default=False)
    boundary_width_low: FloatProperty(name='低边界完整照度区间宽度',
        description='完整照度区间宽度，0 为硬边；不是像素宽度', default=0, min=0, max=1)
    boundary_width_high: FloatProperty(name='高边界完整照度区间宽度',
        description='完整照度区间宽度，0 为硬边；不是像素宽度', default=0, min=0, max=1)
    use_finish: BoolProperty(name='携带高光与边缘光', default=False)
    finish: PointerProperty(type=TOONCEL_Finish)


class TOONCEL_Slot(bpy.types.PropertyGroup):
    role: StringProperty(name='材质角色')
    target: PointerProperty(name='对象', type=bpy.types.Object)
    slot: IntProperty(name='槽', min=0)
    link: StringProperty()
    expected_name: StringProperty()
    expected_uid: StringProperty(options={'HIDDEN'})
    binding_epoch: StringProperty(options={'HIDDEN'})


class TOONCEL_Line(bpy.types.PropertyGroup):
    role: StringProperty(name='描边角色')
    width: FloatProperty(name='像素宽', default=1.1, min=.1, max=32)
    color: FloatVectorProperty(name='线色', subtype='COLOR', size=4,
        default=(.025, .02, .035, 1), min=0, max=1)


class TOONCEL_LineTarget(bpy.types.PropertyGroup):
    role: StringProperty(name='描边角色')
    target: PointerProperty(name='对象', type=bpy.types.Object)


class TOONCEL_Settings(bpy.types.PropertyGroup):
    recipe_schema: StringProperty(default='toon-cel-recipe.v1', options={'HIDDEN'})
    recipe_id: StringProperty()
    recipe_name: StringProperty(name='配方名', default='CEL recipe')
    revision: IntProperty(name='版本', min=1, default=1)
    roles: CollectionProperty(type=TOONCEL_Role)
    slots: CollectionProperty(type=TOONCEL_Slot)
    lines: CollectionProperty(type=TOONCEL_Line)
    line_targets: CollectionProperty(type=TOONCEL_LineTarget)
    line_policy: EnumProperty(name='描边处理', items=[
        ('CREATE_FROM_BASIC','从基础描边初始化','仅用于尚无复杂描边作者的新输入'),
        ('PRESERVE','保留现有描边','完整保留现有作者；配方的描边角色不应用')],
        default='CREATE_FROM_BASIC')
    bind_role: StringProperty(name='材质角色', default='body')
    line_role: StringProperty(name='描边角色', default='character')
    file_path: StringProperty(name='CEL 配方 JSON', subtype='FILE_PATH')
    receipt: StringProperty(options={'HIDDEN'})
    message: StringProperty()


def document(context):
    settings = _settings(context)
    roles = {}
    for row in settings.roles:
        if row.role in roles:
            raise ValueError('材质角色重复：' + row.role)
        value = dict(action=row.action)
        if row.use_boundary and settings.recipe_schema != 'toon-cel-recipe.v2':
            raise ValueError('明暗边界羽化仅支持 v2 组合配方')
        if row.action == 'SURFACE':
            s = row.surface
            value['settings'] = {key: s.mode if key == 'mode' else
                list(getattr(s, key)) if key.endswith('_color') else getattr(s, key)
                for key in surface.materials._fields(s.mode)}
            if row.use_boundary:
                value['boundary'] = dict(width_low=row.boundary_width_low,
                                         width_high=row.boundary_width_high)
            if settings.recipe_schema == 'toon-cel-recipe.v2' and row.use_finish:
                from . import finish_nodes
                value['finish'] = {key: list(getattr(row.finish,key))
                    if isinstance(default,list) else getattr(row.finish,key)
                    for key,default in finish_nodes.defaults().items()}
        elif row.use_finish or row.use_boundary:
            raise ValueError('保留原材质的角色不能附加高光或明暗羽化；请保留其原作者')
        roles[row.role] = value
    return _core(settings.recipe_schema).normalize(dict(schema=settings.recipe_schema, recipe_id=settings.recipe_id,
        name=settings.recipe_name, revision=settings.revision, roles=roles,
        lines=[dict(role=r.role, width=r.width, color=list(r.color)) for r in settings.lines]))


def _load_document(settings, doc):
    doc = _core(doc['schema']).normalize(doc)
    settings.recipe_schema = doc['schema']
    settings.recipe_id, settings.recipe_name, settings.revision = doc['recipe_id'], doc['name'], doc['revision']
    settings.roles.clear()
    settings.lines.clear()
    for name, value in doc['roles'].items():
        row = settings.roles.add()
        row.role, row.action = name, value['action']
        if row.action == 'SURFACE':
            surface_ui._load(row.surface, value['settings'])
            row.use_boundary = 'boundary' in value
            if row.use_boundary:
                if doc['schema'] != 'toon-cel-recipe.v2':
                    raise ValueError('明暗边界羽化仅支持 v2 组合配方')
                row.boundary_width_low = value['boundary']['width_low']
                row.boundary_width_high = value['boundary']['width_high']
            row.use_finish = 'finish' in value
            if row.use_finish:
                for key,item in value['finish'].items():
                    setattr(row.finish,key,item)
    for value in doc['lines']:
        row = settings.lines.add()
        row.role, row.width, row.color = value['role'], value['width'], value['color']


def new_current(context, *, composite=False):
    settings = _settings(context)
    if settings.receipt:
        raise ValueError('当前配方已有应用回执；在新工作副本创建新配方')
    if settings.roles or settings.slots or settings.lines or settings.line_targets:
        raise ValueError('当前配方已有作者内容；请保留或导出后使用新工作副本')
    schema = 'toon-cel-recipe.v2' if composite else 'toon-cel-recipe.v1'
    _load_document(settings, _core(schema).new_document())
    settings.message = '已建立静态 CEL 配方；请显式绑定材质槽与描边对象'


def _archive_text(name, encoded):
    """Load large JSON records through Blender's native reader.

    UI receipts contain escaped preset strings, sometimes several megabytes
    on one line. Text.write inserts these character by character and stalls.
    Internal loading keeps the complete record without a live file dependency.
    """
    import os
    import tempfile
    from pathlib import Path
    descriptor, filename = tempfile.mkstemp(prefix='toon-author-record-', suffix='.json')
    text = None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8', newline='\n') as stream:
            stream.write(encoded)
        text = bpy.data.texts.load(filename, internal=True)
        text.name = name
        if json.loads(text.as_string()) != json.loads(encoded):
            raise ValueError('Historical author record readback changed')
        return text
    except BaseException:
        if text is not None:
            bpy.data.texts.remove(text)
        raise
    finally:
        Path(filename).unlink(missing_ok=True)


def fork_composite(context):
    """Keep the previous author record before replacing only the active UI recipe."""
    settings=_settings(context)
    if not settings.recipe_id:
        return new_current(context,composite=True)
    old_document=document(context)
    slots=[{key:getattr(row,key) for key in
        ('role','target','slot','link','expected_name','expected_uid','binding_epoch')}
        for row in settings.slots]
    lines=[dict(role=row.role,target=row.target) for row in settings.line_targets]
    fields={key:getattr(settings,key) for key in
        ('receipt','line_policy','bind_role','line_role','file_path','message')}
    def ref(obj):
        return None if obj is None else dict(name=obj.name_full,
            library=obj.library.filepath if obj.library else None)
    archive=dict(schema='toon-cel-author-archive.v1',document=old_document,
        receipt=fields['receipt'],line_policy=fields['line_policy'],
        slots=[dict(row,target=ref(row['target'])) for row in slots],
        line_targets=[dict(row,target=ref(row['target'])) for row in lines],
        note='Historical UI author record; receipt may predate later native edits. Not a portable binding or automatic restore file.')
    encoded=_json(archive)
    new_doc=_core('toon-cel-recipe.v2').new_document()
    text=None
    try:
        text=_archive_text('CEL previous recipe '+uuid.uuid4().hex,encoded)
        text.use_fake_user=True
        text['toon_cel_author_archive']='toon-cel-author-archive.v1'
        _load_document(settings,new_doc)
        settings.slots.clear();settings.line_targets.clear();settings.receipt=''
        settings.line_policy='CREATE_FROM_BASIC';settings.bind_role='body';settings.line_role='character'
        settings.file_path=''
        settings.message='已保留旧配方记录：'+text.name+'；请显式绑定并选择描边处理'
        return dict(status='created',archive=text.name)
    except BaseException:
        _load_document(settings,old_document)
        settings.slots.clear();settings.line_targets.clear()
        for values in slots:
            row=settings.slots.add()
            for key,value in values.items():setattr(row,key,value)
        for values in lines:
            row=settings.line_targets.add();row.role=values['role'];row.target=values['target']
        for key,value in fields.items():setattr(settings,key,value)
        if text is not None:bpy.data.texts.remove(text)
        raise


def bind_slot(context):
    settings = _settings(context)
    if settings.receipt:
        raise ValueError('配方已应用；保持其绑定身份')
    if settings.bind_role not in {r.role for r in settings.roles}:
        raise ValueError('先填写配方中已有的材质角色')
    obj, index, slot = surface._context(context)
    if any(r.target == obj and r.slot == index for r in settings.slots):
        raise ValueError('这个材质槽已经绑定')
    row = settings.slots.add()
    row.role, row.target, row.slot, row.link = settings.bind_role, obj, index, slot.link
    row.expected_name = _material_name(slot.material)
    row.expected_uid = str(slot.material.session_uid) if slot.material else ''
    row.binding_epoch = _binding_epoch
    settings.message = '已绑定 ' + obj.name + ' / slot ' + str(index)


def bind_line(context):
    settings = _settings(context)
    if settings.receipt:
        raise ValueError('配方已应用；保持其绑定身份')
    if settings.line_role not in {r.role for r in settings.lines}:
        raise ValueError('先填写配方中已有的描边角色')
    obj = context.active_object
    if obj is None or obj.type != 'MESH' or obj.name not in context.scene.objects:
        raise ValueError('选择场景中的明确网格对象')
    if any(r.target == obj for r in settings.line_targets):
        raise ValueError('这个对象已经绑定描边角色')
    row = settings.line_targets.add()
    row.role, row.target = settings.line_role, obj
    settings.message = '已绑定描边：' + obj.name


def bindings(context):
    settings = _settings(context)
    slots = []
    for row in settings.slots:
        obj = row.target
        if obj is None or row.slot >= len(obj.material_slots):
            raise ValueError('材质槽目标失联')
        slot = obj.material_slots[row.slot]
        if not settings.receipt:
            if row.binding_epoch != _binding_epoch:
                raise ValueError('未应用的绑定来自另一会话；请移除该绑定并明确重新绑定活动槽')
            current_uid = str(slot.material.session_uid) if slot.material else ''
            if row.expected_uid != current_uid:
                raise ValueError('绑定材质身份已改变；保留作者替换并重新绑定')
        if slot.link != row.link or _material_name(slot.material) != row.expected_name:
            raise ValueError('材质槽链接或预期材质改变；保留作者修改')
        slots.append(dict(role=row.role, object=obj, slot=row.slot, link=row.link, material=slot.material))
    lines = {}
    for row in settings.line_targets:
        if row.target is None:
            raise ValueError('描边对象失联')
        lines.setdefault(row.role, []).append(row.target)
    return slots, lines


def _pencil_state(scene):
    """Actual RNA author state, independent of a stale cached document."""
    author_fields = {
        'enabled','display_mode','line_width','line_color','opacity','width_multiplier',
        'style_mode','preset_text','preset_revision','groups','basic_line_set_id',
        'basic_elements_only','elements','chains','chain_controls_json','map_amounts',
        'map_amounts_json','dashes','dash_patterns_json','amount_tracks','parameter_tracks',
        'curve_tree','curve_size_curve_enabled','curve_alpha_curve_enabled','curve_revision',
        'silhouette','open_edges','marked_edges','wireframe_edges','material_edges',
        'crease_edges','crease_angle','visibility','intersection_enabled',
    }
    def capture(value):
        result = {}
        for prop in value.bl_rna.properties:
            key = prop.identifier
            if key == 'rna_type' or (value == settings and key not in author_fields):
                continue
            item = getattr(value, key)
            if prop.type == 'COLLECTION':
                result[key] = [capture(v) for v in item]
            elif prop.type == 'POINTER':
                result[key] = None if item is None else [item.bl_rna.identifier,
                    item.name_full, item.library.filepath if item.library else None]
            else:
                result[key] = list(item) if getattr(prop, 'is_array', False) else item
        return result
    settings = scene.toon_viewport_preview
    result = capture(settings)
    result['preset_bytes'] = settings.preset_text.as_string() if settings.preset_text else None
    return result


def _observation(context, doc, slots, lines):
    states = []
    for row in slots:
        obj = row['object']
        old_index = obj.active_material_index
        try:
            obj.active_material_index = row['slot']
            material = row['material']
            state = surface.inspect(SimpleNamespace(scene=context.scene, active_object=obj, mode='OBJECT')) \
                if material is not None and material.get(surface._PROFILE) == surface.PROFILE else None
            if state is not None and state['status'] != 'valid':
                raise ValueError('; '.join(state['issues']))
            item=dict(role=row['role'], object=obj.name_full, slot=row['slot'],
                link=row['link'], material=_material_name(material), surface=state)
            if doc['schema']=='toon-cel-recipe.v2':
                from . import finish_core
                item['finish']=finish_core.inspect(SimpleNamespace(scene=context.scene,
                    active_object=obj,mode='OBJECT')) if material is not None and finish_core.KEY in material else None
            states.append(item)
        finally:
            obj.active_material_index = old_index
    result=dict(document=doc, slots=states, lines={k:[o.name_full for o in v] for k,v in lines.items()},
        pencil=_pencil_state(context.scene))
    if doc['schema']=='toon-cel-recipe.v2':result['line_policy']=_settings(context).line_policy
    from . import line_material_colors
    from .backend import pencil
    if context.scene.toon_viewport_preview.style_mode=='PRESET':
        ink=line_material_colors.resolve(context.scene,pencil('addon').effective_preset(context.scene))
        if ink is not None:result['material_line_colors']=ink
    return result


def _call_core(operation, context, doc, slots, lines):
    kwargs={'line_policy':_settings(context).line_policy} if doc['schema']=='toon-cel-recipe.v2' else {}
    return getattr(_core(doc['schema']),operation)(context,doc,slots,lines,**kwargs)


def inspect_current(context):
    settings = _settings(context)
    doc = document(context)
    slots, lines = bindings(context)
    if settings.receipt:
        expected = json.loads(settings.receipt)
        if _json(_observation(context, doc, slots, lines)) != _json(expected['observation']):
            raise ValueError('配方、绑定或原生作者内容已改变；保留修改并停止覆盖')
        result = dict(status='unchanged', receipt=expected['result'])
    else:
        result = _call_core('inspect', context, doc, slots, lines)
    settings.message = result.get('status', 'checked')
    return result


def apply_current(context):
    settings = _settings(context)
    if settings.receipt:
        return inspect_current(context)
    doc = document(context)
    slots, lines = bindings(context)
    # Validate the observation encoder before mutation; no new ID references
    # are stored in this receipt. The domain core owns transaction rollback.
    _observation(context, doc, slots, lines)
    # v2 already exposes a transaction for the family workbench. Keep that
    # native commit recoverable until this standalone panel commits its receipt.
    core = _core(doc['schema'])
    previous = [(row, row.expected_name, row.expected_uid, row.binding_epoch)
                for row in settings.slots]
    previous_receipt, previous_message = settings.receipt, settings.message
    token = None
    captured = []
    try:
        if doc['schema']=='toon-cel-recipe.v2':
            result, token = core.apply_transaction(context, doc, slots, lines,
                line_policy=settings.line_policy,
                capture=lambda result, value: captured.append(value))
        else:
            result = _call_core('apply', context, doc, slots, lines)
        for row in settings.slots:
            material = row.target.material_slots[row.slot].material
            row.expected_name = _material_name(material)
            row.expected_uid = str(material.session_uid) if material else ''
            row.binding_epoch = _binding_epoch
        slots, lines = bindings(context)
        settings.receipt = _json(dict(schema='toon-cel-ui-receipt.v1', result=result,
            observation=_observation(context, doc, slots, lines)))
        settings.message = '已应用静态 CEL 配方；重复应用只检查身份与原生作者内容'
        return result
    except BaseException as error:
        for row, name, uid, epoch in previous:
            row.expected_name, row.expected_uid, row.binding_epoch = name, uid, epoch
        settings.receipt, settings.message = previous_receipt, previous_message
        if token is None and captured:
            token = captured[0]
        if token is not None and token['status']=='applied':
            try:
                core.rollback_transaction(context, token)
            except Exception as rollback_error:
                raise RuntimeError('CEL 面板提交失败且原生回退不完整：'+str(rollback_error)) from error
        raise


def import_current(context):
    from pathlib import Path
    settings = _settings(context)
    if settings.receipt or settings.slots or settings.line_targets:
        raise ValueError('请在尚未绑定的新配方中导入，再显式绑定真实对象')
    path=Path(bpy.path.abspath(settings.file_path))
    if path.stat().st_size>1024*1024:raise ValueError('CEL 配方文件过大')
    schema=json.loads(path.read_text(encoding='utf-8-sig')).get('schema')
    doc = _core(schema).load_document(str(path))
    _load_document(settings, doc)
    settings.message = '已导入配方；文件不携带场景对象绑定'


def export_current(context):
    doc=document(context)
    result = _core(doc['schema']).export_document(doc, bpy.path.abspath(_settings(context).file_path))
    _settings(context).message = '已导出静态 CEL 配方'
    return result


class _Action:
    def execute(self, context):
        try:
            self.action(context)
            return {'FINISHED'}
        except Exception as error:
            _settings(context).message = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONCEL_OT_new(_Action, bpy.types.Operator):
    bl_idname='toon_cel.new'; bl_label='新建 CEL 配方'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(new_current)
class TOONCEL_OT_new_composite(_Action, bpy.types.Operator):
    bl_idname='toon_cel.new_composite'; bl_label='新建 CEL 组合配方'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(lambda context:new_current(context,composite=True))
class TOONCEL_OT_fork_composite(_Action, bpy.types.Operator):
    bl_idname='toon_cel.fork_composite'; bl_label='保留当前配方并新建组合配方'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(fork_composite)
class TOONCEL_OT_bind_slot(_Action, bpy.types.Operator):
    bl_idname='toon_cel.bind_slot'; bl_label='绑定活动材质槽'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(bind_slot)
class TOONCEL_OT_bind_line(_Action, bpy.types.Operator):
    bl_idname='toon_cel.bind_line'; bl_label='绑定活动对象描边'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(bind_line)
class TOONCEL_OT_inspect(_Action, bpy.types.Operator):
    bl_idname='toon_cel.inspect'; bl_label='预检配方'; bl_options={'REGISTER'}
    action=staticmethod(inspect_current)
class TOONCEL_OT_apply(_Action, bpy.types.Operator):
    bl_idname='toon_cel.apply'; bl_label='应用 CEL 配方'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(apply_current)
class TOONCEL_OT_import(_Action, bpy.types.Operator):
    bl_idname='toon_cel.import_recipe'; bl_label='导入 CEL 配方'; bl_options={'REGISTER','UNDO'}
    action=staticmethod(import_current)
class TOONCEL_OT_export(_Action, bpy.types.Operator):
    bl_idname='toon_cel.export_recipe'; bl_label='导出 CEL 配方'; bl_options={'REGISTER'}
    action=staticmethod(export_current)
class TOONCEL_OT_remove(bpy.types.Operator):
    bl_idname='toon_cel.remove_binding'; bl_label='移除未应用绑定'; bl_options={'REGISTER','UNDO'}
    kind: EnumProperty(items=[('slots','材质槽',''),('line_targets','描边对象','')])
    index: IntProperty(min=0)
    def execute(self,context):
        settings=_settings(context)
        values=getattr(settings,self.kind)
        if settings.receipt or self.index>=len(values):
            return {'CANCELLED'}
        values.remove(self.index)
        return {'FINISHED'}


def draw_contents(layout, context):
    settings=_settings(context)
    if not settings.recipe_id:
        layout.operator('toon_cel.new')
        layout.operator('toon_cel.new_composite')
        layout.prop(settings,'file_path')
        layout.operator('toon_cel.import_recipe')
        return
    layout.prop(settings,'recipe_name'); layout.prop(settings,'revision')
    layout.operator('toon_cel.fork_composite')
    layout.label(text='显式槽与描边角色；面部 SDF / 头部法向保留')
    for role in settings.roles:
        box=layout.box()
        box.label(text=role.role); box.prop(role,'action')
        if role.action=='SURFACE':
            for key in ('base_color','shadow_color','mid_color','lit_color','threshold_low','threshold_high'):
                box.prop(role.surface,key)
            if role.surface.mode=='eevee_cel3_reflect':
                for key in ('reflection_color','reflection_strength','reflection_roughness'):
                    box.prop(role.surface,key)
            if settings.recipe_schema=='toon-cel-recipe.v2':
                box.prop(role,'use_boundary')
                if role.use_boundary:
                    box.prop(role,'boundary_width_low')
                    box.prop(role,'boundary_width_high')
                    box.label(text='完整照度区间宽度；0 为硬边，不是像素')
                box.prop(role,'use_finish')
                if role.use_finish:
                    from . import finish_nodes
                    for key in finish_nodes.defaults():box.prop(role.finish,key)
                    box.label(text='光向为静态快照；换灯后须显式更新')
    layout.prop(settings,'bind_role'); layout.operator('toon_cel.bind_slot')
    for i,row in enumerate(settings.slots):
        r=layout.row(align=True)
        r.label(text=row.role+' / '+(row.target.name if row.target else '(失联)')+' / '+str(row.slot))
        if not settings.receipt:
            op=r.operator('toon_cel.remove_binding',text='',icon='X'); op.kind='slots'; op.index=i
    if settings.recipe_schema=='toon-cel-recipe.v2':
        layout.prop(settings,'line_policy')
        if settings.line_policy=='PRESERVE':layout.label(text='保留全部现有描边；下列配方线角色不应用')
    for row in settings.lines:
        box=layout.box(); box.label(text=row.role)
        box.prop(row,'width'); box.prop(row,'color')
    layout.prop(settings,'line_role'); layout.operator('toon_cel.bind_line')
    for i,row in enumerate(settings.line_targets):
        r=layout.row(align=True)
        r.label(text=row.role+' / '+(row.target.name if row.target else '(失联)'))
        if not settings.receipt:
            op=r.operator('toon_cel.remove_binding',text='',icon='X'); op.kind='line_targets'; op.index=i
    row=layout.row(align=True); row.operator('toon_cel.inspect'); row.operator('toon_cel.apply')
    layout.label(text='静态配方；未绑定角色不应用，已有作者修改保留冲突')
    layout.prop(settings,'file_path')
    row=layout.row(align=True); row.operator('toon_cel.import_recipe'); row.operator('toon_cel.export_recipe')
    if settings.message: layout.label(text=settings.message[:120])


CLASSES=(TOONCEL_Finish,TOONCEL_Role,TOONCEL_Slot,TOONCEL_Line,TOONCEL_LineTarget,TOONCEL_Settings,
    TOONCEL_OT_new,TOONCEL_OT_new_composite,TOONCEL_OT_fork_composite,TOONCEL_OT_bind_slot,TOONCEL_OT_bind_line,TOONCEL_OT_inspect,
    TOONCEL_OT_apply,TOONCEL_OT_import,TOONCEL_OT_export,TOONCEL_OT_remove)


def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('CEL UI Scene property belongs to another registration')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls); _classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONCEL_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer(); _registered=True
    except Exception:
        unregister(); raise


def unregister():
    global _registered,_descriptor
    descriptor=bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer()!=_descriptor:
        raise RuntimeError('CEL UI property was replaced; preserve its foreign owner')
    if descriptor is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear(); _registered=False
