#!/bin/sh
# CardForge installer (macOS; Linux best effort; Windows: install.ps1 / install.bat).
# Designers double-click installer/install.command (macOS): it runs this script with --gui.
# Assets ship inside the folder: assets/cardforge-data-<v>.tar.gz and assets/models/ (weights) are
# found automatically; --data / --weights-from override them.
#   sh installer/install.sh [--data ARCHIVE] [--plugin auto|claude|codex|both|none] [--gui]
#        [--home ~/.cardforge] [--weights-from DIR] [--no-data] [--dry-run]
# Checks the machine, installs uv and Python 3.12 if missing, copies this folder to
# <home>/versions/<version> (+ <home>/current), installs the dependencies from uv.lock, downloads the
# model weights by sha256 (models/*/source.json), unpacks the data archive (customer references,
# never published), asks for the OpenRouter key, links the MCP launcher, installs the plugin, runs
# `cardforge doctor`. Re-running it repairs or updates an install; the key in <home>/.env is kept.
set -eu

HOME_DIR="${CARDFORGE_INSTALL_HOME:-$HOME/.cardforge}"
DATA=""
NO_DATA=0
PLUGIN="auto"
WEIGHTS=""
GUI=0
SRC=$(cd "$(dirname "$0")/.." && pwd)
DRY=0

abspath() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd)" "$(basename "$1")"); }

while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_DIR="$2"; shift 2 ;;
    --data) DATA="$2"; shift 2 ;;
    --no-data) NO_DATA=1; shift ;;
    --plugin) PLUGIN="$2"; shift 2 ;;
    --weights-from) WEIGHTS="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --gui) GUI=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() {
  printf '\033[31mCardForge: %s\033[0m\n' "$*" >&2
  [ "$GUI" = 1 ] && dialog "CardForge не установлен: $*" stop
  exit 1
}
# A dialog window (double-click install): no typing in the Terminal.
dialog() {
  [ "$GUI" = 1 ] && [ "$DRY" = 0 ] && [ "${CARDFORGE_NO_DIALOG:-0}" != 1 ] && command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display dialog \"$(printf '%s' "$1" | sed 's/"/\\"/g')\" with title \"CardForge\" buttons {\"OK\"} default button \"OK\" with icon ${2:-note}" >/dev/null 2>&1 || true
}
# Agents installed from the Finder do not always put their CLIs on the PATH of a script.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# 1. Machine and inputs (fail early, before anything is downloaded)
OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in
  Darwin) ;;
  Linux) say "Linux: best effort (OCR: RapidOCR, not calibrated)";;
  *) fail "unsupported OS $OS: on Windows use installer/install.ps1";;
esac
mkdir -p "$HOME_DIR" 2>/dev/null || [ "$DRY" = 1 ]
FREE_GB=$(df -Pk "$(dirname "$HOME_DIR")" | awk 'NR==2 {print int($4/1024/1024)}')
[ "$FREE_GB" -ge 6 ] || fail "need 6 GB free disk, have ${FREE_GB} GB"
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
[ -n "$VERSION" ] || fail "no version in $SRC/pyproject.toml (run the script from the CardForge folder)"
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
  # any other app bundle that ships the Codex CLI
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
# Assets shipped inside this folder (never in git): the data archive and the model weights.
if [ -z "$DATA" ] && [ "$NO_DATA" = 0 ]; then
  for f in "$SRC"/assets/cardforge-data-*.tar.gz; do [ -f "$f" ] && DATA="$f"; done
fi
if [ -z "$WEIGHTS" ] && [ -d "$SRC/assets/models" ]; then WEIGHTS="$SRC/assets/models"; fi
if [ -n "$DATA" ]; then
  [ -f "$DATA" ] || fail "data archive not found: $DATA"
  DATA=$(abspath "$DATA")
elif [ "$NO_DATA" = 0 ] && [ ! -d "$HOME_DIR/current/dataset" ]; then
  fail "нет архива данных: положите cardforge-data-$VERSION.tar.gz в папку assets/ рядом с installer/ (или --data <архив>)"
fi
[ -z "$WEIGHTS" ] || WEIGHTS=$(cd "$WEIGHTS" && pwd)
case "$PLUGIN" in
  claude|both) [ -n "$CLAUDE_BIN" ] || fail "Claude Code не найден (ни команда claude, ни приложение Claude)";;
esac
case "$PLUGIN" in
  codex|both) [ -n "$CODEX_BIN" ] || fail "Codex не найден (ни команда codex, ни приложение Codex / ChatGPT)";;
esac
say "CardForge $VERSION → $HOME_DIR ($OS $ARCH, ${FREE_GB} GB free)"

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

# 3. Code
TARGET="$HOME_DIR/versions/$VERSION"
say "copying the core to $TARGET"
run mkdir -p "$TARGET"
if [ "$DRY" = 0 ]; then
  (cd "$SRC" && tar --exclude ./.venv --exclude ./dataset --exclude ./runs --exclude ./assets --exclude '__pycache__' \
     --exclude './models/*/models' --exclude './models/*/*.bin' -cf - .) | (cd "$TARGET" && tar -xf -)
