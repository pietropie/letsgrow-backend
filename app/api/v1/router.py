from fastapi import APIRouter

from app.api.v1 import ai, auth, events, grows, iot, plants, pots, sensors

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(grows.router, prefix="/grows", tags=["grows"])
api_router.include_router(pots.router, prefix="/grows", tags=["pots"])
api_router.include_router(plants.router, prefix="/grows", tags=["plants"])
api_router.include_router(events.router, prefix="/plants", tags=["events"])
api_router.include_router(sensors.router, prefix="/sensors", tags=["sensors"])
api_router.include_router(iot.router, prefix="/iot", tags=["iot"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
