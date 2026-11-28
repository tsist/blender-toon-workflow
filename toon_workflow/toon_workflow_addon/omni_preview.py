# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, editable-input Toon light/camera contact-sheet previews.

The UI submits a copy snapshot to an isolated Blender worker. ``run`` is the
same synchronous core used by that worker. Original materials/occlusion stay;
native Beauty is not formal Combined/Line or an artistic acceptance result.
"""
import hashlib
import importlib
import itertools
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid
import zlib

SCHEMA='toon-omni-preview.v1'
MAX_TILES=256
PAGE_SIZES=(9,16,36,64)
_processes={}


def _need(value,message):
    if not value:raise ValueError(message)


def _sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
def _read(path):return json.loads(Path(path).read_text('utf-8-sig'))


def _write(path,value):
    path=Path(path);temporary=path.with_name('.o-'+uuid.uuid4().hex[:12]+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),'utf8')
    for attempt in range(3):
        try:temporary.replace(path);return
        except PermissionError:
            if attempt==2:raise
            time.sleep(.02)


def preset(name):
    choices={
        'LIGHT_9':dict(light_yaws=[0,120,240],light_elevations=[-60,0,60],page_size=9),
        'LIGHT_16':dict(light_yaws=[0,90,180,270],light_elevations=[-60,-20,20,60],page_size=16),
        'LIGHT_36':dict(light_yaws=list(range(0,360,60)),light_elevations=[-75,-45,-15,15,45,75],page_size=36),
        'LIGHT_64':dict(light_yaws=list(range(0,360,45)),light_elevations=[-80,-60,-40,-20,20,40,60,80],page_size=64),
        'MULTI_16':dict(light_yaws=[0,90,180,270],light_elevations=[30],camera_yaws=[0,90,180,270],camera_elevations=[0],camera_mode='ORBIT',page_size=16)}
    _need(name in choices,'Unknown omni preset')
    return dict(camera_mode='SOURCE',camera_yaws=[0],camera_elevations=[0],**choices[name]) if name!='MULTI_16' else dict(choices[name])


def _number(value,label):
    _need(type(value) in (int,float) and math.isfinite(value),label+' must be finite numeric, never bool')
    return float(value)


def _angles(value,label,elevation=False):
    _need(type(value)is list and 1<=len(value)<=256,label+' requires 1..256 explicit angles')
    result=[]
    for item in value:
        angle=_number(item,label)
        if elevation:_need(-90<=angle<=90,label+' must be in [-90,90]')
        else:angle=angle%360.
        if angle not in result:result.append(angle)
    return result


def _directions(yaws,elevations):
    result=[];removed=[];seen={}
    for yaw,elevation in itertools.product(yaws,elevations):
        y,e=math.radians(yaw),math.radians(elevation)
        vector=(math.sin(y)*math.cos(e),math.cos(y)*math.cos(e),math.sin(e))
        key=(0. if abs(elevation)==90. else yaw,elevation)
        duplicate=seen.get(key)
        if duplicate:removed.append(dict(yaw=yaw,elevation=elevation,same_as=[duplicate['yaw'],duplicate['elevation']]))
        else:
            _need(len(result)<MAX_TILES,'More than 256 distinct directions; split this request explicitly')
            row=dict(yaw=yaw,elevation=elevation,unit_head=list(vector));result.append(row);seen[key]=row
    return result,removed


def normalize(document):
    _need(type(document)is dict,'Preview document must be an object')
    required={'schema','reference','sun','camera','output_directory','light_yaws','light_elevations','camera_yaws','camera_elevations'}
    optional={'camera_mode','tile_size','samples','page_size','orbit_distance','focus_offset'}
    _need(required<=set(document) and not(set(document)-required-optional),'Unknown/missing preview fields')
    _need(document['schema']==SCHEMA,'Unsupported omni preview schema')
    doc=dict(document)
    for key in ('reference','sun','camera'):
        _need(type(doc[key])is str and doc[key] and '\x00' not in doc[key],'Explicit source object name required: '+key)
    _need(type(doc['output_directory'])is str and Path(doc['output_directory']).is_absolute(),'New absolute output directory required')
    doc['camera_mode']=doc.get('camera_mode','SOURCE');_need(doc['camera_mode'] in ('SOURCE','ORBIT'),'Unknown camera mode')
    for key in ('light_yaws','camera_yaws'):doc[key]=_angles(doc[key],key)
    for key in ('light_elevations','camera_elevations'):doc[key]=_angles(doc[key],key,True)
    if doc['camera_mode']=='SOURCE':
        _need(doc['camera_yaws']==[0.] and doc['camera_elevations']==[0.],'SOURCE camera uses singleton [0]/[0]; use ORBIT for camera sweeps')
    for key,default,low,high in [('tile_size',256,64,1024),('samples',64,1,512)]:
        doc[key]=doc.get(key,default);_need(type(doc[key])is int and low<=doc[key]<=high,key+' exceeds the explicit resource range')
    doc['page_size']=doc.get('page_size',16);_need(type(doc['page_size'])is int and doc['page_size'] in PAGE_SIZES,'Page must have 9/16/36/64 slots')
    doc['orbit_distance']=_number(doc.get('orbit_distance',0.),'orbit_distance');_need(doc['orbit_distance']>=0,'Orbit distance must be nonnegative')
    offset=doc.get('focus_offset',[0.,0.,0.]);_need(type(offset)is list and len(offset)==3,'focus_offset must be [right,forward,up] in world units')
    doc['focus_offset']=[_number(v,'focus_offset') for v in offset]
    lights,light_removed=_directions(doc['light_yaws'],doc['light_elevations'])
    cameras,camera_removed=_directions(doc['camera_yaws'],doc['camera_elevations'])
    _need(len(lights)*len(cameras)<=MAX_TILES,'At most 256 rendered tiles; split this request explicitly')
    cases=[dict(index=i,light=light,camera=view,camera_mode=doc['camera_mode'])
        for i,(light,view) in enumerate(itertools.product(lights,cameras))]
    return doc,dict(cases=cases,tiles=len(cases),pages=math.ceil(len(cases)/doc['page_size']),
        duplicate_light_directions_removed=light_removed,duplicate_camera_directions_removed=camera_removed,
        convention='GS reference +X=right,-Y=forward,up=cross(forward,right); positive yaw turns forward to right; Sun +Z points toward light',
        resource_limits=dict(total_tiles=MAX_TILES,page_slots=64),quality_status='not_evaluated')


def _native(obj):
    return dict(location=list(obj.location),scale=list(obj.scale),rotation_mode=obj.rotation_mode,
        rotation_euler=list(obj.rotation_euler),rotation_quaternion=list(obj.rotation_quaternion),rotation_axis_angle=list(obj.rotation_axis_angle),
        delta_location=list(obj.delta_location),delta_scale=list(obj.delta_scale),delta_rotation_euler=list(obj.delta_rotation_euler),
        delta_rotation_quaternion=list(obj.delta_rotation_quaternion),matrix_world=[list(row) for row in obj.matrix_world])


def _restore_native(obj,value):
    _need(obj.rotation_mode==value['rotation_mode'],'Native rotation mode changed unexpectedly')
    for key in ('location','scale','delta_location','delta_scale','delta_rotation_euler','delta_rotation_quaternion',
                'rotation_quaternion','rotation_axis_angle','rotation_euler'):setattr(obj,key,value[key])
    active='rotation_quaternion' if value['rotation_mode']=='QUATERNION' else 'rotation_axis_angle' if value['rotation_mode']=='AXIS_ANGLE' else 'rotation_euler'
    setattr(obj,active,value[active])


def _basis(matrix):
    from mathutils import Vector
    right=(matrix.to_3x3()@Vector((1,0,0))).normalized()
    forward=matrix.to_3x3()@Vector((0,-1,0));forward-=right*forward.dot(right)
    _need(forward.length>1e-8,'Reference forward/right axes are degenerate')
    forward.normalize();return right,forward,forward.cross(right).normalized()


def _world_direction(angles,basis):
    return sum((axis*value for axis,value in zip(basis,angles['unit_head'])),basis[0]*0.).normalized()


def _managed_guard(scene):
    import numpy as np
    from . import hair_map,face_sdf,eye_overlays
    result=dict(hair=[],face=[],eye=eye_overlays.collect(scene),slots=[],uv={})
    for obj in scene.objects:
        if obj.type!='MESH':continue
        protected=False
        for index,slot in enumerate(obj.material_slots):
            material=slot.material
            result['slots'].append([obj.name,index,slot.link,material.name if material else None,str(material.as_pointer()) if material else None])
            if material is None:continue
            if hair_map.present(material):
                result['hair'].append(dict(object=obj.name,slot=index,descriptor=hair_map.describe(scene,obj,index)));protected=True
            if material.node_tree:
                for node in material.node_tree.nodes:
                    if node.get('toon_face_sdf_binding'):
                        described=face_sdf.inspect_reader(node.node_tree,target=obj)
                        _need(described['status']=='valid','Source face SDF is invalid')
                        descriptor={k:described[k] for k in ('image','uv','channel','mirror','sampling','shader','binding')}
                        if described['profile'] in (face_sdf.POLAR_PROFILE,face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                            descriptor.update(vertical_source=described['vertical_source'],direction_encoding=described['direction_encoding'])
                        if described['profile'] in (face_sdf.DIRECTION_PROFILE,face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                            descriptor.update(direction_source=described['direction_source'],
                                direction_correction=described['direction_correction'])
                        if described['profile'] in (face_sdf.CONTINUITY_PROFILE,face_sdf.AUTHORED_PROFILE):
                            descriptor.update(pair_source=described['pair_source'],continuity_keys=described['continuity_keys'])
                        result['face'].append(dict(object=obj.name,slot=index,descriptor=descriptor));protected=True
        if protected:
            layers={}
            for uv in obj.data.uv_layers:
                values=np.empty(len(uv.data)*2,np.float32);uv.data.foreach_get('uv',values)
                layers[uv.name]=dict(sha256=hashlib.sha256(values.tobytes()).hexdigest(),active=uv==obj.data.uv_layers.active,render=bool(uv.active_render))
            result['uv'][obj.name]=layers
    from . import component_slots
    result['components']=component_slots.snapshot(scene)
    return result


def _sample_fields(evaluated,graph,names):
    mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
    try:
        result={}
        for key,name in names.items():
            attr=mesh.attributes.get(name)
            expected=('FLOAT_VECTOR',) if key=='direction_pair' else ('FLOAT','BOOLEAN')
            _need(attr is not None and attr.domain=='POINT' and attr.data_type in expected and len(attr.data)>0,'Actual preview carrier field missing: '+name)
            indices=sorted({0,len(attr.data)//2,len(attr.data)-1})
            if key=='direction_pair':
                values=[list(attr.data[index].vector) for index in indices]
                _need(all(all(math.isfinite(v) for v in row) and all(-2e-6<=v<=1+2e-6 for v in row[:2]) and row[2]==0. for row in values),'Invalid actual packed face key sample')
                result[key]=dict(attribute=name,domain='POINT',data_type='FLOAT_VECTOR',count=len(attr.data),indices=indices,values=values)
                continue
            values=[float(attr.data[index].value) for index in indices]
            low,high=(-math.pi/2,math.pi/2) if key=='elevation' else (0.,1.)
            _need(all(math.isfinite(v) and low-2e-6<=v<=high+2e-6 for v in values),'Invalid actual preview gate sample')
            result[key]=dict(attribute=name,domain='POINT',count=len(attr.data),indices=indices,values=values)
        return result
    finally:evaluated.to_mesh_clear()


def _witness(scene,layer,camera,sun,reference,guard,expected_light,expected_camera,expected_reference):
    import numpy as np
    from . import joint_render,hair_map,hair_sdf_nodes,face_thresholds
    def capture(graph,override):
        selected=override if override is not None else graph.scene_eval.camera
        _need(graph.mode=='RENDER' and selected is not None and getattr(selected,'original',selected)==camera,'Actual RENDER camera does not match preview camera')
        visible={i.object.original for i in graph.object_instances if i.show_self and i.object.type=='LIGHT' and not i.object.hide_render and i.object.data.energy>0}
        _need(visible=={sun},'Preview requires exactly its single active Sun; no other lights are hidden automatically')
        ev_camera=camera.evaluated_get(graph);ev_sun=sun.evaluated_get(graph);ev_ref=reference.evaluated_get(graph)
        _need(np.allclose(ev_ref.matrix_world,expected_reference,atol=2e-6,rtol=0),'Head reference moved during the orbit; reject camera/reference feedback')
        _need(np.allclose(ev_camera.matrix_world,expected_camera,atol=2e-6,rtol=0),'Actual camera matrix differs from planned orbit/source')
        direction=(ev_sun.matrix_world.to_3x3()@expected_light.__class__((0,0,1))).normalized()
        _need(direction.dot(expected_light)>1-2e-6,'Actual Sun direction differs from planned light')
        basis=_basis(ev_ref.matrix_world);horizontal=math.hypot(direction.dot(basis[0]),direction.dot(basis[1]))
        hair=[]
        for row in guard['hair']:
            descriptor=row['descriptor'].get('sdf')
            if descriptor is None:continue
            obj=scene.objects[row['object']];group=obj.material_slots[row['slot']].material[hair_map.KEY]
            actual_settings=hair_sdf_nodes.shader_parameters(group.evaluated_get(graph))
            _need(actual_settings==descriptor['settings'],'Actual SDF parameters changed during preview')
            fields=_sample_fields(obj.evaluated_get(graph),graph,descriptor['carrier']['attributes'])
            child_reference=scene.objects[descriptor['reference']['name']].evaluated_get(graph)
            _need(descriptor['profile'] in ('toon-hair-sdf.v1','toon-hair-sdf.v2'),'Unsupported prevalidated hair direction profile')
            oracle=hair_sdf_nodes.direction_frame(child_reference.matrix_world,ev_sun.matrix_world,mode='omni_v2') if descriptor['profile']=='toon-hair-sdf.v2' else hair_sdf_nodes.direction_frame(child_reference.matrix_world,ev_sun.matrix_world)
            errors={}
            for key,field in fields.items():
                delta=[abs(v-oracle[key]) for v in field['values']]
                if key=='phase':delta=[min(v,abs(1-v)) for v in delta]
                errors[key]=max(delta);_need(errors[key]<=2e-5,'Actual sampled hair direction gate differs from its oracle')
            oracle_keys=('phase','valid','horizontal','elevation') if descriptor['profile']=='toon-hair-sdf.v2' else ('phase','valid','horizontal')
            hair.append(dict(object=obj.name,profile=descriptor['profile'],fields=fields,oracle={k:oracle[k] for k in oracle_keys},sample_max_error=errors,settings=actual_settings,
                elevation_units='radians' if descriptor['profile']=='toon-hair-sdf.v2' else None))
        face=[]
        for name in sorted({r['object'] for r in guard['face']}):
            obj=scene.objects[name]
            for modifier in obj.modifiers:
                if face_thresholds.is_managed(modifier):
                    report=face_thresholds.inspect(obj,modifier,depsgraph=graph,evaluate=False)
                    _need(report['status']=='valid','Actual face carrier preflight failed')
                    face.append(dict(object=name,profile=report['profile'],frame=report['frame'],fields=_sample_fields(obj.evaluated_get(graph),graph,report['attributes'])))
        projection=ev_camera.calc_matrix_camera(graph,x=scene.render.resolution_x,y=scene.render.resolution_y,
            scale_x=scene.render.pixel_aspect_x,scale_y=scene.render.pixel_aspect_y)
        return dict(mode='RENDER',frame=graph.scene_eval.frame_current,subframe=float(graph.scene_eval.frame_subframe),camera=camera.name,
            camera_world=[list(r) for r in ev_camera.matrix_world],projection=[list(r) for r in projection],sun_world=[list(r) for r in ev_sun.matrix_world],
            reference_world=[list(r) for r in ev_ref.matrix_world],world_L=list(direction),head_elevation_degrees=math.degrees(math.atan2(direction.dot(basis[2]),horizontal)),
            head_yaw_degrees=(math.degrees(math.atan2(direction.dot(basis[0]),direction.dot(basis[1])))%360) if horizontal>1e-6 else None,
            hair=hair,face=face,field_scope='Three actual POINT samples per field; not a full-array freeze or artistic test')
    return joint_render._run_render_callback(scene,layer,capture)


def _save_display(scene,pixels,path):
    import bpy
    import numpy as np
    size=pixels.shape[0];image=bpy.data.images.new('Toon omni display writer',width=size,height=size,alpha=True,float_buffer=True)
    try:
        image.colorspace_settings.name='Linear Rec.709'
        rgba=pixels.copy();rgba[:,:,:3]+=(1-rgba[:,:,3:4])*.18;rgba[:,:,3]=1.
        image.pixels.foreach_set(rgba.ravel())
        fmt=scene.render.image_settings;fmt.file_format='PNG';fmt.color_mode='RGBA';fmt.color_depth='8';fmt.color_management='FOLLOW_SCENE'
        image.save_render(str(path),scene=scene)
    finally:bpy.data.images.remove(image)


def _display_bytes(path):
    import bpy
    import numpy as np
    image=bpy.data.images.load(str(path),check_existing=False)
    try:
        image.colorspace_settings.name='Non-Color';image.alpha_mode='CHANNEL_PACKED'
        w,h=image.size;values=np.empty(w*h*4,np.float32);image.pixels.foreach_get(values)
        return np.rint(np.clip(values.reshape(h,w,4),0,1)*255).astype(np.uint8)[::-1].copy()
    finally:bpy.data.images.remove(image)


_FONT={
 '0':['111','101','101','101','111'],'1':['010','110','010','010','111'],'2':['111','001','111','100','111'],
 '3':['111','001','111','001','111'],'4':['101','101','111','001','001'],'5':['111','100','111','001','111'],
 '6':['111','100','111','101','111'],'7':['111','001','001','001','001'],'8':['111','101','111','101','111'],
 '9':['111','101','111','001','111'],'L':['100','100','100','100','111'],'C':['111','100','100','100','111'],
 'S':['111','100','111','001','111'],'R':['110','101','110','101','101'],'+':['000','010','111','010','000'],
 '-':['000','000','111','000','000'],'.':['000','000','000','000','010'],'/':['001','001','010','100','100'],
 '#':['101','111','101','111','101'],':':['000','010','000','010','000'],
 'e':['000','111','111','100','111']}


def _text(canvas,x,y,text,scale,*,clip_right=None):
    right=min(canvas.shape[1],clip_right if clip_right is not None else canvas.shape[1])
    for char in text:
        if x>=right:break
        glyph=_FONT.get(char,['000']*5)
        for row,line in enumerate(glyph):
            for col,bit in enumerate(line):
                left=x+col*scale
                if bit=='1' and left<right:
                    canvas[y+row*scale:y+(row+1)*scale,left:min(x+(col+1)*scale,right),:3]=235
        x+=4*scale


def _png_bytes(path,rgba):
    """Write already display-referred RGBA bytes without a second view transform."""
    def chunk(handle,kind,data):handle.write(struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff))
    h,w,_=rgba.shape
    with Path(path).open('xb') as handle:
        handle.write(b'\x89PNG\r\n\x1a\n');chunk(handle,b'IHDR',struct.pack('>IIBBBBB',w,h,8,6,0,0,0));chunk(handle,b'sRGB',b'\x00')
        compressor=zlib.compressobj(6)
        for row in rgba:
            data=compressor.compress(b'\0'+row.tobytes())
            if data:chunk(handle,b'IDAT',data)
        chunk(handle,b'IDAT',compressor.flush());chunk(handle,b'IEND',b'')


def _sheet(frames,path,slots,tile):
    import numpy as np
    cols=int(math.sqrt(slots));scale=1 if tile<128 else 2;bar=18*scale;cell=tile+2
    canvas=np.full((cols*(tile+bar+2),cols*cell,4),32,np.uint8);canvas[:,:,3]=255
    index=[]
    for i,frame in enumerate(frames):
        pixels=_display_bytes(frame['png']);_need(pixels.shape==(tile,tile,4),'Decoded tile PNG extent differs')
        x=(i%cols)*cell+1;y=(i//cols)*(tile+bar+2)+1
        canvas[y:y+tile,x:x+tile]=pixels
        case=frame['case'];light=case['light'];view=case['camera']
        labels=['#'+str(case['index']+1),'L'+format(light['yaw'],'.4g')+'/'+format(light['elevation'],'.4g'),
                'C:SRC' if case['camera_mode']=='SOURCE' else 'C'+format(view['yaw'],'.4g')+'/'+format(view['elevation'],'.4g')]
        for line,label in enumerate(labels):_text(canvas,x+2,y+tile+line*6*scale,label,scale,clip_right=x+tile)
        index.append(dict(tile=case['index'],cell=[i%cols,i//cols],labels=labels,png=frame['png'],png_sha256=frame['png_sha256']))
    _png_bytes(path,canvas)
    return dict(file=str(path),sha256=_sha(path),size=[canvas.shape[1],canvas.shape[0]],tiles=index,
        color='Source-view-transformed PNG display bytes; Non-Color decode, one byte collage, no second OCIO transform',
        label_precision='Four significant digits in image labels; full angles preserved in JSON')


class PreviewCancelled(RuntimeError):pass


def _has_effective_animation(obj):
    data=obj.animation_data
    return data is not None and (data.action is not None or len(data.drivers)>0 or any(len(track.strips)>0 for track in data.nla_tracks))


def _scalar_rna(owner,*,writable_only=False,exclude=()):
    """Read actual-build RNA fields instead of guessing AnimData setters."""
    result={}
    for prop in owner.bl_rna.properties:
        if prop.identifier=='rna_type' or prop.identifier in exclude or prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:continue
        if writable_only and prop.is_readonly:continue
        value=getattr(owner,prop.identifier)
        result[prop.identifier]=list(value) if getattr(prop,'is_array',False) else sorted(value) if isinstance(value,set) else value
    return result


def _curve_data(curve):
    return dict(fields=_scalar_rna(curve,exclude=('is_valid','is_empty')),
        group=curve.group.name if curve.group else None,
        keys=[_scalar_rna(point) for point in curve.keyframe_points],
        samples=[list(point.co) for point in curve.sampled_points],
        modifiers=[dict(fields=_scalar_rna(modifier),control_points=[_scalar_rna(point) for point in modifier.control_points]
            if hasattr(modifier,'control_points') else []) for modifier in curve.modifiers])


def _action_data(action):
    if action is None:return None
    layers=[]
    for layer in action.layers:
        strips=[]
        for strip in layer.strips:
            bags=[dict(slot_handle=bag.slot_handle,curves=[_curve_data(curve) for curve in bag.fcurves])
                  for bag in getattr(strip,'channelbags',())]
            strips.append(dict(fields=_scalar_rna(strip),channelbags=bags))
        layers.append(dict(fields=_scalar_rna(layer),strips=strips))
    legacy=[_curve_data(curve) for curve in action.fcurves] if not layers and getattr(action,'is_action_legacy',False) else []
    return dict(name=action.name,identity=str(action.as_pointer()),fields=_scalar_rna(action,writable_only=True,exclude=('tag',)),
        slots=[dict(handle=slot.handle,identifier=slot.identifier,target_id_type=slot.target_id_type) for slot in action.slots],
        layers=layers,legacy_curves=legacy)


def _action_hash(value):
    # Infinite strip bounds can be a native range convention. Encode them
    # explicitly for the witness without editing or quantizing Action data.
    def safe(item):
        if isinstance(item,float) and not math.isfinite(item):return dict(native_float=item.hex())
        if isinstance(item,list):return [safe(v) for v in item]
        if isinstance(item,dict):return {k:safe(v) for k,v in item.items()}
        return item
    return hashlib.sha256(_encode(safe(value)).encode()).hexdigest()


def _camera_animation(camera):
    ad=camera.animation_data
    if ad is None:return dict(container=None,action=None,slot=None,handle=None,fields={},data=None)
    _need(len(ad.drivers)==0 and not ad.use_tweak_mode,'Camera drivers or NLA tweak mode require a separate preview contract')
    active_tracks=[track for track in ad.nla_tracks if not track.mute]
    solo=[track for track in active_tracks if track.is_solo]
    if solo:active_tracks=solo
    _need(not ad.use_nla or not any(not strip.mute for track in active_tracks for strip in track.strips),
          'Camera effective NLA strips are not supported by the static-frame orbit profile')
    return dict(container=ad,action=ad.action,slot=ad.action_slot,handle=ad.action_slot_handle,
        fields=_scalar_rna(ad,writable_only=True,exclude=('action_slot_handle',)),data=_action_data(ad.action))


def _animation_guard(camera,state,current_action_data):
    ad=camera.animation_data
    if state['container'] is None:return ad is None
    return (ad==state['container'] and ad.action==state['action'] and ad.action_slot==state['slot']
        and ad.action_slot_handle==state['handle'] and _scalar_rna(ad,writable_only=True,exclude=('action_slot_handle',))==state['fields']
        and current_action_data==state['data'])


def _restore_camera_animation(camera,state):
    ad=camera.animation_data
    _need(ad is not None and ad==state['container'],'Camera animation container changed; do not clear/recreate it')
    # This is the verified 5.2 binding order used by finish_animation._restore_ad.
    ad.action=state['action']
    if state['action'] is not None:ad.action_slot=state['slot']
    for key,value in state['fields'].items():setattr(ad,key,value)
    _need(ad.action==state['action'] and ad.action_slot==state['slot'] and ad.action_slot_handle==state['handle'],
          'Original camera Action/slot/handle did not rebind exactly')


def _camera_render_matrix(scene,layer,camera):
    from . import joint_render
    def capture(graph,override):
        _need(graph.mode=='RENDER','Camera baseline needs actual RENDER evaluation')
        return [list(row) for row in camera.evaluated_get(graph).matrix_world]
    return joint_render._run_render_callback(scene,layer,capture)


def _source_linear_name(scene):
    # Blender 5.2 can return an invalid empty enum for a new Scene. Native RNA
    # cannot restore it after choosing EXR, including via property_unset.
    # Reject before mkdir/snapshot/render changes instead of normalizing input.
    name=scene.render.image_settings.linear_colorspace_settings.name
    _need(isinstance(name,str) and bool(name),
          '源场景的线性色彩空间尚未有效设置。请先在输出属性中为 OpenEXR 选择有效的线性色彩空间，再提交预览；当前场景未被修改。')
    return name


def run(context,document,*,cancel_requested=None,progress=None):
    import bpy
    import numpy as np
    from mathutils import Matrix,Vector
    from . import joint_render,hair_map
    doc,plan=normalize(document);scene=context.scene;layer=context.view_layer
    old_linear=_source_linear_name(scene)
    directory=Path(doc['output_directory']);_need(not directory.exists() and directory.parent.is_dir(),'Output directory must be new with an existing parent')
    reference=scene.objects.get(doc['reference']);sun=scene.objects.get(doc['sun']);camera=scene.objects.get(doc['camera'])
    _need(reference is not None and sun is not None and sun.type=='LIGHT' and sun.data.type=='SUN' and camera is not None and camera.type=='CAMERA','Explicit head reference, Sun and source camera are required')
    _need(reference not in (sun,camera),'Head reference must be independent of the orbit camera and Sun')
    _need(camera==scene.camera,'Choose the current Scene source camera; switch its registered shot explicitly before this preview')
    _need(context.mode=='OBJECT','Preview requires Object mode')
    _need(scene.render.engine=='BLENDER_EEVEE','This quick-preview profile requires an EEVEE source')
    _need(scene.display_settings.display_device=='sRGB','Contact-sheet display-byte output currently requires the source sRGB display device')
    _need(sun.parent is None and len(sun.constraints)==0 and not _has_effective_animation(sun),
          'Sun must be unparented/unconstrained and have no effective animation; empty containers are retained')
    _need(camera.parent is None and len(camera.constraints)==0,'Camera parents/constraints require a separate orbit contract')
    animation=_camera_animation(camera)
    pause_action=doc['camera_mode']=='ORBIT' and animation['action'] is not None
    _need(not pause_action or bpy.app.background,'An Action camera orbit must use the isolated background preview submission')
    hair_map._sun(scene,sun)
    _need(sun.data.use_shadow,'Preview preserves native shadows; enable them in the source if intended')
    native_sun,native_camera=_native(sun),_native(camera);old_camera=scene.camera
    light_data=dict(energy=float(sun.data.energy),color=list(sun.data.color),use_shadow=bool(sun.data.use_shadow),angle=float(sun.data.angle))
    camera_data={key:getattr(camera.data,key) for key in ('type','lens','ortho_scale','clip_start','clip_end','sensor_fit','sensor_width','sensor_height','shift_x','shift_y')}
    selected=list(context.selected_objects);active=layer.objects.active
    indices={o:o.active_material_index for o in scene.objects if o.type=='MESH'}
    visibility={o:o.hide_render for o in scene.objects};markers=[(m,m.camera) for m in scene.timeline_markers]
    old_layers={v:v.use for v in scene.view_layers}
    render={key:getattr(scene.render,key) for key in ('engine','filepath','resolution_x','resolution_y','resolution_percentage','use_compositing','use_sequencer','use_border')}
    fmt=scene.render.image_settings;old_fmt={key:getattr(fmt,key) for key in ('file_format','color_mode','color_depth','exr_codec','color_management')}
    old_samples=scene.eevee.taa_render_samples
    guard=_managed_guard(scene);guard_sha=hashlib.sha256(_encode(guard).encode()).hexdigest()
    reference_world=reference.matrix_world.copy()
    basis=_basis(reference_world);focus=reference_world.translation+sum((axis*v for axis,v in zip(basis,doc['focus_offset'])),Vector())
    radius=doc['orbit_distance'] or (camera.matrix_world.translation-focus).length
    _need(radius>1e-6,'Camera orbit radius is zero; choose an explicit distance')
    directory.mkdir();frames=directory/'frames';frames.mkdir()
    report=dict(schema=SCHEMA,status='running',document=doc,plan=plan,frames=[],pages=[],art_status='not_evaluated',guard_before_sha256=guard_sha,
        source_time=[scene.frame_current,float(scene.frame_subframe)],engine='BLENDER_EEVEE',
        source_camera_data=camera_data,source_light_data=light_data,
        camera_action=dict(name=animation['action'].name if animation['action'] else None,slot_identifier=animation['slot'].identifier if animation['slot'] else None,
            slot_handle=animation['handle'],data_sha256=_action_hash(animation['data']),
            mode='temporarily_detached_in_background' if pause_action else 'kept_attached_at_current_frame',animation_data_fields=animation['fields']),
        scope='Native full-material Beauty only; original shaders/occlusion; no Pencil Line compositing')
    def checkpoint():
        _write(directory/'REPORT.json',report)
        if progress:progress(report)
    def cancelled():return cancel_requested is not None and cancel_requested()
    error=None;action_paused=False;camera_evaluated_before=None
    try:
        for marker,_ in markers:marker.camera=None
        scene.camera=camera
        for view in scene.view_layers:view.use=view==layer
        scene.render.engine='BLENDER_EEVEE';scene.render.resolution_x=scene.render.resolution_y=doc['tile_size'];scene.render.resolution_percentage=100
        scene.render.use_compositing=False;scene.render.use_sequencer=False;scene.render.use_border=False;scene.eevee.taa_render_samples=doc['samples']
        camera_evaluated_before=_camera_render_matrix(scene,layer,camera)
        report['camera_evaluated_before']=camera_evaluated_before
        if pause_action:
            animation['container'].action=None;action_paused=True
            _need(camera.animation_data==animation['container'] and camera.animation_data.action is None,'Camera Action did not pause in its existing container')
            layer.update()
        checkpoint()
        for case in plan['cases']:
            if cancelled():raise PreviewCancelled('Cancelled between tiles; current successful outputs retained')
            light=_world_direction(case['light'],basis)
            matrix=light.to_track_quat('Z','Y').to_matrix().to_4x4();matrix.translation=Vector(native_sun['matrix_world'][i][3] for i in range(3));sun.matrix_world=matrix
            if doc['camera_mode']=='ORBIT':
                back=_world_direction(case['camera'],basis)
                camera.matrix_world=(-back).to_track_quat('-Z','Y').to_matrix().to_4x4();camera.location=focus+back*radius
            layer.update();expected_camera=camera.matrix_world.copy() if doc['camera_mode']=='ORBIT' else Matrix(camera_evaluated_before)
            from . import component_slots
            # component-preview-values.v1: per-tile light/extent, restored before checkpoint
            with component_slots.render_values(scene,light_direction=list(light),resolution=[doc['tile_size'],doc['tile_size']]) as component_values:
                before=_witness(scene,layer,camera,sun,reference,guard,light,expected_camera,reference_world)
                stem=str(case['index']+1).zfill(3);exr=frames/(stem+'.exr');png=frames/(stem+'.png')
                fmt.file_format='OPEN_EXR';fmt.color_mode='RGBA';fmt.color_depth='32';fmt.exr_codec='ZIP';fmt.color_management='OVERRIDE';fmt.linear_colorspace_settings.name='Linear Rec.709'
                pixels=joint_render._beauty(scene,layer,exr,doc['tile_size'],doc['tile_size'])
                _need(pixels.shape==(doc['tile_size'],doc['tile_size'],4) and np.isfinite(pixels).all(),'Native preview pixels invalid')
                after=_witness(scene,layer,camera,sun,reference,guard,light,expected_camera,reference_world)
                _need(_encode(before)==_encode(after),'Actual camera/light/gates changed during the tile')
                _save_display(scene,pixels,png)
                report['frames'].append(dict(case=case,exr=str(exr),exr_sha256=_sha(exr),png=str(png),png_sha256=_sha(png),before=before,after=after,components=component_values))
            count=len(report['frames']);start=(count-1)//doc['page_size']*doc['page_size']
            if count%doc['page_size']==0 or count==plan['tiles']:
                page=directory/('sheet-'+str(len(report['pages'])+1).zfill(3)+'.png')
                report['pages'].append(_sheet(report['frames'][start:count],page,doc['page_size'],doc['tile_size']))
            checkpoint()
        report['status']='completed'
    except BaseException as exc:
        error=exc;report.update(status='cancelled' if isinstance(exc,PreviewCancelled) else 'failed',error=str(exc))
        covered=sum(len(page['tiles']) for page in report['pages'])
        if covered<len(report['frames']):
            page=directory/('sheet-'+str(len(report['pages'])+1).zfill(3)+'.png')
            if not page.exists():
                try:
                    partial=_sheet(report['frames'][covered:],page,doc['page_size'],doc['tile_size'])
                    partial['partial']=True;report['pages'].append(partial)
                except Exception as sheet_error:report['partial_sheet_error']=str(sheet_error)
    finally:
        restoration_errors=[]
        def restore_step(label,operation):
            try:return operation()
            except BaseException as failure:restoration_errors.append(label+': '+str(failure));return None
        if action_paused:restore_step('camera Action/slot',lambda:_restore_camera_animation(camera,animation))
        restore_step('Sun native channels',lambda:_restore_native(sun,native_sun))
        restore_step('camera native channels',lambda:_restore_native(camera,native_camera))
        for marker,value in markers:restore_step('camera marker '+marker.name,lambda marker=marker,value=value:setattr(marker,'camera',value))
        restore_step('Scene camera',lambda:setattr(scene,'camera',old_camera))
        for obj,value in visibility.items():restore_step('visibility '+obj.name,lambda obj=obj,value=value:setattr(obj,'hide_render',value))
        for view,value in old_layers.items():restore_step('View Layer '+view.name,lambda view=view,value=value:setattr(view,'use',value))
        for key,value in render.items():restore_step('render '+key,lambda key=key,value=value:setattr(scene.render,key,value))
        for key,value in old_fmt.items():restore_step('image format '+key,lambda key=key,value=value:setattr(fmt,key,value))
        restore_step('linear output colorspace',lambda:setattr(fmt.linear_colorspace_settings,'name',old_linear))
        restore_step('EEVEE samples',lambda:setattr(scene.eevee,'taa_render_samples',old_samples))
        for obj in layer.objects:restore_step('selection '+obj.name,lambda obj=obj:obj.select_set(obj in selected))
        for obj,index in indices.items():restore_step('active material slot '+obj.name,lambda obj=obj,index=index:setattr(obj,'active_material_index',index))
        restore_step('active object',lambda:setattr(layer.objects,'active',active));restore_step('View Layer update',layer.update)
        camera_evaluated_after=restore_step('camera actual RENDER matrix',lambda:_camera_render_matrix(scene,layer,camera)) if camera_evaluated_before is not None else None
        restore_step('Scene camera after evaluation',lambda:setattr(scene,'camera',old_camera))
        report['camera_evaluated_after']=camera_evaluated_after
        action_data_after=restore_step('camera Action curve snapshot',lambda:_action_data(animation['action']))
        report['camera_action']['data_sha256_after']=_action_hash(action_data_after)
        action_exact=restore_step('camera Action data guard',lambda:_animation_guard(camera,animation,action_data_after))
        managed_exact=restore_step('managed source guard',lambda:_managed_guard(scene)==guard)
        checks=dict(native_sun_exact=_native(sun)==native_sun,native_camera_exact=_native(camera)==native_camera,
            source_camera_exact=scene.camera==old_camera,markers_exact=all(m.camera==v for m,v in markers),
            visibility_exact=all(o.hide_render==v for o,v in visibility.items()),time_exact=[scene.frame_current,float(scene.frame_subframe)]==report['source_time'],
            selection_exact=set(context.selected_objects)==set(selected) and layer.objects.active==active,
            material_slots_exact=all(o.active_material_index==i for o,i in indices.items()),managed_guard_exact=managed_exact is True,
            render_settings_exact=all(getattr(scene.render,k)==v for k,v in render.items()),
            image_settings_exact=all(getattr(fmt,k)==v for k,v in old_fmt.items()) and fmt.linear_colorspace_settings.name==old_linear,
            samples_and_view_layers_exact=scene.eevee.taa_render_samples==old_samples and all(v.use==value for v,value in old_layers.items()),
            light_data_exact=dict(energy=float(sun.data.energy),color=list(sun.data.color),use_shadow=bool(sun.data.use_shadow),angle=float(sun.data.angle))==light_data,
            camera_data_exact=all(getattr(camera.data,k)==v for k,v in camera_data.items()),
            camera_Action_slot_all_data_exact=action_exact is True,
            camera_actual_RENDER_matrix_exact=camera_evaluated_before==camera_evaluated_after,
            restoration_operations_succeeded=not restoration_errors)
        report['restoration_errors']=restoration_errors
        report['restoration']=checks
        if not all(checks.values()):report.update(status='failed',restoration_error='Owned preview state did not restore exactly')
        checkpoint()
    if error is not None and not isinstance(error,PreviewCancelled):raise error
    _need(all(report['restoration'].values()),'Preview restoration failed; see retained report')
    return report


def submit(context,document):
    """Save a new copy snapshot, verify host state, launch one owned hidden worker."""
    import bpy
    from . import workbench
    doc,plan=normalize(document);directory=Path(doc['output_directory'])
    _source_linear_name(context.scene)
    _need(not directory.exists() and directory.parent.is_dir(),'Choose a new job directory')
    reference=context.scene.objects.get(doc['reference']);sun=context.scene.objects.get(doc['sun']);camera=context.scene.objects.get(doc['camera'])
    _need(reference is not None and sun is not None and sun.type=='LIGHT' and sun.data.type=='SUN'
          and camera is not None and camera.type=='CAMERA' and camera==context.scene.camera
          and reference not in (sun,camera) and context.mode=='OBJECT','Choose an independent head reference, single Sun and current Scene camera in Object mode')
    _need(context.scene.render.engine=='BLENDER_EEVEE','This quick-preview profile requires an EEVEE source')
    root_name=__package__.split('.')[0];root=importlib.import_module(root_name);package=Path(root.__file__).resolve().parent
    source=bpy.data.filepath;source_sha=_sha(source) if source and Path(source).is_file() else None
    before=dict(filepath=source,dirty=bool(bpy.data.is_dirty),camera=context.scene.camera,sun=_native(sun),selected_camera=_native(camera),
                active=context.view_layer.objects.active,selected=set(context.selected_objects))
    camera_animation=_camera_animation(camera)
    resources=[dict(file=name,sha256=_sha(name)) for name in workbench.collect_resources()]
    directory.mkdir();snapshot=directory/'snapshot.blend';record=dict(schema=SCHEMA,status='preparing',directory=str(directory),snapshot=str(snapshot),
        source_filepath=source,source_sha256=source_sha,source_dirty_before=before['dirty'],scene=context.scene.name,view_layer=context.view_layer.name,resources=resources,total=plan['tiles'],
        root_module=root_name,domain_module=__package__,package_directory=str(package),document=dict(doc,output_directory=str(directory/'preview')))
    try:
        _need(bpy.ops.wm.save_as_mainfile(filepath=str(snapshot),copy=True,check_existing=False,relative_remap=True)=={'FINISHED'},'Copy snapshot failed')
        checks=dict(filepath=bpy.data.filepath==before['filepath'],dirty=bool(bpy.data.is_dirty)==before['dirty'],camera=context.scene.camera==before['camera'],
            native_sun=_native(sun)==before['sun'],native_camera=_native(camera)==before['selected_camera'],active=context.view_layer.objects.active==before['active'],
            selection=set(context.selected_objects)==before['selected'],source_disk_sha=source_sha is None or _sha(source)==source_sha,
            camera_Action_slot_data=_animation_guard(camera,camera_animation,_action_data(camera_animation['action'])))
        record.update(snapshot_host_checks=checks,source_dirty_after=bool(bpy.data.is_dirty),snapshot_sha256=_sha(snapshot))
        _need(all(checks.values()),'Snapshot changed host state; dirty state is reported, never claimed restored')
        _write(directory/'request.json',record)
        worker=directory/'worker.py'
        worker.write_text("import sys,importlib\nsys.dont_write_bytecode=True\nsys.path.insert(0,"+repr(str(package.parent))+")\nmodule=importlib.import_module("+repr(__package__+'.omni_preview')+")\nmodule._worker_main("+repr(str(directory/'request.json'))+")\n",'utf8')
        env={k:v for k,v in os.environ.items() if not k.startswith('BLENDER_') and k not in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP')}
        for name in ('CONFIG','SCRIPTS','DATAFILES','EXTENSIONS'):
            path=directory/'user'/name.lower();path.mkdir(parents=True);env['BLENDER_USER_'+name]=str(path)
        env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUNBUFFERED']='1'
        command=[bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec','--offline-mode','--python-exit-code','23','--python',str(worker)]
        options=dict(cwd=str(directory),env=env,stdin=subprocess.DEVNULL)
        if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
        with (directory/'stdout.log').open('xb') as stdout,(directory/'stderr.log').open('xb') as stderr:
            process=subprocess.Popen(command,stdout=stdout,stderr=stderr,**options)
        _processes[str(directory)]=process;record.update(status='submitted',pid=process.pid,command=command,preview_report=str(directory/'preview/REPORT.json'))
        _write(directory/'submission.json',record)
        return record
    except BaseException as exc:
        record.update(status='failed',error=str(exc),source_dirty_after=bool(bpy.data.is_dirty));_write(directory/'submission.json',record);raise


def _worker_main(request_path):
    import bpy
    request=_read(request_path);directory=Path(request_path).parent;root=importlib.import_module(request['root_module'])
    try:
        _need(Path(root.__file__).resolve().parent==Path(request['package_directory']),'Worker imported another plugin package')
        _need(_sha(request['snapshot'])==request['snapshot_sha256'],'Snapshot changed before worker open')
        _need(all(_sha(r['file'])==r['sha256'] for r in request['resources']),'External source resources changed')
        root.register();_need(bpy.ops.wm.open_mainfile(filepath=request['snapshot'],load_ui=False,use_scripts=False)=={'FINISHED'},'Worker snapshot open failed')
        scene=bpy.data.scenes[request['scene']];bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[request['view_layer']]
        def progress(report):_write(directory/'job.json',dict(status=report['status'],worker_terminal=False,completed=len(report['frames']),total=report['plan']['tiles'],report=str(Path(request['document']['output_directory'])/'REPORT.json')))
        report=run(bpy.context,request['document'],cancel_requested=lambda:(directory/'CANCEL.request').exists(),progress=progress)
        _need(_sha(request['snapshot'])==request['snapshot_sha256'] and all(_sha(r['file'])==r['sha256'] for r in request['resources']),'Worker input files changed')
        _write(directory/'job.json',dict(status=report['status'],worker_terminal=True,completed=len(report['frames']),total=report['plan']['tiles'],report=str(Path(request['document']['output_directory'])/'REPORT.json')))
    except BaseException as exc:
        _write(directory/'job.json',dict(status='failed',worker_terminal=True,error=str(exc)));raise


def status(directory, *, include_report=True):
    directory=Path(directory);submission=_read(directory/'submission.json')
    _need(submission.get('schema')==SCHEMA and Path(submission['directory'])==directory,'Not an owned omni submission')
    result=_read(directory/'job.json') if (directory/'job.json').is_file() else dict(status=submission['status'],completed=0,total=submission.get('total'))
    process=_processes.get(str(directory));result['process_exit_code']=process.poll() if process is not None else None
    result['process_observation']='owned in this UI session' if process is not None else 'file evidence only; process not owned in this UI session'
    if result['status'] in ('completed','cancelled') and not result.get('worker_terminal',False):result['status']='finalizing'
    if process is not None and result['process_exit_code'] is not None and result['process_exit_code']!=0:result['status']='failed'
    if include_report and result.get('report') and Path(result['report']).is_file():
        report=_read(result['report']);result['pages']=report['pages'];result['restoration']=report.get('restoration')
    return result


def request_cancel(directory):
    directory=Path(directory);state=status(directory)
    _need(state['status'] not in ('completed','cancelled','failed'),'Preview is already terminal')
    _write(directory/'CANCEL.request',dict(schema=SCHEMA,requested=True))
    return dict(status='cancel_requested',note='Current tile may finish; cancellation is cooperative between tiles')


__all__=['normalize','preset','run','submit','status','request_cancel']
