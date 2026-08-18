# cfuel

**A fuel gauge for your Claude Code accounts.** One command opens one screen
that answers two questions: *what quota am I about to waste*, and *am I about
to hit a wall* — and, if you let it, moves you off an account before either
happens.

![cfuel](art/screenshot.png)

A fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap). The
upstream tool switches accounts and reports usage; this fork adds the parts
that answer *when* and *why* — a deadline-aware strategy, a burn rate measured
from your own transcripts, and a screen built around both. Everything upstream
still works: `cswap list`, `cswap switch`, `cswap auto`, session mode, the
menu bar.

---

## Install

Needs **Python ≥ 3.12**. `uv` fetches a suitable one, so nothing has to change
about your system Python.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # if you don't have uv
uv tool install "git+ssh://git@github.com/kuohsuanlo/claude-fuel.git"
```

Installs three commands: **`cfuel`** (the screen), plus `cswap` and
`claude-swap` (the upstream CLI, unchanged).

> The repository is private, so HTTPS installs fail — SSH is required and the
> machine needs a key with access. To update, re-run the same command with
> `--force --reinstall`; `uv tool upgrade` does not re-fetch a git source.

**Do not `uv tool install claude-swap`** — that name on PyPI is upstream, and
installing it replaces this fork.

---

## `cfuel`

```
cfuel
```

Accounts, settings and login state are shared with `cswap` — same package,
another entry point — so there is nothing to copy across.

| Key | Does |
| --- | --- |
| `a` | Auto-switching on / off (**on** at launch) |
| `↑` `↓` | Pick an account — only when auto is **off** |
| `enter` | Switch to the picked account |
| `t` then `←` `→` | Adjust the threshold — **written to settings.json** |
| `r` | Adopt the suggested threshold |
| `h` | Hide the engine's log line |
| `q` | Quit |

Arming and disarming never disturbs the display: the readings keep updating
every second either way, because you arm it *after* reading the screen, and a
toggle that reset what you were looking at would make that impossible.

### The three gauges

```
All fuel    28 pts expire within 24h    AUTO    switch at 99.7%
                  ▼ dev5 8/19 Wed 13:59 (12h)
  7d     ━━━━━━━━━╸━━━━━━━━━━━╸ ── ────────────  2 92% 4d · 1 58% 6d · 3 72% 12h
                  ▲ dev 8/25 Tue 06:59 (6d) active
```

One bar per window — session (5h), weekly (7d), and your per-model weekly
limit. Each is the **whole fleet's remaining quota for that window**, with
every account's share packed to the left, so the length of the coloured run is
how much fuel is left and the boundaries inside it are who holds it.

- **Colour is an account's identity**, ranked once by how fast its weekly quota
  is being wasted, and reused on all three bars. Green is in no danger, red
  dies first. The bars are ordered on that same axis, so the spectrum always
  runs green (left) to red (right) and a colour means the same thing on every
  row. Ranking each row by its own reset — which is what it did first — made an
  account amber on one bar and red on the next, and the colour stopped being an
  identity at all.
- **`▼` above** names the account whose quota in *that* window expires soonest;
  it is hidden when there is nothing left to lose.
- **`▲` below** names the account being spent right now.
- The gap between those two markers is the entire point of the screen.
- **Every absolute time carries its weekday** (`8/25 Tue 06:59`). A bare date
  is a lookup — is that tomorrow, the weekend, next week? — and that judgement
  is the whole reason the number is on screen.

### The burn readout

```
burn  5h     0.023%/s  ·  1% every 43s   suggested 99.5% (yours 99.9% — press r)
      7d     0.012%/s  ·  1% every 87s
      Fable  1,118 tok/s  ·  calibrating
      at this rate dev5 wastes 12 of its 47 pts
