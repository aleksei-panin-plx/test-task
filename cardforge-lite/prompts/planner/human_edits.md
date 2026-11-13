<!-- Добавляется к инструкции задачи `repair`, когда человек правит план на G1 (`edit_plan`). -->
`human_edits` are the producer's own G1 edits of `previous_plan` (newest last): apply every one
exactly, keep all other cards unless an edit asks to change them, and still satisfy set_grammar
and the policy rules.
