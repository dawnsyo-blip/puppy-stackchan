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
- `host/puppy_engine_v4.py` — 行为状态机（人脸检测、触摸、空闲计时、语音唤醒），当前最新版
- `host/voice_test.py` — 语音链路独立测试（录音→STT→LLM→TTS→播放）
- `表情映射v7.xlsx`（仓库根目录，不在 git 里）— 每个状态的触发/退出条件、表情、
  舵机、LED、声音规格表，是行为设计的权威参考。这份是跟当前代码同步过的
  （v6 存在跟代码对不上的地方，已经改完存成 v7 并删掉了 v6），改状态机行为
  时应该先查这张表；如果代码要改成跟表不一致的行为，应该同时更新表格，不要
  让两边再次脱节。

## HTTP API
- `/face?expr=<表情>` — 切换表情（neutral/happy/sleepy/curious/sorry/thinking/excited/privacy）
- `/servo?yaw=N&pitch=N&speed=N` — 控制舵机（yaw 越大越向右转，越小越向左转，
  见下面"人脸追踪方向"）
- `/camera` — 拍照返回 JPEG。**这是机身正面朝外的摄像头，拍到的是房间/用户，
  不是屏幕截图**——固件没有截屏接口，没有办法远程看到 LCD 上表情/按钮的实际
  渲染效果，调表情/UI 只能靠实机肉眼看或者让用户拍照确认。
- `/touch` — 触摸传感器状态
- `/play?url=<url>` — 播放 WAV 音频
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
  （`puppy_engine_v4.py`）里，播放某个关键词前必须提前一步把它的 TTS 合成
  好（不能等按钮弹起再现合成——edge-tts 网络合成的延迟有几百毫秒到一两秒，
  会让音频比"放大"动画晚很多才响）。另外**第一个关键词前的按钮动画容易被
  吞掉**：按钮从隐藏到出现本身也是一段过渡动画（复用同一个
  `buttonScaleAnim_`），如果出现后立刻触发第一次"按一下"，可能会在出现过渡
  还没播完时就被打断，视觉上看不出"缩小"这一半——出现后要显式等一小段时间
  （等于固件那边动画时长）确认已经长到正常大小，再开始第一次按一下。

## 语音唤醒 / 流式监听（MicStream）
`host/puppy_engine_v4.py` 的语音唤醒不再靠轮询 `/volume` + 触发后另开
`/record`（两者都是一次性、有限时长的调用，大部分时间设备根本没在"听"）。
现在是 StackChan 主动通过 `/stream?port=N` 连到 host 常驻监听的 TCP 端口
持续推 PCM，host 端的 `MicStream` 类维护滚动缓冲区 + 简单 VAD（按
`STREAM_CHUNK_SECONDS` 分段算 RMS，超过 `STREAM_RMS_THRESHOLD` 记为说话，
连续 `STREAM_SILENCE_SECONDS` 低于阈值判定说完），说完那一刻直接把这段语音
从缓冲区切出来，不需要再另外录一次——`check_voice_wake()` 因此变成纯内存
队列查询，不发 HTTP 请求，可以每个 tick 都调用。
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
`puppy_engine_v4.py` 里的人设是比格犬"小狗"，称主人"老大"，家庭朋友三花猫
"咪咪"，公园朋友萨摩耶"耶耶"/边牧"边边"/田园犬"大黄"；qa_complex/other 的
回应是从一套固定的 AAC 风格词库（枢纽词/需求词/时间词/对象词/地点词/状态词/
情感词/动作词）里选 2-4 个关键词。改这份 prompt 时踩过两类坑，都不是"改一次
就好"，是这个模型/这套架构下会反复遇到的模式：

- **`DEEPSEEK_MODEL = "deepseek-chat"` 是非推理模型，没有隐藏思维链**——
  如果 prompt 直接让它"输出关键词"而不要求先写一句真实的回答，它会走最省力
  的路径：直接从老大问题原句里捞词拼一拼（尤其问题本身就包含词库里也有的
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