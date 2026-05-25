"""
Ядро навыка. Явное выполнение 3 режимов без автоматического fallback.
"""
from typing import Dict, Any

class AuditAnalyzer:
    def __init__(self, db, scripts, vector, llm, config):
        self.db = db
        self.scripts = scripts
        self.vector = vector
        self.llm = llm
        self.config = config

    def execute_mode(self, mode: str, query: str) -> Dict[str, Any]:
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
        script = self.scripts.find_matching_script(query, threshold=0.4)
        if not script:
            return {"status": "error", "data": {"message": "No predefined script matches query"}}
        
        params = self.scripts.extract_parameters_from_query(query, script)
        sql = script.generate_sql(params)
        return {"status": "success", "data": {"script_name": script.name, "sql": sql, "parameters": params}}

    def _run_vector(self, query: str) -> Dict[str, Any]:
        if not self.vector.is_available():
            return {"status": "error", "data": {"message": "Vector index not available"}}
        results = self.vector.search(query)
        if not results:
            return {"status": "success", "data": {"message": "No relevant documents found", "count": 0}}
        return {"status": "success", "data": {"results": results, "count": len(results)}}

    def _run_sql(self, query: str) -> Dict[str, Any]:
        schema_desc = self.db.get_schema_description()
        sql = self.llm.generate_sql(schema_desc, query)
        validation = self.db.validate_sql(sql)
        if validation:
            return {"status": "error", "data": {"message": f"SQL validation failed: {validation}"}}
        # Выполняем только read-only
        return {"status": "success", "data": {"sql": sql, "note": "SQL generated and validated. Execution requires DB access."}}
