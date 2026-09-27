"""Канонические списки runtime-инвентаря.

Single source of truth для ожидаемых хуков, project tools и runtime-патчей.
Используется:

  * в ``ApplicationContext._log_connected_hooks()`` и после ``apply_all()``
    для prominent-логирования расхождений (missing/unexpected/failed) в
    startup-логе gateway/CLI;
  * в ``tools/diagnose_startup.py`` для парсинга startup-лога и печати
    таблицы ``expected vs actual`` с exit code != 0 при расхождениях.

Зачем выделено в отдельный модуль:

  * один источник правды — все три потребителя (startup-логирование,
    diagnose-скрипт, тесты) видят одно и то же;
  * ``PatchSpec.required`` нет в ``RuntimePatcher`` (есть только ``risk``)
    — здесь ``required`` определяется явно по criticality;
  * динамическое сканирование ``workspace/hooks/*.py`` / ``workspace/tools/*.py``
    делается здесь же, чтобы runtime-инвентарь не разъезжался с реальным
    диском.

Изменения канонических списков:

  * добавить новый hook → ``canonical_plugin_hooks()``;
  * добавить новый project tool → ``canonical_project_tools()``;
  * добавить новый runtime patch → ``RuntimePatcher.patch_specs()`` (там
    же ``required`` через ``high_risk_required`` ниже, если патч ломает
    runtime при отказе).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HookSpec:
    """Каноническая спецификация одного хука."""

    name: str
    kind: Literal["framework", "plugin", "factory"]
    required: bool
    description: str
    source: str


@dataclass(frozen=True)
class ToolSpec:
    """Каноническая спецификация одного project tool'а."""

    name: str
    module: str
    required: bool
    description: str
    config_key: str | None = None


@dataclass(frozen=True)
class RuntimePatchSpec:
    """Каноническая спецификация одного runtime-патча."""

    name: str
    required: bool
    risk: str
    purpose: str


def canonical_framework_hooks() -> list[HookSpec]:
    """Фреймворковые хуки из ``lib/hooks/*`` (всегда в ``ctx.hooks``)."""
    return [
        HookSpec(
            name="ToolAuditHook",
            kind="framework",
            required=True,
            description="аудит вызовов tool'ов (tool_call_id, duration_ms, status, error_type)",
            source="lib/hooks/tool_audit_hook.py",
        ),
        HookSpec(
            name="TerminalToolPrintHook",
            kind="framework",
            required=True,
            description="живой вывод результатов tool'ов в терминал",
            source="lib/hooks/terminal_tool_print_hook.py",
        ),
    ]


def canonical_plugin_hooks() -> list[HookSpec]:
    """Plugin-хуки из ``workspace/hooks/*`` (auto-scan через allowlist)."""
    return [
        HookSpec(
            name="SessionFileRedirectHook",
            kind="plugin",
            required=True,
            description="перенаправление файлов сессии в workspace/data_store/cache/sessions/<key>/",
            source="workspace/hooks/session_file_redirect_hook.py",
        ),
        HookSpec(
            name="RecentFilesHook",
            kind="plugin",
            required=True,
            description="auto-attach созданных файлов к OutboundMessage.media",
            source="workspace/hooks/recent_files_hook.py",
        ),
        HookSpec(
            name="StreamDiagnosisHook",
            kind="plugin",
            required=False,
            description="диагностика стриминга (debug_stream_diag.py; помечен REMOVED в post-0.3.5-hook-migration)",
            source="workspace/hooks/debug_stream_diag.py",
        ),
    ]


def canonical_hook_factories() -> list[HookSpec]:
    """Per-turn hook factories (создаются по одному инстансу на request)."""
    return [
        HookSpec(
            name="DatabaseLoggingHook",
            kind="factory",
            required=True,
            description="БД-логирование tool/llm/run-событий (per-turn, создаётся в make_db_logging_hook_factory)",
            source="lib/hooks/database_logging_hook.py",
        ),
    ]


