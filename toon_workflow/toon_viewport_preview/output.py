# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable transparent PNG output from the resolved linear line layer.

Input rows use Blender/GPU bottom origin and premultiplied linear Rec.709.
PNG stores top-origin, straight sRGB RGBA8 without an artistic view transform.
Positive HDR RGB is clipped by this explicitly LDR output; Alpha is linear.
"""
import hashlib
import math
import os
from pathlib import Path
import struct
import sys
import uuid
import zlib

import numpy as np


def _validate(pixels):
    if (not isinstance(pixels, np.ndarray) or pixels.dtype not in (np.float32, np.float64)
            or pixels.ndim != 3 or pixels.shape[2] != 4):
        raise ValueError('Expected a float32/float64 H x W x 4 line image')
    height, width = pixels.shape[:2]
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 16777216):
        raise ValueError('Line image exceeds the PNG output dimensions')
    if not np.isfinite(pixels).all() or np.any(pixels < 0) or np.any(pixels[:, :, 3] > 1):
        raise ValueError('Line image must be finite, nonnegative, with Alpha in [0, 1]')
    return width, height


def _chunk(kind, payload):
    return (struct.pack('>I', len(payload)) + kind + payload
            + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))


def encode_png(pixels, *, pixel_aspect=None):
    """Encode a finite premultiplied linear layer, without modifying its array."""
    width, height = _validate(pixels)
    physical=b''
    if pixel_aspect is not None:
        if len(pixel_aspect)!=2:raise ValueError('Expected two positive pixel aspect values')
        px,py=map(float,pixel_aspect)
        if not all(math.isfinite(v) and v>0 for v in (px,py)):
            raise ValueError('Pixel aspect must be finite and positive')
        # Like Blender's PNG output, retain non-square pixels through pHYs.
        # Keep a 72 dpi baseline on the denser axis; RGBA bytes are unchanged.
        density=72/0.0254;minimum=min(px,py)
        x,y=int(density*minimum/px),int(density*minimum/py)
        if not (1<=x<=0xffffffff and 1<=y<=0xffffffff):
            raise ValueError('Pixel aspect exceeds PNG physical density precision')
        physical=_chunk(b'pHYs',struct.pack('>IIB',x,y,1))
    encoder = zlib.compressobj(6)
    compressed = []
    for source in pixels[::-1]:
        row = source.astype(np.float64, copy=True)
        rgb = np.divide(row[:, :3], row[:, 3, None],
                        out=np.zeros_like(row[:, :3]), where=row[:, 3, None] > 0)
        np.clip(rgb, 0, 1, out=rgb)
        low = rgb <= .0031308
        rgb[low] *= 12.92
        rgb[~low] = 1.055 * rgb[~low] ** (1 / 2.4) - .055
        row[:, :3] = rgb
        quantized = np.rint(row * 255).astype(np.uint8)
        quantized[quantized[:, 3] == 0, :3] = 0
        compressed.append(encoder.compress(b'\x00' + quantized.tobytes()))
    compressed.append(encoder.flush())
    return (b'\x89PNG\r\n\x1a\n'
            + _chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + _chunk(b'sRGB', b'\x00')
            + physical
            + _chunk(b'IDAT', b''.join(compressed))
            + _chunk(b'IEND', b''))


def save_png_new(path, pixels, *, pixel_aspect=None):
    """Publish one complete new PNG, refusing existing and late-arriving files."""
    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != '.png' or not path.parent.is_dir():
        raise ValueError('Choose a new absolute .png path in an existing directory')
    if path.exists() or path.is_symlink():
        raise FileExistsError('PNG already exists; choose a new filename: ' + str(path))
    encoded = encode_png(pixels,pixel_aspect=pixel_aspect)
    result = dict(path=str(path), width=pixels.shape[1], height=pixels.shape[0],
                  sha256=hashlib.sha256(encoded).hexdigest(), color_space='sRGB',
                  source_color_space='scene_linear_Rec709', alpha_mode='STRAIGHT', bit_depth=8)
    staged = path.with_name('.toon-' + uuid.uuid4().hex + '.tmp')
    created = False
    published = False
    try:
        with staged.open('xb') as stream:
            created = True
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and create-only, unlike POSIX rename.
        # An unsupported destination filesystem fails explicitly.
        os.link(staged, path)
        published = True
    finally:
        primary_error = sys.exc_info()[1]
        if created:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                warning = 'Temporary file cleanup failed: ' + str(staged) + ': ' + str(error)
                if published:
                    result.update(cleanup_warning=warning, staged_path=str(staged))
                elif primary_error is not None:
                    primary_error.add_note(warning)
                else:
                    raise
    return result


def save_image_new(path, pixels, *, pixel_aspect=None):
    """Select explicit line-image encoding by a supported file extension."""
    suffix = Path(path).suffix.lower()
    if suffix == '.png':
        return save_png_new(path, pixels, pixel_aspect=pixel_aspect)
    if suffix == '.exr':
        from .exr_output import save_exr_new
        return save_exr_new(path, pixels, pixel_aspect=pixel_aspect)
    raise ValueError('Choose a new PNG or OpenEXR filename')
