"""Регрессия на flush RUNNING-маркера из cli.py.

Тест проверяет ровно одно свойство: маркер ``status=running`` должен попасть в
stdout subprocess **до** того, как завершится долгая операция. Без
``flush=True`` на ``print(...)`` stdout subprocess буферизуется, и наблюдатель
(агент) не увидит RUNNING, пока процесс не выйдет и Python не сбросит буфер.

**Это интеграционный тест, и он ходит к живому провайдеру LLM.**

История, важная для следующего читателя. Тест изначально подменял работу скилла
стабом ``summarizer.py``. Скил давно разнесён по пакетам
(``llm/``, ``application/``, ``chunking/``, ``output/``, ``document/``), модуля
``summarizer`` в нём больше нет, а ``cli.py`` импортирует
``from llm.config import ...`` и ``from application.service import ...``. Стаб
подставлялся в ``sys.path``, но его никто не импортировал: ``import summarizer``
проходил, модуль оседал в кэше импортов и не использовался ни одним
``import``. Тест молча выполнял настоящий скил с настоящим вызовом LLM и
зависел от сети — при живом провайдере он был зелёным, при недоступном
падал, и ничто в его имени или коде об этом не говорило.

Мёртвый стаб удалён. Живой прогон не удалён, а сделан **явной опцией**:
``NANOBOT_LEGAL_SUMMARIZER_LIVE_TESTS=1``. Причина именно в opt-in, а не в
мягком skip по наличию провайдера: в полном прогоне к этому моменту
``SETTINGS`` уже инициализирован предыдущими тестами, конфигурация доступна,
и «мягкая» проверка решила бы, что провайдер есть — то есть приёмка получала
результат, зависящий от лимитов и доступности внешнего сервиса.

Отдельный герметичный тест ``TestFlushContract.test_emit_uses_flush`` закрывает
контракт flush без процесса и без сети, поэтому выключенный по умолчанию
интеграционный прогон не оставляет регрессию непокрытой.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLI = _REPO_ROOT / "workspace" / "skills" / "legal_summarizer" / "scripts" / "cli.py"

#: Переменные, которые уводят дочерний CLI в сеть/БД. В полном прогоне они
#: уже выставлены предыдущими тестами (``SessionStorageService`` пишет
#: resolved DSN в ``os.environ``, конфиг экспортирует ключи), и ребёнок
#: начинает подключаться к БД вместо того, чтобы просто печатать в stdout.
_CHILD_ENV_DENYLIST = frozenset(
    {
        "DATABASE_URL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "EMBED_TOKEN",
        "OLLAMA_URL",
        "NANOBOT_PROFILE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)

#: Потолок ожидания маркера. В норме тест укладывается в секунды; значение
#: большое, потому что при живом провайдере сеть может висеть долго.
_MARKER_DEADLINE_SEC = 120.0

#: Оптический выключатель живого прогона. Тест ходит к настоящему провайдеру
#: и по умолчанию выключен: в полном прогоне к этому моменту ``SETTINGS`` уже
#: инициализирован, конфигурация доступна, и тест молча уходил в сеть — то
#: есть приёмка получала результат, зависящий от лимитов и доступности
#: внешнего сервиса. Отсутствие провайдера проверялось бы «мягко» (skip) и не
#: отражало бы, что в этом прогоне он есть.
_LIVE_ENV_FLAG = "NANOBOT_LEGAL_SUMMARIZER_LIVE_TESTS"


def _live_disabled() -> bool:
    """Нужен ли явный opt-in для прогона против живого провайдера."""
    return os.environ.get(_LIVE_ENV_FLAG, "").strip() not in {"1", "true", "yes"}


def _make_doc(tmp_path: Path) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text("Это тестовый документ. " * 200, encoding="utf-8")
    return p


class TestFlushContract:
    """Герметичная часть: контракт flush проверяется без процесса и без сети."""

    def test_emit_uses_flush(self) -> None:
        """Каждый ``print`` в пути выдачи маркера обязан заканчиваться ``flush=True``.

        Проверяется исходник, а не поведение, — сознательно: единственная
        альтернатива потребовала бы живого провайдера, и регрессия «убрали
        ``flush=True``» ждала бы своей недели, пока сеть жива.
        """
        source = _CLI.read_text(encoding="utf-8")
        assert "def _emit" in source, "в cli.py нет функции выдачи маркера"

        emit_body = source[source.index("def _emit") :]
        # Тело _emit — до следующей функции верхнего уровня.
        next_def = emit_body.find("\ndef ", 1)
        if next_def != -1:
            emit_body = emit_body[:next_def]

        prints = re.findall(r"print\((?:[^()]|\([^()]*\))*\)", emit_body)
        assert prints, "в _emit нет ни одного print"
        for statement in prints:
            assert "flush=True" in statement, (
                f"print в _emit без flush=True: {statement!r}. Без него stdout "
                "subprocess буферизуется, и RUNNING-маркер дойдёт только после "
                "завершения операции"
            )


@pytest.mark.skipif(
    _live_disabled(),
    reason=(
        "интеграционный тест против живого провайдера LLM, по умолчанию "
        "выключен: результат приёмки не должен зависеть от лимитов и "
        "доступности внешнего сервиса. Включить: NANOBOT_LEGAL_SUMMARIZER_LIVE_TESTS=1. "
        "Контракт flush без сети закрыт TestFlushContract.test_emit_uses_flush"
    ),
)
def test_running_marker_arrives_before_run_completes(tmp_path):
    """RUNNING-маркер должен попасть в stdout до завершения долгой операции.

    Чтение stdout — построчное через ``readline``: блок ``read(1024)`` хрупок
    к packetization/buffering на разных платформах, построчно стабильнее.
    ``cli._emit()`` печатает multi-line JSON, поэтому строки копятся в буфер и
    разбираются ``raw_decode`` с любой непробельной позиции.
    """
    doc = _make_doc(tmp_path)

    bootstrap = textwrap.dedent(
        f"""
        import runpy
        runpy.run_path({str(_CLI)!r}, run_name="__main__")
        """
    )

    child_env = {
        k: v for k, v in os.environ.items() if k not in _CHILD_ENV_DENYLIST
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            bootstrap,
            "--file",
            str(doc),
            "--length",
            "brief",
            "--confirm",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
        cwd=str(_CLI.parent),
    )

    assert proc.stdout is not None

    started = time.monotonic()
    first_marker_at: float | None = None
    proc_alive_when_running: bool | None = None
    deadline = started + _MARKER_DEADLINE_SEC
    decoder = json.JSONDecoder()
    buffer = ""

    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        buffer += line
        stripped = buffer.lstrip()
        if not stripped:
            buffer = ""
            continue
        try:
            obj, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            continue
        buffer = stripped[end:]
        if not isinstance(obj, dict):
            continue
        if obj.get("status") == "running" and first_marker_at is None:
            first_marker_at = time.monotonic() - started
            proc_alive_when_running = proc.poll() is None
            break
        if obj.get("status") == "completed":
            break

    proc.poll()

    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        proc.wait(timeout=30)
    child_err = (proc.stderr.read() if proc.stderr else "")[-2000:]

    assert first_marker_at is not None, (
        "RUNNING-маркер не пришёл в stdout за "
        f"{_MARKER_DEADLINE_SEC:.0f} сек. "
        f"returncode={proc.returncode}\n--- child stderr ---\n{child_err}"
    )
    assert proc_alive_when_running, (
        "RUNNING пришёл ТОЛЬКО ПОСЛЕ завершения процесса — stdout "
        "буферизуется, контракт долгой операции нарушен.\n"
        f"returncode={proc.returncode}\n--- child stderr ---\n{child_err}"
    )
    assert proc.returncode == 0, (
        f"returncode={proc.returncode}\n--- child stderr ---\n{child_err}"
    )