def canonical_project_tools() -> list[ToolSpec]:
    """Project tools из ``workspace/tools/*.py`` (auto-discover)."""
    return [
        ToolSpec(
            name="compact_context",
            module="compact_context",
            required=True,
            description="ручное сжатие контекста (читает gateway.compact.*)",
            config_key="tools.compact_context.enable",
        ),
        ToolSpec(
            name="history_search",
            module="history_search_tool",
            required=True,
            description="generic-поиск по долговечному журналу agent_gateway_logs",
            config_key="tools.history_search.enable",
        ),
        ToolSpec(
            name="legal_summarizer_query",
            module="legal_summarizer_query",
            required=True,
            description="вопрос-ответ по пакетам документов legal_summarizer",
            config_key=None,
        ),
        ToolSpec(
            name="ExampleTool",
            module="example",
            required=False,
            description="шаблон с правильным паттерном (disabled by config — служебный)",
            config_key=None,
        ),
    ]


def canonical_runtime_patches() -> list[RuntimePatchSpec]:
    """Все runtime-патчи из ``RuntimePatcher.patch_specs()``.

    ``required`` = если патч fail'ит, runtime ломается (subagent не
    пишется в БД, tool-результаты не обрезаются, и т.п.). Список
    критичных имён захардкожен (нет поля ``required`` в ``PatchSpec``):
    критичность определяется по реальному fail-impact, а не по ``risk``.
    """
    high_risk_required = frozenset({
        "assemble_outbound",
        "subagent_logging",
        "save_turn",
        "context_governor",
    })
    from lib.services.runtime_patcher import RuntimePatcher

    return [
        RuntimePatchSpec(
            name=name,
            required=name in high_risk_required,
            risk=spec.risk,
            purpose=spec.purpose,
        )
        for name, spec in RuntimePatcher.patch_specs().items()
    ]


def collect_actual_hook_names(ctx: object) -> tuple[list[str], int]:
    """Вернуть ``(имена классов в ctx.hooks, кол-во factories)``.

    Безопасно работает с любым ctx-like объектом (для тестов с моками).
    """
    hooks = getattr(ctx, "hooks", None) or []
    names = [type(h).__name__ for h in hooks]
    factory_count = len(getattr(ctx, "hook_factories", []) or [])
    return names, factory_count


def diff_hooks(
    actual_hook_names: list[str],
    *,
    actual_factory_count: int,
) -> dict[str, list[str]]:
    """Сравнить фактические хуки с каноническими списками.

    Args:
        actual_hook_names: имена классов хуков в ``ctx.hooks``.
        actual_factory_count: количество per-turn hook factories.

    Returns:
        dict с ключами ``missing_required`` / ``missing_optional`` /
        ``unexpected`` / ``missing_factory``.
    """
    framework = {h.name: h for h in canonical_framework_hooks()}
    plugins = {h.name: h for h in canonical_plugin_hooks()}
    factories = canonical_hook_factories()

    actual_set = set(actual_hook_names)
    expected_set = {h.name for h in framework.values()} | {h.name for h in plugins.values()}

    missing_required: list[str] = []
    missing_optional: list[str] = []
    for spec in list(framework.values()) + list(plugins.values()):
        if spec.name not in actual_set:
            (missing_required if spec.required else missing_optional).append(spec.name)

    unexpected = sorted(name for name in actual_set if name not in expected_set)

    missing_factory: list[str] = []
    if actual_factory_count < len(factories):
        missing_factory = [f.name for f in factories[actual_factory_count:]]

    return {
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "unexpected": unexpected,
        "missing_factory": missing_factory,
    }


