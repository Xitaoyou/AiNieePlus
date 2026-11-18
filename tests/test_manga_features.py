"""Batch replace, style presets, character context and archive export/import."""

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from Backend.application.execution_context import ExecutionContext
from Backend.application.manga_service import MangaService
from Backend.application.manga_store import (
    ARCHIVE_EXTENSIONS,
    EXTENSIONS,
    MangaStore,
)
from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import (
    CharacterProfile,
    MangaSettings,
    Rect,
    Region,
    StylePreset,
)


class StubModels:
    root = ""
    bundled = ""

    def prepare(self, *_args, **_kwargs):
        return None


def make_document(tmp_path, sources=("こんにちは", "お前か")):
    folder = tmp_path / "input"
    folder.mkdir()
    image = Image.new("RGB", (200, 120), "white")
    for index in range(2):
        image.save(folder / f"{index + 1}.png")
    store = MangaStore(tmp_path / "projects")
    document = store.import_images([str(folder)])
    rect = Rect(x=8, y=8, width=80, height=40)
    for page, text in zip(document.pages, sources):
        page.regions = [
            Region(
                ocr_rect=rect,
                erase_rect=rect,
                layout_rect=rect,
                source=text,
                target=f"{text}译",
            )
        ]
    return store, store.save(document, document.revision)


def test_style_presets_and_characters_live_in_settings():
    settings = MangaSettings()
    assert settings.style_presets == [] and settings.characters == []
    assert settings.context_neighbors == 1
    assert settings.characterization_switch is True

    preset = StylePreset(name="对白", style={"size": 30, "vertical": True})
    character = CharacterProfile(
        original_name="お前",
        translated_name="你",
        gender="男",
        speech_style="短句",
    )
    assert len(preset.id) == 32
    assert character.to_row() == {
        "original_name": "お前",
        "translated_name": "你",
        "gender": "男",
        "age": "",
        "personality": "",
        "speech_style": "短句",
        "additional_info": "",
    }
    merged = MangaSettings.model_validate(
        {"style_presets": [preset.model_dump()], "characters": [character.model_dump()]}
    )
    assert merged.style_presets[0].style.size == 30
    assert merged.characters[0].translated_name == "你"


def test_context_neighbors_is_bounded():
    with pytest.raises(ValueError):
        MangaSettings(context_neighbors=6)
    with pytest.raises(ValueError):
        MangaSettings(context_neighbors=-1)


def test_replace_text_preview_then_apply(tmp_path):
    store, document = make_document(tmp_path)
    service = MangaService(store, StubModels())

    preview = service.replace_text(document.id, "こんにちは", "你好", dry_run=True)
    assert preview["changed"] == 1
    assert preview["dry_run"] is True
    assert preview["samples"][0]["after"] == "你好译"
    assert store.load(document.id).pages[0].regions[0].target == "こんにちは译"

    applied = service.replace_text(document.id, "^こんにちは", "你好")
    assert applied["changed"] == 1
    saved = store.load(document.id)
    assert saved.pages[0].regions[0].target == "你好译"
    assert saved.pages[1].regions[0].target == "お前か译"


def test_replace_text_validates_pattern_and_scope(tmp_path):
    store, document = make_document(tmp_path)
    service = MangaService(store, StubModels())
    with pytest.raises(PipelineError) as failure:
        service.replace_text(document.id, "([")
    assert failure.value.code == "INVALID_MANGA_PATTERN"

    scoped = service.replace_text(
        document.id, "译", "・", page_ids=[document.pages[0].id]
    )
    assert scoped["changed"] == 1
    saved = store.load(document.id)
    assert saved.pages[0].regions[0].target.endswith("・")
    assert saved.pages[1].regions[0].target.endswith("译")


def test_replace_source_resets_translation_state(tmp_path):
    store, document = make_document(tmp_path)
    service = MangaService(store, StubModels())
    with store.lock:
        latest = store.load(document.id)
        latest.pages[0].regions[0].translation_state = "succeeded"
        store.save(latest, latest.revision)
    service.replace_text(document.id, "こんにちは", "どうも", field="source")
    saved = store.load(document.id)
    region = saved.pages[0].regions[0]
    assert region.source == "どうも"
    assert region.translation_state == "idle"


def test_cbz_import_uses_natural_order(tmp_path):
    assert ".cbz" in EXTENSIONS and ARCHIVE_EXTENSIONS == {".cbz", ".zip"}
    buffer = BytesIO()
    Image.new("RGB", (40, 60), "red").save(buffer, "PNG")
    payload = buffer.getvalue()
    archive_path = tmp_path / "chapter.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("page10.png", payload)
        archive.writestr("page2.png", payload)
        archive.writestr("page1.png", payload)
        archive.writestr("notes.txt", "skip")
    store = MangaStore(tmp_path / "projects")
    document = store.import_images([archive_path])
    names = [page.name for page in document.pages]
    assert len(document.pages) == 3
    assert all("notes" not in name for name in names)
    assert [name.split("-")[-1] for name in names] == [
        "page1.png",
        "page2.png",
        "page10.png",
    ]
    assert document.pages[0].width == 40 and document.pages[0].height == 60


