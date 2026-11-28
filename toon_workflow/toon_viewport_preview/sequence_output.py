# SPDX-License-Identifier: GPL-3.0-or-later
"""One owned worker per frozen sequence; only the parent publishes complete frames."""
import copy
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import uuid

import bpy
from . import sequence_protocol as protocol

_jobs={}
_processes={}
_thread=threading.get_ident()

def _main():
    if threading.get_ident()!=_thread:raise RuntimeError('Sequence exports require their owning Blender thread')

def jobs():
    _main();return copy.deepcopy(list(_jobs.values()))

def _receipt(job):
    job['receipt_path']=str(Path(job['work_directory'])/'sequence.json')
    job['receipt_saved']=True
    try:protocol.atomic_json(job['receipt_path'],job);return True
    except (OSError,ValueError,TypeError) as error:
        job.update(receipt_saved=False,receipt_warning=str(error));return False

def _kill(token):
    process=_processes.get(token)
    if process is not None and process.poll() is None:
        try:process.kill()
        except OSError as error:_jobs[token]['termination_warning']=str(error)

def request(output_directory,prefix,frames,extension,context,*,elements=None,memory_budget_bytes=None):
    _main()
    from .camera import capture
    from . import addon,curve_editor,render_elements,render_output
    ordered=protocol.frames(frames);stem=protocol.prefix(prefix);suffix=protocol.extension(extension)
    directory=Path(bpy.path.abspath(str(output_directory)))
    if not directory.is_absolute() or not directory.is_dir():raise ValueError('请选择已有的序列输出目录')
    directory=directory.resolve()
    paths=[directory/protocol.frame_name(stem,frame,suffix) for frame in ordered]
    if any(path.exists() or path.is_symlink() for path in paths):raise FileExistsError('序列目标已存在，请更换目录或前缀')
    scene=context.scene;layer=context.view_layer
    if scene.library is not None:raise ValueError('请先使用本地场景工作副本')
    frame=capture(context)
    settings=scene.toon_viewport_preview
    if settings.style_mode=='PRESET':
        from . import addon
        plan=addon.effective_preset(scene)
        addon._map_amounts(scene,plan);addon._dash_patterns(scene,plan)
    if settings.curve_tree is not None:
        curve_editor.synchronize(scene)
        if settings.curve_error:raise ValueError('请先修复或放弃未应用的曲线修改')
    scene_key=(scene.as_pointer(),layer.as_pointer())
    normalized_paths={os.path.normcase(str(path)) for path in paths}
    for job in list(_jobs.values())+render_output.jobs():
        if job['status']!='QUEUED':continue
        if tuple(job['scene_key'])==scene_key:
            raise ValueError('此场景视图层已有待完成的渲染线图或序列')
        existing=set(job.get('normalized_paths',[]))
        if job.get('normalized_path'):existing.add(job['normalized_path'])
        if normalized_paths&existing:raise ValueError('已有任务正在写入相同的序列文件名')
    config=admission=None
    if elements is not None:
        if suffix!='.exr':raise ValueError('分层序列需要 EXR 格式')
        plan=addon._preset(scene) if settings.style_mode=='PRESET' else None
        config=render_elements.normalize(elements,style_mode=settings.style_mode,
            group_ids=[g['id'] for g in plan['groups']] if plan else ['__BASIC__'])
        admission=render_elements.admit(frame['width'],frame['height'],len(config['elements']),
            render_elements.DEFAULT_BUDGET if memory_budget_bytes is None else memory_budget_bytes,
            depth=any(e['type']=='DEPTH' for e in config['elements']))
    history_warnings=[]
    for token in list(_jobs):
        if len(_jobs)<20:break
        old=_jobs[token]
        if old['status']=='QUEUED':continue
        if not old.get('receipt_saved') and not _receipt(old):
            history_warnings.append(dict(id=old['id'],receipt_path=old.get('receipt_path'),
                warning=str(old.get('receipt_warning','回执保存失败'))[-1000:]))
        del _jobs[token]
    if len(_jobs)>=20:raise ValueError('等待完成的序列记录过多')
    token=uuid.uuid4().hex
    work=Path(tempfile.mkdtemp(prefix='.s-',dir=directory))
    root=Path(__file__).resolve().parent
    job=dict(id=token,sequence_id=token,status='QUEUED',mode='SEQUENCE',scene_key=scene_key,
        scene=scene.name,view_layer=layer.name,work_directory=str(work),output_directory=str(directory),
        prefix=stem,frames=ordered,extension=suffix,paths=list(map(str,paths)),
        normalized_paths=sorted(normalized_paths),published_frames=[],completed_count=0,total_count=len(ordered),
        stage='SETUP',worker_stage='LOADING',message='后台逐帧渲染：使用提交时的场景副本，可继续操作或取消')
    if history_warnings:job['history_warnings']=history_warnings
    if config is not None:job.update(element_config=config,element_config_sha256=render_elements.config_sha(config),element_admission=admission)
    _jobs[token]=job
    try:
        if not _receipt(job):raise OSError('无法保存序列记录：'+job['receipt_warning'])
        render_output._publish_preflight(work)
        current_path=bpy.data.filepath;current_frame=scene.frame_current;current_subframe=scene.frame_subframe
        if bpy.ops.wm.save_as_mainfile(filepath=str(work/'scene.blend'),copy=True,check_existing=False)!={'FINISHED'}:
            raise RuntimeError('无法建立序列场景副本')
        if bpy.data.filepath!=current_path or scene.frame_current!=current_frame or scene.frame_subframe!=current_subframe:
            raise RuntimeError('建立序列副本改变了当前文件或帧')
        package_files={p.relative_to(root).as_posix():protocol.digest(p) for p in root.rglob('*.py') if '__pycache__' not in p.parts}
        snapshot_sha=protocol.digest(work/'scene.blend')
        data=dict(scene=scene.name,view_layer=layer.name,source_engine=scene.render.engine,
            extension=suffix,package_root=str(root),package_files=package_files,snapshot_sha256=snapshot_sha,
            sequence_frames=ordered,sequence_id=token)
        if config is not None:data.update(element_config=config,element_config_sha256=job['element_config_sha256'],memory_budget_bytes=admission['budget_bytes'])
        protocol.atomic_json(work/'request.json',data)
        job.update(snapshot_sha256=snapshot_sha,package_files=package_files,
            original_main_path=current_path,submission_frame=current_frame,submission_subframe=current_subframe)
        args=[bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
              '--python',str(root/'render_worker.py'),'--',str(work/'request.json')]
        with (work/'stdout.log').open('wb') as out,(work/'stderr.log').open('wb') as err:
            process=subprocess.Popen(args,stdout=out,stderr=err,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        _processes[token]=process
        job.update(pid=process.pid,stage='RENDERING')
    except Exception as error:
        _kill(token)
        job.update(status='FAILED',stage='SETUP_FAILED',message='序列提交失败：'+str(error))
        _receipt(job);error.add_note('Sequence task directory: '+str(work));raise
    _receipt(job)
    return dict(id=token,status='QUEUED',work_directory=str(work),paths=list(map(str,paths)))

def cancel(token):
    _main();job=_jobs.get(token)
    if job is None or job['status']!='QUEUED':return False
    job.update(cancel_requested=True,stage='CANCELLING',message='正在取消序列；已发布的完整帧将保留')
    _kill(token);_receipt(job);return True

def _validate_frame(job,index):
    work=Path(job['work_directory']);marker=work/protocol.marker_name(index)
    if not marker.is_file() or marker.is_symlink():raise ValueError('Invalid sequence marker file')
    raw=protocol.read_json(marker)
    result=protocol.checked_frame_marker(raw,token=job['id'],index=index,frame=job['frames'][index])
    pixels=work/protocol.pixel_relative(index,job['extension'])
    expected=('camera_render_depsgraph_elements_exr.v1' if 'element_config' in job else
              'camera_render_depsgraph_line_'+job['extension'][1:]+'.v1')
    if (Path(result['path']).resolve()!=pixels.resolve() or result.get('contract')!=expected
            or result.get('evaluation')!='RENDER' or result.get('depsgraph_mode')!='RENDER'
            or result.get('mode')!='RENDER' or result.get('source_text_executed') is not False
            or result.get('scene')!=job['scene'] or result.get('view_layer')!=job['view_layer']
            or result.get('snapshot_sha256')!=job['snapshot_sha256']):
        raise ValueError('Completed frame source or RENDER identity changed')
    if not pixels.is_file() or pixels.is_symlink() or protocol.digest(pixels)!=result['sha256']:
        raise ValueError('Completed frame pixels changed')
    extent=result['output_extent']
    if not isinstance(extent,list) or len(extent)!=2 or any(type(v) is not int or v<1 for v in extent):
        raise ValueError('Invalid completed frame extent')
    if (type(result.get('width')) is not int or type(result.get('height')) is not int
            or [result['width'],result['height']]!=extent):
        raise ValueError('Completed frame dimensions do not match its extent')
    aspect=result.get('pixel_aspect')
    if (not isinstance(aspect,list) or len(aspect)!=2 or any(
            type(value) not in (int,float) or not math.isfinite(value) or value<=0 for value in aspect)):
        raise ValueError('Invalid completed frame pixel aspect')
    camera=result.get('camera_name')
    if not isinstance(camera,str) or not camera:raise ValueError('Invalid completed frame camera name')
    camera.encode('utf8')
    png=job['extension']=='.png'
    expected_format='PNG' if png else 'OPEN_EXR_MULTILAYER' if 'element_config' in job else 'OPEN_EXR'
    if (result.get('format','PNG' if png else None)!=expected_format
            or result.get('alpha_mode')!=('STRAIGHT' if png else 'PREMUL')
            or result.get('color_space')!=('sRGB' if png else 'scene_linear_Rec709')
            or result.get('source_color_space')!='scene_linear_Rec709'
            or type(result.get('bit_depth')) is not int or result['bit_depth']!=(8 if png else 32)
            or (not png and result.get('compression')!='ZIP')
            or (png and 'compression' in result)):
        raise ValueError('Invalid completed frame image summary')
    if job['extension']=='.png':
        with pixels.open('rb') as stream:header=stream.read(24)
        if header[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or list(struct.unpack('>II',header[16:24]))!=extent:
            raise ValueError('Completed PNG extent does not match its frame')
    elif 'element_config' in job:
        from . import render_elements
        if result.get('element_config_sha256')!=job['element_config_sha256']:raise ValueError('Completed frame element configuration changed')
        passes=[dict(id=e['id'],pass_name=e['pass_name'],label=e['label']) for e in job['element_config']['elements']]
        if result.get('element_passes')!=passes:raise ValueError('Completed frame element mapping changed')
        render_elements.validate_file(pixels,job['element_config'],*extent,result['pixel_aspect'])
    else:
        from .exr_header import inspect_exr
        inspected=inspect_exr(pixels,max_parts=1)
        if inspected['multipart'] or len(inspected['parts'])!=1:raise ValueError('Invalid single-frame EXR layout')
        part=inspected['parts'][0]
        if (part['data_window']!=[0,0,extent[0]-1,extent[1]-1]
                or part['display_window']!=[0,0,extent[0]-1,extent[1]-1] or not inspected['chunks_checked']):
            raise ValueError('Completed EXR extent or chunks do not match its frame')
    return result,protocol.digest(marker)

def _progress(value,job):
    """Return a small detached advisory subset, or ignore a malformed update."""
    stages={'LOADING','FRAME_START','CAPTURING','INTERSECTION_CAPTURE','CHAIN_SNAPSHOT','CHAIN_PHASE',
            'STYLING','DRAWING','ELEMENT','ELEMENT_DRAWING','PACKING_ELEMENTS','FRAME_COMPLETE','COMPLETE','FAILED'}
    if not isinstance(value,dict) or type(value.get('stage')) is not str or value['stage'] not in stages:return None
    total=len(job['frames']);result=dict(stage=value['stage'])
    if 'sequence_id' in value:
        if type(value['sequence_id']) is not str or value['sequence_id']!=job['id']:return None
        result['sequence_id']=value['sequence_id']
    for name,low,high in (('index',0,total-1),('frame',protocol.MIN_FRAME,protocol.MAX_FRAME),
                          ('total',total,total),('completed',0,total)):
        if name in value:
            item=value[name]
            if type(item) is not int or not low<=item<=high:return None
            result[name]=item
    if 'index' in result and 'frame' in result and job['frames'][result['index']]!=result['frame']:return None
    if 'element_id' in value:
        item=value['element_id']
        if type(item) is not str or not 1<=len(item)<=32:return None
        try:item.encode('utf8')
        except UnicodeError:return None
        result['element_id']=item
    return result

def poll():
    _main();finished=[]
    for token,process in list(_processes.items()):
        job=_jobs[token];work=Path(job['work_directory']);code=process.poll()
        if not job.get('cancel_requested') and not job.get('failure'):
            try:
                if (work/'progress.json').is_file():
                    try:
                        progress=_progress(protocol.read_json(work/'progress.json'),job)
                        if progress is not None:job['worker_progress']=progress;job['worker_stage']=progress['stage']
                    except (OSError,ValueError,TypeError):pass
                while job['completed_count']<len(job['frames']):
                    index=job['completed_count'];marker=work/protocol.marker_name(index)
                    if not marker.exists():break
                    if protocol.digest(work/'scene.blend')!=job['snapshot_sha256']:raise ValueError('Frozen scene snapshot changed')
                    try:result,marker_sha=_validate_frame(job,index)
                    except OSError:
                        if code is None:break
                        raise
                    path=job['paths'][index]
                    # The worker cannot publish and never receives this path.
                    os.link(work/protocol.pixel_relative(index,job['extension']),path)
                    # Record the completed commit before any optional receipt write.
                    # Large per-source style/geometry diagnostics live in the
                    # immutable frame marker, not in every UI status copy.
                    summary={key:result[key] for key in ('contract','format','width','height','output_extent',
                        'pixel_aspect','frame_subframe','camera_name','evaluation','snapshot_sha256','alpha_mode',
                        'color_space','bit_depth','compression','element_config_sha256','element_passes') if key in result}
                    summary.update(path=path,sha256=result['sha256'])
                    job['published_frames'].append(dict(index=index,frame=job['frames'][index],path=path,
                        sha256=result['sha256'],marker_path=str(marker),marker_sha256=marker_sha,result=summary))
                    job['completed_count']+=1
                    job['message']='已发布完整帧 '+str(job['completed_count'])+' / '+str(len(job['frames']))
                    _receipt(job)
                if code is not None:
                    if code!=0:
                        error=(work/'error.txt').read_text(encoding='utf8') if (work/'error.txt').is_file() else 'Blender worker exit '+str(code)
                        raise RuntimeError(error[-3000:])
                    final=protocol.read_json(work/'result.json')
                    expected=[dict(index=r['index'],frame=r['frame'],marker_sha256=r['marker_sha256']) for r in job['published_frames']]
                    final_frames=protocol.frames(final.get('frames'))
                    final_markers=final.get('markers')
                    if not isinstance(final_markers,list) or any(
                            not isinstance(row,dict) or set(row)!={'index','frame','marker_sha256'}
                            or type(row['index']) is not int or type(row['frame']) is not int
                            or not isinstance(row['marker_sha256'],str) for row in final_markers):
                        raise ValueError('Invalid final sequence marker identities')
                    if (final.get('schema')!=protocol.RESULT_SCHEMA or final.get('ok') is not True
                            or final.get('sequence_id')!=token or final_frames!=job['frames']
                            or final_markers!=expected or job['completed_count']!=len(job['frames'])):
                        raise ValueError('Worker did not complete the exact requested sequence')
                    diagnostics=final.get('progress_diagnostics',dict(advisory=True,write_errors=0,last_error=None))
                    if (not isinstance(diagnostics,dict) or diagnostics.get('advisory') is not True
                            or type(diagnostics.get('write_errors')) is not int or diagnostics['write_errors']<0
                            or not (diagnostics.get('last_error') is None or isinstance(diagnostics['last_error'],str))):
                        raise ValueError('Invalid worker advisory progress diagnostics')
                    job['worker_diagnostics']=diagnostics
                    job.update(status='COMPLETE',stage='COMPLETE',message='序列导出完成')
            except Exception as error:
                job.update(failure=str(error),stage='STOPPING_FAILED',message='序列导出失败：'+str(error));_kill(token)
                code=process.poll()
        if code is not None:
            _processes.pop(token);job['worker_exit_code']=code
            if job.get('cancel_requested'):job.update(status='CANCELLED',stage='CANCELLED',message='已取消序列，完整已发布帧已保留')
            elif job.get('failure'):job.update(status='FAILED',stage='FAILED')
            _receipt(job);finished.append(copy.deepcopy(job))
        elif job.get('cancel_requested') or job.get('failure'):_receipt(job)
    return finished

def cancel_all():
    _main()
    for token in list(_processes):cancel(token)
    for token,process in list(_processes.items()):
        if process.poll() is None:
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                _kill(token)
                try:process.wait(timeout=1.)
                except (OSError,subprocess.TimeoutExpired) as error:_jobs[token]['termination_warning']=str(error)
    return poll()
