# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of the host Toon project and is distributed under the
# project's GPL-3.0 license terms.
#
# Design source (read for the formula, not copied):
#   P. F. Felzenszwalb and D. P. Huttenlocher, "Distance Transforms of
#   Sampled Functions", Theory of Computing 8 (2012) 415-428,
#   DOI:10.4086/toc.2012.v008a019, Section 2.
# The one-dimensional lower envelope of equal-curvature parabolas, the
# separable column-then-row squared distance transform, and the final square
# root are implemented independently here with NumPy.  No upstream C++ or
# other GPL project source was copied.
"""Numerical kernel for per-Toon SDF key light-angle compilation.

The metric is Euclidean distance between pixel centers on a regular image
grid.  This is deliberately *not* a mathematical space SDF, a continuous
contour/curve distance, a UV-space distance, or a production-performance
claim.  Shadow is encoded by ``True`` and means ``hard shadow = T <= phase``.

Public interface (the only supported entry points):

``edt_squared(features)``
    Exact squared Euclidean distance to the nearest ``True`` pixel center.
    Returns ``+inf`` when there is no ``True`` pixel.

``signed_distance(mask)``
    ``True`` is shadow.  Positive inside shadow, negative outside; magnitude
    is the Euclidean distance to the nearest opposite-class pixel center.
    An entirely lit mask returns ``-hypot(h, w)`` and an entirely shadowed
    mask returns ``+hypot(h, w)`` as finite sentinels, matching
    ``prototype_reference.py``.  Those diagonal sentinels are a finite
    boundary convention, not physical speeds, and no C1 continuity is claimed.

``compile_onset(phases, masks, *, progress=None)``
    Compile a key sequence into a threshold field.  Adjacent keys must be
    nested.  A pixel that changes from ``False`` at key ``i`` to ``True`` at
    key ``i + 1`` receives::

        alpha = -d_i / (d_{i + 1} - d_i)
        T = phase_i + alpha * (phase_{i + 1} - phase_i)

    where ``d_i < 0 < d_{i + 1}`` are the signed distances at the two keys.
    Every pixel must be assigned by exactly one adjacent ``0 -> 1`` interval.

The compiler keeps only the previous and current signed-distance maps.  It
never materialises all ``K`` distance maps.  Masks are read only.  The
optional ``progress(stage, index, total)`` callback is called at bounded
stages and row blocks; exceptions from it propagate unchanged and are not
swallowed as cancellation.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

import numpy as np

__all__ = ["edt_squared", "signed_distance", "compile_onset"]

SCHEMA = "toon-face-sdf-key-compile.v1"
PIXEL_BUDGET = 32 * 1024 * 1024
MAX_KEYS = 16
MAX_SIDE = 2048
ROW_BLOCK = 128

Progress = Callable[[str, int, int], None]


def _as_bool_2d(value, name: str) -> np.ndarray:
    """Validate a non-empty 2-D bool array without converting its dtype."""
    arr = np.asarray(value)
    if arr.dtype != np.bool_:
        raise ValueError(f"{name} must have dtype np.bool_, got {arr.dtype}")
    if arr.ndim != 2:
        raise ValueError(f"{name} must be 2-D, got shape {arr.shape}")
    if arr.size == 0:
        raise ValueError(f"{name} must be non-empty, got shape {arr.shape}")
    return arr


def _validate_masks(masks) -> np.ndarray:
    """Validate the 3-D bool key stack without silently converting dtype."""
    arr = np.asarray(masks)
    if arr.dtype != np.bool_:
        raise ValueError(f"masks must have dtype np.bool_, got {arr.dtype}")
    if arr.ndim != 3:
        raise ValueError(f"masks must be 3-D (K, H, W), got shape {arr.shape}")
    k, h, w = (int(v) for v in arr.shape)
    if not (2 <= k <= MAX_KEYS):
        raise ValueError(f"masks must contain 2..{MAX_KEYS} keys, got {k}")
    if not (1 <= h <= MAX_SIDE and 1 <= w <= MAX_SIDE):
        raise ValueError(
            f"masks height/width must be 1..{MAX_SIDE}, got {h}x{w}"
        )
    pixels = k * h * w
    if pixels > PIXEL_BUDGET:
        raise ValueError(
            "masks exceed the pixel budget: "
            f"K*H*W={pixels} > {PIXEL_BUDGET}"
        )
    return arr


def _validate_phases(phases, k: int) -> np.ndarray:
    """Validate phases and convert only real numeric dtypes to float64."""
    raw = np.asarray(phases)
    if raw.dtype == np.bool_:
        raise ValueError("phases must be real numeric values, not bool")
    if raw.ndim != 1:
        raise ValueError(f"phases must be 1-D, got shape {raw.shape}")
    if raw.size != k:
        raise ValueError(
            f"phases length must equal the number of masks ({k}), got {raw.size}"
        )
    # Preserve mixed Python bool provenance before NumPy numeric promotion.
    # Already numeric ndarrays carry their current values, not source history.
    if any(isinstance(v, (bool, np.bool_))
           for v in np.asarray(phases, dtype=object).flat):
        raise ValueError("phases must be real numeric values, not bool")
    if (not np.issubdtype(raw.dtype, np.number)
            or np.issubdtype(raw.dtype, np.complexfloating)):
        raise ValueError(f"phases must be real numeric values, got {raw.dtype}")
    try:
        ph = raw.astype(np.float64, copy=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"phases cannot be converted to float64: {exc}") from None
    if ph.shape != (k,):
        raise ValueError(f"phases must have shape ({k},), got {ph.shape}")
    if not np.isfinite(ph).all():
        raise ValueError("phases must all be finite (no NaN or inf)")
    if float(ph[0]) != 0.0 or float(ph[-1]) != 1.0:
        raise ValueError(
            f"phases must start at 0 and end at 1, got {float(ph[0])!r} .. "
            f"{float(ph[-1])!r}"
        )
    if not np.all(np.diff(ph) > 0.0):
        raise ValueError("phases must be strictly increasing from 0 to 1")
    return ph


def _digest_bytes(arr: np.ndarray) -> str:
    """Return a stable digest of an array's C-order bytes."""
    contiguous = np.ascontiguousarray(arr)
    return hashlib.sha256(contiguous.tobytes(order="C")).hexdigest()


