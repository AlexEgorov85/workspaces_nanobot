"""Реестр настроек платформы: что вообще можно настроить и кто это читает.

Зачем файл реестра, если настройки и так приходят через окружение
--------------------------------------------------------------------

Настройки платформы приходили через 28 имён переменных, прочитанных тремя
разными способами: литерально в ``servers/enterprise/server.py`` (по пяти
функциям, пять из них — инлайном в ``_build_container``), ещё десять
описаны декларативной таблицей ``_ENV_NAMES`` в ``libs/llm/config.py``,
DSN — через ``resolve_dsn()``. Перечислить настройки, не открыв три
файла, было нельзя.

Сейчас разбор **один**: ``Settings`` — единственный, кто читает окружение
и ``platform.json``, и единственный, кто владеет дефолтами. Остальной код
получает уже разрешённое значение, поэтому ``server.py`` не обращается к
``os.environ`` вовсе. Пока это было не так, файл с настройками читался
только в тестах, а на процессе действовали дефолты из кода: значения в
``platform.json`` выглядели рабочими и не влияли ни на что.

Два свойства, из-за которых реестр и нужен:

1. **Владелец.** По части значений у платформы есть мнение, по части
   его быть не должно: выбор модели и ключи — дело агента, а размер
   буфера журнала, размер пула и таймаут запроса — дело платформы. Это
   разные вещи, и в одном списке они неразличимы.
2. **Место, где живут значения без агента.** Свои ручки платформы
   существуют только как дефолты в коде. Задать их без агента можно
   только файлом, и перечислить их — тоже негде.

Структура реестра повторяет код
-------------------------------

Настройки сгруппированы по capability, и у каждой capability названы её
``service`` и её операции — ровно как они устроены в
``servers/enterprise/capabilities/``. Причина практическая: вопрос «кто читает
эту настройку» должен отвечать на вопрос про потребителя, а не про
``_build_container``, который только собирает сервисы. Первая версия реестра
была плоским списком и отвечала именно на второй вопрос.

Страж сверяет дерево с диском, поэтому capability без сервиса, операция,
которой нет в ``tools/``, и настройка без владельца падают в тестах, а не
при разборе инцидента.

Приоритет значений
------------------

``окружение > файл > дефолт``, с одним исключением — ``db.dsn`` (см. ниже)
--------------------------------

Окружение остаётся старшим намеренно. Пока агент экспортирует значения в
дочерний процесс, файл не может ничего сломать: всё, что работает
сегодня, продолжит работать. Второй, нижний слой вводится первым, и
только потом агент перестаёт экспортировать платформенные значения — по
одному, с проверкой на каждом шаге. Обратный порядок (сначала убрать
экспорт, потом добавить файл) оставил бы развёртывание без значения
после неудачного деплоя.

Исключение: DSN
---------------

``db.dsn`` — единственная настройка с ``file_first=True``, и это не каприз,
а разница между объявлением и наследованием. Процесс ``enterprise-mcp``
получает ``dict(os.environ)`` агента целиком, поэтому ``DATABASE_URL`` в его
окружении — это секрет агента, а не решение платформы о своей базе. С
общим приоритетом значение из файла оказалось бы декоративным: сервер
показывал бы в конфигурации одну базу, а подключался бы к другой.

В файле лежит **шаблон**: хост, порт и имя базы литералом, логин и пароль
подстановками ``${ПЕРЕМЕННАЯ}``, которые разворачиваются из окружения
процесса. Так DSN виден в конфигурации (это и было требованием), а пароль
от рабочей базы не попадает в файл под git. Подстановка разворачивается
**только** для значений из файла: значение из окружения уже готово, и
повторный разворот съел бы пароль, содержащий ``${``.

Владелец настройки
------------------

``owner="platform"`` — у платформы есть мнение, и оно живёт в
``platform.json``. ``owner="agent"`` — решение агента (модель, ключ,
адрес, путь снимка, пока снимок грузит агент), значение приходит через
окружение и в файл не попадает никогда.

Настройки вне capability
-----------------------

Пул соединений нужен всем capability сразу и ни одной из них не
принадлежит, поэтому он объявлен в ``SHARED_SETTINGS`` и лежит в файле в
собственной секции ``pool``, а не в секции какой-нибудь capability. Секции
перечислены в ``SHARED_SECTIONS``, и страж сверяет файл именно с этим
списком: секция, которой нет ни в capability, ни в ``SHARED_SECTIONS``, —
это опечатка, которая выглядела бы как «настройка прочитана».

Значений в коде нет
-------------------

У платформенной настройки в реестре нет значения — есть ``FROM_FILE``, то
есть «это значение обязано прийти из ``platform.json``». Нет ключа в файле —
сервер не поднимается и говорит какой.

Это не украшение, а запрет второго места. Именно дефолт в коде породил
исходный дефект: размеры пула жили в ``_DEFAULT_POOL``, в реестре для них не
было записей, и изменить их было нечем — при том что файл с настройками
выглядел рабочим. Второй список дефолтов рядом с первым разъезжается при
первом же изменении, и вопрос «что применяется на самом деле» получает два
ответа.

Поэтому в ``libs/enterprise_data/db.py`` нет ``_DEFAULT_POOL``: там
``_POOL_SPEC`` — только ключи и типы, а набор значений приходит из файла
целиком. Неполный набор — ошибка конфигурации, а не «остальное как было».

Секреты — отдельный слой
------------------------

``mcp-platform/.secrets.env`` лежит под тем же ``.gitignore``, что и
агентский: пароль от рабочей базы и ключ провайдера не должны жить в файле,
который под git. Подстановки ``${DB_USER}`` в ``platform.json`` читаются
сначала оттуда, потом из окружения процесса.

Секреты здесь — **секреты самого MCP**, а не копия агентских. Сервер может
жить на другой машине; общий секрет связал бы развёртывания в одно, и
правка одного повредила бы другому.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: Плохой файл настройки — это проблема развёртывания, а не плохой запрос
#: вызывающей стороны, поэтому InfrastructureError, а не InvalidRequestError.
#: Тот же класс, что и у «не задан DSN».
from libs.enterprise_common.errors import InfrastructureError

#: Корень платформы — родитель ``libs``.
PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent

#: Файл настроек платформенной части. Лежит рядом с ``pyproject.toml`` и
#: намеренно НЕ в каталоге ``config/``: такое имя перебило бы модуль
#: ``config`` агента, который платформе импортировать запрещено.
PLATFORM_CONFIG_PATH = PLATFORM_ROOT / "platform.json"

#: Секреты платформы. Отдельный файл рядом с конфигурацией, под тем же
#: ``.gitignore``, что и агентский: ``platform.json`` живёт под git, пароль от
#: рабочей базы и ключ провайдера — нет. Сервер может жить на другой машине,
#: поэтому у него должны быть **свои** секреты: у агента свои, и делить их
#: через окружение процесса — значит связать их судьбой.
SECRETS_PATH = PLATFORM_ROOT / ".secrets.env"

OWNER_PLATFORM = "platform"
OWNER_AGENT = "agent"


class FromFile:
    """Значение обязано прийти из ``platform.json``.

    Смысл — не «умолчание», а **запрет второго места**. У платформенной
    настройки значение живёт в файле, и в коде его нет: иначе появляется
    дефолт, который тихо перебивает файл, и вопрос «что применяется на
    самом деле» получает два ответа. Конкретный случай, который это
    стоил: размеры пула жили в ``_DEFAULT_POOL``, и задать их было нечем.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - только для сообщений
        return "<из platform.json>"


#: Подставьте вместо значения настройки, которая обязана быть в файле.
FROM_FILE = FromFile()


