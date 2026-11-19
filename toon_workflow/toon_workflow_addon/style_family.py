# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose existing static CEL and complete ink in the single workbench.

The first family is CEL. Preserved native ink is an explicit local foundation,
not a portable INDEX transport. All rendering still uses the existing readers.
"""
import copy,json,uuid
from . import cel_recipe_v2 as CEL,cel_ui as CUI,line_template as LINE

SCHEMA='toon-style-family.v1'

def encode(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def normalize(value):
    if type(value) is not dict or set(value)!={'schema','family_id','name','family','recipe','line'}:
        raise ValueError('风格家族字段不完整')
    if value['schema']!=SCHEMA or value['family']!='CEL':
        raise ValueError('当前已实现的家族为 CEL；GAME/MANGA 尚需独立机制与验收')
    if type(value['family_id']) is not str or str(uuid.UUID(value['family_id']))!=value['family_id']:
        raise ValueError('风格家族需要明确UUID')
    if type(value['name']) is not str or not 1<=len(value['name'])<=128 or any(ord(x)<32 for x in value['name']):
        raise ValueError('风格名无效')
    recipe=CEL.normalize(value['recipe'])
    if recipe['lines']:
        raise ValueError('家族描边由完整模板或原作者承担；CEL的简化线角色须为空')
    line=value['line']
    if type(line) is not dict:raise ValueError('描边组件无效')
    if line.get('mode')=='PRESERVE' and set(line)=={'mode'}:
        line=dict(mode='PRESERVE')
    elif line.get('mode')=='TEMPLATE' and set(line)=={'mode','document'}:
        line=dict(mode='TEMPLATE',document=LINE.normalize(line['document']))
    else:raise ValueError('请选择保留原描边或完整模板')
    return dict(schema=SCHEMA,family_id=value['family_id'],name=value['name'],family='CEL',recipe=recipe,line=line)

def new_document(recipe,name='CEL风格家族',line_document=None):
    recipe=copy.deepcopy(recipe);recipe['lines']=[]
    line=dict(mode='PRESERVE') if line_document is None else dict(mode='TEMPLATE',document=line_document)
    return normalize(dict(schema=SCHEMA,family_id=str(uuid.uuid4()),name=name,family='CEL',recipe=recipe,line=line))

def _coverage(context,bindings):
    scene=context.scene;objects={}
    bound=set()
    for row in bindings:
        obj=row['object']
        if scene.objects.get(obj.name)!=obj or obj.type!='MESH':raise ValueError('绑定须为本场景真实Mesh')
        objects[obj.name]=obj;bound.add((obj.name,row['slot']))
    used={(obj.name,p.material_index) for obj in objects.values() for p in obj.data.polygons}
    if not used:raise ValueError('家族需要明确已用材质槽')
    missing=sorted(used-bound);extra=sorted(bound-used)
    if missing or extra:raise ValueError('家族须覆盖所绑定对象全部已用槽；缺少 '+str(missing[:12])+'；多余 '+str(extra[:12]))
    return dict(targets=sorted(objects),used_slots=[list(x) for x in sorted(used)],scope='Explicit bound objects only; not all Scene objects or unseen-character art')

def _rows_now(bindings):
    return [dict(row,material=row['object'].material_slots[row['slot']].material) for row in bindings]

def _protected(scene,bindings):
    """Use domain readers, not guessed shader names or a generic RNA clone."""
    from . import face_sdf,eye_overlays,head_normals,hair_map
    readers=[];seen=set();hair=[]
    for row in bindings:
        obj=row['object'];mat=obj.material_slots[row['slot']].material
        if mat is None or mat.as_pointer() in seen:continue
        seen.add(mat.as_pointer())
        if mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.get('toon_face_sdf_binding'):
                    info=face_sdf.inspect_reader(n.node_tree,target=obj)
                    if info['status']!='valid':raise ValueError('保留的SDF读者无效')
                    readers.append(dict(material=mat.name,reader=info,softness=float(n.inputs['Softness'].default_value),shadow=list(n.inputs['Shadow Color'].default_value)))
        if hair_map.KEY in mat:hair.append(hair_map.describe(scene,obj,row['slot']))
    return dict(sdf=readers,head=head_normals.inspect(scene),eye=eye_overlays.collect(scene),hair=hair)

def observe(context,document,bindings,*,template_bindings=None,ink_bindings=None):
    doc=normalize(document);rows=_rows_now(bindings)
    state=CUI._observation(context,doc['recipe'],rows,{})
    # The family core always composes CEL with its Pencil-preserving policy.
    state['line_policy']='PRESERVE'
    result=dict(document=doc,coverage=_coverage(context,rows),components=state,protected=_protected(context.scene,rows))
    if doc['line']['mode']=='TEMPLATE':
        from . import line_template_ui
        result['ink']=line_template_ui._persistent_ink(LINE.ink_observation(context.scene,doc['line']['document'],template_bindings or {},ink_bindings))
    return result

def preflight(context,document,bindings,*,template_bindings=None,ink_bindings=None):
    doc=normalize(document);coverage=_coverage(context,bindings)
    from . import eye_overlay_nodes
    for row in bindings:
        if eye_overlay_nodes.is_present(row['material']) and doc['recipe']['roles'][row['role']]['action']!='PRESERVE':
            raise ValueError('眼部半透明叠色层必须保留，不用家族表面替换')
    plan=CEL.preflight(context,doc['recipe'],bindings,{},line_policy='PRESERVE')
    if plan['unbound_roles']:raise ValueError('家族有未绑定的材质角色：'+str(plan['unbound_roles']))
    line=None
    if doc['line']['mode']=='TEMPLATE':
        # Replacing a Material would invalidate a template's explicit material
        # role before it is installed. Stage native surfaces first, then bind.
        if any(r['action']=='create' for r in plan['records']):
            raise ValueError('完整线模板需先建立表面并明确重绑材质角色；家族不猜新Material')
        line=LINE.inspect(context.scene,doc['line']['document'],template_bindings or {},mode='REPLACE',conflict='ERROR',ink_bindings=ink_bindings)
    else:
        CUI._pencil_state(context.scene)
    return dict(status='ready',schema=SCHEMA,family='CEL',coverage=coverage,
        surface_finish=CEL.inspect(context,doc['recipe'],bindings,{},line_policy='PRESERVE'),
        line=dict(mode=doc['line']['mode'],portable=doc['line']['mode']=='TEMPLATE',inspection=line),
        note='Readiness is not artistic acceptance or full scene coverage')

def apply(context,document,bindings,*,template_bindings=None,ink_bindings=None):
    doc=normalize(document)
    preflight(context,doc,bindings,template_bindings=template_bindings,ink_bindings=ink_bindings)
    # Establish protected-state readability before mutation as well as after.
    before_protected=_protected(context.scene,bindings)
    cel_token=None;line_token=None;captured_cel=[];captured_line=[]
    try:
        cel_result,cel_token=CEL.apply_transaction(context,doc['recipe'],bindings,{},line_policy='PRESERVE',capture=lambda result,token:captured_cel.append(token))
        if doc['line']['mode']=='TEMPLATE':
            line_token=LINE.apply(context.scene,doc['line']['document'],template_bindings or {},mode='REPLACE',conflict='ERROR',ink_bindings=ink_bindings,capture=captured_line.append)
            line_result=LINE.receipt(line_token)
        else:line_result=dict(status='retained_native',portable=False,note='Original complete Pencil including INDEX retained; not transported')
        current=_rows_now(bindings)
        after=observe(context,doc,current,template_bindings=template_bindings,ink_bindings=ink_bindings)
        if encode(after['protected'])!=encode(before_protected):raise ValueError('家族应用改变了保留的头脸/眼层/发影状态')
        result=dict(status='applied',schema=SCHEMA,family_id=doc['family_id'],family='CEL',coverage=after['coverage'],cel=cel_result,line=line_result,art_status='not_evaluated')
        encode(result);encode(after)
        return dict(status='applied',scene=context.scene,result=result,observation=after,bindings=current,cel_token=cel_token,line_token=line_token,document=doc,template_bindings=template_bindings,ink_bindings=ink_bindings)
    except BaseException as error:
        if cel_token is None and captured_cel:cel_token=captured_cel[0]
        if line_token is None and captured_line:line_token=captured_line[0]
        issues=[]
        if line_token is not None and line_token['status']=='applied':
            try:LINE.rollback(context.scene,line_token)
            except Exception as e:issues.append('line: '+str(e))
        if cel_token is not None and cel_token['status']=='applied':
            try:CEL.rollback_transaction(context,cel_token)
            except Exception as e:issues.append('surface/finish: '+str(e))
        if issues:raise RuntimeError('家族应用失败且回退不完整：'+'; '.join(issues)) from error
        raise

def receipt(token):
    if token.get('status')!='applied':raise ValueError('风格家族事务未应用')
    return copy.deepcopy(token['result'])

def rollback(context,token):
    if token.get('status')!='applied' or context.scene!=token['scene']:raise ValueError('本场景没有可回退的运行时家族事务')
    now=observe(context,token['document'],token['bindings'],template_bindings=token['template_bindings'],ink_bindings=token['ink_bindings'])
    if encode(now)!=encode(token['observation']):raise ValueError('家族应用后已有修改；保留后续作者状态')
    CEL.validate_transaction(context,token['cel_token'])
    if token['line_token'] is not None:LINE.rollback(context.scene,token['line_token'])
    CEL.rollback_transaction(context,token['cel_token']);token['status']='rolled_back'
    return dict(status='rolled_back',family_id=token['document']['family_id'])
