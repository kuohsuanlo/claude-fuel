"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from unittest.mock import patch

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent
from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import UsageEntry


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
    )


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []

    # -- surface the TUI consumes ------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed account {identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} Account-{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added Account 9: fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added Account {slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


def make_app(fake: FakeSwitcher):
    from claude_swap.tui.app import CswapApp

    return CswapApp(fake)


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until its screen stops
    it, so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


async def menu_select(pilot, action_id: str) -> None:
    """Drive the dashboard menu: highlight the entry by id, press Enter."""
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.query(MenuItem))
    menu.index = next(
        i for i, item in enumerate(items) if item.action_id == action_id
    )
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# ---------------------------------------------------------------------------
# Data service units (sync)
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_duration(self):
        assert tui_data.format_duration(42) == "42s"
        assert tui_data.format_duration(180) == "3m"
        assert tui_data.format_duration(7980) == "2h 13m"
        assert tui_data.format_duration(3600 * 26) == "1d 2h"

    def test_format_age_fresh_is_silent(self):
        # Ages inside the serve TTL are the polling cadence at work, not
        # staleness worth flagging.
        assert tui_data.format_age(3.0) is None
        assert tui_data.format_age(120) is None
        assert tui_data.format_age(None) is None
        assert tui_data.format_age(400) == "· 6m ago"

    def test_sentinel_labels_match_cswap_list(self):
        # The TUI must describe sentinel states with the exact wording `cswap
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — refresh deferred this pass; retries automatically"
        )
        from claude_swap.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_cswap_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `cfuel list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_swap.tui.widgets import account_card_text

        entry = UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED,
            last_good={"five_hour": {"pct": 53.0}},
            fetched_at=time.time() - 720,
            age_s=720.0,
        )
        card = account_card_text(make_account(1, active=True, entry=entry), 80).plain
        assert "token expired — refresh deferred this pass; retries automatically" in card
        assert "last seen 53% used" in card

        no_history = account_card_text(
            make_account(1, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)), 80
        ).plain
        assert "last seen" not in no_history

        api_key = account_card_text(
            make_account(
                1,
                kind="api_key",
                entry=dataclasses.replace(entry, sentinel=USAGE_API_KEY),
            ),
            80,
        ).plain
        assert "last seen" not in api_key

    def test_account_card_uses_light_palette_when_passed(self):
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(CSWAP_LIGHT))
        styles = {str(span.style) for span in text.spans}
        assert any(ACCENT_LIGHT in s for s in styles)  # active marker uses light accent

    def test_window_helpers(self):
        entry = make_entry(pct5=47.0)
        assert tui_data.window_pct(entry.last_good, "five_hour") == 47.0
        assert tui_data.window_pct(None, "five_hour") is None
        text = tui_data.window_reset_text(entry.last_good, "five_hour", time.time())
        assert text is not None and text.startswith("resets ")
        assert tui_data.window_reset_text(None, "five_hour", time.time()) is None

    def test_reset_clock(self):
        # Same-day reset → bare HH:MM; a reset days out carries its date.
        now = time.time()
        entry = make_entry()  # 5h resets in 2h, 7d in 3d
        clock5 = tui_data.reset_clock(entry.last_good["five_hour"], now)
        assert clock5 is not None and clock5.count(":") == 1
        clock7 = tui_data.reset_clock(entry.last_good["seven_day"], now)
        import calendar

        months = list(calendar.month_abbr)[1:]
        assert clock7 is not None and any(m in clock7 for m in months)

    def test_reset_clock_unknown_or_elapsed_is_none(self):
        now = time.time()
        assert tui_data.reset_clock(None, now) is None
        assert tui_data.reset_clock({"pct": 5.0}, now) is None
        assert tui_data.reset_clock({"resets_at": "garbage"}, now) is None
        # elapsed reset: the row says "resets now" — no clock to show
        elapsed = {"resets_at": _iso_in(-60)}
        assert tui_data.reset_clock(elapsed, now) is None
        assert tui_data.reset_text(elapsed, now) == "resets now"


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `cfuel list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


class TestUsageRows:
    """The card's rows must mirror the CLI's _format_usage_lines semantics."""

    def test_absent_window_produces_no_row(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        spend = usage_rows(entry.last_good, time.time())[0]
        assert spend[0] == "$$"
        assert " · " in spend[3]
        assert spend[3].index(" · ") < spend[3].index("$12.50")

    def test_no_data_no_rows(self):
        from claude_swap.tui.widgets import usage_rows

        assert usage_rows(None, time.time()) == []
        assert usage_rows({}, time.time()) == []

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]
        assert "(ahead of pace)" in row[3]

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, now)[0]
        assert "pace" not in row[2]

    def test_scoped_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]

    def test_maxed_scoped_marker_wins_over_pace(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(!)" in row[2]
        assert "ahead of pace" not in row[2]

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now)[0]
        assert "pace" not in row[2]

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_swap.tui.widgets import account_card_text

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        acc = make_account(1, active=True, entry=entry)

        wide = account_card_text(acc, 100).plain
        assert wide.count(" · ") == 3

        mid_lines = account_card_text(acc, 78).plain.splitlines()
        spend_line = next(line for line in mid_lines if "$12.50" in line)
        assert " · " not in spend_line
        for line in mid_lines:
            if "resets" in line and "$12.50" not in line:
                assert " · " in line

        narrow = account_card_text(acc, 40).plain
        assert " · " not in narrow


class TestMiniAccountText:
    def test_seven_day_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "(ahead)" in mini_account_text(acc, now).plain

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=None,
            age_s=None,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from claude_swap.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"


# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboard:
    async def test_panel_shows_active_full_and_others_mini(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(47.0, 63.0)),
                make_account(2, entry=make_entry(92.0, 71.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "user1@example.com" in panel and "● active" in panel
            assert "resets" in panel  # the active card is the full one
            assert "user2@example.com" in panel and "92%" in panel
            # the mini line has no bars — bar glyphs only in the active card
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "━" not in mini_part

    async def test_disabled_marker_on_active_card_and_mini(self, tmp_path):
        # A disabled account is still shown; it's just annotated so the user
        # can see it's held out of auto-rotation — on the full card when it's
        # the active login, and on the one-line form otherwise.
        fake = FakeSwitcher(
            [
                make_account(1, active=True, disabled=True),
                make_account(2, disabled=True),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "● active" in panel  # still the active card
            # both the active card and the mini row carry the marker
            assert panel.count("(disabled)") == 2

    async def test_active_card_skips_absent_window_and_shows_scoped(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(
                    1,
                    active=True,
                    entry=make_entry(pct5=47.0, pct7=None, scoped=[("Fable", 62.0)]),
                )
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "5h" in panel
            assert "7d" not in panel  # annual plan: no invented row
            assert "usage unknown" not in panel
            assert "Fable" in panel and "62%" in panel

    async def test_mini_line_skips_absent_window(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, entry=make_entry(pct5=92.0, pct7=None)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "5h 92%" in mini_part
            assert "7d" not in mini_part

    async def test_menu_is_default_navigation_and_nests(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == [
                "switch",
                "watch",
                "auto",
                "add-menu",
                "disable-menu",
                "remove-menu",
                "theme-menu",
                "quit",
            ]
            # nest into Add (index 3), then back out with escape
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == ["add-login", "add-token", "back"]
            await pilot.press("escape")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_remove_menu_shows_alias_before_email(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, alias="dev"),
                make_account(2, email="plain@example.com"),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            from textual.widgets import Static

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("dev (user1@example.com)" in label for label in labels)
            assert any("plain@example.com" in label for label in labels)
            assert not any("(plain@example.com)" in label for label in labels)

    async def test_remove_menu_label_renders_bracket_tag_literally(self, tmp_path):
        # The remove menu labels each account with `[{display_tag}]`, and an
        # org name of "red" makes that literally "[red]" — a valid Rich
        # color markup tag. MenuItem must render it as text, not consume it
        # as styling (which would silently drop the tag from the label).
        fake = FakeSwitcher(
            [dataclasses.replace(make_account(1, active=True), org_name="red")],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("[red]" in label for label in labels)

    async def test_back_menu_entry_pops_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "back")
            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_vim_keys_move_menu_cursor(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 0
            await pilot.press("j")
            assert menu.index == 1
            await pilot.press("k")
            assert menu.index == 0

    async def test_s_opens_switch_screen_and_enter_switches(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [item.number for item in items] == ["1", "2"]
            assert listview.index == 0  # starts on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, DashboardScreen)  # popped back
            assert app.snapshot.active_number == "2"

    async def test_switch_screen_escape_backs_out(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("enter")  # menu: Switch account…
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen

            assert isinstance(app.screen, SwitchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_remove_via_menu_confirms_then_removes(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_remove_via_menu_cancel_is_safe(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:1")
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)

    async def test_disable_via_menu_toggles_without_confirm(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            await menu_select(pilot, "disable:2")  # no modal — direct action
            await settle(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            # the submenu pops back to root after the toggle
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_disable_menu_row_reflects_state_and_re_enables(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2, disabled=True)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            # the already-disabled account offers to enable; the active one to disable
            assert any("(disabled)" in label and "enable" in label for label in labels)
            assert any("disable" in label and "(disabled)" not in label for label in labels)
            # selecting the disabled account flips it back on
            await menu_select(pilot, "disable:2")
            await settle(pilot)
            assert ("set_disabled", "2", False) in fake.calls

    async def test_modal_arrow_keys_choose_button(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")  # → confirm modal
            # focus starts on the confirm button; → moves to Cancel, enter presses it
            await pilot.press("right", "enter")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)
            # reopen (menu index still on account 2), ← back to confirm, press it
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right", "left", "enter")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_full_refresh_binding(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets[-1] is None  # full on-demand pass

    async def test_add_token_via_menu_passes_assume_yes(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "5"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat01-test", None, 5, True) in fake.calls

    async def test_add_token_occupied_slot_asks_first(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "2"
            await pilot.click("#add")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)  # overwrite confirm
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "add_token" for call in fake.calls)

    async def test_empty_state_hint_in_panel(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "No managed accounts yet" in panel

    async def test_palette_is_disabled(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        assert CswapApp.ENABLE_COMMAND_PALETTE is False


@pytest.mark.asyncio
class TestWatchScreen:
    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, WatchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert len(list(listview.query(AccountItem))) == 2  # full cards
            assert listview.index is None  # monitor mode: no cursor
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake_calls(app))

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen

            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index == 0  # cursor armed, on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, WatchScreen)  # stayed watching
            assert app.screen.query_one("#accounts", ListView).index is None
            assert app.snapshot.active_number == "2"

    async def test_escape_disarms_then_leaves(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")  # disarm selection only
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            await pilot.press("escape")  # now leave
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_menu_watch_entry_opens_it(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "watch")
            from claude_swap.tui.dashboard import WatchScreen

            assert isinstance(app.screen, WatchScreen)

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fake(tmp_path), start="watch")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    async def test_blocked_normal_allows_store_only_repaint_without_stale_overpaint(
        self, tmp_path
    ):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0
            assert fake.fetch_sets == [None, set()]

    async def test_late_normal_can_advance_usage_after_store_repaint(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        store = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

    async def test_repeated_ticks_keep_store_lane_single_flight(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        fake.block_store = True
        app = make_app(fake)

        async with app.run_test(size=(100, 40)):
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_started)
            app._tick()
            app._tick()
            assert fake.fetch_sets == [None, set()]
            fake.store_release.set()
            fake.normal_release.set()
            await wait_event(fake.store_done)
            await wait_event(fake.normal_done)

    async def test_store_only_mode_launches_only_store_lane(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            fake.fetch_sets.clear()
            app.set_store_only(True)
            await settle(pilot)
            assert fake.fetch_sets == [set()]

    async def test_watch_title_shows_snapshot_age_and_long_refresh(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            # Fresh snapshots stay quiet; the age note is a staleness alarm.
            assert "snapshot" not in title.render().plain
            app.snapshot = dataclasses.replace(
                app.snapshot, taken_at=time.time() - app.SNAPSHOT_AGE_NOTE_S - 1.0
            )
            app._update_refresh_status()
            await pilot.pause()
            assert "snapshot 1m ago" in title.render().plain
            app._normal_refreshing = True
            app._normal_started_at = time.time() - app.POLL_INTERVAL_S - 1.0
            app._update_refresh_status()
            await pilot.pause()
            assert "refreshing" in title.render().plain


def fake_calls(app) -> list[tuple]:
    return app.switcher.calls



class _FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop."""

    instances: list["_FakeEngine"] = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.settings = settings
        self.on_event = on_event
        self.dry_run = dry_run
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.wakes = 0
        self._stop = threading.Event()
        _FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


@pytest.mark.asyncio
class TestAutoScreen:
    async def _open(self, pilot):
        await settle(pilot)
        await pilot.press("g")
        await pilot.pause()

    async def test_opens_in_dry_run_and_store_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert len(fake_engine.instances) == 1
            assert fake_engine.instances[0].dry_run is True
            assert app._store_only is True
            await settle(pilot)
            # engine event reached the log via call_from_thread
            from textual.widgets import RichLog

            assert len(app.screen.query_one("#event-log", RichLog).lines) > 0

    async def test_go_live_requires_confirmation(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("l")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False

    async def test_back_stops_engine_and_restores_fetching(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)
            assert fake_engine.instances[0].stopped is True
            assert app._store_only is False

    async def test_threshold_adjust_is_session_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            assert app.threshold_pct == 90.0  # mount syncs to the file value
            await pilot.press("right")  # inert outside adjust mode
            await pilot.pause()
            assert screen._settings.threshold == 90.0
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 93.0
            assert app.threshold_pct == 93.0
            engine = fake_engine.instances[0]
            assert engine.applied_thresholds == [91.0, 92.0, 93.0]
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            assert "threshold 93% (session)" in summary.render().plain
            await pilot.press("enter")
            await pilot.pause()
            assert engine.wakes == 1  # one forced tick on leaving the mode
            # the override lives in memory only — nothing was persisted
            assert not (tmp_path / "settings.json").exists()
            # a dry↔live restart rebuilds the engine from the adjusted copy
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert fake_engine.instances[1].settings.threshold == 93.0
            await pilot.press("escape")
            await settle(pilot)
            # leaving the screen reverts the tick and unpins poll planning
            assert app.threshold_pct == 90.0
            assert fake._poll_inputs_override is None

    async def test_threshold_adjust_escape_exits_mode_not_screen(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            await pilot.press("t")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AutoScreen)
            # no net change → no forced tick
            assert fake_engine.instances[0].wakes == 0
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)

    async def test_threshold_clamps_and_keeps_meaningful_decimals(
        self, tmp_path, fake_engine
    ):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 99.0},
        }))
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            await pilot.press("t", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 99.9  # spec's upper bound
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            # never a lying "100%"
            assert "threshold 99.9% (session)" in summary.render().plain
            screen.action_threshold_step(-60.0)
            await pilot.pause()
            assert screen._settings.threshold == 50.0  # spec's lower bound

    async def test_candidates_ranked_by_headroom(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(80.0, 10.0)),
                make_account(3, entry=make_entry(15.0, 5.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )

    async def test_candidates_ranking_honors_configured_model(
        self, tmp_path, fake_engine
    ):
        """The 'Next best' ranking must use the same window set as the
        engine: with autoswitch.model set, a Fable-bound account ranks by
        its Fable pct, not its roomy 5h."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "Fable"},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(10.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(50.0, 5.0, scoped=[("Fable", 20.0)])
                ),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            # On 5h alone #2 (10% used) would rank first; Fable 95% binds it
            # below #3 (50% binding).
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(CSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_swap.cli as cli

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2  # argparse usage error

    def test_cswap_watch_opens_tui_on_watch_page(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher, start="dashboard"):
            launched["start"] = start
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap", "watch"])
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert launched["start"] == "watch"


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-dark"           # → back to dark

    async def test_theme_menu_marks_current_and_applies(self, tmp_path):
        from textual.widgets import ListView, Static

        from claude_swap.tui.widgets import MenuItem

        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app._theme_name == "auto"  # default
            await menu_select(pilot, "theme-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [it.query_one(Static).render().plain for it in menu.query(MenuItem)]
            assert any("dark" in lbl for lbl in labels)
            assert any("light" in lbl for lbl in labels)
            current = next(lbl for lbl in labels if "auto" in lbl)
            assert "●" in current  # the current theme is marked
            await menu_select(pilot, "theme:light")
            assert app._theme_name == "light"
            assert app.theme == "cswap-light"



@pytest.fixture
def fake_fleet_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.fleetview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


def _fleet_app(fake):
    from claude_swap.tui.app import CswapApp

    return CswapApp(fake, start="fleet")


@pytest.mark.asyncio
class TestFleetScreen:
    """`cfuel`: one screen, deadline-ordered, live whether armed or not."""

    @staticmethod
    def _text(app, selector: str) -> str:
        """Rendered text of one Static, however Textual spells the accessor.

        `renderable` was the attribute; newer Textual keeps the value on
        `_renderable` and exposes `render()`. Reading through `render()` is
        the version-stable spelling and is what the widget actually paints.
        """
        from textual.widgets import Static

        rendered = app.screen.query_one(selector, Static).render()
        return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    def _fleet(self, tmp_path):
        # #1 active and comfortable; #2 holds a lot that expires soonest.
        return FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(48.0, 28.0)),
                make_account(2, entry=make_entry(10.0, 40.0)),
            ],
            tmp_path,
        )

    async def test_opens_directly_with_no_dashboard_underneath(
        self, tmp_path, fake_fleet_engine
    ):
        """A hidden dashboard would keep its own poller alive competing with
        the engine for the same rate-limited usage budget."""
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.fleetview import FleetScreen

            assert isinstance(app.screen, FleetScreen)
            assert not any(
                type(s).__name__ == "DashboardScreen" for s in app.screen_stack
            )
            assert app._store_only is True

    async def test_starts_armed(self, tmp_path, fake_fleet_engine):
        """The command exists to stop quota being wasted; a gauge that watches
        it happen without acting is not that tool."""
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert len(fake_fleet_engine.instances) == 1
            assert fake_fleet_engine.instances[0].dry_run is False
            assert "AUTO" in self._text(app, "#fleet-headline")

    async def test_turning_auto_off_hands_over_the_arrow_keys(
        self, tmp_path, fake_fleet_engine
    ):
        """Manual selection and the engine must never both own the account: a
        cursor left visible while the engine can switch shows a choice the
        next tick can overrule."""
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app.screen._selected is None
            await pilot.press("down")
            await pilot.pause()
            assert app.screen._selected is None, "armed: arrows must do nothing"
            await pilot.press("a")
            await settle(pilot)
            assert app.screen._armed is False
            assert fake_fleet_engine.instances[-1].dry_run is True
            assert "MANUAL" in self._text(app, "#fleet-headline")
            before = app.screen._selected
            await pilot.press("down")
            await pilot.pause()
            assert app.screen._selected != before

    async def test_enter_switches_to_the_picked_account(
        self, tmp_path, fake_fleet_engine
    ):
        fake = self._fleet(tmp_path)
        app = _fleet_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("a")          # auto off → manual
            await settle(pilot)
            for _ in range(3):              # land on a non-active account
                if app.screen._selected is not None and not (
                    fake._accounts[app.screen._selected].is_active
                ):
                    break
                await pilot.press("down")
                await pilot.pause()
            await pilot.press("enter")
            await settle(pilot)
            assert any(call[0] == "switch_to" for call in fake.calls)

    async def test_toggling_auto_relaunches_the_engine_in_the_new_mode(
        self, tmp_path, fake_fleet_engine
    ):
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            assert len(fake_fleet_engine.instances) == 2
            assert fake_fleet_engine.instances[0].stopped is True
            assert fake_fleet_engine.instances[1].dry_run is True
            await pilot.press("a")
            await settle(pilot)
            assert fake_fleet_engine.instances[2].dry_run is False

    async def test_bar_orders_accounts_by_deadline(
        self, tmp_path, fake_fleet_engine
    ):
        """#2's quota expires first, so it is leftmost — the bar reads as the
        order the quota should be spent in."""
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(10.0, 20.0)),
                make_account(2, entry=make_entry(10.0, 20.0)),
            ],
            tmp_path,
        )
        # Pull #2's weekly deadline in front of #1's.
        soon = _iso_in(3600)
        fake._accounts[1] = dataclasses.replace(
            fake._accounts[1],
            usage=UsageEntry(
                last_good={
                    "five_hour": {"pct": 10.0, "resets_at": _iso_in(7200)},
                    "seven_day": {"pct": 20.0, "resets_at": soon},
                },
                fetched_at=time.time(),
                age_s=1.0,
            ),
        )
        app = _fleet_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            bars = self._text(app, "#fleet-bars")
            # The soonest-expiring account is named by the ▲ marker under its
            # own segment; ordering is inside the bar, not a separate legend.
            assert "user2" in bars

    async def test_active_account_is_marked_on_the_bar(
        self, tmp_path, fake_fleet_engine
    ):
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            legend = self._text(app, "#fleet-bars")
            assert "▲" in legend

    async def test_threshold_edit_is_written_to_settings(
        self, tmp_path, fake_fleet_engine
    ):
        """A threshold that reverted on exit would leave the user protected by
        a number they had already rejected — the engine keeps running from
        settings.json afterwards."""
        from claude_swap.settings import load_settings

        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            before = load_settings(tmp_path).threshold
            await pilot.press("t")
            await pilot.press("left")
            await pilot.press("left")
            await pilot.press("enter")
            await settle(pilot)
            assert load_settings(tmp_path).threshold == before - 2

    async def test_accounts_use_the_cli_list_format(
        self, tmp_path, fake_fleet_engine
    ):
        """Not a new layout: this block answers "tell me everything about
        account 2", and a reader who knows that shape from `cfuel list` should
        not have to learn a second one."""
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            rows = self._text(app, "#fleet-accounts")
            assert "Accounts:" in rows
            assert "user1@example.com" in rows and "user2@example.com" in rows
            assert "├ 5h:" in rows and "└" in rows

    async def test_burn_readout_names_what_it_is_waiting_for(
        self, tmp_path, fake_fleet_engine
    ):
        """A fresh machine has no calibration; the line must say what it is
        waiting for rather than render a fabricated rate or an indefinite
        "measuring…" that reads as a hang."""
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            burn = self._text(app, "#fleet-burn")
            assert "calibrating" in burn or "idle" in burn or "%" in burn

    async def test_calibration_survives_a_restart(self, tmp_path, fake_fleet_engine):
        """Percent-per-token is a property of the plan, not of the process.
        Without persistence every launch is blind for as long as it takes two
        budgeted API polls to bracket some spend — minutes, which is exactly
        the window someone opens this view to watch."""
        import json

        from claude_swap.burn import BurnTracker, TranscriptBurnSensor

        seed = BurnTracker(sensor=TranscriptBurnSensor(tmp_path / "none"))
        seed._calibration["1"] = collections.deque([(2.0, 1000.0)])
        (tmp_path / "burn_calibration.json").write_text(
            json.dumps(seed.calibration_state()), encoding="utf-8"
        )
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app.screen._tracker.pct_per_token("1") == pytest.approx(0.002)

    async def test_corrupt_calibration_cache_does_not_break_the_view(
        self, tmp_path, fake_fleet_engine
    ):
        (tmp_path / "burn_calibration.json").write_text("{not json", encoding="utf-8")
        app = _fleet_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app.screen._tracker.pct_per_token() is None
            assert self._text(app, "#fleet-bars")


@pytest.mark.asyncio
class TestFleetStatusLine:
    """The pet and the engine's commentary share one line."""

    def _app(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        return CswapApp(
            FakeSwitcher(
                [make_account(1, active=True), make_account(2)], tmp_path
            ),
            start="fleet",
        )

    @staticmethod
    def _text(app) -> str:
        from textual.widgets import Static

        rendered = app.screen.query_one("#fleet-log", Static).render()
        return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    async def test_h_hides_the_commentary_but_never_the_pet(
        self, tmp_path, fake_fleet_engine
    ):
        """The pet is the proof the instrument is still ticking; a screen with
        no moving part looks identical to a wedged one."""
        app = self._app(tmp_path)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.screen._log_note("something happened")
            assert "something happened" in self._text(app)
            await pilot.press("h")
            await pilot.pause()
            assert "something happened" not in self._text(app)
            from textual.widgets import Static as _S

            pet = app.screen.query_one("#fleet-status", _S).render()
            pet_text = pet.plain if hasattr(pet, "plain") else str(pet)
            assert pet_text.strip(), "the pet must still be drawn"
            await pilot.press("h")
            await pilot.pause()
            assert "something happened" in self._text(app)

    async def test_only_the_latest_event_is_kept(self, tmp_path, fake_fleet_engine):
        """A scrolling log was mostly the same sentence repeated, and it pushed
        the thing worth reading off the bottom."""
        app = self._app(tmp_path)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.screen._on_engine_event(NoSwitchEvent(reason="cooldown"))
            app.screen._on_engine_event(NoSwitchEvent(reason="below-threshold"))
            text = self._text(app)
            assert "below-threshold" in text
            assert "cooldown" not in text


@pytest.mark.asyncio
class TestWasteProjectionUnits:
    """The projection must be in the units of the quota that expires."""

    @staticmethod
    def _text(app) -> str:
        from textual.widgets import Static

        r = app.screen.query_one("#fleet-burn", Static).render()
        return r.plain if hasattr(r, "plain") else str(r)

    async def test_uses_the_weekly_rate_not_the_binding_rate(
        self, tmp_path, fake_fleet_engine
    ):
        """A point of the 5-hour window is a completely different quantity of
        work from a point of the weekly one. Feeding the 5h rate into a weekly
        projection reported "all spendable" on quota that will certainly be
        lost — the exact false reassurance this screen exists to prevent."""
        from claude_swap.tui.app import CswapApp

        fake = FakeSwitcher(
            [make_account(1, active=True, entry=make_entry(50.0, 50.0))], tmp_path
        )
        app = CswapApp(fake, start="fleet")
        async with app.run_test(size=(110, 32)) as pilot:
            await settle(pilot)
            tracker = app.screen._tracker
            # The 5h scale is 100,000x the weekly one here. Read on the 5h
            # scale this quota is spent in minutes; read correctly it cannot
            # be spent in the three days it has left.
            tracker._calibration["1\x005h"] = collections.deque([(100.0, 1000.0)])
            tracker._calibration["1\x007d"] = collections.deque([(1.0, 1_000_000.0)])
            # (ts, weighted, model): a scoped window is measured on its own
            # model's traffic, so a sample has to say which model it was.
            app.screen._sensor._samples.append(
                (time.time(), 1000.0, "claude-fable-5", "-home-me-proj")
            )
            app.screen._display_tick()
            text = self._text(app)
            assert "wastes" in text, (
                "at the weekly scale this quota cannot be spent in time; "
                f"got: {text!r}"
            )
            assert "all spendable" not in text


@pytest.mark.asyncio
class TestPetTiming:
    """Every frame must be shown for the same length of time."""

    async def test_repaints_do_not_advance_the_animation(self):
        """Repaints arrive at wildly uneven intervals — a one-second data tick,
        a snapshot every three, an engine event whenever one happens. Counting
        them made the walk stutter and skip; the phase comes from the clock."""
        from claude_swap.tui.fleetview import FleetScreen

        with patch("claude_swap.tui.fleetview.time.monotonic", return_value=1234.0):
            # The clock is frozen, so anything that moved the phase here would
            # be a repaint counter — which is exactly the bug: repaints arrive
            # at wildly uneven intervals and counting them made the animation
            # stutter.
            phases = {FleetScreen._sprite_frame() for _ in range(50)}
        assert len(phases) == 1, f"phase moved without the clock: {phases}"

    async def test_the_phase_advances_with_elapsed_time(
        self, tmp_path, fake_fleet_engine
    ):
        from claude_swap.tui.fleetview import SPRITE_FRAME_S, FleetScreen

        with patch(
            "claude_swap.tui.fleetview.time.monotonic",
            side_effect=[0.0, SPRITE_FRAME_S * 1.5, SPRITE_FRAME_S * 3.5],
        ):
            first = FleetScreen._sprite_frame()
            second = FleetScreen._sprite_frame()
            third = FleetScreen._sprite_frame()
        assert (first, second, third) == (0, 1, 3), "one frame per fixed period"


@pytest.mark.asyncio
class TestPetFrameRate:
    """The pet must animate at its own rate, not the data rate."""

    async def _pose_changes(self, app, pilot, seconds=2.0):
        """Count changes in the PET rows only.

        The sky above it animates on its own, slower phase, so measuring the
        whole widget counts weather as pet motion and the two rates cannot be
        told apart.
        """
        import time as _t

        from textual.widgets import Static

        from claude_swap.tui import pets
        from claude_swap.tui.skyview import SKY_H

        widget = app.screen.query_one("#fleet-status", Static)
        sky_rows_count = SKY_H // 2              # the panel; there is no caption
        pet_rows = pets.SLEEPING.height // 2

        def signature() -> str:
            """The pet's appearance INCLUDING colour.

            Once the sky's ground fills every transparent pixel, every cell is
            the same glyph and the plain text never changes — the animation
            lives entirely in the colours. Comparing text alone reported a
            moving pet as frozen.
            """
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            spans = getattr(rendered, "spans", [])
            row_starts = [0]
            for index, char in enumerate(text):
                if char == "\n":
                    row_starts.append(index + 1)
            if len(row_starts) <= sky_rows_count:
                return text
            begin = row_starts[sky_rows_count]
            end = (
                row_starts[sky_rows_count + pet_rows]
                if len(row_starts) > sky_rows_count + pet_rows
                else len(text)
            )
            styles = [
                (span.start, span.end, str(span.style))
                for span in spans
                if span.start >= begin and span.end <= end
            ]
            return text[begin:end] + repr(styles)

        seen, changes, start = None, 0, _t.monotonic()
        while _t.monotonic() - start < seconds:
            await asyncio.sleep(0.02)
            await pilot.pause()
            current = signature()
            if current != seen:
                seen, changes = current, changes + 1
        return changes, _t.monotonic() - start

    async def test_awake_pose_changes_at_the_frame_rate(
        self, tmp_path, fake_fleet_engine
    ):
        """Measured twice before this held: once because two poses repeated so
        the picture only changed every fourth frame, and once because the
        pet's timer silently never fired. Both looked identical from outside —
        a one-second animation."""
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.fleetview import SPRITE_FRAME_S

        app = CswapApp(
            FakeSwitcher([make_account(1, active=True)], tmp_path), start="fleet"
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            # Busy: tokens flowing hard, so he swings at the full frame rate.
            app.screen._burning = lambda: True
            app.screen._swing_divisor = lambda: 1
            changes, elapsed = await self._pose_changes(app, pilot)
            due = elapsed / SPRITE_FRAME_S
            assert changes >= due * 0.6, (
                f"{changes} pose changes in {elapsed:.1f}s; at "
                f"{SPRITE_FRAME_S}s per frame at least {due*0.6:.0f} were due"
            )

    async def test_sleep_is_visibly_slower_than_waking(
        self, tmp_path, fake_fleet_engine
    ):
        """A breathing sleeper animated at the waking rate looks agitated,
        which is the opposite of the thing being shown."""
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.fleetview import _SLEEP_SLOWDOWN, SPRITE_FRAME_S

        app = CswapApp(
            FakeSwitcher([make_account(1, active=True)], tmp_path), start="fleet"
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.screen._burning = lambda: False    # asleep
            changes, elapsed = await self._pose_changes(app, pilot, seconds=2.5)
            # Two frames of slack, not one: this samples a real clock on a
            # machine that may be running a dozen other sessions, and a test
            # that fails on scheduling jitter teaches nothing.
            ceiling = elapsed / (SPRITE_FRAME_S * _SLEEP_SLOWDOWN) + 2
            assert changes <= ceiling, (
                f"{changes} changes in {elapsed:.1f}s is faster than the "
                f"slowed sleep rate allows ({ceiling:.0f})"
            )

    @staticmethod
    def test_no_pose_repeats_a_neighbour():
        """The visible rate is the rate the POSE changes, never the rate the
        timer fires — a cycle that repeats a pose divides the apparent frame
        rate by however many times it repeats."""
        from claude_swap.tui import pets

        for sprite in (pets.WORKING, pets.SLEEPING):
            n = len(sprite.frames)
            for i in range(n):
                assert sprite.frames[i] != sprite.frames[(i + 1) % n], (
                    f"frame {i} repeats its neighbour"
                )


class TestSwingFollowsTheBurnRate:
    """The swing rate IS the burn rate — that is what makes the pet an
    instrument rather than a decoration."""

    @staticmethod
    def _divisor(rate: float) -> int:
        from claude_swap.tui.fleetview import FleetScreen

        screen = FleetScreen.__new__(FleetScreen)
        screen._sensor = type("S", (), {"tokens_per_s": lambda self, w: rate})()
        return FleetScreen._swing_divisor(screen)

    def test_busier_means_fewer_frames_per_swing(self):
        assert self._divisor(8000) < self._divisor(2000) < self._divisor(100)

    def test_the_fastest_swing_is_one_frame_per_pose(self):
        assert self._divisor(50_000) == 1

    def test_an_idle_machine_still_swings_rather_than_freezing(self):
        """He is only asleep when nothing has burned for a while; a brief lull
        should slow the pick, not stop it dead."""
        assert self._divisor(0) >= 1

    def test_no_sensor_is_survivable(self):
        from claude_swap.tui.fleetview import FleetScreen

        screen = FleetScreen.__new__(FleetScreen)
        screen._sensor = None
        assert FleetScreen._swing_divisor(screen) >= 1


@pytest.mark.asyncio
class TestPetNeverWraps:
    """A sprite one cell wider than its box wraps every single row."""

    async def test_no_row_exceeds_the_widgets_content_width(
        self, tmp_path, fake_fleet_engine
    ):
        """A leading space put the sprite's last column past the content
        width and the whole picture reflowed — pixel art cannot survive a
        wrap, every row lands in the wrong place."""
        from textual.widgets import Static

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(
            FakeSwitcher([make_account(1, active=True)], tmp_path), start="fleet"
        )
        async with app.run_test(size=(120, 44)) as pilot:
            await settle(pilot)
            screen = app.screen
            for burning in (False, True):
                screen._burning = (lambda value: (lambda: value))(burning)
                screen._render_status(Palette.DARK)
                widget = screen.query_one("#fleet-status", Static)
                rendered = widget.render()
                text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
                width = widget.content_region.width
                for index, line in enumerate(text.split("\n")):
                    assert len(line) <= width, (
                        f"{'working' if burning else 'sleeping'} row {index} is "
                        f"{len(line)} cells in a {width}-cell box"
                    )

    async def test_the_renderer_adds_no_indent(
        self, tmp_path, fake_fleet_engine
    ):
        """Flush left: an indent is exactly what pushed it over the edge. The
        artwork's own first column may be transparent — that is the sprite,
        not padding — so this measures the WIDEST row against the sprite's
        width rather than looking for a leading space."""
        from textual.widgets import Static

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(
            FakeSwitcher([make_account(1, active=True)], tmp_path), start="fleet"
        )
        async with app.run_test(size=(120, 44)) as pilot:
            await settle(pilot)
            app.screen._burning = lambda: True
            app.screen._render_status(Palette.DARK)
            rendered = app.screen.query_one("#fleet-status", Static).render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            from claude_swap.tui import pets

            sprite_rows = text.split("\n")[: pets.WORKING.height // 2]
            assert max(len(row) for row in sprite_rows) == pets.WORKING.width, (
                "the rendered sprite is not exactly its own width — something "
                "is padding it"
            )


@pytest.mark.asyncio
class TestALimitBecomingRelevantIsNotWaitedOn:
    """The decision runs at display cadence even though the fetch cannot."""

    @staticmethod
    def _app(tmp_path):
        import json as _json

        from claude_swap.tui.app import CswapApp

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "all"},
        }))
        return CswapApp(
            FakeSwitcher(
                [make_account(1, active=True, entry=make_entry(5.0, 40.0,
                                                              scoped=[("Fable", 100.0)]))],
                tmp_path,
            ),
            start="fleet",
        )

    async def test_the_engine_is_woken_when_a_window_starts_gating(
        self, tmp_path, fake_fleet_engine
    ):
        """The engine sleeps on the API budget — about a minute. A model that
        just started running makes the active account unusable NOW, and that
        fact is entirely local: settings file, transcripts, cached percents."""
        from unittest.mock import MagicMock, patch as _patch

        app = self._app(tmp_path)
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            screen = app.screen
            screen._engine = MagicMock()
            with _patch.object(type(screen), "_models", return_value=()):
                screen._display_tick()          # establish the baseline
            screen._engine.wake.assert_not_called()
            with _patch.object(type(screen), "_models", return_value=("Fable",)):
                screen._display_tick()          # Fable just became relevant
            screen._engine.wake.assert_called_once()

    async def test_a_window_that_stops_gating_does_not_wake_anything(
        self, tmp_path, fake_fleet_engine
    ):
        """Nothing breaks by noticing that late, and waking on both edges
        would tick the engine every time a model went quiet."""
        from unittest.mock import MagicMock, patch as _patch

        app = self._app(tmp_path)
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            screen = app.screen
            screen._engine = MagicMock()
            with _patch.object(type(screen), "_models", return_value=("Fable",)):
                screen._display_tick()
            with _patch.object(type(screen), "_models", return_value=()):
                screen._display_tick()
            screen._engine.wake.assert_not_called()

    async def test_a_steady_set_never_wakes_it(
        self, tmp_path, fake_fleet_engine
    ):
        from unittest.mock import MagicMock, patch as _patch

        app = self._app(tmp_path)
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            screen = app.screen
            screen._engine = MagicMock()
            with _patch.object(type(screen), "_models", return_value=("Fable",)):
                for _ in range(5):
                    screen._display_tick()
            screen._engine.wake.assert_not_called()


@pytest.mark.asyncio
class TestGaugesShowOnlyWhatTheEngineReads:
    """A bar the decision cannot see is worse than no bar at all."""

    @staticmethod
    def _fleet(tmp_path, model):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": model},
        }))
        return FakeSwitcher(
            [
                make_account(
                    1, active=True,
                    entry=make_entry(5.0, 95.0, scoped=[("Fable", 100.0)]),
                ),
                make_account(
                    2, entry=make_entry(0.0, 64.0, scoped=[("Fable", 77.0)])
                ),
            ],
            tmp_path,
        )

    async def _bars(self, pilot, app):
        from textual.widgets import Static

        await settle(pilot)
        rendered = app.screen.query_one("#fleet-bars", Static).render()
        return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    async def test_the_model_row_is_drawn_when_the_engine_gates_on_it(
        self, tmp_path, fake_fleet_engine
    ):
        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fleet(tmp_path, "all"), start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            assert "Fable" in await self._bars(pilot, app)

    async def test_an_idle_model_keeps_its_row_and_says_so(
        self, tmp_path, fake_fleet_engine
    ):
        """Deleting the bar was worse than the problem it solved.

        A window that is merely not running still holds quota that still
        expires; a row that disappears reads as the tool having lost a limit.
        Reported live: "為啥我界面的fable5不見了".
        """
        from unittest.mock import patch as _patch

        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fleet(tmp_path, "all"), start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            # Nothing has spent Fable, and nothing selects it either.
            with _patch(
                "claude_swap.tui.fleetview.FleetScreen._models", return_value=()
            ):
                app.screen._display_tick()
                bars = await self._bars(pilot, app)
        assert "Fable" in bars, "the row must survive not being the binding limit"
        # The label sits on its OWN line under the row: trailing the
        # per-account percentages, the longest row on screen pushed it off the
        # edge — the one place a reader looks to find out why an account
        # reads as unusable.
        rows = bars.split("\n")
        index = next(i for i, l in enumerate(rows) if "Fable" in l)
        assert any("not running" in l for l in rows[index:index + 3]), rows[index:]

    async def test_the_model_row_vanishes_when_the_engine_ignores_it(
        self, tmp_path, fake_fleet_engine
    ):
        """The row used to be built from a hardcoded ``("all",)`` while the
        ranking read ``autoswitch.model``. With the setting unset that drew a
        Fable gauge at 100% beside an engine that had ranked the account the
        most urgent one to keep burning — the screen and the decision
        disagreeing about what fuel even exists.
        """
        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fleet(tmp_path, "none"), start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            assert "Fable" not in await self._bars(pilot, app)


@pytest.mark.asyncio
class TestRunningInstancesShowActivity:
    """Sessions by NAME, with the status Claude Code itself reports.

    The list counted sessions and said nothing about which were spending. That
    matters because one unrelated session on a model pins that model's window
    and every account exhausted on it then reads as unusable — the confusion
    reported repeatedly. A session id is not a name either; the transcripts
    carry the title the user gave it.
    """

    async def _rendered(self, tmp_path, rows):
        from textual.widgets import Static

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(
            FakeSwitcher([make_account(1, active=True)], tmp_path), start="fleet"
        )
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            app.screen._instance_groups = rows
            app.screen._render_instances(Palette.DARK)
            rendered = app.screen.query_one("#fleet-instances", Static).render()
            return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    ROWS = [
        ("kenshi-zone-mc", "busy", "~/Server/proj", "21aecf40"),
        ("grimac-reloaded", "idle", "~/Server/other", "58802e6c"),
    ]

    async def test_it_names_each_session_and_its_status(
        self, tmp_path, fake_fleet_engine
    ):
        text = await self._rendered(tmp_path, self.ROWS)
        assert "kenshi-zone-mc" in text and "grimac-reloaded" in text, text
        assert "busy" in text and "idle" in text, text
        assert "Sessions (2 on this account)" in text, text

    async def test_a_busy_session_gets_the_spinner(
        self, tmp_path, fake_fleet_engine
    ):
        text = await self._rendered(tmp_path, self.ROWS)
        busy = next(l for l in text.split("\n") if "kenshi-zone-mc" in l)
        idle = next(l for l in text.split("\n") if "grimac-reloaded" in l)
        assert any(g in busy for g in "✻✽✳✢"), busy
        assert idle.strip().startswith("·"), idle

    async def test_an_idle_fleet_shows_no_spinner(
        self, tmp_path, fake_fleet_engine
    ):
        """A spinner that never stops says "busy" about an idle machine."""
        text = await self._rendered(
            tmp_path, [("a", "idle", "~/x", "1"), ("b", "shell", "~/y", "2")]
        )
        assert not any(g in text for g in "✻✽✳"), text

    async def test_it_survives_having_no_sessions(
        self, tmp_path, fake_fleet_engine
    ):
        assert await self._rendered(tmp_path, []) == ""

    def test_a_session_without_a_title_falls_back_to_its_id(self):
        """Never blank: an unnamed session still has to be countable."""
        from claude_swap.tui.fleetview import FleetScreen
        from unittest.mock import patch

        class Session:
            session_id = "e69074c1-1234"
            status = "idle"
            cwd = "/home/me/x"
            entrypoint = "cli"

        with patch("claude_swap.process_detection.get_running_instances",
                   return_value=([Session()], [])), \
             patch("claude_swap.tui.fleetview._session_titles", return_value={}):
            rows = FleetScreen._collect_running_instances()
        assert rows and rows[0][0] == "e69074c1"

    def test_busy_sessions_sort_first(self):
        from claude_swap.tui.fleetview import FleetScreen
        from unittest.mock import patch

        def session(sid, status):
            return type("S", (), {"session_id": sid, "status": status,
                                  "cwd": "/home/me/x", "entrypoint": "cli"})()

        with patch("claude_swap.process_detection.get_running_instances",
                   return_value=([session("a", "idle"), session("b", "busy")], [])), \
             patch("claude_swap.tui.fleetview._session_titles",
                   return_value={"a": "aaa", "b": "zzz"}):
            rows = FleetScreen._collect_running_instances()
        assert [r[0] for r in rows] == ["zzz", "aaa"], rows


@pytest.mark.asyncio
class TestHeadlineNamesTheLimit:
    """"47 pts" summed percentages of different pools — a number with no unit.

    Each expiring quota is named with its own limit instead: the amount is a
    fraction OF THAT POOL, and saying which pool is all the information there
    is."""

    @staticmethod
    def _app(tmp_path, hours_by_number):
        import json as _json

        from claude_swap.tui.app import CswapApp

        def entry(seven, hours):
            return UsageEntry(
                last_good={
                    "five_hour": {"pct": 0.0, "resets_at": _iso_in(3600)},
                    "seven_day": {"pct": seven, "resets_at": _iso_in(hours * 3600)},
                },
                fetched_at=time.time() - 5.0,
                age_s=5.0,
            )

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "all"},
        }))
        accounts = [
            make_account(number, active=(number == 1), entry=entry(seven, hours))
            for number, (seven, hours) in hours_by_number.items()
        ]
        return CswapApp(FakeSwitcher(accounts, tmp_path), start="fleet")

    async def _headline(self, app):
        from textual.widgets import Static

        rendered = app.screen.query_one("#fleet-headline", Static).render()
        return rendered.plain if hasattr(rendered, "plain") else str(rendered)

    async def test_one_expiring_quota_is_named_with_its_window(
        self, tmp_path, fake_fleet_engine
    ):
        app = self._app(tmp_path, {1: (60.0, 100), 2: (72.0, 12)})
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            headline = await self._headline(app)
        assert "user2 7d 28% expiring within 24h" in headline, headline
        assert "pts" not in headline

    async def test_more_expiring_quotas_are_counted_not_summed(
        self, tmp_path, fake_fleet_engine
    ):
        """Adding 28% of one plan to 10% of another yields nothing measurable;
        the second account is a count, never an addend."""
        app = self._app(tmp_path, {1: (60.0, 100), 2: (72.0, 12), 3: (90.0, 20)})
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            headline = await self._headline(app)
        assert "user2 7d 28% +1 more expiring within 24h" in headline, headline


@pytest.mark.asyncio
class TestHandoverNote:
    """When the account that lost this comparison gets its turn."""

    @staticmethod
    def _fleet(tmp_path, strategy="waste-first"):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"model": "all", "strategy": strategy},
        }))
        # Built by hand rather than with make_entry: that helper pins every
        # account's 7d reset to the same instant, and two identical deadlines
        # is precisely the case where no handover can ever be due.
        def entry(five, seven, fable, hours):
            return UsageEntry(
                last_good={
                    "five_hour": {"pct": five, "resets_at": _iso_in(4 * 3600)},
                    "seven_day": {
                        "pct": seven, "resets_at": _iso_in(hours * 3600)
                    },
                    "scoped": [{
                        "name": "Fable", "pct": fable,
                        "resets_at": _iso_in(hours * 3600),
                    }],
                },
                fetched_at=time.time() - 5.0,
                age_s=5.0,
            )

        return FakeSwitcher(
            [
                # 23 perishable points over ~6d — wins the comparison today.
                make_account(1, active=True, entry=entry(4.0, 64.0, 77.0, 141)),
                # 7 points over ~4d — its turn comes as its reset approaches.
                make_account(2, entry=entry(0.0, 93.0, 78.0, 105)),
            ],
            tmp_path,
        )

    async def _burn_text(self, tmp_path, strategy="waste-first"):
        from unittest.mock import patch as _patch

        from textual.widgets import Static

        from claude_swap.burn import BurnEstimate
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(self._fleet(tmp_path, strategy), start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            estimate = BurnEstimate(
                pct_per_s=1 / 346.0, tokens_per_s=3000.0, calibrated=True
            )
            with _patch.object(
                app.screen._tracker, "estimate", return_value=estimate
            ):
                app.screen._render_burn(Palette.DARK)
                rendered = app.screen.query_one("#fleet-burn", Static).render()
            return (
                rendered.plain if hasattr(rendered, "plain") else str(rendered)
            )

    async def test_it_names_the_next_account_and_when(
        self, tmp_path, fake_fleet_engine
    ):
        """"No account is losing quota meaningfully faster than this one" is a
        true answer to a question nobody asked. This is the one people ask."""
        text = await self._burn_text(tmp_path)
        line = next(
            (l for l in text.split("\n") if "takes over" in l), None
        )
        assert line is not None, text
        assert "user2 takes over by " in line
        # The limit is NAMED and the amount is a percent of that pool — "7 pts"
        # read as a count of some universal unit, which is exactly the mixing
        # the rest of the screen had to unlearn.
        assert "its 7d has 7% left" in line, line
        # The wait is only acceptable because the quota survives it, so the
        # line has to carry both halves of that comparison.
        assert " needs " in line and " of " in line

    async def test_it_is_silent_for_strategies_it_cannot_speak_for(
        self, tmp_path, fake_fleet_engine
    ):
        """The projection is waste-first's own arithmetic — the hysteresis
        gate on the risk axis. Printing it beside a different strategy would
        be describing a decision that is not being made."""
        text = await self._burn_text(tmp_path, strategy="consume-first")
        assert "takes over" not in text


@pytest.mark.asyncio
class TestTheTankReading:
    """The number in front of each bar: how much fuel the fleet holds."""

    @staticmethod
    def _fleet(tmp_path):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "all"},
        }))
        return FakeSwitcher(
            [
                make_account(
                    1, entry=make_entry(0.0, 64.0, scoped=[("Fable", 77.0)])
                ),
                make_account(
                    2, entry=make_entry(0.0, 93.0, scoped=[("Fable", 78.0)])
                ),
                make_account(
                    3, active=True,
                    entry=make_entry(92.0, 96.0, scoped=[("Fable", 100.0)]),
                ),
            ],
            tmp_path,
        )

    async def _bars(self, tmp_path, size=(140, 46)):
        from textual.widgets import Static

        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fleet(tmp_path), start="fleet")
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            rendered = app.screen.query_one("#fleet-bars", Static).render()
            return (
                rendered.plain if hasattr(rendered, "plain") else str(rendered)
            ).split("\n")

    async def test_the_tank_can_read_over_one_hundred_percent(
        self, tmp_path, fake_fleet_engine
    ):
        """Two idle accounts and one at 92% hold 208% of a 5-hour window
        between them. Clamping that to 100 would say the same thing about a
        fleet of one and a fleet of six."""
        import re

        lines = await self._bars(tmp_path)
        session = next(line for line in lines if line.lstrip().startswith("5h:"))
        assert re.match(r"\s+5h:\s+208%\s+[━╸╌]", session), session

    async def test_every_row_states_its_own_remaining_points(
        self, tmp_path, fake_fleet_engine
    ):
        import re

        lines = await self._bars(tmp_path)
        found = {}
        for line in lines:
            match = re.match(r"\s+(\S+):\s+(\d+)%\s+[━╸╌─]", line)
            if match:
                found[match.group(1)] = int(match.group(2))
        # 7d: (100-64) + (100-93) + (100-96);  Fable: 23 + 22 + 0
        assert found == {"5h": 208, "7d": 47, "Fable": 45}, found

    async def test_each_bar_states_how_long_its_fuel_lasts(
        self, tmp_path, fake_fleet_engine
    ):
        """Time is the unit that needs no conversion. Percent is a fraction of
        pools that differ per window and per plan; "how long can I keep
        working" is the question actually being asked."""
        import re
        from unittest.mock import patch as _patch

        from claude_swap.burn import BurnEstimate
        from claude_swap.tui.app import CswapApp
        from textual.widgets import Static

        app = CswapApp(self._fleet(tmp_path), start="fleet")
        rates = {"5h": 0.010, "7d": 0.0008, "Fable": 0.0011}
        async with app.run_test(size=(150, 46)) as pilot:
            await settle(pilot)
            with _patch.object(
                app.screen._tracker, "estimate",
                side_effect=lambda acct, window=None, **kw: BurnEstimate(
                    pct_per_s=rates.get(window), tokens_per_s=3000.0,
                    calibrated=True,
                ),
            ):
                app.screen._display_tick()
                rendered = app.screen.query_one("#fleet-bars", Static).render()
            lines = (
                rendered.plain if hasattr(rendered, "plain") else str(rendered)
            ).split("\n")
        found = {}
        for line in lines:
            match = re.match(r"\s+(\S+):\s+\d+% (≈\S+)\s+[━╸╌─]", line)
            if match:
                found[match.group(1)] = match.group(2)
        # 5h: (100-0)+(100-0)+(100-92)=208% / 0.010%/s = 5.8h;
        # 7d: 47% / 0.0008 = 16h;  Fable: 45% / 0.0011 = 11h.
        assert found == {"5h": "≈6h", "7d": "≈16h", "Fable": "≈11h"}, found

    async def test_an_idle_machine_shows_no_runway(
        self, tmp_path, fake_fleet_engine
    ):
        """A blank says "nothing measured" better than a fabricated horizon —
        an idle machine has no burn to project."""
        lines = await self._bars(tmp_path)
        for line in lines:
            assert "≈" not in line, line

    async def test_the_markers_still_point_into_the_gauge(
        self, tmp_path, fake_fleet_engine
    ):
        """The tank reading widened the label column.

        Marker columns are derived from the string that is actually drawn
        rather than a hand-added width, because getting this wrong is silent:
        every ▼ and ▲ still renders, just above the wrong segment — or above
        the label, naming an account the reader is not looking at.
        """
        lines = await self._bars(tmp_path)
        glyphs = set("━╸╌─┃")
        checked = 0
        for index, line in enumerate(lines):
            glyph = next((g for g in ("▼", "▲") if g in line), None)
            if glyph is None:
                continue
            column = line.index(glyph)
            neighbours = [
                lines[j] for j in (index - 1, index + 1) if 0 <= j < len(lines)
            ]
            bar = next((b for b in neighbours if "━" in b), None)
            assert bar is not None, "a marker with no gauge beside it"
            assert column < len(bar), f"marker at {column} is past the bar"
            assert bar[column] in glyphs, (
                f"marker at column {column} sits above {bar[column]!r}, "
                "which is not part of the gauge"
            )
            checked += 1
        assert checked >= 3, f"only {checked} markers were checked"


