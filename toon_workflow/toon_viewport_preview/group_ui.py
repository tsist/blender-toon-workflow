# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic active-group RNA, element filters and reversible author actions."""
import copy
import re
from contextlib import contextmanager

import bpy
from bpy.props import EnumProperty,FloatProperty,FloatVectorProperty,StringProperty
from . import group_lifecycle as G

FIELDS=('name','label','enabled','width_scale','opacity','line_set_id','elements_only')
_GUARDS=set()


def guarded(scene):return scene.as_pointer() in _GUARDS


@contextmanager
def guard(scene):
    key=scene.as_pointer();_GUARDS.add(key)
    try:yield
    finally:_GUARDS.discard(key)


def snapshot(settings):
    return dict(groups=[{k:getattr(g,k) for k in FIELDS} for g in settings.groups],
        elements={e.name:[dict(name=g.name,label=g.label,enabled=g.enabled) for g in e.group_filters] for e in settings.elements},
        curve_group=settings.curve_group,curve_slot=settings.curve_slot,curve_error=settings.curve_error)


def _animation_paths(scene):
    ad=scene.animation_data
    result=[d.data_path for d in ad.drivers] if ad is not None else []
    actions={(ad.action,ad.action_slot.handle if ad.action_slot else None)} if ad is not None and ad.action is not None else set()
    for track in ad.nla_tracks if ad is not None else ():
        for strip in track.strips:
            if strip.action is not None:actions.add((strip.action,strip.action_slot.handle if strip.action_slot else None))
    for action,handle in actions:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip,'channelbags',[]):
                    if handle is None or bag.slot_handle==handle:result.extend(fc.data_path for fc in bag.fcurves)
    # Incoming SINGLE_PROP variables are references too. They may belong to an
    # Object/Material/NodeTree instead of the Scene's own animation container.
    for block in bpy.data.user_map():
        animation=getattr(block,'animation_data',None)
        if animation is None:continue
        for driver in animation.drivers:
            for variable in driver.driver.variables:
                for target in variable.targets:
                    if target.id==scene:result.append(target.data_path)
    return result


def prepare(scene, document, plan, report):
    settings=scene.toon_viewport_preview;old=snapshot(settings)
    wanted={g['id']:g for g in plan['groups']};restored=report.get('restore')
    rows=[copy.deepcopy(g) for g in old['groups'] if g['name'] in wanted]
    for gid,g in wanted.items():
        if any(r['name']==gid for r in rows):continue
        row=(copy.deepcopy(restored['group']) if restored and restored['group']['name']==gid else
            dict(name=gid,label=g['label'] or gid,enabled=True,width_scale=1.,opacity=1.,line_set_id=g['source_line_set_id'],elements_only=g['output_to_elements_only']))
        index=min(restored['index'],len(rows)) if restored and restored['group']['name']==gid else len(rows)
        rows.insert(index,row)
    for path in _animation_paths(scene):
        match=re.match(r'toon_viewport_preview\.groups\[(\d+)\]',path)
        if match:
            index=int(match.group(1))
            G.need(index<len(old['groups']) and index<len(rows) and old['groups'][index]['name']==rows[index]['name'],
                   '组控制存在按序号绑定的动画/驱动，请先改为按组名称绑定后再移除或恢复')
    elements={}
    for element in settings.elements:
        previous={r['name']:r['enabled'] for r in old['elements'][element.name]}
        elements[element.name]=[]
        for row in rows:
            gid=row['name'];enabled=previous.get(gid,not element.use_group_filter)
            if restored and gid==restored['group']['name']:
                enabled=restored['elements'].get(element.name,enabled)
            elements[element.name].append(dict(name=gid,label=row['label'],enabled=enabled))
    selected=report.get('select_group',old['curve_group'])
    if selected not in wanted:selected=rows[0]['name']
    slot=old['curve_slot'] if selected==old['curve_group'] else 'brush'
    selected_group=wanted[selected]
    if slot not in selected_group['brush_slots'] and slot not in selected_group.get('intersection_sources',{}).get('authors',{}) and slot!='intersection_brush':slot='brush'
    return old,dict(groups=rows,elements=elements,curve_group=selected,curve_slot=slot,curve_error='')


