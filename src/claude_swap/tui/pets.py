"""The pet: Beep, mining while tokens burn and asleep in bed when they stop.

WHY A PET. The screen recomputes several times a second and a static figure
looks identical whether it is measuring or wedged. The pet settles that, and
says one thing the numbers cannot: whether a burn reading of zero means idle
or broken.

THE HEAD IS EXTRACTED, EVERYTHING ELSE IS DRAWN. The head comes pixel by pixel
from the squad portrait — its own colours, nothing quantised, nothing scaled.
Body, pickaxe, rock and bedding are drawn, because the source has no such
poses and a chibi body reads better than a real one at this size.

MINING. He FACES the rock: the head is mirrored so he looks left, and the ore
sits on the left where he is looking. The arm leaves the SHOULDER at torso
height and the haft continues from the hand — an earlier version started the
haft at head height and it read as growing out of his cheek. That needed the
canvas taller than the head, so the swing has somewhere to happen. Sparks land
on the strike frame only; on every frame they read as a glow, not an impact.
The swing RATE follows the burn rate, which is what makes him an instrument
rather than a decoration.

SLEEPING. The head is rotated ninety degrees so he genuinely lies down, crown
to the left, the bed running the way his body does. The shut eye is drawn
AFTER the rotation: rotating a horizontal eye-line gave a vertical black bar
across his cheek that read as damage. The sleeper BREATHES AS ONE — head and
blanket rise and fall together while the mattress and pillow stay put, because
a bed that breathes with its occupant reads as an earthquake.

EVERY CONSECUTIVE FRAME DIFFERS. The visible rate is the rate the POSE
changes, never the rate the timer fires; two breath positions gave a repeated
frame, so the cycle runs over three.
"""

from __future__ import annotations

from claude_swap.tui.sprite import Sprite

#: The head's own colours, plus the drawn bedding, timber, iron and stone.
_PALETTE = {
    "a": "#000000",
    "b": "#464340",
    "c": "#52452a",
    "d": "#34507e",
    "e": "#785430",
    "f": "#705834",
    "g": "#605c58",
    "h": "#736446",
    "i": "#4a6ea8",
    "j": "#967848",
    "k": "#6086c0",
    "l": "#9b875e",
    "m": "#a38554",
    "n": "#aea184",
    "o": "#c5b595",
    "p": "#d6b278",
    "q": "#c6c8ce",
    "r": "#cbcbc7",
    "s": "#d1cdc4",
    "t": "#ffd68c",
    "u": "#f0f0ec",
}

