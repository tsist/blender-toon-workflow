# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional shot look in the workbench, with explicit scene-local region IDs."""
import json
import math
import struct
import bpy
from bpy.props import CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty
from . import manga_look

PROP='toon_shot_look'
_registered=False
_classes_registered=[]
_descriptor=None
_FIELDS=tuple(manga_look.DEFAULT_SETTINGS)
_LEGACY_FIELDS=frozenset(('mode','exposure','brightness','tone','paper_level','ink_level','dot_spacing_px','hatch_spacing_px','dot_angle_deg','hatch_angle_deg'))
_BOUNDS=dict(exposure=(-8.,8.),brightness=(-1.,1.),tone=(-1.,1.),paper_level=(.4,1.),ink_level=(0.,.6),
    dot_spacing_px=(2.,128.),hatch_spacing_px=(2.,128.),dot_angle_deg=(-180.,180.),hatch_angle_deg=(-180.,180.))
_TRANSACTION='toon-shot-look.transaction.v1'


def _look_properties():
    # Complete DEFAULT_SETTINGS; all legacy foreground RNA names stay intact.
    return dict(
        mode=EnumProperty(name='整镜头输出',items=[('OFF','原始合成','保留原始Combined'),('MANGA','漫画附加图','纸白、圆点、交叉排线；保留原通道')],default='OFF'),
        paper_rgb=FloatVectorProperty(name='纸色',subtype='COLOR',size=3,min=0,max=1,default=(1,1,1)),
        ink_rgb=FloatVectorProperty(name='墨色',subtype='COLOR',size=3,min=0,max=1,default=(.003,.003,.003)),
        exposure=FloatProperty(name='曝光',min=-8,max=8,default=0),
        brightness=FloatProperty(name='亮度',min=-1,max=1,default=0),
        tone=FloatProperty(name='中调浓度',min=-1,max=1,default=0),
        paper_level=FloatProperty(name='纸白阈值',min=.4,max=1,default=.88),
        ink_level=FloatProperty(name='墨黑阈值',min=0,max=.6,default=.16),
        offset_px=FloatVectorProperty(name='图案偏移（像素）',size=2,min=-1000000,max=1000000,default=(0,0)),
        dot_spacing_px=FloatProperty(name='圆点间距（像素）',min=2,max=128,default=6),
        hatch_spacing_px=FloatProperty(name='排线间距（像素）',min=2,max=128,default=6),
        dot_angle_deg=FloatProperty(name='圆点角度',min=-180,max=180,default=45),
        hatch_angle_deg=FloatProperty(name='排线角度',min=-180,max=180,default=45),
        aa_samples=IntProperty(name='网点采样（2或4）',min=2,max=4,default=4))


class TOONLOOK_BackgroundSettings(bpy.types.PropertyGroup):
    __annotations__=_look_properties()


class TOONLOOK_BackgroundObject(bpy.types.PropertyGroup):
    object: PointerProperty(name='背景网格',type=bpy.types.Object,poll=lambda self,obj:obj.type=='MESH')


class TOONLOOK_Settings(bpy.types.PropertyGroup):
    __annotations__=_look_properties()
    line_ink_strength: FloatProperty(name='原描边墨色',min=0,max=1,default=1)
    region_mode: EnumProperty(name='作用范围',items=[('UNIFORM','统一','整张图使用同一组参数'),('SEPARATE','主体 / 背景','仅明确指定的网格属于背景')],default='UNIFORM')
    background: PointerProperty(type=TOONLOOK_BackgroundSettings)
    background_objects: CollectionProperty(type=TOONLOOK_BackgroundObject)


def _json(value):
    return json.loads(json.dumps(value,allow_nan=False))


def _f32(value):
    return struct.unpack('f',struct.pack('f',value))[0]


def _read_settings(settings,*,foreground=False):
    values={}
    for key in _FIELDS:
        value=getattr(settings,key)
        if key in ('paper_rgb','ink_rgb','offset_px'):value=list(value)
        # New unset fields retain the legacy flat snapshot's exact Python defaults.
        if foreground and key not in _LEGACY_FIELDS and not settings.is_property_set(key):
            value=manga_look.DEFAULT_SETTINGS[key]
        if key in _BOUNDS:
            lo,hi=_BOUNDS[key]
            if value==_f32(lo):value=lo
            elif value==_f32(hi):value=hi
        values[key]=value
    # A legal JSON64 threshold gap can round below .15 in native float32.
    # Canonicalize only that representational boundary, within one f32 ULP.
    gap=values['paper_level']-values['ink_level']
    if 0<=.15-gap<=2.**-24:
        values['ink_level']=math.nextafter(values['paper_level']-.15,-math.inf)
    value=_json(manga_look.normalize_settings(values))
    if foreground:
        strength=float(settings.line_ink_strength)
        if not math.isfinite(strength) or not 0<=strength<=1:raise ValueError('Invalid original-line ink strength')
        value['line_ink_strength']=strength
    return value


