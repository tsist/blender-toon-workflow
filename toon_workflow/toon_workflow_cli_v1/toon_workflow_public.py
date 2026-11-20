"""Public 2D Toon discovery, closed contract and supervisor integration."""
import json
import sys
from pathlib import Path
from protocol import ROOT, Failure, digest, read_json
from deployment import domain

TOOLS = Path(__file__).resolve().parent.parent


def normalize(command, params):
    check = domain('workflow_contract').normalize
    try:
        return check(command.split('.')[1], params)
    except (ValueError, TypeError, KeyError) as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc


def parameter_schema(operation):
    import copy
    if operation not in {'run','inspect','edit','recover'}:
        raise Failure('INVALID_REQUEST','Select --operation for its schema')
    def obj(fields):return dict(type='object',properties=fields,required=list(fields),additionalProperties=False)
    file=obj(dict(file=dict(type='string',minLength=1,description='absolute saved file path'),
        expected_sha256=dict(type='string',pattern='^[0-9a-f]{64}$')))
    implementation=dict(type='object',minProperties=1,maxProperties=256,
        additionalProperties=dict(type='string',pattern='^[0-9a-f]{64}$'),
        description='exact current describe inventory; relative tools paths, no parent traversal')
    manifest=obj(dict(schema=dict(enum=['toon-workflow-manifest.v1','toon-workflow-manifest.v2']),
        scene=dict(type='string',minLength=1,maxLength=256),view_layer=dict(type='string',minLength=1,maxLength=256),
        times=dict(type='array',minItems=1,maxItems=96,items=dict(type='number'),description='ordered nominal times; v2 records native effective times and rejects native-time collisions'),
        samples_per_axis=dict(enum=[1,2,4,8]),lighting_samples=dict(type='integer',minimum=1,maximum=256),
        memory_budget_bytes=dict(type='integer',minimum=1)))
    fields=dict(file=file['properties']['file'],expected_sha256=file['properties']['expected_sha256'],
        resources=dict(type='array',maxItems=100,items=file),implementation=implementation,manifest=manifest)
    if operation=='edit':fields.update(style=file,expected_author_sha256=dict(type='string',pattern='^[0-9a-f]{64}$'))
    if operation=='recover':return obj(dict(recovery=file,implementation=implementation))
    source=obj(fields)
    if operation=='inspect':return dict(oneOf=[source,obj(dict(state=file,implementation=implementation))])
    return source


def describe(operation=None,schema=False):
    if schema:return parameter_schema(operation)
    __version__ = domain().__version__
    code_inventory = domain('joint_request').code_inventory
    return dict(domain='toon-workflow',version=__version__,manifest='toon-workflow-manifest.v2',
        legacy_manifests=['toon-workflow-manifest.v1'],
        state='toon-workflow-state.v1',operations=['run','inspect','edit','recover'],
        source_mode='SAVED_DISK_V1; no implicit live GUI synchronization',
        blender=dict(version='5.2.1 LTS',build_hash='9e2066aef7ef'),
        joint_renderer_builds=[dict(version='.'.join(map(str, version)), build_hash=build)
                               for version, build in sorted(domain('runtime_compat').JOINT_BUILDS)],
        output_profile='eevee_joint_grid.v3; explicit native_media_artline.v1; eevee_joint_sequence.v2; legacy eevee_joint_sequence.v1',
        style_families=['toon-style-family.v1 (unchanged)', 'toon-style-family.v2 (CEL/GAME/MANGA with explicit Look)'],
        media_profile=dict(selection='saved Scene toon_native_media.enabled',
            beauty='native EEVEE media, DoF and motion blur',
            line='explicit OVERLAY or EXCLUDE; uniform artistic depth and velocity',
            limits='no refracted/volume-transported Line; nonempty regional Look rejects media'),
        time_policy=dict(current='blender-frame-set-f32-total.v1',
                         legacy='floor + float32 subframe; preserve order',
                         requested_times='preserved with strict effective-time receipts'),
        implementation=code_inventory(),resources_limit=100,
        edit_styles=dict(preset='toon.autonomous-style-file.v1; full document/recipe',
                         basic=dict(schema='toon-basic-line-width-edit.v1',
                                    fields=['schema','line_width'],
                                    line_width='absolute static pixel width0.1..32; BASIC only; rejects animation/driver references and META NLA',
                                    example=dict(schema='toon-basic-line-width-edit.v1',line_width=1.21)),
                         baseline='expected_author_sha256 from matching source inspect; no implicit mode conversion'),
        claims=['opaque local EEVEE mesh workflow on the qualified Blender build',
                'new2D domain; legacy toon geometry manifests/states are rejected',
                'public supervisor owns cancellation/timeout/process tree',
                'recovery reuses verified complete prefix only; source/resources/settings/pin must match'],
        fields=dict(run=['file','expected_sha256','resources','implementation','manifest'],
                    inspect='run-shaped source inspection, or state descriptor + implementation',
                    edit='run fields + style file descriptor + expected_author_sha256',
                    recover='recovery descriptor of persisted toon-workflow-request.json + implementation'))


