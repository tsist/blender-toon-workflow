# SPDX-License-Identifier: GPL-3.0-or-later
"""Transient ID picker; only accepting a current dialog writes the author."""
import json
import uuid

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import selector_bindings as B
from .selector_capture import reference

PROP = 'toon_viewport_selector_picker'
LABELS = dict(objects='对象', collections='集合', materials='材质')


class SelectorChoice(bpy.types.PropertyGroup):
    identity: StringProperty()
    library: StringProperty()
    session_uid: StringProperty()
    selected: BoolProperty(default=False)
    missing: BoolProperty(default=False)


class SelectorPickerState(bpy.types.PropertyGroup):
    token: StringProperty()
    scene: PointerProperty(type=bpy.types.Scene)
    group: StringProperty()
    kind: StringProperty()
    text_pointer: StringProperty()
    baseline: StringProperty()
    revision: IntProperty()
    query: StringProperty(name='筛选', options={'TEXTEDIT_UPDATE'})
    choices: CollectionProperty(type=SelectorChoice)
    index: IntProperty(min=0)


def state(context):
    return getattr(context.window_manager, PROP)


def clear(context, token=None):
    # Blender can deliver a modal cancel callback after add-on unregistration.
    staged = getattr(context.window_manager, PROP, None)
    if staged is None:
        return
    # A stale dialog must never clear a newer dialog's selection.
    if token is not None and staged.token != token:
        return
    staged.token = ''
    staged.scene = None
    staged.baseline = ''
    staged.text_pointer = ''
    staged.group = staged.kind = staged.query = ''
    staged.choices.clear()
    staged.index = 0


def clear_all():
    for wm in bpy.data.window_managers:
        if hasattr(wm, PROP):
            staged = getattr(wm, PROP)
            staged.token = ''
            staged.scene = None
            staged.baseline = staged.text_pointer = ''
            staged.choices.clear()


def begin(context, gid, kind):
    from .selector_ui import _seed
    from .wire_ui import _require
    if kind not in B.KINDS:
        raise ValueError('未知绑定类型')
    settings = _require(context.scene)
    staged = state(context)
    if staged.token:
        raise ValueError('请先确认或取消已打开的绑定选择器')
    seed = _seed(context.scene, gid)
    selected = {B.key(ref): ref for ref in seed[kind]}
    catalog = {B.key(reference(block)): block for block in getattr(bpy.data, kind)}
    token = uuid.uuid4().hex
    try:
        clear(context)
        staged.scene, staged.group, staged.kind = context.scene, gid, kind
        staged.text_pointer = str(settings.preset_text.as_pointer())
        staged.baseline = settings.preset_text.as_string()
        staged.revision = settings.preset_revision
        for identity in sorted(set(catalog) | set(selected),
                               key=lambda k: (json.loads(k)['name'].casefold(), k)):
            ref = json.loads(identity)
            block = catalog.get(identity)
            row = staged.choices.add()
            row.name, row.library, row.identity = ref['name'], ref['library'] or '', identity
            row.selected, row.missing = identity in selected, block is None
            row.session_uid = '' if block is None else str(block.session_uid)
        staged.token = token
    except Exception:
        clear(context)
        raise
    return token


def require_current(context, token):
    from .wire_ui import _require
    staged = state(context)
    if not token or staged.token != token or staged.scene != context.scene:
        raise ValueError('选择器已过期或当前场景已改变，请重新打开')
    settings = _require(context.scene)
    if (str(settings.preset_text.as_pointer()) != staged.text_pointer
            or settings.preset_revision != staged.revision
            or settings.preset_text.as_string() != staged.baseline):
        raise ValueError('样式已更新，请重新打开绑定选择器')
    return staged


def shown(row, query):
    query = query.strip().casefold()
    return not query or query in (row.name + ' · ' + (row.library or '本地')).casefold()


def select(context, token, action):
    staged = require_current(context, token)
    if action not in {'ALL', 'NONE', 'INVERT'}:
        raise ValueError('未知批量选择操作')
    for row in staged.choices:
        visible = shown(row, staged.query)
        row.selected = (visible and (action == 'ALL' or action == 'INVERT' and not row.selected))


