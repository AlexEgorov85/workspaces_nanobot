"""MCP-сервер Follow Up: инструменты навыка для единого окна.

Запускается не нами, а агентом. Его инструменты
(`workspace/tools/follow_up.py` в репозитории агента) при старте gateway
поднимают лаунчер навыка, тот — этот сервер тем же Python, и держат с ним
MCP-сессию по stdio, пока живёт gateway. Отдельного развёртывания, порта и
супервизора нет — по этой причине транспорт и выбран.

    python -m backend.skill.mcp_server

У агента инструменты называются `mcp_follow_up_<инструмент>`, их описания
и схемы — отсюда (`_TOOLS`; сборщик навыка кладёт их в `tools.json`), и
ровно эти имена стоят в SKILL.md навыка.

Время: `ask` и `hypotheses` синхронны и укладываются в дедлайн хода
(`turn_deadline_sec`, 150 с); инструменты агента ждут до 240 с. Карточка
исполнения строится минутами и потому идёт задачей с опросом
(`card_start` → `card_status`).

Модели грузятся при первом обращении, а не при старте: агент поднимает
дочерние процессы на своём старте, и три гигабайта в этот момент задержали
бы его на всех остальных.

**Про второй процесс над той же базой.** Локальная SQLite открыта с
`vfs=unix-none` — блокировки отключены целиком (это лечило зависания на NFS
DataLab), а от гонок защищает `storage/writer.write_tx`, обычный
`threading.RLock`. Он в пределах процесса. Значит два процесса, пишущих в
один файл, портят не запись, а базу: пишут в общий rollback-журнал, ничего
друг о друге не зная.

Собственный сервер Follow Up помечает файл своим (`writer.claim_db_owner`)
на старте. Этот процесс делает то же самое и, если файл занят живым
процессом, отказывается стартовать: пустой чат из-за понятного отказа лучше
испорченного корпуса, который обнаружится через неделю.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

SERVER_NAME = "follow_up"

# Ключ сессии, если агент его не передал. У нанобота модель не видит свой
# ключ сессии в промпте (проверено по `ContextBuilder.build_system_prompt`),
# и требовать его — значит получать выдуманные значения или отказ. Общий
# ключ хуже личного: память диалога у всех, кто его не передал, одна на
# всех. Но это деградация, а не поломка: контекст беседы у агента свой, наша
# память — второй эшелон.
DEFAULT_SESSION_KEY = "nanobot:shared"

# Описания уходят в промпт агента — это они решают, выберет он нас или
# соседний навык. Границу с audit_analyzer держим явной: «текст акта и
# цитата — сюда, агрегаты по витринам — туда».
#
# Здесь же живёт указание отдавать текст дословно. В плане оно было отведено
# `SKILL.md`, но по пути MCP никакого SKILL.md нет: агент видит только имя,
# описание и схему. Значит единственные два места, куда его можно положить, —
# описание инструмента и сам результат (`skill/render.RELAY_NOTE`).
_VERBATIM = (" Ответ возвращается готовым текстом в поле answer_md — "
             "выводить его пользователю дословно, без пересказа.")
_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "ask",
        "description": (
            "Вопрос по корпусу актов проверок ОАРБ: отклонения конкретной КМ, "
            "в каких актах встречается тема или человек, детализация проверки, "
            "цитаты из акта. Отвечает готовым текстом со ссылками на источники. "
            "НЕ для агрегатов и произвольного SQL по витринам аудита — это "
            "audit_analyzer."
            + _VERBATIM
        ),
        "schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "вопрос аудитора как есть"},
                "session_key": {"type": "string",
                                "description": "идентификатор беседы — им "
                                               "держится память диалога; "
                                               "инструменты агента подставляют "
                                               "его сами, передавать не нужно"},
                "user_id": {"type": "string",
                            "description": "логин аудитора, если известен"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "hypotheses",
        "description": (
            "Гипотезы и перечень того, что стоит проверить, на старте новой "
            "проверки по теме. Собирает похожие прошлые КМ и типовые отклонения."
            + _VERBATIM
        ),
        "schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "тема проверки"},
                "session_key": {"type": "string",
                                "description": "идентификатор беседы, как в ask"},
                "user_id": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "deviations",
        "description": (
            "Реестр отклонений с фильтрами по проверке, категории, критичности, "
            "сумме ущерба, системе и подразделению. Модель не зовётся — "
            "отвечает быстро."
            + _VERBATIM
        ),
        "schema": {
            "type": "object",
            "properties": {
                "check_id": {"type": "string", "description": "номер КМ"},
                "category": {"type": "string"},
                "severity": {"type": "string"},
                "min_rub": {"type": "number"},
                "system": {"type": "string"},
                "unit": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "status",
        "description": (
            "Что есть в корпусе: сколько проверок, актов, отклонений, и в каком "
            "режиме база. Звать, когда поиск ничего не нашёл, — отличить "
            "«не нашлось» от «этого тут нет»."
        ),
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "card_start",
        "description": (
            "Запустить сборку карточки контроля исполнения по ответу "
            "профильного подразделения. Возвращает job_id сразу: карточка "
            "строится минутами. Дальше звать card_status: он сам ждёт "
            "готовности до 20 секунд, отдельная пауза не нужна."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session_key": {"type": "string",
                                "description": "идентификатор беседы, как в ask"},
                "letter_path": {"type": "string",
                                "description": "путь к письму: .docx, .txt, .pdf"},
                "letter_text": {"type": "string",
                                "description": "или текст письма напрямую"},
                "user_id": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "card_status",
        "description": (
            "Стадия и результат сборки карточки по job_id. Вызов САМ ждёт "
            "готовности до 20 секунд — отдельную паузу делать не нужно и "
            "нельзя. Пока done=false: показать пользователю stage и позвать "
            "снова. Когда готово, карточка приходит текстом."
            + _VERBATIM
        ),
        "schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_sec": {"type": "number",
                             "description": "сколько ждать готовности, сек; "
                                            "по умолчанию 20, максимум 60"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Забыть контекст диалога: следующий вопрос начнётся с чистого "
            "листа. Звать, когда аудитор переключился на другую проверку и "
            "подставляется прежний номер."
        ),
        "schema": {
            "type": "object",
            "properties": {"session_key": {"type": "string",
                                           "description": "идентификатор беседы, как в ask"}},
            "required": [],
        },
    },
]


async def dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Один вызов инструмента. Отделён от транспорта ради тестов.

    Исключение наружу не выпускается: у агента отказ инструмента — это
    оборванный ход и пустой экран у аудитора. Ошибка должна вернуться
    текстом, который модель может пересказать человеку.
    """
    from backend.skill import api

    # Подстановка ключа живёт здесь, на транспорте, а не в `api`: там
    # отсутствие ключа — ошибка вызывающего, и тесты это держат. Здесь
    # вызывающий — модель, которая ключа не знает.
    session_key = (args.get("session_key") or "").strip() or DEFAULT_SESSION_KEY

    try:
        if name == "ask":
            return await api.ask(args.get("question", ""), session_key,
                                 args.get("user_id"))
        if name == "hypotheses":
            return await api.hypotheses(args.get("topic", ""), session_key,
                                        args.get("user_id"))
        if name == "deviations":
            return await api.deviations(
                check_id=args.get("check_id"), category=args.get("category"),
                severity=args.get("severity"), min_rub=args.get("min_rub"),
                system=args.get("system"), unit=args.get("unit"),
                limit=int(args.get("limit") or 50))
        if name == "status":
            return await api.status()
        if name == "card_start":
            return await api.card_start(session_key,
                                        args.get("letter_path"),
                                        args.get("letter_text"),
                                        args.get("user_id"))
        if name == "card_status":
            raw = args.get("wait_sec")
            # Потолок нужен: инструменты агента ждут ответа ограниченно
            # (`timeout_sec`, по умолчанию 240 с), и вызов, который просит
            # ждать дольше, оборвётся на его стороне — задача останется
            # висеть, а агент решит, что она упала.
            wait = min(float(raw), 60.0) if raw is not None else None
            return await api.card_status(args.get("job_id", ""), wait)
        if name == "forget":
            return api.forget(session_key)
        return {"ok": False, "reason": f"неизвестный инструмент «{name}»"}
    except Exception as e:                                      # noqa: BLE001
        logger.exception(f"[mcp] {name} упал")
        return {"ok": False,
                "reason": f"инструмент {name} не отработал: "
                          f"{type(e).__name__}: {e}"}


