"""Tests for local burn-rate sensing (burn.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap.burn import (
    BURST_MULTIPLIER,
    BURST_WINDOW_S,
    BurnEstimate,
    BurnTracker,
    TranscriptBurnSensor,
    weigh_usage,
)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _assistant_line(
    *,
    message_id: str,
    ts: float,
    output: int = 0,
    input_: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": _iso(ts),
            "message": {
                "id": message_id,
                "usage": {
                    "input_tokens": input_,
                    "output_tokens": output,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            },
        }
    )


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    (root / "-home-user-proj").mkdir(parents=True)
    return root


def _session(projects: Path, name: str = "s1") -> Path:
    return projects / "-home-user-proj" / f"{name}.jsonl"


def _append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


class TestWeighUsage:
    def test_weights_output_above_input(self):
        assert weigh_usage({"output_tokens": 100}) > weigh_usage({"input_tokens": 100})

    def test_cache_reads_are_cheap(self):
        assert weigh_usage({"cache_read_input_tokens": 100}) < weigh_usage(
            {"input_tokens": 100}
        )

    def test_ignores_non_token_members(self):
        """`usage` carries `iterations`, which REPEATS the same counts — summing
        it blindly would double-count exactly the busiest requests."""
        plain = weigh_usage({"output_tokens": 10})
        noisy = weigh_usage(
            {
                "output_tokens": 10,
                "service_tier": "standard",
                "server_tool_use": {"web_search_requests": 3},
                "iterations": [{"output_tokens": 10, "input_tokens": 999}],
            }
        )
        assert plain == noisy

    def test_bad_input_is_zero(self):
        assert weigh_usage(None) == 0.0
        assert weigh_usage({"output_tokens": "lots"}) == 0.0
        assert weigh_usage({"output_tokens": True}) == 0.0


class TestSensorIngest:
    def test_first_sighting_does_not_replay_history(self, projects: Path):
        """A pre-existing transcript is scale, not spend: replaying it would
        date a whole session's tokens to startup and poison every rate."""
        path = _session(projects)
        _append(path, _assistant_line(message_id="m1", ts=999_000.0, output=50_000))
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        assert sensor.tokens_per_s() == 0.0

    def test_counts_growth_after_first_sighting(self, projects: Path):
        path = _session(projects)
        _append(path, _assistant_line(message_id="m0", ts=999_000.0, output=1))
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=100))
        sensor.poll()
        assert sensor.tokens_since(clock.now - 10) == pytest.approx(500.0)

    def test_deduplicates_content_block_lines(self, projects: Path):
        """Claude Code writes one line per content block, all sharing a message
        id and an identical `usage` — measured 85 lines for 38 real requests."""
        path = _session(projects)
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        line = _assistant_line(message_id="dup", ts=clock.now, output=100)
        _append(path, line, line, line)
        sensor.poll()
        assert sensor.tokens_since(clock.now - 10) == pytest.approx(500.0)

    def test_partial_trailing_line_is_not_consumed_until_complete(
        self, projects: Path
    ):
        path = _session(projects)
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        full = _assistant_line(message_id="m1", ts=clock.now, output=100)
        head, tail = full[:20], full[20:]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(head)
        sensor.poll()
        assert sensor.tokens_per_s() == 0.0
        with path.open("a", encoding="utf-8") as handle:
            handle.write(tail + "\n")
        sensor.poll()
        assert sensor.tokens_since(clock.now - 10) == pytest.approx(500.0)

    def test_truncation_resets_cursor_without_crashing(self, projects: Path):
        path = _session(projects)
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=100))
        sensor.poll()
        path.write_text("", encoding="utf-8")
        sensor.poll()  # must not raise, must not re-read
        _append(path, _assistant_line(message_id="m2", ts=clock.now, output=20))
        sensor.poll()
        assert sensor.tokens_since(clock.now - 10) == pytest.approx(600.0)

    def test_ignores_non_assistant_and_malformed_lines(self, projects: Path):
        path = _session(projects)
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        _append(
            path,
            json.dumps({"type": "user", "message": {"usage": {"output_tokens": 999}}}),
            '{"type": "assistant", "message": {"usage": broken',
        )
        sensor.poll()
        assert sensor.tokens_per_s() == 0.0

    def test_missing_projects_dir_is_silent(self, tmp_path: Path):
        sensor = TranscriptBurnSensor(tmp_path / "nope", clock=FakeClock())
        sensor.poll()
        assert sensor.tokens_per_s() == 0.0

    def test_samples_outside_window_are_pruned(self, projects: Path):
        path = _session(projects)
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, window_s=100.0, clock=clock)
        sensor.poll()
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=100))
        sensor.poll()
        assert sensor.tokens_per_s(60.0) > 0
        clock.advance(200.0)
        sensor.poll()
        assert sensor.tokens_per_s(60.0) == 0.0


