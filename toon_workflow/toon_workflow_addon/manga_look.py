"""Standalone, pure-NumPy manga sidecar look.

The module consumes a scene-linear, premultiplied RGBA float image and returns
another scene-linear, premultiplied RGBA image.  It deliberately contains no
Blender, UI, file-I/O or external-library integration: the caller is
responsible for resolving Beauty/Line into ``image`` and for compositing the
returned MANGA sidecar.

Design invariants
-----------------
* ``mode="OFF"`` returns an exact copy of the input, including RGB values in
  fully transparent pixels.  It never re-composites Beauty or Line.
* ``mode="MANGA"`` preserves the alpha channel bit-for-bit, zeroes RGB wherever
  alpha is exactly zero, and returns premultiplied RGB.
* Tonal classification is performed on unpremultiplied RGB.  A dark
  premultiplied edge pixel is therefore not mistaken for a dark surface.
* Screen patterns are generated in full-frame pixel coordinates.  ``offset_px``
  can be set to a crop origin so that an independently processed crop matches
  the corresponding region of a full-frame result.
* Dot and cross-hatch edges use actual per-pixel coverage from a fixed 2x2 or
  4x4 sub-sample grid.  Sampling is row-tiled to keep peak memory bounded.
* The MANGA sidecar is intentionally monochrome.  It preserves silhouette
  alpha coverage, not internal hue; the caller's Combined, Beauty/Line and
  character materials are never modified.

The result is an approximate luminance-driven ink look, not a production
manga renderer.  It cannot distinguish an intrinsically dark material from a
cast shadow, and its screen pattern does not follow surface curvature.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import numpy as np

__all__ = [
    "DEFAULT_SETTINGS",
    "MangaSettingsError",
    "MODE_MANGA",
    "MODE_OFF",
    "VERSION",
    "apply",
    "normalize_settings",
]

VERSION = "1.0.0"
MODE_OFF = "OFF"
MODE_MANGA = "MANGA"
_MODES = frozenset((MODE_OFF, MODE_MANGA))

# 4x4 is the fixed upper sampling cap.  Only 2x2 and 4x4 are accepted.
_AA_CHOICES = frozenset((2, 4))
_MAX_OFFSET_PX = 1_000_000.0
_TONE_SAMPLE_TILE_PIXELS = 262_144
_SOLID_INK_LEVEL = 0.995
_HATCH_START = 0.35
_HATCH_FULL = 0.62
_SOLID_START = 0.90

# Rec. 709 luminance weights (scene-linear RGB).
_LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float64)

_DEFAULTS: dict[str, Any] = {
    "mode": MODE_OFF,
    "paper_rgb": (1.0, 1.0, 1.0),
    "ink_rgb": (0.003, 0.003, 0.003),
    "exposure": 0.0,
    "brightness": 0.0,
    "tone": 0.0,
    "paper_level": 0.88,
    "ink_level": 0.16,
    "offset_px": (0.0, 0.0),
    "dot_spacing_px": 6.0,
    "dot_angle_deg": 45.0,
    "hatch_spacing_px": 6.0,
    "hatch_angle_deg": 45.0,
    "aa_samples": 4,
}

DEFAULT_SETTINGS: Mapping[str, Any] = MappingProxyType(_DEFAULTS)
_SETTING_NAMES = frozenset(_DEFAULTS)


class MangaSettingsError(ValueError):
    """Raised when a settings mapping is malformed or out of range."""


def _number(
    value: Any,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise MangaSettingsError(f"{field} must be a finite number, not bool")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise MangaSettingsError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise MangaSettingsError(f"{field} must be finite")
    if number < minimum or number > maximum:
        raise MangaSettingsError(
            f"{field} must be in [{minimum:g}, {maximum:g}], got {number:g}"
        )
    return number


def _vector3(value: Any, field: str) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise MangaSettingsError(f"{field} must be a sequence of three numbers")
    try:
        items = list(value)
    except TypeError as exc:
        raise MangaSettingsError(
            f"{field} must be a sequence of three numbers"
        ) from exc
    if len(items) != 3:
        raise MangaSettingsError(f"{field} must contain exactly three numbers")
    values = tuple(
        _number(item, f"{field}[{index}]", 0.0, 1.0)
        for index, item in enumerate(items)
    )
    return values[0], values[1], values[2]


def _vector2_px(value: Any, field: str) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise MangaSettingsError(f"{field} must be a sequence of two numbers")
    try:
        items = list(value)
    except TypeError as exc:
        raise MangaSettingsError(
            f"{field} must be a sequence of two numbers"
        ) from exc
    if len(items) != 2:
        raise MangaSettingsError(f"{field} must contain exactly two numbers")
    values = tuple(
        _number(item, f"{field}[{index}]", -_MAX_OFFSET_PX, _MAX_OFFSET_PX)
        for index, item in enumerate(items)
    )
    return values[0], values[1]


def normalize_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and normalize MANGA settings into a fresh canonical dict.

    Unknown fields, non-finite values, booleans in numeric fields, wrong vector
    lengths and out-of-range values are rejected.  ``None`` and an empty mapping
    select the defaults, which are intentionally OFF.
    """

    if settings is None:
        supplied: Mapping[str, Any] = {}
    elif isinstance(settings, Mapping):
        supplied = settings
    else:
        raise MangaSettingsError("settings must be a mapping or None")

    unknown = sorted(str(key) for key in set(supplied) - _SETTING_NAMES)
    if unknown:
        raise MangaSettingsError(
            "unknown settings field(s): " + ", ".join(unknown)
        )

    mode = supplied.get("mode", _DEFAULTS["mode"])
    if not isinstance(mode, str) or mode not in _MODES:
        raise MangaSettingsError(
            f"mode must be one of {sorted(_MODES)!r}, got {mode!r}"
        )

    paper_rgb = _vector3(
        supplied.get("paper_rgb", _DEFAULTS["paper_rgb"]), "paper_rgb"
    )
    ink_rgb = _vector3(
        supplied.get("ink_rgb", _DEFAULTS["ink_rgb"]), "ink_rgb"
    )
    exposure = _number(
        supplied.get("exposure", _DEFAULTS["exposure"]), "exposure", -8.0, 8.0
    )
    brightness = _number(
        supplied.get("brightness", _DEFAULTS["brightness"]),
        "brightness",
        -1.0,
        1.0,
    )
    tone = _number(
        supplied.get("tone", _DEFAULTS["tone"]), "tone", -1.0, 1.0
    )
    paper_level = _number(
        supplied.get("paper_level", _DEFAULTS["paper_level"]),
        "paper_level",
        0.40,
        1.0,
    )
    ink_level = _number(
        supplied.get("ink_level", _DEFAULTS["ink_level"]),
        "ink_level",
        0.0,
        0.60,
    )
    if paper_level - ink_level < 0.15:
        raise MangaSettingsError(
            "paper_level must exceed ink_level by at least 0.15"
        )

    offset_px = _vector2_px(
        supplied.get("offset_px", _DEFAULTS["offset_px"]), "offset_px"
    )
    dot_spacing_px = _number(
        supplied.get("dot_spacing_px", _DEFAULTS["dot_spacing_px"]),
        "dot_spacing_px",
        2.0,
        128.0,
    )
    dot_angle_deg = _number(
        supplied.get("dot_angle_deg", _DEFAULTS["dot_angle_deg"]),
        "dot_angle_deg",
        -180.0,
        180.0,
    )
    hatch_spacing_px = _number(
        supplied.get("hatch_spacing_px", _DEFAULTS["hatch_spacing_px"]),
        "hatch_spacing_px",
        2.0,
        128.0,
    )
    hatch_angle_deg = _number(
        supplied.get("hatch_angle_deg", _DEFAULTS["hatch_angle_deg"]),
        "hatch_angle_deg",
        -180.0,
        180.0,
    )

    raw_aa = supplied.get("aa_samples", _DEFAULTS["aa_samples"])
    aa_number = _number(raw_aa, "aa_samples", 2.0, 4.0)
    aa_samples = int(round(aa_number))
    if aa_number != aa_samples or aa_samples not in _AA_CHOICES:
        raise MangaSettingsError("aa_samples must be either 2 or 4")

    return {
        "mode": mode,
        "paper_rgb": paper_rgb,
        "ink_rgb": ink_rgb,
        "exposure": exposure,
        "brightness": brightness,
        "tone": tone,
        "paper_level": paper_level,
        "ink_level": ink_level,
        "offset_px": offset_px,
        "dot_spacing_px": dot_spacing_px,
        "dot_angle_deg": dot_angle_deg,
        "hatch_spacing_px": hatch_spacing_px,
        "hatch_angle_deg": hatch_angle_deg,
        "aa_samples": aa_samples,
    }


