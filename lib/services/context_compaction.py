"""ContextCompactionService — единая точка записи факта сжатия контекста.

Три входа — один путь записи (заметка в ``agent_conversation_messages``,
loguru INFO, опциональный Rich-вывод в терминал gateway):

  1. **Ручной запуск**: slash-команда ``/compact``
     (``lib/commands/compact_command.py`` + регистрация
     ``RuntimePatcher.patch_compact_command``), CLI-команда ``/compact``
     (``lib/cli/console_loop.py::_run_cli_compact``) или tool агента
     ``compact_context`` (``workspace/tools/compact_context.py``).
     Метод :py:meth:`compact` сам зовёт штатный ``Consolidator`` из
     nanobot 0.3.0 (``maybe_consolidate_by_tokens`` /
     ``compact_idle_session``), замеряет состояние сессии до/после
     и формирует ``report``.

  2. **Авто idle-сжатие** (``AutoCompact._archive``). Обёртка
     ``runtime_patcher._wrap_auto_compact_archive``
     вызывает :py:meth:`record_external_compaction` после успешного
     оригинального ``_archive``, передавая готовые замеры.

  3. **Авто token-budget сжатие**
     (``Consolidator.maybe_consolidate_by_tokens``). Обёртка
     ``runtime_patcher._wrap_maybe_consolidate_by_tokens``
     делает то же: diff ``last_consolidated`` до/после +
     ``record_external_compaction``.

Результат для всех трёх путей одинаков: один ``format_report``,
один ``_write_history_notice``, одна loguru-строка. Пользователь и
логи не различают, было ли сжатие ручным или автоматическим.

Заметка в ``agent_conversation_messages`` видна в UI-чате (Streamlit),
но НЕ попадает в контекст промпта: контекст агента строится из
``PGSessionManager`` (``agent_session_messages``), а таблица обмена —
транспорт показа сообщений.

Импортируется без nanobot: тяжёлые зависимости резолвятся лениво.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _get_setting(settings: Any, *keys: str, default: Any = None) -> Any:
    """Прочитать значение из SETTINGS (dict или объект с атрибутами)."""
    value: Any = settings
    for key in keys:
        try:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = getattr(value, key)
        except (AttributeError, KeyError, TypeError):
            return default
        if value is None:
            return default
    return value


class ContextCompactionService:
    """Единая точка запуска сжатия контекста (tool агента + CLI /compact)."""

    def __init__(self, agent: Any, settings: Any = None) -> None:
        self.agent = agent
        self._settings = settings
        self._section = _get_setting(settings, "gateway", "compact", default={}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self._section.get("enabled", True))

    @property
    def notify_in_history(self) -> bool:
        return bool(self._section.get("notify_in_history", True))

    @property
    def print_to_terminal(self) -> bool:
        return bool(self._section.get("print_to_terminal", False))

    async def compact(
        self,
        session_key: str | None = None,
        *,
        idle: bool = False,
        force: bool = False,
        max_suffix: int = 8,
    ) -> dict:
        """Сжать контекст сессии и вернуть отчёт.

        Args:
            session_key: ключ сессии. ``None`` — берётся из текущего request
                context (когда tool вызван внутри оборота).
            idle: ``True`` — жёсткое idle-сжатие (``compact_idle_session``),
                ``False`` — token-budget сжатие (``maybe_consolidate_by_tokens``).
            force: ``True`` — ручной запуск: жёстко сжать сессию
                (``compact_idle_session``) **независимо от порога токенов**.
                ``idle`` указывать необязательно: ``force`` уже подразумевает
                жёсткое усечение. Используется CLI-командой ``/compact`` и
                tool'ом ``compact_context`` при пустых аргументах.
            max_suffix: сколько последних сообщений оставить при idle-сжатии.

        Returns:
            Словарь-отчёт с полями ``session_key/mode/ok/archived_msgs/
            kept_msgs/tokens_before/tokens_after/summary/raw_dump``.
        """
        if not self.enabled:
            return self._empty("gateway.compact.enabled=false")

        session_key = session_key or self._current_session_key()
        if not session_key:
            return self._empty("Не определён session_key сессии")

        consolidator = getattr(self.agent, "consolidator", None)
        sessions = getattr(self.agent, "sessions", None)
        runtime_for_session = getattr(self.agent, "runtime_for_session", None)
        if consolidator is None or sessions is None or runtime_for_session is None:
            return self._empty("agent.consolidator/sessions/runtime_for_session отсутствуют")

        session = sessions.get_or_create(session_key)
        runtime = runtime_for_session(session)
        if runtime is None:
            return self._empty("runtime_for_session вернул None")

        before_msgs = len(getattr(session, "messages", []) or [])
        before_cursor = int(getattr(session, "last_consolidated", 0) or 0)
        before_tokens, _ = await self._estimate(session, runtime)

        use_idle = bool(idle or force)
        summary: str | None = None
        try:
            if use_idle:
                result = await consolidator.compact_idle_session(
                    session_key, runtime=runtime, max_suffix=max_suffix,
                )
                if result == "":
                    summary = None
                else:
                    summary = result
            else:
                from nanobot.session.manager import replay_max_messages_for_context

                await consolidator.maybe_consolidate_by_tokens(
                    session,
                    runtime=runtime,
                    replay_max_messages=replay_max_messages_for_context(
                        runtime.context_window_tokens
                    ),
                )
        except Exception as exc:
            logger.opt(exception=exc).error(
                "Context compaction failed for {}", session_key,
            )
            return self._empty(f"Сжатие не удалось: {exc}")

        fresh = sessions.get_or_create(session_key)
        after_cursor = int(getattr(fresh, "last_consolidated", 0) or 0)
        after_msgs = len(getattr(fresh, "messages", []) or [])
        after_tokens, _ = await self._estimate(fresh, runtime)

        if use_idle:
            archived = max(0, before_msgs - after_msgs)
        else:
            archived = max(0, after_cursor - before_cursor)

        report = {
            "session_key": session_key,
            "mode": "idle" if use_idle else "token",
            "ok": True,
            "archived_msgs": archived,
            "kept_msgs": after_msgs,
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
            "summary": summary,
            "raw_dump": bool(archived > 0 and not summary),
        }

        if archived > 0:
            await self._notify(session_key, report)
        else:
            logger.info(
                "Context compaction idle for {}: ничего не сжато, estimated={}/{}",
                session_key, after_tokens, runtime.context_window_tokens,
            )

        return report

    @staticmethod
    def format_report(report: dict) -> str:
        """Человекочитаемое представление отчёта.

        Структура текста (для ``ok=True`` и ``archived > 0``):
          1. Полная сводка (если LLM-саммарайзер вернул ``summary``);
          2. Итоговая строка-выжимка: «заархивировано N сообщений,
             <before> → <after> токенов (экономия ≈X%)».
        """
        if not report.get("ok"):
            return f"Сжатие не выполнено: {report.get('reason', 'неизвестная причина')}"
        key = report.get("session_key") or "?"
        archived = int(report.get("archived_msgs") or 0)
        if archived <= 0:
            after = int(report.get("tokens_after") or 0)
            return (
                f"Сжатие сессии «{key}» не потребовалось: "
                f"контекст уже в пределах бюджета ({after} токенов)."
            )
        kept = int(report.get("kept_msgs") or 0)
        before = int(report.get("tokens_before") or 0)
        after = int(report.get("tokens_after") or 0)
        saved = max(0, before - after)
        pct = (saved / before * 100.0) if before > 0 else 0.0

        parts: list[str] = []
        summary = report.get("summary")
        if summary:
            preview = str(summary).strip()
            if preview and preview != "(nothing)":
                parts.append(preview)
        parts.append(
            f"Итог: заархивировано {archived} сообщений (осталось {kept}), "
            f"{before} → {after} токенов (экономия ≈{pct:.0f}%)."
        )
        return "\n\n".join(parts)

    async def _estimate(self, session: Any, runtime: Any) -> tuple[int, str]:
        try:
            est = self.agent.consolidator.estimate_session_prompt_tokens(
                session, runtime=runtime,
            )
            if hasattr(est, "__await__"):
                est = await est
            if not isinstance(est, tuple) or len(est) != 2:
                raise TypeError(
                    f"estimate_session_prompt_tokens returned {type(est).__name__}, expected tuple"
                )
            return est
        except Exception as exc:
            logger.warning(
                "estimate_session_prompt_tokens failed for {}: {}",
                getattr(session, "key", "?"), exc,
            )
            return self._estimate_fallback(session, runtime)

    @staticmethod
    def _estimate_fallback(session: Any, runtime: Any) -> tuple[int, str]:
        """Грубая fallback-оценка токенов, если ``estimate_session_prompt_tokens`` упал.

        Считаем примерный размер по ``messages``: ~4 символа ≈ 1 токен (общепринятая
        эвристика для англоязычных и смешанных текстов). Используется только когда
        нативный метод бросил исключение — чтобы логи/отчёт не врали «0 токенов» при
        реальном размере промпта в десятки тысяч токенов.
        """
        try:
            msgs = getattr(session, "messages", []) or []
            total_chars = 0
            for m in msgs:
                content = ""
                if isinstance(m, dict):
                    content = m.get("content") or ""
                else:
                    content = getattr(m, "content", "") or ""
                if isinstance(content, list):
                    content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
                total_chars += len(str(content))
            approx_tokens = max(1, total_chars // 4)
            limit = int(getattr(runtime, "context_window_tokens", 0) or 0)
            used_pct = (approx_tokens / limit * 100.0) if limit > 0 else 0.0
            chain = (
                f"[fallback] ~{approx_tokens} токенов по {len(msgs)} сообщ., "
                f"{used_pct:.0f}% от {limit}"
            )
            return approx_tokens, chain
        except Exception:
            return 0, ""

    @staticmethod
    def _current_session_key() -> str | None:
        try:
            from nanobot.agent.tools.context import current_request_session_key
            return current_request_session_key()
        except Exception:
            return None

    def _empty(self, reason: str) -> dict:
        return {
            "session_key": None,
            "mode": "token",
            "ok": False,
            "reason": reason,
            "archived_msgs": 0,
            "kept_msgs": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "summary": None,
            "raw_dump": False,
        }

    async def _notify(self, session_key: str, report: dict) -> None:
        text = self.format_report(report)
        logger.info(
            "Context compaction [{}] {}: archived={}, tokens {}→{}",
            report["mode"], session_key,
            report["archived_msgs"], report["tokens_before"], report["tokens_after"],
        )
        if self.print_to_terminal:
            try:
                from rich.console import Console
                Console().print(f"[dim]🗜️ {text}[/dim]")
            except Exception:
                pass
        if self.notify_in_history:
            await self._write_history_notice(session_key, report)

    async def record_external_compaction(
        self,
        *,
        session_key: str,
        mode: str,
        summary: str | None,
        archived_msgs: int,
        kept_msgs: int,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        """Записать факт сжатия, выполненного штатным кодом nanobot.

        Используется из обёрток ``runtime_patcher.patch_compaction_tracking``
        вокруг ``AutoCompact._archive`` и
        ``Consolidator.maybe_consolidate_by_tokens``: после того как
        нативный код сделал архивацию и сдвинул ``last_consolidated``,
        обёртка собирает замеры и зовёт этот метод — он пишет заметку
        в ``agent_conversation_messages`` ровно тем же кодом, что и
        ручной ``compact()`` (общий ``_notify`` + ``_write_history_notice``).
        Никакого двойного замера и отдельной ветки логирования.
        """
        if not self.notify_in_history:
            return
        if not archived_msgs or archived_msgs <= 0:
            return
        report = {
            "session_key": session_key,
            "mode": mode,
            "ok": True,
            "archived_msgs": int(archived_msgs),
            "kept_msgs": int(kept_msgs),
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "summary": summary,
            "raw_dump": bool(archived_msgs > 0 and not summary),
        }
        try:
            await self._notify(session_key, report)
        except Exception as exc:
            logger.warning("Auto history notice for {} failed: {}", session_key, exc)

    async def _write_history_notice(self, session_key: str, report: dict) -> None:
        """Записать заметку о сжатии в ``agent_conversation_messages``.

        Поддерживает session_key видов ``postgres:<chat_id>`` и
        ``streamlit:<chat_id>`` — это единственные каналы, у которых
        есть таблица обмена. Для прочих префиксов (например, ``cli:...``)
        — выходим без записи: история диалога CLI живёт в REPL-выводе
        и ``PGSessionManager`` (``agent_session_messages``).
        """
        try:
            prefix, _, chat_id = (session_key + ":").partition(":")
        except Exception:
            return
        if prefix not in ("postgres", "streamlit") or not chat_id:
            return
        pg = _get_setting(self._settings, "channels", "postgres", default={}) or {}
        dsn = pg.get("dsn") or ""
        if not dsn:
            return
        schema = pg.get("schema", "public")
        table = pg.get("table_name", "")
        if not table:
            return

        text = self.format_report(report)
        try:
            import asyncio as _asyncio

            from psycopg2.extras import Json
            from utils.db import configure, execute
        except Exception as exc:
            logger.warning("History notice import failed: {}", exc)
            return

        try:
            configure(dsn)
            # ``utils.db.execute`` — sync-функция (docs/INTERNAL_API.md § ``ctx.config``
            # vs ``ctx._settings_ref``: используем ~тот же threading-обход,
            # что и для sync-IO в ``postgres_channel``). Без ``asyncio.to_thread``
            # ``await execute(...)`` падает на ``'str' object can't be awaited``
            # (execute возвращает command tag, а не корутину).
            await _asyncio.to_thread(
                execute,
                f'INSERT INTO "{schema}"."{table}" '
                "(chat_id, user_id, role, content, media, metadata, "
                "buttons, status, created_at, updated_at) "
                "VALUES (%s, %s, 'assistant', %s, %s, %s, %s, "
                "'completed', NOW(), NOW())",
                chat_id, "agent", text,
                Json([]), Json({"kind": "context_compact", "compact": report}),
                Json([]),
            )
        except Exception as exc:
            logger.warning(
                "History notice for {} not written: {}", session_key, exc,
            )
