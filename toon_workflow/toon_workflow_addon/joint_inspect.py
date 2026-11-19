# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent, bounded cold reader for a completed correlated output bundle."""
import hashlib
import json
from pathlib import Path
import zipfile
from .backend import pencil


def inspect(report_path):
    import numpy as np
    path = Path(report_path).resolve(strict=True)
    if path.stat().st_size > 8*1024*1024:
        raise ValueError('Joint report exceeds the reader budget')
    report = json.loads(path.read_text(encoding='utf8'))
    if report.get('schema')=='toon-native-media-artline-report.v1':
        from . import media_render
        return media_render.inspect(path)
    supported = {('toon-joint-report.v1', 'eevee_joint_grid.v1'),
                 ('toon-joint-report.v2', 'eevee_joint_grid.v2'),
                 ('toon-joint-report.v3', 'eevee_joint_grid.v3'),
                 ('toon-joint-report.v4', 'eevee_joint_grid.eye.v1'),
                 ('toon-joint-report.v5', 'eevee_joint_grid.material-ink.v1'),
                 ('toon-joint-report.v6', 'eevee_joint_grid.uv-ink.v1')}
    if ((report.get('schema'), report.get('profile')) not in supported
            or report.get('status') != 'completed'):
        raise ValueError('Expected a completed supported joint report')
    if report['schema'] in {'toon-joint-report.v2','toon-joint-report.v3','toon-joint-report.v4','toon-joint-report.v5','toon-joint-report.v6'}:
        lighting = report['lighting']
        count = lighting['samples_per_position']
        if (type(count) is not int or not 1 <= count <= 256
                or lighting['filter_size'] != 0.0
                or lighting['contract'] != ('fixed-position-managed-eye-lighting.v1'
                    if report['schema']=='toon-joint-report.v4' or report['schema'] in ('toon-joint-report.v5','toon-joint-report.v6') and 'eye_layers' in report else 'fixed-position-opaque-lighting.v1')
                or report['admission']['beauty_samples_per_position'] != count):
            raise ValueError('Unsupported fixed-position lighting contract')
    if report['schema']=='toon-joint-report.v4' or report['schema'] in ('toon-joint-report.v5','toon-joint-report.v6') and 'eye_layers' in report:
        from .eye_overlays import validate_receipt
        validate_receipt(report.get('eye_layers'))
    if 'hair_fixed_shadows' in report:
        witness=report['hair_fixed_shadows']
        if (witness.get('profile')!='toon-hair-fixed-shadow-freeze.v1'
                or witness.get('data_exact') is not True
                or not witness.get('before') or witness['before']!=witness.get('frozen')):
            raise ValueError('Fixed hair shadow freeze receipt differs')
    if (report['schema'] in ('toon-joint-report.v5','toon-joint-report.v6')) != ('line_material_colors' in report):
        raise ValueError('Material ink receipt requires the explicit v5 report profile')
    ink_count=None
    if 'line_material_colors' in report:
        from .line_material_colors import validate_receipt
        witness=report['line_material_colors']
        if (set(witness)!={'before','frozen','data_exact'} or witness['data_exact'] is not True
                or witness['before']!=witness['frozen']
                or report.get('author_style',{}).get('line_material_colors',{}).get('resolved')!=witness['before']):
            raise ValueError('Material line-color frozen receipt differs')
        ink_count=validate_receipt(witness['before'])
    uv_count=None
    has_uv=any(r['policy']['source']['kind']=='UV_IMAGE' for r in report.get('line_material_colors',{}).get('before',{}).get('policies',[]))
    if has_uv!=(report['schema']=='toon-joint-report.v6') or has_uv!=('line_uv_sources' in report):
        raise ValueError('UV source receipt requires the explicit v6 profile')
    if 'line_uv_sources' in report:
        from . import line_image_source
        uv=report['line_uv_sources']
        if (set(uv)!={'before','frozen','data_exact'} or uv['data_exact'] is not True or uv['before']!=uv['frozen']
                or report['author_style']['line_material_colors'].get('uv_sources')!=uv['before']):
            raise ValueError('UV source frozen receipt differs')
        uv_count=line_image_source.validate_receipt(uv['before'])
        expected={r['policy']['id']:r for r in report['line_material_colors']['before']['policies'] if r['policy']['source']['kind']=='UV_IMAGE'}
        if set(expected)!={r['policy_id'] for r in uv['before']['policies']}:raise ValueError('UV policy coverage differs')
        for row in uv['before']['policies']:
            source=expected[row['policy_id']]
            if row['source']!=source['policy']['source'] or row['image']!=source['source_descriptor']:raise ValueError('UV image source differs')
    root = path.parent
    normal_count = None
    face_count = None
    if 'artistic_normals' in report:
        from .normal_witness import validate_receipt, validate_head_receipt
        normal_count = validate_receipt(report['artistic_normals'])
        validate_head_receipt(report.get('head_normal_config'),report['artistic_normals'])
    if 'face_thresholds' in report:
        from .face_witness import validate_receipt as validate_face
        face_count=validate_face(report['face_thresholds'])
    sdf_count=cast_count=None
    if 'face_sdf' in report:
        from . import sdf_witness
        sdf_count=sdf_witness.validate_receipt(report['face_sdf'],report.get('face_thresholds'))
    if 'sdf_cast' in report:
        from . import sdf_witness
        cast_count=sdf_witness.validate_cast_receipt(report['sdf_cast'],report.get('face_thresholds'))
    cast_art_count=None
    if 'cast_art' in report:
        from .cast_art_witness import validate_receipt as validate_cast_art
        if (report['cast_art'].get('schema') == 'toon-face-cast-art-freeze.v2'
                and report.get('cel_finish',{}).get('render_time') is None):
            raise ValueError('Cast-art v2 requires the independently recorded joint RENDER time')
        cast_art_count=validate_cast_art(report['cast_art'],report.get('face_sdf'),report.get('face_thresholds'),
            expected_render_time=report.get('cel_finish',{}).get('render_time'))
    finish_count=None
    if 'cel_finish' in report:
        from .finish_witness import validate_receipt as validate_finish
        finish_count=validate_finish(report['cel_finish'])
    hair_map_count=None
    if 'hair_map' in report:
        from .hair_map_witness import validate_receipt as validate_hair_map
        stamp=report.get('cel_finish',{}).get('render_time')
        if stamp is None:
            raise ValueError('HM requires the independently recorded joint RENDER time')
        if not report['hair_map'].get('targets'):
            raise ValueError('HM feature requires an explicit material target')
        hair_map_count=validate_hair_map(report['hair_map'],expected_render_time=stamp)
    boundary_count=None
    if 'band_boundary' in report:
        from .band_boundary_witness import validate_receipt as validate_boundary
        stamp=report.get('cel_finish',{}).get('render_time')
        if stamp is None:
            raise ValueError('Boundary requires the independently recorded joint RENDER time')
        boundary_count=validate_boundary(report['band_boundary'],expected_render_time=stamp)
        expected_checks=2*(len(report['samples'])+1)
        if any(row['render_checks'] != expected_checks for row in report['band_boundary']['targets']):
            raise ValueError('Boundary receipt does not cover both sides of every frozen line sample')
    shot=report.get('camera_shot')
    if shot is not None:
        import re
        corrections={row['owner']:row for row in report['camera_corrections']}
        if (shot.get('profile')!='toon-camera-shots.v1' or shot.get('status')!='applied'
                or not re.fullmatch('[0-9a-f]{32}',str(shot.get('shot_id','')))
                or shot.get('camera')!=report['frame']['camera_name']
                or type(shot.get('targets')) is not list or not 1<=len(shot['targets'])<=256):
            raise ValueError('Camera-shot receipt identity differs')
        for binding in shot['targets']:
            actual=corrections.get(binding['owner'])
            if actual is None or any(actual.get(k)!=v for k,v in dict(target=binding['target'],
                    modifier_uid=binding['modifier_uid'],weight_attribute=binding['weight_attribute'],
                    camera=shot['camera'],reference=shot['reference'],strength=shot['strength']).items()):
                raise ValueError('Camera-shot target differs from actual RENDER correction')
    checked = []
    def check(filename, expected):
        member = Path(filename)
        if member.is_symlink() or not member.resolve(strict=True).is_relative_to(root):
            raise ValueError('Joint artifact is outside its output bundle')
        with member.open('rb') as stream:
            observed = hashlib.file_digest(stream, 'sha256').hexdigest()
        if observed != expected:
            raise ValueError('Joint artifact changed: '+str(member))
        checked.append(str(member))
        return member
    check(report['author_checkpoint'], report['author_sha256'])
    for row in report['artifacts'].values():
        check(row['path'], row['sha256'])
    width, height = report['frame']['output_extent']
    if not (type(width) is int and type(height) is int and 1<=width<=2048 and 1<=height<=2048):
        raise ValueError('Unsupported joint image extent')
    arrays = Path(report['artifacts']['correlated_arrays']['path'])
    names = {'Combined', 'Beauty', 'Line', 'BeautyLineInteraction'}
    with zipfile.ZipFile(arrays) as archive:
        if (set(archive.namelist()) != {name+'.npy' for name in names}
                or any(info.file_size > width*height*4*4+1024 for info in archive.infolist())):
            raise ValueError('Correlated array archive exceeds its declared extent')
    with np.load(arrays, allow_pickle=False) as archive:
        layers = {name:archive[name] for name in names}
    for name, value in layers.items():
        if value.shape != (height,width,4) or value.dtype != np.float32 or not np.isfinite(value).all():
            raise ValueError('Invalid joint layer: '+name)
        if np.any(value[:,:,3]<0) or np.any(value[:,:,3]>1):
            raise ValueError('Invalid layer Alpha: '+name)
    if report['schema'] in {'toon-joint-report.v3','toon-joint-report.v4','toon-joint-report.v5','toon-joint-report.v6'} and report['line_element_config']:
        config=report['line_element_config']
        expected={e['pass_name'] for e in config['elements']}
        if not 1<=len(expected)<=128:raise ValueError('Invalid author element count')
        element_path=Path(report['artifacts']['line_element_arrays']['path'])
        with zipfile.ZipFile(element_path) as archive:
            if (set(archive.namelist())!={name+'.npy' for name in expected}
                    or any(i.file_size>width*height*4*4+1024 for i in archive.infolist())):
                raise ValueError('Element arrays exceed declared extent')
        with np.load(element_path,allow_pickle=False) as archive:
            for name in expected:
                value=archive[name]
                if value.shape!=(height,width,4) or value.dtype!=np.float32 or not np.isfinite(value).all():
                    raise ValueError('Invalid author element array')
        validate_file = pencil('render_elements').validate_file
        validate_file(report['artifacts']['line_elements_exr']['path'],config,width,height,report['frame']['pixel_aspect'])
    from . import shot_look_output
    look_result=shot_look_output.validate(report,layers,width,height)
    reconstructed = (layers['Line'].astype(np.float64)+layers['Beauty']
                     - layers['BeautyLineInteraction'])
    error = float(np.max(abs(reconstructed-layers['Combined'])))
    maximum = max(1., *(float(abs(value).max()) for value in layers.values()))
    bound = float(8*np.finfo(np.float32).eps*maximum)
    if error > bound:
        raise ValueError('Correlated layers do not reconstruct the saved combined result')
    result = dict(status='pass', profile=report['profile'], experimental=True,
        checked_artifacts=len(checked), output_extent=[width,height],shot_look=look_result,
        reconstruction_max_error=error, float32_roundoff_bound=bound,
        frame_evaluation=report['frame']['evaluation'],
        camera_correction_count=len(report.get('camera_corrections',[])),
        artistic_normal_count=normal_count,
        face_threshold_count=face_count,
        face_sdf_count=sdf_count, sdf_cast_count=cast_count, cel_finish_count=finish_count, cast_art_count=cast_art_count,
        hair_map_count=hair_map_count, line_material_color_count=ink_count, line_uv_source_count=uv_count,
        author_frozen_reference=report.get('author_frozen_reference'),
        claim='output integrity and correlated-layer replay only; artistic and temporal acceptance are separate')
    if boundary_count is not None:
        result['band_boundary_count'] = boundary_count
    return result
