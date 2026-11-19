# SPDX-License-Identifier: GPL-3.0-or-later
"""Declared autonomous coverage profiles; no native AA equivalence claim."""
import math
import numbers


PROFILES = ('round_capsule.v1', 'extent_falloff.v1')


def options(settings):
    """Normalize once at each public layer boundary, without inventing a profile.

    round_capsule widths are core diameters with the requested outer feather.
    extent_falloff widths are outer diameters with one pixel of inward falloff;
    the legacy feather option has no effect on that profile.
    """
    profile = settings.get('coverage_profile', 'round_capsule.v1')
    if profile not in PROFILES:
        raise ValueError('Unknown viewport coverage profile: '+str(profile))
    feather = settings.get('feather_px', 1.0)
    if (not isinstance(feather, numbers.Real) or isinstance(feather, bool)
            or not math.isfinite(float(feather)) or not 0 <= feather <= 256):
        raise ValueError('Viewport feather must be finite in [0,256]')
    return dict(coverage_profile=profile,
                feather_px=float(feather) if profile == 'round_capsule.v1' else 0.0)