def diff_project_tools(
    registered: list[str],
    skipped_disabled: list[str],
    *,
    failed: list[str] | None = None,
) -> dict[str, list[str]]:
    """Сравнить фактический список project tools с каноническим.

    Args:
        registered: tool'ы, успешно зарегистрированные (``detail`` из
            ``patch_project_tools`` — секция до ``; ``).
        skipped_disabled: tool'ы, отключённые конфигом (``detail`` —
            секция ``disabled by config``). Required tool, попавший сюда,
            попадает в ``disabled_required`` (конфиг выключил обязательный
            инструмент — отдельная категория, чтобы подсветить).
        failed: tool'ы, упавшие при ``Tool.create()`` (если есть).

    Returns:
        dict с ключами ``missing_required`` / ``disabled_required`` /
        ``unexpected`` / ``failed``.
    """
    expected = {t.name: t for t in canonical_project_tools()}
    actual = set(registered)
    disabled = set(skipped_disabled)

    missing_required: list[str] = []
    disabled_required: list[str] = []
    for name, spec in expected.items():
        if spec.required and name not in actual:
            if name in disabled:
                disabled_required.append(name)
            else:
                missing_required.append(name)

    expected_names = set(expected.keys())
    unexpected = sorted(name for name in actual if name not in expected_names)

    return {
        "missing_required": missing_required,
        "disabled_required": disabled_required,
        "unexpected": unexpected,
        "failed": list(failed or []),
    }


def diff_runtime_patches(
    applied: list[str],
    skipped: list[tuple[str, str]],
    failed: list[tuple[str, str]],
) -> dict[str, list[str]]:
    """Сравнить фактический ``PatchReport`` с каноническим списком патчей.

    Args:
        applied: имена патчей из ``patch_report.applied``.
        skipped: список ``(name, reason)`` из ``patch_report.skipped``.
        failed: список ``(name, reason)`` из ``patch_report.failed``.

    Returns:
        dict с ключами ``missing_required`` (applied + skipped без required
        патча) / ``failed_required`` (required-патч попал в failed) /
        ``unexpected_applied``.
    """
    expected = {p.name: p for p in canonical_runtime_patches()}
    applied_set = set(applied)
    skipped_set = {n for n, _ in skipped}
    failed_set = {n for n, _ in failed}
    seen = applied_set | skipped_set | failed_set

    missing_required: list[str] = []
    for name, spec in expected.items():
        if spec.required and name not in seen:
            missing_required.append(name)

    failed_required = sorted(
        n for n, _ in failed if expected.get(n, RuntimePatchSpec(n, False, "", "")).required
    )

    expected_names = set(expected.keys())
    unexpected_applied = sorted(n for n in applied_set if n not in expected_names)

    return {
        "missing_required": missing_required,
        "failed_required": failed_required,
        "unexpected_applied": unexpected_applied,
    }


def parse_project_tools_detail(detail: str) -> dict[str, list[str]]:
    """Распарсить ``detail`` патча ``project_tools`` в structured-формат.

    Формат ``detail`` (см. ``RuntimePatcher.patch_project_tools``):
        ``[INTERNAL_FAILED] N project tools registered: a, b; M disabled by config: c;
        K already registered: d; J failed: e``

    Returns:
        ``{"registered": [...], "disabled": [...], "duplicate": [...], "failed": [...]}``.
    """
    import re

    out: dict[str, list[str]] = {
        "registered": [],
        "disabled": [],
        "duplicate": [],
        "failed": [],
    }
    if not detail:
        return out
    m = re.search(r"registered(?::\s*([^;]*))?", detail)
    if m and m.group(1):
        out["registered"] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    m = re.search(r"disabled by config:\s*([^;]*)", detail)
    if m and m.group(1):
        out["disabled"] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    m = re.search(r"already registered:\s*([^;]*)", detail)
    if m and m.group(1):
        out["duplicate"] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    m = re.search(r"failed:\s*([^;]*)", detail)
    if m and m.group(1):
        out["failed"] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    return out


def diff_project_tools_from_detail(detail: str) -> dict[str, list[str]]:
    """Сравнить ``project_tools`` ``detail`` с каноническим списком."""
    parsed = parse_project_tools_detail(detail)
    return diff_project_tools(
        registered=parsed["registered"],
        skipped_disabled=parsed["disabled"],
        failed=parsed["failed"],
    )
