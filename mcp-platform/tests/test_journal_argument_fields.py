"""Журнал вызова: ``operation_id`` объявлен полем аргументов, а не подобран.

Дефект, который ловит этот файл
--------------------------------

Идентификатор операции разбора (``platform.analyze_document`` /
``platform.query_operation``, change ``2026-10-05-legal-summarizer-session-scope``,
задача 5.12) в журнал вызовов не попадал. Запись об отказе по таймауту и запись о
начале прогона существуют, связать их нечем: поиск остаётся только по имени
операции и времени, то есть по догадке.

Механизм прежний — **белый список** полей
(``execution.log_argument_fields`` → ``policy.log_argument_fields`` →
``collect_argument_fields``, ``libs/enterprise_common/execution/logger.py:200-210``).
Механизм не трогали: добавлено одно имя в перечень, объявленный в файле.

Что проверяет файл
------------------

* перечень читается **из ``platform.json`` настоящим чтением настроек**, а не
  константой из текста теста: значение в тесте не повторяется, краснеет проба
  при удалении имени из файла;
* объявление приходит из файла (``source == file:platform.json``), а не из
  окружения и не из кода;
* имя доезжает до записи журнала по всей цепочке ``файл → реестр → политика →
  ``payload.arguments`` и ``payload.arguments_excerpt`` — то есть находится
  поиском ``history_search``;
* **отказ по таймауту** несёт то же имя: ради него задача и ставилась;
* перечень остаётся белым списком: необъявленный параметр по-прежнему не пишется;
* маскирование задевает новое поле: значение формы секрета в ``operation_id``
  скрывается и считается, а обычный идентификатор проходит дословно. Декларация
  поля не должна обходить маскирование — иначе «белый список» стал бы второй
  дорогой для утечки.

Файл не переписывает ``tests/test_payload_excerpt.py``: тот держит свойство
механизма на своих значениях, этот — объявление живого файла.

Чего файл не утверждает
-----------------------

Поле попадает в журнал **там, где оно аргумент вызова**. У
``platform.query_operation`` ``operation_id`` обязателен, у
``platform.analyze_document`` — необязателен: на первом запуске его нет среди
аргументов, он вычисляется внутри операции уже после того, как запись о начале
уехала в журнал (``_compute_operation_id``), и живёт в ответе. Такая запись
поля не несёт, и доказанным здесь это не сделано: корреляция первого запуска
по идентификатору — задача для ``servers/enterprise/tools/analyze_document.py``,
а не для перечня в файле. Пробы ниже проверяют ровно объявленный предмет: имя
объявлено и доезжает до записи, когда оно в аргументах.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from libs.enterprise_common.execution.errors import ExecutionTimeout
from libs.enterprise_common.execution.logger import REDACTED
from libs.enterprise_common.execution.policy import ExecutionPolicy
from libs.enterprise_common.settings import BY_NAME, Settings

from conftest import DUMMY_SECRETS, call_meta, make_layer

from test_tool_execution_pipeline import Sink, definition

FIELDS_SETTING = "ENTERPRISE_EXEC_LOG_ARG_FIELDS"
REDACT_SETTING = "ENTERPRISE_EXEC_LOG_REDACT_KEYS"

#: Идентификатор разбора в его настоящей форме: ``op_<hash>_<hash>_<length>``.
OPERATION_ID = "op_3fd766556d9b_9f18799e_detailed"


class _RegistryView(Mapping):
    """Вид реестра настроек для ``ExecutionPolicy.from_settings``.

    ``Settings.get`` принимает одно имя, а политика спрашивает значения с
    запасным (``get(имя, умолчание)``). Обёртка нужна ради одной строки
    протокола ``Mapping`` и ничего не подменяет: имя, которого нет в реестре,
    по-прежнему не значение, а отказ.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def __getitem__(self, name: str) -> Any:
        if name not in BY_NAME:
            raise KeyError(name)
        return self._settings.get(name)

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(BY_NAME))

    def __len__(self) -> int:
        return len(BY_NAME)


