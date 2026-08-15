"""
StackChan 小狗行为引擎 v4
========================
新增内容 (v3 → v4)：
- 整合 voice_test.py 的语音链路：FunASR SenseVoice STT（启动时预加载一次）、
  DeepSeek LLM（API key 从项目根目录 .env 手动解析，不读环境变量）、edge-tts
  语音合成、HTTP 音频服务器播放。
- 新增 CURIOUS（好奇/录音中）、THINKING（思考/LLM处理中）、SORRY（抱歉）三个状态。
- 音量唤醒（方案A）：主循环持续轮询 /volume，音量突增直接扫描找人 → HAPPY →
  进入 CURIOUS 开始真正的问题录音（不再单独录一段做唤醒词校验）。
- 完整对话链路：CURIOUS(录音5s) → THINKING(STT+LLM，四路意图分支) →
  qa_simple(点头/摇头) / qa_complex(逐个念关键词) → HAPPY；
  praise → EXCITED；scold → SORRY。
- EXPRESSION_MAP 改用固件实际支持的表情名（见 firmware.ino handleFace()）。
- 人脸检测、触摸检测（短按扫描/长按兴奋）、空闲计时器、开心状态面部追踪，
  这些 v3 已有的功能全部原样保留。
- 稳定性修复：主循环轮询太密集（触摸/音量/人脸每轮全部调用）会把 StackChan
  的 HTTP 请求打崩导致反复重启。改成触摸/音量/人脸三者轮流、每轮只发起一种
  轮询请求，并放宽了各自的轮询间隔；API 请求失败后等 2 秒重试一次而不是立刻
  重试，避免在设备本来就吃紧的时候继续加压。

依赖（此项目用的是 anaconda "stackchan" 环境，基础环境没装这些）：
    conda activate stackchan
    pip install funasr torch torchaudio edge-tts pydub
ffmpeg 需要在系统 PATH 里（pydub 转 WAV 要用）。
SenseVoiceSmall / fsmn-vad 模型首次运行会自动从 ModelScope 下载并缓存，
如果下载失败/很慢，可以先设 HF_ENDPOINT=https://hf-mirror.com 再运行。

用法: python host/puppy_engine_v4.py
退出: Ctrl+C
"""

import requests
import time
import sys
import json
import asyncio
import tempfile
import threading
import http.server
import socket
import queue
import wave
import io
from pathlib import Path
from enum import Enum
from urllib.parse import quote

import numpy as np
import cv2
import mediapipe as mp
from mediapipe.tasks.python import vision
from mediapipe.tasks.python import BaseOptions


# ╔══════════════════════════════════════════════╗
# ║            可调参数（调参改这里）             ║
# ╚══════════════════════════════════════════════╝

BASE_URL = "http://192.168.137.100"
COMPUTER_IP = "192.168.137.1"   # 电脑在热点网络上的 IP（StackChan 用它来下载要播放的音频，
                                 # 也是 /stream 推流要连过来的地址）
AUDIO_SERVER_PORT = 8080
TIMEOUT = 5                     # 普通请求（/face、/servo、/touch）超时
PLAY_TIMEOUT = 30               # /play 是阻塞调用（播完才返回），要给够时间
API_RETRY_DELAY_SEC = 2.0       # API 请求失败（连不上/超时）后，等这么久再重试一次，
                                 # 不要立刻重试，避免在设备本来就吃紧时继续加压

MAIN_LOOP_INTERVAL_SEC = 0.5    # 主循环 tick() 间隔

# --- 计时器（秒） ---
IDLE_TO_SLEEPY_SEC = 180
SLEEPY_TO_PRIVACY_SEC = 600
FACE_LOST_GRACE_SEC = 20        # 人脸消失后等多久才离开开心
EXCITED_DURATION_SEC = 6

# --- 触摸触发 ---
# 隐私状态太容易被误触退出（之前是短按就退出，很容易被不小心碰一下打断）。
# 现在只保留隐私这一条触摸触发路径，而且要求长按满 3 秒才生效，其它状态
# 原来的触摸触发（短按→扫描找人、长按1秒→兴奋）暂时先关掉——见 tick() 里
# 触摸处理那一段的注释。
PRIVACY_EXIT_HOLD_SEC = 3.0

# --- 面部追踪 ---
FACE_CHECK_INTERVAL_SEC = 3.0
FACE_RETRACK_INTERVAL_SEC = 5   # 开心状态下每N秒回正重检
FACE_DETECTION_CONFIDENCE = 0.6
FACE_CONFIRM_FRAMES = 2
FACE_MODEL_PATH = "C:/tmp/blaze_face_short_range.tflite"

# --- CURIOUS/THINKING 期间的轻量级人脸追踪 ---
# CURIOUS 状态整个持续时间都被 /record 的一次阻塞式 HTTP 调用占满——StackChan
# 的 WebServer 是单线程的，处理 /record 请求期间完全不会响应 /camera，所以
# "每 3 秒追踪一次" 在 CURIOUS 里做不到，只能在开始录音前追踪一次。THINKING
# 阶段等 DeepSeek 回复时设备是空闲的，用后台线程跑 LLM 请求、主线程照常每隔
# FACE_TRACK_INTERVAL_SEC 追踪一次，这里才是真正能做到"持续追踪"的地方。
FACE_TRACK_INTERVAL_SEC = 3.0
# 实机验证过方向是反的，这里记一下排查过程方便以后参考：
# - M5StackChan 库 motion.h 里 lookAtNormalized() 的文档明确写了
#   "X-axis (Yaw): -1.0 (Max Left) <---> 1.0 (Max Right)"，即 yaw 越大越往右转。
# - 实测：脸在画面偏右（face_x=0.56）时，用旧的正数 GAIN 算出来的调整方向会
#   让 yaw 变小（往左转），调整后人脸在画面里的位置从 0.56 移到了 0.65——
#   不是更居中而是更偏右，说明每次"修正"其实是在往错误的方向转，越修越偏，
#   最后脸会转出画面外（这正是"回答完之后缓慢向左漂移、最终丢人脸"的成因）。
# - 另外单独测过：舵机彻底空闲、完全不下发任何 /servo 指令的情况下，连续
#   40 秒每 5 秒读一次 /status，yaw 纹丝不动——排除了"后台有别的代码在悄悄
#   转头"的可能，问题就是这里的方向算反了，不是别处有东西在动舵机。
# 现在改成负数修正：人脸水平偏移(-1..1)换算成 yaw 微调量的系数。
FACE_TRACK_YAW_GAIN = -300
FACE_TRACK_YAW_MAX_STEP = 250    # 单次微调的最大幅度，避免一帧检测误差导致
                                  # 舵机猛地转过头

# --- 开心动画参数 ---
HAPPY_YAW_RANGE = 500
HAPPY_YAW_SPEED = 400
HAPPY_CYCLES = 3
HAPPY_CYCLE_DELAY = 0.4
HAPPY_PITCH = 300

# --- 兴奋动画参数 ---
EXCITED_YAW_RANGE = 800
EXCITED_YAW_SPEED = 500
EXCITED_CYCLES = 5
EXCITED_CYCLE_DELAY = 0.3
EXCITED_PITCH_HIGH = 500
EXCITED_PITCH_LOW = 200

# --- 困倦动画参数 ---
SLEEPY_PITCH_STEPS = [400, 350, 300, 250, 200, 150, 100]
SLEEPY_STEP_DELAY = 0.8
SLEEPY_SPEED = 50

# --- 隐私动画参数 ---
PRIVACY_YAW = 800
PRIVACY_PITCH = 100
PRIVACY_SPEED = 150

# --- LED（对照表情映射v7.xlsx）---
# 固件的 /led 现在支持 mode 参数（solid/blink/breathe/rainbow/fade），呼吸/
# 闪烁/彩虹/渐暗这些需要"持续"播放的效果由固件自己在本地 loop() 里驱动（见
# firmware.ino 的 updateLed()）。host 端只在状态切换时调一次 /led 告诉固件
# "从现在起用哪种模式"，不需要开一个后台线程持续轮询去模拟——早期版本试过
# host 端连续调用 /led 模拟隐私状态的渐暗效果，相当于给这个本不该被高频调用
# 的接口硬造出一次 /volume 当年那种轮询（CLAUDE.md 里记过那次教训：碎片化
# 堆，最后设备反复重启），所以改成了现在这个"固件本地驱动"的架构。
WARM_WHITE_RGB = (255, 180, 90)       # 呼吸灯/闪烁/渐暗/常亮统一用这个暖白色调
CURIOUS_LED_BREATHE_PERIOD_MS = 1600  # 好奇/思考共用"暖白呼吸灯"
SORRY_LED_PERIOD_MS = 1500            # 抱歉"缓慢闪烁"
EXCITED_LED_PERIOD_MS = 300           # 兴奋"彩虹快闪"（具体颜色表内置在固件里）
SLEEPY_LED_FADE_MS = 5000             # 困倦"渐暗至熄灭"
PRIVACY_LED_FADE_MS = 2000            # 隐私"渐暗至熄灭"

# --- 抱歉(sorry)动画参数（数值参考表情映射v7.xlsx）---
SORRY_PITCH = 200                # 微低头
SORRY_YAW = 100                  # 微微偏转，避开视线
SORRY_SPEED = 100

# --- 点头(是) / 摇头(否) 动画参数 ---
NOD_PITCH_A = 300
NOD_PITCH_B = 500
NOD_SPEED = 400
NOD_CYCLES = 2
NOD_CYCLE_DELAY = 0.3

SHAKE_YAW_A = -300
SHAKE_YAW_B = 300
SHAKE_SPEED = 400
SHAKE_CYCLES = 2
SHAKE_CYCLE_DELAY = 0.3

# --- 扫描找人参数 ---
SCAN_POSITIONS = [0, -500, 0, 500, 0]
SCAN_SPEED = 300
SCAN_PAUSE = 1.0

