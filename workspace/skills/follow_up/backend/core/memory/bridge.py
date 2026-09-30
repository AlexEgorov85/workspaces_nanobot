"""Follow Up 2.0 — мост «память → старый путь».

Живёт до ЭТАПА 4 и удаляется вместе с `query_understanding`. Задача одна:
подставить фокус диалога ВЫШЕ определения интента, а не внутри одного агента.

Почему выше. Фраза «а какие данные использовались?» не содержит ни одного
местоимения из белого списка `needs_reference_resolution`, и подстановка внутри
KM_DETAIL её не спасёт: до агента она доедет уже без номера. Проверено прогоном
регексов — эта же фраза попадает в FOLLOWUP, а не в KM_DETAIL.

Почему НЕ подставлять всегда. Охватный вопрос («в каких актах», «все акты»,
«сколько всего») фокусом сужать нельзя: аудитор спрашивает про корпус, а получил
бы ответ про одну проверку — то же самое молчаливое враньё, только наоборот.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Маркеры охвата. Список, а не регекс в исходнике: калибруется на офлайн-наборе
# формулировок, и править его должно быть можно без правки кода.
CORPUS_WIDE_MARKERS = (
    "в каких актах", "во всех актах", "все акты", "всех проверках",
    "в каких проверках", "по всем", "сколько всего", "сколько актов",
    "перечисли акты", "список актов", "везде", "по корпусу",
    "в каких кейсах", "во всех кейсах",
)


def is_corpus_wide(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in CORPUS_WIDE_MARKERS)


def fill_focus(ctx, session_id: int) -> Optional[str]:
    """Подставить проверку из памяти. Возвращает подставленный номер или None.

    Порядок проверок — это и есть защита доменного правила: аудитор, назвавший
    номер сам, всегда сильнее памяти; охватный вопрос не сужается никогда.
    """
    from backend.config import get_settings
    from backend.core.memory import state as memory

    if getattr(ctx, "km_numbers", None):
        return None                       # аудитор назвал номер сам
    if is_corpus_wide(getattr(ctx, "raw_query", "")):
        return None                       # охватный вопрос сужать нельзя

    st = memory.load(session_id)
    if not st.check_id:
        return None
    if st.is_inferred:
        # Подставленный фокус живёт ограниченно: молча тянуть его через всю
        # сессию значит однажды ответить не про ту проверку
        cfg = get_settings()
        try:
            from backend.storage.database import ChatMessage, get_db
            with get_db() as db:
                since = (db.query(ChatMessage)
                         .filter(ChatMessage.session_id == session_id,
                                 ChatMessage.role == "user").count())
            if since > cfg.focus_inferred_ttl_turns * 2:
                return None
        except Exception:
            pass

    ctx.km_numbers = [st.check_id]
    ctx.resolved_from_history = True
    logger.info(f"[memory] Фокус подставлен: {st.check_id} ({st.basis})")
    return st.check_id
