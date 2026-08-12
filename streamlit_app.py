"""Streamlit UI — тонкий клиент gateway через conversation_messages."""
from __future__ import annotations

import base64
import json
import mimetypes
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
from config import SETTINGS

_pg = (getattr(SETTINGS, "channels", {}) or {}).get("postgres", {})
_dsn = _pg.get("dsn", "")
_schema = _pg.get("schema", "public")
_table = _pg.get("table_name", "conversation_messages")
_fq_table = f"{_schema}.{_table}"

_MAX_WAIT = SETTINGS.streamlit.get("max_wait", 600)
_POLL_INTERVAL = SETTINGS.streamlit.get("poll_interval", 1.0)
_CHAT_ID = SETTINGS.streamlit.get("chat_id", "streamlit")
_USER_ID = SETTINGS.streamlit.get("user_id", "user")

# Папка для загруженных/скачанных файлов
_FILES_DIR = Path(__file__).parent / "workspace" / "data_store" / "streamlit_files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)

if _dsn:
    configure(_dsn)


def _decode_jsonb(val) -> dict:
    """Распарсить JSONB-значение из PG в dict.

    Принимает None, строку JSON, уже готовый dict или Mapping.
    """
    if val is None:
        return {}
    if isinstance(val, str):
        return json.loads(val) if val else {}
    if isinstance(val, dict):
        return val
    return dict(val) if val else {}


def _decode_media_list(val) -> list[Any]:
    """Распарсить JSONB-список media из БД."""
    if val is None:
        return []
    if isinstance(val, str):
        return json.loads(val) if val else []
    if isinstance(val, list):
        return val
    return []


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
        
        # Добавляем reasoning если есть
        if role == "assistant" and metadata.get("reasoning"):
            msg_entry["reasoning"] = metadata["reasoning"]
        
        # Добавляем файлы если есть
        if media:
            msg_entry["media"] = media
        
        messages.append(msg_entry)
    
    return messages


def _get_extension_from_mime(mime_type: str) -> str:
    """Получить расширение файла по MIME-типу.

    Для неизвестного типа возвращается ``.bin`` — файл никогда не должен
    оставаться без расширения (иначе скачивание агентских файлов даёт имя
    вида ``file_1a2b3c4d`` без расширения).
    """
    if not mime_type:
        return ""
    mime = mime_type.split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(mime)
    if ext:
        return ext
    fallback = {
        "application/octet-stream": ".bin",
        "text/markdown": ".md",
        "text/html": ".html",
        "text/xml": ".xml",
        "application/x-7z-compressed": ".7z",
        "application/x-rar-compressed": ".rar",
        "application/vnd.rar": ".rar",
    }
    return fallback.get(mime, ".bin")


def _save_file_from_data_url(data_url: str, filename: str) -> str | None:
    """Сохранить файл из data URL в локальную папку, вернуть путь."""
    try:
        if not data_url.startswith("data:"):
            return None
        
        # Парсим data:MIME;base64,DATA
        header, data = data_url.split(",", 1)
        mime_part = header.split(":")[1].split(";")[0]
        
        file_data = base64.b64decode(data)
        
        # Создаём уникальный путь с правильным расширением
        safe_filename = Path(filename).name
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        
        # Если расширения нет в имени файла, пробуем получить его из MIME-типа
        if not suffix:
            ext = _get_extension_from_mime(mime_part)
            if ext:
                safe_filename = f"{stem}{ext}"
        
        file_path = _FILES_DIR / safe_filename
        
        # Если файл существует, добавляем суффикс
        counter = 1
        while file_path.exists():
            stem = Path(safe_filename).stem
            suffix = Path(safe_filename).suffix
            file_path = _FILES_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
        
        file_path.write_bytes(file_data)
        return str(file_path)
    except Exception:
        return None


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
    return {"content": row["content"] or "", "reasoning": meta.get("reasoning", "")}


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
        st.markdown(entry["content"])
        
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
                    # Dict с filename/path/data
                    filename = media_item.get("filename", "file")
                    data_url = media_item.get("data", "")
                    path = media_item.get("path", "")
                    
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
        failed_since: float | None = None

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
                # Статус может вернуться в работу (retry канала). Даём окно
                # в 5 минут на повторную обработку, и только после него
                # показываем ошибку окончательно.
                if failed_since is None:
                    failed_since = time.time()
                failed_elapsed = int(time.time() - failed_since)
                if failed_elapsed >= 300:
                    status.update(label="❌ Ошибка", state="error")
                    placeholder.markdown("⚠️ Ошибка обработки. Ответ не получен.")
                    st.session_state._processing = False
                    st.rerun()
                placeholder.markdown(
                    f"⚠️ Получена ошибка, перепроверяю... {failed_elapsed}с из 300с"
                )
                time.sleep(_POLL_INTERVAL)
                continue

            # status in ('pending', 'processing') — сообщение в работе,
            # ждём бесконечно, без таймаута.
            failed_since = None

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

            time.sleep(_POLL_INTERVAL)

# Загрузка файлов пользователем
_upload_key = st.session_state.get("_upload_key", 0)
uploaded_files = st.file_uploader(
    "Вложения",
    accept_multiple_files=True,
    key=f"attachments_{_upload_key}",
    disabled=processing,
)

prompt = st.chat_input("Напишите сообщение...", disabled=processing)

if prompt and not processing:
    # Сохраняем сообщение пользователя в БД
    msg_id = str(uuid.uuid4())

    media_entries = []
    if uploaded_files:
        for f in uploaded_files:
            b64 = base64.b64encode(f.getvalue()).decode("ascii")
            mime = f.type or "application/octet-stream"
            media_entries.append({
                "filename": f.name,
                "data": f"data:{mime};base64,{b64}",
            })
    
    # Обновляем ключ загрузчика чтобы очистить список
    st.session_state._upload_key = _upload_key + 1

    # Вставляем сообщение в БД
    execute(
        f"INSERT INTO {_fq_table} (id, chat_id, user_id, role, content, media, status) "
        f"VALUES (%s, %s, %s, 'user', %s, %s::jsonb, 'pending')",
        msg_id, _CHAT_ID, _USER_ID, prompt, json.dumps(media_entries),
    )

    # Добавляем в session_state для немедленного отображения
    user_msg = {"role": "user", "content": prompt}
    if media_entries:
        user_msg["media"] = media_entries
    st.session_state.messages.append(user_msg)

    st.session_state["_msg_id"] = msg_id
    st.session_state._processing = True
    st.rerun()
