# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded OpenEXR v2 scanline structure inspection, without pixel decoding.

Original implementation from the OpenEXR File Layout design reference:
https://openexr.com/en/latest/OpenEXRFileLayout.html
Only ordinary single/multipart scanline files are supported. ZIP files also
have their chunk tables, ownership, scanline coordinates and byte bounds
checked. Compressed payloads are never decompressed or validated as pixels.
"""
import io
import math
import os
import struct


_MAGIC = b'\x76\x2f\x31\x01'
_LONG_NAMES = 0x400
_MULTIPART = 0x1000
# Finite product checker, not a general large-image archive reader.
_MAX_ZIP_CHUNKS = 1048576
_KNOWN = {
    'channels': ('chlist', None), 'compression': ('compression', 1),
    'dataWindow': ('box2i', 16), 'displayWindow': ('box2i', 16),
    'lineOrder': ('lineOrder', 1), 'pixelAspectRatio': ('float', 4),
    'screenWindowCenter': ('v2f', 8), 'screenWindowWidth': ('float', 4),
    'name': ('string', None), 'type': ('string', None),
    'chunkCount': ('int', 4), 'version': ('int', 4),
    'colorInteropID': ('string', None),
}
_REQUIRED = ('channels', 'compression', 'dataWindow', 'displayWindow',
             'lineOrder', 'pixelAspectRatio', 'screenWindowCenter', 'screenWindowWidth')


class _Reader:
    def __init__(self, stream, size, limit=None):
        self.stream, self.size, self.limit = stream, size, limit

    def check(self, count):
        end = self.stream.tell() + count
        if count < 0 or end > self.size:
            raise ValueError('Truncated EXR structure')
        if self.limit is not None and end > self.limit:
            raise ValueError('EXR header exceeds byte budget')

    def read(self, count):
        self.check(count)
        data = self.stream.read(count)
        if len(data) != count:
            raise ValueError('EXR changed or ended during inspection')
        return data

    def skip(self, count):
        self.check(count)
        self.stream.seek(count, 1)

    def string(self, maximum):
        value = bytearray()
        for unused in range(maximum + 1):
            byte = self.read(1)
            if byte == b'\0':
                return bytes(value).decode('utf-8', 'surrogateescape')
            value.extend(byte)
        raise ValueError('EXR name exceeds supported name length')


def _text(value, name):
    if len(value) > 255 or b'\0' in value:
        raise ValueError('EXR '+name+' string is invalid or exceeds 255 bytes')
    return value.decode('utf-8', 'surrogateescape')


def _channels(value, name_limit):
    reader = _Reader(io.BytesIO(value), len(value))
    channels, names = [], set()
    while True:
        name = reader.string(name_limit)
        if not name:
            break
        if name in names:
            raise ValueError('Duplicate EXR channel: '+name)
        names.add(name)
        pixel_type, linear, reserved, xs, ys = struct.unpack('<iB3sii', reader.read(16))
        if pixel_type not in (0, 1, 2) or linear not in (0, 1) or reserved != b'\0\0\0':
            raise ValueError('Invalid EXR channel descriptor')
        if xs <= 0 or ys <= 0:
            raise ValueError('EXR channel sampling must be positive')
        channels.append(dict(name=name, pixel_type=pixel_type, x_sampling=xs, y_sampling=ys))
    if not channels or reader.stream.tell() != len(value):
        raise ValueError('Empty or malformed EXR channel list')
    return channels


def _part(attributes, multipart, name_limit):
    if any(name not in attributes for name in _REQUIRED):
        raise ValueError('Missing required EXR scanline attribute')
    if multipart and any(name not in attributes for name in ('name', 'type', 'chunkCount')):
        raise ValueError('Multipart EXR requires name, type and chunkCount')
    name = _text(attributes.get('name', b''), 'part name')
    kind = _text(attributes.get('type', b'scanlineimage'), 'part type')
    if (multipart and not name) or kind != 'scanlineimage':
        raise ValueError('Only named ordinary multipart scanline EXR parts are supported')
    if 'version' in attributes and struct.unpack('<i', attributes['version'])[0] != 1:
        raise ValueError('Unsupported EXR part version')
    windows = {}
    for key in ('dataWindow', 'displayWindow'):
        window = list(struct.unpack('<4i', attributes[key]))
        if window[2] < window[0] or window[3] < window[1]:
            raise ValueError('EXR window has invalid bounds')
        windows[key] = window
    compression, line_order = attributes['compression'][0], attributes['lineOrder'][0]
    if compression > 11 or line_order > 2:
        raise ValueError('Unsupported EXR compression or line order')
    aspect = struct.unpack('<f', attributes['pixelAspectRatio'])[0]
    center = struct.unpack('<2f', attributes['screenWindowCenter'])
    screen_width = struct.unpack('<f', attributes['screenWindowWidth'])[0]
    if not math.isfinite(aspect) or aspect <= 0 or not all(map(math.isfinite, center)):
        raise ValueError('Invalid EXR aspect or screen center')
    if not math.isfinite(screen_width) or screen_width <= 0:
        raise ValueError('Invalid EXR screen window width')
    count = struct.unpack('<i', attributes['chunkCount'])[0] if 'chunkCount' in attributes else None
    if count is not None and count <= 0:
        raise ValueError('Invalid EXR chunkCount')
    data = windows['dataWindow']
    return dict(name=name, type=kind, channels=_channels(attributes['channels'], name_limit),
                data_window=data, display_window=windows['displayWindow'],
                width=data[2]-data[0]+1, height=data[3]-data[1]+1,
                compression=compression, pixel_aspect=aspect,
                color_interop_id=_text(attributes.get('colorInteropID', b''), 'colorInteropID'),
                line_order=line_order, chunk_count=count)


def _zip_chunks(stream, size, header_bytes, parts, multipart):
    counts = []
    for part in parts:
        count = (part['height'] + 15) // 16
        if part['chunk_count'] is not None and part['chunk_count'] != count:
            raise ValueError('ZIP EXR chunkCount does not match its data window')
        counts.append(count)
    total = sum(counts)
    chunk_header = 12 if multipart else 8
    data_start = header_bytes + 8 * total
    if total > _MAX_ZIP_CHUNKS:
        raise ValueError('EXR ZIP chunk count exceeds finite checker limit')
    if data_start + chunk_header * total > size:
        raise ValueError('Truncated EXR chunk table or chunk headers')
    reader = _Reader(stream, size)
    stream.seek(header_bytes)
    entries, offsets = [], set()
    for part_index, count in enumerate(counts):
        for block in range(count):
            offset = struct.unpack('<Q', reader.read(8))[0]
            if offset < data_start or offset + chunk_header > size or offset in offsets:
                raise ValueError('EXR chunk offset is duplicate or outside its data region')
            offsets.add(offset)
            entries.append((offset, part_index, block))
    intervals = []
    for offset, part_index, block in entries:
        stream.seek(offset)
        # Native OpenEXR multipart chunk ownership is a 32-bit part index.
        if multipart and struct.unpack('<i', reader.read(4))[0] != part_index:
            raise ValueError('EXR chunk belongs to a different part')
        y, packed_size = struct.unpack('<ii', reader.read(8))
        expected_y = parts[part_index]['data_window'][1] + 16 * block
        if y != expected_y or packed_size < 0:
            raise ValueError('EXR chunk scanline or signed payload size is invalid')
        end = stream.tell() + packed_size
        if end > size:
            raise ValueError('Truncated EXR chunk payload')
        intervals.append((offset, end))
    intervals.sort()
    if any(first[1] > second[0] for first, second in zip(intervals, intervals[1:])):
        raise ValueError('Overlapping EXR chunk byte ranges')
    return total


def inspect_exr(path, *, max_parts, max_header_bytes=None):
    """Inspect actual v2 scanline headers and, for ZIP, chunk byte bounds.

    ``header_bytes`` is the file offset of the first chunk offset table,
    including the 8-byte magic/version prefix. The default budget is
    4096 + 8192 * max_parts bytes. Names/string metadata are limited to
    31/255 and 255 bytes respectively; ZIP tables to 1,048,576 chunks.
    Non-ZIP ordinary scanline files receive header-only inspection, explicitly
    reported by ``scope`` and ``chunks_checked=False``. Payload content,
    decompression, pixel values and contemporaneous file immutability are
    outside this inspection contract.
    """
    if type(max_parts) is not int or max_parts <= 0:
        raise ValueError('max_parts must be a positive integer')
    budget = 4096 + 8192 * max_parts if max_header_bytes is None else max_header_bytes
    if type(budget) is not int or budget < 9:
        raise ValueError('max_header_bytes must be an integer of at least 9')
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        reader = _Reader(stream, size, budget)
        if reader.read(4) != _MAGIC:
            raise ValueError('Invalid OpenEXR magic')
        version_flags = struct.unpack('<I', reader.read(4))[0]
        version = version_flags & 0xff
        flags = version_flags & ~0xff
        if version != 2:
            raise ValueError('Unsupported OpenEXR version')
        if flags & (0x200 | 0x800):
            raise ValueError('Tiled and deep OpenEXR files are unsupported')
        if flags & ~(_LONG_NAMES | _MULTIPART):
            raise ValueError('Unknown OpenEXR version flags')
        multipart = bool(flags & _MULTIPART)
        name_limit = 255 if flags & _LONG_NAMES else 31
        parts, part_names = [], set()
        while True:
            attributes, seen = {}, set()
            while True:
                name = reader.string(name_limit)
                if not name:
                    break
                if not attributes and not seen and len(parts) >= max_parts:
                    raise ValueError('EXR part count exceeds max_parts')
                if name in seen:
                    raise ValueError('Duplicate EXR attribute: '+name)
                seen.add(name)
                kind = reader.string(name_limit)
                if not kind:
                    raise ValueError('Empty EXR attribute type')
                length = struct.unpack('<i', reader.read(4))[0]
                reader.check(length)
                if name in _KNOWN:
                    expected_type, expected_size = _KNOWN[name]
                    if kind != expected_type or (expected_size is not None and length != expected_size):
                        raise ValueError('Wrong EXR attribute type or size: '+name)
                    if name in ('name', 'type', 'colorInteropID') and length > 255:
                        raise ValueError('EXR string metadata exceeds supported length')
                    attributes[name] = reader.read(length)
                else:
                    reader.skip(length)
            if not seen:
                if multipart and parts:
                    break
                raise ValueError('Empty EXR header')
            part = _part(attributes, multipart, name_limit)
            if multipart and part['name'] in part_names:
                raise ValueError('Duplicate EXR part name')
            part_names.add(part['name'])
            parts.append(part)
            if not multipart:
                break
        header_bytes = stream.tell()
        chunks_checked = all(part['compression'] == 3 for part in parts)
        count = _zip_chunks(stream, size, header_bytes, parts, multipart) if chunks_checked else None
        return dict(version=version, multipart=multipart, header_bytes=header_bytes, parts=parts,
                    chunks_checked=chunks_checked, chunks_count=count,
                    scope='headers_and_zip_scanline_chunk_bounds' if chunks_checked else 'headers_only',
                    pixels_decoded=False)
