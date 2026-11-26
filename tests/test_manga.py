"""Manga persistence, task boundaries, editing merge and export contracts."""

import json
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import onnx
import pytest
from PIL import Image, ImageDraw

from Backend.application.execution_context import ExecutionContext
from Backend.application.manga_service import MangaService
from Backend.application.manga_store import MangaStore
from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import MangaWorkspace, Rect, Region
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

    def spec(self, model):
        from Backend.infrastructure.manga.models import CATALOGUE

        found = next((s for s in CATALOGUE if s["id"] == model), None)
        return found or {"id": model, "kind": "local", "backend": model}

    def package_available(self, backend):
        return True

    def api_config(self, provider):
        return {}


class StubService(MangaService):
    def compute(self, context, **kw):
        if kw["operation"] == "detect":
            if kw.get("model_id") in self.TEXT_LINE_FALLBACKS:
                # 行级模型：一个气泡被切成两列，另有一个独立气泡
                return [
                    {"class_id": 1, "score": 0.9, "rect": [20, 20, 50, 50]},
                    {"class_id": 1, "score": 0.8, "rect": [72, 22, 48, 48]},
                    {"class_id": 1, "score": 0.7, "rect": [150, 100, 60, 40]},
                ]
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
    # 面板模型贡献 panels + 它自己有把握的 1 个气泡；行级聚合再补 1 个新气泡
    assert len(page["panels"]) == 1
    assert len(page["regions"]) == 2
    assert all(r["panel_id"] == page["panels"][0]["id"] for r in page["regions"])


def test_panel_model_lines_come_from_line_detector(workspace):
    store, doc = workspace
    result = StubService(store, StubModels()).run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    report = result["report"]
    assert report["errors"] == []
    assert len(report["fallback_pages"]) == 1
    assert report["fallback_pages"][0]["model"] == "manga-panel"
    # 行级模型把同一气泡的两列聚成一个框，且不与主模型已报的框重复
    regions = result["workspace"]["pages"][0]["regions"]
    rects = [
        (r["ocr_rect"]["x"], r["ocr_rect"]["y"], r["ocr_rect"]["width"], r["ocr_rect"]["height"])
        for r in regions
    ]
    assert len(rects) == len(set(rects))
    assert not any(
        MangaService.boxes_overlap(a, b) for i, a in enumerate(rects) for b in rects[i + 1 :]
    )


@pytest.mark.parametrize(
    "model_id",
    ["manga-with-layout-768", "japanese-lines", "japanese-manga", "legacy-balloon-yolov4"],
)
def test_detection_keeps_the_whole_dialogue_and_separate_neighbors(workspace, model_id):
    store, doc = workspace
    doc.settings.detector = model_id
    doc.settings.detect_panels = True
    doc = store.save(doc, doc.revision)

    # The wide dialogue box contains two narrow columns, each with a
    # higher score. One column extends slightly beyond the top edge.
    detections = [
        {"class_id": 0, "score": .9, "rect": [0, 0, 240, 160]},
        {"class_id": 1, "score": .88, "rect": [30, 17, 12, 60]},
        {"class_id": 1, "score": .85, "rect": [69, 34, 13, 50]},
        {"class_id": 1, "score": .4, "rect": [28, 20, 55, 68]},
        {"class_id": 1, "score": .8, "rect": [100, 26, 16, 50]},
    ]
    service = StubService(store, StubModels())
    service.compute = lambda *args, **kw: detections
    result = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    page = result["workspace"]["pages"][0]
    assert len(page["panels"]) == 1
    assert {
        tuple(r["ocr_rect"][key] for key in ("x", "y", "width", "height"))
        for r in page["regions"]
    } == {(28, 20, 55, 68), (100, 26, 16, 50)}


