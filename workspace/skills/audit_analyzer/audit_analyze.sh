#!/usr/bin/env bash
# audit_analyze.sh — кросс-платформенный entrypoint навыка audit_analyzer.
#
# На Linux: ``python`` часто отсутствует (только ``python3``); на Windows +
# Git Bash / WSL — ``python`` указывает на python.exe. Пытаемся сначала
# ``python3`` (Linux-style), потом ``python`` (Windows-style), и только
# потом сдаёмся. Также учитываем переопределение через ``$PYTHON``.
#
# ВАЖНО: задаём ``umask 022`` до ``chmod +x``, чтобы файлы оставались
# читаемыми группой/всеми (на Linux-серверах с umask 077 по умолчанию
# ``chmod +x`` может выдать 0xxx, который всё равно не запустится у
# других пользователей).

set -e

cd "$(dirname "$0")" || exit 1

# 1) Найти интерпретатор Python.
PYTHON_CMD="${PYTHON:-}"
if [ -z "$PYTHON_CMD" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_CMD="python"
    else
        echo "Ошибка: Python не найден (нужен python3 или python в PATH)." >&2
        exit 1
    fi
fi

# 2) Сделать .py исполняемыми (на Linux — нужно; на Windows — no-op).
umask 022 2>/dev/null || true
chmod +x scripts/*.py 2>/dev/null || true

# 3) Установить UTF-8 локаль, чтобы вывод skill'а не падал на
#    Cyrillic в Linux-контейнерах с POSIX/C локалью.
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

exec "$PYTHON_CMD" scripts/cli.py "$@"
