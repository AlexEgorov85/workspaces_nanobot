"""AutoAttachHook — автоматически прикрепляет свежесозданные файлы к ответу.

Зачем: бот должен вызывать ``message(content, media=[path])`` для отправки
локальных файлов в чат, но LLMs часто забывают это делать и вместо этого
описывают файл в тексте. Этот хук страхует:

  1. ``before_execute_tool`` запоминает, какие файлы инструменты
     ``write_file`` / ``edit_file`` / ``apply_patch`` / ``exec`` собираются
     создать или изменить (мы читаем абсолютный путь из ``params``).
  2. ``after_execute_tool`` проверяет, что файл реально появился (или
     изменился) на диске — и добавляет его в per-session bucket.
  3. ``RuntimePatcher.patch_assemble_outbound`` (см.
     ``lib/services/runtime_patcher.py``) при сборке финального
     ``OutboundMessage`` достаёт накопленные пути из
     ``AutoAttachRegistry.drain(session_key)`` и добавляет их в
     ``result.media`` — но только если бот сам их явно не прикрепил.

Per-turn хуки пишут в общий ``AutoAttachRegistry`` (class-level),
разделённый по ``session_key`` — race conditions между конкурентными
оборотами невозможны, потому что ключ — это ``session_key``, уникальный
внутри канала.

Кросс-платформенность:
  * Пути нормализуются через ``pathlib.PurePath`` — на Linux это
    ``PurePosixPath``, на Windows — ``PureWindowsPath``. Внутри хука
    сравниваем нормализованные пути, поэтому ``/a/b`` и ``/a/./b``
    эквивалентны в любой ОС.
  * Размер файла проверяется через ``stat().st_size`` — одинаково работает
    на Linux и Windows.
  * Каталог кеша ``data_store/cache/`` ищется относительно
    ``context.session_key`` (``sessions/<session_key>/``) и в плоском виде
    (``data_store/cache/<file>``) — оба случая легитимны.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePath
from typing import Any, ClassVar, Dict, Optional, Set, Tuple

from nanobot.agent import AgentHook

logger = logging.getLogger(__name__)


# File-write tools, по имени tool_call'а (как приходит в nanobot 0.3.0+).
_FILE_TOOL_NAMES: ClassVar[frozenset[str]] = frozenset({
    "write",
    "write_file",
    "create_file",
    "edit",
    "edit_file",
    "apply_patch",
    "exec",
    "execute_command",
    "shell",
})


# Ключи параметра "путь" в разных инструментах.
_PATH_KEYS: ClassVar[Tuple[str, ...]] = (
    "path",
    "filePath",
    "file_path",
    "filepath",
    "target",
    "file",
    "destination",
)


class AutoAttachRegistry:
    """Глобальный реестр свежесозданных файлов, разделённый по ``session_key``.

    Per-turn хуки просят ``registry.record(session_key, path)``,
    а ``RuntimePatcher.patch_assemble_outbound`` делает
    ``registry.drain(session_key)`` и получает список путей. ``drain``
    атомарно возвращает и очищает bucket — повторный ``drain`` отдаёт
    пустой список (защита от двойного применения).
    """

    _fresh: Dict[str, Set[PurePath]] = {}
    _sizes: Dict[str, Dict[PurePath, int]] = {}

    @classmethod
    def reset(cls, session_key: Optional[str] = None) -> None:
        """Сбросить bucket (вызывать в начале оборота)."""
        key = session_key if isinstance(session_key, str) else ""
        cls._fresh.pop(key, None)
        cls._sizes.pop(key, None)

    @classmethod
    def record_pending(
        cls, session_key: str, path: PurePath, size_before: int
    ) -> None:
        """Запомнить, что бот собирается создать/изменить ``path``."""
        bucket = cls._fresh.setdefault(session_key, set())
        bucket.add(path)
        sizes = cls._sizes.setdefault(session_key, {})
        sizes[path] = size_before

    @classmethod
    def confirm(
        cls,
        session_key: str,
        path: PurePath,
        *,
        require_size_change: bool,
    ) -> bool:
        """Подтвердить, что ``path`` существует (или изменился).

        Возвращает ``True``, если файл остаётся в bucket'е, и ``False``
        если его надо убрать (не появился / не менялся).

        ``PurePath`` нормализует кросс-платформенные пути, но для
        операций файловой системы (``exists()``, ``stat()``) требуется
        конкретный ``Path`` — иначе вылетит ``AttributeError``.
        """
        real = Path(str(path))
        try:
            exists = real.exists()
        except OSError:
            exists = False
        if not exists:
            return False
        if require_size_change:
            new_size = real.stat().st_size
            old_size = cls._sizes.get(session_key, {}).get(path, -1)
            if old_size == new_size:
                return False
        return True

    @classmethod
    def prune(cls, session_key: str, keep: Set[PurePath]) -> None:
        """Оставить в bucket'е только ``keep``."""
        if keep:
            cls._fresh[session_key] = keep
        else:
            cls._fresh.pop(session_key, None)

    @classmethod
    def drain(cls, session_key: Optional[str] = None) -> list[str]:
        """Атомарно достать и очистить пути для ``session_key``.

        Возвращает список **строковых** абсолютных путей, готовых для
        ``OutboundMessage.media``. Детерминированный порядок (sorted).
        """
        key = session_key if isinstance(session_key, str) else ""
        bucket = cls._fresh.pop(key, None) or set()
        cls._sizes.pop(key, None)
        return sorted(str(p) for p in bucket)


