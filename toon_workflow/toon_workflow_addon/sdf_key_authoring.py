# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit registered-UV key masks -> new PNG16 author input.

No reader, material, UV or geometry is changed. The compiled file uses the
existing T>phase decoder, not another shader profile or sentinel convention.
"""
import hashlib
import json
import math
from pathlib import Path
import struct
import uuid
import zlib

SCHEMA = 'toon-face-sdf-key-author.v1'
OUTPUT_SCHEMA = 'toon-face-sdf-key-output.v1'
COORDINATES = 'SDF_TEXTURE_UV_TOP_LEFT'


def _need(value, message):
    if not value:
        raise ValueError(message)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value, name):
    _need(type(value) in (int, float) and math.isfinite(value), name+' must be finite numeric, never bool')
    return float(value)


def _keys(value, allowed, name):
    _need(type(value) is dict and set(value) == set(allowed), 'Unexpected '+name+' fields')


def validate_recipe(value):
    """Small external-input boundary; image data is checked in Blender later."""
    _keys(value, ('schema', 'binding', 'size', 'shadow', 'keys'), 'key recipe')
    _need(value['schema'] == SCHEMA, 'Unsupported SDF key recipe schema')
    binding = value['binding']
    _keys(binding, ('target', 'mesh', 'uv', 'mirror', 'coordinates'), 'UV binding')
    _need(all(type(binding[k]) is str and binding[k] and '\x00' not in binding[k]
              for k in ('target', 'mesh')), 'Explicit target/Mesh identity required')
    _need(type(binding['uv']) is dict and type(binding['mirror']) is bool
          and binding['coordinates'] == COORDINATES, 'Explicit registered texture-UV coordinates required')
    _need(value['shadow'] == 'WHITE', 'This profile requires white shadow / black lit masks')
    size = value['size']
    _need(type(size) is list and len(size) == 2 and all(type(v) is int and 1 <= v <= 2048 for v in size),
          'Key image dimensions must be integers in1..2048')
    rows = value['keys']
    _need(type(rows) is list and 2 <= len(rows) <= 16 and len(rows)*size[0]*size[1] <= 32*1024*1024,
          'Key count/image budget exceeded')
    phases = []
    for row in rows:
        _need(type(row) is dict, 'Key must be an object')
        if 'constant' in row:
            _keys(row, ('phase', 'constant'), 'constant key')
            _need(row['constant'] in ('LIT', 'SHADOW'), 'Unknown constant key')
        else:
            _keys(row, ('phase', 'image', 'channel', 'threshold'), 'image key')
            _keys(row['image'], ('file', 'expected_sha256'), 'key image')
            path, digest = row['image']['file'], row['image']['expected_sha256']
            _need(type(path) is str and Path(path).is_absolute() and Path(path).suffix.lower() == '.png',
                  'Each key must name an existing absolute PNG')
            _need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
                  'Each key must carry its exact lowercase SHA256')
            _need(row['channel'] in ('R', 'G', 'B'), 'Explicit key scalar channel required')
            threshold = _number(row['threshold'], 'Binary mask threshold')
            _need(0 <= threshold <= 1, 'Binary mask threshold outside0..1')
        phases.append(_number(row['phase'], 'Key phase'))
    _need(phases[0] == 0 and phases[-1] == 1 and all(a < b for a, b in zip(phases, phases[1:])),
          'Key phases must strictly increase from0 to1')
    return value


def read_recipe(path):
    path = Path(path)
    _need(path.is_absolute() and path.is_file() and path.stat().st_size <= 1024*1024,
          'Choose an existing absolute key recipe JSON up to1MiB')
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, 'Duplicate key recipe field: '+key)
            result[key] = value
        return result
    return validate_recipe(json.loads(path.read_text('utf-8-sig'), object_pairs_hook=pairs))


def _png16(codes):
    """PNG16 RGB, identical channels, top-to-bottom, no display transform."""
    import numpy as np
    _need(codes.dtype == np.uint16 and codes.ndim == 2, 'PNG16 integer array required')
    height, width = codes.shape
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)
    rgb = np.repeat(codes[..., None], 3, axis=2).astype('>u2')
    pixels = b''.join(b'\0'+row.tobytes() for row in rgb)
    header = struct.pack('>IIBBBBB', width, height, 16, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', header)+chunk(b'IDAT', zlib.compress(pixels))+chunk(b'IEND', b'')


def _read_mask(row, size):
    import bpy
    import numpy as np
    width, height = size
    if 'constant' in row:
        return np.full((height, width), row['constant'] == 'SHADOW', dtype=bool), dict(constant=row['constant'])
    path = Path(row['image']['file']).resolve(strict=True)
    _need(path.is_file() and path.stat().st_size <= 64*1024*1024 and _sha(path) == row['image']['expected_sha256'],
          'Key source image changed/missing or exceeds64MiB: '+str(path))
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        _need(image.source == 'FILE' and len(image.tiles) == 1 and tuple(image.size) == (width, height),
              'Key PNG size/type differs from registered recipe')
        values = np.empty(width*height*4, np.float32)
        _need(len(image.pixels) == len(values), 'Unexpected key RGBA extent')
        image.pixels.foreach_get(values)
        index = ('R', 'G', 'B').index(row['channel'])
        scalar = values.reshape(height, width, 4)[::-1, :, index].copy()
        _need(np.isfinite(scalar).all() and np.all((scalar >= 0)&(scalar <= 1)), 'Key contains invalid scalar data')
        _need(_sha(path) == row['image']['expected_sha256'], 'Key source image changed during read')
        mask = scalar >= row['threshold']
        return mask, dict(file=str(path), expected_sha256=row['image']['expected_sha256'],
            channel=row['channel'], threshold=row['threshold'], colorspace='Non-Color',
            source_shape=[width, height], source_is_float=bool(image.is_float),
            scalar_sha256=hashlib.sha256(scalar.astype('<f4').tobytes()).hexdigest(),
            shadow_pixels=int(mask.sum()))
    finally:
        bpy.data.images.remove(image)


def compile_keys(context, recipe, output_directory, *, progress=None):
    """Compile into a new atomic directory and return an independent Image ID.

    ``binding.uv`` must be the full actual ``sdf_witness.snapshot`` for this
    target. All key images are already registered in that same UV plane.
    Atlas unwrap/projection transfer and material binding are separate work.
    """
    import bpy
    import numpy as np
    from . import sdf_ui, sdf_witness, sdf_keys_core
    recipe = validate_recipe(recipe)
    target = sdf_ui._target(context)
    binding = recipe['binding']
    _need(target.name == binding['target'] and target.data.name == binding['mesh'],
          'Key recipe target/Mesh differs from active object')
    uv_name = binding['uv'].get('uv_name')
    current_uv = sdf_witness.snapshot(target.data, uv_name)
    _need(current_uv == binding['uv'], 'Actual UV changed; prepare/review key shapes against the current UV')
    directory = Path(output_directory)
    _need(directory.is_absolute() and directory.name not in ('', '.', '..') and not directory.exists(),
          'Choose a NEW absolute output directory; existing outputs are preserved')
    parent = directory.parent.resolve(strict=True)
    _need(parent.is_dir(), 'Output parent must already exist')
    directory = parent/directory.name
    rows, receipts = [], []
    for index, row in enumerate(recipe['keys']):
        if progress: progress('read_key', index, len(recipe['keys']))
        mask, receipt = _read_mask(row, recipe['size'])
        rows.append(mask); receipts.append(dict(phase=row['phase'], **receipt))
    masks = np.stack(rows); del rows
    field, core_report = sdf_keys_core.compile_onset([r['phase'] for r in recipe['keys']], masks, progress=progress)
    codes = np.rint(field*65535).astype(np.uint16)
    decoded = codes.astype(np.float64)/65535
    for index, phase in enumerate(core_report['phases']):
        errors = int(np.count_nonzero((decoded <= phase) != masks[index]))
        _need(errors == 0, 'PNG16 cannot preserve key '+str(index)+' ('+str(errors)+' pixels); choose representable phases')
    _need(sdf_witness.snapshot(target.data, uv_name) == current_uv,
          'UV changed during compilation; preserve author state and retry from the new snapshot')
    stage = parent/('.'+directory.name+'.partial-'+uuid.uuid4().hex)
    stage.mkdir()
    image = None
    try:
        png = stage/'onset.png'; png.write_bytes(_png16(codes))
        image = bpy.data.images.load(str(png), check_existing=False)
        image.colorspace_settings.name = 'Non-Color'
        width, height = recipe['size']; values = np.empty(width*height*4, np.float32)
        _need(tuple(image.size) == (width, height) and image.is_float and len(image.pixels) == len(values),
              'Blender did not preserve PNG16 extent/float storage')
        image.pixels.foreach_get(values)
        actual = values.reshape(height, width, 4)[::-1, :, 0]
        _need(np.isfinite(actual).all() and np.array_equal(np.rint(actual.astype(np.float64)*65535).astype(np.uint16), codes),
              'PNG16 read-back changed encoded values')
        bpy.data.images.remove(image); image = None
        report = dict(schema=OUTPUT_SCHEMA, status='passed', recipe=recipe, sources=receipts,
            binding=current_uv, mirror=binding['mirror'], coordinates=COORDINATES,
            compiler=core_report, image=dict(file=str(directory/'onset.png'), expected_sha256=_sha(png),
                bit_depth=16, channels='RGB identical', colorspace='Non-Color', channel='R',
                size=[width, height], blender_readback_codes_exact=True),
            source_masks_unchanged=True, quantized_keys_exact=True,
            shader_decoder='lit=T>phase; no always-lit sentinel at1',
            effects='Independent new author image only; no reader/material/UV/geometry edits',
            limitations=['Keys must be authored in the same registered UV plane; no automatic atlas unwrap/UV transfer',
                          'Pixel-centre distances; filtering/softness and actual model appearance require separate verification',
                          'Empty/full finite-distance boundaries do not imply physical/C1 motion'])
        (stage/'recipe.json').write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf8')
        (stage/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        _need(not directory.exists(), 'Output was created by another writer; candidate remains staged')
        stage.rename(directory)
        image = bpy.data.images.load(str(directory/'onset.png'), check_existing=False)
        image.colorspace_settings.name = 'Non-Color'; image.pack(); image.use_fake_user = True
        return dict(status='compiled', image=image.name, report=str(directory/'report.json'),
                    output_directory=str(directory), uv_map=uv_name, mirror=binding['mirror'],
                    source_sha256=report['image']['expected_sha256'])
    except Exception:
        if image is not None:
            bpy.data.images.remove(image)
        # Preserve new staged evidence on failure; it is never accepted/bound.
        raise


def compile_from_ui(context):
    """Stage the new input in the existing SDF picker; don't bind a material."""
    import bpy
    from . import sdf_ui
    settings = getattr(context.scene, sdf_ui.PROP)
    result = compile_keys(context, read_recipe(settings.key_recipe_path), settings.key_output_directory)
    settings.image = bpy.data.images[result['image']]
    settings.uv_map = result['uv_map']; settings.mirror = result['mirror']; settings.channel = 'R'
    return result
