# Папка 2 `cardforge-server/` — приёмка (2026-11-10)

Сборка: `scripts/build_server_dist.py` проекта CardForge → `dist/cardforge-server/` (`SERVER.json`).
Ядро взято синхронизацией из папки 1 `dist/cardforge/` (`docs/core-sync.md`). Без деплоя, без платных
вызовов, без новых весов; всё офлайн, $0.

План портала (`cardforge-ai-portal-service-plan-2026-11-09.md`) писался до двух решений, сверено с
`AGENTS.md`: **G2 = none по умолчанию** (страница G2 — только если включён на прогон) и **G1 = план
коллекции** (16 тем, сеты 1–15 по одному списку; число сетов из запроса). Чек-листа `ip_check` в сценарии
сотрудника больше нет (решение 2026-11-10), поэтому «`ip_check` только из браузера» не применимо.

## Критерии §9 плана портала, применимые к папке 2

| # | Критерий | Статус | Чем подтверждено |
|---|---|---|---|
| 1 | `cardforge demo` на FakeBackend проходит через `/mcp` в Linux-контейнере (без сети и платных вызовов) | **частично**: e2e на FakeBackend через MCP HTTP проходит на macOS-хосте; в Linux-контейнере не запускался (демон Docker не запущен, сборка образа потребовала бы загрузки колёс torch и др. сверх базового образа) | `tests_server/test_e2e_mcp_http.py` |
| 2 | Claude Code и Codex без Python подключаются по `.mcp.json` с личным ключом и доводят прогон до G3; решения G3 и `ip_check` — только из браузера | **частично**: `plugin/.mcp.json` (http + Bearer), ключи с отзывом; живого подключения агентов не было. G3 только из браузера — настройкой `CARDFORGE_G3_CHANNELS=web` (по умолчанию `mcp,web`, как у локального плагина — **открытый вопрос пользователю**); `ip_check` не применим | `test_auth_http.py::test_g3_only_from_the_browser_when_configured` |
| 3 | Прогон, начатый через MCP, открывается CLI ядра (`status`, `export`) без конвертации | **выполнено** | `test_e2e_mcp_http.py` (вызов `python -m cardforge status/export`) |
| 4 | Прогоны двух пользователей идут одновременно без смешения файлов и бюджета; второй ждёт в очереди при исчерпанном `WORKER_CONCURRENCY` | **выполнено** (учёт стоимости и месячный лимит — по владельцу, см. 5) | `test_queue_isolation.py::test_two_users_are_isolated_and_the_second_waits_in_the_queue` |
| 5 | Расход каждого вызова OpenRouter виден в админ-статистике с фактической ценой и владельцем; месячный лимит останавливает прогон как `partial / budget_exhausted` | **выполнено для папки 2**: каждый платный вызов — событие `usage_events` (канал worker, владелец, фактическая цена, модель), `GET /admin/usage`, `/admin/costs`; лимит $50 по умолчанию, меняет админ, прогон останавливается как `partial / budget_exhausted`. Передача в статистику портала (`usage-logger`, `cost_usd_override`) — шаг сервиса портала | `test_usage_limit_retention.py` |
| 6 | Каждый запрос к OpenRouter несёт `zdr=true`, `data_collection=deny`, `allow_fallbacks=false` (кроме GPT-судьи) — тест на сформированном запросе | **выполнено для судей** (все кандидаты `evaluation.toml`, теневой Kimi `kimi-veto-v2` на живом прогоне через MockTransport, GPT-судья — единственный без ZDR и только при явном `zdr=False`). **Рендер и GPT-edit** несут `allow_fallbacks=false` и `data_collection=deny`, но **не `zdr`** — правило ZDR в `AGENTS.md` задано для судей; зафиксировано строгим xfail | `test_openrouter_payloads.py` |
| 7 | Метки людей и решения гейтов не уходят во внешние вызовы — тест на payload судей | **выполнено** (решения G1/G2 с маркерами, `labels.private/`, `*.private.json` в прогоне; все тела запросов судьи до и после перерисовки) | `test_openrouter_payloads.py::test_human_labels_and_gate_decisions_never_reach_judge_payloads` |
| 8 | `runs/STOP` / флаг `stopped` останавливает воркер до следующего платного вызова | **выполнено**: при `runs/STOP` диспетчер не стартует задания; идущие останавливает ядро после текущего вызова (`stop_requested`); `stop_run` отменяет задание прогона | `test_queue_isolation.py::test_kill_switch_and_run_stop` |
| 9 | Middleware RequestID и Audit; Access Control по `service_id = cardforge` | **не применимо к папке 2** (это платформа портала); вместо них — личные ключи со scope, проверка владельца на каждый вызов, зеркало решений `decisions` (канал, инструмент, владелец), проверка `Origin`, CSP на страницах | `test_auth_http.py` |

