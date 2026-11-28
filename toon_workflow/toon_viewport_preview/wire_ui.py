# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit wire author transactions and compact group controls."""
import copy
import json
import uuid
from contextlib import ExitStack

import bpy
from bpy.props import BoolProperty, StringProperty

from . import chain_ui, chain_controls, map_amount_ui, map_amount_controls
from . import dash_ui, dash_controls, amount_animation, curve_editor
from . import source_bindings, wire_authoring, wire_sources


def _require(scene):
    settings = scene.toon_viewport_preview
    if scene.library is not None or not scene.is_editable:
        raise ValueError('Linked Scene wire sources are read-only')
    text = settings.preset_text
    if text is None or text.library is not None or not text.get('toon_viewport_preview_owned'):
        raise ValueError('Wire authoring requires an owned local preset document')
    return settings


def _invalidate(scene):
    from . import addon
    key = scene.as_pointer()
    for stamp in list(addon._style_cache):
        if stamp[0] == key:
            addon._style_cache.pop(stamp)
    addon._chain_input_cache.pop(key, None)
    for cache in (chain_ui._BINDINGS, chain_ui._SYNC, map_amount_ui._SYNC,
                  map_amount_ui._RUNTIME, dash_ui._SYNC, dash_ui._RUNTIME):
        cache.pop(key, None)


def set_sources(scene, updates):
    return apply_migration(scene, updates, wire_authoring.migrate)


def apply_migration(scene, updates, migrate, *, wrapped_migration=False, lifecycle=False):
    """Prepare document, exact tables and proxy rows before one guarded commit.

    Curve edits are committed to their existing author first. That independent
    user edit remains protected if the subsequent wire import is rejected.
    Amount-track collections and Actions are never rewritten by this operation.
    """
    from . import addon
    settings = _require(scene)
    tree = settings.curve_tree
    if (settings.preset_text.users > 1 and tree is not None
            and tree.get('toon_viewport_curve_role') == curve_editor.ROLE
            and curve_editor.signature(tree, settings) != tree.get('last_signature')):
        raise ValueError('共享样式存在待提交曲线，请先在独立样式副本中完成编辑')
    curve_editor.commit_pending(scene)
    previous_plan = addon._preset(scene)
    wrapped = json.loads(settings.preset_text.as_string())
    old_bindings = source_bindings.recover(wrapped['document'])
    if wrapped_migration:
        new_wrapped, plan, bindings, report = migrate(wrapped, updates)
        document = new_wrapped['document']
    else:
        document, plan, bindings, report = migrate(wrapped['document'], wrapped['recipe'], updates)
        new_wrapped = copy.deepcopy(wrapped)
        new_wrapped['document'] = document
    amount_animation.validate_registry(scene, bindings['actuals'])
    from . import parameter_animation
    parameter_animation.validate_registry(scene,document)
    old_chain = chain_ui._decode(settings.chain_controls_json) if settings.chain_controls_json else None
    if old_chain is not None:
        old_chain = chain_controls.normalize_controls(old_bindings, old_chain)
    chains = chain_controls.merge_controls(bindings, previous=old_chain)
    old_amount = (chain_ui._decode(settings.map_amounts_json) if settings.map_amounts_json
                  else map_amount_controls.defaults(old_bindings))
    old_amount = map_amount_controls.normalize(old_bindings, old_amount)
    # A previously absent amount table meant effect1, not a native import.
    amounts = map_amount_controls.merge(bindings, old_amount, document)
    old_dash = chain_ui._decode(settings.dash_patterns_json) if settings.dash_patterns_json else None
    if old_dash is not None:
        old_dash = dash_controls.normalize(old_bindings, old_dash)
    dashes = dash_controls.merge(bindings, old_dash)
    chain_rows = chain_ui._display_rows(scene, bindings, chains)
    amount_rows = map_amount_ui._display(bindings, amounts)
    dash_rows = dash_ui._display(bindings, dashes)
    raws = (chain_ui._canonical(chains), chain_ui._canonical(amounts), chain_ui._canonical(dashes))
    group_change = None
    if lifecycle:
        from . import group_ui
        group_change = group_ui.prepare(scene, document, plan, report)
    body = json.dumps(new_wrapped, ensure_ascii=False, indent=2, allow_nan=False)
    old = dict(text=settings.preset_text, revision=settings.preset_revision,
        chain=settings.chain_controls_json, amount=settings.map_amounts_json, dash=settings.dash_patterns_json,
        chain_rows=chain_ui._snapshot(settings), amount_rows=map_amount_ui._rows(settings),
        dash_rows=dash_ui._rows(settings), errors=(settings.chain_error, settings.map_amount_error, settings.dash_error))
    text = bpy.data.texts.new('.toon_viewport_style.'+uuid.uuid4().hex)
    # A failed transaction must leave no fake-user Text behind after rollback.
    text.use_fake_user = False
    try:
        text['toon_viewport_preview_owned'] = True
        text['toon_viewport_preview_reclaimable'] = True
        text.from_string(body)
        with ExitStack() as guards:
            for module in (chain_ui, map_amount_ui, dash_ui):
                guards.enter_context(module._guard(scene))
            settings.preset_text = text
            settings.preset_revision = old['revision']+1
            chain_ui._apply_rows(settings, chain_rows, rebuild=True)
            map_amount_ui._apply(settings, amount_rows)
            dash_ui._apply(settings, dash_rows)
            settings.chain_controls_json, settings.map_amounts_json, settings.dash_patterns_json = raws
            if group_change is not None:group_ui.apply(settings, group_change[1])
    except Exception:
        with ExitStack() as guards:
            for module in (chain_ui, map_amount_ui, dash_ui):
                guards.enter_context(module._guard(scene))
            settings.preset_text = old['text']
            settings.preset_revision = old['revision']
            settings.chain_controls_json = old['chain']
            settings.map_amounts_json = old['amount']
            settings.dash_patterns_json = old['dash']
            chain_ui._apply_rows(settings, old['chain_rows'], rebuild=True)
            map_amount_ui._apply(settings, old['amount_rows'])
            dash_ui._apply(settings, old['dash_rows'])
            settings.chain_error, settings.map_amount_error, settings.dash_error = old['errors']
            if group_change is not None:group_ui.apply(settings, group_change[0])
        _invalidate(scene)
        addon._style_cache[(scene.as_pointer(), old['text'].as_pointer(), old['revision'])] = previous_plan
        if text.users == 0:
            bpy.data.texts.remove(text)
        raise
    _invalidate(scene)
    addon._style_cache[(scene.as_pointer(), text.as_pointer(), settings.preset_revision)] = plan
    settings.chain_error = settings.map_amount_error = settings.dash_error = ''
    # Transaction succeeded. Disposal of the now-stale curve helper is cleanup,
    # distinct from committing the durable author document and exact tables.
    try:
        curve_editor.detach(scene)
    except Exception as error:
        report['cleanup_warning'] = str(error)
    addon._touch(settings, bpy.context)
    return report


