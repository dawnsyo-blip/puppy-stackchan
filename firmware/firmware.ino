/*
 * StackChan HTTP API Firmware
 * Turns StackChan into a WiFi-controlled robot with HTTP endpoints
 * for face expressions, servo, camera, audio, and touch.
 *
 * Hardware: M5Stack StackChan (CoreS3 ESP32-S3)
 * All AI/ML runs on a host computer — this firmware is the "dumb terminal".
 *
 * API endpoints:
 *   GET /           — control panel
 *   GET /face       — set expression (neutral/happy/sad/angry/sleepy/doubt/love/eyeroll)
 *   GET /servo      — move servos (yaw, pitch, speed)
 *   GET /camera     — capture JPEG photo
 *   GET /status     — device status JSON
 *   GET /home       — center servos
 *   GET /touch      — touch sensor readings
 *   GET /record     — record audio (WAV)
 *   GET /stream     — start/stop continuous mic PCM streaming to host via TCP
 *                     (?port=N to start, ?stop=1 to stop; non-blocking — runs
 *                     as a background task, see streamTaskFn() below)
 *   GET /play       — play WAV from URL (streaming, up to 2MB/62s)
 *   GET /speech     — display subtitle text
 *   GET /volume     — mic volume level
 *   GET /led        — set LED color (r,g,b) or turn off (off=1). Also takes
 *                     mode=solid/blink/breathe/rainbow/fade (+period_ms /
 *                     fade_ms) for continuous effects driven locally from
 *                     loop() via updateLed() — caller sets the mode once per
 *                     state change, no need to keep polling /led.
 *   GET /button     — show/hide the on-screen paw button (state=up/down/off)
 *   GET /display    — turn LCD backlight+panel off (off=1) or back on (on=1)
 */
#include <M5StackChan.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <esp_camera.h>
#include <mutex>
#include <atomic>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// Forward-declared here so PuppyFace.h (included below) can call it from
// PuppyEar::draw() — the real definition (with the subtitle line-wrapping
// logic) lives further down in this file, after the g_sub* globals it reads.
// M5Canvas itself (a `using` alias onto m5gfx::M5Canvas) is already visible
// via the M5StackChan.h include above.
static void drawSubtitle(M5Canvas *spi, uint16_t fg);

#include <Avatar.h>
#include <Face.h>
#include "PuppyFace.h"
#include <HTTPClient.h>

#include "config.h"

using namespace m5avatar;

// ============ Custom Face ============
// 7 expressions: happy ^^, sad, love (heart eyes + blush), angry,
// sleepy (Zzz), doubt (?), eyeroll
// Base: rectangular eyes + horizontal line mouth + no eyebrows

// Custom expressions beyond the library's built-in set.
// Rendered only when avatar is Neutral, so reverting to Neutral
// automatically restores the custom face.
String g_customExpr = "";

// 当前舵机的实际 yaw（不是最后一次下发的目标值，是 Motion 库正在插值经过的
// 真实角度），每帧在 loop() 里从 M5StackChan.Motion.getCurrentAngles() 刷新。
// PuppyFace.h 用它决定"好奇"表情整体歪头的镜像方向（见 doubtMirrorSign()）
// ——捉迷藏游戏扫描房间时舵机会左右来回摆动，歪头方向应该跟着摆向的一侧镜像，
// 而不是不管转到哪边都固定歪同一侧。必须是 volatile：写在 loop()
// 所在的 Arduino 主任务里，读在 avatar.init() 起的独立渲染任务里（跟
// g_buttonState/g_subNLines 是同一种跨任务共享场景，同样的理由）——不加
// volatile 的话，编译器可以合法地把渲染任务里的读取缓存在寄存器里，
// 导致 doubtMirrorSign() 读到的值一直不刷新，表现出来就是歪头方向卡死、
// 好奇表情看起来"没有变化"。
volatile int g_currentYaw = 0;

// Subtitle system (replaces the library's Balloon which is too small for CJK)
// Layout is done once in handleSpeech; render thread only reads static arrays.
const int SUB_MAX_LINES = 16;
char g_subLines[SUB_MAX_LINES][64];
int g_subLineChars[SUB_MAX_LINES];
volatile int g_subNLines = 0;
int g_subTotalChars = 0;
unsigned long g_speechStart = 0;
unsigned long g_speechDurMs = 0;

// 字幕框几何：整体只有 50px 高（原来是 66px），但仍然比 16px 高的 CJK 字体
// （efontCN_16）宽裕得多，两行文字加翻页圆点都放得下——SUB_BOX_Y 保持跟原来
// 一样的 168，只往上收了底边，所以框的顶边位置不变。
static const int SUB_BOX_X = 6;
static const int SUB_BOX_Y = 168;
static const int SUB_BOX_W = 308;
static const int SUB_BOX_H = 50;
static const int SUB_BOX_R = 10;
static const int SUB_LINE1_Y = 176;   // 两行文字时，第一行的 y
static const int SUB_LINE2_Y = 194;   // 两行文字时，第二行的 y
static const int SUB_SINGLE_Y = 186;  // 只有一行文字时，垂直居中的 y
static const int SUB_DOTS_Y = 213;    // 翻页圆点的 y（超过两行文字才会出现）

// Keyword-broadcast paw button (drawn by PuppyEar): 0=hidden, 1=up (idle),
// 2=down (pressed-in, flashed briefly before each keyword plays).
volatile int g_buttonState = 0;

// 头顶双击 / 屏幕点击计数器：host 端只以约 1Hz 轮询 /touch，而
// TouchSensor.wasDoubleClicked() 这类手势判定只在库内部状态机"判定成立"的
// 那一次 update() 里为 true（M5StackChan.update() 每帧都在跑，但对应的那
// 一帧转瞬即逝）——host 轮询频率跟不上，直接问"现在是不是刚双击"大概率会
// 错过。改成单调递增计数器：loop() 里每次观测到手势成立就 +1，host 端只需
// 要跟自己上次记的值比较有没有变化，不会因为轮询节奏对不上而漏掉事件（就
// 算错过好几次 poll，计数器的差值也如实反映到底发生了几次）。二者都只在
// loop() 所在的主线程里读写，没有并发，不需要原子类型。
uint32_t g_headDoubleTapCount = 0;
uint32_t g_screenTapCount = 0;

// UTF-8 line breaking: CJK (3 bytes) = 16px, ASCII = 8px, max line width 292px
static void buildSubtitle(const String &text) {
  g_subNLines = 0;
  memset(g_subLineChars, 0, sizeof(g_subLineChars));
  int li = 0, lineW = 0, lineLen = 0, total = 0;
  bool full = false;
  for (unsigned int i = 0; i + 1 <= text.length() && !full;) {
    uint8_t c = text[i];
    int clen = (c < 0x80) ? 1 : (c < 0xE0) ? 2 : (c < 0xF0) ? 3 : 4;
    if (i + clen > text.length()) break;
    int cw = (clen == 1) ? 8 : 16;
    if (lineW + cw > 292 || lineLen + clen > 60) {
      g_subLines[li][lineLen] = '\0';
      if (++li >= SUB_MAX_LINES) { full = true; break; }
      lineW = 0; lineLen = 0;
    }
    for (int k = 0; k < clen; k++) g_subLines[li][lineLen++] = text[i + k];
    g_subLineChars[li]++;
    total++;
    lineW += cw;
    i += clen;
  }
  if (!full) {
    g_subLines[li][lineLen] = '\0';
    li++;
  }
  g_subTotalChars = total;
  g_subNLines = (total > 0) ? li : 0;
}

// Bottom subtitle bar: 2 lines per page, auto-paging synced to speech duration
static void drawSubtitle(M5Canvas *spi, uint16_t fg) {
  int nLines = g_subNLines;
  if (nLines <= 0) return;
  int nPages = (nLines + 1) / 2;

  int page = nPages - 1;
  unsigned long durMs = g_speechDurMs > 0 ? g_speechDurMs : (unsigned long)g_subTotalChars * 250;
  unsigned long elapsed = millis() - g_speechStart;
  unsigned long acc = 0;
  for (int p = 0; p < nPages; p++) {
    int pc = g_subLineChars[p * 2] + (p * 2 + 1 < nLines ? g_subLineChars[p * 2 + 1] : 0);
    acc += (unsigned long)pc * durMs / (g_subTotalChars ? g_subTotalChars : 1);
    if (elapsed < acc) { page = p; break; }
  }

  spi->fillRoundRect(SUB_BOX_X, SUB_BOX_Y, SUB_BOX_W, SUB_BOX_H, SUB_BOX_R, 0);
  spi->drawRoundRect(SUB_BOX_X, SUB_BOX_Y, SUB_BOX_W, SUB_BOX_H, SUB_BOX_R, fg);
  spi->drawRoundRect(SUB_BOX_X + 1, SUB_BOX_Y + 1, SUB_BOX_W - 2, SUB_BOX_H - 2, SUB_BOX_R - 1, fg);
  spi->setFont(&fonts::efontCN_16);
  spi->setTextSize(1);
  spi->setTextColor(fg);
  spi->setTextDatum(TL_DATUM);
  bool hasL2 = (page * 2 + 1 < nLines);
  if (!hasL2) {
    spi->drawString(g_subLines[page * 2], 14, SUB_SINGLE_Y);
  } else {
    spi->drawString(g_subLines[page * 2], 14, SUB_LINE1_Y);
    spi->drawString(g_subLines[page * 2 + 1], 14, SUB_LINE2_Y);
  }
  if (nPages > 1) {
    for (int p = 0; p < nPages && p < 8; p++) {
      int dx = 302 - (nPages - 1 - p) * 10;
      if (p == page) spi->fillCircle(dx, SUB_DOTS_Y, 2, fg);
      else spi->drawCircle(dx, SUB_DOTS_Y, 2, fg);
    }
  }
  spi->setFont(&fonts::Font0);
}

