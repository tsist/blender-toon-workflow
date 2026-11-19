# SPDX-License-Identifier: GPL-3.0-or-later
"""Presentation-only visible/hidden author switching; render gates stay intact."""
import bpy
from bpy.props import BoolProperty


def route_channel(route):
    name=route.get('native_route_name') or route.get('slot','')
    return 'VISIBLE' if name.startswith('v_') else 'HIDDEN' if name.startswith('h_') else 'BOTH'


def actual_channels(document,gid,actual,bindings=None):
    from . import edge_sources,ix_sources,source_bindings
    aliases={a['slot']:a['brush_id'] for a in document['request'].get('boundary_aliases',[]) if a['group']==gid}
    result=set()
    edge=edge_sources.records(document).get(gid)
    if edge:
        result.update(r['channel'] for r in edge['routes'] if aliases.get(r['slot'])==actual)
    ix=ix_sources.records(document).get(gid)
    if ix:
        result.update(ch for ch,r in ix['routes'].items() if ix['authors'].get(r['slot'],{}).get('actual')==actual)
    if not result:
        bindings=bindings or source_bindings.recover(document)
        result.update(route_channel(r) for r in bindings['routes'] if r['group_id']==gid and r['actual_brush_id']==actual)
    return result or {'BOTH'}


def update_tree(tree,context):
    channel='VISIBLE' if tree.show_visible_lines else 'HIDDEN'
    for node in tree.nodes:
        if node.bl_idname=='TOON_AuthorBrush':
            node.hide=node.author_channel not in ('BOTH',channel)
        for socket in node.inputs:
            if socket.bl_idname=='TOON_AuthorSocket' and socket.kind=='BRUSH':
                # Blender keeps linked sockets visible. Preserve every link;
                # collapse the inactive brush author and hide only free sockets.
                socket.hide=not socket.is_linked and socket.author_channel not in ('BOTH',channel)


class TOONAUTHOR_OT_show_visible_lines(bpy.types.Operator):
    bl_idname='toon_viewport.show_visible_lines'
    bl_label='切换作者可见性频道'
    bl_description='仅切换可见线或隐藏线的编辑控件，保留两通道的绘制与作者数据'
    bl_options={'REGISTER','UNDO'}
    show:BoolProperty(default=True)

    def execute(self,context):
        context.scene.toon_viewport_preview.show_visible_lines=self.show
        tree=context.scene.toon_author_files.author_tree
        if (tree is not None and tree.bl_idname=='TOON_AuthorTree' and tree.library is None and tree.users<=1
                and tree.get('owner')==context.scene.get('toon_author_graph_owner')):
            tree.show_visible_lines=self.show
        if context.screen:
            for area in context.screen.areas:area.tag_redraw()
        return {'FINISHED'}


def draw(layout,settings):
    row=layout.row(align=True);row.label(text='编辑频道')
    for show,label in ((True,'可见线 V'),(False,'隐藏线 H')):
        op=row.operator(TOONAUTHOR_OT_show_visible_lines.bl_idname,text=label,depress=settings.show_visible_lines==show)
        op.show=show


CLASSES=(TOONAUTHOR_OT_show_visible_lines,)
