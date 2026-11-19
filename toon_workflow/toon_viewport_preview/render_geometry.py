# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture mesh instances from the actual RENDER depsgraph for line drawing.

The caller supplies the depsgraph observed inside a render-engine callback.
This module does not render, mutate scene data, or accept a non-RENDER
depsgraph as a fallback.
"""
import time

import numpy as np

from .geometry import Geometry, Mesh


def _instance_key(instance, source_name):
    """Return primitive identity stable across an object_instances iteration."""
    parent = instance.parent
    raw_id = instance.persistent_id
    try:
        persistent_id = tuple(int(value) for value in raw_id)
    except TypeError:
        persistent_id = (int(raw_id),)
    # Direct object instances share an all-zero persistent id, so source_name
    # is required to distinguish them. It is part of the key, not a dedup key.
    return (None if parent is None else parent.original.name,
            persistent_id, bool(instance.is_instance), source_name)


def capture(depsgraph):
    """Synchronously capture every shown RENDER mesh instance as a ``Mesh``."""
    if depsgraph.mode != 'RENDER':
        raise ValueError('Render geometry capture requires a RENDER depsgraph')

    result = Geometry.__new__(Geometry)
    result.evaluation = 'RENDER'
    result.meshes = []
    result.names = []
    result.capture_report = []
    result.capture_seconds = 0.
    result.reference_points={obj.original.name:np.array(obj.matrix_world.translation,np.float64) for obj in depsgraph.objects}
    offset = 1
    from .selector_capture import Context
    selector_context=Context()

    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type != 'MESH' or not instance.show_self:
            continue

        # The depsgraph instance is temporary; copy its transform now and keep
        # only primitive/array data in the returned Geometry.
        matrix_world = np.array(instance.matrix_world, dtype=np.float64,
                                copy=True)
        source_name = obj.original.name
        instance_key = _instance_key(instance, source_name)
        # Evaluated object slots include both Geometry Nodes material additions
        # and object-linked overrides. mesh.materials alone loses the overrides.
        material_names = [slot.material.name if slot.material else ''
                          for slot in obj.material_slots]
        started = time.perf_counter()
        row = Mesh(obj.original, depsgraph, matrix_world=matrix_world,
                   source_name=source_name, instance_key=instance_key,
                   material_names=material_names,selector_context=selector_context,selector_parent=instance.parent,
                   material_blocks=[slot.material for slot in obj.material_slots])
        from .holdout import masked
        row.holdout=masked(obj,depsgraph.view_layer,instance.parent)

        # Reserve at least one identity slot so repeated empty instances also
        # remain distinguishable by face_offset.
        row.face_offset = offset
        offset += max(1, len(row.triangles))
        if offset >= 2**24:
            raise ValueError(
                'Render polygon identity exceeds exact float32 integer range')
        result.capture_seconds += time.perf_counter() - started

        result.meshes.append(row)
        result.names.append(source_name)
        result.capture_report.append(dict(
            instance_key=instance_key,
            source_name=source_name,
            matrix_world=matrix_world.tolist(),
            triangles=int(len(row.triangles)),
            material_names=material_names,
            type=obj.type,
            holdout=row.holdout,
        ))

    result.names = tuple(result.names)
    result.triangle_count = sum(len(row.triangles) for row in result.meshes)
    result.nonmanifold_count = sum(row.nonmanifold_count for row in result.meshes)
    return result
