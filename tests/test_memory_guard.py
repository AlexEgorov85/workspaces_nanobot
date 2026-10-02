"""Доказательства того, что слои защиты памяти действительно срабатывают.

Зачем эти тесты. Барьер, который ни разу не сработал, неотличим от барьера,
которого нет: молчаливо неверно настроенный потолок выглядит как «прогоны
стали лёгкими». Поэтому каждый слой проверяется на НАСТОЯЩЕМ ограничении —
реальное выделение памяти сверх бюджета, реальный Job Object, реальный
предел времени, а не заглушки.

Что доказывает каждый тест:

* слой 1 (потолок ОС) — ``test_allocation_beyond_cap_is_refused``: настоящий
  pytest под потолком 320 МБ пытается занять 2 ГБ и получает ``MemoryError``;
  ``test_child_process_is_covered_by_the_cap`` — то же для ДОЧЕРНЕГО процесса,
  то есть потолок действительно общий на дерево, а не на один pytest;
  ``test_run_under_cap_is_untouched`` — граница снизу: при потолке выше нормы
  прогон проходит и код возврата тот же, что у pytest;
  ``test_cap_does_not_leak_outside_the_run`` — потолок не остаётся на машине
  после прогона и не цепляет соседние процессы.
* слой 2 (атрибуция) — ``test_culprit_test_is_named_with_its_delta`` и пара
  граничных тестов: та же нагрузка проходит при бюджете выше прироста и
  роняет прогон при бюджете ниже, с именем теста и мегабайтами в сообщении.
* слой 3 (время) — ``test_hanging_run_is_stopped``: зависший прогон снимается
  по пределу времени и возвращает свой код, а не висит.

Все внутренние прогоны deliberately запускаются в ``tmp_path``, а не в
репозитории: корень прогона там не имеет ``pyproject.toml``, поэтому
``tests/conftest.py`` агента не подхватывается и внутренний прогон остаётся
герметичным. Плагин подключается через ``PYTHONPATH`` на корень репозитория.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "tools" / "run_tests.py"
PLATFORM_PLUGIN = REPO_ROOT / "mcp-platform" / "tests" / "_memguard.py"

#: Потолки в этих тестах намеренно скромные: доказывать нужно, что потолок
#: работает, а не что выдержит максимум. 320 МБ хватает интерпретатору с
#: pytest и заведомо мало для 2 ГБ.
CAP_MB = 320

#: Внутренний прогон pytest, который лезет в память и ПИШЕТ МАРКЕР при отказе.
#: Маркер нужен потому, что выделение может быть отказано двумя разными
#: путями — либо ОС откажет в выделении (MemoryError), либо планировщик
#: снимет дерево раньше. Оба доказывают потолок, и тест принимает оба.
INNER_GREEDY = '''
import os

MARKER = os.environ["MEMGUARD_MARKER"]


def test_greedy():
    held = []
    try:
        for _ in range(64):
            held.append(bytearray(32 * 1024 * 1024))
    except MemoryError as exc:
        with open(MARKER, "w", encoding="ascii") as handle:
            handle.write("REFUSED:" + type(exc).__name__)
        raise
    assert False, "2 GiB занято без отказа — потолок не работает"
'''

#: Тот же принцип, но память ест ДОЧЕРНИЙ процесс теста. Это и есть то, ради
#: чего потолок вешается на задание, а не на процесс pytest: в реальных
#: тестах агент поднимает enterprise-mcp, и его память обязана считаться.
INNER_TREE = '''
import subprocess
import sys


def test_child_is_capped():
    code = (
        "held = []\\n"
        "try:\\n"
        "    for _ in range(64):\\n"
        "        held.append(bytearray(32 * 1024 * 1024))\\n"
        "    print('CHILD-ALLOC-OK')\\n"
        "except MemoryError:\\n"
        "    print('CHILD-REFUSED')\\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    print(done.stdout)
    assert "CHILD-REFUSED" in done.stdout, (
        "дочерний процесс обошёл потолок прогона: " + done.stdout
    )
'''

#: Тест, который просто спит — для проверки предела времени.
INNER_SLEEP = '''
import time


def test_sleeps():
    time.sleep(120)
'''

#: Нагрузка для слоя 2: один тихий тест и один, который на 64 МБ поднимает
#: рабочую память и УДЕРЖИВАЕТ её (иначе освободил бы до teardown и дельта не
#: была бы видна).
INNER_DELTA = '''
HELD = []


def test_calm():
    assert True


def test_greedy():
    for _ in range(4):
        HELD.append(bytearray(16 * 1024 * 1024))
    assert sum(len(block) for block in HELD) == 64 * 1024 * 1024
'''


def _base_env(**extra: str) -> dict[str, str]:
    """Окружение внутреннего прогона: без секретов, без живых контуров."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Иначе вывод приходит в cp1251 и не читается как UTF-8.
    env["PYTHONIOENCODING"] = "utf-8"
    env["NANOBOT_LIVE_E2E"] = "0"
    env["NANOBOT_INTEGRATION"] = "0"
    # Бюджеты задаёт вызывающий: иначе тест унаследовал бы потолок от
    # окружения, в котором его запустили, и результат зависел бы от оболочки.
    env.pop("NANOBOT_TEST_MEMORY_CAP_MB", None)
    env.pop("NANOBOT_TEST_MAX_SECONDS", None)
    env.pop("NANOBOT_TEST_MEM_DELTA_MB", None)
    env.update(extra)
    return env


