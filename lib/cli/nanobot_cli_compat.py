"""Адаптер приватных CLI-хелперов ``nanobot``.

Upstream не публикует REPL-хелперы как стабильный API — они живут в
приватных модулях и мигрируют между версиями:

* ``nanobot <= 0.3.0`` — ``nanobot.cli.commands``
  (``_init_prompt_session``, ``_is_exit_command``,
  ``_read_interactive_input_async``, ``_restore_terminal``);
* ``nanobot >= 0.3.5`` — ``nanobot.cli.terminal`` (upstream сам
  импортирует его как ``cli_terminal`` в ``nanobot/cli/agent.py``).

Модуль скрывает это различие за одним публичным контрактом, чтобы
``lib/cli/console_loop.py`` не содержал version-specific импортов.
Для каждого набора имён выбирается кандидат с наибольшим числом
совпадений; если какое-то имя недоступно, бросается ``AttributeError``
с поимённым списком отсутствующих символов вместо ``ImportError``
на длинном кортеже (диагностика указывает, что именно сломалось).
"""

from __future__ import annotations

import importlib
from typing import Any

#: Кандидаты модулей в порядке приоритета (сначала текущая схема 0.3.5+).
_TERMINAL_MODULES: tuple[str, ...] = ("nanobot.cli.terminal", "nanobot.cli.commands")
#: ``_model_display``/``_sanitize_surrogates``: сначала их родной модуль,
#: затем реэкспорт из ``nanobot.cli.commands`` (nanobot 0.3.5).
_AUX_MODULES: tuple[str, ...] = (
    "nanobot.cli.runtime_config",
    "nanobot.cli.commands",
    "nanobot.cli.terminal",
)
_AUX_HELPERS: tuple[str, ...] = ("_model_display", "_sanitize_surrogates")

#: Имена приватных REPL-хелперов, которые должен скрывать адаптер.
# Расширено для upstream-структуры run_interactive:
#   * ``_print_agent_response`` / ``_print_interactive_response`` —
#     рендер финального ответа с markdown.
#   * ``_maybe_print_interactive_progress`` — обработка ProgressEvent
#     по флагам (reasoning_delta / reasoning_end / tool_hint и т.д.)
#     + ContextCompactionEvent + RetryWaitEvent.
#   * ``_ReasoningBuffer`` — буферизация reasoning с sentence-boundary
#     flush.
#   * ``_flush_pending_tty_input`` — flush pending stdin input
#     (вызывается перед user prompt).
_TERMINAL_HELPERS: tuple[str, ...] = (
    "_init_prompt_session",
    "_is_exit_command",
    "_read_interactive_input_async",
    "_restore_terminal",
    "_flush_pending_tty_input",
    "_print_agent_response",
    "_print_interactive_response",
    "_maybe_print_interactive_progress",
    "_ReasoningBuffer",
)

_resolved: dict[str, Any] | None = None


def _resolve() -> dict[str, Any]:
    """Найти все хелперы, кэшируя результат.

    Raises:
        AttributeError: если хотя бы один хелпер недоступен ни в одном
            из кандидатов; в сообщении перечислены все отсутствующие имена.
    """
    global _resolved
    if _resolved is not None:
        return _resolved

    # Терминальные хелперы могут лежать в разных модулях, а вспомогательные —
    # в другом наборе; резолвим каждый набор независимо, общим поиском.
    found: dict[str, Any] = {}
    missing: list[str] = []
    for names, candidates in (
        (_TERMINAL_HELPERS, _TERMINAL_MODULES),
        (_AUX_HELPERS, _AUX_MODULES),
    ):
        module = _best_module(candidates, names)
        for name in names:
            if not hasattr(module, name):
                missing.append(f"{module.__name__}.{name}")
            else:
                found[name] = getattr(module, name)

    if missing:
        raise AttributeError(
            "nanobot CLI REPL helpers are unavailable: "
            + ", ".join(missing)
            + f" (searched: terminal={', '.join(_TERMINAL_MODULES)}"
            f"; aux={', '.join(_AUX_MODULES)})"
        )

    _resolved = found
    return _resolved


def _best_module(candidates: tuple[str, ...], names: tuple[str, ...]) -> Any:
    """Кандидат с наибольшим числом доступных ``names`` (порядок решает ничью).

    Модуль выбирается даже неполным, чтобы ``_resolve`` мог сообщить о
    каждом отсутствующем имени поимённо, а не падать на первом кандидате.

    Raises:
        ImportError: если ни один кандидат не импортируется.
    """
    best: Any = None
    best_score = -1
    tried: list[str] = []
    for dotted in candidates:
        try:
            module = importlib.import_module(dotted)
        except ImportError as exc:
            tried.append(f"{dotted} ({exc})")
            continue
        score = sum(1 for name in names if hasattr(module, name))
        tried.append(f"{dotted} (missing: {', '.join(n for n in names if not hasattr(module, n))})")
        if score > best_score:
            best, best_score = module, score
    if best is None:
        raise ImportError("no nanobot CLI module is importable: " + "; ".join(tried))
    return best


def get_repl_helpers() -> dict[str, Any]:
    """Вернуть словарь приватных REPL-хелперов upstream.

    Состав: ``_init_prompt_session``, ``_is_exit_command``,
    ``_read_interactive_input_async``, ``_restore_terminal``,
    ``_model_display``, ``_sanitize_surrogates``.
    """
    return dict(_resolve())


def get_logo_version() -> tuple[str, str]:
    """Вернуть ``(__logo__, __version__)`` из публичного ``nanobot``."""
    import nanobot

    return str(nanobot.__logo__), str(nanobot.__version__)


def model_display(config: Any) -> tuple[str, str]:
    """Адаптер над приватным ``_model_display`` (defensive копия результата)."""
    model, preset_tag = _resolve()["_model_display"](config)
    return str(model), str(preset_tag)
