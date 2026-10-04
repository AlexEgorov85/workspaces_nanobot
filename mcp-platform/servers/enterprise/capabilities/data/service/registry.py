"""Реестр операций данных по аудитории вызова — место, где классы записаны.

Класс работы в пуле определяется аудиторией, объявленной в сигнатуре метода
:class:`~servers.enterprise.capabilities.data.service.main.DataService`, и
ничем иным. Этот модуль — единственное место, где эти классы перечислены
поимённо, и он нужен не сервису, а сверке: пока класс не записан здесь, его
не видно ни в одном обзоре, а «забыл внести в список» — это молчаливое
изменение приоритета операции.

Почему реестр, а не чтение сигнатур: сигнатуры разъезжаются молча. Право
исправить опечатку в имени метода есть, права забыть внести его в список —
тоже, и второе не оставляет следов. Реестр нужен именно для того, чтобы
расхождение было видно: **каждая запись обязана соответствовать существующему
методу** (мёртвая запись — дефект реестра, а не безобидная запасная строка),
**и каждая операция, трогающая пул, обязана в нём быть**.

Значения берутся из :mod:`libs.enterprise_data.audience` — определение имён
аудиторий одно, и capability не заводит второго. Пул, в свою очередь, знает,
какие бывают аудитории, а какая операция к какой относится, — не его дело.
"""

from __future__ import annotations

from libs.enterprise_data.audience import JOB_AUDIENCE_MODEL, JOB_AUDIENCE_RUNTIME

#: Имя метода ``DataService`` → аудитория его работы в пуле.
#:
#: Ключ — имя метода, а не имя операции платформы: право на вызов и класс
#: работы решаются в одном месте, и разойтись им здесь негде. Метод с
#: ``self.submit`` / ``self.submit_transaction``, которого в списке нет, —
#: это работа без класса: пул не знает, чья она, и обслуживает её наравне
#: со всем остальным.
OPERATION_AUDIENCE: dict[str, str] = {
    # Модель ходит в базу за историей и за проверкой схемы: обе операции
    # читают, обе ограничены потолком времени своего класса, и именно они
    # не должны ждать работу системы.
    "history_search": JOB_AUDIENCE_MODEL,
    "schema_check": JOB_AUDIENCE_MODEL,
    # Журнал: внутренние потоки процесса, модель сюда не ходит.
    "_write_events": JOB_AUDIENCE_RUNTIME,
    # Очередь задач и контекст оборота — служебные операции канала.
    # ``claim_tasks`` — батчевый захват, а ``claim_task`` — его представление
    # для одной задачи. Оба зовут пул (второй через первый), и оба обязаны
    # быть здесь поимённо: класс работы у очереди один, а запись без
    # поимённой строки выглядит как готовый реестр, в котором её нет.
    "claim_task": JOB_AUDIENCE_RUNTIME,
    "claim_tasks": JOB_AUDIENCE_RUNTIME,
    "update_task_status": JOB_AUDIENCE_RUNTIME,
    "unstick_tasks": JOB_AUDIENCE_RUNTIME,
    "release_claimed_tasks": JOB_AUDIENCE_RUNTIME,
    "fail_task": JOB_AUDIENCE_RUNTIME,
    "queue_stats": JOB_AUDIENCE_RUNTIME,
    # Журнал и контекст прогона: то же, что выше, — только вызывается иначе.
    "upsert_question_run": JOB_AUDIENCE_RUNTIME,
    "purge_logs": JOB_AUDIENCE_RUNTIME,
    # Путь оборота: сообщения задачи, доставка, рассуждение, уведомление.
    "append_assistant_message": JOB_AUDIENCE_RUNTIME,
    "delete_assistant_message": JOB_AUDIENCE_RUNTIME,
    "patch_message_metadata": JOB_AUDIENCE_RUNTIME,
    "merge_tool_delivery": JOB_AUDIENCE_RUNTIME,
    "append_reasoning": JOB_AUDIENCE_RUNTIME,
    "append_history_notice": JOB_AUDIENCE_RUNTIME,
    "get_message": JOB_AUDIENCE_RUNTIME,
    "finalize_turn": JOB_AUDIENCE_RUNTIME,
    # Зеркало сессии. Две операции идут через аренду соединения
    # (``submit_transaction``), но класс у них тот же: это работа системы.
    "mirror_session": JOB_AUDIENCE_RUNTIME,
    "cleanup_session_mirror": JOB_AUDIENCE_RUNTIME,
    "session_mirror_state": JOB_AUDIENCE_RUNTIME,
}
