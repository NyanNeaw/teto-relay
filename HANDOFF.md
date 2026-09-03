# Teto Relay — status and handoff

Written at the end of the build session. The README covers *why* each decision
was made; this covers **where things stand** and **what to do next**.

There is **no git repository** here — everything is plain files on disk under
`D:\Claude\teto-relay`. Nothing is staged or uncommitted; what is on disk is
the state.

---

## Working end to end

Hold **F8**, speak, release → Teto sings it into VB-Cable about 2 s later.

```bash
cd D:\Claude\teto-relay; .\.venv\Scripts\python.exe -m teto_relay
```

Or with the browser control panel (settings, start/stop, live log):

```bash
cd D:\Claude\teto-relay; .\.venv\Scripts\python.exe -m teto_relay --web
```
then <http://127.0.0.1:8765/>.

| Stage | How it works now |
|---|---|
| 1. Capture | Push-to-talk on F8. You mark the boundaries, so whisper never sees half a sentence. |
| 2. Transcribe | faster-whisper `base.en` on CPU, with anti-hallucination gates and a vocabulary prompt. |
| 2b. Align | `torchaudio` MMS_FA forced alignment on GPU measures when each word was really said (~0.08 s). |
| 3. Pitch | torchcrepe on GPU (~0.1 s), octave-error correction, shift onto the bank's recorded pitch. |
| 4. Lyrics | English converted to Japanese morae ("i love you" → あい らぶ ゆう), one mora per note. |
| 5. Render | OpenUtau hosted in-process via pythonnet, WORLDLINE-R. No GUI. |
| 6. Playback | Resampled to the output device, into VB-Cable. |

**110 unit tests pass**: `.\.venv\Scripts\python.exe -m unittest discover -s tests`

---

## Settings that matter

`config.json` is already written with these. It **overrides** the dataclass
defaults, so if behaviour ever contradicts the code, check this file first —
a stale `config.json` silently reverted two fixes during the session.

| Setting | Value | Why |
|---|---|---|
| `voicebank` | `tandoku` | 104/104 morae. renzokubeta has no youon and drops sounds. |
| `lyric_mode` | `japanese` | Under review — see the mode comparison below. |
| `pitch_method` | `crepe` | ~0.1 s vs pyin's 0.97–3.17 s, and consistent. |
| `use_alignment` | `true` | Whisper's timings run ~0.12 s early. |
| `note_gap_ms` | `2` | Zero collapses the phonemizer; 30 ms was audible between morae. |
| `renderer_backend` | `openutau` | Falls back to tone synthesis if the engine fails. |
| `mode` | `utau` | `voice` runs RVC conversion instead — both work. |

Tuning knobs, if it still isn't right:

- `min_note_seconds` (0.16) and `seconds_per_syllable` (0.22) — how long each
  **word** is held. English/native mode only.
- `min_mora_seconds` (0.06) and `max_mora_seconds` (0.25) — the bounds on a
  **mora** in Japanese mode. These are only bounds: the length itself is
  measured by the aligner, and the floor that actually applies is
  `voicebank.mora_floor`, read from the bank's oto (67 ms for tandoku).
  Raising the floor trades rhythm for intelligibility.
- `pause_borrow` (0.5) — how much of the gap after a word its morae may sing
  into. 1.0 matches the speech's total length exactly but leaves no silence and
  runs words together; 0.0 is the flat-rhythm case. Lower it if words smear.
- `contour_smooth_ms` (60) and `contour_range_cents` (300) — intra-note pitch
  movement. Lower smoothing = more expressive, more warble.
- `whisper_model` — `small.en` is more accurate but ~3× slower.

---

## Environment

Two virtualenvs, both on D: (**C: has under 2 GB free — keep everything off it**).

- `.venv` — the working environment. torch 2.5.1+cu121, torchcrepe, torchaudio,
  faster-whisper, pythonnet, cmudict, sounddevice, pynput.
- `.venv-rvc` — a separate environment for the abandoned RVC attempt. torch and
  pythonnet only. Safe to delete if RVC is dropped.

Model caches are redirected to `.cache/` **in code** (`teto_relay/__init__.py`),
not by a shell script, because running the app without `tools/rvc_env.ps1` was
still filling C:.

External dependencies, neither of which lives in this folder:

- OpenUtau at `D:\Work\OpenUtau` (hosted, never launched)
- Voicebanks at `D:\Claude\TETO-*`

---

## Two traps that will waste an hour if forgotten

