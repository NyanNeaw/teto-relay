; Inno Setup script for Teto Relay. Built by packaging\build.ps1, which passes
; the version and the PyInstaller output folder:
;
;   ISCC.exe /DAppVersion=0.2.0 /DSourceDir=...\dist\TetoRelay packaging\installer.iss
;
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Teto Relay.
; Settings, logs and models live in %LOCALAPPDATA%\TetoRelay and are kept on
; uninstall, so reinstalling or upgrading does not lose them.
;
; UNTESTED: not yet compiled or run on Windows.

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
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=TetoRelay-{#AppVersion}-setup
SetupIconFile=teto_relay.ico
UninstallDisplayIcon={app}\TetoRelay.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\docs\SETUP.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Teto Relay"; Filename: "{app}\TetoRelay.exe"
Name: "{group}\Teto Relay - check setup"; Filename: "{cmd}"; Parameters: "/k ""{app}\TetoRelayConsole.exe"" --doctor"
Name: "{group}\Teto Relay setup guide"; Filename: "{app}\SETUP.md"
Name: "{group}\Uninstall Teto Relay"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Teto Relay"; Filename: "{app}\TetoRelay.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TetoRelay.exe"; Description: "Start Teto Relay"; Flags: nowait postinstall skipifsilent

[Code]
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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not HasDotNet8Desktop()) and (not WizardSilent()) then
    MsgBox('Teto Relay needs the .NET 8 Desktop Runtime (x64) to sing through OpenUtau, ' +
           'and it was not found.' + #13#10#13#10 +
           'Download it from https://dotnet.microsoft.com/download/dotnet/8.0 ' +
           '(".NET Desktop Runtime 8", Windows x64), then start Teto Relay.' + #13#10#13#10 +
           'You will also need VB-Cable, OpenUtau and a UTAU voicebank - see the setup guide ' +
           'in the Start menu. "Teto Relay - check setup" tells you what is missing.',
           mbInformation, MB_OK);
end;
