# CardForge Light installer for Windows. NOT VERIFIED on a Windows machine yet: written and reviewed on
# macOS (same steps as install.sh). Light plans only (G1): no data archive, no model weights, no API key.
#   double-click installer\install.bat, or: powershell -ExecutionPolicy Bypass -File installer\install.ps1 [-Plugin claude]
param(
  [string]$HomeDir = "$env:USERPROFILE\.cardforge-lite",
  [ValidateSet("auto", "none", "claude", "codex", "both")][string]$Plugin = "auto"
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

$Src = Split-Path -Parent $PSScriptRoot
# The folder must be complete: OneDrive "files on demand" not downloaded yet look like missing files here.
$CoreFiles = @("src\cardforge\__init__.py", "src\cardforge\cli.py", "src\cardforge\mcp\server.py", "pyproject.toml", "uv.lock", "plugin\.mcp.json", "prompts\planner\card_rules.md", "knowledge\catalog\vocabulary.yaml")
function First-Missing($Dir) { foreach ($F in $CoreFiles) { if (-not (Test-Path (Join-Path $Dir $F))) { return $F } }; return "" }
function Src-Count($Dir) { @("src", "prompts", "knowledge" | ForEach-Object { Get-ChildItem (Join-Path $Dir $_) -Recurse -File -ErrorAction SilentlyContinue } | Where-Object { $_.FullName -notmatch '__pycache__' }).Count }
$Missing = First-Missing $Src
if ($Missing) { throw "the CardForge Light folder is incomplete: $Missing is missing ($Src). Copy the whole cardforge-lite folder or clone the repository again (OneDrive: make the folder 'Always keep on this device' first)" }
if ($Plugin -eq "auto") {
  $C = [bool](Get-Command claude -ErrorAction SilentlyContinue); $X = [bool](Get-Command codex -ErrorAction SilentlyContinue)
  $Plugin = if ($C -and $X) { "both" } elseif ($C) { "claude" } elseif ($X) { "codex" } else { throw "neither Claude Code (claude) nor Codex (codex) found" }
}
$Version = (Select-String -Path "$Src\pyproject.toml" -Pattern '^version\s*=\s*"(.+)"').Matches[0].Groups[1].Value
Say "CardForge Light $Version -> $HomeDir"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "installing uv (astral.sh)"
  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv python install 3.12

$Target = "$HomeDir\versions\$Version"
Say "copying the core to $Target"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
robocopy $Src $Target /E /XD .venv runs __pycache__ .pytest_cache .ruff_cache labels.private /XF .env *.private* | Out-Null
$Copy = $LASTEXITCODE  # robocopy: 0-7 = copied, 8 and above = some files failed
$Missing = First-Missing $Target
if ($Copy -ge 8 -or $Missing -or ((Src-Count $Src) -ne (Src-Count $Target))) {
  throw "CardForge Light was not copied completely to $Target (robocopy $Copy; $Missing). Move the folder out of OneDrive (for example to Downloads) and run the installer again"
}
$global:LASTEXITCODE = 0
# The junction itself, never its target: Remove-Item -Recurse on a junction can empty the target.
if (Test-Path "$HomeDir\current") { [IO.Directory]::Delete("$HomeDir\current") }
New-Item -ItemType Junction -Path "$HomeDir\current" -Target $Target | Out-Null
Set-Location $Target

Say "installing dependencies (uv.lock)"
uv sync --frozen --no-dev --python 3.12

# MCP launcher: Windows runs the .cmd; the installed plugin's .mcp.json points to it.
$Bin = "$HomeDir\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Copy-Item "$Target\plugin\bin\cardforge-lite-mcp.cmd" "$Bin\cardforge-lite-mcp.cmd" -Force
$Cmd = ("$Bin\cardforge-lite-mcp.cmd" -replace '\\','\\')
Set-Content -Path "$Target\plugin\.mcp.json" -Value ('{"mcpServers":{"cardforge-lite":{"command":"' + $Cmd + '","args":[]}}}')
Set-Content -Path "$Target\plugin\.mcp.codex.json" -Value ('{"mcpServers":{"cardforge-lite":{"command":"' + $Cmd + '","args":[],"startup_timeout_sec":60,"tool_timeout_sec":120}}}')

if ($Plugin -in @("claude", "both")) { claude plugin marketplace add "$HomeDir\current"; claude plugin install cardforge-lite@cardforge-lite }
if ($Plugin -in @("codex", "both")) { codex plugin marketplace add "$HomeDir\current"; codex plugin add cardforge-lite@cardforge-lite }

Say "doctor"
.\.venv\Scripts\cardforge.exe doctor --root .
if ($LASTEXITCODE -ne 0) { throw "doctor found errors (lines [ERROR] above): fix them and run this script again" }
Say "CardForge Light is installed in $HomeDir\current. Restart Claude Code and type /cardforge-lite:doctor"
