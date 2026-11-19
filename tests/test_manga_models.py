"""Model catalogue coverage: kinds, API state, download guards and dialog rendering."""

import os
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest


_QT_APP = None  # 模块级持有，避免 QApplication 被回收导致 qconfig 失效


@pytest.fixture(scope="module", autouse=True)
def qt_application():
    from Frontend.platform.qt_runtime import configure_qt_plugins

    global _QT_APP
    configure_qt_plugins()
    _QT_APP = QApplication.instance() or QApplication([])
    yield _QT_APP


from pathlib import Path

from PyQt5.QtWidgets import QApplication

from Backend.application.pipeline_common import PipelineError
from Backend.infrastructure.manga.models import CATALOGUE, ModelRegistry


@pytest.fixture()
def registry():
    root = Path("_model_catalogue_test")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    instance = ModelRegistry(root=root / "models", bundled=root / "bundled")
    yield instance
    shutil.rmtree(root, ignore_errors=True)


def test_catalogue_covers_detection_ocr_api_and_system_models():
    ids = [spec["id"] for spec in CATALOGUE]
    assert len(ids) == len(set(ids)), "模型 id 必须唯一"
    kinds = {spec.get("kind", "local") for spec in CATALOGUE}
    assert {"local", "package", "api", "system"} <= kinds
    roles = {spec["role"] for spec in CATALOGUE}
    assert {"detect", "ocr"} <= roles
    detect = [s for s in CATALOGUE if s["role"] == "detect"]
    ocr = [s for s in CATALOGUE if s["role"] == "ocr"]
    # 移除 Legacy YOLOv4 后剩 11 条：面板、行级、四语种气泡、旧版日漫、布局与 RFDETR。
    assert len(detect) >= 11, "检测模型应覆盖 balloon-dataset 的可用部分"
    assert len(ocr) >= 20, "OCR 应覆盖本地 + 云 API + 系统"
    api = [s for s in CATALOGUE if s.get("kind") == "api"]
    assert len(api) >= 15
    assert all(s.get("requires_api") for s in api), "API 模型必须标记 requires_api"
    assert all(s.get("api_provider") for s in api), "API 模型必须有 provider"
    # 大体积模型默认标记高级/可选
    heavy = [
        s for s in CATALOGUE if s.get("size_mb", 0) >= 150 and not s.get("core")
    ]
    assert all(s.get("advanced") for s in heavy), "≥150MB 的非内置模型应标记为高级"
    assert any("japanese-manga" == s["id"] for s in detect)
    assert any(s["id"] == "ppocr-manga-det" for s in detect)
    # Legacy Scaled YOLOv4 检出质量差且占 269 MB，已从目录移除；
    # 阈值统一对齐 ImageTrans 同权重 model.json 的取值。
    assert "legacy-balloon-yolov4" not in {s["id"] for s in detect}
    assert {s["backend"] for s in detect} <= {
        "yolo26",
        "yolo_v8",
        "yolo_generic",
        "rapidocr_det",
        "layout",
        "rfdetr",
    }, "目录里不应再出现 darknet/yolov4 后端"
    for model_id in (
        "japanese-lines",
        "japanese-manga",
        "chinese-webtoon",
        "korean-webtoon",
        "comic-speech-bubble",
        "japanese-manga-old",
    ):
        spec = next(s for s in detect if s["id"] == model_id)
        assert spec["backend"] == "yolo_v8", model_id


def test_api_state_tracks_configuration(registry, monkeypatch):
    def api_model(provider):
        return next(
            m
            for m in registry.catalogue()
            if m.get("kind") == "api" and m.get("api_provider") == provider
        )

    model = api_model("openai")
    assert model["installed"] is False and model["api_ready"] is False
    registry.save_api_config("openai", {"api_key": "sk-test"})
    model = api_model("openai")
    # 填了密钥但尚未验证：可选列表里不应出现
    assert model["installed"] is True and model["api_ready"] is False
    monkeypatch.setattr(
        registry, "_probe_api", lambda backend, config: (True, "")
    )
    ok, message = registry.verify_api("openai")
    assert ok and message == ""
    assert api_model("openai")["api_ready"] is True
    # 网关模式同样需要验证通过
    registry.save_api_config("gemini", {"base_url": "https://gw.local/v1"})
    assert api_model("gemini")["api_ready"] is False
    registry.verify_api("gemini")
    assert api_model("gemini")["api_ready"] is True
    # 验证失败要记录错误且不可选
    monkeypatch.setattr(
        registry, "_probe_api", lambda backend, config: (False, "鉴权失败")
    )
    registry.save_api_config("gemini", {"api_key": "bad"})
    ok, message = registry.verify_api("gemini")
    assert not ok and "鉴权失败" in message
    model = api_model("gemini")
    assert model["api_ready"] is False and "鉴权失败" in model["api_error"]
    # 清除后回到未启用
    registry.save_api_config("openai", {})
    model = api_model("openai")
    assert model["installed"] is False
    assert registry.api_configs().get("openai") is None


