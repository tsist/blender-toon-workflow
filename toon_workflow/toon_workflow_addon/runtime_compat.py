# SPDX-License-Identifier: GPL-3.0-or-later
"""Build identities and scoped native compositor capture for the Toon worker."""
from contextlib import contextmanager
import re


from .host_compat import MINIMUM_BLENDER_VERSION as MINIMUM_JOINT_VERSION, is_supported_host


def _require_joint_identity(version, build_hash):
    if not isinstance(build_hash, str) or not re.fullmatch(r'[0-9a-f]{7,64}', build_hash):
        raise ValueError('Toon joint renderer requires a hexadecimal Blender build hash')
    if not is_supported_host(version):
        raise ValueError(f'Unsupported Toon joint renderer build: {(version, build_hash)}')


def require_joint_build(app):
    _require_joint_identity(tuple(app.version), app.build_hash.decode('ascii', errors='replace'))


def require_joint_receipt(host):
    version = host['version']
    if not isinstance(version, str) or len(version) > 80:
        raise ValueError('Native-media receipt requires a Blender version')
    parsed = re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?: [A-Za-z0-9 -]+)?', version)
    if parsed is None:
        raise ValueError('Native-media receipt requires a Blender version')
    _require_joint_identity(tuple(map(int, parsed.groups())), host['build_hash'])


def signature_inputs(node):
    """Keep old artist signatures for the inactive, pristine 5.3 light index."""
    if node.bl_idname != 'ShaderNodeAttribute' or node.attribute_type == 'LIGHT':
        return node.inputs
    return tuple(socket for socket in node.inputs
                 if not (socket.identifier == 'LightIndex' and socket.is_unavailable
                         and not socket.is_linked and socket.default_value == 0))


@contextmanager
def capture_effect(scene, tree):
    """Evaluate just the private capture tree and restore the artist's stack."""
    effects = getattr(scene, 'compositor_effects', None)
    if effects is None:
        original = scene.compositing_node_group
        try:
            scene.compositing_node_group = tree
            yield
        finally:
            scene.compositing_node_group = original
        return

    active = effects.active
    flags = [(effect, effect.enable_for_render) for effect in effects]
    if flags and active is None:
        raise ValueError('Cannot restore a nonempty effect stack without an active item')
    temporary = effects.new(tree.name)
    try:
        temporary.node_group = tree
        temporary.enable_for_render = True
        for effect, _ in flags:
            effect.enable_for_render = False
        yield
    finally:
        effects.remove(temporary)
        for effect, enabled in flags:
            effect.enable_for_render = enabled
        if active is not None:
            effects.active = active
