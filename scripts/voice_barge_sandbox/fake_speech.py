"""Wyoming speech-to-text (port 10300) and text-to-speech (port 10200) stand-ins.
STT answers with the text in $BARGE_WORK/next_question.txt (the Echo emulator writes it
before each question); TTS answers each text with 1.5 s of a quiet tone."""
import asyncio, math, os, struct, time
from functools import partial
from pathlib import Path
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

# where the spoken WAVs (wake.wav, q1.wav, q2.wav) and next_question.txt are
HERE = Path(os.environ.get("BARGE_WORK", Path.home() / "sbx/barge"))
ATTR = Attribution(name="sandbox", url="http://localhost")

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

class Stt(AsyncEventHandler):
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(Info(asr=[AsrProgram(name="fake-stt", description="fake", attribution=ATTR,
                installed=True, version="1", models=[AsrModel(name="fake", description="fake", attribution=ATTR,
                installed=True, languages=["en"], version="1")])]).event())
            return True
        if AudioStop.is_type(event.type):
            text = (HERE / "next_question.txt").read_text().strip()
            log(f"STT -> {text!r}")
            await self.write_event(Transcript(text=text).event())
            return False
        return True

class Tts(AsyncEventHandler):
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(Info(tts=[TtsProgram(name="fake-tts", description="fake", attribution=ATTR,
                installed=True, version="1", voices=[TtsVoice(name="fake", description="fake", attribution=ATTR,
                installed=True, languages=["en"], version="1")])]).event())
            return True
        if Synthesize.is_type(event.type):
            text = Synthesize.from_event(event).text
            log(f"TTS <- {text!r}")
            rate = 16000
            pcm = b"".join(struct.pack("<h", int(800 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * 1.5)))
            await self.write_event(AudioStart(rate=rate, width=2, channels=1).event())
            for i in range(0, len(pcm), 4096):
                await self.write_event(AudioChunk(rate=rate, width=2, channels=1, audio=pcm[i:i+4096]).event())
            await self.write_event(AudioStop().event())
            return False
        return True

async def main():
    s1 = AsyncServer.from_uri("tcp://127.0.0.1:10300")
    s2 = AsyncServer.from_uri("tcp://127.0.0.1:10200")
    await asyncio.gather(s1.run(partial(Stt)), s2.run(partial(Tts)))

asyncio.run(main())
