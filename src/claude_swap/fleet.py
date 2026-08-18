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

    @property
    def deadline_text(self) -> str:
        """``8/19 14:00`` — the calendar instant the quota expires."""
        if self.reset_ts is None:
            return "—"
        return datetime.fromtimestamp(self.reset_ts).strftime("%-m/%-d %H:%M")

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
        blocked=reachable is not None and reachable <= 0,
        unknown=binding is None,
    )


def order_segments(segments: Sequence[FleetSegment]) -> list[FleetSegment]:
    """Soonest deadline leftmost — the order the quota should be spent in.

    Unknown deadlines sort last: an account nobody can schedule around is not
    urgent, and putting it first would push a real deadline off the eye's
    starting point. Ties break on account number so the bar does not reshuffle
    between repaints when two windows share a reset instant.
    """
    return sorted(
        segments,
        key=lambda s: (
            s.reset_ts if s.reset_ts is not None else float("inf"),
            int(s.number),
        ),
    )


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
