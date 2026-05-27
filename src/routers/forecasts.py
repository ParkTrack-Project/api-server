from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text, func
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import ConfidenceLevel, Forecast, ParkingZone, User
from ..dependencies import require
from ..schemas.forecasts import (
    CreateForecastRequest,
    ForecastMapItem,
    ForecastPointResponse,
    ForecastSeriesPoint,
    UpdateForecastRequest,
)

router = APIRouter(prefix="/forecasts", tags=["Forecasts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _predicted_free_count(f: Forecast, db: Session) -> int:
    row = db.execute(
        text("SELECT predicted_free_count FROM forecasts WHERE forecast_id = :id"),
        {"id": f.forecast_id},
    ).one_or_none()
    return row[0] if row else (f.capacity - f.predicted_occupied)


def _confidence_level(confidence: float) -> ConfidenceLevel | None:
    if confidence >= 0.85:
        return ConfidenceLevel.high
    elif confidence >= 0.65:
        return ConfidenceLevel.medium
    elif confidence >= 0.40:
        return ConfidenceLevel.low
    else:
        return ConfidenceLevel.very_low


def _serialize(f: Forecast, db: Session) -> ForecastPointResponse:
    return ForecastPointResponse(
        forecast_id=f.forecast_id,
        zone_id=f.zone_id,
        camera_id=f.camera_id,
        partner_id=f.partner_id,
        model_type=f.model_type,
        model_version=f.model_version,
        generated_at=f.generated_at,
        predicted_for=f.predicted_for,
        capacity=f.capacity,
        predicted_occupied=f.predicted_occupied,
        predicted_free_count=_predicted_free_count(f, db),
        probability_free_space=f.probability_free_space,
        confidence=f.confidence,
        confidence_level=f.confidence_level.value if f.confidence_level else None,
        metadata=f.metadata_json,
        created_by_user_id=f.created_by_user_id,
    )


def _get_forecast_or_404(db: Session, forecast_id: int) -> Forecast:
    f = db.query(Forecast).filter(Forecast.forecast_id == forecast_id).one_or_none()
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Forecast not found"})
    return f


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
        return min_lon, min_lat, max_lon, max_lat
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "bbox must be min_lon,min_lat,max_lon,max_lat"})


