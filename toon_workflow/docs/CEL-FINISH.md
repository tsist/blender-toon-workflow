# CEL 高光、边缘光与强度动画

0.24 首次在同一 Toon Workflow 工作台提供「高光与边缘光」，0.25 沿用并增加可选头部参考光带。它把一个原生、可编辑的 finish 节点组放在已有 shader 与活动 Material Output.Surface 之间；基础颜色、面部 SDF、真实发影及已有 Normal 连接由各自模块保留。初始范围为本地、不透明 EEVEE Mesh 材质，不代表完整风格或发行验收。

## 使用

1. 选定对象和实际已使用的活动材质槽。材质应只由该对象使用；先另存工作副本。
2. 在母工作台展开「高光与边缘光」，选原生表面法向，或明确选择此对象已有的艺术法向修改器。后者新建独立 reader，不增加旧 reader 的输出消费者。受管活动槽表面当前只支持原生表面法向。
3. 设置高光阈值/过渡、边缘光幂次/扩展和颜色。高光与边缘光强度默认均为0，可先添加再逐步增加。
4. 半角高光方向是明确的世界空间艺术方向；可从指定 Sun 读取当前方向快照。Sun 后续动画不会自动改变这个方向。
5. 用「读取与检查效果」读取实际参数。修改静态参数后「更新效果参数」。
6. 强度动画在当前实际帧（包含子帧）写入指定的高光或边缘光强度；动作归专属 finish 节点组，插值为 LINEAR。Scene 面板字段是编辑暂存值，不是动画通道。
7. 「保留动作并撤下动画」解除绑定并为旧 Action 保留 fake-user，保持当前效果值。随后可「移除效果并恢复原输出」。有 finish 的受管表面须先移除 finish 才能恢复原材质槽。

## 可编辑参数

| 参数 | 语义 | 范围 |
|---|---|---|
| highlight_strength | 高光加性强度，可写原生关键帧 | 0–2 |
| highlight_color | 高光颜色，RGBA，alpha固定1 | 0–1 |
| highlight_threshold | 半角向量点积阈值 | 0–1 |
| highlight_softness | 阈值以上的平滑过渡宽度 | 0.001–1 |
| rim_strength | 视角边缘光加性强度，可写原生关键帧 | 0–2 |
| rim_color | 边缘光颜色，RGBA，alpha固定1 | 0–1 |
| rim_power | 边缘收紧幂次 | 0.1–64 |
| rim_lift | 边缘扩展偏置 | -1–1 |
| light_direction | 非零世界空间方向 | 3分量 |

高光采用 N、V、L 归一化后的半角向量和 smoothstep 阈值，并受 max(N·L,0) 门控。边缘光采用 clamp(1−N·V+lift)^power。结果通过 Emission 加到 Base Shader；它是艺术控制，不推称物理光源跟踪或真实遮挡。内部参数节点可检查；所有权接口只接受声明的参数/两条动画路径，手改拓扑或外来消费者需保留并显式处理。

## 保护与公共作业

只包装唯一活动 ALL Output 的 Surface 边；不重连 SDF 的 Emission.Color 或 cast/normal 的 Diffuse.Normal。已有材质/嵌入树动画、未知 Action、驱动、NLA、共享材质、透明/体积 closure 等在当前边界明确拒绝。

公共 run/edit 不新增字段。联合渲染为每个实际帧记录可选 toon-finish-freeze.v1：实际参数、专属 Action/slot/channelbag、节点形状、最终链接在 RENDER 冻结前后须一致。旧无 finish 的报告仍按旧合同读取。冻结见证是技术证据；离散样本不证明完整连续动画或所有角色美术通过。

## 参考与验证入口

