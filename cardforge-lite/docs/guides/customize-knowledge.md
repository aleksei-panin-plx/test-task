# Как настроить знания CardForge Light: фоны, категории, описание карточки

Эта инструкция — для того, кто меняет правила планировщика: какие фоны допустимы, как фон контрастирует с
объектом, что значат категории 1–4 и сколько их должно быть в сете, насколько подробно описывать объект.
Почти всё правится в текстовых файлах, без кода. Пути даны от папки `cardforge-lite/`; в установленной
копии это `~/.cardforge-lite/current/`.

> После любой правки — проверка (раздел 5): тесты и dry-run на fake LLM. В установленную копию правка
> попадает, если повторно запустить установщик из исправленной папки; файлы в `~/.cardforge-lite/current/`
> тоже можно править на месте, но следующая установка их перезапишет.

## 1. Карта знаний: где что лежит

| Что | Файл | Кто читает |
|---|---|---|
| Определения категорий 1–4 (и 5 — золотые) | `knowledge/catalog/vocabulary.yaml` → `category` | задача `plan` (`vocabulary.category`), агент |
| Допустимые мотивы фона | `knowledge/catalog/vocabulary.yaml` → `motif` | задача `plan`, валидатор |
| Допустимые цвета фона | `knowledge/catalog/vocabulary.yaml` → `color` | задача `plan`, валидатор |
| Материалы | `knowledge/catalog/vocabulary.yaml` → `materials` | задача `plan`, валидатор (предупреждение) |
| Какие мотивы разрешены какой категории | `knowledge/set_grammar/v1.yaml` → `category_motifs` | задача `plan`, валидатор, `doctor` |
| Сколько категорий, масштабов, типов и цветов должно быть в сете | `knowledge/set_grammar/v1.yaml` → `min_object_*`, `object_count` | задача `plan`, валидатор |
| Золотые позиции по сетам | `knowledge/catalog/set_statistics.json` (категория 5) | `planner/collection.py` |
| Правило контраста фона с объектом, смысл категорий словами, описание карточки | `prompts/planner/card_rules.md` | текст `instruction` задачи `plan` |
| План коллекции (16 сетов, золотые, сет 16) | `prompts/planner/collection.md` | текст `instruction` задачи `plan` |
| Нормализация брифа, ремонт, правки человека | `prompts/planner/normalize.md`, `repair.md`, `human_edits.md` | `instruction` задач `normalize` / `repair` |
| Примеры (few-shot): три эталонных сета 1, 7, 13 | `knowledge/catalog/cards.jsonl` (+ `human_review.jsonl`) | задача `plan` (`few_shot`) |
| Стиль: правила и отзывы людей | `knowledge/style_profile/reference_cards_v1.yaml`, `human_feedback_v1.json` | задача `plan` (`style`) |
| Язык материалов в английском описании (`object_description`) | `config/material_rules.toml`, `config/material_budget.toml` | планировщик после ответа агента |
| Политики (IP, рынки) | `policies/ip/baseline.md`, `policies/culture/*.md` | задачи `plan` и `policy_screen` |

Формат YAML-файлов знаний — JSON-совместимый YAML (как в файле сейчас): кавычки, запятые, скобки. Можно
писать и обычным YAML — читает его `yaml.safe_load`.

## 2. Что зашито в коде (и где)

Инструкция планировщика в полной версии (папка 1) — строки в коде: `COLLECTION_INSTRUCTION` и текст в
`src/cardforge/planner/planner.py::plan_task`. **В Light она вынесена в файлы `prompts/planner/*.md`**:
`plan_task` склеивает `collection.md` + `card_rules.md`, при ремонте добавляет `repair.md`, при правке человека —
`human_edits.md`. HTML-комментарии `<!-- … -->` в задачу не попадают, переносы строк схлопываются в пробелы.

В коде остались (менять — только с тестами):

| Что | Где |
|---|---|
| Поля карточки и их типы: `category` 1–4, `scale_class`, `object_type` (списки значений), `main_object`, `environment` | `src/cardforge/contracts/request.py::CardDraft` |
| Правила полей по категории: 1 — без поверхности; 2–3 — с поверхностью; 4 — с окружением; окружение только у 4 | `CardDraft.category_fields` |
| Какие слова считаются «пустым окружением» (`—`, `-`, `none`, `нет`) | `contracts/request.py::NO_ENVIRONMENT` |
| Проверка «в `main_object` назван цвет» (предупреждение) | `src/cardforge/planner/validators.py::COLOR_WORDS` |
| Число предметных карт и золотых по сетам, сет 16 = только тема | `src/cardforge/planner/collection.py` |
| Число раундов ремонта (2) | `planner.py::MAX_REPAIR_ROUNDS` |
| Какие разделы словаря уходят в задачу (`category` 1–4, `motif`, `color`, `materials`) | `planner.py::plan_task` |
| Порог сходства сетов (Jaccard 0,3), цель 5 цветов фона | `validators.py::MAX_JACCARD`, `TARGET_COLORS` |
| Колонки таблиц Markdown/CSV | `src/cardforge/orchestrator/tables.py::COLUMNS`, `CSV_COLUMNS` |
| Инструкция задачи `policy_screen` | `src/cardforge/policy/screener.py::semantic_task` (это политика, не знание о фонах) |

