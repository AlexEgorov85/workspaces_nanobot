import argparse
import asyncio
import importlib
import queue
import sys
import threading
import time
from pathlib import Path

import streamlit as st

from nanobot.agent.hook import AgentHook
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
    .stream-cursor {display:inline-block; background:#f0f0f0; border-radius:20px; padding:2px 12px 4px; margin-left:4px; font-size:.85rem; color:#888; letter-spacing:2px; vertical-align:middle;}
    .stream-cursor::after {content:'...'; animation: dots 1.5s steps(4) infinite;}
    @keyframes dots {0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}100%{content:''}}
    .stChatInput {border: 1px solid #e0e0e0 !important; border-radius: 12px !important;}
    .tool-events {
        margin-top: 0.5rem;
        padding-top: 0.5rem;
        border-top: 1px dashed #ccc;
    }
    .tool-event {
        font-size: 0.8rem;
        font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
        padding: 0.15rem 0;
        color: #666;
    }
    .tool-event::before {content: "⚙ "; color: #999;}
    .tool-event-error {color: #c00;}
    .tool-event-error::before {content: "✗ "; color: #c00;}
    .tool-event-ok {color: #2a7;}
</style>
""", unsafe_allow_html=True)


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", choices=["file", "postgres", "auto"], default="file")
    args, _ = parser.parse_known_args()
    return args


def _load_hooks(workspace_dir: Path) -> list:
    hooks: list = []
    hooks_dir = workspace_dir / "hooks"
    if not hooks_dir.is_dir():
        return hooks
    sys.path.insert(0, str(hooks_dir))
    for f in sorted(hooks_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(".py") or f.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f.name[:-3])
        except Exception:
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AgentHook)
                and attr is not AgentHook
                and not attr_name.startswith("_")
            ):
                try:
                    hook = attr(workspace_dir=workspace_dir)
                except Exception:
                    try:
                        hook = attr()
                    except Exception:
                        continue
                hooks.append(hook)
    return hooks


@st.cache_resource
def _init(storage: str):
    config = _load_runtime_config(
        config="config.json",
        workspace=str(Path("workspace")),
    )

    pg_cfg = getattr(config.channels, "postgres", {})
    channel_dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else getattr(pg_cfg, "dsn", "")
    sm_cfg = getattr(config, "session_manager", {}) or {}
    if isinstance(sm_cfg, dict):
        sm_dsn = sm_cfg.get("dsn") or channel_dsn
        sm_schema = sm_cfg.get("schema", "public")
        sm_messages_table = sm_cfg.get("messages_table", "session_messages")
        sm_meta_table = sm_cfg.get("meta_table", "session_meta")
        sm_min_conn = sm_cfg.get("min_conn", 1)
        sm_max_conn = sm_cfg.get("max_conn", 4)
        sm_pool_timeout = sm_cfg.get("pool_timeout", 5.0)
    else:
        sm_dsn = channel_dsn
        sm_schema = "public"
        sm_messages_table = "session_messages"
        sm_meta_table = "session_meta"
        sm_min_conn = 1
        sm_max_conn = 4
        sm_pool_timeout = 5.0
    dsn = sm_dsn

    use_pg = True if storage == "postgres" else (
        bool(dsn) if storage == "auto" else False
    )
    session_manager = None
    if use_pg:
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
            schema=sm_schema,
            messages_table=sm_messages_table,
            meta_table=sm_meta_table,
            min_conn=sm_min_conn,
            max_conn=sm_max_conn,
            pool_timeout=sm_pool_timeout,
        )
        session_manager.ensure_tables()

    hooks = _load_hooks(config.workspace_path)
    bus = MessageBus()
    agent = AgentLoop.from_config(config, bus, session_manager=session_manager, hooks=hooks)
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
        elif meta.get("_tool_events"):
            q.put(("tool_events", meta["_tool_events"]))
        elif meta.get("_progress") or meta.get("_turn_end"):
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
        r = entry.get("reasoning", "")
        te = entry.get("tool_events", [])
        if r or te:
            parts = []
            if r:
                parts.append(f'<div class="reasoning-box">{r}</div>')
            if te:
                events_html = ""
                for ev in te:
                    name = ev.get("name", "?")
                    phase = ev.get("phase", "")
                    if phase == "end":
                        result = str(ev.get("result", ""))[:80] or "ok"
                        events_html += f'<div class="tool-event tool-event-ok">{name} → {result}</div>'
                    elif phase == "error":
                        err = ev.get("error", "failed")
                        events_html += f'<div class="tool-event tool-event-error">{name}: {err}</div>'
                parts.append(f'<div class="tool-events">{events_html}</div>')
            st.markdown(
                f'<details class="reasoning-wrap">'
                f'<summary>💭 Размышления</summary>'
                f'{"".join(parts)}'
                f'</details>',
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
        elif type_ == "tool_events":
            for ev in content:
                if ev.get("phase") in ("end", "error"):
                    st.session_state["_tool_events"].append(ev)
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
        tool_events = st.session_state["_tool_events"]
        if reasoning or tool_events:
            parts = []
            if reasoning:
                parts.append(f'<div class="reasoning-box">{reasoning}</div>')
            if tool_events:
                events_html = ""
                for ev in tool_events:
                    name = ev.get("name", "?")
                    phase = ev.get("phase", "")
                    if phase == "end":
                        result = str(ev.get("result", ""))[:80] or "ok"
                        cls = "tool-event-ok"
                        events_html += f'<div class="tool-event {cls}">{name} → {result}</div>'
                    elif phase == "error":
                        err = ev.get("error", "failed")
                        cls = "tool-event-error"
                        events_html += f'<div class="tool-event {cls}">{name}: {err}</div>'
                parts.append(f'<div class="tool-events">{events_html}</div>')
            st.markdown(
                f'<details class="reasoning-wrap">'
                f'<summary>💭 Размышления</summary>'
                f'{"".join(parts)}'
                f'</details>',
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
    st.session_state["_tool_events"] = []
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
        "tool_events": st.session_state["_tool_events"],
    })
    st.rerun()
