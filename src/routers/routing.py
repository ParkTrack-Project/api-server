from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, cast

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import (
    Forecast,
    GlobalRole,
    ParkingZone,
    Route,
    RouteMode,
    RouteStatus,
    User,
)
from ..dependencies import require
from ..schemas.routing import (
    CreateRouteRequest,
    GeoPoint,
    RouteCandidate,
    RouteListResponse,
    RouteResponse,
    SearchRoutingRequest,
    SearchRoutingResponse,
    UpdateRouteRequest,
)

router = APIRouter(prefix="/routing", tags=["Routing"])

GEOAPIFY_ROUTEMATRIX_URL = "https://api.geoapify.com/v1/routematrix"
GEOAPIFY_PROVIDER_NAME = "geoapify"
GEOAPIFY_MODE = "drive"

# Чтобы случайно не отправлять в Geoapify слишком большую матрицу.
# Сначала зоны грубо сортируются по прямому расстоянию, потом лучшие идут в API.
MAX_MATRIX_TARGETS = 200

PUBLIC_ROUTING_USER_EMAIL = "public-routing@parktrack.local"


def _get_public_routing_user_id(db: Session) -> int:
    """
    /routing/new теперь публичный, но routes.user_id в БД NOT NULL.
    Поэтому сохраняем публичные маршруты на технического пользователя.
    """
    result = db.execute(
        text(
            """
            INSERT INTO users (
                full_name,
                email,
                hashed_password,
                global_role,
                is_active
            )
            VALUES (
                'Public Routing User',
                :email,
                'disabled-public-routing-user',
                CAST('user' AS global_roles),
                TRUE
            )
            ON CONFLICT (email)
            DO UPDATE SET email = EXCLUDED.email
            RETURNING user_id
            """
        ),
        {"email": PUBLIC_ROUTING_USER_EMAIL},
    )

    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Внутренние типы
# ---------------------------------------------------------------------------

class RoutingProviderError(Exception):
    pass


@dataclass(frozen=True)
class _ZoneTarget:
    zone: ParkingZone
    point: GeoPoint
    current_occupied: int
    current_free_count: int
    current_confidence: float


@dataclass(frozen=True)
class _CandidateSearchResult:
    candidates: list[RouteCandidate]
    total_candidates: int


# ---------------------------------------------------------------------------
# Общие helpers
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> str | None:
    if value is None:
        return None

    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return str(enum_value)

    return str(value)


def _serialize_route(route: Route) -> RouteResponse:
    candidate: RouteCandidate | None = None
    if route.selected_candidate:
        candidate = RouteCandidate.model_validate(route.selected_candidate)

    destination: GeoPoint | None = None
    if route.destination_latitude is not None and route.destination_longitude is not None:
        destination = GeoPoint(
            latitude=route.destination_latitude,
            longitude=route.destination_longitude,
        )

    return RouteResponse(
        route_id=route.route_id,
        user_id=route.user_id,
        mode=_enum_value(route.mode) or str(route.mode),
        provider=route.provider,
        origin=GeoPoint(
            latitude=route.origin_latitude,
            longitude=route.origin_longitude,
        ),
        destination=destination,
        selected_zone_id=route.selected_zone_id,
        selected_candidate=candidate,
        eta_seconds=route.eta_seconds,
        arrival_time=route.arrival_time,
        polyline=route.polyline,
        deeplink_url=route.deeplink_url,
        status=_enum_value(route.status) or str(route.status),
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


def _get_route_or_404(db: Session, route_id: int) -> Route:
    route = db.query(Route).filter(Route.route_id == route_id).one_or_none()

    if route is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_description": "Route not found"},
        )

    return route


def _assert_owner_or_admin(route: Route, current_user: User) -> None:
    is_admin = current_user.global_role == GlobalRole.admin

    if not is_admin and route.user_id != current_user.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "Access denied: not your route"},
        )


