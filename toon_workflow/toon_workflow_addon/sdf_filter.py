# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-CPU contract for the optional face-SDF image-space filter.

This module intentionally has no ``bpy``, image or GPU dependency.  The
Blender-facing reader builder uses :func:`sampling_descriptor` to construct
nodes and :func:`validate_sampling` to re-check authored metadata; the frozen
witness uses the same functions without importing Blender.
"""
import math

PROFILE_V1 = 'toon-face-sdf-shader.v1'
PROFILE_V2 = 'toon-face-sdf-shader.v2'
PROFILE_V3 = 'toon-face-sdf-shader.v3'
PROFILE_V4 = 'toon-face-sdf-shader.v4'
PROFILE_V5 = 'toon-face-sdf-shader.v5'
PROFILE_V6 = AUTHORED_PROFILE = 'toon-face-sdf-shader.v6'
PROFILES = (PROFILE_V1, PROFILE_V2, PROFILE_V3, PROFILE_V4, PROFILE_V5, PROFILE_V6)
SUPPORTED_PROFILES = frozenset(PROFILES)

SAMPLING_SCHEMA = 'toon-face-sdf-sampling.v2'
KERNEL_PROFILE = 'toon-face-sdf-kernel-3x3-binomial.v1'
KERNEL_EXPRESSION = '3x3 [1,2,1] outer [1,2,1] / 16'
MAX_RADIUS_TEXELS = 64.0


def kernel_weights():
    """Return row-major (dx, dy, integer-weight) pairs; centre is index 4."""
    return tuple((dx, dy, (2 if dx == 0 else 1) * (2 if dy == 0 else 1))
                 for dy in (-1, 0, 1) for dx in (-1, 0, 1))


KERNEL_WEIGHTS = kernel_weights()
_SAMPLING_KEYS = frozenset({
    'schema', 'kernel_profile', 'kernel', 'radius_texels', 'spacing_texels',
    'image_width', 'image_height', 'uv_step', 'samples', 'center_index',
    'kernel_weights', 'normalization', 'interpolation', 'extension',
})


class SamplingError(ValueError):
    """Invalid filter radius, sampling descriptor or profile association."""


def validate_radius(value):
    """Accept only an explicit finite non-bool radius in the author range."""
    if type(value) is bool or type(value) not in (int, float):
        raise SamplingError('filter_radius_texels must be a finite number, never bool')
    radius = float(value)
    if not math.isfinite(radius) or not 0.0 <= radius <= MAX_RADIUS_TEXELS:
        raise SamplingError('filter_radius_texels must be finite in [0,64]')
    return radius


def profile_for_radius(radius):
    """Map the public radius onto the explicit v1/v2 shader profiles."""
    return PROFILE_V1 if validate_radius(radius) == 0.0 else PROFILE_V2


def validate_profile(profile):
    if profile not in SUPPORTED_PROFILES:
        raise SamplingError('Unknown face-SDF shader profile: ' + repr(profile))
    return profile


def _dimension(value, label):
    if type(value) is not int or value <= 0:
        raise SamplingError(label + ' must be a positive integer')
    return value


def _expected_uv_step(radius, width, height):
    return [float(radius) / float(width), float(radius) / float(height)]


def sampling_descriptor(radius_texels, image_width, image_height):
    """Describe the exact 3x3 image-space kernel for one positive radius.

    The radius is the per-axis distance in source texels between the centre and
    the outer kernel taps.  The UV step is derived from the actual image size,
    never guessed from the UV layout.
    """
    radius = validate_radius(radius_texels)
    if radius == 0.0:
        raise SamplingError('A positive filter radius is required for shader v2')
    width = _dimension(image_width, 'image_width')
    height = _dimension(image_height, 'image_height')
    return {
        'schema': SAMPLING_SCHEMA,
        'kernel_profile': KERNEL_PROFILE,
        'kernel': KERNEL_EXPRESSION,
        'radius_texels': radius,
        'spacing_texels': radius,
        'image_width': width,
        'image_height': height,
        'uv_step': _expected_uv_step(radius, width, height),
        'samples': 9,
        'center_index': 4,
        'kernel_weights': [[dx, dy, numerator]
                           for dx, dy, numerator in KERNEL_WEIGHTS],
        'normalization': 16,
        'interpolation': 'Linear',
        'extension': 'EXTEND',
    }


def _close(left, right):
    return (type(left) in (int, float) and not isinstance(left, bool)
            and math.isfinite(float(left))
            and math.isclose(float(left), float(right),
                             rel_tol=1e-12, abs_tol=0.0))


def validate_sampling(value, *, profile=PROFILE_V2,
                      image_width=None, image_height=None):
    """Validate a v2 sampling payload; v1 must not carry one at all."""
    if profile == PROFILE_V1:
        if value is not None:
            raise SamplingError('Shader v1 must not carry a v2 sampling descriptor')
        return None
    if profile not in (PROFILE_V2, PROFILE_V3, PROFILE_V4, PROFILE_V5, PROFILE_V6):
        raise SamplingError('Unknown face-SDF shader profile: ' + repr(profile))
    if type(value) is not dict or set(value) != _SAMPLING_KEYS:
        raise SamplingError('Face-SDF v2 sampling descriptor has the wrong fields')
    if value.get('schema') != SAMPLING_SCHEMA:
        raise SamplingError('Face-SDF v2 sampling schema is invalid')
    if value.get('kernel_profile') != KERNEL_PROFILE or value.get('kernel') != KERNEL_EXPRESSION:
        raise SamplingError('Face-SDF v2 sampling kernel profile is unknown')
    radius = validate_radius(value.get('radius_texels'))
    if radius == 0.0:
        raise SamplingError('Face-SDF v2 sampling radius must be positive')
    if not _close(value.get('spacing_texels'), radius):
        raise SamplingError('Face-SDF v2 spacing does not equal the author radius')
    width = _dimension(value.get('image_width'), 'image_width')
    height = _dimension(value.get('image_height'), 'image_height')
    if image_width is not None and width != image_width:
        raise SamplingError('Face-SDF v2 sampling width differs from the actual image')
    if image_height is not None and height != image_height:
        raise SamplingError('Face-SDF v2 sampling height differs from the actual image')
    step = value.get('uv_step')
    if (type(step) is not list or len(step) != 2
            or any(type(item) not in (int, float) or isinstance(item, bool)
                   or not math.isfinite(float(item)) or item <= 0.0 for item in step)):
        raise SamplingError('Face-SDF v2 UV step must be two positive finite numbers')
    expected = _expected_uv_step(radius, width, height)
    if not all(_close(actual, wanted) for actual, wanted in zip(step, expected)):
        raise SamplingError('Face-SDF v2 UV step does not match radius and image size')
    if value.get('samples') != 9 or value.get('center_index') != 4:
        raise SamplingError('Face-SDF v2 kernel must be the 3x3 nine-tap kernel')
    expected_weights = [[dx, dy, numerator]
                        for dx, dy, numerator in KERNEL_WEIGHTS]
    if value.get('kernel_weights') != expected_weights:
        raise SamplingError('Face-SDF v2 kernel weights were changed')
    if value.get('normalization') != 16:
        raise SamplingError('Face-SDF v2 kernel normalization must remain 16')
    if value.get('interpolation') != 'Linear' or value.get('extension') != 'EXTEND':
        raise SamplingError('Face-SDF v2 sampling must remain Linear/EXTEND')
    return dict(value)


def sampling_for_radius(radius_texels, image_width, image_height):
    """Return ``(profile, sampling_or_none)`` for the public author radius."""
    radius = validate_radius(radius_texels)
    if radius == 0.0:
        return PROFILE_V1, None
    return PROFILE_V2, sampling_descriptor(radius, image_width, image_height)
