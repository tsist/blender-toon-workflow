# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent workspace and selected-face geometry helpers."""
import bpy


def selection_shape(context,*,depth=.05,label='Selection Shape'):
    """Create an editable shape from selected faces, leaving the input intact."""
    obj=context.active_object
    if obj is None or obj.type!='MESH':raise ValueError('请选择本地网格对象及需要的面')
    if obj.mode=='EDIT':obj.update_from_editmode()
    faces=[p for p in obj.data.polygons if p.select]
    if not faces:raise ValueError('至少选择一个面')
    used=sorted({v for p in faces for v in p.vertices});indices={v:i for i,v in enumerate(used)}
    vertices=[tuple(obj.data.vertices[i].co) for i in used]
    polygons=[[indices[v] for v in p.vertices] for p in faces]
    mesh=bpy.data.meshes.new(label);mesh.from_pydata(vertices,[],polygons);mesh.update()
    shape=bpy.data.objects.new(label,mesh);context.collection.objects.link(shape);shape.matrix_world=obj.matrix_world.copy()
    tree=bpy.data.node_groups.new(label+' · Editable Extrusion','GeometryNodeTree')
    tree.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    source=tree.nodes.new('NodeGroupInput');output=tree.nodes.new('NodeGroupOutput')
    extrude=tree.nodes.new('GeometryNodeExtrudeMesh');extrude.mode='FACES'
    extrude.inputs['Offset Scale'].default_value=depth;extrude.inputs['Individual'].default_value=False
    tree.links.new(source.outputs['Geometry'],extrude.inputs['Mesh']);tree.links.new(extrude.outputs['Mesh'],output.inputs['Geometry'])
    modifier=shape.modifiers.new('Selected Shape','NODES');modifier.node_group=tree
    shape['toon_selection_shape_origin']='independent-selected-faces.v1';shape['toon_selection_shape_source']=obj.name
    return shape


