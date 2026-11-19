# SPDX-License-Identifier: GPL-3.0-or-later
"""Native compositor optics for one explicit premultiplied artistic line layer.

Uniform authored depth and native four-channel pixel velocity are screen-space
art controls. They do not describe refraction, volume transport, surface motion,
or a reconstructed scene Z/vector pass. Run in an isolated Blender worker: a
render of the private empty Scene can update Blender's global Render Result.
"""
import math
from pathlib import Path

SCHEMA = 'toon-art-line-optics.v1'
DEFAULTS = dict(mode='SHARP', depth=5., focus_distance=5., fstop=2.8,
                max_blur_px=16., velocity_px=[0., 0., 0., 0.], shutter=.5, samples=32)
MODES = ('SHARP', 'DEFOCUS', 'VECTOR', 'BOTH')


def _number(value, name, low=None, high=None, positive=False):
    if type(value) not in (int, float):
        raise ValueError(name + ' must be a finite number')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(name + ' must be a finite number') from None
    if not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if positive and value <= 0 or low is not None and value < low or high is not None and value > high:
        raise ValueError(name + ' is outside its declared range')
    return float(value)


def normalize(settings=None):
    """Normalize partial settings; reject unknown fields and coercive values."""
    if settings is None:
        settings = {}
    if type(settings) is not dict or set(settings) - set(DEFAULTS):
        raise ValueError('Expected only the closed art-line optics settings fields')
    result = dict(DEFAULTS, **settings)
    if type(result['mode']) is not str or result['mode'] not in MODES:
        raise ValueError('Unknown art-line optics mode')
    for key in ('depth', 'focus_distance', 'fstop'):
        result[key] = _number(result[key], key, positive=True)
    result['max_blur_px'] = _number(result['max_blur_px'], 'max_blur_px', 0., 256.)
    result['shutter'] = _number(result['shutter'], 'shutter', 0., 2.)
    velocity = result['velocity_px']
    if type(velocity) not in (list, tuple) or len(velocity) != 4:
        raise ValueError('velocity_px must contain four native pixel-vector channels')
    result['velocity_px'] = [_number(v, 'velocity_px', -512., 512.) for v in velocity]
    if type(result['samples']) is not int or not 1 <= result['samples'] <= 256:
        raise ValueError('samples must be an integer in [1,256]')
    return result


def _pixels(value):
    import numpy as np
    if (not isinstance(value, np.ndarray) or value.dtype != np.float32
            or value.ndim != 3 or value.shape[2] != 4 or min(value.shape[:2]) < 1
            or not np.isfinite(value).all()):
        raise ValueError('Line requires a nonempty finite HxWx4 float32 premultiplied array')
    alpha = value[:, :, 3]
    if np.any(alpha < 0) or np.any(alpha > 1):
        raise ValueError('Input line Alpha must be in [0,1]')
    if np.any(value[:, :, :3][alpha == 0] != 0):
        raise ValueError('Zero-alpha line pixels must have zero premultiplied RGB')
    return np.ascontiguousarray(value)


def _set_scalar(owner, name, value):
    """Fail visibly if this build clamps a requested setting."""
    setattr(owner, name, value)
    actual = getattr(owner, name)
    if not math.isclose(float(actual), float(value), rel_tol=2e-6, abs_tol=1e-9):
        raise ValueError('Blender did not retain ' + name + ': ' + repr((value, actual)))


def _socket(node, name, value):
    socket = node.inputs.get(name)
    if socket is None:
        raise RuntimeError('Captured Blender compositor socket is missing: ' + node.bl_idname + '.' + name)
    socket.default_value = value
    actual = socket.default_value
    if not math.isclose(float(actual), float(value), rel_tol=2e-6, abs_tol=1e-9):
        raise ValueError('Blender did not retain socket ' + name)


def _read_exr(bpy, path, width, height):
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.alpha_mode = 'PREMUL'
        if tuple(image.size) != (width, height) or not image.is_float:
            raise ValueError('Optical EXR has an unexpected extent or storage type')
        if image.colorspace_settings.name != 'Linear Rec.709':
            raise ValueError('Optical EXR must use Linear Rec.709')
        result = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(result)
        result = result.reshape(height, width, 4)
        if not np.isfinite(result).all():
            raise ValueError('Optical EXR contains non-finite pixels')
        return result
    finally:
        bpy.data.images.remove(image)


