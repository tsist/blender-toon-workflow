# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve compositor ownership through legacy and scene-effect attachments."""


def active_binding(scene):
    effects = getattr(scene, 'compositor_effects', None)
    if effects is None:
        return None, scene.compositing_node_group
    effect = effects.active
    return effect, effect.node_group if effect else None


def scene_trees(scene):
    effects = getattr(scene, 'compositor_effects', None)
    if effects is None:
        tree = scene.compositing_node_group
        return (tree,) if tree else ()
    return tuple(dict.fromkeys(effect.node_group for effect in effects if effect.node_group))


def editable_trees(scene):
    return tuple(tree for tree in scene_trees(scene)
                 if tree.library is None and tree.is_editable and tree.users <= 1)


def attach_tree(scene, effect, tree):
    effects = getattr(scene, 'compositor_effects', None)
    if effects is None:
        scene.compositing_node_group = tree
        return
    if effect is not None:
        effect.node_group = tree
        return
    created = effects.new('Toon Lines')
    try:
        created.node_group = tree
    except Exception:
        effects.remove(created)
        raise


def new_tree(scene):
    import bpy
    tree = bpy.data.node_groups.new('自主线图合成', 'CompositorNodeTree')
    if hasattr(scene, 'compositor_effects'):
        # A newly added effect passes the previous image through while its
        # packed line image waits for an explicit downstream connection.
        for direction in ('INPUT', 'OUTPUT'):
            tree.interface.new_socket(name='Image', in_out=direction, socket_type='NodeSocketColor')
        source = tree.nodes.new('NodeGroupInput')
        destination = tree.nodes.new('NodeGroupOutput')
        tree.links.new(source.outputs['Image'], destination.inputs['Image'])
    return tree