# ---------------------------------------------------------------------------
# GET /forecasts
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[ForecastPointResponse]
                 | list[ForecastSeriesPoint]
                 | list[ForecastMapItem],
)
def list_forecasts(
    # 2026-05-16: открыто без авторизации (по запросу) — режим «Будущее» на карте.
    db:                Annotated[Session, Depends(get_db)],
    zone_id:           int    | None = None,
    camera_id:         int    | None = None,
    partner_id:        int    | None = None,
    model_type:        str    | None = None,
    generated_from:    datetime | None = None,
    generated_to:      datetime | None = None,
    from_:             datetime | None = Query(None, alias="from"),
    to:                datetime | None = None,
    at:                datetime | None = None,
    latest_model_only: bool          = False,
    bbox:              str    | None = None,
    view:              str           = "points",
):
    if view == "map" and at is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Parameter 'at' is required for view=map"})

    query = db.query(Forecast)

    if zone_id is not None:
        query = query.filter(Forecast.zone_id == zone_id)
    if camera_id is not None:
        query = query.filter(Forecast.camera_id == camera_id)
    if partner_id is not None:
        query = query.filter(Forecast.partner_id == partner_id)
    if model_type is not None:
        query = query.filter(Forecast.model_type == model_type)
    if generated_from:
        query = query.filter(Forecast.generated_at >= generated_from)
    if generated_to:
        query = query.filter(Forecast.generated_at <= generated_to)
    if from_:
        query = query.filter(Forecast.predicted_for >= from_)
    if to:
        query = query.filter(Forecast.predicted_for <= to)

    # Если указан at, независимо от view возвращаем по одному прогнозу на каждую зону.
    # Логика:
    # 1. выбираем predicted_for, ближайший к at;
    # 2. если для этого predicted_for есть несколько версий,
    #    берём самую позднюю по generated_at;
    # 3. forecast_id.desc() нужен как стабильный tie-breaker.
    if at is not None:
        time_distance = func.abs(
            func.extract("epoch", Forecast.predicted_for - at)
        )

        ranked_sq = (
            query.with_entities(
                Forecast.forecast_id.label("forecast_id"),
                func.row_number().over(
                    partition_by=Forecast.zone_id,
                    order_by=(
                        time_distance.asc(),
                        Forecast.generated_at.desc(),
                        Forecast.forecast_id.desc(),
                    ),
                ).label("rn"),
            )
            .subquery()
        )

        query = (
            db.query(Forecast)
            .join(ranked_sq, Forecast.forecast_id == ranked_sq.c.forecast_id)
            .filter(ranked_sq.c.rn == 1)
        )

    # Если at не указан, но requested latest_model_only,
    # оставляем старую логику: последняя генерация по каждой зоне + predicted_for.
    elif latest_model_only:
        latest_sq = (
            query.with_entities(
                Forecast.zone_id.label("zone_id"),
                Forecast.predicted_for.label("predicted_for"),
                func.max(Forecast.generated_at).label("max_gen"),
            )
            .group_by(Forecast.zone_id, Forecast.predicted_for)
            .subquery()
        )

        query = query.join(
            latest_sq,
            (Forecast.zone_id == latest_sq.c.zone_id)
            & (Forecast.predicted_for == latest_sq.c.predicted_for)
            & (Forecast.generated_at == latest_sq.c.max_gen),
        )

    # bbox имеет смысл только для карты, но теперь он не должен быть связан
    # с выбором одного прогноза по at.
    if view == "map" and bbox:
        min_lon, min_lat, max_lon, max_lat = _parse_bbox(bbox)

        zone_ids_in_bbox = []
        zones = db.query(ParkingZone).all()

        for z in zones:
            try:
                coords = z.geometry["coordinates"][0]
                z_lon = sum(c[0] for c in coords) / len(coords)
                z_lat = sum(c[1] for c in coords) / len(coords)

                if min_lon <= z_lon <= max_lon and min_lat <= z_lat <= max_lat:
                    zone_ids_in_bbox.append(z.parking_zone_id)
            except Exception:
                pass

        query = query.filter(Forecast.zone_id.in_(zone_ids_in_bbox))

    forecasts = query.order_by(Forecast.predicted_for.asc()).all()

    if view == "series":
        return [
            ForecastSeriesPoint(
                predicted_for=f.predicted_for,
                predicted_occupied=f.predicted_occupied,
                predicted_free_count=_predicted_free_count(f, db),
                capacity=f.capacity,
                probability_free_space=f.probability_free_space,
                confidence=f.confidence,
                confidence_level=f.confidence_level.value if f.confidence_level else None,
                model_type=f.model_type,
                generated_at=f.generated_at,
            )
            for f in forecasts
        ]

    if view == "map":
        result = []
        for f in forecasts:
            zone = db.query(ParkingZone).filter(
                ParkingZone.parking_zone_id == f.zone_id
            ).one_or_none()
            if zone is None:
                continue
            result.append(ForecastMapItem(
                zone_id=f.zone_id,
                camera_id=f.camera_id,
                capacity=f.capacity,
                predicted_occupied=f.predicted_occupied,
                predicted_free_count=_predicted_free_count(f, db),
                probability_free_space=f.probability_free_space,
                confidence=f.confidence,
                confidence_level=f.confidence_level.value if f.confidence_level else None,
                predicted_for=f.predicted_for,
                generated_at=f.generated_at,
                geometry=zone.geometry,
                pay=zone.pay,
                zone_type=zone.zone_type.value,
                location_type=zone.location_type.value if zone.location_type else None,
                is_accessible=zone.is_accessible,
                is_active=zone.is_active,
            ))
        return result

    # view=points (default)
    return [_serialize(f, db) for f in forecasts]


