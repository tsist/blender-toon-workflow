# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene legacy, ViewLayer and local compositor Image-node vector outputs."""
import json
import os

import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, CollectionProperty,
                       EnumProperty, IntProperty, PointerProperty, StringProperty)

from . import vector_output,vector_scope

VECTOR_PROP = vector_output.VECTOR_PROP
_FILE_TYPE_ITEMS = (
    ('AIEPS', 'AI(EPS)', 'Adobe Illustrator 8 compatible EPS with AI5 layer comments', 0),
    ('EPS', 'EPS', 'Encapsulated PostScript without layer comments', 1),
)
_CATEGORY_LABELS = {
    'outline': 'Outline / Open Edge',
    'object': 'Object',
    'intersection': 'Intersection',
    'smoothing': 'Smoothing Boundary',
    'material_id': 'Material ID Boundary',
    'selected_edges': 'Selected Edges',
    'normal_angle': 'Normal Angle',
    'wireframe': 'Wireframe',
}
_GROUPING_ITEMS = (
    ('PRESERVE', 'Preserve Layer Order', 'Keep build_layers layer order; no implicit regrouping'),
    ('LINE', 'Group by Line', 'Explicitly group by layer.line_id / line_hierarchy name'),
    ('LINE_SET', 'Group by Line Set', 'Explicitly group by numeric line_set_id and visibility'),
    ('CHANNEL', 'Group by Visibility', 'Explicitly group visible and hidden streams'),
    ('NONE', 'Single Group', 'Explicitly flatten all groups into one stream'),
)


def _get_output_path(self):
    try:
        return vector_output.output_path_for_item(self)
    except Exception:
        return ''


def _file_type_changed(self, context):
    if self.file_type == 'PLD':
        if '<LineName>' not in self.sub_path.upper():
            self.sub_path += '_<LineName>'
    else:
        self.sub_path = vector_output._PLACEHOLDER.sub('', self.sub_path)


class ToonVectorOutputItem(bpy.types.PropertyGroup):
    """The C02 VectorOutput control surface."""

    on: BoolProperty(name='On', default=True)
    sub_path: StringProperty(name='File Subpath', default='Line', subtype='FILE_NAME')
    file_type: EnumProperty(name='File Type', items=_FILE_TYPE_ITEMS, default='AIEPS',
                            update=_file_type_changed)
    visible_lines_on: BoolProperty(name='Visible Lines', default=True)
    hidden_lines_on: BoolProperty(name='Hidden Lines', default=True)
    outline_on: BoolProperty(name='Outline', default=True)
    object_on: BoolProperty(name='Object', default=True)
    intersection_on: BoolProperty(name='Intersection', default=True)
    smoothing_on: BoolProperty(name='Smoothing Boundary', default=True)
    material_id_on: BoolProperty(name='Material ID Boundary', default=True)
    selected_edges_on: BoolProperty(name='Selected Edges', default=True)
    normal_angle_on: BoolProperty(name='Normal Angle', default=True)
    wireframe_on: BoolProperty(name='Wireframe', default=True)
    line_set_ids: BoolVectorProperty(name='Output Line Set IDs', size=8, default=(True,) * 8)
    grouping: EnumProperty(name='Grouping', items=_GROUPING_ITEMS, default='PRESERVE')
    output_path: StringProperty(name='Output Path', get=_get_output_path)
    label: StringProperty(name='Label', default='')


class ToonVectorOutputState(bpy.types.PropertyGroup):
    """Independent embedded collection on the exact registered scope owner."""

    vector_outputs: CollectionProperty(type=ToonVectorOutputItem)
    vector_output_selected_index: IntProperty(default=0, options={'HIDDEN'})
    vector_output_base_path: StringProperty(name='Base Path', subtype='DIR_PATH')
    vector_output_message: StringProperty(default='', options={'SKIP_SAVE'})
    vector_output_error: BoolProperty(default=False, options={'SKIP_SAVE'})
    last_vector_report: StringProperty(default='', options={'SKIP_SAVE'})


class PCL4_OT_VectorOutput_Mixin:
    """Compatibility control named by the C02 feature map."""

    node_ptr: StringProperty(default='')

    def get_line_outputs(self, context):
        return vector_scope.resolve(context.scene,view_layer=context.view_layer,node_ptr=self.node_ptr,create_token=True)[0]


