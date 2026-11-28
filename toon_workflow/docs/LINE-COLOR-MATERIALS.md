# 描边颜色与材质协作

2026-11-13：0.35.2研究、0.36静态材质线色之后，0.37新增明确UV图像来源。实际验收以本批DELIVERY为准；线宽方法继续由[屏幕像素线宽](SCREEN-PIXEL-LINES.md)维护。

## 0.37 指定UV图像来源

统一工作台的基色来源可选“指定UV贴图”：明确现有已保存/打包的图像、UV层名、缩放/镜像与偏移、过滤和边界方式。必须核对实际材质消费的UV与映射；本轮右眼现有材质是u'=1-u，不能把两眼都按默认UV直接读取。完整作者JSON的UV_IMAGE source包含image、uv_name、uv_transform（2×3仿射展开6值）、filter、extension与support=LOWEST_MATCHING_TRIANGLE；采用toon.line-material-colors.v2，旧v1保持原义。界面只编辑缩放/偏移，旋转或剪切矩阵在完整JSON中编辑，不能读取后静默丢掉非对角值。

UV来自实际RENDER支撑三角形的CORNER层，不改原UV，也不从最终Beauty或背景取色。CPU按真实世界三角形预计算world→UV仿射平面，GPU从原source point逐片元求UV并采图；源点仍用既有透视修正sourceT，不把端点颜色插值冒充纹理采样。接缝明确选本策略材质匹配的最低捕获triangle tag；非流形中间支撑未新增，不宣称该规则就是视觉前景。一个primitive只用首条匹配策略，静态色与贴图色互斥；target=sample_linear_rgb*gain*tint，amount仍与覆盖Alpha独立。

仅支持单张静态、干净的已保存/打包RGBA FILE图，最长边2048；脏图/未保存生成图须先保存或打包。忽略图像Alpha，保原Line的Alpha、宽度及可见性。8位sRGB的bpy Image.pixels是归一化原始通道，本版本先转线性Rec.709再采样；Linear Rec.709/Non-Color按明确数值读取，浮点sRGB及Premul来源拒绝，避免重复转换或透明色解释不明。不是任意材质图求值，也不读光照结果。旧brush的图像解释不在此次修改范围。

一张私有RGBA32F表携带图像、支撑目录和UV平面，新增一个toonInkUV sampler，不新增primitive属性或转换world/source-u。纹理采用texelFetch手动最近点/双线性与EXTEND/REPEAT（含负UV及镜像变换），避免atlas跨图/跨行bleed；暂不提供mip/各向异性过滤。最终布局为原7个采样器→toonInkUV→可选managedEyeTriangles，严格按名字核对。颜色仍在GPU旧Color Map/HSV之后、Eye/DEPTH之前混入。

表内地址保持float32精确整数范围，总表上限128 MiB，仅为本表的准入，不是全作业内存保证。世界到UV的float32存储、原source point量化及保守dot/插值误差以图像texel计，超过四分之一texel的数值范围拒绝；退化world三角形拒绝。此限制应连同实际GPU/目标图像验收，不能把有限系数或float64 SVD通过当作GPU精度证明。大世界原点加微小三角形是已捕获反例，不能放宽源区间容差掩盖。

来源resolved.v2核像素及颜色解释，实际RENDER UV/世界三角/tag/仿射表另存冻结摘要，各外采样再核对；实际存在UV_IMAGE时强制report.v6/eevee_joint_grid.uv-ink.v1及UV回执。缺回执或把带UV策略结果降为v5应拒绝；旧v1–v5无UV报告保原身份。图像只存名字而不增加Image指针消费者，提交作者时保fake-user以避免未被shader引用的图像丢失；外部图仍须公共资源闭合。跨工程新引用运输仍未提供，描边模板拒绝，当前成果以完整blend保留。

本轮实际输入是既有爱音灰蓝瞳孔图，左右原UV与明确镜像分别绑定，不重画或改原贴图/UV。四采样联合输出、实际看图与限定采用由toon-line-uv-image-20261112-v001/DELIVERY.json记录；技术首片不等于全部公式图匹配、受光联动或即时视口已完成。

## 0.36 显式材质线色

统一工作台→“材质与描边线色”：选择网格的真实已用材质槽，读取活动槽，选择作者颜色或指定常量颜色节点，设置独立混色量、基色明度系数及乘色，再应用。操作仅向新的描边作者Text写入本扩展，旧作者保留。原Alpha、像素线宽、世界坐标和源区间不变。移除最后一条方案后字段消失。共享材质会影响所有使用该材质的目标；需要进一步限定时显式指定线组ID和边类别。

