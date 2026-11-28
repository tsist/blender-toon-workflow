"""Import the fixed support modules embedded in the Toon package."""
from importlib import import_module

_NAMES = {"smoothing_flags", "relative_size", "paint_editor_state", "geometry_exact"}


def load(name):
    if name not in _NAMES:
        raise ValueError("Unknown preview dependency: " + name)
    return import_module("." + name, __package__ + "._support")
