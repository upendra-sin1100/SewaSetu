# conftest.py – repo root
# Add app/ to sys.path so `from rules import ...` resolves when pytest
# imports app.py or deemed_approval.py during tests.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))
