# Toon Workflow — 原生核心

## 0.19.0原生子帧合同（2026-11-10）

统一工作台与公共CLI默认生成manifest.v2/sequence.v2，保留名义请求并记录Blender实际有效时间，修复2.8/3.2只规范化子帧导致的严格拒绝；负子帧观察与合法setter参数分开。越界和有效时间碰撞预拒，原精确检查不放宽。旧manifest.v1/sequence.v1继续按原身份读取/执行，恢复不跨政策或pin。所有既有面影/法向/修形/描边仍在单包中。规则与边界见[原生时间](../../docs/toon-workflow/NATIVE-TIME.md)；完整P5/P9仍未完成。

## 0.18.0方向阈值面影（2026-11-09）

新增头部参考空间的左右光向阈值POINT图、独立区域权重与默认0强度，嵌入同一母工作台。原生GN逐帧读取参考与Sun方向，Shader在插值后比较阈值，保留原材质更暗的投影并额外压暗；不改Mesh位置或法向。支持正面侧切混合、顶光淡出、选区编辑、受保护的Emission绑定/解绑及四字段冻结见证。它是方向阈值工具，完整SDF贴图与多灯面影仍待开发。操作与边界见[方向阈值面影](../../docs/toon-workflow/FACE-THRESHOLDS.md)；完整P5/P9仍未完成。

## 0.17.0局部作者法向（2026-11-09）

新增显式opt-in的参考空间方向POINT图、Edit Mode所选顶点方向编辑及双向椭球/映射切换。保原修改器owner/UID/字段和已连接材质读者，默认v1不变；静态图随头部参考运动，不自动传输独立嘴型/下颌或发丝形变。共用原整头配置和单一母工作台，逐帧冻结记录v2地图名/空间并与head实际配置交叉。源码134/单包136执行库存，公共CLI基础监管不变。见[局部作者法向](../../docs/toon-workflow/LOCAL-NORMAL-AUTHORING.md)；完整P5/P9仍未完成。

## 0.16.0头部整体与统一插件（2026-11-09）

新增强ID整头配置，覆盖脸/睫毛/眉毛/头发/眼部/口部/附属物，每部件独立权重与强度、默认0；支持受保护的角色调整和移出。法向嵌入母工作台，内置描边面板委托显示；单一安装候选使用真正包命名空间，并由内置CLI薄适配调用既有公共监管。见[整头法向与单包操作](../../docs/toon-workflow/HEAD-NORMALS.md)。完整P5/P9仍未完成。

## 0.15.0艺术法向实验（2026-11-09）

新增独立默认0的椭球艺术法向字段、区域权重、原生Diffuse绑定与可逆解绑，头骨参考逐帧求值。法向位于镜头修形之前，保留Mesh法向与二维线分类；联合回执对corner normals和三个字段做独立冻结见证。公共执行清单增为130源，overlay身份0.84.0+toon.0.15.0。操作和能力边界见[艺术法向](../../docs/toon-workflow/ARTISTIC-NORMALS.md)。完整P5、面部SDF与完整艺术验收继续进行。

## 0.14.0逐相机修形方案（2026-11-09）

新增真实相机ID方案、全目标预检/回滚、GUI显式预览与后台逐帧匹配。默认强度0，保每目标独立GN/权重，拒绝已修形网格反馈及后置修改器；原生参数动画不与方案管理混用。公共toon-workflow四操作不变，实现清单当前128源；overlay为0.84.0+toon.0.14.0，项目默认CLI本轮读到0.85.0。

指定C94的独立不透明诊断角色已完成六时刻/两相机非零方案及关闭三帧公共联合渲染。原rig警告、原Glass、连续全动画与完整美术资格保留。操作见[逐镜头说明](../../docs/toon-workflow/CAMERA-SHOTS.md)。P5-V仍在进行，旧Flattice Demo不恢复，不宣称商业等价。

## 历史0.13二维领域入口（R3工程已验）

