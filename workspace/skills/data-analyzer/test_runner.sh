#!/bin/bash

echo "🧪 Запуск полного цикла тестирования навыка"
echo "==========================================="

cd "$(dirname "$0")"

# 1. Подготовка данных
echo -e "\n1️⃣  Генерация тестовых файлов..."
python tests/prepare_test_data.py || { echo "❌ Ошибка генерации данных"; exit 1; }

# 2. Запуск интеграционных тестов
echo -e "\n2️⃣  Запуск навыка в разных режимах..."
python tests/run_integration_tests.py
TEST_EXIT=$?

# 3. Валидация результатов (только если тесты прошли)
if [ $TEST_EXIT -eq 0 ]; then
    echo -e "\n3️⃣  Валидация ответов..."
    python tests/validate_results.py
    VALID_EXIT=$?
else
    echo -e "\n⚠️  Пропускаем валидацию: интеграционные тесты не прошли"
    VALID_EXIT=1
fi

# Итог
echo -e "\n==========================================="
if [ $TEST_EXIT -eq 0 ] && [ $VALID_EXIT -eq 0 ]; then
    echo "✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ"
    exit 0
else
    echo "❌ ЕСТЬ ОШИБКИ — проверьте тесты/results/"
    exit 1
fi