# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene-owned selector and ordering author actions using the shared transaction."""
import copy
import json
import bpy
from bpy.props import BoolProperty,EnumProperty,IntProperty,StringProperty
from . import selector_bindings as B
from .selector_capture import reference

LABELS=dict(objects='对象',collections='集合',materials='材质')

def set_selectors(scene,groups,*,order=...):
    from .wire_ui import apply_migration
    changes=dict(groups=groups)
    if order is not ...:changes['order']=order
    return apply_migration(scene,changes,B.migrate)

def _seed(scene,gid):
    from .addon import _preset
    plan=_preset(scene);rows,_=B.records(plan['raw'])
    if gid in rows:return copy.deepcopy(rows[gid])
    group=next(g for g in plan['groups'] if g['id']==gid)
    row=dict(group=gid,enabled=True,objects=[],collections=[],materials=[])
    for kind,names,blocks in [('objects',group['object_ids'],bpy.data.objects),('materials',group['material_names'],bpy.data.materials)]:
        for name in names:
            found=[block for block in blocks if block.name==name]
            if len(found)!=1:raise ValueError('原绑定名称缺失或跨库重名，请显式重新指定绑定：'+name)
            row[kind].append(reference(found[0]))
    return row

class TOONVIEWPORT_OT_selector_toggle(bpy.types.Operator):
    bl_idname='toon_viewport.selector_toggle';bl_label='切换独立绑定';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    enabled:BoolProperty(default=True)
    empty:BoolProperty(default=False)
    def execute(self,context):
        try:
            if self.empty:
                from .addon import _preset
                rows,_=B.records(_preset(context.scene)['raw'])
                if self.group in rows:raise ValueError('已存在独立绑定，请重新启用后编辑或逐项移除')
            row=(dict(group=self.group,enabled=True,objects=[],collections=[],materials=[]) if self.empty else _seed(context.scene,self.group))
            row['enabled']=self.enabled
            set_selectors(context.scene,[row]);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_selector_add(bpy.types.Operator):
    bl_idname='toon_viewport.selector_add';bl_label='添加绑定选择项';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    kind:EnumProperty(items=[(k,v,'') for k,v in LABELS.items()])
    def invoke(self,context,event):
        settings=context.scene.toon_viewport_preview
        settings.binding_object=context.active_object
        settings.binding_collection=context.collection if context.collection in list(bpy.data.collections) else None
        settings.binding_material=context.active_object.active_material if context.active_object else None
        return context.window_manager.invoke_props_dialog(self)
    def draw(self,context):self.layout.prop(context.scene.toon_viewport_preview,'binding_'+dict(objects='object',collections='collection',materials='material')[self.kind],text=LABELS[self.kind])
    def execute(self,context):
        try:
            block=getattr(context.scene.toon_viewport_preview,'binding_'+dict(objects='object',collections='collection',materials='material')[self.kind])
            if block is None:raise ValueError('请选择绑定目标')
            if self.kind=='collections' and block not in list(bpy.data.collections):raise ValueError('请选择命名子集合')
            row=_seed(context.scene,self.group);ref=reference(block)
            if ref not in row[self.kind]:row[self.kind].append(ref)
            row['enabled']=True;set_selectors(context.scene,[row]);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_selector_remove(bpy.types.Operator):
    bl_idname='toon_viewport.selector_remove';bl_label='移除绑定选择项';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    kind:EnumProperty(items=[(k,v,'') for k,v in LABELS.items()])
    index:IntProperty(min=0)
    target_key:StringProperty(options={'HIDDEN'})
    @classmethod
    def description(cls,context,properties):
        try:
            ref=json.loads(properties.target_key)
            return '移除绑定：'+ref['name']+' · '+(ref['library'] or '本地')
        except Exception:return '移除绑定选择项'
    def execute(self,context):
        try:
            row=_seed(context.scene,self.group)
            if self.target_key and B.key(row[self.kind][self.index])!=self.target_key:raise ValueError('绑定列表已更新，请重新选择要移除的项目')
            row[self.kind].pop(self.index)
            set_selectors(context.scene,[row]);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_selector_order(bpy.types.Operator):
    bl_idname='toon_viewport.selector_order';bl_label='调整样式组优先级';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    direction:IntProperty(default=-1,min=-1,max=1)
    restore:BoolProperty(default=False)
    def execute(self,context):
        try:
            from .addon import _preset
            if self.restore:set_selectors(context.scene,[],order=None)
            else:
                plan=_preset(context.scene);_,order=B.records(plan['raw'])
                if order is None:order=[g['id'] for g in sorted(plan['raw']['request']['boundary']['groups'],key=lambda g:g['priority'],reverse=True)]
                active=[g['id'] for g in sorted(plan['groups'],key=lambda g:g['priority'],reverse=True)]
                index=active.index(self.group);target=max(0,min(len(active)-1,index+self.direction))
                a,b=order.index(active[index]),order.index(active[target]);order[a],order[b]=order[b],order[a]
                set_selectors(context.scene,[],order=order)
            return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

def ordered(scene,groups):
    from .addon import _preset
    try:
        plan=_preset(scene);_,order=B.records(plan['raw'])
        if order is not None:return sorted(groups,key=lambda item:order.index(item.name))
    except Exception:pass
    return groups

def has_bindings(scene):
    from .addon import _preset
    try:return any('selection' in group for group in _preset(scene)['groups'])
    except Exception:return False

def draw(layout,plan,gid):
    rows,order=B.records(plan['raw']);record=rows.get(gid);active=record is not None and record['enabled']
    box=layout.box();box.label(text='绑定与优先级')
    row=box.row(align=True)
    op=row.operator('toon_viewport.selector_toggle',text='恢复原绑定' if active else ('重新启用独立绑定' if record is not None else '使用独立绑定'),depress=active)
    op.group,op.enabled=gid,not active
    for direction,icon in [(-1,'TRIA_UP'),(1,'TRIA_DOWN')]:
        op=row.operator('toon_viewport.selector_order',text='',icon=icon);op.group=gid;op.direction=direction
    if order is not None:
        op=box.operator('toon_viewport.selector_order',text='恢复原优先级');op.restore=True
    if not active:
        if record is None:
            op=box.operator('toon_viewport.selector_toggle',text='新建空绑定');op.group=gid;op.enabled=True;op.empty=True
        return
    box.label(text='对象、集合、材质按并集选择；较高优先级先取得所有权')
    for kind in B.KINDS:
        row=box.row();row.label(text=LABELS[kind]+f' · {len(record[kind])}')
        op=row.operator('toon_viewport.selector_picker',text='批量编辑');op.group=gid;op.kind=kind
        op=row.operator('toon_viewport.selector_add',text='',icon='ADD');op.group=gid;op.kind=kind
        for index,ref in enumerate(record[kind]):
            row=box.row(align=True)
            label=ref['name']+' · '+(ref['library'] or '本地')
            row.label(text=label)
            op=row.operator('toon_viewport.selector_remove',text='',icon='X');op.group=gid;op.kind=kind;op.index=index;op.target_key=B.key(ref)
    if not any(record[k] for k in B.KINDS):box.label(text='当前无绑定项，本组不绘线')

CLASSES=(TOONVIEWPORT_OT_selector_toggle,TOONVIEWPORT_OT_selector_add,TOONVIEWPORT_OT_selector_remove,TOONVIEWPORT_OT_selector_order)