当前二维联合流程通过公共 `blenderctl.ps1 toon-workflow describe/run/inspect/edit/recover` 使用，领域版本0.13.0，overlay CLI身份为 `0.84.0+toon.0.13.0`。共享0.84的四个核心文件写入尝试被拒绝后未被原地改写；`tools/toon_workflow_cli_v1/`提供四文件薄overlay及公共helper，由根wrapper显式路由，继续使用公共监督、取消、超时与父接受，不建立私有调度。旧 `toon`、`toon-image` 以及其它已有namespace保持原入口和资格，参数、state和回执不能混用。

先读[0.13领域操作与证据](../../docs/toon-workflow/JOINT-DEVELOPMENT.md)，用 `toon-workflow describe --operation run --schema` 核对当次闭合合同；其它operation为 `inspect/edit/recover`。`workflow_contract.request(source, scene, view_layer, times, *, resources, samples_per_axis=2, lighting_samples=64, memory_budget_bytes=512*1024*1024)`生成full request；`--params`接其params对象。实现清单当前127源，用户资源100项上限不变。

`eevee_joint_grid.v3`／`toon-joint-report.v3`支持完整PRESET和COLOR/DEPTH元素，保留author_style与参数动画求值回执；共同外层网格接管线层AA，保留feather/postfilter，不等同原生完整AA。`toon-workflow-state.v1`由父接受与独立冷读核验；edit候选必须经同公共监管的子inspect冷开，recover只复用同来源、资源、manifest和pin下的已验证完整前缀，在新job重渲其余帧。旧grid v1/v2冷读保留；新领域明确拒绝旧几何manifest/state/carriers，不删除或迁移它们。

本批[标准入口对照](../../work/audit/<batch>/)、[作者往返](../../work/audit/<batch>/)、[恢复实证](../../work/audit/<batch>/)和[真实GUI](../../work/audit/<batch>/)覆盖复杂十时刻、image brush、两条参数轨、两组两Line编辑、运行中保存/重注册/接管和一次取消恢复。标准对照是三EXR pass共69个ZIP像素块及头exact，未用独立float解码器。全部结论限Blender5.2.1／`9e2066aef7ef`、本地不透明Mesh、最长边2048；链接、实例、光学透明、DOF/MB未获资格，完整1080p24与4K60目标保留。

技术证据不签署母整体：R3的T1/T2/T3工程退出已由主线程复核接受，P5/P5-V/R4/P6–P9不结项；T0 main车道三项研究归档管理待办由所属车道处理，不阻断已授权技术开发。旧Wiki build熔断尚未恢复。本批[阶段交付](../../work/audit/<batch>/)与[退出矩阵](../../work/audit/<batch>/)保留最终签署、修复和证据边界。下列0.10/0.11/0.12记录保留历史身份与源资格。

## 历史0.12.0开发候选（2026-11-09）

当前源码在 EEVEE／稳定0.2.27二维描边联合渲染上新增固定空间位置的原生光照积分与显式有序序列：`eevee_joint_grid.v2`／`toon-joint-report.v2`、`eevee_joint_sequence.v1`，保留旧v1冷读。外层共同空间网格每轴1/2/4/8；`lighting_samples`默认64、整数1..256，`filter_size=0`。适用版本限定Blender5.2.1／`9e2066aef7ef`，不宣称与原生完整AA等价。

新公共作业已完成5个乱序含子帧时刻、父级／原生GN修形／灯光动画与双镜头切换，每帧Beauty/Line冻结参照exact；非零修形相机不匹配明确拒绝。480×360 Area灯夹具的16／64光照采样相对旧1采样噪点改善，仅限该夹具，未取得最终画质资格。工作台新增模式、时间数组与光照采样控件；真实UI与取消验证以本批最终记录为准，不沿用旧候选资格。

