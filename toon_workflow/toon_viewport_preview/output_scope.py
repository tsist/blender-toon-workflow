# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent Scene/ViewLayer overrides and scoped public worker submission."""
import copy
from contextlib import contextmanager
import json
import math
from .compositor_scope import active_binding, attach_tree, editable_trees, new_tree

KEY='toon_output_scopes_v1'
SCHEMA='toon.output-scopes.v1'
# Only public controls with known side effects are eligible, never arbitrary RNA.
FIELDS={'enabled','line_width','line_color','opacity','width_multiplier','intersection_enabled','display_mode'}
GROUP_FIELDS={'enabled','width_scale','opacity','line_set_id','elements_only'}


def read(scene):
    value=json.loads(scene.get(KEY,'{"schema":"toon.output-scopes.v1","scene":{},"layers":{}}'))
    if set(value)!={'schema','scene','layers'} or value['schema']!=SCHEMA:raise ValueError('Invalid output scopes')
    return value


def _validate(scene,values):
    if type(values) is not dict or set(values)-{'settings','groups','camera','viewport_background','render_background'}:raise ValueError('Unknown scope controls')
    settings=scene.toon_viewport_preview
    for name,value in values.get('settings',{}).items():
        if name not in FIELDS or not hasattr(settings,name):raise ValueError('Unsupported scoped setting: '+name)
        if name=='display_mode':
            if value not in ('OVERLAY','LINES','WHITE'):raise ValueError('Invalid display mode override')
        else:_scalar(value)
    for gid,fields in values.get('groups',{}).items():
        if settings.groups.get(gid) is None or type(fields) is not dict or set(fields)-GROUP_FIELDS:raise ValueError('Unknown group control')
        for value in fields.values():_scalar(value)
    camera=values.get('camera',{})
    for name in ('viewport_background','render_background'):
        if name in values:
            from .render_elements import normalize
            normalize([dict(id='background',background=values[name])],style_mode='BASIC',group_ids=['__BASIC__'])
    if set(camera)-{'scale','range','border'}:raise ValueError('Unknown camera scope')
    if 'scale' in camera and (type(camera['scale']) is not int or not 1<=camera['scale']<=100):raise ValueError('Camera scale must be 1..100 percent')
    if camera.get('range','CAMERA') not in ('CAMERA','BORDER'):raise ValueError('Invalid camera range')
    if 'border' in camera:
        b=camera['border']
        if type(b) is not list or len(b)!=4 or not all(type(v) in (int,float) and math.isfinite(v) for v in b) or not (0<=b[0]<b[2]<=1 and 0<=b[1]<b[3]<=1):raise ValueError('Invalid camera normalized border')
    return copy.deepcopy(values)


def _scalar(value):
    if type(value) in (list,tuple):
        if not 1<=len(value)<=4:raise ValueError('Invalid override vector')
        for item in value:_scalar(item)
    elif type(value) is not bool and (type(value) not in (int,float) or not math.isfinite(value)):
        raise ValueError('Overrides require finite scalar/vector values')


def set_scope(scene,values,*,view_layer=None):
    values=_validate(scene,values);config=read(scene)
    if view_layer is None:config['scene']=values
    else:
        if scene.view_layers.get(view_layer) is None:raise ValueError('Unknown ViewLayer')
        config['layers'][view_layer]=values
    scene[KEY]=json.dumps(config,ensure_ascii=False,allow_nan=False)
    return config


def remove_scope(scene,*,view_layer=None):
    config=read(scene)
    if view_layer is None:config['scene']={}
    else:config['layers'].pop(view_layer,None)
    scene[KEY]=json.dumps(config,ensure_ascii=False)
    return config


def toggle_bool(scene,name,*,view_layer=None,group=None):
    config=read(scene);values=config['scene'] if view_layer is None else config['layers'].get(view_layer,{})
    values=copy.deepcopy(values);target=values.setdefault('settings',{}) if group is None else values.setdefault('groups',{}).setdefault(group,{})
    source=scene.toon_viewport_preview if group is None else scene.toon_viewport_preview.groups[group]
    value=target.get(name,getattr(source,name))
    if type(value) is not bool:raise ValueError('Only boolean overrides can be toggled')
    target[name]=not value
    return set_scope(scene,values,view_layer=view_layer)


def resolved(scene,view_layer):
    config=read(scene);out=copy.deepcopy(config['scene'])
    for key,values in config['layers'].get(view_layer,{}).items():
        if key=='groups':
            for gid,fields in values.items():out.setdefault('groups',{}).setdefault(gid,{}).update(fields)
        else:out.setdefault(key,{}).update(values)
    return _validate(scene,out)


@contextmanager
def scoped(scene,view_layer):
    values=resolved(scene,view_layer);settings=scene.toon_viewport_preview;saved=[]
    def assign(owner,name,value):
        old=getattr(owner,name);old=list(old) if hasattr(old,'__len__') and not isinstance(old,str) else old
        saved.append((owner,name,old));setattr(owner,name,value)
    try:
        for name,value in values.get('settings',{}).items():assign(settings,name,value)
        for gid,fields in values.get('groups',{}).items():
            for name,value in fields.items():assign(settings.groups[gid],name,value)
        camera=values.get('camera',{});render=scene.render
        if 'scale' in camera:assign(render,'resolution_percentage',camera['scale'])
        if 'range' in camera:
            assign(render,'use_border',camera['range']=='BORDER');assign(render,'use_crop_to_border',camera['range']=='BORDER')
        if 'border' in camera:
            for name,value in zip(('border_min_x','border_min_y','border_max_x','border_max_y'),camera['border']):assign(render,name,value)
        yield values
    finally:
        for owner,name,value in reversed(saved):setattr(owner,name,value)


