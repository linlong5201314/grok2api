"""Vercel Serverless entrypoint.

Vercel only treats files under ``api/`` as Serverless Functions.
"""

from app.main import app

