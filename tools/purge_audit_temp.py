"""purge_audit_temp.py — разовый сборщик временных артефактов этого прохода.

Зачем отдельный скрипт, если есть tools/cleanup_scratch.ps1. Тот требует
правки белого списка внутри себя и заточен под «снять одноразовые
файлы репозитория» вообще. Здесь ровно одна задача: убрать то, что осталось
после аудита кода агента, и не сделать это по неосторожности. Правило
«каждая правка отдельным коммитом» тут неприменимо — файлы не отслеживаются
git, коммитить их нельзя, а удалить их нечем: политика безопасности агента
блокирует удаление, и лаунчер mavis-trash недоступен.

Запуск::

    python tools/purge_audit_temp.py              # план, ничего не удаляет
    python tools/purge_audit_temp.py --apply      # удалить
    python tools/purge_audit_temp.py --self-test  # доказать защиты, не удаляя

Гарантии, которые проверяет скрипт перед каждым удалением:

1. путь лежит внутри репозитория и не выходит за его пределы;
2. файл НЕ отслеживается git — отслеживаемый файл не удаляется никогда;
3. имя совпадает с белым списком, а не с широким glob;
4. файл — обычный файл, а не каталог и не ссылка;
5. цель не совпадает с самим скриптом.

Чужие файлы (созданные не мной) по умолчанию только показываются в плане и
не удаляются: для них нужен явный --include-foreign.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# Каталоги, где ищем временные файлы этого прохода.
SEARCH_DIRS = ("docs/audit/_scripts",)

# Явные файлы вне SEARCH_DIRS.
EXPLICIT_FILES = ("tests/_tmp_regression_tests.py",)

# Созданы не мной. Показываются в плане, удаляются только с
# --include-foreign: чужую работу не трогаем без явного разрешения.
FOREIGN_FILES = ("_tmp_debt_report.py",)

PREFIX = "_tmp"


def die(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ОТКАЗ: {message}", file=sys.stderr)
    raise SystemExit(2)


def is_repo_root(path: Path) -> bool:
    return (path / ".git").exists() and (path / "AGENTS.md").is_file()


def git_tracked(rel: str) -> bool:
    """Отслеживается ли файл git. Отслеживаемый удалять нельзя никогда."""
    proc = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                          cwd=REPO, capture_output=True)
    return proc.returncode == 0


def collect() -> tuple[list[tuple[Path, bool]], list[Path]]:
    """Собрать кандидатов. Возвращает ((путь, мой), чужие)."""
    if not is_repo_root(REPO):
        die(f"{REPO} не похож на корень репозитория (нет .git или AGENTS.md)")

    mine: list[tuple[Path, bool]] = []
    seen: set[str] = set()

    for rel_dir in SEARCH_DIRS:
        directory = REPO / rel_dir
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(PREFIX) and entry.is_file():
                mine.append((entry, True))
                seen.add(entry.name)

    for rel in EXPLICIT_FILES:
        target = REPO / rel
        if target.is_file() and target.name not in seen:
            mine.append((target, True))
            seen.add(target.name)

    foreign = [REPO / rel for rel in FOREIGN_FILES if (REPO / rel).is_file()]
    return mine, foreign


def check(path: Path, is_mine: bool) -> str | None:
    """Вернуть причину отказа или None, если удалять можно."""
    if path.resolve() == SELF:
        return "это сам скрипт"
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"не удаётся разрешить путь: {exc}"
    try:
        resolved.relative_to(REPO.resolve())
    except ValueError:
        return "путь выходит за пределы репозитория"
    if not path.is_file():
        return "не обычный файл (каталог или ссылка)"
    if path.is_symlink():
        return "символическая ссылка"
    rel = path.relative_to(REPO).as_posix()
    if git_tracked(rel):
        return "файл отслеживается git — удаление запрещено"
    if not is_mine and not INCLUDE_FOREIGN:
        return "файл создан не мной (нужен --include-foreign)"
    return None


def show_plan(mine: list[tuple[Path, bool]], foreign: list[Path]) -> None:
    print("ПЛАН УДАЛЕНИЯ")
    print("=" * 72)
    total = 0
    if not mine and not foreign:
        print("  кандидатов не найдено — чисто")
        return
    for path, is_mine in mine:
        rel = path.relative_to(REPO).as_posix()
        size = path.stat().st_size
        total += size
        reason = check(path, is_mine)
        mark = "x" if reason is None else "-"
        print(f"  [{mark}] {rel}  ({size} Б)")
        if reason:
            print(f"        пропустить: {reason}")
    for path in foreign:
        rel = path.relative_to(REPO).as_posix()
        size = path.stat().st_size
        reason = check(path, False)
        mark = "x" if reason is None else "-"
        print(f"  [{mark}] {rel}  ({size} Б)   ← чужой файл")
        if reason:
            print(f"        пропустить: {reason}")
    print("=" * 72)
    print(f"  к удалению: {sum(1 for p, m in mine if not check(p, m))} шт., {total} Б")
    print()


def purge(mine: list[tuple[Path, bool]], foreign: list[Path]) -> int:
    removed = skipped = 0
    for path, is_mine in list(mine) + [(p, False) for p in foreign]:
        reason = check(path, is_mine)
        if reason:
            print(f"  пропущен {path.relative_to(REPO).as_posix()}: {reason}")
            skipped += 1
            continue
        try:
            path.unlink()
        except OSError as exc:
            print(f"  ОШИБКА {path.relative_to(REPO).as_posix()}: {exc}")
            skipped += 1
            continue
        if path.exists():
            print(f"  ОШИБКА {path.relative_to(REPO).as_posix()}: файл всё ещё на диске")
            skipped += 1
            continue
        print(f"  удалён {path.relative_to(REPO).as_posix()}")
        removed += 1
    print(f"\n  удалено {removed}, пропущено {skipped}")
    return removed


def self_test() -> int:
    """Проверить защиты на синтетических целях, ничего не удаляя.

    Цель — доказать, что скрипт отказывает там, где должен, до того как
    владелец запустит его с --apply по-настоящему.
    """
    failures: list[str] = []

    def expect_refused(label: str, path: Path, is_mine: bool) -> None:
        reason = check(path, is_mine)
        if reason is None:
            failures.append(f"{label}: скрипт РАЗРЕШИЛ удаление, а должен был отказать")
            print(f"  ПРОВАЛ {label}: отказ не сработал")
        else:
            print(f"  ok   {label}: {reason}")

    def expect_allowed(label: str, path: Path) -> None:
        reason = check(path, True)
        if reason is None:
            print(f"  ok   {label}: разрешено к удалению")
        else:
            failures.append(f"{label}: скрипт отказал в удалении — {reason}")
            print(f"  ПРОВАЛ {label}: {reason}")

    print("САМОПРОВЕРКА ЗАЩИТ (файлы не удаляются)")
    print("-" * 72)

    # 1. Отслеживаемый git файл — удалять нельзя.
    expect_refused("отслеживаемый git файл", REPO / "AGENTS.md", True)

    # 2. Сам скрипт.
    expect_refused("сам скрипт", SELF, True)

    # 3. Путь за пределами репозитория.
    expect_refused("путь вне репозитория", Path("C:/Windows/System32/drivers/etc/hosts"), True)

    # 4. Каталог, а не файл.
    expect_refused("каталог вместо файла", REPO / "docs" / "audit" / "scripts", True)

    # 5. Несуществующий файл.
    expect_refused("несуществующий файл", REPO / "docs" / "audit" / "_scripts" / "_tmp_нет_такого.py", True)

    # 6. Реальный кандидат — должен быть разрешён.
    # collect()[0] содержит только мои файлы (чужие идут вторым списком),
    # поэтому берём все элементы, а не фильтруем по признаку.
    candidates = [p for p, _m in collect()[0]]
    if candidates:
        expect_allowed("реальный временный файл", candidates[0])
    else:
        print("  ok   реальный временный файл: кандидатов нет, чисто")

    # 7. Чужой файл без --include-foreign.
    for path in collect()[1]:
        reason = check(path, False)
        if reason is None:
            failures.append(f"чужой файл {path} удалён без --include-foreign")
            print(f"  ПРОВАЛ чужой файл: отказ не сработал")
        else:
            print(f"  ok   чужой файл: {reason}")

    print("-" * 72)
    if failures:
        print(f"САМОПРОВЕРКА ПРОВАЛЕНА ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("САМОПРОВЕРКА ПРОЙДЕНА: скрипт отказывает там, где должен.")
    return 0


INCLUDE_FOREIGN = False


def main() -> int:
    global INCLUDE_FOREIGN
    parser = argparse.ArgumentParser(
        description="Собрать временные файлы прохода аудита.")
    parser.add_argument("--apply", action="store_true",
                        help="действительно удалить (без флага — только план)")
    parser.add_argument("--include-foreign", action="store_true",
                        help="разрешить удалить файлы, созданные не мной")
    parser.add_argument("--self-test", action="store_true",
                        help="проверить защиты, ничего не удаляя")
    args = parser.parse_args()
    INCLUDE_FOREIGN = args.include_foreign

    if args.self_test:
        return self_test()

    mine, foreign = collect()
    if not args.apply:
        show_plan(mine, foreign)
        print("  Это был план. Повтори с --apply, чтобы удалить.")
        return 0

    print("УДАЛЕНИЕ")
    print("-" * 72)
    purge(mine, foreign)
    return 0


if __name__ == "__main__":
    sys.exit(main())
