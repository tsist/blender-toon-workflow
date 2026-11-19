# SPDX-License-Identifier: GPL-3.0-or-later
"""Save per-space preview visibility on its Screen; bind only live owned spaces.

Area/space indices locate a saved Blender layout only when reopening. While the
file is open, RNA pointer identity follows a space through layout reordering.
Invalid, duplicate or missing saved targets are never rebound to another space.
"""
import uuid
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty,CollectionProperty,IntProperty

PROP='toon_viewport_scopes'
MAX_ROWS=128
_bindings={}
_descriptor=None
_callbacks=[]

class ViewportScope(bpy.types.PropertyGroup):
    saved_area_index:IntProperty(default=-1,options=set())
    saved_space_index:IntProperty(default=-1,options=set())
    enabled:BoolProperty(default=True,options=set())

def _key(area,space):return (area.as_pointer(),space.as_pointer())

def _restore_screen(screen):
    rows=getattr(screen,PROP);result={};duplicates=set()
    if len(rows)>MAX_ROWS:return result
    names=[row.name for row in rows]
    for row in rows:
        if not row.name or names.count(row.name)!=1:continue
        ai,si=row.saved_area_index,row.saved_space_index
        if not 0<=ai<len(screen.areas):continue
        area=screen.areas[ai]
        if not 0<=si<len(area.spaces):continue
        space=area.spaces[si]
        if space.type!='VIEW_3D':continue
        key=_key(area,space)
        if key in result:duplicates.add(key)
        else:result[key]=row.name
    for key in duplicates:result.pop(key,None)
    return result

def _map(screen):
    pointer=screen.as_pointer()
    if pointer not in _bindings:_bindings[pointer]=_restore_screen(screen)
    return _bindings[pointer]

def current(screen,area,space):
    if screen is None or area is None or space is None or space.type!='VIEW_3D':return None
    rows=getattr(screen,PROP,None)
    if rows is None:return None
    name=_map(screen).get(_key(area,space))
    return rows.get(name) if name is not None else None

def enabled(screen,area,space):
    row=current(screen,area,space)
    return True if row is None else bool(row.enabled)

def set_enabled(screen,area,space,value):
    if type(value) is not bool:raise ValueError('Viewport visibility must be boolean')
    if screen.library is not None or not screen.is_editable:raise ValueError('Screen is read-only')
    ai=next((i for i,a in enumerate(screen.areas) if a==area),None)
    if ai is None:raise ValueError('Viewport area does not belong to this Screen')
    si=next((i for i,s in enumerate(area.spaces) if s==space),None)
    if si is None or space.type!='VIEW_3D':raise ValueError('Expected a VIEW_3D space owned by this area')
    row=current(screen,area,space)
    if row is None:
        rows=getattr(screen,PROP)
        if len(rows)>=MAX_ROWS:raise ValueError('Too many saved viewport settings on this Screen')
        row=rows.add();row.name=uuid.uuid4().hex
        _map(screen)[_key(area,space)]=row.name
    row.saved_area_index=ai;row.saved_space_index=si;row.enabled=value
    area.tag_redraw()
    return row

@persistent
def clear(*unused):_bindings.clear()

@persistent
def restore(*unused):
    _bindings.clear()
    for screen in bpy.data.screens:_bindings[screen.as_pointer()]=_restore_screen(screen)

@persistent
def save(*unused):
    for screen in bpy.data.screens:
        if screen.library is not None or not screen.is_editable:continue
        rows=getattr(screen,PROP);mapping=_map(screen)
        # Invalidate departed spaces before recording current live identities.
        for row in rows:row.saved_area_index=-1;row.saved_space_index=-1
        live={}
        for ai,area in enumerate(screen.areas):
            for si,space in enumerate(area.spaces):
                name=mapping.get(_key(area,space));row=rows.get(name) if name is not None else None
                if row is not None and space.type=='VIEW_3D':
                    row.saved_area_index=ai;row.saved_space_index=si;live[_key(area,space)]=name
        _bindings[screen.as_pointer()]=live

class TOONVIEWPORT_OT_local_visibility(bpy.types.Operator):
    bl_idname='toon_viewport.local_visibility'
    bl_label='切换此视口描边'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls,context):
        return context.screen is not None and context.area is not None and context.area.type=='VIEW_3D' and context.space_data.type=='VIEW_3D'
    def execute(self,context):
        try:set_enabled(context.screen,context.area,context.space_data,not enabled(context.screen,context.area,context.space_data))
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}

CLASSES=(ViewportScope,TOONVIEWPORT_OT_local_visibility)

def install():
    global _descriptor
    if hasattr(bpy.types.Screen,PROP):raise RuntimeError('Screen viewport settings already owned by another registration')
    try:
        setattr(bpy.types.Screen,PROP,CollectionProperty(type=ViewportScope))
        _descriptor=bpy.types.Screen.bl_rna.properties[PROP].as_pointer()
        for collection,callback in ((bpy.app.handlers.load_pre,clear),(bpy.app.handlers.load_post,restore),
                                    (bpy.app.handlers.save_pre,save),(bpy.app.handlers.undo_post,restore),(bpy.app.handlers.redo_post,restore)):
            collection.append(callback);_callbacks.append((collection,callback))
        # Blender enables extensions inside RestrictBlend: data access is not
        # available during register(). Load handlers or the first live lookup
        # restore saved ownership after registration has returned.
        clear()
    except Exception:uninstall();raise

def uninstall(*,persist=False):
    global _descriptor
    if persist and _descriptor is not None:save()
    for collection,callback in reversed(_callbacks):
        if callback in collection:collection.remove(callback)
    _callbacks.clear();_bindings.clear()
    prop=bpy.types.Screen.bl_rna.properties.get(PROP)
    if prop is not None and prop.as_pointer()==_descriptor:delattr(bpy.types.Screen,PROP)
    _descriptor=None
