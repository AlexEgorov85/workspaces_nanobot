"""Follow Up 2.0 — History API Route."""
from __future__ import annotations
import json
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.storage.database import MessageRepo, SessionRepo, get_db

router = APIRouter(prefix="/api/history", tags=["history"])


class RenameSessionRequest(BaseModel):
    title: str


@router.get("/sessions")
async def get_sessions():
    """Список всех сессий (последние 50)."""
    with get_db() as db:
        sessions = SessionRepo.list_all(db)
        return [
            {
                "id": s.id,
                "title": s.title or f"Сессия #{s.id}",
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in sessions
        ]


@router.get("/sessions/{session_id}")
async def get_session_messages(session_id: int):
    """Все сообщения конкретной сессии."""
    with get_db() as db:
        session = SessionRepo.get(db, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        messages = MessageRepo.get_session_messages(db, session_id)
        return {
            "session_id": session_id,
            "title": session.title or f"Сессия #{session_id}",
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "agent_type": m.agent_type,
                    "contexts": json.loads(m.contexts_json) if m.contexts_json else [],
                    "followups": json.loads(m.followups_json) if m.followups_json else [],
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in messages
            ],
        }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """Удалить сессию и все её сообщения."""
    with get_db() as db:
        session = SessionRepo.get(db, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        SessionRepo.delete(db, session_id)
    return {"ok": True}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: int, req: RenameSessionRequest):
    """Переименовать сессию."""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Название не может быть пустым")
    with get_db() as db:
        session = SessionRepo.get(db, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Сессия не найдена")
        SessionRepo.update_title(db, session_id, title)
    return {"ok": True, "session_id": session_id, "title": title}
