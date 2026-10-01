"""Файлы сессии: безопасные пути, рабочий каталог сессии, вложения.

Назначение подсистемы одно — дать платформе место для артефактов вызова и не
дать ей писать куда попало. Поэтому безопасный путь проверяется один раз и в
одном месте (`security.safe_child`), а всё, что пишет на диск, ходит через
`SessionWorkspace`.
"""

from .artifact_store import Artifact, ArtifactError, ArtifactStore
from .security import MAX_NAME_LENGTH, PathDeniedError, safe_child, safe_name
from .workspace import SESSION_SUBDIRS, SessionHandle, SessionWorkspace

__all__ = [
    "MAX_NAME_LENGTH",
    "SESSION_SUBDIRS",
    "Artifact",
    "ArtifactError",
    "ArtifactStore",
    "PathDeniedError",
    "SessionHandle",
    "SessionWorkspace",
    "safe_child",
    "safe_name",
]
