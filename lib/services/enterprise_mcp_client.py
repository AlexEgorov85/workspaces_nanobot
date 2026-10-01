"""Клиент агента к MCP-серверу ``enterprise-mcp``.

Зачем он агентским сервисам, если в nanobot уже есть MCP-клиент: тот
клиент обслуживает **модель** — он поднимает сервер, регистрирует его
операции как tool'ы и прячет сессию в приватном поле. Программно вызвать
операцию из сервиса агента через него нельзя, а лезть в приватное поле
означало бы привязаться к внутренности нанобота.

Поэтому у агента свой клиент. Он читает то же объявление сервера
(``project.json → enterprise_mcp``) и поднимает **единственный** процесс
сервера. Второй экземпляр означал бы второго владельца пула PostgreSQL,
а владелец у разделяемого ресурса должен быть один.

Жизненный цикл ленивый: сервер не стартует, пока не понадобился, и не
мешает старту агента, если платформа выключена или не собрана.

Про LLM
-------

Этот клиент не передаёт платформе ничего о провайдере модели. Настройки
живут в ``mcp-platform/platform.json`` и принадлежат платформе: она и
делает вызов. Агенту они не нужны, и из этого следует практическое
следствие — ключ провайдера не попадает в окружение процессов скиллов,
которые этот клиент не поднимает.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_TOOL_TIMEOUT_SEC = 30.0

#: ``[code] message`` — формат доменной ошибки операции (см. build_server).
_ERROR_PREFIX = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.DOTALL)


class EnterpriseMcpUnavailable(RuntimeError):
    """Сервер не поднялся, упал или не ответил вовремя."""


class EnterpriseOperationError(RuntimeError):
    """Операция ответила доменной ошибкой.

    Код отделён от текста: вызывающий решает по коду, показывать ли это
    модели, и не разбирает строку.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _first_text(result: Any) -> str:
    """Текст первого текстового блока ответа операции."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return str(text)
    return ""


def _split_error_code(text: str) -> tuple[str, str]:
    """Разобрать ``[code] message``; без кода — код ``operation_failed``."""
    match = _ERROR_PREFIX.match(text.strip())
    if match is None:
        return "operation_failed", text.strip()
    return match.group(1), match.group(2).strip()


class EnterpriseMcpClient:
    """Долгоживущая stdio-сессия к одному MCP-серверу.

    Сессия поднимается при первом вызове и живёт до :meth:`aclose`.
    Ошибка транспорта обнуляет сессию: следующий вызов поднимет её заново,
    иначе агент залипал бы на мёртвом процессе до перезапуска.
    """

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        tool_timeout_sec: float = DEFAULT_TOOL_TIMEOUT_SEC,
        server_name: str = "enterprise-mcp",
        snapshot_path: str | None = None,
        expected_tables: tuple[str, ...] = (),
    ) -> None:
        self._command = command
        self._args = list(args or [])
        # Смешанные разделители из резолва ${VAR} недопустимы для cwd
        # на других платформах — приводим к нативному виду.
        self._cwd = str(Path(cwd)) if cwd else None
        self._timeout = float(tool_timeout_sec or DEFAULT_TOOL_TIMEOUT_SEC)
        self._server_name = server_name
        # Путь к файлу снимка приходит снаружи, из единственного механизма
        # агента (resolve_cache_path). None = «снимок не задан», и capability
        # vectors остаётся ненастроенной с внятной ошибкой на операции.
        self._snapshot_path = str(snapshot_path) if snapshot_path else None
        # Обязательные runtime-таблицы для операции ``schema_check``. Список
        # приходит аргументом, а не вычисляется здесь: единственный источник
        # — ``SchemaValidationService.expected_table_names``, и второе
        # вычисление того же списка разошлось бы с ним при первой правке.
        # Пустой список — не «таблиц нет», а «оператор их не объявил»: так и
        # отвечает платформа, и это правда.
        self._expected_tables = tuple(expected_tables or ())
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # -- состояние ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def describe(self) -> dict[str, Any]:
        """Сведения для баннера запуска и диагностики."""
        return {
            "server": self._server_name,
            "command": self._command,
            "args": self._args,
            "cwd": self._cwd,
            "connected": self.is_connected,
        }

    # -- вызов -------------------------------------------------------------

    async def list_operations(self) -> list[str]:
        """Имена операций, которые сервер отдаёт в discovery.

        Нужно не для красоты, а для диагностики: «сервер поднялся» и «сервер
        отдаёт те операции, ради которых поднялся» — разные утверждения. Без
        этого метода несовпадение состава операций обнаруживалось бы только
        по жалобе пользователя на «индексы не ищутся».

        Raises:
            EnterpriseMcpUnavailable: сервер недоступен или не ответил.
        """
        session = await self._ensure_session()
        try:
            result = await asyncio.wait_for(
                session.list_tools(), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"discovery не ответил за {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            raise EnterpriseMcpUnavailable(f"discovery не удался: {exc}") from exc
        tools = sorted(getattr(result, "tools", None) or [], key=lambda t: t.name)
        return [str(t.name) for t in tools]

    async def call(self, operation: str, arguments: dict[str, Any] | None = None) -> str:
        """Вызвать операцию и вернуть её текстовый ответ.

        Raises:
            EnterpriseOperationError: операция ответила доменной ошибкой.
            EnterpriseMcpUnavailable: сервер недоступен или не ответил.
        """
        session = await self._ensure_session()
        try:
            result = await asyncio.wait_for(
                session.call_tool(operation, arguments or {}),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"операция {operation!r} не ответила за {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"вызов операции {operation!r} не удался: {exc}"
            ) from exc

        text = _first_text(result)
        if getattr(result, "isError", False):
            code, message = _split_error_code(text)
            raise EnterpriseOperationError(code, message)
        return text

    # -- жизненный цикл ----------------------------------------------------

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            self._stack = AsyncExitStack()
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                read, write = await self._stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=self._command,
                            args=self._args,
                            cwd=self._cwd,
                            env=self._child_env(),
                        )
                    )
                )
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except BaseException as exc:
                await self._stack.aclose()
                self._stack = None
                raise EnterpriseMcpUnavailable(
                    f"сервер {self._server_name!r} не поднялся: {exc}"
                ) from exc
            self._session = session
            self._loop = asyncio.get_running_loop()
            return session

    async def _reset(self) -> None:
        """Сбросить сессию: оборванный процесс недоступен навсегда."""
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - закрытие не должно ронять вызов
                pass

    async def aclose(self) -> None:
        await self._reset()

    def close(self) -> None:
        """Синхронное закрытие для ``ApplicationContext.stop()``.

        Сессия принадлежит event loop, на котором была создана, поэтому
        закрыть её можно только там же. Если loop уже закрыт (gateway
        закрывает его в ``asyncio.run`` до ``ctx.stop()``) — закрывать
        нечем: stdio-сервер завершается сам, как только у агента
        закроется stdin. Это штатный путь, а не ошибка.
        """
        loop, self._loop = self._loop, None
        if self._stack is None or loop is None:
            self._stack, self._session = None, None
            return
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.aclose(), loop=loop)
                )
                return
            if not loop.is_closed():
                loop.run_until_complete(self.aclose())
                return
        except Exception as exc:  # noqa: BLE001 - остановка не должна падать
            logger.warning("enterprise-mcp: не удалось закрыть сессию: %s", exc)
        self._stack, self._session = None, None

    def _child_env(self) -> dict[str, str]:
        """Окружение процесса сервера.

        Полное наследование окружения агента, а не пустое: сервер поднимает
        fail-fast по ``DATABASE_URL`` и без него не стартует, а агент уже
        экспортировал секреты в ``os.environ``.

        ``PYTHONIOENCODING`` задаётся явно: сервер пишет в stderr по-русски,
        и на Windows с cp1251 кодировка консоли убила бы процесс на
        первом же сообщении.

        Сверх наследования добавляется конфигурация capability ``vectors``
        и ``audit``. Платформа не читает конфиг агента (такой импорт
        запрещён архитектурным стражем), поэтому объявления индексов и путь
        снимка переезжают в окружение процесса, и заполняет его агент.

        Конфигурации провайдера LLM среди них **нет**: она живёт в
        ``mcp-platform/platform.json`` и принадлежит платформе, которая и
        делает вызов. Агент не знает ни адреса, ни модели, ни ключа — и
        потому не может ни разъехаться с платформенной копией, ни утянуть
        секрет в процессы скиллов. Раньше здесь был экспорт всех шести
        ``ENTERPRISE_LLM_*``; вторая копия выбора модели разъезжалась с
        первой при первой же смене модели, и это стоило отдельного файла
        ``lib/services/llm_config.py``.
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(_vectors_env_from_settings(self._snapshot_path))
        env.update(_audit_env_from_project())
        if self._expected_tables:
            env["ENTERPRISE_EXPECTED_TABLES"] = ",".join(self._expected_tables)
        return env


