# SPDX-License-Identifier: GPL-3.0-or-later
"""Public brush author transactions and persistent editable author documents."""
import json
import bpy
from bpy.props import StringProperty,EnumProperty,FloatProperty,IntProperty,BoolProperty,FloatVectorProperty,PointerProperty,CollectionProperty
from . import brush_effects as E,brush_authoring,wire_ui,source_bindings

def set_effects(scene,updates):
    return wire_ui.apply_migration(scene,updates,brush_authoring.migrate)

def get_effects(scene,actual):
    from . import addon
    plan=addon._preset(scene);bindings=source_bindings.recover(plan['raw'])
    if actual not in bindings['actuals']:raise ValueError('Unknown actual brush identity')
    return E.records(plan['raw'],bindings['actuals']).get(actual,E.defaults())

def open_author(scene,actual):
    row=get_effects(scene,actual)
    text=bpy.data.texts.new('Brush Effects · '+actual[-12:])
    text['toon_brush_effect_draft']=True;text['actual_brush']=actual
    text.from_string(json.dumps(dict(actual=actual,effects=row),ensure_ascii=False,indent=2,allow_nan=False))
    text.use_fake_user=True
    scene['toon_brush_effect_draft']=text.name
    return text

def commit_author(scene,text):
    if text.library is not None or not text.get('toon_brush_effect_draft'):raise ValueError('A local owned brush author draft is required')
    payload=json.loads(text.as_string())
    if payload.get('actual')!=text['actual_brush']:raise ValueError('Draft actual identity differs')
    return set_effects(scene,[payload])

class TOONBRUSH_OT_edit(bpy.types.Operator):
    bl_idname='toon_viewport.edit_brush_effects';bl_label='笔刷细节';bl_options={'REGISTER','UNDO'}
    actual:StringProperty()
    shape:EnumProperty(name='笔尖',items=[(x,x,'') for x in ('SIMPLE','NORMAL','MULTIPLE')])
    stretch:FloatProperty(name='拉伸',default=1.,min=.01,max=64.)
    angle:FloatProperty(name='角度（度）',default=0.)
    groove:FloatProperty(name='凹槽强度',default=0.,min=0.,max=1.)
    grooves:IntProperty(name='凹槽数量',default=1,min=1,max=64)
    copies:IntProperty(name='笔触副本（含原始）',default=1,min=1,max=64)
    offset:FloatProperty(name='法向偏移（像素）',default=0.)
    def invoke(self,context,event):
        try:row=get_effects(context.scene,self.actual)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        self.shape=row['shape']['type'];self.stretch=row['shape']['stretch'];self.angle=row['shape']['angle']
        self.groove=row['shape']['groove'];self.grooves=row['shape']['groove_number']
        self.copies=row['stroke']['copies'];self.offset=row['stroke']['normal_offset']
        return context.window_manager.invoke_props_dialog(self,width=420)
    def execute(self,context):
        try:set_effects(context.scene,[dict(actual=self.actual,effects=dict(shape=dict(type=self.shape,stretch=self.stretch,angle=self.angle,
            groove=self.groove,groove_number=self.grooves),stroke=dict(copies=self.copies,normal_offset=self.offset)))])
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}

class TOONBRUSH_OT_open(bpy.types.Operator):
    bl_idname='toon_viewport.open_brush_author';bl_label='编辑完整笔刷 / 纹理 / 曲线';bl_options={'REGISTER','UNDO'}
    actual:StringProperty()
    def execute(self,context):
        try:text=open_author(context.scene,self.actual)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        area=next((a for a in context.screen.areas if a.type=='TEXT_EDITOR'),None) if context.screen else None
        if area:area.spaces.active.text=text
        self.report({'INFO'},'已创建完整笔刷作者文本；编辑后点击提交完整笔刷')
        return {'FINISHED'}

