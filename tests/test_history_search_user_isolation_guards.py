"""Стражи изоляции ``history_search`` — на её нынешнем месте.

Раньше изоляцию обеспечивал агентский tool: он брал ``session_scope`` от
модели и строил выборку сам. Снос его (change ``2026-10-03-mcp-native-tools``,
п. D6) убрал самую опасную часть — **выбор области стал невозможен**: модель
не может попросить «все сессии», потому что параметра ``session_scope`` у
операции больше нет, а область задаётся личностью вызова.

Поэтому guard проверяет границу там, где она теперь:

  1. операция не объявляет параметра области видимости;
  2. обработчик НЕ принимает ``session_id``/``user_id`` аргументами и берёт
     их из ``ctx`` — иначе модель подставила бы чужие значения;
  3. у ``DbLoggingService`` нет публичного резолвера чужого identity (этот
     тест остался без изменений: он касается агента, а не удалённого tool'а).

Это primary-проверка, а не grep-страховка: grep по исходнику удалённого файла
охранял бы от регрессии кода, которого больше нет.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_HISTORY_SEARCH = (
    _REPO
    / "mcp-platform"
    / "servers"
    / "enterprise"
    / "capabilities"
    / "data"
    / "tools"
    / "history_search.py"
)

#: Имена, которыми модель когда-либо могла расширить область видимости.
#: Появление любого из них в сигнатуре операции означает утечку: вызов с
#: чужим значением прошёл бы в выборку.
SCOPE_ARGUMENT_NAMES = frozenset(
    {"session_scope", "scope", "all_sessions", "session_id", "user_id"}
)


def _handler() -> ast.FunctionDef:
    """Внутренний обработчик операции."""
    tree = ast.parse(_HISTORY_SEARCH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "handle_history_search":
            return node
    raise AssertionError("handle_history_search не найден в history_search.py")


def _argument_names(func: ast.FunctionDef) -> set[str]:
    names = {arg.arg for arg in func.args.args}
    names |= {arg.arg for arg in func.args.kwonlyargs}
    return names


def _keyword_value_sources(func: ast.FunctionDef) -> dict[str, str]:
    """Имя ключевого аргумента → откуда взято значение (исходный вид).

    ``service.history_search(session_id=ctx.session_id)`` даёт
    ``{"session_id": "ctx.session_id"}``; ``session_id=session_key`` дал бы
    ``"session_key"`` — и вот это уже значение из тела обработчика, то есть
    потенциально управляемое снаружи.
    """
    found: dict[str, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            found[keyword.arg] = ast.unparse(keyword.value)
    return found


class TestHistorySearchScopeIsNotModelControlled:
    def test_operation_declares_no_scope_argument(self):
        """У модели нет способа попросить область шире своей.

        Прежний tool принимал ``session_scope`` и сам строил выборку по нему —
        это и был источник утечки, а не «удобный режим». Параметра больше
        нет, и возвращаться к нему нельзя.
        """
        args = _argument_names(_handler())
        leaked = args & SCOPE_ARGUMENT_NAMES
        assert not leaked, (
            f"операция history_search принимает {sorted(leaked)} — область "
            "видимости обязана задаваться личностью вызова, а не аргументом"
        )

    def test_identity_comes_from_execution_context(self):
        """``session_id``/``user_id`` берутся из ``ctx``, а не из аргументов.

        Если бы обработник читал их из аргументов, модель подставила бы чужие
        значения, а конвейер (он вырезает ключи идентичности по
        ``LEGACY_IDENTITY_KEYS``) не защитил бы доменный код.
        """
        tree = ast.parse(_HISTORY_SEARCH.read_text(encoding="utf-8"))
        assert isinstance(tree, ast.Module)
        passed = _keyword_value_sources(_handler())
        for field in ("session_id", "user_id"):
            assert field in passed, (
                f"{field} не передаётся в сервис — изоляция вызова потеряна"
            )
            assert passed[field] == f"ctx.{field}", (
                f"{field} передаётся как {passed[field]!r}, а не из контекста "
                "выполнения: значение пришло бы от вызывающей стороны"
            )


class TestDbLoggingHasNoPublicIdentityResolver:
    """У ``DbLoggingService`` нет публичного ``get_request_user_id``.

    Без этого метода ни один компонент агента не может разрезолвить ``user_id``
    чужой сессии: индекс читается только внутри ``_enqueue`` по совпадению
    ``request_id``.
    """

    def test_no_public_identity_resolver(self):
        svc_src = (_REPO / "lib" / "services" / "db_logging_service.py").read_text(
            encoding="utf-8"
        )
        for pattern in (
            "def get_request_user_id",
            "def lookup_user_id",
            "def resolve_user_id",
        ):
            assert pattern not in svc_src, (
                f"DbLoggingService не должен иметь публичный метод: {pattern}"
            )
