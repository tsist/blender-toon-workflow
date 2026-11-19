# SPDX-License-Identifier: GPL-3.0-or-later
"""Eight cyclic shadow keys -> two RGBA image-grid distance atlases.

True means shadow. Each 8-connected coverage component is isolated while
measuring distances. Raster components are UV chart proxies, never strand IDs.
No Blender, SciPy, topology changes, or mathematical/C1 SDF claim.
"""
import hashlib
import math

import numpy as np

from .sdf_keys_core import edt_squared

SCHEMA = 'toon-hair-sdf-fields.v1'
ANGLES = tuple(range(0, 360, 45))


def _need(value, message):
    if not value:
        raise ValueError(message)


def _digest(value):
    return hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()


def _components(coverage):
    """Label 8-connected components; adjacent row runs may touch diagonally."""
    h, w = coverage.shape
    labels = np.zeros((h, w), np.int32)
    parent = [0]

    def root(label):
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    previous = []
    for y in range(h):
        changes = np.diff(np.r_[False, coverage[y], False].astype(np.int8))
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1)
        current = []
        j = 0
        for a, b in zip(starts.tolist(), ends.tolist()):
            label = len(parent); parent.append(label)
            labels[y, a:b] = label
            while j < len(previous) and previous[j][1] < a:
                j += 1
            k = j
            while k < len(previous) and previous[k][0] <= b:
                other = root(previous[k][2]); here = root(label)
                if other != here:
                    parent[max(other, here)] = min(other, here)
                k += 1
            current.append((a, b, label))
        previous = current
    mapping = np.asarray([root(i) for i in range(len(parent))], np.int32)
    roots = np.unique(mapping[1:])
    compact = np.zeros(len(parent), np.int32)
    compact[roots] = np.arange(1, len(roots) + 1, dtype=np.int32)
    labels = compact[mapping[labels]]
    n = len(roots)
    ys, xs = np.nonzero(coverage)
    ids = labels[ys, xs]
    mins_y = np.full(n+1, h, np.int32); mins_x = np.full(n+1, w, np.int32)
    maxs_y = np.full(n+1, -1, np.int32); maxs_x = np.full(n+1, -1, np.int32)
    np.minimum.at(mins_y, ids, ys); np.minimum.at(mins_x, ids, xs)
    np.maximum.at(maxs_y, ids, ys); np.maximum.at(maxs_x, ids, xs)
    boxes = [(int(mins_y[i]), int(maxs_y[i])+1, int(mins_x[i]), int(maxs_x[i])+1)
             for i in range(1, n+1)]
    return labels, boxes


