# Teto Relay — design notes

The long version. This is the engineering record of *why* each part of Teto
Relay works the way it does: measurements, dead ends, and the OpenUtau hosting
quirks that cost real debugging time. For setup and everyday use, see the
[README](../README.md).

## How well does it actually work?

**Honestly: it works about half the time.** Some utterances come out clear and
recognisably Teto; others come back garbled, mistranscribed, or with a word
sung as silence. It is good enough to be fun and usable, not good enough to
rely on.

The pipeline itself is solid — every stage runs, and the failures are mostly
upstream (whisper mishearing you) or inherent to a 2015 sample-based voicebank
being asked to sing conversational speech. If an utterance sounds wrong, the
log usually names the reason: a low-confidence transcription, a word missing
from the dictionary, or a phoneme with no sample.

Expect to repeat yourself sometimes. That is the current state, not a bug to be
reported.

## Status

| Stage | State |
|---|---|
| 1. Mic capture + pause chunking | Working |
| 2. Speech to text (word timestamps) | Working |
| 3. Pitch detection (real F0) | Working, verified to 0.01 semitones |
| 4. `.ustx` generation | Working |
| 5. Headless render | **Working** — real WORLDLINE synthesis, no GUI |
| 6. VB-Cable playback | Working |

All six stages work. `NullRenderer` remains as a tone-only fallback and is
selected automatically if the synthesis engine fails to start.

## Setup

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Paths are configured in `teto_relay/config.py` (override with `config.json`):

- `voicebank_root` — `D:\Claude`, scanned for voicebanks
- `openutau_dir` — `D:\Work\OpenUtau`
- `output_device` — `CABLE Input`

## Running

Control panel in the browser — settings, start/stop, and a live activity log:

```bash
.venv\Scripts\python.exe -m teto_relay --web
```

Then open <http://127.0.0.1:8765/>. It uses only the standard library, so it
adds nothing to disk, and the form is generated from the `Config` dataclass —
any setting added later shows up without editing the page.

Or run it headless:

```bash
.venv\Scripts\python.exe -m teto_relay
```

**Hold F8 and speak; release to process.** Push-to-talk is the default because
silence-splitting cuts mid-sentence and hands whisper short noisy fragments,
which it transcribes as confident nonsense rather than nothing.

Background, no console window:

```bash
.venv\Scripts\pythonw.exe -m teto_relay --tray
```

Useful flags: `--key ctrl_r`, `--vad` (back to silence detection),
`--model small.en`, `--bank renzokubeta`, `--backend openutau`,
`--list-banks`, `--list-devices`, `-v`.

## Transcription accuracy

Measured against Windows SAPI speech with known ground truth:

| Model | Mean WER | Speed | Load |
|---|---|---|---|
| `base.en` | 9.5% | 0.77× realtime | 2 s |
| `small.en` | 4.8% | 1.05× realtime | 20 s |

Both models transcribed ordinary sentences perfectly. Every error in the test
set was the single word *teto*, which is out-of-vocabulary — `small.en` got it
wrong too, for roughly 3× the latency. Model size is the wrong lever for an
unknown proper noun.

`initial_prompt` in `config.json` is the right one: priming with
"Kasane Teto, UTAU, vocaloid, voicebank." fixes *teto* on `base.en`. It can
occasionally append a stray word, so set it to `""` if you prefer the
trade the other way.

If accuracy is still poor on real speech, reach for `--model small.en` — but
expect roughly 3 s of transcription for a 3 s phrase.

## Voicebanks

Discovery walks `voicebank_root` for `character.txt` / `oto.ini`, so all three
banks are found despite having different layouts — renzokubeta keeps `oto.ini`
at its root while the others nest under `重音テト音声ライブラリー`.

| Key | Flavour | Entries | Phonemizer |
|---|---|---|---|
| `english` | en-cvvc | 2681 | `EnXSampaPhonemizer` |
| `renzokubeta` | ja-vcv | 385 | `JapaneseVCVPhonemizer` |
| `tandoku` | ja-cv | 358 | `DefaultPhonemizer` |

Switch at runtime from the tray menu, or per run with `--bank`.