同材质可新增多个有序方案，界面逐条列出并可明确读取/编辑/移除。每条界面支持全部或一个线组，多组列表保留完整JSON编辑；相同材质不同组或类别不是自动合并。读取后若所选策略被其它编辑改变，应用会拒绝覆盖，须重新读取。界面只保存名字与会话身份，不增加材质ID用户，保持表面工作流的消费者保护。

`AUTHOR_RGB`使用线性RGB作者常量；`NODE_RGBA`明确指定材质根节点树的节点名、INPUT/OUTPUT和实际socket.identifier。支持RGB节点输出，以及Principled、Emission、Group的未连接RGBA输入；不按名称猜“Base”，不读取显示色，不遍历任意材质图。链接输入、动画节点树、失联/非本地材质和未用槽拒绝；组内贴图、纹理节点、任意图求值和受光色不在此版本范围。指定常量不必等于最终受光基色，作者需核清其语义。

母作者request的`toon_line_material_colors`为`toon.line-material-colors.v1`，`policies`有序，每条字段：`id`、`material:{name,library:null}`、`groups`（空为全部）、`categories`、`source`、`amount`、`gain`、`tint`。`source`为`{kind:AUTHOR_RGB,color:[r,g,b]}`或`{kind:NODE_RGBA,node:…,direction:INPUT|OUTPUT,socket:…}`。所有RGB、amount/gain/tint范围0–1；`target=source_rgb*gain*tint`，`rgb=mix(existing_rgb,target,amount)`，在原端点插值与GPU颜色图处理后逐片元混合。缺字段或全部amount=0保持原像素输出。

新阶段在原笔刷、Line Functions、颜色图/RGB/HSV处理之后、Eye与DEPTH之前。它是可选的最后RGB调色，不更改旧处理顺序。通过真实support triangles的材质身份匹配；多材质接触边采用“有序方案中首个实际匹配且amount>0的方案”，不冒充视觉前景选择。不要用最终Beauty盲采代替表面身份。该实现按连续相同策略批次设置独立颜色uniform，在authoredBrushColor之后混合；不新增GPU采样器、属性数组，不触碰world/source-u。元素筛选后按其实际支撑重新选色，同层最大Alpha及图元顺序保持。

公共联合渲染在作者/冻结参考及各外采样位置核对同一已解析来源与参数；报告保存明确源RGB、目标RGB、策略摘要与签名，独立inspect核结构及一致性。有本扩展时采用toon-joint-report.v5 / eevee_joint_grid.material-ink.v1，缺新回执拒绝；旧v1–v4无字段报告保原合同，旧读者不会误签新特性。收据仅证明静态来源与处理合同，画面仍须实际查看。改描边会使旧CEL/模板回执失效，保留旧记录后用现有组合配方重新绑定/应用；不得改旧回执冒充原样。带本扩展的CEL观察还记录当前已解析节点RGB与类型；修改节点常量后旧CEL回执应报告变更。

CEL新建／替换被线色方案绑定的材质，预检会在副作用前拒绝。0.44.1 对已有受管材质的同模式参数更新按真实依赖判断：该材质全部相关策略为 `AUTHOR_RGB` 时允许更新，独立固定线色及原策略完全保留；任一 `NODE_RGBA`／`UV_IMAGE` 来源仍拒绝，须显式处理来源后重绑。不能只看首条有效策略，也不因 amount=0 放松保护。`PRESERVE` 和真正 `unchanged` 保持原合同，材质身份／所有权／手改保护不变。此规则由 CEL 公共核心预检执行，家族、GUI 与程序入口一致；保固定线色改表面不等于自动从新表面取色。

本扩展仅在母联合渲染生效，旧即时视口仍待同步。静态跨工程描边模板暂拒携带这些新材质/节点引用，使用完整作者blend保留当前成果；不能让旧repair_names按旧名字悄悄运输新引用。图像取色下一步必须明确真实UV、源表面归属与运输合同，再验Pixel+Eye组合。

## 分清颜色混合与覆盖

Brush `Blend Amount`控制不透明度；Line Functions 的`color/amount`在现有墨色与指定颜色之间插值。两者含义不同。保持像素宽度和Alpha，改变RGB，才能检验颜色协作；不要靠整体降Alpha使轮廓和必要短线再次消失。线色、材质基色和最终受光色也不是同一种数据。

本轮直接取得MyGO官方爱音页的`body_anon.png`：1150×1741、RGBA、506768B，保原始字节。已看整体和头部、发衣、手部裁片。头发线偏暗粉紫，皮肤/衣料轮廓偏冷灰蓝，睫毛偏棕；这是栅格观察，不是官方shader参数或统一精确色卡。图中发色、阴影、抗锯齿、Alpha混合会影响取样；不能把取到的最终RGB直接当未混合墨色。与此前Ave Mujica指南书分别保留来源。不同表情/衣装/机位不用于形状或逐像素相似率。