class TOONBRUSH_OT_commit(bpy.types.Operator):
    bl_idname='toon_viewport.commit_brush_author';bl_label='提交完整笔刷';bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            text=bpy.data.texts.get(context.scene.get('toon_brush_effect_draft',''))
            if text is None:raise ValueError('没有笔刷作者草稿')
            commit_author(context.scene,text)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        return {'FINISHED'}

def draw(layout, plan, gid, *, channel=None):
    rows=E.records(plan['raw']);aliases=plan['raw']['request'].get('boundary_aliases',[])
    from . import author_channels
    actuals=sorted({a['brush_id'] for a in aliases if a['group']==gid})
    if channel is not None:
        actuals=[a for a in actuals if author_channels.actual_channels(plan['raw'],gid,a)&{'BOTH',channel}]
    if not actuals:return
    box=layout.box();box.label(text='笔刷细节、纹理与距离衰减')
    for actual in actuals:
        r=box.row(align=True);author=rows.get(actual,E.defaults())
        op=r.operator('toon_viewport.full_brush_effects',text=author['shape']['type']+' · '+actual[-8:]);op.actual=actual
        op=r.operator(TOONBRUSH_OT_open.bl_idname,text='',icon='TEXT');op.actual=actual
        op=r.operator('toon_viewport.select_parameter_animation',text='',icon='KEY_HLT');op.scope='BRUSH';op.owner=actual
    box.operator(TOONBRUSH_OT_commit.bl_idname,icon='CHECKMARK')

_EDITORS={}
_LABELS=dict(type='笔尖类型',stretch='拉伸',stretch_random='拉伸随机',angle='角度（度）',angle_random='角度随机（度）',groove='凹槽强度',groove_number='凹槽数量',
    size='大小倍数',size_random='大小随机',stroke_size_random='每笔大小随机',extend='端点延伸（像素）',extend_random='延伸随机',copies='副本数（含原始）',copy_random='副本数随机',
    normal_offset='法向偏移（像素）',normal_offset_random='法向偏移随机',x_offset='X 偏移（像素）',x_offset_random='X 偏移随机',y_offset='Y 偏移（像素）',y_offset_random='Y 偏移随机',
    horizontal_space='横向副本间距',horizontal_space_random='横向间距随机',vertical_space='纵向副本间距',vertical_space_random='纵向间距随机',
    distortion_enabled='启用扭曲',distortion_amount='扭曲幅度（像素）',distortion_random='幅度随机',distortion_cycles='扭曲周期',distortion_cycles_random='周期随机',distortion_phase='相位',distortion_phase_random='相位随机',
    stroke_type='连续 / 分段',length='线段长度（像素）',length_random='长度随机',space='空隙（像素）',space_random='空隙随机',seed='随机种子',mode='颜色空间',red='红色偏移',green='绿色偏移',blue='蓝色偏移',hue='色相偏移（圈）',saturation='饱和度偏移',value='明度偏移',
    enabled='启用',amount='混合强度',source_type='纹理来源',uv_source='坐标来源',uv_selection_mode='UV 选择方式',uv_name='UV 名称',uv_index='UV 索引',object_color_selection_mode='颜色属性选择方式',object_color_name='颜色属性名',object_color_index='颜色属性索引',
    offset='坐标偏移',tiling='平铺倍数',rotation='纹理旋转（度）',wrap_mode_u='U 包裹',wrap_mode_v='V 包裹',filter_mode='纹理过滤',size_enabled='启用宽度曲线',alpha_enabled='启用透明度曲线',
    reduction_start='衰减起点',reduction_end='衰减终点',refer_object_on='使用参考对象',pixel_aspect='像素宽高比',off_screen_distance='屏幕外扩展（像素）',antialiasing='抗锯齿羽化（-1 继承）',samples_per_axis='每轴采样',weld_edges='焊接重合边端点',closed_orientation='闭环方向')
