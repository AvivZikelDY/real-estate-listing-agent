"""Root entry point.

Exposes the FastAPI application instance so it can be run as:
    uvicorn app:app --reload
"""

from src.listing_agent.main import app

__all__ = ["app"]
