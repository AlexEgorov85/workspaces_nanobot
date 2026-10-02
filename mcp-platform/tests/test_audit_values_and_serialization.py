"""Два дефекта, найденных живым прогоном, закреплены тестами.

1. **Сериализация ответа операции.** Сервис умеет ``default=str``, а
   tool-слой вызывал ``json.dumps`` без него. Любой вопрос, ответ которого
   содержит дату, падал с ``Object of type date is not JSON serializable`` —
   то есть на ровно том запросе, который модель пишет чаще всего.
2. **Каталог значений.** Модель писала ``status = 'in_progress'`` при данных
   ``'В работе'``: схема не содержит данных, и запрос выполнялся, возвращая
   уверенный ноль. Каталог отдаёт настоящие значения; проверяется, что он
   берёт справочные колонки, а не свободный текст, и что его чтения не
   выглядят как запрос модели.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from libs.audit.generated_sql import (
    _value_catalog,
    is_value_catalog_query,
)


class _Reader:
    """Снимок с одним справочником и одной колонкой свободного текста."""

    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values
        self.queries: list[str] = []

    def __call__(self, text: str, params: list[Any] | None = None) -> dict[str, Any]:
        self.queries.append(text)
        for column, values in self._values.items():
            if f'"{column}"' in text:
                return {
                    "status": "success",
                    "row_count": len(values),
                    "columns": ["value"],
                    "rows": [{"value": v} for v in values],
                }
        return {"status": "success", "row_count": 0, "columns": [], "rows": []}


_TABLES = {
    "audits": {
        "columns": {
            "status": {"type": "character varying(30)"},
            "description": {"type": "text"},
            "amount": {"type": "numeric(10,2)"},
        }
    }
}


class TestValueCatalog:
    def test_listed_enum_values_reach_the_prompt(self) -> None:
        reader = _Reader({"status": ["В работе", "Завершена"]})

        catalog = _value_catalog(reader, ("oarb.audits",), _TABLES)

        assert "'В работе'" in catalog
        assert "'Завершена'" in catalog

    def test_free_text_column_is_skipped(self) -> None:
        """Колонка с длинными значениями — это данные, а не справочник."""
        reader = _Reader({"description": ["x" * 200, "y" * 200]})

        catalog = _value_catalog(reader, ("oarb.audits",), _TABLES)

        assert catalog == ""

    def test_numeric_column_is_not_queried(self) -> None:
        reader = _Reader({})

        _value_catalog(reader, ("oarb.audits",), _TABLES)

        assert all("amount" not in q for q in reader.queries)

    def test_catalog_reads_are_marked(self) -> None:
        """Служебное чтение не должно выглядеть как исполненный запрос модели."""
        reader = _Reader({"status": ["В работе"]})

        _value_catalog(reader, ("oarb.audits",), _TABLES)

        assert reader.queries
        assert all(is_value_catalog_query(q) for q in reader.queries)
        assert not is_value_catalog_query(
            "SELECT COUNT(*) FROM oarb.audits WHERE status = 'В работе' LIMIT 1"
        )

    def test_qualified_name_is_split_not_quoted_whole(self) -> None:
        """``"oarb.audits"`` — несуществующий идентификатор."""
        reader = _Reader({"status": ["В работе"]})

        _value_catalog(reader, ("oarb.audits",), _TABLES)

        assert '"oarb"."audits"' in reader.queries[0]


class TestAuditToolSerialization:
    def test_dates_survive_serialization(self) -> None:
        """Даты в ответе — обычное дело, а не повод уронить операцию."""
        from servers.enterprise.capabilities.audit.service.main import AuditService

        payload = {
            "status": "ok",
            "rows": [
                {
                    "planned_date": dt.date(2024, 1, 16),
                    "actual_date": dt.date(2024, 1, 23),
                    "created_at": dt.datetime(2026, 4, 26, 21, 51, 48),
                }
            ],
        }

        parsed = json.loads(AuditService.dumps(payload))

        assert parsed["rows"][0]["planned_date"] == "2024-01-16"
        assert parsed["rows"][0]["created_at"].startswith("2026-04-26")

    def test_tools_use_the_service_serializer(self) -> None:
        """Правило сериализации живёт в сервисе; tool-слой его не дублирует.

        Раньше сервис знал про ``default=str``, а операции звали ``json.dumps``
        сами — и знание терялось ровно на границе, где оно нужно.
        """
        from pathlib import Path

        tools_dir = (
            Path(__file__).resolve().parent.parent
            / "servers"
            / "enterprise"
            / "capabilities"
            / "audit"
            / "tools"
        )
        for name in ("generate_sql", "run_script", "list_scripts"):
            source = (tools_dir / f"{name}.py").read_text(encoding="utf-8")
            assert "json.dumps(" not in source, f"{name}: сериализация обойдена"
            assert "service.dumps(" in source, f"{name}: не используется правило сервиса"
