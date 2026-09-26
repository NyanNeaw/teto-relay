# Changelog

Teto Relay uses [semantic versioning](https://semver.org/): the minor number
goes up for new features, the patch number for fixes.

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
- Rendered audio no longer starts with 0.2–0.5 s of silence.
- Numbers are sung instead of being silent; small kana and multi-word lyrics
  in Japanese mode no longer produce silent or garbled notes.
- One accented or non-Latin word no longer disables word alignment for the
  whole phrase.
- Quick push-to-talk presses no longer lose a phrase.
- The panel no longer overwrites the speech model setting when saving.
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
