# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic detached brush effect migration; source/native authors are immutable."""
import copy
from . import brush_effects as E,source_bindings

def migrate(document,recipe,updates):
    from .style import compile_preset
    compile_preset(document,recipe)
    bindings=source_bindings.recover(document)
    if type(updates) is not list or not 1<=len(updates)<=512:raise ValueError('Effects require 1..512 explicit actual updates')
    candidate=copy.deepcopy(document);rows=E.records(candidate,bindings['actuals']);seen=set()
    for update in updates:
        if type(update) is not dict or set(update)!={'actual','effects'}:raise ValueError('Effects update requires actual and effects')
        actual=update['actual']
        if actual not in bindings['actuals'] or actual in seen:raise ValueError('Unknown or duplicate effects actual')
        seen.add(actual)
        rows[actual]=E.checked(E.merge(rows.get(actual,E.defaults()),update['effects']))
    candidate['request']['brush_effects']=dict(schema=E.SCHEMA,brushes=rows)
    candidate['inputs']['projection']['editor_document']['revision']+=1
    plan=compile_preset(candidate,recipe)
    return candidate,plan,source_bindings.recover(candidate),dict(updated_actuals=sorted(seen),native_flags_changed=False,brush_effects_schema=E.SCHEMA)