## Verification

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v   # 153 tests
.venv\Scripts\python.exe tools\test_pitch.py --selftest     # known tones
.venv\Scripts\python.exe tools\list_banks.py
.venv\Scripts\python.exe tools\render_once.py --backend null --play
```

To confirm routing, record CABLE Output in OBS or Audacity — if it records,
Discord will see it.

## Notes on stage 5

OpenUtau has no CLI, and [the request for one was closed as not
planned](https://github.com/stakira/OpenUtau/issues/1615). WORLDLINE-R is not a
standalone `.exe`; it is a native library driven from `OpenUtau.Core`. We host
that assembly in-process with pythonnet.

Two things cost real debugging time and are worth knowing before touching
`render/openutau.py` or `dotnet.py`:

- **CoreCLR startup.** OpenUtau's own `runtimeconfig.json` is self-contained,
  and `clr_loader` refuses it (`InvalidConfigFile`). We generate a
  framework-dependent config instead.
- **`Ustx.Load` is unusable.** OpenUtau ships a .NET 9 build of
  `System.IO.Packaging` alongside a self-contained .NET 8.0.19 runtime, so
  loading a project file throws `FileLoadException`. The mismatch is latent in
  the shipped app and only surfaces when hosting Core out-of-process. We parse
  our own YAML and build `UProject` through the object model instead.

- **Legacy codepages must be registered.** .NET Core ships only Unicode and
  Latin-1, so every Shift-JIS `character.txt` fails with `'shift_jis' is not a
  supported encoding name`. This is also why `VoicebankLoader.SearchAll()`
  quietly returned zero banks rather than raising.
- **Singer loading bypasses discovery.** Both of OpenUtau's routes fail from a
  hosted process: `SearchAll()` finds nothing, and `SingerManager` needs
  `PathManager` preferences a never-run install lacks. We build the `Voicebank`
  object ourselves and call the static `VoicebankLoader.LoadVoicebank(...)`.
  Verified: 2681 oto entries load from the English bank.
- **`track.RendererSettings` is not optional.** Without it,
  `RenderPhrase.FromPart` silently returns an empty list.
- **`DocManager` must be initialised** and its `PostOnUIThread` must be a
  *native* delegate. A Python lambda gets called from `PhonemizerRunner`'s
  background thread, where pythonnet cannot marshal the argument and dies with
  "Failed to create Python type for System.Action"; invoking the callback
  inline instead re-enters `ExecuteCmd` and overflows the stack.

- **`PostOnUIThread` is a native queue.** `ConcurrentQueue<Action>.Enqueue` has
  exactly the `Action<Action>` signature, so the delegate binds straight to a
  queue instance — no Python runs on OpenUtau's threads — and `drain_ui()`
  replays the callbacks on ours. Verified: 398 callbacks, no crash.
- **`PhonemizerRunner`'s background loop does not work when hosted.** It
  accepts requests and emits notifications but never sets
  `part.phonemizerResponse`, and `WaitFinish()` *deadlocks* — it waits on
  callbacks only we can pump. Don't call it. The static
  `PhonemizerRunner.Phonemize(request)` runs inline and does work.
- **pythonnet cannot hand reflection a boxed primitive.** `FieldInfo.SetValue`
  rejects `PyInt`, and `Convert.ToInt64`, `Convert.ChangeType` and
  `Array.GetValue` all round-trip back to a Python int. Compile a typed setter
  with `System.Linq.Expressions` instead (`_int64_field_setter`), and build
  `Phonemizer.Note` natively rather than through reflection.

- **The keystone: `Assembly.GetEntryAssembly()` is null when hosted.**
  `clr_loader` starts CoreCLR with no managed entry point, and
  `PathManager..ctor()` dereferences the result. Because PathManager is a
  `Lazy<T>` singleton, that single `NullReferenceException` caused *every*
  earlier symptom — `SingerManager` throwing, `VoicebankLoader.SearchAll()`
  returning zero banks, `DictionariesPath` throwing, and the phonemizer's
  dictionary init faulting — each surfacing many layers from the real problem.
  .NET 8 has an internal `Assembly.SetEntryAssembly`; call it by reflection
  with `OpenUtau.dll` (`dotnet._set_entry_assembly`).
- **The dictionary loads asynchronously.** `SetSinger` returns before it is
  ready (~1.3 s here). Phonemize too early and every phoneme is an empty
  string, with no error at all.
- **Apply the response with `SkipPhonemizer=True`.** A plain `Validate` re-runs
  the phonemizer, bumps the part's timestamp, and discards the response you
  just handed it.
- **Map otos yourself.** `Validate` maps only the *first* phoneme of the part.
  `UPhoneme.ValidateOto` and friends are internal — invoke by reflection.

- **Redirect PathManager's data paths.** It builds `DataPath` from
  `Environment.ProcessPath` — python.exe — so `Cache/` and `Dictionaries/`
  would be created inside the Python installation. `AppContext.BaseDirectory`
  is *not* consulted, so override the string backing fields instead. Ours point
  at `.openutau-host/`.
- **Initialise `ToolsManager`.** It registers the `worldline` resampler;
  `ResamplerItem` indexes the registry directly and throws
  `KeyNotFoundException` without it.
- **A bare `UProject` has zero expressions.** `RenderPhone` reads expressions
  per phoneme, so call `Ustx.AddDefaultExpressions(project)` plus the
  renderer's own `GetSuggestedExpressions`. This is the part of the `.ustx`
  `expressions:` block we chose not to hand-write — omitting it is fine on
  disk, but the in-memory project still needs it.

Verified: `hello there teto` → `- hV, V l, loU, oU -, - DE, E r-, - ti, i t,
toU, oU -`, all 10 phonemes mapped, rendered to 1.45 s of audio (peak 0.70,
96.6% non-silent). All three voicebanks render.

## Japanese pronunciation mode

`lyric_mode` turns English into Japanese-style pronunciation and sings it
through a Japanese bank — "i love you" becomes あい らぶ ゆう.

| Setting | Behaviour |
|---|---|
| `auto` (default) | follow the voicebank: a Japanese bank implies conversion |
| `native` | sing the words as they are |
| `japanese` | always convert |

**Use `tandoku`.** Measured coverage of the standard 104 morae:

| Bank | Coverage |
|---|---|
| **tandoku** | **104/104** |
| renzokubeta | 69/104 — missing every youon (きゃ しゃ ちゃ) |

Note this is *not* because the English bank is sparse — it covers 407 of 408 CV
combinations. English needs coda consonants and clusters, and CVVC has to join
them; Japanese is nearly all clean CV morae, which concatenate far more easily.
That, rather than missing samples, is the reason to try this mode.

Two structural points, both learned the hard way:

- **One mora per note.** A Japanese bank sings あ and い as separate notes;
  handing the phonemizer `あい` as one lyric produces a single unknown phoneme
  and renders nothing. Each word therefore expands into several notes sharing
  its spoken time.
- **Kana is counted in morae**, not Latin vowel groups, or every Japanese word
  would count as one syllable and its note would be sized far too short.

The transliteration lives in `teto_relay/japanese.py` and targets only morae
the bank actually has, so ファ/ティ/ヴ fall back the way Japanese itself borrows
them: F→ハ行, V→バ行, TH→サ行, L→ラ行. Words that are already Japanese
(`teto`, `kasane`, `miku`) use their real spelling rather than a transliterated
English reading.

## Intelligibility

Three things decide whether the output is understandable, and all three bit us:

- *(Superseded: the collapse below was the relay sending touching notes to the
  phonemizer as one group - see "Hardware testing and tuning log". Notes now
  touch within a phrase.)*
  **Notes must never touch, but the gap must be tiny.** Two notes that meet are
  treated as one legato phrase and the phoneme sequence collapses — both banks
  do it. Only *literal zero* breaks it, though: one tick is enough.

  | Gap | English phonemes | Japanese morae |
  |---|---|---|
  | 0 ms | 4 of 15 | 2 of 6 |
  | 1 ms+ | 15 of 15 | 6 of 6 |

  `note_gap_ms` is 2 ms — one tick above the proven minimum. It was originally
  30 ms, picked off a coarse 0/10/30/50/100 grid without testing below 10.
  Inaudible between words; clearly audible between *morae*, where notes are
  ~0.2 s and every syllable got a gap. Dropping it to 2 ms took a Japanese
  phrase from 3.4% silence to 0.8%.

- **Notes need a minimum length.** A CVVC sample carries a consonant, a vowel
  and the transition out of it, and everything after the consonant is stretched
  or looped to fill the note. Whisper's word spans are often much shorter than
  the sample wants, and cramming it in makes short words mumble.
  `min_note_seconds` (0.45 s, chosen by ear from a 0.20–0.90 s comparison)
  extends them, shifting later words along — so the sung line runs a little
  longer than the speech that produced it. That trade is deliberate: length
  wins over exact timing.

- **Aim at the voicebank's recorded pitch, not at a permitted range.** A UTAU
  sample is recorded at one pitch and resampled to whatever is asked for, and
  the further from the original, the thinner it sounds. Measured from the
  samples themselves:

  | Bank | Recorded at |
  |---|---|
  | english | C#4 (MIDI 61.1) |
  | renzokubeta | D4 (62.1) |
  | tandoku | C#4 – D#4 |

  Rendering the English bank at A3 sounds breathy, at A4 strained. The shift is
  measured once per bank (cached) and aims at that pitch. Shifts are in
  semitones, not octaves — every interval is preserved and only the key moves,
  which does not matter for speech. Set `shift_mode: "octave"` to preserve
  pitch class instead.

  **The shift must then stay put.** Re-normalising every utterance onto the
  target throws away your pitch differences *between* sentences and makes the
  voice lurch — two similar phrases came out 4 semitones apart. It is now held
  until the voice drifts more than `shift_tolerance` (6 semitones) from the
  target, so speaking lower actually sings lower:

  | Spoken | Sung | Shift |
  |---|---|---|
  | 43.3 → 52.0 → 46.5 | 56 → 65 → 60 | +13 |
  | 42.5 → 44.2 → 43.0 (next phrase) | 55 → 57 → 56 | +13 (held) |

  Within an utterance the intervals are exact: an 8.7-semitone spoken span
  comes out as a 9-semitone sung span.

- **Octave detection errors have to be caught.** pyin occasionally reports half
  the true frequency, putting one word exactly ~12 semitones out. Live this
  looked like `hello@50 what is@51 up@61 my@61` — and because the stray value
  drags the median, it swung the whole utterance's shift (+17 on one phrase,
  +6 on the next). `correct_octaves` compares each word against *the others*
  (never against a median it helped set) and snaps whole-octave outliers back.

  One- and two-word utterances have nothing to compare against, which is how a
  lone "hello" set a +17 shift for a whole session. They are checked against a
  running baseline of your usual pitch instead — learned only from phrases of
  three or more words, so a single mis-detection cannot define it.

- **The pitch curve must be measured against the word's own pitch, not its
  final tone.** This one hid for a long time. The curve is stored relative to
  the note, and the note has already been transposed onto the voicebank's
  pitch — so comparing raw F0 against the shifted tone gives the transposition
  distance (~1600 cents), which pins every point to the clamp. The "contour"
  was really a constant six-semitone drop below the note, which is what made
  the voice sound strained. `contour_points` now takes the pre-shift median.

  With that fixed, "hello" produces `+102, +52, -3, -46, -150` cents — a real
  falling intonation.

- **Smooth the curve, don't delete it.** Raw F0 carries the intonation you hear
  *and* frame-level jitter. The jitter made it warble; removing the curve
  entirely made every word monotone and robotic. It is now median-filtered
  (killing pyin's octave slips) then averaged, which costs very little:

  | Contour | Spectral flatness (noise) |
  |---|---|
  | flat (robotic) | 0.0042 |
  | **smoothed 60 ms (default)** | **0.0056** |
  | barely smoothed | 0.0055 |

  Tunable via `contour_smooth_ms`, `contour_points` and
  `contour_range_cents`; `emit_contour: false` restores flat notes.

- **Expand contractions before stripping punctuation.** The apostrophe is the
  only thing separating "I'm" from "im", and the dictionary sings the latter as
  *eem*. `clean_lyric` expands 39 contractions first (`I'm` → `i am`, `don't` →
  `do not`), then removes the remaining punctuation, so possessives like
  `teto's` still reduce safely.