_ENUMS={
    ('shape','type'):('SIMPLE','NORMAL','MULTIPLE'),('stroke','stroke_type'):('NORMAL','RAKE','RANDOM','CONTINUOUS','STAMP'),('stroke','line_type'):('FULL','DASHED'),('color','mode'):('RGB','HSV'),
    ('raster','samples_per_axis'):('INHERIT','1','2','4'),('chain','weld_edges'):('INHERIT','TRUE','FALSE'),
    ('chain','closed_orientation'):('INHERIT','retained','clockwise','counterclockwise')}
_MAP_ENUMS=dict(source_type=('IMAGE','OBJECT_COLOR'),uv_source=('STROKE','UV','SCREEN','WORLD'),uv_selection_mode=('INDEX','NAME'),
    object_color_selection_mode=('INDEX','NAME'),wrap_mode_u=('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE'),wrap_mode_v=('REPEAT','MIRROR','CLAMP','CLIP','MIRROR_ONCE'),filter_mode=('NEAREST','LINEAR'))
_FIELDS=[]
_CURVES=[('curves','size'),('curves','alpha')]+[('reduction',ch,k,'curve') for ch in ('VISIBLE','HIDDEN') for k in ('size','alpha')]

def _key(path):return '__'.join(path)
def _get(row,path):
    for part in path:row=row[part]
    return row
def _put(row,path,value):
    for part in path[:-1]:row=row[part]
    row[path[-1]]=value

class TOONBRUSH_CurvePoint(bpy.types.PropertyGroup):
    position:FloatProperty(name='位置',min=0,max=1,precision=4)
    value:FloatProperty(name='数值',min=0,max=64,precision=4)

class TOONBRUSH_Pickers(bpy.types.PropertyGroup):
    seam_object:PointerProperty(name='接缝对象',type=bpy.types.Object)

def pickers(scene):return scene.toon_brush_effect_pickers

class TOONBRUSH_OT_curve_point(bpy.types.Operator):
    bl_idname='toon_viewport.brush_curve_point';bl_label='编辑曲线点';bl_options={'INTERNAL'}
    collection:StringProperty();index:IntProperty(default=-1)
    def execute(self,context):
        editor=_EDITORS.get(context.scene.as_pointer())
        if editor is None:return {'CANCELLED'}
        points=getattr(editor,self.collection)
        if self.index>=0:
            if len(points)<=2 or self.index in (0,len(points)-1):return {'CANCELLED'}
            points.remove(self.index)
        else:
            values=sorted((p.position,p.value) for p in points)
            index=max(range(len(values)-1),key=lambda i:values[i+1][0]-values[i][0])
            values.append(((values[index][0]+values[index+1][0])*.5,(values[index][1]+values[index+1][1])*.5));values.sort()
            points.clear()
            for x,y in values:p=points.add();p.position=x;p.value=y
        return {'FINISHED'}

