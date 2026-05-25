from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import Camera, GlobalRole, Partner, User, DataSource
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


def _active_partner_ids(user: User) -> set[int]:
    return {
        membership.partner_id
        for membership in user.memberships
        if getattr(membership.partner, "is_active", True)
    }


def _has_global_camera_access(user: User) -> bool:
    return user.global_role == GlobalRole.admin


def _scope_camera_query(query, user: User):
    if _has_global_camera_access(user):
        return query
    partner_ids = _active_partner_ids(user)
    if not partner_ids:
        return query.filter(Camera.camera_id.in_([]))
    return query.filter(Camera.partner_id.in_(partner_ids))


def _ensure_camera_visible(camera: Camera, user: User):
    if _has_global_camera_access(user):
        return
    if camera.partner_id not in _active_partner_ids(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Camera not found"})


def _normalize_partner_id_for_camera(body: CreateCameraRequest, user: User) -> int | None:
    if _has_global_camera_access(user):
        return body.partner_id

    partner_ids = _active_partner_ids(user)
    if body.partner_id is None:
        if len(partner_ids) == 1:
            return next(iter(partner_ids))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "partner_id is required for partner camera creation"})
    if body.partner_id not in partner_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail={"error_description": "Cannot manage cameras for this partner"})
    return body.partner_id


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
    query = _scope_camera_query(db.query(Camera), current_user)

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

    partner_id = _normalize_partner_id_for_camera(body, current_user)

    if partner_id is not None:
        if not db.query(Partner).filter(Partner.partner_id == partner_id).one_or_none():
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
        partner_id=partner_id,
        created_by_user_id=current_user.user_id,
        is_active=True,
    )
    db.add(camera)
    db.flush()

    source = DataSource(
        partner_id=camera.partner_id,
        created_by_user_id=current_user.user_id,
        source_type="camera_stream",
        entity_type="camera",
        entity_id=camera.camera_id,
        title=camera.title,
        status="active",
        is_active=True,
    )
    db.add(source)
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
    camera = _get_camera_or_404(db, camera_id)
    _ensure_camera_visible(camera, current_user)
    return _serialize(camera)


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
    _ensure_camera_visible(camera, current_user)

    update_data = body.model_dump(exclude_none=True)
    if "partner_id" in update_data and not _has_global_camera_access(current_user):
        if update_data["partner_id"] not in _active_partner_ids(current_user):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                detail={"error_description": "Cannot manage cameras for this partner"})
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
    _ensure_camera_visible(camera, current_user)
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
    _ensure_camera_visible(camera, current_user)

    if annotated:
        images_directory = os.getenv("CAMERAS_IMAGES_DIRECTORY_PATH")
        if not images_directory:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error_description": "Camera images directory is not configured"},
            )

        snapshot_path = Path(images_directory) / f"{camera_id}.jpg"
        if not snapshot_path.is_file():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": "Camera snapshot not available"},
            )

        return FileResponse(snapshot_path, media_type="image/jpeg")

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
