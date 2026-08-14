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
COMPUTER_IP = "192.168.137.1"   # 电脑在热点网络上的 IP（StackChan 用它来下载要播放的音频）
AUDIO_SERVER_PORT = 8080
TIMEOUT = 5                     # 普通请求（/face、/servo、/touch、/volume）超时
RECORD_TIMEOUT_MARGIN = 8       # /record?seconds=N 的请求超时 = N + 这个余量
PLAY_TIMEOUT = 30               # /play 是阻塞调用（播完才返回），要给够时间
API_RETRY_DELAY_SEC = 2.0       # API 请求失败（连不上/超时）后，等这么久再重试一次，
                                 # 不要立刻重试，避免在设备本来就吃紧时继续加压

MAIN_LOOP_INTERVAL_SEC = 0.5    # 主循环 tick() 间隔

# --- 计时器（秒） ---
IDLE_TO_SLEEPY_SEC = 180
SLEEPY_TO_PRIVACY_SEC = 600
FACE_LOST_GRACE_SEC = 20        # 人脸消失后等多久才离开开心
EXCITED_DURATION_SEC = 6
LONG_PRESS_THRESHOLD = 1.0      # 按住超过这个时间=长按

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
FACE_TRACK_YAW_GAIN = 300        # 人脸水平偏移(-1..1)换算成 yaw 微调量的系数。
                                  # 偏移方向和 yaw 正方向的对应关系没法在没有
                                  # 实机的情况下确认，如果调整方向反了（越调
                                  # 越偏），把这个值取负号即可。
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

# --- 抱歉(sorry)动画参数（数值参考表情映射v6.xlsx）---
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

# --- 语音唤醒（方案A：音量触发）---
VOLUME_POLL_SEC = 3.0            # 轮询 /volume 的间隔。实测 /volume 每次调用都会启停一次
                                  # 麦克风(I2S)，持续高频调用即使 2s 一次也偶尔会让设备重启，
                                  # 所以留了更大的余量；如果还不稳定可以继续调大。
VOLUME_RMS_THRESHOLD = 450       # 音量触发阈值。这个数值经过了几轮固件修复才最终校准：
                                  # 1) /volume 原来只采样 100ms 就报数，每 3s 轮询一次相当于
                                  #    只有 3.3% 的时间在"听"，几乎不可能刚好盖住说话的瞬间——
                                  #    改成整整 1 秒的采样窗口。
                                  # 2) 每次录音在调 M5.Mic.end() 前的最后一块缓冲区读到的是固定
                                  #    的垃圾数据（不同时间录到的值完全一样，明显不是真实声音），
                                  #    多录一小段扔掉来避开这块脏数据。
                                  # 3) M5Unified 给 CoreS3 预设的麦克风增益（magnification）只有
                                  #    1-2，配合库内部再除以 4 的换算，等于把原始信号衰减到约
                                  #    0.5 倍而不是放大——在 setup() 里显式调到 5 才是效果比较
                                  #    合理的档位（调到 48 会削波）。
                                  # 后来实机连续观察到的安静环境 rms 基线其实在 280-367 附近
                                  # （比早期测得的 50-160 更高，可能和当时的环境噪音有关），
                                  # 而说话触发时能冲到 778 左右——600 相对基线的安全边际偏小，
                                  # 调到 450 留出更明确的余量，同时依然明显高于安静基线。
# --- 完整对话链路 ---
CURIOUS_RECORD_SECONDS = 5       # 好奇状态下录真正问题的时长。原来 3-4 秒
                                  # 经常在用户反应过来、开口说完整句话之前就
                                  # 结束了，只录到半句话，延长到 5 秒。
CURIOUS_PRE_RECORD_DELAY_SEC = 1.0   # 进 CURIOUS、切好奇表情之后到真正开始
                                  # 录音之间的固定等待，给用户一个"小狗注意到
                                  # 我了，可以开始说了"的反应缓冲（原来 0.5 秒
                                  # 太短，用户经常还没反应过来表情已经切换完，
                                  # 加长到 1 秒）
KEYWORD_GAP_SEC = 0.5            # qa_complex 逐个念关键词，两个关键词之间的间隔
BUTTON_PRESS_MS = 200            # 每个关键词播放前，按钮"按下"状态维持的时长

