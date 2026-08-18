"""The pet: Beep, extracted pixel by pixel and rigged to walk.

WHY A PET. The screen recomputes every second, and a grid of static figures
looks identical whether it is measuring or wedged. The pet settles that and
nothing else — which is why `h` hides the engine's commentary but never the
pet.

WHO. Beep, the Southern Hive Prince: tall narrow head, two dark compound eyes,
reedy limbs. Kenshi's most-loved character, and cheerful where the rest of that
world is not.

EXTRACTED, NOT DRAWN. The reference is a 448x448 PNG that is really a 64x64
sprite scaled 7x; the block size came out of its run lengths and the figure was
separated from the "BEEP" lettering beside it by column occupancy. All 17 of
its colours survive unquantised. The reduction to 11x20 takes each target
cell's MODE colour and keeps a cell as soon as a third of it is opaque —
averaging turns a one-pixel arm into a smear, and a majority rule deletes those
limbs outright.

SEGMENTED OFF THE COORDINATE GRID, one pixel at a time, and the limbs are
irregular: the left arm is a four-pixel diagonal jutting down-left, the right
arm a straight eight-pixel column, and both legs are ONE pixel wide. An earlier
pass took rectangles instead and got two things wrong — the left arm's shoulder
stayed behind so the arm moved as a detached stick, and the right leg's box
swept up the corner of the skirt, which then flew off with each step.

Each limb is the pixels that are FREE of the body outline; the shoulder and hip
stay with the torso, because a limb that carries its own socket tears a hole in
the silhouette the moment it moves.

THE GAIT. Beep faces the viewer, so the walk is legs apart, together, apart:
the planted leg keeps its full length at its rest column while the swinging leg
pulls toward centre and shortens FROM THE FOOT — cropping from the bottom keeps
the hip attached while the foot leaves the ground, which is what the eye reads
as a step. Body and head rise on the passing frames, where a real gait is
highest, and the arms counter-swing.

The frame period is fixed in the view (``SPRITE_FRAME_S``) and derived from the
clock, so every frame is shown for the same length of time regardless of how
often the screen repaints.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

#: Every colour of the source sprite, ordered dark to light.
_PALETTE = {
    "a": "#333120",
    "b": "#786450",
    "c": "#8c755e",
    "d": "#96896e",
    "e": "#9c8e72",
    "f": "#a19275",
    "g": "#ad9174",
    "h": "#a8997b",
    "i": "#b8997b",
    "j": "#b3a282",
    "k": "#c2a282",
    "l": "#bda8a0",
    "m": "#bfaaa3",
    "n": "#c2b08d",
    "o": "#c6b0a8",
    "p": "#ccb5ad",
    "q": "#d1bab2",
}

#: Standing: a blink, then a shift of weight. Alive, but not working — the
#: contrast against the walk is what carries the armed/disarmed state.
WATCHING = Sprite(
    palette=_PALETTE,
    frames=(
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...ahhal...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...meehee.m",
        ".hh..one..h",
        "b...goqo..h",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "....m..gki.",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhhl...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...meehee.m",
        ".hh..one..h",
        "b...goqo..h",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "....m..gki.",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...ahhal...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...meehee.m",
        "..hm.one..h",
        "bh..goqo..h",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "....m..gki.",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhhl...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...meehee.m",
        ".hh..one..m",
        "b...goqo..h",
        "...gcoqlg.m",
        "...ggccccgb",
        "...gkggkki.",
        "...gkkgkki.",
        "....m..gki.",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    ),
)

#: Working: the walk cycle.
WORKING = Sprite(
    palette=_PALETTE,
    frames=(
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...ahhal...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...meehee.m",
        "..hm.one..h",
        "bh..goqo..m",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "....m..gki.",
        "....g..m...",
        "....g..g...",
        "....b......",
    ),
    (
        "...hhhfl...",
        "...hhhhl...",
        "...ahham...",
        "...hhhpm...",
        "....ffmm...",
        "....jhllhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...eehhheeh",
        "..hm.ene..m",
        "bh...oqo..h",
        "...ggoqo..m",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "....kkggki.",
        "....m...l..",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...ahhal...",
        "...hhhpm...",
        "....hhmm...",
        "......ll...",
        "...jjhhhhjj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "..hmeehee.m",
        "bh...one..m",
        "....goqo..h",
        "...gcoqlg.m",
        "...ggccccgb",
        "...gkggkki.",
        "...gkkgkki.",
        "....mm.gki.",
        ".....g..m..",
        ".....g..g..",
        "........b..",
    ),
    (
        "...hhhfl...",
        "...hhhhl...",
        "...ahham...",
        "...hhhpm...",
        "....ffmm...",
        "....jhllhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...eehhheeh",
        "..hm.ene..m",
        "bh...oqo..h",
        "...ggoqo..m",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "....kkggki.",
        "....m...l..",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    ),
)
