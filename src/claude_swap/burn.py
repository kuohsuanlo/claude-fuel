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
from collections.abc import Sequence
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
# window?" — assume the burn holds BURST_MULTIPLIER times the observed rate
# for BURST_WINDOW_S while the switch is detected and performed.
#
# THE WINDOW IS A FULL ENGINE TICK, not a guess at switch latency. Every tick
# may fetch usage, and the endpoint's budget is ~28-30 requests an hour, so
# the loop cannot run every second: the interval IS the exposure. Reserving
# 10s while ticking every 60s left five sixths of the gap uncovered, and a
# real agent hit a hard limit through it at 0.062 %/s — 3.7 points a minute
# against a 0.6-point reserve.
BURST_MULTIPLIER = 1.0
BURST_WINDOW_S = 60.0

# Floor under the reserve, in percentage points. A measured rate of zero is a
# statement about the last minute, not a promise about the next one — and the
# first heavy turn after an idle spell is exactly when the reserve is needed.
# Without it an idle machine recommended the ceiling and defended nothing.
BURST_FLOOR_PCT = 0.5

# Bounds for the recommendation, matching settings.json's own threshold range
# so the advice is always a value the user could actually set.
_THRESHOLD_LO = 50.0
_THRESHOLD_HI = 99.9


#: Windows every request counts against, whatever model produced it. Anything
#: else the API reports is a per-model ("scoped") window, and only that model's
#: requests move it.
ACCOUNT_WIDE_WINDOWS = ("5h", "7d")

#: Bumped when a stored constant would mean something different. v1 divided
#: a scoped window's percent by the machine's total tokens.
_CALIBRATION_SCHEMA = 2


def window_model_filter(window: str | None) -> str | None:
    """The model name a window is scoped to, or ``None`` for account-wide.

    ``None`` means "count every request"; a string means "count only requests
    whose model matches", which is what makes an idle Fable window read as
    genuinely idle instead of as a share of whatever else is running.
    """
    if not window or window in ACCOUNT_WIDE_WINDOWS:
        return None
    return window


