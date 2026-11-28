# 风格组件、显式模板与整镜头漫画附加图

0.48 开发增量；仍在 P5 验收中。当前默认 0.47 和固定机位角色成果保留。全三维场景描边、后上光面影按用户决定暂缓。

## 同一工作台中的组件

选择本场景本地网格的实际已用材质槽，在组件区选择 GAME、MANGA、ALPHA_SHEET、GLASS、WATER、SMOKE 或 EMISSION 后显式应用。参数修改后点击更新；恢复会换回原 Material ID，不修改原节点、UV 或几何。

GAME 使用既有 CEL／单份 Blinn 与边缘光；GGX 反射是独立开关，默认关闭。静态光向是显式 Sun 快照，不承诺任意多灯或自动动画跟随。MANGA 材质使用纸白、圆点、交叉排线和墨色，Window 域按实际画幅的 X／Y 像素分别缩放；UV／Object 是明确选择的其它域，不承诺固定屏幕密度。UV 模式必须存在指定的真实 UV 层。

已有头脸 SDF、眼部叠色与高光、HM／固定发影等受保护材质应保留。共享 Mesh 的 DATA 槽拒绝局部替换；先在工作副本明确选择 OBJECT 槽或独立数据，不能把 guard 去掉。组件 build／update／inspect 在 `component_catalog`，实际槽位与恢复在 `component_slots`；它们不是旧 `finish_core` 的直通材质绑定。

## 明确角色与机位的模板

模板区新建空模板，填写角色名后从活动槽记录：`PRESERVE` 保留原材质；`COMPONENT` 录制当前合法组件的真实参数。需要多个槽共用角色时使用“绑定活动槽”，不会重新录制角色参数。

机位覆盖绑定真实 Camera ID，角色／机位名只是文档键。记录覆盖后，切换渲染相机，先恢复绑定组件，再明确应用模板；没有自动相机 handler。重复对已应用的受管槽应用会拒绝。角色必须有实际槽绑定，机位覆盖必须有实际相机绑定。

导出为 `toon-style-set.v1`，目标 JSON 必须不存在；旧 v0 的迁移由 `style_set.migrate` 明确执行。导入不运输场景 ID：对象和相机留空，必须重新绑定后应用。JSON 参数与源材质保留不是原始 INDEX 描边的可移植保证；原描边和新的完整线模板仍按各自合同管理。

## 整镜头 MANGA 附加图

对保留头脸／眼发的完整角色，使用工作台“整镜头风格附加图 → MANGA”。公共联合渲染额外输出 `Look.exr`、`Look.png`、`look.npz`；原 Beauty、Line、BeautyLineInteraction、Combined 均保留。OFF 直接使用原 Combined，不重新叠合原通道。

漫画化先在完整画幅处理已 resolve 的 Combined，再裁切。曝光／中调／纸墨阈值、圆点和排线的像素间距／角度可调；图案使用 4×4 覆盖抗锯齿。原描边墨色强度只在已有 Line 覆盖内调整 Look 的 RGB，不增加 Alpha。头脸 SDF、原 UV 和材质不被替换。

参数保存于 Scene，进入作者指纹和独立 `toon-shot-look.v1` 子回执。冷读取重新计算非线性附加图，检查裁切原点与 Alpha exact；不能用原四通道的线性重建公式认证 Look。亮度不能区分固有暗色与投影阴影，屏幕网点也不随曲面弯曲；具体角色须看最终图，技术通过不等于完整商业漫画风格通过。

## 媒体与当前联合渲染的边界

GLASS／WATER 先显式启用本 Scene 的 EEVEE 光线追踪。玻璃厚度模式须符合真实几何：实体球体用 SPHERE，薄实体板用 SLAB；`thin_wall` 不是自动补厚度。水面涟漪与烟雾为可编辑原生节点，不是流体模拟。

原生媒体 Beauty 已有独立保存冷开渲染样片。通用透明、折射、水体和体积尚未取得二维 Line 的光学整合资格，现有 opaque 联合入口继续拒绝；最终 Alpha＋单 Z 不能重建折射路径或体积透射。景深／运动模糊的 Line 协作和 Cycles 尚未验证。

## 验收入口

本次文件与证据：`work/audit/<batch>//`。槽位混用、实际 bpy Operator、故障回滚、保存冷开、公共 run／inspect 与真实图像分开记录。发布包和执行 inventory 以最终 DELIVERY 为准，不能拿旧 pin 改签新包。