def test_empty_archive_is_rejected(tmp_path):
    archive_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("readme.txt", "nothing")
    store = MangaStore(tmp_path / "projects")
    with pytest.raises(PipelineError) as failure:
        store.import_images([archive_path])
    assert failure.value.code == "INVALID_MANGA_INPUT"


def test_prune_assets_drops_stale_keys_only(tmp_path):
    store, document = make_document(tmp_path)
    page = document.pages[0]
    directory = store.directory(document.id)
    for key in range(8):
        (directory / f"{page.id}.{key:04d}.render.png").write_bytes(b"x")
    live = directory / f"{page.id}.{store.background_key(page, document.settings)}.clean.png"
    live.write_bytes(b"live")
    removed = store.prune_assets(document.id, keep=2)
    assert removed >= 5
    assert live.is_file()


def export_service(tmp_path):
    store, document = make_document(tmp_path)
    service = MangaService(store, StubModels())

    def fake_render(document_, page, revision, report, context=None):
        report["warnings"].append(
            {"region_id": "r1", "code": "TEXT_OVERFLOW", "message": "字号过小"}
        )
        target = (
            store.directory(document_.id)
            / f"{page.id}.{store.preview_key(page, document_.settings)}.render.png"
        )
        target.write_bytes(store.asset(document_.id, page.id, "source").read_bytes())
        return target

    service.render = fake_render
    return store, document, service


def test_cbz_and_pdf_export(tmp_path):
    store, document, service = export_service(tmp_path)

    cbz = service.export(document.id, str(tmp_path / "cbz"), "cbz")
    assert len(cbz["files"]) == 1
    assert cbz["quality"] and set(cbz["quality"][0]) >= {"page_id", "code", "message"}
    with zipfile.ZipFile(cbz["files"][0]) as archive:
        names = archive.namelist()
        assert names == ["0002-2.png", "0001-1.png"] or sorted(names) == [
            "0001-1.png",
            "0002-2.png",
        ]
        assert {info.compress_type for info in archive.infolist()} == {
            zipfile.ZIP_STORED
        }
    assert not [
        p for p in Path(cbz["files"][0]).parent.iterdir() if p.suffix == ".png"
    ]

    pdf = service.export(document.id, str(tmp_path / "pdf"), "pdf")
    import fitz

    with fitz.open(pdf["files"][0]) as opened:
        assert len(opened) == 2
        assert round(opened[0].rect.width) == 200
    assert not [
        p for p in Path(pdf["files"][0]).parent.iterdir() if p.suffix == ".png"
    ]


def test_export_quality_is_reported_for_images(tmp_path):
    _, document, service = export_service(tmp_path)
    result = service.export(document.id, str(tmp_path / "out"), "png")
    assert len(result["files"]) == 2
    assert len(result["quality"]) == 2
    assert result["quality"][0]["code"] == "TEXT_OVERFLOW"


def test_translate_forwards_character_context(tmp_path, monkeypatch):
    store, document = make_document(tmp_path)
    with store.lock:
        latest = store.load(document.id)
        latest.settings.characterization_switch = True
        latest.settings.context_neighbors = 2
        latest.settings.characters = [
            CharacterProfile(original_name="お前", translated_name="你", gender="男")
        ]
        for page in latest.pages:
            for region in page.regions:
                region.target = ""
        store.save(latest, latest.revision)

    captured = {}

    class Stop(Exception):
        pass

    def fake_translate(model, config=None, context=None, **_kwargs):
        captured.update(config)
        raise Stop()

    import Backend.application.translation_service as translation

    monkeypatch.setattr(translation, "translate_model", fake_translate)
    service = MangaService(store, StubModels())
    document = store.load(document.id)
    page = document.pages[0]
    with pytest.raises(Stop):
        service.translate(document, page, page.regions, ExecutionContext(), {})
    assert captured["characterization_switch"] is True
    assert captured["characterization_data"][0]["translated_name"] == "你"
    assert captured["pre_line_counts"] == 2

    with store.lock:
        latest = store.load(document.id)
        latest.settings.characterization_switch = False
        latest.settings.context_neighbors = 0
        for page in latest.pages:
            for region in page.regions:
                region.target = ""
        store.save(latest, latest.revision)
    captured.clear()
    document = store.load(document.id)
    page = document.pages[0]
    with pytest.raises(Stop):
        service.translate(document, page, page.regions, ExecutionContext(), {})
    assert "characterization_data" not in captured
    assert "pre_line_counts" not in captured