def _vectors_env_from_settings(snapshot_path: str | None) -> dict[str, str]:
    """Передать в процесс конфигурацию capability ``vectors`` и путь снимка.

    Платформа не читает ``project.json`` агента (такой импорт запрещён
    архитектурным стражем), поэтому объявления индексов переезжают в
    окружение процесса. Экспортируется ровно то, что уже является источником
    истины у агента: ``gateway.vector.index.*`` из ``project.json`` и
    параметры эмбеддера из ``read_embedding_config()``.

    Путь снимка приходит **аргументом**, а не вычисляется здесь заново:
    ``resolve_cache_path()`` — единственный механизм вычисления пути в
    агенте, и второе вычисление того же пути разошлось бы с ним при любой
    будущей правке. Пустой результат — «снимок не задан», а не «снимок
    задан как пустая строка»: платформа тогда оставляет capability
    ненастроенной, и чтение отдаёт внятную ошибку вместо «индексов нет».
    """
    env: dict[str, str] = {}
    if snapshot_path:
        env["ENTERPRISE_SNAPSHOT_PATH"] = str(snapshot_path)

    # Объявления индексов и параметры эмбеддера экспортируются независимо.
    # Модель эмбеддера входит в подпись индекса, поэтому её потеря из-за
    # отсутствия секции ``gateway.vector.index.indexes`` (индексы могут
    # прийти из реестра снимка) сделала бы индекс STALE на ровном месте.
    index = _project_gateway_vector_index()
    storage_table = str(index.get("storage_table") or "").strip()
    if storage_table:
        env["ENTERPRISE_VECTOR_STORAGE_TABLE"] = storage_table
    env["ENTERPRISE_VECTOR_ENABLE"] = "0" if index.get("enable") is False else "1"
    indexes = index.get("indexes")
    if isinstance(indexes, dict) and indexes:
        env["ENTERPRISE_VECTOR_INDEXES"] = json.dumps(indexes, ensure_ascii=False)

    embedding = _agent_embedding_config()
    if embedding.get("model"):
        env["ENTERPRISE_EMBED_MODEL"] = str(embedding["model"])
    if embedding.get("dimension") is not None:
        env["ENTERPRISE_EMBED_DIMENSION"] = str(embedding["dimension"])
    timeout = embedding.get("http_timeout_sec")
    if timeout is not None:
        env["ENTERPRISE_EMBED_TIMEOUT"] = str(timeout)

    # Адрес и токен эмбеддера — это КОНФИГУРАЦИЯ, а не HTTP-клиент, и
    # передавать её необходимо. Эмбеддер агента (Ollama) не является чат-
    # провайдером: другой адрес, другой путь, другая размерность вектора. Без
    # этих переменных платформа пошла бы в эндпойнт эмбеддингов чат-провайдера
    # и получила бы 404, а подпись индекса, посчитанная для mxbai-embed-large,
    # разошлась бы с фактически использованной моделью.
    #
    # Второго HTTP-клиента при этом не появляется: запрос по-прежнему делает
    # ``libs/llm``, и страж ловит ``httpx`` вне его владельца.
    base_url = str(embedding.get("base_url") or "").strip()
    if base_url:
        base, _, path = base_url.rstrip("/").partition("/api/")
        if _:
            # Адрес агента задан полным URL вместе с путём эндпоинта
            # ("http://host:port/api/embed"). Платформа ждёт base и path
            # отдельно, поэтому путь отделяется здесь — единственное место,
            # где формат адреса известен.
            env["ENTERPRISE_EMBED_API_BASE"] = base
            env["ENTERPRISE_EMBED_PATH"] = f"api/{path}"
        else:
            env["ENTERPRISE_EMBED_API_BASE"] = base_url
    token = embedding.get("auth_token")
    if token:
        env["ENTERPRISE_EMBED_API_KEY"] = str(token)
    return env


