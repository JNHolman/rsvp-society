"""Postal-code resolution for RSVP Society member location.

New public signups provide a US ZIP code. The backend resolves it to canonical
city/state plus centroid coordinates so invite geography can use actual location
instead of phone area codes.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit
import os
import re
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ZIP_RE = re.compile(r"^\d{5}$")
DEFAULT_BASE_URL = "https://api.zippopotam.us/us"


class InvalidZipError(ValueError):
    """The supplied ZIP is malformed or does not resolve to a US postal code."""


class ZipLookupUnavailable(RuntimeError):
    """The external postal lookup could not be reached or returned invalid data."""


def resolve_us_zip(zip_code: str, *, timeout: float = 3.0) -> Dict[str, Any]:
    code = (zip_code or "").strip()
    if not ZIP_RE.fullmatch(code):
        raise InvalidZipError("Enter a valid 5-digit ZIP code")

    base = (os.getenv("ZIP_LOOKUP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ZipLookupUnavailable("ZIP lookup must use a valid HTTPS base URL")
    req = Request(
        f"{base}/{code}",
        headers={"Accept": "application/json", "User-Agent": "rsvp-society/1.0"},
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise InvalidZipError("Enter a valid U.S. ZIP code") from exc
        raise ZipLookupUnavailable("ZIP lookup is temporarily unavailable") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ZipLookupUnavailable("ZIP lookup is temporarily unavailable") from exc

    places = payload.get("places") or []
    if not places:
        raise InvalidZipError("Enter a valid U.S. ZIP code")

    place = places[0] or {}
    city = str(place.get("place name") or "").strip()
    state = str(place.get("state abbreviation") or place.get("state") or "").strip()
    lat = str(place.get("latitude") or "").strip()
    lon = str(place.get("longitude") or "").strip()
    if not city or not state or not lat or not lon:
        raise ZipLookupUnavailable("ZIP lookup returned incomplete location data")

    try:
        latitude = float(lat)
        longitude = float(lon)
    except ValueError as exc:
        raise ZipLookupUnavailable("ZIP lookup returned invalid coordinates") from exc

    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ZipLookupUnavailable("ZIP lookup returned invalid coordinates")

    return {
        "zipCode": code,
        "city": city,
        "state": state,
        "latitude": latitude,
        "longitude": longitude,
        "locationSource": "zip",
    }
