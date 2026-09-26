<#
.SYNOPSIS
    Builds Teto Relay for Windows: the app folder, a portable zip and an installer.

.DESCRIPTION
    Run from the repository root in PowerShell:

        powershell -ExecutionPolicy Bypass -File packaging\build.ps1
        powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WithGpu

    Steps:
      1. Makes a clean build virtualenv (.venv-build) with Python 3.11.
      2. Installs the requirements, PyInstaller and, with -WithGpu, the CUDA
         build of torch plus torchcrepe (adds about 2.5 GB).
      3. Runs the test suite. A failing test stops the build.
      4. Builds dist\TetoRelay\ with PyInstaller.
      5. Makes dist\TetoRelay-<version>-portable.zip (with portable.txt, so
         settings stay beside the exe).
      6. If Inno Setup 6 is installed, makes dist\TetoRelay-<version>-setup.exe.

    Nothing here needs admin rights. It does not install OpenUtau, .NET,
    VB-Cable or voicebanks - those are the user's (see docs\SETUP.md).

.PARAMETER WithGpu
    Include torch (CUDA 12.1), torchaudio and torchcrepe for GPU pitch tracking
    and word alignment. Without it the app uses pyin and whisper's timings.

.PARAMETER Python
    The Python launcher command to use. Default: "py -3.11".

.PARAMETER SkipTests
    Build even if you have not run the tests. Not recommended.
#>
param(
    [switch]$WithGpu,
    [string]$Python = "py -3.11",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nBUILD FAILED: $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 1. venv
Step "Creating the build environment (.venv-build)"
$Venv = Join-Path $Root ".venv-build"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) {
    $Exe, $PyArgs = $Python.Split(" ")
    & $Exe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create a virtualenv with '$Python'. Install Python 3.11 from python.org (tick 'py launcher')." }
}
& $Py -m pip install --upgrade pip | Out-Null

# ---------------------------------------------------------------- 2. deps
Step "Installing dependencies"
& $Py -m pip install -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { Fail "pip could not install requirements.txt" }
if ($WithGpu) {
    & $Py -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    if ($LASTEXITCODE -ne 0) { Fail "pip could not install torch (CUDA 12.1)" }
    & $Py -m pip install -r requirements-gpu.txt
    if ($LASTEXITCODE -ne 0) { Fail "pip could not install requirements-gpu.txt" }
}

# ---------------------------------------------------------------- 3. tests
if (-not $SkipTests) {
    Step "Running the tests"
    & $Py -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { Fail "the tests failed - fix them before building" }
}

# ---------------------------------------------------------------- 4. exe
Step "Building dist\TetoRelay with PyInstaller"
& $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\teto_relay.spec
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed (see the output above)" }
$App = Join-Path $Root "dist\TetoRelay"
$Version = (Get-Content (Join-Path $Root "build\version.txt")).Trim()

Step "Smoke test: TetoRelayConsole.exe --version and --doctor"
& (Join-Path $App "TetoRelayConsole.exe") --version
if ($LASTEXITCODE -ne 0) { Fail "the built program does not start" }
# The doctor's exit code is 1 when something on this PC is missing (no
# voicebank, no VB-Cable...), which is fine for a build machine; a crash is not.
& (Join-Path $App "TetoRelayConsole.exe") --doctor
if ($LASTEXITCODE -gt 1) { Fail "the built program crashed running --doctor" }

# ---------------------------------------------------------------- 5. zip
Step "Making the portable zip"
$PortableName = "TetoRelay-$Version-portable"
$Staging = Join-Path $Root "dist\$PortableName"
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
Copy-Item $App $Staging -Recurse
Set-Content -Path (Join-Path $Staging "portable.txt") -Value "Settings, logs and downloaded models are kept in the data folder next to this file. Delete this file to use %LOCALAPPDATA%\TetoRelay instead."
Copy-Item (Join-Path $Root "docs\SETUP.md") (Join-Path $Staging "SETUP.md")
Copy-Item (Join-Path $Root "README.md") (Join-Path $Staging "README.md")
Copy-Item (Join-Path $Root "CHANGELOG.md") (Join-Path $Staging "CHANGELOG.md")
$Zip = Join-Path $Root "dist\$PortableName.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path $Staging -DestinationPath $Zip
Remove-Item $Staging -Recurse -Force

# ---------------------------------------------------------------- 6. installer
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Iscc) {
    Step "Making the installer with Inno Setup"
    & $Iscc "/DAppVersion=$Version" "/DSourceDir=$App" "/O$(Join-Path $Root 'dist')" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup could not build the installer" }
} else {
    Write-Host "`nInno Setup 6 not found - skipping the installer. Get it from https://jrsoftware.org/isdl.php" -ForegroundColor Yellow
}

Step "Done"
Get-ChildItem (Join-Path $Root "dist") -File | ForEach-Object {
    "{0,-45} {1,8:N0} MB" -f $_.Name, ($_.Length / 1MB)
}
Write-Host "App folder: $App"
