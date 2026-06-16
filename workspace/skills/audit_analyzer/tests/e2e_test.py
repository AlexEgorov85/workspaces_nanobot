# -*- coding: utf-8 -*-
"""Сквозные тесты audit_analyzer: реальные вызовы всех режимов."""
import sys, os, json, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Исправление кодировки консоли Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

passed = 0
failed = 0

def ok(msg):
    global passed; passed += 1
    print(f'  [OK] {msg}')

def fail(msg, detail=''):
    global failed; failed += 1
    print(f'  [FAIL] {msg}')
    if detail:
        print(f'     {detail}')

print('=== audit_analyzer: E2E tests ===\n')

from config import load_db_config, get_vector_index_path
from database import Database
import predefined_mode
import sql_mode
import vector_mode


async def run_tests():
    """
    Главная тестовая функция. Выполняет 6 асинхронных тестовых сценариев:
      1. predefined — analytics_by_year_month с реальным SQL
      2. predefined — top_audited_objects с алиасом параметра
      3. predefined — неизвестный скрипт (ожидается ошибка)
      4. predefined — violations_by_type с кодом нарушения
      5. sql mode — генерация SQL через LLM + EXPLAIN + выполнение
      6. vector — FAISS + embedding (при необходимости создаёт тестовый индекс)
    """
    cfg = load_db_config()
    db_cfg = cfg if isinstance(cfg, dict) else {}
    async with Database(db_cfg) as db:

        # ─────────────────────────────────────────────
        # 1. Predefined mode — реальный SQL
        # ─────────────────────────────────────────────
        print('[1] predefined mode — analytics_by_year_month')
        res = await predefined_mode.run('analytics_by_year_month', db, {'year': 2024})

        if res['status'] == 'success':
            ok(f'analytics_by_year_month: {res["data"]["result"]["row_count"]} rows')
            print(f'     SQL: {res["data"]["sql"][:80]}...')
            print(f'     Columns: {res["data"]["result"]["columns"]}')
            print(f'     Rows: {res["data"]["result"]["rows"][:3]}')
        elif res['status'] == 'error':
            err = res['data']['message']
            if 'connection' in err.lower() or 'connect' in err.lower() or 'refused' in err.lower():
                fail('analytics_by_year_month', f'PostgreSQL недоступен: {err}')
            else:
                fail('analytics_by_year_month', err)
        else:
            fail('analytics_by_year_month', json.dumps(res, ensure_ascii=False, indent=2)[:300])

        # ─────────────────────────────────────────────
        # 2. Predefined mode — с алиасом audited_object
        # ─────────────────────────────────────────────
        print('\n[2] predefined mode — top_audited_objects (alias audited_object)')
        res2 = await predefined_mode.run('top_audited_objects', db, {'limit': 3})

        if res2['status'] == 'success':
            ok(f'top_audited_objects: {res2["data"]["result"]["row_count"]} rows')
            print(f'     Columns: {res2["data"]["result"]["columns"]}')
            print(f'     Top: {res2["data"]["result"]["rows"][:2]}')
        elif res2['status'] == 'error':
            err = res2['data']['message']
            if 'connection' in err.lower() or 'connect' in err.lower() or 'refused' in err.lower():
                fail('top_audited_objects', f'PostgreSQL недоступен: {err}')
            elif 'не найден' in err:
                fail('top_audited_objects', err)
            else:
                fail('top_audited_objects', err)
        else:
            fail('top_audited_objects', json.dumps(res2, ensure_ascii=False, indent=2)[:300])

        # ─────────────────────────────────────────────
        # 3. Predefined mode — ошибка (неизвестный скрипт)
        # ─────────────────────────────────────────────
        print('\n[3] predefined mode — неизвестный скрипт')
        res3 = await predefined_mode.run('nonexistent_script', db)
        assert res3['status'] == 'error'
        assert 'не найден' in res3['data']['message']
        ok(f'unknown script error: {res3["data"]["message"][:60]}...')

        # ─────────────────────────────────────────────
        # 4. Predefined mode — violations_by_type
        # ─────────────────────────────────────────────
        print('\n[4] predefined mode — violations_by_type')
        res4 = await predefined_mode.run('violations_by_type', db, {'violation_code': 'VIOL'})

        if res4['status'] == 'success':
            ok(f'violations_by_type: {res4["data"]["result"]["row_count"]} rows')
            print(f'     Columns: {res4["data"]["result"]["columns"]}')
        elif res4['status'] == 'error':
            err = res4['data']['message']
            if 'connection' in err.lower() or 'connect' in err.lower():
                fail('violations_by_type', f'PostgreSQL недоступен: {err}')
            else:
                fail('violations_by_type', err)
        else:
            fail('violations_by_type', json.dumps(res4, ensure_ascii=False, indent=2)[:300])

    # Database закрыт — sql_mode требует своего подключения
    print('\n[5] sql mode — генерация + EXPLAIN + выполнение')
    async with Database(db_cfg) as db_sql:
        res5 = await sql_mode.run('покажи все таблицы в схеме oarb', db_sql)

        if res5['status'] == 'success':
            ok(f'sql mode: {res5["data"]["result"]["row_count"]} rows')
            print(f'     SQL: {res5["data"]["sql"][:100]}...')
            print(f'     Columns: {res5["data"]["result"]["columns"]}')
        elif res5['status'] == 'error':
            err = res5['data'].get('message', '')
            if 'connection' in err.lower() or 'LLM call' in err or '401' in err or '401' in str(res5):
                fail('sql mode', f'LLM или БД недоступны: {err}')
            else:
                fail('sql mode', err)
        else:
            fail('sql mode', json.dumps(res5, ensure_ascii=False, indent=2)[:300])

    # ─────────────────────────────────────────────
    # 6. Vector mode — FAISS + Ollama embedding
    # ─────────────────────────────────────────────
    print('\n[6] vector mode — FAISS + embedding')

    vpath = get_vector_index_path()

    # Если индекса нет — создаём тестовый FAISS-индекс (ASCII путь, без кириллицы)
    import tempfile
    ascii_tmp = tempfile.gettempdir()
    index_file = os.path.join(vpath, 'audit_index.faiss')

    if not os.path.isfile(index_file):
        tmp_index_dir = os.path.join(ascii_tmp, 'audit_analyzer_e2e_test')
        print(f'     Индекс {index_file} не найден — создаю тестовый в {tmp_index_dir}')
        os.makedirs(tmp_index_dir, exist_ok=True)
        try:
            import numpy as np
            import faiss
            dim = 1024
            index = faiss.IndexFlatIP(dim)
            data = np.random.rand(5, dim).astype(np.float32)
            index.add(data)
            idx_path = os.path.join(tmp_index_dir, 'audit_index.faiss')
            faiss.write_index(index, idx_path)
            print(f'     FAISS индекс: {idx_path} ({os.path.getsize(idx_path)} bytes)')
            meta = {
                "metadata": {
                    "0": {"content": "Нарушение пожарной безопасности: отсутствие огнетушителя", "source": "audit_index", "table": "violations", "pk_value": 1},
                    "1": {"content": "Финансовое нарушение: нецелевое расходование средств", "source": "audit_index", "table": "violations", "pk_value": 2},
                    "2": {"content": "Нарушение трудового законодательства: задержка зарплаты", "source": "audit_index", "table": "violations", "pk_value": 3},
                    "3": {"content": "Экологическое нарушение: превышение выбросов", "source": "audit_index", "table": "violations", "pk_value": 4},
                    "4": {"content": "Пожарная безопасность: неисправная сигнализация", "source": "audit_index", "table": "violations", "pk_value": 5},
                }
            }
            meta_path = os.path.join(tmp_index_dir, 'audit_index_metadata.json')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
            print(f'     Метаданные: {meta_path}')
            vpath = tmp_index_dir
        except ImportError:
            print(f'     faiss/numpy не установлены')
            vpath = None
        except Exception as e:
            print(f'     Ошибка: {e}')
            vpath = None
    else:
        print(f'     Индекс найден: {index_file}')

    if vpath:
        res6 = await vector_mode.run('нарушения пожарной безопасности', 'audit_index', index_path=vpath)
    else:
        res6 = {"status": "error", "data": {"message": "Нет индекса для теста"}}

    if res6['status'] == 'success':
        ok(f'vector mode: {res6["data"]["count"]} results')
        for r in res6['data']['results'][:3]:
            print(f'     score={r["score"]:.3f} content={r["content"][:50]}...')
    elif res6['status'] == 'error':
        err = res6['data']['message']
        if 'директория' in err or 'не найден' in err or 'Embedding' in err:
            fail('vector mode', err)
        elif 'faiss' in str(res6) or 'numpy' in str(res6):
            fail('vector mode', 'Зависимости faiss/numpy не установлены')
        else:
            fail('vector mode', err)
    else:
        fail('vector mode', json.dumps(res6, ensure_ascii=False, indent=2)[:300])


asyncio.run(run_tests())

# ─────────────────────────────────────────────
# Итог
# ─────────────────────────────────────────────
print(f'\n{"="*50}')
print(f'ИТОГО: {passed} ✅ passed, {failed} ❌ failed')
if failed > 0:
    sys.exit(1)