# --- 触摸检测 ---
TOUCH_POLL_SEC = 0.5

# --- 语音唤醒（方案B：TCP 流式监听，取代方案A的 /volume 轮询 + /record
#     间歇录音）---
# 旧方案的根本问题：/volume、/record 都是"一次性、有限时长"的调用，
# /volume 每 VOLUME_POLL_SEC 秒才采样一次，中间大段时间设备根本没在听；
# 触发之后还要另开一次 /record 才能录到真正的问题，如果用户说话节奏跟这套
# "听一下→（可能）转头→再录音"的节奏对不上，开头就被切掉、甚至整段错过。
# 流式方案让 StackChan 主动推流、host 常驻监听，音频从来不会"没在录"，
# 触发条件（音量超过阈值）判断到的那一刻，实际说的内容已经在滚动缓冲区里
# 了，不需要再额外录一次。
STREAM_PORT = 8081                 # StackChan 的 /stream?port=N 主动连过来的端口
STREAM_BUFFER_SECONDS = 8.0        # 滚动缓冲区保留的音频时长——够放下一整句话，
                                    # 又不会无限占内存（16kHz/16bit/单声道下
                                    # 8 秒约 256KB）
STREAM_CHUNK_SECONDS = 0.5         # 每凑够这么多新数据就算一次 RMS
STREAM_SILENCE_SECONDS = 1.0       # 连续这么久 RMS 都低于阈值，判定"说完了"
STREAM_PREROLL_SECONDS = 0.4       # 判定"开始说话"的那一刻往前多留一点余量
                                    # （因为是按 0.5s 一段判定的，真正开口的
                                    # 时刻很可能比"这一段整体超过阈值"稍早）
STREAM_RMS_THRESHOLD = 450         # 语音触发阈值——沿用旧方案(/volume 方案)
                                    # 校准出的数值：安静环境基线约 280-367，
                                    # 说话时能冲到 778 左右，麦克风硬件和增益
                                    # 都没变，只是采样方式从"偶尔采 1 秒"改成
                                    # "连续按 0.5 秒一段"，量级应该还适用；如果
                                    # 实机测下来触发不灵/太灵，从这个值开始调。
# --- 完整对话链路 ---
KEYWORD_GAP_SEC = 0.5            # qa_complex 逐个念关键词，两个关键词之间的间隔
BUTTON_PRESS_MS = 200            # 每个关键词播放前，按钮"按下"状态维持的时长

# --- 字幕：语音段识别出结果后，把识别到的文字显示出来，方便用户确认输入
#     内容，一直保留到 LLM 回复出来、即将执行下一步动作时才清空。改成流式
#     监听（MicStream）之后不再有单独的"录音中"状态可以配麦克风图标——语音
#     是持续后台捕捉的，触发的时候内容已经录完了，所以只剩这一段用途。
#     SenseVoice 不是逐字流式识别模型，做不到真正的"边说边一个字一个字往外
#     蹦"，但退而求其次可以做到：只要用户还在说（MicStream 判定"说完"之前），
#     就每隔一小段时间把目前为止录到的全部内容整段重新识别一次，用新结果
#     整体替换掉字幕——旧结果如果因为当时音频不够长而识别错/识别漏，随着
#     音频变长、后面几次重新识别通常会自动"纠正"过来，效果上就是用户能看到
#     字幕跟着说话大致同步出现、偶尔还会自我修正，而不需要真正的流式模型。
#     见 PARTIAL_TRANSCRIBE_* 系列参数和 MicStream.peek_partial()/
#     PuppyEngine._partial_transcribe_loop()。qa_complex 播报关键词期间用的
#     是爪印按钮（见 speak_keywords()），不是字幕；其它状态只切表情，不显示
#     文字。 ---
SUBTITLE_DUR_MS = 15000  # /speech 的 dur 参数：字幕展示上限（两个场景共用）。
                          # 正常情况下都会有显式的清空调用，这个只是兜底上限，
                          # 避免万一清空请求丢了导致字幕卡住不消失

# --- 实时（增量）字幕 ---
PARTIAL_TRANSCRIBE_ENABLED = True
PARTIAL_TRANSCRIBE_INTERVAL_SEC = 1.2   # 两次增量识别之间至少间隔这么久——说话
                                         # 过程中不停地对整段音频重新做一次完整
                                         # 识别，CPU 是有真实开销的，间隔太短会
                                         # 跟真正说完后那次"官方"识别抢 CPU，
                                         # 反而拖慢最终应答。
PARTIAL_TRANSCRIBE_MIN_NEW_SEC = 0.8    # 距上次增量识别，新增音频不足这么多秒
                                         # 就跳过这一轮，没必要为了多几百毫秒
                                         # 数据就整段重新识别一次。

# --- 语音识别 (FunASR SenseVoice) ---
# 从 faster-whisper 切换过来的原因：不再需要本地维护一份模型文件路径，
# SenseVoiceSmall 用模型名从 ModelScope 按需下载并缓存；效果和幻听表现留待
# 实际使用观察，之前针对 whisper-small 训练数据幻听出的"字幕by索兰娅"这类
# 固定短语是那个模型特有的，SenseVoice 是完全不同的模型/训练数据，没有理由
# 复用同一份黑名单，所以没有搬过来。
SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
SENSEVOICE_DEVICE = "cpu"
SENSEVOICE_LANGUAGE = "zh"

# 用 fsmn-vad 做噪音过滤，等价于之前 faster-whisper 里 vad_filter=True 的
# 作用：先判断音频里有没有真实人声、按语音活动切分，只把真正检测到人声的
# 片段送进 SenseVoice 转写，而不是不管三七二十一把整段静音/噪音也丢给模型
# 识别（那样才会出现"什么都没说也识别出一段话"的幻听问题）。
# max_single_segment_time 是 fsmn-vad 单段最长时长，本项目录音最多几秒钟，
# 默认 30000ms（30秒）绰绰有余，不需要跟着单独调。
SENSEVOICE_VAD_MODEL = "fsmn-vad"
SENSEVOICE_VAD_MAX_SEGMENT_MS = 30000

# --- LLM (DeepSeek) ---
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT = 30
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"   # 项目根目录的 .env

# --- 语音合成 (edge-tts) ---
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_SAMPLE_RATE = 16000          # 转成 StackChan /record 同款格式：16kHz/单声道/16bit

AUDIO_DIR = Path(tempfile.gettempdir()) / "stackchan_audio"
AUDIO_DIR.mkdir(exist_ok=True)

# --- 表情映射：key 是引擎内部状态名，value 是固件 /face?expr= 实际支持的名字
#     （neutral/happy/sad/angry/sleepy/doubt/love/eyeroll/thinking/excited/
#      privacy/curious/sorry，见 firmware.ino 的 handleFace()）---
EXPRESSION_MAP = {
    "idle":     "neutral",
    "happy":    "happy",
    "sleepy":   "sleepy",
    "curious":  "curious",
    "sorry":    "sorry",
    "thinking": "thinking",
    "excited":  "excited",
    "privacy":  "privacy",
}


# ╔══════════════════════════════════════════════╗
# ║                 状态定义                      ║
# ╚══════════════════════════════════════════════╝

class State(Enum):
    IDLE     = "常态"
    HAPPY    = "开心"
    EXCITED  = "兴奋"
    SLEEPY   = "困倦"
    PRIVACY  = "隐私"
    CURIOUS  = "好奇"
    THINKING = "思考"
    SORRY    = "抱歉"


# ╔══════════════════════════════════════════════╗
# ║          LLM 意图分类 system prompt           ║
# ╚══════════════════════════════════════════════╝

