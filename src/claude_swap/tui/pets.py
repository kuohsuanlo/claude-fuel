"""The pet: Beep, mining while tokens burn and asleep when they stop.

WHY A PET. The screen recomputes several times a second and a static figure
looks identical whether it is measuring or wedged. The pet settles that, and
says one thing the numbers cannot: whether a burn reading of zero means idle
or broken.

THE HEAD IS EXTRACTED, EVERYTHING ELSE IS DRAWN. The head comes pixel by pixel
from the squad portrait — its own colours, nothing quantised, nothing scaled.
The body, pickaxe, rock face and bedding are drawn, because the source has no
such poses and a chibi body reads better than a real one at this size.

SLEEPING ROTATES THE HEAD NINETY DEGREES so he genuinely lies down, crown to
the left, the bed running the same way his body does. The shut eye is drawn
AFTER the rotation: rotating a horizontal eye-line produced a vertical black
bar across his cheek that read as damage rather than as sleep.

MINING IS A REAL SWING. The haft travels along a path per frame and the pick
head follows it, with sparks on the strike frame only — sparks on every frame
read as a glow rather than as impact. The swing RATE follows the burn rate:
the harder the machine is working, the faster he digs, which is the one thing
that makes him an instrument rather than a decoration.

EVERY CONSECUTIVE FRAME DIFFERS. The visible rate is the rate the POSE
changes, never the rate the timer fires — an earlier cycle repeated each pose
and a 4.5 fps timer produced 1.2 picture changes a second.
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
    "f": "#605c58",
    "g": "#736446",
    "h": "#4a6ea8",
    "i": "#967848",
    "j": "#6086c0",
    "k": "#9b875e",
    "l": "#aea184",
    "m": "#c5b595",
    "n": "#d6b278",
    "o": "#c4c6cc",
    "p": "#cbcbc7",
    "q": "#d1cdc4",
    "r": "#ffd68c",
    "s": "#f0f0ec",
}

#: Tokens are being spent: chibi body, pickaxe, ore.
WORKING = Sprite(palette=_PALETTE, frames=(
    (
        "..llllllkk........................",
        ".llllllkkkgk......................",
        ".lllllkkkkkggc....................",
        ".llllllkkklccgg...................",
        ".llllllkkkllaagc..................",
        ".klllllkkkllaccc..................",
        ".llllllkkkmm........o...bffffbffff",
        ".llllllkkkmq.......eoo..ffffbffffb",
        ".llllllkkkqqq....ee..o..fffbffffbf",
        ".llllllkkkqqq..ee.......ffbffffbff",
        ".llllllkkkkkqee.........fbnnffbfff",
        "aalllllaaallqq..........bfnffbffff",
        "mallllammaalqqq.........ffffbffffb",
        "aaggggaaacclqqq.........fffbffffbf",
        "aallllkaacclqqq.........ffbffnnbff",
        ".llllllkklllqqq.........fbffffbfff",
        ".llllllkkqlmmm..........bffffbffff",
        ".cgggccllmmmm...........ffffnffffb",
        "....iiiiiiii............fffbffffbf",
        "....iiiiiiii............ffbffffbff",
    ),
    (
        "..llllllkk........................",
        ".llllllkkkgk......................",
        ".lllllkkkkkggc....................",
        ".llllllkkklccgg...................",
        ".llllllkkkllaagc..................",
        ".klllllkkkllaccc..................",
        ".llllllkkkmm............bffffbffff",
        ".llllllkkkmq............ffffbffffb",
        ".llllllkkkqqq...........fffbffffbf",
        ".llllllkkkqqq........o..ffbffffbff",
        ".llllllkkkkkqq......eoo.fbnnffbfff",
        "aalllllaaallqq..eeee..o.bfnffbffff",
        "mallllammaalqqee........ffffbffffb",
        "aaggggaaacclqqq.........fffbffffbf",
        "aallllkaacclqqq.........ffbffnnbff",
        ".llllllkklllqqq.........fbffffbfff",
        ".llllllkkqlmmm..........bffffbffff",
        ".cgggccllmmmm...........ffffnffffb",
        "....iiiiiiii............fffbffffbf",
        "....iiiiiiii............ffbffffbff",
    ),
    (
        "..llllllkk........................",
        ".llllllkkkgk......................",
        ".lllllkkkkkggc....................",
        ".llllllkkklccgg...................",
        ".llllllkkkllaagc..................",
        ".klllllkkkllaccc..................",
        ".llllllkkkmm............bffffbffff",
        ".llllllkkkmq............ffffbffffb",
        ".llllllkkkqqq...........fffbffffbf",
        ".llllllkkkqqq...........ffbffffbff",
        ".llllllkkkkkqq..........fbnnffbfff",
        "aalllllaaallqq..........bfnffbffff",
        "mallllammaalqqq.......o.frffbffffb",
        "aaggggaaacclqqq....eeeoorffbffffbf",
        "aallllkaacclqqqeeee....offrffnnbff",
        ".llllllkklllqqq.........rbffffbfff",
        ".llllllkkqlmmm..........bffffbffff",
        ".cgggccllmmmm...........ffffnffffb",
        "....iiiiiiii............fffbffffbf",
        "....iiiiiiii............ffbffffbff",
    ),
    (
        "..llllllkk........................",
        ".llllllkkkgk......................",
        ".lllllkkkkkggc....................",
        ".llllllkkklccgg...................",
        ".llllllkkkllaagc..................",
        ".klllllkkkllaccc..................",
        ".llllllkkkmm............bffffbffff",
        ".llllllkkkmq............ffffbffffb",
        ".llllllkkkqqq...........fffbffffbf",
        ".llllllkkkqqq........o..ffbffffbff",
        ".llllllkkkkkqq......eoo.fbnnffbfff",
        "aalllllaaallqq..eeee..o.bfnffbffff",
        "mallllammaalqqee........ffffbffffb",
        "aaggggaaacclqqq.........fffbffffbf",
        "aallllkaacclqqq.........ffbffnnbff",
        ".llllllkklllqqq.........fbffffbfff",
        ".llllllkkqlmmm..........bffffbffff",
        ".cgggccllmmmm...........ffffnffffb",
        "....iiiiiiii............fffbffffbf",
        "....iiiiiiii............ffbffffbff",
    ),
))

#: Nothing burning: lying down, eyes shut, under the blanket.
SLEEPING = Sprite(palette=_PALETTE, frames=(
    (
        "..................................",
        ".......cc.........................",
        "......ggc......qqqq...............",
        ".sssscgac....qqqqqqm..............",
        "sssssgcaa..qqqqqqqqmssssssssssssss",
        "sssskgcllmqqqklllllmsjjjjjjjjjjjjj",
        "ssssgklllmmqqklallllshhhdhhhhhdhhh",
        "ssskkkkkkkkkkklalllqshhdhhhhhdhhhh",
        "ssskkkkkkkkkkklallkkshdhhhhhdhhhhh",
        "ssslkkkkkkkkkklallkksdhhhhhdhhhhhd",
        "sssllklllllllllallllshhhhhdhhhhhdh",
        "ssslllllllllllllglllshhhhdhhhhhdhh",
        "ssslllllllllllllglllshhhdhhhhhdhhh",
        "ssslllllllllllllglllshhdhhhhhdhhhh",
        "ssslllllllllllllglllshdhhhhhdhhhhh",
        "ssssllllklllllaaaallsdhhhhhdhhhhhd",
        "ppppppppppppppamaappshhhhhdhhhhhdh",
        "ppppppppppppppppppppshhhhdhhhhhdhh",
        "ppppppppppppppppppppshhhdhhhhhdhhh",
        "ppppppppppppppppppppshhdhhhhhdhhhh",
    ),
    (
        "..................................",
        ".......cc.........................",
        "......ggc......qqqq...............",
        ".sssscgac....qqqqqqm..............",
        "sssssgcaa..qqqqqqqqmm.............",
        "sssskgcllmqqqklllllmssssssssssssss",
        "ssssgklllmmqqklallllsjjjjjjjjjjjjj",
        "ssskkkkkkkkkkklalllqshhdhhhhhdhhhh",
        "ssskkkkkkkkkkklallkkshdhhhhhdhhhhh",
        "ssslkkkkkkkkkklallkksdhhhhhdhhhhhd",
        "sssllklllllllllallllshhhhhdhhhhhdh",
        "ssslllllllllllllglllshhhhdhhhhhdhh",
        "ssslllllllllllllglllshhhdhhhhhdhhh",
        "ssslllllllllllllglllshhdhhhhhdhhhh",
        "ssslllllllllllllglllshdhhhhhdhhhhh",
        "ssssllllklllllaaaallsdhhhhhdhhhhhd",
        "ppppppppppppppamaappshhhhhdhhhhhdh",
        "ppppppppppppppppppppshhhhdhhhhhdhh",
        "ppppppppppppppppppppshhhdhhhhhdhhh",
        "ppppppppppppppppppppshhdhhhhhdhhhh",
    ),
    (
        "..................................",
        "..................................",
        ".......cc.........................",
        ".sssssggc......qqqq...............",
        "ssssscgac....qqqqqqm..............",
        "sssssgcaa..qqqqqqqqmssssssssssssss",
        "sssskgcllmqqqklllllmsjjjjjjjjjjjjj",
        "ssssgklllmmqqklallllshhdhhhhhdhhhh",
        "ssskkkkkkkkkkklalllqshdhhhhhdhhhhh",
        "ssskkkkkkkkkkklallkksdhhhhhdhhhhhd",
        "ssslkkkkkkkkkklallkkshhhhhdhhhhhdh",
        "sssllklllllllllallllshhhhdhhhhhdhh",
        "ssslllllllllllllglllshhhdhhhhhdhhh",
        "ssslllllllllllllglllshhdhhhhhdhhhh",
        "ssslllllllllllllglllshdhhhhhdhhhhh",
        "ssslllllllllllllglllsdhhhhhdhhhhhd",
        "ppppllllklllllaaaallshhhhhdhhhhhdh",
        "ppppppppppppppamaappshhhhdhhhhhdhh",
        "ppppppppppppppppppppshhhdhhhhhdhhh",
        "ppppppppppppppppppppshhdhhhhhdhhhh",
    ),
    (
        "..................................",
        "..................................",
        ".......cc.........................",
        ".sssssggc......qqqq...............",
        "ssssscgac....qqqqqqmssssssssssssss",
        "sssssgcaa..qqqqqqqqmsjjjjjjjjjjjjj",
        "sssskgcllmqqqklllllmshhhdhhhhhdhhh",
        "ssssgklllmmqqklallllshhdhhhhhdhhhh",
        "ssskkkkkkkkkkklalllqshdhhhhhdhhhhh",
        "ssskkkkkkkkkkklallkksdhhhhhdhhhhhd",
        "ssslkkkkkkkkkklallkkshhhhhdhhhhhdh",
        "sssllklllllllllallllshhhhdhhhhhdhh",
        "ssslllllllllllllglllshhhdhhhhhdhhh",
        "ssslllllllllllllglllshhdhhhhhdhhhh",
        "ssslllllllllllllglllshdhhhhhdhhhhh",
        "ssslllllllllllllglllsdhhhhhdhhhhhd",
        "ppppllllklllllaaaallshhhhhdhhhhhdh",
        "ppppppppppppppamaappshhhhdhhhhhdhh",
        "ppppppppppppppppppppshhhdhhhhhdhhh",
        "ppppppppppppppppppppshhdhhhhhdhhhh",
    ),
))

#: Kept so older callers keep working; the awake pose is the mining one.
AWAKE = WORKING
