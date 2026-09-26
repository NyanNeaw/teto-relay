# Teto Relay roadmap

This roadmap comes from a full read of the code on the `productionize` branch,
plus a run of the existing test suite. Each finding says where it is, what goes
wrong, and what to do about it. The status column is updated as work lands:

- **done**: fixed on this branch, with tests where the environment allows
- **done (partly hardware)**: the testable part is done; the rest needs real
  Windows / audio / OpenUtau hardware
- **todo** / **proposed**: not done yet / suggested for later

"Untested on real hardware" means exactly that: the logic is unit tested with
mocks, but nobody has run it against a real microphone, VB-Cable, OpenUtau or
GPU. The manual checklist at the end covers those.

---

## How the audit was done

- Read every module in `teto_relay/`, `teto_relay/render/`, `tools/` and
  `tests/`.
- Ran the existing suite in a Linux container with no audio hardware, OpenUtau,
  .NET, voicebanks or GPU. Out of the box it could not even be imported:
  `capture.py` imported `sounddevice` at module level, which needs the
  PortAudio library. With that fixed and the optional packages installed, all
  153 existing tests pass.
- No web access was used for the singing analysis below. The notes on other
  open-source projects come from my own knowledge of them and should be
  double-checked before you commit to one.

---

## P0 — crashes, corruption, unusable, unsafe

| # | Finding | Where | Status |
|---|---|---|---|
| P0-1 | **Any website can drive the control panel.** The panel listens on `127.0.0.1:8765` and accepts cross-site POSTs with no origin, host or token check. A page you visit can rewrite any setting (including `openutau_dir`, `voicebank_root`, `out_dir`, `log_file`), start and stop the relay, and upload files. Uploaded `.pth` files are opened with `torch.load(weights_only=False)`, which unpickles them, so a malicious page can **run code on your PC**. DNS rebinding also works, because the `Host` header is never checked. | `webui.py` `do_POST`, `library.install_rvc_model` | done |
| P0-2 | **A fresh install can't sing in Japanese mode.** `requirements.txt` leaves out `cmudict` and `pykakasi`. Without `cmudict` every English word is "not in the dictionary" and is sung as silence; without `pykakasi` kanji is passed through unsung. Both fail with only a log warning. | `requirements.txt`, `japanese.py`, `translit.py` | done |
| P0-3 | **The committed `config.json` is one person's machine.** It sets `voicebank: "miku"` (no such bank → `ValueError` crash at startup), `ptt_key: "p"`, and absolute `D:\Claude\...` paths for output and logs. A new user's first start crashes. | `config.json`, `.gitignore` | done |
| P0-4 | **Importing the app needs PortAudio.** `sounddevice` was imported at module level in three modules, so the app (and the whole test suite) died on import with `OSError: PortAudio library not found`, a message that doesn't say what to do. | `capture.py`, `devices.py`, `playback.py` | done |
| P0-5 | **Config mistakes crash with a raw traceback.** Invalid JSON raises `JSONDecodeError`. A key from a newer or older version raises `ValueError` and bricks startup. Wrong types (e.g. `"transpose": "3"` from a hand edit, or from the panel) are never checked and crash deep inside the pipeline mid-utterance. | `config.py` `Config.load`, `webui.py` `api/config` | done |
| P0-6 | **A render can hang forever.** `task.Result` on WORLDLINE's render task has no timeout. If the engine stalls, the render thread blocks forever, every later utterance is dropped at the queue, and `stop()` can't join. The panel still says "running". | `render/openutau.py` `render` | done (partly hardware) |
| P0-7 | **Output cleanup can kill the render thread.** `_trim_output` runs in a `finally:` and calls `p.stat()` on each file. If a file disappears between the directory listing and the `stat` (antivirus, the user, the other worker thread), `FileNotFoundError` escapes the `finally` and the thread exits silently. From then on the relay never produces audio. | `app.py` `_trim_output` | done |

## P1 — major reliability, UX, architecture