读取 [联合开发入口](../../docs/toon-workflow/JOINT-DEVELOPMENT.md)；新审计根为`work/audit/<batch>//`，最终交付记录汇总于该批`DELIVERY.md`。旧0.11证据保留在[原批交付记录](../../work/audit/<batch>/)。风格扩展、完整作者／材质与实例、最终专用CLI/state兼容、真实角色美术、透明水烟／DOF／MB、1080p24与4K60压力、技能盲用及封包发行仍待验；母任务范围不缩水。以下0.10及旧CLI/几何描边段落保留历史身份；旧Mesh/Curve描边不因此恢复，新结果不改签旧state/receipt。

自主实现的源身份、受管材质和固定拓扑边描边核心，使用 Blender 原生节点及 Mesh/Curve 输出，无商业运行组件。许可证为 GPL-3.0-or-later，全文见 [LICENSE](LICENSE)。

当前正式代码为Toon0.10.0 / CLI0.82.0，默认run manifest0.9.0；已正式合入并从原源fresh通过正式48FHD创建、更新与回退。旧0.1–0.8合同与历史资格保留；0.9仅支持BLENDER_EEVEE的`eevee_regions`并完整继承0.8参数动画。`eevee_regions`保留未分配槽、OBJECT底层DATA、面索引与有序拓扑；每region独立Material/Core，动画region另有独立Action。静态可移植模板仍为`toon-template-library-v1`，只保存settings，不保存轨道、Action、场景或slot；apply止于`prepared_not_applied`，必须另行普通`toon.run`。区域与模板的历史合同见[区域材质参考](../../skills/blender-toon-workflow/references/eevee-slot-regions.md)及[可移植参数模板](../../skills/blender-toon-workflow/references/portable-templates.md)，新动画操作见[受管参数动画](../../skills/blender-toon-workflow/references/parameter-animation.md)。各批历史pin与回执保持原身份。0.7区域原pin的8+1公共接受、9拒绝及辅助模板复用边界继续见[区域交付](../../work/audit/<batch>/)，不改签到本轮共同pin。

历史0.5.1增量（EEVEE主线）：CLI0.77.1/Toon0.5.1已实现有限三色基础材质`eevee_cel3`，manifest仍为0.5.0。真实道具新pin48帧父接受与原300秒冷内容门已通过，范围见下文；Cycles已有能力与严格失败保留，新增能力及少前2/终末地社区PBR研究独立后续验证。Myona延期，P4/P5整体、GUI/安装和技能独立盲用仍未结项。

当前由公共 `blenderctl toon describe/run/inspect` 及独立模板作业 `toon.template-save/template-edit/template-apply` 调用。`__init__.py` 保持纯 Python；这里尚不是已安装或已验收 GUI 生命周期的发行包。候选本身可在 Blender 中编辑，冷读不依赖 handler 或源 Text 执行。

## 受控 source-u 抖动（0.10.0 / CLI0.82.0有限正式已验）

正式合入10份runtime文件并保留其余227份；并行material describe新增6行保留，新237文件共同pin `9aa2de8ee57ca3dcfa5f98cebe1aa729215ce16634dda3f56281792dd7f284f4`。根从原源通过实际`./blenderctl.ps1`入口fresh正式48FHD24/EEVEE64 create→update→rollback，三作业均父接受；create `704310a7405642fc8d46bc9ed7d14ec1`、update `dcf9b045a448490aac265d26775641c8`、rollback `3f2481738bdc4bcbaa2ffd2f08ed39fb`。正式独审23008门、0失败；144暖/9冷完整解码且冷暖exact，非目标384条完整cache及回退240条完整cache exact，全48回退99,532,800 RGBA像素exact。fresh正式create对0660的全部240条完整line cache和48帧RGBA亦exact；完整state仍有原始差异，不称字节或值相等。

