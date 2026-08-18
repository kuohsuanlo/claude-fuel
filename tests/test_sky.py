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


class TestScene:
    """Sky and pet share one background — there is no hole around him."""

    def _sprite(self):
        return (("ab", ".b"), ("ba", "a."))

    def test_transparent_pet_pixels_take_the_ground(self):
        """The pet used to be a cut-out with the terminal showing through, so
        the two read as unrelated widgets rather than as one scene."""
        from claude_swap.tui.skyview import SKY_H, SKY_W, ground_colour, scene_rows

        state = SkyState("clear", True, 0, 20.0, "", True)
        rows = scene_rows(state, 0, ("a." + "." * (SKY_W - 2),), {"a": "#ff0000"})
        assert len(rows) == (SKY_H + 1 + 1) // 2
        last = rows[-1]
        assert " " not in last.plain, "a transparent pet pixel left the ground bare"

    def test_the_ground_changes_with_the_weather(self):
        from claude_swap.tui.skyview import ground_colour

        clear = ground_colour(SkyState("clear", True, 0, 20.0, "", True))
        rain = ground_colour(SkyState("rain", True, 90, 15.0, "", True))
        night = ground_colour(SkyState("clear", False, 0, 12.0, "", True))
        assert len({clear, rain, night}) == 3
        # overcast is flatter and darker than clear daylight
        assert sum(int(rain[i:i + 2], 16) for i in (1, 3, 5)) < sum(
            int(clear[i:i + 2], 16) for i in (1, 3, 5)
        )

    def test_an_unknown_sky_still_has_a_ground(self):
        from claude_swap.tui.skyview import ground_colour

        assert ground_colour(SkyState()).startswith("#")


class TestTheBodyIsNeverCutOff:
    """A sky this small has one subject; a clipped one is a broken shape."""

    def _extent(self, is_day: bool, fraction: float):
        from claude_swap.tui.skyview import SKY_H, SKY_W, sky_pixels

        buf = sky_pixels(SkyState("clear", is_day, 0, 20.0, "", True), 0,
                         fraction=fraction)
        body = {"#ffcf5c", "#fff2b8"} if is_day else {"#e8e6dc", "#b9b7ae"}
        ys = [y for y in range(SKY_H) for x in range(SKY_W) if buf[y][x] in body]
        xs = [x for y in range(SKY_H) for x in range(SKY_W) if buf[y][x] in body]
        return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if ys else (0, 0)

    @pytest.mark.parametrize("is_day", [True, False])
    def test_full_height_at_every_hour(self, is_day: bool):
        """Eight pixels of panel could not hold a seven-pixel disc AND an arc
        for it to travel, so the panel is twelve — "bigger" and "never
        clipped" are one constraint on the panel's height."""
        for step in range(48):
            width, height = self._extent(is_day, step / 48.0)
            assert height == 7, f"cut to {height} rows at {step / 48.0:.2f}"

    def test_the_sun_keeps_its_full_width(self):
        for step in range(48):
            width, _ = self._extent(True, step / 48.0)
            assert width == 7, f"cut to {width} columns at {step / 48.0:.2f}"

    def test_the_body_crosses_the_panel_over_the_day(self):
        """It should travel, not sit in one place — position is the reading."""
        left, _ = self._extent(True, 0.26)
        right, _ = self._extent(True, 0.74)
        from claude_swap.tui.skyview import SKY_H, SKY_W, sky_pixels

        def centre(fraction):
            buf = sky_pixels(SkyState("clear", True, 0, 20.0, "", True), 0,
                             fraction=fraction)
            xs = [x for y in range(SKY_H) for x in range(SKY_W)
                  if buf[y][x] in {"#ffcf5c", "#fff2b8"}]
            return sum(xs) / len(xs)

        assert centre(0.74) - centre(0.26) > SKY_W * 0.5


class TestOverlaySitsOnTheScene:
    def test_a_letter_takes_the_sky_as_its_background(self):
        """Appended after the rows, the sleep puffs landed on the terminal's
        own background outside the painted scene, which reads as a rendering
        fault rather than as someone sleeping."""
        from claude_swap.tui.skyview import ground_colour, scene_rows

        state = SkyState("clear", False, 0, 12.0, "", True)
        rows = scene_rows(state, 0, ("." * 34,), {}, overlay=[(0, 5, "z", "#ffffff")])
        line = rows[0]
        assert line.plain[5] == "z"
        span = next(s for s in line.spans if s.start <= 5 < s.end)
        assert span.style.bgcolor is not None, "the letter is floating"