// Manga-style anger mark
static void drawAngerMark(M5Canvas *spi, int ax, int ay, uint16_t col) {
  for (int dx = -1; dx <= 1; dx += 2) {
    for (int dy = -1; dy <= 1; dy += 2) {
      int ex = ax + dx * 9, ey = ay + dy * 9;
      spi->drawLine(ex, ey, ex - dx * 7, ey, col);
      spi->drawLine(ex, ey + dy, ex - dx * 7, ey + dy, col);
      spi->drawLine(ex, ey, ex, ey - dy * 7, col);
      spi->drawLine(ex + dx, ey, ex + dx, ey - dy * 7, col);
    }
  }
}

class CustomEye final : public Drawable {
  bool isLeft;
 public:
  CustomEye(bool isLeft) : isLeft(isLeft) {}
  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    uint32_t cx = rect.getCenterX();
    uint32_t cy = rect.getCenterY();
    Expression exp = ctx->getExpression();
    Gaze g = isLeft ? ctx->getLeftGaze() : ctx->getRightGaze();
    float openRatio = isLeft ? ctx->getLeftEyeOpenRatio() : ctx->getRightEyeOpenRatio();
    int ox = g.getHorizontal() * 3;
    int oy = g.getVertical() * 3;
    uint16_t col = ctx->getColorDepth() == 1 ? 1 : ctx->getColorPalette()->get(COLOR_PRIMARY);

    bool customActive = (exp == Expression::Neutral) && g_customExpr.length() > 0;

    // Love: heart eyes + blush lines
    if (customActive && g_customExpr == "love") {
      spi->fillCircle(cx - 8, cy - 5, 10, col);
      spi->fillCircle(cx + 8, cy - 5, 10, col);
      spi->fillTriangle(cx - 17, cy - 2, cx + 17, cy - 2, cx, cy + 17, col);
      for (int i = 0; i < 3; i++) {
        int bx = cx - 10 + i * 7;
        spi->drawLine(bx, cy + 27, bx + 5, cy + 21, col);
        spi->drawLine(bx + 1, cy + 27, bx + 6, cy + 21, col);
      }
      return;
    }

    // Eyeroll: eye socket + pupil at top
    if (customActive && g_customExpr == "eyeroll") {
      int w = 36, h = 18;
      int x = cx - w / 2, y = cy - h / 2;
      spi->drawRect(x, y, w, h, col);
      spi->drawRect(x + 1, y + 1, w - 2, h - 2, col);
      spi->fillRect(cx - 5, y + 2, 10, 6, col);
      return;
    }

    // Happy: ^^ eyes
    if (exp == Expression::Happy && openRatio > 0.4f) {
      for (int i = 0; i < 3; i++) {
        spi->drawLine(cx + ox - 11, cy + oy + 5 + i, cx + ox, cy + oy - 6 + i, col);
        spi->drawLine(cx + ox, cy + oy - 6 + i, cx + ox + 11, cy + oy + 5 + i, col);
      }
      return;
    }

    int w = 28;
    int hBase = (exp == Expression::Angry) ? 7 : 10;
    int h = max(2, (int)(hBase * openRatio));
    if (exp == Expression::Sleepy) h = 3;
    if (exp == Expression::Doubt) oy += isLeft ? -3 : 2;
    int x = cx + ox - w / 2;
    int y = cy + oy - h / 2;
    spi->fillRect(x, y, w, h, col);
  }
};

class CustomMouth final : public Drawable {
 public:
  CustomMouth() {}
  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    uint16_t col = ctx->getColorDepth() == 1 ? 1 : ctx->getColorPalette()->get(COLOR_PRIMARY);
    Expression exp = ctx->getExpression();
    float breath = min(1.0f, ctx->getBreath());
    float openRatio = ctx->getMouthOpenRatio();
    int cx = rect.getLeft();
    int cy = rect.getTop() + (int)(breath * 2);

    // Lip sync: open mouth rectangle during speech
    if (openRatio > 0.15f) {
      int h = max(3, (int)(8 * openRatio));
      spi->fillRect(cx - 12, cy - h / 2, 24, h, col);
      return;
    }

    bool customActive = (exp == Expression::Neutral) && g_customExpr.length() > 0;

    if (customActive && g_customExpr == "love") {
      spi->fillArc(cx, cy - 19, 23, 25, 60, 120, col);
      return;
    }
    if (exp == Expression::Happy) {
      spi->fillArc(cx, cy - 28, 34, 36, 53, 127, col);
      return;
    }
    if (exp == Expression::Sad) {
      spi->fillArc(cx, cy + 28, 34, 36, 233, 307, col);
      return;
    }
    if (exp == Expression::Sleepy) {
      spi->fillEllipse(cx, cy, 6, 8, col);
      return;
    }

    int w = (exp == Expression::Doubt) ? 16 : 42;
    spi->fillRect(cx - w / 2, cy - 1, w, 2, col);
  }
};

class CustomBrow final : public Drawable {
  bool isLeft;
 public:
  CustomBrow(bool isLeft) : isLeft(isLeft) {}
  void draw(M5Canvas *spi, BoundingRect rect, DrawContext *ctx) override {
    Expression exp = ctx->getExpression();
    uint16_t col = ctx->getColorDepth() == 1 ? 1 : ctx->getColorPalette()->get(COLOR_PRIMARY);

    // Subtitle bar (drawn once by right brow instance)
    if (!isLeft && g_subNLines > 0) {
      drawSubtitle(spi, col);
    }

    // Decorations (drawn once by right brow)
    if (!isLeft) {
      if (exp == Expression::Angry) drawAngerMark(spi, 272, 52, col);
      if (exp == Expression::Sleepy) {
        spi->setTextColor(col);
        spi->setTextSize(3); spi->drawString("Z", 264, 32);
        spi->setTextSize(2); spi->drawString("z", 250, 58);
        spi->setTextSize(1); spi->drawString("z", 240, 76);
        spi->setTextSize(1);
      }
      if (exp == Expression::Doubt) {
        spi->setTextColor(col);
        spi->setTextSize(3); spi->drawString("?", 262, 36);
        spi->setTextSize(1);
      }
    }

    if (exp == Expression::Neutral || exp == Expression::Happy || exp == Expression::Sleepy) return;
    int cx = rect.getCenterX();
    int cy = rect.getCenterY();
    int bw = 36;
    int innerTilt = 0;
    if (exp == Expression::Angry) {
      innerTilt = 10;
      cy += 4;
    }
    if (exp == Expression::Sad)    innerTilt = -8;
    if (exp == Expression::Doubt) {
      if (isLeft) { innerTilt = -6; cy -= 5; }
      else        { cy += 3; }
    }
    if (exp == Expression::Happy)  innerTilt = -5;
    int x0 = cx - bw / 2;
    int x1 = cx + bw / 2;
    int y0, y1;
    if (isLeft) {
      y0 = cy - innerTilt;
      y1 = cy + innerTilt;
    } else {
      y0 = cy + innerTilt;
      y1 = cy - innerTilt;
    }
    spi->drawLine(x0, y0, x1, y1, col);
    spi->drawLine(x0, y0 + 1, x1, y1 + 1, col);
  }
};

// StackChan camera pins (GC0308)
#define CAM_PIN_SIOD  12
#define CAM_PIN_SIOC  11
#define CAM_PIN_XCLK  -1
#define CAM_PIN_VSYNC 46
#define CAM_PIN_HREF  38
#define CAM_PIN_PCLK  45
#define CAM_PIN_D0    39
#define CAM_PIN_D1    40
#define CAM_PIN_D2    41
#define CAM_PIN_D3    42
#define CAM_PIN_D4    15
#define CAM_PIN_D5    16
#define CAM_PIN_D6    48
#define CAM_PIN_D7    47

WebServer server(80);
Avatar avatar;

// ============ State ============

String currentExprName = "neutral";
Expression baseExpr = Expression::Neutral;
unsigned long touchExprTime = 0;
bool touchExprActive = false;

// Double-tap detection
unsigned long lastValidTap = 0;
const unsigned long DOUBLE_TAP_WINDOW = 800;

// Shake detection (BMI270 accelerometer)
bool imuReady = false;
float prevAccelMag = 1.0f;
int shakeCount = 0;
unsigned long lastShakeCheck = 0;
unsigned long lastShakeReaction = 0;
const unsigned long SHAKE_COOLDOWN = 5000;
const float SHAKE_THRESHOLD = 0.8f;

// ============ Audio ============

static const int RECORD_SAMPLE_RATE = 16000;
static const int RECORD_CHANNELS = 1;
static const int RECORD_BITS = 16;
bool micActive = false;
bool speakerActive = false;
uint8_t* g_playBuf = nullptr;

