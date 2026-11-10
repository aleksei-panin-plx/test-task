# CardForge (folder 1: core + local plugin + installer)

Built from the CardForge development project by `scripts/build_dist.py` (see `DIST.json`). Do not edit
the core here and in the development project independently: changes go through one of them and are
carried over by a new build (the server folder will use `scripts/sync_core.py`, `docs/core-sync.md`).

- Designer flow: `plugin/skills/cardforge/SKILL.md`; scenario and decisions: `docs/07-plugin.md`.
- Install: `installer/install.sh` (macOS); `installer/install.ps1` (Windows, not verified yet).
- Checks after code changes: `.venv/bin/pytest -m "not api and not gpu"`, `.venv/bin/ruff check src tests`,
  `.venv/bin/cardforge doctor`.
- Policies: `policies/ip/baseline.md` is always active (its section «Чек-лист авторских прав» drives the
  planner and the IP QA; show it from that file, never copy it;
  designers are not asked to confirm it, user 2026-11-10); culture packs attach by `markets`.
- Never: write render prompts by hand, lift a `block` policy rule, publish customer art or runs,
  commit `dataset/`, weights, `.env` or `runs/`.