class OptionalValue:
    """Платформенная настройка, которой **можно** не иметь значения.

    Третья форма рядом с ``FROM_FILE``, и она нужна ровно для
    необязательных capability. ``FROM_FILE`` означал бы «без значения из
    файла сервер не поднимется», а capability ``llm`` не имеет права делать
    это обязательной для всего процесса: забытый ключ провайдера уронил бы
    ``data`` и ``vectors``, которые к нему отношения не имеют.

    Отличие от молчаливого ``default=""`` — в читаемости реестра. Пустая
    строка выглядит как забытое значение, ``OPTIONAL`` — как решение. И
    страж реестра проверяет, что у платформенной настройки нет иных форм:
    значение в коде не проскочит ни под каким видом.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - только для сообщений
        return "<необязательно>"


#: Подставьте вместо значения настройки, которая может остаться незаданной.
OPTIONAL = OptionalValue()

#: Чем заменяется ``OPTIONAL`` при разрешении: пустое значение по типу.
#: Для чисел это ``None``, а не ``0``: ноль — это заданное значение, и
#: ``max_tokens=0`` ведёт себя не так же, как «параметра нет».
_OPTIONAL_EMPTY: dict[str, Any] = {
    "str": "",
    "secret": "",
    "int": None,
    "float": None,
    "bool": False,
    "list": [],
}

#: Подстановка ``${ПЕРЕМЕННАЯ}`` в значении из ``platform.json``. Имена —
#: как в окружении, без префиксов: файл читает человек, и ``${DB_USER}``
#    понятнее, чем ``${ENTERPRISE_DB_USER}``, которого не существует.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Setting:
    """Одна настройка: как её зовут, чем она является и кто её читает.

    Attributes:
        name: основное имя переменной окружения.
        aliases: дополнительные имена, если основное не задано.
        kind: ``int`` | ``float`` | ``bool`` | ``str`` | ``list`` | ``secret``.
        default: значение при отсутствии в окружении и файле. ``None``
            означает «без значения», а не «пустая строка».
        owner: ``platform`` — настройка живёт в ``platform.json``;
            ``agent`` — приходит из окружения, в файле ей не место.
        reader: кто читает; приводится в тексте настройки, чтобы при
            поиске «кто это трогает» не пришлось открывать все файлы.
        purpose: зачем настройка существует, в одну строку.
        required: без значения и без дефолта — операция обязана падать с
            внятной ошибкой, а не работать вслепую.
        file_first: файл важнее окружения. Нужно там, где окружение —
            не объявление, а побочный эффект наследования: дочерний процесс
            ``enterprise-mcp`` получает ``dict(os.environ)`` агента целиком,
            и ``DATABASE_URL`` в нём — это секрет агента, а не решение
            платформы о своей базе. Своя конфигурация владельца бьёт
            унаследованный глобал.
    """

    name: str
    kind: str
    default: Any
    owner: str
    reader: str
    purpose: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    file_key: str = ""
    file_first: bool = False
    """Ключ вложенной секции ``platform.json`` (например ``data.log_table``).

    Человек пишет ``{"data": {"log_table": ...}}``, а окружение получает
    ``ENTERPRISE_LOG_TABLE``. Связь между ними живёт здесь, а не в читателе
    и не в файле: иначе пришлось бы угадывать имя переменной по имени
    секции, и опечатка стала бы молчаливой потерей настройки.
    """
    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def key(self) -> str:
        return self.file_key or self.name

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"

    def coerce(self, raw: Any) -> Any:
        """Привести значение из окружения или файла к типу настройки.

        Raises:
            InfrastructureError: значение не приводится к объявленному
                типу. Молчаливый откат к дефолту выглядел бы как «настройка
                не применилась», и оператор искал бы не там.
        """
        if self.kind == "table_list":
            return _table_pairs(raw, self.name)
        if self.kind == "json":
            return _json_object(raw, self.name)
        text = str(raw).strip()
        if self.kind in ("int", "float", "bool", "list"):
            if not text:
                return self.default
        try:
            if self.kind == "int":
                return int(text)
            if self.kind == "float":
                return float(text)
            if self.kind == "bool":
                return text.lower() in ("1", "true", "yes", "on")
            if self.kind == "list":
                # Перевод строки — разделитель наравне с запятой: агент
                # экспортирует списки таблиц по одной на строку, и раньше
                # их читал разбор, который понимал оба. Забытая здесь
                # запятая склеила бы семь таблиц в одно имя — молча.
                return [
                    item.strip()
                    for item in text.replace("\n", ",").split(",")
                    if item.strip()
                ]
            if self.kind == "secret":
                return text
            return text
        except ValueError as exc:
            raise InfrastructureError(
                f"{self.name}: {raw!r} не приводится к типу {self.kind!r}"
            ) from exc


#: Метка, которой в объявлении помечена таблица реестра предустановленных
#: скриптов. По ней capability ``audit`` отделяет реестр от доменных
#: таблиц: реестр — метаданные, и читать его как схему модели нельзя.
SCRIPTS_REGISTRY_LABEL = "scripts_registry"


def _json_object(raw: Any, setting_name: str) -> dict[str, Any]:
    """Привести значение к JSON-объекту.

    Из файла объявление приходит объектом и остаётся им: оборачивать его в
    строку, а потом разбирать обратно — значит заставить человека править
    экранированный текст вместо читаемых объектов. Из окружения приходит
    строка (так объявляли при переносе) и разбирается здесь, с именем
    настройки в ошибке.
    """
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise InfrastructureError(
            f"{setting_name}: не разбирается как JSON ({exc})"
        ) from exc
    if not isinstance(parsed, dict):
        raise InfrastructureError(
            f"{setting_name}: ожидался JSON-объект, получено {type(parsed).__name__}"
        )
    return parsed


def _table_pairs(raw: Any, setting_name: str) -> tuple[tuple[str, str], ...]:
    """Привести объявление таблиц к паре ``(имя, метка)``.

    Формат тот же, что в ``project.json`` агента: список записей, у которых
    метка есть не у всех. Из окружения приходит строка — по таблице на
    строке или через запятую; метки в ней нет, и это не ошибка разбора, а
    недостаток самого канала: строка не умеет сказать «это реестр».
    """
    if isinstance(raw, (list, tuple)):
        pairs: list[tuple[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                label = str(item.get("label") or "").strip()
            else:
                name, label = str(item).strip(), ""
            if not name:
                raise InfrastructureError(
                    f"{setting_name}: запись без имени — {item!r}"
                )
            pairs.append((name, label))
        return tuple(pairs)
    text = str(raw).strip()
    if not text:
        return ()
    return tuple(
        (item.strip(), "")
        for item in text.replace("\n", ",").split(",")
        if item.strip()
    )


def _s(
    name: str,
    kind: str,
    default: Any,
    owner: str,
    reader: str,
    purpose: str,
    *,
    aliases: tuple[str, ...] = (),
    required: bool = False,
    file_key: str = "",
    file_first: bool = False,
) -> Setting:
    return Setting(
        name=name,
        kind=kind,
        default=default,
        owner=owner,
        reader=reader,
        purpose=purpose,
        aliases=aliases,
        required=required,
        file_key=file_key,
        file_first=file_first,
    )


#: Полный перечень настроек платформы. Единственный список: и документация,
#: и страж сверяются с ним, а не с копией в тесте.
SETTINGS: tuple[Setting, ...] = (
    # -- подключение к базе -------------------------------------------------
    # DSN живёт в файле, но учётная часть — подстановками. Пароль от рабочей
    # базы в коммитируемом файле недопустим, а хост, порт и имя базы — это и
    # есть то, ради чего DSN вообще хочется видеть в конфигурации: по умолчанию
    # процесс молча унаследует чужой DSN из окружения агента, и непонятно, к
    # какой базе он подключится. Поэтому:
    # ``postgresql://${DB_USER}:${DB_PASSWORD}@хост:5432/база``.
    #
    # Пустое значение — не «умолчание», а осознанный ответ «это развёртывание
    # держит DSN в окружении»; тогда сервер падает на старте с внятным
    # текстом, а не подключается не туда.
    _s("DATABASE_URL", "secret", "", OWNER_PLATFORM,
       "servers/enterprise/server.py:_configure_dsn",
       "DSN рабочей базы; пусто — воркер падает внятно, а не подключается не туда",
       aliases=("PG_DSN",), required=True, file_key="db.dsn", file_first=True),
    # -- capability audit ----------------------------------------------------
    # Объявление переехало из окружения агента в platform.json, но form не
    # менялся: это тот же список записей, что в project.json, где реестр
    # предустановленных скриптов помечен label. Метку нельзя заменить
    # отдельным ключом — она и объясняет, почему реестр не доменная
    # таблица, и потерялась бы вместе с формой.
    # ``required`` не нужен и противоречил бы FROM_FILE: отсутствие ключа в
    # файле останавливает сервер на старте с именем ключа, то есть настройка
    # не может разрешиться в «нет значения».
    _s("ENTERPRISE_AUDIT_TABLES", "table_list", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_audit_config",
       "объявление таблиц аудита; запись с label='scripts_registry' — реестр "
       "предустановленных скриптов, в доменные таблицы не входит",
       file_key="audit.tables"),
    _s("ENTERPRISE_AUDIT_ROW_CEILING", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_audit_config",
       "потолок строк для generate_sql; 0 — не применять. Потолок защищает "
       "базу от сгенерированного запроса, поэтому решение платформенное: "
       "агент его не экспортирует, и до появления ключа в файле действовал "
       "дефолт из кода, то есть значение никто не задавал",
       file_key="audit.row_ceiling"),
    # -- capability legal_summarizer ---------------------------------------
    #
    # Владелец — платформа. До переноса корень состояния операции выводился
    # из расположения модуля (``Path(__file__).parents[N]``), и якорь, верный
    # в агенте, после переноса указал на каталог над репозиторием: кэш и
    # follow-up запросы оказывались в домашнем каталоге пользователя. Значение
    # обязано приходить из объявления, а не из расположения файла, поэтому
    # ключ пустой, а не «выводить по умолчанию».
    _s("ENTERPRISE_LEGAL_CACHE_ROOT", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/capabilities/legal_summarizer/service/main.py",
       "корень состояния операций legal_summarizer; пусто - каталог данных "
       "платформы, а не каталог, выведенный из расположения модуля",
       file_key="legal_summarizer.cache_root"),
    # -- capability vectors --------------------------------------------------
    #
    # Владелец — платформа. Снимок DuckDB, объявления индексов и подпись
    # эмбеддера живут здесь, потому что владеет ими тот, кто строит индексы
    # и проверяет их свежесть, — процесс enterprise-mcp. Когда они приходили
    # переменными от агента, у процесса не было ни одного собственного
    # ответа на вопрос «какой индекс считается актуальным»: он переписывал
    # чужое объявление и не мог его проверить.
    # Путь к снимку — OPTIONAL, а не FROM_FILE: capability ``vectors`` имеет
    # право отсутствовать (занятый или битый файл не должен ронять процесс),
    # и пустой путь — это «снимок ненастроен», а не «сервер не запустится».
    # Значение живёт в файле, а не приходит окружением агента: два владельца
    # одного файла означали бы, что снимок читают не оттуда, откуда пишут.
    _s("ENTERPRISE_SNAPSHOT_PATH", "str", OPTIONAL, OWNER_PLATFORM,
       "servers/enterprise/server.py:_snapshot",
       "путь к файлу снимка DuckDB; пусто — снимок не настроен",
       file_key="data.snapshot_path"),
    _s("ENTERPRISE_VECTOR_INDEXES", "json", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "объявления индексов; входят в подпись индекса, поэтому объявляются "
       "здесь, а не приходят из чужого окружения",
       file_key="vectors.indexes"),
    _s("ENTERPRISE_VECTOR_STORAGE_TABLE", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "инфраструктурная таблица хранения векторов; она же таблица "
       "эмбеддингов внутри снимка — второй настройки для того же факта "
       "заводить не будем",
       file_key="vectors.storage_table"),
    _s("ENTERPRISE_VECTOR_ENABLE", "bool", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "выключатель capability vectors",
       file_key="vectors.enable"),
    _s("ENTERPRISE_EMBED_DIMENSION", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "размерность вектора эмбеддера; входит в подпись индекса",
       file_key="llm.embed_dimension"),
    _s("ENTERPRISE_EMBED_TIMEOUT", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "таймаут HTTP-запроса к эмбеддеру, сек", file_key="llm.embed_timeout"),
    # -- capability llm ------------------------------------------------------
    #
    # Владелец — платформа, а не агент. Сервис общения с моделью живёт
    # здесь, и настройки его провайдера обязаны жить рядом с ним: агентская
    # копия конфига была второй копией, которая разъезжалась с этой при
    # первой правке модели. Ключ — подстановкой ``${LLM_API_KEY}``: он
    # разворачивается из ``mcp-platform/.secrets.env`` (свои секреты
    # платформы), а не из репозитория агента, и общая жизнь ключа связала
    # бы развёртывания, которые обязаны жить отдельно.
    #
    # Дефолт — пустая строка, а **не** ``FROM_FILE``, и это единственное
    # отступление от правила платформенных настроек. Причина в назначении
    # capability: ``llm`` необязательна, и её отсутствие не имеет права
    # снимать из работы ``data``, ``audit`` и ``vectors``. ``FROM_FILE``
    # означал бы, что сервер не поднимется без ключа провайдера, то есть
    # необязательная capability стала бы обязательной для всего процесса.
    # Пустое значение — законное состояние, а не забытый ключ: оно видно в
    # баннере (``configured: false``) и приходит как ``infrastructure_error``
    # на своей операции. Настройки, отсутствие которых действительно ломает
    # сервер, объявлены ``FROM_FILE`` — см. ``db.dsn``, ``pool.*``, ``data.*``.
    _s("ENTERPRISE_LLM_PROVIDER", "str", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "провайдер чата", aliases=("LLM_PROVIDER",), required=True,
       file_key="llm.provider"),
    _s("ENTERPRISE_LLM_MODEL", "str", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "модель чата", aliases=("LLM_MODEL",), required=True,
       file_key="llm.model"),
    _s("ENTERPRISE_LLM_API_BASE", "str", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "адрес эндпойнта чата", aliases=("LLM_API_BASE",),
       file_key="llm.api_base"),
    _s("ENTERPRISE_LLM_API_KEY", "secret", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "ключ чата; в файле — подстановка ${LLM_API_KEY} из .secrets.env",
       aliases=("LLM_API_KEY",), file_key="llm.key"),
    _s("ENTERPRISE_LLM_MAX_TOKENS", "int", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "потолок токенов ответа", aliases=("LLM_MAX_TOKENS",),
       file_key="llm.max_tokens"),
    _s("ENTERPRISE_LLM_TEMPERATURE", "float", OPTIONAL, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "температура чата", aliases=("LLM_TEMPERATURE",),
       file_key="llm.temperature"),
    # Эмбеддер — платформенный, как и чат: HTTP-вызов к нему делает тот же
    # владелец (``libs/llm``). Раньше модель эмбеддера объявлялась агентом на
    # том основании, что в подпись индекса входит модель, а индекс строит и
    # проверяет агент. Проверяющая сторона отсюда ушла: подпись считает
    # ``libs.vectors.signature`` в этом процессе, и объявление модели в
    # чужом окружении означало, что половина подписи живёт здесь, а
    # половина — там, и расхождение видно только как «индекс STALE».
    # Дубликат, который остаётся до переноса build-инструментов (фаза 8), —
    # объявление индекса у агента для ``build_vectors.py``; он объявлен
    # явно в ``platform.json``, а не приходит неявно через окружение.
    _s("ENTERPRISE_EMBED_API_BASE", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "адрес эндпойнта эмбеддингов; эмбеддер и чат — разные провайдеры, "
       "поэтому адрес свой", file_key="llm.embed_api_base"),
    # Ключ эмбеддера не пустой не из-за безопасности, а из-за кода:
    # ``libs/llm/embeddings.py`` выбирает ``embed_api_key`` или ``api_key``,
    # то есть без объявленного embed-ключа в запрос к эмбеддеру ушёл бы
    # облачный чатный ключ — секрет, которому там не место. Пустое значение
    # здесь означало бы утечку по недосмотру, а не «авторизация не нужна».
    #
    # Локальный Ollama авторизацию не проверяет, поэтому само значение —
    # соглашение о том, что запрос авторизован, а не секрет в смысле
    # защиты. Секретом оно остаётся только потому, что лежит в
    # ``mcp-platform/.secrets.env``, а не в platform.json под git.
    #
    # Отдельно: ``config.json → providers.ollama.apiKey`` — не источник
    # этого ключа. Это реестр чат-провайдеров nanobot, и эмбеддер он не
    # описывает. Ключ агента — ``EMBED_TOKEN`` из его окружения.
    _s("ENTERPRISE_EMBED_API_KEY", "secret", FROM_FILE, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "ключ эмбеддингов; в файле — подстановка ${EMBED_TOKEN} из "
       ".secrets.env платформы. Общий с чатом секрет был бы ошибкой: "
       "эмбеддер локальный (Ollama), чат облачный, развёртывания разные",
       file_key="llm.embed_key"),
    _s("ENTERPRISE_EMBED_PATH", "str", FROM_FILE, OWNER_PLATFORM,
       "libs/llm/config.py:resolve_llm_config",
       "путь эндпойнта эмбеддингов, если адрес задан полным URL",
       file_key="llm.embed_path"),
    _s("ENTERPRISE_EMBED_MODEL", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "модель эмбеддера; входит в подпись индекса",
       file_key="llm.embed_model"),
    # -- журнал и бюджеты запросов: собственные ручки платформы ---------------
    _s("ENTERPRISE_LOG_TABLE", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_log_table",
       "таблица долговечного журнала gateway", file_key="data.log_table"),
    _s("ENTERPRISE_LOG_QUESTION_RUNS_TABLE", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_question_runs_table",
       "таблица прогонов вопросов; настройки не было ни в реестре, ни в "
       "файле, поэтому сервис получал пустое значение и операции по прогонам "
       "вопросов отвечали ошибкой",
       file_key="data.question_runs_table"),
    _s("ENTERPRISE_TASK_TABLE", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_task_table",
       "таблица очереди задач агента; имя объявляет платформа, а не "
       "вызывающая сторона — иначе вход в данные агента шёл бы мимо его "
       "конфигурации (та же причина, по которой ушли claim_task и "
       "update_task_status в решении 2.18; отменено change "
       "2026-10-02-task-queue-into-mcp)",
       file_key="data.task_table"),
    _s("ENTERPRISE_LOG_BUFFER_MAXLEN", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "потолок буфера журнала; переполнение теряет события, а не растёт",
       file_key="data.log_buffer_maxlen"),
    _s("ENTERPRISE_LOG_FLUSH_INTERVAL", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "период сброса буфера журнала в базу, сек",
       file_key="data.log_flush_interval"),
    # Размер батча раньше был литералом 256 в писателе, и при интервале сброса
    # 5 сек это невидимый потолок ~51 запись/сек: сколько бы операций ни
    # пришло, всё выше этого уходило в очередь и ждало следующего окна. Литерал
    # в коде сделал бы конфигурацию декоративной, поэтому размер — объявленная
    # настройка, как и потолок буфера.
    _s("ENTERPRISE_LOG_BATCH_SIZE", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/capabilities/data/service/writer.py:EventBuffer",
       "сколько строк журнала уходит в базу за один сброс; вместе с "
       "data.log_flush_interval задаёт устойчивую пропускную способность "
       "журнала (строк/сек ≈ log_batch_size / log_flush_interval)",
       file_key="data.log_batch_size"),
    # Срок жизни журнала и чистка мусора — ручки платформы, а не агента:
    # очистка журнала пишет в ту же базу, и агентский ``retention_days``
    # был вторым владельцем правила, из-за чего конфигурация расходилась с
    # тем, что сервер применяет на самом деле.
    _s("ENTERPRISE_LOG_RETENTION_DAYS", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "сколько дней хранится запись журнала; 0 — не вычищать по сроку",
       file_key="data.log_retention_days"),
    _s("ENTERPRISE_LOG_PURGE_EMPTY_OUTBOUND", "bool", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "вычищать ли записи с пустым outbound (stream-чанки) независимо "
       "от срока хранения",
       file_key="data.log_purge_empty_outbound"),
    # Словарь типов событий объявлен (libs/enterprise_common/eventing/types.py),
    # но на пути ``log_events`` он долго не проверялся: в базу попадала любая
    # непустая строка, и опечатка становилась новым постоянным типом. Строгий
    # режим по умолчанию выключен сознательно — агент шлёт свои snake_case-имена
    # (``tool_call``, ``outbound_final``, …), их в словаре нет, и отказ уронил бы
    # весь журнал агента. Поэтому по умолчанию ``soft``: событие записывается,
    # но расхождение считается и один раз на имя называется в лог, а счётчик
    # виден в ``DataService.stats()``. ``strict`` включает отдельный заход —
    # после того, как имена приведены к объявленным с обеих сторон.
    _s("ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY", "str", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/capabilities/data/service/main.py:DataService",
       "как проверяется объявленный словарь типов на пути log_events: soft — "
       "записать и показать расхождение, strict — отклонить вызов (включать "
       "только после унификации имён с агентом)",
       file_key="data.log_unknown_event_type_policy"),
    _s("ENTERPRISE_STATEMENT_TIMEOUT_MS", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "SET statement_timeout для запросов capability data, мс",
       file_key="data.statement_timeout_ms"),
    _s("ENTERPRISE_MAX_ROWS", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "потолок строк в ответах операций", file_key="data.max_rows"),
    # Настройки ``ENTERPRISE_EXPECTED_TABLES`` больше нет: schema_check
    # проверяет таблицы, объявленные в этом файле (журнал, прогоны вопросов,
    # таблицы аудита, реестр скриптов), а список runtime-таблиц агента был
    # принесён окружением и всегда проигрывал файлу, когда тот появлялся.
    # Runtime-таблицы агента он проверяет сам, на своём старте.
    #
    # Секция ``execution`` — слой исполнения операций (change
    # ``enterprise-mcp-platform``, фаза 8). Пороги и флаги живут здесь, а не в
    # коде операций: литерал в коде сделал бы значение декоративным, сервер
    # поднялся бы и применил бы не тот порог, который написан в документации.
    _s("ENTERPRISE_EXEC_MAX_INLINE_BYTES", "int", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/policy.py",
       "порог сериализованного результата, выше которого он сохраняется "
       "артефактом, байт",
       file_key="execution.max_inline_result_bytes"),
    _s("ENTERPRISE_EXEC_PREVIEW_BYTES", "int", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/policy.py",
       "сколько байт превью отдаётся вместо тела крупного результата",
       file_key="execution.preview_bytes"),
    _s("ENTERPRISE_EXEC_TIMEOUT_SEC", "float", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/pipeline.py",
       "предел времени на один вызов операции, с",
       file_key="execution.execution_timeout_sec"),
    _s("ENTERPRISE_EXEC_PERSIST_LARGE", "bool", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/pipeline.py",
       "сохранять крупный результат артефактом; false — отдать целиком и "
       "пометить превышение",
       file_key="execution.persist_large_results"),
    _s("ENTERPRISE_EXEC_QUALITY_CHECK", "bool", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/quality.py",
       "выполнять проверки качества результата",
       file_key="execution.quality_check_enabled"),
    _s("ENTERPRISE_EXEC_LOGGING", "bool", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/logger.py",
       "писать события исполнения в журнал",
       file_key="execution.logging_enabled"),
    _s("ENTERPRISE_EXEC_SESSION_ROOT", "str", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/session/workspace.py",
       "корень файлов сессий: каталоги сессий, крупные результаты, артефакты",
       file_key="execution.session_root"),
    _s("ENTERPRISE_EXEC_LOG_ARG_FIELDS", "str", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/execution/logger.py",
       "белый список полей аргументов, попадающих в журнал как метаданные; "
       "всё остальное логируется размером и хешем",
       file_key="execution.log_argument_fields"),
    _s("ENTERPRISE_EXEC_SESSION_EVENTS", "bool", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/eventing/writer.py",
       "писать события оборота файлом в каталог сессии; по умолчанию "
       "выключено — журнал в базе уже долговечен, вторая копия была бы "
       "вторым источником правды",
       file_key="execution.persist_session_events"),
    _s("ENTERPRISE_EXEC_REQUIRE_CALL_META", "bool", FROM_FILE, OWNER_PLATFORM,
       "libs/enterprise_common/loader.py",
       "требовать params._meta на каждом вызове (contract "
       "runtime/call-contract). false — принимать вызов без метаданных и "
       "писать предупреждение: переключается вместе с агентской стороной, "
       "иначе сервер откажет в вызовах, которые ещё не научились их слать",
       file_key="execution.require_call_meta"),
    # -- пул соединений -----------------------------------------------------
    # Размеры и таймауты пула — ручки платформы: агент о них не знает и
    # знать не должен. До этого они жили только в ``_DEFAULT_POOL``, то
    # есть задать их было нечем, а реестр молчал. Тот же отказ, что был
    # с ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE``: значение не имеет пути из
    # конфигурации.
    _s("ENTERPRISE_POOL_MIN_CONN", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "воркеров пула, поднимаемых на старте", file_key="pool.min_conn"),
    _s("ENTERPRISE_POOL_MAX_CONN", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "потолок пула: больше соединений не открывается никогда",
       file_key="pool.max_conn"),
    _s("ENTERPRISE_POOL_TIMEOUT", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "ожидание места в очереди и порог warning'а при ожидании аренды, сек",
       file_key="pool.pool_timeout"),
    _s("ENTERPRISE_POOL_QUEUE_MAXSIZE", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "предел длины очереди; переполнение — PoolTimeoutError, а не вечный блок",
       file_key="pool.queue_maxsize"),
    _s("ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "первая пауза перед повтором подключения, сек",
       file_key="pool.reconnect_backoff_sec"),
    _s("ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "потолок паузы между попытками подключения, сек",
       file_key="pool.reconnect_backoff_max_sec"),
    _s("ENTERPRISE_POOL_CONNECT_MAX_RETRIES", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "попыток подключения воркера до отказа",
       file_key="pool.connect_max_retries"),
    _s("ENTERPRISE_POOL_IDLE_TIMEOUT_SEC", "float", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "простой воркера, после которого он снимается до min_conn, сек",
       file_key="pool.idle_timeout_sec"),
    _s("ENTERPRISE_POOL_JOB_MAX_RETRIES", "int", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "повторов задания при повторяемой ошибке",
       file_key="pool.job_max_retries"),
    _s("ENTERPRISE_POOL_PRINT_ACTIVITY", "bool", FROM_FILE, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "вывод активности db-worker'ов в stderr процесса",
       file_key="pool.print_activity"),
)

#: Имя -> настройка. Построен один раз; единственный источник правды.
BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS}

#: Все имена, включая синонимы: страж сверяет по ним, иначе синоним
#: ``PG_DSN`` проскочил бы мимо проверки как «неизвестная переменная».
BY_ANY_NAME: dict[str, Setting] = {n: s for s in SETTINGS for n in s.names}


@dataclass(frozen=True)
class CapabilitySettings:
    """Настройки одной capability — в той же форме, в какой устроен код.

    Реестр повторяет структуру платформы, а не выдумывает свою: сначала
    capability, потом её ``service`` и её операции. Вопрос «кто читает эту
    настройку» тогда отвечает на вопрос про потребителя, а не про
    ``_build_container``, который только собирает сервисы.

    Attributes:
        name: имя capability — оно же секция в ``platform.json`` и каталог
            в ``servers/enterprise/capabilities/``.
        service: путь к ``service/main.py``. Проверяется стражем: если
            сервиса нет, настройки этой capability читать некому.
        tools: операции capability. Тоже проверяются — настройка, которая
            никому из них не нужна, вероятно, объявлена зря.
        settings: имена настроек из ``SETTINGS``.
    """

    name: str
    service: str
    tools: tuple[str, ...]
    settings: tuple[str, ...]
    summary: str = ""


#: Capability платформы. Порядок и состав — как на диске, в
#: ``servers/enterprise/capabilities/``; страж сверяет оба.
CAPABILITIES: tuple[CapabilitySettings, ...] = (
    CapabilitySettings(
        name="data",
        service="servers/enterprise/capabilities/data/service/main.py",
        tools=(
            "append_assistant_message",
            "claim_task",
            "delete_assistant_message",
            "fail_task",
            "finalize_turn",
            "get_message",
            "history_search",
            "log_event",
            "log_events",
            "merge_tool_delivery",
            "patch_message_metadata",
            "purge_logs",
            "queue_stats",
            "release_claimed_tasks",
            "schema_check",
            "unstick_tasks",
            "update_task_status",
            "upsert_question_run",
        ),
        settings=(
            "ENTERPRISE_LOG_TABLE",
            "ENTERPRISE_LOG_QUESTION_RUNS_TABLE",
            "ENTERPRISE_TASK_TABLE",
            "ENTERPRISE_LOG_BUFFER_MAXLEN",
            "ENTERPRISE_LOG_FLUSH_INTERVAL",
            "ENTERPRISE_LOG_BATCH_SIZE",
            "ENTERPRISE_LOG_RETENTION_DAYS",
            "ENTERPRISE_LOG_PURGE_EMPTY_OUTBOUND",
            "ENTERPRISE_LOG_UNKNOWN_EVENT_TYPE_POLICY",
            "ENTERPRISE_STATEMENT_TIMEOUT_MS",
            "ENTERPRISE_MAX_ROWS",
        ),
        summary="PostgreSQL: очередь задач, долговечный журнал, чтение журнала",
    ),
    CapabilitySettings(
        name="audit",
        service="servers/enterprise/capabilities/audit/service/main.py",
        tools=("generate_sql", "list_scripts", "run_script"),
        settings=(
            "ENTERPRISE_AUDIT_TABLES",
            "ENTERPRISE_AUDIT_ROW_CEILING",
        ),
        summary="запрос к данным агента: реестр скриптов, их выполнение, генерация SQL",
    ),
    CapabilitySettings(
        name="vectors",
        service="servers/enterprise/capabilities/vectors/service/main.py",
        tools=("index_stats", "list_indexes", "vector_search"),
        settings=(
            "ENTERPRISE_SNAPSHOT_PATH",
            "ENTERPRISE_VECTOR_INDEXES",
            "ENTERPRISE_VECTOR_STORAGE_TABLE",
            "ENTERPRISE_VECTOR_ENABLE",
            "ENTERPRISE_EMBED_MODEL",
            "ENTERPRISE_EMBED_DIMENSION",
            "ENTERPRISE_EMBED_TIMEOUT",
        ),
        summary="снимок DuckDB, FAISS-индексы и параметры эмбеддера",
    ),
    CapabilitySettings(
        name="llm",
        service="servers/enterprise/capabilities/llm/service/main.py",
        tools=("complete", "embed"),
        settings=(
            "ENTERPRISE_LLM_PROVIDER",
            "ENTERPRISE_LLM_MODEL",
            "ENTERPRISE_LLM_API_BASE",
            "ENTERPRISE_LLM_API_KEY",
            "ENTERPRISE_LLM_MAX_TOKENS",
            "ENTERPRISE_LLM_TEMPERATURE",
            "ENTERPRISE_EMBED_API_BASE",
            "ENTERPRISE_EMBED_API_KEY",
            "ENTERPRISE_EMBED_PATH",
            "ENTERPRISE_EMBED_MODEL",
        ),
        summary="вызовы чата и эмбеддингов к внешнему провайдеру",
    ),
    CapabilitySettings(
        name="legal_summarizer",
        service="servers/enterprise/capabilities/legal_summarizer/service/main.py",
        tools=("query_operation",),
        settings=("ENTERPRISE_LEGAL_CACHE_ROOT",),
        summary="follow-up вопросы по уже разобранному юридическому документу",
    ),
)

#: Настройки вне capability: ими владеет общий код платформы.
#:
#: Пул соединений нужен всем capability сразу, и ни одной из них не
#: принадлежит: ``data`` ходит в базу напрямую, ``audit`` — через снимок,
#: загрузчик снимка — тоже через пул. Объявлять пул в секции ``data``
#: означало бы, что размер пула — дело capability PostgreSQL-очереди.
SHARED_SETTINGS: tuple[str, ...] = (
    "DATABASE_URL",
    "ENTERPRISE_POOL_MIN_CONN",
    "ENTERPRISE_POOL_MAX_CONN",
    "ENTERPRISE_POOL_TIMEOUT",
    "ENTERPRISE_POOL_QUEUE_MAXSIZE",
    "ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC",
    "ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC",
    "ENTERPRISE_POOL_CONNECT_MAX_RETRIES",
    "ENTERPRISE_POOL_IDLE_TIMEOUT_SEC",
    "ENTERPRISE_POOL_JOB_MAX_RETRIES",
    "ENTERPRISE_POOL_PRINT_ACTIVITY",
    # Слой исполнения операций: платформенный, ни одной capability
    # не принадлежит — порог ответа или таймаут вызова не дело домена.
    "ENTERPRISE_EXEC_MAX_INLINE_BYTES",
    "ENTERPRISE_EXEC_PREVIEW_BYTES",
    "ENTERPRISE_EXEC_TIMEOUT_SEC",
    "ENTERPRISE_EXEC_PERSIST_LARGE",
    "ENTERPRISE_EXEC_QUALITY_CHECK",
    "ENTERPRISE_EXEC_LOGGING",
    "ENTERPRISE_EXEC_SESSION_ROOT",
    "ENTERPRISE_EXEC_LOG_ARG_FIELDS",
    "ENTERPRISE_EXEC_SESSION_EVENTS",
    "ENTERPRISE_EXEC_REQUIRE_CALL_META",
)

#: Секции ``platform.json``, которых нет в ``servers/enterprise/capabilities``:
#: они принадлежат общему коду платформы. Объявлены явно, потому что страж
#: сверяет с ними файл — секция, не принадлежащая ни capability, ни этому
#: списку, выглядела бы как «настройка прочитана».
#:
#: ``profiles`` — оверлей имён таблиц для не-продовых контуров. Значением
#: настройки не является и в реестре не значится: это подстановка к файлу, а не
#: ещё одна переменная окружения. В списке он по той же причине, что и ``pool``
#: — пересекает сразу несколько capability, ни одной из них не принадлежит.
SHARED_SECTIONS: tuple[str, ...] = ("db", "pool", "execution", "profiles")

#: Ключ пула -> имя настройки. Связь названа один раз здесь, и ею пользуется
#: :func:`pool_config`: иначе второй список ключей разошёлся бы с первым, и
#: неизвестный ключ уехал бы в ``set_pool_config`` молча.
POOL_SETTING_KEYS: dict[str, str] = {
    "min_conn": "ENTERPRISE_POOL_MIN_CONN",
    "max_conn": "ENTERPRISE_POOL_MAX_CONN",
    "pool_timeout": "ENTERPRISE_POOL_TIMEOUT",
    "queue_maxsize": "ENTERPRISE_POOL_QUEUE_MAXSIZE",
    "reconnect_backoff_sec": "ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC",
    "reconnect_backoff_max_sec": "ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC",
    "connect_max_retries": "ENTERPRISE_POOL_CONNECT_MAX_RETRIES",
    "idle_timeout_sec": "ENTERPRISE_POOL_IDLE_TIMEOUT_SEC",
    "job_max_retries": "ENTERPRISE_POOL_JOB_MAX_RETRIES",
    "print_activity": "ENTERPRISE_POOL_PRINT_ACTIVITY",
}


def pool_config(settings: Settings) -> dict[str, Any]:
    """Разрешённые значения пула в форме ``set_pool_config``."""
    return {key: settings.get(name) for key, name in POOL_SETTING_KEYS.items()}


#: capability -> её настройки, для файла и документации.
SETTINGS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    c.name: c.settings for c in CAPABILITIES
}


def settings_owned_by(owner: str) -> tuple[Setting, ...]:
    return tuple(s for s in SETTINGS if s.owner == owner)


def capabilities_of(setting_name: str) -> tuple[str, ...]:
    """Какие capability заинтересованы в настройке (может быть несколько).

    Один и тот же параметр нужен двум capability — например, модель эмбеддера
    нужна и ``vectors`` (подпись индекса), и ``llm`` (вызов эндпойнта).
    Объявлять её дважды нельзя, поэтому настройка одна, а список
    capability — рядом.
    """
    return tuple(c.name for c in CAPABILITIES if setting_name in c.settings)


#: Ключ файла -> настройка. Строится из ``file_key``, а не из имени
#: переменной: человек в файле пишет ``data.log_table``.
BY_FILE_KEY: dict[str, Setting] = {
    s.key: s for s in SETTINGS if s.owner == OWNER_PLATFORM
}


def read_secrets(path: Path | None = None) -> dict[str, str]:
    """Прочитать ``mcp-platform/.secrets.env`` в вид ``имя -> значение``.

    Формат как у агентского ``.secrets.env``: ``ИМЯ=значение``, ``#`` —
    комментарий, ``export`` в начале допустим. Файла может не быть — тогда
    секреты приходят из окружения процесса.

    Файл читает только реестр. Секреты из двух источников — это тот же второй
    путь, от которого мы ушли: значение начинает расходиться между
    окружением и файлом, и непонятно, какое из них применилось.

    Raises:
        InfrastructureError: строка не ``ИМЯ=значение`` или значение
            повторяется. Молча пропущенная строка означала бы пустой секрет
            при виде заполненного файла.
    """
    target = path or SECRETS_PATH
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise InfrastructureError(f"{target.name}: не читается ({exc})") from exc
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise InfrastructureError(
                f"{target.name}, строка {number}: ожидалось ИМЯ=значение"
            )
        name = name.strip()
        if name in values:
            raise InfrastructureError(
                f"{target.name}, строка {number}: {name} уже задан выше"
            )
        values[name] = value.strip()
    return values


def _flatten(raw: dict[str, Any], opaque: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Развернуть вложенные секции в точечные ключи, один уровень глубины.

    Ключи, начинающиеся с подчёркивания (``_about``, ``_owner``), —
    комментарии в данных, а не настройки; они пропускаются молча, иначе
    пришлось бы держать в файле синтаксис комментариев ради двух строк.

    Args:
        opaque: ключи, значение которых — **структура** (список таблиц с
            метками, JSON-объект объявлений индексов). Их не разворачиваем:
            подстановка ключа внутрь значения превратила бы объявление в
            набор отдельных настроек, которых никто не объявлял.
    """
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            if str(key) in opaque:
                flat[str(key)] = value
                continue
            for sub, sub_value in value.items():
                if str(sub).startswith("_"):
                    continue
                flat[f"{key}.{sub}"] = sub_value
        else:
            flat[str(key)] = value
    return flat


