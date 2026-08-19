"""Tests for the fleet collapse and bar geometry (fleet.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap.fleet import (
    FleetSegment,
    burn_head,
    handover_eta_h,
    order_segments,
    remaining_tank_pct,
    runway_s,
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


class TestGaugeDirection:
    """The bar is a fuel gauge, and a gauge is consumed from its right edge."""

    def test_spend_first_sits_at_the_right(self):
        ordered = order_segments(
            [_seg("1", hours=150), _seg("2", hours=20), _seg("3", hours=100)],
            spend_first_last=True,
        )
        assert [s.number for s in ordered] == ["1", "3", "2"]

    def test_it_is_exactly_the_reverse_of_the_list_order(self):
        """One ordering function with a display flag, so the account list and
        the gauge can never disagree about which account is next."""
        segs = [_seg("1", hours=150), _seg("2", hours=20), _seg("3", hours=100)]
        assert order_segments(segs, spend_first_last=True) == list(
            reversed(order_segments(segs))
        )

    def test_unknown_deadlines_stay_at_the_far_end(self):
        """Reversing must not promote an unschedulable account to the position
        the eye reads as 'next to be spent'."""
        ordered = order_segments(
            [_seg("1", hours=None), _seg("2", hours=20), _seg("3", hours=100)],
            spend_first_last=True,
        )
        assert ordered[-1].number == "2"
        assert ordered[0].number == "1"


class TestBurnHead:
    def test_points_at_the_most_urgent_reachable_account(self):
        blocked = FleetSegment(
            number="3", label="c", email="c@x", headroom_pct=47.0,
            reset_ts=NOW + 20 * 3600, risk=2.4, is_active=False, blocked=True,
            unknown=False,
        )
        head = burn_head([blocked, _seg("2", hours=100), _seg("1", hours=150)])
        assert head is not None and head.number == "2", (
            "urgency alone points at an account nobody can draw from"
        )

    def test_none_when_everything_is_blocked(self):
        blocked = FleetSegment(
            number="1", label="a", email="a@x", headroom_pct=10.0,
            reset_ts=NOW + 3600, risk=1.0, is_active=False, blocked=True,
            unknown=False,
        )
        assert burn_head([blocked]) is None

    def test_skips_accounts_with_nothing_left(self):
        assert burn_head([_seg("1", headroom=0, hours=1), _seg("2", hours=99)]).number == "2"

    def test_may_differ_from_the_active_account(self):
        """When it does, the gauge is showing that quota is being drawn from
        the wrong place — the exact condition waste-first exists to fix."""
        active_calm = _seg("1", hours=150, active=True)
        urgent = _seg("2", hours=5)
        assert burn_head([active_calm, urgent]).number == "2"


class TestRemainingTankPct:
    """How much fuel is in the tank, in units of ONE account's window."""

    def test_three_untouched_accounts_read_three_hundred(self):
        """Over 100% is the point, not a bug: a fleet holds more than one
        account's worth of a window, and normalising that away hides whether
        45% is half of one account or a tenth of six."""
        segs = [_seg("1", 100.0), _seg("2", 100.0), _seg("3", 100.0)]
        assert remaining_tank_pct(segs) == pytest.approx(300.0)

    def test_equal_weights_collapse_to_the_plain_points_sum(self):
        """The unit the headline already counts in, so the two agree."""
        segs = [_seg("1", 36.0), _seg("2", 7.0), _seg("3", 4.0)]
        assert remaining_tank_pct(segs) == pytest.approx(47.0)
        assert remaining_tank_pct(segs, [1.0, 1.0, 1.0]) == pytest.approx(47.0)

    def test_plan_size_is_honoured(self):
        """A 20x account's remaining 50% is four times a 5x account's, and the
        gauge already draws it that way — the number has to agree or the two
        state different amounts of the same thing."""
        segs = [_seg("1", 100.0), _seg("2", 0.0)]
        assert remaining_tank_pct(segs, [20.0, 5.0]) == pytest.approx(160.0)
        assert remaining_tank_pct(segs, [5.0, 20.0]) == pytest.approx(40.0)

    def test_an_exhausted_fleet_is_zero_not_missing(self):
        assert remaining_tank_pct([_seg("1", 0.0), _seg("2", 0.0)]) == 0.0

    def test_no_segments_is_zero_rather_than_a_crash(self):
        assert remaining_tank_pct([]) == 0.0

    def test_mismatched_or_degenerate_weights_fall_back_to_equal(self):
        """Defensive: the weights come from a different function, and a bar
        that renders is worth more than a stack trace about their length."""
        segs = [_seg("1", 40.0), _seg("2", 60.0)]
        assert remaining_tank_pct(segs, [1.0]) == pytest.approx(100.0)
        assert remaining_tank_pct(segs, [0.0, 0.0]) == 0.0


