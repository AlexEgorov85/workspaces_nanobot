"""
Examples of using Audit Analyzer skill.
"""

import asyncio
from pathlib import Path
from api import AuditAnalyzerAPI


async def example_predefined_script():
    """Example: Using predefined script with parameters."""
    print("=== Predefined Script Example ===")
    
    api = AuditAnalyzerAPI()
    
    # Query that should match predefined script
    query = "покажи аналитику проверок по годам и месяцам за 2024 год"
    
    result = await api.analyze_query(query, auto_execute=True)
    
    print(f"Query: {query}")
    print(f"Method: {result.get('method', 'unknown')}")
    print(f"Status: {result['status']}")
    
    if result['status'] == 'executed':
        print(f"Script used: {result.get('script_name', 'unknown')}")
        print(f"Parameters: {result.get('parameters', {})}")
        print(f"Rows returned: {result['execution']['row_count']}")
        
        if result['execution']['row_count'] > 0:
            print("Sample results:")
            for i, row in enumerate(result['execution']['rows'][:5]):
                print(f"  {i+1}: {row}")
    
    elif result['status'] == 'error':
        print(f"Error: {result['error']}")


async def example_vector_search():
    """Example: Using vector search."""
    print("\n=== Vector Search Example ===")
    
    api = AuditAnalyzerAPI()
    
    # Create vector index first
    print("Creating vector index...")
    index_result = await api.create_vector_index()
    print(f"Index creation: {index_result['status']}")
    
    if index_result['status'] == 'success':
        print(f"Indexed audits: {index_result.get('indexed_audits', 0)}")
        print(f"Indexed violations: {index_result.get('indexed_violations', 0)}")
    
    # Search for specific topics
    query = "финансовые нарушения в университетах"
    
    result = await api.analyze_query(query)
    
    print(f"\nQuery: {query}")
    print(f"Method: {result.get('method', 'unknown')}")
    print(f"Status: {result['status']}")
    
    if result['status'] == 'vector_search':
        print(f"Found {result['total_found']} documents")
        
        for i, doc in enumerate(result['results'][:3]):
            print(f"\nDocument {i+1} (Score: {doc['score']:.3f}):")
            print(f"  Content: {doc['content'][:100]}...")
            print(f"  Source: {doc['source']}")
    
    elif result['status'] == 'error':
        print(f"Error: {result['error']}")


async def example_generated_sql():
    """Example: Using generated SQL."""
    print("\n=== Generated SQL Example ===")
    
    api = AuditAnalyzerAPI()
    
    # Complex query that requires SQL generation
    query = "найди проверки с наибольшим количеством нарушений по типу 'финансовые' за последний год"
    
    result = await api.analyze_query(query, auto_execute=True)
    
    print(f"Query: {query}")
    print(f"Method: {result.get('method', 'unknown')}")
    print(f"Status: {result['status']}")
    
    if result['status'] == 'executed':
        print(f"Generated SQL:")
        print(result['sql'])
        print(f"\nExplanation: {result['explanation']}")
        print(f"Rows returned: {result['execution']['row_count']}")
        
        if result['execution']['row_count'] > 0:
            print("Results:")
            for i, row in enumerate(result['execution']['rows'][:5]):
                print(f"  {i+1}: {row}")
    
    elif result['status'] == 'error':
        print(f"Error: {result['error']}")


async def example_available_scripts():
    """Example: List available predefined scripts."""
    print("\n=== Available Scripts Example ===")
    
    api = AuditAnalyzerAPI()
    
    scripts_info = api.get_available_scripts()
    
    print(f"Total available scripts: {scripts_info['total_count']}")
    
    for script in scripts_info['scripts']:
        print(f"\n• {script['name']}")
        print(f"  Description: {script['description']}")
        print(f"  Keywords: {', '.join(script['keywords'])}")
        print(f"  Parameters: {', '.join(script['parameters'])}")


async def example_cache_management():
    """Example: Cache management."""
    print("\n=== Cache Management Example ===")
    
    api = AuditAnalyzerAPI()
    
    # Get cache info
    cache_info = api.get_cache_info()
    print(f"Cache entries: {cache_info['entries']}")
    print(f"Cache size: {cache_info['total_size']} bytes")
    
    # Refresh schema
    print("\nRefreshing schema...")
    refresh_result = await api.refresh_schema()
    print(f"Refresh status: {refresh_result['status']}")
    
    # Cleanup expired entries
    print("\nCleaning up expired entries...")
    removed = api.cleanup_cache()
    print(f"Removed {removed} expired entries")


async def main():
    """Run all examples."""
    try:
        await example_available_scripts()
        await example_predefined_script()
        await example_vector_search()
        await example_generated_sql()
        await example_cache_management()
        
        print("\n✅ All examples completed!")
        
    except Exception as e:
        print(f"❌ Example failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())