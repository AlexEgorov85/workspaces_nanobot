"""
Test the new Audit Analyzer skill with real audit query.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from api import AuditAnalyzerAPI


async def test_audit_analytics():
    """Test audit analytics query."""
    print("Testing Audit Analyzer - Real Query")
    print("=" * 40)
    
    try:
        # Initialize API
        config_path = Path(__file__).parent.parent / "config.json"
        api = AuditAnalyzerAPI(config_path)
        
        # Test the original query
        query = "сделай мне аналитику по годам, сколько проверок проведено в каждом месяце года"
        
        print(f"Query: {query}")
        print("\nAnalyzing...")
        
        result = await api.analyze_query(query, auto_execute=True)
        
        print(f"\nStatus: {result['status']}")
        print(f"Method: {result.get('method', 'unknown')}")
        
        if result['status'] == 'executed':
            print(f"\nMethod used: {result['method']}")
            
            if result['method'] == 'predefined_script':
                print(f"Script: {result.get('script_name', 'unknown')}")
                print(f"Parameters: {result.get('parameters', {})}")
            
            print(f"\nSQL:")
            print(result['sql'])
            
            print(f"\nExplanation:")
            print(result.get('explanation', 'No explanation'))
            
            print(f"\nExecution Results:")
            exec_result = result['execution']
            print(f"   Rows returned: {exec_result['row_count']}")
            
            if exec_result['row_count'] > 0:
                print(f"\nData:")
                for i, row in enumerate(exec_result['rows'][:10]):
                    print(f"   {i+1}: {row}")
                
                if exec_result['row_count'] > 10:
                    print(f"   ... and {exec_result['row_count'] - 10} more rows")
            else:
                print("   No data returned")
                
        elif result['status'] == 'vector_search':
            print(f"\nVector Search Results:")
            print(f"Documents found: {result['total_found']}")
            
            for i, doc in enumerate(result['results'][:5]):
                print(f"   {i+1}: {doc['content'][:80]}... (score: {doc['score']:.3f})")
                
        elif result['status'] == 'error':
            print(f"\nError: {result['error']}")
        
        # Show available scripts
        print(f"\n" + "=" * 40)
        print("Available Predefined Scripts:")
        scripts_info = api.get_available_scripts()
        
        for script in scripts_info['scripts']:
            print(f"  • {script['name']}: {script['description']}")
        
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()


async def test_different_queries():
    """Test different types of queries."""
    print("\n" + "=" * 40)
    print("Testing Different Query Types")
    print("=" * 40)
    
    try:
        config_path = Path(__file__).parent.parent / "config.json"
        api = AuditAnalyzerAPI(config_path)
        
        test_queries = [
            "статистика нарушений по типам",
            "топ 5 проверяемых объектов",
            "эффективность проверок за 2024 год",
            "финансовые нарушения в образовательных учреждениях"
        ]
        
        for i, query in enumerate(test_queries, 1):
            print(f"\n{i}. Query: {query}")
            print("-" * 30)
            
            try:
                result = await api.analyze_query(query, auto_execute=False)
                
                print(f"Status: {result['status']}")
                print(f"Method: {result.get('method', 'unknown')}")
                
                if result['status'] == 'generated':
                    print(f"Script: {result.get('script_name', 'N/A')}")
                    print(f"SQL preview: {result['sql'][:100]}...")
                    
                elif result['status'] == 'vector_search':
                    print(f"Documents found: {result['total_found']}")
                    
                elif result['status'] == 'error':
                    print(f"Error: {result['error']}")
                    
            except Exception as e:
                print(f"Query failed: {e}")
    
    except Exception as e:
        print(f"Test suite failed: {e}")


async def main():
    """Run audit analyzer tests."""
    await test_audit_analytics()
    await test_different_queries()


if __name__ == "__main__":
    asyncio.run(main())