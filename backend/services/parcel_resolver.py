"""Just-in-time APN resolution for SITE_SPECIFIC leads that have a real
street address but no printed Assessor's Parcel Number. Without this,
routers/ingest.py's _persist_to_supabase would otherwise have to drop
these leads entirely for lack of an APN to key a parcel on — confirmed
live this session: a real 20-city scrape produced 27 genuine site-specific
leads (a 432-unit project, several use-permit revisions, ...) with real
addresses but apn=None, none of which could be persisted.

Runs synchronously — this module is called from _persist_to_supabase,
itself plain synchronous background-task code — using ordinary httpx
rather than the curl_cffi/TLS-impersonation client the scraper needs;
Nominatim and the county's own ArcGIS service are public, keyless APIs
with no WAF blocking realistic clients.

Two tiers, tried in order:
  1. A direct attribute query against San Mateo County's own public
     parcel layer (ACRE/ACTIVE_PARCELS), matching the address text
     straight against its SITUS_ADDR field. Confirmed live: querying
     "1540 EL CAMINO REAL" / "MENLO PARK" returns APN 118420020 directly
     — no geocoding step needed at all for the common case.
  2. If that finds nothing (the address isn't in San Mateo County, or its
     wording doesn't match SITUS_ADDR closely enough), falls back to
     geocoding the address via Nominatim (OpenStreetMap's free, keyless
     geocoder) and running a spatial point-in-polygon query against the
     same parcel layer instead. Confirmed live, though noticeably less
     precise than tier 1: Nominatim's free-tier geocoding can resolve to
     the street itself rather than the specific building number for some
     addresses, which could occasionally land the point query in a
     neighboring parcel.

TODO(parcel-api): this covers San Mateo County only, and its matching is
best-effort — ambiguous for a multi-address string like "123 Foo St and
456 Bar Ave" (only the first address is resolved; see _first_address). A
building complex where several APNs share one situs address is no longer
a limitation — resolve_apns_from_address returns every matching APN (see
the "distinct APNs" log line in _query_smc_parcels_by_address), and
routers/ingest.py links a lead to all of them via the lead_parcels
junction table rather than picking just one. For broader geographic
coverage or more precise resolution, swap in a dedicated parcel API here
— Regrid (https://regrid.com, nationwide parcel data, paid API) or
another county's own ArcGIS service for leads outside San Mateo County.
"""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

# San Mateo County's own public parcel layer — confirmed live to expose a
# SITUS_ADDR field (the full situs/property address) alongside APN, so a
# real Assessor's Parcel Number can be resolved directly from address
# text with no geocoding step at all for the common case. Its native
# spatial reference is a projected CA State Plane system (EPSG:2227,
# confirmed live), not lat/lon — see _query_smc_parcels_by_point's inSR.
_SMC_PARCELS_QUERY_URL = (
    "https://gis.smcgov.org/maps/rest/services/ACRE/ACTIVE_PARCELS/MapServer/0/query"
)

# OpenStreetMap's free, keyless geocoder — used only as a fallback when
# the direct SITUS_ADDR match above finds nothing. Nominatim's usage
# policy (https://operations.osmfoundation.org/policies/nominatim/)
# requires a descriptive User-Agent and caps requests around 1/second;
# this module is only ever called a handful of times per ingest run, not
# in a tight loop, so no additional client-side rate-limiting is
# implemented here.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_USER_AGENT = "ZoningRadar/0.1 (San Mateo County zoning intelligence tool)"

_REQUEST_TIMEOUT_SECONDS = 10.0

# A signal's address can legitimately name more than one property (e.g.
# "123 Independence Dr and 138 Constitution Dr" — confirmed live from a
# real Gemini extraction) — only the first is used for resolution, rather
# than guessing which one a single resolved APN would actually refer to.
_MULTI_ADDRESS_SPLIT_PATTERN = re.compile(r"\s+and\s+|\s*[,;]\s*", re.IGNORECASE)


def _first_address(address: str) -> str:
    parts = _MULTI_ADDRESS_SPLIT_PATTERN.split(address.strip())
    return parts[0].strip() if parts else address.strip()


