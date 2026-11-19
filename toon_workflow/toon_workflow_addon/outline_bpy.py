"""Blender adapter for explicit-ID evaluated meshes and owned native carriers.

SPDX-License-Identifier: GPL-3.0-or-later
No handlers, automatic Text execution, file saves, render calls or global reset.
"""
import math
import time
import uuid

import bpy
from mathutils import Vector, Matrix

from . import outline_core as mathcore
from .contract import WorkflowError

OBJECT_ID = "_toon_object_id"
MESH_ID = "_toon_mesh_id"
TOPOLOGY = "_toon_topology_sha256"
POINT_ID = "_toon_point_id"
EDGE_ID = "_toon_edge_id"
EDGE_MARK = "_toon_edge_mark"
SCOPE = "_toon_scope_id"
GENERATED = "_toon_generated_outline"


def _fail(code, message, path="$.targets"):
    raise WorkflowError(code, message, path)


def _uuid(value, label):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        _fail("INVALID_SOURCE_UUID", "Expected an explicit UUID", label)
    if str(parsed) != value:
        _fail("INVALID_SOURCE_UUID", "UUID must use canonical lowercase spelling", label)


def _mesh_object(obj):
    if obj is None or obj.type != "MESH" or obj.mode != "OBJECT":
        _fail("INVALID_OUTLINE_TARGET", "An explicit Mesh object in Object Mode is required")
    return obj.data


def _attribute_ids(mesh, name, domain):
    attr = mesh.attributes.get(name)
    size = len(mesh.vertices) if domain == "POINT" else len(mesh.edges)
    if attr is None or attr.domain != domain or attr.data_type != "INT" or len(attr.data) != size:
        _fail("SOURCE_ID_LAYER_MISSING", "Expected complete %s/INT layer %s" % (domain, name))
    values = [int(x.value) for x in attr.data]
    mathcore.validate_ids(values, "$.targets." + name)
    return values


def _topology(mesh, pids, eids):
    edges = [[eid, [pids[i] for i in edge.vertices]] for edge, eid in zip(mesh.edges, eids)]
    faces = [[pids[i] for i in poly.vertices] for poly in mesh.polygons]
    if any(len(set(pair)) != 2 for _, pair in edges):
        _fail("DEGENERATE_SOURCE_EDGE", "A source edge has coincident point identities")
    if len({tuple(sorted(pair)) for _, pair in edges}) != len(edges):
        _fail("DUPLICATE_SOURCE_EDGE", "Distinct edge IDs cannot map to the same source point pair")
    return mathcore.topology_digest(pids, edges, faces)


def initialize_source(obj, object_id, mesh_id, initialize):
    """Initialize only in the caller's isolated candidate Mesh; never save a file."""
    started = time.perf_counter()
    mesh = _mesh_object(obj)
    _uuid(object_id, "$.targets.object_id"); _uuid(mesh_id, "$.targets.mesh_id")
    for owner, name, expected in ((obj, OBJECT_ID, object_id), (mesh, MESH_ID, mesh_id)):
        if name in owner and owner[name] != expected:
            _fail("SOURCE_ID_CONFLICT", "Existing source UUID cannot be reassigned")
    pa, ea = mesh.attributes.get(POINT_ID), mesh.attributes.get(EDGE_ID)
    if bool(pa) != bool(ea):
        _fail("PARTIAL_SOURCE_IDENTITY", "POINT and EDGE identity must be initialized together")
    missing = pa is None
    if missing and not initialize:
        _fail("SOURCE_IDENTITY_REQUIRED", "Source has no explicit point/edge identity")
    if not initialize and (obj.get(OBJECT_ID) != object_id or mesh.get(MESH_ID) != mesh_id or TOPOLOGY not in mesh):
        _fail("SOURCE_IDENTITY_REQUIRED", "Read-only identity validation cannot initialize missing metadata")
    if missing:
        pids, eids = list(range(1, len(mesh.vertices) + 1)), list(range(1, len(mesh.edges) + 1))
        mathcore.validate_ids(pids, POINT_ID); mathcore.validate_ids(eids, EDGE_ID)
    else:
        pids, eids = _attribute_ids(mesh, POINT_ID, "POINT"), _attribute_ids(mesh, EDGE_ID, "EDGE")
    topology = _topology(mesh, pids, eids)
    if TOPOLOGY in mesh and mesh[TOPOLOGY] != topology:
        _fail("SOURCE_TOPOLOGY_CHANGED", "Existing fixed-topology identity no longer matches the original Mesh")
    will_write = missing or obj.get(OBJECT_ID) != object_id or mesh.get(MESH_ID) != mesh_id or TOPOLOGY not in mesh
    if will_write:
        if not initialize:
            _fail("SOURCE_IDENTITY_REQUIRED", "Initialization was not requested")
        consumers = [other for other in bpy.data.objects if other.type == "MESH" and other.data == mesh]
        if obj.library or mesh.library or len(consumers) != 1 or consumers[0] != obj:
            _fail("SOURCE_NOT_ISOLATED", "Identity initialization requires a local, single-consumer working Mesh")
        # Preflight completes before the first mutation.
        if missing:
            pa = mesh.attributes.new(POINT_ID, "INT", "POINT")
            ea = mesh.attributes.new(EDGE_ID, "INT", "EDGE")
            for item, value in zip(pa.data, pids): item.value = value
            for item, value in zip(ea.data, eids): item.value = value
        obj[OBJECT_ID], mesh[MESH_ID], mesh[TOPOLOGY] = object_id, mesh_id, topology
    return {"object_name": obj.name, "object_id": object_id, "mesh_id": mesh_id,
            "topology_sha256": topology, "initialized": bool(will_write),
            "vertex_count": len(pids), "edge_count": len(eids), "face_count": len(mesh.polygons),
            "seconds": time.perf_counter() - started}


