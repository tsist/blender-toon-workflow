# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit opt-in bindings for closed, non-refractive eye layers."""
import json
import uuid
from . import eye_overlay_nodes as nodes

KEY = 'toon_eye_overlays'
OBJECT_ID = 'toon_eye_overlay_object_id'
PROFILE = 'toon-eye-layer-lines.v1'


def configure(scene, bindings):
    """Bind already assigned object/material slots; never assign geometry."""
    import bpy
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 8:
        raise ValueError('Declare one to eight eye-layer material slots')
    if KEY in scene:
        raise ValueError('Eye layers already configured; remove the declaration first')
    rows = []; created = []
    try:
        for binding in bindings:
            if type(binding) is not dict or set(binding) != {'object', 'slot'}:
                raise ValueError('Explicit eye object and slot required')
            obj, slot = binding['object'], binding['slot']
            if (not isinstance(obj, bpy.types.Object) or obj.name not in scene.objects
                    or obj.type != 'MESH' or obj.library or obj.override_library
                    or type(slot) is not int or not 0 <= slot < len(obj.material_slots)):
                raise ValueError('Invalid local eye-layer object/slot')
            material = obj.material_slots[slot].material
            descriptor = nodes.validate(material)
            token = obj.get(OBJECT_ID)
            if token is None:
                token = uuid.uuid4().hex; obj[OBJECT_ID] = token; created.append(obj)
            rows.append(dict(target=obj.name, object_id=token, slot=slot,
                             material=material.name, owner=descriptor['owner']))
        scene[KEY] = json.dumps(dict(profile=PROFILE, bindings=rows), ensure_ascii=False)
        collect(scene)
    except Exception:
        if KEY in scene:del scene[KEY]
        for obj in created:del obj[OBJECT_ID]
        raise
    return collect(scene)


def collect(scene):
    """No declaration means the historical opaque admission remains in force."""
    import bpy
    if KEY not in scene:return []
    try:
        raw = json.loads(scene[KEY])
        if set(raw) != {'profile','bindings'} or raw['profile'] != PROFILE:
            raise ValueError('Unknown eye-layer declaration')
        bindings=raw['bindings']
        if type(bindings) is not list or not 1 <= len(bindings) <= 8:
            raise ValueError('Invalid eye-layer declaration size')
        result=[]; seen=set()
        for row in bindings:
            if set(row) != {'target','object_id','slot','material','owner'}:
                raise ValueError('Unknown eye-layer binding fields')
            obj=scene.objects.get(row['target']);slot=row['slot']
            if (obj is None or obj.type!='MESH' or obj.library or obj.override_library
                    or obj.get(OBJECT_ID)!=row['object_id']
                    or type(slot) is not int or not 0<=slot<len(obj.material_slots)):
                raise ValueError('Eye-layer object/slot identity changed')
            matches=[o for o in bpy.data.objects if o.get(OBJECT_ID)==row['object_id']]
            if matches != [obj]:raise ValueError('Duplicated eye-layer object identity')
            key=(obj.name,slot)
            if key in seen:raise ValueError('Repeated eye-layer binding')
            seen.add(key)
            material=obj.material_slots[slot].material
            descriptor=nodes.validate(material)
            if material.name!=row['material'] or descriptor['owner']!=row['owner']:
                raise ValueError('Eye-layer material identity changed')
            if not any(p.material_index==slot for p in obj.data.polygons):
                raise ValueError('Eye-layer slot has no polygons')
            if descriptor['kind']=='GLINT' and not obj.data.uv_layers.get(descriptor['settings']['uv_name']):
                raise ValueError('Eye glint UV missing')
            result.append(dict(**row, **{k:descriptor[k] for k in ('kind','settings','signature')}))
        # A managed transparent material cannot silently leak into other slots.
        declared={(r['target'],r['slot']) for r in result}
        for obj in scene.objects:
            if obj.type!='MESH' or obj.hide_render:continue
            for i,slot in enumerate(obj.material_slots):
                if nodes.is_present(slot.material) and (obj.name,i) not in declared:
                    raise ValueError('Undeclared eye-layer material assignment')
        return result
    except (TypeError,KeyError,json.JSONDecodeError) as error:
        raise ValueError('Invalid eye-layer declaration') from error


def remove(scene):
    """Remove only opt-in; materials remain editable and opaque run will reject."""
    rows=collect(scene)
    if not rows:return
    del scene[KEY]
    # Object identities remain as inert metadata to avoid affecting consumers.


def validate_receipt(value):
    """Cold validation binds the declared shader/settings to both snapshots."""
    import re
    if (type(value) is not dict or value.get('profile')!=PROFILE
            or value.get('data_exact') is not True
            or value.get('before')!=value.get('frozen')):
        raise ValueError('Eye-layer freeze receipt differs')
    rows=value.get('before')
    snapshot=value.get('render_snapshot',{})
    if (snapshot.get('schema')!='toon-eye-overlay-line-snapshot.v1'
            or not re.fullmatch('[0-9a-f]{64}',str(snapshot.get('sha256','')))
            or type(snapshot.get('triangles')) is not int or not 1<=snapshot['triangles']<=16384):
        raise ValueError('Invalid eye-layer RENDER snapshot')
    if type(rows) is not list or not 1<=len(rows)<=8:
        raise ValueError('Invalid eye-layer receipt size')
    keys=set()
    for row in rows:
        key=(row.get('target'),row.get('slot'))
        if (not isinstance(key[0],str) or not key[0] or type(key[1]) is not int
                or key[1]<0 or key in keys
                or not re.fullmatch('[0-9a-f]{64}',str(row.get('signature','')))):
            raise ValueError('Invalid eye-layer receipt binding')
        keys.add(key)
        nodes._normalize(row.get('kind'),row.get('settings'))
    return len(rows)
