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
- `firmware/expr_preview/expr_preview.ino` — 设计新表情用的独立最小 sketch
  （不含 WiFi/HTTP/摄像头/麦克风，只有屏幕渲染），`#include "../PuppyFace.h"`
  引用主固件那一份，串口按回车切换表情，见"表情系统"一节里"设计全新表情"
  那段流程；`grieved`/`peekaboo`/`dizzy` 都是这么迭代出来的。
- `firmware/config.h` — WiFi 配置（SSID: DAWN, 密码: 12121212）
- `host/puppy_engine_v4.py` — 行为状态机（人脸检测、触摸、空闲计时、语音唤醒），当前最新版
- `host/voice_test.py` — 语音链路独立测试（录音→STT→LLM→TTS→播放）
- `表情映射v11.xlsx`（仓库根目录，不在 git 里）— 每个状态/动作的触发/退出条件、
  表情、舵机、LED、声音规格表，是行为设计的权威参考。这份是跟当前代码同步过的
  （v6 跟代码对不上，改完存成 v7 删掉 v6；v7 只覆盖最早的10个基础状态，后来
  加的触摸手势"贴贴"、捉迷藏游戏各阶段、`grieved`/`peekaboo` 两个新表情都
  没跟进，改完存成 v8 并删掉了 v7；v8 又漏了"定时提醒 + 生气催促"整个功能
  （`angry`/`eat`/`play` 三个表情、两条动作行），改完存成 v9 并删掉了 v8；
  v9 又漏了"再见"手势（挥手/五指捏住再放开 → 委屈 → 隐私，见下面同名一节）
  这个新增的手势扫描窗口触发路径，改完存成 v10 并删掉了 v9；v10 里"小开心"
  改名成"贴贴"（`enter_xiaokaixin()`/`play_xiaokaixin_animation()`/
  `XIAOKAIXIN_*` 同步改成 `enter_tietie()`/`play_tietie_animation()`/
  `TIETIE_*`，纯改名，行为不变），改完存成 v11 并删掉了 v10），改状态机行为
  时应该先查这张表；如果代码要改成跟表不一致的行为，应该同时更新表格，不要
  让两边再次脱节。

## HTTP API
- `/face?expr=<表情>` — 切换表情（neutral/happy/sleepy/curious/sorry/thinking/excited/privacy/grieved/peekaboo）
- `/servo?yaw=N&pitch=N&speed=N&mute=1` — 控制舵机（yaw 越大越向右转，越小越
  向左转，见下面"人脸追踪方向"）。`mute=1` 是可选的"这次移动会有明显噪音，
  转动期间请静音麦克风推流"声明，见下面"舵机噪音防误触发语音"一节；不传时
  默认不静音（人脸追踪的小幅度实时微调必须用这个默认值，否则会截断正在说的
  话）。
- `/camera` — 拍照返回 JPEG。**这是机身正面朝外的摄像头，拍到的是房间/用户，
  不是屏幕截图**——固件没有截屏接口，没有办法远程看到 LCD 上表情/按钮的实际
  渲染效果，调表情/UI 只能靠实机肉眼看或者让用户拍照确认。
- `/touch` — 触摸传感器状态，返回 `front/middle/back/pressed/held_ms/
  double_tap_count/screen_tap_count`。`held_ms`（当前这次连续按住已经持续
  多久）、`double_tap_count`/`screen_tap_count`（单调递增计数器，头顶双击/
  屏幕点击各发生一次就 +1）都是固件端权威计算的实时值，host 端不用也不应该
  自己用时间戳重新推算——见下面"触摸事件：固件是权威真相"一节。
- `/play?url=<url>` — 播放 WAV 音频；`/play?stop=1` — 立刻打断当前播放。
  **非阻塞**——下载+解析+分块播放全部搬进了固件的后台 FreeRTOS 任务
  （`firmware.ino` 的 `playTaskFn()`，模式跟 `/stream` 的 `streamTaskFn()`
  一样），`handlePlay()` 只负责校验参数、启动任务、立刻返回，不会像原来那样
  阻塞到播完才响应（那样的话讲话期间设备完全不响应 `/touch`，"讲话时摸一下
  立刻打断"根本做不到）。host 端因此不能再假设"HTTP 响应回来=播完了"，要靠
  轮询 `/status` 的 `playing` 字段判断是否还在播，见下面"讲话时触摸立刻
  打断"一节和 `puppy_engine_v4.py` 的 `wait_for_playback()`。
- `/status` 字段随功能增加陆续加了几个，容易散在各功能自己的章节里、这里
  的顶层说明反而没跟上，列一份当前的完整清单：`battery_v`/`battery_ma`
  （电池电压/电流）、`yaw`/`pitch`（舵机当前真实角度）、`camera`/
  `camera_err`（摄像头是否就绪）、`mic_streaming`（`/stream` 是否在推流，
  当前 host 端已经不用这条了，见"语音唤醒"一节的方案 C）、`playing`（是否
  有 `/play` 播放任务在跑，见"讲话时触摸立刻打断"一节）、`expr`（当前表情
  名）、`uptime_s`、`ip`、`rssi`、`imu`（`M5.Imu.isEnabled()`，BMI270 是否
  初始化成功）、`shaking`（是否正被明显晃动/拿起，见"晕(dizzy)表情/摇晃
  检测"一节）。以后再给 `/status` 加字段，记得回来更新这份清单，不要只在
  功能自己的章节里提一句就完事——这里才是有人想知道"这个接口现在到底吐
  出什么"时第一个会看的地方。
- `/record?seconds=N` — 录音（一次性、有限时长；puppy_engine_v4.py 不再用它做语音
  唤醒，改用下面的 `/stream`，仅保留给独立测试脚本用）
- `/stream?port=N` / `/stream?stop=1` — 语音唤醒的核心：StackChan 主动连到
  host 的 `port` 上持续推送 PCM（16bit/16kHz/单声道），host 端（见
  `puppy_engine_v4.py` 的 `MicStream`）常驻监听 + 滚动缓冲 + VAD 分段，断线
  自动重连。**非阻塞**——推流跑在固件的后台 FreeRTOS 任务里（详见
  `firmware.ino` 的 `streamTaskFn()`），不会像早期实现那样卡住主 HTTP 线程；
  `/play` 播放期间会短暂让它让路（共享同一个 I2S 麦克风/喇叭外设），播完自动
  恢复。
- `/led` — 除了原来的 `?r=&g=&b=`（立即设成某个静态颜色）/`?off=1`（立即熄灭），
  现在还支持 `?mode=solid/blink/breathe/rainbow/fade`（配合 `period_ms`/
  `fade_ms`）：呼吸/闪烁/彩虹快闪/渐暗这些"持续"效果由固件自己在 `loop()`
  里的 `updateLed()` 本地驱动，host 端只要在状态切换那一刻调一次，不需要
  持续轮询，见下面"LED 系统"一节。
- `/display?off=1` / `/display?on=1` — 关闭/唤醒 LCD 背光+面板睡眠
  （`M5GFX`/`LGFXBase::sleep()`/`wakeup()`，`handleDisplay()`）。目前只有
  `host/puppy_engine_v4.py` 退出程序时的 `play_shutdown_animation()` 会调
  `off=1`，`run()` 开机第一步会无条件调一次 `on=1`（防止上一轮是靠这条
  路径退出、这一轮设备本身没重启的情况下屏幕还停在关闭状态）。avatar 的
  渲染任务不需要跟着停，面板睡眠期间不显示画面，唤醒后直接显示这段时间
  一直在画的最新一帧。

## 表情系统
PuppyFace.h 中每个组件的 draw() 根据 Expression 枚举和 g_customExpr 字符串画不同图形：
- `PuppyEye`（左右各一个实例）替代默认 Eye 组件，`isLeft` 区分左右。
- `PuppyNose` 替代默认 Mouth 组件，同时画鼻子、嘴巴弧线、（兴奋时）舌头。
- `PuppyEar`（左右各一个实例）替代默认 Eyebrow 组件，同时也是兴奋表情爪印动画的
  载体（爪印代码只在 `!isLeft` 的那个实例里跑一次，画左右两个爪印）。

内置 Expression 枚举值（Neutral/Happy/Sleepy/Doubt/Angry/Sad）之外，还有几个通过
`g_customExpr` 字符串扩展的自定义表情（此时 `Expression` 本身是 Neutral）：
`thinking`（眼镜+竖椭圆瞳孔）、`excited`（见下）、`privacy`（闭眼+耳朵变形）、
`grieved`（委屈：无嘴巴，眼睛是同心正圆眼眶+瞳孔，上方一条"⌣"形担心眉毛）、
`peekaboo`（闭眼：五官基础跟 `excited` 共用一套尺寸参数，但左耳画成盖住左眼
的一片弯曲耳朵、左眼不画，鼻子嘴巴额外再缩小一档）。`grieved`/`peekaboo` 目前
只在捉迷藏游戏里触发（见下面"捉迷藏找物品游戏"一节），没有绑定到常规状态机
的任何一个 State。

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

**设计全新表情（不是微调现有表情）时，优先用 `firmware/expr_preview/
expr_preview.ino` 这个独立最小 sketch 反复迭代，而不是直接改主固件反复烧
录**——开发 `grieved`/`peekaboo` 这两个表情时用的就是这个流程：不含
WiFi/HTTP/摄像头/麦克风，只有屏幕渲染，`PuppyFace.h` 直接用相对路径
`#include "../PuppyFace.h"` 引用主固件那一份（不是复制一份改，改完直接就在
正确的地方，不需要"搬"），编译（~575KB/18% flash）和烧录（~3s）都比完整版
firmware.ino（~1.76MB/55% flash，~10s）快很多；串口按回车键切换表情（不依赖
计时器自动轮播，因为调参往往需要盯着某一个表情看好几秒）。看串口输出/发
回车用 `arduino-cli` 自带的监视器，不需要装 Arduino IDE：
`"C:\Users\89823\arduino-cli\arduino-cli.exe" monitor -p COM3 -c baudrate=115200`，
退出按 `Ctrl+C`。**这个监视器会独占串口**：只要它还开着，`arduino-cli
upload` 就会报 `Could not open COM3, the port is busy`——每一轮改完常量要
重新烧录前，先确认监视器已经关掉，烧完再重新打开监视器继续看效果，这一步
在这次开发"晕"表情时被漏过好几次。这套"改
`PuppyFace.h` 里的常量/绘制逻辑 → 编译烧录 expr_preview → 实机看效果 →
根据反馈继续改常量"的循环可以跑很多轮（这两个表情实测跑了将近十轮），
每轮的改动都应该配一条简短的常量注释说明"这一轮改了什么、依据是什么"，
方便下一轮反馈时能看懂当前参数是怎么来的、要在哪个基础上继续调，不用每次
都从头重新试。视觉效果稳定以后再把 `handleFace()` 的 `/face?expr=` 分支和
host 端的触发逻辑接上，烧录真正的主固件——这两步不要在设计阶段就做，早期
每一轮反复烧录 1.76MB 的主固件会显著拖慢迭代节奏。

## 关键词播报按钮（爪印按钮）
`PuppyFace.h` 里 `BUTTON_*` 一组常量控制的"狗粮碗"形状按钮，`qa_complex`
逐个念关键词时出现，画在屏幕右下角，形状是椭圆"碗口"叠在圆角矩形"碗身"上。
- **M5Canvas 没有真正的裁剪/布尔运算 API**，"碗口盖住碗身顶边"这个效果是靠
  三步模拟出来的：①画碗身描边 ②用背景色实心椭圆把落在碗口范围内的部分擦掉
  （连碗身的边一起擦）③补画一次碗口描边，让碗口自己的圆周保持完整。以后再
  遇到"这个形状要盖住那个形状"的需求，可以复用这个"画→擦→补画"套路。
- **改 `BUTTON_PAW_SCALE`/`BUTTON_PAW_TOE_SPREAD_MUL` 之前必须先数值验证**，
  不能凭感觉调：爪印由脚掌三角+四个脚趾（带圆角半径）组成，缩放和脚趾间距
  两个参数一起决定爪印的整体外包络多大；要求是外包络完整落在碗口椭圆内、
  同时脚趾之间不重叠。写一个小 Python 脚本照抄 `drawPawPrint()` 里的坐标计算
  逻辑（pad 三角形+沿边叠圆、四个脚趾的局部偏移和半径），采样多个点算出实际
  外包络再跟椭圆方程比较，比目测/直接改完烧录再看快得多，也更准。
- **按钮"按一下"动画要跟关键词音频同步播**：`speak_keywords()`
  （`puppy_engine_v4.py`）里，播放某个关键词前会提前一步把它的 TTS 合成好，
  这套"提前一步合成"的写法是给 edge-tts 时代（网络合成延迟几百毫秒到一两
  秒，等按钮弹起才现合成会让音频明显晚于"放大"动画）留下的；换成 animalese
  以后合成变成纯本地计算（约 10ms 量级，见"语音合成（animalese）"一节），
  严格来说已经不再需要提前量，但这个写法继续留着无害（合成本来就快，提前
  一步不会有任何副作用），这次迁移没有顺手把它简化掉。

  另外**第一个关键词前的按钮动画容易被
  吞掉**：按钮从隐藏到出现本身也是一段过渡动画（复用同一个
  `buttonScaleAnim_`），如果出现后立刻触发第一次"按一下"，可能会在出现过渡
  还没播完时就被打断，视觉上看不出"缩小"这一半——出现后要显式等一小段时间
  （等于固件那边动画时长）确认已经长到正常大小，再开始第一次按一下。

