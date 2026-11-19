# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved-source 2D domain worker; the public supervisor owns this process."""
import hashlib
import json
from pathlib import Path

from .workflow_contract import SCHEMA, STATE, fingerprint
from .backend import pencil


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _ref(path):
    return dict(file=str(path),expected_sha256=_digest(path))


def _write(path,value):
    from .joint_sequence import _write_new
    _write_new(path,value)


def admit_legacy(scene):
    """Never reinterpret, delete or hide the frozen geometry backend's data."""
    import bpy
    if (scene.get('_toon_state') is not None
            or any(collection.get('_toon_state') is not None for collection in bpy.data.collections)
            or any(
            obj.get('_toon_generated_outline') is True for obj in bpy.data.objects)):
        raise ValueError('Legacy geometry toon state/carriers require an explicit separate migration; no automatic conversion')


def runtime_paths():
    """Native runtime paths, excluding weak append provenance; shared by GUI/CLI."""
    import bpy
    if bpy.data.libraries:
        raise ValueError('Linked libraries require a separate qualified dependency adapter')
    for image in bpy.data.images:
        if image.source not in {'FILE','GENERATED','VIEWER'} and not image.packed_file:
            raise ValueError('Sequence/UDIM/movie image dependencies await a dedicated adapter')
        if image.source=='FILE' and not image.packed_file and not image.filepath:
            raise ValueError('Unpacked file image has no runtime path: '+image.name)
    # blend_paths also enumerates weak append provenance. These are not linked
    # libraries or required runtime resources; actual image paths stay required.
    weak=set();weak_raw=set()
    def raw_identity(value):
        return str(value).replace('\\','/').casefold()
    blocks=list(bpy.data.user_map())
    for block in blocks:
        provenance=getattr(block,'library_weak_reference',None)
        if provenance and provenance.filepath:
            weak_raw.add(raw_identity(provenance.filepath))
            weak.add(str(Path(bpy.path.abspath(provenance.filepath)).resolve()).casefold())
    images={str(Path(bpy.path.abspath(image.filepath,library=image.library)).resolve()).casefold()
            for image in bpy.data.images if image.source=='FILE' and not image.packed_file and image.filepath}
    # Native Windows expansion of historical POSIX paths may choose a different
    # drive than pathlib. Match Blender's raw and absolute enumeration instead
    # of guessing a drive for an append-only provenance record. Both calls run
    # on Blender's main thread without intervening scene mutations.
    raw=list(bpy.utils.blend_paths(absolute=False,packed=False,local=False))
    absolute=list(bpy.utils.blend_paths(absolute=True,packed=False,local=False))
    if len(raw)!=len(absolute):
        raise ValueError('Native dependency inventory changed during classification')
    strong_raw=set()
    for block in blocks:
        if getattr(block,'packed_file',None):continue
        for prop in block.bl_rna.properties:
            if prop.type=='STRING' and getattr(prop,'subtype',None)=='FILE_PATH':
                value=getattr(block,prop.identifier,'')
                if value and value!='<builtin>':strong_raw.add(raw_identity(value))
    for original,value in zip(raw,absolute):
        if original and value and raw_identity(original) in weak_raw:
            weak.add(str(Path(value).resolve()).casefold())
        if original and value and raw_identity(original) in strong_raw:
            images.add(str(Path(value).resolve()).casefold())
    paths=set()
    for original,value in zip(raw,absolute):
        if not value or value=='<builtin>':continue
        path=Path(value)
        identity=str(path.resolve()).casefold()
        if identity in weak and identity not in images:continue
        paths.add(path)
    for image in bpy.data.images:
        if image.source=='FILE' and not image.packed_file and image.filepath:
            paths.add(Path(bpy.path.abspath(image.filepath,library=image.library)))
    return paths


def _resources(params):
    declared={str(Path(row['file']).resolve()).casefold() for row in params['resources']}
    for path in runtime_paths():
        identity=str(path.resolve()).casefold()
        if not path.is_file() or identity not in declared:
            raise ValueError('Missing or undeclared source dependency: '+str(path))


