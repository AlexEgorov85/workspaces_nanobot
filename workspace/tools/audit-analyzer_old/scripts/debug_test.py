"""
Debug test for Audit Analyzer.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from predefined_scripts import PredefinedScripts


async def debug_script_matching():
    """Debug script matching logic."""
    print("Debug: Script Matching")
    print("=" * 30)
    
    scripts = PredefinedScripts()
    
    # Test query
    query = "сделай мне аналитику по годам, сколько проверок проведено в каждом месяце года"
    
    print(f"Query: {query}")
    print(f"Query lower: {query.lower()}")
    
    # Test each script
    for script in scripts.scripts:
        print(f"\nScript: {script.name}")
        print(f"Keywords: {script.keywords}")
        
        # Check keyword matches
        query_lower = query.lower()
        keyword_matches = 0
        matched_keywords = []
        
        for keyword in script.keywords:
            if keyword.lower() in query_lower:
                keyword_matches += 1
                matched_keywords.append(keyword)
        
        match_ratio = keyword_matches / len(script.keywords)
        threshold = 0.4
        
        print(f"Matched keywords: {matched_keywords}")
        print(f"Keyword matches: {keyword_matches}/{len(script.keywords)}")
        print(f"Match ratio: {match_ratio:.2f}")
        print(f"Threshold: {threshold}")
        print(f"Matches: {match_ratio >= threshold}")
        
        if match_ratio >= threshold:
            # Test parameter extraction
            params = scripts.extract_parameters_from_query(query, script)
            print(f"Extracted params: {params}")
            
            # Test SQL generation
            sql = script.generate_sql(params)
            print(f"Generated SQL:\n{sql}")


async def main():
    """Run debug tests."""
    await debug_script_matching()


if __name__ == "__main__":
    asyncio.run(main())