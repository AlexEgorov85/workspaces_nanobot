"""
Predefined audit analysis scripts with parameters.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import re


class PredefinedScript:
    """Represents a predefined audit script with parameters."""
    
    def __init__(self, name: str, description: str, sql_template: str, 
                 parameters: Dict[str, Any], keywords: List[str]):
        self.name = name
        self.description = description
        self.sql_template = sql_template
        self.parameters = parameters
        self.keywords = keywords
    
    def matches_query(self, query: str, threshold: float = 0.8) -> bool:
        """Check if this script matches the user query."""
        query_lower = query.lower()
        keyword_matches = 0
        
        for keyword in self.keywords:
            if keyword.lower() in query_lower:
                keyword_matches += 1
        
        match_ratio = keyword_matches / len(self.keywords)
        return match_ratio >= threshold
    
    def generate_sql(self, params: Dict[str, Any]) -> str:
        """Generate SQL by substituting parameters."""
        sql = self.sql_template
        
        # Replace parameter placeholders
        for param_name, param_value in params.items():
            placeholder = f"{{{param_name}}}"

            if param_value is None or param_value == "":
                # Remove empty parameter placeholders entirely
                if f" AND {placeholder}" in sql:
                    sql = sql.replace(f" AND {placeholder}", "")
                elif f" WHERE {placeholder}" in sql:
                    sql = sql.replace(f" WHERE {placeholder}", "WHERE")
                elif f" HAVING {placeholder}" in sql:
                    sql = sql.replace(f" HAVING {placeholder}", "")
                else:
                    sql = sql.replace(placeholder, "")
            elif isinstance(param_value, str):
                sql = sql.replace(placeholder, param_value)
            elif isinstance(param_value, (int, float)):
                sql = sql.replace(placeholder, str(param_value))
            elif isinstance(param_value, list):
                # Handle list parameters (for IN clauses)
                if param_value:
                    formatted_items = []
                    for item in param_value:
                        if isinstance(item, str):
                            formatted_items.append(f"'{item}'")
                        else:
                            formatted_items.append(str(item))
                    sql = sql.replace(placeholder, f"({', '.join(formatted_items)})")
                else:
                    sql = sql.replace(placeholder, "(NULL)")
            else:
                sql = sql.replace(placeholder, str(param_value))
        
        return sql


class PredefinedScripts:
    """Collection of predefined audit analysis scripts."""
    
    def __init__(self):
        self.scripts = self._initialize_scripts()
    
    def _initialize_scripts(self) -> List[PredefinedScript]:
        """Initialize all predefined scripts."""
        return [
            # Аналитика по годам и месяцам
            PredefinedScript(
                name="analytics_by_year_month",
                description="Аналитика проверок по годам и месяцам",
                sql_template="""
                SELECT
                    EXTRACT(YEAR FROM actual_date) as audit_year,
                    EXTRACT(MONTH FROM actual_date) as audit_month,
                    COUNT(*) as audit_count,
                    TO_CHAR(actual_date, 'Month') as month_name
                FROM oarb.audits
                WHERE actual_date IS NOT NULL
                {year_filter}
                GROUP BY audit_year, audit_month, TO_CHAR(actual_date, 'Month')
                ORDER BY audit_year DESC, audit_month
                """,
                parameters={
                    "year_filter": ""  # Optional: "AND EXTRACT(YEAR FROM actual_date) = 2024"
                },
                keywords=["год", "месяц", "аналитика", "статистика", "количество", "проверок", "проведено"]
            ),
            
            # Нарушения по типам
            PredefinedScript(
                name="violations_by_type",
                description="Анализ нарушений по типам и категориям",
                sql_template="""
                SELECT 
                    v.violation_type,
                    v.violation_category,
                    COUNT(*) as violation_count,
                    COUNT(DISTINCT v.audit_id) as affected_audits
                FROM oarb.violations v
                JOIN oarb.audits a ON v.audit_id = a.id
                WHERE a.actual_date IS NOT NULL
                ${date_filter}
                ${type_filter}
                GROUP BY v.violation_type, v.violation_category
                ORDER BY violation_count DESC
                """,
                parameters={
                    "date_filter": "",  # Optional: "AND a.actual_date >= '2024-01-01'"
                    "type_filter": ""   # Optional: "AND v.violation_type IN ('финансовые', 'административные')"
                },
                keywords=["нарушение", "тип", "категория", "статистика", "анализ"]
            ),
            
            # Топ проверяемых объектов
            PredefinedScript(
                name="top_audited_objects",
                description="Топ объектов по количеству проверок",
                sql_template="""
                SELECT 
                    a.audited_object,
                    COUNT(*) as audit_count,
                    COUNT(DISTINCT EXTRACT(YEAR FROM a.actual_date)) as years_covered,
                    MAX(a.actual_date) as last_audit_date
                FROM oarb.audits a
                WHERE a.actual_date IS NOT NULL
                AND a.audited_object IS NOT NULL
                ${object_filter}
                ${date_filter}
                GROUP BY a.audited_object
                ORDER BY audit_count DESC
                LIMIT ${limit}
                """,
                parameters={
                    "object_filter": "",  # Optional: "AND a.audited_object ILIKE '%университет%'"
                    "date_filter": "",    # Optional: "AND a.actual_date >= '2024-01-01'"
                    "limit": 10
                },
                keywords=["топ", "объект", "проверяемый", "рейтинг", "количество"]
            ),
            
            # Эффективность проверок
            PredefinedScript(
                name="audit_effectiveness",
                description="Оценка эффективности проверок по количеству выявленных нарушений",
                sql_template="""
                SELECT 
                    a.id as audit_id,
                    a.title as audit_title,
                    a.actual_date,
                    COUNT(v.id) as violations_count,
                    COUNT(DISTINCT v.violation_type) as violation_types_count,
                    CASE 
                        WHEN COUNT(v.id) = 0 THEN 'Без нарушений'
                        WHEN COUNT(v.id) <= 3 THEN 'Минимальные нарушения'
                        WHEN COUNT(v.id) <= 10 THEN 'Умеренные нарушения'
                        ELSE 'Множественные нарушения'
                    END as severity_level
                FROM oarb.audits a
                LEFT JOIN oarb.violations v ON a.id = v.audit_id
                WHERE a.actual_date IS NOT NULL
                ${date_filter}
                ${severity_filter}
                GROUP BY a.id, a.title, a.actual_date
                ORDER BY violations_count DESC, a.actual_date DESC
                """,
                parameters={
                    "date_filter": "",      # Optional: "AND a.actual_date >= '2024-01-01'"
                    "severity_filter": ""   # Optional: "HAVING COUNT(v.id) > 0"
                },
                keywords=["эффективность", "результат", "нарушение", "оценка", "качество"]
            ),
            
            # Динамика проверок по периодам
            PredefinedScript(
                name="audit_dynamics",
                description="Динамика проведения проверок по периодам",
                sql_template="""
                SELECT 
                    CASE 
                        WHEN ${period} = 'quarter' THEN 
                            EXTRACT(YEAR FROM actual_date) || '-Q' || EXTRACT(QUARTER FROM actual_date)
                        WHEN ${period} = 'week' THEN 
                            EXTRACT(YEAR FROM actual_date) || '-W' || EXTRACT(WEEK FROM actual_date)
                        ELSE 
                            EXTRACT(YEAR FROM actual_date) || '-' || LPAD(EXTRACT(MONTH FROM actual_date)::text, 2, '0')
                    END as period,
                    COUNT(*) as audit_count,
                    COUNT(DISTINCT a.audited_object) as unique_objects,
                    COUNT(v.id) as total_violations
                FROM oarb.audits a
                LEFT JOIN oarb.violations v ON a.id = v.audit_id
                WHERE a.actual_date IS NOT NULL
                ${date_filter}
                GROUP BY period
                ORDER BY period DESC
                """,
                parameters={
                    "period": "month",     # Options: "month", "quarter", "week"
                    "date_filter": ""      # Optional: "AND a.actual_date >= '2024-01-01'"
                },
                keywords=["динамика", "период", "тренд", "график", "временной"]
            ),
            
            # Статистика по типам проверок
            PredefinedScript(
                name="audit_types_stats",
                description="Статистика по типам проводимых проверок",
                sql_template="""
                SELECT 
                    a.audit_type,
                    COUNT(*) as audit_count,
                    COUNT(DISTINCT a.audited_object) as unique_objects,
                    COUNT(v.id) as total_violations,
                    AVG(v.severity_score) as avg_severity,
                    MAX(a.actual_date) as last_audit_date
                FROM oarb.audits a
                LEFT JOIN oarb.violations v ON a.id = v.audit_id
                WHERE a.actual_date IS NOT NULL
                AND a.audit_type IS NOT NULL
                ${type_filter}
                ${date_filter}
                GROUP BY a.audit_type
                ORDER BY audit_count DESC
                """,
                parameters={
                    "type_filter": "",  # Optional: "AND a.audit_type IN ('финансовый', 'комплексный')"
                    "date_filter": ""   # Optional: "AND a.actual_date >= '2024-01-01'"
                },
                keywords=["тип", "проверка", "статистика", "классификация", "категория"]
            )
        ]
    
    def find_matching_script(self, query: str, threshold: float = 0.8) -> Optional[PredefinedScript]:
        """Find the best matching script for the query."""
        best_match = None
        best_score = 0
        
        for script in self.scripts:
            if script.matches_query(query, threshold):
                # Calculate match score
                query_lower = query.lower()
                keyword_matches = sum(1 for keyword in script.keywords if keyword.lower() in query_lower)
                score = keyword_matches / len(script.keywords)
                
                if score > best_score:
                    best_score = score
                    best_match = script
        
        return best_match
    
    def get_script_by_name(self, name: str) -> Optional[PredefinedScript]:
        """Get script by name."""
        for script in self.scripts:
            if script.name == name:
                return script
        return None
    
    def list_all_scripts(self) -> List[Dict[str, Any]]:
        """List all available scripts."""
        return [
            {
                "name": script.name,
                "description": script.description,
                "parameters": list(script.parameters.keys()),
                "keywords": script.keywords
            }
            for script in self.scripts
        ]
    
    def extract_parameters_from_query(self, query: str, script: PredefinedScript) -> Dict[str, Any]:
        """Extract parameter values from user query."""
        params = {}
        query_lower = query.lower()
        
        # Extract year
        year_match = re.search(r'(\d{4})', query)
        if year_match and 'year_filter' in script.parameters:
            year = year_match.group(1)
            params['year_filter'] = f"AND EXTRACT(YEAR FROM actual_date) = {year}"
        
        # Extract date range
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', query)
        if date_match and 'date_filter' in script.parameters:
            date = date_match.group(1)
            params['date_filter'] = f"AND a.actual_date >= '{date}'"
        
        # Extract limit
        limit_match = re.search(r'топ\s*(\d+)', query_lower)
        if limit_match and 'limit' in script.parameters:
            params['limit'] = int(limit_match.group(1))
        elif 'limit' in script.parameters:
            params['limit'] = 10  # Default limit
        
        # Extract period
        if 'period' in script.parameters:
            if 'квартал' in query_lower or 'quarter' in query_lower:
                params['period'] = 'quarter'
            elif 'недел' in query_lower or 'week' in query_lower:
                params['period'] = 'week'
            else:
                params['period'] = 'month'  # Default
        
        # Extract object filter
        object_match = re.search(r'объект[а-я]*\s*["\']([^"\']+)["\']', query_lower)
        if object_match and 'object_filter' in script.parameters:
            object_name = object_match.group(1)
            params['object_filter'] = f"AND a.audited_object ILIKE '%{object_name}%'"
        
        # Extract violation types
        if 'type_filter' in script.parameters:
            violation_types = []
            type_keywords = ['финансов', 'административ', 'кадров', 'налог', 'санитар']
            for keyword in type_keywords:
                if keyword in query_lower:
                    violation_types.append(f"'{keyword}ые'")
            
            if violation_types:
                params['type_filter'] = f"AND v.violation_type IN ({', '.join(violation_types)})"
        
        return params