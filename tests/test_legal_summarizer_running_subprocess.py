"""Регрессия на flush RUNNING-маркера из cli.py.

План: 4 бага legal_summarizer / шаг 1.

Запускаем cli.py как настоящий subprocess с подменой ``summarizer``
через bootstrap-скрипт (добавляет stub-модуль в ``sys.modules`` до
``runpy.run_path(cli.py)``). Stub ``run()`` спит 1.5 сек — за это время
subprocess должен успеть напечатать ``status=running`` в stdout.

Без ``flush=True`` (см. fix ``_emit/_emit_done``) этот тест красный —
RUNNING приходит только после завершения stub-run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLI = _REPO_ROOT / "workspace" / "skills" / "legal_summarizer" / "scripts" / "cli.py"


_STUB_SUMMARIZER = textwrap.dedent(
    """
    import time as _time

    def get_chunking_config():
        return {"chunk_size": 100000}

    def get_execution_config():
        return {"estimated_chunk_duration_sec": 20,
                "confirmation_threshold_sec": 120,
                "max_chunks_for_execution": 50}

    def run(text, **kwargs):
        # Имитируем долгий LLM-вызов, чтобы увидеть, успеет ли
        # RUNNING-маркер выйти до того, как мы вернём результат.
        # Спим в stderr — наблюдатель может прочитать момент старта.
        import sys as _sys
        print(f"[STUB] run_started t={_time.monotonic():.3f}", file=_sys.stderr, flush=True)
        _time.sleep(5.0)
        print(f"[STUB] run_finished t={_time.monotonic():.3f}", file=_sys.stderr, flush=True)
        return {
            "status": "completed",
            "operation_id": "op_stub",
            "result": {
                "subject": "stub subject",
                "summary": "stub summary",
                "length": "brief",
                "chars_in": len(text),
                "chunks": 1,
                "context_batches": 0,
                "sections": 0,
                "strategy": "single",
                "title": None,
            },
            "stats": {
                "chars_in": len(text),
                "chunks": 1,
                "context_batches_total": 0,
                "sections_total": 0,
                "meaningful_sections": 0,
                "article_count": 0,
                "map_calls": 0,
                "section_reduce_calls": 0,
                "section_trim_calls": 0,
                "document_reduce_calls": 0,
                "reduce_calls": 0,
                "total_llm_calls": 0,
                "retries": 0,
                "failed_batches": [],
                "partial": False,
                "duration_sec": 0.0,
                "strategy": "single",
            },
        }

    def inspect(text, **kwargs):
        return type("I", (), {
            "chars_in": len(text),
            "chunks": [],
            "context_batches": [],
            "tree": None,
            "strategy": "single",
            "estimated_llm_calls": 1,
        })()

    def estimate(insp):
        from dataclasses import dataclass
        @dataclass
        class _Est:
            chunks_count: int = 0
            context_batches: int = 0
            estimated_llm_calls: int = 1
            estimated_duration_min_sec: float = 0.0
            estimated_duration_max_sec: float = 0.0
            confirmation_threshold_sec: float = 120.0
        return _Est()

    def needs_confirmation(est):
        return False

    def quick_estimate(path):
        return {"chars_in": 0, "estimate": estimate(None)}

    def load_text(path, *, mode="full"):
        return "stub text for the test"

    def make_operation_id(text, length):
        return "op_stub"

    def _progress(msg):
        pass

    def _extract_subject(text):
        return "stub"

    def _strip_think_blocks(text):
        return text

    def _load_prompt(name):
        return "stub system prompt"

    _LENGTH_INSTRUCTIONS = {"brief": "b", "detailed": "d"}
    _QUESTION_INSTRUCTION_TEMPLATE = ""
    def _system_instruction(length, question):
        return ""
    """
)


def _write_stub_dir(tmp_path: Path) -> Path:
    """Создать временную папку с подменным ``summarizer.py``."""
    stub_dir = tmp_path / "_stubs"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "summarizer.py").write_text(_STUB_SUMMARIZER, encoding="utf-8")
    return stub_dir


def _make_doc(tmp_path: Path) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text("Это тестовый документ. " * 200, encoding="utf-8")
    return p


def test_running_marker_arrives_before_run_completes(tmp_path):
    """RUNNING-маркер должен попасть в stdout до завершения run().

    Без flush=True на ``print(...)`` stdout subprocess буферизуется —
    агент (или другой наблюдатель) не увидит RUNNING, пока процесс не
    завершится и Python не сбросит буфер при exit.
    """
    doc = _make_doc(tmp_path)
    stubs = _write_stub_dir(tmp_path)

    bootstrap = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(stubs)!r})
        import summarizer  # noqa: F401  (регистрируем в sys.modules)
        import runpy
        runpy.run_path({str(_CLI)!r}, run_name="__main__")
        """
    )

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
        env={**os.environ},
        cwd=str(_CLI.parent),
    )

    assert proc.stdout is not None

    started = time.monotonic()
    first_marker_at: float | None = None
    completion_marker_at: float | None = None
    proc_alive_when_running: bool | None = None
    deadline = started + 15.0
    buffer = ""

    while time.monotonic() < deadline:
        chunk = proc.stdout.read(1024)
        if not chunk:
            if proc.poll() is not None:
                break
            continue
        buffer += chunk
        while buffer:
            stripped = buffer.lstrip()
            if not stripped:
                buffer = ""
                break
            decoder = json.JSONDecoder()
            try:
                obj, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                break
            buffer = stripped[end:]
            now = time.monotonic() - started
            if obj.get("status") == "running" and first_marker_at is None:
                first_marker_at = now
                # Запоминаем, жив ли ещё процесс в момент чтения RUNNING.
                proc_alive_when_running = proc.poll() is None
                break
            elif obj.get("status") == "completed" and completion_marker_at is None:
                completion_marker_at = now
        if first_marker_at is not None:
            break

    proc.poll()

    assert first_marker_at is not None, (
        "RUNNING-маркер не пришёл в stdout за 15 сек. "
        f"returncode={proc.returncode}"
    )
    assert proc_alive_when_running, (
        "RUNNING пришёл ТОЛЬКО ПОСЛЕ завершения процесса — stdout "
        "буферизуется, контракт долгой операции нарушен."
    )

    proc.wait(timeout=30)
    assert proc.returncode == 0
