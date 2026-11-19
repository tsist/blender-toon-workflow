# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit Scene-style line-function authors with material identity bindings."""
import copy
import uuid
import bpy
from bpy.props import BoolProperty,EnumProperty,FloatProperty,FloatVectorProperty,IntProperty,StringProperty
from . import line_functions as F
from .selector_capture import reference
from .selector_bindings import key

def set_functions(scene,updates):
    from .wire_ui import apply_migration
    return apply_migration(scene,updates,F.migrate)

def _rows(scene):
    from .addon import _preset
    ext=F.records(_preset(scene)['raw'])
    return {} if ext is None else {r['id']:r for r in ext['containers']}

class TOONVIEWPORT_OT_line_function_add(bpy.types.Operator):
    bl_idname='toon_viewport.line_function_add';bl_label='添加材质线条功能';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            material=getattr(context,'material',None) or (context.object.active_material if context.object else None)
            if material is None:raise ValueError('请选择一个材质')
            ref=reference(material)
            if any(ref in r['materials'] for r in _rows(context.scene).values()):raise ValueError('该材质已绑定线条功能')
            row=F.default('line-function-'+uuid.uuid4().hex);row['materials']=[ref]
            set_functions(context.scene,dict(containers=[row]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_line_function_edit(bpy.types.Operator):
    bl_idname='toon_viewport.line_function_edit';bl_label='编辑材质线条功能';bl_options={'REGISTER','UNDO'}
    identifier:StringProperty(options={'HIDDEN'})
    disable_intersection:BoolProperty(name='禁用交线')
    draw_hidden_lines:BoolProperty(name='穿过本材质的全部隐藏线作为可见线')
    draw_hidden_lines_of_targets:BoolProperty(name='指定目标隐藏线作为可见线')
    mask_hidden_lines_of_targets:BoolProperty(name='遮住指定目标隐藏线')
    category:EnumProperty(name='边类型',items=[(k,k,'') for k in F.CATEGORIES])
    replace:BoolProperty(name='替换颜色')
    color:FloatVectorProperty(name='颜色',size=3,subtype='COLOR',min=0,max=1,default=(0,0,0))
    amount:FloatProperty(name='替换量',min=0,max=1,default=1)
    def invoke(self,context,event):
        row=_rows(context.scene)[self.identifier]
        for gate in F.GATES:setattr(self,gate,row[gate])
        override=row['overrides'][self.category]
        self.replace=override['on'];self.color=override['color'];self.amount=override['amount']
        return context.window_manager.invoke_props_dialog(self,width=430)
    def draw(self,context):
        for gate in F.GATES:self.layout.prop(self,gate)
        self.layout.label(text='边类型：'+self.category)
        self.layout.prop(self,'replace');self.layout.prop(self,'color');self.layout.prop(self,'amount')
    def execute(self,context):
        try:
            row=_rows(context.scene)[self.identifier]
            for gate in F.GATES:row[gate]=getattr(self,gate)
            row['overrides'][self.category]=dict(on=self.replace,color=list(self.color),amount=self.amount)
            set_functions(context.scene,dict(containers=[row]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_line_function_target(bpy.types.Operator):
    bl_idname='toon_viewport.line_function_target';bl_label='编辑线条功能目标';bl_options={'REGISTER','UNDO'}
    identifier:StringProperty(options={'HIDDEN'})
    kind:EnumProperty(items=[(k,k,'') for k in F.LISTS])
    remove:BoolProperty(default=False)
    index:IntProperty(min=0)
    target_key:StringProperty(options={'HIDDEN'})
    def invoke(self,context,event):
        if self.remove:return self.execute(context)
        settings=context.scene.toon_viewport_preview
        settings.binding_object=context.object
        settings.binding_material=getattr(context,'material',None) or (context.object.active_material if context.object else None)
        return context.window_manager.invoke_props_dialog(self)
    def draw(self,context):self.layout.prop(context.scene.toon_viewport_preview,'binding_object' if self.kind.endswith('objects') else 'binding_material')
    def execute(self,context):
        try:
            row=_rows(context.scene)[self.identifier]
            if self.remove:
                if self.target_key and key(row[self.kind][self.index])!=self.target_key:raise ValueError('目标列表已变化，请重新选择')
                row[self.kind].pop(self.index)
            else:
                block=getattr(context.scene.toon_viewport_preview,'binding_object' if self.kind.endswith('objects') else 'binding_material')
                if block is None:raise ValueError('请选择目标')
                ref=reference(block)
                if ref not in row[self.kind]:row[self.kind].append(ref)
            set_functions(context.scene,dict(containers=[row]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_line_function_remove(bpy.types.Operator):
    bl_idname='toon_viewport.line_function_remove';bl_label='移除线条功能容器';bl_options={'REGISTER','UNDO'}
    identifier:StringProperty(options={'HIDDEN'})
    def execute(self,context):
        try:set_functions(context.scene,dict(remove=[self.identifier]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_hidden_mask(bpy.types.Operator):
    bl_idname='toon_viewport.hidden_mask';bl_label='遮住其他组的隐藏线';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    enabled:BoolProperty(default=True)
    def execute(self,context):
        try:set_functions(context.scene,dict(masks=[dict(group=self.group,enabled=self.enabled)]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class TOONVIEWPORT_OT_self_intersection(bpy.types.Operator):
    bl_idname='toon_viewport.self_intersection';bl_label='设置通道自交线';bl_options={'REGISTER','UNDO'}
    group:StringProperty(options={'HIDDEN'})
    channel:EnumProperty(items=[(ch,ch,'') for ch in ('VISIBLE','HIDDEN')])
    enabled:BoolProperty(default=True)
    def execute(self,context):
        try:
            from .addon import _preset
            document=_preset(context.scene)['raw'];fallback=document['request'].get('intersection',{}).get('settings',{}).get('self_intersection',False)
            row=dict(group=self.group,**F.self_channels(document,self.group,fallback));row[self.channel]=self.enabled
            set_functions(context.scene,dict(self_intersections=[row]));return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

def draw_mask(layout,plan,gid):
    ext=F.records(plan['raw']);active=bool(ext and any(r['group']==gid and r['enabled'] for r in ext['masks']))
    op=layout.operator('toon_viewport.hidden_mask',text='遮住其他组的隐藏线',depress=active);op.group=gid;op.enabled=not active
    op=layout.operator('toon_viewport.select_parameter_animation',text='遮罩开关动画',icon='KEY_HLT');op.scope='MASK';op.owner=gid
    fallback=plan['raw']['request'].get('intersection',{}).get('settings',{}).get('self_intersection',False)
    channels=F.self_channels(plan['raw'],gid,fallback)
    row=layout.row(align=True)
    for ch,label in [('VISIBLE','可见自交线'),('HIDDEN','隐藏自交线')]:
        op=row.operator('toon_viewport.self_intersection',text=label,depress=channels[ch]);op.group=gid;op.channel=ch;op.enabled=not channels[ch]
    op=layout.operator('toon_viewport.select_parameter_animation',text='自交开关动画',icon='KEY_HLT');op.scope='SELF';op.owner=gid

class TOONVIEWPORT_PT_line_functions(bpy.types.Panel):
    bl_label='自研材质线条功能';bl_idname='TOONVIEWPORT_PT_line_functions';bl_space_type='PROPERTIES';bl_region_type='WINDOW';bl_context='material'
    def draw(self,context):
        layout=self.layout
        try:rows=_rows(context.scene)
        except Exception:layout.label(text='请先加载可编辑样式');return
        material=context.material
        ref=reference(material) if material else None
        active=[r for r in rows.values() if ref in r['materials']]
        if not active:layout.operator('toon_viewport.line_function_add');return
        for row in active:
            layout.label(text=row['id'])
            op=layout.operator('toon_viewport.select_parameter_animation',text='线条功能参数动画',icon='KEY_HLT');op.scope='LF';op.owner=row['id']
            for category in F.CATEGORIES:
                op=layout.operator('toon_viewport.line_function_edit',text=category+' · '+('替换' if row['overrides'][category]['on'] else '原色'))
                op.identifier=row['id'];op.category=category
            for kind in F.LISTS:
                box=layout.box();op=box.operator('toon_viewport.line_function_target',text='添加 '+kind);op.identifier=row['id'];op.kind=kind
                for index,target in enumerate(row[kind]):
                    line=box.row();line.label(text=target['name']+' · '+(target['library'] or '本地'))
                    op=line.operator('toon_viewport.line_function_target',text='',icon='X');op.identifier=row['id'];op.kind=kind;op.remove=True;op.index=index;op.target_key=key(target)
            op=layout.operator('toon_viewport.line_function_remove');op.identifier=row['id']

CLASSES=(TOONVIEWPORT_OT_line_function_add,TOONVIEWPORT_OT_line_function_edit,TOONVIEWPORT_OT_line_function_target,TOONVIEWPORT_OT_line_function_remove,TOONVIEWPORT_OT_hidden_mask,TOONVIEWPORT_OT_self_intersection,TOONVIEWPORT_PT_line_functions)