def _dt1d_squared(f: np.ndarray) -> np.ndarray:
    """Lower envelope of parabolas for one sampled line.

    Minimises ``(q - p) ** 2 + f[p]`` over finite ``f[p]``.  This is the
    Section 2 algorithm with an explicit finite-sample envelope and vectorised
    sampling via ``searchsorted``.  Non-finite ``f`` samples are skipped; a
    line with no finite sample returns ``+inf`` everywhere.
    """
    n = int(f.shape[0])
    finite = np.isfinite(f)
    if not finite.any():
        return np.full(n, np.inf, dtype=np.float64)

    values = f.tolist()
    if finite.all():
        q_iter = range(n)
    else:
        q_iter = np.flatnonzero(finite).tolist()

    v = [0] * n
    z = [0.0] * (n + 1)
    k = -1

    for q in q_iter:
        fq = values[q]
        if k < 0:
            k = 0
            v[0] = q
            z[0] = -np.inf
            z[1] = np.inf
            continue
        q2 = q * q
        while True:
            p = v[k]
            s = ((fq + q2) - (values[p] + p * p)) / (2.0 * (q - p))
            if s > z[k]:
                break
            k -= 1
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = np.inf

    z_arr = np.asarray(z[:k + 2], dtype=np.float64)
    v_arr = np.asarray(v[:k + 1], dtype=np.int64)
    qs = np.arange(n, dtype=np.float64)
    envelope_index = np.searchsorted(z_arr, qs, side="right") - 1
    nearest = v_arr[envelope_index]
    out = (qs - nearest) ** 2 + f[nearest]
    return out.astype(np.float64, copy=False)


