# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit source author controls; use the shared guarded author transaction."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from . import edge_sources, edge_authoring, wire_ui

LABELS = dict(outline='轮廓', object='对象内线', smooth='平滑边界', material='材质边界', selected='标记边',
              normal_angle='法线夹角', wireframe='原始边线框')


def set_sources(scene, updates):
    return wire_ui.apply_migration(scene, updates, edge_authoring.migrate)


class TOONVIEWPORT_OT_edge_profile(bpy.types.Operator):
    bl_idname = 'toon_viewport.edge_profile'
    bl_label = '切换统一边界来源'
    bl_options = {'REGISTER', 'UNDO'}
    group: StringProperty(options={'HIDDEN'})
    enabled: BoolProperty(default=True)

    def execute(self, context):
        try:
            set_sources(context.scene, [dict(group=self.group, enabled=self.enabled, routes=[])])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONVIEWPORT_OT_edge_route(bpy.types.Operator):
    bl_idname = 'toon_viewport.edge_route'
    bl_label = '编辑边界来源'
    bl_options = {'REGISTER', 'UNDO'}
    group: StringProperty(options={'HIDDEN'})
    category: EnumProperty(items=[(c, LABELS[c], '') for c in edge_sources.CATEGORIES])
    channel: EnumProperty(items=[('VISIBLE', '可见线', ''), ('HIDDEN', '隐藏线', '')])
    enabled: BoolProperty(name='启用此来源', default=True)
    mode: EnumProperty(name='笔刷', items=[('DEFAULT', '通道默认笔刷', ''),
        ('SPECIFIC', '专用笔刷', '原配置需激活的直接连接；自主组首次使用时建立独立副本')])
    include_open: BoolProperty(name='包含开放边', default=True)
    min_angle: FloatProperty(name='最小角度（度）', min=0, max=180, default=30)
    max_angle: FloatProperty(name='最大角度（度）', min=0, max=180, default=180)

    def invoke(self, context, event):
        try:
            from . import addon
            record = edge_sources.records(addon._preset(context.scene)['raw'])[self.group]
            route = next(r for r in record['routes'] if r['category'] == self.category and r['channel'] == self.channel)
            for name in ('enabled', 'mode', 'include_open', 'min_angle', 'max_angle'):
                setattr(self, name, route[name])
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        layout.label(text=LABELS[self.category]+' · '+('可见线' if self.channel == 'VISIBLE' else '隐藏线'))
        layout.prop(self, 'enabled')
        layout.prop(self, 'mode')
        if self.category in ('outline','object'):
            layout.prop(self, 'include_open')
        if self.category == 'normal_angle':
            layout.prop(self, 'min_angle')
            layout.prop(self, 'max_angle')

    def execute(self, context):
        try:
            change = {key: getattr(self, key) for key in ('category', 'channel', 'enabled', 'mode',
                                                         'include_open', 'min_angle', 'max_angle')}
            set_sources(context.scene, [dict(group=self.group, enabled=True, routes=[change])])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class TOONVIEWPORT_OT_edge_channel(bpy.types.Operator):
    bl_idname = 'toon_viewport.edge_channel'
    bl_label = '设置本组可见性通道'
    bl_options = {'REGISTER', 'UNDO'}
    group: StringProperty(options={'HIDDEN'})
    channel: EnumProperty(items=[('VISIBLE', '可见线', ''), ('HIDDEN', '隐藏线', '')])
    enabled: BoolProperty(default=True)

    def execute(self, context):
        try:
            key = 'visible_enabled' if self.channel == 'VISIBLE' else 'hidden_enabled'
            set_sources(context.scene, [dict(group=self.group, enabled=True, routes=[], **{key: self.enabled})])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def draw(layout, plan, gid, *, channel=None):
    record = edge_sources.records(plan['raw']).get(gid)
    active = record is not None and record['enabled']
    authored=next(g for g in plan['groups'] if g['id']==gid).get('authored',False)
    op = layout.operator('toon_viewport.edge_profile',
        text=('停用边界来源' if active else '启用边界来源') if authored else ('恢复之前来源' if active else '启用统一边界来源'), depress=active)
    op.group, op.enabled = gid, not active
    if not active:
        return False
    group = next(g for g in plan['groups'] if g['id'] == gid)
    row = layout.row(align=True)
    row.label(text='组通道')
    for gate_channel, gate, label in (('VISIBLE', 'visible_enabled', '可见线 V'), ('HIDDEN', 'hidden_enabled', '隐藏线 H')):
        op = row.operator('toon_viewport.edge_channel', text=label, depress=group[gate])
        op.group, op.channel, op.enabled = gid, gate_channel, not group[gate]
    layout.label(text='默认从轮廓开始；各类可独立设置 V/H')
    categories=edge_sources.CATEGORIES if len(record['routes'])==14 else edge_sources.LEGACY_CATEGORIES
    if len(record['routes'])==12:
        op=layout.operator('toon_viewport.edge_profile',text='添加对象内线设置')
        op.group,op.enabled=gid,True
    for category in categories:
        row = layout.row(align=True)
        row.label(text=LABELS[category])
        for route_channel, label in (('VISIBLE', 'V'), ('HIDDEN', 'H')):
            if channel is not None and route_channel!=channel:continue
            route = next(r for r in record['routes'] if r['category'] == category and r['channel'] == route_channel)
            op = row.operator('toon_viewport.edge_route',
                text=label+(' · 专用' if route['mode'] == 'SPECIFIC' else ' · 默认'), depress=route['enabled'])
            op.group, op.category, op.channel = gid, category, route_channel
    layout.label(text='法线角使用世界空间面法线，闭区间（度）')
    if edge_sources.object_active(group):
        layout.label(text='对象内线：正交／透视、实心双面，支持显式合并域')
    return True


CLASSES = (TOONVIEWPORT_OT_edge_profile, TOONVIEWPORT_OT_edge_route, TOONVIEWPORT_OT_edge_channel)
