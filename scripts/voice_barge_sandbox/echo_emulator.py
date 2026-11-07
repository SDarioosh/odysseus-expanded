"""Echo emulator for the barge-in sandbox (docs/fork/sandbox.md#voice-barge-in).

A Wyoming satellite that Home Assistant connects to, with the barge-in logic of the Echo
app (SDarioosh/odysseus-echo) ported line by line from:

  satellite/Satellite.kt                onWakeWordDetected, interruptResponse,
                                        handleWakeWordDetection, isResponding,
                                        processMessage, startAudioPipeline
  satellite/SatelliteAudioPipeline.kt   stages, run-pipeline, audio, watchdog, stop
  odysseus/HaRunGate.kt                 waiting for HA to end the interrupted run
  odysseus/BargeIn.kt                   asking Odysseus to end that run's turn (--mode after)
  odysseus/OdysseusTurn.kt              POST /satellite/turn when the owner stops speaking

--mode before: the app as of 0.14.0+odysseus.13.
--mode after:  0.14.0+odysseus.14 (POST /satellite/interrupt on a barge-in, also after a
               long "Thinking…").

The scenario: "hey athena" + question 1; ``--barge-after`` seconds into "Thinking…"
(Odysseus is answering), "hey athena" + question 2. It prints a RESULT: whether question 2
was answered, and whether the interrupted answer was played. Fictional questions only.
"""
import argparse
import asyncio
import json
import os
import time
import uuid
import wave
from functools import partial
from pathlib import Path

import httpx
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, Satellite
from wyoming.server import AsyncEventHandler, AsyncServer

# where the spoken WAVs (wake.wav, q1.wav, q2.wav) and next_question.txt are
HERE = Path(os.environ.get("BARGE_WORK", Path.home() / "sbx/barge"))
FRAME = 1280  # 80 ms at 16 kHz
T0 = time.monotonic()
DEVICE_ID = "emu-" + uuid.uuid4().hex[:8]
EVENTS: list[tuple[float, str]] = []


def log(msg):
    t = time.monotonic() - T0
    EVENTS.append((t, msg))
    print(f"{t:7.2f}  {msg}", flush=True)


def now_ms():
    return int(time.time() * 1000)


# ------------------------------------------------------------------------- microphone
class Mic:
    """80 ms frames in real time: silence, or the WAV being "spoken"."""

    def __init__(self):
        self.subs: list[asyncio.Queue] = []
        self.speech: list[bytes] = []

    def say(self, name):
        with wave.open(str(HERE / f"{name}.wav")) as w:
            pcm = w.readframes(w.getnframes())
        self.speech = [pcm[i:i + FRAME * 2].ljust(FRAME * 2, b"\0") for i in range(0, len(pcm), FRAME * 2)]

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.subs.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subs:
            self.subs.remove(q)

    async def run(self):
        nxt = time.monotonic()
        while True:
            frame = self.speech.pop(0) if self.speech else b"\0" * (FRAME * 2)
            for q in list(self.subs):
                q.put_nowait(frame)
            nxt += 0.08
            await asyncio.sleep(max(0, nxt - time.monotonic()))