def _edt_squared(features: np.ndarray,
                 row_progress: Optional[Callable[[int, int], None]] = None
                 ) -> np.ndarray:
    """Internal exact separable EDT for a validated 2-D bool array."""
    h, w = (int(v) for v in features.shape)
    blocks = (h + ROW_BLOCK - 1) // ROW_BLOCK
    g = np.empty((h, w), dtype=np.float64)

    # First pass (columns): nearest True pixel above, then below.  Both sweeps
    # are blocked over rows so peak temporary storage is O(ROW_BLOCK * W).
    nearest = np.full(w, -(h + 1), dtype=np.int64)
    for block_index, r0 in enumerate(range(0, h, ROW_BLOCK)):
        r1 = min(h, r0 + ROW_BLOCK)
        block = features[r0:r1]
        rows = np.arange(r0, r1, dtype=np.int64)[:, None]
        above = np.where(block, rows, -(h + 1))
        above[0] = np.maximum(above[0], nearest)
        np.maximum.accumulate(above, axis=0, out=above)
        nearest = above[-1].copy()
        distances = np.minimum(rows - above, h).astype(np.float64)
        g[r0:r1] = distances
        if row_progress is not None:
            row_progress(block_index, blocks)

    nearest = np.full(w, 2 * h, dtype=np.int64)
    for block_index, r0 in enumerate(range(h - 1, -1, -ROW_BLOCK)):
        r1 = r0 + 1
        r0 = max(0, r1 - ROW_BLOCK)
        block = features[r0:r1]
        rows = np.arange(r0, r1, dtype=np.int64)[:, None]
        below = np.where(block, rows, 2 * h)
        below[-1] = np.minimum(below[-1], nearest)
        below = np.minimum.accumulate(below[::-1], axis=0)[::-1]
        nearest = below[0].copy()
        distances = np.minimum(below - rows, h).astype(np.float64)
        np.minimum(g[r0:r1], distances, out=g[r0:r1])
        if row_progress is not None:
            row_progress(block_index, blocks)

    g[g >= h] = np.inf
    np.square(g, out=g)

    # Second pass (rows): lower envelope over the column distances.
    for block_index, r0 in enumerate(range(0, h, ROW_BLOCK)):
        r1 = min(h, r0 + ROW_BLOCK)
        for r in range(r0, r1):
            g[r] = _dt1d_squared(g[r])
        if row_progress is not None:
            row_progress(block_index, blocks)
    return g


def edt_squared(features) -> np.ndarray:
    """Squared Euclidean distance to the nearest ``True`` pixel center.

    Parameters
    ----------
    features:
        Non-empty 2-D ``np.bool_`` array.

    Returns
    -------
    numpy.ndarray
        ``float64`` array of shape ``features.shape``.  Values are squared
        image-pixel distances.  If there is no ``True`` pixel, all values are
        ``+inf``.  No NaN is produced for internal all-infinite lines.
    """
    arr = _as_bool_2d(features, "features")
    return _edt_squared(arr)


def _signed_distance(mask: np.ndarray,
                     row_progress: Optional[Callable[[int, int], None]] = None
                     ) -> np.ndarray:
    """Internal signed distance with the prototype's finite sentinels."""
    h, w = (int(v) for v in mask.shape)
    diagonal = float(np.hypot(h, w))
    if mask.all():
        return np.full((h, w), diagonal, dtype=np.float64)
    if not mask.any():
        return np.full((h, w), -diagonal, dtype=np.float64)

    # Positive inside shadow: distance to the nearest False (lit) pixel.
    inside = _edt_squared(~mask, row_progress=row_progress)
    np.sqrt(inside, out=inside)

    # Negative outside shadow: distance to the nearest True (shadow) pixel.
    outside = _edt_squared(mask, row_progress=row_progress)
    np.sqrt(outside, out=outside)
    np.negative(outside, out=outside)
    np.copyto(inside, outside, where=~mask)
    return inside


