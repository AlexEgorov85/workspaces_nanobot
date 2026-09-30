"""Follow Up 2.0 — тело ответа рендерит код, а не модель.

Стриминга токенов нет, значит длина вывода — это время. Таблица на 27 актов
это полторы-две тысячи токенов, которые аудитор просто ждёт, и одновременно
главный источник выдуманных чисел: модель, переписывающая цифры из леджера,
их иногда меняет.

Поэтому тело собирается детерминированно из леджера и уходит в интерфейс ДО
генерации, а модель пишет только шапку и вывод. Заодно это единственный способ
пересобрать тело после верификатора, не тратя ещё один вызов.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from backend.core.contract import AnswerContract
from backend.core.critic import Claim
from backend.core.evidence import Evidence, Ledger

# Куда уедет текст. Две поверхности с разными возможностями, и разница не
# косметическая: в общем чате Единого рабочего места ответ проходит через
# marked + DOMPurify с закрытым списком тегов
# (`audit_workstation/static/js/shared/sanitize.js`, CHAT_MD_CONFIG). `<details>`
# и `<summary>` в него не входят — обёртка молча исчезает, а её содержимое
# вываливается в текст без заголовка. Плюс там `breaks: true`: каждый
# одиночный перевод строки становится <br>, поэтому абзац, разложенный по
# 80 символов, приезжает лесенкой.
SURFACE_WEB = "web"    # собственный интерфейс Follow Up
SURFACE_CHAT = "chat"  # общий чат AW через шину нанобота

# Что переживает санитайзер чата (CHAT_MD_CONFIG.ALLOWED_TAGS).
CHAT_ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "blockquote", "ul", "ol", "li",
    "strong", "em", "del", "code", "pre", "a", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
})


def _money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.2f} млрд ₽"
    if v >= 1e6:
        return f"{v / 1e6:.1f} млн ₽"
    if v >= 1e3:
        return f"{v / 1e3:.0f} тыс ₽"
    return f"{v:.0f} ₽"


def _clip(s: str, n: int = 220) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def render_body(c: AnswerContract, ledger: Ledger) -> tuple[str, List[Claim]]:
    """Тело + строки, каждая со своей проверкой и своими доказательствами."""
    kind = c.render or "quotes"
    fn = {
        "table_docs": _table_docs, "table_entity": _table_entity,
        "table_rows": _table_rows, "table_compare": _table_compare,
        "profile": _profile, "quotes": _quotes,
    }.get(kind, _quotes)
    return fn(ledger)


def _table_docs(ledger: Ledger) -> tuple[str, List[Claim]]:
    rows, claims = [], []
    for check_id, evs in sorted(ledger.by_check().items()):
        best = evs[0]
        rows.append(f"| {check_id} | {_clip(best.quote, 160)} |")
        claims.append(Claim(text=rows[-1], check_id=check_id,
                            evidence_uids=[best.uid]))
    if not rows:
        return "", []
    head = "| Проверка | Фрагмент |\n|---|---|\n"
    return head + "\n".join(rows), claims


def _table_entity(ledger: Ledger) -> tuple[str, List[Claim]]:
    """Только строки с ЦИТАТОЙ. Строка «проверка — прочерк — пусто» ничего не
    сообщает: она выглядит как найденное упоминание, хотя это просто акт,
    попавший в выдачу другим инструментом."""
    rows, claims = [], []
    for check_id, evs in sorted(ledger.by_check().items()):
        ev = next((e for e in evs if (e.quote or "").strip()), None)
        if ev is None or not check_id:
            continue
        where = {"case_text": "в описании кейса",
                 "requisites": "в реквизитах"}.get(ev.where or "", "—")
        rows.append(f"| {check_id} | {where} | {_clip(ev.quote, 140)} |")
        claims.append(Claim(text=rows[-1], check_id=check_id,
                            evidence_uids=[ev.uid]))
    if not rows:
        return "", []
    return ("| Проверка | Где упомянут | Фрагмент |\n|---|---|---|\n"
            + "\n".join(rows), claims)


def _table_rows(ledger: Ledger) -> tuple[str, List[Claim]]:
    rows, claims = [], []
    for e in ledger.evidence()[:40]:
        f = e.fields or {}
        rows.append(f"| {e.check_id} | {f.get('severity') or '—'} | "
                    f"{_money(f.get('financial_impact_rub'))} | "
                    f"{_clip(e.quote, 130)} |")
        claims.append(Claim(text=rows[-1], check_id=e.check_id,
                            evidence_uids=[e.uid]))
    if not rows:
        return "", []
    return ("| Проверка | Критичность | Ущерб | Отклонение |\n|---|---|---|---|\n"
            + "\n".join(rows), claims)


def _table_compare(ledger: Ledger) -> tuple[str, List[Claim]]:
    rows, claims = [], []
    for e in ledger.evidence():
        if not e.check_id:
            continue
        f = e.fields or {}
        rows.append(f"| {e.check_id} | {f.get('count', '—')} | "
                    f"{', '.join(f.get('categories') or []) or '—'} |")
        claims.append(Claim(text=rows[-1], check_id=e.check_id,
                            evidence_uids=[e.uid]))
    common = next((e for e in ledger.evidence() if not e.check_id), None)
    body = ("| Проверка | Отклонений | Категории |\n|---|---|---|\n"
            + "\n".join(rows)) if rows else ""
    if common:
        body += f"\n\n**Общее:** {_clip(common.quote, 300)}"
        claims.append(Claim(text="общее", check_id=None, axis="corpus"))
    return body, claims


def _profile(ledger: Ledger) -> tuple[str, List[Claim]]:
    e = next(iter(ledger.evidence()), None)
    if e is None:
        return "", []
    f = e.fields or {}
    body = "\n".join(f"- {k}: {v}" for k, v in f.items()
                     if not isinstance(v, (list, dict)))
    cats = f.get("categories") or []
    if cats:
        body += "\n- категории: " + ", ".join(map(str, cats[:12]))
    return body, [Claim(text=body, check_id=None, axis="corpus")]


def _quotes(ledger: Ledger) -> tuple[str, List[Claim]]:
    parts, claims = [], []
    for e in ledger.evidence()[:8]:
        head = f"**{e.check_id}**" + (f" · {e.header_path}" if e.header_path else "")
        parts.append(f"{head}\n> {_clip(e.quote, 500)}")
        claims.append(Claim(text=parts[-1], check_id=e.check_id,
                            evidence_uids=[e.uid]))
    return "\n\n".join(parts), claims


# ──────────────────────────────────────────────────────────────────
# Источники
# ──────────────────────────────────────────────────────────────────

def sources_block(ledger: Ledger, surface: str = SURFACE_WEB) -> str:
    """Источники компактно: где смотреть, а не стена текста.

    Прежний рендер вываливал восемь фрагментов по 500 символов — таблицы
    тарифов и титульные листы вперемешку. Читать это невозможно, а ответ
    тонул под ними.

    В своём интерфейсе список свёрнут в `<details>`. В общем чате обёртка
    вырезается санитайзером, поэтому там — обычный заголовок и нумерованный
    список: номер рядом с цитатой даёт аудитору способ сослаться
    («второй источник — не про то»), которого у маркеров нет.
    """
    items = []
    for e in ledger.evidence()[:8]:
        head = f"**{e.check_id}**"
        if e.header_path:
            head += f" · {e.header_path[:90]}"
        snippet = " ".join((e.quote or "").split())[:180]
        items.append((head, snippet))
    if not items:
        return ""

    if surface == SURFACE_CHAT:
        lines = ["**Источники**", ""]
        for i, (head, snippet) in enumerate(items, 1):
            lines.append(f"{i}. {head} — «{snippet}…»")
        return "\n".join(lines)

    lines = ["<details><summary>Источники ответа</summary>", ""]
    for head, snippet in items:
        lines.append(f"- {head}\n  «{snippet}…»")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Полнота и здоровье
# ──────────────────────────────────────────────────────────────────

def coverage_line(ledger: Ledger, relevance=None,
                  health: Optional[Dict] = None) -> str:
    """Строка полноты: из чисел, а не из ощущения.

    «Нашёл в трёх актах» читается как «в трёх и есть». Здесь сказано, из
    скольких просмотренных, чего не хватило и что при этом отказало.
    """
    cov = ledger.coverage()
    bits = [cov.line()]
    if cov.field_fill:
        bits.append("; ".join(f"поле «{k}» заполнено у {v:.0%}"
                              for k, v in cov.field_fill.items()))
    if relevance is not None and relevance.absent_in_corpus:
        bits.append("в корпусе не встречается: "
                    + ", ".join(f"«{t}»" for t in relevance.absent_in_corpus[:4]))
    for src in ledger.degraded_sources():
        bits.append(f"источник недоступен: {src}")
    if health and not health.get("ok", True):
        bits.append(f"индекс: {health.get('detail', 'состояние неизвестно')}")
    return " · ".join(b for b in bits if b)
