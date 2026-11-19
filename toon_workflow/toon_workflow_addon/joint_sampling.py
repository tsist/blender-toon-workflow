# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit correlated spatial sampling for the experimental EEVEE adapter.

The native EEVEE sample count is not reinterpreted as this profile's quality.
Each outer sample has a fixed-position EEVEE lighting integral and one
complete line image. Composite before averaging; retain E[beauty *
line_alpha] for replay. The admitted opaque line image does not depend on
the inner lighting samples; no optical or native-AA equivalence is implied.
"""
PROFILE = 'eevee_joint_grid.v3'


def admit_lighting(samples):
    if type(samples) is not int or not 1 <= samples <= 256:
        raise ValueError('Fixed-position lighting samples must be an integer in 1..256')
    return samples


def offsets(axis):
    if type(axis) is not int or axis not in (1, 2, 4, 8):
        raise ValueError('Joint grid axis must be 1, 2, 4 or 8')
    return tuple(((x + .5) / axis - .5, (y + .5) / axis - .5)
                 for y in range(axis) for x in range(axis))


def admit(width, height, axis, memory_budget_bytes, lighting_samples=64, element_count=0):
    count = len(offsets(axis))
    admit_lighting(lighting_samples)
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 2048 or not 1 <= height <= 2048):
        raise ValueError('Joint profile requires a frame of at most 2048 per side')
    # Three float64 accumulators, working float32 buffers and export copies.
    estimate = width * height * 4 * (8 * 3 + 4 * 8)
    if type(element_count) is not int or not 0 <= element_count <= 128:
        raise ValueError('Invalid author element count')
    estimate += width * height * 4 * (8 + 4*3) * element_count
    if type(memory_budget_bytes) is not int or memory_budget_bytes < estimate:
        raise ValueError('Joint image memory budget is below the declared estimate')
    return dict(profile=PROFILE, sample_count=count, samples_per_axis=axis,
                estimated_image_bytes=estimate, budget_bytes=memory_budget_bytes,
                memory_scope='image arrays only; EEVEE/GPU/geometry are not a hard quota',
                beauty_samples_per_position=lighting_samples, pixel_filter_size=0.0,
                author_element_count=element_count,
                lighting_scope='fixed spatial position; opaque surfaces without DOF or motion blur',
                native_eevee_sample_equivalent=False)


class Accumulator:
    """Accumulate correlated premultiplied linear RGBA, with bounded memory."""
    def __init__(self, width, height):
        import numpy as np
        self.shape = (height, width, 4)
        self.beauty = np.zeros(self.shape, np.float64)
        self.line = np.zeros(self.shape, np.float64)
        self.interaction = np.zeros(self.shape, np.float64)
        self.count = 0

    def add(self, beauty, line):
        import numpy as np
        for name, image in (('beauty', beauty), ('line', line)):
            if (not isinstance(image, np.ndarray) or image.shape != self.shape
                    or image.dtype.kind != 'f' or not np.isfinite(image).all()
                    or np.any(image[:, :, 3] < 0) or np.any(image[:, :, 3] > 1)):
                raise ValueError('Invalid premultiplied RGBA ' + name + ' sample')
        self.beauty += beauty
        self.line += line
        self.interaction += beauty.astype(np.float64) * line[:, :, 3:4]
        self.count += 1

    def resolve(self, expected_count):
        import numpy as np
        if self.count != expected_count or self.count == 0:
            raise ValueError('Incomplete correlated sample set')
        b = self.beauty / self.count
        l = self.line / self.count
        cross = self.interaction / self.count
        combined = l + b - cross
        # Round-off may occur in the algebraically bounded Alpha channel.
        if np.any(combined[:, :, 3] < -1e-12) or np.any(combined[:, :, 3] > 1 + 1e-12):
            raise ValueError('Correlated composition produced invalid Alpha')
        combined[:, :, 3] = np.clip(combined[:, :, 3], 0, 1)
        naive = l + b * (1 - l[:, :, 3:4])
        return dict(Combined=combined.astype(np.float32), Beauty=b.astype(np.float32),
                    Line=l.astype(np.float32), BeautyLineInteraction=cross.astype(np.float32)), dict(
                    samples=self.count, max_resolve_then_over_error=float(np.max(abs(combined-naive))),
                    pixels_with_resolve_then_over_error=int(np.any(abs(combined-naive)>1e-6, axis=2).sum()),
                    reconstruction='Combined = Line + Beauty - BeautyLineInteraction',
                    alpha_mode='PREMUL', color_space='scene_linear_Rec709')
