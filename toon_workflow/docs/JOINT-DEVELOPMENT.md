# 三渲二维领域入口

当前二维默认使用manifest.v2 / sequence.v2与blender-frame-set-f32-total.v1原生时钟，保留requested/effective时间；旧v1按原身份冷读，不自动迁移，state容器仍v1。以当次describe的实际版本、Schema和实现库存为准，不从历史段落复制pin。

已验0.21专用SDF、整头法向、镜头修形与Pencil均在单一toon_workflow包。面部数据和限制见[专用SDF](FACE-SDF-TEXTURES.md)，0.21有界资格见[交付](../../work/audit/<batch>/)。0.22活动槽表面作者切片沿用当前二维run，操作与模板格式见[活动槽表面](SURFACE-SLOTS.md)，真实资格见[本轮记录](../../work/audit/<batch>/)；不以候选名称代签完整三类艺术风格。

Blender5.2.1LTS /9e2066aef7ef是本机基线。R3的0.13退出、0.14镜头、0.15法向及后续各批按自己的包/源/资格保留。原portable偏好缺精确before，不声称恢复；隔离CONFIG必须先建目录且无需save prefs。

## 入口、身份与兼容边界

使用工程根 `./blenderctl.ps1` 的toon-workflow namespace。0.13建立的[显式薄overlay](../../tools/toon_workflow_cli_v1/)继续由根wrapper路由，保留公共进程树监督、取消、超时、FileGuard和父接受。本轮项目默认CLI已读到0.85.0，Toon overlay保留0.84基础身份；本轮没有修改共享CLI核心。

旧 `toon` 是冻结几何流程的原合同，`toon-image` 及其它既有新namespace保持各自入口。不得把它们的manifest、params、state或receipt交叉使用。旧0.10有限正式资格、0.11与0.12开发候选证据保留原版本、pin和源资格；[0.11交付](../../work/audit/<batch>/)、[0.12交付](../../work/audit/<batch>/)不因新域而改签。旧几何描边不恢复开发。

执行闭包来自当次describe的固定inventory，用户资源仍限100项，二者分开核验，不通过修改资源上限容纳代码。历史128/138/142等数量各绑定原版本，不能当当前常量。实现变化应从当次describe重新取inventory并建立新基线。新域明确拒绝旧几何manifest/state/carriers，不删除、不自动迁移；grid/report v1、v2的有界冷读继续保留。

## 发现与请求

先核对当次describe与具体operation schema；describe不启动Blender：

```powershell
& './blenderctl.ps1' toon-workflow describe
& './blenderctl.ps1' toon-workflow describe --operation run --schema
& './blenderctl.ps1' toon-workflow describe --operation inspect --schema
& './blenderctl.ps1' toon-workflow describe --operation edit --schema
& './blenderctl.ps1' toon-workflow describe --operation recover --schema
```

纯Python helper的实际签名位于 [workflow_contract.py](../../tools/toon_workflow_addon/workflow_contract.py)：

```python
from toon_workflow_addon.workflow_contract import request
full_request = request(source, scene, view_layer, times,
    resources=resources, samples_per_axis=2, lighting_samples=64,
    memory_budget_bytes=512*1024*1024)
# source是已保存工程路径；scene/view_layer是明确名称；times是原始有序数字数组。
# resources为已核对依赖路径列表，确实无依赖时才显式传[]。
# full_request是schema_version/command/params包装；--params只接full_request['params']。
```

完整请求可用 `request` 提交，参数对象可用领域命令提交；二选一，不把full wrapper写入params文件。以下路径占位符须替换为本批绝对路径，全局参数放子命令之前：

```powershell
& './blenderctl.ps1' --jobs-dir '<绝对jobs目录>' --timeout 600 --async request '<绝对full-request.json>'
& './blenderctl.ps1' --jobs-dir '<绝对jobs目录>' --timeout 600 --async toon-workflow run --params '<绝对run-params.json>'
```