def _audit_env_from_project() -> dict[str, str]:
    """Передать в процесс конфигурацию capability ``audit``.

    Таблица реестра скриптов и список доменных таблиц приходят из
    ``project.json → skills.audit_analyzer.tables`` — оттуда же, откуда их
    берёт сам навык. Дублировать эти имена ещё и в ``project.json →
    enterprise_mcp`` нельзя: второе место, где правят, разъедется с первым
    при первой же правке, а платформа запретит потерять таблицы молча.

    Обе переменные обязательны на стороне платформы: с пустым значением
    ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE`` capability ``audit`` отвечает
    ``registry_unavailable`` на каждую операцию, то есть не работает вовсе.

    Реестр намеренно НЕ попадает в ``ENTERPRISE_AUDIT_TABLES``: его
    ``label`` в ``project.json`` означает «реестр метаданных, не для схемы
    модели», и выдав его как доменную таблицу, мы разрешили бы аудиту
    читать собственные предустановленные скрипты в обход проверки строк.
    """
    try:
        from config import load_config_json

        project = load_config_json("project.json")
    except Exception as exc:  # noqa: BLE001 - аудит не должен ронять старт
        logger.warning("enterprise-mcp: project.json не прочитан, capability audit не настроена: %s", exc)
        return {}

    skills = project.get("skills") if isinstance(project, dict) else None
    declared = (skills or {}).get("audit_analyzer", {}).get("tables") or []
    names: list[str] = []
    registry: str | None = None
    for item in declared:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if item.get("label") == "scripts_registry":
            registry = name
        else:
            names.append(name)

    env: dict[str, str] = {}
    if registry:
        env["ENTERPRISE_SCRIPTS_REGISTRY_TABLE"] = registry
    if names:
        env["ENTERPRISE_AUDIT_TABLES"] = ",".join(names)
    return env


