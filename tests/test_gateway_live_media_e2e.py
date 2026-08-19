"""Live e2e: реальный gateway + живой Postgres + живой LLM.

Опт-ин прогон (в обычном ``pytest`` — skip). Включается через env
``NANOBOT_LIVE_E2E=1`` (иначе тест ничего не запускает и не пишет в БД).

Что делаем:
  1. ``ApplicationContext.create(...)`` — реальная сборка сервисов ровно как
     в ``gateway.py`` (конфиг, агент, авто-скан хуков ``workspace/hooks/*``
     включая ``SessionFileRedirectHook`` + ``RecentFilesHook`` +
     ``RuntimePatcher``, ``agent.run``).
  2. ``ChannelFactory`` → ``PostgresChannel`` на **изолированной** таблице
     ``public.agent_conversation_messages_e2e`` в той же живой БД — чтобы
     не трогать боевую очередь ``agent_conversation_messages``.
  3. Прогоняем набор реалистичных пользовательских сценариев: создание
     одного файла, нескольких файлов, файла в подпапке, Python-скрипта,
     HTML-отчёта, ответа с текстом и вложением, edit-and-reattach.
  4. Живой LLM пишет файлы → ``SessionFileRedirectHook`` редиректит в
     сессионную папку → ``message(media=[...])`` → хук подменяет путь на
     реальный файл сессии → ``MessageTool._resolve_media`` → ``serialize``
     → реальный ``PostgresChannel.send`` пишет в БД.
  5. Поллим изолированную таблицу до answer-строки со статусом
     ``completed`` и проверяем: ``media`` дошла корректно (``mime_type``
     непустой, ``file_size > 0``, ``file_id`` начинается с ``data:``).

Проверка падает ДО фикса (файл не находится → ``mime_type``/``file_size``
пустые) и проходит ПОСЛЕ — это доказательство, что исправление работает
через весь реальный продакшн-путь.

``enable_db_logging``/``enable_audit`` отключены намеренно: путь media через
них НЕ идёт, а их включение тянет фоновые треды (DuckDB/аудит) и лишние
живые таблицы, что мешает однозначности диагностики.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import psycopg2
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _PROJECT_ROOT / "workspace"
for _p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_E2E_TABLE = "agent_conversation_messages_e2e"

# Дождёмся ответа до 5 минут: на live-LLM время генерации непредсказуемо.
_MAX_WAIT_S = 300
_POLL_S = 2.0


def _resolve_dsn(ctx) -> str:
    from lib.core.application_context import ApplicationContext

    assert isinstance(ctx, ApplicationContext)
    pg = ctx.config_service.settings_section("channels").get("postgres", {})
    dsn = pg.get("dsn", "")
    assert dsn, "channels.postgres.dsn не задан — нет live-БД для теста"
    return dsn


def _fetch_answer_rows(dsn: str, chat_id: str) -> list[dict[str, Any]]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status, content, media
                FROM public.{_E2E_TABLE}
                WHERE chat_id = %s AND role = 'assistant'
                ORDER BY created_at DESC
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for status, content, media in rows:
        if isinstance(media, str):
            media = json.loads(media) if media else []
        out.append({"status": status, "content": content, "media": media or []})
    return out


def _valid_entries(media: list[Any]) -> list[dict[str, Any]]:
    return [
        m for m in media
        if isinstance(m, dict)
        and m.get("mime_type")
        and m.get("file_size", 0) > 0
        and str(m.get("file_id", "")).startswith("data:")
    ]


@pytest.fixture()
def live_required():
    """Skip, если live-прогон не включён явно (env NANOBOT_LIVE_E2E=1)."""
    if os.environ.get("NANOBOT_LIVE_E2E", "") != "1":
        pytest.skip(
            "Live e2e выключен. Задайте NANOBOT_LIVE_E2E=1, чтобы гонять "
            "реальный gateway + живую БД + живой LLM."
        )


@pytest.fixture()
async def live_env(live_required):
    """Поднять реальный ApplicationContext + PostgresChannel на E2E-таблице.

    Один раз на весь модуль: один поднятый gateway прогоняет несколько
    сценариев (по одному чату на сценарий, чтобы не мешать друг другу).
    Teardown гарантируется даже при падении (try/finally):
      — останавливаем agent task, channels, ctx;
      — удаляем все строки всех тестовых chat_id из изолированной таблицы.
    """
    from lib.channels.postgres_channel import PostgresChannel
    from lib.core.application_context import ApplicationContext
    from lib.services.channel_factory import ChannelFactory

    ctx = ApplicationContext.create(
        script_dir=_PROJECT_ROOT,
        workspace_dir=_WORKSPACE,
        enable_db_logging=False,
        enable_audit=False,
    )
    dsn = _resolve_dsn(ctx)

    pg_channel: PostgresChannel | None = None
    channels = None
    agent_task: asyncio.Task | None = None
    channels_task: asyncio.Task | None = None
    chat_ids: set[str] = set()

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{_E2E_TABLE} (
                LIKE public.agent_conversation_messages
                INCLUDING DEFAULTS
            )
            """
        )
    conn.close()

    # Встроенный websocket/webui-канал по умолчанию включён и пытается занять
    # порт 8765, который может держать реальный gateway. В тесте он не нужен —
    # отключаем. Остальное (postgres-канал, хуки, агент, LLM, БД) реальное.
    try:
        import types

        object.__setattr__(
            ctx.config.channels, "websocket",
            types.SimpleNamespace(enabled=False),
        )
    except Exception:
        pass

    try:
        ctx.start()

        channel_factory = ChannelFactory(transcription=ctx.transcription_service)
        channels, _messages = channel_factory.create_all(
            ctx.config, ctx.settings, ctx.bus, ctx.session_manager,
        )

        pg_cfg = {
            "enabled": True,
            "dsn": dsn,
            "schema": "public",
            "table_name": _E2E_TABLE,
            "poll_interval": 1.0,
            "flush_interval": 1.0,
            "max_concurrent": 1,
            "processing_timeout": 120,
            "allow_from": ["*"],
        }
        pg_channel = PostgresChannel(pg_cfg, ctx.bus)
        pg_channel.send_progress = ctx.config.channels.send_progress
        pg_channel.send_tool_hints = ctx.config.channels.send_tool_hints
        pg_channel.show_reasoning = ctx.config.channels.show_reasoning
        channels.channels["postgres"] = pg_channel

        channels_task = asyncio.create_task(channels.start_all())
        agent_task = asyncio.create_task(ctx.agent.run())

        def _insert(chat_id: str, content: str) -> str:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO public.{_E2E_TABLE}
                            (chat_id, user_id, role, content, status)
                        VALUES (%s, %s, 'user', %s, 'pending')
                        RETURNING id
                        """,
                        (chat_id, chat_id, content),
                    )
                    return str(cur.fetchone()[0])
            finally:
                conn.close()

        async def send_and_wait(
            prompt: str,
            *,
            min_total: int = 1,
            min_valid: int = 1,
            require_text: bool = False,
            max_wait_s: int = _MAX_WAIT_S,
            nudge_on_empty: bool = True,
        ) -> dict[str, Any]:
            """Вставить user-сообщение в изолированную очередь, дождаться
            completed-ответа и проверить media.

            Если первый ответ completed, но media пуст или невалиден — один
            раз отправляем follow-up «прикрепи файл» в тот же чат и ждём
            следующий completed-ответ. Это снимает флаки LLM, который
            иногда игнорирует явную просьбу приложить вложение.
            """
            chat_id = f"live-e2e-{uuid.uuid4().hex}"
            chat_ids.add(chat_id)
            _insert(chat_id, prompt)

            async def _wait_for_completed(deadline_s: float) -> dict[str, Any] | None:
                while time.monotonic() < deadline_s:
                    for row in await asyncio.to_thread(_fetch_answer_rows, dsn, chat_id):
                        if row["status"] == "completed":
                            return row
                    await asyncio.sleep(_POLL_S)
                return None

            deadline = time.monotonic() + max_wait_s
            answer = await _wait_for_completed(deadline)
            assert answer is not None, (
                f"Агент не прислал completed-ответ за {max_wait_s}с "
                f"(chat_id={chat_id}) на запрос: {prompt[:120]!r}"
            )

            def _is_acceptable(a: dict[str, Any]) -> bool:
                if require_text and not (a["content"] or "").strip():
                    return False
                if len(a["media"]) < min_total:
                    return False
                if len(_valid_entries(a["media"])) < min_valid:
                    return False
                return True

            if not _is_acceptable(answer) and nudge_on_empty and time.monotonic() < deadline:
                _insert(
                    chat_id,
                    "Я не вижу вложения в твоём ответе. Пожалуйста, "
                    "перешли нужный файл как вложение через параметр media "
                    "(data:URL или путь к файлу). Это обязательно.",
                )
                deadline = time.monotonic() + max_wait_s
                nudged = await _wait_for_completed(deadline)
                if nudged is not None:
                    answer = nudged

            if require_text:
                assert (answer["content"] or "").strip(), (
                    "Ожидался непустой текст ответа, пришёл пустой. "
                    f"content={answer['content']!r}"
                )
            assert len(answer["media"]) >= min_total, (
                f"Ожидалось >= {min_total} media, получено "
                f"{len(answer['media'])}.\ncontent={answer['content']!r}\n"
                f"media={answer['media']!r}"
            )
            valid = _valid_entries(answer["media"])
            assert len(valid) >= min_valid, (
                f"Ожидалось >= {min_valid} валидных вложений "
                "(mime_type/file_size/data:), получили "
                f"{len(valid)}.\nmedia={answer['media']!r}"
            )
            return answer

        yield {"send_and_wait": send_and_wait}
    finally:
        # --- teardown ---
        if agent_task is not None:
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task
        try:
            ctx.agent.stop()
        except Exception:
            pass
        if channels is not None and channels_task is not None:
            try:
                await channels.stop_all()
            except Exception:
                pass
            with suppress(asyncio.CancelledError):
                await channels_task
        try:
            ctx.stop()
        except Exception:
            pass
        if chat_ids:
            try:
                conn = psycopg2.connect(dsn)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM public.{_E2E_TABLE} "
                        f"WHERE chat_id = ANY(%s)",
                        (list(chat_ids),),
                    )
                conn.close()
            except Exception:
                pass


_SCENARIOS = [
    pytest.param(
        "report.md",
        "Создай ровно один текстовый файл report.md с коротким отчётом "
        "(3-5 строк) и пришли его мне как вложение. В финальном сообщении "
        "оставь только слово «Готово».",
        {"min_total": 1, "min_valid": 1},
        id="single_markdown_file",
    ),
    pytest.param(
        "summary.md, script.py",
        "Создай два файла — summary.md (короткое резюме) и script.py "
        "(полезный мини-скрипт на Python, не пустой) — и приложи оба "
        "в ответе одним вложением (media со списком файлов). "
        "Текст финального сообщения сделай коротким.",
        {"min_total": 2, "min_valid": 2},
        id="two_files_md_and_py",
    ),
    pytest.param(
        "docs/intro.md",
        "Создай файл docs/intro.md (внутри подпапки docs/) с вводным "
        "абзацем и приложи его в ответе как вложение. Финальный текст "
        "короткий.",
        {"min_total": 1, "min_valid": 1},
        id="file_in_subfolder",
    ),
    pytest.param(
        "report.html",
        "Сгенерируй краткий HTML-отчёт report.html с заголовком и "
        "одним разделом (несколько строк) и приложи его как вложение. "
        "В финальном сообщении коротко опиши результат.",
        {"min_total": 1, "min_valid": 1, "require_text": True},
        id="html_report_with_text_reply",
    ),
    pytest.param(
        "notes.md (edit + reattach)",
        "Создай файл notes.md с парой пунктов, потом ОБНОВИ его "
        "(допиши ещё один пункт в конец) и приложи обновлённый файл "
        "как вложение. Текст финального сообщения короткий.",
        {"min_total": 1, "min_valid": 1},
        id="edit_and_reattach",
    ),
    pytest.param(
        "config.json",
        "Создай валидный JSON-файл config.json с парой ключей и приложи "
        "его как вложение. В финальном сообщении кратко скажи, что "
        "конфиг готов.",
        {"min_total": 1, "min_valid": 1, "require_text": True},
        id="json_config_with_text_reply",
    ),
]


@pytest.mark.parametrize(
    "label, prompt, expect",
    _SCENARIOS,
)
@pytest.mark.asyncio
async def test_live_gateway_media_scenarios(live_env, label, prompt, expect):
    """Реалистичный live-сценарий: агент создаёт файл(ы) и прикладывает их.

    Параметризовано набором типовых ситуаций (одиночный файл, несколько,
    подпапка, HTML, edit+reattach, JSON). Все пишут в живой Postgres через
    реальный gateway — media должна доходить до БД с валидным mime/size.
    """
    answer = await live_env["send_and_wait"](
        prompt,
        min_total=expect["min_total"],
        min_valid=expect["min_valid"],
        require_text=expect.get("require_text", False),
    )
    valid = _valid_entries(answer["media"])
    # Доп. мягкая проверка: ни одно из media не должно быть «пустым битым»
    # (mime пустой И file_size == 0) — это и есть симптом до фикса.
    broken = [
        m for m in answer["media"]
        if isinstance(m, dict)
        and not m.get("mime_type")
        and m.get("file_size", 0) == 0
    ]
    assert not broken, (
        f"Сценарий [{label}]: есть битые вложения (mime пустой, size=0) — "
        "признак того, что файл НЕ найден. Это симптом ДО фикса. "
        f"media={answer['media']!r}"
    )
    # Удобный диагностический print для ручного просмотра при прогоне.
    filenames = [
        m.get("filename") for m in valid if isinstance(m, dict)
    ]
    print(
        f"\n[scenario={label}] valid media={len(valid)}/{len(answer['media'])} "
        f"filenames={filenames} content_len={len(answer['content'] or '')}"
    )