"""Куда девается stderr процесса платформы и как на него смотреть.

Зачем
-----
``enterprise-mcp`` — отдельная программа, которую агент поднимает как
дочерний процесс. Всё, что она печатает, уходит в **stderr**, и по
умолчанию этот stderr — тот же самый stderr агента: строки платформы
попадают в консоль gateway вперемешку с журналом самого агента, без
границы между ними. Отделить одно от другого в этой смеси нельзя, а
именно это и нужно при разборе «платформа молчит» или «платформа отвечает
не тем».

Почему не «просто окно у процесса»
----------------------------------
Окно у дочернего процесса на Windows нельзя получить, не завладев его
созданием: SDK mcp создаёт процесс с ``CREATE_NO_WINDOW``, а параметра
creationflags у ``stdio_client`` нет — только ``errlog``. Взять создание
процесса на себя значит скопировать к себе протокольную обвязку
``stdio_client`` (потоки, task group, разбор сообщений), а она
переписывается при каждом обновлении SDK.

Поэтому перенаправляется ровно то, что у процесса отдано наружу:
``errlog``. Это публичный параметр ``mcp.client.stdio.stdio_client`` — тем
же самым, которым SDK сам отправляет stderr сервера в stderr агента.
Открытый нами файл становится приёмником, а окно, его показывающее, —
отдельной программой, которая читает этот файл и ждёт продолжения.

Одна приёмная часть — на обеих платформах. Различаются только зрители:
на Windows это новая консоль с ``Get-Content -Wait``, на Linux — терминал
с ``tail -F``. На headless-машине окна нет и быть не может, тогда
остаётся файл, и об этом сказано прямо в отчёте, а не обнаруживается по
отсутствию строк в консоли.

Чего модуль не делает
---------------------
Второй процесс платформы — тот, что поднимает штатный ``MCPProvider``
нанобота для ``mcp_enterprise_*`` — здесь не виден: нанобот зовёт
``stdio_client(params)`` без ``errlog``, и перенаправить его stderr можно
только патчем нанобота. Его вывод по-прежнему идёт в stderr агента, то
есть виден, но вперемешку. Объявление обоих процессов — в AGENTS.md.

Никаких настроек платформы отсюда не уходит: файл журнала — это
собственность транспорта на стороне агента (куда девать stderr процесса,
который агент и поднимает), а не объявление платформы. Поэтому в
``_child_env`` ничего не добавляется, и страж границы
(``tests/test_enterprise_mcp_settings_contract.py``) остаётся зелёным.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from loguru import logger

#: Заголовок окна. Одинаков на обеих платформах: по нему окно узнаётся
#: в списке задач, где заголовок «enterprise-mcp» слишком общий — рядом
#: может висеть окно самого gateway.
VIEWER_TITLE = "enterprise-mcp stderr"

#: Команда зрителя на Linux. ``-n +1`` — печатать файл с начала, чтобы в
#: окне был не только хвост: подъём платформы идёт первыми строками, и
#: именно он чаще всего нужен. ``-F``, а не ``-f``: файл могут
#: пересоздать (перезапуск агента), а ``-F`` переоткроет его сам.
TAIL_COMMAND: tuple[str, ...] = ("tail", "-n", "+1", "-F")


@dataclass(frozen=True)
class TerminalCandidate:
    """Терминал-зритель: как именно он умеет принять команду.

    ``exec_arg`` — флаг, после которого идёт команда (``None`` — сразу
    после опций, как у ``kitty``). ``joined`` означает, что команду надо
    склеить в одну строку: ``gnome-terminal -e`` и ``xfce4-terminal -e``
    разбирают остаток как одну команду, и несколькими аргументами команду
    им не передать. ``title`` — умеет ли терминал переименовать окно; у
    ``x-terminal-emulator`` ответ зависит от того, на что указывает
    alternatives-ссылка, поэтому заголовок ему не передаётся.
    """

    name: str
    exec_arg: str | None = "-e"
    joined: bool = True
    title: bool = True


#: Порядок — от самого вероятного к самому базовому. Проверяется
#: ``shutil.which``, отсутствие терминала пропускается молча: отсутствие
#: графической оболочки не должна быть видно как ошибка запуска.
TERMINAL_CANDIDATES: tuple[TerminalCandidate, ...] = (
    TerminalCandidate("gnome-terminal"),
    TerminalCandidate("konsole"),
    TerminalCandidate("xfce4-terminal"),
    TerminalCandidate("xterm"),
    TerminalCandidate("alacritty", joined=False),
    TerminalCandidate("kitty", exec_arg=None, joined=False),
    TerminalCandidate("x-terminal-emulator", title=False),
)


@dataclass(frozen=True)
class StderrRedirect:
    """Готовый приёмник: файл, открытый на запись, и результат попытки
    открыть окно.

    ``viewer`` — человекочитаемый отчёт, а не признак успеха: окно могло
    и не открыться (нет ``DISPLAY``, нет терминала), и вызывающая сторона
    печатает этот текст в баннере, чтобы режим наблюдения не был
    молчаливым.
    """

    path: Path
    handle: IO[str]
    viewer: str


def open_redirect(raw_path: str | os.PathLike[str] | None) -> StderrRedirect | None:
    """Открыть файл-приёмник stderr и попытаться показать его в окне.

    ``None`` — перенаправления нет: либо путь не объявлен, либо файл не
    открылся. Второй случай **не поднимает исключение**: невозможность
    писать журнал в файл не должна ронять старт агента — процесс
    платформы в этом случае продолжит писать в stderr агента, как было до
    перенаправления, и отчёт об этом попадёт в баннер.

    Файл открывается на **запись с обрезкой**: журнал относится к прогону,
    а зритель, висящий с прошлого прогона, не должен показывать его хвост
    как текущий вывод. Один клиент открывает файл один раз, а
    переподключение после обрыва дописывает в него же — иначе
    диагностический лог разорвался бы ровно в том месте, ради которого его
    и читают.
    """
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "enterprise-mcp: stderr в файл не пишется ({}: {}) — "
            "процесс продолжит писать в stderr агента",
            path,
            exc,
        )
        return None
    return StderrRedirect(path=path, handle=handle, viewer=spawn_viewer(path))


def spawn_viewer(path: Path) -> str:
    """Показать файл в отдельном окне. Возвращает отчёт, не бросает."""
    if sys.platform.startswith("win"):
        return _spawn_windows_viewer(path)
    if sys.platform.startswith("linux"):
        return _spawn_linux_viewer(path)
    return f"окно на {sys.platform} не поддерживается — журнал: {path}"


def windows_viewer_command(path: Path) -> list[str]:
    """Команда зрителя Windows: PowerShell, читающий файл с ожиданием.

    ``chcp 65001`` и ``OutputEncoding`` обязательны: stderr процесса
    платформы — UTF-8 (этим занимается ``PYTHONIOENCODING`` в
    ``_child_env``), а консоль Windows по умолчанию cp866/cp1251, и
    русские сообщения в окне превратились бы в мусор именно там, где их
    читают.

    ``-NoExit`` оставляет окно после окончания чтения: если файл
    недоступен или команда падает, окно с текстом ошибки полезнее
    мигнувшего и исчезнувшего.

    Кавычки в пути удваиваются: литерал PowerShell, а не аргумент
    командной строки.
    """
    literal = str(path).replace("'", "''")
    script = (
        "chcp 65001 | Out-Null;"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        f"$host.UI.RawUI.WindowTitle = '{VIEWER_TITLE}';"
        f"Get-Content -LiteralPath '{literal}' -Encoding UTF8 -Wait"
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-NoLogo",
        "-NoExit",
        "-Command",
        script,
    ]


def linux_viewer_argv(candidate: TerminalCandidate, path: Path) -> list[str]:
    """Команда зрителя Linux для конкретного терминала."""
    tail = [*TAIL_COMMAND, str(path)]
    argv = [candidate.name]
    if candidate.title:
        argv.append(f"--title={VIEWER_TITLE}")
    if candidate.exec_arg:
        argv.append(candidate.exec_arg)
    if candidate.joined:
        argv.append(shlex.join(tail))
    else:
        argv.extend(tail)
    return argv


def _spawn_windows_viewer(path: Path) -> str:
    """Отдельная консоль: ``CREATE_NEW_CONSOLE``, а не окно в текущей.

    Своя консоль нужна по двум причинам. ``CREATE_NO_WINDOW`` (дефолт SDK)
    не даёт окна вовсе, а приведённый к общему ``DETACHED_PROCESS``
    зритель нельзя было бы закрыть вместе с родителем и нельзя было бы
    увидеть его ошибку. И обратное тоже важно: своя консоль означает, что
    ``Ctrl+C`` в консоли gateway до зрителя не доходит.
    """
    flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    for shell in ("powershell.exe", "pwsh"):
        exe = shutil.which(shell)
        if exe is None:
            continue
        argv = [exe, *windows_viewer_command(path)[1:]]
        try:
            process = subprocess.Popen(  # noqa: S603 - argv собран выше, путь экранирован
                argv,
                creationflags=flag,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            logger.warning("enterprise-mcp: зритель {} не запустился: {}", shell, exc)
            continue
        return f"отдельная консоль {shell} (pid {process.pid}), файл: {path}"
    return f"окно не открылось (не найден powershell/pwsh) — файл: {path}"


def _spawn_linux_viewer(path: Path) -> str:
    """Терминал с ``tail -F``.

    Без ``DISPLAY``/``WAYLAND_DISPLAY`` окно открыть некуда — это не
    ошибка (шлюз нередко поднимают на машине без графики), поэтому
    возвращается отчёт, а окно не ищется впустую.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return f"headless (нет DISPLAY) — окно не открыто, файл: {path}"
    for candidate in TERMINAL_CANDIDATES:
        exe = shutil.which(candidate.name)
        if exe is None:
            continue
        argv = [exe, *linux_viewer_argv(candidate, path)[1:]]
        try:
            process = subprocess.Popen(  # noqa: S603 - argv собран выше
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            logger.warning(
                "enterprise-mcp: зритель {} не запустился: {}", candidate.name, exc
            )
            continue
        return f"отдельное окно {candidate.name} (pid {process.pid}), файл: {path}"
    return f"окно не открылось (нет терминала) — файл: {path}"


def write_marker(redirect: StderrRedirect | None, text: str) -> None:
    """Дописать строку в файл-приёмник.

    Нужна, чтобы окно зрителя не осталось висеть молча после остановки
    агента: строка видна в окне и объясняет, почему вывод перестал
    появляться. Ошибка записи молча игнорируется — это диагностический
    текст, а не данные.
    """
    if redirect is None:
        return
    try:
        redirect.handle.write(f"{text}\n")
        redirect.handle.flush()
    except (OSError, ValueError):  # noqa: BLE001 - файл мог закрыться вместе с процессом
        pass


def describe_redirect(redirect: StderrRedirect | None, *, declared: Any = None) -> dict[str, Any]:
    """Сведения о перенаправлении для баннера и диагностики.

    ``declared`` — путь из объявления на случай, когда файл ещё не
    открыт: баннер печатается и до подъёма сессии, а «ничего не
    объявлено» и «объявлено, но ещё не открыто» — разные утверждения, и
    свести их к одному значит соврать о режиме наблюдения.
    """
    if redirect is not None:
        return {
            "stderr_log": str(redirect.path),
            "stderr_viewer": redirect.viewer,
        }
    if declared:
        return {
            "stderr_log": str(declared),
            "stderr_viewer": "ещё не открыт",
        }
    return {"stderr_log": None, "stderr_viewer": "выключено"}