| 操作 | params与接受边界 |
|---|---|
| `run` | `file/expected_sha256/resources/implementation/manifest`；新默认manifest为 `toon-workflow-manifest.v2`（含明确time_policy），明确Scene、ViewLayer、times、空间/光照采样及图像预算。 |
| `inspect`源 | 与run相同形状的参数；产出 `toon-author.json`，其中 `author_sha256` 是后续edit的作者基线。 |
| `inspect`结果 | 只给 `state: {file, expected_sha256}` 和当前 `implementation`；state须为本域 `toon-workflow-state.v1`。 |
| `edit` | run参数加 `style: {file, expected_sha256}` 与 `expected_author_sha256`；后者来自匹配源inspect的toon-author.json，不是该JSON文件自身的SHA。 |
| `recover` | 只给 `recovery: {file, expected_sha256}` 和当前 `implementation`；recovery指向原job的 `toon-workflow-request.json`，不是state、普通full-request或新造manifest。 |

四个操作的命令均为 `toon-workflow <操作> --params '<绝对参数JSON>'`。文件描述符使用绝对路径和64个小写十六进制字符的SHA-256；实际字段、限制与版本始终以describe schema及当前contract校验为准。

## 作者编辑、GUI与来源保护

工作台在“三渲二”侧栏保留基础控件，折叠委托稳定0.2.27原生PRESET、作者管理、Line层级、参数动画、元素与独立线图输出面板；不复制一套作者UI。独立线图按钮不输出联合Beauty/Combined。母与独立Pencil公开package注册采用母lease及布尔external owner；两种启停顺序保留仍在使用的底层，foreign partial ABI明确拒绝，只清理自身handler/classes/timer。私有addon生命周期入口不在此承诺内。

保存快照前显式同步pending曲线，非法曲线或curve_error拒绝保存。`is_dirty`只是状态提示：脚本Text写入不一定置位，不能据此认定没有后续作者修改；实际GUI原生模式/变换的dirtyTrue另有证据。公共作业读取磁盘快照，不自动追赶GUI编辑。

edit在新job保存候选，不覆写源。父进程对候选启动受同一公共监督的子inspect，独立冷开后才接受。receive按捕获作者指纹检查冲突，后续作者改变则拒绝接收；后续几何与备注保留，不用旧候选覆盖它们。可移植样式只处理静态document/recipe与合法scene_controls；Action、FCurve、作者图等完整状态留在blend。新增actual后，chain/map amount/dash控制表必须按新bindings用原生merge补齐并normalize，不能把旧完整actual表当新explicit全集；旧actual值及动画target身份须保留。

## 渲染、输出与恢复

新 `eevee_joint_grid.v3`／`toon-joint-report.v3`完整消费PRESET、COLOR/DEPTH元素，记录 `author_style` 和参数动画求值回执；新默认序列profile为 `eevee_joint_sequence.v2`；历史v1只按其原时钟/身份处理。共同外层网格每轴1/2/4/8采样，接管layer AA；feather与postfilter保留。固定空间位置原生光照采样默认64、整数1..256，`filter_size=0`。这不是原生完整AA、DOF或MB等价证明。

支持本地不透明Mesh、PERSP/ORTHO，最长边2048；当前最多96个明确时刻。输入顺序保留；新默认按[原生时间政策](NATIVE-TIME.md)记录请求和Blender实际effective时刻，setter与observed表示不同。越界和effective碰撞预拒，不能用epsilon合并时间或套用历史v1的floor-only规则。每帧从同一源求值实际RENDER网格、相机、灯光与作者动画。链接/Override、完整实例、非Mesh表面、光学透明/水烟/体积、DOF/MB未获得该profile资格。

每帧的 `Combined.exr/Combined.png`、`Beauty.exr`、`Line.exr`、`BeautyLineInteraction.exr` 及 `correlated-layers.npz/report.json` 保留身份和SHA。关联层为预乘scene-linear Rec.709，重建关系为 `Combined = Line + Beauty - BeautyLineInteraction`；普通resolved Alpha Over不能代替相关重建。序列位于 `sequence-output/frame-%03d/joint-output/`，按输入序号编号，完整帧有marker，总完成须有 `complete.json`。公共PNG只代表最后请求时刻，作者blend为首请求时刻检查点；交付保留整个job、原源与依赖闭包。

