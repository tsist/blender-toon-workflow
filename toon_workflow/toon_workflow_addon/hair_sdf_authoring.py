# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit actual-UV binary keys -> independently reloaded float EXR atlases.

Only new output files and dedicated Image IDs are authored. This function never
binds a material, changes UVs, installs handlers, or saves Blender preferences.
"""
import hashlib
import json
from pathlib import Path

import numpy as np

from . import face_sdf
from .hair_sdf_compile import compile_fields

SCHEMA = 'toon-hair-sdf-keys.v1'
OUTPUT_SCHEMA = 'toon-hair-sdf-compile.v1'
SCHEMA_V2 = 'toon-hair-sdf-keys.v2'
OUTPUT_SCHEMA_V2 = 'toon-hair-sdf-compile.v2'
BINARY_TOLERANCE = 1/255 + 1e-6
READBACK_TOLERANCE = 2e-7


def _need(value, message):
    if not value:
        raise ValueError(message)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fields(value, names, label):
    _need(type(value) is dict and set(value) == set(names), 'Unexpected '+label+' fields')


def _pairs(items):
    result = {}
    for key, value in items:
        _need(key not in result, 'Duplicate plan field: '+key)
        result[key] = value
    return result


def _file_ref(value):
    _fields(value, ('file','expected_sha256'), 'image reference')
    name, digest = value['file'], value['expected_sha256']
    _need(type(name) is str and '\x00' not in name and Path(name).is_absolute()
          and Path(name).suffix.lower() == '.png', 'Explicit absolute PNG required')
    _need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
          'Image reference needs lowercase SHA256')


def _read_plan(path):
    _need(isinstance(path, (str, Path)), 'Plan path must be an absolute path')
    path = Path(path)
    _need(path.is_absolute() and path.is_file() and path.stat().st_size <= 1024*1024,
          'Choose an existing absolute JSON plan up to 1MiB')
    before = _sha(path)
    doc = json.loads(path.read_text('utf-8-sig'), object_pairs_hook=_pairs,
                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON: '+value)))
    _fields(doc, ('schema','uv','coverage','keys'), 'hair key plan')
    _need(doc['schema'] in (SCHEMA, SCHEMA_V2) and type(doc['uv']) is dict, 'Unsupported hair plan or missing UV descriptor')
    omni = doc['schema'] == SCHEMA_V2
    _file_ref(doc['coverage'])
    _need(type(doc['keys']) is list and len(doc['keys']) == (10 if omni else 8),
          'v1 requires eight cyclic keys; v2 requires eight +30 ring keys, top, bottom')
    for i, row in enumerate(doc['keys']):
        _fields(row, ('angle_degrees','elevation_degrees','mask') if omni else ('angle_degrees','mask'), 'hair key')
        angle = i*45 if i < 8 else 0
        _need(type(row['angle_degrees']) in (int,float) and row['angle_degrees'] == angle,
              'Keys must be explicitly ordered 0,45,...,315 degrees, never bool')
        if omni:
            elevation = 30 if i < 8 else (90 if i == 8 else -90)
            _need(type(row['elevation_degrees']) in (int,float) and row['elevation_degrees'] == elevation,
                  'v2 order is eight keys at +30 elevation, then unique +90 top, -90 bottom; pole yaw=0')
        _file_ref(row['mask'])
    _need(_sha(path) == before, 'Plan changed during read')
    return path, before, doc


def _binary_png(ref, expected_size=None):
    import bpy
    path = Path(ref['file']).resolve(strict=True)
    _need(path.is_file() and path.stat().st_size <= 64*1024*1024 and _sha(path) == ref['expected_sha256'],
          'PNG changed, missing or exceeds 64MiB: '+str(path))
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        image.alpha_mode = 'CHANNEL_PACKED'
        width, height = map(int, image.size)
        _need(image.source == 'FILE' and len(image.tiles) == 1 and 1 <= width <= 2048 and 1 <= height <= 2048,
              'Binary PNG must be a single image within 2048 per side')
        _need(expected_size is None or (width,height) == expected_size, 'All binary PNG dimensions must match')
        values = np.empty(width*height*4, np.float32)
        _need(len(image.pixels) == values.size, 'Binary PNG RGBA extent is unexpected')
        image.pixels.foreach_get(values)
        scalar = values.reshape(height,width,4)[:,:,0].copy()
        _need(np.isfinite(scalar).all() and np.all(np.minimum(abs(scalar),abs(scalar-1)) <= BINARY_TOLERANCE),
              'R must be binary near 0/1; grayscale/antialiased art requires explicit binary authoring')
        _need(_sha(path) == ref['expected_sha256'], 'PNG changed during read')
        return scalar >= .5, (width,height), dict(file=str(path), expected_sha256=ref['expected_sha256'],
            channel='R', colorspace='Non-Color', alpha_mode='CHANNEL_PACKED', size=[width,height],
            scalar_sha256=hashlib.sha256(scalar.astype('<f4').tobytes()).hexdigest(),
            white_pixels=int(np.count_nonzero(scalar >= .5)), binary_tolerance=BINARY_TOLERANCE)
    finally:
        bpy.data.images.remove(image)


def _active_uv(context, descriptor):
    obj = context.view_layer.objects.active
    _need(obj is not None and obj.type == 'MESH' and context.scene.objects.get(obj.name) == obj,
          'Choose the actual target Mesh in this Scene')
    _need(obj.mode == 'OBJECT', 'Compile from Object mode so the actual Mesh UV is authoritative')
    _need(type(descriptor.get('uv_name')) is str, 'Plan must contain the full named UV descriptor')
    current = face_sdf._uv_descriptor(obj.data, descriptor['uv_name'])
    _need(current == descriptor, 'Actual Mesh UV differs from the complete plan descriptor')
    return obj, current


def _write_reload(atlas, path, scene):
    import bpy
    height, width, channels = atlas.shape
    _need(channels == 4 and atlas.dtype == np.float32, 'RGBA float32 atlas required')
    generated = bpy.data.images.new('Hair SDF temporary atlas writer', width=width, height=height,
                                    alpha=True, float_buffer=True)
    loaded = None
    try:
        generated.colorspace_settings.name = 'Non-Color'
        generated.alpha_mode = 'CHANNEL_PACKED'
        generated.pixels.foreach_set(atlas.ravel())
        generated.save_render(str(path), scene=scene)
        _need(path.is_file(), 'EXR writer produced no file')
        loaded = bpy.data.images.load(str(path), check_existing=False)
        loaded.colorspace_settings.name = 'Non-Color'; loaded.alpha_mode = 'CHANNEL_PACKED'
        _need(loaded.is_float and tuple(loaded.size) == (width,height) and len(loaded.pixels) == width*height*4,
              'EXR did not retain float RGBA dimensions')
        values = np.empty(width*height*4, np.float32); loaded.pixels.foreach_get(values)
        actual = values.reshape(height,width,4)
        _need(np.isfinite(actual).all(), 'EXR readback contains nonfinite channels')
        error = float(np.max(np.abs(actual.astype(np.float64)-atlas)))
        _need(error <= READBACK_TOLERANCE, 'Float EXR roundtrip changed RGBA: '+str(error))
        receipt = dict(file=str(path), expected_sha256=_sha(path),
            channels='RGBA: four independent cyclic keys, including Alpha as DATA',
            alpha_mode='CHANNEL_PACKED', colorspace='Non-Color', storage='OPEN_EXR RGBA FLOAT32 ZIP',
            maximum_absolute_error=error, allowed_absolute_error=READBACK_TOLERANCE, all_rgba_verified=True)
        result = loaded; loaded = None
        return result, actual, receipt
    finally:
        bpy.data.images.remove(generated)
        if loaded is not None:
            bpy.data.images.remove(loaded)


def compile_omni_fields(masks, coverage):
    """Compile ten genuinely authored masks using the unchanged chart/EDT kernel.

    The pole pair is repeated only to satisfy that kernel's eight-field API;
    unused repeated fields are discarded, never substituted for author inputs.
    """
    masks = np.asarray(masks)
    _need(masks.dtype == np.bool_ and masks.ndim == 3 and masks.shape[0] == 10,
          'Omni keys must be bool[10,H,W], not inferred or implicit masks')
    ring = compile_fields(masks[:8], coverage)
    poles = compile_fields(np.stack([masks[8], masks[9]] * 4), coverage)
    height, width = masks.shape[1:]
    atlas = np.ones((height, width, 4), np.float32)
    atlas[:, :, :2] = poles['atlases'][0, :, :, :2]
    atlases = np.concatenate((ring['atlases'], atlas[None]), axis=0)
    report = dict(schema='toon-hair-sdf-fields.v2', status='passed', keys=10,
        ring_elevation_degrees=30, pole_order=['top', 'bottom'], pole_padding_BA=1.,
        atlas_channels=[[0, 45, 90, 135], [180, 225, 270, 315], ['top', 'bottom', 'constant_1', 'constant_1']],
        ring=ring['report'], poles_kernel=poles['report'],
        interpolation='cyclic linear distance on ring, piecewise linear distance by actual elevation; one final smoothstep',
        limitations=['No C1 continuity or all-direction artistic claim',
                     'All ten actual-UV source masks and hidden-surface coverage require author evidence'])
    return dict(atlases=atlases, report=report)


def compile_plan(context, plan_path, output_dir):
    """Return two v1 or three explicit-v2 Images plus report; never attach.

    PNG white=lit/black=shadow, coverage white=valid. All arrays retain Blender
    bottom-up image ordering. Failure preserves a visibly partial new directory,
    removes all owned Image/Scene IDs, and never reports success.
    """
    import bpy
    path, plan_sha, plan = _read_plan(plan_path)
    omni = plan['schema'] == SCHEMA_V2
    target, uv = _active_uv(context, plan['uv'])
    directory = Path(output_dir)
    _need(directory.is_absolute() and directory.name not in ('','.','..') and not directory.exists(),
          'Choose a new absolute output directory; existing outputs are preserved')
    parent = directory.parent.resolve(strict=True)
    _need(parent.is_dir(), 'Output parent must already exist')
    directory = parent / directory.name
    # mkdir(exist_ok=False) is also the concurrent-writer admission boundary.
    directory.mkdir()
    images, temporary_scene = [], None
    report = dict(schema=OUTPUT_SCHEMA_V2 if omni else OUTPUT_SCHEMA, status='partial', source_plan=str(path), source_plan_sha256=plan_sha,
                  target=target.name, mesh=target.data.name, uv=uv)
    marker = directory / 'PARTIAL.json'
    marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
    try:
        coverage, size, coverage_report = _binary_png(plan['coverage'])
        _need(coverage.any(), 'Coverage is empty')
        rows, sources = [], []
        for key in plan['keys']:
            white_lit, _, source = _binary_png(key['mask'], size)
            rows.append(~white_lit)
            row = dict(angle_degrees=key['angle_degrees'], **source)
            if omni:
                row['elevation_degrees'] = key['elevation_degrees']
            sources.append(row)
        masks = np.stack(rows); del rows
        compiled = compile_omni_fields(masks, coverage) if omni else compile_fields(masks, coverage)
        _need(_sha(path) == plan_sha, 'Plan changed during compilation')
        _need(face_sdf._uv_descriptor(target.data, uv['uv_name']) == uv, 'Actual UV changed during compilation')
        temporary_scene = bpy.data.scenes.new('Hair SDF independent EXR writer')
        temporary_scene.view_settings.view_transform = 'Raw'
        temporary_scene.view_settings.exposure = 0; temporary_scene.view_settings.gamma = 1
        temporary_scene.view_settings.use_curve_mapping = False
        settings = temporary_scene.render.image_settings
        settings.file_format='OPEN_EXR'; settings.color_mode='RGBA'; settings.color_depth='32'; settings.exr_codec='ZIP'
        settings.color_management='OVERRIDE'; settings.linear_colorspace_settings.name='Linear Rec.709'
        files, checks = [], []
        for index in range(3 if omni else 2):
            suffix = ('000-135', '180-315', 'top-bottom')[index]
            output = directory / ('keys-'+suffix+'.exr')
            image, actual, file_report = _write_reload(compiled['atlases'][index], output, temporary_scene)
            images.append(image)
            image.name = ('Hair SDF omni +30 ring ' if omni and index < 2 else 'Hair SDF cyclic keys ') + suffix
            if index == 2:
                image.name = 'Hair SDF omni poles top-bottom'
                _need(np.all(actual[:, :, 2:] == 1.), 'Pole atlas unused B/A must remain constant one')
                file_report['channels'] = 'R=unique +90 top; G=unique -90 bottom; B=A=constant one'
            for channel in range(2 if index == 2 else 4):
                key = index*4+channel
                mismatch = int(np.count_nonzero(((actual[:,:,channel] <= .5) != masks[key]) & coverage))
                _need(mismatch == 0, 'Reloaded EXR changed key classification: '+str(key))
                check = dict(key=key, angle_degrees=key*45 if key < 8 else 0, mismatches=mismatch)
                if omni:
                    check['elevation_degrees'] = 30 if key < 8 else (90 if key == 8 else -90)
                checks.append(check)
            file_report['image'] = image.name
            files.append(file_report)
        # No callback/interleaving is allowed to silently retarget the active Mesh.
        _need(context.view_layer.objects.active == target and face_sdf._uv_descriptor(target.data, uv['uv_name']) == uv,
              'Target or UV changed before publication')
        _need(_sha(path) == plan_sha, 'Plan changed before publication')
        for ref in [plan['coverage']] + [r['mask'] for r in plan['keys']]:
            _need(_sha(Path(ref['file'])) == ref['expected_sha256'], 'Source PNG changed before publication')
        for image in images:
            image.pack(); image.use_fake_user = True
        # Finish owned Scene cleanup before publishing a success receipt.
        bpy.data.scenes.remove(temporary_scene); temporary_scene = None
        report.update(status='passed', coverage=coverage_report, keys=sources, compiler=compiled['report'],
            files=files, images=[image.name for image in images], size=list(size),
            reloaded_key_reconstruction=checks, all_rgba_roundtrip_verified=True,
            array_coordinates='BLENDER_IMAGE_PIXELS_BOTTOM_UP; row0 corresponds V=0',
            input_binary_semantics='Keys white=lit / black=shadow; coverage white=valid',
            effects='Dedicated new Image IDs and new output files only; no binding/material/UV/Scene edits',
            art_status='not_evaluated', limitations=['Actual model sampling, cyclic interpolation and all-angle artistic quality require separate render verification'])
        pending = directory / 'COMPILE.pending.json'
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), 'utf8')
        marker.unlink()
        pending.rename(directory / 'COMPILE.json')
        return dict(images=images, report=report)
    except BaseException as exc:
        for image in images:
            bpy.data.images.remove(image)
        report.update(status='failed_partial', error=str(exc), images=[])
        marker.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), 'utf8')
        raise
    finally:
        if temporary_scene is not None:
            bpy.data.scenes.remove(temporary_scene)


__all__ = ['compile_plan', 'compile_omni_fields']
