# 原始绘制控制与按Line覆盖

0.35.1在母Toon联合绘线入口加入按完整Line限定的覆盖/滤波设置。内置Pencil0.2.27的120文件不改，无此设置时保持原绘制路径。原有INDEX宽度图和曲线与覆盖单位需共同恢复，不能只抄size或全局减淡。

先逐对象比较原始Mesh的拓扑、有序颜色层INDEX/名称/域/类型和完整值，再核锐边、平滑面、Freestyle、自定义角点法向和原UV。POINT与CORNER不能平均替代；原来缺失的INDEX也有意义，不补白层。新层追加在原前缀之后不自动代表丢绘制。

锐边指Mark Sharp的逐边标记（`sharp_edge`/`use_edge_sharp`），应对原边ID完整比较。它与折痕权重`crease_edge`/`crease_vert`、Freestyle标记及平滑面是不同数据，必须分列；不能以折痕一致代替锐边一致。

然后核实际route、对象/材质选择、DEFAULT/SPECIFIC共用关系、Blend、RELATIVE单位及沿笔画曲线。旧captured转换只复制已有执行投影，不自动补齐原四Line、object类及weld。多类别原本回落同一笔刷时应共享actual，不能拆成多层over。眉毛可能是躯干上的材质区域，改名后先核原槽和实际面集合，再显式重绑。头发weld在本产品是自主精确世界坐标链策略，不宣称商业算法等价。

完整自主作者JSON的`document.request.toon_line_raster_profiles`由母Toon解释：

```json
{
  "schema": "toon.line-raster-profiles.v1",
  "profiles": [{
    "groups": ["实际自主线组UID"],
    "coverage_profile": "extent_falloff.v1",
    "feather_px": 0.0,
    "post_filter_sigma": 1.0
  }]
}
```

每个涉及的Line需全部覆盖，并使用同一滤波sigma；范围外Line保留原设置。组需真实存在且不重复。旧`round_capsule.v1`/`extent_falloff.v1`的feather为0–16，sigma为0–4。0.35.2新增固定1px footprint的`pixel_box.v1`，用于保留亚像素线及正确截断人工可见区间，具体边界见[屏幕像素线宽](SCREEN-PIXEL-LINES.md)。上例extent+sigma1为历史配置，不作为恢复固定像素观感的推荐。作者Text参与原指纹和公共run/inspect；报告`author_style.raster_profiles`记录实际采用。空间采样仍由联合外层网格控制，不等同原Pencil AA/oversampling。

白底图使用线性预乘RGB加`1-alpha`后编码Standard sRGB。检查报告中的实际有效camera/投影/帧/RENDER几何/分辨率；设置过scene.camera不证明render camera改变。诊断需显式传入实际求值相机并回读。先看整脸轮廓、眼部主次、鼻嘴、发梢，再回生产镜头与完整材质。透明眼高光可由材质承担软边，不为纯线基准强加黑弧；原有弱线/断线不一概判新增。

当前完整静态模板v1仍拒绝legacy启用的INDEX宽度图，此修复不取消保护或授予跨角色运输资格。已保存blend及完整自主作者文件可在同一工作台使用，portable模板的显式迁移另列待办。正面通过不代签任意透视、侧面或角色，几何/相机不同不计算逐像素还原率。
