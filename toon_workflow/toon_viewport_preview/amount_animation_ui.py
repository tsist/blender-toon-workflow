# SPDX-License-Identifier: GPL-3.0-or-later
"""Controlled named-path amount key authoring; the RNA target is never drawn."""
import bpy
from . import amount_animation, chain_ui, map_amount_ui


def _target(scene):
    from . import addon
    plan=addon._preset(scene)
    bindings,static=map_amount_ui.read_controls(scene,plan)
    if static is None:raise ValueError('请先载入尺寸贴图参数。')
    actual=chain_ui._selected_actual(scene,plan,bindings)
    return plan,bindings,static,actual


def _evaluate(context):
    scene=context.scene
    scene.frame_set(scene.frame_current,subframe=scene.frame_subframe)
    context.view_layer.update()
    chain_ui._redraw()


def _message(error):
    reason=str(error)
    if 'LINEAR keys' in reason:return '目前只支持线性关键帧；请改为线性插值，或停用动画恢复静态值。'
    if 'NLA' in reason or 'tweak mode' in reason or 'drivers' in reason:
        return '此场景含 NLA 或驱动，暂不支持影响量动画；可停用动画恢复静态值。'
    if 'linked' in reason or 'read-only' in reason:return '链接或只读的场景、动作无法编辑影响量动画。'
    if 'no amount key' in reason:return '当前帧没有此笔刷的影响量关键帧。'
    if 'no editable named amount curve' in reason:return '此笔刷还没有可删除的影响量关键帧，或曲线已锁定。'
    if 'locked' in reason:return '影响量曲线已锁定，请先解锁。'
    if 'indexed, orphan' in reason:return '影响量动画路径已改变；请停用动画，并恢复原笔刷的关键帧路径。'
    return reason


def _failed(operator,scene,error):
    message='影响量动画操作失败：'+_message(error)
    scene.toon_viewport_preview.amount_animation_error=message
    operator.report({'ERROR'},message)
    return {'CANCELLED'}


class _AuthorOperator:
    @classmethod
    def poll(cls,context):
        scene=context.scene
        return (scene is not None and scene.library is None and scene.is_editable
                and scene.toon_viewport_preview.style_mode=='PRESET')


class TOONVIEWPORT_OT_amount_animation_sample(_AuthorOperator,bpy.types.Operator):
    bl_idname='toon_viewport.amount_animation_sample'
    bl_label='取当前影响量'
    bl_description='把当前实际影响量复制到待写关键帧值，不改变静态配置或动画'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            from . import addon
            plan,bindings,static,actual=_target(context.scene)
            settings=context.scene.toon_viewport_preview
            settings.amount_key_value=addon._map_amounts(context.scene,plan).get(actual,1.)
            settings.amount_animation_error='';chain_ui._redraw()
            return {'FINISHED'}
        except Exception as error:return _failed(self,context.scene,error)


class TOONVIEWPORT_OT_amount_animation_key(_AuthorOperator,bpy.types.Operator):
    bl_idname='toon_viewport.amount_animation_key'
    bl_label='插入／更新关键帧'
    bl_description='按当前笔刷的固定身份写入当前帧，启用线性影响量动画'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            plan,bindings,static,actual=_target(context.scene)
            settings=context.scene.toon_viewport_preview
            amount_animation.set_key(context.scene,actual,settings.amount_key_value,bindings['actuals'])
            settings.amount_animation_error='';_evaluate(context)
            return {'FINISHED'}
        except Exception as error:return _failed(self,context.scene,error)


class TOONVIEWPORT_OT_amount_animation_remove(_AuthorOperator,bpy.types.Operator):
    bl_idname='toon_viewport.amount_animation_remove'
    bl_label='删除当前关键帧'
    bl_description='仅删除此笔刷在当前帧的影响量关键帧'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            plan,bindings,static,actual=_target(context.scene)
            amount_animation.remove_key(context.scene,actual,bindings['actuals'])
            context.scene.toon_viewport_preview.amount_animation_error='';_evaluate(context)
            return {'FINISHED'}
        except Exception as error:return _failed(self,context.scene,error)


class TOONVIEWPORT_OT_amount_animation_toggle(_AuthorOperator,bpy.types.Operator):
    bl_idname='toon_viewport.amount_animation_toggle'
    bl_label='切换影响量动画'
    bl_description='启用时绝对覆盖静态影响量；停用后恢复原静态配置，保留关键帧'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            plan,bindings,static,actual=_target(context.scene)
            item=amount_animation.track(context.scene,actual)
            amount_animation.set_enabled(context.scene,actual,not(item is not None and item.enabled),
                bindings['actuals'],initial_value=static['amounts'][actual])
            context.scene.toon_viewport_preview.amount_animation_error='';_evaluate(context)
            return {'FINISHED'}
        except Exception as error:return _failed(self,context.scene,error)


def draw(layout,scene,actual,plan,bindings,static):
    box=layout.box();box.label(text='影响量动画',icon='ACTION')
    settings=scene.toon_viewport_preview
    item=amount_animation.track(scene,actual);enabled=item is not None and item.enabled
    try:
        from . import addon
        current=addon._map_amounts(scene,plan).get(actual,1.)
        box.label(text=('当前动画值：' if enabled else '当前静态值：')+format(current,'.6g'))
        curve=amount_animation.curve(scene,actual) if item is not None else None
        frame=scene.frame_current_final
        keyed=curve is not None and any(float(key.co[0])==amount_animation._frame(scene,frame) for key in curve.keyframe_points)
        box.label(text='帧 '+format(frame,'.6g')+(' · 已有关键帧' if keyed else ''),
                  icon='KEY_HLT' if keyed else 'KEY_DEHLT')
    except Exception as error:
        box.label(text=_message(error),icon='ERROR')
    box.operator(TOONVIEWPORT_OT_amount_animation_toggle.bl_idname,
                 text='停用动画 · 恢复静态值' if enabled else '启用影响量动画',
                 icon='CHECKBOX_HLT' if enabled else 'CHECKBOX_DEHLT')
    row=box.row(align=True)
    row.prop(settings,'amount_key_value',text='待写关键帧',slider=True)
    row.operator(TOONVIEWPORT_OT_amount_animation_sample.bl_idname,text='',icon='EYEDROPPER')
    row=box.row(align=True)
    row.operator(TOONVIEWPORT_OT_amount_animation_key.bl_idname,icon='KEY_HLT')
    row.operator(TOONVIEWPORT_OT_amount_animation_remove.bl_idname,text='',icon='KEY_DEHLT')
    box.label(text='启用后覆盖静态值；关键帧之间线性过渡')
    if settings.amount_animation_error:box.label(text=settings.amount_animation_error,icon='ERROR')


CLASSES=(TOONVIEWPORT_OT_amount_animation_sample,TOONVIEWPORT_OT_amount_animation_key,
         TOONVIEWPORT_OT_amount_animation_remove,TOONVIEWPORT_OT_amount_animation_toggle)
