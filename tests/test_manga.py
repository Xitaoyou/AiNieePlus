"""Manga persistence, task boundaries, editing merge and export contracts."""

from copy import deepcopy
import json
from pathlib import Path
import zipfile
import pytest
import numpy as np
from PIL import Image, ImageDraw

from Backend.application.execution_context import ExecutionContext
from Backend.application.manga_store import MangaStore
from Backend.application.manga_service import MangaService
from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import Region, Rect, MangaWorkspace
from Backend.infrastructure.manga.models import ModelRegistry
from Backend.infrastructure.manga.render import clean_page
from Frontend.services.manga_merge import merge_edits


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "input"
    folder.mkdir()
    for name in ["page10.png", "page2.png"]:
        image = Image.new("RGB", (240, 160), "white")
        ImageDraw.Draw(image).text((22, 22), "HELLO WORLD", fill="black")
        image.save(folder / name)
    store = MangaStore(tmp_path / "projects")
    doc = store.import_images([str(folder)], {"auto_recognize": False})
    rect = Rect(x=10, y=10, width=160, height=80)
    for page in doc.pages:
        page.regions = [
            Region(
                ocr_rect=rect,
                erase_rect=rect,
                layout_rect=rect,
                source="こんにちは",
                target="你好",
            )
        ]
    return store, store.save(doc, doc.revision)


def test_natural_sort_and_immutable_original(workspace, tmp_path):
    store, doc = workspace
    assert [p.name for p in doc.pages] == ["page2.png", "page10.png"]
    doc.pages[0].source = "../../outside.png"
    with pytest.raises(PipelineError, match="原图引用"):
        store.save(doc, doc.revision)
    with pytest.raises(PipelineError):
        store.directory("../../test")


def test_revision_conflict_and_page_delete_undo(workspace):
    store, doc = workspace
    original = doc.model_copy(deep=True)
    doc.pages.pop(0)
    saved = store.save(doc, doc.revision)
    with pytest.raises(PipelineError, match="工程已更新"):
        store.save(original, original.revision)
    original.revision = saved.revision
    restored = store.save(original, saved.revision)
    assert len(restored.pages) == 2


def test_rect_validation_and_duplicate_identity(workspace):
    _, doc = workspace
    values = doc.model_dump()
    values["pages"][0]["regions"][0]["ocr_rect"]["width"] = 5000
    with pytest.raises(ValueError):
        MangaWorkspace.model_validate(values)
    values = doc.model_dump()
    values["pages"][1]["id"] = values["pages"][0]["id"]
    with pytest.raises(ValueError):
        MangaWorkspace.model_validate(values)
    with pytest.raises(ValueError):
        Rect(x=float("nan"))


def test_merge_keeps_user_text_and_new_ocr(workspace):
    _, doc = workspace
    base = doc.model_dump()
    local = deepcopy(base)
    remote = deepcopy(base)
    local["pages"][0]["regions"][0]["target"] = "人工校对"
    remote["pages"][0]["regions"][0]["source"] = "再识别"
    remote["revision"] += 3
    merged = merge_edits(base, local, remote)
    assert merged["pages"][0]["regions"][0]["target"] == "人工校对"
    assert merged["pages"][0]["regions"][0]["source"] == "再识别"
    assert merged["revision"] == remote["revision"]


def test_merge_keeps_page_order_during_job(workspace):
    _, doc = workspace
    base = doc.model_dump()
    local = deepcopy(base)
    remote = deepcopy(base)
    local["pages"].reverse()
    remote["pages"][0]["regions"][0]["source"] = "新识别内容"
    result = merge_edits(base, local, remote)
    assert result["pages"][0]["id"] == local["pages"][0]["id"]
    assert result["pages"][1]["regions"][0]["source"] == "新识别内容"


class StubModels:
    def folder(self, model):
        return Path("/unused")


class StubService(MangaService):
    def compute(self, context, **kw):
        if kw["operation"] == "detect":
            return [
                {"class_id": 0, "score": 0.9, "rect": [0, 0, 240, 160]},
                {"class_id": 1, "score": 0.9, "rect": [20, 20, 100, 50]},
            ]
        return "識別結果"


def test_stage_scope_locks_and_cancel(workspace):
    store, doc = workspace
    service = StubService(store, StubModels())
    doc.pages[0].regions[0].source_locked = True
    doc = store.save(doc, doc.revision)
    result = service.run(doc.id, "ocr", options={"overwrite_source": True})
    assert result["workspace"]["pages"][0]["regions"][0]["source"] == "こんにちは"
    assert result["workspace"]["pages"][1]["regions"][0]["source"] == "識別結果"
    result = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    assert result["status"] == "partial"
    context = ExecutionContext()
    context.cancel()
    with pytest.raises(PipelineError, match="取消"):
        service.run(doc.id, "ocr", context=context)


def test_panel_is_not_a_text_region(workspace):
    store, doc = workspace
    service = StubService(store, StubModels())
    result = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    page = result["workspace"]["pages"][0]
    assert len(page["panels"]) == 1 and len(page["regions"]) == 1
    assert page["regions"][0]["panel_id"] == page["panels"][0]["id"]


def test_translation_adapter_validates_real_intermediate_schema(workspace, monkeypatch):
    from Backend.domain.models.intermediate import parse_model
    from Backend.application import translation_service
    from Backend.core.protocol import to_wire

    store, doc = workspace
    calls = []

    def translate(model, config, context):
        project = parse_model(model)
        calls.append(project)
        for file in project.files.values():
            for item in file.items:
                item.translated_text = "译文通过原接口"
        return {"model": to_wire(project), "status": "success"}

    monkeypatch.setattr(translation_service, "translate_model", translate)
    doc.pages[0].regions[0].target = ""
    doc = store.save(doc, doc.revision)
    result = MangaService(store, StubModels()).run(
        doc.id, "translate", page_ids=[doc.pages[0].id]
    )
    assert calls[0].project_type == "I18next"
    assert result["workspace"]["pages"][0]["regions"][0]["target"] == "译文通过原接口"