def _settings() -> Settings:

    """Настройки как на процессе: файл настоящий, окружение чистое.

    Окружение задаётся явно, а не берётся из процесса: иначе проба зависела бы от
    того, задал ли кто-то ``ENTERPRISE_EXEC_LOG_ARG_FIELDS`` руками, и краснела
    бы без правки файла.
    """
    return Settings(env=dict(DUMMY_SECRETS), secrets={})


def declared_fields() -> tuple[str, ...]:
    """Белый список полей аргументов, разобранный политикой платформы.

    Разбор идёт настоящим ``ExecutionPolicy.from_settings``, а не разделением
    строки в тесте: политика — единственное место, где форма файла
    превращается в перечень имён, и проба обязана краснеть на её правке.
    """
    return ExecutionPolicy.from_settings(_RegistryView(_settings())).log_argument_fields


def _overrides() -> dict[str, Any]:
    """Перекрытия слоя исполнения: объявления берутся из файла.

    Потолки выдержки подняты, чтобы проверка маскирования искала маркер, а не
    обрезок тела: усечение стало бы второй, не связанной с предметом причиной
    жёсткой проверки.
    """
    settings = _settings()
    return {
        FIELDS_SETTING: settings.get(FIELDS_SETTING),
        REDACT_SETTING: settings.get(REDACT_SETTING),
        "ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES": 4096,
        "ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES": 4096,
    }


def _execute(tmp_path: Path, handler: Any, arguments: dict[str, Any]) -> Sink:
    sink = Sink()
    layer = make_layer(tmp_path, sink=sink, **_overrides())
    layer.pipeline.execute(definition(handler), arguments, call_meta())
    return sink


class TestDeclaration:
    """Объявление в файле — источник, а не украшение."""

    def test_operation_id_is_declared_in_the_platform_file(self) -> None:
        """Имя объявлено в ``platform.json``, и пришло оно оттуда.

        Ожидание здесь одно — имя предмета. Весь перечень не повторяется
        константой: добавление или снятие соседнего поля эту пробу не волнует, а
        снятие ``operation_id`` роняет её сразу.
        """
        settings = _settings()
        assert settings.source(FIELDS_SETTING) == "file:platform.json", (
            "перечень полей пришёл не из platform.json "
            f"(источник {settings.source(FIELDS_SETTING)}); значение в коде или "
            "окружении сделало бы проверку ниже фиктивной"
        )
        fields = declared_fields()
        assert "operation_id" in fields, (
            "operation_id не объявлен в execution.log_argument_fields "
            f"(в перечне: {fields}) — запись об отказе по таймауту не с чем "
            "связать, кроме как поиском по времени запуска"
        )

    def test_declaration_is_a_whitelist_of_names(self) -> None:
        """Перечень остаётся перечнем имён: ни мусора, ни пустых мест."""
        raw = _settings().get(FIELDS_SETTING)
        assert isinstance(raw, str), raw
        fields = declared_fields()
        assert fields, "перечень пуст: в журнал не пишется ни одного поля"
        assert all(field.strip() == field and field for field in fields), fields
        assert len(fields) == len(set(fields)), f"в перечне повторы: {fields}"


