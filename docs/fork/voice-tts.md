# Voices: Piper and Kokoro

The voice assistant's spoken answers come from a text-to-speech (TTS) engine in Home Assistant
([voice-assistant.md](voice-assistant.md)). Two local engines are supported, both through the
Wyoming protocol, so nothing leaves the owner's network:

| | Piper | Kokoro-82M |
|---|---|---|
| Sound | clear but clearly synthetic | much closer to a person (rhythm, emphasis) |
| Speed on a desktop CPU (Ryzen 7 3700X class) | ~20–50× faster than real time | ~3–10× faster than real time |
| Delay before a short sentence starts | ~0.1 s | ~0.3–1 s |
| Memory | ~100 MB | ~0.5–1 GB RAM, no GPU needed |
| How it runs | Home Assistant add-on | a Docker container on the server |

The speed figures are typical values, not measured on the owner's server.

## What "Set up Home Assistant" does with voices

`fork/voice/ha_setup.py`, steps "Speech" and "Assist pipeline":

- **The "Odysseus" pipeline keeps the engine and voice it has.** A rerun changes them only when
  they are gone from Home Assistant: then it takes Piper and its first English voice.
- **Every other local TTS engine gets a pipeline of its own**: "Odysseus (Kokoro)" (or
  "Odysseus (Piper)" once Kokoro is the main voice). Same speech-to-text, same agent, same
  settings; only the voice differs. It starts with `af_heart` when the engine has it, else the
  engine's first English voice, and a rerun keeps a voice chosen by hand. It is never the
  preferred assistant.
- Setup never deletes a pipeline: remove one you no longer want by hand.

## Set up Kokoro (step by step)

On the server (Ubuntu, Docker), as the admin account. This adds a container next to the others;
it doesn't touch the Odysseus install.

1. **A folder for the model** (about 330 MB), and the model files, downloaded once:

   ```bash
   sudo mkdir -p /srv/kokoro
   ```

   ```bash
   sudo wget -O /srv/kokoro/kokoro-v1_0.pth https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/kokoro-v1_0.pth
   ```

   ```bash
   sudo wget -O /srv/kokoro/config.json https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/config.json
   ```

2. **Start Kokoro** ([wyoming-kokoro-torch](https://pypi.org/project/wyoming-kokoro-torch/),
   CPU image). Replace `192.168.1.10` with the server's home-network address. A Wyoming server
   has no password, so bind it to that address only and never forward the port. Port 10201 sits
   next to Piper's 10200 (10300 is usually Whisper's). It uses at most 4 of the 8 cores, so it
   doesn't slow the language model or Whisper down.

   ```bash
   sudo docker run -d --name wyoming-kokoro --restart unless-stopped \
     -p 192.168.1.10:10201:10201 -v /srv/kokoro:/data \
     --cpus 4 -e OMP_NUM_THREADS=4 \
     debackerl/wyoming-kokoro-torch:v3.2.0-cpu \
     /usr/local/bin/wyoming-kokoro-torch --voice af_heart --streaming \
       --uri tcp://0.0.0.0:10201 --data-dir /data
   ```

   The image has no entrypoint, so the program's path comes before its options. `--streaming`
   lets speech start on the first sentence. Each voice is downloaded into `/srv/kokoro` the first
   time it is used (a model-weight download; the text being spoken never leaves the server).

3. **Check it started:**

   ```bash
   sudo docker logs wyoming-kokoro 2>&1 | tail -5
   ```

   It ends with `Ready`.

4. **Add it to Home Assistant:** Settings → Devices & services → Add integration → **Wyoming
   Protocol** → host `192.168.1.10`, port `10201`. It shows up as "kokoro".

5. **Make the Kokoro assistant:** in Odysseus, Settings → Integrations → Voice assistant →
   **Set up Home Assistant** (run it again). The "Speech" step lists `also tts.kokoro`, and
   "Assist pipeline" ends with `to switch to: Odysseus (Kokoro): tts.kokoro (af_heart)`. Your
   "Odysseus" pipeline keeps its Piper voice.

## Switch between them

- **One Echo** (to compare two Echos side by side): Home Assistant → Settings → Devices &
  services → the Echo's device ("VACA <name>") → its **Assistant** select → "Odysseus (Kokoro)".
  Back: "preferred" or "Odysseus".
- **All Echos at once:** Settings → Voice assistants → "Odysseus (Kokoro)" → ⋮ → **Set as
  preferred**. Back: the same on "Odysseus". (A rerun of Set up Home Assistant makes "Odysseus"
  the preferred one again.)
- **Another Kokoro voice:** Settings → Voice assistants → "Odysseus (Kokoro)" → Text-to-speech →
  Voice. Good ones to try: `af_heart`, `af_bella`, `am_michael`, `am_fenrir`, `bf_emma`,
  `bm_george`. Setup keeps your choice.

## Keep Kokoro for good

Settings → Voice assistants → "Odysseus" → Text-to-speech → `kokoro` and a voice. Setup keeps it
from then on and adds "Odysseus (Piper)" to switch back to. "Odysseus (Kokoro)" is then the
same as "Odysseus": delete it if you like.

## Tuning and removal

- **Speed:** add `--speed 0.95` (slower) or `--speed 1.05` to the command in step 2, then remove
  the container (`sudo docker rm -f wyoming-kokoro`) and start it again.
- **Slower answers while Kokoro speaks** mean the CPU is shared with the language model: lower
  `--cpus` and `OMP_NUM_THREADS` to 2, or go back to Piper.
- **Remove:** `sudo docker rm -f wyoming-kokoro`, then delete the Kokoro entry under Wyoming
  Protocol in Home Assistant and the "Odysseus (Kokoro)" pipeline. `/srv/kokoro` holds only model
  files.

## Make Piper sound better

Piper add-on → Configuration, or the pipeline's Voice:

- a `-high` voice (`en_US-lessac-high`, `en_US-ryan-high`, `en_US-ljspeech-high`) or a good
  medium one (`en_US-amy-medium`, `en_GB-alba-medium`); samples at
  <https://rhasspy.github.io/piper-samples>;
- `length_scale` 1.05–1.1 (a little slower), `noise_scale` 0.7–0.8 (more varied tone),
  `noise_w` 0.85–0.95 (looser rhythm). Change one at a time: too much noise makes it warble.
