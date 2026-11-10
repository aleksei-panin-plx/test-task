# CardForge — install

[Русский](README.md) · using it after install — [plugin/README.en.md](plugin/README.en.md)

CardForge makes collections of game item cards (16 sets × 10 cards) in the game's style, as a plugin in
Claude Code (or Codex).

## Requirements

macOS (Windows: not verified, see below); Claude Code or Codex installed (the installer finds them); internet
during the install (≈ 1 GB of Python packages; the model weights are already in the folder); ≥ 6 GB free disk;
5–15 minutes; the company OpenRouter key from the CardForge team (without a key: plans only). No admin rights.

You get one `cardforge` folder (≈ 3 GB) from the CardForge team. Its `assets/` holds the data archive (customer
references — **never publish it, never put it in git**) and the model weights; the installer finds them itself.
**A copy taken from the git repository has no `assets/`:** ask the CardForge team for `cardforge-data-*.tar.gz`,
put it into `cardforge/assets/` (or run `sh installer/install.sh --data <archive>`); the installer downloads the
weights itself (sha256 from `models/*/source.json`).

## Install — double-click

Open `cardforge/installer` in Finder and double-click **`install.command`**. A Terminal window opens — nothing to
type there. Paste the OpenRouter key into the dialog (or press "Без ключа" for plans only). A "CardForge
установлен" dialog ends the install. If macOS refuses to open the file (unidentified developer — the folder came
from the internet or AirDrop): System Settings → Privacy & Security → "Open Anyway", then double-click again.

The installer checks the machine, installs uv and Python 3.12 if missing, copies CardForge to `~/.cardforge/current`,
installs the dependencies from `uv.lock`, takes the weights from `assets/` (sha256-checked), unpacks the data,
stores the key in `~/.cardforge/.env`, connects the plugin to every agent found and runs `cardforge doctor`.

**Success:** the "CardForge установлен" dialog; in Terminal the doctor line says `plugin mode full; no errors`.
Restart Claude Code and type `/cardforge:doctor`. Codex: restart it and ask in words ("check CardForge", "make 2
sets of the Summer collection"). Check the install any time: `~/.cardforge/current/.venv/bin/cardforge doctor`.

## First run

- `/cardforge:new collection Summer, 2 sets` — **start here**: the collection plan (G1) with 16 set themes, only
  2 sets × 10 cards are drawn.
- `/cardforge:new collection Summer` — the whole collection: 16 themes, sets 1–15 drawn (set 16 is the gold grand
  set, theme only).
- `/cardforge:new 3 variants of the set "Picnic" of the Summer collection` — set variants, only on request: a main
  variant picked by the judges + the number of alternatives you name.

There is no default collection: if the theme is ambiguous, the agent asks. Edit the G1 plan in words, say "go";
all planned sets are drawn in one background run; on G3 keep, redraw or swap the marked cards; then the agent names
the delivery folder. More: [plugin/README.en.md](plugin/README.en.md).

## Your own OpenRouter key

The company key needs nothing. A key of **another** OpenRouter account needs two account settings, or no cards are
drawn: **Guardrails** (https://openrouter.ai/settings/guardrails, for the account and for the key if it has its own
limits) — allow the **OpenAI** provider (GPT Image 2.5 is served only by OpenAI); **Privacy**
(https://openrouter.ai/settings/privacy) — allow providers **without ZDR** (GPT Image has no ZDR endpoint; Kimi and
Nano Banana are pinned to ZDR providers by CardForge). A key of the same account or organisation needs only its own
key limits checked. `cardforge doctor` sends one tiny request (≈ $0) per route with the real settings and names the
setting to change; until then the mode is "blocked".

## Problems, reinstall, uninstall

- No agent found → install Claude Code or Codex; no data archive → ask for the complete folder; download or
  sha256 error → run the install again; `doctor found errors` → fix the `[ERROR]` lines and rerun.
- No key: `printf 'OPENROUTER_API_KEY=%s\n' 'your-key' > ~/.cardforge/.env`.
- Update / reinstall: double-click `install.command` in the new folder (the key is kept).
- Uninstall: `claude plugin uninstall cardforge@cardforge`, `claude plugin marketplace remove cardforge`,
  `rm -rf ~/.cardforge ~/.local/bin/cardforge-mcp` (this also deletes finished sets in `runs/`); Codex:
  `codex plugin remove cardforge@cardforge`, `codex plugin marketplace remove cardforge`.
- The `sh installer/install.sh` options (`--data`, `--plugin`, `--home`, `--weights-from`, `--no-data`,
  `--dry-run`) are listed in [README.md](README.md).

## Windows — not verified

The Windows installer is written but has never been run on Windows. Double-click `installer\install.bat` (it also
takes the archive and weights from `assets\`). On Windows the text check uses RapidOCR and is not calibrated.
