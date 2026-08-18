/*
 * PuppyFace.h — StackChan 小狗表情组件
 * =======================================
 * 替换默认人脸，绘制极简线条风格的小狗脸：
 *   耷拉耳朵 + 圆点眼睛 + 椭圆鼻子 + 弧线嘴巴
 *
 * 8 种表情：
 *   Neutral  → 常态（对称、平静）
 *   Happy    → 开心（^^ 眼）
 *   Sleepy   → 困倦（眯缝眼、耳朵更垂）
 *   Doubt    → 好奇（五官绕鼻子锚点转 15°，长耳转完后再单独多转 15°；旋转
 *              方向随当前舵机 yaw 的符号镜像，见 doubtMirrorSign()）
 *   Sad      → 抱歉（大眼向上看、耳朵翻过来）
 *   custom "thinking"  → 思考（圆框眼镜 + 竖椭圆瞳孔）
 *   custom "excited"   → 兴奋（'><' 眼 + 舌头的静态表情，整体按 EXCITED_SCALE 缩小，
 *                         2s 后开始爪印动画，见下方 PuppyEar 顶部注释）
 *   custom "privacy"   → 隐私（闭眼、耳朵变形）
 *
 * 表情切换时，尺寸类参数（耳朵长宽、鼻子/嘴巴大小、旋转角度）会用
 * FloatTransition 在 500ms 内线性插值过渡；开心/好奇/思考时耳朵左右
 * 轻轻摆动。
 *
 * 放置路径：firmware/PuppyFace.h
 * 在 firmware.ino 中 #include "PuppyFace.h"
 */

#ifndef PUPPY_FACE_H_
#define PUPPY_FACE_H_

#include <M5Unified.h>
#include <BoundingRect.h>
#include <DrawContext.h>
#include <Drawable.h>
#include <Face.h>
#include <Expression.h>

// g_customExpr 定义在 firmware.ino，用于扩展表情
extern String g_customExpr;
// g_buttonState 定义在 firmware.ino：0=隐藏 1=弹起(up) 2=按下(down)，
// 关键词播报按钮
extern volatile int g_buttonState;
// g_subNLines 定义在 firmware.ino：字幕当前的行数，0=没有字幕要显示
extern volatile int g_subNLines;
// g_currentYaw 定义在 firmware.ino：舵机当前真实的 yaw（每帧从 Motion 库
// 刷新，不是最后一次下发的目标值），驱动"好奇"表情的镜像方向。volatile
// 是必须的——写在 loop() 的主任务里，读在这个文件里 draw() 所在的独立
// 渲染任务里，见定义处的完整说明。
extern volatile int g_currentYaw;

namespace m5avatar {

// ╔══════════════════════════════════════════════╗
// ║              过渡动画 / 旋转工具              ║
// ╚══════════════════════════════════════════════╝

// 单个数值的平滑过渡：每次调用传入最新目标值，内部用 500ms 线性插值。
// 如果目标值在过渡途中又变了，会以"当前已经插值到的值"作为新起点重新起步，
// 保证来回切换表情时不会跳变。
class FloatTransition {
 public:
  static const unsigned long DURATION_MS = 500;  // 默认过渡时长，绝大多数用法用这个

  FloatTransition() : durationMs_(DURATION_MS) {}
  // 允许单个实例用比默认更短/更长的过渡时长（比如爪印这种切换节奏很快的
  // 动画，需要比 DURATION_MS 更短的淡入淡出，否则上一次的淡出还没走完，
  // 下一次出现就已经开始了，会看起来像突然跳了一下位置）。
  explicit FloatTransition(unsigned long durationMs) : durationMs_(durationMs) {}

  float update(float target) {
    unsigned long now = millis();
    if (!inited_) {
      from_ = to_ = current_ = target;
      startMs_ = now;
      inited_ = true;
      return current_;
    }
    if (target != to_) {
      from_ = current_;
      to_ = target;
      startMs_ = now;
    }
    float t = (float)(now - startMs_) / (float)durationMs_;
    if (t < 0.0f) t = 0.0f;
    if (t > 1.0f) t = 1.0f;
    current_ = from_ + (to_ - from_) * t;
    return current_;
  }

  // 硬重置：立刻把当前值/起点/终点都设成 value，不做任何插值过渡。
  // 用于"重新进入某个表情时不应该看到上一次残留状态往回过渡"的场景。
  void reset(float value) {
    inited_ = true;
    from_ = to_ = current_ = value;
    startMs_ = millis();
  }