def selection_generator(context,*,mode='FACES',label='Selection Generator',threshold=.5,
                        subdivide=1,render_subdivide=2,merge_distance=.0001,offset=.001,depth=.01,
                        border_only=False,border_radius=.004,image=None,ray_target=None,
                        ray_direction=(0,0,-1),ray_length=100.,material=None,border_material=None):
    """Autonomous field-driven mask geometry, with editable Geometry Nodes.

    Sources are selected faces, spatial noise, UV image alpha or ray hits. This
    is an independent implementation, not the vendor sample's internal graph.
    """
    obj=context.active_object
    if obj is None or obj.type!='MESH':raise ValueError('请选择网格输入')
    if mode not in ('FACES','NOISE','IMAGE','RAYCAST'):raise ValueError('Unknown selection field')
    if mode=='IMAGE' and image is None:raise ValueError('Image mode requires a loaded image')
    if mode=='RAYCAST' and ray_target is None:raise ValueError('Raycast mode requires an explicit target object')
    if obj.mode=='EDIT':obj.update_from_editmode()
    mesh=obj.data.copy();mesh.name=label
    attribute=mesh.attributes.get('toon_selection') or mesh.attributes.new('toon_selection','BOOLEAN','FACE')
    for polygon,value in zip(mesh.polygons,attribute.data):value.value=polygon.select
    shape=bpy.data.objects.new(label,mesh);context.collection.objects.link(shape);shape.matrix_world=obj.matrix_world.copy()
    tree=bpy.data.node_groups.new(label+' · Field Mask','GeometryNodeTree')
    def socket(name,kind,value=None,in_out='INPUT'):
        s=tree.interface.new_socket(name=name,in_out=in_out,socket_type=kind)
        if value is not None:s.default_value=value
        return s
    socket('Geometry','NodeSocketGeometry');socket('Geometry','NodeSocketGeometry',in_out='OUTPUT')
    for name,value in (('Viewport Subdivision',subdivide),('Render Subdivision',render_subdivide)):socket(name,'NodeSocketInt',value)
    for name,value in (('Threshold',threshold),('Merge Distance',merge_distance),('Normal Offset',offset),('Depth',depth),('Border Radius',border_radius)):socket(name,'NodeSocketFloat',value)
    socket('Border Only','NodeSocketBool',border_only);socket('Material','NodeSocketMaterial',material);socket('Border Material','NodeSocketMaterial',border_material)
    source=tree.nodes.new('NodeGroupInput');output=tree.nodes.new('NodeGroupOutput')
    link=tree.links.new
    viewport=tree.nodes.new('GeometryNodeIsViewport');level=tree.nodes.new('GeometryNodeSwitch');level.input_type='INT'
    link(viewport.outputs[0],level.inputs['Switch']);link(source.outputs['Render Subdivision'],level.inputs['False']);link(source.outputs['Viewport Subdivision'],level.inputs['True'])
    subdiv=tree.nodes.new('GeometryNodeSubdivideMesh');link(source.outputs['Geometry'],subdiv.inputs['Mesh']);link(level.outputs[0],subdiv.inputs['Level'])
    position=tree.nodes.new('GeometryNodeInputPosition')
    if mode=='FACES':
        field=tree.nodes.new('GeometryNodeInputNamedAttribute');field.data_type='BOOLEAN';field.inputs['Name'].default_value='toon_selection';selected=field.outputs['Attribute']
    elif mode in ('NOISE','IMAGE'):
        compare=tree.nodes.new('FunctionNodeCompare');compare.data_type='FLOAT';compare.operation='GREATER_THAN'
        link(source.outputs['Threshold'],compare.inputs['B'])
        if mode=='NOISE':
            noise=tree.nodes.new('ShaderNodeTexNoise');noise.inputs['Scale'].default_value=3.;link(position.outputs[0],noise.inputs['Vector']);link(noise.outputs['Fac'],compare.inputs['A'])
        else:
            socket('Selection Image','NodeSocketImage',image)
            uv=tree.nodes.new('GeometryNodeInputNamedAttribute');uv.data_type='FLOAT_VECTOR';uv.inputs['Name'].default_value=mesh.uv_layers.active.name if mesh.uv_layers.active else 'UVMap'
            texture=tree.nodes.new('GeometryNodeImageTexture');link(source.outputs['Selection Image'],texture.inputs['Image']);link(uv.outputs['Attribute'],texture.inputs['Vector']);link(texture.outputs['Alpha'],compare.inputs['A'])
        selected=compare.outputs['Result']
    else:
        socket('Ray Target','NodeSocketObject',ray_target)
        info=tree.nodes.new('GeometryNodeObjectInfo');info.transform_space='RELATIVE';link(source.outputs['Ray Target'],info.inputs['Object'])
        ray=tree.nodes.new('GeometryNodeRaycast');link(info.outputs['Geometry'],ray.inputs['Target Geometry']);link(position.outputs[0],ray.inputs['Source Position'])
        ray.inputs['Ray Direction'].default_value=ray_direction;ray.inputs['Ray Length'].default_value=ray_length;selected=ray.outputs['Is Hit']
    separate=tree.nodes.new('GeometryNodeSeparateGeometry');separate.domain='FACE';link(subdiv.outputs['Mesh'],separate.inputs['Geometry']);link(selected,separate.inputs['Selection'])
    merge=tree.nodes.new('GeometryNodeMergeByDistance');link(separate.outputs['Selection'],merge.inputs['Geometry']);link(source.outputs['Merge Distance'],merge.inputs['Distance'])
    normal=tree.nodes.new('GeometryNodeInputNormal');scale=tree.nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';link(normal.outputs[0],scale.inputs[0]);link(source.outputs['Normal Offset'],scale.inputs['Scale'])
    shift=tree.nodes.new('GeometryNodeSetPosition');link(merge.outputs['Geometry'],shift.inputs['Geometry']);link(scale.outputs['Vector'],shift.inputs['Offset'])
    extrude=tree.nodes.new('GeometryNodeExtrudeMesh');extrude.mode='FACES';extrude.inputs['Individual'].default_value=False
    link(shift.outputs['Geometry'],extrude.inputs['Mesh']);link(source.outputs['Depth'],extrude.inputs['Offset Scale'])
    surface=tree.nodes.new('GeometryNodeSetMaterial');link(extrude.outputs['Mesh'],surface.inputs['Geometry']);link(source.outputs['Material'],surface.inputs['Material'])
    neighbors=tree.nodes.new('GeometryNodeInputMeshEdgeNeighbors');boundary=tree.nodes.new('FunctionNodeCompare');boundary.data_type='INT';boundary.operation='EQUAL'
    # Integer Compare sockets have the same display labels as float sockets.
    ints=[s for s in boundary.inputs if s.type=='INT'];link(neighbors.outputs['Face Count'],ints[0]);ints[1].default_value=1
    curves=tree.nodes.new('GeometryNodeMeshToCurve');link(shift.outputs['Geometry'],curves.inputs['Mesh']);link(boundary.outputs['Result'],curves.inputs['Selection'])
    circle=tree.nodes.new('GeometryNodeCurvePrimitiveCircle');circle.inputs['Resolution'].default_value=6;link(source.outputs['Border Radius'],circle.inputs['Radius'])
    tube=tree.nodes.new('GeometryNodeCurveToMesh');link(curves.outputs['Curve'],tube.inputs['Curve']);link(circle.outputs['Curve'],tube.inputs['Profile Curve'])
    border=tree.nodes.new('GeometryNodeSetMaterial');link(tube.outputs['Mesh'],border.inputs['Geometry']);link(source.outputs['Border Material'],border.inputs['Material'])
    switch=tree.nodes.new('GeometryNodeSwitch');switch.input_type='GEOMETRY';link(source.outputs['Border Only'],switch.inputs['Switch']);link(surface.outputs['Geometry'],switch.inputs['False']);link(border.outputs['Geometry'],switch.inputs['True']);link(switch.outputs[0],output.inputs['Geometry'])
    modifier=shape.modifiers.new('Autonomous Selection Field','NODES');modifier.node_group=tree
    shape['toon_selection_shape_origin']='independent-field-mask.v1';shape['toon_selection_shape_mode']=mode
    return shape


def workspace(context,*,name='自主线条'):
    if context.window is None:raise ValueError('Workspace模板需要可见Blender窗口')
    previous=context.workspace;before={w.as_pointer() for w in bpy.data.workspaces}
    bpy.ops.workspace.duplicate()
    created=[w for w in bpy.data.workspaces if w.as_pointer() not in before]
    if len(created)!=1:raise RuntimeError('Workspace duplication did not create a separate workspace')
    result=created[0];context.window.workspace=result
    result.name=name
    for area in result.screens[0].areas:
        if area.type=='VIEW_3D':
            area.spaces.active.show_region_ui=True
            area.spaces.active.shading.type='SOLID'
        elif area.type=='PROPERTIES':area.spaces.active.context='RENDER'
    result['toon_workspace_origin']='independent-line-authoring.v1'
    return result


def select_group(scene,gid,*,brush='brush'):
    from . import addon
    plan=addon._preset(scene)
    group=next((g for g in plan['groups'] if g['id']==gid),None)
    if group is None or brush not in group['brushes']:raise ValueError('Unknown author group or brush slot')
    settings=scene.toon_viewport_preview;settings.curve_group=gid;settings.curve_slot=brush
    return dict(group=gid,brush=brush)
