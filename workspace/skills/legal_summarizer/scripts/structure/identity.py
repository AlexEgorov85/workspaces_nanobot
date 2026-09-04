"""DocumentIdentity — единый идентификатор документа (PLAN §5, §77).

Заменяет **две параллельные** реализации fingerprint:

* ``scripts/fingerprint.py::compute_fingerprint`` (sha256 от path/size/mtime).
* ``scripts/structure/physical.py::_physical_cache_key`` (тот же алгоритм
  в другой обёртке).

С введением ``DocumentIdentity`` все downstream-компоненты
(``PhysicalDocument``, ``DocumentStructure``, ``manifest``, ``document_cache``,
``retrieval``, ``semantic analysis``) получают **один** объект, который
передаётся вниз по pipeline. Fingerprint считается один раз и
используется всеми.

Не делает НИЧЕГО, кроме:

1. Считает fingerprint от ``(resolved_path, size, mtime)``.
2. Хранит ``document_id`` (== fingerprint[:12]) и ``physical_cache_key``
   (== fingerprint).
3. Проверяет freshness: ``is_fresh(path)`` — сравнивает ``(size, mtime)``
   с закэшированным.

Вся остальная информация (title, blocks, structure) — ответственность
``PhysicalDocument`` / ``DocumentStructure``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DocumentIdentity:
    """Единый идентификатор документа (PLAN §5).

    Attributes:
        document_id: короткий ID (первые 12 hex fingerprint'а) для
            логирования и manifest.
        fingerprint: полный sha256 hex от ``(resolved_path, size, mtime)``.
        physical_cache_key: == fingerprint (для обратной совместимости
            с ``_physical_cache_key`` из ``physical.py``).
        resolved_path: абсолютный путь к файлу.
        size_bytes: размер файла при создании identity.
        mtime_ns: ``stat().st_mtime_ns`` при создании identity.
    """

    document_id: str
    fingerprint: str
    physical_cache_key: str
    resolved_path: str
    size_bytes: int
    mtime_ns: int

    def is_fresh(self, path: str | Path) -> bool:
        """``True`` если ``(size, mtime)`` совпадают с закэшированными.

        Используется при cache lookup: если ``is_fresh`` == ``False``,
        кэш нужно пересчитать (PLAN §77).
        """
        p = Path(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            return False
        return (
            str(p.resolve()) == self.resolved_path
            and st.st_size == self.size_bytes
            and st.st_mtime_ns == self.mtime_ns
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "document_id": self.document_id,
            "fingerprint": self.fingerprint,
            "physical_cache_key": self.physical_cache_key,
            "resolved_path": self.resolved_path,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_path(cls, path: str | Path) -> "DocumentIdentity":
        p = Path(path)
        st = p.stat()
        raw = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return cls(
            document_id=fingerprint[:12],
            fingerprint=fingerprint,
            physical_cache_key=fingerprint,
            resolved_path=str(p.resolve()),
            size_bytes=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )

    @classmethod
    def from_path_with_mtime(cls, path: str | Path, *, size_bytes: int, mtime_ns: int) -> "DocumentIdentity":
        """Создать identity по явно переданным ``size_bytes``/``mtime_ns``.

        Полезно для back-compat с ``_physical_cache_key``,
        который использовал ``st.st_mtime`` (секунды).
        """
        p = Path(path)
        raw = f"{p.resolve()}|{size_bytes}|{mtime_ns}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return cls(
            document_id=fingerprint[:12],
            fingerprint=fingerprint,
            physical_cache_key=fingerprint,
            resolved_path=str(p.resolve()),
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )


__all__ = ["DocumentIdentity"]