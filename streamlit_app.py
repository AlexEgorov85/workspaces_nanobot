import argparse
import asyncio
import queue
import threading
import time
from pathlib import Path

import streamlit as st

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _load_runtime_config
from pg_session_manager import PGSessionManager


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
        margin: 0.5rem 0;
        font-size: 0.85rem;
        line-height: 1.5;
        color: #555;
    }
    .reasoning-header {
        cursor: pointer;
        user-select: none;
        font-size: 0.8rem;
        color: #888;
        margin-bottom: 0.25rem;
    }
    .reasoning-header:hover {color: #555;}
    .stream-cursor {display:inline-block; background:#f0f0f0; border-radius:20px; padding:2px 12px 4px; margin-left:4px; font-size:.85rem; color:#888; letter-spacing:2px; vertical-align:middle;}
    .stream-cursor::after {content:'...'; animation: dots 1.5s steps(4) infinite;}
    @keyframes dots {0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}100%{content:''}}
    .stChatInput {border: 1px solid #e0e0e0 !important; border-radius: 12px !important;}
</style>
""", unsafe_allow_html=True)


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", choices=["file", "postgres", "auto"], default="file")
    args, _ = parser.parse_known_args()
    return args


@st.cache_resource
def _init(storage: str):
    config = _load_runtime_config(
        config="config.json",
        workspace=str(Path("workspace")),
    )

    pg_cfg = getattr(config.channels, "postgres", {})
    dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else getattr(pg_cfg, "dsn", "")

    use_pg = True if storage == "postgres" else (
        bool(dsn) if storage == "auto" else False
    )
    session_manager = None
    if use_pg:
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
        )
        session_manager.ensure_tables()

    bus = MessageBus()
    agent = AgentLoop.from_config(config, bus, session_manager=session_manager)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=_bg_loop, args=(agent, loop), daemon=True)
    t.start()
    return bus, loop


def _bg_loop(agent: AgentLoop, loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(agent.run())


def _publish(text: str):
    msg = InboundMessage(
        channel="streamlit",
        sender_id="user",
        chat_id="default",
        content=text,
    )
    asyncio.run_coroutine_threadsafe(bus.publish_inbound(msg), agent_loop)


def _consume():
    future = asyncio.run_coroutine_threadsafe(bus.consume_outbound(), agent_loop)
    return future.result(timeout=600)


def _process_in_background(prompt: str, q: queue.Queue):
    _publish(prompt)
    while True:
        try:
            msg = _consume()
        except Exception as e:
            q.put(("error", str(e)))
            break
        meta = msg.metadata or {}

        if meta.get("_reasoning_delta"):
            q.put(("reasoning", msg.content))
        elif meta.get("_reasoning_end"):
            continue
        elif meta.get("_stream_delta"):
            q.put(("delta", msg.content))
        elif meta.get("_stream_end"):
            continue
        elif meta.get("_progress") or meta.get("_turn_end") or meta.get("_tool_events"):
            continue
        else:
            q.put(("final", msg.content))
            break

    q.put(("done", ""))


_args = _parse_args()
bus, agent_loop = _init(_args.storage)

if "messages" not in st.session_state:
    st.session_state.messages = []

st.markdown("## Чат с агентом")

for entry in st.session_state.messages:
    with st.chat_message(entry["role"]):
        if r := entry.get("reasoning"):
            st.markdown(
                f'<div class="reasoning-header">💭 Размышления</div>'
                f'<div class="reasoning-box">{r}</div>',
                unsafe_allow_html=True,
            )
        st.markdown(entry["content"])

processing = st.session_state.get("_processing", False)

if processing:
    q = st.session_state["_queue"]
    while True:
        try:
            type_, content = q.get_nowait()
        except queue.Empty:
            break
        if type_ == "reasoning":
            st.session_state["_reasoning_parts"].append(content)
        elif type_ == "delta":
            st.session_state["_response_accum"] += content
        elif type_ == "final":
            st.session_state["_response_accum"] = content
        elif type_ == "error":
            st.session_state["_error"] = content
            st.session_state["_done"] = True
        elif type_ == "done":
            st.session_state["_done"] = True

    with st.chat_message("assistant"):
        reasoning = "".join(st.session_state["_reasoning_parts"])
        if reasoning:
            st.markdown(
                f'<div class="reasoning-header">💭 Размышления</div>'
                f'<div class="reasoning-box">{reasoning}</div>',
                unsafe_allow_html=True,
            )
        response = st.session_state["_response_accum"]
        if st.session_state.get("_error"):
            st.error(st.session_state["_error"])
        if st.session_state.get("_done"):
            st.markdown(response)
        else:
            st.markdown(f'{response}<span class="stream-cursor"></span>',
                        unsafe_allow_html=True)

    if not st.session_state.get("_done"):
        time.sleep(0.15)
        st.rerun()

prompt = st.chat_input("Напишите сообщение...", disabled=processing)

if prompt and not processing:
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state["_processing"] = True
    st.session_state["_reasoning_parts"] = []
    st.session_state["_response_accum"] = ""
    st.session_state["_done"] = False
    st.session_state["_error"] = ""
    st.session_state["_queue"] = queue.Queue()
    threading.Thread(
        target=_process_in_background,
        args=(prompt, st.session_state["_queue"]),
        daemon=True,
    ).start()
    time.sleep(0.15)
    st.rerun()

if processing and st.session_state.get("_done"):
    st.session_state["_processing"] = False
    st.session_state.messages.append({
        "role": "assistant",
        "content": st.session_state["_response_accum"],
        "reasoning": "".join(st.session_state["_reasoning_parts"]),
    })
    st.rerun()
