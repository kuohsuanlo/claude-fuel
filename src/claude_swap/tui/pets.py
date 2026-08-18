"""The pet: Beep, extracted pixel-for-pixel and rigged to walk.

WHY A PET. The screen recomputes every second, and a grid of static figures
looks identical whether it is measuring or wedged. The pet settles that and
nothing else — which is why `h` hides the engine's commentary but never hides
the pet.

WHO. Beep, the Southern Hive Prince: the tall narrow head, two dark compound
eyes, reedy limbs. Kenshi's most-loved character, and cheerful where the rest
of that world is not.

NOT DRAWN — EXTRACTED. The reference is a 448x448 PNG that is really a 64x64
sprite scaled 7x. The block size came out of its run lengths, the figure was
separated from the "BEEP" lettering beside it by column occupancy, and every
one of its 17 colours is carried through unquantised. Reduction to 11x20
takes each target cell's MODE colour and keeps the cell as soon as a third of
it is opaque — averaging turns a one-pixel arm into a smear, and a majority
rule deletes those limbs outright.

RIGGED, NOT REDRAWN. The figure was cut into head, torso, both arms and both
legs as pixel SETS rather than boxes (a box around a swinging arm drags torso
pixels with it), and each frame composites those parts at an offset. So the
animation is the same body moving, and a change to the artwork flows into
every frame.

THE GAIT. Beep faces the viewer, so the walk is legs apart, together, apart,
with the planted leg at full length and the swinging leg raised and pulled in;
the body and head rise on the passing frames, where a real gait is highest,
and the arms swing opposite the legs. The swing is deliberately exaggerated:
the reduction eats a one-pixel step, and caricature beats fidelity when the
alternative is no visible motion at all.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

#: Every colour from the source sprite, ordered dark to light.
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

#: Standing: a blink, then a shift of weight from one leg to the other. Alive,
#: but not working — the contrast with the walk is what carries the state.
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
        "..h..one..h",
        "bh..goqo..h",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "...mm..gki.",
        "...g....m..",
        "...g....g..",
        "...b....b..",
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

#: Working: the walk cycle. Faster-reading than the idle by design, so "the
#: engine is armed" is visible from across the room.
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
        "..h..one..h",
        "bh..goqo..m",
        "...gcoqlg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkkgkki.",
        "..m.m....gk",
        "..g.......m",
        "..g.......g",
        "..b........",
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
        "..hmeehee.m",
        "bh...one..m",
        "....goqo..h",
        "...gcoqlg.m",
        "...ggccccgb",
        "...gkggkki.",
        "...gkkgkki.",
        "....mgki...",
        "......m....",
        "......g....",
        "......b....",
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
        "....m..gki.",
        "....g...m..",
        "....g...g..",
        "....b...b..",
    ),
    ),
)
