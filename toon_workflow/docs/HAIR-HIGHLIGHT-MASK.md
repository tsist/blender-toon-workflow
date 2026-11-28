# 单一受光头发高光遮罩

0.32新增显式高光遮罩，仍在同一三渲二工作台的HM区域。它把遮罩与当前光照/视向高光合成一个权重，以同一颜色混合一次；不另叠一条固定光带。默认旧图和旧材质不自动改变。

选择已使用HM的头发槽，先读取当前参数，再在“统一高光遮罩”里选独立Non-Color图像与真实UV，连接遮罩。遮罩读取R：黑色无高光，灰色控制过渡，白色允许当前受光高光。黑白位默认0/1；生成图的黑背景有微小灰值时，先实测再显式标定，改变黑白位需移除遮罩后重连。纹理负责形状，光照响应负责显隐和强度；它不是自发光贴花。移除遮罩恢复原HM颜色接线，当前作者参数保持原值。

同一槽只选一个高光来源。启用HM高光时先把旧CEL/HEAD_BAND高光归零；反向开启旧高光也会在写入前拒绝冲突。新增正值高光关键帧同样预检，现有数据仍保护。不会自动删旧光带或替作者清动画；无效手改或未预检的旧冲突应先关闭/移除旧finish，再继续。

遮罩下的有效权重：

`coverage = clamp((mask.R - black_level) / (white_level - black_level), 0, 1)`

`a = clamp(coverage * HM_highlight_strength * half_response * max(N·L,0) * lit_raw * illumination_gate, 0, 1)`

`Color = mix(HM_palette, highlight_color, a)`，再按原合同经过B固定发影的颜色乘法。该颜色只写一次，没有“固定亮带+光照高光”两项相加。B控制固定暗纹，不把B暗纹间的底色亮条误称另一层高光。高光颜色应与基础色协调，作者选择各通道均不低于当前亮色时不会压暗亮区。

R阈值塑形和新R遮罩是不同输入。若希望新遮罩主导轮廓，可将已有R阈值塑形强度明确设0，并用较宽的半角响应调制；不要把阈值图改名当覆盖图。前后发用独立Image ID，即使像素来自同一文件，也不绕过消费者限制。未连接遮罩时原HM v1图和参数保持原义。

作者API：`hair_highlight_mask.attach(context, image, uv_name, black_level=0, white_level=1)`、`remove(context)`；真实描述并入`hair_map.describe(...)["highlight_mask"]`。母UI操作为`toon_hair_map.mask_attach`、`mask_remove`。源Mesh/UV/Image引用、内容和graph都受验证，冻结时另读真实RENDER的遮罩UV，冷验关联实际HM owner/target。HM整体移除也可回原接缝；单将HM强度归零不会撤销更早的独立finish参数修改。

当前仍继承HM的EEVEE、单白Sun、零World和静态能量条件，受光门控是该条件下的近似。实际暗面/遮挡/零光、联合输出、美术与本次具体角色范围以`work/audit/<batch>/`为准；不外推任意半影、多光或Cycles。任意角色先看真实UV/根梢/分段节奏，旧爱音的遮罩位置、颜色和强度不是通用预设。

公式图月牙形状修正另见`work/audit/<batch>/`。本次只替换前后发独立覆盖图，复用0.32原参数和入口，不升级内核或重写源UV。逐束细长弯曲、尖收、错开长短，不默认画成整头横环；具体判断方法归同一技能的`references/reference-style-calibration.md`。生成图仍只读R，实际尺寸/通道差/背景微噪声和黑白位在该批textures/MEASUREMENTS.json，不把提示中的“纯灰/纯黑/1024”当文件事实。

后续用户指出该批实际落点不理想，其位置匹配采用撤回。当前UV纠偏恢复入口为`work/audit/<batch>/`：旧投影下段压扁、原UV镜像共享均有反例，最终仅新增独立ToonHairHighlightUVFit并原生烘焙适配，保全部旧UV。遮罩与固定贴图必须先适配真实UV/映射及表面落点；平面底图、合法绑定或开关图不能代签位置。具体规范集中于同一技能`references/reference-style-calibration.md#遮罩与固定贴图的uv适配`，采用文件和验证边界以新DELIVERY为准。
