<!--
Инструкция планировщика: правила карт и фонов (вторая часть `instruction` задачи `plan` / `repair`).
Что здесь: смысл темы, один объект на карту, фон и контраст с объектом, категории 1–4, поля
`main_object` и `environment`, порядок карт в сете. Допустимые мотивы и цвета — не здесь, а в
knowledge/catalog/vocabulary.yaml; пороги сета — в knowledge/set_grammar/v1.yaml.
Правка без кода; проверка — docs/guides/customize-knowledge.md.
-->
The collection theme frames every card and the set theme is its genre or flavour: for collection
«Кино» each object belongs to the world of cinema — shooting, showing or watching a film — in
that genre (a western film set and its props, crew gear, premiere, cinema), not the genre's real
world itself (user feedback 2026-11-03).

Each card is one object that carries the theme by itself: do not attach props, stickers or
accessories (ropes, cacti, moustaches) just to signal the genre; a supporting element appears
only when it explains how the object is used, at most one or two per card, never two of the same
kind (one spotlight, not two), and the object never points downward (user G2 2026-11-04). The
`object_description` names the object and only those supporting elements: no genre decorations,
pictograms or accessories added to it.

Categories (`vocabulary.category` defines them): 1 — the object floats on a plain background,
no surface; 2 — it stands on a plain surface (`surface` = plain); 3 — it stands on a recognisable
real surface (`surface` = wood, stone, grass, water, sand, fabric…) in front of a plain backdrop;
4 — it is shown inside its surroundings (a scene). The set rules in `set_grammar` say which
categories a set must contain. Use for `background.motif` only the motifs that
`set_grammar.category_motifs` allows for the card's category: category 4 has no background plate,
its motif is `scene` and its `background.color` is the dominant colour of the surroundings (sky,
water, wall).

Use only vocabulary values for motif, colour and materials. Follow set_grammar. Policy outranks
producer wishes. Every rule in `plan_rules` is a restriction, whatever its severity (block, review
or advise) and whichever pack it comes from (the IP baseline and every culture pack are always
active): no object, background, surface or environment may match its `semantic` description or
keywords; instead pick one of its `alternatives` or another object of the same scale and category;
each plan's policy_notes names every conflicting rule id with the replacement and the reason. Copyright: genre archetypes
only, see policies/ip/baseline.md#Чек-лист авторских прав. Functional parts need a plausible use
or mounting context; vary graphic motifs across the collection.

Keep background motifs bold and readable, light but less saturated than the object, which carries
the bright accent colours (user feedback 2026-11-03). Pick each card's background colour opposite
or complementary to the object's main colour (a brown, orange or yellow object on a blue, cyan or
purple backdrop; a red object on green or cyan), never in the object's own hue family (user G2
2026-11-04: «цвет фона и основной цвет объекта были противоположными / комплиментарными»).

Describe every card for the content table, in the language of the request:
`main_object` — the main object in one phrase: its real form, its proportions (tall and narrow,
low and wide, round, elongated…) and its main colour, plus at most one secondary colour (e.g.
«пузатый медный чайник, низкий и широкий, рыжий с чёрной ручкой»); the main colour named here is
the one the background colour must contrast with.
`environment` — only for category 4: the secondary surroundings around the object in one short
phrase (e.g. «деревянный причал у воды, вечернее небо»), never a second main object; for
categories 1–3 leave it empty (null).

The object cards of a set tell one visual story in card order: from small, simple objects to
bigger and more complex ones, ending with a culmination card (user G1 2026-11-04: «визуальную
линию одной истории — от простых предметов к сложным и кульминации»); say the story in `concept`.

Give every card an English `object_description` for the image model and judges (short, concrete,
with its real form, proportions and main colour, e.g. 'red-and-cream console',
'brass spyglass telescope, extended').