正式透视create另经真实新进程冷开原生probe：48帧/240组帧/576639安全内部截面，最大误差`.004431139986991184px`，通过固定`.016px`门；此项仅理论视锥内安全侧边内部，不资格化帽join、遮挡表面或渲染像素。合入记录另保留矩阵与Mesh手改两负例，均`CONFLICT`且未产生新expectation/plan/candidate/state，其原作业身份不改签。详见本批`FORMAL-MERGE-v002.json`、`NATIVE-RUNTIME-PIN-v004.json`、`evidence-review/formal-v001/FORMAL-INDEPENDENT-REVIEW-v002.json`与`native-analysis/FORMAL-PERSP-ACTUAL-48-v001.json`。合入记录的pending是当时快照，正式终态由后续三job与独审证明。0660的正交/更新原生测量和三支媒体继续保留隔离身份，不因正式create相等而改签。


manifest0.9的全局与组`jitter_px`均为[0,64]，按每个有效bucket的继承结果分支。effective J>0即使宽度样式中性也走styled路径；effective J=0保持旧native路径。完整物理源边先与正near/far深度相交，以完整source-u/key/seed/time bucket计算噪声；dash、mask与视锥不重置相位。Fraction只枚举可见范围的严格内部网格索引，保留2048实际采样与2M总顶点门；不可表示或重复样本明确拒绝。

正抖动fragment/chain保留完整正深度边direction/normal；原生carrier仅正分支采用相机原点相对Mesh坐标和实际translation矩阵，保存前盖几何SHA，冷读以实际RNA矩阵/坐标双精度组合核验。原`.0005px`屏幕位置、`2e-6`深度与cold300秒门不变。机制、操作、有限证据与未验范围见[受控抖动](../../skills/blender-toon-workflow/references/styled-jitter.md)。

隔离237文件pin `0660a0bcac34dc7f94bf1ac86dedb894deccf2b0477d90c031b8d8a308cc4d0b`：根38纯检查、旧8套schema/maps保留、两景48FHD24/EEVEE64 create及透视update/rollback父接受，create独审9089门、链独审11079门通过；三组48帧独立原生安全内部截面分析通过原`.016px`门。三支48帧技术视频完整CPU解码/PTS/BT709通过。零抖动仅已证完整native记录/哈希、Ink/Action及真实暖图RGBA相等，原始顶点数组exact未做、完整state不相等。上述仍是0660本轮五线原创夹具的隔离阶段证据；后续9aa2正式资格单列于本节开头，二者不改签，也不扩大Myona/Cycles/帽join/光学、GUI/安装/独立技能盲用或P4–P9资格。下列0.9参数动画正式证据保留其原pin与版本。

## 受管参数动画（0.9.0 / CLI0.81.0有限正式已验）

普通`toon.run`的manifest0.8显式提供`animations`。轨道按template_id/field选择实际消费的颜色或标量，插值仅LINEAR/CONSTANT，mode/UUID/槽绑定不动画；同模板各region不共享Action。更新冻结有序selectors，保留未请求合法静态手改，拒绝曲线/slot/结构/NLA/driver或外部消费者变化；静态模板v1不迁移Action。最终low<high按连续域、每region合并后的有效值核验。

state/frame/chain为v7/v6/v3；逐帧实际shader样本、独立reference、完整原生Action与managed Material/真实槽位交叉绑定。写前重算计划，写完统一求值与snapshot，异常恢复验证限本次联合参数更新，不等于整个apply_plan的进程内事务。原源保护、独立冷读、父接受与原cold300秒门保持。

隔离v004 pin `b95359bc974b31080cae286c220de3647e3af90256f7371de318ce03c5395721`已有原创四形体48FHD24/EEVEE64 create→update→rollback父接受；399项复核、144暖图/9实际冷图及48帧回退RGBA exact见[隔离交付](../../work/audit/<batch>/)。该pin的159个原生时刻抽样、4例失败注入与11项父回执克隆分别计证据，不外推子帧图像、任意资产或完整审美。

