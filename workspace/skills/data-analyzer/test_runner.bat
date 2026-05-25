@echo off
setlocal enabledelayedexpansion

echo 🧪 Запуск полного цикла тестирования навыка
echo ===========================================

cd /d "%~dp0"

:: 1. Подготовка данных
echo.
echo 1️⃣  Генерация тестовых файлов...
python tests\prepare_test_data.py
if errorlevel 1 (
    echo ❌ Ошибка генерации данных
    exit /b 1
)

:: 2. Запуск интеграционных тестов
echo.
echo 2️⃣  Запуск навыка в разных режимах...
python tests\run_integration_tests.py
set TEST_EXIT=!errorlevel!

:: 3. Валидация (только если тесты прошли)
if !TEST_EXIT! equ 0 (
    echo.
    echo 3️⃣  Валидация ответов...
    python tests\validate_results.py
    set VALID_EXIT=!errorlevel!
) else (
    echo.
    echo ⚠️  Пропускаем валидацию: интеграционные тесты не прошли
    set VALID_EXIT=1
)

:: Итог
echo.
echo ===========================================
if !TEST_EXIT! equ 0 if !VALID_EXIT! equ 0 (
    echo ✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ
    exit /b 0
) else (
    echo ❌ ЕСТЬ ОШИБКИ — проверьте tests/results/
    exit /b 1
)
