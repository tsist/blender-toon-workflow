# SPDX-License-Identifier: GPL-3.0-or-later
"""Use the stable Pencil author/raster core for one complete outer sample.

No edits to the independently delivered 0.2.27 package are required. Its
per-layer spatial averaging is replaced explicitly by the outer joint grid;
authoring, chain computation, priorities, brush and visibility remain there.
"""

from .backend import pencil


def raster_profiles(plan):
    """Mother-owned coverage overrides; the embedded Pencil authors stay intact.

    Width maps and original size/curve data can have different coverage units
    from another Line in the same scene. The complete request is persisted in
    the existing author Text and therefore participates in its frozen identity.
    """
    import math
    payload = plan['raw']['request'].get('toon_line_raster_profiles') if plan else None
    if payload is None:
        return {}
    if (type(payload) is not dict or set(payload) != {'schema', 'profiles'}
            or payload['schema'] != 'toon.line-raster-profiles.v1'
            or type(payload['profiles']) is not list or not 1 <= len(payload['profiles']) <= 32):
        raise ValueError('Invalid Toon Line raster profiles')
    known = {g['id']: g for g in plan['groups']}
    result = {}
    for row in payload['profiles']:
        if type(row) is not dict or set(row) != {'groups', 'coverage_profile', 'feather_px', 'post_filter_sigma'}:
            raise ValueError('Invalid Toon Line raster profile fields')
        if (type(row['groups']) is not list or not row['groups'] or len(row['groups']) > 32
                or row['coverage_profile'] not in ('round_capsule.v1', 'extent_falloff.v1', 'pixel_box.v1')):
            raise ValueError('Invalid Toon Line raster profile groups or coverage')
        for field, high in (('feather_px', 16), ('post_filter_sigma', 4)):
            value = row[field]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= high:
                raise ValueError('Invalid Toon Line raster profile '+field)
        if row['coverage_profile'] == 'pixel_box.v1':
            from .pixel_coverage import footprint
            footprint(row['feather_px'])
        for gid in row['groups']:
            if type(gid) is not str or gid not in known or gid in result:
                raise ValueError('Unknown or duplicate Toon Line raster profile group')
            result[gid] = {k: row[k] for k in ('coverage_profile', 'feather_px', 'post_filter_sigma')}
    # The backend resolves and filters once per Line/channel. A profile may not
    # silently change the filter of another group sharing that Line.
    lines = {}
    for gid, group in known.items():
        lines.setdefault(group.get('line_id'), []).append(gid)
    for gids in lines.values():
        if any(gid in result for gid in gids):
            if any(gid not in result for gid in gids) or len({result[gid]['post_filter_sigma'] for gid in gids}) != 1:
                raise ValueError('Raster profile must cover a complete Line with one filter')
    pixel_groups = {gid for gid, row in result.items() if row['coverage_profile'] == 'pixel_box.v1'}
    if pixel_groups:
        from .pixel_coverage import validate_brushes
        validate_brushes(plan, pixel_groups)
    return result


def draw(geometry, frame, scene, evaluated_scene, progress):
    """Compatibility entry returning only the combined line image and metadata."""
    bundle = draw_bundle(geometry, frame, scene, evaluated_scene, progress)
    return bundle['pixels'], bundle['style']


