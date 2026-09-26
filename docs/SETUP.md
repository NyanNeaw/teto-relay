# Setting up Teto Relay

This guide is for the packaged app: the installer (`TetoRelay-<version>-setup.exe`)
or the portable zip. To run from the source code instead, see the README.

## 1. What you need first

Teto Relay doesn't include these, because they come from other projects and
you may already have them. Install each one once.

| What | Why | Where |
|---|---|---|
| **Windows 10 or 11, 64-bit** | The engine and audio routing are Windows-only. | |
| **.NET 8 Desktop Runtime (x64)** | Teto Relay runs OpenUtau's singing engine through it. | <https://dotnet.microsoft.com/download/dotnet/8.0>, under ".NET Desktop Runtime 8" |
| **OpenUtau** | Its engine does the actual singing. You never need to open it. | <https://github.com/stakira/OpenUtau/releases> |
| **VB-Cable** | The virtual cable Teto sings into, which Discord or OBS then uses as a microphone. | <https://vb-audio.com/Cable/> (restart after installing) |
| **A Kasane Teto UTAU voicebank** | Her voice. A folder with an `oto.ini` and `.wav` files. | The official Teto site. The "tandoku" (単独音) bank works best. |
| **An NVIDIA GPU** | Optional. Speeds up pitch tracking and word timing. | Keep the driver up to date. |

## 2. Install Teto Relay

**Installer:** run `TetoRelay-<version>-setup.exe`. It installs for your user
only (no admin prompt) and adds Start menu entries. If the .NET 8 Desktop
Runtime is missing, it tells you at the end.

**Portable zip:** unzip it anywhere you can write to (not `Program Files`).
The `portable.txt` inside means settings, logs and downloaded models are kept
in a `data` folder next to `TetoRelay.exe`, so the whole folder can be moved
or deleted in one go.

## 3. First start

1. Start **Teto Relay**. Your browser opens the control panel
   (<http://127.0.0.1:8765/>). If it doesn't, open that address yourself.
2. Press **Check setup** (below the settings). Every line marked `[FAIL]` says
   what to do; fix those first. `[WARN]` lines are optional.
3. Open **Show all settings → Setup**:
   - **OpenUtau folder**: the folder that contains `OpenUtau.exe`. Leave it
     empty and the usual install places are searched.
   - **Voicebank folder**: where your voicebanks are. Left empty, these are
     searched in order and the first that has a bank is used: the
     `voicebanks` folder in Teto Relay's data folder, OpenUtau's `Singers`,
     `Documents\OpenUtau\Singers`, `Documents\UTAU\voice`. With none, it is
     the `voicebanks` folder. You can also drop a bank's `.zip` onto **Add a
     voicebank** in the panel.
   - Changing either folder takes effect when you press Stop and Start; the
     OpenUtau folder only after quitting and restarting Teto Relay.
4. Pick your **Microphone** and set **Output** to **CABLE Input**, then press
   **Save settings**.
5. Press **Start**. The first start downloads the speech model (about 150 MB)
   and, with a GPU, the word-timing model (about 1.2 GB), so give it a few
   minutes. Later starts take seconds.
6. Hold **F8**, say something, let go. Teto sings it a couple of seconds later.

To hear her in **Discord**, **OBS** or anything else, pick **CABLE Output** as
the microphone in that app.

## 4. Where things are

| | Installed | Portable |
|---|---|---|
| Program | `%LOCALAPPDATA%\Programs\Teto Relay` | the folder you unzipped |
| Settings (`config.json`), log, `latency.csv`, models | `%LOCALAPPDATA%\TetoRelay` | `data\` next to `TetoRelay.exe` |

Uninstalling leaves your settings and models where they are. Delete
`%LOCALAPPDATA%\TetoRelay` too if you want everything gone.

## 5. Troubleshooting

Start with **Check setup** in the panel, or run **Teto Relay - check setup**
from the Start menu (`TetoRelayConsole.exe --doctor`). It lists everything
missing, with the fix.

| What you see | What to do |
|---|---|
| Plain beeps instead of Teto | OpenUtau could not start. Check setup names the reason, usually a missing .NET 8 Desktop Runtime or a wrong OpenUtau folder. |
| "No output device matching 'CABLE Input'" | Install VB-Cable and restart Windows, or choose another Output. |
| "The microphone is not available" banner | Plug the mic in, or close any app using it in exclusive mode. Teto Relay keeps retrying; if a re-plugged USB mic doesn't come back, press Stop and Start. |
| Teto hears herself and loops | Your Microphone is set to *CABLE Output*. Set it to your real mic. |
| Nothing when you hold F8 | Another app may be taking the key. Change **Push-to-talk key**. |
| A word comes out silent | It isn't in the dictionary. Create `pronunciations.json` in the data folder with your fixes (see the README); they are added to the built-in ones. |
| It's slow | See the `Latency` lines in the log, or `latency.csv` in the data folder. They show which stage takes the time. A GPU and **Listen on: cuda** help most. |
| "The control panel could not use port 8765 ... Another program is using it" | Another program has that port. Close it, or start with `TetoRelayConsole.exe --web --port 8766`. (Starting Teto Relay twice just opens the panel that is already running.) |

The log (`teto-relay.log`, in the data folder) has the details of anything
that went wrong. In tray mode (`TetoRelayConsole.exe --tray`), **Open log** in
the tray menu opens it.

## 6. Options worth knowing

- **Singing (experimental)**: *Singing style: sung* puts what you say into a
  key, adds vibrato to long notes and holds the last one. It sounds more like
  singing and less like speaking. *Connect syllables* joins up the syllables
  of each word on Japanese banks.
- **Voice engine**: converts your own voice to Teto's timbre with an RVC
  model instead of singing through a voicebank. *Convert while I talk* makes
  it real-time.

These are new and haven't been tested much. Turn them off again if they sound
wrong.
