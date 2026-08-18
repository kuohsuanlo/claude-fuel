"""``cfuel``: the whole fleet as one bar, with a live burn readout.

ONE SCREEN, ONE COMMAND. Everything needed to answer "am I about to waste
quota, and am I about to hit a wall?" is here: the deadline bar (every
account's perishable quota, soonest expiry leftmost), the burn rate measured
at second resolution, the threshold that rate implies, and a single switch
that decides whether the engine acts on any of it.

TWO INDEPENDENT LOOPS, deliberately. The display ticks every second off local
transcripts, which cost no quota; the engine ticks on its own budgeted
schedule against the usage API. Arming or disarming the engine changes only
whether it may *switch* — the numbers keep moving either way, so the screen is
just as useful as a passive gauge as it is as an autopilot.

The threshold edited here is PERSISTED. An auto-switch threshold is a policy,
not a view setting: a value that silently reverted when the screen closed
would be a trap, since the engine keeps running from settings.json afterwards.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap import fleet, oauth
from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent, pct_label
from claude_swap.burn import BurnTracker, TranscriptBurnSensor
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, load_settings, save_settings
from claude_swap.tui.autoview import event_text
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

# The display tick. One second is chosen because the instrument behind it is
# free: transcript tailing reads only bytes appended since the last pass, so
# the cost is a handful of stats and no network at all.
DISPLAY_INTERVAL_S = 1.0

# Quota expiring inside this window is what the headline "about to lose"
# figure counts. A day is the horizon a person can actually act on — anything
# further out can be rescued by tomorrow's session.
AT_RISK_HORIZON_S = 24 * 3600.0

_FILLED = "█"
_HATCHED = "▓"  # holds quota, but its short-term window is spent
_EMPTY = "─"
# Segment joins are drawn, not merely colored. Color alone carries the whole
# structure only for a reader with a color terminal and normal color vision;
# a drawn boundary keeps the bar legible in a pipe, a screenshot, or a
# monochrome scrollback, and it is what makes the shape read as "these are
# separate accounts" rather than one gradient.
_JOIN = "│"


class FleetScreen(Screen):
    """The single-screen fleet view."""

    BINDINGS = [
        Binding("a", "toggle_armed", "Arm / disarm"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("r", "apply_recommended", "Use suggested"),
        Binding("f", "app.refresh_full", "Refresh usage"),
        Binding("q", "app.quit", "Quit"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        self._armed = False
        self._adjusting = False
        self._sensor: TranscriptBurnSensor | None = None
        self._tracker: BurnTracker | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="fleet-top"):
            yield Static("", id="fleet-headline")
            yield Static("", id="fleet-bar")
            yield Static("", id="fleet-legend")
            yield Static("", id="fleet-burn")
            yield Static("", id="fleet-accounts")
        yield RichLog(id="fleet-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        # The engine is the only thing allowed to hit the usage API while this
        # screen is up; the app's own poller drops to store-only so the two
        # cannot race for the same rate-limited budget.
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        self.app.threshold_pct = self._settings.threshold
        self._sensor = TranscriptBurnSensor()
        self._tracker = BurnTracker(sensor=self._sensor)
        self._load_calibration()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        self._start_engine()
        self.set_interval(DISPLAY_INTERVAL_S, self._display_tick)
        self._display_tick()

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        self.app.switcher.clear_poll_policy_inputs()
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._display_tick()

    def _on_snapshot(self, snapshot: AccountsSnapshot | None) -> None:
        """Feed every fresh API reading to the calibrator, then repaint.

        The tracker is idempotent per ``fetched_at``, so re-reading the same
        stored row on each of these (they arrive far more often than fetches
        do) neither double-counts nor disturbs the calibration.
        """
        if snapshot is None or self._tracker is None:
            return
        models = tuple(self._models())
        before = self._tracker.pct_per_token()
        for account in snapshot.accounts:
            entry = account.usage
            if entry.last_good is None or entry.fetched_at is None:
                continue
            headroom = oauth.account_headroom(entry.last_good, models)
            if headroom is None:
                continue
            self._tracker.observe(account.number, 100.0 - headroom, entry.fetched_at)
        if self._tracker.pct_per_token() != before:
            # Only on a genuine change: observations arrive several times a
            # second from repaints, and rewriting the cache on each would be a
            # disk write per tick for a value that changes once per API fetch.
            self._save_calibration()
        self._display_tick()

    def _calibration_path(self):
        return self.app.switcher.backup_dir / "burn_calibration.json"

    def _load_calibration(self) -> None:
        """Best-effort: an unreadable cache costs freshness, never the view."""
        import json

        try:
            self._tracker.restore_calibration(
                json.loads(self._calibration_path().read_text(encoding="utf-8"))
            )
        except Exception:
            pass

    def _save_calibration(self) -> None:
        from claude_swap.settings import atomic_write_json

        try:
            atomic_write_json(
                self._calibration_path(), self._tracker.calibration_state()
            )
        except Exception:
            pass

    def _models(self) -> tuple[str, ...]:
        from claude_swap.settings import parse_model_names

        return parse_model_names(self._settings.model if self._settings else None)

    # -- engine -------------------------------------------------------------

    def _start_engine(self) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            # Disarmed is a real engine in dry-run, not a stopped one: it keeps
            # polling and keeps logging what it WOULD do, which is what makes
            # the screen honest before anyone trusts it with a switch.
            dry_run=not self._armed,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            # Same worker group the auto view uses: the engine loop runs
            # until its screen stops it, so every test helper that waits for
            # workers has to know to skip it, and one name means one exclusion.
            group="engine",
            exit_on_error=False,
            name=f"fleet-engine-{'live' if self._armed else 'dry'}",
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            pass  # screen tearing down mid-tick; the event has nowhere to go

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#fleet-log", RichLog).write(event_text(event, palette=palette))
        if event.kind == "switch":
            self.app.request_refresh()

    # -- actions ------------------------------------------------------------

    def action_toggle_armed(self) -> None:
        if self._armed:
            self._set_armed(False)
            return
        self.app.push_screen(
            ConfirmModal(
                "Arm auto-switch? claude-swap will change your active account "
                "on its own when quota is about to expire or run out.\n\n"
                "The display keeps working either way.",
                title="Arm auto-switch",
                yes_label="Arm",
            ),
            lambda confirmed: self._set_armed(True) if confirmed else None,
        )

    def _set_armed(self, armed: bool) -> None:
        self._armed = armed
        if self._engine is not None:
            self._engine.stop()
        self._start_engine()
        self._log_note("auto-switch ARMED" if armed else "auto-switch disarmed")
        self._display_tick()

    def action_adjust_threshold(self) -> None:
        self._adjusting = not self._adjusting
        self.refresh_bindings()
        self._display_tick()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._adjusting = False
            self.refresh_bindings()
            self._display_tick()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False
        return True

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting or self._settings is None:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        self._commit_threshold(
            min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        )

    def action_apply_recommended(self) -> None:
        """Adopt the threshold the measured burn rate implies."""
        estimate = self._estimate()
        recommended = estimate.recommended_threshold() if estimate else None
        if recommended is None:
            self._log_note("no burn measured yet — nothing to suggest")
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        self._commit_threshold(min(spec.hi, max(spec.lo, round(recommended, 1))))

    def _commit_threshold(self, value: float) -> None:
        """Set the threshold everywhere it is read, and write it to disk.

        PERSISTED ON PURPOSE. The engine outlives this screen — it runs from
        settings.json in `cswap auto` and on the next launch — so a threshold
        that reverted on exit would leave the user protected by a number they
        had already rejected.
        """
        if self._settings is None or value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        try:
            save_settings(self.app.switcher.backup_dir, self._settings)
        except Exception as error:  # pragma: no cover - disk-level failure
            self._log_note(f"could not save threshold: {error}")
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self._display_tick()

    def _log_note(self, message: str) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#fleet-log", RichLog).write(
            Text(f"— {message} —", style=palette.muted)
        )

    # -- render -------------------------------------------------------------

    def _estimate(self):
        if self._tracker is None:
            return None
        snapshot = self.app.snapshot
        active = snapshot.active_number if snapshot else None
        return self._tracker.estimate(active or "")

    def _segments(self, now: float) -> list[fleet.FleetSegment]:
        snapshot = self.app.snapshot
        if snapshot is None:
            return []
        models = self._models()
        return fleet.order_segments([
            fleet.segment_for(
                number=account.number,
                email=account.email,
                alias=account.alias,
                usage=account.usage.last_good,
                models=models,
                now=now,
                is_active=account.is_active,
            )
            for account in snapshot.accounts
            if account.kind != "api_key"
        ])

    def _display_tick(self) -> None:
        if self._sensor is not None:
            self._sensor.poll()
        if not self.is_attached:
            return
        now = time.time()
        palette = Palette.from_theme(self.app.current_theme)
        segments = self._segments(now)
        self._render_headline(segments, now, palette)
        self._render_bar(segments, palette)
        self._render_legend(segments, now, palette)
        self._render_burn(palette)
        self._render_accounts(segments, now, palette)

    def _tier_style(self, segment: fleet.FleetSegment, palette: Palette) -> str:
        return {
            "urgent": palette.sev_crit,
            "watch": palette.sev_warn,
            "calm": palette.sev_ok,
            "unknown": palette.muted,
        }[segment.tier()]

    def _render_headline(
        self, segments: list[fleet.FleetSegment], now: float, palette: Palette
    ) -> None:
        text = Text()
        at_risk = fleet.total_at_risk(segments, now, AT_RISK_HORIZON_S)
        if at_risk > 0:
            text.append(f"{at_risk:.0f} points expire within 24h", style=palette.sev_crit)
        elif segments:
            text.append("nothing expiring within 24h", style=palette.sev_ok)
        else:
            text.append("no readable accounts", style=palette.muted)
        text.append("   ")
        if self._armed:
            text.append(" AUTO ON ", style=f"bold {palette.accent} reverse")
        else:
            text.append(" AUTO OFF ", style=f"{palette.muted} reverse")
        if self._settings is not None:
            text.append("   switch at ", style=palette.muted)
            text.append(
                f"{pct_label(self._settings.threshold)}%",
                style=palette.accent if self._adjusting else palette.foreground,
            )
            if self._adjusting:
                text.append("  ← → adjust · enter done", style=palette.muted)
        self.query_one("#fleet-headline", Static).update(text)

    def _bar_width(self) -> int:
        # Two cells of padding keep the bar off the frame at every terminal
        # size; the floor stops the geometry being asked for a negative width
        # mid-resize, which Textual can transiently report.
        return max(10, self.size.width - 2)

    def _render_bar(
        self, segments: list[fleet.FleetSegment], palette: Palette
    ) -> None:
        width = self._bar_width()
        widths = fleet.segment_widths(segments, width)
        text = Text()
        drawn = 0
        for segment, cells in zip(segments, widths):
            if cells <= 0:
                continue
            style = self._tier_style(segment, palette)
            glyph = _HATCHED if segment.blocked else _FILLED
            # The join replaces the segment's first cell rather than adding
            # one, so the bar's total width stays exactly what the geometry
            # computed and the legend below still lines up with it.
            if drawn:
                text.append(_JOIN, style=style)
                cells -= 1
            if cells > 0:
                text.append(glyph * cells, style=style)
            drawn += 1
        if not text.plain:
            text.append(_EMPTY * width, style=palette.track)
        self.query_one("#fleet-bar", Static).update(text)

    @staticmethod
    def _legend_label(segment: fleet.FleetSegment, cells: int, now: float) -> str:
        """The most informative label that fits in ``cells``.

        A narrow segment used to get NO label, which is the worst outcome
        available: a small stake is exactly the one whose deadline a reader
        cannot infer from the picture, and it is often the one expiring first.
        So the label degrades instead of vanishing, and it degrades toward the
        DEADLINE rather than the name — an unlabelled account is still
        identifiable by position and color, while an unlabelled deadline is
        simply lost.
        """
        marker = "▲ " if segment.is_active else ""
        countdown = segment.countdown_text(now)
        date = segment.deadline_text
        candidates = [
            f"{marker}{segment.label} {date}" + (f" ({countdown})" if countdown else ""),
            f"{marker}{segment.label} {countdown}" if countdown else "",
            f"{marker}{date}",
            f"{marker}{countdown}" if countdown else "",
            marker.strip(),
        ]
        # One cell is reserved so a label can never touch the next segment's
        # label: "dev4 5d▲ dev" reads as one token and silently reassigns a
        # deadline to the wrong account.
        room = cells - 1
        for candidate in candidates:
            if candidate and len(candidate) <= room:
                return candidate
        return ""

    def _render_legend(
        self, segments: list[fleet.FleetSegment], now: float, palette: Palette
    ) -> None:
        """Deadlines written UNDER their own segment, aligned to its start.

        Placing each label at its segment's left edge is what makes the bar
        readable as a timeline of expiries rather than a stack of colors: the
        eye reads left to right and finds "8/19 · 20h" exactly where that
        account's quota begins. A label that would overrun the next segment's
        start is dropped rather than shifted — a misaligned date is worse than
        an absent one, because it silently reassigns quota to the wrong
        deadline.
        """
        width = self._bar_width()
        widths = fleet.segment_widths(segments, width)
        line = [" "] * width
        styles: list[tuple[int, int, str]] = []
        cursor = 0
        for segment, cells in zip(segments, widths):
            if cells <= 0:
                continue
            label = self._legend_label(segment, cells, now)
            if label and cursor + len(label) <= width:
                for offset, char in enumerate(label):
                    line[cursor + offset] = char
                styles.append(
                    (cursor, cursor + len(label), self._tier_style(segment, palette))
                )
            cursor += cells
        text = Text("".join(line).rstrip())
        for start, end, style in styles:
            if start < len(text.plain):
                text.stylize(style, start, min(end, len(text.plain)))
        self.query_one("#fleet-legend", Static).update(text)

    def _render_burn(self, palette: Palette) -> None:
        estimate = self._estimate()
        text = Text()
        text.append("burn  ", style=palette.muted)
        if estimate is None or estimate.pct_per_s is None:
            # Percent needs two API samples bracketing local spend; tokens do
            # not. Showing the token rate meanwhile proves the instrument is
            # alive and says what it is waiting for — an indefinite
            # "measuring…" reads as a hang.
            if estimate is not None and estimate.tokens_per_s > 0:
                text.append(
                    f"{estimate.tokens_per_s:,.0f} tok/s", style=palette.foreground
                )
                text.append(
                    "  ·  calibrating against the next usage sample",
                    style=palette.muted,
                )
            else:
                text.append("idle — nothing burning locally", style=palette.muted)
            self.query_one("#fleet-burn", Static).update(text)
            return
        rate = estimate.pct_per_s
        text.append(f"{rate * 100:.2f}%/100s", style=palette.foreground)
        seconds_per_pct = estimate.seconds_per_pct
        if seconds_per_pct is None:
            text.append("  idle", style=palette.sev_ok)
        else:
            text.append(f"  ·  1% every {seconds_per_pct:.0f}s", style=palette.muted)
        text.append(
            "  ·  " + ("calibrated" if estimate.calibrated else "API average"),
            style=palette.muted,
        )
        recommended = estimate.recommended_threshold()
        if recommended is not None and self._settings is not None:
            text.append("     suggested threshold ", style=palette.muted)
            good = recommended >= self._settings.threshold
            text.append(
                f"{pct_label(recommended)}%",
                style=palette.sev_ok if good else palette.sev_warn,
            )
            if not good:
                text.append(
                    f" (yours is {pct_label(self._settings.threshold)}% — press r)",
                    style=palette.sev_warn,
                )
        self.query_one("#fleet-burn", Static).update(text)

    def _render_accounts(
        self, segments: list[fleet.FleetSegment], now: float, palette: Palette
    ) -> None:
        """One line per account: the three windows collapsed to what binds.

        Reading a fleet used to mean holding three percentages per account in
        your head. What actually decides anything is the worst one, so that is
        what each row leads with; the rest is the deadline the bar above is
        drawn from.
        """
        snapshot = self.app.snapshot
        if snapshot is None:
            self.query_one("#fleet-accounts", Static).update(
                Text("loading…", style=palette.muted)
            )
            return
        models = self._models()
        by_number = {account.number: account for account in snapshot.accounts}
        text = Text()
        for segment in segments:
            account = by_number.get(segment.number)
            if account is None:
                continue
            headroom = oauth.account_headroom(account.usage.last_good, models)
            marker = "▸" if segment.is_active else " "
            text.append(f"{marker}{segment.number} ", style=palette.accent
                        if segment.is_active else palette.muted)
            text.append(f"{account.email:<28.28} ", style=palette.foreground)
            if headroom is None:
                text.append("usage unknown", style=palette.muted)
            else:
                used = 100.0 - headroom
                text.append(f"{used:3.0f}% used", style=palette.severity(used))
                # WHICH window is at that number. "90% used" on the 5-hour
                # window means blocked for a few hours; the same figure on the
                # weekly means blocked for days. Collapsing three windows to
                # one number is only safe if the number says which one it is.
                windows = oauth.relevant_windows(account.usage.last_good, models)
                if windows:
                    binding = max(windows, key=lambda w: w[1])
                    text.append(f" ({binding[0]})", style=palette.muted)
                text.append(
                    f"   {segment.headroom_pct:3.0f} pts expire "
                    f"{segment.deadline_text}",
                    style=palette.muted,
                )
                countdown = segment.countdown_text(now)
                if countdown:
                    text.append(f" ({countdown})", style=self._tier_style(
                        segment, palette))
                if segment.blocked:
                    text.append("  · blocked until its window resets",
                                style=palette.sev_warn)
            text.append("\n")
        self.query_one("#fleet-accounts", Static).update(text)
