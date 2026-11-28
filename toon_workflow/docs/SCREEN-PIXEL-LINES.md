# 屏幕像素线宽与可见区间

线宽先在屏幕像素中定义，世界距离只改变几何投影，不改变名义笔宽。PSOFT Pencil+ 4 的 Absolute 固定输出像素；Relative 按 640×480 基准随输出分辨率缩放。相机距离 Size/Alpha Reduction 是独立设置，不把 Relative 解释为世界厚度，也不为了距离一致而一律改 Absolute。

母 Toon 的完整自主作者支持 `toon.line-raster-profiles.v1` 中的 `pixel_box.v1`。覆盖组必须包括完整 Line，并为同一 Line 使用相同滤波；示例：

```json
{"groups":["实际组UID"],"coverage_profile":"pixel_box.v1","feather_px":1.0,"post_filter_sigma":0.0}
```

此配置保留原 Size、RELATIVE、INDEX 宽度图、Alpha、笔画曲线及 DEFAULT/SPECIFIC 共用关系。`feather_px` 必须为 1，表示一个输出像素的固定积分范围；不以修改原图来抵消覆盖错误。简单圆形、无拉伸/随机尺寸/扭曲及有效虚线是当前适用边界；未验笔形明确拒绝。没有新 profile 时走原路径，原 Pencil 后端文件不改。

半径 r、中心距离 d 的横截面为 `clamp(r+0.5-d,0,1)-clamp(0.5-r-d,0,1)`。长直线的 Alpha 等效宽度保持名义宽度，亚像素线随宽度连续变淡，零宽透明。它是自主覆盖近似，不是商业 Pencil 算法等价，也不保证任意斜线、有限圆帽和 taper 的精确二维面积。联合外层 2×2 仍负责空间采样，不重复增加内层采样。

可见性裁切生成的内部端点不应新生圆帽。母适配器根据真实 captured Mesh/source-edge 身份，先把同一边上相接的可见碎段合并为连续范围，再区分原始端点与人工切口：完整原始端点保留圆头；人工切口再应用同一像素范围的纵向覆盖，零区间透明，极短范围连续减弱。所有原图元保留，共享所属连续范围；不能逐碎段积分，否则同层最大Alpha竞争会让密集细分线变淡。只在当前选中层内合并，类别筛选后重新计算，不跨真实隐藏间隙或借其它类别的支撑。相对误差限仅用于当前float64 captured源上的计算衔接；严重病态的源边或越界数据明确拒绝。它不构成任意尺度/极端条件数的精确几何保证。不要按世界距离或像素长度删除短线、关闭类别、改原绘制、锐边或几何。有效虚线需独立 dash 端点来源，当前拒绝。

制作时同时核对名义宽度、Alpha 等效宽度和实际可读性。用同一共享 Mesh、同一输出分辨率、不同深度与亚像素相位比较横截面，再回完整材质的生产机位及白底 Line。分辨率变化、原图/曲线调制与距离变化分开验收；不同空间位置的同一笔刷保持相同像素单位，并不抹掉作者刻意设置的粗细差异。

爱音11证据位于 `work/audit/<batch>//`。原4角色 Line 为 RELATIVE，38个角色笔刷的距离 Size/Alpha Reduction 均关闭。原 18 颜色层、8 Mesh 的原 UV 和锐边由独立保护核验。旧 extent 内收覆盖加 sigma1 令亚像素线过淡；普通圆帽又将裁切塌缩区间画成小点，不能通过再糊或删短线处理。

依据（2026-11-12核读）：[PSOFT Line设置](https://docs.psoft.co.jp/plb400w/en/4.1.3/reference/Line_LineSet.html)、[GPU Gems 2 第22章](https://developer.nvidia.com/gpugems/gpugems2/part-iii-high-quality-rendering/chapter-22-fast-prefiltered-lines)。有限范围运行证据与完整艺术结论分别见本批交付记录，不由配置存在代签通过。
