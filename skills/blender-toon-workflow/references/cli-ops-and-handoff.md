# 公共 CLI 执行、现场接手、恢复与合成交接

这一份参考管怎么用 `blenderctl.ps1` 稳妥跑完 Toon 后台作业、接手现场 Blender 窗口里的手调改动、做好隔离保护与故障恢复，以及怎么把分层通道干净交给合成部门。

## 1. 公共命令全景速查

所有操作先用 `discover --product toon` 和对应 `--help` 核对当前宿主与参数，再构造 UTF-8 JSON 提交。全局选项（`--jobs-dir`、`--transactions-dir`、`--timeout`、`--async`、`--compact`）放在子命令前面。

| 命令 | 典型用途 |
|---|---|
| `toon-workflow describe` | 查询当前插件契约、模块清单、编辑模式与已选宿主 |
| `toon-workflow run` | 对已保存工作副本执行联合渲染，产出 `Beauty`/`Line`/`BeautyLineInteraction`/`Combined` 与状态回执 |
| `toon-workflow inspect` | 源检查取得 `author_sha256`；结果检查传入状态描述符核对独立冷重开 |
| `toon-workflow edit` | 按匹配 `expected_author_sha256` 编辑静态 `BASIC` 线宽或完整 `PRESET` 样式并冷验新候选 |
| `toon-workflow recover` | 指向原作业持久 `toon-workflow-request.json`，复用已验连续前缀并重渲未完帧 |
| `toon-workflow surface-slot` | 活动槽表面着色（`CEL3`/`GAME_REFLECT`/`INK_FLAT`）、明暗过渡、眼层、固定发影、HM/遮罩高光与 `--normal` 法向 |
| `toon-workflow finish-slot` | 具名槽独立高光、边缘光、`HEAD_BAND`、`snapshot-sun` 与双强度曲线动画 |
| `toon-workflow component-slot` | 具名槽 `GAME`/`MANGA` 与 `ALPHA_SHEET`/`GLASS`/`WATER`/`SMOKE`/`EMISSION` 组件及场景透射开关 |
| `toon-workflow cel-recipe` | 多槽静态 CEL 组合配方的创建、绑定、预检、应用与导入导出 |
| `toon-workflow sdf-face` | 面部前投 UV、方向载体、2D/3D 方向模式、关键光角 `compile-keys`、阈值属性与艺术投影接受层 |
| `toon-workflow sdf-hair` | 头发 SDF 图集编译（`cyclic_v1`/`omni_v2`）、挂载、调节与卸载 |
| `toon-workflow shot-look` | 漫画纸墨网点与前后景分区 `Look`、原生媒体对象列表与艺术线叠层设置 |
| `toon-workflow lighting-author` | 主副光、场景/材质调色板、可关键帧影形控制器与面部法向联动修复 |
| `toon-workflow viewport-lines` | `PRESET` 线组、选择器、边源、线层级、材质线色、线模板、家族/样式集、修形方案与相机多层 EXR 输出 |
| `toon-workflow brush-settings` | 笔刷精细字段补丁、草稿、接链、虚线、映射衰减与强度关键帧 |
| `toon-workflow viewport-session` | 绑定已打开 Blender 视口，控制局部描边、笔刷/曲线草稿、影形控制点与作业提交/取消 |
| `toon-workflow vector-output` | 视图层/合成节点矢量线列表编辑与 `EPS`/`AIEPS` 导出 |
| `toon-workflow omni-preview` | 多光向/多机位环绕预览的规划、提交、查态、取消与分页 |

## 2. 四大核心操作与请求构造

执行联合渲染或源/结果核验时，按下面口径准备参数：

- **构造完整请求**：可用插件自带纯 Python 助手（`toon_workflow_addon.workflow_contract.request`）读取源 `.blend` 的 SHA256 与当前实现指纹，生成闭合的 `manifest` 与时间数组（支持整数帧与子帧，保留原请求顺序）。`resources` 必须逐项列出真实外部依赖与 SHA256（无外链时明确填 `[]`，上限 100 项）。完整 `{schema_version, command, params}` 送给 `blenderctl.ps1 request <file>`；纯 `params` 对象送给 `blenderctl.ps1 toon-workflow <run|inspect|edit|recover> --params <file>`。
- **区分源检查与结果检查**：
  - 源检查：传 `run` 同形状参数运行 `toon-workflow inspect`，从返回的 `toon-author.json` 中取出 `author_sha256`。
  - 编辑：在 `run` 参数上附加样式文件描述符 `{file, expected_sha256}` 与匹配源的 `expected_author_sha256`（注意它是作者指纹，与样式 JSON 文件自身 SHA 分开填写）。
  - 结果检查：只传本域 `state` 描述符与当前 `implementation`，独立核对序列完整度、来源绑定与冷重开一致性。