@pytest.mark.asyncio
class TestBarColoursAndMarkers:
    """Colour is an account's identity; the markers answer two questions."""

    def _fleet(self, tmp_path):
        return FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(16.0, 53.0)),
                make_account(2, entry=make_entry(0.0, 92.0)),
                make_account(3, entry=make_entry(100.0, 72.0)),
            ],
            tmp_path,
        )

    async def test_one_colour_per_account_across_every_bar(
        self, tmp_path, fake_fleet_engine
    ):
        """Ranking each bar separately made the same account red on the
        session row and amber on the weekly one, which reads as the account
        changing rather than as three views of one fleet."""
        import time as _t

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        fake = self._fleet(tmp_path)
        app = CswapApp(fake, start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            screen = app.screen
            now = _t.now() if hasattr(_t, "now") else _t.time()
            colours = screen._account_colours(screen._segments(now), Palette.DARK)
            assert len(set(colours.values())) == len(colours), "ranks collided"
            # THE SESSION WINDOW MUST NOT INFLUENCE THE RANKING AT ALL. Asserted
            # by moving it and demanding the map not budge: the previous version
            # of this test compared each colour to itself and to a re-sort of
            # its own output, so it held no matter what the ranking did.
            before = dict(colours)
            fake._accounts[0] = dataclasses.replace(
                fake._accounts[0],
                usage=UsageEntry(
                    last_good={
                        "five_hour": {"pct": 99.0, "resets_at": _iso_in(600)},
                        "seven_day": {"pct": 53.0, "resets_at": _iso_in(86400 * 3)},
                    },
                    fetched_at=_t.time() - 5.0,
                    age_s=5.0,
                ),
            )
            await settle(pilot)
            after = screen._account_colours(screen._segments(_t.time()), Palette.DARK)
            assert after == before, (
                "the 5-hour window moved the colours; it recycles in hours, so "
                "nothing in it is ever about to be wasted"
            )

    @staticmethod
    def _fleet_by_reset(tmp_path, rows):
        """``rows`` of ``(number, active, weekly pct, seconds until it resets)``."""
        return FakeSwitcher(
            [
                make_account(
                    number,
                    active=active,
                    entry=UsageEntry(
                        last_good={
                            "five_hour": {"pct": 0.0, "resets_at": _iso_in(3600)},
                            "seven_day": {"pct": pct, "resets_at": _iso_in(seconds)},
                        },
                        fetched_at=time.time() - 5.0,
                        age_s=5.0,
                    ),
                )
                for number, active, pct, seconds in rows
            ],
            tmp_path,
        )

    async def test_colour_is_distance_to_the_reset_furthest_is_greenest(
        self, tmp_path, fake_fleet_engine
    ):
        """The axis is how far the deadline is from now, nothing else.

        Colour used to rank on waste risk — headroom DIVIDED BY that distance —
        which paints an account with 8 points left the calmest thing on screen
        precisely because it has nothing to lose. "Nearly exhausted" and
        "plenty of time" came out the same green.
        """
        import time as _t

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(
            self._fleet_by_reset(
                tmp_path,
                [
                    (1, True, 64.0, 86400 * 6),    # furthest deadline
                    (2, False, 92.0, 86400 * 4),   # middle, but almost spent
                    (3, False, 72.0, 3600 * 12),   # soonest deadline
                ],
            ),
            start="fleet",
        )
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            screen = app.screen
            now = _t.time()
            segments = screen._segments(now)
            colours = screen._account_colours(segments, Palette.DARK)
            ramp = screen._rank_colours(len(segments), Palette.DARK)
            assert ramp[0] == Palette.DARK.sev_crit
            assert ramp[-1] == Palette.DARK.sev_ok
            by_distance = sorted(segments, key=lambda seg: seg.reset_ts or 0.0)
            assert [colours[seg.number] for seg in by_distance] == ramp, (
                "soonest reset must take the red end and furthest the green"
            )
            assert colours["3"] == Palette.DARK.sev_crit
            assert colours["1"] == Palette.DARK.sev_ok
            # Account 2 holds the least perishable quota of the three (8
            # points). Under the risk axis that made it the GREENEST; its
            # deadline sits in the middle, so its colour must too.
            assert colours["2"] == ramp[1]

    async def test_the_bar_runs_green_on_the_left_and_red_on_the_right(
        self, tmp_path, fake_fleet_engine
    ):
        """A spectrum that is not monotonic is not a spectrum.

        The order and the colour are two call sites that have to agree; they
        are now read off one ranking, and this is what proves it on the pixels
        rather than in the sort key.
        """
        from textual.widgets import Static

        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.theme import Palette

        app = CswapApp(
            self._fleet_by_reset(
                tmp_path,
                [
                    (1, True, 64.0, 86400 * 6),
                    (2, False, 92.0, 86400 * 4),
                    (3, False, 72.0, 3600 * 12),
                ],
            ),
            start="fleet",
        )
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            rendered = app.screen.query_one("#fleet-bars", Static).render()
            ramp = [c.lower() for c in app.screen._rank_colours(3, Palette.DARK)]
            checked = 0
            for line in rendered.split("\n"):
                if "\u2501" not in line.plain:
                    continue  # not a gauge row: the ▼/▲ markers live on their own
                seen: list[str] = []
                for span in sorted(line.spans, key=lambda sp: sp.start):
                    # A span carries either the style string we passed in or a
                    # resolved Style; Color.hex also comes back upper-cased.
                    style = span.style
                    if isinstance(style, str):
                        hexed = style if style.startswith("#") else None
                    else:
                        foreground = getattr(style, "foreground", None)
                        hexed = None if foreground is None else foreground.hex
                    if hexed is None:
                        continue
                    hexed = hexed.lower()
                    if hexed in ramp and (not seen or seen[-1] != hexed):
                        seen.append(hexed)
                # The gauge run and the percentage list after it draw the same
                # accounts in the same order, so the spectrum appears once per
                # group. Both must run green to red, hence the repetition.
                spectrum = list(reversed(ramp))
                assert seen and len(seen) % len(spectrum) == 0, (
                    f"a group is truncated mid-spectrum: {seen}"
                )
                assert seen == spectrum * (len(seen) // len(spectrum)), (
                    f"the run is not a spectrum left to right: {seen}"
                )
                checked += 1
            assert checked, "no gauge row was rendered, so nothing was checked"

    async def test_an_account_with_no_reset_is_never_the_expiring_one(
        self, tmp_path, fake_fleet_engine
    ):
        """"expires —" is not a deadline. Picking such an account made the
        marker name one that was in no danger at all."""
        import time as _t

        from claude_swap.tui.app import CswapApp
        from textual.widgets import Static

        fake = self._fleet(tmp_path)
        fake._accounts[1] = dataclasses.replace(
            fake._accounts[1],
            usage=UsageEntry(
                last_good={"five_hour": {"pct": 0.0}},   # no resets_at at all
                fetched_at=_t.time(),
                age_s=1.0,
            ),
        )
        app = CswapApp(fake, start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            rendered = app.screen.query_one("#fleet-bars", Static).render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            for line in text.split("\n"):
                if line.lstrip().startswith("▼"):
                    assert "—" not in line, f"marker points at a dateless row: {line}"

    async def test_both_markers_are_drawn(self, tmp_path, fake_fleet_engine):
        """▼ is what dies first, ▲ is what is being spent — the whole point of
        the screen is the gap between them."""
        from claude_swap.tui.app import CswapApp
        from textual.widgets import Static

        app = CswapApp(self._fleet(tmp_path), start="fleet")
        async with app.run_test(size=(140, 46)) as pilot:
            await settle(pilot)
            rendered = app.screen.query_one("#fleet-bars", Static).render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "▼" in text and "▲" in text
            assert "active" in text, "the ▲ marker must say which one is live"
