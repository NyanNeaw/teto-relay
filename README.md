# Teto Relay

Talk into your mic and Kasane Teto sings it back.

Teto Relay listens to what you say, works out the words and the pitch of your
voice, and re-sings it through a Kasane Teto UTAU voicebank. The result comes
out of a virtual audio cable, so you can use it as your "mic" in Discord, OBS,
or anything else that takes audio input.

It all runs on one PC, in the background. You don't need to open OpenUtau or
click through anything while it's running.

## Heads up: it's a fun toy, not a polished product

It works roughly half the time. Some phrases come out clear and sound just like
Teto; others come back garbled, misheard, or with a word missing. It's also not
instant: expect about **2 seconds** of delay between you speaking and Teto
singing.

Most of the misses come from the speech recognition mishearing you, or from an
old sample-based voicebank being asked to sing everyday speech. If a phrase
sounds wrong, the log usually says why. Often you just need to say it again.

## How it works

1. **Record.** Hold a key, talk, let go.
2. **Transcribe.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   turns your speech into words, with timing for each one.
3. **Find the pitch.** It measures your actual voice pitch, word by word, and
   moves it into Teto's range.
4. **Build a song.** The words and pitches become an OpenUtau project (`.ustx`).
5. **Sing.** OpenUtau's engine renders the audio in the background, with no
   window.
6. **Play.** The audio goes out through VB-Cable.

## What you need

- **Windows.** Paths, audio devices and the OpenUtau setup all assume it.
- **Python 3** with a virtual environment.
- **[VB-Cable](https://vb-audio.com/Cable/)**, the virtual audio cable Teto
  sings into.
- **[OpenUtau](https://github.com/stakira/OpenUtau)**, installed. You don't
  need to open it; Teto Relay loads its engine directly.
- **A Kasane Teto UTAU voicebank.**
- **An NVIDIA GPU (optional).** It makes pitch detection much faster. Without
  one, set `pitch_method` to `"pyin"` in `config.json`.

## Setup

1. Create a virtual environment and install the dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   To use the GPU features (crepe pitch tracking and word alignment), also
   install `torch`, `torchaudio` and `torchcrepe` with CUDA support.

2. Point it at your files. Edit `config.json` (or the defaults in
   `teto_relay/config.py`):

   | Setting | What it is | Example |
   |---|---|---|
   | `voicebank_root` | Folder that contains your voicebanks | `D:\Claude` |
   | `openutau_dir` | Where OpenUtau is installed | `D:\Work\OpenUtau` |
   | `output_device` | Where the audio goes | `CABLE Input` |

3. Check that it can see everything:

   ```bash
   .venv\Scripts\python.exe -m teto_relay --list-banks
   .venv\Scripts\python.exe -m teto_relay --list-devices
   ```

## Usage

The easiest way is the browser control panel:

```bash
.venv\Scripts\python.exe -m teto_relay --web
```

Then open <http://127.0.0.1:8765/>. From there you can change settings,
start and stop the relay, and watch the live log. On Windows you can also
double-click `run.bat`.

Once it's running, **hold the push-to-talk key, speak, and let go.** The key is
F8 by default; `config.json` can change it with `ptt_key`. Teto sings the
phrase a couple of seconds later.

To hear it in Discord (or anywhere else), pick **CABLE Output** as your
microphone in that app.

### Other ways to run it

```bash
# In the terminal, no browser
.venv\Scripts\python.exe -m teto_relay

# In the system tray, no console window
.venv\Scripts\pythonw.exe -m teto_relay --tray
```

### Handy options

| Option | What it does |
|---|---|
| `--key ctrl_r` | Use a different push-to-talk key |
| `--vad` | Stop recording when you go quiet, instead of using a key |
| `--bank tandoku` | Pick which voicebank to use |
| `--model small.en` | Use a more accurate (but slower) speech model |
| `--backend null` | Play plain tones instead of singing, to test the audio path |
| `--input` / `--output` | Choose mic / output device by name |
| `--list-banks` | Show the voicebanks it found |
| `--list-devices` | Show your audio devices |
| `--config path` | Use a different config file |
| `-v` | Show more detail in the log |

## Voicebanks

Teto Relay searches `voicebank_root` and picks up these Teto banks:

| Key | Type | Notes |
|---|---|---|
| `english` | English (CVVC) | Sings the words as spoken |
| `tandoku` | Japanese (CV) | **Recommended.** Has every sound it needs |
| `renzokubeta` | Japanese (VCV) | Missing some sounds (きゃ, しゃ, ちゃ…), so words drop out |

With a Japanese bank, your English is converted to Japanese-style pronunciation
first. For example, "i love you" becomes あい らぶ ゆう. The `lyric_mode`
setting controls this:

- `auto` (default): convert only when the voicebank is Japanese
- `native`: never convert
- `japanese`: always convert

## Tips and troubleshooting

- **Teto mishears a word.** Try `--model small.en`. It's more accurate, but
  slower.
- **A name or unusual word comes out silent or wrong.** Add it to
  `pronunciations.json`:

  ```json
  {
    "phonemes":    { "teto": "t E t oU" },
    "respellings": { "teto": "teh toe" }
  }
  ```

  `respellings` are the easy option: spell the word out with other English
  words. `phonemes` are more exact. See [docs/NOTES.md](docs/NOTES.md) for the
  symbols you can use.
- **Nothing reaches Discord.** Record *CABLE Output* in OBS or Audacity. If it
  shows up there, the relay is working and the problem is on the Discord side.
- **Some setting doesn't seem to change anything.** `config.json` overrides
  the defaults in the code, so check there first.
- **The first phrase is slow.** Models load on startup, and the first run
  downloads about 1.2 GB for word alignment.

## Running the tests

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe tools\test_pitch.py --selftest
.venv\Scripts\python.exe tools\render_once.py --backend null --play
```

## Project layout

```
teto_relay/       the app: capture, speech-to-text, pitch, notes, rendering, playback
teto_relay/render the OpenUtau engine host and the tone-only fallback
tools/            small scripts for testing and debugging single stages
tests/            unit tests
config.json       your settings (overrides the defaults in config.py)
pronunciations.json  fixes for words Teto says wrong
```

## Want the details?

- **[HANDOFF.md](HANDOFF.md)** covers the current status, recommended
  settings, known issues and next steps.
- **[docs/NOTES.md](docs/NOTES.md)** covers the design notes: why each choice
  was made, benchmarks, and the tricks needed to run OpenUtau's engine without
  its app.
