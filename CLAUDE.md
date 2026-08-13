# StackChan 小狗行为引擎

## 项目概述
基于 Stack-chan 桌面机器人的狗行为启发情感表达系统。
技术基座：zziying/stackchan-openapi（HTTP API 架构）。

## 架构
- 电脑端（Python）：行为状态机 + MediaPipe 人脸检测 + DeepSeek LLM + edge-tts
- StackChan 端（ESP32 C++）：执行舵机、表情、音频指令
- 通信方式：WiFi HTTP（StackChan IP: 192.168.137.100）

## 关键文件
- `firmware/firmware.ino` — 主固件，包含 HTTP API 服务器和表情渲染
- `firmware/PuppyFace.h` — 自定义小狗表情组件（PuppyEye, PuppyNose, PuppyEar）
- `firmware/config.h` — WiFi 配置（SSID: DAWN, 密码: 12121212）
- `host/puppy_engine_v3.py` — 行为状态机（人脸检测、触摸、空闲计时）
- `host/voice_test.py` — 语音链路（录音→STT→LLM→TTS→播放）

## HTTP API
- `/face?expr=<表情>` — 切换表情（neutral/happy/sleepy/curious/sorry/thinking/excited/privacy）
- `/servo?yaw=N&pitch=N&speed=N` — 控制舵机
- `/camera` — 拍照返回 JPEG
- `/touch` — 触摸传感器状态
- `/play?url=<url>` — 播放 WAV 音频
- `/record?seconds=N` — 录音

## 表情系统
PuppyFace.h 中每个组件的 draw() 根据 Expression 枚举和 g_customExpr 字符串画不同图形：
- `PuppyEye`（左右各一个实例）替代默认 Eye 组件，`isLeft` 区分左右。
- `PuppyNose` 替代默认 Mouth 组件，同时画鼻子、嘴巴弧线、（兴奋时）舌头。
- `PuppyEar`（左右各一个实例）替代默认 Eyebrow 组件，同时也是兴奋表情爪印动画的
  载体（爪印代码只在 `!isLeft` 的那个实例里跑一次，画左右两个爪印）。

内置 Expression 枚举值（Neutral/Happy/Sleepy/Doubt/Angry/Sad）之外，还有几个通过
`g_customExpr` 字符串扩展的自定义表情（此时 `Expression` 本身是 Neutral）：
`thinking`（眼镜+竖椭圆瞳孔）、`excited`（见下）、`privacy`（闭眼+耳朵变形）。

所有可动画参数（耳朵长宽、鼻子嘴巴大小、旋转角度、爪印出现进度等）都通过
`FloatTransition` 做平滑过渡，默认时长 500ms（`FloatTransition::DURATION_MS`）；
如果某个动画需要更快/更慢的节奏（比如兴奋表情爪印在快速交替时，默认 500ms
会导致上一次淡出还没播完、下一次又换了随机位置，看起来像"跳一下"），可以给
`FloatTransition` 传一个自定义时长（如 `FloatTransition anim_{150};`），不会
影响其它用默认时长的实例。

**兴奋（excited）表情**是目前最复杂的一个，独立由两条互不等待的时间线驱动
（都基于 `elapsedSinceTrue()` 算出"进入兴奋表情已经过了多久"）：
1. 五官时间线：进入即为静态姿势（'><' 大眼、耳朵和鼻子嘴巴都朝眼睛靠拢），
   `EXCITED_BLINK_SWING_START_MS` 之后眼睛才开始正常眨眼、耳朵才开始左右摆动。
2. 爪印时间线：`EXCITED_PAW_START_MS` 之后开始出现爪印，用 `PuppyEar` 里的
   `pawPhaseIdx_` 状态机驱动"左、右、左、右、左出现并保持、右出现并保持"的
   顺序（`totalSteps = 2*EXCITED_PAW_CYCLES + 1`），每一段交替的持续时长在
   `[EXCITED_PAW_MIN_MS, EXCITED_PAW_MAX_MS]` 内随机；每只爪印重新出现时都会
   调用 `randomizePawAppearance()` 重新随机它的位置抖动和旋转角度——**只在
   该爪印当前是"隐藏"状态、即将变为"显示"的那一刻调用**，不能在爪印已经在
   显示的时候调用，否则会看到位置突然跳变（这是本项目踩过的一个真实 bug）。
   双爪都稳定常驻之后还会一起左右摇晃。爪印的"脚掌"是三角形三个顶点+沿三条
   边连续叠圆磨出来的圆角效果（近似"布尔并集"），不是真的多边形圆角算法。
3. 表情切出时通过 `wasExcited_`/`excitedStartMs_` 判断"是不是刚重新进入"，
   刚重新进入时要把爪印相关的 `FloatTransition` 和状态机硬重置（`reset()`），
   否则会播放上一次残留状态的过渡动画。

`firmware/host/expr_review.py` 里的 `STEPS`列表维护着每个表情改动后要重点看
什么，每次改表情视觉效果后都要同步更新对应表情的说明文字。

## 编译 / 烧录 / 验证流程
```
arduino-cli compile --fqbn m5stack:esp32:m5stack_cores3 firmware
arduino-cli upload --fqbn m5stack:esp32:m5stack_cores3 --port COM3 firmware
```
本机有一个本地沙盒 HTTP 代理会拦截到设备的请求，验证时需要绕过它（`curl` 加
`--noproxy '*'`，PowerShell/bash 里也可以先清空 `http_proxy`/`https_proxy`
等环境变量），然后用 `/face?expr=<表情>` 切换表情、轮询 `/status` 的
`uptime_s` 字段确认设备没有重启（`uptime_s` 应该持续增长，如果掉回很小的值
说明触发了重启）。每次改完 `PuppyFace.h` 都应该：编译 → 烧录 → 用 `/status`
验证目标表情持续一段时间不重启 → 顺手回归测试几个其它表情确认没有连带影响。

## 其它注意事项
- vendored 的 M5Stack-Avatar 库文件 `C:\Users\89823\Documents\Arduino\libraries\
  M5Stack_Avatar\src\Effect.h` 里禁用了 Doubt/Angry/Happy/Sad/Sleepy 五个表情
  自带的装饰动画（汗滴/怒气/爱心/竖线/气泡），因为小狗脸不需要这些。这个文件
  在 git 仓库之外，重装 Arduino 库时改动会丢失，需要重新手动禁用。
- 每次成功编译 `firmware/PuppyFace.h` 之后应主动 `git add` + `git commit`，
  方便回滚到任何一个历史版本。