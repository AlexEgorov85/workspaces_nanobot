"""
Test script for Audit Analyzer skill.
"""

import asyncio
from pathlib import Path
from api import AuditAnalyzerAPI


async def test_all_methods():
    """Test all three analysis methods."""
    print("Testing Audit Analyzer - All Methods")
    print("=" * 50)
    
    api = AuditAnalyzerAPI()
    
    # Test queries for each method
    test_queries = [
        {
            "query": "покажи статистику проверок по месяцам 2024 года",
            "expected_method": "predefined_script",
            "description": "Predefined script test"
        },
        {
            "query": "финансовые нарушения в образовательных учреждениях",
            "expected_method": "vector_search", 
            "description": "Vector search test"
        },
        {
            "query": "найди топ-5 объектов с максимальным количеством нарушений за последний год",
            "expected_method": "generated_sql",
            "description": "Generated SQL test"
        }
    ]
    
    for i, test_case in enumerate(test_queries, 1):
        print(f"\n{i}. {test_case['description']}")
        print(f"Query: {test_case['query']}")
        print("-" * 40)
        
        try:
            result = await api.analyze_query(test_case['query'], auto_execute=True)
            
            print(f"Status: {result['status']}")
            print(f"Method: {result.get('method', 'unknown')}")
            print(f"Expected: {test_case['expected_method']}")
            
            if result['status'] == 'executed':
                print(f"Rows returned: {result['execution']['row_count']}")
                
                # Show sample data
                if result['execution']['row_count'] > 0:
                    print("Sample data:")
                    for j, row in enumerate(result['execution']['rows'][:3]):
                        print(f"  {j+1}: {row}")
            
            elif result['status'] == 'vector_search':
                print(f"Documents found: {result['total_found']}")
                
                if result['total_found'] > 0:
                    print("Sample documents:")
                    for j, doc in enumerate(result['results'][:2]):
                        print(f"  {j+1}: {doc['content'][:80]}...")
            
            elif result['status'] == 'error':
                print(f"Error: {result['error']}")
            
            # Check if method matches expectation
            actual_method = result.get('method', 'unknown')
            if actual_method == test_case['expected_method']:
                print("✅ Method matches expectation")
            else:
                print(f"⚠️ Method mismatch. Expected: {test_case['expected_method']}, Got: {actual_method}")
        
        except Exception as e:
            print(f"❌ Test failed: {e}")


async def test_predefined_scripts():
    """Test predefined scripts in detail."""
    print("\nTesting Predefined Scripts")
    print("=" * 30)
    
    api = AuditAnalyzerAPI()
    
    # Get available scripts
    scripts_info = api.get_available_scripts()
    print(f"Available scripts: {scripts_info['total_count']}")
    
    # Test each script with appropriate query
    script_tests = [
        ("analytics_by_year_month", "покажи аналитику по годам и месяцам"),
        ("violations_by_type", "статистика нарушений по типам"),
        ("top_audited_objects", "топ 10 проверяемых объектов"),
        ("audit_effectiveness", "оценка эффективности проверок"),
        ("audit_dynamics", "динамика проверок по кварталам"),
        ("audit_types_stats", "статистика по типам проверок")
    ]
    
    for script_name, query in script_tests:
        print(f"\nTesting script: {script_name}")
        print(f"Query: {query}")
        
        try:
            result = await api.analyze_query(query, auto_execute=False)
            
            if result['status'] == 'generated' and result.get('method') == 'predefined_script':
                print(f"✅ Script matched: {result.get('script_name')}")
                print(f"Parameters: {result.get('parameters', {})}")
                print(f"SQL generated successfully")
            else:
                print(f"⚠️ Script not matched. Status: {result['status']}, Method: {result.get('method')}")
        
        except Exception as e:
            print(f"❌ Script test failed: {e}")


async def test_vector_search():
    """Test vector search functionality."""
    print("\nTesting Vector Search")
    print("=" * 25)
    
    api = AuditAnalyzerAPI()
    
    # Check if vector search is available
    vector_info = api.get_vector_index_info()
    print(f"Vector search status: {vector_info['status']}")
    
    if vector_info['status'] == 'error':
        print("Vector search not available, skipping tests")
        return
    
    # Create index if needed
    print("Creating/updating vector index...")
    index_result = await api.create_vector_index()
    print(f"Index creation: {index_result['status']}")
    
    if index_result['status'] == 'success':
        print(f"Indexed items: {index_result.get('total_indexed', 0)}")
    
    # Test searches
    search_queries = [
        "финансовые нарушения",
        "проверки университетов", 
        "кадровые вопросы",
        "налоговые проверки"
    ]
    
    for query in search_queries:
        print(f"\nSearching: {query}")
        
        try:
            result = await api.analyze_query(query)
            
            if result['status'] == 'vector_search':
                print(f"✅ Found {result['total_found']} documents")
                
                for i, doc in enumerate(result['results'][:2]):
                    print(f"  {i+1}: {doc['content'][:60]}... (score: {doc['score']:.3f})")
            
            else:
                print(f"⚠️ Vector search not used. Status: {result['status']}")
        
        except Exception as e:
            print(f"❌ Search failed: {e}")


async def test_database_connection():
    """Test database connection and schema."""
    print("\nTesting Database Connection")
    print("=" * 30)
    
    api = AuditAnalyzerAPI()
    
    try:
        # Test schema retrieval
        schema_result = await api.get_schema_info()
        
        if schema_result['status'] == 'success':
            schema = schema_result['schema']
            print(f"✅ Database connected successfully")
            print(f"Schema: {len(schema['tables'])} tables")
            
            for table_name, table_info in schema['tables'].items():
                print(f"  • {table_name}: {len(table_info['columns'])} columns")
        
        else:
            print(f"❌ Schema retrieval failed: {schema_result['error']}")
    
    except Exception as e:
        print(f"❌ Database test failed: {e}")


async def main():
    """Run all tests."""
    try:
        await test_database_connection()
        await test_predefined_scripts()
        await test_vector_search()
        await test_all_methods()
        
        print("\n" + "=" * 50)
        print("✅ All tests completed!")
        
    except Exception as e:
        print(f"❌ Test suite failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())