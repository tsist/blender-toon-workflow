# SPDX-License-Identifier: GPL-3.0-or-later
"""Delegate Pencil UI through the mother while retaining independent owners.

Only Python Panel poll methods of the acquired backend are temporarily wrapped;
source files, operators, RNA, author state and draw algorithms are unchanged.
"""
_polls=[]
_DELEGATED={'TOONVIEWPORT_PT_preview','TOONVIEWPORT_PT_author_files',
    'TOONVIEWPORT_PT_lines','TOONPARAM_PT_animation','TOONVIEWPORT_PT_elements',
    'TOONVIEWPORT_PT_sequence','TOONVECTOR_PT_output'}


def install():
    if _polls:
        return
    import bpy
    from .backend import pencil
    from . import joint_ownership
    namespace=pencil().__name__+'.'
    pending=list(bpy.types.Panel.__subclasses__());seen=set()
    while pending:
        cls=pending.pop()
        if cls in seen:continue
        seen.add(cls);pending.extend(cls.__subclasses__())
        if (not cls.__module__.startswith(namespace) or cls.__name__ not in _DELEGATED
                or getattr(cls,'bl_space_type',None)!='VIEW_3D'):continue
        previous=cls.__dict__.get('poll')
        original=getattr(cls,'poll',None)
        def poll(panel,context,_original=original):
            state=joint_ownership._state
            if state is not None and state.owners and not state.external:
                return False
            return bool(_original(context)) if _original is not None else True
        replacement=classmethod(poll)
        setattr(cls,'poll',replacement)
        _polls.append((cls,previous,replacement))


def uninstall():
    for cls,previous,replacement in reversed(_polls):
        if cls.__dict__.get('poll') is not replacement:
            continue
        if previous is None:delattr(cls,'poll')
        else:setattr(cls,'poll',previous)
    _polls.clear()