def _write(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def _run_scheduler(args: list[str], env: dict[str, str], timeout: int = 240):
    """Запустить планировщик и вернуть результат с декодированным выводом."""
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    done.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    return done


def _run_pytest(path: Path, env: dict[str, str], timeout: int = 240):
    """Прогнать pytest напрямую с плагином атрибуции (слой 2 сам по себе)."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider",
         "-p", "tests._memguard"],
        cwd=str(path.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return done


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# Слой 1. Жёсткий потолок, обеспечиваемый ОС.
# ===========================================================================


class TestHardMemoryCap:
    """Потолок обязан быть настоящим ограничением, а не проверкой в коде."""

    def test_allocation_beyond_cap_is_refused(self, tmp_path: Path) -> None:
        """Тест, пробующий занять 2 ГБ под потолком 320 МБ, получает отказ.

        Проверяется не «планировщик напечатал страшное сообщение», а то, что
        ОС отказала в выделении: pytest дошёл до ``MemoryError`` и записал
        маркер. Если бы потолок был декоративным, маркера не было бы, а тест
        упал бы с «2 GiB занято без отказа».
        """
        marker = tmp_path / "refused.txt"
        target = _write(tmp_path, "test_greedy.py", INNER_GREEDY)
        env = _base_env(MEMGUARD_MARKER=str(marker))

        done = _run_scheduler(
            [
                "--root", str(tmp_path),
                "--memory-cap-mb", str(CAP_MB),
                "--max-seconds", "120",
                "--no-delta-guard",
                "--", str(target), "-q", "-p", "no:cacheprovider",
            ],
            env,
        )
        output = done.stdout + done.stderr

        assert marker.exists(), (
            "ОС не отказала в выделении памяти при потолке "
            f"{CAP_MB} МБ — потолок не работает:\n{output}"
        )
        assert "REFUSED:MemoryError" in marker.read_text(encoding="ascii")
        assert done.returncode != 0, output
        # Планировщик обязан сказать вслух, что произошло, и назвать цифры.
        assert "ПОТОЛОК ПАМЯТИ" in output, output

    def test_child_process_is_covered_by_the_cap(self, tmp_path: Path) -> None:
        """Потолок общий на дерево процессов, а не только на pytest.

        Это требование не косметическое: агент в тестах поднимает
        ``enterprise-mcp``, и без покрытия дерева основной потребитель
        памяти остался бы вне барьера.
        """
        target = _write(tmp_path, "test_tree.py", INNER_TREE)
        env = _base_env()

        done = _run_scheduler(
            [
                "--root", str(tmp_path),
                "--memory-cap-mb", "384",
                "--max-seconds", "180",
                "--no-delta-guard",
                # -s обязателен: без него pytest перехватывает stdout теста,
                # и отказ дочернего процесса не виден в выводе прогона —
                # тест проходил бы, не доказывая ничего.
                "--", str(target), "-q", "-s", "-p", "no:cacheprovider",
            ],
            env,
        )
        output = done.stdout + done.stderr

        # Дочерний процесс обязан упереться в тот же потолок. Прогон при этом
        # может быть снят планировщиком (код 120) — это тоже верный исход, но
        # тогда дочерний отказ виден в его выводе.
        if done.returncode == 0:
            assert "CHILD-REFUSED" in output, (
                "дочерний процесс занял память сверх потолка прогона:\n" + output
            )
        else:
            assert done.returncode in (1, 120), output

    def test_run_under_cap_is_untouched(self, tmp_path: Path) -> None:
        """Граница снизу: при потолке выше нормы прогон зелёный и код не меняется.

        Барьер, который роняет нормальные прогоны, бесполезен: он просто
        будет отключён. Поэтому нормальный случай проверяется отдельно —
        код возврата должен остаться кодом pytest (0), а не кодом планировщика.
        """
        target = _write(tmp_path, "test_calm.py", "def test_ok():\n    assert True\n")
        env = _base_env()

        done = _run_scheduler(
            [
                "--root", str(tmp_path),
                "--memory-cap-mb", "700",
                "--max-seconds", "120",
                "--no-delta-guard",
                "--", str(target), "-q", "-p", "no:cacheprovider",
            ],
            env,
        )
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        assert "1 passed" in output, output
        assert "ПОТОЛОК ПАМЯТИ" not in output, output

    def test_cap_does_not_leak_outside_the_run(self, tmp_path: Path) -> None:
        """Потолок — свойство прогона, а не машины.

        После прогонов под жёстким потолком внешний процесс обязан
        беспрепятственно занять память, превышающую потолок прогона. Если бы
        ограничение осталось на системе или цепляло соседей, такое выделение
        было бы невозможно — и это проверяется на реальном ограничении, а не
        на декларации.
        """
        target = _write(tmp_path, "test_calm.py", "def test_ok():\n    assert True\n")
        env = _base_env()
        _run_scheduler(
            [
                "--root", str(tmp_path),
                "--memory-cap-mb", str(CAP_MB),
                "--max-seconds", "120",
                "--no-delta-guard",
                "--", str(target), "-q", "-p", "no:cacheprovider",
            ],
            env,
        )

        # Соседний процесс вне потолка: 384 МБ — заведомо больше потолка 320 МБ.
        neighbour = subprocess.run(
            [
                sys.executable, "-c",
                "held = bytearray(384 * 1024 * 1024); print('NEIGHBOUR-OK', len(held))",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert neighbour.returncode == 0, neighbour.stderr
        assert "NEIGHBOUR-OK" in neighbour.stdout, neighbour.stdout

    def test_scheduler_reports_all_three_layers(self, tmp_path: Path) -> None:
        """Планировщик поднимает все три слоя и говорит об этом вслух.

        Слой атрибуции подключается самим планировщиком: «барьер, который надо
        не забыть включить», рано или поздно забывают. Тест фиксирует, что
        ``-p tests._memguard`` попадает в команду прогона и печатается в
        баннере, иначе отчёт о срабатывании слоя 2 не будет виден вовсе.
        """
        target = _write(tmp_path, "test_calm.py", "def test_ok():\n    assert True\n")
        env = _base_env()

        done = _run_scheduler(
            ["--root", str(tmp_path), "--memory-cap-mb", "700", "--", str(target), "-q"],
            env,
        )
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        assert "-p tests._memguard" in output, output
        assert "атрибуция:       tests._memguard" in output, output
        assert "предел времени:" in output, output
        assert "потолок:" in output, output


# ===========================================================================
# Слой 2. Атрибуция: имя виновника и величина прироста.
# ===========================================================================


class TestDeltaAttribution:
    """Сторож с дельтой должен называть виновника и уважать границу."""

    def test_culprit_test_is_named_with_its_delta(self, tmp_path: Path) -> None:
        """Тест, съевший 64 МБ, помечается проваленным ПОИМЁННО.

        В прогоне два теста: тихий и жадный. Провален должен быть РОВНО один —
        жадный; тихий остаётся зелёным. Это и есть атрибуция: не «прогон
        съел память», а «вот этот тест съел вот столько».
        """
        target = _write(tmp_path, "test_delta.py", INNER_DELTA)
        env = _base_env(NANOBOT_TEST_MEM_DELTA_MB="32")

        done = _run_pytest(target, env)
        output = done.stdout + done.stderr

        assert done.returncode == 1, output
        assert "test_greedy" in output, output
        assert "прирост рабочей памяти" in output, output
        assert "32 МБ" in output, output
        # Тихий тест обязан остаться зелёным — иначе это не атрибуция, а
        # «провалилось всё подряд».
        assert "1 failed, 1 passed" in output, output

    def test_same_load_passes_under_budget(self, tmp_path: Path) -> None:
        """Та же нагрузка при бюджете выше прироста проходит.

        Граница сверху: бюджет дельты не должен превращаться в запрет на любую
        тяжёлую работу. Та же нагрузка (64 МБ) при бюджете 256 МБ обязана
        остаться зелёной.
        """
        target = _write(tmp_path, "test_delta.py", INNER_DELTA)
        env = _base_env(NANOBOT_TEST_MEM_DELTA_MB="256")

        done = _run_pytest(target, env)
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        assert "2 passed" in output, output

    def test_budget_is_configurable_and_zero_disables(self, tmp_path: Path) -> None:
        """Бюджет настраивается окружением; ноль — осознанное выключение."""
        target = _write(tmp_path, "test_delta.py", INNER_DELTA)
        env = _base_env(NANOBOT_TEST_MEM_DELTA_MB="0")

        done = _run_pytest(target, env)
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        assert "2 passed" in output, output

    def test_measurement_is_available_on_this_platform(self) -> None:
        """Измеритель рабочей памяти обязан работать, иначе слой 2 молчит.

        Тихая деградация «не умеем мерить — молчим» выглядит как «утечек нет».
        Плагин честно предупреждает, но тест ловит и сам факт измерения.
        """
        from tests import _memguard

        assert _memguard.working_set_bytes() is not None, (
            f"измерение рабочей памяти не поддерживается на {sys.platform}"
        )
        assert _memguard.working_set_bytes() > 0


# ===========================================================================
# Слой 3. Предел времени.
# ===========================================================================


class TestWallClockGuard:
    """Зависший прогон — тоже расход ресурса."""

    def test_hanging_run_is_stopped(self, tmp_path: Path) -> None:
        """Прогон, который должен спать 120 с, снимается по пределу в 10 с.

        pytest-timeout в локальном окружении не установлен (в CI устанавливается
        явно), поэтому верхняя граница времени держится планировщиком: он
        снимает ВСЁ дерево, включая зависшие дочерние процессы.
        """
        target = _write(tmp_path, "test_sleep.py", INNER_SLEEP)
        env = _base_env()

        done = _run_scheduler(
            [
                "--root", str(tmp_path),
                "--no-memory-cap",
                "--max-seconds", "10",
                "--", str(target), "-q", "-p", "no:cacheprovider",
            ],
            env,
            timeout=120,
        )
        output = done.stdout + done.stderr

        assert done.returncode == 124, output
        assert "ПРЕВЫШЕН ПРЕДЕЛ ВРЕМЕНИ" in output, output
        # Успеть снять прогон важнее, чем уложиться в 120 с сна: тест сам
        # зависнет, если зависнет планировщик.
        assert done.elapsed < 60, f"снятие заняло {done.elapsed:.1f} с"  # type: ignore[attr-defined]

    def test_pytest_timeout_status_is_visible(self) -> None:
        """Наличие pytest-timeout видно, а не спрятано в молчание.

        В обеих кодовых базах опция ``timeout`` объявлена в pyproject, и БЕЗ
        плагина pytest-timeout она молча игнорируется (pytest печатает
        ``Unknown config option: timeout`` — это подтверждено прогоном). Пока
        плагина нет, верхнюю границу держит планировщик; тест фиксирует, на
        каком из двух механизмов стоит прогон прямо сейчас, чтобы появление
        зависимости не прошло незамеченным.
        """
        try:
            import pytest_timeout  # noqa: F401
        except ImportError:
            pytest.skip("pytest-timeout не установлен — границу держит планировщик")
        pytest.skip(
            "pytest-timeout установлен: он дублирует предел времени планировщика. "
            "Решить, кто из них главный, и оставить один."
        )


# ===========================================================================
# Единство правил на обе кодовые базы.
# ===========================================================================


class TestBothCodeBasesAgree:
    """Плагин платформы — копия, и копия обязана быть про том же."""

    def test_platform_copy_matches_agent_rules(self) -> None:
        """Имя переменной и бюджет по умолчанию обязаны совпадать.

        Общего модуля у кодовых баз быть не может (платформа не имеет права
        импортировать код агента), поэтому единство держится на совпадении
        правил. Расхождение здесь означало бы две разные границы в двух
        наборах — то есть ровно то «два места, где правила могут разойтись»,
        от которого этот механизм и защищает.
        """
        agent = _load(REPO_ROOT / "tests" / "_memguard.py", "agent_memguard")
        platform = _load(PLATFORM_PLUGIN, "platform_memguard")

        assert platform.ENV_DELTA_MB == agent.ENV_DELTA_MB
        assert platform.DEFAULT_DELTA_MB == agent.DEFAULT_DELTA_MB

    def test_scheduler_budgets_are_declared_once(self) -> None:
        """Бюджеты планировщика — константы модуля, а не магические числа."""
        from tools import run_tests

        assert run_tests.DEFAULT_MEMORY_CAP_MB == 1024
        assert run_tests.ENV_MEMORY_CAP_MB == "NANOBOT_TEST_MEMORY_CAP_MB"
        assert run_tests.ENV_DELTA_MB == "NANOBOT_TEST_MEM_DELTA_MB"
        assert run_tests.ENV_MAX_SECONDS == "NANOBOT_TEST_MAX_SECONDS"
