"""
Ядро навыка. Явное выполнение 3 режимов без автоматического fallback.
"""
from typing import Dict, Any

class AuditAnalyzer:
    """Ядро навыка: управляет тремя режимами анализа (predefined, vector, sql)
    и маршрутизирует запросы в соответствующий метод."""

    def __init__(self, db, scripts, vector, llm, config):
        """Инициализирует анализатор пятью зависимостями: db — обёртка БД,
        scripts — реестр предопределённых скриптов, vector — векторный индекс,
        llm — LLM-клиент, config — конфигурация навыка."""
        self.db = db
        self.scripts = scripts
        self.vector = vector
        self.llm = llm
        self.config = config

    def execute_mode(self, mode: str, query: str) -> Dict[str, Any]:
        """Маршрутизирует запрос в один из трёх режимов: predefined (подбор скрипта),
        vector (поиск по векторному индексу) или sql (генерация SQL).
        Возвращает словарь со статусом, данными и метаинформацией."""
        base = {"mode": mode, "query": query}
        try:
            if mode == "predefined":
                return {**base, **self._run_predefined(query)}
            elif mode == "vector":
                return {**base, **self._run_vector(query)}
            elif mode == "sql":
                return {**base, **self._run_sql(query)}
            else:
                return {**base, "status": "error", "data": {"message": "Unknown mode"}}
        except Exception as e:
            return {**base, "status": "error", "data": {"message": str(e)}}

    def _run_predefined(self, query: str) -> Dict[str, Any]:
        """Ищет предопределённый скрипт по запросу, извлекает параметры и
        генерирует SQL. Возвращает статус, имя скрипта, SQL и параметры."""
        script = self.scripts.find_matching_script(query, threshold=0.4)
        if not script:
            return {"status": "error", "data": {"message": "No predefined script matches query"}}
        
        params = self.scripts.extract_parameters_from_query(query, script)
        sql = script.generate_sql(params)
        return {"status": "success", "data": {"script_name": script.name, "sql": sql, "parameters": params}}

    def _run_vector(self, query: str) -> Dict[str, Any]:
        """Выполняет поиск по векторному индексу. Если индекс недоступен или
        результаты не найдены, возвращает соответствующее сообщение."""
        if not self.vector.is_available():
            return {"status": "error", "data": {"message": "Vector index not available"}}
        results = self.vector.search(query)
        if not results:
            return {"status": "success", "data": {"message": "No relevant documents found", "count": 0}}
        return {"status": "success", "data": {"results": results, "count": len(results)}}

    def _run_sql(self, query: str) -> Dict[str, Any]:
        """Генерирует SQL по описанию схемы и запросу пользователя, проверяет
        валидность через БД. Возвращает сгенерированный SQL или ошибку валидации."""
        schema_desc = self.db.get_schema_description()
        sql = self.llm.generate_sql(schema_desc, query)
        validation = self.db.validate_sql(sql)
        if validation:
            return {"status": "error", "data": {"message": f"SQL validation failed: {validation}"}}
        # Выполняем только read-only
        return {"status": "success", "data": {"sql": sql, "note": "SQL generated and validated. Execution requires DB access."}}