#: Tokens are being spent: facing the rock, swinging.
WORKING = Sprite(palette=_PALETTE, frames=(
    (
        "....................llnnnnnn......",
        "..................lhlllnnnnnn.....",
        "................chhlllllnnnnn.....",
        "...............hhccnlllnnnnnn.....",
        "..............chaannlllnnnnnn.....",
        "..............cccannlllnnnnnl.....",
        "..................oolllnnnnnn.....",
        "..................solllnnnnnn.....",
        ".................ssslllnnnnnn.....",
        "gbggggbggg.......ssslllnnnnnn.....",
        "bggggbgggg......sslllllnnnnnn.....",
        "ggggbggggb......ssnnaaannnnnaa....",
        "ggppggggbgq....sssnaaooannnnao....",
        "ggpggggbpqq....sssnccaaahhhhaa....",
        "gbggggbggq.e...sssnccaalnnnnaa....",
        "bggggbgggg..e..sssnnnllnnnnnn.....",
        "ggggbggggb..e...ooonsllnnnnnn.....",
        "gggbggppbg...m...oooonncchhhc.....",
        "ggbggggbgg....mjjjjjjjjjjj........",
        "gbggggbggg.....mjjjjjjjjjj........",
        "bggggbgggg.....jjjjjjjjjjj........",
        "ggggpggggb.....jjjjjjjjjjj........",
        "gggbggggbg.....fffffffffff........",
        "ggbggggbgg.....fffffffffff........",
    ),
    (
        "....................llnnnnnn......",
        "..................lhlllnnnnnn.....",
        "................chhlllllnnnnn.....",
        "...............hhccnlllnnnnnn.....",
        "..............chaannlllnnnnnn.....",
        "..............cccannlllnnnnnl.....",
        "..................oolllnnnnnn.....",
        "..................solllnnnnnn.....",
        ".................ssslllnnnnnn.....",
        "gbggggbggg.......ssslllnnnnnn.....",
        "bggggbgggg......sslllllnnnnnn.....",
        "ggggbggggb......ssnnaaannnnnaa....",
        "ggppggggbg.....sssnaaooannnnao....",
        "ggpggggbpg.....sssnccaaahhhhaa....",
        "gbggggbggq.....sssnccaalnnnnaa....",
        "bggggbggqq.....sssnnnllnnnnnn.....",
        "ggggbgggqbee....ooonsllnnnnnn.....",
        "gggbggppbg..e....oooonncchhhc.....",
        "ggbggggbgg...mmjjjjjjjjjjj........",
        "gbggggbggg.....mjjjjjjjjjj........",
        "bggggbgggg.....jjjjjjjjjjj........",
        "ggggpggggb.....jjjjjjjjjjj........",
        "gggbggggbg.....fffffffffff........",
        "ggbggggbgg.....fffffffffff........",
    ),
    (
        "....................llnnnnnn......",
        "..................lhlllnnnnnn.....",
        "................chhlllllnnnnn.....",
        "...............hhccnlllnnnnnn.....",
        "..............chaannlllnnnnnn.....",
        "..............cccannlllnnnnnl.....",
        "..................oolllnnnnnn.....",
        "..................solllnnnnnn.....",
        ".................ssslllnnnnnn.....",
        "gbggggbggg.......ssslllnnnnnn.....",
        "bggggbgggg......sslllllnnnnnn.....",
        "ggggbggggb......ssnnaaannnnnaa....",
        "ggppggggbg.....sssnaaooannnnao....",
        "ggpggggbpg.....sssnccaaahhhhaa....",
        "gbggggbggg.....sssnccaalnnnnaa....",
        "bggggbgggg.....sssnnnllnnnnnn.....",
        "ggggbggggb......ooonsllnnnnnn.....",
        "gggbgtppqg.......oooonncchhhc.....",
        "ggbgggtqqeee...jjjjjjjjjjj........",
        "gbggtgbqgg..emmmjjjjjjjjjj........",
        "bggggbtggg.....jjjjjjjjjjj........",
        "ggggptgggb.....jjjjjjjjjjj........",
        "gggbggggbg.....fffffffffff........",
        "ggbggggbgg.....fffffffffff........",
    ),
    (
        "....................llnnnnnn......",
        "..................lhlllnnnnnn.....",
        "................chhlllllnnnnn.....",
        "...............hhccnlllnnnnnn.....",
        "..............chaannlllnnnnnn.....",
        "..............cccannlllnnnnnl.....",
        "..................oolllnnnnnn.....",
        "..................solllnnnnnn.....",
        ".................ssslllnnnnnn.....",
        "gbggggbggg.......ssslllnnnnnn.....",
        "bggggbgggg......sslllllnnnnnn.....",
        "ggggbggggb......ssnnaaannnnnaa....",
        "ggppggggbg.....sssnaaooannnnao....",
        "ggpggggbpg.....sssnccaaahhhhaa....",
        "gbggggbggq.....sssnccaalnnnnaa....",
        "bggggbggqq.....sssnnnllnnnnnn.....",
        "ggggbgggqbee....ooonsllnnnnnn.....",
        "gggbggppbg..e....oooonncchhhc.....",
        "ggbggggbgg...mmjjjjjjjjjjj........",
        "gbggggbggg.....mjjjjjjjjjj........",
        "bggggbgggg.....jjjjjjjjjjj........",
        "ggggpggggb.....jjjjjjjjjjj........",
        "gggbggggbg.....fffffffffff........",
        "ggbggggbgg.....fffffffffff........",
    ),
))

