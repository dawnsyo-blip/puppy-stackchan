# StackChan 小狗行为引擎

基于 [M5Stack StackChan](https://github.com/stack-chan/stack-chan) 桌面机器人的**小狗行为启发情感表达系统**。小狗会追踪你的脸、被摸头会"贴贴"、能听懂你说话并用屏幕按钮动画回应、认得手势、还会陪你玩捉迷藏、到点提醒你喝水吃饭。

（以下是碎碎念，想看技术架构和状态机玩法可以跳过）
## 为什么设计成小狗而不是直接接入 LLM？
1. 直接用原生的小智框架没法接入 Claude。
2. 如果想要一个服务型的工具 agent，用文字的方式沟通更加精准且直接。
3. 选择桌面机器人而不是屏幕桌宠，是因为不希望工作/学习的时候心流被屏幕上移动的事物打断，同时希望能够在休息的时候不用再进行文字工作，改成轻度的物理互动、语音互动。
4. 只是喜欢小狗。

所以结论就是：只要笨笨的就可以了^_^

如果你也喜欢，欢迎和我一起养小狗~

## 交互模式的设计路线

### 表情
其实 M5Stack 的 Avatar 库里面也有小狗的模板表情，但是这个原生的表情没有耳朵，看起来就会像狗的脸长在了一个方形的头上。所以我还是自己设计了带有耳朵的小狗表情。

一般在国内见得比较多的犬种的耳朵都是立耳的，但是我发现立耳放在这个小屏幕里会非常尖锐，可能是因为 stackchan 整体设计偏向圆润。所以我选择了比格犬的垂耳。垂耳的面积比较大，轮廓比较柔软，在视觉上可能起到了一个"过渡元素"的作用，模糊了方形外壳边界和动物面孔之间的断裂。

不过其实按照恐怖谷的理论，给没有皮肤的机器人加上有明显动图特征的耳朵，其实相当于把拟兽化的程度推向一个中间值，理论上来说，其实应该是更容易触发恐怖谷效应。

那为什么加上耳朵，而且是垂耳，观感会更好？可能涉及到 design ambiguity（设计模糊度）和 design atypicality（设计非典型性）这两个因素[（MK Strait，2017）](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2017.01366/full)。

简单来说，模糊度就是指观察者看到的机器的第一眼，可能会把这个物体划归到什么类别中：这是人，还是机器？

设计非典型程度指的就是观察者感觉到的这个机器的设计和这个机器被归属到的类别之间的差异：这个人身上是否有不应该属于人的特征？例如之前讨论度高的 AI 模板脸，大家一眼就可以看出来这是一张人脸，但是仔细看会发现这张脸没有眼神光、没有毛孔、眼神没有变化，这是真人不应该有的特征。这就是高非典型性。

在这篇论文里，对于机器人的评价，非典型性驱动了最强的厌恶。据此我猜测，给 stackchan 加上小狗耳朵以后，虽然没有改善模糊度，但是非典型性减少了，因为垂耳的这个特征让"狗"的归类更加明确。所以整体的不适感也降低了。

### 声音
类似的问题在声音设计上也出现过。TTS 一开始我使用的是女性的声音，而且保留了完整的句子。

也许是因为，我心理上已经把它归类为小狗，那如果它发出了人类的声音，对我来说是一个非常尖锐的非典型性噪音。但是如果不说话，仅仅靠字幕来和人类沟通，又会觉得缺少互动感。

所以，第二版的改动是用关键词来代替完整的句子，模拟小狗用声音按钮和人类沟通的情境。这样，声音和动物机器人外形的冲突就减少了很多。这次改动过程中，我还考虑过换成带有机械音效的儿童音色，因为我本来觉得这可能会更加符合它这种小巧、可爱的外形。但是我发现，因为桌面机器人本身没有办法剥离工具属性，而且 stackchan 的外形还是有很明显的机器人特征，如果给它装儿童的声音，那么看起来就像在物化一个儿童。所以我最后还是舍弃掉了。

那么应该给 ta 选择怎样的音色呢？电影和游戏作品肯定讨论过这个问题，所以我想到了小黄人和[动森](https://github.com/Acedio/animalese.js)。这两种音效基本上都是通过加速还有一些固定的映射规则把语言处理成听不懂的声音，但是同时又保留了语音语调，从而保留了这个非人但又有点像人的角色在说话的真实感。

所以，我最终的方案就是：用关键词和拟人的、中性的声音取代原来的完整的句子表达和有性别、年龄特征的人声，来降低拟兽机器人和人之间的关联。

梳理这个声音设计的过程也让我想到，拟兽机器人身上其实有两条关于写实性的连续脉络。也就是，一方面，恐怖谷效应不仅可能是源于和动物的相似性，同时，还可能源于拟兽机器人为了满足人机交互而被赋予的一些人类的特征以后出现的设计非典型性。而有语音意义的声音就是一个重要的因素。

## 技术路线

技术基座是 [zziying/stackchan-openapi](https://github.com/zziying/stackchan-openapi) 的 HTTP API 架构：ESP32 只负责硬件执行，所有 AI/行为决策都在电脑上跑。

### 架构

```
电脑（大脑）                                    StackChan（身体，ESP32-S3）
┌─────────────────────────────┐                ┌──────────────────────────┐
│ puppy_engine_v4.py（状态机）  │   WiFi HTTP    │  舵机（水平/俯仰）        │
│  ├─ MediaPipe 人脸/手势检测   │ ─────────────▶ │  表情屏幕（自定义狗脸）    │
│  ├─ FunASR 语音识别 (STT)    │ ◀───────────── │  摄像头（GC0308）         │
│  ├─ DeepSeek LLM（意图/回复） │                │  触摸传感器（头顶3区+屏幕）│
│  ├─ animalese 拟声词合成     │                │  麦克风 / 扬声器           │
│  └─ 无线麦克风采集（本地）    │                │  RGB LED                  │
└─────────────────────────────┘                └──────────────────────────┘
```

电脑和 StackChan 通过同一个 WiFi 热点通信；StackChan 暴露一组 HTTP 接口（`/face`、`/servo`、`/touch`、`/camera`、`/play`、`/stream`、`/led`、`/status` 等），电脑端的状态机负责"该做什么"，ESP32 只管"怎么执行"。

### 状态机一览

整个行为引擎用 [Mermaid](https://mermaid.js.org/) 画了一份完整的状态流转图，涵盖语音/视觉/触摸/时间四类触发各自会走到哪个状态，包括这里没有展开讲的所有细节分支：

![状态机图](docs/state_machine.svg)

其中两个比较好玩的功能：

1. **捉迷藏**：通过语音说"我们来玩捉迷藏吧"触发。流程是：把要藏起来的物品放在小狗摄像头前让它"看一眼"→ 小狗报告识别到的物品名称；如果识别错了，有一小段窗口期可以说"不是这个"，它会重新看一次 → 确认无误后小狗"闭眼"并倒数 → 倒数结束后转动舵机在房间里扫描寻找目标。
2. **装死**：摸一下屏幕触发"贴贴"反应之后，会进入约 15 秒的手势识别窗口期。在这段时间内，在设备摄像头前方约 5 厘米处比出"手指枪"的手势，即可触发小狗的"装死"状态；双击头顶可以把它唤醒。

后续还想加入心情日志等功能，缓慢更新。

### 演示视频

<table>
<tr>
<td width="50%">

https://github.com/user-attachments/assets/eae52002-e6ad-4e85-b8a6-3ef63f604e24

语音对话演示：小狗用 animalese 拟声词回应

</td>
<td width="50%">

https://github.com/user-attachments/assets/bad6f06d-566c-4c22-829f-9d35b4a11e76

手指枪手势触发"装死"状态

</td>
</tr>
</table>

### 使用须知

- **语音对话依赖你自己接入的大语言模型**（推理模型或非推理模型均可，DeepSeek、其他兼容 API 都行）——没有配置的话，人脸追踪、触摸反应等其它功能不受影响，但小狗听不懂你在说什么。想让"喝水/出去玩"提醒带上天气相关的关键词，还需要接入一个天气 API（当前用的是和风天气）；这两者都是可选增强，未配置时会自动降级成固定文案，不影响其它功能运行。
- **手势识别（"手指枪→装死"等）本身是纯本地计算**，靠的是 MediaPipe 的手部关键点检测模型，只需要下载一次模型文件，不需要任何 API key。捉迷藏游戏里的物品识别可以选择性接入一个视觉大模型（可以用 Qwen-VL）来提升准确度，但不接入也能跑，退化成基于颜色直方图的简单匹配。
- **语音唤醒目前是基于音量阈值触发的**，建议使用一个连接电脑的外接麦克风来对话，减少环境噪音（尤其是舵机转动声）的干扰；也可以改用机身自带麦克风，但识别准确率可能会明显下降。

## 硬件

- M5Stack StackChan 套件（CoreS3，ESP32-S3）：GC0308 摄像头、双麦克风、扬声器、2 个舵机（水平/俯仰）、头顶触摸传感器 + 触屏、RGB LED。
- 一台能跑 Python 的电脑（Windows/macOS/Linux 均可），建议带 GPU 但非必需。
- 一个无线麦克风（USB 接收器，供电脑采集语音用）。
- 电脑和 StackChan 需要在同一个 WiFi 网络下（推荐用电脑开热点）。

## 快速开始

### 1. 烧录固件

```bash
# 复制并填写你自己的 WiFi/IP 配置
cp firmware/config.h.example firmware/config.h
# 编辑 firmware/config.h 填入 WIFI_SSID / WIFI_PASSWORD / 电脑 IP 等

arduino-cli compile --fqbn m5stack:esp32:m5stack_cores3 firmware
arduino-cli upload --fqbn m5stack:esp32:m5stack_cores3 --port <你的串口> firmware
```

### 2. 配置电脑端

```bash
conda create -n stackchan python=3.10
conda activate stackchan
pip install requests numpy opencv-python mediapipe sounddevice scipy \
            funasr torch torchaudio pypinyin

# 复制并填写你自己的 API key（均为可选增强，缺失时会自动降级/跳过对应功能）
cp .env.example .env
```

需要在 `host/puppy_engine_v4.py` 顶部把 `BASE_URL`（StackChan 的 IP）、`COMPUTER_IP`（电脑在这个 WiFi 下的 IP）改成你自己的实际地址。

### 3. 运行

```bash
python host/puppy_engine_v4.py
```

首次运行会自动下载 `animalese.wav`（字母拟声词音频库）和 FunASR 的语音模型，需要联网。`host/hand_landmarker.task`（MediaPipe 手势检测模型）需要手动下载一次：

```bash
curl -o host/hand_landmarker.task \
  "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
```

## 项目结构

```
firmware/
├── firmware.ino          # 主固件：HTTP API 服务器 + 表情渲染
├── PuppyFace.h            # 自定义小狗表情组件（眼睛/鼻子/耳朵）
├── config.h.example       # WiFi/网络配置模板
└── expr_preview/          # 设计新表情用的独立最小 sketch

host/
└── puppy_engine_v4.py     # 行为状态机（人脸/手势检测、触摸、语音、状态机主循环）
```

## 致谢

- 硬件与固件基座：[stack-chan](https://github.com/stack-chan/stack-chan)、[zziying/stackchan-openapi](https://github.com/zziying/stackchan-openapi)
- 拟声词语音合成算法参考：[animalese.js](https://github.com/Acedio/animalese.js)
