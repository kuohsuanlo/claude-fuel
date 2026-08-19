"""The fleet as ONE quantity: how much perishable quota exists, and when it dies.

Every account reports three or more utilization windows, and reading a fleet
means holding a dozen percentages in your head and doing deadline arithmetic
on all of them. This module collapses that to the only question worth asking —
*what am I about to lose, and when* — and lays it out as a single bar whose
segments are accounts, ordered by deadline, sized by what each still holds.

The collapse is opinionated and identical to the one the waste-first strategy
ranks on (both call ``autoswitch.weekly_binding``), so the picture can never
disagree with the decision: a segment that looks urgent IS the segment the
engine would move to.

Only the WEEKLY windows appear. Quota left in a 5-hour window is not lost when
it resets — it is back before the day is out — so drawing it as "at risk"
would be a lie. The 5-hour window still decides whether an account is REACHABLE
right now, which is a different fact and is carried separately as
``blocked``.

Pure geometry and arithmetic: no Textual, no I/O. The screen renders what this
returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence

from claude_swap import oauth
from claude_swap.autoswitch import waste_risk, weekly_binding
from claude_swap.poll_policy import parse_reset_ts

# Risk bands, in percentage points per hour, for coloring a segment. A band is
# a statement about whether the quota can realistically be spent before it
# expires: at 2 %/h a machine burning steadily will just about clear it, while
# 5 %/h needs sustained heavy use to not waste some. Tuned to the same scale
# `waste_risk` reports, so the legend and the engine speak one language.
RISK_URGENT_PCT_PER_H = 2.0
RISK_WATCH_PCT_PER_H = 0.8


@dataclass(frozen=True)
class FleetSegment:
    """One account's stake in the fleet's perishable quota."""

    number: str
    label: str  # alias, else the local part of the email
    email: str
    headroom_pct: float  # weekly points still unspent — the width driver
    reset_ts: float | None  # when those points vanish
    risk: float | None  # points per hour that must be spent to save them
    is_active: bool
    blocked: bool  # holds quota, but its short-term window is spent
    unknown: bool  # no readable weekly window at all
    # WHICH LIMIT the numbers above describe ("7d", "Fable"). Carried so any
    # message can say "7d 1%" instead of inventing a unit: a bare "1 pt" hid
    # that it was one percent OF THE 7d POOL, which is not the same quantity
    # as a percent of any other window — the exact mixing this screen keeps
    # having to unlearn.
    window: str = ""

    @property
    def deadline_text(self) -> str:
        """``8/19 Wed 14:00`` — the calendar instant the quota expires.

        The weekday earns its three characters: a deadline is only actionable
        once you know whether it lands before or after the weekend.
        """
        if self.reset_ts is None:
            return "—"
        return datetime.fromtimestamp(self.reset_ts).strftime("%-m/%-d %a %H:%M")

    def countdown_text(self, now: float) -> str:
        """``20h`` / ``6d`` — how long is left, at the coarsest useful unit."""
        if self.reset_ts is None:
            return ""
        remaining = self.reset_ts - now
        if remaining <= 0:
            return "now"
        if remaining < 3600:
            return f"{int(remaining // 60)}m"
        if remaining < 86400:
            return f"{int(remaining // 3600)}h"
        return f"{int(remaining // 86400)}d"

    def tier(self) -> str:
        """``"urgent" | "watch" | "calm" | "unknown"`` — the color band."""
        if self.risk is None:
            return "unknown"
        if self.risk >= RISK_URGENT_PCT_PER_H:
            return "urgent"
        if self.risk >= RISK_WATCH_PCT_PER_H:
            return "watch"
        return "calm"


def segment_for(
    *,
    number: str,
    email: str,
    alias: str,
    usage: dict | None,
    models: Sequence[str],
    now: float,
    is_active: bool,
) -> FleetSegment:
    """Collapse one account's windows into its stake in the fleet."""
    binding = weekly_binding(usage, models)
    headroom = 0.0 if binding is None else max(0.0, 100.0 - binding[1])
    reset_ts = None if binding is None else parse_reset_ts(binding[2])
    # An account whose BINDING window (any window, weekly or not) is spent
    # still owns its weekly points — it simply cannot reach them until the
    # short-term window rolls over. Drawn, but hatched: the difference between
    # "you have nothing" and "you have something you can't touch yet" is the
    # difference between adding an account and waiting three hours.
    reachable = oauth.account_headroom(usage, models)
    return FleetSegment(
        number=number,
        label=alias or email.split("@", 1)[0],
        email=email,
        headroom_pct=headroom,
        reset_ts=reset_ts,
        risk=waste_risk(usage, models, now),
        is_active=is_active,
        window="" if binding is None else binding[0],
        blocked=reachable is not None and reachable <= 0,
        unknown=binding is None,
    )


