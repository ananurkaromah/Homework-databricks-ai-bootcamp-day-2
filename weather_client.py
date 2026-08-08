"""
weather_client.py
------------------
Client for the National Weather Service API (api.weather.gov).

Given a list of locations (either "City, ST" strings or (lat, lon) tuples),
this module:
  1. Resolves each location to lat/lon (geocoding "City, ST" via the free
     Census Bureau geocoder — api.weather.gov itself does not geocode
     place names, only raw coordinates, so this extra hop is required).
  2. Resolves lat/lon to a NWS grid point via GET /points/{lat},{lon}.
  3. Fetches active alerts for that point (GET /alerts/active).
  4. Fetches the latest Area Forecast Discussion (AFD) text product for
     the point's CWA (GET /products/types/AFD/locations/{cwa}).
  5. Normalizes every alert / forecast discussion into a flat document
     record ready to hand to lakebase.upsert_weather_documents().

NWS API conventions used here:
  - Base URL: https://api.weather.gov
  - A descriptive User-Agent header is required by NWS (they ask for an
    app name + contact, not a browser-style UA string).
  - JSON responses use the GeoJSON-ish "properties" envelope for most
    endpoints.

No API key is required for api.weather.gov.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional, Union

import requests

logger = logging.getLogger(__name__)

NWS_BASE_URL = "https://api.weather.gov"
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

USER_AGENT = "weather-intelligence-app (contact: weather-app@example.com)"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/geo+json",
}

Location = Union[str, tuple]


class WeatherClientError(Exception):
    """Raised for unrecoverable errors talking to NWS / the geocoder."""


def _get(url: str, params: Optional[dict] = None, timeout: int = 15) -> dict:
    resp = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def geocode_location(location: str) -> tuple:
    """
    Resolve a "City, ST" (or general address) string to (lat, lon) using
    the free US Census Bureau geocoder. Only used for string locations —
    if the caller already passes (lat, lon), this is skipped entirely.
    """
    params = {
        "address": location,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    data = requests.get(CENSUS_GEOCODER_URL, params=params, timeout=15).json()
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        raise WeatherClientError(f"Could not geocode location: {location!r}")
    coords = matches[0]["coordinates"]
    return float(coords["y"]), float(coords["x"])  # (lat, lon)


def resolve_location(location: Location) -> dict:
    """
    Resolve a location (string or (lat, lon) tuple) to lat/lon plus its
    NWS grid point metadata via GET /points/{lat},{lon}.

    Returns a dict with: label, lat, lon, grid_id, grid_x, grid_y, cwa,
    forecast_url, forecast_zone.
    """
    if isinstance(location, str):
        label = location
        lat, lon = geocode_location(location)
    else:
        lat, lon = location
        label = f"{lat},{lon}"

    points = _get(f"{NWS_BASE_URL}/points/{lat},{lon}")
    props = points["properties"]

    return {
        "label": label,
        "lat": lat,
        "lon": lon,
        "grid_id": props["gridId"],
        "grid_x": props["gridX"],
        "grid_y": props["gridY"],
        "cwa": props["cwa"],
        "forecast_zone": props.get("forecastZone"),
    }


def fetch_active_alerts(resolved: dict) -> list[dict]:
    """
    Fetch active alerts for a resolved location's coordinates via
    GET /alerts/active?point={lat},{lon}.
    """
    data = _get(
        f"{NWS_BASE_URL}/alerts/active",
        params={"point": f"{resolved['lat']},{resolved['lon']}"},
    )
    return data.get("features", [])


def fetch_forecast_discussions(resolved: dict, limit: int = 1) -> list[dict]:
    """
    Fetch the most recent Area Forecast Discussion (AFD) text product(s)
    for the location's CWA (county warning area) via:
        GET /products/types/AFD/locations/{cwa}

    Each product listing only has a UUID; the full text body requires a
    follow-up GET /products/{id} call, which this function performs for
    each of the `limit` most recent products.
    """
    listing = _get(
        f"{NWS_BASE_URL}/products/types/AFD/locations/{resolved['cwa']}"
    )
    product_stubs = listing.get("@graph", [])[:limit]

    products = []
    for stub in product_stubs:
        product_id = stub.get("id")
        if not product_id:
            continue
        full = _get(f"{NWS_BASE_URL}/products/{product_id}")
        products.append(full)
    return products


def _stable_hash(*parts: str) -> str:
    key = "|".join(p or "" for p in parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def normalize_alert(feature: dict, location_label: str) -> dict:
    """
    Normalize a single NWS alert GeoJSON feature into the shared document
    schema. Dedup key is the alert's own stable `id` field (a NWS-issued
    URN), which is unique per alert.
    """
    props = feature.get("properties", {})
    narrative = "\n\n".join(
        filter(None, [props.get("description"), props.get("instruction")])
    )
    return {
        "id": feature.get("id") or _stable_hash(location_label, props.get("headline", "")),
        "location": location_label,
        "source_type": "alert",
        "headline": props.get("headline") or props.get("event"),
        "narrative_text": narrative or props.get("event", ""),
        "issued_at": props.get("sent"),
        "effective_at": props.get("effective"),
        "payload": feature,
    }


def normalize_forecast(product: dict, location_label: str) -> dict:
    """
    Normalize a single AFD text product into the shared document schema.
    Dedup key is a hash of location + issuance time, since AFD products
    don't expose a short stable business key the way alerts do.
    """
    issued_at = product.get("issuanceTime")
    narrative_text = product.get("productText", "")
    return {
        "id": _stable_hash(location_label, issued_at or "", product.get("id", "")),
        "location": location_label,
        "source_type": "forecast",
        "headline": f"Area Forecast Discussion ({location_label})",
        "narrative_text": narrative_text,
        "issued_at": issued_at,
        "effective_at": issued_at,
        "payload": product,
    }


def harvest(locations: Iterable[Location], limit: int = 50) -> list[dict]:
    """
    End-to-end harvest for a list of locations: resolve -> fetch alerts +
    forecast discussions -> normalize -> return a flat list of document
    records, capped at `limit` total documents.

    Errors resolving/fetching an individual location are logged and
    skipped rather than aborting the whole batch, so one bad location
    input doesn't fail the entire sync.
    """
    documents: list[dict] = []

    for location in locations:
        try:
            resolved = resolve_location(location)
        except Exception as exc:  # noqa: BLE001 - log and continue per-location
            logger.warning("Failed to resolve location %r: %s", location, exc)
            continue

        label = resolved["label"]

        try:
            alerts = fetch_active_alerts(resolved)
            for feature in alerts:
                documents.append(normalize_alert(feature, label))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch alerts for %r: %s", label, exc)

        try:
            forecasts = fetch_forecast_discussions(resolved, limit=1)
            for product in forecasts:
                documents.append(normalize_forecast(product, label))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch forecast discussion for %r: %s", label, exc)

        if len(documents) >= limit:
            break

    return documents[:limit]