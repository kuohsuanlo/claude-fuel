"""Second-level burn-rate sensing, from local transcripts rather than the API.

WHY THIS EXISTS. The switch decision needs to know how fast quota is being
spent *right now*, and the usage endpoint cannot answer that: its measured
budget is ~28-30 requests per rolling hour per identity (see
``poll_policy``), so the fastest honest sampling of ``pct`` is one point
every three minutes. A three-minute average is exactly the wrong instrument
for the failure this module was written for — a heavy parallel turn can
cross ten points of a 5-hour window between two polls, so a threshold set
where the *average* looks safe is overshot before the next sample lands.

The signal that IS free is local: Claude Code appends one JSONL line per
content block to ``<config home>/projects/<slug>/<session>.jsonl``, and every
assistant line carries the request's own ``usage`` (input, output, and both
cache counters) plus a timestamp. Reading it costs no quota, can be done
every second, and covers every session on this machine including subagent
sidechains. What it does NOT carry is the window percentage — tokens are not
percent — so this module pairs the two: local tokens supply the SHAPE of the
curve at second resolution, and the occasional API ``pct`` observation
supplies its SCALE.

CALIBRATION, not a hardcoded formula. Weighted tokens (``_WEIGHTS``) are a
cost-shaped proxy, not the provider's real accounting, and the real one is
undocumented and can be retuned. So the constant relating them to window
percent is never assumed: it is measured, as ``sum(delta pct) /
sum(delta tokens)`` over intervals bracketed by two API observations. Any
consistently-shaped proxy therefore converges to the right scale, and a
change in the provider's accounting shows up as the ratio drifting rather
than as a silently wrong number. Until at least one interval has been
observed the estimate falls back to the API-derived average, which is
correct but coarse — never to a guess.

Everything here is pure measurement. Nothing in this module decides, fetches,
or writes; ``recommended_threshold`` returns advice and the caller chooses
what to do with it.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.paths import get_claude_config_home

# Relative weights folding one request's token counts into a single
# cost-shaped scalar. These mirror the published price ratios (cache reads
# are cheap, output is dear); their ABSOLUTE values are irrelevant because
# calibration divides them out, but their RATIOS matter — they are what keeps
# the proxy linear when the input/output mix changes mid-session, which is
# precisely what a long tool-using turn does.
_WEIGHTS: dict[str, float] = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}

# How much transcript history to retain. Sized by CALIBRATION, not by the
# rate display: a calibration interval spans two API observations, which the
# poll planner can space up to POST_429_MAX_INTERVAL_S (30 min) apart under
# congestion. Retaining less would silently truncate the token side of that
# interval, and an undercounted denominator inflates percent-per-token —
# i.e. the tool would report burning FASTER than it is, exactly the error
# that makes a threshold recommendation useless.
DEFAULT_WINDOW_S = 1800.0

# The rate reported as "instantaneous". Shorter reacts faster but samples too
# few requests to be stable (one 40k-token response inside 10s reads as an
# implausible sustained rate); longer smooths away the burst this module
# exists to catch.
INSTANT_WINDOW_S = 60.0

# Files untouched for longer than this are not re-stat'ed for tailing. A
# session that has been silent for over an hour cannot contribute to a
# 5-minute window, and skipping them keeps the per-second poll O(active
# sessions) instead of O(every session ever recorded).
_IDLE_FILE_S = 3600.0

# Calibration keeps this many bracketed intervals. Enough to average out a
# single mis-attributed interval (a switch mid-interval, a window rollover),
# few enough that a genuine change in accounting is reflected within an hour.
_CALIBRATION_SAMPLES = 24

# Burst allowance behind ``recommended_threshold``. The recommendation answers
# "where must the trigger sit so that a sudden burst still lands inside the
# window?" — assume the burn can jump to BURST_MULTIPLIER times the observed
# rate and hold it for BURST_WINDOW_S while the switch is detected and
# performed. 2.0x for 10s is deliberately generous: overshoot costs a hard
# rate-limit mid-turn, while a threshold a few points low costs nothing but
# an earlier switch.
BURST_MULTIPLIER = 2.0
BURST_WINDOW_S = 10.0

# Bounds for the recommendation, matching settings.json's own threshold range
# so the advice is always a value the user could actually set.
_THRESHOLD_LO = 50.0
_THRESHOLD_HI = 99.9


def weigh_usage(usage: dict | None) -> float:
    """One request's ``usage`` block as a single cost-shaped scalar.

    Unknown keys are ignored rather than summed blindly: ``usage`` carries
    non-token members (``service_tier``, ``server_tool_use``, an ``iterations``
    list that REPEATS the same counts) and adding those would double-count the
    very requests that matter most.
    """
    if not isinstance(usage, dict):
        return 0.0
    total = 0.0
    for key, weight in _WEIGHTS.items():
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value) * weight
    return total


def _parse_ts(value: object) -> float | None:
    """ISO-8601 ``timestamp`` from a transcript line, as an epoch."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class _FileCursor:
    """Where tailing left off in one transcript file.

    ``offset`` is a byte position, so re-reading appended data costs only the
    new bytes. A file that SHRANK was rotated or replaced (Claude Code can
    rewrite a session file on resume), and its cursor is reset rather than
    trusted — a stale offset into a shorter file would silently skip
    everything before it, or slice a line in half.
    """

    offset: int = 0
    size: int = 0


