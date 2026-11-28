# 文档契约、历史版本与审计索引

本文件集中存放 Toon Workflow 的工程内设计契约文档链接、Wiki 知识库条目、版本演进脉络与历史审计路径，方便查溯旧作业、旧回执或深入查阅单模块算法细节。当前制作以 `blenderctl.ps1 discover --product toon` 和 `blenderctl.ps1 toon-workflow describe` 实时返回为准。

5.3 制作先读 [当前公共交接](<<PROJECT_ROOT>/docs/cli/workflows/toon-53-public.md>) 与 [商业验收](commercial-acceptance.md)。找可拆工程、三作分镜时间码和面颈方向场工法时，从 [教程库三渲二专题](<<PROJECT_ROOT>/tutorial-library/_INDEX.md>) 进入。 [冻结运行契约](<<PROJECT_ROOT>/runtime/toon-workflow/0.52.3/toon_workflow/toon_workflow_addon/workflow_contract.py>)

## 1. 工程内功能契约文档（`docs/toon-workflow/`）

| 主题 | 文档链接 |
|---|---|
| 二维联合领域合同与源码契约 | [JOINT-DEVELOPMENT.md](<<PROJECT_ROOT>/docs/toon-workflow/JOINT-DEVELOPMENT.md>) · [0.52.3 workflow_contract.py](<<PROJECT_ROOT>/runtime/toon-workflow/0.52.3/toon_workflow/toon_workflow_addon/workflow_contract.py>) |
| GUI 与 CLI 交接规范 | [GUI-CLI-HANDOFF.md](<<PROJECT_ROOT>/docs/toon-workflow/GUI-CLI-HANDOFF.md>) |
| 原生实际时刻与子帧政策 | [NATIVE-TIME.md](<<PROJECT_ROOT>/docs/toon-workflow/NATIVE-TIME.md>) |
| 活动槽表面与明暗边界过渡 | [SURFACE-SLOTS.md](<<PROJECT_ROOT>/docs/toon-workflow/SURFACE-SLOTS.md>) · [CEL-BAND-BOUNDARY.md](<<PROJECT_ROOT>/docs/toon-workflow/CEL-BAND-BOUNDARY.md>) |
| CEL 静态配方、高光修饰与风格家族 | [CEL-RECIPES.md](<<PROJECT_ROOT>/docs/toon-workflow/CEL-RECIPES.md>) · [CEL-FINISH.md](<<PROJECT_ROOT>/docs/toon-workflow/CEL-FINISH.md>) · [STYLE-FAMILIES.md](<<PROJECT_ROOT>/docs/toon-workflow/STYLE-FAMILIES.md>) |
| 三风格组件、漫画 Look 与原生媒体 | [STYLE-COMPONENTS.md](<<PROJECT_ROOT>/docs/toon-workflow/STYLE-COMPONENTS.md>) · [STAGE6-STYLES-MEDIA.md](<<PROJECT_ROOT>/docs/toon-workflow/STAGE6-STYLES-MEDIA.md>) |
| 面部 SDF 标准、纹理、三维光向与关键角 | [FACE-SDF-PRODUCTION-STANDARD.md](<<PROJECT_ROOT>/docs/toon-workflow/FACE-SDF-PRODUCTION-STANDARD.md>) · [FACE-SDF-TEXTURES.md](<<PROJECT_ROOT>/docs/toon-workflow/FACE-SDF-TEXTURES.md>) · [FACE-SDF-3D.md](<<PROJECT_ROOT>/docs/toon-workflow/FACE-SDF-3D.md>) · [SDF-KEY-AUTHORING.md](<<PROJECT_ROOT>/docs/toon-workflow/SDF-KEY-AUTHORING.md>) · [FACE-THRESHOLDS.md](<<PROJECT_ROOT>/docs/toon-workflow/FACE-THRESHOLDS.md>) |
| 整头法向与局部顶点法向 | [HEAD-NORMALS.md](<<PROJECT_ROOT>/docs/toon-workflow/HEAD-NORMALS.md>) · [LOCAL-NORMAL-AUTHORING.md](<<PROJECT_ROOT>/docs/toon-workflow/LOCAL-NORMAL-AUTHORING.md>) · [ARTISTIC-NORMALS.md](<<PROJECT_ROOT>/docs/toon-workflow/ARTISTIC-NORMALS.md>) |
| 眼层、固定发影与头发高光/HM/SDF | [EYE-LAYERS-AND-HAIR-SHADOW.md](<<PROJECT_ROOT>/docs/toon-workflow/EYE-LAYERS-AND-HAIR-SHADOW.md>) · [HAIR-HIGHLIGHT-MASK.md](<<PROJECT_ROOT>/docs/toon-workflow/HAIR-HIGHLIGHT-MASK.md>) · [HAIR-MAP.md](<<PROJECT_ROOT>/docs/toon-workflow/HAIR-MAP.md>) · [HAIR-SDF.md](<<PROJECT_ROOT>/docs/toon-workflow/HAIR-SDF.md>) · [HAIR-SDF-OMNI.md](<<PROJECT_ROOT>/docs/toon-workflow/HAIR-SDF-OMNI.md>) · [HAIR-SDF-PROMPTS.md](<<PROJECT_ROOT>/docs/toon-workflow/HAIR-SDF-PROMPTS.md>) · [PROCEDURAL-HAIR-RESEARCH-20261117.md](<<PROJECT_ROOT>/docs/toon-workflow/PROCEDURAL-HAIR-RESEARCH-20261117.md>) |
| 二维描边、像素线宽、原绘制、线色与模板 | [BASIC-LINE-EDIT.md](<<PROJECT_ROOT>/docs/toon-workflow/BASIC-LINE-EDIT.md>) · [SCREEN-PIXEL-LINES.md](<<PROJECT_ROOT>/docs/toon-workflow/SCREEN-PIXEL-LINES.md>) · [ORIGINAL-PAINTED-LINES.md](<<PROJECT_ROOT>/docs/toon-workflow/ORIGINAL-PAINTED-LINES.md>) · [LINE-COLOR-MATERIALS.md](<<PROJECT_ROOT>/docs/toon-workflow/LINE-COLOR-MATERIALS.md>) · [LINE-TEMPLATES.md](<<PROJECT_ROOT>/docs/toon-workflow/LINE-TEMPLATES.md>) · [SOURCE-SPAN-NUMERICS.md](<<PROJECT_ROOT>/docs/toon-workflow/SOURCE-SPAN-NUMERICS.md>) |
| 逐镜头修形与环绕预览 | [CAMERA-SHOTS.md](<<PROJECT_ROOT>/docs/toon-workflow/CAMERA-SHOTS.md>) · [OMNI-PREVIEW.md](<<PROJECT_ROOT>/docs/toon-workflow/OMNI-PREVIEW.md>) · [OMNI-PREVIEW-ACCEPTANCE.md](<<PROJECT_ROOT>/docs/toon-workflow/OMNI-PREVIEW-ACCEPTANCE.md>) · [SDF-OMNI-RESEARCH-AND-ACCEPTANCE.md](<<PROJECT_ROOT>/docs/toon-workflow/SDF-OMNI-RESEARCH-AND-ACCEPTANCE.md>) |

