"""Streamlit UI — тонкий клиент gateway через agent_conversation_messages."""
from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

# Подключаем workspace, чтобы импортировать utils.db
_workspace = str(Path(__file__).resolve().parent / "workspace")
if _workspace not in sys.path:
    sys.path.insert(0, _workspace)

from utils.db import configure, fetch, fetchone, execute
from utils.session_file_store import SessionFileStore
from utils.jsonb import decode_jsonb as _decode_jsonb
from utils.jsonb import decode_json_list as _decode_media_list
from utils.media import serialize as _media_serialize
from utils.media import read_for_ui as _media_read_for_ui
from utils.media import entry_from_data_url as _media_entry_from_data_url
from config import SETTINGS

_pg = (getattr(SETTINGS, "channels", {}) or {}).get("postgres", {})
_dsn = _pg.get("dsn", "")
_schema = _pg.get("schema", "public")
_table = _pg.get("table_name", "agent_conversation_messages")
_fq_table = f"{_schema}.{_table}"

_MAX_WAIT = SETTINGS.streamlit.get("max_wait", 600)
_POLL_INTERVAL = SETTINGS.streamlit.get("poll_interval", 1.0)
_CHAT_ID = SETTINGS.streamlit.get("chat_id", "streamlit")
_USER_ID = SETTINGS.streamlit.get("user_id", "user")
_ERROR_WINDOW = SETTINGS.streamlit.get("error_window_sec", 300)

# Единый стор файлов — ассистентские вложения (download) и пользовательские
# upload-файлы (те, что стримлит кладёт в БД) хранятся здесь, в одной
# иерархии с результатами инструментов агента. Никакого отдельного
# "data_store/streamlit_files" больше нет.
_session_cache_dir = SETTINGS.streamlit.get(
    "files_dir",
    _pg.get("media_cache_dir", "data_store/cache/sessions"),
)
_PATH = Path(_session_cache_dir)
if not _PATH.is_absolute():
    _PATH = Path(__file__).parent / "workspace" / _session_cache_dir
_FILE_STORE_BASE = _PATH.parent if _PATH.name == "sessions" else _PATH
_FILE_STORE = SessionFileStore(_FILE_STORE_BASE, attachments_subdir="attachments")

if _dsn:
    configure(_dsn)


def _load_chat_history(chat_id: str = _CHAT_ID) -> list[dict]:
    """Загрузить историю чата из БД.
    
    Возвращает список сообщений в формате для st.session_state.messages.
    """
    rows = fetch(
        f"SELECT id, role, content, media, metadata, reply_to, status, created_at "
        f"FROM {_fq_table} "
        f"WHERE chat_id = %s AND status IN ('completed', 'pending', 'processing') "
        f"ORDER BY created_at ASC",
        chat_id,
    )
    
    messages = []
    for row in rows:
        role = row["role"]
        content = row["content"] or ""
        metadata = _decode_jsonb(row["metadata"])
        media = _decode_media_list(row["media"])
        
        # Пропускаем системные/технические сообщения
        if role not in ("user", "assistant"):
            continue
        
        msg_entry: dict = {"role": role, "content": content}

        # Служебная заметка о сжатии контекста (ContextCompactionService).
        # Пишется в metadata.kind="context_compact" самим сервисом при
        # ручном или автоматическом сжатии, если это сделал
        # ContextCompactionService. UI выделяет такие записи стилем
        # ``.compact-notice`` (см. CSS ниже).
        if metadata.get("kind") == "context_compact":
            msg_entry["compact_notice"] = True

        # Метрика занятости контекстного окна (M1+T1+S1).
        # Блок ``context_window`` кладётся в metadata финального outbound
        # патчем ``RuntimePatcher.patch_assemble_outbound`` и сливается
        # в БД каналом. UI рисует прогресс-бар и подпись (used/limit, %).
        if role == "assistant" and isinstance(metadata.get("context_window"), dict):
            msg_entry["context_window"] = metadata["context_window"]

        # Добавляем reasoning если есть
        if role == "assistant" and metadata.get("reasoning"):
            msg_entry["reasoning"] = metadata["reasoning"]
        
        # Добавляем файлы если есть
        if media:
            msg_entry["media"] = media
        
        messages.append(msg_entry)
    
    return messages


