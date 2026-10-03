"""Классы работы доходят до пула операторских процессов.

Дефект, который ловит этот файл
--------------------------------

``load_snapshot`` и ``build_index`` — не части сервера, а отдельные
процессы: каждый поднимает **свой** пул и обязан настроить его сам, иначе
не выполнил бы ни одной работы. Пул настраивается двумя секциями — ``pool``
(размер и резерв) и ``job_classes`` (чем этот размер разрешают ждать) — и
применяет их одна функция, ``servers.enterprise.server::_apply_pool_settings``.

Пока вторая секция не дошла до пула процесса, его работа не ограничена
ничем: загрузка снимка идёт пулом потоков и встаёт в очередь без предела.
Это не отказ, а худшее молчание — ``platform.json`` прочитан, проверен
стражем и выглядит рабочим, а на процессе не действует.

Что здесь проверяется по существу:

* после подготовки пула точкой входа ``job_classes_configured()`` истинно, а
  ``statement_timeout_ms`` отдаёт **объявленное** значение, а не дефолт;
* состояние зафиксировано в момент ``start()``: конфигурация обязана
  прийти до старта, иначе первые воркеры поднимутся без пределов, а
  отчёт об этом не скажет ничего;
* неполная секция останавливает процесс на старте, называя ключ, и не
  достраивает его значением по умолчанию.

Значения берутся из ``platform.json`` через реестр настроек и сравниваются
с тем, что применил пул. Выписывать их здесь нельзя: проверка сравнивала бы
объявление с его же копией в тесте и проходила бы на любом значении.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from libs.enterprise_common.settings import (  # noqa: E402
    Settings,
    job_class_setting_names,
)
from libs.enterprise_data.audience import (  # noqa: E402
    ALL_AUDIENCES,
    JOB_AUDIENCE_RUNTIME,
)
from libs.enterprise_data.db import _JOB_CLASS_SPEC  # noqa: E402
from servers.enterprise import build_index, load_snapshot  # noqa: E402

#: Подстановки ``platform.json``: без них разбор файла падает на ``db.dsn``
#: в тесте, который проверяет вообще другое. Настоящие секреты живут в
#: ``mcp-platform/.secrets.env`` и в тесты не попадают; набор тот же, что
#: задаёт ``conftest`` на сессию, и расти он обязан вместе с файлом.
DUMMY_SECRETS: dict[str, str] = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
    "EMBED_TOKEN": "test",
}

#: Ключ контракта класса, на котором проверяется доезд до пула. Потолок
#: времени выбран потому, что у него есть читатель снаружи пула
#: (``statement_timeout_ms``); остальные параметры читает сам пул, когда
#: воркер берёт работу. Это **имя** ключа, а не значение: значения секции
#: объявляет ``platform.json``, и копия их в тесте обесценила бы проверку.
TIMEOUT_KEY = "statement_timeout_ms"


def _settings(**env: Any) -> Settings:
    """Реестр настроек по настоящему ``platform.json``.

    Именно настоящий файл, а не подставные значения: проверка отвечает на
    вопрос «объявленное доезжает до процесса», и подставной реестр отвечал
    бы на другой — «механизм доставки работает». Оба ответа нужны, но
    объявление проверяется только здесь.
    """
    return Settings(env={**DUMMY_SECRETS, **env}, secrets={})


def _declared(settings: Settings, audience: str, key: str) -> Any:
    """Значение, объявленное для класса, по имени настройки из реестра.

    Имя берётся у реестра (``job_class_setting_names``), порядок ключей — у
    контракта класса (``db._JOB_CLASS_SPEC``). Оба списка названы здесь
    поимённо, а не выписаны: разошёлся бы любой из них, и упало бы не на
    домене, а на собственном дублировании.
    """
    names = job_class_setting_names(audience)
    keys = tuple(_JOB_CLASS_SPEC)
    assert len(names) == len(keys), (audience, names, keys)
    return settings.get(names[keys.index(key)])


@pytest.fixture(autouse=True)
def _empty_pool_config():
    """Пустые настройки пула на время теста.

    Сбрасываются, а не только восстанавливаются: «секция не применена» —
    утверждение, и оно верно лишь тогда, когда до теста пул ничего не знал.
    Иначе результат зависел бы от того, какой тест шёл раньше.
    """
    from libs.enterprise_data import db as data_db

    saved_pool = dict(data_db._pool_cfg)
    saved_classes = {
        audience: dict(values) for audience, values in data_db._job_class_cfg.items()
    }
    data_db._pool_cfg = {}
    data_db._job_class_cfg = {}
    try:
        yield
    finally:
        data_db._pool_cfg = saved_pool
        data_db._job_class_cfg = saved_classes


def _watch_pool(monkeypatch) -> dict:
    """Зафиксировать состояние пула в тот момент, когда он поднимается.

    Состояние после прогона ничего не говорит о порядке: секция, применённая
    после ``start()``, выглядела бы в отчёте так же, как применённая до, а
    воркеры между этими двумя моментами работали бы без пределов.
    """
    from libs.enterprise_data import db as pool

    seen: dict = {}

    def start() -> None:
        seen["started"] = True
        seen["configured_at_start"] = pool.job_classes_configured()
        seen["timeout_at_start"] = pool.statement_timeout_ms(JOB_AUDIENCE_RUNTIME)

    monkeypatch.setattr(pool, "start", start)
    monkeypatch.setattr(pool, "shutdown", lambda: None)
    return seen


def _assert_classes_applied(seen: dict, settings: Settings) -> None:
    """Секция классов объявлена, полна и применена **до** старта пула."""
    from libs.enterprise_data import db as pool

    expected = _declared(settings, JOB_AUDIENCE_RUNTIME, TIMEOUT_KEY)
    assert seen.get("started") is True, "пул не поднимался — точка входа не дошла"
    assert seen.get("configured_at_start") is True, (
        "в момент start() секция job_classes ещё не была применена: работа "
        "процесса пошла бы без потолка времени, предела ожидания и запрета "
        "на аренду"
    )
    assert seen.get("timeout_at_start") == expected, (
        f"потолок времени не тот: пул получил {seen.get('timeout_at_start')!r}, "
        f"объявлено {expected!r}"
    )
    assert set(pool.job_class_config()) == set(ALL_AUDIENCES), (
        f"применён не весь набор аудиторий: {sorted(pool.job_class_config())}"
    )


class _WithoutOneClass:
    """Реестр, у которого одно объявление секции классов недоступно.

    Не подмена значения, а именно его отсутствие: неполная секция обязана
    останавливать процесс на старте, называя ключ. Подставить вместо
    отсутствующего значения дефолт — значит объявить, что процесс
    настроен, и ограничить его чужими пределами.
    """

    def __init__(self, settings: Settings, name: str) -> None:
        self._settings = settings
        self._name = name

    def get(self, name: str) -> Any:
        return None if name == self._name else self._settings.get(name)

    def source(self, name: str) -> str:
        return self._settings.source(name)


def _broken_registry(settings: Settings) -> Settings:
    """Реестр без потолка времени у аудитории рантайма."""
    names = job_class_setting_names(JOB_AUDIENCE_RUNTIME)
    keys = tuple(_JOB_CLASS_SPEC)
    return _WithoutOneClass(settings, names[keys.index(TIMEOUT_KEY)])


class _FakeStore:
    """Хранилище снимка, которое только и нужно этому тесту.

    Загрузка снимка для проверки не требуется: важно, что пул процесса
    настроен, а он настраивается до того, как откроется файл.
    """

    def is_ready(self) -> bool:
        return True

    def reset(self) -> list[str]:
        return []

    def close(self) -> None:
        return None


def _patch_load_snapshot(monkeypatch) -> None:
    """Убрать PostgreSQL и снимок: точка входа должна дойти до отчёта."""
    from libs.enterprise_data.loader import SnapshotLoadResult, SnapshotLoadService

    class _FakeLoader:
        def __init__(self, **kwargs: Any) -> None:
            self._kwargs = kwargs

        def load(self) -> SnapshotLoadResult:
            return SnapshotLoadResult(loaded_tables=1, total_tables=1, rows_total=0)

    monkeypatch.setattr(
        "libs.enterprise_data.snapshot.open_snapshot_store",
        lambda *args, **kwargs: _FakeStore(),
    )
    monkeypatch.setattr(
        "libs.enterprise_data.loader.SnapshotLoadService", _FakeLoader
    )


def _patch_build_index(monkeypatch) -> None:
    """Убрать провайдера и PostgreSQL: до отчёта об индексах тут не дойти.

    Сборка индексов — пакетная работа на часы; выполнять её ради проверки
    настройки пула нельзя, а вот дойти до ``start()`` с объявленной секцией
    обязательно.
    """

    class _FakeBuilder:
        def __init__(self, **kwargs: Any) -> None:
            self._kwargs = kwargs

        def build(self, index: str | None = None) -> list:
            return []

    monkeypatch.setattr(build_index, "_embedder", lambda settings: (lambda text: []))
    monkeypatch.setattr("libs.vectors.builder.VectorBuilder", _FakeBuilder)


class TestLoadSnapshot:
    """Загрузка снимка — самая долгая работа, и её пул настраивается здесь."""

    def test_job_classes_reach_the_process_pool(self, tmp_path, monkeypatch) -> None:
        settings = _settings(ENTERPRISE_SNAPSHOT_PATH=str(tmp_path / "cache.duckdb"))
        seen = _watch_pool(monkeypatch)
        _patch_load_snapshot(monkeypatch)

        code = load_snapshot.main([], settings=settings)

        assert code == 0
        _assert_classes_applied(seen, settings)

    def test_incomplete_section_stops_the_process(self, tmp_path, monkeypatch) -> None:
        settings = _settings(ENTERPRISE_SNAPSHOT_PATH=str(tmp_path / "cache.duckdb"))
        seen = _watch_pool(monkeypatch)
        _patch_load_snapshot(monkeypatch)

        with pytest.raises(InfrastructureError) as refusal:
            load_snapshot.main([], settings=_broken_registry(settings))

        assert TIMEOUT_KEY in str(refusal.value), (
            f"отказ обязан называть ключ: {refusal.value}"
        )
        assert seen.get("started") is None, (
            "пул поднялся на неполной секции: процесс объявил бы себя "
            "настроенным и пошёл бы работать без пределов"
        )


class TestBuildIndex:
    """Сборка индексов идёт в тот же пул и теряет те же пределы."""

    def test_job_classes_reach_the_process_pool(self, monkeypatch) -> None:
        settings = _settings()
        seen = _watch_pool(monkeypatch)
        _patch_build_index(monkeypatch)

        code = build_index.main([], settings=settings)

        assert code == 0
        _assert_classes_applied(seen, settings)

    def test_incomplete_section_stops_the_process(self, monkeypatch) -> None:
        settings = _settings()
        seen = _watch_pool(monkeypatch)
        _patch_build_index(monkeypatch)

        with pytest.raises(InfrastructureError) as refusal:
            build_index.main([], settings=_broken_registry(settings))

        assert TIMEOUT_KEY in str(refusal.value), (
            f"отказ обязан называть ключ: {refusal.value}"
        )
        assert seen.get("started") is None, (
            "пул поднялся на неполной секции: процесс объявил бы себя "
            "настроенным и пошёл бы работать без пределов"
        )
