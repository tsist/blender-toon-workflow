---
name: blender-toon-workflow
description: 使用统一 Toon Workflow 插件与公共 blenderctl 制作和精修三渲二风格，涵盖区域着色与混合 PBR、面部 SDF 影形、头部法向与头发高光、色阶配色、二维视口描边、镜头修形、环绕预览与后台线稿通道输出。接到角色或场景三渲二风格化、描边防抖排错或输出独立线色通道交下游合成时使用。
metadata:
  version: "1.0.0"
---

# Blender 三渲二工作流

用 `blenderctl --edition toon@1.0.0 toon-workflow describe` 读回 1.0 的包清单。保存场景的联合输出分别核对 Beauty、Line、OpticalLine、Combined。既有 0.52.1 和 0.52.3 作业显式选择原发行版，再使用保存的请求与原作业目录恢复。

本技能负责把定稿角色、道具或镜头做成可控的三渲二画面与独立线稿通道，属于 [渲染与风格化部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/render.md)。工作聚焦着色、影形、描边、镜头修形与联合输出，保留上游传入的原网格、UV、法向、权重、形态键、骨架与动作；公共 CLI 的批次目录、SHA 校验与长作业等待见 [blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md)。

## 第一步：查真实入口，摸清场景与分层

动手前先通过公共入口查清当前宿主与可用子命令，省去猜参数名或套旧接口的麻烦。

```powershell
$ctl = './blenderctl.ps1'

# 查看 Toon 产品入口与当前宿主环境
& $ctl discover --product toon
& $ctl discover --product toon --edition 0.52.3 --action 'toon53.bind#DIRECTIONAL_FACE'

# 查看工作流契约与子命令帮助（纯查询，直接返回）
& $ctl --edition toon@0.52.3 toon-workflow describe
& $ctl toon-workflow --help
& $ctl toon-workflow viewport-lines --help
```

本流程的制作基线为 Windows Blender 5.3.0 `02f0899d78e3` 与已签收的 Toon 0.52.3。磁盘作业用 `--edition toon@版本` 选择同版实现；`lighting-author` 在请求内指定 `edition`。旧作业使用其原版实现指纹。核对新版部署、候选范围与保存稿交接时，见 [公共 CLI 交接](references/cli-ops-and-handoff.md)。

建好独立批次目录并在工作副本上只读摸底，留好源文件对照：

1. **读清场景上下文**：确认目标 `Scene`、`ViewLayer`、有效相机、起止帧、`Sun` 主光与全部已用材质槽。
2. **核对真实消费链**：列出共享材质的对象、槽位、贴图与现有受管读者（面部 SDF、眼层、固定发影、线作者），把修改落到精确槽位。
3. **按层拆清职责**：大块面影交面部 SDF，局部转折交法向，五官与轮廓交二维线组，特定机位比例交镜头修形，分层定责再动手。

## 第二步：配准参考与区域着色

风格化按材质用途逐槽切入，背景和道具可继续保留原生 PBR。先按公式图或剧照把基础设计与镜头光色拆开再调参。

1. **逐槽施加基础色阶**：用 `toon-workflow surface-slot` 对已用槽施加 `CEL3`、`GAME_REFLECT` 或 `INK_FLAT`，先调稳暗部、中间色、亮部与双阈值，需柔边时再用 `boundary-apply` 开启照度域过渡宽度。
2. **按材质用途叠修饰**：用 `toon-workflow finish-slot` 给金属或饰品加高光、边缘光或头级参考光带（`HEAD_BAND`），支持关键帧强度动画；棉布等哑光件保住干净大块明暗。
3. **混用风格组件与原生媒体**：用 `toon-workflow component-slot` 施加 `GAME`、`MANGA` 或透明薄片、玻璃、水、烟、发光组件；漫画网点分区或原生媒体艺术线叠层走 `toon-workflow shot-look`。
4. **编排主副光与配方复用**：用 `toon-workflow lighting-author` 维护主副光、调色板与影形控制器；多槽协同和跨资产迁移走 `toon-workflow cel-recipe` 显式重绑槽位，复杂原纹理槽选 `PRESERVE` 保留原有节点。

详见 [区域着色、风格组件与混合 PBR](references/shading-slots-and-styles.md)。

