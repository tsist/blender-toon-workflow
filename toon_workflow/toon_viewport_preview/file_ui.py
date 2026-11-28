# SPDX-License-Identifier: GPL-3.0-or-later
"""Public author file, helper and scoped output UI; registered by the host."""
import json
import bpy
from bpy.props import StringProperty,EnumProperty,BoolProperty,PointerProperty,FloatProperty
from . import file_authoring as F,preset_catalog as P,output_scope as O,author_helpers as H,author_graph


class AuthorSettings(bpy.types.PropertyGroup):
    author_tree:PointerProperty(name='作者图',type=bpy.types.NodeTree)
    path:StringProperty(name='样式 / 预设文件',subtype='FILE_PATH')
    label:StringProperty(name='作者名称',default='自主线条')
    conflict:EnumProperty(name='同名处理',items=[(k,label,'') for k,label in [('ERROR','停止并报告'),('RENAME','自动改名'),('REPLACE_LABEL','替换同名自主组')]])
    preset:EnumProperty(name='自主预设',items=[(n,n,'独立自主设计') for n in P.NAMES])
    scope:EnumProperty(name='输出作用域',items=[('SCENE','场景',''),('LAYER','当前视图层','')])
    overrides:StringProperty(name='覆盖值 JSON',default='{"settings":{"width_multiplier":1.0}}')
    keyed_only:BoolProperty(name='仅关键帧输出',default=False)
    preview_image:PointerProperty(type=bpy.types.Image)
    worker_timeout:FloatProperty(name='后台输出超时（秒）',default=300.,min=.1,max=86400.)
    abort_on_error:BoolProperty(name='设置错误时中止并抛出',default=True)
    shape_mode:EnumProperty(name='选择来源',items=[('FACES','选面',''),('NOISE','空间噪声',''),('IMAGE','UV 图像 Alpha',''),('RAYCAST','射线命中','')])
    shape_image:PointerProperty(name='选择图像',type=bpy.types.Image)
    shape_target:PointerProperty(name='射线目标',type=bpy.types.Object)
    shape_threshold:FloatProperty(name='选择阈值',default=.5,min=0,max=1)
    shape_border:BoolProperty(name='仅边界形状',default=False)


class TOONVIEWPORT_OT_author_file(bpy.types.Operator):
    bl_idname='toon_viewport.author_file';bl_label='自主作者文件';bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[(n,n,'') for n in ('NEW','IMPORT','APPEND','EXPORT','PRESET','RESET','LOAD_PRESET','SAVE_PRESET','WORKSPACE','SHAPE','OVERRIDE','REMOVE_OVERRIDE','OUTPUT','CLEANUP','FIX_NAMES','PREVIEW')])
    use_current_scope_values:BoolProperty(default=False, options={'HIDDEN'})
    def execute(self,context):
        scene=context.scene;s=scene.toon_author_files
        try:
            if self.action=='NEW':F.new_scene_style(scene,label=s.label)
            elif self.action in ('CLEANUP','FIX_NAMES'):F.cleanup_references(scene,remove_missing=self.action=='CLEANUP',match_names=self.action=='FIX_NAMES')
            elif self.action in ('IMPORT','APPEND'):F.import_style(scene,bpy.path.abspath(s.path),mode='APPEND' if self.action=='APPEND' else 'REPLACE',conflict=s.conflict)
            elif self.action=='EXPORT':F.export_style(scene,bpy.path.abspath(s.path))
            elif self.action=='WORKSPACE':H.workspace(context,name=s.label)
            elif self.action=='SHAPE':H.selection_generator(context,label=s.label,mode=s.shape_mode,image=s.shape_image,ray_target=s.shape_target,threshold=s.shape_threshold,border_only=s.shape_border)
            elif self.action=='OVERRIDE':
                layer = context.view_layer.name if s.scope=='LAYER' else None
                if self.use_current_scope_values:
                    pencil = scene.toon_viewport_preview
                    document = O.read(scene)
                    values = document['scene'] if layer is None else document['layers'].get(layer, {})
                    values.setdefault('settings', {}).update(
                        {name: list(getattr(pencil, name)) if name=='line_color' else getattr(pencil, name)
                         for name in O.FIELDS})
                else:
                    values = json.loads(s.overrides)
                O.set_scope(scene,values,view_layer=layer)
            elif self.action=='REMOVE_OVERRIDE':O.remove_scope(scene,view_layer=context.view_layer.name if s.scope=='LAYER' else None)
            elif self.action=='OUTPUT':
                result=O.request(bpy.path.abspath(s.path),context,keyed_only=s.keyed_only)
                if result.get('status')=='FAILED':raise ValueError(result['error'])
            else:
                from . import source_bindings,edge_sources,brush_effects
                settings=scene.toon_viewport_preview;wrapped=F.read_scene(scene)
                actual=next(r['actual_brush_id'] for r in source_bindings.recover(wrapped['document'])['routes']
                    if r['group_id']==settings.curve_group and r['slot']==settings.curve_slot)
                if self.action=='PRESET':P.apply(scene,actual,s.preset)
                elif self.action=='PREVIEW':
                    from . import brush_preview
                    s.preview_image=brush_preview.render(scene,actual,bpy.path.abspath(s.path))
                elif self.action=='RESET':P.reset(scene,actual)
                elif self.action=='LOAD_PRESET':P.load(scene,actual,bpy.path.abspath(s.path))
                elif self.action=='SAVE_PRESET':
                    effects=brush_effects.records(wrapped['document']).get(actual,brush_effects.defaults())
                    P.save(bpy.path.abspath(s.path),effects,label=s.label)
            self.report({'INFO'},'操作已完成');return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class TOONVIEWPORT_PT_author_files(bpy.types.Panel):
    bl_label='自主作者 · 文件与范围';bl_idname='TOONVIEWPORT_PT_author_files'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='二维描边'
    def draw(self,context):draw(self.layout,context)


