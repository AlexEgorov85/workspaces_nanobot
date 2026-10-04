"""Блок настроек агента: файл, который агент отдаёт платформе целиком.

Зачем файл, а не флаг. Значение в ``argv`` видно в списке процессов, а блок
сделан с расчётом на то, что в нём однажды окажется секрет (ключ
провайдера, токен). И второе: флаг на каждое значение означал бы, что
добавление настройки — это правка кода запуска, а файл означает, что это
одна строка в :data:`AGENT_BLOCK_PATHS`.

Где файл. Путь объявляет **платформа** — ``mcp-platform/platform.json →
agent_settings``, верхнеуровневый ключ рядом с ``execution`` и ``session_root``.
Агент его не вычисляет: вычисленный путь разошёлся бы с объявленным при
первом же переносе, и вопрос «где агент ищет настройки» получил бы два
ответа. В объявлении живёт подстановка ``${NANOBOT_WORKSPACE}`` — тот же
механизм, что у ``execution.session_root``.

Три состояния файла, и различаются они намеренно:

* файла нет — настроек не задано, и это **не** ошибка: контур, в котором блок
  не заведён, существует, и «порог не задан» должно быть состоянием, а не
  сбоем;
* файл есть и разбирается — значения применяются;
* файл есть, но не разбирается, — отказ на старте. Молчаливый откат к пустому
  блоку выглядел бы как «настроек нет», а опечатка в значении уводила бы
  контур на боевые значения, и следом был бы журнал, в котором этих событий
  просто нет.

Блок — это **вход** платформы, а не её словарь: какие ключи допустимы,
объявляет ``libs/enterprise_common/settings.py``. Здесь перечислены только
пути конфигурации агента, которые наполняются; проверку «платформа такой ключ
принимает» держит ``tests/test_agent_settings_block.py``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from config import ConfigurationError

#: Ключ верхнего уровня в ``mcp-platform/platform.json``, объявляющий путь к
#: файлу блока. Рядом с ``execution`` и ``session_root``, а не внутри секции
#: настроек: это объявление **пути**, а значение настройки.
AGENT_SETTINGS_DECLARATION = "agent_settings"

#: Единственный аргумент запуска, которым блок доезжает до платформы. Литерал
#: с обеих сторон процесса, как ``--profile``: общего модуля у агента и
#: платформы нет, а объявлять флаг в третьем месте — значит завести ещё одно
#: объявление.
AGENT_SETTINGS_FILE_FLAG = "--agent-settings-file"

#: Подстановка ``${ПЕРЕМЕННАЯ}`` в объявлении пути.
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Права файла блока. Секрета в нём пока нет, но блок создаётся с расчётом на
#: него: 0644 означал бы, что читает вся группа, а менять файл может вся
#: группа — то есть значение, которым доверяет процесс, мог бы подменить
#: кто-то посторонний.
BLOCK_FILE_MODE = 0o600
#: На POSIX это режим файла, и ``chmod`` выставляет его. На Windows режим
#: выразить нечем: ``chmod`` умеет только read-only бит, а ``0o600`` читается
#: как «не read-only», то есть вызов проходит без ошибки, а
#: ``stat().st_mode`` остаётся ``0o666``. Там доступ ограничивает ACL,
#: унаследованная от каталога, и проверяется она не здесь. Страж (см.
#: ``tests/test_agent_settings_block.py::TestFilePermissions``) проверяет
#: поэтому сам факт вызова с этим режимом, а не ``stat`` под Windows.

#: Путь к порогу журнала в конфигурации агента. Объявлен один раз и на
#: чтение, и на запись в блок: писатель журнала агента
#: (``lib/core/application_context.py``) читает **этот же** ключ. Ключ поднят
#: в корень ``SETTINGS`` из ``gateway.agent`` функцией
#: ``config._lift_agent_sections`` — читать надо ``SETTINGS["logging"]``, а не
#: ``SETTINGS["gateway"]["agent"]["logging"]``: второго пути к тому же
#: значению быть не должно.
#:
#: Второе объявление того же пути — ``config.JOURNAL_MIN_LEVEL_PATH``; равенство
#: двух имён проверяет ``tests/test_mcp_platform_declaration.py``.
JOURNAL_MIN_LEVEL_PATH: tuple[str, ...] = ("logging", "db", "min_level")

#: Ключи блока, которые агент наполняет из своего ``config.json``.
#:
#: Ключ блока совпадает с точечным путём в конфигурации агента, поэтому запись
#: одна: «этот путь конфига уезжает платформе». Это **не** словарь
#: принимаемых ключей — тот объявляет платформа
#: (``settings.agent_settings_dictionary``). Добавить сюда ключ, который
#: платформа не принимает, можно лишь вместе с красным тестом.
AGENT_BLOCK_PATHS: dict[str, tuple[str, ...]] = {
    "logging.db.min_level": JOURNAL_MIN_LEVEL_PATH,
}


@dataclass(frozen=True, slots=True)
class AgentSettingsBlock:
    """Опубликованный блок: путь, значения и аргумент запуска."""

    path: Path
    values: Mapping[str, str]

    def argv(self) -> list[str]:
        """Аргументы запуска: путь и ничего больше.

        Значений в argv нет и не должно быть: командная строка видна всем,
        кто может прочитать список процессов.
        """
        return [AGENT_SETTINGS_FILE_FLAG, str(self.path)]

    def summary(self) -> str:
        """Строка для журнала запуска: что и откуда уехало."""
        applied = ", ".join(f"{k}={v!r}" for k, v in sorted(self.values.items()))
        return f"{self.path}: {applied or 'ключей нет'}"


def read_declaration(platform_json: Path) -> str | None:
    """Значение ``platform.json → agent_settings`` как есть.

    ``None`` — ключ не объявлен: контур блок не заводит, и это законное
    состояние («значения агента не передаются»), а не поломка. Объявленный,
    но негодный ключ — уже отказ.

    Raises:
        ConfigurationError: файл платформы не читается, не объект или ключ
            объявлен не строкой.
    """
    try:
        raw = json.loads(platform_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Файла платформы нет — блок не передаётся, и это состояние контура, а
        # не поломка: платформа поднимается и без него. Отличать отказ от
        # необъявленного нечего, сообщение у них одно.
        logger.warning(f"{platform_json}: файла нет — блок настроек не передан")
        return None
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"{platform_json}: не читается ({exc}); без него не известно, "
            "где агент ищет файл блока настроек"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{platform_json}: ожидался объект")
    declared = raw.get(AGENT_SETTINGS_DECLARATION)
    if declared is None:
        return None
    if not isinstance(declared, str) or not declared.strip():
        raise ConfigurationError(
            f"{platform_json} → {AGENT_SETTINGS_DECLARATION}: ожидался путь "
            "строкой, получено "
            f"{type(declared).__name__}. Объявленный, но негодный путь хуже "
            "необъявленного: по нему полагают блок, которого не будет."
        )
    return declared.strip()


def declared_block_path(
    platform_json: Path, env: Mapping[str, str] | None = None
) -> Path | None:
    """Путь файла блока из объявления платформы, с подстановками.

    ``None`` — объявления нет, блок не передаётся.

    Отсутствующий ``platform.json`` — тот же ответ: файла нет, значит и
    объявления в нём нет. Ломать старт агента из-за того, что он не может
    прочитать конфигурацию соседнего процесса, было бы отказом не по тому
    поводу: этот процесс поднимется и без блока, а свою конфигурацию
    прочитает сам и скажет о ней на своём языке.

    Подстановка ``${ПЕРЕМЕННАЯ}`` — тот же алфавит, что у
    ``execution.session_root``, и **строгий**: неразвёрнутая переменная в пути
    дала бы каталог, буквально названный ``${NANOBOT_WORKSPACE}``, то есть
    настройки уехали бы в место, о котором никто не знает, и выглядело бы это
    как «файла нет».
    """
    raw = read_declaration(platform_json)
    if raw is None:
        return None
    source = os.environ if env is None else env
    missing: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value = source.get(name)
        if value is None or value == "":
            missing.append(name)
            return ""
        return value

    resolved = PLACEHOLDER.sub(_substitute, raw)
    if missing:
        raise ConfigurationError(
            f"{platform_json} → {AGENT_SETTINGS_DECLARATION}: не задана "
            + ", ".join(sorted(set(missing)))
            + ". Подстановка ${имя} читается из окружения агента."
        )
    return Path(resolved).expanduser()


def load_agent_settings(path: Path) -> dict[str, Any]:
    """Прочитать файл блока.

    Отсутствующий файл — пустой блок, а не ошибка (см. доктрину модуля).

    Raises:
        ConfigurationError: файл есть, но не разбирается или не объект. Отказ
            называет файл: иначе на старте видно только то, что «настроек
            нет», а их на самом деле нет из-за опечатки.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"{path}: блок настроек агента не разбирается ({exc}). Опечатка в "
            "файле не должна уводить контур на значения по умолчанию молча."
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{path}: блок настроек агента ожидался объектом, получено "
            f"{type(raw).__name__}"
        )
    return {str(key): value for key, value in raw.items()}


