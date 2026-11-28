# 面部 SDF：真实三维光向

## 0.47：定向影形与自然受光（开发候选）

新增显式`DIRECTION_AUTHORED`，使用原FTv4与新FSv6；descriptor.v5、SDF freeze.v6，face/cast freeze继续v4。旧FSv5/DIRECTION_CONTINUITY保原含义，不自动升级。该模式只让已有独立key8/key11与受guard保护的top运输参与作者修正；其余上后方方向保原LRUD残余，不将未合格的共享影形强加到全部方向。

唯一新ADD取原packed.X+Y作为U'，den仍为max(1,U'+V)，原pair delta和top/guard沿相同结构进入一次比较；没有按yaw硬切。与同den配合时，U'Dold+Σwi(Di−Dold)=w8D8+w11D11。旧D来源身份仍显式保留；不因此把残余T−phase或top余量称为严格SDF。profile与新encoding固定独立作者集，不沿旧profile的图签名保存临时复制组。

工作台选择「定向影形与自然受光」，明确迁移共享载体的全部材质读者及依赖后重绑。四源/各自实际UV/独立pair采样保持；鼻片关闭修正仍pair三参数None、36纹理、无向量/pair/新ADD；开启修正为45纹理。原UV、高光/固定影、法向、paint/Sharp和像素线宽不变。

本地依据：0.46的16格发现225/270+30眼下横向亮带；固定225+30三态确认旧共享D本身带横带。新RAM四图中270侧光仍保侧颊纵向亮区，225回原LRUD，不能仅以全脸变暗判为修复。具体生产、控制、冷开/正式Combined/白底Line和最终采用资格以`work/audit/<batch>/`为准，不授连续整球、其它角色、多灯、Cycles或完整风格家族。C0不代签影界速度。


## 0.46：连续方向面影（开发候选）

同一工作台新增显式`DIRECTION_CONTINUITY`，使用FTv4/FSv5；原DIRECTION_KEYS/FTv3/FSv4保持原规则，不自动迁移。当前固定覆盖key8/key11，其它upper仍为既有作者来源，不能称全部方向均已独立制作。具体资格以`work/audit/<batch>//`的最终回执为准。

保留原H、独立V及旧D三张图，再添加Non-Color连续图：R=key8，G=key11，B=原顶光canonical作者域的下颌运输距离。连续图有自己的实际UV和正采样半径；旧图6像素和连续图1像素是本角色来源设置，不是全图必须相同的参数。完整RGBA、UV、尺寸、采样、固定key布局与编码都进入新版描述及冻结。

原生GN增加一个POINT/FLOAT_VECTOR，XY存两key权重、Z固定0，shader单次Attribute.Vector读取。距离差在同一次明暗比较前混合；顶光运输余量使用`max(Djaw, Gtop-.5)`，与guard复用同一个真实过滤Gtop输出，保持原鼻唇/下颌响应。这个兼容余量场不是严格SDF，也不保证匀速；实际邻帧及方向锥仍须看完整材质。

工作台选择「连续方向面影」，在明确解绑共享载体的全部读者/cast后显式切换；绑定原三源及连续图/实际UV/独立采样半径。皮肤开启修正；独立鼻影关闭时，第四来源明确为None、不开pair读者，保原LRUD，仍签新版身份。API参数为`pair_image`、`pair_uv`、`pair_filter_radius_texels`；关闭时三参数均省略。旧mode拒绝这些参数。

作者画布仍须对齐自己的canonical相机、实际求值表面与UV。当前pair和jaw来自两个不同捕获，即使都为640/d80，也不能称共同当前投影；来源证明留作者回执，运行时验证实际Image/UV/完整像素身份。迁移后同时重绑NODE_RGBA描边来源、冷开、正式Combined与白底Line，不能保存旧元数据的临时复制组冒充新功能。

## 0.45：同一工作台的显式关键方向模式

