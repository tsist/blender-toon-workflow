# 请求时刻与原生实际时间

0.19开发增量，限定当前已验Blender 5.2.1 LTS/build9e2066aef7ef。统一工作台和公共CLI默认生成toon-workflow-manifest.v2，输出eevee_joint_sequence.v2。名义时间仍可写[2.8,3.2,-0.25]，无需手动换成较长的小数。

## 实际语义

Blender的frame_set接受整数frame和0..1子帧。它先把子帧存成float32，再把两者相加的总时间转成float32，最后用modf拆回原生frame_current/frame_subframe。负总时间的原生子帧可以是负数：-0.25会记录为frame0/subframe-0.25。直接把这个观察值送回frame_set会被夹到0；重放使用合法的floor整数帧与非负子帧，再严格核对观察值。

新规则保留原请求列表requested_times；times记录上述调用链真正产生的时间。2.8对应2+.7999999523162842，3.2对应3+.20000004768371582；这项转换公开记录，校验仍是确定的逐值相同，没有增加epsilon。负零与正零是同一时刻。

当前原生整数帧硬界为-1048574..1048574。Blender本身会静默钳位越界参数，公共v2入口先拒绝原请求或setter整数帧越界；不把钳位后的结果当成功。bool、NaN、Infinity及无效类型拒绝。不同名义数值若落入同一有效时间也拒绝，例如2.8和2.79999998；输入顺序保留，最多96个时刻。

这也不是直接把原请求转为float32：-1e-10经floor=-1和float32子帧carry后实际为0，须按真实setter链处理。仅比较原数字或子帧本身会漏掉这种碰撞。

## 旧工程与恢复

旧manifest.v1和sequence.v1保留原floor+float32子帧规则，旧报告按原身份冷读，旧作业不改签。新policy名称为blender-frame-set-f32-total.v1；未知policy/profile组合拒绝。仅已识别的旧profile可在历史只读路径兼容缺少policy的旧报告，新输出必须显式记录。

toon-workflow-state.v1继续作为状态容器，内部绑定真实manifest版本、原请求、有效时间、profile、policy、相机、来源、资源和执行pin。不能只改外层SHA让内部时间声明自动通过。

recover仍创建新公共作业，只复用已验证的完整连续前缀。原请求、完整settings、原来源和执行pin须全部相同；不跨v1/v2迁移或借更换名义时间复用原帧，不支持任意sample中点续算。

## 支持边界

原有面影、整头法向、镜头修形和二维描边仍在同一个插件里。时间修复不扩大原材质、实例、透明光学、DOF/MB、连续美术或跨构建资格。名义/实际时间和每帧相机有回执，采样完整性与完整动画美术分别验收。具体运行证据见本轮审计目录work/audit/<batch>//的DELIVERY与QUALIFICATION。
