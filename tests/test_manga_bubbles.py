# -*- coding: utf-8 -*-
"""气泡聚合与重叠判定的单元测试（不依赖 tmp_path，沙箱里也能跑）"""
from Backend.application.manga_service import MangaService


def box(x, y, w, h, score=0.9, cls=1):
    return {"rect": [x, y, w, h], "score": score, "class_id": cls}


def test_vertical_columns_of_one_bubble_are_merged():
    # 一个气泡里的两列竖排文字：横向贴住 + 纵向大半重叠
    merged = MangaService.merge_into_bubbles(
        [box(100, 100, 40, 180, 0.9), box(146, 102, 38, 176, 0.88)]
    )
    assert len(merged) == 1
    assert merged[0]["rect"] == [100, 100, 84, 180]
    assert merged[0]["score"] == 0.9


def test_horizontal_lines_of_one_bubble_are_merged():
    merged = MangaService.merge_into_bubbles(
        [box(100, 100, 180, 40, 0.9), box(102, 144, 176, 38, 0.88)]
    )
    assert len(merged) == 1
    assert merged[0]["rect"] == [100, 100, 180, 82]


def test_distant_bubbles_stay_separate():
    merged = MangaService.merge_into_bubbles(
        [box(100, 100, 60, 60), box(400, 600, 60, 60)]
    )
    assert len(merged) == 2


def test_panels_are_not_merged_into_text():
    merged = MangaService.merge_into_bubbles(
        [box(0, 0, 240, 160, 0.9, cls=0), box(20, 20, 100, 50)]
    )
    assert len(merged) == 1 and merged[0]["class_id"] == 1


def test_single_box_is_returned_asis():
    source = [box(20, 20, 100, 50)]
    assert MangaService.merge_into_bubbles(source) == source
    assert MangaService.merge_into_bubbles([]) == []


def test_boxes_overlap_detects_containment():
    # 小框整块落在气泡里：IoU 很低，但确实是同一个目标
    assert MangaService.boxes_overlap([100, 100, 300, 300], [150, 150, 50, 50])
    # 只有一点点搭边，不算同一个气泡
    assert not MangaService.boxes_overlap([0, 0, 100, 100], [80, 0, 100, 100])
    assert not MangaService.boxes_overlap([0, 0, 50, 50], [200, 200, 50, 50])


def test_fallback_bubbles_do_not_duplicate_existing_boxes():
    # panel 模型自己报了 2 个气泡；行级聚合又给出「重复的大框」和「一个真新框」
    own = [[501, 33, 250, 451], [51, 31, 180, 120]]
    bubbles = own + [[51, 31, 180, 120], [600, 800, 120, 160]]
    extra = [
        b
        for b in bubbles
        if not any(MangaService.boxes_overlap(b, o) for o in own)
    ]
    assert extra == [[600, 800, 120, 160]]


def test_bubble_inside_panel_bbox_counts_as_duplicate():
    # 行级聚合出的大框把 panel 模型的小气泡包住，也算同一目标
    own = [292, 33, 71, 175]
    bubble = [250, 10, 160, 220]
    assert MangaService.boxes_overlap(bubble, own)

