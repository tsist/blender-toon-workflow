# SPDX-License-Identifier: GPL-3.0-or-later
"""Native finish parameters/Action and shader shape across RENDER freezing."""
import math
import re
from types import SimpleNamespace

from . import finish_core as finish, finish_nodes as nodes, finish_animation as animation, finish_band as band

SCHEMA='toon-finish-freeze.v1'
MASK_SCHEMA='toon-finish-freeze.v2'
COMPONENT_SCHEMA='toon-finish-freeze.components.v1'


def _observe(material, group, *, frozen=False, target=None):
    owner=group[finish._OWNER]
    tree=material.node_tree
    instance=finish._tagged(tree,finish._MARK,owner)
    source=finish._tagged(tree,finish._SOURCE,owner)
    output=finish._tagged(tree,finish._OUTPUT,owner)
    base=finish._effect_socket(instance,'base')
    result=finish._effect_socket(instance,'shader')
    surface=finish._socket(output.inputs,'Surface')
    if len(surface.links)==1 and surface.links[0].from_node.get('toon53_stage'):
        from .lighting_53 import original_surface_socket
        destination=original_surface_socket(surface)
    else:
        destination=surface
    original=finish._socket(source.outputs,group[finish._SOCKET])
    if (instance.node_tree!=group or instance.mute or not output.is_active_output
            or len(base.links)!=1 or base.links[0].from_socket!=original
            or len(result.links)!=1 or result.links[0].to_socket!=destination
            or len(destination.links)!=1 or destination.links[0].from_socket!=result):
        raise ValueError('Frozen finish passthrough footprint changed')
    result=dict(settings=nodes.parameters(group),shape_sha256=finish._digest(nodes.structure(group)),
        animation=animation.inspect(group),source=[source.name,source.bl_idname,original.identifier],
        output=[output.name,output.bl_idname,surface.identifier],instance=instance.name)
    if band.reference(group) is not None:
        result['band']=dict(band.validate(group),reference_id=band.reference_id(group))
    from . import finish_band_mask as mask
    if mask.present(group):
        reader=mask.frozen_source_descriptor if frozen else mask.descriptor
        result['band_mask']=reader(group,target)
    return result


def _render_time(depsgraph):
    if depsgraph.mode!='RENDER':
        raise ValueError('Finish evaluation requires the actual RENDER dependency graph')
    scene=depsgraph.scene_eval
    return dict(scene=scene.name,frame=scene.frame_current,subframe=scene.frame_subframe)


def _evaluated(row, depsgraph):
    """Read the Group reached through the actual evaluated material instance."""
    import bpy
    original=bpy.data.objects.get(row['target'])
    if original is None:raise ValueError('Finish render target is missing')
    evaluated=original.evaluated_get(depsgraph)
    settings=[];bands=[]
    for slot in row['slots']:
        material=evaluated.material_slots[slot['index']].material
        if material is None or material.original.name!=row['material']:
            raise ValueError('Evaluated finish material identity changed')
        instance=finish._tagged(material.node_tree,finish._MARK,row['owner'])
        group=instance.node_tree
        if group is None or group.original.name!=row['group'] or group.get(finish._OWNER)!=row['owner']:
            raise ValueError('Evaluated finish Group identity changed')
        # Blender may retain an original ID reference on the evaluated group
        # instance; explicitly request that ID's evaluation in this graph.
        settings.append(nodes.parameters(group.evaluated_get(depsgraph)))
        band_state=band.evaluate(group.original,depsgraph)
        if band_state is not None:band_state['reference_id']=band.reference_id(group.original)
        bands.append(band_state)
    if not settings or any(value!=settings[0] for value in settings) or any(value!=bands[0] for value in bands):
        raise ValueError('Finish evaluated slot parameters disagree')
    return settings[0],bands[0]


