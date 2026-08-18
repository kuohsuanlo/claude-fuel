"""The sky above the pet: sun or moon by the clock, and the real weather.

DRAWN PER FRAME, not stored as sprites. The sun's position follows the local
clock and the rain falls on its own phase, so there is no fixed set of frames
to author — the picture is computed each time and handed to the same
half-block cell rule the fixed sprites use, which is what keeps a procedural
drawing from picking up the inherited-foreground fringe.

WHAT IT SHOWS, in order of how much it earns its space:

- WHETHER IT IS DARK OUTSIDE, from the machine's own clock. That is the one
  fact this panel can always state truthfully, with no network at all.
- WHERE in the day or night, as the body's height along an arc. Low at dawn
  and dusk, high at noon and midnight.
- THE ACTUAL WEATHER, when a reading is available: cloud, rain, snow or
  storm. Without one it draws a clear sky and says so in the caption rather
  than presenting a default as a measurement.

The glow and the drifting clouds are on their own phases, deliberately slower
than the pet's animation, because a sky that flickers at four frames a second
reads as a fault rather than as weather.
"""

from __future__ import annotations

import math

from rich.text import Text

from claude_swap.sky import SkyState, arc_position, day_fraction
from claude_swap.tui.sprite import render_pixels

#: Panel size in PIXELS. Eight rows of pixels is four rows of text — enough
#: for a body, a glow around it and something falling past.
SKY_W, SKY_H = 34, 8

# Slower than the pet on purpose: weather that twitches at animation rate
# reads as a fault. Every Nth pet frame advances the sky by one.
SKY_SLOWDOWN = 3

# The ground the WHOLE panel is painted on, pet included: one scene rather
# than a picture of weather sitting above an unrelated cut-out. Overcast and
# wet days are flatter and greyer than clear ones, which is most of what
# weather looks like from indoors.
_GROUND = {
    (True, "clear"): "#3d6191",
    (True, "cloud"): "#55617a",
    (True, "rain"): "#454f63",
    (True, "snow"): "#5d6879",
    (True, "storm"): "#333a49",
    (False, "clear"): "#141a2e",
    (False, "cloud"): "#1c2133",
    (False, "rain"): "#181d2b",
    (False, "snow"): "#232838",
    (False, "storm"): "#12151f",
}
_DAY_SKY = _GROUND[(True, "clear")]
_NIGHT_SKY = _GROUND[(False, "clear")]
_SUN = "#ffcf5c"
_SUN_CORE = "#fff2b8"
_RAY = "#e8a83a"
_MOON = "#e8e6dc"
_MOON_SHADE = "#b9b7ae"
_CLOUD = "#6d768c"
_CLOUD_LIT = "#8c94a8"
_RAIN = "#7fa8d8"
_SNOW = "#e6f0ff"
_BOLT = "#ffe98a"


def ground_colour(state: SkyState) -> str:
    """The scene's base colour — the pet stands on this too."""
    return _GROUND.get((bool(state.is_day), state.kind), _DAY_SKY)


def _blank(width: int, height: int) -> list[list[str | None]]:
    return [[None] * width for _ in range(height)]


def _disc(buf, cx: int, cy: int, radius: float, colour: str, core: str | None = None):
    """A filled circle, clipped to the buffer."""
    height, width = len(buf), len(buf[0])
    for y in range(height):
        for x in range(width):
            distance = math.hypot(x - cx, (y - cy) * 1.0)
            if distance <= radius:
                buf[y][x] = core if (core and distance <= radius * 0.45) else colour


def _cloud(buf, cx: int, cy: int, colour: str, lit: str):
    """A small three-lobe puff with a lit top edge.

    Lobes rather than a rectangle: at this size a blob with a flat top reads
    as a building. Kept under two pixels tall so it cannot swallow the moon it
    drifts past.
    """
    height, width = len(buf), len(buf[0])
    for dx, r in ((-2, 1.2), (0, 1.6), (2, 1.3)):
        _disc(buf, cx + dx, cy, r, colour)
    for x in range(max(0, cx - 3), min(width, cx + 4)):
        y = cy - 1
        if 0 <= y < height and buf[y][x] == colour:
            buf[y][x] = lit


