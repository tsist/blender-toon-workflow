# SPDX-License-Identifier: GPL-3.0-or-later
"""Production GPU brush/stroke thumbnails in an isolated owned preview scene."""
from pathlib import Path
import bpy
import numpy as np
from mathutils import Matrix

_pending=None


class TOONVIEWPORT_PreviewEngine(bpy.types.RenderEngine):
    bl_idname='TOON_AUTHOR_PREVIEW';bl_label='自主笔刷笔触预览';bl_use_gpu_context=True
    def render(self,depsgraph):
        global _pending
        request=_pending
        if request is None:return
        try:
            from .brush_engine import preview_fixture
            from .drawing import Renderer
            from .output import save_png_new
            width,height=request['width'],request['height']
            points=np.column_stack((np.linspace(20,width-20,81),height*.5+height*.12*np.sin(np.linspace(0,2*np.pi,81))))
            geometry,layer=preview_fixture(points,request['author'],width=width,height=height,line_width=request['line_width'])
            renderer=Renderer()
            for _ in renderer.render_steps(geometry,Matrix.Identity(4),layer['projected'],None,width,height,1.,layer['supports'],layers=[layer]):pass
            pixels=np.asarray(renderer.color.read(),np.float32).reshape(height,width,4).copy()
            request['result']=save_png_new(request['path'],pixels)
            del renderer
        except Exception as error:request['error']=error
        finally:
            result=self.begin_result(0,0,1,1);result.layers[0].passes['Combined'].rect=[[0,0,0,0]];self.end_result(result)


def render(scene,actual,path,*,width=256,height=128,line_width=8.):
    from . import file_authoring as F,brush_effects as B
    from .source_bindings import recover
    global _pending
    if _pending is not None:raise ValueError('A brush preview is already rendering')
    wrapped=F.read_scene(scene);bindings=recover(wrapped['document'])
    if actual not in bindings['actuals']:raise ValueError('Unknown preview brush')
    path=Path(path)
    if not path.is_absolute() or path.exists() or path.suffix.lower()!='.png' or not path.parent.is_dir():raise ValueError('Choose a new absolute PNG thumbnail path')
    if any(type(v) is not int or not 32<=v<=1024 for v in (width,height)):raise ValueError('Invalid thumbnail dimensions')
    from .addon import effective_preset
    effective=effective_preset(scene)
    author=B.records(effective['raw'],bindings['actuals']).get(actual,B.defaults())
    owned=bpy.data.scenes.new('.toon_brush_preview');camera_data=bpy.data.cameras.new('.toon_brush_preview')
    camera=bpy.data.objects.new('.toon_brush_preview',camera_data);owned.collection.objects.link(camera);owned.camera=camera
    request=dict(author=author,path=path,width=width,height=height,line_width=line_width)
    try:
        owned.render.engine=TOONVIEWPORT_PreviewEngine.bl_idname;owned.render.resolution_x=1;owned.render.resolution_y=1
        _pending=request;bpy.ops.render.render(scene=owned.name)
        if 'error' in request:raise request['error']
        if 'result' not in request:raise RuntimeError('Brush preview render callback did not complete')
        image=bpy.data.images.load(str(path),check_existing=False)
        image.pack();image['toon_preview_actual']=actual
        return image
    finally:
        _pending=None;bpy.data.objects.remove(camera,do_unlink=True);bpy.data.cameras.remove(camera_data);bpy.data.scenes.remove(owned)


def register():bpy.utils.register_class(TOONVIEWPORT_PreviewEngine)
def unregister():bpy.utils.unregister_class(TOONVIEWPORT_PreviewEngine)
