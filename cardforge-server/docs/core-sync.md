# Синхронизация ядра (core-sync)

Эта папка несёт **автономную копию** ядра CardForge (решение пользователя 2026-11-09 «Автономные
копии»). Ядро не правится здесь и в другой копии независимо: правка делается в одной копии и
переносится в другую только `scripts/sync_core.py <источник> <папка>`. Расхождения, которые
синхронизация не затирает, перечислены в `core-sync.toml` и ниже.

## Последняя синхронизация

- дата (UTC): 2026-11-10T16:37:47+00:00
- источник: `../cardforge`
- sha256 ядра источника (`src/cardforge/`, алгоритм `DIST.json` `core_sha256`): `02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd`
- sha256 ядра этой папки после синхронизации и правок: `02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd`
- деревья: `src/cardforge/`, `knowledge/`, `policies/`, `prompts/`, `config/`, `tests/`, `agents/`; файлы: `scripts/stage04_blind_review.py`, `scripts/stage04_blind_review.html`, `scripts/stage04_blind_targets.py`

### Изменения

- добавлено: нет

- изменено (1):
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

## История

- 2026-11-10T16:20:45+00:00: ядро `02a8eece57371bfe…` из `../cardforge` (+0 ~2 −0)
- 2026-11-10T15:34:19+00:00: ядро `02a8eece57371bfe…` из `../cardforge` (+2 ~12 −0)
- 2026-11-10T11:54:35+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:49:07+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:47:37+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:47:16+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:44:40+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:23:05+00:00: ядро `efac5e625af94661…` из `../cardforge` (+0 ~0 −0)
- 2026-11-10T11:22:50+00:00: ядро `efac5e625af94661…` из `../cardforge` (+216 ~0 −0)
- 2026-11-10T11:22:49+00:00: ядро `efac5e625af94661…` из `../cardforge` (+216 ~0 −0)
- 2026-11-10T11:04:25+00:00: ядро `efac5e625af94661…` из `cardforge` (+216 ~0 −0)

<!-- sync-json:{"date": "2026-11-10T16:37:47+00:00", "target_core_sha256": "02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd", "source_name": "../cardforge", "n": [0, 1, 0], "history": [{"date": "2026-11-10T16:20:45+00:00", "target_core_sha256": "02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd", "source_name": "../cardforge", "n": [0, 2, 0]}, {"date": "2026-11-10T15:34:19+00:00", "target_core_sha256": "02a8eece57371bfe388b1aa57f7f5a707f3e47d69e02aadd8b0d323c8ffd52fd", "source_name": "../cardforge", "n": [2, 12, 0]}, {"date": "2026-11-10T11:54:35+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:49:07+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:47:37+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:47:16+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:44:40+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:23:05+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [0, 0, 0]}, {"date": "2026-11-10T11:22:50+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [216, 0, 0]}, {"date": "2026-11-10T11:22:49+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "../cardforge", "n": [216, 0, 0]}, {"date": "2026-11-10T11:04:25+00:00", "target_core_sha256": "efac5e625af946612cb6ddb04d33098838af38a10c7008fe9ac7c9499a193c9a", "source_name": "cardforge", "n": [216, 0, 0]}]}-->
