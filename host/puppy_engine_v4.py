"""
StackChan 小狗行为引擎 v4
========================
新增内容 (v3 → v4)：
- 整合 voice_test.py 的语音链路：FunASR SenseVoice STT（启动时预加载一次）、
  DeepSeek LLM（API key 从项目根目录 .env 手动解析，不读环境变量）、animalese
  拟声词合成（纯本地计算，不可懂，见 tts_to_wav()/set_subtitle() 配套的字幕
  联动）、HTTP 音频服务器播放。
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
    pip install funasr torch torchaudio pypinyin sounddevice scipy
animalese.wav 字母音频库首次运行会自动从 GitHub 下载并缓存到 host/ 目录
（~172KB），不需要手动下载；edge-tts/pydub/ffmpeg 这三个依赖随着 TTS 换成
animalese 已经不再需要。
语音输入现在走电脑本地无线麦克风（sounddevice 采集，见 MicStream 类），不
再依赖 StackChan 机身麦克风——Windows 需要能看到一个名字包含
WIRELESS_MIC_NAME_HINT 的输入设备（无线麦克风接收器），否则语音唤醒不可用。
SenseVoiceSmall / fsmn-vad 模型首次运行会自动从 ModelScope 下载并缓存，
如果下载失败/很慢，可以先设 HF_ENDPOINT=https://hf-mirror.com 再运行。

用法: python host/puppy_engine_v4.py
退出: Ctrl+C
"""

import re
import requests
import time
import sys
import json
import base64
import random
import asyncio
import tempfile
import threading
import http.server
import socket
import queue
import wave
import io
import math
import urllib.request
from pathlib import Path
from collections import deque
from enum import Enum
from datetime import datetime, timedelta
from urllib.parse import quote

import numpy as np
import cv2
import mediapipe as mp
from mediapipe.tasks.python import vision
from mediapipe.tasks.python import BaseOptions
import sounddevice as sd
from scipy.signal import resample_poly


# ╔══════════════════════════════════════════════╗
# ║            可调参数（调参改这里）             ║
# ╚══════════════════════════════════════════════╝

# 电脑和 StackChan 都改接手机热点后，这两个值要跟着换：BASE_URL 换成 StackChan
# 开机屏幕上打印出来的实际 IP（固件已改成 DHCP，不再是固定的 .100）；
# COMPUTER_IP 换成电脑在手机热点这个网络上的 IP（连上后用 ipconfig 查）。
BASE_URL = "http://192.168.137.100"
COMPUTER_IP = "192.168.137.1"   # 电脑在热点网络上的 IP（StackChan 用它来下载要播放的音频，
                                 # 也是 /stream 推流要连过来的地址）
AUDIO_SERVER_PORT = 8090  # 8080 会被本机的沙盒代理/VPN 工具（Jamjams）保留，bind 会报
                           # WinError 10013（拒绝访问，不是端口占用），换一个不常见端口即可
TIMEOUT = 5                     # 普通请求（/face、/servo、/touch）超时
FACE_BG_TIMEOUT_SEC = 1.5       # check_face()/retrack_face() 改成后台线程之后
                                 # 新增的超时——这两个后台 worker 的 /camera 请求
                                 # 走的是跟主线程共用的 _device_lock（见那把锁
                                 # 声明处的注释），如果这次请求卡住不响应，会一直
                                 # 攥着锁到超时才放手，期间主线程哪怕只是想发一个
                                 # /face?expr=... 也要陪着等——实测触摸反应"还是
                                 # 好久"，等的时长（~5s）跟普通请求的 TIMEOUT(5s)
                                 # 对得上，就是这个机制在作祟。后台检测本来就是
                                 # "错过一次也无所谓"的低优先级任务（下次
                                 # FACE_CHECK_INTERVAL_SEC/FACE_RETRACK_
                                 # INTERVAL_SEC 后还会再试），没必要占着锁等满
                                 # 5 秒，换成短得多的超时、且不重试（见
                                 # _check_face_worker()/_retrack_face_worker()），
                                 # 把"背景检测偶尔卡顿拖累前台触摸反应"这个最坏
                                 # 情况从 ~5s（甚至加上重试的 ~12s）压到 ~1.5s。
PLAY_TIMEOUT = 30               # /play 本身现在是非阻塞的（固件后台任务播放，
                                 # 见 firmware.ino 的 playTaskFn()），这个超时是
                                 # PuppyEngine.wait_for_playback() 轮询等自然
                                 # 播完的兜底上限，不是单次 HTTP 请求的超时
API_RETRY_DELAY_SEC = 2.0       # API 请求失败（连不上/超时）后，等这么久再重试一次，
                                 # 不要立刻重试，避免在设备本来就吃紧时继续加压

MAIN_LOOP_INTERVAL_SEC = 0.2    # 主循环 tick() 间隔——原来是 0.5s，从触摸到
                                 # 反应动作激活的延迟主要就取决于这个数字（见
                                 # TOUCH_POLL_SEC 旁边的说明）。调低以后其它
                                 # 轮询没有跟着变频繁：人脸检测仍然由自己的
                                 # FACE_CHECK_INTERVAL_SEC/FACE_RETRACK_
                                 # INTERVAL_SEC 内部节流（这两个值没变），
                                 # tick() 只是"有资格检查"的时机变密了，实际
                                 # 发不发请求还是那两个值说了算。
CAMERA_SLOW_WARN_SEC = 0.8       # 单次 /camera 请求（含排队等待锁的时间）
                                  # 超过这个值就打印警告——独立用 curl 连续
                                  # 测过 /camera，稳定在 ~0.5s，这里留了
                                  # 三成左右的余量再报警，避免正常波动也
                                  # 天天刷屏。
SLOW_TICK_WARN_SEC = 1.0        # 单次 tick() 实际耗时超过这个值就打印警告——
                                 # 排查"触摸反应慢"时，光看 `[循环] tick #N`
                                 # 的编号完全看不出真实耗时（编号只是数数，不
                                 # 代表每次间隔都接近 MAIN_LOOP_INTERVAL_SEC），
                                 # 之前排查"碰屏幕→贴贴延迟"几轮都卡在"到底是
                                 # 哪个环节慢"这个问题上，run() 里其实早就在算
                                 # 每次 tick() 的真实耗时（用来决定还要不要
                                 # 补睡），只是从来没有打印出来。1.0s 远大于
                                 # 正常 tick()（不涉及任何设备请求时几乎是 0，
                                 # 涉及一次 HTTP 请求时通常几十到几百毫秒），
                                 # 只有真的有什么阻塞了才会触发。

# --- 计时器（秒） ---
IDLE_TO_SLEEPY_SEC = 180
SLEEPY_TO_PRIVACY_SEC = 600
FACE_LOST_GRACE_SEC = 20        # 人脸消失后等多久才离开开心
EXCITED_DURATION_SEC = 6

# --- 触摸触发 ---
# 具体判断/处理逻辑见 PuppyEngine.handle_touch_trigger()。长按满这个时长
# 进/出隐私（同一个动作，按当前是否已经在隐私状态决定方向）。
PRIVACY_HOLD_SEC = 3.0

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

# --- "贴贴"动画参数：用开心表情，但舵机动作是轻微小幅度抬头，不是完整
#     摇头动画——碰屏幕、以及听到"小狗小狗"呼唤都会触发这个动作（见
#     enter_tietie()/play_tietie_animation()）---
TIETIE_PITCH_UP = 330      # 默认回正是 450，抬头幅度（第一版 400，反馈"可以调大一点"，
                                # 从偏移 50 加大到 120，仍然小于 HAPPY_PITCH(300) 偏移 150，
                                # 保持"比完整开心动作小"这个既定关系，只是更明显一点）
TIETIE_PITCH_DOWN = 450
TIETIE_SPEED = 300
TIETIE_CYCLES = 3
TIETIE_CYCLE_DELAY = 0.3

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

# --- 隐私→兴奋过渡（_face_person_before_excited()）---
# 双击头顶退出隐私时，需要先转回正对前方才能拍照定位人脸——PRIVACY_YAW=800
# 离正前方很远，转回来这段时间是用户能感觉到的等待，要尽量压缩：转速改用
# EXCITED_YAW_SPEED（兴奋动画同款已验证转速），比原来 go_home() 走的固件
# 默认归位速度快；settle 超时也跟着从原来的 3.0s 收紧——转速已经提上去了，
# 不需要再留那么多余量。拍照定位改用短超时+不重试（FACE_BG_TIMEOUT_SEC，
# 跟后台人脸检测 _check_face_worker() 同一套参数），不用默认 TIMEOUT(5s)+
# 重试那一套给"阻塞等待、必须等到结果"路径设计的参数——这里等的是用户能
# 看见的一次手势反馈，找不到人脸就直接跳过微调进兴奋，没必要死等。
FACE_PERSON_SETTLE_TOLERANCE = 30
FACE_PERSON_SETTLE_TIMEOUT_SEC = 1.2

# --- LED（对照表情映射v7.xlsx）---
# 固件的 /led 现在支持 mode 参数（solid/blink/breathe/rainbow/fade），呼吸/
# 闪烁/彩虹/渐暗这些需要"持续"播放的效果由固件自己在本地 loop() 里驱动（见
# firmware.ino 的 updateLed()）。host 端只在状态切换时调一次 /led 告诉固件
# "从现在起用哪种模式"，不需要开一个后台线程持续轮询去模拟——早期版本试过
# host 端连续调用 /led 模拟隐私状态的渐暗效果，相当于给这个本不该被高频调用
# 的接口硬造出一次 /volume 当年那种轮询（CLAUDE.md 里记过那次教训：碎片化
# 堆，最后设备反复重启），所以改成了现在这个"固件本地驱动"的架构。
WARM_WHITE_RGB = (255, 180, 90)       # 呼吸灯/闪烁/渐暗/常亮统一用这个暖白色调
THINKING_GREEN_RGB = (0, 200, 0)      # 好奇（识别语音）/思考共用的绿色呼吸灯，
                                       # 跟字幕是同一个"正在处理"窗口的两个信号
CURIOUS_LED_BREATHE_PERIOD_MS = 1600  # 好奇/思考共用的呼吸灯周期
SORRY_LED_PERIOD_MS = 1500            # 抱歉"缓慢闪烁"
EXCITED_LED_PERIOD_MS = 300           # 兴奋"彩虹快闪"（具体颜色表内置在固件里）
SLEEPY_LED_FADE_MS = 5000             # 困倦"渐暗至熄灭"
PRIVACY_LED_FADE_MS = 2000            # 隐私"渐暗至熄灭"
DIZZY_LED_RGB = (150, 120, 255)       # 晕：淡紫色呼吸灯，没有实机验证过，先跟其它
                                       # 状态的颜色区分开
DIZZY_LED_BREATHE_PERIOD_MS = 1200    # 晕呼吸灯周期
DIZZY_LINGER_SEC = 2.0                # 摇晃/拿起信号消失后，"晕"表情还要
                                       # 继续停留这么久才转"兴奋"，不是信号
                                       # 一消失就立刻切走（第一版1.0s，反馈
                                       # 加长到2.0s）

# --- 装死(dead)动画参数 ---
DEAD_PITCH_DOWN = 80             # 低头角度，参考 CLAUDE.md 里 Y 轴舵机建议范围
                                  # 5~85°，用接近下限的保守值——这个值已经过
                                  # 实机端到端验证，低头视觉效果符合预期。
DEAD_PITCH_UP = 600              # 进入装死时先向上抬一点，再落到 DEAD_PITCH_DOWN
                                  # 定格（反馈要求加的过渡动作）。第一版复用
                                  # EXCITED_PITCH_HIGH(500)，反馈"幅度可以再大
                                  # 一点"后加到 600——这是本项目 pitch 轴目前
                                  # 用过的最大值（此前最大是 EXCITED_PITCH_HIGH
                                  # =500，已验证），600 没有单独实机验证过，
                                  # 硬件上限是 [0,900]（firmware handleServo()
                                  # 的 constrain()），留了不小的余量，但第一次
                                  # 测试还是要留意有没有卡顿/异响。
DEAD_PITCH_UP_SETTLE_TOLERANCE = 30   # 判定"已经抬到位"的容差
DEAD_PITCH_UP_SETTLE_TIMEOUT_SEC = 1.0  # 等舵机转到位的最长时间，超时就按
                                  # 当前角度继续，不卡死（跟 _settle_privacy_
                                  # mic() 是同一个轮询套路）
DEAD_PITCH_UP_HOLD_SEC = 0.3     # 转到位之后再停留多久才落下——太短会跟落下
                                  # 动作糊在一起看不出"先抬再落"，没有实机验证
                                  # 过具体值。
DEAD_SPEED = 300                 # 抬头/落下的舵机速度，第一版 100（跟 SORRY_
                                  # SPEED 同量级），反馈"可以再快一点"后加大。
                                  # 300 仍然低于 EXCITED_YAW_SPEED(500，本项目
                                  # pitch/yaw 轴用过的最快速度，已验证)，没有
                                  # 直接顶格用最快值，留了安全边际。
DEAD_LED_RGB = (255, 0, 0)       # 红色，没有实机验证过
DEAD_LED_BLINK_PERIOD_MS = 300
DEAD_LED_BLINK_HOLD_SEC = 0.6    # 闪两下红灯保持的时长，之后切渐灭
DEAD_LED_FADE_MS = 1500

# --- 手势扫描窗口（碰屏幕"贴贴"之后的互动期待期，检测"手指枪"触发装死）---
GESTURE_WINDOW_SEC = 15.0        # 窗口持续时间
GESTURE_POLL_SEC = 0.8           # 窗口期内摄像头轮询间隔——瓶颈是 /camera 本身
                                  # 的响应速度（200-500ms），不是 MediaPipe 推理
                                  # 速度（20-50ms），这个间隔留了余量，不会请求堆积
FINGER_GUN_CONFIRM_HITS = 2      # 最近 FINGER_GUN_WINDOW_FRAMES 帧里至少命中
                                  # 这么多次才真正触发，防止单帧误判
FINGER_GUN_WINDOW_FRAMES = 3     # 滑动窗口大小——原来是"必须连续命中"，用
                                  # host/gesture_test.py 实机测过才发现这个
                                  # 要求太脆：即使全程稳定比着手指枪，单帧
                                  # landmark 抖动也会让某一帧偶尔判不中（比如
                                  # 食指伸展比例卡在阈值附近来回跳），"连续"
                                  # 要求会因为这一次孤立的漏判把已经攒的进度
                                  # 清零，变成要重新连中 FINGER_GUN_CONFIRM_
                                  # HITS 次才行。改成"最近几帧里累计命中够
                                  # 次数"（窗口 3 帧、要求命中 2 帧），能容忍
                                  # 中间偶尔漏判一帧，不需要要求帧帧都对。
# 手指枪判定用的都是"距离比例"而不是绝对坐标差（原因见 classify_finger_
# gun_pose() 顶部的详细说明：绝对坐标差被实测证明对手离摄像头的距离/画面
# 里的旋转角度太敏感，同一个手势换个距离/角度判定结果会飘）。这几个比例
# 阈值是根据手部关键点的典型几何比例估的第一版，没有实机验证过具体是否
# 合适——host/gesture_test.py 诊断脚本会把这几个比例的实际数值都打印
# 出来，需要拿真实手势测过、看着实际比例数值再调。
FINGER_EXTEND_RATIO = 1.05       # 手指"伸展比例"（指尖到手腕距离 / PIP到手腕距离）
                                  # 大于这个值才算伸直，用于食指——原来是 1.2，
                                  # 改成 3D 距离（见 _landmark_dist()）之后用
                                  # gesture_test.py 又测了一轮，日志显示中指/
                                  # 无名指/小指弯曲、拇指张开这两个条件基本都
                                  # 稳定满足（3/3 弯、拇指张开），食指伸展比例
                                  # 却在 1.06~1.39 之间跨阈值来回跳，超过一半
                                  # 卡在 1.2 门槛以下——这不是随机噪声（噪声
                                  # 应该窄幅抖动），是这个用户/这个握持角度下
                                  # 食指真实伸展比例的中位数本来就落在 1.2
                                  # 附近，阈值定得比实际分布的中位数还高，
                                  # 天然只有不到一半的帧能过。降到 1.05（只
                                  # 略高于"指尖比PIP关节离手腕更远"这个最基础
                                  # 的判据，明显区别于真正弯曲手指典型的
                                  # 0.6~0.9），能把这次日志里那一大批"其它
                                  # 条件都满足、只被食指卡住"的帧转正，同时
                                  # 不会误伤"手掌完全张开"的帧（那类帧靠
                                  # curled_count 不足 2 就已经被排除，不依赖
                                  # 这个阈值）。
FINGER_CURL_RATIO = 0.9          # 手指"伸展比例"小于这个值才算弯曲，用于中指/
                                  # 无名指/小指——三根里至少两根弯曲就算数（不要求
                                  # 三根全弯，见 classify_finger_gun_pose() 的说明）
FINGER_GUN_THUMB_SPREAD_RATIO = 0.6  # 拇指指尖到食指根部的距离，相对手掌尺度
                                  # （手腕到中指根部的距离）的比例，大于这个值
                                  # 才算张开
HAND_MODEL_PATH = str(Path(__file__).resolve().parent / "hand_landmarker.task")
GESTURE_LED_BREATHE_PERIOD_MS = 2000  # 窗口期"等待手势"的绿色呼吸灯周期

# --- "再见"手势（挥手 / 五指捏住再放开）→ 委屈 → 隐私 ---
# 跟"手指枪"共用同一个手势扫描窗口、同一次 /camera 拍照 + Hand Landmarker
# 检测结果（check_gesture() 里一次检测后两种判定都跑一遍，不重复拍照/
# 推理），判定逻辑在 classify_open_pinch_pose()，同样是"距离比例"而不是
# 绝对坐标差（原因见 classify_finger_gun_pose() 顶部说明），同样没有实机
# 验证过——先给一版能跑的默认值，等实机测过挥手/捏放的真实手感再调。
FINGER_SPREAD_OPEN_RATIO = 1.15   # 食指/中指/无名指/小指四指的伸展比例都要
                                   # 超过这个值才算"张开手掌"——比手指枪判定
                                   # 单独一根食指用的 FINGER_EXTEND_RATIO(1.2)
                                   # 略宽松一点，因为这里要求四指同时达标，
                                   # 单指阈值定太高会让四指同时达标变得很苛刻
PINCH_TIP_SPREAD_RATIO = 0.35     # 五个指尖（拇指/食指/中指/无名指/小指）到
                                   # 它们质心的平均距离，相对手掌尺度的比例，
                                   # 小于这个值算"五指捏拢"
RELEASE_TIP_SPREAD_RATIO = 0.55   # 同一个比例，大于这个值算"（从捏拢）放开"。
                                   # 故意比 PINCH_TIP_SPREAD_RATIO 留出间隔而
                                   # 不用同一个阈值来回判——两个阈值挨得太近，
                                   # 手指停在临界值附近会来回抖动着重复触发
PINCH_RELEASE_TIMEOUT_SEC = 3.0   # 捏拢之后必须在这段时间内放开，否则这次
                                   # "捏拢"作废，不再等，需要重新捏一次才算数
WAVE_HISTORY_SEC = 6.0            # 挥手判定回看最近这么久、张开手掌时的手掌
                                   # 水平位置——GESTURE_POLL_SEC=0.8s 一帧，
                                   # 这个窗口内大约能采到 7~8 个样本
WAVE_MIN_SWINGS = 2               # 水平方向至少要反向摆动这么多次才算"挥手"，
                                   # 防止手掌只是举着不动、或单纯平移一次被误判
WAVE_MIN_AMPLITUDE_RATIO = 0.5    # 每次摆动的水平位移，相对手掌尺度的最小
                                   # 比例，太小的抖动/检测噪声不计入一次摆动
GOODBYE_GRIEVED_HOLD_SEC = 1.5    # "再见"手势确认后，"委屈"表情停留多久再
                                   # 转入隐私，给这个过渡表情留出被看清的时间

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

# --- 游戏：捉迷藏找物品 ---
# 玩法：人拿物品给小狗"看"一眼（拍照记颜色特征 + 可选 VLM 文字描述，识别
# 结果用关键词念出来），小狗低头闭眼倒数"5 4 3 2 1"给人时间藏好，倒数完
# 转头扫描房间，用颜色直方图相关度粗筛+可选视觉大模型精确确认判断有没有
# 找到。全程用关键词+按钮动画播报（跟 speak_keywords() 同一套按钮/AAC
# 风格，不用整句 TTS 旁白），整个过程是一次同步阻塞的交互（跟
# run_conversation_turn() 一样），tick() 主循环观察不到中间的子阶段，见
# PuppyEngine.play_game_hide_seek()。
GAME_SCAN_YAW_MIN = -800         # 复用 EXCITED_YAW_RANGE/PRIVACY_YAW 同量级的
GAME_SCAN_YAW_MAX = 800          # 安全幅度，不是设计稿里没验证过的 100~900
GAME_SCAN_YAW_JITTER = 120       # 每次扫描起点/终点在这个范围内随机抖动一点，
                                  # 不要每次路径都一模一样，同时不会因为抖动
                                  # 太大而漏掉边缘区域
# 扫描用"之"字形覆盖两个不同的俯仰角度（抬头一遍、低头一遍），而不是原来
# 固定在单一水平面扫——之前只扫一个 pitch，会导致右下角/桌面以下这类不在
# 那个水平带里的东西永远拍不到。300/500 都是这个项目里已经用过、验证过
# 安全的既有数值（HAPPY_PITCH=300、EXCITED_PITCH_HIGH=500），不是新猜的角度；
# 具体哪个数值对应"抬头"哪个对应"低头"没有实机验证过（这份表情映射表里
# pitch 数值和"抬头/低头"文字描述在别处也有过对不上的先例），如果实机看
# 起来方向反了，直接把这个列表顺序倒过来就行，不影响其它逻辑。
GAME_SCAN_PITCH_LEVELS = [300, 500]
GAME_SCAN_STEPS_PER_LEVEL = 6     # 每个俯仰层扫几步，两层加起来共 12 步，
                                  # 跟改版前单层 12 步的总耗时量级一致
GAME_SCAN_PAUSE_SEC = 1.0        # 每步转头后停留多久再拍照（等舵机到位+防抖）
GAME_SCAN_SPEED_MIN = 150        # 扫描起步速度，比常规的 SCAN_SPEED(300) 更
                                  # 从容——不能一下子转太快就找到，也给人多一点
                                  # 悬念
GAME_SCAN_SPEED_MAX = 450        # 扫描末段的最快速度，仍然低于这个项目里
                                  # 用过的最快值（EXCITED_YAW_SPEED=500，兴奋
                                  # 摇摆），确保"从慢到快"最终也落在安全范围内
GAME_SCAN_TIMEOUT_SEC = 60       # 扫描总耗时上限，超过就算超时没找到
GAME_HIST_THRESHOLD = 0.35       # 颜色直方图相关度阈值——初始猜测值，需要
                                  # 实机测试调整，误报多就调高，漏检多就调低
GAME_VLM_TIMEOUT_SEC = 10        # 每次 VLM 调用的超时
GAME_FOUND_CELEBRATE_SEC = 1.5   # 找到后兴奋动画+位置关键词播完，停留多久
                                  # 再回常态
GAME_TIMEOUT_LINGER_SEC = 3.0    # 超时抱歉表情停留多久再回常态
GAME_COUNTDOWN_PITCH = 250       # 倒计时时的低头角度（回正是450，越小越低头）
GAME_COUNTDOWN_NUMBERS = ["5", "4", "3", "2", "1"]  # 倒计时报的数字，本身
                                  # 就是给人留出的藏东西时间，不需要额外阻塞等待
GAME_COUNTDOWN_GAP_SEC = 1.0     # 倒计时数字之间的间隔，比默认的
                                  # KEYWORD_GAP_SEC(0.5s) 更从容，像真的在数数
GAME_REJECTION_WINDOW_SEC = 3.0  # 念完识别到的物品关键词后，留这么久给人
                                  # 说"不是这个"之类的否定/重来指令，见
                                  # is_registration_rejection()

# --- 游戏 LED（对照表情映射v7.xlsx 里没有的新增行）---
GAME_COUNTDOWN_LED_RGB = (0, 100, 255)
GAME_COUNTDOWN_LED_PERIOD_MS = 2000
GAME_SCAN_LED_PERIOD_MS = 3000

# --- 游戏固定词汇 TTS 预热 ---
# "小狗""看""闭眼""没有"和倒计时数字这几个词，内容从来不随游戏而变，没必要
# 每次触发游戏都现合成一次——TTS 合成是一次网络往返，几百毫秒到一两秒不等，
# 用户反馈"从听到邀请到说出'小狗 看'中间等太久"，这段网络延迟是主因之一。
# PuppyEngine.__init__() 后台预热合成好并缓存路径（_prewarm_game_tts()），
# _game_speak_keywords() 优先用缓存，只有缓存未命中（预热还没跑完，或者是
# LLM 生成的动态关键词，比如识别到的物品/位置）才现合成。
GAME_FIXED_PHRASES = ["小狗", "看", "闭眼", "没有"] + GAME_COUNTDOWN_NUMBERS

# --- 视觉大模型（Qwen-VL，通过阿里云 DashScope 的 OpenAI 兼容端点）---
# 用途有三处：①注册阶段把拍到的物品压缩成 1 个关键词念出来（比如"橘子"）
# ②扫描阶段颜色直方图粗筛命中后的精确确认（"画面里是不是真的是这个东西"）
# ③找到后描述物品大概在哪个位置/容器，同样压缩成关键词念出来（比如"桌子
# 下面"）。三处都直接用 Qwen-VL 一次调用完成"看图+给出关键词风格文字"，不
# 需要再链式调用 DeepSeek 做二次压缩——Qwen-VL 本身就有文本生成能力，prompt
# 里直接要求它输出 AAC 风格的短词，效果和延迟都跟现有 DeepSeek 文字对话
# 调用同一量级（单图+短 prompt 通常 1-3 秒）。
# API key 从 .env 的 DASHSCOPE_API_KEY 读取（跟 DEEPSEEK_API_KEY 同一份
# .env，同一套 load_env_key() 解析逻辑）；没有配置 key、或者调用失败/超时/
# key 无效，call_vision_llm() 都统一返回 None，调用方全部设计成能在没有
# VLM 的情况下优雅降级（注册阶段跳过物品关键词播报、扫描阶段只看颜色直方
# 图、找到后跳过位置关键词播报），不会因为 VLM 不可用就让整个游戏玩不了。
QWEN_VL_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_VL_MODEL = "qwen-vl-plus"
GAME_OBJECT_DESC_PROMPT = (
    "这是一张照片，图片中最主要的物体是什么？请用1个简短的词语描述它是"
    "什么（比如\"橘子\"\"小兔子\"\"红色杯子\"），不超过6个字，只输出这个"
    "词语本身，不要输出其它任何内容。"
)
GAME_LOCATION_DESC_PROMPT = (
    "这张照片是在寻找一个藏起来的物品时拍到的，物品大概率就在画面中。请用"
    "1-3个简短的词语描述它可能所在的位置/容器/家具（比如\"桌子\" \"下面\" "
    "\"手里\" \"沙发\" \"缝隙\"），不超过10个字，只输出描述本身，不要输出"
    "其它内容。"
)

# --- 触摸检测 ---
# 从"手指碰到传感器"到"反应动作真正开始"的延迟，上限基本就是
# MAIN_LOOP_INTERVAL_SEC + TOUCH_POLL_SEC 这两个数字之和（HTTP 往返本身只有
# 30-50ms，可以忽略）：check_touch() 已经不再受 poll_slot 轮流限制、每个
# tick 都会调用，但它内部还有这个独立节流——一起从 0.5s 降到 0.2s，把最坏情况
# 延迟从原来最多 ~1s 压到 ~0.4s。之所以敢往下调：/touch 早就是
# handleTouch() 那种零堆分配的静态缓冲区实现（历史上 /volume 那次教训是
# malloc()+高频轮询的组合拳，不是单纯"轮询快"本身的问题），提高到 5Hz 左右
# 不会重蹈那次覆辙。
TOUCH_POLL_SEC = 0.2
SCREEN_TAP_DEBOUNCE_SEC = 1.0    # 单次物理点按屏幕，固件的 screen_tap_count
                                  # 有时会连续涨好几次（实机日志见过同一次碰
                                  # 屏幕在三个相邻 tick 里各被判定成一次"新的"
                                  # 点击，各自都会触发一次完整的"贴贴"动画，
                                  # 三次连播加起来就是用户反馈的"碰屏幕→贴贴
                                  # 之间有 3~5 秒延迟"——不是延迟，是贴贴动画
                                  # 被连续重放了两三遍）。计数器设计本身是为了
                                  # 让"轮询慢也不会漏事件"，但没法区分"这是
                                  # 三次独立的点击"还是"同一次点击被触摸传感器
                                  # 弹跳成了三次计数"，所以在这之上再加一层
                                  # 基于时间的冷却：距离上一次真正触发"贴贴"
                                  # 不到这个时长，就算 screen_tap_count 又变了
                                  # 也不重新触发。1.0s 远大于日志里观察到的
                                  # 弹跳间隔（约 0.2~0.4s，几个 tick 之隔），
                                  # 但又不会明显影响用户想连续拍两下的正常
                                  # 使用（连续拍两下本来间隔通常也不会短于 1s）。