def test_translation_adapter_validates_real_intermediate_schema(workspace, monkeypatch):
    from Backend.application import translation_service
    from Backend.core.protocol import to_wire
    from Backend.domain.models.intermediate import parse_model

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
    # 导入时必须校验模型身份：做一个只含 "images" 输入的最小 ONNX，
    # 权重本身不需要能推理，checksums.json 才是这里要验的对象。
    folder = tmp_path / "corrupt"
    folder.mkdir()
    model = onnx.helper.make_model(
        onnx.helper.make_graph(
            [onnx.helper.make_node("Identity", ["images"], ["output0"])],
            "probe",
            [onnx.helper.make_tensor_value_info(
                "images", onnx.TensorProto.FLOAT, [1, 3, 640, 640]
            )],
            [onnx.helper.make_tensor_value_info(
                "output0", onnx.TensorProto.FLOAT, [1, 3, 640, 640]
            )],
        ),
        opset_imports=[onnx.helper.make_opsetid("", 13)],
    )
    (folder / "model.onnx").write_bytes(model.SerializeToString())
    (folder / "model.json").write_text("{}", encoding="utf-8")
    registry = ModelRegistry(tmp_path / "models", tmp_path / "empty")
    registry.import_folder("japanese-manga", folder)
    installed = registry.verify("japanese-manga")
    (installed / "model.onnx").write_text("corrupt")
    with pytest.raises(PipelineError, match="损坏"):
        registry.verify("japanese-manga")


