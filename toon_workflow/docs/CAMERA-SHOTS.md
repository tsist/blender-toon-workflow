# 0.14逐镜头修形方案：操作与有限证据

2026-11-09。母包0.14.0在现有独立镜头修形上增加 `toon-camera-shots.v1` 方案管理。它按真实相机Object选择相机、深度参考和强度，不创建新修形器，不修改已有权重或约束，也不是商业Flattice等价实现。完整美术、全动画及P5-V阶段仍未结项，旧Flattice Demo继续归档。

## 使用前提

- 使用当前已验Blender5.2.1／`9e2066aef7ef`与本地可编辑Scene。每个target必须是Scene内不同的本地Mesh，已有且仅有一个受管独立GN修形器，各自保留独立owner及modifier身份。
- 每个target已有对应 `FLOAT`、`POINT` 权重属性，所有值有限且在[0,1]；方案记录属性名，不重建、覆盖或重新涂权重。共享/损坏受管节点、改变owner或modifier UID、删除对象或权重均会明确拒绝。
- 每个方案使用Scene内真实的PERSP相机Object和有效深度参考Object。参考需在相机前，变换、参考深度和目标权重通过现有修形预检。镜头方案不扩大底层的反馈、矩阵或渲染准入范围。
- 最多32个方案、每方案1–256个targets；同一相机只能有一个方案。所有镜头必须绑定完全相同的target owner集合及相同Object/modifier身份；只换相机、参考和强度，不把各镜头定义为不同目标子集。
- shot管理与受管modifier的camera/reference/weight/strength原生FCurve或driver互斥；apply遇到这些参数的原生动画会拒绝，不替用户删动画。方案metadata本身也不接受Action、NLA或driver。相机/角色的正常动画仍由原系统求值。

具体约束见 [camera_shots.py](../../tools/toon_workflow_addon/camera_shots.py) 和 [camera_correction.py](../../tools/toon_workflow_addon/camera_correction.py)。方案默认强度0；目标全零权重同样不会产生修形。透视相机空间的核心为 `x'=x×[1+s×w×(d/dref−1)]`、y同式、z不变；保Z不证明体积、法线、阴影或美术收益。

## 在工作台定义和预览

1. 在工作副本中先为每个目标建立并检查独立修形GN与权重。选择全部要受方案管理的已建修形Mesh；避免把未建修形的Mesh一起选入。方案保存按钮处理所有选中Mesh，不只处理活动对象。
2. 在3D View侧栏“三渲二”中的“逐镜头修形方案”，选择“修形相机”“深度参考”和“镜头方案强度”。保持默认0，或填写已明确要试验的[0,1]强度。
3. 点击“保存所选网格的镜头方案”。首次创建不勾“更新已有镜头方案”；已有同相机方案时，只有显式勾选更新才替换。定义/替换仅保存metadata，尚未改动目标modifier参数；replace保留该镜头的Collection与shot UUID。
4. 为另一相机定义方案时保持同一套选中targets。切换时间或marker后，确认Scene的当前真实相机，再点“预览当前镜头方案”。不是只改变3D视口观察方向；apply匹配的是 `scene.camera` Object。
5. 面板列出各镜头已存强度。调整后再次显式预览；不会安装逐帧handler/timer来悄悄应用。若已有方案但当前相机没有唯一匹配项，apply拒绝；先补齐该相机方案或切回已声明镜头。
6. 将方案保存在新的作者blend中。准备联合快照前先指定已经存在的绝对输出目录；缺失目录被工作台正确拒绝，不能据此绕过验证。

实际按钮是 `toon_workbench.define_shot` 与 `toon_workbench.apply_shot`，实现见 [workbench.py](../../tools/toon_workflow_addon/workbench.py)。定义不会创建权重、复制对象、改变现有约束或自动替换动画。

## Python与公共CLI

在已有Blender上下文中使用明确的对象引用：

```python
from toon_workflow_addon import camera_shots

# scene、camera、reference和targets均为已核对的真实Blender ID。
created = camera_shots.define(scene, camera, reference, targets,
    strength=0.0, replace=False)
registry = camera_shots.inspect(scene)   # 结构检查，不应用修形
receipt = camera_shots.apply(scene)      # 使用scene.camera，事务配置所有targets
```

实际签名为 `define(scene, camera, reference, targets, *, strength=0.0, replace=False)`、`inspect(scene)`、`apply(scene)`。未定义方案时inspect/apply返回None，旧无shot作者源继续有效；有registry但没有当前相机的唯一方案则拒绝。apply在首个参数写入前完成全目标预检，失败时尝试恢复该次实例参数；回滚失败会显式报错，不声称所有异常均可恢复。

