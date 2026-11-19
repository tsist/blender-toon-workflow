# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU 2D strokes with direct triangle queries and per-layer maximum Alpha."""
import numpy as np
import gpu
from contextlib import contextmanager
from gpu_extras.batch import batch_for_shader
from .visibility import build_bvh_steps, GLSL as VISIBILITY_GLSL
from .coverage import options as coverage_options, PROFILES
from .sampling import options as sampling_options
from . import source_alpha
from . import depth_output
from . import brush_engine,brush_gpu

# Bound each GPU vertex allocation without dropping or resampling strokes.
# All batches of one sample share its maximum-Alpha arbitration buffer.
_LINE_BATCH_PRIMITIVES = 65536


def _line_batch(shader, layer, alpha_ids, matrix, first, end):
    points = np.asarray(layer['world'][first:end], np.float32)
    clipw = points @ np.asarray(matrix, np.float32)[3, :3] + float(matrix[3][3])
    worlds = np.concatenate((points, clipw[:, :, None]), axis=2)
    p = layer['projected'][first:end]
    rgb=layer.get('rgb_endpoints')
    if rgb is None:rgb=np.tile(np.asarray(layer['color'],np.float32),(len(layer['world']),2,1))
    corners = np.array([[0,-1],[1,-1],[1,1],[0,-1],[1,1],[0,1]], np.float32)
    return batch_for_shader(shader, 'TRIS', {
        'corner': np.tile(corners, (len(p), 1)),
        'first': np.repeat(p[:,0], 6, axis=0), 'last': np.repeat(p[:,1], 6, axis=0),
        'support_ids': np.repeat(layer['supports'][first:end], 6, axis=0),
        'world_first': np.repeat(worlds[:,0], 6, axis=0), 'world_last': np.repeat(worlds[:,1], 6, axis=0),
        'endpoint_width': np.repeat(layer['widths'][first:end], 6, axis=0),
        'endpoint_alpha': np.repeat(layer['alphas'][first:end], 6, axis=0),
        'source_alpha_id': np.repeat(alpha_ids[first:end], 6),
        'rgb_first':np.repeat(rgb[first:end,0],6,axis=0),
        'rgb_last':np.repeat(rgb[first:end,1],6,axis=0),
        'line_function_policy':np.repeat(layer.get('line_function_policy',np.full(len(layer['world']),-1,np.float32))[first:end],6),
        'source_preclassified':np.repeat(layer.get('preclassified',np.zeros(len(layer['world']),bool))[first:end],6).astype(np.float32),
        'brush_effect_id':np.repeat(np.arange(first,end,dtype=np.float32) if 'brush_effect_rows' in layer else np.full(end-first,-1,np.float32),6)})

def _shader(inputs, constants, vertex, fragment, interface=None, samplers=(), depth_write=False):
    info=gpu.types.GPUShaderCreateInfo()
    for index,(kind,name) in enumerate(inputs):info.vertex_in(index,kind,name)
    for kind,name in constants:info.push_constant(kind,name)
    for index,name in enumerate(samplers):info.sampler(index,'FLOAT_2D',name)
    if interface:
        stage=gpu.types.GPUStageInterfaceInfo('toon_preview_interface')
        for kind,name in interface:stage.flat(kind,name)
        info.vertex_out(stage)
    if depth_write:info.depth_write('ANY')
    info.fragment_out(0,'VEC4','fragColor')
    info.vertex_source(vertex);info.fragment_source(fragment)
    return gpu.shader.create_from_info(info)

@contextmanager
def _gpu_state(width,height):
    """Restore all modified GPU state before returning control to Blender."""
    viewport=gpu.state.viewport_get();scissor=gpu.state.scissor_get()
    blend=gpu.state.blend_get();depth=gpu.state.depth_test_get();mask=gpu.state.depth_mask_get()
    try:
        gpu.state.scissor_set(0,0,width,height)
        yield
    finally:
        gpu.state.viewport_set(*viewport);gpu.state.scissor_set(*scissor)
        gpu.state.blend_set(blend);gpu.state.depth_test_set(depth);gpu.state.depth_mask_set(mask)


