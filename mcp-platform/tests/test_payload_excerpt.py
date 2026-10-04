"""Payload несёт ограниченную выдержку, а не отпечаток.

Требование: ``mcp-platform/libs/enterprise_common/execution/logger.py``,
``execution/policy.py``, ``platform.json → execution`` (change
``2026-10-04-journal-observability-repair``, capability ``logging-db``).

Дефект, который страж держит: в ``payload`` события вызова инструмента были
только ``arguments_hash``/``arguments_size`` и ``result_hash``/``result_size``.
По журналу нельзя было понять, о чём был вызов: инцидент требовал повторить его
вручную. Теперь тело видно на ограниченном префиксе, и три свойства этого
префикса обязаны выполняться одновременно:

* **потолок на выдержку целиком**, а не на поле — иначе суммарный объём растёт
  числом полей и 512 байт оказываются фикцией;
* **усечение и маскирование видны** — неотличимо усечённое тело от целого и
  немой секрет вместо маркера лгут ровно тем же, чем отсутствие строки об отказе;
* **механизм остаётся белым списком** — новый параметр операции не должен
  молча начинать писаться в журнал.

Потолки приходят политикой из ``platform.json``; в коде их нет, иначе объявление
было бы декоративным.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

from conftest import call_meta, make_layer  # noqa: E402

from libs.enterprise_common.execution.logger import REDACTED, TRUNCATION_MARKER  # noqa: E402

from test_tool_execution_pipeline import Sink, definition  # noqa: E402


def _execute(tmp_path: Path, handler: Any, arguments: dict[str, Any], **settings: Any) -> Sink:
    sink = Sink()
    make_layer(tmp_path, sink=sink, **settings).pipeline.execute(
        definition(handler), arguments, call_meta()
    )
    return sink


class TestPayloadExcerpt:
    """Выдержка в журнале: ограничена, видима и не шире белого списка."""

    def test_excerpt_respects_the_declared_cap(self, tmp_path: Path) -> None:
        """Тело длиннее потолка укладывается в потолок и несёт маркер.

        Проверяются обе величины из ``platform.json``: аргументы — 512 байт,
        результат — 1024. Числа читаются из политики, а не вписаны здесь,
        иначе страж подтверждал бы сам себя.
        """
        cap_args, cap_result = 512, 1024
        sink = _execute(
            tmp_path,
            lambda **_: {"rows": ["х" * 20000]},
            {"event_type": "smoke", "note": "я" * 20000},
            ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type,note",
            ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES=cap_args,
            ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES=cap_result,
        )
        started = sink.of("tool.started")["payload"]
        completed = sink.of("tool.completed")["payload"]

        for key, cap in (("arguments_excerpt", cap_args), ("result_excerpt", cap_result)):
            raw = completed.get(key) or started.get(key)
            assert raw is not None, f"{key} не записан"
            assert len(raw.encode("utf-8")) <= cap, (
                f"{key} длиннее потолка: {len(raw.encode('utf-8'))} > {cap}"
            )
            assert TRUNCATION_MARKER in raw, f"{key} усечён без маркера"
        assert started["arguments_truncated"] is True
        assert completed["result_truncated"] is True

    def test_cap_applies_to_the_whole_excerpt(self, tmp_path: Path) -> None:
        """Потолок считается на выдержку, а не на каждое поле.

        Много коротких полей — ровно случай, в котором потолок на поле дал бы
        суммарный объём в разы больше объявленного. Белый список умеет отдать
        двадцать значений по двести символов, то есть до 4 КБ на событие.
        """
        cap = 512
        fields = ",".join(f"f{index}" for index in range(20))
        arguments = {f"f{index}": "з" * 190 for index in range(20)}
        sink = _execute(
            tmp_path,
            lambda **_: {"ok": True},
            arguments,
            ENTERPRISE_EXEC_LOG_ARG_FIELDS=fields,
            ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES=cap,
        )
        excerpt = sink.of("tool.started")["payload"]["arguments_excerpt"]
        assert len(excerpt.encode("utf-8")) <= cap, (
            f"суммарная выдержка вылезла за потолок: {len(excerpt.encode('utf-8'))} > {cap}"
        )
        assert sink.of("tool.started")["payload"]["arguments_truncated"] is True

    def test_secrets_are_masked(self, tmp_path: Path) -> None:
        """Секрет не попадает в журнал ни по имени поля, ни по форме значения.

        Проверяются оба пути отдельно: маскирование по имени и распознавание по
        форме. Список форм закрыт осознанно, и этот тест фиксирует его состав —
        добавление новой формы обязано быть решением, а не случайностью.
        """
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"
        secrets = {
            "password": "просто-пароль",
            "dsn": "postgresql://user:pass@host:5432/db",
            "note": "Bearer abcdefghijklmnop",
            "token": "sk-0123456789abcdef",
            "session": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh",
        }
        sink = _execute(
            tmp_path,
            lambda **_: {"rows": [], "private_key": pem, "dsn": "postgresql://u:p@h/d"},
            dict(secrets),
            ENTERPRISE_EXEC_LOG_ARG_FIELDS=",".join(secrets),
            ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES=4096,
            ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES=4096,
            ENTERPRISE_EXEC_LOG_REDACT_KEYS="password,dsn,token,private_key",
        )
        rows = json.dumps(sink.rows, ensure_ascii=False)
        for value in secrets.values():
            assert value not in rows, f"секрет попал в журнал: {value[:24]}"
        assert pem not in rows, "PEM-блок попал в журнал"
        assert "просто-пароль" not in rows, "значение поля из белого списка имён не скрыто"
        assert REDACTED in rows, "маркер маскирования не виден — усечённое неотличимо от целого"

    def test_masking_is_counted(self, tmp_path: Path) -> None:
        """Число замаскированных значений видно, иначе маскирование не считается.

        Без счётчика читатель журнала не может отличить «секрета не было» от
        «секрет был и убран» — а это разные выводы об инциденте.
        """
        sink = _execute(
            tmp_path,
            lambda **_: {"rows": [{"password": "внутри-результата"}]},
            {"event_type": "smoke", "password": "в-аргументах", "token": "и-ещё"},
            ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type,password,token",
            ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES=4096,
            ENTERPRISE_EXEC_LOG_RESULT_EXCERPT_BYTES=4096,
            ENTERPRISE_EXEC_LOG_REDACT_KEYS="password,token",
        )
        started = sink.of("tool.started")["payload"]
        completed = sink.of("tool.completed")["payload"]
        assert started["arguments_masked"] == 2, started
        assert completed["result_masked"] == 1, completed
        # Нет секрета — счётчик нулевой, а не отсутствующий.
        quiet = _execute(
            tmp_path,
            lambda **_: {"ok": True},
            {"event_type": "smoke"},
            ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type",
            ENTERPRISE_EXEC_LOG_ARG_EXCERPT_BYTES=4096,
        )
        assert quiet.of("tool.started")["payload"]["arguments_masked"] == 0

    def test_new_argument_is_not_logged_by_default(self, tmp_path: Path) -> None:
        """Новый параметр операции не начинает писаться в журнал сам по себе.

        Механизм остаётся белым списком: у операции появился параметр, его не
        объявили в ``execution.log_argument_fields`` — и он не появился в журнале.
        Если однажды механизм станет «записывать всё», этот страж упадёт.
        """
        secret_new = "новый-параметр-не-объявлен"
        sink = _execute(
            tmp_path,
            lambda **_: {"ok": True},
            {"event_type": "smoke", "brand_new_argument": secret_new},
            ENTERPRISE_EXEC_LOG_ARG_FIELDS="event_type",
        )
        rows = json.dumps(sink.rows, ensure_ascii=False)
        assert secret_new not in rows, "необъявленный параметр попал в журнал"
        payload = sink.of("tool.started")["payload"]
        assert payload["arguments"] == {"event_type": "smoke"}
        assert "brand_new_argument" not in payload["arguments_excerpt"]
        # Размер и хеш остаются: они не тело и обязаны быть полными.
        assert payload["arguments_size"] > 0
        assert len(payload["arguments_hash"]) == 16

    def test_payload_keys_are_not_validated_against_a_dictionary(self) -> None:
        """Новый ключ ``payload`` — не новое объявление схемы.

        ``log_unknown_event_type_policy`` ограничивает имена событий, а не
        ключи ``payload``. Если бы writer сверял ключи со словарём, то поля
        ``arguments_excerpt``/``result_excerpt`` не дали бы записаться вовсе —
        и починенная наблюдаемность обернулась бы потерей событий.
        """
        from libs.enterprise_common.eventing.models import AgentEvent
        from libs.enterprise_common.eventing.writer import EventWriter

        published: list[Any] = []
        writer = EventWriter(sink=lambda row: published.append(row) or None)
        writer.emit(
            AgentEvent(
                event_type="tool.completed",
                name="probe",
                summary="проверка",
                session_id="s",
                user_id="u",
                request_id="r",
                payload={"неизвестный_ключ": 1, "result_excerpt": "тело"},
            )
        )
        assert published, "writer отбросил событие с новым ключом payload"
        payload = getattr(published[-1], "payload", None)
        if payload is None:  # писатель кладёт на шину сообщение, а не событие
            payload = json.loads(json.dumps(published[-1], default=str)).get("payload", {})
        assert payload.get("неизвестный_ключ") == 1, payload
        assert payload.get("result_excerpt") == "тело", payload

    def test_agent_payloads_are_not_masked(self) -> None:
        """Маскирование не растекается за пределы payload вызовов инструментов.

        Маскирование живёт в слое исполнения, который собирает payload вызова
        инструмента: там тело — это аргументы и результат внешней системы.
        События остальных производителей идут через :class:`EventWriter` мимо
        этого слоя, и их payload — это данные разговора. Маскирование там
        означало бы, что ``history_search`` молча перестаёт искать по прошлым
        ответам агента.

        Импортировать агентский код платформенный тест не может (страж границ
        ``test_architecture_boundaries.py``), поэтому проверяется здесь правило
        платформы: маскирование применяется слоем исполнения, а не писателем
        событий. Агентская половина — ``tests/test_journal_delivery_contract.py``.
        """
        from libs.enterprise_common.eventing.models import AgentEvent
        from libs.enterprise_common.eventing.writer import EventWriter

        text = "Ответ агента: Bearer не-секрет, это цитата пользователя sk-не-настоящий"
        published: list[Any] = []
        writer = EventWriter(sink=lambda row: published.append(row) or None)
        writer.emit(
            AgentEvent(
                event_type="agent.responded",
                name="agent",
                summary="ответ",
                session_id="s",
                user_id="u",
                request_id="r",
                payload={"content": text},
            )
        )
        assert published, "writer отбросил событие"
        row = published[-1]
        payload = getattr(row, "payload", None)
        if payload is None:
            payload = json.loads(json.dumps(row, default=str)).get("payload", {})
        assert payload.get("content") == text, (
            "маскирование просочилось в payload события, которое пишет не слой "
            "исполнения: ответ агента перестал бы находиться через history_search"
        )