# ---------------------------------------------------------------------------
# POST /forecasts/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED)
def create_forecast(
    body:         CreateForecastRequest,
    current_user: Annotated[User, require("forecasts.write")],
    db:           Annotated[Session, Depends(get_db)],
):
    zone = db.query(ParkingZone).filter(
        ParkingZone.parking_zone_id == body.zone_id
    ).one_or_none()
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_description": "Zone not found"})

    capacity = body.capacity if body.capacity is not None else zone.capacity

    if body.predicted_occupied > capacity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Validation error: predicted_occupied must be between 0 and capacity"})

    conflict = db.query(Forecast).filter_by(
        zone_id=body.zone_id,
        generated_at=body.generated_at,
        predicted_for=body.predicted_for,
    ).one_or_none()
    if conflict:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_description": "Forecast for this zone/generated_at/predicted_for already exists"})

    f = Forecast(
        zone_id=body.zone_id,
        camera_id=zone.camera_id,
        partner_id=zone.partner_id,
        model_type=body.model_type,
        model_version=body.model_version,
        generated_at=body.generated_at,
        predicted_for=body.predicted_for,
        capacity=capacity,
        predicted_occupied=body.predicted_occupied,
        probability_free_space=body.probability_free_space,
        confidence=body.confidence,
        confidence_level=_confidence_level(body.confidence),
        metadata_json=body.metadata,
        created_by_user_id=current_user.user_id,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return {"forecast_id": f.forecast_id}


# ---------------------------------------------------------------------------
# GET /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.get("/{forecast_id}", response_model=ForecastPointResponse)
def get_forecast(
    forecast_id:  int,
    current_user: Annotated[User, require("forecasts.view")],
    db:           Annotated[Session, Depends(get_db)],
):
    return _serialize(_get_forecast_or_404(db, forecast_id), db)


# ---------------------------------------------------------------------------
# PUT /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.put("/{forecast_id}", response_model=ForecastPointResponse)
def update_forecast(
    forecast_id:  int,
    body:         UpdateForecastRequest,
    current_user: Annotated[User, require("forecasts.write")],
    db:           Annotated[Session, Depends(get_db)],
):
    f = _get_forecast_or_404(db, forecast_id)

    new_capacity = body.capacity if body.capacity is not None else f.capacity
    new_occupied = body.predicted_occupied if body.predicted_occupied is not None else f.predicted_occupied

    if new_occupied > new_capacity:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error_description": "Validation error: predicted_occupied must be between 0 and capacity"})

    if body.model_version is not None:
        f.model_version = body.model_version
    if body.generated_at is not None:
        f.generated_at = body.generated_at
    if body.predicted_for is not None:
        f.predicted_for = body.predicted_for
    if body.predicted_occupied is not None:
        f.predicted_occupied = body.predicted_occupied
    if body.capacity is not None:
        f.capacity = body.capacity
    if body.probability_free_space is not None:
        f.probability_free_space = body.probability_free_space
    if body.confidence is not None:
        f.confidence = body.confidence
        f.confidence_level = _confidence_level(body.confidence)
    if body.metadata is not None:
        f.metadata_json = body.metadata

    db.commit()
    db.refresh(f)
    return _serialize(f, db)


# ---------------------------------------------------------------------------
# DELETE /forecasts/{forecast_id}
# ---------------------------------------------------------------------------

@router.delete("/{forecast_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_forecast(
    forecast_id:  int,
    current_user: Annotated[User, require("forecasts.delete")],
    db:           Annotated[Session, Depends(get_db)],
):
    f = _get_forecast_or_404(db, forecast_id)
    db.delete(f)
    db.commit()
    return None
