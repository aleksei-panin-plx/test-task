@echo off
rem Starts the CardForge Light MCP server (stdio) on Windows: %CARDFORGE_LITE_HOME%, else %USERPROFILE%\.cardforge-lite\current.
setlocal
set "CF=%CARDFORGE_LITE_HOME%"
if "%CF%"=="" set "CF=%USERPROFILE%\.cardforge-lite\current"
if not exist "%CF%\.venv\Scripts\cardforge.exe" (
  echo cardforge-lite-mcp: CardForge Light core not found ^(set CARDFORGE_LITE_HOME or run the installer^) 1>&2
  exit /b 1
)
cd /d "%CF%"
"%CF%\.venv\Scripts\cardforge.exe" mcp --root "%CF%"
