"""Compatibility ASGI entrypoint.

This keeps old deployment targets (including existing Vercel/FastAPI presets)
working after the project moved the real app entrypoint to ``app/main.py``.
"""

from app.main import app

