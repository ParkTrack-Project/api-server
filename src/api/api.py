from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware


class URL(BaseModel):
    port: int
    host: str

class PublicAPI:
    title = "API Server"
    version = "0.0.1"
    description="ParkTrack server API built with FastAPI"

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.db_manager.connect()

        self.app = FastAPI(
            title=self.title,
            version=self.version,
            description=self.description
        )
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "https://parktrack-swagger.nawinds.dev",
                "https://parktrack-labeler.nawinds.dev"
            ],
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