```

One rate per window, because there is no such thing as "the" burn rate — the
same tokens are a large fraction of a five-hour window and a small fraction of
a weekly one. Both forms of the rate are shown, `%/s` and seconds-per-percent,
because a threshold is a decision about *how much warning you get* and the
second form states that directly.

`r` adopts the suggested threshold, which is the highest one the current rate
can survive.

### Beep

<img src="art/beep.gif" width="300" alt="Beep mining"> <img src="art/beep-sleeping.png" width="300" alt="Beep asleep">

The pet is an instrument, not decoration. He **mines while tokens are being
spent**, and his swing rate follows the burn rate — four discrete speeds, so
the change is legible rather than a smooth drift nobody notices. He **sleeps,
eyes closed, when nothing has burned for ninety seconds**, which tells you
something no number on the screen can: whether a burn reading of zero means
idle or means broken.

He is real pixel art — extracted pixel by pixel from reference art of Kenshi's
Beep, then rigged — not characters arranged to suggest a shape. The rigging
rules that took the most work are the ones that stop him looking wrong:
fixed-height bones (head, face, chest, legs) never squash, because a body that
changes height between frames reads as a glitch rather than as motion; the
frame rate is constant, since animation that varies its own timing looks like
lag; he faces the rock he is hitting; and the pick swings from his arm.

Above him is the real sky: sun or moon placed by your local clock along an arc,
with the weather that is actually outside. It **costs no tokens** — nothing
here goes near a model. The sun is arithmetic on the clock; the weather is one
small key-less JSON request on a background thread, a few times an hour,
cached to disk. It never blocks a repaint and never raises; with no network it
draws a clear sky rather than presenting a default as a measurement. Sky and
pet share one background, so it is a scene he is standing in rather than a
cut-out pasted under a weather widget.

![Beep in three skies](art/beep.png)

*Left to right: mining under a clear noon, asleep under a crescent moon, and
mining in the rain — the same pet, three real readings.*

---

## What this fork changed

### `waste-first`, the new default strategy

Ranks accounts by **quota about to expire**: weekly headroom divided by hours
until that window resets. Upstream's default, `best`, ranks by how much is
left — which reliably parks on the account holding the *longest* deadline, the
quota in the least danger of being wasted.

Measured on a real fleet: 49 points expiring in 20 hours scored 2.4 %/h against
the active account's 0.46 %/h. `best` sat on the active account until those 49
points expired. `consume-first` ranks on the deadline alone, so it will happily
move to an account that resets sooner but has 2 points left to rescue.

The deadline axis applies only *below* the threshold. Above it a switch is an
escape, where the question is whether a landing is worth the move, so the
hysteresis margin still gates it and the deadline only orders the candidates
that clear it.

```bash
cswap config set autoswitch.strategy waste-first    # the default
cswap config set autoswitch.strategy consume-first  # soonest reset, ignoring size
cswap config set autoswitch.strategy best           # upstream: most left, no deadlines
```

### Burst guard — what makes a 99% threshold usable

The usage endpoint allows roughly **28–30 requests per rolling hour**, so the
fastest honest sample of your utilization is one point every three minutes. A
heavy parallel turn crosses ten points in that time. A threshold is a
*position*, and a position set where the average looks safe is passed long
before the next sample lands.

So the burn rate is measured locally instead, from Claude Code's own transcripts
(`~/.claude/projects/**/*.jsonl`), which carry each request's token counts and
cost nothing to read. Every session on the machine is watched, not just the one
you launched `cfuel` from. The effective trigger becomes the lower of your
threshold and the value the current rate can survive — by default a burst of 2×
for 10 seconds. **It can only ever switch earlier than you asked.**

```bash
cswap config set autoswitch.burstGuard false   # off
```

### Tokens are not percent, so the scale is measured

Weighted tokens are a cost-shaped proxy, not the provider's accounting, so the
constant relating them to window percent is never assumed — it is calibrated
against the API samples that do arrive, cached across restarts, and kept **per
account and per window**.

Both halves of that were bugs found by measuring:

- **Per window.** Calibration originally paired tokens with the *binding*
  window, and the binding window flips between 5h and 7d as the short one
  resets. Three consecutive samples of one account gave 411k, 2,148k and 348k
  tokens per percent — two different rulers averaged together, understating the
  rate roughly threefold, which is the direction that overshoots a threshold.
- **Per account.** Percent is a fraction of a *plan's* window, so pooling a 20×
  and a 5× account scales both by the fleet average.

An account switch drops the baselines, because an interval spanning a switch
charges one account's percentage with another's tokens — and an *unknown*
active account is not a switch, which is a distinction that cost a round of
silently wiped baselines to find. Note that the API reports **integer
percentages**: one percent of a weekly window is over a million weighted
tokens, so the weekly scale only calibrates after real accumulated use and
reads `API average` until it does.

### Plan sizes

The usage API reports only utilization, so 40% of a 20× plan and 40% of a 5×
plan are identical to it while being four times apart in real work. Unweighted,
one cell of a bar meant different amounts of work on the same row.

```bash
cswap config set autoswitch.accountWeights "1=20,2=5,3=5"
```

Unset accounts fall back to their measured tokens-per-percent, and to equal
weight before that is known.

### Smaller corrections worth knowing about

- **"Stranded quota" is named as such.** "Nothing is more urgent" and "the
  urgent one is unreachable" are opposite situations that both end in holding,
  and the log used to report them identically. The screenshot above catches the
  real case: *account 3 is losing quota faster but has no room to work in;
  frees up in 1h 13m*.
- **The waste projection is in weekly units.** It used to multiply the
  *binding* window's rate by the hours until a *weekly* reset, and report "all
  spendable" about quota that was certainly going to be lost.
- **An account with no reported reset can never be "the expiring one."**
  `expires —` is not a deadline, and it used to win the `▼` marker.
- **`tokens_since` is half-open**, so a sample's boundary tokens are counted
  once rather than into both intervals.
- **The suggested threshold is rounded to one decimal.** It was printed to full
  float precision — `99.86597411%` — which reads as a measurement rather than
  as a setting you are about to type in.

---

## Configuration

`settings.json` lives beside your accounts (`~/.local/share/claude-swap/` on
Linux). `cswap config` lists everything; the keys this fork adds:

| Key | Default | Meaning |
| --- | --- | --- |
| `autoswitch.strategy` | `waste-first` | `waste-first`, `consume-first`, `best` |
| `autoswitch.burstGuard` | `true` | Let the measured rate trigger early |
| `autoswitch.accountWeights` | — | Relative plan sizes, `1=20,2=5` |

The threshold you set with `t` in the TUI is written here too, so it survives
a restart.

---

## Notes

- **Switching moves every session on the machine.** They all share the active
  login, so arming auto-switch moves all of them at once.
- **The on-disk layout is unchanged from upstream** — same directory, same file
  names — so this can be installed over an existing claude-swap without
  touching your accounts, and you can go back.

## Development

```bash
git clone git@github.com:kuohsuanlo/claude-fuel.git
cd claude-fuel
uv sync
uv run pytest -q                       # 2259 tests
uv tool install --force --reinstall .  # install your working tree
```

`art/beep/` keeps the sprite extraction: `PIXELS.txt` is Beep dumped one pixel
at a time with his palette and every limb marked, and `inspect.png` is the same
magnified with coordinates. Upstream is tracked as the `upstream` remote:
`git fetch upstream && git merge upstream/main`.
