# 活动槽表面作者（独立有限切片）

`surface_slots.py` 为明确的活动对象、当前材质槽提供 EEVEE 表面候选。`surface_ui.py` 只有 PropertyGroup、操作和 `draw_contents`；没有新 Panel、handler 或调度器。母工作台的注册、折叠区和库存由母入口集成。不要把这里称为三类完整风格已经完成。

三个内置候选通过 `candidate_settings(name)` 返回新 settings：

| 名称 | 现有材质内核 | 范围 |
|---|---|---|
| CEL3 | `eevee_cel3` | 不透明三色基础填充 |
| GAME_REFLECT | `eevee_cel3_reflect` | 三色加美术总反射；非完整游戏 PBR，非能量守恒叠加 |
| INK_FLAT | `eevee_cel3` 的三色同色设置 | 墨线平涂；不生成排线、纸纹或完整漫画风格 |

它们只处理表面；二维线条仍由已有 Pencil 作者流程决定。不会调用旧 `toon.run`、创建旧三维几何描边、迁移旧状态或替换整个角色。

## 明确操作对象

必须为本地、可编辑 Mesh 的 Object Mode，并有一个已经存在且实际被原始多边形使用的活动槽。DATA 槽要求独占 Mesh；OBJECT 槽可以在共享 Mesh 上仅改此对象的覆盖，不改底层 DATA 材质。只替换这一槽，保留其它槽、link、面索引、拓扑、UV、源图和材质节点图。旧材质即使共享也只保留引用，不改其参数或消费者的槽。

活动材质若包含面部 SDF、cast、艺术法向或旧方向面影 reader／身份标记，会在替换前拒绝。检查使用真实节点树和有界嵌套组遍历（最多 66 个被访问数据块／2048 节点），不依赖组名，也不会猜测其它角色槽或自动配脸。复杂图超界时停止而不视为“未发现保护”。

新材质有独立 profile `toon-surface-slot.v1`／UUID，保存强 ID target、明确 slot/link、原有效 Material 指针及原底层 DATA 指针、真实创建后 snapshot、最新受管 baseline 和最后声明。复用现有 `materials._settings/_build/managed_snapshot/update_parameters`；不写旧材质图。

## API

```python
from toon_workflow_addon import surface_slots as S

result = S.apply(context, S.candidate_settings('CEL3'))
status = S.inspect(context)                 # valid 或 conflict，只读
result = S.update(context, complete_settings)
result = S.restore(context)
library = S.export_library(context, absolute_json_path)
result = S.apply_library(context, absolute_json_path, template_id=None)
```

`apply` 只创建新材质／core 并绑定一个槽；失败恢复原 pointer/link，清理仅限本次创建且无消费者的材质／组。已有受管槽必须先恢复，不能隐式重套模式。

`inspect` 对最新受管 baseline 做严格比较。新材质被其它槽、对象或非槽 ID 使用，或者图、相关输入、wrapper、名字／身份／metadata 被编辑，均报告 conflict；`update`／`restore` 随后拒绝，不覆盖作者修改。冲突不提供“强制”按钮。

`update` 先完整预检，再复用既有三方参数更新／回滚；模式不能变，换模式应先 `restore` 再 `apply`。旧 baseline 的三方合并实现被复用，但本入口的更严格规则不接受未声明的受管输入手改。

`restore` 精确还原记录的原槽 pointer/link，不删除原材质。恢复后暂时未消费的新受管材质保留以利审计／Undo，不做全局孤立清理。若槽 link、source Mesh 或 OBJECT 槽底层 DATA 已改变，不猜测恢复位置。

## 材质库与 Pencil 样式分开

导出使用闭合 `toon-template-library-v1`：库 UUID、revision=1、唯一条目 UUID、engine 和真实当前静态 settings。文件以 exclusive 模式新建，不覆盖已有文件。路径必须是绝对 `.json` 且父目录已存在。

导入先用 `templates.normalize_document` 验证完整旧库 schema。一个条目时可省 template_id；多个必须明确条目 UUID；只消费 EEVEE 条目，拒绝未知 schema、坏参数及其它引擎。应用仍只影响当前一个槽。

这些 JSON 不是 `toon.autonomous-style-file.v1`，不能传给母工作台的 Pencil `style_file`。不保存 slot 分配、Action、FCurve、对象路径、任意节点图或完整角色配方。

## UI 与验证边界

母入口调用 `surface_ui.register()/unregister()` 和 `draw_contents(layout,context)`。UI 显示活动对象／槽／link／材质、候选、颜色和阈值、apply/update/restore/inspect 及库路径／条目 UUID。改变槽的四个操作有原生 UNDO；inspect 和外部文件导出没有虚构 Blender Undo 能撤销文件的承诺。

本切片只针对不透明 EEVEE 受管表面，不包含透明、完整 PBR、排线／漫画、脸部重绑或全角色自动配置。实际通过的 CPU、UI lifecycle、库往返、拒绝和冷开证据以 `work/audit/<batch>//` 当次交付为准；源码可读、已注册或语法通过不等于渲染／艺术验收。

2026-11-10 本机 Blender 5.2.1 LTS / `9e2066aef7ef` / CLI 0.85.0：活动槽 CPU 脚本 44 项通过（其中 4 项为注入故障后真实 RNA 回滚、1 项为全 144 文件库存保护），独立 factory／disable-autoexec 冷开也通过，未导入产品模块。第一版测试选中不可实际静音的 Group Input 导致夹具失败；第二版改用 ShaderNodeMath 并先确认真实 mute 已改变，产品源码未因此修改。证据入口为本批 `surface-tests/SURFACE-DELIVERY.json`。随后单包 0.22.0 i002 在真实 C94 工作副本中经 apply/update 操作加入三个自建道具表面；公共作业 bc9c9f2c3399421da6846d73d624f146 的 frame3 联合图已通过父接受及冷读，8 个法向目标与面影/SDF/cast、Beauty/Line 冻结见证逐值一致。主线程实际看图确认表面可见，但方形镜头裁到右下道具；这只是不透明角色加道具的有限工程切片，完整风格和连续艺术效果仍未验。母入口摘要见本批 STAGE-VERIFICATION.json。