def signed_distance(mask) -> np.ndarray:
    """Signed Euclidean distance to the nearest opposite-class pixel center.

    ``True`` means shadow.  The result is positive inside shadow and negative
    outside it.  An entirely lit mask returns ``-hypot(h, w)``; an entirely
    shadowed mask returns ``+hypot(h, w)``.  These finite diagonal sentinels
    match ``prototype_reference.py`` and are not physical speeds; no C1
    continuity is claimed.
    """
    arr = _as_bool_2d(mask, "mask")
    return _signed_distance(arr)


def _first_pixel(mask: np.ndarray, width: int):
    flat = int(np.argmax(mask))
    y, x = divmod(flat, width)
    return y, x


def compile_onset(phases, masks, *, progress: Optional[Progress] = None):
    """Compile nested key masks into a per-pixel hard-shadow threshold field.

    Parameters
    ----------
    phases:
        1-D finite real numeric sequence, 2..16 items, strictly increasing
        from exactly 0 to exactly 1.  Boolean arrays are rejected.
    masks:
        ``np.bool_`` array of shape ``(K, H, W)`` with ``K`` matching
        ``phases``, ``H`` and ``W`` in 1..2048, and
        ``K * H * W <= 32 * 1024 * 1024``.  ``masks[0]`` must be all False,
        ``masks[-1]`` must be all True, and adjacent masks must be nested.
        Violations are rejected; masks are never unioned automatically.
    progress:
        Optional ``progress(stage, index, total)`` callback.  Bounded stages
        are ``validate``, ``distance``, ``distance-row``, ``assign`` and
        ``verify``.  Row-block callbacks are bounded by
        ``ceil(H / 128)`` per distance pass.  Callback exceptions propagate
        unchanged and can be used for cancellation.

    Returns
    -------
    (field, report):
        ``field`` is a finite ``float64`` array strictly inside ``(0, 1)``.
        ``report`` is a JSON-encodable dict with schema
        ``toon-face-sdf-key-compile.v1``.  The function writes no files.
    """
    if progress is not None and not callable(progress):
        raise TypeError("progress must be callable or None")

    masks_arr = _validate_masks(masks)
    k, h, w = (int(v) for v in masks_arr.shape)
    ph = _validate_phases(phases, k)

    if masks_arr[0].any():
        raise ValueError("masks[0] must be entirely False (fully lit front key)")
    if not masks_arr[-1].all():
        raise ValueError("masks[-1] must be entirely True (fully shadowed back key)")

    violations = 0
    first_violation = None
    for i in range(k - 1):
        bad = masks_arr[i] & ~masks_arr[i + 1]
        count = int(np.count_nonzero(bad))
        if count:
            if first_violation is None:
                y, x = _first_pixel(bad, w)
                first_violation = (i, y, x)
            violations += count
    if violations:
        i, y, x = first_violation
        raise ValueError(
            "non-nested author masks rejected (no implicit union): "
            f"first violation at key {i} -> {i + 1}, pixel "
            f"(row={y}, col={x}); total {violations} pixel(s) lit at the "
            "earlier key but not at the later key"
        )

    masks_digest_before = _digest_bytes(masks_arr)
    phases_digest_before = _digest_bytes(ph)

    if progress is not None:
        progress("validate", 0, 1)

    field = np.full((h, w), np.nan, dtype=np.float64)
    assigned = np.zeros((h, w), dtype=np.uint8)
    interval_counts = []

    def row_callback(block_index: int, block_total: int) -> None:
        progress("distance-row", block_index, block_total)

    row_cb = row_callback if progress is not None else None

    if progress is not None:
        progress("distance", 0, k)
    prev = _signed_distance(masks_arr[0], row_progress=row_cb)

    for i in range(k - 1):
        if progress is not None:
            progress("distance", i + 1, k)
        curr = _signed_distance(masks_arr[i + 1], row_progress=row_cb)
        transition = masks_arr[i + 1] & ~masks_arr[i]
        count = int(np.count_nonzero(transition))
        interval_counts.append(count)

        if count:
            d0 = prev[transition]
            d1 = curr[transition]
            if not (np.all(d0 < 0.0) and np.all(d1 > 0.0)):
                bad = transition & ~((prev < 0.0) & (curr > 0.0))
                y, x = _first_pixel(bad, w)
                raise RuntimeError(
                    "signed-distance interpolation invariant failed "
                    f"(d_i < 0 < d_i+1) at key {i} -> {i + 1}, pixel "
                    f"(row={y}, col={x}); "
                    f"{int(np.count_nonzero(bad))} affected pixel(s)"
                )

            denominator = d1 - d0
            alpha = -d0 / denominator
            if (not np.isfinite(alpha).all()
                    or not np.all(alpha > 0.0)
                    or not np.all(alpha < 1.0)):
                y, x = _first_pixel(
                    ~((alpha > 0.0) & (alpha < 1.0)), w
                )
                raise RuntimeError(
                    "non-finite or non-interior interpolation ratio at "
                    f"key {i} -> {i + 1}, pixel (row={y}, col={x})"
                )

            threshold = ph[i] + alpha * (ph[i + 1] - ph[i])
            if not np.isfinite(threshold).all():
                y, x = _first_pixel(~np.isfinite(threshold), w)
                raise ValueError(
                    "threshold interpolation produced non-finite values at "
                    f"key {i} -> {i + 1}, pixel (row={y}, col={x})"
                )
            outside_range = (threshold <= 0.0) | (threshold >= 1.0)
            if np.any(outside_range):
                y, x = _first_pixel(outside_range, w)
                raise ValueError(
                    "float64 threshold collapsed outside (0, 1) at "
                    f"key {i} -> {i + 1}, pixel (row={y}, col={x}); "
                    f"{int(np.count_nonzero(outside_range))} pixel(s)"
                )

            overlap = transition & (assigned != 0)
            if np.any(overlap):
                y, x = _first_pixel(overlap, w)
                raise RuntimeError(
                    "pixel assigned to more than one key interval at "
                    f"key {i} -> {i + 1}, pixel (row={y}, col={x}); "
                    f"{int(np.count_nonzero(overlap))} overlap(s)"
                )
            field[transition] = threshold
            assigned[transition] = 1

        if progress is not None:
            progress("assign", i, k - 1)
        prev = curr

    del prev

    missing = assigned == 0
    if np.any(missing):
        y, x = _first_pixel(missing, w)
        raise ValueError(
            "not every pixel received exactly one adjacent 0->1 interval: "
            f"first missing pixel (row={y}, col={x}); "
            f"{int(np.count_nonzero(missing))} missing pixel(s)"
        )

    finite = np.isfinite(field)
    if not finite.all():
        y, x = _first_pixel(~finite, w)
        raise ValueError(
            "threshold field contains non-finite values at "
            f"pixel (row={y}, col={x}); "
            f"{int(np.count_nonzero(~finite))} pixel(s)"
        )

    in_range = (field > 0.0) & (field < 1.0)
    if not in_range.all():
        y, x = _first_pixel(~in_range, w)
        raise ValueError(
            "threshold field must be strictly inside (0, 1); first bad "
            f"pixel (row={y}, col={x}); "
            f"{int(np.count_nonzero(~in_range))} pixel(s)"
        )

    if progress is not None:
        progress("verify", 0, 1)

    decode_checks = []
    total_mismatch = 0
    first_mismatch = None
    for i in range(k):
        decoded = field <= ph[i]
        mismatch = decoded != masks_arr[i]
        count = int(np.count_nonzero(mismatch))
        if count and first_mismatch is None:
            y, x = _first_pixel(mismatch, w)
            first_mismatch = (i, y, x)
        total_mismatch += count
        decode_checks.append({
            "key": i,
            "phase": float(ph[i]),
            "mismatches": count,
        })
    if total_mismatch:
        i, y, x = first_mismatch
        raise ValueError(
            "float64 threshold field cannot reproduce every author key "
            f"(indistinguishable key boundary): first mismatch at key {i}, "
            f"pixel (row={y}, col={x}); total {total_mismatch} key/pixel "
            "mismatch(es)"
        )

    masks_digest_after = _digest_bytes(masks_arr)
    phases_digest_after = _digest_bytes(ph)
    if (masks_digest_before != masks_digest_after
            or phases_digest_before != phases_digest_after):
        raise RuntimeError(
            "source masks or phases changed during compile_onset; "
            "the kernel never writes its inputs, so this indicates an "
            "external mutation (for example from a progress callback)"
        )

    threshold_min = float(field.min())
    threshold_max = float(field.max())
    intervals = [
        {
            "index": i,
            "from_key": i,
            "to_key": i + 1,
            "from_phase": float(ph[i]),
            "to_phase": float(ph[i + 1]),
            "pixels": int(interval_counts[i]),
        }
        for i in range(k - 1)
    ]

    report = {
        "schema": SCHEMA,
        "k": k,
        "size": {"height": h, "width": w, "pixels": h * w},
        "phases": [float(v) for v in ph],
        "metric": (
            "Euclidean distance between pixel centers on a regular image "
            "grid; separable squared EDT followed by square root"
        ),
        "distance_convention": {
            "units": "image pixels; pixel centers at integer coordinates",
            "edt_squared": (
                "squared Euclidean distance to the nearest True pixel "
                "center; +inf when no True pixel exists"
            ),
            "signed_distance": (
                "True=shadow; positive inside shadow, negative outside; "
                "magnitude is the Euclidean distance to the nearest "
                "opposite-class pixel center"
            ),
            "all_lit_mask": "-hypot(height, width) finite sentinel",
            "all_shadow_mask": "+hypot(height, width) finite sentinel",
            "endpoint_speed": (
                "the diagonal sentinel is not a physical speed; no C1 "
                "continuity is claimed"
            ),
            "world_space": (
                "not a world-space, continuous-contour, or curve SDF"
            ),
        },
        "threshold": {
            "formula": (
                "T = p_i + (-d_i / (d_i+1 - d_i)) * (p_i+1 - p_i), "
                "with d_i < 0 < d_i+1"
            ),
            "min": threshold_min,
            "max": threshold_max,
            "finite": True,
            "strictly_between_0_and_1": True,
        },
        "assignment": {
            "intervals": intervals,
            "exactly_once_pixels": int(np.count_nonzero(assigned == 1)),
            "missing_pixels": 0,
            "overlapping_pixels": 0,
            "pixels": h * w,
            "distance_maps_retained_simultaneously": 2,
            "all_k_distance_maps_materialised": False,
        },
        "key_decode_checks": decode_checks,
        "nesting": {
            "all_adjacent_masks_nested": True,
            "rejected_pixels": 0,
        },
        "source_checks": {
            "masks_digest_before": masks_digest_before,
            "masks_digest_after": masks_digest_after,
            "phases_digest_before": phases_digest_before,
            "phases_digest_after": phases_digest_after,
            "unchanged": True,
            "kernel_writes_inputs": False,
        },
        "acceptance_scope": {
            "verified": [
                "all threshold values are finite and strictly inside (0, 1)",
                "every pixel is assigned by exactly one adjacent 0->1 interval",
                "field <= phase[i] reproduces masks[i] for every key and pixel",
                "adjacent masks are nested; no implicit union is performed",
                "mask and phase digests are unchanged across the call",
            ],
            "not_claimed": [
                "mathematical space SDF",
                "continuous contour or curve precision",
                "UV mapping or authoring UI",
                "artistic quality",
                "production performance",
            ],
        },
        "progress": {
            "stages": ["validate", "distance", "distance-row", "assign", "verify"],
            "row_block": ROW_BLOCK,
            "row_events_bounded_per_distance_pass": True,
        },
    }
    return field, report
