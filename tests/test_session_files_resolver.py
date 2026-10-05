"""``SessionFileResolver``: один корень, один вызов, отказ вместо запасного пути.

Набор закрывает пять утверждений фазы 2 openspec-предложения
``2026-10-03-session-files``:

* путь сессии совпадает с тем, что вернула операция, и раскладку агент не
  пересобирает;
* второй вызов в ту же сессию в клиент не идёт;
* недоступная или ответившая ошибкой платформа даёт отказ с названной причиной,
  а не «пишем куда-нибудь ещё»;
* без платформы корень объявляет агент, и дефолт используется только пока
  объявления в ``config.json`` нет (задача 2.2);
* резолвер создаёт composition root и доступен плагинам-хукам, которые
  поднимаются с единственным аргументом ``workspace_dir``.

Проверка «ровно один вызов» сделана через подменённый ``call`` настоящего
``EnterpriseMcpClient``, а не через заглушку того же класса: иначе тест
подтверждал бы саму заглушку, а не контракт вызова.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lib.services import session_files as session_files_module
from lib.services.enterprise_mcp_client import (
    CallIdentity,
    EnterpriseMcpClient,
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)
from lib.services.session_files import (
    DEFAULT_ROOT_PARTS,
    FILES_SUBDIR,
    SESSION_FILES_OPERATION,
    SessionFileResolver,
    SessionFilesUnavailable,
    current_session_file_resolver,
    install_session_file_resolver,
)


class FakePlatformClient:
    """Клиент платформы, который запоминает, как его звали."""

    def __init__(self, answer: Any = None, error: BaseException | None = None) -> None:
        self._answer = answer
        self._error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def session_files(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._answer


def _answer(root: Path, *, files_subdir: str = FILES_SUBDIR) -> dict[str, Any]:
    """Ответ операции ``session_files``, как его отдаёт платформа."""
    directory = root / "session_42"
    return {
        "session_id": "postgres:42",
        "root": str(directory),
        "files_dir": str(directory / files_subdir),
        "layout": (FILES_SUBDIR,),
        "created": True,
    }


@pytest.fixture(autouse=True)
def _restore_published_resolver():
    """Публикация резолвера — состояние процесса; тесты её не оставляют."""
    saved = current_session_file_resolver()
    yield
    install_session_file_resolver(saved)


class TestPlatformBacked:
    async def test_paths_are_exactly_the_operation_answer(self, tmp_path: Path) -> None:
        """Путь сессии — ответ операции, а не пересобранный агентом.

        Имя подкаталога берётся из ответа, а не из константы агента: раскладку
        объявляет платформа, и подмена ``files`` на что-то другое обязана
        проходить через резолвер без правок кода.
        """
        answer = _answer(tmp_path, files_subdir="agent-files")
        client = FakePlatformClient(answer)
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        assert await resolver.session_dir("postgres:42") == Path(answer["root"])
        assert await resolver.files_dir("postgres:42") == Path(answer["files_dir"])
        assert await resolver.ensure("postgres:42") == Path(answer["root"])
        assert resolver.has_platform is True

    async def test_operation_carries_no_root_but_is_signed_by_the_gateway(
        self, tmp_path: Path
    ) -> None:
        """Корень не передаётся, личность — да, и она служебная.

        Корень объявлен у платформы: если резолвер начнёт его слать, у
        платформы появится второе объявление корня.

        Личность же передаётся. Прежний страж утверждал обратное — «личность
        собирает сам клиент из оборота» — и это было неверно: каталог
        спрашивает канал на пути разбора ВХОДЯЩЕГО сообщения, оборота ещё нет,
        личность оборота пуста, вызов уходил без params._meta, и платформа
        отвечала identity_missing на каждом входящем сообщении.

        session_id — настоящий: вызов адресован именно этой сессии.
        user_id — шлюз, потому что отправителя в этой точке ещё не существует.
        """
        client = FakePlatformClient(_answer(tmp_path))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        await resolver.files_dir("postgres:42")

        assert len(client.calls) == 1
        args, kwargs = client.calls[0]
        assert args == (), "корень операции передавать нельзя: он объявлен у платформы"
        identity = kwargs["identity"]
        assert identity.session_id == "postgres:42"
        assert identity.user_id == "gateway"

    async def test_signed_call_never_binds_a_question(
        self, tmp_path: Path
    ) -> None:
        """Служебный вызов не прикидывается вопросом.

        ``request_id`` не подставляется: вопроса не было, и связывать каталог
        с чужим вопросом в журнале платформы нельзя. Клиент доставит свой
        ``request_id`` сам — это отдельный механизм, к этому вызову отношения
        не имеющий.
        """
        client = FakePlatformClient(_answer(tmp_path))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        await resolver.files_dir("postgres:42")

        _args, kwargs = client.calls[0]
        identity = kwargs["identity"]
        assert not getattr(identity, "request_id", None), (
            "служебный вызов каталога не должен выдавать себя за вопрос"
        )

    async def test_second_call_does_not_reach_the_client(self, tmp_path: Path) -> None:
        """Один вызов на сессию: кэш, а не «как получится»."""
        client = FakePlatformClient(_answer(tmp_path))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        first = await resolver.files_dir("postgres:42")
        second = await resolver.files_dir("postgres:42")
        third = await resolver.session_dir("postgres:42")

        assert first == second
        assert third == Path(_answer(tmp_path)["root"])
        assert len(client.calls) == 1

    async def test_new_session_calls_again_and_sees_new_root(self, tmp_path: Path) -> None:
        """Смена объявления видна следующей сессии, а не закэшированной.

        Резолвер не сверяет корень при повторном обращении: единственный способ
        узнать его — снова спросить платформу, то есть ровно тот вызов, ради
        устранения которого нужен кэш. Поэтому кэш привязан к ``session_key``,
        и новая сессия видит тот корень, какой объявлен сейчас.
        """
        client = FakePlatformClient(_answer(tmp_path / "first"))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)
        cached = await resolver.files_dir("postgres:42")

        client._answer = _answer(tmp_path / "second")
        fresh = await resolver.files_dir("postgres:77")

        assert fresh == Path(client._answer["files_dir"])
        assert cached == Path(_answer(tmp_path / "first")["files_dir"])
        assert len(client.calls) == 2

    async def test_unavailable_platform_refuses_without_fallback(self, tmp_path: Path) -> None:
        """Платформа объявлена, но не отвечает — отказ, а не запасной каталог.

        Откат на корень по умолчанию вернул бы вторую папку сессии, и по
        содержимому нельзя было бы понять, какая из них своя.
        """
        workspace = tmp_path / "workspace"
        client = FakePlatformClient(error=EnterpriseMcpUnavailable("сервер не поднялся"))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=workspace)

        with pytest.raises(SessionFilesUnavailable) as excinfo:
            await resolver.files_dir("postgres:42")

        assert "сервер не поднялся" in str(excinfo.value)
        assert not (workspace / DEFAULT_ROOT_PARTS[0] / DEFAULT_ROOT_PARTS[1]).exists()
        assert not list(tmp_path.glob("**/session_42"))

    async def test_domain_error_refuses_with_named_reason(self, tmp_path: Path) -> None:
        """Доменный отказ платформы доходит до вызывающего с причиной.

        В переходном режиме общеплатформенная проверка ``identity_missing``
        выключена, но отказ самой операции обязан оставаться отказом: подмена
        его «успешным» ответом скрыла бы, что каталог не получен.
        """
        client = FakePlatformClient(
            error=EnterpriseOperationError("identity_missing", "нет личности вызова")
        )
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        with pytest.raises(SessionFilesUnavailable) as excinfo:
            await resolver.session_dir("postgres:42")

        assert "нет личности вызова" in str(excinfo.value)

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param({"session_id": "postgres:42"}, id="без-путей"),
            pytest.param({"root": "sessions/x", "files_dir": "sessions/x/files"}, id="относительные"),
            pytest.param(["root"], id="не-объект"),
        ],
    )
    async def test_unusable_answer_refuses(self, tmp_path: Path, answer: Any) -> None:
        """Ответ без пригодного абсолютного пути — тоже отказ.

        Подставлять свой путь вместо присланного нельзя: значит, платформа
        ответила не тем, чего ждали, и это должно быть видно, а не замаскировано
        правдоподобным каталогом.
        """
        client = FakePlatformClient(answer)
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        with pytest.raises(SessionFilesUnavailable):
            await resolver.files_dir("postgres:42")

    async def test_blank_session_key_refuses_before_any_call(self, tmp_path: Path) -> None:
        """Оборот без ``session_key`` не получает служебную папку.

        Имя каталога вычисляется после проверки ключа, иначе пустая сессия
        получила бы ``__nosession__`` и поделила папку со всеми такими же.
        """
        client = FakePlatformClient(_answer(tmp_path))
        resolver = SessionFileResolver(enterprise_mcp=client, workspace_dir=tmp_path)

        with pytest.raises(SessionFilesUnavailable):
            await resolver.files_dir("   ")

        assert client.calls == []


class TestWithoutPlatform:
    """Режим без платформы: корень объявляет агент, а объявления платформы нет."""

    def _resolver(
        self, workspace: Path, settings: Any = None
    ) -> SessionFileResolver:
        return SessionFileResolver(enterprise_mcp=None, workspace_dir=workspace, settings=settings)

    async def test_declared_root_is_used(self, tmp_path: Path) -> None:
        root = tmp_path / "declared"
        resolver = self._resolver(tmp_path / "ws", {"session_files": {"root": str(root)}})

        assert resolver.has_platform is False
        assert await resolver.session_dir("postgres:42") == root / "postgres_42"
        assert await resolver.files_dir("postgres:42") == root / "postgres_42" / FILES_SUBDIR

    async def test_default_root_while_the_key_is_absent(self, tmp_path: Path) -> None:
        """Дефолт — на время, пока объявления в ``config.json`` нет (задача 2.2).

        Ключа ``gateway.agent.session_files.root`` в конфиге сейчас нет, и ветка
        дефолта обязана быть рабочей, а не заглушкой: иначе режим без платформы
        был бы сломан до появления объявления.
        """
        workspace = tmp_path / "ws"
        resolver = self._resolver(workspace, {})

        expected = workspace / DEFAULT_ROOT_PARTS[0] / DEFAULT_ROOT_PARTS[1] / "postgres_42"
        assert await resolver.session_dir("postgres:42") == expected
        assert await resolver.files_dir("postgres:42") == expected / FILES_SUBDIR

    async def test_ensure_creates_session_and_files_dirs(self, tmp_path: Path) -> None:
        """Без платформы создаёт агент — и только свой каталог.

        Шесть остальных подкаталогов раскладки объявляет и создаёт платформа;
        заводить их здесь значило бы дублировать объявление, которое ещё и
        разошлось бы с платформенным при первой правке.
        """
        resolver = self._resolver(tmp_path / "ws", {})

        directory = await resolver.ensure("postgres:42")
        files_dir = await resolver.files_dir("postgres:42")

        assert directory.is_dir()
        assert files_dir.is_dir()
        assert not (directory / "calls").exists()
        assert not (directory / "results").exists()

    async def test_distinct_keys_get_distinct_dirs(self, tmp_path: Path) -> None:
        """Две сессии — две папки, а не одна папка на всех.

        Имя берётся из агентской функции ``safe_session_key``: вторая копия
        санитайзера в резолвере разошлась бы с ней при первой же правке, а
        согласие сторон проверяет контрактный тест.
        """
        resolver = self._resolver(tmp_path / "ws", {})

        first = await resolver.session_dir("postgres:42")
        second = await resolver.session_dir("telegram:42")

        assert first != second
        assert first.name == "postgres_42"
        assert second.name == "telegram_42"

    async def test_relative_declared_root_refuses(self, tmp_path: Path) -> None:
        """Относительный корень жил бы рядом с каталогом запуска.

        Именно так прежний корень платформы «переезжал» вместе с тем, откуда
        запустили, и две копии сессии оказывались в разных местах. Объявление
        обязано называть путь целиком.
        """
        resolver = self._resolver(tmp_path / "ws", {"session_files": {"root": "sessions"}})

        with pytest.raises(SessionFilesUnavailable) as excinfo:
            await resolver.session_dir("postgres:42")

        assert "абсолютным" in str(excinfo.value)

    async def test_blank_key_refuses_before_the_root_is_built(self, tmp_path: Path) -> None:
        """Проверка ключа не зависит от режима."""
        resolver = self._resolver(tmp_path / "ws", {})

        with pytest.raises(SessionFilesUnavailable):
            await resolver.files_dir("")


class TestCompositionRootWiring:
    def test_published_to_context_and_to_module(self, tmp_path: Path) -> None:
        """Резолвер достаётся обоими способами, которые реально работают.

        В ``ctx`` — для сервисов, которым composition root передаёт службы явно.
        В модуле — для плагинов ``workspace/hooks/``: ``hook_loader`` поднимает
        хук с единственным аргументом ``workspace_dir``, и ни конструктор хука,
        ни per-turn ``hook_factories`` (там нужен свой инстанс на оборот, а
        резолвер на процесс один) не позволяют положить туда службу.
        """
        from lib.core.application_context import _make_session_file_resolver

        ctx = SimpleNamespace(
            enterprise_mcp=None,
            workspace_dir=tmp_path / "ws",
            settings={},
        )

        resolver = _make_session_file_resolver(ctx)
        ctx.session_file_resolver = resolver

        assert ctx.session_file_resolver is resolver
        assert current_session_file_resolver() is resolver

    def test_unpublished_resolver_is_visible_as_none(self) -> None:
        """Без сборки контекста потребитель видит ``None``, а не ищет путь сам.

        Резолвер под рукой, но не опубликованный, — это отказ, и показывать его
        как ``None`` обязательно: иначе хук пойдёт искать каталог в обход
        резолвера, и объявление корня снова станет делом вызывающего.
        """
        install_session_file_resolver(None)

        assert current_session_file_resolver() is None

    async def test_hook_shaped_consumer_reaches_the_resolver(self, tmp_path: Path) -> None:
        """Плагин, поднятый как ``cls(workspace_dir=...)``, достаёт резолвер.

        Форма взята из ``lib.cli.hook_loader.scan_and_register`` — инстанс
        создаётся с одним аргументом, поэтому путь к службе у хука только один.
        Проверка не дублирует предыдущую: она показывает, что seam годится
        именно для той формы подключения, которая есть в репозитории.
        """
        from lib.core.application_context import _make_session_file_resolver

        class SessionFileRedirectHookStub:
            """Форма будущего ``SessionFileRedirectHook`` (фаза 3)."""

            def __init__(self, workspace_dir: Any) -> None:
                self._workspace_dir = workspace_dir
                self._resolver = current_session_file_resolver()

            def files_dir(self, session_key: str) -> Any:
                return self._resolver

        ctx = SimpleNamespace(
            enterprise_mcp=None,
            workspace_dir=tmp_path / "ws",
            settings={},
        )
        _make_session_file_resolver(ctx)

        hook = SessionFileRedirectHookStub(workspace_dir=ctx.workspace_dir)

        assert isinstance(hook.files_dir("postgres:42"), SessionFileResolver)


class TestClientOperation:
    """Контракт метода клиента: имя операции, аргументы, разбор ответа."""

    def _client(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> tuple[Any, list[Any]]:
        recorded: list[Any] = []

        async def fake_call(operation: str, arguments: Any = None, identity: Any = None) -> str:
            recorded.append((operation, arguments, identity))
            return reply

        client = EnterpriseMcpClient(command="python")
        monkeypatch.setattr(client, "call", fake_call)
        return client, recorded

    async def test_only_ensure_is_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Корень уходит в ответ, а не в аргументы вызова."""
        client, recorded = self._client(
            monkeypatch, json.dumps(_answer(Path("/srv/sessions")))
        )
        identity = CallIdentity(session_id="postgres:42", user_id="u1", request_id="r1")

        answer = await client.session_files(identity)

        assert answer["files_dir"].endswith("files")
        operation, arguments, passed_identity = recorded[0]
        assert operation == SESSION_FILES_OPERATION
        assert arguments == {"ensure": True}
        assert passed_identity is identity

    async def test_ensure_false_is_forwarded_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ensure=False`` доезжает до операции: создавать решает вызывающий."""
        client, recorded = self._client(
            monkeypatch, json.dumps(_answer(Path("/srv/sessions")))
        )

        await client.session_files(ensure=False)

        assert recorded[0][1] == {"ensure": False}

    @pytest.mark.parametrize(
        "reply",
        [
            pytest.param("не json", id="не-json"),
            pytest.param('["root"]', id="не-объект"),
        ],
    )
    async def test_unusable_reply_raises(
        self, monkeypatch: pytest.MonkeyPatch, reply: str
    ) -> None:
        """Метод не подменяет ответ: негодный — доменная ошибка операции.

        Иначе резолвер получил бы пустой объект и отказался уже без причины, а
        по журналу было бы видно «отказ резолвера» там, где отказалась платформа.
        """
        client, _ = self._client(monkeypatch, reply)

        with pytest.raises(EnterpriseOperationError) as excinfo:
            await client.session_files()

        assert excinfo.value.code == "session_files_invalid"


def test_lazy_reader_sees_a_resolver_published_after_import() -> None:
    """Модуль импортирован до публикации — и всё равно видит резолвер.

    Хук читает резолвер лениво, уже в обороте, а не в конструкторе: хук
    поднимается на шаге 6a, а публикация происходит позже. Если бы чтение было
    ранним, порядок composition root'а стал бы частью контракта и хук получил
    бы ``None`` на любом перестановке шагов.
    """
    resolver = SessionFileResolver(enterprise_mcp=None, workspace_dir=Path.cwd())
    assert session_files_module.current_session_file_resolver() is not resolver

    install_session_file_resolver(resolver)

    assert session_files_module.current_session_file_resolver() is resolver
