# SPDX-License-Identifier: GPL-3.0-or-later
"""Create-only native multipart EXR output from resolved premultiplied layers."""
import hashlib
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import numpy as np

from .exr_header import inspect_exr


def save_multilayer_new(path, passes, *, pixel_aspect=(1, 1)):
    """Create a new ZIP RGBA32F multipart EXR, preserving pass insertion order.

    Arrays are bottom-origin, scene-linear Rec.709 and premultiplied RGBA.
    Alpha is in [0,1]; HDR/negative RGB and zero-alpha RGB are retained.
    Combined must be the first dict entry (Blender emits it first natively).
    This synchronous native render must run outside another active render.
    Its temporary scene never shares source objects, but Blender's transient
    global Render Result is replaced by the native render operation.
    """
    import bpy

    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != '.exr' or not path.parent.is_dir():
        raise ValueError('Choose a new absolute .exr path in an existing directory')
    if path.exists() or path.is_symlink():
        raise FileExistsError('EXR already exists; choose a new filename: '+str(path))
    if type(passes) is not dict or not passes or 'Combined' not in passes:
        raise ValueError('Passes must be an ordered dict containing Combined')
    if next(iter(passes)) != 'Combined':
        raise ValueError('Combined must be the first pass to preserve native output order')
    shape = None
    for name, pixels in passes.items():
        if type(name) is not str or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,62}', name) is None:
            raise ValueError('Pass names must be 1..63 safe ASCII letters, digits or underscores')
        if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.float32
                or pixels.ndim != 3 or pixels.shape[2] != 4):
            raise ValueError('Every pass must be a float32 H x W x 4 array')
        if shape is None:
            shape = pixels.shape
        if pixels.shape != shape:
            raise ValueError('All passes must have the same image dimensions')
        if not np.isfinite(pixels).all() or np.any(pixels[:, :, 3] < 0) or np.any(pixels[:, :, 3] > 1):
            raise ValueError('Passes require finite float32 RGB and Alpha in [0,1]')
    height, width = shape[:2]
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and width*height <= 16777216):
        raise ValueError('Multilayer output exceeds supported dimensions')
    aspect = tuple(pixel_aspect)
    if len(aspect) != 2 or not all(math.isfinite(float(v)) and float(v) > 0 for v in aspect):
        raise ValueError('Pixel aspect must contain two finite positive values')
    # Freeze names and array ownership references for the synchronous callback.
    resolved = dict(passes)
    caller_window = bpy.context.window
    caller_scene = bpy.context.scene
    window_scene = caller_window.scene if caller_window is not None else None
    window_layer = caller_window.view_layer if caller_window is not None else None
    scene = camera = camera_data = None
    engine_class = None
    registered = False
    work = staged = None
    published = False
    outcome = {}
    result = None
    try:
        scene = bpy.data.scenes.new('ToonMultilayerWriter')
        layer = scene.view_layers[0]
        layer.name = 'ToonLines'
        layer.use_pass_combined = True
        camera_data = bpy.data.cameras.new('ToonMultilayerWriter')
        camera = bpy.data.objects.new('ToonMultilayerWriter', camera_data)
        scene.collection.objects.link(camera)
        scene.camera = camera
        render = scene.render
        for name, value in zip(('pixel_aspect_x', 'pixel_aspect_y'), aspect):
            prop = render.bl_rna.properties[name]
            if not prop.hard_min <= float(value) <= prop.hard_max:
                raise ValueError('Pixel aspect exceeds Blender output limits')
            setattr(render, name, float(value))
        # RNA resolution has a minimum of four; this gives exact 1x1 and 2x3
        # output without resampling or padding the supplied resolved arrays.
        render.resolution_x = width*4
        render.resolution_y = height*4
        render.resolution_percentage = 25
        if (render.resolution_x, render.resolution_y) != (width*4, height*4):
            raise ValueError('Blender clamped the requested output dimensions')
        render.use_border = False
        render.use_crop_to_border = False
        render.use_compositing = False
        render.use_sequencer = False
        render.use_file_extension = True
        fmt = render.image_settings
        fmt.media_type = 'MULTI_LAYER_IMAGE'
        fmt.file_format = 'OPEN_EXR_MULTILAYER'
        fmt.color_mode = 'RGBA'
        fmt.color_depth = '32'
        fmt.exr_codec = 'ZIP'
        fmt.use_exr_interleave = False
        fmt.use_preview = False
        fmt.color_management = 'OVERRIDE'
        fmt.linear_colorspace_settings.name = 'Linear Rec.709'
        work = Path(tempfile.mkdtemp(prefix='.m-', dir=path.parent))
        staged = work/'pixels.exr'
        render.filepath = str(staged)

        class ToonMultilayerWriter(bpy.types.RenderEngine):
            bl_idname = 'TOON_EXR_PACK_'+uuid.uuid4().hex[:16]
            bl_label = 'Toon Native Multilayer Writer'
            bl_use_preview = False
            bl_use_postprocess = False

            def update_render_passes(self, source_scene, source_layer):
                for pass_name in resolved:
                    self.register_pass(source_scene, source_layer, pass_name, 4, 'RGBA', 'COLOR')

            def render(self, depsgraph):
                tile = None
                try:
                    for pass_name in resolved:
                        if pass_name != 'Combined':
                            self.add_pass(pass_name, 4, 'RGBA', layer=layer.name)
                    tile = self.begin_result(0, 0, width, height, layer=layer.name)
                    actual = tile.layers[0].passes
                    for pass_name, pixels in resolved.items():
                        actual[pass_name].rect = pixels.reshape(width*height, 4)
                    self.end_result(tile)
                    tile = None
                    outcome['complete'] = True
                except Exception as error:
                    outcome['error'] = error
                    if tile is not None:
                        self.end_result(tile, cancel=True, do_merge_results=False)

        engine_class = ToonMultilayerWriter
        bpy.utils.register_class(engine_class)
        registered = True
        render.engine = engine_class.bl_idname
        status = bpy.ops.render.render(write_still=True, scene=scene.name, layer=layer.name)
        if 'error' in outcome:
            raise RuntimeError('Native multilayer render callback failed') from outcome['error']
        if status != {'FINISHED'} or not outcome.get('complete'):
            raise RuntimeError('Native multilayer render did not complete')
        header = inspect_exr(staged, max_parts=len(resolved))
        actual_names = [part['name'] for part in header['parts']]
        if actual_names != ['ToonLines.'+name for name in resolved] or not header['chunks_checked']:
            raise ValueError('Native EXR pass structure or ordering differs from the request: '+repr(actual_names))
        for part, pass_name in zip(header['parts'], resolved):
            expected_channels = {'ToonLines.'+pass_name+'.'+channel for channel in 'RGBA'}
            if ((part['width'], part['height']) != (width, height) or part['compression'] != 3
                    or part['data_window'] != [0, 0, width-1, height-1]
                    or part['display_window'] != [0, 0, width-1, height-1]
                    or not math.isclose(part['pixel_aspect'], render.pixel_aspect_x/render.pixel_aspect_y, rel_tol=2e-6)
                    or part['color_interop_id'] != 'lin_rec709_scene'
                    or {channel['name'] for channel in part['channels']} != expected_channels
                    or any(channel['pixel_type'] != 2 or channel['x_sampling'] != 1
                           or channel['y_sampling'] != 1 for channel in part['channels'])):
                raise ValueError('Native EXR dimensions or channel encoding differs from the request')
        with staged.open('r+b') as stream:
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        result = dict(path=str(path), width=width, height=height, sha256=digest,
                      passes=list(resolved), pass_names=actual_names, view_layer='ToonLines',
                      pixel_aspect=[render.pixel_aspect_x, render.pixel_aspect_y],
                      contract='toon.multilayer-exr.v1', format='OPEN_EXR_MULTILAYER',
                      color_space='scene_linear_Rec709', source_color_space='scene_linear_Rec709',
                      alpha_mode='PREMUL', bit_depth=32, compression='ZIP',
                      header=header)
        os.link(staged, path)
        published = True
    finally:
        primary = sys.exc_info()[1]
        warnings = []
        if caller_window is not None:
            try:
                if caller_window.scene != window_scene:
                    caller_window.scene = window_scene
                if caller_window.view_layer != window_layer:
                    caller_window.view_layer = window_layer
            except Exception as error:
                warnings.append('Caller window restoration failed: '+str(error))
        for value, remove in ((scene, bpy.data.scenes.remove), (camera, bpy.data.objects.remove),
                              (camera_data, bpy.data.cameras.remove)):
            if value is not None:
                try:
                    remove(value)
                except Exception as error:
                    warnings.append('Temporary Blender data cleanup failed: '+str(error))
        if registered:
            try:
                bpy.utils.unregister_class(engine_class)
            except Exception as error:
                warnings.append('Temporary RenderEngine cleanup failed: '+str(error))
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError as error:
                warnings.append('Temporary EXR cleanup failed: '+str(staged)+': '+str(error))
        if work is not None:
            try:
                work.rmdir()
            except OSError as error:
                warnings.append('Temporary EXR directory cleanup failed: '+str(work)+': '+str(error))
        try:
            if bpy.context.scene != caller_scene:
                warnings.append('Caller context scene identity differs after native render')
        except Exception as error:
            warnings.append('Caller context identity check failed: '+str(error))
        if warnings:
            warning = '; '.join(warnings)
            if published:
                result['cleanup_warning'] = warning
            elif primary is not None:
                primary.add_note(warning)
            else:
                raise RuntimeError(warning)
    return result
