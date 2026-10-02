"""Схема в промпте обязана выводиться из снимка, а не быть зашитой.

Проверяется не «в коде нет литералов» (это плохая проверка: литерал легко
спрятать в промпт), а само свойство:

* текст схемы равен ``format_schema`` от того, что вернул снимок;
* в промпт попадает **пересечение** «что есть в снимке» и «что в белом
  списке» — ни больше, ни меньше;
* колонки в промпте — колонки снимка.

Последнее и есть содержательное доказательство: добавьте в снимок таблицу
и колонку — они появятся в промпте; уберите — исчезнут. Зашитый текст так
себя не ведёт.
"""

from __future__ import annotations

import pytest
from libs.audit.generated_sql import _read_filtered_schema, _schema_for_prompt
from libs.enterprise_data.sql_safety import format_schema


def _snapshot(tables: dict) -> dict:
    return {"schema": "oarb", "tables": tables}


_AUDITS = {
    "comment": "Проверки",
    "columns": {
        "id": {"type": "integer", "not_null": True, "comment": "ID"},
        "status": {"type": "character varying(30)", "not_null": False, "comment": ""},
    },
}
_LOGS = {
    "comment": "Журнал",
    "columns": {
        "id": {"type": "integer", "not_null": True, "comment": "ID"},
    },
}


class _Snap:
    def __init__(self, tables: dict) -> None:
        self._tables = tables

    def __call__(self) -> dict:
        return _snapshot(self._tables)


def _prompt(tables: dict, allowed: tuple[str, ...]) -> str:
    filtered = _read_filtered_schema(_Snap(tables), allowed, "oarb")
    return _schema_for_prompt(filtered)


class TestSchemaComesFromSnapshot:
    def test_text_equals_format_schema_of_snapshot(self) -> None:
        """Промпт — это отформатированное описание снимка, и ничего сверху."""
        tables = {"audits": _AUDITS}
        allowed = ("oarb.audits",)

        prompt = _prompt(tables, allowed)

        assert prompt == format_schema(_snapshot(tables))

    def test_table_in_snapshot_but_not_allowed_stays_out(self) -> None:
        """Снимок шире белого списка — и промпт остаётся узким.

        Именно это отличает генерацию по данным от выдуманной схемы: в
        снимке есть и журнал, и таблица векторов, но модели они не показываются.
        """
        tables = {"audits": _AUDITS, "agent_gateway_logs": _LOGS}
        allowed = ("oarb.audits",)

        prompt = _prompt(tables, allowed)

        assert "audits" in prompt
        assert "agent_gateway_logs" not in prompt

    def test_a_new_column_of_snapshot_reaches_the_prompt(self) -> None:
        """Колонка появляется в промпте, потому что появилась в снимке.

        Если бы схема была зашита, этот тест упал бы: литерал в коде не
        меняется сам собой при изменении данных.
        """
        before = _prompt({"audits": _AUDITS}, ("oarb.audits",))
        enriched = {
            "audits": {
                **_AUDITS,
                "columns": {
                    **_AUDITS["columns"],
                    "auditee_entity": {
                        "type": "character varying(120)",
                        "not_null": False,
                        "comment": "Проверяемый",
                    },
                },
            }
        }
        after = _prompt(enriched, ("oarb.audits",))

        assert "auditee_entity" not in before
        assert "auditee_entity" in after

    def test_a_renamed_table_of_snapshot_reaches_the_prompt(self) -> None:
        """Переименование таблицы в снимке меняет промпт — текст не зашит."""
        prompt = _prompt({"checks": _AUDITS}, ("oarb.checks",))

        assert "checks" in prompt
        assert "audits" not in prompt

    def test_column_comment_from_metadata_survives(self) -> None:
        """Комментарий колонки приходит из метаданных снимка."""
        tables = {
            "audits": {
                "comment": "Проверки",
                "columns": {
                    "status": {
                        "type": "character varying(30)",
                        "not_null": False,
                        "comment": "Текущий статус проверки",
                    }
                },
            }
        }

        assert "Текущий статус проверки" in _prompt(tables, ("oarb.audits",))

    def test_unknown_table_in_whitelist_is_reported(self) -> None:
        """Белый список шире снимка — это ошибка данных, а не пустой промпт."""
        from libs.audit.errors import QueryFailedError

        with pytest.raises(QueryFailedError, match="белого списка"):
            _read_filtered_schema(
                _Snap({"audits": _AUDITS}), ("oarb.no_such_table",), "oarb"
            )
