# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit source-mesh direction maps and managed local normal weights.

Direction APIs keep their static reference-covector semantics. The separate
weight API edits only the managed modifier's actual scalar source attribute.
Neither changes mode, materials, topology, position or custom normals. Mapped
directions follow the reference transform, not an independently deforming jaw.
"""
import math
import struct

from . import artistic_normals as normals

SCHEMA = 'toon-normal-map-authoring.v1'
SPACE = 'REFERENCE_COVECTOR'
WEIGHT_SCHEMA = 'toon-normal-weight-authoring.v1'


class AuthoringError(ValueError):
    """Invalid, shared, internal or degenerate static authoring input."""


def _target(target, *, write=False):
    import bpy
    if (not isinstance(target,bpy.types.Object) or target not in list(bpy.data.objects)
            or target.type!='MESH' or target.library or target.override_library):
        raise AuthoringError('An existing local MESH target is required')
    mesh=target.data
    if write and (mesh.library or mesh.override_library or mesh.users!=1
                  or mesh.is_editmode or target.mode!='OBJECT'):
        raise AuthoringError('Authoring requires Object Mode and an exclusive editable source Mesh')
    return mesh


def _name(name):
    if type(name) is not str or not name or len(name.encode('utf8'))>63 or '\x00' in name:
        raise AuthoringError('Use a nonempty attribute name of at most 63 UTF-8 bytes')
    return name


def _protected(target,name):
    if name=='position':raise AuthoringError('The native position attribute cannot be a direction map')
    for modifier in target.modifiers:
        if normals.is_managed(modifier):
            _,_,fields=normals._owned(target,modifier)
            if name in fields.values():raise AuthoringError('An author map cannot alias an owned normal/mix/valid output')


def _vector(value):
    try:values=tuple(float(v) for v in value)
    except (TypeError,ValueError,OverflowError) as error:raise AuthoringError('A finite nonzero reference-space XYZ direction is required') from error
    if len(values)!=3 or not all(math.isfinite(v) for v in values) or math.hypot(*values)<=normals._EPSILON:
        raise AuthoringError('A finite nonzero reference-space XYZ direction is required')
    return values


def _float32(value):
    try:converted=tuple(struct.unpack('f',struct.pack('f',v))[0] for v in value)
    except (OverflowError,struct.error) as error:raise AuthoringError('Direction cannot be stored as finite FLOAT_VECTOR') from error
    return _vector(converted)


def _map(target,name,*,write=False):
    mesh=_target(target,write=write);_name(name);_protected(target,name)
    try:attribute=normals._mapping_values(mesh,name)
    except normals.NormalError as error:raise AuthoringError(str(error)) from error
    if attribute.is_internal:raise AuthoringError('Internal mesh attributes cannot be authored as direction maps')
    return mesh,attribute


def inspect_map(target,name):
    mesh=_target(target);_name(name)
    report=dict(schema=SCHEMA,status='valid',mapping_attribute=name,mapping_space=SPACE,
                vertices=len(mesh.vertices),count=0,domain=None,type=None,issues=[])
    attribute=mesh.attributes.get(name)
    if attribute is not None:report.update(count=len(attribute.data),domain=attribute.domain,type=attribute.data_type)
    try:_map(target,name)
    except (AuthoringError,normals.NormalError) as error:report.update(status='invalid',issues=[str(error)])
    return report


def initialize(target,modifier,name):
    """Create q=B^-1*A*p on original source vertices; do not activate mapped mode."""
    mesh=_target(target,write=True);_name(name);_protected(target,name)
    group,interface,_=normals._owned(target,modifier)
    if group[normals.PROFILE_KEY]!=normals.PROFILE:
        raise AuthoringError('Initialize requires an existing valid v1 field; clear mapped mode first')
    normals.validate(target,modifier)
    if mesh.attributes.get(name) is not None:raise AuthoringError('Refusing to overwrite an existing mesh attribute')
    reference=normals._input(modifier,interface,'reference').value
    a=normals._matrix(target.matrix_world);inverse=normals._inverse(reference.matrix_world)
    values=[]
    for vertex in mesh.vertices:
        point=tuple(float(v) for v in vertex.co)
        if not all(math.isfinite(v) for v in point):raise AuthoringError('Original mesh coordinates must be finite')
        world=[sum(a[i][j]*point[j] for j in range(3))+a[i][3] for i in range(3)]
        q=[sum(inverse[i][j]*world[j] for j in range(3))+inverse[i][3] for i in range(3)]
        values.extend(_float32(_vector(q)))
    attribute=None
    try:
        attribute=mesh.attributes.new(name,'FLOAT_VECTOR','POINT')
        if attribute.name!=name:raise AuthoringError('Blender changed the requested map name; no overwrite is allowed')
        attribute.data.foreach_set('vector',values)
        mesh.update();target.update_tag()
        report=inspect_map(target,name)
        if report['status']!='valid':raise AuthoringError('; '.join(report['issues']))
        return dict(report,status='initialized',created=True,pose_policy='SOURCE_MESH_STATIC')
    except Exception:
        if attribute is not None:mesh.attributes.remove(attribute)
        raise


def write_direction(target,name,indices,direction,blend=1.0):
    """Preflight the complete explicit edit, then publish one source-attribute update."""
    mesh,attribute=_map(target,name,write=True)
    if type(indices) not in (list,tuple) or not indices or any(type(i) is not int for i in indices):
        raise AuthoringError('Provide a nonempty explicit list of integer vertex indices')
    if len(set(indices))!=len(indices) or any(i<0 or i>=len(mesh.vertices) for i in indices):
        raise AuthoringError('Vertex indices must be unique and within the original mesh')
    if type(blend) not in (int,float) or not math.isfinite(blend) or not 0<=blend<=1:
        raise AuthoringError('blend must be finite in [0,1], never bool')
    direction=_vector(direction);length=math.hypot(*direction);unit=tuple(v/length for v in direction)
    if blend==0:
        return dict(schema=SCHEMA,status='unchanged',mapping_attribute=name,mapping_space=SPACE,
                    indices=list(indices),count=len(indices),blend=0.0,pose_policy='SOURCE_MESH_STATIC')
    original=[tuple(item.vector) for item in attribute.data]
    updated=list(original)
    for index in indices:
        mixed=_vector(tuple((1-blend)*old+blend*new for old,new in zip(original[index],unit)))
        magnitude=math.hypot(*mixed);updated[index]=_float32(tuple(v/magnitude for v in mixed))
    flattened=[v for row in updated for v in row]
    try:
        attribute.data.foreach_set('vector',flattened);mesh.update();target.update_tag()
    except Exception as error:
        try:
            mesh.attributes[name].data.foreach_set('vector',[v for row in original for v in row])
            mesh.update();target.update_tag()
        except Exception as rollback_error:raise AuthoringError('Direction write failed; source-map rollback also failed: '+str(rollback_error)) from error
        raise
    return dict(schema=SCHEMA,status='written',mapping_attribute=name,mapping_space=SPACE,
                indices=list(indices),count=len(indices),blend=float(blend),pose_policy='SOURCE_MESH_STATIC')


def write_weight(target,modifier,indices,value,blend=1.0):
    """Write explicit source vertices using the owned field's actual weight input."""
    mesh=_target(target,write=True)
    _,interface,fields=normals._owned(target,modifier)
    name=normals._input(modifier,interface,'weight').value
    _name(name)
    if name=='position' or name in fields.values():
        raise AuthoringError('Normal weights cannot alias position or owned normal/mix/valid outputs')
    attribute=mesh.attributes.get(name)
    if (attribute is None or attribute.domain!='POINT' or attribute.data_type!='FLOAT'
            or len(attribute.data)!=len(mesh.vertices)):
        raise AuthoringError('Managed weight source must be FLOAT POINT with one value per original vertex')
    if attribute.is_internal:
        raise AuthoringError('Internal mesh attributes cannot be authored as normal weights')
    original=[float(item.value) for item in attribute.data]
    if any(not math.isfinite(item) or not 0<=item<=1 for item in original):
        raise AuthoringError('All source weight values must be finite in [0,1]')
    if type(indices) not in (list,tuple) or not indices or any(type(i) is not int for i in indices):
        raise AuthoringError('Provide a nonempty explicit list of integer vertex indices')
    if len(set(indices))!=len(indices) or any(i<0 or i>=len(mesh.vertices) for i in indices):
        raise AuthoringError('Vertex indices must be unique and within the original mesh')
    for label,item in (('value',value),('blend',blend)):
        if type(item) not in (int,float) or not 0<=item<=1 or not math.isfinite(item):
            raise AuthoringError(label+' must be finite in [0,1], never bool')
    value=float(value);blend=float(blend)
    def record(status):
        current=mesh.attributes[name]
        return dict(schema=WEIGHT_SCHEMA,status=status,weight_attribute=name,
                    indices=list(indices),count=len(indices),value=value,blend=blend,
                    readback=[dict(index=index,value=float(current.data[index].value)) for index in indices])
    if blend==0:return record('unchanged')
    updated=list(original)
    for index in indices:updated[index]=(1-blend)*original[index]+blend*value
    try:
        attribute.data.foreach_set('value',updated);mesh.update();target.update_tag()
        result=record('written')
    except Exception as error:
        try:
            mesh.attributes[name].data.foreach_set('value',original)
            mesh.update();target.update_tag()
        except Exception as rollback_error:
            raise AuthoringError('Weight write failed; source-weight rollback also failed: '+str(rollback_error)) from error
        raise
    return result
