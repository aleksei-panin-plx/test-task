#!/bin/sh
# CardForge Light installer (macOS; Linux best effort; Windows: install.ps1 / install.bat).
# Designers double-click installer/install.command (macOS): it runs this script with --gui.
#   sh installer/install.sh [--plugin auto|claude|codex|both|none] [--gui] [--home ~/.cardforge-lite] [--dry-run]
# Checks the machine, installs uv and Python 3.12 if missing, copies this folder to
# <home>/versions/<version> (+ <home>/current), installs the dependencies from uv.lock, links the MCP
# launcher, installs the plugin into Claude Code and/or Codex, runs `cardforge doctor`.
# Light plans only (G1): no data archive, no model weights, no API key. Re-running it repairs or
# updates an install. It lives next to the full CardForge (~/.cardforge) and never touches it.
set -eu

HOME_DIR="${CARDFORGE_LITE_INSTALL_HOME:-$HOME/.cardforge-lite}"
PLUGIN="auto"
GUI=0
SRC=$(cd "$(dirname "$0")/.." && pwd)
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_DIR="$2"; shift 2 ;;
    --plugin) PLUGIN="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --gui) GUI=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() {
  printf '\033[31mCardForge Light: %s\033[0m\n' "$*" >&2
  [ "$GUI" = 1 ] && dialog "CardForge Light не установлен: $*" stop
  exit 1
}
# A dialog window (double-click install): no typing in the Terminal.
dialog() {
  [ "$GUI" = 1 ] && [ "$DRY" = 0 ] && [ "${CARDFORGE_NO_DIALOG:-0}" != 1 ] && command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display dialog \"$(printf '%s' "$1" | sed 's/"/\\"/g')\" with title \"CardForge Light\" buttons {\"OK\"} default button \"OK\" with icon ${2:-note}" >/dev/null 2>&1 || true
}
# Agents installed from the Finder do not always put their CLIs on the PATH of a script.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# 1. Machine and inputs (fail early, before anything is downloaded)
OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in
  Darwin) ;;
  Linux) say "Linux: best effort";;
  *) fail "unsupported OS $OS: on Windows use installer/install.ps1";;
