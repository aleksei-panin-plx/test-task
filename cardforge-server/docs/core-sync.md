# Синхронизация ядра (core-sync)

Эта папка несёт **автономную копию** ядра CardForge (решение пользователя 2026-11-09 «Автономные
копии»). Ядро не правится здесь и в другой копии независимо: правка делается в одной копии и
переносится в другую только `scripts/sync_core.py <источник> <папка>`. Расхождения, которые
синхронизация не затирает, перечислены в `core-sync.toml` и ниже.

## Последняя синхронизация

- дата (UTC): 2026-11-10T15:34:19+00:00
- источник: `../cardforge`
- sha256 ядра источника (`src/cardforge/`, алгоритм `DIST.json` `core_sha256`): `02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd`
- sha256 ядра этой папки после синхронизации и правок: `02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd`
- деревья: `src/cardforge/`, `knowledge/`, `policies/`, `prompts/`, `config/`, `tests/`, `agents/`; файлы: `scripts/stage04_blind_review.py`, `scripts/stage04_blind_review.html`, `scripts/stage04_blind_targets.py`

### Изменения

- добавлено (2):
  - `src/cardforge/openrouter_probe.py`
  - `tests/test_openrouter_probe.py`

- изменено (12):
  - `config/cardforge.toml`
  - `config/evaluation.toml`
  - `config/runtime.toml`
  - `src/cardforge/cli.py`
  - `src/cardforge/contracts/request.py`
  - `src/cardforge/doctor.py`
  - `src/cardforge/evaluate/openrouter_judge.py`
  - `src/cardforge/export/delivery.py`
  - `src/cardforge/mcp/tools.py`
  - `src/cardforge/orchestrator/product.py`
  - `src/cardforge/render/openrouter_image.py`
  - `tests/test_plugin.py`

- удалено: нет

- сохранено по `keep`: нет


## Намеренные расхождения

| Файл | Почему | Статус |
|---|---|---|
| `config/runtime.toml` | Сервер: план составляет агент пользователя через MCP (решение 2026-11-09 «план составляет агент пользователя, сервер валидирует»), поэтому `host-agent` разрешён и в профиле `server`. Предложено перенести в ядро (сессия этапа 7). | applied |
| `config/cardforge.toml` | Рабочая папка сервера в контейнере — `/srv/cardforge` (в папке 1 — `~/.cardforge/current` дизайнера). | applied |

- `keep` (не перезаписываются): нет
- `exclude` (не копируются из источника): нет

Никогда не копируются и не удаляются: `*.private*`, `labels.private/`, `dataset/`, `runs/`, `.env*`, кэши, картинки рамки из архива данных (`knowledge/frames/frame_preview.png`, `occlusion_v2.png`).

## Проверка после синхронизации 2026-11-10T15:34 (этап 10)

Перенесено: проверка маршрутов OpenRouter в `doctor` (`openrouter_probe.py`, `doctor.py`,
`tests/test_openrouter_probe.py`, `route_probe = true`, `probe_model`), варианты набора по запросу
(`start_run(…, alternatives=N)`), конфиг папки 1 `[openrouter]` (`enforce_price_dates = false`,
`enforce_ledger_cap = false`, `budget_usd = 1000000.0`: расход сервера ограничивают месячный лимит на
пользователя и лимит ключа). Патчи `core-sync.toml` применились.

- `pytest -m "not api and not gpu"`: 613 passed, 3 skipped, 1 xfailed; 5 упали и отключены в `conftest.py`
  (серверная часть, ядро не правилось):
  - `test_plugin.py::test_double_click_install_finds_assets_and_agents`, `…::test_installer_finds_the_claude_desktop_app_cli` —
    проверяют `installer/` папки 1, которого здесь нет;
  - `test_openrouter_probe.py::test_doctor_probe_switch`, `…::test_price_date_check_is_off_in_folder_1`,
    `…::test_ledger_cap_is_off_in_folder_1` — рассчитаны на конфиг проекта разработки (`route_probe = false`,
    потолок $176.2) и **падают и в папке 1**; передано владельцу ядра (этап 7), снять отключение после
    исправления теста.
- повтор `tests/test_openrouter_probe.py tests/test_plugin.py tests_server`: 54 passed, 9 deselected, 1 xfailed;
  `ruff check server tests_server scripts conftest.py` — чисто.

## История

- 2026-11-10T11:54:35+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:49:07+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:47:37+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:47:16+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:44:40+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:23:05+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:22:50+00:00: ядро `efac5e625af94661…` из `../cardforge` (+216 ~0 −0)
- 2026-11-10T11:22:49+00:00: ядро `efac5e625af94661…` из `../cardforge` (+216 ~0 −0)
- 2026-11-10T11:04:25+00:00: ядро `efac5e625af94661…` из `cardforge` (+216 ~0 −0)

<!-- sync-json:{"date": "2026-11-10T15:34:19+00:00", "target_core_sha256": "02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd", "source_name": "../cardforge", "n": [2, 12, 0], "history": [{"date": "2026-11-10T11:54:35+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:49:07+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:47:37+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:47:16+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:44:40+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:23:05+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:22:50+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [216, 0, 0]}, {"date": "2026-11-10T11:22:49+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [216, 0, 0]}, {"date": "2026-11-10T11:04:25+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "cardforge", "n": [216, 0, 0]}]}-->