正式合并15份文件并冻结237文件共同pin `cbd5cd1fca1df3665119ab95fa09c33e5cabdc96162204bc5d1f8bf12321d542`；实际blenderctl.ps1的48FHD24/EEVEE64 create→update→rollback均父接受，独立1996项检查0失败，144暖图/9冷图RGBA差0，回退99,532,800个RGBA像素exact。13个公开回归作业均符合预期，覆盖5拒绝、4个保留/兼容正例、实际159原生样本和4例失败注入。见[正式全片独立复核](../../work/audit/<batch>/)及[正式回归](../../work/audit/<batch>/)。两段新视频已完成全48帧CPU编码/解码、精确24fps/2秒/PTS及BT709标记验收；有损SDR不声称像素无损或完整审美批准。隔离历史保持原pin；实际调用仍以当次describe核身份。Myona延期，Cycles原严格失败、GUI/安装/技能盲用未验及P4–P9整体未完成保持。

## 可移植参数模板（历史0.8.0 / CLI0.80.0增量）

三个模板操作只处理静态数据。`template-save`从已保存工程的原生受管材质导出实际完整settings；`template-edit`在`expected_revision`匹配时生成revision+1的新文件且不覆盖原文件；`template-apply`按显式binding只替换普通`eevee_regions` run请求中的目标settings，输出`toon-resolved-request.json`，状态为`prepared_not_applied`，不会生成候选，后续必须执行普通`toon.run`。

该历史增量的模板库为`toon-template-library-v1`，普通run manifest为`0.7.0`，不自动迁移旧state/pin；模板操作本身不携参数动画轨道、Action或驱动，也不支持稀疏设置、任意wrapper图、slot绑定或GUI直接应用。现有证据为pin `e030188c7c82b7ea6f26d972089d65614f791225d6795eb83a53a33711110e65`下四形体48帧create/update/rollback真实父接受、1010项交叉检查通过及回退99,532,800个RGBA像素exact；8个公共拒绝场景、实际额外wrapper拒绝、旧0.6普通run及实际参数只读导出已通过；其它旧版本迁移未验。字段、命令发现、输出与完整边界见[可移植参数模板](../../skills/blender-toon-workflow/references/portable-templates.md)。

当前默认发现合同为`0.9.0`，旧`0.1.0`至`0.8.0`分支保持原合同与历史身份。0.9使用state-v8、frame-v7、chain-v3、cold-v2，0.8动画语义完整继承；正式入口身份仍以当次describe为准。0.8使用state-v7、frame-v6、chain-v3、cold-v2；0.7仍使用state-v6、frame-v5、chain-v3、cold-v2，没有跨pin/state迁移。0.6新增反射mode严格十字段，旧mode字段不变；state/frame/chain仍v5/v5/v3及冷读v2，不提供跨pin/state自动迁移。0.1使用state/frame/provenance v2/v2/v1，0.2为v3/v3/v2，0.3为v4/v4/v3；0.4与0.5共用v5/v5/v3及冷读content/combined回执v2，run报告仍为1.1。0.5完整继承0.4 lines/stroke/placement和实际源重建证明门。完整有序链来源逐帧存入job侧车，工程内保留canonical SHA/counts及完整native/材质/源保护记录；不同合同或实现基线不隐式迁移。

0.5新增`BLENDER_EEVEE`专用闭合七字段：`mode/base_color/shadow_color/mid_color/lit_color/threshold_low/threshold_high`。RGBA均alpha=1、数值有限[0,1]且low<high；真实Diffuse→ShaderToRGB→RGBToBW经双GREATER_THAN和两层MixRGB输出Emission1，等号属于较暗档。0.5仍接受旧preserve/native_cel完整七字段，不能跨mode混字段。创建替换隔离目标全部材质槽并保留原图；update按前次声明/实际snapshot做三方合并，保留未请求合法静态socket手改和合同wrapper快照，不能外推任意RNA或动画/驱动。不允许更换mode/engine/time/render，核心结构或共享消费者变化仍拒绝。完整desired的mid-only示例见[三色操作参考](../../skills/blender-toon-workflow/references/eevee-cel3.md)。

