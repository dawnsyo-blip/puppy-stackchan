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
 *   Doubt    → 好奇（五官绕鼻子锚点顺时针转 15°，右耳转完后再单独逆时针转 15°）
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

namespace m5avatar {

// ╔══════════════════════════════════════════════╗
// ║              过渡动画 / 旋转工具              ║
// ╚══════════════════════════════════════════════╝

// 单个数值的平滑过渡：每次调用传入最新目标值，内部用 500ms 线性插值。
// 如果目标值在过渡途中又变了，会以"当前已经插值到的值"作为新起点重新起步，
// 保证来回切换表情时不会跳变。
class FloatTransition {
 public:
  static const unsigned long DURATION_MS = 500;

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
    float t = (float)(now - startMs_) / (float)DURATION_MS;
    if (t < 0.0f) t = 0.0f;
    if (t > 1.0f) t = 1.0f;
    current_ = from_ + (to_ - from_) * t;
    return current_;
  }

 private:
  bool inited_ = false;
  float from_ = 0, to_ = 0, current_ = 0;
  unsigned long startMs_ = 0;
};

// Doubt（好奇）：五官整体围绕"鼻子锚点"顺时针旋转的最大角度。
// 屏幕坐标系 y 轴向下，这里用的旋转公式在该坐标系下视觉效果为顺时针（正角度）/
// 逆时针（负角度）；如果实机看到的方向反了，把对应角度取负号即可翻转。
static const float DOUBT_ROTATE_RAD = 15.0f * PI / 180.0f;
// 近似鼻子锚点的位置，好奇表情的旋转、兴奋表情的爪印位置都以这一点为基准。
static const int DOUBT_PIVOT_X = 160;
static const int DOUBT_PIVOT_Y = 140;

// ---- 兴奋(excited)表情的爪印动画时间线 ----
// 静态姿势（'><' 眼 + 舌头）保持 EXCITED_PAW_START_MS 之后：
//   左爪出现 EXCITED_PAW_BLINK_MS，消失；右爪出现 EXCITED_PAW_BLINK_MS，消失；
//   如此左右交替 EXCITED_PAW_CYCLES 轮，之后左右爪一起出现并常驻。
static const unsigned long EXCITED_PAW_START_MS = 2000;
static const unsigned long EXCITED_PAW_BLINK_MS = 1000;
static const int EXCITED_PAW_CYCLES = 2;

// 兴奋表情整体缩小比例：眼睛/鼻子/嘴巴/舌头/耳朵都按这个比例缩小。
// 爪印在此基础上再额外缩小 20%（EXCITED_PAW_SCALE），同时爪印内部脚趾间距、
// 两只爪印之间的间距、爪印与五官的距离都相应加大，避免整体缩小后挤在一起。
static const float EXCITED_SCALE = 0.7f;
static const float EXCITED_PAW_SCALE = EXCITED_SCALE * 0.8f;

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

// 兴奋表情的爪印动画阶段：0=都不显示 1=只显示左爪 2=只显示右爪 3=左右都显示（最终常驻状态）。
static int excitedPawPhase(unsigned long elapsed) {
  if (elapsed < EXCITED_PAW_START_MS) return 0;
  unsigned long t = elapsed - EXCITED_PAW_START_MS;
  const unsigned long cycleLen = 2 * EXCITED_PAW_BLINK_MS;
  const unsigned long blinkPhaseLen = (unsigned long)EXCITED_PAW_CYCLES * cycleLen;
  if (t < blinkPhaseLen) {
    unsigned long posInCycle = t % cycleLen;
    return (posInCycle < EXCITED_PAW_BLINK_MS) ? 1 : 2;
  }
  return 3;
}

// ╔══════════════════════════════════════════════╗
// ║              PuppyEye 小狗眼睛               ║
// ╚══════════════════════════════════════════════╝
//
// 替代 Eye 组件。根据表情画不同眼睛：
//   常态/生气/好奇: 竖向椭圆（好奇时会转）
//   开心: ^^ 弧线
//   兴奋: '><' 眉眼形（比其它眼睛款式更大，静态不旋转）
//   困倦: 向下弯的曲线
//   抱歉: 竖椭圆眼眶 + 瞳孔朝上
//   思考: 竖椭圆瞳孔+眼镜框（右眼带眨眼）
//   隐私: 闭合弧线
//
// 切换到不同"眼睛款式"时，会用 0→1 的缩放在 500ms 内把新款式画出来
// （旧款式所在的表情消失的同时，新款式从中心"长"出来）。

class PuppyEye : public Drawable {
  bool isLeft;  // true=屏幕左侧的眼睛, false=屏幕右侧