class TestJournalRecord:
    """Объявленное имя доезжает до записи журнала."""

    def test_started_record_carries_operation_id(self, tmp_path: Path) -> None:
        """Запись о начале несёт ``operation_id`` и в структуре, и в выдержке.

        Выдержка — не украшение: журнал читают через ``data.history_search``, то
        есть подстрокой по полю ``payload``. Имя в одной структуре, но не в
        выдержке, искалось бы только разбором JSON на стороне читателя.
        """
        sink = _execute(tmp_path, lambda **_: {"ok": True}, {"operation_id": OPERATION_ID})
        payload = sink.of("tool.started")["payload"]
        assert payload["arguments"]["operation_id"] == OPERATION_ID, payload
        assert OPERATION_ID in payload["arguments_excerpt"], payload
        # Размер и хеш остаются полными: объявление поля не отменяет их.
        assert payload["arguments_size"] > 0, payload
        assert len(payload["arguments_hash"]) == 16, payload

    def test_timeout_record_carries_operation_id(self, tmp_path: Path) -> None:
        """Отказ по таймауту коррелируется с прогоном по имени операции.

        Именно этот случай из задачи 5.12: два вызова одного прогона — начало и
        отказ — обязаны называть одну операцию, иначе прогон ищется по времени.
        Проверяется и отличие от записи об успехе: отказ не должен выглядеть как
        успех.

        Запись об **успешном** завершении аргументов не несёт и не должна:
        ``logger.completed`` пишет сводку результата, а тело аргументов — в
        записи о начале. Успешный прогон коррелируется именно ею, поэтому
        сверка идёт по паре «начало + отказ» одного прогона.
        """
        def timed_out(**_: Any) -> dict[str, Any]:
            raise ExecutionTimeout("операция не уложилась в отведённое время")

        sink = _execute(tmp_path, timed_out, {"operation_id": OPERATION_ID})
        started = sink.of("tool.started")["payload"]
        refusal = sink.of("tool.timeout")["payload"]
        assert refusal["status"] == "timeout", refusal
        assert refusal["error_code"] == "timeout", refusal
        assert refusal["arguments"]["operation_id"] == OPERATION_ID, refusal
        assert started["arguments"]["operation_id"] == OPERATION_ID, (
            "запись о начале и запись об отказе называют разные операции — "
            "прогон не собрать"
        )

        ok = _execute(tmp_path, lambda **_: {"ok": True}, {"operation_id": OPERATION_ID})
        done = ok.of("tool.completed")["payload"]
        assert done["status"] == "ok", done
        assert done["status"] != refusal["status"], "отказ не отличим от успеха"

    def test_undeclared_argument_is_still_dropped(self, tmp_path: Path) -> None:
        """Добавленное имя не превратило белый список в «писать всё».

        Механизм объявления — часть требования, а не деталь: если бы слой стал
        писать любой аргумент, журнал начал бы тянуть тело каждого вызова, и
        страж молча снял бы потолок объявления. Проверяется на объявлении из
        файла, а не на тестовом.
        """
        undeclared = "текст-не-объявленного-параметра"
        sink = _execute(
            tmp_path,
            lambda **_: {"ok": True},
            {"operation_id": OPERATION_ID, "brand_new_argument": undeclared},
        )
        payload = sink.of("tool.started")["payload"]
        assert undeclared not in json.dumps(sink.rows, ensure_ascii=False), (
            "необъявленный параметр попал в журнал"
        )
        assert "brand_new_argument" not in payload["arguments"], payload
        assert "brand_new_argument" not in payload["arguments_excerpt"], payload


class TestMasking:
    """Объявленное поле проходит то же маскирование, что и остальные."""

    def test_operation_id_in_secret_form_is_masked_and_counted(self, tmp_path: Path) -> None:
        """Значение формы секрета скрывается и попадает в счётчик.

        Маскирование применяется к **отобранным** полям, поэтому новое имя в
        перечне обязано его унаследовать: иначе «объявил поле» означало бы
        «выключил для него маскирование». Имя поля при этом не секрет — значение
        формы (``postgresql://…``) ловится независимо от имени.
        """
        secret = "postgresql://user:pass@host:5432/db"
        sink = _execute(tmp_path, lambda **_: {"ok": True}, {"operation_id": secret})
        payload = sink.of("tool.started")["payload"]
        assert secret not in json.dumps(sink.rows, ensure_ascii=False), (
            "значение формы секрета попало в журнал через новое поле"
        )
        assert payload["arguments"]["operation_id"] == REDACTED, payload
        assert payload["arguments_masked"] == 1, payload

    def test_plain_operation_id_is_not_masked(self, tmp_path: Path) -> None:
        """Обычный идентификатор проходит дословно, счётчик остаётся нулевым.

        Идентификатор вычисляется из содержимого документа и не секретен. Если
        распознавание по форме начнёт цеплять такие имена, корреляция отказа с
        прогоном станет невозможной ровно там, ради чего объявление и сделано.
        """
        sink = _execute(tmp_path, lambda **_: {"ok": True}, {"operation_id": OPERATION_ID})
        payload = sink.of("tool.started")["payload"]
        assert payload["arguments"]["operation_id"] == OPERATION_ID, payload
        assert payload["arguments_masked"] == 0, (
            "идентификатор операции посчитан замаскированным: "
            f"{payload}"
        )