// /play 原来是阻塞到播完才返回的 handler——ESP32 的 WebServer 单线程，播放
// 期间（可能好几秒）整个设备完全不响应任何其它请求，包括 /touch，导致"讲话
// 时摸一下立刻打断"这种交互根本做不到：host 端发出的 stop 请求要等当前这次
// /play 的 handler 自己跑完才轮得到处理，那时候都已经播完了，等于打断永远
// 迟到。改成跟 /stream 一样的后台 FreeRTOS 任务模式：handlePlay() 只负责
// 校验参数、启动 playTaskFn()、立刻返回；真正的下载+解析+分块播放放到后台
// 任务里做，g_playShouldStop 由 handlePlay() 的 stop=1 分支或需要打断播放的
// 地方直接置位，playTaskFn() 的下载/播放循环里频繁检查这个 flag，检测到就
// 立刻调 M5.Speaker.stop() 硬切断，不等手头这一小段播完。
std::atomic<bool> g_playTaskRunning{false};
std::atomic<bool> g_playShouldStop{false};
TaskHandle_t g_playTaskHandle = nullptr;
static char g_playUrl[256];

// ============ Lip Sync ============
bool g_lipSyncActive = false;
int16_t* g_lipData = nullptr;
uint32_t g_lipSamples = 0;
uint32_t g_lipRate = 16000;
unsigned long g_lipStart = 0;
unsigned long g_lipLastUpdate = 0;
const float LIP_RMS_SCALE = 1800.0f;

void startMic() {
  if (speakerActive) {
    while (M5.Speaker.isPlaying()) { delay(1); }
    M5.Speaker.end();
    speakerActive = false;
  }
  if (!micActive) {
    M5.Mic.begin();
    micActive = true;
  }
}

void startSpeaker() {
  if (micActive) {
    while (M5.Mic.isRecording()) { delay(1); }
    M5.Mic.end();
    micActive = false;
  }
  if (!speakerActive) {
    M5.Speaker.begin();
    M5.Speaker.setVolume(255);
    M5.Speaker.setChannelVolume(0, 255);
    speakerActive = true;
  }
}

// ============ Mic streaming (background task) ============
// /stream used to block the ESP32's single-threaded HTTP server (the same
// thread that also runs M5StackChan.update() and every other endpoint) for
// its whole duration — fine for an occasional one-off call, but once the
// host wants to keep the mic streaming continuously (to replace /volume
// polling), that blocking meant the whole device would freeze — no face
// animation, no /servo, no /touch, no /face, nothing — for as long as
// streaming was active, which would be almost always. Fixed by moving the
// connect/record/send loop onto its own FreeRTOS task pinned to core 0,
// away from the main loop() (which the Arduino framework runs on core 1),
// so the two run in true parallel and the HTTP server stays responsive.
//
// The mic and speaker share one I2S peripheral (see startMic()/startSpeaker()
// above — starting one always stops the other), so this background task and
// any handler that needs the speaker (handlePlay(), and defensively
// handleRecord()/handleVolume() too) must not touch it at the same time:
// g_i2sMutex serializes the actual begin/end/record/playRaw calls, and
// g_streamPauseForOtherAudio tells the streaming task to back off and not
// even try to reacquire the mic while something else needs the peripheral —
// without that second flag, the streaming task would grab the mic back (via
// startMic(), which stops the speaker) on its very next loop iteration,
// ~100ms later, cutting off playback almost as soon as it started.
std::mutex g_i2sMutex;
std::atomic<bool> g_streamTaskRunning{false};
std::atomic<bool> g_streamPauseForOtherAudio{false};
std::atomic<int> g_streamPort{0};
TaskHandle_t g_streamTaskHandle = nullptr;

// 舵机转动本身的机械噪音很容易被 host 端 MicStream 的 RMS 阈值误判成"有人
// 在说话"（尤其隐私姿势、扫描找人这类幅度比较大的动作）。M5StackChan::
// Motion::isMoving() 能精确反映舵机当前是不是还在物理转动，转动期间及停止
// 后一小段冷却时间内，streamTaskFn() 推流的音频会被替换成静音，噪音从源头
// 就不会进流。
//
// 但不能不分青红皂白地对着"isMoving()==true"就静音——人脸追踪（好奇/思考
// 期间的 track_face_once()、开心状态的 retrack_face()）也会频繁小幅度调整
// yaw，这些调整跟老大正在说话是完全可能同时发生的（追踪本来就是按固定
// 间隔触发，不看老大是不是正好在说话），第一版不分场景全部静音时，这些追踪
// 动作会把正在录的真人语音也挖出一段空白，表现为"说话说不全"。所以加了
// g_currentMoveIsNoisy 这个开关：只有 host 端明确标记"这次移动比较吵、这
// 期间不指望还在听人说话"的移动（隐私姿势、扫描找人、开心/兴奋/困倦/抱歉
// 这些反应型动画）才会真的触发静音，见 handleServo() 的 mute 参数和
// handleHome()；人脸追踪那种"对话进行中顺手微调"的移动默认不带这个标记，
// 不会静音。
std::atomic<bool> g_muteStreamForServo{false};
std::atomic<bool> g_currentMoveIsNoisy{false};
static const unsigned long SERVO_MUTE_COOLDOWN_MS = 300;

static void streamTaskFn(void* param) {
  WiFiClient client;
  static int16_t chunk[1600];
  unsigned long lastConnectAttempt = 0;

  while (g_streamTaskRunning) {
    if (g_streamPauseForOtherAudio) {
      if (client.connected()) client.stop();
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }

    if (!client.connected()) {
      unsigned long now = millis();
      if (now - lastConnectAttempt < 1000) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
      lastConnectAttempt = now;
      if (!client.connect(CALLBACK_HOST, g_streamPort)) { continue; }
      client.setNoDelay(true);
    }

    bool ok;
    {
      std::lock_guard<std::mutex> lock(g_i2sMutex);
      if (g_streamPauseForOtherAudio) continue;  // re-check now that we hold the lock
      startMic();
      ok = M5.Mic.record(chunk, 1600, RECORD_SAMPLE_RATE);
    }
    if (!ok) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }

    // 仍然照常读麦克风（保持 I2S 缓冲区正常排空、时序不乱），只是舵机还在
    // 动/刚停不久的这段时间，发给 host 的这一帧直接换成静音，不让噪音真的
    // 影响到 VAD 判断。
    if (g_muteStreamForServo) {
      memset(chunk, 0, sizeof(chunk));
    }

    size_t off = 0;
    while (off < sizeof(chunk)) {
      size_t n = client.write(((const uint8_t*)chunk) + off, sizeof(chunk) - off);
      if (n == 0) { client.stop(); break; }
      off += n;
    }
  }

  if (client.connected()) client.stop();
  {
    std::lock_guard<std::mutex> lock(g_i2sMutex);
    if (micActive) { M5.Mic.end(); micActive = false; }
  }
  g_streamTaskHandle = nullptr;
  vTaskDelete(nullptr);
}

void startStreamTask(int port) {
  g_streamPort = port;
  if (g_streamTaskRunning) return;  // already running — next reconnect picks up the new port
  g_streamTaskRunning = true;
  xTaskCreatePinnedToCore(streamTaskFn, "micStream", 8192, nullptr, 1, &g_streamTaskHandle, 0);
}

void stopStreamTask() {
  g_streamTaskRunning = false;  // task notices, cleans up, and deletes itself
}

void writeWavHeader(uint8_t* buf, uint32_t dataLen) {
  uint32_t fileSize = 36 + dataLen;
  buf[0]='R'; buf[1]='I'; buf[2]='F'; buf[3]='F';
  memcpy(buf+4, &fileSize, 4);
  buf[8]='W'; buf[9]='A'; buf[10]='V'; buf[11]='E';
  buf[12]='f'; buf[13]='m'; buf[14]='t'; buf[15]=' ';
  uint32_t fmtSize = 16;     memcpy(buf+16, &fmtSize, 4);
  uint16_t audioFmt = 1;     memcpy(buf+20, &audioFmt, 2);
  uint16_t channels = RECORD_CHANNELS; memcpy(buf+22, &channels, 2);
  uint32_t srate = RECORD_SAMPLE_RATE; memcpy(buf+24, &srate, 4);
  uint32_t byteRate = RECORD_SAMPLE_RATE * RECORD_CHANNELS * (RECORD_BITS/8);
  memcpy(buf+28, &byteRate, 4);
  uint16_t blockAlign = RECORD_CHANNELS * (RECORD_BITS/8);
  memcpy(buf+32, &blockAlign, 2);
  uint16_t bps = RECORD_BITS; memcpy(buf+34, &bps, 2);
  buf[36]='d'; buf[37]='a'; buf[38]='t'; buf[39]='a';
  memcpy(buf+40, &dataLen, 4);
}

// ============ Touch Callback ============

const int TOUCH_PORT = 7070;
const int VOICE_PORT = 7072;

void notifyCallback(const char* type, int port) {
  WiFiClient client;
  if (client.connect(CALLBACK_HOST, port)) {
    String body = String("{\"event\":\"touch\",\"type\":\"") + type + "\"}";
    client.println("POST /touch HTTP/1.1");
    client.printf("Host: %s\r\n", CALLBACK_HOST);
    client.println("Content-Type: application/json");
    client.printf("Content-Length: %d\r\n", body.length());
    client.println();
    client.print(body);
    client.stop();
  }
}