**1. Load order is fatal.** faster-whisper (ctranslate2) and torch each bundle
their own cuDNN. If whisper loads first, the next CUDA convolution — crepe or
the aligner — kills the **process** with `Could not load symbol
cudnnGetLibConfig` and no Python traceback. `TetoRelay._warmup` touches every
torch model *before* whisper. Do not reorder it.

**2. `OpenUtau.Core.Format.Ustx.Load` cannot be used.** OpenUtau ships a .NET 9
`System.IO.Packaging` beside a self-contained .NET 8.0.19 runtime, so loading a
project file throws. We parse our own YAML and build `UProject` in memory
instead. The full hosting sequence is documented in `teto_relay/dotnet.py`.

---

## Open issues

- **Pronunciation: six mapping bugs found and fixed**, so "sometimes a bit off"
  should be noticeably better. These were defects in `japanese.py`, not the
  voicebank. Audited by running 246 common words through the converter and
  checking every mora against the bank's actual oto entries.

  | | was | now | |
  |---|---|---|---|
  | `music` | んゆうじく | みゅうじく | M/N before Y collapsed to ん and ate the youon — a word *starting* with ん |
  | `computer` | かんぴゅうたあ | こんぴゅうたあ | schwa before a closing nasal is オ (コン, not カン) |
  | `little`, `people` | りたる, ぴいぱる | りとる, ぴいぷる | the `-tle`/`-ple`/`-ful` schwa takes the vowel epenthesis would insert |
  | `months`, `clothes` | まんすす, くろうずず | まんす, くろうず | each consonant in a cluster got its own vowel, doubling the mora |
  | `not`, `stop`, `job` | なと, すたぷ, じゃぶ | のと, すとぷ, じょぶ | AA covers both "hot" and "father"; Japanese splits them by spelling |
  | `car`, `start` | かる, すたと | かあ, すたあと | a word-final R became る instead of lengthening the vowel |
  | `around`, `we` | あ**あああ**うんど, うい**い** | ああうんど, うい | a vowel written twice where one phone ended on the vowel the next began with |

  A/B renders are in `out/ab_pron_compare.wav` and `out/ab_vowel_compare.wav`
  (before, pause, after). Nine tests cover the new rules; the suite is 119.

  Note that `test_m_before_a_consonant_is_a_nasal_but_not_at_the_end` had
  **pinned the computer bug** (`startswith("かん")`) while the module docstring
  claimed こん. The test was written from observed output, not intended output.

- **Pronunciation limits that remain**, all from the bank's 104 morae rather
  than the mapping, so they need a different fix (or a different bank):

  - No sokuon っ: `not` is のと, not ノット. Nothing to hold it — the bank has no
    っ and one-mora-per-note leaves nowhere to put a geminate pause.
  - No ファ/ティ/ディ/ウォ, so `father` is はざあ, `body` ぼぢい, `water` をおたあ.
  - Word-final NG loses its ぐ: `sing` is しん, `morning` もおにん.
  - A schwa before a *word-final* nasal stays あ: `common` is こまん (コモン).
    Widening the rule to cover it breaks `everyone`, so it was left alone.

- **Japanese mode costs 2.22 morae per English syllable**, measured over 246
  common words. Most of that is not a defect and cannot be tuned away —
  ストリーム really is 5 morae for a one-syllable word, フレンド 4, サンクス 4.
  It is the main reason speech is harder to follow in this mode than in
  `native`, and the main argument for re-testing `native` now that the
  pronunciation bugs are gone. The words still above standard Japanese are
  `question` (くうえすちゃん, +1 over クエスチョン) and `always`
  (おおるうえいず, +1 over オールウェイズ), both from `K W`/`L W` clusters.
- **`renzokubeta` is unusable for this pipeline** — 69/104 morae, no youon. It
  dropped 8 of 12 morae in a test phrase. Use `tandoku`.
- **The 14.30 s latency spike was cold model load, not pyin.** Originally filed
  as pitch-tracker variance; that was wrong. Measured on this machine: crepe
  cold-loads in ~9 s and the aligner in ~9 s, against a warm pipeline of 2.3 s
  (stt 2.0, pitch 0.2, align 0.06). pyin's whole range is 0.97–3.17 s, so it
  cannot produce 14.3 s — but `_warmup` failing silently can, and used to: its
  own docstring records a ~20 s first utterance from the same cause. Switching
  to crepe did not address it.

  Warmup now reports per-stage timings and **warns loudly** when a stage fails,
  and each `Analysed` line carries a stage breakdown plus the pitch method that
  actually ran. If it recurs, the log says which stage and whether crepe fell
  back to pyin. Falsified along the way: warmup probes with 0.5 s while
  utterances run to 10 s, but there is no per-shape retune cost — first touch at
  a new duration costs ~0.05 s over a repeat, scaling linearly.

  A live line now looks like:

  ```
  Analysed 1.71s of speech in 1.55s [stt 1.26s align 0.05s pitch 0.23s
  notes+ustx 0.00s] via crepe/full@cuda
  ```

