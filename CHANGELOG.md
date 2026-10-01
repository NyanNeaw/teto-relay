# Changelog

Teto Relay uses [semantic versioning](https://semver.org/): the minor number
goes up for new features, the patch number for fixes.

## 0.3.0 — tested on real hardware, retuned

The branch was run on the target PC (Windows, GTX 1060, OpenUtau, VB-Cable)
and retuned on the user's own recordings. How it sounds has changed a lot:
see "Hardware testing and tuning log" in docs/NOTES.md for the measurements.

### Upgrading from 0.2.0

A `config.json` from an older version lists every setting, so new defaults
don't reach it. To get the new tuning, set these in the panel (or delete them
from the file): **Connect words** (`legato`) on, **Measure word timing**
(`use_alignment`) off. On an NVIDIA GPU, **Listen on: cuda** with
**Speech model: small** is the most accurate and still fast.

### Removed
- **The Voice engine (RVC).** Teto Relay is Teto singing what you say; RVC
  made her a filter on your own voice instead, needed the 4 GB CUDA build to
  keep up, and doubled the settings. Old settings files load as before (the
  RVC settings are dropped quietly). The code is at the git tag
  `before-rvc-removal`.

### Fixed
- The packaged app: a double-click opened no window (Edge inherited the
  bundle's DLL path), and TetoRelay.exe with options ran invisibly.
- Much smaller: installer 1.6 GB -> 511 MB, portable zip 2.6 GB -> 856 MB,
  installed 4.2 GB -> 1.6 GB. Whisper keeps the GPU; pitch tracking and
  alignment run on the CPU, where they measured as fast and as accurate.
- Without CUDA, crepe ran its full model on the CPU (4.8 s a phrase); it
  uses the tiny one there (0.28 s).
- Pitch curves were written in cents but OpenUtau reads tenths of a semitone:
  every inflection was ten times too big.
- Pitch was sung 0.2–0.5 s late against the syllables on every phrase.
- Touching notes lost their syllables; a rounding gap cut connected words
  ("to sing" → "to… sing").
- ARPAsing voicebanks (e.g. a Miku English bank) sang nothing, and a bank's
  recorded pitch could be an octave off.
- `float16` on GTX 10xx GPUs made every phrase fail; now falls back to `int8`.
- A repetitive phrase could take 17 s to transcribe; an accented phrase could
  be thrown away as "not speech".
- Japanese: long-vowel marks and small kana sang as silence; 明日, 君, 今日は
  and katakana okurigana (紛レ) were misread.
- Voice mode: `harvest` reused the first phrase's pitch; streaming dropped the
  end of a phrase when the GPU lagged.
- The word aligner never loaded on 8 GB PCs; it now needs ~0.4 GB of RAM.
- Switching voicebank while running changed the lyrics but not the voice.
- Whisper's invented words are dropped: words timed after you stopped
  speaking, over silence, and a phrase repeated until the token budget ran
  out ("... สวัสดี ครับ สวัสดี ครับ สวัสดี ครั"), and a much less confident
  second segment tacked on after the speech (a video intro: "สวัสดี ครับ
  คลิป นี้ เป็น รายการ ..."). The Thai model is also run with a repetition
  penalty, which stopped its loops on every test phrase.
- Other people's voicebanks: romaji-alias banks sang silence; banks with no
  character.txt played tones; Japanese zips installed with garbled file
  names; a multi-pitch bank inside an author folder installed one pitch;
  Japanese CVVC banks were sung as VCV.

### Changed
- Timing keeps your rhythm: syllables start where you said them, pauses stay
  silent, phrases are sung connected, held notes last as long as you hold
  them, Japanese syllables are timed from the aligner.
- Loudness, breath and phrase endings follow your own delivery.
- Sung style: scoops, glides, overshoot, falls and delayed vibrato.
- Doubled voice on sung phrases: an On/Off switch in the panel.
- Long vowels are held instead of re-attacked.
- Default speech model: multilingual `base` (the `.en` models misheard
  accented English badly).
- The panel: plain look, most-used settings first, switches for singing
  style / doubling / language, every change applies at once - settings the
  relay reads only at start restart it by themselves. Springy motion
  (sliding switches, cards and words that pop in); off with the system's
  reduce-motion setting, or with the sparkle button beside the theme button.
  The voicebank picture stays still.

### Added
- **Thai.** Speak Thai and Teto sings it, on a Japanese or an English bank:
  heard with Thonburian Whisper (a Whisper trained on Thai), cut into real
  words, and pronounced by sound with pythainlp's Thai G2P - long vowels held,
  unreleased stops as short rests, ท as t. Before, Thai was sung from
  whisper's character fragments through a spelling that read ท as English
  "th", and an ARPAsing bank sang it as silence.
- **Rename a voicebank** from the panel (the pencil beside its name). The
  name is kept in Teto Relay's data folder; the bank's files are not touched,
  and an empty name goes back to the bank's own.
- **App window**: the panel opens in Teto Relay's own window (WebView2, the
  engine Edge uses), so its taskbar button is Teto Relay's and pins as Teto
  Relay; closing it closes the program. *Open the panel as* in Setup switches
  to a browser tab.
- **Song lyrics** on the main screen: paste the lines you'll sing and they're
  heard correctly.
- `keep_input_audio` (Keep what I said), `tools/tuning_eval.py`, and
  `tests/test_hardware.py` (renders through OpenUtau when
  `TETO_RELAY_HARDWARE_TESTS=1`).

## 0.2.0 — productionize

Nothing about how the default pipeline sounds has changed. Everything below
is either a fix or new, and the new singing options are off by default.

### Upgrading from 0.1.0 (a source checkout)

`config.json` is no longer tracked by git. `git pull` deletes an unmodified
tracked copy, so **copy `config.json` somewhere safe before pulling**, and put
it back afterwards; it is ignored from now on. Two settings no longer default
to one machine's folders: leave `openutau_dir` and `voicebank_root` empty to
have them found automatically, or set them in the panel's Setup group.

### Security
- The control panel refuses requests from other websites (Host check plus a
  required `X-Teto-Relay` header), so a web page can no longer change settings,
  start the relay or upload files.
- Uploaded voice models are opened without unpickling arbitrary objects.

### Fixed
- A fresh install from `requirements.txt` could not sing in Japanese mode
  (`cmudict` and `pykakasi` were missing).
- The app no longer needs PortAudio just to import.
- Bad settings are explained (all at once, with the fix) instead of crashing.
- A stalled render gives up after 30 s instead of hanging the relay forever.
- A file vanishing from `out/` no longer kills the render thread.
- The microphone is reopened when it fails or goes quiet.
- A failed start stops whatever it had started.
- Rendered audio is trimmed to start at the first sound, which should remove
  0.2–0.5 s of silence (not yet confirmed with real OpenUtau output).
- Numbers are sung instead of being silent; small kana and multi-word lyrics
  in Japanese mode no longer produce silent or garbled notes.
- One accented or non-Latin word no longer disables word alignment for the
  whole phrase.
- Quick push-to-talk presses no longer lose a phrase.
- The panel no longer overwrites the speech model setting when saving.
- The panel uses the `--config` file it was started with, and a relative
  `--config` path keeps working after the relay starts.
- Installing an RVC model no longer stops later setting changes from reaching
  the running relay.
- A portable folder keeps working after being moved.
- Very long numbers (phone numbers, IDs) no longer crash transcription.
- Note gaps survive rounding to ticks.

### Added
- `--doctor` and the panel's Check setup: what is missing and how to fix it.
- Per-stage latency in the log and `latency.csv`.
- Settings live in a data folder (`%LOCALAPPDATA%\TetoRelay` when installed, a
  `data` folder when portable, the project folder from source).
- Tray: error state, Retry, Open log.
- Packaging: PyInstaller spec, Inno Setup installer script, portable zip and a
  Windows build script.
- `--version`.
