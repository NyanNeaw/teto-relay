# Teto Relay

Listens to your microphone, detects the real pitch of your voice, and re-sings
what you said through a Kasane Teto UTAU voicebank into VB-Cable — with no
OpenUtau window, no keystroke automation, and no second PC.

> **Picking this up fresh? Read [HANDOFF.md](HANDOFF.md) first** — current
> status, the settings to use, open issues and next steps. This file explains
> *why* each decision was made, which is longer than you need to get running.

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

- **Notes must never touch, but the gap must be tiny.** Two notes that meet are
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