- **Unknown words are sung as silence.** `EnXSampaPhonemizer` returns a single
  `error` phoneme for anything outside its English dictionary — names, Japanese
  words, invented ones. Worse, one unknown word used to poison every word after
  it in the same request; those are now retried individually.

  `pronunciations.json` fixes the word itself, two ways:

  ```json
  {
    "phonemes":    { "kasane": "k A s A n E", "teto": "t E t oU" },
    "respellings": { "kasane": "kah sah neh", "teto": "teh toe" }
  }
  ```

  **Phonemes are exact and take precedence** — space-separated X-SAMPA passed
  to the phonemizer as a phonetic hint, bypassing the dictionary entirely while
  OpenUtau still builds the CVVC transitions. Use the symbol set in
  `teto_relay/phonemes.py`, which was read out of this bank's own oto aliases:
  vowels `3 @ A E I O OI U V aI aU e eI i oU u {`, consonants
  `D N S T Z b d dZ f g h j k l m n p r s t tS v w z`. Hints must be **space**
  separated — commas are taken literally and produce one unusable phoneme.

  **Respellings approximate** the word using other English words, and are the
  fallback. They are easier to write by ear but only as good as the guess, and
  an entry that is itself unknown just moves the problem (`utauloid` →
  `oo tao loid` fails, because `loid` is not a word).

  A word can be *in* the dictionary and still wrong: `teto` resolves to
  `- ti, i t, toU, oU -`, the English reading TEE-toh. Overrides apply to those
  too, not only to unknown words.

The log names the exact cause whenever something is silent: the specific
phonemes with no sample, or the specific words missing from the dictionary.

## Design notes

- **Word timings are measured, not guessed.** Whisper derives them from
  attention, and they are systematically wrong: measured against a forced
  aligner, *every* word in every test sentence started **later** than whisper
  claimed — by 0.06 to 0.20 s, averaging ~0.12 — and most spans ran too long.

  That error propagates twice over, because both the note length and the pitch
  window come from those spans: a span that starts early and runs long samples
  silence and the neighbouring word. `use_alignment` (on by default) replaces
  them with `torchaudio`'s MMS_FA alignment for about **0.08 s** per utterance.
  On a test sentence it changed 3 of 8 notes. The model is ~1.2 GB, downloaded
  on first use.

- **Load order matters, and getting it wrong is fatal.** faster-whisper (via
  ctranslate2) and torch each bundle their own cuDNN, and whichever initialises
  first wins. Load whisper first and the next CUDA convolution — crepe, or the
  aligner — dies with `Could not load symbol cudnnGetLibConfig` and takes the
  **whole process** with it, with no Python traceback. `_warmup` therefore
  touches every torch-backed model *before* whisper is loaded. Do not reorder
  it.

- **Model caches are redirected in code**, not in a shell script — `torch`,
  HuggingFace and the aligner all default to the user profile on C:, which has
  under 2 GB free here. `teto_relay/__init__.py` points them at `.cache/` on D:
  before torch or huggingface_hub can be imported.