| # | Finding | Where | Status |
|---|---|---|---|
| P1-1 | **Losing the microphone is silent.** If the input stream fails to open or dies (device unplugged, exclusive mode, driver reset), the capture thread logs one exception and exits. The relay keeps saying "running" and nothing happens when you talk. | `capture.py` `MicCapture.run` | done (partly hardware): reconnects with backoff and reports its status |
| P1-2 | **A failed start leaks a half-started relay.** If `TetoRelay.start()` fails partway (after the player, hotkey or mic started), nothing stops them. The mic stays open and threads keep running, and a second Start opens a second set. | `app.py`, `webui.Controller.start`, `tray.py` | done |
| P1-3 | **Tray mode fails invisibly.** Under `pythonw` there is no console, and a start failure only goes to the log file. The icon still shows red ("live"), and there is no way to open the log or the panel from the menu. | `tray.py` | done (partly hardware) |
| P1-4 | **Errors are tracebacks, not instructions.** The CLI lets exceptions escape for a missing voicebank folder, no banks found, an unknown bank key, a missing output device, OpenUtau not found, the port already in use, and so on. The rule is: say what happened, why, and what to do next. | `__main__.py`, `voicebank.py`, `devices.py`, `webui.serve` | done |
| P1-5 | **Paths assume a source checkout.** Config, `out/`, the log, `.cache/`, `.openutau-host/` and `pronunciations.json` are all resolved relative to the package folder. In a PyInstaller build that folder is a temporary extraction directory (for onefile) or a read-only install folder (Program Files), so settings would not persist or could not be written. | `config.py`, `__init__.py`, `dotnet.py`, `pronunciations.py`, `voicebank.py` | done: `paths.py`: portable vs installed data folders |
| P1-6 | **No per-stage latency numbers.** The log records analyse time and render time, but not queue waits, output-stream open, or the time from releasing the key to hearing sound, so it can't show where the ~2 s goes. | `app.py`, `playback.py` | done: one `Latency` line per utterance |
| P1-7 | **Rendered audio starts with silence.** `_mix` places each phrase at its absolute project position. The part starts at the first word's onset inside the chunk (pre-roll plus your reaction time after pressing the key, typically 0.2–0.5 s), so every WAV begins with that much silence, which plays straight into VB-Cable as extra latency. It also clamps a negative first offset to 0 without shifting the others, which can misalign the first phrase by its preutterance. | `render/openutau.py` `_mix` | done (partly hardware): trims to the first sound; untested against real WORLDLINE output |
| P1-8 | **Voicebank discovery walks the whole tree.** `rglob` over `voicebank_root` has no depth limit while walking (the depth check happens afterwards). The default root `D:\Claude` contains this repo, its venv and its model caches, so startup, the panel's `/api/config` and each bank-image request walk thousands of files. | `voicebank.py` `_find_singer_roots`, `discover` | done: depth-limited walk that skips venvs and caches |
| P1-9 | **No dependency or setup check.** A new user has no way to learn that .NET 8, VB-Cable, OpenUtau, a voicebank, CUDA, `cmudict` or `pykakasi` is missing, except by reading tracebacks. | new `doctor.py`, `--doctor` | done |
| P1-10 | **`webui.py` is over-coupled.** 750 lines of HTML, CSS and JS live in a Python string alongside the HTTP handler, the controller, the settings metadata and voicebank image handling. That makes it hard to change the page, impossible to lint the JS, and awkward to package. | `webui.py` | done: page moved to `teto_relay/web/index.html` |
| P1-11 | **`render/openutau.py` repeats work on every render.** Each render re-runs `Assembly.LoadFrom`, reflection lookups, and compiles a new `System.Linq.Expressions` setter. The UI callback queue is only drained 256 items at a time, so it can grow without bound. | `render/openutau.py`, `dotnet.py` | done (partly hardware): cached; untested on OpenUtau |
| P1-12 | **The process-wide `chdir` breaks relative paths.** `dotnet.start` calls `os.chdir(openutau_dir)`, so any relative path in the config silently resolves under the OpenUtau folder after the renderer starts. | `dotnet.py`, `config.py` | done: paths made absolute at load |
| P1-13 | **Control panel startup.** A port already in use shows a raw `OSError`. The browser isn't opened, so a double-clicked exe appears to do nothing. | `webui.serve` | done |

## P2 — polish

