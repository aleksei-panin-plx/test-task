# Этап 7 — плагин CardForge для Claude Code и Codex

Сценарий согласован с пользователем 2026-11-10 в сессии этапа 7 («Да, как рекомендовано»). Решения с цитатами —
`docs/decisions.md`, запись «2026-11-10: этап 7 — сценарий плагина». Этот файл — сценарий, критерии приёмки и
протокол реализации.

## 1. Кто и что

Пользователь — гейм-дизайнер, не программист. Работает в своём агенте (Claude Code или Codex), в котором
установлен плагин CardForge: `SKILL.md`, слэш-команды и MCP-сервер `cardforge mcp` (stdio) поверх локального
ядра. Агент — проводник: сам отвечает на задачи ядра (план, нормализация, скрининг и IP-проверка картинок),
показывает человеку только то, где нужно его решение, и **никогда не пишет промпты рендера**.

Без ключа OpenRouter доступны только планы наборов; с ключом — полный сценарий (рендер GPT Image 2.5, Nano
Banana — резерв). Целевые ОС этапа — **macOS и Windows** (решение «В этапе 7», 2026-11-10; было «macOS, Windows —
следующим шагом»).

## 2. Сценарий

### 2.1. Запрос → план (G1) — исходная версия; заменена планом коллекции (§5.5)

1. Обязательна только **тема набора** («пиратское кино», «вестерн»). По умолчанию: коллекция «Кино», 3 варианта,
   рынков нет, пожелания — свободный текст пользователя. Рынок агент уточняет одним вопросом, только если бриф
   на него намекает (Китай и т. п.).
2. `start_run` создаёт прогон; агент отвечает на задачи нормализации и плана (host-agent), ядро проверяет
   грамматику сета и политики, ремонтирует план.
3. **G1 показывается всегда** (`g1 = all`): по варианту — название, идея и таблица из 10 строк (объект,
   категория, фон), флаги политик и замены. **Ни стоимости, ни потолка** (решение пользователя).
4. Правки — словами («в v02 вместо торта бокалы», «v03 убери, дай другой»): `edit_plan` записывает слова человека,
   прогон уходит на ремонт плана, агент переписывает план по правке, ядро заново прогоняет валидаторы и политики
   и снова открывает G1. «Запускай» → `approve_plan` (по варианту `approve` / `reject`).

### 2.2. Генерация

- Рендер идёт **фоновым процессом** (`cardforge work`), MCP-вызов не блокируется. Агент опрашивает `get_run`
  и коротко сообщает прогресс: «18/30 готово, 2 карты ждут тебя». Стоимость не показывается.
- Когда ядро ждёт агента (IP-проверка картинок, правки), агент берёт задачи `get_agent_tasks`, отвечает
  `submit_agent_answer` и продолжает `resume_run`.
- «Стоп» → `stop_run` (файл `STOP` в папке прогона; общий стоп-кран `runs/STOP` остаётся). «Продолжи» →
  `resume_run`; готовые карты не перерисовываются.
- **Бюджета прогона нет.** Скрытый предохранитель — лимит попыток на карту (3); карта на лимите приходит
  человеку на G2 как обычная флаговая. Расход пишется в журнал прогона (`costs.json`, `manifest.json`) для
  компании, дизайнеру не показывается.

### 2.3. G2 — по умолчанию выключен (2026-11-10)

Решение «Да, делаем»: человек только на G1 и G3; G2 = none по умолчанию, ядро само перерисовывает спорные карты,
лучшая попытка идёт в набор с пометкой «не подтверждена судьями» (§4.2). Ниже — режим `g2 = flagged`, если его
включить на прогон.

- Авто-пропуск GPT действует (G2 = `flagged`). Карты вне авто-пропуска, на лимите попыток и с сомнением
  IP-проверки (`uncertain`) приходят человеку **с причиной словами** и, если есть, **комментарием IP**
  («силуэт похож на известную модель фургона»).
- До ~12 карт — прямо в чате (`get_flags` отдаёт миниатюры), решения словами: «3 ок, 5 переделай: ручка короче,
  7 убери» → `submit_card_decisions` (`accept` / `reject` / `fix: <текст>`). Больше — локальная страница
  `open_review_page`, решения импортируются из её JSON.

### 2.4. G3 — выбор варианта

- `get_sheets`: листы 5×2 полных вариантов (миниатюры), авто-пропущенные карты помечены, у варианта —
  комментарии IP-проверки, если были.
- Человек называет вариант(ы): `select_variant`. Можно вернуть авто-пропущенную карту (`g3_return` внутри
  `regenerate_cards`).
- **Чек-листа авторских прав и подтверждения `confirm-ip` в сценарии нет** (решение пользователя). Правила
  `block` по-прежнему не снимаются; IP-проверка работает на каждом прогоне.

### 2.5. Выдача

После выбора на G3 `export_run` собирает папку выдачи `delivery/current/` в папке прогона:

