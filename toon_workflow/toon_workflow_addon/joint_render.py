# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental EEVEE/Pencil joint-grid producer for isolated Blender workers.

This is a new explicit rendering profile, not an upgrade of old toon states
or an assertion of native EEVEE AA equivalence. Author data are saved before
any frame-local render freezing. Source meshes are not line carriers.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from . import joint_sampling
from .backend import pencil


def _progress_callback(root, progress, *, clock=time.perf_counter):
    """Keep cancellation live; journal progress without a replace/read race.

    The snapshot remains compatible with existing readers. Windows readers
    without DELETE sharing may temporarily block its replacement; the complete
    append-only journal remains available and the next checkpoint retries it.
    """
    last_data, last_write = None, float('-inf')

    def checkpoint(stage, **detail):
        nonlocal last_data, last_write
        # Never throttle the supervisor's cancellation/health callback.
        progress(stage, **detail)
        data = dict(stage=stage, **detail)
        now = clock()
        if data == last_data and now - last_write < 1.0:
            return
        text = json.dumps(data, ensure_ascii=False)
        with (root/'progress.jsonl').open('a', encoding='utf8') as stream:
            stream.write(text+'\n')
        temporary = root/'progress.tmp'
        try:
            temporary.write_text(text, encoding='utf8')
            temporary.replace(root/'progress.json')
        except PermissionError as error:
            if os.name != 'nt' or error.winerror not in (5, 32, 33):
                raise
            # This is optional UI telemetry, not a content/acceptance receipt.
            with (root/'progress-publication-warnings.jsonl').open('a', encoding='utf8') as stream:
                stream.write(json.dumps(dict(stage=stage, winerror=error.winerror,
                    snapshot='deferred', journal='progress.jsonl'))+'\n')
        last_data, last_write = data, now

    return checkpoint


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _geometry_identity(geometry):
    digest = hashlib.sha256()
    for mesh in geometry.meshes:
        digest.update(mesh.name.encode('utf8'))
        for name in ('vertices', 'triangles', 'edges', 'triangle_polygons', 'face_material'):
            value = getattr(mesh, name)
            digest.update(str(value.shape).encode('ascii'))
            digest.update(value.tobytes())
    return digest.hexdigest()


def _sdf_carrier_snapshot(mesh, face):
    """Independent full-array evidence for the explicit side carrier."""
    import numpy as np
    result = {}
    for role, expected in (('left', 0.), ('right', 1.)):
        name = face[role+'_attribute']; attribute = mesh.attributes.get(name)
        if (attribute is None or attribute.domain != 'POINT' or attribute.data_type != 'FLOAT'
                or len(attribute.data) != len(mesh.vertices)):
            raise ValueError('Missing SDF side carrier: '+name)
        values = np.empty(len(attribute.data), dtype='<f4'); attribute.data.foreach_get('value', values)
        if not np.isfinite(values).all() or not np.all(values == expected):
            raise ValueError('SDF side carrier is not constant: '+name)
        result[role] = dict(attribute=name, domain='POINT', data_type='FLOAT', count=len(values),
                            min=float(values.min()), max=float(values.max()),
                            sha256=hashlib.sha256(values.tobytes()).hexdigest())
    return result


def _sdf_binding_snapshot(reader):
    from . import face_sdf
    value = face_sdf._binding_report(face_sdf._binding_info(reader))
    return value


def _sdf_material_snapshot(target, reader):
    import bpy
    from . import face_sdf
    info = face_sdf._binding_info(reader)
    materials = [m for m in bpy.data.materials if m.node_tree == info.get('tree')]
    if info.get('state') != 'bound' or len(materials) != 1:
        raise ValueError('SDF reader has no unique bound Material')
    material = materials[0]
    slots = [i for i, slot in enumerate(target.material_slots) if slot.material == material]
    if not slots or not any(p.material_index in slots for p in target.data.polygons):
        raise ValueError('SDF material is not assigned to its explicit target')
    return dict(target=target.name, material=material.name, slots=slots)


def _sdf_polar_image_sources(reader):
    """Resolve the explicit 18 horizontal/9 independent vertical texture roles."""
    from . import face_sdf
    horizontal={'left_texture','right_texture'}|{
        side+'_texture_'+str(i) for side in ('left','right') for i in range(9) if i!=4}
    vertical={'vertical_texture_'+str(i) for i in range(9)}
    textures=[node for node in reader.nodes if node.bl_idname=='ShaderNodeTexImage']
    roles={node.get(face_sdf._NODE):node for node in textures}
    if len(textures)!=27 or len(roles)!=27 or set(roles)!=horizontal|vertical:
        raise ValueError('Polar face-SDF requires its explicit 18+9 texture inventory')
    h_images={roles[key].image for key in horizontal};v_images={roles[key].image for key in vertical}
    if len(h_images)!=1 or len(v_images)!=1 or None in h_images|v_images or h_images==v_images:
        raise ValueError('Polar face-SDF horizontal and vertical image groups must be distinct and individually unambiguous')
    return next(iter(h_images)),next(iter(v_images))


def _sdf_direction_image_sources(reader, *, continuity=False):
    """Resolve the exact three/four-source inventory by actual texture roles."""
    from . import face_sdf
    horizontal={'left_texture','right_texture'}|{
        side+'_texture_'+str(i) for side in ('left','right') for i in range(9) if i!=4}
    vertical={'vertical_texture_'+str(i) for i in range(9)}
    direction={'direction_texture_'+str(i) for i in range(9)}
    pair={'continuity_texture_'+str(i) for i in range(9)} if continuity else set()
    textures=[node for node in reader.nodes if node.bl_idname=='ShaderNodeTexImage']
    roles={node.get(face_sdf._NODE):node for node in textures}
    count=45 if continuity else 36
    if len(textures)!=count or len(roles)!=count or set(roles)!=horizontal|vertical|direction|pair:
        raise ValueError('Direction-key face-SDF texture inventory differs from its enabled sources')
    groups=(horizontal,vertical,direction,pair) if continuity else (horizontal,vertical,direction)
    images=[{roles[key].image for key in keys} for keys in groups]
    if any(len(values)!=1 or None in values for values in images):
        raise ValueError('Direction-key face-SDF image groups must each be unambiguous')
    values=tuple(next(iter(items)) for items in images)
    if len(set(values))!=len(groups):
        raise ValueError('Direction-key face-SDF requires distinct actual source Image IDs')
    return values