def _object_list(scene,objects):
    if type(objects) is not list:raise ValueError('Explicit background Object list required')
    pointers=set()
    for obj in objects:
        if not isinstance(obj,bpy.types.Object):raise ValueError('Background requires actual Objects')
        try:
            pointer=obj.as_pointer()
            valid=pointer and obj.type=='MESH' and scene.objects.get(obj.name)==obj
        except ReferenceError:valid=False
        if not valid:raise ValueError('Background requires live Mesh Objects in the target scene')
        if pointer in pointers:raise ValueError('Duplicate background Object')
        pointers.add(pointer)
    return objects


def _background_objects(scene,settings):
    return _object_list(scene,[item.object for item in settings.background_objects])


def snapshot(scene):
    settings=getattr(scene,PROP,None)
    if settings is None or settings.mode=='OFF':return None
    foreground=_read_settings(settings,foreground=True)
    if settings.region_mode=='UNIFORM':return foreground
    from . import shot_regions
    background=_read_settings(settings.background)
    normalized=shot_regions.normalize(dict(foreground=foreground,background=background,background_roles=[]))
    return dict(schema=shot_regions.SCHEMA,foreground=normalized['foreground'],background=normalized['background'],
        background_objects=sorted(obj.name for obj in _background_objects(scene,settings)))


def document(scene):
    """Scene observation only: portable family JSON receives roles elsewhere."""
    value=snapshot(scene)
    if value is None:return None
    if 'schema' in value:return {key:value[key] for key in ('foreground','background','background_objects')}
    return dict(foreground=value,background=_json(manga_look.normalize_settings()),background_objects=[])


def _group_state(group,fields):
    return dict(values={key:list(getattr(group,key)) if key in ('paper_rgb','ink_rgb','offset_px') else getattr(group,key)
                        for key in fields},set_fields=[key for key in fields if group.is_property_set(key)])


def _state(scene):
    settings=getattr(scene,PROP)
    return dict(foreground=_group_state(settings,_FIELDS+('line_ink_strength','region_mode')),
        background=_group_state(settings.background,_FIELDS),objects=[item.object for item in settings.background_objects])


def _restore_group(group,state):
    for key,value in state['values'].items():
        if key in state['set_fields']:setattr(group,key,value)
        else:group.property_unset(key)


def _restore(scene,state):
    # Expired old IDs fail before any RNA is touched.
    for obj in state['objects']:
        if obj is not None:
            try:valid=bool(obj.as_pointer()) and bpy.data.objects.get(obj.name)==obj
            except ReferenceError:valid=False
            if not valid:raise ValueError('Previous background Object no longer exists')
    settings=getattr(scene,PROP)
    _restore_group(settings,state['foreground']);_restore_group(settings.background,state['background'])
    settings.background_objects.clear()
    for obj in state['objects']:settings.background_objects.add().object=obj
    if _state(scene)!=state:raise ValueError('Shot Look rollback RNA readback differs')


def _close(actual,expected):
    if isinstance(expected,dict):
        return type(actual) is dict and set(actual)==set(expected) and all(_close(actual[k],v) for k,v in expected.items())
    if isinstance(expected,(list,tuple)):
        return isinstance(actual,(list,tuple)) and len(actual)==len(expected) and all(_close(a,e) for a,e in zip(actual,expected))
    if type(expected) in (int,float):
        return type(actual) in (int,float) and math.isclose(float(actual),float(expected),rel_tol=2.e-7,abs_tol=2.e-8)
    return actual==expected


def _write_settings(group,value):
    for key in _FIELDS:setattr(group,key,value[key])


def apply_document(scene,look,background_objects):
    """Apply validated role settings using caller-resolved Object IDs atomically."""
    from . import shot_regions
    normalized=None if look is None else shot_regions.normalize(look)
    objects=[] if normalized is None else _object_list(scene,background_objects)
    if normalized is not None and bool(objects)!=bool(normalized['background_roles']):
        raise ValueError('Background Objects and explicit roles must both be empty or nonempty')
    settings=getattr(scene,PROP)
    before=_state(scene)
    try:
        if normalized is None:
            settings.mode='OFF';expected=None
        else:
            _write_settings(settings,normalized['foreground'])
            settings.line_ink_strength=normalized['foreground']['line_ink_strength']
            _write_settings(settings.background,normalized['background'])
            settings.background_objects.clear()
            for obj in objects:settings.background_objects.add().object=obj
            settings.region_mode='SEPARATE'
            expected=dict(foreground=normalized['foreground'],background=normalized['background'],
                background_objects=sorted(obj.name for obj in objects))
        after=document(scene)
        if not _close(after,expected):raise ValueError('Applied Shot Look native readback differs')
        return dict(schema=_TRANSACTION,scene=scene,before=before,after=after,
            after_state=_state(scene),status='applied')
    except Exception:
        _restore(scene,before)
        raise


