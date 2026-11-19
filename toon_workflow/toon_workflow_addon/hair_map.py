# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, reversible HM color layer. No legacy graph is automatically migrated.

The first profile admits a single white Sun, a zero World and opaque two-color
hair ramps. Its Sun-aligned Diffuse readback is an illumination approximation,
not a renderer visibility pass. Geometry, UVs and original normal readers stay
native; the independent carrier stores only a constant world light direction.
"""
import hashlib
import json
import math
import uuid

from . import hair_map_nodes as N, hair_fixed_shadow as B

PROFILE = 'toon-hair-map.v1'
KEY = 'toon_hm_binding'
OWNER = 'toon_hm_owner'
PREFIX = 'toon_hm_'
_TARGET = PREFIX+'target'
_SUN = PREFIX+'sun'
_CARRIER = PREFIX+'carrier'
_UID = PREFIX+'carrier_uid'
_ATTRIBUTE = PREFIX+'direction_attribute'
_SHAPE = PREFIX+'shape'
_DECLARED = PREFIX+'declared'
_BINDING = PREFIX+'seam'
_IMAGE = PREFIX+'image'
_IMAGE_VALUE = PREFIX+'image_descriptor'
_UV = PREFIX+'uv'
_RAMP = PREFIX+'ramp'
_NORMAL = PREFIX+'normal'
_DIFFUSE = PREFIX+'diffuse'
_CREATED_NORMAL = PREFIX+'created_normal'
_CARRIER_PROFILE = 'toon-hair-map-direction.v1'


class HairMapError(ValueError):
    pass


def _need(value, message):
    if not value:
        raise HairMapError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _plain(value):
    if value is None or type(value) in (str,int,bool):return value
    if isinstance(value,float):
        _need(math.isfinite(value),'Nonfinite HM node state')
        return value
    if hasattr(value,'name_full'):return dict(name=value.name_full,type=value.bl_rna.identifier)
    return [_plain(x) for x in value]


def present(material):
    if material is None:return False
    return (any(str(k).startswith(PREFIX) for k in material.keys()) or
        bool(material.node_tree and any(n.get(OWNER) is not None or
          (n.bl_idname=='ShaderNodeGroup' and n.node_tree and N.PROFILE_KEY in n.node_tree)
          for n in material.node_tree.nodes)))


def _socket(sockets, identifier):
    values=[s for s in sockets if s.identifier==identifier]
    _need(len(values)==1,'HM socket identity changed: '+identifier)
    return values[0]


def _target(scene,obj,index,*,frozen=False):
    import bpy
    _need(scene is not None,'HM requires an explicit Scene')
    _need(isinstance(obj,bpy.types.Object) and obj.type=='MESH' and scene.objects.get(obj.name)==obj
          and not obj.library and not obj.override_library and obj.is_editable,'Choose a local Mesh in this Scene')
    _need(obj.data is not None and not obj.data.library and not obj.data.override_library,'HM needs a local Mesh')
    _need(type(index) is int and 0<=index<len(obj.material_slots),'Choose one explicit used material slot')
    slot=obj.material_slots[index];material=slot.material
    _need(material is not None and material.use_nodes and material.node_tree is not None
          and not material.library and not material.override_library and material.is_editable,'HM needs a local node material')
    _need(any(p.material_index==index for p in obj.data.polygons),'HM slot has no faces')
    consumers=[(o,i) for o in bpy.data.objects if o.type=='MESH'
               for i,s in enumerate(o.material_slots) if s.material==material]
    _need(consumers==[(obj,index)],'HM material must belong to one explicit Object/slot')
    _need(not material.animation_data and not material.node_tree.animation_data,'HM v1 material parameters must be static')
    if not frozen:
        _need(obj.data.users==1,'HM requires an exclusive source Mesh')
    return material,material.node_tree


def _world(scene):
    world=scene.world
    _need(world is not None and world.use_nodes and not world.animation_data,'HM requires an explicit zero World')
    t=world.node_tree
    _need(t is not None and not t.animation_data and len(t.nodes)==2 and len(t.links)==1,'HM v1 requires a simple zero Background World')
    backgrounds=[n for n in t.nodes if n.bl_idname=='ShaderNodeBackground' and not n.mute]
    outputs=[n for n in t.nodes if n.bl_idname=='ShaderNodeOutputWorld' and n.is_active_output and not n.mute]
    _need(len(backgrounds)==len(outputs)==1,'HM World has changed')
    b,o=backgrounds[0],outputs[0];link=list(t.links)[0]
    _need(link.from_socket==b.outputs['Background'] and link.to_socket==o.inputs['Surface']
          and not b.inputs['Strength'].is_linked and b.inputs['Strength'].default_value==0.,'HM v1 requires zero World strength')
    return dict(name=world.name,policy='single constant Background, strength=0')


def _sun(scene,sun,*,depsgraph=None,target=None):
    import bpy
    _need(isinstance(sun,bpy.types.Object) and scene.objects.get(sun.name)==sun and sun.type=='LIGHT'
          and sun.data.type=='SUN' and not sun.library and not sun.data.library
          and not sun.override_library and not sun.data.override_library and not sun.hide_render,'Choose a local render-visible Sun')
    _need(not sun.data.animation_data,'HM v1 Sun energy/color must be static')
    # Blender 5.2 creates lights with nodes enabled. Admit only the factory
    # constant white/unit Emission tree, never an arbitrary node light.
    if sun.data.use_nodes:
        t=sun.data.node_tree
        _need(t is not None and not t.animation_data and len(t.nodes)==2 and len(t.links)==1,
              'HM Sun nodes must be the constant white unit Emission')
        es=[n for n in t.nodes if n.bl_idname=='ShaderNodeEmission' and not n.mute]
        os=[n for n in t.nodes if n.bl_idname=='ShaderNodeOutputLight' and n.is_active_output and not n.mute]
        _need(len(es)==len(os)==1,'HM Sun light output changed')
        e,o=es[0],os[0];link=list(t.links)[0]
        _need(link.from_socket==e.outputs['Emission'] and link.to_socket==o.inputs['Surface']
              and not e.inputs['Color'].is_linked and list(e.inputs['Color'].default_value)==[1.,1.,1.,1.]
              and not e.inputs['Strength'].is_linked and e.inputs['Strength'].default_value==1.,
              'HM Sun light Emission must be white/unit')
    ld=sun.data
    _need(ld.use_shadow and ld.diffuse_factor==1. and ld.exposure==0.,
          'HM v1 requires Sun shadows enabled, diffuse factor 1 and exposure 0')
    _need(all(v==1. for v in ld.color) and math.isfinite(ld.energy) and 0.<=ld.energy<=1.e6,
          'HM v1 requires a white Sun with finite energy')
    if depsgraph is not None:
        visible={i.object.original for i in depsgraph.object_instances if i.show_self}
        _need(sun in visible,'HM Sun is absent from the actual dependency graph')
        lights=[o for o in visible if o.type=='LIGHT' and not o.hide_render
                and o.evaluated_get(depsgraph).data.energy>0.]
        from .lighting_53 import is_fill_excluded
        _need(all(o==sun or is_fill_excluded(scene,target,o) for o in lights),
              'HM v1 requires its single white Sun for this material')
        _need(not any(o.type=='LIGHT_PROBE' and not o.hide_render for o in visible),'HM v1 does not admit light probes')
    _world(scene)
    return dict(object=sun.name,data=ld.name,type='SUN',energy=float(ld.energy),color=list(ld.color),
                angle=float(ld.angle),use_shadow=bool(ld.use_shadow),diffuse_factor=float(ld.diffuse_factor),
                exposure=float(ld.exposure),world=_world(scene),policy='static energy; evaluated live world +Z; illumination approximation')


def _image(image,group=None):
    from . import face_sdf as sdf
    r=sdf._image_content_descriptor(image,'R');g=sdf._image_content_descriptor(image,'G')
    allowed=set() if group is None else {group}
    expected=int(image.use_fake_user)+(2 if group is not None else 0)
    _need(not image.use_extra_user and image.users==expected and not(sdf._image_consumers(image)-allowed),
          'HM needs a dedicated Non-Color Image ID; do not reuse the old sRGB/B or finish Image')
    return dict(R=r,G=g,sampling='Linear/EXTEND/FLAT',channels='R threshold, G directional shadow')


def _ramp(node):
    _need(node is not None and node.bl_idname=='ShaderNodeValToRGB' and not node.mute,'HM source ramp changed')
    ramp=node.color_ramp
    _need(ramp.interpolation=='CONSTANT' and ramp.color_mode=='RGB' and len(ramp.elements)==2
          and all(e.color[3]==1. for e in ramp.elements),'HM v1 requires an opaque two-color CONSTANT RGB ramp')
    return dict(node=node.name,interpolation=ramp.interpolation,color_mode=ramp.color_mode,
                elements=[dict(position=float(e.position),color=list(e.color)) for e in ramp.elements])


def _carrier_shape(group):
    return dict(nodes=[dict(name=n.name,type=n.bl_idname,inputs=[(s.identifier,_plain(s.default_value))
              for s in n.inputs if hasattr(s,'default_value')],properties={k:_plain(getattr(n,k))
              for k in ('operation','transform_space','data_type','domain','is_active_output') if hasattr(n,k)})
              for n in group.nodes],
        interface=[dict(name=s.name,identifier=s.identifier,in_out=s.in_out,type=s.socket_type)
                   for s in group.interface.items_tree if s.item_type=='SOCKET'],
        links=sorted((l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier) for l in group.links))


def _create_carrier(obj,sun,owner):
    import bpy
    group=bpy.data.node_groups.new('Toon HM Sun '+owner[:8],'GeometryNodeTree');modifier=None
    try:
        a=group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        b=group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
        entry=group.nodes.new('NodeGroupInput');out=group.nodes.new('NodeGroupOutput')
        info=group.nodes.new('GeometryNodeObjectInfo');info.transform_space='ORIGINAL';info.inputs['Object'].default_value=sun
        direction=group.nodes.new('FunctionNodeTransformDirection');direction.inputs['Direction'].default_value=(0,0,1)
        unit=group.nodes.new('ShaderNodeVectorMath');unit.operation='NORMALIZE'
        store=group.nodes.new('GeometryNodeStoreNamedAttribute');store.data_type='FLOAT_VECTOR';store.domain='POINT'
        attr='toon_hm_sun_'+owner;store.inputs['Name'].default_value=attr
        for source,dest in ((info.outputs['Transform'],direction.inputs['Transform']),
              (direction.outputs['Direction'],unit.inputs[0]),(unit.outputs['Vector'],store.inputs['Value']),
              (_socket(entry.outputs,a.identifier),store.inputs['Geometry']),
              (store.outputs['Geometry'],_socket(out.inputs,b.identifier))):group.links.new(source,dest)
        group[OWNER]=owner;group['profile']=_CARRIER_PROFILE;group[_TARGET]=obj;group[_SUN]=sun;group[_ATTRIBUTE]=attr
        group[_SHAPE]=_sha(_carrier_shape(group))
        modifier=obj.modifiers.new('Toon HM Live Sun','NODES');modifier.node_group=group
        group[_UID]=modifier.persistent_uid
        with bpy.context.temp_override(object=obj,active_object=obj,selected_objects=[obj],selected_editable_objects=[obj]):
            _need(bpy.ops.object.modifier_move_to_index(modifier=modifier.name,index=0)=={'FINISHED'},'Could not place HM carrier')
        return modifier,group
    except BaseException:
        if modifier is not None:obj.modifiers.remove(modifier)
        if group.users==0:bpy.data.node_groups.remove(group)
        raise


def _link(tree,source,dest):
    _need(len(dest.links)==1 and dest.links[0].from_socket==source,'HM external input footprint changed')


def _owned(scene,obj,index,*,frozen=False,_with_mask=False):
    import bpy
    material,tree=_target(scene,obj,index,frozen=frozen);group=material.get(KEY)
    _need(isinstance(group,bpy.types.NodeTree) and group.get(N.PROFILE_KEY)==N.PROFILE
          and group.get(_TARGET)==obj and not group.library and not group.override_library,'HM binding is missing or unsupported')
    owner=group.get(OWNER)
    _need(type(owner) is str and len(owner)==32 and all(x in '0123456789abcdef' for x in owner),'HM owner is invalid')
    instances=[n for n in tree.nodes if n.get(OWNER)==owner and n.bl_idname=='ShaderNodeGroup']
    _need(len(instances)==1 and instances[0].node_tree==group and not instances[0].mute,'HM instance identity changed')
    instance=instances[0]
    _need(group.users==2 and not group.use_fake_user and not group.use_extra_user
          and bpy.data.user_map(subset={group}).get(group,set()).issubset({material,tree}),'HM Group acquired a foreign consumer')
    _need(not group.animation_data and group.get(_SHAPE)==_sha(N.structure(group)),'HM graph was edited outside its declared state')
    settings=N.parameters(group);_need(_json(settings)==group.get(_DECLARED),'HM parameters conflict with declared state')
    binding=json.loads(group[_BINDING]);ramp=tree.nodes.get(binding['ramp'])
    _need(ramp is not None and ramp.get(_RAMP)==owner,'HM original ramp identity changed')
    _need(_ramp(ramp)==binding['ramp_state'],'Original hair palette changed; remove HM before editing the source ramp')
    _link(tree,ramp.outputs['Color'],instance.inputs['Original Color'])
    _need(len(ramp.outputs['Color'].links)==1 and len(instance.outputs['Color'].links)==1,'HM Color seam is not exclusive')
    normal=tree.nodes.get(binding['normal_node'])
    _need(normal is not None and normal.get(_NORMAL)==owner and normal.bl_idname==binding['normal_type'],'HM Normal source identity changed')
    _link(tree,_socket(normal.outputs,binding['normal_socket']),instance.inputs['Normal'])
    diffuse=tree.nodes.get(binding['diffuse'])
    _need(diffuse is not None and diffuse.bl_idname=='ShaderNodeBsdfDiffuse' and diffuse.get(_DIFFUSE)==owner,
          'HM original Diffuse identity changed')
    if binding['original_normal_linked']:
        _link(tree,_socket(normal.outputs,binding['normal_socket']),diffuse.inputs['Normal'])
    else:
        _need(not diffuse.inputs['Normal'].is_linked,'HM original geometric Normal was replaced')
    destination=tree.nodes.get(binding['destination'])
    _need(destination is not None and destination.bl_idname==binding['destination_type'],'HM downstream node changed')
    _link(tree,instance.outputs['Color'],_socket(destination.inputs,binding['destination_socket']))
    if binding['fixed_shadow']:
        _need(material.get(B.PROFILE_KEY)==B.HM_PROFILE,'HM requires its explicit fixed-shadow v3 seam')
        B.validate(material,observation=frozen)
    cg=group.get(_CARRIER);sun=group.get(_SUN)
    _need(isinstance(cg,bpy.types.NodeTree) and cg.get('profile')==_CARRIER_PROFILE and cg.get(OWNER)==owner
          and cg.get(_TARGET)==obj and cg.get(_SUN)==sun and cg.get(_UID)==group.get(_UID)
          and cg.get(_ATTRIBUTE)==group.get(_ATTRIBUTE) and cg.get(_SHAPE)==_sha(_carrier_shape(cg)),
          'HM direction carrier identity/graph changed')
    infos=[n for n in cg.nodes if n.bl_idname=='GeometryNodeObjectInfo']
    _need(len(infos)==1 and infos[0].inputs['Object'].default_value==sun,
          'HM carrier lost its true Sun Object reference')
    _need(cg.users==1+(0 if frozen else 1) and not cg.use_fake_user and not cg.use_extra_user,
          'HM direction carrier acquired a foreign consumer')
    if not frozen:
        mods=[m for m in obj.modifiers if m.type=='NODES' and m.node_group==cg]
        _need(len(mods)==1 and mods[0].persistent_uid==group[_UID] and mods[0].show_render and mods[0].show_viewport,
              'HM carrier modifier changed or disabled')
        _need(group[_ATTRIBUTE] not in obj.data.attributes,'Raw Mesh conflicts with the owned HM output')
    image=group.get(_IMAGE)
    _need(_image(image,group)==json.loads(group[_IMAGE_VALUE]),'HM Image source or pixels changed')
    _need(obj.data.uv_layers.get(group[_UV]) is not None,'HM UV is missing')
    light=_sun(scene,sun)
    _need(abs(settings['irradiance_reference']-max(light['energy']/math.pi,1.e-6))<=max(4.e-7,settings['irradiance_reference']*2.e-7),
          'HM Sun energy changed; explicitly refresh HM with its new static light reference')
    # The older additive highlight has no HM cast gate. Require one selected
    # highlight source whenever the new R response is enabled.
    if settings['strength']>0 and settings['highlight_strength']>0:
        from . import finish_core as finish,finish_nodes
        fg=material.get(finish.KEY)
        if fg is not None:
            _need(finish_nodes.parameters(fg)['highlight_strength']==0.,'Disable the old finish highlight before enabling HM R; two highlights would add')
    from . import hair_highlight_mask
    mask=hair_highlight_mask.describe(group,obj,frozen=frozen)
    from . import hair_sdf
    sdf=hair_sdf.describe(group,obj,frozen=frozen)
    result=material,tree,group,instance,binding,light
    return result+(mask,sdf) if _with_mask else result


def describe(scene,obj,index,*,frozen=False):
    material,tree,g,instance,binding,sun,mask,sdf=_owned(scene,obj,index,frozen=frozen,_with_mask=True)
    b=B.validate(material,observation=frozen) if binding['fixed_shadow'] else None
    result=dict(profile=PROFILE,owner=g[OWNER],target=obj.name,material=material.name,group=g.name,
        slots=[dict(index=index,link=obj.material_slots[index].link)],settings=N.parameters(g),
        kernel_sha256=g[_SHAPE],binding=dict(binding,instance=instance.name,fixed_descriptor=b),
        uv_name=g[_UV],image=json.loads(g[_IMAGE_VALUE]),carrier=dict(group=g[_CARRIER].name,owner=g[OWNER],
            uid=g[_UID],attribute=g[_ATTRIBUTE],graph_signature=g[_CARRIER][_SHAPE]),sun=sun)
    if mask is not None:
        result['highlight_mask']=mask
    if sdf is not None:
        result['sdf']=sdf
    return result


def observe(scene,obj,index,depsgraph,*,frozen=False):
    import numpy as np
    from mathutils import Vector
    _need(depsgraph.mode=='RENDER','HM witness needs an actual RENDER graph')
    source=describe(scene,obj,index,frozen=frozen);g=obj.material_slots[index].material[KEY]
    sdf_evaluated=None
    if 'sdf' in source:
        from . import hair_sdf
        sdf_evaluated=hair_sdf.observe(g,obj,depsgraph,frozen=frozen)
    sun=g[_SUN];_sun(scene,sun,depsgraph=depsgraph,target=obj)
    ev=obj.evaluated_get(depsgraph);mesh=ev.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
    try:
        material=ev.material_slots[index].material
        _need(material is not None and material.original==obj.material_slots[index].material,'HM evaluated material changed')
        instances=[n for n in material.node_tree.nodes if n.get(OWNER)==g[OWNER] and n.bl_idname=='ShaderNodeGroup']
        _need(len(instances)==1 and instances[0].node_tree.original==g,'HM evaluated group changed')
        params=N.parameters(instances[0].node_tree.evaluated_get(depsgraph))
        _need(params==source['settings'],'HM Main and evaluated settings disagree')
        uv=mesh.uv_layers.get(g[_UV]);_need(uv is not None,'HM evaluated UV is missing')
        values=np.empty((len(mesh.loops),2),dtype='<f4');uv.data.foreach_get('uv',values.ravel())
        attr=mesh.attributes.get(g[_ATTRIBUTE])
        _need(attr is not None and attr.domain=='POINT' and attr.data_type=='FLOAT_VECTOR' and len(attr.data)==len(mesh.vertices),
              'HM evaluated direction attribute changed')
        dirs=np.empty((len(mesh.vertices),3),dtype='<f4');attr.data.foreach_get('vector',dirs.ravel())
        matrix=sun.evaluated_get(depsgraph).matrix_world
        expected=matrix.to_3x3()@Vector((0,0,1))
        _need(all(math.isfinite(v) for row in matrix for v in row) and expected.length>1.e-8,'HM Sun transform is singular/nonfinite')
        expected.normalize();delta=np.max(np.abs(dirs-np.asarray(expected))) if len(dirs) else math.inf
        _need(np.isfinite(values).all() and np.isfinite(dirs).all() and delta<=2.e-5,'HM evaluated direction/UV is invalid')
        evaluated=dict(uv=dict(uv_name=g[_UV],sha256=hashlib.sha256(values.tobytes()).hexdigest(),shape=list(values.shape)),
            direction=dict(attribute=g[_ATTRIBUTE],sha256=hashlib.sha256(dirs.tobytes()).hexdigest(),shape=list(dirs.shape)),
            sun_direction=list(expected),sun_world=[list(row) for row in matrix],settings=params)
        if 'highlight_mask' in source:
            name=source['highlight_mask']['uv']['uv_name']
            mask_uv=mesh.uv_layers.get(name)
            _need(mask_uv is not None,'Unified highlight evaluated mask UV is missing')
            mask_values=np.empty((len(mesh.loops),2),dtype='<f4')
            mask_uv.data.foreach_get('uv',mask_values.ravel())
            _need(np.isfinite(mask_values).all(),'Unified highlight evaluated mask UV is nonfinite')
            evaluated['highlight_mask_uv']=dict(uv_name=name,
                sha256=hashlib.sha256(mask_values.tobytes()).hexdigest(),shape=list(mask_values.shape))
        if sdf_evaluated is not None:
            evaluated['sdf']=sdf_evaluated
        return dict(source=source,evaluated=evaluated)
    finally:ev.to_mesh_clear()


def attach(context,image,sun,uv_name,settings=None):
    """Explicit new HM mode, disabled by default; original Ramp/B/finish stay."""
    import bpy
    scene,obj=context.scene,context.active_object;index=obj.active_material_index
    _need(scene.render.engine=='BLENDER_EEVEE','HM authoring v1 requires EEVEE')
    material,tree=_target(scene,obj,index)
    _need(not present(material),'HM is already present or has damaged reserved metadata')
    _need(not any(str(k).startswith(('toon_face_','toon_eye_','toon_surface_')) for k in material.keys()),
          'HM does not adopt protected face/eye/managed-surface materials')
    _image(image);light=_sun(scene,sun,depsgraph=bpy.context.evaluated_depsgraph_get())
    _need(type(uv_name) is str and obj.data.uv_layers.get(uv_name) is not None,'Choose an existing explicit hair UV')
    fixed=B.is_present(material)
    if fixed:
        checked=B.validate(material);_need(checked['profile']==B.PROFILE,'HM attachment requires an existing legal v2 fixed shadow')
        _,binstance,_,source,destination=B._binding(material)
        ramp=source.node;dest=binstance.inputs[0]
    else:
        seams=[l for l in tree.links if l.from_node.bl_idname=='ShaderNodeValToRGB' and l.from_socket==l.from_node.outputs['Color']
            and l.to_node.bl_idname=='ShaderNodeEmission' and l.to_socket==l.to_node.inputs['Color']]
        _need(len(seams)==1,'HM v1 needs one ramp Color -> Emission Color seam')
        ramp=seams[0].from_node;source=seams[0].from_socket;dest=seams[0].to_socket
    original=_ramp(ramp)
    _need(len(source.links)==1 and len(dest.links)==1,'HM requires an exclusive original Color seam')
    diffuse=[n for n in tree.nodes if n.bl_idname=='ShaderNodeBsdfDiffuse' and not n.mute]
    _need(len(diffuse)==1,'HM requires one original Diffuse normal source')
    normal_links=diffuse[0].inputs['Normal'].links
    _need(len(normal_links)<=1,'Original hair Normal input is ambiguous')
    owner=uuid.uuid4().hex;g=instance=modifier=cg=helper=None;bound=False
    values=N.defaults() if settings is None else N.normalize(settings)
    if settings is None:
        values.update(shadow_color=original['elements'][0]['color'],lit_color=original['elements'][1]['color'])
    values['irradiance_reference']=max(light['energy']/math.pi,1.e-6)
    try:
        modifier,cg=_create_carrier(obj,sun,owner)
        g=N.build(owner,values,image,uv_name,cg[_ATTRIBUTE])
        for k,v in ((_TARGET,obj),(_SUN,sun),(_CARRIER,cg),(_UID,modifier.persistent_uid),(_ATTRIBUTE,cg[_ATTRIBUTE]),
                    (_IMAGE,image),(_UV,uv_name)):g[k]=v
        instance=tree.nodes.new('ShaderNodeGroup');instance.node_tree=g;instance[OWNER]=owner
        instance.name='Toon HM '+owner[:8];instance.label='HM逐发束明暗 / R阈值高光'
        material[KEY]=g
        if normal_links:normal_source=normal_links[0].from_socket
        else:
            helper=tree.nodes.new('ShaderNodeNewGeometry');helper[_CREATED_NORMAL]=owner;normal_source=helper.outputs['Normal']
        _need(normal_source.node.get(_NORMAL) is None and ramp.get(_RAMP) is None
              and diffuse[0].get(_DIFFUSE) is None,'HM source node has foreign ownership markers')
        normal_source.node[_NORMAL]=owner;ramp[_RAMP]=owner;diffuse[0][_DIFFUSE]=owner
        tree.links.new(normal_source,instance.inputs['Normal'])
        g[_BINDING]=_json(dict(ramp=ramp.name,ramp_state=original,destination=dest.node.name,destination_type=dest.node.bl_idname,
          destination_socket=dest.identifier,normal_node=normal_source.node.name,normal_type=normal_source.node.bl_idname,
          normal_socket=normal_source.identifier,diffuse=diffuse[0].name,original_normal_linked=helper is None,
          fixed_shadow=fixed,created_normal=helper is not None))
        g[_IMAGE_VALUE]=_json(_image(image,g));g[_DECLARED]=_json(N.parameters(g));g[_SHAPE]=_sha(N.structure(g))
        if fixed:B.bind_hm(material,instance)
        else:
            tree.links.new(source,instance.inputs['Original Color']);tree.links.new(instance.outputs['Color'],dest)
        bound=True;obj.update_tag()
        return describe(scene,obj,index)
    except BaseException:
        if bound and fixed:B.unbind_hm(material)
        elif instance is not None:
            tree.links.new(source,dest)
        if instance is not None:tree.nodes.remove(instance)
        if KEY in material:del material[KEY]
        for node,key in ((ramp,_RAMP),(normal_source.node if 'normal_source' in locals() else None,_NORMAL),(diffuse[0],_DIFFUSE)):
            if node is not None and node.get(key)==owner:del node[key]
        if helper is not None:tree.nodes.remove(helper)
        if modifier is not None:obj.modifiers.remove(modifier)
        if g is not None and g.users==0:bpy.data.node_groups.remove(g)
        if cg is not None and cg.users==0:bpy.data.node_groups.remove(cg)
        raise


def update(context,settings):
    scene,obj=context.scene,context.active_object;index=obj.active_material_index
    return _update(scene,obj,index,settings)


def _update(scene,obj,index,settings):
    material,tree,g,instance,binding,light=_owned(scene,obj,index)
    values=N.normalize(settings);_need(values['irradiance_reference']==N.parameters(g)['irradiance_reference'],
          'HM light reference is derived at attachment; remove/re-attach for another static energy')
    from .lighting_53 import has_hair_stage
    _need(values['highlight_strength']==0. or not has_hair_stage(material),
          '此材质由 5.3 亮带控制；请在新工作台调节亮带强度')
    old=N.parameters(g);declared,shape=g[_DECLARED],g[_SHAPE]
    try:
        N.write_parameters(g,values);g[_DECLARED]=_json(N.parameters(g));g[_SHAPE]=_sha(N.structure(g))
        return describe(scene,obj,index)
    except BaseException:
        N.write_parameters(g,old);g[_DECLARED],g[_SHAPE]=declared,shape
        raise


def remove(context):
    import bpy
    scene,obj=context.scene,context.active_object;index=obj.active_material_index
    material,tree,g,instance,binding,light=_owned(scene,obj,index)
    from . import hair_sdf
    _need(not hair_sdf.present(g),'Remove the explicit hair SDF layer before removing its parent HM')
    cg=g[_CARRIER];owner=g[OWNER];ramp=tree.nodes[binding['ramp']];normal=tree.nodes[binding['normal_node']]
    mods=[m for m in obj.modifiers if m.type=='NODES' and m.node_group==cg]
    _need(len(mods)==1,'HM carrier cannot be removed safely')
    if binding['fixed_shadow']:B.unbind_hm(material)
    else:tree.links.new(ramp.outputs['Color'],_socket(tree.nodes[binding['destination']].inputs,binding['destination_socket']))
    tree.nodes.remove(instance);del material[KEY];del ramp[_RAMP];del normal[_NORMAL]
    del tree.nodes[binding['diffuse']][_DIFFUSE]
    if binding['created_normal']:tree.nodes.remove(normal)
    obj.modifiers.remove(mods[0]);_need(g.users==0,'HM Group acquired another consumer');bpy.data.node_groups.remove(g)
    _need(cg.users==0,'HM carrier acquired another consumer');bpy.data.node_groups.remove(cg)
    return dict(profile=PROFILE,removed_owner=owner,restored=True)
