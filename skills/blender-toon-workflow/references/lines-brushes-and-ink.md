# 二维描边、笔刷、线色与线模板

这一份参考管二维描边从组线、调笔刷、保原绘制、配材质线色，到线模板迁移、组合回执收尾和多层 EXR/矢量线稿输出的全流程。

## 1. 先查模式再编辑：`BASIC` 与 `PRESET`

动手改线前先做只读检查，看清当前工程处于 `BASIC` 还是 `PRESET`，顺着对应通道改值，保留同场其他线组与动画。

- **`BASIC` 整体静态线宽**：用 `toon-workflow inspect` 取得源 `author_sha256` 和当前像素线宽，写一份 `toon-basic-line-width-edit.v1` JSON（含 `schema` 与 `line_width`，绝对像素宽 `0.1..32`），通过 `toon-workflow edit` 写出新候选。想加粗 10% 时把原像素线宽乘以 `1.1`，保持 `BASIC` 模式原样。
- **`PRESET` 完整线组与选择器**：用 `toon-workflow viewport-lines` 执行 `inspect` 与 `edit`，每次修改绑定源 `.blend` SHA256、`edition`、`context {scene, view_layer, frame}`、`scope` 与 `expected_author_sha256`，输出全新 `.blend` 候选并独立冷重开核验。
- **线层级与组生命周期**：
  - 用 `inspect-line-hierarchy` / `manage-line-hierarchy` 管理独立 Line（`new`、`set`、`connect`、`move`、`socket`、`shrink`、`remove`、`merge_group`、`merge_collection`）及像素尺寸模式、抗锯齿、过采样、出画距离与 `elements_only`。新增线组默认挂在第一条 Line 上，需要独立像素覆盖或滤波参数时，将其接到独立 Line 并保持原有 Line 顺序。
  - 用 `inspect-line-groups` / `manage-line-groups` 执行 `NEW`、`ARCHIVE`、`RESTORE`、`SET_OUTPUT` 与 `SELECT_EDITOR`。
  - 用 `inspect-edge-sources` / `edit-edge-sources` 稀疏修改 `VISIBLE`/`HIDDEN` 通道的边类别路由、交线补丁（`intersection_patches`）与旧线框配置（`wire_patches`）。
  - 用 `inspect-line-visibility` / `edit-line-visibility` 设置材质线功能绘制/遮罩目标与自交可见性；用 `inspect-parameter-tracks` / `manage-parameter-tracks` 对场景级线参数打关键帧（`KEY`）、移除（`REMOVE`）或开关（`TOGGLE`）。

## 2. 笔刷细节与参数控制：`brush-settings`

调整具体笔刷的粗细、形态、虚线、接链和衰减映射，用 `toon-workflow brush-settings`（保留同场其他笔刷与曲线动画）。

- **读与改**：用 `inspect` 列出具名笔刷与 `actual` ID，用 `update` 提交点号路径补丁（例如 `shape.stretch`、`maps.size.uv_name`）；需要直接调文本草稿时用 `open-draft`，导出原生样式 JSON 用 `export-style`。
- **接链、虚线与映射控制**：用 `inspect-controls` 取得 `expected_controls_sha256`，再用 `apply-controls` 执行 `PREPARE_CHAINS`/`SET_CHAIN`（设置 `split_angle_degrees`、`line_direction_degrees` 及独立开关）、`PREPARE_DASHES`/`SET_DASH`（先把模式切到 `DASHED` 再调 `dash_length_px` 与 `gap_length_px`）、`PREPARE_MAP_AMOUNTS`/`SET_MAP_AMOUNT` 及 `KEY_AMOUNT`/`REMOVE_AMOUNT_KEY`。

## 3. 屏幕固定像素线宽、原绘制与五官短线

保住动画质感的关键在于：远近粗细受控、原模型手绘控制不丢、五官细节在正常尺寸下清晰可读。

- **固定像素宽与防碎点**：同一物体远近保持一致像素宽时选绝对像素单位；`RELATIVE` 随输出分辨率缩放，与距离衰减分属两条控制。母插件像素覆盖层会先合并同一原始边的相接可见碎段再做亚像素横截面处理，保住真实短边圆头的同时消除密集碎段产生的毛刺小点。
- **保留原始顶点绘制与锐边**：出现新旧描边不符时，逐对象核对有序颜色属性层的 `INDEX`、名称、域（`POINT`/`CORNER`）与数值，并核对锐边（Mark Sharp）、平滑面、Freestyle 标记与自定义角点法向。恢复原样式时保留默认笔刷共享与按 Line 覆盖设置，原缺失 `INDEX` 的回落行为保持原样。
- **鼻嘴五官短线恢复**：减掉多余碎线时守住正常展示尺寸的可读下限——闭口姿态下需清楚看见短鼻线、独立鼻影与完整嘴线。
  1. 局部 alpha 图负责减线；要补回缺失的鼻嘴短线，先在原网格表面定位明确边链，打上 `BOOLEAN/EDGE` 的 `freestyle_edge` 标记（避开覆盖他人已有标记），再建互斥的自主线组并启用 `VISIBLE` 选中边专用笔刷，默认关闭该组的 alpha/width map。
  2. 新短线补回后若旧唇线有错位残留形成双线，只在明确的前唇 `CORNER` R 属性上抑制旧贡献，保护 GBA 通道与后颈等非目标区，形成单一干净轮廓。
  3. 排查局部二维线采样问题时，区分原始 `polygon` 与实际 `RENDER` `triangle`、`triangle_supports` 及 `CORNER` loop 索引；对零 UV 支持面优先按空间坐标与面连通性核对选区，避开屏幕投影误伤后颈。

