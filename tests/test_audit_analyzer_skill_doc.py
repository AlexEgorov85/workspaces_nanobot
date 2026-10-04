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

#: Операции домена аудита. Держится в паре с белым списком модели
#: ``config.json → tools.mcpServers.enterprise.enabled_tools``: навык, называющий
#: операцию вне списка, уводит модель в вызов, которого у неё нет.
#: Проверку связи с объявлением делает ``tests/test_mcp_platform_declaration.py``;
#: здесь список нужен как «какие строки таблицы обязаны быть».
ROUTED_OPERATIONS = ("list_scripts", "run_script", "generate_sql", "vector_search")

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
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "list_scripts": (),
    "run_script": ("script",),
    "generate_sql": ("query",),
    "vector_search": ("query", "index_name"),
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
#: ``vectors`` — поэтому в навыке оно упоминается как предупреждение, а не как
#: каталог, и в пересечение с объявленными не попадает.
PLATFORM_DEFAULT_INDEX = "default_index"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _declared_indexes() -> set[str]:
    payload = json.loads(PLATFORM_JSON.read_text(encoding="utf-8"))
    return set(payload["vectors"]["indexes"])


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

    def test_no_unrouted_operation_is_promised(self) -> None:
        """Операция вне роутера — обещание, которого инструмент не выполнит.

        ``list_indexes`` и ``index_stats`` существуют на платформе, но
        ``audit_analyzer_query`` их не маршрутизирует. Навык, обещавший их
        модели, отправил бы её в вызов, который tool отсекает как
        ``invalid_operation``.
        """
        text = _skill_text()
        for operation in ("list_indexes", "index_stats"):
            assert f"`{operation}`" not in text, (
                f"{operation!r} не маршрутизируется инструментом — упоминать его "
                "как доступную операцию нельзя"
            )


class TestSkillDocIndexCatalog:
    def test_every_declared_index_is_documented(self) -> None:
        """Объявленный на платформе индекс обязан быть описан.

        Индексы не выводятся через discovery (в отличие от скриптов), поэтому
        навык — единственное место, где модель узнаёт их имена. Новый индекс в
        ``platform.json`` без строки в ``SKILL.md`` = молчаливо
        недоступный поиск.
        """
        declared = _declared_indexes()
        mentioned = _mentioned_indexes(_skill_text()) - {PLATFORM_DEFAULT_INDEX}
        missing = declared - mentioned
        assert not missing, (
            f"индексы объявлены на платформе, но не описаны в SKILL.md: "
            f"{sorted(missing)} — vector_search их не найдёт"
        )

    def test_no_phantom_index_is_documented(self) -> None:
        """Обратная сторона: описанный индекс обязан существовать."""
        declared = _declared_indexes()
        phantom = _mentioned_indexes(_skill_text()) - declared - {PLATFORM_DEFAULT_INDEX}
        assert not phantom, (
            f"SKILL.md упоминает индексы, не объявленные в platform.json: "
            f"{sorted(phantom)} — vector_search вернёт ошибку"
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
