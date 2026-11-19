# SPDX-License-Identifier: GPL-3.0-or-later
"""Static owned component evidence, separate from legacy finish passthrough.

Only GAME/MANGA and opaque EMISSION are admitted. Original slot ownership is
checked before freeze; evaluated IDs are read without editable-author checks.
No animation adapter, material replacement, or marker removal is performed.
"""
import math
import re
import uuid

from . import component_catalog as C, component_slots as S

SCHEMA = 'toon-component-freeze.static.v1'
STYLES = {'GAME', 'MANGA', 'EMISSION'}
_STATIC = 'no-assigned-actions-or-drivers.v1'


def _time(graph):
    if graph.mode != 'RENDER':
        raise ValueError('Components require the actual RENDER dependency graph')
    scene = graph.scene_eval
    return dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe)


def _static(block):
    """An empty AnimData container is harmless; assigned Actions are unsupported."""
    for value in (block, getattr(block, 'original', block)):
        data = getattr(value, 'animation_data', None)
        if data is None:
            continue
        if data.action is not None or len(data.drivers):
            raise ValueError('Static components do not admit Material/tree Actions or drivers')
        if any(getattr(strip, 'action', None) is not None
               for track in data.nla_tracks for strip in track.strips):
            raise ValueError('Static components do not admit NLA Actions')


def _tree_state(root, graph=None):
    """Reuse the component's bounded shader record, evaluating reached groups."""
    rows, identities, seen = [], [], set()
    def visit(reached, path):
        original = getattr(reached, 'original', reached)
        pointer = original.as_pointer()
        if pointer in seen:
            return
        seen.add(pointer)
        if len(seen) > 64:
            raise ValueError('Component group traversal exceeds its static profile')
        tree = reached.evaluated_get(graph) if graph is not None else reached
        if getattr(tree, 'original', tree) != original:
            raise ValueError('Evaluated component group original identity changed')
        _static(tree)
        ordered = sorted(tree.nodes, key=C.styles._node_key)
        records = [C.styles._node_record(node) for node in ordered]
        if len({row['id'] for row in records}) != len(records):
            raise ValueError('Duplicate static component node identity')
        for node, record in zip(ordered, records):
            if node.bl_idname == 'ShaderNodeTexCoord':
                obj = node.object
                record['properties']['object'] = None if obj is None else obj.original.name
        interface = []
        for item in tree.interface.items_tree:
            row = {'item_type': item.item_type}
            for name in ('identifier', 'name', 'in_out', 'socket_type', 'default_value'):
                if hasattr(item, name):
                    row[name] = C.styles._plain(getattr(item, name))
            interface.append(row)
        identities.append(dict(path=path, name=original.name))
        rows.append(dict(path=path, nodes=records, interface=interface, links=sorted([
            [C.styles._node_key(link.from_node), link.from_socket.identifier,
             C.styles._node_key(link.to_node), link.to_socket.identifier]
            for link in tree.links])))
        for node in ordered:
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None:
                visit(node.node_tree, path + '/' + C.styles._node_key(node))
    visit(root, 'material')
    return rows, identities


def _snapshot(material, style, graph=None):
    _static(material)
    if not material.use_nodes or material.node_tree is None:
        raise ValueError('Component native shader tree is missing')
    if style in ('GAME', 'MANGA'):
        owner = material.get(C.styles.OWNER_KEY)
        info = {'conflicts': []}
        reader = C.styles._inspect_game if style == 'GAME' else C.styles._inspect_manga
        actual = reader(material, info, owner)
        if style == 'GAME':
            from . import finish_nodes
            instance = C.styles._find_node(material.node_tree, 'game:finish', owner)
            reached = instance.node_tree
            if reached is None:
                raise ValueError('GAME finish instance has no reached Group')
            evaluated = reached.evaluated_get(graph) if graph is not None else reached
            if getattr(evaluated, 'original', evaluated) != getattr(reached, 'original', reached):
                raise ValueError('GAME evaluated finish Group identity changed')
            actual.update(finish_nodes.parameters(evaluated))
        errors = [r['message'] for r in info['conflicts'] if r['severity'] == 'error']
        if errors:
            raise ValueError('Component native read conflict: ' + '; '.join(errors))
    elif style == 'EMISSION':
        shader = C.media._nodes(material)['shader']
        if shader.bl_idname != 'ShaderNodeEmission':
            raise ValueError('Opaque emission native shader changed')
        actual = dict(color=list(shader.inputs['Color'].default_value),
                      strength=float(shader.inputs['Strength'].default_value))
    else:
        raise ValueError('Unsupported static component style')
    defaults = C.defaults(style)
    settings = C.normalize({key: defaults[key] if key in ('schema', 'style') else actual[key]
                            for key in defaults})
    trees, identities = _tree_state(material.node_tree, graph)
    options = {name: getattr(material, name) for name in (
        'use_nodes', 'surface_render_method', 'use_raytrace_refraction',
        'use_transparent_shadow', 'use_backface_culling', 'use_transparency_overlap')
        if hasattr(material, name)}
    return dict(settings=settings, shape_sha256=C.styles._digest(dict(trees=trees, material=options)),
                tree_ids=identities, animation=_STATIC)


