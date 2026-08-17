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
- `/status` 现在多一个 `playing` 字段（当前是否有播放任务在跑）。
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
- **对象词库里指代主人的词是"人"，不是"老大"**：SYSTEM_PROMPT 描述人设/
  规则的大段说明文字里仍然用"老大"（"你叫主人'老大'""老大在表扬你"等，
  这些没有改，是小狗对主人的称呼设定本身）；但第 5 条规则里 `对象词` 这个
  分类下、真正会被当成关键词念出来的词表，已经从"老大"改成了"人"（连同
  规则说明里举例的"老大叫我"→"人叫我"、"指代对象（老大、咪咪等）"→
  "（人、咪咪等）"一起改了，保持内部一致）。以后只想改"关键词库里念出来
  的词"，不要连带把持续沿用的人设称呼也改掉，这是两件独立的事——一个是
  小狗心里怎么称呼主人，一个是它用 AAC 按钮"说话"时选用哪个词。

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
- **当前各状态的 LED 常驻效果**（跟`表情映射v7.xlsx`保持同步）：常态
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
  松开沿上，不含屏幕触摸——碰屏幕本身会立刻触发"小开心"的表情+舵机动作，
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

## 触摸触发映射（handle_touch_trigger() 的三条路径）
判断/处理逻辑集中在 `PuppyEngine.handle_touch_trigger(touch)` 一个方法里
——不止 `tick()` 调用它，讲话过程中（`wait_for_playback()`）也要用同一套
判断，两处不能各写一份，见下面"讲话时触摸立刻打断"一节。
- **碰屏幕 → "小开心"（摸头反应）**：`enter_xiaokaixin()`，不设状态限制、
  也不依赖 `scan_for_face()`。之前这条复用的是"短按→扫描找人"那套逻辑，
  `scan_for_face()` 转头找不到人脸时会静默失败、既不报错也不进开心——这才
  是"碰屏幕没反应"的根因，不是触摸事件本身没测到。碰屏幕时用户显然就在
  设备正前方，不需要再靠摄像头确认一次。**"小开心"**是这个反应动作的
  正式名字：开心表情 + 轻微小幅度抬头 3 次（`play_xiaokaixin_animation()`,
  `XIAOKAIXIN_PITCH_UP/DOWN`），跟"进入开心"状态本身的完整摇头动画
  （`play_happy_animation()`）是两个不同的动作，动作播完保持在开心表情/
  状态上；即使当前已经是 HAPPY 状态、摸一下屏幕也会再触发一次这个反应
  动画。**只有碰屏幕这一条路径触发"小开心"**——听到呼唤"小狗小狗"（见下面
  "'小狗小狗'呼唤"一节）触发的是完整的"开心"（`enter_happy()`，跟被动
  检测到人脸是同一个方法），不是"小开心"；两者名字很像，改的时候别弄混。
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
  （播完整摇头动画），不是"小开心"**，这两个名字容易搞混，别改错。不会
  像 qa_simple/qa_complex 那样调 `_settle_happy(track_ok)`，道理是用户显然
  就在附近正对着它说话，不需要摄像头再确认一次人脸位置。

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