def _escape_sql_literal(value: str) -> str:
    """Escapes a value for safe interpolation into an ArcGIS `where`
    clause string literal (doubling any single quote). This endpoint's
    `where` parameter is a real SQL-like filter evaluated server-side,
    and `address`/`city` here ultimately originate from LLM-extracted PDF
    text — third-party document content that shouldn't be trusted to
    already be safe to interpolate."""
    return value.replace("'", "''")


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _run_smc_address_query(address: str, city: str | None) -> list[str]:
    where_clauses = [f"SITUS_ADDR LIKE '%{_escape_sql_literal(address.upper())}%'"]
    if city:
        where_clauses.append(f"SITUS_CITY = '{_escape_sql_literal(city.upper())}'")

    params = {
        "where": " AND ".join(where_clauses),
        "outFields": "APN",
        "returnGeometry": "false",
        "f": "json",
    }
    response = httpx.get(_SMC_PARCELS_QUERY_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()

    return [
        feature["attributes"]["APN"]
        for feature in payload.get("features") or []
        if feature.get("attributes", {}).get("APN")
    ]


def _query_smc_parcels_by_address(address: str, city: str | None) -> list[str]:
    """Tier 1: direct attribute query against San Mateo County's own
    ACRE/ACTIVE_PARCELS layer, matching `address` against its SITUS_ADDR
    field. Tries scoped to `city` first (SITUS_CITY equality) when given,
    for precision on a county-wide layer where the same street number can
    exist in more than one city — but retries without that filter if the
    scoped query finds nothing, since the county's own SITUS_CITY spelling
    doesn't always match a jurisdiction name confirmed live (a real S. San
    Francisco parcel had no match at all until the city filter was
    dropped). Returns every distinct matching APN (confirmed live: e.g.
    "1540 El Camino Real" matches 2 distinct APNs sharing that situs
    address — a real multi-parcel case, not a data error) — never just the
    first, so a caller can associate a lead with all of them rather than
    silently dropping every parcel but one."""
    apns = _run_smc_address_query(address, city)
    if not apns and city:
        apns = _run_smc_address_query(address, None)

    if not apns:
        return []
    distinct = sorted(set(apns))
    if len(distinct) > 1:
        logger.info(
            "SMC parcel address lookup for %r matched %d distinct APNs (%s) — returning all "
            "of them.",
            address,
            len(distinct),
            distinct,
        )
    return distinct


def _geocode_via_nominatim(address: str, city: str | None) -> tuple[float, float] | None:
    """Tier 2 helper: geocodes `address` (with `city`, when given, appended
    for disambiguation) via Nominatim, returning (lat, lon), or None if
    nothing was found."""
    query = f"{address}, {city}, CA" if city else f"{address}, California"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": _NOMINATIM_USER_AGENT}
    response = httpx.get(
        _NOMINATIM_URL, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def _query_smc_parcels_by_point(lat: float, lon: float) -> list[str]:
    """Tier 2: spatial point-in-polygon query against the same SMC parcel
    layer, using a geocoded point instead of address text. inSR=4326
    tells ArcGIS the input point is WGS84 lat/lon and to reproject it
    itself into the layer's native EPSG:2227 rather than requiring that
    conversion client-side. Returns every matching APN — a point can land
    on more than one polygon at a shared boundary — never just the first."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "APN",
        "returnGeometry": "false",
        "f": "json",
    }
    response = httpx.get(_SMC_PARCELS_QUERY_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()

    apns = [
        feature["attributes"]["APN"]
        for feature in payload.get("features") or []
        if feature.get("attributes", {}).get("APN")
    ]
    return _dedupe_preserve_order(apns)


def resolve_apns_from_address(address: str, city: str) -> list[str]:
    """Best-effort JIT APN resolution for a real street address with no
    printed APN. Tries San Mateo County's own parcel layer directly by
    address text first, then falls back to geocoding + a spatial lookup
    against the same layer. Returns every APN matched by whichever tier
    found something — a lead can legitimately span more than one parcel
    (an assemblage of adjacent lots, or several APNs sharing one situs
    address, both confirmed live) — never just the first. Returns an empty
    list (never raises) if neither tier resolves anything or if a request
    fails — a resolution failure is treated exactly like "no APN
    available", the same outcome _persist_to_supabase already handles for
    a lead with no address at all, rather than letting a network hiccup
    here crash the ingest run."""
    if not address or not address.strip():
        return []

    target_address = _first_address(address)

    try:
        apns = _query_smc_parcels_by_address(target_address, city)
        if apns:
            return apns
    except Exception:
        logger.warning(
            "SMC parcel address lookup failed for %r — falling back to geocoding.",
            target_address,
            exc_info=True,
        )

    try:
        point = _geocode_via_nominatim(target_address, city)
        if point is None:
            return []
        return _query_smc_parcels_by_point(*point)
    except Exception:
        logger.warning(
            "Geocode/spatial APN resolution failed for %r.", target_address, exc_info=True
        )
        return []
