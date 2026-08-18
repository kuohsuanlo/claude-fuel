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
separated from the "BEEP" lettering beside it by column occupancy. The native
figure is 11x41 in 17 colours, all carried through unquantised, and it is
dumped pixel by pixel in ``art/beep/PIXELS.txt`` next to an annotated grid.

PROPORTION IS THE WHOLE PROBLEM AT THIS SIZE. Beep is 1:3.7, and an earlier
pass rendered him 11x20 — which halved his height and made him read as a
different, squatter character. He is drawn at 11x30 here: 73% of true
height, chosen against the alternative of spending 21 text rows on a status
indicator. The reduction takes each target cell's MODE colour and keeps a cell
as soon as a third of it is opaque — averaging turns a one-pixel arm into a
smear, and a majority rule deletes those limbs outright.

LIMBS ARE IRREGULAR, so they are segmented one pixel at a time rather than by
rectangle: the left arm is a four-pixel diagonal jutting down-left, the right
arm a straight eight-pixel column, and both legs are exactly ONE pixel wide.
Boxes got this wrong twice — the left arm's shoulder stayed behind so the arm
moved as a detached stick, and the right leg's box swept up the corner of the
skirt, which flew off with every step. Each limb is only the pixels FREE of the
body outline; the shoulder and hip stay with the torso, because a limb carrying
its own socket tears a hole in the silhouette the moment it moves.

THE GAIT. Beep faces the viewer, so the walk is legs apart, together, apart:
the planted leg keeps full length at its rest column while the swinging leg
pulls toward centre and shortens FROM THE FOOT — cropping from the bottom keeps
the hip attached while the foot leaves the ground, which is what the eye reads
as a step. Body and head rise on the passing frames, where a real gait is
highest, and the arms counter-swing.

Frame timing lives in the view (``SPRITE_FRAME_S``) and is derived from the
clock, so every frame is shown for exactly the same length of time however
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
WATCHING = Sprite(palette=_PALETTE, frames=(
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...ahhal...",
        "...ahham...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheeh",
        "...m.ene..m",
        "..h..one..h",
        "bh...oqo..h",
        "....goqo..m",
        "...gcoql..b",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....m..gki.",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...hhhhl...",
        "...hhhhm...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheeh",
        "...m.ene..m",
        "..h..one..h",
        "bh...oqo..h",
        "....goqo..m",
        "...gcoql..b",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....m..gki.",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...ahhal...",
        "...ahham...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheeh",
        ".....ene..m",
        "...m.one..h",
        ".hh..oqo..h",
        "b...goqo..m",
        "...gcoql..b",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....m..gki.",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...hhhhl...",
        "...hhhhm...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheeh",
        "...m.ene..m",
        "..h..one..m",
        "bh...oqo..h",
        "....goqo..h",
        "...gcoql..m",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....m..gki.",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
))

#: Working: the walk cycle.
WORKING = Sprite(palette=_PALETTE, frames=(
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...ahhal...",
        "...ahham...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheem",
        ".....ene..h",
        "...m.one..h",
        ".hh..oqo..h",
        "b...goqo..b",
        "...gcoql..b",
        "...gccolgg.",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....m..gki.",
        "....g..m...",
        "....g..m...",
        "....g..g...",
        "....b......",
        "....b......",
    ),
    (
        "...hhhfl...",
        "...hhhfl...",
        "...hhhhl...",
        "...ahham...",
        "...hhhpm...",
        "...hhhpm...",
        "....ffmm...",
        "......ll...",
        "....jhllhj.",
        "...jhhhhhhj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...eehhheeh",
        "...meehee.m",
        "..h..one..h",
        ".h...oqo..h",
        "b...goqo..h",
        "...gcoql..b",
        "...gccolg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkggkki.",
        "....kkggki.",
        "....m..gki.",
        "....m...l..",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
    (
        "...hhhfddd.",
        "...hhhfl...",
        "...hhhfl...",
        "...ahhal...",
        "...ahham...",
        "...hhhpm...",
        "....hhmm...",
        "....ffmm...",
        "......ll...",
        "....jhhhhj.",
        "...jhhhhhhj",
        "...ehhhhheh",
        "...ehhhhheh",
        "...eehhheeh",
        "..h..ene..m",
        ".h...one..m",
        "b....oqo..h",
        "....goqo..h",
        "...gcoql..m",
        "...gccolggb",
        "...gkggggi.",
        "...gkggkki.",
        "...gkkgkki.",
        "....kkggki.",
        "....mm.gki.",
        ".....g..m..",
        ".....g..m..",
        ".....g..g..",
        "........m..",
        "........b..",
    ),
    (
        "...hhhfl...",
        "...hhhfl...",
        "...hhhhl...",
        "...ahham...",
        "...hhhpm...",
        "...hhhpm...",
        "....ffmm...",
        "......ll...",
        "....jhllhj.",
        "...jhhhhhhj",
        "...ehhhhhhh",
        "...ehhhhheh",
        "...eehhheeh",
        "...meehee.m",
        "..h..one..h",
        ".h...oqo..h",
        "b...goqo..h",
        "...gcoql..b",
        "...gccolg.b",
        "...ggccccg.",
        "...gkggkki.",
        "...gkggkki.",
        "....kkggki.",
        "....m..gki.",
        "....m...l..",
        "....g...m..",
        "....g...m..",
        "....g...g..",
        "....b...m..",
        "....b...b..",
    ),
))