## 2. 共享 Wiki 知识库条目

查阅细节背景与原理分析时，可通过本地 Wiki 检索以下条目标识：

- 路线与集成决策：`decisions/2026-10-17-blender-toon-plugin-cli-integration.md`、`decisions/2026-10-22-blender-toon-eevee-first-development.md`
- 商业验收与方向场：`concepts/toon-commercial-acceptance-layered-framework.md`、`procedures/blender-toon-temporal-acceptance.md`、`procedures/blender-53-toon-face-shadow-jump-diagnosis.md`、`references/toon-face-shadow-external-workflows-and-direction-data.md`
- 描边与早期机制：`concepts/blender-toon-outline-visibility.md`、`procedures/blender-toon-grouped-line-editing.md`、`procedures/blender-toon-source-u-stroke-styling.md`、`procedures/blender-toon-camera-plane-placement.md`、`procedures/blender-toon-jitter-screen-sampling.md`、`procedures/blender-cycles-sample-subset-diagnostics.md`
- 着色与冷检查：`procedures/blender-toon-eevee-cel3-editing.md`、`procedures/blender-toon-eevee-slot-regions.md`、`procedures/blender-toon-eevee-cold-performance.md`、`procedures/blender-toon-native-parameter-animation.md`
- 角色头部、SDF、眼层与头发：`procedures/toon-face-sdf-production-standard.md`、`procedures/toon-3d-direction-key-face-shadow-prototype.md`、`procedures/toon-artistic-normal-fields-and-frame-freezing.md`、`procedures/toon-head-reference-art-bands-and-persistent-reference-anchors.md`、`procedures/toon-eye-overlays-and-fixed-hair-shadow-with-depth-consistent-line-integration.md`、`procedures/toon-hair-cyclic-sdf-uv-authoring.md`、`procedures/toon-static-cel-recipes-with-explicit-slot-and-line-roles.md`、`references/weytant-hm-hair-map-research-and-toon-integration-gaps.md`

