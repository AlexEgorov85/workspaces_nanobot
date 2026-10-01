"""``schema_check`` проверяет то, чем пользуется платформа, а не чужие таблицы.

Раньше агент вычислял список своих runtime-таблиц
(``SchemaValidationService.expected_table_names``) и отдавал его процессу
переменной ``ENTERPRISE_EXPECTED_TABLES``, чтобы операция ``schema_check``
имела что проверять. Это была зависимость платформы от чужой конфигурации,
и она же была тихой поломкой: как только у платформы появлялось своё
объявление, старая переменная перебивала его (окружение приоритетнее файла).

Теперь ``schema_check`` проверяет таблицы, объявленные в
``mcp-platform/platform.json``: журнал, прогоны вопросов, таблицы аудита и
реестр предустановленных скриптов. Runtime-таблицы агента он не проверяет —
это делает сам агент на своём старте (``lib/services/schema_validation.py``),
и лишняя проверка в чужом процессе только разошлась бы с его.

Файл оставлен стражем: если операция снова начнёт ждать список извне, она
вернётся к отказу «не задано ни одной ожидаемой таблицы» при полностью
рабочей базе.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_FILE = REPO_ROOT / "mcp-platform" / "platform.json"
CONTEXT = REPO_ROOT / "lib" / "core" / "application_context.py"


def _data() -> dict:
    raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    return raw["data"]


class TestPlatformDeclaresWhatItUses:
    def test_log_and_question_runs_are_declared(self) -> None:
        """Обе таблицы, в которые платформа пишет, объявлены в файле."""
        data = _data()
        for key in ("log_table", "question_runs_table"):
            value = str(data.get(key) or "").strip()
            assert "." in value, (
                f"data.{key} = {value!r}: ожидалось '<schema>.<table>'. Без "
                f"объявления операции журнала и прогонов вопросов отвечали бы "
                f"«не настроено», а не работали бы."
            )

    def test_audit_tables_are_declared_too(self) -> None:
        raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
        assert raw["audit"]["tables"], "audit.tables пуст — schema_check нечего проверять"


class TestAgentDoesNotComputeIt:
    def test_helper_is_gone(self) -> None:
        source = CONTEXT.read_text(encoding="utf-8")
        assert "def _expected_tables(" not in source, (
            "_expected_tables снова объявлен: агент вычисляет список таблиц "
            "для платформы, то есть зависимость вернулась"
        )
        assert "ENTERPRISE_EXPECTED_TABLES" not in source, (
            "агент снова экспортирует список ожидаемых таблиц"
        )

    def test_client_takes_no_expected_tables(self) -> None:
        from lib.services import enterprise_mcp_client as mod

        import inspect

        params = set(inspect.signature(mod.client_from_settings).parameters)
        assert "expected_tables" not in params, (
            "фабрика клиента снова принимает список таблиц платформы"
        )
        init_params = set(
            inspect.signature(mod.EnterpriseMcpClient.__init__).parameters
        )
        assert "expected_tables" not in init_params
        assert "snapshot_path" not in init_params, (
            "клиент снова принимает путь снимка: файл снимка принадлежит "
            "платформе, второй владелец означал бы чтение не оттуда, откуда пишут"
        )