def _evaluated(target, material, index, style, graph):
    _time(graph)
    obj = target.evaluated_get(graph)
    if obj.original != target or not 0 <= index < len(obj.material_slots):
        raise ValueError('Evaluated component target/slot identity changed')
    actual = obj.material_slots[index].material
    if actual is None or actual.original != material:
        raise ValueError('Evaluated component Material original identity changed')
    return _snapshot(actual, style, graph)


def capture(scene, objects, *, depsgraph):
    stamp = _time(depsgraph)
    if stamp != dict(scene=scene.name, frame=scene.frame_current, subframe=scene.frame_subframe):
        raise ValueError('Component source and RENDER graph times disagree')
    rows = []
    for target in sorted(objects, key=lambda obj: obj.name):
        for index in sorted({p.material_index for p in target.data.polygons}):
            material = target.material_slots[index].material
            if material is None or S.KEY not in material:
                continue
            slot, material, state = S._owned(scene, target, index)
            checked = C.inspect(material)
            if checked['status'] != 'valid' or checked['settings'] != state['settings']:
                raise ValueError('Original component catalog inspection conflicts')
            style = checked['settings']['style']
            if style not in STYLES:
                raise ValueError('Joint static components only admit GAME/MANGA/opaque EMISSION')
            before = _snapshot(material, style)
            row = dict(target=target.name, material=material.name, slot=index, style=style,
                source_mesh=target.data.name, slot_owner=material[S.OWNER], link=slot.link,
                before=before, render_before=_evaluated(target, material, index, style, depsgraph))
            if before != row['render_before']:
                raise ValueError('Component Main and actual RENDER parameters/structure differ')
            rows.append(row)
    return dict(schema=SCHEMA, render_time=stamp, targets=rows)


def owns_game_finish(material, receipt, *, finish_key, mark_key, profile_key):
    """Authorize exactly one already-observed component GAME finish instance."""
    if finish_key in material or not any(
        row['material'] == material.name and row['style'] == 'GAME'
        and row['slot_owner'] == material.get(S.OWNER) and row['before'] == row['render_before']
        for row in receipt['targets']):
        return False
    reached, pending, seen = [], [material.node_tree], set()
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.get(mark_key) is not None:
                return False
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None:
                if node.node_tree.get(profile_key) is not None:
                    reached.append(node)
                pending.append(node.node_tree)
    return (len(reached) == 1
        and reached[0].get(C.styles.NODE_KEY) == 'game:finish'
        and reached[0].get(C.styles.NODE_OWNER_KEY) == material.get(C.styles.OWNER_KEY))


def _bound(row, scene_name):
    """Preserve original ownership but do not compare frozen Mesh to source Mesh."""
    import bpy
    target = bpy.data.objects.get(row['target'])
    material = bpy.data.materials.get(row['material'])
    scene = bpy.data.scenes.get(scene_name)
    if (target is None or material is None or scene is None or scene.objects.get(target.name) != target
            or not 0 <= row['slot'] < len(target.material_slots)):
        raise ValueError('Frozen component target/material/slot is missing')
    slot = target.material_slots[row['slot']]
    if (slot.material != material or slot.link != row['link']
            or material.get(S.KEY) != S.PROFILE or material.get(S.TARGET) != target
            or material.get(S.SLOT) != row['slot'] or material.get(S.LINK) != row['link']
            or material.get(S.MESH) != row['source_mesh'] or material.get(S.OWNER) != row['slot_owner']
            or S._signature(material) != material.get(S.SIGNATURE)):
        raise ValueError('Frozen component binding/ownership changed')
    state = C.inspect(material)
    if state['status'] != 'valid' or state['settings']['style'] != row['style']:
        raise ValueError('Frozen original component shader conflicts')
    return target, material


