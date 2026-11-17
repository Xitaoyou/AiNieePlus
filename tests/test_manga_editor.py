"""Qt component state tests; no user desktop input or existing projects touched."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest


@pytest.fixture(scope="module", autouse=True)
def qt_application():
    configure_qt_plugins()
    app = QApplication.instance() or QApplication([])
    yield app


from copy import deepcopy

from PyQt5.QtWidgets import QApplication

from Backend.domain.models.manga import MangaPage as Page
from Backend.domain.models.manga import MangaWorkspace
from Frontend.pages.media.manga_page import MangaPage
from Frontend.platform.qt_runtime import configure_qt_plugins


class Backend:
    def request(self, method, path, **kwargs):
        if path == "/api/v2/manga/capabilities":
            return {"editor_version": 3}
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
    editor.add_region({"x": 20, "y": 30, "width": 160, "height": 100, "rotation": 0})
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


def test_pointer_create_delete_escape_and_page_switch():
    from PyQt5.QtCore import QBuffer, QIODevice, QPointF, Qt
    from PyQt5.QtGui import QImage
    from PyQt5.QtTest import QTest

    configure_qt_plugins()
    app = QApplication.instance() or QApplication([])
    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(name=f"{i}.png", source=f"{i}.png", sha256="", width=640, height=800)
            for i in range(2)
        ]
    )
    editor.loaded(doc.model_dump())
    image = QImage(640, 800, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    editor.image_data = bytes(buffer.data())
    editor.resize(1600, 1100)
    editor.show()
    app.processEvents()
    editor.draw_canvas()
    editor.canvas.fitInView(editor.canvas.sceneRect(), Qt.KeepAspectRatio)
    editor.tool.setCurrentIndex(1)
    view = editor.canvas.viewport()
    start = editor.canvas.mapFromScene(QPointF(40, 40))
    end = editor.canvas.mapFromScene(QPointF(240, 180))
    QTest.mousePress(view, Qt.LeftButton, pos=start)
    QTest.mouseMove(view, end, 20)
    QTest.mouseRelease(view, Qt.LeftButton, pos=end)
    app.processEvents()
    assert len(editor.page()["regions"]) == 1
    assert editor.canvas.selected_ids() == [editor.selected]
    editor.tool.setCurrentIndex(0)
    QTest.mouseClick(
        view, Qt.LeftButton, pos=editor.canvas.mapFromScene(QPointF(100, 90))
    )
    QTest.keyClick(editor.canvas, Qt.Key_Delete)
    app.processEvents()
    assert not editor.page()["regions"]
    editor.undo.undo()
    assert len(editor.page()["regions"]) == 1
    editor.tool.setCurrentIndex(1)
    QTest.mousePress(view, Qt.LeftButton, pos=start)
    QTest.mouseMove(view, end, 10)
    QTest.keyClick(editor.canvas, Qt.Key_Escape)
    QTest.mouseRelease(view, Qt.LeftButton, pos=end)
    assert len(editor.page()["regions"]) == 1
    editor.pages.setCurrentRow(1)
    assert editor.image_data is None
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.close()
    editor.deleteLater()
    app.processEvents()


def test_large_document_undo_only_records_changed_field():

    from Backend.domain.models.manga import Region
    from Frontend.services.manga_edits import apply, changes

    doc = MangaWorkspace(
        pages=[
            Page(
                name=f"{i}.png",
                source=f"{i}.png",
                sha256="",
                width=640,
                height=800,
                regions=[Region() for _ in range(5 + (i < 66))],
            )
            for i in range(337)
        ]
    ).model_dump()
    assert sum(len(p["regions"]) for p in doc["pages"]) == 1751
    changed = deepcopy(doc)
    changed["pages"][150]["regions"][2]["target"] = "edited"
    edits = changes(doc, changed)
    assert len(edits) == 1
    # A background OCR completion on another page survives undo/redo.
    doc["pages"][0]["regions"][0]["source"] = "new OCR"
    apply(doc, edits)
    assert doc["pages"][150]["regions"][2]["target"] == "edited"
    apply(doc, edits, forward=False)
    assert doc["pages"][150]["regions"][2]["target"] == ""
    assert doc["pages"][0]["regions"][0]["source"] == "new OCR"


def test_missing_preview_falls_back_and_late_page_response_is_ignored():
    import httpx
    from PyQt5.QtCore import QBuffer, QIODevice, Qt
    from PyQt5.QtGui import QImage

    app = QApplication.instance()
    image = QImage(100, 100, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    png = bytes(buffer.data())
    backend = Backend()
    requests = []

    def transport(request):
        requests.append(request.url.path)
        return (
            httpx.Response(200, content=png)
            if request.url.path.endswith("/source")
            else httpx.Response(404)
        )

    backend.http = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(transport)
    )
    editor = MangaPage(backend=backend)
    doc = MangaWorkspace(
        pages=[
            Page(name=f"{i}.png", source=f"{i}.png", sha256="", width=100, height=100)
            for i in range(2)
        ]
    )
    doc.settings.removal = "precise"
    editor.loaded(doc.model_dump())
    editor.variant.setCurrentIndex(editor.variant.findData("render"))
    editor.select_page(1)
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert editor.page_id == doc.pages[1].id
    assert editor.image_data == png and editor.image_is_source
    assert any(p.endswith("/clean") for p in requests)
    assert doc.pages[1].id in editor.image_ticket
    editor.save()
    assert "保存" in editor.status.text()
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.deleteLater()
    app.processEvents()
    backend.http.close()


def test_live_resize_reflows_text_before_mouse_release():
    from PyQt5.QtCore import QBuffer, QEvent, QIODevice, QPointF, Qt
    from PyQt5.QtGui import QImage, QMouseEvent
    from PyQt5.QtTest import QTest

    from Frontend.pages.media.manga_canvas import RegionItem

    app = QApplication.instance()
    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[Page(name="test", source="test", sha256="", width=640, height=800)]
    )
    editor.loaded(doc.model_dump())
    image = QImage(640, 800, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    editor.image_data = bytes(buffer.data())
    editor.resize(1440, 1000)
    editor.show()
    app.processEvents()
    editor.add_region({"x": 40, "y": 40, "width": 240, "height": 300, "rotation": 0})
    editor.region_value("target", "文字应当随框实时排版，而不必切换页面。" * 5)
    editor.style_value("size", 48)
    editor.draw_canvas()
    editor.canvas.fitInView(editor.canvas.sceneRect(), Qt.KeepAspectRatio)
    app.processEvents()
    item = next(i for i in editor.canvas.scene().items() if isinstance(i, RegionItem))
    original_size = item.layout_info["font_size"]
    original_undo_count = editor.undo.count()
    start = editor.canvas.mapFromScene(QPointF(277, 337))
    end = editor.canvas.mapFromScene(QPointF(115, 200))
    QTest.mousePress(editor.canvas.viewport(), Qt.LeftButton, pos=start)
    app.sendEvent(
        editor.canvas.viewport(),
        QMouseEvent(QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton, Qt.NoModifier),
    )
    app.processEvents()
    assert editor.region()["layout_rect"]["width"] == 240  # not committed until release
    assert item.layout_info["font_size"] < original_size
    assert not item.layout_info["overflow"]
    QTest.mouseRelease(editor.canvas.viewport(), Qt.LeftButton, pos=end)
    app.processEvents()
    assert editor.undo.count() == original_undo_count + 1
    assert editor.region()["layout_rect"]["width"] < 100
    editor.undo.undo()
    assert editor.region()["layout_rect"]["width"] == 240
    editor.undo.redo()
    assert editor.region()["layout_rect"]["width"] < 100
    editor.erase_region()
    assert editor.region()["erase_only"]
    editor.undo.undo()
    assert not editor.region()["erase_only"]
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.close()
    editor.deleteLater()
    app.processEvents()


def test_background_result_undo_preserves_human_corrections():
    from Backend.domain.models.manga import Region

    app = QApplication.instance()
    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(
                name="p",
                source="p",
                sha256="",
                width=640,
                height=800,
                regions=[Region(), Region()],
            )
        ]
    ).model_dump()
    editor.loaded(deepcopy(doc))
    remote = deepcopy(doc)
    remote["pages"][0]["regions"][0]["source"] = "machine OCR"
    remote["pages"][0]["regions"][1]["source"] = "second machine OCR"
    editor.document = deepcopy(remote)
    editor.document["pages"][0]["regions"][1]["source"] = "human correction"
    editor.record_external_edit(doc, remote, "OCR")
    editor.undo.undo()
    assert editor.document["pages"][0]["regions"][0]["source"] == ""
    assert editor.document["pages"][0]["regions"][1]["source"] == "human correction"
    editor.undo.redo()
    assert editor.document["pages"][0]["regions"][0]["source"] == "machine OCR"
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.deleteLater()
    app.processEvents()


def test_text_field_undo_shortcut_uses_workspace_history():
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    from Backend.domain.models.manga import Region

    app = QApplication.instance()
    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(
                name="p",
                source="p",
                sha256="",
                width=640,
                height=800,
                regions=[Region()],
            )
        ]
    )
    editor.loaded(doc.model_dump())
    editor.select_region(doc.pages[0].regions[0].id)
    editor.show()
    editor.target.setFocus()
    app.processEvents()
    QTest.keyClicks(editor.target, "abc")
    assert editor.region()["target"] == "abc"
    QTest.keyClick(editor.target, Qt.Key_Z, Qt.ControlModifier)
    app.processEvents()
    assert editor.region()["target"] == "ab"
    QTest.keyClick(editor.target, Qt.Key_Y, Qt.ControlModifier)
    app.processEvents()
    assert editor.region()["target"] == "abc"
    editor.select_region("")
    assert not editor.source.isEnabled() and not editor.target.isEnabled()
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.close()
    editor.deleteLater()
    app.processEvents()


def test_task_undo_does_not_revert_human_changes_already_saved_to_backend():
    from Backend.domain.models.manga import Region

    app = QApplication.instance()
    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(
                name="p",
                source="p",
                sha256="",
                width=640,
                height=800,
                regions=[Region()],
            )
        ]
    ).model_dump()
    editor.loaded(deepcopy(doc))
    editor.select_region(doc["pages"][0]["regions"][0]["id"])
    editor.job_id = "in-flight"
    editor.region_value("target", "human target")
    # The background job reloads the saved project, so its returned document
    # includes human edits as well as the OCR it performed.
    remote = deepcopy(editor.document)
    remote["pages"][0]["regions"][0]["source"] = "new OCR"
    editor.document = deepcopy(remote)
    editor.job_id = None
    editor.record_external_edit(doc, remote, "OCR")
    editor.undo.undo()
    assert editor.region()["target"] == "human target"
    assert editor.region()["source"] == ""
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.deleteLater()
    app.processEvents()


def test_late_background_results_are_disconnected_after_editor_cleanup():
    from threading import Event
    from PyQt5.QtCore import QCoreApplication,QEvent
    from PyQt5 import sip
    app=QApplication.instance()
    release=Event()
    called=[]
    pool=None
    for _ in range(20):
        editor=MangaPage(backend=Backend())
        pool=editor.pool
        editor.background(lambda: (release.wait(2),42)[1], called.append)
        editor.cleanup()
        editor.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        assert sip.isdeleted(editor)
    release.set()
    pool.waitForDone(4000)
    app.processEvents()
    assert called == []