run/recover形成 `toon-workflow-state.v1`，父接受及独立冷读共用完整state语义校验：source、manifest、资源、历史实现、作者指纹及recovery request必须与实际序列一致，输出清单必须闭合。历史只读检查使用该state自己的实现身份；首批缺省空资源记录仅在只读且资源为空时兼容，新run/recover必须显式记录。旧几何Collection._toon_state和Scene/carriers均拒绝且保留原数据。检查公共终态、`candidate_accepted=true`、`parent_output_validation=passed`、父接受回执及完整输出清单；worker passed、文件存在或单帧marker都不足以代替接受。edit还需子inspect冷开候选后的父接受。

recover在新job工作，来源、资源描述符、manifest、实现pin必须与原持久请求相同，只复用逐项验证完成的连续前缀，剩余帧重新渲染。不修改旧job，不复用未完成帧，不接受改资源后混帧；源或实现变化应建立新run。它不是任意中帧断点恢复，也不使原取消job变为成功。

镜头修形保持独立美术域和默认强度0。逐帧检查实际RENDER可见且show_render开启的修形；非零强度要求修形相机等于当前渲染相机，合法零强度允许不同相机，不自动修绑。保Z不证明阴影或美术收益；旧Flattice Demo保持归档，不宣称商业实现等价。

## 本批真实证据与剩余签署

审计根为 `work/audit/<batch>//`。以下均为对应夹具实证，不外推为任意资产或完整产品资格：

| 证据 | 已验证范围 |
|---|---|
| [复杂公共作业85aa7cd0bb9e465caed1dc5bad270175](../../work/audit/<batch>/) | 十时刻、5个Mesh、Area灯、image brush、2条参数轨、0.35px/frame慢移动及COLOR/DEPTH元素。 |
| [STANDARD-COMPARISON.json](../../work/audit/<batch>/) | 单空间位置稳定0.2.27标准入口与joint的Combined/Color/Depth三EXR pass，通道头及69个ZIP压缩像素块exact；没有独立float解码，不把该比较称作独立像素解码验收。 |
| [AUTHOR-ROUNDTRIP-v2.json](../../work/audit/<batch>/) | 真实CLI edit得到两组两Line，候选独立冷开；GUI receive拒绝后续作者冲突且保留后续几何/备注，保存/重注册/冷开通过。 |
| [两Line三时刻公共作业c4662623e2814e1591faa713782fcd2c](../../work/audit/<batch>/) | 编辑后的双Line在真实三时刻run成功；默认色宽不构成美术优化证明。 |
| [RECOVERY-VALIDATION.json](../../work/audit/<batch>/) | 实际取消旧job `fca230356f3f451e83dedc0938f7e369`；新job `59800a1461f149bebdcfefe28fa76863`复用1帧、另2帧重渲，旧prefix字节未变。 |
| [OWNER-RUNTIME-v2.json](../../work/audit/<batch>/) | 10项native owner/双顺序/幂等清理与foreign partial ABI拒绝，保护foreign descriptor。 |
| [admission-v2/PRE-FLIGHT.json](../../work/audit/<batch>/) | 实际invalid曲线在快照前拒绝，合法pending曲线保存；不将脚本Text写入的dirty提示当修改完整见证。 |
| [gui-v2/GUI-REPORT.json](../../work/audit/<batch>/) | actualUILayout/CUA核完整作者与双Line，原生交互dirtyTrue、submit/run期间disable/register/cold接管、后续备注与原源保护。 |
| [NEGATIVE-PUBLIC.json](../../work/audit/<batch>/) | 旧几何state/carrier和缺图像为UNSUPPORTED，旧manifest为INVALID_REQUEST，pin漂移为CONFLICT；较早本域state经新reader inspect通过，区别于旧几何state。 |

主线程已结合实际GPU/UI、冷读和最终独立审查接受R3的T1/T2/T3工程退出。见[阶段交付](../../work/audit/<batch>/)、[退出矩阵](../../work/audit/<batch>/)与[最终公共回归](../../work/audit/<batch>/)。审查发现的Collection旧状态漏检和state内部绑定已修复；各历史作业保留自己的pin，不改签。

母P5/P5-V/R4/P6–P9仍未结项，保留真实角色/未见资产的风格美术、复杂材质/实例、光学、完整1080p24和4K60压力、独立技能盲用、安装封包发行等目标。T0 main车道三项研究归档管理待办仍由所属车道处理，不绕过，也不阻断已授权R3技术开发。旧Wiki build熔断未恢复，不宣称知识库维护闭环完成。
