"""Клиент платформы: разговор с процессом enterprise-mcp по протоколу MCP.

Зачем он, если агент уже умеет говорить с сервером
-------------------------------------------------

Агент работает **внутри** процесса, который сам поднял сервер, и держит его
клиент в ``lib/services/enterprise_mcp_client.py``. Skill-конвейер — нет:
он запускается как отдельный процесс и своего клиента не имеет.

Раньше такой процесс обходился собственным HTTP-вызовом к провайдеру. Это
и была вторая реализация: свой резолв настроек, свой retry, свой разбор
ответа — и расхождение с платформенной при первой же смене модели.

Ограничение, которое обошти
---------------------------

MCP — stdio, а он один на процесс: ребёнок получает ``stdin``/``stdout``
родителя, а не вторую пару каналов. Поэтому процесс, которому нужен
``complete``, поднимает **свой** экземпляр сервера, и наивная реализация
сделала бы его вторым владельцем пула PostgreSQL и вторым держателем
блокировки на файле снимка DuckDB.

Отсюда две части решения:

* сервер умеет подниматься с отбором capability
  (``--capabilities llm``, см. ``servers/enterprise/server.py``) — процесс
  без ``data`` не трогает ни базу, ни снимок, ни журнал;
* этот клиент всегда просит именно ``llm``.

Один процесс, одна сессия, много вызовов
----------------------------------------

Вызовов бывает много: конвейер суммаризации шлёт по одному запросу на
чанк, и это сотни обращений за проход. Поднимать сервер на каждый вызов
было бы дороже самой модели, поэтому процесс держит сессию живой, а
многопоточность (чанки считаются в пуле) обслуживает одним event loop в
фоновом потоке: весь обмен с сервером происходит на нём, вызывающие потоки
лишь ждут своего ответа. Сериализация вызовов — на стороне потребителя
(у суммаризации это single-flight), здесь она не нужна и не навязывается.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    # Импорт не на верхнем уровне намеренно: клиент поднимает сервер как
    # подпроцесс и рассчитан на то, чтобы импортироваться раньше, чем
    # платформа окажется в ``sys.path``. Импортировать контракт нужно в тот
    # момент, когда корень платформы точно доступен, — при создании клиента.
    from libs.enterprise_common.execution.context import McpCallContext

logger = logging.getLogger(__name__)

#: Операции, ради которых поднимается лёгкий сервер. ``data`` в списке нет
#: намеренно: см. докстринг модуля.
_LLM_CAPABILITIES = "llm"

#: Разбор доменной ошибки конверта: ``[код] сообщение``.
_ERROR = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.DOTALL)

#: Таймаут одного вызова по умолчанию. С запасом относительно сетевого
#: таймаута операции: сюда входит и подъём процесса, и сам запрос.
DEFAULT_CALL_TIMEOUT_SEC = 180.0


class LlmUnavailable(RuntimeError):
    """Сервер не поднялся, сессия оборвалась или не ответила вовремя."""


class LlmOperationError(RuntimeError):
    """Сервер ответил доменной ошибкой: сервис не настроен или отказал."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def platform_root() -> Path:
    """Корень платформы: каталог с ``platform.json``.

    Считается от этого файла (``libs/enterprise_client/llm.py`` → два
    уровня вверх), а не из ``cwd``: клиент вызывается из skill-скрипта,
    запущенного с произвольным рабочим каталогом, и «случайно угаданный
    корень» означал бы, что сервер не поднимется в одном месте и поднимется
    в другом.
    """
    return Path(__file__).resolve().parents[2]


def _first_text(result: Any) -> str:
    """Текст первого элемента ответа операции."""
    content = getattr(result, "content", None) or []
    if not content:
        return ""
    return str(getattr(content[0], "text", "") or "")


def _split_error(text: str) -> tuple[str, str]:
    """Разделить конверт ошибки на код и текст."""
    match = _ERROR.match(text.strip())
    if match:
        return match.group(1), match.group(2).strip()
    return "unknown", text.strip()


