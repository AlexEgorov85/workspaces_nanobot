"""
Audit Analyzer API
Comprehensive audit analysis tool with predefined scripts, SQL generation, and vector search.
"""

import json
import re
import asyncio
from typing import Dict, Any, Optional, List
from pathlib import Path
import aiohttp
from datetime import datetime, timedelta

from database import DatabaseManager
from predefined_scripts import PredefinedScripts
from vector_integration import AuditVectorSearch


class AuditAnalyzerAPI:
    """Main API class for Audit Analyzer skill."""
    
    def __init__(self, config_path: Optional[Path] = None):
        """Initialize the Audit Analyzer with configuration."""
        self.config_path = config_path or Path(__file__).parent / "config.json"
        self.config = self._load_config()
        
        # Initialize components
        db_config = self.config["settings"]["database"]
        cache_ttl = db_config.get("schema_cache_ttl", 86400)
        self.db_manager = DatabaseManager(
            connection_string=db_config["connection_string"],
            cache_ttl=cache_ttl
        )
        
        self.predefined_scripts = PredefinedScripts()
        self.vector_search = AuditVectorSearch(self.config)
        
        # Cache for schema
        self.schema_cache = {}
        self.cache_timestamp = None
        
        # Detection thresholds
        self.keyword_threshold = self.config["settings"]["script_detection"]["keyword_threshold"]
        self.vector_threshold = self.config["settings"]["script_detection"]["vector_threshold"]
        
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in configuration file: {e}")
    
    async def analyze_query(self, query: str, auto_execute: bool = False) -> Dict[str, Any]:
        """
        Analyze audit query using the best available method.
        
        Priority:
        1. Predefined scripts with parameters
        2. Vector search for relevant documents
        3. Generated SQL with LLM
        """
        try:
            # Try predefined scripts first
            script_result = await self._try_predefined_script(query)
            if script_result["status"] == "success":
                if auto_execute:
                    execution_result = await self.db_manager.execute_query(script_result["sql"])
                    if execution_result["status"] == "success":
                        return {
                            "status": "executed",
                            "method": "predefined_script",
                            "sql": script_result["sql"],
                            "explanation": script_result["explanation"],
                            "script_name": script_result["script_name"],
                            "parameters": script_result["parameters"],
                            "execution": execution_result
                        }
                    else:
                        return {
                            "status": "error",
                            "method": "predefined_script",
                            "sql": script_result["sql"],
                            "error": execution_result["error"]
                        }
                else:
                    return {
                        "status": "generated",
                        "method": "predefined_script",
                        "sql": script_result["sql"],
                        "explanation": script_result["explanation"],
                        "script_name": script_result["script_name"],
                        "parameters": script_result["parameters"]
                    }
            
            # Try vector search
            vector_result = self.vector_search.search_audit_documents(query)
            if vector_result["status"] == "success" and vector_result["total_found"] > 0:
                return {
                    "status": "vector_search",
                    "method": "vector_search",
                    "results": vector_result["results"],
                    "query": query,
                    "total_found": vector_result["total_found"],
                    "explanation": f"Найдено {vector_result['total_found']} релевантных документов по запросу"
                }
            
            # Fall back to SQL generation
            sql_result = await self._generate_sql_from_llm(query)
            if sql_result["status"] == "success":
                if auto_execute:
                    execution_result = await self.db_manager.execute_query(sql_result["sql"])
                    if execution_result["status"] == "success":
                        return {
                            "status": "executed",
                            "method": "generated_sql",
                            "sql": sql_result["sql"],
                            "explanation": sql_result["explanation"],
                            "execution": execution_result
                        }
                    else:
                        return {
                            "status": "error",
                            "method": "generated_sql",
                            "sql": sql_result["sql"],
                            "error": execution_result["error"]
                        }
                else:
                    return {
                        "status": "generated",
                        "method": "generated_sql",
                        "sql": sql_result["sql"],
                        "explanation": sql_result["explanation"]
                    }
            
            return {
                "status": "error",
                "error": "Не удалось обработать запрос. Попробуйте переформулировать."
            }
            
        except Exception as e:
            return {
                "status": "error",
                "error": f"Analysis failed: {e}"
            }
    
    async def _try_predefined_script(self, query: str) -> Dict[str, Any]:
        """Try to match query with predefined scripts."""
        try:
            # Find matching script
            script = self.predefined_scripts.find_matching_script(query, self.keyword_threshold)
            
            if not script:
                return {
                    "status": "no_match",
                    "message": "No matching predefined script found"
                }
            
            # Extract parameters from query
            params = self.predefined_scripts.extract_parameters_from_query(query, script)
            
            # Generate SQL
            sql = script.generate_sql(params)
            
            return {
                "status": "success",
                "sql": sql,
                "explanation": f"Использован заготовленный скрипт: {script.description}",
                "script_name": script.name,
                "parameters": params
            }
            
        except Exception as e:
            return {
                "status": "error",
                "error": f"Script processing failed: {e}"
            }
    
    async def _generate_sql_from_llm(self, query: str) -> Dict[str, Any]:
        """Generate SQL using LLM (fallback method)."""
        try:
            # Get database schema
            schema = await self._get_database_schema()
            
            # Format schema for LLM
            schema_text = self._format_schema_for_llm(schema)
            
            # Create prompts
            system_prompt = """You are a PostgreSQL expert. Generate safe, read-only SQL queries for audit analysis.
            
Rules:
- Use only SELECT, WITH, SHOW queries
- Join tables properly using foreign keys
- Handle NULL values appropriately
- Use proper date functions
- Order results meaningfully
- Add descriptive column aliases"""
            
            user_prompt = f"""Database Schema:
{schema_text}

User Request: {query}

Generate a PostgreSQL query to answer this request. Return only the SQL query without explanation."""
            
            # Call LLM API
            llm_result = await self._call_llm_api(user_prompt, system_prompt)
            
            if llm_result["status"] != "success":
                return {
                    "status": "error",
                    "error": f"LLM API error: {llm_result['error']}"
                }
            
            sql = llm_result["content"].strip()
            
            # Validate SQL
            validation_error = self._validate_sql(sql, schema)
            if validation_error:
                return {
                    "status": "error",
                    "error": validation_error
                }
            
            return {
                "status": "success",
                "sql": sql,
                "explanation": "SQL-запрос сгенерирован нейросетью на основе схемы базы данных"
            }
            
        except Exception as e:
            return {
                "status": "error",
                "error": f"SQL generation failed: {e}"
            }
    
    async def _call_llm_api(self, prompt: str, system_prompt: str) -> Dict[str, Any]:
        """Call external LLM API for SQL generation."""
        llm_config = self.config["settings"]["llm"]
        
        headers = {
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": llm_config["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": llm_config["temperature"],
            "max_tokens": llm_config["max_tokens"]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{llm_config['base_url']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return {
                            "status": "error",
                            "error": f"LLM API error: {response.status} - {error_text}"
                        }
                    
                    result = await response.json()
                    
                    if "choices" not in result or not result["choices"]:
                        return {
                            "status": "error",
                            "error": "Invalid LLM API response format"
                        }
                    
                    content = result["choices"][0]["message"]["content"]
                    
                    return {
                        "status": "success",
                        "content": content
                    }
                    
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "error": "LLM API timeout"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"LLM API error: {e}"
            }
    
    async def _get_database_schema(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get database schema with caching."""
        try:
            db_config = self.config["settings"]["database"]
            schema_name = db_config.get("schema", "public")
            tables = db_config.get("tables")
            
            # Use database manager's built-in caching
            schema = await self.db_manager.get_schema(
                schema_name=schema_name, 
                tables=tables,
                force_refresh=force_refresh
            )
            
            # Keep backward compatibility with old cache
            self.schema_cache = schema
            self.cache_timestamp = datetime.now()
            
            return schema
        except Exception as e:
            raise Exception(f"Failed to fetch database schema: {e}")
    
    def _format_schema_for_llm(self, schema: Dict[str, Any]) -> str:
        """Format database schema for LLM consumption."""
        formatted = []
        for table_name, table_info in schema.get("tables", {}).items():
            table_comment = table_info.get("comment", "")
            if table_comment:
                formatted.append(f"Table: {table_name} - {table_comment}")
            else:
                formatted.append(f"Table: {table_name}")
            
            columns = []
            for col_name, col_info in table_info.get("columns", {}).items():
                col_type = col_info.get("type", "unknown")
                col_comment = col_info.get("comment", "")
                
                if col_comment:
                    columns.append(f"  {col_name}: {col_type} - {col_comment}")
                else:
                    columns.append(f"  {col_name}: {col_type}")
            
            formatted.append("\n".join(columns))
        
        return "\n\n".join(formatted)
    
    def _validate_sql(self, sql: str, schema: Dict[str, Any]) -> Optional[str]:
        """Validate generated SQL for safety."""
        sql_upper = sql.upper().strip()
        
        # Only allow read-only queries
        if not sql_upper.startswith(("SELECT", "WITH", "SHOW")):
            return "❌ Only read-only queries (SELECT/WITH/SHOW) are allowed."
        
        # Check table existence against configured tables
        db_config = self.config["settings"]["database"]
        configured_tables = set(t.lower() for t in db_config.get("tables", []))
        schema_tables = {t.lower() for t in schema.get("tables", {}).keys()}
        
        # Use configured tables if specified, otherwise use schema tables
        allowed_tables = configured_tables if configured_tables else schema_tables
        
        found_tables = set(re.findall(r'(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)', sql_upper))
        invalid_tables = found_tables - allowed_tables
        
        if invalid_tables:
            return f"❌ Unknown tables: {', '.join(invalid_tables)}. Allowed tables: {', '.join(allowed_tables)}"
        
        return None
    
    async def get_schema_info(self) -> Dict[str, Any]:
        """Get database schema information."""
        try:
            schema = await self._get_database_schema()
            return {
                "status": "success",
                "schema": schema,
                "cached_at": self.cache_timestamp.isoformat() if self.cache_timestamp else None
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def refresh_schema(self) -> Dict[str, Any]:
        """Force refresh database schema cache."""
        schema = await self._get_database_schema(force_refresh=True)
        return {
            "status": "success",
            "message": "Schema cache refreshed",
            "schema": schema,
            "cached_at": self.cache_timestamp.isoformat()
        }
    
    def get_available_scripts(self) -> Dict[str, Any]:
        """Get list of available predefined scripts."""
        scripts = self.predefined_scripts.list_all_scripts()
        return {
            "status": "success",
            "scripts": scripts,
            "total_count": len(scripts)
        }
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about schema cache."""
        return self.db_manager.get_cache_info()
    
    def clear_cache(self) -> None:
        """Clear all schema cache."""
        self.db_manager.clear_cache()
        self.schema_cache = {}
        self.cache_timestamp = None
    
    def cleanup_cache(self) -> int:
        """Remove expired cache entries."""
        return self.db_manager.cleanup_cache()
    
            """Create vector index from audit database."""
        return self.vector_search.create_audit_index_from_database(self.db_manager)
    
    def get_vector_index_info(self) -> Dict[str, Any]:
        """Get vector search index information."""
        return self.vector_search.get_index_info()


# Convenience function for direct usage
async def analyze_audit_query(
    query: str, 
    config_path: Optional[str] = None,
    auto_execute: bool = False
) -> Dict[str, Any]:
    """
    Analyze audit query using the best available method.
    
    Args:
        query: Natural language query about audit data
        config_path: Path to configuration file
        auto_execute: Whether to automatically execute generated SQL
    
    Returns:
        Analysis result with SQL, vector search results, or error
    """
    api = AuditAnalyzerAPI(config_path)
    return await api.analyze_query(query, auto_execute)