class TestCalibration:
    def _tracker(self, projects: Path, clock: FakeClock) -> tuple[BurnTracker, Path]:
        path = _session(projects)
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        return BurnTracker(sensor=sensor, clock=clock), path

    def test_uncalibrated_falls_back_to_api_average(self, projects: Path):
        clock = FakeClock()
        tracker, _ = self._tracker(projects, clock)
        tracker.observe("1", "5h", 10.0, clock.now)
        clock.advance(600.0)
        tracker.observe("1", "5h", 16.0, clock.now)
        est = tracker.estimate("1", "5h")
        assert est.source == "api"
        assert est.calibrated is False
        assert est.pct_per_s == pytest.approx(0.01)

    def test_calibrates_from_bracketed_interval(self, projects: Path):
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        tracker.observe("1", "5h", 10.0, clock.now)
        clock.advance(60.0)
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=200))
        tracker.sensor.poll()
        tracker.observe("1", "5h", 12.0, clock.now)
        # 2 pct across 1000 weighted tokens.
        assert tracker.pct_per_token() == pytest.approx(0.002)
        est = tracker.estimate("1", "5h")
        assert est.source == "local"
        assert est.calibrated is True

    def test_window_rollover_is_not_calibration_data(self, projects: Path):
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        tracker.observe("1", "5h", 90.0, clock.now)
        clock.advance(60.0)
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=200))
        tracker.sensor.poll()
        tracker.observe("1", "5h", 2.0, clock.now)  # window reset
        assert tracker.pct_per_token() is None

    def test_remote_burn_is_not_attributed_to_local_tokens(self, projects: Path):
        """Percent moved with no local spend — another machine is burning the
        same account. Real, but not a statement about our tokens-to-pct ratio."""
        clock = FakeClock()
        tracker, _ = self._tracker(projects, clock)
        tracker.observe("1", "5h", 10.0, clock.now)
        clock.advance(60.0)
        tracker.observe("1", "5h", 30.0, clock.now)
        assert tracker.pct_per_token() is None

    def test_restated_snapshot_is_idempotent(self, projects: Path):
        """Every surface re-reads the same stored row between fetches; that
        must not add observations or calibration samples."""
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        tracker.observe("1", "5h", 10.0, clock.now)
        clock.advance(60.0)
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=200))
        tracker.sensor.poll()
        tracker.observe("1", "5h", 12.0, clock.now)
        first = tracker.pct_per_token()
        for _ in range(5):
            tracker.observe("1", "5h", 12.0, clock.now)
        assert tracker.pct_per_token() == first

    def test_accounts_keep_separate_observations(self, projects: Path):
        clock = FakeClock()
        tracker, _ = self._tracker(projects, clock)
        tracker.observe("1", "5h", 10.0, clock.now)
        tracker.observe("2", "5h", 80.0, clock.now)
        clock.advance(100.0)
        tracker.observe("1", "5h", 20.0, clock.now)
        assert tracker.estimate("1", "5h").pct_per_s == pytest.approx(0.1)
        assert tracker.estimate("2", "5h").pct_per_s is None