def configured_models(config_home=None) -> tuple[str, ...]:
    """Models Claude Code is CONFIGURED to use, from its own settings files.

    KNOWN BEFORE THE FIRST REQUEST, which observation can never be: a
    transcript line is written after a response, so a model's first use is
    always discovered by having already spent it. If that first request lands
    on an account whose limit for that model is gone, it fails — and no amount
    of polling can undo a request that already happened.

    The selected model is persisted (``settings.json``'s ``model``), so a
    ``/model`` switch is visible the moment it is made. Suffixes like
    ``[1m]`` are context-window variants of the same model and are stripped.

    Never raises: an unreadable or absent settings file simply contributes
    nothing, leaving the measured mix to speak on its own.
    """
    from claude_swap import paths

    home = config_home if config_home is not None else paths.get_claude_config_home()
    found: list[str] = []
    for name in ("settings.json", "settings.local.json"):
        try:
            raw = json.loads((home / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        value = raw.get("model")
        if isinstance(value, str) and value.strip():
            model = value.split("[", 1)[0].strip()
            if model and model not in found:
                found.append(model)
    return tuple(found)


def burning_models(
    sensor,
    declared: Sequence[str],
    reported: Sequence[str],
    *,
    window_s: float = DEFAULT_WINDOW_S,
    configured: Sequence[str] | None = None,
) -> tuple[str, ...] | None:
    """Which per-model windows should gate a switch: the ones being SPENT.

    ``declared`` is ``autoswitch.model`` (names, or ``all``); ``reported`` is
    the scoped windows this account actually has. The result is the subset
    that has seen traffic in the trailing ``window_s``.

    WHY MEASURE INSTEAD OF DECLARE. A static list says "all my work needs
    Fable". When it does not, the engine reads an account holding Fable at
    100% as having no quota at all — on a live fleet that turned an account
    with 4 points left and the highest waste risk on the board into one scored
    0.000 and treated as unusable.

    IDLE IS NOT EVIDENCE. If the machine has spent nothing at all over the
    window, every declared window is kept. "You are running Opus, not Fable"
    and "you are running nothing" look identical in the token stream and mean
    opposite things: the first is a measurement, the second is the absence of
    one, and relaxing a gate on an absence is how you get sent to an account
    that blocks the moment work resumes.
    """
    if not declared:
        return ()
    wanted = {d.strip().lower() for d in declared if d.strip()}
    names = (
        list(reported)
        if "all" in wanted
        else [name for name in reported if name.lower() in wanted]
    )
    if not names:
        # The declared names match nothing any account reports. Returning an
        # empty gating list would silently disable the windows AND swallow the
        # one-shot typo guard, so this says "nothing to narrow" instead and
        # leaves the caller's declared list intact.
        return None
    if sensor.tokens_per_s(window_s) <= 0:
        return tuple(names)  # nothing ran: no evidence either way
    # THE UNION OF CHOSEN AND OBSERVED. A model that is merely SELECTED has
    # spent nothing yet, and waiting for it to spend something means the first
    # request of the session is the one that discovers the limit is gone. A
    # model that is RUNNING gates whatever the setting says, which is what
    # covers a session started with its own --model override.
    chosen = configured if configured is not None else configured_models()
    return tuple(
        name
        for name in names
        if sensor.tokens_per_s(window_s, name) > 0
        or any(model_matches(name, model) for model in chosen)
    )


def model_matches(window_name: str, model_id: object) -> bool:
    """Does one request's model count against a scoped window?

    The API names a scoped window by DISPLAY name (``Fable``) while a
    transcript records the model ID (``claude-fable-5``). Substring on the
    display name is the only join the two sides offer, and it is the one a
    reader makes by eye. A request with no model recorded counts against
    nothing scoped: guessing would attribute spend to a window that may not
    have moved at all.
    """
    if not isinstance(model_id, str):
        return False
    return window_name.lower() in model_id.lower()


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
        # (timestamp, weighted tokens, model id). The model is what lets a
        # per-model window be measured on its OWN traffic rather than on a
        # constant share of the machine's total.
        self._samples: deque[tuple[float, float, str | None]] = deque()
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
        model = message.get("model")
        self._samples.append((ts, weighted, model if isinstance(model, str) else None))

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # -- read ---------------------------------------------------------------

    def tokens_since(
        self, since_ts: float, model_filter: str | None = None
    ) -> float:
        """Weighted tokens recorded strictly AFTER ``since_ts``.

        Half-open on purpose. Consecutive calibration intervals share an
        endpoint — one ends at an observation, the next begins at it — so
        counting the boundary instant in both charges the same tokens twice
        and inflates percent-per-token. Measured with two back-to-back
        brackets of 1000 tokens each, an inclusive bound reported the second
        account as consuming 2000 and halved its calibrated scale, which
        understates its burn: the direction that overshoots a threshold.
        """
        return sum(
            w for ts, w, model in self._samples
            if ts > since_ts
            and (model_filter is None or model_matches(model_filter, model))
        )

    def tokens_per_s(
        self,
        window_s: float = INSTANT_WINDOW_S,
        model_filter: str | None = None,
    ) -> float:
        """Weighted tokens per second over the trailing ``window_s``.

        Zero when nothing was spent — an idle machine has a real rate of
        zero, which is different from "unknown" and is reported as such.
        """
        window_s = max(1.0, min(window_s, self.window_s))
        now = self.clock()
        return self.tokens_since(now - window_s, model_filter) / window_s


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
        floor_pct: float = BURST_FLOOR_PCT,
    ) -> float | None:
        """Highest trigger that still survives a burst, or None if unknown.

        The margin is what the burn could spend before the next tick can act:
        ``rate x multiplier x window``, never less than ``floor_pct``. At the
        defaults a run burning 0.05 %/s (one point every 20s) reserves three
        points and recommends 97; one burning 0.5 %/s reserves thirty.

        THE FLOOR APPLIES TO AN IDLE READING TOO. Zero measured is not a
        promise of zero next minute, and returning the ceiling there defended
        nothing at exactly the moment before a heavy turn starts.
        """
        if self.pct_per_s is None:
            return None
        margin = max(
            floor_pct,
            max(0.0, self.pct_per_s) * burst_multiplier * burst_window_s,
        )
        # Rounded to a tenth, which is the resolution settings.json accepts.
        # Unrounded it renders as "99.86597411%" — ten significant digits of a
        # measurement whose inputs are a 60-second sample and an integer
        # percentage, i.e. eight digits of false precision.
        return round(max(_THRESHOLD_LO, min(_THRESHOLD_HI, 100.0 - margin)), 1)


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
    # Which account the machine is currently spending; see `note_active`.
    _active: str | None = None

    def note_active(self, account: str | None) -> None:
        """Tell the tracker which account the machine is currently spending.

        Calibration divides a window's percentage movement by the tokens this
        machine spent in the same interval, and those tokens land on whichever
        account was ACTIVE. An interval that spans a switch therefore charges
        one account's percentage with another's tokens. Rather than try to
        apportion it, the baselines are dropped on a switch so the next
        interval starts clean — one lost sample against a permanently skewed
        ratio.
        """
        if account is None or account == self._active:
            # Unknown is not a switch. A snapshot that cannot name the active
            # account would otherwise clear the baselines, and clearing them
            # on alternate ticks means no interval ever closes.
            return
        self._active = account
        self._observations.clear()

    def observe(
        self, account: str, window: str, pct: float | None, fetched_at: float
    ) -> None:
        """Record one API reading for ONE named window of ``account``.

        PER WINDOW, not per account. The 5-hour and weekly windows are
        different sizes, so a token is a different fraction of each — measured
        on a live fleet, mixing them gave 411k, 2,148k and 348k tokens per
        percent from three consecutive samples of the same account, because
        the binding window flipped from 7d to 5h partway through. Averaging
        those understated the burn rate roughly threefold, which is the
        direction that lets a threshold be overshot.

        Idempotent per fetch: re-reporting the same ``fetched_at`` (every
        surface re-reads the same stored row between fetches) neither adds an
        observation nor a calibration sample.
        """
        if pct is None:
            return
        key = f"{account}\u0000{window}"
        history = self._observations.setdefault(key, deque(maxlen=2))
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
        # A SCOPED WINDOW IS CALIBRATED ON ITS OWN MODEL'S TOKENS. Dividing
        # its percent by the machine's TOTAL tokens bakes the model mix at
        # calibration time into the constant: calibrate during Fable-heavy
        # work, switch to Opus, and the Fable window is still reported as
        # burning at a share of everything else.
        delta_tokens = self.sensor.tokens_since(prev_ts, window_model_filter(window))
        if delta_tokens <= 0:
            # Percent moved with no local spend: another machine (or the
            # phone app) is burning the same account. Real, but not
            # attributable to local tokens — excluded from calibration so the
            # ratio stays a property of THIS machine's transcripts.
            return
        samples = self._calibration.setdefault(key, deque())
        samples.append((delta_pct, delta_tokens))
        while len(samples) > _CALIBRATION_SAMPLES:
            samples.popleft()

    def pct_per_token(
        self, account: str | None = None, window: str | None = None
    ) -> float | None:
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
        if account is not None and window is not None:
            samples = list(self._calibration.get(f"{account}\u0000{window}", ()))
        if not samples and window is not None:
            # Another account's ratio for the SAME window is a far better
            # guess than this account's ratio for a different one: window size
            # dominates, plan size only scales it.
            for key, pairs in self._calibration.items():
                if key.endswith(f"\u0000{window}"):
                    samples.extend(pairs)
        if not samples and window is None:
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
            "schemaVersion": _CALIBRATION_SCHEMA,
            # Keyed "account|window": the ratio belongs to a window, not to
            # an account, and a cache that forgot which would restore the same
            # mixing bug it was written to fix.
            "windows": {
                key.replace("\u0000", "|"): [[d, t] for d, t in pairs]
                for key, pairs in self._calibration.items()
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
        if state.get("schemaVersion") != _CALIBRATION_SCHEMA:
            # Version 1 divided a per-model window's percent by the machine's
            # TOTAL tokens, so its constants carry whatever mix was running
            # when they were measured. There is no way to unmix them; they are
            # dropped rather than restored wrong.
            return
        windows = state.get("windows")
        if not isinstance(windows, dict):
            # Pre-fix caches keyed by account alone mixed two window sizes
            # into one ratio; there is no way to unmix them, so they are
            # dropped rather than restored wrong.
            return
        for label, pairs in windows.items():
            if not isinstance(label, str) or not isinstance(pairs, list):
                continue
            account = label.replace("|", "\u0000")
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
        self,
        account: str,
        window: str | None = None,
        *,
        window_s: float = INSTANT_WINDOW_S,
    ) -> BurnEstimate:
        """Current burn rate for ONE window of ``account``.

        ``window`` names which utilization the rate is expressed in — the
        caller's binding window, normally. Without it the tracker falls back
        to whatever it has, which is only meaningful on a fleet whose windows
        happen to be the same size.
        """
        # Matched to the filter `observe` calibrated with — the constant and
        # the rate it multiplies have to count the same tokens, or the product
        # is two different measurements multiplied together.
        model_filter = window_model_filter(window)
        tokens_per_s = self.sensor.tokens_per_s(window_s, model_filter)
        k = self.pct_per_token(account, window)
        if k is not None:
            return BurnEstimate(
                pct_per_s=k * tokens_per_s,
                source="local",
                calibrated=True,
                # The MACHINE's rate, not the filtered one: this field exists
                # to show that work is happening at all, and an idle Fable
                # window must not make a busy machine look stopped.
                tokens_per_s=self.sensor.tokens_per_s(window_s),
            )
        history = self._observations.get(f"{account}\u0000{window}") if window else None
        if history and len(history) == 2:
            (t0, p0), (t1, p1) = history[0], history[1]
            span = t1 - t0
            if span > 0 and p1 >= p0:
                return BurnEstimate(
                    pct_per_s=(p1 - p0) / span,
                    source="api",
                    calibrated=False,
                    tokens_per_s=self.sensor.tokens_per_s(window_s),
                )
        return BurnEstimate(tokens_per_s=self.sensor.tokens_per_s(window_s))

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