// ============ Camera ============

bool cameraReady = false;
String cameraError = "";

bool initCamera() {
  M5.In_I2C.release();

  camera_config_t config;
  config.pin_pwdn = -1;
  config.pin_reset = -1;
  config.pin_xclk = CAM_PIN_XCLK;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_d7 = CAM_PIN_D7;
  config.pin_d6 = CAM_PIN_D6;
  config.pin_d5 = CAM_PIN_D5;
  config.pin_d4 = CAM_PIN_D4;
  config.pin_d3 = CAM_PIN_D3;
  config.pin_d2 = CAM_PIN_D2;
  config.pin_d1 = CAM_PIN_D1;
  config.pin_d0 = CAM_PIN_D0;
  config.pin_vsync = CAM_PIN_VSYNC;
  config.pin_href = CAM_PIN_HREF;
  config.pin_pclk = CAM_PIN_PCLK;
  config.xclk_freq_hz = 20000000;
  config.ledc_timer = LEDC_TIMER_0;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.pixel_format = PIXFORMAT_RGB565;
  config.frame_size = FRAMESIZE_QVGA;
  config.jpeg_quality = 0;
  // fb_count=1 + GRAB_WHEN_EMPTY: capture on demand only.
  // With fb_count=2 + GRAB_LATEST the camera DMA streams 24/7 (30fps into
  // PSRAM even when idle), causing sporadic crashes/reboots under periodic
  // /camera polling (e.g. face tracking).
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.sccb_i2c_port = -1;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    char buf[64];
    snprintf(buf, sizeof(buf), "esp_camera_init error: 0x%x (%s)", err, esp_err_to_name(err));
    cameraError = String(buf);
    return false;
  }
  cameraError = "none";
  return true;
}

// ============ HTTP Handlers ============

void handleRoot() {
  String html = "<html><head><title>StackChan API</title></head><body>";
  html += "<h1>StackChan HTTP API</h1>";
  html += "<h2>Face</h2>";
  html += "<a href='/face?expr=neutral'>Neutral</a> | ";
  html += "<a href='/face?expr=happy'>Happy</a> | ";
  html += "<a href='/face?expr=sad'>Sad</a> | ";
  html += "<a href='/face?expr=angry'>Angry</a> | ";
  html += "<a href='/face?expr=sleepy'>Sleepy</a> | ";
  html += "<a href='/face?expr=doubt'>Doubt</a> | ";
  html += "<a href='/face?expr=love'>Love</a> | ";
  html += "<a href='/face?expr=eyeroll'>Eyeroll</a>";
  html += "<h2>Servo</h2>";
  html += "<a href='/servo?yaw=0&pitch=450'>Center</a> | ";
  html += "<a href='/servo?yaw=600&pitch=450'>Look Left</a> | ";
  html += "<a href='/servo?yaw=-600&pitch=450'>Look Right</a>";
  html += "<h2>Camera</h2>";
  html += "<a href='/camera'>Capture</a>";
  html += "<h2>Voice</h2>";
  html += "<a href='/record?seconds=3'>Record 3s</a> | ";
  html += "<a href='/volume'>Volume</a>";
  html += "<p>/play?url=http://host/file.wav to play audio</p>";
  html += "<h2>Status</h2>";
  html += "<a href='/status'>Status</a>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

void handleFace() {
  String expr = server.arg("expr");
  bool valid = true;
  g_customExpr = "";
  if (expr == "neutral") { avatar.setExpression(Expression::Neutral); baseExpr = Expression::Neutral; }
  else if (expr == "happy") { avatar.setExpression(Expression::Happy); baseExpr = Expression::Happy; }
  else if (expr == "sad") { avatar.setExpression(Expression::Sad); baseExpr = Expression::Sad; }
  else if (expr == "angry") { avatar.setExpression(Expression::Angry); baseExpr = Expression::Angry; }
  else if (expr == "sleepy") { avatar.setExpression(Expression::Sleepy); baseExpr = Expression::Sleepy; }
  else if (expr == "doubt") { avatar.setExpression(Expression::Doubt); baseExpr = Expression::Doubt; }
  else if (expr == "love" || expr == "eyeroll" ||
           expr == "thinking" || expr == "excited" || expr == "privacy" ||
           expr == "grieved" || expr == "peekaboo") {
    avatar.setExpression(Expression::Neutral);
    baseExpr = Expression::Neutral;
    g_customExpr = expr;
  }
  else if (expr == "curious") {
    avatar.setExpression(Expression::Doubt);
    baseExpr = Expression::Doubt;
  }
  else if (expr == "sorry") {
    avatar.setExpression(Expression::Sad);
    baseExpr = Expression::Sad;
  }
  else { valid = false; }

  if (!valid) {
    server.send(400, "application/json", "{\"error\":\"unknown expression. options: neutral/happy/sad/angry/sleepy/doubt/love/eyeroll\"}");
    return;
  }
  currentExprName = expr;
  touchExprActive = false;
  server.send(200, "application/json", "{\"ok\":true,\"expr\":\"" + expr + "\"}");
}

// LED modes: 表情映射表里每个状态大多要求 LED "持续"播放某种效果（呼吸/
// 闪烁/彩虹快闪），不是进状态那一刻闪一下就完事，之前如果让 host 端每隔
// 100~300ms 打一次 /led 来模拟，等于给这个本来就不高频调用的接口硬造出一个
// 类似 /volume 当年那样的高频轮询——CLAUDE.md 里记过那次教训（碎片化堆，
// 最后设备反复重启）。这里改成固件自己在本地用 updateLed() 持续驱动效果
// （从 loop() 里非阻塞地调用，不用 delay()），host 端只在状态切换时调一次
// /led 告诉固件"从现在起用哪种模式"，之后固件自己接管，不需要持续发请求。
enum class LedMode { OFF, SOLID, BLINK, BREATHE, RAINBOW, FADE_OUT, FADE_IN };
LedMode g_ledMode = LedMode::OFF;
uint8_t g_ledR = 0, g_ledG = 0, g_ledB = 0;      // BLINK/BREATHE/FADE_OUT/FADE_IN 的基色
uint32_t g_ledPeriodMs = 200;                     // BLINK/BREATHE/RAINBOW 的周期
uint32_t g_ledFadeMs = 2000;                      // FADE_OUT/FADE_IN 的总时长
uint32_t g_ledModeStartMs = 0;                    // 本次模式的起始 millis()，动画相位从这里算
bool g_ledFadeSettled = false;                    // FADE_OUT/FADE_IN 到终点以后只需要真正调一次
unsigned long g_ledLastUpdateMs = 0;              // 节流：不用每次 loop() 都重算颜色

static const uint8_t LED_RAINBOW_COLORS[][3] = {
  {255, 0, 0}, {255, 140, 0}, {255, 255, 0}, {0, 200, 0}, {0, 120, 255}, {160, 0, 220},
};
static const int LED_RAINBOW_COUNT = sizeof(LED_RAINBOW_COLORS) / sizeof(LED_RAINBOW_COLORS[0]);

// 从 loop() 里每次都调用，内部自己节流到约 50Hz——比任何一种效果的周期都
// 密，肉眼看不出跟"每帧都算"的差别，但省掉大部分冗余的 showRgbColor() 调用。
static void updateLed() {
  unsigned long now = millis();
  if (now - g_ledLastUpdateMs < 20) return;
  g_ledLastUpdateMs = now;
  uint32_t t = now - g_ledModeStartMs;

  switch (g_ledMode) {
    case LedMode::OFF:
    case LedMode::SOLID:
      break;  // 静态颜色只需要在 handleLed() 里设一次，这里不用重复发送
    case LedMode::BLINK: {
      bool on = (t / (g_ledPeriodMs / 2)) % 2 == 0;
      M5StackChan.showRgbColor(on ? g_ledR : 0, on ? g_ledG : 0, on ? g_ledB : 0);
      break;
    }
    case LedMode::BREATHE: {
      float phase = fmodf((float)t, (float)g_ledPeriodMs) / (float)g_ledPeriodMs;
      float bness = (1.0f - cosf(2.0f * PI * phase)) / 2.0f;  // 0..1 平滑呼吸
      M5StackChan.showRgbColor((uint8_t)(g_ledR * bness), (uint8_t)(g_ledG * bness), (uint8_t)(g_ledB * bness));
      break;
    }
    case LedMode::RAINBOW: {
      int idx = (t / g_ledPeriodMs) % LED_RAINBOW_COUNT;
      M5StackChan.showRgbColor(LED_RAINBOW_COLORS[idx][0], LED_RAINBOW_COLORS[idx][1], LED_RAINBOW_COLORS[idx][2]);
      break;
    }
    case LedMode::FADE_OUT: {
      if (t >= g_ledFadeMs) {
        if (!g_ledFadeSettled) {
          M5StackChan.showRgbColor(0, 0, 0);
          g_ledFadeSettled = true;
        }
      } else {
        float frac = 1.0f - (float)t / (float)g_ledFadeMs;
        M5StackChan.showRgbColor((uint8_t)(g_ledR * frac), (uint8_t)(g_ledG * frac), (uint8_t)(g_ledB * frac));
      }
      break;
    }
    case LedMode::FADE_IN: {
      // FADE_OUT 的反向：从熄灭渐亮到基色，到终点以后停在基色常亮，不用
      // 另外的 SOLID 调用衔接。
      if (t >= g_ledFadeMs) {
        if (!g_ledFadeSettled) {
          M5StackChan.showRgbColor(g_ledR, g_ledG, g_ledB);
          g_ledFadeSettled = true;
        }
      } else {
        float frac = (float)t / (float)g_ledFadeMs;
        M5StackChan.showRgbColor((uint8_t)(g_ledR * frac), (uint8_t)(g_ledG * frac), (uint8_t)(g_ledB * frac));
      }
      break;
    }
  }
}

void handleLed() {
  if (server.hasArg("off") && server.arg("off") == "1") {
    g_ledMode = LedMode::OFF;
    M5StackChan.showRgbColor(0, 0, 0);
    server.send(200, "application/json", "{\"ok\":true,\"off\":true}");
    return;
  }

  int r = constrain(server.hasArg("r") ? server.arg("r").toInt() : 0, 0, 255);
  int g = constrain(server.hasArg("g") ? server.arg("g").toInt() : 0, 0, 255);
  int b = constrain(server.hasArg("b") ? server.arg("b").toInt() : 0, 0, 255);
  long periodMs = constrain(server.hasArg("period_ms") ? server.arg("period_ms").toInt() : 200, 20, 10000);
  long fadeMs = constrain(server.hasArg("fade_ms") ? server.arg("fade_ms").toInt() : 2000, 100, 20000);
  String mode = server.hasArg("mode") ? server.arg("mode") : "solid";

  g_ledR = r; g_ledG = g; g_ledB = b;
  g_ledPeriodMs = (uint32_t)periodMs;
  g_ledFadeMs = (uint32_t)fadeMs;
  g_ledModeStartMs = millis();
  g_ledFadeSettled = false;

  if (mode == "blink") g_ledMode = LedMode::BLINK;
  else if (mode == "breathe") g_ledMode = LedMode::BREATHE;
  else if (mode == "rainbow") g_ledMode = LedMode::RAINBOW;
  else if (mode == "fade") g_ledMode = LedMode::FADE_OUT;
  else if (mode == "fade_in") g_ledMode = LedMode::FADE_IN;
  else { g_ledMode = LedMode::SOLID; M5StackChan.showRgbColor(r, g, b); }

  // 用 static 缓冲区 + snprintf 拼 JSON，不用 String 拼接——这个接口现在每次
  // 状态切换才调一次，调用频率不高，但顺手按项目里已经确立的零堆分配习惯写。
  static char buf[160];
  snprintf(buf, sizeof(buf),
    "{\"ok\":true,\"mode\":\"%s\",\"r\":%d,\"g\":%d,\"b\":%d,\"period_ms\":%lu,\"fade_ms\":%lu}",
    mode.c_str(), r, g, b, (unsigned long)periodMs, (unsigned long)fadeMs);
  server.send(200, "application/json", buf);
}

// 关闭/唤醒屏幕背光+面板睡眠（M5GFX LGFXBase::sleep()/wakeup()，setBrightness(0)
// 顺带把亮度也清零）。host 端退出程序（"关机"）流程的最后一步用；avatar 的
// 渲染任务不需要跟着停，睡眠状态下面板不显示画面，唤醒后会立刻显示渲染任务
// 这段时间一直在画的最新一帧，不需要额外刷新。
void handleDisplay() {
  if (server.hasArg("off") && server.arg("off") == "1") {
    M5StackChan.Display().sleep();
    server.send(200, "application/json", "{\"ok\":true,\"off\":true}");
    return;
  }
  if (server.hasArg("on") && server.arg("on") == "1") {
    M5StackChan.Display().wakeup();
    server.send(200, "application/json", "{\"ok\":true,\"on\":true}");
    return;
  }
  server.send(400, "application/json", "{\"error\":\"missing off=1 or on=1\"}");
}

void handleServo() {
  int yaw = server.hasArg("yaw") ? server.arg("yaw").toInt() : 0;
  int pitch = server.hasArg("pitch") ? server.arg("pitch").toInt() : 450;
  int speed = server.hasArg("speed") ? server.arg("speed").toInt() : 500;
  yaw = constrain(yaw, -1280, 1280);
  pitch = constrain(pitch, 0, 900);
  speed = constrain(speed, 0, 1000);
  // mute=1：这次移动交给 host 端标记为"比较吵、这期间不指望还在听人说话"
  // （反应型动画/扫描/隐私姿势），会触发 g_muteStreamForServo；不传或者
  // mute!=1（比如人脸追踪的小幅度微调）则不会——每次调用都要显式赋值，不能
  // 只在 mute=1 时设 true，不然上一次"吵"的移动会一直脏着这个标志，污染
  // 下一次本该安静的追踪微调。
  g_currentMoveIsNoisy = (server.hasArg("mute") && server.arg("mute") == "1");
  M5StackChan.Motion.move(yaw, pitch, speed);
  server.send(200, "application/json",
    "{\"ok\":true,\"yaw\":" + String(yaw) +
    ",\"pitch\":" + String(pitch) +
    ",\"speed\":" + String(speed) + "}");
}

void handleCamera() {
  if (!cameraReady) {
    server.send(503, "application/json", "{\"error\":\"camera not ready\"}");
    return;
  }
  camera_fb_t* old = esp_camera_fb_get();
  if (old) esp_camera_fb_return(old);
  delay(150);  // let the sensor capture a fresh frame after the flush
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    server.send(500, "application/json", "{\"error\":\"capture failed\"}");
    return;
  }
  uint8_t* jpg_buf = NULL;
  size_t jpg_len = 0;
  bool converted = frame2jpg(fb, 80, &jpg_buf, &jpg_len);
  esp_camera_fb_return(fb);
  if (!converted || !jpg_buf) {
    server.send(500, "application/json", "{\"error\":\"jpeg conversion failed\"}");
    return;
  }
  server.sendHeader("Content-Disposition", "inline; filename=capture.jpg");
  server.send_P(200, "image/jpeg", (const char*)jpg_buf, jpg_len);
  free(jpg_buf);
}

