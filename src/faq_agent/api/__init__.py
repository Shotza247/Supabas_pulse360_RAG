"""FastAPI entry point; supports uvicorn faq_agent.api:app."""

from faq_agent.api.routes import app, create_app

__all__ = ["app", "create_app"]
