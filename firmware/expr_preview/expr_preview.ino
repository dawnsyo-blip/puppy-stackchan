/*
 * expr_preview.ino — 新表情快速预览（独立最小 sketch）
 * ======================================================
 * 只用来快速看新表情的视觉效果，不含 WiFi/HTTP/摄像头/麦克风——只有屏幕
 * 渲染，编译和烧录都比完整版 firmware.ino 快很多，方便反复调整 PuppyFace.h
 * 里的参数、重新烧录查看，不用碰、也不会影响正在跑的主固件。目前列表里有
 * "委屈"（grieved）、"peekaboo"（已经在游戏里正式接入）和"晕"（dizzy，
 * 设计中，还没接入 handleFace()/host）。
 *
 * PuppyFace.h 直接引用主固件那一份（#include "../PuppyFace.h"），不是复制
 * 一份改——确认视觉效果满意后，改动本来就已经在正确的地方，不需要"搬"过去，
 * 直接编译烧录主固件 firmware.ino 就行。
 *
 * 用法：烧录后打开串口（115200），每按一次回车切到下一个表情，常态 →
 * 委屈 → peekaboo → 晕 → 循环，不受打字内容影响，只看有没有收到换行符。
 * 屏幕上方会印当前是哪个方便对照；同时也会打印到串口。
 *
 * 视觉效果确认满意后，这个预览 sketch 完成使命，可以删掉；下一步是去
 * firmware.ino 的 handleFace() 里加上 grieved/peekaboo 的 /face?expr= 分支，
 * 再在 host 端 puppy_engine_v4.py 里决定什么场景触发这两个表情。
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

const char* EXPR_NAMES[] = {"neutral", "grieved", "peekaboo", "dizzy", "dead"};
const int NUM_EXPR = 5;
int exprIdx = 0;

void applyExpr(const char* name) {
  g_customExpr = "";
  if (strcmp(name, "grieved") == 0 || strcmp(name, "peekaboo") == 0 || strcmp(name, "dizzy") == 0 || strcmp(name, "dead") == 0) {
    avatar.setExpression(Expression::Neutral);
    baseExpr = Expression::Neutral;
    g_customExpr = name;
  } else {
    avatar.setExpression(Expression::Neutral);
    baseExpr = Expression::Neutral;
  }

  M5StackChan.Display().fillRect(0, 0, 320, 20, TFT_BLACK);
  M5StackChan.Display().setCursor(4, 2);
  M5StackChan.Display().setTextSize(1);
  M5StackChan.Display().setTextColor(TFT_WHITE, TFT_BLACK);
  M5StackChan.Display().printf("expr: %s", name);

  Serial.printf("[预览] 切到表情: %s\n", name);
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
