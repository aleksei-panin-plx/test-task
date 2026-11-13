---
created: 2026-10-31
updated: 2026-11-03
type: reference
status: draft
topics: [content-policy, localization, china]
project: Test task
source: web research
policy_id: culture.cn
policy_kind: culture
region: CN
policy_version: 0.1.1
includes: []
verified: 2026-10-31
review_after: 2027-01-31
---

# Китай (материковый): базовые культурные ограничения

Пакет политик для CardForge. В репозитории лежит как `policies/culture/china.md`. В CardForge Light активен всегда, как и все пакеты из `policies/culture/` (в полной версии — когда в запросе `markets` есть `CN` или пакет указан в `policy_packs`). Формат файла и порядок применения описаны в [[Technical Architecture]] §9.

**Статус:** базовый тестовый пакет.
- Пакет сознательно консервативный: запрещает больше, чем строго требуется. Для предметных карточек казуальной игры это дешевле, чем отказ при проверке.
- Это не юридическая экспертиза. Перед релизом на китайском рынке пакет проверяет эксперт по локализации или юрист.
- Часть источников 2019–2020 годов (см. «Источники»), поэтому `review_after` стоит на 2027-01-31.

## Область применения

- **Предметные карточки категорий 1–4:** объект, фон, поверхность, окружение, диегетический текст.
- **Тема сета и пожелания продюсера:** проверяются на этапе нормализации брифа.
- **Персонажи (категория 5)** вне рамок тестового. Правила с `applies_to: [character]` заложены на будущее.

## Уровни и стадии

| Severity | Что значит |
|---|---|
| `block` | Нельзя ни в плане, ни на картинке. Объект заменяется, картинка перегенерируется |
| `review` | Допустимо только после решения человека. Без человека — консервативная замена (`review_policy: replace`) |
| `advise` | Лучше выбрать альтернативу; в отчёте остаётся заметка |

Стадии:
- `plan` — проверка объектов и темы до генерации;
- `prompt` — позитивные формулировки в промпте без названия запрещённого;
- `qa` — вопрос к VLM-инспектору и детекторы.

## Правила

