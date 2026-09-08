"""v1 路由聚合。"""
from fastapi import APIRouter

from app.api.v1 import account, admin, auth, skills, threads

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(account.router)
api_router.include_router(admin.router)
api_router.include_router(threads.router)
api_router.include_router(skills.router)