def _open(params):
    import bpy
    pencil_module = pencil()
    pencil_module.register()
    # Restore saved SDF picker RNA before image-consumer validation. Blender
    # keeps this pointer hidden when only the domain worker is imported.
    from . import sdf_ui
    sdf_ui.register()
    from . import surface_ui,cel_ui,line_template_ui,style_family_ui
    surface_ui.register();cel_ui.register();line_template_ui.register();style_family_ui.register()
    from . import component_ui,style_set_ui,shot_look_ui,media_render_ui
    component_ui.register();style_set_ui.register();shot_look_ui.register();media_render_ui.register()
    if _digest(params['file'])!=params['expected_sha256']:
        raise ValueError('Author source fingerprint differs')
    if bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)!={'FINISHED'}:
        raise RuntimeError('Source did not open')
    spec=params['manifest'];scene=bpy.data.scenes[spec['scene']];layer=scene.view_layers[spec['view_layer']]
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    if style_family_ui.settings(bpy.context).receipt:
        style_family_ui.inspect_current(bpy.context)
    try:
        admit_legacy(scene);_resources(params)
    except ValueError as error:
        from protocol import Failure
        raise Failure('UNSUPPORTED',str(error)) from error
    from .joint_render import _admit_drivers
    _admit_drivers()
    curve_editor = pencil('curve_editor')
    if scene.toon_viewport_preview.curve_tree is not None:
        curve_editor.synchronize(scene)
        if scene.toon_viewport_preview.curve_error:
            raise ValueError('Pending author curves are invalid or conflict; repair or discard them first')
    return scene,layer


def capture_author(scene):
    file_authoring = pencil('file_authoring')
    settings=scene.toon_viewport_preview
    basic=('silhouette','open_edges','marked_edges','wireframe_edges','material_edges',
           'crease_edges','crease_angle','visibility','coverage_profile','feather_px',
           'samples_per_axis','post_filter_sigma')
    values={name:getattr(settings,name) for name in basic if hasattr(settings,name)}
    value=dict(schema='toon-workflow-author.v1',mode=settings.style_mode,basic=values,
        controls=file_authoring.capture_controls(scene),
        document=file_authoring.read_scene(scene) if settings.preset_text else None,
        persistence='full animations and author graphs stay in the blend; static portable style is separate')
    from . import component_slots
    components=component_slots.snapshot(scene)
    if components:
        value['schema']='toon-workflow-author.components.v1'
        value['components']=components
    from . import style_set_ui,shot_look_ui
    template=style_set_ui.snapshot(scene);look=shot_look_ui.snapshot(scene)
    if template is not None or look is not None:
        value['schema']='toon-workflow-author.extensions.v1'
        if template is not None:value['style_set']=template
        if look is not None:value['shot_look']=look
    from . import media_render_ui
    media=media_render_ui.snapshot(scene)
    if media is not None:
        value['schema']='toon-workflow-author.extensions.v2'
        value['native_media']=media
    return value


def non_author_signature(scene):
    """Edit policy: native objects/mesh topology/material bindings stay intact."""
    import numpy as np
    digest=hashlib.sha256()
    for obj in sorted(scene.objects,key=lambda o:o.name):
        digest.update(json.dumps([obj.name,obj.type,obj.parent.name if obj.parent else None,
            [list(r) for r in obj.matrix_world],[(s.link,s.material.name if s.material else None)
            for s in obj.material_slots]],sort_keys=True).encode('utf8'))
        if obj.type=='MESH':
            for group,prop,dim,dtype in ((obj.data.vertices,'co',3,np.float32),
                    (obj.data.edges,'vertices',2,np.int32),(obj.data.loops,'vertex_index',1,np.int32),
                    (obj.data.polygons,'material_index',1,np.int32)):
                buffer=np.empty(len(group)*dim,dtype);group.foreach_get(prop,buffer);digest.update(buffer.tobytes())
    return digest.hexdigest()


def _inventory_outputs(root):
    return [_ref(path) for path in sorted(root.rglob('*')) if path.is_file()
            and path.name not in {'worker-result.json','toon-workflow-report.json','toon-workflow-state.json'}
            and ('sequence-output' in path.parts or path.name in {'toon-author.json',
                'toon-author-after.json','toon-author-style.json','toon-candidate.blend','toon-workflow-request.json'})]