def verify_frozen(receipt):
    for row in receipt['targets']:
        target, material = _bound(row, receipt['render_time']['scene'])
        row['frozen'] = _snapshot(material, row['style'])
        row['data_exact'] = row['before'] == row['frozen']
    validate_receipt(receipt, require_render=False)


def verify_render(receipt, depsgraph):
    if _time(depsgraph) != receipt['render_time']:
        raise ValueError('Component actual RENDER time changed')
    for row in receipt['targets']:
        target, material = _bound(row, receipt['render_time']['scene'])
        row['render_frozen'] = _evaluated(target, material, row['slot'], row['style'], depsgraph)
        row['render_exact'] = row['render_before'] == row['render_frozen']
    validate_receipt(receipt)


def _validate_snapshot(value, style):
    if type(value) is not dict or set(value) != {'settings', 'shape_sha256', 'tree_ids', 'animation'}:
        raise ValueError('Invalid native component snapshot')
    if (value['animation'] != _STATIC or type(value['settings']) is not dict
            or value['settings'].get('style') != style
            or C.normalize(value['settings']) != value['settings']
            or re.fullmatch('[0-9a-f]{64}', str(value['shape_sha256'])) is None):
        raise ValueError('Invalid component native parameters/shape/static witness')
    ids = value['tree_ids']
    if (type(ids) is not list or not ids or len(ids) > 64
            or any(type(item) is not dict or set(item) != {'path', 'name'}
                or any(type(item[key]) is not str or not item[key] for key in item) for item in ids)
            or ids[0]['path'] != 'material' or len({item['path'] for item in ids}) != len(ids)):
        raise ValueError('Invalid component reached-tree identities')


def validate_receipt(receipt, *, require_render=True):
    if (type(receipt) is not dict or set(receipt) != {'schema', 'render_time', 'targets'}
            or receipt['schema'] != SCHEMA or type(receipt['targets']) is not list):
        raise ValueError('Invalid static component freeze receipt')
    stamp = receipt['render_time']
    if (type(stamp) is not dict or set(stamp) != {'scene', 'frame', 'subframe'}
            or type(stamp['scene']) is not str or not stamp['scene'] or type(stamp['frame']) is not int
            or type(stamp['subframe']) not in (int, float) or not math.isfinite(stamp['subframe'])):
        raise ValueError('Invalid component RENDER stamp')
    seen = set()
    required = {'target', 'material', 'slot', 'style', 'source_mesh', 'slot_owner', 'link',
                'before', 'render_before', 'frozen', 'data_exact'}
    for row in receipt['targets']:
        if (type(row) is not dict or not required <= set(row)
                or set(row) - required - {'render_frozen', 'render_exact'}
                or type(row['style']) is not str or row['style'] not in STYLES
                or type(row['slot']) is not int or row['slot'] < 0
                or row['link'] not in ('DATA', 'OBJECT')
                or any(type(row[key]) is not str or not row[key]
                       for key in ('target', 'material', 'source_mesh', 'slot_owner'))):
            raise ValueError('Invalid component target/slot/style witness')
        try:
            if str(uuid.UUID(row['slot_owner'])) != row['slot_owner']:
                raise ValueError('Noncanonical component slot owner')
        except (ValueError, AttributeError) as error:
            raise ValueError('Invalid component slot owner') from error
        key = (row['target'], row['slot'])
        if key in seen:
            raise ValueError('Duplicate component target/slot witness')
        seen.add(key)
        for field in ('before', 'render_before', 'frozen'):
            _validate_snapshot(row[field], row['style'])
        if (row['data_exact'] is not True or row['before'] != row['frozen']
                or row['before'] != row['render_before']):
            raise ValueError('Component native parameters/structure did not freeze exactly')
        if require_render or 'render_frozen' in row or 'render_exact' in row:
            if row.get('render_exact') is not True or row.get('render_frozen') != row['render_before']:
                raise ValueError('Component actual RENDER parameters/structure changed')
    return len(seen)