def request(path,context,*,elements=None,frames=None,keyed_only=False,prefix='line',extension='.png'):
    """The existing public workers snapshot synchronously while scope is active."""
    from . import render_output,sequence_output,runtime_preferences as preferences
    scene=context.scene
    try:
        with scoped(scene,context.view_layer.name):
            if frames is not None or keyed_only:
                frames=keyed_frames(scene) if keyed_only else frames
                if not frames:raise ValueError('No keyed frames in the requested scene range')
                from pathlib import Path
                target=Path(path)
                if target.suffix.lower() in ('.png','.exr'):prefix,extension,path=target.stem,target.suffix.lower(),target.parent
                submitted=sequence_output.request(path,prefix,frames,extension,context,elements=elements);owner=sequence_output
            else:submitted=render_output.request(path,context,elements=elements);owner=render_output
        return preferences.watch(scene,owner,submitted)
    except Exception as error:
        if preferences.configuration(scene)['abort_on_error']:raise
        return dict(status='FAILED',stage='SETUP_FAILED',error=str(error))


def keyed_frames(scene):
    """Collect actual keyframe times, including active action slots and NLA maps."""
    import bpy
    result=set()
    def curves(action,handle):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip,'channelbags',[]):
                    if handle is None or bag.slot_handle==handle:
                        yield from bag.fcurves
    blocks=[scene]+list(scene.objects)+[scene.camera.data] if scene.camera else [scene]+list(scene.objects)
    blocks+=list(bpy.data.materials)
    for block in blocks:
        ad=getattr(block,'animation_data',None)
        if ad is None:continue
        if ad.action:
            for fc in curves(ad.action,ad.action_slot.handle if ad.action_slot else None):
                result.update(round(k.co.x) for k in fc.keyframe_points)
        for track in ad.nla_tracks:
            if track.mute:continue
            for strip in track.strips:
                if strip.mute or strip.action is None:continue
                for fc in curves(strip.action,strip.action_slot.handle if strip.action_slot else None):
                    for k in fc.keyframe_points:
                        for repeat in range(math.ceil(strip.repeat)):
                            frame=strip.frame_start+(k.co.x-strip.action_frame_start+repeat*(strip.action_frame_end-strip.action_frame_start))*strip.scale
                            if strip.frame_start<=frame<=strip.frame_end:result.add(round(frame))
    return sorted(f for f in result if scene.frame_start<=f<=scene.frame_end)


def restore_outputs(scene):
    """Rebind only this extension's tagged compositor images after load."""
    import bpy
    restored=[]
    if scene.library or not scene.is_editable:
        return restored
    from pathlib import Path
    for tree in editable_trees(scene):
        for node in tree.nodes:
            path=node.get('toon_line_output_path')
            if path and node.bl_idname=='CompositorNodeImage':
                absolute=bpy.path.abspath(path)
                if Path(absolute).is_file():
                    node.image=bpy.data.images.load(absolute,check_existing=True)
                    restored.append(node.name)
    return restored


def add_image_node(scene,path,*,view_layer,pass_name='Combined'):
    import bpy
    if scene.view_layers.get(view_layer) is None:raise ValueError('Unknown ViewLayer')
    if scene.library or not scene.is_editable:raise ValueError('Scene must be local and editable')
    effect, source = active_binding(scene)
    tree = source
    image = node = selected = None
    try:
        image=bpy.data.images.load(bpy.path.abspath(path),check_existing=False)
        if source is None:
            tree=new_tree(scene)
        elif source.library or not source.is_editable or source.users>1:
            tree=source.copy()
            tree.use_fake_user=False
        node=tree.nodes.new('CompositorNodeImage')
        node.image=image
        if image.type=='MULTILAYER':
            node.layer='ToonLines'
            socket=next((output for output in node.outputs if output.identifier==pass_name),None)
            if socket is None:raise ValueError('Unknown line output pass: '+pass_name)
            selected=tree.nodes.new('NodeReroute')
            selected.label=view_layer+' · '+pass_name
            tree.links.new(socket,selected.inputs[0])
            selected['toon_line_pass']=pass_name
        image.pack()
        node.label=view_layer+' · '+pass_name
        node['toon_line_output_path']=str(path)
        node['toon_line_view_layer']=view_layer
        node['toon_line_pass']=pass_name
        if tree != source:
            attach_tree(scene,effect,tree)
        return node
    except Exception:
        if selected is not None:tree.nodes.remove(selected)
        if node is not None:tree.nodes.remove(node)
        if image is not None and image.users==0:bpy.data.images.remove(image)
        if tree is not None and tree!=source and tree.users==0:bpy.data.node_groups.remove(tree)
        raise


def background(scene,view_layer,*,render=False):
    return resolved(scene,view_layer).get('render_background' if render else 'viewport_background',dict(enabled=False,rgba=[1.,1.,1.,1.]))


def render_background(scene,view_layer,pixels):
    from .render_elements import with_background
    return with_background(pixels,background(scene,view_layer,render=True))