`DIRECTION_KEYS` 使用 FT `toon-face-threshold.v3` 和 FS `toon-face-sdf-shader.v4`；旧三代读者及 FTv1/v2不自动迁移。正式载体在原位置保留六字段，再增加均匀 POINT `direction_upper` / `direction_top`。14键/24锥文档及身份进入图签名，不依赖审计脚本、SciPy运行时、handler或timer。六个上后方键当前共用同一作者影形，不是任意六套独立影形。

保留原水平R/UV与独立上下GB/UV作为T；第三张独立Non-Color图R/G存上后方/顶光距离，明确其实际UV。第三图可以共用已合格的上下UV，但图像ID独立；三个来源按各自真实尺寸计算采样步长，不假定等大。所有来源完整RGBA、UV、采样、编码与decode均进descriptor和冻结记录。

在工作台选择「三维关键方向修正」，显式切换已解绑的载体，再指定水平、上下和第三图及真实UV。`direction_correction`为逐材质静态开关：皮肤可以启用，鼻影等共享载体的独立材质可以关闭以保留原LRUD响应。关闭仍使用新版身份和三来源记录，不能让旧FSv3偷偷读取FTv3。迁移须处理同一对象的全部读者、cast与接受层，并重绑实际常量插槽的描边线色来源；不能只迁皮肤而遗漏鼻片。

算法复用原Gtop的同一个9tap和Softness：`M=smoothstep(.5+s,.5+2s,Gtop)`（s>0），s=0时`M=Gtop>.5`。`den=max(1,U+V)`、`u=U/den`、`v=V*M/den`；`S=(1-u-v)T+(u+v)phase+uDupper+vDtop`，仍一次比较/压暗。关闭该材质修正时U/V先精确乘0。不消费任何高度POINT范围或第三图B范围。

**距离单位须分清。** d是共同640像素模型投影中的有符号像素距离，文件编码`E=.5+.5*d/640`；shader消费的是phase阈值偏移`d/80`，所以解码为`8*(2E-1)`。8不是把编码还原成原始像素d的系数，不应改成640。纯顶保原全亮域还需`Dtop>=Softness`，不能只证明Dtop>0。

新版身份：FT freeze.v3、FS descriptor.v3 / freeze.v4，依赖FTv3的cast freeze.v3。旧schema拒绝新字段；接受层继续经完整SDF见证关联。环绕预览也记录第三图及逐材质开关，公共run/inspect/edit/recover沿原入口，执行库存包括两个新核心和原polar模块。

本批已完成精确包最小运行与角色工作副本迁移，最终冷开/实图/正式Combined和白底Line资格以[本批证据](../../work/audit/<batch>/)为准。finite/uniform与哈希保持不代签任意病态参考矩阵的数值精度，也不代签连续整球、多灯、其它角色或完整风格家族。以下v2/v3文字保留为原模式与历史证据。

适用：统一 Toon Workflow 的开发候选、Blender 5.2，核验日期2026-11-14。新方向载体为 `toon-face-threshold.v2`，新读者为 `toon-face-sdf-shader.v3`。旧默认仍是水平左右模式，不会自动升级已有工程。

本轮方向核心、爱音角色独立上下UV/图像及32组实际光照/机位、cold和正式公共run/inspect已通过限定验收，证据见[本批交付](../../work/audit/<batch>/)。父线程实际看过Combined/白底Line，鼻嘴线及原绘制/锐边/像素线宽保留。全角连续艺术、其它角色和多灯仍未授予资格；本页接口说明不替代具体画面验收。

**实际UV决定来源结构。** bundle-v001曾试验单RGB共UV；真实角色回填发现8643个矛盾texel后已否决。当前v3保留原水平Image/UV，新增独立vertical Image/UV（G上/B下）。证据见本批 `author/skin-OLD-UV-CONFLICT.json`。方向数学与UV可承载性分别判断，不能凭旧核心检查继承角色UV资格。

## 光向与阈值

