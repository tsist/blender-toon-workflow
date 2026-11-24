# 区域着色、风格组件与混合 PBR

这一份参考管材质与大风格怎么搭：从单槽三色核心、明暗羽化、高光与边缘光、游戏与漫画组件，到跨资产混合 PBR 和静态组合配方。所有子命令参数以当次 `blenderctl.ps1 toon-workflow <子命令> --help` 实时回读为准。

## 1. 活动槽基础着色：`surface-slot`

给明确对象上的单个已用材质槽切入三渲二核心，保留未点名槽位与原有外部依赖。

- **三类候选与模式**：用 `operation: apply` 施加 `CEL3`、`GAME_REFLECT` 或 `INK_FLAT`，或显式传入 `settings.mode`（`native_cel`、`eevee_cel3`、`eevee_cel3_reflect`）。
- **三阶色表与照度分工**：`base_color` 送进 Diffuse 产生受光信号，`shadow_color`、`mid_color`、`lit_color` 决定最终可见三档色，`threshold_low < threshold_high` 划定分界。要看到清晰三层，把三色与双阈值一起检查；想缩减为两阶主影时，优先合并暗部与中间色，保住亮面边界。
- **可控明暗过渡宽度**：先调稳三色、阈值与主光，需要柔和影界时再用 `boundary-apply` 或 `boundary-update` 设置 `boundary: {width_low, width_high}`，在照度域做受控过渡。要回到纯硬赛璐璐，用 `boundary-close` 将两宽归零以走原始硬比较分支；改动阈值前先把过宽的过渡收窄或归零，确保两窗口相离且留在有效区间。
- **局部更新与材质库往返**：用 `inspect` 只读核对当前所有权，用 `update` 提交字段补丁，用 `export-library` 和 `import-library` 做静态参数库导出与导入。所有颜色分量保持线性 `[0, 1]` 且 `alpha = 1`。

## 2. 独立高光、边缘光与光带：`finish-slot`

先按部件用途定好基础明暗，再在需要的槽位上叠修饰，棉布与皮肤大面保持干净。

- **命令操作**：用 `toon-workflow finish-slot` 执行 `apply`、`update`、`inspect`、`key`、`clear`、`remove`、`snapshot-sun`。对未绑定槽传入 `expected_owner: null`。
- **避免双亮带打架**：金属或饰品同时存在原生 GGX 反射（`GAME_REFLECT`）与艺术高光（`finish-slot`）时，先指定一个主生产者。用艺术高光主导亮带时把原生反射强度归零，保留单层弱边缘光独立控制。
- **头级参考光带（`HEAD_BAND`）**：指定明确的头部参考对象与可选 Non-Color 图像/UV 遮罩，在局部 X/Z 平面控制高度、半宽、边缘与弧度；光带位置跟随头部参考，正面亮度受光照方向调制。更换模式或参考对象时，先执行 `remove` 再重新 `apply`。
- **光向快照与强度动画**：`snapshot-sun` 读取当前 `Sun` 的世界方向作静态艺术光向，换灯后显式重读更新。用 `key` 在指定帧（支持可选子帧）记录高光与边缘光两条原生强度曲线，用 `clear` 解除曲线并保留旧 Action。

## 3. 游戏、漫画与媒体组件：`component-slot` 与 `shot-look`

把三渲二推进到游戏质感、黑白网点或含透明介质的镜头时，按材质角色分别挂组件。