def test_download_guards_explain_non_local_kinds(registry):
    cases = {
        "api-openai": "MODEL_API_ONLY",
        "paddleocr": "MODEL_PACKAGE_REQUIRED",
        "winrtocr": "MODEL_SYSTEM_OCR",
        "manga-with-layout": "MODEL_IMPORT_REQUIRED",
    }
    for model_id, code in cases.items():
        with pytest.raises(PipelineError) as error:
            registry.download(model_id)
        assert error.value.code == code, (model_id, error.value.code)


def test_recognize_requires_api_configuration():
    from Backend.infrastructure.manga.engines import recognize

    with pytest.raises(PipelineError) as error:
        recognize(None, Path("."), "api-openai", backend="api_openai", api_config={})
    assert error.value.code == "MODEL_API_NOT_CONFIGURED"
    with pytest.raises(PipelineError) as error:
        recognize(None, Path("."), "api-custom", backend="api_custom", api_config={})
    assert error.value.code == "MODEL_API_NOT_CONFIGURED"


def test_model_dialog_groups_and_marks_missing_api(registry):
    from Frontend.pages.media.manga_page import MangaPage, ModelDialog

    class BackendStub:
        def __init__(self):
            self.models = registry.catalogue()

        def request(self, method, path, **kwargs):
            if path == "/api/v2/manga/capabilities":
                return {"editor_version": 4}
            if path == "/api/config":
                return {"config": {"platforms": {}}}
            if path == "/api/v2/manga/fonts":
                return {"fonts": []}
            if path == "/api/v2/manga/models":
                return {"models": self.models}
            if path == "/api/v2/manga/model-apis":
                return {"configs": registry.api_configs()}
            return {"workspaces": []}

    app = QApplication.instance()
    editor = MangaPage(backend=BackendStub())
    editor.load_image = lambda: None
    dialog = ModelDialog(editor)
    dialog.loaded({"models": registry.catalogue()})
    app.processEvents()
    # 分组标题行 + 每个模型一行
    assert dialog.table.rowCount() >= len(CATALOGUE)
    # 第一行是分组标题，第二行起是检测模型
    assert "检测模型" in dialog.table.item(0, 0).text()
    assert dialog.table.item(1, 1).text().startswith("检测")
    # tooltip 必须说明功能、影响与体积
    tooltip = dialog.table.item(1, 0).toolTip()
    assert "体积" in tooltip or "MB" in tooltip
    assert "影响" in tooltip
    # 至少存在提示配置 API 或缺省状态的行
    states = [
        dialog.table.item(r, 2).text()
        for r in range(dialog.table.rowCount())
        if dialog.table.item(r, 2) is not None
    ]
    assert any(
        "需要配置" in s or "未配置" in s or "待验证" in s or "已就绪" in s
        for s in states
    )
    # 启用一个 API 模型后状态变为需要配置（红字）
    registry.save_api_config("gemini", {"api_key": "g-key"})
    dialog.loaded({"models": registry.catalogue()})
    app.processEvents()
    states = [
        dialog.table.item(r, 2).text()
        for r in range(dialog.table.rowCount())
        if dialog.table.item(r, 2) is not None
    ]
    assert "待验证" in states
    registry._probe_api = lambda backend, config: (True, "")
    registry.verify_api("gemini")
    dialog.loaded({"models": registry.catalogue()})
    app.processEvents()
    states = [
        dialog.table.item(r, 2).text()
        for r in range(dialog.table.rowCount())
        if dialog.table.item(r, 2) is not None
    ]
    assert "已就绪" in states
    dialog.deleteLater()
    editor.cleanup()
    editor.pool.waitForDone(3000)
    editor.deleteLater()
    app.processEvents()


def test_project_settings_only_lists_available_models(registry):
    from Frontend.pages.media.manga_page import MangaPage

    class BackendStub:
        def request(self, method, path, **kwargs):
            if path == "/api/v2/manga/capabilities":
                return {"editor_version": 4}
            if path == "/api/config":
                return {"config": {"platforms": {}}}
            if path == "/api/v2/manga/fonts":
                return {"fonts": []}
            if path == "/api/v2/manga/models":
                return {"models": registry.catalogue()}
            if path == "/api/v2/manga/model-apis":
                return {"configs": registry.api_configs()}
            return {"workspaces": []}

    app = QApplication.instance()
    editor = MangaPage(backend=BackendStub())
    editor.refresh_model_catalogue(registry.catalogue())
    editor.fill_model_choices()
    ids = [editor.detector.itemData(i) for i in range(editor.detector.count())]
    assert ids, "检测模型下拉不应为空"
    assert all(isinstance(item, str) and item for item in ids)
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