光向取**实际 Sun 的求值世界 +Z**，即表面指向光源的方向，不读相机朝向或固定世界轴。头部基准从实际reference世界矩阵构造正交轴：+X为right，−Y经Gram–Schmidt去除right分量后为forward，`up=normalize(forward×right)`。转头或转灯都会改变相对光向。reference须可逆；Sun世界轴须正交且为正手系。

设单位光向在该头基准中为 `(x,y,z)`，其中y朝前、z朝上：

```text
phase = acos(clamp(y, −1, 1)) / π
t = sqrt(x² + z²)
vertical_side = 1 if z >= 0 else 0
vertical_weight = z² / max(t², ε²) × smoothstep01(t / 0.1)
mix = authored_weight × strength
```

正前phase0，正后1，正上/下0.5。固定 `0.1` 是前后轴附近的sin-cone平滑区，使上下选择连续退回水平分支；它不是新增美术旋钮。旧左右side语义保持。新mix不再乘旧vertical fade，否则纯上/下作者图会被关闭；v2要求 `vertical_fade=0`。

## 独立左右图与上下图，一次压暗

| 通道 | 含义 | UV读取 |
| --- | --- | --- |
| 水平Image的R | 既有水平入影阈值 | 原UV与可选左右镜像UV，按side混合 |
| 独立vertical Image的G | 上方经线入影阈值 | 独立vertical UV，不镜像 |
| 独立vertical Image的B | 下方经线入影阈值 | 独立vertical UV，不镜像 |

先按vertical_side选择G/B，再按vertical_weight与水平R混成一个阈值，最后与phase比较。27个Image Texture节点分为原水平左右各9tap和独立竖图9tap。两Image ID和两UV层名必须不同；各自完整RGBA内容均签入descriptor，不能只核水平R。

v3必须使用正采样半径，沿用既有3×3 binomial过滤、实际图像尺寸与Linear/EXTEND。Softness只决定同一混合阈值的硬/软比较，不把水平与上下分别压暗。最终沿原单个DARKEN出口叠加：主作者层只能进一步压暗，不能凭亮区抬亮上游已暗的底色或投影。实际效果仍须分别检查作者阈值、上游材质、cast与完整画面。

固定编码身份为 `R=horizontal;vertical.GB=top_bottom;independent_UV;polar_angle/pi.v2`，写入读者descriptor及冻结见证。`vertical_source`分别记录竖图G/RGBA身份、原始UV descriptor及按竖图尺寸导出的采样步长。

## UI操作

在3D视图侧栏「三渲二」→工作台「专用SDF贴图面影」中：

1. 选中明确的脸部对象和实际材质slot，核对Sun、头reference、区域权重、实际UV及独立图像。已有工程先保留检查点。
2. 先保检查点，明确解绑旧面影/SDF读者、接受层与cast，并移除cast carrier，再点「清理未使用的旧面影读者」；它只删除当前载体无实例且未改动的受管旧定义，保留图像、UV和GN，可Undo，仍被使用的读者会跳过。只隐藏节点不算解绑，未知或改动过的孤立定义会拒绝整次清理；罕见的部分删除异常须立即撤销。
3. 清理完成后选择「面影方向模式 → 三维上下左右」，点击「应用面影方向模式」。原生输入动画/driver仍会阻止切换。已有载体显式切换时保持UID、owner和源属性，转v2置fade0、回v1置0.1；不静默迁移，同profile不重建图。
4. 保留水平图及原UV，channel使用R，明确水平镜像；另选择「上下光独立阈值图像」与「上下光独立UV层」，G上/B下不镜像。设置正采样半径，再「绑定SDF读者」。使用「检查SDF实际carrier与读者」核对两个来源。需要cast时另按正式流程绑定。
5. 用真实Sun的左右、上/下、混合方向及前后近轴实际预览；不能仅看贴图窗口或一张Beauty就签全角。