class TranscriptBurnSensor:
    """Tails Claude Code transcripts and reports recent weighted token spend.

    Deliberately ignorant of accounts: transcripts record what THIS MACHINE
    spent, and whichever account was active at the time is the one that paid.
    Attributing an interval to an account is the caller's job (it knows when
    it switched); doing it here would need a second source of truth about
    active-slot history that this module has no way to verify.
    """

    def __init__(
        self,
        projects_dir: Path | None = None,
        *,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.time,
    ):
        self.projects_dir = projects_dir or (get_claude_config_home() / "projects")
        self.window_s = window_s
        self.clock = clock
        self._cursors: dict[str, _FileCursor] = {}
        # Whether the first scan has happened. It separates the two kinds of
        # "file I have not seen": one that existed BEFORE this sensor started
        # (history — skipped to its end, see _ingest) and one that APPEARED
        # while it was running (a new session, every byte of which is spend
        # this sensor should count).
        self._primed = False
        # (epoch, weighted tokens) newest-last, pruned to ``window_s``.
        self._samples: deque[tuple[float, float]] = deque()
        # Message ids already counted. Claude Code writes ONE LINE PER CONTENT
        # BLOCK, so a single API response appears 2-4 times carrying the same
        # `usage` — measured on a live transcript: 85 assistant lines for 38
        # distinct message ids, with identical usage every time. Counting
        # lines instead of messages overstates spend by 2-4x, which would
        # inflate every rate and every recommendation downstream.
        self._seen_ids: deque[str] = deque()
        self._seen_set: set[str] = set()

    # -- ingest -------------------------------------------------------------

    def poll(self) -> None:
        """Read whatever has been appended since the last call. Never raises.

        Best-effort by design: this runs on a one-second UI timer, and a
        transcript being rotated, truncated, or written mid-line underneath us
        is ordinary. A lost sample slightly understates one interval; an
        exception would take the whole display down.
        """
        now = self.clock()
        try:
            paths = list(self._active_files(now))
        except OSError:
            return
        for path in paths:
            try:
                self._ingest(path, now)
            except OSError:
                continue
        self._primed = True
        self._prune(now)

    def _active_files(self, now: float) -> Iterable[Path]:
        """Transcript files recent enough to still matter, newest first."""
        root = self.projects_dir
        if not root.is_dir():
            return []
        found: list[tuple[float, Path]] = []
        for project in os.scandir(root):
            if not project.is_dir():
                continue
            for entry in os.scandir(project.path):
                if not entry.name.endswith(".jsonl") or not entry.is_file():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                # An unseen file is read from its END, not its start (see
                # _ingest), so admitting an idle one costs a stat and nothing
                # else — but a file whose first sighting is mid-burst must not
                # be excluded just because its mtime predates the lookback.
                if now - mtime > _IDLE_FILE_S and entry.path not in self._cursors:
                    continue
                found.append((mtime, Path(entry.path)))
        found.sort(reverse=True)
        return [path for _, path in found]

    def _ingest(self, path: Path, now: float) -> None:
        key = str(path)
        cursor = self._cursors.get(key)
        size = path.stat().st_size
        if cursor is None:
            # A file already present at the FIRST scan is history: skip to its
            # end. Replaying it would inject a whole session's tokens dated to
            # startup — millions on a long transcript — and every rate derived
            # from them would be nonsense for a full retention window.
            #
            # A file that appears LATER is a session that started while we were
            # watching, so every byte of it is spend we are here to measure;
            # read it from the beginning. (Records too old for the window are
            # dropped in _consume_line, so even a long-idle file that resumes
            # and is scanned for the first time cannot backfill stale spend.)
            start = size if not self._primed else 0
            self._cursors[key] = cursor = _FileCursor(offset=start, size=size)
            if start >= size:
                return
        if size < cursor.size:
            # Truncated or replaced: the old offset points into different
            # content now. Restart from the new end rather than re-reading.
            cursor.offset = size
            cursor.size = size
            return
        if size == cursor.offset:
            return
        with path.open("rb") as handle:
            handle.seek(cursor.offset)
            data = handle.read(size - cursor.offset)
        # A trailing partial line means the writer is mid-append. Keep the
        # remainder unconsumed so it is parsed once it is complete, instead of
        # discarding a record or parsing half of one.
        consumed = data.rfind(b"\n") + 1
        cursor.offset += consumed
        cursor.size = size
        for raw in data[:consumed].splitlines():
            self._consume_line(raw, now)

    def _consume_line(self, raw: bytes, now: float) -> None:
        if b'"usage"' not in raw:
            return  # cheap reject: user/tool-result lines carry no usage
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(record, dict) or record.get("type") != "assistant":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if isinstance(message_id, str):
            if message_id in self._seen_set:
                return
            self._seen_set.add(message_id)
            self._seen_ids.append(message_id)
            while len(self._seen_ids) > 4096:
                self._seen_set.discard(self._seen_ids.popleft())
        weighted = weigh_usage(message.get("usage"))
        if weighted <= 0:
            return
        ts = _parse_ts(record.get("timestamp"))
        if ts is None or ts > now:
            # No timestamp, or one in the future (clock skew against the
            # writer): the ingest itself is the evidence that this spend just
            # happened, so date it now rather than discard a real request.
            ts = now
        elif now - ts > self.window_s:
            # Genuinely older than anything we retain — a resumed session's
            # backlog, read on its first sighting. DROPPED, never clamped:
            # clamping would stack an entire history onto "now" and report a
            # burst that never happened.
            return
        self._samples.append((ts, weighted))

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # -- read ---------------------------------------------------------------

    def tokens_since(self, since_ts: float) -> float:
        """Weighted tokens recorded strictly AFTER ``since_ts``.

        Half-open on purpose. Consecutive calibration intervals share an
        endpoint — one ends at an observation, the next begins at it — so
        counting the boundary instant in both charges the same tokens twice
        and inflates percent-per-token. Measured with two back-to-back
        brackets of 1000 tokens each, an inclusive bound reported the second
        account as consuming 2000 and halved its calibrated scale, which
        understates its burn: the direction that overshoots a threshold.
        """
        return sum(w for ts, w in self._samples if ts > since_ts)

    def tokens_per_s(self, window_s: float = INSTANT_WINDOW_S) -> float:
        """Weighted tokens per second over the trailing ``window_s``.

        Zero when nothing was spent — an idle machine has a real rate of
        zero, which is different from "unknown" and is reported as such.
        """
        window_s = max(1.0, min(window_s, self.window_s))
        now = self.clock()
        return self.tokens_since(now - window_s) / window_s


