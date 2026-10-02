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
Для операций оборота (``finalize_turn``, ``fail_task``,
``merge_tool_delivery``) личность настоящая — сессия и пользователь задачи
известны. Для служебных (``claim_task``, ``queue_stats``, ``unstick_tasks``,
``release_claimed_tasks``) оборота ещё нет, и подставлять сессию пользователя
значило бы выдать служебный вызов за пользовательский.

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
"""

from __future__ import annotations

import json
from typing import Any

from lib.services.enterprise_mcp_client import CallIdentity

#: Префикс сессии служебного вызова. Помечает вызов как не связанный с
#: пользовательским оборотом — и для чтения в логах, и для возможной
#: политики шума на платформе.
SERVICE_SESSION_PREFIX = "task-worker"

#: Пользователь служебного вызова. Не существует как человек: обозначает
#: сам шлюз, а не того, кто что-то писал.
SERVICE_USER = "gateway"


class QueueOpsError(RuntimeError):
    """Ответ операции не удалось разобрать.

    Отдельный тип, потому что это не «сервер недоступен» и не доменная
    ошибка: связь жива, а формат ответа не тот, на который канал рассчитывал.
    Тихое ``{}`` на месте ответа выглядело бы как «задача не найдена» — то
    есть канал решил бы, что очередь пуста, и пошёл дальше.
    """


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

    async def claim_task(
        self,
        *,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Атомарно захватить одну задачу. ``None`` — очередь пуста."""
        payload = await self._invoke(
            "claim_task",
            {
                "error_retry_delay_sec": error_retry_delay_sec,
                "priority_contents": list(priority_contents or []) or None,
            },
            identity=self._service_identity(),
        )
        task = payload.get("claimed")
        if task is None:
            return None
        if not isinstance(task, dict):
            raise QueueOpsError(
                f"claim_task: claimed - не объект: {type(task).__name__}"
            )
        return task

    async def release_claimed_tasks(self, task_ids: list[str]) -> dict[str, Any]:
        """Вернуть незавершённые задачи в очередь при остановке."""
        return await self._invoke(
            "release_claimed_tasks",
            {"task_ids": list(task_ids)},
            identity=self._service_identity(),
        )

    async def unstick_tasks(
        self, *, processing_timeout_sec: float, max_stuck_retries: int
    ) -> list[str]:
        """Вернуть зависшие задачи в обработку. Счётчик растёт — они падали."""
        payload = await self._invoke(
            "unstick_tasks",
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
            "update_task_status",
            {"task_id": task_id, "status": status, "role": role},
            identity=self._turn_identity(
                session_id or f"task:{task_id}", user_id or SERVICE_USER
            ),
        )

    async def append_assistant_message(
        self, *, chat_id: str, reply_to: str, content: str = "", media: Any = None,
        metadata_patch: dict[str, Any] | None = None, user_id: str | None = None,
    ) -> str:
        """Создать заглушку ответа. Возвращает её ``id``."""
        payload = await self._invoke(
            "append_assistant_message",
            {
                "chat_id": chat_id,
                "reply_to": reply_to,
                "content": content,
                "media": media,
                "metadata_patch": metadata_patch,
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
            "patch_message_metadata",
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
            "merge_tool_delivery",
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
            "finalize_turn",
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
            "fail_task",
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
            "append_reasoning",
            {"assistant_msg_id": assistant_msg_id, "delta": delta},
            identity=self._turn_identity(
                session_id or f"assistant:{assistant_msg_id}",
                user_id or SERVICE_USER,
            ),
        )

    async def get_message(self, task_id: str) -> dict[str, Any] | None:
        """Прочитать строку очереди. ``None`` — строки нет."""
        payload = await self._invoke(
            "get_message",
            {"task_id": task_id},
            identity=self._service_identity(),
        )
        message = payload.get("message")
        return message if isinstance(message, dict) else None

    async def queue_stats(self) -> dict[str, int]:
        """Размер очереди для вывода активности."""
        payload = await self._invoke(
            "queue_stats", {}, identity=self._service_identity()
        )
        return {
            "pending": int(payload.get("pending") or 0),
            "error": int(payload.get("error") or 0),
        }
