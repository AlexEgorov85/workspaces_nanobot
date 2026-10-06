"""ContextCompactionService — единая точка записи факта сжатия контекста.

Два входа — один путь записи (заметка в ``agent_conversation_messages``,
loguru INFO, опциональный Rich-вывод в терминал gateway):

  1. **Ручной запуск**: slash-команда ``/compact`` (upstream
     ``nanobot/command/builtin.py::cmd_compact``), CLI-команда ``/compact``
     (``lib/cli/console_loop.py::_run_cli_compact``) или tool агента
     ``compact_context`` (``workspace/tools/compact_context.py``).
     Метод :py:meth:`compact` сам зовёт штатный ``Consolidator`` из
     nanobot 0.3.0 (``maybe_consolidate_by_tokens`` /
     ``compact_idle_session``), замеряет состояние сессии до/после
     и формирует ``report``.

  2. **Авто-сжатие upstream** (``AutoCompact`` и
     ``Consolidator.maybe_consolidate_by_tokens``). Обёрток в
     ``runtime_patcher`` для них нет: nanobot сам публикует
     ``nanobot.events.ContextCompactionEvent``, канал фильтрует его через
     ``CompactionEventSubscriber`` и зовёт
     :py:meth:`notify_session_compacted`. Раньше docstring описывала
     ``_wrap_auto_compact_archive`` и ``_wrap_maybe_consolidate_by_tokens`` —
     таких функций в ``runtime_patcher`` никогда не было на этой ветке, и
     описание обещало перехват, которого не было.

Результат для обоих путей одинаков: один ``format_report``,
один ``_write_history_notice``, одна loguru-строка. Пользователь и
логи не различают, было ли сжатие ручным или автоматическим.

Заметка в ``agent_conversation_messages`` видна в UI-чате,
но НЕ попадает в контекст промпта: контекст агента строится из
upstream JSONL-стора ``SessionManager`` (mirror в PG через
``lib/gateway/mirror/``), а таблица обмена —
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

    def __init__(
        self, agent: Any, settings: Any = None,
        *,
        db_logging_service: Any = None,
        enterprise_mcp: Any | None = None,
    ) -> None:
        self.agent = agent
        self._settings = settings
        self._db_logging_service = db_logging_service
        # Клиент платформы для записи заметки в историю диалога. Раньше
        # заметка писалась напрямую по DSN и имени таблицы из конфига агента,
        # то есть в обход оверлея профиля: под тестовым профилем она уходила
        # в боевую таблицу. ``None`` — платформа выключена по решению
        # оператора; тогда заметка не пишется вовсе, и это молчание видно
        # в логе, а не выглядит как запись «куда-то».
        self._enterprise_mcp = enterprise_mcp
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
                summarize_fn = getattr(
                    consolidator, "summarize_provider_compaction", None,
                )
                if summarize_fn is None:
                    return self._empty(
                        "Consolidator не предоставляет summarize_provider_compaction",
                    )
                return self._empty(
                    "token-budget компакция через ContextCompactionService.compact "
                    "не поддержана в nanobot 0.3.5 — используйте /compact или "
                    "upstream-событие ContextCompactionEvent через "
                    "CompactionEventSubscriber",
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
        await self._record_event_log(session_key, report, text)
        if self.notify_in_history:
            await self._write_history_notice(session_key, report)

    async def _record_event_log(
        self, session_key: str, report: dict, text: str,
    ) -> None:
        """Записать событие ``agent.compacted`` в долговечный журнал
        ``agent_gateway_logs``.

        Закрывает gap №1 из ``docs/architecture/HISTORY_SEARCH_ANALYSIS.md``:
        инструкция для агента в ``description`` tool'а ``history_search`` и в
        ``workspace/TOOLS.md`` обещала событие, которого в журнале не было.
        Теперь обещание согласовано с фактическим поведением.

        Единственный writer — ``DbLoggingService`` (через
        ``try_log_event`` — defensive helper). При отсутствии сервиса
        событие теряется (no-op for business) и пишется WARNING —
        observability-trail НЕ должен зависеть от ``notify_in_history``:
        даже если UI-уведомления выключены, ``history_search(event_type=
        "agent.compacted")`` должен находить событие (это закрывает
        design D8 — факт сжатия пишется в журнал независимо от
        ``notify_in_history``, а не «если повезло»).

        ``user_id`` берётся из identity-store текущего request (для
        ``history_search(session_scope="all")`` как security boundary).
        ``request_id`` — идентификатор вопроса из очереди
        (``RequestContext.message_id``, см. ``_current_request_id``): событие
       compact'а принадлежит тому обороту, чей контекст сжало, и без якоря
        строка не джойнилась ни с одним прогоном.
        При отсутствии identity-store — событие пишется с ``user_id IS NULL``
        и НЕ участвует в ``scope='all'`` (безопасный default). ``DbLoggingService``
        резолвит ``user_id`` через ``_enqueue`` security-boundary path,
        но явное значение через LogEvent.user_id имеет приоритет (для
        случаев вроде subagent'ов, которым нужно прокинуть identity родителя).
        """
        from lib.services.db_logging_service import LogEvent, is_question_anchor, try_log_event

        summary = text[:200] if text else "context compacted"
        payload = {
            "mode": report.get("mode"),
            "archived_msgs": report.get("archived_msgs"),
            "kept_msgs": report.get("kept_msgs"),
            "tokens_before": report.get("tokens_before"),
            "tokens_after": report.get("tokens_after"),
            "summary": report.get("summary"),
            "raw_dump": report.get("raw_dump", False),
        }
        log_event = LogEvent(
            event_type="agent.compacted",
            level="INFO",
            session_id=session_key,
            channel="system",
            actor="consolidator",
            name="consolidator",
            summary=summary,
            payload=payload,
            user_id=_current_request_sender_id(),
            # Поле идентификатора вопроса не должно занимать значение, которое
            # идентификатором вопроса не является. Проверка по форме, а не по
            # длине: ``VARCHAR(256)`` — это не «поместилось, значит годится».
            request_id=_anchor_or_none(_current_request_id(), is_question_anchor),
        )
        try:
            try_log_event(
                self._db_logging_service,
                log_event,
                producer="ContextCompactionService",
                event_type="agent.compacted",
            )
        except Exception as exc:
            logger.warning(
                "agent_gateway_logs write for {} failed: {}",
                session_key, exc,
            )

    async def notify_session_compacted(
        self,
        *,
        session_key: str,
        phase: str,
        compaction_id: str,
    ) -> None:
        """Записать факт compaction-фазы upstream (``ContextCompactionEvent``).

        Вызывается из ``CompactionEventSubscriber.feed`` (см.
        ``lib/services/compaction_event_subscriber.py``) при получении
        ``OutboundMessage.event`` типа ``ContextCompactionEvent`` из
        ``bus.outbound``. Канал (postgres/redis) дёргает
        subscriber из своего ``send``; CLI-gateway вызывает метод
        напрямую (минуя шину).

        Фаза ``succeeded`` соответствует фактической архивации. Событие
        ``event_type="agent.compacted"`` пишется в ``agent_gateway_logs``
        **при любой фазе и независимо от** ``notify_in_history``; заметка в
        ``agent_conversation_messages`` добавляется только для ``succeeded``
        и только при ``notify_in_history=True``. Обращения прямые —
        ``_record_event_log`` и ``_write_history_notice``, а не через
        ``_notify``: у upstream-события нет замеров, и собирать ``report``
        для ``_notify`` тут не из чего.

        Параметры ``tokens_before``/``tokens_after``/``archived_msgs``
        неизвестны из upstream-события (содержит только ``compaction_id``
        и ``phase``), поэтому history-notice для upstream-сжатия
        содержит сводку без замеров; численные поля остаются
        ``None``/``0``.
        """
        if not session_key:
            return
        event_id = compaction_id or f"context_compacted:{session_key}"

        try:
            await self._record_event_log(
                session_key=session_key,
                report={
                    "session_key": session_key,
                    "mode": "upstream",
                    "phase": phase,
                    "compaction_id": event_id,
                    "archived_msgs": 0,
                    "kept_msgs": 0,
                    "tokens_before": 0,
                    "tokens_after": 0,
                    "summary": f"upstream compaction phase={phase} ({event_id})",
                    "raw_dump": False,
                },
                text=f"upstream compaction phase={phase} ({event_id})",
            )
        except Exception:
            logger.opt(exception=True).warning(
                "notify_session_compacted: event_log for {} failed", session_key,
            )

        if phase != "succeeded" or not self.notify_in_history:
            return
        history_text = (
            f"🗜️ upstream-сжатие ({phase}, {event_id}) выполнено upstream-механизмом"
        )
        try:
            await self._write_history_notice(
                session_key=session_key,
                report={
                    "session_key": session_key,
                    "mode": "upstream",
                    "ok": True,
                    "archived_msgs": 0,
                    "kept_msgs": 0,
                    "tokens_before": 0,
                    "tokens_after": 0,
                    "summary": history_text,
                    "raw_dump": False,
                    "compaction_id": event_id,
                    "phase": phase,
                },
            )
        except Exception:
            logger.opt(exception=True).warning(
                "notify_session_compacted: history_notice for {} failed", session_key,
            )

    async def _write_history_notice(self, session_key: str, report: dict) -> None:
        """Записать заметку о сжатии в историю диалога — через платформу.

        Поддерживает session_key вида ``postgres:<chat_id>`` — единственный
        канал, у которого есть таблица обмена. Для прочих префиксов
        (например, ``cli:...``)
        — выходим без записи: история диалога CLI живёт в REPL-выводе
        и upstream JSONL-сторе ``SessionManager`` (mirror в PG через
        ``lib/gateway/mirror/``).

        Запись идёт операцией ``append_history_notice`` платформы, а не
        прямым SQL. Причина не в «чистоте»: имя таблицы задаётся
        ``platform.json → profiles.<имя>``, и прямой INSERT писал в
        ``agent_conversation_messages`` из конфига агента. Под тестовым
        профилем это означало запись в БОЕВУЮ таблицу — успешно и молча.

        Если платформа выключена, заметка не пишется: молчание об этом
        остаётся в логе, потому что потерять запись в истории диалога
        молча — хуже, чем не записать её вовсе.
        """
        try:
            prefix, _, chat_id = session_key.partition(":")
        except Exception:
            return
        if prefix != "postgres" or not chat_id:
            return
        # Пространство имён канала и сам чат — разные вещи, а разделитель
        # один. ``"postgres:chat-1" + ":"`` перед partition'ом оставлял в
        # id двоеточие, и заметка уезжала в чат ``"chat-1:"``, которого нет:
        # запись проходила, и её не видел ни один клиент.
        chat_id = chat_id.strip()
        if not chat_id:
            return

        client = self._enterprise_mcp
        if client is None:
            logger.warning(
                "Заметка о сжатии для {} не записана: enterprise-mcp выключен. "
                "Прямая запись в PostgreSQL больше не выполняется по решению "
                "оператора — канал общается с БД только через платформу.",
                session_key,
            )
            return

        text = self.format_report(report)
        try:
            await client.call(
                "data.append_history_notice",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "metadata": {"kind": "context_compact", "compact": report},
                },
            )
        except Exception as exc:
            logger.warning(
                "History notice for {} not written: {}", session_key, exc,
            )


def _current_request_sender_id() -> str | None:
    """``RequestContext.sender_id`` текущего request (или ``None``).

    Используется :py:meth:`ContextCompactionService._record_event_log`
    для прокидывания ``user_id`` в ``agent_gateway_logs`` (security
    boundary для ``history_search(session_scope="all")``).
    """
    # Чтение личности оборота живёт в lib/services/turn_identity.py:
    # тот же contextvar читались хук подписи вызовов, клиент платформы,
    # подписчик событий и ещё три места, и копии правила разъезжались бы
    # молча — подпись в журнале и файл сессии описали бы разные вызовы.
    # Импорт ленивый: функцию подменяют в тестах по имени, и лишний
    # импорт на этапе сборки модуля ей не нужен.
    from lib.services.turn_identity import read_turn_context

    turn = read_turn_context()
    return turn.user_id if turn is not None else None


def _anchor_or_none(value: str | None, predicate) -> str | None:
    """Значение проходит ``predicate`` — берётся, иначе остаётся пустым.

    Обёртка нужна, чтобы вызывающая сторона не дублировала проверку в две строки
    и чтобы «не прошло» означало одно и то же во всех местах, где агенту есть
    дело до поля идентификатора вопроса: пусто, а не «как получится».
    """
    if predicate is None:
        return value
    try:
        return value if predicate(value) else None
    except Exception:
        return None


def _current_request_id() -> str | None:
    """Идентификатор ВОПРОСА текущего request (или ``None``).

    ``RequestContext.message_id`` — это ``metadata["message_id"]`` входящего
    сообщения, то есть ``id`` строки ``role='user'`` очереди, взятой этим
    оборотом (change 2026-10-04-queue-as-anchor-identity, Ф1.1). До правки
    событие ``agent.compacted`` подписывалось ``user_id`` текущего вопроса, но
    идентификатора вопроса не несло: строка выглядела принадлежащей вопросу по
    подписи, а джойнилась ни с чем. Это ровно та форма, которую change называет
    «привязанность выглядит лучше, чем она есть», только по ``user_id`` вместо
    выдуманного UUID.

    Пустое значение — законное состояние: сжатие может быть вызвано вне оборота
    (например, из CLI без строки очереди), и подставлять тогда нечего. Идентификатор
    НЕ выдумывается никогда.
    """
    try:
        from nanobot.agent.tools.context import current_request_context
    except Exception:
        return None
    try:
        ctx = current_request_context()
    except Exception:
        return None
    if ctx is None:
        return None
    message_id = getattr(ctx, "message_id", None)
    if isinstance(message_id, str) and message_id:
        return message_id
    return None
