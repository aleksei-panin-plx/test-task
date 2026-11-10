# CardForge server (папка 2)

Сервер CardForge для агентов Claude Code и Codex: **MCP по Streamable HTTP** (тот же набор из 18
инструментов, что у локального плагина), очередь прогонов, личные ключи с отзывом и страницы гейтов
G1/G2/G3 в браузере. Внутри — **автономная копия ядра** CardForge (`src/cardforge/`, `knowledge/`,
`policies/`, `prompts/`, `config/`); она синхронизируется с другой копией только скриптом
`scripts/sync_core.py` (см. `docs/core-sync.md`). Документ — для администратора.

Статус: локальный запуск и тесты. **Ничего не развёрнуто**; образ Docker описан, но на машине
разработки не собирался (см. «Что не проверено»).

## Что внутри

| Путь | Что |
|---|---|
| `server/app.py` | HTTP-процесс: `/mcp`, `/health`, `/web/…` (гейты), `/admin/…` |
| `server/mcp_http.py` | транспорт MCP Streamable HTTP (JSON-ответы, без SDK) |
| `server/tools.py` | инструменты ядра `cardforge.mcp.tools` + права, владелец, ссылки на страницы |
| `server/worker.py` | диспетчер очереди: `WORKER_CONCURRENCY` прогонов, каждый — процесс `cardforge work` |
| `server/store_index.py` | SQLite: прогоны → владелец/состояние/стоимость, очередь, ключи, сессии, зеркало решений |
| `server/auth.py` | личные ключи `cf_…` (scope, срок, отзыв), сессии браузера |
| `server/usage.py` | единый журнал использования `usage_events`: web, MCP, платные вызовы воркера |
| `server/budget_users.py` | месячный лимит OpenRouter на пользователя ($50 по умолчанию) |
| `server/retention.py` | хранение прогонов: архив через 30 дней после выдачи |
| `server/core_hooks.py` | две точки встраивания в ядро без его правки (владелец прогона, очередь вместо фонового процесса) |
| `server/web.py`, `web/static/` | страницы G1/G2/G3; G2 — страница ядра `review_page.html` |
| `plugin/.mcp.json` | подключение агента по HTTP |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Linux, CPU-torch, RapidOCR |
| `tests/` | общие тесты ядра (из синхронизации), `tests_server/` — тесты сервера |

Сервер не добавляет Python-зависимостей: HTTP, MCP и индекс — стандартная библиотека. `uv.lock` —
тот же, что у ядра (переименован только корневой пакет).

## Что нужно и что не лежит в git

- Python 3.12 и [uv](https://docs.astral.sh/uv/); Linux или macOS; ≥ 6 ГБ RAM (torch + CSD на CPU), ≈ 4 ГБ диска.
- **Не в репозитории** (`.gitignore`): архив данных заказчика `cardforge-data-*.tar.gz` и его содержимое
  (`dataset/`, картинки рамки `knowledge/frames/*.png`) — его передаёт команда CardForge из рук в руки; веса
  (`models/*/` кроме `source.json`) — `cardforge fetch-assets` качает сам; `runs/`, `.env`, `.venv`.

## Запуск без Docker (macOS / Linux)

```bash
uv sync --frozen --extra analysis --extra evaluation --extra api
cp .env.example .env            # вписать OPENROUTER_API_KEY
.venv/bin/cardforge fetch-assets  # веса u2net и CSD ViT-L по models/*/source.json (sha256)
.venv/bin/python -m server.auth issue --owner anna --scopes read,run,decide,export --days 90
.venv/bin/python -m server.app
```

Ключ из `issue` показывается один раз; его получает сотрудник. Отзыв: `python -m server.auth revoke <id>`,
список: `python -m server.auth list`. Ключ администратора — `--scopes admin`.

## Запуск в Docker (образ не собирался)

`Dockerfile` и `docker-compose.yml` написаны, но **образ ни разу не собирался** и в контейнере ничего не
запускалось — первый запуск проверяйте сами.

```bash
mkdir data && tar -xzf cardforge-data-0.0.1.tar.gz -C data   # данные заказчика, только локально
cp .env.example .env
docker compose up --build -d
docker compose exec cardforge-server cardforge fetch-assets
docker compose exec cardforge-server python -m server.auth issue --owner anna
```

Тома: `cardforge_runs` (прогоны, `index.sqlite3`, журнал OpenRouter `runs/_api/`, paid lock),
`cardforge_models` (веса), `./data` только на чтение. Порт открыт только на `127.0.0.1`; TLS — прокси перед ним.
`mem_limit` 6 GB (torch + CSD на CPU), `stop_grace_period` 5 мин (текущий платный вызов закрывает запись журнала).

## Веса

`cardforge fetch-assets` качает u2net (176 MB) и CSD ViT-L (≈ 2,3 GB) по `models/*/source.json`
со сверкой sha256; `--from <папка models>` копирует уже скачанные. Новых весов сервер не требует.

## Данные заказчика

Архив `cardforge-data-<версия>.tar.gz` (референсы `dataset/`, картинки рамки, свидетельства ревью
масок) распаковывается рядом с кодом (без Docker) или в `./data` (Docker: compose монтирует `dataset/` и картинки рамки на их места; символьные ссылки ядро не принимает).
В git он не входит (`.gitignore`, `.dockerignore`). Референсы открыты для облачных моделей (решение
2026-11-08); **метки людей (`*.private*`, `labels.private/`) и решения гейтов не уходят во внешние
вызовы** (тест `tests_server/test_openrouter_payloads.py`).

## Ключи и правила OpenRouter

- `OPENROUTER_API_KEY` — только в окружении или `.env`; `/health` показывает лишь факт наличия.
- Каждый запрос к судье: один закреплённый провайдер, `allow_fallbacks=false`, `require_parameters=true`,
  `zdr=true`, `data_collection=deny`; GPT-судья — без ZDR (решение 2026-11-04), в сценарии сервера не
  используется. Рендер и GPT-edit: `allow_fallbacks=false`, `data_collection=deny` (без `zdr`, см.
  `docs/acceptance.md`).
- Журнал расходов — журнал ядра `runs/_api/openrouter-ledger.json`. Настройки `config/evaluation.toml`
  `[openrouter]` приходят из папки 1 синхронизацией: `enforce_ledger_cap = false` (общего потолка на машину
  нет), `enforce_price_dates = false`; расход сервера ограничивают **месячный лимит на пользователя** (ниже) и
  лимит самого ключа OpenRouter. Нужен общий потолок — задайте лимит ключа в OpenRouter или добавьте
  расхождение в `core-sync.toml` (иначе следующая синхронизация вернёт значение папки 1).
- Проверка маршрутов: `.venv/bin/cardforge doctor` шлёт по одному крошечному запросу на каждый маршрут
  (`route_probe = true` в `config/cardforge.toml`) и называет настройку аккаунта, если маршрут отклонён
  (Guardrails: провайдер OpenAI; Privacy: запросы без ZDR — подробно в README папки 1, раздел «Свой ключ
  OpenRouter»: [../cardforge/README.md](../cardforge/README.md)). `--offline` — без проб.

## Подключение агента

Claude Code: `plugin/.mcp.json` в проект или `claude mcp add --transport http cardforge <URL>/mcp
--header "Authorization: Bearer $CARDFORGE_KEY"`. Переменные: `CARDFORGE_SERVER_URL`, `CARDFORGE_KEY`.

Codex (`~/.codex/config.toml`, не проверено вживую):

```toml
[mcp_servers.cardforge]
url = "http://127.0.0.1:8006/mcp"
bearer_token_env_var = "CARDFORGE_KEY"
```

Сценарий агента — скилл `cardforge` локального плагина (папка 1): те же инструменты; на сервере
`get_run` дополнительно отдаёт `queue` и `pages` (ссылки на страницы гейтов).

## Гейты (по умолчанию G1 = человек, G2 = none, G3 = человек)

- **G1** — план коллекции: в чате (`get_plan` → `approve_plan` / `edit_plan`) или страница
  `/web/runs/<run>/g1`.
- **G2** — только если включён на прогон (`start_run g2=flagged`): чат или страница `…/g2` (слепая
  страница ядра; «Отправить решения» пишет решения в прогон).
- **G3** — листы 5×2, «не подтверждена судьями», перерисовка с правкой, сборка и скачивание выдачи
  (`…/g3`, `…/delivery.zip`). `CARDFORGE_G3_CHANNELS=web` оставляет G3 только браузеру.

Решения всегда записываются от имени владельца ключа (поле `reviewer` агента заменяется).

## Учёт использования, лимит, хранение (решения пользователя 2026-11-10)

- **Журнал использования** (`usage_events` в `runs/_server/index.sqlite3`) — единый источник: каждое
  событие приписано проверенному владельцу (ключ MCP, сессия браузера или владелец прогона), канал
  `web | mcp | worker`, операция, прогон, стоимость, модель, время. MCP — каждый `tools/call`; web —
  действия на гейтах, вход, скачивание выдачи (статика, рендер страниц и картинки — не события);
  worker — каждый платный вызов OpenRouter с фактической ценой из `costs.json` прогона (без повторов).
  Просмотр: `GET /admin/usage?owner=&channel=&month=`, `python -m server.store_index usage`. Отсюда
  будущий сервис портала передаст события в `usage-logger` портала.
- **Месячный лимит** на пользователя — `USER_MONTH_LIMIT_USD` (по умолчанию **$50**); свой лимит —
  `python -m server.budget_users set --owner anna --usd 80` (`unset`, `show`) или `POST /admin/limits
  {"owner": "anna", "usd": 80}` (`"usd": null` — вернуть по умолчанию). Перед каждым заданием потолок
  прогона ставится по остатку месяца; при исчерпании ядро не отправляет вызов и прогон становится
  `partial / budget_exhausted`; дизайнер видит только «месячный лимит исчерпан» (без сумм). После
  увеличения лимита — `resume_run`. Расход по пользователям — `GET /admin/costs`.
- **Хранение**: прогон с выдачей архивируется через `RUN_RETENTION_DAYS` (30) дней без активности после
  выдачи — `runs/_archive/<run>.tar.gz`, папка удаляется, инструменты отвечают «в архиве». Задача идёт в
  диспетчере каждые `RETENTION_EVERY_H` часов; вручную — `python -m server.retention [--dry-run]`;
  вернуть — `python -m server.retention restore <run>`. Прогоны без выдачи не архивируются.

## Стоп-кран

`runs/STOP` (или `POST /admin/stop {"on": true}`, или `CARDFORGE_STOP=1`): новые задания не стартуют,
идущие останавливаются после текущего вызова. `stop_run` — стоп одного прогона, `resume_run` — продолжить.

## Синхронизация ядра

```bash
python -I scripts/sync_core.py <папка-источник> .            # например ../cardforge
python -I scripts/sync_core.py <папка-источник> . --dry-run  # только отчёт
```

Намеренные расхождения — `core-sync.toml`; отчёт и sha256 — `docs/core-sync.md`. После синхронизации:
тесты (ниже). Править `src/cardforge/` здесь напрямую нельзя (`tests_server/test_sync_core.py` это ловит).

## Проверки

```bash
.venv/bin/pytest -m "not api and not gpu"
.venv/bin/ruff check server tests_server scripts
```

e2e на FakeBackend через MCP HTTP: `tests_server/test_e2e_mcp_http.py` (без сети, $0).

## Что не проверено

- образ Docker не собирался и e2e в Linux-контейнере не запускался;
- подключение Claude Code / Codex к живому серверу и прогон до G3 агентом;
- платный прогон через сервер (нужно отдельное разрешение);
- Windows как хост сервера (цель — Linux-контейнер).
