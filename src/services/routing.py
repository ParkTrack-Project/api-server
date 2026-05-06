"""
Сервис поиска кандидатов парковки.

Логика:
1. Берём все активные зоны из БД, опционально фильтруем.
2. Считаем расстояние/время от origin до зоны через Haversine (MVP).
3. Если use_forecast=True — подтягиваем ближайший прогноз к arrival_time.
4. Рассчитываем score и ранжируем кандидатов.
5. Возвращаем список RouteCandidate.

Deeplink для Yandex Navigator:
  yandexnavi://build_route_on_map?lat_to={lat}&lon_to={lon}
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from ..db_models import Forecast, ParkingZone
from ..schemas.routing import GeoPoint, RouteCandidate

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Haversine
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0
_WALKING_SPEED_MPS = 1.4        # м/с (~5 км/ч)
_DRIVING_SPEED_MPS = 8.33       # м/с (~30 км/ч в городе)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Возвращает расстояние в метрах между двумя точками."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _zone_centroid(zone: ParkingZone) -> tuple[float, float]:
    """Возвращает (lat, lon) центра зоны из GeoJSON Polygon."""
    try:
        coords = zone.geometry["coordinates"][0]   # внешнее кольцо
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    except (KeyError, TypeError, IndexError, ZeroDivisionError):
        return zone.geometry.get("lat", 0.0), zone.geometry.get("lon", 0.0)


def _duration_seconds(distance_m: float) -> int:
    """Грубая оценка времени поездки на автомобиле."""
    return max(30, int(distance_m / _DRIVING_SPEED_MPS))


def _duration_on_foot(distance_m: float) -> int:
    """Грубая оценка времени пешей прогулки."""
    return max(10, int(distance_m / _WALKING_SPEED_MPS))


# ---------------------------------------------------------------------------
# Deeplink
# ---------------------------------------------------------------------------

def build_deeplink(provider: str, lat: float, lon: float) -> str | None:
    if provider == "yandex":
        return f"yandexnavi://build_route_on_map?lat_to={lat}&lon_to={lon}"
    return None


# ---------------------------------------------------------------------------
# Прогноз к моменту прибытия
# ---------------------------------------------------------------------------

def _get_forecast_for_arrival(
    db: Session,
    zone_id: int,
    arrival_time: datetime,
) -> Forecast | None:
    """
    Выбираем прогноз с predicted_for ближайшим к arrival_time (не позже чем +30 мин),
    из самой последней генерации.
    """
    window_start = arrival_time - timedelta(minutes=30)
    window_end   = arrival_time + timedelta(minutes=30)

    return (
        db.query(Forecast)
        .filter(
            Forecast.zone_id == zone_id,
            Forecast.predicted_for >= window_start,
            Forecast.predicted_for <= window_end,
        )
        .order_by(Forecast.generated_at.desc(), Forecast.predicted_for.asc())
        .first()
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(
    free_count: int,
    capacity: int,
    confidence: float,
    distance_m: float,
    pay: int,
    max_distance: float,
    probability_free_space: float | None,
) -> float:
    """
    Итоговый score в диапазоне 0..1.
    Учитывает: доступность мест, уверенность, расстояние, стоимость.
    """
    occupancy_score  = (free_count / max(capacity, 1)) * 0.35
    confidence_score = confidence * 0.20
    distance_score   = max(0.0, 1.0 - distance_m / max(max_distance, 1)) * 0.30
    pay_score        = (1.0 / (1.0 + pay / 100)) * 0.10
    forecast_score   = (probability_free_space or 0.0) * 0.05

    return round(
        occupancy_score + confidence_score + distance_score + pay_score + forecast_score,
        4,
    )


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def find_candidates(
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
) -> list[RouteCandidate]:
    """
    Возвращает отсортированный список кандидатов.
    Если selected_zone_id указан — возвращает ровно этого кандидата первым
    (если проходит фильтры).
    """
    now = datetime.now(timezone.utc)

    query = db.query(ParkingZone).filter(ParkingZone.is_active.is_(True))

    if max_pay is not None:
        query = query.filter(ParkingZone.pay <= max_pay)
    if min_free_count is not None:
        query = query.filter((ParkingZone.capacity - ParkingZone.occupied) >= min_free_count)
    if min_confidence is not None:
        query = query.filter(ParkingZone.confidence >= min_confidence)
    if include_accessible is True:
        query = query.filter(ParkingZone.is_accessible.is_(True))
    if selected_zone_id is not None:
        query = query.filter(ParkingZone.parking_zone_id == selected_zone_id)

    zones: list[ParkingZone] = query.all()

    if not zones:
        return []

    # Максимальное расстояние от origin для нормировки score
    max_dist_for_score = 5000.0

    raw: list[tuple[float, RouteCandidate]] = []

    for zone in zones:
        z_lat, z_lon = _zone_centroid(zone)

        # --- расстояние от origin до зоны ---
        dist_origin = _haversine(origin.latitude, origin.longitude, z_lat, z_lon)
        dur_origin  = _duration_seconds(dist_origin)

        if max_duration_from_origin_seconds and dur_origin > max_duration_from_origin_seconds:
            continue

        # --- расстояние от зоны до destination ---
        dist_dest = None
        dur_dest  = None
        if destination:
            dist_dest = _haversine(z_lat, z_lon, destination.latitude, destination.longitude)
            dur_dest  = _duration_on_foot(dist_dest)
            if max_distance_to_destination_meters and dist_dest > max_distance_to_destination_meters:
                continue

        # --- прогноз к моменту прибытия ---
        arrival_time     = now + timedelta(seconds=dur_origin)
        forecast         = _get_forecast_for_arrival(db, zone.parking_zone_id, arrival_time) if use_forecast else None
        pred_occupied    = forecast.predicted_occupied    if forecast else None
        pred_free        = (zone.capacity - forecast.predicted_occupied) if forecast else None
        prob_free        = forecast.probability_free_space if forecast else None
        forecast_conf    = forecast.confidence             if forecast else None
        predicted_for_at = forecast.predicted_for         if forecast else arrival_time

        # --- score ---
        effective_free  = pred_free if pred_free is not None else (zone.capacity - zone.occupied)
        effective_conf  = forecast_conf if forecast_conf is not None else (zone.confidence or 0.0)
        score = _score(
            free_count=effective_free,
            capacity=zone.capacity,
            confidence=effective_conf,
            distance_m=dist_origin,
            pay=zone.pay,
            max_distance=max_dist_for_score,
            probability_free_space=prob_free,
        )

        candidate = RouteCandidate(
            zone_id=zone.parking_zone_id,
            camera_id=zone.camera_id,
            geometry=zone.geometry,
            zone_type=zone.zone_type.value,
            location_type=zone.location_type.value if zone.location_type else None,
            is_accessible=zone.is_accessible,
            pay=zone.pay,
            capacity=zone.capacity,
            current_occupied=zone.occupied,
            current_free_count=zone.capacity - zone.occupied,
            current_confidence=zone.confidence or 0.0,
            predicted_for_arrival=predicted_for_at,
            predicted_occupied=pred_occupied,
            predicted_free_count=pred_free,
            probability_free_space=prob_free,
            forecast_confidence=forecast_conf,
            distance_from_origin_meters=int(dist_origin),
            duration_from_origin_seconds=dur_origin,
            distance_to_destination_meters=int(dist_dest) if dist_dest is not None else None,
            duration_to_destination_seconds=dur_dest,
            score=score,
            rank=0,    # проставим после сортировки
        )
        raw.append((score, candidate))

    # Сортировка: если selected_zone_id задан — он первый; остальные по убыванию score
    raw.sort(key=lambda x: x[0], reverse=True)
    if selected_zone_id is not None:
        raw.sort(key=lambda x: (0 if x[1].zone_id == selected_zone_id else 1, -x[0]))

    results = []
    for rank, (_, candidate) in enumerate(raw[:limit], start=1):
        candidate.rank = rank
        results.append(candidate)

    return results
