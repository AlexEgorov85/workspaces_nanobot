"""RecentFilesHook — отслеживание файлов, которые агент создал за оборот.

Задача: при формировании ``OutboundMessage.media`` автоматически прикладывать
пути ко всем файлам, которые агент только что записал через ``write_file``
(и другие файловые инструменты) — независимо от того, указал ли их модель
явно в ``media``. Это закрывает три проблемы:

  1. Модель забыла приложить созданный файл (на скрине: ``.html`` после
     ``write_file`` создан, но в ``message({"media": []})`` не приложен).
  2. Модель приложила несуществующий путь (``test.docx``, который не
     создался из-за блокировки ``pip install``) — этот путь отбрасываем
     через ``Path.is_file()``.
  3. Модель приложила нереальный абсолютный путь типа
     ``/home/<user>/<project>/workspace/test/test.md`` — этот путь мы
     берём ПОСЛЕ ``SessionFileRedirectHook``, т.е. уже перенаправленный
     в ``data_store/cache/sessions/<session_key>/...``.

Архитектура:

  * ``after_execute_tool`` ловит ВСЕ вызовы файловых инструментов и
    сохраняет ``params["path"]`` (это уже финальный путь после редиректа).
  * ``drain(session_key)`` возвращает и обнуляет записи bucket'а
    конкретной сессии (конкурентность изолирована).
  * В ``RuntimePatcher._wrap`` после ``tool_audit_hook.drain`` мы берём
    ``recent_files_hook.drain(...)`` и подмешиваем в ``result.media``
    только те пути, которых там ещё нет (по basename) и что реально
    существуют на диске.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Optional

from nanobot.agent import AgentHook


# Полный список файловых инструментов, которые могут оставлять файлы
# в ``data_store/cache/sessions/<key>/``*. Любой из них после нашего
# хука должен пройти через ``SessionFileRedirectHook`` (см.
# ``workspace/hooks/session_file_redirect_hook.py``), поэтому мы берём
# финальный путь из ``params["path"]``.
_FILE_TOOLS: ClassVar[frozenset[str]] = frozenset({
    "write", "edit", "create_file", "write_file",
})

# Ключи в ``params``, которые могут содержать путь к файлу.
_PATH_KEYS: ClassVar[tuple[str, ...]] = (
    "path", "filePath", "file_path", "filepath",
)


class RecentFilesHook(AgentHook):
    """Собирает пути к файлам, которые агент создал/изменил за оборот.

    Изолирует состояние по ``session_key``: разные вопросы (конкурентные
    сессии) не «путают» файлы. Используется в ``RuntimePatcher._wrap``
    после ``tool_audit_hook.drain`` для автоприкладывания в
    ``OutboundMessage.media``.
    """

    def __init__(self, workspace_dir: "str | None" = None) -> None:
        # ``workspace_dir`` принимается для совместимости с единым контрактом
        # ``hook_loader.scan_and_register``; ``RecentFilesHook`` хранит пути
        # из ``params["path"]`` после ``SessionFileRedirectHook`` — путь уже
        # абсолютный, workspace_dir не нужен.
        super().__init__()
        self._paths: dict[str, list[str]] = {}

    @staticmethod
    def _bucket_key(ctx: Any) -> str:
        """Вернуть ``session_key`` из контекста (``""`` если его нет/не строка)."""
        key = getattr(ctx, "session_key", None)
        return key if isinstance(key, str) else ""

    @staticmethod
    def _extract_path(params: Any) -> Optional[str]:
        """Извлечь путь из ``params`` (поддержка dict- и kwargs-вариантов)."""
        if isinstance(params, dict):
            for key in _PATH_KEYS:
                value = params.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    async def after_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        """Записать финальный путь файла, если инструмент файловый.

        ``params["path"]`` на этом этапе уже отредактирован
        ``SessionFileRedirectHook`` (если он зарегистрирован раньше
        RecentFilesHook в ``AgentLoop.hooks``), поэтому здесь мы получаем
        реальный путь, по которому файл был записан — независимо от того,
        что прислала LLM.
        """
        name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None)
        if not name or name not in _FILE_TOOLS:
            return
        path = self._extract_path(params)
        if not path:
            return
        key = self._bucket_key(context)
        if not key:
            return
        self._paths.setdefault(key, []).append(path)

    def drain(self, session_key: Optional[str] = None) -> list[str]:
        """Вернуть и обнулить список путей текущей сессии.

        Порядок путей сохраняется (в порядке их создания агентом).
        Дубликаты не отбрасываются — фильтрация делается вызывающей
        стороной (``RuntimePatcher``), которая сравнивает по ``Path(p).name``
        и проверяет ``Path(p).is_file()``.
        """
        key = session_key if isinstance(session_key, str) else ""
        return self._paths.pop(key, [])

    def collected(self, session_key: Optional[str] = None) -> list[str]:
        """Снимок путей без обнуления (для тестов/диагностики)."""
        key = session_key if isinstance(session_key, str) else ""
        return list(self._paths.get(key, []))