def build_server():
    """Собрать MCP-сервер поверх `dispatch`."""
    import mcp.types as types
    from mcp.server import Server

    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> List[types.Tool]:
        return [types.Tool(name=t["name"], description=t["description"],
                           inputSchema=t["schema"]) for t in _TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any] | None):
        result = await dispatch(name, arguments or {})
        return [types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, default=str))]

    return server


async def _serve() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _boot() -> None:
    """Поднять корпус: база, индексы, фоновое пополнение.

    Нанобот — единственное, что запускает человек, и внутри него живут
    навыки всех проектов. Значит своего сервера у Follow Up на этой машине
    нет, и всё, что раньше делал его lifespan, обязан сделать этот процесс.
    Иначе инструменты отвечают, но по данным на день установки: гидратация,
    синк витрины и догрузка актов просто некому запустить.

    Отдельно — владение файлом базы. SQLite открыта с `vfs=unix-none`,
    блокировок нет вовсе, а `write_tx` защищает только внутри процесса.
    Если файл уже занят живым процессом, стартовать вторым писателем нельзя:
    это портит базу, а не теряет запись.
    """
    import os

    from backend.core import boot

    # Тесты транспорта поднимают этот же модуль подпроцессом, и там подъём
    # корпуса не нужен: он занимает файл базы и стоит секунды на каждый
    # запуск. Хуже того — если у разработчика рядом работает свой сервер,
    # подпроцесс честно откажется стартовать, и падение выглядело бы как
    # поломка транспорта.
    if os.environ.get("FU_SKIP_CORPUS_BOOT") == "1":
        logger.info("[mcp] FU_SKIP_CORPUS_BOOT=1 — корпус не поднимаю")
        return

    ok, why = boot.open_local_store()
    if not ok:
        logger.error(
            f"[mcp] Не стартую: {why}. Рядом уже работает Follow Up — "
            f"два процесса над одной SQLite портят базу (блокировок нет, "
            f"vfs=unix-none). Остановите второй экземпляр."
        )
        raise SystemExit(2)

    # Модель, Greenplum и модели поиска — у агента, как у соседних навыков.
    # До первого вопроса и до запуска фонового пополнения корпуса.
    boot.adopt_agent_llm()
    boot.adopt_agent_gp()
    boot.adopt_agent_models()
    boot.load_indexes()

    # Модели НЕ греем: нанобот поднимает дочерние процессы на своём старте,
    # и три гигабайта задержали бы его запуск для всех остальных навыков.
    # Первый вопрос заплатит за загрузку, дальше они в памяти.
    started, why = boot.start_corpus_upkeep()
    if started:
        logger.info("[mcp] Пополнение корпуса запущено: синк витрины, "
                    "гидратация, догрузка актов")
    else:
        logger.warning(f"[mcp] Корпус не пополняется: {why}")


# Совместимость: имя из первой редакции защиты.
_claim_database_or_die = _boot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        # stdout занят протоколом: любая печать туда ломает JSON-RPC
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for h in logging.getLogger().handlers:
        import sys
        h.setStream(sys.stderr)                                  # type: ignore
    _boot()
    logger.info(f"[mcp] Сервер {SERVER_NAME} поднят, инструментов: {len(_TOOLS)}")
    try:
        asyncio.run(_serve())
    finally:
        from backend.storage import writer
        # Снять метку, иначе следующий запуск решит, что файл занят
        writer.release_db_owner()


if __name__ == "__main__":
    main()
