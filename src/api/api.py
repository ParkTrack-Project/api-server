from fastapi import FastAPI, HTTPException, status, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from .models import CreateCamera, CreateZone

import json

class URL(BaseModel):
    port: str
    host: str

class PublicAPI:
    title = "API Server"
    version = "0.1.0"
    description="ParkTrack server API built with FastAPI"

    # Обязательно подключу когда-нибудь
    # valid_tokens = set() 

    def __init__(self, db_manager):
        self.db_manager = db_manager

        self.app = FastAPI(
            title=self.title,
            version=self.version,
            description=self.description
        )
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "https://swagger.parktrack.live",
                "https://labeler.parktrack.live",
                "https://parktrack.live"
            ],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )
        self._setup_routes()

    def run(self, listen_on: URL):
        import uvicorn
        uvicorn.run(self.app, host=listen_on.host, port=listen_on.port)

    def _setup_routes(self):

        @self.app.get("/health")
        async def get_health():
            db_ok = self.db_manager.check_connection()
            return {"status": "healthy" if db_ok else "degraded"}
        
        @self.app.get("/version")
        async def get_version():
            return {"api_version": self.version}
        
        @self.app.post("/cameras/new")
        async def create_new_camera(new_camera: CreateCamera):
            try:
                if self.db_manager.camera_title_already_exists(new_camera.title):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Camera with title '{new_camera.title}' already exists"
                    )
                
                camera_id = self.db_manager.create_camera({
                    "title": new_camera.title,
                    "latitude": new_camera.latitude,
                    "longitude": new_camera.longitude,
                    "source": new_camera.source,
                    "image_width": new_camera.image_width,
                    "image_height": new_camera.image_height,
                    "calib": new_camera.calib
                })
                
                return {
                    "status": "success",
                    "message": "Camera created successfully",
                    "camera_id": camera_id
                }
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )

        @self.app.post('/zones/new')
        async def create_new_zone(new_zone: CreateZone):
            try:
                if not self.db_manager.camera_id_exists(new_zone.camera_id):
                    raise HTTPException(
                        status_code=404,
                        detail=f"Camera with id {new_zone.camera_id} doesn't exist"
                    )
                
                zone_id = self.db_manager.create_zone({
                    "zone_type": new_zone.zone_type,
                    "parking_lots_count": new_zone.capacity,
                    "camera_id": new_zone.camera_id,
                    "pay": new_zone.pay,
                    "points": new_zone.points
                })

                return {
                    "zone_id": zone_id
                }
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
        
        @self.app.get("/zones/{zone_id}")
        async def get_zone(zone_id: int):
            try:
                zone = self.db_manager.get_zone(zone_id)

                if zone is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Zone with id {zone_id} doesn't exist"
                    )
                
                return zone

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.get("/zones")
        async def get_zones(
            camera_id: int = None, 
            min_free_count: int = None, 
            max_pay: int = None):
            try:
                zones = self.db_manager.get_all_zones(camera_id, min_free_count, max_pay)
                
                return zones

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.get("/cameras")
        async def get_cameras(
            q: str = None, 
            top_left_corner_latitude: float = None, 
            top_left_corner_longitude: float = None,
            bottom_right_corner_latitude: float = None,
            bottom_right_corner_longitude: float = None):
            try:
                zones = self.db_manager.get_all_cameras(
                    q, 
                    top_left_corner_latitude,
                    top_left_corner_longitude,
                    bottom_right_corner_latitude,
                    bottom_right_corner_longitude)
                
                return zones

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.get("/cameras/next")
        async def get_next_camera():
            try:
                camera = self.db_manager.get_most_outdated_camera()
                
                return camera

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.get("/cameras/{camera_id}")
        async def get_camera(camera_id: int):
            try:
                camera = self.db_manager.get_camera(camera_id)
                
                return camera

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.put("/cameras/{camera_id}")
        async def update_camera(camera_id: int, updated_fields: Request):
            try:
                updated_fields = await updated_fields.json()
                camera = self.db_manager.update_camera(camera_id, updated_fields)
                
                return camera

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.put("/zones/{zone_id}")
        async def update_zone(zone_id: int, updated_fields: Request):
            try:
                updated_fields = await updated_fields.json()
                zone = self.db_manager.update_zone(zone_id, updated_fields)
                
                return zone

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )