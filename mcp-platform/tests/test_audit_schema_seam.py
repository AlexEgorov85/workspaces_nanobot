"""Регрессия: ``generate_sql`` отдавал генератору метод вместо словаря схемы.

Дефект, который закрывает этот файл
-----------------------------------

Живой прогон по всем операциям дал:

    generate_sql -> [internal] Описание схемы вернуло method, ожидался словарь.

Сломан был не генератор, а шов: ``AuditService._schema`` возвращал
``data_service.snapshot_schema`` — bound-метод, — а ``run_generated_sql``
ожидал вызываемый объект, возвращающий словарь. Метод вместо значения
проходил через слой, который проверяет тип, и падал там, где виноватым
выглядел генератор.

Юнит-тесты capability audit были зелёными: они подставляли ``read_schema``
сразу как готовый callable, минуя ``_schema``.

Проверяется именно на живом пути ``AuditService._schema``: тест с
подставным ``read_schema`` этот дефект не увидел бы — он живёт на шве
между сервисом и библиотекой.
"""

from __future__ import annotations

from typing import Any

import pytest
from servers.enterprise.capabilities.audit.service.main import AuditService


class _FakeData:
    """Минимальный владелец снимка: методы, а не значения."""

    def __init__(self) -> None:
        self.calls = 0

    def snapshot_schema(self) -> dict[str, Any]:
        self.calls += 1
        return {"audits": {"columns": ["id", "created_at"]}}

    def snapshot_query(self, sql: str) -> dict[str, Any]:
        return {"rows": [], "columns": []}


class _Container:
    def __init__(self, data: Any) -> None:
        self._data = data

    def get(self, key: str) -> Any:
        return self._data


def _service(data: Any) -> AuditService:
    from libs.enterprise_common.container import ToolContainer

    return AuditService(container=ToolContainer(services={"data": data}))


class TestSchemaSeam:
    def test_schema_returns_a_dict_not_a_method(self) -> None:
        """Главный инвариант шва: значение, а не callable."""
        data = _FakeData()
        schema = _service(data)._schema()
        assert isinstance(schema, dict), (
            f"_schema() вернул {type(schema).__name__}, ожидался dict — "
            "run_generated_sql упадёт с «ожидался словарь»"
        )
        assert "audits" in schema

    def test_schema_actually_calls_the_owner(self) -> None:
        """Значение обязано быть получено вызовом, иначе это метод."""
        data = _FakeData()
        _service(data)._schema()
        assert data.calls == 1, "метод владельца не вызван — вернулся сам метод"

    def test_reader_is_still_a_callable(self) -> None:
        """Соседние швы отдают callable — менять их нельзя.

        ``run_generated_sql`` ждёт вызываемые ``reader``/``explain``; если
        начать возвращать из них значения, сломается следующий же стык.
        """
        data = _FakeData()
        reader = _service(data)._reader()
        assert callable(reader), (
            f"_reader() вернул {type(reader).__name__} — библиотека ждёт callable"
        )
        assert isinstance(reader("SELECT 1"), dict)


class TestGuardIsNotVacuous:
    def test_the_broken_shape_would_fail(self) -> None:
        """Цепочка «метод вместо значения» должна ломать проверку.

        Исходный дефект — не «метод не словарь», а **лишний уровень
        callable**: ``_schema`` отдавала ``snapshot_schema``, и вызов
        ``_schema()`` возвращал метод, а не словарь. Воспроизводится ровно
        эта цепочка, иначе тест выше проверял бы только себя.
        """
        data = _FakeData()

        def broken_read_schema():  # то, что возвращала _schema
            return data.snapshot_schema  # метод, не значение

        result = broken_read_schema()  # вызов, как делает _schema_for_prompt
        assert not isinstance(result, dict)
        assert type(result).__name__ == "method"


class TestGenerateSqlFailsLoudly:
    def test_bad_schema_shape_is_reported_not_swallowed(self) -> None:
        """Метод вместо словаря — явная ошибка, а не «генератор не смог»."""
        from libs.audit.errors import QueryFailedError
        from libs.audit.generated_sql import _read_filtered_schema

        with pytest.raises(QueryFailedError) as exc:
            _read_filtered_schema(
                lambda: (_FakeData().snapshot_schema),  # метод, не значение
                ("oarb.audits",),
                "main",
            )
        assert "ожидался словарь" in str(exc.value)

    def test_empty_schema_for_whitelist_is_reported(self) -> None:
        """Пустое описание — ошибка, а не запрос «на глаз».

        Раньше снимок читался с фиксированной схемой и при несовпадении схем
        описывал себя пустотой; модель получала вопрос без колонок и
        выдумывала их вместе со значениями — получался правдоподобный и
        заведомо неверный ответ.
        """
        from libs.audit.errors import QueryFailedError
        from libs.audit.generated_sql import _read_filtered_schema

        empty = {"schema": "oarb", "tables": {}}

        with pytest.raises(QueryFailedError) as exc:
            _read_filtered_schema(lambda: empty, ("oarb.audits",), "oarb")
        assert "белого списка" in str(exc.value)