 private:
  bool inited_ = false;
  float from_ = 0, to_ = 0, current_ = 0;
  unsigned long startMs_ = 0;
  unsigned long durationMs_;
};

// Doubt（好奇）：五官整体围绕"鼻子锚点"旋转的最大角度。
// 屏幕坐标系 y 轴向下，这里用的旋转公式在该坐标系下视觉效果为顺时针（正角度）/
// 逆时针（负角度）；如果实机看到的方向反了，把对应角度取负号即可翻转。
static const float DOUBT_ROTATE_RAD = 15.0f * PI / 180.0f;

// 好奇表情的歪头方向随当前舵机 yaw 的符号镜像：捉迷藏游戏扫描房间时舵机
// 左右来回摆动找东西，歪头方向应该跟着摆向的一侧镜像，而不是不管转到哪边
// 都固定歪同一侧。yaw<=0 时保持原本的旋转方向（DOUBT_ROTATE_RAD 本来的正
// 角度，见上面的顺时针/逆时针约定）；yaw>0 时整体镜像成相反方向。所有用到
// DOUBT_ROTATE_RAD 的地方都应该乘上这个符号，而不是直接用常量本身——这样
// PuppyEye/PuppyNose/PuppyEar 三个独立的 FloatTransition 实例各自在下一帧
// 拿到新的目标角度时，会各自平滑过渡过去，过渡的起点天然就是 yaw 符号翻转
// （也就是 yaw=0）的那一刻，不需要另外写专门的过渡触发逻辑。
inline float doubtMirrorSign() {
  return (g_currentYaw > 0) ? -1.0f : 1.0f;
}
// 近似鼻子锚点的位置，好奇表情的旋转、兴奋表情的爪印位置都以这一点为基准。
static const int DOUBT_PIVOT_X = 160;
static const int DOUBT_PIVOT_Y = 140;

// ---- 兴奋(excited)表情的时间线 ----
// 进入后 EXCITED_PAW_START_MS 就开始出现爪印：先左爪，再右爪，如此交替
// EXCITED_PAW_CYCLES 轮，每次显示时长在 EXCITED_PAW_MIN_MS~EXCITED_PAW_MAX_MS
// 之间随机；交替结束后左右爪一起出现并常驻，常驻之后两只爪印会一起微微
// 左右摇晃。眼睛/耳朵这边则是保持静态姿势（'><' 眼更大一圈、耳朵向眼睛方向
// 靠拢）到 EXCITED_BLINK_SWING_START_MS，期间不眨眼、不摇晃，之后才开始眨眼
// 和摇晃——这两条时间线是各自独立的，不需要互相等待。每只爪印出现时位置会
// 在基准位置上叠加一点随机抖动。
static const unsigned long EXCITED_BLINK_SWING_START_MS = 1000;
static const unsigned long EXCITED_PAW_START_MS = 200;
static const unsigned long EXCITED_PAW_MIN_MS = 300;
static const unsigned long EXCITED_PAW_MAX_MS = 800;
static const int EXCITED_PAW_CYCLES = 2;
static const int EXCITED_PAW_JITTER_PX = 15;  // 爪印位置随机抖动的最大幅度（像素）
static const float EXCITED_PAW_ROT_BASE_DEG = 12.0f;   // 爪印整体旋转的基准角度：左爪逆时针、右爪顺时针
static const float EXCITED_PAW_ROT_JITTER_DEG = 5.0f;  // 每次出现时在基准角度上叠加的随机浮动幅度（度）

// 兴奋表情整体缩小比例：眼睛/鼻子/嘴巴/舌头/耳朵都按这个比例缩小。
// 爪印在此基础上再额外缩小 20%（EXCITED_PAW_SCALE），同时爪印内部脚趾间距、
// 两只爪印之间的间距、爪印与五官的距离都相应加大，避免整体缩小后挤在一起。
static const float EXCITED_SCALE = 0.7f;
static const float EXCITED_PAW_SCALE = EXCITED_SCALE * 0.8f;
static const float EXCITED_EYE_BOOST = 1.4f;       // 兴奋时眼睛在整体缩放基础上再放大一点
static const float EXCITED_EYE_INWARD_PX = 8.0f;   // 兴奋时两只眼睛互相靠拢的像素数
static const float EXCITED_EAR_INWARD_PX = 12.0f;  // 兴奋时耳朵朝眼睛方向靠拢的像素数
static const float EXCITED_NOSE_UP_PX = 10.0f;     // 兴奋时鼻子朝眼睛方向靠拢（往上贴）的像素数

// peekaboo 复用"兴奋"的整套尺寸参数（exciteLike），但整体要放大——第一轮
// 放大 10%，第二轮"整体都再放大20%"（1.1*1.2=1.32），第三轮又反馈"整体
// 还是再放大20%"，所以在 1.32 基础上再乘 1.2 = 1.584。每个用到
// EXCITED_SCALE/EXCITED_*_INWARD_PX/EXCITED_NOSE_UP_PX 的地方，peekaboo
// 分支都额外乘一个 sizeMul（= isPeekaboo ? PEEKABOO_SIZE_MUL : 1.0f，在
// 各自 draw() 里就近算好），不影响真正的"兴奋"表情本身的大小。
static const float PEEKABOO_SIZE_MUL = 1.584f;

// peekaboo 左耳盖眼的那片"耳朵"专用：整体再往外挪一点，离中轴线（嘴巴
// 所在的竖线）远一点，避免跟嘴巴的线条重叠；随 sizeMul 一起缩放。
// 踩过一个坑：这个常量本身乘的是 sizeMul，但耳朵形状里伸得最远的几个点
// （bellyX/curlCtrlX 等）系数比它大得多，sizeMul 每整体放大一次，这些点
// 往中线方向"长"得比这个常量的补偿量更快，导致"整体放大"反而让耳朵实际
// 看起来离中线更近——这一轮把数值调大了不少来补偿，以后再整体放大
// peekaboo 时要记得这个常量大概率也要跟着往上调，不能指望它自动跟上。
static const float PEEKABOO_EAR_AWAY_PX = 22.0f;

// peekaboo 的鼻子/嘴巴比"兴奋"整体缩小（跟 PEEKABOO_SIZE_MUL 是分开的
// 独立系数——sizeMul 管的是五官整体跟"兴奋"相比放大多少，这个只管鼻子和
// 嘴巴额外再缩一档，眼睛/耳朵不受影响）。第一轮缩小10%，第二轮反馈"继续
// 缩小20%"（在第一轮基础上继续缩，不是把10%改成20%），所以 0.9*0.8=0.72。
static const float PEEKABOO_NOSE_MOUTH_SCALE = 0.72f;

// 委屈（grieved）：鼻子比默认再放大 10%；鼻子/耳朵各自朝眼睛方向靠拢一点
// （复用跟"兴奋朝眼睛靠拢"同一个思路，但幅度是单独调的，不共用 EXCITED_*
// 那组数值，因为委屈不属于 exciteLike）；眼睛（三个同心圆）整体放大 20%。
static const float GRIEVED_NOSE_SCALE = 1.1f;
static const float GRIEVED_NOSE_CLOSER_PX = 6.0f;  // 鼻子朝眼睛靠拢的像素数
static const int GRIEVED_EAR_CLOSER_PX = 10;        // 耳朵朝眼睛靠拢的像素数（加在 topYTarget 上）
static const float GRIEVED_EYE_SCALE = 1.2f;        // 三个同心圆整体放大比例
static const float GRIEVED_SOCKET_SCALE = 0.8f;     // 眼眶（最外层空心圆）在 GRIEVED_EYE_SCALE 基础上再缩小20%
// 高光（最内层空心圆）在 GRIEVED_EYE_SCALE 基础上再缩小——第一轮5%，
// 第二轮反馈"再缩小30%"（在第一轮基础上继续缩），所以 0.95*0.7=0.665。
static const float GRIEVED_HIGHLIGHT_SCALE = 0.665f;

// 委屈：眼睛上方一条"担心"眉毛，弯成一条弧线（不是直线）——跟五官同步用
// FloatTransition 淡入，不需要额外常量控制过渡时长（PuppyEye 里复用眼睛
// 自己的 s 过渡进度）。
static const int GRIEVED_BROW_GAP = 9;     // 眉毛距离眼眶最高点的间隙（像素）
static const int GRIEVED_BROW_HALF_W = 9;  // 眉毛线段半宽——这一轮反馈"收口
                                            // 不要太聚拢，宽度保持和现在一样"，不动
static const int GRIEVED_BROW_TILT = 5;    // 眉毛倾斜幅度：内侧（靠鼻子）比外侧高这么多像素
static const int GRIEVED_BROW_ARC = 13;    // 眉毛中点相对两端连线再往下凹多少像素（"⌣"的弯曲程度，
                                            // 这一轮加深到更接近"u"型，宽度不变，只加深凹陷）

// ---- 关键词播报按钮：像一个从侧面看的狗粮碗线框——椭圆形"碗口"和圆角矩形
// "碗身"都只画白色描边（不填充），碗口盖住碗身的顶边（用背景色椭圆擦除模拟
// 布尔运算，具体做法见下面 draw() 里的实现注释），碗口正中间画一个白色实心
// 爪印。固定画在屏幕右下角。BODY_TOP/BOTTOM 是碗身顶/底边相对 BUTTON_CY（碗口
// 椭圆圆心）的偏移：BODY_TOP 是负数、绝对值比碗口竖直半径 BUTTON_RY 小，让
// 碗身顶边落在碗口范围内（会被擦掉）；BODY_BOTTOM 比 BUTTON_RY 大，让碗身
// 底边露出在碗口下方一截。碗身宽度 BUTTON_BODY_W 直接等于 2*BUTTON_RX，让
// 圆角矩形的左右两边跟椭圆最宽处（左右两个端点）严格对齐在同一条竖直线上——
// down/up 缩放时两者用同一个 buttonScale 系数，所以任意缩放比例下都还是对齐
// 的，不只是静止状态。down 态整体再按 BUTTON_DOWN_SCALE 缩小，靠
// buttonScaleAnim_ 过渡出"按一下"的动画。----
static const int BUTTON_CX = 270;
static const int BUTTON_CY = 205;
static const int BUTTON_RX = 23;              // 碗口（椭圆）横向半径（比上一版整体放大20%）
static const int BUTTON_RY = 11;              // 碗口（椭圆）竖向半径（在放大20%的基础上再压扁，横竖半径比从1.77升到2.09）
static const int BUTTON_BODY_W = 2 * BUTTON_RX;  // 碗身宽度=2*BUTTON_RX，两侧与碗口对齐
static const int BUTTON_BODY_TOP = -1;        // 碗身顶边相对 BUTTON_CY 的偏移
static const int BUTTON_BODY_BOTTOM = 16;     // 碗身底边相对 BUTTON_CY 的偏移（比上一版整体放大20%）
static const int BUTTON_BODY_RADIUS = 5;      // 碗身圆角半径（比上一版整体放大20%）
static const float BUTTON_DOWN_SCALE = 0.75f; // 按下瞬间整体缩小的比例
// 下面两个爪印参数是按 BUTTON_RX/RY 算过的：保证爪印整体（脚掌三角+四个脚趾，
// 含边缘磨圆半径）的外包络完全落在碗口椭圆内部，同时脚趾与脚趾、脚趾与脚掌
// 之间留有几像素不重叠的间隙（碗口是扁椭圆，比爪印天然的长宽比更"矮"，所以
// 缩放系数比脚趾动画用的默认值小很多）。改这两个值之前建议先算一遍外包络，
// 不要直接凭感觉调大，否则爪印会露出碗口或者脚趾互相粘在一起。
static const float BUTTON_PAW_SCALE = 0.34f;           // 爪印相对 drawPawPrint 原始大小的缩放（比上一版再缩小10%）
static const float BUTTON_PAW_TOE_SPREAD_MUL = 1.30f;  // 爪印内部脚趾相对脚掌/彼此的间距放大系数（爪印缩小后余量变多，适当调大避免脚趾粘连）

// 把局部偏移量 (ox,oy) 绕原点旋转 angle 弧度，用于让部件"自身"的朝向也跟着转
// （而不只是把部件的位置搬到旋转后的地方）。angle=0 时精确还原原始偏移量。
static void rotateLocalOffset(float angle, float ox, float oy, int &rox, int &roy) {
  rox = (int)roundf(ox * cosf(angle) - oy * sinf(angle));
  roy = (int)roundf(ox * sinf(angle) + oy * cosf(angle));
}

// 先把局部坐标 (lx,ly) 绕 (pivotX,pivotY) 转 hingeAngle（一次局部小铰链旋转，
// pivot 本身在这一步不动），再把结果整体绕原点转 bodyAngle（比如整只耳朵/眼睛
// 跟着表情旋转的角度）。用于"部件的一部分绕自己的某个端点多转一点"的效果。
static void hingeThenRotate(float lx, float ly, float pivotX, float pivotY,
                             float hingeAngle, float bodyAngle, int &outX, int &outY) {
  int hx, hy;
  rotateLocalOffset(hingeAngle, lx - pivotX, ly - pivotY, hx, hy);
  rotateLocalOffset(bodyAngle, pivotX + hx, pivotY + hy, outX, outY);
}

// 把 (cx,cy) 绕指定枢轴点旋转 angle 弧度（angle 通常来自某个 FloatTransition，
// 所以在表情切换时会平滑地转过去，而不是瞬间跳到目标角度）。
static void applyRotationAroundPivot(float angle, int pivotX, int pivotY, int &cx, int &cy) {
  int rdx, rdy;
  rotateLocalOffset(angle, (float)(cx - pivotX), (float)(cy - pivotY), rdx, rdy);
  cx = pivotX + rdx;
  cy = pivotY + rdy;
}

// 用三角扇形近似画一个可以旋转的实心椭圆（M5Canvas 的 fillEllipse 不支持旋转角）。
static void fillRotatedEllipse(M5Canvas *spi, int cx, int cy, int rx, int ry,
                                float angle, uint16_t col) {
  const int N = 16;
  int px[N], py[N];
  for (int i = 0; i < N; i++) {
    float t = 2.0f * PI * i / N;
    int rox, roy;
    rotateLocalOffset(angle, rx * cosf(t), ry * sinf(t), rox, roy);
    px[i] = cx + rox;
    py[i] = cy + roy;
  }
  for (int i = 1; i < N - 1; i++) {
    spi->fillTriangle(px[0], py[0], px[i], py[i], px[i + 1], py[i + 1], col);
  }
}

// 开心 / 好奇 / 思考时耳朵左右轻轻摆动的偏移量（sin 驱动，周期 1.2s，振幅 4px）。
static int earSwingOffset() {
  const float periodMs = 1200.0f;
  const float amplitude = 4.0f;
  return (int)roundf(sinf(2.0f * PI * (float)millis() / periodMs) * amplitude);
}

// 计算"某个条件持续为真已经过了多久"。调用方持有一对状态（wasActive/startMs），
// 条件刚变为真的那一刻记录时间戳，条件变假时清零，这样就能独立判断某个动画
// 阶段是否已经跑完（比如"主体旋转的 500ms 是否已经结束"）。
static unsigned long elapsedSinceTrue(bool isActive, bool &wasActive, unsigned long &startMs) {
  if (!isActive) {
    wasActive = false;
    return 0;
  }
  if (!wasActive) {
    wasActive = true;
    startMs = millis();
  }
  return millis() - startMs;
}


// ╔══════════════════════════════════════════════╗
// ║              PuppyEye 小狗眼睛               ║
// ╚══════════════════════════════════════════════╝
//
// 替代 Eye 组件。根据表情画不同眼睛：
//   常态/生气/好奇: 竖向椭圆（好奇时会转）
//   开心: ^^ 弧线
//   兴奋: '><' 眉眼形（比其它眼睛款式更大，静态不旋转）；custom "peekaboo"
//     复用同一套'><'画法（左眼被耳朵挡住，不画，见下方 style==1）
//   困倦: 向下弯的曲线
//   抱歉: 竖椭圆眼眶 + 瞳孔朝上
//   思考: 竖椭圆瞳孔+眼镜框（右眼带眨眼）
//   隐私: 闭合弧线
//   custom "grieved"（委屈）: 三个纵向椭圆共享同一个最上端顶点——从外到内
//     依次是空心眼眶、实心瞳孔、无描边的空心高光
//
// 切换到不同"眼睛款式"时，会用 0→1 的缩放在 500ms 内把新款式画出来
// （旧款式所在的表情消失的同时，新款式从中心"长"出来）。

class PuppyEye : public Drawable {
  bool isLeft;  // true=屏幕左侧的眼睛, false=屏幕右侧

