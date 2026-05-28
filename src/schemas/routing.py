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

# Ограничения для скорости.
# Мы НЕ отправляем в Geoapify все парковки из БД.
MAX_MATRIX_TARGETS = 80
MIN_CHEAP_CANDIDATES_FOR_COMPARE = 40

# Радиусы расширения поиска вокруг anchor-точки.
# Для find_parking anchor = origin.
# Для route_to_destination anchor = destination.
RADIUS_STEPS_METERS = [
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    25_000,
    50_000,
    100_000,
    250_000,
    500_000,
    1_000_000,
    None,  # финальный fallback: взять все зоны, если рядом вообще ничего нет
]

# Оценка пешего пути от парковки до destination.
# Geoapify Route Matrix здесь используем для поездки origin -> parking.
# А parking -> destination считаем быстро по haversine с коэффициентом.
WALKING_SPEED_METERS_PER_SECOND = 1.35
WALKING_DETOUR_FACTOR = 1.35

# Если до парковки ехать меньше этого времени, а свободных мест нет,
# она считается почти бесполезной без сильного прогноза на освобождение.
SHORT_TRIP_SECONDS = 30 * 60

# Если прогноз показывает хотя бы столько мест, занятая сейчас парковка
# может стать нормальным кандидатом.
FORECAST_OPPORTUNITY_FREE_COUNT = 2

# Сколько времени вокруг arrival_time забираем прогнозы одним батчем.
FORECAST_LOOKAROUND = timedelta(hours=2)

# Для публичного /routing/new без авторизации.
# Лучше задать в .env существующий user_id.
PUBLIC_ROUTING_USER_ID_ENV = "PUBLIC_ROUTING_USER_ID"


# ---------------------------------------------------------------------------
# Внутренние типы
# ---------------------------------------------------------------------------

class RoutingProviderError(Exception):
    pass


@dataclass(frozen=True)
class _ZoneTarget:
    zone: ParkingZone
    point: GeoPoint
    anchor_distance_meters: int
    current_occupied: int
    current_free_count: int
    current_confidence: float


@dataclass(frozen=True)
class _RoutedCandidate:
    zone_target: _ZoneTarget
    distance_from_origin_meters: int
    duration_from_origin_seconds: int
    distance_to_destination_meters: int | None
    duration_to_destination_seconds: int | None
    arrival_time: datetime


@dataclass(frozen=True)
class _ForecastView:
    predicted_occupied: int | None
    predicted_free_count: int | None
    probability_free_space: float | None
    forecast_confidence: float | None


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


def _to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _datetime_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.timestamp()


def _seconds_between(a: datetime, b: datetime) -> float:
    return abs((_to_utc_naive(a) - _to_utc_naive(b)).total_seconds())


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


def _get_public_routing_user_id(db: Session) -> int:
    """
    /routing/new у тебя отключён от авторизации, но routes.user_id обычно NOT NULL.
    Поэтому используем PUBLIC_ROUTING_USER_ID из .env, если он задан.
    Если не задан — берём первого существующего пользователя.
    """
    raw_user_id = os.getenv(PUBLIC_ROUTING_USER_ID_ENV)

    if raw_user_id:
        try:
            return int(raw_user_id)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_description": f"{PUBLIC_ROUTING_USER_ID_ENV} must be integer"
                },
            )

    row = db.query(User.user_id).order_by(User.user_id.asc()).first()

    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_description": (
                    "Cannot create public route: no users exist. "
                    f"Create a technical user or set {PUBLIC_ROUTING_USER_ID_ENV}."
                )
            },
        )

    return int(row[0])


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


def _estimated_walking_seconds(distance_meters: int) -> int:
    return int(distance_meters * WALKING_DETOUR_FACTOR / WALKING_SPEED_METERS_PER_SECOND)


def _build_map_deeplink(destination: GeoPoint) -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&destination={destination.latitude},{destination.longitude}"
    )