# --- 字幕：分两段出现，帮用户确认小狗"听懂了什么"——①录音期间显示麦克风
#     图标，表示"在听"；②录音结束、语音识别出结果后，把识别到的文字显示
#     出来，方便用户确认输入内容，一直保留到 LLM 回复出来、即将执行下一步
#     动作时才清空。注意 SenseVoice 是整段录完才能出结果，不是逐字流式识别，
#     所以这里做不到"边说边出字幕"，只能做到"识别完成的那一刻立刻显示"——
#     架构上要支持真正的逐字实时字幕，需要把 /record 改成边录边传的流式接口，
#     这里先不做。qa_complex 播报关键词期间用的是爪印按钮（见
#     speak_keywords()），不是字幕；其它状态只切表情，不显示文字。 ---
SUBTITLE_DUR_MS = 15000  # /speech 的 dur 参数：字幕展示上限（两个场景共用）。
                          # 正常情况下都会有显式的清空调用，这个只是兜底上限，
                          # 避免万一清空请求丢了导致字幕卡住不消失

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

SYSTEM_PROMPT = """你是一只可爱的电子小狗，名叫 StackChan。
你需要理解用户说的话，然后在回答末尾用 JSON 标注意图和关键词。

规则：
1. 先用一句简短可爱的话回应用户（像小狗一样热情）
2. 然后输出 JSON，格式如下：

如果是可以用是/否回答的问题：
{"type": "qa_simple", "answer": "yes" 或 "no"}

如果是开放式问题，提取回答中最重要的3个关键词（只用名词和动词，不要语气词、助词、形容词）：
{"type": "qa_complex", "keywords": ["关键词1", "关键词2", "关键词3"]}

如果用户在表扬你：
{"type": "praise"}

如果用户在责备你：
{"type": "scold"}

其他情况，也提取3个关键词（只用名词和动词，不要语气词、助词、形容词）：
{"type": "other", "keywords": ["关键词1", "关键词2", "关键词3"]}

示例：
用户: 今天天气怎么样？
今天天气超好，适合散步！
{"type": "qa_complex", "keywords": ["天气", "好", "散步"]}

用户: 你吃饭了吗？
还没吃呢！
{"type": "qa_simple", "answer": "no"}

用户: 你真棒！
谢谢夸奖！
{"type": "praise"}"""


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
    """调用固件的 /led 控制机身 LED。off=True 时忽略 r/g/b 直接关灯。"""
    if off:
        api_get("/led?off=1")
    else:
        api_get(f"/led?r={r}&g={g}&b={b}")

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

def get_volume():
    # /volume now captures a full 1s of audio on the device before replying
    # (see firmware.ino handleVolume()), so give it more headroom than the
    # old 100ms-capture version needed.
    r = api_get("/volume", timeout=5)
    if r and r.status_code == 200:
        try:    return r.json()
        except: return None
    return None