def rollback_document(scene,token):
    """Only undo our native result; later author changes block rollback."""
    if (type(token) is not dict or token.get('schema')!=_TRANSACTION or token.get('scene')!=scene
            or token.get('status')!='applied'):raise ValueError('Invalid Shot Look runtime token')
    if _state(scene)!=token['after_state'] or document(scene)!=token['after']:
        raise ValueError('Shot Look changed after this transaction')
    _restore(scene,token['before']);token['status']='rolled_back'


class TOONLOOK_OT_background_add(bpy.types.Operator):
    bl_idname='toon_workflow.shot_look_background_add'
    bl_label='将活动网格添加到背景'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        obj=context.active_object
        return context.scene is not None and obj is not None and obj.type=='MESH'

    def execute(self,context):
        settings=getattr(context.scene,PROP);obj=context.active_object
        try:_object_list(context.scene,[obj])
        except ValueError as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        if any(item.object==obj for item in settings.background_objects):
            self.report({'WARNING'},'该网格已在背景列表');return {'CANCELLED'}
        settings.background_objects.add().object=obj
        return {'FINISHED'}


class TOONLOOK_OT_background_remove(bpy.types.Operator):
    bl_idname='toon_workflow.shot_look_background_remove'
    bl_label='移除背景网格'
    bl_options={'REGISTER','UNDO'}
    index: IntProperty(default=-1)

    def execute(self,context):
        collection=getattr(context.scene,PROP).background_objects
        index=self.index
        if index<0:
            active=context.active_object
            index=next((i for i,item in enumerate(collection) if active is not None and item.object==active),-1)
        if not 0<=index<len(collection):return {'CANCELLED'}
        collection.remove(index)
        return {'FINISHED'}


def _draw_settings(layout,settings,*,foreground=False):
    for key in _FIELDS:
        if key!='mode':layout.prop(settings,key)
    if foreground:layout.prop(settings,'line_ink_strength')


def draw_contents(layout,context):
    settings=getattr(context.scene,PROP)
    layout.label(text='整镜头风格附加图');layout.prop(settings,'mode')
    if settings.mode=='OFF':return
    layout.prop(settings,'region_mode')
    if settings.region_mode=='SEPARATE':
        layout.label(text='主体');_draw_settings(layout,settings,foreground=True)
        box=layout.box();box.label(text='背景');box.prop(settings.background,'mode')
        if settings.background.mode!='OFF':_draw_settings(box,settings.background)
        box.label(text='仅下列真实网格属于背景；其他对象属于主体')
        for index,item in enumerate(settings.background_objects):
            row=box.row(align=True);row.prop(item,'object',text='')
            row.operator(TOONLOOK_OT_background_remove.bl_idname,text='',icon='X').index=index
        box.operator(TOONLOOK_OT_background_add.bl_idname,icon='ADD')
        box.operator(TOONLOOK_OT_background_remove.bl_idname,text='将活动网格移出背景',icon='REMOVE')
    else:_draw_settings(layout,settings,foreground=True)
    layout.label(text='通过联合渲染输出 Look.png；原通道保留')
    layout.label(text='按亮度分层；深色涂装和阴影不作语义区分')


def register():
    global _registered,_descriptor
    if _registered:return
    if hasattr(bpy.types.Scene,PROP):raise RuntimeError('Shot Look Scene property belongs to another owner')
    try:
        for cls in (TOONLOOK_BackgroundSettings,TOONLOOK_BackgroundObject,TOONLOOK_Settings,
                    TOONLOOK_OT_background_add,TOONLOOK_OT_background_remove):
            bpy.utils.register_class(cls);_classes_registered.append(cls)
        setattr(bpy.types.Scene,PROP,PointerProperty(type=TOONLOOK_Settings))
        _descriptor=bpy.types.Scene.bl_rna.properties[PROP].as_pointer();_registered=True
    except Exception:
        unregister();raise


def unregister():
    global _registered,_descriptor
    descriptor=bpy.types.Scene.bl_rna.properties.get(PROP)
    if descriptor is not None and descriptor.as_pointer()!=_descriptor:raise RuntimeError('Shot Look property owner changed')
    if descriptor is not None:delattr(bpy.types.Scene,PROP)
    _descriptor=None
    for cls in reversed(_classes_registered):bpy.utils.unregister_class(cls)
    _classes_registered.clear();_registered=False