class TOONVECTOR_OT_new_vector_output(PCL4_OT_VectorOutput_Mixin, bpy.types.Operator):
    bl_idname = 'toon_vector.new_vector_output'
    bl_label = 'New Vector Output'
    bl_options = {'REGISTER', 'UNDO'}
    node_ptr: StringProperty(default='')

    def execute(self, context):
        try:state = self.get_line_outputs(context)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        item = state.vector_outputs.add()
        item.label = 'Output ' + str(len(state.vector_outputs))
        state.vector_output_selected_index = len(state.vector_outputs) - 1
        if len(state.vector_outputs) == 1 and not state.vector_output_base_path:
            render_path = bpy.path.abspath(context.scene.render.filepath)
            state.vector_output_base_path = (render_path if render_path and
                                             os.path.isdir(render_path)
                                             else os.path.dirname(render_path))
        return {'FINISHED'}


class TOONVECTOR_OT_remove_vector_output(PCL4_OT_VectorOutput_Mixin, bpy.types.Operator):
    bl_idname = 'toon_vector.remove_vector_output'
    bl_label = 'Remove Vector Output'
    bl_options = {'REGISTER', 'UNDO'}
    node_ptr: StringProperty(default='')

    def execute(self, context):
        try:state = self.get_line_outputs(context)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        index = state.vector_output_selected_index
        if 0 <= index < len(state.vector_outputs):
            state.vector_outputs.remove(index)
            state.vector_output_selected_index = min(index, len(state.vector_outputs) - 1)
            return {'FINISHED'}
        return {'CANCELLED'}


class TOONVECTOR_OT_vector_outputs_list_move_item(PCL4_OT_VectorOutput_Mixin, bpy.types.Operator):
    bl_idname = 'toon_vector.vector_outputs_list_move_item'
    bl_label = 'Move Item'
    bl_options = {'REGISTER', 'UNDO'}
    node_ptr: StringProperty(default='')

    button_type: StringProperty(default='UP')

    def execute(self, context):
        try:state = self.get_line_outputs(context)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        index0 = state.vector_output_selected_index
        if not 0 <= index0 < len(state.vector_outputs):
            return {'CANCELLED'}
        index1 = index0 + (-1 if self.button_type == 'UP' else (1 if self.button_type == 'DOWN' else 0))
        if 0 <= index1 < len(state.vector_outputs):
            state.vector_outputs.move(max(index0, index1), min(index0, index1))
            state.vector_output_selected_index = index1
            return {'FINISHED'}
        return {'CANCELLED'}


class TOONVECTOR_OT_export_vector(PCL4_OT_VectorOutput_Mixin,bpy.types.Operator):
    bl_idname = 'toon_vector.export_vector'
    bl_label = 'Export Vector Files'
    bl_description = 'Write enabled EPS/AIEPS outputs'
    bl_options = {'REGISTER'}
    node_ptr: StringProperty(default='')

    def execute(self, context):
        state=None
        try:
            state=self.get_line_outputs(context)
            report = vector_output.request_vector_output(
                state.vector_output_base_path, context,view_layer=context.view_layer,node_ptr=self.node_ptr)
        except Exception as error:
            message='Vector export failed: '+str(error)
            if state is not None:state.vector_output_message=message;state.vector_output_error=True
            self.report({'ERROR'},message)
            return {'CANCELLED'}
        state.last_vector_report = json.dumps(report, ensure_ascii=False, sort_keys=True)
        state.vector_output_error = not report.get('ok', False)
        if report.get('blocked'):
            self.report({'WARNING'}, state.vector_output_message)
        else:
            self.report({'INFO'}, state.vector_output_message)
        return {'FINISHED'}


class TOONVECTOR_UL_outputs(bpy.types.UIList):
    bl_idname = 'TOONVECTOR_UL_outputs'

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        split = row.split(factor=0.68)
        left = split.row(align=True)
        left.prop(item, 'on', text='')
        sub = left.row()
        sub.enabled = item.on
        sub.prop(item, 'sub_path', text='', emboss=False)
        right = split.row(align=True)
        right.alignment = 'RIGHT'
        right.enabled = item.on
        right.prop(item, 'file_type', text='', emboss=False)


class TOONVECTOR_PT_output(bpy.types.Panel):
    bl_idname = 'TOONVECTOR_PT_output'
    bl_label = 'Vector File Output'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '二维描边'
    bl_order = 20

    def draw(self, context):
        try:
            state = vector_output.vector_state(context.scene,view_layer=context.view_layer)
        except Exception as error:
            self.layout.label(text=str(error), icon='ERROR')
            return
        draw_outputs(self.layout,context,state,target_label='ViewLayer: '+context.view_layer.name)