重建读者时还要检查描边颜色的显式`NODE_RGBA`来源。若它引用被解绑节点的常量RGBA插槽，节点名称和接口identifier不能沿用猜测：先记录原来源的实际角色、插槽及RGB，重建后显式指向同角色的新常量插槽，再核所有线色来源/目标RGB精确一致、其它描边配置原样。读者有效不代表旧线色引用仍有效；本批真实角色在家族重新观察时确实拒绝了失效的旧插槽，随后采用显式来源迁移，实际画面仍需最终Line验收。

## 最小API

前置条件：`target`为本地可编辑独占Mesh；已有FLOAT/POINT源属性 `FaceLeftZero=0`、`FaceRightOne=1`及已设计的 `FaceWeight`。图像为独立FILE/PACKED Non-Color数据；`uv_name`为真实匹配的UV。`bpy.context`须明确激活该对象与唯一活动材质。下例的采样半径仅示范正值，不是角色美术参数标准。

```python
import bpy
from toon_workflow.toon_workflow_addon import face_thresholds as FT, face_sdf as FS

# 仅用于没有面影载体的新目标；返回真实modifier。
modifier = FT.build(
    target, head_reference, sun,
    "FaceLeftZero", "FaceRightOne", "FaceWeight",
    strength=1.0, vertical_fade=0.0, profile=FT.POLAR_PROFILE,
)

# 已有载体则先解除读者/cast，使用下句代替build：
# FT.switch_profile(target, modifier, FT.POLAR_PROFILE)

FS.bind(
    bpy.context, image, uv_name,
    channel="R", mirror=True, filter_radius_texels=1.0,
    direction_mode="POLAR_LRUD",
    vertical_image=vertical_image, vertical_uv="ToonFaceVertical",
)
```

`FT.initialize()`旧默认左右均0.5、权重0，不能把它直接当作SDF所需left0/right1已经完成。已有源属性保持其真实值；不要为接入新读者盲目覆盖整脸权重。

## 制图与适用边界

先导出实际UV、确认材质slot与使用面，再在模型上设计各经线的关键黑白影形，用数值工具编译入影阈值。R可保留旧水平作者数据作为起点，G/B须按同一实际UV专门制作；上下不能直接翻U冒充。见[制作与验收标准](FACE-SDF-PRODUCTION-STANDARD.md)。AI可辅助官方参考分析、影形草案或模型上的候选设计，不能直接把生出的灰度图当作具有正确数值语义的SDF成品。

这是单Sun、单polar入影阈值模型，不是完整二维方向函数：同一分支每个像素仍只能表达一次亮→影。后半球、正前/正后端点与多次明暗反转须单独确定作者目标；前后平滑区只处理选择连续性，不保证所有角度的艺术质量。多灯不会自动合成一个正确的主光。

旧B55/+30的有限艺术采用不能继承到v3：新phase包含真实仰角，底层cast与作者层的接管也可能变化。必须重新观察该方向及反向、顶/底等必要对照，不能用相同RGB字节或旧通过状态代签新效果。本批已重新观察55°上下控制、顶底、侧背和两组机位并完成formal，限定32组范围采用；连续全角艺术仍单独未签。

## 冻结身份与已验范围

- 任一FTv2使用 `toon-face-threshold-freeze.v2`，六字段含vertical_side/vertical_weight；旧四字段保留旧身份。
- 任一shader v3使用 `toon-face-sdf-freeze.v3`，允许多材质，并逐row记录material/slot绑定；读者descriptor使用 `toon-face-sdf-reader-descriptor.v2`。独立竖UV另记录source_vertical_uv及实际vertical_before/copied/frozen，三阶段数据须精确一致。
- cast sensor依然是旧sensor profile；嵌套FTv2时使用 `toon-face-sdf-cast-freeze.v2`。空cast或全旧face仍保留旧freeze默认。
- 旧v1/v2读者、签名与默认选择不被自动改写；新profile不能偷读旧carrier或旧schema。

