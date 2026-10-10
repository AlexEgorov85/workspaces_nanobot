"""Аудит перекрёстных ссылок канона: `категория/имя` → существующая спека.

Зачем. В каноне нашлись 12 висящих ссылок: `runtime/db-logging` (настоящее
имя — `observability/logging-db`), `storage/session-hybridization` (категория
переименована в `sessions`), `runtime/cli-client` (спека удалена вместе с
отменённым change). Читаются они гладко и уводят в никуда, а
`check_spec_citations.py` их не видит: тот проверяет ссылки `путь:строка`, а не
идентификаторы спек.

Правило намеренно узкое, как и в `change_ref_audit.py`:

* категория берётся из дерева, а не зашивается — иначе проверка устареет вслед
  за переименованием;
* ссылка считается висящей, только если её категория есть в дереве, а
  `категория/имя` — нет, И в том же предложении нет слов исторического
  характера («удалён», «снят», «прежде», «был в архиве»…);
* пути к коду (`lib/…`, `mcp-platform/…`, `tools/…`) и псевдоидентификаторы
  вроде `try/except` отсекаются списком префиксов;
* **имя есть в другой категории** — тоже находка, даже если сама категория
  пропала. Первая версия молчала здесь, и это стоило четырёх ссылок: категорию
  `storage` переименовали в `sessions`, и четыре упоминания
  `storage/session-hybridization` выпали из проверки, потому что категории
  `storage` больше нет в дереве;
* **свой H1 сверяется с местом файла**, но только если он написан формой
  `# категория/имя Specification`. Заголовки словами («# Модель архитектурного
  компонента») — обычный стиль канона и сверять их не с чем.

Историческая ссылка на снятую спеку — верна по определению, и ломать её было бы
хуже, чем оставить: читатель узнает, что предмет уходил. Поэтому фильтр по
предложению, а не по абзацу: иначе соседняя фраза гасит настоящую находку
(ровно эта ошибка была в первой версии `change_ref_audit.py`).

Чего инструмент не ловит и что решается чтением: ссылки на предметы, для
которых спеки нет вовсе (`runtime/message-delivery`, `data/journal`,
`runtime/agent-loop`). Подобрать им замену нечем — значит предмет не
обслуживается, и это уже решение владельца, а не правка формулировки.

Запуск: ``python tools/spec_crosslink_audit.py`` — код 1 при находках.
``--quiet`` — только код возврата.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "openspec" / "specs"

CANDIDATE = re.compile(r"`([a-z][a-z0-9-]*/[a-z0-9-]+)`")

CODE_PREFIXES = (
    "lib/", "libs/", "mcp-platform/", "tools/", "workspace/", "sql/", "docs/",
    "tests/", ".github/", "openspec/", "src/", "node_modules/",
)
NOT_A_SPEC = ("try/", "if/", "for/", "while/")

HISTORICAL = (
    "удалён", "удален", "удалена", "удалено", "снят", "снята", "снято",
    "прежде", "раньше", "до правки", "в архиве", "не перенесён",
    "не перенесен", "исторически", "устарел", "было", "была", "было",
)
HISTORICAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in HISTORICAL) + r")\b")
SENTENCE_BREAK = re.compile(r"(?:;\s|\.\s|—\s|\n)")
# Только форма «# категория/имя Specification»; заголовки словами пропускаем.
H1_ID = re.compile(r"^#\s*([a-z][a-z0-9-]*/[a-z0-9-]+)\s+Specification\s*$")


def spec_ids() -> tuple[set[str], set[str]]:
    """Идентификаторы спек и множество категорий, оба — из дерева."""
    ids: set[str] = set()
    for path in SPECS.rglob("spec.md"):
        category = path.parent.parent.name
        ids.add(f"{category}/{path.parent.name}")
    cats = {i.split("/", 1)[0] for i in ids}
    return ids, cats


def items(text: str) -> list[tuple[str, int]]:
    """Пункты списка вместе с номером первой строки — границы для привязки."""
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


def _historical(paragraph: str, at: int) -> bool:
    """Исторический маркер только в предложении вокруг ссылки."""
    before, after = paragraph[:at], paragraph[at:]
    head = before[-200:]                       # не больше 200 символов контекста
    cut = SENTENCE_BREAK.search(head)
    if cut:
        head = head[cut.end():]
    cut = SENTENCE_BREAK.search(after)
    if cut:
        after = after[:cut.start()]
    return bool(HISTORICAL_RE.search((head + after).lower()))


def audit() -> tuple[list[str], int]:
    ids, cats = spec_ids()
    findings: list[str] = []
    checked = 0
    for path in sorted(SPECS.rglob("spec.md")):
        rel = path.relative_to(ROOT).as_posix()
        actual = f"{path.parent.parent.name}/{path.parent.name}"
        text = path.read_text(encoding="utf-8")

        declared = H1_ID.match(text.splitlines()[0] if text else "")
        if declared and declared.group(1) != actual:
            findings.append(
                f"{rel}:1\n"
                f"    H1 объявляет `{declared.group(1)}`, файл лежит в `{actual}`")

        for block, start in items(text):
            for m in CANDIDATE.finditer(block):
                target = m.group(1)
                if target.startswith(CODE_PREFIXES) or target.startswith(NOT_A_SPEC):
                    continue
                category, _, name = target.partition("/")
                near = sorted(i for i in ids if i.split("/", 1)[1] == name)
                if category not in cats and not near:
                    continue          # это не идентификатор спеки вовсе
                checked += 1
                if _historical(block, m.start()):
                    continue
                if target in ids:
                    continue
                line_no = start + block[:m.start()].count("\n")
                hint = ("  похожее: " + ", ".join(near)) if near else \
                       "  имени нет ни в одной категории"
                findings.append(f"{rel}:{line_no}\n"
                                f"    ссылка: `{target}`{hint}")
    return findings, checked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    findings, checked = audit()
    if not args.quiet:
        print(f"ПРОВЕРЕНО ССЫЛОК НА СПЕКИ: {checked}")
        print(f"НАХОДОК: {len(findings)}")
        for f in findings:
            print(f"  {f}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())