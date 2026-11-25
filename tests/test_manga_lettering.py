"""Export and live preview share pixel-measured text layout."""

import pytest

from Backend.domain.models.manga import MangaPage, Rect, Region
from Backend.infrastructure.manga.render import lettering, resolve_font
from Shared.manga_lettering import render_text


@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("rotation", [0, 35])
def test_autofit_contains_ink_in_narrow_frames(vertical, rotation):
    r = Region(
        layout_rect=Rect(x=0, y=0, width=65, height=290, rotation=rotation),
        target="因为昨天的翻译文字比较多，现在应当完整地显示在这个窄小的气泡里面。" * 2,
    )
    r.style.vertical = vertical
    r.style.size = 48
    r.style.stroke_width = 2
    tile, info = render_text(r.model_dump())
    bbox = tile.getbbox()
    assert bbox and bbox[0] >= 0 and bbox[1] >= 0
    assert bbox[2] <= 65 and bbox[3] <= 290
    assert info["font_size"] < 48 and not info["overflow"]
    large, large_info = render_text(r.model_dump(), 260, 500)
    assert large_info["font_size"] > info["font_size"]


def test_autofit_can_shrink_below_advisory_minimum():
    r = Region(
        layout_rect=Rect(x=0, y=0, width=35, height=80),
        target="窄框中也不应静默裁掉任何译文。" * 4,
    )
    r.style.size = 40
    r.style.min_size = 20
    tile, info = render_text(r.model_dump())
    assert info["below_minimum"] and info["font_size"] < 20
    assert not info["overflow"] and tile.getbbox()


def test_live_tile_matches_export_pixels():
    r = Region(
        layout_rect=Rect(x=12, y=15, width=100, height=170, rotation=15),
        target="完整的中文与ABC译文",
    )
    r.style.vertical = True
    r.style.bold = True
    r.style.stroke_width = 1
    r.style.line_spacing = 0.7
    page = MangaPage(
        name="test", source="test", sha256="", width=140, height=220, regions=[r]
    )
    layer, _ = lettering(page, (140, 220))
    tile, _ = render_text(r.model_dump(), font_path=resolve_font())
    assert layer.crop((12, 15, 112, 185)).tobytes() == tile.tobytes()


def test_wrapped_punctuation_does_not_force_needless_font_shrink():
    r = Region(
        layout_rect=Rect(x=0, y=0, width=180, height=140),
        target="这是漫画编辑器的排版测试。",
    )
    r.style.size = 28
    _, info = render_text(r.model_dump())
    assert info["font_size"] >= 28


def test_kinsoku_keeps_punctuation_off_line_starts():
    from Shared.manga_lettering import apply_kinsoku

    # 竖排：列首的句号要挂回上一列，连续标点整体挪走
    assert apply_kinsoku(["あいう", "。えおか", "きくけ"], True) == [
        "あいう。",
        "えおか",
        "きくけ",
    ]
    assert apply_kinsoku(["あい", "。」か", "きく"], True)[0] == "あい。」"
    # 竖排：正常内容不动
    assert apply_kinsoku(["あい", "えお"], True) == ["あい", "えお"]


def test_kinsoku_keeps_open_brackets_off_line_ends():
    from Shared.manga_lettering import apply_kinsoku

    assert apply_kinsoku(["あい「", "うえ"], False) == ["あい", "「うえ"]
    assert apply_kinsoku(["あい『（", "うえ"], False) == ["あい", "『（うえ"]
    # 行尾是句号不算违规
    assert apply_kinsoku(["あい。", "うえ"], False) == ["あい。", "うえ"]


def test_vertical_column_never_starts_with_punctuation():
    r = Region(
        layout_rect=Rect(x=0, y=0, width=52, height=240),
        target="あいうえお。かきくけこ。さしすせそ",
    )
    r.style.vertical = True
    r.style.autofit = False
    r.style.size = 20
    _, info = render_text(r.model_dump())
    for line in info["lines"]:
        assert not line.startswith(("。", "、", "」", "…")), line


def test_narrow_tall_bubble_stays_readable():
    r = Region(
        layout_rect=Rect(x=0, y=0, width=28, height=135),
        target="钓到了~！",
    )
    r.style.vertical = True
    r.style.autofit = True
    _, info = render_text(r.model_dump())
    assert info["font_size"] >= 16
    assert not info["overflow"]