def validate_edge_overrides(targets, settings):
    """Check explicit canonical selectors before any identity/cache mutation.

    Missing IDs have the same declared initialization sequence as
    initialize_source. This predicts the sequence without writing attributes;
    the writer and evaluated capture still perform their full identity checks.
    """
    overrides = settings.get('edge_overrides', [])
    if not overrides:
        return
    for obj, target in targets:
        selected = [e for e in overrides if e['object_id'] == target['object_id']]
        if not selected:
            continue
        mesh = _mesh_object(obj)
        pa, ea = mesh.attributes.get(POINT_ID), mesh.attributes.get(EDGE_ID)
        if (pa is None) != (ea is None):
            _fail('CONFLICT', 'Local stroke selector requires complete source identity')
        if pa is None:
            if not target['initialize_identity']:
                _fail('CONFLICT', 'Local stroke selector cannot initialize undeclared source identity')
            pids = list(range(1, len(mesh.vertices) + 1))
            eids = list(range(1, len(mesh.edges) + 1))
        else:
            pids, eids = _attribute_ids(mesh, POINT_ID, 'POINT'), _attribute_ids(mesh, EDGE_ID, 'EDGE')
        topology = _topology(mesh, pids, eids)
        if TOPOLOGY in mesh and mesh[TOPOLOGY] != topology:
            _fail('CONFLICT', 'Local stroke selector source topology changed')
        keys = {(eid, *sorted(pids[i] for i in edge.vertices)) for edge, eid in zip(mesh.edges, eids)}
        for item in selected:
            if item['mesh_id'] != target['mesh_id'] or (item['edge_id'], *item['point_ids']) not in keys:
                _fail('CONFLICT', 'Local stroke selector does not match an actual canonical source edge', '$.manifest.lines.edge_overrides')


def _source_identity(obj):
    mesh = _mesh_object(obj)
    for value, label in ((obj.get(OBJECT_ID), OBJECT_ID), (mesh.get(MESH_ID), MESH_ID)):
        _uuid(value, "$.targets." + label)
    pids, eids = _attribute_ids(mesh, POINT_ID, "POINT"), _attribute_ids(mesh, EDGE_ID, "EDGE")
    topology = _topology(mesh, pids, eids)
    if mesh.get(TOPOLOGY) != topology:
        _fail("SOURCE_TOPOLOGY_CHANGED", "Original Mesh identity/connectivity changed")
    return mesh, pids, eids, topology