## 4. 材质线色协作

让皮肤、头发、睫毛与衣料各自拥有协调的深色墨线，比全角色纯黑描边更贴近商业番剧质感。

- **编辑策略**：用 `viewport-lines` 的 `inspect-line-material` 与 `edit-line-material` 对明确已用槽 `target {object, slot, material}` 执行 `ADD` / `UPDATE` / `REMOVE`。
- **三类线色来源**：
  - `AUTHOR_RGB`：作者固定线性色，独立于材质色表；更新已有受管材质颜色时，全量 `AUTHOR_RGB` 策略可顺滑保留。
  - `NODE_RGBA`：绑定到材质节点树里明确节点与输出插槽的常量颜色；重绑 SDF 或更换材质节点后，显式把策略迁到新插槽并复核 RGB。
  - `UV_IMAGE`：按实际 `RENDER` 支撑三角形的 `CORNER` UV、显式镜像与仿射矩阵逐片元采样贴图（例如左右眼镜像各自单独标定），保持原 Line Alpha 覆盖不变；换用新贴图（如重绘虹膜图）时同步更新材质节点与线色策略两边引用。
- **GPU 阶段与色彩校验**：材质线色在 GPU 的笔刷 Color Map 与 HSV 之后、眼层与 `DEPTH` 之前生效，用 `amount`、`gain`、`tint` 控明暗与染色；验证时同时检查白底 `Line` 与完整 `Combined`，核对暗发束与鼻嘴细线的对比度。

## 5. 完整线模板迁移与组合回执收尾

把已打磨好的角色或环境描边规则复用到新资产，按规范重绑对象、集合与材质角色。

- **捕获与套用线模板**：用 `viewport-lines` 的 `capture-line-template` 导出静态模板 JSON，再用 `inspect-line-template` 预检冲突与线色绑定，最后用 `apply-line-template` 显式重绑全部对象/材质/集合角色并产出新候选，需归档旧记录时运行 `reset-line-template`。
- **资产环境复用**：给建筑或场景集合复用模板时，按语义材质（屋顶、结构、墙面、窗口）和必要 `Collection` 角色分组绑定，确保根集合与窗口集合互斥，避免双重出线或板缝亮线拼出 CAD 格网。
- **独立改线后的家族/CEL 组合回执收尾**：如果工程里既有家族或 `CEL` 组合回执记录了旧线作者，直接跑独立线编辑并冷重开时会触发组合回执校验拦截。按以下四步平稳收尾：
  1. 读清子 `inspect` 报错确认冲突来自旧组合回执，保留原源与报错记录。
  2. 在工作副本用 `viewport-lines edit-style-family` 的 `ARCHIVE` 留存旧家族回执。
  3. 针对新副本重取源 SHA 与 `author_sha256`，执行独立的描边修改。
  4. 用 `edit-style-family` 的 `NEW_V2`（或 `NEW`）与 `APPLY` 建立并应用匹配现状的新组合基线，保存后独立冷重开核对两层 `inspect` 均呈一致状态。

## 6. 多层 EXR 线稿与矢量输出

- **管理渲染元素与相机线稿输出**：用 `viewport-lines` 的 `inspect-elements` / `manage-elements` 维护 `COLOR` 与 `DEPTH` 线通道（`ADD`/`COPY`/`UPDATE`/`REMOVE`/`UP`/`DOWN`/`SYNC_GROUPS`/`SELECT`）与显存预算（`element_budget_mb`）。用 `inspect-file-output` / `edit-file-output` / `render-file-output` 输出单帧或序列 PNG / 多层 EXR，或用 `render-camera`（支持 `saved_elements=true` 与 `saved_sequence=true`）按已保存相机直接出片供合成部门接线。
- **导出平面矢量线稿**：需要 `EPS` 或 `AIEPS` 矢量线稿供平面排版或后期矢量加工时，用 `toon-workflow vector-output` 先运行 `inspect` 取 `expected_vector_sha256`，再按 `VIEW_LAYER`、`SCENE_LEGACY` 或 `IMAGE_NODE` 作用域执行 `edit`（`ADD`/`SELECT`/`UPDATE`/`REMOVE`/`UP`/`DOWN`）与 `export`。