def apply(settings, data):
    scene=settings.id_data
    with guard(scene):
        settings.groups.clear()
        for row in data['groups']:
            item=settings.groups.add()
            for key,value in row.items():setattr(item,key,value)
        for element in settings.elements:
            element.group_filters.clear()
            for row in data['elements'][element.name]:
                item=element.group_filters.add()
                for key,value in row.items():setattr(item,key,value)
        settings.curve_group=data['curve_group'];settings.curve_slot=data['curve_slot'];settings.curve_error=data['curve_error']


def change(scene,action,**values):
    from .wire_ui import apply_migration
    settings=scene.toon_viewport_preview
    if action=='ARCHIVE':
        gid=values['group'];rows=snapshot(settings)
        index=next(i for i,g in enumerate(rows['groups']) if g['name']==gid)
        values['snapshot']=dict(group=rows['groups'][index],index=index,
            elements={name:next((r['enabled'] for r in filters if r['name']==gid),False) for name,filters in rows['elements'].items()})
    return apply_migration(scene,dict(action=action,**values),G.migrate,lifecycle=True)


class TOONVIEWPORT_OT_group_new(bpy.types.Operator):
    bl_idname='toon_viewport.group_new';bl_label='新建独立样式组';bl_options={'REGISTER','UNDO'}
    label:StringProperty(name='名称',default='独立样式组',maxlen=256)
    def invoke(self,context,event):return context.window_manager.invoke_props_dialog(self,width=360)
    def draw(self,context):
        self.layout.prop(self,'label');self.layout.label(text='独立 V/H 默认笔刷，初始绑定为空')
    def execute(self,context):
        try:change(context.scene,'NEW',label=self.label);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_OT_group_state(bpy.types.Operator):
    bl_idname='toon_viewport.group_state';bl_label='移除或恢复样式组';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    action:EnumProperty(items=[('ARCHIVE','移除并保留恢复',''),('RESTORE','恢复组','')])
    def execute(self,context):
        try:change(context.scene,self.action,group=self.group);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_OT_group_brush(bpy.types.Operator):
    bl_idname='toon_viewport.group_brush';bl_label='编辑独立笔刷';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    actual:StringProperty(options={'HIDDEN'})
    native_size:FloatProperty(name='笔刷尺寸',default=1,min=0)
    blend_amount:FloatProperty(name='不透明度',default=1,min=0,max=1)
    color:FloatVectorProperty(name='颜色',subtype='COLOR',size=3,default=(0,0,0),min=0,max=1)
    def invoke(self,context,event):
        try:
            from .addon import _preset
            document=_preset(context.scene)['raw'];eg=next(g for g in document['inputs']['projection']['editor_document']['groups'] if g['uid']==self.group)
            brush=eg['brushes'][G.E.slot_for(self.actual)]
            for key in ('native_size','blend_amount','color'):setattr(self,key,brush[key])
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self,width=360)
    def draw(self,context):
        for key in ('native_size','blend_amount','color'):self.layout.prop(self,key)
    def execute(self,context):
        try:
            from .wire_ui import apply_migration
            apply_migration(context.scene,dict(action='BRUSH',group=self.group,actual=self.actual,
                values=dict(native_size=self.native_size,blend_amount=self.blend_amount,color=list(self.color))),G.migrate)
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


def draw_header(layout,plan):
    layout.operator('toon_viewport.group_new',icon='ADD')
    archived=G.records(plan['raw'])['archived']
    if archived:
        box=layout.box();box.label(text='可恢复的样式组')
        for gid,saved in archived.items():
            op=box.operator('toon_viewport.group_state',text='恢复 · '+saved['group']['label'],icon='LOOP_BACK');op.group=gid;op.action='RESTORE'


def draw_group(layout,plan,gid):
    op=layout.operator('toon_viewport.group_state',text='移除组并保留恢复',icon='REMOVE');op.group=gid;op.action='ARCHIVE'
    row=G.owned(plan['raw']).get(gid)
    if row is not None:
        box=layout.box();box.label(text='自主笔刷 · 独立于其他组')
        for actual,meta in row['brushes'].items():
            op=box.operator('toon_viewport.group_brush',text=meta['channel']+' · '+meta['role']);op.group=gid;op.actual=actual


CLASSES=(TOONVIEWPORT_OT_group_new,TOONVIEWPORT_OT_group_state,TOONVIEWPORT_OT_group_brush)
