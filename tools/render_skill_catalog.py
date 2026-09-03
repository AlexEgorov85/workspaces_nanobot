"""Standalone-утилита: рендерит SKILL.md через SkillCatalog.

Проверяет, что rendered-версия SKILL.md соответствует реальному runtime-
каталогу (auto-populated env-vars SKILL_<NAME>_*). Используется:

    * В CI — ``--check`` валит сборку, если SKILL.md расходится с runtime;
    * В локальной отладке — ``--stdout`` печатает expanded-версию;
    * Для миграции — ``--out PATH`` пишет expanded-копию в файл.

Контракт:
    * ``--check`` возвращает exit code 0 если совпадает, иначе 1;
    * ``--stdout`` печатает rendered SKILL.md в stdout;
    * ``--out PATH`` пишет rendered SKILL.md в файл;
    * без флагов — эквивалентно ``--stdout``.

В отличие от runtime-расширения (RuntimePatcher.patch_skill_catalogs),
эта утилита **не** использует nanobot. Env-vars читаются напрямую из
``os.environ``. Для standalone-режима без gateway нужно вызвать
``lib.core.application_context._populate_skill_catalog_env`` явно
или поднять ApplicationContext.

Совместимость: Python ≥ 3.14.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# tools/render_skill_catalog.py — generic утилита.
# Гарантируем, что ``lib.*`` доступен при запуске из любого cwd.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _read_skill_md(skill_name: str) -> str:
    """Прочитать SKILL.md из workspace/skills/<name>/SKILL.md."""
    from config import SETTINGS  # noqa: F401  (загружает SETTINGS)

    candidates = [
        Path.cwd() / "workspace" / "skills" / skill_name / "SKILL.md",
        Path(__file__).resolve().parent.parent / "workspace" / "skills" / skill_name / "SKILL.md",
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"SKILL.md для {skill_name!r} не найден. Проверены: {candidates}"
    )


def _populate_env(skill_name: str) -> None:
    """Заполнить SKILL_<NAME>_* env-vars из текущего runtime-состояния.

    Поднимает минимальный ApplicationContext-цикл: auto-register skills +
    populate env. Не запускает sync/db services.
    """
    from lib.core.application_context import _populate_skill_catalog_env

    _populate_skill_catalog_env()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Рендерит SKILL.md через SkillCatalog (runtime-каталог)."
    )
    parser.add_argument(
        "skill_name",
        help="Имя skill'а (например, audit_analyzer)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1, если реальный SKILL.md не совпадает с rendered",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Печатать rendered SKILL.md в stdout",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="Записать rendered SKILL.md в файл",
    )
    parser.add_argument(
        "--skip-populate",
        action="store_true",
        help="Не вызывать _populate_skill_catalog_env (env-vars уже выставлены)",
    )

    args = parser.parse_args(argv)

    if not args.check and not args.stdout and args.out is None:
        args.stdout = True

    if not args.skip_populate:
        _populate_env(args.skill_name)

    from lib.utils.skill_catalog import SkillCatalog

    rendered = SkillCatalog.render_expanded_skill(
        args.skill_name,
        _read_skill_md(args.skill_name),
    )

    if args.stdout:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    if args.out is not None:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"Written: {args.out}", file=sys.stderr)
        return 0

    if args.check:
        try:
            real = _read_skill_md(args.skill_name)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if real == rendered:
            print(
                f"OK: SKILL.md для {args.skill_name!r} совпадает с runtime-каталогом.",
                file=sys.stderr,
            )
            return 0
        print(
            f"DRIFT: SKILL.md для {args.skill_name!r} расходится с runtime-каталогом.",
            file=sys.stderr,
        )
        print(
            "  Diff hint: запустите `python tools/render_skill_catalog.py "
            f"{args.skill_name} --stdout` чтобы увидеть актуальный вариант.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
