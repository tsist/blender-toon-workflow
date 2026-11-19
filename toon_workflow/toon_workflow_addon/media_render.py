# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent native EEVEE media Beauty plus an explicit artistic line layer.

This is not the correlated opaque joint profile. Native Beauty is resolved
once, Line is resolved independently, then the optical artistic line is overed.
The cold inspector verifies integrity/composition, not optical reconstruction.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import tempfile
import time
import zipfile
from . import line_optics
from .backend import pencil

PROFILE = 'native_media_artline.v1'
SCHEMA = 'toon-native-media-artline-report.v1'
SETTINGS_SCHEMA = 'toon-native-media-settings.v1'
_ARRAYS = {'Beauty', 'Line', 'OpticalLine', 'Combined'}
_ARTIFACTS = _ARRAYS | {'preview', 'arrays'}
_REPORT_KEYS = {'schema','profile','status','frame','source','author_checkpoint','author_sha256',
                'admission','lighting','artifacts','settings','line_optics','native_camera',
                'author_setup','limits','blender','elapsed_seconds','shot_look'}
_FRAME_KEYS = {'camera_name','camera_type','frame','frame_subframe','full_extent','output_extent',
               'resolution_percentage','pixel_aspect','border_rect_px','use_border','use_crop_to_border',
               'normalized_camera_world','view','projection','mvp','evaluation','depsgraph_mode',
               'render_equivalent','render_subdivision_claimed'}
_LIMITS = ['artistic screen overlay; no optical transmission, refraction or volume transport of Line',
           'native Beauty AA and independently resolved Line are not the old correlated four-layer decomposition',
           'Line is evaluated at the requested frame; authored uniform depth/velocity do not reconstruct native temporal motion',
           'native eye-layer Beauty is not the old analytical eye-layer resolve and is not claimed exact to it',
           'optics precede border extraction; artistic optical compositor uses square pixels',
           'image budget estimates array memory only; not a hard GPU/geometry quota',
           'cold inspect checks integrity and the over formula; it does not replay or certify optical transport']


