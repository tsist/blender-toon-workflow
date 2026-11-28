# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent V/H intersection source author controls."""
import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from . import ix_sources, ix_authoring, wire_ui


def set_sources(scene, updates):
    return wire_ui.apply_migration(scene, updates, ix_authoring.migrate, wrapped_migration=True)


class TOONVIEWPORT_OT_ix_profile(bpy.types.Operator):
    bl_idname = 'toon_viewport.ix_profile'
    bl_label = '切换交线独立来源'
    bl_options = {'REGISTER','UNDO'}
    group: StringProperty(options={'HIDDEN'})
    enabled: BoolProperty(default=True)

    def execute(self,context):
        try:
            set_sources(context.scene,[dict(group=self.group,enabled=self.enabled,routes={})])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_OT_ix_route(bpy.types.Operator):
    bl_idname = 'toon_viewport.ix_route'
    bl_label = '编辑交线通道来源'
    bl_options = {'REGISTER','UNDO'}
    group: StringProperty(options={'HIDDEN'})
    channel: EnumProperty(items=[('VISIBLE','可见线',''),('HIDDEN','隐藏线','')])
    enabled: BoolProperty(name='启用此通道',default=True)
    mode: EnumProperty(name='笔刷',items=[('DEFAULT','通道默认笔刷',''),
        ('SPECIFIC','已连接专用笔刷','采集时已激活、直接连接的 SIMPLE 笔刷')])

    def invoke(self,context,event):
        try:
            from . import addon
            row=ix_sources.records(addon._preset(context.scene)['raw'])[self.group]['routes'][self.channel]
            self.enabled,self.mode=row['enabled'],row['mode']
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self,width=320)

    def draw(self,context):
        self.layout.label(text='交线 · '+('可见线' if self.channel=='VISIBLE' else '隐藏线'))
        self.layout.prop(self,'enabled');self.layout.prop(self,'mode')

    def execute(self,context):
        try:
            set_sources(context.scene,[dict(group=self.group,enabled=True,
                routes={self.channel:dict(enabled=self.enabled,mode=self.mode)})])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


def draw(layout, plan, gid, *, channel=None):
    record=ix_sources.records(plan['raw']).get(gid)
    active=record is not None and record['enabled']
    op=layout.operator('toon_viewport.ix_profile',text='恢复原交线来源' if active else '启用交线 V/H 独立来源',depress=active)
    op.group,op.enabled=gid,not active
    if active:
        row=layout.row(align=True)
        for route_channel,label in (('VISIBLE','交线 V'),('HIDDEN','交线 H')):
            if channel is not None and route_channel!=channel:continue
            route=record['routes'][route_channel]
            op=row.operator('toon_viewport.ix_route',text=label+(' · 专用' if route['mode']=='SPECIFIC' else ' · 默认'),depress=route['enabled'])
            op.group,op.channel=gid,route_channel
        layout.label(text='共享曲线可在下方笔刷列表选择；全局交线开关仍生效')


CLASSES=(TOONVIEWPORT_OT_ix_profile,TOONVIEWPORT_OT_ix_route)
