# 注册UV上的关键光角作者（0.40）

制作前先按[面部SDF制作与验收标准](FACE-SDF-PRODUCTION-STANDARD.md)确定目标形状和表示能力。本页定义0.40的实际作者格式，不是所有二游的通用SDF标准；外部图的UV、方向、角度轴与端点先按实际读者核对。

在同一工作台的SDF面板填写「关键光角计划」JSON与新的绝对输出目录，再点击「编译关键光角阈值图」。成功后生成图放入现有待绑定图像选择；不会自动改变材质、原图或UV。之后按现有SDF流程显式绑定活动材质；有艺术投影接受层须先保留其图和设置并合法移除，重绑后恢复。不同材质保持独立Image数据块。

计划schema为`toon-face-sdf-key-author.v1`，严格字段如下：

```json
{
  "schema": "toon-face-sdf-key-author.v1",
  "binding": {
    "target": "实际对象名",
    "mesh": "实际Mesh名",
    "uv": "用sdf_witness.snapshot(mesh, uv_name)返回的完整对象替换此字符串",
    "mirror": true,
    "coordinates": "SDF_TEXTURE_UV_TOP_LEFT"
  },
  "size": [1024, 1024],
  "shadow": "WHITE",
  "keys": [
    {"phase": 0, "constant": "LIT"},
    {
      "phase": 0.25,
      "image": {"file": "D:/author/key45.png", "expected_sha256": "实际文件的小写SHA256"},
      "channel": "R", "threshold": 0.5
    },
    {"phase": 1, "constant": "SHADOW"}
  ]
}
```

这是字段说明，需填入真实UV对象与SHA才是有效计划。每张PNG须已经在实际消费UV平面配准，大小与size完全一致；不做自动展开、缩放、裁切、UV转移或左右图猜测。二维数组行从图像顶部向下；Blender UV原点在下方，输入/输出读回明确翻行。mirror记录生成图的使用意图，绑定时仍由实际SDF读者显式选择。

黑=亮，白=影；选定R/G/B的Non-Color标量`>=threshold`解释为影区。phase为当前头部水平光角`abs(yaw)/pi`，不是含仰角的离轴角、余弦或外部sentinel格式。2–16个key，首phase=0全亮、末phase=1全影，严格递增；单边1..2048，K×H×W不超过32M。早期影区须包含在后期影区内，冲突明确报出key/像素，不自动union修图。

内核以规则图像像素中心到反类中心的欧氏距离，在相邻key的0→1区域按距离比分配阈值T，解码影区为`T<=phase`。采用Felzenszwalb/Huttenlocher 2012 §2的可分离距离变换设计，自主实现，标准库+NumPy，无新增依赖。空/满key使用有限对角线距离约定；不是世界空间或连续曲线SDF，不保证速度C1连续。

输出新目录含`onset.png`（RGB相同的16位数据）、`recipe.json`与`report.json`。每key在float64、PNG16量化后均须精确重建；Blender Non-Color重新加载验证码值后才完成。目录已存在、UV或源SHA变化、非嵌套、量化不能保key等均拒绝，不覆盖源。异常暂存目录仅留诊断，不能作已接受结果。

作者形状先在实际脸部检查，再采用编译结果。保留生图原件和派生处理说明；让生图负责明暗形状，数值编译负责阈值，不能把8位升格冒充新增精度。采样平滑与Softness可能移动/软化关键边界，PNG码值精确不替代完整材质、必要中间光角、镜像方向与最终Combined/白底Line验收。

API：`sdf_key_authoring.read_recipe(path)`、`compile_keys(context, recipe, new_output_dir, progress=None)`、`compile_from_ui(context)`。数值内核`sdf_keys_core`不依赖bpy。该作者入口不新增公共run/edit参数，不增加第二插件，也不改变既有SDF解码或冻结profile。具体包/艺术资格从当前批次DELIVERY读取。
