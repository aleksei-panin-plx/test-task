# ruff: noqa: E501  (tool descriptions and Russian messages)
"""MCP tools of CardForge Light: the G1 flow only (plan of the collection and content tables).

The stdio transport is `cardforge.mcp.server`. Results are JSON text. Never returns cost and
never a copyright checklist. There are no render, judge, review or export tools in Light.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from cardforge.orchestrator import run
from cardforge.orchestrator.workspace import Workspace

RUN = {"run_id": {"type": "string", "description": "Run id from start_plan / list_runs"}}
WHO = {"reviewer": {"type": "string", "description": "The person's name (who decides)"}}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    call: Callable[[Workspace, dict], dict]
    read_only: bool = False


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _doctor(ws: Workspace, a: dict) -> dict:
    from cardforge.doctor import collect

    report = collect(ws.root)
    return {
        "edition": "lite",
        "plugin_mode": "plan-only (G1)",
        "ok": not report.has_errors,
        "errors": [c.detail for c in report.checks if c.status == "error"],
        "warnings": [f"{c.name}: {c.detail}" for c in report.checks if c.status == "warning"],
        "note": "Light делает только план коллекции и таблицы контента; картинок нет, ключи не нужны.",
    }


def _start(ws: Workspace, a: dict) -> dict:
    rid = run.create(ws, a["collection"], sets=a.get("sets"), wishes=a.get("wishes", ""), markets=a.get("markets"))
    return run.advance(ws, rid)


TOOLS: list[Tool] = [
    Tool("doctor", "Проверить окружение CardForge Light (знания, политики, инструкция планировщика, папка планов).",
         _obj({}, []), _doctor, read_only=True),
    Tool("start_plan",
         "Создать прогон и составить план коллекции (G1). `collection` обязательна: тема всей коллекции "
         "(«Лето», «Путешествие», «Кино»); если из слов человека она не ясна однозначно (например, «Япония» — "
         "японское кино или коллекция «Япония»?), спроси, не угадывай. По умолчанию — вся коллекция: 16 тем "
         "сетов, сеты 1–15 со списком предметных карт (золотые позиции только отмечены), сет 16 — только тема. "
         "Если человек назвал число сетов («2 сета», «4 набора»), передай `sets` и не переспрашивай. "
         "Дальше: get_agent_tasks → submit_agent_answer → resume_run, пока next не станет show_plan.",
         _obj({"collection": {"type": "string", "description": "Тема коллекции словами человека"},
               "sets": {"type": "integer", "minimum": 1, "maximum": 16,
                        "description": "Сколько сетов, если человек назвал число; иначе не передавать (16)"},
               "wishes": {"type": "string", "description": "Пожелания словами человека"},
               "markets": {"type": "array", "items": {"type": "string"}, "description": "Рынки, например CN"}},
              ["collection"]), _start),
    Tool("get_agent_tasks",
         "Задачи ядра для агента: normalize (бриф), plan / repair (план коллекции), policy_screen (политики). "
         "Ответ — строго по answer_schema задачи.",
         _obj(RUN, ["run_id"]), lambda ws, a: run.agent_tasks(ws, a["run_id"]), read_only=True),
    Tool("submit_agent_answer", "Ответ агента на одну задачу ядра (поля answer_schema, без конверта).",
         _obj({**RUN, "task": {"type": "string", "description": "Путь задачи из get_agent_tasks"},
               "answer": {"type": "object"}, "provider": {"type": "string", "description": "claude-code или codex"},
               "model": {"type": "string"}}, ["run_id", "task", "answer", "provider", "model"]),
         lambda ws, a: run.answer_task(ws, a["run_id"], a["task"], a["answer"], a["provider"], a["model"])),
    Tool("resume_run", "Продолжить планирование после ответов на задачи ядра.",
         _obj(RUN, ["run_id"]), lambda ws, a: run.resume(ws, a["run_id"])),
    Tool("get_plan",
         "План коллекции для G1: темы сетов и по каждому сету таблица контента в Markdown (№, объект, основной "
         "объект, категория, окружение, фон, политики; золотые позиции отмечены, сет 16 — только тема), пути к "
         "файлам CSV и JSON.",
         _obj(RUN, ["run_id"]), lambda ws, a: run.plan_view(ws, a["run_id"]), read_only=True),
    Tool("edit_plan",
         "Правка плана на G1 словами человека (дословно). План уходит на ремонт: ответь на задачу repair "
         "(get_agent_tasks), затем resume_run и снова покажи get_plan.",
         _obj({**RUN, "edit": {"type": "string"}, **WHO}, ["run_id", "edit", "reviewer"]),
         lambda ws, a: run.edit_plan(ws, a["run_id"], a["edit"], a["reviewer"])),
    Tool("approve_plan", "«Ок»: утвердить план — таблицы помечаются как утверждённые. Рендера в Light нет.",
         _obj({**RUN, **WHO, "human_answer": {"type": "string", "description": "Слова человека дословно"}},
              ["run_id", "reviewer", "human_answer"]),
         lambda ws, a: run.approve_plan(ws, a["run_id"], a["reviewer"], a["human_answer"])),
    Tool("get_run", "Состояние прогона и следующий шаг `next` (answer_tasks, show_plan, done, failed).",
         _obj(RUN, ["run_id"]), lambda ws, a: run.view(ws, a["run_id"]), read_only=True),
    Tool("list_runs", "Последние прогоны: id, тема, число сетов, состояние.", _obj({}, []),
         lambda ws, a: run.list_runs(ws), read_only=True),
]  # fmt: skip
BY_NAME = {t.name: t for t in TOOLS}


def describe() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.schema,
            "annotations": {"readOnlyHint": t.read_only, "destructiveHint": False, "openWorldHint": False},
        }
        for t in TOOLS
    ]


def call(ws: Workspace, name: str, arguments: dict) -> dict:
    """MCP `tools/call` result: JSON text; a core refusal is `isError` text."""
    tool = BY_NAME.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"нет инструмента {name}"}], "isError": True}
    try:
        result = tool.call(ws, arguments or {})
    except (run.ServiceError, ValueError, KeyError, OSError) as exc:
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}], "structuredContent": result}
