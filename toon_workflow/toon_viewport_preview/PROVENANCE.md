# 支持源码的本地来源

本记录覆盖扩展内五个项目支持源码副本及下述 Object 区间核心。项目路径相对于 Blender AI 项目根，包内路径相对于扩展根；本地来源路径用于追溯，不是用户安装依赖。

五份源码首行均声明 `SPDX-License-Identifier: GPL-3.0-or-later`；完整许可正文见同目录 [`LICENSE`](LICENSE)。保留现有声明，不以本地字节匹配代替最终权利结论。

| 包内路径 | 本地项目来源路径 | origin | 已核副本内容 SHA-256 |
|---|---|---|---|
| `_support/smoothing_flags.py` | `tools/toon_image_cpu/smoothing_flags.py` | declared | `737d09ec1b96e4cdf8cec1fdfe6fcdabe9071332b13158eac5eb3fa525e2217b` |
| `_support/relative_size.py` | `tools/toon_image_cpu/relative_size.py` | declared | `8b7685a926e899e82311c5314ec84f1b8299e09bd3246fe984d9b77c0e3676b2` |
| `_support/paint_editor_state.py` | `tools/toon_image_session_ui_c4/paint_editor_state.py` | declared | `c6a7f7bd112b573d784c700fa5a7a20ee02d33ad5247f8e1c84d297d47c57006` |
| `_support/geometry_exact.py` | `tools/toon_image_backend/intersections/geometry_exact.py` | declared | `36beb29191c3770ed54bd25c1c03c6c1c82aa5c00fb54cd5d26e305571b0139a` |
| `_support/chain_policy.py` | `tools/toon_image_chain_policy/policy.py` | inferred | `880e19e930b1aa85a32d6fcc31e15a21d60d60c3df15f58f51f160578ca6badc` |

`declared`：来源路径由当前 `dependencies.py` 明示。主线程于2026-11-04重新读取表列五个本地原文件，五份文件的完整内容SHA-256与当前包内对应副本均逐字节核同；其中四条路径为明示来源，`chain_policy`仍按下述推定路径记录。该核对不证明历史复制方向、最初来源或完整原创史，也不构成完整权利结论。

`inferred`：`chain_policy` 于2026-11-04与给定候选 `tools/toon_image_chain_policy/policy.py` 逐字节核同；该项目的 `LICENSE` 同样声明 GPL-3.0-or-later。当前包未明示其历史复制路径，不能从相同字节推断最初复制方向或完整原创史。

没有依据的源码发布版本、commit、实现代码copyright holder仍为unknown；不以schema版本、文件日期或GPL正文的FSF版权补填。本记录不为第三方代码、用户模型/贴图/字体或商业运行库授予新许可。

## Object 区间核心

`_object_core/` 包含当前项目 `tools/toon_image_backend/` 的八个 Python 数学模块及 GPL-3.0-or-later 许可，部分模块有本插件的本地修改；逐文件原路径、原始／当前内容 SHA 和 modified 状态见 `_object_core/SOURCES.json`。geometry增加显式正交视口有符号深度策略，直接相机输入的默认约束保留。该目录是随插件保留的内部副本，运行时不读取表列开发路径。新增 `object_adapter.py`、`object_view.py`、`object_fragments.py` 负责捕获快照适配、父边分类复用和已求值笔画区间裁切。

本轮使用现有项目自主实现，没有新增第三方依赖或商业二进制调用。字节核同和许可声明仅证明此份本地副本的追溯关系，不扩称最终完整权利审计通过。

## 0.2.27 最终发行材料

13份本地GPL支持副本的原始／当前SHA、4份修改说明与声明／推定关系已在SOURCES.json汇总。最终110个Python文件保留GPL-3.0-or-later头；LICENSE和_object_core/LICENSE完整保留。实现版权人、首次作者与未给出的历史commit使用NOASSERTION，不用FSF许可正文版权代替实现作者。NOTICE保留分发通知与不随包材料范围；SBOM.spdx.json列出文件级SHA，排除它自身的循环哈希，外部PACKAGE.json覆盖全部120文件。

新增viewport_scope.py独立实现按Screen保存逐Space的显示开关，并在保存时记录area/space定位；设计行为参考已有官方4.1.5 GPL视口源码，未复制其实现或商业组件。addon.py仅加入实际门控、按钮和注册清理，绘线数学／作者／输出模块保持.26字节。

运行提供的Blender／NumPy等不随此扩展ZIP发行。paint_editor_state支持副本中保留原源树的cpu_editor_state／paint_settings适配函数；当前插件的curve_editor仅使用对应的纯JSON／曲线校验函数，不提供该源树的完整CPU编辑器接口。dependencies.py也保留开发用fallback；正常完整安装首先读取包内_support，最终独立运行另外核对实际载入位置。这些源树适配函数不是新增运行依赖声明。
