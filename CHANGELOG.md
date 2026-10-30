# Changelog

## 0.3.1 beta (2026-10-30)

- **A calmer panel.** Far less text: the main settings carry a small icon
  instead of a line of help (point at one for its tip), the style switch
  says *Speak* / *Sing* instead of *Speech* / *Sung*, and buttons, groups
  and the take sheet have icons too. Drop-downs show names, not codes
  ("Hold the talk key", not "ptt").
- **A new look**, with a friend's touches: a logo that fills with the
  voice's colour while she runs, a status pill, and an opening where the
  app's logo pops in, sings, and lands in the top bar.
- **A mini player.** Minimize the window and a small player stays on top in
  the corner, Spotify-style: Start/Stop, Record, and a hold-to-talk button.
- **Adding a voicebank**: a popup shows the upload's progress, then
  "ready" with a button to use her straight away. Or drop the .zip anywhere
  on the window. Big voicebanks no longer freeze the app window (a 436 MB
  bank took minutes, or never finished; now about 6 s).
- **Japanese voicebanks keep your rhythm on English.** Each English word
  becomes several Japanese syllables, and each used to push the next word
  later, up to a second behind by the end of a sentence. Words now start
  where you said them (median 162 ms late → on time, worst 1.1 s → 0.18 s),
  and a phrase lasts about as long as you took to say it.
- **VCV voicebanks** no longer sing longer than others, and find a sample
  for every syllable.
- **The talk key works in any keyboard language.** After switching Windows
  to Thai, holding it did nothing.
- **The window no longer shows "Not Responding"** for a while when it opens.
- **Record a take, for song covers.** Press Record next to Start, sing your
  lines over the song (no key needed: each pause ends a line), press it
  again. Every phrase lands where you sang it, so it lines up with the
  music. Download Teto's singing (.wav), an OpenUtau
  project (.ustx) to fix notes or swap the voicebank, your own voice (.wav)
  as a guide, or all three in a .zip. A take where nothing was sung isn't
  kept, and the panel says so.
- **The take sheet.** The round record button opens a sheet: the lines
  appear as you sing, then the take shows on a piano roll you can play.
  **Fix a word she got wrong**: click the line, see her notes with their
  lyrics, type the right word and she sings that line again, with the same
  timing and melody. **Drag the take into FL Studio** (or any DAW) straight
  from the sheet.
- **Works with the newest OpenUtau (0.1.576), and .NET no longer has to be
  installed.** OpenUtau carries its own .NET, and Teto Relay now uses it, so
  0.1.576 (a .NET 10 build) sings instead of failing every phrase with
  "property is read-only", and the installer no longer downloads .NET 8.
- **Microphones that only work at their own sample rate** (the "Invalid
  sample rate [PaErrorCode -9997]" banner) are now recorded at that rate and
  converted.
- **Running the installer again after the internet dropped** offers OpenUtau
  again, ticked. OpenUtau is now downloaded first and on its own, so a model
  that fails to download can't take it down too.
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
