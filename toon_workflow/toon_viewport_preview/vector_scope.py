# SPDX-License-Identifier: GPL-3.0-or-later
"""Real independent vector state on Scenes, ViewLayers and local Image nodes.

Decimal node pointers are current-session addresses only. UUID tokens survive
save/reopen. Invalid explicit targets never fall back to the legacy Scene.
"""
import re
import uuid
import bpy
from bpy.props import PointerProperty
from .compositor_scope import editable_trees, scene_trees

PROP='toon_vector_outputs'
UUID_KEY='toon_vector_output_uuid'
_installed=[]
_legacy_descriptor=None


class VectorScopeError(ValueError):pass


def need(value,message):
    if not value:raise VectorScopeError(message)


def preflight():
    for kind in (bpy.types.ViewLayer,bpy.types.CompositorNodeImage):
        need(not hasattr(kind,PROP),kind.__name__+'.'+PROP+' is already owned by another registration')


def register(state_type):
    global _legacy_descriptor
    preflight();installed=[]
    try:
        for kind in (bpy.types.ViewLayer,bpy.types.CompositorNodeImage):
            setattr(kind,PROP,PointerProperty(type=state_type))
            installed.append((kind,kind.bl_rna.properties[PROP].as_pointer()))
    except Exception:
        for kind,pointer in reversed(installed):
            prop=kind.bl_rna.properties.get(PROP)
            if prop is not None and prop.as_pointer()==pointer:delattr(kind,PROP)
        raise
    _installed.extend(installed)
    legacy=bpy.types.Scene.bl_rna.properties.get(PROP)
    _legacy_descriptor=legacy.as_pointer() if legacy is not None else None


def unregister():
    global _legacy_descriptor
    for kind,pointer in reversed(_installed):
        prop=kind.bl_rna.properties.get(PROP)
        # A later owner may have replaced this descriptor. Never remove it.
        if prop is not None and prop.as_pointer()==pointer:delattr(kind,PROP)
    _installed.clear()
    _legacy_descriptor=None


def _require_property(kind):
    expected=_legacy_descriptor if kind==bpy.types.Scene else next((pointer for owner,pointer in _installed if owner==kind),None)
    current=kind.bl_rna.properties.get(PROP)
    need(expected is not None and current is not None and current.as_pointer()==expected,'Vector scope property is unregistered or its owner changed')


def scene_local(scene):
    need(isinstance(scene,bpy.types.Scene),'Vector scope requires a Blender Scene')
    need(scene.library is None and scene.is_editable,'Vector scope requires an editable local Scene')


def resolve_layer(scene,view_layer):
    if view_layer is None:return None
    if isinstance(view_layer,str):
        layer=scene.view_layers.get(view_layer)
        need(layer is not None,'Unknown ViewLayer: '+view_layer)
        return layer
    need(isinstance(view_layer,bpy.types.ViewLayer),'ViewLayer must be its exact name or a Blender ViewLayer')
    try:pointer=view_layer.as_pointer()
    except ReferenceError as error:raise VectorScopeError('Expired ViewLayer reference') from error
    layer=next((candidate for candidate in scene.view_layers if candidate.as_pointer()==pointer),None)
    need(layer is not None,'ViewLayer does not belong to the requested Scene')
    return layer


def _local_images(scene):
    return [node for tree in editable_trees(scene) for node in tree.nodes
            if node.bl_idname == 'CompositorNodeImage']


def _own_node(scene, node):
    scene_local(scene)
    need(node is not None and node.bl_idname == 'CompositorNodeImage' and node.id_data in scene_trees(scene),
         'Image node does not belong to this Scene compositor')
    need(node.id_data in editable_trees(scene), 'Linked or shared compositor requires an independent local copy')


def node_token(scene,node):
    _own_node(scene,node)
    value=node.get(UUID_KEY)
    if value is None:value=uuid.uuid4().hex;node[UUID_KEY]=value
    need(type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None,'Invalid persistent vector node UUID')
    need(sum(candidate.get(UUID_KEY)==value for candidate in _local_images(scene))==1,'Ambiguous duplicate vector node UUID')
    return 'uuid:'+value


def node_address(scene,node):
    """Read-only UI address; execute paths assign durable UUIDs when needed."""
    _own_node(scene,node)
    return node_token(scene,node) if node.get(UUID_KEY) is not None else str(node.as_pointer())


def resolve_node(scene,node_ptr):
    scene_local(scene)
    need(type(node_ptr) is str and 0<len(node_ptr)<=96,'Explicit Image node address must be a bounded string')
    images=_local_images(scene)
    if re.fullmatch('[1-9][0-9]{0,23}',node_ptr):
        pointer=int(node_ptr);need(pointer<2**64,'Invalid node pointer address')
        matches=[node for node in images if node.as_pointer()==pointer]
    elif re.fullmatch('uuid:[0-9a-f]{32}',node_ptr):
        matches=[node for node in images if node.get(UUID_KEY)==node_ptr[5:]]
    else:raise VectorScopeError('Unknown node address format; use a decimal session pointer or uuid:<32hex>')
    need(len(matches)==1,'Unknown, foreign, expired or ambiguous Image node address')
    node=matches[0];node_address(scene,node)
    return node


def resolve(scene,*,view_layer=None,node_ptr='',create_token=False):
    scene_local(scene);need(type(node_ptr) is str,'node_ptr must be a string')
    layer=resolve_layer(scene,view_layer)
    if node_ptr:
        _require_property(bpy.types.CompositorNodeImage)
        node=resolve_node(scene,node_ptr);state=getattr(node,PROP,None)
        address=node_token(scene,node) if create_token else node_address(scene,node)
        info=dict(kind='IMAGE_NODE',scene=scene.name,node=node.name,node_token=address,view_layer=layer.name if layer else None)
    elif layer is not None:
        _require_property(bpy.types.ViewLayer)
        state=getattr(layer,PROP,None);info=dict(kind='VIEW_LAYER',scene=scene.name,view_layer=layer.name)
    else:
        _require_property(bpy.types.Scene)
        state=getattr(scene,PROP,None);info=dict(kind='SCENE_LEGACY',scene=scene.name,view_layer=None)
    need(state is not None,'Vector properties are not registered for the requested owner')
    return state,layer,info


def states(scene):
    """Owner lookup uses real embedded RNA, not unsupported path_from_id()."""
    state=getattr(scene,PROP,None)
    if state is not None:yield state,dict(kind='SCENE_LEGACY',scene=scene.name,view_layer=None)
    for layer in scene.view_layers:
        state=getattr(layer,PROP,None)
        if state is not None:yield state,dict(kind='VIEW_LAYER',scene=scene.name,view_layer=layer.name)
    for node in _local_images(scene):
        state=getattr(node,PROP,None)
        if state is not None:yield state,dict(kind='IMAGE_NODE',scene=scene.name,node=node.name,node_token='uuid:'+node[UUID_KEY] if node.get(UUID_KEY) else None,view_layer=None)


def owner_for_item(item):
    pointer=item.as_pointer();data=item.id_data
    for scene in bpy.data.scenes:
        if data!=scene and data not in editable_trees(scene):continue
        for state,info in states(scene):
            if not hasattr(state,'vector_outputs'):continue
            for index,candidate in enumerate(state.vector_outputs):
                if candidate.as_pointer()==pointer:return scene,state,index,info
    return None,None,None,None
