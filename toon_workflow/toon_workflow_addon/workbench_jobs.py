# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect/recover submission glue; the public CLI owns every running job.

Recovery consumes the original persisted disk request, never a current GUI
capture. All disk admission and process launch precede changing the GUI owner.
"""
import copy
import json
import math
import os
from pathlib import Path
import uuid


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _ordinary(path, *, directory=False):
    path = Path(path)
    _require(path.is_absolute(), '工作台历史路径必须为绝对路径')
    for node in (path, *path.parents):
        if node.is_symlink() or (node.exists() and getattr(node.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('历史提交不接受符号链接或重解析路径')
    _require(path.is_dir() if directory else path.is_file(), '历史提交文件或目录失联：' + str(path))
    return path.resolve(strict=True)


def _reference(wb, path):
    path = _ordinary(path)
    return dict(file=str(path), expected_sha256=wb._sha(path))


def _checked(wb, descriptor):
    from .workflow_contract import descriptor as normalize_descriptor
    row = normalize_descriptor(descriptor)
    path = _ordinary(row['file'])
    _require(wb._sha(path) == row['expected_sha256'], '历史提交指纹已改变：' + str(path))
    return path


def _read_bound(wb, path, anchors):
    row = _reference(wb, path)
    value = wb._read(Path(row['file']))
    _checked(wb, row)
    anchors.append(row)
    return value


def _public_request(value, operation):
    from .workflow_contract import normalize
    _require(type(value) is dict and set(value) == {'schema_version', 'command', 'params'}
        and value['schema_version'] == '1.0' and value['command'] == 'toon-workflow.' + operation,
        '历史公共请求的操作或闭合字段不匹配')
    normalize(operation, value['params'])
    return value


def _owned_terminal(wb, context):
    settings = getattr(context.scene, wb.PROP)
    record = wb._record(settings)  # Includes the parent's real Scene-owner check.
    _ordinary(record['directory'], directory=True)
    job_id = record.get('job_id')
    _require(type(job_id) is str and len(job_id) == 32 and all(c in '0123456789abcdef' for c in job_id),
             '拥有的提交尚未取得明确公共作业编号')
    _require(settings.job_id == job_id, '当前GUI作业编号与拥有的提交不一致')
    job = _ordinary(Path(record['jobs_directory']) / job_id, directory=True)
    anchors = []
    status = _read_bound(wb, job / 'status.json', anchors)
    _require(status.get('job_id') == job_id and status.get('state') in
             {'succeeded', 'failed', 'cancelled', 'canceled', 'timed_out'},
             '必须等待原公共作业达到真实终态后再检查或恢复')
    anchors.append(_reference(wb, Path(record['directory']) / 'submission.json'))
    return record, job, status, anchors


def _original_params(wb, record, job, anchors):
    from .workflow_contract import normalize, fingerprint
    from .joint_request import code_inventory
    _require(record.get('operation') in {'run', 'recover'}, '只有原run/recover作业可恢复或检查完成状态')
    persisted = _read_bound(wb, job / 'toon-workflow-request.json', anchors)
    _require(type(persisted) is dict and set(persisted) == {'schema', 'params'}
        and persisted['schema'] == 'toon-workflow-request.v1', '缺少可恢复的原始持久请求锚点')
    original = normalize('run', persisted['params'])
    public = _public_request(_read_bound(wb, job / 'request.json', anchors), record['operation'])
    if record['operation'] == 'run':
        _require(fingerprint(public['params']) == fingerprint(original), '公共run请求与持久原参数不同')
    else:
        previous = _checked(wb, public['params']['recovery'])
        anchor = _read_bound(wb, previous, anchors)
        _require(type(anchor) is dict and set(anchor) == {'schema', 'params'}
            and anchor['schema'] == 'toon-workflow-request.v1'
            and fingerprint(normalize('run', anchor['params'])) == fingerprint(original),
            '公共recover请求与其原始参数谱系不同')
        _require(public['params']['implementation'] == original['implementation'], '原恢复请求改写了implementation')
    _require(original['implementation'] == code_inventory(),
             '原作业实现指纹与当前代码不同；保留旧pin，请选择匹配实现，不自动重签')
    source = {key: original[key] for key in ('file', 'expected_sha256')}
    _checked(wb, source)
    _require(Path(record['snapshot']).resolve() == Path(source['file']).resolve()
        and record['snapshot_sha256'] == source['expected_sha256'], '原提交的快照与持久source pin不符')
    spec = original['manifest']
    _require(record['scene'] == spec['scene'] and record['view_layer'] == spec['view_layer']
        and fingerprint(record['times']) == fingerprint(spec['times'])
        and record['lighting_samples'] == spec['lighting_samples'], '原提交场景/时间/光照设置与持久请求不同')
    declared = {str(Path(row['file']).resolve()) for row in original['resources']}
    _require({str(Path(path).resolve()) for path in record['resources']} == declared, '原提交的资源集合不同')
    for row in (source, *original['resources']):
        _checked(wb, row)
        anchors.append(copy.deepcopy(row))
    return original, public


def _accepted_state(wb, record, job, status, original, public, anchors):
    from .workflow_contract import descriptor, fingerprint, STATE
    _require(status['state'] == 'succeeded', '只有成功run/recover具有可检查的已接受完成状态')
    result = _read_bound(wb, job / 'result.json', anchors)
    acceptance = _read_bound(wb, job / 'toon-workflow-acceptance.json', anchors)
    _require(result.get('job_id') == record['job_id'] and result.get('ok') is True
        and result.get('data', {}).get('candidate_accepted') is True, '原成功结果没有公共父验收')
    _require(acceptance.get('schema') == 'toon-workflow-parent-acceptance.v1'
        and acceptance.get('status') == 'accepted' and acceptance.get('operation') == record['operation']
        and acceptance.get('request_sha256') == fingerprint(public), '公共acceptance不绑定原请求')
    report_path = _checked(wb, acceptance['report'])
    _require(report_path == job / 'toon-workflow-report.json', '原验收报告路径越出拥有的作业')
    report = _read_bound(wb, report_path, anchors)
    source = {key: original[key] for key in ('file', 'expected_sha256')}
    _require(report.get('schema') == 'toon-workflow-report.v1' and report.get('status') == 'passed'
        and report.get('operation') == record['operation'] and report.get('source') == source
        and report.get('implementation') == original['implementation'], '已接受报告的source或pin不同')
    _require(fingerprint({key: result['data'].get(key) for key in report['result']}) == fingerprint(report['result']),
             '公共result与权威report摘要不同')
    outputs = acceptance.get('outputs')
    _require(type(outputs) is list and outputs and outputs == result['data'].get('outputs'), '原验收输出清单不完整')
    by_path = {}
    for row in outputs:
        descriptor(row)
        path = _checked(wb, row)
        _require(path.is_relative_to(job) and path not in by_path, '原验收输出越界或重复')
        by_path[path] = row
    state_ref = descriptor(report['result']['state'])
    state_path = _checked(wb, state_ref)
    _require(state_path == job / 'toon-workflow-state.json' and by_path.get(state_path) == state_ref,
             '完成state未包含在原验收输出中')
    state = _read_bound(wb, state_path, anchors)
    _require(state.get('schema') == STATE and state.get('status') == 'completed'
        and state.get('source') == source and state.get('implementation') == original['implementation']
        and state.get('manifest') == original['manifest'] and state.get('resources') == original['resources'],
        '完成state不绑定原source/settings/resources/pin')
    _require(all(by_path.get(_checked(wb, row)) == row for row in state['outputs']), '完成state输出不属于父验收清单')
    return state_ref


def _preflight_launch(wb, context, record):
    settings = getattr(context.scene, wb.PROP)
    timeout = settings.timeout_seconds
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, '公共作业超时参数无效')
    wb._submission_root(record)
    return wb._command(record, '--timeout', str(timeout), '--async', 'request', record['request'])


def _launch_and_activate(wb, context, record, command, anchors):
    # Recheck all admitted historical descriptors before giving the closed
    # request to the public supervisor, which holds its own runtime FileGuards.
    for row in anchors:
        _checked(wb, row)
    wb._launch(record, command, 'submit')
    wb._activate_record(context, record)
    settings = getattr(context.scene, wb.PROP)
    settings.job_state = 'submitting'
    settings.message = ('已从原持久快照提交恢复；当前GUI编辑未捕获' if record['operation'] == 'recover'
                        else '已提交公共检查；结果由公共作业系统验收')
    return record


def _historical_submission(wb, context, original_record, operation, params, anchors, job):
    from .workflow_contract import normalize
    request = dict(schema_version='1.0', command='toon-workflow.' + operation,
                   params=normalize(operation, copy.deepcopy(params)))
    _public_request(request, operation)
    record = copy.deepcopy(original_record)  # Preserve GUI identity and waiting-period guards.
    owner = uuid.uuid4().hex
    directory = Path(original_record['directory']).parent / ('toon-joint-' + owner)
    _ordinary(directory.parent, directory=True)
    _require(not directory.exists(), '新提交目录已存在，停止覆盖')
    record.update(owner=owner, directory=str(directory), jobs_directory=str(directory / 'jobs'),
        request=str(directory / 'request.json'), operation=operation, job_id='', state='prepared',
        artifact='', cancel_sent=False,
        origin=dict(owner=original_record['owner'], submission=_reference(wb, Path(original_record['directory']) / 'submission.json'),
                    job_directory=str(job), operation=original_record['operation']),
        origin_anchors=copy.deepcopy(anchors))
    if operation == 'recover':
        record['recovery_ref'] = copy.deepcopy(params['recovery'])
    command = _preflight_launch(wb, context, record)
    for row in anchors:
        _checked(wb, row)
    directory.mkdir(exist_ok=False)
    (directory / 'jobs').mkdir(exist_ok=False)
    def write_new(path, value):
        with path.open('x', encoding='utf8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    write_new(directory / 'request.json', request)
    if operation == 'recover':
        write_new(directory / 'recovery-ref.json', params['recovery'])
    write_new(directory / 'submission.json', record)
    return _launch_and_activate(wb, context, record, command, anchors)


def start_inspect(context):
    from . import workbench as wb
    settings = getattr(context.scene, wb.PROP)
    if settings.owner_token:
        owned = wb._record(settings)
        if owned.get('job_id'):
            record, job, status, anchors = _owned_terminal(wb, context)
            if record.get('operation') in {'run', 'recover'} and status['state'] == 'succeeded':
                original, public = _original_params(wb, record, job, anchors)
                state = _accepted_state(wb, record, job, status, original, public, anchors)
                return _historical_submission(wb, context, record, 'inspect',
                    dict(state=state, implementation=copy.deepcopy(original['implementation'])), anchors, job)
    # Failed/non-run submissions have no accepted state. This explicit inspect
    # branch captures a NEW current snapshot, rather than repairing old records.
    record = wb._prepare(context, operation='inspect', activate=False)
    _public_request(wb._read(Path(record['request'])), 'inspect')
    command = _preflight_launch(wb, context, record)
    return _launch_and_activate(wb, context, record, command, [])


def start_recover(context):
    from . import workbench as wb
    record, job, status, anchors = _owned_terminal(wb, context)
    original, public = _original_params(wb, record, job, anchors)
    recovery = _reference(wb, job / 'toon-workflow-request.json')
    return _historical_submission(wb, context, record, 'recover',
        dict(recovery=recovery, implementation=copy.deepcopy(original['implementation'])), anchors, job)
