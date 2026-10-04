"""Общие условия прогона платформы.

Подстановки DSN в ``platform.json`` разворачиваются из
``mcp-platform/.secrets.env`` или из окружения процесса, и незаданная
переменная останавливает сборку настроек. Прогон не должен зависеть от
локальных секретов: подставляются заглушки, достаточные для разбора
конфигурации. Настоящий DSN тестам не нужен — пул подключается лениво.

Отдельный ``DATABASE_URL`` в окружении остаётся: сервер обязан отказываться
подниматься без DSN (иначе буфер журнала молча теряет каждое событие), и
проверка этого поведения не должна зависеть от файла.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

#: Подстановки ``platform.json``, которых должно хватать для разбора: DSN и
#: ключ провайдера. Пока в файле есть обе, заглушек быть должно обе — новая
#: подстановка обязана заставлять поправить этот словарь, иначе она тихо
#: валит половину прогона вместо одной понятной ошибки.
#:
#: Словарь — про подстановки, а не только про секреты: ``NANOBOT_WORKSPACE``
#: в нём секретом не является, но объявляет ``execution.session_root``, и
#: корень файлов сессии обязан лежать внутри рабочего каталога агента, иначе
#: граница файловых инструментов откажет в записи. Значение уводится в TEMP:
#: проверяется объявление, а не путь, и прогон не должен писать в каталог
#: платформы.
DUMMY_SECRETS: dict[str, str] = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
    "EMBED_TOKEN": "test",
    "NANOBOT_WORKSPACE": str(Path(tempfile.gettempdir()) / "nanobot-platform-tests"),
}


@pytest.fixture(autouse=True, scope="session")
def _platform_environment():
    """Задать DSN и подстановки платформы на время сессии."""
    wanted = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        **DUMMY_SECRETS,
    }
    previous = {key: os.environ.get(key) for key in wanted}
    os.environ.update(wanted)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


#: Настройки слоя исполнения для тестовой сборки. Значения — минимальные, но
#: осмысленные: нулевой порог означал бы «крупный результат не измеряется», а
#: ``session_root`` в ``tmp_path`` — что тест не пишет в каталог платформы.
#: Всё, что проверяет пороги, задаёт их само.
def execution_settings(session_root: Path) -> dict[str, Any]:
    return {
        "ENTERPRISE_EXEC_MAX_INLINE_BYTES": 65536,
        "ENTERPRISE_EXEC_PREVIEW_BYTES": 512,
        "ENTERPRISE_EXEC_TIMEOUT_SEC": 30.0,
        "ENTERPRISE_EXEC_PERSIST_LARGE": True,
        "ENTERPRISE_EXEC_QUALITY_CHECK": True,
        "ENTERPRISE_EXEC_LOGGING": True,
        "ENTERPRISE_EXEC_SESSION_ROOT": str(session_root),
        "ENTERPRISE_EXEC_LOG_ARG_FIELDS": "event_type,tool_name",
        "ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES": 512,
        "ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES": 1024,
        "ENTERPRISE_EXEC_LOG_REDACT_KEYS": "password,secret,token,api_key,dsn",
        "ENTERPRISE_EXEC_SESSION_EVENTS": False,
        "ENTERPRISE_EXEC_REQUIRE_CALL_META": True,
    }


def make_layer(
    session_root: Path,
    *,
    sink: Any = None,
    **overrides: Any,
) -> Any:
    """Слой исполнения для теста: каталог сессий в ``tmp_path``.

    Метаданные вызова по умолчанию **обязательны** (``require_call_meta=True``):
    тест, который проверяет домен, не должен случайно проходить по переходному
    пути из аргументов и тем самым подтверждать то, что проверять не нужно.
    """
    from libs.enterprise_common.execution.factory import build_execution_layer

    settings = {**execution_settings(session_root), **overrides}
    return build_execution_layer(settings, sink=sink, session_root=session_root)


def call_meta(
    request_id: str = "req-1", session_id: str = "sess-1", user_id: str = "user-1"
) -> dict[str, str]:
    """Метаданные вызова в форме ``params._meta`` (§ ``runtime/call-contract``)."""
    from libs.enterprise_common.execution.context import (
        KEY_REQUEST_ID,
        KEY_SESSION_ID,
        KEY_USER_ID,
    )

    return {
        KEY_REQUEST_ID: request_id,
        KEY_SESSION_ID: session_id,
        KEY_USER_ID: user_id,
    }


async def call_tool(transport: Any, name: str, arguments: dict[str, Any], meta: Any = None) -> Any:
    """Вызов операции по протоколу MCP с метаданными оборота.

    Обёртка нужна всем тестам, идущим через провод: без неё каждый второй тест
    звал бы ``session.call_tool`` по-своему, и половина забыла бы про
    ``meta=`` — отказ выглядел бы как дефект домена.
    """
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(transport) as session:
        return await session.call_tool(
            name,
            arguments=arguments,
            meta=call_meta() if meta is None else meta,
        )