class TOONVIEWPORT_OT_wire_sources(bpy.types.Operator):
    bl_idname = 'toon_viewport.wire_sources'
    bl_label = '设置本组线框来源'
    bl_options = {'REGISTER', 'UNDO'}
    group: StringProperty(options={'HIDDEN'})
    enabled: BoolProperty(default=True)
    visible: BoolProperty(default=True)
    hidden: BoolProperty(default=True)

    def execute(self, context):
        try:
            set_sources(context.scene, [dict(group=self.group, enabled=self.enabled,
                                           visible=self.visible, hidden=self.hidden)])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def draw(layout, plan, gid, *, channel=None):
    if next(g for g in plan['groups'] if g['id']==gid).get('authored'):
        return
    rows = wire_sources.records(plan['raw'])
    record = rows.get(gid)
    active = record is not None and record['enabled']
    op = layout.operator('toon_viewport.wire_sources', text='恢复原线源' if active else '使用原始边线框',
                         depress=active)
    op.group = gid
    op.enabled = not active
    op.visible = record['visible'] if record else True
    op.hidden = record['hidden'] if record else True
    if active:
        row = layout.row(align=True)
        for key, label in (('visible', '可见线'), ('hidden', '隐藏线')):
            if channel is not None and (key=='visible')!=(channel=='VISIBLE'):continue
            op = row.operator('toon_viewport.wire_sources', text=label, depress=record[key])
            op.group = gid
            op.enabled = True
            op.visible = not record['visible'] if key == 'visible' else record['visible']
            op.hidden = not record['hidden'] if key == 'hidden' else record['hidden']


CLASSES = (TOONVIEWPORT_OT_wire_sources,)
