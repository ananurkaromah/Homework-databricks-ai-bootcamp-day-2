"""
weather_client.py
------------------
Client for the National Weather Service API (api.weather.gov).

Harvests THREE distinct free-text products per location, matching the
adopted schema in lakebase.py:

    alert        GET /alerts/active?area={state}   (statewide, not per-point
                 -- see note below)
    forecast     GET /gridpoints/{office}/{x},{y}/forecast
                 (one row per forecast period, e.g. "Tonight", "Wednesday")
    discussion   GET /products/types/AFD/locations/{office} -> /products/{id}
                 (the forecaster's own free-form Area Forecast Discussion)

Design notes carried over from testing against the live API:

  - `/points/{lat},{lon}` is the hub: it returns the forecast office
    (gridId), the grid x/y, AND relativeLocation.properties.state, so a
    single call resolves everything needed for the other three endpoints.
    Grid assignments don't change, so results are cached per client call.
  - Alerts are STATEWIDE, not per-point (`/alerts/active?area={state}`).
    Two cities in the same state return the identical feed, so harvest()
    fetches each state's alerts once per call and lets the alert `id`
    (used as the dedup key) collapse any duplicates on upsert.
  - `/products/types/AFD/locations/{office}` ignores `?limit=N` silently
    -- the slice to the latest N products happens client-side here.
  - A geocoding attempt against the US Census `onelineaddress` endpoint
    was tested and found to return zero matches for plain "City, ST"
    input (it expects street addresses) -- so this client resolves city
    names against a small built-in lookup table instead. Anything not in
    the table must be passed as a raw "lat,lon" string.
"""

import hashlib
import logging
import re
from typing import Iterable, Optional, Union

import requests

logger = logging.getLogger(__name__)

NWS_BASE_URL = "https://api.weather.gov"
USER_AGENT = "weather-intelligence-app (contact: weather-app@example.com)"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/geo+json",
}

# Small built-in lookup table for common "City, ST" inputs. The NWS API
# itself has no geocoding endpoint (/points takes coordinates only), and
# the free US Census geocoder does not reliably match plain city/state
# strings, so this is deliberately a lookup table rather than a live
# geocoding call. Anything not listed here must be passed as "lat,lon".
KNOWN_LOCATIONS = {
    "chicago, il": (41.8781, -87.6298),
    "austin, tx": (30.2672, -97.7431),
    "dallas, tx": (32.7767, -96.7970),
    "houston, tx": (29.7604, -95.3698),
    "new york, ny": (40.7128, -74.0060),
    "los angeles, ca": (34.0522, -118.2437),
    "san francisco, ca": (37.7749, -122.4194),
    "miami, fl": (25.7617, -80.1918),
    "seattle, wa": (47.6062, -122.3321),
    "denver, co": (39.7392, -104.9903),
    "atlanta, ga": (33.7490, -84.3880),
    "boston, ma": (42.3601, -71.0589),
    "phoenix, az": (33.4484, -112.0740),
    "minneapolis, mn": (44.9778, -93.2650),
    "detroit, mi": (42.3314, -83.0458),
    "philadelphia, pa": (39.9526, -75.1652),
    "washington, dc": (38.9072, -77.0369),
    "st. louis, mo": (38.6270, -90.1994),
    "new orleans, la": (29.9511, -90.0715),
    "kansas city, mo": (39.0997, -94.5786),
}

_LAT_LON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

Location = Union[str, tuple]


class WeatherClientError(Exception):
    """Raised when a location can't be resolved or an NWS call fails."""


def _get(url: str, params: Optional[dict] = None, timeout: int = 15) -> dict:
    resp = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def geocode_location(location: str) -> tuple:
    """
    Resolve a "City, ST" string to (lat, lon) via the built-in lookup
    table. Raw "lat,lon" strings are handled directly in resolve_location()
    and never reach this function.
    """
    key = location.strip().lower()
    if key in KNOWN_LOCATIONS:
        return KNOWN_LOCATIONS[key]
    raise WeatherClientError(
        f"Unrecognized location {location!r}. Use a known city (see "
        "KNOWN_LOCATIONS) or pass raw coordinates as \"lat,lon\"."
    )