def capture(scene, objects, *, depsgraph):
    render_time=_render_time(depsgraph)
    if render_time!=dict(scene=scene.name,frame=scene.frame_current,subframe=scene.frame_subframe):
        raise ValueError('Finish source and RENDER graph times disagree')
    from . import component_witness
    objects=tuple(objects)
    components=component_witness.capture(scene,objects,depsgraph=depsgraph)
    rows=[];seen=set()
    for obj in sorted(objects,key=lambda item:item.name):
        used={p.material_index for p in obj.data.polygons}
        for index,slot in enumerate(obj.material_slots):
            material=slot.material
            if index not in used or material is None or material.as_pointer() in seen:
                continue
            seen.add(material.as_pointer())
            graph=material.node_tree
            marked=graph is not None and any(n.get(finish._MARK) is not None or
                (n.bl_idname=='ShaderNodeGroup' and n.node_tree is not None
                 and n.node_tree.get(nodes.PROFILE_KEY) is not None) for n in graph.nodes)
            if finish.KEY not in material:
                if marked and not component_witness.owns_game_finish(material,components,
                        finish_key=finish.KEY,mark_key=finish._MARK,profile_key=nodes.PROFILE_KEY):
                    raise ValueError('Finish instance has lost its explicit material binding')
                continue
            old=obj.active_material_index
            try:
                obj.active_material_index=index
                checked=finish.inspect(SimpleNamespace(scene=scene,active_object=obj,mode='OBJECT'))
                group=material[finish.KEY]
                before=_observe(material,group,target=obj)
                row=dict(profile=finish.PROFILE,target=obj.name,material=material.name,
                    group=group.name,owner=checked['owner'],slots=checked['slots'],before=before)
                row['render_before'],band_before=_evaluated(row,depsgraph)
                if band_before is not None:row['render_band_before']=band_before
                if before.get('band_mask') is not None:
                    row['render_band_mask_before']=_evaluated_mask(row,depsgraph)
                if row['render_before']!=before['settings']:
                    raise ValueError('Finish Main and RENDER evaluated parameters disagree')
                rows.append(row)
            finally:obj.active_material_index=old
    receipt=dict(schema=MASK_SCHEMA if any('band_mask' in row['before'] for row in rows) else SCHEMA,
        render_time=render_time,targets=rows)
    if components['targets']:
        receipt.update(schema=COMPONENT_SCHEMA,components=components)
    return receipt


def _evaluated_mask(row,depsgraph, *, frozen=False):
    import bpy
    from . import finish_band_mask as mask
    if depsgraph.mode!='RENDER':raise ValueError('Band mask requires actual RENDER evaluation')
    original=bpy.data.objects.get(row['target'])
    if original is None:raise ValueError('Band mask target is missing')
    evaluated=original.evaluated_get(depsgraph)
    mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
    try:
        values=[]
        for slot in row['slots']:
            material=evaluated.material_slots[slot['index']].material
            if material is None or material.original.name!=row['material']:
                raise ValueError('Evaluated band mask material changed')
            node=finish._tagged(material.node_tree,finish._MARK,row['owner'])
            group=node.node_tree
            if group is None or group.original.name!=row['group']:
                raise ValueError('Evaluated band mask Group changed')
            values.append(mask.evaluate(group.evaluated_get(depsgraph),original,mesh,frozen=frozen))
        if not values or any(v!=values[0] for v in values):
            raise ValueError('Band mask evaluated material slots disagree')
        return values[0]
    finally:evaluated.to_mesh_clear()


def verify_frozen(receipt):
    import bpy
    if receipt.get('schema')==COMPONENT_SCHEMA:
        from . import component_witness
        component_witness.verify_frozen(receipt['components'])
    for row in receipt['targets']:
        material=bpy.data.materials.get(row['material'])
        group=bpy.data.node_groups.get(row['group'])
        target=bpy.data.objects.get(row['target'])
        if (material is None or group is None or target is None or material.get(finish.KEY)!=group
                or group.get(finish._OWNER)!=row['owner']):
            raise ValueError('Frozen finish material/group identity changed')
        row['frozen']=_observe(material,group,frozen=True,target=target)
        row['data_exact']=row['before']==row['frozen']
    validate_receipt(receipt,require_render=False)


def verify_render(receipt, depsgraph):
    if _render_time(depsgraph)!=receipt['render_time']:
        raise ValueError('Frozen finish RENDER time changed')
    if receipt.get('schema')==COMPONENT_SCHEMA:
        from . import component_witness
        component_witness.verify_render(receipt['components'],depsgraph)
    for row in receipt['targets']:
        row['render_frozen'],band_frozen=_evaluated(row,depsgraph)
        if band_frozen is not None:row['render_band_frozen']=band_frozen
        if row['before'].get('band_mask') is not None:
            row['render_band_mask_frozen']=_evaluated_mask(row,depsgraph,frozen=True)
    validate_receipt(receipt)