历史pin`031baaa138a93f8b5c6975134d4aa6106a89c8d27c532de251ab72957fd5d063`的48FHD24/64samples创建→mid→high→回退链经独审1022门；回退全99,532,800个RGBA像素exact，四作业首中尾共12张实际冷图与暖图差异0，不是全48冷重渲。旧四版frame24单帧和0.5 camera_plane depth4单帧已公共接受。证据见本批[Full48独审](../../work/audit/<batch>/)与`SEQUENCE-legacy-and-plane.json`。material有限独审1204门与physical7作业有限独审已通过，保护范围及遮挡仅左面板102点的边界见三色参考；三材质负例顶层实际为WORKER_FAILED、phase分别为managed_parameter_conflict/managed_structure_conflict/shared_material_consumer，mode切换为UNSUPPORTED。fixture媒体已实际通过全48CPU解码/精确PTS/2秒/CICP，回执在本批`media/create-full-v001/VIDEO-DELIVERY.json`，未做完整播放。该旧pin真实道具48暖帧后的独立content固定300秒超时、无父接受，历史失败保持；不能用新pin结果改标旧作业。

Toon0.5.1历史pin为`5288f1815ed609c2a9cfd8579065ef4a259246188f5f0c08f4f91d6c7851fc8c`。调用内face normal复用、仅冷检查省去未消费assignment诊断行，不做期望key过滤，不减少完整classify、选中来源及source/cache/material/placement核对。新pin从原source建立fresh基线，真实道具48帧完整冷内容272.447秒通过原300秒门；独审509门、105张实际PNG解码，新旧full48全99,532,800个RGBA像素exact。实际cold仅首中尾三帧，另有short3三帧；native独立差分38门及技术媒体通过均保持各自范围，不是任意资产性能或全48冷渲认证。新pin另有frame24、FHD/64samples的native_depth创建→mid→high→rollback四步链及独立camera_plane/depth4创建，五作业独审325/325门通过，native四步更新间完整线cache/Ink/Action/raw链exact，native rollback全RGBA像素exact，五作业各自实际暖/冷图差异0；独立plane仅保留自身depth4放置证明及暖冷一致，不声称其cache或画面与native相同；证据见本批`review/fixture-chain/REVIEW.md`。新pin单帧通过，未认证新pin完整48帧更新链，旧pin完整48帧更新链保持原归属。证据、实现与未验边界见[冷检查参考](../../skills/blender-toon-workflow/references/eevee-cold-checks.md)。

P4.1 新分组按目标 Object UUID 和线类定义独立宽色及节奏。关闭的组移除命中类别，剩余类别按 `kind_order` 选唯一 primary owner；同边不重复绘制。首批限不透明 screen ribbon，所有组共享每帧总输出预算。CLI 参数更新可替换生成线；手改 Ink 节点、几何或外部消费者导致明确冲突，尚不做 Ink 手改颜色的三方保留。P4 全集和 Myona 尚未完成；当前验证范围见 `work/audit/<batch>//`。

P4.2 在0.3新增canonical source-u的dash、taper、butt/square/round端帽、分段线性宽度profile、相机轴向深度宽度衰减及精确物理边局部mask/profile。裁切不重置原u相位，视锥人工切口强制butt；局部profile替换组profile后仍乘组宽、taper及fade。旧0.3–0.8 styled切片要求`jitter_px=0`，深度fade改变不透明几何宽度，不是alpha或光学透过率；source-u虚线也不保证恒定像素长度。操作与边界见[源边样式说明](../../skills/blender-toon-workflow/references/source-u-strokes.md)。