def _get_extension_from_mime(mime_type: str) -> str:
    """Получить расширение файла по MIME-типу (без принудительного дефолта).

    Делегирует общей ``utils.session_file_store.guess_ext_from_mime``
    с ``default_ext=""`` — для неизвестного типа возвращается пустая
    строка (как и раньше, без подстановки ``.bin``).
    """
    from utils.session_file_store import guess_ext_from_mime

    return guess_ext_from_mime(mime_type or "", default_ext="")


def _save_file_from_data_url(data_url: str, filename: str) -> str | None:
    """Сохранить файл из data URL через общий ``SessionFileStore``.

    Дисковая иерархия теперь совпадает с каналом PostgresChannel и
    инструментами агента: ``cache/sessions/{session_key}/attachments/…``.
    ``filename`` используется как подсказка для сохранения человеко-читаемого
    суффикса; уникальность имени берёт на себя стор (``{uuid12}_{filename}``).
    """
    if not data_url or not isinstance(data_url, str):
        return None
    info = _FILE_STORE.save_attachment(
        session_key=f"streamlit:{_CHAT_ID}",
        data_url=data_url,
        filename=filename or None,
    )
    return info["path"] if info else None


def _check_response(msg_id: str) -> tuple[str | None, dict | None]:
    """Проверяет ответ assistant'а.
    
    Возвращает кортеж (контент, метаданные) или (None, None).
    """
    row = fetchone(
        f"SELECT content, metadata, media, status FROM {_fq_table} "
        f"WHERE reply_to = %s AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
        msg_id,
    )
    if not row:
        return None, None
    
    status = row["status"]
    if status == "completed":
        metadata = _decode_jsonb(row["metadata"])
        media = _decode_media_list(row["media"])
        result = {"content": row["content"] or "", "metadata": metadata, "media": media}
        return row["content"] or "", result
    if status == "failed":
        return "⚠️ Ошибка обработки", None
    return None, None


def _get_processing_state(msg_id: str) -> dict | None:
    """Возвращает промежуточное состояние processing-сообщения (контент, размышления)."""
    row = fetchone(
        f"SELECT content, metadata, status FROM {_fq_table} "
        f"WHERE reply_to = %s AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
        msg_id,
    )
    if not row or row["status"] != "processing":
        return None
    meta = _decode_jsonb(row["metadata"])
    state: dict = {"content": row["content"] or "", "reasoning": meta.get("reasoning", "")}
    cw = meta.get("context_window")
    if isinstance(cw, dict):
        state["context_window"] = cw
    return state


def _render_context_window(block: dict) -> None:
    """Отрисовать прогресс-бар занятости контекстного окна (M1 UI).

    Блок ``{used, limit, pct, model}`` кладётся в ``metadata.context_window``
    финального outbound (S1) и обновляется в processing-строке в фоне
    (T2) — UI для обоих случаев рисует одну и ту же полосу.

    pct уже clamp'нут в 0..1 на стороне патча; тут только дефолт-страховка
    на случай неполного блока (если metadata писалась вручную/миграцией).
    """
    if not isinstance(block, dict):
        return
    try:
        used = int(block.get("used") or 0)
        limit = int(block.get("limit") or 0)
    except (TypeError, ValueError):
        return
    if limit <= 0 or used < 0:
        return
    try:
        pct = float(block.get("pct", 0.0))
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(1.0, pct))
    model = block.get("model") or ""
    label = f"Контекст: {used} / {limit} · {int(round(pct * 100))}%"
    if model:
        label = f"{label} · {model}"
    st.progress(pct, text=label)


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
    .file-download-link {
        display: inline-block;
        margin: 0.25rem 0.5rem 0.25rem 0;
        padding: 0.25rem 0.5rem;
        background: #e8f4fd;
        border: 1px solid #b3d9ff;
        border-radius: 4px;
        font-size: 0.85rem;
        text-decoration: none;
        color: #0066cc;
    }
    .file-download-link:hover {
        background: #d0e8f8;
        text-decoration: underline;
    }
    /* Служебная заметка о сжатии контекста (ContextCompactionService). */
    .compact-notice {
        background: #fff8e1;
        border-left: 3px solid #f0c040;
        border-radius: 0 8px 8px 0;
        padding: 0.5rem 0.75rem;
        font-size: 0.85rem;
        color: #7a5c00;
        white-space: pre-wrap;
    }
