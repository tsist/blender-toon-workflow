# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated Blender worker. All files written here belong to one private job."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def main(request_path):
    import bpy
    import numpy as np
    sys.dont_write_bytecode=True
    request_path=Path(request_path).resolve()
    work=request_path.parent
    request=json.loads(request_path.read_text(encoding='utf8'))
    snapshot=work/'scene.blend'
    root=Path(request['package_root']).resolve()
    report={};last_progress=0.
    callback_count=0
    progress_diagnostics=dict(advisory=True,write_errors=0,last_error=None)
    sequence_index=None;sequence_frame=None
    destination=work

    def write(name,value):
        tmp=work/(name+'.tmp')
        tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
        os.replace(tmp,work/name)

    def progress(stage,**extra):
        nonlocal last_progress
        now=time.perf_counter()
        if now-last_progress>=.2 or stage in {'LOADING','CAPTURING','COMPLETE','FAILED','FRAME_COMPLETE'}:
            value=dict(stage=stage,**extra)
            if sequence_index is not None:
                value.update(sequence_id=request['sequence_id'],index=sequence_index,frame=sequence_frame,total=len(sequence_frames))
            try:write('progress.json',value)
            except OSError as error:
                # Progress is advisory. A host/file-sharing failure here must
                # not fail a complete render or weaken pixel/marker commits.
                progress_diagnostics['write_errors']+=1
                progress_diagnostics['last_error']=str(error)[-1000:]
            last_progress=now

    if digest(snapshot)!=request['snapshot_sha256']:
        raise ValueError('Scene snapshot changed before the worker opened it')
    for name,expected in request['package_files'].items():
        path=root/name
        if not path.resolve().is_relative_to(root) or digest(path)!=expected:
            raise ValueError('Worker package changed: '+name)
    spec=importlib.util.spec_from_file_location('toon_render_package',root/'__init__.py',submodule_search_locations=[str(root)])
    package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package
    spec.loader.exec_module(package);package.register()
    progress('LOADING')
    bpy.ops.wm.open_mainfile(filepath=str(snapshot),use_scripts=False)
    scene=bpy.data.scenes[request['scene']]
    layer=scene.view_layers[request['view_layer']]
    from toon_render_package import addon,camera,render_geometry
    from toon_render_package.drawing import Renderer
    from toon_render_package.geometry import project_segments
    from toon_render_package.strokes import build_layers_steps
    from toon_render_package.output import save_image_new
    from toon_render_package import render_elements
    sequence_frames=None
    if 'sequence_frames' in request or 'sequence_id' in request:
        from toon_render_package import sequence_protocol
        sequence_frames=sequence_protocol.frames(request['sequence_frames'])
        sequence_protocol.sequence_id(request['sequence_id'])
        (work/'frames').mkdir(exist_ok=False)
    extension=request['extension']
    if extension not in {'.png','.exr'}:raise ValueError('Unsupported line image format')
    element_config=request.get('element_config');element_buffers={}
    if element_config is not None:
        if extension!='.exr' or render_elements.config_sha(element_config)!=request['element_config_sha256']:
            raise ValueError('Invalid frozen element request')

    class TOON_ISOLATED_RENDER(bpy.types.RenderEngine):
        bl_idname='TOON_ISOLATED_RENDER'
        bl_label='Isolated Toon Line Worker'
        bl_use_preview=False
        bl_use_postprocess=False
        bl_use_gpu_context=True

        def render(self,depsgraph):
            nonlocal callback_count
            renderer=None
            try:
                if depsgraph.mode!='RENDER':raise ValueError('Expected RENDER depsgraph')
                if depsgraph.view_layer.name!=layer.name:
                    raise ValueError('Render callback evaluated a different requested view layer')
                callback_count+=1
                if callback_count!=1:raise ValueError('Repeated requested view-layer render callback')
                progress('CAPTURING')
                geometry=render_geometry.capture(depsgraph)
                frame=camera.capture_render(depsgraph.scene_eval,depsgraph,camera=self.camera_override)
                settings=scene.toon_viewport_preview
                plan=addon.effective_preset(scene,evaluated_scene=depsgraph.scene_eval) if settings.style_mode=='PRESET' else None
                map_amounts=addon._map_amounts(scene,plan,evaluated_scene=depsgraph.scene_eval) if plan is not None else {}
                dash_patterns=addon._dash_patterns(scene,plan) if plan is not None else {}
                style=None;layers=None;w,h=frame['width'],frame['height']
                admission=None
                if element_config is not None:
                    admission=render_elements.admit(w,h,len(element_config['elements']),request['memory_budget_bytes'],
                        depth=any(e['type']=='DEPTH' for e in element_config['elements']))
                if plan is not None:
                    chain_phase=None
                    chain_inputs=addon._chain_inputs(scene,plan)
                    if chain_inputs is not None:
                        from toon_render_package import chain_snapshot,chain_compute
                        bindings,controls=chain_inputs
                        if (settings.intersection_enabled
                                and any(row['intersection_enabled'] for row in controls['brushes'].values())):
                            from toon_render_package.intersection_strokes import _plan as intersection_plan
                            if intersection_plan(plan) is not None and not hasattr(geometry,'intersection_sources'):
                                from toon_render_package.intersection_sources import capture_steps
                                intersection_capture=capture_steps(geometry.meshes)
                                try:
                                    while True:
                                        try:value=next(intersection_capture)
                                        except StopIteration as done:
                                            geometry.intersection_sources=done.value
                                            break
                                        progress('INTERSECTION_CAPTURE',detail=value)
                                finally:
                                    intersection_capture.close()
                        snapshot_steps=chain_snapshot.snapshot_steps(geometry,plan,frame['view'],frame['mvp'],frame['perspective'],w,h,1,settings,bindings,controls)
                        while True:
                            try:detail=next(snapshot_steps);progress('CHAIN_SNAPSHOT',detail=detail)
                            except StopIteration as done:chain_payload=done.value;break
                        if addon.dash_controls.needs_metric(dash_patterns):chain_payload['include_dash_metric']=True
                        chain_phase=chain_compute.compute_snapshot(chain_payload,lambda:progress('CHAIN_PHASE'))
                    steps=build_layers_steps(geometry,plan,frame['view'],frame['mvp'],frame['perspective'],w,h,1,settings,
                                            include_provenance=element_config is not None,chain_phase=chain_phase,
                                             map_amounts=map_amounts,dash_patterns=dash_patterns)
                    while True:
                        try:value=next(steps);progress('STYLING',detail=value)
                        except StopIteration as done:layers,style=done.value;break
                    projected=np.empty((0,2,3),np.float32);supports=np.empty((0,2),np.float32);world=None
                else:
                    segments,supports=geometry.segment_records(frame['view'],frame['perspective'],settings)
                    projected,indices,u=project_segments(segments,frame['mvp'],w,h,with_parameters=True)
                    world=segments[indices,0,None,:]+u[:,:,None]*(segments[indices,1]-segments[indices,0])[:,None,:]
                    supports=supports[indices]
                    if element_config is not None:layers=render_elements.basic_layers(geometry,frame,settings)
                renderer=Renderer()
                combined=render_elements.combined_layers(layers) if element_config is not None else layers
                for value in renderer.render_steps(geometry,frame['mvp'],projected,settings,w,h,1,supports,world=world,layers=combined):
                    progress('DRAWING',detail=value)
                pixels=np.asarray(renderer.color.read(),np.float32).reshape(h,w,4)
                pixels=camera.extract_pixels(frame,pixels)
                from toon_render_package import output_scope
                pixels=output_scope.render_background(scene,layer.name,pixels)
                if element_config is None:
                    result=save_image_new(destination/('pixels'+extension),pixels,pixel_aspect=frame['metadata']['pixel_aspect'])
                else:
                    element_buffers['Combined']=pixels.copy()
                    for index,element in enumerate(element_config['elements']):
                        progress('ELEMENT',element_id=element['id'],index=index+1,total=len(element_config['elements']))
                        selected=render_elements.select_layers(layers,element)
                        from toon_render_package import depth_output
                        for value in renderer.render_steps(geometry,frame['mvp'],projected,settings,w,h,1,supports,world=world,layers=selected,
                                                           depth=depth_output.parameters(element,frame)):
                            progress('ELEMENT_DRAWING',element_id=element['id'],detail=value)
                        value=camera.extract_pixels(frame,np.asarray(renderer.color.read(),np.float32).reshape(h,w,4))
                        element_buffers[element['pass_name']]=render_elements.with_background(value,element['background']).copy()
                    result=dict(element_admission=admission,
                        element_config_sha256=request['element_config_sha256'],
                        element_passes=[dict(id=e['id'],pass_name=e['pass_name'],label=e['label']) for e in element_config['elements']],
                        classification_profile=(render_elements.PROFILE if plan else render_elements.BASIC_PROFILE),
                        native_category_equivalent=False,depth_implemented=any(e['type']=='DEPTH' for e in element_config['elements']))
                    if result['depth_implemented']:
                        result.update(depth_profile=depth_output.PROFILE,native_depth_equivalent=False,
                            depth_background='canonical premultiplied line-over-straight-linear-author-background',
                            depth_precision='FLOAT32_GPU_RGBA')
                result.update(frame['metadata'],contract=('camera_render_depsgraph_elements_exr.v1' if element_config is not None else
                              'camera_render_depsgraph_line_'+extension[1:]+'.v1'),mode='RENDER',
                    scene=scene.name,view_layer=layer.name,triangles=geometry.triangle_count,source_names=geometry.names,
                    geometry_report=geometry.capture_report,style_report=style,snapshot_sha256=request['snapshot_sha256'],
                    parameter_animation=addon.parameter_animation.receipt(scene,evaluated_scene=depsgraph.scene_eval),
                    source_engine=request['source_engine'],evaluation_engine=self.bl_idname,
                    settings_time='SUBMISSION_SNAPSHOT',source_text_executed=False,
                    evaluated_view_layer=depsgraph.view_layer.name,source_layer_enabled_flags=layer_flags_before,
                    render_callback_count=callback_count)
                report.update(ok=True,result=result)
                # Only a tiny private Render Result is needed. The line PNG is
                # already resolved above; no compositor or beauty pass is run.
                tiny=self.begin_result(0,0,min(w,4),min(h,4))
                tiny.layers[0].passes['Combined'].rect=np.zeros((min(w,4)*min(h,4),4),np.float32)
                self.end_result(tiny)
            except Exception:
                report.update(ok=False,error=traceback.format_exc())
            finally:
                renderer=None

    bpy.utils.register_class(TOON_ISOLATED_RENDER)
    old_engine=scene.render.engine
    layer_flags_before={candidate.name:bool(candidate.use) for candidate in scene.view_layers}
    try:
        scene.render.engine=TOON_ISOLATED_RENDER.bl_idname
        # These are private-copy render-pipeline controls, not geometry flags.
        scene.render.use_compositing=False
        scene.render.use_sequencer=False
        # Blender can call a custom engine for every enabled layer even with
        # the operator's layer name. This private snapshot explicitly owns
        # exactly the requested layer for both single images and sequences.
        for candidate in scene.view_layers:candidate.use=(candidate==layer)
        markers=[]
        # The single-image branch uses the original destination, frame and
        # contract. Sequences open one snapshot, then run native frame_set and
        # the same real RENDER callback for each requested integer frame.
        for index,target_frame in enumerate(sequence_frames if sequence_frames is not None else (None,)):
            callback_count=0
            if sequence_frames is not None:
                sequence_index=index;sequence_frame=target_frame
                destination=work/'frames'/format(index,'06d')
                destination.mkdir(exist_ok=False)
                report.clear();element_buffers.clear()
                scene.frame_set(target_frame,subframe=0.0)
                progress('FRAME_START')
            result=bpy.ops.render.render(write_still=False,scene=scene.name,layer=layer.name)
            if result!={'FINISHED'} or not report.get('ok'):
                raise RuntimeError(report.get('error','Render callback did not complete'))
            if element_config is not None:
                from toon_render_package.multilayer_output import save_multilayer_new
                progress('PACKING_ELEMENTS')
                packed=save_multilayer_new(destination/'pixels.exr',element_buffers,pixel_aspect=report['result']['pixel_aspect'])
                packed.pop('contract',None)
                packed['output_view_layer']=packed.pop('view_layer')
                report['result'].update(packed)
                render_elements.validate_file(destination/'pixels.exr',element_config,
                    report['result']['output_extent'][0],report['result']['output_extent'][1],report['result']['pixel_aspect'])
            if sequence_frames is not None:
                marker=work/sequence_protocol.marker_name(index)
                value=dict(schema=sequence_protocol.FRAME_SCHEMA,ok=True,sequence_id=request['sequence_id'],
                    index=index,frame=target_frame,result=report['result'],progress_diagnostics=dict(progress_diagnostics))
                sequence_protocol.atomic_json(marker,value,create_only=True)
                markers.append(dict(index=index,frame=target_frame,marker_sha256=digest(marker)))
                progress('FRAME_COMPLETE',completed=index+1)
                element_buffers.clear();report.clear()
            else:
                progress('COMPLETE')
                report['progress_diagnostics']=dict(progress_diagnostics)
                write('result.json',report)
        if sequence_frames is not None:
            progress('COMPLETE')
            sequence_protocol.atomic_json(work/'result.json',dict(schema=sequence_protocol.RESULT_SCHEMA,
                ok=True,sequence_id=request['sequence_id'],frames=sequence_frames,markers=markers,
                progress_diagnostics=dict(progress_diagnostics)),create_only=True)
    finally:
        scene.render.engine=old_engine
        bpy.utils.unregister_class(TOON_ISOLATED_RENDER)
        package.unregister()


if __name__=='__main__':
    request=Path(sys.argv[sys.argv.index('--')+1])
    try:main(request)
    except Exception:
        (request.parent/'error.txt').write_text(traceback.format_exc(),encoding='utf8')
        raise
