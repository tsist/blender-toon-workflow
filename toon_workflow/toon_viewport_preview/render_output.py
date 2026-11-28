# SPDX-License-Identifier: GPL-3.0-or-later
"""Own isolated render workers and publish verified complete line images."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import bpy

_jobs={}
_processes={}


def _digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def jobs():return copy.deepcopy(list(_jobs.values()))


def _receipt(job):
    """A terminal RAM-history eviction must not erase the only job record."""
    work=Path(job['work_directory']);job['receipt_path']=str(work/'job.json')
    job['receipt_saved']=True
    try:
        temporary=work/'job.json.tmp'
        temporary.write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf8')
        os.replace(temporary,work/'job.json')
        return True
    except OSError as error:
        job['receipt_saved']=False;job['receipt_warning']=str(error)
        return False


def _publish_preflight(work):
    source=work/'publish-probe-source';target=work/'publish-probe-link';owned=[]
    try:
        with source.open('xb') as stream:owned.append(source);stream.write(b'toon-publish-probe')
        os.link(source,target);owned.append(target)
    except OSError as error:
        raise ValueError('无法验证输出目录的硬链接发布能力（需支持硬链接，例如 NTFS）：'+str(error)) from error
    finally:
        for path in reversed(owned):path.unlink()


def request(path,context,*,elements=None,memory_budget_bytes=None):
    from .camera import capture
    from . import curve_editor
    frame=capture(context)  # Explicit camera/frame bounds preflight; geometry is not consumed.
    scene=context.scene;layer=context.view_layer
    if scene.library is not None:raise ValueError('请先将场景保存为本地工作副本')
    settings=scene.toon_viewport_preview
    if settings.style_mode=='PRESET':
        from . import addon
        plan=addon.effective_preset(scene)
        addon._map_amounts(scene,plan);addon._dash_patterns(scene,plan)
    element_config=None;admission=None
    if elements is not None:
        from . import addon,render_elements
        plan=addon._preset(scene) if settings.style_mode=='PRESET' else None
        element_config=render_elements.normalize(elements,style_mode=settings.style_mode,
            group_ids=[g['id'] for g in plan['groups']] if plan else ['__BASIC__'])
        admission=render_elements.admit(frame['width'],frame['height'],len(elements),
            render_elements.DEFAULT_BUDGET if memory_budget_bytes is None else memory_budget_bytes,
            depth=any(e['type']=='DEPTH' for e in element_config['elements']))
    if settings.curve_tree is not None:
        curve_editor.synchronize(scene)
        if settings.curve_error:raise ValueError('请先修复或放弃未应用的曲线修改')
    path=Path(bpy.path.abspath(str(path)))
    if not path.is_absolute() or path.suffix.lower() not in {'.png','.exr'} or not path.parent.is_dir():
        raise ValueError('请选择已有目录中的新 PNG 或 EXR 文件名')
    if path.exists() or path.is_symlink():raise FileExistsError('文件已存在，请选择新文件名')
    if element_config is not None and path.suffix.lower()!='.exr':raise ValueError('分层线图请选择新的 EXR 文件名')
    normalized=os.path.normcase(str(path.resolve()))
    scene_key=(scene.as_pointer(),layer.as_pointer())
    from . import sequence_output
    for job in sequence_output.jobs():
        if job['status']=='QUEUED' and (tuple(job['scene_key'])==scene_key or normalized in job.get('normalized_paths',())):
            raise ValueError('此场景视图层或文件名已有待完成的序列线图')
    for job in _jobs.values():
        if job['status']=='QUEUED' and (tuple(job['scene_key'])==scene_key or job['normalized_path']==normalized):
            raise ValueError('此场景视图层或文件名已有待完成的渲染线图')
    for token in list(_jobs):
        if len(_jobs)<20:break
        if _jobs[token]['status']!='QUEUED' and _jobs[token].get('receipt_saved'):del _jobs[token]
    if len(_jobs)>=20:raise ValueError('等待导出的渲染线图过多')
    token=uuid.uuid4().hex
    work=Path(tempfile.mkdtemp(prefix='.r-',dir=path.parent))
    root=Path(__file__).resolve().parent
    job=dict(id=token,path=str(path),normalized_path=normalized,status='QUEUED',mode='RENDER',
        scene_key=scene_key,scene=scene.name,view_layer=layer.name,work_directory=str(work),stage='SETUP',
        extension=path.suffix.lower(),message='后台渲染线图：使用提交时的场景状态，可继续操作或取消')
    if element_config is not None:
        job.update(element_config=element_config,element_config_sha256=render_elements.config_sha(element_config),
                   element_admission=admission,output_extent=frame['metadata']['output_extent'],
                   pixel_aspect=frame['metadata']['pixel_aspect'])
    _jobs[token]=job
    try:
        if not _receipt(job):raise OSError('无法保存任务记录：'+job['receipt_warning'])
        _publish_preflight(work)
        before=bpy.data.filepath
        if bpy.ops.wm.save_as_mainfile(filepath=str(work/'scene.blend'),copy=True,check_existing=False)!={'FINISHED'}:
            raise RuntimeError('无法建立渲染工作副本')
        if bpy.data.filepath!=before:raise RuntimeError('保存工作副本改变了当前文件身份')
        request_data=dict(scene=scene.name,view_layer=layer.name,source_engine=scene.render.engine,
            extension=job['extension'],
            package_root=str(root),snapshot_sha256=_digest(work/'scene.blend'),
            package_files={p.relative_to(root).as_posix():_digest(p) for p in root.rglob('*.py') if '__pycache__' not in p.parts})
        if element_config is not None:
            request_data.update(element_config=element_config,element_config_sha256=job['element_config_sha256'],
                                memory_budget_bytes=admission['budget_bytes'])
        (work/'request.json').write_text(json.dumps(request_data,ensure_ascii=False,indent=2),encoding='utf8')
        args=[bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
            '--python',str(root/'render_worker.py'),'--',str(work/'request.json')]
        with (work/'stdout.log').open('wb') as out,(work/'stderr.log').open('wb') as err:
            process=subprocess.Popen(args,stdout=out,stderr=err,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        _processes[token]=process
        job.update(pid=process.pid,stage='RENDERING',worker_stage='LOADING',snapshot_sha256=request_data['snapshot_sha256'])
    except Exception as error:
        job.update(status='FAILED',stage='SETUP_FAILED',message='渲染线图提交失败：'+str(error))
        _receipt(job);error.add_note('Render task directory: '+str(work))
        raise
    _receipt(job)
    return dict(id=token,status='QUEUED',path=str(path),work_directory=str(work))


def cancel(token):
    job=_jobs.get(token)
    if job is None or job['status']!='QUEUED':return False
    job['cancel_requested']=True
    process=_processes.get(token)
    if process is not None and process.poll() is None:
        try:process.kill()
        except OSError as error:job['cancel_warning']=str(error)
    job['message']='正在取消后台渲染线图';job['stage']='CANCELLING'
    _receipt(job)
    return True


def poll():
    finished=[]
    for token,process in list(_processes.items()):
        job=_jobs[token];work=Path(job['work_directory'])
        code=process.poll()
        if code is None:
            if not job.get('cancel_requested') and (work/'progress.json').is_file():
                try:job['worker_stage']=json.loads((work/'progress.json').read_text(encoding='utf8'))['stage']
                except (OSError,ValueError,KeyError):pass
            continue
        _processes.pop(token);job['worker_exit_code']=code
        if job.get('cancel_requested'):
            job.update(status='CANCELLED',stage='CANCELLED',message='已取消后台渲染线图')
        else:
            published=False
            try:
                job['stage']='WORKER_FAILED' if code!=0 else 'VERIFYING'
                if code!=0:
                    error=(work/'error.txt').read_text(encoding='utf8') if (work/'error.txt').exists() else 'Blender worker exit '+str(code)
                    raise RuntimeError(error[-3000:])
                report=json.loads((work/'result.json').read_text(encoding='utf8'))
                result=report['result'];pixels=work/('pixels'+job['extension'])
                if not report['ok'] or Path(result['path']).resolve()!=pixels.resolve():raise ValueError('Invalid worker output identity')
                expected_contract=('camera_render_depsgraph_elements_exr.v1' if 'element_config' in job else
                                   'camera_render_depsgraph_line_'+job['extension'][1:]+'.v1')
                if result['contract']!=expected_contract or result['evaluation']!='RENDER':
                    raise ValueError('Worker did not produce RENDER geometry')
                if result['scene']!=job['scene'] or result['view_layer']!=job['view_layer']:
                    raise ValueError('Worker source scene or view layer changed')
                if result['snapshot_sha256']!=job['snapshot_sha256'] or _digest(pixels)!=result['sha256']:
                    raise ValueError('Worker output content changed')
                if 'element_config' in job:
                    from . import render_elements
                    if result['element_config_sha256']!=job['element_config_sha256']:
                        raise ValueError('Worker element configuration changed')
                    expected=[dict(id=e['id'],pass_name=e['pass_name'],label=e['label']) for e in job['element_config']['elements']]
                    if result['element_passes']!=expected:raise ValueError('Worker element pass mapping changed')
                    w,h=job['output_extent']
                    render_elements.validate_file(pixels,job['element_config'],w,h,job['pixel_aspect'])
                # The worker never receives the public destination. Cancellation
                # cannot race a worker publication; this main-thread link is the
                # sole create-only commit point, after the worker has exited.
                committed=dict(result,path=job['path'])
                job['stage']='PUBLISHING'
                os.link(pixels,job['path']);published=True
                job.update(status='COMPLETE',stage='COMPLETE',result=committed,message='已导出渲染线图：'+Path(job['path']).name)
            except Exception as error:
                if published:
                    job.update(status='COMPLETE',stage='COMPLETE',result=committed,ui_warning=str(error))
                else:
                    stage={'VERIFYING':'VALIDATION_FAILED','PUBLISHING':'PUBLISH_FAILED'}.get(job['stage'],'WORKER_FAILED')
                    job.update(status='FAILED',stage=stage,message='渲染线图导出失败：'+str(error))
        _receipt(job)
        finished.append(job)
    return finished


def cancel_all():
    for token in list(_processes):cancel(token)
    for token,process in list(_processes.items()):
        if process.poll() is None:
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                try:process.kill();process.wait(timeout=1.)
                except (OSError,subprocess.TimeoutExpired) as error:
                    _jobs[token]['cancel_warning']=str(error);_receipt(_jobs[token])
    return poll()
