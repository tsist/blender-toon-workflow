# SPDX-License-Identifier: GPL-3.0-or-later
"""Read evaluated geometry, classify real mesh edges, then clip/project in 2D.

No Blender datablocks are created or edited. Arrays own their data after capture.
This preview does not claim the exact backend's semantic or arithmetic contract.
"""
import time
import numpy as np


def _array(collection, name, width, dtype):
    value = np.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(name, value)
    return value.reshape((-1, width)) if width > 1 else value


class Mesh:
    def __init__(self, obj, graph, *, matrix_world=None, source_name=None,
                 instance_key=None, material_names=None,selector_context=None,selector_parent=None,material_blocks=None):
        evaluated = obj.evaluated_get(graph)
        self.holdout=False
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
        try:
            mesh.calc_loop_triangles()
            local = _array(mesh.vertices, 'co', 3, np.float32)
            world = np.asarray(evaluated.matrix_world if matrix_world is None
                               else matrix_world, dtype=np.float64)
            self.vertices = np.asarray(local @ world[:3, :3].T + world[:3, 3], dtype=np.float32)
            self.triangles = _array(mesh.loop_triangles, 'vertices', 3, np.int32)
            triangle_loops = _array(mesh.loop_triangles,'loops',3,np.int32)
            self.triangle_loops=triangle_loops
            self.triangle_polygons = _array(mesh.loop_triangles, 'polygon_index', 1, np.int32)
            self.edges = _array(mesh.edges, 'vertices', 2, np.int32)
            self.name = obj.name if source_name is None else source_name
            self.pointer = obj.as_pointer()
            self.instance_key = (None if instance_key is None
                                 else tuple(instance_key))
            normals = _array(mesh.polygons, 'normal', 3, np.float64)
            # A singular transform has no well-defined normal transform.
            if not np.isfinite(world).all() or not np.isfinite(self.vertices).all():
                raise ValueError('Non-finite evaluated transform or vertices: '+obj.name)
            try:
                normal_matrix = np.linalg.inv(world[:3, :3]).T
                self.normals = normals @ normal_matrix.T
                self.normal_policy='inverse_transpose'
            except np.linalg.LinAlgError:
                starts=_array(mesh.polygons,'loop_start',1,np.int32)
                totals=_array(mesh.polygons,'loop_total',1,np.int32)
                vertices=_array(mesh.loops,'vertex_index',1,np.int32)
                points=self.vertices[vertices].astype(np.float64)
                following=np.arange(len(points))+1
                if len(starts):following[starts+totals-1]=starts
                self.normals=(np.add.reduceat(np.cross(points,points[following]),starts)
                              if len(starts) else np.empty((0,3)))
                self.normal_policy='world_polygon_area_for_singular'
            length = np.linalg.norm(self.normals, axis=1)
            self.normals /= np.maximum(length[:, None], np.finfo(np.float64).tiny)
            centers = _array(mesh.polygons, 'center', 3, np.float64)
            self.centers = centers @ world[:3, :3].T + world[:3, 3]
            loop_edges = _array(mesh.loops, 'edge_index', 1, np.int32)
            self.loop_edge = loop_edges.copy()
            self.loop_vertex = _array(mesh.loops,'vertex_index',1,np.int32)
            self.face_start = _array(mesh.polygons,'loop_start',1,np.int32)
            totals = _array(mesh.polygons, 'loop_total', 1, np.int32)
            following=np.arange(len(mesh.loops),dtype=np.int32)+1
            if len(totals):following[self.face_start+totals-1]=self.face_start
            loop_faces = np.repeat(np.arange(len(mesh.polygons), dtype=np.int32), totals)
            order = np.argsort(loop_edges, kind='stable')
            sorted_edges = loop_edges[order]
            unique, first, counts = np.unique(sorted_edges, return_index=True, return_counts=True)
            self.counts = np.zeros(len(self.edges), dtype=np.int32)
            self.faces0 = np.zeros(len(self.edges), dtype=np.int32)
            self.faces1 = np.zeros(len(self.edges), dtype=np.int32)
            self.counts[unique] = counts
            self.faces0[unique] = loop_faces[order[first]]
            self.faces1[unique] = loop_faces[order[first + counts - 1]]
            self.extra_faces = [(int(edge),loop_faces[order[offset:offset+count]])
                for edge,offset,count in zip(unique,first,counts) if count>2]
            self.corners0=np.full((len(self.edges),2),-1,dtype=np.int32)
            self.corners1=self.corners0.copy()
            for target,locations in ((self.corners0,order[first]),(self.corners1,order[first+counts-1])):
                pairs=np.stack((locations,following[locations]),axis=1)
                flip=self.loop_vertex[locations]!=self.edges[unique,0]
                pairs[flip]=pairs[flip,::-1]
                target[unique]=pairs
            mark = mesh.attributes.get('freestyle_edge')
            if mark is not None:
                if mark.domain != 'EDGE' or mark.data_type != 'BOOLEAN':
                    raise ValueError('freestyle_edge requires a BOOLEAN EDGE attribute')
                self.marks = _array(mark.data, 'value', 1, np.bool_)
            else:
                self.marks = np.zeros(len(self.edges), dtype=np.bool_)
            material = _array(mesh.polygons, 'material_index', 1, np.int32)
            self.face_material=material
            from .selector_capture import Context
            selectors=selector_context if selector_context is not None else Context()
            self.selector_meta=selectors.metadata(obj,material_blocks if material_blocks is not None else
                [slot.material for slot in evaluated.material_slots],material,parent=selector_parent)
            self.material_names=(list(material_names) if material_names is not None else
                [slot.material.name if slot.material else '' for slot in obj.material_slots])
            self.face_material_names=np.array([self.material_names[i] if 0<=i<len(self.material_names) else ''
                                               for i in material],dtype=object)
            self.material_boundary = ((material[self.faces0] != material[self.faces1]) & (self.counts == 2)
                                      if len(material) else np.zeros(len(self.edges),dtype=np.bool_))
            dots = (np.einsum('ij,ij->i', self.normals[self.faces0], self.normals[self.faces1])
                    if len(self.normals) else np.ones(len(self.edges)))
            self.normal_dot = np.clip(dots, -1.0, 1.0)
            self.layers=[]
            for attr in mesh.color_attributes:
                linear=_array(attr.data,'color',4,np.float32)
                if attr.data_type=='BYTE_COLOR':
                    srgb=_array(attr.data,'color_srgb',4,np.float32)
                else:
                    srgb=linear.copy()
                    rgb=srgb[:,:3]
                    srgb[:,:3]=np.where(rgb<=0.0031308,12.92*rgb,1.055*np.maximum(rgb,0)**(1/2.4)-0.055)
                self.layers.append(dict(name=attr.name,domain=attr.domain,
                    linear_rgba=linear,srgb_rgba=srgb))
            from .style import PreparedLayers
            self.layers=PreparedLayers(self.layers)
            self.uv_layers=[dict(name=layer.name,uv=_array(layer.data,'uv',2,np.float32)) for layer in mesh.uv_layers]
            sharp=_array(mesh.edges,'use_edge_sharp',1,np.bool_)
            smooth=_array(mesh.polygons,'use_smooth',1,np.bool_)
            self.smoothing=sharp.copy()
            self.triangle_supports=np.full((len(self.edges),2),-1,dtype=np.int32)
            if len(smooth):
                from .dependencies import load
                compute_flags=load('smoothing_flags').compute_flags
                flags=compute_flags(dict(edges=self.edges,edge_sharp=sharp,face_smooth=smooth,
                    face_start=self.face_start,face_total=totals,loop_vertex=self.loop_vertex,
                    loop_edge=loop_edges,triangles=self.triangles,
                    triangle_loop=triangle_loops,
                    triangle_polygon=self.triangle_polygons))
                self.smoothing=np.asarray(flags['edge_smoothing'])
                edge_ids=flags['triangle_edge_ids'].reshape(-1)
                real=edge_ids>=0
                tids=np.repeat(np.arange(len(self.triangles),dtype=np.int32),3)[real]
                edge_ids=edge_ids[real]
                order=np.argsort(edge_ids,kind='stable')
                unique,first,count=np.unique(edge_ids[order],return_index=True,return_counts=True)
                self.triangle_supports[unique,0]=tids[order[first]]
                self.triangle_supports[unique,1]=tids[order[first+count-1]]
            self._triangle_contours(triangle_loops)
            self.depth_batch = None
            self.nonmanifold_count = int(np.count_nonzero(self.counts > 2))
        finally:
            evaluated.to_mesh_clear()

    def _triangle_contours(self,triangle_loops):
        """Select contours on the same triangulated surface used for occlusion.

        Nonplanar quads can have a contour on their tessellation diagonal.
        Preserve original edge order/flags and append only missing diagonals.
        """
        self.original_edge_count=len(self.edges)
        pairs=self.triangles[:,((0,1),(1,2),(2,0))].reshape((-1,2))
        corners=triangle_loops[:,((0,1),(1,2),(2,0))].reshape((-1,2))
        joined=np.concatenate((np.sort(self.edges,axis=1),np.sort(pairs,axis=1)))
        # Mesh vertex IDs are nonnegative int32. Packed pairs sort in exactly
        # the same lexicographic order, without NumPy's structured-row sort.
        keys=(joined[:,0].astype(np.uint64)<<32)|joined[:,1].astype(np.uint64)
        packed,inverse=np.unique(keys,return_inverse=True)
        unique=np.column_stack((packed>>32,packed&0xffffffff)).astype(np.int32)
        original_u=inverse[:len(self.edges)];triangle_u=inverse[len(self.edges):]
        owned=np.zeros(len(unique),bool);owned[original_u]=True
        virtual_u=np.flatnonzero(~owned)
        edge_u=np.concatenate((original_u,virtual_u))
        count=np.bincount(triangle_u,minlength=len(unique))
        first_triangle=np.full(len(unique),-1,np.int32);last_triangle=first_triangle.copy()
        first_corner=np.full(len(unique),-1,np.int32);last_corner=first_corner.copy()
        order=np.argsort(triangle_u,kind='stable')
        ids,first,counts=np.unique(triangle_u[order],return_index=True,return_counts=True)
        first_corner[ids]=order[first];last_corner[ids]=order[first+counts-1]
        first_triangle[ids]=first_corner[ids]//3;last_triangle[ids]=last_corner[ids]//3
        self.triangle_supports=np.stack((first_triangle[edge_u],last_triangle[edge_u]),axis=1)
        self.triangle_edge_count=count[edge_u]
        self.extra_triangles=[(int(e),order[first[j]:first[j]+counts[j]]//3)
            for j,u in enumerate(ids) if counts[j]>2 for e in np.flatnonzero(edge_u==u)]
        self.virtual_edges=np.concatenate((np.zeros(len(self.edges),bool),np.ones(len(virtual_u),bool)))
        if len(virtual_u):
            added=unique[virtual_u]
            self.edges=np.concatenate((self.edges,added))
            self.counts=np.concatenate((self.counts,count[virtual_u]))
            self.faces0=np.concatenate((self.faces0,self.triangle_polygons[first_triangle[virtual_u]]))
            self.faces1=np.concatenate((self.faces1,self.triangle_polygons[last_triangle[virtual_u]]))
            for attr,source in (('corners0',first_corner),('corners1',last_corner)):
                support=corners[source[virtual_u]].copy()
                flip=self.loop_vertex[support[:,0]]!=added[:,0]
                support[flip]=support[flip,::-1]
                setattr(self,attr,np.concatenate((getattr(self,attr),support)))
            for attr in ('marks','smoothing','material_boundary'):
                setattr(self,attr,np.concatenate((getattr(self,attr),np.zeros(len(added),bool))))
            self.normal_dot=np.concatenate((self.normal_dot,np.ones(len(added))))
        points=self.vertices[self.triangles].astype(np.float64)
        self.triangle_normals=np.cross(points[:,1]-points[:,0],points[:,2]-points[:,0])
        self.triangle_centers=points.mean(axis=1)

    def silhouettes(self,view_matrix,perspective):
        if not len(self.triangles):return np.zeros(len(self.edges),bool)
        inv=np.linalg.inv(np.asarray(view_matrix,np.float64))
        facing=(np.einsum('ij,ij->i',self.triangle_normals,inv[:3,3]-self.triangle_centers)>=0 if perspective
                else self.triangle_normals@inv[:3,2]>=0)
        a,b=self.triangle_supports.T
        selected=(self.triangle_edge_count==2)&(facing[np.maximum(a,0)]!=facing[np.maximum(b,0)])
        for edge,triangles in self.extra_triangles:
            selected[edge]=bool(np.any(facing[triangles]) and not np.all(facing[triangles]))
        return selected

    def facing(self,view_matrix,perspective):
        inv=np.linalg.inv(np.asarray(view_matrix,dtype=np.float64))
        return (np.einsum('ij,ij->i',self.normals,inv[:3,3]-self.centers)>=0.0 if perspective
                else self.normals@inv[:3,2]>=0.0)

    def categories(self,view_matrix,perspective):
        """Return one autonomous source kind per real edge; no native credit."""
        kind=np.zeros(len(self.edges),np.int8)
        kind[self.smoothing]=3
        kind[self.counts<=1]=2
        kind[self.silhouettes(view_matrix,perspective)]=1
        kind[self.marks]=4
        return kind

    def selected_indices(self, view_matrix, perspective, settings):
        if self.holdout:return np.empty(0,np.int64)
        if not len(self.normals):
            keep = np.full(len(self.edges),settings.open_edges,dtype=np.bool_)
            if settings.marked_edges:
                keep |= self.marks
            if getattr(settings, 'wireframe_edges', False):
                keep[:self.original_edge_count] |= True
            return np.flatnonzero(keep)
        facing=self.facing(view_matrix,perspective)
        f0, f1 = facing[self.faces0], facing[self.faces1]
        manifold = self.counts == 2
        keep = np.zeros(len(self.edges), dtype=np.bool_)
        if settings.silhouette:
            keep |= self.silhouettes(view_matrix,perspective)
        if settings.open_edges:
            keep |= self.counts == 1
        if settings.marked_edges:
            keep |= self.marks
        if settings.crease_edges:
            keep |= manifold & (f0 | f1) & (self.normal_dot < np.cos(np.deg2rad(settings.crease_angle)))
        if settings.material_edges:
            keep |= self.material_boundary & (f0 | f1)
        if settings.open_edges:
            keep |= self.counts == 0
        if getattr(settings, 'wireframe_edges', False):
            keep[:self.original_edge_count] |= True
        return np.flatnonzero(keep)

    def selected_edges(self, view_matrix, perspective, settings):
        return self.vertices[self.edges[self.selected_indices(view_matrix,perspective,settings)]]


class Geometry:
    def __init__(self, context):
        job=self.capture_steps(context)
        while True:
            try:next(job)
            except StopIteration as done:
                self.__dict__.update(done.value.__dict__);break

    @classmethod
    def capture_steps(cls,context):
        result=cls.__new__(cls);result.meshes=[];result.capture_seconds=0.
        graph=context.evaluated_depsgraph_get()
        result.reference_points={obj.name:np.array(obj.evaluated_get(graph).matrix_world.translation,np.float64) for obj in context.view_layer.objects}
        objects=tuple(obj for obj in context.view_layer.objects if obj.type=='MESH'
            and obj.visible_get(view_layer=context.view_layer,viewport=context.space_data))
        names=tuple(obj.name for obj in objects)
        result.object_identity=tuple(obj.as_pointer() for obj in objects)
        offset=1
        from .selector_capture import Context
        selector_context=Context()
        from . import holdout
        use_holdout=holdout.viewport_enabled(context)
        for obj in objects:
            started=time.perf_counter()
            # No evaluated Mesh or depsgraph survives a yield. The owner checks
            # scene/layer/membership/epoch before each resume.
            row=Mesh(obj,context.evaluated_depsgraph_get(),selector_context=selector_context)
            row.holdout=bool(use_holdout and holdout.masked(obj,context.view_layer))
            row.face_offset=offset;offset+=len(row.triangles)
            if offset>=2**24:raise ValueError('Preview polygon identity exceeds exact float32 integer range')
            result.meshes.append(row);result.capture_seconds+=time.perf_counter()-started
            yield dict(stage='geometry',meshes_done=len(result.meshes),meshes_total=len(names))
        result.names=names
        result.holdout_signature=tuple(row.holdout for row in result.meshes)
        result.triangle_count=sum(len(row.triangles) for row in result.meshes)
        result.nonmanifold_count=sum(row.nonmanifold_count for row in result.meshes)
        return result

    def matches(self, context):
        from . import holdout
        objects=tuple(obj for obj in context.view_layer.objects
            if obj.type=='MESH' and obj.visible_get(view_layer=context.view_layer,viewport=context.space_data))
        return (self.names == tuple(obj.name for obj in objects)
                and getattr(self,'holdout_signature',None)==tuple(bool(holdout.viewport_enabled(context) and holdout.masked(obj,context.view_layer)) for obj in objects)
                and getattr(self,'object_identity',None)==tuple(obj.as_pointer() for obj in objects))

    def segments(self, view_matrix, perspective, settings):
        rows = [row.selected_edges(view_matrix, perspective, settings) for row in self.meshes]
        return np.concatenate(rows) if rows else np.empty((0, 2, 3), dtype=np.float32)

    def segment_records(self, view_matrix, perspective, settings):
        rows=[];supports=[]
        for row in self.meshes:
            ids=row.selected_indices(view_matrix,perspective,settings)
            rows.append(row.vertices[row.edges[ids]])
            tags=np.where(row.triangle_supports[ids]>=0,row.triangle_supports[ids]+row.face_offset,0)
            supports.append(tags.astype(np.float32))
        return ((np.concatenate(rows),np.concatenate(supports)) if rows else
                (np.empty((0,2,3),np.float32),np.empty((0,2),np.float32)))


def project_segments(segments, matrix, width, height, *, with_parameters=False,padding=0.):
    """Homogeneous clip against all six planes, including crossing the near plane."""
    if not len(segments):
        empty=np.empty((0, 2, 3), dtype=np.float32)
        return (empty,np.empty(0,np.int64),np.empty((0,2))) if with_parameters else empty
    points = np.concatenate((segments.astype(np.float64), np.ones((*segments.shape[:2], 1))), axis=2)
    clip = points @ np.asarray(matrix, dtype=np.float64).T
    start, delta = clip[:, 0], clip[:, 1] - clip[:, 0]
    lower, upper = np.zeros(len(start)), np.ones(len(start))
    alive = np.isfinite(clip).all(axis=(1, 2))
    for axis in range(3):
        for sign in (-1, 1):
            extent=1.+2.*padding/(width if axis==0 else height) if axis<2 else 1.
            a = start[:, 3]*extent + sign * start[:, axis]
            d = delta[:, 3]*extent + sign * delta[:, axis]
            parallel = d == 0.0
            alive &= ~(parallel & (a < 0.0))
            bound = np.divide(-a, d, out=np.zeros_like(a), where=~parallel)
            lower = np.where(d > 0.0, np.maximum(lower, bound), lower)
            upper = np.where(d < 0.0, np.minimum(upper, bound), upper)
    alive &= upper >= lower
    first = start[alive] + delta[alive] * lower[alive, None]
    last = start[alive] + delta[alive] * upper[alive, None]
    valid = (first[:, 3] > 0.0) & (last[:, 3] > 0.0)
    result = np.stack((first[valid, :3] / first[valid, 3, None],
                       last[valid, :3] / last[valid, 3, None]), axis=1)
    result[:, :, :2] = (result[:, :, :2] + 1.0) * np.array([width, height]) * 0.5
    result=result.astype(np.float32)
    if with_parameters:
        indices=np.flatnonzero(alive)[valid]
        return result,indices,np.stack((lower[indices],upper[indices]),axis=1)
    return result