def compile_fields(masks, coverage, padding_pixels=8, cap_pixels=None):
    """Return {atlases: float32[2,H,W,4], report: JSON-safe dict}.

    Key i has phase i/8 and angle i*45 degrees. Atlas 0 RGBA stores keys
    0..3; atlas 1 RGBA keys 4..7. E>.5 is lit; E<=.5 is shadow.
    Outside coverage only the nearest chart within padding may author a value.
    Equal-distance exterior ties use the earlier raster component deterministically.
    """
    masks = np.asarray(masks); coverage = np.asarray(coverage)
    _need(masks.dtype == np.bool_ and masks.ndim == 3 and masks.shape[0] == 8,
          'masks must be bool[8,H,W]; no implicit binary conversion')
    _, h, w = masks.shape
    _need(1 <= h <= 2048 and 1 <= w <= 2048 and masks.size <= 32*1024*1024,
          'Image dimensions/budget exceed 2048 per side or 32M key pixels')
    _need(coverage.dtype == np.bool_ and coverage.shape == (h, w) and coverage.any(),
          'coverage must be a nonempty bool[H,W] matching all keys')
    _need(type(padding_pixels) is int and padding_pixels >= 0,
          'padding_pixels must be a nonnegative integer, never bool')
    if cap_pixels is None:
        cap_pixels = max(h, w) * .25
    _need(type(cap_pixels) in (int, float) and math.isfinite(cap_pixels) and cap_pixels > 0,
          'cap_pixels must be finite and positive, never bool')
    cap = float(cap_pixels)
    masks_sha = _digest(masks); coverage_sha = _digest(coverage)
    labels, boxes = _components(coverage)
    atlases = np.full((2, h, w, 4), .5, np.float32)
    best = np.full((h, w), np.inf, np.float64)
    owner = labels.copy()
    constants, charts = [], []
    for chart_id, (y0, y1, x0, x1) in enumerate(boxes, 1):
        a = max(0, y0-padding_pixels); b = min(h, y1+padding_pixels)
        c = max(0, x0-padding_pixels); d = min(w, x1+padding_pixels)
        local_labels = labels[a:b, c:d]
        domain = local_labels == chart_id
        nearest = edt_squared(domain)
        outer = ((local_labels == 0) & (nearest <= padding_pixels**2)
                 & (nearest < best[a:b, c:d]))
        write = domain | outer
        best_region = best[a:b, c:d]; best_region[outer] = nearest[outer]
        owner_region = owner[a:b, c:d]; owner_region[outer] = chart_id
        charts.append(dict(chart=chart_id, valid_pixels=int(domain.sum()),
                           bbox=[y0, y1, x0, x1], padded_bbox=[a, b, c, d]))
        for key in range(8):
            shadow = masks[key, a:b, c:d] & domain
            lit = ~masks[key, a:b, c:d] & domain
            if not shadow.any() or not lit.any():
                all_shadow = not lit.any()
                signed = np.full(domain.shape, cap if all_shadow else -cap, np.float64)
                constants.append(dict(chart=chart_id, key=key,
                                      state='shadow' if all_shadow else 'lit', sentinel=float(signed.flat[0])))
            else:
                # Feature masks contain this chart's valid pixels only.
                # At any valid pixel one term is zero, giving true opposite-class distance.
                signed = np.sqrt(edt_squared(lit)) - np.sqrt(edt_squared(shadow))
            encoded = (.5 - np.clip(signed, -cap, cap)/(2*cap)).astype(np.float32)
            target = atlases[key//4, a:b, c:d, key%4]
            target[write] = encoded[write]
    checks = []
    for key in range(8):
        decoded = atlases[key//4, :, :, key%4] <= .5
        mismatch = int(np.count_nonzero((decoded != masks[key]) & coverage))
        _need(mismatch == 0, 'Float32 atlas cannot reconstruct key '+str(key))
        checks.append(dict(key=key, phase=key/8., angle_degrees=ANGLES[key], mismatches=mismatch))
    _need(np.isfinite(atlases).all(), 'Nonfinite compiled atlas')
    _need(_digest(masks) == masks_sha and _digest(coverage) == coverage_sha, 'Inputs changed during compile')
    report = dict(schema=SCHEMA, status='passed', size=[w, h], keys=8, phases=[i/8. for i in range(8)],
        atlas_channels=[[0,45,90,135],[180,225,270,315]],
        true_means='shadow', encoding='E=.5-clamp(d,-cap,cap)/(2*cap); lit iff E>.5',
        metric='Euclidean image-grid pixel-centre distance within each raster 8-connected chart; d=sqrt(distToLitSquared)-sqrt(distToShadowSquared)',
        cap_pixels=cap, padding_pixels=padding_pixels, covered_pixels=int(coverage.sum()),
        padded_pixels=int(np.count_nonzero((owner > 0) & ~coverage)), charts=charts,
        chart_connectivity=8,
        chart_domain='8-connected coverage components; raster UV chart proxies, not geometric strand IDs',
        exterior_assignment='Nearest own-chart valid pixel within padding; earlier chart wins exact distance ties',
        exterior_unassigned_value=.5, constant_chart_keys=constants, key_reconstruction=checks,
        sources=dict(masks_sha256=masks_sha, coverage_sha256=coverage_sha, unchanged=True),
        limitations=['Pixel-centre image-grid metric, not world/geodesic/continuous-contour SDF',
                     'No C1 motion, all-angle artistic quality or production performance claim',
                     'Coverage raster gaps/touching charts alter connected components; verify actual UV/render separately',
                     'Diagonal raster contact may join distinct geometric UV islands; 8-connectivity is not geometry/strand identity',
                     'Distance interpolation on exterior padding is a chart-local extension, not measured opposite-class interior distance',
                     'Finite +/-cap sentinels for constant chart keys are boundary conventions'])
    return dict(atlases=atlases, report=report)


__all__ = ['compile_fields']