  // 眼睛款式编号：0隐私 1兴奋/peekaboo 2思考 3开心 4困倦 5抱歉
  // 6常态/生气/好奇 7委屈
  int lastStyle_ = -1;
  unsigned long styleStartMs_ = 0;
  FloatTransition doubtAngleAnim_;

  bool wasExcited_ = false;
  unsigned long excitedStartMs_ = 0;
  FloatTransition eyeInwardAnim_;  // 兴奋：两只眼睛互相靠拢

 public:
  PuppyEye(bool isLeft) : isLeft(isLeft) {}

  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    bool custom = (exp == Expression::Neutral) && g_customExpr.length() > 0;
    bool isExcited = custom && g_customExpr == "excited";
    // peekaboo："基础表情和兴奋一样"——五官尺寸/位置这些跟"兴奋"共用的效果，
    // 都用 exciteLike 统一判断；只有爪印、左眼是否画出来这两点不一样（分别
    // 在 PuppyEar 和下面 style==1 里单独处理，不受 exciteLike 影响）。
    bool isPeekaboo = custom && g_customExpr == "peekaboo";
    bool exciteLike = isExcited || isPeekaboo;
    // peekaboo 比"兴奋"整体再放大 PEEKABOO_SIZE_MUL（10%），乘在所有用到
    // EXCITED_SCALE/EXCITED_*_PX 的地方；真正的"兴奋"不受影响（sizeMul=1）。
    float sizeMul = isPeekaboo ? PEEKABOO_SIZE_MUL : 1.0f;
    unsigned long excitedElapsed = elapsedSinceTrue(isExcited, wasExcited_, excitedStartMs_);

    float doubtAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD * doubtMirrorSign() : 0.0f);
    applyRotationAroundPivot(doubtAngle, DOUBT_PIVOT_X, DOUBT_PIVOT_Y, cx, cy);
    float rotTotal = doubtAngle;

    // 兴奋/peekaboo：两只眼睛各自朝对方靠拢一点（左眼往右挪，右眼往左挪）。
    int eyeInward = (int)roundf(eyeInwardAnim_.update(exciteLike ? EXCITED_EYE_INWARD_PX * sizeMul : 0.0f));
    cx += isLeft ? eyeInward : -eyeInward;

    uint16_t col = ctx->getColorDepth() == 1
                       ? 1
                       : ctx->getColorPalette()->get(COLOR_PRIMARY);

    // 视线偏移（Avatar 库的 Gaze 系统）
    Gaze g = isLeft ? ctx->getLeftGaze() : ctx->getRightGaze();
    int ox = g.getHorizontal() * 3;
    int oy = g.getVertical() * 3;

    int style;
    if (custom && g_customExpr == "privacy") style = 0;
    else if (exciteLike) style = 1;
    else if (custom && g_customExpr == "thinking") style = 2;
    else if (exp == Expression::Happy) style = 3;
    else if (exp == Expression::Sleepy) style = 4;
    else if (exp == Expression::Sad) style = 5;
    else if (custom && g_customExpr == "grieved") style = 7;
    else style = 6;

    unsigned long now = millis();
    if (style != lastStyle_) {
      lastStyle_ = style;
      styleStartMs_ = now;
    }
    float s = (float)(now - styleStartMs_) / (float)FloatTransition::DURATION_MS;
    if (s < 0.0f) s = 0.0f;
    if (s > 1.0f) s = 1.0f;

    // ---- 隐私：闭眼弧线 ----
    if (style == 0) {
      int hx = (int)roundf(8 * s);
      int hy = (int)roundf(4 * s);
      for (int t = -1; t <= 1; t++) {
        spi->drawLine(cx - hx, cy + t, cx, cy + hy + t, col);
        spi->drawLine(cx, cy + hy + t, cx + hx, cy + t, col);
      }
      return;
    }

    // ---- 兴奋：'><' 眉眼形——左眼是 '>'，右眼是 '<'，静态不转，整体按
    //      EXCITED_SCALE 缩小后再按 EXCITED_EYE_BOOST 放大一点；进入兴奋
    //      EXCITED_BLINK_SWING_START_MS 之前眼睛保持全开，之后才开始跟随正常
    //      的自动眨眼节奏。openRatio>=0.5 时画平时的'><'折线（随 openRatio 连续
    //      收窄）；openRatio<0.5（快闭眼/闭眼）时改画一条微微鼓出的竖向弧线，
    //      左右眼各自往相反方向鼓，类似"）（"，而不是收成一条横线或者一个点。----
    if (style == 1) {
      // peekaboo：左耳盖住了左眼（见 PuppyEar 的特殊耳型），左眼的 '>' 不需要
      // 画出来——直接返回，右眼照常画。
      if (isPeekaboo && isLeft) return;
      float openRatio = 1.0f;
      if (isExcited && excitedElapsed >= EXCITED_BLINK_SWING_START_MS) {
        openRatio = isLeft ? ctx->getLeftEyeOpenRatio() : ctx->getRightEyeOpenRatio();
      }
      if (openRatio < 0.5f) {
        int hh = (int)roundf(6 * EXCITED_SCALE * sizeMul * EXCITED_EYE_BOOST * s);   // 弧线竖直方向的半高
        int bow = (int)roundf(2 * EXCITED_SCALE * sizeMul * EXCITED_EYE_BOOST * s);  // 弧线中间鼓出的幅度
        int bowX = isLeft ? bow : -bow;  // 左右眼往相反方向鼓，类似"）（"
        for (int t = -1; t <= 1; t++) {
          spi->drawBezier(cx + t, cy - hh, cx + bowX + t, cy, cx + t, cy + hh, col);
        }
        return;
      }
      int width = (int)roundf(7 * EXCITED_SCALE * sizeMul * EXCITED_EYE_BOOST * s);
      int a = (int)roundf(width * openRatio);
      int b = (int)roundf(4 * EXCITED_SCALE * sizeMul * EXCITED_EYE_BOOST * s);
      int armX = isLeft ? -width : width;   // 张开的两条边朝向的一侧（固定，不随眨眼变化）
      int tipX = isLeft ? b : -b;           // 尖角（顶点）朝向的一侧
      for (int t = -1; t <= 1; t++) {
        spi->drawLine(cx + armX, cy - a + t, cx + tipX, cy + t, col);
        spi->drawLine(cx + tipX, cy + t, cx + armX, cy + a + t, col);
      }
      return;
    }

    // ---- 思考：圆点 + 圆形眼镜框（放大），右眼带眨眼动画 ----
    if (style == 2) {
      int r1 = (int)roundf(19 * s);
      int r2 = (int)roundf(20 * s);
      int r3 = (int)roundf(21 * s);
      spi->drawCircle(cx, cy, r1, col);
      spi->drawCircle(cx, cy, r2, col);
      spi->drawCircle(cx, cy, r3, col);
      // 右眼眨眼：闭眼时画 "<" 形状，模拟单侧眨眼
      if (!isLeft) {
        float openRatio = ctx->getRightEyeOpenRatio();
        if (openRatio < 0.5f) {
          int a = (int)roundf(5 * s);
          int b = (int)roundf(3 * s);
          for (int t = -1; t <= 1; t++) {
            spi->drawLine(cx + ox + a + t, cy + oy - a, cx + ox - b + t, cy + oy, col);
            spi->drawLine(cx + ox - b + t, cy + oy, cx + ox + a + t, cy + oy + a, col);
          }
          return;
        }
      }
      // 瞳孔：实心竖椭圆（比圆点更细长）
      spi->fillEllipse(cx + ox, cy + oy, (int)roundf(4 * s), (int)roundf(7 * s), col);
      return;
    }

