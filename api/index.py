"""Vercel entry point: the FastAPI app as one serverless function.

vercel.json sends every /api/* request here; the React build is served as static files.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import app  # noqa: E402, F401
