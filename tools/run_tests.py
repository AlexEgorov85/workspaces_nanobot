"""Планировщик прогона pytest под жёстким потолком памяти.

Зачем он нужен. Наблюдался прогон pytest, раздувшийся до 3.5-9.2 ГБ на машине
с 16 ГБ: свободная память падала до 455 МБ, и это грозило потерей всей работы.
Локализовать утечку не удалось — контролируемые прогоны тех же файлов
укладываются в 24-42 секунды и память не трогают. Значит нужен барьер,
который работает ВСЕГДА, а не разбор конкретного теста.

Скрипт реализует ДВА слоя из трёх; третий — плагин ``tests/_memguard.py``,
его планировщик подключает сам:

1. Жёсткий потолок, обеспечиваемый ОС. Тест не может физически превысить
   бюджет: выделение памяти получает отказ. Лимит вешается на ВСЁ дерево
   процессов прогона, потому что агент в тестах порождает дочерние процессы
   (``enterprise-mcp``), и они тоже обязаны попадать под потолок.
2. Предел времени прогона. Зависший тест — тоже расход ресурса.

Запуск (флаги pytest — после ``--``, иначе argparse считает их своими)::

    # агентский набор (корень репозитория)
    python tools/run_tests.py -- -m "not live and not integration"

    # платформенный набор (другая кодовая база, тот же планировщик)
    python tools/run_tests.py --root mcp-platform -- -m "not live and not integration"

    # другой потолок
    python tools/run_tests.py --memory-cap-mb 2048 -- tests/test_foo.py -q

Коды возврата:

* ``0-5`` — код pytest пройден как есть (0 успех, 1 падения, 2 прерывание,
  3 внутренняя ошибка, 4 ошибка Usage, 5 тестов не собрано);
* ``120`` — сработал потолок памяти, дерево процессов снято планировщиком;
* ``121`` — ОС-ограничение построить НЕ удалось и прогон не состоялся.
  Молча пропускать барьер нельзя: «барьер, который не включился» хуже, чем
  его отсутствие, потому что о нём потом забывают. Обход —
  ``--no-memory-cap``, осознанно и с пониманием цены;
* ``124`` — истёк предел времени прогона, дерево процессов снято.

Что честно НЕ защищает. Лимит ограничивает КОММИТИРОВАННУЮ память (commit),
а не RSS, ни в Job Object (``JOB_OBJECT_LIMIT_JOB_MEMORY``), ни в
``RLIMIT_AS``. Это осознанный выбор в пользу одного определения бюджета:
commit включает резервирование адресного пространства, то есть ловит и
«съеденную» память, и распухание virt, чего RSS не видит. Плата — потолок
срабатывает чуть раньше, чем выйдет из-под него именно физическая память
процесса; на нормальных тестах запас между commit и RSS многократный.

Параллельный прогон (pytest-xdist). Потолок ВЕСЬ на дерево, а не на рабочий
процесс: Job Object и cgroup считают СУММУ по всем процессам задания. Это
сознательный выбор — суммарный бюджет это ровно то, ради чего барьер и
нужен (иначе N воркеров по 1 ГБ съедят N ГБ). Обратная сторона: при
``-n 4`` каждый воркер получает примерно четверть потолка, и лимит
сработает РАНЬШЕ времени. Что делать: на N воркеров поднимать потолок
примерно в N раз (``--memory-cap-mb 4096`` при ``-n 4``) либо запускать
воркеры отдельными прогонами планировщика. Слой атрибуции
(``tests/_memguard.py``) в этой схеме НЕ делится: каждый воркер измеряет
свой процесс и получает полный бюджет дельты на тест.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Бюджеты по умолчанию. Объявлены здесь и ТОЛЬКО здесь: планировщик и плагин
# атрибуции получают значения из окружения, но канон живёт в одном файле на
# кодовую базу (у платформы — свой экземпляр того же файла).
# ---------------------------------------------------------------------------

#: Потолок на ВСЁ дерево прогона. 1 ГБ — по условию задачи и по замеру:
#: нормальный прогон агентских тестов держится в ~157 МБ, то есть потолок
#: вшестеро выше нормы и вчетверо ниже того, что машина переживала (3.4 ГБ).
DEFAULT_MEMORY_CAP_MB = 1024

#: Предел времени прогона. Агентский набор замерян в ~2.5 минуты, 30 минут —
#: заведомо выше любого штатного прогона и заведомо ниже «висит, пока не убьют
#: вручную». Планировщик — последний рубеж: pytest-timeout в локальном
#: окружении не установлен (в CI устанавливается явно, см. ci.yml).
DEFAULT_MAX_SECONDS = 1800

ENV_MEMORY_CAP_MB = "NANOBOT_TEST_MEMORY_CAP_MB"
ENV_MAX_SECONDS = "NANOBOT_TEST_MAX_SECONDS"
ENV_DELTA_MB = "NANOBOT_TEST_MEM_DELTA_MB"

#: Плагин атрибуции: какой именно тест съел память. Подключается самим
#: планировщиком (слой 2), чтобы не зависеть от того, вспомнил ли человек
#: про ``-p``.
DELTA_GUARD_PLUGIN = "tests._memguard"

MB = 1024 * 1024

EXIT_MEMORY_CAP = 120
EXIT_NO_LIMIT = 121
EXIT_TIMEOUT = 124


# ---------------------------------------------------------------------------
# Разбор бюджетов: флаг > переменная окружения > значение по умолчанию.
# ---------------------------------------------------------------------------


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[run_tests] {name}={raw!r} не число — беру значение по умолчанию")
        return None


def _resolve(value: float | None, env_name: str, default: float) -> tuple[float, str]:
    if value is not None:
        return value, "флаг командной строки"
    from_env = _env_float(env_name)
    if from_env is not None:
        return from_env, env_name
    return float(default), "значение по умолчанию"


# ---------------------------------------------------------------------------
# Слой 1. Windows: Job Object с JOB_OBJECT_LIMIT_JOB_MEMORY.
#
# Лимит вешается на ЗАДАНИЕ (job), а не на процесс: в задание попадает pytest
# и всё, что он порождает (enterprise-mcp, воркеры pytest-xdist). Сам
# планировщик в задание НЕ входит — иначе ограничение накрыло бы и его, а с
# ним всё, что запущено из этой оболочки.
# ---------------------------------------------------------------------------


class _WinLimits:
    """Описатели структур и констант win32 для Job Object.

    Вынесено в отдельный класс не ради красоты, а ради одной причины: имена
    структур нужны и при создании задания, и при чтении пика, и при разборе
    «кто жив». Дублировать объявления struct в трёх функциях — это три места,
    где одна ошибка в размере поля не совпадёт с тем, что ожидает ядро.
    """

    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    JobObjectBasicAccountingInformation = 1
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400


def _win_kinds():
    """Структуры win32, собранные один раз (ctypes.Structure — на класс)."""
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t
    SIZE_T = ctypes.c_size_t
    ULONGLONG = ctypes.c_ulonglong  # IO_COUNTERS, LARGE_INTEGER — всегда 8 байт
    DWORD = wintypes.DWORD

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ULONGLONG),
            ("WriteOperationCount", ULONGLONG),
            ("OtherOperationCount", ULONGLONG),
            ("ReadTransferCount", ULONGLONG),
            ("WriteTransferCount", ULONGLONG),
            ("OtherTransferCount", ULONGLONG),
        ]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", DWORD),
            ("SchedulingClass", DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    class BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", DWORD),
            ("TotalProcesses", DWORD),
            ("ActiveProcesses", DWORD),
            ("TotalTerminatedProcesses", DWORD),
        ]

    return EXTENDED_LIMIT, BASIC_ACCOUNTING


def _windows_create_job(cap_bytes: int):
    """Создать job с потолком памяти ``cap_bytes``. Возвращает job_handle.

    Задание создаётся ДО порождения pytest — и это не оптимизация, а
    требование к порядку: иначе между ``CreateProcess`` и
    ``AssignProcessToJobObject`` ребёнок успевает закоммитировать больше
    потолка, и при входе в задание с уже превышенным лимитом ОС не отказывает
    в уже сделанных выделениях. Наблюдалось ровно это: ребёнок дошёл до 2 ГБ
    при потолке 320 МБ, а счётчик задания остался на 0.8 МБ.
    """
    import ctypes
    from ctypes import wintypes

    extended_limit, _ = _win_kinds()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW не удался")

    info = extended_limit()
    info.BasicLimitInformation.LimitFlags = (
        _WinLimits.JOB_OBJECT_LIMIT_JOB_MEMORY | _WinLimits.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    # Именно СУММА по всем процессам задания: это то, что делает потолок
    # общим для дерева, а не на каждый процесс отдельно.
    info.JobMemoryLimit = cap_bytes
    if not kernel32.SetInformationJobObject(
        job,
        _WinLimits.JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(err, "SetInformationJobObject(JOB_OBJECT_LIMIT_JOB_MEMORY) не удался")

    readback = extended_limit()
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    if kernel32.QueryInformationJobObject(
        job, _WinLimits.JobObjectExtendedLimitInformation, ctypes.byref(readback), ctypes.sizeof(readback), None
    ):
        if readback.JobMemoryLimit != cap_bytes:
            kernel32.CloseHandle(job)
            raise OSError(
                f"ОС приняла другие границы ({readback.JobMemoryLimit} вместо {cap_bytes})"
            )
    return job


def _windows_assign_job(job, pid: int):
    """Поместить уже порождённый процесс ``pid`` в готовое задание."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    handle = kernel32.OpenProcess(
        # PROCESS_SET_QUOTA обязателен: без него AssignProcessToJobObject
        # возвращает ACCESS_DENIED, и это выглядит как «ограничения не работают».
        _WinLimits.PROCESS_TERMINATE
        | _WinLimits.PROCESS_SET_QUOTA
        | _WinLimits.PROCESS_QUERY_INFORMATION,
        False,
        pid,
    )
    if not handle:
        raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) не удался")
    if not kernel32.AssignProcessToJobObject(job, handle):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(
            err,
            f"AssignProcessToJobObject({pid}) не удался — процесс, вероятно, уже в "
            "задании, не допускающем вложенности",
        )
    return handle


