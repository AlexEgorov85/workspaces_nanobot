"""Живая проверка предела времени startup-валидации схемы.

Запуск: ``NANOBOT_LIVE_E2E=1 pytest tests/test_startup_schema_validation_live.py``
с ``DATABASE_URL`` в окружении. Без переменной — тест пропускается
и ничего не делает.

Модульные тесты проверяют, что ``statement_timeout`` **выставляется**.
Этот файл проверяет, что он **срабатывает**: настоящий PostgreSQL,
настоящий пул воркеров, настоящий ``QueryCanceled``, настоящий перевод в
``SchemaValidationTimeoutError``. Мок не может доказать, что сервер понимает
оператор — а понимать он перестал бы при смене версии или за прокси-обёрткой.

Живой прогон окупился сразу: он показал, что по истечении
``statement_timeout`` PostgreSQL **прерывает транзакцию**, и сброс предела в
``finally`` без предварительного отката падает с ``InFailedSqlTransaction``.
На моке это увидеть невозможно — а соединение из общего пула после этого
отказало бы следующему владельцу.

Здесь же проверяется, что после отказа пул остаётся годным.

См. ``openspec/specs/runtime/startup-schema-validation/spec.md``.
"""
from __future__ import annotations

import os
import time
from typing import Any

import pytest

from lib.services.schema_validation import (
    SchemaValidationService,
    SchemaValidationTimeoutError,
)

pytest.importorskip("psycopg2")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NANOBOT_LIVE_E2E", "") != "1",
        reason=(
            "Живой прогон выключен. Задайте NANOBOT_LIVE_E2E=1 и DATABASE_URL, "
            "чтобы проверить предел времени на настоящем PostgreSQL."
        ),
    ),
]

#: Заведомо долгий SELECT. ``pg_sleep`` выбран потому, что ни один
#: оптимизатор не выбросит его из плана — в отличие от тяжёлого чтения.
_SLEEPING_SQL = "SELECT pg_sleep(3) AS table_schema, pg_sleep(0) AS table_name"


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("DATABASE_URL не задан — живая проверка невозможна")
    return dsn


@pytest.fixture
def pool():
    """Настоящий пул воркеров против живой базы."""
    from utils import db as utils_db

    utils_db.configure(_dsn())
    utils_db.start()
    try:
        yield utils_db
    finally:
        utils_db.shutdown()


def _bounded(db: Any, timeout_sec: float):
    """Адаптер, который проверка получает вместо «голого» ``fetch``."""

    def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
        return db.fetch_with_timeout(sql, *params, timeout_sec=timeout_sec)

    return _fetch


class TestTimeoutOnRealServer:
    """Механизм предела проверяется на намеренно долгом запросе.

    Реальный SELECT к ``information_schema.tables`` укладывается в
    миллисекунды, поэтому вызвать таймаут на нём нельзя: проверка
    прошла бы, будучи бесполезной. Долгий запрос подставляется явно.
    """

    def test_expired_statement_is_cancelled_by_server(self, pool) -> None:
        """``pg_sleep`` под настоящим пределом: сервер отменяет сам."""
        from psycopg2 import errors as pg_errors

        with pytest.raises(pg_errors.QueryCanceled):
            pool.fetch_with_timeout(_SLEEPING_SQL, timeout_sec=0.3)

    def test_timeout_is_armed_not_ignored(self, pool) -> None:
        """Предел, который сервер игнорирует, — это не предел.

        Если бы ``statement_timeout`` не применялся, запрос отработал бы все
        три секунды и вернул строки. Замер это отличает.
        """
        from psycopg2 import errors as pg_errors

        started = time.monotonic()
        with pytest.raises(pg_errors.QueryCanceled):
            pool.fetch_with_timeout(_SLEEPING_SQL, timeout_sec=0.3)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, (
            f"отмена пришла через {elapsed:.2f} с — предел не применился "
            "(отменял клиент, а не сервер)"
        )

    def test_pool_stays_usable_after_timeout(self, pool) -> None:
        """Соединение возвращается в общий пул — предел и транзакция сняты.

        Без отката соединение осталось бы с прерванной транзакцией, и
        следующий владелец получил бы ``InFailedSqlTransaction`` от запроса,
        который он даже не писал.
        """
        from psycopg2 import errors as pg_errors

        with pytest.raises(pg_errors.QueryCanceled):
            pool.fetch_with_timeout(_SLEEPING_SQL, timeout_sec=0.3)
        # Тот же пул после отказа: обычный SELECT обязан пройти.
        assert pool.fetchval("SELECT 1") == 1

    def test_timeout_is_not_left_on_a_pool_connection(self, pool) -> None:
        """Предел снимается и тогда, когда запрос не был прерван.

        Иначе тяжёлый запрос к ``information_schema`` ушёл бы в чужие
        соединения общего пула.
        """
        pool.fetch_with_timeout(
            "SELECT table_schema, table_name FROM information_schema.tables",
            timeout_sec=5.0,
        )
        value = str(pool.fetchval("SHOW statement_timeout"))
        assert value in ("0", "0ms"), (
            f"предел остался на соединении после успешного запроса: {value}"
        )

    def test_server_side_cancel_is_translated_to_domain_error(self, pool) -> None:
        """Полный путь: настоящая отмена сервером → доменный отказ.

        Проверка предела не имеет права сообщить «нет таблиц»: оператор
        пошёл бы применять миграции там, где нужен DBA.
        """
        from config import runtime_table

        def _slow_fetch(_sql: str, *_params: Any) -> list[dict[str, Any]]:
            return pool.fetch_with_timeout(_SLEEPING_SQL, timeout_sec=0.3)

        with pytest.raises(SchemaValidationTimeoutError) as exc_info:
            SchemaValidationService.check_tables(
                _slow_fetch,
                [("public", runtime_table("gateway_logs"))],
                timeout_sec=0.3,
            )
        assert exc_info.value.timeout_sec == 0.3
        # Сообщение адресовано оператору и по-русски: проверяется именно
        # actionable-часть, а не английский идентификатор настройки.
        assert "gateway.startup.schema_validation.timeout_sec" in str(exc_info.value)
        assert "0.3" in str(exc_info.value)


class TestRealRuntimeTables:
    def test_runtime_tables_are_found_on_a_real_server(self, pool) -> None:
        """Путь целиком на настоящих данных.

        Пять runtime-таблиц читаются одним SELECT — тем же кодом, что
        работает на старте агента.
        """
        from config import runtime_table

        expected = [
            ("public", runtime_table("conversation_messages")),
            ("public", runtime_table("session_messages")),
            ("public", runtime_table("session_meta")),
            ("public", runtime_table("gateway_logs")),
            ("public", runtime_table("question_runs")),
        ]
        missing = SchemaValidationService.check_tables(
            _bounded(pool, 5.0), expected
        )
        assert missing == [], (
            "runtime-таблицы отсутствуют в живой БД: "
            f"{[m.full_name for m in missing]}. Проверьте, что профиль test "
            "применён (python tools/apply_test_profile_tables.py)."
        )

    def test_absent_table_is_reported_on_a_real_server(self, pool) -> None:
        """Заведомо несуществующая таблица обязана попасть в список missing."""
        missing = SchemaValidationService.check_tables(
            _bounded(pool, 5.0), [("public", "agent_definitely_absent_table")]
        )
        assert [m.name for m in missing] == ["agent_definitely_absent_table"]
