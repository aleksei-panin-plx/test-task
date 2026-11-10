# CardForge installer for Windows (stage 7, user 2026-11-10 «В этапе 7»). NOT VERIFIED on a Windows
# machine yet: written and reviewed on macOS. On Windows the core uses RapidOCR (config [ocr] auto)
# and CPU-only torch (uv.lock, pytorch-cpu index).
#   double-click installer\install.bat, or: powershell -ExecutionPolicy Bypass -File installer\install.ps1 [-Data ...]
param(
  [string]$HomeDir = "$env:USERPROFILE\.cardforge",
  [string]$Data = "",
  [ValidateSet("auto", "none", "claude", "codex", "both")][string]$Plugin = "auto"
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

$Src = Split-Path -Parent $PSScriptRoot
if ($Data) { $Data = (Resolve-Path $Data).Path }  # before Set-Location below
# Assets shipped inside the folder (double-click install.bat): data archive and model weights.
if (-not $Data) { $Found = Get-ChildItem "$Src\assets\cardforge-data-*.tar.gz" -ErrorAction SilentlyContinue | Select-Object -Last 1; if ($Found) { $Data = $Found.FullName } }
$Joined = ""
if (-not $Data) {
  # git clone (user 2026-11-10): the archive is split under GitHub's 100 MB file limit (data-parts\)
  $Part0 = Get-ChildItem "$Src\data-parts\cardforge-data-*.tar.gz.part-00" -ErrorAction SilentlyContinue | Select-Object -Last 1
  if ($Part0) {
    $Base = $Part0.FullName -replace '\.part-00$', ''
    New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
    $Joined = Join-Path $HomeDir (Split-Path $Base -Leaf)
    Say "joining $(Split-Path $Base -Leaf) from data-parts"
    $Out = [IO.File]::Create($Joined)
    try {
      foreach ($P in (Get-ChildItem "$Base.part-*" | Sort-Object Name)) {
        $In = [IO.File]::OpenRead($P.FullName); try { $In.CopyTo($Out) } finally { $In.Close() }
      }
    } finally { $Out.Close() }
    if (Test-Path "$Base.sha256") {
      $Want = ((Get-Content "$Base.sha256" -Raw).Trim() -split '\s+')[0]
      $Got = (Get-FileHash $Joined -Algorithm SHA256).Hash.ToLower()
      if ($Want -ne $Got) { Remove-Item $Joined; throw "data archive from data-parts is damaged (sha256 $Got != $Want): download the repository again" }
    }
    $Data = $Joined
  }
}
$Weights = if (Test-Path "$Src\assets\models") { "$Src\assets\models" } else { "" }
if (-not $Data -and -not (Test-Path "$HomeDir\current\dataset")) { throw "no data archive: put cardforge-data-*.tar.gz into the assets folder" }
if ($Plugin -eq "auto") {
  $C = [bool](Get-Command claude -ErrorAction SilentlyContinue); $X = [bool](Get-Command codex -ErrorAction SilentlyContinue)
  $Plugin = if ($C -and $X) { "both" } elseif ($C) { "claude" } elseif ($X) { "codex" } else { throw "neither Claude Code (claude) nor Codex (codex) found" }
}
$Version = (Select-String -Path "$Src\pyproject.toml" -Pattern '^version\s*=\s*"(.+)"').Matches[0].Groups[1].Value
$Drive = (Get-Item $env:USERPROFILE).PSDrive
if (($Drive.Free / 1GB) -lt 6) { throw "need 6 GB free disk" }
Say "CardForge $Version -> $HomeDir"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "installing uv (astral.sh)"
  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv python install 3.12

$Target = "$HomeDir\versions\$Version"
Say "copying the core to $Target"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
robocopy $Src $Target /E /XD .venv dataset runs assets data-parts __pycache__ /XF *.bin *.onnx | Out-Null
if (Test-Path "$HomeDir\current") { Remove-Item "$HomeDir\current" -Force -Recurse }
New-Item -ItemType Junction -Path "$HomeDir\current" -Target $Target | Out-Null
Set-Location $Target

Say "installing dependencies (uv.lock)"
uv sync --frozen --python 3.12 --extra analysis --extra evaluation
Say "model weights"
if ($Weights) { .\.venv\Scripts\cardforge.exe fetch-assets --root . --from $Weights } else { .\.venv\Scripts\cardforge.exe fetch-assets --root . }

if ($Data) { Say "unpacking $Data"; tar -xzf $Data -C $Target; if ($Joined) { Remove-Item $Joined } }

$EnvFile = "$HomeDir\.env"
$Key = $env:OPENROUTER_API_KEY
if (-not $Key -and -not (Test-Path $EnvFile)) {
  $Secure = Read-Host "OpenRouter key (empty = plans only)" -AsSecureString
  $Key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure))
}
if ($Key) { Set-Content -Path $EnvFile -Value "OPENROUTER_API_KEY=$Key" -NoNewline }
if (Test-Path $EnvFile) { Copy-Item $EnvFile "$Target\.env" -Force }

# MCP launcher: Windows runs the .cmd; the installed plugin's .mcp.json points to it.
$Bin = "$HomeDir\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Copy-Item "$Target\plugin\bin\cardforge-mcp.cmd" "$Bin\cardforge-mcp.cmd" -Force
$Cmd = ("$Bin\cardforge-mcp.cmd" -replace '\\','\\')
Set-Content -Path "$Target\plugin\.mcp.json" -Value ('{"mcpServers":{"cardforge":{"command":"' + $Cmd + '","args":[]}}}')
Set-Content -Path "$Target\plugin\.mcp.codex.json" -Value ('{"mcpServers":{"cardforge":{"command":"' + $Cmd + '","args":[],"startup_timeout_sec":60,"tool_timeout_sec":600}}}')
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Bin", "User") }

if ($Plugin -in @("claude", "both")) { claude plugin marketplace add "$HomeDir\current"; claude plugin install cardforge@cardforge }
if ($Plugin -in @("codex", "both")) { codex plugin marketplace add "$HomeDir\current"; codex plugin add cardforge@cardforge }

Say "doctor"
.\.venv\Scripts\cardforge.exe doctor --root .
