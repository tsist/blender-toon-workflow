# SPDX-License-Identifier: GPL-3.0-or-later
"""Public Line/socket and collection-merge authoring through the shared commit."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from . import line_hierarchy as H
_ENUM_ITEMS={}


def set_hierarchy(scene,updates):
    from .wire_ui import apply_migration
    return apply_migration(scene,updates,H.migrate)


def _groups(self,context):
    try:
        from .addon import _preset
        plan=_preset(context.scene)
        labels={g.name:g.label for g in context.scene.toon_viewport_preview.groups}
        rows=[(g['id'],labels.get(g['id'],g['id']),'') for g in plan['raw']['request']['boundary']['groups']]
        _ENUM_ITEMS[context.scene.as_pointer()]=rows
        return rows
    except Exception:return [('','无样式组','')]


class TOONVIEWPORT_OT_line_edit(bpy.types.Operator):
    bl_idname='toon_viewport.line_edit';bl_label='编辑Line层级';bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[(a,b,'') for a,b in [('new','新建Line'),('set','修改Line'),
        ('socket','增加空槽'),('shrink','收起空槽'),('connect','连接样式组'),('move','移动槽位'),('remove','移除空Line')]])
    line:StringProperty(options={'HIDDEN'})
    index:IntProperty(default=0,min=0,max=63)
    direction:IntProperty(default=-1,min=-1,max=1)
    group:EnumProperty(items=_groups)
    name:StringProperty(name='名称',default='Line',maxlen=128)
    priority:IntProperty(name='渲染优先级',default=0,min=-1000000,max=1000000)
    enabled:BoolProperty(name='启用',default=True)
    size_mode:EnumProperty(name='线宽尺寸',items=[('INHERIT','沿用组设置',''),('ABSOLUTE','绝对像素',''),('RELATIVE','相对画面尺寸','')])
    antialiasing:FloatProperty(name='抗锯齿羽化（-1沿用）',default=-1,min=-1,max=16)
    oversampling:EnumProperty(name='每轴采样',items=[('0','沿用',''),('1','1',''),('2','2',''),('4','4','')])
    off_screen_distance:FloatProperty(name='画外延伸像素（-1沿用）',default=-1,min=-1,max=65536)
    elements_only:BoolProperty(name='仅输出到渲染元素',default=False)
    def invoke(self,context,event):
        if self.action=='set':
            from .addon import _preset
            row=next(r for r in H.records(_preset(context.scene)['raw'])['lines'] if r['id']==self.line)
            self.name=row['name'];self.priority=row['priority'];self.enabled=row['enabled']
            self.size_mode=row['size_mode'] or 'INHERIT';self.antialiasing=-1 if row['antialiasing'] is None else row['antialiasing']
            self.oversampling=str(row['oversampling'] or 0);self.off_screen_distance=-1 if row['off_screen_distance'] is None else row['off_screen_distance']
            self.elements_only=row['elements_only']
        if self.action in ('new','set','connect'):return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)
    def draw(self,context):
        if self.action in ('new','set'):
            self.layout.prop(self,'name');self.layout.prop(self,'priority')
            if self.action=='set':self.layout.prop(self,'enabled')
            for field in ('size_mode','antialiasing','oversampling','off_screen_distance','elements_only'):self.layout.prop(self,field)
        elif self.action=='connect':self.layout.prop(self,'group',text='样式组')
    def execute(self,context):
        try:
            fields=dict(action=self.action,line=self.line,index=self.index,direction=self.direction)
            if self.action in ('new','set'):fields.update(name=self.name,priority=self.priority,enabled=self.enabled)
            if self.action in ('new','set'):fields.update(size_mode=None if self.size_mode=='INHERIT' else self.size_mode,
                antialiasing=None if self.antialiasing<0 else self.antialiasing,oversampling=int(self.oversampling) or None,
                off_screen_distance=None if self.off_screen_distance<0 else self.off_screen_distance,elements_only=self.elements_only)
            if self.action=='connect':fields['group']=self.group
            set_hierarchy(context.scene,fields);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_OT_merge_edit(bpy.types.Operator):
    bl_idname='toon_viewport.merge_edit';bl_label='编辑集合合并域';bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[('merge_collection','集合合并域',''),('merge_group','描边合并开关','')])
    domain:StringProperty(name='合并域名称',default='Merge',maxlen=128)
    remove:BoolProperty(name='移出合并域',default=False)
    group:StringProperty(options={'HIDDEN'})
    visible:BoolProperty(name='合并可见轮廓',default=True)
    hidden:BoolProperty(name='合并隐藏轮廓',default=True)
    def invoke(self,context,event):
        if self.action=='merge_collection':
            context.scene.toon_viewport_preview.binding_collection=context.collection
        else:
            from .addon import _preset
            row=next((r for r in H.records(_preset(context.scene)['raw'])['merge_groups'] if r['group']==self.group),None)
            self.visible=bool(row and row['visible']);self.hidden=bool(row and row['hidden'])
        return context.window_manager.invoke_props_dialog(self)
    def draw(self,context):
        if self.action=='merge_collection':
            self.layout.prop(context.scene.toon_viewport_preview,'binding_collection',text='集合')
            self.layout.prop(self,'domain');self.layout.prop(self,'remove')
        else:self.layout.prop(self,'visible');self.layout.prop(self,'hidden')
    def execute(self,context):
        try:
            if self.action=='merge_collection':
                from .selector_capture import reference
                collection=context.scene.toon_viewport_preview.binding_collection
                if collection is None:raise ValueError('请选择集合')
                fields=dict(action=self.action,collection=reference(collection),domain=None if self.remove else self.domain)
            else:fields=dict(action=self.action,group=self.group,visible=self.visible,hidden=self.hidden)
            set_hierarchy(context.scene,fields);return {'FINISHED'}
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_PT_lines(bpy.types.Panel):
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='二维描边';bl_label='Line层级与集合合并'
    bl_options={'DEFAULT_CLOSED'}
    def draw(self,context):
        layout=self.layout
        try:
            from .addon import _preset
            plan=_preset(context.scene);value=H.records(plan['raw'])
        except Exception:
            layout.label(text='请先载入或创建独立样式');return
        labels={g.name:g.label for g in context.scene.toon_viewport_preview.groups}
        layout.operator('toon_viewport.line_edit',text='新建Line',icon='ADD').action='new'
        for line in sorted(value['lines'],key=lambda r:(r['priority'],r['name'],r['id'])):
            box=layout.box();header=box.row();header.label(text=line['name']+' · '+str(line['priority']),icon='HIDE_OFF' if line['enabled'] else 'HIDE_ON')
            op=header.operator('toon_viewport.line_edit',text='',icon='PREFERENCES');op.action='set';op.line=line['id']
            op=header.operator('toon_viewport.select_parameter_animation',text='',icon='KEY_HLT');op.scope='LINE';op.owner=line['id']
            for i,gid in enumerate(line['slots']):
                row=box.row(align=True);row.label(text=str(i+1)+'. '+(labels.get(gid,gid) if gid else '空槽'))
                if gid:
                    op=row.operator('toon_viewport.merge_edit',text='',icon='AUTOMERGE_ON');op.action='merge_group';op.group=gid
                    op=row.operator('toon_viewport.select_parameter_animation',text='',icon='KEY_HLT');op.scope='MERGE';op.owner=gid
                else:
                    op=row.operator('toon_viewport.line_edit',text='连接');op.action='connect';op.line=line['id'];op.index=i
                for delta,icon in ((-1,'TRIA_UP'),(1,'TRIA_DOWN')):
                    cell=row.row();cell.enabled=0<=i+delta<len(line['slots'])
                    op=cell.operator('toon_viewport.line_edit',text='',icon=icon);op.action='move';op.line=line['id'];op.index=i;op.direction=delta
            row=box.row(align=True)
            for action,label in [('socket','增加空槽'),('shrink','收起空槽'),('remove','移除空Line')]:
                op=row.operator('toon_viewport.line_edit',text=label);op.action=action;op.line=line['id']
        layout.operator('toon_viewport.merge_edit',text='设置集合合并域',icon='OUTLINER_COLLECTION').action='merge_collection'
        for row in value['merge_domains']:layout.label(text=row['id']+': '+', '.join(r['name'] for r in row['collections']))


CLASSES=(TOONVIEWPORT_OT_line_edit,TOONVIEWPORT_OT_merge_edit,TOONVIEWPORT_PT_lines)
