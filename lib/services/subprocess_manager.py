"""SubprocessManager — запуск и корректное завершение дочерних процессов.

Перенесено из gateway.py (запуск Streamlit UI на :8501 и его остановка).
Сейчас в нём только Streamlit; добавление новых фоновых процессов —
через дополнительные ``spawn_*`` методы.

Ключевые решения:
  * Логи Streamlit пишутся в ``logs/streamlit.log`` (append) — чтобы
    вывод не терялся при падении UI и был доступен для диагностики;
  * Streamlit запускается с ``--server.headless true`` — без открытия
    браузера при старте (это нужно в server-окружениях);
  * Если скрипт не найден или запуск упал — это НЕ роняет вызывающего
    (только ``return False``); gateway продолжает работу без UI;
  * Graceful shutdown: ``terminate()`` → ``wait(timeout)`` → ``kill()``
    если процесс не завершился за таймаут. Это гарантирует, что при
    ``Ctrl+C`` Streamlit корректно закрывает свои подпроцессы
    (websocket-серверы) до того как родитель убьёт его.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from config import get_setting


class SubprocessManager:
    """Управление фоновыми процессами (Streamlit UI; добавляются через ``spawn_*``).
    Attributes:
        _log_dir: директория для логов (создаётся при первом ``spawn_*``).
        _processes: список ``(Popen, file_handle)`` — для terminate в
            ``terminate_all()`` и закрытия file-handle.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir or Path.cwd() / "logs"
        self._default_port: int = int(get_setting("gateway", "streamlit_port", default=8501))
        self._log_filename: str = str(
            get_setting("gateway", "streamlit_log_filename", default="streamlit.log")
        )
        self._shutdown_timeout: float = float(
            get_setting("gateway", "subprocess_shutdown_timeout_sec", default=5.0)
        )
        self._processes: list[tuple[subprocess.Popen, object | None]] = []

    # ------------------------------------------------------------------
    # Streamlit
    # ------------------------------------------------------------------

    def spawn_streamlit(self, script_path: Path, port: int | None = None) -> bool:
        """Запустить Streamlit UI как subprocess.

        Args:
            script_path: путь к ``streamlit_app.py``.
            port: порт (по умолчанию берётся из
                ``gateway.streamlit_port`` в project.json, иначе 8501).

        Returns:
            ``True`` если процесс запущен, ``False`` если скрипт не
            найден или запуск упал. Возврат ``False`` НЕ ломает
            вызывающего (gateway продолжает работу без UI).

        Notes:
            Использует ``--server.headless true`` (без автооткрытия
            браузера — иначе в server-окружениях будет ошибка).
            stdout+stderr редиректятся в ``<log_dir>/<streamlit_log_filename>`` —
            иначе вывод теряется при завершении родителя.
        """
        script = Path(script_path)
        if not script.exists():
            return False
        port = int(port) if port else self._default_port

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_handle = open(self._log_dir / self._log_filename, "a", encoding="utf-8")
        except OSError:
            log_handle = None

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", str(script),
                 "--server.headless", "true",
                 "--server.port", str(port)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            if log_handle is not None:
                log_handle.close()
            return False

        self._processes.append((proc, log_handle))
        return True

    def __enter__(self) -> SubprocessManager:
        return self

    def __exit__(self, *exc) -> None:
        self.terminate_all()

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------

    def terminate_all(self, timeout_sec: float | None = None) -> None:
        """Корректно завершить все subprocess'ы.

        Алгоритм для каждого процесса:
          1. ``proc.terminate()`` — послать SIGTERM (graceful);
          2. ``proc.wait(timeout=timeout_sec)`` — дождаться;
          3. Если процесс не завершился за таймаут — ``proc.kill()`` (SIGKILL).

        После — закрыть log-handle. Исключения каждого шага глотаются
        (иначе один зависший процесс мог бы оставить остальные
        subprocess'ы живыми).
        """
        for proc, handle in self._processes:
            try:
                proc.terminate()
                proc.wait(timeout=timeout_sec if timeout_sec is not None else self._shutdown_timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._processes.clear()