def _project_gateway_vector_index() -> dict[str, Any]:
    """``project.json → gateway.vector.index`` без инициализации ``SETTINGS``.

    Читается файл проекта напрямую, а не через ``SETTINGS``: клиент
    создаётся при инициализации контекста, и требовать от него
    lifecycle-gate конфигурации значило бы связать порядок сборки сервисов с
    порядком инициализации настроек. Структура секции валидируется в
    ``ProjectSettings``.
    """
    try:
        from config import load_config_json

        project = load_config_json("project.json")
        gateway = project.get("gateway") or {}
        vector = gateway.get("vector") or {}
        index = vector.get("index") or {}
    except Exception:  # noqa: BLE001 - отсутствие секции = индексов не объявлено
        return {}
    return index if isinstance(index, dict) else {}


def _agent_embedding_config() -> dict[str, Any]:
    """Параметры эмбеддера из единственного источника агента.

    Адрес и bearer-токен **не** экспортируются: HTTP принадлежит capability
    ``llm``, и вторая копия подключения к эмбеддеру была бы тем же нарушением
    владения, ради которого страж ловит ``httpx`` вне её владельца.
    """
    try:
        from lib.services.cache_provider_impl import read_embedding_config

        cfg = read_embedding_config()
    except Exception:  # noqa: BLE001 - нет конфигурации = подпись индекса неполна
        logger.debug("enterprise-mcp: конфигурация эмбеддера не прочитана", exc_info=True)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def client_from_settings(
    settings: Any,
    *,
    snapshot_path: str | None = None,
    expected_tables: tuple[str, ...] = (),
) -> EnterpriseMcpClient | None:
    """Собрать клиента из ``project.json → enterprise_mcp``.

    ``None`` — раздел выключен или не задан: тогда потребитель сообщает
    об этом структурной ошибкой, а не падает.

    ``snapshot_path`` — путь к файлу снимка, вычисленный вызывающей стороной
    через ``resolve_cache_path()``. Клиент его не вычисляет: единственный
    механизм вычисления пути в агенте один, и второе вычисление того же пути
    разошлось бы с ним при первой же правке.

    ``expected_tables`` — полные имена обязательных runtime-таблиц для
    операции ``schema_check``, полученные вызывающей стороной из
    ``SchemaValidationService.expected_table_names``. Без них операция
    отвечает «не задано ни одной ожидаемой таблицы»: она связана и видна,
    но сказать что-либо не может.
    """
    section = settings.get("enterprise_mcp") if settings is not None else None
    if not section:
        return None
    if section.get("enabled") is False:
        return None
    command = section.get("command")
    if not command:
        return None
    return EnterpriseMcpClient(
        command=str(command),
        args=list(section.get("args") or []),
        cwd=section.get("cwd"),
        tool_timeout_sec=float(
            section.get("tool_timeout_sec") or DEFAULT_TOOL_TIMEOUT_SEC
        ),
        snapshot_path=snapshot_path,
        expected_tables=expected_tables,
    )
