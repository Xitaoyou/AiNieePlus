"""Model catalogue coverage: kinds, API state, download guards and dialog rendering."""

import os
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest


@pytest.fixture(scope="module", autouse=True)
def qt_application():
    from Frontend.platform.qt_runtime import configure_qt_plugins

    configure_qt_plugins()
    app = QApplication.instance() or QApplication([])
    yield app


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
    assert len(detect) >= 12, "检测模型应覆盖 balloon-dataset 全量"
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


def test_api_state_tracks_configuration(registry):
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
    assert model["installed"] and model["api_ready"]
    # 只填网关地址也算可用（兼容网关模式）
    registry.save_api_config("gemini", {"base_url": "https://gw.local/v1"})
    assert api_model("gemini")["api_ready"]
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
        "legacy-balloon-yolov4": "MODEL_IMPORT_REQUIRED",
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
    assert dialog.table.rowCount() == len(CATALOGUE)
    # 分组顺序：第一行是检测模型
    assert dialog.table.item(0, 1).text().startswith("检测")
    # tooltip 必须说明功能与来源
    tooltip = dialog.table.item(0, 0).toolTip()
    assert "来源" in tooltip
    # 至少存在提示配置 API 或缺省状态的行
    states = [dialog.table.item(r, 2).text() for r in range(dialog.table.rowCount())]
    assert any("需要配置" in s or "未启用" in s or "已配置" in s for s in states)
    # 启用一个 API 模型后状态变为需要配置（红字）
    registry.save_api_config("gemini", {"api_key": "g-key"})
    dialog.loaded({"models": registry.catalogue()})
    app.processEvents()
    states = [dialog.table.item(r, 2).text() for r in range(dialog.table.rowCount())]
    assert "已配置" in states
    dialog.deleteLater()
    editor.cleanup()
    editor.pool.waitForDone(3000)
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