def _sdf_source_rows(reader, row):
    from . import face_sdf
    if row['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
        active_pair=row['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE) and row['pair_source'] is not None
        images=_sdf_direction_image_sources(reader,continuity=active_pair)
        rows=((images[0],'R',row['image']),(images[1],'G',row['vertical_source']['image']),
              (images[2],'R',row['direction_source']['image']))
        return rows+((images[3],'R',row['pair_source']['image']),) if active_pair else rows
    h,v=_sdf_polar_image_sources(reader)
    return ((h,'R',row['image']),(v,'G',row['vertical_source']['image']))


def _sdf_source_schemas(profile):
    """A mixed container is versioned by its highest target profile."""
    from . import sdf_witness, face_sdf
    if profile==face_sdf.AUTHORED_PROFILE:return (sdf_witness.AUTHORED_SCHEMA,)
    if profile==face_sdf.CONTINUITY_PROFILE:return (sdf_witness.CONTINUITY_SCHEMA,sdf_witness.AUTHORED_SCHEMA)
    if profile==face_sdf.DIRECTION_PROFILE:return (sdf_witness.DIRECTION_SCHEMA,sdf_witness.CONTINUITY_SCHEMA,sdf_witness.AUTHORED_SCHEMA)
    return (sdf_witness.POLAR_SCHEMA,sdf_witness.DIRECTION_SCHEMA,sdf_witness.CONTINUITY_SCHEMA,sdf_witness.AUTHORED_SCHEMA)


def _verify_sdf_materials(feature):
    from . import sdf_witness, face_sdf
    if feature is None or feature['schema'] not in (sdf_witness.MATERIAL_SCHEMA, sdf_witness.POLAR_SCHEMA, sdf_witness.DIRECTION_SCHEMA, sdf_witness.CONTINUITY_SCHEMA, sdf_witness.AUTHORED_SCHEMA):
        return
    import bpy
    for row in feature['targets']:
        reader = bpy.data.node_groups.get(row['descriptor_frozen']['shader']['name'])
        target = bpy.data.objects.get(row['target'])
        if (reader is None or target is None
                or face_sdf.reader_descriptor(reader) != row['descriptor_frozen']
                or _sdf_binding_snapshot(reader) != row['binding_frozen']
                or _sdf_material_snapshot(target, reader) != row['material_binding_frozen']):
            raise ValueError('Face-SDF material identity changed before line drawing')
        if row['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            if feature['schema'] not in _sdf_source_schemas(row['profile']):
                raise ValueError('Face-SDF material source requires its versioned freeze schema')
            for image,channel,stored in _sdf_source_rows(reader,row):
                current=face_sdf._image_content_descriptor(image,channel)
                if current['name']!=stored['name'] or not face_sdf._same_image_identity(current,stored):
                    raise ValueError('Polar face-SDF material source image changed before line drawing')
            continue
        images = {n.image for n in reader.nodes if n.bl_idname == 'ShaderNodeTexImage'}
        if len(images) != 1:
            raise ValueError('Face-SDF material image identity is ambiguous')
        current = face_sdf._image_content_descriptor(next(iter(images)), row['channel'])
        if not face_sdf._same_image_identity(current, row['image']):
            raise ValueError('Face-SDF material image changed before line drawing')


def _verify_sdf_v2_render(feature, depsgraph):
    """Re-check v2 or explicit independent-source v3 graph/images/UV before Line."""
    from . import sdf_witness, face_sdf
    if feature is None:
        return
    rows = [row for row in feature.get('targets', [])
            if row.get('profile') in (face_sdf.FILTER_PROFILE, face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE)]
    if not rows:
        return
    import bpy
    for row in rows:
        reader = bpy.data.node_groups.get(row['descriptor_frozen']['shader']['name'])
        target = bpy.data.objects.get(row['target'])
        if reader is None or target is None:
            raise ValueError('Face-SDF v2 reader or target disappeared before line drawing')
        current = face_sdf.reader_descriptor(reader)
        if current != row['descriptor_frozen']:
            raise ValueError('Face-SDF v2 shader graph/sampling changed before line drawing')
        if current.get('profile') != row['profile']:
            raise ValueError('Face-SDF v2 reader profile changed before line drawing')
        if (face_sdf._binding_report(face_sdf._binding_info(reader))
                != row['binding_frozen']):
            raise ValueError('Face-SDF v2 material binding changed before line drawing')
        if row['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            if feature.get('schema') not in _sdf_source_schemas(row['profile']):
                raise ValueError('Face-SDF RENDER source requires its versioned freeze schema')
            for image,channel,stored in _sdf_source_rows(reader,row):
                current_image=face_sdf._image_descriptor(image,channel,allowed_consumers={reader})
                if current_image['name']!=stored['name'] or not face_sdf._same_image_identity(current_image,stored):
                    raise ValueError('Polar face-SDF source image changed before line drawing')
        else:
            images = {node.image for node in reader.nodes
                      if node.bl_idname == 'ShaderNodeTexImage'}
            if len(images) != 1 or None in images:
                raise ValueError('Face-SDF v2 material image identity is ambiguous')
            current_image = face_sdf._image_descriptor(
                next(iter(images)), row['channel'], allowed_consumers={reader})
            if not face_sdf._same_image_identity(current_image, row['image']):
                raise ValueError('Face-SDF v2 image changed before line drawing')
        evaluated = target.evaluated_get(depsgraph)
        if sdf_witness.snapshot(evaluated.data, row['uv_name']) != row['frozen']:
            raise ValueError('Face-SDF v2 RENDER UV changed before line drawing')
        if row['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            vertical_name=row['vertical_source']['uv']['uv_name']
            current_vertical=sdf_witness.snapshot(evaluated.data,vertical_name)
            sdf_witness._vertical_uv(current_vertical,vertical_name,row['frozen'])
            if current_vertical!=row['vertical_frozen']:
                raise ValueError('Polar face-SDF independent RENDER UV changed before line drawing')
        if row['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            name=row['direction_source']['uv']['uv_name']
            current_direction=sdf_witness.snapshot(evaluated.data,name)
            sdf_witness._vertical_uv(current_direction,name,row['frozen'])
            if current_direction!=row['direction_frozen']:
                raise ValueError('Direction-key face-SDF RENDER UV changed before line drawing')
        if row['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE) and row['pair_source'] is not None:
            name=row['pair_source']['uv']['uv_name']
            current_pair=sdf_witness.snapshot(evaluated.data,name)
            sdf_witness._vertical_uv(current_pair,name,row['frozen'])
            if current_pair!=row['pair_frozen']:
                raise ValueError('Continuous face-SDF pair RENDER UV changed before line drawing')


def _cast_shader_snapshot(group):
    from . import sdf_cast
    return [dict(name=reader.name, sha256=sdf_cast.graph_signature(reader))
            for reader in sdf_cast._readers(group)]


def _cast_binding_snapshot(target, bindings):
    import bpy
    from . import sdf_cast
    rows = []
    for binding in bindings:
        material = bpy.data.materials.get(binding['material'])
        if material is None or material not in [slot.material for slot in target.material_slots]:
            raise ValueError('SDF cast material identity disappeared')
        state = sdf_cast._binding_state(material.node_tree, binding['token'])
        if state is None:raise ValueError('SDF cast material binding disappeared')
        rows.append(dict(material=material.name, token=binding['token'],
                         sha256=hashlib.sha256(state.encode('utf8')).hexdigest()))
    return rows


def _verify_cast_art_render(feature, depsgraph):
    import bpy
    from . import face_cast_art
    for row in feature['targets']:
        material = bpy.data.materials.get(row['material'])
        if material is None:
            raise ValueError('Face cast-art material disappeared before line drawing')
        actual = face_cast_art.snapshot_frozen(material, depsgraph, row['descriptor_before'],
            expected_render_time=feature.get('render_time'))
        if actual != row['descriptor_frozen']:
            raise ValueError('Face cast-art image, binding or settings changed before line drawing')


def _native_beauty(scene, layer, path, width, height, *, capture=None):
    import bpy
    import numpy as np
    if capture is not None:
        from . import render_capture
        result=render_capture.capture(scene,layer,path,(width,height),background_objects=capture['objects'])
        capture['mask']=result['Background'][:,:,0]
        capture['metadata']=result['metadata']
        if not np.all((capture['mask']==0)|(capture['mask']==1)):
            raise ValueError('Regional MANGA needs binary fixed-position opaque coverage')
        return result['Beauty']
    scene.render.filepath = str(path)
    if bpy.ops.render.render(write_still=True, scene=scene.name, layer=layer.name) != {'FINISHED'}:
        raise RuntimeError('EEVEE beauty did not finish')
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.alpha_mode = 'PREMUL'
        if tuple(image.size) != (width, height) or image.colorspace_settings.name != 'Linear Rec.709':
            raise ValueError('Joint beauty requires the declared extent and linear Rec.709')
        value = np.empty(width*height*4, np.float32)
        image.pixels.foreach_get(value)
        return value.reshape(height, width, 4)
    finally:
        bpy.data.images.remove(image)


def _beauty(scene, layer, path, width, height, *, capture=None):
    """Keep the old opaque path; eye layers use physical order over receivers."""
    import bpy
    from . import eye_overlays
    rows=eye_overlays.collect(scene)
    if not rows:return _native_beauty(scene,layer,path,width,height,capture=capture)
    native_path=Path(path).with_name(Path(path).stem+'-opaque-receivers.exr')
    white=bpy.data.materials.new('Toon Eye Receiver Probe')
    white.use_nodes=True;white.surface_render_method='BLENDED'
    white.use_transparency_overlap=False
    tree=white.node_tree;tree.nodes.clear()
    output=tree.nodes.new('ShaderNodeOutputMaterial');transparent=tree.nodes.new('ShaderNodeBsdfTransparent')
    transparent.inputs['Color'].default_value=(1,1,1,1)
    tree.links.new(transparent.outputs['BSDF'],output.inputs['Surface'])
    restore=[]
    try:
        for row in rows:
            slot=scene.objects[row['target']].material_slots[row['slot']]
            restore.append((slot,slot.material));slot.material=white
        base=_native_beauty(scene,layer,native_path,width,height,capture=capture)
    finally:
        for slot,material in restore:slot.material=material
        bpy.data.materials.remove(white)
        layer.update()
    camera,render_geometry=pencil('camera'),pencil('render_geometry')
    def resolve(graph,override):
        from . import eye_overlay_beauty
        geometry=render_geometry.capture(graph)
        frame=camera.capture_render(graph.scene_eval,graph,camera=override)
        return eye_overlay_beauty.resolve(base,geometry,frame,
            [{k:row[k] for k in ('target','material','slot','kind','settings')} for row in rows])[0]
    pixels=_run_render_callback(scene,layer,resolve)
    if capture is not None:
        import numpy as np
        if np.any(np.any(pixels!=base,axis=2)&(capture['mask']!=0)):
            raise ValueError('Eye-overlay contribution crosses the explicit background partition; conditional eye passes are required')
    # Working samples may reuse their name; reference artifacts are exclusive.
    if Path(path).exists():Path(path).unlink()
    pencil('output').save_image_new(Path(path),pixels)
    return pixels


def _line(scene, layer, progress, expected_geometry=None, *, include_bundle=False, finish_receipt=None, cast_art_receipt=None, sdf_receipt=None, hm_receipt=None, boundary_receipt=None):
    camera, render_geometry = pencil('camera'), pencil('render_geometry')
    def capture(depsgraph, override):
        if finish_receipt is not None:
            from . import finish_witness
            finish_witness.verify_render(finish_receipt,depsgraph)
        if hm_receipt is not None:
            from . import hair_map_witness
            hair_map_witness.verify_render(hm_receipt,depsgraph)
        if boundary_receipt is not None:
            from . import band_boundary_witness
            band_boundary_witness.verify_render(boundary_receipt,depsgraph)
        if cast_art_receipt is not None:
            _verify_cast_art_render(cast_art_receipt, depsgraph)
        _verify_sdf_v2_render(sdf_receipt, depsgraph)
        _verify_sdf_materials(sdf_receipt)
        geometry = render_geometry.capture(depsgraph)
        identity = _geometry_identity(geometry)
        if expected_geometry is not None and identity != expected_geometry:
            raise ValueError('Frozen RENDER geometry changed before line drawing')
        frame = camera.capture_render(depsgraph.scene_eval, depsgraph, camera=override)
        from .joint_line_adapter import draw_bundle
        from . import eye_overlays
        eye_layers = eye_overlays.collect(scene)
        bundle = draw_bundle(geometry, frame, scene, depsgraph.scene_eval, progress)
        if boundary_receipt is not None:
            band_boundary_witness.verify_render(boundary_receipt,depsgraph)
        result = (bundle['pixels'], frame['metadata'], identity)
        return (*result, bundle) if include_bundle else result
    return _run_render_callback(scene, layer, capture)


def _run_render_callback(scene, layer, callback):
    import bpy
    import numpy as np
    result = {}

    class JointCapture(bpy.types.RenderEngine):
        bl_idname = 'TOON_JOINT_'+uuid.uuid4().hex[:16]
        bl_label = 'Toon Joint Frame Capture'
        bl_use_gpu_context = True
        bl_use_preview = False
        bl_use_postprocess = False

        def render(self, depsgraph):
            try:
                if depsgraph.mode != 'RENDER' or depsgraph.view_layer.name != layer.name:
                    raise ValueError('Expected the requested RENDER view layer')
                if result:
                    raise ValueError('Unexpected repeated render callback')
                result['value'] = callback(depsgraph, self.camera_override)
                tile = self.begin_result(0, 0, 1, 1, layer=layer.name)
                tile.layers[0].passes['Combined'].rect = np.zeros((1, 4), np.float32)
                self.end_result(tile)
            except Exception:
                import traceback
                result['error'] = traceback.format_exc()

    old_engine = scene.render.engine
    bpy.utils.register_class(JointCapture)
    try:
        scene.render.engine = JointCapture.bl_idname
        status = bpy.ops.render.render(scene=scene.name, layer=layer.name)
        if status != {'FINISHED'} or 'error' in result or 'value' not in result:
            raise RuntimeError(result.get('error', 'Render callback did not finish'))
        return result['value']
    finally:
        scene.render.engine = old_engine
        bpy.utils.unregister_class(JointCapture)


def _freeze_render_frame(scene, layer):
    """Preserve evaluated local mesh data and arbitrary world affine matrices.

    A private identity parent plus an explicit parent-inverse matrix avoids
    lossy decomposition of shear into TRS. All changes belong to the worker.
    Instancing/non-mesh render geometry await a separate implementation.
    """
    import bpy
    from mathutils import Matrix
    camera, render_geometry = pencil('camera'), pencil('render_geometry')
    rows = []
    from . import band_boundary_witness
    boundary_authors = band_boundary_witness.prepare(scene, layer.objects)

    def capture(depsgraph, override):
        from . import camera_correction, artistic_normals, normal_witness, face_thresholds, face_witness
        from . import face_sdf, sdf_cast, sdf_witness, finish_witness, face_cast_art, cast_art_witness, hair_map_witness
        corrections = []
        sdf_targets = []; cast_targets = []; cast_art_targets = []
        normal_targets = {}
        face_targets = {}
        visible_meshes = {item.object.original for item in depsgraph.object_instances
                          if item.show_self and item.object.type == 'MESH'}
        cel_finish=finish_witness.capture(scene,visible_meshes,depsgraph=depsgraph)
        hair_map=hair_map_witness.capture(scene,visible_meshes,depsgraph=depsgraph)
        boundary=band_boundary_witness.capture(scene,visible_meshes,depsgraph=depsgraph,
                                               authors=boundary_authors)
        for evaluated in depsgraph.objects:
            original = evaluated.original
            if original not in visible_meshes:
                continue
            for modifier in original.modifiers:
                if modifier.show_render and modifier.type=='NODES' and modifier.node_group:
                    profile=modifier.node_group.get(artistic_normals.PROFILE_KEY)
                    if (isinstance(profile,str) and profile.startswith('toon-artistic-normal.v')
                            and not artistic_normals.is_managed(modifier)):
                        raise ValueError('Unsupported artistic-normal profile; author data is preserved: '+profile)
                    face_profile=modifier.node_group.get(face_thresholds.PROFILE_KEY)
                    if (isinstance(face_profile,str) and face_profile.startswith('toon-face-threshold.v')
                            and not face_thresholds.is_managed(modifier)):
                        raise ValueError('Unsupported face-threshold profile; author data is preserved: '+face_profile)
                if (modifier.show_render and modifier.type == 'NODES' and modifier.node_group
                        and modifier.node_group.get(camera_correction.PROFILE_KEY) == camera_correction.PROFILE):
                    checked = camera_correction.validate(original, modifier, depsgraph=depsgraph)
                    if checked['strength'] != 0.0 and checked['camera'] != (override or scene.camera).name:
                        raise ValueError('Correction camera differs from the requested render camera')
                    corrections.append(dict(target=original.name, effect_enabled=checked['strength'] != 0.0,
                                            **checked))
                if modifier.show_render and artistic_normals.is_managed(modifier):
                    if original in normal_targets:
                        raise ValueError('Only one artistic-normal field per render target is qualified')
                    normal_targets[original] = artistic_normals.validate(original, modifier, depsgraph=depsgraph)
                if modifier.show_render and face_thresholds.is_managed(modifier):
                    if original in face_targets:
                        raise ValueError('Only one face-threshold field per render target is qualified')
                    face_targets[original]=face_thresholds.validate(original,modifier,depsgraph=depsgraph)
            for modifier in original.modifiers:
                if modifier.type == 'NODES' and modifier.node_group is not None:
                    profile = modifier.node_group.get(sdf_cast._PROFILE)
                    if isinstance(profile, str) and profile.startswith('toon-face-sdf-cast.v') and not sdf_cast.is_managed(modifier):
                        raise ValueError('Unknown SDF cast profile')
                if modifier.show_render and sdf_cast.is_managed(modifier):
                    faces=[m for m in original.modifiers if face_thresholds.is_managed(m) and m.show_render]
                    if len(faces)!=1:raise ValueError('SDF cast requires one enabled face carrier')
                    checked=sdf_cast.validate(original,faces[0],modifier,depsgraph=depsgraph)
                    checked.pop('field_snapshot', None)
                    checked['shader_before']=_cast_shader_snapshot(modifier.node_group)
                    checked['binding_before']=_cast_binding_snapshot(original,checked['bindings'])
                    checked['_reader_group']=modifier.node_group
                    cast_targets.append(checked)
            for reader in bpy.data.node_groups:
                if reader.bl_idname != 'ShaderNodeTree' or reader.get(face_sdf._TARGET) != original or not reader.users:
                    continue
                profile=reader.get(face_sdf.PROFILE_KEY)
                if (isinstance(profile,str) and profile.startswith('toon-face-sdf-shader.v')
                        and profile not in face_sdf.SUPPORTED_PROFILES):
                    raise ValueError('Unknown face SDF texture reader profile')
                if not isinstance(profile,str) or profile not in face_sdf.SUPPORTED_PROFILES:continue
                live=face_sdf.validate_reader(reader,target=original,require_live=True)
                raw=live['descriptor']; carrier=raw['carrier']; face=face_targets.get(original)
                if face is None:raise ValueError('SDF image reader requires an enabled face carrier')
                checked=dict(profile=raw['profile'],status='valid',owner=raw['owner'],
                    carrier_owner=carrier['core_owner'],target=original.name,modifier_uid=raw['modifier_uid'],
                    attributes=carrier['fields'],carrier_contract=face_sdf.CARRIER_CONTRACT,
                    **{key:carrier[key] for key in ('left_attribute','right_attribute','weight_attribute')},
                    uv_name=raw['uv']['uv_name'],channel=raw['channel'],mirror=raw['mirror'],
                    interpolation='Linear',extension='EXTEND',reader_signature=raw['shader']['signature'],
                    image=raw['image'],descriptor_before=raw,source_uv=sdf_witness.snapshot(original.data,raw['uv']['uv_name']),
                    source_carrier=_sdf_carrier_snapshot(original.data,face),binding_before=_sdf_binding_snapshot(reader),
                    _reader_group=reader)
                if raw['profile'] in (face_sdf.FILTER_PROFILE, face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    checked['sampling'] = raw['sampling']
                if raw['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    checked['direction_encoding'] = raw['direction_encoding']
                    checked['vertical_source'] = raw['vertical_source']
                    checked['source_vertical_uv'] = sdf_witness.snapshot(original.data,raw['vertical_source']['uv']['uv_name'])
                    sdf_witness._vertical_source(checked['vertical_source'],checked)
                if raw['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    checked['direction_correction'] = raw['direction_correction']
                    checked['direction_source'] = raw['direction_source']
                    checked['source_direction_uv'] = sdf_witness.snapshot(original.data,raw['direction_source']['uv']['uv_name'])
                    sdf_witness._direction_source(checked['direction_source'],checked)
                if raw['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    checked.update(continuity_keys=raw['continuity_keys'],pair_source=raw['pair_source'])
                    if raw['pair_source'] is not None:
                        checked['source_pair_uv']=sdf_witness.snapshot(original.data,raw['pair_source']['uv']['uv_name'])
                        sdf_witness._vertical_uv(checked['source_pair_uv'],raw['pair_source']['uv']['uv_name'],checked['source_uv'])
                sdf_targets.append(checked)
            for material in {slot.material for slot in original.material_slots if slot.material}:
                if not face_cast_art.is_present(material):
                    continue
                actual = face_cast_art.snapshot(material, depsgraph)
                if actual['target'] != original.name:
                    raise ValueError('Face cast-art bound target differs from render object')
                cast_art_targets.append(dict(
                    **{key:actual[key] for key in ('profile','owner','target','material','sdf_owner',
                        'modifier_uid','uv_name','fields','channel','settings','image')},
                    status='valid', descriptor_before=actual, _material=material))
        geometry = render_geometry.capture(depsgraph)
        frame = camera.capture_render(depsgraph.scene_eval, depsgraph, camera=override)
        seen = set()
        for instance in depsgraph.object_instances:
            evaluated = instance.object
            if not instance.show_self:
                continue
            if instance.is_instance:
                raise ValueError('Joint-grid v1 does not yet freeze render instances')
            if evaluated.type in {'EMPTY', 'LIGHT', 'CAMERA', 'ARMATURE', 'LATTICE'}:
                continue
            if evaluated.type != 'MESH':
                raise ValueError('Joint-grid v1 requires mesh render surfaces: '+evaluated.name)
            original = evaluated.original
            if original in seen or original.library or original.override_library:
                raise ValueError('Repeated/linked/override mesh requires a separate freeze adapter')
            if original.particle_systems:
                raise ValueError('Particle systems require a separate joint render adapter')
            seen.add(original)
            mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
            for checked in sdf_targets:
                if checked['target']!=original.name:continue
                checked['before']=sdf_witness.snapshot(evaluated.data,checked['uv_name'])
                checked['copied']=sdf_witness.snapshot(mesh,checked['uv_name'])
                checked['render_carrier']=_sdf_carrier_snapshot(evaluated.data,face_targets[original])
                if checked['before']!=checked['copied']:raise ValueError('Mesh copying changed face SDF UV')
                if checked['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    name=checked['vertical_source']['uv']['uv_name']
                    checked['vertical_before']=sdf_witness.snapshot(evaluated.data,name)
                    checked['vertical_copied']=sdf_witness.snapshot(mesh,name)
                    sdf_witness._vertical_uv(checked['vertical_before'],name,checked['before'])
                    sdf_witness._vertical_uv(checked['vertical_copied'],name,checked['copied'])
                    if checked['vertical_before']!=checked['vertical_copied']:
                        raise ValueError('Mesh copying changed independent polar face SDF UV')
                if checked['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                    name=checked['direction_source']['uv']['uv_name']
                    checked['direction_before']=sdf_witness.snapshot(evaluated.data,name)
                    checked['direction_copied']=sdf_witness.snapshot(mesh,name)
                    sdf_witness._vertical_uv(checked['direction_before'],name,checked['before'])
                    sdf_witness._vertical_uv(checked['direction_copied'],name,checked['copied'])
                    if checked['direction_before']!=checked['direction_copied']:
                        raise ValueError('Mesh copying changed direction-key face SDF UV')
                if checked['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE) and checked['pair_source'] is not None:
                    name=checked['pair_source']['uv']['uv_name']
                    checked['pair_before']=sdf_witness.snapshot(evaluated.data,name)
                    checked['pair_copied']=sdf_witness.snapshot(mesh,name)
                    sdf_witness._vertical_uv(checked['pair_before'],name,checked['before'])
                    sdf_witness._vertical_uv(checked['pair_copied'],name,checked['copied'])
                    if checked['pair_before']!=checked['pair_copied']:
                        raise ValueError('Mesh copying changed continuous face-SDF pair UV')
            for checked in cast_targets:
                if checked['target']!=original.name:continue
                checked['before']=normal_witness.snapshot(evaluated.data,[checked['normal_attribute']])
                checked['copied']=normal_witness.snapshot(mesh,[checked['normal_attribute']])
                if checked['before']!=checked['copied']:raise ValueError('Mesh copying changed SDF cast direction')
            if original in normal_targets:
                checked = normal_targets[original]
                names = list(checked['attributes'].values())
                before = normal_witness.snapshot(evaluated.data, names)
                copied = normal_witness.snapshot(mesh, names)
                if before != copied:
                    raise ValueError('Mesh copying changes artistic-normal shading data: '+original.name)
                checked.update(before=before, copied=copied)
            if original in face_targets:
                checked=face_targets[original];names=list(checked['attributes'].values())
                before=face_witness.snapshot(evaluated.data,names)
                copied=face_witness.snapshot(mesh,names)
                if before!=copied:
                    raise ValueError('Mesh copying changes face-threshold shading data: '+original.name)
                checked.update(before=before,copied=copied)
            slots = [(slot.link, slot.material.original if slot.material else None)
                     for slot in evaluated.material_slots]
            # Evaluated material pointers die with the render depsgraph.
            # Only original Main IDs may cross this callback boundary.
            for index, (_, material) in enumerate(slots):
                if index < len(mesh.materials):
                    mesh.materials[index] = material
            rows.append((original, mesh, instance.matrix_world.copy(), slots))
        sdf_schema = sdf_witness.schema_for_targets(sdf_targets)
        if sdf_schema in (sdf_witness.MATERIAL_SCHEMA, sdf_witness.POLAR_SCHEMA, sdf_witness.DIRECTION_SCHEMA, sdf_witness.CONTINUITY_SCHEMA, sdf_witness.AUTHORED_SCHEMA):
            for checked in sdf_targets:
                checked['material_binding_before'] = _sdf_material_snapshot(
                    bpy.data.objects[checked['target']], checked['_reader_group'])
        cam = (override or depsgraph.scene_eval.camera).evaluated_get(depsgraph)
        result = dict(frame=frame, geometry_sha256=_geometry_identity(geometry),
                    triangles=geometry.triangle_count, camera_data=cam.data.copy(),
                    camera_world=cam.matrix_world.copy(), corrections=corrections,
                    artistic_normals=dict(schema=normal_witness.SCHEMA, targets=list(normal_targets.values())),
                    face_thresholds=dict(schema=face_witness.schema_for_targets(list(face_targets.values())), targets=list(face_targets.values())),
                    face_sdf=dict(schema=sdf_schema,targets=sdf_targets),
                    sdf_cast=dict(schema=sdf_witness.cast_schema_for_targets(cast_targets),targets=cast_targets),cel_finish=cel_finish,
                    hair_map=hair_map,
                    cast_art=dict(schema=cast_art_witness.SCHEMA,targets=cast_art_targets,
                        render_time=dict(scene=depsgraph.scene_eval.name,frame=depsgraph.scene_eval.frame_current,
                                         subframe=depsgraph.scene_eval.frame_subframe)))
        if boundary['targets']:
            result['band_boundary'] = boundary
        return result

    state = _run_render_callback(scene, layer, capture)
    anchor = bpy.data.objects.new('ToonJointFrameAnchor', None)
    scene.collection.objects.link(anchor)
    for original, mesh, matrix, slots in rows:
        original.animation_data_clear()
        original.constraints.clear()
        original.modifiers.clear()
        original.parent = anchor
        original.matrix_parent_inverse = matrix
        original.matrix_basis = Matrix.Identity(4)
        original.data = mesh
        for slot, (link, material) in zip(original.material_slots, slots):
            slot.link = link
            slot.material = material
    data = state.pop('camera_data')
    data.animation_data_clear()
    cam = bpy.data.objects.new('ToonJointSampleCamera', data)
    scene.collection.objects.link(cam)
    cam.parent = anchor
    cam.matrix_parent_inverse = state.pop('camera_world')
    cam.matrix_basis = Matrix.Identity(4)
    # The effective shot camera was resolved by the source render callback.
    # Sample jitter must not be reset to a marker's author camera thereafter.
    # Clearing a marker itself updates scene.camera. Assign the frozen camera
    # only after every marker has been detached (including future markers).
    for marker in scene.timeline_markers:
        if marker.camera is not None:
            marker.camera = None
    scene.camera = cam
    layer.update()
    from . import normal_witness
    for row in state['artistic_normals']['targets']:
        original = bpy.data.objects[row['target']]
        row['frozen'] = _run_render_callback(scene, layer, lambda graph, override:
            normal_witness.snapshot(original.evaluated_get(graph).data, list(row['attributes'].values())))
        row['data_exact'] = row['before'] == row['copied'] == row['frozen']
    normal_witness.validate_receipt(state['artistic_normals'])
    from . import face_witness
    for row in state['face_thresholds']['targets']:
        original=bpy.data.objects[row['target']]
        row['frozen']=_run_render_callback(scene,layer,lambda graph,override:
            face_witness.snapshot(original.evaluated_get(graph).data,list(row['attributes'].values())))
        row['data_exact']=row['before']==row['copied']==row['frozen']
    face_witness.validate_receipt(state['face_thresholds'])
    from . import face_sdf, sdf_cast, sdf_witness
    for row in state['face_sdf']['targets']:
        reader=row.pop('_reader_group');original=bpy.data.objects[row['target']]
        row['frozen']=_run_render_callback(scene,layer,lambda graph,override:
            sdf_witness.snapshot(original.evaluated_get(graph).data,row['uv_name']))
        if row['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            name=row['vertical_source']['uv']['uv_name']
            row['vertical_frozen']=_run_render_callback(scene,layer,lambda graph,override:
                sdf_witness.snapshot(original.evaluated_get(graph).data,name))
        if row['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            name=row['direction_source']['uv']['uv_name']
            row['direction_frozen']=_run_render_callback(scene,layer,lambda graph,override:
                sdf_witness.snapshot(original.evaluated_get(graph).data,name))
        if row['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE) and row['pair_source'] is not None:
            name=row['pair_source']['uv']['uv_name']
            row['pair_frozen']=_run_render_callback(scene,layer,lambda graph,override:
                sdf_witness.snapshot(original.evaluated_get(graph).data,name))
        row['descriptor_frozen']=face_sdf.reader_descriptor(reader)
        row['binding_frozen']=_sdf_binding_snapshot(reader)
        if state['face_sdf']['schema'] in (sdf_witness.MATERIAL_SCHEMA, sdf_witness.POLAR_SCHEMA, sdf_witness.DIRECTION_SCHEMA, sdf_witness.CONTINUITY_SCHEMA, sdf_witness.AUTHORED_SCHEMA):
            row['material_binding_frozen'] = _sdf_material_snapshot(original, reader)
        row['data_exact']=(row['before']==row['copied']==row['frozen'] and row['descriptor_before']==row['descriptor_frozen']
                           and row['binding_before']==row['binding_frozen'])
        if state['face_sdf']['schema'] in (sdf_witness.MATERIAL_SCHEMA, sdf_witness.POLAR_SCHEMA, sdf_witness.DIRECTION_SCHEMA, sdf_witness.CONTINUITY_SCHEMA, sdf_witness.AUTHORED_SCHEMA):
            row['data_exact'] = row['data_exact'] and row['material_binding_before'] == row['material_binding_frozen']
        if row['profile'] in (face_sdf.POLAR_PROFILE, face_sdf.DIRECTION_PROFILE, face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            row['data_exact'] = row['data_exact'] and row['vertical_before']==row['vertical_copied']==row['vertical_frozen']
        if row['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
            row['data_exact'] = row['data_exact'] and row['direction_before']==row['direction_copied']==row['direction_frozen']
        if row['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE) and row['pair_source'] is not None:
            row['data_exact'] = row['data_exact'] and row['pair_before']==row['pair_copied']==row['pair_frozen']
    for row in state['sdf_cast']['targets']:
        group=row.pop('_reader_group');original=bpy.data.objects[row['target']]
        row['frozen']=_run_render_callback(scene,layer,lambda graph,override:
            normal_witness.snapshot(original.evaluated_get(graph).data,[row['normal_attribute']]))
        row['shader_frozen']=_cast_shader_snapshot(group)
        row['binding_frozen']=_cast_binding_snapshot(original,row['bindings'])
        row['data_exact']=(row['before']==row['copied']==row['frozen'] and row['shader_before']==row['shader_frozen']
                           and row['binding_before']==row['binding_frozen'])
    sdf_witness.validate_receipt(state['face_sdf'],state['face_thresholds'])
    sdf_witness.validate_cast_receipt(state['sdf_cast'],state['face_thresholds'])
    from . import face_cast_art, cast_art_witness
    for row in state['cast_art']['targets']:
        material = row.pop('_material')
        row['descriptor_frozen'] = _run_render_callback(scene, layer, lambda graph, override:
            face_cast_art.snapshot_frozen(material, graph, row['descriptor_before'],
                expected_render_time=state['cast_art'].get('render_time')))
        row['data_exact'] = row['descriptor_before'] == row['descriptor_frozen']
    cast_art_witness.validate_receipt(state['cast_art'],state['face_sdf'],state['face_thresholds'],
                                    expected_render_time=state['cel_finish']['render_time'])
    from . import finish_witness
    finish_witness.verify_frozen(state['cel_finish'])
    from . import hair_map_witness
    hair_map_witness.verify_frozen(state['hair_map'])
    if 'band_boundary' in state:
        band_boundary_witness.verify_frozen(state['band_boundary'])
    return state


def _opaque_materials(scene, *, components=None):
    """Conservative admission for the first opaque-surface joint profile."""
    from . import component_slots
    if components is None:
        component_slots.snapshot(scene)
    else:
        from . import component_witness
        component_witness.verify_frozen(components)
    from . import eye_overlays
    eye_layers = eye_overlays.collect(scene)
    eye_slots = {(row['target'],row['slot']) for row in eye_layers}
    visited = set()
    allowed = {'ShaderNodeEmission', 'ShaderNodeBackground', 'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfGlossy',
               'ShaderNodeBsdfAnisotropic', 'ShaderNodeBsdfPrincipled', 'ShaderNodeBsdfToon',
               'ShaderNodeMixShader', 'ShaderNodeAddShader', 'NodeGroupInput'}

    def scan(tree):
        if tree in visited:
            return
        visited.add(tree)
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeGroup':
                scan(node.node_tree)
            elif node.bl_idname == 'ShaderNodeBsdfPrincipled':
                for key, expected in (('Alpha', 1.), ('Transmission Weight', 0.)):
                    socket = node.inputs[key]
                    if socket.is_linked or abs(socket.default_value-expected) > 1e-8:
                        raise ValueError('Transparent/transmissive surfaces await joint optical qualification')
            elif any(socket.type == 'SHADER' for socket in node.outputs) and node.bl_idname not in allowed:
                from .lighting_53 import is_owned_group
                if node.bl_idname != 'ShaderNodeLightAccumulation' or not is_owned_group(tree):
                    raise ValueError('Unqualified surface shader in joint opaque profile: '+node.bl_idname)
            if node.bl_idname in {'ShaderNodeOutputMaterial', 'ShaderNodeOutputWorld'}:
                if node.inputs.get('Volume') and node.inputs['Volume'].is_linked:
                    raise ValueError('Volume shading awaits a joint optical adapter')
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.hide_render:
            continue
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is not None:
                if (obj.name,index) in eye_slots:
                    continue
                if material.node_tree:
                    scan(material.node_tree)
                elif material.diffuse_color[3] != 1:
                    raise ValueError('Non-opaque material is outside joint-grid v1')
    if scene.world and scene.world.node_tree:
        scan(scene.world.node_tree)


def _admit_drivers():
    """Do not silently replace a GUI's Python driver semantics with bypasses."""
    import bpy
    blocks = []
    for name in ('objects', 'meshes', 'shape_keys', 'materials', 'node_groups',
                 'worlds', 'lights', 'cameras', 'scenes'):
        blocks.extend(getattr(bpy.data, name))
    blocks.extend(block.node_tree for block in list(blocks)
                  if getattr(block, 'node_tree', None) is not None)
    seen = set()
    count = 0
    for block in blocks:
        if block in seen:
            continue
        seen.add(block)
        animation = block.animation_data
        if animation is None:
            continue
        for curve in animation.drivers:
            if curve.driver.type == 'SCRIPTED' and not curve.driver.is_simple_expression:
                raise ValueError('Non-simple scripted drivers require a separate reviewed joint adapter')
            count += 1
    return dict(admitted_drivers=count, python_scripted_driver_policy='simple expressions only; no autoexec enablement')


def _shift_projection(camera, scene, layer, baseline, offset, width, height):
    """Solve actual Blender camera-shift derivatives; support both projections."""
    import bpy
    import numpy as np
    from mathutils import Vector
    data = camera.data
    def projection():
        layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        return camera.calc_matrix_camera(graph, x=width, y=height,
            scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y)
    def center(matrix):
        p = matrix @ Vector((0, 0, -1, 1))
        return np.asarray(p[:2], np.float64) / p.w
    data.shift_x, data.shift_y = baseline
    data.update_tag()
    original = projection()
    c0 = center(original)
    columns = []
    step = .125
    for key in ('shift_x', 'shift_y'):
        setattr(data, key, getattr(data, key)+step)
        data.update_tag()
        columns.append((center(projection())-c0)/step)
        data.shift_x, data.shift_y = baseline
        data.update_tag()
    desired = -2 * np.asarray(offset, np.float64) / (width, height)
    change = np.linalg.solve(np.column_stack(columns), desired)
    data.shift_x = baseline[0]+float(change[0])
    data.shift_y = baseline[1]+float(change[1])
    data.update_tag()
    actual = (center(projection())-c0) * (width/2, height/2)
    error = float(np.max(abs(actual+offset)))
    # Camera matrices and RNA are float32. This bound scales with image size.
    bound = float(8*np.finfo(np.float32).eps*max(width, height))
    if not np.isfinite(error) or error > bound:
        raise ValueError('Camera shift could not reproduce the requested joint sample')
    return error


def render(scene, layer, output_directory, *, samples_per_axis=2,
           memory_budget_bytes=512*1024*1024, lighting_samples=64, progress=None):
    """Run in a disposable background process, save authoring and output bundle.

    The caller must provide a new absolute directory in an existing parent.
    This first profile deliberately rejects unsupported optical cases instead
    of changing source scene settings and calling them verified features.
    """
    import bpy
    import numpy as np
    from . import media_render_ui
    if media_render_ui.snapshot(scene) is not None:
        from . import media_render
        return media_render.render(scene,layer,output_directory,samples_per_axis=samples_per_axis,
            memory_budget_bytes=memory_budget_bytes,lighting_samples=lighting_samples,progress=progress)
    camera, render_geometry = pencil('camera'), pencil('render_geometry')
    save_image_new = pencil('output').save_image_new
    if not bpy.app.background:
        raise ValueError('Joint rendering must run in an isolated background worker')
    if scene.render.engine != 'BLENDER_EEVEE':
        raise ValueError('This explicit joint-grid profile requires EEVEE')
    from .runtime_compat import require_joint_build
    require_joint_build(bpy.app)
    if not scene.camera:
        raise ValueError('An explicit scene camera is required')
    if (scene.render.use_multiview or scene.camera.data.dof.use_dof
            or scene.render.use_motion_blur):
        raise ValueError('Multiview/DOF/motion blur require separate joint sampling adapters')
    if not hasattr(scene, 'toon_viewport_preview'):
        raise ValueError('Register the stable Pencil author core before opening the source')
    if layer.id_data != scene:
        raise ValueError('View layer does not belong to the scene')
    if bpy.context.scene != scene or bpy.context.view_layer != layer:
        raise ValueError('Set the isolated worker context to the requested scene/view layer')
    driver_policy = _admit_drivers()
    from .joint_workflow import admit_legacy
    admit_legacy(scene)
    from . import camera_shots, head_normals
    head_normal_config = head_normals.apply(scene)
    shot_correction = camera_shots.apply(scene)
    layer.update()
    _opaque_materials(scene)
    from . import eye_overlays
    eye_before = eye_overlays.collect(scene)
    base_frame = camera.capture(bpy.context)
    width, height = base_frame['width'], base_frame['height']
    pencil_addon = pencil('addon')
    element_count = len(pencil_addon.element_configuration(scene.toon_viewport_preview))
    admission = joint_sampling.admit(width, height, samples_per_axis, memory_budget_bytes,
                                     lighting_samples, element_count)
    root = Path(output_directory)
    if not root.is_absolute() or not root.parent.is_dir() or root.exists():
        raise ValueError('Choose a new absolute joint output directory')
    source = Path(bpy.data.filepath)
    if not source.is_file():
        raise ValueError('Save an authoring source before joint rendering')
    source_sha = _digest(source)
    root.mkdir()
    started = time.perf_counter()
    progress = progress or (lambda *args, **kwargs: None)
    checkpoint = _progress_callback(root, progress)
    if bpy.ops.wm.save_as_mainfile(filepath=str(root/'author.blend'), copy=True, check_existing=False) != {'FINISHED'}:
        raise RuntimeError('Authoring checkpoint did not save')
    for candidate in scene.view_layers:
        candidate.use = candidate == layer
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    # Border extraction is done once after joint resolve, retaining full-frame
    # projection and cross-sample alignment for both cropped and full outputs.
    scene.render.use_border = False
    author_sampling = dict(taa_render_samples=int(scene.eevee.taa_render_samples),
                           filter_size=float(scene.render.filter_size))
    # Zero spatial filter keeps every inner EEVEE lighting sample at the
    # outer grid position. The opaque line image is invariant under those
    # lighting samples, so averaging beauty before its local over is linear.
    # Qualification: Blender 5.2.1 / 9e2066aef7ef. Other builds need evidence.
    scene.eevee.taa_render_samples = lighting_samples
    scene.render.filter_size = 0.0
    if scene.render.filter_size != 0.0 or scene.eevee.taa_render_samples != lighting_samples:
        raise ValueError('Blender did not retain the fixed-position lighting settings')
    fmt = scene.render.image_settings
    fmt.file_format = 'OPEN_EXR'; fmt.color_mode = 'RGBA'; fmt.color_depth = '32'; fmt.exr_codec = 'ZIP'
    fmt.color_management = 'OVERRIDE'; fmt.linear_colorspace_settings.name = 'Linear Rec.709'
    from . import shot_look_ui,shot_look_output
    look_before = shot_look_ui.snapshot(scene)
    regional=look_before is not None and look_before.get('schema')=='toon-shot-look.regions.v2'
    region_objects=[]
    if regional:
        from . import shot_regions
        region_objects=[row.object for row in scene.toon_shot_look.background_objects]
        if any(o is None or scene.objects.get(o.name)!=o for o in region_objects):raise ValueError('Regional background Object binding is missing')
        if sorted(o.name for o in region_objects)!=look_before['background_objects']:raise ValueError('Regional background identities changed')
        eye_targets={row['target'] for row in eye_before}
        if any(o.name in eye_targets for o in region_objects):raise ValueError('Declared eye layers cannot be background partition targets')
    author_capture=dict(objects=region_objects) if regional else None
    checkpoint('AUTHOR_REFERENCE')
    author_beauty = _beauty(scene, layer, root/'author-reference.exr', width, height,capture=author_capture)
    author_line, author_camera, author_geometry, author_bundle = _line(scene, layer, checkpoint, include_bundle=True)
    author_style = author_bundle['style']
    element_config = author_bundle['element_config']
    checkpoint('FREEZING_RENDER_GEOMETRY')
    frozen = _freeze_render_frame(scene, layer)
    if head_normal_config is not None:
        visible_owners={row['owner'] for row in frozen['artistic_normals']['targets']}
        for binding in head_normal_config['bindings']:
            binding['render_visible']=binding['owner'] in visible_owners
    if frozen['geometry_sha256'] != author_geometry:
        raise ValueError('Source RENDER geometry changed between reference and freeze')
    # The viewport capture above is admission/crop metadata only. Camera
    # matrices and time in the published receipt come from actual RENDER.
    crop = base_frame['metadata']
    base_frame = frozen['frame']
    for key in ('use_border', 'use_crop_to_border', 'border_rect_px', 'output_extent'):
        base_frame['metadata'][key] = crop[key]
    base_frame['output_width'], base_frame['output_height'] = crop['output_extent']
    _opaque_materials(scene, components=frozen['cel_finish'].get('components'))  # Evaluated GN remains scanned.
    eye_frozen = eye_overlays.collect(scene)
    if eye_before != eye_frozen:
        raise ValueError('Eye-layer shader/binding changed during frame freezing')
    scene.render.engine = 'BLENDER_EEVEE'
    checkpoint('FROZEN_REFERENCE')
    frozen_capture=dict(objects=region_objects) if regional else None
    frozen_beauty = _beauty(scene, layer, root/'frozen-reference.exr', width, height,capture=frozen_capture)
    if regional and not np.array_equal(author_capture['mask'],frozen_capture['mask']):
        raise ValueError('Freezing changed actual regional RENDER coverage')
    frozen_line, _, _, frozen_bundle = _line(scene, layer, checkpoint, frozen['geometry_sha256'], include_bundle=True,
        finish_receipt=frozen['cel_finish'],cast_art_receipt=frozen['cast_art'],sdf_receipt=frozen['face_sdf'],
        hm_receipt=frozen['hair_map'],boundary_receipt=frozen.get('band_boundary'))
    reference = dict(beauty_exact=bool(np.array_equal(author_beauty, frozen_beauty)),
        line_exact=bool(np.array_equal(author_line, frozen_line)),
        beauty_max_error=float(np.max(abs(author_beauty-frozen_beauty))),
        line_max_error=float(np.max(abs(author_line-frozen_line))),
        elements_exact=bool(element_config == frozen_bundle['element_config'] and
            set(author_bundle['elements']) == set(frozen_bundle['elements']) and all(
                np.array_equal(value, frozen_bundle['elements'][name])
                for name,value in author_bundle['elements'].items())))
    material_colors = author_style.get('line_material_colors')
    material_color_snapshot = material_colors['resolved'] if material_colors is not None else None
    material_uv_snapshot = material_colors.get('uv_sources') if material_colors is not None else None
    def verify_material_colors(bundle):
        row=bundle['style'].get('line_material_colors')
        actual=row['resolved'] if row is not None else None
        uv=row.get('uv_sources') if row is not None else None
        if uv!=material_uv_snapshot:raise ValueError('UV line source changed within frozen RENDER frame')
        if actual!=material_color_snapshot:
            raise ValueError('Material line-color source changed within frozen RENDER frame')
    verify_material_colors(frozen_bundle)
    eye_snapshot = author_bundle.get('eye_snapshot')
    hair_snapshot = author_bundle.get('hair_snapshot')
    if hair_snapshot != frozen_bundle.get('hair_snapshot'):
        raise ValueError('Fixed hair shadow shader/image/RENDER UV changed during freezing')
    if eye_snapshot != frozen_bundle.get('eye_snapshot'):
        raise ValueError('Eye-layer RENDER triangle/UV data changed during freezing')
    if not reference['beauty_exact'] or not reference['line_exact'] or not reference['elements_exact']:
        (root/'freeze-reference-failed.json').write_text(json.dumps(reference, indent=2), encoding='utf8')
        raise ValueError('Frame freezing changes source beauty or line; this input requires another adapter')
    del author_beauty, author_line, frozen_beauty, frozen_line, author_bundle, frozen_bundle
    baseline = (scene.camera.data.shift_x, scene.camera.data.shift_y)
    accumulator = joint_sampling.Accumulator(width, height)
    region_accumulator=shot_regions.Accumulator((height,width,4)) if regional else None
    samples = joint_sampling.offsets(samples_per_axis)
    sample_records = []
    element_sums = {}
    beauty_path = root/'working-beauty.exr'
    for index, offset in enumerate(samples):
        checkpoint('SAMPLE', index=index, total=len(samples), offset=offset)
        shift_error = _shift_projection(scene.camera, scene, layer, baseline, offset, width, height)
        sample_capture=dict(objects=region_objects) if regional else None
        beauty = _beauty(scene, layer, beauty_path, width, height,capture=sample_capture)
        line, sample_camera, _, bundle = _line(scene, layer, checkpoint, frozen['geometry_sha256'], include_bundle=True,
            finish_receipt=frozen['cel_finish'],cast_art_receipt=frozen['cast_art'],sdf_receipt=frozen['face_sdf'],
            hm_receipt=frozen['hair_map'],boundary_receipt=frozen.get('band_boundary'))
        verify_material_colors(bundle)
        if bundle['element_config'] != element_config:
            raise ValueError('Author element configuration changed within a frozen frame')
        if bundle.get('eye_snapshot') != eye_snapshot:
            raise ValueError('Eye-layer RENDER triangle/UV data changed between samples')
        if bundle.get('hair_snapshot') != hair_snapshot:
            raise ValueError('Fixed hair shadow shader/image/RENDER UV changed between samples')
        for name,value in bundle['elements'].items():
            if name not in element_sums:element_sums[name] = np.zeros_like(value, dtype=np.float64)
            element_sums[name] += value
        accumulator.add(beauty, line)
        if regional:region_accumulator.add(beauty,line,sample_capture['mask'],sample_capture['metadata'])
        sample_records.append(dict(index=index, offset=list(offset), camera_shift_error_px=shift_error,
                                   camera=sample_camera))
    passes, composition = accumulator.resolve(len(samples))
    if shot_look_ui.snapshot(scene) != look_before:raise ValueError('Shot Look author settings changed during rendering')
    look_pixels = look_receipt = None
    region_pixels=None
    if look_before is not None:
        if regional:
            resolved_regions,region_capture=region_accumulator.resolve(len(samples),passes['Combined'])
            region_capture['author_frozen_masks_exact']=True
            look_pixels,look_receipt,region_pixels=shot_look_output.build_regions(passes['Combined'],base_frame,look_before,
                line=passes['Line'],regions=resolved_regions,capture=region_capture)
        else:look_pixels,look_receipt=shot_look_output.build(passes['Combined'],base_frame,look_before,line=passes['Line'])
    passes = {name:camera.extract_pixels(base_frame, value) for name, value in passes.items()}
    elements = {name:camera.extract_pixels(base_frame, (value/len(samples)).astype(np.float32))
                for name,value in element_sums.items()}
    checkpoint('SAVING')
    artifacts = {}
    for name, pixels in passes.items():
        artifacts[name] = save_image_new(root/(name+'.exr'), pixels, pixel_aspect=base_frame['metadata']['pixel_aspect'])
    artifacts['preview'] = save_image_new(root/'Combined.png', passes['Combined'], pixel_aspect=base_frame['metadata']['pixel_aspect'])
    np.savez_compressed(root/'correlated-layers.npz', **passes)
    artifacts['correlated_arrays'] = dict(path=str(root/'correlated-layers.npz'),
        sha256=_digest(root/'correlated-layers.npz'))
    if look_receipt is not None:
        artifacts['Look']=save_image_new(root/'Look.exr',look_pixels,pixel_aspect=base_frame['metadata']['pixel_aspect'])
        artifacts['look_preview']=save_image_new(root/'Look.png',look_pixels,pixel_aspect=base_frame['metadata']['pixel_aspect'])
        np.savez_compressed(root/'look.npz',Look=look_pixels)
        artifacts['look_arrays']=dict(path=str(root/'look.npz'),sha256=_digest(root/'look.npz'))
        if region_pixels is not None:
            np.savez_compressed(root/'look-regions.npz',**region_pixels)
            artifacts['look_region_arrays']=dict(path=str(root/'look-regions.npz'),sha256=_digest(root/'look-regions.npz'))
    if elements:
        save_multilayer_new = pencil('multilayer_output').save_multilayer_new
        artifacts['line_elements_exr'] = save_multilayer_new(root/'LineElements.exr',
            dict(Combined=passes['Line'], **elements), pixel_aspect=base_frame['metadata']['pixel_aspect'])
        np.savez_compressed(root/'line-elements.npz', **elements)
        artifacts['line_element_arrays'] = dict(path=str(root/'line-elements.npz'),sha256=_digest(root/'line-elements.npz'))
    if _digest(source) != source_sha:
        raise ValueError('Authoring source changed while the worker was rendering')
    report = dict(schema='toon-joint-report.v4' if eye_before else 'toon-joint-report.v3',
        profile='eevee_joint_grid.eye.v1' if eye_before else joint_sampling.PROFILE, experimental=True,
        camera_shot=shot_correction,
        head_normal_config=head_normal_config,
        status='completed', source=dict(file=str(source), sha256=source_sha, unchanged=True),
        author_checkpoint=str(root/'author.blend'), frame=base_frame['metadata'],
        author_sha256=_digest(root/'author.blend'),
        author_frozen_reference=reference,
        blender=dict(version=bpy.app.version_string, build_hash=bpy.app.build_hash.decode()),
        driver_policy=driver_policy,
        lighting=dict(samples_per_position=lighting_samples, filter_size=0.0,
            author_sampling=author_sampling, line_invariant_under_lighting_samples=True,
            contract='fixed-position-managed-eye-lighting.v1' if eye_before else 'fixed-position-opaque-lighting.v1'),
        camera_corrections=frozen['corrections'],
        artistic_normals=frozen['artistic_normals'],
        face_thresholds=frozen['face_thresholds'],
        face_sdf=frozen['face_sdf'], sdf_cast=frozen['sdf_cast'], cel_finish=frozen['cel_finish'],
        cast_art=frozen['cast_art'],
        author_style=author_style, line_element_config=element_config,
        line_element_semantics='authored COLOR/axial-DEPTH RGBA; common outer spatial resolve; not optical scene depth',
        frozen_render_geometry=dict(sha256=frozen['geometry_sha256'], triangles=frozen['triangles'],
            contract='single-frame-local-mesh-and-full-affine.v1', author_meshes_are_not_line_carriers=True),
        admission=admission, composition=composition, samples=sample_records, artifacts=artifacts,
        source_text_executed=False, elapsed_seconds=time.perf_counter()-started,
        limitations=['opaque mesh surfaces only', 'no instances or linked/library overrides yet',
            'no optical transparency, volumes, DOF or motion blur yet',
            'native EEVEE multi-sample lighting/AA equivalence is not claimed',
            'separate resolved Beauty and Line require BeautyLineInteraction to reconstruct this result',
            'changing author parameters requires a new joint render'])
    if material_color_snapshot is not None:
        report['line_material_colors']=dict(before=material_color_snapshot,frozen=material_color_snapshot,data_exact=True)
    if frozen['hair_map']['targets']:
        report['hair_map']=frozen['hair_map']
    if 'band_boundary' in frozen:
        from . import band_boundary_witness
        boundary_receipt = band_boundary_witness.portable(frozen['band_boundary'])
        band_boundary_witness.validate_receipt(boundary_receipt,
            expected_render_time=frozen['cel_finish']['render_time'])
        report['band_boundary'] = boundary_receipt
    if hair_snapshot:
        report['hair_fixed_shadows']=dict(profile='toon-hair-fixed-shadow-freeze.v1',
            before=hair_snapshot,frozen=hair_snapshot,data_exact=True)
    if eye_before:
        report['eye_layers'] = dict(profile=eye_overlays.PROFILE, before=eye_before,
            frozen=eye_frozen, data_exact=True, render_snapshot=eye_snapshot,
            beauty='opaque EEVEE receivers with analytic unlit eye layers in physical ray-distance order',
            line_color='per-fragment E+T*rgb for eye layers strictly in front of the source line; Alpha unchanged',
            line_elements='COLOR includes eye transfer; DEPTH retains authored raw axial encoding',
            limitations=['closed static unlit eye shaders only; no refraction or general glass',
                'one nearest hit per material; folded or repeated same-material sheets are not qualified',
                'native material preview uses BLENDED; joint output resolves physical eye-layer order independently'])
        report['admission']['profile']='eevee_joint_grid.eye.v1'
        report['admission']['lighting_scope']='fixed spatial position; opaque receivers and declared static unlit eye layers'
        report['limitations']=[s for s in report['limitations'] if s!='opaque mesh surfaces only']
        report['limitations'].append('only explicitly bound eye-layer transparency; general transparency remains rejected')
    if material_color_snapshot is not None:
        report['schema']='toon-joint-report.v5'
        report['profile']='eevee_joint_grid.material-ink.v1'
        report['admission']['profile']=report['profile']
    if material_uv_snapshot is not None:
        report['schema']='toon-joint-report.v6';report['profile']='eevee_joint_grid.uv-ink.v1'
        report['admission']['profile']=report['profile']
        report['line_uv_sources']=dict(before=material_uv_snapshot,frozen=material_uv_snapshot,data_exact=True)
    if look_receipt is not None:report['shot_look']=look_receipt
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    checkpoint('COMPLETE', report=str(root/'report.json'))
    return report