    // ---- 开心：^^ 弧线 ----
    if (style == 3) {
      int hx = (int)roundf(10 * s);
      int hy = (int)roundf(5 * s);
      for (int t = 0; t < 3; t++) {
        spi->drawLine(cx - hx, cy + hy + t, cx, cy - hy + t, col);
        spi->drawLine(cx, cy - hy + t, cx + hx, cy + hy + t, col);
      }
      return;
    }

    // ---- 困倦：向下弯的曲线（")" 顺时针转 90° 的效果） ----
    if (style == 4) {
      int hw = (int)roundf(15 * s);
      int depth = (int)roundf(7 * s);
      for (int t = -1; t <= 1; t++) {
        spi->drawBezier(cx - hw, cy + t, cx, cy + depth + t, cx + hw, cy + t, col);
      }
      return;
    }

    // ---- 抱歉：竖椭圆眼眶 + 瞳孔朝上（puppy eyes，眼眶形状与 neutral 一致） ----
    if (style == 5) {
      int rx1 = (int)roundf(10 * s), ry1 = (int)roundf(16 * s);
      int rx2 = (int)roundf(11 * s), ry2 = (int)roundf(17 * s);
      spi->drawEllipse(cx, cy, rx1, ry1, col);
      spi->drawEllipse(cx, cy, rx2, ry2, col);
      // 瞳孔向上偏移（怯怯地往上看），两眼同向（都靠左），不再镜像对称
      int pupilOx = (int)roundf(-2 * s);
      int pupilOy = (int)roundf(-4 * s);
      spi->fillCircle(cx + pupilOx, cy + pupilOy, (int)roundf(6 * s), col);
      return;
    }

    // ---- 委屈：三个同心正圆共享同一个最上端顶点（cx, topY）——从外到内
    //      依次是空心眼眶（描边）、实心瞳孔、无描边的空心高光。三个圆
    //      各自的中心 = topY + 自己的半径，半径越小中心越靠上，天然做出
    //      "层层嵌在最上端"的效果，不需要额外算相交逻辑。上方再加一条
    //      "担心"眉毛。----
    if (style == 7) {
      // 眼眶半径的换算基准应该是原始椭圆里较大的那个维度（ry1=16，眼眶本来
      // 是"瘦高"的竖椭圆），上一轮改成正圆时误用了较小的 rx1=9 做基准，导致
      // 眼眶（10px）反而比瞳孔（12px）还小——这一轮顺手修正过来。
      int r1 = (int)roundf(16 * 0.9f * GRIEVED_EYE_SCALE * GRIEVED_SOCKET_SCALE * s);     // 眼眶（空心，正圆，比最初小10%、整体放大20%后又缩小20%）
      int r2 = (int)roundf(10 * 0.95f * GRIEVED_EYE_SCALE * s);                            // 瞳孔（实心，正圆，再缩小5%）
      int r3 = (int)roundf(5.2f * GRIEVED_EYE_SCALE * GRIEVED_HIGHLIGHT_SCALE * s);        // 高光（无描边，正圆，再缩小5%）
      int topY = cy - r1;
      int highlightGap = (int)roundf(3 * GRIEVED_EYE_SCALE * s);  // 高光中心比瞳孔顶边再往下挪这么多，
                                                                    // 不要贴着瞳孔的边

      spi->drawCircle(cx, topY + r1, r1, col);
      if (r1 > 1) {
        spi->drawCircle(cx, topY + r1, r1 - 1, col);  // 加粗描边
      }
      spi->fillCircle(cx, topY + r2, r2, col);
      if (r3 > 0) {
        uint16_t bgCol = ctx->getColorDepth() == 1
                             ? ERACER_COLOR
                             : ctx->getColorPalette()->get(COLOR_BACKGROUND);
        spi->fillCircle(cx, topY + highlightGap + r3, r3, bgCol);
      }

      // ---- 眉毛：眼眶上方一条弧线，形状是"（"逆时针转90°——两端跟原来
      //      一样有高低差（靠鼻子一侧更高），但弧线中点要往下凹（而不是
      //      上一版的往上拱），整条弧线看起来像"⌣"（两端翘、中间低），
      //      不是"⌢"。dir 沿用 eyeInward/bowX 那套镜像约定（isLeft 时
      //      +cx 方向是"靠近鼻子"），如果实机看起来方向反了（变成挑向
      //      外侧、像生气而不是担心），把 dir 的符号取反即可。----
      int dir = isLeft ? 1 : -1;
      int halfW = (int)roundf(GRIEVED_BROW_HALF_W * s);
      int tilt = (int)roundf(GRIEVED_BROW_TILT * s);
      int gap = (int)roundf(GRIEVED_BROW_GAP * s);
      int arc = (int)roundf(GRIEVED_BROW_ARC * s);
      int browY = topY - gap;
      int innerX = cx + dir * halfW, innerY = browY - tilt;
      int outerX = cx - dir * halfW, outerY = browY + tilt;
      int midX = (innerX + outerX) / 2;
      int midY = (innerY + outerY) / 2 + arc;
      for (int t = -1; t <= 1; t++) {
        spi->drawBezier(innerX, innerY + t, midX, midY + t, outerX, outerY + t, col);
      }
      return;
    }

    // ---- 常态 / 生气 / 好奇：竖向椭圆，带眨眼动画；好奇时椭圆本身也跟着转 ----
    float openRatio = isLeft ? ctx->getLeftEyeOpenRatio() : ctx->getRightEyeOpenRatio();
    int rx = (int)roundf(8 * s);
    int ry = max(1, (int)(13 * openRatio * s));
    if (rotTotal != 0.0f) {
      fillRotatedEllipse(spi, cx + ox, cy + oy, rx, ry, rotTotal, col);
    } else {
      spi->fillEllipse(cx + ox, cy + oy, rx, ry, col);
    }
  }
};

// ╔══════════════════════════════════════════════╗
// ║         PuppyNose 小狗鼻子+嘴巴+舌头          ║
// ╚══════════════════════════════════════════════╝
//
// 替代 Mouth 组件。所有表情都画：
//   上方: 椭圆形鼻子
//   下方: 两条弧线嘴巴（W 形 / 胡须形）
//   兴奋时额外画一个 U 型舌头，两端精确落在嘴巴弧线自己的曲线上（不是估算的
//   附近位置），和嘴巴弧线一起围成一个封闭的空间；舌头宽度正好是嘴巴宽度的一半。
//
// 鼻子大小、位置偏移（隐私时）、嘴巴弧线宽度/深度都用 FloatTransition
// 平滑过渡；好奇时鼻子和嘴巴自身也跟着顺时针转；兴奋时鼻子连带嘴巴/舌头整体
// 朝眼睛方向靠拢 EXCITED_NOSE_UP_PX 像素。

class PuppyNose : public Drawable {
  FloatTransition rxAnim_, ryAnim_, offXAnim_, offYAnim_;
  FloatTransition cwAnim_, cdAnim_;
  FloatTransition doubtAngleAnim_;
  FloatTransition noseUpAnim_;  // 兴奋：鼻子（连带嘴巴/舌头）整体朝眼睛方向靠拢

 public:
  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    uint16_t col = ctx->getColorDepth() == 1
                       ? 1
                       : ctx->getColorPalette()->get(COLOR_PRIMARY);

    bool custom = (exp == Expression::Neutral) && g_customExpr.length() > 0;
    bool isPrivacy = custom && g_customExpr == "privacy";
    bool isExcited = custom && g_customExpr == "excited";
    bool isGrieved = custom && g_customExpr == "grieved";
    // peekaboo："基础表情和兴奋一样"——鼻子/嘴巴/舌头这些跟"兴奋"共用同一套
    // 参数，用 exciteLike 统一判断；跟 PuppyEye 里的用法保持一致。
    bool isPeekaboo = custom && g_customExpr == "peekaboo";
    bool exciteLike = isExcited || isPeekaboo;
    // peekaboo 比"兴奋"整体再放大 PEEKABOO_SIZE_MUL（10%），乘在所有用到
    // EXCITED_SCALE/EXCITED_*_PX 的地方；真正的"兴奋"不受影响（sizeMul=1）。
    float sizeMul = isPeekaboo ? PEEKABOO_SIZE_MUL : 1.0f;
    // peekaboo 的鼻子/嘴巴要在上面那个整体放大的基础上再缩小一档，跟
    // sizeMul 是两件独立的事，只乘在鼻子/嘴巴相关的目标值上，不影响
    // "兴奋"本身（isExcited 时这个系数恒为1）。
    float noseMouthMul = isPeekaboo ? PEEKABOO_NOSE_MOUTH_SCALE : 1.0f;

    // ---- 目标参数 ----
    float targetRx = isPrivacy
                          ? 9.0f
                          : (exciteLike ? 10.0f * EXCITED_SCALE * sizeMul * noseMouthMul
                                        : (isGrieved ? 10.0f * GRIEVED_NOSE_SCALE : 10.0f));
    float targetRy = isPrivacy
                          ? 6.0f
                          : (exciteLike ? 7.0f * EXCITED_SCALE * sizeMul * noseMouthMul
                                        : (isGrieved ? 7.0f * GRIEVED_NOSE_SCALE : 7.0f));
    float targetOffX = isPrivacy ? -8.0f : 0.0f;
    float targetOffY = isPrivacy ? -5.0f : 0.0f;