- **Found by that instrumentation, immediately: cmudict loaded lazily on the
  first utterance.** `notes+ustx` was 0.84 s on the first phrase and 0.00 s on
  the second. The warmup probe is a tone, so whisper returns no words and the
  loop exits before the lyric stage is ever reached — the same "warmup does not
  cover it" shape as the spike, one order of magnitude smaller. `_warmup` now
  has a `lyrics` stage (~1.5 s, japanese mode only) and the first utterance
  measures 0.01 s.
- **The phonemizer is intermittently flaky** — a word occasionally returns the
  `error` sentinel and is retried individually (`_retry_failed_groups`). The
  retry works; the underlying cause is unknown.

---

## Note length in Japanese mode is measured, not set

Worth reading before touching the timing settings, because the obvious knob is
the wrong one.

Live, Teto sang 40–80% longer than the speech she came from (3.36 s of talking →
6.16 s of singing) and sounded lifeless. The cause: **every note was exactly
0.22 s**. `seconds_per_syllable` was tuned when a note was a whole word, but
Japanese mode makes a note one mora, and "understand" is eight of them.

Replacing it with a smaller per-mora tempo fixed the length and *not* the
lifelessness — because a tempo number is the wrong shape of answer. Squeezed
inside their own word, morae come to 30–80 ms each, which is under any floor
short enough to still be singable, so 100% of them were rounded to the same
value at every setting tried. Identical lengths are a metronome.

The room has to come from the pauses: 48% of a typical utterance is silence
between words. Each word's morae are now laid out from its aligned onset to the
**next word's onset**, so they may sing into the gap that follows. Onsets stay
exactly where the aligner measured them — that is what is heard as rhythm — and
only the release is borrowed.

| | uniform 0.22 s | uniform 0.11 s | measured |
|---|---|---|---|
| sung vs spoken | 226% | 117% | **100%** |
| distinct note lengths | 1 | 1 | **7** (0.060–0.181 s) |
| notes pinned at the floor | 26/26 | 26/26 | **3/26** |

The floor is measured from the voicebank rather than guessed: a note shorter
than a sample's preutterance is all consonant run-up and no vowel.
`voicebank.mora_floor` takes the 75th percentile of the oto's preutterance
values — 67 ms for tandoku, whose median is 40 ms and worst case 128 ms.

A/B renders: `out/ab_measured_compare.wav` (0.22 uniform → 0.11 uniform →
measured) and `out/ab_timing_sweep.wav` (a tempo sweep, superseded).

## Japanese mode vs native mode, re-run