def capture_frame():
    r = api_get("/camera")
    if r and r.status_code == 200:
        arr = np.frombuffer(r.content, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return None

def record_audio(seconds):
    """调用 /record?seconds=N，返回 WAV 字节，失败返回 None。led=0 关掉固件
    自带的录音指示灯（会用暗一点的绿色自动开关），改由主机侧的 set_led()
    统一控制灯光节奏，避免两边抢着切换颜色。"""
    r = api_get(f"/record?seconds={seconds}&led=0", timeout=seconds + RECORD_TIMEOUT_MARGIN)
    if r is None or r.status_code != 200:
        print(f"  [录音] 失败: {r.status_code if r else '无响应'}")
        return None
    return r.content


# ╔══════════════════════════════════════════════╗
# ║               动画函数                        ║
# ╚══════════════════════════════════════════════╝

def play_happy_animation():
    set_expression("happy")
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
    for _ in range(EXCITED_CYCLES):
        move_servo(yaw=EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_HIGH, speed=EXCITED_YAW_SPEED)
        time.sleep(EXCITED_CYCLE_DELAY)
        move_servo(yaw=-EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_LOW, speed=EXCITED_YAW_SPEED)
        time.sleep(EXCITED_CYCLE_DELAY)
    move_servo(yaw=0, pitch=450, speed=400)

def play_sleepy_animation():
    set_expression("sleepy")
    move_servo(yaw=0, speed=200)
    time.sleep(0.2)
    for p in SLEEPY_PITCH_STEPS:
        move_servo(pitch=p, speed=SLEEPY_SPEED)
        time.sleep(SLEEPY_STEP_DELAY)

def play_privacy_animation():
    set_expression("privacy")
    move_servo(yaw=PRIVACY_YAW, pitch=PRIVACY_PITCH, speed=PRIVACY_SPEED)

def play_idle_animation():
    set_expression("idle")
    go_home()

def play_curious_animation():
    """好奇：显示表情即可，真正的录音由 run_conversation_turn() 触发。"""
    set_expression("curious")

def play_thinking_animation():
    """思考：显示表情即可，持续时长就是 STT+LLM 实际处理耗时。"""
    set_expression("thinking")

def play_sorry_animation():
    set_expression("sorry")
    move_servo(pitch=SORRY_PITCH, yaw=SORRY_YAW, speed=SORRY_SPEED)

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


def reorder_keywords_nouns_first(keywords):
    """把 LLM 提取的关键词重排成"名词全部在前、动词全部在后"，组内保持原有
    相对顺序（Python sorted() 是稳定排序）。用 jieba 词性标注判断每个关键词
    是名词还是动词：取该词切分后第一个分词的词性，只要不是以 'v' 开头就归为
    名词一类——SYSTEM_PROMPT 里已经要求关键词只能是名词或动词，不需要处理
    第三种词性。"""
    import jieba.posseg as pseg

    def is_verb(word):
        for _, flag in pseg.cut(word):
            return flag.startswith("v")
        return False

    return sorted(keywords, key=is_verb)


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

        # 语音唤醒
        self.last_volume_poll = 0

        # 主循环计数（用于轮流轮询 + 心跳打印）
        self.tick_count = 0

        # 语音链路：启动时一次性预加载，避免每次对话都重新加载模型。
        # 模型首次运行会自动从 ModelScope 下载并缓存到本地，之后启动就是直接
        # 加载缓存，不会重复下载。
        print("[引擎] 预加载 SenseVoice 模型...")
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        self._rich_transcription_postprocess = rich_transcription_postprocess
        self.asr_model = AutoModel(
            model=SENSEVOICE_MODEL,
            vad_model=SENSEVOICE_VAD_MODEL,
            vad_kwargs={"max_single_segment_time": SENSEVOICE_VAD_MAX_SEGMENT_MS},
            device=SENSEVOICE_DEVICE,
            disable_update=True,
        )
        print("[引擎] SenseVoice 就绪")

        self.deepseek_api_key = load_deepseek_api_key()
        if self.deepseek_api_key:
            print("[引擎] DeepSeek API key 已从 .env 加载")
        else:
            print("[引擎] [警告] 没有 DeepSeek API key，对话链路里的 LLM 调用会失败")

        ensure_audio_server()

        print("[引擎] 小狗行为引擎 v4 启动！")
        print(f"[引擎] 当前状态: {self.state.value}")
        print("[引擎] 轻点头顶 → 扫描找人")
        print("[引擎] 长按头顶(1秒) → 兴奋")
        print(f"[引擎] 音量突增 (rms >= {VOLUME_RMS_THRESHOLD}) → 扫描找人 → 好奇聆听 → 思考 → 回应")
        print("[引擎] Ctrl+C 退出\n")

    # ---------- 状态转移 ----------

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
        """短按 vs 长按检测。返回 'short_tap' / 'long_press' / None。"""
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
        result = None
        if not pressed and self.touch_pressed:
            duration = now - self.touch_press_start
            if duration >= LONG_PRESS_THRESHOLD:
                print(f"[触摸] 长按松开（{duration:.1f}s）")
                result = "long_press"
            else:
                print(f"[触摸] 短按松开（{duration:.1f}s）")
                result = "short_tap"
            self.record_interaction()

        self.touch_pressed = pressed
        return result

    # ---------- 语音唤醒 + 完整对话链路 ----------

    def transcribe(self, wav_bytes, filename):
        """把一段 WAV 字节数据识别成文字（复用预加载好的 SenseVoice 模型）。
        AutoModel 初始化时挂了 fsmn-vad（见 __init__），会先做语音活动检测，
        只把真正检测到人声的片段送进 SenseVoice——没有人声时 generate() 直接
        不返回文本，不会像纯靠主模型自己判断"有没有在说话"那样，在纯噪音/
        静音输入上瞎编一段结果出来。"""
        wav_path = AUDIO_DIR / filename
        wav_path.write_bytes(wav_bytes)
        res = self.asr_model.generate(
            input=str(wav_path), language=SENSEVOICE_LANGUAGE, use_itn=True,
        )
        if not res or not res[0].get("text"):
            return ""
        return self._rich_transcription_postprocess(res[0]["text"]).strip()

    def check_voice_wake(self):
        """轮询 /volume；音量超过阈值直接扫描找人→开心→进入好奇开始问题录音
        （不再做单独的唤醒词校验录音——是不是噪音由 run_conversation_turn()
        里的识别结果是否为空来判断）。返回 True 表示这次 tick 已经被这套流程占用。"""
        now = time.time()
        if now - self.last_volume_poll < VOLUME_POLL_SEC:
            return False
        self.last_volume_poll = now

        vol = get_volume()
        if vol is None:
            return False
        rms = vol.get("rms", 0)
        print(f"[唤醒] rms={rms:.0f} (阈值={VOLUME_RMS_THRESHOLD})")
        if rms < VOLUME_RMS_THRESHOLD:
            return False

        print(f"[唤醒] 音量突增 (rms={rms:.0f})，扫描找人...")
        self.record_interaction()
        if self.scan_for_face():
            self.enter_happy()
            time.sleep(0.3)
            self.run_conversation_turn()
        return True

    def run_conversation_turn(self):
        """run_conversation_turn() 内部涉及好几层网络调用（DeepSeek、edge-tts、
        StackChan 的 HTTP 接口），任何一层偶尔抛出的未预料异常（比如 DeepSeek
        返回了不含 choices 字段的响应体）以前都不会被 run() 主循环之外的任何
        地方接住——while True 主循环只 catch KeyboardInterrupt，一旦这里抛出
        异常整个引擎进程就直接崩溃退出，之后不管是语音、触摸还是人脸检测全部
        失灵，表现出来就是"只有第一次对话能成功，后续再也不触发"。这里包一层
        try/except 确保对话链路里出的任何问题都只是这一轮对话失败、退回安全
        状态，不会打死整个引擎；finally 里按用户要求打印一行确认状态机确实
        恢复了、下一轮 tick 会继续轮询 /volume。"""
        try:
            self._run_conversation_turn_body()
        except Exception as e:
            print(f"[对话] [异常] run_conversation_turn 出错，回退到安全状态: {e!r}")
            if self.face_detected:
                self.enter_happy()
            else:
                self.transition(State.IDLE)
        finally:
            # 保证整轮对话结束后灯一定是关的，不依赖某个具体分支有没有记得关——
            # 万一中间哪一步在开灯之后、关灯之前抛了异常，这里兜底收尾。
            set_led(off=True)
            print(f"[对话] 对话结束，回到状态={self.state.value}，恢复音量监听")

    def _run_conversation_turn_body(self):
        """完整的一次语音交互：好奇录音 → 思考(STT+LLM) → 按意图四路分支应对。
        全程用 track_ok 记录"这轮对话期间人脸追踪是否一直成功"，成功的话结束
        后就不需要再重新 scan_for_face() 一次——详见 _settle_happy()。"""
        track_ok = True

        # 音量突增触发后先闪两下白灯再常亮，跟切好奇表情同步——灯光比表情变化
        # 更显眼，让用户第一时间意识到小狗有反应了。闪烁=开→关→开（两次
        # "开"调用中间夹一次"关"），最后停在常亮状态。
        set_led(255, 255, 255)
        time.sleep(0.15)
        set_led(off=True)
        time.sleep(0.15)
        set_led(255, 255, 255)

        self.transition(State.CURIOUS)
        # 两阶段录音：先切好奇表情让用户知道小狗注意到了，固定等待
        # CURIOUS_PRE_RECORD_DELAY_SEC 给一个反应缓冲，再真正开始录音——之前
        # 进 CURIOUS 后立刻开录（中间还夹了一次人脸追踪的网络往返，耗时不固定），
        # 经常在用户反应过来、开口说完整句话之前就把开头吃掉了，只录到半句话。
        # 这里不在开录前做人脸追踪，就是为了让"表情切换→开始录音"之间的等待
        # 是固定可预期的 CURIOUS_PRE_RECORD_DELAY_SEC 秒，不被追踪请求的耗时
        # 拖长；追踪挪到录音结束后（不影响开录时机）做一次，具体在下面。
        time.sleep(CURIOUS_PRE_RECORD_DELAY_SEC)

        # 开始录音时切成绿灯，示意"可以说话了"；屏幕上同时显示麦克风图标，
        # 表示小狗在听。dur 只是兜底上限（避免下面的显式清空请求万一丢了导致
        # 卡住），真正的消失时机是下面进 THINKING 前的显式 set_subtitle("")。
        set_led(0, 255, 0)
        set_subtitle("🎤", dur_ms=SUBTITLE_DUR_MS)
        print("[对话] 录音中...")
        wav_bytes = record_audio(CURIOUS_RECORD_SECONDS)
        if not wav_bytes:
            print("[对话] 录音失败")
            set_led(off=True)
            set_subtitle("")
            self._settle_happy(track_ok)
            return

        # CURIOUS 整个持续时间都在阻塞式地调 /record，StackChan 的 WebServer
        # 是单线程的，这期间无法响应 /camera，所以人脸追踪放在录音刚结束、
        # 进 THINKING 之前做一次，保证每轮对话至少有一次追踪判断。
        if not self.track_face_once():
            track_ok = False

        set_led(off=True)
        # 语音输入到这里才算确定结束（即将进入思考），麦克风图标字幕在这一刻
        # 清空——不再靠录音时长倒推的定时器，是真正跟"结束"这个事件对齐的
        # 显式调用。
        set_subtitle("")
        self.transition(State.THINKING)
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

        else:  # qa_complex / other：逐个念关键词
            keywords = data.get("keywords") or [reply_text[:6]]
            keywords = reorder_keywords_nouns_first(keywords)
            print(f"[对话] 复杂回应，播报关键词（名词优先）: {keywords}")
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
        按钮先"按一下"（down 保持 BUTTON_PRESS_MS 后弹回 up，固件那边会把这
        一段渲染成缩小再放大的动画），关键词音频要在按钮"放大"的同一刻开始
        播——如果像之前那样等按钮弹回 up 以后才现合成 TTS，合成本身的网络
        延迟（几百毫秒到一两秒不等）会让音频比放大动画晚很多才响，两者根本
        对不上。所以改成提前一步合成：进循环前先把第 1 个词的音频备好，每播
        完一个词、趁按钮消失前的间隔时间顺手把下一个词也合成好，这样每次轮
        到按钮弹起时音频总是已经就绪，可以立刻播放。全部念完后按钮消失。这
        是全系统唯一用到按钮的场景。"""
        set_button("up")
        next_wav = tts_to_wav(keywords[0], "kw_0") if keywords else None
        for i in range(len(keywords)):
            set_button("down")
            time.sleep(BUTTON_PRESS_MS / 1000)
            set_button("up")
            wav_path = next_wav
            if wav_path:
                play_wav_file(wav_path)
            if i + 1 < len(keywords):
                next_wav = tts_to_wav(keywords[i + 1], f"kw_{i + 1}")
            time.sleep(KEYWORD_GAP_SEC)
        set_button("off")

    # ---------- 计时器 ----------

    def idle_seconds(self):
        return time.time() - self.last_interaction

    def state_duration(self):
        return time.time() - self.state_enter_time

    # ---------- 主循环 ----------

    def tick(self):
        self.tick_count += 1
        print(f"[循环] tick #{self.tick_count}, 状态={self.state.value}")

        # 触摸/音量(语音唤醒)/人脸 三者轮流，每一轮 tick 只发起其中一种会打
        # HTTP 请求的轮询，避免同一轮里对 StackChan 连打三个请求。
        poll_slot = self.tick_count % 3   # 0=触摸 1=音量 2=人脸

        # --- 触摸（最高优先级）---
        touch = self.check_touch() if poll_slot == 0 else None

        if touch == "long_press":
            print("[触发] 长按 → 兴奋！")
            self.transition(State.EXCITED)
            return

        if touch == "short_tap":
            if self.state in (State.IDLE, State.SLEEPY, State.PRIVACY):
                print("[触发] 短按 → 扫描找人")
                if self.scan_for_face():
                    self.enter_happy()
            elif self.state == State.HAPPY:
                print("[触发] 短按（已在开心，跳过扫描）")
            return

        # --- 语音唤醒（好奇/思考期间已经在处理语音了，不重复轮询）---
        if poll_slot == 1 and self.state not in (State.CURIOUS, State.THINKING):
            if self.check_voice_wake():
                return

        # --- 状态内行为 ---
        # 人脸检测（会发 /camera 请求）只在轮到 poll_slot==2 时真正执行；
        # 计时器类判断（空闲/困倦/兴奋持续时间）不发请求，每轮都可以正常判断。
        do_face_check = (poll_slot == 2)

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
            if do_face_check:
                self.check_face()
            if self.face_detected:
                print("[触发] 隐私中检测到人脸！")
                self.enter_happy()

        elif self.state == State.SORRY:
            # 表情映射v6.xlsx：抱歉状态只由新的语音唤醒打断（上面已经处理），
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
            print("[引擎] 已退出。")


if __name__ == "__main__":
    PuppyEngine().run()