    float targetCw = 16.0f, targetCd = 8.0f;  // 标准嘴巴弧线
    if (isPrivacy || isGrieved) {
      // 隐私、委屈都不画嘴巴：把目标收成 0，过渡时会看到嘴巴慢慢收起来
      targetCw = 0.0f;
      targetCd = 0.0f;
    } else if (exp == Expression::Happy) {
      targetCw = 20.0f;
      targetCd = 12.0f;
    } else if (exciteLike) {
      targetCw = 20.0f * EXCITED_SCALE * sizeMul * noseMouthMul;
      targetCd = 12.0f * EXCITED_SCALE * sizeMul * noseMouthMul;
    } else if (exp == Expression::Sleepy) {
      targetCw = 10.0f;
      targetCd = 5.0f;
    }

    // ---- 过渡插值 ----
    float rxF = rxAnim_.update(targetRx);
    float ryF = ryAnim_.update(targetRy);
    float offXF = offXAnim_.update(targetOffX);
    float offYF = offYAnim_.update(targetOffY);
    float curveWidthF = cwAnim_.update(targetCw);
    float curveDepthF = cdAnim_.update(targetCd);
    float rotAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD * doubtMirrorSign() : 0.0f);

    // 好奇表情下鼻子/嘴巴自身也跟着转。枢轴就是鼻子自己的锚点，
    // 所以鼻子中心的位置不动，但形状（椭圆朝向、嘴巴弧线）会跟着转。
    applyRotationAroundPivot(rotAngle, DOUBT_PIVOT_X, DOUBT_PIVOT_Y, cx, cy);

    // ---- 兴奋/peekaboo：鼻子连带嘴巴/舌头整体往上挪，贴近眼睛（眼睛的锚点 y
    //      比鼻子小，即在眼睛上方；这里统一改 cy，鼻子和嘴巴会一起挪动，不会
    //      跟嘴巴脱节）。委屈：鼻子也朝眼睛方向靠拢一点，幅度单独调（不是
    //      exciteLike，不共用 EXCITED_NOSE_UP_PX）----
    float noseCloserPx = exciteLike ? EXCITED_NOSE_UP_PX * sizeMul
                                     : (isGrieved ? GRIEVED_NOSE_CLOSER_PX : 0.0f);
    float noseUp = noseUpAnim_.update(noseCloserPx);
    cy -= (int)roundf(noseUp);

    // ---- 画鼻子 ----
    int noseCx = cx + (int)roundf(offXF);
    int noseCy = cy + (int)roundf(offYF);
    int rx = (int)roundf(rxF);
    int ry = (int)roundf(ryF);
    if (rotAngle != 0.0f) {
      fillRotatedEllipse(spi, noseCx, noseCy, rx, ry, rotAngle, col);
    } else {
      spi->fillEllipse(noseCx, noseCy, rx, ry, col);
    }

    // ---- 画嘴巴（弧线宽度趋近 0 时不画，隐私状态下最终会完全收起）----
    int curveWidth = (int)roundf(curveWidthF);
    int curveDepth = (int)roundf(curveDepthF);
    if (curveWidth < 1) return;

    int mouthOffY = exciteLike ? (int)roundf(10 * EXCITED_SCALE * sizeMul) : 10;  // 嘴巴起点相对鼻子中心的 Y 偏移
    for (int t = -1; t <= 1; t++) {
      int p0x, p0y, l1x, l1y, l2x, l2y, r1x, r1y, r2x, r2y;
      rotateLocalOffset(rotAngle, 0, mouthOffY + t, p0x, p0y);
      rotateLocalOffset(rotAngle, -curveWidth / 2, mouthOffY + curveDepth + t, l1x, l1y);
      rotateLocalOffset(rotAngle, -curveWidth, mouthOffY + 2 + t, l2x, l2y);
      rotateLocalOffset(rotAngle, curveWidth / 2, mouthOffY + curveDepth + t, r1x, r1y);
      rotateLocalOffset(rotAngle, curveWidth, mouthOffY + 2 + t, r2x, r2y);

      // 左弧线
      spi->drawBezier(cx + p0x, cy + p0y, cx + l1x, cy + l1y, cx + l2x, cy + l2y, col);
      // 右弧线
      spi->drawBezier(cx + p0x, cy + p0y, cx + r1x, cy + r1y, cx + r2x, cy + r2y, col);
    }

    // ---- 兴奋/peekaboo：加一个 U 型舌头，一直跟嘴巴一起显示。宽度正好是嘴巴
    //      宽度的一半（tongueHalfW = curveWidth/2，和嘴巴弧线控制点的 x 完全
    //      一样）；两端 y 坐标用二次贝塞尔的精确公式算出嘴巴弧线在这个 x 上的
    //      真实位置（不是估算值），保证舌头和嘴巴严丝合缝地连在一起、围成一个
    //      封闭的空间。----
    if (exciteLike) {
      int tongueHalfW = curveWidth / 2;
      // 嘴巴弧线是二次贝塞尔 (0,mouthOffY) -> (curveWidth/2, mouthOffY+curveDepth)
      // -> (curveWidth, mouthOffY+2)，在 x=curveWidth/2 处（t=0.5）的精确 y：
      float tongueAttachY = mouthOffY + curveDepth * 0.5f + 0.5f;
      const int tongueDepth = (int)roundf(8 * EXCITED_SCALE * sizeMul);  // U 形往下鼓出的深度
      for (int t = -1; t <= 1; t++) {
        int t0x, t0y, t1x, t1y, t2x, t2y;
        rotateLocalOffset(rotAngle, -tongueHalfW, tongueAttachY + t, t0x, t0y);
        rotateLocalOffset(rotAngle, 0, tongueAttachY + tongueDepth + t, t1x, t1y);
        rotateLocalOffset(rotAngle, tongueHalfW, tongueAttachY + t, t2x, t2y);
        spi->drawBezier(cx + t0x, cy + t0y, cx + t1x, cy + t1y, cx + t2x, cy + t2y, col);
      }
    }
  }
};

// ╔══════════════════════════════════════════════╗
// ║           PuppyEar 小狗耷拉耳朵              ║
// ╚══════════════════════════════════════════════╝
//
// 替代 Eyebrow 组件。画 U 形耷拉耳朵。
// 用两段贝塞尔曲线拼成一个 U 形。
// 不同表情改变耳朵的长度、宽度和不对称度，切换表情时这些尺寸用
// FloatTransition 平滑过渡；开心/好奇/思考时耳朵左右轻轻摆动。
//
// 好奇：整体绕鼻子锚点顺时针转 15°；主体转完 500ms 后，右耳再单独绕自己顶部
// 端点缓入逆时针转 15°。
//
// 兴奋：耳朵在"开心"短耳基础上再按 EXCITED_SCALE 缩小，并朝眼睛方向靠拢
// EXCITED_EAR_INWARD_PX 像素，不转。EXCITED_BLINK_SWING_START_MS 之前静止不动，
// 之后开始左右轻轻摆动（跟眼睛的眨眼动画同时开始）。爪印动画的时间线——
//   1. 保持静态姿势到 EXCITED_PAW_START_MS，不显示任何爪印；
//   2. 之后左爪出现、消失，右爪出现、消失，如此交替 EXCITED_PAW_CYCLES 轮，
//      每一段显示的时长在 [EXCITED_PAW_MIN_MS, EXCITED_PAW_MAX_MS] 内随机；
//      每只爪印每次开始显示时，位置都会在基准位置上叠加一点随机抖动
//      （幅度 ±EXCITED_PAW_JITTER_PX 像素）；
//   3. 最后左右爪一起出现并常驻，直到离开兴奋表情（此时也会重新抖动一次位置），
//      常驻之后两只爪印会一起微微左右摇晃。
// 重新进入兴奋表情时，爪印状态机和缓动状态都会硬重置，不会播放上一次残留的
// "消失动画"。爪印在 EXCITED_SCALE 的基础上再额外缩小 20%（EXCITED_PAW_SCALE），
// 但爪印内部脚趾间距、两只爪印之间的间距、爪印与五官的距离都相应加大了，
// 避免整体缩小后挤在一起看不清。

class PuppyEar : public Drawable {
  bool isLeft;  // true=屏幕左侧耳朵, false=屏幕右侧

  FloatTransition lenAnim_, wAnim_, topYAnim_;
  FloatTransition doubtAngleAnim_;
  FloatTransition earTwistAnim_;        // 好奇：主体旋转结束后，"长耳"额外多转 15°
                                         // ——哪只耳朵是"长耳"由 doubtMirrorSign()
                                         // 决定，不固定是左耳还是右耳，见 draw()
  bool wasDoubt_ = false;
  unsigned long doubtStartMs_ = 0;

  bool wasExcited_ = false;
  unsigned long excitedStartMs_ = 0;
  FloatTransition earInwardAnim_;  // 兴奋：耳朵朝眼睛方向靠拢
  // 兴奋：左右爪印各自的出现/消失缓动。用比默认 500ms 更短的 150ms，确保就算
  // 交替节奏很快（最短 EXCITED_PAW_MIN_MS=300ms 一段）也能在下一次轮到它之前
  // 完整淡出，不会出现"上一次淡出还没走完，新一轮又换了位置"的跳变。
  FloatTransition leftPawAnim_{150}, rightPawAnim_{150};

  // 关键词播报按钮的缩放动画（只在 !isLeft 的耳朵实例上使用）：主机端
  // down→短暂停留→up 的调用节奏，靠这个 150ms 的过渡动画渲染成"按一下"的
  // 缩小再放大效果。
  FloatTransition buttonScaleAnim_{150};