## 3. Фоны: мотивы, цвета, контраст с объектом

### 3.1. Добавить или убрать мотив

1. `knowledge/catalog/vocabulary.yaml` → раздел `motif`: добавьте ключ и описание словами, например
   `"dots": "Повторяющийся горох; крупные редкие точки."`. Ключ — латиницей, без пробелов: его пишет агент
   в `background.motif`, и он попадает в таблицу.
2. `knowledge/set_grammar/v1.yaml` → `category_motifs`: впишите новый мотив в списки категорий, которым он
   разрешён (обычно `"1"`, `"2"`, `"3"`). Мотив, которого нет ни в одном списке, валидатор не пропустит.
3. Убрать мотив — удалите его из обоих мест. `doctor` скажет об ошибке, если в `category_motifs` остался мотив,
   которого нет в словаре.

### 3.2. Цвета фона

`vocabulary.yaml` → `color`: ключ — имя цвета, значение — пояснение (для `cyan`/`blue` там граница оттенков).
Добавленный цвет сразу допустим в плане. Если в `config/material_rules.toml` используется исправление «объект
только серый/чёрный» (`sole_neutral`), добавьте новый цвет и в `[sole_neutral.accent_for_background]` —
какой акцентный цвет объекта ставить на этот фон.

### 3.3. Правило контраста фона с объектом

Сейчас: цвет фона противоположен или комплементарен главному цвету объекта (коричневый/оранжевый/жёлтый
объект — на синем, голубом или фиолетовом; красный — на зелёном или голубом), никогда не в той же гамме;
мотив фона яркий и читаемый, но светлее и менее насыщен, чем объект. Это **текст** в
`prompts/planner/card_rules.md`, абзац «Keep background motifs bold…». Меняйте его словами — например,
добавьте пары цветов или запрет («never a red object on pink»). Главный цвет объекта агент называет в
`main_object` (раздел 6) — по нему человек и проверяет контраст в таблице.

Автоматической проверки пары «цвет объекта ↔ цвет фона» в Light нет (цвет объекта — свободный текст);
валидатор лишь предупреждает, если в `main_object` цвет не назван.

### 3.4. Фон у категории 4

У категории 4 фоновой плашки нет: объект показан в окружении. Это задано в двух местах: `category_motifs."4"
= ["scene"]` (валидатор) и абзац «Categories…» в `card_rules.md` (агенту: мотив `scene`, цвет = главный цвет
окружения — небо, вода, стена). Чтобы разрешить категории 3 только плашки, уберите `"scene"` из
`category_motifs."3"`.

## 4. Категории 1–4

### 4.1. Что значит каждая

| Категория | Смысл (из `vocabulary.yaml` → `category`) | Поля карточки |
|---|---|---|
| 1 | объект без мелких деталей висит в воздухе; простой фон | `surface` пусто, `environment` пусто |
| 2 | объект стоит на простой поверхности; простой вертикальный фон | `surface` = `plain` |
| 3 | объект стоит на узнаваемой реалистичной поверхности (дерево, камень, трава, вода…); простой задник | `surface` = поверхность |
| 4 | объект в центре реалистичной окружающей обстановки (сцена) | `environment` — обязательно, фон `scene` |
| 5 | золотая сюжетная карта — **не планируется**, только отмечается место | — |

Определения 1–4 уходят агенту в задачу `plan` (`vocabulary.category`) — правьте текст в `vocabulary.yaml`.
Краткий пересказ для агента — абзац «Categories…» в `prompts/planner/card_rules.md`; держите оба места
согласованными. Правила полей (1 — без поверхности, 4 — с окружением) — в коде (`CardDraft.category_fields`).

### 4.2. Правила наличия в сете

`knowledge/set_grammar/v1.yaml`:

- `object_count.all_categories_from` (сейчас 8): при стольких предметных картах в сете обязательны все
  четыре категории; при меньшем числе (сеты с золотыми позициями) одна категория может отсутствовать
  (предупреждение), две — ошибка.
- `min_object_scales` (3), `min_object_types` (4), `min_object_colors` (4): минимум разных масштабов, типов
  объекта и цветов фона; для сетов с золотыми позициями порог снижается (`min(правило, n − 2)`, для масштабов
  `n − 3`) — формула в `validators.py::object_count_rules`.
- `rules[].statement` — эти же правила словами; они тоже уходят агенту (`set_grammar` в задаче).

