from fastapi import APIRouter

from app.api.v1.endpoints import accounts, auth, dashboard, drafts, emails, health, notifications, tasks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(emails.router)
api_router.include_router(drafts.router)
api_router.include_router(notifications.router)
api_router.include_router(tasks.router)
api_router.include_router(dashboard.router)