@dataclass
class BurnEstimate:
    """How fast the binding window is being consumed, and what follows.

    ``pct_per_s`` is None only when nothing can be said yet (no local spend
    observed and fewer than two API observations). ``source`` names which
    instrument produced it so a UI can be honest about resolution:
    ``"local"`` is second-scale and calibrated, ``"api"`` is a multi-minute
    average.
    """

    pct_per_s: float | None = None
    source: str | None = None  # "local" | "api"
    calibrated: bool = False
    seconds_to_threshold: float | None = None
    # Local weighted tokens per second. Known from the first display tick,
    # long before percent is: reported separately so an uncalibrated view can
    # still show that work IS happening (and how much), rather than an
    # indefinite "measuring…" that looks like a hung instrument.
    tokens_per_s: float = 0.0

    @property
    def seconds_per_pct(self) -> float | None:
        """How long one percentage point currently takes. None when idle."""
        if self.pct_per_s is None or self.pct_per_s <= 0:
            return None
        return 1.0 / self.pct_per_s

    def recommended_threshold(
        self,
        *,
        burst_multiplier: float = BURST_MULTIPLIER,
        burst_window_s: float = BURST_WINDOW_S,
    ) -> float | None:
        """Highest trigger that still survives a burst, or None if unknown.

        The margin is what a burst could spend before a switch completes:
        ``rate x multiplier x window``. At the defaults a run burning 0.05
        %/s (one point every 20s) reserves one point and recommends 99;
        a run burning 0.5 %/s reserves ten and recommends 90.

        An idle machine gets the ceiling rather than 100: a zero-rate reading
        is a statement about the last minute, not a promise about the next
        one, and the ceiling is where settings.json already stops.
        """
        if self.pct_per_s is None:
            return None
        margin = max(0.0, self.pct_per_s) * burst_multiplier * burst_window_s
        return max(_THRESHOLD_LO, min(_THRESHOLD_HI, 100.0 - margin))


