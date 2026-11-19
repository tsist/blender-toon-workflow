# SPDX-License-Identifier: GPL-3.0-or-later
"""Sampling controls from each explicitly identified request schema."""
import math
import numbers


def _sigma(value):
    if (not isinstance(value,numbers.Real) or isinstance(value,bool)
            or not math.isfinite(float(value)) or not 0<=value<=4):
        raise ValueError('Viewport post-filter sigma must be finite in [0,4]')
    return float(value)


def options(settings, *, sigma_key='post_filter_sigma'):
    if sigma_key not in ('post_filter_sigma','postfilter_sigma'):
        raise ValueError('Unknown post-filter setting identity')
    samples=settings.get('samples_per_axis',1)
    if type(samples) is not int or samples not in (1,2,4):
        raise ValueError('Viewport samples_per_axis must be 1,2,4')
    sigma=_sigma(settings.get(sigma_key,0.0))
    other='postfilter_sigma' if sigma_key=='post_filter_sigma' else 'post_filter_sigma'
    if other in settings and _sigma(settings[other])!=sigma:
        raise ValueError('Conflicting post-filter setting identities')
    return dict(samples_per_axis=samples,post_filter_sigma=sigma)