def capture_source(obj, depsgraph):
    started = time.perf_counter()
    source_mesh, original_pids, original_eids, topology = _source_identity(obj)
    mark_layer = source_mesh.attributes.get(EDGE_MARK)
    if mark_layer and (mark_layer.domain != "EDGE" or mark_layer.data_type != "BOOLEAN" or len(mark_layer.data) != len(original_eids)):
        _fail("INVALID_MARK_ATTRIBUTE", "Optional edge-mark layer must be complete EDGE/BOOLEAN")
    marks = {eid: bool(mark_layer.data[i].value if mark_layer else getattr(edge, "use_freestyle_mark", False))
             for i, (eid, edge) in enumerate(zip(original_eids, source_mesh.edges))}
    evaluated = obj.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world.copy()
    determinant = matrix.to_3x3().determinant()
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        _fail("SINGULAR_SOURCE_TRANSFORM", "Evaluated object transform is singular")
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    if mesh is None:
        _fail("EVALUATED_MESH_UNAVAILABLE", "Evaluated target did not produce a Mesh")
    try:
        pids, eids = _attribute_ids(mesh, POINT_ID, "POINT"), _attribute_ids(mesh, EDGE_ID, "EDGE")
        if sorted(pids) != sorted(original_pids) or sorted(eids) != sorted(original_eids) or _topology(mesh, pids, eids) != topology:
            _fail("EVALUATED_TOPOLOGY_CHANGED", "Evaluated ID sets or directed connectivity differ from the fixed source")
        positions = {str(pid): list(matrix @ vertex.co) for pid, vertex in zip(pids, mesh.vertices)}
        normal_matrix = matrix.to_3x3().inverted().transposed()
        adjacency = {tuple(sorted(pids[j] for j in edge.vertices)): [] for edge in mesh.edges}
        faces = []
        for index, polygon in enumerate(mesh.polygons):
            ids = [pids[j] for j in polygon.vertices]
            corners = [positions[str(pid)] for pid in ids]
            center = [sum(p[k] for p in corners) / len(corners) for k in range(3)]
            normal = mathcore.unit(tuple(normal_matrix @ polygon.normal))
            faces.append({"vertex_ids": ids, "normal": list(normal), "center": center,
                          "material": int(polygon.material_index)})
            for a, b in zip(ids, ids[1:] + ids[:1]):
                pair = tuple(sorted((a, b)))
                if pair not in adjacency:
                    _fail("EVALUATED_TOPOLOGY_CHANGED", "Face boundary has no corresponding evaluated edge")
                adjacency[pair].append(index)
        edges = []
        for eid, edge in zip(eids, mesh.edges):
            pair = sorted(pids[i] for i in edge.vertices)
            adjacent = adjacency[tuple(pair)]
            if len(adjacent) > 2:
                _fail("NONMANIFOLD_SOURCE_EDGE", "More than two adjacent faces are not qualified")
            edges.append({"edge_id": eid, "vertices": pair, "faces": adjacent, "marked": marks[eid]})
        if not all(math.isfinite(v) for point in positions.values() for v in point):
            _fail("NONFINITE_SOURCE_GEOMETRY", "Evaluated world positions must be finite")
        capture = {"object_name": obj.name, "object_id": obj[OBJECT_ID], "mesh_id": source_mesh[MESH_ID],
                   "topology_sha256": topology, "positions": positions, "faces": faces,
                   "edges": sorted(edges, key=lambda e: e["edge_id"]),
                   "matrix_world": [list(row) for row in matrix], "determinant": determinant,
                   "counts": {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons),
                              "boundary_edges": sum(len(e["faces"]) == 1 for e in edges),
                              "loose_edges": sum(not e["faces"] for e in edges)},
                   "mark_source": "original explicit EDGE/BOOLEAN attribute if present, otherwise original Freestyle marks mapped by stable EdgeID",
                   "normal_policy": "evaluated polygon normal transformed by inverse transpose; no extra negative-determinant flip"}
        capture["evaluated_geometry_sha256"] = mathcore.digest(capture)
        capture["seconds"] = time.perf_counter() - started
        return capture
    finally:
        evaluated.to_mesh_clear()