def window_segment(
    *,
    number: str,
    email: str,
    alias: str,
    usage: dict | None,
    label: str,
    models: Sequence[str],
    now: float,
    is_active: bool,
) -> FleetSegment | None:
    """One account's stake in ONE named window (``5h`` / ``7d`` / a model name).

    ``None`` when the account does not report that window at all, so a fleet
    where one account lacks a per-model limit draws a shorter bar rather than
    a phantom empty segment.

    ``blocked`` here means *this window* is spent, which is narrower than the
    account-wide sense used by :func:`segment_for`: on the session bar an
    account with no 5-hour headroom is genuinely unusable right now, while on
    the weekly bar the same account may still have a week's quota intact.
    """
    for name, pct, resets_at in oauth.relevant_windows(usage, models):
        if name != label:
            continue
        return FleetSegment(
            number=number,
            label=alias or email.split("@", 1)[0],
            email=email,
            headroom_pct=max(0.0, 100.0 - pct),
            reset_ts=parse_reset_ts(resets_at),
            risk=waste_risk(usage, models, now),
            is_active=is_active,
            window=label,
            blocked=pct >= 100.0,
            unknown=False,
        )
    return None


def order_segments(
    segments: Sequence[FleetSegment], *, spend_first_last: bool = False
) -> list[FleetSegment]:
    """Consumption order: soonest deadline first.

    Unknown deadlines sort last either way: an account nobody can schedule
    around is not urgent, and letting it lead would push a real deadline off
    the eye's starting point. Ties break on account number so the bar does not
    reshuffle between repaints when two windows share a reset instant.

    ``spend_first_last`` reverses the sequence for the BAR, which is drawn as a
    fuel gauge: a gauge is consumed from its right edge, so the segment being
    spent now belongs there and the bar shortens leftward as quota is used.
    Kept as a display flag on one function rather than two orderings, so the
    list and the gauge can never disagree about which account is next.
    """
    ordered = sorted(
        segments,
        key=lambda s: (
            s.reset_ts if s.reset_ts is not None else float("inf"),
            int(s.number),
        ),
    )
    return list(reversed(ordered)) if spend_first_last else ordered


def burn_head(segments: Sequence[FleetSegment]) -> FleetSegment | None:
    """The segment quota should be coming out of right now.

    The most urgent account that is actually REACHABLE — urgency alone would
    point at an account whose short-term window is spent, which is advice
    nobody can act on. Returns ``None`` when every account holding perishable
    quota is blocked.

    Deliberately distinct from "the active account": when the two differ, the
    gauge is showing that quota is being drawn from the wrong place, which is
    precisely the condition the waste-first strategy exists to correct.
    """
    for segment in order_segments(segments):
        if segment.headroom_pct > 0 and not segment.blocked:
            return segment
    return None