def _provider_unavailable(exc: RoutingProviderError) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error_description": str(exc)},
    )


def _zone_centroid(zone: ParkingZone) -> GeoPoint | None:
    geometry = zone.geometry

    if not isinstance(geometry, dict):
        return None

    try:
        coords = list(geometry["coordinates"][0])

        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]

        if not coords:
            return None

        latitude = sum(float(point[1]) for point in coords) / len(coords)
        longitude = sum(float(point[0]) for point in coords) / len(coords)

        return GeoPoint(latitude=latitude, longitude=longitude)

    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        try:
            return GeoPoint(
                latitude=float(geometry["lat"]),
                longitude=float(geometry["lon"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _haversine_meters(a: GeoPoint, b: GeoPoint) -> int:
    earth_radius_meters = 6_371_000

    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    delta_lat = math.radians(b.latitude - a.latitude)
    delta_lon = math.radians(b.longitude - a.longitude)

    h = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )

    return int(2 * earth_radius_meters * math.asin(math.sqrt(h)))


# ---------------------------------------------------------------------------
# Geoapify
# ---------------------------------------------------------------------------

def _geoapify_api_key() -> str:
    api_key = os.getenv("GEOAPIFY_API_KEY")

    if not api_key:
        raise RoutingProviderError("Geoapify API key is not configured")

    return api_key


def _geoapify_matrix(
    sources: list[GeoPoint],
    targets: list[GeoPoint],
) -> list[list[dict[str, Any]]]:
    if not sources or not targets:
        return []

    api_key = _geoapify_api_key()

    payload = {
        "mode": GEOAPIFY_MODE,
        "sources": [
            {"location": [point.longitude, point.latitude]}
            for point in sources
        ],
        "targets": [
            {"location": [point.longitude, point.latitude]}
            for point in targets
        ],
    }

    try:
        response = requests.post(
            GEOAPIFY_ROUTEMATRIX_URL,
            params={"apiKey": api_key},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RoutingProviderError("Geoapify Route Matrix API is unavailable") from exc

    if response.status_code >= 500:
        raise RoutingProviderError(
            f"Geoapify Route Matrix API is unavailable: HTTP {response.status_code}"
        )

    if response.status_code >= 400:
        raise RoutingProviderError(
            f"Geoapify Route Matrix API rejected request: "
            f"HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RoutingProviderError("Geoapify returned invalid JSON") from exc

    matrix = data.get("sources_to_targets")

    if not isinstance(matrix, list):
        raise RoutingProviderError("Geoapify response does not contain sources_to_targets")

    return matrix


def _matrix_cell(
    matrix: list[list[dict[str, Any]]],
    source_index: int,
    target_index: int,
) -> tuple[int, int] | None:
    try:
        cell = matrix[source_index][target_index]
    except (IndexError, TypeError):
        return None

    if not isinstance(cell, dict):
        return None

    distance = cell.get("distance")
    duration = cell.get("time")

    if distance is None or duration is None:
        return None

    try:
        return int(round(float(distance))), int(round(float(duration)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Кандидаты
# ---------------------------------------------------------------------------

def _query_zone_targets(
    db: Session,
    origin: GeoPoint,
    max_pay: int | None,
    min_free_count: int | None,
    min_confidence: float | None,
    include_accessible: bool | None,
    selected_zone_id: int | None,
    use_forecast: bool,
) -> list[_ZoneTarget]:
    query = db.query(ParkingZone).filter(ParkingZone.is_active.is_(True))

    if selected_zone_id is not None:
        query = query.filter(ParkingZone.parking_zone_id == selected_zone_id)

    if max_pay is not None:
        query = query.filter(ParkingZone.pay <= max_pay)

    if include_accessible is False:
        query = query.filter(
            or_(
                ParkingZone.is_accessible.is_(False),
                ParkingZone.is_accessible.is_(None),
            )
        )

    zones = query.all()

    targets: list[_ZoneTarget] = []

    for zone in zones:
        point = _zone_centroid(zone)

        if point is None:
            continue

        capacity = max(int(zone.capacity or 0), 0)
        occupied = max(int(zone.occupied or 0), 0)
        free_count = max(capacity - occupied, 0)
        confidence = float(zone.confidence or 0.0)

        # Если forecast включён, min_free_count/min_confidence лучше проверять
        # после прогноза к arrival_time. Здесь фильтруем только для current-mode.
        if not use_forecast:
            if min_free_count is not None and free_count < min_free_count:
                continue

            if min_confidence is not None and confidence < min_confidence:
                continue

        targets.append(
            _ZoneTarget(
                zone=zone,
                point=point,
                current_occupied=occupied,
                current_free_count=free_count,
                current_confidence=confidence,
            )
        )

    targets.sort(key=lambda item: _haversine_meters(origin, item.point))

    return targets[:MAX_MATRIX_TARGETS]


def _find_forecast_for_arrival(
    db: Session,
    zone_id: int,
    arrival_time: datetime,
) -> Forecast | None:
    time_distance = func.abs(
        func.extract("epoch", Forecast.predicted_for - arrival_time)
    )

    return (
        db.query(Forecast)
        .filter(Forecast.zone_id == zone_id)
        .order_by(
            time_distance.asc(),
            Forecast.generated_at.desc(),
            Forecast.forecast_id.desc(),
        )
        .first()
    )


def _score_candidate(
    pay: int,
    capacity: int,
    effective_free_count: int,
    effective_confidence: float,
    probability_free_space: float | None,
    duration_from_origin_seconds: int,
    duration_to_destination_seconds: int | None,
) -> float:
    free_ratio = effective_free_count / max(capacity, 1)
    free_score = max(0.0, min(free_ratio, 1.0))

    probability_score = (
        max(0.0, min(probability_free_space, 1.0))
        if probability_free_space is not None
        else (1.0 if effective_free_count > 0 else 0.0)
    )

    confidence_score = max(0.0, min(effective_confidence, 1.0))

    # 15 минут — условно хорошее время до парковки.
    origin_time_score = 1.0 / (1.0 + duration_from_origin_seconds / 900.0)

    if duration_to_destination_seconds is None:
        destination_time_score = 1.0
    else:
        # 5 минут — условно хорошее время от парковки до точки назначения.
        destination_time_score = 1.0 / (1.0 + duration_to_destination_seconds / 300.0)

    price_score = 1.0 / (1.0 + pay / 100.0)

    score = (
        0.30 * probability_score
        + 0.20 * free_score
        + 0.20 * origin_time_score
        + 0.15 * destination_time_score
        + 0.10 * confidence_score
        + 0.05 * price_score
    )

    return round(score, 6)


def _search_candidates(
    db: Session,
    origin: GeoPoint,
    destination: GeoPoint | None,
    mode: str,
    max_pay: int | None,
    min_free_count: int | None,
    min_confidence: float | None,
    max_distance_to_destination_meters: int | None,
    max_duration_from_origin_seconds: int | None,
    include_accessible: bool | None,
    use_forecast: bool,
    limit: int,
    selected_zone_id: int | None = None,
) -> _CandidateSearchResult:
    if mode == "route_to_destination" and destination is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "destination is required for mode=route_to_destination"},
        )

    zone_targets = _query_zone_targets(
        db=db,
        origin=origin,
        max_pay=max_pay,
        min_free_count=min_free_count,
        min_confidence=min_confidence,
        include_accessible=include_accessible,
        selected_zone_id=selected_zone_id,
        use_forecast=use_forecast,
    )

    if not zone_targets:
        return _CandidateSearchResult(candidates=[], total_candidates=0)

    origin_matrix = _geoapify_matrix(
        sources=[origin],
        targets=[item.point for item in zone_targets],
    )

    destination_matrix: list[list[dict[str, Any]]] | None = None
    if destination is not None:
        destination_matrix = _geoapify_matrix(
            sources=[item.point for item in zone_targets],
            targets=[destination],
        )

    now = datetime.now(timezone.utc)
    candidates: list[RouteCandidate] = []

    for index, item in enumerate(zone_targets):
        from_origin = _matrix_cell(origin_matrix, 0, index)

        if from_origin is None:
            continue

        distance_from_origin, duration_from_origin = from_origin

        if (
            max_duration_from_origin_seconds is not None
            and duration_from_origin > max_duration_from_origin_seconds
        ):
            continue

        distance_to_destination: int | None = None
        duration_to_destination: int | None = None

        if destination_matrix is not None:
            to_destination = _matrix_cell(destination_matrix, index, 0)

            if to_destination is None:
                continue

            distance_to_destination, duration_to_destination = to_destination

            if (
                max_distance_to_destination_meters is not None
                and distance_to_destination > max_distance_to_destination_meters
            ):
                continue

        zone = item.zone
        capacity = max(int(zone.capacity or 0), 0)
        pay = max(int(zone.pay or 0), 0)

        predicted_for_arrival = now + timedelta(seconds=duration_from_origin)

        predicted_occupied: int | None = None
        predicted_free_count: int | None = None
        probability_free_space: float | None = None
        forecast_confidence: float | None = None

        effective_free_count = item.current_free_count
        effective_confidence = item.current_confidence

        if use_forecast:
            forecast = _find_forecast_for_arrival(
                db=db,
                zone_id=int(zone.parking_zone_id),
                arrival_time=predicted_for_arrival,
            )

            if forecast is not None:
                forecast_capacity = int(forecast.capacity or capacity)
                predicted_occupied = max(int(forecast.predicted_occupied or 0), 0)
                predicted_free_count = max(forecast_capacity - predicted_occupied, 0)
                probability_free_space = (
                    float(forecast.probability_free_space)
                    if forecast.probability_free_space is not None
                    else None
                )
                forecast_confidence = (
                    float(forecast.confidence)
                    if forecast.confidence is not None
                    else None
                )

                effective_free_count = predicted_free_count
                effective_confidence = (
                    forecast_confidence
                    if forecast_confidence is not None
                    else item.current_confidence
                )

        if min_free_count is not None and effective_free_count < min_free_count:
            continue

        if min_confidence is not None and effective_confidence < min_confidence:
            continue

        score = _score_candidate(
            pay=pay,
            capacity=capacity,
            effective_free_count=effective_free_count,
            effective_confidence=effective_confidence,
            probability_free_space=probability_free_space,
            duration_from_origin_seconds=duration_from_origin,
            duration_to_destination_seconds=duration_to_destination,
        )

        candidates.append(
            RouteCandidate(
                zone_id=int(zone.parking_zone_id),
                camera_id=cast(int | None, zone.camera_id),
                geometry=zone.geometry,
                zone_type=_enum_value(zone.zone_type) or "unknown",
                location_type=_enum_value(zone.location_type),
                is_accessible=cast(bool | None, zone.is_accessible),
                pay=pay,
                capacity=capacity,
                current_occupied=item.current_occupied,
                current_free_count=item.current_free_count,
                current_confidence=item.current_confidence,
                predicted_for_arrival=predicted_for_arrival,
                predicted_occupied=predicted_occupied,
                predicted_free_count=predicted_free_count,
                probability_free_space=probability_free_space,
                forecast_confidence=forecast_confidence,
                distance_from_origin_meters=distance_from_origin,
                duration_from_origin_seconds=duration_from_origin,
                distance_to_destination_meters=distance_to_destination,
                duration_to_destination_seconds=duration_to_destination,
                score=score,
                rank=0,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.duration_from_origin_seconds,
            candidate.distance_from_origin_meters,
        )
    )

    total_candidates = len(candidates)

    ranked_candidates = [
        candidate.model_copy(update={"rank": rank})
        for rank, candidate in enumerate(candidates, start=1)
    ]

    return _CandidateSearchResult(
        candidates=ranked_candidates[:limit],
        total_candidates=total_candidates,
    )


def _selected_candidate_or_422(
    db: Session,
    origin: GeoPoint,
    destination: GeoPoint | None,
    mode: str,
    use_forecast: bool,
    selected_zone_id: int,
) -> RouteCandidate:
    result = _search_candidates(
        db=db,
        origin=origin,
        destination=destination,
        mode=mode,
        max_pay=None,
        min_free_count=None,
        min_confidence=None,
        max_distance_to_destination_meters=None,
        max_duration_from_origin_seconds=None,
        include_accessible=None,
        use_forecast=use_forecast,
        limit=1,
        selected_zone_id=selected_zone_id,
    )

    if not result.candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "Cannot build route to the selected zone"},
        )

    return result.candidates[0]


# ---------------------------------------------------------------------------
# POST /routing/search
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchRoutingResponse)
def search_routing(
    body: SearchRoutingRequest,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        result = _search_candidates(
            db=db,
            origin=body.origin,
            destination=body.destination,
            mode=body.mode,
            max_pay=body.max_pay,
            min_free_count=body.min_free_count,
            min_confidence=body.min_confidence,
            max_distance_to_destination_meters=body.max_distance_to_destination_meters,
            max_duration_from_origin_seconds=body.max_duration_from_origin_seconds,
            include_accessible=body.include_accessible,
            use_forecast=body.use_forecast,
            limit=body.limit,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    selected_zone_id = result.candidates[0].zone_id if result.candidates else None

    return SearchRoutingResponse(
        mode=body.mode,
        provider=GEOAPIFY_PROVIDER_NAME,
        generated_at=datetime.now(timezone.utc),
        selected_zone_id=selected_zone_id,
        total_candidates=result.total_candidates,
        candidates=result.candidates,
    )


# ---------------------------------------------------------------------------
# POST /routing/new
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED, response_model=RouteResponse)
def create_route(
    body: CreateRouteRequest,
    db: Annotated[Session, Depends(get_db)],
):
    if body.selected_zone_id is not None:
        zone_exists = (
            db.query(ParkingZone.parking_zone_id)
            .filter(ParkingZone.parking_zone_id == body.selected_zone_id)
            .one_or_none()
        )

        if zone_exists is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": f"Zone {body.selected_zone_id} not found"},
            )

    try:
        result = _search_candidates(
            db=db,
            origin=body.origin,
            destination=body.destination,
            mode=body.mode,
            max_pay=body.max_pay,
            min_free_count=body.min_free_count,
            min_confidence=body.min_confidence,
            max_distance_to_destination_meters=body.max_distance_to_destination_meters,
            max_duration_from_origin_seconds=body.max_duration_from_origin_seconds,
            include_accessible=body.include_accessible,
            use_forecast=body.use_forecast,
            limit=body.limit,
            selected_zone_id=body.selected_zone_id,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    if not result.candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "No suitable parking zones found"},
        )

    best = result.candidates[0]
    now = datetime.now(timezone.utc)
    public_user_id = _get_public_routing_user_id(db)

    route = Route(
        user_id=public_user_id,
        mode=RouteMode(body.mode),
        provider=GEOAPIFY_PROVIDER_NAME,
        origin_latitude=body.origin.latitude,
        origin_longitude=body.origin.longitude,
        destination_latitude=body.destination.latitude if body.destination else None,
        destination_longitude=body.destination.longitude if body.destination else None,
        selected_zone_id=best.zone_id,
        selected_candidate=best.model_dump(mode="json"),
        eta_seconds=best.duration_from_origin_seconds,
        arrival_time=best.predicted_for_arrival,
        polyline=None,
        deeplink_url=None,
        status=RouteStatus.active,
        created_at=now,
        updated_at=now,
    )

    try:
        db.add(route)
        db.commit()
        db.refresh(route)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Route could not be saved",
                "error_type": exc.__class__.__name__,
            },
        )

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# GET /routing
# ---------------------------------------------------------------------------

