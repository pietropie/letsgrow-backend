from fastapi import APIRouter

from app.api.v1 import ai, auth, iot, plants, sensors, uploads

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(plants.router, prefix="/plants", tags=["plants"])
api_router.include_router(sensors.router, prefix="/sensors", tags=["sensors"])
api_router.include_router(iot.router, prefix="/iot", tags=["iot"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