## 语音合成（animalese）
`tts_to_wav()`（`puppy_engine_v4.py`）从调用 edge-tts（可懂的中文人声）
换成了 animalese——《集合啦！动物森友会》风格的无字义拟声词，逐字母拼接
一份现成的字母音频库（`animalese.wav`，26 个英文字母各一段，首次运行自动
从 GitHub 下载并缓存到 `host/` 目录，~172KB）。核心算法直接移植自
[animalese.js](https://github.com/Acedio/animalese.js)：中文先经
`pypinyin` 转不带声调的拼音，音节之间插空格（animalese 里空格=停顿，让每
个字听起来是分开的音节），逐字母从库里截取一小段拼起来，再用
`resample_poly` 从库的原生 44100Hz 重采样到 `TTS_SAMPLE_RATE`(16000)。纯
本地计算，实测合成耗时 ~14ms，不像 edge-tts 那样有网络往返。
- **调用方完全不用改**：`speak_keywords()`/`_game_speak_keywords()`/
  `_prewarm_game_tts()` 这四个调用点全部只依赖 `tts_to_wav(text, out_stem)
  -> wav_path` 这一个签名，内部实现换了但接口没变，四处调用点一行都没动。
  以后再要迁移合成后端（比如换别的拟声词方案），只要保持这个签名，同样
  可以做到"只改一个函数"。
- **声音本身不可懂，字幕从"辅助"变成"唯一理解途径"**：edge-tts 时代用户
  凭耳朵就能听懂中文，字幕只是锦上添花；animalese 完全不可懂，用户必须
  靠屏幕字幕才知道小狗在说什么。`speak_keywords()`/`_game_speak_keywords()`
  的播放循环因此各自加了一对 `set_subtitle(keywords[i])`（`start_play()`
  之前）/`set_subtitle("")`（`finally` 块里，播完/被打断都会执行）——顺序
  不能反：字幕必须先于声音出现，不然用户会先听到拟声词、后看到字幕，体验
  上是"字幕追着声音跑"；清空放在 `finally` 而不是播放成功之后，是延续
  "'小狗小狗'呼唤"一节上面"字幕框只应该在临时展示的那段时间出现"同一条
  纪律——不管这次播放是自然播完还是被触摸打断，字幕都不能残留到下一个
  关键词开始播放的时候。
- **字幕框跟按钮在屏幕上会重叠，这次一起改了固件**：字幕框原来 308px 宽
  （几乎跟 320px 屏幕等宽，右边界在 314），跟右下角关键词按钮的区域
  （约 247~293）直接重叠——以前两者从没在同一时刻都显示过所以没暴露；
  这次两者会在同一个播放循环里同时出现，字幕框（`fillRoundRect`，实心）
  会整个盖住按钮。改成 224px（屏幕宽度 70%，`SUB_BOX_X` 不动，只收窄
  `SUB_BOX_W`），右边界落到 230，跟按钮之间留出十几像素空隙。**这处改动
  必须同时改两个地方**：`SUB_BOX_W`（画框用）和 `buildSubtitle()`
  （`firmware.ino`）里硬编码的换行宽度阈值（跟 `SUB_BOX_W`是两个独立的
  数字，`308→224` 时这个阈值要跟着从 `292→208`，只改一个的话文字会按旧
  宽度换行、超出新的更窄的框）——以后再调字幕框宽度，记得这两个数字要一
  起动。
- **`ANIMALESE_VOLUME_BOOST` 先后试过 `3.0`（独立原型 `host/animalese_
  test.py` 里针对 1W 小喇叭偏小声试出来的值）和最初设计草案里的 `1.0`
  ——接入主固件实机听过之后反馈 `3.0` 偏大声（放大 3 倍再 `np.clip` 到
  `[-1,1]` 还会有明显削波失真），改回了 `1.0`。独立原型脚本的参数是在
  另一套播放链路上试出来的，不能想当然地当作主固件的最终结论，两边听感
  不一定一致，最终以接入主固件后的实际反馈为准。
- `_prewarm_game_tts()`/`self._game_tts_cache` 这套给 edge-tts 网络延迟
  设计的预热缓存机制，animalese 时代已经不再是必需的（合成本来就
  <20ms），但继续留着无害（缓存命中时依然会跳过一次合成调用），这次
  迁移没有顺手清理，只是不再是刚需。

## 语音唤醒 / 流式监听（MicStream）
`host/puppy_engine_v4.py` 的语音唤醒不再靠轮询 `/volume` + 触发后另开
`/record`（两者都是一次性、有限时长的调用，大部分时间设备根本没在"听"）。
方案演进到现在一共有三版：A（`/volume`轮询+`/record`）→ B（StackChan 主动
通过 `/stream?port=N` 连到 host 常驻监听的 TCP 端口持续推 PCM）→ **C（当前，
改用电脑本地接的无线麦克风）**。host 端的 `MicStream` 类维护滚动缓冲区 +
简单 VAD（按 `STREAM_CHUNK_SECONDS` 分段算 RMS，超过 `STREAM_RMS_THRESHOLD`
记为说话，连续 `STREAM_SILENCE_SECONDS` 低于阈值判定说完），说完那一刻直接
把这段语音从缓冲区切出来，不需要再另外录一次——`check_voice_wake()` 因此
变成纯内存队列查询，不发 HTTP 请求，可以每个 tick 都调用；这套 VAD/缓冲逻辑
在方案 B→C 切换时完全没动，只是喂给它的音频来源换了。
- **方案 C：电脑本地无线麦克风**（`sounddevice` 直接采集，`MicStream` 类
  头部注释）——动机是环境音、尤其是 StackChan 自己转动舵机的机械噪音总是
  被机身麦克风收进去干扰识别；无线麦克风别在人身上，离嘴近、离舵机远，
  信噪比好得多。`find_wireless_mic_device()` 按 `WIRELESS_MIC_NAME_HINT`
  子串在 **Windows WASAPI** host API 下找设备（同一个物理设备在 MME/
  DirectSound/WASAPI/WDM-KS 下各出现一次，WASAPI 延迟最低）。**踩过一个坑**：
  WASAPI 共享模式不接受用任意采样率打开设备（`sd.InputStream(samplerate=
  16000, ...)` 直接报 `Invalid sample rate`），必须先查
  `dev["default_samplerate"]` 拿到设备真实原生采样率（这个无线麦克风接收器
  实测是 **48kHz 立体声**，不是随便猜的 44100 或 16000），照原生格式打开，
  再在回调里用 `scipy.signal.resample_poly`（按 `math.gcd` 现算 up/down
  比例，不要硬编码）降混单声道 + 重采样到 16kHz。阈值也要重新校准——
  `STREAM_RMS_THRESHOLD` 是给机身麦克风（固件端 `mic_cfg.magnification=5`
  放大过）校准的 600，换了麦克风增益特性完全不同（实测无线麦克风安静基线
  只有 0~25，说话能到 90~190），**换音频源必须重新测、不能沿用旧数值**。
  StackChan 端 `/stream` 相关的固件代码（`streamTaskFn()`、`g_i2sMutex`等，
  见下面两条）原样保留没删，host 只是不再调用 `/stream?port=N` 告诉它推流
  ——以后想切回机身麦克风，恢复调用即可，不需要改固件。**已知局限**：只有
  戴/拿着无线麦克风的那个人能被听到（不像机身麦克风谁在附近说话都能收到）；
  设备中途断开没有自动重连，需要重启进程。
- **切到无线麦克风以后必须在播放 TTS 期间显式静音，否则 StackChan 会打断
  自己**：机身麦克风跟喇叭共用同一个 I2S 外设、播放期间物理上就听不到自己
  的声音（不需要额外处理）；无线麦克风是电脑本地独立的物理设备，没有这层
  物理隔离，房间里放出来的 TTS 声音会被它原样录进去、误判成用户在说话，
  中途打断正在播的这句、开始新一轮对话——这也会连带拖累识别准确率（把自己
  的播报声和后半句用户说的话混在一起送去识别，容易出乱码）。修法是
  `MicStream.set_muted()`：播放期间静音本地麦克风的音频回调（数据直接丢
  弃，不进缓冲区、不参与 VAD），播完/被打断后立刻取消静音；粒度跟着每个
  关键词单独播放走（`speak_keywords()`/`_game_speak_keywords()` 循环内部，
  `start_play()`前静音、`wait_for_playback()`后取消），不是整个关键词列表
  播完才取消——关键词之间的间隔本来就是安静的，这段时间应该能正常听。以后
  给固件/host 加任何新的"设备主动出声"的地方（不只是关键词播报），只要
  改成了本地麦克风采集，都要照这个模式补一次静音，不能想当然地认为"设备
  播放"和"host 端语音采集"互不影响。
- **上面这条静音修完以后实测还是偶尔会把自己说的话录进去，根因是取消静音
  的时机比声音真正停止早了一步**：`wait_for_playback()` 靠轮询 `/status`
  的 `playing` 字段判断"播完了"，但固件 `playTaskFn()`（`firmware.ino`）分块
  播放循环里，只在喂下一块前才等上一块播完（`M5.Speaker.isPlaying(0)>=2`
  的等待循环），最后一块音频是用 `M5.Speaker.playRaw()` 非阻塞喂给 I2S 就
  直接退出任务、把 `g_playTaskRunning` 清 false——最后一个 `CHUNK_BYTES`
  （6400 字节，16kHz/16bit 下约 0.2s）在 `playing` 已经变 false 之后仍在
  物理播放。`set_muted(False)` 如果紧跟着 `wait_for_playback()` 返回就执行，
  这~0.2s 的尾音会被无线麦克风原样录进去。修法是 `speak_keywords()`/
  `_game_speak_keywords()` 里加一个 `MIC_UNMUTE_COOLDOWN_SEC`(0.35s) 冷却期：
  只有自然播完（`finished_ok=True`）才等这段时间再取消静音，被触摸打断走的
  是 `M5.Speaker.stop()` 硬切断（`stopPlayTaskAndWait()`），没有这条尾巴，
  不需要等。跟固件那边处理舵机噪音的 `SERVO_MUTE_COOLDOWN_MS`(300ms) 是
  同一个思路——**"状态标志变化"不等于"物理效果立刻消失"，这类靠轮询状态
  位判断"是否还在输出"的场景，都要留一点缓冲，不能状态一变就立刻放行**，
  以后再遇到类似"标志已经翻转但物理动作还有尾巴"的情况，先往这个方向想。
- **ESP32 的 `WebServer` 是单线程的**（`loop()` 里同步调用
  `server.handleClient()`）：任何 handler 只要阻塞得久，就会让设备在那段
  时间内完全无法响应其它请求（表情、舵机、触摸、摄像头全部卡住）。
  `/stream` 一开始就是这么写的（阻塞到超时或断开），后来发现"常驻推流"这个
  用法下这几乎等于设备大部分时间都是死的，改成了后台 FreeRTOS 任务（用
  `xTaskCreatePinnedToCore` 钉在另一个核心上跑，见 `firmware.ino` 的
  `streamTaskFn()`），HTTP handler 只负责启动/停止这个任务、立刻返回。以后
  给固件加"需要持续跑一段时间"的新功能，从一开始就该走这个模式，不要直接
  写成阻塞的 HTTP handler。
- **麦克风和喇叭共用同一个 I2S 外设**（`startMic()`/`startSpeaker()`
  互斥，见 `firmware.ino`）：后台推流任务和任何要用喇叭的 handler（`/play`，
  以及仍保留但已不用的 `/record`/`/volume`）现在通过 `g_i2sMutex` +
  `g_streamPauseForOtherAudio` 协调——没有这个协调，推流任务会在 TTS 播放
  期间的下一次循环（~100ms 后）就把麦克风抢回去，把播放打断。
- **`check_voice_wake()` 命中人脸已确认在场时跳过 `scan_for_face()`**：一次
  对话里每问一句都重新转头扫描一遍会浪费至少 `SCAN_PAUSE`(1s)、最多 5s，
  全部堆在"思考"前面。现在只要 `self.face_detected` 是新鲜的（HAPPY 状态下
  `retrack_face()` 每 `FACE_RETRACK_INTERVAL_SEC` 秒都在刷新），就直接跳过
  扫描进对话；就算这期间人其实走开了也不怕，CURIOUS 阶段的
  `track_face_once()` 会立刻重新确认，追踪失败的话 `_settle_happy()` 收尾
  时还是会补一次完整扫描。
- **增量（实时）字幕**：SenseVoice 不是流式模型，做不到真正逐字输出。
  `MicStream.peek_partial()` 在用户还在说话、还没被判定"说完"之前，把目前
  为止录到的音频整段吐出来；`PuppyEngine._partial_transcribe_loop()` 后台
  线程每隔 `PARTIAL_TRANSCRIBE_INTERVAL_SEC` 拿这段音频重新整体识别一次、
  直接整段替换字幕——旧结果因为音频不够长而识别错的部分，随音频变长通常会
  在后面几次重跑里自动"纠正"。跟 `run_conversation_turn()` 里"说完了"以后
  的正式识别共用同一个 `AutoModel` 实例，两边都要经过 `PuppyEngine._asr_lock`
  互斥——ONNX/PyTorch 在 CPU 上不保证能安全并发 `generate()`。
- **字幕框只应该在"录音+语音识别"这段时间出现，不能靠函数末尾的一个
  `finally` 兜底清空**：早期实现里，`_run_conversation_turn_body()` 识别出
  最终文本后调用 `set_subtitle(user_text, ...)`，然后一路不清空，直到整个
  函数最后的 `finally: set_subtitle("")` 才清——这中间还夹着 LLM 网络请求、
  以及"表扬→兴奋"这类意图分支触发的表情/状态切换，实测字幕框会挡住"兴奋"
  表情的一部分（"复杂回应"逐词播报、捉迷藏整局游戏这些更长的分支同样会被
  拖着，只是没那么显眼）。修法是把"清空"从"函数结束时兜底"改成"用完立刻
  显式清空"：`set_subtitle(user_text, ...)` 后紧接着就是 `set_subtitle("")`
  ——识别结果本身闪一下即可，一旦要开始等 LLM 回复就必须清掉；`_settle_happy()`
  /`enter_happy()` 之前也要补一次清空（否则会挡住这两个分支各自触发的表情）。
  以后任何"临时显示的 UI 元素后面紧跟着可能会切表情/切状态的动作"，都应该
  在触发那个动作之前显式清掉，不要依赖函数末尾兜底的 `finally`——那只是"最终
  不会卡死"的安全网，不等于"不会短暂挡住画面"。
- **`AutoModel(model="iic/SenseVoiceSmall", ...)` 每次启动都会连网络，哪怕
  模型早就缓存在本地**：funasr 的 `download_from_ms()` 只要看到 `model`
  参数是个 ID 字符串（不是本地已存在的路径），就会调 ModelScope 的
  `snapshot_download()` 挨个核对文件哈希——这才是"预加载"启动慢、偶尔卡顿
  的来源（本机还挂着代理，网络往返更容易变慢），不是真的在重新下载 ~900MB
  的权重。`local_model_cache_dir()` 会把模型 ID 解析成本地缓存目录的绝对
  路径（复刻了 funasr 内部的 `name_maps_ms` 别名表，比如 `"fsmn-vad"` 实际
  对应 `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`），传路径而不是 ID 时
  `download_from_ms()` 会直接跳过整个网络检查——实测两个模型一起加载从
  8.80s 降到 4.13s。缓存目录还没建好（比如第一次运行）就原样传回 ID 字符
  串，照常走一遍下载，不影响首次可用性。
- **关键词顺序完全由 `SYSTEM_PROMPT` 约束 LLM 直接输出，host 端不再做任何
  后处理重排**（`puppy_engine_v4.py` 的 `_run_conversation_turn_body()`）。
  早期版本用 `jieba.posseg` 判断名词/动词、机械把"名词全部排到动词前面"
  （`reorder_keywords_nouns_first()`，已删除），后来改成基于犬类 AAC 按钮的
  固定词库（枢纽词/需求词/时间词/对象词/地点词/状态词/情感词/动作词）+
  "最迫切的词放最前、指代对象或地点放前面"等提示词规则后，这套机械重排会
  破坏 LLM 自己给出的有意义顺序（比如"小狗 想 零食"里"想"必须紧跟在
  "小狗"后面，按词性重排会拆散这个组合），所以直接信任 prompt 产出的顺序。
  以后如果关键词顺序又出现问题，先检查 `SYSTEM_PROMPT` 里的规则描述/示例
  够不够清楚，而不是想着在 host 端加后处理。

## LLM 提示词（SYSTEM_PROMPT）设计
`puppy_engine_v4.py` 里的人设是比格犬"小狗"，称主人"人"（体现平等关系，见
下面那条），家庭朋友三花猫"咪咪"，公园朋友萨摩耶"耶耶"/边牧"边边"/田园犬
"大黄"；qa_complex/other 的
回应是从一套固定的 AAC 风格词库（枢纽词/需求词/时间词/对象词/地点词/状态词/
情感词/动作词）里选 2-4 个关键词。改这份 prompt 时踩过两类坑，都不是"改一次
就好"，是这个模型/这套架构下会反复遇到的模式：

- **`DEEPSEEK_MODEL = "deepseek-chat"` 是非推理模型，没有隐藏思维链**——
  如果 prompt 直接让它"输出关键词"而不要求先写一句真实的回答，它会走最省力
  的路径：直接从人问题原句里捞词拼一拼（尤其问题本身就包含词库里也有的
  词时），看起来像回答，其实是复读，答非所问（实测案例："你为什么不去找
  咪咪玩？" 曾经被答成"咪咪 想 玩"，根本没回答"为什么"）。修法是让它先输出
  一句大白话式的真实回答（这句话不会被念出来，只是逼它先"过一遍脑子"，
  这些 token 本身就是在做 CoT），再压缩成关键词，并且明确禁止关键词跟问题
  原句里的词重合。以后再遇到"LLM 给出的回应看起来没经过思考"，先怀疑是不是
  又把这种显式推理步骤精简掉了。
- **词库分类的措辞会被模型当真，标"优先级最高"的词会被当成默认填充词过度
  使用**：枢纽词（想/要/来/好/不要）一开始写的是"优先级最高，可以和任何词
  搭配"，结果几乎每次回应都会带上；把"小狗"标进对象词库后，模型也倾向于
  把它当默认开场词，即使问题根本没有需要强调"是我自己"的语境。两次都是靠
  显式把这些词降到词库里权重最低（"默认不用/默认少用，只有需要强调时才用
  一次"）才压下去的。以后新增词库分类、或者调整某个分类的说明文字时，要
  意识到这段文字本身就是在给模型的选词频率打权重，不是纯粹的分类标签。
- **指代主人的词全部是"人"，不是"老大"**：这是分两步改的，记一下顺序方便
  理解现状。第一步只改了第 5 条规则里 `对象词` 这个分类下、真正会被当成
  关键词念出来的词表（"老大"→"人"，连同规则说明里举例的"老大叫我"→
  "人叫我"、"指代对象（老大、咪咪等）"→"（人、咪咪等）"），当时特意保留了
  SYSTEM_PROMPT 描述人设/规则的大段说明文字里的"老大"（"你叫主人'老大'"
  "老大在表扬你"等），理由是"小狗心里怎么称呼主人"和"它用 AAC 按钮说话时
  选哪个词"是两件独立的事。第二步用户明确说明希望的是平等关系，不只是
  AAC 按钮词选得中性——把 SYSTEM_PROMPT 里剩下的人设/规则说明文字，以及
  host 端引用同一个称呼概念的代码注释和 `print()` 日志（`_run_conversation_
  turn_body()`/`check_voice_wake()` 附近）里的"老大"也全部换成了"人"，
  现在整个代码库里已经没有"老大"这个称呼了。以后新增涉及"小狗怎么称呼
  主人"的 prompt 文字或注释，统一用"人"，不要图省事写回"老大"。

## LED 系统
表情映射表里每个状态大多要求 LED "持续"播放某种效果（呼吸灯/快闪/彩虹快闪/
渐暗），不是进状态那一刻闪一下就结束。
- **不要在 host 端开线程连续轮询 `/led` 去模拟呼吸/闪烁**——这是 `/volume`
  那次教训（见下面"其它注意事项"）的变体：把一个本来低频调用的接口硬造成
  每秒好几次的高频轮询。改成了固件侧方案：`/led` 加了 `mode` 参数
  （solid/blink/breathe/rainbow/fade，配合 `period_ms`/`fade_ms`），
  `firmware.ino` 的 `updateLed()` 从 `loop()` 里非阻塞驱动（不用 `delay()`，
  按 `millis()` 时间戳计算当前该显示的颜色），host 端只在状态切换那一刻调
  一次 `/led` 告诉固件"从现在起用哪个模式"，之后固件自己接管，不需要持续
  发请求维持效果。以后固件要加别的"需要持续播放"的效果（不只是 LED），都
  应该优先考虑这个模式：状态在固件本地维护、`loop()` 里非阻塞更新、host 端
  只发一次性的"切换"指令。
- **一次性"借用" LED 的地方，用完必须显式恢复当前状态该有的常驻效果**：
  触发对话时闪两下白灯、`speak_keywords()` 念关键词时每个词播放期间点亮
  暖白灯，这些都是临时覆盖掉当前状态的常驻 LED 模式（比如开心的暖白常亮，
  常态目前是关灯）。之前漏过两处：`enter_happy()` 的"静默确认"分支
  （`session_active` 已经是 `True`，不重新播放开心动画）没有重新点亮；
  `speak_keywords()` 念完关键词后也没把 LED 交还回去。这两处都会导致 LED
  卡在"熄灭"上，直到下一次
  真正的完整状态切换才会恢复，中间可能是好几轮对话的时间。以后新增任何
  临时借用 LED（或者其它"应该跟随状态持续"的效果）的代码，都要问一句"用完
  之后谁负责把它还回去"。
- **当前各状态的 LED 常驻效果**（跟`表情映射v11.xlsx`保持同步）：常态
  （IDLE）是完全关闭、不点亮（`play_idle_animation()` 用 `set_led(off=True)`）；
  开心（HAPPY）是暖白灯常亮（`set_led_mode("solid", *WARM_WHITE_RGB)`，不是
  之前的闪烁）。`setup()` 里 `M5StackChan.begin()` 之后立刻加了
  `M5StackChan.showRgbColor(0, 0, 0)`，保证设备刚开机、host 脚本还没连上时
  LED 就是暗的，不会残留库自带的默认点亮状态。
- **"用完就还回去"这条规矩现在有一个统一的还回去的地方**：
  `PuppyEngine.restore_state_led()` 按 `self.state` 把 LED 设回该状态对应的
  常驻效果（覆盖全部 8 个状态，不再是只处理 HAPPY/IDLE 两种的临时拼凑）。
  `speak_keywords()` 念完关键词、`check_touch()` 里头顶触摸松开时都改成调
  这一个方法，而不是各自重复写一遍"如果是 HAPPY 就……如果是 IDLE 就……"。
  以后新增"临时借用 LED"的地方，也应该找机会结束时调 `restore_state_led()`
  而不是照抄一段新的分支判断。
- **头顶触摸传感器"感受到触摸"时点亮暖白灯，给用户一个"确实碰到了"的即时
  反馈**：`check_touch()` 检测到按下沿（`pressed` 从 False 变 True）时调
  `set_led_mode("solid", *WARM_WHITE_RGB)`，检测到松开沿时调
  `restore_state_led()` 还回去。这条路径最需要反馈的场景是头顶长按进/出
  隐私——要按满 `PRIVACY_HOLD_SEC`(3s) 才有下一步反应，中间这几秒完全没有
  任何提示的话，用户分不清"按对了在等"还是"设备根本没感应到"。用
  `set_led_mode` 而不是一次性 `/led?r=&g=&b=`：固件 `updateLed()` 每帧都会
  按当前 `g_ledMode` 重算颜色，如果当前正处于呼吸/闪烁/渐暗这类持续模式，
  一次性设色马上就会被下一帧覆盖掉，必须真正切换模式才压得住。这条基础
  版只挂在头顶触摸传感器（`TouchSensor.isPressed()`/`held_ms`）的按下沿/
  松开沿上，不含屏幕触摸——碰屏幕本身会立刻触发"贴贴"的表情+舵机动作，
  反馈已经很明显，不需要再叠加一次 LED 提示；头顶双击→兴奋这条瞬时手势另外
  在 `handle_touch_trigger()` 里单独点了一次反馈灯，见上面"触摸触发映射"
  一节，因为它不总能被这里的按下沿可靠捕捉到。

## 舵机噪音防误触发语音
麦克风灵敏度为了能听清小声说话被放大了 5 倍（`mic_cfg.magnification = 5`，
`firmware.ino` 的 `setup()`），代价是舵机转动的机械噪音也很容易越过
`MicStream` 的 RMS 阈值（`STREAM_RMS_THRESHOLD`），被 VAD 当成"有人在说话"，
说完（噪音停止）`STREAM_SILENCE_SECONDS` 后判定"说完了"，送去转成一句莫名其妙
的空识别或者随便什么误判文本，走到对话分支。这个问题在隐私模式最先暴露（一
进隐私就转很大角度的舵机），但排查后发现所有会转舵机的动作（开心摇头、追踪
人脸、扫描找人、兴奋摆动）都有同样的风险。
- **两个 flag 配合实现"选择性"静音，而不是"舵机在动就静音"**：
  `g_currentMoveIsNoisy`（host 通过 `/servo?mute=1` 每次显式声明"这次移动
  有噪音"）AND `Motion::isMoving()`（固件用 `M5StackChan.Motion.isMoving()`
  查询舵机是否真的还在物理转动）两者同时为真才会把 `g_muteStreamForServo`
  置真，`streamTaskFn()` 里推流前发现这个 flag 为真就把整个 PCM chunk
  `memset` 成 0（麦克风本身仍正常读取，只是发出去的数据归零，不影响 I2S
  时序），转动结束后再加 `SERVO_MUTE_COOLDOWN_MS`（300ms）冷却期防止噪音
  余振。**为什么不能只用 `isMoving()` 不分青红皂白全静音**：最早这么做过，
  结果 `track_face_servo()` 那种对话过程中持续做的小幅度追踪微调（跟人说话
  同时发生）也被静音，表现为"说话无法完全被识别"——真人说话被这些高频小
  动作打了很多空洞。所以改成 host 端按调用场景显式声明"这次会不会吵"：
  反应性动画（开心摇头摆尾、隐私姿势、兴奋摆动、扫描找人）等大幅度/预期会
  响的移动传 `mute=1`；`track_face_servo()` 和 `play_nod_animation()`/
  `play_shake_animation()`（都发生在对话进行中，随时可能跟真实语音重叠）
  故意保持默认的不静音。`handleServo()` 每次调用都无条件重新赋值
  `g_currentMoveIsNoisy`（不是只在传参时才改），避免上一次"吵"的调用把
  flag 卡在 true 上影响下一次没传 mute 的调用。`/home` 端点固定传
  `mute=1`（复位动作从不会跟对话重叠）。
- **隐私模式额外多一层"确认真正静下来"的等待**：舵机停止转动不等于噪音立刻
  归零、VAD 的滑动窗口也需要时间把噪音判定"翻篇"。`transition()` 进
  PRIVACY 后调用 `_settle_privacy_mic()`：先轮询 `/status` 直到 yaw/pitch
  落在隐私姿势目标值 ±20 以内（实测约 1-2s），再等
  `STREAM_SILENCE_SECONDS + 0.3s`，最后调用 `mic_stream.take_utterance()`
  把这段时间里滚动缓冲区攒下的、可能包含噪音尾巴的"一段话"直接丢弃，避免
  刚进隐私就立刻又被拉回对话。

## 触摸事件：固件是权威真相，不要靠 host 轮询重建
早期 `check_touch()` 自己用一个时间戳（按下时记一次、松开时再算一次差值）
在 host 端重建"按了多久"，结果隐私模式长按 3 秒经常判定不到——根因是
`run_conversation_turn()`（STT+LLM+TTS 全程同步阻塞）经常一卡就是好几秒，
`tick()` 顾不上按时轮询触摸，等终于轮询到"松开"时，`self.state` 可能已经
被另一条路径（比如语音唤醒）带离了判断长按用的那个状态，长按 3 秒的检测就
静默失效了。同理，`TouchSensor.wasDoubleClicked()`（头顶双击）、屏幕点击
这类瞬时 flag 在 M5Unified 的 `Button_Class` 里只在判定成立的那一帧
（~10ms，对应 loop() 的 ~100Hz）为真，host 大约 1Hz 的轮询节奏直接问"现在
是不是刚发生"基本会错过。
- **时长类状态（按了多久）**：交给固件持续维护、host 只读结果。
  `Button_Class` 本来就在 `getUpdateMsec() - lastChange()` 里连续算着"当前
  这次连续按住了多久"，`/touch` 直接把它报成 `held_ms` 字段，host 不管什么
  时候被耽误、什么时候才有空轮询，读到的都是固件此刻的真实值，不会因为
  轮询延迟算错。
- **一次性/瞬时事件（点了几次）**：交给固件用单调递增计数器"攒"起来，host
  只比较数字变没变，不问"现在是不是true"。`loop()` 里 `wasDoubleClicked()`
  为真就把 `g_headDoubleTapCount++`，屏幕点击同理 `g_screenTapCount++`；
  host 端的 `check_touch()` 拿当前值跟上次记的值（`self.last_double_tap_
  count`/`self.last_screen_tap_count`，首次读取只做基准值 priming、不触发
  事件）比较，只要变了就说明期间发生过至少一次，不会因为轮询跟不上而漏掉，
  最多是把连续发生的好几次事件合并成一次响应。
- **以后任何"瞬时状态"或"持续时长"要暴露给慢速轮询的 host，都按这个模式
  设计**：瞬时 → 固件端单调计数器；时长 → 固件端连续维护、按需只读一个
  数值，不要在 host 端用采样时间戳重建，采样节奏一旦被别的同步阻塞打乱就
  会算错或漏判。

## 触摸触发映射（handle_touch_trigger()）
判断/处理逻辑集中在 `PuppyEngine.handle_touch_trigger(touch)` 一个方法里
——不止 `tick()` 调用它，讲话过程中（`wait_for_playback()`）也要用同一套
判断，两处不能各写一份，见下面"讲话时触摸立刻打断"一节。
- **碰屏幕 → "贴贴"（摸头反应）**：`enter_tietie()`，不设状态限制、
  也不依赖 `scan_for_face()`。之前这条复用的是"短按→扫描找人"那套逻辑，
  `scan_for_face()` 转头找不到人脸时会静默失败、既不报错也不进开心——这才
  是"碰屏幕没反应"的根因，不是触摸事件本身没测到。碰屏幕时用户显然就在
  设备正前方，不需要再靠摄像头确认一次。**"贴贴"**是这个反应动作的
  正式名字（原名"小开心"，纯改名——`enter_xiaokaixin()`/`play_xiaokaixin_
  animation()`/`XIAOKAIXIN_*` 系列常量同步改成 `enter_tietie()`/
  `play_tietie_animation()`/`TIETIE_*`，行为/舵机参数完全没变，`表情映射
  v11.xlsx` 也同步改了名字）：开心表情 + 轻微小幅度抬头 3 次（`play_tietie_animation()`,
  `TIETIE_PITCH_UP/DOWN`，抬头偏移量第一版 50、反馈"幅度可以调大一点"
  后加大到 120，仍然小于 `enter_happy()` 完整动画里 `HAPPY_PITCH` 的偏移量
  150，保持"比完整开心动作小"这个既定关系），跟"进入开心"状态本身的完整摇头动画
  （`play_happy_animation()`）是两个不同的动作，动作播完保持在开心表情/
  状态上；即使当前已经是 HAPPY 状态、摸一下屏幕也会再触发一次这个反应
  动画。**只有碰屏幕这一条路径触发"贴贴"**——听到呼唤"小狗小狗"（见下面
  "'小狗小狗'呼唤"一节）触发的是完整的"开心"（`enter_happy()`，跟被动
  检测到人脸是同一个方法），不是"贴贴"；这两个反应容易被搞混（一个是轻微
  抬头 3 次，一个是完整摇头动画），改的时候要认清是哪一个。
  **`play_tietie_animation()` 曾经踩过"舵机噪音防误触发语音"一节记录
  的固件 `handleServo()` 默认值 bug**：三次抬头循环里 `move_servo()` 只传了
  `pitch`，没传 `yaw`——固件省略参数不是保持当前角度，是重置成硬编码默认值
  （yaw 默认 0），等于每一下抬头都悄悄把 yaw 拉回正前方，把人脸追踪之前转到
  的角度抹掉。表现是"贴贴"一播完舵机就回正、不再朝着人，紧接着的手势扫描
  窗口（见"装死表情/手指枪手势检测"一节）经常因为镜头没对准举手势的人而拍
  不到。修法是播放前先读一次当前 yaw，动画里每次 `move_servo()` 都显式带上
  这个值钉住——跟"生气"序列转头动作是同一个套路。
- **头顶双击 → 兴奋**：不设状态限制。双击本身是瞬时手势，`check_touch()`
  里 press/release 边沿驱动的触摸反馈灯（下面"触摸反馈灯"一节）不一定能
  可靠地在两次快速点按之间抓到，所以 `handle_touch_trigger()` 在双击分支
  里额外显式点一次暖白灯确认"感应到了"，再进兴奋状态的彩虹快闪；如果已经
  在 EXCITED 状态（`transition()` 对"目标状态跟当前一样"是空操作，不会
  重播彩虹快闪），不加这个兜底的话双击会显得毫无反应，所以这里点完暖白灯
  以后还会强制调一次 `restore_state_led()` 确保最终落回彩虹快闪、不卡在
  反馈用的暖白上。
- **头顶长按满 `PRIVACY_HOLD_SEC`(3s) → 进/出隐私**：按当前是否已经在
  PRIVACY 状态决定方向（同一个 held_ms 判断，不是两套独立逻辑）。这条之前
  只写了"隐私状态下长按→退出"，压根没有"非隐私状态长按→进入"的分支，所以
  长按头顶从来没能触发过隐私——不是跟双击手势冲突（长按是持续按住 3 秒，
  双击是两次快速点按，`firmware.ino` 里 `DOUBLE_TAP_WINDOW=800ms` 以内，
  手势形态差异明显，不会互相误判），单纯是这条路径当初没写全。
  **同一次连续按住只应该触发一次**：`held_ms` 越过阈值触发"进入隐私"以后，
  只要手指还按着，`held_ms` 会继续增长、下一次轮询时状态已经是
  PRIVACY——如果不加限制，会被立刻当成"退出隐私"的条件再次命中，同一次
  长按里进出闪一下。用 `self.privacy_hold_fired` 这个 flag 挡住：越过阈值
  触发一次后置 True，`check_touch()` 检测到松开（`held_ms` 归零）才重置回
  False，保证一次连续按住只触发一次。
- **长按也可能被语音唤醒"偷走" tick()，看起来像长按失效**：即使上面两条
  都修好了，长按头顶进/出隐私还是可能不触发——`check_voice_wake()` 只要
  判定捕捉到一段语音（哪怕是环境噪音误触发、根本没人说话），就会同步跑完
  整个对话链路（STT+LLM+TTS+播放，好几秒起步），这几秒里 `tick()` 完全被
  占住，正在进行中的长按会因此错过后续轮询——等对话链路跑完才有机会再看
  一眼 `held_ms`，这时用户多半已经松手了，长按就白按了。修法是双管齐下：
  ①`tick()` 里只要 `self.touch_pressed` 为真（头顶正被按住）就跳过这一轮
  的语音唤醒检查，不让语音链路在触摸手势进行中把 tick() 抢走；②
  `STREAM_RMS_THRESHOLD` 从 450 提到 600，减少环境噪音本身触发"假语音"的
  频率（离真实说话的 ~778 还留了余量，不该明显影响拾音灵敏度）——这两条
  谁都不能替代对方：①防的是"即使没有假语音也不该被任何语音打断触摸"，②
  减少的是假语音触发的基础概率。**调这个阈值不要碰固件端的物理麦克风增益
  （`setup()` 里 `mic_cfg.magnification=5`）**——那是专门为了让小声说话也能
  被听清才调高的，物理增益负责"听得到多小声"，`STREAM_RMS_THRESHOLD` 负责
  "多大声才算是人在说话"，是两个独立的问题，混着调容易把已经验证过的东西
  又调回去。`MicStream._emit_utterance()` 现在会把每段捕捉到的语音的 RMS
  一起打印出来，环境噪音又触发误判时可以直接看日志里的数值，比凭感觉猜
  这个阈值该调到多少更准。
- **触摸响应"特别慢"：根因是主循环轮询节奏被人脸检测的耗时拖垮，不是触摸
  本身检测得慢**。`tick()` 原来用 `poll_slot = tick_count % 2` 把触摸和人脸
  检测轮流分到两轮，本意是避免同一轮里对 StackChan 连打好几个请求（教训见
  下面"其它注意事项"），但 `check_touch()` 自己已经有 `TOUCH_POLL_SEC` 节流，
  不需要再靠轮流限流——之前那么做等于平白把触摸检测频率砍掉一半，还搭上一个
  更大的坑：`run()` 主循环里不管 `tick()` 本身耗时多久，每轮都无脑
  `time.sleep(MAIN_LOOP_INTERVAL_SEC)`；人脸检测那一轮要打 `/camera`（下载
  一张 JPEG）再跑一次 mediapipe 推理，单次可能花大半秒到一两秒，这段耗时会
  直接叠加在那 0.5s 睡眠之上，使得下一次真正轮到检测触摸的时机被进一步推
  迟。现在改成两处：①`check_touch()` 从 `poll_slot` 轮流里摘出来，每个
  tick 都调用（靠自己的节流控制实际请求频率，不额外增加设备负载）；
  ②`run()` 记录 `tick()` 实际耗时，只睡够"凑满一个完整间隔"还欠的那部分
  （`max(0, MAIN_LOOP_INTERVAL_SEC - elapsed)`），一次慢 tick 不再连带拖慢
  下一轮。人脸检测本身仍然按 `poll_slot==1` 轮流+各自的 `FACE_CHECK_
  INTERVAL_SEC`/`FACE_RETRACK_INTERVAL_SEC` 内部节流，没有变得更频繁，请求
  总量没有增加，只是不再拖累触摸的响应速度。
  **修完上面这些结构性问题以后，剩下的延迟上限基本就是
  `MAIN_LOOP_INTERVAL_SEC + TOUCH_POLL_SEC` 这两个数字之和**（HTTP 往返
  本身只有 30-50ms，可以忽略），把这两个都从 0.5s 降到 0.2s，最坏情况延迟
  从 ~1s 压到 ~0.4s。敢往下调是因为 `/touch` 早就是 `handleTouch()` 那种
  零堆分配的静态缓冲区实现（`/volume` 那次教训是 malloc()+高频轮询的组合
  拳，不是单纯"轮询快"本身的问题），5Hz 左右不会重蹈覆辙。以后如果这个
  延迟还嫌大，先确认是不是这两个数字还能再降，而不是又去找别的"结构性"
  原因——目前已知的结构性瓶颈（轮流轮询、慢 tick 拖累下一轮）都已经解决，
  剩下的就是纯粹的轮询间隔取舍。

  **上面这套修复留了一个没堵上的口子，后来被实测暴露**（用户反馈：碰屏幕
  →贴贴之间延迟依然很明显，实测 ~2s）：`check_touch()` 从轮流里摘出来了
  没错，但 `check_face()`/`retrack_face()` 本身在 IDLE/HAPPY/SLEEPY/SORRY
  状态下仍然是**同步阻塞调用** `detect_face_once()`（`/camera` 下载+
  mediapipe 推理，单次可达 1~2s）——`tick()` 里触摸检查虽然排在人脸检查
  前面、优先处理，但这防不住"用户碰屏幕的那一刻，恰好上一步的人脸检测正卡
  在阻塞调用里"：这次触摸要等到当前这个（慢）tick() 整个跑完、下一个
  tick() 重新开始才会被看到，等待时长最坏情况就是这次人脸检测实际耗时，
  跟 ~2s 的反馈对得上——这是"轮询间隔"（上面已经修好）和"单次调用本身
  阻塞"（这里没修）两个独立的问题，前者决定"多久轮到检查一次"，后者决定
  "检查这一次要卡多久"，只修前者防不住后者。
  修法：`check_face()`/`retrack_face()` 改成只负责判断"这个 tick 要不要
  发起一次检测"，真正的拍照+推理丢给后台线程（`_check_face_worker()`/
  `_retrack_face_worker()`，`threading.Thread(daemon=True)`）异步做，
  `tick()` 因此几乎不会再被人脸检测卡住。检测结果通过
  `face_detected`/`face_confirm_count` 这些实例属性异步写回，跟原来效果
  一致，只是从"这个 tick 内看到最新结果"变成"下一两个 tick 才看到"——
  检测间隔本来就有 `FACE_CHECK_INTERVAL_SEC`(3s)/`FACE_RETRACK_
  INTERVAL_SEC`(5s) 这么长，晚一两个 tick（≤1s）不影响实际行为。新增
  `face_detect_lock`（`threading.Lock()`）保护 `self.face_detector.detect()`
  本身，防止后台 worker 跟 `scan_for_face()`/`track_face_once()`/
  `_face_person_before_excited()` 这些仍然同步阻塞调用同一个 mediapipe
  实例的路径并发执行——跟 `_asr_lock` 保护 SenseVoice 并发调用是同一个
  理由；`_face_worker_busy` 防止上一次后台检测还没跑完就叠加起下一次。
  **以后再遇到"轮询间隔已经调到最小，响应还是偶尔很慢"，先怀疑是不是又是
  这种"间隔够小，但单次调用本身太慢"的情况，不要只在间隔数字上继续抠**。

  **这次改完后台线程，实机测试当场就暴露了一个新坑**：碰屏幕触发"贴贴"
  时，`play_tietie_animation()` 连续发了好几个 `/servo` 请求，其中两个
  直接 `ConnectTimeoutError`（连 TCP 连接都建立不起来，不是响应慢）——发生
  的时机正好是后台的人脸检测 worker 还在等 `/camera` 的响应。根因：
  `check_face()`/`retrack_face()` 改成后台线程之后，host 端第一次出现了
  "两个线程同时各自发一个请求给设备"的情况；ESP32 的 `WebServer` 单线程，
  能不能撑住第二条并发连接取决于它的 TCP 接受队列（backlog）能撑多大，
  实测撑不住——第二条连接直接连不上，不是排队变慢。修法：给 `api_get()`
  （所有设备请求，不管是 `/servo`/`/face`/`/touch`/`/camera` 还是别的，
  全都从这一个函数发出去）内部实际发请求那一行加一个模块级
  `_device_lock`（`threading.Lock()`），让所有线程的请求真正排队串行——
  这跟设备本来的单线程处理能力是匹配的，不会比"一切都在主线程里天然串行"
  的旧模型更慢，只是把新增的后台线程也纳入这个队列，不让它绕开。**以后
  再给 host 端加任何新的后台线程（不管是不是碰摄像头），只要这个线程会
  调用 `api_get()`/`move_servo()`/`get_status()` 等任何一个发请求给设备的
  函数，都不需要额外操心并发问题——`_device_lock` 已经在最底层统一兜住了，
  不需要每个新线程自己重新发明一套串行化机制。**

  **这把锁本身又在下一轮反馈里暴露了一个新的权衡问题**：用户反馈"碰屏幕→
  贴贴""双击→兴奋"这些触摸反应有时"还是好久"，报的数字（~5s）跟
  `api_get()` 的默认单次超时 `TIMEOUT`(5s) 高度吻合——排查下来：后台人脸
  检测 worker 的 `/camera` 请求如果恰好卡住（本机之前就出现过一次真实的
  `ConnectTimeoutError`，WiFi/网络不是绝对稳定），会攥着 `_device_lock`
  攥到超时才放手，期间主线程哪怕只是想发一个 `/face?expr=happy`，也要陪
  着等——最坏情况下（超时+重试）能拖到 ~12s。**这是"设备只能一次处理一个
  请求"这个物理限制的必然代价，不管 host 端怎么排队都绕不开，唯一能做的
  是缩短"背景任务卡住时占用锁的时长"**。后台人脸检测本来就是"错过一次也
  无所谓"的低优先级任务（下次 `FACE_CHECK_INTERVAL_SEC`/`FACE_RETRACK_
  INTERVAL_SEC` 后还会再试），不需要跟触摸反应这种要"手感"的操作用同一套
  超时/重试策略。`api_get()`/`capture_frame()`/`capture_frame_with_bytes()`/
  `detect_face_once()` 都加了透传的 `timeout`/`_retry` 参数（默认值不变，
  不影响 `scan_for_face()`/`track_face_once()`/`_face_person_before_
  excited()` 这些仍然阻塞等待、需要保留原超时+重试的路径），后台的
  `_check_face_worker()`/`_retrack_face_worker()` 改用新增的
  `FACE_BG_TIMEOUT_SEC`(1.5s) 且不重试——把"背景检测偶尔卡顿拖累前台触摸
  反应"这个最坏情况从 ~5~12s 压到 ~1.5s。**以后再给任何"低优先级、错过一次
  无所谓"的后台任务加设备请求，都应该用短超时+不重试，不要用给"阻塞等待、
  必须等到结果"的路径设计的默认 TIMEOUT——两类调用对"等多久"的容忍度完全
  不一样，不能用同一套参数。**

  另外评估过一个"固件本地即时反馈"的候选方案（碰到触摸时固件在
  `loop()` 里本地立刻闪灯，不经过 host）：这个思路能绕开"设备一次只能
  处理一个请求"这个瓶颈本身（本地反应完全不走网络），但如果做成本地直接
  切表情/播动画，固件不知道 host 端状态机的排除条件（比如隐私/装死状态下
  碰屏幕本该被忽略），容易出现"设备自己先反应了，host 后面又说其实不该
  反应"的状态不一致——如果以后确实要做，应该把范围收紧到纯视觉、不带状态
  语义的反馈（比如只闪一下灯，不改表情），真正的表情/动作决策依然完全留在
  host 端。这次先只做了上面的超时收紧（纯 host 端改动，风险更低），固件
  本地反馈作为如果还不够跟手时的后续选项，先没有动。
- **隐私状态退出被收紧成"只认头顶触摸"**：语音唤醒不再能退出隐私
  （`check_voice_wake()` 在 PRIVACY 状态下直接丢弃这段语音、返回 False）；
  碰屏幕在 PRIVACY 状态下也故意不生效（`handle_touch_trigger()` 里加了
  `and self.state != State.PRIVACY`）。只剩长按（进/出对称）和头顶双击
  （不分状态的全局触发，隐私状态下自然也生效）两条路径。**踩过一个坑**：
  最初想给隐私专门做一个"头顶单击"退出手势（M5Unified `Button_Class` 的
  `wasSingleClicked()`），实测这个判定很难可靠触发，已经改回复用双击，
  相关的 `g_headSingleTapCount`/`single_tap_count` 也已经从固件和 host
  端删掉——以后不要再想着用 `wasSingleClicked()` 做这类手势。从隐私状态
  双击进兴奋时，`enter_excited_from_touch()` 会先
  `_face_person_before_excited()` 转回正对人脸再开始兴奋动画——隐私姿势
  本身刻意把头转开，不这样处理动作会从背对人的角度开始。长按退出隐私时
  新增了反向 LED（`fade_in` 模式，固件里 `FADE_OUT` 的镜像实现，从熄灭
  渐亮回基色）。

## 讲话时触摸立刻打断
"小狗讲话时摸一下就立刻打断"这个需求，卡在一个硬限制上：ESP32 的
`WebServer` 单线程，`/play` 原来的实现是阻塞到整段 WAV 播完才返回
`handleClient()`——播放期间（qa_complex 逐个念关键词，每个词可能占用
好几秒）设备完全不会处理任何其它 HTTP 请求，包括 `/touch`。host 端就算
拿到触摸信号也没用：想发一个"stop"请求过去，这个请求要排队等当前这次
`/play` 的 handler 自己跑完才轮得到处理，那时候都已经播完了，"打断"永远
迟到。唯一的解法是让 `/play` 也变成非阻塞的，跟 `/stream` 一样的架构。
- **固件端**：`handlePlay()` 现在只做参数校验、把下载+解析+分块播放这一整
  套逻辑丢给后台 FreeRTOS 任务 `playTaskFn()`（`xTaskCreatePinnedToCore`，
  跟 `streamTaskFn()` 同一个模式），立刻返回。`g_playShouldStop`
  这个原子 flag 由 `handlePlay()` 的 `?stop=1` 分支置位，`playTaskFn()`
  的下载循环和"等上一块播完"的等待循环里都频繁检查它，一旦发现就调
  `M5.Speaker.stop()` 立刻硬切断——不是"播完手头这一小段再停"，是真正
  意义上的立刻打断。`g_i2sMutex`/`g_streamPauseForOtherAudio` 这套麦克风/
  喇叭互斥协议原本就是为"多个线程可能同时想用 I2S 外设"设计的（见
  `streamTaskFn()` 声明处的注释），`playTaskFn()` 只是又多了一个使用方，
  机制不用改。`loop()` 里原来兜底调用的 `updateLipSync()` 现在改成
  `if (!g_playTaskRunning) updateLipSync();`——播放任务运行期间它自己会在
  循环里调这个函数，主线程不能再重复调一遍，否则两个线程同时读写
  `g_lipSyncActive`/`g_lipData` 这些全局状态、还都在调
  `avatar.setMouthOpenRatio()`，是一个数据竞争。`/status` 新增 `playing`
  字段，host 端靠轮询它判断"是不是还在播"，不能再假设"HTTP 响应回来=
  播完了"。**这次顺带把 `handleStatus()` 也从 `String` 拼接改成了静态
  缓冲区 + `snprintf`**（跟 `handleTouch()`/`handleVolume()` 一样的零堆
  分配模式）——host 现在等语音播完要按几百毫秒的间隔反复轮询这个接口，
  虽然单次播放持续时间不长，但累积起来已经够得上 CLAUDE.md 里 `/volume`
  那次教训描述的"持续高频调用"量级，不提前修的话迟早会在这里重演一次同样
  的堆碎片化问题。
- **host 端**：`play_wav_file()` 拆成了 `start_play()`（发 `/play?url=`，
  只负责启动，几乎立刻返回）和 `stop_play()`（发 `/play?stop=1`）。
  `PuppyEngine.wait_for_playback()` 轮询 `/status` 的 `playing` 字段等
  自然播完，轮询间隙顺带 `check_touch()`；一旦 `handle_touch_trigger()`
  判定这次触摸是个真手势（不是空按），就返回 `False` 告诉调用方"没播完，
  是被打断的"。`speak_keywords()` 逐个关键词播放那里改成
  `if start_play(...) and not self.wait_for_playback(): ...中止剩下的
  关键词...`——被打断时不再继续念下一个词，也不再调
  `restore_state_led()`（`handle_touch_trigger()` 已经把 LED 设成新状态
  该有的样子，这里再设一次反而可能把它盖掉）。
- **`handle_touch_trigger()` 不管当前是不是正在讲话，处理任何一个手势前都
  无条件先发一次 `stop_play()`**：`/play?stop=1` 在没有播放任务时只是个
  空操作，没有副作用，不需要先判断"是不是真的在放"，这样 `tick()` 里的
  正常触摸处理和 `wait_for_playback()` 里的打断处理可以走同一份代码，不用
  为"当前是不是在播放"这件事另外分支。
- 目前只有 `speak_keywords()`（qa_complex 逐个念关键词）会真正播放音频，
  是全系统唯一需要"讲话时被打断"这件事的地方；`play_nod_animation()`/
  `play_shake_animation()`（qa_simple 的点头/摇头）不播放音频，不涉及这
  一套。

## "小狗小狗"呼唤
`is_calling_puppy(text)`（`puppy_engine_v4.py`）判断 STT 识别结果是不是在
叫名字（"小狗小狗"这种呼唤语），不是在问一个提到了"小狗"这个词的问题。
先剥掉标点/空格只留中文字符（SenseVoice 识别结果可能带"小狗小狗！""小狗，
小狗～"这类变体，直接做字符串相等判断会漏掉大部分实际说法），再要求剩下
的内容很短（`<=8` 个字，避免"小狗你说小狗喜不喜欢吃肉"这种长句子里恰好
出现两次"小狗"被误判）且至少出现两次"小狗"。
- **不走 LLM 语义分类**：`_run_conversation_turn_body()` 里这个判断插在
  STT 识别结果出来之后、送进 LLM 之前——直接拦截，不再走 `ask_llm()`。这是
  刻意的：SYSTEM_PROMPT 的四路意图分类（qa_simple/qa_complex/praise/scold）
  对"小狗小狗"这种没有实际问题内容的招呼语没有天然归属，让 LLM 去猜大概率
  会猜成 qa_complex（开放式问题）念一串不相干的关键词，而不是我们想要的
  反应；直接拦截还省了一次网络往返。
  触发后直接调 `self.enter_happy()` 然后 `return`——**注意是完整的"开心"
  （播完整摇头动画），不是"贴贴"**，这两个反应容易搞混，别改错。不会
  像 qa_simple/qa_complex 那样调 `_settle_happy(track_ok)`，道理是用户显然
  就在附近正对着它说话，不需要摄像头再确认一次人脸位置。

## 晕(dizzy)表情 / 摇晃检测
新表情，走的是 CLAUDE.md"设计全新表情"一节说的标准流程：先在
`firmware/expr_preview/expr_preview.ino` 里反复迭代视觉效果（这一版跑了五轮
反馈：漩涡大小/间距、双眼不同速转出的错位感、圈数、有没有舌头、鼻子嘴巴
靠拢眼睛多少，每一轮都在 `PuppyFace.h` 顶部对应的常量块留了改动记录），
确认满意后再接入 `handleFace()` 和 host 端。视觉设计本身：漩涡眼（从中心向外
的螺旋折线，双眼同一转速持续自转，右眼比左眼领先 45° 相位，不会转到完全
同步的画面）+ 张嘴（不吐舌）+ 双耳同步朝同一方向轻晃（复用 `earSwingOffset()`
本来就给左右耳返回同一个偏移量这个既有机制，不需要额外镜像逻辑）。所有
调整过的常量都在 `PuppyFace.h` 里 `DIZZY_*` 开头，改动历史直接写在常量块
上方的注释里。

触发条件是 CoreS3 板载的 BMI270 九轴传感器检测到"被摇晃或拿起"：
- **固件端只算连续状态，不直接触发任何反应**：`firmware.ino` 里原有的
  "摇一摇"逻辑是攒够 3 次冲击就直接在 `loop()` 里自己调
  `avatar.setExpression(Doubt)`、闪灯、转舵机（`notifyCallback("shake",...)`
  还会阻塞发一个 TCP 请求出去，这条路径此前就没被任何东西监听，是死代码）
  ——这套一次性反应会跟 host 端通过 `/face` 控制的表情打架，已经整个删掉。
  改成固件只负责算一个连续布尔量 `g_shaking`：`loop()` 里每 50ms 采一次
  加速度模长，跟上一次采样比较，变化超过 `SHAKE_THRESHOLD` 就刷新"最近一次
  冲击"的时间戳，`g_shaking = (millis() - 最近一次冲击) < SHAKE_HOLD_MS`
  （600ms，给摇晃动作本身的节奏间隙留缓冲，不然会在一次连续摇晃期间反复
  true/false 跳变）。`/status` 新增 `imu`（`M5.Imu.isEnabled()`，确认
  BMI270 初始化成功）和 `shaking` 两个字段，host 端只读，不自己用原始加速度
  数据判断——跟"触摸事件：固件是权威真相"是同一个模式。
- **host 端 `check_shaking()` 轮询 `/status`**，`tick()` 里优先级仅次于
  触摸（在语音唤醒之前）：不管当前是 tick() 能观察到的哪个状态（常态/
  开心/兴奋/困倦/隐私/抱歉）都可以被打断进入 `State.DIZZY`，包括隐私——
  物理摇晃/拿起设备是很明确的"人在互动"信号，比隐私状态下偶尔混进来的
  杂散摄像头帧可信得多。`is_shaking` 这个 tick 只查一次、存下来，进入分支
  和"晕"状态内部判断"摇晃是否已经结束"共用同一次结果，不重复请求。
- **收尾顺序按要求做成 晕 → 兴奋 → 开心+人脸追踪**：`State.DIZZY` 分支里
  `is_shaking` 变 false 不是立刻切走，而是先记下这一刻的时间戳
  （`self.dizzy_shake_stopped_at`），在"晕"表情上继续停留 `DIZZY_LINGER_
  SEC`(2.0s，第一版1.0s、反馈加长到2.0s) 才 `transition(State.EXCITED)`——
  这是第二版反馈加的，第一版信号一消失就立刻切，被要求改成"晃完还在晕
  一下"的缓冲；信号中途又恢复（摇一下停一下）会把计时器清掉重新等，不会
  让已经走了一半的停留时间继续算数。"兴奋"结束后走的是 `tick()` 里
  `EXCITED` 分支已有的逻辑
  （`state_duration()` 超过 `EXCITED_DURATION_SEC` 就按人脸是否还在场决定
  `enter_happy()` 还是回常态），不需要另外写。
- **`play_dizzy_animation()` 不移动舵机**：这个状态触发的前提就是设备正在
  被外力摇晃/托举，这时候舵机再主动转头只会跟外力对抗、徒增机械负担，
  视觉上也会被摇晃动作本身淹没，没有实际意义——跟"舵机噪音防误触发语音"
  一节里"反应性动画大幅度移动"的默认思路刻意不一样，是这个状态专属的
  例外。LED 用淡紫色呼吸灯（`DIZZY_LED_RGB`），跟其它状态的配色区分开，
  没有表情映射表可参考，是第一版自己定的，没有实机验证过是否合适。
- **`DIZZY_LED_RGB`/`DIZZY_LED_BREATHE_PERIOD_MS`/`SHAKE_THRESHOLD`/
  `SHAKE_HOLD_MS` 都没有实机验证过**，尤其是摇晃阈值/保持时长这两个——
  跟表情映射表里其它新功能一样，先给了一版能跑的默认值，等实机测过真实的
  摇晃/拿起手感以后再按反馈调整。

## 装死(dead)表情 / 手指枪手势检测
新的交互链路：碰屏幕触发"贴贴"后，小狗进入 `GESTURE_WINDOW_SEC`(15s) 的
"手势扫描窗口"，期间通过 `/camera` 加速轮询检测"手指枪"（finger gun）手势，
检测到就播放"装死"（XX 眼 + 吐舌头）并停留在这个状态，直到用户双击头顶
唤醒回"兴奋"。走的是 CLAUDE.md"设计全新表情"一节的标准流程：先在
`expr_preview.ino` 里迭代视觉效果，满意后再接入固件和 host 端状态机。

- **视觉设计**（`PuppyFace.h`，`DEAD_` 前缀常量）：XX 眼（两条交叉短线，
  不眨眼）+ 吐舌头（复用 `excited` 的舌头画法，`DEAD_TONGUE_SCALE=0.7`）。
  入场动画分两段：T+0~500ms 整张脸绕鼻子锚点顺时针转 `DEAD_ROTATION_DEG`
  (30°)——跟"好奇"（`Expression::Doubt`）共用同一套整体旋转机制
  （`DOUBT_PIVOT_X/Y` 锚点、`rotateLocalOffset`/`applyRotationAroundPivot`），
  但角度固定顺时针、不随 `doubtMirrorSign()` 镜像；T+500~800ms
  （`DEAD_EAR_DROOP_DELAY_MS`）两只耳朵各自绕自己耳根下垂
  `DEAD_EAR_DROOP_DEG`(30°)，按 `dir` 镜像角度让两耳对称往外垂，用独立的
  `deadEarDroopAnim_`（跟"好奇"长耳那个 `earTwistAnim_` 分开，过渡时长不同，
  300ms 而不是默认 500ms）。经过几轮反馈调整：眼睛累计缩小到初版的 72%
  （14→12.6→10.08）并朝鼻子方向靠拢（`DEAD_EYE_INWARD_PX`，复用兴奋表情
  "两眼互相靠拢"的机制）；两侧耳朵朝中轴线靠拢（`DEAD_EAR_INWARD_PX`，同一
  机制复用，从 10px 加大到 16px）；鼻子从"跟常态一样大"改成缩小 20%
  （`DEAD_NOSE_SCALE`）并朝眼睛方向靠拢（`DEAD_NOSE_CLOSER_PX`，复用
  兴奋/委屈/晕表情"鼻子朝眼睛靠拢"的同一套机制）；左耳下垂完成后不再
  定格，改成绕耳根（跟下垂用同一个铰链点）叠加一个 ±5°
  （`DEAD_EAR_WOBBLE_DEG`）的正弦来回轻微旋转（**不是**整只耳朵水平
  平移——最初做成平移，反馈明确要求改成绕耳根旋转），只有左耳有这个效果、
  右耳保持定格。
- **`handleFace()` 注册**：收到 `expr=dead` 时设 `Expression::Neutral` +
  `g_customExpr="dead"`，跟 `thinking`/`excited`/`privacy`/`grieved`/
  `peekaboo`/`dizzy` 同一个模式，没有新增其它固件逻辑（LED/舵机全部由
  host 端现有 HTTP API 驱动）。
- **`State.DEAD` 状态机**（`puppy_engine_v4.py`）：
  - `enter_dead()` 同步阻塞播完整套收尾动作（跟 `enter_excited_from_
    touch()` 是同一个模式）：切表情 + LED 开始闪红灯 → 舵机先抬头一下
    （`DEAD_PITCH_UP=500`，复用 `EXCITED_PITCH_HIGH` 同一个已验证幅度，
    视觉上像"中枪一震"）→ 舵机落到低头定格（`DEAD_PITCH_DOWN=80`，这个
    值已经过实机端到端验证）→ 闪完 `DEAD_LED_BLINK_HOLD_SEC` 后 LED 渐灭
    → 最后才 `transition(State.DEAD)`——`transition()` 的 if/elif 链里
    没有给 `DEAD` 加分支，落到这个状态时什么都不做，避免动画重复播放。
    舵机全程 `mute=True`（大幅度移动会有噪音）。"先抬后落"这个两段式动作
    是反馈加的，`DEAD_PITCH_UP` 没有单独做过实机验证，只是复用了已知安全
    的幅度数值。

    **第一版"抬头"动作实测完全看不出来**（反馈"没有抬头的舵机运动"）：
    第一版是发完 `move_servo(pitch=DEAD_PITCH_UP,...)` 后盲等一个固定的
    `DEAD_PITCH_UP_HOLD_SEC`(0.3s) 就紧接着发下一条 `move_servo(pitch=
    DEAD_PITCH_DOWN,...)`——`Motion.move()` 是非阻塞的，只是设定新的目标
    角度和速度，真正的物理转动由固件后台任务异步完成；如果 0.3s 内舵机
    还没转到 `DEAD_PITCH_UP` 附近，第二条指令会立刻把目标角度覆盖成
    `DEAD_PITCH_DOWN`，物理上只会看到舵机拐了个弯直接往下走，"抬头"这一
    截会被截断到几乎不可见。改成跟 `_settle_privacy_mic()`/`_face_
    person_before_excited()` 同一个套路：先轮询 `/status` 确认 pitch 真的
    到了 `DEAD_PITCH_UP` 附近（容差 `DEAD_PITCH_UP_SETTLE_TOLERANCE`=30，
    超时 `DEAD_PITCH_UP_SETTLE_TIMEOUT_SEC`=1.0s 就放弃等待、按当前角度
    继续，不会卡死），到位以后才开始数 `DEAD_PITCH_UP_HOLD_SEC` 的停留
    时间，最后再发落下指令。**以后任何"先移动到 A、停留一下、再移动到
    B"的两段式舵机动作，都要照这个模式轮询确认到位，不能对着一个非阻塞
    的 `move_servo()` 盲等一个猜的固定时长——猜的时长比实际转动时间短，
    效果就会跟这次一样被截断到看不出来。**
  - **装死状态下人脸/语音/摇晃检测全部跳过**，触摸只认头顶双击：
    `handle_touch_trigger()` 在判断 `is_screen_trigger`/长按分支的条件里
    都排除了 `State.DEAD`（碰屏幕、长按头顶被直接忽略），双击本来就是
    不分状态的全局触发，不需要加分支，落进现成的双击处理逻辑，自然调用
    `enter_excited_from_touch()`。`check_voice_wake()` 内部也把 `DEAD`
    并进了 `PRIVACY` 那个"忽略语音但仍要调用以排空队列"的分支——**不能
    在 `tick()` 顶层直接跳过 `check_voice_wake()` 的调用**，否则装死期间
    说的话会一直堆在 `MicStream` 队列里排不空，状态切走以后突然冒出一段
    "旧"语音被当成刚说的话处理，跟隐私状态是同一个坑。触摸的按下/松开
    反馈灯（`check_touch()` 的 press/release 边沿）**仍然保留**——即使
    "死了"，碰头顶时亮一下灯确认"设备感应到了"依然合理，`restore_
    state_led()` 因此也补了一个 `DEAD` 分支（效果是熄灯，虽然跟兜底的
    `else` 分支一样，但显式写出来避免"靠 else 隐式生效"）。
  - **从装死恢复：`_face_person_before_excited()` 试过给装死也走一遍，
    已经撤销**——最初以为装死"摄像头没轮询、face_detected 过时"跟隐私是
    同一类问题，让 `enter_excited_from_touch()` 的判断条件从"只有
    `PRIVACY`"扩展成"`PRIVACY` 或 `DEAD`"都先调
    `_face_person_before_excited()`（`go_home()`+等转到位+拍照确认人脸，
    最多 3 秒）。实测反馈两个问题：①双击后要等好几秒兴奋动画才真正
    开始；②等待期间表情会闪一下"常态"再变成兴奋。装死其实不需要这一步
    ——隐私需要是因为姿势里 yaw 转开了一大截（背对着人），装死只是
    pitch 低头看地，没有背对人，直接进兴奋动画本身的摇摆很快就会把
    角度带正，不需要专门等 3 秒重新对准。已经把 `DEAD` 从这个判断条件
    里去掉，只保留 `PRIVACY`——两个实测问题一起解决了，没有再深究"常态"
    那次闪现具体是哪一步导致的，反正装死走这条路径本来就没必要。
  - **上面这次修复之后，用户又反馈"双击→兴奋"之间还是会先闪一下——
    这次描述成"变成开心"而不是"常态"，才找到真正的根因**：
    `handle_touch_trigger()` 的双击分支之前不管当前是什么状态，一律先
    `set_led_mode("solid", *WARM_WHITE_RGB)`（点一次暖白灯确认"感应到了
    双击"）再调 `enter_excited_from_touch()`。这个暖白灯**正好是"开心"
    状态的招牌配色**（跟 `enter_happy()`/`restore_state_led()` 里 HAPPY
    分支用的是同一个 `WARM_WHITE_RGB`）——从装死双击进兴奋时，这一下
    预先点亮的暖白灯，跟紧随其后（`play_excited_animation()` 内
    `set_expression("excited")`+`set_led_mode("rainbow",...)`，一次 HTTP
    往返、~100ms 级）的真正状态切换撞在一起，视觉上第一眼会读成"先开心
    了一下、再兴奋"，不是真的有过渡动画，也不是触摸传感器延迟（用户当时
    提出的两个猜测都不对）——是这次多余的确认闪光本身制造出的错觉。这一下
    暖白灯原本是为"双击时已经在 EXCITED 状态、`transition()` 是空操作、
    不会有任何其它可见变化"这个边界情况准备的（不加的话双击会显得毫无
    反应）；**只有这一种情况才需要它**——只要目标状态会真正发生切换（从
    装死/隐私/其它任何状态双击进兴奋），转场本身已经足够快、足够明显，
    不需要再叠加一次confirmation 闪光。改成 `if self.state ==
    State.EXCITED:` 才点这次暖白灯，其它情况直接调
    `enter_excited_from_touch()`，让转场只发生一次、不再有暖白→彩虹的
    两段式视觉。**这类"用某个状态的招牌配色去做别的用途的临时提示"，
    以后都要留意会不会跟那个状态本身的常驻效果撞色，尤其是紧跟着一次
    真正的状态切换发生时——LED 系统一节的暖白/彩虹/绿色呼吸各自代表一个
    状态，混用容易在转场瞬间制造出"看起来切错了状态"的错觉。**
- **手势扫描窗口**（`enter_tietie()` 末尾设置
  `self.gesture_scan_until = time.time() + GESTURE_WINDOW_SEC`，配一个
  柔和暖白呼吸灯提示"小狗在等你比手势"）：`tick()` 里窗口开着的时候
  **不做人脸检测**（`do_face_check` 额外加了 `and not gesture_window_
  active`），改成每个 tick 都调 `check_gesture()`（自己按
  `GESTURE_POLL_SEC=0.8s` 节流，调用方不用关心频率）。**不做人脸检测的
  理由不是省 ESP32 负载**（MediaPipe 推理在电脑端跑，ESP32 端拍照传图
  不管给谁用开销都一样），**而是人脸检测会触发 `track_face_servo()`
  移动舵机，画面跟着偏移会让手势检测的取景不稳定**。窗口关闭有两条路径，
  处理不一样：检测成功由 `check_gesture()` 自己把 `gesture_scan_until`
  置 0 主动关闭（LED 交给 `enter_dead()` 自己的红灯动画接管，不能再调
  `restore_state_led()` 覆盖掉）；自然过期则由 `tick()` 里
  `gesture_window_active` 变 False 但 `gesture_scan_until != 0.0` 这个
  分支处理（清空 `finger_gun_count`、`restore_state_led()`、把
  `gesture_scan_until` 归零标记"已处理"，避免每个 tick 重复调）。
- **`check_gesture()` 手势判定**：用 MediaPipe 的 **Hand Landmarker**
  （不是 Gesture Recognizer——"手指枪"不在预训练的 7 种手势里：
  Closed_Fist/Open_Palm/Pointing_Up/Thumb_Down/Thumb_Up/Victory/
  ILoveYou），跟 `self.face_detector` 一样在 `__init__()` 里创建好、
  之后复用，不每次检测都重新建。判定逻辑在模块级函数
  `classify_finger_gun_pose(lm)` 里（`host/gesture_test.py` 诊断脚本
  和生产代码共用这一份实现，不要各自维护）。要求连续
  `FINGER_GUN_CONFIRM_FRAMES`(2) 帧都命中才真正触发，用
  `self.finger_gun_count` 计数，没命中就归零；窗口自然过期时如果这个
  计数器还没归零（凑够确认帧数之前窗口就到期了），`tick()` 里也会显式
  清零，避免残留到下一次开窗口时被当成"本来就有"的确认帧数。
  - **第一版用绝对坐标差判定，实测被证明不够稳健，已经改成距离比例**：
    最初的写法直接比较 landmark 的 x/y 坐标差（比如"指尖 y 小于 PIP
    关节 y"判断手指伸直），第一次实机测试连续 5 帧稳定命中，但紧接着
    第二次用同一个诊断脚本、同一段代码重测，全程一次都没命中——失败
    模式集中在"中指已经弯了，但无名指/小指判定成没弯"。根因是绝对坐标
    差对手离摄像头的距离、手在画面里的旋转角度都很敏感，同一个物理
    手势换个距离/角度，坐标差的数值会飘出阈值范围，不是使用者没摆稳
    姿势，是判定方法本身设计有缺陷。
  - **现在用手腕(landmark 0)作参考点算距离比例**：手掌尺度参考
    `hand_scale` = 手腕到中指根部(landmark 9)的距离；每根手指的"伸展
    比例" = 指尖到手腕的距离 / 对应 PIP 关节到手腕的距离，只依赖点与点
    的相对距离，手在画面里怎么平移/旋转都不影响。食指：伸展比例 >
    `FINGER_EXTEND_RATIO`(1.2) 判定伸直。中指/无名指/小指：伸展比例 <
    `FINGER_CURL_RATIO`(0.9) 判定弯曲，**三根里至少两根弯曲就算数，不
    要求三根全弯**——无名指、小指天生比中指更难独立弯曲（肌腱互相
    牵连），实测数据也证实很多人自然摆出的手指枪这两根手指弯曲程度
    不如中指，要求三根全弯会把真实的手指枪判定成"不是"。拇指：拇指
    指尖(4)到食指根部(5)的距离换算成相对 `hand_scale` 的比例，大于
    `FINGER_GUN_THUMB_SPREAD_RATIO`(0.6) 判定张开。改完之后重测，
    出现了多段连续 2~4 帧命中，`FINGER_GUN_CONFIRM_FRAMES`(2) 稳定
    够用。这几个比例阈值是根据数据估的，不是精确校准过的，以后如果
    又出现"识别不稳定"的反馈，先怀疑是不是又是这类距离/角度敏感的
    绝对量判断，不要想当然地只调数值。
  - **`landmark_projection_calculator.cc` 会打一条警告**："Using
    NORM_RECT without IMAGE_DIMENSIONS is only supported for the
    square ROI"——StackChan 摄像头画面不是正方形，理论上可能轻微影响
    手部关键点定位精度；改成距离比例之后识别已经足够稳定，这条警告
    目前没有必要再深究，除非以后识别效果又变差了再回来查。
- **`hand_landmarker.task` 模型文件不提交进 git**：从 Google 官方 CDN
  下载（`curl -o host/hand_landmarker.task "https://storage.googleapis.
  com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/
  hand_landmarker.task"`，约 7.8MB），放在 `host/` 目录——这跟
  `self.face_detector` 用的 `FACE_MODEL_PATH`（`C:/tmp/blaze_face_
  short_range.tflite`，同样在仓库之外）是同一个惯例：MediaPipe 模型
  文件是本地依赖，不是仓库内容，重新拉取仓库/换机器需要重新下载这个
  文件才能跑手势检测（人脸检测同理）。
- **端到端验证过了**：碰屏幕 → 贴贴 → 手势扫描窗口（暖白呼吸灯）→
  举手指枪 → 装死（表情/舵机低头/红灯闪烁再渐灭）→ 头顶双击 → 兴奋，
  完整链路在真机上跑通过，包括装死状态下触摸手势的隔离（碰屏幕/长按
  被忽略）。`DEAD_PITCH_DOWN`/`DEAD_EAR_WOBBLE_DEG` 等表情相关参数
  实际观感是否需要微调、`FINGER_EXTEND_RATIO` 等手势阈值在更多不同的
  手型/光线条件下是否依然稳定，还可以继续观察调整，但核心链路已经
  确认可用，不再是原型验证阶段。

## "再见"手势（挥手 / 五指捏住再放开）检测
跟"手指枪"（上一节）共用同一个手势扫描窗口（碰屏幕"贴贴"之后的
`GESTURE_WINDOW_SEC` 窗口期）、同一次 `/camera` 拍照 + Hand Landmarker
检测结果——`check_gesture()` 一次检测后两种手势的判定都跑一遍，不重复
拍照/推理，谁先确认就处理谁，不会同一帧里两个都触发。识别到之后按要求
先"委屈"过渡一下、再转入"隐私"：`enter_goodbye()` 播 `play_grieved_
reaction()`（捉迷藏没找到目标那套现成的动作参数：微低头 + 暖白闪烁，只是
表情换成 `grieved`），停留 `GOODBYE_GRIEVED_HOLD_SEC`(1.5s) 让这个过渡
表情被看清，再 `transition(State.PRIVACY)`——那条路径本来就会播
`play_privacy_animation()`（转隐私姿势）+ `_settle_privacy_mic()`（等
舵机转到位、丢弃转动噪音可能误触发的语音），跟平常进隐私是同一条路径，
没有另外重写一遍。

判定逻辑是模块级函数 `classify_open_pinch_pose()`（`puppy_engine_v4.py`,
跟 `host/gesture_test.py` 诊断脚本共用同一份实现，不要各自维护，见上面
"手指枪"一节记过的同一条教训），同样用**距离比例**而不是绝对坐标差
（原因见 `classify_finger_gun_pose()` 顶部的详细说明：绝对坐标差对手离
摄像头的距离/画面里的旋转角度太敏感），同样没有实机验证过——先给一版
能跑的默认值，等实机测过挥手/捏放的真实手感再调。

- **两种触发方式，判定思路不一样**：
  - **挥手**：`classify_open_pinch_pose()` 判定"张开手掌"（食指/中指/
    无名指/小指四指的伸展比例都超过 `FINGER_SPREAD_OPEN_RATIO`，比手指枪
    只要求食指一根更严格），`check_gesture()` 只在张开手掌的帧才把手掌
    水平位置（`palm_x`，用 landmark 9 中指根部而不是指尖，指尖在挥手时
    摆动幅度更大、更容易被单帧噪声带偏）计入 `self.wave_x_history`
    （按 `WAVE_HISTORY_SEC` 滚动裁剪）。`_count_wave_swings()` 是经典的
    "折线摆动计数"算法：从上一个极值点开始，水平位移超过振幅阈值（相对
    当时手掌尺度 `hand_scale` 的比例 `WAVE_MIN_AMPLITUDE_RATIO`，不是
    固定像素值，手离摄像头远近不同不需要重新调参数）才算移动到新的极值
    点，方向跟上一段相反才计一次摆动，连续反向摆动够 `WAVE_MIN_SWINGS`
    (2) 次才触发。只在"张开"的帧累积样本，是为了避免"五指捏住再放开"
    手势本身的移动被误算成挥手的一次摆动。
  - **五指捏住再放开**：`classify_open_pinch_pose()` 另外算一个"指尖
    散开比例"——五个指尖（拇指4/食指8/中指12/无名指16/小指20）到它们
    质心的平均距离，相对 `hand_scale` 的比例；小于 `PINCH_TIP_SPREAD_
    RATIO`(0.35) 算"捏拢"，大于 `RELEASE_TIP_SPREAD_RATIO`(0.55) 算
    "放开"——**两个阈值故意留出间隔，不用同一个阈值来回判**，不然手指
    停在临界值附近会来回抖动着重复触发（跟游戏里`GAME_HIST_THRESHOLD`
    这类"避免临界值抖动"的教训是同一个思路）。`check_gesture()` 用
    `self.goodbye_pinch_since` 记"什么时候看到的捏拢"：非 0 表示"已经
    捏拢、正在等放开"，在 `PINCH_RELEASE_TIMEOUT_SEC`(3s) 内看到"放开"
    才算一次完整手势；超时还没放开，这次捏拢作废，要重新捏一次才算数,
    不会无限期地等一个很久以前的捏拢。
- **窗口关闭时两类手势的残留状态要一起清干净**：手指枪的连续帧计数
  （`self.finger_gun_count`）之前已经在窗口自然过期时被清零，这次把
  挥手历史（`self.wave_x_history`）和"已捏拢等放开"标记
  （`self.goodbye_pinch_since`）也一起收进了新增的 `_reset_gesture_
  scan_state()`，`check_gesture()` 检测成功主动关闭窗口、以及 `tick()`
  里窗口自然过期这两条路径都改成调这一个方法，不再各自零散清理——道理
  跟手指枪那次一样：不清干净会被下一次开窗口时的检测当成"本来就有"的
  残留数据。
- **用 Hand Landmarker 而不是 Gesture Recognizer**：虽然 Gesture
  Recognizer 原生支持 `Open_Palm`（张开手掌），但那只是静态姿势，没有
  "挥手"（时序上的来回摆动）这个手势本身，"五指捏住再放开"也不在预训练
  的 7 种手势里；用同一个 Hand Landmarker 模型统一判定手指枪和"再见"两类
  手势更省事，也不用额外加载第二个模型、多占一份内存/推理开销。
- **`FINGER_SPREAD_OPEN_RATIO`/`PINCH_TIP_SPREAD_RATIO`/`RELEASE_TIP_
  SPREAD_RATIO`/`WAVE_HISTORY_SEC`/`WAVE_MIN_SWINGS`/`WAVE_MIN_
  AMPLITUDE_RATIO`/`PINCH_RELEASE_TIMEOUT_SEC`/`GOODBYE_GRIEVED_HOLD_
  SEC` 全部没有实机验证过**，尤其是挥手判定——`GESTURE_POLL_SEC`(0.8s)
  一帧，`WAVE_HISTORY_SEC`(6s) 窗口内大约只能采到 7~8 个样本，够不够
  稳定捕捉到 2 次反向摆动需要实机测过挥手的真实节奏才知道；`host/
  gesture_test.py` 诊断脚本已经同步加了"再见"判定的逐帧打印（张开/
  捏拢/放开三个布尔量 + 指尖散开比例的实际数值），可以对着真实手势现场
  看数值调阈值，跟手指枪那次同一个流程。这个功能目前只做到"逻辑实现
  完成、单元测试验证过核心判定和摆动计数算法本身正确"，还没有过实机
  端到端验证，不能当成已经确认可用。
- 这个功能已经补进了 `表情映射v11.xlsx`（更新了"手势扫描窗口"行的触发/
  打断条件，新增"再见（挥手/五指捏放）"一行），删掉了 v9，不再是只在
  `CLAUDE.md` 里记录、表格脱节的状态。

## 开机自动找人
`PuppyEngine.run()` 开头以前是 `play_idle_animation()`（回正、常态表情、
关灯），纯被动等第一次 IDLE 状态下的 `check_face()` 才会看到人（最多要等
`FACE_CHECK_INTERVAL_SEC` 才轮到）。现在改成开机直接 `scan_for_face
(pitch=HAPPY_PITCH)` 主动抬头扫描——`pitch` 传的是"开心"状态用的抬头角度
（`HAPPY_PITCH=300`，回正是 450），不是 `scan_for_face()` 默认的平视角度
450，让开机这次扫描的姿态本身就带着"抬头找人"的效果。扫到人直接
`enter_happy()`（完整摇头动画 + 进入持续追踪）；没扫到就跟原来一样落回
`play_idle_animation()`。`scan_for_face()` 因此多了一个 `pitch` 形参（默认
值还是 450，其它调用方不用改）。

## 关机动画（已撤销，退出脚本仍保持表情显示）
`run()` 主循环收到 `KeyboardInterrupt`（Ctrl+C 退出程序）时，一直是直接调
`play_idle_animation()`（回正、切中性表情、关灯）——脚本退出后屏幕继续
显示中性表情，不会变黑。中间短暂试过一版"关机动画"（`play_shutdown_
animation()`：闭眼→停留→调 `/display?off=1` 关闭屏幕背光），但用户反馈
希望"像之前一样，退出脚本以后也依然显示小狗的表情"，已经把这条路径整个
撤销（`play_shutdown_animation()`、`SHUTDOWN_EYES_CLOSE_DELAY_SEC` 都已从
`puppy_engine_v4.py` 删除），退出处理恢复成单纯调用 `play_idle_animation()`。
`/display`（`handleDisplay()`，`firmware.ino`）这个固件接口本身没有撤销
（`sleep()`/`wakeup()` 包装，零风险的独立能力），只是现在没有任何 host 端
代码路径会主动调 `off=1` 了；`run()` 开机第一步仍然调一次
`set_display(on=True)`，作为"万一有人手动 curl 过 `/display?off=1`"的
兜底，不依赖"上一轮是怎么退出的"这个已经不存在的前提。

**曾经尝试过让物理电源键长按也触发类似的收尾动画（纯固件实现），也已经
撤销——留个教训记录**：CoreS3 的电源管理芯片是 AXP2101，`setup()` 里被
M5Unified 配置成"长按 1 秒触发芯片内部的长按标记（IRQ 状态位）/ 长按满 4
秒芯片自己硬件断电"（寄存器初始化数组旁的原厂注释：`0x27, 0x00 //
PowerKey Hold=1sec / PowerOff=4sec`）。当时的想法是"蹭"这个 1 秒信号：
`loop()` 轮询 `M5.BtnPWR.wasHold()`（`M5.update()` 内部把芯片那个 IRQ 状态位
翻译成标准 `Button_Class` 接口，读一次自动清零，理论上应该是边沿触发），
检测到就调一个纯固件版的 `playShutdownAnimation()`（闭眼、灯灭、关屏幕），
在真正断电前的 ~3 秒窗口里抢先播完收尾动画，不依赖电脑上的 host 脚本。

**实测反馈：屏幕会在没有长按电源键、设备也没有真正断电的情况下自己黑屏，
且时机不可预测**（用户反馈"没有测试脚本连接时会自行黑屏，按电源键关机
再开机后表情正常，但过一阵子又会再次黑屏"）——`Display().sleep()` 只关
背光/让面板休眠，ESP32、WiFi、HTTP server 全部继续正常运行，跟"实际上
没有关机"这个观察完全吻合，几乎可以确定是 `M5.BtnPWR.wasHold()` 在没有
真实长按的情况下被误判为真，具体是芯片内部哪个环节误触发的没有条件深入
排查（需要示波器，或者至少能一边盯着串口日志一边确认电源键完全没被碰过，
这次没有这样的调试条件），已经把这条自动触发路径整个删掉，`playShutdown
Animation()` 这个固件函数也一并删掉（删掉触发源以后已经没有任何地方在
调用它）。**教训：给物理按键的芯片级中断状态位接一个有副作用的动作之前，
应该先纯打印、不接真正反应地跑至少几个小时，确认这个信号本身在无人触碰
时始终保持稳定的 false，而不是直接接上"关屏幕"这种有副作用的动作就上
线**——这次第一次真正暴露问题已经是在用户的日常使用中。

**物理电源键的"按两下才能重新点亮屏幕"现象，跟这份固件代码完全无关**：
`firmware.ino` 里现在没有任何代码读取 `M5.BtnPWR`（上面这条路径已撤销），
按一下电源键如果按住够久（寄存器配置的 `PowerOff=4sec` 阈值），会触发
芯片真正切断电源（ESP32 完全失电，属于纯硬件行为）；断电状态下再按一下
会重新上电、走一次完整冷启动，`setup()` 跑完屏幕自然重新显示内容——表现
上就是"按两下"，第一下是真关机、第二下是真开机，不是软件层面的显示睡眠/
唤醒，也没有可以在固件里修的地方。

## 捉迷藏找物品游戏
`puppy_engine_v4.py` 里的一个新游戏模式：听到"我们玩捉迷藏吧"先开心点头
回应 → 按钮播报"小狗 看"（看的时候LED绿灯，拍照记住物品，VLM 识别结果也
按关键词念出来，比如"橘子"）→ 点两下头、按钮说"小狗 闭眼" → 低头闭眼按钮报数
"5 4 3 2 1"（报数本身就是留给人藏东西的时间）→ 转头扫描房间找。语音触发
（LLM 意图分类新增 `game_hide_seek` 一条，`SYSTEM_PROMPT` 规则第 5 条），
触发后调用 `PuppyEngine.play_game_hide_seek()`。
- **游戏里所有播报都走跟 `speak_keywords()` 同一套"按钮按一下+关键词 TTS
  同步播放"的 AAC 风格**（`_game_speak_keywords()`），不用整句 TTS 旁白
  ——这版是从"游戏旁白可以整句话说"改过来的：既然对话回应必须靠关键词表达
  是这只小狗"表达自己的唯一方式"，游戏里的播报没道理破例，所以"小狗 看"、
  识别到的物品/位置、倒计时数字、"小狗 闭眼"，全部统一成关键词+按钮动画。
  `_game_speak_keywords()` 不能直接复用 `speak_keywords()`——那个内部调用
  `self.wait_for_playback()`，会走 `handle_touch_trigger()` 的完整手势分发
  （碰屏幕→贴贴等），游戏进行中触发这些会跟游戏状态冲突，所以另外写了一份
  只响应长按中止信号的版本。
- **整个游戏是一次同步阻塞的交互，从 `_run_conversation_turn_body()` 的
  `game_hide_seek` 分支进来**，架构上跟对话链路本身占住 `tick()` 好几秒是
  同一种模式，不是新引入的阻塞写法。`self.state` 全程停在新增的
  `State.GAME_HIDE_SEEK`，不经过 `transition()` 的常规状态分发表（那张表
  是给"进入即播放一次性动画"的状态设计的）——直接改 `self.state`，游戏内部
  每个子阶段自己精细控制表情/LED/舵机；找到/没找到复用标准的 `State.EXCITED`/
  `State.SORRY`（走 `transition()`，播放它们各自现成的动画），游戏彻底结束
  时复用 `_settle_happy(False)`（强制走"重新扫描找人脸"分支——游戏期间头
  转了一圈，追踪肯定已经丢了，这跟对话结束时"追踪丢失过，重新扫描"是同一
  个收尾逻辑）恢复到常态并保持人脸追踪，不是简单丢回 IDLE。
- **游戏进行中只接受长按头顶这一个"中止游戏"信号**，不接受碰屏幕/双击这些
  正常状态下的触摸手势（那些语义在游戏语境下会冲突，比如摸屏幕平时是
  "贴贴"，游戏里用户很可能只是想确认"已经藏好了"）——所以不走
  `self.check_touch()`/`handle_touch_trigger()` 那一整套手势分发，改成
  `_game_check_abort()` 直接读原始 `/touch` 的 `held_ms`。
- **找物品靠两层判断**：先用 OpenCV 的 HSV 颜色直方图相关度
  （`extract_color_hist()`/`compare_hist()`，阈值 `GAME_HIST_THRESHOLD`）做
  便宜的粗筛，每个扫描步都能算一次；粗筛命中后如果有物品的文字描述，再调
  一次视觉大模型精确确认（`call_vision_llm()`，通义千问 Qwen-VL，走阿里云
  DashScope 的 OpenAI 兼容端点）。同一个 `call_vision_llm()` 还用来把"这是
  什么"（注册阶段）和"大概在哪"（找到之后）压缩成 AAC 风格的关键词——直接
  让 Qwen-VL 一次调用完成"看图+给出关键词风格文字"，不需要再链式调用
  DeepSeek 做二次压缩（Qwen-VL 本身就有文本生成能力，prompt 里直接要求它
  输出短词）。**VLM 是可选增强，不是硬依赖**——API key 从 `.env` 的
  `DASHSCOPE_API_KEY` 读取（`load_env_key()`，跟 `DEEPSEEK_API_KEY` 共用
  同一套解析逻辑），没配置这个 key、或者调用失败/超时/key 本身无效，
  `call_vision_llm()` 统一返回 `None`，三处调用点（识别物品、精确确认、
  识别位置）都设计成能各自优雅降级，不会因为 VLM 不可用就整个玩不了。
- **调试 Qwen-VL key 未生效的教训**：`DASHSCOPE_API_KEY` 变量名、`.env` 格式
  （无多余空格/引号/换行）都正确，直接 curl 测过 DashScope 的
  `compatible-mode/v1/chat/completions` 端点，返回的是
  `401 invalid_api_key`——网络能通、端点/模型名没写错，是这把 key 本身
  没有通过阿里云那边的校验（不是网络代理拦截，本机代理只拦截到 StackChan
  设备的局域网请求，不影响这类外网 HTTPS 调用）。以后遇到"VLM 好像没生效"，
  先怀疑 key 是否真的是对应服务商签发的、有没有被这个服务商的 API 接受，
  不要先怀疑代码里的调用逻辑——`call_vision_llm()` 对所有失败都是静默降级
  （只打日志，不抛异常），表现上跟"没配置 key"一模一样，容易被误判成"代码
  没接上"。
- `GAME_HIST_THRESHOLD`（初始 0.35）和扫描相关的 `GAME_SCAN_*` 系列参数都
  没有实机验证过，需要拿真实物品测过再调——误报多就调高阈值，漏检多就调低；
  `GAME_SCAN_YAW_MIN/MAX` 用的是 `±800`，跟 `EXCITED_YAW_RANGE`/
  `PRIVACY_YAW` 同一量级的已验证安全幅度。
- 倒计时的"闭眼"用专门设计的 `set_expression("peekaboo")`（`_game_countdown_
  phase()` 一开始就切换，一直保持到倒计时的关键词念完；`_game_scan_phase()`
  一开始会切成 `curious`，所以自然在扫描开始时结束）。这个表情最初是在早期
  版本里借用 `set_expression("privacy")` 的闭眼视觉效果实现的（`privacy` 当
  时是唯一真正闭眼的表情，`sleepy` 只是眯眼）；后来单独设计并反复调整出了
  专属的 `peekaboo` 表情（见"表情系统"一节），已经把这里换成用它，不再需要
  借用隐私表情。
- **没找到目标时用"委屈"（`grieved`）表情反应，不是"抱歉"（`sorry`）**：
  `_game_on_timeout()` 调用 `play_grieved_reaction()`——动作和灯效（微低头 +
  暖白闪烁）完全复用 `play_sorry_animation()` 那一套参数，只是表情换成
  `grieved`。故意不走 `self.transition(State.SORRY)`：`State.SORRY` 在别处
  （"责备"语音意图，见 `_run_conversation_turn_body()` 的 `scold` 分支）仍然
  要保留"抱歉"（`sad`）表情，两个场景视觉上应该不一样，所以没有改共享的
  `State.SORRY`，而是给游戏单独写了一个不经过状态机的反应函数，`self.state`
  全程留在 `GAME_HIDE_SEEK`。播完委屈反应后按钮再说一句"没有"
  （`_game_speak_keywords(["没有"])`），跟游戏里其它播报同一套按钮+关键词
  TTS 机制，说完才收尾。
- **没找到的收尾故意不用 `_game_settle_after_result()`，改用专门的
  `_game_settle_after_timeout()`**：前者在没确认到人脸时会调
  `self.transition(State.IDLE)`，会顺带播 `play_idle_animation()`——把表情
  切回中性、舵机归位，这样"委屈"表情播完立刻就被盖掉了，用户明确要求委屈
  表情要一直保持到真的重新看到人脸为止。`_game_settle_after_timeout()`
  不调 `transition()`，直接把 `self.state` 设成 `IDLE`（`state_enter_time`
  跟着更新）交给 `tick()` 里已有的 `State.IDLE` 分支被动接管——那个分支
  每隔一个 tick 就拍照检查一次人脸，检测到就自动走 `enter_happy()` 切
  过去，跳过 `transition(State.IDLE)` 只是不再顺带播放归位动画，人脸检测/
  追踪这条路径本身没有绕开。`_game_settle_after_result()` 现在只给
  `_game_on_found()`（找到）用，两者的收尾方式因为这个改动第一次不一样，
  改的时候别搞混。
- **固定词汇 TTS 预热**（`_prewarm_game_tts()`/`_game_tts()`）：`GAME_FIXED_
  PHRASES`（"小狗""看""闭眼""没有"+倒计时数字）内容从来不变，`PuppyEngine.__init__()`
  用后台线程提前合成好并缓存路径，`_game_speak_keywords()` 优先用缓存，
  跳过一次网络 TTS 往返（几百毫秒到一两秒）。这是为了解决"从听到邀请到
  说出'小狗 看'中间等太久"的延迟反馈加的——**以后任何"每次触发都要念的
  固定文案"，都应该按这个模式预热缓存，而不是现合成**，这不是捉迷藏游戏
  专属的技巧。缓存未命中（预热还没跑完，或者是 LLM 生成的动态关键词，
  比如识别到的物品/位置）会自动退化成现合成，不影响正确性。
- **注册阶段可以用语音打断重来**（`_game_listen_for_rejection()`）：念完
  识别到的物品关键词后，开一个 `GAME_REJECTION_WINDOW_SEC`(3s) 窗口期
  轮询 `MicStream`，说"不是这个""看错了"之类的话（`is_registration_
  rejection()`，关键词匹配，不经过 LLM）会让 `_game_register_phase()` 回到
  循环开头重新"看"一次。窗口开始前会先丢弃一次可能残留在队列里的旧语音段
  （"小狗 看"播放期间麦克风被静音，恢复推流后可能积攒一小段噪音），避免
  误判成否定指令。
- **扫描路径改成两层俯仰的"之"字形**（`_game_build_scan_plan()`），不再是
  固定单一水平面来回扫——原来的实现只在一个 pitch 上扫，会导致不在那个
  水平带里的区域（比如桌面以下、右下角）永远拍不到。`GAME_SCAN_PITCH_
  LEVELS = [300, 500]` 复用项目里已经验证过安全的既有数值（`HAPPY_PITCH`/
  `EXCITED_PITCH_HIGH`），**但具体哪个数值对应"抬头"哪个对应"低头"没有
  实机验证过**（这份表情映射表里 pitch 数值和文字方向描述在别处也有过对不
  上的先例，参照"人脸追踪的 yaw 方向符号不确定时不要靠猜"那条教训，这里
  同样没法在没有硬件的情况下确认）——如果实机测出来方向反了，直接把这个
  列表顺序倒过来就行。每层的起点/终点各自随机抖动一点（`GAME_SCAN_YAW_
  JITTER`），层与层之间插一个 yaw 回正的过渡点（不拍照）；转动速度随扫描
  进度从 `GAME_SCAN_SPEED_MIN`(150) 线性升到 `GAME_SCAN_SPEED_MAX`(450)，
  后者仍然低于项目里用过的最快值（`EXCITED_YAW_SPEED`=500）。
- 这个功能已经补进了 `表情映射v11.xlsx`（"捉迷藏-看物品/倒计时/扫描搜索/
  找到/没找到"五个"游戏"类型的行，外加 `grieved`/`peekaboo` 两个"表情"类型
  的行），不再是只在 `CLAUDE.md` 里记录、表格脱节的状态。
- **好奇（Doubt）表情跟随舵机 yaw 镜像歪头方向**（`doubtMirrorSign()`，
  `PuppyFace.h`）：扫描时舵机左右摆动，五官整体旋转方向、以及"长耳/垂耳"
  角色分配都会跟着 yaw 符号镜像，不再固定歪向一侧。驱动它的
  `g_currentYaw`（`firmware.ino`，每帧从 `Motion.getCurrentAngles()`
  刷新）**必须声明成 `volatile int`**——踩过一次坑：一开始漏加
  `volatile`，写在 `loop()` 的主任务、读在 `avatar.init()` 起的独立渲染
  任务，编译器合法地把读取缓存在寄存器里，表现出来就是镜像方向"卡死不
  变"。这个项目里 `g_buttonState`/`g_subNLines` 早就因为同样的跨任务
  共享场景被标成过 `volatile`，以后再加类似"主任务写、渲染任务读"的
  共享状态，直接照这个模式来，不要再漏。
- **找到/没找到之后不能复用 `_settle_happy()` 收尾**：普通对话结束时
  `_settle_happy(track_ok)` 在追踪丢失时会调 `scan_for_face()`——切
  "好奇"表情、转头扫描 `SCAN_POSITIONS` 好几个位置，这套动作接在
  `EXCITED`/`SORRY` 刚播完的表情后面，会打断用户正在看的兴奋/抱歉反馈
  （实测反馈：'找到/没找到之后的表情反馈会被打断'）。改成专门的
  `_game_settle_after_result()`：只拍一帧轻量确认人脸在不在（不切表情、
  不转头扫描），在的话直接 `enter_happy()`（`session_active` 通常已经是
  `True`，只是安静切换，不会又播一次动画），不在也不强行扫，直接回常态
  交给后续 `tick()` 的被动检测接管。以后任何"游戏/动画结束后要不要重新
  找人脸"的收尾场景，如果紧跟在一段本身就该被看到、不该被打断的表情
  后面，都应该考虑这种轻量确认，而不是无脑复用 `_settle_happy()`。

## 定时提醒 + 生气催促
`puppy_engine_v4.py` 里的一个行为：小狗在指定时间主动提醒主人（喝水/吃饭/
出去玩），提醒发出后如果 10 分钟内主人一直没离开，小狗会"生气"催促，直到
被原谅。只在 host 端实现——固件早就有 `angry`/`eat`/`play` 这三个自定义
表情（见 `PuppyFace.h` 头部注释）和 LED/舵机/触摸/摄像头这些全部需要的
能力，不用改固件。

- **两种调度方式并存**：`REMINDERS`（吃饭）是固定时间点，`eat_lunch`
  11:30、`eat_dinner` 17:30，到点前后 `REMINDER_WINDOW_SEC`（2分钟）窗口
  内算命中；`DYNAMIC_REMINDER_TEMPLATES`（喝水/出去玩）不进这张固定时间
  表，而是从当天第一次成功发出的吃饭提醒开始（`self._dynamic_next_time`
  被 `_deliver_reminder()` 设成非 None），按 `DYNAMIC_INTERVAL_MIN/
  MAX_SEC`（1~1.5小时）随机间隔连续触发，每次触发时两者随机二选一，直到
  `DYNAMIC_ACTIVE_HOUR_END`（21点）才停；超出 `DYNAMIC_ACTIVE_HOUR_
  START/END`（8:00~21:00）活跃时段时不会攒着一开门就响一次凑数，而是
  重新从时段起点算一次随机间隔。这两套调度共用同一个互斥：`self._
  reminder_recheck_target` 非 None（有一条提醒正在 10 分钟复查中）时，
  `_check_reminders()`/`_check_dynamic_reminder()` 都会跳过，避免三条
  提醒的复查窗口互相打断。活跃时段范围是没有实机验证过的默认猜测，具体
  几点合适要看实际作息反馈调整。

- **`_deliver_reminder(reminder)` 是三条提醒共用的发出逻辑**：先
  `scan_for_face()` 确认人在场，没找到直接放弃、不进复查（催促一个不在
  场的人没有意义）。两条分支：
  - `want_play`（想出去玩）：关键词优先用 `get_weather_keywords()`（和
    风天气 QWeather 现挑，失败退回固定兜底 `["出去","走","动"]`）→ 好友
    名字（边边/大黄/耶耶）后面强制补一个"玩"字（`enforce_friend_needs_
    play()`）→ 摆头（`play_reminder_swing_animation()`，参数参考"小
    开心"：振幅120，3次往复，速度300）→ 念关键词 → 切"玩(play)"表情并
    保持。
  - `want_drink`/`want_eat`（喝水/吃饭，两者走同一套时序）：先"委屈"
    过渡（同款摆头 + `grieved` 表情 + LED 呼吸灯，表达"小狗还在惦记这件
    事"）→ 切"吃饭(eat)"表情 + LED 闪烁 + 念关键词 → 说完保持"吃饭"
    表情。
  两条分支说完都**不调 `enter_happy()`**——那个会直接把表情设回 happy
  盖掉刚设的 play/eat，所以手动复制它的静默切换记账（`session_active=
  True`/`state=State.HAPPY`/`state_enter_time`），表情留在 play/eat 上，
  一直保持到 10 分钟复查触发 `_play_angry_reminder()`（会自己切到
  angry）或者主人提前离开。

- **关键词库设计**：喝水必含"水"，候选池（喝/杯杯/咕嘟）按当前气温追加
  （`get_drink_keywords()`，≥30℃追加渴/冰/凉凉，≤5℃追加暖/热乎，来自
  同一个 `_fetch_weather_now()` 天气查询），不含时间段词（用户明确要求
  删掉）；吃饭必含"饭饭"，候选池（时间/肚肚/空/肉肉/零食/香香）+可选
  时间段词（`get_eat_keywords()`）。三条提醒的关键词最终都会过一遍
  `apply_cute_substitutions()`（AAC 叠词替换："饭"/"吃饭"→"饭饭"，"饿"
  →"肚肚"+"空"）。`pick_time_of_day_word()` 按当前小时挑一个候选词
  （早上5-10点"morning"、10-18点"亮亮"、18-24点/0-5点"暗暗"），只是放
  进随机抽样池子里的"候选"，不保证一定会说出来（跟气温词"冷"/"暖"同一
  个地位）。每次挑词数量在 `WEATHER_KEYWORD_COUNT_MIN/MAX`（2~4个）之间
  随机，不固定为3个。

- **QWeather（和风天气）接入**：免费版账号是"API Host"子域名架构
  （`https://{host}/v7/weather/now`），不是共享的 `devapi.qweather.com`
  （那个域名会返回 `403 Invalid Host`）；免费版实测没有 GeoAPI（城市名
  查 LocationID）权限，所以不查城市名，杭州的 LocationID 直接写死
  （`WEATHER_LOCATION_ID = "101210101"`，本身不会变，没必要每次先查一
  遍）。图标代码按范围粗分类：100/150=晴、101-103/151-153=多云、
  104/154=阴、300-318=雨、400-499=雪、500+=雾霾沙尘等其它
  （`_classify_weather_icon()`）。`WEATHER_SIGNATURE_WORD` 给部分分类
  （晴"阳光"、雨"雨"、雪"冷"）保证一个"代表词"提前占好一个位置再随机
  填剩下的、最后整体洗牌——不这样做的话 `random.sample()` 会把天气状况
  词和气温词当成同等概率的候选，实测出现过大雨天小狗说"玩/暖/外面"完全
  没提雨的情况。`get_drink_keywords()` 的气温词沿用同一个
  `_fetch_weather_now()` 辅助函数，不重复写一遍请求/解析逻辑。天气 API
  key 从 `.env` 的 `QWEATHER_API_KEY`/`QWEATHER_API_HOST` 读取，没配置
  或调用失败统一返回 None，调用方（`get_weather_keywords()`）退化成
  固定兜底文本，不影响可用性——跟捉迷藏游戏里 Qwen-VL 的"可选增强，不是
  硬依赖"是同一个架构模式。

- **生气（`_play_angry_reminder()`）序列**：切"生气(angry)"表情 + LED
  闪红灯3次（`ANGRY_LED_BLINK_COUNT`，400ms周期）再常亮 → 静止拍照确认
  正脸（轮询 `detect_face_once()`，**不用** `scan_for_face()`——那个会
  转头扫描，会跟接下来"左转45°"这个动作混在一起、也会临时把表情切成
  "curious"）→ 找到后 `track_face_servo()` 对齐一次、停 1 秒
  （`ANGRY_FACE_FOUND_DELAY_SEC`）→ yaw 在当前角度基础上 `+
  ANGRY_YAW_TURN`（200，约45°，"+"=向左转，这是本项目里少数几个已经用
  大幅度实测确认过符号的方向）转动，**显式带上当前 pitch 一起传**——
  固件 `handleServo()` 有个 bug：省略的参数不是保持当前角度，而是重置
  成硬编码默认值（`pitch` 默认450、`yaw` 默认0），只传 yaw 会把 pitch
  意外拉回450，这个 bug 目前只在生气转头这里显式绕开了（读当前 pitch
  再原样传回去），项目里其它单独传 yaw 的调用（比如
  `track_face_servo()`）理论上同样受影响，只是还没有具体症状触发去修。
  转动本身是非阻塞的（`move_servo()`），之后轮询 `/status` 确认真的转
  到位（容差 `ANGRY_YAW_SETTLE_TOLERANCE`=30，超时
  `ANGRY_YAW_SETTLE_TIMEOUT_SEC`=3.0s 放弃等待、按当前角度继续）——
  这个轮询是必须的，不能对着非阻塞调用盲等一个固定时长就去监听双击，
  不然双击可能在转动真正播完之前就把它打断，视觉上转动几乎看不出来
  （跟"装死"抬头动作踩过的坑是同一类问题）。原谅只认**头顶双击**（一次）
  或**头顶长按**（阈值跟 `PRIVACY_HOLD_SEC` 一样，双击判定不到位时的
  兜底）：`_angry_forgive()` 回正（`go_home()`）、保持3秒生气、
  `mic_stream.take_utterance()` 把生气期间队列里堆积的语音直接丢弃
  （避免"生气"结束后突然冒出一段旧语音被当成刚说的话处理），最后
  `enter_happy()`。**整个序列是一次同步阻塞方法**（跟
  `play_game_hide_seek()` 是同一种架构），执行期间 `tick()` 完全停摆，
  触摸判断直接读 `get_touch()` 原始值（`_angry_double_tap_check()`），
  不走 `self.check_touch()`/`handle_touch_trigger()` 那一整套——那套
  是给正常 `tick()` 循环设计的，会顺带触发跟生气无关的副作用（碰屏幕→
  贴贴、双击→兴奋的全局手势分发等）。生气期间不播语音（不调
  `speak_keywords()`），所以 `handle_touch_trigger()` 不需要加
  `State.ANGRY` 分支——唯一可能在生气期间被调用的路径是
  `wait_for_playback()` 内部的轮询，而生气序列完全不经过那条路径。
  以后如果给生气加语音播报，要重新检查这一条是否还成立。

- **`reminder_test_driver.py`**（不进 git，跟 `voice_test.py` 等测试
  脚本同一惯例）可以跳过时间匹配/10分钟等待，直接手动触发某一条提醒或
  生气序列，方便测试：
  ```
  python host/reminder_test_driver.py deliver eat_lunch
  python host/reminder_test_driver.py deliver drink_water
  python host/reminder_test_driver.py angry
  ```
  `deliver [label]` 先查 `REMINDERS`（固定时间表），查不到再查
  `DYNAMIC_REMINDER_TEMPLATES`（动态调度模板），两边都直接喂给
  `_deliver_reminder()`，绕开真正的调度逻辑，只测"发出这一条提醒"本身
  的效果。

## 编译 / 烧录 / 验证流程
本机的 `arduino-cli` 不在 PATH 里，可执行文件在
`C:\Users\89823\arduino-cli\arduino-cli.exe`：
```
"C:\Users\89823\arduino-cli\arduino-cli.exe" compile --fqbn m5stack:esp32:m5stack_cores3 firmware
"C:\Users\89823\arduino-cli\arduino-cli.exe" upload --fqbn m5stack:esp32:m5stack_cores3 --port COM3 firmware
```
本机有一个本地沙盒 HTTP 代理会拦截到设备的请求，验证时需要绕过它（`curl` 加
`--noproxy '*'`，PowerShell/bash 里也可以先清空 `http_proxy`/`https_proxy`
等环境变量），然后用 `/face?expr=<表情>` 切换表情、轮询 `/status` 的
`uptime_s` 字段确认设备没有重启（`uptime_s` 应该持续增长，如果掉回很小的值
说明触发了重启）。每次改完 `firmware.ino`（不只是 `PuppyFace.h`——LED/舵机/
音频这些非表情相关的固件改动也一样）都应该：编译 → 烧录 → 用 `/status`
验证目标行为持续一段时间不重启 → 顺手回归测试几个其它表情/接口确认没有
连带影响。

## 其它注意事项
- **`ensure_audio_server()`（`puppy_engine_v4.py`）给 StackChan 下载 TTS 音频
  用的本地 HTTP 服务器必须用 `http.server.ThreadingHTTPServer`，不能用普通
  `http.server.HTTPServer`**——后者单线程同步处理请求，且 accept 到的连接
  没有设置任何 socket 超时；只要有一次连接卡住（设备端 WiFi 抖动、开了 TCP
  连接但没有及时发出请求、下载中途被打断没干净关闭……），唯一的服务线程
  会永久阻塞在等这一个连接的数据上，之后所有音频下载请求都进不来、也不会
  自己恢复——表现出来是"能正常说上几轮话，之后小狗的声音彻底消失，不会
  自愈"，且设备本身没有崩溃（`/status` 照常能查询到，只是新音频下不下来）。
  已经改成 `ThreadingHTTPServer`（每个连接单开一个线程，一个卡住的连接不
  会拖累其它请求），用一个模拟"连接了但不发数据"的卡住连接验证过修复有效
  （旧代码下这个测试会直接把新请求也一起卡死，改完以后新请求 0.2s 内正常
  返回）。以后 host 端再起任何"设备会主动来连"的本地 HTTP 服务器，都应该
  默认用 `ThreadingHTTPServer`（或至少显式设置 socket 超时），不要用裸的
  `HTTPServer`。
- vendored 的 M5Stack-Avatar 库文件 `C:\Users\89823\Documents\Arduino\libraries\
  M5Stack_Avatar\src\Effect.h` 里禁用了 Doubt/Angry/Happy/Sad/Sleepy 五个表情
  自带的装饰动画（汗滴/怒气/爱心/竖线/气泡），因为小狗脸不需要这些。这个文件
  在 git 仓库之外，重装 Arduino 库时改动会丢失，需要重新手动禁用。
- 每一轮改动（不管是固件还是 host 端 Python）完成并验证过之后，都应该主动
  `git add` + `git commit`，不用等用户明确要求——这是用户的标准指示，方便
  随时回滚到任何一个历史版本。只 add 实际改动到的文件，不要用 `git add -A`
  之类的宽泛写法，`.env` 等看起来像密钥的文件永远不要提交。
- **任何会被 host 端持续高频轮询的 HTTP 接口（比如 `/volume`），handler 里禁止用
  `malloc()`/`heap_caps_malloc()` 或 Arduino `String` 拼接。** 这两者对偶尔调用
  一次的接口（`/camera`、`/record`）没问题，但对方持续每隔几秒调用一次、可能
  连续运行几小时的接口，会慢慢碎片化本就很小的内部堆，表现为：先是偶尔响应
  变慢/超时，然后连不上，最后设备直接重启。`handleVolume()` 就踩过这个坑（详见
  git log 里 "Fix root cause of StackChan reboots" 那次提交）——修法是用
  `static` 缓冲区 + `snprintf` 到栈上的定长 char 数组，全程零堆分配。以后新增
  类似"host 端常驻轮询"的接口，从一开始就按这个模式写。
- **人脸追踪的 yaw 方向符号不确定时不要靠猜，也不要只做小幅度实测**：
  `track_face_servo()`（`puppy_engine_v4.py`）之前 `FACE_TRACK_YAW_GAIN` 的
  正负号是没有实机条件下猜的，实际是反的——表现为"回答完之后缓慢往一侧
  漂移、最终跟丢人脸"，因为每次"修正"其实都在把人脸推得更偏，越修越偏。
  排查/验证方法：①先查 M5StackChan 库 `motion.h` 里 `lookAtNormalized()` 的
  文档确认 yaw 符号约定（越大越向右转）；②实机测试要用**大幅度**的单次
  转动去看人脸在画面里的位置变化方向——小幅度调整量（几十个单位）容易被
  人体自然晃动的噪声淹没，可能测出"两个方向调整后都显得更偏"的误导结果；
  ③最终用真实的比例控制公式连续跑好几轮，看 yaw 是收敛到一个小区间（方向
  对）还是持续单调跑偏（方向反了），这个方法比单次位移测试更贴近真实运行
  情况、也不怕被噪声干扰。
- **隐私(PRIVACY)状态下摄像头是关闭的（不做人脸检测），这跟困倦(SLEEPY)
  不一样**——`tick()` 的 PRIVACY 分支故意不调用 `check_face()`，只靠语音
  （`check_voice_wake()`）或触摸短按重新唤醒。起因是实机遇到过没人在场时
  被别的物体误判成人脸、突然从隐私跳回开心的情况；而表情映射表里隐私这一
  行的退出条件本来就只写了"听到提问"和触摸，从没提过人脸检测，所以直接把
  这条摄像头轮询去掉了。SLEEPY 没有做这个改动，仍然靠人脸检测退出——以后
  别想当然地假设这两个状态的退出逻辑是对称的。`transition()` 进入 PRIVACY
  时还会重置 `face_detected`/`face_confirm_count`，避免语音唤醒时复用进
  隐私前残留的"人脸已确认"状态而跳过该做的那次扫描。
- **`requests.Session()` 默认 `trust_env=True`，会自动用本机的
  `HTTP_PROXY`/`HTTPS_PROXY` 环境变量**——本机跑着一个本地沙盒代理（也是
  为什么 `curl` 验证设备接口时永远要加 `--noproxy '*'`，见"编译/烧录/验证
  流程"一节），host 端如果不显式关掉，`api_get()` 用的那个全局 `_session`
  也会一样被代理接管。**踩过一次坑，而且很隐蔽**：代理把请求转走以后对
  `192.168.137.100` 这种局域网地址返回的是 `503` + 空 body，不是连接失败
  ——`requests.get()` 不会抛异常（503 是"正常"收到的 HTTP 响应，不是网络层
  错误），`api_get()` 的 `except requests.exceptions.RequestException` 完全
  抓不到，也没做返回值检查，所以整个引擎（表情、舵机、触摸轮询、摄像头…
  所有接口）会全程"看起来正常运行、不报任何错"，实际上每一个请求都没有
  真正到达设备——表现出来就是"舵机怎么摆都不动，好像也听不见说话"，一开始
  很容易被误判成某个具体功能（比如舵机控制本身）的 bug，实际是最底层的
  HTTP 请求就没发出去。修法是创建 `_session` 后立刻
  `_session.trust_env = False`，让它彻底不看代理设置，直连设备。以后
  host 端新增任何直接发请求给 StackChan 的代码，都必须走这个共享
  `_session`（或者同样设置 `trust_env=False`），不要图省事直接
  `requests.get(...)`。
- **本机 PATH 里的 `python`（`where python` 第一个命中）是 Anaconda3 的
  base 环境，没装 cv2/mediapipe/funasr 这些 `puppy_engine_v4.py` 依赖的库**
  ——直接 `python host/puppy_engine_v4.py` 或者用它 `import` 这个文件都会在
  `import cv2` 那行报 `ModuleNotFoundError`。真正装好完整依赖的是一个专门
  的 conda 环境，可执行文件在
  `C:\Users\89823\anaconda3\envs\stackchan\python.exe`——写任何独立诊断/
  测试脚本（比如用 `importlib.util.spec_from_file_location` 单独加载
  `puppy_engine_v4.py` 里的某个函数验证行为，这次调试关机音效/摇晃检测时
  用过好几次这个手法）都要显式用这个路径调用，不能依赖 `python`/`python3`
  这些裸命令解析到正确的环境。
- **`arduino-cli monitor` 有时会稳定返回空的串口捕获**（哪怕设备真的在
  这段时间里重启/打印过内容），原因没有深挖清楚（疑似跟命令行工具对它
  stdout 的重定向/后台化方式有关），遇到这种情况改用 PowerShell 原生的
  `System.IO.Ports.SerialPort` 类更可靠，能确认稳定拿到真实数据（排查
  固件是不是刷错、`/play` 连不上设备这两次都是靠它才拿到关键证据）。用
  法要点：`ReadTimeout` 设一个不太长的值（比如500ms）配合 `try/catch`
  轮询 `ReadLine()`，**必须同时捕获 `[System.TimeoutException]`（正常的
  "这一轮没数据"）和 `[System.IO.IOException]`（端口被物理拔断/设备
  重置导致端口关闭）**——只接 `TimeoutException` 的话，端口一旦在读取
  中途关闭，会陷入一个疯狂抛异常的死循环，产出几 MB 的错误堆栈刷屏（真
  实踩过一次）。用之前要确认没有 `arduino-cli monitor`/别的进程占着串口
  （查一下有没有相关进程，`arduino-cli upload` 同理），这个类不会跟它们
  抢，但两边同时开着谁都读不到完整数据。
- **设备"开机行为看起来不对"时，先确认刷的到底是哪个 sketch**，不要想
  当然是 `firmware.ino`。排查过一次"开机不显示 Starting/WiFi 这些进度
  文字，一插电就直接是小狗表情"的问题，一开始以为是显示逻辑的 bug，最后
  靠抓串口日志看到一行 `expr_preview.ino` 专属的调试字符串
  （`[预览] 切到表情: neutral`，`firmware.ino` 里没有这行）才确认设备
  当时烧的其实是 `expr_preview.ino`（为了快速设计新表情用的最小 sketch，
  见"设计全新表情"一节）——不含 WiFi/HTTP，固件本身没有联网逻辑，自然
  也没有开机进度文字。这类"行为对不上预期"的问题，花点时间先确认设备
  当前实际运行的固件版本，比直接扎进代码逻辑排查更快。
- **`/play` 突然没声音，且没有任何报错**（host 端 `start_play()` 返回
  成功，`/status` 的 `playing` 字段却一直是 `false`，从没变过 `true`）
  ——这是"设备连不上 host 本地那个音频 HTTP 服务器（`ensure_audio_
  server()`，端口 `AUDIO_SERVER_PORT`=8090）"的固定症状，不是代码
  bug，固定诊断方法：抓设备串口日志，看有没有一行 `[play] fetch
  failed: url=... code=-1`（`playTaskFn()` 打的，`code=-1` 是 ESP32
  HTTPClient 库的 `HTTPC_ERROR_CONNECTION_REFUSED`，但实际含义比字面
  更宽，只要 `WiFiClient::connect()` 没成功都会归到这个码，不一定真的
  是"连接被拒绝"），同时确认本机侧一切正常（`netstat` 看 8090 真的在
  `LISTENING`；`curl` 自己的热点 IP 能拿到200）——如果两边都正常但设备
  就是连不上，问题出在本机到热点这段网络路径上，已知会导致这个症状的
  三个独立原因，按改动成本从低到高依次排查：
  1. **Windows 移动热点（ICS）本身的状态问题**：连热点的设备访问外网
     没问题，但"反过来连电脑自己开的端口"这条通路有时会莫名其妙断掉，
     跟防火墙规则对不对没关系。关一次热点再开通常能重置这个状态，不
     需要管理员权限，是成本最低的第一步尝试。
  2. **Windows 防火墙没放行入站端口**：`netsh advfirewall firewall
     add rule name="StackChan Audio Server" dir=in action=allow
     protocol=TCP localport=8090 profile=any`，需要管理员权限，本项目
     的 Claude Code 会话没有，只能请用户自己在管理员终端跑或者走
     "Windows Defender 防火墙"GUI。
  3. **第三方防火墙/安全软件**（比如本机装的火绒 Huorong，`HipsDaemon`/
     `HipsTray` 进程）：它的网络防火墙是一套完全独立于 Windows 自带
     防火墙的引擎，`netsh advfirewall`/`Get-NetFirewallRule` 这些工具
     只能看到 Windows 自带防火墙那一层，看不到火绒自己的拦截逻辑；
     `Get-CimInstance -Namespace root/SecurityCenter2 -ClassName
     FirewallProduct` 也不一定能查到它（火绒不一定往这个 WMI 接口
     注册，之前排查时就被漏掉过一次）。真要排查得让用户自己打开火绒
     的"网络防火墙"→"联网记录"/"拦截日志"，搜 `python.exe` 或端口
     `8090` 看有没有拦截记录，或者查"入站规则"/"程序规则"里
     `python.exe` 是不是被设成了拒绝。
  三个原因目前没办法只凭本机侧的命令行检查互相区分清楚（表现完全一
  样），只能按上面的顺序依次尝试；确认是防火墙类问题（2/3）而不是
  ICS（1）以后，规则一旦配置对了通常就长期有效，不会每次都要重新配。