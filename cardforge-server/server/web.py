# ruff: noqa: E501  (HTML)
"""Gate pages G1 / G2 / G3 for the browser (portal plan §7), from the core's own data and pages.

- G2 is the core page `cardforge/export/review_page.html` itself (blind, `page_data(blind=True)`),
  rebuilt at request time: its inline script is served as `/web/static/g2-core.js` (CSP
  `script-src 'self'`), the data goes in a JSON block, and `web/static/g2-server.js` replaces
  «Скачать решения» with «Отправить решения» (POST → the core `decide_cards` with the blind map).
  A core change of the page reaches the server with the next `sync_core.py`, no copy here.
- G1 is the collection plan of `product.plan_view`: approve or edit in words (→ repair).
- G3 is `product.sheets`: 5×2 sheets, «не подтверждена судьями» with the reason, regenerate
  with an edit, build the delivery and download it.
The style block is the core page's, so the three pages look alike.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import cardforge.export.review_page as review_page

FOLDER = Path(__file__).resolve().parents[1]
STATIC = FOLDER / "web/static"
CORE_PAGE = Path(review_page.__file__).with_name("review_page.html")
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")  # fmt: skip


def _core_parts() -> tuple[str, str, str]:
    text = CORE_PAGE.read_text(encoding="utf-8")
    head, rest = text.split("<script>", 1)
    js, tail = rest.split("</script>", 1)
    return head, js, tail


def core_style() -> str:
    head = _core_parts()[0]
    return head[head.index("<style>"): head.index("</style>") + len("</style>")]


def g2_core_js() -> str:
    js = _core_parts()[1]
    if "const DATA = __DATA__;" not in js:
        raise RuntimeError("core review_page.html changed: DATA placeholder not found (re-check web.py)")
    return js.replace("const DATA = __DATA__;", 'const DATA = JSON.parse(document.getElementById("cf-data").textContent);')


def _json_block(data) -> str:
    return '<script id="cf-data" type="application/json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def _nav(run_id: str, owner: str, active: str) -> str:
    links = " · ".join(
        f'<b>{g.upper()}</b>' if g == active else f'<a href="/web/runs/{html.escape(run_id)}/{g}">{g.upper()}</a>'
        for g in ("g1", "g2", "g3")
    )
    return (f'<nav class="cfnav"><a href="/web/">Мои прогоны</a> · {links} · <span>{html.escape(owner)}</span> · '
            f'<a href="/web/logout">Выйти</a></nav>')  # fmt: skip


EXTRA_CSS = """<style>
.cfnav { padding: 8px 16px; font-size: 13px; border-bottom: 1px solid var(--line); }
.cfnav a { color: inherit; }
main.cf { padding: 12px 16px; max-width: 1200px; }
main.cf table.plan { border-collapse: collapse; margin: 6px 0 16px; font-size: 13px; }
main.cf table.plan td, main.cf table.plan th { border: 1px solid var(--line); padding: 3px 8px; text-align: left; }
main.cf .set { margin: 18px 0; }
main.cf .sheet { max-width: 100%; height: auto; border-radius: 8px; }
main.cf .note { color: var(--muted); font-size: 13px; }
main.cf textarea { min-height: 60px; }
main.cf .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
main.cf .warn { color: var(--fix); }
</style>"""


def _page(title: str, body: str, scripts: list[str], run_id: str = "", owner: str = "", active: str = "") -> str:
    tags = "".join(f'<script src="/web/static/{s}"></script>' for s in scripts)
    nav = _nav(run_id, owner, active) if run_id else ""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title>{core_style()}{EXTRA_CSS}</head><body>{nav}<main class="cf">{body}</main>{tags}</body></html>')  # fmt: skip


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


# ---------------------------------------------------------------- pages


def login(error: str = "", next_url: str = "/web/") -> str:
    msg = f'<p class="warn">{e(error)}</p>' if error else ""
    body = (f"<h1>CardForge — вход</h1>{msg}<form method='post' action='/web/login'>"
            f"<input type='hidden' name='next' value='{e(next_url)}'>"
            "<p>Личный ключ (выдаёт администратор сервера):</p>"
            "<input type='password' name='key' size='60' autocomplete='off' required> "
            "<button class='primary' type='submit'>Войти</button></form>"
            "<p class='note'>Ключ меняется на короткую сессию этого браузера; сам ключ страница не хранит.</p>")  # fmt: skip
    return _page("CardForge — вход", body, [])


def runs_list(owner: str, runs: list[dict]) -> str:
    rows = "".join(
        f"<tr><td><a href='/web/runs/{e(r['run_id'])}/{(r['awaiting'] or 'g1') if (r['awaiting'] or '') in ('g1', 'g2', 'g3') else 'g1'}'>{e(r['run_id'])}</a></td>"
        f"<td>{e(r['theme'])}</td><td>{e(r['state'])}</td><td>{e(r['awaiting'] or '')}</td><td>{e(r['created_at'])}</td></tr>"
        for r in runs
    )
    body = (f"<h1>Мои прогоны</h1><p class='note'>{e(owner)}</p><table class='plan'><tr><th>Прогон</th><th>Коллекция</th>"
            f"<th>Состояние</th><th>Ждёт</th><th>Создан</th></tr>{rows or '<tr><td colspan=5>пока нет</td></tr>'}</table>"
            "<p class='note'>Прогоны запускает агент (Claude Code / Codex) через MCP; здесь — решения на гейтах.</p>")  # fmt: skip
    return _page("CardForge — прогоны", body, [])


def g1(run_id: str, owner: str, plan: dict, view: dict) -> str:
    coll = plan.get("collection") or {}
    themes = "".join(f"<li>{e(t)}</li>" for t in coll.get("set_themes", []))
    sets = []
    for v in plan["variants"]:
        rows = "".join(f"<tr><td>{e(c['card_id'])}</td><td>{e(c['object'])}</td><td>{e(c['category'])}</td>"
                       f"<td>{e(c['background'])}</td><td>{e('; '.join(c['policy_flags']))}</td></tr>" for c in v["cards"])  # fmt: skip
        notes = "".join(f"<li>{e(n)}</li>" for n in v.get("policy_notes", []))
        sets.append(f"<div class='set'><h3>{e(v.get('set_index') or v['variant_id'])}. {e(v['title'])}</h3>"
                    f"<p class='note'>{e(v['concept'])} · золотых позиций: {e(v.get('gold_slots') or 0)}</p>"
                    f"{('<ul class=warn>' + notes + '</ul>') if notes else ''}"
                    f"<table class='plan'><tr><th>Карта</th><th>Объект</th><th>Кат.</th><th>Фон</th><th>Политики</th></tr>{rows}</table></div>")  # fmt: skip
    edits = "".join(f"<li>{e(t)}</li>" for t in plan.get("edits_so_far", []))
    open_ = view["state"] == "awaiting_g1"
    actions = (
        "<h2>Решение</h2><div class='row'><button id='approve' class='primary'>Запускай</button></div>"
        "<p>Правка словами (план уйдёт на ремонт и снова откроется здесь):</p>"
        "<textarea id='edit' style='width:100%'></textarea><div class='row'><button id='sendEdit'>Отправить правку</button></div>"
        if open_ else f"<p class='note'>G1 сейчас не открыт: прогон в состоянии {e(view['state'])} ({e(view.get('next'))}).</p>"
    )  # fmt: skip
    body = (f"<h1>G1 · план коллекции «{e(plan['brief']['collection'])}»</h1>"
            f"<p class='note'>сетов: {e(plan['brief'].get('sets'))} · предметных карт: {e(coll.get('object_cards'))} · "
            f"золотых позиций: {e(coll.get('gold_slots'))} · гранд-сет: {e(coll.get('grand_set_theme'))}</p>"
            f"{('<h2>Темы сетов</h2><ol>' + themes + '</ol>') if themes else ''}"
            f"{('<p>Правки до сих пор:</p><ul>' + edits + '</ul>') if edits else ''}"
            f"{''.join(sets)}{actions}<p id='msg' class='note'></p>")  # fmt: skip
    return _page(f"G1 · {run_id}", body, ["g1.js"], run_id, owner, "g1")


def g2(run_id: str, owner: str, store, root: Path) -> str:
    data = review_page.page_data(store, blind=True, root=root)
    head, _js, tail = _core_parts()
    head = head.replace("__TITLE__", "Разметка G2")
    head = head.replace("<body>", "<body>" + _nav(run_id, owner, "g2"), 1)
    head = head.replace('<button id="saveBtn" class="primary">Скачать решения</button>',
                        '<button id="saveBtn" class="primary">Отправить решения</button>')
    # The help line of the local page points to the CLI; on the server the page sends itself.
    start = head.find('<p class="help">')
    end = head.find("</p>", start)
    if start != -1 and end != -1:
        head = (head[:start] + '<p class="help">Клик по картинке — крупно. Клавиши на выбранной карточке: <b>A</b> принять, '
                '<b>R</b> отклонить, <b>F</b> правка (опишите её в комментарии или отметьте причины). Слепая разметка: '
                'модель, прогон и номер попытки скрыты. Черновик хранится в этом браузере; «Отправить решения» записывает '
                'их в прогон от вашего имени, и прогон продолжается сам.' + head[end:])  # fmt: skip
    meta = f'<meta name="cf-owner" content="{e(owner)}">'
    head = head.replace("<head>", "<head>" + meta, 1)
    scripts = _json_block(data) + '<script src="/web/static/g2-core.js"></script><script src="/web/static/g2-server.js"></script>'
    return head + scripts + tail


def g3(run_id: str, owner: str, sheets: dict, view: dict, delivered: bool) -> str:
    rows = sheets.get("sets") or sheets.get("variants") or []
    blocks = []
    for v in rows:
        unconf = "".join(
            f"<li><b>{e(u['card_id'])}</b> {e(u['object'])}: {e('; '.join(u['why']))}"
            f"{(' · IP: ' + e('; '.join(u['policy_notes']))) if u.get('policy_notes') else ''}</li>"
            for u in v.get("unconfirmed", [])
        )
        notes = "".join(f"<li>{e(n)}</li>" for n in v.get("policy_notes", []))
        img = f"/web/runs/{e(run_id)}/file/sheets/{e(v['variant_id'])}.png"
        blocks.append(
            f"<div class='set'><h3>{e(v.get('set_index') or v['variant_id'])}. {e(v['title'])}</h3>"
            f"<p class='note'>принято {e(v['accepted'])} · авто-пропуск: {e(', '.join(v.get('auto_passed', [])) or 'нет')}"
            f"{(' · не готовы: ' + e(', '.join(v['not_ready']))) if v.get('not_ready') else ''}</p>"
            f"<img class='sheet' src='{img}' alt='лист {e(v['variant_id'])}' loading='lazy'>"
            f"{('<p class=warn>Не подтверждены судьями:</p><ul>' + unconf + '</ul>') if unconf else ''}"
            f"{('<p>Комментарии проверки IP:</p><ul>' + notes + '</ul>') if notes else ''}</div>"
        )  # fmt: skip
    ready = view["state"] in ("completed", "partial")
    actions = (
        "<h2>Решение G3</h2>"
        "<p>Перерисовать карты с правкой (номера карт через запятую, например v03_c04):</p>"
        "<div class='row'><input id='cards' size='40' placeholder='v01_c02, v03_c04'></div>"
        "<textarea id='note' style='width:100%' placeholder='правка словами'></textarea>"
        "<div class='row'><button id='regen'>Перерисовать</button>"
        "<button id='export' class='primary'>Принять и собрать выдачу</button>"
        + (f"<a href='/web/runs/{e(run_id)}/delivery.zip'>Скачать выдачу (zip)</a>" if delivered else "")
        + "</div>"
        if ready else f"<p class='note'>G3 ещё не открыт: прогон в состоянии {e(view['state'])} ({e(view.get('next'))}).</p>"
    )  # fmt: skip
    body = (f"<h1>G3 · {e(sheets.get('collection') or '')}</h1>"
            f"<p class='note'>{e(sheets.get('how'))}</p>{''.join(blocks)}{actions}<p id='msg' class='note'></p>")  # fmt: skip
    return _page(f"G3 · {run_id}", body, ["g3.js"], run_id, owner, "g3")


def static(name: str) -> tuple[bytes, str] | None:
    if name == "g2-core.js":
        return g2_core_js().encode(), "text/javascript; charset=utf-8"
    path = (STATIC / name).resolve()
    if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
        return None
    kind = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(path.suffix)
    return (path.read_bytes(), kind) if kind else None
