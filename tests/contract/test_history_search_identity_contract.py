"""Contract-тест: ``nanobot.agent.tools.context.RequestContext.sender_id``.

Это dependency contract: фиксирует, что ``RequestContext`` содержит поле
``sender_id`` с типом, совместимым с ``str | None``. Если в будущей
версии nanobot поле будет переименовано, адаптация делается в том же
change, который обновляет nanobot-зависимость и ``_current_user_id()``
в ``history_search_tool.py``.

Тест НЕ правит существующий ``tests/contract/test_tools_and_context.py``
— это наш dependency contract, не чужой contract subset.
"""
from __future__ import annotations

import dataclasses
import typing

import pytest


def _load_request_context():
    try:
        from nanobot.agent.tools.context import RequestContext
    except Exception as exc:
        pytest.skip(f"nanobot RequestContext недоступен: {exc}")
    return RequestContext


class TestRequestContextSenderId:
    def test_request_context_importable(self):
        """``RequestContext`` доступен как dataclass из nanobot."""
        RequestContext = _load_request_context()
        assert dataclasses.is_dataclass(RequestContext)

    def test_request_context_has_sender_id_field(self):
        """У ``RequestContext`` есть поле ``sender_id`` (identity-store role)."""
        RequestContext = _load_request_context()
        field_names = {f.name for f in dataclasses.fields(RequestContext)}
        assert "sender_id" in field_names, (
            "RequestContext.sender_id должен присутствовать как "
            "identity-store для history_search(session_scope='all')"
        )

    def test_sender_id_annotation_is_optional_str(self):
        """Аннотация ``sender_id`` совместима с ``str | None``.

        Допустимы: ``str``, ``Optional[str]``, ``str | None``,
        ``Union[str, None]``. Тест падает при отсутствии совместимости —
        это сигнал, что change не соответствует установленной версии nanobot.
        """
        RequestContext = _load_request_context()
        sender_field = next(
            f for f in dataclasses.fields(RequestContext) if f.name == "sender_id"
        )
        ann = sender_field.type

        def _is_optional_str(tp: Any) -> bool:  # noqa: ANN401
            if tp is str:
                return True
            origin = getattr(tp, "__origin__", None)
            if origin is typing.Union:
                args = getattr(tp, "__args__", ())
                return str in args and type(None) in args
            return False

        # Современный nanobot хранит аннотации как string ('str | None').
        # Пытаемся распарсить строку и проверить форму Optional[str].
        if isinstance(ann, str):
            normalized = ann.strip()
            # Принимаем: 'str | None', 'Optional[str]', 'Union[str, None]',
            # и одиночный 'str' (без None — это не optional, но совместимо
            # с семантикой "sender_id может быть строкой"; None не допустим,
            # поэтому явный optional предпочтительнее).
            if normalized in ("str", "str | None", "Optional[str]"):
                return  # accepted
            if normalized == "Union[str, None]":
                return  # accepted
            # Если что-то ещё — падаем с понятным сообщением.
            assert False, (
                f"RequestContext.sender_id должен быть совместим с str | None, "
                f"получено: {ann!r}"
            )

        assert _is_optional_str(ann), (
            f"RequestContext.sender_id должен быть совместим с str | None, "
            f"получено: {ann!r}"
        )