esac
mkdir -p "$HOME_DIR" 2>/dev/null || [ "$DRY" = 1 ]
FREE_MB=$(df -Pk "$(dirname "$HOME_DIR")" | awk 'NR==2 {print int($4/1024)}')
[ "$FREE_MB" -ge 500 ] || fail "need 500 MB free disk, have ${FREE_MB} MB"
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
[ -n "$VERSION" ] || fail "no version in $SRC/pyproject.toml (run the script from the CardForge Light folder)"
case "$PLUGIN" in auto|none|claude|codex|both) ;; *) fail "--plugin must be auto, claude, codex, both or none";; esac
# The agents' command-line tools: on PATH, or inside the desktop apps (Claude, ChatGPT, Codex),
# which do not put them on PATH. A plugin installed with these tools lands in the same
# ~/.claude and ~/.codex settings the apps read.
find_claude() {
  command -v claude 2>/dev/null && return 0
  [ "${CARDFORGE_AGENT_SEARCH:-all}" = path ] && return 1  # tests: PATH only
  for f in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    [ -x "$f" ] && { echo "$f"; return 0; }
  done
  # Claude desktop app: …/claude-code/<version>/<build>/claude.app/Contents/MacOS/claude, newest first
  base="$HOME/Library/Application Support/Claude/claude-code"
  found=""
  for v in $(ls "$base" 2>/dev/null | sort -V); do
    for f in "$base/$v"/*/claude.app/Contents/MacOS/claude; do [ -x "$f" ] && found="$f"; done
  done
  [ -n "$found" ] && { echo "$found"; return 0; }
  return 1
}
find_codex() {
  command -v codex 2>/dev/null && return 0
  [ "${CARDFORGE_AGENT_SEARCH:-all}" = path ] && return 1
  for f in "$HOME/.codex/packages/standalone/current/bin/codex" /opt/homebrew/bin/codex /usr/local/bin/codex \
           /Applications/Codex.app/Contents/Resources/codex /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex \
           "$HOME/Applications/Codex.app/Contents/Resources/codex" "$HOME/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"; do
    [ -x "$f" ] && { echo "$f"; return 0; }
  done
  find /Applications "$HOME/Applications" -maxdepth 6 -type f -name codex -perm -u+x 2>/dev/null | head -1 | grep . && return 0
  return 1
}
CLAUDE_BIN=$(find_claude || true)
CODEX_BIN=$(find_codex || true)
if [ "$PLUGIN" = auto ]; then  # every agent found on this machine
  HAS_CLAUDE=0; HAS_CODEX=0
  [ -n "$CLAUDE_BIN" ] && HAS_CLAUDE=1
  [ -n "$CODEX_BIN" ] && HAS_CODEX=1
  case "$HAS_CLAUDE$HAS_CODEX" in
    11) PLUGIN=both ;; 10) PLUGIN=claude ;; 01) PLUGIN=codex ;;
    *) fail "не найден ни Claude Code (claude), ни Codex (codex): установите один из них и запустите установку снова" ;;
  esac
fi
case "$PLUGIN" in
  claude|both) [ -n "$CLAUDE_BIN" ] || fail "Claude Code не найден (ни команда claude, ни приложение Claude)";;
esac
case "$PLUGIN" in
  codex|both) [ -n "$CODEX_BIN" ] || fail "Codex не найден (ни команда codex, ни приложение Codex / ChatGPT)";;
esac
# The folder must be complete: a partial copy (or iCloud Drive files not downloaded yet) fails
# later in `uv sync` with a hatchling traceback.
CORE_FILES="src/cardforge/__init__.py src/cardforge/cli.py src/cardforge/mcp/server.py pyproject.toml uv.lock plugin/.mcp.json prompts/planner/card_rules.md knowledge/catalog/vocabulary.yaml"
first_missing() {
  for f in $CORE_FILES; do [ -f "$1/$f" ] || { echo "$f"; return 1; }; done
}
src_count() { (cd "$1" && find src prompts knowledge -type f ! -path '*__pycache__*' 2>/dev/null | wc -l | tr -d ' '); }
ICLOUD_HINT="если папка лежит в iCloud Drive («Рабочий стол», «Документы»), перенесите её, например, в «Загрузки» и запустите установку снова"
MISSING=$(first_missing "$SRC") || fail "папка CardForge Light неполная: нет $MISSING ($SRC). Скопируйте папку cardforge-lite целиком или склонируйте репозиторий заново; $ICLOUD_HINT"
say "CardForge Light $VERSION → $HOME_DIR ($OS $ARCH, ${FREE_MB} MB free)"

# 2. uv and Python 3.12
if ! command -v uv >/dev/null 2>&1; then
  [ -x "$HOME/.local/bin/uv" ] && PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv (astral.sh)"
  run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  PATH="$HOME/.local/bin:$PATH"
fi
run uv python install 3.12

# 3. Code (never the local state of this folder: .venv, runs, .env, private labels)
TARGET="$HOME_DIR/versions/$VERSION"
say "copying the core to $TARGET"
run mkdir -p "$TARGET"
if [ "$DRY" = 0 ]; then
  COPY_LOG=$(mktemp)
  # The exit status of the reading tar is lost in a pipe: it leaves a mark file instead.
  (cd "$SRC" && tar --exclude ./.venv --exclude ./runs --exclude ./.env --exclude '*.private*' --exclude 'labels.private' \
     --exclude '__pycache__' --exclude .pytest_cache --exclude .ruff_cache -cf - . 2>"$COPY_LOG" || touch "$COPY_LOG.failed") \
    | (cd "$TARGET" && tar -xf -) || touch "$COPY_LOG.failed"
  MISSING=$(first_missing "$TARGET") || touch "$COPY_LOG.failed"
  [ "$(src_count "$SRC")" = "$(src_count "$TARGET")" ] || touch "$COPY_LOG.failed"
  if [ -f "$COPY_LOG.failed" ]; then
    tail -5 "$COPY_LOG" >&2
    rm -f "$COPY_LOG" "$COPY_LOG.failed"
    fail "не удалось полностью скопировать CardForge Light в $TARGET (${MISSING:+нет $MISSING; }файлов: $(src_count "$TARGET") из $(src_count "$SRC")). Частые причины: $ICLOUD_HINT; или у Терминала нет доступа к папке (Системные настройки → Конфиденциальность и безопасность → Файлы и папки)"
  fi
  rm -f "$COPY_LOG"
fi
run ln -sfn "$TARGET" "$HOME_DIR/current"
cd "$TARGET" 2>/dev/null || [ "$DRY" = 1 ]

# 4. Dependencies from uv.lock (≈ 30 MB: pydantic, typer, pyyaml)
say "installing dependencies (uv.lock)"
run uv sync --frozen --no-dev --python 3.12

# 5. MCP launcher and the plugin
run mkdir -p "$HOME/.local/bin"
run ln -sfn "$HOME_DIR/current/plugin/bin/cardforge-lite-mcp" "$HOME/.local/bin/cardforge-lite-mcp"
case "$PLUGIN" in
  claude|both)
    say "Claude Code plugin"
    say "  using $CLAUDE_BIN"
    run "$CLAUDE_BIN" plugin marketplace add "$HOME_DIR/current" || say "marketplace already added"
    run "$CLAUDE_BIN" plugin install cardforge-lite@cardforge-lite || run "$CLAUDE_BIN" plugin update cardforge-lite@cardforge-lite || true ;;
esac
case "$PLUGIN" in
  codex|both)
    say "Codex plugin"
    say "  using $CODEX_BIN"
    run "$CODEX_BIN" plugin marketplace add "$HOME_DIR/current" || say "marketplace already added"
    run "$CODEX_BIN" plugin add cardforge-lite@cardforge-lite || true ;;
esac

# 6. Doctor
say "doctor"
if run .venv/bin/cardforge doctor --root .; then
  say "CardForge Light is installed in $HOME_DIR/current. Restart Claude Code and type /cardforge-lite:doctor (Codex: restart it and ask «проверь CardForge Light»)"
  case "$PLUGIN" in claude) AGENTS="Claude Code" ;; codex) AGENTS="Codex" ;; both) AGENTS="Claude Code и Codex" ;; *) AGENTS="агент" ;; esac
  dialog "CardForge Light установлен.\n\nПерезапустите $AGENTS и попросите: «план коллекции Лето, 2 сета» (в Claude Code: /cardforge-lite:new коллекция Лето, 2 сета)."
else
  fail "doctor found errors (lines [ERROR] above): fix them and run this script again"
fi