- **Pitch tracking runs on the GPU.** `pitch_method` defaults to `crepe`
  (torchcrepe, CUDA). Measured against pyin on the same speech:

  | Sentence | pyin | crepe-full | crepe-tiny |
  |---|---|---|---|
  | "hello what is up my name is jay" | **3.17 s** | 0.28 s | 0.09 s |
  | "let us see if the pitch is accurate" | 1.33 s | 0.33 s | 0.09 s |
  | "i know what is up right now" | 0.97 s | 0.23 s | 0.08 s |

  The win is speed *and consistency* — pyin ranged 0.97–3.17 s on
  similar-length speech, which is the same behaviour as the 14 s analysis spike
  seen live. Both produce identical notes on the same audio (`[62, 61, 61, 60,
  59]`, shift `+19`) and agree on tones to within 0.1 semitones.

  Note what is *not* claimed: crepe was not shown to fix octave errors. Both
  trackers were clean on every test signal available here, so the octave
  correction stays. `pitch_method: "pyin"` needs no GPU.

- **Latency is inherent.** Measured on this machine, steady state:

  | Utterance | whisper | pyin | total analyse |
  |---|---|---|---|
  | 1.0 s | 0.80 s | 0.45 s | 1.25 s |
  | 2.0 s | 0.80 s | 0.88 s | 1.67 s |
  | 3.0 s | 0.91 s | 1.33 s | 2.23 s |
  | 5.0 s | 2.03 s | 2.23 s | 4.27 s |

  Add the 0.4 s pause that closes a chunk, so a typical short phrase lands
  about **2 s** behind you. It is a relay, not a live voice changer.

  `pyin` is roughly half that cost and scales with `f0_min` — the widest search
  is the most expensive. On a 2 s chunk: 65 Hz → 0.92 s, 80 Hz → 0.70 s,
  110 Hz → 0.50 s. If your voice is not especially deep, raising `f0_min` to
  80–110 in `config.json` nearly halves pitch-tracking time.

- **Warm up before opening the microphone.** Loading the whisper model is not
  enough: CTranslate2 defers work to the first inference and `pyin` is
  numba-compiled, so first calls cost ~20 s combined. Left unwarmed, the first
  real utterance stalled that long while three more queued behind it.

- **Output is resampled to the device.** WASAPI shared mode rejects anything
  but the device's configured format — a 44.1 kHz render into a 48 kHz CABLE
  Input fails outright with `Invalid sample rate [PaErrorCode -9997]`.
- **Queues drop their oldest item.** Being a few seconds behind is worse than
  missing a phrase, so a slow renderer costs you an utterance rather than
  accumulating lag.
- **Octave shifting, not clamping.** A speaking voice sits well below Teto's
  range; shifting by whole octaves preserves every interval, where clamping
  would flatten the melody against the range limit.
- **The renderer falls back to tones** if the synthesis engine fails to start,
  so a bad install degrades the relay instead of killing it.

## Productionization log

Findings and decisions from the `productionize` branch. The prioritised list is
in [ROADMAP.md](../ROADMAP.md); this section records *why*.

- **The control panel had to stop trusting the browser.** It listens on
  localhost, but any page open in the same browser can send it requests. A
  cross-site POST could rewrite any setting, start the relay, or upload a
  `.pth`, and uploads were checked with `torch.load(weights_only=False)`, which
  unpickles them and so runs whatever code the file names. There are now two
  guards. The `Host` header must name this machine, which defeats DNS rebinding.
  Every state-changing request must carry an `X-Teto-Relay` header, which a
  foreign page cannot add without a CORS preflight, and the server never answers
  one. Where output and logs are written (`out_dir`, `log_file`) can no longer
  be set through the API; the Setup folders (`openutau_dir`,
  `voicebank_root`) and the RVC model and index paths still can, behind the
  header check, because the panel has fields for them. Uploaded models are opened with `weights_only=True`, which reads
  every genuine RVC checkpoint (tensors, numbers, strings) and refuses anything
  that would need to execute code.
- **`sounddevice` is imported on first use.** At module level it loads
  PortAudio, and without that library the whole app failed to import with a
  bare `OSError`.
- **Settings and data now live in one data folder** (`teto_relay/paths.py`).
  A source checkout keeps using the project folder, so nothing moves. A packaged
  build uses `%LOCALAPPDATA%\TetoRelay`, or a `data` folder beside the exe when
  a `portable.txt` sits next to it. PyInstaller's onefile mode unpacks the code
  into a temporary folder, and Program Files is read-only, so neither can hold
  settings. `TETO_RELAY_HOME` overrides the choice.
