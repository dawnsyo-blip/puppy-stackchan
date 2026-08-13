"""
StackChan 小狗行为引擎 v4
========================
新增内容 (v3 → v4)：
- 整合 voice_test.py 的语音链路：Whisper STT（启动时预加载一次）、DeepSeek LLM
  （API key 从项目根目录 .env 手动解析，不读环境变量）、edge-tts 语音合成、
  HTTP 音频服务器播放。
- 新增 CURIOUS（好奇/录音中）、THINKING（思考/LLM处理中）、SORRY（抱歉）三个状态。
- 关键词唤醒（方案A）：主循环持续轮询 /volume，音量突增时录 3 秒做唤醒词校验，
  Whisper 识别到"小狗"后扫描找人 → HAPPY → 进入 CURIOUS 开始真正的问题录音。
- 完整对话链路：CURIOUS(录音4s) → THINKING(STT+LLM，四路意图分支) →
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
    pip install faster-whisper edge-tts pydub
ffmpeg 需要在系统 PATH 里（pydub 转 WAV 要用）。

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

# --- 语音唤醒（方案A：音量触发 + Whisper 校验）---
VOLUME_POLL_SEC = 3.0            # 轮询 /volume 的间隔。实测 /volume 每次调用都会启停一次
                                  # 麦克风(I2S)，持续高频调用即使 2s 一次也偶尔会让设备重启，
                                  # 所以留了更大的余量；如果还不稳定可以继续调大。
VOLUME_RMS_THRESHOLD = 22000     # 音量触发阈值，需要根据实际环境噪音调（可以先手动
                                  # 轮询 /volume 看安静时 rms 大概多少，再定这个值）
WAKE_RECORD_SECONDS = 3          # 音量触发后，先录这么久做唤醒词校验
WAKE_WORDS = ["小狗", "xiǎo gǒu", "xiao gou"]

# --- 完整对话链路 ---
CURIOUS_RECORD_SECONDS = 4       # 好奇状态下录真正问题的时长
KEYWORD_GAP_SEC = 0.5            # qa_complex 逐个念关键词，两个关键词之间的间隔

# --- 语音识别 (Whisper) ---
WHISPER_MODEL_PATH = "C:/Users/89823/whisper-small"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_LANGUAGE = "zh"

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

def get_volume():
    r = api_get("/volume", timeout=3)
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
    """调用 /record?seconds=N，返回 WAV 字节，失败返回 None。"""
    r = api_get(f"/record?seconds={seconds}", timeout=seconds + RECORD_TIMEOUT_MARGIN)
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

    full_reply = r.json()["choices"][0]["message"]["content"].strip()
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

        # 触摸检测
        self.last_touch_poll = 0
        self.touch_pressed = False
        self.touch_press_start = 0

        # 语音唤醒
        self.last_volume_poll = 0

        # 主循环计数（用于轮流轮询 + 心跳打印）
        self.tick_count = 0

        # 语音链路：启动时一次性预加载，避免每次对话都重新加载模型
        print("[引擎] 预加载 Whisper 模型...")
        from faster_whisper import WhisperModel
        self.whisper_model = WhisperModel(
            WHISPER_MODEL_PATH, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE
        )
        print("[引擎] Whisper 就绪")

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
        print(f"[引擎] 说出唤醒词 {WAKE_WORDS} → 好奇聆听 → 思考 → 回应")
        print("[引擎] Ctrl+C 退出\n")

    # ---------- 状态转移 ----------

    def transition(self, new_state):
        old = self.state
        if old == new_state:
            return
        print(f"[转移] {old.value} → {new_state.value}")
        self.state = new_state
        self.state_enter_time = time.time()

        if   new_state == State.HAPPY:    play_happy_animation()
        elif new_state == State.EXCITED:  play_excited_animation()
        elif new_state == State.SLEEPY:   play_sleepy_animation()
        elif new_state == State.PRIVACY:  play_privacy_animation()
        elif new_state == State.IDLE:     play_idle_animation()
        elif new_state == State.CURIOUS:  play_curious_animation()
        elif new_state == State.THINKING: play_thinking_animation()
        elif new_state == State.SORRY:    play_sorry_animation()

    def record_interaction(self):
        self.last_interaction = time.time()

    # ---------- 人脸检测 ----------

    def detect_face_once(self):
        """拍一帧检测人脸。"""
        img = capture_frame()
        if img is None:
            return False
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        results = self.face_detector.detect(mp_img)
        return len(results.detections) > 0

    def check_face(self):
        """带连续确认的人脸检测。"""
        now = time.time()
        if now - self.last_face_check < FACE_CHECK_INTERVAL_SEC:
            return
        self.last_face_check = now

        found = self.detect_face_once()

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
        """开心状态下定期回正重新检测人脸。"""
        now = time.time()
        if now - self.last_retrack_time < FACE_RETRACK_INTERVAL_SEC:
            return
        self.last_retrack_time = now

        # 回到正面位置拍照
        move_servo(yaw=0, pitch=450, speed=300)
        set_expression("happy")
        time.sleep(0.5)

        if self.detect_face_once():
            self.last_face_seen_time = now
            self.face_detected = True
            self.record_interaction()
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
            if self.detect_face_once():
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
        """把一段 WAV 字节数据识别成文字（复用预加载好的 Whisper 模型）。"""
        wav_path = AUDIO_DIR / filename
        wav_path.write_bytes(wav_bytes)
        segments, _ = self.whisper_model.transcribe(str(wav_path), language=WHISPER_LANGUAGE)
        return "".join(seg.text for seg in segments).strip()

    def check_voice_wake(self):
        """轮询 /volume；音量超过阈值时录音校验唤醒词，命中后扫描找人→开心→
        进入好奇开始真正的问题录音。返回 True 表示这次 tick 已经被这套流程占用。"""
        now = time.time()
        if now - self.last_volume_poll < VOLUME_POLL_SEC:
            return False
        self.last_volume_poll = now

        vol = get_volume()
        if vol is None or vol.get("rms", 0) < VOLUME_RMS_THRESHOLD:
            return False

        print(f"[唤醒] 音量突增 (rms={vol.get('rms', 0):.0f})，录音校验唤醒词...")
        wav_bytes = record_audio(WAKE_RECORD_SECONDS)
        if not wav_bytes:
            return True

        text = self.transcribe(wav_bytes, "wake_check.wav")
        print(f"[唤醒] 校验录音识别: 「{text}」")
        if not any(w in text for w in WAKE_WORDS):
            print("[唤醒] 未包含唤醒词，忽略")
            return True

        print("[唤醒] 唤醒词命中！扫描找人...")
        self.record_interaction()
        if self.scan_for_face():
            self.transition(State.HAPPY)
            time.sleep(0.3)
            self.run_conversation_turn()
        return True

    def run_conversation_turn(self):
        """完整的一次语音交互：好奇录音 → 思考(STT+LLM) → 按意图四路分支应对。"""
        self.transition(State.CURIOUS)
        wav_bytes = record_audio(CURIOUS_RECORD_SECONDS)
        if not wav_bytes:
            print("[对话] 录音失败")
            self.transition(State.HAPPY if self.face_detected else State.IDLE)
            return

        self.transition(State.THINKING)
        user_text = self.transcribe(wav_bytes, "question.wav")
        print(f"[对话] 识别结果: 「{user_text}」")
        if not user_text:
            print("[对话] 没识别到内容")
            self.transition(State.HAPPY if self.face_detected else State.IDLE)
            return

        reply_text, intent, data = ask_llm(user_text, self.deepseek_api_key)
        if reply_text is None:
            print("[对话] LLM 调用失败")
            self.transition(State.HAPPY if self.face_detected else State.IDLE)
            return

        print(f"[对话] 回复:「{reply_text}」 意图: {intent}")
        self.record_interaction()

        if intent == "qa_simple":
            answer = data.get("answer", "no")
            self.transition(State.HAPPY)
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
            print(f"[对话] 复杂回应，播报关键词: {keywords}")
            self.transition(State.HAPPY)
            self.speak_keywords(keywords)

    def speak_keywords(self, keywords):
        """依次合成并播放每个关键词，关键词之间间隔 KEYWORD_GAP_SEC。"""
        for i, kw in enumerate(keywords):
            wav_path = tts_to_wav(kw, f"kw_{i}")
            if wav_path:
                play_wav_file(wav_path)
            time.sleep(KEYWORD_GAP_SEC)

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
                    self.transition(State.HAPPY)
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
                self.transition(State.HAPPY)
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
                self.transition(State.HAPPY if self.face_detected else State.IDLE)

        elif self.state == State.SLEEPY:
            if do_face_check:
                self.check_face()
            if self.face_detected:
                print("[触发] 困倦中检测到人脸！")
                self.transition(State.HAPPY)
            elif self.state_duration() > SLEEPY_TO_PRIVACY_SEC:
                print("[触发] 困倦超过 10 分钟 → 隐私")
                self.transition(State.PRIVACY)

        elif self.state == State.PRIVACY:
            if do_face_check:
                self.check_face()
            if self.face_detected:
                print("[触发] 隐私中检测到人脸！")
                self.transition(State.HAPPY)

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
