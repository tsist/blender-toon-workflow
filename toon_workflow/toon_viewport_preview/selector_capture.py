# SPDX-License-Identifier: GPL-3.0-or-later
"""Owning-thread capture of object/library, collection ancestry and material IDs."""
import os
import bpy
import numpy as np
from .selector_bindings import key

def reference(block):
    block=block.original
    library=block.library
    path=None if library is None else os.path.normcase(os.path.realpath(bpy.path.abspath(library.filepath,library=library.parent)))
    return dict(name=block.name,library=path)

class Context:
    def __init__(self):
        self.parents={collection:[] for collection in bpy.data.collections}
        for parent in bpy.data.collections:
            for child in parent.children:self.parents.setdefault(child,[]).append(parent)
    def metadata(self,obj,materials,face_material,parent=None):
        objects=[obj.original]
        if parent is not None and parent.original not in objects:objects.append(parent.original)
        collections=set()
        def visit(collection):
            if collection in collections or collection not in self.parents:return
            collections.add(collection)
            for ancestor in self.parents[collection]:visit(ancestor)
        for source in objects:
            for collection in source.users_collection:visit(collection)
        palette=[key(reference(material)) if material is not None else '' for material in materials]
        return dict(objects=[key(reference(source)) for source in objects],
            collections=sorted(key(reference(collection)) for collection in collections),
            materials=np.asarray([palette[i] if 0<=i<len(palette) else '' for i in face_material],dtype=str))