def segment_widths(segments: Sequence[FleetSegment], width: int) -> list[int]:
    """Cell widths proportional to each segment's quota, summing to ``width``.

    Two properties the naive ``round(share * width)`` does not have:

    A SEGMENT WITH QUOTA IS NEVER INVISIBLE. An account holding 2 points next
    to one holding 90 rounds to zero cells and vanishes — and a vanished
    segment reads as "that account has nothing", which is exactly wrong when
    those 2 points expire tonight. Anything above zero gets at least one cell.

    THE WIDTHS SUM EXACTLY. Rounding each share independently leaves the bar a
    cell or two short or long, which shows up as a ragged right edge that
    twitches between repaints. Remainders are distributed largest-first, so the
    total is exact and the assignment is stable for stable inputs.
    """
    live = [s for s in segments if s.headroom_pct > 0]
    if not live or width <= 0:
        return [0] * len(segments)
    if len(live) > width:
        # More accounts than cells: proportionality is meaningless, so give
        # every account with quota one cell and drop the rest rather than
        # rendering a bar that silently omits accounts by rounding.
        live = live[:width]
    total = sum(s.headroom_pct for s in live)
    chosen = {id(s) for s in live}
    floor_widths: dict[int, int] = {}
    remainders: list[tuple[float, int, int]] = []
    for index, segment in enumerate(segments):
        if id(segment) not in chosen:
            floor_widths[index] = 0
            continue
        exact = segment.headroom_pct / total * width
        base = max(1, int(exact))
        floor_widths[index] = base
        remainders.append((exact - int(exact), -index, index))
    # Reclaim or hand out cells until the row is exactly `width` wide. Taking
    # from the smallest remainder first (and never below one cell) keeps the
    # correction on the segments least entitled to the extra cell.
    spare = width - sum(floor_widths.values())
    remainders.sort(reverse=True)
    order = [index for _, _, index in remainders]
    position = 0
    while spare > 0 and order:
        floor_widths[order[position % len(order)]] += 1
        position += 1
        spare -= 1
    position = 0
    while spare < 0 and order:
        index = order[-1 - (position % len(order))]
        if floor_widths[index] > 1:
            floor_widths[index] -= 1
            spare += 1
        position += 1
        if position > 4 * len(order):
            break  # every segment is down to its floor; nothing left to give
    return [floor_widths[i] for i in range(len(segments))]


def remaining_tank_pct(
    segments: Sequence[FleetSegment], weights: Sequence[float] | None = None
) -> float:
    """The fleet's unspent quota for ONE window, in units of one account's
    window — so three untouched accounts read 300%, not 100%.

    Deliberately NOT normalised to the fleet. "The tank is 45% full" hides
    whether that is half of one account or a tenth of six, and the number is
    here to say how much work is left, not how tidy the fleet looks. Passing
    100% is the normal case for a healthy fleet, not an error.

    Weighted by plan size, using the SAME weights the gauge is drawn with, so
    the printed number and the length of the coloured run can never state
    different amounts — the failure mode this screen has already hit twice.
    With equal weights it collapses to the plain sum of each account's
    remaining points, which is the unit the headline already counts in.
    """
    if not segments:
        return 0.0
    if not weights or len(weights) != len(segments):
        weights = [1.0] * len(segments)
    total = sum(weights)
    if total <= 0:
        return 0.0
    weighted = sum(seg.headroom_pct * w for seg, w in zip(segments, weights))
    return len(segments) * weighted / total


