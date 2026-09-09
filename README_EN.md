# StackChan Puppy Behavior Engine

A **dog-behavior-inspired emotional expression system** built on the [M5Stack StackChan](https://github.com/stack-chan/stack-chan) desktop robot. The puppy tracks your face, gives a little "nuzzle" reaction when you touch its screen, understands what you say and answers back with on-screen button animations, recognizes hand gestures, plays hide-and-seek with you, and reminds you to drink water or eat on schedule.

(What follows is a bit of a ramble — skip ahead to the architecture and state machine if you just want the technical bits.)

## Why a dog, instead of just wiring up an LLM?

1. **Observation**: a desktop robot's advantage over an on-screen desktop pet is a richer kind of interaction — it lets downtime shift from typing into light physical/voice interaction, which lets you relax for a bit without pulling you completely out of flow.
2. **A better alternative, considered and rejected**: if what I actually wanted was a service-style tool agent, talking to it in text is more precise and direct anyway. A desktop robot can only give low-frequency, vague feedback through voice and a small screen, and it's hard to build trust in a "reliable tool" on feedback that imprecise.
3. **The dog as a symbol**: since a desktop robot is mostly there for emotional companionship anyway, that role is a lot like a dog's place in human society — it's fine for it to be a bit dumb ^_^
4. **Technical constraints**: the stock Xiaozhi framework has no way to plug in Claude, and even if it did, there'd be network latency.
5. **Personal preference**: I just like dogs.

If you like the idea too, come raise a puppy with me~

## Interaction design notes

### Expression

M5Stack's own Avatar library actually ships a dog template expression, but the stock version has no ears, so it ends up looking like a dog's face stuck onto a square head. So I designed my own puppy face with ears.

Most dog breeds you commonly see around here have prick (upright) ears, but I found that prick ears look oddly sharp on this small screen — probably because StackChan's overall design leans rounded. So I went with a beagle's floppy ears instead. Floppy ears have a larger surface area and a softer outline, which visually seems to act as a "transition element," blurring the break between the square shell and the animal face.

That said, going by uncanny valley theory, adding ears with clearly animal-like features to a robot that has no skin should, in theory, push the degree of zoomorphism toward a middle value — which should, in theory, make it *more* likely to trigger the uncanny valley effect, not less.

So why does adding ears — especially floppy ones — actually look better? It might come down to two factors in machine design: design ambiguity and design atypicality [(MK Strait, 2017)](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2017.01366/full).

In short, "design ambiguity" is about what category an observer's first glance sorts an object into: is this a person, or a machine?

"Design atypicality" is about the gap an observer perceives between a design and the category they've sorted it into: does this "person" have features a person shouldn't have? Take the much-discussed AI template face as an example — at a glance it clearly reads as a human face, but look closer and it has no catchlights in the eyes, no pores, no variation in expression — features a real person should have. That's high atypicality.

In that paper, atypicality was the strongest driver of negative reactions to robots. Based on that, my guess is that adding puppy ears to StackChan doesn't improve ambiguity, but it does reduce atypicality — the floppy-ear feature makes the "dog" categorization more definite. So the overall unease goes down too.

### Sound

A similar issue came up in the sound design. The first version of the TTS used a female human voice, and kept full sentences intact.

Maybe it's because I'd already mentally filed it as a puppy — so a human voice coming out of it felt like a sharply atypical noise to me. But if it didn't speak at all and communicated purely through subtitles, it felt like it was missing interactivity.

So the second version swapped full sentences for keywords instead, mimicking a dog "talking" via AAC-style sound buttons. That cut down a lot of the friction between the voice and the animal-robot form factor. Along the way I also considered switching to a mechanical-sounding child's voice, thinking it might suit the small, cute form better. But I realized that since a desktop robot can't really shed its "tool" identity, and StackChan's shape still has obvious robotic features, giving it a child's voice would end up feeling like objectifying a child. So I dropped that idea.

So what voice should it actually have? Film and games have surely run into this question before, so I thought of the Minions and [Animalese](https://github.com/Acedio/animalese.js). Both effects work by speeding up speech and applying some fixed mapping rules to turn language into unintelligible sound, while still preserving intonation — keeping a sense that this is a non-human-but-somewhat-human-like character actually talking.

So my final approach was: replace the original full-sentence delivery and the gendered, aged human voice with keywords and a neutral, humanlike voice, to loosen the association between the animal-robot and a human.

Working through this sound design also got me thinking that zoomorphic robots probably sit on two separate continuums of realism. On one hand, the uncanny valley effect may come from resemblance to an animal; on the other, it may also come from the design atypicality introduced once a zoomorphic robot is given human traits in service of human-robot interaction. And a voice with linguistic meaning is one important factor in that second continuum.

## Technical Approach

It's built on top of [zziying/stackchan-openapi](https://github.com/zziying/stackchan-openapi)'s HTTP API architecture: the ESP32 only handles hardware execution, while all the AI/behavior decisions run on a host computer.

### Architecture

```
Computer (the brain)                            StackChan (the body, ESP32-S3)
┌─────────────────────────────┐                ┌──────────────────────────┐
│ puppy_engine_v4.py (FSM)     │   WiFi HTTP    │  Servos (yaw/pitch)      │
│  ├─ MediaPipe face/hand      │ ─────────────▶ │  Expression screen        │
│  ├─ FunASR speech-to-text    │ ◀───────────── │  Camera (GC0308)          │
│  ├─ DeepSeek LLM (intent)    │                │  Touch sensors (head+screen)│
│  ├─ animalese speech synth   │                │  Microphone / speaker      │
│  └─ local wireless mic input │                │  RGB LED                  │
└─────────────────────────────┘                └──────────────────────────┘
```

The computer and StackChan talk over the same WiFi hotspot; StackChan exposes a set of HTTP endpoints (`/face`, `/servo`, `/touch`, `/camera`, `/play`, `/stream`, `/led`, `/status`, etc.). The host-side state machine decides *what* to do, and the ESP32 only handles *how* to execute it.

### State Machine Overview

The behavior engine is built around a dozen or so states — **Idle, Happy, Excited, Sleepy, Privacy, Curious, Thinking, Sorry, Dizzy, Play Dead, Angry, Hide-and-Seek** — each triggered by a different kind of input (face tracking, voice conversation, touch gestures, shake detection, scheduled reminders, ...), and each with its own expression, servo motion, and LED pattern.

The full state-transition map is maintained as a [Mermaid](https://mermaid.js.org/) diagram, covering every voice/vision/touch/time-triggered branch not spelled out below:

![State machine diagram](docs/state_machine_en.svg)

Two of the more fun behaviors:

1. **Hide-and-seek**: triggered by saying (in Chinese) "let's play hide and seek." Hold the object you want to hide in front of the puppy's camera so it can take a "look" — it reports back what it thinks the object is; if it got it wrong, there's a short window to say "not this one" and it'll take another look. Once confirmed, it "closes its eyes" and counts down, then sweeps the servos around the room to search for the object.
2. **Play dead**: touching the screen triggers a brief "nuzzle" reaction, which opens a roughly 15-second gesture-recognition window. Making a "finger gun" gesture about 5 cm in front of the device's camera during that window triggers the puppy's "play dead" state; double-tapping the top of its head wakes it back up.

More features (like a mood log) are planned — updates will come slowly.

### Demo videos

<table>
<tr>
<td width="50%">

https://github.com/user-attachments/assets/eae52002-e6ad-4e85-b8a6-3ef63f604e24

Voice conversation demo: the puppy replies with animalese sounds

</td>
<td width="50%">

https://github.com/user-attachments/assets/bad6f06d-566c-4c22-829f-9d35b4a11e76

Finger-gun gesture triggering "play dead"

</td>
</tr>
</table>

### Notes on setup

- **Voice conversation depends on a large language model you bring yourself** (either a reasoning or non-reasoning model works — DeepSeek or any compatible API). Without one configured, face tracking, touch reactions, and everything else still work fine — the puppy just won't understand what you're saying. Giving the "drink water / go outside" reminders weather-flavored keywords also needs a weather API (currently QWeather). Both are optional enhancements: missing either just falls back to fixed text, without affecting anything else.
- **Gesture recognition (finger-gun → play dead, etc.) runs entirely locally** via a MediaPipe hand-landmark model — you only need to download the model file once, no API key required. The hide-and-seek game's object recognition can optionally call out to a vision-capable LLM (currently Qwen-VL) for better accuracy, but it still works without one, falling back to simple color-histogram matching.
- **Voice wake-up currently triggers on a volume/RMS threshold.** Using an external microphone plugged into the computer is recommended, to cut down on ambient noise (especially servo motor noise). You can switch to the robot's built-in microphone instead, but recognition accuracy may drop noticeably.

## Hardware

- M5Stack StackChan kit (CoreS3, ESP32-S3): GC0308 camera, dual microphones, speaker, 2 servos (yaw/pitch), head touch sensor + touchscreen, RGB LED.
- A computer that can run Python (Windows/macOS/Linux all work); a GPU helps but isn't required.
- A wireless microphone (USB receiver, used by the computer to capture speech).
- The computer and StackChan need to be on the same WiFi network (using the computer's own hotspot is recommended).

## Quick Start

### 1. Flash the firmware

```bash
# Copy and fill in your own WiFi/IP settings
cp firmware/config.h.example firmware/config.h
# Edit firmware/config.h: WIFI_SSID / WIFI_PASSWORD / your computer's IP, etc.

arduino-cli compile --fqbn m5stack:esp32:m5stack_cores3 firmware
arduino-cli upload --fqbn m5stack:esp32:m5stack_cores3 --port <your-serial-port> firmware
```

### 2. Set up the host side

```bash
conda create -n stackchan python=3.10
conda activate stackchan
pip install requests numpy opencv-python mediapipe sounddevice scipy \
            funasr torch torchaudio pypinyin

# Copy and fill in your own API keys (all optional enhancements — missing
# ones just degrade gracefully / skip the corresponding feature)
cp .env.example .env
```

You'll also need to update `BASE_URL` (StackChan's IP) and `COMPUTER_IP` (your computer's IP on that WiFi network) near the top of `host/puppy_engine_v4.py` to match your actual setup.

### 3. Run it

```bash
python host/puppy_engine_v4.py
```

The first run automatically downloads `animalese.wav` (the letter-sound audio library) and the FunASR speech models, which requires internet access. `host/hand_landmarker.task` (the MediaPipe gesture-detection model) needs to be downloaded manually once:

```bash
curl -o host/hand_landmarker.task \
  "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
```

## Project Structure

```
firmware/
├── firmware.ino          # Main firmware: HTTP API server + expression rendering
├── PuppyFace.h            # Custom puppy face components (eyes/nose/ears)
├── config.h.example       # WiFi/network config template
└── expr_preview/          # Standalone minimal sketch for designing new expressions

host/
└── puppy_engine_v4.py     # Behavior state machine (face/gesture detection, touch, voice, main loop)
```

## Credits

- Hardware and firmware foundation: [stack-chan](https://github.com/stack-chan/stack-chan), [zziying/stackchan-openapi](https://github.com/zziying/stackchan-openapi)
- Speech synthesis algorithm reference: [animalese.js](https://github.com/Acedio/animalese.js)
