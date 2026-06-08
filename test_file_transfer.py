"""Тест передачи файлов через public.conversation_messages.

1. Применяет миграцию (если нужно)
2. Вставляет сообщение с файлом (data URI)
3. Ждёт ответа от агента (поллинга)
4. Выводит результат

DSN берётся из gateway_settings.py — единственный источник правды.
"""

import asyncio
import base64
import json
import time

from gateway_settings import SETTINGS
from workspace.utils.db import db

db.configure(SETTINGS.pg.dsn)


async def main():
    # ---- 1. Миграция (если колонок нет) ----
    for col in ["chat_id TEXT", "user_id TEXT", "media JSONB DEFAULT '[]'::jsonb"]:
        await db.execute(f"ALTER TABLE public.conversation_messages ADD COLUMN IF NOT EXISTS {col}")

    # ---- 2. Вставить сообщение с файлом ----
    test_content = "Привет, бот! Это тестовый файл. Ответь, пожалуйста, что ты видишь в этом файле."
    b64 = base64.b64encode(test_content.encode()).decode()
    data_uri = f"data:text/plain;base64,{b64}"

    row = await db.fetchone("""
        INSERT INTO public.conversation_messages
            (chat_id, user_id, conversation_id, role, content, media, status)
        VALUES ($1, $2, uuid_generate_v4(), 'user', $3, $4::jsonb, 'pending')
        RETURNING id
    """, "file_test_chat", "test_user", "Прочитай этот файл", json.dumps([data_uri]))
    msg_id = row["id"]
    print(f"[1] Вставлено сообщение {msg_id} с файлом (data URI, {len(test_content)} байт)")

    # ---- 3. Ждать ответа агента ----
    print("[2] Ожидание ответа агента (поллинг каждые 2с)...")
    for attempt in range(30):  # максимум 60 секунд
        await asyncio.sleep(2)
        row = await db.fetchone(
            "SELECT id, role, content, media, status FROM public.conversation_messages "
            "WHERE chat_id = 'file_test_chat' AND role = 'assistant' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if row:
            print(f"\n[3] Получен ответ от агента!")
            print(f"    Статус: {row['status']}")
            print(f"    Текст: {row['content'][:200]}")
            raw_media = row["media"]
            if isinstance(raw_media, str):
                raw_media = json.loads(raw_media)
            if raw_media:
                print(f"    Файлы в ответе ({len(raw_media)}):")
                for m in raw_media:
                    preview = m[:80] + "..." if len(m) > 80 else m
                    print(f"      - {preview}")
            else:
                print("    Файлов в ответе нет")
            break
        print(f"    Попытка {attempt + 1}/30 — ответа пока нет...")
    else:
        print("[3] Ответ не получен за 60 секунд. Проверь, запущен ли gateway.py")

    # ---- 4. Показать всю переписку ----
    print("\n[4] Все сообщения в этом чате:")
    rows = await db.fetch(
        "SELECT role, left(content, 80) AS content_preview, media, status "
        "FROM public.conversation_messages WHERE chat_id = 'file_test_chat' "
        "ORDER BY created_at"
    )
    for r in rows:
        print(f"  [{r['role']:9}] {r['content_preview']}  | status={r['status']}  media={bool(r['media'])}")


asyncio.run(main())
