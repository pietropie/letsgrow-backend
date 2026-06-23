from fastapi import APIRouter

from app.api.v1 import (
    admin,
    admin_ai,
    admin_dev_mode,
    admin_iot,
    admin_plans,
    admin_stats,
    admin_strains,
    admin_users,
    ai,
    auth,
    feature_flags,
    grows,
    iot,
    plans,
    plants,
    sensors,
    strains,
    uploads,
    users,
)

api_router = APIRouter()

api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_ai.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_users.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_plans.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_strains.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_stats.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_iot.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_dev_mode.router, prefix="/admin", tags=["admin"])
api_router.include_router(feature_flags.admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(grows.router, prefix="/grows", tags=["grows"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(plants.router, prefix="/plants", tags=["plants"])
api_router.include_router(plans.router, prefix="/plans", tags=["plans"])
api_router.include_router(strains.router, prefix="/strains", tags=["strains"])
api_router.include_router(sensors.router, prefix="/sensors", tags=["sensors"])
api_router.include_router(iot.router, prefix="/iot", tags=["iot"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
api_router.include_router(feature_flags.public_router, tags=["feature-flags"])
