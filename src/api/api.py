from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from .models import CreateCamera

class URL(BaseModel):
    port: str
    host: str

class PublicAPI:
    title = "API Server"
    version = "0.1.0"
    description="ParkTrack server API built with FastAPI"

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
                "https://labeler.parktrack.live"
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
        def get_health():
            db_ok = self.db_manager.check_connection()
            return {"status": "healthy" if db_ok else "degraded"}
        
        @self.app.get("/version")
        def get_version():
            return {"api_version": self.version}
        
        @self.app.post("/cameras/new")
        def create_new_camera(new_camera: CreateCamera):
            try:
                if self.db_manager.camera_already_exists(new_camera.title):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Camera with title '{new_camera.title}' already exists"
                    )
                
                camera_id = self.db_manager.create_camera({
                    "title": new_camera.title,
                    "latitude": new_camera.latitude,
                    "longitude": new_camera.longitude
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
