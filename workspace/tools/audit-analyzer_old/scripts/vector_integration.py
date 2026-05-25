"""
Vector search integration for audit analysis.
"""

import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
import json

# Add skills directory to path for vector-search import
skills_path = Path(__file__).parent.parent.parent
sys.path.append(str(skills_path))

try:
    from vector_search import VectorSearch
    VECTOR_SEARCH_AVAILABLE = True
except ImportError:
    VECTOR_SEARCH_AVAILABLE = False
    print("Warning: vector-search skill not available")


class AuditVectorSearch:
    """Vector search integration for audit documents.

    Поддерживает только поиск по готовому индексу. Создание индексов не поддерживается.
    """

    def __init__(self, config: Dict[str, Any], vector_dbs: Optional[Dict[str, Dict[str, Any]]] = None):
        self.config = config
        self.vector_search = None
        self.config = config
        self.vector_dbs = vector_dbs or {}
        self.current_db = None
        self.vector_search = None
        self.index_path = None
        self.top_k = None
        self.similarity_threshold = None

        # Проверка существования индекса при инициализации
        import os
        if not self.vector_dbs:
            raise ValueError("No vector databases configured")

        # Set default vector DB if not specified
        if "default" not in self.vector_dbs:
            if len(self.vector_dbs) == 1:
                self.vector_dbs["default"] = next(iter(self.vector_dbs.values()))
            else:
                raise ValueError("Multiple vector databases configured but no default specified")

        self.current_db = "default"
        self._set_current_db_config(self.vector_dbs[self.current_db])

        if VECTOR_SEARCH_AVAILABLE:
            self._initialize_vector_search()

    def _set_current_db_config(self, db_config: Dict[str, Any]):
        """Set configuration for the current vector database."""
        self.index_path = db_config["index_path"]
        self.top_k = db_config["top_k"]
        self.similarity_threshold = db_config["similarity_threshold"]

        # Initialize vector search with new config
        self._initialize_vector_search()

    def _initialize_vector_search(self):
        """Initialize vector search component."""
        try:
            self.vector_search = VectorSearch(self.index_path)
        except Exception as e:
            print(f"Failed to initialize vector search: {e}")
            self.vector_search = None

    def is_available(self) -> bool:
        """Check if vector search is available."""
        return VECTOR_SEARCH_AVAILABLE and self.vector_search is not None

    def search_audit_documents(self, query: str, db_name: Optional[str] = None, search_mode: str = "top_k", threshold: Optional[float] = None) -> Dict[str, Any]:
        """Search audit-related documents using vector search."""
        if db_name:
            if db_name not in self.vector_dbs:
                return {
                    "status": "error",
                    "error": f"Vector database '{db_name}' not configured"
                }
            self._set_current_db_config(self.vector_dbs[db_name])

        if not self.is_available():
            return {
                "status": "error",
                "error": "Vector search not available"
            }

        try:
            # Perform vector search
            search_params = {
                "query": query,
                "top_k": self.top_k if search_mode == "top_k" else None,
                "threshold": threshold if threshold is not None else self.similarity_threshold
            }

            # Remove None values from search_params
            search_params = {k: v for k, v in search_params.items() if v is not None}

            results = self.vector_search.search(**search_params)

            if not results.get("results"):
                return {
                    "status": "no_results",
                    "message": "No relevant audit documents found",
                    "query": query
                }

            # Process results
            processed_results = []
            for result in results["results"]:
                processed_results.append({
                    "content": result.get("content", ""),
                    "score": result.get("score", 0),
                    "metadata": result.get("metadata", {}),
                    "source": result.get("source", "unknown")
                })

            return {
                "status": "success",
                "results": processed_results,
                "query": query,
                "total_found": len(processed_results)
            }

        except Exception as e:
            return {
                "status": "error",
                "error": f"Vector search failed: {e}"
            }

    def get_index_info(self, db_name: Optional[str] = None) -> Dict[str, Any]:
        """Get information about the vector index."""
        if db_name:
            if db_name not in self.vector_dbs:
                return {
                    "status": "error",
                    "error": f"Vector database '{db_name}' not configured"
                }
            self._set_current_db_config(self.vector_dbs[db_name])

        if not self.is_available():
            return {
                "status": "error",
                "error": "Vector search not available"
            }

        try:
            # Get index statistics
            info = self.vector_search.get_index_info()

            return {
                        "status": "success",
                        "current_db": self.current_db,
                        "index_path": self.index_path,
                        "top_k": self.top_k,
                        "similarity_threshold": self.similarity_threshold,
                        "index_info": info
                    }

        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to get index info: {e}"
            }