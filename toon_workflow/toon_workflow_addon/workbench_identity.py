# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI waiting-period guards; no locks on permitted geometry edits."""
import os
import bpy
from .backend import pencil
from .workflow_contract import fingerprint


def identity(scene):
    refs=[]
    seen=set()
    for obj in scene.objects:
        for block in (obj,getattr(obj,'data',None),*(s.material for s in obj.material_slots)):
            if block is None or block.session_uid in seen:continue
            seen.add(block.session_uid)
            refs.append(dict(kind=block.bl_rna.identifier,name=block.name_full,uid=block.session_uid,users=block.users))
    settings=scene.toon_viewport_preview
    for block in (settings.preset_text,settings.curve_tree):
        if block is not None:
            refs.append(dict(kind=block.bl_rna.identifier,name=block.name_full,uid=block.session_uid,users=block.users))
    bindings={str(obj.session_uid):dict(data=getattr(obj.data,'session_uid',None) if hasattr(obj,'data') else None,
        slots=[dict(link=s.link,material=s.material.session_uid if s.material else None) for s in obj.material_slots])
        for obj in scene.objects}
    return dict(pid=os.getpid(),scene_uid=scene.session_uid,references=refs,bindings=bindings)


def check_owner(scene,record,*,receive=False):
    settings=scene.toon_workbench
    duplicates=[s.name for s in bpy.data.scenes if s!=scene and s.toon_workbench.owner_token==settings.owner_token]
    if duplicates:raise ValueError('提交所有权被复制到多个场景；请从目标场景重新保存快照')
    saved=record.get('gui_identity')
    if saved is None:
        if receive:raise ValueError('旧提交没有GUI身份保护；请重新保存快照制作候选')
        return
    if saved['pid']==os.getpid():
        if scene.session_uid!=saved['scene_uid']:raise ValueError('当前场景不是本提交的原始GUI目标')
    elif receive:
        raise ValueError('GUI会话已改变，无法证明原引用身份；可检查或恢复原作业，接收需从当前场景制作新候选')
    if receive:
        current={r['uid']:r for r in identity(scene)['references']}
        if any(current.get(r['uid'])!=r for r in saved['references']):
            raise ValueError('等待期间对象、材质、作者引用或共享关系已改变；未覆盖当前编辑')
        bindings=identity(scene)['bindings']
        if any(bindings.get(uid)!=value for uid,value in saved.get('bindings',{}).items()):
            raise ValueError('等待期间对象网格或材质槽绑定已改变；未覆盖当前编辑')


def _scalar_rna(value):
    result={}
    for prop in value.bl_rna.properties:
        if prop.identifier=='rna_type' or prop.type not in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:continue
        item=getattr(value,prop.identifier)
        result[prop.identifier]=list(item) if getattr(prop,'is_array',False) else item
    return result


def animation_signature(scene):
    """Read the actual bound Scene actions/drivers and incoming author paths."""
    ad=scene.animation_data
    actions=[]
    if ad:
        if ad.action:actions.append(ad.action)
        for track in ad.nla_tracks:
            for strip in track.strips:
                if strip.action:actions.append(strip.action)
    rows=[]
    for action in sorted(set(actions),key=lambda a:a.name_full):
        curves=[]
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip,'channelbags',[]):
                    for fc in bag.fcurves:
                        curves.append(dict(slot=bag.slot_handle,curve=_scalar_rna(fc),
                            points=[_scalar_rna(p) for p in fc.keyframe_points],
                            samples=[_scalar_rna(p) for p in fc.sampled_points],
                            modifiers=[_scalar_rna(m) for m in fc.modifiers]))
        rows.append(dict(name=action.name_full,uid=action.session_uid,curves=curves))
    drivers=[]
    if ad:
        for fc in ad.drivers:
            drivers.append(dict(curve=_scalar_rna(fc),driver=_scalar_rna(fc.driver),
                variables=[dict(value=_scalar_rna(v),targets=[dict(value=_scalar_rna(t),
                    id=t.id.session_uid if t.id else None) for t in v.targets]) for v in fc.driver.variables]))
    return fingerprint(dict(paths=sorted(pencil('group_ui')._animation_paths(scene)),actions=rows,drivers=drivers,
        binding=dict(value=_scalar_rna(ad),action=ad.action.session_uid if ad.action else None,
            slot=ad.action_slot.handle if ad.action_slot else None,
            nla=[dict(value=_scalar_rna(t),strips=[dict(value=_scalar_rna(s),
                action=s.action.session_uid if s.action else None) for s in t.strips]) for t in ad.nla_tracks]) if ad else None))
