"""McpIdentityHook — подстановка личности вызова в аргументы MCP-операции.

Навык ``McpIdentityHook`` — framework-хук уровня агента. Единственная
задача: перед вызовом любой операции ``mcp_enterprise_*`` дописать в словарь
аргументов ``session_id``, ``user_id`` и ``request_id`` текущего оборота.
``request_id`` оборота при этом необязателен: если вопроса в обороте нет
(неочередной канал — webUI, CLI), вызывающая сторона досылает самостоятельное
значение (требование «Вызывающая сторона модели подставляет личность хуком»
спеки ``runtime/call-contract``). Отсутствие ``session_id`` или ``user_id`` —
другое дело: подставлять нечего, и отказ поднимает **сам хук**, до похода в
платформу (:class:`McpIdentityRefused`), так что модель получает читаемую
причину отказа, а не внутренний код платформы ``identity_missing``, где
«попробовать иначе» бессмысленно: личности у вызова не будет и в следующий раз.

**Зачем это нужно.** Платформа исполняет операцию в изоляции вызвавшего:
журнал событий, каталог сессии и корпоративные выборки строятся по личности
вызова, поэтому вызов без неё отвергается кодом ``identity_missing``
(``mcp-platform/libs/enterprise_common/execution/context.py``). Модель эти
значения не знает и знать не должна: подставленная моделью сессия выглядела
бы в журнале как настоящая.

**Почему отказ поднимает хук, а не платформа.** Тот же приём, что и у отказа
редиректа файлов сессии (``SessionFileRedirectBlocked``): тип отказа — подкласс
``RepeatGuardBlocked``, который ловит уже существующий патч
``repeat_guard_block`` (``lib/services/runtime_patcher.py``). Молча пропущенный
вызов уезжал в сеть без личности, платформа отвергала его ``identity_missing``,
и модель получала внутренний код, где ничего нельзя поменять.

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

from lib.hooks.repeat_guard_hook import RepeatGuardBlocked
from lib.services.turn_identity import (
    IDENTITY_KEYS,  # noqa: F401 - реэкспорт: контракт сверяют тесты платформы
    call_identity,
    new_envelope_request_id,
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


class McpIdentityRefused(RepeatGuardBlocked):
    """Вызов операции платформы отвергнут: у него нет личности оборота.

    Подкласс, а не новый тип — ровно тот приём, что уже применён к отказу
    редиректа файлов сессии (``SessionFileRedirectBlocked``): превращать отказ
    в синтетический результат инструмента умеет существующий патч
    ``repeat_guard_block``, и он ловит ``RepeatGuardBlocked``; подкласс
    ловится тем же предложением ``except``. Второй механизм отказа означал бы,
    что патч придётся расширять на каждый новый повод — и что при его
    отсутствии этот отказ, в отличие от отказа защитника от повторов, дойдёт
    до диспетчера хуков и уронит весь оборот.

    Имя типа обязано отличаться от ``RepeatGuardBlocked``: по журналу читатель
    обязан различать отказ защитника от повторов и отказ по личности, не
    разбирая текст.
    """


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
        # ``reraise``: без него диспетчер хуков проглатывает исключение и
        # логирует его — отказ стал бы молчаливым no-op, который при этом
        # выглядит работающим (см. ``SessionFileRedirectHook``).
        super().__init__(reraise=True)
        self._db_logging_service = db_logging_service
        self._tool_prefix = tool_prefix
        # Счётчик досылок — для строки на уровне ``debug``. Публичным он не
        # сделан: читателю достаточно увидеть в журнале, что досылка была, а
        # число проверяется через ``caplog``.
        self._generated_request_ids = 0

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
        tool_name = self._tool_name(tool_call, tool)
        if not tool_name.startswith(self._tool_prefix):
            return

        identity = self._identity(context)
        if identity is None:
            # Отказ, а не пропуск. Молча уехавший вызов платформа отвергает
            # внутренним кодом ``identity_missing``, и модель получает код без
            # смысла: «попробовать иначе» там невозможно. Этот текст увидит
            # модель в синтетическом результате патча, поэтому он обязан
            # называть, чего не хватает, и говорить, что повтор не поможет.
            reason = (
                "вызов операции платформы вне оборота: не хватает личности "
                "вызова — session_id и/или user_id (сессии и/или отправителя). "
                "Подставлять нечего: выдуманные значения в журнале выглядели "
                "бы как настоящий вход чужого пользователя. Повтор этого "
                "вызова не поможет — личность есть только у вызова внутри "
                "оборота. Скажи пользователю, что операция недоступна вне "
                "оборота, и предложи обычные инструменты (работа с файлами, "
                "поиск)."
            )
            # ``error`` на КАЖДЫЙ отказ: прежнее «одно на процесс» после первого
            # раза переставало различать один отказ и отказов двести.
            logger.error("McpIdentityHook: отвергнут вызов %s — %s", tool_name, reason)
            raise McpIdentityRefused(
                reason,
                context=context,
                tool_call=tool_call,
                tool=tool,
                params=params,
            )

        # Присваивание, а не setdefault: см. докстринг модуля.
        params.update(identity)

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _identity(self, context: Any) -> dict[str, str] | None:
        """Личность текущего оборота, а ``None`` — только когда её не из чего собрать.

        То есть ``None`` означает одно: нет ни сессии, ни отправителя, то есть
        вызов идёт вне оборота вообще. Неполной личности не бывает: отсутствие
        ``request_id`` оборота закрывается досылкой ниже.

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
            # ``request_id`` оборота у вызывающей стороны НЕ обязателен, и это
            # не поломка: очередь — единственный канал, который кладёт
            # идентификатор вопроса в метаданные входящего сообщения, поэтому
            # на webUI или в CLI вопроса в обороте просто нет. Отказ здесь делал
            # MCP неработающим на этих каналах целиком — платформа отвергала
            # вызов с ``identity_missing``.
            #
            # Отсутствие ``request_id`` — НЕ то же самое, что отсутствие
            # отправителя или сессии (см. ветку выше): сессию и отправителя
            # выдумать нельзя, а идентификатор конверта вызывающая сторона
            # создать вправе и обязана. Значение берётся у владельца правила —
            # ``new_envelope_request_id`` в ``lib/services/turn_identity.py``,
            # оттуда же его берёт клиент фоновых служб.
            generated = new_envelope_request_id()
            self._generated_request_ids += 1
            # Факт досылки виден один раз за процесс, дальше — счётчиком:
            # «вызов уехал с личностью» и «вызов уехал без неё» по журналу
            # неразличимы, а различать приходится читателю.
            if self._generated_request_ids == 1:
                logger.info(
                    "McpIdentityHook: у оборота нет request_id, досылаю "
                    "самостоятельный — канала без вопроса (webUI, CLI) у "
                    "вызова нет. Дальше — счётчиком, подробности на debug"
                )
            logger.debug(
                "McpIdentityHook: подставлен самостоятельный request_id=%s "
                "(всего %d за процесс)",
                generated,
                self._generated_request_ids,
            )
            request_id = generated

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
