"""``SessionFileResolver`` — единственное место на стороне агента, где
вычисляется каталог сессии.

Зачем он. Путь каталога сессии считался в нескольких местах, и места эти
разошлись: хук писал в ``data_store/sessions/<key>``, хранилище
вложений — в то же дерево по своей копии санитайзера, платформа — в свой
корень. Пока вычисление повторяется, «где мои файлы» приходится угадывать по
содержимому каталога. Резолвер закрывает это: хук, канал и скиллы берут путь
здесь, а не собирают его сами.

Два режима, и оба объявляют корень **один раз**:

* **платформа поднята** (``enterprise_mcp`` есть) — корень объявляет платформа
  (``mcp-platform/platform.json → execution.session_root``), а агент спрашивает
  его операцией ``session_files`` и берёт ответ как есть. Агент корень не
  вычисляет, из ``platform.json`` не читает и платформе не передаёт: значение,
  присланное вызывающей стороной, приоритетнее файла и молча затирает
  объявление платформы;
* **платформы нет** (``enterprise_mcp`` выключен) — корень объявляет агент, и
  процесса платформы, который объявил бы второй корень, не существует.

Значит, оба объявления не могут быть активны одновременно: второе объявление
читается только тогда, когда первого нет.

Почему отказ, а не «куда-нибудь». Если платформа объявлена, но каталог получить
не удалось, резолвер бросает :class:`SessionFilesUnavailable` с названной
причиной. Откат на другой каталог вернул бы ровно тот дефект, который резолвер
и убирает: две папки сессии вместо одной, и по содержимому не понять, какая
своя.

Почему кэш по ``session_key`` и почему он не проверяет корень. Один вызов
операции на сессию достаточен: ответ — это путь, а путь не меняется сам по
себе. Сверять корень при повторном обращении нельзя: единственный способ узнать
его — снова спросить у платформы, то есть ровно тот вызов, ради устранения
которого кэш и нужен. Поэтому гарантия узкая и честная: **один вызов на
``session_key``**. Новый ``session_key`` вызовет операцию снова и увидит тот
корень, какой объявлен сейчас, — то есть смена объявления видна следующей
сессии, а не той, что уже закэширована.

Почему имя каталога в режиме без платформы берётся чужой функцией. Платформенный
``safe_name`` живёт в процессе платформы, а агент без платформы всё равно обязан
куда-то положить файл. Свою копию санитайзера агент не заводит: он зовёт
``workspace.utils.session_key.safe_session_key`` — единственную агентскую
функцию имени. Согласие сторон обеспечивает контрактный тест
``tests/contract/test_session_dir_name_contract.py``, а не копирование
алгоритма; резолвер политику имени не дублирует и не улучшает.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Имя платформенной операции. Объявлено здесь, а не в клиенте: единственный
#: потребитель — резолвер, и второе упоминание имени операции в агенте было бы
#: вторым местом, где его можно переименовать.
SESSION_FILES_OPERATION = "session_files"

#: Подкаталог, в который пишет агент. Имя повторено намеренно: в режиме без
#: платформы раскладку никто не создаёт, и взять её имя больше неоткуда. Раскладку
#: целиком объявляет платформа (``SESSION_SUBDIRS``); агент заводит только тот
#: каталог, в который пишет сам, и не создаёт шесть чужих.
FILES_SUBDIR = "files"

#: Дефолтный корень в режиме без платформы — ``<workspace_dir>/data_store/sessions``.
#: Это запасной вариант для объявления, а не второе объявление: как только в
#: ``config.json`` появится ``gateway.agent.session_files.root``, читается он, и
#: значение ниже перестаёт использоваться. Ключа в конфиге пока нет (задача 2.2
#: openspec-предложения ``2026-10-03-session-files``), поэтому ветка дефолта —
#: рабочая, а не мёртвая.
DEFAULT_ROOT_PARTS: tuple[str, ...] = ("data_store", "sessions")

#: Путь к объявлению агента в поднятых ``SETTINGS``. Секция ``session_files``
#: физически лежит в ``config.json → gateway.agent.session_files`` и поднимается
#: в корень функцией ``config._lift_agent_sections`` — так же, как
#: ``enterprise_mcp`` и ``logging``. Второго пути к тому же значению
#: (``gateway.agent.session_files``) быть не должно: два чтения одного ключа
#: разъезжаются при первой же правке объявления.
DECLARED_ROOT_PATH: tuple[str, ...] = ("session_files", "root")


class SessionFilesUnavailable(RuntimeError):
    """Каталог сессии получить не удалось.

    Отдельный тип, а не ``RuntimeError`` с текстом: вызывающая сторона (хук,
    канал) обязана отличать «каталога нет» от «каталог есть», иначе отказ
    пришлось бы распознавать по строке — а строки меняются.
    """


@dataclass(frozen=True, slots=True)
class SessionFilesLayout:
    """Ответ операции ``session_files``, разобранный в значения резолвера.

    Пути — ``Path``, а не строки: единственный потребитель ходит с ними к
    файловым инструментам, и разбирать строку обратно в путь в нём не должно.

    Имя поля ``root`` оставлено таким, как в контракте операции, и это **каталог
    сессии**, а не корень каталогов сессий: корень сессий в ответе отсутствует
    намеренно, агент его не знает и знать не должен.
    """

    session_id: str
    root: Path
    files_dir: Path
    layout: tuple[str, ...]
    created: bool


def declared_root(settings: Any) -> str | None:
    """Прочитать корень из объявления агента (``config.json``).

    ``None`` — ключа нет или он пуст. Отдельной ошибки тут нет: отсутствие
    объявления законно (дефолт), и превращать его в отказ значило бы запретить
    режим без платформы.
    """
    if settings is None:
        return None
    node: Any = settings
    for key in DECLARED_ROOT_PATH:
        getter = getattr(node, "get", None)
        node = getter(key) if callable(getter) else None
        if node is None:
            return None
    raw = str(node).strip()
    return raw or None


def _agent_session_dir_name(session_key: str) -> str:
    """Имя каталога сессии по агентской функции (режим без платформы).

    Импорт ленивый: ``lib/services`` не тянет ``workspace`` при импорте модуля
    (по той же причине, что и остальные тяжёлые зависимости в этом слое).
    """
    from workspace.utils.session_key import safe_session_key

    return safe_session_key(session_key)


class SessionFileResolver:
    """Каталог сессии по ``session_key`` — одна папка, один вызов, один корень.

    Экземпляр на процесс, состояние — кэш ответов по ``session_key``. Потокобезопасность
    не обеспечивается сознательно: обращения идут из оборота агента по одной
    сессии за раз, а блокировка вокруг сетевого вызова означала бы, что один
    медленный ответ платформы задерживает чужие сессии.
    """

    def __init__(
        self,
        *,
        enterprise_mcp: Any = None,
        workspace_dir: Any,
        settings: Any = None,
        session_dir_name: Callable[[str], str] | None = None,
    ) -> None:
        self._client = enterprise_mcp
        self._workspace_dir = Path(workspace_dir)
        self._settings = settings
        self._name = session_dir_name or _agent_session_dir_name
        self._cache: dict[str, SessionFilesLayout] = {}

    @property
    def has_platform(self) -> bool:
        """Объявлена ли платформа.

        По этому признаку видно, какой корень активен: при ``True`` —
        платформенный, при ``False`` — агентский. Одновременно оба быть не могут.
        """
        return self._client is not None

    # -- публичный контракт -------------------------------------------------

    async def session_dir(self, session_key: str) -> Path:
        """Каталог сессии. Не создаёт ничего сама."""
        layout = await self._layout(session_key)
        return layout.root

    async def files_dir(self, session_key: str) -> Path:
        """Каталог файлов агента внутри сессии. Не создаёт ничего сама."""
        layout = await self._layout(session_key)
        return layout.files_dir

    def resolved_session_dir(self, session_key: str) -> Path:
        """Каталог сессии **из уже полученного** ответа платформы, синхронно.

        Для кодека вложений (``workspace/utils/media.py``), который синхронен, а
        резолвер асинхронен: канал дожидается каталога один раз, вызывая
        :meth:`ensure`, и дальше отдаёт хранилищу функцию, читающую этот ответ.

        Обращения к платформе здесь нет, иначе получился бы второй сетевой
        вызов в пути, который синхронен. Если каталог ещё не получен, это отказ,
        а не вычисление имени на стороне агента: вызывающий обязан сначала
        дождаться :meth:`ensure`.
        """
        key = str(session_key or "").strip()
        layout = self._cache.get(key)
        if layout is None:
            raise SessionFilesUnavailable(
                f"каталог сессии {session_key!r} ещё не получен: сначала "
                f"дождитесь ensure(), иначе путь пришлось бы вычислять здесь"
            )
        return layout.root

    async def ensure(self, session_key: str) -> Path:
        """Каталог сессии, готовый к записи; возвращает его же.

        При платформе каталог создаёт операция (в её ответе есть ``created``) —
        отдельного кода создания в агенте нет и не должно быть: вторая копия
        ``mkdir`` разошлась бы с платформенной раскладкой при первой же правке
        подкаталога. Без платформы создавать некого, поэтому агент заводит
        каталог сессии и ``files/`` — тот единственный подкаталог, в который
        пишет он сам.
        """
        layout = await self._layout(session_key)
        if self.has_platform:
            return layout.root
        try:
            layout.root.mkdir(parents=True, exist_ok=True)
            layout.files_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionFilesUnavailable(
                f"не удалось создать каталог сессии {layout.root}: {exc}"
            ) from exc
        return layout.root

    # -- вычисление ---------------------------------------------------------

    async def _layout(self, session_key: str) -> SessionFilesLayout:
        key = str(session_key or "").strip()
        if not key:
            # Служебное имя каталога здесь означало бы «все сессии без ключа в
            # одной папке», а отказ — что у оборота нет папки вообще. Второе
            # правда: запись без сессии отменяет смысл каталога сессии.
            raise SessionFilesUnavailable(
                "session_key не задан: каталог сессии не вычисляется"
            )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        layout = await self._resolve(key)
        self._cache[key] = layout
        return layout

    async def _resolve(self, session_key: str) -> SessionFilesLayout:
        if not self.has_platform:
            return self._local_layout(session_key)
        return await self._platform_layout(session_key)

    async def _platform_layout(self, session_key: str) -> SessionFilesLayout:
        # Личность вызова не собирается здесь: её единственное место —
        # ``EnterpriseMcpClient._identity_from_turn``, и копия этой функции в
        # резолвере означала бы, что событие в журнале и каталог сессии могут
        # описывать разные вызовы. Клиент сам доберёт ``request_id`` оборота.
        try:
            answer = await self._client.session_files()
        except Exception as exc:  # noqa: BLE001 - причина наружу, а не класс
            # Отказ, а не откат на каталог по умолчанию: платформа объявлена,
            # значит каталог объявляет она, и «запасной» путь был бы вторым
            # объявлением, которое тихо разошлось бы с первым.
            raise SessionFilesUnavailable(
                f"платформа не отдала каталог сессии: {exc}"
            ) from exc
        return self._parse(answer, session_key)

    def _parse(self, answer: Any, session_key: str) -> SessionFilesLayout:
        if not isinstance(answer, dict):
            raise SessionFilesUnavailable(
                f"ответ операции {SESSION_FILES_OPERATION!r} — не объект: {type(answer).__name__}"
            )
        root = answer.get("root")
        files_dir = answer.get("files_dir")
        if not root or not files_dir:
            raise SessionFilesUnavailable(
                f"ответ операции {SESSION_FILES_OPERATION!r} без root/files_dir: "
                f"session_key={session_key!r}"
            )
        root_path = Path(str(root))
        files_path = Path(str(files_dir))
        if not root_path.is_absolute() or not files_path.is_absolute():
            raise SessionFilesUnavailable(
                f"ответ операции {SESSION_FILES_OPERATION!r} вернул не абсолютные "
                f"пути: root={root!r}, files_dir={files_dir!r}"
            )
        return SessionFilesLayout(
            session_id=str(answer.get("session_id") or session_key),
            root=root_path,
            files_dir=files_path,
            layout=tuple(str(name) for name in (answer.get("layout") or ())),
            created=bool(answer.get("created")),
        )

    def _local_layout(self, session_key: str) -> SessionFilesLayout:
        root = self._local_root()
        directory = root / self._name(session_key)
        return SessionFilesLayout(
            session_id=session_key,
            root=directory,
            files_dir=directory / FILES_SUBDIR,
            layout=(FILES_SUBDIR,),
            created=False,
        )

    def _local_root(self) -> Path:
        raw = declared_root(self._settings)
        if raw is None:
            return self._workspace_dir.joinpath(*DEFAULT_ROOT_PARTS)
        root = Path(raw).expanduser()
        if not root.is_absolute():
            # Относительный корень жил бы рядом с текущим каталогом процесса —
            # ровно тот дефект, из-за которого прежний корень платформы уезжал
            # в ``mcp-platform/.sessions`` и «переезжал» вместе с тем, откуда
            # запустили. Объявление обязано называть путь целиком.
            raise SessionFilesUnavailable(
                f"корень файлов сессии должен быть абсолютным, а объявлен {raw!r}"
            )
        return root


#: Резолвер текущего процесса. Нужен плагинам ``workspace/hooks/``:
#: ``lib.cli.hook_loader.scan_and_register`` инстанцирует хук как
#: ``cls(workspace_dir=workspace_dir)`` — единственный аргумент, — поэтому
#: composition root не может передать ему службу ни через конструктор, ни через
#: ``AgentFactory.hook_factories`` (там нужен per-turn инстанс, а резолвер на
#: процесс один). Чтение ленивое, поэтому порядок не важен: хук поднимается на
#: шаге 6a, а резолвер публикуется позже, и обращение случается уже в обороте.
_CURRENT: SessionFileResolver | None = None


def install_session_file_resolver(resolver: SessionFileResolver | None) -> None:
    """Опубликовать резолвер процесса (``None`` — снять публикацию)."""
    global _CURRENT
    _CURRENT = resolver


def current_session_file_resolver() -> SessionFileResolver | None:
    """Резолвер, опубликованный composition root'ом, либо ``None``.

    ``None`` — контекст не собран (тест, CLI-процесс без сборки) или сборка
    не дошла до публикации. Потребитель обязан трактовать это как отказ, а не
    искать каталог самостоятельно: запасной путь здесь означал бы второе
    объявление корня, то есть исходный дефект.
    """
    return _CURRENT
