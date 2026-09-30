from __future__ import annotations

import sys

# nanobot installed in user site-packages, not in .venv
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
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


# ---------------------------------------------------------------------------
# Repo-root resolution (Phase 8 — was hardcoded ``Path("workspace/...")``
# в 3 аудит-тест-файлах, ломался при запуске pytest не из cwd репо).
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Подняться от ``start`` вверх до корня репозитория.

    Ищем ``workspace/skills/audit_analyzer/SKILL.md`` вверх по дереву.
    Используется для абсолютного пути к skill'у без зависимости от cwd.
    """
    cur = start.resolve()
    for _ in range(8):
        if (cur / "workspace" / "skills" / "audit_analyzer" / "SKILL.md").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(
        f"Cannot find repo root from {start}: "
        "workspace/skills/audit_analyzer/SKILL.md not found"
    )


REPO_ROOT = _find_repo_root(Path(__file__).parent)
AUDIT_SKILL_DIR = REPO_ROOT / "workspace" / "skills" / "audit_analyzer"
AUDIT_SKILL_MD = AUDIT_SKILL_DIR / "SKILL.md"
AUDIT_CLI_PATH = AUDIT_SKILL_DIR / "scripts" / "cli.py"


# =============================================================================
# Generic table-name fixtures (placeholder values for hermetic tests).
#
# Тесты инфраструктуры (cache_store, registry, sync, skill_config) не должны
# зависеть от доменных имён (`oarb.audits`, `oarb.audit_vectors`). Это
# обеспечивает portability проекта: при переносе на другой домен
# (другие таблицы) generic-тесты продолжают работать без правок.
#
# Audit-specific тесты (если появятся в будущем) могут ссылаться на
# реальные доменные имена через свои собственные фикстуры.
#
# Конвенция: префикс ``TEST_`` чётко маркирует «это тестовая заглушка».
# Формат ``schema.table`` обязателен для VectorResource (см.
# ``table_registry.VectorResource.__post_init__``) — используем
# схему ``test``.
# =============================================================================

TEST_TABLE = "test.audits"
TEST_TABLE_2 = "test.violations"
TEST_VECTOR_TABLE = "test.audit_vectors"
TEST_VECTOR_INDEX_NAME = "test_index"