- `index.html` — выбранные наборы и раздел «Не прошли отбор»;
- по варианту `<vNN>/png/NN-<card>.png` (430×480), `sheet-5x2.png`, `framed/` (превью в рамке v2), `cards.json`;
- `screened_out.json` + `screened_out.html` — все картинки вне итоговых наборов с путями (`final.png`, `raw.png`),
  вариантом, позицией и причиной;
- `alternatives.md` — по каждой позиции итогового набора: чем её можно заменить (пути);
- технический `report.md` прогона остаётся рядом (журнал для компании).

Повторный экспорт переносит прежний `delivery/current/` в `delivery/vNN/` (история версий).

### 2.6. Перегенерация с правками

`cardforge regenerate <run> --card v03_c04 [--card …] --note "без надписей, руль перед вторым сиденьем"`
(MCP `regenerate_cards`):

- работает до и после экспорта; для принятой, отклонённой, флаговой или авто-пропущенной карты (авто-пропуск
  засчитывается как возврат G3 — предохранитель авто-пропуска это видит);
- новая попытка рисуется с правкой человека (`fix_normalize`), прежняя картинка остаётся альтернативой;
- карте добавляется одна попытка сверх лимита; **перерисованную карту всегда смотрит человек** (авто-пропуск
  для неё выключен);
- после решения на G2 агент повторяет `export_run`: выдача пересобирается с новой версией.

### 2.7. Что агент не делает

- не пишет промпты рендера, не меняет профиль рендера посреди прогона;
- не снимает `block` политик, не принимает лицензии;
- ничего не публикует и не отправляет во внешние сервисы (Miro, git, движок);
- не решает за человека на G1/G2/G3: решения записываются его словами.

## 3. Поставка

