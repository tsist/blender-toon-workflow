# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy an integrity-qualified completed prefix into a new sequence job.

This is disk-only recovery, called inside the public worker. It starts no
process and never changes the previous output. The caller owns input locks,
implementation admission and the lifetime of both job directories. Only the
receipt-bound product closure is copied; orphan render scratch is not reused.
"""
import copy
import os
import re
from pathlib import Path
import shutil
import stat
import uuid

from . import joint_inspect
from . import joint_sequence as sequence


def _ordinary_path(path, *, directory=False):
    """Reject links/reparse points in every existing component, before resolve."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Recovery requires absolute ordinary paths')
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Recovery rejects symlink/reparse paths: ' + str(component))
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Recovery path ancestor is not a directory')
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError('Recovery requires ordinary files/directories: ' + str(path))
    return path.resolve(strict=True)


def _ordinary_tree(root):
    """Do not traverse directory links, including Windows junctions."""
    _ordinary_path(root, directory=True)
    pending = [root]
    while pending:
        for path in pending.pop().iterdir():
            # lstat before is_dir prevents even probing through a junction.
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise ValueError('Recovery rejects symlink/reparse tree entries')
            directory = stat.S_ISDIR(info.st_mode)
            child = _ordinary_path(path, directory=directory)
            if not child.is_relative_to(root):
                raise ValueError('Recovery entry escaped previous output')
            if directory:
                pending.append(child)


def _checked_reference(reference, root):
    # The public cold-reader check verifies SHA and containment; the extra
    # check covers intermediate junctions/reparse points on Windows too.
    path = _ordinary_path(reference['path'])
    checked = sequence._checked_reference(reference, root)
    if path != checked:
        raise ValueError('Recovery reference resolution differs')
    return checked



def _contract_relative(path, root, directory_pattern, filename):
    """Lexical producer-owned location; never resolve a deleted process file."""
    if type(path) is not str:
        raise ValueError('Recovery receipt path must be a string')
    candidate=Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('Recovery receipt path is not absolute or contains an escape')
    try:relative=candidate.relative_to(root)
    except ValueError:raise ValueError('Recovery receipt path escaped its joint output') from None
    if (len(relative.parts)!=2 or re.fullmatch(directory_pattern,relative.parts[0]) is None
            or relative.parts[1] not in filename):
        raise ValueError('Recovery receipt path is outside its producer contract location')
    return relative


def _existing_ordinary_prefix(root, relative):
    """Check each existing component while allowing a deleted temporary leaf."""
    current=_ordinary_path(root,directory=True)
    for index,part in enumerate(relative.parts):
        current=current/part
        try:info=current.lstat()
        except FileNotFoundError:break
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Recovery rejects reparse receipt path components')
        expected=stat.S_ISREG if index==len(relative.parts)-1 else stat.S_ISDIR
        if not expected(info.st_mode):raise ValueError('Recovery receipt path has a nonordinary component')


def _regional_references(joint, frame_root):
    """Only the retained files in the qualified regions.v2 native captures."""
    look=joint.get('shot_look')
    if not isinstance(look,dict) or look.get('schema')!='toon-shot-look.regions.v2':return []
    capture=look.get('capture')
    if (type(capture) is not dict or capture.get('profile')!='toon-shot-look.regions.v2'
            or type(capture.get('captures')) is not list):
        raise ValueError('Recovery regions.v2 capture contract differs')
    root=frame_root/'joint-output'
    result=[]
    for row in capture['captures']:
        if (type(row) is not dict or row.get('profile')!='toon-native-capture.v1'
                or type(row.get('files')) is not list or not row['files']):
            raise ValueError('Recovery requires qualified native capture file records')
        for reference in row['files']:
            if type(reference) is not dict or set(reference)!={'file','sha256'}:
                raise ValueError('Recovery native capture reference contract differs')
            _contract_relative(reference['file'],root,r'native-capture-[0-9a-f]{32}',
                               {'PassBackground.exr','PassDepth.exr','PassVector.exr'})
            result.append(dict(path=reference['file'],sha256=reference['sha256']))
    return result