Сверх §9 (решения пользователя 2026-11-10):
- единый журнал использования web | mcp | worker с приписанным владельцем; статика и рендер страниц не
  пишутся; вызов MCP и вызов воркера дают по одной записи — `test_mcp_call_and_worker_call_each_give_one_attributed_event`,
  `test_web_actions_are_events_static_and_pages_are_not`;
- хранение 30 дней после выдачи, затем архив — `test_runs_are_archived_30_days_after_delivery`;
- paid lock: каждое задание — отдельный процесс `cardforge work` (`product.work` берёт
  `runs/_api/paid-run.lock` для платных прогонов) — `test_paid_runs_go_through_the_core_paid_lock`;
- потерянное задание после падения диспетчера возвращается в очередь — `test_process_jobs_are_separate_pids_and_a_lost_job_is_requeued`;
- общие тесты ядра проходят в копии (кроме 4 тестов файлов папки 1, `conftest.py`). Прогон 2026-11-10:
  `pytest -m "not api and not gpu"` — 585 passed, 13 failed, 3 skipped, 4 deselected, 1 xfailed; все 13 упали
  на `dataset/`, подключённом символьной ссылкой (ядро отказывает: «path escapes artifact root»), и прошли
  повтором с настоящей копией `dataset/` (13 passed). Итого 598 passed (в том числе 18 тестов сервера) и 1 строгий xfail (ZDR рендера); после добавления учёта, лимита и хранения тесты сервера — 23 passed и 1 xfail;
- `sync_core.py`: diff, sha256, расхождения, приватные данные не копируются и не удаляются — `test_sync_core.py`.

## Что не проверено

- сборка образа Docker и e2e в Linux-контейнере (демон Docker не запущен; загрузки сверх обычных образов не делались);
- `uv sync --frozen` в папке 2 офлайн: в кэше uv нет колеса torch для macOS; тесты шли интерпретатором проекта
  (тот же `uv.lock`, корневой пакет переименован, `uv lock --check --offline` — ок) с ядром папки 2 первым в
  `sys.path` (проверено `cardforge.__file__`);
- живое подключение Claude Code / Codex по HTTP; платный прогон через сервер; Windows как хост;
- данные заказчика для тестов: `dataset/` (клон APFS), картинки рамки и свидетельства ревью масок извлечены
  из `dist/assets/cardforge-data-0.0.1.tar.gz` в папку 2 локально (в `.gitignore`, `.dockerignore`).
  Дымовой запуск `python -m server.app`: `/health`, `initialize` с ключом (200 + `Mcp-Session-Id`), без ключа 401.

## Правки ядра, предложенные сессии этапа 7

1. `config/runtime.toml` `[deployment.server] llm_providers`: добавить `host-agent` (план составляет агент
   пользователя через MCP). Сейчас — намеренное расхождение `core-sync.toml`.
2. Точки встраивания вместо подмены атрибутов (`server/core_hooks.py`): `product.start(..., owner=, deployment=)`
   с передачей в `service.create_run`, и настраиваемый запуск фонового прогона (например
   `CARDFORGE_WORKER=external` → `_start_worker` ничего не запускает, или регистрируемая функция).
3. `render/edit_runtime.paid_lock` повторно входим **по pid**: два потока одного процесса оба проходят замок,
   и первый, выходя, удаляет его под вторым. Сервер обходит это процессами; в ядре стоит добавить
   поточный замок или id потока в `owner.txt`.
4. ZDR для рендера: профили `openrouter-image` не ставят `zdr` (Nano Banana и др. не-OpenAI). Если пользователь
   хочет ZDR и для генераторов — `zdr = true` в профилях; иначе снять xfail в `test_openrouter_payloads.py`
   и записать решение.
5. `.gitignore` папки 1 (`build_dist.DIST_GITIGNORE`) не скрывает картинки рамки из архива данных
   (`knowledge/frames/frame_preview.png`, `occlusion_v2.png`): после распаковки они видны git как новые файлы.
6. Manifest прогона: поле для версии сервера есть только в `versions` (сервер пишет `core_version`,
   `server_version` туда же); при желании — явное поле.