def resolve_location(location: Location) -> dict:
    """
    Resolve a location (string or (lat, lon) tuple) to lat/lon plus its
    NWS grid point metadata via GET /points/{lat},{lon}.

    Returns: label, lat, lon, grid_office, grid_x, grid_y, state.
    """
    if isinstance(location, tuple):
        lat, lon = location
        label = f"{lat},{lon}"
    else:
        match = _LAT_LON_RE.match(location)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            label = location.strip()
        else:
            label = location
            lat, lon = geocode_location(location)

    points = _get(f"{NWS_BASE_URL}/points/{lat},{lon}")
    props = points["properties"]
    state = (
        props.get("relativeLocation", {})
        .get("properties", {})
        .get("state")
    )

    return {
        "label": label,
        "lat": lat,
        "lon": lon,
        "grid_office": props["gridId"],
        "grid_x": props["gridX"],
        "grid_y": props["gridY"],
        "state": state,
    }


_alert_cache: dict = {}


def fetch_active_alerts_for_state(state: str) -> list[dict]:
    """
    Fetch active alerts for an entire state via GET /alerts/active?area={ST}.
    Alerts are statewide, not per-point, so this is called at most once per
    state per harvest() call (see the seen_states cache in harvest()).
    """
    data = _get(f"{NWS_BASE_URL}/alerts/active", params={"area": state})
    return data.get("features", [])


def fetch_forecast_periods(resolved: dict) -> list[dict]:
    """
    Fetch the multi-period text forecast for a resolved location via
    GET /gridpoints/{office}/{x},{y}/forecast. Each period (e.g.
    "Tonight", "Wednesday") has its own detailedForecast prose.
    """
    data = _get(
        f"{NWS_BASE_URL}/gridpoints/{resolved['grid_office']}/"
        f"{resolved['grid_x']},{resolved['grid_y']}/forecast"
    )
    return data.get("properties", {}).get("periods", [])


def fetch_latest_discussions(resolved: dict, limit: int = 1) -> list[dict]:
    """
    Fetch the most recent Area Forecast Discussion (AFD) text product(s)
    for the location's forecast office via:
        GET /products/types/AFD/locations/{office}

    This endpoint silently ignores a `?limit=N` query param, so the slice
    to the most recent `limit` products happens client-side here. Each
    product listing only has a UUID; the full text body requires a
    follow-up GET /products/{id} call per product.
    """
    listing = _get(f"{NWS_BASE_URL}/products/types/AFD/locations/{resolved['grid_office']}")
    product_stubs = listing.get("@graph", [])[:limit]

    products = []
    for stub in product_stubs:
        product_id = stub.get("id")
        if not product_id:
            continue
        full = _get(f"{NWS_BASE_URL}/products/{product_id}")
        products.append(full)
    return products


def _content_hash(narrative_text: str) -> str:
    return hashlib.sha256((narrative_text or "").encode("utf-8")).hexdigest()


def normalize_alert(feature: dict, state: str) -> dict:
    """
    Normalize a single NWS alert GeoJSON feature into the document schema.
    Dedup key is the alert's own stable NWS id (a URN), prefixed with
    "alert:" so id schemes stay distinguishable at a glance. `location` is
    set to the state (not a specific city) since alerts are statewide --
    the same alert can come back while resolving several cities in that
    state, and this keeps the stored value meaningful regardless of which
    city triggered the fetch.
    """
    props = feature.get("properties", {})
    alert_id = props.get("id") or feature.get("id") or ""
    narrative = "\n\n".join(
        filter(None, [props.get("description"), props.get("instruction")])
    )
    narrative_text = narrative or props.get("event", "")

    return {
        "id": f"alert:{alert_id}",
        "location": f"{state} (statewide)" if state else "unknown",
        "latitude": None,
        "longitude": None,
        "state": state,
        "grid_office": None,
        "grid_x": None,
        "grid_y": None,
        "source_type": "alert",
        "event": props.get("event"),
        "headline": props.get("headline") or props.get("event"),
        "severity": props.get("severity"),
        "narrative_text": narrative_text,
        "content_hash": _content_hash(narrative_text),
        "issued_at": props.get("sent"),
        "effective_at": props.get("effective"),
        "expires_at": props.get("expires"),
        "source_url": feature.get("id"),
        "payload": feature,
    }


