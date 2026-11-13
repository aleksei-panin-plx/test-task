# CardForge Light (G1 only: collection plan and content tables)

A reduced copy of the folder-1 core (`dist/cardforge/`): planner, validators, policies and the host-agent
task protocol. No render, judges, image edits, export, data archive or weights; no API key.

- Designer flow: `plugin/skills/cardforge-lite/SKILL.md`. Editing knowledge: `docs/guides/customize-knowledge.md`.
- The planner instruction is `prompts/planner/*.md` (not code). Card fields: `src/cardforge/contracts/request.py`.
- Checks after changes: `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests`, `.venv/bin/cardforge doctor --root .`,
  dry run `.venv/bin/cardforge plan "Лето" --sets 2 --fake --root .`.
- `tests/test_plugin.py::test_every_module_is_used_and_nothing_else_is_shipped` keeps the scope: every module is
  reachable from the entry points and no render/evaluate/export module comes back.
- Policies: `policies/ip/baseline.md` is always active (its «Чек-лист авторских прав» drives the planner; never
  shown to designers or copied); every `policies/culture/*.md` pack is always active (user 2026-11-13) and any plan-stage rule hit,
  whatever its severity, is a replacement recorded in `policy_notes` and shown in the G1 «Политики» section.
- Never: lift a `block` policy rule, commit `.env`, `runs/`, `*.private*`, `labels.private/`.