class TOONBRUSH_OT_full(bpy.types.Operator):
    bl_idname='toon_viewport.full_brush_effects';bl_label='完整笔刷设置';bl_options={'REGISTER','UNDO'}
    actual:StringProperty()
    last_error:StringProperty(options={'HIDDEN'})
    section:EnumProperty(name='设置',items=[('shape','笔尖',''),('stroke','笔触',''),('color','颜色',''),('maps','纹理',''),('curves','沿线曲线',''),('reduction','距离衰减',''),('raster','光栅与采样',''),('chain','链接与闭环','')])
    map_name:EnumProperty(name='纹理用途',items=[('brush','笔尖透明度',''),('size','宽度',''),('alpha','透明度',''),('color','颜色',''),('distortion','扭曲','')])
    distance_channel:EnumProperty(name='可见性',items=[('VISIBLE','可见线',''),('HIDDEN','隐藏线','')])
    distance_kind:EnumProperty(name='衰减量',items=[('size','宽度',''),('alpha','透明度','')])
    curve_kind:EnumProperty(name='曲线',items=[('size','宽度',''),('alpha','透明度','')])
    seam_on:BoolProperty(name='指定闭环接缝')
    seam_edge:IntProperty(name='边索引',min=0)
    seam_endpoint:EnumProperty(name='端点',items=[('0','起点',''),('1','终点','')])
    def invoke(self,context,event):
        try:row=get_effects(context.scene,self.actual)
        except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
        for path in _FIELDS:
            value=_get(row,path)
            if path==('chain','weld_edges'):value='INHERIT' if value is None else ('TRUE' if value else 'FALSE')
            elif path==('chain','closed_orientation'):value=value or 'INHERIT'
            elif path==('raster','samples_per_axis'):value='INHERIT' if value is None else str(value)
            elif path==('raster','antialiasing'):value=-1. if value is None else value
            setattr(self,_key(path),value)
        for path in _CURVES:
            points=getattr(self,_key(path));points.clear()
            for x,y in _get(row,path):p=points.add();p.position=x;p.value=y
        picker=pickers(context.scene)
        for key in E.MAP_NAMES:setattr(picker,'image_'+key,None)
        for ch in ('VISIBLE','HIDDEN'):
            for kind in ('size','alpha'):setattr(picker,'reference_'+ch+'_'+kind,context.scene.objects.get(row['reduction'][ch][kind]['object_reference']))
        anchor=row['chain']['seam_anchor'];self.seam_on=anchor is not None
        picker.seam_object=context.scene.objects.get(anchor['object']) if anchor else None
        if anchor:self.seam_edge=anchor['edge'];self.seam_endpoint=str(anchor['endpoint'])
        _EDITORS[context.scene.as_pointer()]=self
        return context.window_manager.invoke_props_dialog(self,width=620)
    def cancel(self,context):_EDITORS.pop(context.scene.as_pointer(),None)
    def _draw_curve(self,layout,path):
        name=_key(path);points=getattr(self,name)
        for i,p in enumerate(points):
            line=layout.row(align=True);line.prop(p,'position');line.prop(p,'value')
            if i not in (0,len(points)-1):op=line.operator(TOONBRUSH_OT_curve_point.bl_idname,text='',icon='REMOVE');op.collection=name;op.index=i
        op=layout.operator(TOONBRUSH_OT_curve_point.bl_idname,text='添加控制点',icon='ADD');op.collection=name;op.index=-1
    def draw(self,context):
        layout=self.layout;layout.prop(self,'section',expand=True)
        if self.section=='maps':layout.prop(self,'map_name',expand=True);prefix=('maps',self.map_name)
        elif self.section=='reduction':
            layout.prop(self,'distance_channel',expand=True);layout.prop(self,'distance_kind',expand=True);prefix=('reduction',self.distance_channel,self.distance_kind)
        else:prefix=(self.section,)
        col=layout.column(align=True)
        for path in _FIELDS:
            if path[:len(prefix)]==prefix:col.prop(self,_key(path))
        if self.section=='maps':
            col.prop(pickers(context.scene),'image_'+self.map_name)
            row=get_effects(context.scene,self.actual)['maps'][self.map_name]
            if row['image'] is not None:col.label(text='已捕获图像：%d × %d；选择图像可替换'% (row['image']['width'],row['image']['height']))
        elif self.section=='curves':
            layout.prop(self,'curve_kind',expand=True);self._draw_curve(layout,('curves',self.curve_kind))
        elif self.section=='reduction':
            col.prop(pickers(context.scene),'reference_'+self.distance_channel+'_'+self.distance_kind)
            self._draw_curve(layout,prefix+('curve',))
        elif self.section=='chain':
            col.prop(self,'seam_on')
            if self.seam_on:col.prop(pickers(context.scene),'seam_object');col.prop(self,'seam_edge');col.prop(self,'seam_endpoint')
            col.label(text='链接长度、分割角、方向、最小长度与虚线使用组下方现有链控件')
    def execute(self,context):
        try:
            row=get_effects(context.scene,self.actual)
            for path in _FIELDS:
                value=getattr(self,_key(path))
                if path==('chain','weld_edges'):value=None if value=='INHERIT' else value=='TRUE'
                elif path==('chain','closed_orientation'):value=None if value=='INHERIT' else value
                elif path==('raster','samples_per_axis'):value=None if value=='INHERIT' else int(value)
                elif path==('raster','antialiasing'):value=None if value<0 else value
                elif path[-1] in ('offset','tiling','pixel_aspect'):value=list(value)
                _put(row,path,value)
            for path in _CURVES:_put(row,path,[[p.position,p.value] for p in getattr(self,_key(path))])
            for key in E.MAP_NAMES:
                image=getattr(pickers(context.scene),'image_'+key)
                if image is not None:row['maps'][key]['image']=E.snapshot_image(image)
            for ch in ('VISIBLE','HIDDEN'):
                for kind in ('size','alpha'):
                    obj=getattr(pickers(context.scene),'reference_'+ch+'_'+kind);row['reduction'][ch][kind]['object_reference']='' if obj is None else obj.name
            if self.seam_on:
                obj=pickers(context.scene).seam_object
                if obj is None:raise ValueError('请选择接缝对象')
                row['chain']['seam_anchor']=dict(object=obj.name,edge=self.seam_edge,endpoint=int(self.seam_endpoint))
            else:row['chain']['seam_anchor']=None
            set_effects(context.scene,[dict(actual=self.actual,effects=row)])
        except Exception as error:self.last_error=str(error);self.report({'ERROR'},str(error));return {'CANCELLED'}
        _EDITORS.pop(context.scene.as_pointer(),None);return {'FINISHED'}