def make_auto_attach_hook_factory() -> Any:
    """Фабрика оборота: на каждый turn — свежий ``AutoAttachHook``.

    Per-turn хуки безопасны при конкурентной обработке: общее состояние
    изолировано по ``session_key`` через ``AutoAttachRegistry``.
    """

    def _factory(turn_context: Any) -> "AutoAttachHook":
        session_key = getattr(turn_context, "session_key", None) or None
        AutoAttachRegistry.reset(session_key)
        return AutoAttachHook(session_key=session_key)

    return _factory


class AutoAttachHook(AgentHook):
    """Per-turn agent hook — фиксирует пути, в которые пишет агент.

    Используется внутри ``make_auto_attach_hook_factory``; наружу
    предпочитайте фабрику. Экземпляр читает/пишет в
    ``AutoAttachRegistry`` по своему ``session_key``.
    """

    def __init__(
        self,
        workspace_dir: Optional[str] = None,
        session_key: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._workspace = (
            Path(workspace_dir).expanduser() if workspace_dir else Path.cwd()
        ).resolve()
        self._session_key = session_key or ""

    # ------------------------------------------------------------------
    # AgentHook
    # ------------------------------------------------------------------

    async def before_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        """Запомнить целевой путь, если инструмент — файловый writer."""
        name = self._tool_name(tool_call)
        if name not in _FILE_TOOL_NAMES:
            return
        if not isinstance(params, dict):
            return
        target = self._extract_path(params)
        if target is None:
            return
        resolved = self._resolve(target)
        # ``PurePath`` нормализует кросс-платформенные пути, но для
        # операций с FS (``exists()``, ``stat()``) нужен конкретный
        # ``Path`` — иначе выскочит ``AttributeError``.
        real = Path(str(resolved))
        try:
            size_before = real.stat().st_size if real.exists() else -1
        except OSError:
            size_before = -1
        AutoAttachRegistry.record_pending(
            self._session_key, resolved, size_before,
        )

    async def after_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        """Подтвердить, что файл появился (или изменился)."""
        name = self._tool_name(tool_call)
        if name not in _FILE_TOOL_NAMES:
            return
        # Для exec'а требуем изменения; для write/edit — только наличие.
        require_change = name in {"exec", "execute_command", "shell"}
        # Проходим копию — будем удалять «отвалившиеся» пути.
        current: Set[PurePath] = set()
        registry_dict = AutoAttachRegistry._fresh.get(self._session_key) or set()
        for path in list(registry_dict):
            if AutoAttachRegistry.confirm(
                self._session_key, path, require_size_change=require_change,
            ):
                current.add(path)
        AutoAttachRegistry.prune(self._session_key, current)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None)
        return str(name) if name else ""

    @staticmethod
    def _extract_path(params: dict) -> Optional[str]:
        for key in _PATH_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _resolve(self, target: str) -> PurePath:
        """Привести путь к нормализованному абсолютному виду.

        На Linux: ``PurePosixPath``-like. На Windows: ``PureWindowsPath``.
        ``PurePath`` нормализует относительные сегменты и убирает
        ``./``, ``../``; сравнение нормализованных путей —
        платформо-независимо.
        """
        if os.path.isabs(target):
            return PurePath(target)
        return PurePath(self._workspace / target)


__all__ = [
    "AutoAttachHook",
    "AutoAttachRegistry",
    "make_auto_attach_hook_factory",
]