SYSTEM_PROMPT = """你是一只比格犬，名字叫"小狗"，你叫主人"老大"。
你家里的朋友：一只三花小猫，名字叫"咪咪"。
你在公园认识的朋友：萨摩耶"耶耶"、边牧"边边"、田园犬"大黄"。

你需要理解老大说的话，用 JSON 标注意图和关键词回应。

规则：
1. 是非问题（可以用"是/否"回答的问题）：不用关键词，直接输出，不要输出别的文字：
{"type": "qa_simple", "answer": "yes" 或 "no"}

2. 老大在表扬你：直接输出，不要输出别的文字：
{"type": "praise"}

3. 老大在责备你：直接输出，不要输出别的文字：
{"type": "scold"}

4. 老大让你去睡觉/休息/自己待一会儿、暗示不再需要你陪着（比如"去睡觉吧"
"你去休息吧""我们先聊到这里"这类意思）：直接输出，不要输出别的文字：
{"type": "privacy"}

5. 开放式问题（qa_complex）或其它情况（other）：
   第一步——先想清楚：如果你是这只小狗，针对老大这句话，你会有什么具体、
   符合逻辑的真实反应？用一句大白话写下来，哪怕很短也行。这句话不会被
   念出来，只是逼自己先想清楚答案，不许跳过这一步，也不许把老大问题里的
   词原样当成"想清楚的答案"。
   第二步——把这句大白话压缩成 2-4 个关键词（空格分隔），再换行输出 JSON：
   {"type": "qa_complex", "keywords": [...]}  或  {"type": "other", "keywords": [...]}

第 5 条的关键词选择规则：
- 关键词必须来自你自己想清楚的那句大白话，不能是老大问题原句里出现的词的
  简单复读。如果发现自己选的词和问题原句几乎一样，说明大概率是偷懒没有
  真的回答，回去重想。
- **每一个关键词本身必须是单个词/短语（1-3个字为主），绝对不能是完整的
  主谓宾句子**（比如不能写"老大叫我"这种，应该拆成"老大"和"叫"两个独立
  的词，各占 keywords 数组里的一项）。你说的每一句话，不管是简单回应还是
  复杂回应，都只能通过关键词表达，不能输出完整语句——这是这只小狗表达
  自己的唯一方式，类似 AAC（辅助沟通）设备，不是在写一句正常的话。
- 优先从下面的词库里选，但不局限于词库，需要时可以用词库外的词（权重从高
  到低排列）：
  需求词（权重最高）：外面、出门、玩、水、零食、飞盘、球球、拔河、罐罐、
  牛奶、睡觉、尿尿、噗噗
  时间词：今天、明天、现在、刚才、结束
  对象词：老大、小猫、咪咪、耶耶、边边、大黄
  地点词：外面、厨房、阳台、房间
  状态词：开心、怕怕、累、饿、痛痛
  情感词：love you、爱你
  动作词：打架、风
  枢纽词（权重次低，仅高于"小狗"，默认少用——只有需要表达明确的意愿/态度、
  单靠其它词说不清楚时才加一个，通常放在词语组合的最后而不是最前面）：
  想、要、来、好、不要
  自称词（权重最低，默认不用）：小狗——你说的每句话本来就是小狗在说，
  没必要每次自报家门。只有在需要特别强调"这件事是我自己的感受/我自己想
  要"这种强烈情感场合，才用一次"小狗 + 想/要 + 具体需求"（小狗放最前
  面），偶尔出现就好，不要变成习惯性开头。
- 每次 2-4 个词，根据表达需要灵活选：简单回应用 2 个，需要更多信息用 3-4 个。
- 不用动名词组合（说"飞盘"，不说"玩飞盘"）。
- 不要重复意思相近的动词。
- 最迫切/最重要的词放最前面。
- 指代对象（老大、咪咪等）或地点（外面、厨房）放在前面。
- 允许重复同一个词表达强烈情感，比如"外面 外面 外面"。

示例：
用户：今天天气怎么样？
今天天气很好，还有风
外面 好 风
{"type": "qa_complex", "keywords": ["外面", "好", "风"]}

用户：你想吃什么？
好想吃零食和罐罐
零食 罐罐 想
{"type": "qa_complex", "keywords": ["零食", "罐罐", "想"]}

用户：咪咪在哪里？
咪咪在厨房呀
咪咪 厨房
{"type": "qa_complex", "keywords": ["咪咪", "厨房"]}

用户：你为什么不去找咪咪玩？
因为咪咪在睡觉，不想吵她
咪咪 睡觉
{"type": "qa_complex", "keywords": ["咪咪", "睡觉"]}

用户：谁最想出去玩呀？
是我呀，我最想出去玩了
小狗 想 外面
{"type": "qa_complex", "keywords": ["小狗", "想", "外面"]}

用户：你想出去玩吗？
{"type": "qa_simple", "answer": "yes"}

用户：你真棒！
{"type": "praise"}

用户：去睡觉吧
{"type": "privacy"}

用户：你把鞋子咬坏了！
{"type": "scold"}"""


# ╔══════════════════════════════════════════════╗
# ║              API 辅助函数                     ║
# ╚══════════════════════════════════════════════╝

# 复用一个 Session：StackChan 的 WebServer 没有主动发 "Connection: close"，
# 支持 HTTP keep-alive。之前每次 requests.get() 都会新开一个 TCP 连接，
# 对 ESP32 本来就紧张的 WiFi/LWIP 连接资源是额外压力；用同一个 Session 让
# urllib3 复用连接，减少设备侧频繁建连/拆连的开销。
_session = requests.Session()

def api_get(endpoint, timeout=None, _retry=True):
    """GET 请求失败（连不上/超时）时不要立刻重试——先等 API_RETRY_DELAY_SEC，
    重试一次；再失败就放弃，返回 None。避免在设备已经吃紧时连续拍请求。"""
    try:
        return _session.get(f"{BASE_URL}{endpoint}", timeout=timeout or TIMEOUT)
    except requests.exceptions.RequestException as e:
        print(f"  [请求失败] {endpoint}: {e}")
        if _retry:
            time.sleep(API_RETRY_DELAY_SEC)
            return api_get(endpoint, timeout=timeout, _retry=False)
        return None

def set_expression(key):
    api_get(f"/face?expr={EXPRESSION_MAP.get(key, 'neutral')}")

def set_subtitle(text, dur_ms=None):
    """调用固件的 /speech 显示底部字幕气泡（见 firmware.ino 的 handleSpeech()/
    drawSubtitle()）。传空字符串等于清空字幕。"""
    q = f"/speech?text={quote(text)}"
    if dur_ms:
        q += f"&dur={int(dur_ms)}"
    api_get(q)

def set_led(r=0, g=0, b=0, off=False):
    """调用固件的 /led 立即设成某个静态颜色（off=True 时直接关灯），用于一次性
    的短促效果（比如语音触发时闪两下白灯）。会覆盖掉当前状态本该持续播放的
    LED 效果（呼吸/闪烁等），用完记得恢复，见 set_led_mode()。"""
    if off:
        api_get("/led?off=1")
    else:
        api_get(f"/led?r={r}&g={g}&b={b}")

def set_led_mode(mode, r=0, g=0, b=0, period_ms=None, fade_ms=None):
    """调用固件 /led 的 mode 参数，让固件自己在本地持续驱动呼吸/闪烁/彩虹/
    渐暗这些效果（见 firmware.ino 的 updateLed()）。状态切换时调一次就够，
    之后固件自己接管，不需要 host 端持续发请求维持效果。"""
    q = f"/led?mode={mode}&r={r}&g={g}&b={b}"
    if period_ms is not None:
        q += f"&period_ms={int(period_ms)}"
    if fade_ms is not None:
        q += f"&fade_ms={int(fade_ms)}"
    api_get(q)

def set_button(state):
    """调用固件的 /button 控制关键词播报按钮：up/down/off。"""
    api_get(f"/button?state={state}")

def move_servo(yaw=None, pitch=None, speed=None):
    params = []
    if yaw is not None:   params.append(f"yaw={yaw}")
    if pitch is not None: params.append(f"pitch={pitch}")
    if speed is not None: params.append(f"speed={speed}")
    if params: api_get(f"/servo?{'&'.join(params)}")

def go_home():
    api_get("/home")

def get_touch():
    r = api_get("/touch")
    if r:
        try:    return r.json()
        except: return None
    return None

def get_status():
    """/status 会带回舵机当前实际的 yaw/pitch，用来给人脸追踪算增量微调，
    不用自己在 host 端维护一份"当前 yaw"的状态（很多地方都会改 yaw，维护
    起来容易和实机不同步）。"""
    r = api_get("/status")
    if r:
        try:    return r.json()
        except: return None
    return None

def capture_frame():
    r = api_get("/camera")
    if r and r.status_code == 200:
        arr = np.frombuffer(r.content, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return None


# ╔══════════════════════════════════════════════╗
# ║               动画函数                        ║
# ╚══════════════════════════════════════════════╝

def play_happy_animation():
    set_expression("happy")
    set_led_mode("solid", *WARM_WHITE_RGB)
    move_servo(pitch=HAPPY_PITCH, speed=HAPPY_YAW_SPEED)
    time.sleep(0.2)
    for _ in range(HAPPY_CYCLES):
        move_servo(yaw=HAPPY_YAW_RANGE, speed=HAPPY_YAW_SPEED)
        time.sleep(HAPPY_CYCLE_DELAY)
        move_servo(yaw=-HAPPY_YAW_RANGE, speed=HAPPY_YAW_SPEED)
        time.sleep(HAPPY_CYCLE_DELAY)
    # 动画结束后回正，确保摄像头对准人
    move_servo(yaw=0, pitch=450, speed=300)

def play_excited_animation():
    set_expression("excited")
    set_led_mode("rainbow", period_ms=EXCITED_LED_PERIOD_MS)
    for _ in range(EXCITED_CYCLES):
        move_servo(yaw=EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_HIGH, speed=EXCITED_YAW_SPEED)
        time.sleep(EXCITED_CYCLE_DELAY)
        move_servo(yaw=-EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_LOW, speed=EXCITED_YAW_SPEED)
        time.sleep(EXCITED_CYCLE_DELAY)
    move_servo(yaw=0, pitch=450, speed=400)

def play_sleepy_animation():
    set_expression("sleepy")
    set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=SLEEPY_LED_FADE_MS)
    move_servo(yaw=0, speed=200)
    time.sleep(0.2)
    for p in SLEEPY_PITCH_STEPS:
        move_servo(pitch=p, speed=SLEEPY_SPEED)
        time.sleep(SLEEPY_STEP_DELAY)

def play_privacy_animation():
    set_expression("privacy")
    move_servo(yaw=PRIVACY_YAW, pitch=PRIVACY_PITCH, speed=PRIVACY_SPEED)
    set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=PRIVACY_LED_FADE_MS)

def play_idle_animation():
    set_expression("idle")
    go_home()
    # 常态的灯效关掉了（原来是"微弱暖白常亮"）——按要求直接熄灯。
    set_led(off=True)

def play_curious_animation():
    """好奇：显示表情即可——语音已经由后台流式监听（MicStream）捕捉完毕，
    这里不用再等录音。"""
    set_expression("curious")
    set_led_mode("breathe", *WARM_WHITE_RGB, period_ms=CURIOUS_LED_BREATHE_PERIOD_MS)

def play_thinking_animation():
    """思考：显示表情即可，持续时长就是 STT+LLM 实际处理耗时。LED 呼吸灯
    延续好奇状态的效果（表情映射表里"思考"这行写的也是"保持暖白呼吸灯"），
    这里重新调一次只是为了保证独立进入 THINKING 时也一定是对的，不依赖
    "一定是从好奇过来的"这个假设。"""
    set_expression("thinking")
    set_led_mode("breathe", *WARM_WHITE_RGB, period_ms=CURIOUS_LED_BREATHE_PERIOD_MS)

def play_sorry_animation():
    set_expression("sorry")
    move_servo(pitch=SORRY_PITCH, yaw=SORRY_YAW, speed=SORRY_SPEED)
    set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)