The original "Japanese wins" verdict predates every fix in this session, so the
comparison was run again on one utterance ("and it is kind of hard to
understand", 2.91 s). `out/ab_mode_compare.wav`.

| | japanese (tandoku) | native (english) |
|---|---|---|
| notes | **26** | **8** — one per word |
| sung length | 3.06 s (105%) | 2.36 s (81%) |
| note lengths | 0.110 s, all identical | 0.220–0.660 s, varied |
| bank | 358 samples, ja-cv | 2681 samples, en-cvvc |

Native is 3.25× fewer notes because it does not expand syllables into morae at
all, and its note lengths vary because a word's length is its own rather than a
floor. Japanese mode's density (2.22 morae per English syllable) is structural
and cannot be tuned away.

**To switch:** set `voicebank` to `english` and leave `lyric_mode` on `auto` —
auto follows the bank's flavour, so an `en-` bank implies native lyrics. Setting
`lyric_mode: native` while leaving the bank on `tandoku` will not work: a
Japanese CV bank cannot sing English phonemes.

## Note length in Japanese mode: measured, then floored

The floor ended up mattering more than the measurement. Morae sized purely from
the aligner land at 60–80 ms, and this bank's preutterance is 40 ms at the
median but 98 ms at the p90 — so those notes are largely consonant run-up with
little vowel. Judged by ear, **evenly-sung morae that reach their vowel beat an
accurate rhythm made of half-sounded ones**, so `min_mora_seconds` is 0.11
(clearing the p90) and in practice overrides most measured lengths. The
measuring machinery is still there and takes over wherever a word has room.

## The control panel

`--web` serves a rebuilt panel on <http://127.0.0.1:8765/>. Still
dependency-free `http.server`, still generated from the `Config` dataclass, so
a new setting appears without touching the page.

The screen is the utterance, not a form. Everything else is secondary to it.

- **"Now singing" is the page.** The English whisper heard is set large and
  light (38px/300); the Japanese reading sits under it in kana. A `sung` tag
  marks which of the two the bank is actually singing, so a Japanese bank
  singing morae cannot be confused with an English bank singing words — and on
  an English bank the kana line is visibly a caption. Fed by
  `last_source` / `last_kana` / `last_notes`.
- **The melody is drawn.** Under the two lines, an SVG ribbon plots each note's
  MIDI tone across the utterance, aligned to the note chips beneath it and
  drawn on arrival. It is the one piece of decoration in the panel and it is
  made of real data — you can see the octave slips.
- **Five controls on the main screen** (`ESSENTIALS`): microphone, output,
  push-to-talk key, transpose, volume. Everything else — renderer, phonemizer,
  timing, contour, RVC, the log — is behind **Show advanced**, which remembers
  its state in `localStorage`.
- **The model picker sits in the top bar with the bank's icon** and applies
  live: `POST /api/voicebank` saves *and* calls `set_voicebank` on a running
  relay. This is why `voicebank` is not a row in the Mode group — two controls
  for one setting, one live and one needing a restart, is worse than one.
- **Numeric settings are sliders** where a range is meaningful (`RANGES`),
  labelled from `LABELS` in words rather than field names ("Sing into pauses",
  not `pause_borrow`) and shown in the unit people say out loud — seconds-valued
  settings read as ms.

- **Microphone and Output are real dropdowns**, built from `sounddevice` and
  deduplicated by name (Windows lists each device once per host API), ranked by
  the same host-API preference `find_device` uses to resolve them. Because the
  config stores a *substring*, whatever is already saved is kept as an option —
  `"CABLE Input"` does not vanish just because the full name is
  `"CABLE Input (VB-Audio Virtual Cable)"`.
- **Light and dark.** The page follows the system until the toggle is used,
  then remembers the choice in `localStorage`. Tokens are defined on `:root`
  for light, re-declared under `@media (prefers-color-scheme:dark)
  :root:not([data-theme=light])` and again under `:root[data-theme=dark]`.

Glass over light: two large blurred colour fields sit off-centre behind
frosted panels (`backdrop-filter: blur(22px) saturate(150%)` with an inset
top highlight), over a fine SVG grain — glass with no texture behind it reads
as a CSS demo. Crimson is the running/sung colour, violet is the pitch ribbon.

Two traps found while building it, both worth remembering:

- **Never transition the `background` shorthand when the value is a `var()`.**
  It pins to the old value when the variable changes, so the page kept its dark
  ground after switching to light while `html` (untransitioned) updated
  correctly. Transition `background-color`.
- **The `☀`/`☾` code points render as a weak asterisk in the Windows UI font.**
  The theme toggle draws inline SVG instead.
- **A dark page does not give you dark form controls.** The `<select>` popup,
  scrollbars and focus rings are drawn by the browser, so the open dropdown
  list stayed white and unreadable over the dark panel. `color-scheme` on
  `:root` per theme is the fix; `option` also gets an opaque `--solid`
  background, since a translucent list over a blurred page is illegible.

New endpoints: `GET /api/bank-image?bank=KEY` (BMP converted to PNG once via
Pillow, cached in memory) and `POST /api/voicebank`.

## Next steps, roughly in order of value

1. **Judge the sustain.** Morae now ring into the pauses. If that smears words
   together, lower `max_mora_seconds` (0.25) — it caps how far a word can
   spread into the silence after it — rather than raising the floor.
2. **Grow the Japanese vocabulary.** `japanese.NATIVE` holds words that are
   already Japanese (`teto` → てと). Anything not in cmudict and not in that
   table is left as-is and will not sing. Measured: **0 of 246 common English
   words** fall through, so ordinary speech is covered — the gap is names,
   invented words, and Japanese vocabulary beyond the 14 entries in `NATIVE`.
3. **RVC voice conversion** (`mode: "voice"`). **The dependency wall is broken
   and conversion works standalone** — see the section below. What remains is
   wiring it into the pipeline.
4. **Tray app** (`--tray`) exists but has had less testing than `--web`.

---

## RVC voice conversion: working standalone

`.venv-rvc\Scripts\python.exe tools\rvc_probe.py in.wav out.wav` converts audio
into Kasane Teto's voice. Verified: 5.17 s in, 5.16 s out, median F0 90 Hz →
176 Hz (+11.5 semitones, as asked), voiced fraction 40% → 43%. A/B render in
`out/rvc_before_after.wav`.

**How the fairseq wall was got round.** Both PyPI packages (`rvc`, `rvc-python`)
declare `fairseq`, which is sdist-only and needs MSVC — and this machine has no
compiler at all (no `cl.exe`, no vswhere, no VS2022). But fairseq is imported by
exactly **two** files in the `rvc` package, and neither is on the inference
path: a JIT helper, and `load_hubert()` — eight lines that read a fairseq
checkpoint. The synthesiser and pipeline never touch it. So:

- `rvc==0.3.5` is installed with `--no-deps`, and its real dependencies are
  installed separately, minus fairseq/hydra/omegaconf.
- A stub `fairseq` module is injected into `sys.modules` before importing rvc.
  Its `checkpoint_utils.load_model_ensemble_and_task` returns a ContentVec
  encoder loaded through `transformers` instead. The pipeline only ever calls
  `extract_features(source, padding_mask, output_layer=12)` and takes element
  [0] — for v2 it does not even use `final_proj` — so a ~10-line wrapper
  satisfies it.
- `omegaconf==2.0.6` turns out to have a wheel now, so half the old blocker had
  already expired.

**Model facts** (read from the checkpoint, not assumed): v2, `tgt_sr` 40000,
`f0=1`, 200 epochs, 457 tensors, and a 168 MB v2 index.

**Four traps, all of which fail quietly rather than loudly:**

- `faiss.read_index` needs a `str`. Handed a `Path` it raises *inside* the
  pipeline, which swallows the error and converts with **no index at all** —
  you get a worse-sounding result and no warning.
- The pipeline returns **int16-scaled** samples. Writing them as float clips
  everything; divide by 32768 first.
- `rvc.lib.audio.load_audio` decodes via PyAV, and av ≥ 13 rejects its `"rb"`
  mode. Bypassed with a librosa read — the relay will pass a numpy array
  anyway, so that path is dead weight.
- transformers 5 refuses to `torch.load` a `.bin` under torch 2.6
  (CVE-2025-32434), and ContentVec ships no safetensors. `tools/rvc_probe.py`
  converts the weights once into `.cache/contentvec/` (361 MB) rather than
  moving torch off 2.5.1+cu121.

**Performance**, GPU, `is_half=False`, 5.16 s of speech: f0 1.91 s, infer
1.38 s, features 0.14 s — about 3.4 s of real work, i.e. **faster than
realtime**. Model load is 0.6 s; the rest of the first-run cost is the 168 MB
index. F0 is the biggest slice and uses rvc's own crepe; the main venv's
torchcrepe does the same work in roughly a third of the time, so wiring that in
is the obvious next win.

**It is now wired into the app.** `teto_relay/voice.py` holds the converter;
`mode: "voice"` runs a single `convert` worker in place of analyse+render:

```
utau   mic -> whisper -> align -> crepe -> notes -> OpenUtau -> playback
voice  mic -> content encoder -> crepe -> RVC -> playback
```

Voice mode builds no OpenUtau host at all (no CoreCLR, no singer), warms the
voice model and content encoder instead of whisper and the aligner, and reuses
the torchcrepe warmed for pitch — which is where the speed comes from:

| | 5s utterance |
|---|---|
| standalone probe (cold torchcrepe) | 3.4 s — 0.66× realtime |
| in-app, sharing the warm torchcrepe | **1.64 s — 0.35× realtime** |

Warmup is ~16 s (pitch 7.0, voice model 6.7, first conversion 2.5).

**Two more quiet failures found during integration:**

- `get_vc` walks `os.getenv("index_root")` looking for a matching `.index`.
  Unset, that is `os.walk(None)`, which raises "expected str, bytes or
  os.PathLike object, not NoneType" from a traceback that mentions no
  environment variable at all. `voice.py` sets weight/index/rmvpe roots.
- `rvc_f0_method` defaulted to `rmvpe`, whose model was never downloaded, so
  voice mode died on a missing `rmvpe.pt`. The default is now `crepe`, and an
  rmvpe setting with no `rmvpe.pt` beside the model warns and falls back rather
  than failing.

## Thai and Japanese speech (UTAU mode)

A voicebank sings one alphabet, so each source language is routed to the
writing its bank wants. `teto_relay/translit.py`:

| speech | bank | route |
|---|---|---|
| english | japanese | cmudict → morae (unchanged) |
| english | english | as spoken (unchanged) |
| **thai** | **japanese** | romanise → morae (th → en → ja) |
| **thai** | **english** | romanise, sung from explicit X-SAMPA |
| **japanese** | **japanese** | straight to hiragana, kanji read out |
| **japanese** | **english** | romaji, sung from explicit X-SAMPA |

Romanisation is the hinge: the one writing every source can reach and both
banks can be driven from. It is lossy — Thai tone is gone — but a bank with 104
morae was never carrying Thai tone. Set the language in the panel's **Language**
box; the bank decides the rest. Thai needs a **multilingual** speech model, so
the `.en` entries are gone from the list.

Verified: `สวัสดี ครับ` → じ/す/わ/と/ぢ + く/ら/ぷ on a Japanese bank, and
`swatdi` / `khrap` with phonemes `s u w A t u d i` / `k u r A p u` on the
English one. `重音テト` → じゅうおんてと.

Two traps worth keeping:

- **Thai is written without spaces**, so whisper's output must be segmented
  (`pythainlp`, newmm) before any of it can be romanised.
- **Do not palatalise on a back vowel alone.** Only a written `y` glide makes
  youon. Palatalising every a/u/o turned `phom` into ぴょむ and `khopkhun` into
  きょぷきゅん. `ny` is likewise *not* a digraph — matching it as one eats the
  glide and gives ぬ for `nyu` instead of にゅ.

**Untested:** Thai *speech*. There is no Thai voice installed on this machine
(SAPI has only en-US), so the chain is verified from Thai text onward. Whether
whisper's Thai word timings are good enough for the note spans is unknown.

## Latency and quality controls

The settings that trade time for quality are now reachable:

- **Transcription** — `whisper_device` (cpu/cuda) and `whisper_compute_type`
  (int8/float16/float32) were never in the panel and are the two biggest levers
  on how long whisper takes; plus beam width. Note the cuDNN load-order trap
  before moving whisper to cuda.
- **Voice conversion** — reordered so the ones that change the sound come
  first: pitch tracking (crepe accurate, pm fastest), voice likeness (index
  rate), consonant protection, pitch smoothing, dynamics.

## Adding your own voices

`teto_relay/library.py` installs both kinds, validating before keeping:

- **UTAU voicebank** — a `.zip` of the bank folder, POSTed to
  `/api/install/voicebank`. The oto.ini may be nested anywhere in the archive;
  it is found and the bank moved up. Rejected with a reason if there is no
  oto.ini/character.txt, no `.wav` samples, or the name is taken.
- **RVC model** — a `.pth` (and optional `.index`) to `/api/install/rvc`. The
  checkpoint is opened and checked for `weight`/`config`/`sr`; anything else is
  deleted rather than kept, and the panel reports its version, rate and whether
  it is pitched. A valid `.pth` becomes `rvc_model` immediately.

Uploads are the raw file as the body with the filename in the query — no
multipart parser on either end, since we write both sides.

**Zip slip is guarded explicitly**: entries naming `..` or an absolute path are
skipped, because `extractall` alone has not always been enough.

## The panel takes its colour from the voice

Each bank's accent is read from its own icon (`library.accent_colour`): the
most frequent *saturated* colour, ignoring greys and near-blacks, because an
icon is mostly outline and background and tinting by those makes every bank the
same dark grey. Teto reads `#e75455`. It is cached per bank and cleared on
install, so a bank you add brings its own colour with no configuration.

**The `background` trap, third time:** a property whose value is a `var()` and
which is *transitioned* gets pinned to its old colour when the variable changes.
It hit `body` (light/dark), then `button` (accent). Switching the shorthand to
`background-color` was not enough for the button - the transition itself had to
go. Do not transition a colour that comes from a variable.

## Diagnostic tools

```bash
.venv\Scripts\python.exe tools\list_banks.py        # voicebanks and their flavours
.venv\Scripts\python.exe tools\test_pitch.py --selftest
.venv\Scripts\python.exe tools\render_once.py --bank tandoku --play
.venv\Scripts\python.exe -m teto_relay --list-devices
```

`out/` holds the generated `.ustx` and `.wav` for every utterance, trimmed to
the most recent 50 — useful for hearing exactly what was sung.