void handleStatus() {
  // 原来是 String 拼接（每次 += 都可能触发一次重新分配），只在偶尔调用一次
  // 的场合没问题。现在 host 端等语音播完（wait_for_playback()）要按几百
  // 毫秒的间隔反复轮询这个接口的 playing 字段，虽然单次播放持续的时间不长，
  // 但累积起来已经够得上 CLAUDE.md 里 /volume 那次教训描述的"持续高频调用"
  // 量级了，所以照同样的模式改成静态缓冲区 + snprintf，零堆分配（IP 地址
  // 那个 WiFi.localIP().toString() 除外，它本身在库内部分配、且只是几个
  // 字节的小对象，不是当年 /volume 那种量级，不在这次修的范围内）。
  float voltage = M5StackChan.getBatteryVoltage();
  float current = M5StackChan.getBatteryCurrent();
  auto angles = M5StackChan.Motion.getCurrentAngles();
  static char buf[400];
  snprintf(buf, sizeof(buf),
    "{\"battery_v\":%.2f,\"battery_ma\":%.2f,\"yaw\":%d,\"pitch\":%d,"
    "\"camera\":%s,\"camera_err\":\"%s\",\"mic_streaming\":%s,\"playing\":%s,"
    "\"expr\":\"%s\",\"uptime_s\":%lu,\"ip\":\"%s\",\"rssi\":%d}",
    voltage, current, (int)angles.x, (int)angles.y,
    cameraReady ? "true" : "false",
    cameraError.c_str(),
    g_streamTaskRunning ? "true" : "false",
    g_playTaskRunning ? "true" : "false",
    currentExprName.c_str(),
    (unsigned long)(millis() / 1000),
    WiFi.localIP().toString().c_str(),
    (int)WiFi.RSSI());
  server.send(200, "application/json", buf);
}