def _closed(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('Invalid closed ' + label)
    return value


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _sha_string(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Expected a SHA-256 string')
    return value


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('Duplicate JSON field: ' + key)
        value[key] = item
    return value


def normalize_settings(value):
    _closed(value, {'schema','enabled','line_mode','line_optics','media_objects'}, 'native-media settings')
    if (value['schema'] != SETTINGS_SCHEMA or value['enabled'] is not True
            or type(value['line_mode']) is not str or value['line_mode'] not in ('OVERLAY','EXCLUDE')):
        raise ValueError('Invalid native-media settings mode')
    names = value['media_objects']
    if (type(names) is not list or len(names) > 256
            or any(type(name) is not str or not 1 <= len(name) <= 256 for name in names)
            or names != sorted(set(names))):
        raise ValueError('Expected unique sorted explicit media Object names')
    _closed(value['line_optics'], line_optics.DEFAULTS, 'art-line optics settings')
    return dict(schema=SETTINGS_SCHEMA, enabled=True, line_mode=value['line_mode'],
                line_optics=line_optics.normalize(value['line_optics']), media_objects=list(names))


def _admit(width, height, axis, budget, lighting_samples):
    from . import joint_sampling
    offsets = joint_sampling.offsets(axis)
    joint_sampling.admit_lighting(lighting_samples)
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 2048 or not 1 <= height <= 2048):
        raise ValueError('Native media profile requires at most 2048 pixels per side')
    estimate = width * height * 256
    if type(budget) is not int or budget < estimate:
        raise ValueError('Native media image budget is below the array estimate')
    return dict(samples_per_axis=axis, sample_count=len(offsets), budget_bytes=budget,
                beauty_samples_per_position=lighting_samples)


def _over(beauty, line):
    import numpy as np
    return (line.astype(np.float64) + beauty.astype(np.float64) *
            (1. - line[:, :, 3:4].astype(np.float64))).astype(np.float32)


def _frame(value):
    _closed(value, _FRAME_KEYS, 'actual RENDER frame')
    if value['evaluation'] != 'RENDER' or value['depsgraph_mode'] != 'RENDER':
        raise ValueError('Native media report requires actual RENDER metadata')
    for field in ('full_extent','output_extent'):
        pair = value[field]
        if type(pair) is not list or len(pair) != 2 or any(type(v) is not int or not 1 <= v <= 2048 for v in pair):
            raise ValueError('Invalid native media image extent')
    w, h = value['full_extent']
    rect = value['border_rect_px']
    if (type(rect) is not list or len(rect) != 4 or any(type(v) is not int for v in rect)
            or not 0 <= rect[0] < rect[2] <= w or not 0 <= rect[1] < rect[3] <= h):
        raise ValueError('Invalid native media border rectangle')
    for key in ('use_border','use_crop_to_border','render_equivalent','render_subdivision_claimed'):
        if type(value[key]) is not bool: raise ValueError('Invalid frame boolean')
    if value['render_equivalent'] is not False or value['render_subdivision_claimed'] is not True:
        raise ValueError('Unexpected RENDER frame qualification')
    expected = [rect[2]-rect[0], rect[3]-rect[1]] if value['use_border'] and value['use_crop_to_border'] else [w,h]
    if value['output_extent'] != expected: raise ValueError('Frame extraction extent differs')
    if not value['use_border'] and rect != [0,0,w,h]: raise ValueError('Unexpected disabled border rectangle')
    if type(value['camera_name']) is not str or not value['camera_name'] or value['camera_type'] not in ('PERSP','ORTHO'):
        raise ValueError('Invalid frame camera')
    if type(value['frame']) is not int or type(value['resolution_percentage']) is not int:
        raise ValueError('Invalid frame number/percentage')
    if type(value['frame_subframe']) not in (int,float) or not math.isfinite(value['frame_subframe']) or not 0 <= value['frame_subframe'] <= 1:
        raise ValueError('Invalid frame subframe')
    aspect = value['pixel_aspect']
    if type(aspect) is not list or len(aspect) != 2 or any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in aspect):
        raise ValueError('Invalid frame pixel aspect')
    for key in ('normalized_camera_world','view','projection','mvp'):
        matrix = value[key]
        if (type(matrix) is not list or len(matrix) != 4
                or any(type(row) is not list or len(row) != 4 for row in matrix)
                or any(type(v) not in (int,float) or not math.isfinite(v) for row in matrix for v in row)):
            raise ValueError('Invalid frame matrix: ' + key)
    return value



def _look_settings(value):
    from . import manga_look
    _closed(value,set(manga_look.DEFAULT_SETTINGS)|{'line_ink_strength'},'uniform media Look')
    value=dict(value);strength=value.pop('line_ink_strength')
    if type(strength) not in (int,float) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('Invalid original-line ink strength')
    result=manga_look.normalize_settings(value)
    if result['mode']!='MANGA': raise ValueError('Native media Look requires explicit MANGA')
    result=dict(result,line_ink_strength=float(strength))
    return json.loads(json.dumps(result,allow_nan=False))


def _build_look(combined,line,frame,settings,*,author_snapshot,flattened_empty_partition):
    from . import manga_look, shot_look_output
    normalized=dict(settings);strength=normalized.pop('line_ink_strength')
    pixels=shot_look_output._ink(manga_look.apply(combined,normalized),line,strength,normalized['ink_rgb'])
    info=frame['metadata'];crop=info['use_border'] and info['use_crop_to_border']
    offset=info['border_rect_px'][:2] if crop else [0,0]
    replay=dict(settings)
    replay['offset_px']=[settings['offset_px'][0]+offset[0],settings['offset_px'][1]+offset[1]]
    receipt=dict(schema='toon-native-media-look.v1',settings=settings,replay_settings=replay,
                 source='full-frame-native-media-composite',line_source='OpticalLine',alpha_exact=True,
                 author_snapshot=author_snapshot,flattened_empty_partition=flattened_empty_partition)
    return pencil('camera').extract_pixels(frame,pixels),receipt