## 第三步：雕琢面部 SDF、头部法向与头发眼层

角色观感八成看头脸。所有定位贴图（面部 SDF、高光遮罩、固定发影、眼层）先核对真实消费的 UV、表面锚点与镜像，位置对了再调软硬和光照。

1. **面部影形优先走 SDF**：已有 5.3 方向场时，从 `lighting-author` 的 `directional-face-inspect` 开始，核对头轴、实际采样 UV、脸域和原 Surface；首次连续面影绑定可登记主光、副光和轮廓光。需制作传统方向贴图时，用 `toon-workflow sdf-face` 建立前投 UV，并从真实模型画二值关键光角再用 `compile-keys` 编译。两种路径使用各自的输入契约。
2. **按需挂艺术投影接受层**：单主光平涂皮肤受杂乱投影干扰时，用 `sdf-face` 的 `bind-cast-art` 在原 SDF 上游挂入默认零强度的接受层；鼻影面片支持同对象独立槽单独绑定 SDF。
3. **整头与局部法向做体积辅助**：用 `toon-workflow surface-slot --normal` 为脸、眉、睫、发与附属物分别配参考方向与顶点权重，修饰平滑受光体积。
4. **分清眼层、固定发影与单一受光高光**：用 `surface-slot` 的 `eye-create` 建深度一致的眼影平面与瞳孔高光小片；用 `hair-shadow-attach` 在动态色阶后正片叠底固定发影 B；用 `hair-map-attach` 和 `hair-map-mask-attach` 把遮罩高光融进同一受光权重并只着色一次，避免双亮带打架。需发束独立方向影形时，再评估 `toon-workflow sdf-hair`。

详见 [面部 SDF、头部法向与上下光检查](references/face-sdf-and-normals.md) 与 [头发高光、眼层与发束明暗](references/hair-and-eye-craft.md)。

## 第四步：设置二维描边、线色与镜头修形

先查清当前工程是 `BASIC` 还是 `PRESET` 线模式，顺着现有作者做最小增量修改，保留原模型顶点绘制与锐边（Mark Sharp）。

1. **调线宽、线组与笔刷**：整体静态线宽走 `toon-workflow edit`（`BASIC`）；多线组、选择器、边源、像素线宽、线模板与渲染元素走 `toon-workflow viewport-lines`；笔刷形态、虚线、链角度与映射衰减走 `toon-workflow brush-settings`。
2. **配材质线色与五官短线**：用 `viewport-lines edit-line-material` 按 `AUTHOR_RGB`、`NODE_RGBA` 或 `UV_IMAGE` 协调墨色；鼻嘴短线缺失时用 `freestyle_edge` 自主选边组补回单一轮廓，再用局部 `CORNER` R 属性减掉重复碎线。
3. **接手视口手调与导出线稿**：用户在 Blender 窗口调好描边或笔刷后，用 `toon-workflow viewport-session` 同步草稿、选取影形控制点并接收工作副本；导出 `EPS`/`AIEPS` 矢量线稿走 `toon-workflow vector-output`。
4. **按机位挂镜头修形**：特写或斜侧镜需微调透视轮廓时，用 `viewport-lines edit-workbench-correction` 保存逐相机修形方案，虎牙等附属件绑定无修形动态参考以防二次变形。

详见 [二维描边、笔刷、线色与线模板](references/lines-brushes-and-ink.md)。

## 第五步：环绕预览、连续帧防抖与联合验收

每次实质改动后，都在同机位、同光向下看新图对照，确认转光与转头都稳住再交付。

1. **固定机位拆变量**：用同一半身持物镜头依次做只转头、只移主光、只开关副光。对照角色脸颈、眼发、衣料和道具。用环绕图集定位斜角、纯顶底及背缝疑点，再实渲疑点的邻角。
2. **按实际配置交接通道**：原生媒体艺术线使用 `OpticalLine`，不透明相关采样使用 `BeautyLineInteraction`。两路均交 `Beauty`、`Line`、`Combined`。续跑传原持久请求和同版 `implementation`，并核对复用帧和补帧。
3. **连续播放并冷重开**：审查目标分辨率下的脸影跳变、短线断裂、错误遮挡和高光闪烁。独立进程重开同一交付稿，核对依赖、原数据保护与代表帧，再按约定等级填写 G1–G5。