def handover_eta_h(
    active: FleetSegment,
    candidate: FleetSegment,
    now: float,
    *,
    ratio: float,
    floor: float,
) -> float | None:
    """Hours until ``candidate`` overtakes ``active`` on the waste-risk axis.

    ``None`` when it never does before one of them resets.

    The risk axis carries the deadline in its DENOMINATOR, so a candidate that
    loses today's comparison does not lose it forever: its urgency climbs on
    its own as its reset approaches, until it clears the hysteresis gate. That
    is the answer to "then when does the other account get its turn", which is
    a question the screen could not answer while it could only say that
    nothing was more urgent right now.

    Solved with both headrooms HELD CONSTANT, which makes the result a LATE
    bound rather than a prediction: spending the active account lowers its
    risk and brings the handover forward, never back. Callers should say "by",
    not "at".

    ``ratio`` and ``floor`` are the engine's own gate (a candidate must beat
    ``ratio × active`` and clear ``floor``), passed in rather than imported so
    this stays arithmetic and the engine keeps owning the policy.
    """
    if active.reset_ts is None or candidate.reset_ts is None:
        return None
    if candidate.headroom_pct <= 0:
        return None
    t_active = (active.reset_ts - now) / 3600.0
    t_cand = (candidate.reset_ts - now) / 3600.0
    if t_active <= 0 or t_cand <= 0:
        return None
    h_active, h_cand = active.headroom_pct, candidate.headroom_pct
    # Past whichever window closes first the comparison is between quantities
    # that no longer exist, so that is the horizon for an answer.
    horizon = min(t_cand, t_active)

    def risk(headroom: float, remaining: float, elapsed: float) -> float:
        left = remaining - elapsed
        return float("inf") if left <= 0 else headroom / left

    # THE RATIO TEST IS MONOTONE, but which WAY depends on who resets first:
    # risk_c/risk_a is (h_c/h_a)·(t_a-t)/(t_c-t), and that fraction climbs
    # only while the candidate's window closes first. A candidate resetting
    # LATER than the active account can only fall further behind, so waiting
    # never helps it.
    beats_ratio_now = h_cand / t_cand > ratio * (h_active / t_active)
    if t_cand >= t_active:
        if not beats_ratio_now:
            return None
        t_ratio = 0.0
    elif beats_ratio_now:
        t_ratio = 0.0
    else:
        denominator = ratio * h_active - h_cand
        if denominator <= 0:
            return None
        t_ratio = (ratio * h_active * t_cand - h_cand * t_active) / denominator
        if not 0.0 <= t_ratio < horizon:
            return None
    # The floor is an absolute rate and clears at an instant of its own. It is
    # a SEPARATE gate, not a tie-break: a candidate can be well past the ratio
    # and still be held back because a sliver of quota is not worth a switch.
    # Folding the two together read "never" for exactly that case.
    t_floor = 0.0 if floor <= 0 else max(0.0, t_cand - h_cand / floor)
    when = max(t_ratio, t_floor, 0.0)
    if when >= horizon:
        return None
    probe = min(when + 1e-6, (when + horizon) / 2.0)
    if risk(h_cand, t_cand, probe) <= max(
        ratio * risk(h_active, t_active, probe), floor
    ):
        return None
    return when


def runway_s(
    segments: Sequence[FleetSegment],
    rate_of,
) -> float | None:
    """Seconds this window's fleet-wide fuel lasts at the current burn.

    TIME IS THE UNIT THAT NEEDS NO CONVERSION. Percent answers "how much of a
    pool", and the pools differ per window and per plan; tokens answer "how
    much work", which nobody budgets in. "How long can I keep working" is the
    question actually being asked, and it is comparable across every row.

    The work belongs to the MACHINE, not to an account — switching accounts
    does not change what is running. So the fleet's runway is the time to burn
    each account's share in turn: sum of ``headroom_i / rate_i``, where
    ``rate_i`` is what this machine's current traffic costs per second in
    account *i*'s own window (each plan prices the same tokens differently).

    ``rate_of(number)`` returns that account's %/s for this window, or None.
    Accounts with no measured rate borrow the MEDIAN of the measured ones —
    the same equal-plan default the gauge widths already use; a fleet where
    nothing is measured returns None rather than a guess. Blocked segments
    still count: their quota is spendable within the window's own life, just
    not this instant.
    """
    rates: dict[str, float] = {}
    for segment in segments:
        rate = rate_of(segment.number)
        if rate is not None and rate > 0:
            rates[segment.number] = rate
    if not rates:
        return None
    known = sorted(rates.values())
    default = known[len(known) // 2]
    total = 0.0
    for segment in segments:
        if segment.headroom_pct <= 0:
            continue
        total += segment.headroom_pct / rates.get(segment.number, default)
    return total


def total_at_risk(segments: Sequence[FleetSegment], now: float, horizon_s: float) -> float:
    """Weekly points across the fleet that expire within ``horizon_s``.

    The headline number: "you are about to lose N points". Counts only quota
    with a KNOWN deadline inside the horizon — an unknown reset is not
    evidence of an imminent loss, and inflating this figure with maybes would
    make the one number on the screen that should provoke action untrustworthy.
    """
    return sum(
        s.headroom_pct
        for s in segments
        if s.reset_ts is not None and 0 < s.reset_ts - now <= horizon_s
    )