def validate_completed_state(path, expected_params=None):
    """Bind a historical completed state to its own immutable run and bundle.

    Source/resources need not still exist for output inspection. Their recorded
    identities, including the historical implementation, must agree throughout.
    Public callers separately hold input/output FileGuards and reject reparse paths.
    """
    from .workflow_contract import normalize, descriptor, manifest_time_policy, LEGACY_SCHEMA
    from .joint_sequence import _read, normalize_times, inspect_sequence
    from .joint_time import profile_for_policy, validate_settings_times, NATIVE_TIME_POLICY
    path=Path(path).resolve(strict=True)
    state=_read(path)
    keys={'schema','status','source','implementation','manifest','resources',
          'author_sha256','outputs','sequence_root','recovery_request','old_geometry_migration'}
    if (type(state) is not dict or set(state)!=keys or state.get('schema')!=STATE
            or state.get('status')!='completed' or state['old_geometry_migration'] is not False):
        raise ValueError('Expected a completed explicit 2D workflow state')
    source=descriptor(state['source'])
    params=normalize('run',dict(**source,manifest=state['manifest'],
        resources=state['resources'],implementation=state['implementation']))
    if expected_params is not None and fingerprint(params)!=fingerprint(expected_params):
        raise ValueError('Completed state parameters differ from the submitted run')
    root=path.parent/'sequence-output'
    if Path(state['sequence_root']).resolve()!=root:
        raise ValueError('State sequence root differs')
    if type(state['outputs']) is not list or not state['outputs']:
        raise ValueError('State output inventory is empty')
    for row in state['outputs']:
        descriptor(row)
        member=Path(row['file']).resolve(strict=True)
        if not member.is_relative_to(path.parent):
            raise ValueError('State output escaped its bundle')
    if fingerprint(state['outputs'])!=fingerprint(_inventory_outputs(path.parent)):
        raise ValueError('State output closure is incomplete or changed')
    recovery=descriptor(state['recovery_request'])
    request_path=path.parent/'toon-workflow-request.json'
    if Path(recovery['file']).resolve()!=request_path or recovery['expected_sha256']!=_digest(request_path):
        raise ValueError('State recovery request binding differs')
    request=_read(request_path)
    if (set(request)!={'schema','params'} or request['schema']!='toon-workflow-request.v1'
            or fingerprint(normalize('run',request['params']))!=fingerprint(params)):
        raise ValueError('State parameters differ from its persisted recovery request')
    author=_read(path.parent/'toon-author.json')
    if (author.get('schema')!='toon-workflow-author-snapshot.v1'
            or author.get('source')!=source or author.get('author_sha256')!=state['author_sha256']
            or fingerprint(author['author'])!=state['author_sha256']):
        raise ValueError('State author fingerprint differs from its saved snapshot')
    spec=params['manifest']
    policy=manifest_time_policy(spec)
    expected=dict(source=dict(file=str(Path(source['file']).resolve()),sha256=source['expected_sha256']),
        scene=spec['scene'],view_layer=spec['view_layer'],
        times=normalize_times(spec['times'],time_policy=policy),
        samples_per_axis=spec['samples_per_axis'],memory_budget_bytes=spec['memory_budget_bytes'],
        render_options=dict(lighting_samples=spec['lighting_samples']),
        implementation=params['implementation'],resource_bindings=params['resources'],
        profile=profile_for_policy(policy),time_policy=policy)
    if policy==NATIVE_TIME_POLICY:
        expected['requested_times']=spec['times']
    actual=_read(root/'request.json')
    allow_legacy_missing=expected_params is None and spec['schema']==LEGACY_SCHEMA
    if validate_settings_times(actual,allow_legacy_missing=allow_legacy_missing)!=policy:
        raise ValueError('State manifest and sequence time policy differ')
    observed={key:actual.get(key) for key in expected}
    if allow_legacy_missing and 'time_policy' not in actual:
        observed['time_policy']=policy
    # Early 0.13 development receipts omitted only the empty resource list.
    # Keep their read-only identity; new run/recover outputs must record it.
    if expected_params is None and 'resource_bindings' not in actual and not params['resources']:
        observed['resource_bindings']=[]
    if fingerprint(observed)!=fingerprint(expected):
        raise ValueError('State settings/source/resources/implementation differ from the sequence')
    return state,inspect_sequence(root)


