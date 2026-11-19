# SPDX-License-Identifier: GPL-3.0-or-later
"""Owning-thread Blender holdout capture; no RNA retained by consumers."""
def masked(obj,view_layer,parent=None):
    def state(block):
        original=block.original
        return bool(original.is_holdout or original.holdout_get(view_layer=view_layer))
    return state(obj) or bool(parent is not None and state(parent))

def viewport_enabled(context):
    space=getattr(context,'space_data',None)
    return bool(space is not None and space.type=='VIEW_3D' and space.shading.type in ('MATERIAL','RENDERED'))

def active(geometry):return any(getattr(mesh,'holdout',False) for mesh in geometry.meshes)