class LlmClient:
    """Синхронный разговор с capability ``llm`` через MCP.

    Один экземпляр — один процесс сервера. Потокобезопасен: вызывать можно
    из пула потоков, сессия обслуживается на фоновом event loop.
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        call_timeout_sec: float = DEFAULT_CALL_TIMEOUT_SEC,
        identity: McpCallContext | None = None,
    ) -> None:
        """Инициализировать клиент.

        Args:
            root: Корень платформы. По умолчанию — от расположения этого файла.
            call_timeout_sec: Сколько ждать ответа операции.
            identity: Идентичность оборота, которая уедет в ``params._meta``.

        Идентичность — аргумент конструктора, а не чтение окружения, и это
        принципиально. Окружение читает только реестр: клиент, начавший
        разбирать ``ENTERPRISE_*`` сам, стал бы вторым читателем настройки, и
        проверка «значение приходит из Settings» перестала бы что-либо
        значить. Здесь она приходит от вызывающего явно.

        Один клиент — один оборот. Подпроцесс скилла живёт внутри оборота и
        обслуживает сотни вызовов из пула чанков, поэтому идентичность
        постоянна для его жизни и меняться не может: смена означала бы, что
        вызовы одного оборота начали бы подписываться чужим.
        """
        self._root = root if root is not None else platform_root()
        self._call_timeout = float(call_timeout_sec)
        self._meta: dict[str, str] | None = (
            dict(identity.as_meta()) if identity is not None else None
        )
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stack: Any = None

    @property
    def meta(self) -> dict[str, str] | None:
        """Идентичность, которая уедет в ``params._meta``. ``None`` — её нет.

        Открыта наружу, чтобы «кто я в этом обороте» можно было проверить, не
        открывая транспорт. Копия, а не сам словарь: подменить идентичность
        на лету нельзя.
        """
        return dict(self._meta) if self._meta is not None else None

    # -- вызов -------------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Отправить сообщения и получить текст ответа.

        Настройки провайдера не передаются: они в ``platform.json``, и
        вызывающая сторона их не знает и не должна знать. Словарь настроек
        в аргументах означал бы вторую копию выбора модели.
        """
        arguments: dict[str, Any] = {"prompt": _user_text(messages)}
        system = _system_text(messages)
        if system is not None:
            arguments["system"] = system
        for name, value in (
            ("model", model),
            ("max_tokens", max_tokens),
            ("temperature", temperature),
            ("max_retries", max_retries),
            ("timeout", timeout),
        ):
            if value is not None:
                arguments[name] = value
        if context:
            # ``complete`` принимает историю диалога; собранные вызывающей
            # стороной сообщения уходят как ``context``, а последнее
            # пользовательское — как ``prompt``.
            arguments["context"] = list(context)
        return self._call("llm.complete", arguments)

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any] | None:
        """Как :meth:`complete`, но ответ разбирается как JSON-объект.

        ``None`` — ответ не разобрался; доменная ошибка сервера
        пробрасывается: молча превращать отказ провайдера в «модель ответила
        не тем» значило бы замаскировать сбой.
        """
        payload = self._call(
            "llm.complete",
            _json_arguments(messages, context, model, max_tokens, temperature),
        )
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> list[float]:
        """Вернуть эмбеддинг текста через операцию ``embed``."""
        arguments: dict[str, Any] = {"text": text}
        for name, value in (
            ("model", model),
            ("max_retries", max_retries),
            ("timeout", timeout),
        ):
            if value is not None:
                arguments[name] = value
        payload = self._call("llm.embed", arguments)
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise LlmUnavailable(f"эмбеддинг вернулся не разобранным: {exc}") from exc
        if isinstance(parsed, dict) and isinstance(parsed.get("vector"), list):
            return [float(value) for value in parsed["vector"]]
        raise LlmUnavailable("эмбеддинг вернулся без поля vector")

    # -- жизненный цикл ----------------------------------------------------

    def close(self) -> None:
        """Закрыть сессию и остановить фоновый поток.

        Идемпотентен и не бросает: вызывается из ``finally`` и из
        деструктора, а падение при закрытии замаскировало бы результат
        работы, которая уже состоялась.
        """
        with self._lock:
            loop, thread = self._loop, self._thread
            self._session, self._stack = None, None
            self._loop, self._thread = None, None
        if loop is None or thread is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._aclose(), loop)
            future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001 - закрытие не должно ронять вызов
            logger.warning("llm-клиент: сессия не закрылась: %s", exc)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            if not loop.is_closed():
                loop.close()

    def __enter__(self) -> LlmClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def _aclose(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            finally:
                self._stack = None

    # -- транспорт ---------------------------------------------------------

    def _call(self, operation: str, arguments: dict[str, Any]) -> str:
        """Выполнить операцию, дождавшись ответа вызывающего потока."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._call_async(operation, arguments), loop
        )
        try:
            return future.result(timeout=self._call_timeout)
        except TimeoutError as exc:
            self.close()
            raise LlmUnavailable(
                f"операция {operation!r} не ответила за {self._call_timeout:g}с"
            ) from exc

    async def _call_async(self, operation: str, arguments: dict[str, Any]) -> str:
        session = await self._ensure_session()
        try:
            if self._meta is None:
                # Без идентичности ``meta`` не отправляется вовсе, а не
                # отправляется пустым: пустой ``_meta`` выглядел бы на
                # сервере как «идентичность была и оказалась пустой».
                result = await session.call_tool(operation, arguments)
            else:
                result = await session.call_tool(
                    operation, arguments, meta=self._meta
                )
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            # Оборванный процесс не восстанавливается сам: следующий вызов
            # получил бы ту же ошибку, поэтому сессия сбрасывается.
            await self._reset()
            raise LlmUnavailable(f"вызов {operation!r} не удался: {exc}") from exc

        text = _first_text(result)
        if getattr(result, "isError", False):
            code, message = _split_error(text)
            raise LlmOperationError(code, message)
        return text

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Фоновый event loop: весь обмен с сервером — только на нём."""
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="enterprise-llm-client",
                daemon=True,
            )
            thread.start()
            self._loop, self._thread = loop, thread
            return loop

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(
                stdio_client(self._server_parameters())
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as exc:
            await stack.aclose()
            raise LlmUnavailable(
                f"сервер {self._root.name!r} (capability {_LLM_CAPABILITIES}) "
                f"не поднялся: {exc}"
            ) from exc
        self._stack, self._session = stack, session
        return session

    async def _reset(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - сброс не должен ронять вызов
                pass

    def _server_parameters(self) -> Any:
        """Параметры подъёма сервера только с capability ``llm``."""
        from mcp import StdioServerParameters

        env = dict(os.environ)
        # Сервер логирует по-русски; на Windows с cp1251 кодировка консоли
        # убила бы процесс на первом же сообщении.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(self._root), env.get("PYTHONPATH", "")) if part
        )
        return StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "servers.enterprise.server",
                "--capabilities",
                _LLM_CAPABILITIES,
            ],
            cwd=str(self._root),
            env=env,
        )


