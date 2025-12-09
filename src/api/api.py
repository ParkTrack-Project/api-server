from fastapi import FastAPI, HTTPException, status, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from .models import *

import cv2
from fastapi.responses import StreamingResponse
from fastapi import HTTPException, status
from io import BytesIO
from PIL import Image


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
                "https://parktrack.live",
                "http://localhost:5173"
            ],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )
        self._setup_routes()

    def run(self, listen_on: URL):
        import uvicorn
        uvicorn.run(self.app, host=listen_on.host, port=int(listen_on.port))

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
                camera = self.db_manager.get_next_camera()

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
                
                if camera is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Camera with id {camera_id} doesn't exist"
                    )

                return camera

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.put("/cameras/{camera_id}")
        async def update_camera(camera_id: int, updated_fields: CameraBase):
            try:
                camera = self.db_manager.update_camera(camera_id, updated_fields)
                
                if camera is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Camera with id {camera_id} doesn't exist"
                    )

                return camera

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )
            
        @self.app.put("/zones/{zone_id}")
        async def update_zone(zone_id: int, update: UpdateZone):
            try:
                zone = self.db_manager.update_zone(zone_id, update)

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
            
        @self.app.delete("/cameras/{camera_id}")
        async def delete_camera(camera_id: int):
            try:
                camera = self.db_manager.get_camera(camera_id)

                if camera is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Camera with id {camera_id} doesn't exist"
                    )
                
                self.db_manager.delete_camera(camera)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )

        @self.app.get("/cameras/{camera_id}/snapshot")
        async def get_camera_snapshot(camera_id: int):
            try:
                camera = self.db_manager.get_camera(camera_id)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Internal server error: {str(e)}"
                )

            print(camera)
            source_url = camera["source"]

            try:
                cap = cv2.VideoCapture(source_url)

                if not cap.isOpened():
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to open video stream"
                    )

                ret, frame = cap.read()

                cap.release()

                if not ret:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to capture a frame from the video stream"
                    )

                _, buffer = cv2.imencode('.jpg', frame)
                img = Image.open(BytesIO(buffer))

                img_byte_arr = BytesIO()
                img.save(img_byte_arr, format='JPEG')
                img_byte_arr.seek(0)

                return StreamingResponse(img_byte_arr, media_type="image/jpeg")

            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Error capturing frame: {str(e)}"
                )
