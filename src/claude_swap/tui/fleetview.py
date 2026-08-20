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
from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from claude_swap import fleet, oauth
from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent, pct_label
from claude_swap import burn as burn_mod
from claude_swap.burn import BurnTracker, TranscriptBurnSensor
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    SETTING_SPECS,
    load_settings,
    parse_account_weights,
    save_settings,
)
from claude_swap.sky import SkyWatcher
from claude_swap.tui import data, pets
from claude_swap.tui.skyview import SKY_H, SKY_SLOWDOWN, scene_rows
from claude_swap.tui.sprite import render as render_sprite
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

# The display tick. One second is chosen because the instrument behind it is
# free: transcript tailing reads only bytes appended since the last pass, so
# the cost is a handful of stats and no network at all.
DISPLAY_INTERVAL_S = 1.0

# Seconds per animation frame. The pet's phase is derived from the CLOCK, never
# from a repaint counter: repaints arrive at wildly uneven intervals (a one
# second data tick, a snapshot every three, an engine event whenever one
# happens), so counting them made the walk stutter and skip. Deriving the frame
# from elapsed time means a repaint cannot change the phase and the gait runs
# at exactly this period no matter how often the screen is redrawn.
SPRITE_FRAME_S = 0.22

# Sleep runs at a fraction of the waking rate. A breathing sleeper animated at
# 4.5 fps looks agitated, which is the opposite of the thing being shown.
_SLEEP_SLOWDOWN = 5

# (weighted tokens per second, timer frames per swing frame), fastest first.
# Logarithmic, because token rates span orders of magnitude between one quiet
# edit and a fleet of subagents; a linear map would sit pinned at one end.
_SWING_STEPS = ((4000.0, 1), (1500.0, 2), (400.0, 3), (0.0, 4))

# The sleep puffs, top row first. A row's puff is chosen by its distance from
# the current phase, so the whole column appears to rise.
_ZZZ = ("zZ", "z", "", "Z", "")

# How recently tokens must have been spent for the pet to count as awake.
# Longer than one turn's thinking pause, so he does not nod off between tool
# calls, and short enough that a finished session puts him to sleep promptly.
_BURN_WINDOW_S = 90.0

# Quota expiring inside this window is what the headline "about to lose"
# figure counts. A day is the horizon a person can actually act on — anything
# further out can be rescued by tomorrow's session.
AT_RISK_HORIZON_S = 24 * 3600.0

# The house glyph vocabulary, shared with widgets.bar_cells so the fleet bars
# and the per-window bars on the dashboard read as the same instrument. Solid
# blocks were tried first and clashed badly: cswap's whole visual register is
# thin lines.
_FILLED = "━"
_BLOCKED = "╌"  # quota is there, but this window is spent — cannot draw on it
_CAP = "╸"  # half-width end cap: where one account's fuel stops
_EMPTY = "─"
# Segment joins are DRAWN, not merely colored. Color alone carries the whole
# structure only for a reader with a colour terminal and normal colour vision;
# a drawn boundary survives a pipe, a screenshot, and a monochrome scrollback.
_JOIN = "┃"

# The three windows drawn as bars, in the order a person thinks about them:
# what stops me in the next few hours, what stops me this week, and what stops
# the model I actually use. `None` means "whatever per-model windows the
# accounts report", resolved at render time.
#: Claude Code's own idle-star cycle. Borrowed on purpose: the reader already
#: reads this shape as "a session is working", and inventing a second vocabulary
#: for the same fact would make them learn it twice.
_SPINNER = ("✻", "✽", "✳", "✢", "·", "✢", "✳", "✽")

#: A project counts as RUNNING if it has spent anything in this window. Short,
#: because the question is "is something happening now", not "has this project
#: been used lately" — the same distinction that made the gating window wrong.
_INSTANCE_ACTIVE_WINDOW_S = 90.0


def _session_titles() -> dict[str, str]:
    """``session id -> the name you gave it``, from Claude Code's transcripts.

    A session id is not a name. Claude Code records the title as a
    ``custom-title`` line, so only the tail of each transcript is read — the
    record is rewritten on every rename, and the newest wins.
    """
    import json

    from claude_swap.paths import get_claude_config_home

    titles: dict[str, str] = {}
    try:
        paths = list((get_claude_config_home() / "projects").glob("*/*.jsonl"))
    except OSError:
        return titles
    for path in paths:
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 200_000))
                for raw in handle:
                    if b'"custom-title"' not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    title = record.get("customTitle")
                    if record.get("type") == "custom-title" and isinstance(title, str):
                        titles[path.stem] = title.strip()
        except OSError:
            continue
    return titles


def _project_label(encoded: str) -> str:
    """``-home-logocat-Server-x`` -> ``x``.

    Claude Code encodes a project path by replacing separators with dashes,
    which is unreadable and long. Only the leaf matters here — the question is
    which of your sessions, not where it lives.
    """
    leaf = encoded.rstrip("-").rsplit("-", 1)[-1]
    return leaf or encoded


def _short_duration(hours: float) -> str:
    """``40m`` / ``11.6h`` / ``4d`` — one unit, chosen so the number stays
    readable. Two units ("4d 9h") is more precise than any of these estimates
    deserve to look."""
    if hours < 1.0:
        return f"{max(1, round(hours * 60)):.0f}m"
    if hours < 24.0:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.0f}d"


_BAR_ROWS: tuple[tuple[str, str | None], ...] = (
    ("session", "5h"),
    ("weekly", "7d"),
    ("model", None),
)

# Bar width follows the dashboard's own rule (widgets.account_card_text):
# capped, never spanning the terminal. A gauge that grows with the window
# stops being comparable between glances.
_BAR_MIN = 12
_BAR_MAX = 36


