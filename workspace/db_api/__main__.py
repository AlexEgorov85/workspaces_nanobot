import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # workspace/db_api/
sys.path.insert(0, str(HERE.parent))    # workspace/ → for utils.db

from db_api.client import main

sys.exit(main())