def _windows_job_peak(job) -> int:
    """Пик закоммиченной памяти задания в байтах (0 — не удалось прочитать)."""
    import ctypes
    from ctypes import wintypes

    extended_limit, _ = _win_kinds()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    buf = extended_limit()
    ok = kernel32.QueryInformationJobObject(
        job, _WinLimits.JobObjectExtendedLimitInformation, ctypes.byref(buf), ctypes.sizeof(buf), None
    )
    return int(buf.PeakJobMemoryUsed) if ok else 0


def _windows_active_processes(job) -> int:
    """Сколько процессов живо в задании (диагностика при срабатывании)."""
    import ctypes
    from ctypes import wintypes

    _, accounting = _win_kinds()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    buf = accounting()
    ok = kernel32.QueryInformationJobObject(
        job,
        _WinLimits.JobObjectBasicAccountingInformation,
        ctypes.byref(buf),
        ctypes.sizeof(buf),
        None,
    )
    return int(buf.ActiveProcesses) if ok else 0


def _windows_terminate(job, exit_code: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject(job, exit_code)


# ---------------------------------------------------------------------------
# Слой 1. Linux: cgroup v2 (memory.max), иначе RLIMIT_AS.
#
# ЧЕСТНО О РАЗЛИЧИЯХ ДВУХ МЕХАНИЗМОВ:
#
# * cgroup v2 ограничивает память задачи по ``memory.max`` и бьёт по сумме
#   дерева — самый близкий к «хотим ограничить память» механизм, прямой
#   аналог Job Object. Требует делегированной поддеревьевой cgroup: в
#   контейнере без systemd её обычно нет, поэтому путь используется только
#   когда он реально доступен, иначе срабатывает запасной вариант.
# * RLIMIT_AS ограничивает ВИРТУАЛЬНУЮ память (адресное пространство), а НЕ
#   RSS. Поэтому значение обязано быть ВЫШЕ целевого RSS: у процесса с numpy,
#   FAISS и пулом потоков VSZ в разы больше рабочего набора. Множитель
#   ``--linux-as-multiplier`` (по умолчанию 3.0) поднимает потолок так, чтобы
#   реальный лимит пришёлся примерно на целевой RSS. Плата очевидна: при
#   приложении, которое резервирует адресное пространство, но физически его не
#   трогает, RLIMIT_AS срабатывает раньше времени; при утечке, которую ОС
#   компенсирует возвратом страниц, VSZ может не дойти до порога при уже
#   переполненном RSS. Этот путь НЕ равноценен Job Object и остаётся запасным.
# ---------------------------------------------------------------------------

LINUX_AS_MULTIPLIER = 3.0
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _linux_cgroup_prepare(cap_bytes: int) -> Path | None:
    """Создать под-cgroup с ``memory.max``. None — делегирования нет."""
    try:
        if not (CGROUP_ROOT / "cgroup.controllers").exists():
            return None
        subtree = CGROUP_ROOT / "cgroup.subtree_control"
        if "memory" not in subtree.read_text().split():
            # Делегирования нет — раскладывать дальше бессмысленно.
            subtree.write_text("+memory")
        group = CGROUP_ROOT / f"nanobot-tests-{os.getpid()}"
        group.mkdir(exist_ok=True)
        (group / "memory.max").write_text(str(cap_bytes))
        return group
    except OSError:
        return None


def _linux_rlimit_as(cap_bytes: int, multiplier: float):
    """Возвращает ``preexec_fn``, ставящий RLIMIT_AS в ПОТОМКЕ.

    Ставится именно в потомке: ``setrlimit`` необратим для процесса, и в
    планировщике он урезал бы потолок самому планировщику.
    """
    import resource

    limit = int(cap_bytes * multiplier)

    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    _apply.limit_bytes = limit  # type: ignore[attr-defined]
    return _apply


# ---------------------------------------------------------------------------
# Сторож прогона: потолок памяти + предел времени.
# ---------------------------------------------------------------------------


class _Watchdog:
    """Фоновый поток: следит за пиком памяти задания и за временем.

    Потолок ловится по ПИКОВОМУ значению задания: как только сумма
    закоммиченной памяти дерева дошла до лимита, система обязана отказать в
    выделении. Сторож превращает это в внятную диагностику и снимает дерево —
    иначе прогон продолжал бы «мылить» в MemoryError по всем тестам подряд.

    Лимитирующим является НЕ сторож, а ОС: потолок поставлен до запуска
    pytest, и отказ в выделении памяти получает сам тест. Сторож — страховка
    и источник диагностики, а не единственная линия.
    """

    def __init__(self, proc: subprocess.Popen, job, cap_bytes: int, max_seconds: float) -> None:
        self._proc = proc
        self._job = job
        self._cap_bytes = cap_bytes
        self._max_seconds = max_seconds
        self.memory_hit = False
        self.timeout_hit = False
        self.peak_bytes = 0
        self.active_processes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="run_tests-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(0.2):
            if self._proc.poll() is not None:
                # Последний замер ПЕРЕД выходом. Без него прогон, упавший в
                # потолок быстро, классифицировался бы как обычный: цикл
                # опрашивает раз в 0,2 с, а процесс к тому моменту уже мёртв
                # и опроса не будет. Именно так и выглядел отказ ОС при нулевой
                # диагностике — худший вид поломки: потолок сработал, а
                # отчёт сказал «прогон просто упал». Пик задания переживает
                # смерть процесса, поэтому показание доступно и после неё.
                self._sample_once()
                return
            if time.monotonic() - started >= self._max_seconds:
                self.timeout_hit = True
                self._kill_tree()
                return
            if self._job is None:
                continue
            peak = _windows_job_peak(self._job)
            if peak > self.peak_bytes:
                self.peak_bytes = peak
            if peak >= self._cap_bytes:
                self.memory_hit = True
                try:
                    self.active_processes = _windows_active_processes(self._job)
                except OSError:
                    self.active_processes = 0
                self._kill_tree()
                return

    def _sample_once(self) -> None:
        """Снять пик задания один раз; ничего не убивать и не классифицировать.

        Вызывается после смерти процесса, поэтому единственное, что тут
        уместно, — обновить показание. Потолок уже сработал на стороне ОС, и
        повторный ``kill`` тут был бы выстрелом в мёртвое дерево.
        """
        if self._job is None:
            return
        peak = _windows_job_peak(self._job)
        if peak > self.peak_bytes:
            self.peak_bytes = peak
        if peak >= self._cap_bytes and not self.memory_hit:
            self.memory_hit = True
            try:
                self.active_processes = _windows_active_processes(self._job)
            except OSError:
                self.active_processes = 0

    def _kill_tree(self) -> None:
        try:
            if self._job is not None:
                _windows_terminate(self._job, EXIT_MEMORY_CAP)
        except OSError:
            pass
        if self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Запуск.
# ---------------------------------------------------------------------------


def _split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Разделить ``argv`` на свои флаги и аргументы pytest.

    Разделение по первому ``--`` сделано вручную, а не через
    ``nargs=REMAINDER``: argparse не может отличить ``-m pytest`` от своих
    опций и роняет прогон с «unrecognized arguments» ещё ДО запуска pytest.
    """
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    # Без ``--`` принимаются только позиционные цели (пути к тестам) и флаги
    # самого планировщика: иначе опечатка в флаге планировщика ушла бы в
    # pytest молча. ``-h``/``--help`` обязаны остаться здесь — иначе вопрос
    # «как этим пользоваться» приводит к ЗАПУСКУ прогна с ключом --help,
    # который тихо съедает pytest.
    known = {"--memory-cap-mb", "--max-seconds", "--root", "--no-delta-guard",
             "--no-memory-cap", "--linux-as-multiplier", "-h", "--help"}
    own: list[str] = []
    rest: list[str] = []
    seen_rest = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not seen_rest:
            key = arg.split("=", 1)[0]
            if arg in known or (key in known and "=" in arg):
                own.append(arg)
                if key in {"--memory-cap-mb", "--max-seconds", "--root", "--linux-as-multiplier"}:
                    if "=" not in arg and i + 1 < len(argv):
                        own.append(argv[i + 1])
                        i += 1
            else:
                seen_rest = True
                rest.append(arg)
        else:
            rest.append(arg)
        i += 1
    return own, rest


def _inject_delta_guard(argv: list[str]) -> list[str]:
    """Подключить слой атрибуции, если пользователь его ещё не указал."""
    if DELTA_GUARD_PLUGIN in argv:
        return argv
    return [*argv, "-p", DELTA_GUARD_PLUGIN]


def _banner(args, cap_mb, cap_source, seconds, seconds_source, mechanism, command) -> None:
    print("[run_tests] прогон pytest под жёстким потолком памяти")
    print(f"  механизм:        {mechanism}")
    print(f"  потолок:         {cap_mb:g} МБ на ВСЁ дерево прогона")
    print(f"  источник:        {cap_source}")
    print(f"  предел времени:  {seconds:g} с ({seconds_source})")
    print(f"  атрибуция:       {'выключена' if args.no_delta_guard else DELTA_GUARD_PLUGIN}")
    print(f"  корень прогона:  {args.root}")
    print(f"  команда:         {' '.join(command)}")
    sys.stdout.flush()


def _harden_streams() -> None:
    """Сделать вывод непадающим при любой кодировке консоли.

    Скрипт печатает по-русски, а кодировка stdout на Windows по умолчанию
    cp1251. Раньше это ничем не грызло ровно до тех пор, пока скрипт не
    запускали из чужого окружения: ``--help`` через ``argparse`` печатает
    русские строки описаний, и на cp1251 процесс падал с
    ``UnicodeEncodeError`` прямо во время разбора аргументов — то есть
    ещё до какого-либо прогона. Обход был внешний: ``PYTHONUTF8=1`` в
    вызывающей оболочке. Скрипт, который обязаны запускать и из pytest, и из
    CI, и из cron, не должен требовать настройки консоли вызывающего.

    Поэтому потоки переводятся в UTF-8 с ``errors="replace"``: непредставимый
    символ заменяется, а не роняет прогон. Замена вместо исключения выбрана
    сознательно — потеря одного символа в баннере не стоит трассировки,
    которая не даёт ни запустить прогон, ни прочитать его.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in ("utf8",):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # поток подменён — оставляем как есть
            pass


def main(argv: list[str] | None = None) -> int:
    _harden_streams()
    raw = list(sys.argv[1:] if argv is None else argv)
    own, pytest_args = _split_argv(raw)

    parser = argparse.ArgumentParser(
        prog="tools/run_tests.py",
        description="Прогон pytest под жёстким потолком памяти и пределом времени.",
        epilog="Аргументы pytest — после `--`, например: run_tests.py -- -m 'not live'",
    )
    parser.add_argument(
        "--memory-cap-mb",
        type=float,
        default=None,
        help=f"потолок памяти на дерево прогона, МБ (по умолчанию {DEFAULT_MEMORY_CAP_MB})",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help=f"предел времени прогона, с (по умолчанию {DEFAULT_MAX_SECONDS})",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="корень прогона: корень репозитория или mcp-platform",
    )
    parser.add_argument(
        "--no-delta-guard",
        action="store_true",
        help="не подключать слой атрибуции (tests._memguard)",
    )
    parser.add_argument(
        "--no-memory-cap",
        action="store_true",
        help=(
            "осознанно отказаться от потолка памяти. Оставлено явным флагом: "
            "молчаливый отказ от барьера означал бы, что его нет"
        ),
    )
    parser.add_argument(
        "--linux-as-multiplier",
        type=float,
        default=LINUX_AS_MULTIPLIER,
        help=(
            "множитель для RLIMIT_AS на Linux (потолок × множитель = лимит "
            "виртуальной памяти); см. докстринг про RSS против VSZ"
        ),
    )
    args = parser.parse_args(own)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[run_tests] корень прогона не существует: {root}", file=sys.stderr)
        return 4

    cap_mb, cap_source = _resolve(args.memory_cap_mb, ENV_MEMORY_CAP_MB, DEFAULT_MEMORY_CAP_MB)
    seconds, seconds_source = _resolve(args.max_seconds, ENV_MAX_SECONDS, DEFAULT_MAX_SECONDS)
    if cap_mb <= 0:
        print("[run_tests] потолок памяти должен быть положительным", file=sys.stderr)
        return 4

    # Слой атрибуции подключается здесь, а не только в инструкции: «барьер,
    # который надо не забыть включить», — это не барьер.
    argv_for_pytest = list(pytest_args)
    if not args.no_delta_guard:
        argv_for_pytest = _inject_delta_guard(argv_for_pytest)
    command = [sys.executable, "-m", "pytest", *argv_for_pytest]

    popen_kwargs: dict = {}
    job = None
    cgroup_group = None
    preexec = None

    if args.no_memory_cap:
        mechanism = "потолок НЕ включён (--no-memory-cap)"
    elif sys.platform == "win32":
        mechanism = (
            "Job Object, JOB_OBJECT_LIMIT_JOB_MEMORY — сумма по всему дереву "
            "процессов, KILL_ON_JOB_CLOSE снимает осиротевшие процессы"
        )
        # Задание готовится ДО запуска pytest (см. докстринг функции): иначе
        # ребёнок успевает перешагнуть потолок ещё до входа в задание, и ОС
        # обязана продолжить работать с уже сделанными выделениями.
        try:
            job = _windows_create_job(int(cap_mb * MB))
        except OSError as exc:
            print(f"[run_tests] НЕ УДАЛОСЬ поднять потолок памяти: {exc}", file=sys.stderr)
            return EXIT_NO_LIMIT
    else:
        cgroup_group = _linux_cgroup_prepare(int(cap_mb * MB))
        if cgroup_group is not None:
            mechanism = f"cgroup v2 memory.max ({cgroup_group}) — сумма по всему дереву"
        else:
            preexec = _linux_rlimit_as(int(cap_mb * MB), args.linux_as_multiplier)
            mechanism = (
                "RLIMIT_AS ×"
                f"{args.linux_as_multiplier:g} = {preexec.limit_bytes // MB} МБ "
                "(ограничение ВИРТУАЛЬНУЮ память, не RSS)"
            )

    _banner(args, cap_mb, cap_source, seconds, seconds_source, mechanism, command)

    if preexec is not None:
        popen_kwargs["preexec_fn"] = preexec

    proc = subprocess.Popen(command, cwd=str(root), **popen_kwargs)  # noqa: S603

    if job is not None:
        # Окно между порождением и входом в задание — единицы миллисекунд:
        # ctypes и нужные DLL к этому моменту уже загружены.
        try:
            _process_handle = _windows_assign_job(job, proc.pid)
        except OSError as exc:
            print(
                "[run_tests] НЕ УДАЛОСЬ ограничить память: "
                f"{exc}\n"
                "            Барьер без потолка — это не барьер. Либо запустите вне\n"
                "            ограничивающего окружения, либо явно согласитесь:\n"
                "                python tools/run_tests.py --no-memory-cap -- ...",
                file=sys.stderr,
            )
            try:
                proc.kill()
            except OSError:
                pass
            return EXIT_NO_LIMIT
    elif cgroup_group is not None:
        try:
            (cgroup_group / "cgroup.procs").write_text(str(proc.pid))
        except OSError:
            pass

    watchdog = _Watchdog(proc, job, int(cap_mb * MB), seconds)
    watchdog.start()
    started = time.monotonic()
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        # KILL_ON_JOB_CLOSE добьёт дерево при выходе из функции; сторож
        # нужно остановить, чтобы он не стрелял в уже мёртвое задание.
        watchdog.stop()
        raise
    finally:
        watchdog.stop()

    elapsed = time.monotonic() - started
    print(f"[run_tests] прогон завершён за {elapsed:.1f} с, код pytest: {rc}")

    if watchdog.memory_hit:
        if watchdog.peak_bytes:
            detail = (
                f"пик {watchdog.peak_bytes / MB:.1f} МБ при потолке {cap_mb:g} МБ, "
                f"процессов в дереве: {watchdog.active_processes or 'н/д'}"
            )
        else:
            detail = f"потолок {cap_mb:g} МБ упёрся в отказ выделения на стороне ОС"
        print(
            f"[run_tests] СРАБОТАЛ ПОТОЛОК ПАМЯТИ: {detail}\n"
            "            Дерево процессов прогона снято. Ни один тест не имеет\n"
            "            права столько съедать: сузьте прогон (-x) — виновник\n"
            "            печатается в выводе pytest.",
            file=sys.stderr,
        )
        return EXIT_MEMORY_CAP
    if watchdog.timeout_hit:
        print(
            f"[run_tests] ПРЕВЫШЕН ПРЕДЕЛ ВРЕМЕНИ ПРОГОНА ({seconds:g} с). "
            "Дерево процессов снято.",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
