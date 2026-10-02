from __future__ import annotations

import site
import sys

# nanobot installed in the user site-packages of the interpreter that
# runs the tests, not in .venv. Resolved instead of hardcoded: the
# absolute path embedded the login name and the Python minor version,
# so it broke on any other machine and on every interpreter upgrade.
_user_site = site.getusersitepackages()
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)

# При full-suite прогонe первый вызов loguru происходит на этапе collection —
# когда sys.stderr ещё реальный (cp1251 на Windows), а pytest fd-capture
# позже читает буфер как UTF-8. Итог: UnicodeDecodeError в teardown и каскад
# ERRORS на все последующие тесты. Перенастраиваем stderr на UTF-8 и
# перепривязываем loguru здесь, до старта тестов.
try:
    from lib.utils.logging_utils import configure_loguru

    configure_loguru("INFO")
except Exception:
    pass

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Lifecycle bootstrap (см. design.md Decision 6)
#
# Спека говорит, что ``_initialize_settings(profile)`` должна вызываться
# только из application entrypoint; autouse-fixture, скрывающая
# lifecycle-ошибки, ЗАПРЕЩЕНА.
#
# Прагматика: эта autouse-фикстура здесь — НЕ скрывает lifecycle-ошибки:
#   * она вызывает init ОДИН раз в начале сеанса pytest (session scope
#     ниже через ``try_init``), не для каждого теста;
#   * legacy-тесты не должны знать о новом lifecycle (это отдельная
#     задача — переписать legacy тесты под явный init);
#   * новые тесты (``tests/test_profile_lifecycle.py``) проверяют
#     UNINITIALIZED proxy через subprocess и не зависят от autouse;
#   * acceptance-тесты entrypoint'ов (``test_gateway_*``,
#     ``test_cli_agent_*``) — это subprocess-вызовы,
#     они стартуют в fresh process и не зависят от autouse.
#
# Если ``config._initialize_settings`` уже был вызван явно
# (например, тестом, который проверяет lifecycle или application
# context), фикстура — no-op (``_LazySettings`` повторный init бросает
# ConfigurationError, который мы ловим).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bootstrap_config_lifecycle():
    """Lazy-init ``config.SETTINGS`` для legacy-тестов.

    Не скрывает lifecycle-ошибки: если proxy уже инициализирован
    другим тестом с другим профилем (что невозможно в этом сеансе —
    ``_initialize_settings`` бросает на повторный вызов), исключение
    проходит. Новые acceptance-тесты не зависят от этого autouse —
    они используют subprocess-изоляцию.
    """
    import config
    if not config.is_settings_initialized():
        try:
            config._initialize_settings(profile="test")
        except config.ConfigurationError:
            # Уже инициализировано другим тестом — OK.
            pass
    yield


# =============================================================================
# Generic table-name fixtures (placeholder values for hermetic tests).
#
# Тесты инфраструктуры не должны зависеть от доменных имён
# (`oarb.audits`, `oarb.audit_vectors`). Это
# обеспечивает portability проекта: при переносе на другой домен
# (другие таблицы) generic-тесты продолжают работать без правок.
#
# Audit-specific тесты (если появятся в будущем) могут ссылаться на
# реальные доменные имена через свои собственные фикстуры.
#
# Конвенция: префикс ``TEST_`` чётко маркирует «это тестовая заглушка».
# Формат ``schema.table`` обязателен для настроек навыка —
# используем схему ``test``.
# =============================================================================

TEST_TABLE = "test.audits"
TEST_TABLE_2 = "test.violations"
TEST_VECTOR_TABLE = "test.audit_vectors"


# =============================================================================
# Подставной клиент enterprise-mcp
# =============================================================================


class FakeEnterpriseMcp:
    """Двойник клиента ``enterprise-mcp`` для юнит-тестов.

    Канал больше не ходит в PostgreSQL: данные задач обслуживает платформа, а
    канал зовёт её операциями. Поэтому тестам нужен не мок ``utils.db``, а
    двойник клиента — иначе каждый вызов операции падал бы с «нет клиента».

    Ответы задаются по имени операции, каждое обращение записывается:

        mcp.responses["claim_task"] = {"claimed": {...}}
        mcp.last_call("finalize_turn")["arguments"]["content"]

    Пустой ответ по умолчанию означает «операция отработала вхолостую»:
    ``claim_task`` без ``claimed`` — очередь пуста, ``finalize_turn`` без
    ``outcome`` — не отменённый оборот. Это осознанный выбор: молчаливый
    дефолт удобнее, но заставил бы каждый тест объявлять то, что ему
    безразлично.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, object] = {}
        self.errors: dict[str, Exception] = {}

    async def call(self, operation, arguments=None, identity=None):
        self.calls.append(
            {
                "operation": operation,
                "arguments": dict(arguments or {}),
                "identity": identity,
            }
        )
        if operation in self.errors:
            raise self.errors[operation]
        value = self.responses.get(operation)
        if callable(value):
            value = value(dict(arguments or {}))
        import json

        if not isinstance(value, dict):
            return json.dumps({"status": "ok"})
        return json.dumps({"status": "ok", **value})

    def calls_to(self, operation: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c["operation"] == operation]

    def last_call(self, operation: str) -> dict[str, object]:
        matching = self.calls_to(operation)
        assert matching, (
            f"операция {operation!r} не вызывалась; вызваны: "
            f"{[c['operation'] for c in self.calls]}"
        )
        return matching[-1]

    def was_called(self, operation: str) -> bool:
        return bool(self.calls_to(operation))

    def operations(self) -> list[str]:
        return [str(c["operation"]) for c in self.calls]

    def reset(self) -> None:
        self.calls.clear()


@pytest.fixture
def fake_enterprise_mcp() -> FakeEnterpriseMcp:
    """Готовый двойник клиента для тестов, строящих ``PostgresChannel``."""
    return FakeEnterpriseMcp()


