# HM逐发束明暗与高光（0.31首片）

同一三渲二工作台的HM区提供「创建零层 / 读取 / 应用 / 移除」。物体模式选择本地独占网格的已用材质槽，指定独立Non-Color图像、现有UV和一个白色Sun。创建保留原两色Ramp配色、默认总强度0；先读取再编辑，不覆盖原图、UV或原法向。它不是旧HEAD_BAND/R遮罩的自动升级。

R是高光阈值作者场，G是方向明暗作者场。R/G共用一张专用Image ID；图像字段使用名称搜索，不能用旧sRGB/B图直接改色彩空间。G影响N·L影界，R影响N·H阈值；二者不是最终颜色简单乘通道。

接线为原Ramp→HM→固定B→Emission→原finish。B仍按原图/原色彩空间在下游叠底，包含新HM高光；显式创建时旧B-v2只升级外部接缝到HM-v3，移除HM恢复v2，不重建B内部图或owner。没有B也可接原Ramp→Emission。先移除HM才能单独移除B。已登记脸部/眼层/受管surface拒绝接管，通用surface/recipe也保护已存在的HM。

总强度零旁路整个HM；只把G塑形设零仍会进入新明暗模型。启用HM R时先将原finish高光强度设零，避免两个高光相加。低R不等于严格黑遮罩；若`highlight_threshold+(1-R)*highlight_map_strength`仍小于1，冠顶仍可产生高光，先核阈值范围再修改贴图。

首片只支持EEVEE、两色不透明CONSTANT RGB Ramp、一个实际可见白Sun、零简单Background World，无其它光源/探针。Sun启用阴影、diffuse factor=1、exposure=0；能量/颜色静态。世界方向由独立GN POINT载体随真实Sun对象变换求值；能量更换须先移除HM→改灯→重建。若先误改导致拒绝，恢复创建时能量后再移除，不编辑签名或声明解锁。

Sun对齐白Diffuse→ShaderToRGB除以E/pi提供受光近似，分别门控明暗与新R高光，不宣称通用精确visibility。零能量分母有限。多灯、环境照明、Point/Area、Cycles、曲线/透明头发和任意连续发型效果仍需另行验证。

原生作者API为`hair_map.attach(context,image,sun,uv_name,settings=None)`、`describe(scene,obj,index)`、`update(context,settings)`和`remove(context)`；RNA为`toon_hair_map.attach/read/update/remove`。公共CLI仍是`toon-workflow run/inspect/edit/recover`，HM状态随blend保存，无新manifest字段；source inspect的作者指纹仍是Pencil风格，HM完整验收由联合结果独立见证承担。

图结构、真实材质槽、R/G内容/色彩空间、实际RENDER UV/方向/Sun引用及同帧时间均冻结；每次Line采样重读。Image/Object名称和结构hash不能替代真实ID引用。换贴图或UV先移除再创建，原Normal改接亦应先移除HM。

实际包/机位/测试、源保护与美术资格从当批DELIVERY读取。零/卸载恢复、机制小样、原生UI操作、单帧联合输出和完整发型艺术是不同证据。贴图需要按真实UV绘制并实际看图，原生生图候选不保证数值、岛边界或最终风格。
