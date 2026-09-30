"""Diagnose startup-inventory: парсит лог gateway/CLI и сверяет с каноном.

Зачем: после старта gateway/CLI оператор/агент должен иметь возможность
по сохранённому логу проверить, что **все** ожидаемые хуки/tools/патчи
зарегистрировались. Этот скрипт:

  1. Парсит startup-лог (по ``--log PATH`` или stdin).
  2. Извлекает секции:
     - ``Hooks connected: ...`` → имена хуков;
     - ``Custom (project) tools: ...`` → registered/disabled/failed;
     - ``Runtime patches:\\n...`` → applied/skipped/failed;
     - ``Registered N tools: [...]`` → built-in tools;
     - ``X runtime patch(es) failed: [...]`` → failed-patches (warning).
  3. Сверяет с каноническими списками из ``lib.services.runtime_inventory``.
  4. Печатает таблицу diff (expected vs actual) с цветами:
     - ``OK`` (зелёный) — расхождений нет;
     - ``DRIFT`` (жёлтый) — есть optional missing / unexpected;
     - ``CRITICAL`` (красный) — есть required missing / failed.
  5. Возвращает exit code:
     - ``0`` — всё ОК;
     - ``1`` — есть critical drift;
     - ``2`` — только warning (drift без critical, либо warning-секции
       в логе, которые не парсятся).

Использование::

    # Сохранить лог gateway и проверить его
    python gateway.py --profile=prod > gateway.log 2>&1
    python tools/diagnose_startup.py --log gateway.log

    # Или сразу через pipe (без файла)
    python gateway.py --profile=prod 2>&1 | python tools/diagnose_startup.py

    # Строгий режим: warning тоже считается за failure (exit 1)
    python tools/diagnose_startup.py --log gateway.log --strict

    # Только JSON для CI/тестов
    python tools/diagnose_startup.py --log gateway.log --json

См.::

    openspec/changes/startup-inventory-diagnose
    lib/services/runtime_inventory.py    — канонические списки
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOKS_LINE_RE = re.compile(r"Hooks connected:\s*(.+)")
TOOLS_LINE_RE = re.compile(r"Registered\s+(\d+)\s+tools:\s*\[(.+)\]")
CUSTOM_TOOLS_RE = re.compile(r"Custom \(project\) tools:\s*(.+?)(?:\s+—\s+.+)?$")
REGISTERED_RE = re.compile(r"(\d+)\s+project tools registered(?::\s*([^;]*))?")
DISABLED_RE = re.compile(r"(\d+)\s+disabled by config:\s*([^;]*)")
FAILED_RE = re.compile(r"(\d+)\s+failed:\s*([^;]*)")
PATCH_APPLIED_RE = re.compile(r"^[\u2713\u2714]\s+(\S+)")
PATCH_SKIPPED_RE = re.compile(r"^[\u26a0\u26a0\ufe0f]\s+(\S+)\s+skipped:\s*(.+)")
PATCH_FAILED_RE = re.compile(r"^[\u2717\u2715]\s+(\S+)\s+failed:\s*(.+)")
FAILED_HEADER_RE = re.compile(r"(\d+)\s+runtime patch\(es\)\s+failed:\s*\[(.+?)\]")


@dataclass
class StartupFacts:
    """Распарсенные факты из startup-лога."""

    hook_names: list[str] = field(default_factory=list)
    hook_factory_count: int = 0
    builtin_tool_names: list[str] = field(default_factory=list)
    project_tools_registered: list[str] = field(default_factory=list)
    project_tools_disabled: list[str] = field(default_factory=list)
    project_tools_failed: list[str] = field(default_factory=list)
    runtime_patches_applied: list[str] = field(default_factory=list)
    runtime_patches_skipped: list[tuple[str, str]] = field(default_factory=list)
    runtime_patches_failed: list[tuple[str, str]] = field(default_factory=list)
    runtime_patches_failed_header: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.hook_names
            or self.builtin_tool_names
            or self.project_tools_registered
            or self.runtime_patches_applied
        )


def parse_startup_log(text: str) -> StartupFacts:
    """Парсит текст startup-лога и возвращает структурированные факты."""
    facts = StartupFacts()

    lines = text.splitlines()
    in_patches_block = False

    for raw in lines:
        line = raw.rstrip()

        if "Runtime patches" in line and line.startswith("Runtime patches"):
            in_patches_block = True
            continue
        if in_patches_block and line.startswith("-"):
            continue
        if in_patches_block and not line.strip():
            in_patches_block = False
            continue

        if in_patches_block:
            m = PATCH_APPLIED_RE.match(line.strip())
            if m:
                facts.runtime_patches_applied.append(m.group(1))
                continue
            m = PATCH_SKIPPED_RE.match(line.strip())
            if m:
                facts.runtime_patches_skipped.append((m.group(1), m.group(2)))
                continue
            m = PATCH_FAILED_RE.match(line.strip())
            if m:
                facts.runtime_patches_failed.append((m.group(1), m.group(2)))
                continue

        m = HOOKS_LINE_RE.search(line)
        if m:
            label = m.group(1)
            for token in re.split(r",\s*", label):
                token = token.strip()
                if not token:
                    continue
                mm = re.match(r"(\d+)\s+hook factory", token)
                if mm:
                    facts.hook_factory_count = int(mm.group(1))
                else:
                    facts.hook_names.append(token)
            continue

        m = TOOLS_LINE_RE.search(line)
        if m:
            inner = m.group(2)
            for tok in re.findall(r"'([^']+)'", inner):
                facts.builtin_tool_names.append(tok)
            continue

        m = CUSTOM_TOOLS_RE.search(line)
        if m:
            detail = m.group(1)
            mm = REGISTERED_RE.search(detail)
            if mm and mm.group(2):
                facts.project_tools_registered = [
                    s.strip() for s in mm.group(2).split(",") if s.strip()
                ]
            mm = DISABLED_RE.search(detail)
            if mm and mm.group(2):
                facts.project_tools_disabled = [
                    s.strip() for s in mm.group(2).split(",") if s.strip()
                ]
            mm = FAILED_RE.search(detail)
            if mm and mm.group(2):
                facts.project_tools_failed = [
                    s.strip() for s in mm.group(2).split(",") if s.strip()
                ]
            continue

        m = FAILED_HEADER_RE.search(line)
        if m:
            inner = m.group(2)
            facts.runtime_patches_failed_header = [
                s.strip().strip("'\"") for s in inner.split(",") if s.strip()
            ]
            continue

    if facts.is_empty:
        facts.parse_errors.append(
            "Не нашли ни одной распознаваемой секции "
            "(Hooks connected / Registered N tools / Custom (project) tools / Runtime patches). "
            "Возможно, лог неполный или формат неожиданный."
        )

    return facts


def diff_against_canonical(facts: StartupFacts) -> dict[str, Any]:
    """Сравнить распарсенные факты с каноническими списками.

    Returns:
        dict с ключами ``hooks``, ``project_tools``, ``runtime_patches``.
    """
    from lib.services.runtime_inventory import (
        diff_hooks,
        diff_project_tools,
        diff_runtime_patches,
    )

    hooks_diff = diff_hooks(
        facts.hook_names,
        actual_factory_count=facts.hook_factory_count,
    )
    project_tools_diff = diff_project_tools(
        registered=facts.project_tools_registered,
        skipped_disabled=facts.project_tools_disabled,
        failed=facts.project_tools_failed,
    )
    runtime_patches_diff = diff_runtime_patches(
        applied=facts.runtime_patches_applied,
        skipped=facts.runtime_patches_skipped,
        failed=facts.runtime_patches_failed,
    )
    return {
        "hooks": hooks_diff,
        "project_tools": project_tools_diff,
        "runtime_patches": runtime_patches_diff,
    }


def _print_panel(console: Any, title: str, body: str, style: str) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel

        if isinstance(console, Console):
            console.print(
                Panel(
                    body,
                    title=title,
                    border_style=style,
                    title_align="left",
                )
            )
            return
    except Exception:
        pass
    sys.stderr.write(f"--- {title} ---\n{body}\n")


def render_report(
    facts: StartupFacts,
    diffs: dict[str, Any],
    *,
    use_color: bool = True,
) -> int:
    """Распечатать отчёт. Возвращает exit code (0/1/2)."""
    try:
        from rich.console import Console

        console = Console(stderr=True)
        if not use_color:
            console.no_color = True
    except Exception:
        console = None

    def _line(text: str) -> None:
        if console is not None:
            console.print(text)
        else:
            print(text)

    def _ok(msg: str) -> None:
        _line(f"[green]OK[/green] {msg}" if console else f"OK {msg}")

    def _warn(msg: str) -> None:
        _line(f"[yellow]DRIFT[/yellow] {msg}" if console else f"DRIFT {msg}")

    def _err(msg: str) -> None:
        _line(f"[red]CRITICAL[/red] {msg}" if console else f"CRITICAL {msg}")

    exit_code = 0
    for err in facts.parse_errors:
        _err(err)
        exit_code = max(exit_code, 2)

    _line("")
    _line(f"=== HOOKS ===")
    _line(f"  actual: {facts.hook_names or '(none)'}")
    _line(f"  factories: {facts.hook_factory_count}")
    h = diffs["hooks"]
    if h["missing_required"]:
        _err(f"  MISSING REQUIRED: {h['missing_required']}")
        exit_code = 1
    if h["missing_factory"]:
        _err(f"  MISSING FACTORY: {h['missing_factory']}")
        exit_code = 1
    if h["unexpected"]:
        _warn(f"  UNEXPECTED: {h['unexpected']}")
        exit_code = max(exit_code, 2)
    if h["missing_optional"]:
        _warn(f"  MISSING OPTIONAL: {h['missing_optional']}")
        exit_code = max(exit_code, 2)
    if not any(h.values()):
        _ok("  hooks inventory matches canonical")

    _line("")
    _line(f"=== BUILT-IN TOOLS ===")
    _line(f"  count: {len(facts.builtin_tool_names)}")
    if not facts.builtin_tool_names:
        _warn("  Built-in tools not detected (нет секции 'Registered N tools: [...]'). "
              "Это нормально, если лог обрезан или smoke-режим.")

    _line("")
    _line(f"=== PROJECT TOOLS ===")
    _line(f"  registered: {facts.project_tools_registered or '(none)'}")
    _line(f"  disabled:   {facts.project_tools_disabled or '(none)'}")
    _line(f"  failed:     {facts.project_tools_failed or '(none)'}")
    pt = diffs["project_tools"]
    if pt["missing_required"]:
        _err(f"  MISSING REQUIRED: {pt['missing_required']}")
        exit_code = 1
    if pt["failed"]:
        _err(f"  FAILED: {pt['failed']}")
        exit_code = 1
    if pt["unexpected"]:
        _warn(f"  UNEXPECTED: {pt['unexpected']}")
        exit_code = max(exit_code, 2)
    if not any(pt.values()):
        _ok("  project tools match canonical")

    _line("")
    _line(f"=== RUNTIME PATCHES ===")
    _line(f"  applied: {facts.runtime_patches_applied or '(none)'}")
    if facts.runtime_patches_skipped:
        _line(f"  skipped: {[n for n, _ in facts.runtime_patches_skipped]}")
    if facts.runtime_patches_failed:
        _line(f"  failed:  {[n for n, _ in facts.runtime_patches_failed]}")
    if facts.runtime_patches_failed_header:
        _line(f"  failed-header: {facts.runtime_patches_failed_header}")
    rp = diffs["runtime_patches"]
    if rp["missing_required"]:
        _err(f"  MISSING REQUIRED: {rp['missing_required']}")
        exit_code = 1
    if rp["failed_required"]:
        _err(f"  FAILED REQUIRED: {rp['failed_required']}")
        exit_code = 1
    if rp["unexpected_applied"]:
        _warn(f"  UNEXPECTED APPLIED: {rp['unexpected_applied']}")
        exit_code = max(exit_code, 2)
    if not any(rp.values()) and not facts.runtime_patches_failed:
        _ok("  runtime patches match canonical")
    elif facts.runtime_patches_failed and not rp["failed_required"]:
        _warn(f"  optional failed: {[n for n, _ in facts.runtime_patches_failed]}")
        exit_code = max(exit_code, 2)

    _line("")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose startup-inventory by parsing gateway/CLI log.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Путь к startup-логу. Если не указан — читает stdin.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Считать warning за failure (exit 1 вместо exit 2).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Отключить ANSI-цвета (для CI / log-файлов).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести JSON вместо human-readable отчёта.",
    )
    args = parser.parse_args(argv)

    if args.log is not None:
        text = args.log.read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        sys.stderr.write(
            "diagnose_startup: укажи --log PATH или передай лог через stdin.\n"
        )
        return 2

    facts = parse_startup_log(text)
    diffs = diff_against_canonical(facts)

    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "facts": {
                        "hook_names": facts.hook_names,
                        "hook_factory_count": facts.hook_factory_count,
                        "builtin_tool_names": facts.builtin_tool_names,
                        "project_tools_registered": facts.project_tools_registered,
                        "project_tools_disabled": facts.project_tools_disabled,
                        "project_tools_failed": facts.project_tools_failed,
                        "runtime_patches_applied": facts.runtime_patches_applied,
                        "runtime_patches_skipped": [
                            [n, r] for n, r in facts.runtime_patches_skipped
                        ],
                        "runtime_patches_failed": [
                            [n, r] for n, r in facts.runtime_patches_failed
                        ],
                        "runtime_patches_failed_header": facts.runtime_patches_failed_header,
                        "parse_errors": facts.parse_errors,
                    },
                    "diff": diffs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.stdout.write("\n")
        exit_code = 0
        h, pt, rp = diffs["hooks"], diffs["project_tools"], diffs["runtime_patches"]
        if (h["missing_required"] or h["missing_factory"]
                or pt["missing_required"] or pt["failed"]
                or pt.get("disabled_required")
                or rp["missing_required"] or rp["failed_required"]):
            exit_code = 1
        elif (h["unexpected"] or h["missing_optional"]
              or pt["unexpected"]
              or rp["unexpected_applied"]
              or facts.parse_errors
              or facts.runtime_patches_failed
              or facts.runtime_patches_failed_header):
            exit_code = 2
        return exit_code

    exit_code = render_report(facts, diffs, use_color=not args.no_color)
    if args.strict and exit_code == 2:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
