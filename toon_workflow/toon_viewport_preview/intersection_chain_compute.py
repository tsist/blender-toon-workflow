# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure owned-array intersection owner selection and caller-topology phase.

No bpy, RNA, GPU, brushes, curves or maps enter this module.  Owner/side rules
copy intersection_strokes' existing priority/front/least-facing algorithm.
"""
import numpy as np
from .intersection_phase_adapter import adapt_intersection_phase
from .ix_sources import channel_rows

_SOURCE_ARRAYS = ("world", "edges", "pair", "references", "supports")


def intersection_owners(data, meshes, compiled, view, perspective, checkpoint,channel=None):
    """Existing consumer owner order, support policy and self filter, unchanged."""
    checkpoint()
    n = len(data["world"])
    inv = np.linalg.inv(np.asarray(view, np.float64))
    tris = data["triangles"][data["pair"]]
    normals = np.cross(tris[:, :, 1] - tris[:, :, 0], tris[:, :, 2] - tris[:, :, 0])
    norms = np.linalg.norm(normals, axis=2)
    normals = np.divide(normals, norms[:, :, None], out=np.zeros_like(normals),
                        where=norms[:, :, None] > 0)
    toward = inv[:3, 3] - tris.mean(axis=2) if perspective else np.broadcast_to(inv[:3, 2], normals.shape)
    if perspective:
        toward /= np.linalg.norm(toward, axis=2)[:, :, None]
    facing = np.einsum("nsi,nsi->ns", normals, toward)
    owner = np.full(n, -1, np.int32)
    side = np.zeros(n, np.int8)
    refs = data["references"]
    from .line_functions import intersection_eligible
    eligible = intersection_eligible(data,meshes,compiled.get('disabled_materials',[]))
    if channel is None and not compiled["self_intersection"]:
        eligible &= refs[:, 0, 0] != refs[:, 1, 0]
    for gi, group in enumerate(compiled["groups"]):
        checkpoint()
        matches = np.zeros((n, 2), bool)
        for mi, mesh in enumerate(meshes):
            checkpoint()
            for support_side in (0, 1):
                ids = np.flatnonzero(refs[:, support_side, 0] == mi)
                local = refs[ids, support_side, 1]
                from .selector_bindings import face_membership
                whole,faces=face_membership(mesh['name'],mesh['face_material_names'],group,mesh.get('selector_meta'))
                match=faces[mesh['triangle_polygons'][local]]
                if whole:
                    match[:] = True
                matches[ids, support_side] = match
        group_eligible=eligible
        if channel is not None and not group.get('self_channels',{}).get(channel,compiled['self_intersection']):
            group_eligible=eligible&(refs[:,0,0]!=refs[:,1,0])
        keep = (owner < 0) & group_eligible & matches.any(axis=1)
        if compiled["policy"] == "priority_front_side.v1":
            prefer1 = ~matches[:, 0] | (matches[:, 1] & (facing[:, 1] > 0) & ~(facing[:, 0] > 0))
        elif compiled["policy"] == "priority_least_absolute_facing.v1":
            prefer1 = ~matches[:, 0] | (matches[:, 1] & (abs(facing[:, 1]) < abs(facing[:, 0])))
        else:
            raise ValueError("Unknown intersection support policy")
        owner[keep] = gi
        side[keep & prefer1] = 1
    checkpoint()
    return owner, side


def compute_intersection_phases(payload, checkpoint):
    """Return original-instance phase/active with an independent explicit IX gap."""
    include_arc=payload.get('include_dash_metric',False)
    if type(include_arc) is not bool:raise ValueError('include_dash_metric must be a boolean')
    controls = payload["controls"]["brushes"]
    requested = [actual for actual, row in controls.items() if row["intersection_enabled"]]
    empty = dict(lookup={}, enabled_actuals=[], requested_actuals=requested, reports=[])
    if include_arc:empty['arc_lookup']={}
    checkpoint()
    snapshot = payload.get("intersection", {})
    if not requested:
        return dict(empty, status=dict(state="disabled", reason="intersection chain controls off"))
    if not snapshot.get("global_enabled", False):
        return dict(empty, status=dict(state="disabled", reason="viewport intersection switch off"))
    compiled = snapshot.get("plan")
    if compiled is None:
        return dict(empty, status=dict(state="disabled", reason="no intersection request"))
    data = snapshot.get("sources")
    if data is None:
        return dict(empty, status=dict(state="pending", reason="intersection capture unavailable; not computed"))
    frame = payload["frame"]
    meshes = payload["meshes"]
    owner, side = intersection_owners(data, meshes, compiled, frame["view"],
                                      frame["perspective"], checkpoint)
    channel_owners={ch:intersection_owners(data,meshes,compiled,frame['view'],frame['perspective'],checkpoint,ch)
                    for ch in ('VISIBLE','HIDDEN')} if compiled.get('channel_ownership') else None
    runtime_groups = {row["id"]: row for row in payload["settings"]["groups"]}
    counts = [len(mesh["triangles"]) for mesh in meshes]
    lookup = {}
    if include_arc:arc_lookup={}
    enabled = []
    reports = []
    gap_by_actual = {}
    for gi, group in enumerate(compiled["groups"]):
        checkpoint()
        runtime = runtime_groups.get(group["id"])
        # Ownership is assigned before runtime/phase disables, exactly as in
        # the consumer: a disabled high-priority owner never falls to a lower one.
        if runtime is not None and not runtime["enabled"]:
            continue
        ids = np.flatnonzero(owner == gi)
        for channel, channel_enabled, actual, unused_brush in channel_rows(group):
            checkpoint()
            if channel_owners is not None:
                owner,side=channel_owners[channel];ids=np.flatnonzero(owner==gi)
            control = controls.get(actual)
            if control is None or not control['intersection_enabled']:
                continue
            if actual not in enabled:
                enabled.append(actual)
            radius = control['intersection_max_gap_px'] * payload['pixel_scale']
            gap_by_actual[actual] = radius
            if not channel_enabled or payload["settings"]["visibility"] not in (channel, "BOTH"):
                continue
            if not len(ids):
                reports.append(dict(group_id=group["id"], actual_brush=actual,
                                    channel=channel, instances=0, max_gap_px=radius,
                                    status="no owned sources"))
                continue
            selected = {name: data[name][ids].copy() for name in _SOURCE_ARRAYS}
            policy = dict(control["policy"])
            policy["min_length_px"] *= payload["pixel_scale"]
            result = adapt_intersection_phase(
                selected, group_id=group["id"], actual_brush_id=actual, channel=channel,
                full_mvp=frame["mvp"], width=frame["width"], height=frame["height"],
                mesh_count=len(meshes), mesh_triangle_counts=counts,
                endpoint_count=snapshot.get("endpoint_count"), policy=policy,
                max_gap_px=radius, checkpoint=checkpoint,include_arc=include_arc)
            for ordinal, index in enumerate(ids):
                if ordinal % 256 == 0:
                    checkpoint()
                key = (group["id"], actual, channel, int(index))
                if key in lookup:
                    raise ValueError("Repeated intersection consumer lookup identity")
                lookup[key] = (tuple(float(value) for value in result["phase"][ordinal]),
                               bool(result["active"][ordinal]))
                if include_arc:arc_lookup[key]=tuple(float(value) for value in result['arc_px'][ordinal])
            reports.append(dict(group_id=group["id"], actual_brush=actual, channel=channel,
                                instances=len(ids), canonical_sources=len(result["canonical_map"]),
                                support_side1_count=int(np.count_nonzero(side[ids])),
                                counts=result["policy_plan"]["evidence"]["counts"],
                                evidence=result["evidence"], qualification=result["qualification"],
                                boundary_gap_setting_not_applied=True))
            if radius > 0:
                reports[-1].update(intersection_max_gap_px=control["intersection_max_gap_px"],
                    applied_max_gap_px=radius, gap_linker=result["gap_linker"]["evidence"],
                    gap_qualification=result["gap_linker"]["qualification"])
    checkpoint()
    result = dict(lookup=lookup, enabled_actuals=enabled, requested_actuals=requested,
                reports=reports, status=dict(state="ready", max_gap_px=0.0,
                    native_equivalent=False, source_instances_retained=True,
                    boundary_gap_setting_not_applied=True))
    if any(radius > 0 for radius in gap_by_actual.values()):
        result["status"].update(max_gap_px=max(gap_by_actual.values()),
            max_gap_px_by_actual=gap_by_actual,
            profile="caller_declared_intersection_unique_degree1_gap.v1",
            policy_link_length_px=0, gap_geometry_generated=False,
            applied_gap_link_count=sum(row.get("gap_linker", {}).get("applied_link_count", 0) for row in reports))
    if include_arc:result['arc_lookup']=arc_lookup
    return result