class TestEstimateMath:
    def test_seconds_per_pct_is_the_inverse(self):
        assert BurnEstimate(pct_per_s=0.05).seconds_per_pct == pytest.approx(20.0)

    def test_idle_has_no_seconds_per_pct(self):
        assert BurnEstimate(pct_per_s=0.0).seconds_per_pct is None
        assert BurnEstimate().seconds_per_pct is None

    def test_recommended_threshold_reserves_the_burst(self):
        est = BurnEstimate(pct_per_s=0.05)
        expected = 100.0 - 0.05 * BURST_MULTIPLIER * BURST_WINDOW_S
        assert est.recommended_threshold() == pytest.approx(expected)

    def test_faster_burn_recommends_a_lower_threshold(self):
        slow = BurnEstimate(pct_per_s=0.05).recommended_threshold()
        fast = BurnEstimate(pct_per_s=0.5).recommended_threshold()
        assert fast < slow

    def test_idle_recommends_the_ceiling_not_one_hundred(self):
        assert BurnEstimate(pct_per_s=0.0).recommended_threshold() == pytest.approx(
            99.9
        )

    def test_recommendation_is_floored_at_the_settable_minimum(self):
        assert BurnEstimate(pct_per_s=99.0).recommended_threshold() == pytest.approx(
            50.0
        )

    def test_unknown_rate_has_no_recommendation(self):
        assert BurnEstimate().recommended_threshold() is None


class TestProjection:
    def test_counts_down_to_the_threshold(self, projects: Path):
        clock = FakeClock()
        tracker = BurnTracker(
            sensor=TranscriptBurnSensor(projects, clock=clock), clock=clock
        )
        est = BurnEstimate(pct_per_s=0.1)
        assert tracker.project(80.0, est, 90.0) == pytest.approx(100.0)

    def test_already_past_the_threshold_is_zero(self, projects: Path):
        clock = FakeClock()
        tracker = BurnTracker(
            sensor=TranscriptBurnSensor(projects, clock=clock), clock=clock
        )
        assert tracker.project(95.0, BurnEstimate(pct_per_s=0.1), 90.0) == 0.0

    def test_idle_never_arrives(self, projects: Path):
        clock = FakeClock()
        tracker = BurnTracker(
            sensor=TranscriptBurnSensor(projects, clock=clock), clock=clock
        )
        assert tracker.project(80.0, BurnEstimate(pct_per_s=0.0), 90.0) is None


class TestPerAccountCalibration:
    """Percent is a fraction of a PLAN's window, and plans differ."""

    def _tracker(self, projects: Path, clock: FakeClock):
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        return BurnTracker(sensor=sensor, clock=clock), _session(projects)

    def _bracket(self, tracker, path, clock, account, pct_from, pct_to, output):
        tracker.observe(account, "5h", pct_from, clock.now)
        clock.advance(60.0)
        _append(path, _assistant_line(
            message_id=f"{account}-{clock.now}", ts=clock.now, output=output))
        tracker.sensor.poll()
        tracker.observe(account, "5h", pct_to, clock.now)

    def test_accounts_do_not_share_a_scale(self, projects: Path):
        """A big-plan account and a small-plan one calibrate to different
        ratios; pooling would understate the burn on one of them, and
        understating is the direction that overshoots a threshold."""
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        # #1: 1 pct per 1000 weighted tokens. #2: 4 pct for the same spend.
        self._bracket(tracker, path, clock, "1", 10.0, 11.0, 200)
        self._bracket(tracker, path, clock, "2", 10.0, 14.0, 200)
        assert tracker.pct_per_token("1", "5h") == pytest.approx(0.001)
        assert tracker.pct_per_token("2", "5h") == pytest.approx(0.004)

    def test_uncalibrated_account_borrows_the_same_window_from_a_peer(
        self, projects: Path
    ):
        """Window size dominates and plan size only scales it, so another
        account's ratio for the SAME window is a far better guess than
        nothing — and it is replaced the moment this account brackets an
        interval of its own."""
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        self._bracket(tracker, path, clock, "1", 10.0, 12.0, 200)
        assert tracker.pct_per_token("2", "5h") == pytest.approx(0.002)

    def test_own_samples_win_over_the_pool(self, projects: Path):
        clock = FakeClock()
        tracker, path = self._tracker(projects, clock)
        self._bracket(tracker, path, clock, "1", 10.0, 12.0, 200)
        self._bracket(tracker, path, clock, "2", 10.0, 18.0, 200)
        assert tracker.pct_per_token("2", "5h") == pytest.approx(0.008)


