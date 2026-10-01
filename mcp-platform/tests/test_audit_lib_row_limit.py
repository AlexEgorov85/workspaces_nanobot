"""Потолок строк сгенерированного запроса (пункт 4.8).

В агенте ``LIMIT`` дописывал только сборщик предопределённых скриптов.
Сгенерированный запрос уходил в снимок как есть — сколько вернула модель,
столько и тянулось, а часто это был ``LIMIT 1000000`` или вообще без
``LIMIT``.

Требование пункта 4.8 — не «дописать LIMIT», а «применить потолок **и
проверить, что он применён**». Поэтому здесь проверяются обе стороны:
``enforce_row_limit`` правильно ставит потолок, и ``assert_row_limit``
отказывает на тексте, где потолка нет (в том числе на том, который
пришёл извне и не проходил через ``enforce_row_limit``).
"""

from __future__ import annotations

import pytest
from libs.audit import (
    AuditValidationError,
    RowLimitNotAppliedError,
    assert_row_limit,
    enforce_row_limit,
    read_row_limit,
)

CEILING = 1000


class TestEnforceAppliesCeiling:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("SELECT * FROM oarb.audits", "SELECT * FROM oarb.audits LIMIT 1000"),
            (
                "SELECT * FROM oarb.audits LIMIT 1000000",
                "SELECT * FROM oarb.audits LIMIT 1000",
            ),
            (
                "SELECT * FROM oarb.audits LIMIT 1000",
                "SELECT * FROM oarb.audits LIMIT 1000",
            ),
            ("SELECT * FROM oarb.audits LIMIT 5", "SELECT * FROM oarb.audits LIMIT 5"),
            (
                "SELECT * FROM oarb.audits LIMIT ALL",
                "SELECT * FROM oarb.audits LIMIT 1000",
            ),
            (
                "SELECT * FROM oarb.audits FETCH FIRST 5000000 ROWS ONLY",
                "SELECT * FROM oarb.audits LIMIT 1000",
            ),
            (
                "SELECT * FROM oarb.audits OFFSET 5",
                "SELECT * FROM oarb.audits LIMIT 1000 OFFSET 5",
            ),
            (
                "SELECT * FROM oarb.audits ORDER BY id",
                "SELECT * FROM oarb.audits ORDER BY id LIMIT 1000",
            ),
        ],
        ids=[
            "без-limit",
            "слишком-большой",
            "ровно-потолок",
            "меньше-потолка",
            "limit-all",
            "fetch-first",
            "offset",
            "order-by",
        ],
    )
    def test_result_has_expected_ceiling(self, text: str, expected: str) -> None:
        assert enforce_row_limit(text, CEILING) == expected

    def test_applied_limit_is_verified_by_reparsing(self) -> None:
        """Возвращённый текст сам себе доказывает потолок."""
        applied = enforce_row_limit("SELECT * FROM oarb.audits", CEILING)
        assert read_row_limit(applied) == CEILING
        assert assert_row_limit(applied, CEILING) == CEILING

    def test_ceiling_is_honoured(self) -> None:
        assert enforce_row_limit("SELECT * FROM oarb.audits", 50).endswith("LIMIT 50")

    def test_small_existing_limit_is_not_inflated(self) -> None:
        """Потолок не должен увеличивать выдачу."""
        assert enforce_row_limit("SELECT * FROM oarb.audits LIMIT 7", CEILING) == (
            "SELECT * FROM oarb.audits LIMIT 7"
        )

    def test_union_gets_limit_on_the_whole_query(self) -> None:
        applied = enforce_row_limit(
            "SELECT id FROM oarb.audits UNION SELECT id FROM oarb.violations", CEILING
        )
        assert applied.endswith("LIMIT 1000")
        assert read_row_limit(applied) == CEILING

    def test_subquery_without_limit_gets_outer_limit(self) -> None:
        applied = enforce_row_limit(
            "SELECT * FROM (SELECT * FROM oarb.audits) t", CEILING
        )
        assert read_row_limit(applied) == CEILING

    def test_subquery_with_own_limit_still_gets_outer_limit(self) -> None:
        applied = enforce_row_limit(
            "SELECT * FROM (SELECT * FROM oarb.audits LIMIT 1) t", CEILING
        )
        assert read_row_limit(applied) == CEILING

    def test_cte_query_gets_limit(self) -> None:
        applied = enforce_row_limit(
            "WITH x AS (SELECT * FROM oarb.audits) SELECT * FROM x", CEILING
        )
        assert applied.endswith("LIMIT 1000")

    def test_result_text_is_what_gets_executed(self) -> None:
        """Именно отрисованный текст, а не исходный, уходит в снимок."""
        applied = enforce_row_limit("SELECT * FROM oarb.audits LIMIT 99999999", CEILING)
        assert read_row_limit(applied) <= CEILING


class TestAssertRejectsUnbounded:
    """Проверка, которая честно отказывает, а не доверяет намерению."""

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM oarb.audits",
            "SELECT * FROM oarb.audits LIMIT ALL",
            "SELECT * FROM oarb.audits LIMIT 1000000",
            "SELECT * FROM oarb.audits FETCH FIRST 5000000 ROWS ONLY",
            "SELECT * FROM (SELECT * FROM oarb.audits LIMIT 1) t",
        ],
        ids=["без-limit", "limit-all", "слишком-большой", "fetch", "только-внутренний"],
    )
    def test_rejects_text_without_usable_ceiling(self, text: str) -> None:
        with pytest.raises(RowLimitNotAppliedError) as excinfo:
            assert_row_limit(text, CEILING)
        assert excinfo.value.code == "row_limit_not_applied"
        assert excinfo.value.ceiling == CEILING

    def test_accepts_within_ceiling(self) -> None:
        assert assert_row_limit("SELECT * FROM oarb.audits LIMIT 10", CEILING) == 10

    def test_enforce_output_always_passes_assert(self) -> None:
        for text in [
            "SELECT * FROM oarb.audits",
            "SELECT * FROM oarb.audits LIMIT 1000000",
            "SELECT * FROM oarb.audits LIMIT 3",
        ]:
            assert_row_limit(enforce_row_limit(text, CEILING), CEILING)

    def test_read_row_limit_of_garbage_raises(self) -> None:
        with pytest.raises(AuditValidationError):
            read_row_limit("не SQL вовсе")


class TestBadCeilings:
    @pytest.mark.parametrize(
        "ceiling", [0, -1, -1000, "1000", None, 10.5, True, False]
    )
    def test_invalid_ceiling_rejected(self, ceiling: object) -> None:
        """``bool`` — подкласс ``int``, но потолком быть не может."""
        with pytest.raises(AuditValidationError):
            enforce_row_limit("SELECT * FROM oarb.audits", ceiling)  # type: ignore[arg-type]
        with pytest.raises(AuditValidationError):
            assert_row_limit("SELECT * FROM oarb.audits LIMIT 1", ceiling)  # type: ignore[arg-type]

    def test_ddl_is_not_given_a_limit(self) -> None:
        """Потолок строк не должен превращать ``DROP`` в executes-only-текст."""
        with pytest.raises(AuditValidationError):
            enforce_row_limit("DROP TABLE oarb.audits", CEILING)

    def test_multi_statement_is_not_given_a_limit(self) -> None:
        """Иначе потолок приклеится к первой операции, а вторая уедет как есть."""
        with pytest.raises(AuditValidationError):
            enforce_row_limit(
                "SELECT * FROM oarb.audits; DELETE FROM oarb.audits", CEILING
            )

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(AuditValidationError):
            enforce_row_limit("", CEILING)
