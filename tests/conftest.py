import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Default to fake creds so config import doesn't blow up under pytest.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("VOYAGE_API_KEY", "test-voyage-key")
