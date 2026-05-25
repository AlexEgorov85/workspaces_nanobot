"""
Cache management for Audit Analyzer skill.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from pathlib import Path


class SchemaCache:
    """Manages schema caching with configurable TTL."""
    
    def __init__(self, cache_dir: str = "cache", ttl_seconds: int = 86400):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.cache_file = self.cache_dir / "schema_cache.json"
    
    def _get_cache_key(self, schema_name: str, tables: Optional[list] = None) -> str:
        """Generate cache key based on schema and tables."""
        if tables:
            tables_str = "_".join(sorted(tables))
            return f"schema_{schema_name}_{tables_str}"
        return f"schema_{schema_name}"
    
    def _is_cache_valid(self, timestamp: datetime) -> bool:
        """Check if cache entry is still valid."""
        return datetime.now() - timestamp < timedelta(seconds=self.ttl_seconds)
    
    def get(self, schema_name: str, tables: Optional[list] = None) -> Optional[Dict[str, Any]]:
        """Get cached schema if valid."""
        if not self.cache_file.exists():
            return None
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            cache_key = self._get_cache_key(schema_name, tables)
            
            if cache_key in cache_data:
                entry = cache_data[cache_key]
                timestamp = datetime.fromisoformat(entry["timestamp"])
                
                if self._is_cache_valid(timestamp):
                    return entry["schema"]
                else:
                    # Remove expired entry
                    del cache_data[cache_key]
                    self._save_cache(cache_data)
        
        except (json.JSONDecodeError, KeyError, ValueError):
            # Cache corrupted, remove it
            if self.cache_file.exists():
                self.cache_file.unlink()
        
        return None
    
    def set(self, schema_name: str, schema: Dict[str, Any], tables: Optional[list] = None) -> None:
        """Cache schema with timestamp."""
        cache_key = self._get_cache_key(schema_name, tables)
        
        # Load existing cache
        cache_data = {}
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
            except json.JSONDecodeError:
                cache_data = {}
        
        # Add new entry
        cache_data[cache_key] = {
            "schema": schema,
            "timestamp": datetime.now().isoformat(),
            "schema_name": schema_name,
            "tables": tables or []
        }
        
        self._save_cache(cache_data)
    
    def _save_cache(self, cache_data: Dict[str, Any]) -> None:
        """Save cache to file."""
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
    
    def clear(self) -> None:
        """Clear all cache."""
        if self.cache_file.exists():
            self.cache_file.unlink()
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about cached entries."""
        if not self.cache_file.exists():
            return {"entries": 0, "total_size": 0}
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            entries = []
            total_size = self.cache_file.stat().st_size
            
            for cache_key, entry in cache_data.items():
                timestamp = datetime.fromisoformat(entry["timestamp"])
                is_valid = self._is_cache_valid(timestamp)
                
                entries.append({
                    "key": cache_key,
                    "schema_name": entry["schema_name"],
                    "tables": entry["tables"],
                    "timestamp": entry["timestamp"],
                    "is_valid": is_valid
                })
            
            return {
                "entries": len(entries),
                "total_size": total_size,
                "ttl_seconds": self.ttl_seconds,
                "cache_file": str(self.cache_file),
                "details": entries
            }
        
        except (json.JSONDecodeError, KeyError, ValueError):
            return {"entries": 0, "total_size": 0, "error": "Cache corrupted"}
    
    def cleanup_expired(self) -> int:
        """Remove expired entries and return count of removed items."""
        if not self.cache_file.exists():
            return 0
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            original_count = len(cache_data)
            expired_keys = []
            
            for cache_key, entry in cache_data.items():
                timestamp = datetime.fromisoformat(entry["timestamp"])
                if not self._is_cache_valid(timestamp):
                    expired_keys.append(cache_key)
            
            # Remove expired entries
            for key in expired_keys:
                del cache_data[key]
            
            if cache_data:
                self._save_cache(cache_data)
            else:
                self.cache_file.unlink()
            
            return len(expired_keys)
        
        except (json.JSONDecodeError, KeyError, ValueError):
            # Cache corrupted, remove it
            if self.cache_file.exists():
                self.cache_file.unlink()
            return 1