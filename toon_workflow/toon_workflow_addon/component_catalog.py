# SPDX-License-Identifier: GPL-3.0-or-later
"""Small common entry for surface styling and native optical media.

Native media capability does not grant admission to the opaque joint renderer.
Its Beauty remains available to native rendering/quick-preview; optical ink
integration is separately qualified.
"""
from . import style_components as styles,media_components as media
MEDIA_STYLES={'ALPHA_SHEET','GLASS','WATER','SMOKE','EMISSION'}

def _provider(settings):
    if type(settings) is not dict:raise ValueError('组件参数必须为明确文档')
    return media if settings.get('style') in MEDIA_STYLES else styles

def defaults(style):
    settings=(media if style in MEDIA_STYLES else styles).defaults(style)
    if style=='MANGA':settings.update(ink_tone=.004,light_gain=1.)
    return settings

def normalize(settings):return _provider(settings).normalize(settings)

def build(bpy,settings,name):return _provider(settings).build(bpy,settings,name)

def inspect(material):
    try:
        provider=styles if material is not None and material.get(styles.PROFILE_KEY) is not None else media
        state=provider.inspect(material)
        if 'status' in state:return state
        # Original component study reports validity/actual/declaration; keep
        # its richer diagnostic format behind this small common adapter.
        return dict(status='valid' if state['valid'] else 'conflict',
                    settings=state['declaration'] if state['valid'] else None,
                    issues=[r['message'] for r in state['conflicts'] if r['severity']=='error'])
    except (ValueError,TypeError,KeyError,AttributeError,ReferenceError) as error:
        return dict(status='conflict',settings=None,issues=[str(error)])

def update(material,settings):
    _provider(settings).update(material,settings)
    return inspect(material)