# --- 语音唤醒 ---
# 方案演进：最早是 /volume 轮询 + /record 间歇录音（方案A），根本问题是
# 两者都是"一次性、有限时长"的调用，/volume 每 VOLUME_POLL_SEC 秒才采样
# 一次，中间大段时间设备根本没在听，触发之后还要另开一次 /record 才能录到
# 真正的内容，说话节奏对不上就会被切掉、甚至整段错过。方案B改成 StackChan
# 主动推流（/stream?port=N）、host 常驻监听，音频从来不会"没在录"。
#
# 现在是方案C：改用电脑本地接的无线麦克风（sounddevice 直接从系统音频设备
# 采集），不再依赖 StackChan 机身麦克风经 WiFi 转发。动机是环境音/机身舵机
# 噪音总是被机身麦克风收进去、干扰语音识别；无线麦克风别在人身上、离嘴近、
# 离舵机远，信噪比好得多。StackChan 端的 /stream 相关代码原样保留（没有
# 删），只是不再被这里调用——如果以后想切回机身麦克风，恢复调用即可，不需要
# 改固件。方案B的滚动缓冲区+RMS阈值VAD这套逻辑完全复用，只是喂给它的音频
# 来源换了（见 MicStream._sd_callback()），"判断是不是真的有人在说话"的
# 判定方式不变。
WIRELESS_MIC_NAME_HINT = "Wireless Mic Rx"  # sounddevice 设备名子串匹配，
                                    # 优先在 WASAPI host API 下找（延迟低、
                                    # Windows 音频引擎共享模式管理，比 MME/
                                    # DirectSound 稳定）；实测这个无线麦克风
                                    # 接收器在 WASAPI 下的原生格式是 48kHz
                                    # 立体声，不是设备名旁边显示的那个默认值，
                                    # 所以代码里按查到的 default_samplerate
                                    # 现算重采样比例，不要硬编码 44100/48000。
STREAM_BUFFER_SECONDS = 8.0        # 滚动缓冲区保留的音频时长——够放下一整句话，
                                    # 又不会无限占内存（16kHz/16bit/单声道下
                                    # 8 秒约 256KB）
STREAM_CHUNK_SECONDS = 0.5         # 每凑够这么多新数据就算一次 RMS
STREAM_SILENCE_SECONDS = 1.0       # 连续这么久 RMS 都低于阈值，判定"说完了"
STREAM_PREROLL_SECONDS = 0.4       # 判定"开始说话"的那一刻往前多留一点余量
                                    # （因为是按 0.5s 一段判定的，真正开口的
                                    # 时刻很可能比"这一段整体超过阈值"稍早）
STREAM_RMS_THRESHOLD = 180          # 换成电脑无线麦克风后的校准值，改过两次：
                                    # 第一次测（隔离环境，只开麦克风单独测）
                                    # 量到安静基线 0~25、说话 90~190，定了 60；
                                    # 结果实际跑完整引擎（模型/摄像头/主循环都
                                    # 在跑的真实环境）以后，环境噪音基线本身
                                    # 就常态性地冲到 90~319，60 太低导致几乎
                                    # 每个 tick 都误判成"有人在说话"，这才是
                                    # "舵机好像没转"的根因之一（另一个更大的
                                    # 根因是代理拦截请求，见 _session.trust_env
                                    # 那条）——一直被新的（假）语音打断重新
                                    # 开始扫描，从来没机会真正转到位。改成
                                    # 450 解决了误触发，但又变得太不灵敏——
                                    # 实测跑完整引擎跑了158个tick零误触发，
                                    # 成功识别到的语音 RMS 落在 470~926，但
                                    # 需要凑近麦克风才够得到这个范围，正常
                                    # 说话距离识别不到。180 是在"零误触发的
                                    # 450"和"凑近才够的下限470"之间取的折中，
                                    # 给正常距离说话留出空间，同时还在观测到
                                    # 的噪音区间（90~319）里——不能保证零风险，
                                    # 后续如果又开始误触发，从这个值继续往上调；
                                    # 如果还是不够灵敏，继续往下调。教训：阈值
                                    # 校准必须在"实际会跑的完整环境"下测，用
                                    # 隔离的诊断脚本测很容易失真（比如脚本自己
                                    # 20 秒窗口跑完了，人才刚看到"现在说话"的
                                    # 提示，采到的全是没人说话的安静数据）。
MIC_UNMUTE_COOLDOWN_SEC = 0.35     # wait_for_playback() 看到 /status 的 playing
                                    # 变 false 就以为"播完了"，其实固件那边
                                    # playTaskFn() 最后一块音频是用
                                    # M5.Speaker.playRaw() 非阻塞喂给 I2S 就直接
                                    # 退出任务、把 playTaskRunning 清 false 的
                                    # （firmware.ino 里的分块循环只在喂下一块前
                                    # 等上一块播完，最后一块喂完不等），最后一个
                                    # CHUNK_BYTES=6400 字节（16kHz/16bit 下约
                                    # 0.2s）在 playing 已经变 false 之后仍在物理
                                    # 播放。之前 set_muted(False) 紧跟着
                                    # wait_for_playback() 返回就执行，这段尾音会
                                    # 被无线麦克风原样录进去、当成用户在说话——
                                    # 这就是"静音了还是会把小狗自己的话录进去"
                                    # 的根因，不是 set_muted() 本身没生效。只有
                                    # 自然播完（finished_ok=True）才需要这个冷却
                                    # ——被触摸打断走的是 M5.Speaker.stop() 硬
                                    # 切断（stopPlayTaskAndWait()），没有这条尾巴。
                                    # 跟固件那边处理舵机噪音的 SERVO_MUTE_
                                    # COOLDOWN_MS(300ms) 是同一个思路：状态标志
                                    # 变化不等于物理效果立刻消失，需要留一点缓冲。
# --- 完整对话链路 ---
KEYWORD_GAP_SEC = 0.8            # qa_complex 逐个念关键词，两个关键词之间的间隔
BUTTON_PRESS_MS = 200            # 每个关键词播放前，按钮"按下"状态维持的时长
KEYWORD_MAX_CHARS = 10           # 单个关键词允许的最大字符数——SYSTEM_PROMPT 要求
                                  # 每个关键词 1-3 个字，词库里最长的合法例外是英文
                                  # "love you"(8 字符)，这里留了点余量；LLM 偶尔不
                                  # 遵守这条规则，会把没拆开的整句话塞进 keywords
                                  # 数组的某一项，见 sanitize_keywords()

# --- 字幕：语音段识别出结果后，把识别到的文字显示出来，方便用户确认输入
#     内容。字幕框只应该在"录音+语音识别"这段时间出现——实测字幕框会挡住
#     "兴奋"等表情的一部分，所以最终识别结果一显示出来就立刻清空，不会留到
#     LLM 回复出来、更不会留到后面的表情/状态切换（见
#     _run_conversation_turn_body()）。改成流式监听（MicStream）之后不再有
#     单独的"录音中"状态可以配麦克风图标——语音是持续后台捕捉的，触发的时候
#     内容已经录完了，所以只剩这一段用途。
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

# --- 语音合成 (animalese) ---
# 从可懂的中文人声 TTS（edge-tts）换成 animalese（《集合啦！动物森友会》风格
# 的无字义拟声词）——声音本身不可懂，用户必须靠屏幕字幕才能理解小狗在说
# 什么，见 speak_keywords()/_game_speak_keywords() 里配套的字幕联动。纯本地
# 计算（逐字母拼接现成的音频片段），合成耗时 <10ms，不像 edge-tts 那样要经过
# 一次网络往返；_prewarm_game_tts() 那套预热缓存机制因此不再是必需的，但
# 继续用也无害（缓存命中时依然会跳过一次合成，只是省下的时间已经很少了），
# 这次没有顺手删掉它。
ANIMALESE_WAV_URL = "https://github.com/Acedio/animalese.js/raw/master/animalese.wav"
ANIMALESE_LIBRARY_PATH = Path(__file__).resolve().parent / "animalese.wav"
ANIMALESE_LIBRARY_SAMPLE_RATE = 44100   # animalese.wav 字母库本身的采样率（26 个
                                         # 英文字母各一段，8-bit/单声道）
ANIMALESE_LIBRARY_LETTER_SECS = 0.15    # 库中每个字母片段的时长
ANIMALESE_OUTPUT_LETTER_SECS = 0.075    # 输出每个字母的时长——只取库里每段的前半
                                         # 部分（原版的一半），念起来更快更像"叽里
                                         # 呱啦"，而不是拖长的原始采样
ANIMALESE_PITCH = 1.0                   # 音高：1.0 正常，<1 更低沉/慢，>1 更高亢/快
ANIMALESE_VOLUME_BOOST = 1.0            # 音量放大倍数——host/animalese_test.py 里试出来
                                         # 的 3.0 实机听起来偏大声（而且 3x 放大配合
                                         # np.clip 到 [-1,1] 会有明显削波失真），改回
                                         # 不放大
TTS_SAMPLE_RATE = 16000          # 转成 StackChan /record 同款格式：16kHz/单声道/16bit

AUDIO_DIR = Path(tempfile.gettempdir()) / "stackchan_audio"
AUDIO_DIR.mkdir(exist_ok=True)

# --- 表情映射：key 是引擎内部状态名，value 是固件 /face?expr= 实际支持的名字
#     （neutral/happy/sad/angry/sleepy/doubt/love/eyeroll/thinking/excited/
#      privacy/curious/sorry/dizzy，见 firmware.ino 的 handleFace()）---
EXPRESSION_MAP = {
    "idle":     "neutral",
    "happy":    "happy",
    "sleepy":   "sleepy",
    "curious":  "curious",
    "sorry":    "sorry",
    "thinking": "thinking",
    "excited":  "excited",
    "privacy":  "privacy",
    "grieved":  "grieved",
    "peekaboo": "peekaboo",
    "dizzy":    "dizzy",
    "dead":     "dead",
    "angry":    "angry",
    "play":     "play",
    "eat":      "eat",
}

# --- 定时提醒 + 生气催促 ---
# 小狗在指定时间主动提醒主人（喝水/吃饭/走动），提醒后如果在场监测窗口内
# 主人一直没离开座位，小狗表现出"生气"行为催促。只在 host 端实现，固件
# 早就有 angry 表情 + LED/舵机/触摸/摄像头这些全部需要的能力，不用改固件。
#
# 吃饭是固定时间点（中午/傍晚各一次），提醒完就结束，不进在场监测、不会
# 催促生气。喝水/出去玩是完全独立的另一条链，跟吃饭提醒没有依赖关系——
# 引擎启动时就直接排好下一次触发时间，按 DYNAMIC_INTERVAL_MIN/MAX_SEC
# 随机间隔连续触发、每次按天气二选一，直到 DYNAMIC_ACTIVE_HOUR_END 才停
# （见 _check_dynamic_reminder()）——不用固定时间点是因为"每隔1~1.5小时"
# 这种持续节奏本身就跟"某个固定时刻"是两种不同的调度模型，硬塞进
# REMINDERS 这张固定时间表反而别扭。这条链发出提醒后会启动在场监测窗口，
# 是唯一可能触发"生气"的路径。
REMINDERS = [
    {
        "hour": 11, "minute": 30,
        "expression": "want_eat",
        "label": "eat_lunch",
    },
    {
        "hour": 17, "minute": 30,
        "expression": "want_eat",
        "label": "eat_dinner",
    },
]

REMINDER_WINDOW_SEC = 120          # 到达设定时间前后 2 分钟内算命中
REMINDER_COOLDOWN_SEC = 600        # 同一条提醒触发后 10 分钟内不再重复

# --- 提醒后的在场监测窗口 ---
# 提醒发出后不是"过了固定时长就无条件生气"，而是持续采样人脸、按窗口结束
# 时的在场率决定要不要生气——主人中途离开过（比如去接了杯水又回来坐下）
# 就不该被催促。窗口用采样次数而不是时间戳判断结束（见
# _check_presence_monitoring()），样本数攒够就结束。
PRESENCE_SAMPLE_INTERVAL_SEC = 30  # 采样间隔（秒）。不要调得更短——参考
                                    # FACE_BG_TIMEOUT_SEC 旁边记的教训，请求
                                    # 太频繁会把 ESP32 打崩（堆碎片化重启）。
PRESENCE_WINDOW_SAMPLES = 20       # 总采样次数（30s × 20 = 600s = 10 分钟）
PRESENCE_ANGRY_THRESHOLD = 0.70    # 在场率严格大于此值才触发生气（14/20=
                                    # 0.70 不算，15/20=0.75 才算）
PRESENCE_WINDOW_MAX_SEC = 1800     # 窗口的硬性兜底时长（30 分钟）。正常情况
                                    # 下窗口靠攒够 PRESENCE_WINDOW_SAMPLES 个
                                    # 样本结束；但 PRIVACY 状态下不采样（见
                                    # _check_presence_monitoring()），如果
                                    # 主人长时间待在 PRIVACY 里，样本会一直
                                    # 攒不够，窗口就卡住不结束——
                                    # self._reminder_recheck_target 会一直
                                    # 非 None，导致 _check_reminders()/
                                    # _check_dynamic_reminder() 互斥挡住后续
                                    # 所有新提醒。这个兜底保证不管样本够不够，
                                    # 最多等这么久窗口也会强制结束（用现有
                                    # 样本，哪怕是 0 个，算一次在场率）。取
                                    # 30 分钟是给 PRIVACY 这类中断留够缓冲，
                                    # 比理想的 10 分钟窗口宽松不少，但仍然
                                    # 远好于"无限期卡住"。

# --- 喝水/出去玩的动态调度 ---
# 不进 REMINDERS 固定时间表，靠 self._dynamic_next_time（下一次触发的
# time.time() 时间戳）驱动，在引擎启动时（`__init__()`）就直接排好第一次
# 触发时间，按 DYNAMIC_INTERVAL_MIN/MAX_SEC 随机间隔连续触发一条独立的链，
# 每次触发完（不管有没有真的找到人）都会重新排一次下一次的时间。
#
# 这条链跟吃饭提醒（REMINDERS）之间没有依赖关系——早期版本要求当天第一次
# 吃饭提醒成功发出之后才开始这条链，现在按状态机设计（见
# 表情映射v11.xlsx/CLAUDE.md 里的状态图）把两者拆成完全并行、互不依赖的
# 两条时间触发路径，只共用同一个"是否有提醒正在监测中"的互斥
# （`self._reminder_recheck_target`）。
#
# 活跃时段收紧到下午/傍晚——13点前（含上午、深夜、凌晨）不触发，21点后也
# 不触发；这个时间范围是没有实机验证过的默认猜测，具体几点合适要看实际
# 作息反馈调整。落在活跃时段之外时不会攒着一开门就立刻触发，而是重新
# 从活跃时段起点开始算一次随机间隔（见 _check_dynamic_reminder()），避免
# "憋了一晚上13点整准时响"这种不像随机节奏的效果。
DYNAMIC_ACTIVE_HOUR_START = 13
DYNAMIC_ACTIVE_HOUR_END = 21
DYNAMIC_INTERVAL_MIN_SEC = 3600     # 1 小时
DYNAMIC_INTERVAL_MAX_SEC = 5400     # 1.5 小时

# 喝水/出去玩各自的提醒模板，跟 REMINDERS 里的条目是同一种 dict 形状，
# _deliver_reminder() 不关心调用方是固定时间表还是动态调度器传进来的。
DYNAMIC_REMINDER_TEMPLATES = {
    "drink_water": {
        "expression": "want_drink",
        "label": "drink_water",
    },
    "move_around": {
        "expression": "want_play",
        "keywords": ["出去", "走", "动"],   # get_weather_keywords() 失败时
                                            # 的兜底，见旧 REMINDERS 条目里
                                            # 同一条注释
        "label": "move_around",
    },
}


def _next_dynamic_delay():
    return random.uniform(DYNAMIC_INTERVAL_MIN_SEC, DYNAMIC_INTERVAL_MAX_SEC)


def _in_dynamic_active_hours(dt):
    return DYNAMIC_ACTIVE_HOUR_START <= dt.hour < DYNAMIC_ACTIVE_HOUR_END

# 三种提醒现在都直接 set_expression() 到各自专属表情（want_play→"play"，
# want_drink/want_eat→"eat"，见 _deliver_reminder()），不再需要一张
# "占位表情映射表"——REMINDER_EXPR_MAP 这个中间层已经没有调用点，删掉了。

# 提醒开场的"委屈"过渡（时间到 → 摆头 + 委屈表情 + LED 呼吸灯，见
# _deliver_reminder()），呼吸灯颜色沿用项目里"委屈"相关反应一贯用的暖白
# （play_grieved_reaction() 也是暖白，只是那边是 blink 不是 breathe）。
REMINDER_GRIEVED_LED_BREATHE_PERIOD_MS = 2000

# --- "想出去玩"提醒的天气感知关键词库 ---
# 只用于 REMINDERS 里 expression=="want_play" 的条目（move_around 这一条，
# 主题是"想出去"）。drink_water 原来也是 want_play，现在改成独立的
# want_drink（见下面 get_drink_keywords()），不再共用这一套。
#
# 和风天气免费版账号实测没有 GeoAPI（城市名查 LocationID）权限，但直接用
# 已知的 LocationID 查 /v7/weather/now 是通的，所以不查城市名，杭州的
# LocationID 直接写死（本身不会变，没必要每次先查一遍）。
WEATHER_LOCATION_ID = "101210101"      # 杭州
WEATHER_API_TIMEOUT_SEC = 5
WEATHER_COLD_TEMP_C = 5                # 低于这个温度，关键词池里会带上"冷"
WEATHER_HOT_TEMP_C = 30                # 高于这个温度，关键词池里会带上"暖"
WEATHER_KEYWORD_COUNT_MIN = 2          # 每次挑几个词，2~4 之间随机——不再
WEATHER_KEYWORD_COUNT_MAX = 4          # 固定 3 个，加了时间词以后凑不满
                                        # 3 个/超过 3 个都应该是正常情况，
                                        # 不用刻意凑数或者砍掉

# 时间段描述词——按当前小时挑一个候选词，但只是"候选"，不保证一定会说出来
# （跟气温词"冷"/"暖"同一个地位，都放进随机抽样的池子里，抽不抽得到看
# 运气），不需要单独再搞一套"多少概率要不要加"的判断。"morning" 是用户
# 原话给的例子，故意保留英文没有转成中文——animalese 合成链路本身就是把
# 中文转拼音再逐字母合成，纯拉丁字母的词跳过转拼音那一步直接进合成，两种
# 混着用不会出问题。
TIME_OF_DAY_WORDS = {
    "morning": (5, 10),    # 5:00-9:59
    "亮亮":    (10, 18),    # 10:00-17:59，白天
    "暗暗":    (18, 24),    # 18:00-23:59，晚上（跟下面 0-5 点合起来才是完整的"暗"）
}

def pick_time_of_day_word():
    hour = datetime.now().hour
    for word, (start, end) in TIME_OF_DAY_WORDS.items():
        if start <= hour < end:
            return word
    return "暗暗"  # 0:00-4:59，深夜也算"暗"

# 和风天气 icon 代码分类（官方文档 https://dev.qweather.com/docs/resource/icons/）：
# 100/150=晴，101-103/151-153=多云/少云/晴间多云，104/154=阴，300-318=雨，
# 400-499=雪，500+=雾霾沙尘。用区间粗分类，不需要精确到每一个具体代码。
def _classify_weather_icon(icon_code):
    try:
        code = int(icon_code)
    except (TypeError, ValueError):
        return "other"
    if code in (100, 150):
        return "sunny"
    if code in (101, 102, 103, 151, 152, 153):
        return "cloudy"
    if code in (104, 154):
        return "overcast"
    if 300 <= code <= 318:
        return "rain"
    if 400 <= code <= 499:
        return "snow"
    return "other"

# 喝水/出去玩动态提醒二选一的依据：晴/多云/阴天算适合外出，雨/雪/"other"
# （雾霾沙尘等，见上面 _classify_weather_icon() 的分类注释）算不适合。跟
# WEATHER_KEYWORD_POOLS 是两套独立的判断——那边是"选中出去玩之后，用什么
# 词描述这次出去玩"，这里是"到底该提醒出去玩还是提醒喝水"这个更前置的
# 二选一决策，见 _check_dynamic_reminder()。
def _weather_suitable_for_outdoor(category):
    return category not in ("rain", "snow", "other")

# 这几个池子是照用户给的例子（"雨天→外面/水/玩"、"晴天→玩/阳光/风"）加上
# 他提供的完整词库手工分的，没有实机验证过挑出来的组合听着自不自然，用户
# 反馈了再调。"边边"/"大黄"/"耶耶"（小狗在公园认识的朋友，人设见
# SYSTEM_PROMPT）只放进适合"出去找朋友玩"的晴天/多云池子，雨天/雪天不放
# ——下雨下雪应该不会有人带狗出门碰到朋友。
WEATHER_KEYWORD_POOLS = {
    "sunny":    ["玩", "阳光", "风", "草地", "花花", "虫虫", "外面", "边边", "大黄", "耶耶"],
    "cloudy":   ["玩", "外面", "风", "草地", "边边", "大黄", "耶耶"],
    "overcast": ["玩", "外面", "风", "草地"],
    "rain":     ["外面", "水", "雨", "玩"],
    "snow":     ["外面", "冷", "玩"],
    "other":    ["玩", "外面"],
}

# 每种天气类型里"最能代表这个天气"的词——挑关键词时会优先保证选进最终
# 结果，剩下的名额才轮到随机（气温词"冷"/"暖"也在随机名额里，优先级排在
# 天气状况词之后）。cloudy/overcast 词库里没有能一眼看出"多云"/"阴"的
# 专属词（可用的都是"玩"/"外面"这类什么天气都通用的词），没有强制。
# 加这条是因为实测踩过一次：下大雨那天随机抽出来的是"玩/暖/外面"，完全
# 没提到雨，天气感知形同虚设——根源是天气状况词和气温词混在同一个池子里
# 平等抽样，运气不好就会漏掉真正代表这个天气的词。
WEATHER_SIGNATURE_WORD = {
    "sunny": "阳光",
    "cloudy": None,
    "overcast": None,
    "rain": "雨",
    "snow": "冷",   # 词库里没有专门的"雪"，用"冷"代表下雪天的体感
    "other": None,
}

def _fetch_weather_now(api_key, api_host):
    """查一次杭州实时天气，返回 (分类, 摄氏温度, 天气文字描述)；任何失败
    （没配置 key/host、网络错误、响应格式不对）都返回 None。
    get_weather_keywords()（想出去玩）和 get_drink_keywords()（想喝水）
    都要用到同一份天气数据，抽出来一份，两边不用各自发一次请求、各自
    重写一遍同样的错误处理。"""
    if not api_key or not api_host:
        return None
    try:
        r = requests.get(
            f"https://{api_host}/v7/weather/now",
            params={"location": WEATHER_LOCATION_ID, "key": api_key},
            timeout=WEATHER_API_TIMEOUT_SEC,
        )
    except requests.exceptions.RequestException as e:
        print(f"  [天气] 请求失败: {e}")
        return None
    if r.status_code != 200:
        print(f"  [天气] API 返回 {r.status_code}: {r.text[:200]}")
        return None
    try:
        now = r.json()["now"]
        category = _classify_weather_icon(now["icon"])
        temp_c = float(now["temp"])
        text = now.get("text", "?")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"  [天气] 响应格式不对: {e!r}")
        return None
    return category, temp_c, text

def get_weather_keywords(api_key, api_host, count=None):
    """按天气类型+气温挑一组随机关键词（AAC 风格，表达"小狗想去外面玩
    什么"）。天气查不到时返回 None，调用方退化成用 REMINDERS 里那条
    提醒自带的固定关键词——跟 call_vision_llm() 是同一个"可选增强，不是
    硬依赖"的设计，不能假设这里一定有结果。count 不传时在
    [WEATHER_KEYWORD_COUNT_MIN, WEATHER_KEYWORD_COUNT_MAX] 之间随机挑，
    不再固定 3 个。"""
    if count is None:
        count = random.randint(WEATHER_KEYWORD_COUNT_MIN, WEATHER_KEYWORD_COUNT_MAX)
    fetched = _fetch_weather_now(api_key, api_host)
    if fetched is None:
        return None
    category, temp_c, text = fetched

    pool = list(WEATHER_KEYWORD_POOLS.get(category, WEATHER_KEYWORD_POOLS["other"]))
    temp_word = None
    if temp_c <= WEATHER_COLD_TEMP_C:
        temp_word = "冷"
    elif temp_c >= WEATHER_HOT_TEMP_C:
        temp_word = "暖"
    if temp_word and temp_word not in pool:
        pool.append(temp_word)
    # 时间段词跟气温词同一个地位，加进候选池子，抽不抽得到看随机结果，
    # 不强制（用户原话"不一定每次都要说"）。
    time_word = pick_time_of_day_word()
    if time_word not in pool:
        pool.append(time_word)

    # 天气状况词优先占一个名额，剩下的名额（包含气温词在内）才随机抽。
    result = []
    signature = WEATHER_SIGNATURE_WORD.get(category)
    if signature and signature in pool:
        result.append(signature)
        pool.remove(signature)
    remaining = count - len(result)
    if remaining > 0:
        result.extend(random.sample(pool, min(remaining, len(pool))))
    random.shuffle(result)  # 不然天气状况词会永远排在关键词列表第一个，显得死板

    print(f"  [天气] {text} {temp_c}°C → 分类={category}，代表词={signature or '无'}")
    return result

# --- "想喝水"提醒的关键词库 ---
# "水"是每次都会说的必要词（用户原话"除了水这个必要的关键词"），剩下的
# 名额从基础候选池（哪种天气都适用）里随机抽；如果天气查得到，按气温
# 再加几个候选——热天倾向"渴/冰/凉凉"，冷天倾向"暖/热乎"（跟
# get_weather_keywords() 的"冷"/"暖"是同一个温度阈值，但这里用更具体的
# "想喝口凉的/暖的"而不是单纯"冷"/"暖"，更贴近"想喝水"这个主题）。跟
# get_weather_keywords() 不同的是，这个函数不依赖天气 API 也能正常工作
# （天气查不到就只用基础候选池），因为"水"和基础候选词本身就无关天气，
# 不会因为查不到天气就完全没有变化，所以不需要返回 None 走外层兜底。
# 故意不接时间段词（跟 get_eat_keywords()/get_weather_keywords() 不
# 一样）——用户反馈想喝水这条不需要时间段变量，删掉了。
DRINK_KEYWORD_POOL = ["喝", "杯杯", "咕嘟"]
DRINK_HOT_WORDS = ["渴", "冰", "凉凉"]
DRINK_COLD_WORDS = ["暖", "热乎"]

def get_drink_keywords(api_key, api_host, count=None):
    if count is None:
        count = random.randint(WEATHER_KEYWORD_COUNT_MIN, WEATHER_KEYWORD_COUNT_MAX)
    pool = list(DRINK_KEYWORD_POOL)
    fetched = _fetch_weather_now(api_key, api_host)
    if fetched is not None:
        category, temp_c, text = fetched
        if temp_c >= WEATHER_HOT_TEMP_C:
            pool.extend(DRINK_HOT_WORDS)
        elif temp_c <= WEATHER_COLD_TEMP_C:
            pool.extend(DRINK_COLD_WORDS)
        print(f"  [天气] {text} {temp_c}°C（想喝水关键词参考这个气温）")

    result = ["水"]
    remaining = count - len(result)
    if remaining > 0 and pool:
        result.extend(random.sample(pool, min(remaining, len(pool))))
    random.shuffle(result)
    return result

