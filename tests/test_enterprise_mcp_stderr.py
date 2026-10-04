"""Тесты перенаправления stderr процесса платформы.

Дефект, который закрывает этот файл
-----------------------------------
У процесса ``enterprise-mcp`` нет своего окна и своего журнала: SDK mcp
создаёт его с ``CREATE_NO_WINDOW``, а его stderr уходит в stderr агента.
В консоли gateway строки платформы лежат вперемешку с журналом агента, и
«посмотреть, что делает платформа» было нечем.

Проверяется не «окно красивое», а четыре утверждения, каждое из которых
ломается своим способом:

  * файл-приёмник действительно создаётся и **обнуляется** на каждый
    прогон (в режиме дописки зритель прошлого прогона показывал бы свой
    хвост как текущий вывод);
  * путь из объявления доезжает до ``stdio_client`` как ``errlog``, а без
    объявления поведение прежнее — stderr агента;
  * зрители на обеих платформах строятся из аргументов, а не из
    предположений (headless, другой терминал, кавычки в пути);
  * путь **не едет в argv процесса**: это транспорт агента, а не
    настройка платформы.

Тесты не открывают окон и не запускают процессов: на это стоит
автофикстура, и она держит два ограничения сразу.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lib.services import enterprise_mcp_stderr as stderr_mod
from lib.services.enterprise_mcp_client import client_from_settings
from lib.services.enterprise_mcp_stderr import (
    VIEWER_TITLE,
    StderrRedirect,
    TerminalCandidate,
    describe_redirect,
    linux_viewer_argv,
    open_redirect,
    windows_viewer_command,
    write_marker,
)

#: Ответ заглушки зрителя: по нему видно, что окно действительно
#: запрашивалось, а не что тест прошёл мимо шва.
STUB_VIEWER = "зритель (тест)"

#: Настоящая функция, снятая до подмены автофикстурой. Проверки разведения
#: по платформам зовут именно её: после подмены имя в модуле указывает на
#: заглушку, и вызов ``stderr_mod.spawn_viewer`` молча проверил бы заглушку
#: вместо разведения.
REAL_SPAWN_VIEWER = stderr_mod.spawn_viewer


@pytest.fixture(autouse=True)
def _window_probe(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """Зрителей в тестах нет — ни окон, ни процессов. Проба вместо них.

    Ограничений два, и каждое закрывает свой обход. ``spawn_viewer``
    подменён заглушкой: иначе на машине с графикой каждый вызов
    ``open_redirect`` открывал бы настоящее окно. ``subprocess.Popen``
    запрещён: иначе достаточно одного нового прямого вызова в обход шва,
    чтобы окна снова пошли, и никто бы этого не заметил.

    Заглушка снимает содержимое файла **в момент подъёма зрителя** — так
    проверяется порядок «заголовок написан, потом открыто окно».
    """
    probes: list[tuple[Path, str]] = []

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail(f"тест попытался запустить процесс: {args[:1]}")

    def _stub(path: Path) -> str:
        try:
            seen = Path(path).read_text(encoding="utf-8")
        except OSError:
            seen = ""
        probes.append((Path(path), seen))
        return STUB_VIEWER

    monkeypatch.setattr(stderr_mod, "spawn_viewer", _stub)
    monkeypatch.setattr(stderr_mod.subprocess, "Popen", _forbidden)
    return probes


def _section(**extra: Any) -> dict:
    section: dict[str, Any] = {
        "enabled": True,
        "command": "python",
        "args": ["-m", "servers.enterprise.server"],
    }
    section.update(extra)
    return {"enterprise_mcp": section}


class TestRedirectFile:
    def test_absent_declaration_means_no_redirect(self) -> None:
        """Нет объявления — нет файла: поведение до этой правки."""
        assert open_redirect(None) is None
        assert open_redirect("") is None

    def test_declared_path_creates_the_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "enterprise-mcp.log"

        redirect = open_redirect(target)

        assert redirect is not None
        assert target.exists()
        assert redirect.path == target
        assert redirect.handle.writable()
        assert redirect.viewer == STUB_VIEWER

    def test_log_is_truncated_not_appended(self, tmp_path: Path) -> None:
        """Журнал относится к прогону.

        Режим дописки смешал бы вывод двух прогонов в одном файле, и
        зритель второго прогона показывал бы чужой хвост как текущий.
        """
        target = tmp_path / "enterprise-mcp.log"
        target.write_text("прошлый прогон\n", encoding="utf-8")

        redirect = open_redirect(target)

        assert redirect is not None
        assert "прошлый прогон" not in target.read_text(encoding="utf-8")

    def test_window_is_never_empty(self, tmp_path: Path, _window_probe) -> None:
        """Окно не может открыться пустым — и это проверяется по порядку.

        Платформа печатает первое сообщение через несколько секунд (импорт,
        прогрев, индексы), а окно открывается раньше. Без заголовка в
        начале журнала окно какое-то время пусто, и пустое окно неотличимо
        от «смотреть не на что».
        """
        open_redirect(tmp_path / "enterprise-mcp.log")

        assert _window_probe, "зритель вообще не поднимался"
        path, content = _window_probe[-1]
        assert path.name == "enterprise-mcp.log"
        assert "stderr процесса enterprise-mcp" in content, (
            "зритель поднялся раньше, чем в файле появился заголовок"
        )

    def test_unopenable_file_is_not_fatal(self, tmp_path: Path) -> None:
        """Каталог вместо файла — не повод ронять старт агента.

        Процесс платформы продолжит писать в stderr агента, и отчёт об
        этом попадёт в баннер: невозможность вести журнал в файл не
        должна выглядеть как невозможность поднять платформу.
        """
        directory = tmp_path / "not-a-file"
        directory.mkdir()

        assert open_redirect(directory) is None

    def test_marker_reaches_the_file(self, tmp_path: Path) -> None:
        """Метка о закрытии видна в окне зрителя."""
        target = tmp_path / "enterprise-mcp.log"
        redirect = open_redirect(target)
        assert redirect is not None

        write_marker(redirect, "— закрыто —")

        assert "— закрыто —" in target.read_text(encoding="utf-8")

    def test_marker_without_redirect_is_noop(self) -> None:
        write_marker(None, "— закрыто —")


class TestClientWiring:
    def test_declared_path_becomes_errlog(self, tmp_path: Path) -> None:
        target = tmp_path / "enterprise-mcp.log"
        client = client_from_settings(_section(stderr_log=str(target)))
        assert client is not None

        errlog = client._errlog()

        assert errlog is not None
        assert errlog.fileno() > 0
        assert target.exists()
        assert str(target) in client.stderr_report()

    def test_without_declaration_errlog_stays_none(self) -> None:
        """``None`` — значение по умолчанию ``stdio_client``: stderr агента."""
        client = client_from_settings(_section())
        assert client is not None

        assert client._errlog() is None
        assert "stderr агента" in client.stderr_report()

    def test_blank_declaration_is_no_declaration(self) -> None:
        client = client_from_settings(_section(stderr_log="   "))
        assert client is not None

        assert client._errlog() is None

    def test_file_is_opened_once_per_client(self, tmp_path: Path) -> None:
        """Переподключение после обрыва дописывает в тот же файл.

        Иначе каждый подъём сессии открывал бы файл заново и обнулял его,
        то есть лог обрывался бы ровно там, ради чего его читают.
        """
        target = tmp_path / "enterprise-mcp.log"
        client = client_from_settings(_section(stderr_log=str(target)))
        assert client is not None

        first = client._errlog()
        assert first is not None
        write_marker(
            StderrRedirect(path=target, handle=first, viewer=STUB_VIEWER),
            "первая строка",
        )
        second = client._errlog()

        assert second is first
        assert "первая строка" in target.read_text(encoding="utf-8")

    def test_path_never_reaches_process_arguments(self, tmp_path: Path) -> None:
        """Путь журнала — транспорт агента, а не объявление платформы.

        Стража границы (``test_enterprise_mcp_settings_contract``) смотрит
        на ``ENTERPRISE_*`` в коде клиента и не заметила бы путь к файлу в
        argv: такой аргумент выглядел бы как настройка платформы, и
        окружение вновь начало бы перебивать ``platform.json``.
        """
        target = tmp_path / "platform-secret-path" / "enterprise-mcp.log"
        client = client_from_settings(_section(stderr_log=str(target)))
        assert client is not None

        args = " ".join(str(a) for a in client.describe()["args"])

        assert "enterprise-mcp.log" not in args
        assert "platform-secret-path" not in args

    def test_describe_reports_declared_path_before_session(self, tmp_path: Path) -> None:
        """Баннер печатается и до подъёма сессии."""
        target = tmp_path / "enterprise-mcp.log"
        client = client_from_settings(_section(stderr_log=str(target)))
        assert client is not None

        described = client.describe()

        assert described["stderr_log"] == str(target)
        assert described["stderr_viewer"] == "ещё не открыт"

    def test_describe_reports_no_redirect_when_not_declared(self) -> None:
        client = client_from_settings(_section())
        assert client is not None

        assert client.describe()["stderr_log"] is None
        assert client.describe()["stderr_viewer"] == "выключено"


class TestWindowsViewer:
    def test_command_waits_for_the_file(self, tmp_path: Path) -> None:
        target = tmp_path / "enterprise-mcp.log"
        command = windows_viewer_command(target)

        assert "Get-Content" in command[-1]
        assert "-Wait" in command[-1]
        assert str(target) in command[-1]

    def test_command_forces_utf8(self, tmp_path: Path) -> None:
        """Без ``chcp 65001`` русские сообщения в окне — мусор.

        stderr процесса — UTF-8 (этим занимается ``PYTHONIOENCODING``), а
        консоль Windows по умолчанию однобайтовая. Именно в окне эти
        сообщения и читают, поэтому кодировка задаётся явно.
        """
        script = windows_viewer_command(tmp_path / "x.log")[-1]

        assert "chcp 65001" in script
        assert "OutputEncoding" in script

    def test_quote_in_path_is_escaped(self, tmp_path: Path) -> None:
        """Литерал PowerShell, а не аргумент командной строки."""
        target = tmp_path / "odd'name" / "enterprise-mcp.log"

        script = windows_viewer_command(target)[-1]

        assert "odd''name" in script
        assert script.count("'") % 2 == 0

    def test_window_stays_open_after_the_file_ends(self, tmp_path: Path) -> None:
        """``-NoExit``: ошибка зрителя должна остаться видимой."""
        assert "-NoExit" in windows_viewer_command(tmp_path / "x.log")

    def test_title_is_shared_with_linux(self) -> None:
        assert VIEWER_TITLE == "enterprise-mcp stderr"


class TestLinuxViewer:
    def test_joined_terminals_get_one_command(self, tmp_path: Path) -> None:
        """``gnome-terminal -e`` разбирает остаток как одну команду."""
        target = tmp_path / "enterprise-mcp.log"

        argv = linux_viewer_argv(TerminalCandidate("gnome-terminal"), target)

        assert argv[0] == "gnome-terminal"
        assert argv[1] == f"--title={VIEWER_TITLE}"
        assert argv[2] == "-e"
        assert str(target) in argv[-1]
        assert "tail" in argv[-1]

    def test_argv_terminals_get_separate_arguments(self, tmp_path: Path) -> None:
        target = tmp_path / "enterprise-mcp.log"

        argv = linux_viewer_argv(
            TerminalCandidate("kitty", exec_arg=None, joined=False), target
        )

        assert argv[0] == "kitty"
        assert "-e" not in argv
        assert argv[-5:] == ["tail", "-n", "+1", "-F", str(target)]

    def test_tail_follows_a_recreated_file(self, tmp_path: Path) -> None:
        """``-F``, а не ``-f``: файл пересоздаётся при перезапуске агента."""
        target = tmp_path / "enterprise-mcp.log"

        joined = linux_viewer_argv(TerminalCandidate("xterm"), target)[-1]
        separate = linux_viewer_argv(
            TerminalCandidate("kitty", exec_arg=None, joined=False), target
        )

        assert "-F" in joined
        assert "-F" in separate

    def test_tail_prints_from_the_start(self, tmp_path: Path) -> None:
        """Подъём платформы — первые строки, и они нужнее всего."""
        argv = linux_viewer_argv(TerminalCandidate("xterm"), tmp_path / "x.log")

        assert "+1" in argv[-1]

    def test_title_is_skipped_where_it_is_not_known_to_work(self, tmp_path: Path) -> None:
        """``x-terminal-emulator`` — ссылка на разный терминал."""
        argv = linux_viewer_argv(
            TerminalCandidate("x-terminal-emulator", title=False), tmp_path / "x.log"
        )

        assert not any(arg.startswith("--title") for arg in argv)

    def test_candidate_list_is_not_empty(self) -> None:
        """Пустой список зрителей прошёл бы проверки выше вхолостую."""
        assert stderr_mod.TERMINAL_CANDIDATES
        assert all(candidate.name for candidate in stderr_mod.TERMINAL_CANDIDATES)


class TestViewerWritesToItsOwnConsole:
    """Регрессия: окно открывалось и оставалось пустым.

    ``Get-Content`` и ``tail`` пишут в **stdout**, а stdout зрителя был
    перенаправлен в ``DEVNULL`` — то есть в NUL. Заголовок окна при этом
    ставился (его задаёт сама PowerShell, а не вывод), поэтому окно
    выглядело живым: правильное имя, ноль строк. Нашлось это только глазами
    на экране, и потому проверка обязана быть отдельной: остальные тесты
    зрителей работают через заглушку и потоков не видят вовсе.
    """

    @staticmethod
    def _capture(
        monkeypatch: pytest.MonkeyPatch, terminal: str
    ) -> dict[str, Any]:
        """Подмена ``Popen`` и ``which``: запуск настоящий не нужен."""
        captured: dict[str, Any] = {}

        def _capture_popen(argv: list[str], **kwargs: Any) -> Any:
            captured["argv"] = list(argv)
            captured.update(kwargs)
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(stderr_mod.subprocess, "Popen", _capture_popen)
        monkeypatch.setattr(
            stderr_mod.shutil, "which", lambda name: f"/opt/fake/bin/{name}"
        )
        monkeypatch.setattr(
            stderr_mod, "TERMINAL_CANDIDATES", (TerminalCandidate(terminal),)
        )
        return captured

    @pytest.mark.parametrize("stream", ["stdin", "stdout", "stderr"])
    def test_windows_viewer_keeps_the_new_console(self, stream, tmp_path, monkeypatch) -> None:
        captured = self._capture(monkeypatch, "powershell.exe")

        report = stderr_mod._spawn_windows_viewer(tmp_path / "enterprise-mcp.log")

        assert captured, "зритель не был запущен — проверка вхолостую"
        assert captured.get(stream) is None, (
            f"{stream} зрителя перенаправлен в {captured.get(stream)!r}: "
            "вывод уйдёт мимо его окна, и окно будет пустым"
        )
        assert "creationflags" in captured
        assert "4242" in report

    @pytest.mark.parametrize("stream", ["stdin", "stdout", "stderr"])
    def test_linux_viewer_keeps_the_window(self, stream, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("DISPLAY", ":0")
        captured = self._capture(monkeypatch, "gnome-terminal")

        report = stderr_mod._spawn_linux_viewer(tmp_path / "enterprise-mcp.log")

        assert captured, "зритель не был запущен — проверка вхолостую"
        assert captured.get(stream) is None, (
            f"{stream} зрителя перенаправлен в {captured.get(stream)!r}: "
            "tail уйдёт в никуда, и окно будет пустым"
        )
        assert "4242" in report


class TestPlatformDispatch:
    def test_headless_linux_reports_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Нет ``DISPLAY`` — окно открыть некуда, и это не ошибка.

        Шлюз нередко поднимают на машине без графики. Отчёт обязан
        называть файл: иначе «окно не появилось» неотличимо от «смотреть
        не на что».
        """
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        target = tmp_path / "enterprise-mcp.log"

        report = stderr_mod._spawn_linux_viewer(target)

        assert str(target) in report
        assert "DISPLAY" in report

    def test_unknown_platform_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stderr_mod.sys, "platform", "darwin")

        report = REAL_SPAWN_VIEWER(tmp_path / "enterprise-mcp.log")

        assert "darwin" in report
        assert "enterprise-mcp.log" in report

    def test_windows_dispatch_reaches_the_windows_spawner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []
        monkeypatch.setattr(stderr_mod.sys, "platform", "win32")
        monkeypatch.setattr(
            stderr_mod,
            "_spawn_windows_viewer",
            lambda path: seen.append(path) or "окно",
        )

        assert REAL_SPAWN_VIEWER(tmp_path / "x.log") == "окно"
        assert seen == [tmp_path / "x.log"]

    def test_linux_dispatch_reaches_the_linux_spawner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []
        monkeypatch.setattr(stderr_mod.sys, "platform", "linux")
        monkeypatch.setattr(
            stderr_mod,
            "_spawn_linux_viewer",
            lambda path: seen.append(path) or "окно",
        )

        assert REAL_SPAWN_VIEWER(tmp_path / "x.log") == "окно"
        assert seen == [tmp_path / "x.log"]