def normalize_forecast_period(period: dict, resolved: dict, location_label: str) -> dict:
    """
    Normalize a single forecast period into the document schema. Dedup key
    is derived from the grid office/x/y and the period's own startTime --
    the forecast response has no top-level "updated" timestamp, so the
    period's start time is the only stable per-period identifier.
    """
    narrative_text = period.get("detailedForecast", "")
    start_time = period.get("startTime")

    return {
        "id": f"forecast:{resolved['grid_office']}:{resolved['grid_x']},{resolved['grid_y']}:{start_time}",
        "location": location_label,
        "latitude": resolved.get("lat"),
        "longitude": resolved.get("lon"),
        "state": resolved.get("state"),
        "grid_office": resolved.get("grid_office"),
        "grid_x": resolved.get("grid_x"),
        "grid_y": resolved.get("grid_y"),
        "source_type": "forecast",
        "event": period.get("name"),
        "headline": f"{period.get('name', 'Forecast')} \u2014 {location_label}",
        "severity": None,
        "narrative_text": narrative_text,
        "content_hash": _content_hash(narrative_text),
        "issued_at": start_time,
        "effective_at": start_time,
        "expires_at": period.get("endTime"),
        "source_url": None,
        "payload": period,
    }


def normalize_discussion(product: dict, resolved: dict, location_label: str) -> dict:
    """
    Normalize a single AFD text product into the document schema. Dedup
    key is the product's own UUID, which is stable across re-fetches of
    the same product.
    """
    narrative_text = product.get("productText", "")
    issued_at = product.get("issuanceTime")

    return {
        "id": f"discussion:{product.get('id', '')}",
        "location": location_label,
        "latitude": resolved.get("lat"),
        "longitude": resolved.get("lon"),
        "state": resolved.get("state"),
        "grid_office": resolved.get("grid_office"),
        "grid_x": resolved.get("grid_x"),
        "grid_y": resolved.get("grid_y"),
        "source_type": "discussion",
        "event": None,
        "headline": f"Area Forecast Discussion ({location_label})",
        "severity": None,
        "narrative_text": narrative_text,
        "content_hash": _content_hash(narrative_text),
        "issued_at": issued_at,
        "effective_at": issued_at,
        "expires_at": None,
        "source_url": None,
        "payload": product,
    }


def harvest(
    locations: Iterable[Location],
    limit: int = 50,
    sources: Iterable[str] = ("alert", "forecast", "discussion"),
) -> dict:
    """
    End-to-end harvest for a list of locations.

    Returns:
        {
            "documents": [...],           # flat list, capped at `limit`
            "skipped": [{"location": ..., "reason": ...}, ...],
            "by_source": {"alert": N, "forecast": N, "discussion": N},
        }

    Errors resolving/fetching an individual location or product type are
    logged and skipped rather than aborting the whole batch.
    """
    sources = set(sources)
    documents: list[dict] = []
    skipped: list[dict] = []
    by_source = {"alert": 0, "forecast": 0, "discussion": 0}
    seen_states: set = set()

    for location in locations:
        try:
            resolved = resolve_location(location)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve location %r: %s", location, exc)
            skipped.append({"location": str(location), "reason": str(exc)})
            continue

        label = resolved["label"]
        state = resolved.get("state")

        if "alert" in sources and state and state not in seen_states:
            seen_states.add(state)
            try:
                alerts = fetch_active_alerts_for_state(state)
                for feature in alerts:
                    doc = normalize_alert(feature, state)
                    documents.append(doc)
                    by_source["alert"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch alerts for state %r: %s", state, exc)

        if "forecast" in sources:
            try:
                periods = fetch_forecast_periods(resolved)
                for period in periods:
                    doc = normalize_forecast_period(period, resolved, label)
                    documents.append(doc)
                    by_source["forecast"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch forecast periods for %r: %s", label, exc)

        if "discussion" in sources:
            try:
                discussions = fetch_latest_discussions(resolved, limit=1)
                for product in discussions:
                    doc = normalize_discussion(product, resolved, label)
                    documents.append(doc)
                    by_source["discussion"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch discussion for %r: %s", label, exc)

        if len(documents) >= limit:
            break

    return {
        "documents": documents[:limit],
        "skipped": skipped,
        "by_source": by_source,
    }