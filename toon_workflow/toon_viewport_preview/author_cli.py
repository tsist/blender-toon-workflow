# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON public CLI for Blender --python-expr or embedding; no private RenderApp."""
def execute(request,context=None):
    import bpy
    from . import file_authoring as F,output_scope as O,preset_catalog as P,author_helpers as H
    context=context or bpy.context
    if type(request) is not dict or type(request.get('action')) is not str:raise ValueError('Expected JSON action')
    action=request['action'];scene=context.scene
    if action=='STATE':return F.state(scene)
    if action=='GRAPH_NEW':
        from . import author_graph
        return dict(tree=author_graph.create(scene).name)
    if action=='GRAPH_APPLY':
        from . import author_graph
        return author_graph.apply(scene)
    if action=='PREFERENCES':
        from . import runtime_preferences
        return runtime_preferences.configuration(scene)
    if action=='CLEANUP':return F.cleanup_references(scene,remove_missing=request.get('remove_missing',True),match_names=request.get('match_names',False),replacements=request.get('replacements'))
    if action=='NEW':return F.new_scene_style(scene,label=request.get('label','自主线条'))
    if action=='IMPORT':return F.import_style(scene,request['path'],mode=request.get('mode','REPLACE'),conflict=request.get('conflict','ERROR'),objects=request.get('objects'),materials=request.get('materials'),collections=request.get('collections'))
    if action=='EXPORT':return F.export_style(scene,request['path'])
    if action=='OVERRIDE':return O.set_scope(scene,request['values'],view_layer=request.get('view_layer'))
    if action=='REMOVE_OVERRIDE':return O.remove_scope(scene,view_layer=request.get('view_layer'))
    if action=='TOGGLE_OVERRIDE':return O.toggle_bool(scene,request['property'],view_layer=request.get('view_layer'),group=request.get('group'))
    if action=='OUTPUT':return O.request(request['path'],context,elements=request.get('elements'),frames=request.get('frames'),keyed_only=request.get('keyed_only',False),prefix=request.get('prefix','line'),extension=request.get('extension','.png'))
    if action=='PRESET':return P.apply(scene,request['actual'],request['preset'])
    if action=='PREVIEW':
        from . import brush_preview
        image=brush_preview.render(scene,request['actual'],request['path'],width=request.get('width',256),height=request.get('height',128),line_width=request.get('line_width',8.))
        return dict(image=image.name,path=request['path'])
    if action=='SHAPE':
        obj=H.selection_generator(context,mode=request.get('mode','FACES'),depth=request.get('depth',.01),label=request.get('label','Selection Shape'),
            image=bpy.data.images.get(request.get('image','')),ray_target=bpy.data.objects.get(request.get('ray_target','')),
            threshold=request.get('threshold',.5),border_only=request.get('border_only',False),subdivide=request.get('subdivide',1),render_subdivide=request.get('render_subdivide',2),
            merge_distance=request.get('merge_distance',.0001),offset=request.get('offset',.001),border_radius=request.get('border_radius',.004))
        return dict(object=obj.name,vertices=len(obj.data.vertices),faces=len(obj.data.polygons))
    if action=='RESTORE_OUTPUTS':return O.restore_outputs(scene)
    if action=='SELECT':return H.select_group(scene,request['group'],brush=request.get('brush','brush'))
    raise ValueError('Unknown public author action: '+action)
