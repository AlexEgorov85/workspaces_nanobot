"""Команда ``/compact`` — ручное сжатие контекста как настоящая slash-команда.

Реализована по образцу встроенных команд nanobot (см.
``nanobot/command/builtin.py``, например ``cmd_new`` или ``cmd_model``):
handler — async-функция, принимающая ``CommandContext`` и возвращающая
``OutboundMessage | None``. Команда регистрируется в ``CommandRouter``
агента (``agent.commands``) патчем ``RuntimePatcher.patch_compact_command``.

Почему это правильный путь вместо tool'а ``compact_context``:
в ``run()`` (``loop.py:1053-1090``) slash-команды, зарегистрированные в
router'е, перехватываются ДО LLM (``is_priority`` / ``is_dispatchable_command``
→ ``_dispatch_command_inline``), либо обрабатываются в ``_state_command``.
``/compact`` без регистрации уходит в LLM как обычное user-сообщение, и
решение «сжимать или нет» принимает модель — она часто отвечает текстом
«сжатие не требуется», не вызывая tool. Как настоящая команда ``/compact``
срабатывает детерминированно и безоговорочно для любого канала
(postgres, streamlit, telegram и т.д.).
"""
from __future__ import annotations

from typing import Any

from loguru import logger


async def cmd_compact(ctx: Any, settings: Any = None) -> Any:
    """Принудительное сжатие контекста сессии.

    Всегда сжимает жёстко (``force=True`` → ``compact_idle_session``),
    независимо от размера контекста и порога ``consolidationRatio`` —
    это явная команда пользователя. Поддерживает аргумент ``idle``
    (``/compact idle``) для совместимости; ``force`` подразумевается.

    ``settings`` — SETTINGS (``gateway.compact.*``), чтобы уважать
    ``enabled=false``. ``RuntimePatcher.patch_compact_command`` пробрасывает
    реальные settings через ``functools.partial``; при ``None`` берётся
    дефолт ``enabled=True`` (как в CLI-пути ``_run_cli_compact``).
    """
    from nanobot.bus.events import OutboundMessage

    from lib.services.context_compaction import ContextCompactionService
    from lib.utils.outbound_meta import FINAL_TURN_KEY

    raw = (ctx.raw or "").strip()
    tokens = raw.split()
    idle = any(t in ("idle", "--idle", "-i") for t in tokens[1:])

    svc = ContextCompactionService(ctx.loop, settings=settings)
    session_key = ctx.key
    metadata = dict(ctx.msg.metadata or {})

    def reply(text: str) -> OutboundMessage:
        # Command-обороты не проходят через ``_assemble_outbound``, поэтому
        # финальный маркер ставим здесь. Без него ``postgres_channel.send``
        # трактует ответ как промежуточную публикацию (merge) и НЕ закрывает
        # слот/claim и НЕ ставит ``status='completed'`` — ответ не доходит до
        # чата, а задача пере-обрабатывается по истечении lease.
        metadata[FINAL_TURN_KEY] = True
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=text, metadata=metadata,
        )

    if not svc.enabled:
        return reply("Сжатие контекста отключено (gateway.compact.enabled=false).")

    try:
        report = await svc.compact(
            session_key=session_key, idle=idle, force=True,
        )
        text = svc.format_report(report)
    except Exception as exc:
        logger.opt(exception=exc).error("Command /compact failed for {}", session_key)
        text = f"Сжатие не выполнено: {exc}"

    return reply(text)