## 当前可复用能力

| 方式 | 当前能力 | 边界 |
|---|---|---|
| 按材质/边类别指定线色与混合量 | `line_functions`材质容器；`rgb=(1-amount)*brush+amount*target` | 手动作者目标色；不读取材质Base Color。多支撑时当前按最低三角形ID中首个有效规则选色，不保证等于视觉前景材质 |
| 显式颜色属性或图像 | 现有brush color map；支持POINT/CORNER颜色属性及指定UV/SCREEN/WORLD/STROKE | 必须显式绑定实际图像/属性/UV，非任意材质节点求值。该图会在材质换色之后参与颜色处理 |
| 自动读取材质基色/最终受光色 | 尚无通用入口 | `Material.diffuse_color`是显示色，不自动等于节点基色。Beauty是成图，不是可靠的源表面颜色查询 |

当前顺序为笔刷基础色→材质/边类别换色→颜色图与RGB/HSV调整→有限Eye传递→DEPTH专用灰度。不要静默重排已有作者。若将来需要最终强制覆盖，需明确新优先级合同；不能一边把颜色图放在后面、一边声称材质覆盖优先级最高。

颜色图与pixel_box的组合尚需受影响验证：`brush_engine.py`的样式展开可把world转成float32，而pixel_box人工切口使用captured源上的精度约束。现有Color Map能力不等于这条组合已经通过；优先保原world/source-u或采用独立颜色阶段，不用放宽切口容差掩盖精度变化。

## 采用的研究方向

先以部件角色维护墨色主次，再可选引入材质基色调制。建议把颜色源、混合量、暗化/色相设计与光色权重分开；默认零影响、显式材质/已用槽与来源，不遍历任意shader猜基色。肤线的冷灰蓝、睫毛的棕色说明“所有线统一乘基色”不足以还原这张参考。

可研究的颜色关系是`Cline = mix(Cink_role, T(Cbase_surface), beta)`，在线性RGB完成；T为明确的艺术暗化/色相设计。它是方案，不是现行新API；beta与原描边Alpha独立。优先先做显式作者常量/已知Base节点，再验证明确UV纹理；受光色属于下一步，需同外采样、源表面身份与未重复Eye处理的读者。

禁止直接在最终Combined上重新染线，或在外轮廓屏幕位置盲采Beauty：那里可能是背景、后发或另一材质。应从同一样本的真实支撑表面/材质/UV取得颜色，在Line累积和Eye传递前处理。保持四关联`Combined=Line+Beauty-Interaction`；不重复将Eye已染过的Beauty再次送入managedEye。多材质接触边需明确来源选择策略，不能把最低triangle ID当美术规则。

## 本机对照与采用边界

`work/audit/<batch>//`包含原图、来源、研究brief与两个单中心实际GPU候选。两候选只新增Line Function RGB作者，原请求其它字段、scene_controls、Source文件及RENDER几何/相机保持；Line Alpha对基线逐像素exact，RGB确实改变。Beauty沿用匹配的已验中心底图，不是本批新做的四采样正式联合输出。

全量取样色换入会让当前暗部发束线对比不足，故不作全局默认。保留原墨色、按部件混入色彩后，发束分隔和整体色系更协调，作为下一步设计方向；仍需按外轮廓/内线、亮/影分别设计，不能将本例beta或RGB推广成官方值。两候选均为研究，不覆盖已采用0.35.2工程，不声称自动随材质/光照联动、正式发布或完整公式图匹配。

## 来源

- [MyGO官方爱音页](https://anime.bang-dream.com/mygo/character/anon/)：参考图与实际观察。
- [PSOFT Brush Settings](https://docs.psoft.co.jp/plb400w/en/latest/reference/Line_BrushSetting.html)：Blend Amount、Color Map与线宽分别控制。
- [PSOFT Material Line Functions 4.1.3](https://docs.psoft.co.jp/plb400w/en/4.1.3/reference/Material_Line_Functions.html)：按材质/边类别换色。
- [Unity Toon Shader Outline](https://docs.unity3d.com/Packages/com.unity.toonshader@0.9/manual/Outline.html)：基色/线色协调的设计参考；已读页面标0.9.7-preview。
- Unity官方仓库commit`a04621d8ea682de3a0ae760c12e150d76430992d`、0.16.1-preview，`BuiltIn/UCTS_Outline.cginc`：基色纹理调制和独立光色开关的实现参照。其Unity Companion License限Unity-dependent项目，本任务只比较设计，未复制代码或添加依赖；不引入其反壳几何/距离线宽方案。
