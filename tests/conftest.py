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

import tempfile
from pathlib import Path
from typing import Iterator

import pytest
import yaml


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
#     ``test_cli_agent_*``, ``test_streamlit_*``) — это subprocess-вызовы,
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

from benchmarks.models import (
    BenchExpect,
    BenchItem,
    BenchResult,
    BenchStep,
    BenchSuite,
    CheckResult,
    EvalResult,
    StepResult,
)


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


@pytest.fixture
def sample_expect() -> BenchExpect:
    return BenchExpect(
        tools=["exec", "glob"],
        skills=["coding"],
        keywords_include=["hello", "world"],
        keywords_exclude=["error"],
        max_iterations=10,
        match_type="keyword",
        check_file="output.txt",
        check_file_content="success",
    )


@pytest.fixture
def sample_item_single() -> BenchItem:
    return BenchItem(
        id="test-1",
        name="Test item",
        difficulty=3,
        category="general",
        type="single",
        question="What is 2+2?",
        expect=BenchExpect(
            keywords_include=["4"],
            tools=["exec"],
        ),
    )


@pytest.fixture
def sample_item_multi_step() -> BenchItem:
    return BenchItem(
        id="multi-1",
        name="Multi-step task",
        difficulty=7,
        category="coding",
        type="multi_step",
        steps=[
            BenchStep(step=1, question="Step one", weight=0.5),
            BenchStep(step=2, question="Step two", weight=0.5),
        ],
    )


@pytest.fixture
def sample_eval_result() -> EvalResult:
    return EvalResult(
        passed=True,
        total_score=0.85,
        checks=[
            CheckResult("tools", True, 1.0, "All tools used"),
            CheckResult("keywords_include", True, 1.0, "Keywords found"),
            CheckResult("iterations", True, 0.7, "Within limit"),
        ],
    )


@pytest.fixture
def sample_step_results() -> list[StepResult]:
    return [
        StepResult(
            step=1,
            weight=0.6,
            passed=True,
            score=0.9,
            response="Step one done",
            tools_used=["exec"],
            iterations=3,
            duration_sec=5.0,
        ),
        StepResult(
            step=2,
            weight=0.4,
            passed=True,
            score=0.8,
            response="Step two done",
            tools_used=["glob"],
            iterations=2,
            duration_sec=3.0,
        ),
    ]


@pytest.fixture
def temp_yaml_file() -> Iterator[Path]:
    data = [
        {
            "id": "yaml-1",
            "name": "YAML test",
            "difficulty": 2,
            "category": "basic",
            "type": "single",
            "question": "Test?",
            "expect": {"keywords_include": ["yes"]},
        },
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8", delete=False
    ) as f:
        yaml.dump(data, f)
        tmp_path = Path(f.name)
    yield tmp_path
    tmp_path.unlink(missing_ok=True)


@pytest.fixture
def temp_dir_with_yaml() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        d = Path(tmp_dir)
        for i, name in enumerate(["a.yaml", "b.yaml", "_template.yaml"]):
            items = [
                {
                    "id": f"file-{i}-1",
                    "name": f"Item from {name}",
                    "difficulty": 3,
                    "category": "test",
                    "type": "single",
                    "question": f"Question {i}?",
                }
            ]
            file_data = {"name": name.replace(".yaml", ""), "items": items}
            (d / name).write_text(yaml.dump(file_data), encoding="utf-8")
        yield d


@pytest.fixture
def sample_bench_result() -> BenchResult:
    return BenchResult(
        item_id="result-1",
        item_name="Result item",
        difficulty=5,
        passed=True,
        total_score=0.85,
        response="Done",
        tools_used=["exec"],
        total_iterations=5,
        duration_sec=10.0,
    )
