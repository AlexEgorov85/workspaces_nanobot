"""Имя каталога сессии по ``session_key`` — единственная агентская копия правила.

Правило объявлено один раз в спецификации openspec-предложения
``2026-10-03-session-files`` (раздел «Имя каталога сессии — контракт между
сторонами») и повторено здесь буквально. Согласие с платформой обеспечивает
контрактный тест ``tests/contract/test_session_dir_name_contract.py``, а не
копирование алгоритма; правка правила здесь без правки спеки расводит стороны.

Отказ, а не подстановка. Недопустимый ключ даёт :class:`SessionDirNameDenied`:
служебное имя каталога означало бы «все неразрешимые сессии в одной папке», то
есть потерю данных вместо отказа. Прежнее правило резало всё вне
``[A-Za-z0-9._-]``, и ``привет`` с ``ключ`` давали одно и то же имя.

Используется:
  - ``SessionFileRedirectHook`` — перенаправление записи в ``files/`` сессии;
  - ``SessionFileResolver`` — имя каталога в режиме без платформы;
  - ``SessionFileStore`` — вложения канала.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_SESSION_PATH_RE = re.compile(
    r"(?:^|[/\\])data_store[/\\]cache[/\\]sessions[/\\]([^/\\]+)"
)

#: Метка сессии, у которой нет пригодного имени каталога. Каталогом сессии она
#: не бывает (требование «Псевдосессии запрещены») и нужна только журналу.
__nosession__ = "__nosession__"

#: Правило 5: на ``_`` заменяются только эти символы. Пробел, точка и дефис
#: законны в имени файла Windows, поэтому заменять их нельзя: набор — это
#: пересечение запретов Windows и Linux, и в этом суть переносимости.
_INVALID_DIR_CHARS: re.Pattern[str] = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')

#: Правило 3: зарезервированные имена Windows. Проверяется часть до первой точки,
#: потому что зарезервировано и ``CON``, и ``CON.txt``.
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Правило 4: длиннее — отказ, а не усечение.
MAX_DIR_NAME_LEN: int = 128

#: Служебные имена, каталогом сессии быть не могут. Без учёта регистра: на
#: Windows ``__NOSESSION__`` и ``__nosession__`` — один и тот же каталог.
_SERVICE_NAMES: frozenset[str] = frozenset({__nosession__, "_shared"})


class SessionDirNameDenied(ValueError):
    """``session_key`` непригоден как имя каталога сессии.

    Наследник ``ValueError``: контрактный тест ловит отказ этим классом, и
    вызывающий, который ловил ``ValueError``, увидит отказ, а не подставленное
    имя, — молчаливый откат к прежнему поведению здесь и есть тот дефект,
    который фаза убирает.
    """


def safe_session_key(key: str) -> str:
    """Имя каталога сессии по ``session_key``; правило спецификации буквально.

    Отказ (:class:`SessionDirNameDenied`) на пустом ключе, разделителях пути,
    ``.``/``..``, зарезервированном имени Windows, длине больше 128 символов и
    на служебном имени. Иначе на ``_`` заменяются только ``< > : " | ? *`` и
    управляющие символы; всё остальное сохраняется, включая не-ASCII буквы и
    точки, а ведущие и конечные точки с дефисами не срезаются — срезание
    склеило бы ``x`` и ``.x`` в одно имя.

    Examples:
        >>> safe_session_key("cli:1")
        'cli_1'
        >>> safe_session_key("telegram:8281248569")
        'telegram_8281248569'
        >>> safe_session_key("привет")
        'привет'
    """
    raw = "" if key is None else str(key)
    if not raw.strip():
        raise SessionDirNameDenied("session_key пуст: имя каталога сессии не вычисляется")
    if "/" in raw or "\\" in raw:
        raise SessionDirNameDenied(f"session_key содержит разделитель пути: {raw!r}")
    if raw in (".", ".."):
        raise SessionDirNameDenied(f"session_key — относительный путь, а не имя: {raw!r}")
    if len(raw) > MAX_DIR_NAME_LEN:
        raise SessionDirNameDenied(
            f"длина session_key {len(raw)} больше {MAX_DIR_NAME_LEN}: усечение "
            f"склеило бы две сессии в один каталог"
        )
    name = _INVALID_DIR_CHARS.sub("_", raw)
    if name.partition(".")[0].upper() in _WINDOWS_RESERVED:
        raise SessionDirNameDenied(f"зарезервированное имя Windows: {raw!r}")
    if name.casefold() in _SERVICE_NAMES:
        raise SessionDirNameDenied(f"служебное имя не может стать сессией: {raw!r}")
    return name


def raw_session_key(context: Any) -> str | None:
    """Ключ сессии из контекста — **как есть**, без приведения к имени каталога.

    Отделено от :func:`resolve_session_key` потому, что каталог сессии считает не
    агент, а резолвер, и он получает именно идентичность оборота. Подстановка
    сюда уже приведённого имени означала бы объявить резолверу не то, что
    пришло от канала, и разошлись бы обе стороны контрактного теста.

    Источники (по убыванию приоритета):
        1. ``context.session_key`` — обычно есть (см. database_logging_hook).
        2. ``context.metadata.session_key`` — fallback.
        3. ``None`` — идентичности у оборота нет. Подставлять вместо неё
           служебное имя нельзя: каталог сессии из него не создаётся, а запись
           «куда-нибудь» отменяет смысл каталога сессии (потребитель обязан
           трактовать ``None`` как отказ).
    """
    key = getattr(context, "session_key", None)
    if isinstance(key, str) and key:
        return key
    metadata = getattr(context, "metadata", None)
    if metadata is not None:
        key = getattr(metadata, "session_key", None)
        if isinstance(key, str) and key:
            return key
    return None


def _identity_label(key: str) -> str:
    """Метка сессии для журнала: имя каталога либо служебная метка.

    Здесь идентичность нужна как **метка в журнале**, а не как каталог, поэтому
    отказ правила имени не должен ронять запись в лог — вместо имени
    возвращается служебная метка. Псевдосессией она не становится: каталог под
    таким именем не создаёт никто, а обход резолвера с этой меткой отвергнут
    правилом имени (см. :func:`safe_session_key`).
    """
    try:
        return safe_session_key(key)
    except SessionDirNameDenied:
        return __nosession__


def resolve_session_key(context: Any) -> str:
    """Стабильная метка сессии из контекста — для журнала, не для каталога.

    Хук перенаправления эту функцию больше не зовёт: каталог сессии считает
    резолвер, и ему нужна идентичность оборота, а не метка (см.
    :func:`raw_session_key`). Остаётся она как единственное место, где
    приведение к безопасному виду применяется к ключу, полученному из
    контекста, — то есть там, где отказ правила имени недопустим.

    Examples:
        >>> resolve_session_key(SimpleNamespace(session_key="telegram:8281248569"))
        'telegram_8281248569'
        >>> resolve_session_key(None)
        '__nosession__'
    """
    key = raw_session_key(context)
    if key is None:
        return __nosession__
    return _identity_label(key)


def resolve_session_key_for_subprocess(
    file_path: str | Path | None = None,
) -> str:
    """Получить стабильный ключ сессии для standalone CLI/skill-процесса.

    Зеркало ``resolve_session_key`` для случая, когда у нас НЕТ in-process
    контекста nanobot (subprocess скилла). Агент, вызывающий навык, **не знает**
    ключ сессии (CLI должен работать без явного ``--session-key``), поэтому
    резолвим автоматически из окружения и из самого файла:

    Источники (по убыванию приоритета):
        1. ``os.environ["SESSION_KEY"]`` — выставляется nanobot-каналом,
           если доступен (например, ``"telegram:8281248569"``).
        2. ``_identity_label(Path(file_path).name)`` — стабильный ключ из
           basename файла. В рамках одной CLI-сессии имена файлов не
           пересекаются, поэтому basename достаточен для cache isolation.
        3. ``__nosession__`` — last resort.

    Examples:
        >>> import os; os.environ["SESSION_KEY"] = "telegram:42"
        >>> resolve_session_key_for_subprocess(Path("/tmp/foo.pdf"))
        'telegram_42'
        >>> del os.environ["SESSION_KEY"]
        >>> resolve_session_key_for_subprocess(Path("/tmp/foo.pdf"))
        'foo.pdf'
        >>> resolve_session_key_for_subprocess(None)
        '__nosession__'
    """
    env_key = os.environ.get("SESSION_KEY")
    if env_key:
        return _identity_label(env_key)
    if file_path is not None:
        try:
            name = Path(file_path).name
            if name:
                return _identity_label(name)
        except (OSError, ValueError):
            pass
    return __nosession__


def extract_session_key_from_path(file_path: str) -> str | None:
    """Извлечь raw session_key из пути ``data_store/cache/sessions/<key>/...``.

    Поддерживает POSIX и Windows пути. ``None`` если session_key в пути
    не найден.
    """
    if not file_path:
        return None
    normalized = file_path.replace("\\", "/")
    m = _SESSION_PATH_RE.search(normalized)
    return m.group(1) if m else None
