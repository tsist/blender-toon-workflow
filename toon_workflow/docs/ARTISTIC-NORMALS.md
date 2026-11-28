# 艺术法向（0.15 实验切片）

这是独立的着色控制：不改原始顶点、拓扑或 Mesh 法向，不改变二维线的几何分类。默认强度 **0**。每个目标独立的区域权重、几何节点字段与材质读取节点，可与逐相机镜头修形同时使用。完整 P5 与面部风格仍在开发。

## 使用

注册母插件后，在 3D 视图侧栏「三渲二 → 艺术法向（实验）」操作：

1. 选中本地 Mesh，指定独立参考 Object 和 FLOAT/POINT 区域权重属性。参考的位置定义中心，旋转定义朝向，三个轴缩放定义椭球形状。动画角色可把参考绑定到头骨；只用角色 Object 原点不能代替头部跟随。
2. 「创建全零权重」只建立不存在的属性；共享 Mesh 会先隔离，已有权重不会覆盖。按需要通过原生属性工具制作局部权重。
3. 「创建法向字段」以强度0建立独立 GN，不自动改材质。它放在已有原生变形之后、受管镜头修形之前。
4. 材质须由当前目标独占，且有唯一原生 Diffuse BSDF，其 Normal 尚未连接。「绑定」接入艺术法向读取组；多 Diffuse、共享材质、已有 Normal 连接会明确拒绝。复杂材质可手动使用核心读取组接入所需分支，并自行管理其混合语义。
5. 调整待应用强度后显式应用。该操作只改强度，保留实际参考/权重绑定，拒绝覆盖原生参数动画。可逆解绑只处理 UI 自己创建、没有被手改的精确连接。

Python API不保存文件、不改灯光或原材质：

```python
from toon_workflow_addon import artistic_normals as normal
modifier = normal.build(target, reference, 'artist_region', strength=0.0)
reader = normal.shader_group(modifier)  # 未绑定的 ShaderNodeTree，输出 Normal
normal.configure(target, modifier, strength=0.35)
receipt = normal.validate(target, modifier)
```

`remove(target, modifier)` 保护仍有消费者或已手改的读取组。GN 图是受管结构，作者修改后自动操作拒绝；Shader 读取组允许原生编辑，检查回执标记 `author_edited`，UI不会替作者删除它。没有 handler、自动改灯或全局偏好。

## 空间与渲染语义

Profile为 `toon-artistic-normal.v1`。设目标矩阵A，椭球参考完整仿射矩阵B，原生变形后的局部点p：

`q = B⁻¹ A p`，`n_world = normalize(B_linear⁻ᵀ q)`。

这是真正的椭球梯度；方向不含平移。负缩放不额外乘行列式符号。参考/目标必须有限、可逆，权重必须有限且位于[0,1]；近中心退化、缺字段或错误域会被检查拒绝。每帧 GN 重新计算世界向量、有效强度与诊断三个 POINT 属性。Shader 按区域权重混合原 Geometry.Normal 并归一化，退化混合回原法向。

空间明确为 **WORLD_PRE_CAMERA_CORRECTION**：艺术法向按修形前的作者大形采样；随后镜头修形改变位置，字段随点传播。它不声称是修形后曲面的几何法向，也不承诺保体积、真实阴影或所有强度下的美术稳定性。默认关闭，按镜头选择局部区域。

读取组可接 Diffuse.Normal→ShaderToRGB→色阶，继续使用原生灯光响应；几何轮廓和实际投影仍由几何决定。改变光向、头发遮挡和 Shadow Terminator 设置仍可能显著改变脸部。球化不自动生成设计过的鼻颊分界，面部 SDF 是另一个待开发的分区机制。

## 联合回执与边界

新作业在原 grid/report v3 中增加显式 feature `toon-artistic-normal-freeze.v1`。实际 RENDER 下先验证目标，再比较 evaluated→复制→赋回后求值的原生 corner normals 与三个指定字段的域、类型、数量和内容哈希；不一致拒绝。现有每帧 author/frozen Beauty、Line、elements exact 门保持。旧报告没有该 feature 时不宣称法向字段资格，也不修改旧回执。

世界字段仅用于该帧的 Blender 渲染快照；不是可跨帧复用的导出法向资产。任意 FBX/glTF、Cycles、透明/体积、未见角色、连续动画的风格稳定性及完整面部 SDF 尚未资格化。

本轮证据、实际候选、失败和人工观察统一保存在：
`work/audit/<batch>/`。
