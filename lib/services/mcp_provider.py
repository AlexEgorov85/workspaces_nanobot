"""Модельная поверхность операций платформы — ``MCPProvider`` нанобота.

Зачем модуль. Объявление ``config.json -> tools.mcpServers.enterprise`` было
живым текстом без единой строки кода, которая его читает: ``MCPProvider`` в
нашей композиции не создавался, поэтому семь операций
``mcp_enterprise_*`` не попадали в реестр инструментов, а навыки обещали
модели вызовы, которых в рантайме нет. Это ровно тот класс дефекта, который
проект уже называет «настройка объявлена, но не действует».

Два процесса платформы — не случайность. Наш собственный клиент
(:mod:`lib.services.enterprise_mcp_client`) обслуживает фоновые подсистемы,
ходящие в платформу вне оборота; провайдер отдаёт операции модели. Оба
читают одно объявление о процессе (``tools.mcpServers.enterprise``) и одно
имя контура, поэтому журнал тестового контура не может оказаться в боевых
таблицах ни через один из путей.

Почему отказ здесь громкий. ``MCPProvider.connect()`` отказ **не** бросает:
он пишет warning и пробует при следующей проверке готовности. Для модели это
означало бы, что tool'ов в реестре нет, а навык продолжает их обещать, —
дефект без единого признака в логе. Поэтому успех проверяется здесь, сверкой
объявленных серверов с соединёнными, и несовпадение поднимается наружу: в
gateway оно уходит в ``GatewayRunner`` (перезапуск с backoff), в CLI — в
понятную ошибку до REPL.
"""

from __future__ import annotations

from typing import Any


class McpProviderUnavailable(RuntimeError):
    """Объявленный MCP-сервер не поднялся.

    Отдельный тип, а не текст: решение о старте не принято по существу
    (сервер не дал инструменты), повтор и перезапуск не исправят ничего,
    пока не починят окружение. ``servers`` — имя сервера и его статус из
    ``runtime_status()``, чтобы причина была в сообщении, а не в логе
    библиотеки, который никто не ищет.
    """

    def __init__(self, message: str, *, servers: dict[str, str]) -> None:
        super().__init__(message)
        self.servers = servers


def build_mcp_provider(config: Any, registry: Any) -> Any | None:
    """Создать провайдера поверх реестра, который уйдёт в ``AgentLoop``.

    Реестр один и тот же объект: ``AgentLoop`` держит **ссылку** на него
    (``nanobot/agent/loop.py:383``), поэтому инструменты, зарегистрированные
    при ``connect()`` позже, видны циклу без пересборки агента. Свой реестр
    на каждый вызов означал бы ровно то, что было: провайдер пишет в свою
    корзину, модель едет с пустой.

    ``None`` — серверы не объявлены (``tools.mcpServers`` пуст). Это не
    ошибка: оператора, который не хотел модели ходить в платформу, ломать
    старт нечем.
    """
    from nanobot.agent.tools.mcp import MCPProvider

    provider = MCPProvider.from_config(config, registry)
    if not provider.configured_server_names:
        return None
    return provider


async def connect_mcp_provider(provider: Any | None) -> list[str]:
    """Поднять объявленные серверы и убедиться, что инструменты получены.

    Возвращает число зарегистрированных операций — оно же попадает в
    вердиктную строку старта, чтобы «поверхность для модели есть» было видно
    числом, а не утверждением.

    ``None`` — провайдера нет, нечего поднимать.
    """
    if provider is None:
        return []

    await provider.connect()

    connected = set(provider.connected_server_names)
    declared = set(provider.configured_server_names)
    missing = declared - connected
    if missing:
        statuses = provider.runtime_status()
        detail = {
            name: str(statuses.get(name, "unknown"))
            for name in sorted(missing)
        }
        raise McpProviderUnavailable(
            "объявленные MCP-серверы не поднялись: "
            + ", ".join(f"{name}={state}" for name, state in detail.items()),
            servers=detail,
        )

    return _tool_count(provider)


def _tool_count(provider: Any) -> list[str]:
    """Имена подключённых серверов — для вердиктной строки."""
    return sorted(provider.connected_server_names)


async def close_mcp_provider(provider: Any | None) -> None:
    """Закрыть соединения провайдера. Отказ молчалив, но с записью в лог."""
    if provider is None:
        return
    try:
        await provider.aclose()
    except Exception as exc:  # noqa: BLE001 - остановка не должна падать
        import logging

        logging.getLogger(__name__).warning(
            "MCPProvider.aclose failed: %s", exc
        )
