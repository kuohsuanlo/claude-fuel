"""Tests for the weather/sun panel (sky.py)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.sky import SkyState, SkyWatcher, _classify, arc_position, day_fraction


class TestClassify:
    def test_wmo_codes_map_to_drawable_groups(self):
        assert _classify(0, 5) == "clear"
        assert _classify(3, 5) == "cloud"
        assert _classify(63, 5) == "rain"
        assert _classify(75, 5) == "snow"
        assert _classify(95, 5) == "storm"

    def test_cover_breaks_a_clear_code_that_is_not_clear(self):
        """"Clear" at 80% cover is not what anyone sees out of the window."""
        assert _classify(0, 80) == "cloud"

    def test_an_unknown_code_falls_back_to_cover(self):
        assert _classify(None, 90) == "cloud"
        assert _classify("nonsense", 10) == "clear"


class TestArc:
    def test_the_body_rises_and_sets(self):
        assert arc_position(0.25, is_day=True) == pytest.approx(0.0)   # 06:00
        assert arc_position(0.50, is_day=True) == pytest.approx(0.5)   # noon
        assert arc_position(0.75, is_day=True) == pytest.approx(1.0)   # 18:00

    def test_night_wraps_across_midnight(self):
        """Night runs 18:00 to 06:00, so its middle is midnight — the arc has
        to cross the date boundary without jumping."""
        assert arc_position(0.75, is_day=False) == pytest.approx(0.0)
        assert arc_position(0.0, is_day=False) == pytest.approx(0.5)
        assert arc_position(0.245, is_day=False) == pytest.approx(0.99, abs=0.02)

    def test_day_fraction_tracks_the_local_clock(self):
        assert 0.0 <= day_fraction() < 1.0


class TestWatcherNeverBlocksOrRaises:
    def test_state_is_available_before_any_fetch(self, tmp_path: Path):
        """The UI paints every frame whether or not the network is reachable."""
        watcher = SkyWatcher(cache_path=tmp_path / "sky.json")
        with patch("claude_swap.sky.SkyWatcher.refresh"):
            state = watcher.state()
        assert isinstance(state, SkyState)
        assert state.fresh is False, "a fallback must not claim to be a reading"

    def test_a_failed_fetch_leaves_the_last_reading_alone(self, tmp_path: Path):
        watcher = SkyWatcher(cache_path=tmp_path / "sky.json")
        good = SkyState("rain", True, 90, 18.0, "Somewhere", True)
        watcher._state = good
        with patch("claude_swap.sky._fetch_json", return_value=None):
            watcher._fetch()
        assert watcher._state is good

    def test_a_raising_fetch_is_swallowed(self, tmp_path: Path):
        """Decoration must never take the screen down."""
        watcher = SkyWatcher(cache_path=tmp_path / "sky.json")
        with patch("claude_swap.sky._fetch_json", side_effect=OSError("no network")):
            watcher._fetch()
        assert watcher.state() is not None

    def test_a_good_fetch_is_cached_and_reloaded(self, tmp_path: Path):
        cache = tmp_path / "sky.json"
        watcher = SkyWatcher(cache_path=cache)
        payload = {
            "current": {
                "weather_code": 61,
                "cloud_cover": 88,
                "is_day": 0,
                "temperature_2m": 17.5,
            }
        }
        with patch("claude_swap.sky._fetch_json", return_value=payload), patch.object(
            SkyWatcher, "_coords", return_value=(25.0, 121.5, "Taipei")
        ):
            watcher._fetch()
        assert watcher.state().kind == "rain"
        assert json.loads(cache.read_text())["kind"] == "rain"
        assert SkyWatcher(cache_path=cache).state().kind == "rain"

    def test_a_stale_cache_is_ignored(self, tmp_path: Path):
        """Six-hour-old weather is not weather."""
        cache = tmp_path / "sky.json"
        cache.write_text(json.dumps({"at": time.time() - 40_000, "kind": "storm"}))
        assert SkyWatcher(cache_path=cache)._state.fresh is False

    def test_a_corrupt_cache_is_ignored(self, tmp_path: Path):
        cache = tmp_path / "sky.json"
        cache.write_text("{not json")
        assert SkyWatcher(cache_path=cache)._state.fresh is False

    def test_a_configured_location_skips_the_network(self, tmp_path: Path):
        watcher = SkyWatcher(cache_path=tmp_path / "sky.json", location="25.03,121.56")
        with patch("claude_swap.sky._fetch_json") as fetch:
            lat, lon, _ = watcher._coords()
        fetch.assert_not_called()
        assert (round(lat, 2), round(lon, 2)) == (25.03, 121.56)


class TestLabel:
    def test_an_unknown_sky_says_so(self):
        """A default presented as a measurement is worse than an admission."""
        assert "unknown" in SkyState().label

    def test_a_reading_reads_as_a_place_and_a_temperature(self):
        assert SkyState("rain", True, 90, 17.4, "Taipei", True).label == (
            "Taipei 17° rain"
        )
