"""API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from powerguard.api.routes import devices, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(devices.router)
