# BASIC 描边的公共宽度编辑

公共 `toon-workflow edit` 的 `style` 仍是带 SHA 的 JSON 文件描述符。完整 Pencil 样式继续使用 `toon.autonomous-style-file.v1`。BASIC 模式仅调整整体线宽时，使用独立的闭合文档：

```json
{
  "schema": "toon-basic-line-width-edit.v1",
  "line_width": 1.21
}
```

`line_width` 是 BASIC 实际消费的绝对静态像素线宽，范围为 0.1..32。把当前线宽增加 10% 时，先从匹配源的 `inspect` 作者快照读取原线宽，再乘 1.1；例如原值约 1.1px 时请求约 1.21px，不能一律填 1.1。实际 RNA 值按 Blender float32 读回，写入 `toon-author-style.json`，其它作者字段必须逐值保留。

`width_multiplier` 属于 PRESET，BASIC 不消费该字段。0.22.1 的错误候选只改了倍数，三帧线图完全不变，保留为失败证据；新 schema 不接收旧错误字段，也不改签旧包。无需修改 Pencil 绘制内核。

仍需 `expected_author_sha256`，它来自该源的 `toon-author.json` 内部作者指纹，不能拿文件 SHA 代替。编辑输出新的可编辑候选，不渲染图像；继续以新候选运行并冷读结果。源文件不覆盖。

此文档只适用于 BASIC，不创建或转换 PRESET，不修改既有 document/recipe、线色、边分类、组、元素、面部 SDF、法向或镜头方案。目标宽度有原生 Action/普通 NLA/driver 或外部驱动变量引用时拒绝；Scene 含 META NLA 容器也拒绝，避免漏掉内层动画。动画应在其原生作者入口显式编辑。未知字段、bool、非有限值和越界值均拒绝。

这不是表面材质库，也不是完整风格模板。实际运行资格以本批 `basic-edit/` 及盲用报告为准；开发中的文档不代替运行验收。
