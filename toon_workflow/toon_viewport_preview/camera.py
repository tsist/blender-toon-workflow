# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit camera frame and border extraction for the viewport line pass.

``capture`` reads only the active scene camera and render resolution.  It does
not render, mutate scene data, save preferences, or use render-engine
subdivision.  ``projection``/``mvp`` always describe the complete effective
frame so the viewport geometry is independent of border cropping.
"""
import numpy as np

_MAX_SIDE = 8192
_MAX_PIXELS = 16777216


def _matrix_rows(matrix):
    return [[float(value) for value in row] for row in matrix]


def _effective_extent(value, percentage):
    # Match the integer floor used by the render resolution code.
    return int(value) * int(percentage) // 100


def _border_rect(render, width, height):
    coords = (float(render.border_min_x), float(render.border_max_x),
              float(render.border_min_y), float(render.border_max_y))
    if not all(np.isfinite(value) and 0.0 <= value <= 1.0 for value in coords):
        raise ValueError('Render border coordinates must be finite and within 0..1')
    # BKE_render_resolution performs the bound conversion in C float
    # precision.  Using Python float64 here can move a bound by one pixel.
    x0 = int(np.float32(coords[0]) * np.float32(width))
    x1 = int(np.float32(coords[1]) * np.float32(width))
    y0 = int(np.float32(coords[2]) * np.float32(height))
    y1 = int(np.float32(coords[3]) * np.float32(height))
    return x0, y0, x1, y1


def capture(context):
    """Return an explicit VIEWPORT_LIMITED frame for the active scene camera.

    The returned top-level keys are width, height, output_width, output_height,
    view, projection, mvp, perspective, signature and metadata.  width/height
    are the complete effective frame; output_* are the border-cropped extent
    when ``use_border`` and ``use_crop_to_border`` are both enabled.  The
    matrices always use the complete frame.  Metadata extents are
    ``[width, height]`` and ``border_rect_px`` is ``[x0, y0, x1, y1]`` in
    bottom-origin pixels.
    """
    scene = getattr(context, 'scene', None)
    if scene is None:
        raise ValueError('Camera capture requires a scene context')
    depsgraph = context.evaluated_depsgraph_get()
    if depsgraph.mode != 'VIEWPORT':
        raise ValueError('Camera line output requires viewport evaluation')
    return _capture(scene, depsgraph, 'VIEWPORT_LIMITED')


def capture_render(scene, depsgraph, *, camera=None):
    """Capture the actual camera supplied by an isolated render-engine callback."""
    if depsgraph.mode != 'RENDER':
        raise ValueError('Render camera capture requires a RENDER depsgraph')
    return _capture(scene, depsgraph, 'RENDER', camera=camera)


def _capture(scene, depsgraph, evaluation, *, camera=None):
    camera = camera if camera is not None else scene.camera
    if camera is None or camera.type != 'CAMERA' or camera.data is None:
        raise ValueError('Scene has no active camera')
    render = scene.render
    if render.use_multiview:
        raise ValueError('Multiview rendering is not supported')
    evaluated = camera.evaluated_get(depsgraph)
    camera_type = evaluated.data.type
    if camera_type not in {'PERSP', 'ORTHO'}:
        raise ValueError(
            "Unsupported camera type %r; only PERSP and ORTHO are supported"
            % camera_type)

    percentage = int(render.resolution_percentage)
    width = _effective_extent(render.resolution_x, percentage)
    height = _effective_extent(render.resolution_y, percentage)
    if not (1 <= width <= _MAX_SIDE and 1 <= height <= _MAX_SIDE
            and width * height <= _MAX_PIXELS):
        raise ValueError(
            'Effective frame %dx%d exceeds 8192 pixels per side or 16777216 '
            'total pixels' % (width, height))

    aspect_x = float(render.pixel_aspect_x)
    aspect_y = float(render.pixel_aspect_y)
    if not (np.isfinite(aspect_x) and np.isfinite(aspect_y)
            and aspect_x > 0.0 and aspect_y > 0.0):
        raise ValueError('Render pixel aspect must be finite and positive')

    use_border = bool(render.use_border)
    use_crop = bool(render.use_crop_to_border)
    if use_border:
        x0, y0, x1, y1 = _border_rect(render, width, height)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(
                'Render border is empty after pixel rounding: %r'
                % ((x0, y0, x1, y1),))
        border_coords = tuple(float(value) for value in (
            render.border_min_x, render.border_max_x,
            render.border_min_y, render.border_max_y))
    else:
        x0, y0, x1, y1 = 0, 0, width, height
        border_coords = ()
    border_px = [int(x0), int(y0), int(x1), int(y1)]
    if use_border and use_crop:
        output_width = x1 - x0
        output_height = y1 - y0
    else:
        output_width = width
        output_height = height

    normalized_world = evaluated.matrix_world.normalized()
    view = normalized_world.inverted()
    projection = evaluated.calc_matrix_camera(
        depsgraph, x=width, y=height, scale_x=aspect_x, scale_y=aspect_y)
    mvp = projection @ view

    normalized_rows = _matrix_rows(normalized_world)
    view_rows = _matrix_rows(view)
    projection_rows = _matrix_rows(projection)
    mvp_rows = _matrix_rows(mvp)
    if not all(np.isfinite(value) for matrix in (view_rows, projection_rows, mvp_rows)
               for row in matrix for value in row):
        raise ValueError('Camera transform and projection must be finite')
    frame = int(scene.frame_current)
    subframe = float(scene.frame_subframe)

    signature = (
        'toon-camera-render.v1' if evaluation == 'RENDER' else 'toon-camera-viewport.v1',
        camera.as_pointer(), camera.name, camera_type,
        frame, subframe,
        tuple(tuple(row) for row in view_rows),
        tuple(tuple(row) for row in projection_rows),
        int(width), int(height),
        int(output_width), int(output_height),
        aspect_x, aspect_y,
        use_border, use_crop,
        border_coords,
        tuple(border_px),
    )
    metadata = {
        'camera_name': camera.name,
        'camera_type': camera_type,
        'frame': frame,
        'frame_subframe': subframe,
        'full_extent': [int(width), int(height)],
        'output_extent': [int(output_width), int(output_height)],
        'resolution_percentage': percentage,
        'pixel_aspect': [aspect_x, aspect_y],
        'border_rect_px': border_px,
        'use_border': use_border,
        'use_crop_to_border': use_crop,
        'normalized_camera_world': normalized_rows,
        'view': view_rows,
        'projection': projection_rows,
        'mvp': mvp_rows,
        'evaluation': evaluation,
        'depsgraph_mode': depsgraph.mode,
        'render_equivalent': False,
        'render_subdivision_claimed': evaluation == 'RENDER',
    }
    return dict(width=int(width), height=int(height),
                output_width=int(output_width), output_height=int(output_height),
                view=view, projection=projection, mvp=mvp,
                perspective=bool(camera_type == 'PERSP'),
                signature=signature, metadata=metadata)


def extract_pixels(frame, pixels):
    """Return a new float32 bottom-origin view of a complete line frame.

    ``pixels`` must be the complete ``frame['height'] x frame['width']``
    premultiplied linear RGBA image.  The input is never modified and no color
    transform is applied.  With a cropped border only the bottom-origin
    ``[y0:y1, x0:x1]`` rectangle is returned; without cropping the complete
    extent is returned with RGBA zero outside the border.
    """
    if not isinstance(pixels, np.ndarray) or pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ValueError('Expected an H x W x 4 pixel array')
    if not np.issubdtype(pixels.dtype, np.floating):
        raise ValueError('Expected floating-point premultiplied linear pixels')
    height = int(frame['height'])
    width = int(frame['width'])
    if pixels.shape[:2] != (height, width):
        raise ValueError('Pixels must match the complete frame extent %dx%d'
                         % (width, height))
    info = frame['metadata']
    if info['use_border']:
        x0, y0, x1, y1 = (int(value) for value in info['border_rect_px'])
        if info['use_crop_to_border']:
            return np.array(pixels[y0:y1, x0:x1], dtype=np.float32,
                            copy=True, order='C')
        result = np.zeros((height, width, 4), dtype=np.float32)
        result[y0:y1, x0:x1] = pixels[y0:y1, x0:x1]
        return result
    return np.array(pixels, dtype=np.float32, copy=True, order='C')
