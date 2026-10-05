"""Операции очереди задач через ``enterprise-mcp`` — единственный шов канала.

До этого изменения канал держал собственный пул PostgreSQL и писал в
``agent_conversation_messages`` сам. Один и тот же ресурс значился за двумя
владельцами: платформа считала себя единственным владельцем пула, а агент
молча брал второй. Меняется и владелец данных, и то, кто решает, что
произойдёт с задачей.

Здесь три вещи, которых больше нет в канале:

**Разбор ответа.** Операция отвечает JSON-текстом; доменный вид в канале
не появляется сам.

**Идентичность вызова.** Сервер требует ``params._meta`` на каждом вызове.
Для операций оборота (``data.finalize_turn``, ``data.fail_task``,
``data.merge_tool_delivery``) личность настоящая — сессия и пользователь задачи
известны. Для служебных (``data.claim_task``, ``data.queue_stats``,
``data.unstick_tasks``, ``data.release_claimed_tasks``) оборота ещё нет, и
подставлять сессию пользователя значило бы выдать служебный вызов за
пользовательский.

**Разделение этих двух случаев.** Служебный вызов подписывается личностью
воркера: ``session_id`` вида ``task-worker:<id>``, ``user_id`` — ``gateway``.
Это не выдуманная сессия, а честное имя того, кто зовёт. Сервер сам помечает
вызовы, чей ``request_id`` не ссылается на реальный оборот, как
``correlated = false`` — механизм для таких случаев уже есть, и выдумывать
собственный второй не нужно.

Атомарность здесь не собирается заново: она обеспечена на платформе, по одной
транзакции на операцию. Именно поэтому операций шесть, а не пятнадцать: две
транзакции, разнесённые на вызовы, тихо разрушают то, ради чего и
затевался перенос.

**Форма ответа захвата объявлена, а не угадана.** Поле ``claimed`` платформа
возвращает **списком всегда**, в том числе при ``batch = 1``: одиночный захват
у неё — представление ``claim_tasks(batch=1)``, а не отдельная форма ответа.
Потребитель при этом получает словарь задачи, как и раньше, и это преобразование
делает :class:`QueueOps`, а не платформа: форма ответа известна в одной точке, и
каждый новый потребитель не разбирает её заново.

Держать словарь и список одновременно «на всякий случай» здесь нельзя. Разбор
словаря в списке означал бы, что угодно, что не список, считается задачей: опрос
увёл бы в обработку мусор, и заметить это можно было бы только по содержимому
ответа. Не-list отвергается поимённо (:meth:`QueueOps._claimed_tasks`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lib.services.enterprise_mcp_client import CallIdentity
from lib.services.service_identity import QUEUE_WORKER

#: Префикс сессии служебного вызова. Помечает вызов как не связанный с
#: пользовательским оборотом — и для чтения в логах, и для возможной
#: политики шума на платформе.
SERVICE_SESSION_PREFIX = "task-worker"

#: Пользователь служебного вызова. Не существует как человек: это имя
#: опросчика очереди, а не того, кто что-то писал. Раньше на всех компонентах
#: стоял один ``gateway``, и 97 % строк журнала были подписаны им — по журналу
#: было не видно, кто что делал. Имя объявлено один раз в
#: ``lib/services/service_identity.py``.
SERVICE_USER = QUEUE_WORKER


class QueueOpsError(RuntimeError):
    """Ответ операции не удалось разобрать.

    Отдельный тип, потому что это не «сервер недоступен» и не доменная
    ошибка: связь жива, а формат ответа не тот, на который канал рассчитывал.
    Тихое ``{}`` на месте ответа выглядело бы как «задача не найдена» — то
    есть канал решил бы, что очередь пуста, и пошёл дальше.
    """


@dataclass(frozen=True)
class ClaimedBatch:
    """Выдача захвата: задачи пачки и курсор продолжения.

    ``next_cursor`` заполнен только по полному батчу (``data.claim_task`` на
    платформе, ``capabilities/data/service/main.py::ClaimedBatch``) — то есть
    это сигнал «есть ещё». ``None`` на неполном батче означает «очередь дошла
    до конца, следующий опрос начинай с головы».
    """

    tasks: list[dict[str, Any]]
    next_cursor: str | None = None


class QueueOps:
    """Вызовы операций очереди задач от имени канала.

    Attributes:
        _client: живой клиент ``enterprise-mcp`` (``None`` — тесты и
            standalone: все методы поднимают ``QueueOpsError``, чтобы
            отсутствие транспорта не выглядело как пустая очередь).
        _worker_id: номер воркера в листе идентичности служебных вызовов.
    """

    def __init__(self, client: Any | None, *, worker_id: str = "0") -> None:
        self._client = client
        self._worker_id = str(worker_id)

    @property
    def available(self) -> bool:
        """Есть ли вообще чем звать платформу."""
        return self._client is not None

    # ------------------------------------------------------------------
    # вызов
    # ------------------------------------------------------------------

    async def _invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        identity: CallIdentity | None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise QueueOpsError(
                f"{operation}: нет клиента enterprise-mcp — канал не может "
                f"обслуживать очередь"
            )
        try:
            text = await self._client.call(operation, arguments, identity=identity)
        except Exception as exc:  # noqa: BLE001 - домен и транспорт едины здесь
            raise QueueOpsError(f"{operation}: вызов не удался: {exc}") from exc
        return self._parse(operation, text)

    @staticmethod
    def _parse(operation: str, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise QueueOpsError(
                f"{operation}: ответ не разобран как JSON: {text[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise QueueOpsError(
                f"{operation}: ответ - не объект: {type(payload).__name__}"
            )
        return payload

    def _service_identity(self) -> CallIdentity:
        """Личность служебного вызова: воркер шлюза, а не пользователь.

        Подставлять сюда сессию задачи нельзя даже там, где она известна:
        вызов без оборота подписан сессией оборота, и в журнале служебная
        правка выглядит как действие пользователя.
        """
        return CallIdentity(
            session_id=f"{SERVICE_SESSION_PREFIX}:{self._worker_id}",
            user_id=SERVICE_USER,
        )

    def _turn_identity(self, session_id: str, user_id: str) -> CallIdentity:
        """Личность оборота: сессия и пользователь задачи."""
        return CallIdentity(session_id=session_id, user_id=user_id)

    # ------------------------------------------------------------------
    # захват и откат
    # ------------------------------------------------------------------

    async def claim_tasks(
        self,
        *,
        batch: int = 1,
        cursor: str | None = None,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | None = None,
    ) -> ClaimedBatch:
        """Атомарно захватить до ``batch`` задач, начиная с ``cursor``.

        ``batch`` и ``cursor`` уходят в вызов **явно**, а не подставляются под
        умолчание платформы: молчаливое умолчание означало бы, что размер
        пачки — решение, о котором в коде не знает никто, и что однажды
        поменяется без предупреждения. Границы значения проверяет платформа
        (``1 <= batch <= 100``); дублировать их здесь нельзя — второе место,
        где правило могло бы разойтись с платформой, и есть тот самый класс
        расхождений, который этот класс ошибок заменил.

        Возвращает пачку (список задач + курсор продолжения). Имена задач
        приходят от платформы: канал их не выбирает, поэтому в аргументах
        никаких имён нет (см. модульную строку).
        """
        payload = await self._invoke(
            "data.claim_task",
            {
                "error_retry_delay_sec": error_retry_delay_sec,
                # null — «без фильтра по содержимому», и схема
                # ``data.claim_task`` его принимает: поле объявлено как
                # ``["array", "null"]``. Отправлять здесь ``[]`` нельзя:
                # платформа добавит ``AND content = ANY(%s)``, и отбор не
                # найдёт ничего, то есть очередь молча покажется пустой.
                "priority_contents": list(priority_contents or []) or None,
                "batch": batch,
                "cursor": cursor,
            },
            identity=self._service_identity(),
        )
        return ClaimedBatch(
            tasks=self._claimed_tasks(payload),
            next_cursor=self._next_cursor(payload),
        )

    async def claim_task(
        self,
        *,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Атомарно захватить одну задачу. ``None`` — очередь пуста.

        Прежний контракт потребителя сохранён целиком: **одна** задача или
        пусто. Это представление ``claim_tasks(batch=1)``, а не отдельный
        вызов с отдельной формой ответа: словарь задачи, который возвращался
        здесь всегда, теперь берётся первым элементом платформенного списка.
        """
        batch = await self.claim_tasks(
            batch=1,
            error_retry_delay_sec=error_retry_delay_sec,
            priority_contents=priority_contents,
        )
        return batch.tasks[0] if batch.tasks else None

    @staticmethod
    def _claimed_tasks(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Разобрать ``claimed``: **всегда список**.

        Форма платформы — список, в том числе при ``batch=1``. Прежний разбор
        ждал словарь или ``None``, то есть форму, которой у платформы нет:
        подставной клиент в тестах отдавал словарь, поэтому расхождение не
        всплывало, а на боевом вызове поднимало ``QueueOpsError`` — опрос не
        проходил вовсе. Словарь отвергается поимённо, а не принимается за
        задачу: иначе следующая смена формы снова прошла бы молча.
        """
        claimed = payload.get("claimed")
        if claimed is None:
            # Ключа нет вовсе: операция отработала вхолостую. Это не форма
            # ответа, а пустая очередь, и она норма.
            return []
        if not isinstance(claimed, list):
            raise QueueOpsError(
                "claim_task: claimed - "
                f"{type(claimed).__name__}, а платформа отдаёт claimed "
                "списком всегда, в том числе при batch=1; словарь — форма, "
                "которой у платформы нет"
            )
        for index, task in enumerate(claimed):
            if not isinstance(task, dict):
                raise QueueOpsError(
                    f"claim_task: claimed[{index}] - не объект: "
                    f"{type(task).__name__}"
                )
        return list(claimed)

    @staticmethod
    def _next_cursor(payload: dict[str, Any]) -> str | None:
        """Разобрать курсор продолжения: строка либо «продолжения нет»."""
        cursor = payload.get("next_cursor")
        if cursor is None:
            return None
        if not isinstance(cursor, str):
            raise QueueOpsError(
                "claim_task: next_cursor - не строка: "
                f"{type(cursor).__name__}"
            )
        return cursor or None

    async def release_claimed_tasks(self, task_ids: list[str]) -> dict[str, Any]:
        """Вернуть незавершённые задачи в очередь при остановке."""
        return await self._invoke(
            "data.release_claimed_tasks",
            {"task_ids": list(task_ids)},
            identity=self._service_identity(),
        )

    async def unstick_tasks(
        self, *, processing_timeout_sec: float, max_stuck_retries: int
    ) -> list[str]:
        """Вернуть зависшие задачи в обработку. Счётчик растёт — они падали."""
        payload = await self._invoke(
            "data.unstick_tasks",
            {
                "processing_timeout_sec": processing_timeout_sec,
                "max_stuck_retries": max_stuck_retries,
            },
            identity=self._service_identity(),
        )
        recovered = payload.get("recovered") or []
        if not isinstance(recovered, list):
            raise QueueOpsError(
                f"unstick_tasks: recovered - не список: {type(recovered).__name__}"
            )
        return [str(x) for x in recovered]

    # ------------------------------------------------------------------
    # оборот
    # ------------------------------------------------------------------

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        role: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Сменить статус задачи.

        ``session_id``/``user_id`` обязательны для любой задачи: запись идёт
        по чужой строке, и подписать её служебной личностью значило бы
        убрать из журнала, кто именно её тронул.
        """
        return await self._invoke(
            "data.update_task_status",
            {"task_id": task_id, "status": status, "role": role},
            identity=self._turn_identity(
                session_id or f"task:{task_id}", user_id or SERVICE_USER
            ),
        )

    async def append_assistant_message(
        self, *, chat_id: str, reply_to: str, content: str = "", media: Any = None,
        user_id: str | None = None,
    ) -> str:
        """Создать заглушку ответа. Возвращает её ``id``.

        ``metadata_patch`` здесь больше не отправляется: операция не знает такого
        параметра ни в объявленной схеме
        (``servers/enterprise/capabilities/data/tools/append_assistant_message.py``),
        ни в сигнатуре обработчика, ни в ``DataService.append_assistant_message``.
        Пока схема строилась из подписи обработчика, лишний ключ не доходил до
        вызова, и намерение канала просто терялось; как только объявленная схема
        стала исполняемой, вызов начал отвергаться целиком
        (``unexpected keyword argument 'metadata_patch'``) — и заодно вскрылся
        тот самый случай «дефект, скрытый мёртвой подсистемой». Патчить метаданные
        заглушки нечего: она создаётся пустой, а содержимое и метаданные пишутся
        позже через ``data.update_task_status``, где ``metadata_patch`` законен.
        """
        payload = await self._invoke(
            "data.append_assistant_message",
            {
                "chat_id": chat_id,
                "reply_to": reply_to,
                "content": content,
                "media": media,
            },
            identity=self._turn_identity(f"chat:{chat_id}", user_id or SERVICE_USER),
        )
        msg_id = payload.get("assistant_msg_id")
        if not msg_id:
            raise QueueOpsError("append_assistant_message: ответ без id заглушки")
        return str(msg_id)

    async def patch_message_metadata(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        role: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Слить ``patch`` в ``metadata`` сообщения."""
        return await self._invoke(
            "data.patch_message_metadata",
            {"task_id": task_id, "patch": patch, "role": role},
            identity=self._turn_identity(
                session_id or f"task:{task_id}", user_id or SERVICE_USER
            ),
        )

    async def merge_tool_delivery(
        self,
        assistant_msg_id: str,
        *,
        content: str = "",
        metadata_patch: dict[str, Any] | None = None,
        buttons: list[Any] | None = None,
        media: list[Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Дописать промежуточную доставку, не закрывая ответ."""
        return await self._invoke(
            "data.merge_tool_delivery",
            {
                "assistant_msg_id": assistant_msg_id,
                "content": content,
                "metadata_patch": metadata_patch,
                "buttons": buttons,
                "media": media,
            },
            identity=self._turn_identity(
                session_id or f"assistant:{assistant_msg_id}",
                user_id or SERVICE_USER,
            ),
        )

    async def finalize_turn(
        self,
        user_msg_id: str,
        assistant_msg_id: str,
        *,
        content: str = "",
        metadata_patch: dict[str, Any] | None = None,
        buttons: list[Any] | None = None,
        media: list[Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Закрыть оборот. ``outcome`` различает запись и отмену."""
        return await self._invoke(
            "data.finalize_turn",
            {
                "user_msg_id": user_msg_id,
                "assistant_msg_id": assistant_msg_id,
                "content": content,
                "metadata_patch": metadata_patch,
                "buttons": buttons,
                "media": media,
            },
            identity=self._turn_identity(
                session_id or f"task:{user_msg_id}", user_id or SERVICE_USER
            ),
        )

    async def fail_task(
        self,
        user_msg_id: str,
        assistant_msg_id: str | None,
        reason: str,
        max_stuck_retries: int,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Пометить оборот ошибочным, увеличив счётчик попыток."""
        return await self._invoke(
            "data.fail_task",
            {
                "user_msg_id": user_msg_id,
                "assistant_msg_id": assistant_msg_id,
                "reason": reason,
                "max_stuck_retries": max_stuck_retries,
            },
            identity=self._turn_identity(
                session_id or f"task:{user_msg_id}", user_id or SERVICE_USER
            ),
        )

    # ------------------------------------------------------------------
    # чтения
    # ------------------------------------------------------------------

    async def append_reasoning(
        self, assistant_msg_id: str, delta: str, *, session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Дописать кусок рассуждений к ответу.

        Конкатенация выполняется на платформе, поэтому два параллельных
        сброса не затирают друг друга. Обратный переход на чтение и запись
        в агенте потребовал бы блокировки, то есть признания гонки.
        """
        return await self._invoke(
            "data.append_reasoning",
            {"assistant_msg_id": assistant_msg_id, "delta": delta},
            identity=self._turn_identity(
                session_id or f"assistant:{assistant_msg_id}",
                user_id or SERVICE_USER,
            ),
        )

    async def get_message(self, task_id: str) -> dict[str, Any] | None:
        """Прочитать строку очереди. ``None`` — строки нет."""
        payload = await self._invoke(
            "data.get_message",
            {"task_id": task_id},
            identity=self._service_identity(),
        )
        message = payload.get("message")
        return message if isinstance(message, dict) else None

    async def queue_stats(self) -> dict[str, int]:
        """Размер очереди для вывода активности."""
        payload = await self._invoke(
            "data.queue_stats", {}, identity=self._service_identity()
        )
        return {
            "pending": int(payload.get("pending") or 0),
            "error": int(payload.get("error") or 0),
        }