@router.get("", response_model=RouteListResponse)
def list_routes(
    current_user: Annotated[User, require("routing.view")],
    db: Annotated[Session, Depends(get_db)],
    route_status: Annotated[str | None, Query(alias="status")] = None,
    mode: Annotated[str | None, Query()] = None,
    top: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = db.query(Route)

    if current_user.global_role != GlobalRole.admin:
        query = query.filter(Route.user_id == current_user.user_id)

    if route_status is not None:
        try:
            query = query.filter(Route.status == RouteStatus(route_status))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_description": f"Unknown status: {route_status}"},
            )

    if mode is not None:
        try:
            query = query.filter(Route.mode == RouteMode(mode))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_description": f"Unknown mode: {mode}"},
            )

    total = query.count()

    routes = (
        query
        .order_by(Route.created_at.desc())
        .offset(offset)
        .limit(top)
        .all()
    )

    return RouteListResponse(
        items=[_serialize_route(route) for route in routes],
        total=total,
        top=top,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /routing/{route_id}
# ---------------------------------------------------------------------------

@router.get("/{route_id}", response_model=RouteResponse)
def get_route(
    route_id: int,
    current_user: Annotated[User, require("routing.view")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# PUT /routing/{route_id}
# ---------------------------------------------------------------------------

@router.put("/{route_id}", response_model=RouteResponse)
def update_route(
    route_id: int,
    body: UpdateRouteRequest,
    current_user: Annotated[User, require("routing.create")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    if body.status is not None:
        route.status = RouteStatus(body.status)

    # provider в request оставляем для совместимости, но фактически работаем через Geoapify.
    if body.provider is not None:
        route.provider = GEOAPIFY_PROVIDER_NAME

    if body.selected_zone_id is not None:
        zone = (
            db.query(ParkingZone)
            .filter(ParkingZone.parking_zone_id == body.selected_zone_id)
            .one_or_none()
        )

        if zone is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": f"Zone {body.selected_zone_id} not found"},
            )

        origin = GeoPoint(
            latitude=route.origin_latitude,
            longitude=route.origin_longitude,
        )

        destination: GeoPoint | None = None

        if route.destination_latitude is not None and route.destination_longitude is not None:
            destination = GeoPoint(
                latitude=route.destination_latitude,
                longitude=route.destination_longitude,
            )

        try:
            candidate = _selected_candidate_or_422(
                db=db,
                origin=origin,
                destination=destination,
                mode=_enum_value(route.mode) or "find_parking",
                use_forecast=True,
                selected_zone_id=body.selected_zone_id,
            )
        except RoutingProviderError as exc:
            raise _provider_unavailable(exc) from exc

        route.provider = GEOAPIFY_PROVIDER_NAME
        route.selected_zone_id = body.selected_zone_id
        route.selected_candidate = candidate.model_dump(mode="json")
        route.eta_seconds = candidate.duration_from_origin_seconds
        route.arrival_time = candidate.predicted_for_arrival
        route.polyline = None
        route.deeplink_url = None

    route.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(route)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": "Route could not be updated",
                "error_type": exc.__class__.__name__,
            },
        )

    return _serialize_route(route)


# ---------------------------------------------------------------------------
# DELETE /routing/{route_id}
# ---------------------------------------------------------------------------

@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    route_id: int,
    current_user: Annotated[User, require("routing.delete")],
    db: Annotated[Session, Depends(get_db)],
):
    route = _get_route_or_404(db, route_id)
    _assert_owner_or_admin(route, current_user)

    route.status = RouteStatus.cancelled
    route.updated_at = datetime.now(timezone.utc)

    db.commit()

    return None