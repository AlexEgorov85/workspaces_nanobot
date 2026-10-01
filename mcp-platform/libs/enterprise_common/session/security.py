"""Граница каталога сессии: относительный путь, проверенный по канону.

Проверка идёт сравнением **канонизированных** путей, а не разбором ``..`` в
строке. Разбор строки ловит только явный выход вверх по дереву и молча пропускает
две вещи, которые в файловой системе встречаются чаще: симлинк наружу и
абсолютный путь. Канонизация ловит и то, и другое, потому что
``resolve(strict=False)`` проходит по ссылкам.

Поэтому путь наружу не «запрещается похожим на выход», а просто не может
вычислиться внутри сессии: дальше границы нет ни для чтения, ни для записи.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

#: Имена файлов, которые нельзя создать ни при каких условиях.
_RESERVED_NAMES = frozenset({"", ".", ".."})

#: Символы, недопустимые в имени файла в Windows и POSIX.
_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

#: Длина имени файла ограничена снизу и сверху: слишком короткое имя не
#: читается в журнале, слишком длинное не создаётся на Windows.
MAX_NAME_LENGTH = 128


class PathDeniedError(ValueError):
    """Путь ведёт за пределы каталога сессии.

    Наследник ``ValueError``, а не доменный ``InvalidRequestError``: это ошибка
    контракта доступа, а не результат доменной работы. Наружу уходит код
    ``invalid_params``.
    """

    code = "invalid_params"


def safe_child(root: Path, relative_path: str) -> Path:
    """Разрешить относительный путь внутри ``root``.

    ``root`` не обязан существовать — каталог создаёт платформа при первом
    обращении, и проверка не должна требовать существующего дерева.

    Отказ — не на «похожий на выход», а на выход: путь, который не может
    вычислиться внутри сессии, не получает ни чтения, ни записи. Отдельно
    отвергаются два случая, которые канонизация поймала бы поздно и невнятно:

    * ``.`` — вычисляется в сам каталог, и запись по нему падает уже на
      ``open`` с сообщением про права, а не про границу сессии;
    * путь с буквой диска (``C:\\...``) — ``PurePosixPath`` считает его
      относительным, а на Windows он уводит вычисление на другой диск.
    """
    raw = str(relative_path or "").strip()
    if not raw:
        raise PathDeniedError("путь не задан")
    candidate = PurePosixPath(raw.replace("\\", "/"))
    if candidate.is_absolute():
        raise PathDeniedError(f"абсолютный путь недопустим: {raw!r}")
    if not candidate.parts or any(part in _RESERVED_NAMES for part in candidate.parts):
        # ``parts`` пуст у «.»: путь, у которого нет ни одной части, — это
        # и есть сам каталог, а не файл внутри него.
        raise PathDeniedError(f"выход за пределы каталога: {raw!r}")
    if any(":" in part for part in candidate.parts):
        raise PathDeniedError(f"путь с буквой диска недопустим: {raw!r}")

    base = root.expanduser().resolve()
    target = (base / Path(*candidate.parts)).resolve()
    if target != base and base not in target.parents:
        raise PathDeniedError(f"путь уходит за пределы каталога сессии: {raw!r}")
    return target


def safe_name(raw: str, *, suffix: str = "") -> str:
    """Привести имя файла к безопасному виду.

    Имя артефакта приходит от домена, а домен — от модели. Санитизация здесь, а
    не проверка отказа: безопасное имя полезнее отказа, и отказ не нужен там, где
    можно просто убрать опасные символы. Разделители пути и ``..`` при этом не
    «очищаются», а отвергаются — иначе ``../../x`` превратился бы в ``x`` и имя
    потеряло бы смысл.
    """
    raw = str(raw or "").strip()
    if not raw:
        raise PathDeniedError("имя не задано")
    if "/" in raw or "\\" in raw or raw in _RESERVED_NAMES:
        raise PathDeniedError(f"имя содержит разделитель пути: {raw!r}")
    stem = Path(raw).stem
    extension = Path(raw).suffix
    stem = _UNSAFE_CHARS.sub("_", stem).strip(" .")
    extension = _UNSAFE_CHARS.sub("_", extension)[:16]
    if not stem:
        raise PathDeniedError(f"имя не содержит пригодной части: {raw!r}")
    stem = stem[:MAX_NAME_LENGTH]
    return f"{stem}{extension}{suffix}"
