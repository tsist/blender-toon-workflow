# CEL 明暗过渡宽度（0.49 开发增量）

在同一个三渲二工作台中，为普通、静态、独占受管 CEL3／CEL3_reflect 材质显式添加明暗过渡。它沿用真实 Diffuse→ShaderToRGB 照度与原三色色表；头脸 SDF、HM、UV、法向和描边另守原合同，不自动迁移。

活动槽表面区有「应用明暗羽化」「更新明暗羽化」「关闭明暗羽化（归零）」。低／高宽度分别控制 shadow→mid、mid→lit 的完整照度区间，单位不是像素、世界长度或灯光角度。

对于阈值 `t` 和正宽度 `w`，在 `[t-w/2,t+w/2]` 内使用线性过渡，两侧保原纯色。宽度为0时使用原 GREATER_THAN 比较器，相等仍归较暗带。窗口须位于 `[0,1]` 且不相交；不自动裁剪或放宽阈值。选宽度时先固定色表、相机和灯光看完整部件及局部，再检查其他已用光向，避免整件糊成渐变。参数例子仅属于当次角色。

组合配方 `toon-cel-recipe.v2` 的 SURFACE 角色可增加：

```json
"boundary": {"width_low": 0.065, "width_high": 0.10}
```

与 `finish` 同级；它不进入旧基础 `settings`。省略时保留已有合法边界；v1、PRESERVE 或不支持的模式拒绝携带。家族沿用组合配方运输此字段，真实对象与槽仍须显式重绑。材质参数库只运输基础 settings，不运输边界。已有窗口与新阈值不相容时，先缩小／归零宽度再改阈值，不隐式临时关闭。

应用顺序为 surface→boundary→finish→Pencil，失败倒序回滚，包含界面最终提交失败。保 Material、core Group、原硬比较器与 Shader socket 身份；新增节点必然改变结构哈希，零宽度画面一致也不等于旧结构相同。整槽恢复仍使用原表面恢复入口；有finish时先明确移除finish。关闭宽度会保留可编辑功能节点。

真实宽度、未链接输入、受管图形和已有finish都经过严格检查；手改、共享core或失去元数据会拒绝。正式联合渲染对存在此功能的槽增加 `toon-band-boundary-freeze.v1` 见证，比较作者、实际RENDER、冻结后的同材质与着色图，并在每次线绘前后再核验；冷inspect检查非空闭合回执。未启用此功能时不新增空报告字段，不改变旧快照或旧配方回执形状。

本机证据与精确包以 `work/audit/<batch>/` 为入口。该增量不单独授予完整GAME家族、全向角色质量、Cycles、边界参数动画或媒体光学资格；0.47默认与两条暂缓分支保持。

做法参考：[MToon shading shift/toony](https://github.com/vrm-c/vrm-specification/blob/master/specification/VRMC_materials_mtoon-1.0/README.md)、[Unity Toon Shader basic shading](https://docs.unity3d.com/Packages/com.unity.toonshader@0.11/manual/Basic.html)。这里采用边界位置和宽度分离原则，不照搬其它引擎参数。
