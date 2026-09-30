"""Подпись конфигурации индекса и её проверка.

Портировано из агента: ``lib/services/cache_provider_impl.py``
(``compute_index_signature``, ``verify_index_signature``). Удаление агентской
копии — фазы 4/5/9. Логика и набор полей подписи перенесены без изменений —
иначе подписи, посчитанные агентом и платформой, разошлись бы при любом
сравнении исторических снимков.

Назначение: индекс считается пригодным только когда его конфигурация
(модель эмбеддингов, размерность, колонки, chunk-параметры, метрика)
совпадает с текущей. Расхождение — ``STALE``, повреждённая подпись —
``INVALID``. Оба случая поднимаются вызывающим как ``IndexIntegrityError`` с
отдельным кодом (``stale_index`` / ``invalid_index``), а не деградируют в
«ничего не найдено».
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

# Канонический список полей, по которым вычисляется signature индекса.
# Любое изменение этих параметров между сборками → индекс устарел.
INDEX_SIGNATURE_FIELDS = (
    "src_table",
    "pk_column",
    "content_cols",
    "embedding_cols",
    "track_column",
    "embedding_model",
    "embedding_dimension",
    "chunk_size",
    "chunk_overlap",
    "metric",
)


def compute_index_signature(cfg: dict[str, Any]) -> str:
    """SHA256-хеш канонической конфигурации индекса.

    Вход: dict, где ключи — поля из ``INDEX_SIGNATURE_FIELDS`` (неполный
    допустим; отсутствующие трактуются как ``""``). Выход: 64-char hex.

    Детерминирована: одинаковый вход → одинаковый выход на любой платформе.
    """
    parts: list[str] = []
    for key in INDEX_SIGNATURE_FIELDS:
        val = cfg.get(key)
        if isinstance(val, (list, tuple)):
            val = ",".join(str(v) for v in val)
        parts.append(f"{key}={val if val is not None else ''}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_index_signature(
    stored_meta: dict[str, Any] | None,
    current_cfg: dict[str, Any],
) -> Literal["CURRENT", "STALE", "INVALID"]:
    """Сравнить signature в сохранённом metadata с текущим конфигом.

    Returns:
        ``CURRENT`` — ``stored_meta`` пуст/None (индекс без persisted-signature:
            она вычисляется inline при сборке и совпадает с текущим конфигом)
            ИЛИ signature в нём совпадает с текущим.
        ``STALE``  — signature присутствует и не совпадает с текущим (изменилась
            модель эмбеддингов, размерность, chunk-параметры, колонки).
        ``INVALID`` — signature присутствует, но повреждена (не hex / не длиной
            64).
    """
    if stored_meta is None:
        return "CURRENT"
    stored_sig = stored_meta.get("signature")
    if not stored_sig:
        return "CURRENT"
    if not isinstance(stored_sig, str) or len(stored_sig) != 64:
        return "INVALID"
    current_sig = compute_index_signature(current_cfg)
    return "CURRENT" if stored_sig == current_sig else "STALE"
