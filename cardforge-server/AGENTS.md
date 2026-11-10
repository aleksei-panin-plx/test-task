# CardForge server (folder 2: server + MCP Streamable HTTP)

Built from the CardForge development project by `scripts/build_server_dist.py` (see `SERVER.json`).
Admin guide: `README.md`. Acceptance status: `docs/acceptance.md`.

## Core sync rule (user decision 2026-11-09 «Автономные копии»)

- `src/cardforge/`, `knowledge/`, `policies/`, `prompts/`, `config/` (and the shared core tests
  `tests/`, `agents/`) are an **autonomous copy** of the core. Never edit them here and in the other
  copy independently: a core change is made in one copy and carried to the other **only** by
  `python -I scripts/sync_core.py <source> <target>`, which writes `docs/core-sync.md` (sha256, date,
  diff). `tests_server/test_sync_core.py` fails if `src/cardforge/` differs from the last sync record.
- Intentional divergences of this copy live in `core-sync.toml` (patches re-applied after every sync,
  `keep`, `exclude`) and are listed in `docs/core-sync.md`. A patch that no longer applies fails the
  sync: re-decide it, never drop it silently.
- Server code lives outside the synced trees: `server/`, `web/`, `tests_server/`, `plugin/`. It plugs
  into the core only through `server/core_hooks.py`.
- After every sync: `.venv/bin/pytest -m "not api and not gpu"` (core tests and server tests must pass).

## Rules that stay as in the core project

- Gates: G1 = human, G2 = none, G3 = human by default (`config/autonomy.toml`, per-run override).
  G1 = the collection plan (16 set themes, sets 1–15 with one object-card list; a number of sets in the
  request overrides it). No budget display and no copyright checklist for the designer; IP doubts are
  comments at G2/G3. Decisions are recorded verbatim and bound to the key's owner.
- OpenRouter: pinned provider, no fallbacks, `zdr=true`, `data_collection=deny` for every judge (the GPT
  judge without ZDR only by the 2026-11-04 decision); the core ledger and the paid lock; the kill switch
  `runs/STOP`. No new weights, no cloud GPUs, no deployment without an explicit user decision.
- Keys only in the environment or `.env`; logs record presence only.
- Usage from day one (user 2026-11-10, portal lesson `[M3-USAGE-MISSING]`): every MCP call, web action and
  paid worker call is one event in `usage_events` (`server/usage.py`) attributed to the verified owner;
  a new tool, page action or worker path must write it (tests in `tests_server/test_usage_limit_retention.py`).
  Monthly OpenRouter limit per user $50 by default, changed by the admin; runs archived 30 days after delivery.
- Human labels (`*.private*`, `labels.private/`), gate decisions and customer data (`dataset/`, the data
  asset) never leave the machine and never enter git or a judge payload.
- Policies: `policies/ip/baseline.md` is always active (its section «Чек-лист авторских прав» drives the
  planner and the IP QA; show it from that file, never copy it; designers are not asked to confirm it,
  user 2026-11-10); culture packs attach by `markets`; policy flags are explained by
  `agents/compliance-reviewer.md`.
- Never: write render prompts by hand, lift a `block` policy rule, publish customer art or runs, commit
  `dataset/`, weights, `.env` or `runs/`.