[方向核心v002](../../work/audit/<batch>/) 的55个真实RENDER光向、双向/同profile切换及旧v1图签名exact继续复用，最大字段误差约5.55e-6；其单图读者切片只作历史证据。[独立双图v004](../../work/audit/<batch>/) 为passed，28项执行检查覆盖双Image/UV、live/frozen descriptor、FTv2依赖的cast与cast-freeze.v2、J双UV冻结及128²作者/冻结像素exact；另有四项切换故障边界已通过。计数是执行记录，不是需求覆盖率或角色艺术通过证明。

实现交叉审查见[reader review](../../work/audit/<batch>/)与[witness说明](../../work/audit/<batch>/)。角色实际结果及最终交付范围以后续明确记录为准。

## 设计参考

### 三维方向关键影形研究原型（2026-11-14）

上侧逆光的跨方向单G目标在实际消费域出现硬冲突；ADMM未收敛、单侧压G实图产生嘴边断带，均未采用。新原型保留旧T、全部旧图和已验UV，只在比较前加入一个方向调制的signed key：`S=(1-W(L))*T+W(L)*(phase+D(UV))`。D从原生生成的已注册模型二值影形按共同640px投影距离编译，正亮负影；不是全方向物理距离。仍沿原softness、mix/valid及单DARKEN出口一次比较/压暗，头发保持法向、R高光及B固定影。

方向文档14键/24凸锥，非负射线/平面重心插值，C0而非C1；零作者anchor保原，六个上后方作者键暂共同一D。作者期用SciPy烘焙锥矩阵，Blender运行仅需原生GN/NumPy，无SciPy运行依赖。GN直接读实际Sun世界+Z及头部GS三轴，每次求值产生W，不加handler或timer。实际Blender5.2.1 RENDER23光向已通过数值对照，全命中、最大W误差6.06e-7、局部坐标exact，清理后原guard与属性/修改器恢复。

12角Python权重原型图片已逐张审看并回读全部24个EXR数组；前55/305+30、0+60和120-60与旧图exact，上侧逆光双团块改善为连贯下半脸影。纯顶光有2像素、最大.00048828125差异，不称exact。原图片运行收尾变量遮蔽导致exit1，恢复报告明确区分原in-run断言与事后重取source receipt，不改写原失败身份。后续原生7角退出检查已完成，85/87.5旧岛回流，拒绝该gated艺术采用；共同端点新九角结果见文末最新判决。

**资格仅为RAM机制原型。** 没有新生产blend、注册FSv4/FT profile、descriptor/freeze、GUI/CLI或正式Line资格，不替换当前默认和0.44.2包。不能将复制后改图但旧metadata仍在的shader保存为可用交付。正式接入须在同一Toon插件内实现身份、所有权、冻结与冷开；原v3不静默迁移。证据根：`work/audit/<batch>//`，重点看CONSTRAINT-ANALYSIS.json、REJECTED-ROUTES.json、LIVE-DIRECTION-VERIFY-v002.json、direction-preview-v001/RECOVERED-REPORT.json。

研究依据：Shading Rig原作者项目页 https://lohit.dev/ShadingRig/ 与Blender实现作者公开说明 https://josephclaytonhansen.github.io/blender-addons/shading-rig-correlations/ 支持按三维灯向编辑着色的工作思路。后者说明归一化反距离权重；本项目采用自主正锥重心法，不宣称相同算法。其商业许可不适用于引入本项目，无下载、安装、代码复制或运行依赖。TOG大PDF取回失败，未读全文。

