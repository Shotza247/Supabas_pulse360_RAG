"""API entry point: python -m uvicorn main:app --host 127.0.0.1 --port 8767."""

from faq_agent.api.routes import app

__all__ = ["app"]
