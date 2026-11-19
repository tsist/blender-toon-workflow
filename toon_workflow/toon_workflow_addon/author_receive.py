# SPDX-License-Identifier: GPL-3.0-or-later
"""One synchronous, reversible receive into Scene.toon_viewport_preview.

The caller owns candidate provenance and its waiting-period conflict checks.
This module owns schema dispatch, native author rollback, and the final check.
It never opens a blend, edits animation, or replaces a Scene/mesh/material.
"""
import copy
from contextlib import ExitStack
from pathlib import Path
import re

from .backend import pencil
from . import joint_basic_style, joint_workflow
from .workflow_contract import fingerprint

SCHEMA = 'toon-author-receive.v1'


def _plain(value):
    if hasattr(value, 'to_dict'):
        return {key:_plain(item) for key,item in value.to_dict().items()}
    if hasattr(value, 'to_list'):
        return [_plain(item) for item in value.to_list()]
    if isinstance(value, dict):
        return {key:_plain(item) for key,item in value.items()}
    if isinstance(value, (list,tuple)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _custom(block, excluded=()):
    return {key:_plain(block[key]) for key in block.keys() if key not in excluded}


def _restore_custom(block, values, excluded=()):
    for key in list(block.keys()):
        if key not in excluded and key not in values:
            del block[key]
    for key,value in values.items():
        if key not in block or _plain(block[key]) != value:
            block[key] = copy.deepcopy(value)


def _capture_rna(group, depth=0):
    """Capture registered writable RNA, nested collection rows and extra keys."""
    if depth > 8:
        raise ValueError('Author RNA collection nesting exceeds the receive contract')
    values = {}
    for prop in group.bl_rna.properties:
        key = prop.identifier
        if key == 'rna_type':
            continue
        if prop.type == 'COLLECTION':
            collection = getattr(group,key)
            if not hasattr(collection,'add') or not hasattr(collection,'clear'):
                raise ValueError('Author collection cannot be restored: '+key)
            values[key] = dict(kind='COLLECTION', rows=[_capture_rna(row,depth+1) for row in collection])
            continue
        if prop.is_readonly or group.is_property_readonly(key):
            continue
        if prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM','POINTER'}:
            raise ValueError('Unsupported writable author RNA: '+key)
        value = getattr(group,key)
        if getattr(prop,'is_array',False):
            value = tuple(value)
        elif prop.type == 'ENUM' and prop.is_enum_flag:
            value = frozenset(value)
        values[key] = dict(kind=prop.type, value=value, set=group.is_property_set(key))
    return dict(values=values, custom=_custom(group,values))


def _write_scalar(group,key,saved):
    if saved['set']:
        value = saved['value']
        setattr(group,key,set(value) if isinstance(value,frozenset) else value)
    else:
        group.property_unset(key)


def _restore_collection(group,key,saved):
    collection = getattr(group,key)
    collection.clear()
    for row in saved['rows']:
        _restore_rna(collection.add(),row)


def _restore_rna(group,state):
    values = state['values']
    # Dynamic group/parameter enums require their original Text and groups first.
    for key,saved in values.items():
        if saved['kind'] == 'POINTER':
            _write_scalar(group,key,saved)
    if 'groups' in values and values['groups']['kind'] == 'COLLECTION':
        _restore_collection(group,'groups',values['groups'])
    for key,saved in values.items():
        if saved['kind'] not in {'POINTER','COLLECTION'}:
            _write_scalar(group,key,saved)
    # style_mode can resynchronize element filters. Restore the complete original
    # elements and all remaining author tables after that callback has finished.
    for key,saved in values.items():
        if saved['kind'] == 'COLLECTION' and key != 'groups':
            _restore_collection(group,key,saved)
    _restore_custom(group,state['custom'],values)


def _caches(addon,chain,amount,dash):
    result = []
    for module,names in ((addon,('_style_cache','_chain_input_cache','_errors')),
                         (chain,('_BINDINGS','_SYNC','_SYNC_FAILURES')),
                         (amount,('_SYNC','_RUNTIME','_FAILED')),
                         (dash,('_SYNC','_RUNTIME','_FAILED'))):
        for name in names:
            cache = getattr(module,name)
            # Cached plans contain native references. Keep their actual objects;
            # setters replace/clear cache entries, not these saved plan objects.
            result.append((cache,cache.copy()))
    return result


def _restore_caches(saved):
    for cache,contents in saved:
        cache.clear()
        cache.update(contents)


def _curve_state(settings,curves):
    tree = settings.curve_tree
    if tree is None:
        return None
    if tree.library is not None or tree.get('toon_viewport_curve_role') != curves.ROLE:
        raise ValueError('Receive requires a local owned curve editor or no curve editor')
    if any(tree.nodes.get(key) is None or tree.nodes[key].bl_idname != 'ShaderNodeFloatCurve'
           for key in curves.KEYS):
        raise ValueError('Owned curve editor is incomplete')
    if curves.signature(tree,settings) != tree.get('last_signature'):
        raise ValueError('Synchronize pending author curves before receiving a candidate')
    codec = curves.codec()
    return dict(block=tree, custom=_custom(tree), fake=bool(tree.use_fake_user),
                mappings={key:codec._capture_curve(tree.nodes[key].mapping,
                    bool(getattr(settings,'curve_'+key+'_enabled'))) for key in curves.KEYS})


def _restore_curve(saved,curves):
    if saved is None:
        return
    tree = saved['block']
    codec = curves.codec()
    for key,value in saved['mappings'].items():
        node = tree.nodes.get(key)
        if node is None or node.bl_idname != 'ShaderNodeFloatCurve':
            raise RuntimeError('Original curve nodes were externally replaced during receive')
        actual = codec._capture_curve(node.mapping,value['enabled'])
        if actual != value:
            codec._restore_curve(node.mapping,value)
    _restore_custom(tree,saved['custom'])


def _guard(stack,scene,groups,chain,amount,dash):
    if groups.guarded(scene):
        raise ValueError('Finish the active author transaction before receiving')
    stack.enter_context(groups.guard(scene))
    for module in (chain,amount,dash):
        stack.enter_context(module._guard(scene))


def _release_pins(text_state,curve_state):
    for state in (text_state,curve_state):
        if state is not None:
            state['block'].use_fake_user = state['fake']


def _remove_owned_orphans(bpy,before,curves):
    removed = dict(texts=0,curves=0)
    for blocks,kind,owned in ((bpy.data.texts,'texts',lambda block:bool(block.get('toon_viewport_preview_owned'))),
            (bpy.data.node_groups,'curves',lambda block:block.get('toon_viewport_curve_role') == curves.ROLE)):
        for block in list(blocks):
            if (block.as_pointer() not in before[kind] and block.library is None
                    and block.users == 0 and owned(block)):
                blocks.remove(block)
                removed[kind] += 1
    return removed


def _reclaim_previous(bpy,text_state,curve_state,curves):
    """After commit only, retain anything with real users or a prior fake user."""
    warnings = []
    for state,blocks,owned in ((text_state,bpy.data.texts,
            lambda block:bool(block.get('toon_viewport_preview_reclaimable'))),
            (curve_state,bpy.data.node_groups,
             lambda block:block.get('toon_viewport_curve_role') == curves.ROLE)):
        if state is None or state['fake']:
            continue
        block = state['block']
        try:
            if block.library is None and block.users == 0 and owned(block):
                blocks.remove(block)
        except ReferenceError:
            pass
        except Exception as error:
            # Keeping an old owned orphan is safe; a published receipt must not
            # be turned into a false receive failure by optional reclamation.
            warnings.append('Retained previous author helper: '+str(error))
    return warnings


def apply(scene,path,expected_author_sha256,preserve_non_author,*,commit=None):
    """Receive one accepted style and return its verified author hash/summary.

    expected_author_sha256 is the candidate's target hash. preserve_non_author
    is the caller's current non_author_signature string. The caller must already
    have verified the accepted job and the waiting-period baseline. commit, when
    supplied, persists the caller's receipt and must not mutate Blender data.
    A commit exception restores the author just like an importer exception.
    """
    import bpy
    for value in (expected_author_sha256,preserve_non_author):
        if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:
            raise ValueError('Expected an explicit SHA-256 receive anchor')
    if commit is not None and not callable(commit):
        raise ValueError('Receive commit must be callable')
    if scene.library is not None or not scene.is_editable:
        raise ValueError('Receive requires a local editable Scene')
    if joint_workflow.non_author_signature(scene) != preserve_non_author:
        raise ValueError('Non-author state changed before receive')
    path = Path(path)
    document = joint_basic_style.read(path)
    basic = type(document) is dict and document.get('schema') == joint_basic_style.SCHEMA
    addon,files,curves,groups,chain,amount,dash = (pencil(name) for name in
        ('addon','file_authoring','curve_editor','group_ui','chain_ui','map_amount_ui','dash_ui'))
    settings = scene.toon_viewport_preview
    if not basic:
        animation = scene.animation_data
        if animation is not None and any(strip.type == 'META' for track in animation.nla_tracks for strip in track.strips):
            raise ValueError('Receive does not replace author data under META NLA')
        if any(path.startswith('toon_viewport_preview.') for path in groups._animation_paths(scene)):
            raise ValueError('Full-style receive has native author animation/driver references; edit those explicitly')
    text = settings.preset_text
    if text is not None and text.library is not None:
        raise ValueError('Receive requires a local author Text')
    before_hash = fingerprint(joint_workflow.capture_author(scene))
    state = _capture_rna(settings)
    text_state = None if text is None else dict(block=text,body=text.as_string(),
        custom=_custom(text),fake=bool(text.use_fake_user))
    curve_state = _curve_state(settings,curves)
    caches = _caches(addon,chain,amount,dash)
    before_ids = dict(texts={block.as_pointer() for block in bpy.data.texts},
                      curves={block.as_pointer() for block in bpy.data.node_groups})
    with ExitStack() as stack:
        _guard(stack,scene,groups,chain,amount,dash)
        try:
            for saved in (text_state,curve_state):
                if saved is not None:
                    saved['block'].use_fake_user = True
            if basic:
                joint_basic_style.apply(scene,document)
            else:
                files.import_style(scene,path,mode='REPLACE')
            observed = fingerprint(joint_workflow.capture_author(scene))
            if observed != expected_author_sha256:
                raise ValueError('Received author differs from the accepted candidate')
            if joint_workflow.non_author_signature(scene) != preserve_non_author:
                raise ValueError('Receive changed non-author data')
            # No orphan is deleted until commit returns. Releasing fake users
            # leaves those original IDs alive, so rollback still has its anchors.
            _release_pins(text_state,curve_state)
            if (fingerprint(joint_workflow.capture_author(scene)) != observed
                    or joint_workflow.non_author_signature(scene) != preserve_non_author):
                raise ValueError('Receive final readback changed Blender data')
            # Durable receipt is the final fallible step. It must only write
            # its own file, never Blender data. No rollback-triggering check
            # follows a successfully published receipt.
            if commit is not None:
                commit()
        except BaseException as error:
            try:
                if text_state is not None:
                    old = text_state['block']
                    if old.as_string() != text_state['body']:
                        old.from_string(text_state['body'])
                    _restore_custom(old,text_state['custom'])
                _restore_curve(curve_state,curves)
                _restore_rna(settings,state)
                _release_pins(text_state,curve_state)
                _remove_owned_orphans(bpy,before_ids,curves)
                if _capture_rna(settings) != state:
                    raise RuntimeError('Receive rollback native RNA readback differs')
                if fingerprint(joint_workflow.capture_author(scene)) != before_hash:
                    raise RuntimeError('Receive rollback author fingerprint differs')
                if joint_workflow.non_author_signature(scene) != preserve_non_author:
                    raise RuntimeError('Receive rollback detected a non-author mutation')
            except BaseException as rollback_error:
                raise RuntimeError('Author receive failed and rollback did not complete: '+str(rollback_error)) from error
            finally:
                _restore_caches(caches)
            raise
    warnings = _reclaim_previous(bpy,text_state,curve_state,curves)
    return dict(schema=SCHEMA,author_sha256=observed,style_schema=document.get('schema'),
                cleanup_warnings=warnings)
