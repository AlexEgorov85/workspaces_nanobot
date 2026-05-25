"""
Direct test of the audit analyzer with manual SQL execution.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from database import DatabaseManager


async def test_direct_sql():
    """Test direct SQL execution."""
    print("Direct SQL Test")
    print("=" * 20)
    
    try:
        # Initialize database manager
        db_manager = DatabaseManager(
            connection_string="postgresql://postgres:1@localhost:5432/postgres",
            cache_ttl=86400
        )
        
        # Simple analytics query
        sql = """
        SELECT 
            EXTRACT(YEAR FROM actual_date) as audit_year,
            EXTRACT(MONTH FROM actual_date) as audit_month,
            COUNT(*) as audit_count,
            TO_CHAR(actual_date, 'Month') as month_name
        FROM oarb.audits 
        WHERE actual_date IS NOT NULL
        GROUP BY audit_year, audit_month, TO_CHAR(actual_date, 'Month')
        ORDER BY audit_year DESC, audit_month
        """
        
        print("Executing SQL:")
        print(sql)
        print("\n" + "=" * 50)
        
        # Execute the query
        result = await db_manager.execute_query(sql)
        
        if result["status"] == "success":
            print(f"Query executed successfully!")
            print(f"Rows returned: {result['row_count']}")
            
            if result['row_count'] > 0:
                print("\nResults:")
                print("Year | Month | Month Name | Count")
                print("-" * 40)
                
                for row in result['rows']:
                    year = int(row['audit_year'])
                    month = int(row['audit_month'])
                    month_name = row['month_name'].strip()
                    count = row['audit_count']
                    
                    print(f"{year} | {month:02d}  | {month_name:10s} | {count}")
                
                # Summary by year
                print("\nSummary by year:")
                year_totals = {}
                for row in result['rows']:
                    year = int(row['audit_year'])
                    year_totals[year] = year_totals.get(year, 0) + row['audit_count']
                
                for year in sorted(year_totals.keys(), reverse=True):
                    print(f"  {year}: {year_totals[year]} total audits")
            else:
                print("No data found")
                
        else:
            print(f"Query failed: {result['error']}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Run direct test."""
    await test_direct_sql()


if __name__ == "__main__":
    asyncio.run(main())