```yaml policy
rules:
  # --- Политика, государство, история ---
  - id: CN-POL-01
    title: "Реальная география: карты, глобусы, очертания стран и границ"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background]
    match:
      keywords: ["map", "world map", "globe", "atlas", "country outline", "карта мира", "глобус", "атлас", "контур страны"]
      semantic: "Любое изображение реальной географии: карта, глобус, береговая линия, очертания стран или границ, в том числе стилизованные или частичные, в том числе как узор фона"
    prompt_constraint: "plain decorative background without any geography"
    qa_question: "Does the image show any real-world map, globe, coastline or country outline, even stylized, partial or as a background pattern?"
    alternatives: ["компас", "подзорная труба", "свёрнутый свиток без изображения", "дорожный указатель без надписей"]
    rationale: "Карты с неверными границами (Тайвань, Гонконг, Южно-Китайское море) — известная причина отказа. Надёжнее не изображать реальную географию вообще."
    sources: [S3]

  - id: CN-POL-02
    title: "Вымышленные карты (карта сокровищ)"
    severity: review
    stages: [plan, qa]
    applies_to: [object]
    match:
      keywords: ["treasure map", "карта сокровищ", "fantasy map"]
      semantic: "Вымышленная карта, очертания которой могут напоминать реальные территории"
    prompt_constraint: "abstract dotted path with an X mark, no landmasses"
    qa_question: "Does the map in the image contain landmass shapes that could resemble real countries or islands?"
    alternatives: ["свиток с пунктирной тропой и крестиком без материков", "сундук с сокровищами", "компас"]
    rationale: "Вымышленная карта допустима, но случайное сходство очертаний с реальными территориями создаёт риск из CN-POL-01."
    sources: [S3]

  - id: CN-POL-03
    title: "Государственные символы: флаги, гербы, эмблемы, военная форма и знаки различия"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, text]
    match:
      keywords: ["national flag", "flag", "coat of arms", "emblem", "military insignia", "army uniform", "флаг", "герб", "эмблема", "погоны", "военная форма"]
      semantic: "Национальные флаги и гербы любых стран, символы госорганов и армий, военная форма с опознавательными знаками"
    prompt_constraint: "festive bunting in plain solid colors without symbols"
    qa_question: "Does the image show a national flag, coat of arms, government emblem or military insignia of any country?"
    alternatives: ["гирлянда флажков однотонных цветов", "вымпел без символов", "медаль без надписей и символов"]
    rationale: "Неуважительное или неверное изображение государственной символики и армии — прямой повод для отказа."
    sources: [S1, S3]

  - id: CN-POL-04
    title: "Реальные политики, политические события, лозунги и протестная символика"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, text, theme]
    match:
      keywords: ["politician", "protest", "demonstration", "slogan", "banner", "политик", "протест", "демонстрация", "лозунг", "транспарант"]
      semantic: "Портреты и узнаваемые образы реальных политиков, отсылки к политическим событиям, протестная символика, политические лозунги"
    prompt_constraint: "neutral everyday props only"
    qa_question: "Does the image reference a real politician, a political event, a protest or a political slogan?"
    alternatives: ["нейтральный бытовой реквизит той же сцены"]
    rationale: "Содержание, затрагивающее государственное единство, руководство и политически чувствительные события, запрещено."
    sources: [S1, S3]

  - id: CN-POL-05
    title: "Жёлтый зонт как отдельный герой-объект"
    severity: advise
    stages: [plan]
    applies_to: [object]
    match:
      keywords: ["yellow umbrella", "жёлтый зонт"]
      semantic: "Жёлтый зонт как центральный объект карточки"
    prompt_constraint: null
    qa_question: null
    alternatives: ["зонт другого цвета", "дождевик", "резиновые сапоги"]
    rationale: "Жёлтый зонт — символ протестов 2014 года в Гонконге. Сам по себе не запрещён, но цвет легко поменять."
    sources: [S6]

  - id: CN-HIS-01
    title: "Милитаристская и экстремистская символика, искажение истории, вторжение в Китай"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, theme]
    match:
      keywords: ["rising sun flag", "swastika", "nazi", "imperial army", "invasion", "флаг восходящего солнца", "свастика", "нацист", "вторжение"]
      semantic: "Флаг восходящего солнца, нацистская и фашистская символика, прославление войны и агрессии, изображение иностранного вторжения в Китай, искажение истории Китая, насмешка над историческими героями"
    prompt_constraint: "generic period props without insignia"
    qa_question: "Does the image contain militarist or extremist symbols (rising sun flag, swastika, fascist insignia) or glorify war or invasion?"
    alternatives: ["каска без знаков", "полевой бинокль", "полевая рация", "армейская палатка", "джип без опознавательных знаков"]
    rationale: "Пропаганда фашизма и войны, иностранное вторжение и искажение истории прямо названы среди запрещённого."
    sources: [S1, S3]

  # --- Насилие ---
  - id: CN-VIO-01
    title: "Кровь любого цвета"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background]
    match:
      keywords: ["blood", "bloody", "gore", "splatter", "кровь", "окровавленный", "брызги крови"]
      semantic: "Кровь, брызги, потёки или лужи любого цвета, в том числе зелёная, чёрная или «бутафорская»"
    prompt_constraint: "clean, spotless surfaces"
    qa_question: "Does the image show blood or blood-like splatter, drips or stains of any color?"
    alternatives: ["драматичный свет", "разбитое стекло без следов", "киношная бутафория без крови (клапперборд)"]
    rationale: "С 2019 года запрещена кровь любого цвета; перекрашивание в зелёный больше не принимается."
    sources: [S2, S3]

  - id: CN-VIO-02
    title: "Трупы, скелеты, черепа, кости, мумии"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, character]
    match:
      keywords: ["skeleton", "skull", "bones", "corpse", "mummy", "coffin with body", "скелет", "череп", "кости", "труп", "мумия"]
      semantic: "Скелеты, черепа и кости (в том числе декоративные и мультяшные), трупы, мумии, гробы с телом, черепа как узор фона"
    prompt_constraint: "cheerful, lively props"
    qa_question: "Does the image show a skeleton, skull, bones, a corpse or a mummy, including cartoonish, decorative or pattern forms?"
    alternatives: ["старинный сундук", "подсвечник со свечой", "старинный ключ", "фонарь"]
    rationale: "Трупы запрещены прямо. По скелетам источники расходятся (формального запрета может не быть), но издатели их убирают. Пакет консервативен и относит их к block."
    sources: [S2, S3, S7]

  - id: CN-VIO-03
    title: "Жестокость: орудия пыток и казни, расчленение"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background]
    match:
      keywords: ["torture", "guillotine", "gallows", "noose", "chainsaw", "пытка", "гильотина", "виселица", "петля", "бензопила"]
      semantic: "Орудия пыток и казни, расчленение, сцены жестокости"
    prompt_constraint: "harmless everyday props"
    qa_question: "Does the image show torture or execution devices, dismemberment or cruelty?"
    alternatives: ["нейтральный реквизит той же сцены"]
    rationale: "Пропаганда насилия, жестокости и пыток запрещена."
    sources: [S1, S3]

  - id: CN-VIO-04
    title: "Реалистичное огнестрельное оружие как герой-объект; оружие, направленное на зрителя"
    severity: review
    stages: [plan, prompt, qa]
    applies_to: [object]
    match:
      keywords: ["gun", "pistol", "revolver", "rifle", "shotgun", "assault rifle", "пистолет", "револьвер", "винтовка", "ружьё", "автомат"]
      semantic: "Реалистичное огнестрельное оружие как центральный объект или оружие, направленное на зрителя"
    prompt_constraint: "object shown at rest, pointing away from the viewer"
    qa_question: "Is a realistic firearm the central object, or is any weapon pointed at the viewer?"
    alternatives: ["пустая кобура", "ковбойская шляпа", "лассо", "рация", "бинокль", "кейс"]
    rationale: "Само оружие не запрещено, но прославление насилия запрещено. В казуальных предметных карточках безопаснее обойтись без него."
    sources: [S1, S3]

  # --- Сверхъестественное, религия ---
  - id: CN-SUP-01
    title: "Призраки, зомби, вампиры, демоны, оккультизм и гадание"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, theme, character]
    match:
      keywords: ["ghost", "zombie", "vampire", "demon", "haunted", "possessed doll", "ouija", "tarot", "crystal ball", "pentagram", "призрак", "зомби", "вампир", "демон", "одержимая кукла", "спиритическая доска", "таро", "хрустальный шар", "пентаграмма"]
      semantic: "Призраки, зомби, вампиры, демоны, нечисть, одержимые предметы, оккультные символы, гадание и предсказание судьбы"
    prompt_constraint: "cozy, friendly atmosphere"
    qa_question: "Does the image depict ghosts, zombies, vampires, demons, occult symbols or fortune-telling items?"
    alternatives: ["лупа", "старинный ключ", "фонарь", "свеча в подсвечнике", "старинный телефон"]
    rationale: "Пропаганда суеверий запрещена; призраки, зомби, вампиры и гадание прямо названы в практике проверки игр."
    sources: [S1, S3]

  - id: CN-SUP-02
    title: "«Слишком страшное»: пугающие образы и хоррор-атмосфера"
    severity: review
    stages: [plan, prompt, qa]
    applies_to: [object, background, theme]
    match:
      keywords: ["scary", "creepy", "horror", "terrifying", "страшный", "жуткий", "хоррор"]
      semantic: "Пугающие лица и маски, гнетущая тёмная атмосфера, элементы, рассчитанные на испуг"
    prompt_constraint: "bright high-key lighting, friendly mood"
    qa_question: "Would the image be perceived as frightening or horror-themed by a child?"
    alternatives: ["мистический детектив вместо хоррора: лупа, ключ, фонарь, загадочный конверт"]
    rationale: "Слишком страшный контент убирают из игр (пример — удалённая карта Haunted Woods)."
    sources: [S3]

  - id: CN-REL-01
    title: "Религиозные символы и предметы культа как декор или шутка"
    severity: review
    stages: [plan, qa]
    applies_to: [object, background]
    match:
      keywords: ["buddha statue", "cross", "crucifix", "idol", "altar", "cult", "статуя будды", "крест", "распятие", "идол", "алтарь", "культ"]
      semantic: "Предметы культа и религиозные символы в декоративной, комической или неуважительной подаче; символика культов"
    prompt_constraint: null
    qa_question: "Does the image use religious symbols or cult imagery as decoration or in a humorous context?"
    alternatives: ["нейтральный декор той же эпохи"]
    rationale: "Религиозные группы и культы относятся к чувствительному контенту, культы запрещены."
    sources: [S1, S3]

  # --- Азарт, вещества, взрослый контент, криминал ---
  - id: CN-GAM-01
    title: "Азартные игры и их атрибутика"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, theme]
    match:
      keywords: ["casino", "poker", "poker chips", "roulette", "slot machine", "dice", "lottery ticket", "gambling", "казино", "покер", "фишки", "рулетка", "игровой автомат", "игральные кости", "лотерейный билет", "ставки"]
      semantic: "Казино, фишки, рулетка, игровые автоматы, игральные карты и кости в контексте ставок, маджонг на деньги, лотерейные билеты, деньги на игровом столе"
    prompt_constraint: "empty table surface"
    qa_question: "Does the image show gambling items or a betting context (chips, roulette, slot machine, cards or dice with money)?"
    alternatives: ["шляпа", "колокольчик на стойке", "граммофон", "настольная лампа"]
    rationale: "Пропаганда азартных игр запрещена; покер и маджонг убирали из списка одобряемых игр."
    sources: [S1, S2, S3]

  - id: CN-DRG-01
    title: "Наркотики и атрибутика употребления"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background]
    match:
      keywords: ["drugs", "syringe", "pills pile", "cannabis", "bong", "наркотики", "шприц", "конопля", "бонг"]
      semantic: "Наркотики, растения и атрибутика употребления, намёки на них"
    prompt_constraint: "clean, wholesome props"
    qa_question: "Does the image show drugs, drug paraphernalia or hints of drug use?"
    alternatives: ["аптечка с красным крестом (без надписей)", "термос"]
    rationale: "Пропаганда наркотиков запрещена."
    sources: [S1, S3]

  - id: CN-TOB-01
    title: "Табак как герой-объект"
    severity: review
    stages: [plan, qa]
    applies_to: [object]
    match:
      keywords: ["cigarette", "cigar", "smoking pipe", "ashtray", "сигарета", "сигара", "курительная трубка", "пепельница"]
      semantic: "Сигареты, сигары, трубки, пепельницы как центральный объект или заметный элемент"
    prompt_constraint: null
    qa_question: "Does the image show tobacco products or smoking?"
    alternatives: ["лупа", "шляпа-федора", "карманные часы", "блокнот детектива"]
    rationale: "Поощрение табака среди несовершеннолетних запрещено, а аудитория казуальной игры широкая."
    sources: [S3]

  - id: CN-ALC-01
    title: "Алкоголь как герой-объект"
    severity: review
    stages: [plan, qa]
    applies_to: [object]
    match:
      keywords: ["whiskey", "vodka", "beer", "wine bottle", "liquor", "cocktail", "виски", "водка", "пиво", "бутылка вина", "коктейль"]
      semantic: "Бутылки и бокалы с алкоголем как центральный объект"
    prompt_constraint: null
    qa_question: "Is an alcoholic drink the central or prominent object?"
    alternatives: ["кувшин лимонада", "стакан молочного коктейля", "кофейник", "сифон с содовой"]
    rationale: "Поощрение алкоголя среди несовершеннолетних запрещено."
    sources: [S3]

  - id: CN-SEX-01
    title: "Обнажённость, сексуальные намёки, бельё"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [object, background, character]
    match:
      keywords: ["lingerie", "nude", "sexy", "pin-up", "бельё", "обнажённый", "сексуальный", "пин-ап"]
      semantic: "Обнажённость, бельё, сексуальные намёки, двусмысленные формы, пин-ап"
    prompt_constraint: "family-friendly props"
    qa_question: "Does the image contain nudity, lingerie or sexual innuendo, including suggestive shapes?"
    alternatives: ["роза", "письмо с сердцем", "коробка конфет"]
    rationale: "Непристойность и сексуальный контент в любой форме запрещены."
    sources: [S1, S3]

  - id: CN-CRM-01
    title: "Романтизация преступления"
    severity: review
    stages: [plan, qa]
    applies_to: [object, theme]
    match:
      keywords: ["heist", "robber mask", "money bag", "crowbar", "lockpick", "stolen", "ограбление", "маска грабителя", "мешок с деньгами", "фомка", "отмычка"]
      semantic: "Реквизит, который подаёт преступление как привлекательное: маски грабителей, мешки денег, инструменты взлома, GTA-подобная эстетика"
    prompt_constraint: null
    qa_question: "Does the image present criminal activity as attractive or cool (robbery gear, stolen money, burglary tools)?"
    alternatives: ["сейф с кодовым замком (закрытый)", "кейс", "рация", "шляпа-федора"]
    rationale: "Пропаганда преступлений и обучение преступным методам запрещены; GTA-подобные игры не допускаются."
    sources: [S1, S3]

  # --- Бытовые и культурные символы ---
  - id: CN-SYM-01
    title: "Зелёная шляпа или кепка как объект"
    severity: advise
    stages: [plan, prompt, qa]
    applies_to: [object]
    match:
      keywords: ["green hat", "green cap", "зелёная шляпа", "зелёная кепка"]
      semantic: "Головной убор зелёного цвета как центральный объект"
    prompt_constraint: "hat in a non-green color"
    qa_question: "Is there a green hat or green cap in the image?"
    alternatives: ["та же шляпа другого цвета"]
    rationale: "«Носить зелёную шляпу» (戴绿帽子) означает, что человеку изменяет партнёр. На предмете это просто неудачная ассоциация, на персонаже — оскорбление (см. CN-CHR-01)."
    sources: [S4]

  - id: CN-SYM-02
    title: "Акцент на числе 4"
    severity: advise
    stages: [plan, qa]
    applies_to: [object, text]
    match:
      keywords: ["number 4", "number 14", "номер 4"]
      semantic: "Число 4 (и 14) как заметный номер: на форме, табличке, двери, таймере"
    prompt_constraint: null
    qa_question: "Is the digit 4 prominently visible in the image?"
    alternatives: ["номер 8", "номер 6", "без номера"]
    rationale: "4 (sì) созвучно слову «смерть» (sǐ); номер 4 часто пропускают в нумерации этажей и комнат."
    sources: [S4]
    detector: {kind: ocr_digit, digits: ["4"], min_confidence: 0.5}

  - id: CN-SYM-03
    title: "Палочки, воткнутые вертикально в рис"
    severity: review
    stages: [plan, prompt, qa]
    applies_to: [object]
    match:
      keywords: ["chopsticks in rice", "палочки в рисе"]
      semantic: "Палочки для еды, вертикально воткнутые в миску с рисом"
    prompt_constraint: "chopsticks resting horizontally on a chopstick rest"
    qa_question: "Are chopsticks stuck upright in a bowl of rice?"
    alternatives: ["палочки на подставке рядом с миской"]
    rationale: "Похоже на благовония на похоронах; сильная траурная ассоциация."
    sources: [S4]

  - id: CN-SYM-04
    title: "Траурные ассоциации в праздничном контексте"
    severity: advise
    stages: [plan, qa]
    applies_to: [object]
    match:
      keywords: ["white chrysanthemum", "clock gift", "gift-wrapped clock", "white envelope", "белые хризантемы", "часы в подарок", "белый конверт"]
      semantic: "Белые хризантемы, часы в подарочной упаковке, белые или чёрные конверты в праздничной сцене"
    prompt_constraint: null
    qa_question: "Does a festive scene contain white chrysanthemums, a gift-wrapped clock or a white or black envelope?"
    alternatives: ["красные или жёлтые цветы", "красный конверт", "подарочная коробка без часов"]
    rationale: "Белые хризантемы — траурные цветы; «дарить часы» (送钟) звучит как «провожать в последний путь» (送终)."
    sources: [S4]

  - id: CN-SYM-05
    title: "Китайский дракон (лун) как злой или побеждённый монстр"
    severity: review
    stages: [plan, qa]
    applies_to: [object, theme]
    match:
      keywords: ["chinese dragon", "loong", "dragon slayer", "китайский дракон", "убийца драконов"]
      semantic: "Китайский дракон в роли злодея, жертвы или трофея"
    prompt_constraint: null
    qa_question: "Is a Chinese-style dragon depicted as evil, defeated or as a trophy?"
    alternatives: ["западный дракон в фэнтези-сете", "китайский дракон в благоприятной, праздничной подаче"]
    rationale: "Лун — благоприятный символ; изображать его злым или побеждённым неуместно."
    sources: [S5]

  - id: CN-SYM-06
    title: "Смешение культур: японские или корейские элементы, поданные как китайские"
    severity: advise
    stages: [plan, qa]
    applies_to: [object, background, theme]
    match:
      keywords: ["torii", "kimono", "katana", "тории", "кимоно", "катана"]
      semantic: "Элементы японской или корейской культуры в сете или карточке, которые подаются как китайские"
    prompt_constraint: null
    qa_question: "Does an element presented as Chinese actually belong to Japanese or Korean culture?"
    alternatives: ["аутентичный китайский аналог, проверенный человеком"]
    rationale: "Культурная неточность заметна игрокам и снижает доверие."
    sources: [S5]

  # --- Текст ---
  - id: CN-TXT-01
    title: "Иероглифы, сгенерированные моделью"
    severity: block
    stages: [prompt, qa]
    applies_to: [text]
    match:
      keywords: []
      semantic: "Любые иероглифы или псевдоиероглифы на изображении, не проверенные человеком"
    prompt_constraint: "no lettering, signs use simple pictograms"
    qa_question: "Does the image contain Chinese characters or character-like glyphs?"
    alternatives: ["пиктограмма вместо надписи"]
    rationale: "Генераторы часто рисуют несуществующие или искажённые иероглифы — для носителей языка это сразу заметно. Детектор — OCR с zh-Hans."
    sources: [S3]
    detector: {kind: ocr_script, script: han, min_confidence: 0.3}

  - id: CN-TXT-02
    title: "Латинские буквы и надписи в диегетическом тексте"
    severity: review
    stages: [prompt, qa]
    applies_to: [text]
    match:
      keywords: []
      semantic: "Вывески, таблички и надписи латиницей на предметах"
    prompt_constraint: "signs use simple pictograms instead of words"
    qa_question: "Does the image contain Latin letters or words on signs or objects?"
    alternatives: ["пиктограмма", "надпись, локализованная человеком"]
    rationale: "Для китайской версии латинские надписи и символы заменяют (пример: знак STOP заменён на 停)."
    sources: [S3]
    detector: {kind: ocr_script, script: latin, min_confidence: 0.5}

  # --- Персонажи (категория 5, вне рамок тестового) ---
  - id: CN-CHR-01
    title: "Зелёная шляпа на персонаже"
    severity: block
    stages: [plan, prompt, qa]
    applies_to: [character]
    match:
      keywords: ["green hat", "зелёная шляпа"]
      semantic: "Персонаж (особенно мужской) в зелёном головном уборе"
    prompt_constraint: "character wears a hat in a non-green color"
    qa_question: "Is any character wearing a green hat or cap?"
    alternatives: ["головной убор другого цвета"]
    rationale: "Прямое оскорбление: персонаж выглядит обманутым мужем."
    sources: [S4]
```