| # | Finding | Status |
|---|---|---|
| P2-1 | **Digits are sung as silence.** Whisper writes "2", "10", "5:30"; neither dictionary knows digits. Convert numbers to words before lyric processing. | done: `numbers.py` (English) |
| P2-2 | **Small kana (ぁぃぅぇぉゎ and katakana ァィゥェォ) become notes of their own** with no sample (ファ → ふ + ぁ). Expand them to the full-size vowel, which every bank has. | done |
| P2-3 | **One odd word disables alignment for the whole utterance.** A token outside the MMS_FA vocabulary (a digit, an accented letter, kana) makes the aligner throw, so every word falls back to whisper's timings. Now only the alignable words are aligned; the rest keep whisper's timings. | done |
| P2-4 | **Push-to-talk can lose a phrase.** The finished chunk is handed over through a single `_pending` slot. A quick release/press/release before the next audio frame overwrote it. | done |
| P2-5 | **Log spam from the audio callback.** It logs one warning per dropped frame, and logging from the audio callback itself adds jitter. Now counted and reported once. | done |
| P2-6 | **`pronunciations.json` is read and parsed twice per utterance.** Now cached until the file changes. | done |
| P2-7 | **The bank-pitch cache is keyed by bank key only**, so swapping the folder behind a key reused a stale pitch. Now keyed by folder path. | done |
| P2-8 | **Zip bombs.** A voicebank zip is size-checked when uploaded, but not when unpacked. | done |
| P2-9 | **`ptt_key` is inserted into the page with `innerHTML`.** | done |
| P2-10 | **Ticks can collide.** Note positions and durations are rounded to ticks separately, so a small `note_gap_ms` could round two notes into touching or overlapping, which collapses the phonemizer. Now enforced in the tick domain. | done |
| P2-11 | **No versioning.** Added `--version`, a version line in the log, and a version on the panel's status; `CHANGELOG.md`. | done |
| P2-12 | **A new output stream per utterance** (`sd.play`) adds device-open time to every phrase and uses sounddevice's global stream. | done (partly hardware): `persistent_output` flag, off by default |
| P2-13 | **`out/` trimming counts files, not utterances** (each utterance is a `.ustx` and a `.wav`). | done |

## P3 — nice to have

| # | Finding | Status |
|---|---|---|
| P3-1 | Real-time streaming voice conversion (see singing analysis, limitation 1). | done (partly hardware): streaming engine and crossfade, tested with a stand-in converter; the RVC hookup is behind `voice_streaming` and untested |
| P3-2 | Phoneme recognition instead of word ASR (limitation 2). | proposed |
| P3-3 | A DiffSinger render backend through OpenUtau (limitation 3). | proposed |
| P3-4 | Play the first rendered phrase while the rest render. | proposed |
| P3-5 | Auto-updating release notes / update check. | proposed |

---

## Singing-instrument analysis

I traced one push-to-talk utterance through the code, with timings from the
existing notes (`docs/NOTES.md`, measured on your PC):

```
key down ──► you speak (T seconds) ──► key up
                                         │  chunk queued
                                         ▼
  whisper base.en, CPU int8, beam 5 ...... ~0.8 s  (≈ constant up to ~3 s of speech)
  forced alignment (MMS_FA, GPU) ......... ~0.06 s
  pitch (crepe full, GPU) ................ ~0.1–0.3 s
  notes + .ustx .......................... ~0.01 s
                                         ▼
  OpenUtau: phonemize + WORLDLINE render . not measured before this branch
  leading silence in the WAV ............. 0.2–0.5 s  (P1-7)
  new output stream + playback ........... not measured before this branch
                                         ▼
                                  first sound ≈ T + 2 s after you start speaking
```

"Real-time instrument" means sound follows you while you are still
performing. This pipeline cannot start singing until you have *finished* the
phrase, and then needs about 2 s. For a performer the gap is T + 2 s, not 2 s.

The three biggest limitations, in order, and none of them is pitch detection:

### 1. The architecture is phrase-at-a-time (responsiveness)

Nothing can come out until the whole phrase has been recognised as text,
because the lyrics come from ASR. Whisper also has a near-constant cost per
call, because it pads every input to a 30-second window: 1 s and 3 s of speech
both cost about 0.8 s on the CPU. So streaming whisper in smaller pieces
doesn't help much. Each piece still pays about 0.8 s.

