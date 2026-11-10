---
description: Проверить флаговые карты (G2), если G2 включён для прогона
argument-hint: [run_id]
---
Следуй скиллу `cardforge`, шаг 3. Прогон: $ARGUMENTS (если пусто — последний из `list_runs`).
`get_flags`: покажи карты в чате (или дай страницу `open_review_page`, если их много), собери решения
человека его словами и передай `submit_card_decisions`.
По умолчанию G2 выключен (`config/autonomy.toml`): тогда скажи, что спорные карты помечены на листах G3, и предложи `/cardforge:finish`.