- **`config.json` is no longer in git.** The committed file was one machine's
  settings (`voicebank: "miku"`, `D:\` paths), and a new user's first start
  crashed on it. `openutau_dir` and `voicebank_root` now default to empty,
  which means "search the usual places" (`teto_relay/locate.py`). The panel
  has a Setup group to set them. **Upgrade note for an existing checkout:**
  `git pull` deletes an unmodified tracked `config.json`, so copy it aside
  first and put it back afterwards; it is now ignored by git.
- **The config is validated.** Hand edits and form fields send `"3"` for `3`
  and `"true"` for `true`, and those used to be stored as-is and crash
  mid-utterance. Values are now coerced to the setting's type, and ranges,
  choices and relationships (`f0_min < f0_max`, `sample_rate` must be 16000)
  are checked. Every problem is listed at once, with the fix. Keys this version
  doesn't know are ignored with a warning instead of refusing to start, so a
  config from a newer or older version still loads. Saves go through a temp
  file, so a crash mid-write can't leave a broken config behind. Relative paths
  are resolved against the data folder when loaded, because the OpenUtau host
  `chdir`s into its own folder later.
- **Voice mode no longer needs a voicebank.** It never sings through one, but
  it refused to start without one.
- **Housekeeping can't kill a worker any more.** `_trim_output` runs in each
  worker's `finally:`, and a file vanishing between the directory listing and
  its `stat()` raised out of the loop and ended the render thread for good.
  It now swallows its own errors. It also counts utterances (a `.ustx` + `.wav`
  pair) rather than files, and never trims below what is still queued.
- **A stalled render gives up after `render_timeout_seconds` (30 s).** The
  synthesis task was awaited with `task.Result`, which has no limit. It is now
  `task.Wait(timeout)`, then the cancellation token, then a `RenderError`, which
  costs one utterance instead of silently blocking every later one. Untested
  against a real stall; the timeout path is unit tested with a fake task.
- **Errors are for people.** `errors.TetoRelayError` marks a problem the user
  can fix, and its message has to say what happened, why, and what to do. The
  CLI prints those without a traceback (exit 2). Anything else is treated as a
  bug: a one-line summary pointing at the log file and `--doctor` (exit 1),
  with the traceback in the log. Under `pythonw` (tray, packaged app) there is
  no console, so these go to a Windows message box; before, a startup error
  there just made the app vanish. Config, voicebank, device and OpenUtau errors
  were all rewritten to this standard. OpenUtau failing to start still falls
  back to tones, but now says so in plain words.
- **A failed start cleans up after itself.** `TetoRelay.start()` stops whatever
  had already started before re-raising. A failure after the player or the
  mic had opened used to leave them running, and a second Start opened a second
  set.
- **`--doctor`** checks Python, packages, the data folder, audio devices
  (without opening them), feedback loops, voicebanks, OpenUtau, the .NET 8
  Desktop Runtime (by looking in `dotnet\shared`, without starting it), torch
  and CUDA, and voice-mode files, and prints the fix beside each problem.
- **The control panel** opens the browser itself, explains a busy port, and
  simply shows the already-running panel when launched twice. On Windows it
  binds with `SO_EXCLUSIVEADDRUSE`, because `http.server`'s default
  `SO_REUSEADDR` lets a second process share the port silently there. Handler
  threads are daemons, so Ctrl+C no longer waits for the browser's keep-alive
  connections to close.
- **The microphone reconnects.** If the input stream couldn't be opened, or
  died, the capture thread logged once and exited, and the relay kept saying
  "running" with nothing listening. It now retries with a backoff (1 s,
  doubling, capped at 10 s). It also treats 2 s without any audio as a dead
  stream, because an unplugged USB mic often stops calling back without raising
  anything. `MicCapture.state` / `last_error` and `TetoRelay.health()` report it
  to the panel. Dropped frames and overflows are counted in the audio callback
  and logged from the capture thread, instead of one log line per frame from
  inside the callback. Tested with a scripted fake stream; untested with a real
  unplug.
- **The tray shows failures.** It's the only UI under `pythonw`, and a failed
  start used to leave a red "live" icon over a relay that never started. Now
  the icon turns amber, the first menu line gives the reason (or the mic
  problem, or the last phrase), and the menu has Retry start, Open log and Open
  data folder, plus a Windows notification. The start/failure logic lives in
  `TrayApp`, apart from pystray, so it is unit tested. The pystray menu itself
  is untested here (no desktop).
- **Latency is measured stage by stage** (`teto_relay/latency.py`). Each
  utterance carries a `Timeline` from key release to first sound:
  `wait_analyse`, `asr`, `align`, `pitch`, `notes`, `ustx` (or `convert` in
  voice mode), `wait_render`, `render` (split into `phonemize` and `synth`),
  `wait_output`, `output` (reading, resampling, opening the stream) and
  `lead_silence`, the silence at the head of the audio, which is latency too.
  One `Latency ...` line is logged when playback starts, and a row is appended
  to `latency.csv` in the data folder. The panel gets it as
  `stats.latency`. An end-to-end test runs a real utterance through
  pyin → notes → `.ustx` → tone renderer → player (fake sounddevice) and checks
  every stage is reported. That test caught `Player._started` being silently
  shadowed by `threading.Thread`'s own `_started` event.
- **Rendered audio starts at the first sound** (`trim_leading_silence`, on by
  default). `_mix` laid phrases out at their absolute project time, and the
  part starts where the first word was said inside the recording (200 ms
  pre-roll plus your reaction time), so every WAV began with 0.2–0.5 s of
  silence that went straight into VB-Cable. All phrases are now shifted
  together so the earliest starts at zero. The old code also clamped a negative
  first offset to zero on its own, which moved that phrase against the others.
  Whether WORLDLINE's `positionMs` is absolute (as I read OpenUtau's source) is
  untested here. If it turns out to be relative, the change is a no-op, and the
  `lead_silence` stage in the latency line will show which it is.
- **Voicebank discovery is bounded.** It used `rglob`, which walks every file
  under the root and filters by depth afterwards. The old default root
  (`D:\Claude`) held this repo, its venv and gigabytes of model caches, and
  was walked at every start, every panel load and every bank-image request.
  Discovery now prunes while walking: at most 3 folders deep, and it never
  enters hidden folders (`.venv`, `.git`, `.cache`, `.installing-*`),
  `__pycache__`, `site-packages` or OpenUtau's own `Cache`/`Dictionaries`/
  `Plugins`. A bank with no `character.txt`, buried deeper than 3 folders,
  is no longer found; move it up or point `voicebank_root` nearer to it.
- **The OpenUtau renderer stops repeating itself.** Each utterance re-ran
  `Assembly.LoadFrom`, three reflection lookups and one or more
  `System.Linq.Expressions` compiles for the `timestamp` setter (one more per
  retried word). These are now done once per renderer or per field. The
  UI-callback queue used to be drained 256 items at a time, so anything past
  that stayed queued and piled up; it is now drained completely. Untested
  against OpenUtau, and the saving per utterance hasn't been measured; the
  `phonemize` stage in the latency line will show it.
- **The panel page lives in `teto_relay/web/index.html`.** It shows a banner
  for current problems, has a Check setup button (the doctor), and a
  release-to-sound meter. `tools/panel_smoke.mjs` drives it in headless
  Chromium. On its first run it found that a dropdown whose saved value wasn't
  among its choices showed the first choice instead, so Save silently changed
  the setting; the default speech model `base.en` wasn't in the list. It also
  found the avatar being re-requested every second when no bank was installed.
- **Numbers are spelled out** (`teto_relay/numbers.py`). Whisper writes
  digits, and neither dictionary can pronounce `2`, so every number was sung
  as silence. `clean_lyric` now spells them the way they are said: counts, a
  four-digit number on its own as a year, `5:30` as "five thirty", ordinals,
  decimals, `%` and `$`. English only.
- **Multi-word lyrics are converted word by word in Japanese mode.** An
  expanded contraction ("I'm" → "i am") or a spelled number stays one lyric,
  and it was looked up in cmudict as one string, missed, and romanised letter
  by letter (`iam` → い あ む by spelling). Each word is now converted on its
  own and the kana joined.
- **Small kana are expanded.** pykakasi turns ファ into ふぁ, and `split_morae`
  made ぁ a note of its own, with no sample in any bank. Small vowels now
  become full-size (ふぁ → ふ あ) and ゔ becomes ぶ. A test checks that loanwords
  come out as morae the tables can produce.
- **One odd word no longer disables alignment.** The MMS_FA tokenizer raises on
  any character outside a–z and the apostrophe, and `refine` then fell back to
  whisper's timings for the *whole* utterance. Tokens are now normalised
  ("café" → "cafe"). A word with nothing alignable (kana, kanji) keeps
  whisper's timing, and the rest are aligned as usual.
- **Smaller fixes (P2):**
  - Push-to-talk hands finished phrases to the capture thread through a queue.
    A single slot lost a phrase on a quick release/press/release.
  - `pronunciations.json` is parsed once until it changes, instead of twice
    per utterance.
  - The measured bank pitch is cached per folder rather than per short key.
  - Voicebank zips are checked for their unpacked size (≤ 4 GB, and it must
    fit on disk) before anything is extracted.
  - The `.ustx` writer keeps at least one tick between notes that had a gap in
    seconds, because rounding could make them touch or overlap. Notes that
    touch in seconds, or are marked `legato`, may touch.
  - `persistent_output` (off by default) keeps one output stream open and
    writes each phrase to it in 1024-frame blocks, instead of `sd.play`
    opening a new stream for every phrase. Tested with a fake stream only.
    Whether it helps shows up in the `output` stage of the latency line.
- **Singing options, off by default** (see ROADMAP.md for the analysis behind
  them). `singing_style: "sung"` (`teto_relay/singing.py`) snaps notes to a
  scale and a key. The key is found per phrase as the one needing the smallest
  total nudge, and held between phrases unless another fits clearly better
  (by more than 1.5 semitones in total). It also scales the spoken contour
  down (`sung_contour_amount`), puts vibrato on notes of at least
  `vibrato_min_seconds` (written to the `.ustx` `vibrato` block and set on
  OpenUtau's `UNote.vibrato`), and holds the last note for
  `final_hold_seconds`. `legato` lets the morae of one word touch while words
  keep their gap. The tone renderer (`backend null`) renders the vibrato too,
  so it can be heard without OpenUtau. Unit tested at the note and `.ustx`
  level. How OpenUtau renders the vibrato, and whether touching morae still
  collapse the hosted phonemizer, are untested.
- **Streaming voice conversion** (`voice_streaming`, voice mode only, off by
  default; `teto_relay/streaming.py`). This is the one path that can respond
  while you're still talking. Microphone frames go to a `BlockStreamer`, which
  converts every `stream_block_ms` of new audio together with
  `stream_context_ms` of what came before. It keeps only the output that
  belongs to the new block, and joins blocks with a crossfade placed where the
  two overlap best (SOLA), so the seams neither click nor phase. Output goes to
  a kept-open stream, with soxr resampling to the device rate so block edges
  are continuous. Push-to-talk opens and closes a gate; releasing finishes the
  last block. The expected delay is one block plus the crossfade (0.35 s by
  default) plus conversion time, and the log warns if a block takes longer to
  convert than it lasts.
  Tests: an identity converter is reconstructed to 1e-6, a 3× rate change,
  a converter that shifts its output by 40 samples is realigned by SOLA, the
  gate/flush logic, the mic tap and the resampling output. Untested: RVC's
  speed and per-block quality on a real GPU, and the audio devices.
- **Packaging** (`packaging/`). A PyInstaller onedir build with two programs
  sharing one folder: `TetoRelay.exe` has no console and opens the panel, and
  `TetoRelayConsole.exe` is for `--doctor` and the other options. Onefile was
  ruled out because it unpacks hundreds of MB to a temp folder on every start.
  The Inno Setup installer is per-user (no admin), checks for the .NET 8
  Desktop Runtime, and leaves the data folder on uninstall. `build.ps1` runs
  the tests before building, smoke-tests the built exe, and makes the portable
  zip with `portable.txt`. The panel gained Quit, because the windowed exe has
  no console to Ctrl+C. The spec was built and run on Linux as a check; the
  Windows build hasn't been run. `--doctor` now uses cmudict and pykakasi
  instead of only locating them. That found the frozen build missing cmudict's
  `importlib.metadata`, which cmudict reads when it is imported.
- **An independent review of the branch** (two reviewers: one for code, one
  checking the docs against the code) found 26 issues; all are fixed except
  the replugged-mic limitation below. The code-review fixes have regression
  tests that fail on the reviewed commit; of the doc-review code fixes only
  the `--config` one has a test (the restart hints, the `innerHTML` change and
  the installer change don't). A third pass over the fixes found two more
  small gaps, fixed with tests: a one-sample loss per seam when a converter's
  output length is a sample short, and a remaining race between
  `StreamOutput.write` and `close`. The notable ones: the panel ignored
  `--config`; installing an RVC file silently disconnected the panel's settings
  from the running relay (a bug from before this branch); saved paths inside
  the data folder are now stored relative to it, so a portable copy survives
  being moved; `BlockStreamer` clamps its crossfade and search to the context,
  because with less context it dropped samples at every seam; numbers of a
  trillion or more are read digit by digit instead of crashing.
- **Known limitation: a replugged USB mic.** PortAudio enumerates devices only
  when it initialises, so after an unplug/replug the old device index can stay
  invalid. Re-initialising PortAudio from the capture thread would also close
  the output stream mid-phrase, so it isn't done. Stop/Start recovers it.

## Hardware testing and tuning log

The `productionize` branch was then run on the target PC: Windows 10, a
GTX 1060 6 GB (Pascal), 8 GB of RAM, OpenUtau in `D:\Work\OpenUtau`, the three
Teto banks plus a Miku English bank, VB-Cable. Everything below was found by
running it there, measured, and fixed with a regression test where one could
be written. The user recorded 14 phrases (English, Japanese, sung) that the
tuning was scored on with `tools/tuning_eval.py`.

### How the tuning was judged

`tools/tuning_eval.py` runs recorded phrases through exactly what the relay
does (`TetoRelay.analyse`, then the renderer) and scores the singing:

- **intelligibility**: a second whisper model (`small`, not the relay's)
  transcribes her, compared with what the relay heard (word or kana error,
  capped at 1 per phrase - a judge looping on a held vowel scored 4);
- **stretch** and **pauses kept**, against the recording's noise floor;
- **missing** phonemes (no sample in the bank).

Plus, as scratch scripts: loudness similarity and **dropouts** (the user is
singing, Teto is silent), and **onset correlation** (do her syllables start
where theirs did). The judge's run-to-run noise on 16 phrases is about
+-0.03 word error, so every decision below was taken on two runs or more, and
the user listened to each round and said what was wrong.

### Bugs that affected every phrase

- **The pitch curve was written in cents; OpenUtau reads tenths of a
  semitone.** Measured: a tone-60 note with its curve held at y=30 sings
  MIDI 63.05, at y=100 70.05. Every inflection was ten times too big - a
  "hello" shown as tone 62 was sung at MIDI 86.7. (`a0c795d`)
- **The pitch was sung late by the part's start time.** In a hosted project
  OpenUtau places phonemes relative to the part but reads the pitch at
  project time, and every part starts where the first word was said (0.2-
  0.5 s in). A staircase of notes: 0.2 semitones mean error with the part at
  0 s, 3.6 at 1 s. The hosted part now sits at 0. The user's Senbonzakura had
  the right notes and the wrong pitch because of this. (`26f2b0e`,
  `tests/test_hardware.py`)
- **Touching notes "collapsed" because the relay grouped them.** OpenUtau's
  phonemizer groups are one note plus its `+` extensions; the relay sent each
  run of touching notes as one group, so only the first lyric of each run
  was sung. This was the "notes must never touch" rule in this file and the
  reason for `note_gap_ms`. (`e69b847`)
- **A one-tick gap between touching notes** (start and length rounded
  separately) was heard as a rest: "to sing" came out "to -" + "- sing", a
  hard stop mid-phrase. (`f8c0098`)

### Timing

- Onsets are the rhythm. Each note used to get 0.22 s a syllable first and
  push the rest along; a phrase could end 0.6 s late. Onsets now stay where
  they were said; a squeezed word may borrow at most `onset_push_ms` (60 ms)
  from the next one. (`5165d44`, `27c3bad`)
- A pause is a rest. Half of every pause used to be spread over the word
  before it ("it just stretches a word when you leave a gap"). The release
  into a rest is at most 0.15 s; the utterance's last word gets it too.
- Word spans come from the recording's sound, not whisper's timestamps:
  trimmed to the last block of sound (whisper folds a pause into the start
  of the next word; a click inside a 1.7 s pause kept it inside "that") and
  extended while the sound goes on (a held ら of 桜 ran past whisper's end
  of the word and Teto stopped while the user was still singing). (`09d560d`,
  `d09ad85`)
- `legato` is on: words said without a pause (`phrase_gap_ms`, 250 ms) touch,
  so the bank joins them (VCV/CVVC transitions).
- Each word's vowel is on the beat and its consonant before it
  (`vowel_on_beat`), as parts are written. (`4f52bfb`)
- **Japanese syllables are timed from the forced aligner** (`align_morae`):
  morae romanised, aligned sound by sound, each note starting at its vowel.
  Evenly spread morae put the user's sung syllables off the melody. Onset
  correlation with the recording 0.418 -> 0.439 (Senbonzakura 0.253 ->
  0.337). The aligner had never run on this PC: torchaudio's loader needs
  ~2.5 GB of RAM at once; it is now built on the meta device with the
  checkpoint memory-mapped (0.4 GB). (`7465a6d`)
- **English word alignment is off** (`use_alignment`): on the user's accented
  English it made the singing less clear (0.07 -> 0.12 word error, twice);
  it aligns by spelling. (`6b23ee9`)
- **Where two English words meet is measured** (`align_boundaries`). The
  user: "control" is said for a very short time. Traced: the note was 0.34 s
  for 0.40-0.46 s of speech, and the "it" after it 0.42 s for ~0.14 s -
  whisper ends long words early and hands the rest to the next word (aligner
  vs whisper on the user's takes: long words end +0.14 s later). Inside the
  note OpenUtau's English phonemizer also gives every syllable but the last
  a fixed ~60 ms vowel; splitting words into "+" notes per syllable changed
  that by 20 ms on Teto and nothing on Miku, so it was not the cause.
  Moving all word edges to the aligner's (`use_alignment`) cost words, so
  only the line between two touching words moves - to the middle of the
  aligner's end and next start, at most 0.3 s, each word keeping 70 ms a
  syllable. Scored by one fixed judge (whisper medium, temperature 0, same
  references; whisper small with fallback sampling varied 0.04-0.10 on the
  same renders): user's takes 0.123/0.131 -> 0.113, with five "control"
  sentences 0.189 -> 0.102/0.109; Miku unchanged (0.825 -> 0.823). Sung
  length against said (aligner on both): timing error 0.53 -> 0.32, within
  25% 34% -> 50%; "control" 72-83% -> 97-101%.
  The aligner's model pass needs no words, so it runs on a thread while
  whisper listens (`align.Pending`). On the CPU (the packaged app) it is
  0.5 s for a 2 s phrase and 0.9-1.6 s for 5-7 s; overlapped with whisper on
  the GPU that leaves +0.15 s / +0.5 s / +0.9 s for 2 / 5 / 7 s phrases.
  int8 quantisation was only 1.35x faster and moved one boundary 0.38 s.
  Off with both on the CPU, where they would compete.

### Sound

- Performance (`teto_relay/performance.py`), sung style: a scoop into each
  phrase (1.5 semitones below, held 25 ms, then up), portamento scaled to the
  interval and mostly after the boundary, overshoot on leaps, a fall at each
  phrase end, vibrato that waits then grows. Several of these came from a
  VOCALOID tuning guide the user shared.
- Dynamics follow the singer's own phrasing (upper envelope over 200 ms,
  compressed by half); a fixed -12 dB fade at every phrase end made a sung
  line sound as if it faded out. Following every 40 ms copied their
  consonant dips onto hers.
- Breathiness and voicing touch only the last moment of each phrase. With
  points only at phrase ends they had ramped across the whole next phrase.
- WORLDLINE-R's curve units were measured before use: `dyn` is tenths of a
  dB and clips above ~+5 dB, `brec` +50 costs ~6 dB of harmonics-to-noise,
  `tenc` distorts near +100 (not used), `voic` 0 is a whisper.
- Long vowels (ムー, こーひー, ミュ|ウ) are `+` extension notes: one sample
  held, instead of a new う sample with a glottal attack. (`bbd8bbe`)
- Doubling (`double_voice`, `double_when`): quiet detuned, delayed copies
  under the lead, only on phrases that were sung by default. A phrase counts
  as sung when 38% of its voiced time is held notes; the user's speech
  scored at most 0.34, their singing at least 0.41. (`d53a819`)
- Tried and rejected, measured: consonant velocity (VEL) on crowded words -
  word error 0.09 -> 0.14 on this OpenUtau build; a wider melody - the user:
  "tries too much"; a `-` tail note - "word not found" on this build.

### Recognition and reading

- Contractions are kept whole ("can't", "I'm") and sung from CMUdict's
  pronunciation - a hint for English banks, morae for Japanese ones (かんと,
  not かんのと). They had been expanded ("can not") because stripping the
  apostrophe made "im" sing as "eem"; expanded, two syllables were sung in the
  time of one. Three sentences full of contractions, judged by whisper small:
  Teto English 0.54 -> 0.25 word error, tandoku 0.66 -> 0.59, Miku 0.77 ->
  0.74. The expansion is still the fallback for a word with no pronunciation.

- Whisper `small` (multilingual) on the GPU: 2.8% word error on the user's
  English, 0.4 s. `base.en` got 42% (it invents words); the default is now
  `base`. `float16` doesn't run on Pascal and falls back to `int8`.
- Output tokens are bounded by the audio length: a repetitive phrase looped
  to 448 tokens at every fallback temperature (17.7 s for 2 s of speech).
- A phrase is dropped only if unsure *and* probably not speech: accented
  Japanese at avg_logprob -0.97/-1.02 used to vanish.
- `lyrics_hint`: sung Japanese is misheard by every model size; with the
  lyric as the prompt all of them heard Senbonzakura exactly.
- Japanese is read as a phrase (pykakasi with katakana made hiragana first,
  readings shared back to whisper's words), with `READING_FIXES` for lone
  kanji it misreads (君 くん, 人 にん, 月 がつ, 日 にち, 今日は, 愛し). Split
  kanji are rejoined (明|日 あした); a lone ー / small kana joins the word
  before it. MeCab/UniDic was compared and not used (249 MB, and 私 わたくし,
  明日 あす).

### Voicebanks

- ARPAsing banks (the Miku English bank: "- hh", "aa", "aa -") are a flavour
  of their own and use OpenUtau's ARPAbet phonemizer; they sang nothing
  before. That bank has no "w" and almost no transitions, so it is far less
  clear than Teto (about 3 in 4 words wrong to the judge).
- A bank's recorded pitch is sampled across all its files: the first 12 by
  name were Miku's consonants, read an octave low, and she was aimed an
  octave below her voice.

### Thai

Scored on 20 Thai phrases (two Thai text-to-speech voices; the user's own
recordings are next) with `tools/tuning_eval.py`, letter error as heard by
whisper `medium`, which the relay does not use:

| bank | before | after |
|---|---|---|
| Teto tandoku (Japanese CV) | 0.873, 98 missing samples | 0.589, 0 missing |
| Teto English (X-SAMPA) | 0.925, 98 missing | 0.660, 0 missing |
| Miku English (ARPAsing) | 0.910, 96 missing | 0.832, 9 missing |

- **Whisper hands Thai back in pieces**, a character or two each
  ("ส | ว | ั | ส | ด | ี"), and gives vowel and tone marks no duration -
  those were dropped as zero-length words, so ชื่อ became ช-อ. Each piece was
  then converted on its own. Pieces with no duration now stay with the one
  before, and a run of Thai pieces is re-cut into words (pythainlp), each
  timed from the pieces it spans. (`b215fd8`)
- **Listening**: with language `th` the relay uses Thonburian Whisper small
  (Mahidol University's biodatlab, Apache-2.0): 1.9% character error on the
  test phrases against 3.6% for whisper `small`, at the same speed (`medium`:
  0.5%, 2.3x slower). The faster-whisper conversion is a third party's, so it
  is pinned to a revision and its weights are hash-checked - they are
  byte-identical to a conversion made here from biodatlab's release.
- **Pronunciation by sound**: pythainlp's `thaig2p` model gives each word's
  syllables with vowel length and finals (ความรัก: kʰwaːm . rak̚). It loops on
  long inputs and sometimes drops syllables, so its answer is checked
  (at most one syllable per two letters, no syllable three times running, no
  fewer than the rule-based romanisation's vowel groups) and retried a word
  or a dictionary syllable at a time. The old route romanised the spelling:
  ท came out as English "th" (เธอ -> せ), finals grew a vowel (รัก -> らく),
  ด was ぢ.
- **Japanese banks**, each choice scored on tandoku: long vowels held (not
  holding them: 0.731); an unreleased stop is a rest the length of a mora
  (left out, the vowel ran into the next syllable: 0.683; a Japanese extra
  mora ら-く: 0.705); clusters kept with an inserted vowel, ครับ くらっ
  (dropping the r as in conversation: 0.683); ɤ (เธอ, เลย) as あ (as う:
  0.647). The rest has to be a note through the layout - left as an empty
  slot, the layout stretched the vowel over it - and is dropped when the
  project is built.
- **English banks** get whole syllables as X-SAMPA, stops included; ARPAsing
  banks get the same converted to ARPAbet. Before, an ARPAsing bank got the
  romanised word with no hint, looked it up in its English dictionary, and
  sang silence.
- てぃ / でぃ / ふぁ are one mora; the small vowel alone had no sample. A bank
  without one sings the nearest basic mora (でぃ -> ぢ).
- **Invented words.** Unsure, the Thai model repeats a stock phrase
  ("สวัสดีครับ", "ขอบคุณที่ติดตาม") until the token budget runs out - the
  user's "... ตลอด เว้ย" came back with "สวัสดี ครับ" three times after it.
  Tested on phrases with a noise tail: without the English vocabulary prompt
  it looped on half of them, so the prompt stays; faster-whisper's
  `hallucination_silence_threshold` made it far worse (invented text on
  almost every phrase); a repetition penalty of 1.15 stopped every loop at
  the same accuracy. The invented words were all timed after the last sound,
  over silence, so words there are dropped (except a letter or two right
  after the voice: a final บ is a silent closure), and a loop that ran out of
  budget is cut to one copy. (`stt.invented`, `stt._trim_loop`)
- That did not catch what the user met next: a video intro tacked onto a
  real sentence ("... หนึ่ง สวัสดี ครับ คลิป นี้ เป็น รายการ เกี่ยวกับ
  ข้อมูล"). Reproduced by making the 20 test phrases quieter with room noise
  and a 1.5 s noisy tail: 2 of 22 phrases grew such endings. Each was a
  second segment starting after the speech, at avg_logprob -0.6 against
  -0.2 for the sentence, its first letter at probability ~0.05 - and the
  noise tail's spikes had put "the end of the speech" at the end of the
  recording, so the silence check passed it. The end of speech is now the
  150 ms median loudness against the room's noise floor, and such a segment
  is dropped (`stt.invented_segment`). Noisy set: 0 phrases with invented
  endings (was 2), letter error 0.084 -> 0.033; nothing dropped from the
  user's English or Japanese recordings.
- Tone is not carried by the lyrics. In *Speech* style the pitch follows the
  speaker, who carries it; *Sung* style replaces it with a melody.

### Voice mode (removed in 0.3.0; kept as a record)

- Streaming: the push-to-talk gate was read when a frame was dequeued, so a
  lagging converter dropped the end of the phrase. On the GTX 1060, crepe
  with the index takes ~800 ms per 300 ms block; `pm` without the index
  keeps up (~250 ms, ~0.8 s delay).
- `harvest` reused the first utterance's pitch (rvc's lru_cache keyed on a
  fixed path) and crashed on a different length.
