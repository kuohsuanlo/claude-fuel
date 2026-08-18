"""The pet: Beep, awake or tucked up in bed.

WHY A PET. The screen recomputes several times a second and a static figure
looks identical whether it is measuring or wedged. The pet settles that, and
says one thing the numbers cannot: whether a burn reading of zero means idle
or broken. Awake = tokens are being spent on this machine. Asleep = none are.

THE HEAD IS EXTRACTED, THE BED IS DRAWN. The head comes pixel by pixel from
the squad portrait — all its own colours, nothing quantised, nothing scaled.
The pillow, blanket and the body under it are drawn, because the source has no
such pose and a chibi body under a blanket is more readable at this size than
any real one would be.

HIS FACE STAYS TOWARD THE VIEWER WHILE HE SLEEPS. Rotating the head ninety
degrees to lie him down was tried and the face stopped being a face — at
sixteen pixels there is not enough of it to survive a rotation. The reference
does the same thing: lying in bed, face still to camera.

SLEEP IS A LINE, NOT AN ABSENCE. The eye socket fills with face tone and one
dark row is drawn across it; blanking the socket alone makes him look eyeless
rather than asleep. The blanket hem stops BELOW the eyes for the same reason —
it is bedding, not a mask.

EVERY CONSECUTIVE FRAME DIFFERS. The visible rate is the rate the POSE
changes, never the rate the timer fires: an earlier cycle repeated each pose
and a 4.5 fps timer produced 1.2 picture changes a second.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

#: The head's own colours, plus the linen and blue of the bedding.
_PALETTE = {
    "a": "#000000",
    "b": "#52452a",
    "c": "#34507e",
    "d": "#736446",
    "e": "#4a6ea8",
    "f": "#6086c0",
    "g": "#9b875e",
    "h": "#aea184",
    "i": "#c5b595",
    "j": "#cbcbc7",
    "k": "#d1cdc4",
    "l": "#f0f0ec",
}

#: Tokens are being spent: eyes open, breathing.
AWAKE = Sprite(palette=_PALETTE, frames=(
    (
        "........hhhhhhgg..............",
        ".......hhhhhhgggdg............",
        ".......hhhhhgggggddb..........",
        ".......hhhhhhggghbbdd.........",
        ".......hhhhhhggghhaadb........",
        ".......ghhhhhggghhabbb........",
        ".......hhhhhhgggii............",
        ".......hhhhhhgggik............",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggggkk..........",
        "......aahhhhhaaahhkk..........",
        "......iahhhhaiiaahkkk.........",
        "......aaddddaaabbhkkk.........",
        "......aahhhhgaabbhkkk.........",
        ".......hhhhhhgghhhkkk.........",
        ".......hhhhhhggkhiii..........",
        ".......bdddbbhhiiii...........",
        "..............................",
        "..............................",
        "..............................",
        "..............................",
    ),
    (
        "..............................",
        "........hhhhhhgg..............",
        ".......hhhhhhgggdg............",
        ".......hhhhhgggggddb..........",
        ".......hhhhhhggghbbdd.........",
        ".......hhhhhhggghhaadb........",
        ".......ghhhhhggghhabbb........",
        ".......hhhhhhgggii............",
        ".......hhhhhhgggik............",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggggkk..........",
        "......aahhhhhaaahhkk..........",
        "......iahhhhaiiaahkkk.........",
        "......aaddddaaabbhkkk.........",
        "......aahhhhgaabbhkkk.........",
        ".......hhhhhhgghhhkkk.........",
        ".......hhhhhhggkhiii..........",
        ".......bdddbbhhiiii...........",
        "..............................",
        "..............................",
        "..............................",
    ),
    (
        "..............................",
        "..............................",
        "........hhhhhhgg..............",
        ".......hhhhhhgggdg............",
        ".......hhhhhgggggddb..........",
        ".......hhhhhhggghbbdd.........",
        ".......hhhhhhggghhaadb........",
        ".......ghhhhhggghhabbb........",
        ".......hhhhhhgggii............",
        ".......hhhhhhgggik............",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggggkk..........",
        "......aahhhhhaaahhkk..........",
        "......iahhhhaiiaahkkk.........",
        "......aaddddaaabbhkkk.........",
        "......aahhhhgaabbhkkk.........",
        ".......hhhhhhgghhhkkk.........",
        ".......hhhhhhggkhiii..........",
        ".......bdddbbhhiiii...........",
        "..............................",
        "..............................",
    ),
    (
        "..............................",
        "........hhhhhhgg..............",
        ".......hhhhhhgggdg............",
        ".......hhhhhgggggddb..........",
        ".......hhhhhhggghbbdd.........",
        ".......hhhhhhggghhaadb........",
        ".......ghhhhhggghhabbb........",
        ".......hhhhhhgggii............",
        ".......hhhhhhgggik............",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggkkk...........",
        ".......hhhhhhgggggkk..........",
        "......aahhhhhaaahhkk..........",
        "......iahhhhaiiaahkkk.........",
        "......aaddddaaabbhkkk.........",
        "......aahhhhgaabbhkkk.........",
        ".......hhhhhhgghhhkkk.........",
        ".......hhhhhhggkhiii..........",
        ".......bdddbbhhiiii...........",
        "..............................",
        "..............................",
        "..............................",
    ),
))

#: Nothing burning: in bed, eyes shut, the blanket rising and falling.
SLEEPING = Sprite(palette=_PALETTE, frames=(
    (
        "..hhhhhhggghbbdd..............",
        "..hhhhhhggghhaadb.............",
        "..ghhhhhggghhabbb.............",
        "..hhhhhhgggii.................",
        "..hhhhhhgggik.................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggggkk...............",
        ".aahhhhhhhhhhkk...............",
        ".iahhhhaaaaahkkk..............",
        ".aaddddhhhhhhkkk..............",
        ".aahhhhhhhhhhkkkllll..........",
        "llhhhhhhgghhhkkklllll.........",
        "llhhhhhhggkhiiilllllllllllllll",
        "llbdddbbhhiiiillllffffffffffff",
        "lllllllllllllllllleeeceeeeecee",
        "lllllllllllllllllleeceeeeeceee",
        "ffffffffffffffffffeceeeeeceeee",
        "ceeeeeceeeeeceeeeeceeeeeceeeee",
        "eeeeeceeeeeceeeeeceeeeeceeeeec",
        "eeeeceeeeeceeeeeceeeeeceeeeece",
        "eeeceeeeeceeeeeceeeeeceeeeecee",
    ),
    (
        "..hhhhhhggghbbdd..............",
        "..hhhhhhggghhaadb.............",
        "..ghhhhhggghhabbb.............",
        "..hhhhhhgggii.................",
        "..hhhhhhgggik.................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggggkk...............",
        ".aahhhhhhhhhhkk...............",
        ".iahhhhaaaaahkkk..............",
        ".aaddddhhhhhhkkk..............",
        ".aahhhhhhhhhhkkkllll..........",
        "llhhhhhhgghhhkkklllll.........",
        "llhhhhhhggkhiiillllll.........",
        "llbdddbbhhiiiillllllllllllllll",
        "llllllllllllllllllffffffffffff",
        "jjjjjjjjjjjjjjjjjjeeceeeeeceee",
        "lllllllllllllllllleceeeeeceeee",
        "ffffffffffffffffffceeeeeceeeee",
        "eeeeeceeeeeceeeeeceeeeeceeeeec",
        "eeeeceeeeeceeeeeceeeeeceeeeece",
        "eeeceeeeeceeeeeceeeeeceeeeecee",
    ),
    (
        "..hhhhhgggggddb...............",
        "..hhhhhhggghbbdd..............",
        "..hhhhhhggghhaadb.............",
        "..ghhhhhggghhabbb.............",
        "..hhhhhhgggii.................",
        "..hhhhhhgggik.................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggggkk...............",
        ".aahhhhhhhhhhkk...............",
        ".iahhhhaaaaahkkk..............",
        ".aaddddhhhhhhkkkllll..........",
        "laahhhhhhhhhhkkklllll.........",
        "llhhhhhhgghhhkkklllll.........",
        "llhhhhhhggkhiiilllllllllllllll",
        "llbdddbbhhiiiillllffffffffffff",
        "jjjjjjjjjjjjjjjjjjeeceeeeeceee",
        "lllllllllllllllllleceeeeeceeee",
        "ffffffffffffffffffceeeeeceeeee",
        "eeeeeceeeeeceeeeeceeeeeceeeeec",
        "eeeeceeeeeceeeeeceeeeeceeeeece",
        "eeeceeeeeceeeeeceeeeeceeeeecee",
    ),
    (
        "..hhhhhgggggddb...............",
        "..hhhhhhggghbbdd..............",
        "..hhhhhhggghhaadb.............",
        "..ghhhhhggghhabbb.............",
        "..hhhhhhgggii.................",
        "..hhhhhhgggik.................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggkkk................",
        "..hhhhhhgggggkk...............",
        ".aahhhhhhhhhhkk...............",
        ".iahhhhaaaaahkkk..............",
        ".aaddddhhhhhhkkkllll..........",
        "laahhhhhhhhhhkkklllll.........",
        "llhhhhhhgghhhkkkllllllllllllll",
        "llhhhhhhggkhiiilllffffffffffff",
        "llbdddbbhhiiiilllleeeceeeeecee",
        "lllllllllllllllllleeceeeeeceee",
        "ffffffffffffffffffeceeeeeceeee",
        "ceeeeeceeeeeceeeeeceeeeeceeeee",
        "eeeeeceeeeeceeeeeceeeeeceeeeec",
        "eeeeceeeeeceeeeeceeeeeceeeeece",
        "eeeceeeeeceeeeeceeeeeceeeeecee",
    ),
))
