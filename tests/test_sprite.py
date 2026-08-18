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
        top_only = render(_sprite(("K", TRANSPARENT)), 0)[0]
        assert top_only.plain == "▀"
        assert top_only.spans[0].style.bgcolor is None
        bottom_only = render(_sprite((TRANSPARENT, "R")), 0)[0]
        assert bottom_only.spans[0].style.color is None
        assert bottom_only.spans[0].style.bgcolor.triplet.hex == "#8a4b2a"

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