def _validate_image(image: np.ndarray) -> None:
    if not isinstance(image, np.ndarray):
        raise TypeError("image must be a numpy.ndarray")
    if image.ndim != 3 or image.shape[2] != 4:
        raise ValueError("image must have shape (H, W, 4)")
    if image.shape[0] < 1 or image.shape[1] < 1:
        raise ValueError("image must have at least one row and one column")
    if not np.issubdtype(image.dtype, np.floating):
        raise TypeError("image dtype must be a floating type")
    if not np.isfinite(image).all():
        raise ValueError("image must contain only finite values")
    if np.any(image[..., 3] < 0.0):
        raise ValueError("image alpha must be non-negative")


def _srgb_encode_linear(values: np.ndarray) -> np.ndarray:
    """Map scene-linear [0, 1] values to display-referred sRGB."""

    x = np.clip(values, 0.0, 1.0)
    result = np.empty_like(x, dtype=np.float64)
    low = x <= 0.0031308
    result[low] = 12.92 * x[low]
    high = x[~low]
    result[~low] = 1.055 * np.power(high, 1.0 / 2.4) - 0.055
    return result


def _smoothstep(value: np.ndarray, low: float, high: float) -> np.ndarray:
    t = np.clip((value - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _subsample_offsets(aa_samples: int) -> tuple[float, ...]:
    step = 1.0 / aa_samples
    return tuple((index + 0.5) * step for index in range(aa_samples))


def _pattern_coverage(
    darkness: np.ndarray,
    row_start: int,
    width: int,
    settings: Mapping[str, Any],
) -> np.ndarray:
    """Return anti-aliased dot/hatch ink coverage for a row block.

    ``darkness`` is in [0, 1] (0 paper, 1 solid ink).  Coordinates include the
    half-pixel offset of each sub-sample, so the returned coverage is the actual
    fraction of sub-samples covered by ink in every output pixel.
    """

    rows = darkness.shape[0]
    coverage = np.zeros((rows, width), dtype=np.float64)

    if darkness.size == 0:
        return coverage
    if np.all(darkness <= 0.0):
        return coverage
    if np.all(darkness >= _SOLID_INK_LEVEL):
        coverage.fill(1.0)
        return coverage

    offset_x, offset_y = settings["offset_px"]
    aa_samples = settings["aa_samples"]
    offsets = _subsample_offsets(aa_samples)

    dot_spacing = settings["dot_spacing_px"]
    dot_angle = math.radians(settings["dot_angle_deg"])
    dot_cos = math.cos(dot_angle)
    dot_sin = math.sin(dot_angle)

    hatch_spacing = settings["hatch_spacing_px"]
    hatch_angle = math.radians(settings["hatch_angle_deg"])
    hatch_angles = (hatch_angle, hatch_angle + math.pi / 2.0)
    hatch_trig = tuple((math.cos(a), math.sin(a)) for a in hatch_angles)

    # Dot size and hatch width are functions of the desired darkness.  A dot's
    # area fraction is c = pi*r^2 / spacing^2.  Two orthogonal line families of
    # width fraction f cover 2f - f^2 of the plane, hence the inverse below.
    dot_radius = np.minimum(
        np.sqrt(np.clip(darkness, 0.0, 1.0) / math.pi),
        0.5,
    ) * dot_spacing
    dot_radius_sq = dot_radius * dot_radius
    hatch_half_width = (
        0.5
        * hatch_spacing
        * (1.0 - np.sqrt(np.clip(1.0 - darkness, 0.0, 1.0)))
    )
    hatch_weight = _smoothstep(darkness, _HATCH_START, _HATCH_FULL)
    solid_weight = _smoothstep(darkness, _SOLID_START, _SOLID_INK_LEVEL)

    base_x = np.arange(width, dtype=np.float64)[None, :] + offset_x
    base_y = (
        np.arange(row_start, row_start + rows, dtype=np.float64)[:, None]
        + offset_y
    )

    inv_samples = 1.0 / float(aa_samples * aa_samples)
    for sy in offsets:
        sample_y = base_y + sy
        for sx in offsets:
            sample_x = base_x + sx

            dot_u = sample_x * dot_cos + sample_y * dot_sin
            dot_v = -sample_x * dot_sin + sample_y * dot_cos
            dot_du = dot_u - np.rint(dot_u / dot_spacing) * dot_spacing
            dot_dv = dot_v - np.rint(dot_v / dot_spacing) * dot_spacing
            dot_mask = (dot_du * dot_du + dot_dv * dot_dv) < dot_radius_sq

            hatch_mask = np.zeros((rows, width), dtype=bool)
            for cos_a, sin_a in hatch_trig:
                hatch_u = sample_x * cos_a + sample_y * sin_a
                hatch_distance = np.abs(
                    hatch_u - np.rint(hatch_u / hatch_spacing) * hatch_spacing
                )
                hatch_mask |= hatch_distance < hatch_half_width

            mixed = (
                dot_mask.astype(np.float64) * (1.0 - hatch_weight)
                + hatch_mask.astype(np.float64) * hatch_weight
            )
            mixed = mixed * (1.0 - solid_weight) + solid_weight
            coverage += mixed

    coverage *= inv_samples
    return np.clip(coverage, 0.0, 1.0)


def _render_block(
    block: np.ndarray,
    row_start: int,
    settings: Mapping[str, Any],
) -> np.ndarray:
    """Render one RGBA row block.  Alpha is copied separately by the caller."""

    rgb = block[..., :3].astype(np.float64, copy=False)
    alpha = block[..., 3].astype(np.float64, copy=False)
    alpha3 = alpha[..., None]

    straight = np.zeros_like(rgb)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        np.divide(rgb, alpha3, out=straight, where=alpha3 > 0.0)
    # Extreme HDR values are legitimate in scene-linear data; clamping here
    # only keeps the display transform finite.  It prevents inf/NaN in output.
    np.clip(straight, 0.0, 1.0e6, out=straight)

    luminance = np.sum(straight * _LUMA, axis=2)
    luminance *= math.pow(2.0, settings["exposure"])

    display = _srgb_encode_linear(luminance)
    display = np.clip(display + settings["brightness"], 0.0, 1.0)
    tone_exponent = math.pow(2.0, settings["tone"])
    display = np.power(display, tone_exponent)

    paper_level = settings["paper_level"]
    ink_level = settings["ink_level"]
    darkness = 1.0 - _smoothstep(display, ink_level, paper_level)
    width = block.shape[1]
    coverage = _pattern_coverage(darkness, row_start, width, settings)

    paper = np.asarray(settings["paper_rgb"], dtype=np.float64)
    ink = np.asarray(settings["ink_rgb"], dtype=np.float64)
    gray = paper[None, None, :] * (1.0 - coverage[..., None])
    gray += ink[None, None, :] * coverage[..., None]

    premultiplied = gray * alpha3
    premultiplied[alpha3[..., 0] == 0.0] = 0.0
    return premultiplied


def apply(image: np.ndarray, settings: Mapping[str, Any] | None = None) -> np.ndarray:
    """Apply the configured manga look to a premultiplied scene-linear image.

    Parameters
    ----------
    image:
        ``float`` array with shape ``(H, W, 4)`` in scene-linear,
        premultiplied RGBA.  Alpha should be non-negative (normally 0..1).
    settings:
        Optional mapping; see :func:`normalize_settings` and ``README.md``.

    Returns
    -------
    numpy.ndarray
        A new array with the same shape and floating dtype.  OFF is an exact
        copy.  MANGA preserves alpha values bit-for-bit and zeroes RGB where
        alpha is zero.
    """

    normalized = normalize_settings(settings)
    _validate_image(image)

    if normalized["mode"] == MODE_OFF:
        # Do not unpremultiply, retone or over anything: OFF must be an exact
        # value copy, including RGB in fully transparent pixels.
        return np.array(image, copy=True, order="K")

    height, width = image.shape[:2]
    output = np.empty((height, width, 4), dtype=image.dtype)
    output[..., 3] = image[..., 3]

    rows_per_block = max(1, _TONE_SAMPLE_TILE_PIXELS // width)
    for row_start in range(0, height, rows_per_block):
        row_stop = min(height, row_start + rows_per_block)
        block = image[row_start:row_stop]
        rendered = _render_block(block, row_start, normalized)
        output[row_start:row_stop, :, :3] = rendered.astype(
            image.dtype, copy=False
        )

    return output
