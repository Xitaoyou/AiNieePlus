from copy import deepcopy

import pytest

from Backend.application.manga_punctuation import TABLE_ID, get_state, update_rules
from Backend.application.pipeline_common import PipelineError
from Backend.domain.config.tables import TableConfigHelper
from Backend.domain.models.manga import Rect, Region
from Shared.manga_lettering import normalize_punctuation, render_text


def test_defaults_fullwidth_stops_and_custom_deletions_are_preserved():
    other = TableConfigHelper.new_table(
        "post_translation", "User", [{"src": "keep", "dst": "user", "regex": False}]
    )
    config = {
        "post_translation_tables": [deepcopy(other)],
        "post_translation_active_table_id": other["id"],
    }
    assert normalize_punctuation("．．．…︙") == "........."
    update_rules(config, True)
    update_rules(config, True)
    assert len(config["post_translation_tables"]) == 2
    assert config["post_translation_tables"][0] == other
    assert config["post_translation_active_table_id"] == other["id"]
    state = get_state(config)
    edited = [r for r in state["rows"] if r["src"] != "．．．"]
    update_rules(config, True, edited, state["rows"])
    update_rules(config, False)
    update_rules(config, True)
    assert not any(r["src"] == "．．．" for r in get_state(config)["rows"])
    assert normalize_punctuation("．．．", get_state(config)["rows"]) == "．．．"
    snapshot = deepcopy(config)
    TableConfigHelper.apply_runtime_snapshot(snapshot)
    assert snapshot["post_translation_switch"]
    assert any(r["src"] == "keep" for r in snapshot["post_translation_data"])
    assert any(r["src"] == "…" for r in snapshot["post_translation_data"])


def test_rule_edit_merges_concurrent_rows_and_rejects_bad_regex():
    config = {}
    update_rules(config, True)
    baseline = get_state(config)["rows"]
    table = next(t for t in config["post_translation_tables"] if t["id"] == TABLE_ID)
    table["rows"].append({"src": "concurrent", "dst": "preserved", "regex": False})
    update_rules(config, True, [], baseline)
    assert get_state(config)["rows"] == [
        {"src": "concurrent", "dst": "preserved", "regex": False}
    ]
    before = deepcopy(config)
    with pytest.raises(PipelineError):
        update_rules(config, True, [{"src": "[", "dst": "x", "regex": True}])
    assert config == before


def test_vertical_ellipsis_and_lines_rotate_only_when_enabled():
    r = Region(layout_rect=Rect(x=0, y=0, width=100, height=200), target="…")
    r.style.vertical = True
    r.style.autofit = False
    r.style.size = 48
    raw, _ = render_text(r.model_dump())
    r.style.auto_punctuation = True
    replaced, _ = render_text(r.model_dump())
    a, b = raw.getbbox(), replaced.getbbox()
    assert a[2] - a[0] > a[3] - a[1]
    assert b[3] - b[1] > b[2] - b[0]
    assert r.target == "…"
    r.target = "—"
    line, _ = render_text(r.model_dump())
    box = line.getbbox()
    assert box[3] - box[1] > box[2] - box[0]


def test_auto_size_grows_even_if_old_preferred_size_is_tiny():
    r = Region(layout_rect=Rect(x=0, y=0, width=180, height=400), target="测试")
    r.style.size = 6
    r.style.autofit = True
    _, info = render_text(r.model_dump())
    assert info["font_size"] > 50


def test_invalid_rule_from_public_editor_does_not_crash_painting():
    rules = [{"src": "[", "dst": "x", "regex": True}]
    assert normalize_punctuation("safe", rules) == "safe"
    r = Region(target="safe")
    r.style.auto_punctuation = True
    _, info = render_text(r.model_dump(), punctuation_rules=rules)
    assert info["invalid_rules"]
