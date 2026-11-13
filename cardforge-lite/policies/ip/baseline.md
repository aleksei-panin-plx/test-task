---
created: 2026-11-03
updated: 2026-11-03
type: reference
status: draft
topics: [content-policy, ip, copyright]
project: CardForge
source: Technical Architecture §9.1, §9.8; Implementation Roadmap stage 3
policy_id: ip.baseline
policy_kind: ip
region: null
policy_version: 0.1.0
includes: []
verified: 2026-11-03
review_after: 2027-05-03
---

# IP baseline: базовые авторские права и товарные знаки

Обязательный пакет CardForge. Активен в каждом прогоне и **не отключается запросом**: ни через `markets`, ни через `policy_packs`, ни через режим гейтов. Формат файла — [[Technical Architecture]] §9.2.

**Это не юридическая экспертиза.** Пакет консервативен: он отсекает очевидные риски, но не доказывает, что набор чист. Полнота автоматических проверок в MVP не измерена. В сценарии плагина (решение пользователя 2026-11-10) человек чек-лист не подтверждает: сомнения IP-проверки показываются ему комментариями на G2/G3, а `block` по-прежнему не снимается.

## Принцип

- **Жанровый архетип — можно, конкретная франшиза — нельзя.** Ковбойская шляпа — да; шляпа конкретного героя конкретного фильма — нет. Плащ супергероя — да; плащ с эмблемой конкретного героя — нет.
- **Пожелание «как в X» читается как жанр.** «Супергерои, как в Marvel» → супергеройский жанр без объектов, имён и символики Marvel. Конфликт пишется в `CollectionRequest.conflicts`, планировщик выбирает архетипы.
- **Проверка на трёх стадиях:** план (исключить франшизные объекты), промпт (никаких названий франшиз, брендов и героев), QA (вопрос VLM о сходстве с франшизой; срабатывание уходит человеку как `review`).

## Чек-лист авторских прав

Основной механизм IP в MVP ([[Technical Architecture]] §9.8). Это единственный экземпляр текста: агент, роль `compliance-reviewer` и экраны интерфейсов ссылаются на этот раздел и не копируют его.

1. Нет узнаваемых персонажей, лиц и силуэтов актёров.
2. Нет логотипов, товарных знаков, названий фильмов, студий и брендов, в том числе стилизованных.
3. Нет фирменного реквизита, костюмов, транспорта и оружия конкретных франшиз — допустим только жанровый архетип.
4. Нет постеров, кадров, обложек и узнаваемых композиций конкретных фильмов.
5. Нет надписей и шрифтов, повторяющих фирменные.
6. Референсы только из датасета заказчика, сторонние изображения не используются.

**Где применяется:**
- агент сверяет план с чек-листом до G1;
- PolicyInspector задаёт IP-вопрос по каждой картинке; сомнение (`uncertain`) уходит человеку на G2 комментарием у карты и видно у варианта на G3;
- с 2026-11-10 (решение пользователя) человек чек-лист не подтверждает. Поле `ip_check` в `manifest.json` остаётся `pending`; `cardforge policy confirm-ip` — только для разработки и только словами человека.

## Правила

Ключевые слова — дешёвый первый фильтр, а не исчерпывающий список франшиз. Основную работу на плане делает семантическая проверка по `match.semantic`.