def _validate_look(report,arrays,width,height):
    import numpy as np
    from . import manga_look, shot_look_output
    receipt=report['shot_look']
    if receipt is None:return None
    _closed(receipt,{'schema','settings','replay_settings','source','line_source','alpha_exact','author_snapshot','flattened_empty_partition'},'native-media Look receipt')
    if (receipt['schema']!='toon-native-media-look.v1' or receipt['source']!='full-frame-native-media-composite'
            or receipt['line_source']!='OpticalLine' or receipt['alpha_exact'] is not True):
        raise ValueError('Invalid native-media Look semantics')
    normalized=_look_settings(receipt['settings']);replay=_look_settings(receipt['replay_settings'])
    author=receipt['author_snapshot']
    if receipt['flattened_empty_partition'] is True:
        _closed(author,{'schema','foreground','background','background_objects'},'empty author Look partition')
        if (author['schema']!='toon-shot-look.regions.v2' or author['background_objects']!=[]
                or _look_settings(author['foreground'])!=normalized):
            raise ValueError('Media Look cannot flatten a nonempty author partition')
        _closed(author['background'],manga_look.DEFAULT_SETTINGS,'empty-partition background Look')
        manga_look.normalize_settings(author['background'])
    elif receipt['flattened_empty_partition'] is False:
        if _look_settings(author)!=normalized:raise ValueError('Media uniform Look author identity differs')
    else:raise ValueError('Invalid empty-partition flatten flag')
    info=report['frame'];crop=info['use_border'] and info['use_crop_to_border']
    offset=info['border_rect_px'][:2] if crop else [0,0]
    expected=dict(normalized);expected['offset_px']=[normalized['offset_px'][0]+offset[0],normalized['offset_px'][1]+offset[1]]
    if replay!=expected:raise ValueError('Media Look crop origin differs')
    path=Path(report['artifacts']['look_arrays']['path'])
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist())!=1 or archive.namelist()!=['Look.npy'] or archive.infolist()[0].file_size>width*height*16+1024:
            raise ValueError('Look array exceeds declared dimensions')
    with np.load(path,allow_pickle=False) as archive:pixels=archive['Look']
    settings=dict(replay);strength=settings.pop('line_ink_strength')
    expected=shot_look_output._ink(manga_look.apply(arrays['Combined'],settings),arrays['OpticalLine'],strength,settings['ink_rgb'])
    if (pixels.shape!=(height,width,4) or pixels.dtype!=np.float32 or not np.isfinite(pixels).all()
            or not np.array_equal(pixels,expected) or not np.array_equal(pixels[:,:,3],arrays['Combined'][:,:,3])):
        raise ValueError('Native-media Look replay differs')
    return dict(schema=receipt['schema'],nonlinear_replay_exact=True,alpha_exact=True)



def _image_format_state(scene):
    """Observe even Blender's unassignable empty color-space enum state."""
    fmt=scene.render.image_settings
    keys=('file_format','color_mode','color_depth','exr_codec','color_management')
    return dict(values={key:getattr(fmt,key) for key in keys},
                set_fields={key:fmt.is_property_set(key) for key in keys},
                linear_space=fmt.linear_colorspace_settings.name,
                linear_space_set=fmt.linear_colorspace_settings.is_property_set('name'))


