# SPDX-License-Identifier: GPL-3.0-or-later
"""Mother leases plus one independent owner at Pencil's public package seam.

Only package.register/unregister are mediated while a mother lease exists.
Private addon entry points and replacement RNA descriptors are outside this
contract. The independently registered owner is a boolean, matching Pencil's
idempotent public registration; repeated register calls do not create counts.
"""

_state = None
_PROP = 'toon_viewport_preview'


def _descriptor(scene_type):
    value = scene_type.bl_rna.properties.get(_PROP)
    return value.as_pointer() if value is not None else None


class _Ownership:
    def __init__(self, package, scene_type, external):
        self.package = package
        self.scene_type = scene_type
        self.external = external
        self.owners = set()
        self.original_register = package.register
        self.original_unregister = package.unregister
        self.descriptor = _descriptor(scene_type)
        # Store bound method objects so identity checks also protect callbacks
        # replaced by another integration while our leases were active.
        self.register_entry = self.register
        self.unregister_entry = self.unregister

    def matches(self):
        return self.descriptor is not None and _descriptor(self.scene_type) == self.descriptor

    def register(self):
        if not self.matches():
            raise RuntimeError('Pencil RNA 已改变，不能沿用原注册所有权')
        self.external = True

    def unregister(self):
        if not self.matches():
            raise RuntimeError('Pencil RNA 已改变，不能注销另一注册实例')
        self.external = False
        # The mother lease keeps the production core, classes and timers alive.

    def restore(self):
        if self.package.register is self.register_entry:
            self.package.register = self.original_register
        if self.package.unregister is self.unregister_entry:
            self.package.unregister = self.original_unregister


def acquire(owner, *, package=None, scene_type=None):
    """Acquire an idempotent mother lease; optional inputs enable CPU tests."""
    global _state
    if _state is not None:
        if package is not None and package is not _state.package:
            raise RuntimeError('Pencil package 与现有母工作台 lease 不同')
        if scene_type is not None and scene_type is not _state.scene_type:
            raise RuntimeError('Pencil Scene 类型与现有 lease 不同')
        if not _state.matches():
            raise RuntimeError('Pencil RNA 已改变，不能扩展原注册 lease')
        _state.owners.add(owner)
        return
    if package is None:
        from .backend import pencil
        package = pencil()
    if scene_type is None:
        import bpy
        scene_type = bpy.types.Scene
    external = _descriptor(scene_type) is not None
    state = _Ownership(package, scene_type, external)
    if not external:
        state.original_register()
        state.descriptor = _descriptor(scene_type)
    if state.descriptor is None:
        raise RuntimeError('Pencil 注册后缺少 Scene.toon_viewport_preview')
    state.owners.add(owner)
    package.register = state.register_entry
    package.unregister = state.unregister_entry
    _state = state


def release(owner):
    """Release only this lease and only tear down our unchanged descriptor."""
    global _state
    state = _state
    if state is None or owner not in state.owners:
        return
    state.owners.remove(owner)
    if state.owners:
        return
    try:
        if not state.external and state.matches():
            state.original_unregister()
    finally:
        state.restore()
        _state = None
