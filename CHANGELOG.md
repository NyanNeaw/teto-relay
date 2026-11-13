# Changelog

## 0.4.0 beta (2026-11-14)

A new look, a Studio for whole songs, tunings with effects, and a Market for
sharing what you make.

- **A new look, in three pages: Live, Studio and Tune.** *Live* is talking
  and singing as before, with your voicebank on a card, a lyrics notepad,
  the last phrase she sang, and the talk key in a quick strip. *Studio* is
  for recording and arranging songs. *Tune* is how she sings. The technical
  settings moved into the gear popup. The app opens on the page you left.
  While you record she still isn't sent to CABLE Input.
- **Studio: a playlist with lanes.** Every line you sang is a clip on a lane,
  where you sang it, with its words on it: up to 16 lanes for her voice, plus
  audio lanes. Drag a clip to move it (her singing, the OpenUtau project and
  your guide voice all follow, nothing is sung again); click it to open its
  notes in the piano roll. Drop music or audio files onto audio lanes. A dock
  under the playlist holds the **piano roll**, a **mixer** and the **lyrics**.
  A browser on the left lists your Library and the Market.
- **Sing along to music.** **Add music** (or drop a .wav, .mp3, .flac or .ogg)
  puts your backing track in. Record plays it in your headphones from the
  start, each line lands where you sang it against it, and **mix.wav** is her
  singing over the music. The music is never put into teto.wav or played
  into CABLE Input. Wear headphones, or the microphone records it too.
- **A delay fix, so clips land on the beat.** The timer button in Studio
  opens **Recording delay**: press **Calibrate**, hold one side of your
  headphones against the microphone, and a few clicks measure the delay; or
  type it. New clips are placed that much earlier, and **Shift all clips**
  moves a take you already recorded. Wired headphones keep time; Bluetooth
  ones drift.
- **Projects and recordings.** A *project* is a song: its playlist, music and
  audio clips. A *recording* is a part you sang, kept with the voicebank and
  tuning it was sung with. Drag a recording (or one line of it) onto a
  project, or press its **+** to add it where you sang it. Both can be
  renamed.
- **Another voice for something you sang.** **Voice…** sings a recording or a
  clip again with another voicebank or tuning: the same words, pitch and
  timing. *Copy onto a free lane* stacks the new voice beside the old one.
  Every line remembers its voice, so a song sounds the same after a restart.
- **Tunings, with effects.** The Tune page shows tunings as cards, with
  **12 built in** that sound clearly different: *Just like you*, *Pop
  singer*, *Whisper*, *Metal scream*, *Robot*, *Diva*, *Choir*, *Lo-fi
  radio*, *Kawaii*, *Demon*, *Lullaby* and *Rap*. Four **Character** knobs
  (Grit, Air, Bright, Size) and sections for Voice, Shape, Effects, Singing,
  Pitch and Timing: effects after she sings (octave, bright, drive, growl,
  ring, crush, radio band, air, chorus, reverb), her tone through OpenUtau
  (gender, tension, breath, voicing), and the shape of each note (scoop,
  fall-off, consonant speed, attack, release). **Hear it** sings your last
  phrase again with the change; *Play after every change* does it for you.
  Every knob's number can be clicked to type a value. Save a tuning, export it
  as a small `.tetostyle` file, or import one; a file that isn't a tuning, or
  has a value out of range, is refused with the reason.
- **Sing follows your notes or makes a tune.** *Style* is Speak or Sing, and
  **Sing follows** is *Auto* (your notes when you sing, a made-up tune when
  you talk), *My notes* (always yours, for covers: in your key, moved only by
  whole octaves, so she is in tune with the music) or *Make a tune*. The old
  Cover style is *My notes* now; old settings and presets still load.
- **The Market.** Tunings, Voicebanks, Sounds and Favourites, each with a
  picture, a play button for its demo, and **Get**. Make an account to
  **Share** your own tunings, voicebanks and sounds, like things, and see
  each creator's page. A shared voicebank installs in one click; a sound
  drags straight onto an audio lane. Things with a price link to the
  creator's own shop: the Market never takes payment. Kasane Teto's
  voicebank is only ever a link to her official site. Shared things are
  listed at once; anything that breaks the rules is taken down, and reports
  from several people hide it until then.
- **Voicebanks from .rar and .7z, or a folder.** Add one as a .zip, .rar,
  .7z or .uar, or point at a folder. Voicebanks, Thai ones especially, are
  often shared as .rar; 7-Zip is used if it is installed, otherwise the tar
  built into Windows 10 and 11.
- **Lyrics follow the song.** With the song's lyrics pasted in, speech
  recognition used to be told only the end of a long song (it reads just the
  last part of what it is given). It is now given the part being sung, and
  moves on as each line is placed.
- **Recording hears a quiet microphone.** A headset microphone that came in
  very quietly used to cut a verse into scraps. The level now starts just
  above the room and follows your voice.
- **Fixed: closing after "reopening the microphone" on PCs with little
  memory.** The 1.6 GB syllable-timing model was loaded in the background
  just as the microphone opened, even when it was not downloaded yet and
  would not be used. On a 6 GB laptop the PC stalled, the app took the stall
  for a dead microphone, kept reopening it, and closed. Now that model is only
  loaded ahead when it is already downloaded and memory is free (otherwise
  when it is first needed). A busy moment no longer counts as a dead
  microphone, and a microphone that really stops is cut off at once instead
  of waited on.
- **A crash log.** If the app ever closes by itself, `teto-relay-crash.log`
  (beside `teto-relay.log`) says where it was.

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
