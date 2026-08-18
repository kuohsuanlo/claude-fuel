"""The pet: Beep's head, extracted pixel by pixel from the squad portrait.

WHY A PET. The screen recomputes several times a second and a static figure
looks identical whether it is measuring or wedged. The pet settles that, and
it says one more thing: whether any work is actually happening. Awake means
tokens are being spent on this machine; asleep means nothing is.

WHY A HEAD. The full standing figure was 21x56 — 28 text rows of a status
indicator, which is most of a screen and stopped being cute somewhere around
row twelve. The head alone is 11 rows and reads better small.

EVERY CONSECUTIVE FRAME DIFFERS, and that is a hard requirement rather than a
nicety. An earlier cycle repeated each pose twice; the frame timer ran at
4.5 per second while the picture changed 1.2 times per second, and the
animation looked like it was stuck on a one-second tick. The visible rate is
the rate at which the POSE changes, never the rate the timer fires.

The bob is 0-1-2-1 pixels — a rigid translation of the whole head. Nothing is
scaled or cropped, so the head keeps its exact size in every frame.

SLEEP IS A LINE, NOT AN ABSENCE. The eye socket fills with face tone and one
dark row is drawn across it: blanking the socket alone makes him look eyeless
rather than asleep. The floating zZzZ is drawn by the view as text beside him,
because letters at 16 pixels wide are unreadable as pixels.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

#: Every colour of the source portrait, ordered dark to light.
_PALETTE = {
    "a": "#000000",
    "b": "#52452a",
    "c": "#736446",
    "d": "#9b875e",
    "e": "#aea184",
    "f": "#c5b595",
    "g": "#d1cdc4",
}

#: Tokens are being spent: eyes open, breathing.
AWAKE = Sprite(palette=_PALETTE, frames=(
    (
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeaaaeegg..",
        "faeeeeaffaaeggg.",
        "aaccccaaabbeggg.",
        "aaeeeedaabbeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
        "................",
    ),
    (
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeaaaeegg..",
        "faeeeeaffaaeggg.",
        "aaccccaaabbeggg.",
        "aaeeeedaabbeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
    ),
    (
        "................",
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeaaaeegg..",
        "faeeeeaffaaeggg.",
        "aaccccaaabbeggg.",
        "aaeeeedaabbeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
    ),
    (
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeaaaeegg..",
        "faeeeeaffaaeggg.",
        "aaccccaaabbeggg.",
        "aaeeeedaabbeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
    ),
))

#: Nothing burning: eyes shut to a line, still breathing.
SLEEPING = Sprite(palette=_PALETTE, frames=(
    (
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeeeeeegg..",
        "faeeeeaaaaaeggg.",
        "aacccceeeeeeggg.",
        "aaeeeeeeeeeeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
        "................",
    ),
    (
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeeeeeegg..",
        "faeeeeaaaaaeggg.",
        "aacccceeeeeeggg.",
        "aaeeeeeeeeeeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
    ),
    (
        "................",
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeeeeeegg..",
        "faeeeeaaaaaeggg.",
        "aacccceeeeeeggg.",
        "aaeeeeeeeeeeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
    ),
    (
        "................",
        "..eeeeeedd......",
        ".eeeeeedddcd....",
        ".eeeeedddddccb..",
        ".eeeeeedddebbcc.",
        ".eeeeeedddeeaacb",
        ".deeeeedddeeabbb",
        ".eeeeeedddff....",
        ".eeeeeedddfg....",
        ".eeeeeedddggg...",
        ".eeeeeedddggg...",
        ".eeeeeedddddgg..",
        "aaeeeeeeeeeegg..",
        "faeeeeaaaaaeggg.",
        "aacccceeeeeeggg.",
        "aaeeeeeeeeeeggg.",
        ".eeeeeeddeeeggg.",
        ".eeeeeeddgefff..",
        ".bcccbbeeffff...",
        "................",
        "................",
        "................",
    ),
))