- `plugin/` — `SKILL.md` (`skills/cardforge/SKILL.md`), `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
  `.mcp.json` (stdio: `bin/cardforge-mcp` → `cardforge mcp`), слэш-команды `commands/`.
- **Слэш-команды** Claude Code: `/cardforge:new <тема>`, `/cardforge:status`, `/cardforge:review`,
  `/cardforge:finish`, `/cardforge:redo`, `/cardforge:doctor`. В Codex те же сценарии — через скилл.
- **MCP-инструменты:** `doctor`, `start_run`, `get_plan`, `edit_plan`, `approve_plan`, `get_run`, `list_runs`,
  `get_agent_tasks`, `submit_agent_answer`, `resume_run`, `stop_run`, `get_flags`, `submit_card_decisions`,
  `open_review_page`, `get_sheets`, `select_variant`, `export_run`, `regenerate_cards`.

## 4. Критерии приёмки (заменяют критерии дорожной карты от 2026-11-01)

- [ ] Плагин устанавливается в Claude Code и в Codex строго по README, без дополнительных объяснений.
- [ ] **Тест чистого листа** (платный, ≈ $3–4 на агента, только по отдельному разрешению пользователя): новая
      сессия каждого агента без истории получает бриф «Кино: <новый жанр>», доводит прогон до выдачи с ≥ 3
      вариантами; вмешательства человека — только G1, флаговые G2 и выбор G3; число вмешательств записано.
- [ ] По `events.jsonl` видно, что агент не писал промпты рендера сам и не обходил гейты.
- [ ] При флагах политик агент объясняет их ролью `compliance-reviewer` и не снимает `block`.
- [ ] В сценарии сотрудника нет потолка бюджета, оценок стоимости и чек-листа авторских прав; комментарии
      IP-проверки видны на G2/G3 (тест).
- [ ] Правка плана на G1 словами перестраивает план через ремонт с валидаторами (тест).
- [ ] Выбор варианта на G3 и выдача (`delivery/`, отсеянные карты, альтернативы) делает ядро, без скриптов
      пилота (тест).
- [ ] `regenerate` перерисовывает выбранные карты с правкой, в том числе после экспорта; перерисованная карта
      идёт человеку; выдача пересобирается с версией (тест).
- [ ] Рендер идёт фоновым процессом, `stop_run` останавливает прогон, `resume_run` продолжает (тест).
- [ ] Без ключа рендер не предлагается: `start_run` даёт только план, `doctor` объясняет, чего не хватает;
      с ключом Nano Banana — только резерв, это видно в `doctor`.
- [ ] Невалидный ответ агента на задачу превращается в задачу на ремонт, а не в падение прогона (тест).
- [ ] MCP-сервер stdio отвечает на `initialize`, `tools/list`, `tools/call`; e2e на `FakeBackend` проходит через MCP
      (тест).
- [ ] Матрица доступности в README; ячейки, не проверенные на машинах проекта (Codex, Windows), помечены
      «не проверено».
- [ ] Тест синхронности Codex-адаптеров (`agents/` ↔ плагин) зелёный.

### 4.2. Человек только на G1 и G3 (2026-11-10, «Да, делаем»)

- [ ] По умолчанию G1 = human, G2 = none, G3 = human (`config/autonomy.toml [gates]`); переопределение на прогон
      в CLI и MCP (тест).
- [ ] При G2 = none карты вне авто-пропуска перерисовываются в пределах лимита; лучшая попытка идёт в набор с
      пометкой «не подтверждена судьями» и причиной — на листе G3, в выдаче и в отчёте (тест).
- [ ] На G3: принять, перерисовать с правкой, заменить альтернативой; авто-отказ выключен, kill switch остаётся.

### 4.3. План коллекции (2026-11-10)

- [ ] Темы коллекции по умолчанию нет; если она не ясна однозначно, агент спрашивает (`start_run` требует
      `collection`, тест).
- [ ] G1 = план всей коллекции: 16 тем сетов, сеты 1–15 — по одному списку предметных карт, золотые позиции по
      образцу референсов (0 → 4), сет 16 — только тема; правки словами (тест).
- [ ] Число сетов из запроса (`sets`) — планируются и рисуются только они (тест).
- [ ] После G1 все сеты рисуются одним прогоном; G3 — проверка по сетам без выбора варианта; выдача по сетам
      (тест).
- [ ] `set_grammar` для 10 − N предметных карт (тест).

### 4.1. Расширение объёма (2026-11-10, «тогда сразу на этапе 7 реализовывай»)

- [ ] Папка 1 `cardforge/` собирается из проекта без git и без арта заказчика; в ней проходят тесты и `doctor`.
- [ ] Windows: OCR без macOS (RapidOCR), torch без CUDA, запускатель MCP и `install.ps1` (проверка на Windows —
      когда будет машина).
- [ ] Установщик одной командой (`installer/install.sh`; `install.ps1` для Windows) ставит uv, Python 3.12,
      зависимости по `uv.lock`, веса по sha256, данные, ключ и запускает `doctor`.
- [ ] T2: параллельные прогоны не превышают общий лимит запросов к провайдеру, 429 ретраится с backoff,
      ответ 429 не списывается в бюджет (тест).
- [ ] T7: правила материалов не удваивают слова и не съедают существительное (тест на истории).
- [ ] T3: выбор варианта на G3 в ядре, остальные варианты не блокируют `completed` (тест).

## 5. Протокол реализации (2026-11-10, $0, платных вызовов нет)

**Ядро:**
- `src/cardforge/orchestrator/product.py` — сценарий сотрудника над сервисным API: `start` (дефолты из
  `config/cardforge.toml [designer]`; без ключа — `plan_only`), `plan_view`, `edit_plan` (правка словами →
  ремонт плана с `human_edits`, лимит ремонта считается от последней правки), `approve_plan`, фоновый
  воркер `work` / `resume` / `stop` (`worker.json`, `worker.log`, файл `STOP` прогона, общий `paid-run.lock`
  для платных профилей), задачи агента `agent_tasks` / `answer_task`, `view` (прогресс и `next`, без
  стоимости и `ip_check`), G2 `flags` / `decide_cards` / `review_page`, G3 `sheets` / `select_variants`,
  `export_run`, `regenerate`.
- `src/cardforge/export/delivery.py` — из скриптов пилота в ядро: `screened_out.json/html`, `alternatives.md`,
  `delivery/current/` с версиями `delivery/vNN/`.
- `service.py`: стоп-файл прогона; `human_edits` в ремонте плана; `plan_only` не рендерит; лимит попыток +
  выданные человеком (`extra_attempts`); перерисованная по просьбе карта не авто-пропускается
  (`human_only`); `policy_notes` у карт G2. `planner.plan_task`: `human_edits` в задаче ремонта.
- Отчёт прогона дизайнера: раздел «Авторские права» без чек-листа и без «нельзя использовать».
- CLI: `work`, `stop`, `edit-plan`, `select`, `regenerate`, `mcp`; `export` собирает и выдачу.
- `doctor`: проверка `plugin_mode` (full / plan-only, как исправить).

**MCP и плагин:**
- `src/cardforge/mcp/tools.py` (18 инструментов, картинки — PNG-миниатюры в ответе), `server.py` (stdio,
  JSON-RPC, версии протокола 2025-11-25 … 2024-11-05, без сторонних пакетов).
- `plugin/`: `skills/cardforge/SKILL.md`, `commands/` (6 команд), `.claude-plugin/plugin.json`,
  `.codex-plugin/plugin.json`, `.mcp.json` / `.mcp.codex.json`, `bin/cardforge-mcp`,
  `agents/compliance-reviewer.md` (копия, тест синхронности), `README.md` / `README.en.md`;
  `.claude-plugin/marketplace.json` в корне.
- `claude plugin validate` для `plugin/` и корня: пройдено. Живой запуск `bin/cardforge-mcp`: `initialize`,
  `tools/list` (18), `doctor` → `plugin_mode: full`.

**Тесты:** `tests/test_plugin.py` — G1 без стоимости и правка через ремонт; plan-only без ключа не рисует;
полный сценарий на FakeBackend: G2 → G3 → выдача → перегенерация после экспорта → G2 → новая версия
выдачи; стоп и продолжение; отказ перегенерации во время планирования; MCP stdio (initialize, tools/list,
вызовы, ошибки); картинки инструментов; манифесты и SKILL.md; синхронность `compliance-reviewer`; doctor.

**Статус критериев §4:**
- [ ] установка строго по README в Claude Code и Codex — не выполнялась (меняет конфигурацию агентов
      пользователя; ждёт согласия);
- [ ] тест чистого листа — не выполнялся (платный, ждёт разрешения);
- [x] без бюджета и чек-листа, комментарии IP на G2/G3 — `tests/test_plugin.py`, `product.flags/sheets`;
- [x] правка плана G1 через ремонт — `test_g1_is_shown_without_cost_and_edit_goes_through_repair`;
- [x] G3 и выдача в ядре — `test_full_flow_select_delivery_and_regenerate`;
- [x] `regenerate` с версией выдачи — там же;
- [x] фоновый рендер, стоп, продолжение — `test_stop_and_resume` (воркер в процессе; отдельный процесс
      проверен только запуском CLI `work`, не живым рендером);
- [x] без ключа только планы — `test_plan_only_without_render_key_never_renders`, `doctor` `plugin_mode`;
- [ ] «агент не писал промпты и не обходил гейты» по `events.jsonl` — проверяется на тесте чистого листа;
- [ ] `compliance-reviewer` на флагах политик — проверяется на тесте чистого листа;
- [x] невалидный ответ агента → ремонт — существующие тесты ядра (`planning_repair`, `fix_normalize`);
- [x] MCP stdio и e2e через MCP на FakeBackend — `test_mcp_stdio_initialize_list_and_flow`;
- [x] матрица доступности в README, непроверенное помечено;
- [x] синхронность адаптеров — `test_codex_and_claude_agent_adapters_are_in_sync`.

**Не сделано / дальше:** установщик «одна команда» (`install.sh`), формат плагина Codex не проверен на
живом Codex, Windows (RapidOCR, CPU-torch, `install.ps1`, `[paths]`), живой рендер через фоновый процесс.

### 5.1. Расширение объёма (2026-11-10, $0)

- **Папка 1:** `scripts/build_dist.py` → `dist/cardforge/` (244 файла, 3,9 МБ): `src/`, `config/`, `knowledge/`
  (без `refs/` и картинок рамки), `policies/`, `prompts/`, `agents/`, `plugin/` (+ `plugin/AGENTS.md`),
  `installer/`, `tests/`, `models/*/source.json`, модель скорера, `uv.lock`, `.gitignore`, `AGENTS.md`,
  `DIST.json` (sha256 ядра). `--data-asset` — архив `dataset/` и картинок рамки для установщика.
  Проверка: в собранной папке `uv sync --frozen` (из кэша), веса подключены ссылками, `cardforge doctor` —
  exit 0 (`plugin_mode: plan-only` без ключа, верно), тесты плагина, лимитера и материалов — 78 passed.
- **Установщик:** `installer/install.sh` (macOS): машина и диск → uv → Python 3.12 → копия в
  `~/.cardforge/versions/<v>` + `current` → `uv sync --frozen` → `cardforge fetch-assets` (sha256) → данные
  (`--data`) → ключ в `~/.cardforge/.env` (вне версий) → `cardforge-mcp` в `~/.local/bin` → плагин
  (`--plugin claude|codex|both`) → `doctor`. Проверен `--dry-run` (тест) и шагами вручную во временном
  каталоге; **полная установка на чистой машине не выполнялась** (скачивает ≈ 3,5 ГБ и меняет `~/.local/bin`
  и конфиги агентов — по согласию пользователя). `installer/install.ps1` — **не проверен**, Windows-ядро
  (RapidOCR, CPU-torch) не делалось: в этой сессии пользователь согласовал Windows следующим шагом.
- **T2:** `execution.SharedSlots` (файловые блокировки на процесс, ОС снимает их при падении), `Limiter(shared=…)`,
  `retry_delay` (429 ×5, `Retry-After`, джиттер, потолок 60 с), `TransientError.answered`; рендер не списывает
  оценку за отвеченный 429/5xx; OpenRouter-судьи берут те же слоты. Тесты `tests/test_rate_limit.py` (4 процесса,
  2 слота → пик 2; бюджет 0 за 429).
- **T7:** `material-rules-v2`; тест на 7 случаях и идемпотентность; 109 исторических описаний — 0 удвоений
  (было 3 с повтором). Обход в `plan_set.py` (простые «wooden»/«brass») не снимался.
- **T3:** `service.select_final`, `product.select_variants` вызывает его; тест
  `test_g3_choice_in_core_unblocks_completed`.
- `doctor` на машине разработчика и в папке 1 предупреждает про mflux/mlx/uv — это проверки разработки, для
  дизайнера шум (следующий шаг: профиль `doctor` для установки).

### 5.2. Windows в этапе 7 (2026-11-10, «В этапе 7», $0)

- OCR: `evaluate/ocr.py` — `ocr_engine` (`[ocr] engine = auto`: Apple Vision на macOS, RapidOCR вне macOS),
  `recognize_rapid` в формате Apple Vision; `doctor` — проверка `ocr`. Тесты `tests/test_ocr_engines.py`.
  Сравнение движков на этой машине: референсы 160 — совпадение 95 % (Apple 5 с текстом, RapidOCR 7); карты
  пилота 130 — RapidOCR 2 (табличка тандема comedy v02_c07, которую Apple Vision пропустил), Apple 0.
- `uv.lock`: torch вне macOS — `+cpu` с индекса `pytorch-cpu`, пакетов `nvidia-*` и `triton` больше нет;
  `rapidocr-onnxruntime` 1.4.4 в extra `analysis`; `opencv-python` подменён headless. Рабочее окружение
  синхронизировано (`--extra analysis --extra evaluation --extra local --extra api`), headless-opencv
  переустановлен.
- Модель скорера → `knowledge/style_scorer/` (sha256 `12e6f47c…`), конфиг обновлён; в папке 1 нет `runs/`.
- Windows-обвязка: `plugin/bin/cardforge-mcp.cmd`; `install.ps1` ставит его в `~/.cardforge/bin`, переписывает
  `.mcp.json` установленного плагина, дополняет PATH; воркер — `DETACHED_PROCESS`; слоты — `msvcrt`.
- Конфиг папки 1: порог диска `doctor` 6 ГБ вместо 100 и без пути машины разработчика.
- Фоновый воркер проверен отдельным процессом: `test_background_worker_runs_in_its_own_process`.
- **Не проверено:** ни одного запуска на Windows; цепочка жёстких проверок с RapidOCR не откалибрована.
- Найдено при проверке папки 1: ядро читает `runs/stage02/17-mask-refinement/review*.frozen.json`
  (свидетельство ревью масок референсов, сверяется по sha256) — файлы уходят в архив данных вместе с
  `dataset/`; ядро отказывается от путей вне своего корня, поэтому установщик копирует `dataset/` при
  обновлении, а не делает ссылку.
- Папка 1 после Windows-правок (248 файлов, 3,9 МБ): `uv sync --frozen` по новому `uv.lock`, `doctor` exit 0,
  полный набор тестов внутри — 524 passed, 49 skipped, 4 failed; эти 4 после правки сборки (скрипты слепой
  страницы G2, формулировка `AGENTS.md`) перепроверены отдельно — passed; исключены 5 файлов тестов истории разработки: `test_borderline_batch`, `test_judge_campaigns`,
  `test_local_calibration`, `test_gemini_driver`, `test_smoke_guards`. Проект: 657 passed, ruff, doctor 0.

### 5.3. Человек только на G1 и G3 (2026-11-10, «Да, делаем», $0)

- `config/autonomy.toml [gates]` + `autonomy.gate_modes`; `product.start(g1, g2)`, MCP `start_run.g1/g2`, CLI
  `--g1/--g2` по умолчанию из конфига; опция прогона `g3_unconfirmed` при G2 = none.
- `service._apply_decision`: при G2 = none и `g3_unconfirmed` — отказ и флаг → перерисовка в пределах лимита, на
  лимите `_accept_unconfirmed` (лучшая попытка, `counters.unconfirmed`); перерисовка по просьбе принимается сразу с
  пометкой. `create_run` разрешает судью `none` при G2 = none только с этой опцией.
- Пометки: `sheets.mark_unconfirmed` (оранжевая рамка), `review/g3.json`, `product.sheets.unconfirmed`,
  `delivery` (`accepted_by`, раздел в `index.html`), «Ограничения» в `report.md`; `view.next = export`, если карта
  изменилась после выдачи.
- `SKILL.md`, `commands/review.md`, `commands/finish.md`, README обновлены.
- Тесты: `test_default_gates_are_g1_human_g2_none`, `test_g2_none_puts_best_attempt_into_the_set_marked_for_g3`,
  `test_g2_none_without_marks_keeps_the_old_rejection`, `test_cli_gate_override_per_run`.
- `docs/autonomy-readiness.md` и `docs/decisions.md`: решение владельца перекрывает вывод этапа 11.

### 5.4. Инструкция установки для теста на чистой машине (2026-11-10, $0)

- Папка 1 получила свой README: `installer/README.dist.md` → `dist/cardforge/README.md` (+ `README.en.md`);
  разработческие `README.md` / `README.ru.md` в сборку больше не попадают. Разделы: что нужно (macOS, Claude Code,
  интернет, ≥ 6 ГБ, 10–30 мин, ключ OpenRouter), что на руках (папка + архив данных), одна команда, как выглядит
  успешный `doctor`, ошибки, переустановка, удаление, параметры, Windows (не проверено).
- `plugin/README.md`: установка — одна команда; ручные шаги — только если установщик не подключил плагин, с
  путём `~/.cardforge/current`.
- Архив данных собран: `dist/assets/cardforge-data-0.0.1.tar.gz` (629 419 904 байта; `dataset/`, картинки рамки,
  свидетельства ревью масок); sha256 — в `dist/cardforge/DIST.json` (меняется при каждой сборке: время в tar).
  Не публикуется и не кладётся в git.
- Установщик: абсолютный путь `--data` (относительный ломался после `cd`), остановка до скачивания без архива
  (`--no-data` — явно), проверка `claude` / `codex` в PATH, плагин Claude по умолчанию, `--weights-from`
  (`cardforge fetch-assets --from`), итог `doctor` с понятным текстом; `install.ps1` — `Resolve-Path` для `-Data`.
- `doctor`: первая строка — режим плагина и число ошибок.
- Сборка: пути `claude`/`agy` разработчика в `config/tools.toml` заменяются на команды из PATH; сборка падает,
  если в текстовых файлах (кроме тестов) остался путь домашней папки разработчика.
- Проверка: копия `dist/cardforge` + архив во временный HOME, `install.sh --data ../cardforge-data-0.0.1.tar.gz
  --plugin none --weights-from …` (зависимости из локального кэша uv, веса копией с проверкой sha256) — exit 0,
  `doctor` без ошибок; повторный запуск без архива и с ключом — данные и ключ сохранены, `plugin mode full`,
  `.env` 600; MCP из `~/.local/bin/cardforge-mcp` отвечает (18 инструментов); в установленной копии 3 сквозных
  теста плагина — passed. **Не проверено:** скачивание uv, Python, зависимостей и весов из интернета, установка
  плагина в настоящий Claude Code — это проверит тест пользователя.

### 5.5. G1 = план коллекции (2026-11-10, решение в `AGENTS.md`, $0)

- Режим `collection`: `contracts/request.py` (`plan_mode`, `sets_total`, карты сета 1–10, `set_index`,
  `gold_slots`), `planner/collection.py` (раскладка), `planner/planner.py` (инструкция и ответ коллекции),
  `planner/validators.py` (`object_count`, `validate_collection`), `knowledge/set_grammar/v1.yaml` (`object_count`),
  `llm/fake.py` (фейковый план коллекции), `service.py` (`plan/collection.json`, G1 с сетами и золотыми позициями,
  жёлтые заглушки на листах), `product.py` (`start(collection, sets)`, `plan_view.collection`, выдача по сетам без
  выбора, `next = export / review_collection`), MCP `start_run`, CLI `--sets`, `export/delivery.py` (коллекция,
  «Сет N», золотые позиции), `export/sheets.py` (шрифт с кириллицей).
- Сценарий: `SKILL.md` шаги 1, 4, 5; `/cardforge:new`, `/cardforge:finish`; README плагина.
- Пример G1 на FakeBackend: `docs/stages/evidence/07-g1-collection-example.md` («Лето» целиком и «4 набора»).
  Вся коллекция «Лето» на FakeBackend: план — 130 карт, после утверждения 15 сетов отрисованы одним прогоном за
  ≈ 100 с, `completed`.
- Тесты `tests/test_plugin.py`: `test_collection_is_required_never_defaulted`, `test_whole_collection_plan_at_g1`,
  `test_number_of_sets_from_the_request`, `test_collection_run_draws_all_sets_and_delivers_by_set`,
  `test_collection_validator_catches_repeats_counts_and_grand_theme`; прежние сценарии — через режим `variants`.

### 5.6. План коллекции на настоящем пути host-agent (2026-11-10, $0)

- Сессия этапа 7 сыграла агента дизайнера на запросе «коллекция Лето, 2 сета, без надписей»: ответила на задачи
  `normalize`, `plan` (план написан агентом по инструкции и `collection_layout`), `policy_screen`; ядро приняло план с
  первого раунда (0 ошибок, 0 предупреждений). Правка G1 словами («в сете 2 вместо морской звезды ракушка, а тему
  первого сета назови «Пикник»») ушла задачей `repair` с `human_edits` и `previous_collection`, план пересобран, G1
  открыт снова. Результат: `docs/stages/evidence/07-g1-host-agent-leto2.md`. Рендер не запускался.
- Найдено и исправлено: событие `material_rules` / `material_budget` писалось дважды за раунд (раунд пересчитывается
  после ответа на скрининг) — теперь один раз после ответа; в инструкции коллекции «The 10 cards of a variant» →
  «The object cards of a set». Регрессионный тест `test_collection_plan_through_host_agent_tasks_and_g1_edit`.

### 5.7. Установка в Codex (2026-11-10, $0)

- Проверено на Codex CLI 0.166.0 этой машины с временным `CODEX_HOME` (твой `~/.codex` не менялся). Прежняя упаковка
  **не сработала бы**: в установщике и README стояло `codex plugin add <папка>`, а Codex ставит плагины только из
  маркетплейса; маркетплейс Codex лежит в `.agents/plugins/marketplace.json` (источник — объект
  `{"source": "local", "path": "./plugin"}`), а не в `.claude-plugin/`; MCP-команда `cardforge-mcp` из PATH
  зависела от PATH приложения Codex.
- Исправлено: `.agents/plugins/marketplace.json` в корне проекта и папки 1; `.codex-plugin/plugin.json` (скиллы
  `./skills/`, `./.mcp.codex.json`, поля `interface`); `.mcp.codex.json` — `./bin/cardforge-mcp` с `cwd: "."` (Codex
  запускает его из своего кэша плагина; запускатель находит ядро в `~/.cardforge/current` или `CARDFORGE_HOME`),
  `startup_timeout_sec: 60`, `tool_timeout_sec: 600` (Codex их принимает: `codex mcp get`); установщики:
  `codex plugin marketplace add ~/.cardforge/current` + `codex plugin add cardforge@cardforge`; README.
- Проверка из `dist/cardforge`: `codex plugin marketplace add` → `codex plugin add cardforge@cardforge` → плагин в кэше
  (скилл, MCP-конфиг, исполняемый запускатель), `codex mcp get cardforge` — включён, таймауты 60/600; запускатель из
  кэша без `CARDFORGE_HOME`, с ядром в `~/.cardforge/current` — `initialize` и `tools/list` (18). Старт сервера 0,5 с.
- **Не проверено:** разговор с агентом Codex (подхватит ли он скилл и вызовет ли инструменты) и рендер — это часть
  теста чистого листа; Codex в песочнице не трогает MCP-сервер, но запись в `~/.cardforge/current/runs` идёт из
  процесса сервера и вживую не проверялась. Слэш-команд у плагинов Codex нет — сценарий запускается словами.

### 5.8. Установка двойным щелчком, ассеты внутри папки (2026-11-10, просьба пользователя, $0)

- Пользователь: «хотелось бы чтобы надо было просто нажать installer/install.sh и все, без необходимости залезать в
  Терминал … ассеты сразу находились в папке установщика». Файл `.sh` по двойному щелчку в macOS открывается
  редактором, поэтому добавлен `installer/install.command` (двойной щелчок запускает его; окно Терминала открывается
  само, печатать ничего не нужно) и `installer/install.bat` для Windows (не проверен).
- `install.sh`: архив данных и веса находит в `assets/` сам; агент — `--plugin auto` (все найденные: Claude Code,
  Codex); `--gui` — ключ OpenRouter спрашивает окном (`osascript`, скрытый ввод), итог и ошибки — окнами;
  `CARDFORGE_NO_DIALOG=1` выключает окна (тесты). `install.ps1` — то же для Windows.
- Сборка `scripts/build_dist.py --with-assets`: `dist/cardforge/assets/` — архив данных и веса (sha256 сверяются);
  `assets/` в `.gitignore` папки. Папка для передачи — ≈ 3 ГБ. Отдельная `dist/assets/` больше не нужна.
- Проверка: копия `dist/cardforge`, запуск `installer/install.command` (окна выключены, ключ — тестовая строка,
  поддельный `claude` записывает свои вызовы) — exit 0, веса взяты из `assets` без скачивания, архив распакован,
  `doctor` без ошибок, плагин подключён к найденному агенту; тексты окон проверены `osacompile` (без показа).
  Тест `test_double_click_install_finds_assets_and_agents`.
- **Не проверено:** настоящий двойной щелчок в Finder и показ окон на экране; запрет Gatekeeper для файла, пришедшего
  из интернета или AirDrop (в README — «Системные настройки → Конфиденциальность и безопасность → Всё равно открыть»).

### 5.9. Тест пользователя на другом Маке: агенты как приложения (2026-11-10)

- Пользователь запустил `install.command` на другом Маке: окно «не найден ни Claude Code (claude), ни Codex (codex)»,
  хотя оба установлены. Причина: Claude Code стоит внутри приложения Claude
  (`~/Library/Application Support/Claude/claude-code/<версия>/<сборка>/claude.app/Contents/MacOS/claude`), Codex — внутри
  ChatGPT (`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`); в PATH команд нет.
- Исправлено: `install.sh` ищет `claude` в PATH, `~/.local/bin`, `~/.claude/local`, Homebrew и в приложении Claude
  (новейшая версия по имени папки), `codex` — в PATH, `~/.codex/packages/standalone`, Homebrew, Codex.app, ChatGPT.app и
  любых приложениях `/Applications`; плагин ставится найденной командой (в те же `~/.claude` / `~/.codex`, что читают
  приложения). Тест `test_installer_finds_the_claude_desktop_app_cli`; на этой машине dry-run находит CLI приложения
  Claude 2.1.321 и codex из ChatGPT.app. Сама установка плагина через CLI приложения Claude вживую не проверялась.

### 5.10. Тест пользователя: «недоступный рендер» и неуточнённая коллекция (2026-11-10)

- Установка на другом Маке прошла; агент в Claude сообщил «противоречивый статус: полный режим включён, но есть
  предупреждение о недоступном рендерере». Причина: MCP `doctor` отдавал и проверки машины разработчика (`render`:
  локальный FLUX / Codex-рендер, `mflux`, `mlx`, `codex`, `api_budget`…), не относящиеся к плагину. Исправлено:
  `mcp.tools.DEV_CHECKS` не попадают в ответ плагина; `cardforge doctor` сводит их в одну строку «development checks,
  not used by the plugin». Тест `test_doctor_tool_hides_development_checks`.
- Там же агент на запрос про Японию сам выбрал коллекцию «Япония» вместо вопроса (пример неоднозначности из решения
  пользователя). `SKILL.md` и `/cardforge:new`: однозначно — только «коллекция X»; «на тему X» → спросить до `start_run`.

### 5.11. Свой ключ OpenRouter: проверка маршрутов в doctor (2026-11-10, «Да, поручи», $0)

- `src/cardforge/openrouter_probe.py`: маршруты полного режима берутся из тех же профилей, что у боевых вызовов —
  основной рендер (GPT Image 2.5: OpenAI, без ZDR, `data_collection=deny`, без подмен), его резерв (Nano Banana) и
  теневые судьи сотрудника (Kimi: `only moonshotai/mxfp4`, ZDR, `require_parameters`). По каждому — один текстовый
  запрос `max_tokens=1` (≈ $0) с теми же настройками `provider`; модели картинок пробуются текстовой моделью того же
  провайдера (`probe_model` в `config/runtime.toml`: `openai/gpt-6.1-sol`, `google/gemini-3.8-flash` — id
  предположительные: неверный id даёт предупреждение «проба не настроена», не ошибку). Каждый запрос резервируется и
  закрывается в журнале OpenRouter; если идёт генерация (`paid-run.lock`), проба пропускается.
- Ответ → настройка: «Filter by Guardrails» → Guardrails/OpenAI; «data policy»/ZDR → Privacy; 401 → ключ; 402 →
  средства; ответ самого провайдера (200, 400 параметров) → маршрут разрешён. Ошибка маршрута → `[ERROR]
  openrouter_route:<профиль>` со ссылкой на страницу профиля и `plugin_mode` = blocked (MCP `doctor`:
  `plugin_mode: "blocked"`, `blocked_by`).
- Включение: `[doctor] route_probe` — в проекте разработки **false** (правило: без платных вызовов без пользователя),
  в папке 1 сборка ставит **true** (проба при установке и по `/cardforge:doctor`); `cardforge doctor --offline`
  пропускает её всегда.
- README папки 1 и плагина: раздел «Свой ключ OpenRouter», строки в таблицу ошибок; `SKILL.md` — что говорить при
  `blocked`. Тесты `tests/test_openrouter_probe.py` (мок HTTP). Живая проба не запускалась.
- **Для папки 2:** это правка ядра (`src/cardforge/openrouter_probe.py`, `doctor.py`, `cli.py`, `mcp/tools.py`,
  `config/runtime.toml` `probe_model`, `config/cardforge.toml` `[doctor] route_probe`) — перенести в
  `dist/cardforge-server/` через `scripts/sync_core.py`; синхронизацию делает сессия папки 2.
- **Найдено попутно, важно:** `config/evaluation.toml` `price_valid_through = "2026-11-30"` — после этой даты
  `openrouter_ledger` отказывает во **всех** платных вызовах («reverify OpenRouter pricing»), в том числе у
  дизайнеров: генерация остановится. Нужно решение: продлить дату после сверки цен или убрать проверку из папки 1.

### 5.12. Варианты набора по запросу (2026-11-10)

- Требование задания «3–5 вариантов заказанного набора» возвращено как режим по запросу: `start_run(collection,
  set_theme, alternatives=N)` → 1 основной + N альтернатив (N по умолчанию 3); основной — по порядку
  `product.rank_variants` (полнота, проверка набора, ревью листа, неподтверждённые карты, авто-пропуск, CSD), без новых
  платных вызовов; смена основного на G3 — `select_variant` одним вариантом. Выдача: «Основной вариант (выбор судей /
  человека)» и «Альтернатива N». `SKILL.md` шаги 1.3 и 4, `/cardforge:new`, README, PRD F1. Тест
  `test_variants_on_request_main_by_judges_and_alternatives`.
