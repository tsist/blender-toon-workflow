# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicit, opt-in head normal profile per Scene, with existing GN fields.

The Scene retains an unlinked Collection and ID-bound child records. No mesh,
weight, modifier, material, object link, handler or animation is created here.
Define is metadata-only; apply is a bounded all-target input transaction.
"""
import json
import math
import struct
import uuid

from . import artistic_normals as normals

PROFILE = 'toon-head-normals.v1'
KEY = '_toon_head_normals'
ROLES = ('FACE','LASH','BROW','HAIR','EYE','MOUTH','ACCESSORY')
MAX_BINDINGS = 64
_SCHEMA = 'toon_head_schema'
_OWNER = 'toon_head_owner'
_KIND = 'toon_head_kind'
_ID = 'toon_head_id'
_REFERENCE = 'toon_head_reference'
_TARGET = 'toon_head_target'
_GN_OWNER = 'toon_head_normal_owner'
_UID = 'toon_head_modifier_uid'
_WEIGHT = 'toon_head_weight_attribute'
_ATTRIBUTES = 'toon_head_attributes'
_ROLE = 'toon_head_role'
_STRENGTH = 'toon_head_strength'
_COUNT = 'toon_head_count'
_MEMBERS = 'toon_head_members'


class HeadError(ValueError):
    """Invalid, externally shared, damaged or conflicting head profile."""


def _bpy():
    import bpy
    return bpy


def _uuid(value):
    if type(value) is not str or len(value)!=32 or any(c not in '0123456789abcdef' for c in value):
        raise HeadError('Invalid head profile owner/record UUID')
    return value


def _strength(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise HeadError('Head strength must be a finite number in [0,1], never bool')
    return float(value)


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _parse(record,key):
    text=record.get(key)
    if type(text) is not str or len(text)>65536:
        raise HeadError('Missing or oversized head profile manifest')
    try:return json.loads(text)
    except (ValueError,TypeError) as error:raise HeadError('Invalid head profile manifest') from error


def _scene(scene):
    bpy=_bpy()
    if (not isinstance(scene,bpy.types.Scene) or scene not in list(bpy.data.scenes)
            or scene.library or scene.override_library):
        raise HeadError('An existing local editable Scene is required')


def _object(scene,value,label):
    bpy=_bpy()
    if (not isinstance(value,bpy.types.Object) or value not in list(bpy.data.objects)
            or value not in list(scene.objects) or value.library or value.override_library):
        raise HeadError(label+' must be an existing local Object in this Scene')
    return value


def _collection(value,owner,kind,parent):
    bpy=_bpy()
    if (not isinstance(value,bpy.types.Collection) or value not in list(bpy.data.collections)
            or value.library or value.override_library or value.get(_SCHEMA)!=PROFILE
            or value.get(_OWNER)!=owner or value.get(_KIND)!=kind):
        raise HeadError('Missing or unowned head '+kind+' Collection')
    if value.objects or value.use_fake_user or value.users!=1:
        raise HeadError('Head metadata must have only its declared owner and no linked Objects')
    parents=[candidate for candidate in [*bpy.data.collections,*(s.collection for s in bpy.data.scenes)]
             if value in list(candidate.children)]
    if parents != ([] if parent is None else [parent]):
        raise HeadError('Head metadata was linked, moved or externally shared')
    animation=getattr(value,'animation_data',None)
    if animation is not None and (animation.action or animation.drivers or animation.nla_tracks):
        raise HeadError('Head profile metadata cannot compete with native animation')


def _native(target):
    if target.type!='MESH':raise HeadError('Head normal targets must be MESH Objects')
    found=[m for m in target.modifiers if normals.is_managed(m)]
    if len(found)!=1:raise HeadError('Each head target needs exactly one existing artistic normal modifier')
    modifier=found[0]
    group,interface,attributes=normals._owned(target,modifier)
    parameters={key:normals._input(modifier,interface,key) for key in ('reference','weight','strength')}
    return dict(target=target,modifier=modifier,parameters=parameters,owner=_uuid(group[normals.OWNER]),
        uid=modifier.persistent_uid,weight=parameters['weight'].value,attributes=attributes)


def _identity(row):
    return dict(binding_id=row['id'],owner=row['owner'],modifier_uid=row['uid'],role=row['role'],
                weight_attribute=row['weight'],attributes=row['attributes'])


def _read(scene):
    _scene(scene)
    if KEY not in scene:return None
    root=scene.get(KEY)
    if not isinstance(root,_bpy().types.Collection):raise HeadError('Head root ID reference is missing or invalid')
    owner=_uuid(root.get(_OWNER));_collection(root,owner,'root',None)
    config_id=_uuid(root.get(_ID))
    for other in _bpy().data.scenes:
        if other!=scene and isinstance(other.get(KEY),_bpy().types.Collection):
            other_root=other.get(KEY)
            if other_root==root or other_root.get(_OWNER)==owner:
                raise HeadError('Head metadata/owner is shared by another Scene')
    reference=_object(scene,root.get(_REFERENCE),'Head reference')
    children=list(root.children)
    if not 1<=len(children)<=MAX_BINDINGS:raise HeadError('A head profile must bind 1..64 targets')
    rows=[];seen_targets=[];seen_owners=set();seen_ids=set()
    for record in children:
        _collection(record,owner,'binding',root)
        if record.children:raise HeadError('Head binding records must not have child Collections')
        target=_object(scene,record.get(_TARGET),'Head target')
        row=_native(target);row.update(record=record,id=_uuid(record.get(_ID)),role=record.get(_ROLE),
                                     requested_strength=_strength(record.get(_STRENGTH)))
        if row['role'] not in ROLES:raise HeadError('Unknown head binding role')
        if target in seen_targets or row['owner'] in seen_owners or row['id'] in seen_ids:
            raise HeadError('Duplicate head target/normal owner/record UUID')
        seen_targets.append(target);seen_owners.add(row['owner']);seen_ids.add(row['id'])
        if (record.get(_GN_OWNER)!=row['owner'] or type(record.get(_UID)) is not int
                or record[_UID]!=row['uid'] or record.get(_WEIGHT)!=row['weight']
                or _parse(record,_ATTRIBUTES)!=row['attributes']):
            raise HeadError('Head target normal owner/modifier UID/weight/field binding changed')
        rows.append(row)
    expected=sorted((_identity(row) for row in rows),key=lambda row:row['binding_id'])
    if type(root.get(_COUNT)) is not int or root[_COUNT]!=len(rows) or _parse(root,_MEMBERS)!=expected:
        raise HeadError('Head member count/identity manifest changed; a target was removed, rebound or added')
    return dict(root=root,owner=owner,id=config_id,reference=reference,bindings=rows)


def _receipt(value,status,reports):
    roles={role:sum(row['role']==role for row in value['bindings']) for role in ROLES}
    bindings=[]
    for row,gn in zip(value['bindings'],reports):
        actual=row['parameters']['reference'].value
        bindings.append(dict(binding_id=row['id'],role=row['role'],target=row['target'].name,
            owner=row['owner'],modifier_uid=row['uid'],reference=actual.name if actual else None,
            requested_reference=value['reference'].name,requested_strength=row['requested_strength'],
            strength=float(row['parameters']['strength'].value),weight_attribute=row['weight'],
            attributes=row['attributes'],gn=gn))
    return dict(profile=PROFILE,status=status,owner=value['owner'],config_id=value['id'],
        reference=value['reference'].name,binding_count=len(bindings),
        roles={key:count for key,count in roles.items() if count},bindings=bindings,
        scope='one explicit head configuration in this Scene; FACE coverage is authored by its own weight')


def inspect(scene):
    """Read metadata and actual evaluated native GN status; invalid IDs raise."""
    value=_read(scene)
    if value is None:return None
    reports=[normals.inspect(row['target'],row['modifier']) for row in value['bindings']]
    return _receipt(value,'valid' if all(r['status']=='valid' for r in reports) else 'invalid',reports)


def controls(scene):
    """Explicit author controls with real IDs; strength is requested metadata only."""
    value=_read(scene)
    if value is None:return None
    return dict(reference=value['reference'],bindings=[dict(target=row['target'],role=row['role'],
        strength=row['requested_strength'],control=row['record'],strength_property=_STRENGTH)
        for row in value['bindings']])


def clear(scene):
    """Remove only a fully valid exclusive metadata tree, preserving native data."""
    value=_read(scene)
    if value is None:return None
    retired=[value['root'],*value['root'].children]
    receipt=dict(profile=PROFILE,status='cleared',owner=value['owner'],config_id=value['id'],
                 binding_count=len(value['bindings']))
    # _read has already preflighted every referenced ID, owner, parent and user.
    # Native GN/modifier/mesh/weight/material IDs are never deletion candidates.
    del scene[KEY]
    receipt['retirement_issues']=_cleanup(retired)
    return receipt


def _new(name,owner,kind,created):
    value=_bpy().data.collections.new(name);created.append(value)
    value[_SCHEMA],value[_OWNER],value[_KIND]=PROFILE,owner,kind
    return value


def _cleanup(values):
    issues=[]
    for value in reversed(values):
        try:_bpy().data.collections.remove(value,do_unlink=True)
        except Exception as error:issues.append(str(error))
    return issues


def define(scene,reference,bindings,*,replace=False):
    """Metadata-only explicit head membership; replacement never repairs damaged IDs.

    Omitted binding strength means zero. Replacement preserves owner/config UUID
    and retained binding UUIDs; it stages a new tree and atomically swaps the
    Scene pointer. Removed members' independent native modifiers remain intact.
    """
    _scene(scene);_object(scene,reference,'Head reference')
    if type(replace) is not bool:raise HeadError('replace must be explicit Boolean')
    existing=_read(scene)
    if existing is not None and not replace:raise HeadError('A head profile already exists; explicit replace=True is required')
    if type(bindings) is not list or not 1<=len(bindings)<=MAX_BINDINGS:
        raise HeadError('Provide an explicit list of 1..64 head bindings')
    rows=[];targets=[];owners=set()
    for binding in bindings:
        if (type(binding) is not dict or set(binding)-{'target','role','strength'}
                or not {'target','role'}<=set(binding) or binding['role'] not in ROLES):
            raise HeadError('Bindings require target, known role and optional strength only')
        target=_object(scene,binding['target'],'Head target');row=_native(target)
        if target in targets or row['owner'] in owners:raise HeadError('Head bindings require unique target IDs and normal owners')
        targets.append(target);owners.add(row['owner'])
        row.update(role=binding['role'],requested_strength=_strength(binding.get('strength',0.0)))
        current=normals.inspect(target,row['modifier'])
        if current['status']!='valid':raise HeadError('Existing artistic normal field is invalid: '+target.name)
        normals._preflight(target,reference,row['weight'],row['requested_strength'])
        old_owner=next((old for old in existing['bindings'] if old['owner']==row['owner']),None) if existing else None
        if old_owner and (old_owner['target']!=target or old_owner['uid']!=row['uid']):
            raise HeadError('An existing head normal owner UUID cannot be rebound to another Object/modifier')
        previous=next((old for old in existing['bindings'] if old['target']==target),None) if existing else None
        row['id']=previous['id'] if previous else uuid.uuid4().hex
        rows.append(row)
    owner=existing['owner'] if existing else uuid.uuid4().hex
    config_id=existing['id'] if existing else uuid.uuid4().hex
    created=[];committed=False
    try:
        root=_new('Toon Head Normals',owner,'root',created)
        root[_ID],root[_REFERENCE],root[_COUNT]=config_id,reference,len(rows)
        for row in rows:
            record=_new('Toon Head '+row['role']+' '+row['target'].name,owner,'binding',created)
            record[_ID],record[_TARGET],record[_GN_OWNER]=row['id'],row['target'],row['owner']
            record[_UID],record[_WEIGHT],record[_ATTRIBUTES]=row['uid'],row['weight'],_json(row['attributes'])
            record[_ROLE],record[_STRENGTH]=row['role'],row['requested_strength']
            record.id_properties_ui(_STRENGTH).update(min=0.0,max=1.0,soft_min=0.0,soft_max=1.0)
            root.children.link(record)
        root[_MEMBERS]=_json(sorted((_identity(row) for row in rows),key=lambda row:row['binding_id']))
        scene[KEY]=root
        value=_read(scene)
        # Prepare the complete return value before retiring any previous IDs.
        reports=[normals.inspect(row['target'],row['modifier']) for row in value['bindings']]
        receipt=_receipt(value,'replaced' if existing else 'defined',reports)
        committed=True
    except Exception as error:
        if existing is not None:scene[KEY]=existing['root']
        elif KEY in scene:del scene[KEY]
        failures=_cleanup(created)
        if failures:raise HeadError('Head definition failed; newly created metadata cleanup also failed: '+'; '.join(failures)) from error
        raise
    if committed and existing is not None:
        # A completed pointer swap is the commit point. Retirement never touches
        # native data; any cleanup issue is returned explicitly, not hidden as a
        # failed definition after its old metadata has already been retired.
        receipt['retirement_issues']=_cleanup([existing['root'],*existing['root'].children])
    return receipt


def apply(scene):
    """All-target preflight, native-animation rejection and rollback on any failure."""
    value=_read(scene)
    if value is None:return None
    snapshots=[]
    for row in value['bindings']:
        normals._owned(row['target'],row['modifier'])
        normals._preflight(row['target'],value['reference'],row['weight'],row['requested_strength'])
        controlled=normals._animated_paths(row['target'])
        if controlled.intersection(parameter.path_from_id('value') for parameter in row['parameters'].values()):
            raise HeadError('Head profile conflicts with native reference/weight/strength animation: '+row['target'].name)
        snapshots.append((row['target'],row['parameters'],{key:parameter.value for key,parameter in row['parameters'].items()}))
    try:
        for row in value['bindings']:
            normals.configure(row['target'],row['modifier'],reference=value['reference'],
                weight_attribute=row['weight'],strength=row['requested_strength'])
        reports=[]
        for row in value['bindings']:
            parameters=row['parameters']
            expected=struct.unpack('f',struct.pack('f',row['requested_strength']))[0]
            if (parameters['reference'].value!=value['reference'] or parameters['weight'].value!=row['weight']
                    or float(parameters['strength'].value)!=expected):
                raise HeadError('Native RNA readback differs from the head request: '+row['target'].name)
            report=normals.inspect(row['target'],row['modifier'])
            if report['status']!='valid':raise HeadError('Applied artistic normal field is invalid: '+row['target'].name+'; '+'; '.join(report['issues']))
            reports.append(report)
        receipt=_receipt(value,'applied',reports)
    except Exception as error:
        failures=[]
        for target,parameters,original in reversed(snapshots):
            for key,parameter in parameters.items():
                try:parameter.value=original[key]
                except Exception as rollback_error:failures.append(target.name+':'+key+':'+str(rollback_error))
            try:target.update_tag()
            except Exception as rollback_error:failures.append(target.name+':update:'+str(rollback_error))
        if failures:raise HeadError('Head apply failed; native input rollback also failed: '+'; '.join(failures)) from error
        raise
    return receipt