## 3. 版本演进与历史基线摘要

| 阶段 / 版本 | 核心里程碑 | 备注 |
|---|---|---|
| 早期几何描边（0.1–0.10 / manifest 0.1–0.9） | 原生 `screen_ribbon`/`tube_world` 载体、三色 `eevee_cel3`、总反射 `eevee_cel3_reflect`、槽区域 `eevee_regions`、静态参数模板 `template-save/edit/apply`、受管参数动画与受控 `source-u` 抖动 | 2026-10-24 起路线切换为二维独立绘线与联合渲染，旧几何域保持冻结归档，历史回执按原身份查溯 |
| 二维联合早期（0.12–0.14） | 建立 `toon-workflow` 二维联合域、外层共同空间网格采样、完整 `PRESET` 与 `COLOR`/`DEPTH` 元素、取消/前缀恢复与 `camera_shots` 逐镜头修形 | 奠定 `run`/`inspect`/`edit`/`recover` 四操作合同 |
| 角色材质与 SDF 成熟期（0.23–0.49） | 0.23 静态 CEL 配方；0.24–0.25 独立 `finish`、强度动画与 `HEAD_BAND`；0.26–0.28 艺术投影接受层及动画、光带 R 遮罩；0.29–0.30 半透明眼层、固定发影 B 正片叠底与鼻影双 SDF 读者；0.31–0.32 HM 首片与单一受光高光遮罩 R；0.33–0.35 SDF 空间平滑、组合配方与完整线模板；0.35.1–0.35.2 原绘制/锐边恢复与固定像素线宽；0.36–0.39 材质线色（`AUTHOR_RGB`/`NODE_RGBA`/`UV_IMAGE`）、固定发影换图、混合线色模板 v2 与透视区间修复；0.40–0.43 关键光角 PNG16 编译、CEL 家族、头发 SDF（`cyclic_v1`/`omni_v2`）与环绕预览；0.45–0.49 三维关键方向/连续/定向面影、`GAME`/`MANGA`/媒体组件、漫画 Look 与明暗边界宽度 | 形成统一单插件工作台与完整角色三渲二工艺链 |
| 0.50–0.51.1（Blender 5.2.1 LTS 基线） | 三风格 v2、漫画分区与原生媒体艺术线（第 6 阶段）、GUI/CLI 交接与共享提升（第 7 阶段）、跨角色新输入与住宅实战（第 8 阶段）、独立线编辑与组合回执（第 9 阶段）、精确封包交付（第 10 阶段） | 历史入口见 [0.51.1 交付入口](<<PROJECT_ROOT>/deliveries/toon-workflow-0.51.1-20261116/START-HERE.md>) |
| 0.52.1（Windows Blender 5.3.0 基线） | 迁移至 Blender 5.3.0 EEVEE，新增 `lighting-author` 主副光/调色板/影形控制器、眼侧修形、发束亮带与动态 `PRESET` 六帧四层 EXR 实证 | 当前项目唯一支持与验收宿主，交付参考见 [5.3 本地迁移审看](<<PROJECT_ROOT>/deliveries/blender-5.3-migration-20261119/toon-review/README.md>) |
| 0.52.3（Windows Blender 5.3.0 制作基线） | 连续面影、逐灯资格、真实角色转头和室内外迁移、原生媒体四层和真实取消补帧 | [真实作品验收](<work/audit/<batch>/>) · [公共 CLI 回执](<work/audit/<batch>/>) |
| 1.0.0（SWM-12 隔离候选） | Snow 第 5 帧四层与 0.52.3 指纹相同。冻版恢复保留原请求与实现指纹 | [候选验证](<work/audit/<batch>/>) |

