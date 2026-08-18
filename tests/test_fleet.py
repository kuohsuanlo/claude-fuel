"""Tests for the fleet collapse and bar geometry (fleet.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap.fleet import (
    FleetSegment,
    order_segments,
    segment_for,
    segment_widths,
    total_at_risk,
)

NOW = 1_700_000_000.0


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _usage(pct5: float, pct7: float, hours: float | None = None) -> dict:
    seven: dict = {"pct": pct7}
    if hours is not None:
        seven["resets_at"] = _iso(NOW + hours * 3600.0)
    return {"five_hour": {"pct": pct5}, "seven_day": seven}


def _seg(
    number: str = "1",
    headroom: float = 50.0,
    hours: float | None = 24.0,
    risk: float | None = 1.0,
    active: bool = False,
) -> FleetSegment:
    return FleetSegment(
        number=number,
        label=f"acct{number}",
        email=f"a{number}@example.com",
        headroom_pct=headroom,
        reset_ts=None if hours is None else NOW + hours * 3600.0,
        risk=risk,
        is_active=active,
        blocked=False,
        unknown=False,
    )


class TestSegmentFor:
    def test_width_comes_from_the_weekly_window_not_the_five_hour_one(self):
        """A spent 5h window is not lost quota — it recycles. Sizing on it
        would show an account as empty hours before it refills."""
        seg = segment_for(
            number="1", email="a@x.com", alias="", usage=_usage(95, 40, 24),
            models=(), now=NOW, is_active=False,
        )
        assert seg.headroom_pct == pytest.approx(60.0)

    def test_spent_short_window_marks_blocked_not_empty(self):
        seg = segment_for(
            number="1", email="a@x.com", alias="", usage=_usage(100, 40, 24),
            models=(), now=NOW, is_active=False,
        )
        assert seg.blocked is True
        assert seg.headroom_pct == pytest.approx(60.0), (
            "blocked is about reachability; the quota is still there"
        )

    def test_alias_wins_over_the_email_local_part(self):
        seg = segment_for(
            number="1", email="dev@x.com", alias="work", usage=_usage(10, 10, 24),
            models=(), now=NOW, is_active=False,
        )
        assert seg.label == "work"
        seg = segment_for(
            number="1", email="dev@x.com", alias="", usage=_usage(10, 10, 24),
            models=(), now=NOW, is_active=False,
        )
        assert seg.label == "dev"

    def test_unreadable_usage_is_flagged_not_zeroed(self):
        seg = segment_for(
            number="1", email="a@x.com", alias="", usage=None,
            models=(), now=NOW, is_active=False,
        )
        assert seg.unknown is True
        assert seg.risk is None

    def test_risk_matches_the_strategy_axis(self):
        """60 points expiring in 20 hours is 3 %/h — the same number the engine
        ranks on, because both call weekly_binding."""
        seg = segment_for(
            number="1", email="a@x.com", alias="", usage=_usage(10, 40, 20),
            models=(), now=NOW, is_active=False,
        )
        assert seg.risk == pytest.approx(3.0)
        assert seg.tier() == "urgent"

    def test_tiers_span_the_bands(self):
        assert _seg(risk=5.0).tier() == "urgent"
        assert _seg(risk=1.0).tier() == "watch"
        assert _seg(risk=0.1).tier() == "calm"
        assert _seg(risk=None).tier() == "unknown"


class TestDeadlineText:
    def test_countdown_uses_the_coarsest_useful_unit(self):
        assert _seg(hours=0.5).countdown_text(NOW) == "30m"
        assert _seg(hours=20).countdown_text(NOW) == "20h"
        assert _seg(hours=150).countdown_text(NOW) == "6d"

    def test_elapsed_deadline_reads_now(self):
        assert _seg(hours=-1).countdown_text(NOW) == "now"

    def test_no_deadline_has_no_countdown(self):
        assert _seg(hours=None).countdown_text(NOW) == ""
        assert _seg(hours=None).deadline_text == "—"


class TestOrdering:
    def test_soonest_deadline_is_leftmost(self):
        ordered = order_segments([_seg("1", hours=150), _seg("2", hours=20),
                                  _seg("3", hours=100)])
        assert [s.number for s in ordered] == ["2", "3", "1"]

    def test_unknown_deadlines_sort_last(self):
        ordered = order_segments([_seg("1", hours=None), _seg("2", hours=100)])
        assert [s.number for s in ordered] == ["2", "1"]

    def test_equal_deadlines_do_not_reshuffle_between_repaints(self):
        segs = [_seg("3", hours=20), _seg("1", hours=20), _seg("2", hours=20)]
        assert [s.number for s in order_segments(segs)] == ["1", "2", "3"]
        assert [s.number for s in order_segments(list(reversed(segs)))] == [
            "1", "2", "3",
        ]


class TestSegmentWidths:
    def test_widths_are_proportional(self):
        widths = segment_widths([_seg("1", headroom=75), _seg("2", headroom=25)], 100)
        assert widths == [75, 25]

    def test_widths_always_sum_to_the_requested_total(self):
        for total in (10, 37, 60, 81, 100):
            segs = [_seg("1", headroom=33.3), _seg("2", headroom=33.3),
                    _seg("3", headroom=33.4)]
            assert sum(segment_widths(segs, total)) == total

    def test_a_tiny_stake_never_vanishes(self):
        """2 points beside 90 rounds to zero cells, and a missing segment reads
        as 'that account has nothing' — the opposite of the truth when those 2
        points expire tonight."""
        widths = segment_widths([_seg("1", headroom=2), _seg("2", headroom=90)], 40)
        assert widths[0] >= 1
        assert sum(widths) == 40

    def test_empty_accounts_get_no_cells(self):
        widths = segment_widths([_seg("1", headroom=0), _seg("2", headroom=50)], 20)
        assert widths == [0, 20]

    def test_all_empty_draws_nothing(self):
        assert segment_widths([_seg("1", headroom=0), _seg("2", headroom=0)], 20) == [
            0, 0,
        ]

    def test_more_accounts_than_cells_is_bounded(self):
        segs = [_seg(str(i), headroom=10) for i in range(1, 13)]
        widths = segment_widths(segs, 5)
        assert sum(widths) == 5
        assert all(w >= 0 for w in widths)

    def test_zero_width_is_survivable(self):
        assert segment_widths([_seg("1"), _seg("2")], 0) == [0, 0]

    def test_stable_for_stable_input(self):
        segs = [_seg("1", headroom=41.7), _seg("2", headroom=8.3),
                _seg("3", headroom=50.0)]
        assert segment_widths(segs, 63) == segment_widths(segs, 63)


class TestTotalAtRisk:
    def test_counts_only_quota_inside_the_horizon(self):
        segs = [_seg("1", headroom=49, hours=20), _seg("2", headroom=72, hours=150)]
        assert total_at_risk(segs, NOW, 24 * 3600.0) == pytest.approx(49.0)

    def test_unknown_deadlines_are_not_counted_as_imminent(self):
        segs = [_seg("1", headroom=80, hours=None)]
        assert total_at_risk(segs, NOW, 24 * 3600.0) == 0.0

    def test_elapsed_deadlines_are_not_counted(self):
        segs = [_seg("1", headroom=80, hours=-2)]
        assert total_at_risk(segs, NOW, 24 * 3600.0) == 0.0