  // 兴奋表情爪印的状态机（只在 !isLeft 的耳朵实例上使用）：
  //   -1 = 还没到 EXCITED_PAW_START_MS，都不显示
  //   0..2*EXCITED_PAW_CYCLES = 左右交替阶段（偶数=左爪，奇数=右爪），最后一个
  //     偶数下标固定停在左爪，让左爪先"转正"为常驻，避免最后一步同时改两只爪
  //   totalSteps(=2*EXCITED_PAW_CYCLES+1) = 右爪也常驻出现，左右都稳定显示
  int pawPhaseIdx_ = -1;
  unsigned long pawPhaseStartMs_ = 0;     // 当前阶段起点（相对 excitedElapsed 的时间基准）
  unsigned long pawPhaseDurationMs_ = 0;  // 当前交替阶段随机出的持续时长
  int pawJitterX_[2] = {0, 0};            // 每只爪印当前这次出现时的位置随机抖动 [0]=左 [1]=右
  int pawJitterY_[2] = {0, 0};
  float pawRotDeg_[2] = {0, 0};           // 每只爪印当前这次出现时的整体旋转角度（度），[0]=左 [1]=右

 public:
  PuppyEar(bool isLeft) : isLeft(isLeft) {}

  void drawThickBezier(M5Canvas *spi,
                       int x0, int y0, int x1, int y1, int x2, int y2,
                       int thickness, uint16_t col) {
    for (int t = -(thickness / 2); t <= thickness / 2; t++) {
      spi->drawBezier(x0 + t, y0, x1 + t, y1, x2 + t, y2, col);
    }
  }

  // 画一个写实一点的爪印：一个大脚掌 + 4 个小脚趾（用旋转椭圆近似，从内到外
  // 逐渐往外翘）。mirror=true 时脚趾左右镜像（用于右爪）。scale 控制从 0 长到
  // 1 / 从 1 缩到 0 的出现与消失动画。rotRad 让整只爪印（脚掌+脚趾）绕爪印
  // 中心整体转一点（左爪逆时针、右爪顺时针，每次出现时角度略有不同）。
  // toeSpreadMul 在基础间距系数上再乘一个倍数，默认 1.0（不影响原有的兴奋
  // 表情爪印），关键词播报按钮传更大的值让爪印内部脚趾与脚掌的间距更松散。
  void drawPawPrint(M5Canvas *spi, int cx, int cy, float scale, bool mirror, float rotRad, uint16_t col,
                     float toeSpreadMul = 1.0f) {
    if (scale <= 0.02f) return;
    int m = mirror ? -1 : 1;
    const float toeSpread = 1.6f * toeSpreadMul;  // 脚趾彼此之间的间距放大系数（只放大位置，不放大半径），以脚掌为中心向外放大

    // ---- 大脚掌：三角形的基本轮廓，边缘用沿着每条边连续叠圆的方式磨光滑——
    //      半径从顶点处的 padCornerR 平滑过渡到边中点的 padBulgeR 再过渡回
    //      padCornerR（sin 曲线，没有突变），这样整条边看起来是一条连续膨出的
    //      弧线，不会在顶点圆和中点圆之间露出直线的"腰身"；采样点数比上一版更
    //      多，边缘更光滑。整体面积比最初版本累计缩小约 10%（两次各 5%，
    //      PAD_SIZE_SCALE = sqrt(0.95)*sqrt(0.95) = 0.95，长度方向缩小 5%）。
    //      顶点整体保持在远离脚趾一侧挪了一点的位置，跟脚趾之间留出间隙。
    //      所有坐标先按 scale 缩放，再整体绕爪印中心转 rotRad（跟脚趾一起转）。----
    const float PAD_SIZE_SCALE = 0.95f;  // 两轮各 5% 面积缩小的累计线性比例
    const float padLocal[3][2] = {
        {0.0f, -4.0f},   // 顶点：朝向脚趾一侧
        {-9.0f, 10.0f},  // 左下角
        {9.0f, 10.0f},   // 右下角
    };
    float padScale = scale * PAD_SIZE_SCALE;
    int padPx[3], padPy[3];
    for (int i = 0; i < 3; i++) {
      int rx, ry;
      rotateLocalOffset(rotRad, padLocal[i][0] * padScale, padLocal[i][1] * padScale, rx, ry);
      padPx[i] = cx + rx;
      padPy[i] = cy + ry;
    }
    spi->fillTriangle(padPx[0], padPy[0], padPx[1], padPy[1], padPx[2], padPy[2], col);
    int padCornerR = max(1, (int)roundf(3.0f * padScale));
    int padBulgeR = max(1, (int)roundf(5.0f * padScale));
    const int EDGE_STEPS = 8;  // 每条边采样点数（含两端），越多边缘越光滑
    for (int i = 0; i < 3; i++) {
      int j = (i + 1) % 3;
      for (int k = 0; k <= EDGE_STEPS; k++) {
        float t = (float)k / EDGE_STEPS;
        int px = padPx[i] + (int)roundf((padPx[j] - padPx[i]) * t);
        int py = padPy[i] + (int)roundf((padPy[j] - padPy[i]) * t);
        float r = padCornerR + (padBulgeR - padCornerR) * sinf(t * PI);
        spi->fillCircle(px, py, max(1, (int)roundf(r)), col);
      }
    }

    // 4 个脚趾（比之前更小一圈）：{ 局部x偏移, 局部y偏移, 半径x, 半径y, 倾斜角度(度) }。
    // 局部偏移和半径都以脚掌为中心衡量，放大 toeSpread 只放大偏移不放大半径，
    // 所以脚趾始终围绕同一个中心（脚掌）分布，只是彼此之间挨得更远。
    const float toes[4][5] = {
        {-11.0f, -6.0f,  3.0f, 4.5f, -25.0f},
        { -4.0f, -12.0f, 3.2f, 4.8f, -8.0f},
        {  4.0f, -12.0f, 3.2f, 4.8f,  8.0f},
        { 11.0f, -6.0f,  3.0f, 4.5f,  25.0f},
    };
    for (int i = 0; i < 4; i++) {
      int rx, ry;
      rotateLocalOffset(rotRad, m * toes[i][0] * toeSpread * scale, toes[i][1] * toeSpread * scale, rx, ry);
      int tx = cx + rx;
      int ty = cy + ry;
      int trx = (int)roundf(toes[i][2] * scale);
      int try_ = (int)roundf(toes[i][3] * scale);
      float ang = m * toes[i][4] * PI / 180.0f + rotRad;
      fillRotatedEllipse(spi, tx, ty, trx, try_, ang, col);
    }
  }

  // 给某只爪印（which: 0=左 1=右）重新抽一次这次出现要用的位置随机抖动
  // （±EXCITED_PAW_JITTER_PX 像素）和整体旋转角度（左爪基准逆时针、右爪基准
  // 顺时针，在基准角度上再叠加 ±EXCITED_PAW_ROT_JITTER_DEG 度的随机浮动）。
  void randomizePawAppearance(int which) {
    pawJitterX_[which] = random(-EXCITED_PAW_JITTER_PX, EXCITED_PAW_JITTER_PX + 1);
    pawJitterY_[which] = random(-EXCITED_PAW_JITTER_PX, EXCITED_PAW_JITTER_PX + 1);
    float base = (which == 0) ? -EXCITED_PAW_ROT_BASE_DEG : EXCITED_PAW_ROT_BASE_DEG;
    const int jitterTenths = (int)roundf(EXCITED_PAW_ROT_JITTER_DEG * 10.0f);
    float jitter = random(-jitterTenths, jitterTenths + 1) / 10.0f;
    pawRotDeg_[which] = base + jitter;
  }

  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    bool custom = (exp == Expression::Neutral) && g_customExpr.length() > 0;
    bool isExcited = custom && g_customExpr == "excited";
    // peekaboo："基础表情和兴奋一样"——耳朵尺寸/靠拢这些跟"兴奋"共用的效果，
    // 用 exciteLike 统一判断；爪印只认真正的 isExcited（下面 931 行附近那个
    // 判断没有改），左耳的形状单独特殊处理（见下面 isPeekaboo && isLeft）。
    bool isPeekaboo = custom && g_customExpr == "peekaboo";
    bool exciteLike = isExcited || isPeekaboo;
    bool isGrieved = custom && g_customExpr == "grieved";
    // peekaboo 比"兴奋"整体再放大 PEEKABOO_SIZE_MUL（10%），乘在所有用到
    // EXCITED_SCALE/EXCITED_*_PX 的地方；真正的"兴奋"不受影响（sizeMul=1）。
    float sizeMul = isPeekaboo ? PEEKABOO_SIZE_MUL : 1.0f;
    bool wasExcitedBefore = wasExcited_;
    unsigned long excitedElapsed = elapsedSinceTrue(isExcited, wasExcited_, excitedStartMs_);
    if (isExcited && !wasExcitedBefore) {
      // 刚重新进入兴奋表情：硬重置爪印状态，不要把上一次残留的爪印缓动
      // 状态当成"消失动画"播放一遍。
      leftPawAnim_.reset(0.0f);
      rightPawAnim_.reset(0.0f);
      pawPhaseIdx_ = -1;
      pawPhaseStartMs_ = 0;
    }