def _native_beauty_data(scene,layer,path,width,height):
    """Read the same native render through a private File Output node format.

    Scene ImageSettings remain read-only, including the unassignable initial
    empty color-space enum. The proven 5.2.1 route is RLayers -> File Output
    during render(write_still=False); no Render Result export is attempted.
    """
    import bpy
    path=Path(path)
    if (not path.is_absolute() or path.suffix.lower()!='.exr' or not path.parent.is_dir()
            or path.exists() or path.is_symlink()):
        raise ValueError('Choose a new absolute native-Beauty EXR path')
    before=_image_format_state(scene)
    from .runtime_compat import capture_effect
    old_use=scene.render.use_compositing
    old_device=scene.render.compositor_device
    old_precision=scene.render.compositor_precision
    group=None
    try:
        group=bpy.data.node_groups.new('Toon private native-Beauty File Output','CompositorNodeTree')
        group.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
        render=group.nodes.new('CompositorNodeRLayers')
        render.scene=scene;render.layer=layer.name
        output=group.nodes.new('NodeGroupOutput')
        group.links.new(render.outputs['Image'],output.inputs['Image'])
        file=group.nodes.new('CompositorNodeOutputFile')
        file.directory=str(path.parent);file.file_name=''
        file.file_output_items.new('RGBA',path.stem)
        fmt=file.format
        fmt.media_type='IMAGE'
        fmt.file_format='OPEN_EXR';fmt.color_mode='RGBA';fmt.color_depth='32';fmt.exr_codec='ZIP'
        fmt.color_management='OVERRIDE';fmt.linear_colorspace_settings.name='Linear Rec.709'
        group.links.new(render.outputs['Image'],file.inputs[path.stem])
        scene.render.use_compositing=True
        scene.render.compositor_device='CPU'
        scene.render.compositor_precision='FULL'
        with capture_effect(scene, group):
            status=bpy.ops.render.render(write_still=False,scene=scene.name,layer=layer.name)
        if _image_format_state(scene)!=before:
            raise RuntimeError('Native render(write_still=False) changed source ImageSettings; exact source state cannot be restored')
        if status!={'FINISHED'}:raise RuntimeError('Native media Beauty render did not finish')
        if not path.is_file() or path.stat().st_size==0:
            raise RuntimeError('Private native-Beauty File Output did not write the exact requested EXR')
        return line_optics._read_exr(bpy,path,width,height)
    finally:
        scene.render.use_compositing=old_use
        scene.render.compositor_device=old_device
        scene.render.compositor_precision=old_precision
        if group is not None:bpy.data.node_groups.remove(group)
        if _image_format_state(scene)!=before:
            raise RuntimeError('Native-Beauty File Output changed source ImageSettings; refusing approximate restoration')