| Approach | Latency | Quality | Complexity | Fit | Windows/NVIDIA | UTAU fit | Risk |
|---|---|---|---|---|---|---|---|
| **A. Faster ASR settings**: whisper on CUDA float16, `beam_size` 1 | ~0.8 s → roughly 0.1–0.2 s (my estimate, not measured) | slight WER cost from greedy decoding | config only | exact | needs CUDA; the cuDNN load-order trap (see NOTES) applies | unchanged | whisper and torch share cuDNN; must keep the warm-up order |
| **B. Streaming ASR** (e.g. `whisper_streaming` LocalAgreement, sherpa-onnx streaming zipformer, Vosk) | words appear 0.3–1 s after they're spoken | whisper-streaming ≈ whisper; zipformer/Vosk noticeably worse on names | high: incremental notes, re-render, and patching audio that's already playing | poor: the note builder, alignment and octave correction all assume the whole phrase | fine | unchanged | sung output would start before the phrase's pitch shift is known |
| **C. Streaming voice conversion** (RVC real-time as in w-okada voice-changer / Applio; Seed-VC real-time) | 100–300 ms block + model time | natural delivery, Teto timbre, **but it's voice conversion, not UTAU singing** | medium: blocks + crossfade around the existing `VoiceConverter` | good: `mode: voice` already exists and loads RVC | needs a GPU fast enough to convert each block in less than its own length | does not use UTAU banks | per-block quality is worse than whole-phrase; needs SOLA crossfade |
| **D. Trim render/output overhead** (leading silence, persistent stream, play the first phrase while the rest render) | −0.2 to −0.6 s | none | low | exact | n/a | unchanged | low |

**Path:** do D now (P1-7, P2-12). Measure (P1-6) and then try A on your PC,
because only you can tell whether whisper on CUDA coexists with torch there.
Build C as the truly real-time mode. B isn't worth its complexity while the
lyrics depend on seeing the whole phrase.

### 2. Lyrics are re-derived from text, which loses information (intelligibility)

