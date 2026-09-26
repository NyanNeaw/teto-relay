# Building and testing a release

## Build (on Windows)

You need Python 3.11 (from python.org, with the `py` launcher) and, for the
installer, [Inno Setup 6](https://jrsoftware.org/isdl.php). From the
repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WithGpu
```

`-WithGpu` bundles torch (CUDA 12.1) and torchcrepe, adding about 2.5 GB.
Leave it off for a small CPU-only build, which uses pyin for pitch and
whisper's own word timings.

The script makes a clean `.venv-build`, installs the requirements, **runs the
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

None of these can be tested without Windows, audio devices, OpenUtau and a
GPU, so they're untested. Go through them on the target PC before calling a
build a release. The log and `latency.csv` are in the data folder.

### Install and first run
- [ ] Installer runs without an admin prompt and warns if the .NET 8 Desktop Runtime is missing.
- [ ] Portable zip runs from a normal folder; `data\` appears next to the exe.
- [ ] Double-clicking `TetoRelay.exe` opens the panel in the browser; double-clicking again just shows the same panel.
- [ ] **Check setup** reports OpenUtau, .NET 8, VB-Cable, the mic, the voicebanks and the GPU correctly, and each `[FAIL]` fix works.
- [ ] Setup → OpenUtau folder / Voicebank folder, when left empty, find your install; when set, are used.
- [ ] Quit in the panel stops everything (no `TetoRelay.exe` left in Task Manager).

### The default pipeline (UTAU)
- [ ] Hold F8, speak, release: Teto sings the phrase through CABLE Input, and Discord/OBS hears it on CABLE Output.
- [ ] A `Latency ...` line is logged per phrase, and `latency.csv` gets a row. Note the `total` for a typical 2 s phrase: ______ s.
- [ ] `lead_silence` in the latency line is near 0 (the leading-silence trim works with real WORLDLINE output). If it is 0.2–0.5 s, WORLDLINE's positions are relative after all; report it.
- [ ] Numbers ("I have 2 cats at 5:30"), "I'm", and a loanword on a Japanese bank are sung, not silent.
- [ ] Switching voicebank in the panel while running takes effect on the next phrase.
- [ ] Unplug the USB mic while running: the panel shows the microphone banner; plug it back in: it recovers on its own.
- [ ] Rename the OpenUtau folder and start: plain tones play, and the panel banner and Check setup say why.
- [ ] Tray mode (`TetoRelayConsole.exe --tray` or `pythonw -m teto_relay --tray`): red icon when live; with a broken setup, amber icon, the reason in the menu, and Retry / Open log work.

### Latency options
- [ ] `persistent_output: true`: audio still plays correctly; compare the `output` stage with it off.
- [ ] `whisper_device: cuda`, `whisper_compute_type: float16`, `beam_size: 1`: the relay still starts (the cuDNN load-order trap), and `asr` in the latency line drops.

### Singing options (off by default)
- [ ] `singing_style: sung`: notes sound in a key, long notes have audible vibrato **in OpenUtau's output** (not only the tone renderer), and the last note is held.
- [ ] `legato: true` on the tandoku bank: words sound joined up and **no phonemes go missing** (touching notes collapsed the hosted phonemizer in early tests; watch the log for "no sample" / missing phonemes).
- [ ] Voice mode with `voice_streaming: true`: you hear yourself in Teto's voice while still talking; no "falling behind" warning at the default 300 ms blocks; no clicks at block edges. Note the delay: ______ s.

### Shutdown
- [ ] Stop and Quit leave no microphone or output stream open (Windows privacy indicator goes off).
- [ ] Ctrl+C in the console exits within a few seconds, even with the panel open in a browser.
