<#
.SYNOPSIS
    Builds Teto Relay for Windows: the app folder, a portable zip and an installer.

.DESCRIPTION
    Run from the repository root in PowerShell:

        powershell -ExecutionPolicy Bypass -File packaging\build.ps1
        powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WithGpu

    Steps:
      1. Makes a clean build virtualenv (.venv-build) with Python 3.11.
      2. Installs the requirements, PyInstaller and, with -WithGpu, torch
         (CPU) plus torchcrepe, and NVIDIA's cuBLAS for whisper on the GPU.
      3. Runs the test suite. A failing test stops the build.
      4. Builds dist\TetoRelay\ with PyInstaller.
      5. Makes dist\TetoRelay-<version>-portable.zip (with portable.txt, so
         settings stay beside the exe).
      6. If Inno Setup 6 is installed, makes dist\TetoRelay-<version>-setup.exe.

    Nothing here needs admin rights. It does not install OpenUtau, .NET,
    VB-Cable or voicebanks - those are the user's (see docs\SETUP.md).

.PARAMETER WithGpu
    The recommended build. Whisper runs on an NVIDIA GPU (cuBLAS 12.1 from
    NVIDIA's pip wheel, the version torch 2.5.1+cu121 shipped); crepe pitch tracking, the syllable aligner and Thai
    pronunciation run on torch's CPU build. Measured: crepe tiny on the CPU
    takes 0.28 s for a 3 s phrase against 0.34 s for crepe full on a GTX 1060,
    within 10 cents of it; the aligner is as fast on the CPU. The CUDA build of
    torch added ~1.7 GB for no audible gain. Without -WithGpu there is no torch
    at all: pyin pitch, whisper's own timings, rule-based Thai.

.PARAMETER CudaTorch
    With -WithGpu, use torch's CUDA 12.1 build instead (the app grows to about
    4 GB). Only RVC voice conversion needs it, to convert on the GPU.

.PARAMETER Python
    The Python launcher command to use. Default: "py -3.11".

.PARAMETER SkipTests
    Build even if you have not run the tests. Not recommended.
#>
param(
    [switch]$WithGpu,
    [switch]$CudaTorch,
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
    # As an array: "py -3.11".Split() gave -3.11 as a lone string, which
    # PowerShell 5.1 splatted as nothing - and "py -m venv" was lost, so a
    # clean build started an interactive Python instead of making the venv.
    $Parts = @($Python -split " " | Where-Object { $_ })
    $Exe = $Parts[0]
    $PyArgs = @($Parts | Select-Object -Skip 1)
    & $Exe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create a virtualenv with '$Python'. Install Python 3.11 from python.org (tick 'py launcher')." }
}
& $Py -m pip install --upgrade pip | Out-Null

# ---------------------------------------------------------------- 2. deps
Step "Installing dependencies"
& $Py -m pip install -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { Fail "pip could not install requirements.txt" }
if ($WithGpu) {
    if ($CudaTorch) {
        & $Py -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
        if ($LASTEXITCODE -ne 0) { Fail "pip could not install torch (CUDA 12.1)" }
    } else {
        # The +cpu builds by name: "torch==2.5.1" is satisfied by a CUDA torch
        # left in the venv by an earlier -CudaTorch build, which would then be
        # bundled whole.
        & $Py -m pip install torch==2.5.1+cpu torchaudio==2.5.1+cpu --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) { Fail "pip could not install torch (CPU)" }
        # Whisper's GPU library: the cuBLAS torch 2.5.1+cu121 shipped. Not
        # cuDNN: CTranslate2 transcribed the Thai test set identically, as
        # fast, with every cuDNN DLL hidden - it never loaded one beyond the
        # front end - and cuDNN was 1.1 GB of the build.
        & $Py -m pip install nvidia-cublas-cu12==12.1.3.1 --no-deps
        if ($LASTEXITCODE -ne 0) { Fail "pip could not install cuBLAS" }
        # A cuDNN left by an earlier build would be bundled.
        & $Py -m pip uninstall -y nvidia-cudnn-cu12 2>$null | Out-Null
    }
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
# PyInstaller empties dist\TetoRelay, and a portable copy run from there keeps
# its settings and models in dist\TetoRelay\data (and portable.txt): a rebuild
# wiped them, and the user had to set everything up again.
$App = Join-Path $Root "dist\TetoRelay"
$Kept = Join-Path $Root ".kept-from-dist"  # outside build\, which --clean may empty
if (Test-Path $Kept) { Remove-Item $Kept -Recurse -Force }
foreach ($Name in @("data", "portable.txt")) {
    $Item = Join-Path $App $Name
    if (Test-Path $Item) {
        New-Item -ItemType Directory -Force $Kept | Out-Null
        Move-Item $Item (Join-Path $Kept $Name)
    }
}
& $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\teto_relay.spec
$Built = $LASTEXITCODE
if (Test-Path $Kept) {
    Get-ChildItem $Kept | ForEach-Object { Move-Item $_.FullName (Join-Path $App $_.Name) -Force }
}
if ($Built -ne 0) { Fail "PyInstaller failed (see the output above)" }
$Version = (Get-Content (Join-Path $Root "build\version.txt")).Trim()

Step "Smoke test: TetoRelayConsole.exe --version and --doctor"
& (Join-Path $App "TetoRelayConsole.exe") --version
if ($LASTEXITCODE -ne 0) { Fail "the built program does not start" }
# The doctor's exit code is 1 when something on this PC is missing (no
# voicebank, no VB-Cable...), which is fine for a build machine; a crash is not.
& (Join-Path $App "TetoRelayConsole.exe") --doctor
if ($LASTEXITCODE -gt 1) { Fail "the built program crashed running --doctor" }
# Each model once, in the built program: a DLL or data file PyInstaller left
# out only shows when a model is used. Models come from the download cache,
# so on a new build PC this may fetch them; a failure is reported, not fatal.
& (Join-Path $App "TetoRelayConsole.exe") --selftest
if ($LASTEXITCODE -ne 0) { Write-Host "`nWARNING: --selftest reported a failed stage (see above)." -ForegroundColor Yellow }

# ---------------------------------------------------------------- 5. zip
Step "Making the portable zip"
$PortableName = "TetoRelay-$Version-portable"
$Zip = Join-Path $Root "dist\$PortableName.zip"
# Streamed from the app folder: a staging copy needed the app's size again in
# free space, and filled the disk on the test PC.
& $Py (Join-Path $Root "packaging\portable_zip.py") $App $Zip $PortableName `
    (Join-Path $Root "docs\SETUP.md") (Join-Path $Root "README.md") (Join-Path $Root "CHANGELOG.md")
if ($LASTEXITCODE -ne 0) { Fail "could not write the portable zip" }

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
