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

- **Windows 10 or 11 (64-bit).** The engine and the audio routing assume it.
- **The [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/8.0)
  (x64).** Teto Relay runs OpenUtau's singing engine through it.
- **[VB-Cable](https://vb-audio.com/Cable/)**, the virtual audio cable Teto
  sings into.
- **[OpenUtau](https://github.com/stakira/OpenUtau)**, installed. You don't
  need to open it; Teto Relay loads its engine directly.
- **A Kasane Teto UTAU voicebank.**
- **An NVIDIA GPU (optional).** It makes pitch detection much faster. Without
  one, set **Pitch tracker** to `pyin`.

## Install

### The easy way: installer or portable zip

Download `TetoRelay-<version>-setup.exe` or the portable zip from the
releases, and follow **[docs/SETUP.md](docs/SETUP.md)**. No Python needed.

### From the source code

1. Create a virtual environment and install the dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   For the GPU features (crepe pitch tracking and word alignment), install
   torch for your CUDA version and then `requirements-gpu.txt`. The file has
   the exact commands.

2. Check your setup. It lists anything missing (OpenUtau, .NET 8, VB-Cable,
   a voicebank, the GPU...) with the fix for each:

   ```bash
   .venv\Scripts\python.exe -m teto_relay --doctor
   ```

3. OpenUtau and your voicebanks are found automatically if they're in the
   usual places. If not, set them in the control panel under **Show all
   settings → Setup**:

   | Setting | What it is | Empty means |
   |---|---|---|
   | `openutau_dir` | The folder with `OpenUtau.exe` | search the usual install places |
   | `voicebank_root` | The folder with your voicebanks | the first of these that has a bank: `voicebanks` here, OpenUtau's `Singers`, `Documents\OpenUtau\Singers`, `Documents\UTAU\voice` (else `voicebanks` here) |

   The output device is **Output** on the main screen (default `CABLE Input`).

   Settings are saved to `config.json` in the project folder. It's yours and
   isn't tracked by git.

## Usage

The easiest way is the browser control panel:

```bash
.venv\Scripts\python.exe -m teto_relay --web
```

It opens <http://127.0.0.1:8765/> in your browser. From there you can change
settings, start and stop the relay, run **Check setup**, and watch the live log.
On Windows you can also double-click `run.bat`. **Quit** closes it.

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
| `--doctor` | Check the installation and settings, then exit |
| `--port 8766` / `--no-browser` | Panel port / don't open a browser |
| `--version` | Print the version |
| `-v` | Show more detail in the log |

`--config` works everywhere, including the control panel. The other setting
options (`--bank`, `--key`, `--model`, `--vad`, `--input`, `--output`,
`--backend`) apply to terminal and tray mode only: the panel always starts the
relay from what is saved in the config file.

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

- **Something doesn't work.** Run **Check setup** in the panel, or
  `--doctor`. Errors say what happened and what to do; the full details are
  in `teto-relay.log`.
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
  the defaults in the code, so check there first. The panel tells you when a
  setting needs Stop and Start to apply. Changing the OpenUtau folder needs
  Teto Relay itself restarted.
- **The first phrase is slow.** Models load on startup, and the first run
  downloads about 1.2 GB for word alignment.

## Measuring latency

Every phrase logs one line saying where the time went, from releasing the key
to hearing Teto:

```
Latency 1.66s release->sound | speech 1.84s | wait_analyse 0.00s | asr 0.81s | align 0.06s | pitch 0.12s | notes 0.00s | ustx 0.00s | wait_render 0.00s | render 0.62s | phonemize 0.10s | synth 0.52s | wait_output 0.00s | output 0.05s | lead_silence 0.00s
```

The same numbers go to `latency.csv` (next to the log), one row per phrase, so
you can compare settings in a spreadsheet. `speech` is how long you talked and
isn't part of the delay. The biggest lever is usually `asr`: try **Listen on**
`cuda` with **Listening precision** `float16` and **Search width** 1.

## Experimental options

These are off by default because they haven't been tried on real hardware
yet. They're under **Show all settings** in the panel.

- **Singing style: sung** puts what you say into a key, holds notes steadier,
  adds vibrato to long notes and holds the last one. Less speech, more song.
- **Connect syllables** (`legato`) sings each word's syllables joined up on
  Japanese banks.
- **Convert while I talk** (`voice_streaming`, Voice engine only) converts your
  voice to Teto's in real time, in short blocks, instead of after each phrase.
- **`persistent_output`** keeps one audio stream open instead of opening one
  per phrase.

[ROADMAP.md](ROADMAP.md) explains why these exist and what's planned next.

## Running the tests

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe tools\test_pitch.py --selftest
.venv\Scripts\python.exe tools\render_once.py --backend null --play
```

The unit tests need no audio hardware, OpenUtau or GPU; they fake those.
`tools/panel_smoke.mjs` drives the control panel in a headless browser (needs
Node and Playwright). To build the Windows app, installer and portable zip, see
[docs/RELEASE.md](docs/RELEASE.md), which also has the checklist for testing
on real hardware.

## Project layout

```
teto_relay/          the app: capture, speech-to-text, pitch, notes, rendering, playback
teto_relay/render/   the OpenUtau engine host and the tone-only fallback
teto_relay/web/      the control panel page
tools/               small scripts for testing and debugging single stages
tests/               unit tests
packaging/           PyInstaller spec, installer script and Windows build script
docs/                setup guide, release guide and design notes
pronunciations.json  fixes for words Teto says wrong
```

## Want the details?

- [docs/SETUP.md](docs/SETUP.md): setting up the installed or portable app.
- [ROADMAP.md](ROADMAP.md): known issues by priority, and the analysis of what
  limits how natural and responsive it sounds.
- [docs/NOTES.md](docs/NOTES.md): the design notes. Why each choice was made,
  benchmarks, and the tricks needed to run OpenUtau's engine without its app.
- [CHANGELOG.md](CHANGELOG.md): what changed in each version.
