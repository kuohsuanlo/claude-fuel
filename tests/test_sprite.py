"""Tests for half-block pixel sprite rendering (tui/sprite.py)."""

from __future__ import annotations

import pytest

from claude_swap.tui.sprite import TRANSPARENT, Sprite, beside, render

PALETTE = {"K": "#101010", "R": "#8a4b2a", "C": "#7fd6e0"}


def _sprite(*frames: tuple[str, ...]) -> Sprite:
    return Sprite(palette=PALETTE, frames=tuple(frames))


class TestGeometry:
    def test_two_pixel_rows_per_text_row(self):
        """The whole point of the half-block trick: a cell carries two pixels,
        so a 4-pixel-tall sprite is 2 rows of text."""
        sprite = _sprite(("KKKK", "KKKK", "RRRR", "RRRR"))
        assert len(render(sprite, 0)) == 2
        assert sprite.height == 4 and sprite.width == 4

    def test_odd_height_is_reported(self):
        assert "odd" in " ".join(_sprite(("KK", "KK", "KK")).validate())

    def test_short_row_is_reported(self):
        problems = _sprite(("KKKK", "KK")).validate()
        assert any("2 wide" in p for p in problems)

    def test_unknown_palette_key_is_reported(self):
        problems = _sprite(("KKZK", "KKKK")).validate()
        assert any("'Z'" in p for p in problems)

    def test_frames_must_agree_on_height(self):
        problems = Sprite(
            palette=PALETTE, frames=(("KK", "KK"), ("KK", "KK", "KK", "KK"))
        ).validate()
        assert any("4 rows" in p for p in problems)

    def test_a_sound_sprite_reports_nothing(self):
        assert _sprite(("KRCK", "KKKK"), ("KKKK", "KRCK")).validate() == []

    def test_empty_sprite_is_reported_not_crashed(self):
        assert Sprite(palette=PALETTE, frames=()).validate() == ["sprite has no frames"]


class TestPixels:
    def test_top_pixel_is_foreground_bottom_is_background(self):
        rendered = render(_sprite(("K", "R")), 0)[0]
        span = rendered.spans[0]
        assert span.style.color.triplet.hex == "#101010"
        assert span.style.bgcolor.triplet.hex == "#8a4b2a"

    def test_transparent_pixels_paint_nothing(self):
        """Not "paint the background colour" — a sprite that fakes
        transparency against an assumed background becomes a dark rectangle
        the moment someone switches to a light theme."""
        rendered = render(_sprite((TRANSPARENT, TRANSPARENT)), 0)[0]
        assert rendered.plain == " "
        assert not rendered.spans

    def test_half_transparent_cells_keep_the_opaque_half(self):
        """The glyph follows the opaque half, and only one colour is ever set
        — see TestNoInheritedColour for why leaving the other side unset is
        not the same as making it transparent."""
        top_only = render(_sprite(("K", TRANSPARENT)), 0)[0]
        assert top_only.plain == "▀"
        assert top_only.spans[0].style.color.triplet.hex == "#101010"
        assert top_only.spans[0].style.bgcolor is None
        bottom_only = render(_sprite((TRANSPARENT, "R")), 0)[0]
        assert bottom_only.plain == "▄"
        assert bottom_only.spans[0].style.color.triplet.hex == "#8a4b2a"
        assert bottom_only.spans[0].style.bgcolor is None

    def test_missing_bottom_row_is_treated_as_transparent(self):
        """Defensive: validate() rejects odd heights, but a caller that
        skipped validation must get a picture, not an IndexError."""
        rendered = render(Sprite(palette=PALETTE, frames=(("K",),)), 0)[0]
        assert rendered.plain == "▀"


class TestFrames:
    def test_frames_cycle_without_bounds_errors(self):
        sprite = _sprite(("K", "K"), ("R", "R"), ("C", "C"))
        first = render(sprite, 0)[0].spans[0].style.color.triplet.hex
        assert render(sprite, 3)[0].spans[0].style.color.triplet.hex == first
        assert render(sprite, 301)[0].spans[0].style.color.triplet.hex is not None

    def test_dim_is_carried_onto_every_pixel(self):
        rendered = render(_sprite(("K", "R")), 0, dim=True)[0]
        assert rendered.spans[0].style.dim is True


class TestBeside:
    def test_rows_are_joined_and_indented(self):
        text = beside(render(_sprite(("KK", "RR", "CC", "KK")), 0), gap=3)
        lines = text.plain.split("\n")
        assert len(lines) == 2
        assert all(line.startswith("   ") for line in lines)


class TestNoInheritedColour:
    """An unset colour is INHERITED, not transparent."""

    def test_bottom_only_cell_uses_the_lower_glyph(self):
        """Drawing ▀ with no foreground for a bottom-only pixel made the upper
        half paint in the terminal's default text colour, and every sprite
        grew a white fringe along its top edges."""
        rendered = render(_sprite((TRANSPARENT, "R")), 0)[0]
        assert rendered.plain == "▄"
        span = rendered.spans[0]
        assert span.style.color.triplet.hex == "#8a4b2a"
        assert span.style.bgcolor is None

    def test_top_only_cell_uses_the_upper_glyph(self):
        rendered = render(_sprite(("K", TRANSPARENT)), 0)[0]
        assert rendered.plain == "▀"
        assert rendered.spans[0].style.color.triplet.hex == "#101010"
        assert rendered.spans[0].style.bgcolor is None

    def test_a_half_transparent_cell_sets_exactly_one_colour(self):
        """Any colour a cell sets that did not come from a pixel is a fringe
        waiting to happen."""
        sprite = _sprite((f"K{TRANSPARENT}R", f"{TRANSPARENT}CK"))
        row = render(sprite, 0)[0]
        for span in row.spans:
            glyph = row.plain[span.start:span.end]
            colours = (span.style.color is not None) + (span.style.bgcolor is not None)
            expected = 2 if glyph == "▀" and span.style.bgcolor is not None else 1
            assert colours == expected, f"{glyph!r} carries {colours} colours"

    def test_missing_bottom_row_never_inherits(self):
        """An odd-height sprite's last row has no partner; it must still be
        drawn with its own colour rather than borrowing the terminal's."""
        rendered = render(Sprite(palette=PALETTE, frames=(("K",),)), 0)[0]
        assert rendered.plain == "▀"
        assert rendered.spans[0].style.color.triplet.hex == "#101010"
        assert rendered.spans[0].style.bgcolor is None
