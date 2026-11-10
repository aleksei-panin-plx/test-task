@echo off
rem Starts the CardForge MCP server (stdio) on Windows: %CARDFORGE_HOME%, else %USERPROFILE%\.cardforge\current.
setlocal
set "CF=%CARDFORGE_HOME%"
if "%CF%"=="" set "CF=%USERPROFILE%\.cardforge\current"
if not exist "%CF%\.venv\Scripts\cardforge.exe" (
  echo cardforge-mcp: CardForge core not found ^(set CARDFORGE_HOME or run the installer^) 1>&2
  exit /b 1
)
cd /d "%CF%"
"%CF%\.venv\Scripts\cardforge.exe" mcp --root "%CF%"