def _user_text(messages: list[dict[str, Any]]) -> str:
    """Текст последнего сообщения пользователя."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    raise LlmOperationError("invalid_request", "в сообщениях нет реплики пользователя")


def _system_text(messages: list[dict[str, Any]]) -> str | None:
    """Системная инструкция из сообщений, если она есть."""
    for message in messages:
        if message.get("role") == "system":
            return str(message.get("content", ""))
    return None


def _json_arguments(
    messages: list[dict[str, Any]],
    context: list[dict[str, Any]] | None,
    model: str | None,
    max_tokens: int | None,
    temperature: float | None,
) -> dict[str, Any]:
    """Аргументы вызова с разбором ответа в JSON.

    Отдельный метод нужен потому, что операция ``complete`` отдаёт текст, а
    разбор JSON живёт на стороне владельца: платформенный сервис такого
    метода не знает, и заводить его ради одного потребителя означало бы
    продублировать чистку ответа в ещё одном месте.
    """
    arguments: dict[str, Any] = {"prompt": _user_text(messages)}
    system = _system_text(messages)
    if system is not None:
        arguments["system"] = system
    if context:
        arguments["context"] = list(context)
    for name, value in (
        ("model", model),
        ("max_tokens", max_tokens),
        ("temperature", temperature),
    ):
        if value is not None:
            arguments[name] = value
    # Разборщик не повторяет запрос: модель, ответившая не-JSON, ответит так
    # же и второй раз, а молчаливое удвоение числа вызовов выглядело бы
    # как «сервис стал надёжнее».
    arguments["max_retries"] = 0
    return arguments


#: Клиент процесса по умолчанию. Создаётся при первом обращении и живёт до
#: выхода из процесса, чтобы сотни вызовов не поднимали сервер заново.
_default: LlmClient | None = None
_default_lock = threading.Lock()


def default_client(identity: McpCallContext | None = None) -> LlmClient:
    """Клиент процесса. Тот же, что и в прошлый раз.

    Идентичность запоминается вместе с клиентом. Повтор с **другой**
    идентичностью — отказ, а не пересоздание: молча выпустить вызовы нового
    оборота под старым ``request_id`` значило бы испортить журнал именно там,
    где он нужен для разбора.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = LlmClient(identity=identity)
        elif identity is not None and _default.meta != identity.as_meta():
            raise LlmUnavailable(
                "клиент процесса уже создан под другой оборот: "
                f"{sorted(_default.meta or {})} против {sorted(identity.as_meta())}. "
                "Один процесс — один оборот; для другого нужен свой клиент."
            )
        return _default


def close_default() -> None:
    """Закрыть клиент процесса. Для тестов и для аккуратного выхода."""
    global _default
    with _default_lock:
        client, _default = _default, None
    if client is not None:
        client.close()


def complete(
    messages: list[dict[str, Any]],
    *,
    identity: McpCallContext | None = None,
    **kwargs: Any,
) -> str:
    """Отправить сообщения и получить текст ответа. Простой метод.

    Args:
        messages: сообщения ``{role, content}``.
        identity: идентичность оборота. Без неё вызов уходит без ``_meta``,
            и сервер либо примет его по переходному окну, либо откажет кодом
            ``identity_missing`` — в зависимости от ``execution.require_call_meta``.
    """
    return default_client(identity).complete(messages, **kwargs)


def complete_json(
    messages: list[dict[str, Any]],
    *,
    identity: McpCallContext | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Как :func:`complete`, но ответ разбирается как JSON-объект."""
    return default_client(identity).complete_json(messages, **kwargs)


def embed(
    text: str,
    *,
    identity: McpCallContext | None = None,
    **kwargs: Any,
) -> list[float]:
    """Вернуть эмбеддинг текста."""
    return default_client(identity).embed(text, **kwargs)
