# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve the sibling Pencil backend without top-level module aliases."""
import importlib


def pencil(submodule=None):
    """Use the developer sibling or the real enclosing bundle namespace."""
    package = __package__
    prefix = ('toon_viewport_preview' if package == 'toon_workflow_addon'
              else package.rsplit('.', 1)[0] + '.toon_viewport_preview')
    return importlib.import_module(prefix + ('.' + submodule if submodule else ''))
