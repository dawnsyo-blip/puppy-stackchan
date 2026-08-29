/*
 * expr_preview.ino — 表情预览（独立最小 sketch）
 * ======================================================
 * 不含 WiFi/HTTP/摄像头/麦克风——只有屏幕渲染，编译和烧录都比完整版
 * firmware.ino 快很多。有两个用途：①设计新表情时反复调整 PuppyFace.h
 * 里的参数、重新烧录查看效果；②列出当前所有已定型的表情，方便逐个拍照
 * 留存（比如放进 GitHub README 当参考图），不用碰、也不会影响正在跑的
 * 主固件。
 *
 * 当前列表是 handleFace()（firmware.ino）里已经接好的全部 15 个表情，
 * 跟 PuppyFace.h 头部注释的"表情"清单保持一致：内置枚举 5 个（neutral/
 * happy/sleepy/doubt/sad）+ 自定义字符串 10 个（thinking/excited/
 * privacy/grieved/peekaboo/dizzy/dead/angry/eat/play）。以后
 * PuppyFace.h 里新增表情，记得回来把这个列表也补上，不要只更新
 * handleFace() 那边——这里是"看全部表情长什么样"的唯一入口，跟
 * handleFace() 脱节的话会漏掉新表情的参考图。
 *
 * PuppyFace.h 直接引用主固件那一份（#include "../PuppyFace.h"），不是复制
 * 一份改——确认视觉效果满意后，改动本来就已经在正确的地方，不需要"搬"过去，
 * 直接编译烧录主固件 firmware.ino 就行。
 *
 * 用法：烧录后打开串口（115200），每按一次回车切到下一个表情，按上面
 * 列表的顺序循环，不受打字内容影响，只看有没有收到换行符。屏幕上方会印
 * 当前是第几个/哪个表情方便对照拍照；同时也会打印到串口。
 */

#include <M5StackChan.h>
#include <Avatar.h>
#include <Face.h>

// PuppyFace.h 里 PuppyEar::draw() 会调用这个函数（只在 g_subNLines>0 时才
// 真的进那个分支，预览里 g_subNLines 恒为 0，运行时不会真的执行到）；函数
// 本身是在 firmware.ino 里定义的，这个独立 sketch 没有那份代码，需要在
// #include "../PuppyFace.h" 之前先声明一下，跟 firmware.ino 自己的写法
// 一致（那边也在文件靠前的位置手动前置声明了一次，不完全依赖 Arduino 的
// 自动生成函数原型）。
void drawSubtitle(M5Canvas *spi, uint16_t fg);

#include "../PuppyFace.h"

using namespace m5avatar;

// PuppyFace.h 里用 extern 声明、指望在别处定义的几个全局——预览用不到
// 关键词按钮/字幕/舵机镜像，给个固定默认值就行。
String g_customExpr = "";
volatile int g_buttonState = 0;
volatile int g_subNLines = 0;
volatile int g_currentYaw = 0;

// 空实现——预览不需要真的画字幕，见上面的前置声明。
void drawSubtitle(M5Canvas *spi, uint16_t fg) {}

Avatar avatar;
Expression baseExpr = Expression::Neutral;

const char* EXPR_NAMES[] = {
  "neutral", "happy", "sleepy", "doubt", "sad",
  "thinking", "excited", "privacy", "grieved", "peekaboo",
  "dizzy", "dead", "angry", "eat", "play"
};
const int NUM_EXPR = 15;
int exprIdx = 0;

// 跟 handleFace()（firmware.ino）同一套映射：内置枚举 5 个直接
// setExpression()，其余全部是 g_customExpr 字符串扩展表情。
void applyExpr(const char* name) {
  g_customExpr = "";
  if (strcmp(name, "neutral") == 0) {
    avatar.setExpression(Expression::Neutral);
    baseExpr = Expression::Neutral;
  } else if (strcmp(name, "happy") == 0) {
    avatar.setExpression(Expression::Happy);
    baseExpr = Expression::Happy;
  } else if (strcmp(name, "sleepy") == 0) {
    avatar.setExpression(Expression::Sleepy);
    baseExpr = Expression::Sleepy;
  } else if (strcmp(name, "doubt") == 0) {
    avatar.setExpression(Expression::Doubt);
    baseExpr = Expression::Doubt;
  } else if (strcmp(name, "sad") == 0) {
    avatar.setExpression(Expression::Sad);
    baseExpr = Expression::Sad;
  } else {
    avatar.setExpression(Expression::Neutral);
    baseExpr = Expression::Neutral;
    g_customExpr = name;
  }

  M5StackChan.Display().fillRect(0, 0, 320, 20, TFT_BLACK);
  M5StackChan.Display().setCursor(4, 2);
  M5StackChan.Display().setTextSize(1);
  M5StackChan.Display().setTextColor(TFT_WHITE, TFT_BLACK);
  M5StackChan.Display().printf("expr %d/%d: %s", exprIdx + 1, NUM_EXPR, name);

  Serial.printf("[预览] 切到表情 %d/%d: %s\n", exprIdx + 1, NUM_EXPR, name);
}

void setup() {
  Serial.begin(115200);
  M5StackChan.begin();
  M5StackChan.showRgbColor(0, 0, 0);

  auto *nose     = new PuppyNose();
  auto *eyeR     = new PuppyEye(false);
  auto *eyeL     = new PuppyEye(true);
  auto *earR     = new PuppyEar(false);
  auto *earL     = new PuppyEar(true);
  auto *nosePos  = new BoundingRect(140, 160);
  auto *eyeRPos  = new BoundingRect(105, 185);
  auto *eyeLPos  = new BoundingRect(105, 135);
  auto *earRPos  = new BoundingRect(90, 225);
  auto *earLPos  = new BoundingRect(90, 95);
  auto *face = new Face(nose, nosePos, eyeR, eyeRPos, eyeL, eyeLPos,
                        earR, earRPos, earL, earLPos);
  avatar.init();
  avatar.setFace(face);

  M5StackChan.Motion.goHome(300);

  applyExpr(EXPR_NAMES[exprIdx]);
  Serial.println("[预览] 就绪——在串口窗口按回车切到下一个表情");
}

void loop() {
  M5StackChan.update();

  // 一行里不管打了什么，只要收到回车就切一次表情——不关心具体内容，单纯把
  // 回车当"下一个"的信号。不同终端按下回车实际发送的字节不一样（有的发
  // '\r'，有的发'\n'，有的两个都发），'\r'和'\n'都当触发；一次性吃掉这轮
  // 攒着的所有字节再统一判断一次要不要切，"\r\n"两个字节一起到达时只会
  // 触发一次切换，不会被数成两次。
  bool advance = false;
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') advance = true;
  }
  if (advance) {
    exprIdx = (exprIdx + 1) % NUM_EXPR;
    applyExpr(EXPR_NAMES[exprIdx]);
  }

  delay(20);
}