def _ordinary(path):
    import os
    path=Path(path)
    if not path.is_absolute():raise Failure('INVALID_REQUEST','Absolute domain input required')
    for node in [path,*path.parents]:
        if node.is_symlink() or (node.exists() and getattr(node.lstat(),'st_file_attributes',0)&0x400):
            raise Failure('CONFLICT','Domain input uses a reparse/symlink path')
    if not path.is_file():raise Failure('NOT_FOUND','Domain input is not a file: '+str(path))
    return path


def _checked(row):
    path=_ordinary(row['file'])
    if digest(path)!=row['expected_sha256']:raise Failure('CONFLICT','Domain anchor changed')
    return path


def effective_params(command, params):
    if command.endswith('.recover'):
        value=read_json(_checked(params['recovery']))
        if value.get('schema')!='toon-workflow-request.v1':
            raise Failure('UNSUPPORTED','Only a persisted new2D workflow request can be recovered')
        original=normalize('toon-workflow.run',value['params'])
        if original['implementation']!=params['implementation']:
            raise Failure('CONFLICT','Recovery implementation differs from the original request')
        return original
    return params


def anchors(command, params):
    if command.endswith('.recover'):return [params['recovery']]
    if 'state' in params:return [params['state']]
    return []


def input_documents(command, params):
    code_inventory = domain('joint_request').code_inventory
    current=code_inventory()
    if params['implementation']!=current:
        raise Failure('CONFLICT','Toon workflow implementation inventory changed')
    rows=[dict(file=str(TOOLS/name),expected_sha256=sha) for name,sha in current.items()]
    actual=effective_params(command,params)
    if 'state' in actual:
        state=read_json(_checked(actual['state']))
        if state.get('schema')!='toon-workflow-state.v1':
            raise Failure('UNSUPPORTED','Legacy geometry or unknown state; explicit migration is unavailable')
        rows.extend(state['outputs'])
        for row in rows:_ordinary(row['file'])
        return rows
    rows += [{k:actual[k] for k in ('file','expected_sha256')},*actual['resources']]
    if 'style' in actual:rows.append(actual['style'])
    if command.endswith('.recover'):
        root=_checked(params['recovery']).parent/'sequence-output'
        if root.exists():
            # Guard the old complete-prefix closure before the worker reads it.
            for index in range(96):
                frame=root/('frame-%03d'%index)
                if not (frame/'complete.json').is_file():break
                for path in sorted(frame.rglob('*')):
                    if path.is_file():
                        _ordinary(path);rows.append(dict(file=str(path),expected_sha256=digest(path)))
            path=root/'request.json'
            if path.is_file():rows.append(dict(file=str(path),expected_sha256=digest(path)))
    for row in rows:_ordinary(row['file'])
    return rows


