"""Доказательство, что слой атрибуции памяти работает и в платформенном наборе.

Зачем отдельный файл, если правила должны быть одинаковыми. Платформа — не
копия агента, а вторая кодовая база со своим ``pyproject.toml``, и плагин
``tests/_memguard.py`` здесь физически другой файл. Общего модуля быть не
может: платформенный архитектурный страж запрещает платформе импортировать
код агента (``tools`` стоит в FORBIDDEN_ROOTS). Поэтому единство держится
не кодом, а проверками — и эти проверки обязаны существовать с обеих сторон,
иначе «правило одно» останется словами в докстринге.

Что здесь доказывается:

* слой 2 действительно срабатывает — тест, поднявший 64 МБ, помечается
  проваленным ПОИМЁННО, с приростом в мегабайтах, при бюджете 32 МБ;
* та же нагрузка при бюджете 256 МБ проходит (граница сверху);
* измеритель рабочей памяти на этой ОС работает — иначе слой молчал бы и
  «утечек нет» означало бы «кто-то не измеряет»;
* копия плагина не тянет код агента — иначе страж границ правомерно упал бы,
  а это проверяется ДО того, как он упадёт.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = PLATFORM_ROOT / "tests" / "_memguard.py"
SCHEDULER = PLATFORM_ROOT.parent / "tools" / "run_tests.py"

#: Канонические значения. Объявлены здесь как ЭТАЛОН: агентский набор сверяет
#: с ними свою копию, этот — свою. Третьего места, где живут эти числа, быть
#: не должно.
EXPECTED_ENV_DELTA_MB = "NANOBOT_TEST_MEM_DELTA_MB"
EXPECTED_DEFAULT_DELTA_MB = 300.0

#: Корни агента, которые платформе импортировать нельзя. Продублировано из
#: ``test_architecture_boundaries.py`` намеренно: этот файл проверяет КОПИЮ
#: плагина, а не код платформы вообще, и опираться на чужой тест на то, что
#: его правила ещё в силе, нельзя.
AGENT_ROOTS = frozenset({"nanobot", "lib", "workspace", "tools", "benchmarks", "config"})

#: Нагрузка для слоя 2: тихий тест и жадный, удерживающий 64 МБ.
INNER_DELTA = '''
HELD = []


def test_calm():
    assert True


def test_greedy():
    for _ in range(4):
        HELD.append(bytearray(16 * 1024 * 1024))
    assert sum(len(block) for block in HELD) == 64 * 1024 * 1024
'''


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    # ``tests`` платформы — namespace-пакет (нет __init__.py), поэтому
    # ``-p tests._memguard`` находится через корень платформы в PYTHONPATH.
    env["PYTHONPATH"] = str(PLATFORM_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("NANOBOT_TEST_MEM_DELTA_MB", None)
    env.update(extra)
    return env


def _run_inner(path: Path, budget_mb: str):
    """Прогнать платформенный pytest с плагином атрибуции."""
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", str(path), "-q",
            "-p", "no:cacheprovider", "-p", "tests._memguard",
        ],
        cwd=str(path.parent),
        env=_env(NANOBOT_TEST_MEM_DELTA_MB=budget_mb),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )


class TestDeltaAttribution:
    """Сторож с дельтой должен называть виновника и уважать границу."""

    def test_culprit_test_is_named_with_its_delta(self, tmp_path: Path) -> None:
        """Жадный тест помечается проваленным поимённо, тихий остаётся зелёным.

        Прирост 64 МБ против бюджета 32 МБ. Сообщение обязано содержать и имя
        теста, и величину прироста в мегабайтах: «прогон съел память» без
        указания виновника бесполезно ровно настолько же, насколько бесполезен
        отчёт без текста.
        """
        target = tmp_path / "test_delta.py"
        target.write_text(INNER_DELTA, encoding="utf-8")

        done = _run_inner(target, "32")
        output = done.stdout + done.stderr

        assert done.returncode == 1, output
        assert "test_greedy" in output, output
        assert "прирост рабочей памяти" in output, output
        assert "32 МБ" in output, output
        assert "1 failed, 1 passed" in output, output

    def test_same_load_passes_under_budget(self, tmp_path: Path) -> None:
        """Та же нагрузка при бюджете выше прироста проходит.

        Граница сверху: бюджет дельты не должен быть запретом на любую тяжёлую
        работу — под ним лежат штатные тесты со снимком DuckDB и FAISS.
        """
        target = tmp_path / "test_delta.py"
        target.write_text(INNER_DELTA, encoding="utf-8")

        done = _run_inner(target, "256")
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        assert "2 passed" in output, output


class TestPlatformCopyStaysACopy:
    """Копия обязана совпадать с эталоном и не тянуть за собой агента."""

    def test_budget_matches_the_canonical_value(self) -> None:
        """Имя переменной и бюджет по умолчанию — как в эталоне."""
        from tests import _memguard

        assert _memguard.ENV_DELTA_MB == EXPECTED_ENV_DELTA_MB
        assert _memguard.DEFAULT_DELTA_MB == EXPECTED_DEFAULT_DELTA_MB

    def test_measurement_works_on_this_platform(self) -> None:
        """Измеритель обязан работать: молчащая деградация читается как «чисто»."""
        from tests import _memguard

        assert _memguard.working_set_bytes() is not None, (
            f"измерение рабочей памяти не поддерживается на {sys.platform}"
        )
        assert _memguard.working_set_bytes() > 0

    def test_plugin_does_not_import_agent_code(self) -> None:
        """Копия не имеет права импортировать агента — это и есть причина копии.

        Проверяется по AST, а не подстрокой: обсуждать запрет в докстринге
        можно и нужно, ловить надо только реальные импорты.
        """
        tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8-sig"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in AGENT_ROOTS:
                    offenders.append(f"{PLUGIN_PATH.name}:{node.lineno}: {name}")
        assert not offenders, "платформенная копия тянет код агента:\n  " + "\n  ".join(
            offenders
        )

    def test_scheduler_is_available_for_the_platform(self) -> None:
        """Планировщик один на обе кодовые базы и запускается с их корнем.

        Проверяется только то, что он на месте и принимает ``--root`` платформы:
        полный прогон платформы здесь не запускается (это отдельная работа), а
        сам планировщик уже покрыт тестами агентского набора.
        """
        assert SCHEDULER.is_file(), f"планировщик не найден: {SCHEDULER}"
        done = subprocess.run(
            [sys.executable, str(SCHEDULER), "--root", str(PLATFORM_ROOT), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        assert "--memory-cap-mb" in done.stdout
        assert "--max-seconds" in done.stdout


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
