"""FastAPI web app. Server-rendered Jinja pages; one small JS file for the evidence drawer.

Run: PYTHONPATH=src uvicorn ecosystem.web:app --reload
"""
from .app import create_app

app = create_app()