# ------------------------------------------------------------------------- HaRunGate
class HaRunGate:
    MAX_WAIT = 15.0
    STALE_RUN = 200.0
    OLD_AUDIO_GRACE = 10.0

    def __init__(self):
        self.active = False
        self.active_since = 0.0
        self.changed = asyncio.Event()
        self.suppressing = False
        self.old_audio_open = False
        self.old_run_ended_at = 0.0

    def _set(self, v):
        self.active = v
        self.changed.set()

    def run_started(self):
        self.active_since = time.monotonic()
        self._set(True)

    def interrupted(self, fixed=False):
        if not fixed:  # 0.14.0+odysseus.13
            if self.is_active():
                self.suppressing = True
                self.old_audio_open = False
                self.old_run_ended_at = 0
                log("gate: Barge-in: waiting for Home Assistant to end the interrupted run")
            return False
        # the fix: true once per interrupted run
        if not self.is_active() or self.suppressing:
            return False
        self.suppressing = True
        self.old_audio_open = False
        self.old_run_ended_at = 0
        log("gate: Barge-in: waiting for Home Assistant to end the interrupted run")
        return True

    def is_active(self):
        if self.active and time.monotonic() - self.active_since > self.STALE_RUN:
            self._set(False)
        return self.active

    async def await_idle(self) -> bool:
        if not self.is_active():
            return True
        deadline = time.monotonic() + self.MAX_WAIT
        while self.active:
            self.changed.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                log("gate: Barge-in: Home Assistant didn't end the interrupted run in 15 s; not starting a new one")
                return False
            try:
                await asyncio.wait_for(self.changed.wait(), left)
            except asyncio.TimeoutError:
                pass
        return True

    def on_server_packet(self, type_) -> bool:
        is_audio = type_ in ("audio-start", "audio-chunk", "audio-stop")
        if type_ == "pipeline-ended":
            was = self.suppressing
            self._set(False)
            if was:
                self.suppressing = False
                self.old_audio_open = True
                self.old_run_ended_at = time.monotonic()
                log("gate: Barge-in: interrupted run ended")
                return True
            return False
        if self.suppressing:
            return True
        if self.old_audio_open:
            if type_ == "transcript":
                self.old_audio_open = False
            elif time.monotonic() - self.old_run_ended_at > self.OLD_AUDIO_GRACE:
                self.old_audio_open = False
            elif is_audio:
                if type_ == "audio-stop":
                    self.old_audio_open = False
                    log("gate: Barge-in: dropped the interrupted answer's audio")
                return True
        if type_ == "transcribe":
            self.run_started()
        return False


# ------------------------------------------------------------------------- pipeline
STAGES = ["INIT", "STARTING", "STARTED", "LISTENING", "VOICE_STARTED", "VOICE_STOPPED",
          "AWAITING_RESPONSE", "AWAITING_TTS", "STARTING_TTS", "STREAMING_TTS", "COMPLETED_TTS", "ENDED"]
S = {n: i for i, n in enumerate(STAGES)}