```yaml policy
rules:
  - id: IP-CHR-01
    title: "Узнаваемые персонажи, лица и силуэты актёров"
    severity: block
    stages: [plan, prompt]
    applies_to: [object, background, theme, character]
    match:
      keywords: ["spider-man", "spiderman", "batman", "superman", "iron man", "captain america", "hulk", "wolverine", "deadpool", "darth vader", "yoda", "harry potter", "mickey mouse", "james bond", "indiana jones", "terminator", "shrek", "человек-паук", "бэтмен", "супермен", "железный человек", "капитан америка", "халк", "росомаха", "дэдпул", "дарт вейдер", "гарри поттер", "микки маус", "джеймс бонд", "индиана джонс", "терминатор", "шрек"]
      semantic: "Конкретный персонаж франшизы, лицо или силуэт реального актёра, а также объект, по которому однозначно узнаётся конкретный герой"
    prompt_constraint: "an original generic genre design not tied to any existing hero"
    qa_question: null
    alternatives: ["жанровый архетип без имени героя: плащ супергероя без эмблемы", "маска-домино", "шляпа ковбоя без опознавательных деталей"]
    rationale: "Персонажи и внешность актёров охраняются авторским правом и правом на изображение."
    sources: [TA-9.1]

  - id: IP-BRD-01
    title: "Названия франшиз, фильмов, студий и брендов; логотипы и товарные знаки"
    severity: block
    stages: [plan, prompt]
    applies_to: [object, background, text, theme]
    match:
      keywords: ["marvel", "dc comics", "avengers", "justice league", "x-men", "disney", "pixar", "dreamworks", "warner bros", "universal pictures", "paramount", "lucasfilm", "star wars", "star trek", "jurassic park", "ghostbusters", "hogwarts", "pokemon", "transformers", "lego", "coca-cola", "marvel studios", "марвел", "мстители", "лига справедливости", "люди икс", "дисней", "пиксар", "уорнер", "звездные войны", "звездный путь", "парк юрского периода", "охотники за привидениями", "хогвартс", "покемон", "трансформеры", "лего", "кока-кола", "logo", "логотип", "trademark", "товарный знак"]
      semantic: "Название, логотип или товарный знак конкретной франшизы, фильма, студии или бренда, в том числе стилизованные или узнаваемые по форме"
    prompt_constraint: "unbranded original design"
    qa_question: null
    alternatives: ["жанровый архетип без названия", "нейтральная пиктограмма вместо эмблемы"]
    rationale: "Товарные знаки и названия франшиз нельзя использовать без лицензии; стилизация не снимает риск."
    sources: [TA-9.1]

  - id: IP-PRP-01
    title: "Фирменный реквизит, костюмы, транспорт и оружие конкретных франшиз"
    severity: block
    stages: [plan, prompt]
    applies_to: [object, background]
    match:
      keywords: ["lightsaber", "mjolnir", "batmobile", "delorean", "tardis", "proton pack", "infinity gauntlet", "sorting hat", "elder wand", "one ring", "vibranium shield", "web shooter", "световой меч", "мьельнир", "бэтмобиль", "делориан", "тардис", "перчатка бесконечности", "распределяющая шляпа", "бузинная палочка", "кольцо всевластья", "щит капитана"]
      semantic: "Предмет, костюм, транспорт или оружие, которые однозначно узнаются как принадлежащие конкретной франшизе или герою, а не жанру в целом"
    prompt_constraint: "a generic genre prop with an original shape and palette"
    qa_question: null
    alternatives: ["жанровый архетип: светящийся меч без фирменной рукояти", "молот кузнеца", "ретро-автомобиль без узнаваемого дизайна"]
    rationale: "Фирменный дизайн реквизита охраняется так же, как персонаж; допустим только архетип жанра."
    sources: [TA-9.1]

  - id: IP-PST-01
    title: "Постеры, кадры, обложки и узнаваемые композиции фильмов"
    severity: block
    stages: [plan, prompt]
    applies_to: [object, background, theme]
    match:
      keywords: ["movie poster", "film poster", "film still", "dvd cover", "blu-ray cover", "iconic scene", "постер фильма", "афиша фильма", "кадр из фильма", "обложка диска", "культовая сцена"]
      semantic: "Воспроизведение постера, кадра, обложки или узнаваемой композиции конкретного фильма"
    prompt_constraint: "an original composition"
    qa_question: null
    alternatives: ["безымянная афиша с абстрактным узором", "кинохлопушка без надписей", "катушка плёнки"]
    rationale: "Постеры и кадры — охраняемые произведения; узнаваемая композиция тоже риск."
    sources: [TA-9.1]

  - id: IP-QA-01
    title: "Сходство изображения с франшизой, брендом или фирменным шрифтом"
    severity: review
    stages: [qa]
    applies_to: [object, background, text, character]
    match:
      keywords: []
      semantic: "Картинка напоминает конкретную франшизу, бренд, героя, постер или фирменную надпись"
    prompt_constraint: null
    qa_question: "Does the object, any marking, lettering or composition in the image resemble a specific film franchise, character, studio, brand or logo rather than a generic genre item?"
    alternatives: ["перегенерация с жанровым архетипом"]
    rationale: "Полнота VLM-проверки не измерена, поэтому срабатывание уходит человеку, а не решается автоматически (TA §9.8)."
    sources: [TA-9.8]
```

## Источники

- **TA-9.1** — [[Technical Architecture]] §9.1: содержание IP baseline.
- **TA-9.8** — [[Technical Architecture]] §9.8: проверка базовых авторских прав в MVP.

Пакет не заменяет юриста. Перед коммерческим выпуском набор проверяет ответственный за права человек.