void handleHome() {
  // /home 只在"回正/归位"这类明确不是追踪对话的场合被调用，一律当作吵的。
  g_currentMoveIsNoisy = true;
  M5StackChan.Motion.goHome();
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleTouch() {
  auto intensities = M5StackChan.TouchSensor.getIntensities();
  bool pressed = M5StackChan.TouchSensor.isPressed();
  // held_ms：当前这一次连续按住已经持续了多久，直接读 Button_Class 内部
  // 一直在维护的状态（每帧刷新，不依赖 host 多久来问一次一次）——host 端
  // 拿这个直接判断"按满 3 秒了没有"，比 host 自己记按下时刻、事后拿轮询
  // 到的这一刻减一下要稳：host 万一被别的耗时操作（比如跑完一轮对话）
  // 卡住几秒没来得及轮询，固件这边这个数值依然精确，不会因为轮询节奏被
  // 打乱而算错。
  uint32_t heldMs = pressed
    ? (M5StackChan.TouchSensor.getUpdateMsec() - M5StackChan.TouchSensor.lastChange())
    : 0;
  // double_tap_count/screen_tap_count：见声明处注释，单调递增计数器，
  // host 端比较差值判断"有没有发生过"，不会因为轮询跟不上手势本身的判定
  // 节奏而漏掉。
  static char buf[220];
  snprintf(buf, sizeof(buf),
    "{\"front\":%d,\"middle\":%d,\"back\":%d,\"pressed\":%s,\"held_ms\":%lu,"
    "\"double_tap_count\":%lu,\"screen_tap_count\":%lu}",
    intensities[0], intensities[1], intensities[2],
    pressed ? "true" : "false",
    (unsigned long)heldMs,
    (unsigned long)g_headDoubleTapCount,
    (unsigned long)g_screenTapCount);
  server.send(200, "application/json", buf);
}

// ============ Audio Handlers ============

static const uint32_t MAX_RECORD_SAMPLES = RECORD_SAMPLE_RATE * 15;
static const uint32_t MAX_RECORD_BYTES = MAX_RECORD_SAMPLES * sizeof(int16_t);
static const uint32_t MAX_WAV_BYTES = 44 + MAX_RECORD_BYTES;
static int16_t* audioBuffer = NULL;
static uint8_t* wavBuffer = NULL;

void initAudioBuffers() {
  audioBuffer = (int16_t*)heap_caps_malloc(MAX_RECORD_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  wavBuffer = (uint8_t*)heap_caps_malloc(MAX_WAV_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  Serial.printf("Audio buffers: audio=%p wav=%p\n", audioBuffer, wavBuffer);
}

unsigned long lastRecordEnd = 0;
const unsigned long RECORD_COOLDOWN = 500;

float chunkRMS(int16_t* buf, int len) {
  int64_t sum = 0;
  for (int i = 0; i < len; i++) sum += (int64_t)buf[i] * buf[i];
  return sqrt((float)sum / len);
}

void handleRecord() {
  if (millis() - lastRecordEnd < RECORD_COOLDOWN) {
    server.send(429, "application/json", "{\"error\":\"cooldown\"}");
    return;
  }
  if (!audioBuffer || !wavBuffer) {
    server.send(500, "application/json", "{\"error\":\"audio buffers not initialized\"}");
    return;
  }

  int seconds = server.hasArg("seconds") ? server.arg("seconds").toInt() : 3;
  seconds = constrain(seconds, 1, 15);
  bool useVad = server.hasArg("vad") && server.arg("vad") == "1";
  bool showLed = !server.hasArg("led") || server.arg("led") != "0";

  uint32_t maxSamples = RECORD_SAMPLE_RATE * seconds;

  if (showLed) M5StackChan.showRgbColor(0, 60, 0);
  // Defensive: the host no longer calls /record now that /stream covers
  // continuous listening, but keep this safe against manual/legacy calls by
  // pausing the mic-streaming background task for the duration — see the
  // g_streamPauseForOtherAudio comment above streamTaskFn().
  g_streamPauseForOtherAudio = true;
  const int chunkSize = 1600;
  uint32_t recorded = 0;
  {
    std::lock_guard<std::mutex> lock(g_i2sMutex);
    startMic();
    delay(100);

    int silenceChunks = 0;
    bool speechDetected = false;
    const int SILENCE_THRESHOLD = 150;
    const int SPEECH_THRESHOLD = 200;
    const int SILENCE_NEEDED = 30;

    while (recorded < maxSamples) {
      int n = min((uint32_t)chunkSize, maxSamples - recorded);
      M5.Mic.record(audioBuffer + recorded, n, RECORD_SAMPLE_RATE);
      recorded += n;

      if (useVad) {
        float rms = chunkRMS(audioBuffer + recorded - n, n);
        if (!speechDetected) {
          if (rms > SPEECH_THRESHOLD) speechDetected = true;
        } else {
          if (rms < SILENCE_THRESHOLD) {
            silenceChunks++;
            if (silenceChunks >= SILENCE_NEEDED) break;
          } else {
            silenceChunks = 0;
          }
        }
      }
    }

    // The very last M5.Mic.record() chunk before M5.Mic.end() consistently comes
    // back as a fixed garbage pattern (verified bit-identical across separate
    // recordings/reflashes — not real noise, likely a DMA descriptor that never
    // gets filled before the I2S peripheral is torn down). Record one extra
    // throwaway chunk to absorb that artifact so the real requested audio
    // (already fully captured in `recorded` samples above) stays intact.
    {
      static int16_t dummyChunk[chunkSize];
      M5.Mic.record(dummyChunk, chunkSize, RECORD_SAMPLE_RATE);
    }
    M5.Mic.end();
    micActive = false;
  }
  g_streamPauseForOtherAudio = false;
  if (showLed) M5StackChan.showRgbColor(0, 0, 0);

  uint32_t dataLen = recorded * sizeof(int16_t);
  uint32_t wavLen = 44 + dataLen;
  writeWavHeader(wavBuffer, dataLen);
  memcpy(wavBuffer + 44, audioBuffer, dataLen);

  server.sendHeader("Content-Disposition", "inline; filename=recording.wav");
  server.send_P(200, "audio/wav", (const char*)wavBuffer, wavLen);
  lastRecordEnd = millis();
}

// 后台播放任务：下载 WAV、解析、分块喂给扬声器。跟原来 handlePlay() 里的
// 逻辑几乎一样，只是搬到了单独的 FreeRTOS 任务里跑，并且在下载循环和"等
// 上一块播完"的等待循环里都会检查 g_playShouldStop，检测到就立刻
// M5.Speaker.stop() 硬切断——不是"播完手头这一块再停"，是真正意义上的立刻
// 打断。任何退出路径（正常播完/下载失败/格式不对/被打断）最终都要走到
// finishPlayTask()，保证 g_playTaskRunning 一定会被清掉，不然 handlePlay()
// 会一直以为播放还没结束。
static void finishPlayTask() {
  g_playTaskRunning = false;
  g_playTaskHandle = nullptr;
  vTaskDelete(nullptr);
}

static void playTaskFn(void* param) {
  HTTPClient http;
  http.begin(g_playUrl);
  http.setTimeout(10000);
  int httpCode = http.GET();
  if (httpCode != 200) {
    http.end();
    Serial.printf("[play] fetch failed: url=%s code=%d\n", g_playUrl, httpCode);
    finishPlayTask();
  }

  size_t len = http.getSize();
  if (len < 44 || len > 2000000) {
    http.end();
    Serial.printf("[play] bad size: %u\n", (unsigned)len);
    finishPlayTask();
  }

  if (g_playBuf) { free(g_playBuf); g_playBuf = nullptr; }

  uint8_t* wavBuf = (uint8_t*)heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!wavBuf) { wavBuf = (uint8_t*)heap_caps_malloc(len, MALLOC_CAP_8BIT); }
  if (!wavBuf) {
    http.end();
    Serial.printf("[play] malloc failed: %u bytes\n", (unsigned)len);
    finishPlayTask();
  }
  g_playBuf = wavBuf;

  // Streaming playback: parse WAV header, start speaker, feed PCM chunks
  // as they download. First sound comes out immediately, not after full download.
  WiFiClient* stream = http.getStreamPtr();
  size_t read = 0;
  unsigned long lastProgress = millis();

  bool started = false;
  size_t dataOffset = 0;
  size_t playEnd = 0;
  uint32_t sampleRate = 16000;
  size_t playedUpTo = 0;
  const size_t CHUNK_BYTES = 6400;

  // Tell the mic-streaming background task to back off before we touch the
  // speaker — see the g_streamPauseForOtherAudio comment above streamTaskFn()
  // for why this is required, not just an optimization.
  g_streamPauseForOtherAudio = true;

  while (read < len && (millis() - lastProgress < 10000) && !g_playShouldStop) {
    size_t avail = stream->available();
    if (avail > 0) {
      size_t toRead = min(avail, len - read);
      size_t got = stream->readBytes(wavBuf + read, toRead);
      read += got;
      if (got > 0) lastProgress = millis();
    } else {
      delay(1);
    }

    if (!started && read >= 44) {
      if (wavBuf[0]!='R' || wavBuf[1]!='I' || wavBuf[2]!='F' || wavBuf[3]!='F') {
        break;
      }
      size_t pos = 12;
      uint32_t dataSize = 0;
      while (pos + 8 <= read) {
        uint32_t chunkSize; memcpy(&chunkSize, wavBuf+pos+4, 4);
        if (wavBuf[pos]=='d'&&wavBuf[pos+1]=='a'&&wavBuf[pos+2]=='t'&&wavBuf[pos+3]=='a') {
          dataOffset = pos + 8; dataSize = chunkSize; break;
        }
        pos += 8 + chunkSize;
      }
      if (dataOffset > 0 && dataOffset < len) {
        memcpy(&sampleRate, wavBuf+24, 4);
        size_t audioLen = len - dataOffset;
        if (dataSize > 0 && audioLen > dataSize) audioLen = dataSize;
        playEnd = dataOffset + audioLen;
        {
          // Guards against the narrow race where the streaming task is mid
          // M5.Mic.record() (already holding the lock) right when we flip
          // g_streamPauseForOtherAudio — without this, startSpeaker() here
          // could run concurrently with that in-flight record() call.
          std::lock_guard<std::mutex> lock(g_i2sMutex);
          startSpeaker();
        }
        started = true;
        playedUpTo = dataOffset;
        g_lipData = (int16_t*)(wavBuf + dataOffset);
        g_lipSamples = audioLen / sizeof(int16_t);
        g_lipRate = sampleRate;
        g_lipSyncActive = false;
      }
    }

    if (started) {
      size_t cap = (read < playEnd) ? read : playEnd;
      size_t pending = (cap - playedUpTo) & ~((size_t)1);
      bool last = (cap >= playEnd);
      if (pending >= CHUNK_BYTES || (last && pending > 0)) {
        unsigned long w = millis();
        while (M5.Speaker.isPlaying(0) >= 2 && millis() - w < 3000 && !g_playShouldStop) {
          updateLipSync();
          delay(1);
        }
        if (g_playShouldStop) break;
        M5.Speaker.playRaw((const int16_t*)(wavBuf + playedUpTo),
                           pending / sizeof(int16_t), sampleRate, false, 1, 0, false);
        playedUpTo += pending;
        if (!g_lipSyncActive) {
          g_lipStart = millis();
          g_lipLastUpdate = 0;
          g_lipSyncActive = true;
        }
      }
    }

    updateLipSync();
  }
  http.end();

  if (g_playShouldStop) {
    // 立刻硬切断——已经喂进 DMA 缓冲区的那一小段也不让它放完，这才是"立刻
    // 打断"应有的样子，不是"播完手头这一块再停"。
    M5.Speaker.stop();
    Serial.println("[play] stopped early (interrupted)");
  } else if (!started || playEnd == 0) {
    Serial.println("[play] not a WAV or no data chunk");
  }

  g_streamPauseForOtherAudio = false;  // let the mic-streaming task resume
  g_lipSyncActive = false;
  free(wavBuf);
  g_playBuf = nullptr;
  finishPlayTask();
}

// 正常情况下 host 端会等上一次播放真正结束（自然播完或者被打断）以后才发
// 下一个 /play，这里只是防御性兜底：万一真的有新请求在旧任务还没退出时就
// 到达，必须先让旧任务停下来、清空 g_playTaskRunning，再去 free/realloc
// g_playBuf——不然旧任务还在用这块内存的时候被这里 free 掉，就是一次
// use-after-free。有限时间等待（而不是无限等），避免极端情况下卡死整个
// HTTP handler。
void stopPlayTaskAndWait(unsigned long timeoutMs = 500) {
  if (!g_playTaskRunning) return;
  g_playShouldStop = true;
  unsigned long start = millis();
  while (g_playTaskRunning && millis() - start < timeoutMs) {
    delay(2);
  }
}

void handlePlay() {
  if (server.hasArg("stop")) {
    // 只负责置位，不等待——host 端打断讲话要的就是这个 handler 尽快返回，
    // 好让 loop() 继续处理下一个请求；播放任务会在自己的循环里很快（下一次
    // 检查 g_playShouldStop 的间隔顶多几毫秒）发现并停下，真正停没停用
    // /status 的 playing 字段确认。
    g_playShouldStop = true;
    server.send(200, "application/json", "{\"ok\":true,\"stopping\":true}");
    return;
  }

  if (!server.hasArg("url")) {
    server.send(400, "application/json", "{\"error\":\"url parameter required. Usage: /play?url=http://host/file.wav\"}");
    return;
  }

  String url = server.arg("url");
  if (url.length() >= sizeof(g_playUrl)) {
    server.send(400, "application/json", "{\"error\":\"url too long\"}");
    return;
  }

  stopPlayTaskAndWait();

  snprintf(g_playUrl, sizeof(g_playUrl), "%s", url.c_str());
  g_playShouldStop = false;
  g_playTaskRunning = true;
  xTaskCreatePinnedToCore(playTaskFn, "playAudio", 8192, nullptr, 1, &g_playTaskHandle, 0);

  server.send(200, "application/json", "{\"ok\":true,\"started\":true}");
}

// Start/stop continuous mic PCM streaming to host via TCP (for real-time
// STT) — non-blocking, see streamTaskFn() above for the actual loop.
void handleStream() {
  if (server.hasArg("stop")) {
    stopStreamTask();
    server.send(200, "application/json", "{\"ok\":true,\"streaming\":false}");
    return;
  }
  int port = server.hasArg("port") ? server.arg("port").toInt() : 7073;
  startStreamTask(port);
  server.send(200, "application/json",
    "{\"ok\":true,\"streaming\":true,\"port\":" + String(port) + "}");
}

void handleSpeech() {
  String text = server.arg("text");
  g_speechDurMs = server.hasArg("dur") ? (unsigned long)server.arg("dur").toInt() : 0;
  g_speechStart = millis();
  buildSubtitle(text);
  server.send(200, "application/json",
    "{\"ok\":true,\"text\":\"" + text + "\"}");
}

void handleButton() {
  String state = server.arg("state");
  if (state == "up") { g_buttonState = 1; }
  else if (state == "down") { g_buttonState = 2; }
  else if (state == "off") { g_buttonState = 0; }
  else {
    server.send(400, "application/json", "{\"error\":\"unknown state. options: up/down/off\"}");
    return;
  }
  server.send(200, "application/json", "{\"ok\":true,\"state\":\"" + state + "\"}");
}

void handleVolume() {
  // /volume is polled continuously for the device's entire uptime (wake-word
  // listening), unlike every other handler which is only used occasionally
  // during specific states. A per-request heap malloc() for the sample
  // buffer plus Arduino String concatenation for the JSON reply — both fine
  // for occasionally-called handlers — fragment the small internal heap
  // badly under that kind of sustained, repeated call rate, and the device
  // degrades (RMS readings go erratic) and then crashes after a few dozen
  // calls. Use a static sample buffer and a fixed stack buffer + snprintf
  // for the response so this handler makes zero heap allocations.
  // Sample window is 1 full second (not the original 100ms) so a poll every
  // few seconds actually has a real chance of overlapping with speech —
  // 100ms out of every 3s (the host's poll interval) was only a ~3.3% duty
  // cycle, meaning the device was "listening" for a sliver of each cycle and
  // missed almost all real speech even after the RMS threshold was fixed.
  // Still a static buffer (not malloc), so this doesn't reintroduce the heap
  // fragmentation bug fixed above.
  static int16_t volBuf[16000];
  const int sampleCount = 16000;

  // Defensive: the host no longer polls /volume now that /stream covers
  // continuous listening, but keep this safe against manual/legacy calls —
  // see the g_streamPauseForOtherAudio comment above streamTaskFn().
  g_streamPauseForOtherAudio = true;
  {
    std::lock_guard<std::mutex> lock(g_i2sMutex);
    startMic();
    delay(100);
    M5.Mic.record(volBuf, sampleCount, RECORD_SAMPLE_RATE);
    // Same fixed-garbage-tail issue fixed in handleRecord(): the chunk
    // immediately before M5.Mic.end() reads back a constant bogus pattern
    // instead of real mic data (verified bit-identical across separate
    // recordings), inflating rms/peak even in total silence. Absorb it with a
    // throwaway read so it doesn't land in volBuf.
    {
      static int16_t dummyChunk[1600];
      M5.Mic.record(dummyChunk, 1600, RECORD_SAMPLE_RATE);
    }
    // Unlike handleRecord()/handleStream(), this handler used to leave the mic
    // running (never called M5.Mic.end()), which left the I2S mic driver
    // streaming in the background indefinitely — same class of bug as the
    // earlier camera-DMA reboot issue. Release it here just like the other
    // mic-using handlers do.
    M5.Mic.end();
    micActive = false;
  }
  g_streamPauseForOtherAudio = false;

  int64_t sumSq = 0;
  int16_t peak = 0;
  for (int i = 0; i < sampleCount; i++) {
    int16_t s = volBuf[i];
    sumSq += (int64_t)s * s;
    if (abs(s) > peak) peak = abs(s);
  }

  float rms = sqrt((float)sumSq / sampleCount);

  char json[96];
  snprintf(json, sizeof(json),
    "{\"rms\":%.1f,\"peak\":%d,\"threshold_suggestion\":%d}",
    rms, (int)peak, (int)(rms * 2));
  server.send(200, "application/json", json);
}

// ============ Lip Sync ============

void updateLipSync() {
  if (!g_lipSyncActive) return;
  if (!M5.Speaker.isPlaying()) {
    g_lipSyncActive = false;
    avatar.setMouthOpenRatio(0.0f);
    return;
  }
  if (millis() - g_lipLastUpdate < 40) return;
  g_lipLastUpdate = millis();
  uint32_t pos = (uint32_t)((millis() - g_lipStart) / 1000.0f * g_lipRate);
  if (pos < g_lipSamples) {
    uint32_t win = g_lipRate / 50;
    if (pos + win > g_lipSamples) win = g_lipSamples - pos;
    int64_t sum = 0;
    for (uint32_t i = 0; i < win; i++) {
      int32_t s = g_lipData[pos + i];
      sum += (int64_t)s * s;
    }
    float rms = sqrtf((float)sum / win);
    float ratio = rms / LIP_RMS_SCALE;
    if (ratio > 1.0f) ratio = 1.0f;
    avatar.setMouthOpenRatio(ratio);
  }
}

// ============ Setup & Loop ============

void setup() {
  Serial.begin(115200);
  M5StackChan.begin();

  // 显式熄灯：g_ledMode 全局默认就是 OFF，但那只保证 updateLed() 不会主动
  // 点亮它——如果这次是软重启（不是真的断电），LED 硬件本身可能还残留着
  // 重启前最后一次设的颜色，不会自动清零。这里直接发一次熄灯指令，不依赖
  // "没人点过它就该是暗的"这个假设。
  M5StackChan.showRgbColor(0, 0, 0);

  // M5Unified's board profile for CoreS3 sets mic magnification down to 1-2
  // (vs. the library default of 16), and the driver divides magnification by
  // (over_sampling*2)=4 internally — so the effective gain was ~0.25-0.5x,
  // i.e. actually attenuating the raw signal. That left real speech barely
  // distinguishable from ambient noise even spoken loudly right next to the
  // device. Boost it explicitly for usable wake-word/STT sensitivity.
  {
    auto mic_cfg = M5.Mic.config();
    mic_cfg.magnification = 5;
    M5.Mic.config(mic_cfg);
  }

  M5StackChan.Display().setTextSize(2);
  M5StackChan.Display().setTextColor(TFT_WHITE, TFT_BLACK);
  M5StackChan.Display().clear();
  M5StackChan.Display().setCursor(10, 10);
  M5StackChan.Display().println("Starting up...");

  // WiFi with static IP
  IPAddress staticIP(STATIC_IP);
  IPAddress gateway(GATEWAY_IP);
  IPAddress subnet(SUBNET_MASK);
  IPAddress dnsIP(DNS_IP);
  WiFi.config(staticIP, gateway, subnet, dnsIP);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setSleep(false);
  M5StackChan.Display().print("WiFi");
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    M5StackChan.Display().print(".");
    attempts++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    M5StackChan.Display().println(" OK");
    M5StackChan.Display().print("IP: ");
    M5StackChan.Display().println(WiFi.localIP().toString());
  } else {
    M5StackChan.Display().println(" FAIL");
  }

  if (MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 80);
  }

  initAudioBuffers();
  M5StackChan.Display().println(audioBuffer ? "Audio OK" : "Audio FAIL");

  M5StackChan.Display().print("Camera...");
  cameraReady = initCamera();
  M5StackChan.Display().println(cameraReady ? " OK" : " FAIL");

  imuReady = M5.Imu.isEnabled();
  M5StackChan.Display().println(imuReady ? "IMU OK" : "IMU N/A");

  server.on("/", handleRoot);
  server.on("/face", handleFace);
  server.on("/servo", handleServo);
  server.on("/camera", handleCamera);
  server.on("/status", handleStatus);
  server.on("/home", handleHome);
  server.on("/touch", handleTouch);
  server.on("/record", handleRecord);
  server.on("/stream", handleStream);
  server.on("/play", handlePlay);
  server.on("/volume", handleVolume);
  server.on("/speech", handleSpeech);
  server.on("/led", handleLed);
  server.on("/button", handleButton);
  server.on("/display", handleDisplay);
  server.begin();

  M5StackChan.Display().println("Server started!");
  delay(1500);

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
  avatar.setExpression(Expression::Neutral);

  M5StackChan.Motion.goHome(300);
}

