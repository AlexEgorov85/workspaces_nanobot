# Scripts Directory

This directory contains utility scripts and examples for the Audit Analyzer skill.

## Files

- `api.py` - Main API class for audit analysis
- `database.py` - Database connection and schema management
- `cache.py` - Schema caching with configurable TTL
- `predefined_scripts.py` - Predefined audit analysis scripts with parameters
- `vector_integration.py` - Vector search integration
- `examples.py` - Usage examples and demonstrations
- `test_analyzer.py` - Comprehensive test suite
- `README.md` - This file

## Running Examples

```bash
cd scripts
python examples.py
```

## Running Tests

```bash
cd scripts
python test_analyzer.py
```

## Key Components

### Predefined Scripts
The skill includes 6 predefined audit analysis scripts:
- Analytics by year/month
- Violations by type
- Top audited objects
- Audit effectiveness
- Audit dynamics
- Audit types statistics

Each script supports parameter extraction from natural language queries.

### Vector Search Integration
- Automatic indexing of audit data
- Semantic search capabilities
- Integration with vector-search skill

### Three-Mode Analysis
1. **Predefined scripts** - Fast, parameterized queries
2. **Vector search** - Document-based semantic search
3. **Generated SQL** - LLM-powered query generation

## Cache Management

The skill includes intelligent schema caching:

- **TTL Configuration**: Set `schema_cache_ttl` in config.json
- **Automatic Caching**: Schemas are cached after first retrieval
- **Cache Keys**: Different schemas and table combinations have separate cache entries
- **Cleanup**: Automatic cleanup of expired entries

### Cache Methods

```python
api = AuditAnalyzerAPI()

# Get cache information
info = api.get_cache_info()

# Clear all cache
api.clear_cache()

# Remove expired entries only
removed = api.cleanup_cache()

# Force refresh schema
await api.refresh_schema()
```

## Vector Search

```python
# Create vector index from database
await api.create_vector_index()

# Get vector index info
info = api.get_vector_index_info()

# Vector search is automatic when appropriate
result = await api.analyze_query("финансовые нарушения")
```

## Testing

The test suite covers:
- Database connectivity
- Predefined script matching
- Vector search functionality
- SQL generation
- Cache management
- End-to-end analysis workflow