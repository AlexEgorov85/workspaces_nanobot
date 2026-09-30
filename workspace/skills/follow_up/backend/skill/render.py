"""Приведение ответа к тому, что переживёт дорогу до чата аудитора.

Дорога длинная и с двумя сужениями.

**Первое — чужая модель.** Наш текст не показывается напрямую: инструмент
возвращает JSON агенту нанобота, и в чат попадает то, что напишет ЕГО модель.
Пересказ ломает построчную верификацию: проверенным остаётся текст, который
отдали мы, а не тот, который она сочинила по мотивам. Поэтому в конверте
первым полем идёт инструкция отдать `answer_md` дословно, и то же самое
написано в описании каждого инструмента (`mcp_server._TOOLS`). Гарантии это
не даёт — но это всё, чем можно управлять со своей стороны.

**Второе — санитайзер.** Ответ рендерится через marked и вычищается
DOMPurify по закрытому списку тегов (`audit_workstation/static/js/shared/
sanitize.js`, `CHAT_MD_CONFIG`). Всё, чего в списке нет, вырезается вместе с
разметкой, а содержимое вываливается в текст. Плюс `breaks: true`: одиночный
перевод строки становится `<br>`.

Поэтому здесь — не «оформление», а приведение к подмножеству, которое доедет.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from backend.core.render import CHAT_ALLOWED_TAGS

# Инструкция агенту. Лежит первым ключом конверта: модель читает результат
# инструмента сверху вниз, и указание должно попасться раньше текста.
RELAY_NOTE = (
    "Текст в answer_md уже готов к показу и проверен построчно. "
    "Выведи его пользователю ДОСЛОВНО: не пересказывай, не сокращай, "
    "не дополняй своими словами и не меняй нумерацию источников. "
    "Свой комментарий, если он нужен, добавляй отдельным абзацем после."
)

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
# HTML-комментарии. В своём интерфейсе ими помечен фокус диалога
# (`<!-- fu:focus КМ-99-12345 -->` в свёртке карточки) — память читает их
# следующим ходом. Наружу они не нужны: санитайзер их выбросит, но до этого
# они успеют попасть в контекст чужой модели, и та вполне может их
# процитировать пользователю.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANKS_RE = re.compile(r"\n{3,}")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _drop_unsupported_tags(text: str) -> str:
    """Убрать теги, которых нет в белом списке чата, сохранив содержимое.

    Санитайзер сделал бы это сам, но молча и хуже: `<details><summary>
    Источники ответа</summary>` превращается в висящую строку «Источники
    ответа» без всякого признака, что это заголовок списка. Лучше не
    отправлять того, что всё равно вырежут.
    """
    def _sub(m: re.Match) -> str:
        return m.group(0) if m.group(1).lower() in CHAT_ALLOWED_TAGS else ""

    return _TAG_RE.sub(_sub, text)


def _space_out_tables(text: str) -> str:
    """Пустая строка перед таблицей.

    При `breaks: true` строка, приклеенная к абзацу сверху, разбирается как
    продолжение абзаца, и таблица приезжает трубами наружу.
    """
    out: List[str] = []
    for i, line in enumerate(text.split("\n")):
        starts_table = (
            _TABLE_ROW_RE.match(line)
            and out and out[-1].strip()
            and not _TABLE_ROW_RE.match(out[-1])
        )
        if starts_table:
            out.append("")
        out.append(line)
    return "\n".join(out)


def for_chat(text: str) -> str:
    """Текст, пригодный для общего чата: только разрешённая разметка."""
    if not text:
        return ""
    t = _COMMENT_RE.sub("", text)
    t = _drop_unsupported_tags(t)
    t = _space_out_tables(t)
    t = _BLANKS_RE.sub("\n\n", t)
    return t.strip()


# Колонки реестра: что показываем и как называем. Порядок — от «какая
# проверка» к «сколько денег»: аудитор читает слева направо и первым делом
# ищет свою КМ.
_DEV_COLUMNS = (
    ("check_id", "КМ"),
    ("category", "Категория"),
    ("severity", "Критичность"),
    ("damage_rub", "Ущерб"),
    ("system", "Система"),
    ("unit", "Подразделение"),
)
_DEV_ROWS_MAX = 40


def _cell(value: Any) -> str:
    """Значение ячейки: без переносов и без труб, иначе таблица развалится."""
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "/").replace("\n", " ").strip()[:80]


def deviations_table(rows: List[Dict[str, Any]]) -> str:
    """Реестр отклонений таблицей GFM.

    Показываем только колонки, где хоть у одной строки есть значение: пустой
    столбец «Система» на весь экран — это не «нет данных», это шум, из-за
    которого не видно заполненных.
    """
    if not rows:
        return ""
    cols = [(k, title) for k, title in _DEV_COLUMNS
            if any(r.get(k) not in (None, "") for r in rows)]
    if not cols:
        cols = [("check_id", "КМ")]

    shown = rows[:_DEV_ROWS_MAX]
    out = ["| " + " | ".join(t for _, t in cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in shown:
        out.append("| " + " | ".join(_cell(r.get(k)) for k, _ in cols) + " |")
    if len(rows) > len(shown):
        out.append("")
        out.append(f"_Показаны первые {len(shown)} строк из {len(rows)}._")
    return "\n".join(out)


def envelope(result: Dict[str, Any], *,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Конверт инструмента: инструкция, текст, источники, честность.

    Порядок ключей осмысленный, а не алфавитный: сначала как обращаться с
    ответом, потом сам ответ, и только потом служебное. Пустые поля
    выбрасываются — каждый лишний ключ это токены в чужом контексте и ещё
    один повод модели что-нибудь про него сказать.
    """
    text = for_chat(str(result.get("answer_md") or ""))
    out: Dict[str, Any] = {}

    if text:
        out["display"] = "verbatim"
        out["note"] = RELAY_NOTE
    out["ok"] = bool(result.get("ok"))
    if text:
        out["answer_md"] = text

    for key in ("sources", "coverage", "checks", "artifact", "session_key"):
        val = result.get(key)
        if val:
            out[key] = val
    if result.get("insufficient"):
        out["insufficient"] = True
    if result.get("reason"):
        out["reason"] = result["reason"]
    if result.get("clarify"):
        out["clarify"] = result["clarify"]
    if extra:
        out.update(extra)
    return out
