"""FastAPI dependency helpers for application state."""

from __future__ import annotations

from fastapi import HTTPException, Request, status


def require_state(request: Request, name: str):
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is not initialized",
        )
    return value
