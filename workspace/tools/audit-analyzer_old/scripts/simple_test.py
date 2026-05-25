"""
Simple test for predefined script.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from predefined_scripts import PredefinedScripts


def test_simple_script():
    """Test simple script generation."""
    print("Simple Script Test")
    print("=" * 20)
    
    scripts = PredefinedScripts()
    
    # Get the analytics script
    script = scripts.get_script_by_name("analytics_by_year_month")
    
    if not script:
        print("Script not found!")
        return
    
    print(f"Script: {script.name}")
    print(f"Description: {script.description}")
    
    # Test with empty parameters
    params = {"year_filter": ""}
    sql = script.generate_sql(params)
    
    print(f"\nGenerated SQL:")
    print(sql)
    
    # Check if it contains any placeholders
    if "{" in sql or "}" in sql:
        print("\nSQL still contains placeholders!")
    else:
        print("\nSQL looks good!")


if __name__ == "__main__":
    test_simple_script()