P4.3在0.4新增显式native_depth/camera_plane放置；先构造完整样式screen ribbon，仅替换最终depth再反投影。实际源重建与完整三角/XY/depth验证补强保存及update预检，伪造cache/raw自洽不能替代来源验证。它仍会被更近物体遮挡，不是always-visible或光学修复。双引擎48帧创建/更新/回退已公共接受，但Cycles严格带外像素门仍有10帧各1像素、最大3/255的AA边缘残差，原因调查中，P4.3未整体结项。字段、clip/间距准入及当前证据见[相机平面放置](../../skills/blender-toon-workflow/references/camera-plane-placement.md)。

更新读取已保存候选及带SHA的匹配state，核对同合同与实现pin；不包含GUI未保存编辑，也不自动应用到GUI。实现变更后从原源建立新pin基线，不改标旧候选或state。0.3清理前见证原Ink与可见性Action/slot引用，清理后只按精确身份、零用户及节点/slot结构复用；Ink只改desired颜色，Action数据不重写，未知同名孤立ID仍拒绝。0.1/0.2原分支保留。

最终pin `b5c04244a11f34e78d7a6dd0de896aa2b000e987d33770988ed36b59c4ecc5d0` 的fresh→local→color→empty→restore真实保存链33门独审通过：各步未命中完整cache exact，color→restore全部12份完整native cache exact，Action名称漂移0。局部冻结结构/像素门通过，9个非目标group-frame完整record/action的18项补充canonical JSON字节比较亦exact。范围仅该owned cache往返，不宣称全局孤立ID清零。证据见[真实链审查](../../work/audit/<batch>/)。0.3受控opaque新建的局部异常清理不提供全帧或整个apply_plan进程内事务回滚；GUI应用及全帧进程内回滚未支持。

最终pin下的EEVEE/Cycles解析夹具各48帧1080p24/64 samples均父接受，完整48张PNG的冻结像素门通过；两支MP4编码、CPU全48帧解码、逐帧精确i/24 PTS、2秒及BT709 limited SDR检查通过，主线程已查看两张48帧contactsheet。像素报告在本批`pixel-review/final-v002/{eevee-final-v004,cycles-final-v004}/REPORT.json`，媒体回执在`media-preview/final-v002/{eevee-final-v004,cycles-final-v004}/VIDEO-DELIVERY.json`。最终EEVEE的192个实际原生row-frame独立测量通过：最大全宽误差0.009976px、圆帽弦高0.009549px，原始及bias后深度另有独立数值核验，详见本批`native-readback/actual-full-eevee-b5c-analysis-v001/`。这仅认证有限解析夹具，不等于完整播放、双引擎艺术等价或用户审美通过。

历史82的三帧局部更新像素通过，但完整Mesh/Ink结构门因名称漂移失败；73d候选连续更新的Action漂移失败也保留原归属，不改标为最终pin。完整片中mask避开物理frustum端点，不覆盖孤立frustum帽像素测试；真实道具256×144三帧仅为诊断。历史像素证据见[本批像素索引](../../work/audit/<batch>/)，最终范围以本批交付索引为准。

实际合同与参数从 `toon describe` 发现。首切片为不透明基础材质、正常原生深度描边及整数帧缓存；完整笔刷、交线、透明/水体/体积光学、分层、MB/DOF、安装和 GUI 操作在后续阶段验证。保留完整产品目标，不把当前执行子集当作功能终点。

描边使用 VIEWPORT 求值，预检和逐帧生成会拒绝已知视口/渲染开关或细分级别差异、Simplify 及嵌套 Is Viewport 节点；不改用户设置，不承诺任意修改器渲染等价。冷读覆盖全整数帧的局部对象隐藏状态与设置，实际冷渲染取首、中、尾三帧，独立进程上限 300 秒。

P3审计：`work/audit/<batch>//`；唯一技能源：[blender-toon-workflow](../../skills/blender-toon-workflow/SKILL.md)。这些是工程开发证据，不是用户对代表性案例或审美的批准。本轮官方skill quick_validate已通过，仅验证技能元数据与基础格式；P4整体、技能独立盲用及安装生命周期仍待验收。
