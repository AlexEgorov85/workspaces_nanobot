"""
Database utilities for Audit Analyzer skill.
"""

import asyncio
import asyncpg
from typing import Dict, Any, List, Optional
from datetime import datetime
from cache import SchemaCache


class DatabaseManager:
    """Manages database connections and schema operations."""
    
    def __init__(self, connection_string: str, cache_ttl: int = 86400):
        self.connection_string = connection_string
        self._pool = None
        self.cache = SchemaCache(ttl_seconds=cache_ttl)
    
    async def create_pool(self):
        """Create database connection pool."""
        if not self._pool:
            self._pool = await asyncpg.create_pool(
                self.connection_string,
                min_size=1,
                max_size=5,
                command_timeout=60
            )
        return self._pool
        return self._pool
    
    async def close_pool(self):
        """Close database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
    
    async def execute_query(self, query: str) -> Dict[str, Any]:
        """Execute a read-only SQL query and return results."""
        try:
            pool = await self.create_pool()
            
            async with pool.acquire() as conn:
                # Validate query is read-only
                query_upper = query.upper().strip()
                if not query_upper.startswith(("SELECT", "WITH", "SHOW")):
                    return {
                        "status": "error",
                        "error": "Only read-only queries (SELECT/WITH/SHOW) are allowed"
                    }
                
                # Execute query
                result = await conn.fetch(query)
                
                # Convert to list of dicts
                rows = []
                for row in result:
                    rows.append(dict(row))
                
                return {
                    "status": "success",
                    "row_count": len(rows),
                    "rows": rows,
                    "query": query
                }
                
        except asyncpg.PostgresError as e:
            return {
                "status": "error",
                "error": f"Database error: {e}"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Query execution failed: {e}"
            }
    
    async def get_schema(self, schema_name: str = "public", tables: Optional[List[str]] = None, force_refresh: bool = False) -> Dict[str, Any]:
        """Get database schema information with caching."""
        # Try to get from cache first
        if not force_refresh:
            cached_schema = self.cache.get(schema_name, tables)
            if cached_schema:
                return cached_schema
        
        # Fetch from database
        pool = await self.create_pool()
        
        async with pool.acquire() as conn:
            # Get tables - either specified list or all from schema
            if tables:
                # Get only specified tables
                tables_query = """
                    SELECT table_name, table_type, obj_description(c.oid) as table_comment
                    FROM information_schema.tables t
                    JOIN pg_class c ON c.relname = t.table_name
                    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = t.table_schema
                    WHERE t.table_schema = $1
                    AND t.table_type = 'BASE TABLE'
                    AND t.table_name = ANY($2)
                    ORDER BY t.table_name
                """
                tables_data = await conn.fetch(tables_query, schema_name, tables)
            else:
                # Get all tables from schema
                tables_query = """
                    SELECT table_name, table_type, obj_description(c.oid) as table_comment
                    FROM information_schema.tables t
                    JOIN pg_class c ON c.relname = t.table_name
                    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = t.table_schema
                    WHERE t.table_schema = $1
                    AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name
                """
                tables_data = await conn.fetch(tables_query, schema_name)
            
            schema = {"tables": {}}
            
            for table_record in tables_data:
                table_name = table_record["table_name"]
                
                # Get columns for this table with comments
                columns_query = """
                    SELECT 
                        c.column_name, 
                        c.data_type, 
                        c.is_nullable, 
                        c.column_default,
                        col_description(pgc.oid, c.ordinal_position) as column_comment
                    FROM information_schema.columns c
                    JOIN pg_class pgc ON pgc.relname = c.table_name
                    JOIN pg_namespace n ON n.oid = pgc.relnamespace AND n.nspname = c.table_schema
                    WHERE c.table_name = $1
                    AND c.table_schema = $2
                    ORDER BY c.ordinal_position
                """
                columns = await conn.fetch(columns_query, table_name, schema_name)
                
                schema["tables"][table_name] = {
                    "comment": table_record["table_comment"],
                    "columns": {}
                }
                
                for column in columns:
                    col_name = column["column_name"]
                    schema["tables"][table_name]["columns"][col_name] = {
                        "type": column["data_type"],
                        "nullable": column["is_nullable"] == "YES",
                        "default": column["column_default"],
                        "comment": column["column_comment"]
                    }
            
            # Cache the schema
            self.cache.set(schema_name, schema, tables)
            
            return schema
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get cache information."""
        return self.cache.get_cache_info()
    
    def clear_cache(self) -> None:
        """Clear schema cache."""
        self.cache.clear()
    
    def cleanup_cache(self) -> int:
        """Remove expired cache entries."""
        return self.cache.cleanup_expired()