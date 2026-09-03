# Redirect every cache and temp directory to D: before installing anything.
#
# C: has under 2 GB free. pip's cache, TMP/TEMP, torch's model cache and the
# HuggingFace cache all default to C:, and a torch install would fill it and
# fail partway. Dot-source this before any pip or python command:
#
#     . .\tools\rvc_env.ps1

$root = "D:\Claude\teto-relay\.cache"
New-Item -ItemType Directory -Force -Path $root, "$root\pip", "$root\tmp", "$root\torch", "$root\hf" | Out-Null

$env:PIP_CACHE_DIR = "$root\pip"
$env:TMP = "$root\tmp"
$env:TEMP = "$root\tmp"
$env:TORCH_HOME = "$root\torch"
$env:HF_HOME = "$root\hf"
$env:HUGGINGFACE_HUB_CACHE = "$root\hf"

Write-Host "Caches redirected to $root"
Write-Host ("  free on D: {0:N1} GB" -f ((Get-PSDrive D).Free / 1GB))
Write-Host ("  free on C: {0:N1} GB" -f ((Get-PSDrive C).Free / 1GB))
