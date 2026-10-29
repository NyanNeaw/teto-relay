; Inno Setup script for Teto Relay. Built by packaging\build.ps1, which passes
; the version and the PyInstaller output folder:
;
;   ISCC.exe /DAppVersion=0.2.0 /DSourceDir=...\dist\TetoRelay packaging\installer.iss
;
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Teto Relay.
; Settings, logs and models live in %LOCALAPPDATA%\TetoRelay and are kept on
; uninstall, so reinstalling or upgrading does not lose them.
;
; The wizard: Welcome - folder - extras - Ready - (OpenUtau, then the
; models) - install - Finish. Every extra is ticked: a desktop shortcut, and the models Teto
; Relay would otherwise fetch the first time it needs them, downloaded here
; with a progress bar and checked against their SHA-256. A model already in
; place is not fetched again; one that fails to download is left for the app.
; The Finish page opens the how-to-use guide and starts Teto Relay.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\TetoRelay"
#endif

[Setup]
AppId={{6F1C2B8E-7A43-4C1B-9F2E-5B7D3A9E1C44}
AppName=Teto Relay
AppVersion={#AppVersion}
AppPublisher=Teto Relay
DefaultDirName={autopf}\Teto Relay
DefaultGroupName=Teto Relay
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=TetoRelay-{#AppVersion}-setup
SetupIconFile=teto_relay.ico
UninstallDisplayIcon={app}\TetoRelay.exe
; ultra64 over max: a bigger dictionary finds more in the large DLLs.
Compression=lzma2/ultra64
; In its own (64-bit) process: inside the 32-bit compiler, ultra64 with
; several threads ran out of memory.
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2
SolidCompression=yes
WizardStyle=modern
; OpenUtau comes as a .zip: the full extractor reads it (is7z.dll).
ArchiveExtraction=full
; Every box starts ticked on every run, so running Setup again after a
; download failed (offline) offers OpenUtau and the models again.
UsePreviousTasks=no

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nSpeak or sing into your microphone and Teto sings it back.%n%nClose other applications before continuing.
FinishedLabel=Teto Relay is installed. Hold F8, speak, let go - she sings it back.%n%nStill to get yourself: a voicebank, and VB-Cable for Discord and OBS. The guide shows where.

[Tasks]
; Shown only when missing: what Teto Relay cannot sing without. (OpenUtau
; carries its own .NET runtime, which Teto Relay starts: nothing else needed.)
Name: "openutau"; Description: "&OpenUtau 0.1.565, the singing engine (135 MB)"; GroupDescription: "Needed to sing, and not on this PC yet:"; Check: NeedsOpenUtau
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "speechmodel"; Description: "&Speech model for English and Japanese (150 MB)"; GroupDescription: "Download now, so the first start is quick (needs internet; anything left out downloads the first time it is needed):"
Name: "thaimodel"; Description: "&Thai speech model (480 MB) - if you speak Thai"; GroupDescription: "Download now, so the first start is quick (needs internet; anything left out downloads the first time it is needed):"
Name: "timingmodel"; Description: "&Word timing model (1.2 GB) - sharper timing for Japanese and English"; GroupDescription: "Download now, so the first start is quick (needs internet; anything left out downloads the first time it is needed):"

[Files]
; Not data\ or portable.txt: a portable copy run from the build folder keeps
; its settings, recordings and downloaded models there.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "\data\*,\portable.txt"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\docs\how-to-use.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\SETUP.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
; Written at the end of setup (WriteInstalledState), so not in the install log.
Type: files; Name: "{app}\installed.js"

[Icons]
; The panel window's taskbar identity (teto_relay.window.APP_ID): a pinned
; button and these shortcuts are then one app on the taskbar.
Name: "{group}\Teto Relay"; Filename: "{app}\TetoRelay.exe"; AppUserModelID: "KasaneTeto.TetoRelay"
Name: "{group}\How to use Teto Relay"; Filename: "{app}\how-to-use.html"
Name: "{group}\Teto Relay - check setup"; Filename: "{cmd}"; Parameters: "/k ""{app}\TetoRelayConsole.exe"" --doctor"
Name: "{group}\Uninstall Teto Relay"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Teto Relay"; Filename: "{app}\TetoRelay.exe"; AppUserModelID: "KasaneTeto.TetoRelay"; Tasks: desktopicon

[Run]
; The Finish page's ticked boxes.
Filename: "{app}\how-to-use.html"; Description: "Open the how-to-use guide"; Flags: postinstall shellexec nowait skipifsilent
Filename: "{app}\TetoRelay.exe"; Description: "Launch Teto Relay"; Flags: postinstall nowait skipifsilent
; VB-Cable is a driver whose terms leave installing it to VB-Audio: its page, when it is missing.
Filename: "https://vb-audio.com/Cable/"; Description: "Get VB-Cable, so Discord and OBS can hear her"; Flags: postinstall shellexec nowait skipifsilent; Check: NeedsVBCable

[Code]
const
  { Where the installed app keeps its data (teto_relay.paths.data_dir). }
  DataFolder = '{localappdata}\TetoRelay';
  { teto_relay.stt.whisper_source: models\faster-whisper-<name>, the
    default model (Config.whisper_model). }
  WhisperUrl = 'https://huggingface.co/Systran/faster-whisper-base/resolve/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66/';
  { teto_relay.thai_asr.MODEL, pinned the same. }
  ThaiUrl = 'https://huggingface.co/wannaphong/typhoon-asr-realtime-onnx/resolve/04af3e7b6d822cb2807bc539301669d4f84691e7/';
  { torchaudio's MMS_FA checkpoint, where teto_relay.align._checkpoint looks. }
  TimingUrl = 'https://dl.fbaipublicfiles.com/mms/torchaudio/ctc_alignment_mling_uroman/model.pt';
  { The OpenUtau Teto Relay drives, pinned: it reaches into OpenUtau's
    internals, so a newer one may not work. Hash as GitHub publishes it. }
  OpenUtauUrl = 'https://github.com/openutau/OpenUtau/releases/download/0.1.565/OpenUtau-win-x64.zip';
  OpenUtauSha = '6697e84469574d0d9abf3c0e1927475005e64114fb01efa302b7d82d778f439d';
  { One of the places teto_relay.locate looks for it. }
  OpenUtauFolder = '{localappdata}\Programs\OpenUtau';

var
  DownloadPage: TDownloadWizardPage;
  ExtractionPage: TExtractionWizardPage;
  OpenUtauKnown: Integer;  { 0 not looked yet, 1 found, 2 missing }
  { Downloaded into Setup's temporary folder under a name of their own,
    moved into place once the program is installed. }
  PendingTemp: array of String;
  PendingTarget: array of String;

function HasCoreDll(const Folder: String): Boolean;
begin
  Result := (Folder <> '') and FileExists(AddBackslash(Folder) + 'OpenUtau.Core.dll');
end;

{ The OpenUtau folder Teto Relay was already given, from its settings. }
function ConfiguredOpenUtau(): String;
var
  Text: AnsiString;
  Json: String;
  At, Stop: Integer;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{localappdata}\TetoRelay\config.json'), Text) then
    Exit;
  Json := String(Text);
  At := Pos('"openutau_dir"', Json);
  if At = 0 then
    Exit;
  Json := Copy(Json, At + 14, 1024);
  At := Pos('"', Json);
  if At = 0 then
    Exit;
  Json := Copy(Json, At + 1, 1024);
  Stop := Pos('"', Json);
  if Stop = 0 then
    Exit;
  Result := Copy(Json, 1, Stop - 1);
  StringChangeEx(Result, '\\', '\', True);
end;

{ The same places as teto_relay.locate: the usual install folders, then each
  drive's top and one folder down (D:\Work\OpenUtau). }
function FindOpenUtau(): Boolean;
var
  Places: array of String;
  I, Letter: Integer;
  Root: String;
  FindRec: TFindRec;
begin
  Result := True;
  SetArrayLength(Places, 10);
  Places[0] := ConfiguredOpenUtau();
  Places[1] := GetEnv('OPENUTAU_DIR');
  Places[2] := ExpandConstant('{localappdata}\OpenUtau\current');
  Places[3] := ExpandConstant('{localappdata}\OpenUtau');
  Places[4] := ExpandConstant(OpenUtauFolder);
  Places[5] := ExpandConstant('{commonpf64}\OpenUtau');
  Places[6] := ExpandConstant('{commonpf32}\OpenUtau');
  Places[7] := GetEnv('USERPROFILE') + '\OpenUtau';
  Places[8] := GetEnv('USERPROFILE') + '\Desktop\OpenUtau';
  Places[9] := GetEnv('USERPROFILE') + '\Downloads\OpenUtau';
  for I := 0 to GetArrayLength(Places) - 1 do
    if HasCoreDll(Places[I]) then
      Exit;
  for Letter := Ord('C') to Ord('Z') do
  begin
    Root := Chr(Letter) + ':\';
    if not DirExists(Root) then
      Continue;
    if HasCoreDll(Root + 'OpenUtau') then
      Exit;
    if FindFirst(Root + '*', FindRec) then
    begin
      try
        repeat
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and (Copy(FindRec.Name, 1, 1) <> '$')
             and (Copy(FindRec.Name, 1, 1) <> '.') and HasCoreDll(Root + FindRec.Name + '\OpenUtau') then
            Exit;
        until not FindNext(FindRec);
      finally
        FindClose(FindRec);
      end;
    end;
  end;
  Result := False;
end;

function NeedsOpenUtau(): Boolean;
begin
  if OpenUtauKnown = 0 then
    if FindOpenUtau() then OpenUtauKnown := 1 else OpenUtauKnown := 2;
  Result := OpenUtauKnown = 2;
end;

function NeedsVBCable(): Boolean;
var
  FindRec: TFindRec;
begin
  Result := not FindFirst(ExpandConstant('{sys}\drivers\vbaudio_cable*.sys'), FindRec);
  if not Result then
    FindClose(FindRec);
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading',
    'Getting what Teto Relay needs. A model you skip downloads the first time it is needed.', nil);
  ExtractionPage := CreateExtractionPage('Unpacking OpenUtau', 'Putting the singing engine in place.', nil);
end;

{ Queue one file, unless it is already in place and whole. }
procedure Want(const Url, TempName, Target, Sha256: String);
var
  N: Integer;
begin
  if FileExists(Target) and (GetSHA256OfFile(Target) = Sha256) then
    Exit;
  DownloadPage.Add(Url, TempName, Sha256);
  N := GetArrayLength(PendingTemp);
  SetArrayLength(PendingTemp, N + 1);
  SetArrayLength(PendingTarget, N + 1);
  PendingTemp[N] := TempName;
  PendingTarget[N] := Target;
end;

procedure QueueModels;
var
  Models: String;
begin
  Models := ExpandConstant(DataFolder) + '\models\';
  if WizardIsTaskSelected('speechmodel') then
  begin
    Want(WhisperUrl + 'model.bin', 'whisper-model.bin', Models + 'faster-whisper-base\model.bin',
         'd01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9');
    Want(WhisperUrl + 'config.json', 'whisper-config.json', Models + 'faster-whisper-base\config.json',
         '56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a');
    Want(WhisperUrl + 'tokenizer.json', 'whisper-tokenizer.json', Models + 'faster-whisper-base\tokenizer.json',
         'fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab');
    Want(WhisperUrl + 'vocabulary.txt', 'whisper-vocabulary.txt', Models + 'faster-whisper-base\vocabulary.txt',
         '34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913');
  end;
  if WizardIsTaskSelected('thaimodel') then
  begin
    Want(ThaiUrl + 'encoder-fastconformer-quran-ar.onnx', 'thai-encoder.onnx',
         Models + 'typhoon-asr-realtime\encoder-fastconformer-quran-ar.onnx',
         '9573e8224cbad1be622b779e780cccc5638a4dd19097fd6c7258d8ec1c21cf91');
    Want(ThaiUrl + 'decoder_joint-fastconformer-quran-ar.onnx', 'thai-decoder.onnx',
         Models + 'typhoon-asr-realtime\decoder_joint-fastconformer-quran-ar.onnx',
         'c445e567e1133506c1ff5e4722ee61b8adf830243e0449de3b796a3205ea1e4a');
    Want(ThaiUrl + 'tokenizer/vocab.json', 'thai-vocab.json',
         Models + 'typhoon-asr-realtime\vocab.json',
         'a7277aeb7b08f6c8a1ed7838b55d204cf6766dbe583d690bbb9fb0d42de0c2b1');
  end;
  if WizardIsTaskSelected('timingmodel') then
    Want(TimingUrl, 'timing-model.pt',
         ExpandConstant(DataFolder) + '\.cache\torch\hub\checkpoints\model.pt',
         '20ef12963ab4924bef49ac4fc7f58ad5da2ee43b2c11bc8c853c9b90ecdbc680');
end;

{ Unpack OpenUtau, if it was downloaded. Failing leaves Setup going: the
  Finish message and Check setup in the app say what is still missing. }
procedure InstallPrerequisites;
begin
  if FileExists(ExpandConstant('{tmp}\openutau.zip')) then
  begin
    ExtractionPage.Clear;
    ExtractionPage.Add(ExpandConstant('{tmp}\openutau.zip'), ExpandConstant(OpenUtauFolder), True);
    ExtractionPage.Show;
    try
      try
        ExtractionPage.Extract;
        OpenUtauKnown := 1;
      except
        if not ExtractionPage.AbortedByUser then
          SuppressibleMsgBox('OpenUtau could not be unpacked:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
            'Get it from https://github.com/stakira/OpenUtau/releases and unzip it anywhere.',
            mbInformation, MB_OK, IDOK);
      end;
    finally
      ExtractionPage.Hide;
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;
  SetArrayLength(PendingTemp, 0);
  SetArrayLength(PendingTarget, 0);
  { OpenUtau first, and on its own: Teto Relay cannot sing without it, and a
    download stops at its first failure - queued behind the 1.2 GB timing
    model, a dropped connection there used to cost OpenUtau too. }
  if WizardIsTaskSelected('openutau') then
  begin
    DownloadPage.Clear;
    DownloadPage.Add(OpenUtauUrl, 'openutau.zip', OpenUtauSha);
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        if not DownloadPage.AbortedByUser then
          SuppressibleMsgBox('OpenUtau could not be downloaded:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
            'Setup will continue. Run Setup again once you are online: it offers OpenUtau again.',
            mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
  DownloadPage.Clear;
  QueueModels;
  if GetArrayLength(PendingTemp) = 0 then
  begin
    InstallPrerequisites;
    Exit;
  end;
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      { Cancelled, offline or a bad file: install anyway. What did arrive is
        still moved into place; the app fetches the rest when it needs it. }
      if not DownloadPage.AbortedByUser then
        SuppressibleMsgBox('Some models could not be downloaded:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
          'Setup will continue. Teto Relay downloads what is missing the first time it needs it.',
          mbInformation, MB_OK, IDOK);
    end;
  finally
    DownloadPage.Hide;
  end;
  InstallPrerequisites;
end;

{ Downloads are moved, not copied: Setup's temporary folder and the data
  folder are usually on the same drive, and the timing model alone is 1.2 GB. }
procedure PlaceDownloads;
var
  I: Integer;
  Source: String;
begin
  for I := 0 to GetArrayLength(PendingTemp) - 1 do
  begin
    Source := ExpandConstant('{tmp}\') + PendingTemp[I];
    if not FileExists(Source) then
      Continue;
    ForceDirectories(ExtractFileDir(PendingTarget[I]));
    if FileExists(PendingTarget[I]) then
      DeleteFile(PendingTarget[I]);
    if not RenameFile(Source, PendingTarget[I]) then
      if not FileCopy(Source, PendingTarget[I], False) then
        Log('Could not place ' + PendingTarget[I]);
  end;
end;

function JsBool(const Value: Boolean): String;
begin
  if Value then Result := 'true' else Result := 'false';
end;

{ What is on this PC now, for the guide next to the program: the rows it
  covers say "Installed" instead of offering a download. }
procedure WriteInstalledState;
begin
  SaveStringToFile(ExpandConstant('{app}\installed.js'),
    'window.tetoInstalled = {openutau: ' + JsBool(not NeedsOpenUtau()) +
    ', vbcable: ' + JsBool(not NeedsVBCable()) + '};' + #13#10, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  PlaceDownloads;
  WriteInstalledState;
  if (OpenUtauKnown = 2) and (not WizardSilent()) then
    MsgBox('OpenUtau, the singing engine, is not on this PC yet, so Teto cannot sing.' + #13#10#13#10 +
           'Run this Setup again once you are online: it downloads OpenUtau for you. ' +
           'Or get it from https://github.com/stakira/OpenUtau/releases and unzip it anywhere.',
           mbInformation, MB_OK);
end;