本轮只读检查用户资产库的「三渲二着色器v1.0」「着色器0.1」及资产库副本，借鉴独立 mask、软硬过渡和加性组合；未把资产节点、代码或贴图复制进插件。旧图依赖的手工光向和属性前置未直接纳入。另参考 [MToon rim 规范](https://github.com/vrm-c/vrm-specification/blob/master/specification/VRMC_materials_mtoon-1.0/README.md) 的视角幂次控制。

本轮审计：work/audit/<batch>/ DELIVERY.json / QUALIFICATION.json 为已验状态；开发中作业和失败证据不冒充通过。母 WM 的完整三风格、全场景、Cycles、连续美术与 P5/P5-V/R4/P6/P7–P9 仍分别推进。


## 头部参考光带（0.25）

「高光形状」可保留旧半角高光，或明确选择头部参考光带。光带使用指定本地 Empty 的局部 X/Z 坐标；应将参考跟随头骨或目标头部，避免使用不随角色移动的世界锚点。它是独立的美术投影，不称 Kajiya–Kay、FlowMap 或镜头锁定 Angel Ring；不依赖 UV 朝向，不改原 UV、网格、法向或面部 SDF。

| 光带静态参数 | 含义 |
|---|---|
| center / 光带高度 | 参考局部 Z 的中心高度，-8到8 |
| half_width / 光带半宽 | 参考局部坐标中的半宽，0.0001到8 |
| softness / 光带边缘过渡 | 向内的边缘过渡宽度，0.0001到半宽 |
| curvature / 两侧抬升 | 沿局部 X² 的高度变化，-4到4 |

这些单位来自参考的实际缩放，并非固定米数；可用不同参考尺度和弧度塑造头顶横带。光带位置由参考决定，光向只控制原正面亮度门控，边缘光独立。半角阈值/过渡在该模式不参与光带形状，面板隐藏它们。光带四参数是静态作者值，动画仍只开放两强度原生通道。

新模式使用 toon-finish-node.band.v1；旧 toon-finish-node.v1 不迁移、不重签，已有节点、Action和原生值继续读取。更新可调整光带四参数；更换模式或参考须先明确撤下动画、移除当前效果，再添加所选模式。四个新常量及声明、TexCoord真实Object引用与局部轴/连线各自核验，手改冲突保留作者数据；不提供任意图重签入口。

逐帧除原九参数/Action/shape外，再从实际 RENDER 依赖图记录所用参考的完整4×4矩阵及光带设置。冻结后在既有line回调重读，矩阵与参数都须同刻一致。旧无光带报告保持兼容；不把一个静态矩阵对照称为完整动画验收。

本轮先联网读 Unity Toon Shader Highlight/AngelRing 文档和 Feng 作者的发高光说明。UTS使用Unity Companion License，本插件只借鉴独立美术投影设计，不引入其代码/贴图；实际输入的UV探针及两候选640诊断支持本例选参考光带，切线候选没有纳入正式模式。研究与最终资格见 work/audit/<batch>/


光带首次绑定会在明确选择的 Empty 上增加一条 owned 身份元数据 toon_finish_band_reference_id，两个头发可共享同一参考。Group 保存同一持久标识，以拒绝同名对象替换及复制出的重复身份。最后一个受管光带解除并删除零用户组后，只在键仍属于本功能且没有其他消费者时回收；构建失败也清理本次新键，不覆盖外来值。该声明内元数据增量与原始参考变换、骨架、Mesh/UV/Actions保护分开记录。

## 分段高光遮罩（0.28）

头部参考光带可显式开启「发束高光遮罩」，选择已加载的本地 FILE/PACKED、Non-Color 图像及目标已有UV层。默认关闭；只在 HEAD_BAND 可用。实际采样固定为 R、Linear、EXTEND、FLAT，并将高光乘以该值；rim 与原基础着色不受此遮罩影响。图像名称面板只是暂存，添加后用强 Image/Object 引用核验。模块不自动改色彩空间、打包、画图或覆盖UV。

一个受管Group独占一个Image ID，其强引用与Texture节点共两个消费者；允许独立Fake User。两个发部可加载同一PNG为两个独立Image ID。更新效果可改强度和光带形状；更换图像、UV或遮罩开关须移除后重新添加，已有着色动画先依法解除保留。

作者 API 为 `finish_core.apply_with_band_mask(context, settings, normal_modifier=..., band_settings=..., band_reference=..., band_mask={'image': image, 'uv_name': name})`。原 `apply` 及旧无遮罩工程保持原义。专用UV可依据明确头部参考投影新增，保护旧UV及active/render指针；静态投影不是沿发丝流向或任意形变跟随。画面需要分段尖收高光时优先设计合适的作者图，再检查实际采样，不靠强度穷举修整条横带。

有遮罩的联合输出使用 `toon-finish-freeze.v2`，分别记录原始UV、实际RENDER UV、图像与目标身份，并核对before/frozen/Line重读；无遮罩仍为v1。不能把原始UV哈希当作细分后RENDER UV。当前开发与限定运行证据位于 `work/audit/<batch>/`；以最终DELIVERY为准，图像及私人角色不随插件分发。
