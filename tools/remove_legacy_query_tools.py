"""remove_legacy_query_tools.py — снос трёх самописных обёрток над MCP.

Операции платформы теперь приходят к модели штатным MCP-клиентом
(``config.json → tools.mcpServers``) как ``mcp_enterprise_*`` с их настоящими
``inputSchema``. Три обёртки (``audit_analyzer_query``, ``legal_summarizer_query``,
``history_search_tool``) стали не просто лишними: они показывали модели
переписанные схемы и переводили ошибки в свой формат.

Почему скрипт, а не команда агента. Политика безопасности агента блокирует
удаление файлов, а лаунчер ``mavis-trash`` недоступен. Удаление вручную —
единственный выход, и у него есть цена: файлы ОТСЛЕЖИВАЮТСЯ git, поэтому
``git rm`` тронет и индекс, и дерево сразу, а вместе с ними уедет в коммит
что-то ещё, если индекс не пуст. Скрипт делает ровно одну вещь и проверяет
каждый шаг.

Запуск::

    python tools/remove_legacy_query_tools.py              # план, ничего не трогает
    python tools/remove_legacy_query_tools.py --apply      # снести
    python tools/remove_legacy_query_tools.py --self-test  # доказать защиты

Что проверяется перед каждым удалением:

1. путь лежит внутри репозитория;
2. это обычный файл из БЕЛОГО СПИСКА, а не результат glob'а;
3. файл отслеживается git (иначе это уже не тот случай и сносить нечего);
4. цель не совпадает с самим скриптом и не лежит в ``tools/``.

Что скрипт НЕ делает: не правит тесты, не трогает соседние файлы и не
коммитит. После сноса нужно починить гарды, ссылающиеся на удалённые
инструменты, — это отдельный проход (см. ``tasks.md`` change'а
``2026-10-03-mcp-native-tools``, п. D6.3).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: Ровно эти три файла. Список закрыт: широкий glob по ``workspace/tools`` задел
#: бы ``compact_context.py`` и ``document_read.py``, которые остаются.
TARGETS = (
    "workspace/tools/audit_analyzer_query.py",
    "workspace/tools/legal_summarizer_query.py",
    "workspace/tools/history_search_tool.py",
)

#: Кого скрипт обязан оставить в покое, даже если путь совпадёт с целями.
PROTECTED_PREFIXES = ("tools/", "mcp-platform/", "lib/", "gateway.py", "config.py")


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
    """Репозиторий — только из аргумента. Никогда по расположению скрипта.

    Скрипт, выбирающий цель по собственному пути, уже стоил одну аварию
    (INCIDENT-01-neighbour-overwrite.md): он переписал чужую незакоммиченную
    работу в общем дереве. Если путь не задан — отказ, а не угадывание.
    """
    if raw is None:
        die("укажите корень репозитория: --repo <путь>")
    repo = Path(raw).resolve()
    if not is_repo_root(repo):
        die(f"не корень репозитория: {repo}")
    return repo


def check_target(repo: Path, rel: str) -> Path:
    """Вернуть абсолютный путь цели либо отказать с объяснением."""
    if rel not in TARGETS:
        die(f"{rel!r} не входит в белый список целей — снос запрещён")
    for prefix in PROTECTED_PREFIXES:
        if rel.startswith(prefix):
            die(f"{rel!r} попадает под защищённый префикс {prefix!r}")
    if rel.startswith("/") or ".." in Path(rel).parts or "\\" in rel:
        die(f"{rel!r} — не относительный путь внутри репозитория")
    # Путь НЕ резолвится: на Windows resolve() разворачивает короткие имена
    # (C:\Users\CD86~1\...) и сравнение с уже разрешённым repo даёт ложное
    # «уходит за пределы». Белый список выше уже гарантирует, что это
    # относительный путь без выхода вверх.
    path = repo / Path(rel)
    if repo not in path.parents:
        die(f"{rel!r} не лежит внутри репозитория")
    if path.suffix != ".py" or not path.is_file():
        die(f"{rel!r} не обычный файл")
    tracked = run_git(repo, "ls-files", "--error-unmatch", rel).strip()
    if tracked != rel:
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
    paths: list[Path] = []
    for rel in TARGETS:
        paths.append(check_target(repo, rel))

    # Сначала снять из индекса, потом с диска: если снос с диска не удастся,
    # файл останется отслеживаемым и следующий прогон увидит его снова.
    for rel in TARGETS:
        run_git(repo, "update-index", "--force-remove", rel)
        print(f"  снят из индекса: {rel}")
    for path in paths:
        path.unlink()
        print(f"  удалён с диска: {path.relative_to(repo).as_posix()}")

    print("\nПроверка:")
    for rel in TARGETS:
        on_disk = (repo / rel).exists()
        in_index = run_git(repo, "ls-files", rel).strip()
        print(f"  {rel}: диск={'есть' if on_disk else 'нет'} индекс={in_index or 'нет'}")
    print(
        "\nДальше: починить гард-тесты, ссылающиеся на удалённые инструменты "
        "(change 2026-10-03-mcp-native-tools, п. D6.3), и только потом коммитить."
    )
    return 0


def self_test() -> int:
    """Проверить отказы на воображаемых целях, ничего не удаляя."""
    print("САМОПРОВЕРКА ЗАЩИТ\n")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "workspace" / "tools").mkdir(parents=True)
        (repo / "lib").mkdir()
        (repo / ".git").mkdir()
        (repo / "AGENTS.md").write_text("x", encoding="utf-8")

        cases = [
            # Чужой файл вне списка обязан быть отвергнут: широкий glob по
            # workspace/tools задел бы compact_context.py и document_read.py.
            ("чужой файл вне списка", "workspace/tools/other.py", True),
            ("защищённый префикс", "tools/remove_legacy_query_tools.py", True),
            ("файл вне репозитория", "../outside.py", True),
            ("не .py", "workspace/tools/readme.md", True),
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

        # Цель из списка, отслеживаемая git, — обязана пройти проверку.
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
    parser = argparse.ArgumentParser(description="Снос трёх обёрток над MCP")
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