@pytest.mark.parametrize(
    ("reply_text", "expected"),
    [("你好，世界！", "你好，世界！"), ("你好．．．", "你好...")],
)
def test_original_translation_pipeline_with_controlled_llm_reply(
    workspace, monkeypatch, reply_text, expected
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
    from Backend.application.manga_punctuation import update_rules

    update_rules(snapshot, True)
    snapshot["language_filter_switch"] = False
    snapshot["code_filter_switch"] = False
    monkeypatch.setattr(
        TaskConfig, "read_snapshot", classmethod(lambda cls: deepcopy(snapshot))
    )
    seen = []

    def reply(self, messages, system_prompt, platform_config):
        seen.append((messages, platform_config["target_platform"]))
        return False, "", "<textarea>1." + reply_text + "</textarea>", 12, 6

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
    assert result["workspace"]["pages"][0]["regions"][0]["target"] == expected


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


def test_project_translation_combines_pages_and_commits_batches(workspace, monkeypatch):
    from Backend.application import translation_service
    from Backend.core.protocol import to_wire
    from Backend.domain.models.intermediate import parse_model

    store, doc = workspace
    for page in doc.pages:
        page.regions[0].target = ""
    doc = store.save(doc, doc.revision)
    calls = []

    def translate(model, config, context):
        project = parse_model(model)
        items = project.files["dialogue.json"].items
        calls.append((len(items), config))
        assert len(items) == 2  # both pages share the original text batch scheduler
        items[0].translated_text = "first batch"
        context.on_batch_complete([items[0]], "dialogue.json", {})
        live = store.load(doc.id)
        assert live.pages[0].regions[0].target == "first batch"
        # Human target changes made during translation must survive late results.
        live.pages[1].regions[0].target = "human correction"
        store.save(live, live.revision)
        items[1].translated_text = "late machine result"
        return {"model": to_wire(project), "status": "success"}

    monkeypatch.setattr(translation_service, "translate_model", translate)
    result = MangaService(store, StubModels()).run(doc.id, "translate")
    assert len(calls) == 1
    assert not any(
        k in calls[0][1] for k in ("tokens_limit", "lines_limit", "user_thread_counts")
    )
    assert result["workspace"]["pages"][1]["regions"][0]["target"] == "human correction"


def test_recognize_is_page_order_and_exposes_each_ocr(workspace, monkeypatch):
    store, doc = workspace
    for page in doc.pages:
        page.regions[0].source = ""
    doc = store.save(doc, doc.revision)
    service = MangaService(store, StubModels())
    observed = []

    def compute(context, **kw):
        if kw["operation"] == "ocr":
            observed.append(kw["image_path"])
            if len(observed) == 2:
                assert store.load(doc.id).pages[0].regions[0].source == "recognized"
            return "recognized"
        raise AssertionError(kw)

    monkeypatch.setattr(service, "compute", compute)
    progress = []
    result = service.run(
        doc.id, "recognize", context=ExecutionContext(on_progress=progress.append)
    )
    assert not result["report"]["errors"]
    assert [Path(p).name for p in observed] == [p.source for p in doc.pages]
    assert any(p.get("region_id") for p in progress)


def test_preview_key_ignores_other_pages_and_ocr_but_tracks_pixels(workspace):
    store, doc = workspace
    page = doc.pages[0]
    key = store.preview_key(page, doc.settings)
    page.regions[0].source = "new OCR"
    page.stages["ocr"] = {"status": "complete"}
    assert store.preview_key(page, doc.settings) == key
    page.regions[0].target = "new translation"
    assert store.preview_key(page, doc.settings) != key
    page.regions[0].target = "你好"
    doc.settings.removal = "precise"
    assert store.preview_key(page, doc.settings) != key


def test_separate_translation_exports_and_permanent_delete(workspace, tmp_path):
    store, doc = workspace
    doc.pages[0].regions[0].target = ""
    doc = store.save(doc, doc.revision)
    service = MangaService(store, StubModels())
    source = service.export(doc.id, tmp_path / "out", "i18next-source")["files"][0]
    target = service.export(doc.id, tmp_path / "out", "i18next-target")["files"][0]
    key = doc.pages[0].regions[0].id
    assert json.loads(Path(source).read_text(encoding="utf-8"))[key] == "こんにちは"
    assert json.loads(Path(target).read_text(encoding="utf-8"))[key] == ""
    original = tmp_path / "input" / "page2.png"
    store.delete(doc.id)
    assert original.exists() and Path(source).exists()
    with pytest.raises(PipelineError):
        store.load(doc.id)


def test_translation_progress_and_failed_frames_are_persisted(workspace, monkeypatch):
    from Backend.application import translation_service
    from Backend.core.protocol import to_wire
    from Backend.domain.models.intermediate import parse_model

    store, doc = workspace
    for p in doc.pages:
        p.regions[0].target = ""
    doc = store.save(doc, doc.revision)
    progress = []

    def translate(model, config, context):
        project = parse_model(model)
        items = project.files["dialogue.json"].items
        items[0].translated_text = "success"
        items[0].translation_status = 1
        context.on_batch_complete([items[0]], "dialogue.json", {})
        context.on_progress({"line": 1, "total_line": 2, "total_requests": 1})
        return {
            "model": to_wire(project),
            "status": "partial",
            "report": {
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "skipped": 0,
                "failed_items": [
                    {"item_id": items[1].item_id, "message": "provider timeout"}
                ],
            },
        }

    monkeypatch.setattr(translation_service, "translate_model", translate)
    result = MangaService(store, StubModels()).run(
        doc.id, "translate", context=ExecutionContext(on_progress=progress.append)
    )
    assert result["status"] == "partial"
    assert result["report"]["translation"]["failed"] == 1
    assert any(
        p.get("current") == 1 and p["total"] == 2 and p["unit"] == "框"
        for p in progress
    )
    latest = store.load(doc.id)
    assert latest.pages[0].regions[0].translation_state == "succeeded"
    assert latest.pages[1].regions[0].translation_state == "failed"
    assert latest.pages[1].regions[0].translation_error == "provider timeout"


def test_background_cache_survives_typography_changes(workspace):
    store, doc = workspace
    page = doc.pages[0]
    key = store.background_key(page, doc.settings)
    page.regions[0].layout_rect.width = 120
    page.regions[0].style.size = 40
    page.regions[0].target = "new target text"
    assert store.background_key(page, doc.settings) == key
    page.regions[0].erase_rect.x = 15
    assert store.background_key(page, doc.settings) != key


def test_mixed_directory_imports_pdf_pages_without_touching_originals(tmp_path):
    import hashlib

    import fitz

    folder = tmp_path / "mixed"
    folder.mkdir()
    pdf = fitz.open()
    for i in range(2):
        page = pdf.new_page(width=100, height=150)
        page.insert_text((10, 30), f"Page {i + 1}")
    pdf.save(folder / "book.pdf")
    pdf.close()
    Image.new("RGB", (90, 120), "white").save(folder / "cover.png")
    before = hashlib.sha256((folder / "book.pdf").read_bytes()).hexdigest()
    store = MangaStore(tmp_path / "projects")
    doc = store.import_images([str(folder)], {"auto_recognize": False})
    assert len(doc.pages) == 3
    assert doc.pages[0].name == "book-0001.png"
    assert (doc.pages[0].width, doc.pages[0].height) == (200, 300)
    assert doc.pages[1].name == "book-0002.png"
    assert hashlib.sha256((folder / "book.pdf").read_bytes()).hexdigest() == before
    assert all(store.asset(doc.id, p.id, "source").is_file() for p in doc.pages)


def test_detected_frames_inherit_project_defaults(workspace):
    store, doc = workspace
    doc.settings.default_style.size = 42
    doc.settings.default_style.bold = True
    doc.settings.default_style.autofit = False
    for page in doc.pages:
        page.regions = []
    doc = store.save(doc, doc.revision)
    result = StubService(store, StubModels()).run(doc.id, "detect")
    assert not result["report"]["errors"]
    assert all(
        p["regions"][0]["style"]["size"] == 42 and p["regions"][0]["style"]["bold"]
        for p in result["workspace"]["pages"]
    )


def test_background_editing_does_not_bake_translated_layers(workspace, monkeypatch):
    from Backend.infrastructure.manga import render

    store, doc = workspace
    page = doc.pages[0]
    directory = store.directory(doc.id)

    def forbidden(*args, **kwargs):
        raise AssertionError("background editing must not lay out final text")

    monkeypatch.setattr(render, "lettering", forbidden)
    render.materialize(
        page,
        store.asset(doc.id, page.id, "source"),
        directory,
        "final-key",
        StubModels(),
        background_key="bg-key",
        background_only=True,
    )
    assert (directory / f"{page.id}.bg-key.clean.png").exists()
    assert not (directory / f"{page.id}.final-key.render.png").exists()
    assert not (directory / f"{page.id}.final-key.layer.png").exists()


def test_recognize_emits_fine_grained_detect_and_ocr_progress(workspace, monkeypatch):
    store, doc = workspace
    for page in doc.pages:
        page.regions = []
    doc = store.save(doc, doc.revision)
    service = MangaService(store, StubModels())

    def compute(context, **kw):
        if kw["operation"] == "detect":
            return [
                {"rect": [10, 10, 80, 40], "score": 0.9, "class_id": 1},
                {"rect": [10, 80, 80, 40], "score": 0.8, "class_id": 1},
            ]
        if kw["operation"] == "ocr":
            return "" if kw["rect"][1] > 50 else "こんにちは"
        raise AssertionError(kw)

    monkeypatch.setattr(service, "compute", compute)
    events = []
    result = service.run(
        doc.id, "recognize", context=ExecutionContext(on_progress=events.append)
    )
    assert not result["report"]["errors"]
    detect_pre = [
        e for e in events if e.get("stage") == "detect" and e.get("phase") == "running"
    ]
    assert detect_pre and all(e.get("model") == "manga-panel" and e.get("page_index") for e in detect_pre)
    ocr_events = [
        e for e in events if e.get("stage") == "ocr" and e.get("region_id")
    ]
    assert ocr_events
    assert all(
        e.get("model") == "mangaocr"
        and e.get("region_total") == 2
        and e.get("region_index") in {1, 2}
        for e in ocr_events
    )
    assert any(e.get("empty") is True for e in ocr_events)


def test_detect_marks_tall_japanese_bubbles_vertical(workspace, monkeypatch):
    store, doc = workspace
    doc.pages[0].regions = []
    doc.settings.source_language = "ja"
    doc = store.save(doc, doc.revision)
    service = MangaService(store, StubModels())

    def compute(context, **kw):
        if kw["operation"] == "detect":
            return [
                {"rect": [10, 10, 28, 130], "score": 0.9, "class_id": 1},
                {"rect": [60, 10, 140, 40], "score": 0.9, "class_id": 1},
                {"rect": [220, 10, 18, 50], "score": 0.9, "class_id": 1},
            ]
        raise AssertionError(kw)

    monkeypatch.setattr(service, "compute", compute)
    result = service.run(doc.id, "detect", page_ids=[doc.pages[0].id])
    regions = result["workspace"]["pages"][0]["regions"]
    tall = next(r for r in regions if r["ocr_rect"]["width"] == 28)
    wide = next(r for r in regions if r["ocr_rect"]["width"] == 140)
    tiny = next(r for r in regions if r["ocr_rect"]["width"] == 18)
    assert tall["style"]["vertical"] is True
    assert wide["style"]["vertical"] is False
    assert tiny["ocr_rect"]["width"] == 18
    assert tiny["layout_rect"]["width"] >= 32


def test_legacy_detector_merges_overlapping_columns_and_recovers_sparse_pages(
    workspace, monkeypatch
):
    store, doc = workspace
    doc.pages[0].regions = []
    doc.settings.detector = "legacy-balloon-yolov4"
    doc = store.save(doc, doc.revision)
    service = StubService(store, StubModels())

    # 1) 4 overlapping column boxes in two dialogue groups -> merged into 2 bubbles
    def compute_overlapping(context, **kw):
        if kw["model_id"] == "legacy-balloon-yolov4":
            return [
                {"class_id": 1, "score": 0.85, "rect": [170, 20, 30, 55]},
                {"class_id": 1, "score": 0.80, "rect": [150, 24, 28, 50]},
                {"class_id": 1, "score": 0.88, "rect": [60, 88, 30, 60]},
                {"class_id": 1, "score": 0.84, "rect": [40, 84, 28, 64]},
            ]
        return []

    monkeypatch.setattr(service, "compute", compute_overlapping)
    result = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    assert len(result["workspace"]["pages"][0]["regions"]) == 2

    # 2) Sparse legacy detection (1 tiny edge box) -> falls back to recover missed bubble
    def compute_sparse(context, **kw):
        if kw["model_id"] == "legacy-balloon-yolov4":
            return [{"class_id": 1, "score": 0.70, "rect": [10, 110, 16, 20]}]
        return [
            {"class_id": 1, "score": 0.90, "rect": [140, 20, 60, 100]},
        ]

    monkeypatch.setattr(service, "compute", compute_sparse)
    result2 = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    assert len(result2["workspace"]["pages"][0]["regions"]) == 2
    assert len(result2["report"]["fallback_pages"]) == 1


def test_manga_panel_drops_overlapping_boxes_and_upgrades_narrow_partial_box(
    workspace, monkeypatch
):
    store, doc = workspace
    doc.pages[0].regions = []
    doc.settings.detector = "manga-panel"
    doc = store.save(doc, doc.revision)
    service = StubService(store, StubModels())

    # 1) manga-panel itself outputs two overlapping boxes for one bubble -> keep only the larger one
    def compute_self_overlap(context, **kw):
        if kw["model_id"] == "manga-panel":
            return [
                {"class_id": 1, "score": 0.85, "rect": [40, 20, 70, 120]},
                {"class_id": 1, "score": 0.90, "rect": [42, 24, 34, 114]},
            ]
        return []

    monkeypatch.setattr(service, "compute", compute_self_overlap)
    res1 = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    regions1 = res1["workspace"]["pages"][0]["regions"]
    assert len(regions1) == 1
    assert regions1[0]["ocr_rect"]["width"] == 70

    # 2) manga-panel outputs only a narrow 1-column box, fallback detects full enclosing bubble -> upgrade
    def compute_narrow_then_full(context, **kw):
        if kw["model_id"] == "manga-panel":
            return [{"class_id": 1, "score": 0.88, "rect": [42, 24, 32, 114]}]
        return [
            {"class_id": 1, "score": 0.82, "rect": [40, 20, 42, 120]},
            {"class_id": 1, "score": 0.81, "rect": [74, 20, 40, 120]},
        ]

    monkeypatch.setattr(service, "compute", compute_narrow_then_full)
    res2 = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    regions2 = res2["workspace"]["pages"][0]["regions"]
    assert len(regions2) == 1
    assert regions2[0]["ocr_rect"]["width"] >= 70


def test_translate_model_returns_immediately_on_cancel_with_blocked_request(
    monkeypatch, tmp_path
):
    import threading
    import time
    from Backend.application.translation_service import translate_model
    from Backend.application.tasks.translator_task import TranslatorTask
    from Backend.domain.config.task_config import TaskConfig

    started = threading.Event()
    release = threading.Event()

    def blocked_start(self):
        started.set()
        release.wait(timeout=15)
        return {"check_result": False, "row_count": 0, "error": "timeout"}

    monkeypatch.setattr(TranslatorTask, "prepare", lambda self, *a, **kw: None)
    monkeypatch.setattr(TranslatorTask, "start", blocked_start)
    ctx = ExecutionContext()
    model = {
        "schema_version": 2,
        "project_id": "p1",
        "project_type": "I18next",
        "project_name": "test",
        "files": {
            "a.json": {
                "file_id": "f1",
                "media_type": "document",
                "storage_path": "a.json",
                "file_project_type": "I18next",
                "items": [
                    {
                        "item_id": "i1",
                        "text_index": 0,
                        "source_text": "こんにちは",
                        "locator": {"kind": "document", "index": 0},
                    }
                ],
            }
        },
    }
    resolved = TaskConfig()
    resolved.target_platform = "openai"
    resolved.model = "gpt-4o-mini"
    resolved.api_key = "test"
    resolved.api_url = "https://api.mistral.ai/v1"
    resolved.rpm_limit = 60
    resolved.round_limit = 1
    resolved.actual_thread_counts = 1
    resolved._auto_concurrency = None
    resolved.tokens_limit_switch = False
    resolved.lines_limit_switch = True
    resolved.lines_limit = 10
    resolved.tokens_limit = 512
    resolved.pre_line_counts = 0
    resolved.source_language = "日语"
    resolved.target_language = "简中"
    monkeypatch.setattr(
        "Backend.application.translation_service.resolve_translation_config",
        lambda *_: resolved,
    )

    def cancel_soon():
        started.wait(timeout=5)
        ctx.cancel()

    t = threading.Thread(target=cancel_soon, daemon=True)
    t.start()
    t0 = time.monotonic()
    try:
        out = translate_model(model, config=resolved, context=ctx)
    finally:
        release.set()
        t.join(timeout=2)
    elapsed = time.monotonic() - t0
    assert out["status"] == "cancelled"
    assert elapsed < 3.0


def test_merge_into_bubbles_separates_stacked_vertical_and_merges_gapped_columns():
    # 1) Two vertically stacked vertical bubbles (touching vertically) must stay separate
    stacked = [
        {"class_id": 1, "score": 0.95, "rect": [155, 363, 130, 223]},
        {"class_id": 1, "score": 0.94, "rect": [136, 583, 81, 158]},
    ]
    merged_stacked = MangaService.merge_into_bubbles(stacked)
    assert len(merged_stacked) == 2

    # 2) Two adjacent vertical columns in the same bubble with ~11px gap (gap <= 0.60 * min_w) must merge
    columns = [
        {"class_id": 1, "score": 0.90, "rect": [100, 40, 35, 130]},
        {"class_id": 1, "score": 0.85, "rect": [146, 55, 22, 50]},
    ]
    merged_cols = MangaService.merge_into_bubbles(columns)
    assert len(merged_cols) == 1


def test_manga_panel_never_merges_two_separate_bubbles_via_fallback(
    workspace, monkeypatch
):
    store, doc = workspace
    doc.pages[0].regions = []
    doc.settings.detector = "manga-panel"
    doc = store.save(doc, doc.revision)
    service = StubService(store, StubModels())

    def compute_two_separate(context, **kw):
        if kw["model_id"] == "manga-panel":
            return [
                {"class_id": 1, "score": 0.97, "rect": [80, 10, 50, 60]},
                {"class_id": 1, "score": 0.91, "rect": [82, 74, 44, 60]},
            ]
        # Fallback reports one huge box covering both separate bubbles
        return [{"class_id": 1, "score": 0.95, "rect": [75, 8, 60, 130]}]

    monkeypatch.setattr(service, "compute", compute_two_separate)
    res = service.run(
        doc.id, "detect", page_ids=[doc.pages[0].id], options={"replace_regions": True}
    )
    assert len(res["workspace"]["pages"][0]["regions"]) == 2