def _mask_identity(source, row):
    if (type(source) is not dict or source.get('owner')!=row['owner']
            or type(source.get('target')) is not dict or source['target'].get('name')!=row['target']):
        raise ValueError('Band mask owner/target differs from its finish target')


def validate_receipt(receipt, *, require_render=True):
    if type(receipt) is not dict or receipt.get('schema') not in (SCHEMA,MASK_SCHEMA,COMPONENT_SCHEMA) or type(receipt.get('targets')) is not list:
        raise ValueError('Invalid finish freeze receipt')
    component_count=0
    if receipt['schema']==COMPONENT_SCHEMA:
        from . import component_witness
        components=receipt.get('components')
        component_count=component_witness.validate_receipt(components,require_render=require_render)
        if not component_count or components['render_time']!=receipt.get('render_time'):
            raise ValueError('Component finish profile requires nonempty same-time component evidence')
    elif 'components' in receipt:
        raise ValueError('Legacy finish profile cannot hide component evidence')
    stamp=receipt.get('render_time',{})
    if (type(stamp.get('scene')) is not str or not stamp['scene'] or type(stamp.get('frame')) is not int
            or type(stamp.get('subframe')) not in (int,float) or not math.isfinite(stamp['subframe'])):
        raise ValueError('Invalid finish RENDER time witness')
    seen=set();mask_count=0
    for row in receipt['targets']:
        key=(row.get('target'),row.get('material'))
        if key in seen or row.get('profile')!=finish.PROFILE:
            raise ValueError('Duplicate or unsupported finish freeze target')
        seen.add(key)
        if (type(row.get('owner')) is not str or re.fullmatch('[0-9a-f]{32}',row['owner']) is None
                or row.get('data_exact') is not True or row.get('before')!=row.get('frozen')):
            raise ValueError('Finish parameters/Action/shape did not freeze exactly')
        data=row['before']
        if 'band_mask' in data:
            from . import finish_band_mask as mask, sdf_witness
            if receipt['schema'] not in (MASK_SCHEMA,COMPONENT_SCHEMA):raise ValueError('Masked finish requires freeze.v2')
            source=mask.validate_receipt_payload(data['band_mask'])
            _mask_identity(source,row)
            actual=row.get('render_band_mask_before')
            if type(actual) is not dict or set(actual)!={'source','evaluated_uv'} or actual['source']!=source:
                raise ValueError('Band mask actual RENDER source differs')
            sdf_witness._uv(actual['evaluated_uv'],source['uv']['uv_name'])
            if 'render_band_mask_frozen' in row:
                frozen_actual=row['render_band_mask_frozen']
                _mask_identity(frozen_actual.get('source') if type(frozen_actual) is dict else None,row)
            if require_render and row.get('render_band_mask_frozen')!=actual:
                raise ValueError('Band mask image/UV changed between actual RENDER graphs')
            mask_count+=1
        elif any(key in row for key in ('render_band_mask_before','render_band_mask_frozen')):
            raise ValueError('Band mask witness has no declared source')
        nodes.normalize(data['settings'])
        if (row.get('render_before')!=data['settings'] or
                (require_render and row.get('render_frozen')!=row['render_before'])):
            raise ValueError('Finish parameters were not observed identically in both RENDER graphs')
        if data.get('band') is not None:
            state=row.get('render_band_before',{})
            if (state.get('reference')!=data['band'].get('reference') or
                    state.get('settings')!=data['band'].get('settings') or
                    state.get('reference_id')!=data['band'].get('reference_id')):
                raise ValueError('Finish band RENDER source differs from its declared graph')
            band.normalize(state['settings'])
            matrix=state.get('matrix_world')
            if (type(matrix) is not list or len(matrix)!=4 or any(type(line) is not list or len(line)!=4
                    or any(type(value) not in (int,float) or not math.isfinite(value) for value in line) for line in matrix)):
                raise ValueError('Invalid finish band RENDER reference matrix')
            if require_render and row.get('render_band_frozen')!=state:
                raise ValueError('Finish band reference changed between actual RENDER graphs')
        if (re.fullmatch('[0-9a-f]{64}',str(data.get('shape_sha256'))) is None
                or data.get('animation',{}).get('status')!='valid'):
            raise ValueError('Invalid finish shape/animation witness')
    if receipt['schema']==MASK_SCHEMA and not mask_count:
        raise ValueError('freeze.v2 requires an explicit band mask target')
    return len(seen)+component_count
