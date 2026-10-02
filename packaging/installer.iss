; Inno Setup script for Teto Relay. Built by packaging\build.ps1, which passes
; the version and the PyInstaller output folder:
;
;   ISCC.exe /DAppVersion=0.2.0 /DSourceDir=...\dist\TetoRelay packaging\installer.iss
;
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Teto Relay.
; Settings, logs and models live in %LOCALAPPDATA%\TetoRelay and are kept on
; uninstall, so reinstalling or upgrading does not lose them.
;
; The wizard: Welcome - folder - extras - Ready - (downloads) - install -
; Finish. Every extra is ticked: a desktop shortcut, and the models Teto
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

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nSpeak or sing into your microphone and Teto sings it back.%n%nClose other applications before continuing.
FinishedLabel=Teto Relay is installed. Hold F8, speak, let go - she sings it back.%n%nThe guide shows the one-time setup (OpenUtau, VB-Cable and a voicebank).

[Tasks]
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

[Icons]
; The panel window's taskbar identity (teto_relay.window.APP_ID): a pinned
; button and these shortcuts are then one app on the taskbar.
Name: "{group}\Teto Relay"; Filename: "{app}\TetoRelay.exe"; AppUserModelID: "KasaneTeto.TetoRelay"
Name: "{group}\How to use Teto Relay"; Filename: "{app}\how-to-use.html"
Name: "{group}\Teto Relay - check setup"; Filename: "{cmd}"; Parameters: "/k ""{app}\TetoRelayConsole.exe"" --doctor"
Name: "{group}\Uninstall Teto Relay"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Teto Relay"; Filename: "{app}\TetoRelay.exe"; AppUserModelID: "KasaneTeto.TetoRelay"; Tasks: desktopicon

[Run]
; The Finish page's two ticked boxes.
Filename: "{app}\how-to-use.html"; Description: "Open the how-to-use guide"; Flags: postinstall shellexec nowait skipifsilent
Filename: "{app}\TetoRelay.exe"; Description: "Launch Teto Relay"; Flags: postinstall nowait skipifsilent

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

var
  DownloadPage: TDownloadWizardPage;
  { Downloaded into Setup's temporary folder under a name of their own,
    moved into place once the program is installed. }
  PendingTemp: array of String;
  PendingTarget: array of String;

{ Teto Relay drives OpenUtau's engine through the .NET 8 Desktop Runtime. It
  is not bundled (it is Microsoft's to distribute), so the installer checks
  for it and says where to get it. }
function HasDotNet8Desktop(): Boolean;
var
  FindRec: TFindRec;
  Base: String;
begin
  Result := False;
  Base := ExpandConstant('{commonpf64}\dotnet\shared\Microsoft.WindowsDesktop.App\');
  if FindFirst(Base + '8.*', FindRec) then
  begin
    Result := True;
    FindClose(FindRec);
  end;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading models',
    'Teto Relay''s speech and timing models are being downloaded. You can skip this: ' +
    'whatever is missing downloads the first time it is needed.', nil);
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

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;
  DownloadPage.Clear;
  SetArrayLength(PendingTemp, 0);
  SetArrayLength(PendingTarget, 0);
  QueueModels;
  if GetArrayLength(PendingTemp) = 0 then
    Exit;
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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  PlaceDownloads;
  if (not HasDotNet8Desktop()) and (not WizardSilent()) then
    MsgBox('Teto Relay needs the .NET 8 Desktop Runtime (x64) to sing through OpenUtau, ' +
           'and it was not found.' + #13#10#13#10 +
           'Download it from https://dotnet.microsoft.com/download/dotnet/8.0 ' +
           '(".NET Desktop Runtime 8", Windows x64), then start Teto Relay.' + #13#10#13#10 +
           'The how-to-use guide (it opens at the end) lists the rest: OpenUtau, ' +
           'VB-Cable and a voicebank.',
           mbInformation, MB_OK);
end;