def _camera(scene):
    camera = scene.camera
    if camera is None or camera.type != "CAMERA" or camera.data.type not in {"PERSP", "ORTHO"}:
        _fail("UNSUPPORTED_OUTLINE_CAMERA", "Explicit PERSP or ORTHO camera is required", "$.render.camera")
    matrix = camera.matrix_world
    axes = [tuple(matrix.col[i][:3]) for i in range(3)]
    if any(abs(mathcore.length(axis) - 1) > 1e-5 for axis in axes) or any(abs(mathcore.dot(axes[i], axes[j])) > 1e-5 for i in range(3) for j in range(i)) or abs(matrix.to_3x3().determinant() - 1) > 1e-5:
        _fail("NONRIGID_OUTLINE_CAMERA", "Camera scale/shear/parity is not qualified", "$.render.camera")
    frame = camera.data.view_frame(scene=scene)
    x0, x1 = min(p.x for p in frame), max(p.x for p in frame)
    y0, y1 = min(p.y for p in frame), max(p.y for p in frame)
    resolution = [int(scene.render.resolution_x * scene.render.resolution_percentage / 100),
                  int(scene.render.resolution_y * scene.render.resolution_percentage / 100)]
    ratio = scene.render.pixel_aspect_x / scene.render.pixel_aspect_y
    if min(resolution) <= 0 or min(x1 - x0, y1 - y0, ratio) <= 0:
        _fail("INVALID_OUTLINE_CAMERA", "Camera frame, resolution and pixel aspect must be positive")
    return {"type": camera.data.type, "origin": list(matrix.translation), "axes": [list(x) for x in axes],
            "toward_camera": list(axes[2]), "frame": [x0, x1, y0, y1], "frame_depth": abs(frame[0].z),
            "resolution": resolution, "pixel_ratio": ratio, "near": camera.data.clip_start,
            "far": camera.data.clip_end, "matrix_world": [list(row) for row in matrix],
            "width_metric": "vertical raster pixels; horizontal coordinates scaled by pixel_aspect_x/pixel_aspect_y"}


def _tag(item, scope_id, frame_tag):
    item[SCOPE] = scope_id
    item[GENERATED] = True
    item["_toon_frame_tag"] = str(frame_tag)


def _ink(settings, scope_id, frame_tag):
    material = bpy.data.materials.new("ToonInk_" + str(frame_tag))
    _tag(material, scope_id, frame_tag)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    emit = nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = settings["color"]
    emit.inputs["Strength"].default_value = 1
    alpha = settings["opacity"] * settings["color"][3]
    output = nodes.new("ShaderNodeOutputMaterial")
    if alpha < 1:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        mix = nodes.new("ShaderNodeMixShader")
        mix.inputs[0].default_value = alpha
        links.new(transparent.outputs[0], mix.inputs[1]); links.new(emit.outputs[0], mix.inputs[2])
        links.new(mix.outputs[0], output.inputs["Surface"])
        material.surface_render_method = "DITHERED"
    else:
        links.new(emit.outputs[0], output.inputs["Surface"])
    return material


def _group_ink(settings, scope_id, frame_tag, group_id, ink_pool=None):
    """Reuse only a 0.3 Ink reference witnessed before owned carriers cleared."""
    name = 'ToonInk_' + str(frame_tag) + '_' + group_id
    material = bpy.data.materials.get(name)
    previous = ink_pool.get((str(frame_tag), group_id)) if ink_pool is not None else None
    if material is None and previous is None:
        material = bpy.data.materials.new(name)
        try:
            _tag(material, scope_id, frame_tag)
            material['_toon_group_id'] = group_id
            material.use_nodes = True
            nodes, links = material.node_tree.nodes, material.node_tree.links
            nodes.clear()
            emit = nodes.new('ShaderNodeEmission')
            emit.inputs['Color'].default_value = settings['color']
            emit.inputs['Strength'].default_value = 1
            output = nodes.new('ShaderNodeOutputMaterial')
            links.new(emit.outputs[0], output.inputs['Surface'])
        except Exception:
            if material.users == 0:
                bpy.data.materials.remove(material)
            raise
        return material, True, None
    if (material is None or material is not previous or material.name != name
            or material.get(SCOPE) != scope_id or material.get(GENERATED) is not True
            or material.get('_toon_frame_tag') != str(frame_tag)
            or material.get('_toon_group_id') != group_id
            or material.library or material.override_library or material.users != 0):
        _fail('CONFLICT', 'Exact grouped Ink name is not an isolated witnessed baseline material')
    tree = material.node_tree
    emit = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeEmission'] if tree else []
    outputs = [n for n in tree.nodes if n.bl_idname == 'ShaderNodeOutputMaterial'] if tree else []
    if (not material.use_nodes or tree is None or len(tree.nodes) != 2
            or len(emit) != 1 or len(outputs) != 1 or len(tree.links) != 1
            or tree.links[0].from_node != emit[0] or tree.links[0].to_node != outputs[0]
            or tree.links[0].from_socket != emit[0].outputs[0]
            or tree.links[0].to_socket != outputs[0].inputs['Surface']
            or emit[0].inputs['Strength'].default_value != 1):
        _fail('CONFLICT', 'Witnessed grouped Ink no longer has the opaque node structure')
    previous_color = tuple(emit[0].inputs['Color'].default_value)
    emit[0].inputs['Color'].default_value = settings['color']
    return material, False, previous_color