## 4. 历史审计与交付路径索引

以下路径均位于工程 `<PROJECT_ROOT>\` 下，按需用于查溯特定批次的对照图、回执与归因记录：

- **交付包与迁移审看**：
  - `deliveries/blender-5.3-migration-20261119/toon-review/README.md`（Blender 5.3.0 / Toon 0.52.1 迁移与审看）
  - `deliveries/toon-workflow-0.51.1-20261116/START-HERE.md`（0.51.1 本地封包与支持表）
- **第 6–10 阶段综合验收**：
  - `work/audit/<batch>/`（三风格、漫画分区与媒体艺术线）
  - `work/audit/<batch>/`（GUI/CLI 交接与共享提升）
  - `work/audit/<batch>/`（新角色与住宅跨输入实战）
  - `work/audit/<batch>/`（独立线编辑、组合回执与恢复）
  - `work/audit/<batch>//`（最终封包验证）
- **面部 SDF、上下光与只读表情样本**：
  - `work/audit/<batch>//`、`toon-sdf-cast-art-integration-20261110-v001/`、`toon-face-expression-validation-20261111-v001/`
  - `work/audit/<batch>//`、`toon-nose-material-line-20261111-v001/`、`toon-face-boundary-20261111-v001/`
  - `work/audit/<batch>//`、`toon-sdf-scalar-filter-20261112-v001/`、`toon-face-sdf-clean-contours-20261112-v001/`
  - `work/audit/<batch>//`、`toon-face-real-light-20261114-v001/`、`toon-face-upperback-20261114-v001/`
  - `work/audit/<batch>//`、`toon-selected-authors-formal-20261115-v001/`、`toon-frontback-domain-20261115-v001/`
- **头发高光、眼层、HM 与风格校准**：
  - `work/audit/<batch>//`、`toon-eye-hierarchy-20261111-v001/`、`toon-iris-source-style-20261113-v001/`
  - `work/audit/<batch>//`、`toon-hm-boundary-20261111-v001/`、`toon-highlight-fusion-20261111-v001/`
  - `work/audit/<batch>//`、`toon-back-g-and-highlight-methods-20261112-v001/`、`toon-fixed-shadow-art-20261113-v001/`
  - `work/audit/<batch>//`、`toon-omni-sdf-preview-20261113-v001/`
- **描边、线色、环境复用、修形与风格组件**：
  - `work/audit/<batch>//`、`toon-material-line-color-20261112-v001/`、`toon-line-uv-image-20261112-v001/`
  - `work/audit/<batch>//`、`toon-line-ink-template-20261113-v001/`、`toon-real-scene-reuse-20261113-v001/`
  - `work/audit/<batch>//`、`toon-style-family-20261113-v001/`、`toon-palette-ink-20261114-v001/`
  - `work/audit/<batch>//`、`toon-p5-completion-20261115-v001/`、`toon-game-family-20261115-v001/`、`toon-game-foundation-20261116-v001/`、`toon-band-boundary-20261116-v001/`
  - `work/audit/<batch>//`、`toon-r3-stage-completion-20261109-v001/`
- **早期几何冻结与过渡归档**：
  - `work/audit/<batch>//`、`toon-jitter-development-20261023-v001/`、`toon-animation-integration-20261023-v001/`、`toon-eevee-regions-20261023-v001/`、`toon-eevee-layered-20261023-v001/`、`toon-eevee-cel3-20261022-v001/`、`toon-eevee-cold-performance-20261022-v001/`、`toon-p4-3-development-20261022-v001/`、`toon-p4-2-development-20261021-v001/`、`toon-p3-development-20261019-v001/`