# --- "想吃饭"提醒的关键词库 ---
# "饭饭"是每次都会说的必要词，其它词从跟吃相关的候选池里随机抽。故意
# 不接天气/气温——食欲跟天气没有"想出去玩"/"想喝水"那么直接的关系，
# 只保留时间段词这一个跟环境有关的变量。"肉肉"/"零食"/"香香"是用户
# 给的例子，"肚肚"/"空"是从"饿"这个词拆出来的（见 CUTE_WORD_
# SUBSTITUTIONS 的说明），直接当独立候选词用，不用再靠替换。"时间"
# 保留了原来固定列表里的词。这几个词有没有实机验证过组合起来自不自然，
# 用户反馈了再调。
EAT_KEYWORD_POOL = ["时间", "肚肚", "空", "肉肉", "零食", "香香"]

def get_eat_keywords(count=None):
    if count is None:
        count = random.randint(WEATHER_KEYWORD_COUNT_MIN, WEATHER_KEYWORD_COUNT_MAX)
    pool = list(EAT_KEYWORD_POOL)
    time_word = pick_time_of_day_word()
    if time_word not in pool:
        pool.append(time_word)

    result = ["饭饭"]
    remaining = count - len(result)
    if remaining > 0 and pool:
        result.extend(random.sample(pool, min(remaining, len(pool))))
    random.shuffle(result)
    return result

# 提到朋友的名字，后面必须紧跟"玩"——不能只报朋友的名字不说想干什么。
FRIEND_NAMES = {"边边", "大黄", "耶耶"}

def enforce_friend_needs_play(words):
    """检查关键词列表，只要出现朋友名字、后面没有紧跟着"玩"，就在朋友
    名字后面插入一个"玩"（不是替换掉原来那个词，尽量保留随机挑出来的
    其它词，允许因此变长）。get_weather_keywords() 的随机采样和
    REMINDERS 里手写的固定列表都可能撞上这条规则，所以放在 _deliver_
    reminder() 里统一在关键词最终确定之后调用一次，不分别处理两个来源。"""
    result = []
    for i, w in enumerate(words):
        result.append(w)
        if w in FRIEND_NAMES:
            next_word = words[i + 1] if i + 1 < len(words) else None
            if next_word != "玩":
                result.append("玩")
    return result

# 叠词/AAC 风格的用词替换——"饭"/"吃饭"说成"饭饭"，"饿"拆成"肚肚"+"空"
# 两个词（不是叠词，是"肚子空了"这个意象拆成两个 AAC 词）。一个词可以换成
# 多个词，所以用列表而不是字符串做 value。
CUTE_WORD_SUBSTITUTIONS = {
    "饭":  ["饭饭"],
    "吃饭": ["饭饭"],
    "饿":  ["肚肚", "空"],
}

def apply_cute_substitutions(words):
    """按 CUTE_WORD_SUBSTITUTIONS 替换关键词列表里的词，命中的词换成
    对应的一个或多个词，没命中的原样保留。放在 enforce_friend_needs_
    play() 同一层，两者互不影响，谁先谁后都一样——REMINDERS 里手写的
    固定列表（比如 eat_lunch 的"吃饭"/"饿"）和 get_weather_keywords()
    的随机结果都会经过这一步。"""
    result = []
    for w in words:
        result.extend(CUTE_WORD_SUBSTITUTIONS.get(w, [w]))
    return result

# --- 提醒播报前的轻微左右摆头 ---
# 三条 REMINDERS 都会用到（想出去玩的天气播报前、想喝水/想吃饭的"委屈"
# 过渡阶段，见 _deliver_reminder()）。幅度/节奏直接参考贴贴
# （play_tietie_animation()/TIETIE_*），只是把 pitch 轴的抬头
# 换成 yaw 轴的左右摆动——落差同样是 120（贴贴是 450-330=120），摆动
# 相对当前 yaw 进行（不是绝对角度，因为这一步之前舵机可能已经因为
# scan_for_face()/track_face_servo() 停在任意角度），摆完落回摆动前的
# 原始 yaw，不会留在偏转的位置上。
REMINDER_PLAY_SWING_YAW_AMPLITUDE = 120
REMINDER_PLAY_SWING_SPEED = 300
REMINDER_PLAY_SWING_CYCLES = 3
REMINDER_PLAY_SWING_CYCLE_DELAY = 0.3

def play_reminder_swing_animation():
    status = get_status()
    base_yaw = status.get("yaw", 0) if status else 0
    half = REMINDER_PLAY_SWING_YAW_AMPLITUDE // 2
    for _ in range(REMINDER_PLAY_SWING_CYCLES):
        move_servo(yaw=base_yaw - half, speed=REMINDER_PLAY_SWING_SPEED, mute=True)
        time.sleep(REMINDER_PLAY_SWING_CYCLE_DELAY)
        move_servo(yaw=base_yaw + half, speed=REMINDER_PLAY_SWING_SPEED, mute=True)
        time.sleep(REMINDER_PLAY_SWING_CYCLE_DELAY)
    move_servo(yaw=base_yaw, speed=REMINDER_PLAY_SWING_SPEED, mute=True)

ANGRY_LED_RGB = (255, 30, 0)           # 红灯颜色，没有实机验证过
ANGRY_LED_BLINK_PERIOD_MS = 400        # 闪烁周期——固件 updateLed() 的 BLINK
                                        # 一个完整周期（亮+暗）就是 period_ms，
                                        # 不是亮暗各占一半再乘二
ANGRY_LED_BLINK_COUNT = 3              # 闪烁次数
ANGRY_FACE_FOUND_DELAY_SEC = 1.0       # 找到人脸后停顿，让用户看到小狗在生气
ANGRY_YAW_TURN = 200                   # 左转("哼，不理你")。用户在设计反馈里
                                        # 明确写的是"左转（yaw+）"，跟 CLAUDE.md
                                        # 里 API 文档记的"yaw 越大越向右"方向
                                        # 相反——按用户这次给的方向改，但这个
                                        # 符号本身还没有被实机看到转向后确认过
                                        # （上一版转向这一步被过早的双击打断，
                                        # 用户实际没看到这一下转动），如果实机
                                        # 测出来是反的，取反这个值即可。
ANGRY_YAW_SPEED = 300
ANGRY_YAW_SETTLE_TOLERANCE = 30        # 判定"已经转到位"的容差，跟 enter_dead()
                                        # 的 DEAD_PITCH_UP_SETTLE_TOLERANCE 同一个值
ANGRY_YAW_SETTLE_TIMEOUT_SEC = 3.0     # 等舵机转到位的最长时间，超时就按当前
                                        # 角度继续，不会卡死。直接拿真机测过：
                                        # 200 多个单位的转动大概 1~2 秒才能进
                                        # 容差范围，原来 1.5 秒偏紧，放宽一点
                                        # 留余量。
ANGRY_FORGIVE_HOLD_SEC = 3.0           # 原谅前保持生气表情的时长


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
    DIZZY    = "晕"
    DEAD     = "装死"
    GAME_HIDE_SEEK = "捉迷藏"
    ANGRY    = "生气"


# ╔══════════════════════════════════════════════╗
# ║          LLM 意图分类 system prompt           ║
# ╚══════════════════════════════════════════════╝

SYSTEM_PROMPT = """你是一只比格犬，名字叫"小狗"，你叫主人"人"。
你家里的朋友：一只三花小猫，名字叫"咪咪"。
你在公园认识的朋友：萨摩耶"耶耶"、边牧"边边"、田园犬"大黄"。

你需要理解人说的话，用 JSON 标注意图和关键词回应。

规则：
1. 是非问题（可以用"是/否"回答的问题）：不用关键词，直接输出，不要输出别的文字：
{"type": "qa_simple", "answer": "yes" 或 "no"}

2. 人在表扬你：直接输出，不要输出别的文字：
{"type": "praise"}

3. 人在责备你：直接输出，不要输出别的文字：
{"type": "scold"}

4. 人让你去睡觉/休息/自己待一会儿、暗示不再需要你陪着（比如"去睡觉吧"
"你去休息吧""我们先聊到这里"这类意思）：直接输出，不要输出别的文字：
{"type": "privacy"}

5. 人提议一起玩"捉迷藏找物品"的游戏（比如"我们玩捉迷藏吧""你猜我藏了
什么""找找看""藏东西"这类意思，且是在提议开始一个新游戏，不是在问"你在
哪""你看到了吗"这种和游戏无关的日常问题）：直接输出，不要输出别的文字：
{"type": "game_hide_seek"}

6. 开放式问题（qa_complex）或其它情况（other）：
   第一步——先想清楚：如果你是这只小狗，针对人这句话，你会有什么具体、
   符合逻辑的真实反应？用一句大白话写下来，哪怕很短也行。这句话不会被
   念出来，只是逼自己先想清楚答案，不许跳过这一步，也不许把人问题里的
   词原样当成"想清楚的答案"。
   第二步——把这句大白话压缩成 2-4 个关键词（空格分隔），再换行输出 JSON：
   {"type": "qa_complex", "keywords": [...]}  或  {"type": "other", "keywords": [...]}

第 6 条的关键词选择规则：
- 关键词必须来自你自己想清楚的那句大白话，不能是人问题原句里出现的词的
  简单复读。如果发现自己选的词和问题原句几乎一样，说明大概率是偷懒没有
  真的回答，回去重想。
- **每一个关键词本身必须是单个词/短语（1-3个字为主），绝对不能是完整的
  主谓宾句子**（比如不能写"人叫我"这种，应该拆成"人"和"叫"两个独立
  的词，各占 keywords 数组里的一项）。你说的每一句话，不管是简单回应还是
  复杂回应，都只能通过关键词表达，不能输出完整语句——这是这只小狗表达
  自己的唯一方式，类似 AAC（辅助沟通）设备，不是在写一句正常的话。
  错误示范一（发生过，绝对不要这样）：
  {"type": "qa_complex", "keywords": ["今天天气很好，还伴有微风"]}
  ——这是把整句话原样塞进了 keywords 数组的一项里，等于完全没有拆分成
  关键词，念出来会变成小狗说了一整句完整的话。
  错误示范二（也发生过，同样绝对不要这样）：
  {"type": "qa_complex", "keywords": ["外面 好 风"]}
  ——第二步说的"空格分隔"只是给你自己压缩时用的中间步骤，最终写进
  keywords 数组时必须已经按空格拆成三个独立的项 ["外面","好","风"]；
  把这个中间形式原样整个塞进数组的一项里，会导致这三个词被当成一个词
  连在一起一次性说完，而不是分开、一个一个地说。
- 优先从下面的词库里选，但不局限于词库，需要时可以用词库外的词（权重从高
  到低排列）：
  需求词（权重最高）：外面、出门、玩、水、零食、飞盘、球球、拔河、罐罐、
  牛奶、睡觉、尿尿、噗噗
  时间词：今天、明天、现在、刚才、结束
  对象词：人、小猫、咪咪、耶耶、边边、大黄
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
- 指代对象（人、咪咪等）或地点（外面、厨房）放在前面。
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
{"type": "scold"}

用户：我们玩捉迷藏吧，我藏个东西你来找
{"type": "game_hide_seek"}"""


# ╔══════════════════════════════════════════════╗
# ║              API 辅助函数                     ║
# ╚══════════════════════════════════════════════╝

# 复用一个 Session：StackChan 的 WebServer 没有主动发 "Connection: close"，
# 支持 HTTP keep-alive。之前每次 requests.get() 都会新开一个 TCP 连接，
# 对 ESP32 本来就紧张的 WiFi/LWIP 连接资源是额外压力；用同一个 Session 让
# urllib3 复用连接，减少设备侧频繁建连/拆连的开销。
_session = requests.Session()
# trust_env=False：不读环境变量/系统代理设置（本机跑着一个本地沙盒代理，
# HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1，requests 默认会自动用它）。StackChan
# 是同一个局域网内的设备，永远不该走代理——真正踩过的坑：代理把到设备的请求
# 转走以后返回了某个响应（不是连接失败），requests.get() 不会抛异常，
# api_get() 也不检查返回内容，所以 move_servo() 这类"发了就不管"的调用
# 看起来"成功"了，实际上命令根本没到设备，表现出来就是"没报错但舵机
# 没转"。这行修好之后同样的请求会绕开代理直连设备。
_session.trust_env = False

# ESP32 的 WebServer 是单线程的，同一时刻真正只能服务一个连接；这条锁保证
# host 端不管有多少个线程（主 tick() 循环、check_face()/retrack_face() 改
# 成后台线程之后新增的 worker），发给设备的请求永远是排队串行的，一次只有
# 一个在飞。**这是实测踩过的真坑，不是预防性加的**：check_face()/
# retrack_face() 改成后台线程后，某次后台线程正在等 /camera 响应（这个
# 请求本身可能要 1~2s）的同时，主线程恰好要给 play_tietie_animation()
# 发好几个 /servo 请求，其中两个直接 ConnectTimeoutError——不是"变慢"，是
# 连 TCP 连接都建立不起来，说明设备的连接接受能力（WiFiServer 的 backlog）
# 撑不住两个线程同时各开一条连接（哪怕其中一条是 keep-alive 空闲着）。加锁
# 让所有请求排队发送，才是跟设备真实的单线程处理能力匹配的模型——不会比
# 原来（一切都在同一个主线程里天然串行）更慢，只是把"新增的后台线程"也
# 纳入这个串行队列，不让它绕过去。
_device_lock = threading.Lock()

def api_get(endpoint, timeout=None, _retry=True):
    """GET 请求失败（连不上/超时）时不要立刻重试——先等 API_RETRY_DELAY_SEC，
    重试一次；再失败就放弃，返回 None。避免在设备已经吃紧时连续拍请求。"""
    try:
        with _device_lock:
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

def set_display(off=False, on=False):
    """调用固件的 /display 关闭（off=True）/唤醒（on=True）屏幕背光+面板睡眠。
    目前只在退出程序的关机动画收尾用一次，见 PuppyEngine.play_shutdown_
    animation()。"""
    if off:
        api_get("/display?off=1")
    elif on:
        api_get("/display?on=1")

def set_button(state):
    """调用固件的 /button 控制关键词播报按钮：up/down/off。"""
    api_get(f"/button?state={state}")

def move_servo(yaw=None, pitch=None, speed=None, mute=False):
    """mute=True 告诉固件这次移动"比较吵、这期间不指望还在听人说话"（见
    firmware.ino 的 g_currentMoveIsNoisy）——反应型动画（开心/兴奋/困倦/
    抱歉/隐私）和扫描找人这类跟对话不同步的大幅度移动应该传 True；人脸
    追踪那种"对话进行中顺手微调"的小幅度移动必须留 False（默认值），不然
    固件会把这段时间的真实语音也静音掉，说话说不全。"""
    params = []
    if yaw is not None:   params.append(f"yaw={yaw}")
    if pitch is not None: params.append(f"pitch={pitch}")
    if speed is not None: params.append(f"speed={speed}")
    if mute: params.append("mute=1")
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

def capture_frame(timeout=None, _retry=True):
    img, _ = capture_frame_with_bytes(timeout=timeout, _retry=_retry)
    return img

def capture_frame_with_bytes(timeout=None, _retry=True):
    """跟 capture_frame() 一样拍一帧，但同时把原始 JPEG 字节也返回——人脸
    检测只需要解码后的图像数组，捉迷藏游戏的 VLM 精确确认还需要把原始字节
    编码成 base64 传给视觉大模型，两者共用同一次 /camera 请求，不重复拍照。
    失败时返回 (None, None)。

    timeout/_retry 透传给 api_get()——后台人脸检测线程（见 check_face()/
    retrack_face() 的 _check_face_worker()/_retrack_face_worker()）会传一个
    短得多的超时、且不重试，理由见那两个函数旁边的说明。

    **打印本次请求的真实耗时（超过 CAMERA_SLOW_WARN_SEC 才打印，不是每次都
    打印）**：排查"触摸响应慢"时发现单独用 curl 连续测 `/camera` 稳定在
    500ms 左右，但完整引擎跑起来之后同一个接口偶尔要 1.5s+甚至超时——两者
    矛盾，说明变慢的不是设备/网络本身，而是多个请求（人脸追踪的两个后台
    线程、手势扫描）共用同一把 `_device_lock` 排队等待的结果。这里的计时
    从进入这个函数开始算，会把排队等待的时间也算进去（因为锁是在
    `api_get()` 内部才获取的，调用方等的其实是"排队+真正的网络耗时"这个
    合计），这样才能看出到底是网络请求本身变慢了，还是好几个请求排队排
    出来的。"""
    t0 = time.time()
    r = api_get("/camera", timeout=timeout, _retry=_retry)
    elapsed = time.time() - t0
    if elapsed > CAMERA_SLOW_WARN_SEC:
        print(f"[相机] /camera 这次实际耗时 {elapsed:.2f}s（含排队等待），"
              f"明显超过正常的 ~0.5s")
    if r and r.status_code == 200:
        arr = np.frombuffer(r.content, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR), r.content
    return None, None


# ╔══════════════════════════════════════════════╗
# ║               动画函数                        ║
# ╚══════════════════════════════════════════════╝

def play_happy_animation():
    set_expression("happy")
    set_led_mode("solid", *WARM_WHITE_RGB)
    move_servo(pitch=HAPPY_PITCH, speed=HAPPY_YAW_SPEED, mute=True)
    time.sleep(0.2)
    for _ in range(HAPPY_CYCLES):
        move_servo(yaw=HAPPY_YAW_RANGE, speed=HAPPY_YAW_SPEED, mute=True)
        time.sleep(HAPPY_CYCLE_DELAY)
        move_servo(yaw=-HAPPY_YAW_RANGE, speed=HAPPY_YAW_SPEED, mute=True)
        time.sleep(HAPPY_CYCLE_DELAY)
    # 动画结束后回正，确保摄像头对准人
    move_servo(yaw=0, pitch=450, speed=300, mute=True)

def play_tietie_animation():
    """"贴贴"：用开心的面部表情，但舵机动作是轻微小幅度抬头 3 次，不是
    "进入开心"时的完整摇头动画——碰屏幕、以及听到"小狗小狗"呼唤都会触发这个
    反应，是一次性的动作，不是状态切换动画，所以不复用 play_happy_animation()。

    只调 pitch、不传 yaw 会撞上"舵机噪音防误触发语音"一节记录的固件
    handleServo() 的 bug：省略的参数不是保持当前角度，而是重置成硬编码
    默认值（yaw 默认 0）——之前每次 move_servo() 都只传了 pitch，等于
    这三次抬头循环里每一下都把 yaw 偷偷拉回 0，把人脸追踪好不容易转到
    的角度抹掉，表现就是"贴贴"一结束舵机就回正、不再朝着人，紧接着的
    手势扫描窗口因此经常拍不到举着手指枪的那个人。修法跟"生气"序列的
    转头动作一样：先读一次当前 yaw，之后每次 move_servo() 都显式带上，
    把它钉住，不让固件的默认值悄悄覆盖。"""
    set_expression("happy")
    set_led_mode("solid", *WARM_WHITE_RGB)
    status = get_status()
    current_yaw = status.get("yaw", 0) if status else 0
    for _ in range(TIETIE_CYCLES):
        move_servo(yaw=current_yaw, pitch=TIETIE_PITCH_UP, speed=TIETIE_SPEED, mute=True)
        time.sleep(TIETIE_CYCLE_DELAY)
        move_servo(yaw=current_yaw, pitch=TIETIE_PITCH_DOWN, speed=TIETIE_SPEED, mute=True)
        time.sleep(TIETIE_CYCLE_DELAY)

def play_excited_animation():
    set_expression("excited")
    set_led_mode("rainbow", period_ms=EXCITED_LED_PERIOD_MS)
    for _ in range(EXCITED_CYCLES):
        move_servo(yaw=EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_HIGH, speed=EXCITED_YAW_SPEED, mute=True)
        time.sleep(EXCITED_CYCLE_DELAY)
        move_servo(yaw=-EXCITED_YAW_RANGE, pitch=EXCITED_PITCH_LOW, speed=EXCITED_YAW_SPEED, mute=True)
        time.sleep(EXCITED_CYCLE_DELAY)
    move_servo(yaw=0, pitch=450, speed=400, mute=True)

def play_sleepy_animation():
    set_expression("sleepy")
    set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=SLEEPY_LED_FADE_MS)
    move_servo(yaw=0, speed=200, mute=True)
    time.sleep(0.2)
    for p in SLEEPY_PITCH_STEPS:
        move_servo(pitch=p, speed=SLEEPY_SPEED, mute=True)
        time.sleep(SLEEPY_STEP_DELAY)

def play_privacy_animation():
    set_expression("privacy")
    move_servo(yaw=PRIVACY_YAW, pitch=PRIVACY_PITCH, speed=PRIVACY_SPEED, mute=True)
    set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=PRIVACY_LED_FADE_MS)

def play_idle_animation():
    set_expression("idle")
    go_home()
    # 常态的灯效关掉了（原来是"微弱暖白常亮"）——按要求直接熄灯。
    set_led(off=True)

def play_curious_animation():
    """好奇：显示表情即可——语音已经由后台流式监听（MicStream）捕捉完毕，
    这里不用再等录音。绿色呼吸灯暗示"正在识别/思考"，跟字幕是同一个窗口的
    两个信号（见 _run_conversation_turn_body() 的 try/finally）。"""
    set_expression("curious")
    set_led_mode("breathe", *THINKING_GREEN_RGB, period_ms=CURIOUS_LED_BREATHE_PERIOD_MS)

def play_thinking_animation():
    """思考：显示表情即可，持续时长就是 STT+LLM 实际处理耗时。LED 绿色呼吸灯
    延续好奇状态的效果，这里重新调一次只是为了保证独立进入 THINKING 时也
    一定是对的，不依赖"一定是从好奇过来的"这个假设。"""
    set_expression("thinking")
    set_led_mode("breathe", *THINKING_GREEN_RGB, period_ms=CURIOUS_LED_BREATHE_PERIOD_MS)

def play_sorry_animation():
    set_expression("sorry")
    move_servo(pitch=SORRY_PITCH, yaw=SORRY_YAW, speed=SORRY_SPEED, mute=True)
    set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)

def play_grieved_reaction():
    """捉迷藏没找到目标时的专属反应：动作和灯效跟"抱歉"状态完全一样（微
    低头 + 暖白闪烁），只是换成"委屈"表情。故意不复用 State.SORRY/
    transition()——那条路径别处仍然要保留"抱歉"表情给"责备"语音意图用
    （见 _run_conversation_turn_body() 的 scold 分支），这里只是借用同一套
    动作参数，不改 self.state。"""
    set_expression("grieved")
    move_servo(pitch=SORRY_PITCH, yaw=SORRY_YAW, speed=SORRY_SPEED, mute=True)
    set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)

def play_dizzy_animation():
    """被摇晃/拿起触发。故意不移动舵机——这个状态本来就是设备正在被外力
    晃动/托举的时候触发的，这时候主动转头只会跟外力对抗，徒增舵机负担，
    视觉上也会被摇晃动作本身淹没，没有实际意义。"""
    set_expression("dizzy")
    set_led_mode("breathe", *DIZZY_LED_RGB, period_ms=DIZZY_LED_BREATHE_PERIOD_MS)

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


def load_env_key(var_name):
    """从项目根目录 .env 手动解析指定变量名（不读环境变量，也不依赖
    python-dotenv —— 项目用的 conda 环境里没装这个包）。DEEPSEEK_API_KEY 和
    捉迷藏游戏用的 DASHSCOPE_API_KEY（Qwen-VL）共用同一份 .env、同一套
    解析逻辑。"""
    if not ENV_PATH.exists():
        print(f"  [.env] 未找到: {ENV_PATH}")
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == var_name:
            return value.strip().strip('"').strip("'")
    print(f"  [.env] 未找到 {var_name}")
    return None


def load_deepseek_api_key():
    return load_env_key("DEEPSEEK_API_KEY")


_audio_server = None

class _QuietAudioRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(AUDIO_DIR), **kwargs)

    def log_message(self, *args):
        pass  # 抑制访问日志


def ensure_audio_server():
    """启动一个 HTTP 文件服务器，让 StackChan 能下载要播放的音频。用
    ThreadingHTTPServer 而不是普通 HTTPServer——后者单线程同步处理请求，
    一旦某次连接卡住（比如设备端 WiFi 抖动、开了 TCP 连接但没有及时发出
    请求、或者下载中途被打断没干净关闭），唯一的服务线程会永久阻塞在等
    这一个连接的数据上，之后所有音频下载请求都进不来、也不会自己恢复——
    表现就是"能正常说几轮话，之后声音彻底消失"。ThreadingHTTPServer 给
    每个连接单开一个线程，一个卡住的连接不会拖累其它请求。"""
    global _audio_server
    if _audio_server is not None:
        return

    _audio_server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", AUDIO_SERVER_PORT), _QuietAudioRequestHandler
    )
    thread = threading.Thread(target=_audio_server.serve_forever, daemon=True)
    thread.start()
    print(f"  [音频服务器] http://{COMPUTER_IP}:{AUDIO_SERVER_PORT}/")


def start_play(path):
    """让 StackChan 通过 /play 开始下载并播放本地音频文件。/play 现在是
    非阻塞的（固件把下载+播放放进了后台 FreeRTOS 任务，见 firmware.ino 的
    playTaskFn()），这个调用只负责启动，几乎立刻返回——不等播完。等自然
    播完、或者中途被触摸打断，要靠 PuppyEngine.wait_for_playback() 轮询
    /status 的 playing 字段。"""
    filename = Path(path).name
    play_url = f"http://{COMPUTER_IP}:{AUDIO_SERVER_PORT}/{filename}"
    r = api_get(f"/play?url={play_url}", timeout=TIMEOUT)
    ok = r is not None and r.status_code == 200
    if not ok:
        print(f"  [播放] 启动失败: {r.status_code if r else '无响应'}")
    return ok

def stop_play():
    """叫停当前正在播放的音频（如果有的话）。固件端 g_playShouldStop 置位后
    立刻 M5.Speaker.stop() 硬切断，不是"播完手头这一块再停"；如果这时候
    根本没有播放任务在跑，固件那边只是个空操作，调用方不需要先判断"是不是
    真的在放"再决定要不要调这个。"""
    api_get("/play?stop=1")


_animalese_library = None