def _config_value(settings: Any, path: tuple[str, ...]) -> str:
    """Точечный путь в конфигурации агента; отсутствие — пустая строка."""
    node: Any = settings
    for key in path:
        node = node.get(key) if hasattr(node, "get") else None
        if node is None:
            return ""
    return str(node).strip()


def block_values(settings: Any) -> dict[str, str]:
    """Значения блока из ``config.json`` агента.

    Пустой ключ в блок **не** попадает: «оператор не задал» и «оператор задал
    пустое» — одно и то же состояние, а два представления одного состояния
    разъезжаются при первом же чтении.
    """
    return {
        key: value
        for key, path in AGENT_BLOCK_PATHS.items()
        if (value := _config_value(settings, path))
    }


def publish_agent_settings(
    settings: Any, *, platform_json: Path, env: Mapping[str, str] | None = None
) -> AgentSettingsBlock | None:
    """Записать блок и вернуть путь с аргументом запуска.

    ``None`` — объявления пути в ``platform.json`` нет, блок не передаётся.
    Это состояние контура, а не отказ: платформа поднимается и без него, а
    порог журнала просто не задан.

    Файл пишется всегда, даже когда значений нет: «файла нет» и «значений
    нет» — разные состояния в журнале, и без файла второе выглядело бы как
    первое.
    """
    path = declared_block_path(platform_json, env)
    if path is None:
        logger.warning(
            f"{platform_json}: ключ {AGENT_SETTINGS_DECLARATION!r} не объявлен — "
            "блок настроек агента платформе не передан, её настройки не "
            "применяются. Объявите путь к файлу блока."
        )
        return None
    values = block_values(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Права выставляются после записи, и это **не одно и то же** на всех
    # платформах. ``os.chmod`` на POSIX выставляет режим, и 0644 был бы прямой
    # утечкой блока группе. На Windows ``chmod`` умеет только read-only бит:
    # вызов проходит без ошибки, а ``stat().st_mode`` так и остаётся 0666, и
    # режим 0600 там выражается не битами файла, а ACL, унаследованной от
    # каталога (рабочий каталог агента принадлежит одному пользователю).
    # Отсюда и отказ не быть фатальным: файл создан, читать его может тот же
    # пользователь, что и каталог, а ложная тревога в стартовом логе ничего
    # не добавит.
    try:
        os.chmod(path, BLOCK_FILE_MODE)
    except OSError as exc:  # pragma: no cover - зависит от ФС
        logger.debug(f"права файла блока не выставлены: {exc}")
    block = AgentSettingsBlock(path=path, values=values)
    logger.info(f"настройки агента для платформы: {block.summary()}")
    return block
