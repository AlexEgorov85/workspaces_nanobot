"""Этап 28: DocumentIdentity для operation identity.

Проверяем:

- ``DocumentIdentity.document_id`` детерминирован для одного файла.
- Изменение файла (size/mtime) → новый document_id.
- Документы с одинаковым префиксом → разные document_id
  (нет коллизии из-за первых 12 hex символов).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def test_document_identity_is_deterministic(tmp_path):
    """Один и тот же файл → один и тот же document_id."""
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    p = tmp_path / "doc.txt"
    p.write_text("A" * 1000, encoding="utf-8")

    id1 = DocumentIdentity.from_path(p)
    id2 = DocumentIdentity.from_path(p)
    assert id1.document_id == id2.document_id, (
        f"deterministic identity failed: {id1.document_id} != {id2.document_id}"
    )
    assert id1.fingerprint == id2.fingerprint


def test_file_change_creates_new_identity(tmp_path):
    """Изменение файла → новый document_id."""
    import time as _time

    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    p = tmp_path / "doc.txt"
    p.write_text("version 1 content", encoding="utf-8")
    id1 = DocumentIdentity.from_path(p)

    # Меняем файл — обязательно с новым mtime (на FS с секундной точностью).
    _time.sleep(1.1)
    p.write_text("version 2 content, longer than before.", encoding="utf-8")
    id2 = DocumentIdentity.from_path(p)

    assert id1.document_id != id2.document_id, (
        f"document_id should change on file modification: "
        f"v1={id1.document_id}, v2={id2.document_id}"
    )


def test_different_files_have_different_identities(tmp_path):
    """Два разных файла → разные document_id."""
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    p1 = tmp_path / "a.txt"
    p1.write_text("AAA", encoding="utf-8")
    p2 = tmp_path / "b.txt"
    p2.write_text("BBB", encoding="utf-8")

    id1 = DocumentIdentity.from_path(p1)
    id2 = DocumentIdentity.from_path(p2)

    assert id1.document_id != id2.document_id


def test_documents_with_same_prefix_have_different_ids(tmp_path):
    """Документы с одинаковым префиксом (collision risk на 12 hex chars) — разные."""
    import hashlib
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    # Создаём 100 файлов с разным содержимым — никакой коллизии в 12 hex chars.
    ids = set()
    for i in range(100):
        p = tmp_path / f"doc_{i:04d}.txt"
        p.write_text(f"content-{i}", encoding="utf-8")
        ids.add(DocumentIdentity.from_path(p).document_id)

    # Birthday paradox для 12 hex chars: очень маловероятно.
    assert len(ids) >= 95, (
        f"unexpectedly many collisions: got {len(ids)} unique IDs from 100 files"
    )


def test_is_fresh_detects_modification(tmp_path):
    """is_fresh возвращает False после модификации."""
    import time as _time

    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    p = tmp_path / "doc.txt"
    p.write_text("version 1", encoding="utf-8")
    identity = DocumentIdentity.from_path(p)

    # Свежий.
    assert identity.is_fresh(p) is True

    # Меняем файл.
    _time.sleep(1.1)
    p.write_text("version 2 with more text", encoding="utf-8")
    assert identity.is_fresh(p) is False, "identity must be stale after modification"


def test_document_id_first_12_hex_chars(tmp_path):
    """document_id — это первые 12 hex chars от fingerprint."""
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    p = tmp_path / "doc.txt"
    p.write_text("X", encoding="utf-8")
    identity = DocumentIdentity.from_path(p)

    assert len(identity.document_id) == 12
    assert identity.document_id == identity.fingerprint[:12]
    # hex chars.
    int(identity.document_id, 16)