def sky_pixels(
    state: SkyState, phase: int, *, fraction: float | None = None
) -> list[list[str | None]]:
    """The panel as a pixel buffer, for ``phase`` steps of its own clock.

    ``fraction`` overrides the time of day; the default reads the local clock,
    which is the whole point of the panel. It exists so the drawing can be
    checked at noon without waiting until noon.
    """
    buf = _blank(SKY_W, SKY_H)
    if fraction is None:
        fraction = day_fraction()
    is_day = state.is_day
    ground = ground_colour(state)
    for y in range(SKY_H):
        for x in range(SKY_W):
            buf[y][x] = ground

    # The body rides an arc: low at the horizons, high in the middle. Height
    # is what tells dawn from noon; a body that only slid sideways would look
    # the same at both.
    across = arc_position(fraction, is_day=is_day)
    cx = int(round(2 + across * (SKY_W - 5)))
    cy = int(round(6.0 - math.sin(across * math.pi) * 4.0))

    # WEATHER FIRST, BODY LAST. Drawn the other way round the moon vanished
    # behind the first cloud that drifted over it, and a sky whose only
    # subject disappears is not reporting anything.
    if state.kind in ("cloud", "rain", "snow", "storm"):
        for index in range(3 if state.kind == "cloud" else 2):
            drift = (phase + index * 13) % (SKY_W + 10) - 5
            _cloud(buf, drift, 2 + (index % 2) * 3, _CLOUD, _CLOUD_LIT)
    elif not is_day:
        # Clear night still gets cloud, because a bare disc on a flat field
        # reads as a hole punched in the panel — but only below the moon's
        # own band, so it never hides the thing it frames.
        for index in range(2):
            drift = (phase + index * 17) % (SKY_W + 10) - 5
            _cloud(buf, drift, max(cy + 3, 5) - index, _CLOUD, _CLOUD_LIT)

    if state.kind in ("rain", "storm"):
        # Streaks, not dots. Rain drawn as scattered pixels reads as static;
        # a two-pixel vertical mark falling on a shared phase reads as rain.
        for index in range(9):
            x = (index * 4 + (phase // 2)) % SKY_W
            y = (index * 3 + phase * 2) % (SKY_H + 2) - 1
            for dy in (0, 1):
                if 0 <= y + dy < SKY_H:
                    buf[y + dy][x] = _RAIN
    elif state.kind == "snow":
        for index in range(8):
            x = (index * 5 + (phase // 3)) % SKY_W
            y = (index * 3 + phase) % SKY_H
            buf[y][x] = _SNOW

    if is_day:
        # Rays pulse on the phase rather than rotating: at three pixels long a
        # rotation is a flicker, while a pulse still reads as heat.
        reach = 5 + (phase % 3)
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            for step in range(4, reach + 1):
                x = int(round(cx + math.cos(radians) * step))
                y = int(round(cy + math.sin(radians) * step * 0.6))
                if 0 <= x < SKY_W and 0 <= y < SKY_H:
                    buf[y][x] = _RAY
        _disc(buf, cx, cy, 3.0, _SUN, _SUN_CORE)
    else:
        _disc(buf, cx, cy, 3.0, _MOON, None)
        # The crescent is carved by a second disc of sky, so the moon has a
        # shape rather than being a dot.
        _disc(buf, cx + 3, cy - 1, 2.5, ground)
        for y in range(SKY_H):
            for x in range(SKY_W):
                if buf[y][x] == _MOON and (x + y) % 7 == 0:
                    buf[y][x] = _MOON_SHADE

    if state.kind == "storm" and phase % 6 == 0:
        x = SKY_W // 2
        for y in range(1, SKY_H - 1):
            if 0 <= x < SKY_W:
                buf[y][x] = _BOLT
            x += 1 if y % 2 else -1
    return buf


def sky_rows(state: SkyState, phase: int) -> list[Text]:
    """The panel alone, without the pet standing in it."""
    return render_pixels(sky_pixels(state, phase))


def scene_rows(
    state: SkyState,
    phase: int,
    pet_frame: tuple[str, ...],
    palette: dict[str, str],
    *,
    dim: bool = False,
) -> list[Text]:
    """Sky and pet as ONE picture, sharing a background.

    The pet used to be a transparent cut-out below a weather panel, so the
    terminal showed through underneath him and the two read as unrelated
    widgets. Painting both onto the same ground makes it a scene: he is
    outside, in whatever weather is actually outside.

    Transparent pet pixels take the ground rather than being left unpainted —
    the whole point is that there is no hole around him.
    """
    ground = ground_colour(state)
    sky = sky_pixels(state, phase)
    pet_h = len(pet_frame)
    pet_w = len(pet_frame[0]) if pet_frame else 0
    width = max(SKY_W, pet_w)
    buf: list[list[str | None]] = []
    for row in sky:
        buf.append(list(row) + [ground] * (width - SKY_W))
    for y in range(pet_h):
        line: list[str | None] = []
        for x in range(width):
            key = pet_frame[y][x] if x < pet_w else "."
            line.append(ground if key == "." else palette.get(key, ground))
        buf.append(line)
    return render_pixels(buf, dim=dim)