class TestDescribeRedirect:
    def test_open_redirect_reports_the_viewer(self, tmp_path: Path) -> None:
        redirect = open_redirect(tmp_path / "enterprise-mcp.log")
        assert redirect is not None

        described = describe_redirect(redirect)

        assert described["stderr_log"] == str(tmp_path / "enterprise-mcp.log")
        assert described["stderr_viewer"] == redirect.viewer

    def test_declared_but_not_opened_is_not_the_same_as_off(self) -> None:
        """Два разных утверждения не сворачиваются в одно."""
        assert describe_redirect(None)["stderr_viewer"] == "выключено"
        assert (
            describe_redirect(None, declared="logs/x.log")["stderr_viewer"]
            == "ещё не открыт"
        )


class TestGuardIsNotVacuous:
    def test_forbidden_spawn_really_fails(self) -> None:
        """Если запрет_PROCESS перестанет срабатывать, окна откроются в
        прогоне тестов, и это будет выглядеть как обычный зелёный тест."""
        with pytest.raises(BaseException):  # noqa: B017, PT011 - это pytest.fail
            stderr_mod.subprocess.Popen(["cmd"])

    def test_window_is_opened_through_the_seam(self, tmp_path: Path) -> None:
        """Файл открывает и зрителя, и приёмник — один раз и в одном месте.

        Раздельные пути означали бы, что одна из двух половин работает без
        другой: файл без окна (или окно без файла) выглядит как «смотреть
        можно», а показывать нечего.
        """
        redirect = open_redirect(tmp_path / "enterprise-mcp.log")

        assert redirect is not None
        assert redirect.viewer == STUB_VIEWER

    def test_only_the_two_spawners_start_processes(self) -> None:
        """Третий прямой вызов ``Popen`` — это второй путь к окну."""
        source = Path(stderr_mod.__file__).read_text(encoding="utf-8")
        assert source.count("subprocess.Popen") == 2

    def test_module_declares_no_platform_setting(self) -> None:
        """Файл журнала — собственность транспорта агента.

        Любое ``ENTERPRISE_`` здесь означало бы попытку объявить настройку
        платформы из агента: окружение приоритетнее файла, и
        ``platform.json`` снова стал бы декорацией.
        """
        source = Path(stderr_mod.__file__).read_text(encoding="utf-8")
        assert "ENTERPRISE_" not in source