## Жанровые подсказки для «Кино-коллекции»

Таблица помогает планировщику заранее выбирать безопасные объекты, а не заменять их после проверки.

| Жанр | Основные риски | Безопасные объекты-символы |
|---|---|---|
| Хоррор, мистика | CN-SUP-01, CN-SUP-02, CN-VIO-01, CN-VIO-02 | Для CN лучше подать жанр как «мистический детектив» или «старинный особняк»: старинный ключ, подсвечник, фонарь, лупа, сундук, напольные часы с маятником, загадочный конверт (не белый) |
| Вестерн | CN-GAM-01 (покер в салуне), CN-ALC-01 (виски), CN-VIO-04 (револьвер), CN-TOB-01 (сигара) | Ковбойская шляпа (не зелёная), сапоги со шпорами, лассо, седло, подкова, фургон, водонапорная башня, кактус, ветряк |
| Военный, исторический | CN-HIS-01, CN-POL-03, CN-VIO-04 | Каска без знаков, бинокль, полевая рация, палатка, джип без опознавательных знаков, сигнальный фонарь |
| Криминал, нуар | CN-CRM-01, CN-TOB-01, CN-DRG-01, CN-VIO-04 | Шляпа-федора, тренч, лупа, печатная машинка, дисковый телефон, настольная лампа, кейс |
| Фэнтези | CN-SYM-05, CN-SUP-01 | Волшебный посох, сундук с самоцветами, замок, единорог; китайский дракон — только в праздничной подаче |
| Фантастика | CN-VIO-04 (реалистичное оружие), CN-POL-01 (карта Земли) | Ракета, робот, летающая тарелка, скафандр, бластер в игрушечной стилистике, не направленный на зрителя; планеты без земных материков |
| Романтика | CN-SEX-01, CN-SYM-04 | Красные розы, письмо с сердцем, коробка конфет, кольцо в футляре |
| Мюзикл, мультфильм, спорт, приключения | Низкий риск; следить за CN-SYM-02 (номер 4 на форме) и CN-POL-01 (карты) | Без ограничений сверх общих правил |

