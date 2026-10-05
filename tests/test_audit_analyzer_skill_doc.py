"""Страж ``SKILL.md`` навыка ``audit_analyzer`` (фаза 9, п. 9.1).

Приёмка фазы 9 требует, чтобы навык описывал операции платформы и не содержал
физических имён данных. Проверять это руками бессмысленно: файл переписывают
при каждой правке каталога, и запрет на имена таблиц держится ровно до первого
удобного упоминания. Ниже четыре независимых правила — каждое ловит свой класс
регрессии, и каждое проверено мутацией (правило проекта: страж, который не
пойман, — не страж).

Что здесь **нет** и почему: проверки «в SKILL.md не должно быть имени
навыка» не существует. Навык, названный своим именем в собственном файле,
безобиден; запрет на домен-маркеры относится к generic-слою
(``tests/test_architecture_tool_domain_free.py``), а не к файлу навыка.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILL_MD = REPO_ROOT / "workspace" / "skills" / "audit_analyzer" / "SKILL.md"
PLATFORM_JSON = REPO_ROOT / "mcp-platform" / "platform.json"
CONFIG = REPO_ROOT / "config.json"

#: Операции, которые навык объясняет модели. Держится в паре с белым списком
#: модели ``config.json → tools.mcpServers.enterprise.enabled_tools``: навык,
#: называющий операцию вне списка, уводит модель в вызов, которого у неё нет.
#: Проверку равенства объявлению делает ``tests/test_mcp_platform_declaration.py``;
#: здесь список нужен как «какие строки таблицы обязаны быть».
#:
#: ``vector_search`` и ``list_indexes`` — операции capability ``vectors``, а не
#: ``audit``, и это не оговорка: смысловой поиск по нарушениям и есть ответ на
#: вопрос про нарушения. ``list_indexes`` добавлена вместе с выдачей её модели
#: (см. ``TestSkillDocIndexCatalog``) — до этого навык держал имена индексов
#: таблицей у себя.
ROUTED_OPERATIONS = (
    "list_scripts",
    "run_script",
    "generate_sql",
    "vector_search",
    "list_indexes",
)

#: Обязательные аргументы каждой операции — в том виде, в каком они должны
#: стоять в колонке «Обязательные аргументы» таблицы выбора.
#:
#: Источник истины — опубликованная схема операции (``inputSchema``, которую
#: платформа отдаёт в ``tools/list``): ``list_scripts`` без аргументов,
#: ``run_script`` требует ``script``, ``generate_sql`` — ``query``.
#: Для ``vector_search`` схема требует только ``query``, а ``index_name``
#: необязателен — но цена его отсутствия несимметрична: платформа подставит
#: индекс по умолчанию, которого в объявлении нет, и поиск вернёт пустую
#: выдачу ВМЕСТО ошибки. Поэтому навык обязан требовать оба.
#: ``list_indexes`` аргументов не имеет — это каталог, а не запрос.
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "list_scripts": (),
    "run_script": ("script",),
    "generate_sql": ("query",),
    "vector_search": ("query", "index_name"),
    "list_indexes": (),
}

#: Строка таблицы операций: ``| `имя` | описание | обязательные аргументы |``.
_OPERATION_ROW = re.compile(
    r"^\|\s*`(?P<name>[a-z_]+)`\s*\|(?P<middle>[^|]*)\|(?P<args>[^|]*)\|\s*$",
    re.MULTILINE,
)

#: Физические имена, которых в навыке быть не должно.
#:
#: Домен и бизнес-глоссарий («нарушение», «критичность») остаются — это и есть
#: работа навыка. Запрещено то, чем владеет платформа: имена таблиц, движки
#: хранилища и поиска, пути к коду, который уехал из агента.
FORBIDDEN_SUBSTRINGS = {
    "oarb.": "имя таблицы/схемы аудита — объявлено на платформе",
    "agent_predefined_scripts": "таблица реестра скриптов — внутренность платформы",
    "agent_gateway_logs": "таблица журнала — не зона доменного навыка",
    "duckdb": "движок снимка принадлежит capability data",
    "faiss": "движок индексов принадлежит capability vectors",
    "sqlglot": "валидатор запросов уехал вместе с sql_safety",
    "sql_safety": "модуль агента удалён (фаза 9)",
    "CacheProvider": "интерфейс хранилища — не точка входа навыка",
    "validate_sql": "валидация запроса уехала на платформу",
    "scripts/cli.py": "CLI навыка удалён",
    "predefined/": "внутренний пакет навыка удалён",
    "workspace/skills/audit_analyzer/scripts": "Python-слой навыка удалён",
    "lib/utils/sql_safety": "модуль агента удалён (фаза 9)",
    "llm_client": "выбор модели принадлежит capability llm",
    "project.json": "конфигурация агента не управляет данными аудита",
}

#: Имена индексов в backticks: ``audits_index``, ``default_index``.
_INDEX_IN_BACKTICKS = re.compile(r"`([a-z][a-z0-9_]*_index)`")

#: Платформенное имя индекса по умолчанию. Оно **не объявлено** в разделе
#: ``vectors``, поэтому называть его в навыке можно только как предупреждение
#: («без ``index_name`` платформа подставит индекс по умолчанию»), но не как
#: элемент каталога. Проверка «навык не перечисляет имена индексов» его
#: поэтому не считает нарушением — иначе запрет запрещал бы само
#: предупреждение.
PLATFORM_DEFAULT_INDEX = "default_index"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _declared_indexes() -> set[str]:
    payload = json.loads(PLATFORM_JSON.read_text(encoding="utf-8"))
    return set(payload["vectors"]["indexes"])


def _declared_to_model() -> set[str]:
    """Операции, объявленные модели, — читаются из ``config.json``.

    Отдельный источник, а не ``ROUTED_OPERATIONS``: навык и объявление —
    разные решения, и их расхождение и есть то, что ловят проверки ниже.
    """
    payload = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    return set(payload["tools"]["mcpServers"]["enterprise"]["enabled_tools"])


def _mentioned_indexes(text: str) -> set[str]:
    return set(_INDEX_IN_BACKTICKS.findall(text))


def _operation_rows(text: str) -> dict[str, tuple[str, str]]:
    """Строки таблицы выбора операции: имя -> (описание, обязательные аргументы)."""
    return {
        match.group("name"): (match.group("middle"), match.group("args"))
        for match in _OPERATION_ROW.finditer(text)
    }


class TestSkillDocFrontmatter:
    def test_frontmatter_declares_skill_name(self) -> None:
        text = _skill_text()
        assert text.startswith("---\n"), "SKILL.md обязан начинаться с frontmatter"
        assert "\nname: audit_analyzer\n" in text.split("\n---", 1)[0], (
            "имя навыка в frontmatter обязано быть audit_analyzer — по нему "
            "навык находится при загрузке"
        )

    def test_description_points_at_platform(self) -> None:
        """Описание — то, что модель видит при выборе навыка.

        Старое описание обещало «три режима (predefined / vector /
        generated_sql)», которых больше нет. Проверка ловит возврат к обещанию
        несуществующей поверхности.

        Имена операций в описании **не** требуются: их несёт description
        инструмента, и навык, дублирующий четыре названия в строке для выбора,
        только раздувает её. Требуется одно — что навык называет платформенный
        путь, а не представляет себя владельцем данных.
        """
        frontmatter = _skill_text().split("\n---", 1)[0]
        for gone in ("predefined", "три режима", "CLI"):
            assert gone not in frontmatter, (
                f"frontmatter обещает {gone!r} — такого у навыка больше нет: "
                "данные обслуживает capability audit платформы"
            )
        assert "capability" in frontmatter, (
            "описание обязано называть capability платформы — иначе навык "
            "выглядит владельцем данных, а он им не является"
        )


class TestSkillDocDescribesOperations:
    @pytest.mark.parametrize("operation", ROUTED_OPERATIONS)
    def test_operation_is_documented(self, operation: str) -> None:
        assert f"`{operation}`" in _skill_text(), (
            f"операция {operation!r} не описана в SKILL.md: навык обязан "
            "объяснять, когда и с какими аргументами её звать"
        )

    @pytest.mark.parametrize("operation", ROUTED_OPERATIONS)
    def test_required_arguments_are_declared(self, operation: str) -> None:
        """Обязательные аргументы обязаны быть названы в таблице выбора.

        Проверка существования упоминания (выше) для этого недостаточна: имя
        операции встречается в документе ещё в дереве выбора, в правилах и в
        таблице ошибок, поэтому удаление её из таблицы её не скрывает. А вот
        потеря обязательного аргумента из колонки бьёт по модели напрямую —
        она вызывает операцию без него и получает отказ вместо данных.
        """
        rows = _operation_rows(_skill_text())
        assert operation in rows, (
            f"операции {operation!r} нет в таблице выбора — модель не видит ни "
            "описания, ни обязательных аргументов"
        )
        cell = rows[operation][1]
        present = set(re.findall(r"`([a-z_]+)`", cell))
        expected = set(REQUIRED_ARGS[operation])
        assert present == expected, (
            f"у {operation!r} в таблице указаны обязательные аргументы "
            f"{sorted(present)}, а по контракту tool'а должны быть "
            f"{sorted(expected)}"
        )

    def test_documented_operations_are_declared_to_the_model(self) -> None:
        """Строка таблицы обязана быть операцией, которая у модели есть.

        Проверяется объявление, а не наличие файла операции на платформе:
        ``index_stats`` файл имеет, но модели не объявлена, поэтому навык,
        назвавший её, обещал бы вызов, которого не будет.
        """
        unknown = set(_operation_rows(_skill_text())) - _declared_to_model()
        assert not unknown, (
            f"навык объясняет операции, не объявленные модели: {sorted(unknown)} — "
            "в config.json → tools.mcpServers.enterprise.enabled_tools их нет"
        )

    def test_no_unrouted_operation_is_promised(self) -> None:
        """Операция вне ``enabled_tools`` — обещание без инструмента.

        ``index_stats`` существует на платформе, но модели не объявлена: имя
        индекса и состояние приходят из ``list_indexes``, а состояние
        конкретного поиска — из ``vector_search``. Навык, обещавший её модели,
        отправил бы её в вызов, которого у модели нет.

        Раньше в этом перечне стояла и ``list_indexes`` — она объявлена модели
        теперь, и именно с неё берутся имена индексов.
        """
        text = _skill_text()
        for operation in ("index_stats",):
            assert f"`{operation}`" not in text, (
                f"{operation!r} не объявлена модели — упоминать её как доступную "
                "операцию нельзя"
            )


class TestSkillDocIndexCatalog:
    def test_skill_does_not_hardcode_index_names(self) -> None:
        """Навык не перечисляет имена индексов — их отдаёт платформа.

        Правило обратное прежнему. Навык держал таблицу из трёх имён, и это был
        единственный способ узнать новый индекс: whoever правит
        ``mcp-platform/platform.json → vectors.indexes`` обязан был пойти
        отредактировать ``SKILL.md``. Расхождение ловили два стража этого
        файла, но они ловили его **после** того, как кто-то уже завёл навык в
        согласие с платформой, — то есть заставляли повторять копирование, а
        не отменяли его.

        Теперь имена приходят из ``list_indexes`` (объявлена модели в
        ``config.json → tools.mcpServers.enterprise.enabled_tools``), поэтому
        перечисление в навыке — копия, которая протухает молча: новый индекс
        в платформе появится, а модель о нём не узнает, пока не спросит.
        """
        hardcoded = _mentioned_indexes(_skill_text()) - {PLATFORM_DEFAULT_INDEX}
        assert not hardcoded, (
            f"навык перечисляет имена индексов: {sorted(hardcoded)} — они "
            "объявляет платформа, модель берёт их из list_indexes"
        )

    def test_index_names_reach_the_model_from_the_platform(self) -> None:
        """То, что навык перестал перечислять, обязано приходить откуда-то.

        Проверка на объявление, а не на наличие файла операции: стража выше
        зелёная и при снятом из ``enabled_tools`` ``list_indexes``, то есть
        когда у модели не осталось бы ни одного способа узнать имя индекса.
        """
        assert "list_indexes" in _declared_to_model(), (
            "без list_indexes в enabled_tools имена индексов недоступны модели: "
            "навык их не перечисляет, а объявлять состав индексов агент не должен"
        )

    def test_the_platform_still_declares_indexes(self) -> None:
        """Спрашивать нечего — значит спрашивать не надо.

        Дыра, которую открывает отказ от перечисления в навыке: со всех сторон
        зелёная проверка при пустом ``platform.json → vectors.indexes``, где
        ``list_indexes`` отдаёт пустой каталог и навык советует модели спросить
        имена индексов, которых нет. Прежняя пара стража такой случай не ловила
        по построению: пустое объявление означало и пустое перечисление.
        """
        declared = _declared_indexes()
        assert declared, (
            "platform.json → vectors.indexes пуст: list_indexes отдаст пустой "
            "каталог, а навык больше не перечисляет имена индексов — модели "
            "неоткуда взять index_name"
        )


class TestSkillDocHasNoPhysicalDataNames:
    @pytest.mark.parametrize("token", sorted(FORBIDDEN_SUBSTRINGS))
    def test_forbidden_substring_absent(self, token: str) -> None:
        text = _skill_text()
        assert token not in text, (
            f"SKILL.md содержит {token!r} — {FORBIDDEN_SUBSTRINGS[token]}. "
            "Навык описывает операции, а не физическое хранение."
        )

    def test_no_shell_invocation(self) -> None:
        """Формат прежнего доступа — команда в консоли — умер вместе с CLI.

        Модель, увидев ``python ... --mode vector``, доверится строке сильнее,
        чем описанию инструмента, и потратит оборот на заведомо нерабочую
        команду вместо вызова.
        """
        text = _skill_text()
        for pattern in ("python workspace/skills", "python scripts/", "audit_analyze "):
            assert pattern not in text, (
                f"SKILL.md предлагает вызов {pattern!r} — CLI навыка удалён, "
                "единственный вход — операции capability audit: "
                "mcp_enterprise_{list_scripts,run_script,generate_sql,vector_search}"
            )
