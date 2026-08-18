"""The pet: Beep, traced pixel-for-pixel from the source art.

WHY A PET AT ALL. The screen recomputes every second, and a grid of static
figures looks identical whether it is measuring or wedged. The pet settles
that and nothing more — which is why `h` hides the engine's commentary but
never hides the pet.

WHO. Beep, the Southern Hive Prince: the tall narrow head, the two dark
compound eyes, the reedy limbs. Kenshi's most-loved character, and cheerful
where the rest of that world is not.

NOT DRAWN BY HAND. The reference is a 448x448 PNG that is really a 64x64
pixel sprite scaled 7x; the block size was recovered from the run lengths, the
figure separated from the "BEEP" lettering beside it by column occupancy, and
the result reduced to 10x16 by taking each target cell's MODE colour rather
than its average. Mode matters at this size: averaging turns a one-pixel arm
into a faint smear, and a cell is kept as soon as a third of it is opaque,
because a majority rule erases those limbs outright. Fifteen colours were then
banded by luminance into five inks plus the dashboard's accent.

Ten pixels wide by sixteen tall is eight text rows, and Beep is meant to be
tall and thin — squashing him to four rows was tried and lost the head, which
is the whole silhouette.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

_PALETTE = {
    "K": "#333120",  # the compound eyes
    "D": "#786450",  # deepest shade — feet, far arm
    "B": "#95846a",  # mid shade
    "M": "#ad987a",  # the chitin body tone
    "L": "#c4aca0",  # highlight
    "O": "#d7875f",  # fuel drawn in (the dashboard accent)
}

_BASE = (
    "...MMMMBB.",
    "...MMMML..",
    "...KMMKL..",
    "...MMMLL..",
    "......LL..",
    "...MMMMMMM",
    "...BMMMMMB",
    "...LBBMBBL",
    "DMM..LLL.M",
    "...MMLLL.L",
    "...MBBLLMD",
    "...MLMMLLM",
    "...MLLMMLM",
    "....L...L.",
    "....M...M.",
    "....D...D.",
)


def _edit(rows: tuple[str, ...], **replacements: str) -> tuple[str, ...]:
    """A frame as deltas from the base pose, keyed ``r<row index>``.

    Frames are written as edits rather than as whole grids so a change to the
    figure lands everywhere at once, and so the diff of an animation tweak
    shows the tweak instead of sixteen unchanged lines.
    """
    out = list(rows)
    for key, value in replacements.items():
        out[int(key[1:])] = value
    return tuple(out)


#: Idle: Beep blinks, and his arm sags a row. Awake, not working.
WATCHING = Sprite(
    palette=_PALETTE,
    frames=(
        _BASE,
        _edit(_BASE, r2="...MMMML.."),
        _edit(_BASE, r8=".MM..LLL.M", r9="D..MMLLL.L"),
    ),
)

#: Working: the arm comes up and fuel drifts in from the left, one pixel per
#: frame, until it reaches him.
WORKING = Sprite(
    palette=_PALETTE,
    frames=(
        _edit(_BASE, r4="D.....LL..", r7="O..LBBMBBL", r8=".MM..LLL.M"),
        _edit(_BASE, r4="D.....LL..", r7=".O.LBBMBBL", r8=".MM..LLL.M"),
        _edit(_BASE, r4="D.....LL..", r7="..OLBBMBBL", r8=".MM..LLL.M"),
    ),
)