def ensure_animalese_library():
    """加载 animalese.wav 字母音频库（26 个英文字母各一段，8-bit/44100Hz/单
    声道），返回归一化到 [-1,1] 的 float32 数组。首次调用时如果本地还没有
    这个文件会先从 GitHub 下载一次（~172KB，之后常驻复用，不用每次合成都
    重新加载/下载）。跟 ensure_audio_server() 是同一个"全局单例+惰性初始化"
    模式。"""
    global _animalese_library
    if _animalese_library is not None:
        return _animalese_library

    if not ANIMALESE_LIBRARY_PATH.exists():
        print(f"  [animalese] 首次运行，正在从 GitHub 下载字母音频库...")
        urllib.request.urlretrieve(ANIMALESE_WAV_URL, ANIMALESE_LIBRARY_PATH)

    with wave.open(str(ANIMALESE_LIBRARY_PATH), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    # 8-bit unsigned → float32 (-1.0~1.0)，静音基线在 128
    samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    _animalese_library = (samples - 128.0) / 128.0
    return _animalese_library


def chinese_to_animalese_letters(text):
    """中文→拼音字母（音节之间加空格→animalese 里会变成停顿，让每个字听起来
    是分开的"词"），英文原样小写保留，其它字符（标点等）一律当空格处理。
    例如"想 零食" → "xiang ling shi"。缺 pypinyin 时中文字符会被跳过，只剩
    英文字母能正常发声。"""
    from pypinyin import pinyin, Style
    result = []
    for char in text:
        if '一' <= char <= '鿿':
            py = pinyin(char, style=Style.NORMAL, errors='ignore')
            if py and py[0]:
                if result and result[-1] != ' ':
                    result.append(' ')
                result.append(py[0][0])
                result.append(' ')
        elif char.isascii() and char.isalpha():
            result.append(char.lower())
        else:
            result.append(' ')
    return ' '.join(''.join(result).split())


def synthesize_animalese_audio(library, letters, pitch=ANIMALESE_PITCH):
    """把字母串合成为 animalese 音频（float32, 44100Hz）。算法直接移植自
    animalese.js：逐字母从字母库里截取一小段（ANIMALESE_OUTPUT_LETTER_SECS，
    只取库里原始 0.15s 片段的前半部分，念起来更快），非字母字符（含空格）
    留空白（保持 0，形成停顿）。"""
    lib_samples_per_letter = int(ANIMALESE_LIBRARY_LETTER_SECS * ANIMALESE_LIBRARY_SAMPLE_RATE)
    out_samples_per_letter = int(ANIMALESE_OUTPUT_LETTER_SECS * ANIMALESE_LIBRARY_SAMPLE_RATE)
    output = np.zeros(len(letters) * out_samples_per_letter, dtype=np.float32)
    for idx, ch in enumerate(letters.upper()):
        if not ('A' <= ch <= 'Z'):
            continue
        start = idx * out_samples_per_letter
        letter_offset = (ord(ch) - ord('A')) * lib_samples_per_letter
        for i in range(out_samples_per_letter):
            src_idx = letter_offset + int(i * pitch)
            if src_idx < len(library):
                output[start + i] = library[src_idx]
    return output


def tts_to_wav(text, out_stem):
    """合成 animalese 拟声词音频，转换成 16kHz/单声道/16bit WAV，返回文件
    路径（失败返回 None）。纯本地计算，没有网络往返。"""
    wav_path = AUDIO_DIR / f"{out_stem}.wav"
    try:
        library = ensure_animalese_library()
        letters = chinese_to_animalese_letters(text)
        audio_44k = synthesize_animalese_audio(library, letters)

        # 44100Hz → TTS_SAMPLE_RATE(16000)：跟无线麦克风那边一样，按
        # math.gcd 现算 up/down 比例，不要硬编码 441/160——虽然这两个采样率
        # 都是固定常量不会变，但保持同一套写法，以后改 TTS_SAMPLE_RATE 时
        # 不用记得来回改这里。
        g = math.gcd(ANIMALESE_LIBRARY_SAMPLE_RATE, TTS_SAMPLE_RATE)
        audio_16k = resample_poly(audio_44k, TTS_SAMPLE_RATE // g, ANIMALESE_LIBRARY_SAMPLE_RATE // g)
        audio_16k = np.clip(audio_16k * ANIMALESE_VOLUME_BOOST, -1.0, 1.0)
        pcm_bytes = (audio_16k * 32767).astype(np.int16).tobytes()

        wav_path.write_bytes(pcm_to_wav_bytes(pcm_bytes, sample_rate=TTS_SAMPLE_RATE))
        return wav_path
    except Exception as e:
        print(f"  [TTS] animalese 合成失败: {e}")
        return None


def is_calling_puppy(text):
    """判断识别结果是不是在叫它的名字（"小狗小狗"这种呼唤语），不是在问
    一个提到了"小狗"这个词的问题。直接走固定判断、不经过 LLM 语义分类：
    这是一句字面意义上的招呼语，不需要 LLM 来"理解"，省一次网络往返，也
    不会被 SYSTEM_PROMPT 的四路意图分类（qa_simple/qa_complex/praise/scold）
    意外吞掉、走成复杂回应。
    先剥掉标点/空格/语气词只留中文字符（SenseVoice 识别结果可能带
    "小狗小狗！""小狗，小狗～"这类变体），再要求剩下的内容很短（避免"小狗
    你说小狗喜不喜欢吃肉"这种长句子里恰好出现两次"小狗"被误判）且至少出现
    两次"小狗"。"""
    cleaned = re.sub(r"[^一-鿿]", "", text or "")
    return len(cleaned) <= 8 and cleaned.count("小狗") >= 2


GAME_REJECTION_PATTERNS = [
    "不是这个", "不是那个", "不对", "看错了", "认错了",
    "换一个", "换个", "重新来", "重新看", "不是这样", "不是它",
]


def is_registration_rejection(text):
    """判断这句话是不是在否定捉迷藏游戏刚识别到的物品、要求重新看一次
    （比如"不是这个""看错了""换一个"）。直接走关键词匹配，不经过 LLM 语义
    分类——这类否定短语说法有限，且只在游戏注册阶段一个很窄的窗口期内才会
    去检查，没必要为了这个再多等一次网络往返（跟 is_calling_puppy() 同样
    的设计取舍）。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return any(p in cleaned for p in GAME_REJECTION_PATTERNS)


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


def sanitize_keywords(keywords):
    """qa_complex 的关键词是 AAC 按钮式的短词/短语，SYSTEM_PROMPT 明确要求
    每个 1-3 个字、绝对不能是完整句子——但 LLM 不是每次都遵守。有两种不同的
    违规，分别处理：

    ①一整句话直接塞进 keywords 数组的某一项里（没有任何拆分）。这种违规在
    host 端解析 JSON 时完全合法（就是个正常的字符串数组），不会被 ask_llm()
    的异常处理拦下来，一路传到 speak_keywords()，把这一项当成"一个关键词"
    合成语音播放——表现出来就是"小狗突然说了一整句完整的话"。这里做最后
    一道防线：超过 KEYWORD_MAX_CHARS 的直接丢弃，不做截断（截断出来的半截
    句子听起来更奇怪，不如跳过这一个，用剩下合规的关键词照常回应）；如果
    全部超长（比如就一个词、还刚好是句子），保留第一个并截断，总比什么都
    不说要好。

    ②SYSTEM_PROMPT 第 6 条要求 LLM 先把回答压缩成"2-4 个关键词（空格分隔）"
    的中间形式，再拆成 JSON 数组——LLM 有时会漏掉"拆"这一步，直接把这个
    空格分隔的中间形式整个塞进数组的一项里（比如 ["外面 好 风"] 而不是
    ["外面","好","风"]）。这种情况每一项本身字符数通常不超过
    KEYWORD_MAX_CHARS，不会被①的超长检查拦下来，结果是这几个词被合成成
    一整段连续语音一次性播完，按钮却只按一次——用户听起来像"所有关键词一次
    性说出来"，跟"每念一个词按钮按一下"的设计脱节。关键词本身不应该含空格
    （空格只在压缩阶段的中间形式里出现），所以先按空格把每一项展开成多项，
    再套用①的长度过滤——不管 LLM 有没有老老实实完成"拆"这一步，出来的
    每一项都只会是一个词。"""
    expanded = []
    for kw in keywords:
        if kw:
            expanded.extend(kw.split())
    cleaned = [kw for kw in expanded if kw and len(kw) <= KEYWORD_MAX_CHARS]
    if cleaned:
        return cleaned
    if expanded and expanded[0]:
        print(f"[对话] 关键词全部超长（疑似 LLM 没拆句子），截断保底: {expanded[0][:KEYWORD_MAX_CHARS]}")
        return [expanded[0][:KEYWORD_MAX_CHARS]]
    return []


# ╔══════════════════════════════════════════════╗
# ║       捉迷藏游戏：颜色直方图 + 视觉大模型      ║
# ╚══════════════════════════════════════════════╝

def extract_color_hist(img):
    """从 cv2 解码后的 BGR 图像提取归一化的 HSV 颜色直方图（H 180 bin、S 256
    bin），用来做"这两张照片颜色分布像不像"的粗筛。只看颜色不看形状/位置，
    足够便宜到可以在扫描循环的每一步都算一次。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [180, 256], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def compare_hist(ref_hist, scan_hist):
    """比较两个颜色直方图的相关度，返回 -1.0~1.0（越接近 1 越像）。"""
    return cv2.compareHist(ref_hist, scan_hist, cv2.HISTCMP_CORREL)


def call_vision_llm(image_bytes, prompt, api_key, timeout=GAME_VLM_TIMEOUT_SEC):
    """调用 Qwen-VL（阿里云 DashScope 的 OpenAI 兼容端点）识别一张图片，返回
    模型的文字回答；任何失败（没配置 key、网络错误、超时、响应格式不对）都
    统一返回 None，不抛异常——调用方（捉迷藏游戏的注册/确认阶段）都设计成
    在 VLM 不可用时能优雅降级（注册阶段退化成没有文字描述、扫描阶段退化成
    只看颜色直方图），不能假设这里一定能拿到结果。"""
    if not api_key:
        return None
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    try:
        r = requests.post(
            QWEN_VL_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": QWEN_VL_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 100,
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        print(f"  [VLM] 请求失败: {e}")
        return None
    if r.status_code != 200:
        print(f"  [VLM] API 返回 {r.status_code}: {r.text[:200]}")
        return None
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [VLM] 响应格式不对: {e!r}  body={r.text[:200]}")
        return None


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


def find_wireless_mic_device():
    """在 sounddevice 能看到的输入设备里，按 WIRELESS_MIC_NAME_HINT 子串找无线
    麦克风接收器，优先找 Windows WASAPI host API 下的那个实例（同一个物理设备
    在 MME/DirectSound/WASAPI/WDM-KS 下都会各出现一次，WASAPI 延迟最低、由
    Windows 音频引擎在共享模式下管理，重采样也更好支持）。找不到返回 None。"""
    wasapi_idx = None
    for i, api in enumerate(sd.query_hostapis()):
        if api["name"] == "Windows WASAPI":
            wasapi_idx = i
            break
    if wasapi_idx is None:
        return None
    for i, dev in enumerate(sd.query_devices()):
        if (dev["hostapi"] == wasapi_idx and dev["max_input_channels"] > 0
                and WIRELESS_MIC_NAME_HINT in dev["name"]):
            return i
    return None


class MicStream:
    """电脑本地无线麦克风常驻采集 + 缓冲 + 简单语音活动检测（VAD）。

    早期版本是 StackChan 机身麦克风通过 /stream?port=N 主动连过来推流 PCM，
    这个类在 host 端常驻监听 TCP 端口接收；现在换成直接用 sounddevice 打开
    电脑上的无线麦克风接收设备，音频源变了，但下面这套"滚动缓冲区 + 按
    STREAM_CHUNK_SECONDS 分段算 RMS 判定说话/说完"的逻辑完全没变——`_feed()`/
    `_process_chunk()`/`_emit_utterance()` 只认裸 PCM 字节，不关心这些字节
    是从 TCP socket 收的还是从本地音频设备回调收的。

    - `_sd_callback()` 是 sounddevice 在专门的音频线程上周期性调用的回调：
      拿到的是设备原生采样率/声道数的音频（这个无线麦克风接收器在 WASAPI
      下实测是 48kHz 立体声，不是常见的 16kHz 单声道，Windows 共享模式不
      接受直接以任意采样率打开），先降混成单声道，再用 scipy 的
      resample_poly 重采样到 self.sample_rate（16kHz），最后喂进 _feed()。
    - 跟旧版一样，音频的写入全部发生在同一个线程里（sounddevice 的音频
      回调线程），所以内部状态不需要加锁；跟主线程之间唯一的交接点是线程
      安全的 _utterance_queue。
    - 设备断开/找不到的情况目前没有自动重连——如果无线麦克风接收器中途被
      拔掉，需要重启这个程序，跟旧版"断线自动重连"比是一个已知的能力
      倒退，以后有需要再补。
    """

    def __init__(self, sample_rate=16000,
                 buffer_seconds=STREAM_BUFFER_SECONDS,
                 chunk_seconds=STREAM_CHUNK_SECONDS,
                 silence_seconds=STREAM_SILENCE_SECONDS,
                 preroll_seconds=STREAM_PREROLL_SECONDS,
                 rms_threshold=STREAM_RMS_THRESHOLD):
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
        self._stream = None        # sounddevice.InputStream 实例
        self._resample_up = 1
        self._resample_down = 1
        self._muted = False        # 见 set_muted()
        self.connected = False     # 仅供主循环/调试查看，不用于同步

    def start(self):
        if self._running:
            return
        device_idx = find_wireless_mic_device()
        if device_idx is None:
            print(f"[本地麦克风] 没找到名字包含 {WIRELESS_MIC_NAME_HINT!r} 的 "
                  "WASAPI 输入设备，语音唤醒不可用（检查无线麦克风接收器是否"
                  "插好、Windows 是否识别到）")
            return
        dev = sd.query_devices()[device_idx]
        native_sr = int(round(dev["default_samplerate"]))
        g = math.gcd(native_sr, self.sample_rate)
        self._resample_up = self.sample_rate // g
        self._resample_down = native_sr // g
        channels = dev["max_input_channels"]
        # 100ms 一个回调块：够大，让 resample_poly 每次有足够样本做滤波；
        # 也够小，不会给 VAD/字幕引入明显的额外延迟。
        blocksize = max(1, int(native_sr * 0.1))
        try:
            self._stream = sd.InputStream(
                device=device_idx, samplerate=native_sr, channels=channels,
                dtype="int16", blocksize=blocksize, callback=self._sd_callback)
            self._stream.start()
        except Exception as e:
            print(f"[本地麦克风] 打开输入流失败: {e}")
            self._stream = None
            return
        self._running = True
        self.connected = True
        print(f"[本地麦克风] 已打开 {dev['name']!r}（原生 {native_sr}Hz/"
              f"{channels}ch，重采样到 {self.sample_rate}Hz/单声道）")

    def stop(self):
        """程序退出时调用：停掉音频流。"""
        self._running = False
        self.connected = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

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

        这个方法从主线程（PuppyEngine 的后台增量识别线程）调用，跟 sounddevice
        的音频回调线程之间不像 take_utterance() 靠 _utterance_queue 那样有
        专门的线程安全交接。对一段仅用于展示的字幕来说这个折衷可以接受：最坏
        情况是读到的切片跟 _speech_active 标志之间差了一两帧（比如刚好在这
        一刻判定说完了），不会崩溃或者数据错乱，下一次调用就会用最新状态
        纠正过来。"""
        if not self._speech_active:
            return None
        base = self._total - len(self._buffer)
        lo = max(0, self._speech_start_abs - base)
        return bytes(self._buffer[lo:])

    def set_muted(self, muted: bool):
        """StackChan 播 TTS 时调用 True，播完/被打断后调用 False。机身麦克风
        跟喇叭共用同一个 I2S 外设，播放期间物理上就听不到自己的声音（见
        firmware.ino 的 g_i2sMutex）；这个无线麦克风是电脑本地独立的物理
        设备，没有这层物理隔离，房间里放出来的 TTS 声音会被它原样录进去，
        不静音的话会把 StackChan 自己在说的话当成用户在说话，误触发新一轮
        对话、把正在播的这句打断。静音期间 `_sd_callback()` 直接丢弃收到的
        数据，不写进缓冲区、不参与 VAD；取消静音后从下一帧数据开始正常
        累积，不需要处理边界（内部状态全部基于相对字节数，不依赖墙钟
        时间）。静音的同时顺手清掉"正在说话"的状态，避免静音前后的音频
        被当成同一段拼起来。"""
        self._muted = muted
        if muted:
            self._speech_active = False
            self._quiet_chunks = 0

    # ---------- 内部：采集 + 缓冲 + VAD ----------

    def _sd_callback(self, indata, frame_count, time_info, status):
        if status:
            print(f"[本地麦克风] sounddevice 状态告警: {status}")
        if self._muted:
            return
        mono = indata.mean(axis=1)
        resampled = resample_poly(mono, self._resample_up, self._resample_down)
        self._feed(resampled.astype(np.int16).tobytes())

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
        rms = _pcm_rms(segment)
        print(f"[本地麦克风] 捕捉到一段语音，时长 {dur:.2f}s，RMS={rms:.0f}")
        self._utterance_queue.put(segment)


def _landmark_dist(a, b):
    """三维距离，不是只看画面里的 x/y——手指枪本来就是"朝前指"的手势，
    枪管（食指）天然容易正对着摄像头的光轴指过去，这个方向上的伸展会被
    2D 投影严重压扁：食指明明伸得笔直，但因为主要在"纵深"方向上伸展、
    在画面平面上的水平/垂直位移很小，wrist→指尖 的 2D 距离会显著小于
    真实的 3D 长度，实测下来 index_ratio 经常卡在 0.8~1.15、摸不到
    FINGER_EXTEND_RATIO(1.2) 的门槛，而且不是偶尔一帧的抖动，是连续
    一整段测试期间稳定地测不准——是系统性的投影偏差，不是噪声。
    MediaPipe Hand Landmarker 的每个关键点自带 z（以手腕为原点的相对
    深度，跟 x/y 同一个量纲），加上这一维就是真正的手部关键点间 3D
    距离，不再受"手指恰好指向镜头"这个角度的影响。"""
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5


def classify_finger_gun_pose(lm):
    """判断一组 21 个 MediaPipe 手部关键点是不是"手指枪"姿势，返回一个
    dict：每个子条件的数值和布尔判定，外加最终结果 'is_gun'。host/
    gesture_test.py（独立诊断脚本）和 PuppyEngine.check_gesture()
    共用这一份实现，不要各自维护一份——之前踩过这个坑的前身版本（直接比较
    landmark 的 x/y 坐标差）被实测证明对手离摄像头的距离、手在画面里的
    旋转角度都很敏感：同一个手指枪手势，换个距离/角度，坐标差的绝对值会
    飘出阈值范围，两次独立诊断测试（用同一个脚本、同一段代码）一次连续
    命中 5 帧、另一次全程一次都不命中，且失败模式集中在"中指已经弯了，
    但无名指/小指判定成没弯"——说明问题不是使用者没摆稳，是判定方法本身
    不够稳健。

    改用**比例**而不是绝对坐标差：
      - 手掌尺度参考 `hand_scale` = 手腕(landmark 0)到中指根部(landmark 9)
        的欧氏距离，后面所有距离都换算成相对这个尺度的比例，不管手离
        摄像头多远、画面里手多大，比例关系基本不变。
      - 每根手指的"伸展比例" = 指尖到手腕的距离 / 对应 PIP 关节到手腕的
        距离——明显大于 1 说明指尖比关节离手腕更远（伸直），明显小于 1
        说明指尖折回来比关节还靠近手腕（弯曲）。这个比例只依赖点与点的
        相对距离，手在画面里怎么平移/旋转都不影响，比直接比较 y 坐标
        （隐含假设"手是竖直朝上举着的"）稳健得多。
      - 食指：伸展比例 > FINGER_EXTEND_RATIO 判定为伸直。
      - 中指/无名指/小指：伸展比例 < FINGER_CURL_RATIO 判定为弯曲。
        **中指必须弯曲，无名指/小指里至少一根弯曲就算数**——这三根手指
        的"至少两根弯"不是随便挑的"三选二"：无名指、小指天生比中指更难
        独立弯曲（肌腱互相牵连），实测数据也证实很多人自然摆出的手指枪
        这两根手指弯曲程度不如中指，要求两根都弯太苛刻，容易把真实的
        手指枪判定成"不是"；但这不代表中指本身可以不弯——如果中指也
        允许不弯，"食指+中指一起伸直、无名指+小指弯曲"（比如"耶"的
        剪刀手/胜利手势）会因为无名指+小指凑够 2 根弯曲，被误判成
        手指枪（这是实机测试暴露出来的真实误报，不是假设）。所以中指
        是硬性要求，无名指/小指只挑一根即可，不是三选二那么宽松。
      - 拇指：拇指指尖(4)到食指根部(5)的距离，换算成相对 `hand_scale`
        的比例，大于 FINGER_GUN_THUMB_SPREAD_RATIO 判定为张开（不是贴着
        手掌）。
    """
    wrist = lm[0]
    hand_scale = max(_landmark_dist(wrist, lm[9]), 1e-6)

    def extend_ratio(tip_idx, pip_idx):
        return _landmark_dist(wrist, lm[tip_idx]) / max(_landmark_dist(wrist, lm[pip_idx]), 1e-6)

    index_ratio = extend_ratio(8, 6)
    middle_ratio = extend_ratio(12, 10)
    ring_ratio = extend_ratio(16, 14)
    pinky_ratio = extend_ratio(20, 18)
    thumb_spread_ratio = _landmark_dist(lm[4], lm[5]) / hand_scale

    index_straight = index_ratio > FINGER_EXTEND_RATIO
    middle_curled = middle_ratio < FINGER_CURL_RATIO
    ring_curled = ring_ratio < FINGER_CURL_RATIO
    pinky_curled = pinky_ratio < FINGER_CURL_RATIO
    curled_count = sum([middle_curled, ring_curled, pinky_curled])
    thumb_spread = thumb_spread_ratio > FINGER_GUN_THUMB_SPREAD_RATIO

    return {
        "index_straight": index_straight, "index_ratio": index_ratio,
        "thumb_spread": thumb_spread, "thumb_spread_ratio": thumb_spread_ratio,
        "middle_curled": middle_curled, "middle_ratio": middle_ratio,
        "ring_curled": ring_curled, "ring_ratio": ring_ratio,
        "pinky_curled": pinky_curled, "pinky_ratio": pinky_ratio,
        "curled_count": curled_count,
        # 中指必须弯，无名指/小指只要有一根弯就行——不是"三根里任意两根"，
        # 见上面文档字符串里"剪刀手会被误判"那段实测教训。
        "is_gun": index_straight and thumb_spread and middle_curled and (ring_curled or pinky_curled),
    }


def classify_open_pinch_pose(lm):
    """判断一组 21 个 MediaPipe 手部关键点是"张开手掌"还是"五指捏拢"，给
    "再见"手势的两种触发方式（挥手/五指捏住再放开）共用，跟
    classify_finger_gun_pose() 是同一份职责划分：这里只负责单帧姿态判定，
    跨帧的"挥手来回摆动"/"先捏后放"时序判断留给调用方（PuppyEngine.
    check_gesture()）。同样用**比例**而不是绝对坐标差（原因见
    classify_finger_gun_pose() 顶部的详细说明），手掌尺度 hand_scale 用
    同一个定义（手腕到中指根部的距离）。

      - "张开"：食指/中指/无名指/小指四根手指的伸展比例都超过
        FINGER_SPREAD_OPEN_RATIO——比手指枪只判断食指单独一根更严格，要求
        四指同时达标才算手掌张开。
      - "捏拢"/"放开"：五个指尖（拇指4/食指8/中指12/无名指16/小指20）到
        它们质心的平均距离，相对 hand_scale 的比例——小于
        PINCH_TIP_SPREAD_RATIO 算捏拢，大于 RELEASE_TIP_SPREAD_RATIO 算
        放开（两个阈值故意留出间隔，理由见常量定义处的注释）。
      - 额外带回 palm_x（landmark 9，中指根部，比指尖更稳定）和
        hand_scale，供调用方追踪挥手时手掌的水平位置/换算摆动幅度阈值。
    """
    wrist = lm[0]
    palm = lm[9]
    hand_scale = max(_landmark_dist(wrist, palm), 1e-6)

    def extend_ratio(tip_idx, pip_idx):
        return _landmark_dist(wrist, lm[tip_idx]) / max(_landmark_dist(wrist, lm[pip_idx]), 1e-6)

    index_ratio = extend_ratio(8, 6)
    middle_ratio = extend_ratio(12, 10)
    ring_ratio = extend_ratio(16, 14)
    pinky_ratio = extend_ratio(20, 18)
    is_open = (
        index_ratio > FINGER_SPREAD_OPEN_RATIO and middle_ratio > FINGER_SPREAD_OPEN_RATIO
        and ring_ratio > FINGER_SPREAD_OPEN_RATIO and pinky_ratio > FINGER_SPREAD_OPEN_RATIO
    )

    # 质心距离也算上 z——跟 _landmark_dist() 改成 3D 距离是同一个理由，
    # 指尖聚拢/散开同样可能主要发生在朝向摄像头的纵深方向上（比如手整体
    # 前后运动时指尖投影位置变化很小），只看 x/y 会漏掉这部分变化。
    tip_idxs = (4, 8, 12, 16, 20)
    tips = [lm[i] for i in tip_idxs]
    centroid_x = sum(t.x for t in tips) / len(tips)
    centroid_y = sum(t.y for t in tips) / len(tips)
    centroid_z = sum(t.z for t in tips) / len(tips)
    avg_tip_dist = sum(
        ((t.x - centroid_x) ** 2 + (t.y - centroid_y) ** 2 + (t.z - centroid_z) ** 2) ** 0.5
        for t in tips
    ) / len(tips)
    tip_spread_ratio = avg_tip_dist / hand_scale
    is_pinched = tip_spread_ratio < PINCH_TIP_SPREAD_RATIO
    is_released = tip_spread_ratio > RELEASE_TIP_SPREAD_RATIO

    return {
        "is_open": is_open, "index_ratio": index_ratio, "middle_ratio": middle_ratio,
        "ring_ratio": ring_ratio, "pinky_ratio": pinky_ratio,
        "tip_spread_ratio": tip_spread_ratio, "is_pinched": is_pinched, "is_released": is_released,
        "palm_x": palm.x, "hand_scale": hand_scale,
    }


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
        # detect_face_once() 本身耗时可达 1~2s（/camera 下载 + mediapipe 推理，
        # 见 check_face()/retrack_face() 改成后台线程那段说明），必须靠这个锁
        # 防止两处调用（tick() 触发的后台 worker、以及 scan_for_face() /
        # track_face_once() / _face_person_before_excited() 这些仍然同步阻塞
        # 调用的路径）同时调用同一个 self.face_detector 实例——跟 self._asr_lock
        # 保护 SenseVoice 并发调用是同一个理由。_face_worker_busy 防止上一个
        # 后台检测还没跑完时又叠加启动下一个。
        self.face_detect_lock = threading.Lock()
        self._face_worker_busy = False

        # 手势检测（"手指枪" → 装死，见 check_gesture()）：跟人脸检测是两个
        # 独立的 MediaPipe 模型实例，互不共享状态，在开机初始化时就创建好，
        # 不在每次检测时重新创建——跟 self.face_detector 同一个模式。
        hand_opts = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=HAND_MODEL_PATH),
            num_hands=1,
        )
        self.hand_landmarker = vision.HandLandmarker.create_from_options(hand_opts)
        self.gesture_scan_until = 0.0
        self.last_gesture_check = 0
        # 最近 FINGER_GUN_WINDOW_FRAMES 帧里"是不是手指枪"的滑动窗口
        # （见 FINGER_GUN_WINDOW_FRAMES 常量定义处的说明），命中次数够
        # FINGER_GUN_CONFIRM_HITS 就触发，不要求连续。
        self.finger_gun_history = deque(maxlen=FINGER_GUN_WINDOW_FRAMES)
        # "再见"手势（挥手/五指捏住再放开）状态，跟手指枪共用同一个扫描
        # 窗口和同一次检测结果，见 check_gesture()。wave_x_history 是张开
        # 手掌时手掌水平位置的滚动历史（(时间戳, x, 手掌尺度) 三元组），
        # 用于挥手判定；goodbye_pinch_since 非 0 表示"已经捏拢、正在等
        # 放开"，等到了或者超时（PINCH_RELEASE_TIMEOUT_SEC）都会清零。
        self.wave_x_history = []
        self.goodbye_pinch_since = 0.0
        self.last_face_seen_time = 0

        # 一次"来访"期间是否已经打过招呼——第一次进 HAPPY 播完整开心动画后
        # 置 True，期间人脸丢失/重新检测到都不再重复播放动画，只在真正离开
        # 很久（进 SLEEPY/PRIVACY）之后才重置，见 enter_happy()/transition()。
        self.session_active = False

        # 触摸检测
        self.last_touch_poll = 0
        self.touch_pressed = False
        # 长按进/出隐私每次连续按住只应该触发一次——不然一次持续 3 秒以上的
        # 长按会在越过阈值那一刻先触发一次（比如"进入隐私"），手指还没松开、
        # held_ms 继续增长，下一次轮询看到状态已经是 PRIVACY，又会立刻判定
        # 成"退出隐私"，同一次按住里进出闪一下。靠这个 flag 记"这一次连续
        # 按住是否已经处理过"，在 check_touch() 检测到松开（held_ms 归零）
        # 时重置。
        self.privacy_hold_fired = False
        # 头顶双击 / 屏幕点击是固件端的单调递增计数器（见 firmware.ino 的
        # g_headDoubleTapCount/g_screenTapCount），host 端记上一次看到的值，
        # 靠比较有没有变化判断"这段时间里有没有发生过一次新的手势"——不能
        # 从 0 起算，不然设备没重启、之前测试时点过的次数会在下一次
        # check_touch() 里被误判成"现在刚发生"；第一次真正拿到读数时只
        # 校准基线，不当成事件触发。
        self.last_double_tap_count = None
        self.last_screen_tap_count = None
        # 屏幕点击的冷却时间戳，见 SCREEN_TAP_DEBOUNCE_SEC 定义处的说明——
        # 防止触摸传感器把同一次物理点按弹跳成 screen_tap_count 连续多次
        # 递增，导致"贴贴"动画被连续重放好几遍。0.0 表示还没触发过。
        self.last_screen_tap_trigger_time = 0.0

        # "晕"状态：摇晃/拿起信号消失后，不立刻转"兴奋"，要在"晕"表情上
        # 再停留 DIZZY_LINGER_SEC 秒——None 表示信号还在（或者还没进过一次
        # "晕"），tick() 里 is_shaking 第一次变 false 时记下这一刻的时间戳，
        # 之后每个 tick 比较过去了多久，见 tick() 的 DIZZY 分支。
        self.dizzy_shake_stopped_at = None

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

        # 捉迷藏游戏：Qwen-VL key 是可选的——没配置也不影响游戏能不能玩，
        # 只是"精确确认"这一步会自动降级成只看颜色直方图，见 call_vision_llm()。
        self.qwen_api_key = load_env_key("DASHSCOPE_API_KEY")
        if self.qwen_api_key:
            print("[引擎] Qwen-VL API key 已从 .env 加载（捉迷藏游戏可用精确确认）")
        else:
            print("[引擎] 没有 Qwen-VL API key，捉迷藏游戏会退化成只用颜色直方图判断")
        self.game_ref_hist = None
        self.game_ref_desc = None

        # 定时提醒里"想出去玩"这类提醒的关键词：可选增强，没配置/请求失败
        # 都会退化成 REMINDERS 里各条目自带的固定关键词，见 get_weather_
        # keywords()。
        self.qweather_api_key = load_env_key("QWEATHER_API_KEY")
        self.qweather_api_host = load_env_key("QWEATHER_API_HOST")
        if self.qweather_api_key and self.qweather_api_host:
            print("[引擎] 和风天气 key 已从 .env 加载（提醒关键词会结合天气）")
        else:
            print("[引擎] 没有和风天气 key/host，提醒关键词会用固定文本，不结合天气")
        # animalese 字母库（172KB，读一次转成 numpy 数组，耗时可忽略）：在这里
        # 同步加载一次，保证第一次 speak_keywords() 调用时不用再付一次首次
        # 加载的延迟——不像 SenseVoice 模型/TTS 预热那样耗时到需要放后台线程。
        print("[引擎] 加载 animalese 字母音频库...")
        ensure_animalese_library()
        # 捉迷藏游戏固定词汇（"小狗""看""闭眼"、倒计时数字）的 TTS 预热缓存，
        # 见 GAME_FIXED_PHRASES 和 _prewarm_game_tts()。后台线程跑，不拖慢
        # 引擎启动；万一游戏在预热完成前就被触发，_game_tts() 会自动退化成
        # 现合成，不影响正确性。
        self._game_tts_cache = {}
        threading.Thread(target=self._prewarm_game_tts, daemon=True).start()

        ensure_audio_server()

        # 语音流式监听：现在音频源是电脑本地的无线麦克风（见 MicStream 类
        # 头部注释），不再需要告诉 StackChan 开始推流。
        self.mic_stream = MicStream()
        self.mic_stream.start()

        # 增量字幕：用户还在说话期间，周期性地把目前录到的内容整段重新识别
        # 一次，见 _partial_transcribe_loop()。独立线程运行，不占用主 tick()
        # 循环的时间，也不会拖慢触摸/人脸轮询。
        self._running = True
        if PARTIAL_TRANSCRIBE_ENABLED:
            self._partial_transcribe_thread = threading.Thread(
                target=self._partial_transcribe_loop, daemon=True
            )
            self._partial_transcribe_thread.start()

        # 定时提醒 + 生气催促（见 _check_reminders()/_play_angry_reminder()）。
        # self._reminder_cooldowns 的 key 是 REMINDERS 里的 label，value 是
        # 上次触发的 time.time()，每次检查前清理超过 REMINDER_COOLDOWN_SEC
        # 的旧条目，避免无限增长。self._reminder_recheck_target 非 None 时
        # 表示"有一条提醒发出去了，正在等 10 分钟看主人是否还在"；同一时间
        # 只跟踪一条复查，_check_reminders() 里会用这个当前置条件，避免两条
        # 提醒的复查窗口撞在一起互相覆盖。
        self._reminder_cooldowns: dict[str, float] = {}
        self._reminder_recheck_target: float | None = None
        self._presence_samples: list[bool] = []
        self._presence_last_sample_time: float = 0.0
        self._presence_window_start: float = 0.0
        self._reminder_pending_label: str | None = None
        # 喝水/出去玩动态链条的下一次触发时间戳——跟吃饭提醒没有依赖关系，
        # 引擎启动时就直接排好第一次触发时间，之后每次触发完都会重新排
        # 下一次，见 _check_dynamic_reminder()。落在活跃时段之外时，
        # _check_dynamic_reminder() 会在真正到点检查时自动重新计算成活跃
        # 时段起点，这里不需要预先判断当前是不是活跃时段。
        self._dynamic_next_time: float | None = time.time() + _next_dynamic_delay()

        print("[引擎] 小狗行为引擎 v4 启动！")
        print(f"[引擎] 当前状态: {self.state.value}")
        print("[引擎] 碰一下屏幕 → 贴贴（摸头反应）")
        print("[引擎] 头顶双击 → 兴奋")
        print(f"[引擎] 头顶长按满 {PRIVACY_HOLD_SEC}s → 进/出隐私（隐私状态下头顶双击也能 → 兴奋，退出隐私）")
        print("[引擎] 呼唤\"小狗小狗\" → 开心")
        print("[引擎] 说\"我们玩捉迷藏吧\" → 捉迷藏找物品游戏（游戏中长按头顶可中途退出）")
        print(f"[引擎] 持续流式监听 (rms >= {STREAM_RMS_THRESHOLD}) → 扫描找人 → 开心 → 思考 → 回应（隐私状态下忽略语音）")
        _presence_window_min = PRESENCE_WINDOW_SAMPLES * PRESENCE_SAMPLE_INTERVAL_SEC / 60
        print(f"[引擎] 固定时间提醒 {len(REMINDERS)} 条（吃饭），不进在场监测，不会生气")
        print(f"[引擎] 喝水/出去玩：跟吃饭提醒独立，{DYNAMIC_INTERVAL_MIN_SEC/60:.0f}~{DYNAMIC_INTERVAL_MAX_SEC/60:.0f} 分钟随机间隔触发，"
              f"{DYNAMIC_ACTIVE_HOUR_START}:00~{DYNAMIC_ACTIVE_HOUR_END}:00 活跃，按天气二选一，"
              f"发出后约 {_presence_window_min:.0f} 分钟在场监测窗口内在场率 > {PRESENCE_ANGRY_THRESHOLD:.0%} → 生气催促（双击头顶原谅）")
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
        elif new_state == State.PRIVACY:
            play_privacy_animation()
            self._settle_privacy_mic()
        elif new_state == State.IDLE:     play_idle_animation()
        elif new_state == State.CURIOUS:  play_curious_animation()
        elif new_state == State.THINKING: play_thinking_animation()
        elif new_state == State.SORRY:    play_sorry_animation()
        elif new_state == State.DIZZY:    play_dizzy_animation()

    def _settle_privacy_mic(self):
        """转进隐私姿势要转动舵机，转动本身的机械噪音很容易被 MicStream 的
        RMS 阈值误判成"有人在说话"（用户实测反馈：舵机转完以后又莫名其妙
        重新进了开心——追查下来是这段噪音被当成一次真实的语音触发，绕过了
        `check_voice_wake()` 里给隐私状态关摄像头那条分支，直接跑完一遍对话
        流程，SenseVoice 认不出这段噪音里有什么词，`_settle_happy()` 收尾时
        又刚好还能看到人脸，于是就地满血复活成开心）。

        这里不是瞎猜一个固定时长去等，而是轮询 `/status` 直到舵机真的转到
        （在容差范围内）隐私姿势的目标角度——`PRIVACY_SPEED` 具体对应多久
        没有精确文档，猜大概率会猜错。转到位之后还不能立刻处理，因为 VAD
        要等连续 `STREAM_SILENCE_SECONDS` 秒的安静才会把"这一段"关闭并放进
        队列——舵机停转的那一刻，噪音触发的那段"话"可能还没被判定为"说完
        了"，这时候去看队列多半还是空的。等够这段时间后，把 MicStream 队列
        里可能已经攒着的那一段直接丢弃，不进入对话流程；如果这段时间里真的
        有人在说话（不是噪音），也不要紧——`check_voice_wake()` 本来就是每
        个 tick 都在查，丢掉这一段之后，用户接下来说的任何一句新的话依然会
        被正常捕捉到，不会被永久性地"聋掉"。"""
        deadline = time.time() + 5.0
        settled = False
        while time.time() < deadline:
            status = get_status()
            if status:
                yaw_ok = abs(status.get("yaw", 0) - PRIVACY_YAW) <= 20
                pitch_ok = abs(status.get("pitch", 0) - PRIVACY_PITCH) <= 20
                if yaw_ok and pitch_ok:
                    settled = True
                    break
            time.sleep(0.2)
        if not settled:
            print("[隐私] 等舵机转到位超时，按最长等待时间处理")

        time.sleep(STREAM_SILENCE_SECONDS + 0.3)
        discarded = self.mic_stream.take_utterance()
        if discarded is not None:
            print("[隐私] 丢弃了一段疑似舵机转动噪音触发的误判语音")

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

    def enter_tietie(self, reason="摸头反应"):
        """"贴贴"：碰屏幕、或者听到"小狗小狗"呼唤，都走这里——两条触发
        路径都不依赖 scan_for_face() 先找到人脸再进开心，用户正在碰屏幕/
        正在叫它，人显然就在设备正前方，不需要再转头确认一次。直接进 HAPPY
        状态，播放 play_tietie_animation()（开心表情 + 轻微抬头 3 次，
        跟 enter_happy() 的完整摇头动画是两个不同的动作），动作播完保持在
        开心表情/状态上。不设状态限制，跟头顶双击→兴奋一样，从任何状态
        触发都会执行。reason 只影响转移日志里的说明文字，方便区分是哪条
        路径触发的。"""
        old = self.state
        self.session_active = True
        self.state = State.HAPPY
        self.state_enter_time = time.time()
        if old != State.HAPPY:
            print(f"[转移] {old.value} → 开心（{reason}）")
        play_tietie_animation()
        # "贴贴"动画播完，开一个手势扫描窗口——期待接下来几秒用户可能会
        # 比"手指枪"手势逗小狗装死，见 check_gesture()/tick() 里的窗口机制。
        # 绿色呼吸灯给用户一个更直接的"小狗正在识别手势"的反馈（原来是暖白，
        # 跟"开心"常亮的暖白色调太接近，不容易一眼看出状态已经变了；改用
        # THINKING_GREEN_RGB，跟"好奇/思考"复用同一个已验证的绿色，语义上
        # 也贴切——都是"正在识别中"）；覆盖掉 play_tietie_animation() 刚设的
        # 开心常亮，窗口关闭时（自然过期或者检测成功触发装死/委屈）都会把
        # LED 换掉，见 tick() 里的处理。
        self.gesture_scan_until = time.time() + GESTURE_WINDOW_SEC
        set_led_mode("breathe", *THINKING_GREEN_RGB, period_ms=GESTURE_LED_BREATHE_PERIOD_MS)

    def restore_state_led(self):
        """把 LED 恢复成当前状态本该有的常驻效果——任何"临时借用" LED 的地方
        （摸头触摸反馈灯、speak_keywords() 念关键词期间的暖白灯）用完之后都
        必须显式调这个还回去，不能假设别的地方会自己恢复。CLAUDE.md"LED
        系统"一节记过教训：这里漏掉哪个状态分支，就会导致 LED 卡在错误效果
        上，直到下一次完整的状态切换才会纠正过来。"""
        if self.state == State.HAPPY:
            set_led_mode("solid", *WARM_WHITE_RGB)
        elif self.state == State.EXCITED:
            set_led_mode("rainbow", period_ms=EXCITED_LED_PERIOD_MS)
        elif self.state == State.SLEEPY:
            set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=SLEEPY_LED_FADE_MS)
        elif self.state == State.PRIVACY:
            set_led_mode("fade", *WARM_WHITE_RGB, fade_ms=PRIVACY_LED_FADE_MS)
        elif self.state in (State.CURIOUS, State.THINKING):
            set_led_mode("breathe", *THINKING_GREEN_RGB, period_ms=CURIOUS_LED_BREATHE_PERIOD_MS)
        elif self.state == State.SORRY:
            set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)
        elif self.state == State.DIZZY:
            set_led_mode("breathe", *DIZZY_LED_RGB, period_ms=DIZZY_LED_BREATHE_PERIOD_MS)
        elif self.state == State.DEAD:
            # enter_dead() 播完闪两下红灯之后本来就是渐灭到熄灭，这里显式
            # 写出来（虽然效果跟下面 IDLE 的兜底一样），不要让 DEAD 只靠
            # "else" 兜底隐式生效——碰屏幕的触摸反馈灯松开时会调这个方法，
            # 装死状态下这条路径是真的会被走到的（见 handle_touch_trigger()
            # 的 DEAD 分支说明），不是纯理论上的兜底。
            set_led(off=True)
        elif self.state == State.ANGRY:
            # _play_angry_reminder() 播完闪烁后本来就切到红灯常亮，这里显式
            # 写出来（不靠 else 兜底）——碰屏幕之类的触摸反馈灯理论上不会在
            # ANGRY 期间被触发（_play_angry_reminder() 是同步阻塞的，期间
            # tick() 不会跑），但显式写出来比隐式假设更安全，也跟 DEAD 分支
            # 是同一个考虑。
            set_led_mode("solid", *ANGRY_LED_RGB)
        else:  # IDLE
            set_led(off=True)

    def enter_excited_from_touch(self):
        """从触摸手势进兴奋。只有从隐私状态触发（头顶双击）时才先把舵机
        转到正对人脸再开始兴奋动画——隐私姿势刻意把头转开（大幅度 yaw
        转向一侧，PRIVACY_YAW=800），不这样处理的话摇摆动作会从一个背对
        着人的诡异角度开始，需要靠 _face_person_before_excited() 的
        go_home()+等待转到位+拍照确认人脸这一整套（最多 3 秒）来妥善
        处理。

        **装死不需要这一步，故意不走这条路径**：装死只是低头看地
        （DEAD_PITCH_DOWN，只动 pitch），yaw 没有转开，不是"背对着人"，
        直接开始兴奋动画的摇摆本身很快就会把角度带正，不需要专门等
        3 秒去重新对准人脸——之前图省事把装死也归进跟隐私"同一类问题"
        （摄像头没轮询、face_detected 可能过时）一起处理，结果引入了两个
        实测问题：①双击后要等好几秒兴奋动画才真正开始，明显能感觉到卡顿；
        ②这几秒等待期间用户反馈表情会闪一下"常态"再变成兴奋，怀疑是
        go_home() 这类归位动作跟装死表情自身的动画状态叠加出的视觉问题，
        没有深究到底是哪一步具体导致的——反正装死本来就不需要这个detour，
        直接去掉最省事，两个问题一起解决。"""
        if self.state == State.PRIVACY:
            self._face_person_before_excited()
        self.transition(State.EXCITED)

    def enter_dead(self):
        """手势扫描窗口检测到"手指枪"手势时触发（见 check_gesture()）。同步
        阻塞执行完整的装死收尾动作，跟 enter_excited_from_touch() 是同一个
        模式——动画在这里手动播完，transition(State.DEAD) 只负责记录状态
        本身、更新 state_enter_time，不会重复播放任何动画（transition() 的
        if/elif 链里没有 DEAD 分支，落到这个状态时什么都不做）。

        舵机动作分两步（反馈要求加的）：先抬头一下（DEAD_PITCH_UP，配合
        表情切换和红灯开始闪烁，视觉上像"中枪一震"），转到位以后再停留
        DEAD_PITCH_UP_HOLD_SEC 让这个抬头动作能被看清，最后落到
        DEAD_PITCH_DOWN 定格——不是一步到位直接倒地。

        **不能盲等一个固定时长就发下一条 move_servo() 指令**：如果抬头还
        没转到位，"落下"的第二条指令会立刻覆盖掉第一条的目标角度，物理上
        只会看到舵机拐了个弯直接往下走，抬头这一下会被截断到几乎看不出来
        （第一版就是这么写的，反馈"没有抬头的舵机运动"）。改成跟
        _settle_privacy_mic()/_face_person_before_excited() 同一个套路：
        轮询 /status 确认 pitch 真的到了 DEAD_PITCH_UP 附近（容差
        DEAD_PITCH_UP_SETTLE_TOLERANCE），到位以后才开始数 DEAD_PITCH_
        UP_HOLD_SEC 的停留时间；轮询超时（DEAD_PITCH_UP_SETTLE_TIMEOUT_
        SEC）就按当前角度继续，不会卡死。"""
        set_expression("dead")
        set_led_mode("blink", *DEAD_LED_RGB, period_ms=DEAD_LED_BLINK_PERIOD_MS)
        move_servo(pitch=DEAD_PITCH_UP, speed=DEAD_SPEED, mute=True)
        deadline = time.time() + DEAD_PITCH_UP_SETTLE_TIMEOUT_SEC
        settled = False
        while time.time() < deadline:
            status = get_status()
            if status and abs(status.get("pitch", 0) - DEAD_PITCH_UP) <= DEAD_PITCH_UP_SETTLE_TOLERANCE:
                settled = True
                break
            time.sleep(0.1)
        if not settled:
            print("[装死] 等抬头转到位超时，按当前角度继续")
        time.sleep(DEAD_PITCH_UP_HOLD_SEC)
        move_servo(pitch=DEAD_PITCH_DOWN, speed=DEAD_SPEED, mute=True)
        time.sleep(DEAD_LED_BLINK_HOLD_SEC)
        set_led_mode("fade", *DEAD_LED_RGB, fade_ms=DEAD_LED_FADE_MS)
        self.transition(State.DEAD)

    def enter_goodbye(self):
        """手势扫描窗口检测到"再见"手势（挥手，或五指捏住再放开，见
        check_gesture()）时触发。要求是"委屈"过渡一下再转入"隐私"——不是
        直接 transition(State.PRIVACY)，先用 play_grieved_reaction() 播
        一下"委屈"（复用捉迷藏没找到目标那套现成的动作参数：微低头 + 暖白
        闪烁，只是表情换成 grieved），停留 GOODBYE_GRIEVED_HOLD_SEC 让这个
        过渡表情能被看清，再走 transition(State.PRIVACY)——那条路径本来就
        会播 play_privacy_animation()（转隐私姿势）+ _settle_privacy_mic()
        （等舵机转到位、丢弃转动噪音可能误触发的语音），跟平常进隐私是
        同一条路径，不需要在这里另外重写一遍。"""
        play_grieved_reaction()
        time.sleep(GOODBYE_GRIEVED_HOLD_SEC)
        self.transition(State.PRIVACY)

    def _face_person_before_excited(self):
        """隐私状态下摄像头是关的，不知道人具体在哪——先转回正对前方（比
        停留在隐私姿势转开的角度好），再拍照定位人脸、用 track_face_servo()
        朝人脸方向微调，这一步做完才真正进兴奋（顺序：转正→定位→兴奋）。
        隐私姿势（PRIVACY_YAW=800）离正前方很远，不能瞎猜一个固定时长就去
        拍照，跟 _settle_privacy_mic() 一样直接轮询 /status 确认舵机真的
        转到位了；但这整段是双击之后用户能感觉到的等待时间，要尽量压缩——
        转速用 EXCITED_YAW_SPEED（而不是 go_home() 那个转速不可控的固件
        默认归位）转得更快，settle 超时也跟着收紧（FACE_PERSON_SETTLE_
        TIMEOUT_SEC），拍照定位改用短超时+不重试（FACE_BG_TIMEOUT_SEC，
        跟后台人脸检测同一套参数）而不是默认的 5 秒+重试，没找到人脸就直接
        跳过微调进兴奋，不会为了等一次检测拖慢整个手势反馈。"""
        move_servo(yaw=0, pitch=450, speed=EXCITED_YAW_SPEED, mute=True)
        deadline = time.time() + FACE_PERSON_SETTLE_TIMEOUT_SEC
        while time.time() < deadline:
            status = get_status()
            if status:
                yaw_ok = abs(status.get("yaw", 0) - 0) <= FACE_PERSON_SETTLE_TOLERANCE
                pitch_ok = abs(status.get("pitch", 0) - 450) <= FACE_PERSON_SETTLE_TOLERANCE
                if yaw_ok and pitch_ok:
                    break
            time.sleep(0.1)
        found, face_x = self.detect_face_once(timeout=FACE_BG_TIMEOUT_SEC, _retry=False)
        if found:
            self.face_detected = True
            self.face_confirm_count = FACE_CONFIRM_FRAMES
            self.last_face_seen_time = time.time()
            self.record_interaction()
            self.track_face_servo(face_x)

    def handle_touch_trigger(self, touch):
        """处理一次 check_touch() 返回的手势——不止 tick() 一处需要处理触摸
        手势，讲话过程中（speak_keywords() 逐个念关键词）也要能对触摸立刻
        反应、打断当前播放，两处共用同一份判断和处理逻辑，不应该各写一份。
        返回 True 表示这次 touch 里确实有手势被处理了（调用方应该视情况
        提前返回/中止正在做的事）。

        隐私状态下退出方式被收紧成只有两种，都必须是头顶手势——长按（下面
        held_ms 分支）和头顶双击；碰屏幕在隐私状态下故意不生效（`and
        self.state != State.PRIVACY`），碰屏幕如果也能拉出隐私，就不是"只有
        触摸头顶才能退出"了。双击本来就是不分状态的全局触发，隐私状态下
        自然也生效，不需要单独处理——这里原来试过给隐私状态额外做一个"头顶
        单击"专属退出手势（`wasSingleClicked()`），实测这个单击判定很难可靠
        触发，改回了双击。

        装死状态下收得比隐私更紧：碰屏幕、头顶长按都直接忽略（跟隐私共用
        的 `and self.state != State.PRIVACY`/长按分支的 `and` 条件里都
        补一个排除 DEAD），改成**只要碰到头顶（不要求双击）就退出装死**。
        原来只认头顶双击，实测反馈双击判定不总能可靠触发、而且双击本身要
        等两次点按的窗口，退出装死这个反应不需要双击的"确认"语义（不像
        隐私退出还需要区分"只是碰一下"和"真的要出来"），改成任何一次触摸
        头顶都算数，响应更快也更可靠。`is_dead_touch` 只看
        `self.state == State.DEAD and touch["held_ms"] > 0`，不需要额外的
        "已经触发过"标记去防抖——一旦触发就会调 `enter_excited_from_touch()`
        把状态切离 DEAD，下一次检查时 `self.state == State.DEAD` 天然为
        假，不会重复触发（这点跟长按判断隐私不一样：长按是在同一个状态里
        持续满足阈值，必须靠 `privacy_hold_fired` 这个标记防抖；这里触发
        条件本身就绑定着"还没离开 DEAD"，状态一变条件自动失效）。双击仍然
        是全局触发，如果这次触摸恰好也构成双击，会走 `elif touch["double_
        tap"]:` 那一支，效果一样（都是调 `enter_excited_from_touch()`），
        不算冲突，只是同一个目的地的另一条路径。"""
        is_screen_trigger = touch["screen_tap"] and self.state not in (State.PRIVACY, State.DEAD)
        is_dead_touch = self.state == State.DEAD and touch["held_ms"] > 0
        is_trigger = (
            is_screen_trigger or touch["double_tap"] or is_dead_touch
            or (touch["held_ms"] >= PRIVACY_HOLD_SEC * 1000 and not self.privacy_hold_fired
                and self.state != State.DEAD)
        )
        if not is_trigger:
            return False

        # 触摸手势优先级最高：不管当前是不是正在讲话，先无条件叫停播放，
        # 再处理手势本身——firmware 端 /play?stop=1 在没有播放任务时只是个
        # 空操作，没有副作用，不需要先判断"是不是真的在放"，也不需要在这里
        # 关心调用方是从 tick() 还是从 wait_for_playback() 进来的。
        stop_play()

        if is_screen_trigger:
            print("[触发] 触碰屏幕 → 贴贴（摸头反应）")
            # 不需要在这里另外点一次触摸反馈灯——enter_tietie() 播放的
            # play_tietie_animation() 里已经会把 LED 设成暖白常亮，效果跟触摸
            # 反馈灯完全一样，重复调一次没有任何可观察的区别。
            self.enter_tietie()
        elif touch["double_tap"]:
            print("[触发] 头顶双击 → 兴奋！")
            if self.state == State.EXCITED:
                # 已经在兴奋状态，transition() 对"目标状态跟当前一样"是空
                # 操作，不会重播彩虹快闪——双击不会有任何其它可见变化，感觉
                # 像完全没反应，这里才需要显式点一次暖白灯确认"感应到了"，
                # 再用 restore_state_led() 落回兴奋本该有的彩虹快闪。
                set_led_mode("solid", *WARM_WHITE_RGB)
                self.enter_excited_from_touch()
                self.restore_state_led()
            else:
                # 真正会发生状态切换的情况（从装死/隐私/其它任何状态双击进
                # 兴奋）：transition()→play_excited_animation() 马上就会把
                # LED 换成彩虹快闪、表情换成兴奋，这个变化本身已经足够明显、
                # 足够快（一次 HTTP 往返，~100ms 级），不需要再额外点一次
                # 暖白灯"确认收到"。**之前统一都点这一下，是这次用户反馈
                # "装死→兴奋之间会先变成开心"的真正原因**：暖白灯正好是
                # "开心"状态的招牌配色，这次预先点亮跟紧随其后的真正状态
                # 切换撞在一起，视觉上第一眼会读成"先开心了一下、再兴奋"，
                # 不是真的有过渡动画或者触摸延迟，是这次多余的确认闪光本身
                # 制造出的错觉。
                self.enter_excited_from_touch()
        elif is_dead_touch:
            print("[触发] 装死状态下触摸头顶 → 兴奋")
            self.enter_excited_from_touch()
        else:
            self.privacy_hold_fired = True
            if self.state == State.PRIVACY:
                print(f"[触发] 长按 {touch['held_ms']/1000:.1f}s → 退出隐私")
                # 反向动作：进隐私时 LED 是暖白渐暗到熄灭（fade），退出这里
                # 就用 fade_in 从熄灭渐亮回暖白，同一个 fade_ms，视觉上是
                # 完全对称的反向过程。
                set_led_mode("fade_in", *WARM_WHITE_RGB, fade_ms=PRIVACY_LED_FADE_MS)
                if self.scan_for_face():
                    self.enter_happy()
                else:
                    # 之前这里没有 else 分支：扫描失败时 self.state 会一直
                    # 停留在 PRIVACY（尽管 scan_for_face() 失败时已经把表情
                    # 切成了 idle、舵机回正），内部状态和外部表现不一致——
                    # 下次触发 tick() 里任何"if self.state == State.PRIVACY"
                    # 的分支时会用一个其实已经不对的状态判断。显式落回 IDLE。
                    self.transition(State.IDLE)
            else:
                print(f"[触发] 长按 {touch['held_ms']/1000:.1f}s → 进入隐私")
                self.transition(State.PRIVACY)
        return True

    def record_interaction(self):
        self.last_interaction = time.time()

    # ---------- 人脸检测 ----------

    def detect_face_once(self, timeout=None, _retry=True):
        """拍一帧检测人脸，返回 (是否检测到, 人脸中心的水平归一化坐标)。
        坐标范围 0.0(最左)~1.0(最右)；没检测到人脸时坐标为 None。用锁串行化
        （见 face_detect_lock 声明处注释），调用方不用关心并发安全。

        timeout/_retry 透传给 capture_frame()→api_get()。后台人脸检测线程
        （_check_face_worker()/_retrack_face_worker()）会传一个短得多的
        超时、且不重试——原因见那两个函数旁边的说明：_device_lock 是全局
        互斥的，这次请求卡多久，主线程（触摸/表情/舵机这些真正需要"手感"
        的操作）就要陪着等多久，普通阻塞调用路径（scan_for_face()/
        track_face_once()/_face_person_before_excited() 等）继续用默认的
        TIMEOUT(5s)+重试，这些路径本来就是设计成阻塞等待的，不需要改。"""
        with self.face_detect_lock:
            img = capture_frame(timeout=timeout, _retry=_retry)
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

    def _reset_gesture_scan_state(self):
        """清空手势扫描窗口累积的所有检测状态——手指枪的滑动窗口历史、"再见"
        手势的挥手历史和"已捏拢等放开"标记。窗口关闭的两条路径（检测成功
        主动关闭 / tick() 里自然过期）都要调用，不然残留状态会被下一次开
        窗口的检测当成"本来就有"的数据，见 tick() 里对应分支的说明。"""
        self.finger_gun_history.clear()
        self.wave_x_history = []
        self.goodbye_pinch_since = 0.0

    def _update_wave_history(self, now, palm_x, hand_scale):
        """张开手掌的帧才会调用（见 check_gesture()）——只在手掌张开时累积
        水平位置样本，捏拢/放开过程中的样本不计入，避免"五指捏放"手势的
        手掌移动被误算成"挥手"的一次摆动。按时间窗裁剪，只留最近
        WAVE_HISTORY_SEC 秒内的样本。"""
        self.wave_x_history.append((now, palm_x, hand_scale))
        cutoff = now - WAVE_HISTORY_SEC
        self.wave_x_history = [s for s in self.wave_x_history if s[0] >= cutoff]

    def _count_wave_swings(self):
        """经典的"折线摆动计数"算法：从上一个极值点开始，水平位移超过
        振幅阈值（相对当时手掌尺度的比例，见 WAVE_MIN_AMPLITUDE_RATIO）才
        算移动到了一个新的极值点，方向跟上一段相反才计一次摆动——用手掌
        尺度而不是固定像素阈值，手离摄像头远近不同也不需要重新调参数
        （跟 classify_finger_gun_pose() 用比例不用绝对坐标差是同一个理由）。
        样本太少（少于 3 个）时直接认为还没有足够数据判断，返回 0。"""
        if len(self.wave_x_history) < 3:
            return 0
        xs = [s[1] for s in self.wave_x_history]
        scales = [s[2] for s in self.wave_x_history]
        swings = 0
        direction = 0
        last_extreme_x = xs[0]
        for x, scale in zip(xs[1:], scales[1:]):
            amp_threshold = scale * WAVE_MIN_AMPLITUDE_RATIO
            diff = x - last_extreme_x
            if abs(diff) >= amp_threshold:
                new_direction = 1 if diff > 0 else -1
                if direction != 0 and new_direction != direction:
                    swings += 1
                direction = new_direction
                last_extreme_x = x
        return swings

    def check_gesture(self):
        """手势扫描窗口期内调用（见 tick()），拍一帧、跑一次 Hand
        Landmarker 检测，同一次检测结果拿来判断两类手势：
          1. "手指枪"（finger gun）→ 装死，几何判定见模块级函数
             classify_finger_gun_pose()。
          2. "再见"（挥手，或五指捏住再放开）→ 委屈 → 隐私，几何判定见
             classify_open_pinch_pose()。
        两份判定逻辑都跟 host/gesture_test.py 诊断脚本共用，不要各自维护。
        用 Hand Landmarker 而不是 Gesture Recognizer 是因为"手指枪"不在
        预训练的 7 种手势里，"再见"的两种触发方式虽然 Gesture Recognizer
        原生支持挥手（`Open_Palm` 只是静态张开手掌，没有"挥手"这个手势），
        用同一个模型统一判定更省事，也不用额外加载第二个模型。

        **手指枪优先于"再见"，不是两个独立判定各走各的**：只要这一帧看起来
        像手指枪（哪怕滑动窗口里累计命中次数还没到 FINGER_GUN_CONFIRM_HITS），
        这一帧就不再判断"再见"。这不是预防性设计，是用 host/gesture_test.py 实机
        测出来的真实冲突：从放松的手摆成手指枪的过程会自然经过"指尖先聚拢
        再散开"（其余手指攥起的瞬间指尖靠拢，随后食指伸出、跟攥起的那几根
        拉开距离，指尖又散开），这个过渡形状会依次满足"再见"里"五指捏拢"
        和"放开"的判定条件——那套判定本来是给"挥手/松手道别"设计的，不要求
        连续帧，只要求在 `PINCH_RELEASE_TIMEOUT_SEC` 内先后出现过一次，
        结果被手指枪自己的手部动作抢先触发，见 check_gesture() 内部对应
        分支的详细说明。

        自己按 GESTURE_POLL_SEC 节流（跟 check_touch() 的 TOUCH_POLL_SEC
        是同一个写法），调用方（tick()）不需要关心频率，只要窗口开着就可以
        每个 tick 都调，不会因为节流而漏调。

        **拍照用短超时+不重试（FACE_BG_TIMEOUT_SEC，不是默认 TIMEOUT(5s)+
        重试）**：这个方法是直接在主线程 tick() 里同步调用的，不是像
        check_face()/retrack_face() 那样放进了后台线程——用户反馈"碰屏幕
        →贴贴之间有 3~5 秒延迟"，排查发现正是这里：只要某一次 `/camera`
        请求慢一点甚至失败，tick() 就会跟着卡住最多 5 秒（失败还要再等
        API_RETRY_DELAY_SEC 重试一次，最坏能到 10 秒+），这段时间里任何
        触摸检测都要陪着等。跟"定时提醒"一节 `_check_presence_monitoring()`
        采样、以及 `_check_face_worker()`/`_retrack_face_worker()` 是同一类
        "低优先级、错过一次无所谓"的场景——下面 `img is None` 分支本来就
        把拍照失败当成"这一帧不是手指枪"处理，不是致命错误，没有理由用
        给"阻塞等待、必须等到结果"路径设计的默认超时。"""
        now = time.time()
        if now - self.last_gesture_check < GESTURE_POLL_SEC:
            return
        self.last_gesture_check = now

        img = capture_frame(timeout=FACE_BG_TIMEOUT_SEC, _retry=False)
        if img is None:
            # 拍照失败当成"这一帧不是手指枪"计入滑动窗口，不整个清空——
            # 跟下面判定逻辑的滑动窗口是同一个道理，偶尔一帧的意外（拍照
            # 失败/没检测到手）不该让已经攒的进度全部作废。
            print("[手势] 拍照失败/超时，这一帧跳过")
            self.finger_gun_history.append(False)
            return
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        results = self.hand_landmarker.detect(mp_img)

        if not results.hand_landmarks:
            print("[手势] 这一帧没检测到手")
            self.finger_gun_history.append(False)
            return
        lm = results.hand_landmarks[0]

        # --- 手指枪 → 装死：最近 FINGER_GUN_WINDOW_FRAMES 帧里命中够
        #     FINGER_GUN_CONFIRM_HITS 次才真正触发（防止单帧误判）。原来
        #     要求"连续"命中，host/gesture_test.py 实机测过才发现这个要求
        #     太脆——即使全程稳定比着手指枪，单帧 landmark 抖动也会让某一
        #     帧偶尔判不中（比如食指伸展比例卡在阈值附近来回跳），"连续"
        #     要求会因为这一次孤立的漏判把已经攒的进度清零。改成滑动窗口
        #     累计命中次数，能容忍中间偶尔漏判一帧。
        gun_result = classify_finger_gun_pose(lm)
        self.finger_gun_history.append(gun_result["is_gun"])
        hit_count = sum(self.finger_gun_history)
        if gun_result["is_gun"]:
            print(f"[手势] 检测到手指枪（最近{len(self.finger_gun_history)}帧命中"
                  f"{hit_count}/{FINGER_GUN_CONFIRM_HITS}）")
        else:
            # 检测到手、但不构成手指枪——之前完全不打印这种情况，只有真正
            # 命中才有日志，实机排查"为什么迟迟不触发"时完全看不出是"手
            # 根本没在窗口期内举起来"还是"举了但判定没过"。精简版的判定
            # 明细，跟 host/gesture_test.py 诊断脚本同一批字段，但压缩成
            # 一行，避免每 GESTURE_POLL_SEC 就刷一大段。
            print(f"[手势] 未命中：食指={gun_result['index_straight']}"
                  f"({gun_result['index_ratio']:.2f}) "
                  f"拇指={gun_result['thumb_spread']}"
                  f"({gun_result['thumb_spread_ratio']:.2f}) "
                  f"中指弯={gun_result['middle_curled']}"
                  f"({gun_result['middle_ratio']:.2f})")

        if hit_count >= FINGER_GUN_CONFIRM_HITS:
            print("[触发] 手指枪确认 → 装死")
            self.gesture_scan_until = 0.0
            self._reset_gesture_scan_state()
            self.enter_dead()
            return

        # **这一帧只要像手指枪，就不再往下判断"再见"，即使还没攒够
        # FINGER_GUN_CONFIRM_HITS 次确认**——用 host/gesture_test.py 实机
        # 测过才发现的真实冲突：从放松的手摆成手指枪，中间会经过"手指先
        # 聚拢再散开"这个过渡（其余手指攥起来的瞬间指尖会先靠拢，随后食指
        # 伸出、跟攥起的那几根拉开距离，指尖整体的散开程度又变大）——这个
        # 过渡形状会先满足"再见"手势里"五指捏拢"的判定，等食指伸出去以后
        # 指尖又散开，接着满足"放开"的判定，捏拢→放开这套本来是给"挥手/
        # 松手道别"设计的宽松判定条件（不要求连续帧，只要求在
        # PINCH_RELEASE_TIMEOUT_SEC 内先后出现过一次），结果被比手指枪的
        # 自然手部动作本身抢先触发，还没等手指枪攒够 2 帧确认就被劫走进了
        # "再见→委屈→隐私"。手指枪候选帧（哪怕还没攒够确认帧数）直接跳过
        # 这一帧的"再见"判断，并且清空 goodbye_pinch_since——不清空的话，
        # 这次手指枪尝试半途放弃（比如没坚持满 2 帧）以后，之前手指聚拢
        # 的那一刻仍然会被记成一次"捏拢"，万一后面用户做了别的动作导致
        # 指尖又散开，会用这次已经过时、根本无关的"捏拢"配对出一次假的
        # "再见"手势。
        if gun_result["is_gun"]:
            self.goodbye_pinch_since = 0.0
            return

        # --- "再见"手势：挥手 或 五指捏住再放开 → 委屈 → 隐私 ---
        pose = classify_open_pinch_pose(lm)

        # 挥手：只在手掌张开的帧累积水平位置历史，够多次反向摆动就算数。
        if pose["is_open"]:
            self._update_wave_history(now, pose["palm_x"], pose["hand_scale"])
        wave_swings = self._count_wave_swings()
        if wave_swings >= WAVE_MIN_SWINGS:
            print(f"[手势] 检测到挥手（{wave_swings} 次摆动）")
            print("[触发] 挥手确认 → 委屈 → 隐私")
            self.gesture_scan_until = 0.0
            self._reset_gesture_scan_state()
            self.enter_goodbye()
            return

        # 五指捏住再放开：先记下"捏拢"的时刻，等到在超时时间内看到"放开"
        # 才算一次完整手势；捏拢之后一直没放开、又超时了，这次作废，要
        # 重新捏一次才算数（不会无限期地等一个很久以前的捏拢）。
        if pose["is_pinched"]:
            if self.goodbye_pinch_since == 0.0:
                self.goodbye_pinch_since = now
                print("[手势] 检测到五指捏拢，等待放开")
        elif pose["is_released"] and self.goodbye_pinch_since != 0.0:
            if now - self.goodbye_pinch_since <= PINCH_RELEASE_TIMEOUT_SEC:
                print("[触发] 五指捏住再放开确认 → 委屈 → 隐私")
                self.gesture_scan_until = 0.0
                self._reset_gesture_scan_state()
                self.enter_goodbye()
                return
            self.goodbye_pinch_since = 0.0
        if (self.goodbye_pinch_since != 0.0
                and now - self.goodbye_pinch_since > PINCH_RELEASE_TIMEOUT_SEC):
            self.goodbye_pinch_since = 0.0

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
        """带连续确认的人脸检测——只负责决定"这个 tick 要不要发起一次检测"，
        真正的拍照+推理丢给后台线程做（见 _check_face_worker()）。

        **这是为了修复"碰屏幕→贴贴"延迟明显（实测 ~2s）的问题**：
        detect_face_once() 单次耗时可达 1~2s（/camera 下载一张 JPEG +
        mediapipe 推理），之前是在 tick() 里同步阻塞调用的——tick() 内触摸
        检查虽然排在人脸检查前面、优先处理，但这防不住"用户碰屏幕的那一刻，
        恰好上一步的人脸检测正卡在阻塞调用里"：这种情况下这次触摸要等到
        当前这个（慢）tick() 整个跑完、下一个 tick() 重新开始时才会被
        check_touch() 看到，等待时长最坏情况就是这次人脸检测实际耗时，
        跟用户反馈的 ~2s 延迟对得上。之前"触摸响应特别慢"那次修复解决的是
        轮询节奏和 sleep 计算的问题，没有解决这个"触摸恰好撞上一次慢检测"
        的情况。

        改成后台线程后，check_face() 本身只做时间判断+起一个线程，几乎立刻
        返回，tick() 不会再被这一步卡住；检测结果通过 face_confirm_count/
        face_detected 这些实例属性异步写回，跟原来的效果一致，只是时序上
        从"这个 tick 内看到最新结果"变成"下一两个 tick 才看到"——检测间隔
        本来就有 FACE_CHECK_INTERVAL_SEC(3s) 这么长，晚一两个 tick（≤1s）
        不影响实际行为。"""
        now = time.time()
        if now - self.last_face_check < FACE_CHECK_INTERVAL_SEC:
            return
        if self._face_worker_busy:
            return
        self.last_face_check = now
        self._face_worker_busy = True
        threading.Thread(target=self._check_face_worker, daemon=True).start()

    def _check_face_worker(self):
        try:
            found, _ = self.detect_face_once(timeout=FACE_BG_TIMEOUT_SEC, _retry=False)
            now = time.time()
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
        finally:
            self._face_worker_busy = False

    def retrack_face(self):
        """开心状态下定期检测人脸并跟随：检测到就用 track_face_servo 微调
        朝向，而不是每次都先把头转回 yaw=0 再检测——那样每隔
        FACE_RETRACK_INTERVAL_SEC 就会有一次生硬的"回正"动作，人脸不在正
        前方时看起来像在甩头。现在只做增量微调，跟随更平滑。

        跟 check_face() 同样的原因（见那边的说明）改成后台线程发起，不在
        tick() 里同步阻塞——HAPPY 状态下摸屏幕触发"贴贴"同样会撞上这个
        问题，这里不改的话，开心状态下碰屏幕依然会有 ~2s 延迟。跟
        check_face() 共用同一个 _face_worker_busy 忙碌标记：两者分属不同
        状态（IDLE/SLEEPY/SORRY 用 check_face，HAPPY 用 retrack_face），
        同一时刻只会有一个在跑，不会互相抢占，只是防止上一次还没做完就
        叠加起下一次。"""
        now = time.time()
        if now - self.last_retrack_time < FACE_RETRACK_INTERVAL_SEC:
            return
        if self._face_worker_busy:
            return
        self.last_retrack_time = now
        self._face_worker_busy = True
        threading.Thread(target=self._retrack_face_worker, daemon=True).start()

    def _retrack_face_worker(self):
        try:
            found, face_x = self.detect_face_once(timeout=FACE_BG_TIMEOUT_SEC, _retry=False)
            now = time.time()
            if found:
                self.last_face_seen_time = now
                self.face_detected = True
                self.record_interaction()
                self.track_face_servo(face_x)
                print("[追踪] 人脸仍在")
            else:
                print("[追踪] 本次未检到人脸")
        finally:
            self._face_worker_busy = False

    # ---------- 扫描找人 ----------

    def scan_for_face(self, pitch=450):
        """转头扫描找人脸。pitch 默认是回正角度 450，开机迎接（见 run()）会传
        一个抬起来的值（HAPPY_PITCH）进来，让设备开机时主动抬头去找人，而不是
        用默认的平视角度扫。"""
        print("[扫描] 转头找人...")
        set_expression("curious")

        for yaw_pos in SCAN_POSITIONS:
            move_servo(yaw=yaw_pos, pitch=pitch, speed=SCAN_SPEED, mute=True)
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
        """查询触摸状态，返回一个 dict：
        - held_ms：头顶触摸当前这一次连续按住了多久（毫秒），没按住是 0。
          直接读固件端 Button_Class 每帧都在维护的连续按住时长（见
          firmware.ino 的 handleTouch()），不是 host 自己记按下时刻再拿
          当前轮询时刻相减——host 万一被一整轮对话（好几秒）卡住没来得及
          按时轮询，固件这边的数值依然准确，不会因为轮询节奏被打乱而把
          "按了 3 秒"错算成更长或更短。
        - double_tap：自上次查询以来头顶有没有发生过一次新的"短按两次"
          （比较固件端单调递增计数器 double_tap_count 有没有变化）。
        - screen_tap：自上次查询以来屏幕有没有被点过一次（同理，比较
          screen_tap_count）。
        这两个手势本身在固件内部只在判定成立的那一帧短暂为真，host 端
        大约 1Hz 的轮询频率直接问"现在是不是刚发生"大概率会错过，所以固件
        侧改成了单调计数器，host 端只看差值，poll 慢一点也不会漏事件。

        没轮到这次 poll_slot 或者请求失败时返回 None。"""
        now = time.time()
        if now - self.last_touch_poll < TOUCH_POLL_SEC:
            return None
        self.last_touch_poll = now

        touch = get_touch()
        if touch is None:
            return None

        held_ms = touch.get("held_ms", 0)
        double_tap_count = touch.get("double_tap_count", 0)
        screen_tap_count = touch.get("screen_tap_count", 0)

        if self.last_double_tap_count is None:
            # 第一次拿到读数：只校准基线，不能把设备之前（这次 host 启动
            # 之前）积累的计数差当成"刚刚发生"。
            self.last_double_tap_count = double_tap_count
            self.last_screen_tap_count = screen_tap_count

        double_tap = double_tap_count != self.last_double_tap_count
        screen_tap = screen_tap_count != self.last_screen_tap_count
        self.last_double_tap_count = double_tap_count
        self.last_screen_tap_count = screen_tap_count
        if screen_tap:
            # 计数器变了不代表这是一次独立的新点击——见 SCREEN_TAP_
            # DEBOUNCE_SEC 定义处的说明，同一次物理点按有时会被触摸传感器
            # 弹跳成好几次计数递增，冷却时间内的这些额外递增直接吞掉，不
            # 当成新的一次点击（但计数基线上面已经更新过了，不会遗留到
            # 冷却结束后被误判成"刚刚发生"）。
            if now - self.last_screen_tap_trigger_time < SCREEN_TAP_DEBOUNCE_SEC:
                screen_tap = False
            else:
                self.last_screen_tap_trigger_time = now

        pressed = held_ms > 0
        if pressed and not self.touch_pressed:
            print("[触摸] 按下...")
            # 点亮暖白灯，让人能马上确认"传感器确实感受到了这次触摸"——头顶
            # 长按要按满 PRIVACY_HOLD_SEC(3s) 才会有下一步反应，中间这几秒
            # 完全没反馈的话，用户很难判断是"按对了在等"还是"根本没碰到"。
            # 用 set_led_mode 而不是一次性的 /led?r=&g=&b=：固件的 updateLed()
            # 每帧都会按当前 g_ledMode 重算颜色，如果当前状态本来就是呼吸/
            # 闪烁/渐暗这类持续模式，一次性设色马上就会被下一帧的模式覆盖掉，
            # 必须用 mode 参数真正切换掉当前模式才压得住。
            # **装死状态下不点这一下**：这个按下沿的下一步就是
            # handle_touch_trigger() 判定 is_dead_touch 为真、立刻调
            # enter_excited_from_touch()（切成彩虹快闪），点完暖白灯马上又
            # 被覆盖，跟"装死→兴奋之间会先变成开心"那次教训是同一个模式
            # （暖白正好是开心的招牌配色，紧跟着真正的状态切换会造成"先开心
            # 一下再兴奋"的错觉）——装死本身不需要"按对了在等"这种反馈，
            # 触摸头顶就是立刻退出，不存在需要等待确认的中间态。
            if self.state != State.DEAD:
                set_led_mode("solid", *WARM_WHITE_RGB)
        if not pressed and self.touch_pressed:
            print("[触摸] 松开")
            self.record_interaction()
            self.privacy_hold_fired = False
            # 松手了，把"借用"的反馈灯还给当前状态本该有的常驻效果——不能
            # 假设手势处理分支（双击进兴奋、长按进/出隐私）一定会重设 LED，
            # 比如只是碰了一下又很快松开、没达到任何手势阈值，就得靠这里
            # 兜底恢复，否则 LED 会一直卡在触摸反馈的暖白常亮上。
            self.restore_state_led()
        self.touch_pressed = pressed

        if double_tap:
            print("[触摸] 检测到头顶双击")
            self.record_interaction()
        if screen_tap:
            print("[触摸] 检测到屏幕点击")
            self.record_interaction()

        return {"held_ms": held_ms, "double_tap": double_tap, "screen_tap": screen_tap}

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

        if self.state in (State.PRIVACY, State.DEAD):
            # 隐私/装死状态下都不接受语音唤醒——退出这两个状态只认触摸头顶
            # （隐私：长按/双击；装死：只有双击，见 handle_touch_trigger()）。
            # 这段语音（不管是真的在说话还是环境噪音/装死时舵机低头动作的
            # 噪音误触发）直接丢弃，不进对话流程；MicStream 的滚动缓冲/VAD
            # 本身继续正常跑，不受影响，只是 host 端不理会这次
            # take_utterance() 的结果——这也是为什么 tick() 里不能直接跳过
            # 调用 check_voice_wake()：不调用的话，这段时间里说完的话会一直
            # 堆在队列里排不空，等状态切走以后下一次调用会突然吐出一段"旧"
            # 语音，被当成用户刚说的话处理，跟隐私状态是同一个坑。
            print(f"[唤醒] {self.state.value}状态下忽略语音（只有触摸头顶能退出）")
            return False

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

    def check_shaking(self):
        """轮询 /status 的 imu/shaking 字段，判断当前是否正被明显晃动/拿起。
        判断本身在固件端连续完成（见 firmware.ino 里 g_shaking 声明处的
        注释），host 端只读结果，不自己拿加速度原始数据重新判断——跟"触摸
        事件：固件是权威真相"是同一个模式。IMU 没初始化成功（status 里
        imu=false）或者这次请求本身失败，都当作"没有在晃"处理，不能因为
        这个锦上添花的功能把整个状态机卡住。"""
        status = get_status()
        if status is None or not status.get("imu", False):
            return False
        return status.get("shaking", False)

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
        # 字幕框只应该在"录音+语音识别"这段时间出现——实测字幕框会挡住
        # "兴奋"表情的一部分，如果留到 LLM 思考、乃至回复触发的表情/状态
        # 切换（比如"表扬→兴奋"）都还在显示，就会一路盖住后面的画面。
        # 说话过程中的实时字幕由 _partial_transcribe_loop() 负责（那才是真正
        # 的"录音"阶段）；这里 transcribe() 返回的一刻就是"识别"阶段的终点，
        # 字幕显示一下确认结果之后必须立刻清掉，不能等到 LLM 请求发出去、
        # 更不能等到本函数结束才清（那样会盖到 THINKING 之后的所有画面）。
        # 这段 try 仍然包到函数结束，finally 兜底清一次字幕，防止中途异常导致
        # 字幕卡死在屏幕上；但正常路径下字幕在进 LLM 之前就已经手动清过了。
        try:
            wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate=self.mic_stream.sample_rate)
            user_text = self.transcribe(wav_bytes, "question.wav")
            print(f"[对话] 识别结果: 「{user_text}」")
            if not user_text:
                print("[对话] 没识别到内容")
                set_subtitle("")  # 说话过程中的实时字幕（_partial_transcribe_loop()）
                                   # 可能还留着最后一次的文字，这里没有识别结果可以
                                   # 展示，直接清掉，不要让它挡住 _settle_happy() 可能
                                   # 触发的表情
                self._settle_happy(track_ok)
                return

            # "小狗小狗"是在叫名字，不是在提问——不走 LLM 意图分类（会被
            # SYSTEM_PROMPT 的四路分支之一误吞、走成复杂回应念一串关键词），
            # 直接进完整的"开心"（跟被动检测到人脸/碰屏幕扫描找到人是同一个
            # enter_happy()，会播完整摇头动画，不是"贴贴"那个轻微抬头的反应
            # 动作）。不需要像 _settle_happy() 那样再看 track_ok 决定要不要重新
            # 扫描——用户显然就在附近正对着它说话，不需要摄像头再确认一次。
            if is_calling_puppy(user_text):
                print(f"[对话] 识别到呼唤「{user_text}」→ 开心")
                set_subtitle("")  # 同上，进 enter_happy() 之前先清掉，别挡住开心动画
                self.enter_happy()
                return

            # 识别结果出来就闪一下字幕框确认小狗听到的是什么，然后立刻清掉——
            # 语音识别到这里已经结束，接下来是"思考"（LLM），不属于字幕该
            # 出现的时间段了。
            set_subtitle(user_text, dur_ms=SUBTITLE_DUR_MS)
            set_subtitle("")

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
                print("[对话] 人示意去休息/独处 → 隐私")
                self.transition(State.PRIVACY)

            elif intent == "game_hide_seek":
                print("[对话] 提议捉迷藏游戏 → 进入游戏模式")
                self.play_game_hide_seek()

            else:  # qa_complex / other：逐个念关键词。数量是 2-4 个，不固定，
                   # 顺序（迫切程度/指代对象和地点在前等）由 SYSTEM_PROMPT 里的
                   # 规则直接约束 LLM 输出，这里不再做任何重排——早期版本用
                   # jieba 词性标注做过"名词全部排到动词前面"的后处理，但新
                   # 关键词词库里状态词/情感词等不是单纯名词动词二分，而且现在
                   # 顺序本身就带着语义（比如"小狗 想 零食"里"想"必须紧跟在
                   # "小狗"后面），机械按词性重排反而会破坏这个顺序，所以这版
                   # 直接信任 LLM 给出的顺序。
                keywords = data.get("keywords") or [reply_text[:6]]
                # 过滤掉 LLM 偶尔违规塞进来的完整句子，只丢词不重排，不影响
                # 上面说的"信任 LLM 给出的顺序"。
                keywords = sanitize_keywords(keywords)
                print(f"[对话] 复杂回应，播报关键词: {keywords}")
                self._settle_happy(track_ok)
                self.speak_keywords(keywords)
        finally:
            set_subtitle("")

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

    def wait_for_playback(self, timeout=PLAY_TIMEOUT):
        """轮询 /status 的 playing 字段直到这次播放自然结束；期间如果检测到
        触摸手势，交给 handle_touch_trigger() 处理（它会自己先叫停播放），
        这里只要发现"手势被处理了"就提前返回 False——告诉调用方"没播完，是
        被触摸打断的"，调用方（比如 speak_keywords()）应该据此中止自己接
        下来的流程（剩下的关键词不用再念了），不要不管三七二十一接着播下
        一个。返回 True 表示这次播放正常播完，没有被打断。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            touch = self.check_touch()
            if touch is not None and self.handle_touch_trigger(touch):
                return False
            status = get_status()
            if status is not None and not status.get("playing", False):
                return True
            time.sleep(0.1)
        print("[播放] 等待播放结束超时，强制停止")
        stop_play()
        return False

    def speak_keywords(self, keywords):
        """依次合成并播放每个关键词，关键词之间间隔 KEYWORD_GAP_SEC。每个关键词
        播放期间屏幕上会显示对应的字幕（animalese 声音本身不可懂，字幕是用户
        理解"小狗说了什么"的唯一途径，见下面播放循环里 set_subtitle() 那段）。
        思考一结束就让屏幕右下角的爪印按钮出现，之后每念一个关键词前
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
                # animalese 不可懂，字幕是用户理解"小狗说了什么"的唯一途径
                # （按钮只是视觉装饰）——必须在 start_play() 之前显示，不然
                # 用户会先听到声音才看到字幕，体验上是"字幕追着声音跑"；
                # 播完/被打断后都要清掉（放进下面的 finally，不管是自然播完
                # 还是被触摸打断都会执行），不能让上一个词的字幕停留到下一个
                # 词开始播放。
                set_subtitle(keywords[i])
                # 播放期间静音本地麦克风——无线麦克风会把 StackChan 自己的
                # TTS 声音原样录进去，不静音会把它当成用户在说话，误触发
                # 新一轮对话、打断正在播的这一句。见 MicStream.set_muted()。
                self.mic_stream.set_muted(True)
                finished_ok = False
                try:
                    started = start_play(wav_path)
                    finished_ok = started and self.wait_for_playback()
                finally:
                    # 自然播完时物理尾音还没停（见 MIC_UNMUTE_COOLDOWN_SEC），
                    # 先等一下再取消静音；被打断时是硬切断，没有尾音，不用等。
                    if finished_ok:
                        time.sleep(MIC_UNMUTE_COOLDOWN_SEC)
                    self.mic_stream.set_muted(False)
                    set_subtitle("")
                if started and not finished_ok:
                    # 被触摸打断——handle_touch_trigger() 已经处理完这次
                    # 手势（叫停了播放、切到了新状态），这里只需要把还没念
                    # 完的关键词和按钮 UI 一起收掉，不能继续念下去，也不能
                    # 再调 restore_state_led()：手势处理时已经把 LED 设成
                    # 新状态该有的样子了，这里再设一次反而可能把它盖掉。
                    print("[播放] 讲话被触摸打断，剩下的关键词不念了")
                    set_button("off")
                    return
                set_led(off=True)
            if i + 1 < len(keywords):
                next_wav = tts_to_wav(keywords[i + 1], f"kw_{i + 1}")
            time.sleep(KEYWORD_GAP_SEC)
        set_button("off")
        self.restore_state_led()

    # ---------- 捉迷藏找物品游戏 ----------
    #
    # 整个游戏是一次同步阻塞的交互，从 _run_conversation_turn_body() 的
    # game_hide_seek 分支进来，跟对话链路本身一样会占住 tick() 直到游戏结束
    # （找到/超时/长按中止）——这跟现有架构里"一次完整对话占住 tick() 好几
    # 秒"是同一种模式，不是新引入的阻塞。self.state 全程停在 GAME_HIDE_SEEK，
    # 不经过 transition() 的常规状态分发表（那张表是给"进入即播放一次性动画"
    # 的状态设计的，游戏内部每个子阶段的表情/LED/舵机都要自己精细控制，见
    # enter_tietie() 已经有的先例：直接改 self.state 而不是调
    # transition()）；只有找到/超时结尾复用 EXCITED/SORRY 的标准动画（走
    # transition()），游戏彻底结束时用 _game_settle_after_result() 收尾——
    # 专门写了这个轻量版本而不是复用对话结束用的 _settle_happy()，因为
    # 后者追踪丢失时会调 scan_for_face()（切"好奇"表情+转头扫描好几个
    # 位置），直接接在刚播完的兴奋/抱歉表情后面会打断这段表情反馈，见
    # _game_settle_after_result() 的说明。
    #
    # 游戏里所有的播报（"小狗 看"、识别到的物品/位置、倒计时数字、"小狗 闭眼"）
    # 都走跟 speak_keywords() 同一套"按钮按一下+关键词 TTS 同步播放"的 AAC
    # 风格，不用整句 TTS 旁白——理由跟对话回应必须用关键词表达是同一个：
    # 这是小狗表达自己的统一方式，游戏流程也不应该破例说整句话。
    #
    # 游戏进行中不接受碰屏幕/双击这些正常状态下的触摸手势（那些是"贴贴"/
    # "兴奋"的触发器，游戏语境下含义会冲突——比如用户摸屏幕很可能只是想
    # 确认"我藏好了"，不是想要贴贴反应），只保留长按头顶这一个"中止游戏"
    # 信号，所以不走 self.check_touch()/handle_touch_trigger() 那一整套手势
    # 分发，改成 _game_check_abort() 直接读原始 /touch。

    def play_game_hide_seek(self):
        """捉迷藏找物品：听到邀请先开心点头回应 → 看物品并记住 → 低头闭眼
        倒数给人时间藏好 → 转头扫描房间找。任何一个阶段中途被长按打断都会
        自己调用 _game_abort() 收尾并返回，后续阶段不会再执行。"""
        self.state = State.GAME_HIDE_SEEK
        self.state_enter_time = time.time()
        self.game_ref_hist = None
        self.game_ref_desc = None
        print("[游戏] 捉迷藏开始")

        set_expression("happy")
        play_nod_animation()

        if not self._game_register_phase():
            return
        if not self._game_countdown_phase():
            return
        self._game_scan_phase()

    def _game_check_abort(self):
        """轮询一次头顶触摸，长按满 PRIVACY_HOLD_SEC 就视为"中止游戏"信号。
        直接读 get_touch() 原始返回值，不经过 self.check_touch()——那是给
        正常 tick() 流程用的，会做触摸反馈灯、双击/屏幕点击计数比较等一整套
        跟游戏无关的副作用。"""
        touch = get_touch()
        return bool(touch) and touch.get("held_ms", 0) >= PRIVACY_HOLD_SEC * 1000

    def _game_abort(self):
        print("[游戏] 长按头顶 → 中止捉迷藏")
        stop_play()
        self.transition(State.IDLE)

    def _prewarm_game_tts(self):
        """后台预热合成 GAME_FIXED_PHRASES 里的固定词汇，缓存到
        self._game_tts_cache，见常量定义处的说明。任何一个词合成失败就跳过
        （_game_tts() 缓存未命中时会自动退化成现合成，不是致命问题）。"""
        for i, phrase in enumerate(GAME_FIXED_PHRASES):
            wav_path = tts_to_wav(phrase, f"game_fixed_{i}")
            if wav_path:
                self._game_tts_cache[phrase] = wav_path
        print(f"[游戏] 固定词汇 TTS 预热完成（{len(self._game_tts_cache)}/{len(GAME_FIXED_PHRASES)}）")

    def _game_tts(self, text, stem):
        """跟 tts_to_wav() 一样合成一个词的音频，但固定词汇优先用
        _prewarm_game_tts() 预热好的缓存，跳过一次网络合成——这是用户反馈
        "从听到邀请到说出'小狗 看'中间等太久"之后加的，TTS 合成本身是一次
        网络往返，几百毫秒到一两秒不等，而这几个词内容从来不变，没必要
        每次触发游戏都重新合成。缓存未命中（预热还没跑完，或者是 LLM 生成
        的动态关键词，比如识别到的物品/位置）就照常现合成。"""
        cached = self._game_tts_cache.get(text)
        if cached is not None:
            return cached
        return tts_to_wav(text, stem)

    def _game_wait_for_playback(self, timeout=PLAY_TIMEOUT):
        """跟 wait_for_playback() 一样轮询 /status 的 playing 字段等自然
        播完，但用 _game_check_abort() 代替 handle_touch_trigger()——游戏
        进行中不接受碰屏幕/双击手势，只有长按算数。返回 True 表示正常播完
        （或者播放没能启动，不阻塞游戏流程）；False 表示被长按中止（已经
        调用了 _game_abort()，调用方应该直接 return）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._game_check_abort():
                self._game_abort()
                return False
            status = get_status()
            if status is not None and not status.get("playing", False):
                return True
            time.sleep(0.1)
        stop_play()
        return True

    def _game_speak_keywords(self, keywords, led_rgb=WARM_WHITE_RGB, gap_sec=KEYWORD_GAP_SEC):
        """游戏版的 speak_keywords()：同一套"按钮按一下+关键词 TTS 同步播放"
        机制（按钮从隐藏出现、每个词播放前按一下、全部念完按钮消失），但不
        能直接复用 speak_keywords()——那个内部调用 self.wait_for_playback()，
        会走 handle_touch_trigger() 的完整手势分发（碰屏幕→贴贴等），游戏
        进行中触发这些会跟游戏状态冲突。led_rgb 让调用方可以换成绿色（"正在
        看"）而不是默认的暖白，别的行为跟 speak_keywords() 一致。返回 False
        表示中途被长按中止，调用方应该直接 return（_game_abort() 已经在
        _game_wait_for_playback() 内部调用过了）。"""
        if not keywords:
            return True
        set_button("up")
        time.sleep(BUTTON_PRESS_MS / 1000)
        next_wav = self._game_tts(keywords[0], "game_kw_0")
        for i in range(len(keywords)):
            set_button("down")
            time.sleep(BUTTON_PRESS_MS / 1000)
            set_button("up")
            wav_path = next_wav
            if wav_path:
                set_led(*led_rgb)
                # 同 speak_keywords()：animalese 不可懂，字幕是唯一能看懂
                # "小狗说了什么"的途径，必须在 start_play() 之前显示。
                set_subtitle(keywords[i])
                # 同 speak_keywords()：播放期间静音本地麦克风，见
                # MicStream.set_muted()。
                self.mic_stream.set_muted(True)
                finished_ok = False
                try:
                    started = start_play(wav_path)
                    finished_ok = started and self._game_wait_for_playback()
                finally:
                    # 见 speak_keywords()/MIC_UNMUTE_COOLDOWN_SEC：自然播完
                    # 时最后一块音频还有物理尾音，先等一下再取消静音。
                    if finished_ok:
                        time.sleep(MIC_UNMUTE_COOLDOWN_SEC)
                    self.mic_stream.set_muted(False)
                    set_subtitle("")
                if started and not finished_ok:
                    set_button("off")
                    return False
                set_led(off=True)
            if i + 1 < len(keywords):
                next_wav = self._game_tts(keywords[i + 1], f"game_kw_{i + 1}")
            time.sleep(gap_sec)
        set_button("off")
        return True

    def _game_register_phase(self):
        """阶段一：看物品并记住——按钮播报"小狗 看"，看的时候（含实际拍照
        那一刻）LED 绿色提示正在用摄像头；拍完提取颜色直方图，再调 Qwen-VL
        把识别结果压缩成一个关键词念出来（比如"橘子"）。念完给一个窗口期
        （_game_listen_for_rejection()），人可以说"不是这个"之类的话让小狗
        重新看一次——不这样的话，认错物品只能眼睁睁看着流程走到倒计时才能
        发现。确认（或者超时没人反对）以后，点两下头、按按钮说"小狗 闭眼"，
        示意进入倒计时。全程用 set_expression("thinking") 表示"正在专心记这个
        东西"；VLM 不可用时跳过关键词播报和否定窗口的语义检查基础（没有
        识别结果可以被否定），不影响游戏继续（后面扫描阶段会退化成只靠
        颜色直方图判断）。"""
        set_expression("thinking")
        while True:
            print("[游戏] 阶段：看物品")
            set_led_mode("solid", *THINKING_GREEN_RGB)
            if not self._game_speak_keywords(["小狗", "看"], led_rgb=THINKING_GREEN_RGB):
                return False

            set_led(*THINKING_GREEN_RGB)  # 保证拍照这一刻确实是绿灯，不受限于
                                           # 上面关键词播放间隙里"灭灯"的时序
            img, jpeg_bytes = capture_frame_with_bytes()
            set_led(off=True)
            if img is None:
                print("[游戏] 拍照失败，取消游戏")
                self.transition(State.SORRY)
                time.sleep(1.5)
                self.transition(State.IDLE)
                return False

            self.game_ref_hist = extract_color_hist(img)
            self.game_ref_desc = call_vision_llm(jpeg_bytes, GAME_OBJECT_DESC_PROMPT, self.qwen_api_key)
            print(f"[游戏] 物品描述: {self.game_ref_desc or '（VLM 不可用，仅用颜色直方图判断）'}")
            if self.game_ref_desc:
                desc_keywords = sanitize_keywords(self.game_ref_desc.split())
                if desc_keywords and not self._game_speak_keywords(desc_keywords):
                    return False

            outcome = self._game_listen_for_rejection(GAME_REJECTION_WINDOW_SEC)
            if outcome == "abort":
                return False
            if outcome == "reject":
                print("[游戏] 人说不是这个，重新看一次")
                continue
            break

        set_expression("happy")
        play_nod_animation()
        if not self._game_speak_keywords(["小狗", "闭眼"]):
            return False
        return True

    def _game_listen_for_rejection(self, timeout):
        """给用户一个窗口期，说"不是这个"之类的话可以让小狗重新看一次。
        窗口开始前先丢弃一次可能残留在 MicStream 队列里的旧语音段（比如
        "小狗 看"/识别结果播报期间麦克风被静音，恢复推流后短暂积攒的噪音），
        避免误判成这次的否定指令。识别到语音后调用 self.transcribe()——跟
        正常对话链路共用同一个 SenseVoice 模型和 self._asr_lock，不会真的
        并发调用。跟 _game_check_abort() 一样只做信号判断，不影响 MicStream
        本身的持续录制。返回 "abort"/"reject"/None 三选一，None 表示窗口期
        内没有收到否定指令，可以正常往下走。"""
        self.mic_stream.take_utterance()  # 丢弃窗口开始前可能残留的旧语音段
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._game_check_abort():
                self._game_abort()
                return "abort"
            segment = self.mic_stream.take_utterance()
            if segment is not None:
                wav_bytes = pcm_to_wav_bytes(segment, sample_rate=self.mic_stream.sample_rate)
                text = self.transcribe(wav_bytes, "game_reject_check.wav")
                print(f"[游戏] 确认窗口期间听到:「{text}」")
                if is_registration_rejection(text):
                    return "reject"
            time.sleep(0.1)
        return None

    def _game_countdown_phase(self):
        """阶段二：倒计时——低头闭眼，按钮报数"5 4 3 2 1"，报数本身就是给
        人留出的藏东西时间，不需要再额外阻塞等待。以前这里借用"privacy"
        表情的闭眼视觉效果（当时项目里还没有专门的闭眼表情）；现在有了
        专门为这个场景设计的"peekaboo"（闭眼）表情，改成用它，不再需要
        借用隐私表情。"""
        print("[游戏] 阶段：倒计时")
        set_expression("peekaboo")
        set_led_mode("breathe", *GAME_COUNTDOWN_LED_RGB, period_ms=GAME_COUNTDOWN_LED_PERIOD_MS)
        move_servo(yaw=0, pitch=GAME_COUNTDOWN_PITCH, speed=300, mute=True)
        return self._game_speak_keywords(GAME_COUNTDOWN_NUMBERS, gap_sec=GAME_COUNTDOWN_GAP_SEC)

    def _game_scan_phase(self):
        """阶段三：扫描搜索——按 _game_build_scan_plan() 生成的"之"字形路径
        转头扫描房间（俯仰分两层，覆盖原来固定单一水平面扫不到的低处/高处，
        比如桌面以下、右下角这类之前漏检的区域），每一个"取景点"拍照算
        颜色直方图相关度，超过阈值就算初筛命中；有物品描述的话再调一次 VLM
        精确确认，没有就直接采信颜色匹配结果。转动速度随扫描进度从慢到快
        （GAME_SCAN_SPEED_MIN → MAX），故意不是一开始就用最快速度扫完，
        找到/超时/中途长按打断，三条路径都在这个方法里收尾。"""
        print("[游戏] 阶段：扫描搜索")
        set_expression("curious")
        set_led_mode("rainbow", period_ms=GAME_SCAN_LED_PERIOD_MS)

        plan = self._game_build_scan_plan()
        total_points = len(plan)

        found = False
        found_jpeg_bytes = None
        scan_start = time.time()
        for step, point in enumerate(plan):
            if time.time() - scan_start > GAME_SCAN_TIMEOUT_SEC:
                print("[游戏] 扫描超时")
                break
            if self._game_check_abort():
                self._game_abort()
                return

            progress = step / max(1, total_points - 1)
            speed = int(GAME_SCAN_SPEED_MIN + (GAME_SCAN_SPEED_MAX - GAME_SCAN_SPEED_MIN) * progress)
            move_servo(yaw=point["yaw"], pitch=point["pitch"], speed=speed, mute=True)
            time.sleep(GAME_SCAN_PAUSE_SEC)

            if not point["capture"]:
                continue  # 途径点（比如两层之间的回正点），只是路过，不拍照判断

            img, jpeg_bytes = capture_frame_with_bytes()
            if img is None:
                continue
            corr = compare_hist(self.game_ref_hist, extract_color_hist(img))
            print(f"[游戏] 扫描 yaw={point['yaw']} pitch={point['pitch']} speed={speed} 颜色相关度={corr:.2f}")
            if corr <= GAME_HIST_THRESHOLD:
                continue

            if self.game_ref_desc and not self._game_confirm_with_vlm(jpeg_bytes):
                continue
            found = True
            found_jpeg_bytes = jpeg_bytes
            break

        if found:
            self._game_on_found(found_jpeg_bytes)
        else:
            self._game_on_timeout()

    @staticmethod
    def _game_build_scan_plan():
        """生成一次扫描的完整路径：按 GAME_SCAN_PITCH_LEVELS 依次抬头/低头
        （具体哪个数值对应哪个方向没有实机验证，见常量定义处的说明），每层
        的 yaw 方向跟上一层相反，扫成"之"字形，而不是原来固定在单一水平面
        左右来回扫——那样会漏掉不在那个水平带里的区域（比如桌面以下）。
        每层的起点/终点各自在 GAME_SCAN_YAW_JITTER 范围内随机抖动一点，
        不要每次路径都一模一样；层与层之间插一个 yaw 回正的过渡点（不拍照，
        capture=False），既是"中途回正的途径点"，也让俯仰变化更自然（先转平
        再变俯仰，不是斜着一步到位）。返回一串 {"yaw", "pitch", "capture"}
        字典，按顺序执行。"""
        plan = []
        for level_idx, pitch in enumerate(GAME_SCAN_PITCH_LEVELS):
            left_to_right = (level_idx % 2 == 0)
            jitter_start = random.uniform(0, GAME_SCAN_YAW_JITTER)
            jitter_end = random.uniform(0, GAME_SCAN_YAW_JITTER)
            if left_to_right:
                start, end = GAME_SCAN_YAW_MIN + jitter_start, GAME_SCAN_YAW_MAX - jitter_end
            else:
                start, end = GAME_SCAN_YAW_MAX - jitter_start, GAME_SCAN_YAW_MIN + jitter_end

            for step in range(GAME_SCAN_STEPS_PER_LEVEL):
                t = step / max(1, GAME_SCAN_STEPS_PER_LEVEL - 1)
                yaw = start + (end - start) * t
                yaw = int(max(GAME_SCAN_YAW_MIN, min(GAME_SCAN_YAW_MAX, yaw)))
                plan.append({"yaw": yaw, "pitch": pitch, "capture": True})

            if level_idx < len(GAME_SCAN_PITCH_LEVELS) - 1:
                plan.append({"yaw": 0, "pitch": pitch, "capture": False})

        return plan

    def _game_confirm_with_vlm(self, jpeg_bytes):
        """颜色直方图初筛命中后，调 VLM 精确确认画面里是不是真的是那个物品。
        VLM 调用失败（没配置 key/网络问题/超时）时不应该让"没有精确确认能力"
        变成"永远找不到"，直接采信颜色匹配的结果。"""
        answer = call_vision_llm(
            jpeg_bytes,
            f"画面中是否存在这样的物品：{self.game_ref_desc}？只回答\"是\"或\"否\"，不要输出其它内容。",
            self.qwen_api_key,
        )
        if answer is None:
            print("[游戏] VLM 确认不可用，按颜色匹配结果直接采信")
            return True
        confirmed = ("是" in answer) and ("不是" not in answer) and ("否" not in answer)
        print(f"[游戏] VLM 确认: {answer!r} → {'确认找到' if confirmed else '排除，继续找'}")
        return confirmed

    def _game_on_found(self, jpeg_bytes):
        """找到了：进兴奋状态（复用 play_excited_animation()），再调 VLM 把
        物品大概的位置压缩成关键词念出来（比如"桌子 下面"）。结束后用
        _game_settle_after_result() 收尾，不能用普通对话结束时的
        _settle_happy(False)——那个追踪丢失时会调 scan_for_face()，是一次
        完整的转头扫描 + 切"好奇"表情的动画，用在这里会立刻打断刚播完、
        用户还没看几眼的兴奋表情，表现出来就是"找到反馈被打断"。"""
        print("[游戏] 找到了！")
        self.transition(State.EXCITED)
        location = call_vision_llm(jpeg_bytes, GAME_LOCATION_DESC_PROMPT, self.qwen_api_key)
        print(f"[游戏] 位置描述: {location or '（VLM 不可用，跳过位置播报）'}")
        if location:
            keywords = sanitize_keywords(location.split())
            if keywords and not self._game_speak_keywords(keywords):
                return
        time.sleep(GAME_FOUND_CELEBRATE_SEC)
        self._game_settle_after_result()

    def _game_on_timeout(self):
        """没找到：用"委屈"表情做反应（play_grieved_reaction()，动作/灯效
        跟"抱歉"状态一样，只是换一张脸），按钮说一句"没有"（跟游戏里其它
        播报同一套按钮+关键词 TTS 机制），不经过 self.transition(State.
        SORRY)——self.state 全程留在 GAME_HIDE_SEEK。收尾故意不用
        _game_settle_after_result()：那个方法没找到人脸时会调
        self.transition(State.IDLE)，会顺带把表情切回中性、舵机归位——用户
        明确要求委屈表情要一直保持到真的重新看到人脸，不能被这一步盖掉，
        所以改用专门的 _game_settle_after_timeout()。"""
        print("[游戏] 超时，没找到")
        play_grieved_reaction()
        if not self._game_speak_keywords(["没有"]):
            return
        time.sleep(GAME_TIMEOUT_LINGER_SEC)
        self._game_settle_after_timeout()

    def _game_settle_after_result(self):
        """游戏找到目标以后的收尾：恢复到正常状态、继续追踪人脸。故意不复用
        _settle_happy()——那是给普通对话结束设计的，追踪丢失时会调
        scan_for_face()（切"好奇"表情 + 转头扫描 SCAN_POSITIONS 好几个
        位置），直接接在刚播完的兴奋表情后面，会打断用户正在看的表情反馈，
        感觉像"庆祝到一半突然又开始东张西望"。这里改成只拍一帧轻量确认
        （不切表情、不转头扫描）：确认到人脸就直接 enter_happy()（如果
        session_active 已经是 True，只是安静地切换状态，不会重播开心动画，
        更不会有额外的转头动作打断刚才的表情）；没确认到也不强行扫，直接
        回到常态，交给后续 tick() 里的被动人脸检测自然接管。

        只用于找到（_game_on_found()）——没找到用 _game_settle_after_
        timeout()，两者收尾方式不一样，见那边的说明。"""
        found, face_x = self.detect_face_once()
        if found:
            self.face_detected = True
            self.face_confirm_count = FACE_CONFIRM_FRAMES
            self.last_face_seen_time = time.time()
            self.record_interaction()
            self.enter_happy()
        else:
            self.transition(State.IDLE)

    def _game_settle_after_timeout(self):
        """游戏没找到目标以后的收尾：跟 _game_settle_after_result() 不同——
        用户明确要求"委屈"表情要一直保持，不能被这一步的常态归位盖掉。不调
        self.transition(State.IDLE)（会顺带播 play_idle_animation()，把表情
        切回中性、舵机归位），改成直接把 self.state 设成 IDLE 交给 tick()
        里已有的被动人脸检测逻辑接管（State.IDLE 分支每隔一个 tick 就会拍照
        检查一次，检测到人脸会自动走 enter_happy() 切过去）——委屈表情和
        当前舵机姿势（play_grieved_reaction() 里的微低头）都原样保留，直到
        真的重新看到人脸才会被 enter_happy() 自然换掉。state_enter_time 也
        要跟着更新，IDLE_TO_SLEEPY_SEC 这类依赖它计时的判断才不会算错。"""
        found, face_x = self.detect_face_once()
        if found:
            self.face_detected = True
            self.face_confirm_count = FACE_CONFIRM_FRAMES
            self.last_face_seen_time = time.time()
            self.record_interaction()
            self.enter_happy()
        else:
            self.state = State.IDLE
            self.state_enter_time = time.time()

    # ---------- 定时提醒 + 生气催促 ----------
    # 小狗在指定时间主动提醒主人，提醒后进入在场监测窗口（见
    # _check_presence_monitoring()），如果主人一直没离开座位，就生气催促
    # （_play_angry_reminder()）。发出提醒本身
    # （_deliver_reminder()）是一次同步动作，跟 run_conversation_turn() 占住
    # tick() 几秒是同一种模式，不需要单独的状态；生气催促序列本身则是一个
    # 完整的同步阻塞方法（跟 play_game_hide_seek() 是同一种架构），执行期间
    # tick() 完全停摆，所以生气序列内部的触摸/中止判断都直接读 get_touch()
    # 原始值或复用 _game_check_abort()，不走 self.check_touch()/
    # handle_touch_trigger() 那一整套——那套是给正常 tick() 循环设计的，会
    # 顺带触发跟生气无关的副作用（触摸反馈灯、双击→兴奋的全局手势分发等）。
    # handle_touch_trigger() 因此不需要加 State.ANGRY 分支：唯一可能在
    # ANGRY 期间被调用的路径是 wait_for_playback() 内部的轮询，而生气序列
    # 目前完全不播放语音（不调 speak_keywords()），不会经过那条路径。
    # 以后如果给生气加语音播报，要重新检查这一条是否还成立。

    def _check_reminders(self):
        """检查 REMINDERS 里有没有条目命中当前时间窗口，命中且不在冷却期
        就发出提醒。一次 tick 最多触发一条（找到就 break）。已经有一条
        提醒在复查中（self._reminder_recheck_target 不是 None）时不发新
        的——同一时间只跟踪一条复查状态，避免被第二条提醒覆盖。"""
        if self._reminder_recheck_target is not None:
            return

        now_ts = time.time()
        expired = [label for label, ts in self._reminder_cooldowns.items()
                   if now_ts - ts > REMINDER_COOLDOWN_SEC]
        for label in expired:
            del self._reminder_cooldowns[label]

        now_dt = datetime.now()
        for reminder in REMINDERS:
            label = reminder["label"]
            if label in self._reminder_cooldowns:
                continue
            target_today = now_dt.replace(hour=reminder["hour"], minute=reminder["minute"],
                                           second=0, microsecond=0)
            if abs((now_dt - target_today).total_seconds()) <= REMINDER_WINDOW_SEC:
                self._reminder_cooldowns[label] = now_ts
                self._deliver_reminder(reminder)
                break

    def _check_dynamic_reminder(self):
        """喝水/出去玩是一条独立的链，跟吃饭提醒（REMINDERS）没有依赖关系
        ——引擎启动时就直接排好了 self._dynamic_next_time，按
        DYNAMIC_INTERVAL_MIN/MAX_SEC 随机间隔连续触发，每次触发（不管有
        没有真的找到人）都会重新排一次下一次时间，链条本身不会断，只受
        活跃时段和"是否有其它提醒正在监测中"两个条件节流。跟
        _check_reminders() 共用 self._reminder_recheck_target 这个互斥
        （非 None 时跳过），避免吃饭/喝水/出去玩三条提醒的在场监测窗口
        互相打断。

        喝水/出去玩二选一按天气决定，不再是硬币式的随机——晴/多云/阴天
        （适合外出）选"出去玩"，雨/雪/天气查不到（不适合外出，查不到时
        保守当作不适合）选"喝水"。见 _weather_suitable_for_outdoor()。"""
        if self._dynamic_next_time is None or self._reminder_recheck_target is not None:
            return

        now = time.time()
        if now < self._dynamic_next_time:
            return

        now_dt = datetime.now()
        if not _in_dynamic_active_hours(now_dt):
            next_dt = now_dt.replace(hour=DYNAMIC_ACTIVE_HOUR_START, minute=0,
                                      second=0, microsecond=0)
            if now_dt.hour >= DYNAMIC_ACTIVE_HOUR_END:
                next_dt += timedelta(days=1)
            self._dynamic_next_time = next_dt.timestamp() + _next_dynamic_delay()
            return

        weather = _fetch_weather_now(self.qweather_api_key, self.qweather_api_host)
        if weather is not None and _weather_suitable_for_outdoor(weather[0]):
            kind = "move_around"
            print(f"[REMINDER] 动态链条命中，天气={weather[0]}（适合外出）→ 选中: {kind}")
        else:
            kind = "drink_water"
            reason = f"天气={weather[0]}（不适合外出）" if weather is not None else "天气查不到"
            print(f"[REMINDER] 动态链条命中，{reason} → 选中: {kind}")
        self._deliver_reminder(DYNAMIC_REMINDER_TEMPLATES[kind])
        self._dynamic_next_time = time.time() + _next_dynamic_delay()

    def _deliver_reminder(self, reminder):
        """发出一条提醒：扫描确认人在场 → 切表情/摆头 + 念关键词 → 收尾 →
        （除了 want_eat）启动在场监测窗口。人不在时直接放弃，不进监测——
        催促一个不在场的人没有意义。

        三种 expression 走两条不同的收尾，但关键词最终都会经过
        apply_cute_substitutions()（"饭"/"吃饭"→"饭饭"，"饿"→"肚肚"+
        "空"，见该函数说明——want_drink/want_eat 的关键词池已经直接用的
        是替换后的词，这里对它们是空操作，只对 want_play 偶尔落回的
        REMINDERS 固定兜底列表才可能真的有替换发生）：
        - want_play（move_around）：关键词优先用天气现挑
          （get_weather_keywords() 拿不到结果时落回 reminder["keywords"]
          固定列表），说之前先左右摆头（play_reminder_swing_
          animation()），说完切到"玩"这个表情并一直保持。
        - want_drink（drink_water）/want_eat（eat_lunch）：行为逻辑
          完全一样，只是关键词来源不同（get_drink_keywords()/
          get_eat_keywords()，各自的说明见函数定义）。时序是"委屈"
          过渡（摆头 + 委屈表情 + LED 呼吸灯，表达"小狗还在惦记这件事"）
          → 切"吃饭"表情念关键词 → 说完保持"吃饭"表情。
        这两条分支说完都不调 enter_happy()——那个会直接把表情设回 happy
        盖掉刚设的 play/eat，所以改成手动做 enter_happy() 静默分支同款
        的 state/session_active 记账，只是表情换成对应的自定义表情。

        **只有 want_play/want_drink 会启动在场监测窗口，want_eat 不会**
        ——按状态机设计，吃饭提醒是个终态，不会因为"吃太久"而生气，只有
        喝水/出去玩这两条提醒催促了之后人还不走才会生气。表情/LED 保持
        到主人下次真正的状态切换自然换掉，不需要 _play_angry_reminder()
        兜底收尾。"""
        label = reminder["label"]
        print(f"[REMINDER] {label}: 检查是否发出提醒...")
        if not self.scan_for_face():
            print(f"[REMINDER] {label}: 没找到人，不提醒")
            return

        expr_type = reminder["expression"]
        if expr_type == "want_play":
            set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)
            keywords = reminder["keywords"]
            weather_keywords = get_weather_keywords(self.qweather_api_key, self.qweather_api_host)
            if weather_keywords:
                keywords = weather_keywords
            keywords = enforce_friend_needs_play(keywords)
            keywords = apply_cute_substitutions(keywords)

            play_reminder_swing_animation()
            self.speak_keywords(keywords)

            self.session_active = True
            self.state = State.HAPPY
            self.state_enter_time = time.time()
            set_expression("play")
            set_led_mode("solid", *WARM_WHITE_RGB)
        else:
            if expr_type == "want_drink":
                keywords = get_drink_keywords(self.qweather_api_key, self.qweather_api_host)
            else:  # want_eat
                keywords = get_eat_keywords()
            keywords = apply_cute_substitutions(keywords)

            set_expression("grieved")
            set_led_mode("breathe", *WARM_WHITE_RGB, period_ms=REMINDER_GRIEVED_LED_BREATHE_PERIOD_MS)
            play_reminder_swing_animation()

            set_expression("eat")
            set_led_mode("blink", *WARM_WHITE_RGB, period_ms=SORRY_LED_PERIOD_MS)
            self.speak_keywords(keywords)

            self.session_active = True
            self.state = State.HAPPY
            self.state_enter_time = time.time()
            set_led_mode("solid", *WARM_WHITE_RGB)

        if expr_type == "want_eat":
            print(f"[REMINDER] {label}: 提醒已发出，吃饭不进在场监测")
            return

        _presence_window_min = PRESENCE_WINDOW_SAMPLES * PRESENCE_SAMPLE_INTERVAL_SEC / 60
        print(f"[REMINDER] {label}: 提醒已发出，开始在场监测窗口（每 {PRESENCE_SAMPLE_INTERVAL_SEC}s "
              f"采样一次，共 {PRESENCE_WINDOW_SAMPLES} 次，约 {_presence_window_min:.0f} 分钟）")
        # 只作为"有一条提醒正在监测中"的互斥标记（非 None 时 _check_reminders()/
        # _check_dynamic_reminder() 都不会再发新提醒），窗口本身何时结束交给
        # _check_presence_monitoring() 按采样次数判断，不再靠比较这个时间戳。
        self._reminder_recheck_target = time.time()
        self._presence_samples = []
        self._presence_last_sample_time = time.time()
        self._presence_window_start = time.time()
        self._reminder_pending_label = label

    def _check_presence_monitoring(self):
        """在场监测窗口：提醒发出后，每 PRESENCE_SAMPLE_INTERVAL_SEC 采样一次
        人脸在不在，攒够 PRESENCE_WINDOW_SAMPLES 次样本后按在场率判定"主人是
        否一直在场"，决定要不要生气催促。跟旧版"过了固定时长就无条件生气"
        的区别是这里用采样次数而不是时间戳判断窗口结束——主人中途离开过
        （在场率不够）就视为已经处理过这条提醒，静默交还给 tick() 的常规
        状态管理，不催促。

        采样用短超时+不重试（跟 _check_face_worker()/_retrack_face_worker()
        这两个后台检测同一套参数），因为这是个"低优先级、错过一次无所谓"
        的背景任务，30 秒后自然会有下一次采样补上，不值得为了这一次多等
        默认的 5 秒+重试。detect_face_once() 内部的 api_get() 已经会吞掉
        请求异常返回 None/False，理论上不会往外抛异常，这里仍然包一层
        try/except 兜底，避免任何意外异常打断整个监测窗口。

        PRIVACY 状态下不采样（跟 tick() 里 PRIVACY 分支故意不做人脸检测是
        同一个理由——不该在隐私状态下无端拍照），跳过的这一轮既不算"在"
        也不算"不在"，单纯不计入样本、也不推进窗口。

        兜底：如果长时间待在 PRIVACY（或者别的原因导致一直采不到样），样本
        数可能永远攒不够 PRESENCE_WINDOW_SAMPLES，窗口会卡住不结束——
        self._reminder_recheck_target 就会一直非 None，把 _check_reminders()/
        _check_dynamic_reminder() 的互斥永久锁住，后续所有新提醒都发不出来。
        所以窗口结束的条件是"样本攒够 **或者** 窗口已经开了超过
        PRESENCE_WINDOW_MAX_SEC"，用现有样本（哪怕是 0 个）收尾，保证监测
        窗口最终一定会关闭。"""
        if self._reminder_recheck_target is None:
            return

        now = time.time()
        if (self.state != State.PRIVACY
                and now - self._presence_last_sample_time >= PRESENCE_SAMPLE_INTERVAL_SEC):
            self._presence_last_sample_time = now
            try:
                found, _ = self.detect_face_once(timeout=FACE_BG_TIMEOUT_SEC, _retry=False)
            except Exception as e:
                print(f"[Presence] 采样失败，跳过: {e}")
            else:
                self._presence_samples.append(found)
                total = len(self._presence_samples)
                running = sum(self._presence_samples)
                print(f"[Presence] Sample {total}/{PRESENCE_WINDOW_SAMPLES}: face={found}  "
                      f"(running: {running}/{total} = {running / total:.0%})")

        window_timed_out = now - self._presence_window_start >= PRESENCE_WINDOW_MAX_SEC
        if len(self._presence_samples) < PRESENCE_WINDOW_SAMPLES and not window_timed_out:
            return

        samples = self._presence_samples
        total = len(samples)
        detected = sum(samples)
        ratio = (detected / total) if total else 0.0
        label = self._reminder_pending_label
        self._reminder_recheck_target = None
        self._presence_samples = []
        self._reminder_pending_label = None

        timeout_note = "（样本不足，窗口超时兜底结束）" if window_timed_out else ""
        if ratio > PRESENCE_ANGRY_THRESHOLD:
            print(f"[Presence] Window complete: {detected}/{total} = {ratio:.0%}{timeout_note} — "
                  f"above threshold, triggering angry")
            print(f"[REMINDER] {label}: 主人一直在场（在场率 {ratio:.0%}），生气催促！")
            self._play_angry_reminder()
        else:
            print(f"[Presence] Window complete: {detected}/{total} = {ratio:.0%}{timeout_note} — "
                  f"below threshold, skipping angry")
            print(f"[REMINDER] {label}: 主人中途离开过（在场率 {ratio:.0%}），不催促")

    def _angry_double_tap_check(self, baseline):
        """发一次 get_touch()，返回 (新的 double_tap baseline, 这次是否比
        baseline 多了一次双击, 这次读到的 held_ms)。face-wait 和
        forgive-wait 两处循环共用这一份查询+比较逻辑，不各写一份、也不用
        为双击和长按各发一次请求。直接读 get_touch() 原始值，不走
        self.check_touch()——跟 _game_check_abort() 是同一个理由，避免
        触发跟生气无关的触摸反馈灯/计数副作用。"""
        touch = get_touch()
        if touch is None:
            return baseline, False, 0
        current = touch.get("double_tap_count", baseline)
        return current, current != baseline, touch.get("held_ms", 0)

    def _angry_speak_keywords(self, keywords, last_double_tap):
        """生气专属的"按钮按一下+关键词 TTS 同步播放"，跟 _game_speak_
        keywords() 是同一个理由不能直接复用 speak_keywords()：那个内部调
        self.wait_for_playback()，会走 handle_touch_trigger() 的全局手势
        分发（双击→兴奋、碰屏幕→贴贴），这两个都会跟生气状态自己的
        "双击→原谅"判断打架。改成自己轮询 /status 的 playing 字段，用
        _angry_double_tap_check() 代替 handle_touch_trigger()。

        返回 (新的 last_double_tap baseline, 是否正常播完)——被打断时
        （双击/长按）调用方应该直接去 _angry_forgive()，不用再等 1 秒/
        转头了。"""
        set_button("up")
        time.sleep(BUTTON_PRESS_MS / 1000)
        next_wav = tts_to_wav(keywords[0], "angry_kw_0") if keywords else None
        for i in range(len(keywords)):
            set_button("down")
            time.sleep(BUTTON_PRESS_MS / 1000)
            set_button("up")
            wav_path = next_wav
            if wav_path:
                # 保持红色主题，不用其它状态惯用的暖白——生气播报期间还是
                # 生气，不需要临时借用暖白灯。
                set_led(*ANGRY_LED_RGB)
                set_subtitle(keywords[i])
                self.mic_stream.set_muted(True)
                finished_ok = False
                try:
                    started = start_play(wav_path)
                    if started:
                        deadline = time.time() + PLAY_TIMEOUT
                        while time.time() < deadline:
                            last_double_tap, tapped, held_ms = self._angry_double_tap_check(last_double_tap)
                            if tapped or held_ms >= PRIVACY_HOLD_SEC * 1000:
                                stop_play()
                                break
                            status = get_status()
                            if status is not None and not status.get("playing", False):
                                finished_ok = True
                                break
                            time.sleep(0.1)
                        else:
                            stop_play()
                finally:
                    if finished_ok:
                        time.sleep(MIC_UNMUTE_COOLDOWN_SEC)
                    self.mic_stream.set_muted(False)
                    set_subtitle("")
                if started and not finished_ok:
                    print("[REMINDER] 播报被双击/长按打断")
                    set_button("off")
                    return last_double_tap, False
                set_led(off=True)
            if i + 1 < len(keywords):
                next_wav = tts_to_wav(keywords[i + 1], f"angry_kw_{i + 1}")
            time.sleep(KEYWORD_GAP_SEC)
        set_button("off")
        set_led_mode("solid", *ANGRY_LED_RGB)
        return last_double_tap, True

    def _play_angry_reminder(self):
        """复查确认主人仍在座位 → 生气催促序列：切表情+红灯闪烁→常亮 →
        （不转头，原地）确认人脸还在 → 按钮播报"小狗 生气" → 停顿 1 秒 →
        左转"哼，不理你" → 全程保持生气表情，直到双击头顶原谅（长按也可以
        强制退出，兜底）→ 回正 → 保持生气 3 秒 → 开心。同步阻塞方法，执行
        期间 tick() 停摆。

        表情从进入生气到原谅之前只切一次、不再变化——之前的版本里确认
        人脸这一步复用的是 scan_for_face()，它内部会把表情切成 curious、
        还会转头扫视好几个位置，等于生气表情播出来一半就被盖掉，转头的
        动作也跟"生气瞪着你不理你"的意图不符（实测反馈就是这个转头扫视
        被用户看成了"舵机只有识别人脸的时候有运动"）。现在改成不移动
        舵机、只在当前角度用 detect_face_once() 轻量确认，找不到就原地等
        （随时可以被双击/长按打断，不用等到转完头才能原谅）。"""
        print("[REMINDER] 主人还在! 生气催促...")
        self.state = State.ANGRY
        self.state_enter_time = time.time()

        set_expression("angry")
        set_led_mode("blink", *ANGRY_LED_RGB, period_ms=ANGRY_LED_BLINK_PERIOD_MS)
        # 固件 updateLed() 的 BLINK 一个完整周期（亮+暗）就是 period_ms，
        # 不是亮暗各占一半再乘二。
        time.sleep(ANGRY_LED_BLINK_PERIOD_MS / 1000.0 * ANGRY_LED_BLINK_COUNT)
        set_led_mode("solid", *ANGRY_LED_RGB)

        last_double_tap = self.last_double_tap_count
        if last_double_tap is None:
            last_double_tap, _, _ = self._angry_double_tap_check(0)

        found, face_x = self.detect_face_once()
        while not found:
            time.sleep(FACE_CHECK_INTERVAL_SEC)
            last_double_tap, tapped, held_ms = self._angry_double_tap_check(last_double_tap)
            if tapped:
                print("[REMINDER] 等人脸期间双击头顶 → 原谅")
                self._angry_forgive()
                return
            if held_ms >= PRIVACY_HOLD_SEC * 1000:
                print("[REMINDER] 长按头顶 → 强制退出生气")
                self._angry_forgive()
                return
            found, face_x = self.detect_face_once()

        self.face_detected = True
        self.face_confirm_count = FACE_CONFIRM_FRAMES
        self.last_face_seen_time = time.time()
        self.track_face_servo(face_x)

        last_double_tap, spoke_ok = self._angry_speak_keywords(["小狗", "生气"], last_double_tap)
        if not spoke_ok:
            self._angry_forgive()
            return

        time.sleep(ANGRY_FACE_FOUND_DELAY_SEC)

        status = get_status()
        # 明确把当前 pitch 也传回去，不能只传 yaw——firmware 的 handleServo()
        # 对没传的参数不是"保持不变"，而是各自 fallback 成硬编码默认值
        # （pitch 缺省会直接变成 450），这次调试才发现的：之前每次"只转
        # yaw"的调用其实都在悄悄把 pitch 一起拽回 450，只是大多数时候 pitch
        # 本来就已经在 450 附近所以没被注意到。这里显式带上当前 pitch，
        # 保证这一步只有 yaw 在变，转头动作干净、不会跟"顺带把头抬正"的
        # 竖直动作混在一起，看起来更不像"单纯在转"。
        current_yaw = status.get("yaw", 0) if status else 0
        current_pitch = status.get("pitch", 450) if status else 450
        target_yaw = max(min(current_yaw + ANGRY_YAW_TURN, 1280), -1280)
        print(f"[REMINDER] 左转：yaw {current_yaw} → {target_yaw}")
        move_servo(yaw=target_yaw, pitch=current_pitch, speed=ANGRY_YAW_SPEED, mute=True)

        # 不能对着这个非阻塞的 move_servo() 盲等一个猜的固定时长就去开始
        # 监听双击——上一版就是这么写的，双击稍微快一点就会在舵机还没转到
        # 位时就调用 _angry_forgive()（里面的 go_home() 会立刻把目标角度
        # 覆盖掉），物理上只看到舵机拐了个弯直接回正，"左转不理你"这一下
        # 完全看不出来（跟 enter_dead() 踩过的"抬头动画被截断"是同一个坑，
        # 见那边的说明）。轮询 /status 确认真的转到位了（有容差、有超时，
        # 超时就按当前角度继续，不会卡死）再进入双击监听。实机测过这个
        # 舵机转 200 多个单位大概需要 1~2 秒才能进入容差范围，之前 1.5 秒
        # 的超时偏紧，放宽到 3 秒留够余量。
        deadline = time.time() + ANGRY_YAW_SETTLE_TIMEOUT_SEC
        settled = False
        while time.time() < deadline:
            status = get_status()
            if status and abs(status.get("yaw", 0) - target_yaw) <= ANGRY_YAW_SETTLE_TOLERANCE:
                settled = True
                break
            time.sleep(0.1)
        final_yaw = status.get("yaw", "?") if status else "?"
        print(f"[REMINDER] 左转{'已到位' if settled else '等待超时，按当前角度继续'}，当前 yaw={final_yaw}")

        self._angry_wait_for_forgive(last_double_tap)

    def _angry_wait_for_forgive(self, last_double_tap):
        """保持生气侧头，等双击头顶触发原谅（长按也可以强制退出，兜底）。
        last_double_tap 是调用方（_play_angry_reminder()）已经在等人脸阶段
        跟踪过的基准值，不用重新读一次。"""
        while True:
            time.sleep(TOUCH_POLL_SEC)
            last_double_tap, tapped, held_ms = self._angry_double_tap_check(last_double_tap)
            if tapped:
                print("[REMINDER] 双击头顶 → 原谅")
                self._angry_forgive()
                return

            if held_ms >= PRIVACY_HOLD_SEC * 1000:
                print("[REMINDER] 长按头顶 → 原谅（兜底）")
                self._angry_forgive()
                return

    def _angry_forgive(self):
        """双击/长按 → 原谅收尾：回正对人脸 → 保持 3 秒生气 → 开心。"""
        go_home()
        time.sleep(ANGRY_FORGIVE_HOLD_SEC)

        # 整个生气序列（找人 + 等待原谅）可能持续很久，期间 tick() 没有机会
        # 调用 check_voice_wake() 排空 MicStream 队列——攒下的这段音频不是
        # 用户"刚刚"说的话，原样丢弃，不然 enter_happy() 之后下一次 tick()
        # 会把这段陈旧的录音当成新说的话处理，跟 CLAUDE.md"装死期间语音会
        # 堆积"是同一个坑；只是装死状态下 tick() 仍在跑、check_voice_wake()
        # 每个 tick 都能持续排空，生气是完全阻塞的没有这个机会，只能在序列
        # 结束这一刻做一次性冲刷。
        self.mic_stream.take_utterance()

        self.enter_happy()
        print("[REMINDER] 原谅! 切回开心")

    # ---------- 计时器 ----------

    def idle_seconds(self):
        return time.time() - self.last_interaction

    def state_duration(self):
        return time.time() - self.state_enter_time

    # ---------- 主循环 ----------

    def tick(self):
        self.tick_count += 1
        print(f"[循环] tick #{self.tick_count}, 状态={self.state.value}")

        # 人脸检测（会打 /camera，还要跑一次 mediapipe 推理，单次可能要大半秒）
        # 每隔一个 tick 才轮到，避免跟别的轮询请求挤在同一轮里。触摸不跟着
        # 一起轮流了——check_touch() 自己已经有 TOUCH_POLL_SEC 节流，不需要
        # 再靠 poll_slot 额外限流，之前把它也放进轮流里，等于把摸屏幕/长按的
        # 检测频率平白无故砍掉一半，碰上人脸检测那一轮恰好较慢（/camera 请求
        # 慢，见下面 run() 里 tick 耗时相关的说明）时尤其明显，"碰一下屏幕"
        # 摸头反应会感觉迟钝。语音唤醒也不算在轮转里——check_voice_wake()
        # 现在只是查一下后台流式监听线程的内存队列，不发请求，每个 tick 都
        # 查一次没有额外开销，还能让语音触发反应更快。
        poll_slot = self.tick_count % 2   # 1 时才轮到人脸检测（do_face_check）

        # --- 触摸（最高优先级）---
        # 判断/处理逻辑都在 handle_touch_trigger() 里，见那边的文档字符串。
        touch = self.check_touch()
        if touch is not None and self.handle_touch_trigger(touch):
            return

        # --- 摇晃/拿起（BMI270，优先级仅次于触摸）---
        # is_shaking 这个 tick 只查一次、存下来，进入分支和下面"晕"状态内的
        # 退出判断共用同一次结果，不用重复请求 /status。不管当前处于 tick()
        # 能观察到的哪个状态（常态/开心/兴奋/困倦/隐私/抱歉）都可以被打断，
        # 包括隐私——物理摇晃/拿起设备是很明确的"人在跟我互动"信号，比隐私
        # 状态下偶尔混进来的杂散摄像头帧可信得多。对话/捉迷藏这类同步阻塞的
        # 流程正在进行时 tick() 本身没有机会跑到这里，不需要额外排除。装死
        # 例外：小狗"死了"，不应该被晃醒，只认头顶双击（上面触摸分支已经
        # 处理过了）。
        is_shaking = self.check_shaking()
        if is_shaking and self.state not in (State.DIZZY, State.DEAD):
            print("[触发] 检测到摇晃/拿起 → 晕")
            self.dizzy_shake_stopped_at = None
            self.transition(State.DIZZY)
            return

        # --- 语音唤醒（好奇/思考期间已经在处理语音了，不重复检查；头顶正被
        #     按住时也跳过——check_voice_wake() 一旦捕捉到语音段就会同步跑完
        #     整个对话链路，好几秒起步，这几秒里 tick() 完全被占住，正在
        #     进行中的长按（进/出隐私）会因此错过后续轮询，等对话链路跑完
        #     再来看 held_ms，用户多半已经松手了。self.touch_pressed 由
        #     check_touch() 维护，不要求本轮 tick 恰好轮到触摸这个 poll_slot
        #     也能读到最近一次的按住状态，最多滞后一个轮询周期，长按持续
        #     3 秒以上，这点滞后不影响判断）。装死状态不排除在外——理由见
        #     check_voice_wake() 内部 DEAD 分支的说明，跟隐私状态是同一个
        #     "必须继续调用才能持续排空队列"的道理，不能简单地在这里跳过。
        if self.state not in (State.CURIOUS, State.THINKING) and not self.touch_pressed:
            if self.check_voice_wake():
                return

        # --- 手势扫描窗口（碰屏幕"贴贴"之后的互动期待期，见
        #     enter_tietie() 末尾设置 self.gesture_scan_until）---
        # 窗口期内每个 tick 都调 check_gesture()（自己按 GESTURE_POLL_SEC
        # 节流，不会真的每 tick 都发请求），检测到"手指枪"就直接触发
        # enter_dead()，检测到"再见"手势（挥手，或五指捏住再放开）就直接
        # 触发 enter_goodbye()，两种情况都会在 check_gesture() 内部关闭
        # 窗口。窗口期内故意不做人脸检测——不是为了省 ESP32 负载
        # （MediaPipe 推理在电脑端跑，ESP32 端不管拍的照片给谁用开销都
        # 一样），而是人脸检测会触发 track_face_servo() 移动舵机，画面跟着
        # 偏移会让手势检测的取景不稳定。
        gesture_window_active = time.time() < self.gesture_scan_until
        if gesture_window_active:
            self.check_gesture()
        elif self.gesture_scan_until != 0.0:
            # 窗口自然过期（不是被 check_gesture() 检测成功后主动关闭——那
            # 种情况 gesture_scan_until 已经在 check_gesture() 里被设成
            # 0.0，不会再进这个分支，LED 交给 enter_dead()/enter_goodbye()
            # 自己的动画接管，不需要也不应该在这里覆盖）：清掉残留的确认
            # 状态（手指枪连续帧计数、挥手历史、"已捏拢等放开"标记），避免
            # 留到下一次开窗口时被当成"本来就有"的数据；LED 换回当前状态
            # 本该有的常驻效果，还回去给 restore_state_led() 管。
            # gesture_scan_until 归零标记"已经处理过"，避免每个 tick 都
            # 重复调 restore_state_led()。
            self._reset_gesture_scan_state()
            self.gesture_scan_until = 0.0
            self.restore_state_led()

        # --- 状态内行为 ---
        # 人脸检测（会发 /camera 请求）只在轮到 poll_slot==1 时真正执行，且
        # 手势扫描窗口开着的时候整个跳过（理由见上面那段）；计时器类判断
        # （空闲/困倦/兴奋持续时间）不发请求，每轮都可以正常判断，不受影响。
        do_face_check = (poll_slot == 1) and not gesture_window_active

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

        elif self.state == State.DIZZY:
            # 不轮询摄像头——设备这时候正被摇晃/托举，拍到的画面大概率是
            # 模糊的，没必要浪费一次 /camera 请求。is_shaking 是这个 tick
            # 顶部已经查过的结果，摇晃/拿起信号消失后不立刻转"兴奋"，要在
            # "晕"表情上继续停留 DIZZY_LINGER_SEC 秒（不是瞬间切走，给观众
            # 一个"晃完还在晕"的缓冲）——第一次观察到信号变 false 时记下
            # 这一刻的时间戳，之后每个 tick 比较过去了多久，够了才真正切到
            # "兴奋"（收尾顺序：晕 → 兴奋 → 开心+人脸追踪，兴奋结束后走
            # tick() 里 EXCITED 分支已有的逻辑，人脸还在场就 enter_happy()，
            # 不在场就回常态，这一段不用另外写）。如果信号中途又恢复了
            # （摇了一下又停、又摇），把计时器清掉重新等，不能让上一次已经
            # 走了一半的停留时间继续算数。
            if is_shaking:
                self.dizzy_shake_stopped_at = None
            else:
                if self.dizzy_shake_stopped_at is None:
                    self.dizzy_shake_stopped_at = time.time()
                elif time.time() - self.dizzy_shake_stopped_at >= DIZZY_LINGER_SEC:
                    print("[触发] 晕表情停留结束 → 兴奋")
                    self.transition(State.EXCITED)

        elif self.state == State.DEAD:
            # 不做任何事——不轮询摄像头（人脸/手势检测都不需要在这个状态下
            # 跑）、不判断状态持续时间、不累加空闲计时。退出只认头顶双击，
            # 已经在最上面的触摸分支里处理过了（handle_touch_trigger() 对
            # DEAD 状态的早期返回逻辑），这里到达时说明双击还没发生，继续
            # 保持装死姿势，什么都不用做，跟 PRIVACY 分支的 pass 是同一个
            # 写法。
            pass

        # --- 定时提醒 + 生气催促（最低优先级）---
        # 在场监测采样（_check_presence_monitoring()）跟当前具体状态无关
        # （除了 PRIVACY 不拍照，见该方法说明），提醒一旦发出就要不间断
        # 采样，不能因为小狗恰好不在 IDLE/HAPPY 就漏掉采样点，导致窗口结束
        # 时样本不足被误判成"人不在"。只有"要不要发一条新提醒"
        # （_check_reminders()）才限定在 IDLE/HAPPY——困倦/隐私/游戏/对话
        # 进行中不应该被新提醒打断。
        self._check_presence_monitoring()
        if self.state in (State.IDLE, State.HAPPY):
            self._check_reminders()
            self._check_dynamic_reminder()

        # CURIOUS / THINKING 是瞬时状态：run_conversation_turn() 会同步跑完
        # 录音→识别→LLM→分支应对的整个过程才返回，tick() 观察不到这两个状态。

    def run(self):
        # 兜底显式唤醒屏幕——万一上次是手动 curl /display?off=1 关掉的，或者
        # 设备本身没重启（只是重启了 host 脚本），屏幕不会自己醒过来。
        set_display(on=True)
        # 开机迎接：主动抬头扫描找人，而不是像以前那样直接进常态被动等待
        # ——如果开机时人已经在设备前面，应该立刻被看到、进入人脸追踪，不用
        # 等第一次 IDLE 状态下的被动 check_face()（最多要等 FACE_CHECK_
        # INTERVAL_SEC 才轮到）。找到人就直接进开心（自带完整的追踪逻辑，
        # 见 enter_happy()）；没找到就跟原来一样落回常态。
        print("[引擎] 开机迎接：抬头找人...")
        if self.scan_for_face(pitch=HAPPY_PITCH):
            self.enter_happy()
        else:
            play_idle_animation()
        time.sleep(1)

        hb_interval = 30
        last_hb = time.time()

        try:
            while True:
                tick_start = time.time()
                self.tick()

                now = time.time()
                if now - last_hb > hb_interval:
                    print(
                        f"[心跳] 状态={self.state.value}, "
                        f"空闲={self.idle_seconds():.0f}s, "
                        f"人脸={'有' if self.face_detected else '无'}"
                    )
                    last_hb = now

                # tick() 本身可能已经花了不少时间（人脸检测那一轮要打
                # /camera 再跑一次 mediapipe 推理，单次可能大半秒到一两秒）；
                # 如果不管这个耗时、无脑再睡满 MAIN_LOOP_INTERVAL_SEC，一次
                # 慢 tick 就会让下一轮触摸/语音检测的实际间隔翻倍甚至更多，
                # 表现为"碰一下屏幕/长按头顶反应特别迟钝"——只睡够"凑满一个
                # 完整间隔"还欠的那部分，tick() 已经耗时超过一个间隔就不再
                # 补睡。
                elapsed = time.time() - tick_start
                if elapsed > SLOW_TICK_WARN_SEC:
                    print(f"[主循环] tick #{self.tick_count} 耗时 {elapsed:.2f}s，"
                          f"明显超过正常水平")
                time.sleep(max(0.0, MAIN_LOOP_INTERVAL_SEC - elapsed))

        except KeyboardInterrupt:
            print("\n[引擎] Ctrl+C，回到常态...")
            play_idle_animation()
        finally:
            # 不管是正常 Ctrl+C 退出还是主循环里跑出了没接住的异常，都要把
            # 本地麦克风的音频流关掉。
            print("[引擎] 清理流式监听...")
            self._running = False
            self.mic_stream.stop()
            print("[引擎] 已退出。")


if __name__ == "__main__":
    PuppyEngine().run()
