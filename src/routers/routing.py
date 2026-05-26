from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import GlobalRole, ParkingZone, Route, RouteMode, RouteStatus, User
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
from ..services.routing import RoutingProviderError, build_deeplink, search_candidates

router = APIRouter(prefix="/routing", tags=["Routing"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_route(r: Route) -> RouteResponse:
    candidate: RouteCandidate | None = None
    if r.selected_candidate:
        candidate = RouteCandidate.model_validate(r.selected_candidate)

    destination: GeoPoint | None = None
    if r.destination_latitude is not None and r.destination_longitude is not None:
        destination = GeoPoint(
            latitude=r.destination_latitude,
            longitude=r.destination_longitude,
        )

    return RouteResponse(
        route_id=r.route_id,
        user_id=r.user_id,
        mode=r.mode.value,
        provider=r.provider,
        origin=GeoPoint(latitude=r.origin_latitude, longitude=r.origin_longitude),
        destination=destination,
        selected_zone_id=r.selected_zone_id,
        selected_candidate=candidate,
        eta_seconds=r.eta_seconds,
        arrival_time=r.arrival_time,
        polyline=r.polyline,
        deeplink_url=r.deeplink_url,
        status=r.status.value,
        created_at=r.created_at,
        updated_at=r.updated_at,
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
    """Пользователь может видеть/менять только свои маршруты; admin — любые."""
    if current_user.global_role != GlobalRole.admin and route.user_id != current_user.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error_description": "Access denied: not your route"},
        )


def _provider_unavailable(exc: RoutingProviderError) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error_description": str(exc)},
    )


def _zone_centroid(zone: ParkingZone | None) -> tuple[float, float]:
    if zone is None:
        return 0.0, 0.0
    try:
        coords = list(zone.geometry["coordinates"][0])
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        z_lat = sum(float(c[1]) for c in coords) / len(coords)
        z_lon = sum(float(c[0]) for c in coords) / len(coords)
        return z_lat, z_lon
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return float(zone.geometry.get("lat", 0.0)), float(zone.geometry.get("lon", 0.0))


def _build_candidate_from_zone(
    zone: ParkingZone,
    origin: GeoPoint,
    destination: GeoPoint | None,
    db: Session,
    use_forecast: bool,
    provider: str,
    mode: str,
) -> RouteCandidate:
    """Строим одного кандидата для конкретной зоны (используется при PUT с новым zone_id)."""
    result = search_candidates(
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
        provider=provider,
        selected_zone_id=zone.parking_zone_id,
    )
    if not result.candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "Cannot build route to the selected zone"},
        )
    return result.candidates[0]


# ---------------------------------------------------------------------------
# POST /routing/search  — поиск без сохранения
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchRoutingResponse)
def search_routing(
    body: SearchRoutingRequest,
    current_user: Annotated[User, require("routing.create")],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        result = search_candidates(
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
            provider=body.provider,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    candidates = result.candidates
    selected_zone_id = candidates[0].zone_id if candidates else None

    return SearchRoutingResponse(
        mode=body.mode,
        provider=body.provider,
        generated_at=datetime.now(timezone.utc),
        selected_zone_id=selected_zone_id,
        total_candidates=result.total_candidates,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# POST /routing/new  — построение и сохранение маршрута
# ---------------------------------------------------------------------------

@router.post("/new", status_code=status.HTTP_201_CREATED, response_model=RouteResponse)
def create_route(
    body: CreateRouteRequest,
    current_user: Annotated[User, require("routing.create")],
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
        result = search_candidates(
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
            provider=body.provider,
            selected_zone_id=body.selected_zone_id,
        )
    except RoutingProviderError as exc:
        raise _provider_unavailable(exc) from exc

    candidates = result.candidates
    if not candidates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_description": "No suitable parking zones found"},
        )

    best = candidates[0]
    now = datetime.now(timezone.utc)
    arrival_time = best.predicted_for_arrival

    # Deeplink до выбранной зоны
    zone = db.query(ParkingZone).filter(
        ParkingZone.parking_zone_id == best.zone_id
    ).one_or_none()
    z_lat, z_lon = _zone_centroid(zone)
    deeplink = build_deeplink(body.provider, z_lat, z_lon)

    route = Route(
        user_id=current_user.user_id,
        mode=RouteMode(body.mode),
        provider=body.provider,
        origin_latitude=body.origin.latitude,
        origin_longitude=body.origin.longitude,
        destination_latitude=body.destination.latitude if body.destination else None,
        destination_longitude=body.destination.longitude if body.destination else None,
        selected_zone_id=best.zone_id,
        selected_candidate=best.model_dump(mode="json"),
        eta_seconds=best.duration_from_origin_seconds,
        arrival_time=arrival_time,
        polyline=None,
        deeplink_url=deeplink,
        status=RouteStatus.active,
        created_at=now,
        updated_at=now,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return _serialize_route(route)


# ---------------------------------------------------------------------------
# GET /routing  — маршруты текущего пользователя
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

    # Обычный пользователь видит только свои; admin — все
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
    routes = query.order_by(Route.created_at.desc()).offset(offset).limit(top).all()

    return RouteListResponse(
        items=[_serialize_route(r) for r in routes],
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

    if body.provider is not None:
        route.provider = body.provider

    # Перестроение маршрута при смене зоны
    if body.selected_zone_id is not None:
        zone = db.query(ParkingZone).filter(
            ParkingZone.parking_zone_id == body.selected_zone_id
        ).one_or_none()
        if zone is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error_description": f"Zone {body.selected_zone_id} not found"},
            )

        origin = GeoPoint(
            latitude=route.origin_latitude,
            longitude=route.origin_longitude,
        )
        destination = None
        if route.destination_latitude is not None:
            destination = GeoPoint(
                latitude=route.destination_latitude,
                longitude=route.destination_longitude,
            )

        try:
            candidate = _build_candidate_from_zone(
                zone=zone,
                origin=origin,
                destination=destination,
                db=db,
                use_forecast=True,
                provider=body.provider or route.provider,
                mode=route.mode.value,
            )
        except RoutingProviderError as exc:
            raise _provider_unavailable(exc) from exc

        z_lat, z_lon = _zone_centroid(zone)
        provider = body.provider or route.provider
        route.selected_zone_id = body.selected_zone_id
        route.selected_candidate = candidate.model_dump(mode="json")
        route.eta_seconds = candidate.duration_from_origin_seconds
        route.arrival_time = candidate.predicted_for_arrival
        route.deeplink_url = build_deeplink(provider, z_lat, z_lon)
        route.polyline = None  # пересчёт polyline — задача внешнего провайдера
    elif body.provider is not None and route.selected_zone_id is not None:
        zone = db.query(ParkingZone).filter(
            ParkingZone.parking_zone_id == route.selected_zone_id
        ).one_or_none()
        z_lat, z_lon = _zone_centroid(zone)
        route.deeplink_url = build_deeplink(route.provider, z_lat, z_lon)

    route.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(route)
    return _serialize_route(route)


# ---------------------------------------------------------------------------
# DELETE /routing/{route_id}  — мягкое удаление (статус cancelled)
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