def execute(operation,params,job):
    import bpy
    from toon_workflow_public import effective_params
    from . import joint_sequence
    job=Path(job).resolve()
    original=params
    params=effective_params('toon-workflow.'+operation,params)
    source=None if 'state' in params else {k:params[k] for k in ('file','expected_sha256')}
    summary={}
    if operation=='inspect' and 'state' in params:
        state,cold=validate_completed_state(params['state']['file'])
        summary=dict(state=params['state'],inspection=cold)
    else:
        scene,layer=_open(params)
        before=capture_author(scene)
        author=dict(schema='toon-workflow-author-snapshot.v1',source=source,
            author=before,author_sha256=fingerprint(before))
        _write(job/'toon-author.json',author)
        if operation=='edit':
            if author['author_sha256']!=params['expected_author_sha256']:
                from protocol import Failure
                raise Failure('CONFLICT','Author parameters changed since the explicit edit baseline')
            preserve=non_author_signature(scene)
            from . import joint_basic_style
            style = joint_basic_style.read(params['style']['file'])
            if type(style) is dict and style.get('schema') == joint_basic_style.SCHEMA:
                _write(job/'toon-author-style.json',joint_basic_style.apply(scene,style))
            else:
                file_authoring = pencil('file_authoring')
                file_authoring.import_style(scene,params['style']['file'],mode='REPLACE')
                file_authoring.export_style(scene,job/'toon-author-style.json')
            after=capture_author(scene)
            if non_author_signature(scene)!=preserve:
                raise ValueError('Author edit unexpectedly changed non-author geometry/transforms/bindings')
            _write(job/'toon-author-after.json',dict(schema='toon-workflow-author-snapshot.v1',
                author=after,author_sha256=fingerprint(after),non_author_unchanged=True))
            candidate=job/'toon-candidate.blend'
            if bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,
                                          check_existing=False)!={'FINISHED'}:
                raise RuntimeError('Editable author candidate did not save')
            summary=dict(candidate=str(candidate),candidate_sha256=_digest(candidate),
                style=_ref(job/'toon-author-style.json'),baseline_author_sha256=author['author_sha256'],
                author_sha256=fingerprint(after),non_author_unchanged=True,
                output=str(candidate),image_generated=False)
        elif operation in {'run','recover'}:
            _write(job/'toon-workflow-request.json',dict(schema='toon-workflow-request.v1',params=params))
            spec=params['manifest']
            from .workflow_contract import manifest_time_policy
            previous=Path(original['recovery']['file']).parent/'sequence-output' if operation=='recover' else None
            sequence=joint_sequence.render_sequence(Path(params['file']),spec['scene'],spec['view_layer'],
                job/'sequence-output',spec['times'],samples_per_axis=spec['samples_per_axis'],
                memory_budget_bytes=spec['memory_budget_bytes'],
                render_options=dict(lighting_samples=spec['lighting_samples']),
                implementation=params['implementation'],expected_source_sha256=params['expected_sha256'],
                reuse_from=previous,resource_bindings=params['resources'],
                time_policy=manifest_time_policy(spec))
            inspection=joint_sequence.inspect_sequence(job/'sequence-output')
            outputs=_inventory_outputs(job)
            _write(job/'toon-workflow-state.json',dict(schema=STATE,status='completed',source=source,
                implementation=params['implementation'],manifest=spec,resources=params['resources'],
                author_sha256=author['author_sha256'],outputs=outputs,
                sequence_root=str(job/'sequence-output'),recovery_request=_ref(job/'toon-workflow-request.json'),
                old_geometry_migration=False))
            summary=dict(state=_ref(job/'toon-workflow-state.json'),inspection=inspection,
                reused_frames=sequence.get('reused_frames',0),
                output=str(job/'sequence-output'/('frame-%03d'%(sequence['frame_count']-1))/'joint-output/Combined.png'))
        else:
            summary=dict(author=_ref(job/'toon-author.json'),author_sha256=author['author_sha256'],
                         source_saved=False,legacy_geometry_policy='reject; no migration')
    if source and _digest(source['file'])!=source['expected_sha256']:
        raise ValueError('Author source changed during the domain operation')
    report=dict(schema='toon-workflow-report.v1',status='passed',operation=operation,
        source=source,implementation=original['implementation'],result=summary,
        source_text_executed=False,artistic_approval=False)
    _write(job/'toon-workflow-report.json',report)
    outputs=_inventory_outputs(job)+[_ref(job/'toon-workflow-report.json')]
    if (job/'toon-workflow-state.json').exists():outputs.append(_ref(job/'toon-workflow-state.json'))
    return dict(status='passed',report=str(job/'toon-workflow-report.json'),outputs=outputs,**summary)