def draw_bundle(geometry, frame, scene, evaluated_scene, progress):
    """Draw one full-frame common spatial position, including authored elements.

    Spatial averaging belongs to the caller's joint grid. Layer feathering and
    post-filtering retain their authored values; this is not native Pencil AA
    equivalence. Arrays are detached premultiplied scene-linear RGBA. DEPTH
    elements use the stable axial-source RGBA encoding, not metric GPU depth.
    No crop is performed here.
    """
    import json
    import numpy as np
    addon, depth_output, parameter_animation, render_elements = (
        pencil(name) for name in ('addon', 'depth_output', 'parameter_animation', 'render_elements'))
    project_segments = pencil('geometry').project_segments
    Renderer = pencil('drawing').Renderer
    build_layers_steps = pencil('strokes').build_layers_steps

    settings = scene.toon_viewport_preview
    from . import hair_fixed_shadow
    hair_snapshot=hair_fixed_shadow.render_snapshot(scene,geometry)
    from . import eye_overlays
    eye_rows = eye_overlays.collect(scene)
    eye_snapshot = None
    if eye_rows:
        from . import eye_overlay_lines
        geometry, eye_snapshot = eye_overlay_lines.prepare(geometry,
            [{k:row[k] for k in ('target','material','slot','kind','settings')} for row in eye_rows])
        build_layers_steps=eye_overlay_lines.layer_builder(eye_snapshot)
    w, h = frame['width'], frame['height']
    plan = (addon.effective_preset(scene, evaluated_scene=evaluated_scene)
            if settings.style_mode == 'PRESET' else None)
    profiles = raster_profiles(plan)
    from . import line_material_colors
    material_colors = line_material_colors.resolve(scene, plan)
    declarations = addon.element_configuration(settings)
    element_config = (render_elements.normalize(declarations, style_mode=settings.style_mode,
        group_ids=[group['id'] for group in plan['groups']] if plan is not None else ['__BASIC__'])
        if declarations else None)
    style = None
    if plan is None and (element_config is not None or eye_rows):
        layers = render_elements.basic_layers(geometry, frame, settings)
    elif plan is None:
        segments, supports = geometry.segment_records(frame['view'], frame['perspective'], settings)
        projected, indices, u = project_segments(segments, frame['mvp'], w, h, with_parameters=True)
        world = segments[indices, 0, None, :] + u[:, :, None] * (segments[indices, 1]-segments[indices, 0])[:, None, :]
        layers = [dict(projected=projected, world=world, supports=supports[indices],
                       widths=np.full((len(projected), 2), settings.line_width, np.float32),
                       alphas=np.full((len(projected), 2), settings.opacity, np.float32),
                       color=tuple(settings.line_color), visibility=settings.visibility,
                       coverage_profile=getattr(settings, 'coverage_profile', 'round_capsule.v1'),
                       feather_px=getattr(settings, 'feather_px', 1.),
                       samples_per_axis=getattr(settings, 'samples_per_axis', 1),
                       post_filter_sigma=getattr(settings, 'post_filter_sigma', 0.))]
    else:
        maps = addon._map_amounts(scene, plan, evaluated_scene=evaluated_scene)
        dashes = addon._dash_patterns(scene, plan)
        pixel_groups={gid for gid,row in profiles.items() if row['coverage_profile']=='pixel_box.v1'}
        if pixel_groups:
            from .pixel_coverage import validate_dashes
            validate_dashes(plan,pixel_groups,dashes)
        chain_phase = None
        inputs = addon._chain_inputs(scene, plan)
        if inputs is not None:
            chain_snapshot, chain_compute = pencil('chain_snapshot'), pencil('chain_compute')
            bindings, controls = inputs
            if settings.intersection_enabled and any(r['intersection_enabled'] for r in controls['brushes'].values()):
                _plan = pencil('intersection_strokes')._plan
                if _plan(plan) is not None:
                    capture_steps = pencil('intersection_sources').capture_steps
                    geometry.intersection_sources = _consume(capture_steps(geometry.meshes), progress)
            payload = _consume(chain_snapshot.snapshot_steps(geometry, plan, frame['view'], frame['mvp'],
                frame['perspective'], w, h, 1, settings, bindings, controls), progress)
            if addon.dash_controls.needs_metric(dashes):
                payload['include_dash_metric'] = True
            chain_phase = chain_compute.compute_snapshot(payload, lambda: progress('CHAIN_PHASE'))
        layers, style = _consume(build_layers_steps(geometry, plan, frame['view'], frame['mvp'],
            frame['perspective'], w, h, 1, settings, chain_phase=chain_phase,
            map_amounts=maps, dash_patterns=dashes,
            include_provenance=element_config is not None or bool(eye_rows) or bool(profiles) or material_colors is not None), progress)
    if profiles:
        layers = [dict(layer, **profiles[layer['group_id']])
                  if layer['group_id'] in profiles else layer for layer in layers]
    layers, material_color_report = line_material_colors.apply(geometry, layers, material_colors)
    author_layers = [{key: layer[key] for key in ('line_id', 'group_id', 'actual_brush',
        'samples_per_axis', 'coverage_profile', 'feather_px', 'post_filter_sigma') if key in layer}
        for layer in layers]
    layers = [dict(layer, samples_per_axis=1) for layer in layers]
    metadata = dict(original_style_report=style,
        line_material_colors=material_color_report,
        raster_profiles=dict(schema='toon.line-raster-profiles.v1', groups=profiles,
            sampling='outer joint grid; native Pencil AA equivalence not claimed'),
        parameter_animation=parameter_animation.receipt(scene, evaluated_scene=evaluated_scene),
        element_declarations=declarations,
        author_sampling=dict(settings={key: getattr(settings, key) for key in (
            'samples_per_axis', 'coverage_profile', 'feather_px', 'post_filter_sigma')
            if hasattr(settings, key)}, layers=author_layers),
        spatial_sampling_policy=dict(profile='joint-outer-grid-layer-sample.v1',
            layer_samples_per_axis=1, spatial_resolve_owner='joint outer grid',
            authored_feather_and_post_filter='preserved', native_pencil_aa_equivalent=False),
        output=dict(extent=[w, h], cropped=False, alpha_mode='PREMUL',
            color_space='scene_linear_Rec709', depth_profile=depth_output.PROFILE
            if element_config is not None and any(row['type'] == 'DEPTH'
                for row in element_config['elements']) else None))
    # The shared reports are JSON metadata. Preserve numpy scalars/arrays if a
    # shared reporter returns one; reject non-finite or opaque values explicitly.
    def json_value(value):
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
        raise TypeError('Non-JSON line metadata: ' + type(value).__name__)
    metadata = json.loads(json.dumps(metadata, allow_nan=False, default=json_value))
    pixel_profile = any(row['coverage_profile'] == 'pixel_box.v1' for row in profiles.values())
    if pixel_profile:
        from .pixel_coverage import renderer_class, verify_renderer
        Renderer = renderer_class(Renderer)
    ink_active = material_colors is not None and any(r['policy']['amount']>0 for r in material_colors['policies'])
    if ink_active:
        uv_runtime=next((row['_toon_material_uv_runtime'] for row in layers if '_toon_material_uv_runtime' in row),None)
        Renderer = line_material_colors.renderer_class(Renderer,uv_runtime=uv_runtime)
    renderer = eye_overlay_lines.renderer(eye_snapshot, base_renderer=Renderer) if eye_rows else Renderer()
    if pixel_profile:
        verify_renderer(renderer)
    if ink_active:
        line_material_colors.verify_renderer(renderer)
    try:
        projected = np.empty((0, 2, 3), np.float32)
        supports = np.empty((0, 2), np.float32)
        combined = render_elements.combined_layers(layers) if element_config is not None else layers
        _consume(renderer.render_steps(geometry, frame['mvp'], projected,
            settings, w, h, 1, supports, layers=combined), progress)
        pixels = np.array(renderer.color.read(), np.float32).reshape(h, w, 4)
        elements = {}
        if element_config is not None:
            for index, element in enumerate(element_config['elements']):
                progress('ELEMENT', element_id=element['id'], index=index,
                         total=len(element_config['elements']))
                selected = render_elements.select_layers(layers, element)
                _consume(renderer.render_steps(geometry, frame['mvp'], projected,
                    settings, w, h, 1, supports, layers=selected,
                    depth=depth_output.parameters(element, frame)), progress)
                value = np.array(renderer.color.read(), np.float32).reshape(h, w, 4)
                elements[element['pass_name']] = render_elements.with_background(
                    value, element['background']).copy()
        return dict(pixels=pixels, style=metadata, elements=elements, element_config=element_config,
                    hair_snapshot=hair_snapshot,
                    eye_snapshot=None if eye_snapshot is None else dict(
                        schema=eye_snapshot['schema'],sha256=eye_snapshot['sha256'],
                        triangles=len(eye_snapshot['triangles']),materials=len(eye_snapshot['materials'])))
    finally:
        # Stable Renderer has no close/free API; releasing this owner mirrors
        # CameraRun.close and lets its Blender GPU objects release normally.
        renderer = None


def _consume(iterator, progress):
    while True:
        try:
            value = next(iterator)
            progress('LINE', detail=value)
        except StopIteration as done:
            return done.value
