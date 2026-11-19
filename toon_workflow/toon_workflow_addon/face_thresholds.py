# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in directional face-shadow onset fields, not a distance SDF or UV map.

The default v1 stores four named point attributes; explicit v2 additionally
stores two polar vertical-selection fields, and v3 adds two uniform key weights.
Explicit v4 additionally packs two key weights into a single POINT vector.
Geometry, native mesh normals and
other attributes pass through. Shader comparison happens after interpolation;
there are no handlers, drivers or Python-dependent geometry nodes.
"""
import hashlib
import json
import math
import struct
import uuid

from . import artistic_normals as artistic

PROFILE = 'toon-face-threshold.v1'
PROFILE_V2 = 'toon-face-threshold.v2'
POLAR_PROFILE = PROFILE_V2
PROFILE_V3 = 'toon-face-threshold.v3'
DIRECTION_PROFILE = PROFILE_V3
PROFILE_V4 = CONTINUITY_PROFILE = 'toon-face-threshold.v4'
PROFILE_KEY = 'toon_face_threshold_profile'
OWNER = 'toon_face_threshold_owner'
SPACE = 'HEAD_DIRECTION_AZIMUTH'
SPACE_V2 = 'HEAD_DIRECTION_POLAR_LRUD'
SPACE_POLAR = SPACE_V2
SPACE_DIRECTION = 'HEAD_DIRECTION_POLAR_KEYS_14'
SPACE_CONTINUITY = 'HEAD_DIRECTION_POLAR_KEYS_14_PAIR'
FRONT_BACK_BLEND = .1
_TARGET = 'toon_face_threshold_target'
_UID = 'toon_face_threshold_modifier_uid'
_FIELDS = 'toon_face_threshold_attributes'
_INTERFACE = 'toon_face_threshold_interface'
_SIGNATURE = 'toon_face_threshold_signature'
_NODE = 'toon_face_threshold_node'
_DIRECTION_DOCUMENT = 'toon_face_threshold_direction_document'
_DIRECTION_SHA = 'toon_face_threshold_direction_sha256'
_CONTINUITY_KEYS = 'toon_face_threshold_continuity_keys'
_READER = 'toon-face-threshold-shader.v1'
_UNCHANGED = object()
_EPSILON = 1e-6


class FaceError(ValueError):
    """Invalid source, frame, stack or managed face-field identity."""


def _strength(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise FaceError('strength/value must be finite in [0,1], never bool')
    return float(value)


def _name(value):
    if type(value) is not str or not value or len(value.encode('utf8'))>63 or '\x00' in value:
        raise FaceError('Use a nonempty attribute name of at most 63 UTF-8 bytes')
    return value


def _attributes(owner,profile=PROFILE):
    if profile not in (PROFILE,PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE):raise FaceError('Unknown explicit face-field profile')
    roles=('threshold','phase','mix','valid')
    if profile in (PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE):roles+=('vertical_side','vertical_weight')
    if profile in (DIRECTION_PROFILE,CONTINUITY_PROFILE):roles+=('direction_upper','direction_top')
    if profile==CONTINUITY_PROFILE:roles+=('direction_pair',)
    return {role:'toon_face_'+role+'_'+owner for role in roles}


def _float(mesh,name):
    _name(name);attribute=mesh.attributes.get(name)
    if attribute is None or attribute.domain!='POINT' or attribute.data_type!='FLOAT' or len(attribute.data)!=len(mesh.vertices):
        raise FaceError('Face source must be FLOAT POINT with one value per vertex: '+name)
    if any(not math.isfinite(item.value) or not 0<=item.value<=1 for item in attribute.data):
        raise FaceError('Face source values must be finite in [0,1]: '+name)
    return attribute


def _target(target,*,write=False):
    import bpy
    if (not isinstance(target,bpy.types.Object) or target.type!='MESH' or target.library
            or target.override_library or target not in list(bpy.data.objects)):
        raise FaceError('An existing local editable MESH Object is required')
    mesh=target.data
    if write and (target.mode!='OBJECT' or mesh.is_editmode or mesh.library or mesh.override_library or mesh.users!=1):
        raise FaceError('Painting/initialization requires Object Mode and an exclusive editable Mesh')
    return mesh


def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _unit(value):
    length=math.hypot(*value)
    if not math.isfinite(length) or length==0:raise FaceError('A face-frame direction is degenerate or nonfinite')
    return tuple(v/length for v in value)


def direction_frame(reference_matrix,light_matrix,*,side_blend=.05,vertical_fade=.1):
    """Scalar GS/+Z-Sun oracle; translation never enters any direction."""
    side_blend=_strength(side_blend);vertical_fade=_strength(vertical_fade)
    try:
        b=artistic._matrix(reference_matrix);c=artistic._matrix(light_matrix)
        artistic._inverse(b);artistic._inverse(c)
    except ValueError as error:raise FaceError(str(error)) from error
    right=_unit(tuple(b[i][0] for i in range(3)))
    f0=tuple(-b[i][1] for i in range(3));projection=_dot(f0,right)
    forward=_unit(tuple(v-projection*r for v,r in zip(f0,right)))
    columns=[_unit(tuple(c[i][j] for i in range(3))) for j in range(3)]
    if any(abs(_dot(columns[i],columns[j]))>1e-5 for i,j in ((0,1),(0,2),(1,2))) or _dot(_cross(columns[0],columns[1]),columns[2])<=0:
        raise FaceError('Sun world matrix must have positive orthogonal axes; shear/mirroring is unsupported')
    light=columns[2];x=_dot(light,right);y=_dot(light,forward);h=math.hypot(x,y)
    vertical=h<=_EPSILON
    phase=0.0 if vertical else math.acos(max(-1.0,min(1.0,y/h)))/math.pi
    def smooth(value):
        t=max(0.,min(1.,value));return t*t*(3.-2.*t)
    side_weight=float(x>=0) if side_blend==0 else smooth((x/max(h,_EPSILON)+side_blend)/(2*side_blend))
    vertical_weight=0. if vertical else (1. if vertical_fade==0 else smooth(h/vertical_fade))
    return dict(right=list(right),forward=list(forward),light_direction=list(light),horizontal=h,
                side='LEFT' if x<0 else 'RIGHT',vertical=vertical,phase=phase,
                side_weight=side_weight,vertical_weight=vertical_weight,
                axis_semantics='reference GS +X/right and -X/left, not anatomical left/right',
                vertical_policy='phase=0 threshold=1 mix=0; fade only the additional face layer')


def direction_frame_polar(reference_matrix,light_matrix,*,side_blend=.05):
    """Explicit v2 oracle; v1 supplies identical matrix/GS/Sun admission checks."""
    frame=direction_frame(reference_matrix,light_matrix,side_blend=side_blend,vertical_fade=0.)
    right=tuple(frame['right']);forward=tuple(frame['forward']);light=tuple(frame['light_direction'])
    up=_unit(_cross(forward,right));x=_dot(light,right);y=_dot(light,forward);z=_dot(light,up)
    transverse_squared=x*x+z*z;transverse=math.sqrt(transverse_squared)
    t=max(0.,min(1.,transverse/FRONT_BACK_BLEND))
    vertical_weight=(z*z/max(transverse_squared,_EPSILON*_EPSILON))*(t*t*(3.-2.*t))
    frame.update(up=list(up),space=SPACE_V2,phase=math.acos(max(-1.,min(1.,y)))/math.pi,
        vertical_side=float(z>=0.),vertical_weight=vertical_weight,transverse=transverse,
        front_back_blend=FRONT_BACK_BLEND,
        vertical_policy='Polar phase=acos(dot(L,forward))/pi; threshold keeps LR selection; mix=weight*strength; vertical selector blends continuously to zero at front/back')
    return frame


def direction_frame_keys(reference_matrix,light_matrix,*,side_blend=.05):
    """Explicit v3 oracle; preserve all six polar fields and add key weights."""
    from . import face_direction_keys
    frame=direction_frame_polar(reference_matrix,light_matrix,side_blend=side_blend)
    light=frame['light_direction']
    head=_unit(tuple(_dot(light,frame[axis]) for axis in ('right','forward','up')))
    frame.update(space=SPACE_DIRECTION,head_light_direction=list(head),
        direction_keys=face_direction_keys.identity(),**face_direction_keys.contributions(head))
    return frame


def direction_frame_continuity(reference_matrix,light_matrix,*,side_blend=.05):
    """Explicit v4 oracle; pack key8/key11 from the same fixed key weights."""
    from . import face_direction_keys
    frame=direction_frame_keys(reference_matrix,light_matrix,side_blend=side_blend)
    frame.update(space=SPACE_CONTINUITY,continuity_keys=face_direction_keys.continuity_identity(),
        direction_pair=[frame['weights'][i] for i in face_direction_keys.PAIR_KEY_INDICES]+[0.0])
    return frame


def _preflight(target,reference,light,left,right,weight,strength,*,side_blend=.05,vertical_fade=.1,fields=None,depsgraph=None,profile=_UNCHANGED):
    import bpy
    mesh=_target(target);strength=_strength(strength);side_blend=_strength(side_blend);vertical_fade=_strength(vertical_fade)
    # Older reader adapters pass the owned field map without a profile. Infer
    # only the exact versioned role sets; formal build/inspect pass profile.
    base_roles={'threshold','phase','mix','valid'}
    if profile is _UNCHANGED:
        if fields is None:profile=PROFILE
        elif type(fields)is dict and set(fields)==base_roles:profile=PROFILE
        elif type(fields)is dict and set(fields)==base_roles|{'vertical_side','vertical_weight'}:profile=PROFILE_V2
        elif type(fields)is dict and set(fields)==base_roles|{'vertical_side','vertical_weight','direction_upper','direction_top'}:profile=DIRECTION_PROFILE
        elif type(fields)is dict and set(fields)==base_roles|{'vertical_side','vertical_weight','direction_upper','direction_top','direction_pair'}:profile=CONTINUITY_PROFILE
        else:raise FaceError('Cannot infer a face-field profile from unknown field roles')
    if profile not in (PROFILE,PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE):raise FaceError('Unknown explicit face-field profile')
    if fields is not None and (type(fields)is not dict or set(fields)!=set(_attributes('',profile=profile))):
        raise FaceError('Face output roles do not match their explicit profile')
    if profile in (PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE) and fields is not None:
        prefix='toon_face_threshold_';threshold_name=fields['threshold']
        owner=threshold_name[len(prefix):] if type(threshold_name)is str and threshold_name.startswith(prefix) else ''
        if len(owner)!=32 or any(c not in '0123456789abcdef' for c in owner) or fields!=_attributes(owner,profile=profile):
            raise FaceError('Polar output field names do not share their exact owned identity')
    if profile==PROFILE_V2 and vertical_fade!=0.:raise FaceError('Polar face fields require explicit vertical_fade=0; v1 fade is not a v2 control')
    if profile==DIRECTION_PROFILE and vertical_fade!=0.:raise FaceError('Direction-key face fields require explicit vertical_fade=0; v1 fade is not a v3 control')
    if profile==CONTINUITY_PROFILE and vertical_fade!=0.:raise FaceError('Continuity face fields require explicit vertical_fade=0; v1 fade is not a v4 control')
    for value,label in ((reference,'reference'),(light,'Sun')):
        if (not isinstance(value,bpy.types.Object) or value not in list(bpy.data.objects)
                or value.library or value.override_library or value==target):
            raise FaceError('An existing independent local '+label+' Object is required')
    if light.type!='LIGHT' or light.data.type!='SUN':raise FaceError('Only an actual SUN light Object is supported')
    for name in (left,right,weight):
        if fields and name in fields.values():raise FaceError('Face source cannot alias an owned output field')
        _float(mesh,name)
    graph=depsgraph or bpy.context.evaluated_depsgraph_get()
    try:artistic._inverse(target.evaluated_get(graph).matrix_world)
    except ValueError as error:raise FaceError(str(error)) from error
    for value in (reference,light):
        cursor=value;visited=set()
        while cursor is not None:
            if cursor==target or cursor.as_pointer() in visited:
                raise FaceError('Reference/Sun parenting feedback or cyclic ancestry is unsupported')
            visited.add(cursor.as_pointer())
            if any(getattr(constraint,'target',None)==target for constraint in cursor.constraints):
                raise FaceError('Reference/Sun constraint feedback from the target is unsupported')
            if value==light and any(not math.isfinite(s) or s<=0 for s in cursor.scale):
                raise FaceError('Sun ancestry must have positive finite native scales')
            cursor=cursor.parent
    if profile==CONTINUITY_PROFILE:
        frame=direction_frame_continuity(reference.evaluated_get(graph).matrix_world,light.evaluated_get(graph).matrix_world,side_blend=side_blend)
    elif profile==DIRECTION_PROFILE:
        frame=direction_frame_keys(reference.evaluated_get(graph).matrix_world,light.evaluated_get(graph).matrix_world,side_blend=side_blend)
    elif profile==PROFILE_V2:
        frame=direction_frame_polar(reference.evaluated_get(graph).matrix_world,light.evaluated_get(graph).matrix_world,side_blend=side_blend)
    else:frame=direction_frame(reference.evaluated_get(graph).matrix_world,light.evaluated_get(graph).matrix_world,side_blend=side_blend,vertical_fade=vertical_fade)
    return strength,graph,frame


def is_managed(modifier):
    return getattr(modifier,'type',None)=='NODES' and modifier.node_group is not None and modifier.node_group.get(PROFILE_KEY) in (PROFILE,PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE)


def _family(modifier,key,prefix):
    value=modifier.node_group.get(key) if modifier.type=='NODES' and modifier.node_group else None
    return type(value) is str and value.startswith(prefix)


def _tail(modifier):
    return (_family(modifier,artistic.PROFILE_KEY,'toon-artistic-normal.v')
            or _family(modifier,'toon_camera_correction_profile','toon-camera-xy-depth.v'))


def _downstream(target,start):
    art_seen=correction_seen=False
    for modifier in list(target.modifiers)[start:]:
        if not (modifier.show_viewport or modifier.show_render):continue
        if artistic.is_managed(modifier) and not art_seen and not correction_seen:
            artistic._owned(target,modifier);art_seen=True
        elif _family(modifier,'toon_camera_correction_profile','toon-camera-xy-depth.v1') and not correction_seen:
            from . import camera_correction
            if modifier.node_group.get('toon_camera_correction_profile')!=camera_correction.PROFILE:
                raise FaceError('Unknown downstream camera correction profile')
            camera_correction._owned(target,modifier);correction_seen=True
        else:raise FaceError('Face fields must precede only one owned artistic normal and then an owned camera correction')


def _socket(values,identifier):
    result=[value for value in values if value.identifier==identifier]
    if len(result)!=1:raise FaceError('Expected exactly one native socket: '+identifier)
    return result[0]


def graph_signature(group):
    """Sign actual face nodes/links/interface plus face-specific metadata."""
    def value(v):
        if v is None or isinstance(v,(str,bool,int,float)):return v
        if hasattr(v,'name_full'):return ['ID',v.bl_rna.identifier,v.name_full]
        try:return [value(x) for x in v]
        except TypeError:return str(v)
    nodes=[]
    for node in group.nodes:
        props={key:value(getattr(node,key)) for key in ('operation','data_type','input_type','domain',
            'transform_space','mute','use_clamp','attribute_name','attribute_type') if hasattr(node,key)}
        nodes.append([node.get(_NODE),node.bl_idname,props,[(s.identifier,value(s.default_value)) for s in node.inputs if hasattr(s,'default_value')]])
    links=sorted(((l.from_node.get(_NODE),l.from_socket.identifier,l.to_node.get(_NODE),l.to_socket.identifier) for l in group.links),key=repr)
    sockets=[{key:value(getattr(s,key)) for key in ('identifier','name','in_out','socket_type','default_value','min_value','max_value') if hasattr(s,key)}
             for s in group.interface.items_tree if s.item_type=='SOCKET']
    payload=dict(nodes=sorted(nodes,key=lambda row:str(row[0])),links=links,sockets=sockets,
        profile=group.get(PROFILE_KEY),owner=group.get(OWNER),uid=group.get(_UID),
        fields=group.get(_FIELDS),interface=group.get(_INTERFACE))
    if group.get(PROFILE_KEY) in (DIRECTION_PROFILE,CONTINUITY_PROFILE):
        payload.update(direction_document=group.get(_DIRECTION_DOCUMENT),direction_sha256=group.get(_DIRECTION_SHA))
    if group.get(PROFILE_KEY)==CONTINUITY_PROFILE:
        payload['continuity_keys']=group.get(_CONTINUITY_KEYS)
    return hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _owned(target,modifier):
    if modifier.id_data!=target or not is_managed(modifier):raise FaceError('The face-field modifier/profile does not belong to this target')
    group=modifier.node_group;owner=group.get(OWNER)
    if (type(owner) is not str or len(owner)!=32 or any(c not in '0123456789abcdef' for c in owner)
            or group.get(_TARGET)!=target or group.get(_UID)!=modifier.persistent_uid or group.users!=1):
        raise FaceError('The face-field graph is unowned, rebound or shared')
    try:fields=json.loads(group[_FIELDS]);interface=json.loads(group[_INTERFACE])
    except (ValueError,TypeError,KeyError) as error:raise FaceError('Invalid face-field metadata') from error
    if fields!=_attributes(owner,profile=group.get(PROFILE_KEY)):raise FaceError('Face output roles/names do not match their owner')
    if group.get(PROFILE_KEY) in (DIRECTION_PROFILE,CONTINUITY_PROFILE):
        from . import face_direction_keys
        expected=json.dumps(face_direction_keys.document(),sort_keys=True,allow_nan=False)
        if group.get(_DIRECTION_DOCUMENT)!=expected or group.get(_DIRECTION_SHA)!=face_direction_keys.identity()['document_sha256']:
            raise FaceError('Direction-key bake does not match the explicit v3 profile')
    if group.get(PROFILE_KEY)==CONTINUITY_PROFILE:
        expected=json.dumps(face_direction_keys.continuity_identity(),sort_keys=True,allow_nan=False)
        if group.get(_CONTINUITY_KEYS)!=expected:
            raise FaceError('Continuity packed-pair layout does not match the explicit v4 profile')
    elif _CONTINUITY_KEYS in group:
        raise FaceError('Legacy face-field profile must not carry continuity identity')
    if graph_signature(group)!=group.get(_SIGNATURE):raise FaceError('The managed face-field graph was edited')
    index=list(target.modifiers).index(modifier)
    if any((m.show_viewport or m.show_render) and (_tail(m) or _family(m,PROFILE_KEY,'toon-face-threshold.v')) for m in list(target.modifiers)[:index]):
        raise FaceError('Face fields must be after native deform but before artistic normals/camera correction')
    _downstream(target,index+1)
    return group,interface,fields


def _input(modifier,interface,key):return getattr(modifier.properties.inputs,interface[key])


def _source_guard(target,names):
    for modifier in target.modifiers:
        if _family(modifier,PROFILE_KEY,'toon-face-threshold.v'):
            if not is_managed(modifier):raise FaceError('Unknown face-field profile cannot be authored implicitly')
            _,_,fields=_owned(target,modifier)
            if any(name in fields.values() for name in names):raise FaceError('Face source cannot alias an owned output field')


def initialize(target,left,right,weight):
    mesh=_target(target,write=True);specs={}
    for name,default in ((left,.5),(right,.5),(weight,0.0)):
        _name(name)
        if name in specs and specs[name]!=default:raise FaceError('Conflicting initialization defaults require distinct threshold/weight names')
        specs[name]=default
    _source_guard(target,specs)
    for name in specs:
        if mesh.attributes.get(name) is not None:_float(mesh,name)
    created=[]
    try:
        for name,default in specs.items():
            if mesh.attributes.get(name) is None:
                attribute=mesh.attributes.new(name,'FLOAT','POINT');created.append(attribute.name)
                if attribute.name!=name:raise FaceError('Blender changed the requested source attribute name')
                attribute.data.foreach_set('value',[default]*len(mesh.vertices))
        mesh.update();target.update_tag()
        for name in specs:_float(mesh,name)
    except Exception:
        for name in reversed(created):
            if mesh.attributes.get(name) is not None:mesh.attributes.remove(mesh.attributes[name])
        raise
    return dict(profile=PROFILE,status='initialized',created=created,left_attribute=left,right_attribute=right,weight_attribute=weight)


def write_values(target,name,indices,value,blend=1.0):
    mesh=_target(target,write=True);_source_guard(target,[name]);attribute=_float(mesh,name)
    if attribute.is_internal:raise FaceError('Internal mesh attributes cannot be painted')
    value=_strength(value);blend=_strength(blend)
    if type(indices) not in (list,tuple) or not indices or any(type(i) is not int for i in indices) or len(set(indices))!=len(indices) or any(i<0 or i>=len(mesh.vertices) for i in indices):
        raise FaceError('Paint indices must be an explicit nonempty unique in-range integer list')
    if blend==0:return dict(profile=PROFILE,status='unchanged',attribute=name,count=len(indices))
    original=[item.value for item in attribute.data];updated=list(original)
    for index in indices:updated[index]=(1-blend)*original[index]+blend*value
    try:attribute.data.foreach_set('value',updated);mesh.update();target.update_tag()
    except Exception as error:
        try:mesh.attributes[name].data.foreach_set('value',original);mesh.update();target.update_tag()
        except Exception as rollback_error:raise FaceError('Face paint failed; rollback also failed: '+str(rollback_error)) from error
        raise
    return dict(profile=PROFILE,status='written',attribute=name,count=len(indices),indices=list(indices),blend=blend)


def _build_group(owner,profile=PROFILE):
    if profile==CONTINUITY_PROFILE:return _build_group_direction(owner,continuity=True)
    if profile==DIRECTION_PROFILE:return _build_group_direction(owner)
    if profile==PROFILE_V2:return _build_group_polar(owner)
    if profile!=PROFILE:raise FaceError('Unknown explicit face-field profile')
    import bpy
    group=bpy.data.node_groups.new('Toon Face Threshold Fields','GeometryNodeTree')
    group[PROFILE_KEY],group[OWNER]=PROFILE,owner;fields=_attributes(owner);group[_FIELDS]=json.dumps(fields,sort_keys=True)
    interface={}
    for key,name,kind in [('geometry','Geometry','NodeSocketGeometry'),('reference','Head Reference','NodeSocketObject'),('light','Sun','NodeSocketObject'),
                          ('left','Left Threshold','NodeSocketString'),('right','Right Threshold','NodeSocketString'),('weight','Weight Attribute','NodeSocketString'),('strength','Strength','NodeSocketFloat'),
                          ('side_blend','Side Blend','NodeSocketFloat'),('vertical_fade','Vertical Fade','NodeSocketFloat')]:
        socket=group.interface.new_socket(name=name,in_out='INPUT',socket_type=kind);interface[key]=socket.identifier
        if key in {'strength','side_blend','vertical_fade'}:
            socket.default_value={'strength':0.,'side_blend':.05,'vertical_fade':.1}[key];socket.min_value=0.;socket.max_value=1.
    interface['output']=group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry').identifier
    group[_INTERFACE]=json.dumps(interface,sort_keys=True)
    def node(key,kind,**props):
        v=group.nodes.new(kind);v[_NODE]=key;v.label=key
        for name,value in props.items():setattr(v,name,value)
        return v
    def link(a,b):group.links.new(a,b)
    def math_(key,op,a,b=0.):
        v=node(key,'ShaderNodeMath',operation=op)
        for socket,value in zip(v.inputs[:2],(a,b)):
            if isinstance(value,(int,float)):socket.default_value=value
            else:link(value,socket)
        return _socket(v.outputs,'Value')
    def vector(key,op,a,b=None):
        v=node(key,'ShaderNodeVectorMath',operation=op);link(a,v.inputs[0])
        if b is not None:link(b,_socket(v.inputs,'Scale') if op=='SCALE' else v.inputs[1])
        return _socket(v.outputs,'Value' if op in {'DOT_PRODUCT','LENGTH'} else 'Vector')
    def boolean(key,op,a,b=None):
        v=node(key,'FunctionNodeBooleanMath',operation=op);link(a,_socket(v.inputs,'Boolean'))
        if b is not None:link(b,_socket(v.inputs,'Boolean_001'))
        return _socket(v.outputs,'Boolean')
    def switch(key,kind,test,off,on):
        v=node(key,'GeometryNodeSwitch',input_type=kind);link(test,_socket(v.inputs,'Switch'))
        for name,value in [('False',off),('True',on)]:
            if isinstance(value,(int,float,bool)):_socket(v.inputs,name).default_value=value
            else:link(value,_socket(v.inputs,name))
        return _socket(v.outputs,'Output')
    def smooth(key,value):
        clipped=math_(key+'_max','MINIMUM',math_(key+'_min','MAXIMUM',value,0.),1.)
        return math_(key,'MULTIPLY',math_(key+'_squared','MULTIPLY',clipped,clipped),math_(key+'_three_minus_two','SUBTRACT',3.,math_(key+'_twice','MULTIPLY',2.,clipped)))
    def direction(key,matrix,axis):
        v=node(key,'FunctionNodeTransformDirection');_socket(v.inputs,'Direction').default_value=axis
        link(matrix,_socket(v.inputs,'Transform'));return _socket(v.outputs,'Direction')
    entry=node('input','NodeGroupInput');output=node('output','NodeGroupOutput')
    self_obj=node('self','GeometryNodeSelfObject');self_info=node('target_matrix','GeometryNodeObjectInfo',transform_space='ORIGINAL')
    link(_socket(self_obj.outputs,'Self Object'),_socket(self_info.inputs,'Object'))
    ref=node('reference','GeometryNodeObjectInfo',transform_space='ORIGINAL');light=node('light','GeometryNodeObjectInfo',transform_space='ORIGINAL')
    link(_socket(entry.outputs,interface['reference']),_socket(ref.inputs,'Object'));link(_socket(entry.outputs,interface['light']),_socket(light.inputs,'Object'))
    valid=[]
    for key,matrix in [('target',_socket(self_info.outputs,'Transform')),('reference',_socket(ref.outputs,'Transform')),('light',_socket(light.outputs,'Transform'))]:
        inv=node('inverse_'+key,'FunctionNodeInvertMatrix');link(matrix,_socket(inv.inputs,'Matrix'));valid.append(_socket(inv.outputs,'Invertible'))
    r0=direction('right_raw',_socket(ref.outputs,'Transform'),(1.,0.,0.));f0=direction('forward_raw',_socket(ref.outputs,'Transform'),(0.,-1.,0.))
    r=vector('right','NORMALIZE',r0);along=vector('forward_right','DOT_PRODUCT',f0,r)
    fproj=vector('orthogonal_forward','SUBTRACT',f0,vector('right_projection','SCALE',r,along));f=vector('forward','NORMALIZE',fproj)
    columns=[direction('sun_axis_'+str(i),_socket(light.outputs,'Transform'),axis) for i,axis in enumerate(((1.,0.,0.),(0.,1.,0.),(0.,0.,1.)))]
    units=[vector('sun_unit_'+str(i),'NORMALIZE',value) for i,value in enumerate(columns)];l=units[2]
    for key,value in [('right',r0),('forward',fproj),*[(('sun'+str(i)),value) for i,value in enumerate(columns)]]:
        length=vector(key+'_length','LENGTH',value);valid.extend([math_(key+'_nonzero','GREATER_THAN',length,0.),math_(key+'_finite','LESS_THAN',length,3.402823e38)])
    for i,j in ((0,1),(0,2),(1,2)):
        dot=vector('sun_dot_'+str(i)+str(j),'DOT_PRODUCT',units[i],units[j])
        valid.append(math_('sun_orthogonal_'+str(i)+str(j),'LESS_THAN',math_('sun_abs_'+str(i)+str(j),'ABSOLUTE',dot),1e-5))
    valid.append(math_('sun_positive','GREATER_THAN',vector('sun_handedness','DOT_PRODUCT',vector('sun_cross','CROSS_PRODUCT',units[0],units[1]),units[2]),0.))
    x=vector('light_right','DOT_PRODUCT',l,r);y=vector('light_forward','DOT_PRODUCT',l,f)
    h=math_('horizontal','SQRT',math_('horizontal_squared','ADD',math_('right_squared','MULTIPLY',x,x),math_('forward_squared','MULTIPLY',y,y)))
    vertical=boolean('vertical','NOT',math_('projected','GREATER_THAN',h,_EPSILON))
    cosine=math_('cosine','DIVIDE',y,math_('safe_horizontal','MAXIMUM',h,_EPSILON))
    clipped=math_('cosine_max','MINIMUM',math_('cosine_min','MAXIMUM',cosine,-1.),1.)
    phase_raw=math_('phase_raw','DIVIDE',math_('angle','ARCCOSINE',clipped),math.pi)
    phase=switch('phase_vertical','FLOAT',vertical,phase_raw,0.)
    left_side=math_('choose_left','LESS_THAN',x,0.)
    side_blend=_socket(entry.outputs,interface['side_blend']);vertical_fade=_socket(entry.outputs,interface['vertical_fade'])
    # Substitute 1 only for an exactly disabled width; positive widths retain
    # their explicit artist value, including small positive parameters.
    side_enabled=math_('side_blend_enabled','GREATER_THAN',side_blend,0.)
    side_width=switch('side_width','FLOAT',side_enabled,1.,side_blend)
    side_unit=math_('side_unit','DIVIDE',x,math_('side_safe_h','MAXIMUM',h,_EPSILON))
    side_t=math_('side_t','DIVIDE',math_('side_offset','ADD',side_unit,side_blend),math_('side_denominator','MULTIPLY',2.,side_width))
    side_weight=switch('side_weight','FLOAT',side_enabled,boolean('hard_right','NOT',left_side),smooth('smooth_side',side_t))
    fade_enabled=math_('vertical_fade_enabled','GREATER_THAN',vertical_fade,0.)
    fade_width=switch('fade_width','FLOAT',fade_enabled,1.,vertical_fade)
    fade=switch('vertical_weight','FLOAT',fade_enabled,1.,smooth('smooth_vertical',math_('vertical_t','DIVIDE',h,fade_width)))
    fade=switch('vertical_zero','FLOAT',vertical,fade,0.)
    sources={}
    for key in ('left','right','weight'):
        attr=node('source_'+key,'GeometryNodeInputNamedAttribute',data_type='FLOAT')
        link(_socket(entry.outputs,interface[key]),_socket(attr.inputs,'Name'));value=_socket(attr.outputs,'Attribute');sources[key]=value
        valid.extend([_socket(attr.outputs,'Exists'),math_(key+'_not_nan','GREATER_THAN',value,-1.),
            boolean(key+'_nonnegative','NOT',math_(key+'_negative','LESS_THAN',value,0.)),boolean(key+'_at_most_one','NOT',math_(key+'_over_one','LESS_THAN',1.,value))])
    strength=_socket(entry.outputs,interface['strength'])
    valid.extend([math_('strength_not_nan','GREATER_THAN',strength,-1.),boolean('strength_nonnegative','NOT',math_('strength_negative','LESS_THAN',strength,0.)),
                  boolean('strength_at_most_one','NOT',math_('strength_over_one','LESS_THAN',1.,strength)),math_('horizontal_finite','LESS_THAN',h,3.402823e38)])
    for key,value in [('side_blend',side_blend),('vertical_fade',vertical_fade)]:
        valid.extend([math_(key+'_not_nan','GREATER_THAN',value,-1.),boolean(key+'_nonnegative','NOT',math_(key+'_negative','LESS_THAN',value,0.)),boolean(key+'_at_most_one','NOT',math_(key+'_over_one','LESS_THAN',1.,value))])
    good=valid[0]
    for i,predicate in enumerate(valid[1:]):good=boolean('valid_'+str(i),'AND',good,predicate)
    selected=math_('selected_threshold','ADD',math_('left_contribution','MULTIPLY',sources['left'],math_('left_weight','SUBTRACT',1.,side_weight)),math_('right_contribution','MULTIPLY',sources['right'],side_weight))
    threshold=switch('threshold_vertical','FLOAT',vertical,selected,1.)
    threshold=switch('safe_threshold','FLOAT',good,1.,threshold);phase=switch('safe_phase','FLOAT',good,0.,phase)
    mix=switch('safe_mix','FLOAT',good,0.,math_('effective_mix','MULTIPLY',math_('weighted_strength','MULTIPLY',sources['weight'],strength),fade))
    geometry=_socket(entry.outputs,interface['geometry'])
    for key,kind,value in [('threshold','FLOAT',threshold),('phase','FLOAT',phase),('mix','FLOAT',mix),('valid','BOOLEAN',good)]:
        store=node('store_'+key,'GeometryNodeStoreNamedAttribute',data_type=kind,domain='POINT');_socket(store.inputs,'Name').default_value=fields[key]
        link(geometry,_socket(store.inputs,'Geometry'));link(value,_socket(store.inputs,'Value'));geometry=_socket(store.outputs,'Geometry')
    link(geometry,_socket(output.inputs,interface['output']))
    for i,v in enumerate(group.nodes):v.location=((i%8)*220,-(i//8)*220)
    return group,interface


def _build_group_polar(owner):
    """Derive only explicit v2; the default v1 node-construction body stays intact."""
    import bpy
    group,interface=_build_group(owner)
    try:
        group[PROFILE_KEY]=PROFILE_V2
        fields=_attributes(owner,profile=PROFILE_V2);group[_FIELDS]=json.dumps(fields,sort_keys=True)
        nodes={n.get(_NODE):n for n in group.nodes}
        entry=nodes['input']
        fade_socket=next(s for s in group.interface.items_tree if s.item_type=='SOCKET' and s.identifier==interface['vertical_fade'])
        fade_socket.default_value=0.;fade_socket.max_value=0.
        # The v1 nonnegative/range validity chain now requires precisely zero.
        # A manually/animation-driven positive fade must invalidate v2, even
        # though its old fade calculation is no longer a shading dependency.
        nodes['vertical_fade_over_one'].inputs[0].default_value=0.
        def relink(value,socket):
            for old in list(socket.links):group.links.remove(old)
            group.links.new(value,socket)
        def node(key,kind,**props):
            result=group.nodes.new(kind);result[_NODE]=key;result.label=key
            for name,value in props.items():setattr(result,name,value)
            return result
        def math_(key,operation,a,b=0.):
            result=node(key,'ShaderNodeMath',operation=operation)
            for socket,value in zip(result.inputs[:2],(a,b)):
                if isinstance(value,(int,float)):socket.default_value=value
                else:group.links.new(value,socket)
            return _socket(result.outputs,'Value')
        def vector(key,operation,a,b=None):
            result=node(key,'ShaderNodeVectorMath',operation=operation);group.links.new(a,result.inputs[0])
            if b is not None:group.links.new(b,result.inputs[1])
            return _socket(result.outputs,'Value' if operation=='DOT_PRODUCT' else 'Vector')
        def safe(key,value,good):
            result=node(key,'GeometryNodeSwitch',input_type='FLOAT')
            group.links.new(good,_socket(result.inputs,'Switch'));_socket(result.inputs,'False').default_value=0.
            group.links.new(value,_socket(result.inputs,'True'));return _socket(result.outputs,'Output')
        # Polar phase uses the full 3D forward dot, never the horizontal-normalized
        # v1 cosine or its pure-vertical override. Retain the original LR blend.
        x=_socket(nodes['light_right'].outputs,'Value');y=_socket(nodes['light_forward'].outputs,'Value')
        relink(y,nodes['cosine_min'].inputs[0])
        relink(_socket(nodes['phase_raw'].outputs,'Value'),_socket(nodes['safe_phase'].inputs,'True'))
        relink(_socket(nodes['selected_threshold'].outputs,'Value'),_socket(nodes['safe_threshold'].inputs,'True'))
        relink(_socket(nodes['weighted_strength'].outputs,'Value'),_socket(nodes['safe_mix'].inputs,'True'))
        up=vector('polar_up','NORMALIZE',vector('polar_up_raw','CROSS_PRODUCT',
            _socket(nodes['forward'].outputs,'Vector'),_socket(nodes['right'].outputs,'Vector')))
        z=vector('polar_light_up','DOT_PRODUCT',_socket(nodes['sun_unit_2'].outputs,'Vector'),up)
        vertical_side=math_('polar_vertical_side','SUBTRACT',1.,math_('polar_below','LESS_THAN',z,0.))
        z2=math_('polar_up_squared','MULTIPLY',z,z)
        transverse2=math_('polar_transverse_squared','ADD',_socket(nodes['right_squared'].outputs,'Value'),z2)
        transverse=math_('polar_transverse','SQRT',transverse2)
        ratio=math_('polar_vertical_ratio','DIVIDE',z2,math_('polar_safe_transverse_squared','MAXIMUM',transverse2,_EPSILON*_EPSILON))
        # Fixed profile constant, not a new artist knob. No epsilon hard switch:
        # front/back approach zero continuously, while pure top/bottom remain1.
        t=math_('polar_front_back_t','DIVIDE',transverse,FRONT_BACK_BLEND)
        clipped=math_('polar_front_back_max','MINIMUM',math_('polar_front_back_min','MAXIMUM',t,0.),1.)
        fade=math_('polar_front_back_smooth','MULTIPLY',math_('polar_front_back_squared','MULTIPLY',clipped,clipped),
            math_('polar_front_back_three_minus_two','SUBTRACT',3.,math_('polar_front_back_twice','MULTIPLY',2.,clipped)))
        vertical_weight=math_('polar_vertical_weight','MULTIPLY',ratio,fade)
        good=_socket(nodes['store_valid'].inputs,'Value').links[0].from_socket
        geometry=_socket(nodes['store_valid'].outputs,'Geometry')
        for key,value in (('vertical_side',vertical_side),('vertical_weight',vertical_weight)):
            store=node('store_'+key,'GeometryNodeStoreNamedAttribute',data_type='FLOAT',domain='POINT')
            _socket(store.inputs,'Name').default_value=fields[key]
            group.links.new(geometry,_socket(store.inputs,'Geometry'))
            group.links.new(safe('safe_'+key,value,good),_socket(store.inputs,'Value'))
            geometry=_socket(store.outputs,'Geometry')
        relink(geometry,_socket(nodes['output'].inputs,interface['output']))
        for index,value in enumerate(group.nodes):value.location=((index%8)*220,-(index//8)*220)
        return group,interface
    except Exception:
        if group.users==0:bpy.data.node_groups.remove(group)
        raise


def _build_group_direction(owner,*,continuity=False):
    """Extend the unchanged polar graph in the same formal carrier position."""
    import bpy
    from . import face_direction_keys
    group,interface=_build_group_polar(owner)
    try:
        profile=CONTINUITY_PROFILE if continuity else DIRECTION_PROFILE
        group[PROFILE_KEY]=profile
        fields=_attributes(owner,profile=profile)
        group[_FIELDS]=json.dumps(fields,sort_keys=True)
        group[_DIRECTION_DOCUMENT]=json.dumps(face_direction_keys.document(),sort_keys=True,allow_nan=False)
        group[_DIRECTION_SHA]=face_direction_keys.identity()['document_sha256']
        if continuity:
            group[_CONTINUITY_KEYS]=json.dumps(face_direction_keys.continuity_identity(),sort_keys=True,allow_nan=False)
        nodes={n.get(_NODE):n for n in group.nodes}
        def node(key,kind,**props):
            result=group.nodes.new(kind);result[_NODE]=key;result.label=key
            for name,value in props.items():setattr(result,name,value)
            return result
        def relink(value,socket):
            for old in list(socket.links):group.links.remove(old)
            group.links.new(value,socket)
        combine=node('direction_head_components','ShaderNodeCombineXYZ')
        for axis,key in (('X','light_right'),('Y','light_forward'),('Z','polar_light_up')):
            group.links.new(_socket(nodes[key].outputs,'Value'),_socket(combine.inputs,axis))
        unit=node('direction_unit_head','ShaderNodeVectorMath',operation='NORMALIZE')
        group.links.new(_socket(combine.outputs,'Vector'),unit.inputs[0])
        if continuity:
            values=face_direction_keys.build(group,_socket(unit.outputs,'Vector'),node_key=_NODE,continuity=True)
        else:values=face_direction_keys.build(group,_socket(unit.outputs,'Vector'),node_key=_NODE)
        old_good=_socket(nodes['store_valid'].inputs,'Value').links[0].from_socket
        good_node=node('direction_valid','FunctionNodeBooleanMath',operation='AND')
        group.links.new(old_good,_socket(good_node.inputs,'Boolean'))
        group.links.new(values['found'],_socket(good_node.inputs,'Boolean_001'))
        good=_socket(good_node.outputs,'Boolean')
        # A missing cone is invalid, not a silent zero corrective weight.
        # All existing fallback predicates retain their v2 meaning.
        for key in ('safe_threshold','safe_phase','safe_mix','safe_vertical_side','safe_vertical_weight'):
            relink(good,_socket(nodes[key].inputs,'Switch'))
        relink(good,_socket(nodes['store_valid'].inputs,'Value'))
        geometry=_socket(nodes['store_vertical_weight'].outputs,'Geometry')
        for key in ('direction_upper','direction_top'):
            safe=node('safe_'+key,'GeometryNodeSwitch',input_type='FLOAT')
            group.links.new(good,_socket(safe.inputs,'Switch'))
            _socket(safe.inputs,'False').default_value=0.
            group.links.new(values[key],_socket(safe.inputs,'True'))
            store=node('store_'+key,'GeometryNodeStoreNamedAttribute',data_type='FLOAT',domain='POINT')
            _socket(store.inputs,'Name').default_value=fields[key]
            group.links.new(geometry,_socket(store.inputs,'Geometry'))
            group.links.new(_socket(safe.outputs,'Output'),_socket(store.inputs,'Value'))
            geometry=_socket(store.outputs,'Geometry')
        if continuity:
            safe=node('safe_direction_pair','GeometryNodeSwitch',input_type='VECTOR')
            group.links.new(good,_socket(safe.inputs,'Switch'))
            _socket(safe.inputs,'False').default_value=(0.,0.,0.)
            group.links.new(values['direction_pair'],_socket(safe.inputs,'True'))
            store=node('store_direction_pair','GeometryNodeStoreNamedAttribute',data_type='FLOAT_VECTOR',domain='POINT')
            _socket(store.inputs,'Name').default_value=fields['direction_pair']
            group.links.new(geometry,_socket(store.inputs,'Geometry'))
            group.links.new(_socket(safe.outputs,'Output'),_socket(store.inputs,'Value'))
            geometry=_socket(store.outputs,'Geometry')
        relink(geometry,_socket(nodes['output'].inputs,interface['output']))
        for index,value in enumerate(group.nodes):value.location=((index%8)*220,-(index//8)*220)
        return group,interface
    except Exception:
        if group.users==0:bpy.data.node_groups.remove(group)
        raise


def build(target,reference,light,left_attribute,right_attribute,weight_attribute,*,strength=0.0,side_blend=.05,vertical_fade=.1,profile=PROFILE):
    import bpy
    for value in target.modifiers:
        if _family(value,PROFILE_KEY,'toon-face-threshold.v'):raise FaceError('A face-field profile already occupies this target; no implicit replacement')
    owner=uuid.uuid4().hex;fields=_attributes(owner,profile=profile)
    _preflight(target,reference,light,left_attribute,right_attribute,weight_attribute,strength,side_blend=side_blend,vertical_fade=vertical_fade,fields=fields,profile=profile)
    before=list(target.modifiers);insertion=next((i for i,m in enumerate(before) if (m.show_viewport or m.show_render) and _tail(m)),len(before))
    _downstream(target,insertion);group=modifier=None
    try:
        group,interface=_build_group(owner,profile=profile);modifier=target.modifiers.new('Toon Face Threshold Fields','NODES');modifier.node_group=group
        group[_TARGET],group[_UID]=target,modifier.persistent_uid;group[_SIGNATURE]=graph_signature(group)
        if insertion<len(before):
            with bpy.context.temp_override(object=target,active_object=target,selected_objects=[target],selected_editable_objects=[target]):
                if bpy.ops.object.modifier_move_to_index(modifier=modifier.name,index=insertion)!={'FINISHED'}:raise FaceError('Could not place face fields before shading/camera modifiers')
        configure(target,modifier,reference=reference,light=light,left_attribute=left_attribute,right_attribute=right_attribute,weight_attribute=weight_attribute,strength=strength,side_blend=side_blend,vertical_fade=vertical_fade)
        return modifier
    except Exception:
        if modifier is not None:target.modifiers.remove(modifier)
        if group is not None and group.users==0:bpy.data.node_groups.remove(group)
        raise


def configure(target,modifier,*,reference=_UNCHANGED,light=_UNCHANGED,left_attribute=_UNCHANGED,right_attribute=_UNCHANGED,weight_attribute=_UNCHANGED,strength=_UNCHANGED,side_blend=_UNCHANGED,vertical_fade=_UNCHANGED):
    group,interface,fields=_owned(target,modifier)
    requested=dict(reference=reference,light=light,left=left_attribute,right=right_attribute,weight=weight_attribute,strength=strength,side_blend=side_blend,vertical_fade=vertical_fade)
    parameters={key:_input(modifier,interface,key) for key in requested}
    original={key:parameter.value for key,parameter in parameters.items()}
    values={key:original[key] if value is _UNCHANGED else value for key,value in requested.items()}
    values['strength'],_,_=_preflight(target,values['reference'],values['light'],values['left'],values['right'],values['weight'],values['strength'],side_blend=values['side_blend'],vertical_fade=values['vertical_fade'],fields=fields,profile=group.get(PROFILE_KEY))
    controlled=artistic._animated_paths(target)
    if any(parameters[key].path_from_id('value') in controlled and value!=original[key] for key,value in values.items()):
        raise FaceError('A face-field input is animated/driven; edit native animation explicitly')
    relation_changed=any(values[key]!=original[key] for key in ('reference','light'))
    try:
        for key,value in values.items():parameters[key].value=value
        # Updating Object socket values alone does not rebuild their native
        # dependency relations in Blender 5.2. Reassign the same group ID once
        # after explicit ID changes; keep its interface, inputs and animation.
        if relation_changed:modifier.node_group=group
        target.update_tag();return inspect(target,modifier,evaluate=False)
    except Exception as error:
        failures=[]
        for key,value in original.items():
            try:_input(modifier,interface,key).value=value
            except Exception as rollback_error:failures.append(key+':'+str(rollback_error))
        try:
            if relation_changed:modifier.node_group=group
            target.update_tag()
        except Exception as rollback_error:failures.append(str(rollback_error))
        if failures:raise FaceError('Face configure failed; rollback also failed: '+'; '.join(failures)) from error
        raise


def _readers(group):
    import bpy
    fields=json.loads(group[_FIELDS]);result=[]
    for value in bpy.data.node_groups:
        if value.bl_idname!='ShaderNodeTree':continue
        try:matching_fields=json.loads(value.get(_FIELDS,''))==fields
        except (TypeError,ValueError):matching_fields=False
        sdf_match=(value.get('toon_face_sdf_target')==group[_TARGET]
                   and value.get('toon_face_sdf_modifier_uid')==group[_UID])
        cast_match=False
        if value.get('toon_sdf_cast_profile')=='toon-face-sdf-cast-reader.v1':
            try:cast_match=json.loads(value.get('toon_sdf_cast_face','')).get('owner')==group[OWNER]
            except (TypeError,ValueError):pass
        art_match=(value.get('toon_face_cast_art_target')==group[_TARGET]
                   and value.get('toon_face_cast_art_modifier_uid')==group[_UID])
        if (art_match or value.get(OWNER)==group[OWNER] or (value.get(_TARGET)==group[_TARGET] and value.get(_UID)==group[_UID]) or matching_fields or sdf_match or cast_match):result.append(value)
    return result


def switch_profile(target,modifier,profile):
    """Explicit unbound static carrier switch; retain modifier UID/owner/sources."""
    import bpy
    if profile not in (PROFILE,PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE):raise FaceError('Unknown explicit face-field destination profile')
    old,interface,fields=_owned(target,modifier);old_profile=old[PROFILE_KEY];owner=old[OWNER]
    keys=('reference','light','left','right','weight','strength','side_blend','vertical_fade')
    original={key:_input(modifier,interface,key).value for key in keys}
    _preflight(target,original['reference'],original['light'],original['left'],original['right'],original['weight'],
        original['strength'],side_blend=original['side_blend'],vertical_fade=original['vertical_fade'],fields=fields,profile=old_profile)
    if _readers(old):raise FaceError('Unbind and explicitly remove every face reader, including unused retained readers, before switching profile')
    # A cast GeometryNodeTree sits BEFORE the face modifier and may have no
    # shader reader yet; _readers alone does not establish its absence.
    for group in bpy.data.node_groups:
        if group.get('toon_sdf_cast_profile') is None:continue
        raw=group.get('toon_sdf_cast_face')
        try:state=json.loads(raw) if raw is not None else None
        except (ValueError,TypeError):state=None
        same_target=group.get('toon_sdf_cast_target')==target
        if ((type(state)is dict and (state.get('owner')==owner
                or (same_target and state.get('modifier_uid')==modifier.persistent_uid)))
                or (state is None and same_target)):
            raise FaceError('A retained or referenced cast carrier still depends on this face profile')
    trees=[g for g in bpy.data.node_groups if g.bl_idname=='ShaderNodeTree']
    trees.extend(m.node_tree for m in bpy.data.materials if m.node_tree is not None)
    for tree in trees:
        if any(n.bl_idname=='ShaderNodeAttribute' and n.attribute_name in fields.values() for n in tree.nodes):
            raise FaceError('An unverified Shader Attribute consumer still reads this face carrier: '+tree.name)
    paths={_input(modifier,interface,key).path_from_id('value') for key in keys}
    prefix=modifier.path_from_id()+'.properties.inputs'
    legacy_inputs={modifier.path_from_id()+'['+json.dumps(interface[key])+']' for key in keys}
    controlled=set(artistic._animated_paths(target));animation=target.animation_data
    if animation is not None:
        actions=([animation.action] if animation.action else [])
        for track in animation.nla_tracks:actions.extend(strip.action for strip in track.strips if strip.action)
        for action in actions:
            if getattr(action,'is_action_legacy',False):controlled.update(curve.data_path for curve in action.fcurves)
    if any(path in paths or path.startswith(prefix) or path in legacy_inputs for path in controlled):
        raise FaceError('Profile switching cannot rebuild any native animated/driven modifier input')
    if profile==old_profile:
        report=inspect(target,modifier,evaluate=False)
        if report['status']!='valid':raise FaceError('; '.join(report['issues']))
        report['profile_switch']=dict(status='unchanged',previous_profile=old_profile,profile=profile,
            vertical_fade=original['vertical_fade'],writes_performed=False)
        return report
    requested=dict(original);requested['vertical_fade']=0. if profile in (PROFILE_V2,DIRECTION_PROFILE,CONTINUITY_PROFILE) else .1
    # The qualified NodeSocketFloat instance inputs store IEEE754 binary32.
    # Preserve exact comparisons against their representable requested value,
    # rather than rejecting the intentional v1 0.1 reset as a Python-f64 delta.
    for key in ('strength','side_blend','vertical_fade'):
        requested[key]=struct.unpack('<f',struct.pack('<f',_strength(requested[key])))[0]
    new_fields=_attributes(owner,profile=profile)
    _preflight(target,requested['reference'],requested['light'],requested['left'],requested['right'],requested['weight'],
        requested['strength'],side_blend=requested['side_blend'],vertical_fade=requested['vertical_fade'],fields=new_fields,profile=profile)
    # All admission above precedes group creation. The original remains alive
    # until the new graph and its actual evaluated fields have been accepted.
    existing_groups={g.as_pointer() for g in bpy.data.node_groups}
    original_modifiers=list(target.modifiers);original_uid=modifier.persistent_uid;new=None
    try:
        new,new_interface=_build_group(owner,profile=profile)
        new[_TARGET],new[_UID]=target,original_uid;new[_SIGNATURE]=graph_signature(new)
        modifier.node_group=new
        # New socket identifiers are instance-local: restore by semantic role.
        configured=configure(target,modifier,reference=requested['reference'],light=requested['light'],
            left_attribute=requested['left'],right_attribute=requested['right'],weight_attribute=requested['weight'],
            strength=requested['strength'],side_blend=requested['side_blend'],vertical_fade=requested['vertical_fade'])
        if configured['status']!='valid':raise FaceError('; '.join(configured['issues']))
        report=inspect(target,modifier,evaluate=True)
        if report['status']!='valid':raise FaceError('; '.join(report['issues']))
        actual_inputs={key:_input(modifier,new_interface,key).value for key in keys}
        input_checks={key:actual_inputs[key]==value for key,value in requested.items()}
        switch_checks=dict(modifier_uid_exact=modifier.persistent_uid==original_uid,
            modifier_sequence_exact=list(target.modifiers)==original_modifiers,owner_exact=new[OWNER]==owner,
            fields_exact=report['attributes']==new_fields,old_graph_users_zero=old.users==0,
            input_values_exact=all(input_checks.values()))
        if not all(switch_checks.values()):
            differences={key:dict(actual=repr(actual_inputs[key]),expected=repr(requested[key]))
                         for key,exact in input_checks.items() if not exact}
            raise FaceError('Face profile switch validation failed: '+json.dumps(dict(checks=switch_checks,
                input_checks=input_checks,input_differences=differences,old_graph_users=old.users,
                expected_modifier_uid=original_uid,actual_modifier_uid=modifier.persistent_uid),sort_keys=True))
        target.update_tag()
        report['profile_switch']=dict(status='switched',previous_profile=old_profile,profile=profile,
            previous_vertical_fade=original['vertical_fade'],vertical_fade=requested['vertical_fade'],
            vertical_fade_policy='Explicitly reset to0 for continuity v4' if profile==CONTINUITY_PROFILE else 'Explicitly reset to0 for direction-key v3' if profile==DIRECTION_PROFILE else 'Explicitly reset to0 for polar v2 or0.1 for azimuth v1',
            float_input_policy='Exact requested IEEE754 binary32, no tolerance; rollback retains original RNA values',
            validation=switch_checks,input_checks=input_checks,
            modifier_uid_preserved=True,owner_preserved=True)
        # Commit point: no fallible processing follows deletion of the old ID.
        bpy.data.node_groups.remove(old)
        return report
    except Exception as error:
        failures=[]
        try:modifier.node_group=old
        except Exception as rollback_error:failures.append('old graph: '+str(rollback_error))
        if modifier.node_group==old:
            for key,value in original.items():
                try:_input(modifier,interface,key).value=value
                except Exception as rollback_error:failures.append(key+': '+str(rollback_error))
            try:
                modifier.node_group=old;target.update_tag()
                _owned(target,modifier)
                if any(_input(modifier,interface,key).value!=value for key,value in original.items()):
                    raise FaceError('Original input values did not restore exactly')
            except Exception as rollback_error:failures.append('original validation: '+str(rollback_error))
        # Include an owned partial group if construction failed before returning.
        created=[g for g in bpy.data.node_groups if g.as_pointer() not in existing_groups and g.get(OWNER)==owner]
        for group in created:
            try:
                if group.users!=0:raise FaceError('New face graph still has users; refusing destructive cleanup')
                bpy.data.node_groups.remove(group)
            except Exception as rollback_error:failures.append('new graph cleanup: '+str(rollback_error))
        if failures:raise FaceError('Face profile switch failed; rollback also failed: '+'; '.join(failures)) from error
        raise


def _reader_edited(reader):
    try:
        if reader.get('toon_face_sdf_profile') is not None:
            from . import face_sdf
            return face_sdf.reader_edited(reader)
        if reader.get('toon_sdf_cast_profile') is not None:
            from . import sdf_cast
            return sdf_cast.reader_edited(reader)
        if reader.get('toon_face_cast_art_profile') is not None:
            from . import face_cast_art
            return face_cast_art.reader_edited(reader)
        from . import face_material
        return face_material.reader_edited(reader)
    except (ImportError,ValueError,TypeError,RuntimeError,ReferenceError,KeyError,AttributeError):return True


def inspect(target,modifier,*,depsgraph=None,evaluate=True):
    group,interface,fields=_owned(target,modifier);values={key:_input(modifier,interface,key).value for key in ('reference','light','left','right','weight','strength','side_blend','vertical_fade')}
    def numeric(value):return float(value) if math.isfinite(value) else None
    profile=group.get(PROFILE_KEY)
    report=dict(profile=profile,status='valid',owner=group[OWNER],target=target.name,modifier_uid=modifier.persistent_uid,
        reference=values['reference'].name if values['reference'] else None,light=values['light'].name if values['light'] else None,
        left_attribute=values['left'],right_attribute=values['right'],weight_attribute=values['weight'],strength=numeric(values['strength']),
        side_blend=numeric(values['side_blend']),vertical_fade=numeric(values['vertical_fade']),attributes=fields,space=SPACE_CONTINUITY if profile==CONTINUITY_PROFILE else SPACE_DIRECTION if profile==DIRECTION_PROFILE else SPACE_V2 if profile==PROFILE_V2 else SPACE,issues=[],invalid_points=None,source_semantics='authored directional shadow onset, not a distance SDF or UV texture',
        readers=[dict(name=value.name,users=value.users,author_edited=_reader_edited(value)) for value in _readers(group)])
    if profile in (DIRECTION_PROFILE,CONTINUITY_PROFILE):
        from . import face_direction_keys
        report['direction_keys']=face_direction_keys.identity()
    if profile==CONTINUITY_PROFILE:
        report['continuity_keys']=face_direction_keys.continuity_identity()
    try:
        _,graph,frame=_preflight(target,values['reference'],values['light'],values['left'],values['right'],values['weight'],values['strength'],side_blend=values['side_blend'],vertical_fade=values['vertical_fade'],fields=fields,depsgraph=depsgraph,profile=profile)
        report['frame']=frame
    except FaceError as error:report.update(status='invalid',issues=[str(error)]);return report
    if evaluate:
        evaluated=target.evaluated_get(graph);mesh=evaluated.to_mesh()
        try:
            for name in (values['left'],values['right'],values['weight']):_float(mesh,name)
            for key,name in fields.items():
                attribute=mesh.attributes.get(name);kind='FLOAT_VECTOR' if key=='direction_pair' else 'BOOLEAN' if key=='valid' else 'FLOAT'
                if attribute is None or attribute.data_type!=kind or attribute.domain!='POINT' or len(attribute.data)!=len(mesh.vertices):raise FaceError('Face output is missing or has wrong type/domain: '+key)
                if key=='direction_pair':
                    if any(any(not math.isfinite(v) or not 0<=v<=1 for v in item.vector) or item.vector.z!=0. for item in attribute.data):
                        raise FaceError('Invalid packed continuity values or nonzero Z: '+key)
                elif key!='valid' and any(not math.isfinite(item.value) or not 0<=item.value<=1 for item in attribute.data):raise FaceError('Invalid face output values: '+key)
            invalid=sum(not item.value for item in mesh.attributes[fields['valid']].data)
            report.update(vertices=len(mesh.vertices),invalid_points=invalid,evaluation=graph.mode)
            if invalid:raise FaceError('Invalid face fields at %d points'%invalid)
            if profile in (DIRECTION_PROFILE,CONTINUITY_PROFILE):
                observed={}
                for role in ('direction_upper','direction_top'):
                    numbers=[item.value for item in mesh.attributes[fields[role]].data]
                    if numbers and min(numbers)!=max(numbers):
                        raise FaceError('Direction-key output must be uniform at every point: '+role)
                    observed[role]=dict(value=numbers[0] if numbers else None,
                        oracle=frame[role],maximum_oracle_error=abs(numbers[0]-frame[role]) if numbers else None)
                report['direction_evaluation']=observed
                if profile==CONTINUITY_PROFILE:
                    vectors=[tuple(item.vector) for item in mesh.attributes[fields['direction_pair']].data]
                    if vectors and any(value!=vectors[0] for value in vectors):
                        raise FaceError('Packed continuity output must be uniform at every point')
                    observed['direction_pair']=dict(value=list(vectors[0]) if vectors else None,
                        oracle=frame['direction_pair'],maximum_oracle_error=max(abs(a-b) for a,b in zip(vectors[0],frame['direction_pair'])) if vectors else None)
        except FaceError as error:report.update(status='invalid',issues=[str(error)])
        finally:evaluated.to_mesh_clear()
    return report


def validate(target,modifier,*,depsgraph=None):
    report=inspect(target,modifier,depsgraph=depsgraph)
    if report['status']!='valid':raise FaceError('; '.join(report['issues']))
    return report


def remove(target,modifier):
    import bpy
    group,interface,fields=_owned(target,modifier);readers=_readers(group)
    if any(_reader_edited(reader) for reader in readers):raise FaceError('Face reader has author edits or unverified identity; removal would discard work')
    if any(reader.users for reader in readers):raise FaceError('Face readers have consumers; unbind them explicitly first')
    # Removal alone performs the wider dependency scan: an edited/lost reader
    # identity must never hide actual Attribute consumers of these four fields.
    shader_trees=[tree for tree in bpy.data.node_groups if tree.bl_idname=='ShaderNodeTree']
    shader_trees.extend(material.node_tree for material in bpy.data.materials if material.node_tree is not None)
    for tree in shader_trees:
        if tree in readers:continue
        if any(node.bl_idname=='ShaderNodeAttribute' and node.attribute_name in fields.values() for node in tree.nodes):
            raise FaceError('Unverified Shader Attribute consumer still reads owned face fields: '+tree.name)
    controlled=artistic._animated_paths(target)
    if any(_input(modifier,interface,key).path_from_id('value') in controlled for key in ('reference','light','left','right','weight','strength','side_blend','vertical_fade')):
        raise FaceError('Removing this face field would orphan native input animation')
    owner=group[OWNER];profile=group.get(PROFILE_KEY);target.modifiers.remove(modifier)
    for reader in readers:bpy.data.node_groups.remove(reader)
    if group.users==0:bpy.data.node_groups.remove(group)
    return dict(profile=profile,status='removed',owner=owner,attributes=fields)