Точное число карт каждой категории в сете Light не задаёт. Если нужно — это поле `category_counts` запроса
(`{"1": 3, "2": 2, "3": 2, "4": 3}`), сейчас его не передают ни MCP, ни CLI: потребуется правка
`orchestrator/run.py::create`.

## 5. Как проверить правку

Из папки `cardforge-lite/` (окружение уже создано установщиком или командой `uv sync`):

```bash
.venv/bin/pytest -q
```

```bash
.venv/bin/ruff check src tests
```

```bash
.venv/bin/cardforge doctor --root .
```

```bash
.venv/bin/cardforge plan "Лето" --sets 2 --fake --root .
```

- `pytest` — все тесты, без сети. Правку знаний проверяют `tests/test_knowledge_edits.py` (правка
  `prompts/`, словаря и грамматики доходит до задачи и валидатора) и `tests/test_plan_contract.py` (поля карточки).
- `doctor` — читает словарь, грамматику, few-shot и все `prompts/planner/*.md` теми же функциями, что и
  планировщик; битый YAML, пустой промпт или мотив вне словаря — `[ERROR]`.
- `plan --fake` — dry-run: офлайн-планировщик `FakeLLM` проходит весь путь (нормализация → план → валидатор →
  политики → таблицы) и печатает таблицы. Fake **не читает** инструкцию — он проверяет, что знания
  загружаются, а валидатор принимает или отвергает план. Если вы ужесточили правило, которому fake не
  следует, dry-run закончится `failed` со списком `unmet_rules` — это и есть доказательство, что правило
  работает. Посмотреть, что получит агент: файл `runs/<run_id>/tasks/plan.task.json` из прогона без `--fake`.
- Живая проверка — один план в Claude Code / Codex: `/cardforge-lite:new коллекция Лето, 2 сета` (платных
  вызовов нет: план пишет ваш агент по подписке).

## 6. Подробное описание карточки: `main_object` и `environment`

### 6.1. Где поля

| Поле | Схема | Инструкция агенту | Таблица |
|---|---|---|---|
| `main_object` — основной объект: форма, пропорции, главный цвет | `contracts/request.py::CardDraft.main_object` (обязательно, ≥ 3 символов) | `card_rules.md`, абзац «Describe every card…» | колонка «Основной объект», CSV `main_object` |
| `environment` — второстепенное окружение, только категория 4 | `CardDraft.environment` (у 4 обязательно, у 1–3 пусто; `—`/`none` → пусто) | там же | колонка «Окружение» (`—` у 1–3), CSV `environment` |

Валидатор: `CardDraft.category_fields` (окружение есть ⇔ категория 4) и `validators.py::validate_variant`
(предупреждение «main_object names no main colour»). Поля проходят и скрининг политик
(`policy/screener.py::card_fields`).

### 6.2. Как поменять детализацию

Детализация задаётся текстом абзаца «Describe every card for the content table…» в
`prompts/planner/card_rules.md`. Примеры правок:

- **короче**: «`main_object` — the object and its main colour, at most six words»;
- **подробнее**: «… its form, proportions, main colour, one secondary colour, the material of the main part
  and one distinctive detail; at most 20 words»;
- **окружение подробнее**: «`environment` — two or three secondary elements of the scene and the time of
  day»; запрет: «never people or animals in `environment`».

Если правило должно стать жёстким (например, обязательное слово «пропорции» или длина), добавьте проверку в
`validators.py::validate_variant` (ошибка уходит агенту на ремонт) и тест в `tests/test_plan_contract.py`.
Слова, по которым ищется цвет, — `validators.py::COLOR_WORDS`. Переименование поля — правка кода (схема,
таблицы, тесты), не только текста.

### 6.3. Пример строки таблицы

Markdown (чат и `tables/plan.md`):

| № | Объект | Основной объект | Категория | Окружение | Фон | Политики |
|---|---|---|---|---|---|---|
| v01_c02 | Надувной круг | круглый пухлый круг, широкий и плоский, красный с белыми полосами | 1 · в воздухе | — | rays / cyan | — |
| v01_c09 | Спасательная вышка | высокая узкая вышка на сваях, жёлтая с красной крышей | 4 · в окружении | песчаный пляж у моря, ясное небо | scene / blue | — |

CSV (`tables/cards.csv`, UTF-8 с BOM — открывается в Excel):

```text
set_index,set_theme,card_id,position,slot,object_name,main_object,category,environment,background_motif,background_color,surface,object_description,policy_flags
1,Пляж,v01_c09,9,object,Спасательная вышка,"высокая узкая вышка на сваях, жёлтая с красной крышей",4,"песчаный пляж у моря, ясное небо",scene,blue,sand,"tall yellow lifeguard tower on stilts, red roof",
```

Золотые позиции идут строками `slot = gold` без полей, сет 16 — одной строкой `slot = grand_set`.