void loop() {
  M5StackChan.update();
  server.handleClient();
  updateLed();

  // 每帧刷新真实 yaw，供 PuppyFace.h 的好奇表情镜像判断用（见声明处注释）。
  g_currentYaw = (int)M5StackChan.Motion.getCurrentAngles().x;

  // 头顶双击计数：M5StackChan.update() 刚刷新过 TouchSensor 的状态机，这里
  // 检查是不是刚好判定成立"双击"，是的话计数器 +1（见声明处注释，host 端
  // 靠比较这个值有没有变化来判断"有没有发生过一次新的双击"）。
  if (M5StackChan.TouchSensor.wasDoubleClicked()) {
    g_headDoubleTapCount++;
  }

  // 舵机噪音静音：见 g_muteStreamForServo/g_currentMoveIsNoisy 声明处的注释。
  // 只有"这次移动被标记为吵"且"确实还在物理转动"才静音，isMoving() 每帧都
  // 查，转动期间持续刷新"最后一次观测到在动"的时间戳；转动结束后要再过
  // SERVO_MUTE_COOLDOWN_MS 才解除静音，给机械振动/回响留一点消散余量。
  {
    static unsigned long lastMovingMs = 0;
    if (M5StackChan.Motion.isMoving() && g_currentMoveIsNoisy) {
      g_muteStreamForServo = true;
      lastMovingMs = millis();
    } else if (g_muteStreamForServo && millis() - lastMovingMs >= SERVO_MUTE_COOLDOWN_MS) {
      g_muteStreamForServo = false;
    }
  }

  // WiFi auto-reconnect
  static unsigned long lastWifiCheck = 0;
  if (millis() - lastWifiCheck > 30000) {
    lastWifiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.disconnect();
      WiFi.reconnect();
    }
  }

  if (millis() < 5000) { delay(10); return; }

  // Shake detection
  if (imuReady && millis() - lastShakeCheck > 50) {
    lastShakeCheck = millis();
    float ax = 0, ay = 0, az = 0;
    M5.Imu.getAccelData(&ax, &ay, &az);
    float mag = sqrtf(ax*ax + ay*ay + az*az);
    float delta = fabsf(mag - prevAccelMag);
    prevAccelMag = mag;

    if (delta > SHAKE_THRESHOLD) {
      shakeCount++;
    } else if (shakeCount > 0) {
      shakeCount--;
    }

    if (shakeCount >= 3 && millis() - lastShakeReaction > SHAKE_COOLDOWN) {
      lastShakeReaction = millis();
      shakeCount = 0;
      avatar.setExpression(Expression::Doubt);
      touchExprActive = true;
      touchExprTime = millis();
      M5StackChan.showRgbColor(255, 255, 0);
      M5StackChan.Motion.move(400, 450, 500);
      delay(200);
      M5StackChan.showRgbColor(0, 255, 255);
      M5StackChan.Motion.move(-400, 450, 500);
      delay(200);
      M5StackChan.showRgbColor(255, 0, 255);
      M5StackChan.Motion.move(200, 450, 500);
      delay(200);
      M5StackChan.showRgbColor(0, 0, 0);
      M5StackChan.Motion.goHome(300);
      notifyCallback("shake", TOUCH_PORT);
    }
  }

  // Touch reactions
  bool swiped = false;
  if (M5StackChan.TouchSensor.wasSwipedForward()) {
    swiped = true;
    avatar.setExpression(Expression::Happy);
    touchExprActive = true;
    touchExprTime = millis();
    M5StackChan.showRgbColor(255, 50, 50);
    M5StackChan.Motion.movePitch(600, 400);
    delay(300);
    M5StackChan.Motion.movePitch(300, 400);
    delay(300);
    M5StackChan.showRgbColor(0, 0, 0);
    notifyCallback("pet", TOUCH_PORT);
  }
  if (M5StackChan.TouchSensor.wasSwipedBackward()) {
    swiped = true;
    avatar.setExpression(Expression::Doubt);
    touchExprActive = true;
    touchExprTime = millis();
    notifyCallback("pet_reverse", TOUCH_PORT);
  }
  if (!swiped && M5StackChan.TouchSensor.wasPressed()) {
    int hits = 0;
    for (int i = 0; i < 5; i++) {
      delay(8);
      M5StackChan.update();
      if (M5StackChan.TouchSensor.isPressed()) hits++;
    }
    if (hits >= 3) {
      unsigned long now = millis();
      if (lastValidTap > 0 && (now - lastValidTap) < DOUBLE_TAP_WINDOW) {
        lastValidTap = 0;
        avatar.setExpression(Expression::Happy);
        touchExprActive = true;
        touchExprTime = millis();
        M5StackChan.showRgbColor(255, 100, 100);
        delay(50);
        M5StackChan.showRgbColor(0, 0, 0);
        notifyCallback("tap", TOUCH_PORT);
      } else {
        lastValidTap = now;
        M5StackChan.showRgbColor(50, 50, 100);
        delay(50);
        M5StackChan.showRgbColor(0, 0, 0);
      }
    }
  }

  // Screen touch
  static bool screenWasTouched = false;
  M5.update();
  auto t = M5.Touch.getDetail();
  if (t.wasPressed() && !screenWasTouched) {
    screenWasTouched = true;
    avatar.setExpression(Expression::Happy);
    touchExprActive = true;
    touchExprTime = millis();
    g_screenTapCount++;
    notifyCallback("screen_tap", VOICE_PORT);
  }
  if (t.wasReleased()) {
    screenWasTouched = false;
  }

  // playTaskFn() 现在在自己的后台任务里调用 updateLipSync()（跟原来
  // handlePlay() 阻塞式播放时一样，边喂数据边更新口型）——播放任务运行期间
  // 这里就不要再调一次，两个线程同时读写 g_lipSyncActive/g_lipData 等全局
  // 状态、还都在调 avatar.setMouthOpenRatio() 会有竞态。播放任务不在跑的
  // 时候（没有别的地方在维护口型）才轮到 loop() 这里兜底调用。
  if (!g_playTaskRunning) updateLipSync();

  // Reset touch expression after 3 seconds
  if (touchExprActive && (millis() - touchExprTime > 3600000)) {
    touchExprActive = false;
    avatar.setExpression(baseExpr);
    avatar.setEyeOpenRatio(1.0);
    avatar.setLeftGaze(0, 0);
    avatar.setRightGaze(0, 0);
    avatar.setIsAutoBlink(true);
  }

  delay(10);
}
