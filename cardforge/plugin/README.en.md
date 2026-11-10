# CardForge for game designers

[Русский](README.md)

CardForge makes collections of item cards (16 sets × 10 cards) in the game's style. You tell your agent —
Claude Code or Codex — the collection theme, review the plan, check the finished sets and get the PNGs.

## Install

**Double-click** `installer/install.command` in the `cardforge` folder (Windows: `installer\install.bat`, not
verified); the data archive and the weights are already in `assets/`, the key is asked in a dialog. Full guide in
the `README.md` of the `cardforge` folder. The installer puts the core into `~/.cardforge/current` and connects the
plugin to every agent found; restart the agent afterwards.

**Manual steps — only if the installer could not add the plugin:** `claude plugin marketplace add
~/.cardforge/current`, then `claude plugin install cardforge@cardforge`; key:
`printf 'OPENROUTER_API_KEY=%s\n' 'your-key' > ~/.cardforge/.env`; Codex:
`codex plugin marketplace add ~/.cardforge/current`, then `codex plugin add cardforge@cardforge` (no slash
commands in Codex — ask in words).

### Your own OpenRouter key

The company key needs nothing. A key of another account: allow the **OpenAI** provider in Guardrails
(https://openrouter.ai/settings/guardrails) and providers **without ZDR** in Privacy
(https://openrouter.ai/settings/privacy). `/cardforge:doctor` probes the routes and names the setting to change.

## First run

`/cardforge:new collection Summer, 2 sets` — a short, cheap first test; the whole collection: `/cardforge:new collection Summer`. A
collection is 16 sets × 10 cards on one theme; the agent asks when the collection theme is ambiguous.

1. **Collection plan (G1):** the 16 set themes and, for sets 1–15, one list of object cards each; gold cards
   (characters) are only marked by their slots (none in sets 1–6, then 1–4); set 16 (grand set, gold only) is
   a theme. Edit in words; say "go" when it is right.
2. **Generation:** all planned sets in one background run (the whole collection is 130 cards).
3. **Doubtful cards** do not interrupt you: the core redraws them and, if that fails, puts the best attempt
   into the set marked "not confirmed by the judges" (orange).
4. **Collection review (G3):** 5×2 sheets per set (gold slots are yellow placeholders); for each marked card:
   keep it, "redo: shorter handle", or swap in an alternative from `alternatives.md`. No variant choice.
5. **Delivery:** `delivery/current/` per set: `index.html`, PNG 430×480, the sheet, framed previews; next to it
   "did not pass" (`screened_out.html`) and `alternatives.md`.

**Set variants on request:** `/cardforge:new 3 variants of the set "Picnic" of the Summer collection` — a main
variant (picked by the judges) plus the number of alternatives you name (3 if you name none); you may make another one the main.

**Redraw a card** any time, also after delivery: `/cardforge:redo v02_c04: no lettering`. You review the
redraw yourself; the delivery is rebuilt and the previous one kept in `delivery/v01/`.

## Availability

| You have | Available | Verified |
|---|---|---|
| Claude Code, macOS, OpenRouter key | full flow | FakeBackend tests; live plugin run after the clean-sheet test |
| Codex, macOS, OpenRouter key | full flow | install and MCP server start verified (temporary `CODEX_HOME`); a session with the agent and rendering — not yet |
| Any agent, no key | plans only | test |
| Windows, OpenRouter key | full flow (OCR: RapidOCR, text checks not calibrated) | not verified |
| No agent | not available (web version later) | — |