def _declare(row,path=()):
    for key,value in row.items():
        p=path+(key,);name=_key(p)
        if p in _CURVES:TOONBRUSH_OT_full.__annotations__[name]=CollectionProperty(type=TOONBRUSH_CurvePoint);continue
        if key in ('image','object_reference','seam_anchor','seam_tokens'):continue
        if type(value) is dict:_declare(value,p);continue
        label=_LABELS.get(key,key.replace('_',' '));allowed=_ENUMS.get(p)
        if p[0]=='maps':allowed=_MAP_ENUMS.get(key)
        if allowed:prop=EnumProperty(name=label,items=[(x,x,'') for x in allowed])
        elif p==('raster','antialiasing'):prop=FloatProperty(name=label,default=-1,min=-1,max=16)
        elif type(value) is bool:prop=BoolProperty(name=label,default=value)
        elif type(value) is int:prop=IntProperty(name=label,default=value,min=0,max=2147483647)
        elif type(value) is float:prop=FloatProperty(name=label,default=value,min=-65536,max=65536,precision=4)
        elif type(value) is list:prop=FloatVectorProperty(name=label,default=value,size=len(value),precision=4)
        elif type(value) is str:prop=StringProperty(name=label,default=value)
        else:raise ValueError('Unmapped author UI field '+name)
        TOONBRUSH_OT_full.__annotations__[name]=prop;_FIELDS.append(p)
_declare(E.defaults())
for key in E.MAP_NAMES:TOONBRUSH_Pickers.__annotations__['image_'+key]=PointerProperty(name='图像',type=bpy.types.Image)
for ch in ('VISIBLE','HIDDEN'):
    for kind in ('size','alpha'):TOONBRUSH_Pickers.__annotations__['reference_'+ch+'_'+kind]=PointerProperty(name='参考对象',type=bpy.types.Object)

CLASSES=(TOONBRUSH_CurvePoint,TOONBRUSH_Pickers,TOONBRUSH_OT_curve_point,TOONBRUSH_OT_edit,TOONBRUSH_OT_open,TOONBRUSH_OT_commit,TOONBRUSH_OT_full)

def register():
    if hasattr(bpy.types.Scene,'toon_brush_effect_pickers'):raise ValueError('Brush picker state already registered')
    bpy.types.Scene.toon_brush_effect_pickers=PointerProperty(type=TOONBRUSH_Pickers)

def unregister():
    _EDITORS.clear()
    if hasattr(bpy.types.Scene,'toon_brush_effect_pickers'):del bpy.types.Scene.toon_brush_effect_pickers