def render(scene, layer, output_directory, *, samples_per_axis=2,
           memory_budget_bytes=512*1024*1024, lighting_samples=64, progress=None):
    import bpy
    import numpy as np
    from . import joint_render as J, media_render_ui, head_normals, camera_shots
    from . import joint_sampling, shot_look_ui
    look_author_snapshot = shot_look_ui.snapshot(scene)
    look_settings = look_author_snapshot
    flattened_empty_partition = False
    if look_settings is not None and 'schema' in look_settings:
        if look_settings.get('schema')!='toon-shot-look.regions.v2' or look_settings.get('background_objects')!=[]:
            raise ValueError('Native media does not qualify nonempty foreground/background Look partitions')
        _closed(look_settings,{'schema','foreground','background','background_objects'},'empty author Look partition')
        look_settings=look_settings['foreground']
        flattened_empty_partition=True
    if look_settings is not None: look_settings = _look_settings(look_settings)
    if not bpy.app.background:
        raise ValueError('Native media rendering requires an isolated background worker')
    from .runtime_compat import require_joint_build
    require_joint_build(bpy.app)
    if (scene.render.engine != 'BLENDER_EEVEE' or scene.camera is None
            or scene.render.use_multiview or layer.id_data != scene
            or bpy.context.scene != scene or bpy.context.view_layer != layer):
        raise ValueError('Set the requested EEVEE Scene/ViewLayer and a single explicit camera')
    source_format_before = _image_format_state(scene)
    J._admit_drivers()
    from .joint_workflow import admit_legacy
    admit_legacy(scene)
    settings = media_render_ui.snapshot(scene)
    if settings is None: raise ValueError('Native media art-line profile is not explicitly enabled')
    settings = normalize_settings(settings)
    if not hasattr(scene, 'toon_viewport_preview'): raise ValueError('Register Pencil before native-media rendering')
    source = Path(bpy.data.filepath)
    root = Path(output_directory)
    if not source.is_absolute() or not source.is_file(): raise ValueError('Save an authoring source first')
    if not root.is_absolute() or not root.parent.is_dir() or root.exists() or root.is_symlink():
        raise ValueError('Choose a new absolute native-media output directory')
    source_sha = _sha(source)
    progress = progress or (lambda *args, **kwargs: None)
    started = time.perf_counter()
    author_setup = dict(head_normals=head_normals.apply(scene), camera_shot=camera_shots.apply(scene))
    layer.update()
    camera_api = pencil('camera')
    def capture():
        return J._run_render_callback(scene, layer, lambda graph, override:
            camera_api.capture_render(graph.scene_eval, graph, camera=override))
    base_frame = capture()
    if _image_format_state(scene) != source_format_before:
        raise RuntimeError('Actual RENDER frame capture changed source ImageSettings')
    width, height = base_frame['width'], base_frame['height']
    admission = _admit(width, height, samples_per_axis, memory_budget_bytes, lighting_samples)
    _frame(base_frame['metadata'])
    root.mkdir()
    if bpy.ops.wm.save_as_mainfile(filepath=str(root/'author.blend'), copy=True, check_existing=False) != {'FINISHED'}:
        raise RuntimeError('Native-media author checkpoint did not save')
    render_fields = ('filepath','use_border','use_compositing','use_sequencer')
    saved_render = {key:getattr(scene.render,key) for key in render_fields}
    saved_layers = [(item,item.use) for item in scene.view_layers]
    saved_samples = scene.eevee.taa_render_samples
    baseline = (scene.camera.data.shift_x, scene.camera.data.shift_y)
    media_objects = [scene.objects[name] for name in settings['media_objects']]
    hidden = [(obj,obj.hide_render) for obj in media_objects]
    native_camera = dict(dof_enabled=bool(scene.camera.data.dof.use_dof),
                         motion_blur_enabled=bool(scene.render.use_motion_blur),
                         focus_distance=float(scene.camera.data.dof.focus_distance),
                         fstop=float(scene.camera.data.dof.aperture_fstop))
    try:
        for item in scene.view_layers: item.use = item == layer
        scene.render.use_border = False
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        scene.eevee.taa_render_samples = lighting_samples
        if scene.eevee.taa_render_samples != lighting_samples: raise ValueError('Native EEVEE sample setting changed')
        with tempfile.TemporaryDirectory(prefix='native-media-', dir=root) as temporary:
            temporary = Path(temporary)
            progress('NATIVE_BEAUTY')
            beauty = _native_beauty_data(scene, layer, temporary/'Beauty.exr', width, height)
            line_sum = np.zeros((height,width,4), np.float64)
            if settings['line_mode'] == 'OVERLAY':
                try:
                    for obj, value in hidden: obj.hide_render = True
                    layer.update()
                    offsets = joint_sampling.offsets(samples_per_axis)
                    for index, offset in enumerate(offsets):
                        progress('ART_LINE_SAMPLE', index=index, total=len(offsets), offset=offset)
                        J._shift_projection(scene.camera, scene, layer, baseline, offset, width, height)
                        pixels, _, _ = J._line(scene, layer, progress)
                        if pixels.shape != line_sum.shape or not np.isfinite(pixels).all():
                            raise ValueError('Invalid independent artistic line sample')
                        line_sum += pixels
                finally:
                    for obj, value in hidden: obj.hide_render = value
                    scene.camera.data.shift_x, scene.camera.data.shift_y = baseline
                    scene.camera.data.update_tag(); layer.update()
            raw_line = (line_sum / admission['sample_count']).astype(np.float32)
            progress('ART_LINE_OPTICS')
            optical_line, optics_receipt = line_optics.process_line(raw_line, settings['line_optics'],
                                            temporary/'OpticalLine.exr', camera=scene.camera)
            # Read the actual unshifted RENDER frame again with the author's crop
            # flags. No viewport metadata is promoted to a render witness.
            scene.camera.data.shift_x, scene.camera.data.shift_y = baseline
            scene.camera.data.update_tag()
            scene.render.use_border = saved_render['use_border']
            for obj, value in hidden: obj.hide_render = value
            layer.update()
            final_frame = capture()
            if final_frame['metadata'] != base_frame['metadata']:
                raise ValueError('Unshifted actual RENDER frame changed during media output')
            if media_render_ui.snapshot(scene) != settings:
                raise ValueError('Native media author settings changed during rendering')
            combined = _over(beauty, optical_line)
            look_pixels = look_receipt = None
            if look_settings is not None:
                look_pixels, look_receipt = _build_look(combined, optical_line, final_frame, look_settings,
                    author_snapshot=look_author_snapshot,flattened_empty_partition=flattened_empty_partition)
            if shot_look_ui.snapshot(scene) != look_author_snapshot:
                raise ValueError('Uniform Shot Look changed during native media rendering')
            arrays = {name:camera_api.extract_pixels(final_frame, pixels) for name,pixels in
                      [('Beauty',beauty),('Line',raw_line),('OpticalLine',optical_line),('Combined',combined)]}
            save = pencil('output').save_image_new
            artifacts = {}
            for name,pixels in arrays.items():
                path = root/(name+'.exr')
                save(path, pixels, pixel_aspect=final_frame['metadata']['pixel_aspect'])
                artifacts[name] = dict(path=str(path),sha256=_sha(path))
            preview = root/'Combined.png'
            save(preview, arrays['Combined'], pixel_aspect=final_frame['metadata']['pixel_aspect'])
            artifacts['preview'] = dict(path=str(preview),sha256=_sha(preview))
            np.savez_compressed(root/'arrays.npz', **arrays)
            artifacts['arrays'] = dict(path=str(root/'arrays.npz'),sha256=_sha(root/'arrays.npz'))
            if look_receipt is not None:
                for name, filename in [('Look','Look.exr'),('look_preview','Look.png')]:
                    target=root/filename
                    save(target, look_pixels, pixel_aspect=final_frame['metadata']['pixel_aspect'])
                    artifacts[name]=dict(path=str(target),sha256=_sha(target))
                np.savez_compressed(root/'look.npz',Look=look_pixels)
                artifacts['look_arrays']=dict(path=str(root/'look.npz'),sha256=_sha(root/'look.npz'))
            optics = dict(process=optics_receipt, processing_output_retained=False,
                          published_output=str(root/'OpticalLine.exr'), crop_after_optics=True)
        if _sha(source) != source_sha: raise ValueError('Source blend changed while media worker was running')
        if _image_format_state(scene) != source_format_before:
            raise RuntimeError('Native media processing changed source ImageSettings')
        report = dict(schema=SCHEMA, profile=PROFILE, status='completed', frame=final_frame['metadata'],
                      source=dict(file=str(source),sha256=source_sha,unchanged=True),
                      author_checkpoint=str(root/'author.blend'),author_sha256=_sha(root/'author.blend'),
                      admission=admission,lighting=dict(samples_per_position=lighting_samples),artifacts=artifacts,
                      settings=settings,line_optics=optics,native_camera=native_camera,author_setup=author_setup,shot_look=look_receipt,
                      limits=list(_LIMITS),blender=dict(version=bpy.app.version_string,build_hash=bpy.app.build_hash.decode()),
                      elapsed_seconds=time.perf_counter()-started)
        (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
        progress('COMPLETE', report=str(root/'report.json'))
        return report
    finally:
        for obj,value in hidden: obj.hide_render=value
        scene.camera.data.shift_x, scene.camera.data.shift_y = baseline
        scene.camera.data.update_tag()
        for item,value in saved_layers: item.use=value
        scene.eevee.taa_render_samples=saved_samples
        for key,value in saved_render.items(): setattr(scene.render,key,value)
        layer.update()
        if _image_format_state(scene) != source_format_before:
            raise RuntimeError('Native media output changed source ImageSettings; exact author state is protected')


def _exr_extent(path, width, height):
    """Bounded OpenEXR header check, without importing bpy or decoding pixels."""
    with Path(path).open('rb') as stream:
        if stream.read(4) != struct.pack('<I',20000630): raise ValueError('Invalid EXR magic')
        version = stream.read(4)
        if len(version) != 4: raise ValueError('Truncated EXR header')
        def string():
            value = bytearray()
            while len(value) <= 255:
                char = stream.read(1)
                if char == b'': raise ValueError('Truncated EXR string')
                if char == b'\0': return bytes(value)
                value.extend(char)
            raise ValueError('EXR attribute string exceeds budget')
        attrs = {}
        while stream.tell() < 1048576:
            name = string()
            if not name: break
            kind = string()
            data = stream.read(4)
            if len(data) != 4: raise ValueError('Truncated EXR attribute')
            size = struct.unpack('<I',data)[0]
            if size > 1048576 or stream.tell()+size > 1048576: raise ValueError('EXR header exceeds budget')
            value = stream.read(size)
            if len(value) != size or name in attrs: raise ValueError('Invalid EXR attributes')
            attrs[name]=(kind,value)
        else: raise ValueError('EXR header exceeds budget')
        if attrs.get(b'dataWindow',(None,None))[0] != b'box2i': raise ValueError('Missing EXR dataWindow')
        box = attrs[b'dataWindow'][1]
        if len(box) != 16: raise ValueError('Invalid EXR dataWindow')
        x0,y0,x1,y1=struct.unpack('<4i',box)
        if [x1-x0+1,y1-y0+1] != [width,height]: raise ValueError('EXR dimensions differ from arrays')
        channels=attrs.get(b'channels',(None,None))
        if channels[0] != b'chlist': raise ValueError('Missing EXR channels')
        data=channels[1]; offset=0; names=[]
        while offset<len(data) and data[offset] != 0:
            end=data.find(b'\0',offset)
            if end<offset or end+17>len(data): raise ValueError('Invalid EXR channel list')
            name=data[offset:end]
            pixel_type=struct.unpack('<i',data[end+1:end+5])[0]
            if pixel_type != 2: raise ValueError('EXR channels must be FLOAT32')
            names.append(name);offset=end+17
        if sorted(names) != [b'A',b'B',b'G',b'R']: raise ValueError('Expected EXR RGBA channels')


def inspect(report_path):
    """Pure bounded cold inspection: hashes, arrays and over; never renders."""
    import numpy as np
    path = Path(report_path).resolve(strict=True)
    if path.stat().st_size > 8*1024*1024: raise ValueError('Media report exceeds reader budget')
    report=json.loads(path.read_text(encoding='utf8'),object_pairs_hook=_json_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON constant')))
    _closed(report,_REPORT_KEYS,'native-media report')
    if report['schema']!=SCHEMA or report['profile']!=PROFILE or report['status']!='completed':
        raise ValueError('Expected a completed native-media art-line report')
    frame=_frame(report['frame']); width,height=frame['output_extent']
    settings=normalize_settings(report['settings'])
    _closed(report['admission'],{'samples_per_axis','sample_count','budget_bytes','beauty_samples_per_position'},'media admission')
    a=report['admission']
    expected=_admit(*frame['full_extent'],a['samples_per_axis'],a['budget_bytes'],a['beauty_samples_per_position'])
    if a!=expected: raise ValueError('Native-media sampling admission differs')
    _closed(report['lighting'],{'samples_per_position'},'media lighting')
    if report['lighting']['samples_per_position']!=a['beauty_samples_per_position']: raise ValueError('Native Beauty sample count differs')
    source=_closed(report['source'],{'file','sha256','unchanged'},'source descriptor')
    if type(source['file']) is not str or not Path(source['file']).is_absolute() or source['unchanged'] is not True:
        raise ValueError('Invalid unchanged source descriptor')
    _sha_string(source['sha256'])
    root=path.parent;checked=[]
    def check(filename,sha):
        _sha_string(sha)
        if type(filename) is not str or not Path(filename).is_absolute(): raise ValueError('Expected absolute artifact path')
        member=Path(filename)
        if member.is_symlink() or not member.resolve(strict=True).is_relative_to(root): raise ValueError('Artifact outside media bundle')
        if _sha(member)!=sha: raise ValueError('Native-media artifact SHA differs: '+member.name)
        checked.append(str(member));return member
    check(report['author_checkpoint'],report['author_sha256'])
    expected_artifacts=_ARTIFACTS | ({'Look','look_preview','look_arrays'} if report['shot_look'] is not None else set())
    _closed(report['artifacts'],expected_artifacts,'media artifacts')
    paths={}
    for name,row in report['artifacts'].items():
        _closed(row,{'path','sha256'},'media artifact descriptor')
        paths[name]=check(row['path'],row['sha256'])
    if len(set(paths.values()))!=len(paths): raise ValueError('Media artifacts alias each other')
    for name in _ARRAYS: _exr_extent(paths[name],width,height)
    if report['shot_look'] is not None: _exr_extent(paths['Look'],width,height)
    with paths['preview'].open('rb') as stream:
        header=stream.read(24)
        if len(header)!=24 or header[:8]!=b'\x89PNG\r\n\x1a\n' or struct.unpack('>II',header[16:24])!=(width,height):
            raise ValueError('Preview PNG extent differs')
    with zipfile.ZipFile(paths['arrays']) as archive:
        if (len(archive.infolist())!=4 or set(archive.namelist())!={name+'.npy' for name in _ARRAYS}
                or any(item.file_size>width*height*16+1024 for item in archive.infolist())):
            raise ValueError('Media array archive exceeds declared dimensions')
    with np.load(paths['arrays'],allow_pickle=False) as archive:
        arrays={name:archive[name] for name in _ARRAYS}
    for name,value in arrays.items():
        if (value.shape!=(height,width,4) or value.dtype!=np.float32 or not np.isfinite(value).all()
                or np.any(value[:,:,3]<0) or np.any(value[:,:,3]>1)):
            raise ValueError('Invalid native media array: '+name)
    if not np.array_equal(_over(arrays['Beauty'],arrays['OpticalLine']),arrays['Combined']):
        raise ValueError('Media arrays do not satisfy the explicit premultiplied over formula')
    look_result=_validate_look(report,arrays,width,height)
    if settings['line_mode']=='EXCLUDE' and (np.any(arrays['Line']) or np.any(arrays['OpticalLine'])):
        raise ValueError('EXCLUDE mode has nonzero artistic lines')
    if settings['line_optics']['mode']=='SHARP' and not np.array_equal(arrays['Line'],arrays['OpticalLine']):
        raise ValueError('SHARP artistic line changed')
    optics=_closed(report['line_optics'],{'process','processing_output_retained','published_output','crop_after_optics'},'media optics receipt')
    if optics['processing_output_retained'] is not False or optics['crop_after_optics'] is not True or optics['published_output']!=str(paths['OpticalLine']):
        raise ValueError('Invalid optical output extraction receipt')
    leaf=_closed(optics['process'],{'schema','settings','operations','extent','pixel_aspect','output','storage','input_exact',
                  'alpha_may_change','camera_lens_mm','source_scene_rendered','source_scene_settings_modified','semantics','max_blur_scope','limitations'},'native line optics receipt')
    mode=settings['line_optics']['mode']
    operations=(['CompositorNodeVecBlur'] if mode in ('VECTOR','BOTH') else [])+(['CompositorNodeDefocus'] if mode in ('DEFOCUS','BOTH') else [])
    if (leaf['schema']!=line_optics.SCHEMA or line_optics.normalize(leaf['settings'])!=settings['line_optics']
            or leaf['operations']!=operations or leaf['extent']!=frame['full_extent'] or leaf['pixel_aspect']!=[1.,1.]
            or leaf['source_scene_rendered'] is not False or leaf['source_scene_settings_modified'] is not False
            or leaf['alpha_may_change']!=(mode!='SHARP') or type(leaf['input_exact']) is not bool):
        raise ValueError('Native line optics receipt differs')
    if mode=='SHARP' and leaf['input_exact'] is not True: raise ValueError('SHARP receipt must be exact')
    if type(leaf['output']) is not str or not Path(leaf['output']).is_absolute() or not Path(leaf['output']).is_relative_to(root):
        raise ValueError('Invalid temporary optical process path')
    _closed(report['native_camera'],{'dof_enabled','motion_blur_enabled','focus_distance','fstop'},'native camera settings')
    native=report['native_camera']
    if any(type(native[k]) is not bool for k in ('dof_enabled','motion_blur_enabled')) or any(type(native[k]) not in (int,float) or not math.isfinite(native[k]) or native[k]<0 for k in ('focus_distance','fstop')):
        raise ValueError('Invalid native camera settings')
    _closed(report['author_setup'],{'head_normals','camera_shot'},'author setup')
    _closed(report['blender'],{'version','build_hash'},'Blender identity')
    from .runtime_compat import JOINT_BUILDS
    if report['blender']['build_hash'] not in {build for _, build in JOINT_BUILDS}:
        raise ValueError('Unqualified native-media Blender build')
    if report['limits']!=_LIMITS: raise ValueError('Native-media limitations changed')
    if type(report['elapsed_seconds']) not in (int,float) or not math.isfinite(report['elapsed_seconds']) or report['elapsed_seconds']<0:
        raise ValueError('Invalid elapsed time')
    return dict(status='pass',profile=PROFILE,checked_artifacts=len(checked),output_extent=[width,height],
                reconstruction='Combined = OpticalLine + Beauty * (1 - OpticalLine.alpha)',
                over_exact=True,optical_replay_claimed=False,shot_look=look_result,
                claim='artifact integrity and explicit screen-layer composition only; optical and artistic acceptance are separate')
