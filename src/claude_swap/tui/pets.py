"""The pet: a tiny Kenshi skeleton that proves the instrument is still ticking.

DELIBERATELY SMALL. The screen recomputes every second, and a grid of static
figures looks identical whether it is measuring or wedged — the pet exists to
settle that, and nothing more. Eight pixels square (four text rows) is enough
to read "there is a figure, and it is moving" at a glance, and refusing to
grow past that is what keeps it an indicator instead of a portrait competing
with the gauges above it.

It is CAT-LON in outline: the mad king of Kenshi, a Skeleton on a throne in
the Ashlands with the Falling Sun. Two details survive the shrink because they
are the only two that read at this size — a bare lit-optic head (Skeletons can
never equip headgear, so the pale head over the rusted torso IS the
silhouette) and the weapon, planted in the ground while watching and raised
while working. The state change is the weapon moving corner to corner, which
is legible with the animation paused and with no colour at all.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

_PALETTE = {
    "K": "#191417",  # void
    "M": "#8d7a63",  # worn brass — the bare skeleton head
    "R": "#8a4b2a",  # rusted ancient samurai armour
    "O": "#d7875f",  # the Falling Sun, and fuel (the dashboard accent)
    "C": "#7fe3ea",  # the lit optic
}

#: Idle: the optic blinks, the planted blade glints.
WATCHING = Sprite(
    palette=_PALETTE,
    frames=(
        (
            "..MMM...",
            ".MCKM..O",
            "..MMM..O",
            ".RRRRR.O",
            ".RRRRR.O",
            "..RRR..O",
            "..R.R..O",
            "..R.R...",
        ),
        (
            "..MMM...",
            ".MKKM..O",  # optic dark: power diverting, his machine-fault tic
            "..MMM..O",
            ".RRRRR.O",
            ".RRRRR.O",
            "..RRR..O",
            "..R.R..O",
            "..R.R...",
        ),
        (
            "..MMM...",
            ".MCKM..O",
            "..MMM..O",
            ".RRRRR.O",
            ".RRRRR.O",
            "..RRR..O",
            "..R.R..O",
            "..R.RO..",
        ),
    ),
)

#: Working: the Falling Sun is up, and fuel is drawn in from the left.
WORKING = Sprite(
    palette=_PALETTE,
    frames=(
        (
            ".....OOO",
            "..MMM.OO",
            ".MCCM.O.",
            ".RRRRR..",
            "ORRRRR..",
            "..RRR...",
            "..R.R...",
            "..R.R...",
        ),
        (
            "......OO",
            "..MMM.OO",
            ".MCCM.OO",
            ".RRRRR.O",
            ".RRRRR..",
            "O.RRR...",
            "..R.R...",
            "..R.R...",
        ),
        (
            "........",
            "..MMM..O",
            ".MCCM.OO",
            ".RRRRROO",
            ".RRRRR.O",
            "..RRR...",
            "O.R.R...",
            "..R.R...",
        ),
    ),
)
