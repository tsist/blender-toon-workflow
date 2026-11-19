# SPDX-License-Identifier: GPL-3.0-or-later
"""Native compositor outputs captured from one actual render, not a rerender."""
from pathlib import Path
import hashlib,uuid
import numpy as np
from .runtime_compat import capture_effect

def read(path,extent,*,is_data=False):
    import bpy
    im=bpy.data.images.load(str(path),check_existing=False)
    try:
        im.alpha_mode='CHANNEL_PACKED' if is_data else 'PREMUL'
        if tuple(im.size)!=tuple(extent):raise ValueError('Native capture extent changed')
        data=np.empty(extent[0]*extent[1]*4,np.float32);im.pixels.foreach_get(data)
        data=data.reshape(extent[1],extent[0],4)
        if not np.isfinite(data).all():raise ValueError('Native capture contains nonfinite values')
        return data
    finally:bpy.data.images.remove(im)

def capture(scene,layer,path,extent,*,background_objects=(),depth=False,vector=False):
    import bpy
    path=Path(path)
    if not path.is_absolute() or not path.parent.is_dir():raise ValueError('Native capture needs an absolute output path')
    objects=list(background_objects)
    if len(set(objects))!=len(objects) or any(scene.objects.get(o.name)!=o or o.type!='MESH' for o in objects):
        raise ValueError('Background capture requires explicit unique Scene Mesh IDs')
    if any(',' in o.name for o in objects):raise ValueError('Cryptomatte selection with comma-containing Object names needs an explicit hash adapter')
    flags=('use_pass_cryptomatte_object','pass_cryptomatte_depth','use_pass_cryptomatte_accurate','use_pass_z','use_pass_vector')
    previous={k:getattr(layer,k) for k in flags}
    old=dict(use=scene.render.use_compositing,
             device=scene.render.compositor_device,precision=scene.render.compositor_precision,
             filepath=scene.render.filepath)
    group=None
    folder=path.parent/('native-capture-'+uuid.uuid4().hex);folder.mkdir()
    layer.update()
    before_camera=[list(r) for r in scene.camera.matrix_world]
    try:
        layer.use_pass_cryptomatte_object=bool(objects)
        layer.pass_cryptomatte_depth=6;layer.use_pass_cryptomatte_accurate=True
        layer.use_pass_z=bool(depth);layer.use_pass_vector=bool(vector)
        group=bpy.data.node_groups.new('Toon same-render capture '+uuid.uuid4().hex,'CompositorNodeTree')
        group.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
        result=group.nodes.new('NodeGroupOutput');r=group.nodes.new('CompositorNodeRLayers');r.scene=scene;r.layer=layer.name
        group.links.new(r.outputs['Image'],result.inputs['Image'])
        f=group.nodes.new('CompositorNodeOutputFile');f.directory=str(folder);f.file_name='Pass'
        f.format.media_type='IMAGE';f.format.file_format='OPEN_EXR';f.format.color_mode='RGBA';f.format.color_depth='32';f.format.exr_codec='ZIP'
        def output(name,socket,kind='RGBA'):
            f.file_output_items.new(kind,name);group.links.new(socket,f.inputs[name])
        if objects:
            c=group.nodes.new('CompositorNodeCryptomatteV2');c.source='RENDER';c.scene=scene;c.layer_name=layer.name+'.CryptoObject'
            # The native matte_id field takes raw comma-separated IDs. Quotes
            # are literal name characters, not a CSV escape mechanism.
            c.matte_id=', '.join(o.name for o in objects)
            group.links.new(r.outputs['Image'],c.inputs['Image']);output('Background',c.outputs['Matte'],'FLOAT')
        if depth:output('Depth',r.outputs['Depth'],'FLOAT')
        if vector:output('Vector',r.outputs['Vector'])
        scene.render.use_compositing=True
        scene.render.compositor_device='CPU';scene.render.compositor_precision='FULL';scene.render.filepath=str(path)
        with capture_effect(scene, group):
            if bpy.ops.render.render(write_still=True,scene=scene.name,layer=layer.name)!={'FINISHED'}:raise RuntimeError('Native capture render failed')
        beauty=read(path,extent);aux={}
        for name in (['Background'] if objects else [])+(['Depth'] if depth else [])+(['Vector'] if vector else []):
            files=list(folder.glob('Pass'+name+'*.exr'))
            if len(files)!=1:raise ValueError('Expected exactly one native '+name+' output')
            aux[name]=read(files[0],extent,is_data=True)
        if not objects:aux['Background']=np.zeros_like(beauty)
        if [list(r) for r in scene.camera.matrix_world]!=before_camera:raise ValueError('Camera changed during native capture')
        metadata=dict(profile='toon-native-capture.v1',same_render=True,background_objects=sorted(o.name for o in objects),
            frame=scene.frame_current,subframe=scene.frame_subframe,extent=list(extent),
            filter_size=float(scene.render.filter_size),samples=int(scene.eevee.taa_render_samples),
            camera=scene.camera.name,camera_world=before_camera,
            files=[dict(file=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(folder.glob('*.exr'))])
        return dict(Beauty=beauty,**aux,metadata=metadata)
    finally:
        scene.render.use_compositing=old['use']
        scene.render.compositor_device=old['device'];scene.render.compositor_precision=old['precision'];scene.render.filepath=old['filepath']
        for k,v in previous.items():setattr(layer,k,v)
        if group is not None:bpy.data.node_groups.remove(group)