def test_render_batch_assets_survive_revisions(workspace, tmp_path):
    store, doc = workspace
    service = MangaService(store, StubModels())
    result = service.run(doc.id, "render")
    assert not result["report"]["errors"]
    for page in doc.pages:
        assert store.asset(doc.id, page.id, "render").is_file()
    result = service.export(doc.id, str(tmp_path / "out"), "png")
    assert len(result["files"]) == 2
    for filename in result["files"]:
        with Image.open(filename) as image:
            assert image.size == (240, 160)


def test_precise_never_changes_outside_mask(workspace):
    # Empty precise mask must not require a model or modify the original image.
    _, doc = workspace
    page = doc.pages[0]
    page.regions[0].removal = "precise"
    source = Image.new("RGB", (240, 160), "white")
    clean, mask = clean_page(source, page, StubModels())
    assert np.array_equal(np.asarray(clean), np.asarray(source))
    assert not np.asarray(mask).any()


def test_project_and_i18next_roundtrip(workspace, tmp_path):
    store, doc = workspace
    service = MangaService(store, StubModels())
    exported = service.export(doc.id, str(tmp_path / "project"), "project")
    restored = store.import_project(exported["files"][0])
    assert restored.id != doc.id and restored.pages == doc.pages
    exported = service.export(doc.id, str(tmp_path / "text"), "i18next")
    values = json.loads(Path(exported["files"][0]).read_text(encoding="utf-8"))
    values[doc.pages[0].regions[0].id] = "回填"
    Path(exported["files"][0]).write_text(json.dumps(values), encoding="utf-8")
    result = service.import_translation(doc.id, exported["files"][0])
    assert result["workspace"]["pages"][0]["regions"][0]["target"] == "回填"


def test_malicious_project_cannot_extract_path(workspace, tmp_path):
    store, doc = workspace
    data = doc.model_dump()
    data["pages"][0]["source"] = "../../owned.png"
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("project.json", json.dumps(data))
    with pytest.raises(PipelineError):
        store.import_project(archive)
    assert not (tmp_path / "owned.png").exists()


def test_model_hash_detects_corruption(tmp_path):
    # Legacy import is data only and does not need ONNX installed in this test.
    folder = tmp_path / "legacy"
    folder.mkdir()
    for name in ("model.cfg", "model.weights", "model.json"):
        (folder / name).write_text("fixture")
    registry = ModelRegistry(tmp_path / "models", tmp_path / "empty")
    registry.import_folder("legacy-balloon-yolov4", folder)
    installed = registry.verify("legacy-balloon-yolov4")
    (installed / "model.weights").write_text("corrupt")
    with pytest.raises(PipelineError, match="损坏"):
        registry.verify("legacy-balloon-yolov4")


def test_original_translation_pipeline_with_controlled_llm_reply(
    workspace, monkeypatch
):
    """Keep AiNiee batching/prompts/checks; replace only the outbound model reply."""
    from Backend.domain.config.task_config import TaskConfig
    from Backend.infrastructure.config.defaults import get_defaults
    from Backend.infrastructure.llm.llm_requester import LLMRequester

    snapshot = get_defaults()
    snapshot["platforms"] = {
        "LocalLLM_manga_test": {
            "name": "Local manga test",
            "model": "test",
            "api_url": "http://127.0.0.1:1/v1",
            "api_format": "OpenAI",
            "user_thread_counts": 1,
        }
    }
    snapshot["api_settings"] = {
        "active": "LocalLLM_manga_test",
        "translate": "LocalLLM_manga_test",
    }
    snapshot["language_filter_switch"] = False
    snapshot["code_filter_switch"] = False
    monkeypatch.setattr(
        TaskConfig, "read_snapshot", classmethod(lambda cls: deepcopy(snapshot))
    )
    seen = []

    def reply(self, messages, system_prompt, platform_config):
        seen.append((messages, platform_config["target_platform"]))
        return False, "", "<textarea>1.你好，世界！</textarea>", 12, 6

    monkeypatch.setattr(LLMRequester, "sent_request", reply)
    store, doc = workspace
    doc.pages[0].regions[0].target = ""
    doc = store.save(doc, doc.revision)
    result = MangaService(store, StubModels()).run(
        doc.id, "translate", page_ids=[doc.pages[0].id]
    )
    assert not result["report"]["errors"], json.dumps(
        result["report"], ensure_ascii=True
    )
    assert seen and seen[0][1] == "LocalLLM_manga_test"
    assert result["workspace"]["pages"][0]["regions"][0]["target"] == "你好，世界！"


def test_export_preserves_untranslated_original(workspace, tmp_path):
    store, doc = workspace
    for page in doc.pages:
        page.regions[0].target = ""
    doc = store.save(doc, doc.revision)
    service = MangaService(store, StubModels())
    try:
        result = service.export(doc.id, str(tmp_path / "export"))
        for page, filename in zip(doc.pages, result["files"]):
            with (
                Image.open(store.asset(doc.id, page.id, "source")) as source,
                Image.open(filename) as exported,
            ):
                assert np.array_equal(np.asarray(source), np.asarray(exported))
    finally:
        service.close()


def test_cancelled_export_publishes_nothing(workspace, tmp_path):
    store, doc = workspace
    context = ExecutionContext()
    context.cancel()
    output = tmp_path / "export"
    service = MangaService(store, StubModels())
    with pytest.raises(PipelineError):
        service.export(doc.id, str(output), context=context)
    assert not list(output.iterdir())