def _geometry_capture(obj):
    if obj.type == "MESH":
        return {"type": "MESH", "vertices": [list(v.co) for v in obj.data.vertices],
                "faces": [list(p.vertices) for p in obj.data.polygons],
                "matrix_world": [list(row) for row in obj.matrix_world]}
    return {"type": "CURVE", "bevel_depth": obj.data.bevel_depth,
            "bevel_resolution": obj.data.bevel_resolution, "fill_caps": obj.data.use_fill_caps,
            "splines": [{"cyclic": s.use_cyclic_u, "points": [list(p.co) for p in s.points],
                         "radii": [p.radius for p in s.points]} for s in obj.data.splines],
            "matrix_world": [list(row) for row in obj.matrix_world]}


def build_frame(scene, objects, collection, settings, time_value, frame_tag, ink_pool=None, *, allow_styled_jitter=False):
    """Create this frame's new owned native carrier; parent controls old-frame caches."""
    started = time.perf_counter()
    scope_id = collection.get(SCOPE) if collection else None
    if not scope_id:
        _fail("OWNED_COLLECTION_REQUIRED", "Caller must supply an explicitly scoped collection", "$.scope_id")
    empty = {"objects": [], "object_names": [], "sources": [], "fragments": [], "geometry_sha256": mathcore.digest([]),
             "counts": {"objects": 0, "vertices": 0, "faces": 0, "samples": 0}, "visibility": "native_depth"}
    if 'placement' in settings:
        empty['visibility'] = 'grouped_native_placement'
    if not settings["enabled"]:
        return {**empty, "enabled": False, "seconds": time.perf_counter() - started, "limits": [],
                **({"groups": []} if "groups" in settings else {})}
    if settings["backend"] not in {"screen_ribbon", "tube_world"}:
        _fail("UNSUPPORTED_OUTLINE_BACKEND", "Unknown native carrier backend")
    if ((settings["backend"] == "screen_ribbon" and settings["width_mode"] not in {"pixel", "frame_fraction"}) or
            (settings["backend"] == "tube_world" and settings["width_mode"] != "world")):
        _fail("OUTLINE_WIDTH_DOMAIN", "Native carrier and width domain are incompatible")
    if not math.isfinite(time_value) or abs(time_value - round(time_value)) > 1e-6:
        _fail("OUTLINE_SUBFRAME_NOT_QUALIFIED", "P3 delivery creates integer-frame geometry caches only", "$.time")
    if abs(scene.frame_current + scene.frame_subframe - time_value) > 1e-5:
        _fail("OUTLINE_TIME_MISMATCH", "Scene must already be evaluated at the explicit frame time", "$.time")
    if scene.render.use_motion_blur or (scene.camera and scene.camera.data.dof.use_dof):
        _fail("OUTLINE_IMAGING_NOT_QUALIFIED", "P3 frame caches do not qualify native MB/DOF", "$.render")
    camera = _camera(scene)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    if depsgraph.scene_eval.name != scene.name:
        _fail("OUTLINE_SCENE_CONTEXT", "Evaluated dependency graph belongs to a different scene")
    captures = [capture_source(obj, depsgraph) for obj in objects]
    if len({c["object_id"] for c in captures}) != len(captures):
        _fail("DUPLICATE_OBJECT_ID", "Distinct target objects must have distinct explicit identities")
    if "groups" in settings:
        if 'stroke' in settings:
            return _build_grouped_frame(captures, camera, collection, settings, time_value, frame_tag, started, ink_pool=ink_pool, allow_styled_jitter=allow_styled_jitter)
        return _build_grouped_frame(captures, camera, collection, settings, time_value, frame_tag, started)
    plans = [mathcore.build_plan(c, camera, settings, time_value) for c in captures]
    fragments = [f for p in plans for f in p["fragments"]]
    chains = [c for p in plans for c in p["chains"]]
    vertices, faces, joins = [], [], []
    if settings["backend"] == "screen_ribbon":
        for chain in chains:
            samples = chain["samples"]
            if len(samples) < (3 if chain["closed"] else 2):
                continue
            spec = mathcore.ribbon_mesh([s["screen"] for s in samples], [s["depth"] for s in samples],
                                        plans[0]["width"], chain["closed"])
            base = len(vertices)
            vertices.extend(mathcore.unproject(*v, camera) for v in spec["vertices"])
            faces.extend(tuple(base + i for i in f) for f in spec["triangles"])
            joins.extend(spec["joins"])
            if len(vertices) > 2000000:
                _fail("OUTLINE_OUTPUT_LIMIT", "Frame exceeds the explicit two-million generated vertex bound")
    created = []
    data = material = None
    try:
        if chains and (faces or settings["backend"] == "tube_world"):
            material = _ink(settings, scope_id, frame_tag)
            if settings["backend"] == "screen_ribbon":
                data = bpy.data.meshes.new("ToonRibbon_" + str(frame_tag))
                _tag(data, scope_id, frame_tag)
                data.from_pydata(vertices, [], faces); data.update()
            else:
                data = bpy.data.curves.new("ToonTube_" + str(frame_tag), "CURVE")
                _tag(data, scope_id, frame_tag)
                data.dimensions = "3D"; data.resolution_u = 1
                data.bevel_depth = settings["width"] / 2
                data.bevel_resolution = 3; data.use_fill_caps = True
                for chain in chains:
                    if len(chain["samples"]) < 2:
                        continue
                    spline = data.splines.new("POLY"); spline.points.add(len(chain["samples"]) - 1)
                    spline.use_cyclic_u = chain["closed"]
                    for point, sample in zip(spline.points, chain["samples"]):
                        point.co = (*sample["world"], 1); point.radius = 1
            data.materials.append(material)
            obj = bpy.data.objects.new("ToonOutline_" + str(frame_tag), data)
            _tag(obj, scope_id, frame_tag)
            created.append(obj); collection.objects.link(obj)
        actual = [_geometry_capture(obj) for obj in created]
        geometry_sha = mathcore.digest(actual)
        for obj in created:
            obj["_toon_geometry_sha256"] = mathcore.digest(_geometry_capture(obj))
    except Exception:
        for obj in created:
            if obj.get(SCOPE) == scope_id: bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0 and data.get(SCOPE) == scope_id:
            (bpy.data.meshes if isinstance(data, bpy.types.Mesh) else bpy.data.curves).remove(data)
        if material is not None and material.users == 0 and material.get(SCOPE) == scope_id:
            bpy.data.materials.remove(material)
        raise
    return {"enabled": True, "objects": [obj.name for obj in created], "object_names": [obj.name for obj in created],
            "geometry_sha256": geometry_sha, "objects_geometry": actual, "camera": camera,
            "sources": captures, "fragments": fragments,
            "classified_edges": [e for p in plans for e in p["classified_edges"]],
            "discarded": [e for p in plans for e in p["discarded"]],
            "chains": [{"sources": c["sources"], "closed": c["closed"], "samples": len(c["samples"])} for c in chains],
            "joins": joins, "visibility": "native_depth", "cpu_visibility": "not_called",
            "depth_bias": settings["depth_bias"], "time_value": time_value,
            "counts": {"objects": len(created), "vertices": sum(len(g.get("vertices", [])) for g in actual),
                       "faces": sum(len(g.get("faces", [])) for g in actual),
                       "curve_points": sum(len(s["points"]) for g in actual for s in g.get("splines", [])),
                       "splines": sum(len(g.get("splines", [])) for g in actual),
                       "source_vertices": sum(c["counts"]["vertices"] for c in captures),
                       "source_edges": sum(c["counts"]["edges"] for c in captures),
                       "samples": sum(len(c["samples"]) for c in chains), "fragments": len(fragments), "chains": len(chains)},
            "seconds": time.perf_counter() - started,
            "limits": ["Fixed-topology polyhedral edges; smooth apparent contours, topology generation and nonmanifold adjacency>2 are not qualified.",
                       "Native depth retains hidden source geometry; no CPU visibility deletion. Screen ribbon uses guarded camera clipping; world tube keeps full offscreen source intervals.",
                       "Ribbon segment interiors overlap; translucent opacity can compound. Acute unions are not everywhere constant-width.",
                       "Tube width is full world-space diameter; native bevel corners do not promise constant pixel width.",
                       "Camera-depth bias is an explicit artistic offset, not a general optical/contact guarantee.",
                       "Integer-frame geometry only; no handler, Text execution, MB/DOF or subframe shutter acceptance."]}


