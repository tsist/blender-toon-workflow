# SPDX-License-Identifier: GPL-3.0-or-later
"""Load the four pure project dependencies, including a standalone package.

No historical drawing engine or installed addon is imported. Packaged copies
retain their source licenses and are pinned in the generated bundle manifest.
"""
import importlib.util
from pathlib import Path

_MODULES={}
_SOURCES={
    'smoothing_flags':'toon_image_cpu/smoothing_flags.py',
    'relative_size':'toon_image_cpu/relative_size.py',
    'paint_editor_state':'toon_image_session_ui_c4/paint_editor_state.py',
    'geometry_exact':'toon_image_backend/intersections/geometry_exact.py',
}

def load(name):
    if name not in _SOURCES:raise ValueError('Unknown preview dependency: '+name)
    if name not in _MODULES:
        root=Path(__file__).resolve().parent
        path=root/'_support'/(name+'.py')
        if not path.is_file():path=root.parent/_SOURCES[name]
        if not path.is_file():raise ImportError('Preview dependency missing: '+str(path))
        spec=importlib.util.spec_from_file_location(__name__+'_'+name,path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        _MODULES[name]=module
    return _MODULES[name]