class TestCalibrationPersistence:
    def test_round_trips(self, projects: Path):
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        original = BurnTracker(sensor=sensor, clock=clock)
        original._calibration["1\x005h"] = __import__("collections").deque(
            [(2.0, 1000.0), (1.0, 500.0)]
        )
        restored = BurnTracker(sensor=sensor, clock=clock)
        restored.restore_calibration(original.calibration_state())
        assert restored.pct_per_token("1", "5h") == original.pct_per_token("1", "5h")

    @pytest.mark.parametrize(
        "state",
        [
            None, "nope", {}, {"windows": "nope"}, {"windows": {"1|5h": "nope"}},
            {"windows": {"1|5h": [[1.0]]}},
            {"windows": {"1|5h": [["a", "b"]]}},
            {"windows": {"1|5h": [[1.0, 0.0]]}},   # zero tokens: undefined ratio
            {"windows": {"1|5h": [[True, True]]}},  # bools are not measurements
        ],
    )
    def test_malformed_state_leaves_it_uncalibrated(self, projects: Path, state):
        """A corrupt cache must cost the freshness of one estimate, never the
        view."""
        tracker = BurnTracker(sensor=TranscriptBurnSensor(projects, clock=FakeClock()))
        tracker.restore_calibration(state)
        assert tracker.pct_per_token() is None

    def test_restore_is_bounded(self, projects: Path):
        tracker = BurnTracker(sensor=TranscriptBurnSensor(projects, clock=FakeClock()))
        tracker.restore_calibration(
            {"windows": {"1|5h": [[1.0, 100.0]] * 500}}
        )
        assert len(tracker._calibration["1\x005h"]) <= 24


class TestTokenRateIsAlwaysAvailable:
    def test_estimate_reports_tokens_before_percent_is_knowable(self, projects: Path):
        """The token rate is known from the first tick; percent needs two API
        samples. Reporting only percent leaves the view looking hung."""
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        path = _session(projects)
        _append(path, _assistant_line(message_id="m1", ts=clock.now, output=120))
        sensor.poll()
        estimate = BurnTracker(sensor=sensor, clock=clock).estimate("1")
        assert estimate.pct_per_s is None
        assert estimate.tokens_per_s > 0


class TestIntervalBoundary:
    def test_consecutive_intervals_do_not_share_tokens(self, projects: Path):
        """Intervals are half-open: one ends at an observation, the next begins
        at it. Counting the boundary instant in both charged the same tokens
        twice and halved the second account's calibrated scale — which
        understates its burn, the direction that overshoots a threshold."""
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        path = _session(projects)
        _append(path, _assistant_line(message_id="edge", ts=clock.now, output=100))
        sensor.poll()
        boundary = clock.now
        assert sensor.tokens_since(boundary - 1) == pytest.approx(500.0)
        assert sensor.tokens_since(boundary) == 0.0, (
            "a token spent AT the boundary belongs to the interval that ended "
            "there, not to the one starting there"
        )


class TestRecommendationPrecision:
    def test_recommendation_is_rounded_to_a_settable_value(self):
        """Unrounded it renders as "99.86597411%" — ten significant digits of
        a number whose inputs are a 60-second sample and an integer
        percentage. settings.json accepts a tenth; report a tenth."""
        estimate = BurnEstimate(pct_per_s=0.00067012945)
        value = estimate.recommended_threshold()
        assert value == round(value, 1)


class TestActiveAccountTracking:
    def test_unknown_active_is_not_treated_as_a_switch(self, projects: Path):
        """A snapshot that cannot name the active account would otherwise
        clear the baselines, and clearing them on alternate ticks means no
        interval ever closes."""
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        tracker = BurnTracker(sensor=sensor, clock=clock)
        tracker.note_active("1")
        tracker.observe("1", "5h", 10.0, clock.now)
        tracker.note_active(None)
        assert tracker._observations, "an unknown active must not wipe history"

    def test_a_real_switch_drops_the_baselines(self, projects: Path):
        """An interval spanning a switch charges one account's percentage with
        another's tokens; one lost sample beats a permanently skewed ratio."""
        clock = FakeClock()
        sensor = TranscriptBurnSensor(projects, clock=clock)
        sensor.poll()
        tracker = BurnTracker(sensor=sensor, clock=clock)
        tracker.note_active("1")
        tracker.observe("1", "5h", 10.0, clock.now)
        tracker.note_active("2")
        assert not tracker._observations
