# SPDX-License-Identifier: GPL-3.0-or-later
"""Hide embedded Pencil sidebar panels while the unified workbench owns their UI."""

_detached = []


def install():
    import bpy
    from .backend import pencil

    for cls in pencil("addon")._CLASSES:
        if (issubclass(cls, bpy.types.Panel) and cls.is_registered
                and cls.bl_space_type == "VIEW_3D" and cls.bl_region_type == "UI"):
            bpy.utils.unregister_class(cls)
            _detached.append(cls)


def uninstall():
    import bpy

    while _detached:
        cls = _detached[0]
        if not cls.is_registered:
            bpy.utils.register_class(cls)
        _detached.pop(0)