def accept(context, token):
    from .selector_ui import _seed, set_selectors
    staged = require_current(context, token)
    # Resolve captured ID identities again, including the runtime identity. A
    # replacement ID with the same name/library is not the user's chosen ID.
    catalog = {B.key(reference(block)): block for block in getattr(bpy.data, staged.kind)}
    selected = []
    for row in staged.choices:
        if not row.selected:
            continue
        block = catalog.get(row.identity)
        if row.missing or block is None or str(block.session_uid) != row.session_uid:
            raise ValueError('选中项目已删除、改名或替换，请重新打开选择器：' + row.name)
        selected.append(reference(block))
    if len(selected) > 4096:
        raise ValueError('每类绑定最多支持 4096 项，请缩小选择范围')
    seed = _seed(context.scene, staged.group)
    seed[staged.kind], seed['enabled'] = selected, True
    report = set_selectors(context.scene, [seed])
    clear(context, token)
    return report


class TOONVIEWPORT_UL_selector_choices(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'selected', text='')
        row.label(text=item.name, icon='ERROR' if item.missing else 'NONE')
        row.label(text=(item.library or '本地') + (' · 已失效' if item.missing else ''))
        op = row.operator('toon_viewport.selector_identity', text='', icon='INFO')
        op.identity = item.identity

    def filter_items(self, context, data, propname):
        return [self.bitflag_filter_item if shown(row, data.query) else 0
                for row in getattr(data, propname)], []


class TOONVIEWPORT_OT_selector_select(bpy.types.Operator):
    bl_idname = 'toon_viewport.selector_select'
    bl_label = '批量选择绑定项'
    token: StringProperty(options={'HIDDEN'})
    action: EnumProperty(items=[('ALL', '全选', ''), ('NONE', '全不选', ''), ('INVERT', '反选', '')])

    def execute(self, context):
        try:
            select(context, self.token, self.action)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONVIEWPORT_OT_selector_identity(bpy.types.Operator):
    bl_idname = 'toon_viewport.selector_identity'
    bl_label = '查看完整绑定身份'
    identity: StringProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        try:
            ref = json.loads(properties.identity)
            return ref['name'] + ' · ' + (ref['library'] or '本地')
        except Exception:
            return '查看完整绑定身份'

    def execute(self, context):
        self.report({'INFO'}, self.description(context, self.properties))
        return {'FINISHED'}


class TOONVIEWPORT_OT_selector_picker(bpy.types.Operator):
    bl_idname = 'toon_viewport.selector_picker'
    bl_label = '批量编辑绑定'
    bl_options = {'UNDO'}
    group: StringProperty(options={'HIDDEN'})
    kind: EnumProperty(items=[(k, v, '') for k, v in LABELS.items()])
    token: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        try:
            self.token = begin(context, self.group, self.kind)
            return context.window_manager.invoke_props_dialog(self, width=640)
        except Exception as error:
            clear(context, self.token)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def draw(self, context):
        staged = state(context)
        if staged.token != self.token:
            self.layout.label(text='选择器已过期，请取消后重新打开', icon='ERROR')
            return
        self.layout.label(text=LABELS[staged.kind] + '绑定；确认后替换本类选择')
        self.layout.label(text='列出全文件数据块；集合包含其下级对象和实例')
        self.layout.prop(staged, 'query', text='', icon='VIEWZOOM')
        row = self.layout.row(align=True)
        for action, label in [('ALL', '全选'), ('NONE', '全不选'), ('INVERT', '反选')]:
            op = row.operator('toon_viewport.selector_select', text=label)
            op.token, op.action = self.token, action
        self.layout.label(text='全选与反选仅保留筛选后显示项；全不选清空全部项目')
        self.layout.template_list('TOONVIEWPORT_UL_selector_choices', '', staged, 'choices', staged, 'index', rows=10)
        self.layout.label(text=f'已选 {sum(row.selected for row in staged.choices)} / {len(staged.choices)}')

    def execute(self, context):
        try:
            accept(context, self.token)
            return {'FINISHED'}
        except Exception as error:
            clear(context, self.token)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def cancel(self, context):
        clear(context, self.token)


CLASSES = (SelectorChoice, SelectorPickerState, TOONVIEWPORT_UL_selector_choices,
           TOONVIEWPORT_OT_selector_select, TOONVIEWPORT_OT_selector_identity,
           TOONVIEWPORT_OT_selector_picker)


def register():
    if hasattr(bpy.types.WindowManager, PROP):
        raise RuntimeError('Selector picker property already registered')
    setattr(bpy.types.WindowManager, PROP, PointerProperty(type=SelectorPickerState, options={'SKIP_SAVE'}))


def unregister():
    clear_all()
    if hasattr(bpy.types.WindowManager, PROP):
        delattr(bpy.types.WindowManager, PROP)