#: Nothing burning: lying down, eyes shut, breathing under the blanket.
SLEEPING = Sprite(palette=_PALETTE, frames=(
    (
        "..................................",
        "..................................",
        "..................................",
        "..................................",
        "..................................",
        ".......cc.........................",
        "......hhc......ssss...............",
        ".uuuuchac....sssssso..............",
        "uuuuuhcaa..ssssssssoo.............",
        "uuuulhcnnossslnnnnnouuuuuuuuuuuuuu",
        "uuuuhlnnnoosslnannnnukkkkkkkkkkkkk",
        "uuulllllllllllnannnsuiiiidiiiiidii",
        "uuulllllllllllnannlluiiidiiiiidiii",
        "uuunllllllllllnannlluiidiiiiidiiii",
        "uuunnlnnnnnnnnnannnnuidiiiiidiiiii",
        "uuunnnnnnnnnnnnnhnnnudiiiiidiiiiid",
        "uuunnnnnnnnnnnnnhnnnuiiiiidiiiiidi",
        "uuunnnnnnnnnnnnnhnnnuiiiidiiiiidii",
        "uuunnnnnnnnnnnnnhnnnuiiidiiiiidiii",
        "uuuunnnnlnnnnnaaaannuiidiiiiidiiii",
        "rrrrrrrrrrrrrraoaarruidiiiiidiiiii",
        "rrrrrrrrrrrrrrrrrrrrudiiiiidiiiiid",
        "rrrrrrrrrrrrrrrrrrrruiiiiidiiiiidi",
        "rrrrrrrrrrrrrrrrrrrruiiiidiiiiidii",
    ),
    (
        "..................................",
        "..................................",
        "..................................",
        "..................................",
        ".......cc.........................",
        "......hhc......ssss...............",
        ".....chac....sssssso..............",
        ".uuuuhcaa..ssssssssoo.............",
        "uuuulhcnnossslnnnnnouuuuuuuuuuuuuu",
        "uuuuhlnnnoosslnannnnukkkkkkkkkkkkk",
        "uuulllllllllllnannnsuiiiiidiiiiidi",
        "uuulllllllllllnannlluiiiidiiiiidii",
        "uuunllllllllllnannlluiiidiiiiidiii",
        "uuunnlnnnnnnnnnannnnuiidiiiiidiiii",
        "uuunnnnnnnnnnnnnhnnnuidiiiiidiiiii",
        "uuunnnnnnnnnnnnnhnnnudiiiiidiiiiid",
        "uuunnnnnnnnnnnnnhnnnuiiiiidiiiiidi",
        "uuunnnnnnnnnnnnnhnnnuiiiidiiiiidii",
        "uuuunnnnlnnnnnaaaannuiiidiiiiidiii",
        "uuuuuuuuuuuuuuaoaauuuiidiiiiidiiii",
        "rrrrrrrrrrrrrrrrrrrruidiiiiidiiiii",
        "rrrrrrrrrrrrrrrrrrrrudiiiiidiiiiid",
        "rrrrrrrrrrrrrrrrrrrruiiiiidiiiiidi",
        "rrrrrrrrrrrrrrrrrrrruiiiidiiiiidii",
    ),
    (
        "..................................",
        "..................................",
        "..................................",
        ".......cc.........................",
        "......hhc......ssss...............",
        ".....chac....sssssso..............",
        ".....hcaa..ssssssssoo.............",
        ".uuulhcnnossslnnnnnouuuuuuuuuuuuuu",
        "uuuuhlnnnoosslnannnnukkkkkkkkkkkkk",
        "uuulllllllllllnannnsudiiiiidiiiiid",
        "uuulllllllllllnannlluiiiiidiiiiidi",
        "uuunllllllllllnannlluiiiidiiiiidii",
        "uuunnlnnnnnnnnnannnnuiiidiiiiidiii",
        "uuunnnnnnnnnnnnnhnnnuiidiiiiidiiii",
        "uuunnnnnnnnnnnnnhnnnuidiiiiidiiiii",
        "uuunnnnnnnnnnnnnhnnnudiiiiidiiiiid",
        "uuunnnnnnnnnnnnnhnnnuiiiiidiiiiidi",
        "uuuunnnnlnnnnnaaaannuiiiidiiiiidii",
        "uuuuuuuuuuuuuuaoaauuuiiidiiiiidiii",
        "uuuuuuuuuuuuuuuuuuuuuiidiiiiidiiii",
        "rrrrrrrrrrrrrrrrrrrruidiiiiidiiiii",
        "rrrrrrrrrrrrrrrrrrrrudiiiiidiiiiid",
        "rrrrrrrrrrrrrrrrrrrruiiiiidiiiiidi",
        "rrrrrrrrrrrrrrrrrrrruiiiidiiiiidii",
    ),
    (
        "..................................",
        "..................................",
        "..................................",
        "..................................",
        ".......cc.........................",
        "......hhc......ssss...............",
        ".....chac....sssssso..............",
        ".uuuuhcaa..ssssssssoo.............",
        "uuuulhcnnossslnnnnnouuuuuuuuuuuuuu",
        "uuuuhlnnnoosslnannnnukkkkkkkkkkkkk",
        "uuulllllllllllnannnsuiiiiidiiiiidi",
        "uuulllllllllllnannlluiiiidiiiiidii",
        "uuunllllllllllnannlluiiidiiiiidiii",
        "uuunnlnnnnnnnnnannnnuiidiiiiidiiii",
        "uuunnnnnnnnnnnnnhnnnuidiiiiidiiiii",
        "uuunnnnnnnnnnnnnhnnnudiiiiidiiiiid",
        "uuunnnnnnnnnnnnnhnnnuiiiiidiiiiidi",
        "uuunnnnnnnnnnnnnhnnnuiiiidiiiiidii",
        "uuuunnnnlnnnnnaaaannuiiidiiiiidiii",
        "uuuuuuuuuuuuuuaoaauuuiidiiiiidiiii",
        "rrrrrrrrrrrrrrrrrrrruidiiiiidiiiii",
        "rrrrrrrrrrrrrrrrrrrrudiiiiidiiiiid",
        "rrrrrrrrrrrrrrrrrrrruiiiiidiiiiidi",
        "rrrrrrrrrrrrrrrrrrrruiiiidiiiiidii",
    ),
))

#: Kept so older callers keep working; the awake pose is the mining one.
AWAKE = WORKING