## Источники

- **S1** — [Film Industry Promotion Law of the PRC (2016), ст. 16 — ChinaLawTranslate](https://www.chinalawtranslate.com/en/film-industry-promotion-law/): перечень запрещённого содержания фильмов (суеверия, азарт, наркотики, насилие, непристойность, угроза единству и т. п.). Закон о кино, а не об играх, но перечень совпадает с практикой для игр.
- **S2** — [TechCrunch, 2019-04-21: China's new gaming rules to ban poker, blood and imperial schemes](https://techcrunch.com/2019/04/21/chinas-new-gaming-rules-to-ban-poker-blood-and-imperial-schemes/); [PC Gamer: China imposes new regulations on blood, dead bodies, and history in games](https://www.pcgamer.com/uk/china-imposes-new-regulations-on-blood-dead-bodies-and-history-in-games).
- **S3** — [Yodo1's guide to publishing your mobile game in China, part 2 — PocketGamer.biz, 2020-11-06](http://www.pocketgamer.biz/yodo1s-guide-to-publishing-your-mobile-game-in-china-part-2-content-and-system-changes/): практический список ограничений (карты, кровь, скелеты, сверхъестественное, азарт, табак и алкоголь для несовершеннолетних, замена латиницы).
- **S4** — [The World of Chinese: A Cuckold's Cap (зелёная шляпа)](https://www.theworldofchinese.com/2019/07/a-cuckolds-cap/); [China Mike: Chinese superstitions](https://www.china-mike.com/facts-about-china/chinese-superstitions/) (число 4, часы в подарок, палочки в рисе, белые хризантемы).
- **S5** — [JoSTrans: Journey to the East — Cultural adaptation of video games for the Chinese market (2018)](https://www.jostrans.org/article/view/7827).
- **S6** — общеизвестный контекст (Umbrella Movement, Гонконг, 2014); отдельный источник не проверялся, поэтому правило уровня `advise`.
- **S7** — [Games in Asia via Yahoo: China doesn't censor skeletons](https://uk.news.yahoo.com/china-doesn-t-censor-skeletons-truth-game-censorship-030014760.html): контраргумент о скелетах; учтён в обосновании CN-VIO-02.

**Ограничения источников:** официальный текст правил NPPA для игр не найден; использованы отраслевые пересказы 2019–2020 годов. Перед релизом сверить с актуальными требованиями.
