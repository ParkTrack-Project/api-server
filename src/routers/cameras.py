from __future__ import annotations

from io import BytesIO
from typing import Annotated

import cv2
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from PIL import Image
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Camera, Partner, User
from ..dependencies import CurrentUser, require
from ..schemas.cameras import (
    CameraMapItemResponse,
    CameraNextResponse,
    CameraResponse,
    CreateCameraRequest,
    UpdateCameraRequest,
)

router = APIRouter(prefix="/cameras", tags=["Cameras"])


def _serialize(c: Camera) -> CameraResponse:
    return CameraResponse(
        camera_id=c.camera_id,
        title=c.title,
        source=c.source,
        image_width=c.image_width,
        image_height=c.image_height,
        calib=c.calib,
        latitude=c.latitude,
        longitude=c.longitude,
        partner_id=c.partner_id,
        created_by_user_id=c.created_by_user_id,
        is_active=c.is_active,
        last_snapshot_at=c.last_snapshot_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _serialize_map(c: Camera) -> CameraMapItemResponse:
    return CameraMapItemResponse(
        camera_id=c.camera_id,
        title=c.title,
        latitude=c.latitude,
        longitude=c.longitude,
        partner_id=c.partner_id,
        is_active=c.is_active,
    )


def _serialize_next(c: Camera) -> CameraNextResponse:
    return CameraNextResponse(
        camera_id=c.camera_id,
        source=c.source,
        image_width=c.image_width,
        image_height=c.image_height,
        calib=c.calib,
        partner_id=c.partner_id,
        is_active=c.is_active,
    )


def _get_camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.query(Camera).filter(Camera.camera_id == camera_id).one_or_none()
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})
    return camera


# ---------------------------------------------------------------------------
# GET /cameras
# ---------------------------------------------------------------------------

@router.get("", response_model=list[CameraResponse] | list[CameraMapItemResponse])
def list_cameras(
    current_user: Annotated[User, require("cameras.view")],
    db: Annotated[Session, Depends(get_db)],
    q:          str   | None = None,
    partner_id: int   | None = None,
    is_active:  bool  | None = None,
    bbox:       str   | None = None,   # "min_lon,min_lat,max_lon,max_lat"
    view:       str          = "full",
):
    query = db.query(Camera)

    if q:
        query = query.filter(Camera.title.icontains(q))
    if partner_id is not None:
        query = query.filter(Camera.partner_id == partner_id)
    if is_active is not None:
        query = query.filter(Camera.is_active == is_active)
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail={"error_description": "bbox must be min_lon,min_lat,max_lon,max_lat"})
        query = (
            query
            .filter(Camera.longitude >= min_lon)
            .filter(Camera.latitude  >= min_lat)
            .filter(Camera.longitude <= max_lon)
            .filter(Camera.latitude  <= max_lat)
        )

    cameras = query.order_by(Camera.camera_id).all()

    if view == "map":
        return [_serialize_map(c) for c in cameras]
    return [_serialize(c) for c in cameras]


# ---------------------------------------------------------------------------
# GET /cameras/next  — должен быть ДО /{camera_id}, иначе FastAPI съедает маршрут
# ---------------------------------------------------------------------------

# Простой round-robin через in-memory счётчик (как в оригинале)
_camera_cursor: dict[str, int] = {"index": 0}


@router.get("/next", response_model=CameraNextResponse)
def get_next_camera(
    current_user: Annotated[User, require("cameras.view")],
    db: Annotated[Session, Depends(get_db)],
):
    cameras = (
        db.query(Camera)
        .filter(Camera.is_active.is_(True))
        .order_by(Camera.camera_id)
        .all()
    )
    if not cameras:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "No cameras added"})

    idx = _camera_cursor["index"] % len(cameras)
    camera = cameras[idx]
    _camera_cursor["index"] = idx + 1
    return _serialize_next(camera)


# ---------------------------------------------------------------------------
# POST /cameras/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
def create_camera(
    body: CreateCameraRequest,
    current_user: Annotated[User, require("cameras.create")],
    db: Annotated[Session, Depends(get_db)],
):
    if db.query(Camera).filter(Camera.title == body.title).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_description": "Camera with this title already exists"})

    if body.partner_id is not None:
        if not db.query(Partner).filter(Partner.partner_id == body.partner_id).one_or_none():
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail={"error_description": "Partner not found"})

    camera = Camera(
        title=body.title,
        source=body.source,
        image_width=body.image_width,
        image_height=body.image_height,
        calib=body.calib,
        latitude=body.latitude,
        longitude=body.longitude,
        partner_id=body.partner_id,
        created_by_user_id=current_user.user_id,
        is_active=True,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return {"camera_id": camera.camera_id}


# ---------------------------------------------------------------------------
# GET /cameras/{camera_id}
# ---------------------------------------------------------------------------

@router.get("/{camera_id}", response_model=CameraResponse)
def get_camera(
    camera_id: int,
    current_user: Annotated[User, require("cameras.view")],
    db: Annotated[Session, Depends(get_db)],
):
    return _serialize(_get_camera_or_404(db, camera_id))


# ---------------------------------------------------------------------------
# PUT /cameras/{camera_id}
# ---------------------------------------------------------------------------

@router.put("/{camera_id}", response_model=CameraResponse)
def update_camera(
    camera_id: int,
    body: UpdateCameraRequest,
    current_user: Annotated[User, require("cameras.update")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = _get_camera_or_404(db, camera_id)

    update_data = body.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(camera, field, value)

    db.commit()
    db.refresh(camera)
    return _serialize(camera)


# ---------------------------------------------------------------------------
# DELETE /cameras/{camera_id}
# ---------------------------------------------------------------------------

@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera(
    camera_id: int,
    current_user: Annotated[User, require("cameras.delete")],
    db: Annotated[Session, Depends(get_db)],
):
    camera = _get_camera_or_404(db, camera_id)
    db.delete(camera)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# GET /cameras/{camera_id}/snapshot
# ---------------------------------------------------------------------------

@router.get("/{camera_id}/snapshot")
def get_snapshot(
    camera_id: int,
    current_user: Annotated[User, require("cameras.view")],
    db: Annotated[Session, Depends(get_db)],
    annotated:       bool = False,
    fallback_to_raw: bool = False,
):
    camera = _get_camera_or_404(db, camera_id)

    # annotated-снапшот здесь не реализован (нет CV pipeline) — fallback к raw
    cap = cv2.VideoCapture(camera.source)
    try:
        if not cap.isOpened():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail={"error_description": "Failed to open video stream"})
        ret, frame = cap.read()
        if not ret:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail={"error_description": "Camera snapshot not available"})

        _, buffer = cv2.imencode(".jpg", frame)
        img = Image.open(BytesIO(buffer.tobytes()))
        out = BytesIO()
        img.save(out, format="JPEG")
        out.seek(0)
        return StreamingResponse(out, media_type="image/jpeg")
    finally:
        cap.release()
