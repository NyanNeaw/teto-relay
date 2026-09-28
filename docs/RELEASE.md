# Building and testing a release

## Build (on Windows)

You need Python 3.11 (from python.org, with the `py` launcher) and, for the
installer, [Inno Setup 6](https://jrsoftware.org/isdl.php). From the
repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WithGpu
```

`-WithGpu` bundles torch and torchaudio (CUDA 12.1) and torchcrepe, adding
about 2.5 GB.
Leave it off for a small CPU-only build, which uses pyin for pitch and
whisper's own word timings.

The script creates `.venv-build` (and reuses it on later runs; delete it for a
clean build), installs the requirements, **runs the
tests** (a failure stops the build), builds with PyInstaller, runs the built
`TetoRelayConsole.exe --version` and `--doctor` as a smoke test, and writes:

| File | What it is |
|---|---|
| `dist\TetoRelay\` | The app folder: `TetoRelay.exe` (panel, no console) and `TetoRelayConsole.exe` (command line). |
| `dist\TetoRelay-<version>-portable.zip` | That folder plus `portable.txt`, `SETUP.md`, `README.md`, `CHANGELOG.md`. |
| `dist\TetoRelay-<version>-setup.exe` | The per-user installer (only if Inno Setup is installed). |

The version comes from `teto_relay/__init__.py`. Bump it and add a
`CHANGELOG.md` entry before building a release.

**Status:** the PyInstaller spec has been built and run on Linux as a check
(the frozen app starts, `--doctor` passes the dictionary checks, the panel
works in a browser, portable mode keeps data beside the exe). That check found
and fixed missing `cmudict` metadata. `build.ps1`, the Windows build and the
installer have **not** been run yet. Expect to fix a hidden import or two on
the first Windows build; the console exe's traceback names the module.

## Manual test checklist (real hardware)

Run on the target PC on 2026-09-27/28: Windows 10, GTX 1060 6 GB, 8 GB RAM,
OpenUtau in `D:\Work\OpenUtau`, VB-Cable, Teto english/tandoku/renzokubeta
and a Miku English bank. `[x]` passed, `[!]` failed and was fixed (commit
named), `[ ]` not tested yet. The log and `latency.csv` are in the data
folder.

### Install and first run
- [ ] Installer runs without an admin prompt and warns if the .NET 8 Desktop Runtime is missing. *(Built with Inno Setup 6; not yet run on a clean PC.)*
- [!] `build.ps1 -WithGpu`: tests pass in the build venv, `TetoRelayConsole.exe --version` and `--doctor` run. The portable zip step filled the disk twice (a 3.4 GB staging copy plus Compress-Archive); the zip is now streamed from the app folder (`c682f65`) and passes an integrity check. `pythainlp` was missing from requirements.txt, so the exe had no Thai (`b215fd8`).
- [!] Size: the app was 4.2 GB (installer 1.6 GB, zip 2.6 GB), 3.5 GB of it torch's CUDA libraries for crepe, the aligner and RVC. `-WithGpu` now uses torch's CPU build and NVIDIA's cuBLAS for whisper only (`9c49969`): app 1.6 GB, installer 511 MB, zip 856 MB. `--selftest` in the built app with the user's config: whisper (Thai model) on CUDA, crepe tiny and the aligner on the CPU, Thai pronunciation. `-CudaTorch` keeps the old build for RVC on the GPU.
- [!] Double-clicking `TetoRelay.exe` opened no window: Edge inherited PyInstaller's DLL directory (`b1f79b3`). Verified in the built app: the window opens. `TetoRelay.exe` with options but no `--web` ran an invisible headless relay; it now always opens the panel.
- [x] `TetoRelay.exe` with the user's config: the panel opens, Start loads OpenUtau, the Miku singer and whisper, and switching Miku -> tandoku -> Miku changes the singer. [ ] Singing a phrase from the microphone in the exe: user to confirm.
- [x] **Check setup** reports OpenUtau, .NET 8, VB-Cable, the mic, the voicebanks and the GPU correctly. With OpenUtau pointed at a missing folder it says `[FAIL] OpenUtau: OpenUtau.Core.dll is not in ...` with the fix.
- [x] Setup → OpenUtau folder / Voicebank folder: when set, they are used. When empty, the usual places are searched and listed in the message; this PC keeps both in non-standard folders (`D:\Work\OpenUtau`, `D:\Claude`), so they are not found - as documented.
- [x] Quit in the panel stops everything: the relay stops within 0.6 s, the process exits (exit code 0) and Windows shows the microphone released.

### The default pipeline (UTAU)
- [x] Hold the key, speak, release: Teto sings the phrase. Routed to CABLE Input, the audio was recorded back from CABLE Output.
- [x] A `Latency ...` line is logged per phrase and `latency.csv` gets a row. Typical `total` for a 2-3 s phrase: **~1.9 s** with whisper on the GPU (`int8`, beam 1), **3.1-4.3 s** with the old CPU `base`/beam 5 setting. The biggest stage is **render** (OpenUtau, 1.3-2.1 s: phonemize ~0.7 s + synth ~0.9 s).
- [x] `lead_silence` is 0.00 on every phrase: the leading-silence trim works with real WORLDLINE output (its positions are absolute).
- [x] Numbers ("I have 2 cats at 5:30"), "I'm", and loanwords on a Japanese bank are sung, not silent.
- [!] Switching voicebank in the panel while running: only the lyrics and pitch target followed; OpenUtau kept the first bank's voice, and a bank installed while running was unknown. Fixed (`bfa6ed2`); after a switch the output now equals a fresh render with that bank (defoko → tandoku → miku → a romaji bank).
- [x] Other people's voicebanks (`e803584`, `b8eb9e2`): copies of Defoko rearranged as a romaji bank, pitch-suffixed with prefix.map, character.yaml only, no character file, nested in an author folder, and a Shift-JIS zip all install and sing through OpenUtau. Before: the romaji bank sang silence, the two without character.txt played tones, the Shift-JIS zip installed with garbled names that matched no sample.
- [ ] Unplug the USB mic while running: the panel shows the microphone banner; note whether it recovers on its own; Stop and Start must bring it back.
- [x] OpenUtau not found: plain tones play, and the panel banner and Check setup say why. (Tested by pointing `openutau_dir` at a missing folder rather than renaming the install.)
- [ ] Tray mode (`TetoRelayConsole.exe --tray` or `pythonw -m teto_relay --tray`): red icon when live; with a broken setup, amber icon, the reason in the menu, and Retry / Open log work.

- [x] Changing a start-only setting in the panel while running (push-to-talk key, microphone, whisper model...) restarts the relay by itself: "restarting..." for ~3 s, then listening again with the new value (`8ab3883`). Only the OpenUtau folder still asks to reopen the program.
- [x] Double voice is an On/Off switch; On layers the voice on the phrases that are sung, not the spoken ones.

### Thai and the app window (added 2026-09-28)
- [x] Thai speech on Teto tandoku, Teto English and Miku (20 test phrases, whisper medium as judge): letter error 0.873 -> 0.589, 0.925 -> 0.660, 0.910 -> 0.832; no missing samples on Teto (`b215fd8`). See NOTES.md "Thai".
- [ ] Thai spoken by the user into the mic, heard by the user.
- [x] The panel opens as an Edge app window; Edge reports it installable (manifest, icons, service worker), apart from Playwright's private window (`e011695`).
- [ ] Opened from the packaged exe; installed from Edge's menu.

### Latency options
- [x] `persistent_output: true`: audio plays correctly; the `output` stage is ~0.02 s with it and without, so it gains nothing measurable on this PC.
- [!] `whisper_device: cuda`, `whisper_compute_type: float16`: the GTX 1060 (Pascal) has no fast float16 and every phrase failed. Now falls back to `int8` with a warning (`e149020`). With `int8`, beam 1: `asr` 0.9 s → 0.16 s, and crepe on CUDA still works after whisper on CUDA.

### Singing options
- [x] `singing_style: sung`: notes in a key, vibrato measurable **in OpenUtau's output** (a 25-cent vibrato moves the sung pitch ±20 cents), the last note held.
- [!] `legato`: touching notes lost their phonemes (18 morae → 7) because the relay grouped them (`e69b847`); now on by default. Connected phrases also lost a tick to rounding and cut hard (`f8c0098`).
- [x] Voice mode with `voice_streaming: true`, after fixing the gate (`d37d996`): with the default crepe + index a 300 ms block takes ~800 ms on the GTX 1060 and falls behind; `rvc_f0_method: pm`, `rvc_index_rate: 0` keeps up (~250 ms a block, ~0.8 s delay). Tested with recorded audio; the user did not listen to it.

### Tuning (added during testing)
- [x] Scored on the user's own recordings with `tools/tuning_eval.py`; see "Hardware testing and tuning log" in NOTES.md. English word error 0.37 → ~0.06-0.10, Japanese kana error 0.63 → ~0.2-0.3; dropouts (singing while Teto is silent) 5.5% → 0.3%.

### Shutdown
- [x] Stop and Quit leave no microphone or output stream open (the Windows microphone consent store shows the app's use ended).
- [ ] Ctrl+C in the console exits within a few seconds, even with the panel open in a browser.
