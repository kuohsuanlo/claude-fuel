"""Pixel-art sprites in a terminal, via the half-block trick.

A terminal cell is roughly twice as tall as it is wide, so drawing one pixel
per cell gives squashed, unreadable art. ``▀`` (upper half block) fixes it: the
glyph paints the top half in the FOREGROUND colour and leaves the bottom half
showing the BACKGROUND colour, so one cell carries two vertically-stacked
pixels and each pixel comes out roughly square. A 16x16 sprite is therefore 16
columns by 8 rows of text, and it looks like pixel art rather than like
characters arranged to suggest a shape.

Transparency is real, not faked with a background-coloured pixel: a
transparent half simply gets no colour set on that side, so whatever the
terminal is already painting shows through. Faking it against a known
background is what makes sprites show up as dark rectangles the moment someone
switches to a light theme.

Sprites are plain data — a palette and rows of single-character keys — so they
can be written, diffed, and tested as text, and a new one needs no code.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

# The glyphs that carry two pixels. "▀" paints its top half in the foreground
# and lets the background show below; "▄" is its mirror. Both are needed: a
# cell with only one opaque half must use the glyph matching that half, or the
# unset side inherits the terminal's foreground and shows as a white fringe.
_UPPER = "▀"
_LOWER = "▄"

# Palette key meaning "paint nothing here".
TRANSPARENT = "."


@dataclass(frozen=True)
class Sprite:
    """One animation: a palette plus a list of equal-sized pixel frames.

    ``frames`` are lists of rows, each row a string of palette keys. Frame
    height must be even — every character row consumes two pixel rows, and an
    odd height would silently drop the last one.
    """

    palette: dict[str, str]
    frames: tuple[tuple[str, ...], ...]

    @property
    def width(self) -> int:
        return len(self.frames[0][0]) if self.frames and self.frames[0] else 0

    @property
    def height(self) -> int:
        return len(self.frames[0]) if self.frames else 0

    def validate(self) -> list[str]:
        """Geometry problems, as human-readable strings. Empty means sound.

        Checked rather than trusted because a sprite is hand-authored data:
        one short row shifts every pixel after it and the result is not a
        subtly wrong picture, it is noise.
        """
        problems: list[str] = []
        if not self.frames:
            return ["sprite has no frames"]
        width, height = self.width, self.height
        if height % 2:
            problems.append(f"height {height} is odd; each text row holds 2 pixels")
        for index, frame in enumerate(self.frames):
            if len(frame) != height:
                problems.append(f"frame {index} has {len(frame)} rows, expected {height}")
            for row_index, row in enumerate(frame):
                if len(row) != width:
                    problems.append(
                        f"frame {index} row {row_index} is {len(row)} wide, "
                        f"expected {width}"
                    )
                for key in row:
                    if key != TRANSPARENT and key not in self.palette:
                        problems.append(
                            f"frame {index} row {row_index} uses '{key}', "
                            "which is not in the palette"
                        )
        return problems


def render(sprite: Sprite, frame: int, *, dim: bool = False) -> list[Text]:
    """One frame as text rows, ready to place in a widget.

    Returns a list because a sprite is inherently multi-line; the caller
    decides how to lay the rows out and what sits beside them.

    ``dim`` renders the same pixels at reduced intensity — used for a pet that
    is idle rather than working, so the two states differ in energy as well as
    in pose.
    """
    if not sprite.frames:
        return []
    rows = sprite.frames[frame % len(sprite.frames)]
    out: list[Text] = []
    for top_index in range(0, len(rows), 2):
        top = rows[top_index]
        bottom = rows[top_index + 1] if top_index + 1 < len(rows) else "." * len(top)
        line = Text()
        for column in range(len(top)):
            upper = sprite.palette.get(top[column]) if top[column] != TRANSPARENT else None
            lower = (
                sprite.palette.get(bottom[column])
                if column < len(bottom) and bottom[column] != TRANSPARENT
                else None
            )
            if upper is None and lower is None:
                # Both halves transparent: emit a space rather than a styled
                # block, so nothing is painted at all and the row behind shows.
                line.append(" ")
            elif upper is not None and lower is not None:
                line.append(
                    _UPPER, style=Style(color=upper, bgcolor=lower, dim=dim or None)
                )
            else:
                # HALF-TRANSPARENT CELLS PICK THE GLYPH THAT MATCHES THE
                # OPAQUE HALF, and never set the other side at all.
                #
                # Drawing "▀" with no foreground for a bottom-only pixel was
                # the "white blocks" bug: an unset colour is not transparent,
                # it is INHERITED, so the upper half painted in the terminal's
                # default text colour and every sprite grew a white fringe
                # along its top edges. Choosing ▄ instead leaves the untouched
                # half genuinely unpainted.
                opaque = upper if upper is not None else lower
                glyph = _UPPER if upper is not None else _LOWER
                line.append(glyph, style=Style(color=opaque, dim=dim or None))
        out.append(line)
    return out


def render_pixels(
    pixels: list[list[str | None]], *, dim: bool = False
) -> list[Text]:
    """Same half-block rendering, for a pixel buffer built at draw time.

    Sprites are fixed artwork; some pictures — a sky whose sun moves with the
    clock — are computed per frame and have no fixed palette to key against.
    Both go through the same cell rule, so a procedural drawing cannot pick up
    the inherited-foreground fringe that fixed sprites are tested against.

    ``pixels`` is rows of hex colours, ``None`` meaning transparent. An odd
    number of rows leaves the final half unpainted rather than borrowing a
    colour.
    """
    out: list[Text] = []
    for index in range(0, len(pixels), 2):
        top = pixels[index]
        bottom = pixels[index + 1] if index + 1 < len(pixels) else [None] * len(top)
        line = Text()
        for column in range(len(top)):
            upper = top[column]
            lower = bottom[column] if column < len(bottom) else None
            if upper is None and lower is None:
                line.append(" ")
            elif upper is not None and lower is not None:
                line.append(
                    _UPPER, style=Style(color=upper, bgcolor=lower, dim=dim or None)
                )
            else:
                opaque = upper if upper is not None else lower
                glyph = _UPPER if upper is not None else _LOWER
                line.append(glyph, style=Style(color=opaque, dim=dim or None))
        out.append(line)
    return out


def beside(rows: list[Text], *, gap: int = 2) -> Text:
    """Join sprite rows into one Text with newlines, indented by ``gap``.

    A convenience for the common case of dropping a sprite into a Static on
    its own; anything wanting text alongside the pixels composes the rows
    itself.
    """
    joined = Text()
    for index, row in enumerate(rows):
        if index:
            joined.append("\n")
        joined.append(" " * gap)
        joined.append(row)
    return joined