def _build_grouped_frame(captures, camera, collection, settings, time_value, frame_tag, started, ink_pool=None, *, allow_styled_jitter=False):
    """One capture/classification per source, then one carrier per style bucket."""
    scope_id = collection[SCOPE]
    grouped = [mathcore.build_grouped_plan(c, camera, settings, time_value, allow_styled_jitter=allow_styled_jitter) for c in captures]
    created, resources, groups, all_chains, plans, joins = [], [], [], [], [], []
    borrowed = []
    total_vertices = 0
    try:
        for index, bucket in enumerate(grouped[0]['groups']):
            group_id, effective = bucket['group_id'], bucket['settings']
            group_plans = [p['groups'][index]['plan'] for p in grouped]
            plans.extend(group_plans)
            chains = [c for p in group_plans for c in p['chains']]
            all_chains.extend(chains)
            width = effective['width'] * (camera['resolution'][1] if effective['width_mode'] == 'frame_fraction' else 1)
            if 'placement' in settings:
                from . import placement_core
                placement_core.validate_plane_depth(effective['placement'], camera)
            vertices, faces = [], []
            for chain in chains:
                samples = chain['samples']
                if len(samples) < (3 if chain['closed'] else 2): continue
                if 'stroke_style' in chain:
                    from . import stroke_core
                    reference = (stroke_core.fragment_reference_frame(chain, camera)
                                 if allow_styled_jitter and effective['jitter_px'] > 0 else None)
                    spec = stroke_core.ribbon_mesh(samples, **chain['stroke_style'],
                        **({'reference_direction': reference['direction'], 'reference_normal': reference['normal']}
                           if reference is not None else {}))
                else:
                    spec = mathcore.ribbon_mesh([s['screen'] for s in samples], [s['depth'] for s in samples], width, chain['closed'])
                if 'placement' in settings:
                    spec = placement_core.place_spec(spec, effective['placement'], camera)
                base = len(vertices)
                vertices.extend(mathcore.unproject(*v, camera) for v in spec['vertices'])
                faces.extend(tuple(base + i for i in f) for f in spec['triangles'])
                joins.extend(spec['joins'])
                if total_vertices + len(vertices) > 2000000:
                    _fail('OUTLINE_OUTPUT_LIMIT', 'All groups exceed the two-million generated vertex bound')
            names = []
            if faces:
                suffix = str(frame_tag) + '_' + group_id
                if 'stroke' in settings:
                    material, is_new, previous_color = _group_ink(effective, scope_id, frame_tag, group_id, ink_pool)
                    if is_new:
                        resources.append(material)
                    else:
                        borrowed.append((material, previous_color))
                else:
                    material = _ink(effective, scope_id, suffix); resources.append(material)
                data = bpy.data.meshes.new('ToonRibbon_' + suffix); resources.append(data)
                if 'stroke' in settings:
                    _tag(data, scope_id, frame_tag); data['_toon_group_id'] = group_id
                obj = bpy.data.objects.new('ToonOutline_' + suffix, data)
                if 'stroke' in settings:
                    _tag(obj, scope_id, frame_tag); obj['_toon_group_id'] = group_id
                created.append(obj); collection.objects.link(obj)
                for item in (material, data, obj):
                    _tag(item, scope_id, frame_tag)
                    item['_toon_group_id'] = group_id
                if allow_styled_jitter and effective['jitter_px'] > 0:
                    # Keep near-camera depth in local float32 coordinates.
                    # Translation is sealed by the complete native cache record.
                    origin = camera['origin']
                    data.from_pydata([tuple(v[i] - origin[i] for i in range(3)) for v in vertices], [], faces)
                    obj.matrix_world = Matrix.Translation(origin)
                else:
                    data.from_pydata(vertices, [], faces)
                data.update(); data.materials.append(material)
                obj['_toon_geometry_sha256'] = mathcore.digest(_geometry_capture(obj))
                names = [obj.name]
                total_vertices += len(vertices)
            group_record = {'group_id': group_id, 'objects': names}
            if 'placement' in settings:
                group_record['placement'] = dict(effective['placement'])
            groups.append(group_record)
        actual = [_geometry_capture(obj) for obj in created]
    except Exception:
        for obj in created:
            if obj.get(SCOPE) == scope_id: bpy.data.objects.remove(obj, do_unlink=True)
        for item in reversed(resources):
            if item.users == 0 and item.get(SCOPE) == scope_id:
                (bpy.data.materials if isinstance(item, bpy.types.Material) else bpy.data.meshes).remove(item)
        for material, previous_color in reversed(borrowed):
            emit = next(n for n in material.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission')
            emit.inputs['Color'].default_value = previous_color
        raise
    fragments = [f for p in plans for f in p['fragments']]
    return {'enabled': True, 'objects': [o.name for o in created], 'object_names': [o.name for o in created],
            'groups': groups, 'geometry_sha256': mathcore.digest(actual), 'objects_geometry': actual,
            'camera': camera, 'sources': captures, 'fragments': fragments,
            'classified_edges': [e for p in plans for e in p['classified_edges']],
            'discarded': [e for p in plans for e in p['discarded']],
            'chains': [{'sources': c['sources'], 'closed': c['closed'], 'samples': len(c['samples'])} for c in all_chains],
            'joins': joins, 'visibility': 'grouped_native_placement' if 'placement' in settings else 'native_depth', 'cpu_visibility': 'not_called',
            'depth_bias': settings['depth_bias'], 'time_value': time_value,
            'counts': {'objects': len(created), 'vertices': total_vertices,
                       'faces': sum(len(g['faces']) for g in actual), 'curve_points': 0, 'splines': 0,
                       'source_vertices': sum(c['counts']['vertices'] for c in captures),
                       'source_edges': sum(c['counts']['edges'] for c in captures),
                       'samples': sum(len(c['samples']) for c in all_chains),
                       'fragments': len(fragments), 'chains': len(all_chains)},
            'seconds': time.perf_counter() - started,
            'limits': ['Opaque screen ribbon on fixed-topology polyhedral edges; smooth apparent contours are not qualified.',
                       'Each physical edge has one primary owner; independent group caps may overlap at shared vertices.',
                       'Kind changes across frames may change ownership; deterministic replay does not certify visual continuity.',
                       'Native depth and guarded camera clipping retain canonical source-u; no CPU hidden-edge deletion.',
                       'Integer-frame cache only; no source Text execution, MB, DOF, optical/xray or manual Ink merge.']}


def clear_owned(collection, scope_id):
    """Optional explicit cleanup; never name-match or unlink foreign consumers."""
    if collection is None or collection.get(SCOPE) != scope_id:
        _fail("OWNED_COLLECTION_REQUIRED", "Scope and collection must match exactly")
    owned = [obj for obj in collection.objects if obj.get(SCOPE) == scope_id and obj.get(GENERATED) is True]
    for obj in owned:
        if len(obj.users_collection) != 1 or obj.users_collection[0] != collection:
            _fail("OWNED_OBJECT_FOREIGN_LINK", "An owned output also has a foreign collection consumer")
    removed = []
    for obj in owned:
        name, data = obj.name, obj.data
        bpy.data.objects.remove(obj, do_unlink=True); removed.append(name)
        if data is not None and data.users == 0 and data.get(SCOPE) == scope_id and data.get(GENERATED) is True:
            if isinstance(data, bpy.types.Mesh): bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve): bpy.data.curves.remove(data)
    return {"scope_id": scope_id, "removed_objects": removed}