- **风格与媒体组件**：用 `toon-workflow component-slot` 在具名槽位执行 `apply`、`update`、`inspect`、`import-json`、`export-json`，支持 `GAME`、`MANGA`、`ALPHA_SHEET`、`GLASS`、`WATER`、`SMOKE`、`EMISSION`。施加 `GAME` 时指定明确 `Sun` 名称；替换既有受管表面或组件前，先将原槽恢复干净再施加新风格。开启场景级 EEVEE 透射时，省略 `target` 调用 `enable-transmission`。
- **整镜头漫画分区（`shot-look`）**：用 `toon-workflow shot-look` 的 `read-shot-look` / `edit-shot-look` 设置纸色、墨色、网点排线与 `region_mode`（`UNIFORM` 或 `SEPARATE`），通过有序 `background_edits` 列表增删改背景对象，联合渲染输出独立 `Look.png` 并完整保留原 `Combined` 与 Alpha。开启 `SEPARATE` 分区时，先明确对象的每个已用槽归属前景还是背景。
- **原生媒体与艺术线叠层**：用 `shot-look` 的 `read-native-media` / `edit-native-media` 控制 `enabled`、`line_mode`（`OVERLAY` 或 `EXCLUDE`）、`line_optics` 与 `media_objects` 列表。`media_objects` 列出采线时需要穿过的真实透明或体积对象（如玻璃罩、水体、烟雾）；需要正常遮挡线条的不透明发光体留在常规线源里，避免灯杆穿透灯罩。原生玻璃、水、烟以 Beauty 判断主体观感，艺术线作为显式屏幕叠层叠加，真实物理折射或体积光传输留给常规渲染路线处理。

## 4. 灯光与影形控制器：`lighting-author`

需要把主副光、场景统一调色板和逐材质微调收口在同一条可控链路里时，使用 `toon-workflow lighting-author`。

- **静态与关键帧编排**：在具备活动相机与 EEVEE 场景的工作副本上运行 `inspect` 读取现状，用 `author` 配置 `rig {key, fill}`、可选 `controls`、`keyframes`、`bind`、`eyelid`、`palette`、`scene_palette` 及 `palette_clear`（恢复继承场景调色板）。
- **面部法向联动修复**：对既有受管 `FACE` 目标，用 `repair-face-normal` 按绑定相机修整面部法向；用 `set-face-normal-mix` 传入 `normal_mix {value, mode: STATIC | REPLACE_KEY}` 控制法向混合权重。

## 5. 混合 PBR、新角色原纹理与跨资产复用

实际作品常需要角色三渲二、背景保 PBR，或给自带复杂贴图的新角色做轻量风格化。

- **新角色保留原纹理**：面对未制作专用 SDF 的新角色，先看原生渲染确认实际网格与 UV，沿用原 Base Color 贴图与 UV，以白色 Diffuse 经 Shader to RGB 组织大明暗再乘回原色；断开原高光后再叠单一可控高光。面部起伏被多阶阈值切碎时，减少色阶数量、保住主影界比堆砌小阈值更稳。
- **建筑与环境混合 PBR**：给资产库里的住宅、街道做环境风格化时，先核对 `bpy.data.collections`（已发布集合可能尚未挂进 `Scene`，在工作副本里显式链接即可）。保留墙面、木纹等原生 PBR 与 UV，只对明确窗面、灯罩槽用 `surface-slot` 切到不透明 `CEL3` 并留好原材质恢复入口。
- **衣物道具接入与衣下遮挡**：先区分生产网格与隐藏的供体/重拓扑辅助件；把衣物、道具按已用槽逐槽着色，保护头部 SDF、眼层与头发链原封不动。静态镜头里出现衣下身体穿模时，用可逆原生可见性处理隐藏衣下身体面，并把该步骤排在原生输入最前端——先于 SDF 载体、面部字段与艺术法向，镜头修形保持在修改器栈最末位。
- **静态组合配方（`cel-recipe`）与 `PRESERVE`**：用 `toon-workflow cel-recipe` 的 `new`、`new-composite`、`fork-composite`、`edit`、`bind-slot`、`bind-line`、`remove-binding`、`inspect`、`apply`、`import`、`export`、`read-archive` 组织多槽配方。已有复杂原纹理或头脸专用读者的槽位显式设为 `PRESERVE`；已有复杂 `PRESET` 描边的工程在应用配方时选择保留现有描边，只有干净的 `BASIC` 工程才走配方自动建线。跨角色导入配方 JSON 后，逐一用 `rebind_slots` 绑定新工程的真实对象与材质槽，导出的配方与材质库只承载静态参数，角色绑定与特殊贴图随完整工程副本保存。
- **零槽辅助体与空 VSE 排查**：场景里零材质槽但有面的辅助对象属于合法状态，快照时跳过空槽即可；若源工程的 `render.use_sequencer` 打开且实际零条带导致直渲阻塞，先确认条带数为零，再在工作副本里关闭 `scene.render.use_sequencer` 并独立重开核对。