class TestHandoverEta:
    """When the account that lost today's comparison gets its turn."""

    GATE = {"ratio": 1.25, "floor": 0.1}

    def test_the_candidate_climbs_until_it_clears_the_gate(self):
        """A live case: 23 points over 141h beats 7 points over 105h now, and
        the question the screen could not answer was when that stops being
        true. It is 11.6h before the candidate's own reset — with the 7 points
        needing 40 minutes at the measured rate, so nothing is lost by
        waiting."""
        active = _seg("1", 23.0, hours=141.0, active=True)
        candidate = _seg("2", 7.0, hours=105.0)
        eta = handover_eta_h(active, candidate, NOW, **self.GATE)
        assert eta == pytest.approx(93.41, abs=0.05)
        assert 105.0 - eta == pytest.approx(11.59, abs=0.05)

    def test_a_candidate_already_past_the_gate_reads_zero(self):
        """Not None: "now" and "never" must not collapse to one answer, or the
        caller cannot tell a switch in progress from one that never comes."""
        active = _seg("1", 5.0, hours=100.0, active=True)
        candidate = _seg("2", 40.0, hours=10.0)
        assert handover_eta_h(active, candidate, NOW, **self.GATE) == 0.0

    def test_never_when_the_active_window_closes_first(self):
        """The active account's own risk runs away as its reset nears, so a
        distant candidate never catches it inside the window they share."""
        active = _seg("1", 50.0, hours=10.0, active=True)
        candidate = _seg("2", 5.0, hours=100.0)
        assert handover_eta_h(active, candidate, NOW, **self.GATE) is None

    def test_the_floor_delays_a_candidate_with_almost_nothing_left(self):
        """Beating the ratio is not enough — an account with a sliver of quota
        must also be losing it fast enough in absolute terms, or every fleet
        would thrash over rounding."""
        # The candidate is ALREADY past the ratio (0.02 %/h against an active
        # account's 0.01), so only the floor is holding it. Folding the two
        # gates into one closed form reported "never" for this exact shape.
        active = _seg("1", 2.0, hours=200.0, active=True)
        candidate = _seg("2", 1.0, hours=50.0)
        eta = handover_eta_h(active, candidate, NOW, **self.GATE)
        assert eta is not None
        # 1 point clears 0.1 %/h only inside the last 10 hours of its window.
        assert 50.0 - eta == pytest.approx(10.0, abs=0.05)

    def test_the_ratio_can_bind_later_than_the_floor(self):
        """The other order: plenty of absolute urgency, but a rival holding
        far more perishable quota. Whichever gate opens last decides."""
        active = _seg("1", 60.0, hours=200.0, active=True)
        candidate = _seg("2", 1.0, hours=50.0)
        eta = handover_eta_h(active, candidate, NOW, **self.GATE)
        assert 50.0 - eta == pytest.approx(2.03, abs=0.05)

    def test_an_exhausted_candidate_never_takes_over(self):
        active = _seg("1", 20.0, hours=100.0, active=True)
        assert handover_eta_h(
            active, _seg("2", 0.0, hours=5.0), NOW, **self.GATE
        ) is None

    def test_unknown_resets_are_not_schedulable(self):
        active = _seg("1", 20.0, hours=100.0, active=True)
        assert handover_eta_h(
            active, _seg("2", 30.0, hours=None), NOW, **self.GATE
        ) is None
        assert handover_eta_h(
            _seg("1", 20.0, hours=None, active=True),
            _seg("2", 30.0, hours=5.0), NOW, **self.GATE
        ) is None


class TestRunway:
    """How long a window's fleet-wide fuel lasts at the current burn."""

    def test_runway_is_the_sum_of_each_accounts_turn(self):
        """The work belongs to the machine: switching accounts does not change
        what is running, so the fleet's time is spent burning each share in
        turn at that account's own rate."""
        segs = [_seg("1", 50.0), _seg("2", 30.0)]
        rates = {"1": 0.010, "2": 0.010}
        assert runway_s(segs, rates.get) == pytest.approx((50 + 30) / 0.010)

    def test_each_share_is_priced_in_its_own_plan(self):
        """A 20x plan's percent is a different amount of work from a 5x
        plan's, and the same machine traffic therefore drains them at
        different %/s. The runway must respect each account's own rate."""
        segs = [_seg("1", 50.0), _seg("2", 50.0)]
        rates = {"1": 0.010, "2": 0.002}  # account 2's window is 5x larger
        assert runway_s(segs, rates.get) == pytest.approx(
            50 / 0.010 + 50 / 0.002
        )

    def test_an_unmeasured_account_borrows_the_median_rate(self):
        """The equal-plan default the gauge widths already use — a fleet is
        not blinded by its newest account."""
        segs = [_seg("1", 10.0), _seg("2", 10.0), _seg("3", 10.0)]
        rates = {"1": 0.010, "3": 0.030}
        # median of {0.010, 0.030} with two entries is the upper one here
        expected = 10 / 0.010 + 10 / 0.030 + 10 / 0.030
        assert runway_s(segs, rates.get) == pytest.approx(expected)

    def test_a_wholly_unmeasured_fleet_says_nothing(self):
        """None, not a guess: with no rate at all there is no burn to
        project, and a fabricated horizon reads as a measurement."""
        assert runway_s([_seg("1", 50.0)], lambda n: None) is None

    def test_idle_is_not_a_rate(self):
        assert runway_s([_seg("1", 50.0)], lambda n: 0.0) is None

    def test_exhausted_shares_add_nothing(self):
        segs = [_seg("1", 0.0), _seg("2", 40.0)]
        assert runway_s(segs, {"2": 0.010}.get) == pytest.approx(40 / 0.010)
