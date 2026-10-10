"""Аудит ссылок ``путь:строка`` в change'ах: дрейф номеров, а не вопли.

Зачем. Восемь активных change'ов накопили адреса вида ``file.py:1234``, которых
уже нет. Это молчаливый класс ошибки: текст читается гладко, исполнитель идёт
искать по нему и не находит. Первая же ручная проверка такого свипа дала 564
«расхождения», из которых большинство были ложными — голые имена файлов и
старые копии в ``.worktrees/``. Список, где ложных срабатываний больше
половины, хуже отсутствия списка: его отключают.

Поэтому здесь правило ОДНО и очень узкое:

  флаг, если (а) путь резолвится РОВНО в один файл репозитория по суффиксу
  пути, (б) номер строки больше длины файла, (в) в том же абзаце нет слов
  исторического характера (``удалён``, ``снят``, ``прежде``, ``раньше``, ``был``,
  ``до правки``, ``в архиве``, ``не перенесён``).

Условие (в) — то, что отделяет ошибку от правды: «модуль `X.py` удалён 2026-10-01»
остаётся верным и после того, как файл исчез, а вот «функция в `X.py:1234`»
перестаёт быть проверяемым. Молчаливый пропуск хуже громкой ошибки, но громкая
ошибка на нормальном тексте учит игнорировать инструмент.

Остальные классы (путь не резолвится нигде, голое имя файла) сюда НЕ попадают
намеренно: их разбирает человек, потому что они требуют суда о смысле — «модуль
удалён» и «автор ошибся» выглядят в тексте одинаково. Там же и ещё один класс,
о котором стоит знать:

* **Ссылка на файл, который change создаёт** (``src/pgembed/postgres_server.py``
  в `2026-10-09-dev-postgres`) — это не неточность, а часть плана. Проверка
  таких ссылок ложно ругалась бы на каждом новом change.
* **Ссылка на код внешней библиотеки** (``nanobot/channels/base.py``,
  ``nanobot/agent/tools/mcp.py``) не резолвится: индекс намеренно исключает
  ``.venv``. Иначе проверка номера строки зависела бы от версии библиотеки и
  падала бы в CI по причине, не связанной с этим репозиторием.
* **Неоднозначный суффикс** (``service/main.py`` — их шесть в дереве) не
  флагуется: выбрать файл по регулярке нельзя, а угадывать хуже, чем промолчать.

Запуск: ``python tools/change_ref_audit.py`` — печатает находки и выходит с
кодом 1, если они есть (удобно для CI). ``--quiet`` — только код возврата.
``--report-unresolved`` — дополнительно печатает пути без резолва (суд о смысле
за человеком, ошибкой не считается).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGES = ROOT / "openspec" / "changes"

EXT = r"(?:py|jsonc|json|md|sql|yaml|yml|txt|ts|tsx|js|cfg|ini|toml)"
REF = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*\."
                 + EXT + r"):(\d+)(?:-(\d+))?`")
# Продолжение адреса внутри того же пункта: «(`lib/core/code.py:500`) … (`:600`)».
#
# Их НЕТ в проверке, и это не упущение, а вывод из двух провалов подряд. Ссылка
# ``(`:684`)`` не называет файла: её файл восстанавливается эвристикой, и обе
# эвристики врали — по абзацу номер приписывался соседнему документу
# (TOOLS.md ↔ MCP-CONTRACTS.md), по пункту — SQL-файлу вместо спеки. Инструмент,
# который ругается на верном тексте, хуже отсутствия инструмента: на него
# перестают смотреть. Сокращённые ссылки считаются и печатаются, но не ругают.
BARE_REF = re.compile(r"(?<![A-Za-z0-9_.])`:(\d+)(?:-(\d+))?`")

# Маркеры прошлого. Список неполный, и это осознанно: лучше пропустить находку,
# чем заблокировать работу на верном историческом тексте.
HISTORICAL = (
    "удалён", "удален", "удалена", "снят", "снята", "снято", "прежде",
    "раньше", "до правки", "в архиве", "не перенесён", "не перенесен",
    "исторически", "устарел",
)

# Границы предложения. По абзацу фильтровать нельзя: в первой версии инструмента
# слово «была» в соседней фразе («формулировка была неверной») гасило проверку
# настоящей ссылки — инструмент рапортовал 0 при двух битых адресах.
SENTENCE_BREAK = re.compile(r"(?:;\s|\.\s|—\s|\n)")

# Границы слова обязательны: подстрочный поиск ловил «удаления» как «удалён»
# (тот же класс, что `.json`, съедавший `.jsonc`), и инструмент молчал ровно на
# тех местах, где правка была нужнее всего.
HISTORICAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in HISTORICAL) + r")\b"
)


def _is_historical(paragraph: str, at: int) -> bool:
    """Исторический маркер только в предложении ВОКРУГ ссылки, не во всём абзаце."""
    before = paragraph[:at]
    after = paragraph[at:]
    cut_before = SENTENCE_BREAK.search(before[-200:])
    if cut_before:
        before = before[-(200 - cut_before.end()):]
    cut_after = SENTENCE_BREAK.search(after)
    if cut_after:
        after = after[:cut_after.start()]
    local = (before + after).lower()
    return bool(HISTORICAL_RE.search(local))


def _skip_dirs(parts: tuple[str, ...]) -> bool:
    return any(p in ("__pycache__", "node_modules", ".git", ".worktrees", ".venv")
               for p in parts)


def build_index() -> dict[str, list[str]]:
    """Имя файла -> все пути, где оно лежит (без служебных каталогов)."""
    idx: dict[str, list[str]] = {}
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if _skip_dirs(p.parts):
            continue
        idx.setdefault(p.name, []).append(p.relative_to(ROOT).as_posix())
    return idx


def resolve(raw: str, idx: dict[str, list[str]]) -> str | None:
    """Резолв только когда путь указывает РОВНО на один файл.

    Неоднозначность — повод не флагать: два разных `db.py` в дереве означают,
    что автор имеет в виду один из них по контексту, и проверить это может
    только чтение, а не регулярка.
    """
    direct = ROOT / raw
    if direct.is_file():
        return raw
    base = raw.rsplit("/", 1)[-1]
    suffix = "/" + raw
    hits = [c for c in idx.get(base, []) if c.endswith(suffix)]
    return hits[0] if len(hits) == 1 else None


def line_count(rel: str) -> int:
    try:
        return len((ROOT / rel).read_text(encoding="utf-8",
                                          errors="replace").splitlines())
    except OSError:
        return -1


def items(text: str) -> list[tuple[str, int]]:
    """Блоки «пункт задачи — его продолжения», вместе с номером первой строки.

    Границы: строка пункта (``- [ ]`` / ``- [x]`` / ``-`` / ``*``) и заголовок.
    Абзац для этого слишком груб: в каноне соседние пункты идут без пустой
    строки, и привязка сокращённой ссылки ``(`:684`)`` к «последнему файлу
    абзаца» приписывала номер соседнему документу — инструмент выдавал бы
    находку на верном тексте, а это хуже отсутствия проверки.
    """
    out: list[tuple[str, int]] = []
    cur: list[str] = []
    start = 1
    for i, line in enumerate(text.splitlines(), 1):
        is_item = bool(re.match(r"^\s*(?:[-*+]\s+\[[ xX]\]|[-*+]\s+\S|#\s)", line))
        if is_item:
            if cur:
                out.append(("\n".join(cur), start))
            cur, start = [line], i
        else:
            if not cur:
                start = i
            cur.append(line)
    if cur:
        out.append(("\n".join(cur), start))
    return out


def paragraphs(text: str) -> list[tuple[str, int]]:
    """Абзацы вместе с номером первой строки в файле.

    Номер обязателен: отчёт, где «proposal.md:16» означает шестнадцатую строку
    абзаца, хуже отсутствия отчёта — по нему идут и правят не туда.
    """
    chunks: list[tuple[str, int]] = []
    cur: list[str] = []
    start = 1
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not cur:
                start = i
            cur.append(line)
        elif cur:
            chunks.append(("\n".join(cur), start))
            cur = []
    if cur:
        chunks.append(("\n".join(cur), start))
    return chunks


def audit() -> tuple[list[str], int]:
    idx = build_index()
    findings: list[str] = []
    checked = 0
    bare = 0
    for ch in sorted(d for d in CHANGES.iterdir() if d.is_dir() and d.name != "archive"):
        for doc in sorted(ch.rglob("*.md")):
            docrel = doc.relative_to(ROOT).as_posix()
            for para, para_start in items(doc.read_text(encoding="utf-8")):
                for m in REF.finditer(para):
                    raw, hi = m.group(1), max(int(m.group(2)), int(m.group(3) or 0))
                    checked += 1
                    if _is_historical(para, m.start()):
                        continue
                    rel = resolve(raw, idx)
                    if rel is None:
                        continue
                    n = line_count(rel)
                    if 0 < n < hi:
                        line_no = para_start + para[:m.start()].count("\n")
                        findings.append(
                            f"{docrel}:{line_no}\n"
                            f"    ссылка: {raw}:{m.group(2)}"
                            + (f"-{m.group(3)}" if m.group(3) else "")
                            + f"\n    файл:   {rel} — строк {n}")
                bare += len(BARE_REF.findall(para))
    return findings, checked, bare


def unresolved() -> tuple[list[str], int]:
    """Пути, которые не резолвятся нигде. Отчёт, а не ошибка.

    Такой класс требует суда о смысле: «модуль `cache_provider.py` удалён» и
    «автор ошибся в пути» выглядят в тексте одинаково, и автоматика здесь
    гарантированно даст ложные срабатывания. Поэтому — только печать.
    """
    idx = build_index()
    rows: list[str] = []
    checked = 0
    for ch in sorted(d for d in CHANGES.iterdir() if d.is_dir() and d.name != "archive"):
        for doc in sorted(ch.rglob("*.md")):
            docrel = doc.relative_to(ROOT).as_posix()
            for para, start in paragraphs(doc.read_text(encoding="utf-8")):
                for m in REF.finditer(para):
                    raw = m.group(1)
                    if "/" not in raw:
                        continue
                    checked += 1
                    if resolve(raw, idx) is None:
                        rows.append(f"{docrel}:{start + para[:m.start()].count(chr(10))}"
                                    f"  {raw}")
    return rows, checked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true",
                    help="только код возврата")
    ap.add_argument("--report-unresolved", action="store_true",
                    help="дополнительно напечатать пути, которые нигде не "
                         "резолвятся (суд о смысле — за человеком)")
    args = ap.parse_args()

    findings, checked, bare = audit()
    if not args.quiet:
        print(f"ПРОВЕРЕНО ССЫЛОК С НОМЕРАМИ СТРОК: {checked}")
        print(f"НАХОДОК: {len(findings)}")
        print(f"СОКРАЩЁННЫХ ССЫЛОК (`:NNN`) — считаются, не проверяются: {bare}")
        for f in findings:
            print(f"  {f}")
    if args.report_unresolved:
        rows, total = unresolved()
        print(f"\nПУТЕЙ БЕЗ РЕЗОЛВА (требуют суда о смысле): {len(rows)} из {total}")
        for r in rows:
            print(f"  {r}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())