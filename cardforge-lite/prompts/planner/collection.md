<!--
Инструкция планировщика: план всей коллекции (первая часть `instruction` задачи `plan` / `repair`).
Редактируется без кода: после правки — `.venv/bin/pytest` и `cardforge plan --fake ...`
(docs/guides/customize-knowledge.md). HTML-комментарии в задачу не попадают; переносы строк
схлопываются в пробелы. Имена полей в `обратных кавычках` должны совпадать со схемой PlanDraft.
-->
Plan the whole collection (user 2026-11-10). A collection is 16 sets of 10 cards on one broad
theme (`request.collection_theme`, e.g. «Лето», «Путешествие», «Кино»); its sets are diverse,
non-repeating sub-themes. Write `collection.set_themes`: exactly `sets_total` distinct set themes
in set order, each a clear sub-theme of the collection; the 16th, when present, is the
culminating theme of the grand set (e.g. «Летняя вечеринка»), and `collection.grand_set_theme`
repeats it. Write `variants`: one PlanDraft per set of `collection_layout.drawn`, with its
`variant_id`, `set_index` and `gold_slots` as given, `title` = that set's theme, and exactly
`object_cards` object cards. Gold cards (the collection's key characters in an ironic emotional
situation) are NOT planned: their `gold_slots` close the set and stay empty here; set 16 has no
cards. Every object fits its set theme within the collection theme; no object repeats in any
other set of the collection. Set rules (categories, scales, types, colours) apply to the object
cards of each set.