  // 眼睛款式编号：0隐私 1兴奋 2思考 3开心 4困倦 5抱歉 6常态/生气/好奇
  int lastStyle_ = -1;
  unsigned long styleStartMs_ = 0;
  FloatTransition doubtAngleAnim_;

 public:
  PuppyEye(bool isLeft) : isLeft(isLeft) {}

  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    bool custom = (exp == Expression::Neutral) && g_customExpr.length() > 0;
    bool isExcited = custom && g_customExpr == "excited";

    float doubtAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD : 0.0f);
    applyRotationAroundPivot(doubtAngle, DOUBT_PIVOT_X, DOUBT_PIVOT_Y, cx, cy);
    float rotTotal = doubtAngle;

    uint16_t col = ctx->getColorDepth() == 1
                       ? 1
                       : ctx->getColorPalette()->get(COLOR_PRIMARY);

    // 视线偏移（Avatar 库的 Gaze 系统）
    Gaze g = isLeft ? ctx->getLeftGaze() : ctx->getRightGaze();
    int ox = g.getHorizontal() * 3;
    int oy = g.getVertical() * 3;

    int style;
    if (custom && g_customExpr == "privacy") style = 0;
    else if (isExcited) style = 1;
    else if (custom && g_customExpr == "thinking") style = 2;
    else if (exp == Expression::Happy) style = 3;
    else if (exp == Expression::Sleepy) style = 4;
    else if (exp == Expression::Sad) style = 5;
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

    // ---- 兴奋：'><' 眉眼形——左眼是 '>'，右眼是 '<'，静态不转，整体按 EXCITED_SCALE 缩小 ----
    if (style == 1) {
      int a = (int)roundf(7 * EXCITED_SCALE * s);
      int b = (int)roundf(4 * EXCITED_SCALE * s);
      int armX = isLeft ? -a : a;   // 张开的两条边朝向的一侧
      int tipX = isLeft ? b : -b;   // 尖角（顶点）朝向的一侧
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
// 平滑过渡；好奇时鼻子和嘴巴自身也跟着顺时针转。

class PuppyNose : public Drawable {
  FloatTransition rxAnim_, ryAnim_, offXAnim_, offYAnim_;
  FloatTransition cwAnim_, cdAnim_;
  FloatTransition doubtAngleAnim_;

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

    // ---- 目标参数 ----
    float targetRx = isPrivacy ? 9.0f : (isExcited ? 10.0f * EXCITED_SCALE : 10.0f);
    float targetRy = isPrivacy ? 6.0f : (isExcited ? 7.0f * EXCITED_SCALE : 7.0f);
    float targetOffX = isPrivacy ? -8.0f : 0.0f;
    float targetOffY = isPrivacy ? -5.0f : 0.0f;

    float targetCw = 16.0f, targetCd = 8.0f;  // 标准嘴巴弧线
    if (isPrivacy) {
      // 隐私时不画嘴巴：把目标收成 0，过渡时会看到嘴巴慢慢收起来
      targetCw = 0.0f;
      targetCd = 0.0f;
    } else if (exp == Expression::Happy) {
      targetCw = 20.0f;
      targetCd = 12.0f;
    } else if (isExcited) {
      targetCw = 20.0f * EXCITED_SCALE;
      targetCd = 12.0f * EXCITED_SCALE;
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
    float rotAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD : 0.0f);

    // 好奇表情下鼻子/嘴巴自身也跟着转。枢轴就是鼻子自己的锚点，
    // 所以鼻子中心的位置不动，但形状（椭圆朝向、嘴巴弧线）会跟着转。
    applyRotationAroundPivot(rotAngle, DOUBT_PIVOT_X, DOUBT_PIVOT_Y, cx, cy);

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

    int mouthOffY = isExcited ? (int)roundf(10 * EXCITED_SCALE) : 10;  // 嘴巴起点相对鼻子中心的 Y 偏移
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

    // ---- 兴奋：加一个 U 型舌头，一直跟嘴巴一起显示。宽度正好是嘴巴宽度的
    //      一半（tongueHalfW = curveWidth/2，和嘴巴弧线控制点的 x 完全一样）；
    //      两端 y 坐标用二次贝塞尔的精确公式算出嘴巴弧线在这个 x 上的真实
    //      位置（不是估算值），保证舌头和嘴巴严丝合缝地连在一起、围成一个
    //      封闭的空间。----
    if (isExcited) {
      int tongueHalfW = curveWidth / 2;
      // 嘴巴弧线是二次贝塞尔 (0,mouthOffY) -> (curveWidth/2, mouthOffY+curveDepth)
      // -> (curveWidth, mouthOffY+2)，在 x=curveWidth/2 处（t=0.5）的精确 y：
      float tongueAttachY = mouthOffY + curveDepth * 0.5f + 0.5f;
      const int tongueDepth = (int)roundf(8 * EXCITED_SCALE);  // U 形往下鼓出的深度
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
// 兴奋：耳朵是静态的（在"开心"短耳基础上再按 EXCITED_SCALE 缩小），不转不额外
// 缩放。爪印动画的时间线——
//   1. 保持静态姿势 2s；
//   2. 之后左爪出现 1s、消失，右爪出现 1s、消失，如此交替 2 轮；
//   3. 最后左右爪一起出现并常驻，直到离开兴奋表情。
// 爪印在 EXCITED_SCALE 的基础上再额外缩小 20%（EXCITED_PAW_SCALE），但爪印内部
// 脚趾间距、两只爪印之间的间距、爪印与五官的距离都相应加大了，避免整体缩小后
// 挤在一起看不清。

class PuppyEar : public Drawable {
  bool isLeft;  // true=屏幕左侧耳朵, false=屏幕右侧

  FloatTransition lenAnim_, wAnim_, topYAnim_;
  FloatTransition doubtAngleAnim_;
  FloatTransition rightTwistAnim_;      // 好奇：主体旋转结束后，右耳额外逆时针转 15°
  bool wasDoubt_ = false;
  unsigned long doubtStartMs_ = 0;

  bool wasExcited_ = false;
  unsigned long excitedStartMs_ = 0;
  FloatTransition leftPawAnim_, rightPawAnim_;  // 兴奋：左右爪印各自的出现/消失缓动

 public:
  PuppyEar(bool isLeft) : isLeft(isLeft) {}

  void drawThickBezier(M5Canvas *spi,
                       int x0, int y0, int x1, int y1, int x2, int y2,
                       int thickness, uint16_t col) {
    for (int t = -(thickness / 2); t <= thickness / 2; t++) {
      spi->drawBezier(x0 + t, y0, x1 + t, y1, x2 + t, y2, col);
    }
  }

  // 画一个写实一点的爪印：一个大脚掌（椭圆）+ 4 个小脚趾（用旋转椭圆近似，
  // 从内到外逐渐往外翘）。mirror=true 时脚趾左右镜像（用于右爪）。scale
  // 控制从 0 长到 1 / 从 1 缩到 0 的出现与消失动画。
  void drawPawPrint(M5Canvas *spi, int cx, int cy, float scale, bool mirror, uint16_t col) {
    if (scale <= 0.02f) return;
    int m = mirror ? -1 : 1;
    const float toeSpread = 1.35f;  // 脚趾彼此之间、脚趾与脚掌之间的间距放大系数（只放大位置，不放大半径）

    // 大脚掌（往下方多挪一点，跟脚趾拉开距离）
    int padRx = (int)roundf(8 * scale);
    int padRy = (int)roundf(9 * scale);
    spi->fillEllipse(cx, cy + (int)roundf(8 * scale), padRx, padRy, col);

    // 4 个脚趾：{ 局部x偏移, 局部y偏移, 半径x, 半径y, 倾斜角度(度) }
    const float toes[4][5] = {
        {-11.0f, -6.0f,  4.0f, 6.0f, -25.0f},
        { -4.0f, -12.0f, 4.2f, 6.5f, -8.0f},
        {  4.0f, -12.0f, 4.2f, 6.5f,  8.0f},
        { 11.0f, -6.0f,  4.0f, 6.0f,  25.0f},
    };
    for (int i = 0; i < 4; i++) {
      int tx = cx + (int)roundf(m * toes[i][0] * toeSpread * scale);
      int ty = cy + (int)roundf(toes[i][1] * toeSpread * scale);
      int trx = (int)roundf(toes[i][2] * scale);
      int try_ = (int)roundf(toes[i][3] * scale);
      float ang = m * toes[i][4] * PI / 180.0f;
      fillRotatedEllipse(spi, tx, ty, trx, try_, ang, col);
    }
  }

  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    bool custom = (exp == Expression::Neutral) && g_customExpr.length() > 0;
    bool isExcited = custom && g_customExpr == "excited";
    unsigned long excitedElapsed = elapsedSinceTrue(isExcited, wasExcited_, excitedStartMs_);

    float doubtAngle = doubtAngleAnim_.update(exp == Expression::Doubt ? DOUBT_ROTATE_RAD : 0.0f);
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

    if (exp == Expression::Happy) {
      earLenTarget = 55;   // 开心时耳朵短一些（更精神）
      topYTarget = -30;
    } else if (isExcited) {
      // 兴奋：耳朵在"开心"短耳基础上整体再按 EXCITED_SCALE 缩小
      earLenTarget = (int)roundf(55 * EXCITED_SCALE);
      earWTarget = (int)roundf(earWTarget * EXCITED_SCALE);
      topYTarget = (int)roundf(-30 * EXCITED_SCALE);
    }
    if (exp == Expression::Sleepy) {
      earLenTarget = 80;   // 困倦时更长更垂，两侧耳朵保持相同大小
    }
    if (exp == Expression::Doubt) {
      // 好奇：两只耳朵先整体缩小到常态的 70%（缩放后的目标值变化会被下面的
      // FloatTransition 自动缓动出来），左耳再额外垂一点、右耳再额外长一点，
      // 同时整体也绕鼻子锚点顺时针旋转（见上方 applyRotationAroundPivot）。
      const float DOUBT_EAR_SCALE = 0.7f;
      earLenTarget = (int)roundf(earLenTarget * DOUBT_EAR_SCALE);
      earWTarget = (int)roundf(earWTarget * DOUBT_EAR_SCALE);

      const int LEFT_EAR_DROOP = 12;
      const int RIGHT_EAR_EXTRA = 15;  // 右耳 earLen 比左耳多 15px
      if (isLeft) {
        earLenTarget += LEFT_EAR_DROOP;
        topYTarget += 5;
      } else {
        earLenTarget += LEFT_EAR_DROOP + RIGHT_EAR_EXTRA;
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

    // ---- 摆动动画：开心 / 好奇 / 思考时耳朵左右轻轻摆动（水平平移）----
    bool swinging = (exp == Expression::Happy) || (exp == Expression::Doubt) ||
                    (custom && g_customExpr == "thinking");
    int swing = swinging ? earSwingOffset() : 0;

    // ---- 好奇：主体旋转（500ms）结束以后，右耳再额外绕自己的"耳根顶点"
    //      （耳朵最上方的端点，即 x3,y3）单独逆时针转 15°，用自己的
    //      FloatTransition 缓入，和主体旋转错开、不叠加着一起转。----
    unsigned long doubtElapsed = elapsedSinceTrue(exp == Expression::Doubt, wasDoubt_, doubtStartMs_);
    bool doubtMainRotationDone = doubtElapsed >= FloatTransition::DURATION_MS;
    const float RIGHT_EAR_TWIST_RAD = -15.0f * PI / 180.0f;  // 逆时针 15°
    float twistTarget = (exp == Expression::Doubt && !isLeft && doubtMainRotationDone)
                             ? RIGHT_EAR_TWIST_RAD
                             : 0.0f;
    float rightEarTwist = rightTwistAnim_.update(twistTarget);

    float pivotLX = dir * earW;  // 耳根顶点（远离头部一侧的顶部端点）局部坐标
    float pivotLY = topY;

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
    hingeThenRotate(dir * 5, topY + 15, pivotLX, pivotLY, rightEarTwist, rotTotal, x0, y0);
    hingeThenRotate(dir * (earW / 2.0f), earLen, pivotLX, pivotLY, rightEarTwist, rotTotal, xBot, yBot);
    hingeThenRotate(pivotLX, pivotLY, pivotLX, pivotLY, rightEarTwist, rotTotal, x3, y3);
    hingeThenRotate(-dir * 8, earLen - 15, pivotLX, pivotLY, rightEarTwist, rotTotal, ctrl1X, ctrl1Y);
    hingeThenRotate(dir * (earW + 12), earLen - 15, pivotLX, pivotLY, rightEarTwist, rotTotal, ctrl2X, ctrl2Y);

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
    if (isExcited && !isLeft) {
      int phase = excitedPawPhase(excitedElapsed);
      float leftTarget = (phase == 1 || phase == 3) ? 1.0f : 0.0f;
      float rightTarget = (phase == 2 || phase == 3) ? 1.0f : 0.0f;
      float leftScale = leftPawAnim_.update(leftTarget) * EXCITED_PAW_SCALE;
      float rightScale = rightPawAnim_.update(rightTarget) * EXCITED_PAW_SCALE;

      int pawCy = DOUBT_PIVOT_Y + 75;   // 离五官更远
      drawPawPrint(spi, DOUBT_PIVOT_X - 35, pawCy, leftScale, false, col);   // 两爪间距更大
      drawPawPrint(spi, DOUBT_PIVOT_X + 35, pawCy, rightScale, true, col);
    }
  }
};

}  // namespace m5avatar

#endif  // PUPPY_FACE_H_
