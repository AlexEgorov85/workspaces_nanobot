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
from typing import List, Optional, Tuple


class SubprocessManager:
    """Управление фоновыми процессами (Streamlit UI; добавляются через ``spawn_*``).

    Attributes:
        _log_dir: директория для логов (создаётся при первом ``spawn_*``).
        _processes: список ``(Popen, file_handle)`` — для terminate в
            ``terminate_all()`` и закрытия file-handle.
    """

    def __init__(self, log_dir: Optional[Path] = None) -> None:
        self._log_dir = log_dir or Path.cwd() / "logs"
        self._processes: List[Tuple[subprocess.Popen, Optional[object]]] = []

    # ------------------------------------------------------------------
    # Streamlit
    # ------------------------------------------------------------------

    def spawn_streamlit(self, script_path: Path, port: int = 8501) -> bool:
        """Запустить Streamlit UI как subprocess.

        Args:
            script_path: путь к ``streamlit_app.py``.
            port: порт (по умолчанию 8501 — стандартный для Streamlit).

        Returns:
            ``True`` если процесс запущен, ``False`` если скрипт не
            найден или запуск упал. Возврат ``False`` НЕ ломает
            вызывающего (gateway продолжает работу без UI).

        Notes:
            Использует ``--server.headless true`` (без автооткрытия
            браузера — иначе в server-окружениях будет ошибка).
            stdout+stderr редиректятся в ``<log_dir>/streamlit.log`` —
            иначе вывод теряется при завершении родителя.
        """
        script = Path(script_path)
        if not script.exists():
            return False

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_handle = open(self._log_dir / "streamlit.log", "a", encoding="utf-8")
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

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------

    def terminate_all(self, timeout_sec: float = 5.0) -> None:
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
                proc.wait(timeout=timeout_sec)
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