class FleetScreen(Screen):
    """The single-screen fleet view."""

    BINDINGS = [
        Binding("a", "toggle_armed", "Auto on / off"),
        Binding("up", "select(-1)", "Pick account"),
        Binding("down", "select(1)", "Pick account"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "confirm", "Confirm"),
        Binding("r", "apply_recommended", "Use suggested"),
        Binding("h", "toggle_status", "Hide log"),
        Binding("q", "app.quit", "Quit"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # ARMED BY DEFAULT. The command exists to stop quota being wasted, and
        # a gauge that watches it happen without acting is not that tool. `a`
        # turns it off, and turning it off is what hands the arrow keys over
        # to manual selection — the two modes cannot fight over the account.
        self._armed = True
        self._selected: int | None = None
        self._adjusting = False
        self._sensor: TranscriptBurnSensor | None = None
        self._tracker: BurnTracker | None = None
        self._sky: SkyWatcher | None = None
        self._latest: AutoSwitchEvent | None = None
        self._note: str = ""
        # The engine's running commentary is useful while you are deciding
        # whether to trust it and noise afterwards, so it hides — but the pet
        # never does. It is the proof the instrument is still ticking, and a
        # screen with no moving part looks identical to a wedged one.
        self._show_log = True
        self._ticks = 0
        # The gating set as of the last display tick, so a WIDENING can be
        # spotted the second it happens. None until the first tick.
        self._gating_seen: frozenset[str] | None = None
        # (label, shown path, real path, count) — refreshed on the display
        # tick because process inspection is far too slow for frame rate.
        self._instance_groups: list[tuple[str, str, str, int]] = []

    def compose(self) -> ComposeResult:
        # The pet sits in its own column on the RIGHT. Below the gauges it
        # pushed the account list off short terminals; beside them it costs no
        # vertical space at all, which is the whole reason it can be this size.
        with Vertical(id="fleet-top"):
            yield Static("", id="fleet-headline")
            yield Static("", id="fleet-bars")
            yield Static("", id="fleet-burn")
            # The pet sits BESIDE the account list, not down the whole right
            # edge: the gauges want the full width and the list is the only
            # block tall enough to park something next to.
            with Horizontal(id="fleet-body"):
                yield Static("", id="fleet-accounts")
                yield Static("", id="fleet-status")
            yield Static("", id="fleet-instances")
            # The engine's line goes under Running instances, where it was
            # before: in the pet's narrow column it wrapped across four rows
            # and read as damage.
            yield Static("", id="fleet-log")
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
        # Weather costs no tokens and never blocks: one small key-less request
        # on a background thread, at most a few times an hour, cached to disk
        # so a fresh process is not blank while it waits.
        self._sky = SkyWatcher(
            cache_path=self.app.switcher.backup_dir / "sky.json",
            location=getattr(self._settings, "location", None),
        )
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        self._start_engine()
        # ONE timer, at the animation rate. Two timers were tried — a slow one
        # for the data and a fast one for the pet — and the fast one silently
        # never fired: measured, `_animate` was entered zero times in four
        # seconds while an identical interval registered after mount fired at
        # 4.3/s. Rather than chase that, the single timer runs at frame rate
        # and only does the expensive recompute every Nth call, which is what
        # the split was for anyway.
        self.set_interval(SPRITE_FRAME_S, self._frame_tick)
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
        before = self._tracker.pct_per_token()
        self._tracker.note_active(snapshot.active_number)
        for account in snapshot.accounts:
            entry = account.usage
            if entry.last_good is None or entry.fetched_at is None:
                continue
            if not account.is_active:
                # Only the active account is being spent, so only its windows
                # can be paired with this machine's tokens. An idle account's
                # percentage does move — someone else's machine — and pairing
                # that with local tokens would calibrate against noise.
                continue
            for label, pct, _ in oauth.relevant_windows(entry.last_good, ("all",)):
                self._tracker.observe(
                    account.number, label, pct, entry.fetched_at
                )
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

    def _row_status_note(self, label: str, palette: Palette) -> Text | None:
        """``not running`` / ``running · kenshi-zone-mc`` for a per-model row.

        The quota behind a window that is not gating is real and still
        expires; it simply is not what will stop you right now. Saying so
        beats removing the bar, which reads as the limit having disappeared.

        When it IS gating, naming who is spending on it answers the question
        that follows immediately — the decision covers the whole machine, so
        one unrelated session makes every account exhausted on that model read
        as unusable.
        """
        from claude_swap.burn import ACCOUNT_WIDE_WINDOWS

        if label in ACCOUNT_WIDE_WINDOWS:
            return None
        text = Text(no_wrap=True, overflow="ellipsis")
        if not self._row_gates(label):
            text.append("not running", style=palette.track)
            return text
        text.append("running", style=palette.accent)
        for note in self._gating_source_note(label, palette):
            text.append(note)
        return text

    def _gating_source_note(self, label: str, palette: Palette) -> list[Text]:
        """``· claude-sandbox`` — the projects keeping a model window binding.

        Only for per-model rows: the 5h and 7d windows count every request, so
        naming a source there says nothing a reader does not already know.
        Silent when nothing local is spending on it — the window may be gating
        because the model is merely SELECTED, which is a different fact and
        already visible in the settings.
        """
        from claude_swap.burn import ACCOUNT_WIDE_WINDOWS

        if label in ACCOUNT_WIDE_WINDOWS or self._sensor is None:
            return []
        try:
            sources = self._sensor.recent_sources(model_filter=label)
        except Exception:  # pragma: no cover - sensing must never break a paint
            return []
        if not sources:
            return []
        names = [_project_label(name) for name, _tokens in sources[:2]]
        if len(sources) > 2:
            names.append(f"+{len(sources) - 2}")
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("  · ", style=palette.track)
        text.append(", ".join(names), style=palette.muted)
        return [text]

    def _row_gates(self, label: str) -> bool:
        """Is this window one the engine would actually stop on right now?"""
        from claude_swap.burn import ACCOUNT_WIDE_WINDOWS

        if label in ACCOUNT_WIDE_WINDOWS:
            return True
        gating = self._models()
        return "all" in {name.lower() for name in gating} or label in gating

    def _declared_models(self) -> tuple[str, ...]:
        """What ``autoswitch.model`` asks to care about, before measurement.

        Kept apart from :meth:`_models` because the bars need both. A window
        the user opted OUT of should not be drawn at all; one that merely is
        not running this minute still holds quota that still expires, and
        deleting its bar reads as the tool losing a limit rather than as the
        limit not applying.
        """
        from claude_swap.settings import parse_model_names

        return parse_model_names(self._settings.model if self._settings else None)

    def _models(self) -> tuple[str, ...]:
        """The per-model windows that GATE right now — the engine's own set.

        Narrowed by measurement exactly as the engine narrows it, because a
        gauge drawn from a wider list than the decision reads is a gauge
        showing fuel nothing will ever act on.
        """
        from claude_swap.burn import ACCOUNT_WIDE_WINDOWS, burning_models

        declared = self._declared_models()
        snapshot = self.app.snapshot
        if (
            not declared
            or self._sensor is None
            or snapshot is None
            or not getattr(self._settings, "measured_model_mix", True)
        ):
            return declared
        reported: list[str] = []
        for account in snapshot.accounts:
            for name, _pct, _reset in oauth.relevant_windows(
                account.usage.last_good, ("all",)
            ):
                if name not in ACCOUNT_WIDE_WINDOWS and name not in reported:
                    reported.append(name)
        if not reported:
            return declared
        live = burning_models(self._sensor, declared, reported)
        return declared if live is None else live

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
        """Keep the LATEST line only.

        A scrolling log of every poll was mostly the same sentence repeated,
        and it pushed the thing worth reading — what the engine decided just
        now — off the bottom. One line, replaced in place.
        """
        if not self.is_attached:
            return
        self._latest = event
        self._render_log(Palette.from_theme(self.app.current_theme))
        if event.kind == "switch":
            self.app.request_refresh()

    # -- actions ------------------------------------------------------------

    def action_toggle_armed(self) -> None:
        self._set_armed(not self._armed)

    def _set_armed(self, armed: bool) -> None:
        self._armed = armed
        # Selection belongs to manual mode only: leaving a cursor visible
        # while the engine is free to switch would show a choice the next
        # tick can overrule.
        self._selected = None if armed else self._active_index()
        # check_action gates the arrow keys on `_armed`, and Textual caches
        # that verdict until asked to re-evaluate. Without this the keys stay
        # disabled for the rest of the session after the very first toggle.
        self.refresh_bindings()
        if self._engine is not None:
            self._engine.stop()
        self._start_engine()
        self._log_note("auto-switch ARMED" if armed else "auto-switch disarmed")
        self._display_tick()

    def _active_index(self) -> int | None:
        snapshot = self.app.snapshot
        if snapshot is None:
            return None
        for index, account in enumerate(snapshot.accounts):
            if account.is_active:
                return index
        return 0 if snapshot.accounts else None

    def action_select(self, delta: int) -> None:
        """Move the account cursor. Manual mode only — see `_set_armed`."""
        snapshot = self.app.snapshot
        if self._armed or snapshot is None or not snapshot.accounts:
            return
        if self._selected is None:
            self._selected = self._active_index() or 0
        self._selected = (self._selected + delta) % len(snapshot.accounts)
        self._display_tick()

    def action_confirm(self) -> None:
        """Enter: finish a threshold edit, else switch to the picked account."""
        if self._adjusting:
            self._adjusting = False
            self.refresh_bindings()
            self._display_tick()
            return
        snapshot = self.app.snapshot
        if self._armed or self._selected is None or snapshot is None:
            return
        try:
            account = snapshot.accounts[self._selected]
        except IndexError:
            return
        if account.is_active:
            self._log_note(f"already on account {account.number}")
            return
        self._log_note(f"switching to account {account.number}")
        self.app.do_switch(account.number)

    def action_toggle_status(self) -> None:
        self._show_log = not self._show_log
        self._render_log(Palette.from_theme(self.app.current_theme))

    def action_adjust_threshold(self) -> None:
        self._adjusting = not self._adjusting
        self.refresh_bindings()
        self._display_tick()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "threshold_step" and not self._adjusting:
            return False
        if action == "select" and self._armed:
            # Hidden while the engine owns the account, so the footer never
            # advertises a key that would do nothing.
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
        """Adopt the threshold the measured burn rate implies — DOWNWARD ONLY.

        The suggestion is the HIGHEST trigger the current rate can survive, so
        adopting it on a quiet machine RAISES the threshold: an idle reading
        recommends 99, and a user who had deliberately set 94 would hand back
        five points of reserve by pressing one key. The engine already takes
        the lower of the two every tick, so the only thing this key can
        usefully do is make the stored value tighter.

        A suggestion above the current setting is not an error, and the burn
        line already renders it calmly rather than prompting for it — this
        just refuses to act on it.
        """
        estimate = self._estimate()
        recommended = estimate.recommended_threshold() if estimate else None
        if recommended is None:
            self._log_note("no burn measured yet — nothing to suggest")
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, round(recommended, 1)))
        if self._settings is not None and value >= self._settings.threshold:
            self._log_note(
                f"suggested {pct_label(value)}% is not tighter than yours "
                f"({pct_label(self._settings.threshold)}%) — left alone"
            )
            return
        self._commit_threshold(value)

    def _commit_threshold(self, value: float) -> None:
        """Set the threshold everywhere it is read, and write it to disk.

        PERSISTED ON PURPOSE. The engine outlives this screen — it runs from
        settings.json in `cfuel auto` and on the next launch — so a threshold
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
        self._note = message
        self._latest = None
        self._render_log(Palette.from_theme(self.app.current_theme))

    def _render_status(self, palette: Palette) -> None:
        """The pet, with the single most recent event spoken beside it.

        The event text sits on ONE row of the sprite rather than under it, so
        the block stays the sprite's own height whether the log is shown or
        hidden — a layout that changed height on `h` would shift everything
        above it and make the key feel like it broke something.
        """
        frame = self._sprite_frame()
        # Asleep when nothing is being spent on this machine. That makes the
        # pet report something the numbers do not: whether the burn reading is
        # a measurement of work or of an idle minute.
        asleep = not self._burning()
        if asleep:
            sprite, frame = pets.SLEEPING, frame // _SLEEP_SLOWDOWN
        else:
            sprite, frame = pets.WORKING, frame // self._swing_divisor()
        text = Text(no_wrap=True, overflow="ellipsis")
        # ONE SCENE, not a weather panel above a cut-out. The pet is painted
        # onto the sky's own ground, so there is no hole around him and he
        # reads as being outside in whatever weather is actually outside.
        # No caption: the picture is the statement, and a place name and a
        # temperature beside it are just words competing with it.
        sky = self._sky.state() if self._sky else None
        # The sleep puffs are laid INTO the night sky rather than appended
        # after the rows: appended they landed on the terminal's own
        # background, outside the painted scene.
        overlay: list[tuple[int, int, str, str]] = []
        if asleep:
            for index, puff in enumerate(_ZZZ):
                if not puff:
                    continue
                row = (index + frame) % max(1, SKY_H // 2)
                for offset, char in enumerate(puff):
                    overlay.append((row, 24 + index + offset, char, palette.foreground))
        if sky is not None:
            rows = scene_rows(
                sky,
                self._sprite_frame() // SKY_SLOWDOWN,
                sprite.frames[frame % len(sprite.frames)],
                sprite.palette,
                dim=asleep,
                overlay=overlay,
            )
        else:
            rows = render_sprite(sprite, frame, dim=asleep)
        for index, row in enumerate(rows):
            if index:
                text.append("\n")
            # FLUSH LEFT, no indent. A leading space put the sprite's last
            # column past the widget's content width, and every row wrapped —
            # the picture arrived one cell wider than the space it was given.
            text.append(row)
        # A caption under the icon, so the state is stated as well as drawn.
        text.append("\n")
        text.append(
            "Beep is sleeping…" if asleep else "Beep is working !",
            style=palette.muted if asleep else palette.accent,
        )
        self._update_status(text)

    def _render_log(self, palette: Palette) -> None:
        """The engine's latest line, under Running instances."""
        try:
            target = self.query_one("#fleet-log", Static)
        except Exception:
            return
        if not self._show_log:
            target.update(Text(""))
            return
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(self._status_line(palette))
        target.update(text)

    def _swing_divisor(self) -> int:
        """How many timer frames one pickaxe frame lasts — fewer when busier.

        The swing rate IS the burn rate; that is what makes the pet an
        instrument rather than a decoration. The scale is logarithmic because
        token rates span orders of magnitude between a quiet edit and a fleet
        of subagents, and a linear map would sit pinned at one end.
        """
        rate = self._sensor.tokens_per_s(_BURN_WINDOW_S) if self._sensor else 0.0
        for floor, divisor in _SWING_STEPS:
            if rate >= floor:
                return divisor
        return _SWING_STEPS[-1][1]

    def _burning(self) -> bool:
        """Whether this machine is spending tokens right now."""
        return bool(self._sensor and self._sensor.tokens_per_s(_BURN_WINDOW_S) > 0)

    @staticmethod
    def _sprite_frame() -> int:
        """Animation phase from the monotonic clock, so it cannot be advanced
        by a repaint — only by time passing."""
        return int(time.monotonic() / SPRITE_FRAME_S)

    def _wake_engine_if_a_limit_became_relevant(self) -> None:
        """Tick the engine NOW when a per-model window starts gating.

        The engine's cadence is tied to the API budget — about one usage fetch
        a minute — but noticing that a model started running costs no API call
        at all: the settings file and the transcripts are local and the
        percentages are already cached, so an extra tick is served from the
        store. The DECISION can therefore run at display cadence even though
        the FETCH cannot, which turns "up to a minute on an account that
        cannot serve the work" into about a second.

        Only ever on WIDENING. A window that stopped gating breaks nothing by
        being noticed late; one that started gating is exactly the case where
        the active account may already be unusable.
        """
        gating = frozenset(self._models())
        previous, self._gating_seen = self._gating_seen, gating
        if previous is None or self._engine is None:
            return
        if gating - previous:
            self._engine.wake()

    def _frame_tick(self) -> None:
        """Every frame: repaint the pet. Every Nth: recompute the gauges too."""
        if not self.is_attached:
            return
        self._ticks += 1
        every = max(1, round(DISPLAY_INTERVAL_S / SPRITE_FRAME_S))
        if self._ticks % every == 0:
            self._display_tick()
        else:
            palette = Palette.from_theme(self.app.current_theme)
            self._render_status(palette)
            # Cheap: the groups are cached by the display tick, so this only
            # re-lays the text and advances the spinner.
            self._render_instances(palette)

    def _status_line(self, palette: Palette) -> Text:
        """The latest engine event, or what the screen is doing instead."""
        text = Text(no_wrap=True, overflow="ellipsis")
        if self._latest is not None:
            style = (
                f"bold {palette.accent}"
                if self._latest.kind == "switch"
                else palette.sev_warn
                if self._latest.kind in ("error", "account-quarantined")
                else palette.muted
            )
            text.append(f"{data.clock_stamp()}  ", style=palette.track)
            text.append(self._latest.human(), style=style)
        elif self._note:
            text.append(self._note, style=palette.muted)
        else:
            text.append(
                "auto-switching armed" if self._armed else "watching only",
                style=palette.muted,
            )
        return text

    def _update_status(self, text: Text) -> None:
        try:
            self.query_one("#fleet-status", Static).update(text)
        except Exception:
            pass  # composed before mount, or torn down mid-tick

    # -- render -------------------------------------------------------------

    def _binding_window(self, account) -> str | None:
        """The window whose utilization currently gates this account."""
        windows = oauth.relevant_windows(account.usage.last_good, self._models())
        return max(windows, key=lambda w: w[1])[0] if windows else None

    def _estimate(self):
        """The burn rate expressed in the ACTIVE account's binding window.

        Which window matters because each is a different size: the same tokens
        are several times more of a 5-hour window than of a weekly one, so a
        rate quoted without saying which is a number with no unit.
        """
        if self._tracker is None:
            return None
        snapshot = self.app.snapshot
        if snapshot is None:
            return self._tracker.estimate("")
        active = next(
            (a for a in snapshot.accounts if a.is_active), None
        )
        if active is None:
            return self._tracker.estimate(snapshot.active_number or "")
        return self._tracker.estimate(active.number, self._binding_window(active))

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
        self._wake_engine_if_a_limit_became_relevant()
        segments = self._segments(now)
        self._render_headline(segments, now, palette)
        self._render_status(palette)
        self._render_bars(segments, now, palette)
        self._render_burn(palette)
        self._render_accounts(segments, now, palette)
        self._render_log(palette)

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
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("All fuel", style=f"bold {palette.foreground}")
        text.append("    ")
        # NAMED, NOT SUMMED. The old headline added headroom percentages
        # across accounts into "47 pts" — but a percent is a fraction of ONE
        # plan's pool, and pools differ, so the sum had no unit at all. Each
        # expiring quota is named with its own limit instead.
        expiring = sorted(
            (
                seg for seg in segments
                if seg.reset_ts is not None
                and seg.headroom_pct > 0
                and seg.reset_ts - now <= AT_RISK_HORIZON_S
            ),
            key=lambda seg: seg.reset_ts or 0.0,
        )
        if expiring:
            first = expiring[0]
            lead = f"{first.label} {first.window} {first.headroom_pct:.0f}%"
            if len(expiring) > 1:
                lead += f" +{len(expiring) - 1} more"
            text.append(f"{lead} expiring within 24h", style=palette.sev_crit)
        elif segments:
            text.append("nothing expiring within 24h", style=palette.sev_ok)
        else:
            text.append("no readable accounts", style=palette.muted)
        text.append("   ")
        if self._armed:
            text.append(" AUTO ", style=f"bold {palette.accent} reverse")
        else:
            text.append(" MANUAL ", style=f"{palette.muted} reverse")
            text.append("  ↑↓ pick · enter switch", style=palette.muted)
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
        """Bounded, like every bar in this tool.

        ``widgets.account_card_text`` caps its window bars the same way. A
        gauge that grows with the terminal cannot be compared between two
        glances at different window sizes.
        """
        return max(_BAR_MIN, min(_BAR_MAX, self.size.width - 46))

    @staticmethod
    def _rank_colours(count: int, palette: Palette) -> list[str]:
        """Red → amber → green across ``count`` ranks, most urgent first.

        Interpolated rather than a fixed triple so a fleet of six still gets
        six distinguishable steps of the same ramp.
        """
        def _rgb(value: str) -> tuple[int, int, int]:
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore

        stops = [_rgb(palette.sev_crit), _rgb(palette.sev_warn), _rgb(palette.sev_ok)]
        if count <= 1:
            return [palette.sev_crit]
        out: list[str] = []
        for index in range(count):
            position = index / (count - 1) * (len(stops) - 1)
            low = min(int(position), len(stops) - 2)
            frac = position - low
            channels = [
                round(stops[low][c] + (stops[low + 1][c] - stops[low][c]) * frac)
                for c in range(3)
            ]
            out.append("#%02x%02x%02x" % tuple(channels))
        return out

    @staticmethod
    def _deadline_rank(
        segments: list[fleet.FleetSegment],
    ) -> list[fleet.FleetSegment]:
        """Accounts by how far their deadline is from now, SOONEST FIRST.

        THE ONE ORDERING the colour ramp and the bar both consume, taken from
        this list rather than re-derived at each call site. Two places sorting
        "the same way" is exactly how the ramp stopped being monotonic before.

        Distance to the reset, not waste risk. Risk divides headroom BY that
        distance, so an account with 8 points left comes out the calmest thing
        on screen precisely because it has nothing left to lose — true, but it
        paints "nearly exhausted" and "plenty of time" the same green. The
        deadline on its own is also the number the reader is already looking
        at in the dates printed beside the bar.

        The reset is the account's BINDING weekly window — the one
        ``segment_for`` sizes the segment by — so the colour always agrees
        with the date next to it. No known reset sorts last and takes the calm
        end: an account nobody can schedule around is not an urgent one.
        """
        return sorted(
            segments,
            key=lambda seg: (
                seg.reset_ts if seg.reset_ts is not None else float("inf"),
                int(seg.number),
            ),
        )

    def _account_colours(
        self, segments: list[fleet.FleetSegment], palette: Palette
    ) -> dict[str, str]:
        """One colour per ACCOUNT, ranked on its deadline, for every bar.

        Colour is an identity here, not a per-row measurement. Ranking each
        bar separately made the same account red on the session row and amber
        on the weekly one, which reads as the account changing rather than as
        three views of the same fleet — and the session window is the wrong
        thing to colour by anyway: it recycles in hours, so nothing in it is
        ever "about to be wasted".
        """
        ordered = self._deadline_rank(segments)
        ramp = self._rank_colours(len(ordered), palette)
        return {seg.number: ramp[i] for i, seg in enumerate(ordered)}

    def _window_segments(self, label: str, now: float) -> list[fleet.FleetSegment]:
        snapshot = self.app.snapshot
        if snapshot is None:
            return []
        built = [
            fleet.window_segment(
                number=account.number, email=account.email, alias=account.alias,
                usage=account.usage.last_good, label=label,
                # The DECLARED set: a row exists for every window the user
                # asked to care about. WHETHER IT GATES is a separate fact,
                # drawn as a label rather than by deleting the row.
                models=self._declared_models(),
                now=now, is_active=account.is_active,
            )
            for account in snapshot.accounts
            if account.kind != "api_key"
        ]
        return [segment for segment in built if segment is not None]

    def _model_label(self) -> str | None:
        """The per-model window's own name (``Fable``), read from the data."""
        snapshot = self.app.snapshot
        if snapshot is None:
            return None
        for account in snapshot.accounts:
            scoped = (account.usage.last_good or {}).get("scoped")
            if isinstance(scoped, list):
                for window in scoped:
                    if isinstance(window, dict) and isinstance(window.get("name"), str):
                        return window["name"]
        return None

    def _runway_text(self, label: str, row: list[fleet.FleetSegment]) -> str:
        """``≈41h`` — how long this window's fuel lasts at the current burn.

        Time is the one unit that needs no conversion: percent is a fraction
        of pools that differ per window and per plan, and nobody budgets in
        tokens. Empty when nothing is measured — an idle machine has no burn
        to project, and a blank says that better than a made-up horizon.
        """
        if self._tracker is None:
            return ""
        seconds = fleet.runway_s(
            row, lambda number: self._tracker.estimate(number, label).pct_per_s
        )
        if seconds is None or seconds <= 0:
            return ""
        hours = seconds / 3600.0
        # Beyond a month the number stops informing anything — every window
        # here resets within a week, so the fuel outlives its own deadline
        # and the waste line is the one that speaks to that.
        if hours > 720:
            return "≈30d+"
        # Hours up to two days: "1d" for anything between 24h and 36h is too
        # coarse for the number people actually plan a session around.
        if hours >= 48:
            return f"≈{hours / 24.0:.0f}d"
        if hours >= 1:
            return f"≈{hours:.0f}h"
        return f"≈{max(1, round(hours * 60)):.0f}m"

    def _account_weights(self, row: list[fleet.FleetSegment]) -> list[float]:
        """Relative plan size per account, so the bars compare like with like.

        Three sources, most-trusted first:

        CONFIGURED (``autoswitch.accountWeights``). Only the user knows they
        bought a 20x and a 5x, and the usage API will never say — it reports
        utilization, and 40% of either plan is the same number.

        MEASURED. The burn calibration already answers this as a side effect:
        percent-per-token is small exactly in proportion to how large the
        plan's window is, so its reciprocal is the account's capacity in
        tokens and the ratio between accounts is the multiplier. Free, and it
        self-corrects if a plan changes.

        EQUAL. Before either is available. Wrong for a mixed fleet, but
        visibly wrong in a way the reader can correct, unlike a confident
        weighting derived from nothing.
        """
        configured = parse_account_weights(
            self._settings.account_weights if self._settings else None
        )
        weights: list[float] = []
        measured: dict[str, float] = {}
        if self._tracker is not None:
            for segment in row:
                k = self._tracker.pct_per_token(segment.number)
                if k and k > 0:
                    measured[segment.number] = 1.0 / k
        floor = min(measured.values()) if measured else None
        for segment in row:
            if segment.number in configured:
                weights.append(configured[segment.number])
            elif segment.number in measured and floor:
                weights.append(measured[segment.number] / floor)
            else:
                weights.append(1.0)
        return weights

    def _render_bars(
        self, segments: list[fleet.FleetSegment], now: float, palette: Palette
    ) -> None:
        """Three fuel gauges — 5h, 7d, and the per-model weekly window.

        WHAT IS LEFT IS PACKED TO THE LEFT. Every account contributes an equal
        slice of the bar, but the slices are drawn in two runs: all the
        remaining quota first, contiguous, then all the spent quota as track.
        So the length of the coloured run IS the fleet's remaining fuel for
        that window, readable without counting segments, while the per-account
        boundaries stay visible inside it.

        Ordered latest deadline → soonest, so the account that dies first sits
        at the right-hand edge of the coloured run: the point consumption eats
        into next. ``▲`` names it underneath.
        """
        rows: list[tuple[str, list[fleet.FleetSegment]]] = []
        model = self._model_label()
        for title, window in _BAR_ROWS:
            label = window or model
            if label is None:
                continue
            row = self._window_segments(label, now)
            if row:
                rows.append((label, row))
        if not rows:
            self.query_one("#fleet-bars", Static).update(
                Text("  measuring…", style=palette.muted)
            )
            return

        width = self._bar_width()
        text = Text(no_wrap=True, overflow="ellipsis")
        label_width = max(len(label) for label, _ in rows)
        colour_of = self._account_colours(segments, palette)
        # Furthest deadline first, soonest last — literally the REVERSE of the
        # colour ranking, read off the same list instead of re-sorted, so the
        # run always goes green on the left to red on the right and the two
        # cannot drift apart. The account that dies first lands at the right
        # edge of the coloured run, which is where consumption eats next.
        order = {
            seg.number: rank
            for rank, seg in enumerate(reversed(self._deadline_rank(segments)))
        }
        # HOW LONG THE TANK LASTS at the current burn, per row. Computed for
        # every row before any is drawn so the column has one width and the
        # three gauges keep a shared left edge — the spectrum only reads as
        # one instrument when the bars start together.
        runway_text = {
            label: self._runway_text(label, row) for label, row in rows
        }
        runway_width = max(
            (len(value) for value in runway_text.values() if value), default=0
        )
        for index, (label, row) in enumerate(rows):
            row = sorted(row, key=lambda s: order.get(s.number, 0))
            colours = [colour_of.get(seg.number, palette.muted) for seg in row]
            weights = self._account_weights(row)
            filled, spent, edges = self._gauge(row, colours, width, weights)
            # HOW MUCH IS IN THE TANK, so the run's length does not have to be
            # measured by eye. Over 100% is normal and meaningful: it says the
            # fleet holds more than one account's worth of this window.
            head = (
                f"  {label + ':':<{label_width + 1}} "
                f"{fleet.remaining_tank_pct(row, weights):>4.0f}%"
            )
            if runway_width:
                head += f" {runway_text[label]:<{runway_width}}"
            head += "  "
            # Marker columns come from the string that was actually drawn,
            # never from a hand-added width. Adding this column to a
            # hand-computed gutter would have slid every ▼ and ▲ off the
            # segment it names, silently.
            gutter = len(head)
            if index:
                text.append("\n")
            # ▼ ABOVE: the account whose quota in THIS window dies first.
            # An account with no reported reset is skipped rather than shown
            # with a dash — "expires —" is not a deadline, and picking it made
            # the marker name an account that was in no danger at all.
            expiring = [
                seg for seg in row if seg.reset_ts is not None and seg.headroom_pct > 0
            ]
            if expiring:
                soonest = min(expiring, key=lambda seg: seg.reset_ts or 0.0)
                self._marker(
                    text, gutter + edges.get(soonest.number, 0), "▼",
                    soonest, colour_of.get(soonest.number, palette.muted), palette,
                    now, suffix="",
                )
            text.append(head, style=palette.muted)
            text.append(filled)
            text.append(spent)
            text.append("  ")
            for position, (segment, colour) in enumerate(zip(row, colours)):
                if position:
                    text.append(" · ", style=palette.track)
                text.append(f"{segment.number} ", style=palette.muted)
                text.append(f"{100 - segment.headroom_pct:.0f}%", style=colour)
                countdown = segment.countdown_text(now)
                if countdown:
                    text.append(f" {countdown}", style=palette.muted)
            # ▲ BELOW: where quota is being drawn from right now. Two markers
            # rather than one because "what dies first" and "what I am
            # spending" are different questions, and the whole point of the
            # screen is the gap between them.
            active = next((seg for seg in row if seg.is_active), None)
            if active is not None:
                text.append("\n")
                self._marker(
                    text, gutter + edges.get(active.number, 0), "▲",
                    active, colour_of.get(active.number, palette.accent), palette,
                    now, suffix=" active", newline=False,
                )
            # WHETHER THIS LIMIT IS IN FORCE, on its own line UNDER the row.
            # It used to trail the per-account percentages, where the longest
            # row on the screen pushed it off the edge — the one place a
            # reader looks to find out why an account reads as unusable.
            note = self._row_status_note(label, palette)
            if note is not None:
                text.append("\n")
                text.append(" " * gutter)
                text.append(note)
        text.append("\n")
        # ONCE, WHEN THE TEXT IS COMPLETE. This lived at the end of `_marker`,
        # so the widget was published while the block was still being built —
        # it happened to look right only because nothing was appended after
        # the last marker. Moving one line below a marker made the last row's
        # status vanish, which is the same class of bug as publishing a
        # half-written record.
        self.query_one("#fleet-bars", Static).update(text)

    def _marker(
        self, text, column, glyph, segment, colour, palette, now, *,
        suffix="", newline=True,
    ) -> None:
        """One pointer under or over a segment's own edge, then its deadline."""
        text.append(" " * max(0, column))
        text.append(f"{glyph} ", style=colour)
        text.append(segment.label, style=colour)
        countdown = segment.countdown_text(now)
        if segment.reset_ts is not None:
            text.append(
                f" {segment.deadline_text}" + (f" ({countdown})" if countdown else ""),
                style=palette.muted,
            )
        if suffix:
            text.append(suffix, style=f"bold {palette.accent}")
        if newline:
            text.append("\n")

    def _gauge(
        self,
        row: list[fleet.FleetSegment],
        colours: list[str],
        width: int,
        weights: list[float],
    ) -> tuple[Text, Text, dict[str, int]]:
        """``(remaining run, spent run, each account's last filled column)``.

        Each account's slice is proportional to its PLAN SIZE, not to the
        number of accounts. Percentages are relative to their own plan, so
        equal slices would draw a 20x account's last 10% the same width as a
        5x account's — four times the real work, identical on screen. With the
        weighting, one cell means one amount of work everywhere on the row.

        A segment with anything left never rounds away to nothing: an account
        holding two points is exactly the one whose deadline matters, and an
        invisible segment reads as "that account is empty".
        """
        palette = Palette.from_theme(self.app.current_theme)
        total_weight = sum(weights) or float(max(1, len(row)))
        filled = Text()
        spent = Text()
        # Every account's own right-hand edge, so a marker can point at the
        # account it names instead of at whichever one happens to be last.
        edges: dict[str, int] = {}
        for segment, colour, weight in zip(row, colours, weights):
            share = max(2, round(width * weight / total_weight))
            keep = segment.headroom_pct / 100.0 * share
            cells = min(share, max(1, round(keep))) if segment.headroom_pct > 0 else 0
            if cells:
                # A spent short-term window still owns its quota; drawn dashed
                # so "cannot reach it yet" never reads as "does not have it".
                glyph = _BLOCKED if segment.blocked else _FILLED
                # Each account's run is CAPPED with the half-width glyph, so
                # the boundary survives without colour: packed left, the runs
                # are otherwise one unbroken line and a reader cannot tell
                # three accounts from one long one.
                filled.append(glyph * (cells - 1), style=colour)
                filled.append(_CAP if not segment.blocked else glyph, style=colour)
                edges[segment.number] = len(filled.plain) - 1
            rest = share - cells
            if rest > 0:
                if spent.plain:
                    spent.append(" ")
                spent.append(_EMPTY * rest, style=palette.track)
        if spent.plain:
            spent = Text(" ") + spent
        return filled, spent, edges

    def _render_burn(self, palette: Palette) -> None:
        """One rate per window, because there is no such thing as "the" rate.

        The same tokens are a large fraction of a 5-hour window and a small
        fraction of a weekly one, so a single figure would be a number without
        a unit. Each window is calibrated on its own scale (see
        ``burn.BurnTracker.observe``) and reported on its own line; the
        suggested threshold comes from whichever window is binding, since that
        is the one that will actually stop the account.
        """
        target = self.query_one("#fleet-burn", Static)
        snapshot = self.app.snapshot
        text = Text(no_wrap=True, overflow="ellipsis")
        if self._tracker is None or snapshot is None:
            target.update(Text("burn  measuring…", style=palette.muted))
            return
        active = next((a for a in snapshot.accounts if a.is_active), None)
        tokens_per_s = self._sensor.tokens_per_s() if self._sensor else 0.0
        windows = (
            oauth.relevant_windows(active.usage.last_good, ("all",))
            if active is not None
            else []
        )
        if not windows:
            target.update(Text("burn  measuring…", style=palette.muted))
            return
        binding = max(windows, key=lambda w: w[1])[0]
        label_width = max(len(name) for name, _, _ in windows)
        drawn = 0
        for name, _pct, _reset in windows:
            estimate = self._tracker.estimate(active.number, name)
            text.append("burn  " if not drawn else "\n      ", style=palette.muted)
            drawn += 1
            text.append(
                f"{name:<{label_width}}  ",
                style=palette.accent if name == binding else palette.muted,
            )
            if estimate.pct_per_s is None:
                # Percent needs two API samples of THIS window bracketing some
                # local spend; tokens do not. Showing the token rate meanwhile
                # proves the instrument is alive and names what it waits for.
                if tokens_per_s > 0:
                    text.append(f"{tokens_per_s:,.0f} tok/s", style=palette.foreground)
                    text.append("  ·  calibrating", style=palette.muted)
                else:
                    text.append("idle", style=palette.muted)
                continue
            seconds_per_pct = estimate.seconds_per_pct
            if seconds_per_pct is None:
                text.append("idle", style=palette.sev_ok)
            else:
                text.append(f"{estimate.pct_per_s:.3f}%/s", style=palette.foreground)
                text.append(
                    f"  ·  1% every {seconds_per_pct:.0f}s", style=palette.muted
                )
            if not estimate.calibrated:
                text.append("  ·  API average", style=palette.muted)
            if name == binding and self._settings is not None:
                recommended = estimate.recommended_threshold(
                    floor_pct=getattr(
                        self._settings, "burst_floor_pct", burn_mod.BURST_FLOOR_PCT
                    )
                    if self._settings is not None
                    else burn_mod.BURST_FLOOR_PCT
                )
                if recommended is not None:
                    text.append("   suggested ", style=palette.muted)
                    good = recommended >= self._settings.threshold
                    text.append(
                        f"{pct_label(recommended)}%",
                        style=palette.sev_ok if good else palette.sev_warn,
                    )
                    if not good:
                        text.append(
                            f" (yours {pct_label(self._settings.threshold)}%"
                            " — press r)",
                            style=palette.sev_warn,
                        )
        # THE WEEKLY rate, not the binding one. The quota that expires is
        # weekly, and a percentage point of the 5-hour window is a completely
        # different quantity of work — feeding the 5h rate into a weekly
        # projection reported "all spendable" on quota that will certainly be
        # lost, which is the exact false reassurance this screen exists to
        # prevent.
        weekly = self._weekly_window_label(active)
        waste = self._waste_note(
            self._tracker.estimate(active.number, weekly) if weekly else None,
            palette,
        )
        if waste is not None:
            text.append("\n      ")
            text.append(waste)
        handover = self._handover_note(
            self._tracker.estimate(active.number, weekly) if weekly else None,
            palette,
        )
        if handover is not None:
            text.append("\n      ")
            text.append(handover)
        target.update(text)

    def _less_capable_numbers(self, segments) -> set[str]:
        """Accounts the engine may take WITHOUT waiting for the risk axis.

        Mirrors the engine's own rule: an account whose idle-model windows are
        already spent destroys nothing by being used, so it skips the gate the
        handover projection is a countdown to.
        """
        snapshot = self.app.snapshot
        if snapshot is None:
            return set()
        usage = {a.number: a.usage.last_good for a in snapshot.accounts}
        active = next((seg for seg in segments if seg.is_active), None)
        if active is None:
            return set()
        gating = {name.lower() for name in self._models()}

        def capability(value) -> int:
            if not self._declared_models():
                return 0
            return sum(
                1
                for name, pct, _reset in oauth.relevant_windows(
                    value if isinstance(value, dict) else None, ("all",)
                )
                if name not in ("5h", "7d")
                and name.lower() not in gating
                and pct < 100.0
            )

        mine = capability(usage.get(active.number))
        return {
            seg.number
            for seg in segments
            if not seg.is_active
            and seg.headroom_pct > 0
            and capability(usage.get(seg.number)) < mine
        }

    def _handover_note(self, estimate, palette: Palette) -> Text | None:
        """Which account gets the next turn, and whether its quota survives.

        "No account is losing quota meaningfully faster than this one" is a
        true answer to a question nobody asked. The one people actually ask —
        twice, before this line existed — is *then when does the other account
        get its turn*, and it is answerable: the risk axis has the deadline in
        its denominator, so a candidate's urgency climbs until it clears the
        gate whether or not anything else changes.

        Silent when nothing is holding — while a switch is due the engine is
        already making it, and a countdown to something happening now is
        noise.
        """
        from claude_swap.autoswitch import (
            WASTE_HYSTERESIS_RATIO,
            WASTE_MIN_RISK_PCT_PER_H,
        )

        if self._settings is None or self._settings.strategy != "waste-first":
            return None  # the projection is this strategy's arithmetic
        now = time.time()
        segments = self._segments(now)
        active = next((seg for seg in segments if seg.is_active), None)
        if active is None:
            return None
        # A CANDIDATE THAT ALREADY QUALIFIES HAS NO ETA. The projection solves
        # when the risk axis crosses, and the capability shortcut bypasses that
        # axis entirely — so an account holding model-less quota was reported
        # as taking over in four days while the engine was about to move to it
        # on the next tick.
        capable_now = self._less_capable_numbers(segments)
        soonest: tuple[float, fleet.FleetSegment] | None = None
        for segment in segments:
            if segment.is_active or segment.number in capable_now:
                continue
            hours = fleet.handover_eta_h(
                active, segment, now,
                ratio=WASTE_HYSTERESIS_RATIO, floor=WASTE_MIN_RISK_PCT_PER_H,
            )
            if hours is None or hours <= 0:
                continue  # never, or already due — the engine is mid-switch
            if soonest is None or hours < soonest[0]:
                soonest = (hours, segment)
        if soonest is None:
            return None
        hours, segment = soonest
        text = Text(no_wrap=True, overflow="ellipsis")
        # "by", not "at": the estimate holds both headrooms still, and burning
        # the active account only lowers its risk, which brings this forward.
        text.append(f"{segment.label} takes over by ", style=palette.muted)
        text.append(
            datetime.fromtimestamp(now + hours * 3600.0).strftime("%a %H:%M"),
            style=palette.foreground,
        )
        window_h = ((segment.reset_ts or now) - now) / 3600.0 - hours
        # The limit is named because the number is a fraction OF THAT POOL.
        # "1 pt" read as a count of some universal thing; "7d 1%" says both
        # how much and of what, which is all the information there is.
        text.append(
            f" · its {segment.window} has {segment.headroom_pct:.0f}% left",
            style=palette.muted,
        )
        rate = (estimate.pct_per_s or 0.0) if estimate is not None else 0.0
        if rate > 0:
            need_h = segment.headroom_pct / (rate * 3600.0)
            text.append(" · needs ", style=palette.muted)
            text.append(_short_duration(need_h), style=palette.foreground)
            text.append(" of ", style=palette.muted)
            # Whether the turn is long enough to finish the quota is the whole
            # reason the wait is acceptable; without it this is just a clock.
            text.append(
                _short_duration(window_h),
                style=palette.sev_ok if window_h >= need_h else palette.sev_warn,
            )
        return text

    def _weekly_window_label(self, account) -> str | None:
        """Which reported window carries this account's expiring quota."""
        from claude_swap.autoswitch import weekly_binding

        binding = weekly_binding(account.usage.last_good, self._models())
        return binding[0] if binding else None

    def _waste_note(self, estimate, palette: Palette) -> Text | None:
        """Whether the current rate can finish the soonest-expiring quota.

        Silent on an unmeasured or idle rate. A confident "you will waste
        nothing" derived from an idle minute is worse than saying nothing —
        it is exactly the moment before a heavy turn starts — and one derived
        from an UNCALIBRATED weekly window would be a guess presented as a
        measurement.
        """
        now = time.time()
        rate_pct_per_s = (estimate.pct_per_s or 0.0) if estimate is not None else 0.0
        segments = [s for s in self._segments(now) if s.reset_ts is not None]
        if not segments or rate_pct_per_s <= 0 or estimate is None:
            return None
        soonest = min(segments, key=lambda s: s.reset_ts or float("inf"))
        hours = ((soonest.reset_ts or now) - now) / 3600.0
        if hours <= 0 or soonest.headroom_pct <= 0:
            return None
        spendable = rate_pct_per_s * 3600.0 * hours
        text = Text(no_wrap=True, overflow="ellipsis")
        if spendable >= soonest.headroom_pct:
            text.append(
                f"{soonest.label}'s {soonest.window} {soonest.headroom_pct:.0f}% "
                "is all spendable before it expires",
                style=palette.sev_ok,
            )
        else:
            text.append(
                f"at this rate {soonest.label} wastes "
                f"{soonest.headroom_pct - spendable:.0f}% of its "
                f"{soonest.window} ({soonest.headroom_pct:.0f}% left)",
                style=palette.sev_warn,
            )
        return text

    def _render_accounts(
        self, segments: list[fleet.FleetSegment], now: float, palette: Palette
    ) -> None:
        """The account list, in `cfuel list`'s own format.

        Deliberately not a new layout: this block answers "tell me everything
        about account 2", and a reader who already knows that shape from the
        CLI should not have to learn a second one. The lines come from the
        same formatter the CLI prints, so the two can never drift.
        """
        from claude_swap.switcher import _format_usage_lines

        snapshot = self.app.snapshot
        target = self.query_one("#fleet-accounts", Static)
        if snapshot is None:
            target.update(Text("loading…", style=palette.muted))
            return
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("Accounts:", style=f"bold {palette.foreground}")
        for index, account in enumerate(snapshot.accounts):
            # Blank line between accounts, exactly as `cfuel list` prints it:
            # three windows per account run together into an unreadable block
            # without it.
            text.append("\n\n" if index else "\n")
            cursor = (
                not self._armed
                and self._selected is not None
                and index == self._selected
            )
            text.append("  ")
            text.append(
                f"{account.number}: ",
                style=f"bold {palette.accent}" if cursor else palette.foreground,
            )
            text.append(account.email, style=palette.foreground)
            text.append(f" [{account.display_tag}]", style=palette.muted)
            if account.is_active:
                text.append("  (active)", style=f"bold {palette.accent}")
            if cursor and not account.is_active:
                text.append("   ← enter to switch", style=palette.accent)
            elif cursor:
                text.append("   ← you are here", style=palette.muted)
            lines = (
                _format_usage_lines(account.usage.last_good, account.usage.fetched_at)
                if account.usage.last_good
                else []
            )
            if not lines:
                text.append("\n     ")
                text.append("usage unavailable", style=palette.muted)
                continue
            for position, line in enumerate(lines):
                text.append("\n     ")
                text.append(
                    ("└ " if position == len(lines) - 1 else "├ "), style=palette.track
                )
                text.append(line, style=palette.muted)
        target.update(text)
        self._instance_groups = self._collect_running_instances()
        self._render_instances(palette)

    @staticmethod
    def _collect_running_instances() -> list[tuple[str, str, str, str]]:
        """``(name, status, project, session id)`` per live session.

        PER SESSION, not a count. They all spend the SAME active account, so a
        reader deciding whether to arm the engine wants to know WHICH of them
        would move — and, since one session on a model pins that model's
        window for the whole fleet, which one is responsible.

        ``status`` comes from Claude Code's own session records rather than
        from token traffic: it is the authoritative answer to "is this one
        working", where recent spend is only an inference.

        Split from the drawing because process inspection is far too expensive
        to repeat at animation rate, while the spinner beside each row moves at
        it.
        """
        try:
            from claude_swap.printer import abbreviate_path
            from claude_swap.process_detection import get_running_instances

            sessions, _ides = get_running_instances()
        except Exception:
            return []  # process inspection is best-effort; never break the view
        titles = _session_titles()
        rows: list[tuple[str, str, str, str]] = []
        for session in sessions:
            sid = getattr(session, "session_id", "") or ""
            rows.append(
                (
                    titles.get(sid) or (sid[:8] if sid else "session"),
                    str(getattr(session, "status", "") or ""),
                    abbreviate_path(str(session.cwd)),
                    sid,
                )
            )
        # Working first, then by name: the row that explains a pinned model
        # window belongs at the top, not wherever the OS happened to list it.
        rows.sort(key=lambda row: (row[1] != "busy", row[0].lower()))
        return rows

    def _render_instances(self, palette: Palette) -> None:
        """Which sessions share the account, by name, and which are working."""
        rows = getattr(self, "_instance_groups", None)
        if not rows:
            return
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(
            f"Sessions ({len(rows)} on this account):",
            style=f"bold {palette.foreground}",
        )
        width = max(len(name) for name, _s, _p, _i in rows)
        for name, status, project, _sid in rows:
            working = status == "busy"
            text.append("\n  ")
            if working:
                # Animated only while there is something to animate: a spinner
                # that never stops says "busy" about an idle machine.
                text.append(
                    _SPINNER[(self._ticks // 2) % len(_SPINNER)] + " ",
                    style=palette.accent,
                )
            else:
                text.append("· ", style=palette.track)
            text.append(
                f"{name:<{width}}  ",
                style=palette.foreground if working else palette.muted,
            )
            text.append(
                f"{status:<7}",
                style=palette.accent if working else palette.track,
            )
            text.append(f"  {project}", style=palette.track)
        try:
            self.query_one("#fleet-instances", Static).update(text)
        except Exception:
            pass
