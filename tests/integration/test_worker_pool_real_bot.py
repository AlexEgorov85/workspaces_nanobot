"""Live e2e пула воркеров: реальный ``gateway.py`` subprocess + живой LLM.

Три сценария (opt-in через ``NANOBOT_LIVE_E2E=1`` + ``DATABASE_URL`` +
настроенный ``providers.llm.api_key``):

  1. ``test_real_single_bot_e2e`` — один gateway-процесс обрабатывает
     одно user-сообщение через реальный AgentLoop + LLM; проверяется,
     что assistant-сообщение получило ``status='completed'`` и ``claim``
     снят.
  2. ``test_real_multi_worker_e2e`` — два gateway-процесса с авто-
     сгенерированными ``worker_id`` делят N задач в одной БД; проверяется
     наличие двух разных ``worker_id`` в логах, что все задачи завершены
     и все claims сняты.
  3. ``test_real_kill9_heal`` — после ``kill -9`` gateway во время
     обработки остаётся orphan-claim; имитируем истечение lease
     (``UPDATE claims SET lease_until = NOW() - 1s``) и запускаем
     ``tools/check_worker_pool_integrity.py --fix``: задача должна
     вернуться в ``pending``, claim — удалиться.

Сценарии используют отдельные ``chat_id`` (``e2e_pool_<rand>``) и
``worker_id`` (``e2e_pool_w_<rand>``), чтобы не пересекаться с
продакшен-данными. Тест НЕ дропает прод-таблицы — только чистит
свои ``chat_id`` в ``teardown``.

Стоимость: каждый сценарий — несколько коротких LLM-вызовов
(``temperature=0.1``, ``max_tokens≈200``). Примерно 10-30 секунд и
несколько центов на прогон.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg2
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE = _PROJECT_ROOT / "workspace"
for _p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import SETTINGS  # noqa: E402

_DSN = SETTINGS["channels"]["postgres"]["dsn"]
_GATEWAY = str(_PROJECT_ROOT / "gateway.py")
_TOOL = str(_PROJECT_ROOT / "tools" / "check_worker_pool_integrity.py")
_LOG_DIR = Path(os.environ.get("TEMP", "/tmp")) / "opencode"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _skip_if_not_live() -> None:
    if os.environ.get("NANOBOT_LIVE_E2E") != "1":
        pytest.skip("live e2e: set NANOBOT_LIVE_E2E=1")
    llm = (SETTINGS.get("providers") or {}).get("llm") or {}
    if not llm.get("api_key"):
        pytest.skip("live e2e: providers.llm.api_key не настроен")
    if not _DSN:
        pytest.skip("live e2e: DATABASE_URL не настроен")
    try:
        conn = psycopg2.connect(_DSN, gssencmode="disable")
        conn.close()
    except Exception as exc:
        pytest.skip(f"live e2e: БД недоступна ({exc})")


def _db():
    return psycopg2.connect(_DSN, gssencmode="disable")


def _exec(sql: str, params=(), fetch: bool = True):
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _insert_user(chat: str, content: str) -> str:
    rows = _exec(
        "INSERT INTO public.agent_conversation_messages "
        "(chat_id, user_id, role, content, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
        (chat, "e2e-pool", "user", content, "pending"),
    )
    return rows[0][0]


def _user_status(task_id: str) -> str | None:
    rows = _exec(
        "SELECT status FROM public.agent_conversation_messages "
        "WHERE id = %s AND role = 'user'",
        (task_id,),
    )
    return rows[0][0] if rows else None


def _fetch_assistant(task_id: str):
    return _exec(
        "SELECT id::text, status, content FROM public.agent_conversation_messages "
        "WHERE reply_to = %s AND role = 'assistant'",
        (task_id,),
    )


def _has_claim(task_id: str) -> bool:
    return bool(_exec(
        "SELECT 1 FROM public.agent_worker_claims WHERE task_id = %s",
        (task_id,),
    ))


def _wait_completed(task_ids: list[str], timeout: float) -> dict[str, tuple[str, str]]:
    deadline = time.time() + timeout
    done: dict[str, tuple[str, str]] = {}
    while time.time() < deadline and len(done) < len(task_ids):
        for tid in task_ids:
            if tid in done:
                continue
            row = _fetch_assistant(tid)
            if row and row[0][1] == "completed":
                done[tid] = (row[0][1], row[0][2] or "")
        if len(done) < len(task_ids):
            time.sleep(1.0)
    for tid in task_ids:
        if tid not in done:
            row = _fetch_assistant(tid)
            done[tid] = ((row[0][1] if row else "missing"), (row[0][2] if row else "") or "")
    return done


def _spawn_gateway(tag: str) -> tuple[subprocess.Popen, Path]:
    log_path = _LOG_DIR / f"gateway_pool_{tag}.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, _GATEWAY],
        cwd=str(_PROJECT_ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    return proc, log_path


def _terminate(proc: subprocess.Popen):
    if sys.platform == "win32":
        with __import__("contextlib").suppress(Exception):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=8)
            return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def _kill9(proc: subprocess.Popen):
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _cleanup_chat(chat: str):
    _exec(
        "DELETE FROM public.agent_worker_claims WHERE task_id IN "
        "(SELECT id FROM public.agent_conversation_messages WHERE chat_id = %s)",
        (chat,), fetch=False,
    )
    _exec(
        "DELETE FROM public.agent_conversation_messages WHERE chat_id = %s",
        (chat,), fetch=False,
    )


def _distinct_worker_ids(*paths: Path) -> set[str]:
    wids: set[str] = set()
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        for m in re.finditer(r"worker_id=([A-Za-z0-9_:\-]+)", text):
            wids.add(m.group(1))
    return wids


@pytest.fixture
def chat_id() -> str:
    return f"e2e_pool_{uuid.uuid4().hex[:8]}"


def test_real_single_bot_e2e(chat_id: str):
    """Один gateway-процесс → одно user-сообщение → реальный LLM."""
    _skip_if_not_live()
    try:
        proc, _log = _spawn_gateway("single")
        time.sleep(8.0)
        task_id = _insert_user(chat_id, "Reply with exactly one short English sentence starting with PONG.")
        done = _wait_completed([task_id], timeout=120.0)
        status, content = done[task_id]
        claim = _has_claim(task_id)
        assert status == "completed", f"assistant не завершён: {status!r}"
        assert content, "пустой ответ"
        assert not claim, "claim не снят"
    finally:
        with __import__("contextlib").suppress(Exception):
            _terminate(proc)
        _cleanup_chat(chat_id)


def test_real_multi_worker_e2e(chat_id: str):
    """Два gateway-процесса делят N задач; оба worker_id в логах; всё completed."""
    _skip_if_not_live()
    procs: list[subprocess.Popen] = []
    logs: list[Path] = []
    try:
        g1, l1 = _spawn_gateway("multi_g1")
        g2, l2 = _spawn_gateway("multi_g2")
        procs.extend([g1, g2])
        logs.extend([l1, l2])
        time.sleep(10.0)

        task_ids = [
            _insert_user(chat_id, f"Task {i}: respond with a single word 'ok-{i}'.")
            for i in range(4)
        ]
        done = _wait_completed(task_ids, timeout=180.0)
        n_completed = sum(1 for tid in task_ids if done[tid][0] == "completed")
        n_with_content = sum(1 for tid in task_ids if done[tid][1])
        n_no_claim = sum(1 for tid in task_ids if not _has_claim(tid))

        wids = _distinct_worker_ids(*logs)
        assert n_completed == 4, f"только {n_completed}/4 завершено"
        assert n_with_content == 4, f"только {n_with_content}/4 с контентом"
        assert n_no_claim == 4, f"только {n_no_claim}/4 без claim"
        assert len(wids) >= 2, f"в логах один worker_id: {wids}"
    finally:
        for p in procs:
            with __import__("contextlib").suppress(Exception):
                _terminate(p)
        _cleanup_chat(chat_id)


def test_real_kill9_heal(chat_id: str):
    """kill -9 gateway во время обработки → heal через ``check_worker_pool_integrity --fix``."""
    _skip_if_not_live()
    proc = None
    try:
        proc, _log = _spawn_gateway("kill9")
        time.sleep(8.0)
        task_id = _insert_user(chat_id, "Answer with exactly 'alive'.")
        # Ждём, пока воркер начнёт обработку.
        for _ in range(20):
            time.sleep(1.0)
            if _user_status(task_id) == "processing" and _has_claim(task_id):
                break
        assert _user_status(task_id) == "processing", "task не взят"
        assert _has_claim(task_id), "нет claim"

        _kill9(proc)

        time.sleep(3.0)
        assert _user_status(task_id) == "processing", "после kill статус изменился"
        assert _has_claim(task_id), "после kill claim исчез"

        # Имитируем истечение lease (вместо ожидания processing_timeout=120s).
        _exec(
            "UPDATE public.agent_worker_claims SET lease_until = NOW() - interval '1 second' "
            "WHERE task_id = %s",
            (task_id,), fetch=False,
        )

        result = subprocess.run(
            [sys.executable, _TOOL, "--fix"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert result.returncode in (0, 1), f"check_worker_pool_integrity failed: {result.stderr}"
        assert "исправлены" in result.stdout or "Рассинхроны исправлены" in result.stdout, \
            f"unexpected output: {result.stdout}"

        assert _user_status(task_id) == "pending", "задача не вернулась в pending"
        assert not _has_claim(task_id), "claim не удалён после --fix"
    finally:
        if proc is not None:
            with __import__("contextlib").suppress(Exception):
                _kill9(proc)
        _cleanup_chat(chat_id)