    float doubtAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD * doubtMirrorSign() : 0.0f);
    applyRotationAroundPivot(doubtAngle, DOUBT_PIVOT_X, DOUBT_PIVOT_Y, cx, cy);
    float rotTotal = doubtAngle;

    uint16_t col = ctx->getColorDepth() == 1
                       ? 1
                       : ctx->getColorPalette()->get(COLOR_PRIMARY);

    // ===== 目标参数 =====
    int earLenTarget = 70;   // 耳朵长度（向下垂多少）
    int earWTarget = 35;     // 耳朵宽度
    int topYTarget = -25;    // 耳朵顶部相对中心的偏移
    const int thick = 3;     // 线条粗细

    // 左右镜像
    int dir = isLeft ? -1 : 1;
    // 好奇表情下哪只耳朵是"长耳"（更长、主体旋转结束后再额外多转一点）由
    // doubtMirrorSign() 决定——正常方向时是右耳（原本的设计），镜像时变成
    // 左耳，跟主体旋转方向一起镜像，两只耳朵的角色互换而不是各自独立翻转。
    bool doubtLongEarIsLeft = doubtMirrorSign() < 0.0f;
    bool isLongEar = (isLeft == doubtLongEarIsLeft);

    if (exp == Expression::Happy) {
      earLenTarget = 55;   // 开心时耳朵短一些（更精神）
      topYTarget = -30;
    } else if (exciteLike) {
      // 兴奋/peekaboo：耳朵在"开心"短耳基础上整体再按 EXCITED_SCALE 缩小
      // （peekaboo 额外再乘 sizeMul 整体放大一点）
      earLenTarget = (int)roundf(55 * EXCITED_SCALE * sizeMul);
      earWTarget = (int)roundf(earWTarget * EXCITED_SCALE * sizeMul);
      topYTarget = (int)roundf(-30 * EXCITED_SCALE * sizeMul);
    }
    if (exp == Expression::Sleepy) {
      earLenTarget = 80;   // 困倦时更长更垂，两侧耳朵保持相同大小
    }
    if (isGrieved) {
      // 委屈：耳朵整体往下移一点，缩短跟眼睛的距离（topY 越接近 0 越往下）
      topYTarget += GRIEVED_EAR_CLOSER_PX;
    }
    if (exp == Expression::Doubt) {
      // 好奇：两只耳朵先整体缩小到常态的 70%（缩放后的目标值变化会被下面的
      // FloatTransition 自动缓动出来），"垂耳"再额外垂一点、"长耳"再额外长
      // 一点（哪只耳朵扮演哪个角色见上面 isLongEar），同时整体也绕鼻子锚点
      // 旋转（见上方 applyRotationAroundPivot，方向随 doubtMirrorSign() 镜像）。
      const float DOUBT_EAR_SCALE = 0.7f;
      earLenTarget = (int)roundf(earLenTarget * DOUBT_EAR_SCALE);
      earWTarget = (int)roundf(earWTarget * DOUBT_EAR_SCALE);

      const int DOUBT_EAR_DROOP_BASE = 12;
      const int DOUBT_EAR_LONG_EXTRA = 15;  // "长耳" earLen 比"垂耳"多 15px
      if (isLongEar) {
        earLenTarget += DOUBT_EAR_DROOP_BASE + DOUBT_EAR_LONG_EXTRA;
      } else {
        earLenTarget += DOUBT_EAR_DROOP_BASE;
        topYTarget += 5;
      }
    }
    if (exp == Expression::Sad) {
      // 抱歉：耳朵翻过头顶
      earLenTarget = 75;
      if (!isLeft) {
        topYTarget = -35;    // 右耳更高
        earWTarget += 10;    // 更宽（翻过来的感觉）
      }
    }
    if (custom && g_customExpr == "privacy") {
      // 隐私：耳朵变形（像枕着一只耳朵）
      if (isLeft) {
        earLenTarget = 45;
        earWTarget = 25;
      } else {
        earLenTarget = 85;
        earWTarget = 45;
      }
    }

    // ---- 过渡插值 ----
    int earLen = (int)roundf(lenAnim_.update((float)earLenTarget));
    int earW = (int)roundf(wAnim_.update((float)earWTarget));
    int topY = (int)roundf(topYAnim_.update((float)topYTarget));

    // ---- 兴奋/peekaboo：耳朵整体朝眼睛方向靠拢一点（水平方向），静态常驻，
    //      不随时间变化 ----
    int earInward = (int)roundf(earInwardAnim_.update(exciteLike ? EXCITED_EAR_INWARD_PX * sizeMul : 0.0f));
    cx -= dir * earInward;

    // ---- 摆动动画：开心 / 好奇 / 思考时耳朵左右轻轻摆动（水平平移）；
    //      兴奋时要等静态姿势保持 EXCITED_BLINK_SWING_START_MS 之后才开始摆 ----
    bool excitedSwingReady = isExcited && excitedElapsed >= EXCITED_BLINK_SWING_START_MS;
    bool swinging = (exp == Expression::Happy) || (exp == Expression::Doubt) ||
                    (custom && g_customExpr == "thinking") || excitedSwingReady;
    int swing = swinging ? earSwingOffset() : 0;

    // ---- 好奇：主体旋转（500ms）结束以后，"长耳"再额外绕自己的"耳根顶点"
    //      （耳朵最上方的端点，即 x3,y3）单独多转 15°，用自己的
    //      FloatTransition 缓入，和主体旋转错开、不叠加着一起转。哪只耳朵是
    //      "长耳"、转的方向，都随 doubtMirrorSign() 跟主体旋转一起镜像。----
    unsigned long doubtElapsed = elapsedSinceTrue(exp == Expression::Doubt, wasDoubt_, doubtStartMs_);
    bool doubtMainRotationDone = doubtElapsed >= FloatTransition::DURATION_MS;
    const float DOUBT_EAR_TWIST_RAD = -15.0f * PI / 180.0f * doubtMirrorSign();  // 基准逆时针15°，随主体一起镜像
    float twistTarget = (exp == Expression::Doubt && isLongEar && doubtMainRotationDone)
                             ? DOUBT_EAR_TWIST_RAD
                             : 0.0f;
    float earTwist = earTwistAnim_.update(twistTarget);

    float pivotLX = dir * earW;  // 耳根顶点（远离头部一侧的顶部端点）局部坐标
    float pivotLY = topY;

    // ===== peekaboo 左耳：不画平时朝外垂的 U 形，换成一片朝内下方甩过去、
    //      盖住左眼的耳朵（所以 PuppyEye 那边左眼的 '>' 才不用画）。改成跟
    //      正常耳朵一样的"两段贝塞尔"结构（根部→尖端→收尾回来），而不是
    //      只画一段开放的弧线——之前只有一段，视觉上像"缺了一半"；线条粗细
    //      也从 thick+2 改回跟其它线条一致的 thick，不要显得比别处粗。
    //      x 方向的偏移比上一版更靠近中线（cx 越大越靠中间）。topY 已经包含
    //      exciteLike 的整体缩放（含 peekaboo 的 sizeMul），这里只用它当基准
    //      锚点，额外伸出去的量再单独乘一次 sizeMul。这些偏移量还是按耳朵/
    //      眼睛两个锚点的大致相对位置估的，第一次实机看大概率还要再调，改
    //      这几行就行，不影响下面正常耳朵的画法。=====
    if (isPeekaboo && isLeft) {
      // 两段：①耳根直接甩出大幅度向下向内的主弧线盖住眼睛②到底部以后再
      // 勾回来一小段（控制点故意甩得比落点更远，让曲线在靠近主弧线下方的
      // 地方绕回来，形成一个看得出"卷起来"的钩子）。上一版在耳根多画了一个
      // 小勾，反馈说不需要，已去掉，直接从耳根开始大弧线。整体再往外挪
      // PEEKABOO_EAR_AWAY_PX（离中轴线/嘴巴远一点，避免线条重叠）。
      int earCx = cx - (int)roundf(PEEKABOO_EAR_AWAY_PX * sizeMul);

      int rootX = (int)roundf(4 * sizeMul),  rootY = topY + (int)roundf(6 * sizeMul);
      int sweepCtrlX = (int)roundf(32 * sizeMul), sweepCtrlY = topY + (int)roundf(16 * sizeMul);
      int bellyX = (int)roundf(30 * sizeMul), bellyY = topY + (int)roundf(48 * sizeMul);

      int curlCtrlX = (int)roundf(28 * sizeMul), curlCtrlY = topY + (int)roundf(72 * sizeMul);
      int curlEndX = (int)roundf(8 * sizeMul), curlEndY = topY + (int)roundf(56 * sizeMul);

      int fx0 = earCx + rootX,      fy0 = cy + rootY;
      int fxs = earCx + sweepCtrlX, fys = cy + sweepCtrlY;
      int fx2 = earCx + bellyX,     fy2 = cy + bellyY;
      int fxc = earCx + curlCtrlX,  fyc = cy + curlCtrlY;
      int fx3 = earCx + curlEndX,   fy3 = cy + curlEndY;

      drawThickBezier(spi, fx0, fy0, fxs, fys, fx2, fy2, thick, col);
      drawThickBezier(spi, fx2, fy2, fxc, fyc, fx3, fy3, thick, col);
      return;
    }

    // ===== 绘制 U 形耳朵 =====
    //
    // 耳朵由两段贝塞尔曲线组成：
    //   第一段：从耳根顶部 → 向外弯曲 → 到耳朵最低点
    //   第二段：从耳朵最低点 → 向内弯曲 → 回到耳根底部
    //
    //   (x0,y0) ─ 段1 ─► (xBot,yBot) ─ 段2 ─► (x3,y3)
    //       耳根上               耳尖               耳根下
    //
    // 沿耳朵自身中轴线对称翻转：靠近头部（眼睛）一侧的顶部端点更低，
    // 远离头部一侧的顶部端点更高。先算出相对 (cx,cy) 的局部偏移，绕耳根顶点
    // 做一次小铰链旋转（好奇时右耳主体转完后的额外逆时针 15°），再用 rotTotal
    // 整体旋转，最后加上 swing（左右摆动的水平平移，只作用于三个主要端点）。

    int x0, y0, xBot, yBot, x3, y3, ctrl1X, ctrl1Y, ctrl2X, ctrl2Y;
    hingeThenRotate(dir * 5, topY + 15, pivotLX, pivotLY, earTwist, rotTotal, x0, y0);
    hingeThenRotate(dir * (earW / 2.0f), earLen, pivotLX, pivotLY, earTwist, rotTotal, xBot, yBot);
    hingeThenRotate(pivotLX, pivotLY, pivotLX, pivotLY, earTwist, rotTotal, x3, y3);
    hingeThenRotate(-dir * 8, earLen - 15, pivotLX, pivotLY, earTwist, rotTotal, ctrl1X, ctrl1Y);
    hingeThenRotate(dir * (earW + 12), earLen - 15, pivotLX, pivotLY, earTwist, rotTotal, ctrl2X, ctrl2Y);

    x0 += cx + swing;      y0 += cy;
    xBot += cx + swing;    yBot += cy;
    x3 += cx + swing;      y3 += cy;
    ctrl1X += cx;          ctrl1Y += cy;
    ctrl2X += cx;          ctrl2Y += cy;

    // 画两段粗贝塞尔曲线
    drawThickBezier(spi, x0, y0, ctrl1X, ctrl1Y, xBot, yBot, thick, col);
    drawThickBezier(spi, xBot, yBot, ctrl2X, ctrl2Y, x3, y3, thick, col);

    // ===== 思考表情：从右耳组件画眼镜鼻梁 =====
    if (custom && g_customExpr == "thinking" && !isLeft) {
      // 眼镜鼻梁连接线
      // 两个眼睛位置在 x=105, y=135(左)/185(右)，眼镜框外半径 21
      // 鼻梁从左眼镜框右边缘连到右眼镜框左边缘
      int bridgeY = 105;  // 与眼睛 X 坐标对齐
      int bridgeL = 135 + 21;  // 左眼框右边缘 (y + radius)
      int bridgeR = 185 - 21;  // 右眼框左边缘 (y - radius)
      for (int t = -1; t <= 1; t++) {
        spi->drawLine(bridgeL, bridgeY + t, bridgeR, bridgeY + t, col);
      }
    }

    // ===== 兴奋表情：从右耳组件画爪印动画（五官下方，左右各一个）=====
    // 状态机：EXCITED_PAW_START_MS 之前不显示；之后左右交替 EXCITED_PAW_CYCLES
    // 轮（左,右,左,右,...），每一段的持续时长在 [EXCITED_PAW_MIN_MS,
    // EXCITED_PAW_MAX_MS] 内随机；交替结束后左爪再出现一次并常驻，然后右爪也
    // 出现并常驻——即"左,右,左,右,左(常驻),右(常驻)"。这样每只爪印在"常驻"之前
    // 都是从隐藏状态重新淡入的，不会出现"已经在显示的爪印突然被重新随机位置/
    // 角度导致画面跳一下"的问题（旧版本左右一起进入常驻时，其中一只爪印其实
    // 已经在显示中，重新随机位置会让它瞬间跳一下）。每只爪印开始显示时，都会
    // 给它的位置和整体旋转角度重新随机一次。
    if (isExcited && !isLeft) {
      const int totalSteps = 2 * EXCITED_PAW_CYCLES + 1;  // 交替段数，最后一段固定停在左爪
      if (excitedElapsed < EXCITED_PAW_START_MS) {
        pawPhaseIdx_ = -1;
      } else {
        if (pawPhaseIdx_ == -1) {
          pawPhaseIdx_ = 0;
          pawPhaseStartMs_ = excitedElapsed;
          pawPhaseDurationMs_ = random(EXCITED_PAW_MIN_MS, EXCITED_PAW_MAX_MS + 1);
          randomizePawAppearance(0);  // 先出现左爪
        }
        while (pawPhaseIdx_ >= 0 && pawPhaseIdx_ < totalSteps &&
               excitedElapsed - pawPhaseStartMs_ >= pawPhaseDurationMs_) {
          pawPhaseStartMs_ += pawPhaseDurationMs_;
          pawPhaseIdx_++;
          if (pawPhaseIdx_ < totalSteps) {
            pawPhaseDurationMs_ = random(EXCITED_PAW_MIN_MS, EXCITED_PAW_MAX_MS + 1);
            randomizePawAppearance(pawPhaseIdx_ % 2 == 0 ? 0 : 1);
          } else {
            // 交替结束（停在左爪，左爪已经在显示中，不重新随机它），现在轮到
            // 右爪也出现并常驻——右爪在上一段是隐藏的，这里是一次干净的淡入。
            randomizePawAppearance(1);
          }
        }
      }

      bool showLeft, showRight;
      if (pawPhaseIdx_ < 0) {
        showLeft = showRight = false;
      } else if (pawPhaseIdx_ >= totalSteps) {
        showLeft = showRight = true;
      } else {
        showLeft = (pawPhaseIdx_ % 2 == 0);
        showRight = !showLeft;
      }

      float leftScale = leftPawAnim_.update(showLeft ? 1.0f : 0.0f) * EXCITED_PAW_SCALE;
      float rightScale = rightPawAnim_.update(showRight ? 1.0f : 0.0f) * EXCITED_PAW_SCALE;

      int pawCy = DOUBT_PIVOT_Y + 60;   // 比之前往上抬一点，给爪印更多活动空间
      // 两只爪印都稳定常驻之后，一起微微左右摇晃（复用耳朵摆动同一条 sin 曲线）。
      int pawSwing = (pawPhaseIdx_ >= totalSteps) ? earSwingOffset() : 0;
      float leftRot = pawRotDeg_[0] * PI / 180.0f;
      float rightRot = pawRotDeg_[1] * PI / 180.0f;
      drawPawPrint(spi, DOUBT_PIVOT_X - 35 + pawJitterX_[0] + pawSwing, pawCy + pawJitterY_[0], leftScale, false, leftRot, col);
      drawPawPrint(spi, DOUBT_PIVOT_X + 35 + pawJitterX_[1] + pawSwing, pawCy + pawJitterY_[1], rightScale, true, rightRot, col);
    }

    // ===== 关键词播报按钮：椭圆碗口 + 圆角矩形碗身都只画白色描边（线框），
    // 碗口正中间画一个白色实心爪印，固定画在屏幕右下角（不随表情移动），
    // 只在右耳组件里画一次。buttonScaleAnim_ 每帧都朝目标缩放过渡（即使按钮
    // 当前隐藏也照常更新，保证下次出现时动画状态是对的），目标由
    // g_buttonState 决定：0/1=正常大小(1.0)，2=按下(BUTTON_DOWN_SCALE)。
    // M5Canvas 没有真正的布尔运算/裁剪 API，碗口盖住碗身顶边这个效果靠
    // "先画碗身描边→用背景色实心椭圆把落在碗口范围内的部分擦掉→再补画一次
    // 碗口描边"这三步模拟：擦除让碗身顶边在碗口内的一截连同碗口自己被擦掉的
    // 半圈描边一起消失，随后补画的碗口描边把自己的圆周重新画完整，最终效果
    // 就是碗口的边始终连续不断，碗身只在碗口下方露出的部分能看见边。=====
    if (!isLeft) {
      float buttonScale = buttonScaleAnim_.update(g_buttonState == 2 ? BUTTON_DOWN_SCALE : 1.0f);
      if (g_buttonState != 0) {
        int rx = max(1, (int)roundf(BUTTON_RX * buttonScale));
        int ry = max(1, (int)roundf(BUTTON_RY * buttonScale));
        int bodyHalfW = max(1, (int)roundf(BUTTON_BODY_W / 2.0f * buttonScale));
        int bodyTopY = BUTTON_CY + (int)roundf(BUTTON_BODY_TOP * buttonScale);
        int bodyBottomY = BUTTON_CY + (int)roundf(BUTTON_BODY_BOTTOM * buttonScale);
        int bodyR = max(1, (int)roundf(BUTTON_BODY_RADIUS * buttonScale));
        uint16_t bgCol = ctx->getColorDepth() == 1
                             ? ERACER_COLOR
                             : ctx->getColorPalette()->get(COLOR_BACKGROUND);
        // 碗身：圆角矩形描边
        spi->drawRoundRect(BUTTON_CX - bodyHalfW, bodyTopY, bodyHalfW * 2,
                            bodyBottomY - bodyTopY, bodyR, col);
        // 用背景色实心椭圆擦掉碗身与碗口重叠的部分（布尔差集），再补画一次
        // 碗口描边，让碗口的圆周保持完整、不被碗身的顶边打断。
        spi->fillEllipse(BUTTON_CX, BUTTON_CY, rx, ry, bgCol);
        spi->drawEllipse(BUTTON_CX, BUTTON_CY, rx, ry, col);
        // 爪印：白色实心，画在碗口正中间；BUTTON_PAW_SCALE/BUTTON_PAW_TOE_SPREAD_MUL
        // 两个常量是照实际半径算过的——保证爪印整体（脚掌+四趾）落在碗口椭圆
        // 内部，同时脚趾彼此、脚趾与脚掌之间留有不重叠的间隙。
        drawPawPrint(spi, BUTTON_CX, BUTTON_CY, BUTTON_PAW_SCALE * buttonScale, false, 0.0f, col,
                     BUTTON_PAW_TOE_SPREAD_MUL);
      }
    }

    // ===== 底部字幕：用户看不到设备在录音/思考/播报，靠这个文字提示——只在
    // 右耳组件里画一次。之前这段调用错放在 firmware.ino 里未被实例化的
    // CustomBrow 类中，从来没有真正渲染过，这里搬到实际生效的 PuppyEar 里。=====
    if (!isLeft && g_subNLines > 0) {
      drawSubtitle(spi, col);
    }
  }
};

}  // namespace m5avatar

#endif  // PUPPY_FACE_H_