方案以 `Scene['_toon_camera_shots']` 对未链接Collection根的ID引用保存，子层保存shot/binding Collections及真实相机、参考、target Object引用；不通过名称猜绑定，不把Objects链接进这些metadata Collections。保持它们未挂入场景集合、不共享、不移动、不随意添加fake user或成员；计数、UUID与membership清单用于发现丢失/新增绑定。无需额外安装管理handler。

CLI仍使用 [toon-workflow的run/inspect/edit/recover四操作](JOINT-DEVELOPMENT.md)，没有新增独立camera-shot CLI命令。先在作者blend保存方案，再以该源生成公共请求；当次describe schema、资源和实现pin仍须匹配0.14代码，不沿用旧代码SHA。静态作者样式JSON不能替代保存包含相机方案和原生动画的blend。

后台在每个真实时刻/marker相机求值后、冻结RENDER几何之前调用 `camera_shots.apply(scene)`，见 [joint_render.py](../../tools/toon_workflow_addon/joint_render.py)。每帧 `joint-output/report.json` 的 `camera_shot`记录匹配镜头、target身份、参考与强度；无方案时为None。

`requested_strength`是方案作者值，`strength`才是实际modifier RNA求值强度，另有 `strength_precision='native modifier RNA'`。例如本批0.15的实际RNA为0.15000000596046448。核对几何oracle时使用实际有效值，不把十进制作者值当成float32完全相同的数据；两者差别不应靠放宽几何门掩盖。

## C94诊断角色与动态参考边界

本批起点为用户指定C94原始基线，SHA-256 `7b8c1bf154d73868f39285d3d3102ebf029f7493467ad69aeadf665d7c920113`，来源见 [CHARACTER-BUILD.json](../../work/audit/<batch>/)。在独立工作副本中建立新的不透明诊断外观，保留原nativeRig、Mesh/Keys及原Action，再增加头部、闭眼和笑口控制测试。这个外观不代表原Glass材质已获光学资格。

本例6个修形target为躯干、左右瞳孔、上下睫毛和虎牙；前发/后发明确不修形。虎牙Child Of使用 `Study Uncorrected Deformation Reference`：该对象共享Mesh与Keys，沿用原nativeRig动态变形，但自身没有修形，用于切断对已修形几何的反馈。此安排是本例有证据的动态参考，不能推广旧静态proxy为任意角色的正确办法，也不应把Child Of重新指回已修形body。

六个采样时刻为 `[1, 2, 2.5, 3, 4.25, 5]`，真实marker分别选择 `Shot A Front`（请求0.15）和 `Shot B Three Quarter`（请求0.35）。[CHARACTER-VALIDATION.json](../../work/audit/<batch>/)包含36个target-time的VIEWPORT原生几何/标量oracle，最大误差5.170249656530346e-7m；各采样动态参考与未修形body误差0、虎牙matrix修形前后增量0、头发不变。它是采样几何证据，不等于连续接触、全动画、全rig或美术验收。原生骨架继承的3条无效driver警告保留，本任务未修理或重新资格化整个rig。

最终[可编辑角色](../../work/audit/<batch>/)另以强ID检查点保留原FK demo Action，隐藏不可渲染参考；[独立重开](../../work/audit/<batch>/)确认原网格/Key坐标/顶点权重/静置骨架exact。原渲染作业仍绑定各自的study源SHA，不改签这个最终文件。

## 真实运行与GUI证据分开阅读

| 证据 | 当前能够证明的范围 |
|---|---|
| [公共6帧RENDER结果6201892187e0426aade849ea6bae8f34](../../work/audit/<batch>/) | `toon-workflow.run`成功，candidate_accepted=true、parent_output_validation=passed；与上述VIEWPORT oracle独立计证据。 |
| [该作业完成清单](../../work/audit/<batch>/) | 真实六时刻序列已完成；代表帧的camera_shot可在各joint-output/report.json回读。 |
| [baseline三帧状态0c52dd0be323456a8689efe1cc2af267](../../work/audit/<batch>/) | 已成功且父接受，关闭方案1/3/5三时刻同条件对照完成；实际比较图见本批comparison-three-quarter.png，技术通过不等于完整艺术批准。 |
| [gui-v2/READY.json](../../work/audit/<batch>/) 与 [GUI-ERROR.json](../../work/audit/<batch>/) | 实际CUA点击预览成功、15次真实UILayout draw；后续harness漏建必需输出目录，_prepare正确拒绝，因此该GUI流水没有全通过。 |
| [GUI-POSTCHECK.json](../../work/audit/<batch>/) | 独立后台重开，真实define/apply operators均FINISHED，补齐输出目录后_prepare生成快照；这是后台补验，不冒充前一GUI流水的完整成功。 |

本任务只据源码与已存在报告编写有限操作说明，没有重新启动Blender/GPU。最终索引与后续边界由本批DELIVERY记录；本次未签署完整P5-V退出。完整美术、全动画、连续穿插/接触、头发修形、复杂光学和母产品资格继续待验。
