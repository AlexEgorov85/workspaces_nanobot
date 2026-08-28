"""ActiveFilesHook — side-channel для файлов активной сессии.

Проблема (см. инцидент 2026-08-27):
  Consolidator в nanobot архивирует старые сообщения через LLM-сжатие,
  ориентируясь на шаблон ``agent/consolidator_archive.md``. Шаблон просит
  LLM выделять только SNIP-факты (Signal / Novel / Important / Persistent),
  и «пользователь прислал PDF» LLM считает одноразовым запросом — выдаёт
  ``(nothing)``. После архивации в system prompt остаётся только
  ``[Archived Context Summary]\n\n(nothing)``, и LLM не знает, что в этой
  сессии пользователь прикладывал файлы.

  Это делает невозможной работу с файлами через несколько оборотов:
  в первом обороте LLM видит ``[File: foo.pdf]`` + текст, успешно делает
  саммари, во втором — текст уже в архиве, в третьем — даже упоминание
  о файле потеряно.

Решение:
  Side-channel через ``session.metadata``. Каждое прикладывание файла
  пользователем (msg.media) и каждый созданный агентом файл (write_file)
  записываются в ``session.metadata['user_attachments']`` /
  ``session.metadata['agent_files']``. Записи персилисты через сессию
  (database) и не подвержены Consolidator'у — это просто ключ в metadata.

  RuntimePatcher патчит ``ContextBuilder.build_system_prompt``, чтобы
  добавить в system prompt блок ``# Active session files`` со списком
  всех доступных файлов с полными путями. LLM в любом обороте знает:
    - какие файлы пользователь приложил в этой сессии;
    - какие файлы агент создал за сессию;
  и может в любой момент прочитать их через ``read_file(path)`` или
  запустить ``legal_summarizer/cli.py --file <path>``.

Ограничения:
  * Сам **текст** PDF / DOCX всё равно может быть слишком большим для
    контекста — мы не решаем эту проблему, мы только даём LLM **путь**.
  * Хук не пытается «вернуть» исходный user-message в контекст.
    Это задача Consilidator'а, и у нас нет способа обойти его,
    не ломая совместимость с nanobot 0.3.0.
  * RecentFilesHook (write/edit) продолжает работать как раньше —
    side-channel только **расширяет** его персистентностью через metadata.
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable

from loguru import logger
from nanobot.agent import AgentHook

# Ключи в session.metadata.
_USER_ATTACHMENTS_KEY = "user_attachments"
_AGENT_FILES_KEY = "agent_files"

# Жёсткий лимит записей в side-channel (защита от неконтролируемого
# роста в долгих сессиях). Если больше — старые записи вытесняются.
_MAX_USER_ATTACHMENTS = 20
_MAX_AGENT_FILES = 50


class ActiveFilesHook(AgentHook):
    """Side-channel для файлов активной сессии в ``session.metadata``.

    Изолирует состояние по ``session_key``:
    * user-attachments — из ``msg.media`` через ``before_user_turn``;
    * agent-files — из файловых тулов через ``after_execute_tool``.

    Персистится через наш ``PGSessionManager`` (патч
    ``patch_active_files_in_context``), отдельный от штатных last_consolidated
    Consolidator'а — то есть side-channel живёт **всегда**, пока сессия
    не удалена.
    """

    def __init__(
        self,
        sessions: Any | None = None,
        workspace_dir: Any | None = None,
    ) -> None:
        super().__init__()
        # ``workspace_dir`` — часть единого контракта плагинов
        # ``workspace/hooks/*.py`` (``cls(workspace_dir=...)`` из
        # ``lib/cli/hook_loader.py``). Принимаем его, чтобы хук
        # инстанцировался сканером; сам по себе он здесь не нужен.
        self._workspace_dir = workspace_dir
        # Сохраняем ссылку на SessionManager, чтобы хук мог persist'ить
        # ``session.metadata`` после записи. Если None — записываем только
        # in-memory (для тестов / диагностики). Реальный SessionManager
        # подставляется ``ApplicationContext`` после загрузки плагинов.
        self._sessions = sessions

    # ------------------------------------------------------------------
    # user attachments (msg.media на user-turn)
    # ------------------------------------------------------------------

    async def before_user_turn(
        self, context: Any, message: Any,
    ) -> None:
        """Сохранить пути из ``msg.media`` в ``session.metadata``.

        ``context.session`` обязан быть доступен (это гарантирует AgentLoop
        к моменту вызова ``before_user_turn``). Медиа-пути — это уже
        резолвленные пути после ``SessionFileRedirectHook``.
        """
        session = getattr(context, "session", None)
        if session is None:
            return
        media = getattr(message, "media", None)
        if not media:
            return
        paths = [p for p in media if isinstance(p, str) and p]
        if not paths:
            return
        self._record_user_attachments(session, paths, message=message)
        if self._sessions is not None:
            try:
                self._sessions.save(session)
            except Exception as exc:
                logger.warning(
                    "ActiveFilesHook: failed to save session after user attachment: {}",
                    exc,
                )

    def _record_user_attachments(
        self, session: Any, paths: Iterable[str], *, message: Any,
    ) -> None:
        md = session.metadata if isinstance(session.metadata, dict) else {}
        existing = md.get(_USER_ATTACHMENTS_KEY)
        if not isinstance(existing, list):
            existing = []
        existing_paths = {
            entry.get("path") for entry in existing
            if isinstance(entry, dict)
        }
        # Дедуп: один user-message может содержать несколько media,
        # но мы хотим хранить только уникальные пути. Сначала
        # дедуплицируем входящий список, потом сравниваем с existing.
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            if path in existing_paths:
                continue
            existing_paths.add(path)
            existing.append({
                "path": path,
                "name": _basename(path),
                "size_bytes": _safe_size(path),
                "received_at": _now_iso(),
                "sender_id": getattr(message, "sender_id", None),
                "channel": getattr(message, "channel", None),
            })
        # FIFO trim.
        if len(existing) > _MAX_USER_ATTACHMENTS:
            existing = existing[-_MAX_USER_ATTACHMENTS:]
        md[_USER_ATTACHMENTS_KEY] = existing
        session.metadata = md

    # ------------------------------------------------------------------
    # agent files (write_file / edit через after_execute_tool)
    # ------------------------------------------------------------------

    _FILE_TOOLS: ClassVar[frozenset[str]] = frozenset({
        "write", "edit", "create_file", "write_file",
    })
    _PATH_KEYS: ClassVar[tuple[str, ...]] = (
        "path", "filePath", "file_path", "filepath",
    )

    async def after_execute_tool(
        self, context: Any, tool_call: Any, tool: Any,
        params: Any, result: Any,
    ) -> None:
        session = getattr(context, "session", None)
        if session is None:
            return
        name = (
            getattr(tool_call, "name", None)
            or getattr(tool_call, "tool_name", None)
        )
        if not name or name not in self._FILE_TOOLS:
            return
        path = _extract_path(params, self._PATH_KEYS)
        if not path:
            return
        self._record_agent_file(session, path)
        if self._sessions is not None:
            try:
                self._sessions.save(session)
            except Exception as exc:
                logger.warning(
                    "ActiveFilesHook: failed to save session after agent file: {}",
                    exc,
                )

    def _record_agent_file(self, session: Any, path: str) -> None:
        md = session.metadata if isinstance(session.metadata, dict) else {}
        existing = md.get(_AGENT_FILES_KEY)
        if not isinstance(existing, list):
            existing = []
        existing_paths = {
            entry.get("path") for entry in existing
            if isinstance(entry, dict)
        }
        if path in existing_paths:
            return
        existing.append({
            "path": path,
            "name": _basename(path),
            "size_bytes": _safe_size(path),
            "written_at": _now_iso(),
        })
        if len(existing) > _MAX_AGENT_FILES:
            existing = existing[-_MAX_AGENT_FILES:]
        md[_AGENT_FILES_KEY] = existing
        session.metadata = md

    # ------------------------------------------------------------------
    # helper API — список файлов для system prompt
    # ------------------------------------------------------------------

    def list_active_files(self, session: Any) -> dict[str, list[dict[str, Any]]]:
        """Вернуть user-attachments и agent-files для system prompt.

        Возвращает ``{"user_attachments": [...], "agent_files": [...]}``;
        оба списка могут быть пустыми. Пути фильтруются по
        ``Path.is_file()`` — удалённые файлы отбрасываются (агент не должен
        видеть фантомные ссылки).
        """
        if session is None:
            return {"user_attachments": [], "agent_files": []}
        md = session.metadata if isinstance(session.metadata, dict) else {}
        return {
            "user_attachments": [
                entry for entry in (md.get(_USER_ATTACHMENTS_KEY) or [])
                if isinstance(entry, dict) and _is_existing_file(entry.get("path"))
            ],
            "agent_files": [
                entry for entry in (md.get(_AGENT_FILES_KEY) or [])
                if isinstance(entry, dict) and _is_existing_file(entry.get("path"))
            ],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _basename(path: str) -> str:
    from pathlib import PurePosixPath
    return PurePosixPath(path.replace("\\", "/")).name


def _safe_size(path: str) -> int | None:
    try:
        from pathlib import Path
        return Path(path).stat().st_size
    except OSError:
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _is_existing_file(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    from pathlib import Path
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _extract_path(params: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(params, dict):
        for key in keys:
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def render_active_files_section(
    active: dict[str, list[dict[str, Any]]],
    *,
    max_chars: int = 4000,
) -> str:
    """Формирует Markdown-секцию ``# Active session files``.

    Используется ``RuntimePatcher.patch_active_files_in_context``.
    Если оба списка пусты — возвращает пустую строку (блок не добавляется).
    Длина ограничена ``max_chars`` (защита от раздувания system prompt при
    тысячах записей; обычно не срабатывает — лимиты ``_MAX_*`` уже
    ограничивают количество).

    Секция содержит явные инструкции и готовые команды — чтобы LLM не
    пытался сам читать PDF/DOCX через ``extract_text`` (риск цикла
    «прочитал → LLM-сжало → архивировало → снова прочитал», инцидент
    2026-08-27 с ГК РФ часть 1 на 60K токенов).
    """
    user = active.get("user_attachments") or []
    agent = active.get("agent_files") or []
    if not user and not agent:
        return ""

    lines: list[str] = [
        "# Active session files",
        "",
        "Файлы этой сессии. Список **персилисты** в ``session.metadata`` "
        "(не подвержен Consolidator'у, в отличие от обычной истории чата).",
        "",
        "## Как работать с этими файлами",
        "",
        "- **Документы (PDF/DOCX/TXT)** — НЕ читай через ``extract_text()`` "
        "или ``read_file()``: они огромные (десятки тысяч символов) и "
        "забьют контекст. Используй **только** CLI соответствующего skill:",
        "  - Юридические документы → ``python skills/legal_summarizer/scripts/cli.py --file <path> --length brief|medium|detailed`` (вернёт JSON с subject+summary).",
        "- **Маленькие текстовые файлы** (<10K символов) — можно "
        "``read_file(path)`` для предпросмотра.",
        "- **Аудио/видео/картинки** — перешли пользователю через ``message({'media': [...]})``.",
        "- **Файлы, созданные агентом** (ниже в «agent_files») — "
        "обычные пути на диске; можно ``read_file(path)`` или "
        "приложить к ответу через ``message({'media': [...]})``.",
        "",
    ]
    if user:
        lines.append("## Приложены пользователем")
        lines.append("")
        for entry in user:
            lines.append(_format_entry(entry, "user"))
        lines.append("")
    if agent:
        lines.append("## Созданы агентом")
        lines.append("")
        for entry in agent:
            lines.append(_format_entry(entry, "agent"))
        lines.append("")

    text = "\n".join(lines)
    if len(text) > max_chars:
        # Жёстко обрезаем с предупреждением.
        text = text[:max_chars] + "\n\n_(truncated — список слишком длинный)_"
    return text


def _format_entry(entry: dict[str, Any], kind: str = "user") -> str:
    name = entry.get("name") or entry.get("path") or "?"
    path = entry.get("path") or "?"
    size = entry.get("size_bytes")
    size_str = f" ({_human_size(size)})" if isinstance(size, int) else ""
    when = entry.get("received_at") or entry.get("written_at") or ""
    when_str = f" — {when}" if when else ""
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    is_document = ext in {"pdf", "docx", "doc", "txt", "md", "rtf"}
    extra = ""
    if kind == "user" and is_document:
        # Готовая команда для саммари: минимизирует thinking-LLM о выборе.
        # Используем single-quote escape для путей с апострофами.
        safe_path = path.replace("'", "'\\''")
        extra = (
            f"\n  - Саммари: ``python skills/legal_summarizer/scripts/cli.py "
            f"--file '{safe_path}' --length brief``"
        )
    return f"- `{path}` — **{name}**{size_str}{when_str}{extra}"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"