@dataclass
class BurnTracker:
    """Pairs API ``pct`` observations with local token spend, per account.

    One instance serves the whole app: every surface that fetches usage feeds
    :meth:`observe`, and the calibration each account accumulates is shared by
    all of them.
    """

    sensor: TranscriptBurnSensor
    clock: Callable[[], float] = time.time
    # account number -> (epoch, binding pct) of the two most recent DISTINCT
    # observations, oldest first.
    _observations: dict[str, deque[tuple[float, float]]] = field(default_factory=dict)
    # account number -> bracketed (delta pct, delta weighted tokens) pairs.
    #
    # PER ACCOUNT, because percent is a fraction of a PLAN'S window and plans
    # differ: the same 100k tokens is a far smaller share of a 20x window than
    # of a Pro one. Pooling them would scale every account by the fleet's
    # average plan, quietly overstating the burn on the large accounts and
    # understating it on the small ones — and understating it is the direction
    # that lets a threshold be overshot.
    _calibration: dict[str, deque[tuple[float, float]]] = field(default_factory=dict)

    def observe(self, account: str, pct: float | None, fetched_at: float) -> None:
        """Record one API-derived utilization reading for ``account``.

        Idempotent per fetch: re-reporting the same ``fetched_at`` (every
        surface re-reads the same stored row between fetches) neither adds an
        observation nor a calibration sample.
        """
        if pct is None:
            return
        history = self._observations.setdefault(account, deque(maxlen=2))
        if history and history[-1][0] >= fetched_at:
            return  # same or older snapshot than the one already held
        previous = history[-1] if history else None
        history.append((fetched_at, float(pct)))
        if previous is None:
            return
        prev_ts, prev_pct = previous
        delta_pct = float(pct) - prev_pct
        # A window rollover drops pct; a paused-then-resumed machine yields a
        # zero delta. Neither says anything about the tokens-to-percent
        # ratio, and feeding either in would drag the calibration toward zero.
        if delta_pct <= 0:
            return
        if fetched_at - prev_ts > self.sensor.window_s:
            # The interval reaches back further than the sensor retains, so
            # its token side is truncated by construction. An undercounted
            # denominator inflates percent-per-token, which would report a
            # faster burn than reality — the one error a threshold
            # recommendation must not make. Skip rather than approximate.
            return
        delta_tokens = self.sensor.tokens_since(prev_ts)
        if delta_tokens <= 0:
            # Percent moved with no local spend: another machine (or the
            # phone app) is burning the same account. Real, but not
            # attributable to local tokens — excluded from calibration so the
            # ratio stays a property of THIS machine's transcripts.
            return
        samples = self._calibration.setdefault(account, deque())
        samples.append((delta_pct, delta_tokens))
        while len(samples) > _CALIBRATION_SAMPLES:
            samples.popleft()

    def pct_per_token(self, account: str | None = None) -> float | None:
        """Calibrated percent-per-weighted-token, or None before first bracket.

        Ratio of the SUMS rather than the mean of the ratios: intervals differ
        in length by an order of magnitude, and a mean of ratios would weight
        a 20-second interval the same as a 10-minute one.

        With no samples for ``account`` yet, falls back to the pooled ratio
        across every account that HAS been calibrated. On the common
        same-plan fleet that is the right number immediately; on a mixed-plan
        one it is an approximation that a single bracketed interval on this
        account replaces. Both beat reporting nothing, which is what a fresh
        account would otherwise show for its first few minutes.
        """
        samples: list[tuple[float, float]] = []
        if account is not None:
            samples = list(self._calibration.get(account, ()))
        if not samples:
            for pairs in self._calibration.values():
                samples.extend(pairs)
        if not samples:
            return None
        total_pct = sum(d for d, _ in samples)
        total_tokens = sum(t for _, t in samples)
        if total_tokens <= 0:
            return None
        return total_pct / total_tokens

    # -- persistence --------------------------------------------------------

    def calibration_state(self) -> dict:
        """JSON-safe calibration, for carrying the scale across restarts.

        Without this every launch is blind for as long as it takes two API
        polls to bracket some local spend — minutes at the endpoint's budgeted
        cadence, which is exactly the window a user opens the view to watch.
        The ratio is a property of the plan, not of the process, so it is
        worth keeping.
        """
        return {
            "schemaVersion": 1,
            "accounts": {
                account: [[d, t] for d, t in pairs]
                for account, pairs in self._calibration.items()
                if pairs
            },
        }

    def restore_calibration(self, state: object) -> None:
        """Load :meth:`calibration_state` output. Malformed input is ignored.

        Forgiving on purpose: a corrupt cache file must cost the freshness of
        one estimate, never the view. Anything unparseable simply leaves the
        tracker uncalibrated, which is the state it would have been in anyway.
        """
        if not isinstance(state, dict):
            return
        accounts = state.get("accounts")
        if not isinstance(accounts, dict):
            return
        for account, pairs in accounts.items():
            if not isinstance(account, str) or not isinstance(pairs, list):
                continue
            restored: deque[tuple[float, float]] = deque()
            for pair in pairs[-_CALIBRATION_SAMPLES:]:
                if (
                    isinstance(pair, (list, tuple))
                    and len(pair) == 2
                    and all(isinstance(v, (int, float)) for v in pair)
                    and not any(isinstance(v, bool) for v in pair)
                    and pair[1] > 0
                ):
                    restored.append((float(pair[0]), float(pair[1])))
            if restored:
                self._calibration[account] = restored

    def estimate(
        self, account: str, *, window_s: float = INSTANT_WINDOW_S
    ) -> BurnEstimate:
        """Current burn rate for ``account``, best instrument available."""
        tokens_per_s = self.sensor.tokens_per_s(window_s)
        k = self.pct_per_token(account)
        if k is not None:
            return BurnEstimate(
                pct_per_s=k * tokens_per_s,
                source="local",
                calibrated=True,
                tokens_per_s=tokens_per_s,
            )
        history = self._observations.get(account)
        if history and len(history) == 2:
            (t0, p0), (t1, p1) = history[0], history[1]
            span = t1 - t0
            if span > 0 and p1 >= p0:
                return BurnEstimate(
                    pct_per_s=(p1 - p0) / span,
                    source="api",
                    calibrated=False,
                    tokens_per_s=tokens_per_s,
                )
        return BurnEstimate(tokens_per_s=tokens_per_s)

    def project(
        self, pct: float | None, estimate: BurnEstimate, threshold: float
    ) -> float | None:
        """Seconds until ``pct`` reaches ``threshold`` at the current rate.

        None when idle or already there — "already at the threshold" is the
        caller's own comparison, not a countdown.
        """
        if pct is None or estimate.pct_per_s is None or estimate.pct_per_s <= 0:
            return None
        remaining = threshold - pct
        if remaining <= 0:
            return 0.0
        return remaining / estimate.pct_per_s
