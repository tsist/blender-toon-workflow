"""Deterministic fixed-topology outline math. No bpy or filesystem access.

SPDX-License-Identifier: GPL-3.0-or-later
Own implementation informed by the source-u/ribbon experiments recorded in P2.
"""
import hashlib
import json
import math

from .contract import WorkflowError


def fail(code, message, path="$.lines"):
    raise WorkflowError(code, message, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def add(a, b): return tuple(x + y for x, y in zip(a, b))
def sub(a, b): return tuple(x - y for x, y in zip(a, b))
def mul(a, scalar): return tuple(x * scalar for x in a)
def dot(a, b): return sum(x * y for x, y in zip(a, b))
def length(a): return math.sqrt(dot(a, a))
def lerp(a, b, u): return add(mul(a, 1 - u), mul(b, u))


def unit(a):
    n = length(a)
    if not math.isfinite(n) or n < 1e-12:
        fail("DEGENERATE_OUTLINE_VECTOR", "A direction or evaluated normal is degenerate")
    return mul(a, 1 / n)


def canonical_cycle(ids):
    values = list(ids)
    if len(values) < 3 or len(set(values)) != len(values):
        fail("DEGENERATE_SOURCE_FACE", "Faces require at least three distinct point IDs")
    start = min(range(len(values)), key=values.__getitem__)
    return values[start:] + values[:start]


def topology_digest(point_ids, edges, faces):
    """Enumeration-independent directed topology, deliberately excluding positions."""
    return digest({"points": sorted(point_ids),
                   "edges": sorted([[int(e[0]), *sorted(e[1])] for e in edges]),
                   "faces": sorted(canonical_cycle(f) for f in faces)})


def validate_ids(values, domain):
    if any(type(v) is not int or not 0 < v <= 2147483647 for v in values):
        fail("INVALID_SOURCE_ID", "Positive signed-32-bit integer IDs required", domain)
    if len(set(values)) != len(values):
        fail("DUPLICATE_SOURCE_ID", "Source IDs must be unique within their domain", domain)


def classify(source, camera, settings):
    faces = source["faces"]
    facing = []
    normals = []
    for face in faces:
        direction = (sub(camera["origin"], face["center"]) if camera["type"] == "PERSP"
                     else camera["toward_camera"])
        normal = unit(face["normal"])
        normals.append(normal)
        facing.append(dot(normal, unit(direction)))
    requested = set(settings["kinds"])
    result = []
    for edge in source["edges"]:
        adjacent = edge["faces"]
        if len(adjacent) > 2:
            fail("NONMANIFOLD_SOURCE_EDGE", "More than two adjacent faces are not qualified", "$.targets")
        classes = []
        angle = None
        if len(adjacent) == 1:
            classes.append("boundary")
        if len(adjacent) == 2:
            a, b = adjacent
            cosine = max(-1.0, min(1.0, dot(normals[a], normals[b])))
            angle = math.degrees(math.acos(cosine))
            # Deterministic tie rule; no order-dependent temporal hysteresis.
            if (facing[a] > 1e-10) != (facing[b] > 1e-10):
                classes.append("silhouette")
            if angle >= settings["crease_angle_degrees"]:
                classes.append("crease")
            if faces[a]["material"] != faces[b]["material"]:
                classes.append("material_boundary")
        if edge["marked"]:
            classes.append("marked")
        key = [source["object_id"], source["mesh_id"], edge["edge_id"], *edge["vertices"]]
        result.append({"key": key, "classes": classes,
                       "selected_classes": [c for c in classes if c in requested],
                       "vertices": edge["vertices"], "crease_angle_degrees": angle})
    return sorted(result, key=lambda e: e["key"])


def to_camera(point, camera):
    relative = sub(point, camera["origin"])
    return tuple(dot(relative, axis) for axis in camera["axes"])


def from_camera(point, camera):
    value = camera["origin"]
    for component, axis in zip(point, camera["axes"]):
        value = add(value, mul(axis, component))
    return value


def project(point, camera):
    x, y, z = to_camera(point, camera)
    depth = -z
    x0, x1, y0, y1 = camera["frame"]
    if camera["type"] == "PERSP":
        if abs(depth) < 1e-10:
            fail("PROJECTIVE_CAMERA_PLANE", "Cannot project a point on the camera plane")
        factor = depth / camera["frame_depth"]
        x, y = x / factor, y / factor
    width, height = camera["resolution"]
    # Vertical-pixel metric: unequal physical pixels require horizontal correction.
    return ((x - x0) / (x1 - x0) * width * camera["pixel_ratio"],
            (y - y0) / (y1 - y0) * height, depth)


def unproject(x, y, depth, camera):
    x0, x1, y0, y1 = camera["frame"]
    width, height = camera["resolution"]
    cx = x0 + x / camera["pixel_ratio"] / width * (x1 - x0)
    cy = y0 + y / height * (y1 - y0)
    if camera["type"] == "PERSP":
        factor = depth / camera["frame_depth"]
        cx, cy = cx * factor, cy * factor
    return from_camera((cx, cy, -depth), camera)


def clip_segment(a, b, camera, guard_px=0.0):
    """Six affine halfspaces; returned u remains the original world-edge u."""
    x0, x1, y0, y1 = camera["frame"]
    width, height = camera["resolution"]
    gx = guard_px / camera["pixel_ratio"] / width * (x1 - x0)
    gy = guard_px / height * (y1 - y0)
    x0, x1, y0, y1 = x0 - gx, x1 + gx, y0 - gy, y1 + gy
    def planes(point):
        x, y, z = to_camera(point, camera)
        d = -z
        f = d / camera["frame_depth"] if camera["type"] == "PERSP" else 1.0
        return (x - x0 * f, x1 * f - x, y - y0 * f, y1 * f - y,
                d - camera["near"], camera["far"] - d)
    lo, hi = 0.0, 1.0
    for va, vb in zip(planes(a), planes(b)):
        delta = vb - va
        if abs(delta) < 1e-14:
            if va < 0:
                return None
        elif delta > 0:
            lo = max(lo, -va / delta)
        else:
            hi = min(hi, -va / delta)
    lo, hi = max(0.0, lo), min(1.0, hi)
    return [lo, hi] if hi - lo > 1e-12 else None


def style_offset(key, u, time_value, settings):
    if u <= 0 or u >= 1 or settings["jitter_px"] == 0:
        return 0.0
    bucket = math.floor(time_value / settings["cadence_frames"])
    raw = hashlib.sha256(json.dumps([key, settings["seed"], bucket], separators=(",", ":")).encode()).digest()
    a = int.from_bytes(raw[:4], "big") / 4294967295 * 2 - 1
    b = int.from_bytes(raw[4:8], "big") / 4294967295 * 2 - 1
    return settings["jitter_px"] * (a * math.sin(math.pi * u) + b * math.sin(3 * math.pi * u)) / 2


def chain_fragments(fragments):
    """Exact-ID joins; boundary, manifold and authored loose segments share the graph."""
    incidence = {}
    def endpoint(index, end):
        f = fragments[index]
        u = f["source_interval"][end]
        if abs(u) < 1e-10:
            return tuple(f["source_key"][:2] + ["vertex", f["source_key"][3]])
        if abs(u - 1) < 1e-10:
            return tuple(f["source_key"][:2] + ["vertex", f["source_key"][4]])
        return ("clip", index, end)
    for i in range(len(fragments)):
        for end in (0, 1):
            incidence.setdefault(endpoint(i, end), []).append((i, end))
    starts = [(i, end) for i in range(len(fragments)) for end in (0, 1)
              if len(incidence[endpoint(i, end)]) != 2]
    unused = set(range(len(fragments)))
    start_cursor, seed = 0, 0
    result = []
    while unused:
        while start_cursor < len(starts) and starts[start_cursor][0] not in unused:
            start_cursor += 1
        if start_cursor < len(starts):
            index, entry = starts[start_cursor]
        else:
            while seed not in unused:
                seed += 1
            index, entry = seed, 0
        start = endpoint(index, entry)
        samples, sources, closed = [], [], False
        while index in unused:
            unused.remove(index)
            fragment = fragments[index]
            values = fragment["samples"] if entry == 0 else list(reversed(fragment["samples"]))
            if samples:
                if length(sub(samples[-1]["world"], values[0]["world"])) > 1e-5:
                    fail("DISCONNECTED_SOURCE_JOIN", "Exact source endpoint has inconsistent geometry")
                samples.extend(values[1:])
            else:
                samples.extend(values)
            sources.append({"key": fragment["source_key"], "source_interval": fragment["source_interval"],
                            "reversed": bool(entry), "classes": fragment["classes"]})
            node = endpoint(index, 1 - entry)
            if node == start:
                closed = True
                break
            if len(incidence[node]) != 2:
                break
            pending = [item for item in incidence[node] if item[0] in unused]
            if not pending:
                break
            index, entry = pending[0]
        if closed:
            samples = samples[:-1]
        result.append({"samples": samples, "sources": sources, "closed": closed})
    return result


def ribbon_mesh(points, depths, width, closed=False, miter_limit=2.0):
    """Opaque rectangle union with explicit cyclic miter/bevel and butt open caps."""
    if len(points) != len(depths) or len(points) < (3 if closed else 2) or width <= 0:
        fail("INVALID_RIBBON_INPUT", "Invalid point/depth/width data")
    vertices, triangles, joins = [], [], []
    count = len(points)
    segments = count if closed else count - 1
    directions, normals = [], []
    half = width / 2
    def cross(a, b): return a[0] * b[1] - a[1] * b[0]
    def offset(p, n, amount): return add(p, mul(n, amount))
    def triangle(a, b, c, ds):
        if abs(cross(sub(b, a), sub(c, a))) < 1e-10:
            return
        base = len(vertices)
        vertices.extend([[p[0], p[1], d] for p, d in zip((a, b, c), ds)])
        triangles.append([base, base + 1, base + 2])
    for i in range(segments):
        j = (i + 1) % count
        direction = sub(points[j], points[i])
        if length(direction) < 1e-7:
            fail("DEGENERATE_SCREEN_SEGMENT", "A ribbon segment projects to zero length")
        direction = unit(direction)
        normal = (-direction[1], direction[0])
        directions.append(direction); normals.append(normal)
        al, ar = offset(points[i], normal, half), offset(points[i], normal, -half)
        bl, br = offset(points[j], normal, half), offset(points[j], normal, -half)
        triangle(al, ar, br, (depths[i], depths[i], depths[j]))
        triangle(al, br, bl, (depths[i], depths[j], depths[j]))
    for i in (range(count) if closed else range(1, count - 1)):
        prev, nxt = (i - 1) % segments, i % segments
        a, b = directions[prev], directions[nxt]
        turn = cross(a, b)
        if abs(turn) < 1e-8:
            if dot(a, b) <= 0:
                # Each adjacent segment already has its complete rectangle.
                # End both at this point; no bridge or undefined miter is added.
                joins.append({"point": i, "style": "reversal_butt_caps", "miter_ratio": None})
            continue
        side = -1 if turn > 0 else 1
        center = points[i]
        left = offset(center, normals[prev], side * half)
        right = offset(center, normals[nxt], side * half)
        distance = cross(sub(right, left), b) / turn
        miter = offset(left, a, distance)
        ratio = length(sub(miter, center)) / half
        if ratio <= miter_limit:
            triangle(center, left, miter, (depths[i],) * 3)
            triangle(center, miter, right, (depths[i],) * 3)
            style = "miter"
        else:
            triangle(center, left, right, (depths[i],) * 3)
            style = "bevel_limit_fallback"
        joins.append({"point": i, "style": style, "miter_ratio": ratio})
    return {"vertices": vertices, "triangles": triangles, "joins": joins,
            "closed": closed,
            "caps": ("cyclic_with_reversal_butt_caps" if closed and any(
                join["style"] == "reversal_butt_caps" for join in joins)
                else "cyclic_join" if closed else "butt")}


def build_plan(source, camera, settings, time_value, *, allow_styled_jitter=False):
    if not settings["enabled"]:
        return {"fragments": [], "chains": [], "classified_edges": [], "discarded": []}
    classified = classify(source, camera, settings)
    return _build_plan_classified(source, camera, settings, time_value, classified,
                                  allow_styled_jitter=allow_styled_jitter)


def _build_plan_classified(source, camera, settings, time_value, classified, *,
                           allow_styled_jitter=False):
    """Original sampling body shared by legacy and disjoint primary-owner bins."""
    if 'stroke' in settings:
        from . import stroke_core
        styled = stroke_core.build_plan_classified(
            source, camera, settings, time_value, classified,
            allow_styled_jitter=allow_styled_jitter)
        if styled is not None:
            return styled
    fragments, discarded = [], []
    backend = settings["backend"]
    width = settings["width"] * (camera["resolution"][1] if settings["width_mode"] == "frame_fraction" else 1)
    guard = width + settings["jitter_px"] + 1
    for edge in classified:
        if not edge["selected_classes"]:
            continue
        a, b = (source["positions"][str(pid)] for pid in edge["vertices"])
        interval = clip_segment(a, b, camera, guard) if backend == "screen_ribbon" else [0.0, 1.0]
        if interval is None:
            discarded.append({"key": edge["key"], "reason": "outside_guarded_camera_frustum"})
            continue
        lo, hi = interval
        use_screen = backend == "screen_ribbon" or settings["jitter_px"] != 0
        if use_screen:
            pa, pb = project(lerp(a, b, lo), camera), project(lerp(a, b, hi), camera)
            tangent = sub(pb[:2], pa[:2])
            screen_length = length(tangent)
            if screen_length < 1e-7:
                discarded.append({"key": edge["key"], "reason": "zero_projected_length"})
                continue
            normal = (-tangent[1] / screen_length, tangent[0] / screen_length)
        else:
            screen_length, normal = 0, (0, 0)
        # At zero jitter straight source edges need only endpoints. Dense style sampling
        # is bounded and anchored in full canonical source-u, never fragment phase.
        steps = max(8, int(math.ceil(screen_length / 8))) if settings["jitter_px"] else 1
        if steps > 2048:
            fail("OUTLINE_SAMPLE_LIMIT", "One source edge exceeds the explicit 2048 step bound")
        us = sorted(set([lo, hi] + [i / steps for i in range(1, steps) if lo < i / steps < hi]))
        samples = []
        for u in us:
            world = lerp(a, b, u)
            offset = style_offset(edge["key"], u, time_value, settings)
            if use_screen:
                p = project(world, camera)
                screen = add(p[:2], mul(normal, offset))
                depth = p[2] - settings["depth_bias"]
                if backend == "screen_ribbon" and depth < camera["near"] - 1e-8:
                    fail("OUTLINE_BIAS_CROSSES_NEAR", "Depth bias crosses the actual camera near plane")
                shifted = unproject(*screen, depth, camera)
            else:
                screen, depth = None, None
                if settings["depth_bias"] and camera["type"] == "PERSP":
                    local = to_camera(world, camera)
                    d = -local[2]
                    if abs(d) < 1e-10:
                        fail("PROJECTIVE_CAMERA_PLANE", "Camera-depth bias is undefined at the perspective camera plane")
                    shifted = from_camera(mul(local, (d - settings["depth_bias"]) / d), camera)
                else:
                    shifted = add(world, mul(camera["toward_camera"], settings["depth_bias"]))
            samples.append({"source_u": u, "world": list(shifted),
                            "source_world": list(world), "screen": list(screen) if screen else None,
                            "depth": depth, "offset_px": offset})
        fragments.append({"source_key": edge["key"], "source_interval": [lo, hi],
                          "classes": edge["selected_classes"], "samples": samples})
    return {"classified_edges": classified, "fragments": fragments,
            "chains": chain_fragments(fragments), "discarded": discarded,
            "width": width, "visibility": "native_depth", "cpu_visibility": "not_called"}


def build_grouped_plan(source, camera, settings, time_value, *, retain_assignments=True,
                       allow_styled_jitter=False):
    """Classify once, remove disabled categories, then select one primary owner.

    Callers supply normalized 0.2 settings. Source keys and absolute source-u
    remain untouched; explicit style seed/cadence overrides are not salted or
    restarted by group identity. Each bin uses the original sampling body.
    """
    if not settings["enabled"]:
        return {"groups": [], "assignments": []}
    if settings["backend"] != "screen_ribbon":
        fail("UNSUPPORTED_OUTLINE_BACKEND", "Grouped outlines require screen_ribbon")
    kinds, order = settings["kinds"], settings["kind_order"]
    if (not isinstance(order, list) or len(order) != len(kinds)
            or len(set(order)) != len(order) or set(order) != set(kinds)):
        fail("INVALID_OUTLINE_KIND_ORDER", "kind_order must be a permutation of global kinds", "$.lines.kind_order")
    base = {key: value for key, value in settings.items() if key not in ("groups", "kind_order")}
    groups = [("global", base)]
    owners, disabled, ids = {}, set(), {"global"}
    allowed_style = {"width_mode", "width", "color", "jitter_px", "cadence_frames", "seed"}
    if 'stroke' in settings:
        allowed_style.add('stroke')
    if 'placement' in settings:
        allowed_style.add('placement')
    for group in settings["groups"]:
        group_id = group["group_id"]
        if not isinstance(group_id, str) or not group_id or group_id in ids:
            fail("INVALID_OUTLINE_GROUP", "Group IDs must be unique and cannot be global", "$.lines.groups")
        ids.add(group_id)
        if type(group["enabled"]) is not bool or set(group["style"]) - allowed_style:
            fail("INVALID_OUTLINE_GROUP", "Invalid enabled flag or style override", "$.lines.groups")
        effective = {**base, **group["style"]}
        if effective["width_mode"] not in {"pixel", "frame_fraction"}:
            fail("OUTLINE_WIDTH_DOMAIN", "Grouped ribbon width requires pixel or frame_fraction", "$.lines.groups")
        if group["enabled"]:
            groups.append((group_id, effective))
        if source["object_id"] not in group["object_ids"]:
            continue
        for kind in group["kinds"]:
            if kind in owners:
                fail("OUTLINE_GROUP_OVERLAP", "Object/kind ownership overlaps across groups", "$.lines.groups")
            owners[kind] = group
            if not group["enabled"]:
                disabled.add(kind)
    if base["width_mode"] not in {"pixel", "frame_fraction"}:
        fail("OUTLINE_WIDTH_DOMAIN", "Grouped ribbon width requires pixel or frame_fraction")
    classified = classify(source, camera, settings)
    buckets = {group_id: [] for group_id, _effective in groups}
    assignments, provenance = [], {}
    for edge in classified:
        physical = list(edge["classes"])
        selected = list(edge["selected_classes"])
        excluded = [kind for kind in selected if kind in disabled]
        remaining = [kind for kind in selected if kind not in disabled]
        primary = next((kind for kind in order if kind in remaining), None)
        owner = owners.get(primary)
        group_id = (owner["group_id"] if owner else "global") if primary is not None else None
        if not retain_assignments and primary is None:
            continue
        row = {"key": edge["key"], "physical_classes": physical,
               "global_selected_classes": selected, "excluded_classes": excluded,
               "remaining_classes": remaining, "primary_kind": primary,
               "group_id": group_id, "selected_classes": [primary] if primary is not None else []}
        assignments.append(row)
        if primary is not None:
            buckets[group_id].append({**edge, "selected_classes": [primary]})
            provenance[tuple(edge["key"])] = row
    output = []
    for group_id, effective in groups:
        plan = _build_plan_classified(source, camera, effective, time_value, buckets[group_id],
                                      allow_styled_jitter=allow_styled_jitter)
        for chain in plan["chains"]:
            for item in chain["sources"]:
                row = provenance[tuple(item["key"])]
                item.update(group_id=group_id, primary_kind=row["primary_kind"],
                            physical_classes=list(row["physical_classes"]))
        output.append({"group_id": group_id, "settings": effective, "plan": plan})
    return {"groups": output, "assignments": assignments if retain_assignments else []}
