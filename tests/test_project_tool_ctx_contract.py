from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_TOOLS = _ROOT / "workspace" / "tools"
_LOADER = _ROOT / "lib" / "services" / "project_tool_loader.py"

# Приватные атрибуты, которые `project_tool_loader` реально проставляет
# на объект контекста. Имена с подчёркиванием — в этом главная ловушка:
# `getattr` с опечаткой не бросает исключение, а молча возвращает `None`.
#
# Набор НЕ выдуман: он проверяется тестом `test_loader_sets_exactly_these`
# на реальном коде загрузчика, поэтому разъехаться с ним не может.
_LOADER_ATTRIBUTES = {
    "_agent_ref",
    "_settings_ref",
    "_db_logging_service",
    "_enterprise_mcp",
}

# Публичные поля, которые приходят не от загрузчика, а из
# `nanobot.agent.tools.context.RequestContext` (набор сверен с библиотекой).
_CONTEXT_FIELDS = {
    "channel",
    "chat_id",
    "message_id",
    "session_key",
    "original_user_text",
    "runtime",
    "metadata",
    "sender_id",
    "turn_id",
    "workspace",
    "attributes",
}

_READ_RE = re.compile(r"""getattr\(\s*ctx\s*,\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
_SET_RE = re.compile(r"""ctx\.([A-Za-z_][A-Za-z0-9_]*)\s*=""")


def _tool_files() -> list[Path]:
    assert _WORKSPACE_TOOLS.is_dir(), f"нет {_WORKSPACE_TOOLS}"
    return sorted(
        p for p in _WORKSPACE_TOOLS.glob("*.py") if not p.name.startswith("__")
    )


def test_loader_sets_exactly_these():
    """Сторона контракта под контролем: набор атрибутов не разъедется.

    Если `project_tool_loader` перестанет проставлять атрибут, а tool'ы
    будут на него ссылаться, проверки ниже не заметят — нужен этот якорь.
    """
    ast.parse(_LOADER.read_bytes().decode("utf-8"))
    written = set(_SET_RE.findall(_LOADER.read_bytes().decode("utf-8")))
    written = {a for a in written if a.startswith("_")} - {"_" }
    assert written == _LOADER_ATTRIBUTES, (
        f"project_tool_loader проставляет {sorted(written)}, "
        f"гард ожидает {sorted(_LOADER_ATTRIBUTES)} — приведи в соответствие"
    )


@pytest.mark.parametrize("path", _tool_files(), ids=lambda p: p.name)
def test_tool_reads_only_known_ctx_attributes(path: Path):
    """Tool читает с `ctx` только атрибуты, которые кто-то действительно кладёт.

    Регресс БАГ-3: в `audit_analyzer_query` и `legal_summarizer_query` было
    `getattr(ctx, "db_logging_service")` без подчёркивания, тогда как
    `project_tool_loader` пишет `ctx._db_logging_service`. Результат — `None`
    в проде: `request_id_source` терял связь прогона с `agent_question_runs`.
    """
    text = path.read_bytes().decode("utf-8")
    ast.parse(text)

    for attr in sorted(set(_READ_RE.findall(text))):
        known = attr in _LOADER_ATTRIBUTES or attr in _CONTEXT_FIELDS
        assert known, (
            f"{path.name}: читает ctx.{attr}, которого нет ни в контракте "
            f"project_tool_loader, ни в полях RequestContext — getattr вернёт "
            f"None молча"
        )


@pytest.mark.parametrize("path", _tool_files(), ids=lambda p: p.name)
def test_tool_does_not_write_ctx_attributes(path: Path):
    """Tool'ы не должны писать в переданный контекст: он принадлежит загрузчику."""
    text = path.read_bytes().decode("utf-8")
    ast.parse(text)
    offenders = sorted(set(_SET_RE.findall(text)))
    assert not offenders, f"{path.name}: пишет в ctx: {offenders}"
