"""Тест: бот генерирует файл (CSV) и возвращает его через таблицу."""

import asyncio
import json
import asyncpg


async def main():
    conn = await asyncpg.connect(
        host="localhost", port=5432,
        user="postgres", password="1",
        database="postgres",
    )

    # Убедиться что колонки есть
    for col in ["chat_id TEXT", "user_id TEXT", "media JSONB DEFAULT '[]'::jsonb"]:
        await conn.execute(f"ALTER TABLE public.conversation_messages ADD COLUMN IF NOT EXISTS {col}")

    msg_id = await conn.fetchval("""
        INSERT INTO public.conversation_messages
            (chat_id, user_id, conversation_id, role, content, status)
        VALUES ($1, $2, uuid_generate_v4(), 'user', $3, 'pending')
        RETURNING id
    """, "csv_test_chat", "test_user",
        "Сгенерируй CSV-файл с примерами аудиторских проверок за 2024 год: "
        "колонки: id, object, violation_type, fine_amount, date. "
        "Создай файл через write_file и верни его содержимое."
    )
    print(f"[1] Запрос отправлен (id={msg_id})")

    print("[2] Ожидание ответа агента...")
    for attempt in range(30):
        await asyncio.sleep(2)
        row = await conn.fetchrow(
            "SELECT role, content, media, status FROM public.conversation_messages "
            "WHERE chat_id = 'csv_test_chat' AND role = 'assistant' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if row:
            print(f"\n[3] Ответ получен (status={row['status']})")
            print(f"    Текст ({len(row['content'])} зн.):")
            for line in row['content'].split('\n')[:8]:
                print(f"      {line}")
            if len(row['content'].split('\n')) > 8:
                print(f"      ... и ещё {len(row['content'].split('\n')) - 8} строк")

            raw_media = row['media']
            if isinstance(raw_media, str):
                raw_media = json.loads(raw_media)
            if raw_media:
                print(f"\n    Файлов в ответе: {len(raw_media)}")
                for i, m in enumerate(raw_media):
                    if m.startswith("data:"):
                        meta, b64 = m.split(",", 1)
                        print(f"      [{i}] {meta} ({len(b64)} символов base64)")
                    else:
                        print(f"      [{i}] {m[:120]}")
            else:
                print("\n    Файлов в ответе нет (media пуст)")
            break
        print(f"    Попытка {attempt+1}/30", end="\r")
    else:
        print("[3] Ответ не получен за 60с")

    print("\n[4] Вся переписка:")
    rows = await conn.fetch(
        "SELECT role, left(content, 100) AS preview, media IS NOT NULL AND media != '[]'::jsonb AS has_media "
        "FROM public.conversation_messages WHERE chat_id = 'csv_test_chat' "
        "ORDER BY created_at"
    )
    for r in rows:
        print(f"  [{r['role']:9}] {r['preview']}  | files={r['has_media']}")

    # Почистить
    await conn.execute("DELETE FROM public.conversation_messages WHERE chat_id = 'csv_test_chat'")
    print("\n[5] Тестовые сообщения удалены")
    await conn.close()


asyncio.run(main())
