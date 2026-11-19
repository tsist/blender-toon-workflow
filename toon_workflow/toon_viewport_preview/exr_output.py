# SPDX-License-Identifier: GPL-3.0-or-later
"""Lossless scene-linear OpenEXR output through Blender's native writer.

Arrays are bottom-origin, premultiplied Linear Rec.709 RGBA. This preserves
the resolved raster values, including HDR RGB, without a display transform.
"""
import hashlib
import math
import os
from pathlib import Path
import sys
import tempfile

import numpy as np


def save_exr_new(path, pixels, *, pixel_aspect=None):
    """Create one new ZIP-compressed RGBA32F EXR, never overwrite a target."""
    import bpy

    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != '.exr' or not path.parent.is_dir():
        raise ValueError('Choose a new absolute .exr path in an existing directory')
    if path.exists() or path.is_symlink():
        raise FileExistsError('EXR already exists; choose a new filename: ' + str(path))
    if (not isinstance(pixels, np.ndarray) or pixels.dtype not in (np.float32, np.float64)
            or pixels.ndim != 3 or pixels.shape[2] != 4):
        raise ValueError('Expected a float32/float64 H x W x 4 line image')
    height, width = pixels.shape[:2]
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 16777216):
        raise ValueError('Line image exceeds the EXR output dimensions')
    if (not np.isfinite(pixels).all() or np.any(pixels[:, :, 3] < 0)
            or np.any(pixels[:, :, 3] > 1)
            or np.any(np.abs(pixels[:, :, :3]) > np.finfo(np.float32).max)):
        raise ValueError('EXR pixels must fit finite float32, with Alpha in [0, 1]')
    aspect = (1., 1.) if pixel_aspect is None else tuple(pixel_aspect)
    if len(aspect) != 2 or not all(math.isfinite(float(v)) and float(v) > 0 for v in aspect):
        raise ValueError('Pixel aspect must contain two finite positive values')

    scene = image = None
    work = None
    staged = None
    published = False
    result = None
    try:
        scene = bpy.data.scenes.new('ToonEXRWriter')
        render = scene.render
        for name, value in zip(('pixel_aspect_x', 'pixel_aspect_y'), aspect):
            prop = render.bl_rna.properties[name]
            if not prop.hard_min <= float(value) <= prop.hard_max:
                raise ValueError('Pixel aspect exceeds Blender output limits')
            setattr(render, name, float(value))
        fmt = render.image_settings
        fmt.file_format = 'OPEN_EXR'
        fmt.color_mode = 'RGBA'
        fmt.color_depth = '32'
        fmt.exr_codec = 'ZIP'
        fmt.color_management = 'OVERRIDE'
        fmt.linear_colorspace_settings.name = 'Linear Rec.709'
        image = bpy.data.images.new('ToonEXRWriter', width=width, height=height,
                                    alpha=True, float_buffer=True)
        image.colorspace_settings.name = 'Linear Rec.709'
        image.alpha_mode = 'PREMUL'
        # Native Image.save_render derives EXR pixelAspectRatio from the
        # Image/ImBuf density, rather than scene.render.pixel_aspect_*.
        px, py = render.pixel_aspect_x, render.pixel_aspect_y
        density = 72. / .0254 * min(px, py)
        image.resolution = (density / px, density / py)
        image.pixels.foreach_set(np.asarray(pixels, dtype=np.float32).reshape(-1))
        work = Path(tempfile.mkdtemp(prefix='.toon-exr-', dir=path.parent))
        staged = work / 'pixels.exr'
        image.save_render(str(staged), scene=scene)
        with staged.open('r+b') as stream:
            if stream.read(4) != b'\x76\x2f\x31\x01':
                raise ValueError('Blender did not write an OpenEXR image')
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        result = dict(path=str(path), width=width, height=height, sha256=sha,
                      format='OPEN_EXR', color_space='scene_linear_Rec709',
                      source_color_space='scene_linear_Rec709', alpha_mode='PREMUL',
                      bit_depth=32, compression='ZIP',
                      pixel_aspect=[render.pixel_aspect_x, render.pixel_aspect_y])
        # This link is the sole public commit point. Native save_render only
        # receives our newly-owned private path and cannot overwrite the target.
        os.link(staged, path)
        published = True
    finally:
        primary = sys.exc_info()[1]
        warnings = []
        for value, remove in ((image, bpy.data.images.remove), (scene, bpy.data.scenes.remove)):
            if value is not None:
                try:
                    remove(value)
                except Exception as error:
                    warnings.append('Temporary Blender data cleanup failed: ' + str(error))
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError as error:
                warnings.append('Temporary EXR cleanup failed: ' + str(staged) + ': ' + str(error))
        if work is not None:
            try:
                work.rmdir()
            except OSError as error:
                warnings.append('Temporary EXR directory cleanup failed: ' + str(work) + ': ' + str(error))
        if warnings:
            warning = '; '.join(warnings)
            if published:
                result['cleanup_warning'] = warning
            elif primary is not None:
                primary.add_note(warning)
            else:
                raise RuntimeError(warning)
    return result