#: Ключи, которые допускается перекрывать per-profile оверлеем в
#: ``platform.json → profiles.<имя>``. Список закрытый и совпадает по смыслу с
#: ``PROFILE_OWNED_RUNTIME_KEYS`` агента: журнал, прогоны вопросов и очередь
#: задач — те, что платформа пишет сама. Всё остальное (пул, LLM, эмбеддинги,
#: снимок, индексы) профилем не разделяется намеренно: это shared runtime
#: resources, и так же объявлен кэш-файл у агента
#: (``gateway.cache.local_path`` MUST NOT быть profile-owned).
PROFILE_OWNED_KEYS = frozenset({
    "data.log_table",
    "data.question_runs_table",
    "data.task_table",
})

#: Секции верхнего уровня ``platform.json``, которые НЕ являются настройками и
#: потому не проходят проверку «ключ известен». Из них берётся только ``profiles``
#: — оверлей имён таблиц (см. ``read_profile_overlay``).
RESERVED_SECTIONS = frozenset({"profiles"})


def read_profile_overlay(profile: str, path: Path | None = None) -> dict[str, str]:
    """Per-profile перекрытие имён таблиц из ``platform.json → profiles``.

    Возвращает плоский ``{file_key: значение}`` — ровно те ключи из
    ``PROFILE_OWNED_KEYS``, которые профиль объявил. Пустой результат означает
    «профиль нечего перекрывать», и это законно: профиль может разделять,
    например, только журнал.

    Профиль, которого в файле нет, — ошибка, а не пустой оверлей. Молчаливый
    возврат к базовым именам означал бы ровно то расхождение, ради которого
    профиль и заводится: агент с ``--profile test`` опрашивает
    ``agent_conversation_messages_test``, а платформа продолжает писать в
    ``agent_gateway_logs`` — и это видно только по содержимому боевого журнала.

    Raises:
        InfrastructureError: профиль не объявлен или объявляет чужой ключ.
    """
    target = path or PLATFORM_CONFIG_PATH
    if not target.exists():
        raise InfrastructureError(
            f"{target.name}: профиль {profile!r} запрошен, а файла нет"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfrastructureError(f"{target.name}: не читается ({exc})") from exc
    if not isinstance(raw, dict):
        raise InfrastructureError(f"{target.name}: ожидался объект")

    declared = raw.get("profiles")
    if declared is None:
        raise InfrastructureError(
            f"{target.name}: профиль {profile!r} запрошен, а секции "
            f"'profiles' в файле нет. Объявите её: "
            f"platform.json → profiles.{profile}"
        )
    if not isinstance(declared, dict) or profile not in declared:
        known = ", ".join(sorted(k for k in declared if not k.startswith("_"))) or "—"
        raise InfrastructureError(
            f"{target.name}: профиль {profile!r} не объявлен (известные: {known}). "
            f"Базовые имена таблиц применены бы не были — платформа писала бы "
            f"в боевые таблицы под тестовым профилем."
        )

    overlay = _flatten(declared[profile])
    unknown = sorted(set(overlay) - PROFILE_OWNED_KEYS)
    if unknown:
        raise InfrastructureError(
            f"{target.name}: профиль {profile!r} перекрывает "
            f"{', '.join(repr(k) for k in unknown)} — перекрывать можно только "
            f"{', '.join(sorted(PROFILE_OWNED_KEYS))}. Остальное — shared "
            f"runtime resources, они профилем не разделяются."
        )
    return {key: str(value) for key, value in overlay.items()}


def read_platform_file(path: Path | None = None) -> dict[str, Any]:
    """Прочитать ``platform.json`` в вид ``имя переменной -> значение``.

    Только ``owner="platform"``. Значение настройки агента в этом файле —
    это дублирование, и оно поднимается как ошибка, а не игнорируется:
    иначе файл тихо разошёлся бы с окружением, и виноват был бы тот, кто
    чинит последствия.

    Raises:
        InfrastructureError: файл не читается, не объект, содержит неизвестный
            ключ или ключ настройки агента.
    """
    target = path or PLATFORM_CONFIG_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfrastructureError(f"{target.name}: не читается ({exc})") from exc
    if not isinstance(raw, dict):
        raise InfrastructureError(f"{target.name}: ожидался объект")

    # ``profiles`` — не настройка, а секция оверлея: её читает
    # ``read_profile_overlay``. Здесь она снимается до развёртки, иначе её
    # ключи попали бы под проверку «ключ известен» и упали бы как опечатка.
    for reserved in RESERVED_SECTIONS:
        raw.pop(reserved, None)

    allowed = {s.key for s in settings_owned_by(OWNER_PLATFORM)}
    structured = frozenset(
        s.key for s in settings_owned_by(OWNER_PLATFORM)
        if s.kind in ("table_list", "json")
    )
    flat: dict[str, Any] = {}
    for key, value in _flatten(raw, structured).items():
        if key not in allowed:
            raise InfrastructureError(
                f"{target.name}: {key!r} — не настройка платформы "
                f"(владелец — агент, значение приходит через окружение)"
            )
        if isinstance(value, bool):
            flat[key] = "1" if value else "0"
        elif isinstance(value, (int, float)):
            flat[key] = str(value)
        elif BY_FILE_KEY[key].kind in ("table_list", "json"):
            # Список таблиц с метками и объявления индексов остаются
            # структурой. Склеить их в строку нельзя: метка
            # (scripts_registry) и объекты индексов и есть смысл
            # объявления, и потеря их сделала бы файл нечитаемым.
            flat[key] = value
        elif isinstance(value, (list, tuple)):
            flat[key] = ",".join(str(v) for v in value)
        else:
            flat[key] = str(value)
    return {BY_FILE_KEY[k].name: v for k, v in flat.items()}


class Settings:
    """Разрешённые значения настроек: окружение > файл > дефолт."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        file_path: Path | None = None,
        secrets_path: Path | None = None,
        secrets: Mapping[str, str] | None = None,
        profile: str | None = None,
    ) -> None:
        """
        Args:
            env: источник окружения; по умолчанию ``os.environ``. Реестр —
                единственный, кто его читает, поэтому остальным достаётся уже
                разрешённое значение.
            file_path: ``platform.json``; по умолчанию рядом с пакетом.
            secrets_path: ``.secrets.env`` платформы; по умолчанию рядом.
            secrets: готовый словарь секретов вместо чтения файла. Нужен там,
                где окружение передано явно: тест с ``env={}`` рассчитывает на
                полную изоляцию, а локальный ``.secrets.env`` разработчика
                подставился бы в него и сделал бы результат зависимым от
                машины.
            profile: имя профиля из ``platform.json → profiles``. Применяет
                оверлей имён таблиц (:func:`read_profile_overlay`). ``None`` —
                база, то есть prod. Имя приходит от агента, но **значения не
                приходят**: агент сообщает, какой контур, а что в нём писать —
                объявление платформы.
        """
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._secrets: dict[str, str] = (
            dict(secrets) if secrets is not None else read_secrets(secrets_path)
        )
        self._file: dict[str, Any] = read_platform_file(file_path)
        self._profile = (profile or "").strip() or None
        self._overlay: dict[str, str] = {}
        if self._profile is not None:
            # Оверлей накладывается на БАЗОВЫЙ плоский словарь до резолюции,
            # поэтому действует ровно как ещё одна запись в platform.json —
            # с тем же приоритетом файла над окружением и с тем же
            # разворачиванием ${ПЕРЕМЕННАЯ}.
            #
            # Ключи приходят именами файла (``data.log_table``), а ``_file``
            # хранится именами переменных (``ENTERPRISE_LOG_TABLE``):
            # ``read_platform_file`` переименовывает их на последнем шаге, и
            # оверлей обязан лечь в тот же алфавит, иначе он просто не
            # попадёт в резолюцию.
            for key, value in read_profile_overlay(
                self._profile, file_path
            ).items():
                self._file[BY_FILE_KEY[key].name] = value
        self._values: dict[str, Any] = {}
        self._sources: dict[str, str] = {}
        for setting in SETTINGS:
            self._resolve(setting)

    def _resolve(self, setting: Setting) -> None:
        """Разрешить одну настройку: окружение > файл > дефолт.

        Подстановка ``${ПЕРЕМЕННАЯ}`` разворачивается **только** для значений
        из файла. Окружение — уже готовое значение: его развернул агент, и
        разворачивать его повторно опасно, потому что пароль, содержащий
        ``${`` (вполне обычная последовательность), был бы съеден.
        """
        from_file: tuple[Any, str] | None = None
        if setting.file_first:
            from_file = self._from_file(setting)
        if from_file is None:
            for name in setting.names:
                raw = self._env.get(name)
                if raw is not None and raw.strip():
                    self._values[setting.name] = setting.coerce(raw)
                    self._sources[setting.name] = f"env:{name}"
                    return
            from_file = self._from_file(setting)
        if from_file is not None:
            self._values[setting.name] = setting.coerce(from_file[0])
            self._sources[setting.name] = from_file[1]
            return
        if setting.owner == OWNER_PLATFORM and setting.default is FROM_FILE:
            present = setting.name in self._file
            raise InfrastructureError(
                f"platform.json: {'пустое значение' if present else 'нет ключа'} "
                f"{setting.key!r}. Значение платформенной настройки живёт "
                "только в файле — в коде её нет намеренно, чтобы у вопроса "
                "«что применяется» не было двух ответов. "
                + (
                    "Заполните ключ в platform.json."
                    if present
                    else "Добавьте ключ в platform.json."
                )
            )
        self._values[setting.name] = (
            _OPTIONAL_EMPTY[setting.kind]
            if setting.default is OPTIONAL
            else setting.default
        )
        self._sources[setting.name] = "default"

    def _from_file(self, setting: Setting) -> tuple[Any, str] | None:
        """Значение из файла или ``None``, если ключа нет либо он пуст.

        Подстановки ``${...}`` разворачиваются и в списках — поимённо: имя
        таблицы из переменной окружения не собирается, но и лишней двери
        «забыли подставить» у файла быть не должно.
        """
        raw = self._file.get(setting.name)
        if isinstance(raw, dict):
            # Объявление индексов приходит объектом: подстановки в нём нет,
            # а str() превратил бы его в текст, который потом не прочитать.
            if not raw:
                return None
            return raw, "file:platform.json"
        if isinstance(raw, (list, tuple)):
            if not raw:
                return None
            # Подстановка — только в именах. Запись с меткой приходит
            # словарём, и str() на ней склеил бы объявление в текст,
            # который потом не разобрать.
            return [
                self._expand(item, setting) if isinstance(item, str) else item
                for item in raw
            ], "file:platform.json"
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        return self._expand(text, setting), "file:platform.json"

    def _secret(self, name: str) -> tuple[str, str] | None:
        """Секрет по имени: сначала ``.secrets.env``, потом окружение процесса.

        Секрет — это то же имя переменной, но живёт он в другом файле: рядом с
        настройками платформы, а не в репозитории агента. Окружение остаётся
        запасным источником, потому что развёртывание может задать секрет там
        (docker-compose, systemd), и требовать правки файла на каждый запуск
        было бы лишней церемонией.
        """
        for store, label in ((self._secrets, "secrets:.secrets.env"), (self._env, None)):
            value = store.get(name)
            if value is not None and value.strip():
                return value, label or f"env:{name}"
        return None

    def _expand(self, text: str, setting: Setting) -> str:
        """Развернуть ``${ПЕРЕМЕННАЯ}`` в значении из файла.

        Нужно для DSN и ключей: хост, порт и имя базы — это то, что хочется
        видеть в конфигурации, а логин с паролем в коммитируемый файл
        класть нельзя. Поэтому в файле лежит
        ``postgresql://${DB_USER}:${DB_PASSWORD}@хост``.

        Подстановка ищется в ``.secrets.env`` платформы и в окружении
        процесса. Отсутствующая переменная — ошибка, а не пустая строка:
        подставленная пустота дала бы ``postgresql://:@хост`` и отказ
        соединения там, где виновата опечатка в имени переменной.

        Разворачивается **только** значение из файла. Окружение — уже готовое
        значение: разворачивать его повторно опасно, потому что пароль,
        содержащий ``${`` (вполне обычная последовательность), был бы съеден.
        """
        missing: list[str] = []

        def _substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            found = self._secret(name)
            if found is None:
                missing.append(name)
                return ""
            return found[0]

        expanded = _PLACEHOLDER.sub(_substitute, text)
        if missing:
            raise InfrastructureError(
                f"platform.json, {setting.key}: не задана "
                + ", ".join(sorted(set(missing)))
                + ". Подстановка ${имя} читается из mcp-platform/.secrets.env "
                "или из окружения процесса; задайте значение или уберите "
                "подстановку."
            )
        return expanded

    def get(self, name: str) -> Any:
        """Значение настройки. Неизвестное имя — исключение, не ``None``."""
        if name not in self._values:
            raise InfrastructureError(f"настройка {name!r} не объявлена в реестре")
        return self._values[name]

    def source(self, name: str) -> str:
        """Откуда взято значение: ``env:*``, ``file:platform.json``, ``default``."""
        if name not in self._sources:
            raise InfrastructureError(f"настройка {name!r} не объявлена в реестре")
        return self._sources[name]

    def sections(self) -> Mapping[str, Any]:
        """Секции ``platform.json`` как есть, без разбора значений.

        Нужно потребителям, которым важна **структура** файла, а не отдельные
        значения: слою исполнения, чтобы разрешить политику «операция →
        capability → платформа», и стражам, сверяющим объявленные секции с
        каталогами. Значения отсюда не читаются напрямую — только структура;
        конкретные ключи по-прежнему проходят через :meth:`get`.
        """
        return MappingProxyType(self._file)

    def as_dict(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        """Снимок значений — для баннера запуска и диагностики.

        Args:
            reveal_secrets: если False (по умолчанию), секреты заменены на
                ``***``. Баннер печатается в лог, а лог читают люди и
                системы сбора — значение ключа там быть не должно.
        """
        out: dict[str, Any] = {}
        for setting in SETTINGS:
            value = self._values[setting.name]
            if setting.is_secret and value and not reveal_secrets:
                out[setting.name] = "***"
            else:
                out[setting.name] = value
        return out

    def file_backed(self) -> tuple[str, ...]:
        """Настройки, значение которых пришло из ``platform.json``.

        Для баннера запуска: значение из файла — единственное, о котором
        оператор не знает наверняка, потому что он не передавал его через
        окружение.
        """
        return tuple(
            name
            for name, source in self._sources.items()
            if source == "file:platform.json"
        )

    def as_env(self) -> dict[str, str]:
        """Разрешённые значения в виде ``имя переменной -> значение``.

        Нужен потребителям, которые принимают источник окружения параметром
        (``libs/llm``): они получают ровно то, что разрешил реестр, и не
        читают ``os.environ`` сами. Второй разбор — это и есть «настройка
        прописана в двух местах»: файл меняет значение для одного, а для
        второго остаётся окружение.

        Секреты здесь живут так же, как в окружении, и уходят дальше по
        процессу, — но не в лог: печатать их нельзя, поэтому для диагностики
        есть :meth:`as_dict`, который маскирует.
        """
        out: dict[str, str] = {}
        for setting in SETTINGS:
            value = self._values[setting.name]
            if value is None:
                continue
            if isinstance(value, bool):
                out[setting.name] = "1" if value else "0"
            elif setting.kind == "table_list":
                # Метка в окружение не передаётся: строка не умеет сказать
                # «это реестр». Передаём имена — потребитель, которому нужен
                # реестр, берёт его из файла, а не из процесса.
                out[setting.name] = ",".join(name for name, _ in value)
            elif isinstance(value, (list, tuple)):
                out[setting.name] = ",".join(str(item) for item in value)
            else:
                out[setting.name] = str(value)
        return out

    def missing_required(self) -> tuple[str, ...]:
        """Обязательные настройки без значения."""
        return tuple(
            s.name
            for s in SETTINGS
            if s.required and not self._values.get(s.name)
        )


#: Разобранные настройки процесса. См. :func:`platform_settings`.
_PLATFORM_SETTINGS: Settings | None = None


def platform_settings() -> Settings:
    """Настройки платформы, разобранные один раз на процесс.

    Composition root (``.build()`` в ``servers/enterprise/server.py``) собирает
    :class:`Settings` сам и передаёт объявленные значения сервисам явно. Этот
    помощник нужен тем потребителям, которые собираются **вне** того места
    (писатель буфера журнала, проверки приёма событий) и всё равно обязаны
    брать значение из ``platform.json``, а не из литерала в коде.

    Смысл кэша: реестр читает с диска ``platform.json`` и ``.secrets.env``, а
    настройки в процессе не меняются. Файл читает и подменяет только сам
    реестр — второй владелец разбора у него появиться не должен.
    """
    global _PLATFORM_SETTINGS
    if _PLATFORM_SETTINGS is None:
        _PLATFORM_SETTINGS = Settings()
    return _PLATFORM_SETTINGS
