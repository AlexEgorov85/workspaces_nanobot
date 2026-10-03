"""remove_legacy_query_tests.py — снос тестов снесённых обёрток.

Второй и последний проход D6 (change ``2026-10-03-mcp-native-tools``).
Первый — ``tools/remove_legacy_query_tools.py`` — снёс сами инструменты;
этот снимает их тесты и фикстуры, которые без них не запускаются.

Почему снос, а не перенос. Покрытие не потеряно — оно ушло на платформу вместе
с кодом:

* аудит — ``mcp-platform/tests/test_audit_capability.py`` и двенадцать
  ``test_audit_lib_*``;
* вектора — семь ``test_vectors_*``;
* ``history_search`` — ``test_data_service.py::TestHistorySearchIsolation``
  (область обязательна, значения параметризованы) и
  ``::TestHistorySearchFilters`` (все фильтры на месте).

Бенчмарк сносится отдельно от остальных: он измерял ILIKE и ORDER BY в
агентском tool'е. Такого SQL в агенте больше нет, а переписывать эмулятор на
запрос платформы — отдельная задача для её стороны.

Запуск::

    python tools/remove_legacy_query_tests.py --repo .            # план
    python tools/remove_legacy_query_tests.py --repo . --apply    # снести
    python tools/remove_legacy_query_tests.py --self-test         # защиты

Защиты те же, что в первом скрипте: белый список целей, проверка отслеживания
git, отказ за пределы репозитория, отказ трогать защищённые префиксы. Путь
репозитория — только из аргумента.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: Ровно эти файлы. Список закрыт: широкий glob по tests/ задел бы
#: ``test_history_search_benchmark.py``, который и должен уйти, но и другие.
TARGETS = (
    "tests/test_audit_analyzer_query_tool.py",
    "tests/test_legal_summarizer_query_tool.py",
    "tests/test_history_search_tool.py",
    "tests/test_history_search_benchmark.py",
    "tests/fixtures/history_search/gateway_logs.jsonl",
    "tests/fixtures/history_search/scenarios.json",
)

#: Пустой каталог фикстур убрать, чтобы в дереве не осталось мёртвой папки.
EMPTY_DIR = "tests/fixtures/history_search"

PROTECTED_PREFIXES = ("workspace/", "lib/", "tools/", "mcp-platform/", "sql/")


def die(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ОТКАЗ: {message}", file=sys.stderr)
    raise SystemExit(2)


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True
    ).stdout.decode("utf-8")


def is_repo_root(path: Path) -> bool:
    return (path / ".git").exists() and (path / "AGENTS.md").is_file()


def resolve_repo(raw: str | None) -> Path:
    if raw is None:
        die("укажите корень репозитория: --repo <путь>")
    repo = Path(raw).resolve()
    if not is_repo_root(repo):
        die(f"не корень репозитория: {repo}")
    return repo


def check_target(repo: Path, rel: str) -> Path:
    if rel not in TARGETS:
        die(f"{rel!r} не входит в белый список целей — снос запрещён")
    for prefix in PROTECTED_PREFIXES:
        if rel.startswith(prefix):
            die(f"{rel!r} попадает под защищённый префикс {prefix!r}")
    if rel.startswith("/") or ".." in Path(rel).parts or "\\" in rel:
        die(f"{rel!r} — не относительный путь внутри репозитория")
    path = repo / Path(rel)
    if repo not in path.parents:
        die(f"{rel!r} не лежит внутри репозитория")
    if not path.is_file():
        die(f"{rel!r} не обычный файл")
    if run_git(repo, "ls-files", "--error-unmatch", rel).strip() != rel:
        die(f"{rel!r} не отслеживается git — сносить нечего, проверь ветку")
    return path


def plan(repo: Path) -> int:
    print("ПЛАН СНОСА (ничего не изменено)\n")
    ok = True
    for rel in TARGETS:
        try:
            check_target(repo, rel)
        except SystemExit as exc:
            print(f"  ОТКАЗ  {rel}: {exc}")
            ok = False
            continue
        print(f"  готово  {rel}  ({(repo / rel).stat().st_size} байт)")
    if not ok:
        return 1
    print("\nПовторить с --apply, чтобы снести.")
    return 0


def apply(repo: Path) -> int:
    paths = [check_target(repo, rel) for rel in TARGETS]

    for rel in TARGETS:
        run_git(repo, "update-index", "--force-remove", rel)
        print(f"  снят из индекса: {rel}")
    for path in paths:
        path.unlink()
        print(f"  удалён с диска: {path.relative_to(repo).as_posix()}")

    empty = repo / EMPTY_DIR
    if empty.is_dir() and not any(empty.iterdir()):
        empty.rmdir()
        print(f"  удалён пустой каталог: {EMPTY_DIR}")

    print("\nПроверка:")
    for rel in TARGETS:
        on_disk = (repo / rel).exists()
        in_index = run_git(repo, "ls-files", rel).strip()
        print(f"  {rel}: диск={'есть' if on_disk else 'нет'} индекс={in_index or 'нет'}")
    print("\nПрогнать набор после сноса и закоммитить одним коммитом с D6.")
    return 0


def self_test() -> int:
    print("САМОПРОВЕРКА ЗАЩИТ\n")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "tests" / "fixtures" / "history_search").mkdir(parents=True)
        (repo / "lib").mkdir()
        (repo / ".git").mkdir()
        (repo / "AGENTS.md").write_text("x", encoding="utf-8")

        cases = [
            ("чужой тест вне списка", "tests/test_other.py", True),
            ("защищённый префикс", "lib/services/db_logging_service.py", True),
            ("файл вне репозитория", "../outside.py", True),
            ("не отслеживается", TARGETS[0], True),
        ]
        for name, rel, must_refuse in cases:
            try:
                check_target(repo, rel)
            except SystemExit:
                got = "отказ"
            else:
                got = "пропущен"
            verdict = "ok" if (got == "отказ") == must_refuse else "ПРОВАЛ"
            print(f"  {verdict}: {name} -> {got}")

        target = repo / TARGETS[0]
        target.write_text("x", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
            cwd=repo, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "t"],
            cwd=repo, capture_output=True, check=True,
        )
        try:
            check_target(repo, TARGETS[0])
        except SystemExit as exc:
            print(f"  ПРОВАЛ: цель из списка отвергнута: {exc}")
            return 1
        print("  ok: цель из списка принята")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Снос тестов снесённых обёрток")
    parser.add_argument("--repo", help="корень репозитория (обязателен)")
    parser.add_argument("--apply", action="store_true", help="снести, а не показать план")
    parser.add_argument("--self-test", action="store_true", help="проверить отказы")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    repo = resolve_repo(args.repo)
    return apply(repo) if args.apply else plan(repo)


if __name__ == "__main__":
    sys.exit(main())