def draw(layout,context):
    s=context.scene.toon_author_files
    layout.template_ID(s,'author_tree')
    row=layout.row(align=True)
    for action,label in [('NEW','新作者图'),('SHOW','打开图'),('APPLY','应用图')]:row.operator('toon_viewport.author_graph',text=label).action=action
    row=layout.row(align=True)
    for action,label in [('SYNC','同步组选中'),('RESET','清除节点选择')]:row.operator('toon_viewport.author_graph',text=label).action=action
    layout.prop(s,'label');layout.operator('toon_viewport.author_file',text='从零新建并绑定选中网格').action='NEW'
    layout.prop(s,'path');layout.prop(s,'conflict')
    row=layout.row(align=True)
    for action,label in [('IMPORT','替换载入'),('APPEND','追加'),('EXPORT','完整导出')]:row.operator('toon_viewport.author_file',text=label).action=action
    layout.prop(s,'preset');row=layout.row(align=True)
    row.operator('toon_viewport.author_file',text='应用到当前笔刷').action='PRESET';row.operator('toon_viewport.author_file',text='重置').action='RESET'
    row=layout.row(align=True)
    row.operator('toon_viewport.author_file',text='读取笔刷预设').action='LOAD_PRESET';row.operator('toon_viewport.author_file',text='保存笔刷预设').action='SAVE_PRESET'
    layout.operator('toon_viewport.author_file',text='生成当前笔刷 / 笔触 PNG 预览').action='PREVIEW'
    if s.preview_image:layout.template_ID(s,'preview_image')
    row=layout.row(align=True)
    row.operator('toon_viewport.author_file',text='创建工作区').action='WORKSPACE';row.operator('toon_viewport.author_file',text='选面生成形状').action='SHAPE'
    layout.prop(s,'shape_mode');layout.prop(s,'shape_border')
    if s.shape_mode=='IMAGE':layout.prop(s,'shape_image')
    elif s.shape_mode=='RAYCAST':layout.prop(s,'shape_target')
    if s.shape_mode in ('NOISE','IMAGE'):layout.prop(s,'shape_threshold')
    row=layout.row(align=True)
    row.operator('toon_viewport.author_file',text='清理失效作者引用').action='CLEANUP';row.operator('toon_viewport.author_file',text='按唯一名称修复引用').action='FIX_NAMES'
    layout.separator();layout.prop(s,'scope')
    row=layout.row(align=True)
    op=row.operator('toon_viewport.author_file',text='用当前参数覆盖此范围')
    op.action='OVERRIDE';op.use_current_scope_values=True
    row.operator('toon_viewport.author_file',text='移除覆盖').action='REMOVE_OVERRIDE'
    layout.prop(s,'keyed_only');layout.operator('toon_viewport.author_file',text='按范围导出').action='OUTPUT'
    layout.prop(s,'worker_timeout');layout.prop(s,'abort_on_error')
    layout.label(text='输出程序：当前 Blender（独立后台工作进程）')


CLASSES=author_graph.CLASSES+(AuthorSettings,TOONVIEWPORT_OT_author_file,TOONVIEWPORT_PT_author_files)
def register():
    from . import brush_preview
    brush_preview.register()
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.toon_author_files=PointerProperty(type=AuthorSettings)
    bpy.types.NODE_MT_add.append(author_graph.draw_add)
    bpy.types.NODE_HT_header.append(author_graph.draw_header)
    author_graph.ensure_runtime()
def unregister():
    from . import runtime_preferences
    runtime_preferences.clear()
    author_graph.clear_runtime()
    bpy.types.NODE_MT_add.remove(author_graph.draw_add)
    bpy.types.NODE_HT_header.remove(author_graph.draw_header)
    del bpy.types.Scene.toon_author_files
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
    from . import brush_preview
    brush_preview.unregister()