The pipeline recognises *words* and then re-synthesises them from a
dictionary's pronunciation. It never uses the sounds you actually made. Every
step can lose the word: whisper mishears (the NOTES blame most of the "works
half the time" on this); digits and unknown names are sung as silence; English
→ kana costs 2.22 morae per English syllable and drops sokuon and
ファ/ティ; and a mora shorter than the sample's preutterance is all consonant.

| Approach | Latency | Quality | Complexity | Fit | Windows/NVIDIA | UTAU fit | Risk |
|---|---|---|---|---|---|---|---|
| **A. Text-normalisation fixes** (numbers → words, small kana, per-word alignment fallback) | 0 | removes whole classes of silent words | low | exact | n/a | yes | none |
| **B. Bigger whisper on the GPU** (`small.en` on CUDA) | ~same as today's CPU `base.en` if CUDA works | WER 9.5% → 4.8% in the NOTES test | config only | exact | needs CUDA | yes | same cuDNN caveat |
| **C. Phoneme recognition** (wav2vec2 phoneme CTC such as `facebook/wav2vec2-xlsr-53-espeak-cv-ft`, or Allosaurus) → X-SAMPA hints / kana | ~0.1 s on a GPU | keeps what you *said*, including names and non-words; loses the dictionary's clean pronunciations | medium-high: IPA → X-SAMPA/kana mapping plus timing | good: `phonetic_hint` already carries X-SAMPA to the English bank | CUDA | English bank via hints; Japanese banks need IPA→kana | phone error rates on conversational speech are high; results may sound "mumbled" |

**Path:** A is done on this branch. B is a setting to try. C is the real fix
but needs models and listening tests, so it's proposed (P3-2), not built.

### 3. Every note is synthesised in isolation (naturalness)

Each word (or mora) is a separate note with a forced gap (`note_gap_ms`),
because touching notes collapsed the phonemizer in the hosted path. So there
is no legato and no coarticulation between words: each starts with an onset
(`- X`) and ends with a release (`X -`), which sounds like reading
word-by-word. Notes are flat (vibrato length 0), their pitch is speech F0 with
no sustain or musical shape, and they're rendered by a 2015 concatenative bank
that resamples each sample far from where it was recorded.

| Approach | Latency | Quality | Complexity | Fit | Windows/NVIDIA | UTAU fit | Risk |
|---|---|---|---|---|---|---|---|
| **A. Legato inside a word** (morae of one word touch; the gap stays only between words) | 0 | smoother, more sung | low at the note level | exact | n/a | yes | the "touching notes collapse" behaviour must be re-tested; I suspect it's a hosted-grouping issue, since touching notes are normal in OpenUtau itself |
| **B. Sung style**: snap to a scale, vibrato on long notes, hold the phrase's last note | 0 | clearly "sung" rather than spoken; loses speech intonation | low | exact (note builder + `.ustx`) | n/a | yes: vibrato is a standard OpenUtau note property | taste; vibrato must be applied to the hosted `UNote`, which is untested |
| **C. DiffSinger through OpenUtau** (OpenUtau's built-in DiffSinger renderer with a Teto DiffSinger voicebank, if a suitably licensed one exists) | + acoustic model + vocoder, a few hundred ms on a GPU (estimate) | much more natural phrasing | medium: a different singer type and phonemizer | good: same OpenUtau host | DirectML/CUDA ONNX | needs a DiffSinger bank, not UTAU CV | voicebank availability and licence; more hosting quirks |
| **D. NNSVS/ENUNU** | similar to C | good | high | poor | Windows ok | needs an ENUNU model | small ecosystem |

**Path:** build A and B behind `singing_style` / `legato` flags, **off by
default**, and listen on real hardware. Propose C (P3-3).

### What this branch implements for singing (all off by default)

| Flag | What it does | Verified here | Needs real hardware |
|---|---|---|---|
| `singing_style: "sung"` | snaps notes to `scale` (auto-detected key, or `scale_key`), adds vibrato to notes of at least `vibrato_min_seconds`, holds the last note for `final_hold_seconds`, and narrows the speech contour | unit tests on notes and `.ustx` | vibrato applied to OpenUtau's `UNote` |
| `legato: true` | morae/syllables of one word touch; the gap stays between words | unit tests on note spacing and ticks | the hosted phonemizer with touching notes |
| `voice_streaming: true` | `mode: voice` converts in blocks while you talk, with an overlap crossfade | unit tests with an identity converter (the output reconstructs the input at a fixed delay) | RVC speed per block on your GPU |
| `persistent_output: true` | one output stream stays open instead of one per phrase | unit tests with a mocked `sounddevice` | real WASAPI / VB-Cable |

---

## Release plan

- `packaging/teto_relay.spec`: PyInstaller onedir build, `TetoRelay.exe`
  (control panel) and `TetoRelayConsole.exe`.
- `packaging/installer.iss`: Inno Setup installer that wraps the onedir folder.
- `packaging/build.ps1`: builds the venv, runs the tests, then builds the exe,
  the installer and the portable zip, on Windows.
- A portable zip is the onedir folder plus a `portable.txt` marker, so
  settings stay next to the exe.
- The spec was built and run on Linux as a check: the frozen app starts,
  `--doctor` passes its dictionary checks, the panel works in a browser, and
  portable mode keeps data beside the exe. That check found cmudict's package
  metadata missing, which would have made Japanese mode silent. The Windows
  build, `build.ps1` and the installer have not been run. See the checklist in
  `docs/RELEASE.md`.

---

## Found while implementing

Not in the original audit; each is fixed and has a test.

- `Player._started` was silently shadowed by `threading.Thread`'s own
  `_started` event, so the latency report crashed playback. Found by the
  end-to-end pipeline test.
- The panel's dropdowns showed their first choice when the saved value wasn't
  in the list, and Save wrote it back. The default speech model `base.en`
  wasn't in the list, so any save changed it to `tiny`. Found by the browser
  smoke test.
- With no voicebank installed, the panel re-requested a missing picture every
  second. Found by the browser smoke test.
- Multi-word lyrics ("i am" from "I'm") were looked up in cmudict as one
  string in Japanese mode, missed, and romanised letter by letter.
- `http.server`'s default `SO_REUSEADDR` lets a second process silently share
  the port on Windows, and non-daemon handler threads made Ctrl+C wait for the
  browser's keep-alive connections.
- `stop()` raised on a partly built relay, hiding the real start error.
- The packaged build was missing cmudict's metadata (see above).

## Next steps

1. Build on Windows with `packaging/build.ps1` and go through the manual
   checklist in `docs/RELEASE.md`.
2. Collect a session of `latency.csv` rows and decide on ASR settings (whisper
   on CUDA, `beam_size` 1) from the numbers.
3. Listen to `singing_style: sung`, `legato` and `voice_streaming`, and make
   the good ones defaults.
4. Then P3-2 (phoneme recognition) or P3-3 (DiffSinger), depending on whether
   intelligibility or naturalness bothers you more.
