"""Check actual carriers against source-derived pre-placement ribbons.

No save, render, ID creation, geometry writing or placement-builder call.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math
import bpy
from . import contract, outline_bpy, outline_core, stroke_core, state_contract

SCREEN_TOLERANCE = .0005
DEPTH_RELATIVE_TOLERANCE = 2e-6

def _need(value, message):
    if not value:
        raise contract.WorkflowError('VALIDATION_FAILED', 'Placement verification: '+message)

def _project_actual(point, camera):
    # Deliberately do not call the producer's project/unproject/place_spec.
    relative = [float(point[i])-camera['origin'][i] for i in range(3)]
    x, y, z = [sum(relative[i]*axis[i] for i in range(3)) for axis in camera['axes']]
    depth = -z
    _need(math.isfinite(depth) and depth > 0, 'actual vertex has nonpositive/nonfinite camera depth')
    if camera['type']=='PERSP':
        x *= camera['frame_depth']/depth; y *= camera['frame_depth']/depth
    x0,x1,y0,y1=camera['frame'];w,h=camera['resolution']
    return ((x-x0)/(x1-x0)*w*camera['pixel_ratio'],(y-y0)/(y1-y0)*h,depth)

def validate_frame(scene, collection, manifest, frame_row):
    _need(contract.is_placed(manifest), 'explicit placement-capable manifest required')
    frame=frame_row['frame'];settings=manifest['lines']
    _need(scene.frame_current==frame and scene.frame_subframe==0, 'actual evaluation frame differs')
    result={'status':'passed','groups':[], 'screen_tolerance_px':SCREEN_TOLERANCE,
            'depth_relative_tolerance':DEPTH_RELATIVE_TOLERANCE}
    if not settings['enabled']:
        _need(frame_row['groups']==[] and frame_row['objects']==[], 'disabled frame has carriers')
        return result
    camera=outline_bpy._camera(scene);depsgraph=bpy.context.evaluated_depsgraph_get()
    _need(depsgraph.scene_eval.name==scene.name, 'actual dependency graph scene differs')
    objects=[]
    for target in manifest['targets']:
        matches=[o for o in scene.objects if o.get(outline_bpy.OBJECT_ID)==target['object_id']]
        _need(len(matches)==1,'source Object UUID must resolve uniquely in actual scene')
        obj=matches[0]
        _need(obj.type=='MESH' and obj.data.get(outline_bpy.MESH_ID)==target['mesh_id'],
              'source Mesh UUID differs from explicit target')
        objects.append(obj)
    captures=[outline_bpy.capture_source(o,depsgraph) for o in objects]
    grouped=[outline_core.build_grouped_plan(c,camera,settings,frame,retain_assignments=False,
                allow_styled_jitter=contract.allows_styled_jitter(manifest)) for c in captures]
    source_chains=[]
    effective=contract.effective_placements(settings)
    _need(len(frame_row['groups'])==len(effective)==len(grouped[0]['groups']), 'group domain differs')
    for index,(declared,entry) in enumerate(zip(effective,frame_row['groups'])):
        group_id,placement=declared['group_id'],declared['placement']
        _need(entry['group_id']==group_id and entry['placement']==placement, 'effective placement declaration differs')
        bucket=grouped[0]['groups'][index];style=bucket['settings']
        _need(bucket['group_id']==group_id and style['placement']==placement,'recomputed source group differs')
        plane=placement.get('camera_depth') if placement['mode']=='camera_plane' else None
        if plane is not None:
            gap=1e-4*max(1.,plane,camera['near'])
            _need(plane-camera['near']>gap and camera['far']-plane>gap,'camera plane outside actual guarded frustum')
        vertices=[];triangles=[]
        width=style['width']*(camera['resolution'][1] if style['width_mode']=='frame_fraction' else 1)
        for source in grouped:
            for chain in source['groups'][index]['plan']['chains']:
                source_chains.append({'sources':chain['sources'],'closed':chain['closed'],'samples':len(chain['samples'])})
                samples=chain['samples']
                if len(samples)<(3 if chain['closed'] else 2):continue
                reference=(stroke_core.fragment_reference_frame(chain,camera)
                    if contract.allows_styled_jitter(manifest) and style['jitter_px']>0 else None)
                spec=(stroke_core.ribbon_mesh(samples,**chain['stroke_style'],
                    **({'reference_direction':reference['direction'],'reference_normal':reference['normal']}
                       if reference is not None else {})) if 'stroke_style' in chain else
                    outline_core.ribbon_mesh([s['screen'] for s in samples],[s['depth'] for s in samples],width,chain['closed']))
                base=len(vertices)
                for vertex in spec['vertices']:
                    _need(all(math.isfinite(v) for v in vertex) and vertex[2]>0,'source ribbon has invalid vertex')
                    if plane is not None:_need(plane+gap<vertex[2],'camera plane does not move source forward')
                    vertices.append((vertex[0],vertex[1],plane if plane is not None else vertex[2]))
                triangles.extend(tuple(base+i for i in t) for t in spec['triangles'])
        names=entry['objects']
        _need(len(names)==(1 if triangles else 0),'actual carrier presence differs from source-derived ribbons')
        xy_error=depth_error=0.
        if triangles:
            obj=collection.objects.get(names[0]);_need(obj is not None and obj.type=='MESH','actual carrier missing or not Mesh')
            mesh=obj.data
            _need(len(obj.modifiers)==0 and mesh.shape_keys is None and obj.instance_type=='NONE'
                  and len(obj.particle_systems)==0,
                  'carrier modifiers, shape keys, instancing and particles are not qualified')
            _need(len(mesh.vertices)==len(vertices),'actual vertex count differs')
            _need([tuple(p.vertices) for p in mesh.polygons]==triangles,'actual full triangle topology differs')
            for actual,expected in zip(mesh.vertices,vertices):
                if contract.allows_styled_jitter(manifest) and style['jitter_px'] > 0:
                    # Preserve native local precision during readback; values
                    # still come from the actual saved RNA matrix and vertex.
                    point=tuple(sum(float(obj.matrix_world[i][j])*float(actual.co[j]) for j in range(3))
                                +float(obj.matrix_world[i][3]) for i in range(3))
                else:
                    point=obj.matrix_world@actual.co
                measured=_project_actual(point,camera)
                xy=max(abs(measured[0]-expected[0]),abs(measured[1]-expected[1]))
                delta=abs(measured[2]-expected[2]);tol=DEPTH_RELATIVE_TOLERANCE*max(1.,expected[2])
                _need(math.isfinite(xy) and xy<=SCREEN_TOLERANCE,'actual screen footprint differs from pre-placement ribbon')
                _need(math.isfinite(delta) and delta<=tol,'actual camera depth differs from effective placement')
                xy_error=max(xy_error,xy);depth_error=max(depth_error,delta)
        maximum=max((v[2] for v in vertices),default=0.)
        result['groups'].append({'group_id':group_id,'objects':list(names),'placement':dict(placement),
            'vertex_count':len(vertices),'triangle_count':len(triangles),'max_screen_error_px':xy_error,
            'max_depth_error':depth_error,'expected_depth_max':maximum,
            'depth_tolerance_max':DEPTH_RELATIVE_TOLERANCE*max(1.,maximum)})
    _need(sum(g['vertex_count'] for g in result['groups'])==frame_row['counts']['vertices'],'frame vertex sum differs')
    _need(sum(g['triangle_count'] for g in result['groups'])==frame_row['counts']['faces'],'frame triangle sum differs')
    _need(state_contract.chain_fingerprint(source_chains,manifest)==frame_row['chain_fingerprint'],
          'complete source-derived canonical chain fingerprint differs')
    return result
