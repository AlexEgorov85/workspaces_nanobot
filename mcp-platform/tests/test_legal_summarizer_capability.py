"""Capability ``legal_summarizer``: операция ``query_operation`` и её границы.

Capability читает состояние ранее выполненной суммаризации **с диска**: манифест
лежит в ``<cache_root>/operations/<op>/``.
Поэтому успешный путь проверяется по-настоящему — настоящим манифестом в
``tmp_path``, без БД, без модели и без файла снимка. Всё, что нужно домену для
follow-up'а, это один валидный JSON на диске.

Что здесь защищается (каждый пункт — реальная асикция, а не «зелёная галочка»):

* **регистрация** — операция находится загрузчиком, её дескриптор и схема,
  построенная из сигнатуры обработчика, корректны;
* **успех** — реальный вызов по проводу MCP отдаёт ожидаемую структуру
  результата для нескольких ``field``;
* **доменные отказы** — все три причины недоступности манифеста (нет файла,
  битый JSON, чужой формат) дают доменный отказ с тем кодом конверта, который
  заявлен за этим состоянием, а не необработанное исключение и не дефолтный
  ``internal``;
* **сборка без сервиса** — операция без сервиса в контейнере не собирается
  вовсе, а не падает на первом обращении;
* **идентичность** — вызов без ``params._meta`` отклоняется конвейером с
  ``identity_missing`` ДО входа в домен (доказано тем, что при живом манифесте
  вместо отказа пришёл бы успешный ответ), а корректный ``_meta`` извлекается в
  контекст вызова и доезжает в метаданные ответа.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.container import ToolContainer  # noqa: E402
from libs.enterprise_common.errors import EnterpriseError  # noqa: E402
from libs.enterprise_common.registry import ToolDefinition, build_input_schema  # noqa: E402
from servers.enterprise.capabilities.legal_summarizer.service.main import (  # noqa: E402
    _ERROR_CODES,
    LegalSummarizerService,
)
from servers.enterprise.capabilities.legal_summarizer.tools import (  # noqa: E402
    query_operation as query_tool,
)

CAPABILITY_DIR = (
    PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "legal_summarizer"
)
TOOL_FILE = CAPABILITY_DIR / "tools" / "query_operation.py"

#: Раскладка состояния операции внутри ``cache_root``. Именно её ждёт
#: ``libs.legal_summarizer.cache.manifest.manifest_root``; путь собран вручную,
#: чтобы тест не зависел от внутреннего устройства домена и падал с понятным
#: сообщением, если раскладка изменится.
MANIFEST_SUBPATH = Path("operations")


@pytest.fixture(autouse=True)
def _isolate_domain_config():
    """Вернуть глобальную конфигурацию домена после каждого теста.

    Конструктор сервиса с объявленным ``cache_root`` вызывает
    ``_apply_domain_config`` и подменяет конфигурацию всего домена на время
    процесса. Без восстановления тест, указавший корень в ``tmp_path``, оставил
    бы этот ``tmp_path`` библиотечным тестам, идущим после него, — утечка
    состояния через общий модуль.
    """
    from libs.legal_summarizer.llm import config as domain_config

    saved = domain_config.current()
    yield
    domain_config.configure(saved)


def _service(cache_root: Path | None) -> LegalSummarizerService:
    """Сервис с корнем состояния в ``cache_root`` (явный ``config=``)."""
    if cache_root is None:
        return LegalSummarizerService(config={})
    return LegalSummarizerService(
        config={"legal_summarizer": {"cache_root": str(cache_root)}}
    )


def _write_manifest(cache_root: Path, operation_id: str, **overrides: Any) -> Path:
    """Положить валидный манифест v2 и вернуть его путь.

    ``version=2`` обязателен: домен читает только второй формат, и подмена
    формата — это уже другой тест (см. отказ по версии).
    """
    payload: dict[str, Any] = {
        "version": 2,
        "operation_id": operation_id,
        "status": "completed",
        "chars_in": 12345,
        "chunks_total": 7,
        "context_batches_total": 3,
        "article_count": 42,
        "sections": {
            "ROOT": {"section_path": "", "heading": None, "block_count": 0},
            "s1": {"section_path": "Глава 1", "heading": "Глава 1", "block_count": 5},
        },
        "batches_done": ["b1"],
        "batches_failed": [],
    }
    payload.update(overrides)
    path = cache_root / MANIFEST_SUBPATH / operation_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _domain_error_type(cache_root: Path, operation_id: str) -> str:
    """Доменный ``error_type`` из конверта, без участия сервиса.

    Вызывается ДО проверки кода конверта и специально: показывает, какое
    имя домен реально кладёт в конверт для данного состояния на диске. Иначе
    тест закреплял бы только код сервиса и молчал бы, если переименуют
    доменную сторону.
    """
    from libs.legal_summarizer.cli_query import LegalQueryError, query_operation

    with pytest.raises(LegalQueryError) as excinfo:
        query_operation(operation_id, "stats", workspace_root=cache_root)
    return str(excinfo.value.payload["error_type"])


def _registry(service: LegalSummarizerService) -> Any:
    """Реестр ровно из одной операции capability (как это делает загрузчик)."""
    from libs.enterprise_common.loader import load_definition
    from libs.enterprise_common.registry import ToolRegistry

    return ToolRegistry(
        [
            load_definition(
                TOOL_FILE, ToolContainer(services={"legal_summarizer": service}), PLATFORM_ROOT
            )
        ]
    )


def _wire(transport: Any, name: str, arguments: dict[str, Any], meta: Any = None) -> Any:
    """Один вызов по протоколу MCP через in-memory транспорт."""
    import anyio
    from conftest import call_tool

    return anyio.run(call_tool, transport, name, arguments, meta)


class TestOperationContract:
    """Операция зарегистрирована, а её дескриптор и схема корректны."""

    def test_definition_metadata(self, tmp_path: Path) -> None:
        definition = query_tool.create_tool(
            ToolContainer(services={"legal_summarizer": _service(tmp_path)})
        )
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "query_operation"
        assert definition.category == "legal_summarizer"
        assert definition.description.strip()
        # Не «runtime-only»: операция объявлена модели в config.json, и метка
        # внутренней операции на ней врала. Согласованность объявления и метки
        # проверяет страж агента tests/test_mcp_operation_audience.py.
        assert "runtime-only" not in definition.tags
        assert definition.permissions == ("legal_summarizer:query_operation",)

    def test_schema_is_built_from_handler_signature(self, tmp_path: Path) -> None:
        """Схема обязана требовать ``operation_id`` и не требовать опциональных.

        ``operation_id`` без значения бессмысленна (вопрос — к конкретной
        операции), а ``field``/``max_chunk_summary_chars`` имеют дефолты.
        """
        definition = query_tool.create_tool(
            ToolContainer(services={"legal_summarizer": _service(tmp_path)})
        )
        schema = build_input_schema(definition.handler)
        assert schema["required"] == ["operation_id"]
        assert schema["properties"]["operation_id"] == {"type": "string"}
        assert schema["properties"]["field"] == {"type": "string"}
        assert schema["properties"]["max_chunk_summary_chars"] == {"type": "integer"}

    def test_operation_file_is_discovered(self) -> None:
        from libs.enterprise_common.loader import discover_tool_files

        names = {p.name for p in discover_tool_files(CAPABILITY_DIR.parent)}
        assert "query_operation.py" in names

    def test_missing_service_is_refused_at_assembly(self) -> None:
        """Без сервиса операция не собирается — полусобранный сервер хуже пустого.

        Отказ приходит на загрузке, а не первым вызовом: иначе сервер поднялся
        бы, опубликовал операцию и отвечал бы отказом уже в проде.
        """
        from libs.enterprise_common.loader import load_definition
        from libs.enterprise_common.registry import ToolLoadError

        with pytest.raises(ToolLoadError) as excinfo:
            load_definition(TOOL_FILE, ToolContainer(), PLATFORM_ROOT)
        assert "legal_summarizer" in str(excinfo.value)


class TestServiceQuery:
    """Сервис поверх настоящего манифеста: успех и доменные отказы."""

    def test_stats_returns_operation_metrics(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, "op1")
        result = _service(tmp_path).query_operation(operation_id="op1", field="stats")
        assert result["status"] == "ok"
        assert result["field"] == "stats"
        assert result["article_count"] == 42
        assert result["chunks_total"] == 7
        # sections_total не считает служебный ROOT.
        assert result["sections_total"] == 1

    @pytest.mark.parametrize(
        ("field", "check"),
        [
            ("articles", lambda b: b["article_count"] == 42),
            # ``sections`` отдаёт плоский список, отсортированный по
            # ``section_path``, и служебный ROOT в нём остаётся: фильтрует его
            # только счётчик ``sections_total`` в stats. Закрепляю фактическое
            # поведение домона (ROOT идёт первым, у него пустой путь).
            (
                "sections",
                lambda b: [s["section_id"] for s in b["sections"]] == ["ROOT", "s1"],
            ),
            ("tree", lambda b: any(s["heading"] == "Глава 1" for s in b["sections"])),
            ("all", lambda b: b["manifest"]["operation_id"] == "op1"),
        ],
        ids=["articles", "sections", "tree", "all"],
    )
    def test_each_field_returns_its_own_shape(
        self, tmp_path: Path, field: str, check: Any
    ) -> None:
        """Каждое допустимое поле отвечает своей формой, а не общим JSON."""
        _write_manifest(tmp_path, "op1")
        body = _service(tmp_path).query_operation(operation_id="op1", field=field)
        assert body["status"] == "ok"
        assert body["field"] == field
        assert check(body), body

    def test_missing_manifest_is_not_found(self, tmp_path: Path) -> None:
        """Нет манифеста → ``not_found``, а не «внутренняя ошибка платформы».

        Это различает «проверь имя и спроси снова» (повтор с тем же именем
        бесполезен) от сбоя сервера. Проверяется настоящий доменный путь:
        файла действительно нет.
        """
        assert _domain_error_type(tmp_path, "missing") == "manifest_not_found"
        with pytest.raises(EnterpriseError) as excinfo:
            _service(tmp_path).query_operation(operation_id="missing", field="stats")
        assert excinfo.value.code == "not_found"

    def test_corrupted_manifest_is_internal(self, tmp_path: Path) -> None:
        """Битый JSON на диске — состояние на диске повреждено, а не «не найдено».

        ``internal`` здесь осознанный и таким останется: повреждённый файл
        чинит владелец состояния, ни «проверь имя», ни «повтори» модели не
        помогают. Закрепляется вместе с доменным именем, чтобы правка таблицы
        не «улучшила» этот код молча.
        """
        path = tmp_path / MANIFEST_SUBPATH / "op1" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{это не json", encoding="utf-8")
        assert _domain_error_type(tmp_path, "op1") == "manifest_corrupted"
        with pytest.raises(EnterpriseError) as excinfo:
            _service(tmp_path).query_operation(operation_id="op1", field="stats")
        assert excinfo.value.code == "internal"

    def test_unsupported_version_is_upstream_unavailable(self, tmp_path: Path) -> None:
        """Манифест чужого формата → ``upstream_unavailable``.

        Код конверта обязан быть ровно таким, как заявлен в таблице
        ``_ERROR_CODES``, а таблица — по доменным именам из
        ``cli_query._MANIFEST_ERROR_TYPES``. Домен отдаёт
        ``manifest_unsupported_version``; с ключом в обратном порядке слов
        словарь молча промахивался, и случай уходил с дефолтным
        ``internal`` - агент получал «сбой платформы» вместо «состояние
        записано другим форматом».
        """
        _write_manifest(tmp_path, "op1", version=1)
        assert (
            _domain_error_type(tmp_path, "op1") == "manifest_unsupported_version"
        )
        with pytest.raises(EnterpriseError) as excinfo:
            _service(tmp_path).query_operation(operation_id="op1", field="stats")
        assert excinfo.value.code == "upstream_unavailable"
        # Сообщение остаётся доменным и называет наблюдаемую версию.
        assert "version" in str(excinfo.value).lower()


class TestErrorCodeTable:
    """Таблица перевода обязана совпадать с доменом, а не «почти совпадать»."""

    def test_table_keys_are_exactly_the_domain_error_types(self) -> None:
        """Ключи ``_ERROR_CODES`` = все ``error_type``, которые присылает домен.

        Сверка идёт с обоими источниками: лишний ключ означал бы код, который
        домен никогда не пришлёт, а недостающий - молчаливый откат на
        ``internal`` (именно этим был баг с ``unsupported_manifest_version``).
        Проверка без диска и без моков: чистое следствие двух объявлений.

        Сверяется с ``DOMAIN_ERROR_TYPES``, а не с ``_MANIFEST_ERROR_TYPES``:
        манифестная половина - не весь домен. Отказ по аргументу
        (``invalid_field``) тоже обязан иметь код, иначе модель получала бы
        «виновата платформа» вместо «повтори с одним из шести».
        """
        from libs.legal_summarizer.cli_query import DOMAIN_ERROR_TYPES

        assert set(_ERROR_CODES) == set(DOMAIN_ERROR_TYPES)


class TestWire:
    """Операция работает по протоколу MCP: успех, отказ и идентичность."""

    def test_wire_call_returns_result(self, tmp_path: Path) -> None:
        """Успешный вызов по проводу отдаёт разобранный JSON с метаданными."""
        from conftest import make_layer
        from libs.enterprise_common.loader import build_server

        _write_manifest(tmp_path, "op1")
        service = _service(tmp_path)
        transport = build_server(
            _registry(service), name="enterprise-mcp", pipeline=make_layer(tmp_path).pipeline
        )
        result = _wire(transport, "query_operation", {"operation_id": "op1"})
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["status"] == "ok"
        assert body["article_count"] == 42

    def test_wire_domain_error_carries_envelope_code(self, tmp_path: Path) -> None:
        """Доменный отказ доходит конвертом с кодом, без трассировки.

        Агент читает код, а не разбирает текст; ``Traceback`` в ответе означал
        бы, что отказ не нормализован.
        """
        from conftest import make_layer
        from libs.enterprise_common.loader import build_server

        # Манифест намеренно отсутствует — домен вернёт not_found.
        service = _service(tmp_path)
        transport = build_server(
            _registry(service), name="enterprise-mcp", pipeline=make_layer(tmp_path).pipeline
        )
        result = _wire(transport, "query_operation", {"operation_id": "absent"})
        assert result.isError is True
        assert json.loads(result.content[0].text)["error"]["code"] == "not_found"
        assert "Traceback" not in result.content[0].text

    def test_wire_unknown_field_is_refused_and_manifest_is_not_dumped(
        self, tmp_path: Path
    ) -> None:
        """Поле не из перечня — отказ, даже когда манифест читается.

        Манифест здесь заведомо валиден. Это и есть условие, при котором
        поломка была незаметна: на несуществующем ``operation_id`` домен и так
        отказывал, и отказ по полю не отличить от отказа по состоянию. С
        живым манифестом старое поведение отдавало ``status = "ok"`` и
        ``manifest`` целиком, то есть опечатка в имени поля стоила модели
        целого документа и не давала ни отказа, ни намёка на ошибку.
        """
        from conftest import make_layer
        from libs.enterprise_common.loader import build_server

        _write_manifest(tmp_path, "op1")
        transport = build_server(
            _registry(_service(tmp_path)),
            name="enterprise-mcp",
            pipeline=make_layer(tmp_path).pipeline,
        )
        result = _wire(
            transport, "query_operation", {"operation_id": "op1", "field": "section"}
        )
        assert result.isError is True
        text = result.content[0].text
        assert json.loads(text)["error"]["code"] == "invalid_params"
        # Перечень обязателен: иначе модель не знает, что повторить.
        for field in ("stats", "articles", "chunks", "sections", "tree", "all"):
            assert field in text, f"в отказе не назван допустимый перечень: {field}"
        # Главное: содержимое манифеста утекать не должно.
        assert "chunk_states" not in text, "отказ по полю выдал manifest целиком"
        assert "Traceback" not in text

    def test_call_without_meta_is_refused_before_domain(self, tmp_path: Path) -> None:
        """Без ``params._meta`` вызов не доходит до домена.

        Доказательство «домен не запускался» — само присутствие валидного
        манифеста: будь вызов дошёл до сервиса, в ответ пришёл бы успешный
        ``status=ok``. Приходит ``identity_missing`` — значит конвейер отклонил
        вызов на шаге идентичности, до ``_call_domain``.
        """
        from conftest import make_layer
        from libs.enterprise_common.loader import build_server

        _write_manifest(tmp_path, "op1")
        service = _service(tmp_path)
        transport = build_server(
            _registry(service), name="enterprise-mcp", pipeline=make_layer(tmp_path).pipeline
        )
        result = _wire(transport, "query_operation", {"operation_id": "op1"}, {})
        assert result.isError is True
        body = json.loads(result.content[0].text)
        assert body["error"]["code"] == "identity_missing"
        assert "article_count" not in result.content[0].text, (
            "домен не должен запускаться без идентичности"
        )

    def test_identity_is_extracted_from_call_context(self, tmp_path: Path) -> None:
        """Корректный ``_meta`` извлекается и доезжает в метаданные ответа.

        Проверяется конкретный ``request_id``: он пришёл в ``params._meta``,
        конвейер его разобрал, и то же значение появилось в блоке ``_execution``
        ответа. Это и есть «идентичность операции корректно извлекается из
        контекста вызова» — наблюдаемое, а не декларативное.
        """
        from conftest import call_meta, make_layer
        from libs.enterprise_common.loader import build_server

        _write_manifest(tmp_path, "op1")
        service = _service(tmp_path)
        transport = build_server(
            _registry(service), name="enterprise-mcp", pipeline=make_layer(tmp_path).pipeline
        )
        result = _wire(
            transport,
            "query_operation",
            {"operation_id": "op1"},
            call_meta(request_id="req-legal-42", session_id="sess-legal-7"),
        )
        assert result.isError is False
        execution = json.loads(result.content[0].text)["_execution"]
        assert execution["request_id"] == "req-legal-42"
        assert execution["tool"] == "query_operation"
        assert execution["capability"] == "legal_summarizer"
