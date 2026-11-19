# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed packed key8/key11 overrides and source-compatible jaw transport.

The two key distances and the jaw distance retain their separately declared
author canonical frames. The guarded jaw endpoint is a compatibility margin,
not a certified signed-distance field. No temporal smoothing is introduced.
"""

ENCODING = 'LRUD+packed_key8_key11;canonical_jaw_top_source_floor;polar_angle/pi.v1'
AUTHORED_ENCODING = 'LRUD+independent_key8_key11_only;canonical_jaw_top_source_floor;polar_angle/pi.v1'
SOURCE_ENCODING = 'R=key8;G=key11;B=canonical_jaw;signed_model_distance;E=.5+.5*d/640'
DECODE_SCALE = 8.0


def top_transport():
    return dict(kind='canonical_jaw_with_top_source_margin_floor.v1',
        floor='max(Bdecoded,polar_top_sum_7-.5)',
        pair_domain='current_pair_author_canonical',
        jaw_domain='original_top_author_canonical')


def node_types(enabled, *, authored=False):
    if not enabled:
        return {}
    result = {'continuity_uv':'ShaderNodeUVMap',
              'continuity_attr_pair':'ShaderNodeAttribute',
              'continuity_split_pair':'ShaderNodeSeparateXYZ'}
    for i in range(9):
        result['continuity_texture_'+str(i)]='ShaderNodeTexImage'
        result['continuity_separate_'+str(i)]='ShaderNodeSeparateColor'
        if i != 4:
            result['continuity_uv_offset_'+str(i)]='ShaderNodeVectorMath'
    for role in ('key8','key11','jaw'):
        result.update({'continuity_'+role+'_weight_'+str(i):'ShaderNodeMath' for i in range(9)})
        result.update({'continuity_'+role+'_sum_'+str(i):'ShaderNodeMath' for i in range(8)})
        result.update({'continuity_'+role+'_'+k:'ShaderNodeMath' for k in ('twice','signed','decoded')})
    for role in ('key8','key11'):
        result.update({'continuity_'+role+'_'+k:'ShaderNodeMath' for k in ('enabled','normalized','difference','delta')})
    result.update({'continuity_'+k:'ShaderNodeMath' for k in ('floor_source','top_margin','distance_key8','distance')})
    if authored:result['continuity_authored_upper']='ShaderNodeMath'
    return result


def build(group, fields, image, source, *, enabled, authored=False):
    if not enabled:
        return
    from . import face_sdf as S, sdf_filter
    by={n.get(S._NODE):n for n in group.nodes}
    def node(key,kind,**props):
        value=group.nodes.new(kind);value[S._NODE]='continuity_'+key
        for name,item in props.items():setattr(value,name,item)
        by[value[S._NODE]]=value
        return value
    def edge(key,operation,a,b):
        value=node(key,'ShaderNodeMath',operation=operation)
        for socket,item in zip(value.inputs,(a,b)):
            if type(item) in (int,float):socket.default_value=item
            else:group.links.new(item,socket)
        return value.outputs['Value']
    uv=node('uv','ShaderNodeUVMap',uv_map=source['uv']['uv_name'])
    du,dv=source['sampling']['uv_step']
    for i,(dx,dy,_n) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        vector=uv.outputs['UV']
        if i!=4:
            offset=node('uv_offset_'+str(i),'ShaderNodeVectorMath',operation='ADD')
            group.links.new(vector,offset.inputs[0]);offset.inputs[1].default_value=(dx*du,dy*dv,0.)
            vector=offset.outputs['Vector']
        texture=node('texture_'+str(i),'ShaderNodeTexImage',image=image,
            interpolation='Linear',extension='EXTEND',projection='FLAT')
        group.links.new(vector,texture.inputs['Vector'])
        separate=node('separate_'+str(i),'ShaderNodeSeparateColor',mode='RGB')
        group.links.new(texture.outputs['Color'],separate.inputs['Color'])
    distances={}
    for role,channel in (('key8','Red'),('key11','Green'),('jaw','Blue')):
        terms=[edge(role+'_weight_'+str(i),'MULTIPLY',by['continuity_separate_'+str(i)].outputs[channel],n/16.)
               for i,(_x,_y,n) in enumerate(sdf_filter.KERNEL_WEIGHTS)]
        total=terms[0]
        for i in range(1,9):total=edge(role+'_sum_'+str(i-1),'ADD',total,terms[i])
        distances[role]=edge(role+'_decoded','MULTIPLY',
            edge(role+'_signed','SUBTRACT',edge(role+'_twice','MULTIPLY',total,2.),1.),source['decode_scale'])
    attribute=node('attr_pair','ShaderNodeAttribute',attribute_type='GEOMETRY',attribute_name=fields['direction_pair'])
    split=node('split_pair','ShaderNodeSeparateXYZ')
    group.links.new(attribute.outputs['Vector'],split.inputs['Vector'])
    if authored:
        upper=edge('authored_upper','ADD',split.outputs['X'],split.outputs['Y'])
        group.links.new(upper,by['direction_enabled_upper'].inputs[0])
    floor=edge('floor_source','SUBTRACT',by['polar_top_sum_7'].outputs['Value'],.5)
    top=edge('top_margin','MAXIMUM',distances['jaw'],floor)
    group.links.new(top,by['direction_top_part'].inputs[1])
    deltas={}
    for role,component in (('key8','X'),('key11','Y')):
        # The explicit constant is also checked: disabling a reader must never
        # leave its corrective delta active while U/V have been disabled.
        weight=edge(role+'_enabled','MULTIPLY',split.outputs[component],float(enabled))
        weight=edge(role+'_normalized','DIVIDE',weight,by['direction_denominator'].outputs['Value'])
        delta=edge(role+'_difference','SUBTRACT',distances[role],by['direction_upper_decoded'].outputs['Value'])
        deltas[role]=edge(role+'_delta','MULTIPLY',weight,delta)
    # Retain the qualified prototype's accumulation order, including float32
    # rounding: original distance, key8 delta, then key11 delta.
    total=edge('distance_key8','ADD',by['direction_distance_part'].outputs['Value'],deltas['key8'])
    total=edge('distance','ADD',total,deltas['key11'])
    group.links.new(total,by['direction_scalar'].inputs[1])


def validate(group, by, info):
    """Validate the extension and return the two explicit parent graph edges."""
    if not info['direction_correction']:
        return {}
    from . import face_sdf as S, sdf_filter
    source=info['pair_source'];uv=by['continuity_uv']
    if uv.uv_map!=source['uv']['uv_name']:raise S.FaceSDFError('Continuity pair UV differs')
    image=by['continuity_texture_4'].image
    if image is None or image in (by['left_texture'].image,by['vertical_texture_4'].image,by['direction_texture_4'].image):
        raise S.FaceSDFError('Continuity source must be a fourth actual Image ID')
    du,dv=source['sampling']['uv_step']
    for i,(dx,dy,_n) in enumerate(sdf_filter.KERNEL_WEIGHTS):
        texture=by['continuity_texture_'+str(i)]
        if texture.image!=image or texture.interpolation!='Linear' or texture.extension!='EXTEND' or texture.projection!='FLAT':
            raise S.FaceSDFError('Continuity pair sampling/Image differs')
        if i==4:S._expect_link(texture.inputs['Vector'],uv,'UV','Continuity center UV')
        else:
            offset=by['continuity_uv_offset_'+str(i)]
            if offset.operation!='ADD':raise S.FaceSDFError('Continuity UV offset differs')
            S._expect_link(offset.inputs[0],uv,'UV','Continuity UV base')
            S._expect_default(offset.inputs[1],(dx*du,dy*dv,0.),'Continuity own image texel step')
            S._expect_link(texture.inputs['Vector'],offset,'Vector','Continuity tap UV')
        separate=by['continuity_separate_'+str(i)]
        if separate.mode!='RGB':raise S.FaceSDFError('Continuity channels must be RGB')
        S._expect_link(separate.inputs['Color'],texture,'Color','Continuity tap color')
    attr=by['continuity_attr_pair'];split=by['continuity_split_pair']
    if attr.attribute_type!='GEOMETRY' or attr.attribute_name!=info['fields']['direction_pair']:
        raise S.FaceSDFError('Continuity packed vector identity differs')
    S._expect_link(split.inputs['Vector'],attr,'Vector','Continuity one vector Attribute')
    def edge(key,operation,a,b):
        node=by['continuity_'+key]
        if node.operation!=operation:raise S.FaceSDFError('Continuity operation differs: '+key)
        for i,item in enumerate((a,b)):
            if type(item) in (int,float):S._expect_default(node.inputs[i],item,'Continuity '+key)
            else:S._expect_link(node.inputs[i],by[item[0]],item[1],'Continuity '+key)
        return ('continuity_'+key,'Value')
    distances={}
    for role,channel in (('key8','Red'),('key11','Green'),('jaw','Blue')):
        terms=[edge(role+'_weight_'+str(i),'MULTIPLY',('continuity_separate_'+str(i),channel),n/16.)
               for i,(_x,_y,n) in enumerate(sdf_filter.KERNEL_WEIGHTS)]
        total=terms[0]
        for i in range(1,9):total=edge(role+'_sum_'+str(i-1),'ADD',total,terms[i])
        distances[role]=edge(role+'_decoded','MULTIPLY',
            edge(role+'_signed','SUBTRACT',edge(role+'_twice','MULTIPLY',total,2.),1.),source['decode_scale'])
    floor=edge('floor_source','SUBTRACT',('polar_top_sum_7','Value'),.5)
    top=edge('top_margin','MAXIMUM',distances['jaw'],floor)
    upper=None
    if info['profile']==S.AUTHORED_PROFILE:
        upper=edge('authored_upper','ADD',('continuity_split_pair','X'),('continuity_split_pair','Y'))
    deltas={}
    for role,component in (('key8','X'),('key11','Y')):
        weight=edge(role+'_enabled','MULTIPLY',('continuity_split_pair',component),1.)
        weight=edge(role+'_normalized','DIVIDE',weight,('direction_denominator','Value'))
        diff=edge(role+'_difference','SUBTRACT',distances[role],('direction_upper_decoded','Value'))
        deltas[role]=edge(role+'_delta','MULTIPLY',weight,diff)
    total=edge('distance_key8','ADD',('direction_distance_part','Value'),deltas['key8'])
    total=edge('distance','ADD',total,deltas['key11'])
    result=dict(top_source=top,distance_source=total)
    if upper is not None:result['upper_source']=upper
    return result
