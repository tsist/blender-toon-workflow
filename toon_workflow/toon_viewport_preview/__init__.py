# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in geometric 2D viewport preview; historical backends remain unchanged."""

bl_info = {
    'name': '二维描边 · 自动视口预览', 'author': 'Blender AI',
    'version': (0, 2, 27), 'blender': (5, 2, 0), 'category': '3D View',
}

def register():
    from .addon import register as action
    action()

def unregister():
    from .addon import unregister as action
    action()
