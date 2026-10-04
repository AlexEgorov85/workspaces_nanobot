"""Фоновое зеркалирование локальных источников в холодное хранилище.

Механизм: ``MirrorPoller`` (``mirror_poller.py``) — цикл, дайджест, уборка,
отказы, журнал, метрики. Ресурс: ``SessionMirror`` (``session_mirror.py``) —
зеркало сессий JSONL.

Новый ресурс — это наследник ``MirrorPoller``, а не копия сервиса. Копия
разошлась бы с оригиналом при первой же правке отката или уборки, и расхождение
было бы не видно нигде.
"""

from __future__ import annotations

from lib.gateway.mirror.mirror_poller import (
    SERVICE_SESSION_PREFIX,
    SERVICE_USER,
    VERDICTS_WITHOUT_WRITE,
    MirrorEntry,
    MirrorPoller,
    default_replica_id,
)
from lib.gateway.mirror.session_mirror import (
    MIRROR_OPERATIONS,
    OP_CLEANUP,
    OP_MIRROR,
    OP_STATE,
    SessionMirror,
    file_digest,
)

__all__ = [
    "MIRROR_OPERATIONS",
    "OP_CLEANUP",
    "OP_MIRROR",
    "OP_STATE",
    "SERVICE_SESSION_PREFIX",
    "SERVICE_USER",
    "VERDICTS_WITHOUT_WRITE",
    "MirrorEntry",
    "MirrorPoller",
    "SessionMirror",
    "default_replica_id",
    "file_digest",
]