def accept(job, request, data, stack, checkpoint, *, guard_type=None):
    if guard_type is None:
        from filesystem import FileGuard as guard_type
    inspect_sequence = domain('joint_sequence').inspect_sequence
    fingerprint = domain('workflow_contract').fingerprint
    validate_completed_state = domain('joint_workflow').validate_completed_state
    job=Path(job).resolve()
    if data.get('status')!='passed':raise Failure('VALIDATION_FAILED','Domain worker did not pass')
    outputs=data.get('outputs',[])
    if not outputs:raise Failure('VALIDATION_FAILED','Domain output inventory is empty')
    descriptor = domain('workflow_contract').descriptor
    try:
        for row in outputs:descriptor(row)
    except (ValueError,TypeError) as error:
        raise Failure('VALIDATION_FAILED','Invalid output descriptor') from error
    by_path={str(Path(row['file']).absolute()):row for row in outputs}
    if len(by_path)!=len(outputs) or data.get('report')!=str(job/'toon-workflow-report.json'):
        raise Failure('VALIDATION_FAILED','Duplicate outputs or unexpected public report path')
    def require(path):
        path=Path(path).absolute()
        if str(path) not in by_path:raise Failure('VALIDATION_FAILED','Required output is absent from inventory: '+str(path))
        return path
    require(job/'toon-workflow-report.json')
    for row in outputs:
        path=_ordinary(row['file'])
        if not path.resolve().is_relative_to(job):raise Failure('VALIDATION_FAILED','Domain output escaped job')
        guard=stack.enter_context(guard_type(path))
        if guard.sha256(checkpoint)!=row['expected_sha256']:
            raise Failure('CONFLICT','Domain output changed before parent acceptance')
    report=read_json(job/'toon-workflow-report.json')
    params=effective_params(request['command'],request['params'])
    if (report.get('schema')!='toon-workflow-report.v1' or report.get('status')!='passed'
            or report.get('operation')!=request['command'].split('.')[1]
            or report.get('implementation')!=request['params']['implementation']):
        raise Failure('VALIDATION_FAILED','Domain report identity differs')
    if report.get('source') and report['source']!={k:params[k] for k in ('file','expected_sha256')}:
        raise Failure('VALIDATION_FAILED','Domain source binding differs')
    summary=report['result']
    if fingerprint({key:data.get(key) for key in summary})!=fingerprint(summary):
        raise Failure('VALIDATION_FAILED','Public result differs from the authoritative report')
    if report['operation']=='edit':
        candidate=require(job/'toon-candidate.blend')
        for name in ('toon-author.json','toon-author-after.json','toon-author-style.json'):require(job/name)
        before=read_json(job/'toon-author.json');after=read_json(job/'toon-author-after.json')
        if (summary.get('candidate')!=str(candidate) or summary.get('output')!=str(candidate)
                or summary.get('candidate_sha256')!=digest(candidate)
                or summary.get('baseline_author_sha256')!=params['expected_author_sha256']
                or before['author_sha256']!=params['expected_author_sha256']
                or fingerprint(before['author'])!=before['author_sha256']
                or after['author_sha256']!=summary.get('author_sha256')
                or fingerprint(after['author'])!=after['author_sha256']):
            raise Failure('VALIDATION_FAILED','Author candidate/baseline binding differs')
    if request['command'].endswith(('.run','.recover')):
        state=read_json(require(job/'toon-workflow-state.json'))
        for row in state['outputs']:
            member=require(row['file'])
            if by_path[str(member)]!=row:raise Failure('VALIDATION_FAILED','State output inventory differs')
        for name in ('toon-author.json','toon-workflow-request.json','sequence-output/request.json',
                     'sequence-output/sequence-report.json','sequence-output/complete.json'):require(job/name)
        seq=read_json(job/'sequence-output/sequence-report.json')
        for index in range(len(seq['frames'])):
            base=job/'sequence-output'/('frame-%03d'%index)
            require(base/'complete.json');require(base/'frame-report.json')
            frame=read_json(require(base/'joint-output/report.json'))
            require(frame['author_checkpoint'])
            for artifact in frame['artifacts'].values():require(artifact['path'])
        spec=params['manifest']
        state,cold=validate_completed_state(job/'toon-workflow-state.json',expected_params=params)
        data['cold_inspection']=cold
        expected_preview=job/'sequence-output'/('frame-%03d'%(len(spec['times'])-1))/'joint-output/Combined.png'
        if summary.get('output')!=str(expected_preview):
            raise Failure('VALIDATION_FAILED','Public preview path differs from the last requested frame')
        require(expected_preview)
    if report['operation']=='inspect' and 'state' in params:
        state,cold=validate_completed_state(_checked(params['state']))
        if summary.get('state')!=params['state'] or fingerprint(summary.get('inspection'))!=fingerprint(cold):
            raise Failure('VALIDATION_FAILED','State inspection result differs from the bound output bundle')
    if report['operation']=='inspect' and 'author' in summary:
        require(job/'toon-author.json')
        observed=read_json(job/'toon-author.json')
        if observed['author_sha256']!=summary['author_sha256'] or fingerprint(observed['author'])!=summary['author_sha256']:
            raise Failure('VALIDATION_FAILED','Captured author fingerprint differs')
    if report['operation']=='edit':
        # Reuse the same public supervisor for an independent disk reopen.
        # Its checkpoint observes parent cancellation/timeout; no private worker.
        import runner
        launch=read_json(job/'launch.json')
        child_params={k:params[k] for k in ('resources','implementation','manifest')}
        child_params.update(file=str(job/'toon-candidate.blend'),expected_sha256=digest(job/'toon-candidate.blend'))
        child=runner.prepare(dict(schema_version='1.0',command='toon-workflow.inspect',params=child_params),
            blender=Path(launch['blender']),root=job/'children',timeout=min(120,launch['timeout_seconds']),
            transactions_root=Path(launch['transactions_root']))
        reopened,code=runner.execute(child,parent_checkpoint=checkpoint)
        checkpoint()
        if code!=0 or not reopened.get('ok'):
            child_error=reopened.get('error') or {}
            if child_error.get('code') in {'CANCELLED','TIMEOUT'}:
                raise Failure(child_error['code'],'Author cold reopen: '+str(child_error.get('message','')))
            raise Failure('VALIDATION_FAILED','Independent author candidate reopen failed')
        observed=read_json(child/'toon-author.json')
        if observed['author_sha256']!=summary['author_sha256']:
            raise Failure('VALIDATION_FAILED','Saved author candidate differs after independent reopen')
        data['author_reopen']=dict(job=str(child),author_sha256=observed['author_sha256'],status='pass')
    from protocol import atomic_json
    acceptance=dict(schema='toon-workflow-parent-acceptance.v1',status='accepted',
        operation=report['operation'],report=dict(file=str(job/'toon-workflow-report.json'),
            expected_sha256=digest(job/'toon-workflow-report.json')),outputs=outputs,
        request_sha256=fingerprint(request),claim='bounded technical acceptance; not artistic approval')
    atomic_json(job/'toon-workflow-acceptance.json',acceptance)
    data.update(candidate_accepted=True,parent_output_validation='passed',
                acceptance=str(job/'toon-workflow-acceptance.json'))
    return data