</style>
""", unsafe_allow_html=True)

# Инициализация session_state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "_last_msg_count" not in st.session_state:
    st.session_state._last_msg_count = 0
if "_processing" not in st.session_state:
    st.session_state._processing = False
if "_upload_key" not in st.session_state:
    st.session_state._upload_key = 0

st.markdown("## Чат с агентом")

# === ЗАГРУЗКА СООБЩЕНИЙ ИЗ БД ПРИ КАЖДОМ ОБНОВЛЕНИИ ===
db_messages = _load_chat_history(_CHAT_ID)

# Если сообщений в БД больше чем в session_state — обновляем из БД
# Это гарантирует, что мы не потеряем сообщения при обновлении страницы
if len(db_messages) > len(st.session_state.messages):
    st.session_state.messages = db_messages
elif len(db_messages) == len(st.session_state.messages):
    # Если количество совпадает, проверяем содержимое (на случай если БД обновилась)
    if db_messages and st.session_state.messages:
        # Сравниваем последнее сообщение
        if db_messages[-1].get("content") != st.session_state.messages[-1].get("content"):
            st.session_state.messages = db_messages
elif len(db_messages) < len(st.session_state.messages) and not st.session_state._processing:
    # Если в БД меньше (например, было откатано) — синхронизируемся
    st.session_state.messages = db_messages

# Отображение сообщений с поддержкой файлов
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
        if entry.get("compact_notice"):
            st.markdown(
                f'<div class="compact-notice">🗜️ {entry["content"]}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(entry["content"])

        cw = entry.get("context_window")
        if isinstance(cw, dict):
            _render_context_window(cw)
        
        # Отображение файлов если есть
        media = entry.get("media", [])
        if media:
            for media_item in media:
                if isinstance(media_item, str):
                    # Это может быть data URL или путь к файлу
                    if media_item.startswith("data:"):
                        # Data URL — сохраняем и показываем ссылку на скачивание
                        # Извлекаем MIME-тип из data URL для определения расширения
                        mime_type = ""
                        if "," in media_item:
                            header = media_item.split(",")[0]
                            if ":" in header and ";" in header:
                                mime_type = header.split(":")[1].split(";")[0]
                        
                        ext = _get_extension_from_mime(mime_type) if mime_type else ""
                        filename = f"file_{uuid.uuid4().hex[:8]}{ext}"
                        saved_path = _save_file_from_data_url(media_item, filename)
                        if saved_path:
                            file_path = Path(saved_path)
                            with open(file_path, "rb") as f:
                                st.download_button(
                                    label=f"📎 Скачать {file_path.name}",
                                    data=f.read(),
                                    file_name=file_path.name,
                                    key=f"download_{uuid.uuid4()}",
                                )
                    elif Path(media_item).exists():
                        # Файл существует локально
                        file_path = Path(media_item)
                        with open(file_path, "rb") as f:
                            st.download_button(
                                label=f"📎 Скачать {file_path.name}",
                                data=f.read(),
                                file_name=file_path.name,
                                key=f"download_{uuid.uuid4()}",
                            )
                    else:
                        # Просто показываем как текст (URL или имя)
                        st.markdown(f"📎 `{media_item}`")
                elif isinstance(media_item, dict):
                    # Толерантный читатель: file_id (новый AW) → data (legacy)
                    # → path (после декодинга). Схему UI здесь не знает.
                    data_url, path, filename = _media_read_for_ui(media_item)
                    
                    if data_url and data_url.startswith("data:"):
                        # Извлекаем MIME-тип из data URL для определения расширения
                        mime_type = ""
                        if "," in data_url:
                            header = data_url.split(",")[0]
                            if ":" in header and ";" in header:
                                mime_type = header.split(":")[1].split(";")[0]
                        
                        ext = _get_extension_from_mime(mime_type) if mime_type else ""
                        
                        # Если в filename нет расширения, добавляем его из MIME-типа
                        if not Path(filename).suffix and ext:
                            filename = f"{Path(filename).stem}{ext}"
                        
                        saved_path = _save_file_from_data_url(data_url, filename)
                        if saved_path:
                            file_path = Path(saved_path)
                            with open(file_path, "rb") as f:
                                st.download_button(
                                    label=f"📎 Скачать {file_path.name}",
                                    data=f.read(),
                                    file_name=file_path.name,
                                    key=f"download_{uuid.uuid4()}",
                                )
                    elif path and Path(path).exists():
                        file_path = Path(path)
                        with open(file_path, "rb") as f:
                            st.download_button(
                                label=f"📎 Скачать {filename}",
                                data=f.read(),
                                file_name=filename,
                                key=f"download_{uuid.uuid4()}",
                            )
                    else:
                        st.markdown(f"📎 `{filename}`")

processing = st.session_state.get("_processing", False)

if processing:
    # Блокирующий цикл ожидания: поллим БД пока assistant не ответит
    msg_id = st.session_state["_msg_id"]

    with st.status("⏳ Агент думает...", expanded=True) as status:
        placeholder = st.empty()
        start_time = time.time()
        error_since: float | None = None

        while True:
            elapsed = int(time.time() - start_time)

            # Проверяем статус пользовательского сообщения напрямую
            row = fetchone(
                f"SELECT status FROM {_fq_table} WHERE id = %s", msg_id
            )
            cur_status = row["status"] if row else None

            if cur_status == "completed":
                response, response_data = _check_response(msg_id)
                status.update(label="✅ Ответ получен", state="complete")

                # Формируем полное сообщение с метаданными
                msg_entry = {"role": "assistant", "content": response or ""}
                if response_data:
                    meta = response_data.get("metadata", {})
                    if meta.get("reasoning"):
                        msg_entry["reasoning"] = meta["reasoning"]
                    if response_data.get("media"):
                        msg_entry["media"] = response_data["media"]

                st.session_state.messages.append(msg_entry)
                placeholder.empty()
                st.session_state._processing = False
                st.rerun()

            if cur_status == "failed":
                # failed — терминальный статус: канал больше не будет
                # обрабатывать задачу, показываем ошибку сразу.
                status.update(label="❌ Ошибка", state="error")
                placeholder.markdown("⚠️ Ошибка обработки. Ответ не получен.")
                st.session_state._processing = False
                st.rerun()

            if cur_status == "error":
                # error — повторяемая ошибка: канал вернёт задачу в пул
                # после backoff (error_retry_delay). Даём окно на повторную
                # обработку, и только после него — ошибка окончательно.
                if error_since is None:
                    error_since = time.time()
                error_elapsed = int(time.time() - error_since)
                if error_elapsed >= _ERROR_WINDOW:
                    status.update(label="❌ Ошибка", state="error")
                    placeholder.markdown("⚠️ Ошибка обработки. Ответ не получен.")
                    st.session_state._processing = False
                    st.rerun()
                placeholder.markdown(
                    f"⚠️ Временная ошибка, повторяю... {error_elapsed}с из {_ERROR_WINDOW}с"
                )
                time.sleep(_POLL_INTERVAL)
                continue

            # status in ('pending', 'processing') — сообщение в работе,
            # ждём бесконечно, без таймаута.
            error_since = None

            # Показываем live-состояние: размышления, черновик или просто счётчик
            state = _get_processing_state(msg_id)
            if state and state["reasoning"]:
                placeholder.markdown(
                    f"⏳ Ожидание... {elapsed}с\n\n"
                    f'<details class="reasoning-wrap" open>'
                    f"<summary>💭 Размышления</summary>"
                    f'<div class="reasoning-box">{state["reasoning"]}</div>'
                    f"</details>",
                    unsafe_allow_html=True,
                )
            elif state and state["content"]:
                placeholder.markdown(f"✍️ {state['content'][:200]}...")
            else:
                placeholder.markdown(f"⏳ Ожидание... {elapsed}с")

            # Живой прогресс-бар занятости контекста (T2): канал обновляет
            # ``metadata.context_window`` в processing-строке каждые
            # ``_flush_interval`` секунд. Отдельный placeholder, чтобы
            # маркдаун выше не перетирал бар.
            cw = state.get("context_window") if state else None
            if isinstance(cw, dict):
                _render_context_window(cw)

            time.sleep(_POLL_INTERVAL)

# Загрузка файлов пользователем.
# IMPORTANT: file_uploader делит состояние по key; в комбинации с
# st.chat_input + st.rerun() есть окно гонки, в котором выбранные
# файлы сбрасываются до INSERT. Решение — буферизовать выбранные
# файлы в session_state СРАЗУ при изменении загрузчика через
# callback on_change, и чистить буфер только после успешной записи.
_upload_key = st.session_state.get("_upload_key", 0)


def _buffer_uploads() -> None:
    """on_change для file_uploader: переложить свежевыбранные файлы
    в устойчивый буфер session_state, чтобы они пережили rerun."""
    if "_pending_uploads" not in st.session_state:
        st.session_state._pending_uploads = []
    wkey = f"attachments_{st.session_state.get('_upload_key', 0)}"
    w = st.session_state.get(wkey)
    files = w if isinstance(w, list) else ([w] if w else [])
    for f in files:
        if f is None:
            continue
        try:
            blob = f.getvalue()
        except Exception:
            continue
        mime = getattr(f, "type", "") or "application/octet-stream"
        st.session_state._pending_uploads.append({
            "filename": f.name,
            "mime": mime,
            "data_b64": base64.b64encode(blob).decode("ascii"),
        })


uploaded_files = st.file_uploader(
    "Вложения",
    accept_multiple_files=True,
    key=f"attachments_{_upload_key}",
    on_change=_buffer_uploads,
    disabled=processing,
)

prompt = st.chat_input("Напишите сообщение...", disabled=processing)

if prompt and not processing:
    media_entries = []
    pending = st.session_state.pop("_pending_uploads", []) or []
    for item in pending:
        if not isinstance(item, dict) or not item.get("data_b64"):
            continue
        data_url = f"data:{item.get('mime') or 'application/octet-stream'};base64,{item['data_b64']}"
        media_entries.append(
            _media_entry_from_data_url(data_url, item.get("filename") or "file")
        )

    if uploaded_files and not pending:
        # Fallback: буфер пуст (например, первый сабмит после выбора
        # до того, как on_change успел отработать). Берём прямо из виджета.
        for f in (uploaded_files if isinstance(uploaded_files, list) else [uploaded_files]):
            if f is None:
                continue
            b64 = base64.b64encode(f.getvalue()).decode("ascii")
            mime = f.type or "application/octet-stream"
            data_url = f"data:{mime};base64,{b64}"
            media_entries.append(_media_entry_from_data_url(data_url, f.name))

    msg_id = str(uuid.uuid4())

    execute(
        f"INSERT INTO {_fq_table} (id, chat_id, user_id, role, content, media, status) "
        f"VALUES (%s, %s, %s, 'user', %s, %s::jsonb, 'pending')",
        msg_id, _CHAT_ID, _USER_ID, prompt, json.dumps(media_entries),
    )

    user_msg = {"role": "user", "content": prompt}
    if media_entries:
        user_msg["media"] = media_entries
    st.session_state.messages.append(user_msg)

    st.session_state._upload_key = _upload_key + 1
    st.session_state.pop("_pending_uploads", None)

    st.session_state["_msg_id"] = msg_id
    st.session_state._processing = True
    st.rerun()
