# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, reversible EEVEE surface authoring for one existing used slot.

No legacy run/state/outline entry is invoked. Portable material libraries and
Pencil style documents are separate formats. Face/normal bindings are protected.
"""
import hashlib
import json
from pathlib import Path
import uuid

from . import materials, templates

PROFILE = 'toon-surface-slot.v1'
_PROFILE = 'toon_surface_slot_profile'
_OWNER = 'toon_surface_slot_owner'
_TARGET = 'toon_surface_slot_target'
_SLOT = 'toon_surface_slot_index'
_LINK = 'toon_surface_slot_link'
_MESH = 'toon_surface_slot_mesh_name'
_ORIGINAL = 'toon_surface_slot_original'
_DATA = 'toon_surface_slot_original_data'
_HAD = 'toon_surface_slot_had_original'
_HAD_DATA = 'toon_surface_slot_had_original_data'
_CREATED = 'toon_surface_slot_created_snapshot'
_BASELINE = 'toon_surface_slot_baseline'
_DECLARED = 'toon_surface_slot_declared'
_SIGNATURE = 'toon_surface_slot_signature'


class SurfaceError(ValueError):
    """Invalid slot, protected face binding or changed owned surface."""


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def candidate_settings(name):
    """Return a fresh limited surface candidate, never a complete style family."""
    if name=='CEL3':return dict(mode='eevee_cel3',**{k:list(v) if isinstance(v,list) else v for k,v in materials.CEL3_DEFAULTS.items()})
    if name=='GAME_REFLECT':return dict(mode='eevee_cel3_reflect',**{k:list(v) if isinstance(v,list) else v for k,v in materials.CEL3_REFLECT_DEFAULTS.items()})
    if name=='INK_FLAT':
        return dict(mode='eevee_cel3',base_color=[1.,1.,1.,1.],shadow_color=[.82,.82,.82,1.],mid_color=[.82,.82,.82,1.],lit_color=[.82,.82,.82,1.],threshold_low=.18,threshold_high=.5)
    raise SurfaceError('Unknown surface candidate')


def _context(context,*,eevee=False):
    import bpy
    target=getattr(context,'active_object',None)
    if (not isinstance(target,bpy.types.Object) or target.type!='MESH' or target not in list(bpy.data.objects)
            or target.library or target.override_library or target.data.library or target.data.override_library
            or not target.is_editable or not target.data.is_editable or target.mode!='OBJECT' or target.data.is_editmode
            or getattr(context,'mode','OBJECT')!='OBJECT'):
        raise SurfaceError('Use an existing local editable Mesh in Object Mode')
    if eevee and context.scene.render.engine!='BLENDER_EEVEE':raise SurfaceError('Surface application/update requires EEVEE')
    index=target.active_material_index
    if type(index) is not int or not 0<=index<len(target.material_slots):raise SurfaceError('Choose one existing active material slot')
    if not any(p.material_index==index for p in target.data.polygons):raise SurfaceError('Active slot must actually be used by source polygons')
    slot=target.material_slots[index]
    if slot.link not in {'DATA','OBJECT'}:raise SurfaceError('Unknown material slot link mode')
    if slot.link=='DATA' and target.data.users!=1:raise SurfaceError('DATA slot requires an exclusive source Mesh; isolate it explicitly first')
    return target,index,slot


def _protected(material):
    """Bounded native group traversal; names alone never identify a binding."""
    if material is None:return
    prefixes=('toon_face_sdf_','toon_sdf_cast_','toon_face_threshold_','toon_face_material_',
              'toon_artistic_normal_','toon_art_normal_ui_','toon_hm_')
    pending=[material];seen=set();nodes=0
    while pending:
        block=pending.pop();pointer=block.as_pointer()
        if pointer in seen:continue
        seen.add(pointer)
        if len(seen)>66:raise SurfaceError('Material graph exceeds bounded face/normal/HM protection traversal')
        if any(type(key) is str and key.startswith(prefixes) for key in block.keys()):
            raise SurfaceError('Active material contains a protected face/SDF/cast/normal/HM reader or identity marker')
        tree=block.node_tree if hasattr(block,'node_tree') else block if hasattr(block,'nodes') else None
        if tree is None:continue
        if tree!=block:pending.append(tree);continue
        nodes+=len(tree.nodes)
        if nodes>2048:raise SurfaceError('Material graph exceeds bounded node protection traversal')
        for node in tree.nodes:
            if any(type(key) is str and key.startswith(prefixes) for key in node.keys()):
                raise SurfaceError('Active material contains protected face/SDF/cast/normal/HM node markers')
            if node.bl_idname=='ShaderNodeGroup' and node.node_tree is not None:pending.append(node.node_tree)


def _identity(value):
    if value is None:return None
    return [value.bl_rna.identifier,value.name_full,getattr(getattr(value,'library',None),'filepath',None)]


def _signature(material):
    payload={key:material.get(key) for key in (_PROFILE,_OWNER,_SLOT,_LINK,_MESH,_HAD,_HAD_DATA,_CREATED,_BASELINE,_DECLARED)}
    payload.update(target=_identity(material.get(_TARGET)),original=_identity(material.get(_ORIGINAL)),data_original=_identity(material.get(_DATA)))
    return hashlib.sha256(_json(payload).encode('utf8')).hexdigest()


def _cleanup(materials_created,groups_created):
    import bpy
    for material in reversed(materials_created):
        if material.users==0:bpy.data.materials.remove(material)
    for group in reversed(groups_created):
        if group.users==0:bpy.data.node_groups.remove(group)


def _owned(context,*,eevee=False):
    import bpy
    target,index,slot=_context(context,eevee=eevee);material=slot.material
    if material is None or material.get(_PROFILE)!=PROFILE:raise SurfaceError('Active slot has no owned surface application')
    owner=material.get(_OWNER)
    try:materials._scope(owner)
    except (materials.WorkflowError,ValueError,TypeError,AttributeError) as error:raise SurfaceError('Surface owner UUID changed') from error
    if (material.get(_TARGET)!=target or type(material.get(_SLOT)) is not int or material[_SLOT]!=index
            or material.get(_LINK)!=slot.link or material.get(_MESH)!=target.data.name
            or material.get(materials.SCOPE)!=owner or material.library or material.override_library or not material.is_editable
            or type(material.get(_HAD)) is not bool or type(material.get(_HAD_DATA)) is not bool
            or (_ORIGINAL in material)!=material[_HAD] or (_DATA in material)!=material[_HAD_DATA]
            or (material[_HAD] and not isinstance(material[_ORIGINAL],bpy.types.Material))
            or (material[_HAD_DATA] and not isinstance(material[_DATA],bpy.types.Material))
            or _signature(material)!=material.get(_SIGNATURE)):
        raise SurfaceError('Surface target/slot/link/source identity or metadata changed')
    if slot.link=='OBJECT' and target.data.materials[index]!=material.get(_DATA):
        raise SurfaceError('Underlying DATA material changed beneath the owned OBJECT slot')
    _protected(material)
    baseline=json.loads(material[_BASELINE]);current=materials.managed_snapshot(owner)
    if len(current['materials'])!=1 or current['materials'][0]['name']!=material.name:
        raise SurfaceError('Surface scope has unexpected material identities')
    if _json(current)!=_json(baseline):
        raise SurfaceError('Owned surface graph, input, wrapper, name or consumer changed; preserve author edits')
    declared=materials._settings(json.loads(material[_DECLARED]),'BLENDER_EEVEE')
    return target,index,slot,material,owner,baseline,declared


def apply(context,settings):
    """Apply one new owned material to the active, existing used slot only."""
    target,index,slot=_context(context,eevee=True)
    desired=materials._settings(settings,'BLENDER_EEVEE')
    if not materials.is_managed_mode(desired['mode']):raise SurfaceError('Choose an explicit managed surface, not preserve')
    previous=slot.material
    if previous is not None and _PROFILE in previous:raise SurfaceError('Restore the current surface before applying another mode/candidate')
    _protected(previous)
    owner=str(uuid.uuid4());link=slot.link;data_original=target.data.materials[index]
    created_materials=[];created_groups=[];material=None
    try:
        import bpy
        material=materials._build(bpy,desired,'BLENDER_EEVEE',owner,created_materials,created_groups)
        material[_PROFILE],material[_OWNER],material[_TARGET]=PROFILE,owner,target
        material[_SLOT],material[_LINK],material[_MESH]=index,link,target.data.name
        material[_HAD],material[_HAD_DATA]=previous is not None,data_original is not None
        if previous is not None:material[_ORIGINAL]=previous
        if data_original is not None:material[_DATA]=data_original
        slot.material=material
        material[materials.BINDINGS]=materials._json([dict(object=target.name,mesh=target.data.name,slot=index,link=link)])
        snapshot=materials.managed_snapshot(owner)
        material[_CREATED]=_json(snapshot);material[_BASELINE]=_json(snapshot);material[_DECLARED]=_json(desired)
        material[_SIGNATURE]=_signature(material)
        _owned(context)
    except Exception:
        slot.link=link;slot.material=previous
        _cleanup(created_materials,created_groups)
        raise
    return inspect(context)


def inspect(context):
    """Conflict report is read-only; it never reconciles hand edits."""
    try:
        target,index,slot,material,owner,baseline,declared=_owned(context)
        return dict(profile=PROFILE,status='valid',target=target.name,slot_index=index,slot_link=slot.link,
                    material=material.name,owner=owner,original_material=material[_ORIGINAL].name if material[_HAD] else None,
                    settings=baseline['materials'][0]['settings'],declared=declared,
                    snapshot=baseline,created_snapshot=json.loads(material[_CREATED]),issues=[],
                    scope='one existing used slot; surface only, not complete cel/game/manga styling')
    except (materials.WorkflowError,ValueError,TypeError,KeyError,AttributeError,RuntimeError,ReferenceError) as error:
        return dict(profile=PROFILE,status='conflict',issues=[str(error)])


def update(context,settings):
    """Strict author guard, then the existing three-way/rollback parameter core."""
    _,_,_,material,owner,baseline,declared=_owned(context,eevee=True)
    desired=materials._settings(settings,'BLENDER_EEVEE')
    if desired['mode']!=declared['mode']:raise SurfaceError('Mode changes require explicit restore then apply')
    old_metadata={key:material[key] for key in (_BASELINE,_DECLARED,_SIGNATURE)};result=None
    try:
        result=materials.update_parameters(owner,baseline,desired,baseline_declared=declared)
        material[_BASELINE]=_json(result['snapshot']);material[_DECLARED]=_json(desired);material[_SIGNATURE]=_signature(material)
    except Exception as error:
        try:
            if result is not None:
                materials.update_parameters(owner,result['snapshot'],baseline['materials'][0]['settings'],baseline_declared=desired)
            for key,value in old_metadata.items():material[key]=value
        except Exception as rollback_error:raise SurfaceError('Surface update failed; rollback also failed: '+str(rollback_error)) from error
        raise
    return inspect(context)


def restore(context):
    """Restore the recorded original pointer/link; refuse changed owned work."""
    target,index,slot,material,owner,_,_=_owned(context)
    if material.get('toon_finish_binding') is not None:
        raise SurfaceError('Remove the CEL finish explicitly before restoring its base surface')
    previous=material[_ORIGINAL] if material[_HAD] else None;link=material[_LINK]
    # The witnessed link is still identical, so assigning the pointer touches
    # only this DATA or OBJECT slot. Original graphs and other slots stay intact.
    slot.material=previous
    if slot.link!=link or slot.material!=previous:
        slot.material=material;raise SurfaceError('Original slot did not restore exactly')
    return dict(profile=PROFILE,status='restored',target=target.name,slot_index=index,slot_link=link,
                original_material=previous.name if previous else None,retained_material=material.name,
                owner=owner,retention='Unused owned material retained for audit/undo; restore deletes no author data')


def export_library(context,absolute_json_path):
    """Export actual native static settings into the existing closed library."""
    _,_,_,material,_,baseline,_=_owned(context)
    path=Path(absolute_json_path)
    if not path.is_absolute() or path.suffix.lower()!='.json' or not path.parent.is_dir():
        raise SurfaceError('Choose an absolute .json path in an existing directory')
    document=templates.document_from_entries(str(uuid.uuid4()),'Surface slot '+material.name,1,
        [dict(template_id=str(uuid.uuid4()),name=material.name,tags=['surface-slot','static'],engine='BLENDER_EEVEE',settings=baseline['materials'][0]['settings'])])
    data=json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)
    with path.open('x',encoding='utf8') as stream:stream.write(data)
    return dict(profile=PROFILE,status='exported',file=str(path),library_id=document['library_id'],revision=1,
                template_id=document['templates'][0]['template_id'],settings=document['templates'][0]['settings'],format=templates.SCHEMA_VERSION,
                note='Static material settings only; not a Pencil style_file or complete style family')


def apply_library(context,path,template_id=None):
    path=Path(path)
    if not path.is_absolute() or not path.is_file() or path.suffix.lower()!='.json' or path.stat().st_size>16*1024*1024:
        raise SurfaceError('Choose an existing absolute material-library JSON up to 16MiB')
    def reject(value):raise SurfaceError('Nonfinite library JSON: '+value)
    library=templates.normalize_document(json.loads(path.read_text(encoding='utf-8-sig'),parse_constant=reject))
    entries=library['templates']
    if template_id is None:
        if len(entries)!=1:raise SurfaceError('A multi-entry library requires one explicit template_id')
        entry=entries[0]
    else:
        matches=[row for row in entries if row['template_id']==template_id]
        if len(matches)!=1:raise SurfaceError('Choose an existing explicit library template_id')
        entry=matches[0]
    if entry['engine']!='BLENDER_EEVEE':raise SurfaceError('Active surface workflow only consumes EEVEE library entries')
    return apply(context,entry['settings'])
