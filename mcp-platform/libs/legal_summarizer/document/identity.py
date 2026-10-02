"""DocumentIdentity — единый идентификатор документа.

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

1. Считает fingerprint как SHA-256 от **содержимого** файла
   (:func:`_content_sha256`), а не от ``(resolved_path, size, mtime)``.
2. Хранит ``document_id`` (== fingerprint[:12]) и ``physical_cache_key``
   (== fingerprint).
3. Проверяет freshness: ``is_fresh(path)`` — сравнивает ``(size, mtime)``
   с закэшированными.

Почему контент, а не stat: ``document_id`` — адрес содержимого. Тот же
документ, положенный в другой каталог или скопированный под другим
именем, получает **тот же** ``document_id`` и делит один кэш-разбор
вместо второго парсинга. Обратная сторона: ``touch`` файла больше не
инвалидирует кэш, а ``resolved_path``/``mtime_ns`` в хеш не входят —
остаются только метаданными для :meth:`is_fresh`.

Вся остальная информация (title, blocks, structure) — ответственность
``PhysicalDocument`` / ``DocumentStructure``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# Порция чтения при хешировании: память ограничена этой величиной
# независимо от размера документа (файл целиком в память не читается).
_HASH_CHUNK_BYTES = 1024 * 1024


def _content_sha256(path: Path) -> str:
    """SHA-256 **содержимого** файла, потоково (чанк за чанком).

    Единственное место в проекте, где считается контент-хеш документа:
    определение ``document_id`` обязано быть ровно одно.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class DocumentIdentity:
    """Единый идентификатор документа.

    Attributes:
        document_id: короткий ID (первые 12 hex fingerprint'а) для
            логирования и manifest.
        fingerprint: полный sha256 hex от **содержимого** файла.
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
        кэш нужно пересчитать.
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
    def from_path(cls, path: str | Path) -> DocumentIdentity:
        """Identity по контент-хешу файла.

        Стоимость: читается **весь** файл. SHA-256 через OpenSSL идёт
        примерно 1-2 ГБ/с, то есть 100 МБ ≈ 0.05-0.1 с, 1 ГБ ≈ 0.5-1 с,
        плюс время чтения с диска; память ограничена ``_HASH_CHUNK_BYTES``.

        Это плата за контент-адресность, и её видно на cache-hit пути:
        ``pipeline_structure._try_load_cached`` вызывает ``from_path`` на
        каждом прогоне, поэтому повторный вопрос по уже разобранному
        документу читает файл целиком. Дешёвый stat-gate :meth:`is_fresh`
        чтения не требует, но применим лишь там, где ключ известен заранее:
        snapshot ищется по ``document_id``, то есть по тому самому хешу, а
        индекса «stat → document_id» в слое кэша нет. Поэтому здесь он
        не используется, и хеш считается на каждом прогоне.
        """
        p = Path(path)
        st = p.stat()
        fingerprint = _content_sha256(p)
        return cls(
            document_id=fingerprint[:12],
            fingerprint=fingerprint,
            physical_cache_key=fingerprint,
            resolved_path=str(p.resolve()),
            size_bytes=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )


__all__ = ["DocumentIdentity"]
