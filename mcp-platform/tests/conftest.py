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
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

#: Подстановки ``platform.json``, которых должно хватать для разбора: DSN и
#: ключ провайдера. Пока в файле есть обе, заглушек быть должно обе — новая
#: подстановка обязана заставлять поправить этот словарь, иначе она тихо
#: валит половину прогона вместо одной понятной ошибки.
DUMMY_SECRETS: dict[str, str] = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
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
