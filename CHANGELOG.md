# Changelog

## 0.3.1 (in progress)

- **Record a take, for song covers.** Press Record next to Start, sing your
  lines over the song, press it again. Every phrase lands where you sang it,
  so it lines up with the music. Download Teto's singing (.wav), an OpenUtau
  project (.ustx) to fix notes or swap the voicebank, your own voice (.wav)
  as a guide, or all three in a .zip.
- **Teto answers sooner.** About 0.6 s was lost on every phrase inside
  OpenUtau, and pitch is now measured while the words are being heard: on
  the same recordings, 3.4 s → 2.4 s from letting go of the key to her voice.
- **Pitch is worked out while you're still talking**, so less is left to do
  when you let go: 2.2 s → 2.0 s more on the same recordings.
- **No more 6–10 s wait** on the first phrase after switching language or
  voicebank: the timing model loads in the background after Start.

## 0.3.0 beta (first public release)

- Talk or sing into your mic, and Kasane Teto sings it back through any UTAU
  voicebank, live, into VB-Cable for Discord, OBS and games.
- Hears English, ไทย (Typhoon ASR) and 日本語; any voicebank sings all three.
- **Speech** follows your voice exactly; **Sung** puts it in a key with
  vibrato. **Double voice** layers her on the lines you sing. **Song lyrics**
  helps it hear a song you're singing.
- Add voicebanks by dropping a `.zip` in, and rename them.
- The control panel opens in its own window, with light and dark themes.
- A Windows installer that also gets .NET 8, OpenUtau and the speech models
  when they're missing, and a portable zip.
- A visual [how-to-use guide](https://nyanneaw.github.io/teto-relay/how-to-use.html).
