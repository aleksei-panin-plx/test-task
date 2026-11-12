# ruff: noqa: E501  (tool descriptions and Russian messages)
"""MCP tools of the CardForge plugin (stage 7, `docs/stages/07-plugin.md` §3).

One tool set over the designer flow (`cardforge.orchestrator.product`); the stdio transport is
`cardforge.mcp.server`, a future Streamable HTTP transport reuses this module unchanged.
Results are JSON text; images (G2 cards, 5×2 sheets, cards to inspect) come as small PNG image
content so the agent can show them in the chat. Never returns cost, never a copyright checklist.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from cardforge.orchestrator import product
from cardforge.orchestrator.workspace import Workspace

RUN = {"run_id": {"type": "string", "description": "Run id from start_run / list_runs"}}
WHO = {
    "reviewer": {"type": "string", "description": "The person's name (who decides)"},
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    call: Callable[[Workspace, dict], dict]
    read_only: bool = False
    images: Callable[[dict], list[str]] | None = None  # paths of images to attach
    image_width: int = 215


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _doctor(ws: Workspace, a: dict) -> dict:
    from cardforge.doctor import collect

    report = collect(ws.root)
    key = product.render_key_present(ws.root)
    mode = next((c for c in report.checks if c.name == "plugin_mode"), None)
    blocked = bool(mode and mode.status == "error")
    full = key and not blocked
    return {
        "plugin_mode": "full" if full else "blocked" if blocked else "plan-only",
        "blocked_by": [c.detail for c in report.checks if c.name.startswith("openrouter_route:") and c.status == "error"],
        "render": "GPT Image 2.5 через OpenRouter; Nano Banana — резерв" if full else None,
        "missing": [] if key else [
            "ключ OPENROUTER_API_KEY в окружении или в .env папки CardForge: без него только планы"
        ],
        "errors": [c.detail for c in report.checks if c.status == "error" and c.name not in DEV_CHECKS],
        "warnings": [f"{c.name}: {c.detail}" for c in report.checks
                     if c.status == "warning" and c.name not in DEV_CHECKS | {"plugin_mode"}],
    }  # fmt: skip


# Development-machine checks (local FLUX / MLX, Codex image rendering, the developer's API ceiling,
# memory for local models): the plugin renders through OpenRouter, so they say nothing about it and
# read as a contradiction to the agent («full mode» + «no eligible renderer»).
DEV_CHECKS = frozenset({"mflux", "mlx", "uv", "codex", "render", "api_budget", "credentials", "memory",
                        "openrouter_prices"})  # fmt: skip


def _sheet_paths(r: dict) -> list[str]:
    rows = r.get("sets") or r.get("variants") or []
    return [v["sheet"] for v in rows if Path(v["sheet"]).exists()]


def _flag_paths(r: dict) -> list[str]:
    return [c["image"] for c in r.get("cards", []) if c.get("image")] if r.get("in_chat") else []


def _task_paths(r: dict) -> list[str]:
    return [p for t in r.get("tasks", []) for p in t.get("images", [])]


TOOLS: list[Tool] = [
    Tool("doctor", "Проверить окружение CardForge: полный режим (рендер) или только планы, чего не хватает.",
         _obj({}, []), _doctor, read_only=True),
    Tool("start_run",
         "Создать прогон и составить план коллекции (G1). `collection` обязательна: тема всей коллекции "
         "(«Лето», «Путешествие», «Кино»); если из слов человека она не ясна однозначно (например, «Япония» — "
         "японское кино или коллекция «Япония»?), спроси, не угадывай. По умолчанию — вся коллекция: 16 тем "
         "сетов, сеты 1–15 со списком предметных карт (золотые позиции только отмечены), сет 16 — только тема. "
         "Если человек назвал число наборов («сделай 4 набора»), передай `sets` и не переспрашивай. "
         "Дальше: get_agent_tasks → submit_agent_answer → resume_run, пока get_run.next не станет show_plan.",
         _obj({"collection": {"type": "string", "description": "Тема коллекции словами человека"},
               "sets": {"type": "integer", "minimum": 1, "maximum": 16,
                        "description": "Сколько сетов готовить, если человек назвал число; иначе не передавать (16)"},
               "set_theme": {"type": "string", "description": "Только для вариантов набора: тема набора («Пикник»)"},
               "alternatives": {"type": "integer", "minimum": 1, "maximum": 9,
                                "description": "Только если человек попросил варианты набора: число альтернатив к основному (по умолчанию 3, или сколько он назвал). Тогда рисуется 1 + N вариантов набора `set_theme`, основной выбирают судьи"},
               "wishes": {"type": "string", "description": "Пожелания словами человека"},
               "markets": {"type": "array", "items": {"type": "string"}, "description": "Рынки, например CN"},
               "g1": {"type": "string", "enum": ["human", "auto"], "description": "Только по просьбе человека; по умолчанию из config/autonomy.toml"},
               "g2": {"type": "string", "enum": ["none", "flagged", "all"], "description": "Только по просьбе человека; по умолчанию из config/autonomy.toml"}},
              ["collection"]),
         lambda ws, a: product.start(ws, a["collection"], sets=a.get("sets"), wishes=a.get("wishes", ""),
                                     markets=a.get("markets"), g1=a.get("g1"), g2=a.get("g2"),
                                     set_theme=a.get("set_theme", ""), alternatives=a.get("alternatives"))),
    Tool("get_plan", "План коллекции для G1: темы всех сетов, по каждому сету список предметных карт (объект, категория, фон) и число золотых позиций, тема гранд-сета, флаги и замены политик.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.plan_view(ws, a["run_id"]), read_only=True),
    Tool("edit_plan",
         "Правка плана на G1 словами человека (дословно). План уходит на ремонт: ответь на новую задачу "
         "repair (get_agent_tasks), затем resume_run и снова покажи get_plan.",
         _obj({**RUN, "edit": {"type": "string"}, **WHO}, ["run_id", "edit", "reviewer"]),
         lambda ws, a: product.edit_plan(ws, a["run_id"], a["edit"], a["reviewer"])),
    Tool("approve_plan",
         "«Запускай»: одобрить план (все варианты или decisions {vNN: approve|reject}) — рендер идёт в фоне.",
         _obj({**RUN, **WHO, "human_answer": {"type": "string", "description": "Слова человека дословно"},
               "decisions": {"type": "object", "additionalProperties": {"type": "string", "enum": ["approve", "reject"]}}},
              ["run_id", "reviewer", "human_answer"]),
         lambda ws, a: product.approve_plan(ws, a["run_id"], a["reviewer"], a["human_answer"], a.get("decisions"))),
    Tool("get_run", "Состояние прогона, прогресс (принято/всего) и следующий шаг `next`. Стоимость не показывается.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.view(ws, a["run_id"]), read_only=True),
    Tool("list_runs", "Последние прогоны: id, тема, состояние.", _obj({}, []),
         lambda ws, a: product.list_runs(ws), read_only=True),
    Tool("get_agent_tasks",
         "Задачи ядра для агента: нормализация брифа, план, ремонт плана, скрининг политик, проверка картинок "
         "(IP и рынки), нормализация правок. Ответ — строго по answer_schema задачи. Промпты рендера агент не пишет.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.agent_tasks(ws, a["run_id"]), read_only=True,
         images=_task_paths, image_width=430),
    Tool("submit_agent_answer", "Ответ агента на одну задачу ядра (поля answer_schema).",
         _obj({**RUN, "task": {"type": "string", "description": "Путь задачи из get_agent_tasks"},
               "answer": {"type": "object"}, "provider": {"type": "string", "description": "claude-code или codex"},
               "model": {"type": "string"}}, ["run_id", "task", "answer", "provider", "model"]),
         lambda ws, a: product.answer_task(ws, a["run_id"], a["task"], a["answer"], a["provider"], a["model"])),
    Tool("resume_run", "«Продолжи»: продолжить прогон после ответов, решений или остановки.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.resume(ws, a["run_id"])),
    Tool("stop_run", "«Стоп»: остановить прогон после текущего вызова.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.stop(ws, a["run_id"])),
    Tool("get_flags",
         "G2: карты, которые ждут человека, с причиной словами и комментарием проверки IP/политик. "
         "Если in_chat — показать в чате (картинки приложены), иначе open_review_page.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.flags(ws, a["run_id"]), read_only=True,
         images=_flag_paths),
    Tool("submit_card_decisions",
         "G2: решения человека по картам {vNN_cMM: accept | reject | fix: <слова человека>} или файл JSON со "
         "страницы проверки (from_file). Затем прогон продолжается сам.",
         _obj({**RUN, **WHO, "decisions": {"type": "object", "additionalProperties": {"type": "string"}},
               "from_file": {"type": "string"}}, ["run_id", "reviewer"]),
         lambda ws, a: product.decide_cards(ws, a["run_id"], a["reviewer"], a.get("decisions"),
                                            Path(a["from_file"]) if a.get("from_file") else None)),
    Tool("open_review_page", "G2 на локальной странице (много карт): путь к HTML для человека.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.review_page(ws, a["run_id"])),
    Tool("get_sheets",
         "G3: листы 5×2 всех нарисованных сетов коллекции (картинки приложены; золотые позиции — жёлтые заглушки), авто-пропущенные и «не подтверждённые судьями» карты с причиной, комментарии IP.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.sheets(ws, a["run_id"]), read_only=True,
         images=_sheet_paths, image_width=860),
    Tool("select_variant", "Варианты набора (по запросу): сделать основным другой вариант — один, словами человека; остальные остаются альтернативами. В плане коллекции не нужен.",
         _obj({**RUN, **WHO, "variants": {"type": "array", "items": {"type": "string"}, "minItems": 1},
               "human_answer": {"type": "string"}}, ["run_id", "variants", "reviewer", "human_answer"]),
         lambda ws, a: product.select_variants(ws, a["run_id"], a["variants"], a["reviewer"], a["human_answer"])),
    Tool("export_run",
         "Собрать или пересобрать выдачу коллекции по сетам: delivery/current/ (PNG, листы, превью в рамке), "
         "отсеянные карты и альтернативы; прежняя версия сохраняется в delivery/vNN/.",
         _obj(RUN, ["run_id"]), lambda ws, a: product.export_run(ws, a["run_id"])),
    Tool("regenerate_cards",
         "Перерисовать карты с правкой человека (в том числе после экспорта). Перерисовку человек смотрит на G2; "
         "после решения — export_run.",
         _obj({**RUN, **WHO, "cards": {"type": "array", "items": {"type": "string"}, "minItems": 1},
               "note": {"type": "string", "description": "Правка словами человека дословно"}},
              ["run_id", "cards", "note", "reviewer"]),
         lambda ws, a: product.regenerate(ws, a["run_id"], a["cards"], a["note"], a["reviewer"])),
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


def _png(path: str, width: int) -> dict:
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
    return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/png"}


def call(ws: Workspace, name: str, arguments: dict) -> dict:
    """MCP `tools/call` result: JSON text (+ images); a core refusal is `isError` text."""
    from cardforge.orchestrator.service import ServiceError

    tool = BY_NAME.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"нет инструмента {name}"}], "isError": True}
    try:
        result = tool.call(ws, arguments or {})
    except (ServiceError, ValueError, KeyError, OSError) as exc:
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    content: list[dict] = [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}]
    for path in (tool.images(result) if tool.images else [])[:24]:
        content.append({"type": "text", "text": Path(path).name})
        content.append(_png(path, tool.image_width))
    return {"content": content, "structuredContent": result}