def process_line(line_rgba, settings, output_exr, *, camera=None):
    """Return (actual persisted RGBA32 pixels, receipt), never rendering source.

    The output must be a new absolute .exr path in an existing directory.
    BOTH means VectorBlur followed by Defocus. max_blur_px is exclusively the
    native Defocus.blur_max setting; VectorBlur uses velocity_px and shutter.
    SHARP performs an exact EXR copy and does not invoke the render operator.
    """
    import bpy
    import numpy as np
    settings = normalize(settings)
    pixels = _pixels(line_rgba)
    path = Path(output_exr)
    if (not path.is_absolute() or path.suffix.lower() != '.exr'
            or not path.parent.is_dir() or path.exists() or path.is_symlink()):
        raise ValueError('Choose a new absolute .exr path in an existing directory')
    from .runtime_compat import require_joint_build
    require_joint_build(bpy.app)
    if camera is not None and (getattr(camera, 'type', None) != 'CAMERA' or camera.data is None):
        raise ValueError('camera must be a Camera Object or None')
    if camera is not None and settings['mode'] in ('DEFOCUS', 'BOTH') and camera.data.type != 'PERSP':
        raise ValueError('Art-line Defocus currently requires a perspective camera')
    height, width = pixels.shape[:2]
    original_scene, original_layer = bpy.context.scene, bpy.context.view_layer
    original_window = bpy.context.window
    old_render_result = bpy.data.images.get('Render Result')
    scene = group = camera_data = camera_object = None
    images = []
    owned_render_result = None
    operations = []
    try:
        scene = bpy.data.scenes.new('Toon private art-line compositor')
        scene.render.engine = 'BLENDER_EEVEE'
        scene.render.resolution_x, scene.render.resolution_y = width, height
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.
        scene.render.film_transparent = True
        scene.render.use_motion_blur = False
        scene.render.use_sequencer = False
        scene.render.use_compositing = settings['mode'] != 'SHARP'
        scene.render.compositor_device = 'CPU'
        scene.render.compositor_precision = 'FULL'
        scene.eevee.taa_render_samples = 1
        scene.render.filepath = str(path)
        scene.render.use_file_extension = True
        fmt = scene.render.image_settings
        fmt.file_format = 'OPEN_EXR'
        fmt.color_mode = 'RGBA'
        fmt.color_depth = '32'
        fmt.exr_codec = 'ZIP'
        fmt.color_management = 'OVERRIDE'
        fmt.linear_colorspace_settings.name = 'Linear Rec.709'
        image = bpy.data.images.new('Toon private premultiplied line', width, height,
                                    alpha=True, float_buffer=True)
        images.append(image)
        image.colorspace_settings.name = 'Linear Rec.709'
        image.alpha_mode = 'PREMUL'
        image.pixels.foreach_set(pixels.ravel())
        image.update()
        if settings['mode'] != 'SHARP':
            camera_data = camera.data.copy() if camera is not None else bpy.data.cameras.new('Toon private art-line camera')
            camera_data.animation_data_clear()
            camera_data.dof.focus_object = None
            _set_scalar(camera_data.dof, 'focus_distance', settings['focus_distance'])
            camera_data.dof.use_dof = False
            camera_object = bpy.data.objects.new('Toon private art-line camera', camera_data)
            scene.collection.objects.link(camera_object)
            if camera is not None:
                camera_object.matrix_world = camera.matrix_world.copy()
            scene.camera = camera_object
            group = bpy.data.node_groups.new('Toon private art-line optics', 'CompositorNodeTree')
            effects = getattr(scene, 'compositor_effects', None)
            if effects is None:
                scene.compositing_node_group = group
            else:
                effects.new(group.name).node_group = group
            group.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
            output = group.nodes.new('NodeGroupOutput')
            source = group.nodes.new('CompositorNodeImage')
            source.image = image
            current = source.outputs['Image']
            if settings['mode'] in ('VECTOR', 'BOTH'):
                speed_image = bpy.data.images.new('Toon private native pixel velocity', width, height,
                                                  alpha=True, float_buffer=True, is_data=True)
                images.append(speed_image)
                # Preserve all four channels, including negative and fourth-channel
                # values; this is data, not a premultiplied colour image.
                speed_image.alpha_mode = 'CHANNEL_PACKED'
                speed_pixels = np.empty_like(pixels)
                speed_pixels[:] = settings['velocity_px']
                speed_image.pixels.foreach_set(speed_pixels.ravel())
                speed_image.update()
                speed = group.nodes.new('CompositorNodeImage')
                speed.image = speed_image
                node = group.nodes.new('CompositorNodeVecBlur')
                _socket(node, 'Depth', settings['depth'])
                _socket(node, 'Samples', settings['samples'])
                _socket(node, 'Shutter', settings['shutter'])
                group.links.new(current, node.inputs['Image'])
                group.links.new(speed.outputs['Image'], node.inputs['Speed'])
                current = node.outputs['Image']
                operations.append('CompositorNodeVecBlur')
            if settings['mode'] in ('DEFOCUS', 'BOTH'):
                node = group.nodes.new('CompositorNodeDefocus')
                node.scene = scene
                node.use_zbuffer = True
                _set_scalar(node, 'f_stop', settings['fstop'])
                _set_scalar(node, 'blur_max', settings['max_blur_px'])
                _socket(node, 'Z', settings['depth'])
                group.links.new(current, node.inputs['Image'])
                current = node.outputs['Image']
                operations.append('CompositorNodeDefocus')
            group.links.new(current, output.inputs['Image'])
        # Reserve only after validation/construction. A failed save leaves this
        # call's partial output for diagnosis; retries require a new path.
        with path.open('xb'):
            pass
        if settings['mode'] == 'SHARP':
            image.save_render(str(path), scene=scene)
        else:
            try:
                with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
                    finished = bpy.ops.render.render(write_still=True, scene=scene.name,
                                                     layer=scene.view_layers[0].name)
                if finished != {'FINISHED'}:
                    raise RuntimeError('Private art-line compositor render did not finish')
            finally:
                new_render_result = bpy.data.images.get('Render Result')
                if old_render_result is None and new_render_result is not None:
                    owned_render_result = new_render_result
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError('Private compositor did not write the requested EXR')
        actual = _read_exr(bpy, path, width, height)
        exact = bool(np.array_equal(actual, pixels))
        if settings['mode'] == 'SHARP' and not exact:
            raise ValueError('SHARP EXR round-trip changed line pixels')
        receipt = dict(schema=SCHEMA, settings=settings, operations=operations,
                       extent=[width, height], pixel_aspect=[1., 1.], output=str(path), storage='RGBA32 Linear Rec.709 premultiplied',
                       input_exact=exact, alpha_may_change=settings['mode'] != 'SHARP',
                       camera_lens_mm=float(camera_data.lens) if camera_data is not None else None,
                       source_scene_rendered=False, source_scene_settings_modified=False,
                       semantics='one artistic line layer; uniform authored depth and native four-channel pixel velocity',
                       max_blur_scope='native Defocus.blur_max only; VectorBlur uses velocity_px and shutter',
                       limitations=['no refracted or volume-transported lines',
                                    'not scene Z or object/camera motion reconstruction',
                                    'private Scene render may update an existing global Render Result'])
        return actual, receipt
    finally:
        # The context override unwinds before IDs disappear. Restore a window
        # only if Blender changed it; never delete or edit a pre-existing ID.
        if original_window is not None and (original_window.scene != original_scene
                                            or original_window.view_layer != original_layer):
            original_window.scene = original_scene
            original_window.view_layer = original_layer
        if scene is not None:
            bpy.data.scenes.remove(scene)
        if camera_object is not None:
            bpy.data.objects.remove(camera_object, do_unlink=True)
        if camera_data is not None:
            bpy.data.cameras.remove(camera_data)
        if group is not None:
            bpy.data.node_groups.remove(group)
        for owned_image in images:
            bpy.data.images.remove(owned_image)
        if owned_render_result is not None:
            bpy.data.images.remove(owned_render_result)
        if bpy.context.scene != original_scene or bpy.context.view_layer != original_layer:
            raise RuntimeError('Art-line compositor did not restore the original context')
