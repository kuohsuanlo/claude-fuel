"""Where the sun is, and what the weather is doing, for the pet's sky panel.

COSTS NO TOKENS. Nothing here goes near a model: the sun's position is
arithmetic on the local clock, and the weather is one small JSON request to a
key-less public endpoint, made on a background thread at most a few times an
hour. A dashboard that spent quota to decorate itself would be absurd, given
the whole screen exists to stop quota being wasted.

NEVER BLOCKS, NEVER RAISES. The UI paints every frame whether or not the
network is reachable; a fetch that fails leaves the last good reading in place
and, failing that, a plain clear sky. Weather is decoration — the moment it
can delay a repaint or take the screen down it has cost more than it is worth.

THREE SOURCES OF LOCATION, cheapest first: an explicit setting, then a
key-less IP lookup, then the system timezone's own representative longitude.
The last one needs no network at all and is accurate to about an hour of sun
position, which is all a sixteen-pixel sky can show anyway.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_GEO_URL = (
    "http://ip-api.com/json/?fields=status,city,regionName,lat,lon,timezone"
)
_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat:.3f}&longitude={lon:.3f}"
    "&current=temperature_2m,weather_code,cloud_cover,is_day&timezone=auto"
)

# Weather changes on the scale of tens of minutes and this is a decoration, so
# the endpoint is asked rarely. Being a good citizen of a free public API
# matters more here than freshness.
REFRESH_S = 20 * 60.0

# Anything slower than this is not worth waiting for on a background thread
# that will simply try again later.
_TIMEOUT_S = 6.0

# WMO weather codes, grouped into the only distinctions a tiny sky can draw.
# The full table has 28 entries and most of them differ by intensity, which is
# invisible at this size.
_CLEAR = {0, 1}
_CLOUD = {2, 3, 45, 48}
_RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_SNOW = {71, 73, 75, 77, 85, 86}
_STORM = {95, 96, 99}


@dataclass(frozen=True)
class SkyState:
    """What the sky panel should draw.

    ``fresh`` is False for the fallback — a caller may want to say "weather
    unknown" rather than quietly present a default as a measurement.
    """

    kind: str = "clear"  # clear | cloud | rain | snow | storm
    is_day: bool = True
    cloud_cover: int = 0  # percent
    temperature: float | None = None
    place: str = ""
    fresh: bool = False

    @property
    def label(self) -> str:
        """One short phrase for a caption, e.g. ``Taipei 27° clear``."""
        bits = [self.place] if self.place else []
        if self.temperature is not None:
            bits.append(f"{self.temperature:.0f}°")
        bits.append(self.kind if self.fresh else "weather unknown")
        return " ".join(bits)


def day_fraction(now: float | None = None) -> float:
    """Where we are in the local day, 0.0 at midnight to 1.0 at the next.

    Local rather than UTC on purpose: the panel answers "is it dark outside
    THIS window", and the machine's own clock is the most reliable statement
    of that available without asking anyone.
    """
    stamp = datetime.fromtimestamp(now if now is not None else time.time())
    seconds = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
    return seconds / 86400.0


def arc_position(fraction: float, *, is_day: bool) -> float:
    """0.0 (rising) to 1.0 (setting) along whichever body is up.

    Daylight is treated as 06:00-18:00 and night as the complement. Real
    sunrise moves by an hour or two across the year and by latitude, but the
    panel has room for about eight positions — a correction finer than its own
    resolution would be arithmetic nobody can see.
    """
    if is_day:
        return min(1.0, max(0.0, (fraction - 0.25) / 0.5))
    night = (fraction + 0.25) % 1.0
    return min(1.0, max(0.0, night / 0.5))


def _timezone_longitude() -> float:
    """A representative longitude from the machine's UTC offset.

    Fifteen degrees per hour. Crude, and entirely offline — good to about an
    hour of sun position, which is finer than the panel can draw.
    """
    offset = -time.timezone if not time.daylight else -time.altzone
    return max(-180.0, min(180.0, offset / 3600.0 * 15.0))


def _fetch_json(url: str) -> dict | None:
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "claude-swap-reloaded"}
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _classify(code: object, cloud: int) -> str:
    if isinstance(code, (int, float)):
        value = int(code)
        if value in _STORM:
            return "storm"
        if value in _SNOW:
            return "snow"
        if value in _RAIN:
            return "rain"
        if value in _CLOUD:
            return "cloud"
        if value in _CLEAR:
            # The code says clear, but "clear" at 80% cover is not what anyone
            # sees out of the window; the cover reading breaks the tie.
            return "cloud" if cloud >= 60 else "clear"
    return "cloud" if cloud >= 60 else "clear"


class SkyWatcher:
    """Keeps one :class:`SkyState` current, on a background thread.

    The UI only ever reads :meth:`state`, which returns immediately with
    whatever is known — last reading, cached reading, or the fallback.
    """

    def __init__(self, cache_path: Path | None = None, location: str | None = None):
        self.cache_path = cache_path
        self._configured = location
        self._state = SkyState()
        self._lock = threading.Lock()
        self._last_try = 0.0
        self._thread: threading.Thread | None = None
        self._load_cache()

    # -- reading ------------------------------------------------------------

    def state(self) -> SkyState:
        """The current sky. Never blocks; kicks off a refresh when due."""
        with self._lock:
            state = self._state
        if time.time() - self._last_try >= REFRESH_S:
            self.refresh()
        return state

    def refresh(self) -> None:
        """Start one background fetch, if none is already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._last_try = time.time()
        self._thread = threading.Thread(target=self._fetch, daemon=True)
        self._thread.start()

    # -- fetching -----------------------------------------------------------

    def _coords(self) -> tuple[float, float, str]:
        if self._configured:
            try:
                lat, _, lon = self._configured.partition(",")
                return float(lat), float(lon), ""
            except ValueError:
                pass
        geo = _fetch_json(_GEO_URL)
        if geo and geo.get("status") == "success":
            place = geo.get("city") or geo.get("regionName") or ""
            return float(geo["lat"]), float(geo["lon"]), place
        # Offline last resort: the timezone's own longitude, latitude zero.
        # Wrong about temperature, right about roughly when the sun is up,
        # which is the part the panel actually draws.
        return 0.0, _timezone_longitude(), ""

    def _fetch(self) -> None:
        try:
            lat, lon, place = self._coords()
            data = _fetch_json(_WEATHER_URL.format(lat=lat, lon=lon))
            current = (data or {}).get("current") or {}
            if not current:
                return
            cloud = int(current.get("cloud_cover") or 0)
            state = SkyState(
                kind=_classify(current.get("weather_code"), cloud),
                is_day=bool(current.get("is_day", 1)),
                cloud_cover=cloud,
                temperature=(
                    float(current["temperature_2m"])
                    if current.get("temperature_2m") is not None
                    else None
                ),
                place=place,
                fresh=True,
            )
            with self._lock:
                self._state = state
            self._save_cache(state)
        except Exception:
            # Decoration must never take the screen down, and a failure here
            # simply leaves the previous reading in place.
            pass

    # -- cache --------------------------------------------------------------

    def _load_cache(self) -> None:
        """Seed from disk so a fresh process is not blank for its first fetch."""
        if self.cache_path is None:
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - float(raw["at"]) > 6 * 3600:
                return  # older than the weather it describes
            with self._lock:
                self._state = SkyState(
                    kind=str(raw.get("kind", "clear")),
                    is_day=bool(raw.get("is_day", True)),
                    cloud_cover=int(raw.get("cloud_cover", 0)),
                    temperature=raw.get("temperature"),
                    place=str(raw.get("place", "")),
                    fresh=True,
                )
        except Exception:
            pass

    def _save_cache(self, state: SkyState) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "at": time.time(),
                        "kind": state.kind,
                        "is_day": state.is_day,
                        "cloud_cover": state.cloud_cover,
                        "temperature": state.temperature,
                        "place": state.place,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