class Renderer:
    def __init__(self):
        self.line_shader=_shader(
            [('VEC2','corner'),('VEC3','first'),('VEC3','last'),('VEC2','support_ids'),
             ('VEC4','world_first'),('VEC4','world_last'),('VEC2','endpoint_width'),('VEC2','endpoint_alpha'),('FLOAT','source_alpha_id'),('FLOAT','source_preclassified'),('VEC3','rgb_first'),('VEC3','rgb_last'),('FLOAT','line_function_policy'),('FLOAT','brush_effect_id')],
            [('MAT4','inverseMvp'),('VEC4','rayCamera'),('VEC4','rayForward'),('VEC4','viewport'),
             ('VEC4','strokeCoverage'),('VEC4','depthAxis'),('VEC4','depthRange')],
            '''#define size viewport.xy
            '''+brush_gpu.COMMON+'''
            void main(){
                a=first;b=last;supports=support_ids;
                wa=world_first;wb=world_last;widths=endpoint_width;alphas=endpoint_alpha;
                alphaSourceId=source_alpha_id;
                rgbA=rgb_first;rgbB=rgb_last;alphaPolicy=line_function_policy;
                preclassifiedFlag=source_preclassified;
                brushEffectId=brush_effect_id;
                vec2 d=last.xy-first.xy;float l=length(d);
                vec2 tangent=l>0.0?d/l:vec2(1.0,0.0);
                vec2 normal=vec2(-tangent.y,tangent.x);
                float padding=strokeCoverage.x>0.5?0.0:strokeCoverage.y*0.5;
                // Conservative raster box only; exact profile coverage below
                // discards the guard. Keep subpixel quads from collapsing.
                float radius=max(widths.x,widths.y)*0.5+padding+1.0;
                if(brushEffectId>=0.0){
                    vec4 shape=brushPrimitive(0),extent=brushPrimitive(1);
                    radius=radius*max(shape.x*extent.x,extent.y)*extent.z;
                    radius+=brushRecord(24).y;
                }
                vec2 p=mix(first.xy,last.xy,corner.x)+
                    tangent*(corner.x*2.0-1.0)*radius+normal*corner.y*radius;
                gl_Position=vec4(p/size*2.0-1.0,0.0,1.0);
            }''',
            '''#define size viewport.xy
            #define visibility int(viewport.z)
            #define nodeCount int(viewport.w)
            '''+VISIBILITY_GLSL+source_alpha.GLSL+brush_gpu.FRAGMENT+'''void main(){
                vec2 d=b.xy-a.xy;float l2=dot(d,d);
                vec2 samplePosition=gl_FragCoord.xy+strokeCoverage.zw;
                float t=l2>0.0?clamp(dot(samplePosition-a.xy,d)/l2,0.0,1.0):0.0;
                vec2 center=mix(a.xy,b.xy,t);
                vec2 alphaPosition=samplePosition;
                if(brushEffectId>=0.0){vec4 original=brushPrimitive(20);alphaPosition=mix(original.xy,original.zw,t);}
                float alpha=completeSourceAlpha(alphaPosition,mix(alphas.x,alphas.y,t));
                if(max(widths.x,widths.y)<=0.0 || alpha<=0.0)discard;
                // Coverage uses the linear-radius disk envelope. Alpha and
                // visibility keep their independent centerline projection t.
                float r0=widths.x*0.5,r1=widths.y*0.5,dr=r1-r0;
                float diskT=t;
                if(l2==0.0){diskT=dr>0.0?1.0:0.0;}
                else if(dr!=0.0){
                    float segmentLength=sqrt(l2),slope=abs(dr);
                    if(slope>=segmentLength){diskT=dr>0.0?1.0:0.0;}
                    else{
                        vec2 delta=samplePosition-a.xy;
                        float perpendicular=abs(delta.x*d.y-delta.y*d.x)/segmentLength;
                        float denominator=sqrt((segmentLength-slope)*(segmentLength+slope));
                        diskT=clamp((dot(delta,d)+dr*perpendicular*segmentLength/denominator)/l2,0.0,1.0);
                    }
                }
                float radius=mix(r0,r1,diskT);
                float distance=length(samplePosition-mix(a.xy,b.xy,diskT));
                float brushAttenuation=1.0;
                if(brushEffectId>=0.0){
                    distance=authoredBrushDistance(samplePosition,t,radius,brushAttenuation);
                    vec4 factors=brushPrimitive(18);alpha*=mix(factors.x,factors.y,t);
                    vec4 alphaMap=brushRecord(12),brushMapMeta=brushRecord(0);
                    if(alphaMap.x>0.5)alpha*=max(0.0,mix(1.0,brushMap(2,t,samplePosition).r,alphaMap.y));
                    if(brushMapMeta.x>0.5)alpha*=max(0.0,mix(1.0,brushMap(0,t,samplePosition).a,brushMapMeta.y));
                    alpha*=brushAttenuation;
                    if(radius<=0.0 || alpha<=0.0)discard;
                }
                float coverage;
                if(strokeCoverage.x>0.5){
                    // Outer-extent diameter: falloff stays inside the source.
                    coverage=clamp(radius-distance,0.0,1.0);
                }else if(strokeCoverage.y==0.0){
                    coverage=distance<=radius?1.0:0.0;
                }else{
                    coverage=clamp((radius+strokeCoverage.y*0.5-distance)/strokeCoverage.y,0.0,1.0);
                }
                if(coverage<=0.0)discard;
                float sourceT=(t/wb.w)/((1.0-t)/wa.w+t/wb.w);
                vec3 point=mix(wa.xyz,wb.xyz,sourceT);
                if(preclassifiedFlag<0.5 || alphaPolicy>=0.0){
                    int state=alphaPolicy<0.0 && visibility==2?1:lineState(brushEffectId>=0.0?alphaPosition:center,point,supports);
                    if(state==2)discard;
                    bool visible=state==1;
                    if((visibility==0 && !visible)||(visibility==1 && visible))discard;
                }
                alpha=clamp(alpha*coverage,0.0,1.0);
                gl_FragDepth=1.0-alpha;
                vec3 rgb=mix(rgbA,rgbB,sourceT);
                if(brushEffectId>=0.0)rgb=authoredBrushColor(rgb,t,samplePosition);
                if(depthRange.z>0.5){
                    float axial=dot(depthAxis,vec4(point,1.0));
                    float gray=clamp((depthRange.y-axial)/(depthRange.y-depthRange.x),0.0,1.0);
                    rgb=vec3(gray);
                }
                fragColor=vec4(rgb*alpha,alpha);
            }''',
            [('VEC3','a'),('VEC3','b'),('VEC2','supports'),('VEC4','wa'),('VEC4','wb'),
             ('VEC2','widths'),('VEC2','alphas'),('FLOAT','alphaSourceId'),('FLOAT','preclassifiedFlag'),('VEC3','rgbA'),('VEC3','rgbB'),('FLOAT','alphaPolicy'),('FLOAT','brushEffectId')],
            ['triangleWorld','bvhNodes','sourceAlphaReferences','sourceAlphaKnots','lineFunctionPolicies','brushEffectRecords','brushEffectPixels'],depth_write=True)
        self.display_shader=_shader([('VEC2','position')],
            [('VEC2','size'),('VEC2','origin'),('INT','background'),('VEC4','backgroundColor')],
            'void main(){gl_Position=vec4(position,0.0,1.0);}',
            '''void main(){
                vec2 uv=(gl_FragCoord.xy-origin)/size;
                vec4 c=texture(lineImage,uv);
                if(background!=0){
                    float v=background==2?1.0:
                        (mod(floor(gl_FragCoord.x/12.0)+floor(gl_FragCoord.y/12.0),2.0)<1.0?0.82:0.94);
                    vec4 bg=background==3?backgroundColor:vec4(vec3(v),1.0);
                    c=c+vec4(bg.rgb*bg.a,bg.a)*(1.0-c.a);
                }
                fragColor=c;
            }''',samplers=['lineImage'])
        self.screen=batch_for_shader(self.display_shader,'TRIS',{'position':
            [(-1,-1),(1,-1),(1,1),(-1,-1),(1,1),(-1,1)]})
        self.resolve_shader=_shader([('VEC2','position')], [('FLOAT','weight'),('INT','firstSample')],
            'void main(){gl_Position=vec4(position,0.0,1.0);}',
            '''void main(){
                ivec2 p=ivec2(gl_FragCoord.xy);
                vec4 prior=firstSample!=0?vec4(0.0):texelFetch(previousImage,p,0);
                fragColor=prior+texelFetch(sampleImage,p,0)*weight;
            }''',samplers=['previousImage','sampleImage'])
        self.filter_shader=_shader([('VEC2','position')], [('IVEC2','axis'),('INT','radius')],
            'void main(){gl_Position=vec4(position,0.0,1.0);}',
            '''void main(){
                ivec2 p=ivec2(gl_FragCoord.xy), extent=textureSize(sourceImage,0);
                vec4 sum=vec4(0.0);
                for(int k=-16;k<=16;k++){
                    if(abs(k)>radius)continue;
                    ivec2 q=p+axis*k;
                    if(any(lessThan(q,ivec2(0)))||any(greaterThanEqual(q,extent)))continue;
                    sum+=texelFetch(sourceImage,q,0)*texelFetch(kernelImage,ivec2(k+16,0),0).r;
                }
                fragColor=sum;
            }''',samplers=['sourceImage','kernelImage'])
        self.resolve_screen=batch_for_shader(self.resolve_shader,'TRIS',{'position':
            [(-1,-1),(1,-1),(1,1),(-1,-1),(1,1),(-1,1)]})
        self.filter_screen=batch_for_shader(self.filter_shader,'TRIS',{'position':
            [(-1,-1),(1,-1),(1,1),(-1,-1),(1,1),(-1,1)]})
        self.filter_sigma=None;self.kernel_image=None
        self.width=self.height=0
        self.color_format='RGBA16F'
        self.color=self.layer_color=self.layer_depth=None
        self.line_fb=self.layer_fb=None
        self.channel_color=self.channel_fb=None
        self.scratch_colors=self.scratch_fbs=None
        self.geometry_owner=None
        self.triangle_world=self.bvh_nodes=None
        self.node_count=0
        self.alpha_references_texture=self.alpha_knots_texture=None
        self.alpha_report={}

    @staticmethod
    def _alpha_texture(rows):
        width=2048;height=max(1,(len(rows)+width-1)//width)
        values=np.zeros((height*width,4),np.float32);values[:len(rows)]=rows
        texture=gpu.types.GPUTexture((width,height),format='RGBA32F',
            data=gpu.types.Buffer('FLOAT',(height,width,4),values.reshape(height,width,4)))
        texture.filter_mode(False)
        return texture

    def bind_geometry(self,geometry):
        for _ in self.bind_geometry_steps(geometry):pass

    def bind_geometry_steps(self,geometry):
        if self.geometry_owner is geometry:return
        if not hasattr(geometry,'visibility_bvh'):
            geometry.visibility_bvh=yield from build_bvh_steps(geometry.meshes)
        nodes,triangles=geometry.visibility_bvh
        self.node_count=len(nodes)//2
        def texture(rows):
            width=2048;height=max(1,(len(rows)+width-1)//width)
            values=np.zeros((height*width,4),np.float32);values[:len(rows)]=rows
            buffer=gpu.types.Buffer('FLOAT',(height,width,4),values.reshape((height,width,4)))
            result=gpu.types.GPUTexture((width,height),format='RGBA32F',data=buffer)
            result.filter_mode(False)
            return result
        self.triangle_world=texture(triangles)
        yield dict(stage='visibility-upload',table='triangles')
        self.bvh_nodes=texture(nodes)
        yield dict(stage='visibility-upload',table='nodes')
        self.geometry_owner=geometry

    def resize(self,width,height,*,high_precision=False):
        color_format='RGBA32F' if high_precision else 'RGBA16F'
        if (width,height,color_format)==(self.width,self.height,self.color_format):return
        self.line_fb=self.layer_fb=None
        self.color=gpu.types.GPUTexture((width,height),format=color_format)
        self.layer_color=gpu.types.GPUTexture((width,height),format=color_format)
        self.layer_depth=gpu.types.GPUTexture((width,height),format='DEPTH_COMPONENT32F')
        self.line_fb=gpu.types.GPUFrameBuffer(color_slots=(self.color,))
        self.layer_fb=gpu.types.GPUFrameBuffer(color_slots=(self.layer_color,),depth_slot=self.layer_depth)
        self.channel_color=gpu.types.GPUTexture((width,height),format=color_format)
        self.channel_fb=gpu.types.GPUFrameBuffer(color_slots=(self.channel_color,))
        self.scratch_colors=self.scratch_fbs=None
        self.width,self.height=width,height
        self.color_format=color_format

    def _display_texture(self,texture,width,height,background=0,origin=(0,0),background_color=(1,1,1,1)):
        shader=self.display_shader;shader.bind()
        shader.uniform_float('size',(width,height));shader.uniform_float('origin',origin)
        shader.uniform_int('background',background);shader.uniform_sampler('lineImage',texture)
        shader.uniform_float('backgroundColor',background_color)
        self.screen.draw(shader)

    def _scratch(self):
        if self.scratch_colors is None:
            self.scratch_colors=[gpu.types.GPUTexture((self.width,self.height),format='RGBA32F') for _ in range(2)]
            self.scratch_fbs=[gpu.types.GPUFrameBuffer(color_slots=(t,)) for t in self.scratch_colors]

    def _resolve_sample(self,index,samples):
        self._scratch();target=index%2
        with self.scratch_fbs[target].bind():
            gpu.state.viewport_set(0,0,self.width,self.height)
            gpu.state.blend_set('NONE');gpu.state.depth_test_set('NONE');gpu.state.depth_mask_set(False)
            shader=self.resolve_shader;shader.bind()
            shader.uniform_float('weight',1.0/(samples*samples));shader.uniform_int('firstSample',int(index==0))
            shader.uniform_sampler('previousImage',self.scratch_colors[1-target])
            shader.uniform_sampler('sampleImage',self.layer_color);self.resolve_screen.draw(shader)
        return self.scratch_colors[target]

    def _filter(self,texture,sigma):
        if sigma==0:return texture
        self._scratch()
        radius=int(np.ceil(4*sigma))
        if self.filter_sigma!=sigma:
            offsets=np.arange(-16,17);values=np.zeros((1,33,4),np.float32)
            weights=np.exp(-.5*(offsets.astype(np.float64)/sigma)**2);weights[abs(offsets)>radius]=0
            weights/=weights.sum();values[0,:,0]=weights
            self.kernel_image=gpu.types.GPUTexture((33,1),format='RGBA32F',
                data=gpu.types.Buffer('FLOAT',(1,33,4),values))
            self.filter_sigma=sigma
        source=texture
        for target,axis in enumerate(((1,0),(0,1))):
            with self.scratch_fbs[target].bind():
                gpu.state.viewport_set(0,0,self.width,self.height)
                gpu.state.blend_set('NONE');gpu.state.depth_test_set('NONE');gpu.state.depth_mask_set(False)
                shader=self.filter_shader;shader.bind()
                shader.uniform_int('axis',axis);shader.uniform_int('radius',radius)
                shader.uniform_sampler('sourceImage',source);shader.uniform_sampler('kernelImage',self.kernel_image)
                self.filter_screen.draw(shader)
            source=self.scratch_colors[target]
        return source

    def render(self,*args,**kwargs):
        for _ in self.render_steps(*args,**kwargs):pass

    def render_steps(self,geometry,matrix,projected,settings,width,height,scale,supports,*,world=None,layers=None,depth=None):
        if type(width) is not int or type(height) is not int or width<=0 or height<=0:
            raise ValueError('Positive integer viewport extent required')
        depth_axis,depth_range=depth_output.shader_parameters(depth)
        self.resize(width,height,high_precision=depth is not None)
        yield dict(stage='gpu-resize')
        yield from self.bind_geometry_steps(geometry)
        matrix64=np.asarray(matrix,np.float64);inverse64=np.linalg.inv(matrix64)
        # Screen axes give an orthographic ray independent of clip distance.
        forward=np.cross(matrix64[0,:3],matrix64[1,:3])
        forward/=np.linalg.norm(forward)
        if np.dot(forward,inverse64[:3,2])<0:forward=-forward
        perspective=bool(np.any(matrix64[3,:3]!=0))
        eye=np.zeros(3)
        if perspective:
            homogeneous_eye=inverse64[:,2]
            eye=homogeneous_eye[:3]/homogeneous_eye[3]
        if layers is None:
            if world is None:
                # Analytic fixtures only; product passes source world endpoints.
                ndc=projected.astype(np.float64).copy();ndc[:,:,:2]=ndc[:,:,:2]/[width,height]*2-1
                homogeneous=np.concatenate((ndc,np.ones((*ndc.shape[:2],1))),axis=2)
                homogeneous=homogeneous@np.linalg.inv(np.asarray(matrix,np.float64)).T
                world=homogeneous[:,:,:3]/homogeneous[:,:,3,None]
            raster={name:getattr(settings,name) for name in ('coverage_profile','feather_px',
                'samples_per_axis','post_filter_sigma') if hasattr(settings,name)}
            layers=[dict(**coverage_options(raster),**sampling_options(raster),
                projected=projected,world=world,supports=supports,
                widths=np.full((len(projected),2),settings.line_width*scale,np.float32),
                alphas=np.full((len(projected),2),settings.opacity,np.float32),
                color=settings.line_color,visibility=settings.visibility)]
        from . import holdout,line_functions
        if holdout.active(geometry) and not any('line_function_table' in layer for layer in layers):
            layers=line_functions.apply(geometry,None,layers)
        checked=[(layer,dict(**coverage_options(layer),**sampling_options(layer))) for layer in layers]
        for layer,_ in checked:
            if 'preclassified' in layer:
                flags=np.asarray(layer['preclassified'])
                if flags.shape!=(len(layer['projected']),) or flags.dtype!=np.bool_:
                    raise ValueError('Object preclassification must be a primitive-length boolean array')
        alpha_refs,alpha_knots,alpha_ids,self.alpha_report=yield from source_alpha.pack_steps(layers)
        alpha_by_layer={id(layer):ids for layer,ids in zip(layers,alpha_ids)}
        self.alpha_references_texture=self._alpha_texture(alpha_refs)
        self.alpha_knots_texture=self._alpha_texture(alpha_knots)
        if any(layer['visibility'] not in ('VISIBLE','HIDDEN','BOTH') for layer,_ in checked):
            raise ValueError('Unknown viewport visibility channel')
        if any(layer['visibility']=='BOTH' for layer,_ in checked) and any(layer['visibility']!='BOTH' for layer,_ in checked):
            raise ValueError('Combined visibility layer cannot mix with separate channels')
        yield dict(stage='gpu-upload',layers=len(checked))
        with _gpu_state(width,height):
            with self.line_fb.bind():
                gpu.state.viewport_set(0,0,width,height);self.line_fb.clear(color=(0,0,0,0))
        line_ids=list(dict.fromkeys(layer.get('line_id') for layer,_ in checked))
        for line_id,channel in ((line_id,ch) for line_id in line_ids for ch in ('HIDDEN','VISIBLE','BOTH')):
            selected=[(layer,options) for layer,options in checked if layer['visibility']==channel and layer.get('line_id')==line_id]
            if not selected:continue
            sigmas={options['post_filter_sigma'] for layer,options in selected}
            if len(sigmas)!=1:raise ValueError('Conflicting post-filter sigma within visibility channel')
            with _gpu_state(width,height):
                with self.channel_fb.bind():
                    gpu.state.viewport_set(0,0,width,height);self.channel_fb.clear(color=(0,0,0,0))
            for layer,coverage in selected:
                if not len(layer['projected']):continue
                effect_records,effect_pixels=brush_engine.pack(layer)
                effect_records_texture=self._alpha_texture(effect_records)
                effect_pixels_texture=self._alpha_texture(effect_pixels)
                function_texture=self._alpha_texture(layer.get('line_function_table',np.zeros((1,4),np.float32)))
                p=layer['projected']
                samples=coverage['samples_per_axis'];resolved=self.layer_color
                for index in range(samples*samples):
                    offset=((index%samples+.5)/samples-.5,(index//samples+.5)/samples-.5)
                    with _gpu_state(width,height):
                        with self.layer_fb.bind():
                            gpu.state.viewport_set(0,0,width,height)
                            gpu.state.depth_mask_set(True);gpu.state.depth_test_set('LESS_EQUAL')
                            gpu.state.blend_set('NONE');self.layer_fb.clear(color=(0,0,0,0),depth=1)
                    for first in range(0,len(p),_LINE_BATCH_PRIMITIVES):
                        end=min(first+_LINE_BATCH_PRIMITIVES,len(p))
                        batch=_line_batch(self.line_shader,layer,alpha_by_layer[id(layer)],matrix,first,end)
                        yield dict(stage='gpu-batch',primitives=end-first,primitives_done=end,
                                   primitives_total=len(p),channel=channel)
                        with _gpu_state(width,height):
                          with self.layer_fb.bind():
                            gpu.state.viewport_set(0,0,width,height)
                            gpu.state.depth_mask_set(True);gpu.state.depth_test_set('LESS_EQUAL')
                            gpu.state.blend_set('NONE')
                            shader=self.line_shader;shader.bind()
                            shader.uniform_float('inverseMvp',matrix.inverted())
                            shader.uniform_float('rayCamera',(*eye,float(perspective)))
                            shader.uniform_float('rayForward',(*forward,0.0))
                            shader.uniform_float('depthAxis',depth_axis)
                            shader.uniform_float('depthRange',depth_range)
                            shader.uniform_float('viewport',(width,height,{'VISIBLE':0,'HIDDEN':1,'BOTH':2}[layer['visibility']],self.node_count))
                            shader.uniform_float('strokeCoverage',(PROFILES.index(coverage['coverage_profile']),coverage['feather_px'],*offset))
                            shader.uniform_sampler('triangleWorld',self.triangle_world)
                            shader.uniform_sampler('sourceAlphaReferences',self.alpha_references_texture)
                            shader.uniform_sampler('sourceAlphaKnots',self.alpha_knots_texture)
                            shader.uniform_sampler('brushEffectRecords',effect_records_texture)
                            shader.uniform_sampler('brushEffectPixels',effect_pixels_texture)
                            shader.uniform_sampler('lineFunctionPolicies',function_texture)
                            shader.uniform_sampler('bvhNodes',self.bvh_nodes);batch.draw(shader)
                        batch=None
                    with _gpu_state(width,height):
                        if samples>1:resolved=self._resolve_sample(index,samples)
                    yield dict(stage='gpu-sample',sample=index+1,samples=samples*samples,channel=channel)
                with _gpu_state(width,height):
                    with self.channel_fb.bind():
                        gpu.state.viewport_set(0,0,width,height)
                        gpu.state.depth_mask_set(False);gpu.state.depth_test_set('NONE')
                        gpu.state.blend_set('ALPHA_PREMULT');self._display_texture(resolved,width,height)
                yield dict(stage='gpu-layer',channel=channel)
            with _gpu_state(width,height):
                filtered=self._filter(self.channel_color,next(iter(sigmas)))
                with self.line_fb.bind():
                    gpu.state.viewport_set(0,0,width,height)
                    gpu.state.depth_mask_set(False);gpu.state.depth_test_set('NONE')
                    gpu.state.blend_set('ALPHA_PREMULT');self._display_texture(filtered,width,height)
            yield dict(stage='gpu-channel',channel=channel)

    def display(self,width,height,mode,background=None):
        if self.color is None:return
        blend=gpu.state.blend_get();depth=gpu.state.depth_test_get()
        try:
            gpu.state.blend_set('ALPHA_PREMULT');gpu.state.depth_test_set('NONE')
            custom=background is not None and background.get('enabled',False)
            self._display_texture(self.color,width,height,3 if custom else {'OVERLAY':0,'LINES':1,'WHITE':2}[mode],
                                  gpu.state.viewport_get()[:2],background['rgba'] if custom else (1,1,1,1))
        finally:
            gpu.state.blend_set(blend);gpu.state.depth_test_set(depth)
