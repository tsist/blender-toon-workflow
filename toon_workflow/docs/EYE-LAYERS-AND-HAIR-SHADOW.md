# 眼部叠底、高光小片与固定发影

Toon 0.29 开发增量，目标环境 Blender 5.2.1 / 9e2066aef7ef、EEVEE。单一三渲二工作台的“眼部叠底、高光与固定发影”区。限定运行资格从本批 DELIVERY 读取，本文不代表完整母插件已经完成。

## 眼层

只在确认的眼影槽或高光小片槽创建独立材质。创建按钮替换当前已使用槽，不自动识别或拆分模型；操作前确认槽所包含的面。现有皮肤、虹膜材质保留。阴影使用彩色透射 T=1-opacity*(1-tint)，保留后方眼白与虹膜；高光使用已有 UV 的椭圆 mask，中心为亮色，边缘 smoothstep 渐隐，无需为常量和数学羽化生成位图。

眼影颜色、高光颜色均是线性 RGB，透明度与柔边可通过读取/更新按钮修改。GLINT 的 UV 名称、中心和半径须按实际小片 UV 校准，本案例数值不作为其它角色默认美术批准。三种小片可共享 UV 区域；每条射线对同材质仅取最近一层，前后折叠或叠两片同材质不在资格内。

独立API：eye_overlay_nodes.create / validate / update；eye_overlays.configure(scene,[{object:obj,slot:n}]) 显式登记已分配槽。未登记透明材质继续由原 opaque 入口拒绝，不能仅靠名字或手加标签放行。

联合眼层输出采用 toon-joint-report.v4 / eevee_joint_grid.eye.v1。EEVEE 先计算不透明接收面，眼层按实际射线距离合成；线片元只受到位于该线前方眼层的 E+T*rgb，前方睫毛保持。眼层自身边线和不透明遮挡被排除，其它遮挡保持。RGBA Alpha 与原线覆盖语义保持，COLOR 元素含眼层叠色，DEPTH 仍是原作者轴向编码。四关联数组的重建公式不变。原生 BLENDED 材质预览与复杂重叠排序分别判断，不把普通预览当联合排序证明。

最多 8 个声明材质、16384 个求值眼层三角形（当前角色实际 3456；未签满上界性能）；不支持通用玻璃、折射、体积、透明接收面、折叠同材质多层。原有实例/库覆盖、景深/运动模糊限制不放宽。保存的作者工程保留全部几何/UV/绑定与既有数据。

## 固定发影

参考爱音11的原节点图，固定阴影取旧遮罩 Blue；Blue=1 强制进入暗部，Blue=0 保持受光。旧 v1 层插在 ShaderToRGB Color → ColorRamp Fac，Light'=Light*(1-strength*B)，只保留历史读取/更新/移除。当前新 v2 层插在 ColorRamp Color → Emission Color，在打光色之后正片叠底：C_out=C_dynamic*(1-strength*B*(1-tint))。两层阴影重叠时继续相乘压暗；强度1、遮罩白时为 C_dynamic*tint。不移植旧整组，不替换 ART 法向、原色表或已有光带 Red 遮罩。当前旧图按原 sRGB 语义使用；擅改 Non-Color 会改变灰度。

在活动头发槽选择对应前/后发旧遮罩与明确 UV，添加后强度默认 0；显式提高后启用。提供读取、更新、移除，移除恢复原连接。固定发影只控制基础色阶；高光光带仍是独立艺术层，不把固定阴影自动等同于高光抑制。

API：hair_fixed_shadow.attach / validate / update_strength / update / remove。新建默认v2，tint为线性RGB；旧v1不自动迁移，显式移除再新建。材质独占、图像源与实际像素、UV、图结构、绑定和强度都受校验，手改冲突明确拒绝，不重签掩盖。RENDER 捕获核图像/参数与 UV 的冻结一致性。

## 本批恢复

work/audit/<batch>/ 是有效取证；旧 invalid-graph-keys 与失败日志保留。AUTHORS.json 记录三采用父源与新工作副本。最终包、公共终态、冷开与预览由 DELIVERY.json 绑定。

研究：Colorful Palette 的 Project Sekai 眼部透明开发分享 https://media.colorfulpalette.co.jp/n/n927a776b6b35 ，仅借鉴深度与眼层顺序必须一致的原则，未复制 Unity 实现。Blender 本机实际透明探针区分 BLENDED 彩色透射与 DITHERED 灰度透射。

源边排除同时覆盖 BASIC、旧分类1–4、wireframe来源6和显式edge route7；交叉线来源5另按其原身份处理。

## 0.38 固定发影换图

同一面板“替换固定影图像 / UV”只更换当前v2或HM v3的B源，不改变固定影owner、HM、R月牙遮罩、强度/tint及外部连线。先读取当前层，再选择独立干净FILE/packed图与目标已有UV，最后替换。旧图保fake-user以便保存重开后恢复；不改任何图像像素。失败恢复图像/UV/声明/签名及保活标记。legacy v1不自动迁移，原更新按钮继续只改常量。

API为hair_fixed_shadow.replace_source(material,image=...,uv_name=...)；目标对象/材质消费者及UV层由调用者预检，UI有实际检查。与当前HM图或R高光图同ID的来源拒绝，以免破坏其独占读者。新图颜色空间在替换前确定并完整记录；旧sRGB图不擅改Non-Color，新的数值遮罩可独立用Non-Color，须实际看图验证。仍为Blue、Linear/REPEAT/FLAT与原乘法公式；图像和真实RENDER UV已进入既有冻结见证。

本例纹理清理与参数对照从work/audit/<batch>/恢复。生图即使声明保UV也不保证像素配准，必须检查原岛边界与实际模型。有限艺术候选不代表其它角色或完整风格验收。
