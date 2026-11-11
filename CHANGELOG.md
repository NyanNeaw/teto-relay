# Changelog

## 0.4.0 beta

- **Two modes: Live and Studio.** A switch in the top bar. *Live* is the main
  screen as before: talk, and she sings into your output. *Studio* is the
  recording workspace: the take you are recording, or a saved one, sits in
  the page above the settings instead of in a sheet floating over it.
  Pressing Record in Live takes you to Studio. The app opens in the mode you
  left it in. While a take records she still isn't sent to CABLE Input.
- **A timeline in Studio.** A take is shown on a timeline: a time ruler, a
  music row and a voice row. Every line you sang is a clip on the voice row,
  where you sang it, with its words on it. **Drag a clip left or right** to
  put it somewhere else: her singing (.wav), the OpenUtau project and your
  guide voice all follow, and nothing has to be sung again. A clip stops at
  0:00 and never lands on top of another. **Click a clip** to open its notes
  in the small OpenUtau underneath. Play starts from the playhead (click the
  ruler to move it), and the timeline scrolls sideways and zooms. Lines show
  up as clips while you record, too. The take list, deleting, the downloads
  and the DAW tile are where they were.
- **A music track.** In Studio, press **Add music** (or drop the file on the
  window) to put your song's backing track on the music row: .wav, .mp3,
  .flac or .ogg. It is drawn as a waveform, always from 0:00. Press Record
  and it plays in your headphones from the start, so you can sing along,
  and each line lands where you sang it against the music. Afterwards Play
  plays the music and her singing together, and there is a new download,
  **mix.wav**: her singing over the music. The music is never put into
  teto.wav, and it is never played into CABLE Input. Wear headphones while
  you record, or the microphone records the music too. Studio now opens on a
  **New take** page, where the music is added before recording; the list of
  takes is behind the clock button.
- **A delay fix, so clips land on the beat.** When you sing along, the music
  reaches your ears a little late and your voice reaches the app a little
  late, so every clip landed slightly behind. The timer button in Studio
  opens **Recording delay**: press **Calibrate**, hold one side of your
  headphones against the microphone, and a few clicks measure the delay and
  save it; or type the number of milliseconds yourself. New clips are placed
  that much earlier. **Shift all clips** moves a whole take you already
  recorded. Bluetooth headphones have a delay that keeps changing: wired
  ones keep time.
- **Presets: a way of singing you can save and share.** A **Preset** list
  sits at the top of the quick settings, with four built in that sound
  clearly different: *Just like you*, *Pop singer*, *Robot* and *Diva*. Pick
  one and its settings go on at once. **Save as preset** keeps the way she
  sings now under a name; **Export** makes a small `.tetostyle` file to
  give to someone; **Import**, or dropping the file on the window, reads one.
  A preset only holds settings that change how she sings (style, key, melody,
  vibrato, held last note, doubled voice): never a device, a folder, the talk
  key or the language. A file that isn't a preset, or has a value out of
  range, is refused with the reason and changes nothing.
- **Recording hears a quiet microphone.** While a take records, a line is
  told from a pause by how loud it is, and that level was fixed. A headset
  microphone that came in very quietly sat under it most of the time, so a
  whole verse was saved as two or three scraps. The level now starts just
  above the room and follows your voice; with a microphone at a normal level
  it ends up where it always was.

## 0.3.2 beta (2026-11-04)

- **A small OpenUtau in the take sheet.** Open a line of a take and her notes
  are on a piano roll: drag a note up or down to change its pitch, sideways
  to move it, drag its edge to make it longer or shorter, Alt+click (or the
  scissors) to split it, join it with the next, double-click to change its
  lyric, delete it. Undo steps back; **Sing again** sings the edited line with
  the voice that sang it. Her tuning is drawn over the notes and follows
  them: moved up, a note's bends move up; made longer or shorter, they
  stretch or squeeze to fit, and her loudness and breath go with it. A note
  made longer eats into the start of the next one, so the rest of the line
  stays on the beat.
- **Delete takes from the list**, one at a time or all at once, and delete a
  single line from a take. Questions are asked in the app's own popup.
- **Downloads sit beside the DAW tile**, as three buttons: her singing
  (.wav), the OpenUtau project, and everything in a .zip. (Your own voice is
  still in the .zip.)
- **Song lyrics work for Thai too.** Thai is heard by a model that can't be
  given the lyrics, so the line it heard is now looked up in them, allowing
  for misheard letters, and sung as the lyric's words, each where you sang
  it. Talking that isn't in the song is left as heard.
- **No more jumping an octave mid-line.** The pitch tracker sometimes heard a
  syllable an octave low (or high), so she sang it far below you and then
  leapt up for the next. A stretch an octave away from its neighbour, joined
  by a jump no voice makes, is now moved back; a real sung leap is kept.
- **Stop says it's stopping.** The button spins and reads "Stopping" until
  she has really stopped (it used to go on saying "Stop"), and ending a take
  turns the record button into a spinning ring with "saving take..." until
  it is saved. The mini player shows the same.
- **Hear myself**: a switch on the main screen (and a headphones button on
  the mini player) to hear her in your headphones as well.
- **Recording a take no longer sings into your microphone.** While a take
  records she isn't sent to CABLE Input; turn on Hear myself to hear her.

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