[MingToon作者面部SDF文档](https://studioraming.github.io/mingtoon-docs/en/guides/face-sdf)明确区分左右上下作者通道及单通道镜像的纵向限制，并提示实际UV匹配。其公开设计支持本项目补足上下作者维度；本项目的polar公式、RGB布局和固定前后sin-cone是本地实现取舍，不宣称与该产品内部算法相同。没有复制其代码/贴图，也不引入其商业产品或运行依赖。原文阅读边界见[来源记录](../../work/audit/<batch>/)。


## 后续实图判决：共同端点仍未默认采用

原生7角密采已证伪gated恢复原T的艺术：85°鼻下块、87.5°双点回流，W/hit与数学C0资格保留，艺术拒绝。新共同距离端点让top键也参与作者混合：`S=(1-Wupper-Wtop)T+(Wupper+Wtop)phase+WupperDupper+WtopDtop`；在全作者锥内旧T系数为0，不调W/phase/softness。Dtop从同一模型边界向外延拓，距离不截断，E=.5+.5*d/640、解码(2E-1)*8=d/80，不用常量全亮哨兵。

新九角已由原生attribute驱动并实际看图/EXR回读：85/87.5/89未见旧岛回流；55/305+30与0+60对原源像素exact，局部坐标exact，Wupper/Wtop最大oracle误差约6.06e-7。**纯顶90下颌/颈部180像素被提亮，最大差.385986328125，故仍partial_NOT_ADOPTED_default。** 这不是前版2px浮点差，也不是已确认的艺术改进。源、默认0.44.2、旧图/UV均不替换；没有生产blend、profile/freeze/UI/CLI或正式Line交付。

先复用原顶光实际下颌/颈部影形确定作者范围：顶光不等于所有正mix表面全亮。模型外延拓可消除一类回流，但不能代签面部、下颌和颈部的分工；不能继续扫平移量/cap/W/softness。实际图像、残余问题与正式接入前置见 `work/audit/<batch>/`、SIGNED-ENDPOINT-COMPARISON.png、TOP90-JAW-NECK-CROP.png和native-endpoint-preview-v001/VERIFIED-REPORT.json。


## 2026-11-14 最新限定结果：原顶光源响应保护

后续切片已完成17个active光照/机位、18张新实渲（含1张原版对照）并逐张审看/独立EXR回读。两机位纯顶原有鼻部小影和下颌均exact保留；B的前55/305+30、前上0+60和120-60控制也逐像素exact。75°下颌白带、85/87.5旧鼻颊团块在当前观察范围未见回流。源及0.44.2包未改，仍未注册正式profile/freeze/UI/CLI或生产保存，不授连续整球、多灯/其它角色、正式Line或完整母阶段资格。

最终U（上后方）保持完整，V（top）只乘原顶光源Gtop同一实际采样的fully-lit guard：s>0时`M=smoothstep(.5+s,.5+2s,Gtop)`，s=0时`Gtop>.5`。`d=max(1,U+V)`，`u=U/d,v=V*M/d`；仍一次scalar比较/压暗：`S=(1-u-v)T+(u+v)phase+uDupper+vDtop`。不用高度POINT域或新纹理B作为最终shader的A；相关节点只留在研究诊断，不应带入正式实现。

纯顶保真有明确条件：U=0、T=Gtop、phase=.5；原影和完整soft过渡域M=0。原全亮域还需Dtop≥s，不能只说Dtop>0。当前实际s=.00400000019、PNG16解码Dtop全图min=.5636072，满足充分条件；hard分支和任意参数/矩阵仍须按用途运行验证。

几何高度域不是已采用顶光目标的替代品。纹理B范围剩2边界像素未恢复；POINT范围同时限制U/V虽保顶光却截出75°白带；仅限制V又在另一机位删掉鼻部原小影8px，均拒绝跨机位采用。由此修正的是**按key声明作用域与源响应保真**，没有扫W/phase/Softness、D图平移或重做UV。scopeRGB诊断含非目标材质，不能据此断言mipmap根因。旧导出和当前快照选区UV/原面索引exact但world坐标有差，旧中心回溯也不能代签当前GPU足迹。

证据：`work/audit/<batch>/`、`guarded-top-v001/VERIFIED-REPORT.json`、`FINAL-GUARDED18.png`。当前六个上后方键仍共一张作者图，不能称独立六套艺术。下一步按同批NEXT-FORMAL-INTEGRATION.md进入同一个Toon的身份、第三源、冻结/冷开和正式Combined/白底Line；不再重做本切片或保存旧metadata的复制shader冒充成品。
