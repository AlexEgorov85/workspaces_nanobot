#!/usr/bin/env python3
"""Проверка ссылок ``путь:строка`` в канонических спеках.

Инструмент, а **не** страж: он ничего не проверяет в тестах и не обязан быть
зелёным. Долг канона зафиксирован ниже и снимается постепенно; превращать
инструмент в красный тест можно только вместе с нулевым долгом, иначе его
начнут отключать (ровно это уже случилось с широкой проверкой снятых ссылок,
см. комментарий ``_REMOVED_AGENT_MODULES`` в ``tests/test_docs_consistency.py``).

Что делает. Для каждой ссылки вида `` `some/path.py:42` `` в ``spec.md``:

1. **Файл существует.** Путь ищется от корня репозитория, от корня платформы
   (``mcp-platform/``) и — если не найден — **по короткой форме** обходом дерева.
   Короткая форма (``workspace.py:283``, ``execution/pipeline.py:320``) в каноне
   обычна, поэтому её резолв не ошибка; но неоднозначное совпадение — ошибка:
   ссылка, которую можно прочитать двумя способами, читается неверно.
2. **Строка в пределах файла**, а диапазон не выходит за конец.
3. **Строка не пустая.** Ссылка на пустую строку не несёт смысла, и почти всегда
   это сдвиг на единицу — объявление класса на 92 вместо 91. Читатель по ссылке
   не увидит ничего.

Чего инструмент **не** делает: он не сверяет, что по ссылке написано именно то, о
чём говорит текст. Он ловит битые ссылки, а не ложные утверждения.

Текущее состояние (замер 2026-10-07): 793 ссылки, 106 расхождений. Обе спеки,
развёрнутые в этом же заходе, — чистые: ``runtime/session-files`` 69/0,
``runtime/call-contract`` 45/0.

Запуск:

    python tools/check_spec_citations.py                 # все спеки
    python tools/check_spec_citations.py <путь-к-spec>   # одна или несколько
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = ROOT / "openspec" / "specs"

#: ``путь/к/файлу.py:42`` внутри обратных кавычек. Расширение обязательно: без него
#: ссылку не отличить от упоминания ``Class.method: something``. Путь берётся
#: любыми символами, кроме пробела, двоеточия и обратной кавычки — иначе проба с
#: несуществующим файлом на кириллице прошла бы молча.
CITE_RE = re.compile(r"`([^`\s:]+\.(?:py|md|json|jsonc|sql|toml|yaml|txt)):(\d+)(?:-(\d+))?`")

#: Куда искать файл целиком: от корня агента и от корня платформы.
RESOLVE_PREFIXES = ("", "mcp-platform/", "mcp-platform/libs/", "workspace/")

#: Где искать короткую форму. Обход по этим деревьям; ``data_store/`` и прочее
#: исключено, иначе проверка читала бы бэкапы сессий — полные копии репозитория.
GLOB_ROOTS = ("mcp-platform", "lib", "workspace", "tools", "scripts", "tools")

SKIP_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", "data_store", "node_modules"}


def _library_roots() -> list[Path]:
    """Каталоги установленных пакетов, где лежит код вне репозитория.

    Канон законно ссылается на upstream: ``nanobot/agent/hook.py:33``,
    ``mcp/server/lowlevel/server.py:527``. Этих файлов нет в репозитории, и
    без этого обхода инструмент объявлял бы **ложные** расхождения на каждой
    такой ссылке — а ложное расхождение хуже отсутствия проверки: по нему
    полезли бы «чинить» живой и верный канон.

    Оговорка: номер строки в установленной библиотеке верно только для той
    версии, что стоит в окружении. Поэтому такие ссылки проверяются на
    существование строки, но не на её смысл.
    """
    roots: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        p = Path(entry)
        if p.is_dir() and p.resolve() != ROOT:
            roots.append(p)
    return roots


def resolve(raw: str, *, self_spec: Path | None = None) -> tuple[Path | None, str]:
    """Вернуть (файл, примечание). ``None`` — не нашёлся или неоднозначен."""
    rel = raw.replace("\\", "/").lstrip("./")
    # Ссылка на саму проверяемую спеку: путь относительно её каталога.
    if rel == "spec.md" and self_spec is not None:
        return self_spec, "сама проверяемая спека"
    for prefix in RESOLVE_PREFIXES:
        candidate = (ROOT / prefix / rel).resolve()
        if candidate.is_file():
            return candidate, ""

    for base in _library_roots():
        candidate = base / rel
        if candidate.is_file():
            return candidate, f"установленный пакет ({base.name})"

    tail = rel.split("/", 1)[1] if rel.startswith("…/") else rel
    matches = [
        p
        for base in GLOB_ROOTS
        for p in (ROOT / base).rglob(tail)
        if p.is_file() and not any(part in SKIP_DIR_NAMES for part in p.parts)
    ]
    if len(matches) == 1:
        return matches[0], "найдено по короткой форме"
    if len(matches) > 1:
        listing = ", ".join(m.relative_to(ROOT).as_posix() for m in matches[:5])
        return None, f"НЕОДНОЗНАЧНО ({len(matches)} файлов): {listing}"
    return None, "не найдено ни по полному пути, ни по короткой форме"


def check(spec: Path, *, verbose: bool) -> tuple[int, int, list[str]]:
    text = io.open(spec, encoding="utf-8").read()
    ok = bad = 0
    problems: list[str] = []
    for m in CITE_RE.finditer(text):
        raw, start, end = m.group(1), int(m.group(2)), m.group(3)
        target, how = resolve(raw, self_spec=spec)
        if target is None:
            bad += 1
            problems.append(f"ФАЙЛА НЕТ: {raw}:{start} — {how}")
            continue
        lines = io.open(target, encoding="utf-8", errors="replace").read().split("\n")
        if start < 1 or start > len(lines):
            bad += 1
            problems.append(f"СТРОКИ НЕТ: {raw}:{start} (в файле {len(lines)})")
            continue
        stop = int(end) if end else start
        if stop > len(lines):
            bad += 1
            problems.append(f"КОНЕЦ ДИАПАЗОНА ЗА ФАЙЛОМ: {raw}:{start}-{stop}")
            continue
        if not any(lines[i - 1].strip() for i in range(start, stop + 1)):
            bad += 1
            near = lines[start - 2].strip()[:70] if start >= 2 else ""
            problems.append(f"ПУСТАЯ СТРОКА: {raw}:{start}{'-' + end if end else ''} — рядом: {near!r}")
            continue
        ok += 1
        if verbose:
            # Путь может лежать ВНЕ репозитория (установленная библиотека),
            # и ``relative_to(ROOT)`` на нём бросает исключение. Показываем
            # тогда абсолютный путь.
            try:
                shown = target.relative_to(ROOT).as_posix()
            except ValueError:
                shown = str(target)
            note = f"  [{shown}{'; ' + how if how else ''}]" if shown != raw else ""
            print(f"  {raw}:{start}{'-' + end if end else ''}{note}")
            snippet = " / ".join(x.strip() for x in lines[start - 1:stop] if x.strip())
            print(f"      -> {snippet[:157]}{'...' if len(snippet) > 160 else ''}")
    return ok, bad, problems


def all_specs() -> list[Path]:
    found: list[Path] = []
    for cat in sorted(SPECS_DIR.iterdir()):
        if not cat.is_dir() or cat.name.startswith("."):
            continue
        for spec in sorted(cat.iterdir()):
            if (spec / "spec.md").is_file():
                found.append(spec / "spec.md")
    return found


def main() -> int:
    specs = [Path(p) for p in sys.argv[1:]] or all_specs()
    verbose = bool(sys.argv[1:])
    total_ok = total_bad = 0
    all_problems: list[str] = []
    for spec in specs:
        ok, bad, problems = check(spec, verbose=verbose)
        total_ok += ok
        total_bad += bad
        if problems:
            print(f"{spec.as_posix()}: {ok + bad} ссылок, расхождений {bad}")
            all_problems.extend(f"{spec.as_posix()}: {p}" for p in problems)
    print()
    if all_problems:
        print(f"РАСХОЖДЕНИЯ ({len(all_problems)}):")
        for p in all_problems:
            print("  " + p)
    else:
        print("РАСХОЖДЕНИЙ НЕТ — все ссылки ведут в существующие непустые строки.")
    print(f"ИТОГО: ссылок {total_ok}, расхождений {total_bad}")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())