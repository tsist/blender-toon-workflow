"""Toon Workflow domain package. Importing discovery never imports bpy.

SPDX-License-Identifier: GPL-3.0-or-later
The workbench is a development candidate; import remains bpy-free.
"""

__version__ = '0.52.1'

bl_info = {'name': '三渲二工作台', 'author': 'Blender AI',
           'version': (0,52,1), 'blender': (5,2,0), 'category': '3D View'}
_active=False
_owned=set()


def _components():
    import bpy
    from . import media_render_ui, shot_look_ui, style_set_ui, component_ui, line_material_ui, line_template_ui, style_family_ui, workbench, normal_ui, head_ui, face_ui, sdf_ui, surface_ui, cel_ui, finish_ui, eye_overlay_ui, hair_map_ui, hair_sdf_ui, omni_preview_ui, pencil_ui
    parts = [('workbench',workbench,workbench.register,workbench.unregister),
            ('normal_ui',normal_ui,lambda:normal_ui.register(include_panel=False),normal_ui.unregister),
            ('head_ui',head_ui,head_ui.register,head_ui.unregister),
            ('face_ui',face_ui,face_ui.register,face_ui.unregister),
            ('sdf_ui',sdf_ui,sdf_ui.register,sdf_ui.unregister),
            ('surface_ui',surface_ui,surface_ui.register,surface_ui.unregister),
            ('component_ui',component_ui,component_ui.register,component_ui.unregister),
            ('style_set_ui',style_set_ui,style_set_ui.register,style_set_ui.unregister),
            ('shot_look_ui',shot_look_ui,shot_look_ui.register,shot_look_ui.unregister),
            ('media_render_ui',media_render_ui,media_render_ui.register,media_render_ui.unregister),
            ('cel_ui',cel_ui,cel_ui.register,cel_ui.unregister),
            ('line_template_ui',line_template_ui,line_template_ui.register,line_template_ui.unregister),
            ('style_family_ui',style_family_ui,style_family_ui.register,style_family_ui.unregister),
            ('line_material_ui',line_material_ui,line_material_ui.register,line_material_ui.unregister),
            ('finish_ui',finish_ui,finish_ui.register,finish_ui.unregister),
            ('eye_overlay_ui',eye_overlay_ui,eye_overlay_ui.register,eye_overlay_ui.unregister),
            ('hair_map_ui',hair_map_ui,hair_map_ui.register,hair_map_ui.unregister),
            ('hair_sdf_ui',hair_sdf_ui,hair_sdf_ui.register,hair_sdf_ui.unregister),
            ('omni_preview_ui',omni_preview_ui,omni_preview_ui.register,omni_preview_ui.unregister),
            ('pencil_ui',pencil_ui,pencil_ui.install,pencil_ui.uninstall)]
    if bpy.app.version[:2] == (5, 3):
        from . import lighting_53_ui
        parts.append(('lighting_53_ui', lighting_53_ui, lighting_53_ui.register, lighting_53_ui.unregister))
    return parts


def _live(name,module):
    if name=='pencil_ui':return bool(module._polls)
    if (module._registered or module._classes_registered or module._descriptor is not None):return True
    if name=='workbench':
        from . import joint_ownership
        state=joint_ownership._state
        return state is not None and module._PENCIL_LEASE in state.owners
    return False


def register():
    global _active,_owned
    if _active:return
    if _owned:raise RuntimeError('上次三渲二清理未完成，请先处理所有权冲突')
    components=_components()
    before={name:_live(name,module) for name,module,_,_ in components}
    attempted=[]
    try:
        for name,module,start,stop in components:
            attempted.append((name,module,stop));start()
    except Exception as error:
        failures=[]
        for name,module,stop in reversed(attempted):
            if before[name] or not _live(name,module):continue
            try:stop()
            except Exception as cleanup:
                _owned.add(name);failures.append(name+': '+str(cleanup))
        if failures:raise RuntimeError('三渲二注册失败；部分本次数据未清理：'+'; '.join(failures)) from error
        raise
    _owned={name for name in before if not before[name]}
    _active=True


def unregister():
    global _active
    failures=[]
    for name,module,start,stop in reversed(_components()):
        if name not in _owned:continue
        try:
            stop();_owned.discard(name)
        except Exception as error:failures.append(name+': '+str(error))
    _active=False
    if failures:raise RuntimeError('三渲二部分清理存在所有权冲突：'+'; '.join(failures))
