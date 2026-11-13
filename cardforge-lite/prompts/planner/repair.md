<!-- Добавляется к инструкции задачи `repair` (план не прошёл проверки). -->
Fix every listed error and keep everything else. Every object listed in `replaced_objects` matched a
policy rule: replace it with an object that matches no listed plan rule (prefer the rule's
`alternatives`) and add to that set's `policy_notes` one line per replacement: the rule id, the
replaced object, the new object and the reason (e.g. "CN-POL-01: «Глобус» → «Компас»: реальная
география").
