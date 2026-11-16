"""Qt component state tests; no user desktop input or existing projects touched."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from PyQt5.QtWidgets import QApplication
from Frontend.platform.qt_runtime import configure_qt_plugins
from Frontend.pages.media.manga_page import MangaPage
from Backend.domain.models.manga import MangaWorkspace, MangaPage as Page


class Backend:
    def request(self, method, path, **kwargs):
        if path == "/api/config":
            return {"config": {"platforms": {}}}
        if method == "PATCH":
            doc = deepcopy(kwargs["json"]["workspace"])
            doc["revision"] += 1
            return {"workspace": doc}
        return {"workspaces": []}


def test_editor_frames_undo_split_and_revision():
    configure_qt_plugins()
    app = QApplication.instance() or QApplication([])
    editor = MangaPage(backend=Backend())
    from qfluentwidgets import ComboBox, SimpleCardWidget, Theme, setTheme

    assert isinstance(editor.variant, ComboBox)
    assert isinstance(editor.start_page.drop_card, SimpleCardWidget)
    assert editor.workspace_stack.currentWidget() is editor.start_page
    assert editor.variant.currentData() == "source"
    assert editor.variant.itemData(editor.variant.findData("render")) == "render"
    setTheme(Theme.DARK)
    assert editor.canvas.backgroundBrush().color().name() == "#17191c"
    setTheme(Theme.LIGHT)
    assert editor.canvas.backgroundBrush().color().name() == "#eef1f6"
    # Avoid a network-dependent preview; test the editable document state directly.
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(
                name="page.png",
                source="page.source.png",
                sha256="",
                width=640,
                height=800,
            )
        ]
    )
    editor.loaded(doc.model_dump())
    assert editor.workspace_stack.currentWidget() is editor.work_page
    editor.add_region(dict(x=20, y=30, width=160, height=100, rotation=0))
    region = editor.region()
    assert region is not None
    editor.region_value("source", "原文")
    editor.region_value("target", "人工译文")
    assert editor.target.toPlainText() == "人工译文"
    editor.undo.undo()
    assert editor.region()["target"] == "" and editor.target.toPlainText() == ""
    editor.undo.redo()
    assert editor.region()["target"] == "人工译文"
    editor.split_region()
    assert len(editor.page()["regions"]) == 2
    editor.undo.undo()
    assert len(editor.page()["regions"]) == 1
    editor.document["revision"] = 8
    editor.undo.undo()
    assert editor.document["revision"] == 8
    editor.delete_page()
    assert editor.page() is None
    editor.undo.undo()
    assert editor.page() is not None
    editor.save_timer.stop()
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.deleteLater()
    app.processEvents()