def play_nod_animation():
    """点头，模拟 qa_simple 的"是"。"""
    for _ in range(NOD_CYCLES):
        move_servo(pitch=NOD_PITCH_A, speed=NOD_SPEED)
        time.sleep(NOD_CYCLE_DELAY)
        move_servo(pitch=NOD_PITCH_B, speed=NOD_SPEED)
        time.sleep(NOD_CYCLE_DELAY)
    move_servo(pitch=450, speed=300)

def play_shake_animation():
    """摇头，模拟 qa_simple 的"否"。"""
    for _ in range(SHAKE_CYCLES):
        move_servo(yaw=SHAKE_YAW_A, speed=SHAKE_SPEED)
        time.sleep(SHAKE_CYCLE_DELAY)
        move_servo(yaw=SHAKE_YAW_B, speed=SHAKE_SPEED)
        time.sleep(SHAKE_CYCLE_DELAY)
    move_servo(yaw=0, speed=300)


# ╔══════════════════════════════════════════════╗
# ║              语音链路辅助函数                 ║
# ╚══════════════════════════════════════════════╝

def local_model_cache_dir(model_id, revision="master"):
    """把 funasr AutoModel 的 model/vad_model 参数从"模型 ID 字符串"尽量换成
    本地 ModelScope 缓存目录的绝对路径。

    实测过：即使模型早就缓存在本地，funasr 的 download_from_ms() 只要一看到
    参数是个 ID 字符串（而不是一个已经存在的路径），就会去调
    get_or_download_model_dir() -> ModelScope 的 snapshot_download()，对方会
    挨个请求 SenseVoiceSmall 20 个文件、fsmn-vad 8 个文件的元信息核对哈希——
    这几次网络往返（本机还挂着代理，见 CLAUDE.md）就是"预加载"里那几秒卡顿
    的来源，模型本体其实根本没有重新下载。反过来，如果 model 参数本身就是
    一个已经存在的本地目录，download_from_ms() 会直接跳过
    get_or_download_model_dir()，完全不发请求。用同样的临时脚本量过：
    两个模型一起加载，模型 ID 字符串方式 8.80s，本地路径方式 4.13s，省下的
    ~4.7s 基本就是这几次网络请求的开销，剩下的 4s 多是加载 ~900MB 权重文件
    本身的硬盘 IO + 反序列化，没法再省。

    `fsmn-vad` 这种简写在 funasr 内部会先查表转换成完整 ID（比如
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"）才去找缓存目录，这里要
    自己复刻一遍这个查表，不然拼出来的路径对不上。缓存目录还没建好（比如第
    一次运行、或者以后手动清过缓存）就原样把 model_id 传回去，照常走一遍
    ModelScope 下载——不会因为这个优化导致首次运行失败。"""
    try:
        from funasr.download.name_maps_from_hub import name_maps_ms
        resolved = name_maps_ms.get(model_id, model_id)
    except ImportError:
        resolved = model_id
    cache_dir = (
        Path.home() / ".cache" / "modelscope" / "models"
        / resolved.replace("/", "--") / "snapshots" / revision
    )
    if (cache_dir / "configuration.json").exists() or (cache_dir / "config.yaml").exists():
        return str(cache_dir)
    return model_id


def load_deepseek_api_key():
    """从项目根目录 .env 手动解析 DEEPSEEK_API_KEY（不读环境变量，也不依赖
    python-dotenv —— 项目用的 conda 环境里没装这个包）。"""
    if not ENV_PATH.exists():
        print(f"  [.env] 未找到: {ENV_PATH}")
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DEEPSEEK_API_KEY":
            return value.strip().strip('"').strip("'")
    print("  [.env] 未找到 DEEPSEEK_API_KEY")
    return None


_audio_server = None

def ensure_audio_server():
    """启动一个 HTTP 文件服务器，让 StackChan 能下载要播放的音频。"""
    global _audio_server
    if _audio_server is not None:
        return

    handler = lambda *args: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(AUDIO_DIR)
    )
    handler.log_message = lambda *args: None  # 抑制日志输出

    _audio_server = http.server.HTTPServer(("0.0.0.0", AUDIO_SERVER_PORT), handler)
    thread = threading.Thread(target=_audio_server.serve_forever, daemon=True)
    thread.start()
    print(f"  [音频服务器] http://{COMPUTER_IP}:{AUDIO_SERVER_PORT}/")


def play_wav_file(path):
    """让 StackChan 通过 /play 下载并播放本地音频文件（阻塞到播完）。"""
    filename = Path(path).name
    play_url = f"http://{COMPUTER_IP}:{AUDIO_SERVER_PORT}/{filename}"
    r = api_get(f"/play?url={play_url}", timeout=PLAY_TIMEOUT)
    ok = r is not None and r.status_code == 200
    if not ok:
        print(f"  [播放] 失败: {r.status_code if r else '无响应'}")
    return ok


def tts_to_wav(text, out_stem):
    """用 edge-tts 合成语音，转换成 16kHz/单声道/16bit WAV，返回文件路径（失败返回 None）。"""
    mp3_path = AUDIO_DIR / f"{out_stem}.mp3"
    wav_path = AUDIO_DIR / f"{out_stem}.wav"
    try:
        import edge_tts

        async def _generate():
            communicate = edge_tts.Communicate(text, TTS_VOICE)
            await communicate.save(str(mp3_path))

        asyncio.run(_generate())

        from pydub import AudioSegment
        audio = AudioSegment.from_mp3(str(mp3_path))
        audio = audio.set_frame_rate(TTS_SAMPLE_RATE).set_channels(1).set_sample_width(2)
        audio.export(str(wav_path), format="wav")
        return wav_path
    except Exception as e:
        print(f"  [TTS] 合成失败: {e}")
        return None


def ask_llm(user_text, api_key):
    """调用 DeepSeek，返回 (reply_text, intent, parsed_data)。失败时 intent='other'。"""
    if not api_key:
        return None, "other", {}

    try:
        r = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 200,
                "temperature": 0.7,
            },
            timeout=DEEPSEEK_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        print(f"  [LLM] 请求失败: {e}")
        return None, "other", {}

    if r.status_code != 200:
        print(f"  [LLM] API 返回 {r.status_code}: {r.text[:200]}")
        return None, "other", {}

    try:
        full_reply = r.json()["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [LLM] 响应格式不对，解析失败: {e!r}  body={r.text[:200]}")
        return None, "other", {}
    reply_text = full_reply
    intent = "other"
    data = {}
    try:
        json_start = full_reply.rfind("{")
        if json_start >= 0:
            data = json.loads(full_reply[json_start:])
            intent = data.get("type", "other")
            reply_text = full_reply[:json_start].strip()
    except (json.JSONDecodeError, KeyError):
        pass

    return reply_text, intent, data


# ╔══════════════════════════════════════════════╗
# ║          语音流式监听 (MicStream)             ║
# ╚══════════════════════════════════════════════╝

def _pcm_rms(chunk: bytes) -> float:
    """算一段 16bit PCM 数据的 RMS 响度。"""
    if not chunk:
        return 0.0
    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples)))