def _rebase_known_receipts(new_joint, old_frame, new_frame, rebound):
    """Rebind explicit versioned fields only; arbitrary strings stay untouched."""
    old_root=old_frame/'joint-output';new_root=new_frame/'joint-output'
    if (new_joint.get('schema')=='toon-native-media-artline-report.v1'
            and new_joint.get('profile')=='native_media_artline.v1'):
        optics=new_joint.get('line_optics')
        if (type(optics) is not dict or set(optics)!={'process','processing_output_retained','published_output','crop_after_optics'}
                or optics['processing_output_retained'] is not False or optics['crop_after_optics'] is not True
                or type(optics['process']) is not dict or optics['process'].get('schema')!='toon-art-line-optics.v1'):
            raise ValueError('Recovery native-media optical receipt contract differs')
        # This equality was already qualified by the unchanged cold reader.
        # Do not derive a new publication path from a free-form nested string.
        old_published=rebound(optics['published_output'])
        published=new_joint['artifacts']['OpticalLine']['path']
        if old_published!=published:raise ValueError('Recovery OpticalLine publication binding differs')
        relative=_contract_relative(optics['process']['output'],old_root,r'native-media-[a-z0-9_]{8}',{'OpticalLine.exr'})
        _existing_ordinary_prefix(old_root,relative)
        _existing_ordinary_prefix(new_root,relative)
        optics['published_output']=published
        optics['process']['output']=str(new_root/relative)
    look=new_joint.get('shot_look')
    if isinstance(look,dict) and look.get('schema')=='toon-shot-look.regions.v2':
        # Retained auxiliary files were SHA-checked/copied as frame products.
        # The existing cold inspector checks their rebound paths and hashes.
        for row in look['capture']['captures']:
            for reference in row['files']:reference['file']=rebound(reference['file'])

def _check_frame(frame_root, index, requested, settings):
    """Validate the complete marker -> frame report -> joint report closure."""
    _ordinary_tree(frame_root)
    marker_path = _ordinary_path(frame_root/'complete.json')
    marker = sequence._read(marker_path)
    if marker.get('schema') != 'toon-sequence-frame-complete.v1':
        raise ValueError('Invalid recovery frame complete marker')
    report_path = _checked_reference(marker['frame_report'], frame_root)
    if report_path != frame_root/'frame-report.json':
        raise ValueError('Unexpected recovery frame report location')
    report = sequence._read(report_path)
    if (report.get('schema') != 'toon-sequence-frame-report.v1'
            or report.get('profile') != settings['profile']):
        raise ValueError('Invalid recovery frame report')
    fingerprint = sequence._fingerprint(settings)
    implementation_sha = sequence._fingerprint(settings['implementation'])
    for record in (marker, report):
        if (record.get('status') != 'completed'
                or type(record.get('index')) is not int
                or record['index'] != index
                or sequence._json_bytes(record.get('time')) != sequence._json_bytes(requested)
                or record.get('settings_sha256') != fingerprint
                or record.get('implementation_sha256') != implementation_sha):
            raise ValueError('Recovery frame time/order/settings/implementation differs')
    if (marker['camera'] != report['camera']
            or marker['source_sha256'] != settings['source']['sha256']
            or report['source'] != settings['source']):
        raise ValueError('Recovery frame camera/source differs')
    joint_path = _checked_reference(report['joint_report'], frame_root)
    if joint_path != frame_root/'joint-output'/'report.json':
        raise ValueError('Unexpected recovery joint report location')
    joint = sequence._read(joint_path)
    sequence._joint_time_camera(joint, requested, report['camera'], settings['source'])
    sequence._joint_settings(joint, settings)
    if (report['artifacts'] != joint['artifacts']
            or report['author'] != dict(path=joint['author_checkpoint'],
                                        sha256=joint['author_sha256'])):
        raise ValueError('Recovery frame artifact bindings differ')
    products = {}
    for artifact in [report['author'], *report['artifacts'].values()]:
        path = _checked_reference(dict(path=artifact['path'], sha256=artifact['sha256']), frame_root)
        if path in {marker_path, report_path, joint_path}:
            raise ValueError('Recovery products cannot alias receipts')
        products[path] = artifact['sha256']
    for reference in _regional_references(joint, frame_root):
        path = _checked_reference(reference, frame_root)
        if path in {marker_path, report_path, joint_path}:
            raise ValueError('Recovery regional products cannot alias receipts')
        if path in products and products[path] != reference['sha256']:
            raise ValueError('Recovery regional product SHA bindings differ')
        products[path] = reference['sha256']
    checked = joint_inspect.inspect(joint_path)
    if checked.get('status') != 'pass' or report.get('joint_inspection') != checked:
        raise ValueError('Recovery joint inspection differs or failed')
    return marker, report, joint, products


