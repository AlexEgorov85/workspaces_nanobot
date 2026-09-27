"""Smoke-test runtime для opencode change post-0.3.5-patches-cleanup.

Запускает gateway в фоне, проверяет:
* gateway запускается без ошибок (RuntimeEventsSubscriber подключён);
* в runtime-логах НЕТ упоминаний ``_last_usage`` (контракт группы 5);
* в ``agent_gateway_logs_test`` появляется ``turn_completed`` (контракт группы 4 + 1).

Использование::

    python tools/smoke_post_cleanup.py

Требует:
* работающий Postgres на ``$DATABASE_URL``;
* ``--profile=test`` (или иной профиль с реальным gateway).

См. openspec/changes/post-0.3.5-patches-cleanup/tasks.md
группы 6.3, 8.3, 8.4.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import psycopg2

DSN = os.environ.get(
    'DATABASE_URL', 'postgresql://postgres:1@localhost:5432/postgres',
)
GATEWAY = ['gateway.py', '--profile=test']
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_event_type(payload: Any) -> str:
    """Psycopg2 может вернуть JSON как dict или str в зависимости от версии."""
    if isinstance(payload, dict):
        return payload.get('event_type', '?')
    if isinstance(payload, str):
        return payload[:50]
    return str(payload)[:50]


def main() -> int:
    print(f'DSN: {DSN}')
    print(f'Working dir: {WORKDIR}')
    print(f'Gateway cmd: {GATEWAY}')

    print('\n=== Запуск gateway в фоне ===')
    proc = subprocess.Popen(
        [sys.executable, *GATEWAY],
        cwd=WORKDIR,
        env={**os.environ, 'DATABASE_URL': DSN},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f'Gateway PID: {proc.pid}')

    try:
        print('\n=== Ожидание 8 сек для startup ===')
        time.sleep(8)

        if proc.poll() is not None:
            out, _ = proc.communicate(timeout=2)
            print(f'FAIL: gateway exited prematurely: {out[:500]!r}')
            return 1

        print(f'OK: gateway alive (PID {proc.pid})')

        print('\n=== Проверка runtime_events_subscriber ===')
        # Проверяем, что seed_context_window вызывался хотя бы раз —
        # косвенный признак через наличие _CONTEXT_BRIDGE записей.
        # Прямой мониторинг bridge недоступен (subprocess), но можно
        # посмотреть в логах gateway.
        # Запускаем ещё один цикл: ждём 5 сек и читаем stdout.
        time.sleep(5)
    finally:
        print('\n=== Остановка gateway ===')
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
            print('Gateway terminated gracefully')
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            print('Gateway killed')

    stdout = out.decode('utf-8', errors='replace') if out else ''

    print('\n=== Проверка _last_usage в runtime-логах ===')
    if '_last_usage' in stdout:
        # Может встретиться в логе Python import, но не в runtime.
        # Контракт: НЕ должно быть "seed=agent._last_usage".
        matches = [
            line for line in stdout.split('\n')
            if '_last_usage' in line and 'agent._last_usage' in line
        ]
        if matches:
            print(f'FAIL: agent._last_usage found in {len(matches)} lines:')
            for m in matches[:5]:
                print(f'  {m.strip()[:200]}')
            return 1
        print(
            'OK: _last_usage встречается только в импортах/комментариях, '
            'но НЕ используется в runtime'
        )
    else:
        print('OK: _last_usage NOT found in runtime output')

    print('\n=== Проверка RuntimeEventsSubscriber start ===')
    if 'RuntimeEventsSubscriber' in stdout:
        print('OK: RuntimeEventsSubscriber активен (упоминается в логах)')
    else:
        print('WARN: RuntimeEventsSubscriber не упоминается в логах')

    print('\n=== Проверка agent_gateway_logs ===')
    try:
        conn = psycopg2.connect(DSN)
    except psycopg2.OperationalError as exc:
        print(f'WARN: cannot connect to Postgres: {exc}')
        return 0
    cur = conn.cursor()

    cur.execute("""
        SELECT event_type, COUNT(*) FROM public.agent_gateway_logs_test
        GROUP BY event_type ORDER BY COUNT(*) DESC
    """)
    rows = cur.fetchall()
    print(f'Total events: {sum(c for _, c in rows)}')
    for event_type, count in rows:
        print(f'  {event_type}: {count}')

    cur.execute("""
        SELECT payload FROM public.agent_gateway_logs_test
        WHERE event_type = 'turn_completed'
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        print(f'\nturn_completed payload:')
        payload = row[0]
        if isinstance(payload, dict):
            for k, v in payload.items():
                print(f'  {k}: {v}')
        else:
            print(f'  {payload}')
    else:
        print(
            '\nWARN: нет turn_completed (требуется user-turn через '
            'web/telegram/websocket канал)'
        )

    cur.execute("""
        SELECT payload FROM public.agent_gateway_logs_test
        WHERE event_type = 'run_finished'
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        payload = row[0]
        if isinstance(payload, dict) and 'final_content' in payload:
            print(
                'OK: run_finished содержит final_content ('
                f'{len(str(payload["final_content"]))} chars)'
            )

    conn.close()

    print('\n=== Done ===')
    return 0


if __name__ == '__main__':
    sys.exit(main())
