"""McpIdentityHook — подстановка личности вызова в аргументы MCP-операции.

Навык ``McpIdentityHook`` — framework-хук уровня агента. Единственная
задача: перед вызовом любой операции ``mcp_enterprise_*`` дописать в словарь
аргументов ``session_id``, ``user_id`` и ``request_id`` текущего оборота.

**Зачем это нужно.** Платформа исполняет операцию в изоляции вызвавшего:
журнал событий, каталог сессии и корпоративные выборки строятся по личности
вызова, поэтому вызов без неё отвергается кодом ``identity_missing``
(``mcp-platform/libs/enterprise_common/execution/context.py``). Модель эти
значения не знает и знать не должна: подставленная моделью сессия выглядела
бы в журнале как настоящая.

**Почему аргументами, а не ``params._meta``.** Нанобот зовёт MCP так:
``MCPToolWrapper.execute`` → ``session.call_tool(name, arguments=kwargs)``
(``nanobot/agent/tools/mcp.py:633``). Параметра ``meta=`` в этом вызове нет,
и штатной фабрики сессии или middleware, который его добавил бы, в наноботе
тоже нет. Единственный канал, доступный хуку, — сам словарь ``params``, и он
неизменяемым быть не должен. Ключи идентичности приходят к серверу как
аргументы и **вырезаются им до вызова домена**
(``execution/pipeline.py:297`` — ``LEGACY_IDENTITY_KEYS``), поэтому операция
лишних полей не видит.

Чтобы этот путь был открыт, у платформы включён переходный режим:
``platform.json → execution.require_call_meta = false``. Проверено пробой —
при ``true`` тот же вызов отвергается ``identity_missing``, при ``false``
проходит и возвращает подставленную личность в ``_execution``.

**Почему значения перезаписываются, а не дописываются.** В опубликованной
схеме операции таких полей нет, поэтому в аргументах они могут появиться
только снизу — от модели. Молчаливый ``setdefault`` превратил бы такую
подстановку в средство выдать себя за другого пользователя, поэтому
присваивание безусловное.

Подробности перехода — ``openspec/changes/2026-10-03-mcp-native-tools/``.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobot.agent import AgentHook

from lib.services.turn_identity import (
    IDENTITY_KEYS,  # noqa: F401 - реэкспорт: контракт сверяют тесты платформы
    call_identity,
    read_turn_context,
    request_id_from,
)

logger = logging.getLogger(__name__)

#: Префикс имён операций платформы в реестре нанобота: сервер объявлен в
#: ``config.json → tools.mcpServers`` под именем ``enterprise``, а нанобот
#: склеивает ``mcp_<сервер>_<операция>``. Хук трогает только их: чужие
#: MCP-серверы агента не обязаны знать про ``LEGACY_IDENTITY_KEYS``.
MCP_TOOL_PREFIX = "mcp_enterprise_"

#: ``IDENTITY_KEYS`` приходит из ``lib/services/turn_identity.py`` — рядом с
#: той сборкой, которая из него делает словарь. Отдельное объявление здесь
#: было бы второй копией контракта, и страж
#: ``tests/test_mcp_platform_declaration.py`` сверял бы с платформой ту
#: копию, которая перестала быть используемой.


class McpIdentityHook(AgentHook):
    """Подставляет личность оборота в аргументы вызова операции платформы.

    Состояния не хранит: личность читается из контекста текущего вызова, а не
    из полей инстанса. Поэтому один инстанс обслуживает все обороты
    одновременно, и per-turn фабрика не нужна — в отличие от
    ``DatabaseLoggingHook``, у которого состояние вопроса по натуре общее.
    """

    def __init__(
        self,
        db_logging_service: Any = None,
        *,
        tool_prefix: str = MCP_TOOL_PREFIX,
    ) -> None:
        super().__init__()
        self._db_logging_service = db_logging_service
        self._tool_prefix = tool_prefix
        # Предупреждение о ненайденном request_id повторялось бы на каждом
        # вызове оборота и забило журнал одинаковыми строками, поэтому оно
        # одно на процесс.
        self._warned_incomplete = False

    # ------------------------------------------------------------------
    # AgentHook
    # ------------------------------------------------------------------

    async def before_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        if not isinstance(params, dict):
            return
        if not self._tool_name(tool_call, tool).startswith(self._tool_prefix):
            return

        identity = self._identity(context)
        if identity is None:
            # Подставлять нечего. Вызов уйдёт без личности и будет отвергнут
            # платформой с ``identity_missing`` — это внятнее, чем выдуманная
            # сессия, которая выглядела бы настоящей в журнале.
            self._warn_once()
            return

        # Присваивание, а не setdefault: см. докстринг модуля.
        params.update(identity)

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _identity(self, context: Any) -> dict[str, str] | None:
        """Личность текущего оборота или ``None``, если она неполна.

        Расчёт стоит в ``lib/services/turn_identity.py`` вместе с чтением
        ``RequestContext`` и с носителем записи. Прежняя копия была ещё и в
        клиенте платформы, и держались две копии только комментарием друг об
        друге: разошлись бы — подпись в журнале и содержимое файла сессии
        описали бы разные вызовы, и нигде бы это не было видно.
        """
        turn = read_turn_context()
        # Прямое поле хука важнее contextvar: ``AgentHookContext.session_key``
        # приходит аргументом и есть даже у вызова вне итерации.
        raw_key = getattr(context, "session_key", None)
        session_id = (
            raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
        )
        if not session_id and turn is not None:
            session_id = turn.session_key
        user_id = turn.user_id if turn is not None else None
        if not session_id or not user_id:
            return None

        request_id = request_id_from(self._db_logging_service, session_id)
        if not request_id:
            # Сервер требует все три ключа, а ``request_id`` — PK оборота в
            # ``agent_question_runs``. Нет оборота — нет и связи события с
            # вопросом; подставлять ``local-*`` значило бы привязать вызов к
            # несуществующему вопросу.
            return None

        return call_identity(session_id, user_id, request_id)

    @staticmethod
    def _tool_name(tool_call: Any, tool: Any) -> str:
        """Имя вызываемого tool'а.

        Сначала ``tool_call`` — это то, что выбрала модель, то есть уже
        обёрнутое имя ``mcp_enterprise_*``. Сам ``tool`` — запасной путь.
        """
        name = getattr(tool_call, "name", None)
        if isinstance(name, str) and name:
            return name
        name = getattr(tool, "name", None)
        return name if isinstance(name, str) else ""

    def _warn_once(self) -> None:
        if self._warned_incomplete:
            return
        self._warned_incomplete = True
        logger.warning(
            "McpIdentityHook: личность оборота неполна, вызовы mcp_enterprise_* "
            "уйдут без неё и будут отвергнуты платформой (identity_missing)"
        )