def _write_new_json(path, value):
    """Publish atomically with create-only semantics, including a racing writer."""
    temporary = path.with_name('.j-' + uuid.uuid4().hex[:12] + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(sequence._json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        # link() fails if path exists, unlike replace()/POSIX rename(). The
        # complete JSON becomes visible in one operation, on the same volume.
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def copy_completed_frames(previous_root, new_root, expected_settings):
    """Copy and rebind only the ordered complete prefix, rejecting corruption.

    ``new_root`` must contain only its already-written matching request.json.
    Settings, source and implementation must match exactly. Missing complete
    markers end the prefix; an existing malformed marker is an error. No old
    sequence-level complete marker is required. A failed copy leaves only the
    new job's partial output and never promotes an unverified frame marker.
    """
    previous = _ordinary_path(previous_root, directory=True)
    destination = _ordinary_path(new_root, directory=True)
    if (previous == destination or previous.is_relative_to(destination)
            or destination.is_relative_to(previous)):
        raise ValueError('Recovery roots must be distinct and non-nested')
    if type(expected_settings) is not dict:
        raise ValueError('Recovery requires complete expected settings')
    # Canonical bytes retain JSON field types (e.g. bool versus int), unlike
    # Python's numeric equality. No setting or source/pin condition is relaxed.
    expected = sequence._json_bytes(expected_settings)
    if expected_settings.get('resume_supported') is not False:
        raise ValueError('Unsupported recovery sequence settings')
    from .joint_time import validate_settings_times
    validate_settings_times(expected_settings)
    for root in (previous, destination):
        _ordinary_path(root/'request.json')
        if sequence._json_bytes(sequence._read(root/'request.json')) != expected:
            raise ValueError('Recovery request settings differ')
    if {entry.name for entry in destination.iterdir()} != {'request.json'}:
        raise ValueError('Recovery destination must contain only request.json')
    source = _ordinary_path(expected_settings['source']['file'])
    if sequence._digest(source) != expected_settings['source']['sha256']:
        raise ValueError('Recovery source changed')
    _ordinary_tree(previous)
    qualified = []
    # Qualify the entire prefix before creating any new frame. A corrupt later
    # completed frame cannot silently degrade recovery to a shorter prefix.
    for index, requested in enumerate(expected_settings['times']):
        frame_root = previous/('frame-%03d' % index)
        if not os.path.lexists(frame_root/'complete.json'):
            break
        qualified.append(_check_frame(frame_root, index, requested, expected_settings))
    completed = []
    for index, (marker, report, joint, products) in enumerate(qualified):
        old_frame = previous/('frame-%03d' % index)
        new_frame = destination/old_frame.name
        if os.path.lexists(new_frame):
            raise ValueError('Recovery refuses an existing frame directory')
        new_frame.mkdir()
        (new_frame/'joint-output').mkdir()
        for old_path, sha256 in products.items():
            new_path = new_frame/old_path.relative_to(old_frame)
            new_path.parent.mkdir(parents=True, exist_ok=True)
            _ordinary_path(old_path)
            with old_path.open('rb') as source_stream, new_path.open('xb') as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
            if sequence._digest(new_path) != sha256 or sequence._digest(old_path) != sha256:
                raise ValueError('Recovery product changed during copy')
        def rebound(path):
            old_path = _ordinary_path(path)
            if not old_path.is_relative_to(old_frame):
                raise ValueError('Recovery reference escaped frame')
            return str(new_frame/old_path.relative_to(old_frame))
        new_joint = copy.deepcopy(joint)
        new_joint['author_checkpoint'] = rebound(joint['author_checkpoint'])
        for artifact in new_joint['artifacts'].values():
            artifact['path'] = rebound(artifact['path'])
        _rebase_known_receipts(new_joint, old_frame, new_frame, rebound)
        new_joint_path = new_frame/'joint-output'/'report.json'
        _write_new_json(new_joint_path, new_joint)
        checked = joint_inspect.inspect(new_joint_path)
        if checked.get('status') != 'pass':
            raise ValueError('Copied joint output failed integrity inspection')
        new_report = copy.deepcopy(report)
        new_report.update(joint_report=sequence._reference(new_joint_path),
            joint_inspection=checked, artifacts=new_joint['artifacts'],
            author=dict(path=new_joint['author_checkpoint'], sha256=new_joint['author_sha256']))
        new_report_path = new_frame/'frame-report.json'
        _write_new_json(new_report_path, new_report)
        new_marker = copy.deepcopy(marker)
        new_marker['frame_report'] = sequence._reference(new_report_path)
        _write_new_json(new_frame/'complete.json', new_marker)
        _check_frame(new_frame, index, expected_settings['times'][index], expected_settings)
        completed.append(sequence._reference(new_frame/'complete.json'))
    return completed