def pcm_to_wav_bytes(pcm: bytes, sample_rate=16000, channels=1, sample_width=2) -> bytes:
    """给一段裸 PCM 数据加上 WAV 头，SenseVoice/wave 模块都要读完整的 WAV 文件，
    不能直接吃裸 PCM。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class MicStream:
    """StackChan 通过 /stream?port=N 主动连过来推流 PCM 音频（16bit/16kHz/
    单声道），这个类在 host 端常驻监听、接收、缓冲、做简单的语音活动检测：

    - 一个后台线程负责 accept：绑定端口监听，StackChan 连上来就持续收数据；
      连接断开（无论是对端主动关闭、网络问题，还是固件那边 /play 期间暂停
      推流导致的重连）就回到 accept() 等下一次连接——全程不需要 host 主动
      发起重连，是 StackChan 那边主动连过来的，host 只要一直守着端口就行。
    - 收到的数据一份写入滚动缓冲区（只保留最近 STREAM_BUFFER_SECONDS 秒），
      另一份喂给 VAD：每凑够 STREAM_CHUNK_SECONDS 秒新数据算一次 RMS，超过
      阈值记为"正在说话"，连续 STREAM_SILENCE_SECONDS 秒低于阈值判定"说完
      了"——这时直接从滚动缓冲区里把这段语音（含一点前置余量）切出来放进
      一个小队列，主循环用 take_utterance() 非阻塞取走，不需要再另外调用
      /record 补录一次，因为要说的话已经在缓冲区里了。

    音频的读写全部发生在同一个后台线程里（accept 线程本身），所以内部状态
    不需要加锁；跟主线程之间唯一的交接点是线程安全的 _utterance_queue。
    """

    def __init__(self, port=STREAM_PORT, sample_rate=16000,
                 buffer_seconds=STREAM_BUFFER_SECONDS,
                 chunk_seconds=STREAM_CHUNK_SECONDS,
                 silence_seconds=STREAM_SILENCE_SECONDS,
                 preroll_seconds=STREAM_PREROLL_SECONDS,
                 rms_threshold=STREAM_RMS_THRESHOLD):
        self.port = port
        self.sample_rate = sample_rate
        self._bytes_per_sample = 2
        self._max_buffer_bytes = int(buffer_seconds * sample_rate * self._bytes_per_sample)
        self._chunk_bytes = int(chunk_seconds * sample_rate * self._bytes_per_sample)
        self._silence_chunks_needed = max(1, round(silence_seconds / chunk_seconds))
        self._preroll_bytes = int(preroll_seconds * sample_rate * self._bytes_per_sample)
        self.rms_threshold = rms_threshold

        self._buffer = bytearray()
        self._total = 0            # 已收到的总字节数（绝对偏移，单调递增）
        self._rms_cursor = 0       # 下一次要做 RMS 判定的绝对起点

        self._speech_active = False
        self._speech_start_abs = 0
        self._quiet_chunks = 0

        self._utterance_queue = queue.Queue()
        self._running = False
        self._server_sock = None
        self._accept_thread = None
        self.connected = False     # 仅供主循环/调试查看，不用于同步

    def start(self):
        if self._running:
            return
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self):
        """程序退出时调用：停掉 accept 循环、关掉监听 socket 和当前连接。"""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    def take_utterance(self):
        """非阻塞取出最近一段捕捉到的完整语音（bytes，裸 PCM）；没有就绪的
        返回 None。如果处理不过来、队列里积压了好几段，只保留最新的一段——
        堆积的旧片段大概率是过时的噪音，没必要一段段排队处理。"""
        segment = None
        while True:
            try:
                segment = self._utterance_queue.get_nowait()
            except queue.Empty:
                break
        return segment

    def peek_partial(self):
        """跟 take_utterance() 不同：不等"判定说完"，只要当前正处于"检测到在
        说话"这个状态，就把从开始说话到现在收到的全部音频原样吐出去（还在
        继续增长，可以反复调用）；不在说话状态时返回 None。给增量字幕用。

        这个方法从主线程（PuppyEngine 的后台增量识别线程）调用，跟 accept
        线程之间不像 take_utterance() 靠 _utterance_queue 那样有专门的线程安全
        交接。对一段仅用于展示的字幕来说这个折衷可以接受：最坏情况是读到的
        切片跟 _speech_active 标志之间差了一两帧（比如刚好在这一刻判定说完
        了），不会崩溃或者数据错乱，下一次调用就会用最新状态纠正过来。"""
        if not self._speech_active:
            return None
        base = self._total - len(self._buffer)
        lo = max(0, self._speech_start_abs - base)
        return bytes(self._buffer[lo:])

    # ---------- 内部：接收 + 缓冲 + VAD ----------

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(1)
        srv.settimeout(1.0)
        self._server_sock = srv
        print(f"[流式监听] TCP 服务器已启动 0.0.0.0:{self.port}，等待 StackChan 连接...")

        while self._running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # stop() 关掉了监听 socket

            print(f"[流式监听] StackChan 已连接 {addr}")
            self.connected = True
            # 新连接开始，丢弃上一次连接里可能还没判定完的"正在说话"状态，
            # 避免跨连接（比如中间断了好几分钟）把两段不相干的声音拼在一起。
            self._speech_active = False
            self._quiet_chunks = 0
            self._recv_loop(conn)
            self.connected = False
            print("[流式监听] 连接断开，等待重连...")

        srv.close()

    def _recv_loop(self, conn):
        conn.settimeout(5.0)
        try:
            while self._running:
                data = conn.recv(4096)
                if not data:
                    break
                self._feed(data)
        except socket.timeout:
            print("[流式监听] 5 秒没收到数据，断开重连")
        except OSError as e:
            print(f"[流式监听] 接收出错: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _feed(self, data: bytes):
        self._buffer += data
        self._total += len(data)
        if len(self._buffer) > self._max_buffer_bytes:
            del self._buffer[: len(self._buffer) - self._max_buffer_bytes]

        while self._total - self._rms_cursor >= self._chunk_bytes:
            start_abs = self._rms_cursor
            end_abs = start_abs + self._chunk_bytes
            base = self._total - len(self._buffer)
            lo, hi = start_abs - base, end_abs - base
            if lo < 0:
                # 这个 chunk 已经被滚动缓冲区挤掉了，理论上不会发生（缓冲区
                # 留了 STREAM_BUFFER_SECONDS 秒，远比一个 chunk 长），兜底跳过。
                self._rms_cursor = base
                continue
            chunk = bytes(self._buffer[lo:hi])
            self._rms_cursor = end_abs
            self._process_chunk(start_abs, end_abs, chunk)

    def _process_chunk(self, start_abs, end_abs, chunk):
        rms = _pcm_rms(chunk)
        if rms >= self.rms_threshold:
            if not self._speech_active:
                self._speech_active = True
                self._speech_start_abs = max(0, start_abs - self._preroll_bytes)
            self._quiet_chunks = 0
        elif self._speech_active:
            self._quiet_chunks += 1
            if self._quiet_chunks >= self._silence_chunks_needed:
                # 说完了：结束点回退到"最后一段响的 chunk 结尾"，不把安静的
                # 尾巴也算进去。
                speech_end_abs = end_abs - self._chunk_bytes * (self._quiet_chunks - 1)
                self._emit_utterance(self._speech_start_abs, speech_end_abs)
                self._speech_active = False
                self._quiet_chunks = 0

    def _emit_utterance(self, start_abs, end_abs):
        base = self._total - len(self._buffer)
        lo = max(0, start_abs - base)
        hi = min(len(self._buffer), end_abs - base)
        if hi <= lo:
            return
        segment = bytes(self._buffer[lo:hi])
        dur = len(segment) / self._bytes_per_sample / self.sample_rate
        print(f"[流式监听] 捕捉到一段语音，时长 {dur:.2f}s")
        self._utterance_queue.put(segment)


# ╔══════════════════════════════════════════════╗
# ║               行为引擎                        ║
# ╚══════════════════════════════════════════════╝

class PuppyEngine:
    def __init__(self):
        self.state = State.IDLE
        self.last_interaction = time.time()
        self.state_enter_time = time.time()

        # 人脸检测
        opts = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=FACE_MODEL_PATH),
            min_detection_confidence=FACE_DETECTION_CONFIDENCE,
        )
        self.face_detector = vision.FaceDetector.create_from_options(opts)
        self.last_face_check = 0
        self.last_retrack_time = 0
        self.face_detected = False
        self.face_confirm_count = 0
        self.last_face_seen_time = 0

        # 一次"来访"期间是否已经打过招呼——第一次进 HAPPY 播完整开心动画后
        # 置 True，期间人脸丢失/重新检测到都不再重复播放动画，只在真正离开
        # 很久（进 SLEEPY/PRIVACY）之后才重置，见 enter_happy()/transition()。
        self.session_active = False

        # 触摸检测
        self.last_touch_poll = 0
        self.touch_pressed = False
        self.touch_press_start = 0

        # 主循环计数（用于轮流轮询 + 心跳打印）
        self.tick_count = 0

        # 语音链路：启动时一次性预加载，避免每次对话都重新加载模型。
        # 模型首次运行会自动从 ModelScope 下载并缓存到本地，之后启动就是直接
        # 加载缓存——但"直接加载缓存"不代表零网络开销，见
        # local_model_cache_dir() 的说明：传模型 ID 字符串的话 funasr 每次都会
        # 找 ModelScope hub 核对一遍文件哈希，传本地目录路径能把这一步跳过。
        print("[引擎] 预加载 SenseVoice 模型...")
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        self._rich_transcription_postprocess = rich_transcription_postprocess
        self.asr_model = AutoModel(
            model=local_model_cache_dir(SENSEVOICE_MODEL),
            vad_model=local_model_cache_dir(SENSEVOICE_VAD_MODEL),
            vad_kwargs={"max_single_segment_time": SENSEVOICE_VAD_MAX_SEGMENT_MS},
            device=SENSEVOICE_DEVICE,
            disable_update=True,
        )
        # 增量字幕线程和正式对话链路都会调用 transcribe()，两者共用同一个
        # AutoModel 实例——ONNX/PyTorch 推理会话在 CPU 上不保证能安全并发调用，
        # 用这把锁把两边的 generate() 调用互斥掉，退化成"谁先谁跑"而不是真的
        # 同时跑，避免偶发的资源竞争问题。
        self._asr_lock = threading.Lock()
        print("[引擎] SenseVoice 就绪")

        self.deepseek_api_key = load_deepseek_api_key()
        if self.deepseek_api_key:
            print("[引擎] DeepSeek API key 已从 .env 加载")
        else:
            print("[引擎] [警告] 没有 DeepSeek API key，对话链路里的 LLM 调用会失败")

        ensure_audio_server()

        # 语音流式监听：先在 host 端把 TCP 监听起来，再告诉 StackChan 开始
        # 推流过来——顺序不能反，不然 StackChan 连过来的时候 host 这边端口
        # 还没起来，第一次连接会失败（好在后台任务本身会自动重试，不是致命
        # 问题，但先起监听更干净）。
        self.mic_stream = MicStream()
        self.mic_stream.start()
        api_get(f"/stream?port={STREAM_PORT}")

        # 增量字幕：用户还在说话期间，周期性地把目前录到的内容整段重新识别
        # 一次，见 _partial_transcribe_loop()。独立线程运行，不占用主 tick()
        # 循环的时间，也不会拖慢触摸/人脸轮询。
        self._running = True
        if PARTIAL_TRANSCRIBE_ENABLED:
            self._partial_transcribe_thread = threading.Thread(
                target=self._partial_transcribe_loop, daemon=True
            )
            self._partial_transcribe_thread.start()

        print("[引擎] 小狗行为引擎 v4 启动！")
        print(f"[引擎] 当前状态: {self.state.value}")
        print(f"[引擎] （触摸触发暂时只保留隐私状态退出：长按满 {PRIVACY_EXIT_HOLD_SEC}s，"
              f"其它状态的短按/长按触摸触发已临时关闭）")
        print(f"[引擎] 持续流式监听 (rms >= {STREAM_RMS_THRESHOLD}) → 扫描找人 → 开心 → 思考 → 回应")
        print("[引擎] Ctrl+C 退出\n")

    # ---------- 状态转移 ----------

    # 重要：所有进入 HAPPY 的路径必须走 enter_happy()，不要直接调
    # transition(State.HAPPY)。transition() 每次都会重新播放完整的开心动画
    # （摇头等），只有 enter_happy() 会检查 session_active——同一次来访期间
    # 只在第一次打招呼时播完整动画，之后人脸短暂丢失又重新检测到、或者对话
    # 间隙重新确认人脸在场，都应该静默切到/停在 HAPPY，不能再触发一次完整
    # 动画打断正在进行的事情。全仓库搜索 `self.transition(State.HAPPY)` 的
    # 结果应该只有 enter_happy() 自己内部这一处调用。
    def transition(self, new_state):
        old = self.state
        if old == new_state:
            return
        print(f"[转移] {old.value} → {new_state.value}")
        self.state = new_state
        self.state_enter_time = time.time()

        # 进 SLEEPY/PRIVACY 说明主人已经离开很久了，这次"来访"结束，下次
        # 再见到人脸要重新完整地打招呼。
        if new_state in (State.SLEEPY, State.PRIVACY):
            self.session_active = False

        # 隐私状态下 tick() 不再轮询摄像头（见下面 PRIVACY 分支的说明），
        # 所以进入隐私那一刻起，"是否检测到人脸"这个状态本身就该视为未知，
        # 不能继续沿用进隐私前残留的 True——不然 check_voice_wake() 会因为
        # 这个陈旧的 True 而跳过重新扫描，等于摄像头"看似关了"但旧的判断
        # 结果还在悄悄生效。
        if new_state == State.PRIVACY:
            self.face_detected = False
            self.face_confirm_count = 0

        if   new_state == State.HAPPY:    play_happy_animation()
        elif new_state == State.EXCITED:  play_excited_animation()
        elif new_state == State.SLEEPY:   play_sleepy_animation()
        elif new_state == State.PRIVACY:  play_privacy_animation()
        elif new_state == State.IDLE:     play_idle_animation()
        elif new_state == State.CURIOUS:  play_curious_animation()
        elif new_state == State.THINKING: play_thinking_animation()
        elif new_state == State.SORRY:    play_sorry_animation()

    def enter_happy(self):
        """人脸(重新)确认在场、准备进入/停留在 HAPPY 时统一走这里，而不是直接
        调 transition(State.HAPPY)：一次来访期间只在第一次打招呼时播放完整的
        开心动画（摇头等），之后 session_active 为 True 期间，不管是人脸短暂
        丢失又重新检测到、还是对话间隙重新确认人脸在场，都只是静默切到/停在
        HAPPY，不重复播放动画。"""
        if not self.session_active:
            self.session_active = True
            self.transition(State.HAPPY)
            return
        if self.state == State.HAPPY:
            return
        print(f"[转移] {self.state.value} → 开心（静默，来访进行中，不重复播放动画）")
        self.state = State.HAPPY
        self.state_enter_time = time.time()
        set_expression("happy")
        # 不重复播放摇头动画，但 LED 还是要重新确认一下模式：如果是从别的
        # 地方（比如 speak_keywords() 播报关键词、EXCITED 定时结束）静默切
        # 回开心，LED 可能还停在上一个状态的效果上，这里补一次常亮。
        set_led_mode("solid", *WARM_WHITE_RGB)

    def record_interaction(self):
        self.last_interaction = time.time()

    # ---------- 人脸检测 ----------

    def detect_face_once(self):
        """拍一帧检测人脸，返回 (是否检测到, 人脸中心的水平归一化坐标)。
        坐标范围 0.0(最左)~1.0(最右)；没检测到人脸时坐标为 None。"""
        img = capture_frame()
        if img is None:
            return False, None
        h, w = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        results = self.face_detector.detect(mp_img)
        if not results.detections:
            return False, None
        bbox = results.detections[0].bounding_box
        face_center_x = bbox.origin_x + bbox.width / 2
        return True, face_center_x / w

    def track_face_servo(self, face_x):
        """把人脸水平位置(0.0=最左, 1.0=最右)相对画面中心(0.5)的偏差，
        转换成一次 yaw 微调量并下发。只做增量式的小步微调（受
        FACE_TRACK_YAW_MAX_STEP 限制），不是把头转到某个绝对角度，这样连续
        调用时不会出现大幅度突然转头。"""
        offset = (face_x - 0.5) * 2  # 换算成 -1..1，负值＝人脸偏向画面左侧
        status = get_status()
        current_yaw = status.get("yaw", 0) if status else 0
        delta = max(-FACE_TRACK_YAW_MAX_STEP,
                    min(FACE_TRACK_YAW_MAX_STEP, -offset * FACE_TRACK_YAW_GAIN))
        new_yaw = max(-1280, min(1280, int(current_yaw + delta)))
        print(f"[追踪] 人脸位置={face_x:.2f}，yaw {current_yaw}→{new_yaw}")
        move_servo(yaw=new_yaw, speed=200)

    def track_face_once(self):
        """CURIOUS/THINKING 状态下的轻量级人脸追踪：检测到人脸就用
        track_face_servo 微调朝向，让设备持续朝向说话的人；没检测到人脸就
        不动舵机。返回是否检测到了人脸——调用方用这个判断"这轮对话期间追踪
        是不是全程成功"，决定对话结束后要不要重新触发 scan_for_face()。"""
        found, face_x = self.detect_face_once()
        if not found:
            print("[追踪] 本次未检测到人脸")
            return False
        self.track_face_servo(face_x)
        return True

    def check_face(self):
        """带连续确认的人脸检测。"""
        now = time.time()
        if now - self.last_face_check < FACE_CHECK_INTERVAL_SEC:
            return
        self.last_face_check = now

        found, _ = self.detect_face_once()

        if found:
            self.face_confirm_count += 1
            if self.face_confirm_count >= FACE_CONFIRM_FRAMES:
                if not self.face_detected:
                    print("[检测] 人脸确认")
                self.face_detected = True
                self.last_face_seen_time = now
                self.record_interaction()
        else:
            self.face_confirm_count = 0
            if self.face_detected and (now - self.last_face_seen_time > FACE_LOST_GRACE_SEC):
                print(f"[检测] 人脸丢失（超过 {FACE_LOST_GRACE_SEC}s 未检到）")
                self.face_detected = False

    def retrack_face(self):
        """开心状态下定期检测人脸并跟随：检测到就用 track_face_servo 微调
        朝向，而不是每次都先把头转回 yaw=0 再检测——那样每隔
        FACE_RETRACK_INTERVAL_SEC 就会有一次生硬的"回正"动作，人脸不在正
        前方时看起来像在甩头。现在只做增量微调，跟随更平滑。"""
        now = time.time()
        if now - self.last_retrack_time < FACE_RETRACK_INTERVAL_SEC:
            return
        self.last_retrack_time = now

        found, face_x = self.detect_face_once()
        if found:
            self.last_face_seen_time = now
            self.face_detected = True
            self.record_interaction()
            self.track_face_servo(face_x)
            print("[追踪] 人脸仍在")
        else:
            print("[追踪] 本次未检到人脸")

    # ---------- 扫描找人 ----------

    def scan_for_face(self):
        """转头扫描找人脸。"""
        print("[扫描] 转头找人...")
        set_expression("curious")

        for yaw_pos in SCAN_POSITIONS:
            move_servo(yaw=yaw_pos, pitch=450, speed=SCAN_SPEED)
            time.sleep(SCAN_PAUSE)
            found, _ = self.detect_face_once()
            if found:
                print(f"[扫描] 在 yaw={yaw_pos} 找到人脸！")
                self.face_detected = True
                self.face_confirm_count = FACE_CONFIRM_FRAMES
                self.last_face_seen_time = time.time()
                self.record_interaction()
                return True

        print("[扫描] 没找到人脸")
        go_home()
        set_expression("idle")
        return False

    # ---------- 触摸检测 ----------

    def check_touch(self):
        """检测触摸松开，返回这次按住的秒数（float）；没有发生松开动作时
        返回 None。不再在这里分类"短按/长按"——调用方（tick()）自己按需要
        的阈值判断，目前只有隐私状态退出这一处在用（长按满
        PRIVACY_EXIT_HOLD_SEC 才生效）。"""
        now = time.time()
        if now - self.last_touch_poll < TOUCH_POLL_SEC:
            return None
        self.last_touch_poll = now

        touch = get_touch()
        if touch is None:
            return None

        pressed = touch.get("pressed", False)

        # 按下瞬间：记录开始时间
        if pressed and not self.touch_pressed:
            self.touch_press_start = now
            print("[触摸] 按下...")

        # 松开瞬间：计算持续时间
        duration = None
        if not pressed and self.touch_pressed:
            duration = now - self.touch_press_start
            print(f"[触摸] 松开（{duration:.1f}s）")
            self.record_interaction()

        self.touch_pressed = pressed
        return duration

    # ---------- 语音唤醒 + 完整对话链路 ----------

    def transcribe(self, wav_bytes, filename):
        """把一段 WAV 字节数据识别成文字（复用预加载好的 SenseVoice 模型）。
        AutoModel 初始化时挂了 fsmn-vad（见 __init__），会先做语音活动检测，
        只把真正检测到人声的片段送进 SenseVoice——没有人声时 generate() 直接
        不返回文本，不会像纯靠主模型自己判断"有没有在说话"那样，在纯噪音/
        静音输入上瞎编一段结果出来。"""
        wav_path = AUDIO_DIR / filename
        wav_path.write_bytes(wav_bytes)
        with self._asr_lock:
            res = self.asr_model.generate(
                input=str(wav_path), language=SENSEVOICE_LANGUAGE, use_itn=True,
            )
        if not res or not res[0].get("text"):
            return ""
        return self._rich_transcription_postprocess(res[0]["text"]).strip()

    def _partial_transcribe_loop(self):
        """后台线程：只要 MicStream 判定用户"正在说话"（peek_partial() 不是
        None），就每隔 PARTIAL_TRANSCRIBE_INTERVAL_SEC 把目前为止录到的内容
        整段重新识别一次并更新字幕，让用户不用等到"说完 + 正式识别跑完"才
        第一次看到文字。一旦 MicStream 判定说完（peek_partial() 变回
        None，进入 scan_for_face/CURIOUS/THINKING 那条正式链路），这里立刻
        停手不再发起新的识别——跟 run_conversation_turn() 里的正式识别共用
        同一个模型对象，靠 self._asr_lock 互斥，不会真的并发调用，最多是
        谁先谁跑、后来的等一下。"""
        last_len = 0
        last_call_time = 0.0
        while self._running:
            partial = self.mic_stream.peek_partial()
            if partial is None:
                last_len = 0
                time.sleep(0.2)
                continue

            now = time.time()
            new_seconds = (len(partial) - last_len) / 2 / self.mic_stream.sample_rate
            if (now - last_call_time < PARTIAL_TRANSCRIBE_INTERVAL_SEC
                    or new_seconds < PARTIAL_TRANSCRIBE_MIN_NEW_SEC):
                time.sleep(0.15)
                continue

            last_call_time = now
            last_len = len(partial)
            wav_bytes = pcm_to_wav_bytes(partial, sample_rate=self.mic_stream.sample_rate)
            text = self.transcribe(wav_bytes, "partial.wav")
            # 识别这几百毫秒里用户可能已经说完了（甚至正式链路已经接管），
            # 这种"过期"结果不再展示，避免字幕在正式流程接管之后又被拽回来。
            if text and self.mic_stream.peek_partial() is not None:
                set_subtitle(text, dur_ms=SUBTITLE_DUR_MS)

    def check_voice_wake(self):
        """检查后台流式监听（MicStream）有没有捕捉到一段完整的语音——不需要
        发任何 HTTP 请求，纯内存队列查询，所以这个方法可以每个 tick 都调用，
        不用像旧的 /volume 轮询那样节流。抓到语音就直接扫描找人→开心→跑完
        对话链路的思考/应答部分：不再需要像以前那样另外进 CURIOUS 状态录一次
        音——流式监听本身就一直在录，捕捉到的这段音频就是用户刚刚说的话。
        返回 True 表示这次 tick 已经被这套流程占用。"""
        segment = self.mic_stream.take_utterance()
        if segment is None:
            return False

        dur = len(segment) / 2 / self.mic_stream.sample_rate
        print(f"[唤醒] 捕捉到语音段（{dur:.2f}s）")
        self.record_interaction()

        if self.face_detected:
            # 最近一次人脸检测/追踪已经确认人还在场——HAPPY 状态下
            # retrack_face() 每 FACE_RETRACK_INTERVAL_SEC 秒都在刷新这个
            # 状态，对话进行中的后续几轮问答基本都会走这条分支。没必要每问
            # 一句都重新转头扫描一遍：scan_for_face() 光是第一个位置就要等
            # SCAN_PAUSE(1s)，没扫到还要接着转，这段时间全部堆在"思考"前面，
            # 是可以直接省掉的延迟。就算这期间人其实已经走开了也不要紧：
            # 马上进入的 CURIOUS 阶段 track_face_once() 会立刻再确认一次，
            # 追踪失败的话 _settle_happy() 收尾时还是会补上一次完整扫描。
            print("[唤醒] 人脸已确认在场，跳过扫描，直接开始对话")
            self.enter_happy()
            self.run_conversation_turn(segment)
        elif self.scan_for_face():
            self.enter_happy()
            self.run_conversation_turn(segment)
        return True

    def run_conversation_turn(self, pcm_bytes):
        """run_conversation_turn() 内部涉及好几层网络调用（DeepSeek、edge-tts、
        StackChan 的 HTTP 接口），任何一层偶尔抛出的未预料异常（比如 DeepSeek
        返回了不含 choices 字段的响应体）以前都不会被 run() 主循环之外的任何
        地方接住——while True 主循环只 catch KeyboardInterrupt，一旦这里抛出
        异常整个引擎进程就直接崩溃退出，之后不管是语音、触摸还是人脸检测全部
        失灵，表现出来就是"只有第一次对话能成功，后续再也不触发"。这里包一层
        try/except 确保对话链路里出的任何问题都只是这一轮对话失败、退回安全
        状态，不会打死整个引擎；finally 里按用户要求打印一行确认状态机确实
        恢复了、下一轮 tick 会继续检查流式监听。"""
        try:
            self._run_conversation_turn_body(pcm_bytes)
        except Exception as e:
            print(f"[对话] [异常] run_conversation_turn 出错，回退到安全状态: {e!r}")
            if self.face_detected:
                self.enter_happy()
            else:
                self.transition(State.IDLE)
        finally:
            # 不在这里兜底关灯——现在每个状态都有自己该持续播放的 LED 效果
            # （呼吸/闪烁/常亮等，见 transition()/enter_happy()），无论是正常
            # 走完还是走了上面的异常分支，各自都已经通过 transition()/
            # enter_happy() 把 LED 设成了当前状态该有的样子，这里再关一次灯
            # 反而会把刚设好的效果盖掉。
            print(f"[对话] 对话结束，回到状态={self.state.value}，恢复流式监听")

    def _run_conversation_turn_body(self, pcm_bytes):
        """完整的一次语音交互：好奇 → 思考(STT+LLM) → 按意图四路分支应对。
        全程用 track_ok 记录"这轮对话期间人脸追踪是否一直成功"，成功的话结束
        后就不需要再重新 scan_for_face() 一次——详见 _settle_happy()。要说的话
        已经在 pcm_bytes 里了（后台流式监听 MicStream 捕捉到的完整语音段），
        这里不需要再另外录一次音。"""
        track_ok = True

        # 语音突增触发后先闪两下白灯再常亮，跟切好奇表情同步——灯光比表情变化
        # 更显眼，让用户第一时间意识到小狗有反应了。闪烁=开→关→开（两次
        # "开"调用中间夹一次"关"），最后停在常亮状态。
        set_led(255, 255, 255)
        time.sleep(0.15)
        set_led(off=True)
        time.sleep(0.15)
        set_led(255, 255, 255)

        self.transition(State.CURIOUS)
        # 趁着还在好奇状态顺手做一次人脸追踪——录音已经不需要等了（内容早就
        # 在 pcm_bytes 里），这里纯粹是让好奇表情下有个实际动作，同时保证每轮
        # 对话至少有一次追踪判断。
        if not self.track_face_once():
            track_ok = False

        set_led(off=True)
        self.transition(State.THINKING)
        wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate=self.mic_stream.sample_rate)
        user_text = self.transcribe(wav_bytes, "question.wav")
        print(f"[对话] 识别结果: 「{user_text}」")
        if not user_text:
            print("[对话] 没识别到内容")
            self._settle_happy(track_ok)
            return
        # 识别结果一出来就立刻显示在字幕框里，方便用户确认小狗听到的是什么；
        # 一直留到 LLM 回复出来、即将执行下一步动作时才清空（见下面 join 后）。
        set_subtitle(user_text, dur_ms=SUBTITLE_DUR_MS)

        # 等 DeepSeek 回复期间设备是空闲的（不像 CURIOUS 时被 /record 占满），
        # 用后台线程发 LLM 请求，主线程每隔 FACE_TRACK_INTERVAL_SEC 追踪一次。
        llm_result = {}
        def _call_llm():
            llm_result["value"] = ask_llm(user_text, self.deepseek_api_key)
        llm_thread = threading.Thread(target=_call_llm, daemon=True)
        llm_thread.start()
        last_track = time.time()
        while llm_thread.is_alive():
            if time.time() - last_track >= FACE_TRACK_INTERVAL_SEC:
                last_track = time.time()
                if not self.track_face_once():
                    track_ok = False
            time.sleep(0.2)
        llm_thread.join()
        reply_text, intent, data = llm_result.get("value", (None, "other", {}))
        # LLM 到这里已经有结果了（或者失败），用户提问的字幕不管接下来是失败
        # 兜底还是正常回应都不再需要，统一在这里清空。
        set_subtitle("")

        if reply_text is None:
            print("[对话] LLM 调用失败")
            self._settle_happy(track_ok)
            return

        print(f"[对话] 回复:「{reply_text}」 意图: {intent}")
        self.record_interaction()

        if intent == "qa_simple":
            answer = data.get("answer", "no")
            self._settle_happy(track_ok)
            if answer == "yes":
                print("[对话] 简单回应: 点头 (yes)")
                play_nod_animation()
            else:
                print("[对话] 简单回应: 摇头 (no)")
                play_shake_animation()

        elif intent == "praise":
            print("[对话] 表扬 → 兴奋")
            self.transition(State.EXCITED)

        elif intent == "scold":
            print("[对话] 责备 → 抱歉")
            self.transition(State.SORRY)

        elif intent == "privacy":
            print("[对话] 老大示意去休息/独处 → 隐私")
            self.transition(State.PRIVACY)

        else:  # qa_complex / other：逐个念关键词。数量是 2-4 个，不固定，
               # 顺序（迫切程度/指代对象和地点在前等）由 SYSTEM_PROMPT 里的
               # 规则直接约束 LLM 输出，这里不再做任何重排——早期版本用
               # jieba 词性标注做过"名词全部排到动词前面"的后处理，但新
               # 关键词词库里状态词/情感词等不是单纯名词动词二分，而且现在
               # 顺序本身就带着语义（比如"小狗 想 零食"里"想"必须紧跟在
               # "小狗"后面），机械按词性重排反而会破坏这个顺序，所以这版
               # 直接信任 LLM 给出的顺序。
            keywords = data.get("keywords") or [reply_text[:6]]
            print(f"[对话] 复杂回应，播报关键词: {keywords}")
            self._settle_happy(track_ok)
            self.speak_keywords(keywords)

    def _settle_happy(self, track_ok):
        """对话收尾时决定要不要重新扫描找人：整轮对话期间 track_face_once()
        一直检测到人脸，说明设备本来就一直朝向说话的人，直接进 HAPPY 就好；
        中途丢失过的话，先用 scan_for_face() 转头重新定位一次再决定。
        EXCITED 只会来自这里以外的两处：LLM 判定 praise，或触摸长按——这个
        方法不会触发 EXCITED。"""
        if track_ok:
            print("[追踪] 对话期间人脸追踪全程成功，直接进入开心，不重新扫描")
            self.enter_happy()
        else:
            print("[追踪] 对话期间追踪丢失过，重新扫描定位")
            if self.scan_for_face():
                self.enter_happy()
            else:
                self.transition(State.IDLE)

    def speak_keywords(self, keywords):
        """依次合成并播放每个关键词，关键词之间间隔 KEYWORD_GAP_SEC。不再用字幕
        提示——思考一结束就让屏幕右下角的爪印按钮出现，之后每念一个关键词前
        （包括第一个）按钮都先"按一下"（down 保持 BUTTON_PRESS_MS 后弹回 up，
        固件那边会把这一段渲染成缩小再放大的动画），关键词音频要在按钮
        "放大"的同一刻开始播——如果像之前那样等按钮弹回 up 以后才现合成
        TTS，合成本身的网络延迟（几百毫秒到一两秒不等）会让音频比放大动画晚
        很多才响，两者根本对不上。所以改成提前一步合成：进循环前先把第 1 个
        词的音频备好，每播完一个词、趁按钮消失前的间隔时间顺手把下一个词也
        合成好，这样每次轮到按钮弹起时音频总是已经就绪，可以立刻播放。

        第一个关键词前那次"按一下"容易被吃掉：按钮从隐藏到出现本身也是一段
        0→正常大小的过渡（用的是同一个 buttonScaleAnim_），如果紧接着就触发
        第一次"按一下"，这段出现过渡可能还没播完就被打断——这时候按一下的
        收缩目标（BUTTON_DOWN_SCALE）比当前还没长全的尺寸更大，视觉上完全
        看不出"缩小"这一半，只会像是直接长到了全尺寸，跟后面几个关键词之间
        明明白白的"缩小再放大"不一致。所以按钮刚出现后要显式等一下（借用
        BUTTON_PRESS_MS 这档时长，比固件里 150ms 的动画时长留了余量），确认
        出现动画已经播完、按钮停在正常大小，再开始给第一个关键词播放"按一
        下"的动画。

        全部念完后按钮消失。这是全系统唯一用到按钮的场景。

        LED：表情映射表里这一行写的是"语音响起时亮起暖白灯"——每个关键词的
        音频播放期间用 set_led() 临时点亮暖白色（借用/覆盖掉开心状态本来在
        跑的常亮效果），播完就熄灭。这是一次性覆盖，不是通过 set_led_mode()
        进某个持续模式，所以全部念完以后要显式把 LED 交还给当前状态本该有
        的常驻效果（开心的暖白常亮，或者扫描失败落回常态时保持熄灭）——不
        然会一直卡在"熄灭"上，直到下一次真正的状态切换才会恢复。"""
        set_button("up")
        time.sleep(BUTTON_PRESS_MS / 1000)  # 等"隐藏→出现"这段过渡动画播完，
                                             # 确保第一次"按一下"是从正常大小
                                             # 开始收缩，而不是打断还没长全的
                                             # 出现动画
        next_wav = tts_to_wav(keywords[0], "kw_0") if keywords else None
        for i in range(len(keywords)):
            set_button("down")
            time.sleep(BUTTON_PRESS_MS / 1000)
            set_button("up")
            wav_path = next_wav
            if wav_path:
                set_led(*WARM_WHITE_RGB)
                play_wav_file(wav_path)
                set_led(off=True)
            if i + 1 < len(keywords):
                next_wav = tts_to_wav(keywords[i + 1], f"kw_{i + 1}")
            time.sleep(KEYWORD_GAP_SEC)
        set_button("off")

        if self.state == State.HAPPY:
            set_led_mode("solid", *WARM_WHITE_RGB)
        elif self.state == State.IDLE:
            set_led(off=True)

    # ---------- 计时器 ----------

    def idle_seconds(self):
        return time.time() - self.last_interaction

    def state_duration(self):
        return time.time() - self.state_enter_time

    # ---------- 主循环 ----------

    def tick(self):
        self.tick_count += 1
        print(f"[循环] tick #{self.tick_count}, 状态={self.state.value}")

        # 触摸/人脸两者轮流，每一轮 tick 只发起其中一种会打 HTTP 请求的轮询，
        # 避免同一轮里对 StackChan 连打好几个请求。语音唤醒不算在这个轮转
        # 里——check_voice_wake() 现在只是查一下后台流式监听线程的内存队列，
        # 不发请求，每个 tick 都查一次没有额外开销，还能让语音触发反应更快。
        poll_slot = self.tick_count % 2   # 0=触摸 1=人脸

        # --- 触摸（最高优先级）---
        # 其它状态的触摸触发条件暂时先取消（原来的短按→扫描找人、长按1秒→
        # 兴奋），只留隐私状态这一条退出路径：长按满 PRIVACY_EXIT_HOLD_SEC(3s)
        # 才生效，用来解决隐私状态太容易被误触退出的问题（之前短按就退出，
        # 不小心碰一下就会打断）。
        touch_hold_sec = self.check_touch() if poll_slot == 0 else None
        if touch_hold_sec is not None and self.state == State.PRIVACY:
            if touch_hold_sec >= PRIVACY_EXIT_HOLD_SEC:
                print(f"[触发] 长按 {touch_hold_sec:.1f}s → 退出隐私")
                if self.scan_for_face():
                    self.enter_happy()
            else:
                print(f"[触发] 隐私状态下按了 {touch_hold_sec:.1f}s，不足 "
                      f"{PRIVACY_EXIT_HOLD_SEC}s，忽略")
            return

        # --- 语音唤醒（好奇/思考期间已经在处理语音了，不重复检查）---
        if self.state not in (State.CURIOUS, State.THINKING):
            if self.check_voice_wake():
                return

        # --- 状态内行为 ---
        # 人脸检测（会发 /camera 请求）只在轮到 poll_slot==1 时真正执行；
        # 计时器类判断（空闲/困倦/兴奋持续时间）不发请求，每轮都可以正常判断。
        do_face_check = (poll_slot == 1)

        if self.state == State.IDLE:
            if do_face_check:
                self.check_face()
            if self.face_detected:
                print("[触发] 被动检测到人脸")
                self.enter_happy()
            elif self.idle_seconds() > IDLE_TO_SLEEPY_SEC:
                print("[触发] 空闲超过 3 分钟")
                self.transition(State.SLEEPY)

        elif self.state == State.HAPPY:
            # 持续追踪人脸
            if do_face_check:
                self.retrack_face()
            if not self.face_detected:
                print("[触发] 人脸确认离开")
                self.transition(State.IDLE)

        elif self.state == State.EXCITED:
            if self.state_duration() > EXCITED_DURATION_SEC:
                if self.face_detected:
                    self.enter_happy()
                else:
                    self.transition(State.IDLE)

        elif self.state == State.SLEEPY:
            if do_face_check:
                self.check_face()
            if self.face_detected:
                print("[触发] 困倦中检测到人脸！")
                self.enter_happy()
            elif self.state_duration() > SLEEPY_TO_PRIVACY_SEC:
                print("[触发] 困倦超过 10 分钟 → 隐私")
                self.transition(State.PRIVACY)

        elif self.state == State.PRIVACY:
            # 故意不轮询摄像头：之前跟 SLEEPY/IDLE 一样靠 check_face() 自动
            # 判断退出，但实机遇到过没人在场时被别的物体误判成人脸、突然从
            # 隐私跳回开心的情况。表情映射表里隐私这一行的退出条件本来就只
            # 写了"听到提问"（语音）和触摸，没提摄像头/人脸——这里索性彻底
            # 停止人脸检测，相当于隐私状态下"关摄像头"，直到被 check_voice_
            # wake()（tick() 顶部，不分状态每轮都会跑）或 check_touch() 的
            # 短按分支（已经把 PRIVACY 算进"短按→扫描找人"那一档）重新唤醒
            # 才会再看一眼摄像头。这两条路径已经覆盖了"语音或触摸触发"，这
            # 里不需要再做什么。
            pass

        elif self.state == State.SORRY:
            # 表情映射v7.xlsx：抱歉状态只由新的语音唤醒打断（上面已经处理），
            # 单纯看到人脸不会自动跳出，所以这里只更新追踪状态，不触发转移。
            if do_face_check:
                self.check_face()

        # CURIOUS / THINKING 是瞬时状态：run_conversation_turn() 会同步跑完
        # 录音→识别→LLM→分支应对的整个过程才返回，tick() 观察不到这两个状态。

    def run(self):
        play_idle_animation()
        time.sleep(1)

        hb_interval = 30
        last_hb = time.time()

        try:
            while True:
                self.tick()

                now = time.time()
                if now - last_hb > hb_interval:
                    print(
                        f"[心跳] 状态={self.state.value}, "
                        f"空闲={self.idle_seconds():.0f}s, "
                        f"人脸={'有' if self.face_detected else '无'}"
                    )
                    last_hb = now

                time.sleep(MAIN_LOOP_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n[引擎] Ctrl+C，归位中...")
            play_idle_animation()
        finally:
            # 不管是正常 Ctrl+C 退出还是主循环里跑出了没接住的异常，都要把
            # TCP 监听关掉、告诉 StackChan 别再往这边推流了——不然进程都退出
            # 了，固件那边还在无意义地反复尝试重连一个没人听的端口。
            print("[引擎] 清理流式监听...")
            self._running = False
            self.mic_stream.stop()
            api_get("/stream?stop=1", timeout=3)
            print("[引擎] 已退出。")


if __name__ == "__main__":
    PuppyEngine().run()