# ---------------------------------------------------------------------------
# Geoapify Route Matrix
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
    duration = cell.get("time", cell.get("duration"))

    if distance is None or duration is None:
        return None

    try:
        return int(round(float(distance))), int(round(float(duration)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Быстрый отбор зон до обращения в Geoapify
# ---------------------------------------------------------------------------

def _query_active_zones(
    db: Session,
    max_pay: int | None,
    include_accessible: bool | None,
    selected_zone_id: int | None,
) -> list[ParkingZone]:
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

    return query.all()


def _build_zone_targets(
    zones: list[ParkingZone],
    anchor: GeoPoint,
) -> list[_ZoneTarget]:
    targets: list[_ZoneTarget] = []

    for zone in zones:
        point = _zone_centroid(zone)

        if point is None:
            continue

        capacity = max(int(zone.capacity or 0), 0)
        occupied = max(int(zone.occupied or 0), 0)
        occupied = min(occupied, capacity)
        free_count = max(capacity - occupied, 0)
        confidence = max(0.0, min(float(zone.confidence or 0.0), 1.0))

        targets.append(
            _ZoneTarget(
                zone=zone,
                point=point,
                anchor_distance_meters=_haversine_meters(anchor, point),
                current_occupied=occupied,
                current_free_count=free_count,
                current_confidence=confidence,
            )
        )

    targets.sort(key=lambda item: item.anchor_distance_meters)

    return targets


def _choose_radius_pool(
    targets: list[_ZoneTarget],
    limit: int,
    selected_zone_id: int | None,
) -> list[_ZoneTarget]:
    if selected_zone_id is not None:
        return targets[:1]

    if not targets:
        return []

    required_for_compare = max(MIN_CHEAP_CANDIDATES_FOR_COMPARE, limit * 8)

    for radius in RADIUS_STEPS_METERS:
        if radius is None:
            pool = targets
        else:
            pool = [
                target
                for target in targets
                if target.anchor_distance_meters <= radius
            ]

        if len(pool) >= required_for_compare:
            return pool[:MAX_MATRIX_TARGETS]

    return targets[:MAX_MATRIX_TARGETS]


# ---------------------------------------------------------------------------
# Прогнозы
# ---------------------------------------------------------------------------

def _load_forecasts_for_candidates(
    db: Session,
    zone_ids: list[int],
    min_arrival: datetime,
    max_arrival: datetime,
) -> dict[int, list[Forecast]]:
    if not zone_ids:
        return {}

    from_time = _to_utc_naive(min_arrival - FORECAST_LOOKAROUND)
    to_time = _to_utc_naive(max_arrival + FORECAST_LOOKAROUND)

    rows = (
        db.query(Forecast)
        .filter(Forecast.zone_id.in_(zone_ids))
        .filter(Forecast.predicted_for >= from_time)
        .filter(Forecast.predicted_for <= to_time)
        .order_by(
            Forecast.zone_id.asc(),
            Forecast.predicted_for.asc(),
            Forecast.generated_at.desc(),
            Forecast.forecast_id.desc(),
        )
        .all()
    )

    result: dict[int, list[Forecast]] = {}

    for forecast in rows:
        result.setdefault(int(forecast.zone_id), []).append(forecast)

    return result


def _pick_forecast_for_arrival(
    forecasts: list[Forecast],
    arrival_time: datetime,
) -> Forecast | None:
    if not forecasts:
        return None

    return min(
        forecasts,
        key=lambda forecast: (
            _seconds_between(forecast.predicted_for, arrival_time),
            -_datetime_timestamp(forecast.generated_at),
            -int(forecast.forecast_id),
        ),
    )


def _forecast_view(
    zone_capacity: int,
    forecast: Forecast | None,
) -> _ForecastView:
    if forecast is None:
        return _ForecastView(
            predicted_occupied=None,
            predicted_free_count=None,
            probability_free_space=None,
            forecast_confidence=None,
        )

    forecast_capacity = max(int(forecast.capacity or zone_capacity), 0)
    predicted_occupied = max(int(forecast.predicted_occupied or 0), 0)
    predicted_occupied = min(predicted_occupied, forecast_capacity)

    predicted_free_count = max(forecast_capacity - predicted_occupied, 0)

    probability_free_space = (
        max(0.0, min(float(forecast.probability_free_space), 1.0))
        if forecast.probability_free_space is not None
        else None
    )

    forecast_confidence = (
        max(0.0, min(float(forecast.confidence), 1.0))
        if forecast.confidence is not None
        else None
    )

    return _ForecastView(
        predicted_occupied=predicted_occupied,
        predicted_free_count=predicted_free_count,
        probability_free_space=probability_free_space,
        forecast_confidence=forecast_confidence,
    )


# ---------------------------------------------------------------------------
# Умная оценка кандидата
# ---------------------------------------------------------------------------

def _effective_free_count(
    current_free_count: int,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> int:
    if use_forecast and forecast_view.predicted_free_count is not None:
        return forecast_view.predicted_free_count

    return current_free_count


def _effective_confidence(
    current_confidence: float,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> float:
    if use_forecast and forecast_view.forecast_confidence is not None:
        return forecast_view.forecast_confidence

    return current_confidence


def _availability_probability(
    effective_free_count: int,
    forecast_view: _ForecastView,
    use_forecast: bool,
) -> float:
    if use_forecast and forecast_view.probability_free_space is not None:
        return forecast_view.probability_free_space

    if effective_free_count >= 3:
        return 0.85

    if effective_free_count == 2:
        return 0.70

    if effective_free_count == 1:
        return 0.50

    return 0.05


def _candidate_tier(
    current_free_count: int,
    effective_free_count: int,
    probability_free_space: float,
    effective_confidence: float,
    duration_from_origin_seconds: int,
    requested_min_free_count: int | None,
    use_forecast: bool,
    forecast_view: _ForecastView,
) -> int:
    """
    Чем меньше tier, тем лучше.

    0 — отличный кандидат;
    1 — хороший кандидат;
    2 — сейчас занято, но прогноз к arrival_time хороший;
    3 — рискованный, но возможный;
    4 — запасной вариант;
    5 — почти бесполезный вариант.
    """
    min_required = requested_min_free_count if requested_min_free_count is not None else 1

    if effective_free_count >= max(3, min_required) and probability_free_space >= 0.65:
        return 0

    if effective_free_count >= min_required and probability_free_space >= 0.40:
        return 1

    if (
        use_forecast
        and current_free_count == 0
        and forecast_view.predicted_free_count is not None
        and forecast_view.predicted_free_count >= FORECAST_OPPORTUNITY_FREE_COUNT
        and duration_from_origin_seconds >= 10 * 60
        and (
            forecast_view.probability_free_space is None
            or forecast_view.probability_free_space >= 0.35
        )
    ):
        return 2

    if effective_free_count > 0:
        return 3

    if duration_from_origin_seconds <= SHORT_TRIP_SECONDS:
        return 5

    if effective_confidence < 0.35:
        return 5

    return 4


def _price_bucket(pay: int) -> int:
    if pay <= 0:
        return 0

    if pay <= 50:
        return 1

    if pay <= 150:
        return 2

    if pay <= 300:
        return 3

    return 4


def _duration_bucket(seconds: int) -> int:
    if seconds <= 10 * 60:
        return 0

    if seconds <= 20 * 60:
        return 1

    if seconds <= 40 * 60:
        return 2

    if seconds <= 60 * 60:
        return 3

    if seconds <= 2 * 60 * 60:
        return 4

    return 5


def _walk_bucket(seconds: int | None) -> int:
    if seconds is None:
        return 0

    if seconds <= 5 * 60:
        return 0

    if seconds <= 10 * 60:
        return 1

    if seconds <= 20 * 60:
        return 2

    if seconds <= 30 * 60:
        return 3

    return 4


def _display_score(
    tier: int,
    effective_free_count: int,
    probability_free_space: float,
    effective_confidence: float,
    duration_from_origin_seconds: int,
    duration_to_destination_seconds: int | None,
    pay: int,
) -> float:
    base_by_tier = {
        0: 0.95,
        1: 0.82,
        2: 0.72,
        3: 0.55,
        4: 0.35,
        5: 0.12,
    }

    base = base_by_tier.get(tier, 0.10)

    free_bonus = min(effective_free_count, 6) * 0.015
    probability_bonus = probability_free_space * 0.05
    confidence_bonus = effective_confidence * 0.03

    duration_penalty = min(duration_from_origin_seconds / (2 * 60 * 60), 1.0) * 0.10

    if duration_to_destination_seconds is None:
        walk_penalty = 0.0
    else:
        walk_penalty = min(duration_to_destination_seconds / (30 * 60), 1.0) * 0.08

    price_penalty = min(pay / 500.0, 1.0) * 0.04

    score = (
        base
        + free_bonus
        + probability_bonus
        + confidence_bonus
        - duration_penalty
        - walk_penalty
        - price_penalty
    )

    return round(max(0.0, min(score, 1.0)), 6)


def _ranking_key(
    candidate: RouteCandidate,
    tier: int,
    effective_free_count: int,
    probability_free_space: float,
    effective_confidence: float,
) -> tuple[Any, ...]:
    """
    Здесь важен порядок:
    1. качество доступности;
    2. грубая корзина времени;
    3. пеший путь до destination;
    4. запас свободных мест;
    5. цена;
    6. уверенность как tie-breaker.
    """
    return (
        tier,
        _duration_bucket(candidate.duration_from_origin_seconds),
        _walk_bucket(candidate.duration_to_destination_seconds),
        -effective_free_count,
        -probability_free_space,
        _price_bucket(candidate.pay),
        -effective_confidence,
        candidate.duration_from_origin_seconds,
        candidate.distance_from_origin_meters,
        candidate.distance_to_destination_meters
        if candidate.distance_to_destination_meters is not None
        else 0,
        candidate.pay,
        candidate.zone_id,
    )


def _remove_unreasonable_detours(
    candidates_with_meta: list[tuple[RouteCandidate, tuple[Any, ...], int]],
    limit: int,
    selected_zone_id: int | None,
) -> list[tuple[RouteCandidate, tuple[Any, ...], int]]:
    if selected_zone_id is not None:
        return candidates_with_meta

    if not candidates_with_meta:
        return []

    primary = [
        item
        for item in candidates_with_meta
        if item[2] <= 3
    ]

    if len(primary) < max(3, min(limit, 5)):
        return candidates_with_meta

    best_duration = min(item[0].duration_from_origin_seconds for item in primary)

    # Не показываем вариант на 5 часов, если есть сопоставимые варианты за час.
    max_reasonable_duration = max(
        best_duration + 30 * 60,
        int(best_duration * 2.5),
    )

    max_reasonable_duration = min(
        max_reasonable_duration,
        best_duration + 2 * 60 * 60,
    )

    reasonable: list[tuple[RouteCandidate, tuple[Any, ...], int]] = []
    backup: list[tuple[RouteCandidate, tuple[Any, ...], int]] = []

    for item in candidates_with_meta:
        candidate = item[0]
        tier = item[2]

        if tier <= 3 and candidate.duration_from_origin_seconds <= max_reasonable_duration:
            reasonable.append(item)
        else:
            backup.append(item)

    if len(reasonable) >= limit:
        return reasonable

    return reasonable + backup


# ---------------------------------------------------------------------------
# Основной поиск кандидатов
# ---------------------------------------------------------------------------

def _route_zone_pool(
    origin: GeoPoint,
    destination: GeoPoint | None,
    zone_targets: list[_ZoneTarget],
) -> list[_RoutedCandidate]:
    if not zone_targets:
        return []

    matrix = _geoapify_matrix(
        sources=[origin],
        targets=[item.point for item in zone_targets],
    )

    now = datetime.now(timezone.utc)
    routed: list[_RoutedCandidate] = []

    for index, item in enumerate(zone_targets):
        from_origin = _matrix_cell(matrix, 0, index)

        if from_origin is None:
            continue

        distance_from_origin, duration_from_origin = from_origin
        arrival_time = now + timedelta(seconds=duration_from_origin)

        distance_to_destination: int | None = None
        duration_to_destination: int | None = None

        if destination is not None:
            direct_distance = _haversine_meters(item.point, destination)
            distance_to_destination = int(direct_distance * WALKING_DETOUR_FACTOR)
            duration_to_destination = _estimated_walking_seconds(direct_distance)

        routed.append(
            _RoutedCandidate(
                zone_target=item,
                distance_from_origin_meters=distance_from_origin,
                duration_from_origin_seconds=duration_from_origin,
                distance_to_destination_meters=distance_to_destination,
                duration_to_destination_seconds=duration_to_destination,
                arrival_time=arrival_time,
            )
        )

    return routed


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

    anchor = destination if mode == "route_to_destination" and destination is not None else origin

    zones = _query_active_zones(
        db=db,
        max_pay=max_pay,
        include_accessible=include_accessible,
        selected_zone_id=selected_zone_id,
    )

    zone_targets = _build_zone_targets(zones, anchor)
    cheap_pool = _choose_radius_pool(
        targets=zone_targets,
        limit=limit,
        selected_zone_id=selected_zone_id,
    )

    routed_candidates = _route_zone_pool(
        origin=origin,
        destination=destination,
        zone_targets=cheap_pool,
    )

    if not routed_candidates:
        return _CandidateSearchResult(candidates=[], total_candidates=0)

    filtered_by_route: list[_RoutedCandidate] = []

    for routed in routed_candidates:
        if (
            max_duration_from_origin_seconds is not None
            and routed.duration_from_origin_seconds > max_duration_from_origin_seconds
        ):
            continue

        if (
            max_distance_to_destination_meters is not None
            and routed.distance_to_destination_meters is not None
            and routed.distance_to_destination_meters > max_distance_to_destination_meters
        ):
            continue

        filtered_by_route.append(routed)

    if not filtered_by_route:
        return _CandidateSearchResult(candidates=[], total_candidates=0)

    min_arrival = min(item.arrival_time for item in filtered_by_route)
    max_arrival = max(item.arrival_time for item in filtered_by_route)

    zone_ids = [
        int(item.zone_target.zone.parking_zone_id)
        for item in filtered_by_route
    ]

    forecasts_by_zone = (
        _load_forecasts_for_candidates(
            db=db,
            zone_ids=zone_ids,
            min_arrival=min_arrival,
            max_arrival=max_arrival,
        )
        if use_forecast
        else {}
    )

    candidates_with_meta: list[tuple[RouteCandidate, tuple[Any, ...], int]] = []

    for routed in filtered_by_route:
        target = routed.zone_target
        zone = target.zone

        capacity = max(int(zone.capacity or 0), 0)
        pay = max(int(zone.pay or 0), 0)

        forecast = None

        if use_forecast:
            forecast = _pick_forecast_for_arrival(
                forecasts_by_zone.get(int(zone.parking_zone_id), []),
                routed.arrival_time,
            )

        forecast_view = _forecast_view(
            zone_capacity=capacity,
            forecast=forecast,
        )

        effective_free_count = _effective_free_count(
            current_free_count=target.current_free_count,
            forecast_view=forecast_view,
            use_forecast=use_forecast,
        )

        effective_confidence = _effective_confidence(
            current_confidence=target.current_confidence,
            forecast_view=forecast_view,
            use_forecast=use_forecast,
        )

        probability_free_space = _availability_probability(
            effective_free_count=effective_free_count,
            forecast_view=forecast_view,
            use_forecast=use_forecast,
        )

        # Явные пользовательские ограничения — жёсткие.
        if min_free_count is not None and effective_free_count < min_free_count:
            continue

        if min_confidence is not None and effective_confidence < min_confidence:
            continue

        tier = _candidate_tier(
            current_free_count=target.current_free_count,
            effective_free_count=effective_free_count,
            probability_free_space=probability_free_space,
            effective_confidence=effective_confidence,
            duration_from_origin_seconds=routed.duration_from_origin_seconds,
            requested_min_free_count=min_free_count,
            use_forecast=use_forecast,
            forecast_view=forecast_view,
        )

        score = _display_score(
            tier=tier,
            effective_free_count=effective_free_count,
            probability_free_space=probability_free_space,
            effective_confidence=effective_confidence,
            duration_from_origin_seconds=routed.duration_from_origin_seconds,
            duration_to_destination_seconds=routed.duration_to_destination_seconds,
            pay=pay,
        )

        candidate = RouteCandidate(
            zone_id=int(zone.parking_zone_id),
            camera_id=cast(int | None, zone.camera_id),
            geometry=zone.geometry,
            zone_type=_enum_value(zone.zone_type) or "unknown",
            location_type=_enum_value(zone.location_type),
            is_accessible=cast(bool | None, zone.is_accessible),
            pay=pay,
            capacity=capacity,
            current_occupied=target.current_occupied,
            current_free_count=target.current_free_count,
            current_confidence=target.current_confidence,
            predicted_for_arrival=routed.arrival_time,
            predicted_occupied=forecast_view.predicted_occupied,
            predicted_free_count=forecast_view.predicted_free_count,
            probability_free_space=forecast_view.probability_free_space,
            forecast_confidence=forecast_view.forecast_confidence,
            distance_from_origin_meters=routed.distance_from_origin_meters,
            duration_from_origin_seconds=routed.duration_from_origin_seconds,
            distance_to_destination_meters=routed.distance_to_destination_meters,
            duration_to_destination_seconds=routed.duration_to_destination_seconds,
            score=score,
            rank=0,
        )

        ranking_key = _ranking_key(
            candidate=candidate,
            tier=tier,
            effective_free_count=effective_free_count,
            probability_free_space=probability_free_space,
            effective_confidence=effective_confidence,
        )

        candidates_with_meta.append((candidate, ranking_key, tier))

    candidates_with_meta.sort(key=lambda item: item[1])

    candidates_with_meta = _remove_unreasonable_detours(
        candidates_with_meta=candidates_with_meta,
        limit=limit,
        selected_zone_id=selected_zone_id,
    )

    total_candidates = len(candidates_with_meta)

    ranked = [
        item[0].model_copy(update={"rank": rank})
        for rank, item in enumerate(candidates_with_meta, start=1)
    ]

    return _CandidateSearchResult(
        candidates=ranked[:limit],
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
# POST /routing/search — публичный поиск без авторизации
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
# POST /routing/new — публичное построение и сохранение маршрута
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

    zone_point = GeoPoint(
        latitude=body.origin.latitude,
        longitude=body.origin.longitude,
    )

    selected_zone = (
        db.query(ParkingZone)
        .filter(ParkingZone.parking_zone_id == best.zone_id)
        .one_or_none()
    )

    if selected_zone is not None:
        centroid = _zone_centroid(selected_zone)

        if centroid is not None:
            zone_point = centroid

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
        deeplink_url=_build_map_deeplink(zone_point),
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
# GET /routing — маршруты текущего пользователя
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

        centroid = _zone_centroid(zone)

        route.provider = GEOAPIFY_PROVIDER_NAME
        route.selected_zone_id = body.selected_zone_id
        route.selected_candidate = candidate.model_dump(mode="json")
        route.eta_seconds = candidate.duration_from_origin_seconds
        route.arrival_time = candidate.predicted_for_arrival
        route.polyline = None

        if centroid is not None:
            route.deeplink_url = _build_map_deeplink(centroid)

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