详见 [参考校准、镜头修形、环绕检查与排错闭环](references/calibration-shots-and-qa.md) 与 [公共 CLI 执行、现场接手、恢复与合成交接](references/cli-ops-and-handoff.md)。

正式交付按 [三渲二商业验收](references/commercial-acceptance.md) 配置审片矩阵，另存 [评分模板](assets/quality-scorecard.template.json) 为交付批次的 `quality-scorecard.json`。同角度逐值一致、连续帧稳定和风格对标各有独立证据。

## 交给下游

移交合成部门或提交用户审看前，给齐五样内容并分别写明冷重开结果、画面观察与待确认项：

- **可编辑工作副本**：最终 `.blend` 路径、SHA256 与完整外部贴图/资源清单。
- **分离渲染通道**：按镜头编号命名，注明分辨率、帧率、编号、色彩空间及 Alpha 口径。不透明相关采样附 `BeautyLineInteraction`，原生媒体艺术线附 `OpticalLine`，漫画风按约定附 `Look`。交接时核对所用配置的合成公式。
- **对照审看图**：代表帧完整 `Combined`、白底 `Line` 与关键面部/高光局部裁切。
- **连续帧防抖结果**：含转头或遮挡变化的连续短段检查记录。
- **恢复与复用入口**：原 `toon-workflow-request.json`、状态回执路径及可复用配方/线模板。

无后期静帧直接交用户做**审看②（最终帧）**；需调色、辉光景深或线色叠合的镜头移交 [blender-compositing](https://github.com/tsist/blender-compositing-workflow/blob/main/skills/blender-compositing/SKILL.md)。

## 例子：给角色做三渲二着色与描边，出线稿交合成

接到任务：「把 `work/hero/shot04_lit.blend` 里的主角切到动画赛璐璐风，保留房间背景原生 PBR，出 `1..12` 帧多层 EXR 线色通道交合成部门做辉光叠合。」

1. **摸底**：建 `work/audit/<batch>/`，算源工程 SHA256。跑 `discover --product toon`、`toon-workflow describe` 与 `toon-workflow inspect`，摸清主角 6 个已用槽、面部 UV 和房间 PBR 槽位。
2. **区域着色**：用 `surface-slot` 给主角皮肤、头发与衣物施加 `CEL3` 色表，用 `finish-slot` 给金属扣加弱边缘光；房间建筑槽全部保持原样。
3. **调头脸与高光**：用 `sdf-face` 核对脸部前投 UV 与入影阈值，收稳鼻颊交界；用 `surface-slot` 挂载单一受光月牙高光遮罩与固定发影 B，核实落点贴合发束走向。
4. **调描边与线色**：用 `viewport-lines` 配置 `PRESET` 皮肤/头发/衣物线组、固定像素线宽与 `AUTHOR_RGB` 协调墨色，启用 `freestyle_edge` 自主组保住短鼻线与完整嘴线，输出设为多层线性 EXR。
5. **预检与连续帧出图**：跑 `omni-preview` 查转光脸影连贯，抽 `1, 6, 12` 帧核对视口与后台线稿一致，再用 `toon-workflow run` 算完 `1..12` 帧含转头序列确认无穿线与抖动。
6. **冷重开移交**：对最终候选跑独立 `toon-workflow inspect` 确认外链完好，把 `.blend` 工作副本、12 帧四层线性 EXR、白底线稿对照图与请求回执交合成部门。

## 参考

- [区域着色、风格组件与混合 PBR](references/shading-slots-and-styles.md)
- [面部 SDF、头部法向与上下光检查](references/face-sdf-and-normals.md)
- [头发高光、眼层与发束明暗](references/hair-and-eye-craft.md)
- [二维描边、笔刷、线色与线模板](references/lines-brushes-and-ink.md)
- [参考校准、镜头修形、环绕检查与排错闭环](references/calibration-shots-and-qa.md)
- [公共 CLI 执行、现场接手、恢复与合成交接](references/cli-ops-and-handoff.md)
- [文档契约、历史版本与审计索引](references/sources.md)
