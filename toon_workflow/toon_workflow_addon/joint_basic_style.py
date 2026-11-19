# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit BASIC width edit; never converts author data to a PRESET."""
import copy
import json
import math
from pathlib import Path

SCHEMA = 'toon-basic-line-width-edit.v1'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate style key: ' + key)
        result[key] = value
    return result


def read(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Expected a JSON style file no larger than 16MiB')
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique)


def apply(scene, document):
    """Set one static global width value, preserving every other author field."""
    from .joint_line_adapter import pencil
    from .joint_workflow import capture_author
    from .workflow_contract import fingerprint
    if (type(document) is not dict or set(document) != {'schema', 'line_width'}
            or document['schema'] != SCHEMA):
        raise ValueError('Expected toon-basic-line-width-edit.v1 with line_width only')
    value = document['line_width']
    if type(value) not in (int, float) or not 0.1 <= value <= 32 or not math.isfinite(value):
        raise ValueError('BASIC line_width must be a finite number in 0.1..32 pixels')
    if scene.library is not None or not scene.is_editable:
        raise ValueError('BASIC edit requires a local editable Scene')
    settings = scene.toon_viewport_preview
    if settings.style_mode != 'BASIC':
        raise ValueError('BASIC width edit requires BASIC mode; PRESET data are not converted')
    path = settings.path_from_id('line_width')
    if settings.is_property_readonly('line_width'):
        raise ValueError('BASIC line_width is read-only')
    ad = scene.animation_data
    if ad is not None and any(strip.type == 'META' for track in ad.nla_tracks for strip in track.strips):
        raise ValueError('BASIC static width edit does not support META NLA; edit native animation explicitly')
    if path in pencil('group_ui')._animation_paths(scene):
        raise ValueError('BASIC line_width has animation/driver references; edit those explicitly')
    before = capture_author(scene)
    previous = settings.line_width
    preset = settings.preset_text
    preset_text = preset.as_string() if preset is not None else None
    try:
        settings.line_width = value
        effective = settings.line_width
        expected = copy.deepcopy(before)
        expected['controls']['settings']['line_width'] = effective
        if expected['document'] is not None:
            # read_scene exposes current scene_controls inside its returned view;
            # these are not a write to the inactive native Text document.
            expected['document']['scene_controls']['settings']['line_width'] = effective
        if fingerprint(capture_author(scene)) != fingerprint(expected):
            raise ValueError('BASIC width edit changed unrelated author data')
        if settings.preset_text != preset or (preset is not None and preset.as_string() != preset_text):
            raise ValueError('BASIC width edit changed the inactive PRESET Text')
    except BaseException:
        settings.line_width = previous
        if fingerprint(capture_author(scene)) != fingerprint(before):
            raise RuntimeError('BASIC width edit rollback did not restore author data')
        raise
    return dict(schema=SCHEMA, line_width=effective)
