"""Streamlit UI — тонкий клиент gateway через conversation_messages."""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import streamlit as st

# Подключаем workspace, чтобы импортировать utils.db
_workspace = str(Path(__file__).resolve().parent / "workspace")
if _workspace not in sys.path:
    sys.path.insert(0, _workspace)

from utils.db import configure, fetchone, execute
from gateway_settings import GatewaySettings

_SETTINGS = GatewaySettings()
_pg = _SETTINGS.pg
_dsn = _pg.dsn
_schema = _pg.schema or "public"
_table = _pg.channel.table_name or "conversation_messages"
_fq_table = f"{_schema}.{_table}"

if _dsn:
    configure(_dsn)


def _check_response(msg_id: str) -> str | None:
    """Check conversation_messages for the assistant response. Returns None if not ready."""
    row = fetchone(
        f"SELECT content, metadata, status FROM {_fq_table} "
        f"WHERE reply_to = %s AND role = 'assistant' ORDER BY created_at ASC LIMIT 1",
        msg_id,
    )
    if not row:
        return None
    status = row["status"]
    if status == "completed":
        return row["content"] or ""
    if status == "failed":
        return "⚠️ Ошибка обработки"
    return None


st.set_page_config(page_title="Чат с агентом", page_icon="💬", layout="wide")

st.markdown("""
<style>
    #MainMenu, header, footer {visibility: hidden;}
    .stApp {max-width: none; margin: 0 auto; padding: 0 2rem;}
    .reasoning-box {
        background: #f5f5f5;
        border-left: 3px solid #ddd;
        border-radius: 0 8px 8px 0;
        padding: 0.75rem 1rem;
        font-size: 0.85rem;
        line-height: 1.5;
        color: #555;
    }
    details.reasoning-wrap {
        margin: 0.5rem 0;
    }
    details.reasoning-wrap summary {
        cursor: pointer;
        user-select: none;
        font-size: 0.8rem;
        color: #888;
        margin-bottom: 0.25rem;
        list-style: none;
    }
    details.reasoning-wrap summary:hover {color: #555;}
    details.reasoning-wrap summary::-webkit-details-marker {
        display: none;
    }
    .stChatInput {border: 1px solid #e0e0e0 !important; border-radius: 12px !important;}
</style>
""", unsafe_allow_html=True)

if "messages" not in st.session_state:
    st.session_state.messages = []

st.markdown("## Чат с агентом")

for entry in st.session_state.messages:
    with st.chat_message(entry["role"]):
        r = entry.get("reasoning", "")
        if r:
            st.markdown(
                f'<details class="reasoning-wrap">'
                f'<summary>💭 Размышления</summary>'
                f'<div class="reasoning-box">{r}</div>'
                f'</details>',
                unsafe_allow_html=True,
            )
        st.markdown(entry["content"])

processing = st.session_state.get("_processing", False)

if processing:
    msg_id = st.session_state["_msg_id"]
    response = _check_response(msg_id)
    if response is not None:
        st.session_state["_processing"] = False
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()
    else:
        time.sleep(0.3)
        st.rerun()

prompt = st.chat_input("Напишите сообщение...", disabled=processing)

if prompt and not processing:
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state["_processing"] = True

    # Generate unique message ID
    msg_id = str(uuid.uuid4())

    # Write to conversation_messages — gateway picks it up
    execute(
        f"INSERT INTO {_fq_table} (id, chat_id, user_id, role, content, status) "
        f"VALUES (%s, %s, %s, 'user', %s, 'pending')",
        msg_id, "streamlit", "user", prompt,
    )

    st.session_state["_msg_id"] = msg_id
    st.rerun()