class Pipeline:
    TIMEOUT = 60.0  # APPConfig.pipelineTimeoutSeconds default

    def __init__(self, echo, mode, frames: asyncio.Queue | None, number):
        self.echo, self.mode, self.number = echo, mode, number
        self.stage = 0
        self.stage_start = time.monotonic()
        self.frames = frames  # DetectionBuffer, or None: subscribe on LISTENING
        self.mic_task = None
        self.mic_q = None
        self.done = asyncio.get_running_loop().create_future()
        self.posted = False
        self.heard = None
        self.spoken = []

    def set_stage(self, name):
        v = S[name]
        if v > self.stage:
            self.stage = v
            self.stage_start = time.monotonic()
            self.echo.on_state_change(self, name)
            self._mic(v)
            # OdysseusTurn.onStage: post the satellite context once past speech
            if self.mode == "WAKE_WORD_DETECTED" and not self.posted and S["VOICE_STOPPED"] <= v <= S["AWAITING_RESPONSE"]:
                self.posted = True
                asyncio.create_task(self.echo.post_satellite_turn())

    def _mic(self, v):
        if v == S["LISTENING"] and self.mic_task is None:
            q = self.frames
            if q is None:
                q = self.mic_q = self.echo.mic.subscribe()
            async def pump():
                while True:
                    f = await q.get()
                    await self.echo.send(Event("audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, f))
            self.mic_task = asyncio.create_task(pump())
        elif v > S["VOICE_STARTED"] and self.mic_task is not None:
            self.mic_task.cancel()
            self.mic_task = None
            if self.mic_q is not None:
                self.echo.mic.unsubscribe(self.mic_q)
            if self.frames is not None:
                self.echo.release_buffer(self.frames)

    @property
    def ended(self):
        return self.stage == S["ENDED"]

    async def start(self):
        self.set_stage("STARTING")
        asyncio.create_task(self.watchdog())
        self.set_stage("STARTED")
        if self.mode == "WAKE_WORD_DETECTED":
            await self.echo.send(Event("detection", {"name": "hey_athena", "timestamp": now_ms()}))
            await self.echo.send(Event("run-pipeline", {"name": "VACA emu", "start_stage": "asr", "end_stage": "tts",
                                                        "restart_on_end": False,
                                                        "snd_format": {"rate": 16000, "width": 2, "channels": 1}}))

    async def watchdog(self):
        t0 = time.monotonic()
        while not self.ended and self.stage != S["STREAMING_TTS"]:
            if time.monotonic() - t0 > self.TIMEOUT:
                self.finish("TIMED_OUT"); return
            in_stage = time.monotonic() - self.stage_start
            limit = {S["STARTED"]: 5, S["LISTENING"]: 5, S["VOICE_STARTED"]: 20, S["AWAITING_TTS"]: 5}.get(self.stage)
            if limit is not None and in_stage > limit:
                self.stop("TIMED_OUT"); return
            await asyncio.sleep(0.2)

    def stop(self, reason="FORCE_STOPPED"):
        if not self.ended:
            asyncio.create_task(self.echo.send(Event("audio-stop", {"timestamp": now_ms()})))
        self.finish(reason)

    def finish(self, reason):
        if self.ended:
            return
        if self.mic_task:
            self.mic_task.cancel()
        if self.mic_q is not None:
            self.echo.mic.unsubscribe(self.mic_q)
        if self.frames is not None:
            self.echo.release_buffer(self.frames)
        self.stage = S["ENDED"]
        log(f"pipeline {self.number}: ended ({reason})")
        if not self.done.done():
            self.done.set_result(reason)
        self.echo.on_finish(self, reason)

    async def process(self, ev: Event):
        t = ev.type
        if t == "transcribe":
            self.set_stage("LISTENING")
        elif t == "voice-started":
            self.set_stage("VOICE_STARTED")
        elif t == "voice-stopped":
            self.set_stage("VOICE_STOPPED")
        elif t == "transcript":
            self.heard = ev.data.get("text")
            log(f"pipeline {self.number}: transcript {self.heard!r}")
            self.set_stage("AWAITING_RESPONSE")
        elif t == "synthesize":
            text = ev.data.get("text", "")
            self.spoken.append(text)
            log(f"pipeline {self.number}: answer text {text!r}")
            if self.stage != S["STREAMING_TTS"]:
                self.set_stage("AWAITING_TTS")
        elif t == "audio-start":
            self.set_stage("STARTING_TTS")
        elif t == "audio-chunk":
            if self.stage == S["STARTING_TTS"]:
                self.set_stage("STREAMING_TTS")
                log(f"pipeline {self.number}: PLAYING answer audio")
        elif t == "audio-stop":
            await self.echo.send(Event("played", {}))
            self.set_stage("COMPLETED_TTS")
            self.pipeline_ended()
        elif t == "pipeline-ended":
            self.pipeline_ended()
        elif t == "error":
            log(f"pipeline {self.number}: HA error {ev.data}")
            await self.echo.send(Event("audio-stop", {"timestamp": now_ms()}))
            self.finish("ERRORED")

    def pipeline_ended(self):
        if self.stage in (S["AWAITING_TTS"], S["STREAMING_TTS"]):
            return
        self.finish("END_OF_PIPELINE")


# ------------------------------------------------------------------------- satellite
class Echo:
    def __init__(self, args):
        self.args = args
        self.fixed = args.mode == "after"
        self.mic = Mic()
        self.gate = HaRunGate()
        self.writer = None
        self.pipeline: Pipeline | None = None
        self.last_state_change = time.monotonic()
        self.detection_buffer = None
        self.arbitrating = False
        self.count = 0
        self.connected = asyncio.Event()
        self.pipelines: list[Pipeline] = []
        self.token = json.loads(Path(args.token_file).read_text())["token"]

    async def send(self, ev: Event):
        if self.writer is not None:
            await self.writer(ev)

    def release_buffer(self, q):
        self.mic.unsubscribe(q)
        if self.detection_buffer is q:
            self.detection_buffer = None

    def on_state_change(self, p, name):
        self.last_state_change = time.monotonic()
        log(f"pipeline {p.number}: {name}")
        if S[name] > S["VOICE_STARTED"] and self.detection_buffer is not None:
            self.release_buffer(self.detection_buffer)

    def on_finish(self, p, reason):
        pass

    def is_responding(self):
        p = self.pipeline
        if p is None or p.ended:
            return False
        age = time.monotonic() - self.last_state_change
        if p.stage <= S["AWAITING_RESPONSE"] and age > 15:
            return False
        if p.stage <= S["STREAMING_TTS"] and age > 30:
            return False
        return True

    async def post_satellite_turn(self):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(self.args.odysseus + "/satellite/turn",
                                 headers={"Authorization": "Bearer " + self.token},
                                 files={"device_id": (None, DEVICE_ID), "device_name": (None, "Kitchen Echo"),
                                        "area": (None, "Kitchen"), "wake_word": (None, "hey_athena"),
                                        "started_at": (None, str(now_ms()))})
            log(f"odysseus: satellite turn posted (HTTP {r.status_code})")
        except Exception as e:  # noqa: BLE001
            log(f"odysseus: satellite turn failed: {e}")

    async def interrupt_odysseus(self):
        """The fix: OdysseusLink.interruptTurn (POST /satellite/interrupt)."""
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.post(self.args.odysseus + "/satellite/interrupt",
                                 headers={"Authorization": "Bearer " + self.token},
                                 files={"device_id": (None, DEVICE_ID)})
            log(f"odysseus: interrupt sent (HTTP {r.status_code}) {r.text[:120]}")
        except Exception as e:  # noqa: BLE001
            log(f"odysseus: interrupt failed: {e}")

    def barge_in(self):
        """odysseus/BargeIn.kt interruptHaRun."""
        if not self.gate.interrupted(fixed=True):
            return False
        asyncio.create_task(self.interrupt_odysseus())
        return True

    # Satellite.processMessage
    async def process(self, ev: Event):
        if ev.type != "custom-event" and self.gate.on_server_packet(ev.type):
            if ev.type not in ("audio-chunk",):
                log(f"gate: dropped {ev.type}")
            return
        p = self.pipeline
        if p is not None and not p.ended:
            await p.process(ev)
        elif ev.type == "audio-start":
            log("satellite: audio-start with no pipeline -> START_STREAM_TTS pipeline")
            await self.start_pipeline("START_STREAM_TTS")
            await self.pipeline.process(ev)
        elif ev.type == "transcribe":
            await self.start_pipeline("REQUESTED_BY_SERVER")
            await self.pipeline.process(ev)
        elif ev.type not in ("audio-chunk",):
            log(f"satellite: ignored {ev.type} (no pipeline)")

    async def start_pipeline(self, mode, buffer=None):
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline = None
        if mode != "START_STREAM_TTS":
            self.gate.run_started()
        self.count += 1
        p = Pipeline(self, mode, buffer, self.count)
        self.pipelines.append(p)
        self.pipeline = p
        log(f"pipeline {p.number}: start ({mode})")
        await p.start()

    # SatelliteWakeWorkHandler.onWakeWordDetected
    async def on_wake_word(self):
        log("wake word heard")
        if self.is_responding():
            await self.interrupt_response()
            if self.fixed:
                self.barge_in()  # BargeIn.interruptHaRun
            else:
                self.gate.interrupted()
            await self.handle_wake_word()
        else:
            await self.handle_wake_word()

    async def interrupt_response(self):
        log("satellite: Wake word heard while responding, interrupting")
        if self.detection_buffer is not None:
            self.release_buffer(self.detection_buffer)
        if self.pipeline is not None:
            self.pipeline.stop()
        self.pipeline = None

    async def handle_wake_word(self):
        if self.is_responding():
            return
        if self.arbitrating:
            return
        self.arbitrating = True
        buffer = None
        try:
            buffer = self.detection_buffer = self.mic.subscribe()
            if self.pipeline is not None:
                self.pipeline.stop()
                self.pipeline = None
            if self.fixed:
                self.barge_in()  # BargeIn.interruptHaRun before HaRunGate.awaitIdle
            if not await self.gate.await_idle():
                log("satellite: NEW TURN GIVEN UP (the device goes idle)")
                return
            await self.start_pipeline("WAKE_WORD_DETECTED", buffer)
            buffer = None
        finally:
            self.arbitrating = False
            if buffer is not None:
                self.release_buffer(buffer)


class Handler(AsyncEventHandler):
    def __init__(self, echo, *a, **kw):
        super().__init__(*a, **kw)
        self.echo = echo

    async def handle_event(self, ev: Event) -> bool:
        e = self.echo
        if Describe.is_type(ev.type):
            await self.write_event(Info(satellite=Satellite(
                name="Echo emulator", area="Kitchen", description="Echo emulator",
                attribution=Attribution(name="sandbox", url="http://localhost"),
                installed=True, version="0.14.0")).event())
            return True
        if ev.type == "ping":
            await self.write_event(Event("pong", {"text": (ev.data or {}).get("text")}))
            return True
        if ev.type == "run-satellite":
            e.writer = self.write_event
            e.gate.active = False
            log("HA connected (run-satellite)")
            e.connected.set()
            return True
        if ev.type in ("pause-satellite",):
            return True
        if ev.type == "detect":
            return True
        await e.process(ev)
        return True


async def scenario(echo: Echo, args):
    await echo.connected.wait()
    await asyncio.sleep(1)
    q1, q2 = args.q1, args.q2
    # Turn 1
    (HERE / "next_question.txt").write_text(q1)
    log(f"USER: hey athena")
    await echo.on_wake_word()
    await asyncio.sleep(0.3)
    log(f"USER: {q1}")
    echo.mic.say("q1")
    # wait for "Thinking…"
    while not (echo.pipeline and echo.pipeline.stage == S["AWAITING_RESPONSE"]):
        await asyncio.sleep(0.05)
    await asyncio.sleep(args.barge_after)
    # Barge-in
    (HERE / "next_question.txt").write_text(q2)
    log(f"USER: hey athena (barge-in, {args.barge_after:.0f} s into Thinking…)")
    echo.mic.say("q2")  # spoken straight after the wake word
    log(f"USER: {q2}")
    await echo.on_wake_word()
    # wait for the outcome
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        p = echo.pipeline
        if p and p.number >= 2 and p.ended and p.mode == "WAKE_WORD_DETECTED":
            break
        if p is None and echo.count >= 1 and not echo.arbitrating and time.monotonic() - T0 > 5 and not echo.gate.suppressing \
                and all(x.ended for x in echo.pipelines) and EVENTS[-1][1].startswith("satellite: NEW TURN GIVEN UP"):
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(3)
    second = [p for p in echo.pipelines if p.number >= 2 and p.mode == "WAKE_WORD_DETECTED"]
    answered = any(p.heard == q2 and any(q2.rstrip("?") in s for s in p.spoken) for p in second)
    played_old = any(any(q1.rstrip("?") in s for s in p.spoken) for p in echo.pipelines if p.number >= 2)
    print("\nRESULT:", json.dumps({
        "mode": args.mode,
        "question_2_answered": answered,
        "question_2_transcribed": any(p.heard == q2 for p in second),
        "interrupted_answer_played_in_new_turn": played_old,
        "pipelines": [(p.number, p.mode, p.heard, p.spoken, STAGES[p.stage]) for p in echo.pipelines],
    }, indent=1), flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["before", "after"], default="before")
    ap.add_argument("--port", type=int, default=10700)
    ap.add_argument("--odysseus", default="http://127.0.0.1:7010/api/chat/voice/v1")
    ap.add_argument("--token-file", default=str(Path.home() / "sbx/voice_token.json"))
    ap.add_argument("--barge-after", type=float, default=3.0)
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--q1", default="What did Alice say about the budget?")
    ap.add_argument("--q2", default="Is the kitchen light on?")
    args = ap.parse_args()
    echo = Echo(args)
    server = AsyncServer.from_uri(f"tcp://127.0.0.1:{args.port}")
    asyncio.create_task(server.run(partial(Handler, echo)))
    asyncio.create_task(echo.mic.run())
    log(f"Echo emulator on :{args.port}, mode={args.mode}, device {DEVICE_ID}")
    await scenario(echo, args)


if __name__ == "__main__":
    asyncio.run(main())