- **长作业等待与前缀恢复**：提交长渲染后用 `job wait <job_id>` 观察进度与终态；任务被取消或中途失败时，保留原作业目录与终态日志，排查原因后若源、资源与实现指纹均一致，用 `toon-workflow recover` 指向原作业的 `toon-workflow-request.json` 建立新作业，自动复用已完成且通过验证的连续前缀帧、补算剩余帧。源文件或代码有更新时，基于新快照提交全新 `run`。
- **排查父拒收摘要**：遇到顶层候选冷重开拒收提示时，继续下钻阅读其关联的子 `inspect` 报错日志，再决定按独立线作者、家族回执归档还是材质绑定合同处理。

## 3. 接手现场 Blender 窗口：`viewport-session`

用户在打开的 Blender 窗口里做完视口描边、笔刷曲线或影形控制点手调后，经由 `viewport-session` 稳妥交接，后台工作流始终消费已保存的磁盘快照，保持用户当前窗口原样完好。

1. **查询窗口上下文**：运行 `toon-workflow viewport-session --session-dir <dir> --session-id <id> --operation status` 取得 `view_id` 与 `expected_context`。
2. **同步笔刷与曲线**：使用 `brush-status`、`brush-open-author`、`brush-open-dialog`、`brush-configure`、`brush-point`、`brush-commit`、`curve-select`、`curve-configure`、`curve-discard`、`curve-restore` 核对或提交笔刷与曲线修改；保存前确保待提交曲线合法同步，消除 `curve_error`。
3. **选取影形控制点与提交作业**：用 `shadow-status` 与 `select-shadow-control` 操作视口相机影形控制点；用 `local-visibility` 切换局部描边预览；用 `submit`、`job`、`cancel`、`result`（配合原始 `--request-id` UUID 取回持久回执）驱动具名线稿作业。
4. **接收前核对身份**：从工作台接收后台候选前，确认原作者指纹、对象到网格/材质槽关联、共享用户与原生动画状态匹配，再落盘生成新工作副本继续往下推；需要用 CLI 接续工作台历史任务时，配合 `viewport-lines` 的 `inspect-workbench-author`、`refresh-workbench-job` 与 `receive-workbench-author`。

## 4. 隔离运行环境与源保护习惯

- **隔离配置与自有进程**：后台作业使用独立的批次作业目录与隔离 `BLENDER_USER_CONFIG`，保持全局用户偏好不变。任务达到终态后，只核实并清理本任务启动的已无用自有后台进程，保持用户正在使用的 Blender 窗口完好。
- **保护非目标数据与弱外链**：改前列出共享材质、网格、贴图、节点组与动作的消费者；解绑后仍需留存的旧 Action 按合同明确保留。遇到 `link=False` 追加留下的弱库记录，按原生原始/绝对路径配对核实真实外部文件消费者，打包贴图后用冷重开确认闭包齐全。

## 5. 与合成部门的线色叠合交接合同

向 [blender-compositing](https://github.com/tsist/blender-compositing-workflow/blob/main/skills/blender-compositing/SKILL.md) 移交三渲二渲染结果时，按实际输出配置标明通道名称、色彩空间、Alpha 口径与重建公式，避免合成端错用普通 Alpha Over 产生黑边或发灰：

| 输出配置 | 交付通道 | 色彩与合成重建规则 |
|---|---|---|
| 不透明相关联合渲染 | `Beauty`、`Line`、`BeautyLineInteraction`、`Combined`（多层线性 EXR） | 场景线性 Rec.709 预乘层；按 `Combined = Line + Beauty - BeautyLineInteraction` 精确重建，合成端调线色或叠辉光时以这三层拆分控制 |
| 原生媒体艺术线 | 原生 `Beauty`、`OpticalLine` / `Line`、`Combined` | `OpticalLine` 显式叠加在原生媒体 Beauty 上：`Combined = OpticalLine + Beauty * (1 - OpticalLine.alpha)`；线条作屏幕艺术叠层 |
| 漫画整镜头 Look | `Combined` + `Look`（含纸色、墨色、网点与分区） | 保留原 `Combined` 与 Alpha，`Look` 作为非线性风格成图交付并标明裁剪与 Alpha 状态 |
| 独立后台相机线稿 | `viewport-lines render-file-output` / `render-camera` 产出的 `COLOR`/`DEPTH` 多层 EXR 或 PNG | 标明每层对应线组、色彩空间与预乘方式，直接接合成节点的线色/辉光支路 |

**移交清单**（逐项附在交接说明里）：
1. 最终工作副本 `.blend` 绝对路径与 SHA256，以及已确认的 `resources` 外部依赖清单。
2. 渲染序列或单帧目录，写明帧范围、帧率、分辨率、色彩空间、Alpha 口径与每个通道名。
3. 代表帧的 `Combined`、白底 `Line` 与关键面部/高光局部对照图，以及连续帧防抖观察结论。
4. 独立 `toon-workflow inspect` 冷重开通过摘要、原持久请求路径（便于续跑）及本次遗留或需下游在合成里配合处理的事项。