fi
PREV=""
[ -L "$HOME_DIR/current" ] && PREV=$(cd "$HOME_DIR/current" && pwd -P)
run ln -sfn "$TARGET" "$HOME_DIR/current"
cd "$TARGET" 2>/dev/null || [ "$DRY" = 1 ]

# 4. Dependencies from uv.lock (≈ 1 GB on the first install)
say "installing dependencies (uv.lock)"
run uv sync --frozen --python 3.12 --extra analysis --extra evaluation

# 5. Weights (sha256-checked; ≈ 2.5 GB from Hugging Face and GitHub on the first install)
say "model weights (CSD ViT-L 2.3 GB, u2net 0.17 GB)"
if [ -n "$WEIGHTS" ]; then
  run .venv/bin/cardforge fetch-assets --root . --from "$WEIGHTS"
else
  run .venv/bin/cardforge fetch-assets --root .
fi

# 6. Data archive: customer references, frame images, mask review evidence (never published)
if [ -n "$DATA" ]; then
  say "unpacking $(basename "$DATA")"
  run tar -xzf "$DATA" -C "$TARGET"
elif [ -n "$PREV" ] && [ "$PREV" != "$TARGET" ] && [ -d "$PREV/dataset" ] && [ ! -d "$TARGET/dataset" ]; then
  # update: carry the data over from the previous version (a real copy: the core refuses
  # paths that resolve outside its folder, so a symlink would break the style checks)
  say "copying the data from the previous version"
  run cp -R "$PREV/dataset" "$TARGET/dataset"
  for f in knowledge/frames/frame_preview.png knowledge/frames/occlusion_v2.png \
           runs/stage02/17-mask-refinement/review.frozen.json runs/stage02/17-mask-refinement/review.round2.frozen.json; do
    [ -f "$PREV/$f" ] && run mkdir -p "$(dirname "$TARGET/$f")" && run cp "$PREV/$f" "$TARGET/$f"
  done
fi

# 7. Key (kept outside versions so updates keep it)
ENV_FILE="$HOME_DIR/.env"
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  KEY="$OPENROUTER_API_KEY"
elif [ -f "$ENV_FILE" ] && grep -q '^OPENROUTER_API_KEY=.' "$ENV_FILE"; then
  KEY=""
  say "OpenRouter key: kept from $ENV_FILE"
elif [ "$GUI" = 1 ] && [ "$DRY" = 0 ] && [ "${CARDFORGE_NO_DIALOG:-0}" != 1 ] && command -v osascript >/dev/null 2>&1; then
  KEY=$(osascript -e 'text returned of (display dialog "Вставьте ключ OpenRouter.\nБез ключа CardForge будет делать только планы, без картинок." with title "CardForge" default answer "" with hidden answer buttons {"Без ключа", "OK"} default button "OK")' 2>/dev/null || true)
elif [ -t 0 ] && [ "$DRY" = 0 ]; then
  printf 'OpenRouter key (Enter = plans only): '
  stty -echo; read -r KEY; stty echo; echo
else
  KEY=""
fi
if [ -n "$KEY" ] && [ "$DRY" = 0 ]; then
  (umask 077; printf 'OPENROUTER_API_KEY=%s\n' "$KEY" > "$ENV_FILE")
fi
[ -f "$ENV_FILE" ] && run ln -sfn "$ENV_FILE" "$TARGET/.env"

# 8. MCP launcher and the plugin
run mkdir -p "$HOME/.local/bin"
run ln -sfn "$HOME_DIR/current/plugin/bin/cardforge-mcp" "$HOME/.local/bin/cardforge-mcp"
case "$PLUGIN" in
  claude|both)
    say "Claude Code plugin"
    say "  using $CLAUDE_BIN"
    run "$CLAUDE_BIN" plugin marketplace add "$HOME_DIR/current" || say "marketplace already added"
    run "$CLAUDE_BIN" plugin install cardforge@cardforge || run "$CLAUDE_BIN" plugin update cardforge@cardforge || true ;;
esac
case "$PLUGIN" in
  codex|both)
    say "Codex plugin"
    say "  using $CODEX_BIN"
    run "$CODEX_BIN" plugin marketplace add "$HOME_DIR/current" || say "marketplace already added"
    run "$CODEX_BIN" plugin add cardforge@cardforge || true ;;
esac

# 9. Doctor
say "doctor"
if run .venv/bin/cardforge doctor --root .; then
  say "CardForge is installed in $HOME_DIR/current. Restart Claude Code and type /cardforge:doctor (Codex: restart it and ask «проверь CardForge»)"
  case "$PLUGIN" in claude) AGENTS="Claude Code" ;; codex) AGENTS="Codex" ;; both) AGENTS="Claude Code и Codex" ;; *) AGENTS="агент" ;; esac
  dialog "CardForge установлен.\n\nПерезапустите $AGENTS и попросите: «сделай 2 сета коллекции Лето» (в Claude Code: /cardforge:new коллекция Лето, 2 сета)."
else
  fail "doctor found errors (lines [ERROR] above): fix them and run this script again"
fi