def draw_outputs(layout,context,state,*,node_ptr='',target_label=''):
    """Both panels set every operation's real target using the same helper."""
    layout.label(text=target_label,icon='RENDERLAYERS' if not node_ptr else 'IMAGE_DATA')
    column=layout.column(align=True);column.enabled=len(state.vector_outputs)>0
    column.label(text='Base Path');column.prop(state,'vector_output_base_path',text='')
    layout.separator();split=layout.split(factor=.88);left=split.column();left.label(text='Outputs')
    left.template_list('TOONVECTOR_UL_outputs','',state,'vector_outputs',state,'vector_output_selected_index',rows=3,maxrows=3)
    buttons=left.row(align=True)
    buttons.operator('toon_vector.new_vector_output',text='Add',icon='ADD').node_ptr=node_ptr
    remove=buttons.row();remove.enabled=0<=state.vector_output_selected_index<len(state.vector_outputs)
    remove.operator('toon_vector.remove_vector_output',text='Remove',icon='REMOVE').node_ptr=node_ptr
    right=split.column();right.separator(factor=4.)
    up=right.operator('toon_vector.vector_outputs_list_move_item',text='',icon='TRIA_UP');up.button_type='UP';up.node_ptr=node_ptr
    down=right.operator('toon_vector.vector_outputs_list_move_item',text='',icon='TRIA_DOWN');down.button_type='DOWN';down.node_ptr=node_ptr
    index=state.vector_output_selected_index
    if not 0<=index<len(state.vector_outputs):return
    item=state.vector_outputs[index];layout.separator();box=layout.box();box.enabled=item.on
    for name in ('sub_path','file_type','grouping','output_path'):box.prop(item,name)
    box.separator();box.label(text='Output Categories')
    box.prop(item,'visible_lines_on');box.prop(item,'hidden_lines_on');box.separator();box.label(text='Output Edges')
    for name in vector_output.render_elements.CATEGORIES:box.prop(item,name+'_on',text=_CATEGORY_LABELS.get(name,name))
    box.separator();box.label(text='Output Line Set IDs');grid=box.grid_flow(columns=4,align=True)
    for index in range(8):grid.prop(item,'line_set_ids',text=str(index+1),index=index,toggle=True)
    layout.separator();layout.operator('toon_vector.export_vector',text='Export Vector Files',icon='EXPORT').node_ptr=node_ptr
    if state.vector_output_message:layout.label(text=state.vector_output_message,icon='ERROR' if state.vector_output_error else 'INFO')


class TOONVECTOR_PT_compositor_output(bpy.types.Panel):
    bl_idname='TOONVECTOR_PT_compositor_output';bl_label='Image Node Vector Output'
    bl_space_type='NODE_EDITOR';bl_region_type='UI';bl_category='二维描边'
    @classmethod
    def poll(cls,context):
        space=context.space_data
        return space.type=='NODE_EDITOR' and space.tree_type=='CompositorNodeTree' and space.edit_tree is not None and space.edit_tree.nodes.active is not None and space.edit_tree.nodes.active.bl_idname=='CompositorNodeImage'
    def draw(self,context):
        try:
            node=context.space_data.edit_tree.nodes.active;token=vector_scope.node_address(context.scene,node)
            state=vector_output.vector_state(context.scene,view_layer=context.view_layer,node_ptr=token)
            draw_outputs(self.layout,context,state,node_ptr=token,target_label='Image: '+node.name+' / '+context.view_layer.name)
        except Exception as error:self.layout.label(text=str(error),icon='ERROR')


CLASSES = (
    ToonVectorOutputItem,
    ToonVectorOutputState,
    TOONVECTOR_OT_new_vector_output,
    TOONVECTOR_OT_remove_vector_output,
    TOONVECTOR_OT_vector_outputs_list_move_item,
    TOONVECTOR_OT_export_vector,
    TOONVECTOR_UL_outputs,
    TOONVECTOR_PT_output,
    TOONVECTOR_PT_compositor_output,
)

# The C02 source names remain available as aliases for evidence and addon code.
VectorOutput = ToonVectorOutputItem
ViewLayerLineOutputs = ToonVectorOutputState
