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
            return {"editor_version": 4}
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
    editor.variant.setCurrentIndex(editor.variant.findData("edit"))
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
    app.processEvents()
    assert editor.size.isEnabled()  # entering a size switches to fixed mode
    assert (
        editor.size.lineEdit().width() >= 24
    )  # Fluent arrow buttons must not hide the numeric value
    editor.style_value("autofit", True)  # back to auto-fit for the resize test
    assert not editor.size.isEnabled()
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

    from PyQt5 import sip
    from PyQt5.QtCore import QCoreApplication, QEvent

    app = QApplication.instance()
    release = Event()
    called = []
    pool = None
    for _ in range(20):
        editor = MangaPage(backend=Backend())
        pool = editor.pool
        editor.background(lambda: (release.wait(2), 42)[1], called.append)
        editor.cleanup()
        editor.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert sip.isdeleted(editor)
    release.set()
    pool.waitForDone(4000)
    app.processEvents()
    assert called == []


def make_editor_for_actions():
    from Backend.domain.models.manga import Region

    editor = MangaPage(backend=Backend())
    editor.load_image = lambda: None
    doc = MangaWorkspace(
        pages=[
            Page(
                name=f"page{i}.png",
                source=f"p{i}",
                sha256="",
                width=640,
                height=800,
                regions=[Region(source="original", target="translated")],
            )
            for i in range(2)
        ]
    )
    editor.loaded(doc.model_dump())
    editor.select_region(doc.pages[0].regions[0].id)
    return editor


def dispose_editor(editor):
    app = QApplication.instance()
    editor.cleanup()
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert editor.last_callback_error is None
    editor.close()
    editor.deleteLater()
    app.processEvents()


def test_all_style_defaults_are_inherited_and_clear_actions_are_undoable(monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    editor = make_editor_for_actions()
    editor.style_value("size", 42)
    editor.style_value("bold", True)
    editor.apply_style(True)
    editor.pages.setCurrentRow(1)
    editor.add_region({"x": 220, "y": 20, "width": 100, "height": 120, "rotation": 0})
    assert editor.region()["style"]["size"] == 42
    assert editor.region()["style"]["bold"]
    assert not editor.region()["style"]["autofit"]
    monkeypatch.setattr(editor, "ask", lambda *a, **k: QMessageBox.Yes)
    editor.clear_content("target", "整部漫画")
    assert all(not r["target"] for p in editor.document["pages"] for r in p["regions"])
    editor.undo.undo()
    assert editor.document["pages"][0]["regions"][0]["target"] == "translated"
    editor.clear_content("source", "当前页")
    assert all(not r["source"] for r in editor.page()["regions"])
    editor.undo.undo()
    assert editor.page()["regions"][0]["source"] == "original"
    count = len(editor.page()["regions"])
    editor.clear_content("regions", "当前页")
    assert not editor.page()["regions"]
    editor.undo.undo()
    assert len(editor.page()["regions"]) == count
    editor.clear_selection()
    assert not editor.selected
    dispose_editor(editor)


@pytest.mark.parametrize("kind", ["target", "source", "regions"])
@pytest.mark.parametrize("scope", ["选中的框", "当前页", "整部漫画"])
def test_clear_menu_accepts_real_dialogs_and_respects_scope(monkeypatch, kind, scope):
    from PyQt5.QtCore import QTimer

    from Backend.domain.models.manga import Region
    from Frontend.pages.media.manga_page import ConfirmBox, InputBox

    def click_on_exec(dialog_type, select_scope=False):
        original_exec = dialog_type.exec

        def execute(box):
            if select_scope:
                box.field.setCurrentIndex(box.field.findText(scope))
            QTimer.singleShot(0, box.yesButton.click)
            return original_exec(box)

        monkeypatch.setattr(dialog_type, "exec", execute)

    click_on_exec(InputBox, select_scope=True)
    click_on_exec(ConfirmBox)
    editor = make_editor_for_actions()
    editor.page()["regions"].append(
        Region(source="another", target="other", order=1).model_dump()
    )
    before = deepcopy(editor.document["pages"])
    selected = editor.selected
    editor.clear_content(kind)

    for index, (original, page) in enumerate(zip(before, editor.document["pages"])):
        affected = {
            r["id"]
            for r in original["regions"]
            if scope == "整部漫画"
            or (index == 0 and (scope == "当前页" or r["id"] == selected))
        }
        expected = deepcopy(original["regions"])
        if kind == "regions":
            expected = [r for r in expected if r["id"] not in affected]
        else:
            for r in expected:
                if r["id"] in affected:
                    r[kind] = ""
        actual = page["regions"]
        assert [
            (r["id"], r["source"], r["target"]) for r in actual
        ] == [(r["id"], r["source"], r["target"]) for r in expected]
    assert "已处理" in editor.status.text()
    editor.undo.undo()
    assert editor.document["pages"] == before
    dispose_editor(editor)


def test_one_click_prompts_and_manual_switch_follow_banner(monkeypatch):
    from PyQt5.QtWidgets import QMessageBox

    editor = make_editor_for_actions()
    calls = []
    editor.run = lambda stage, **kw: calls.append((stage, kw))
    editor.document["pages"][0]["regions"] = []
    monkeypatch.setattr(editor, "ask", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(editor, "ask_yes_no", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(editor, "ask_patch", lambda *a, **k: QMessageBox.Yes)
    undetected, pending = editor.pending_counts(1)
    assert undetected == 1 and pending == 0
    editor.one_click_translation()
    assert calls[-1][0] == "recognize" and callable(calls[-1][1]["after"])
    calls[-1][1]["after"]()
    assert calls[-1][0] == "translate"
    editor.ask_ocr(0)
    assert calls[-1][0] == "recognize"
    # ask_detection now uses the task confirm dialog (once-per-task replace)
    seen = []
    monkeypatch.setattr(
        editor,
        "confirm_task",
        lambda title, message, checkbox=None: seen.append(message) or (QMessageBox.No, True),
    )
    editor.ask_detection(0)
    assert calls[-1][0] == "detect" and calls[-1][1]["replace"] is True
    assert seen, "confirmation should explain what is incomplete"
    editor.job_id = "running"
    editor.current_job_stage = "recognize"
    editor.follow_progress = True
    editor.processing_page_id = editor.document["pages"][0]["id"]
    editor.pages.setCurrentRow(1)
    assert not editor.follow_progress and not editor.follow_banner.isHidden()
    editor.resume_follow()
    assert editor.follow_progress and editor.follow_banner.isHidden()
    assert editor.page_id == editor.processing_page_id
    editor.job_id = None
    dispose_editor(editor)


def test_menus_reading_panel_compact_pages_and_zoom():
    from qfluentwidgets import DropDownPushButton

    app = QApplication.instance()
    editor = make_editor_for_actions()
    menus = {
        button.text(): button.task_menu
        for button in editor.findChildren(DropDownPushButton)
        if hasattr(button, "task_menu")
    }
    assert [a.text() for a in menus["识别"]._actions] == [
        "检测当前页文字框",
        "读取当前页原文（OCR）",
    ]
    assert {"翻译当前页"}.issubset({a.text() for a in menus["翻译"]._actions})
    assert {"导入图片 / PDF…", "导入文件夹…", "导出成品…"}.issubset(
        {a.text() for a in menus["文件"]._actions}
    )
    assert all("选中的框" not in a.text() for a in menus["翻译"]._actions)
    assert {"术语表（公共）", "禁翻表（公共）", "文本替换（公共）"}.issubset(
        {a.text() for a in menus["翻译"]._actions}
    )
    assert not any(
        "导出译文" in a.text() or "JSON" in a.text()
        for a in menus["文件"]._actions
    ), "JSON 互通应在「更多」菜单"
    assert any(
        "JSON" in a.text() for a in menus["更多"]._actions
    ), "更多菜单应有 JSON 互通入口"
    assert editor.reading_panel.isHidden() and editor.progress_panel.isHidden()
    editor.reading_toggle.click()
    assert not editor.reading_panel.isHidden()
    editor.reading_toggle.click()
    assert editor.reading_panel.isHidden()
    editor.resize(1280, 900)
    editor.show()
    app.processEvents()
    editor.page_panel.setFixedWidth(64)
    editor.update_page_labels()
    assert editor.pages.item(0).text() == "1" and editor.page_bar_widget.isHidden()
    assert editor.pages.item(0).toolTip().startswith("page0.png")
    editor.page_panel.setFixedWidth(240)
    editor.update_page_labels()
    assert "1 · page0" in editor.pages.item(0).text()
    assert "(" not in editor.pages.item(0).text(), "宽栏也不再用括号挤框数"
    editor.canvas.setSceneRect(0, 0, 640, 800)
    editor.fit_canvas()
    editor.zoom_slider.setValue(100)
    assert abs(editor.canvas.transform().m11() - 1) < 0.001
    assert (
        editor.zoom_overlay.x() + editor.zoom_overlay.width()
        <= editor.canvas.viewport().width()
    )
    assert (
        editor.zoom_overlay.y() + editor.zoom_overlay.height()
        <= editor.canvas.viewport().height()
    )
    dispose_editor(editor)


def test_right_click_ocr_and_delete_apply_only_to_clicked_frame(monkeypatch):
    from PyQt5.QtCore import QPoint
    from qfluentwidgets import RoundMenu

    editor = make_editor_for_actions()
    region_id = editor.page()["regions"][0]["id"]
    editor.add_region({"x": 220, "y": 20, "width": 100, "height": 120, "rotation": 0})
    actions = []
    monkeypatch.setattr(
        RoundMenu, "exec", lambda menu, *a, **k: actions.extend(menu._actions)
    )
    editor.region_context_menu(region_id, QPoint(0, 0))
    calls = []
    editor.run = lambda stage, **kw: calls.append((stage, kw))
    # 已有原文的框会先弹「重试并换引擎」确认框，测试里直接接受
    from PyQt5.QtWidgets import QDialog

    from Frontend.pages.media import manga_page as module

    monkeypatch.setattr(module.RegionRetryDialog, "exec", lambda self: QDialog.Accepted)
    next(a for a in actions if a.text().startswith("OCR 此框")).trigger()
    assert calls[-1][1]["region_ids"] == [region_id] and calls[-1][1]["scope"] == 0
    next(a for a in actions if a.text().startswith("删除此框")).trigger()
    assert (
        len(editor.page()["regions"]) == 1
        and editor.page()["regions"][0]["id"] != region_id
    )
    editor.undo.undo()
    assert len(editor.page()["regions"]) == 2
    dispose_editor(editor)


def test_live_style_and_undo_reuse_backdrop_without_baking():
    from PyQt5.QtCore import QBuffer, QIODevice, Qt
    from PyQt5.QtGui import QImage

    from Frontend.pages.media.manga_canvas import RegionItem

    app = QApplication.instance()
    editor = make_editor_for_actions()
    image = QImage(640, 800, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    editor.image_data = bytes(buffer.data())
    editor.source_cache[editor.page_id] = editor.image_data
    editor.resize(1400, 1000)
    editor.show()
    editor.draw_canvas()
    app.processEvents()
    background_key = editor.canvas.pixmap_item.pixmap().cacheKey()
    item = next(i for i in editor.canvas.scene().items() if isinstance(i, RegionItem))
    before = item.text_key
    editor.style_value("color", "#ff0000")
    app.processEvents()
    assert item.text_key != before
    assert editor.canvas.pixmap_item.pixmap().cacheKey() == background_key
    editor.undo.undo()
    app.processEvents()
    assert item.text_key == before
    assert editor.canvas.pixmap_item.pixmap().cacheKey() == background_key
    assert editor.variant.currentData() == "edit"
    dispose_editor(editor)


def test_updater_uses_our_prereleases_and_rejects_other_repository_assets(monkeypatch):
    import Frontend.app.updates as updates
    from Frontend.app.updates import VersionManager
    from Frontend.services.fork_updates import RELEASES_API, WINDOWS_ASSET

    calls = []
    releases = [
        {
            "tag_name": "manga-18-abcdef0",
            "draft": False,
            "prerelease": True,
            "html_url": "https://github.com/Xitaoyou/AiNieePlus/releases/tag/manga-18-abcdef0",
            "assets": [
                {
                    "name": WINDOWS_ASSET,
                    "browser_download_url": "https://github.com/Xitaoyou/AiNieePlus/releases/download/manga-18-abcdef0/"
                    + WINDOWS_ASSET,
                }
            ],
        }
    ]

    class Response:
        status_code = 200

        def json(self):
            return releases

    monkeypatch.setattr(
        updates.requests, "get", lambda url, **kw: calls.append(url) or Response()
    )
    manager = VersionManager(version="AiNieePlus 17")
    assert manager.check_for_updates() == (True, "18")
    assert all(url == RELEASES_API for url in calls)
    manager.current_version = "18"
    assert manager.check_for_updates() == (False, "18")
    manager.current_version = "17"
    releases[0]["assets"][0]["browser_download_url"] = (
        "https://github.com/NEKOparapa/AiNiee/releases/download/new/other.zip"
    )
    assert manager.check_for_updates() == (False, "17")


def test_windows_installer_handoff_verifies_package_and_waits_for_desktop(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from hashlib import sha256
    from types import SimpleNamespace
    import subprocess

    from Frontend.app import updates
    from Frontend.platform import backend_process
    from Frontend.services.fork_updates import WINDOWS_INSTALLER, WINDOWS_ASSET

    monkeypatch.setattr(updates, "is_windows", lambda: True)
    monkeypatch.setattr(updates, "is_macos", lambda: False)
    monkeypatch.setattr(updates, "executable_root", lambda: tmp_path)
    monkeypatch.setattr(updates, "downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(backend_process, "_child_library_path", nullcontext)
    monkeypatch.setattr(updates, "frontend_resource_path", lambda *args: tmp_path / "updater.exe")
    for name in ("AiNiee.exe", "unins000.exe", "updater.exe"):
        (tmp_path / name).touch()
    events = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: events.append(args))
    manager = updates.VersionManager(version="AiNieePlus 17")
    monkeypatch.setattr(manager, "config_backend", lambda: SimpleNamespace(
        url="http://127.0.0.1:33888", shutdown=lambda: events.append("shutdown")
    ))
    monkeypatch.setattr(manager, "_exit_for_update", lambda: events.append("exit"))
    prefix = "https://github.com/Xitaoyou/AiNieePlus/releases/download/manga-18-abcdef0/"
    assets = [
        {"name": WINDOWS_ASSET, "browser_download_url": prefix + WINDOWS_ASSET},
        {"name": WINDOWS_INSTALLER, "browser_download_url": prefix + WINDOWS_INSTALLER,
         "digest": "sha256:" + sha256(b"original").hexdigest()},
    ]
    url = manager._find_download_url(assets)
    assert url == prefix + WINDOWS_INSTALLER
    manager._remember_update_release({"assets": assets}, "18", url)
    path, _, _ = manager._download_paths()
    assert path.suffix == ".exe"
    path.write_bytes(b"tampered")
    manager._run_updater(path)
    assert events == []
    path.write_bytes(b"original")
    manager._download_completed(str(path))
    assert events[0] == "shutdown" and events[-1] == "exit"
    assert "--installer" in events[1]
    assert f"--wait-pid={os.getpid()}" in events[1]
    (tmp_path / "portable.txt").touch()
    assert manager._find_download_url(assets) == prefix + WINDOWS_ASSET
    assert manager._download_paths()[0].suffix == ".zip"


def test_task_progress_is_hidden_without_job_and_after_completion():
    editor = make_editor_for_actions()
    app = QApplication.instance()
    backend = editor.client.backend
    old_request = backend.request
    received = []
    models = [{"id": "legacy-balloon-yolov4", "installed": True}]

    def request(method, path, **kwargs):
        if path == "/api/jobs/demo":
            return {
                "job": {
                    "state": "succeeded",
                    "progress": {"current": 1, "total": 1},
                    "result": {"models": models},
                }
            }
        return old_request(method, path, **kwargs)

    backend.request = request
    assert editor.progress_panel.isHidden()
    editor.start_request(lambda: {"job": {"id": "demo"}}, received.append)
    assert not editor.progress_panel.isHidden()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.poll_job()
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert editor.progress_panel.isHidden() and editor.job_id is None
    assert received == [{"models": models}]
    dispose_editor(editor)


def test_punctuation_dialog_edits_same_saved_rules_and_toggle_does_not_restore_deleted_rules():
    from Frontend.pages.media.manga_page import PunctuationRulesDialog
    from Shared.manga_lettering import DEFAULT_PUNCTUATION_RULES, normalize_punctuation

    app = QApplication.instance()
    editor = make_editor_for_actions()
    state = {"enabled": False, "rows": deepcopy(DEFAULT_PUNCTUATION_RULES)}
    old_request = editor.client.backend.request

    def request(method, path, **kw):
        if path == "/api/v2/manga/punctuation":
            if method == "POST":
                state["enabled"] = kw["json"]["enabled"]
                if "rows" in kw["json"]:
                    state["rows"] = deepcopy(kw["json"]["rows"])
            return deepcopy(state)
        return old_request(method, path, **kw)

    editor.client.backend.request = request
    dialog = PunctuationRulesDialog(editor)
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert any(
        dialog.table.item(i, 0).text() == "．．．"
        for i in range(dialog.table.rowCount())
    )
    dialog.enabled.setChecked(True)
    dialog.save_rules()
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert state["enabled"] and editor.region()["style"]["auto_punctuation"]
    assert (
        normalize_punctuation(
            "．．．", editor.document["settings"]["punctuation_rules"]
        )
        == "..."
    )
    row = next(
        i
        for i in range(dialog.table.rowCount())
        if dialog.table.item(i, 0).text() == "．．．"
    )
    dialog.table.selectRow(row)
    dialog.remove_rows()
    dialog.save_rules()
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.set_auto_punctuation(False)
    editor.pool.waitForDone(3000)
    app.processEvents()
    editor.set_auto_punctuation(True)
    editor.pool.waitForDone(3000)
    app.processEvents()
    assert not any(r["src"] == "．．．" for r in state["rows"])
    assert editor.document["settings"]["punctuation_rules"] == state["rows"]
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
    dispose_editor(editor)


def test_public_table_links_use_existing_main_window_pages_and_native_import_picker(
    monkeypatch, tmp_path
):
    from PyQt5.QtWidgets import QFileDialog, QWidget

    from Frontend.pages.media import manga_page as module

    app = QApplication.instance()

    class Host(QWidget):
        def switchTo(self, page):
            self.switched_to = page

    host = Host()
    host.prompt_dictionary_page = QWidget(host)
    host.exclusion_list_page = QWidget(host)
    host.text_replace_b_page = QWidget(host)
    editor = MangaPage(parent=host, backend=Backend())
    editor.save = lambda callback=None: callback() if callable(callback) else None
    for name in (
        "prompt_dictionary_page",
        "exclusion_list_page",
        "text_replace_b_page",
    ):
        editor.open_public_table(name)
        assert host.switched_to is getattr(host, name)
    # 图片 / PDF 走系统原生多选对话框（不再用自绘 QFileDialog）
    file = tmp_path / "page.pdf"
    file.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *a, **k: calls.append((a, k)) or ([str(file)], "图片与 PDF"),
    )
    picked = module.pick_image_paths(editor)
    assert picked == [str(file.resolve())]
    assert calls, "native getOpenFileNames must be used"
    assert ".pdf" in editor.start_page.drop_card.file_extensions
    dispose_editor(editor)
    host.deleteLater()
    app.processEvents()


def test_import_folder_uses_native_directory_picker(monkeypatch):
    from PyQt5.QtWidgets import QFileDialog

    app = QApplication.instance()
    folder = "C:/fixture-folder"
    editor = make_editor_for_actions()
    seen = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: folder
    )
    editor.import_paths = lambda paths: seen.extend(paths)
    editor.import_images(folder=True)
    app.processEvents()
    assert seen == [folder]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    editor.import_images(folder=True)
    assert seen == [folder], "取消目录选择不应触发导入"
    dispose_editor(editor)


def test_import_metadata_and_append_preserve_selected_languages(monkeypatch, tmp_path):
    from Frontend.pages.media import manga_page as module

    editor = make_editor_for_actions()
    editor.prepare_to_leave = lambda: True
    payloads = []
    editor.client.request = lambda *args, **kwargs: payloads.append(kwargs["json"])
    editor.start_request = lambda action, callback: action()

    def accept(dialog):
        dialog.name_edit.setText("新漫画")
        dialog.source_language.setCurrentIndex(dialog.source_language.findData("ko"))
        dialog.target_language.setCurrentIndex(dialog.target_language.findData("en"))
        dialog.append.setChecked(True)
        return module.QDialog.Accepted

    monkeypatch.setattr(module.ImportDialog, "exec", accept)
    editor.import_paths([str(tmp_path)])
    assert payloads[0]["title"] == "新漫画"
    assert payloads[0]["workspace_id"] == editor.document["id"]
    assert payloads[0]["settings"]["source_language"] == "ko"
    assert payloads[0]["settings"]["target_language"] == "en"
    dispose_editor(editor)


def test_font_controls_and_progress_overlay_remain_accessible():
    editor = make_editor_for_actions()
    app = QApplication.instance()
    editor.resize(1440, 1000)
    editor.show()
    app.processEvents()
    assert editor.autofit.isVisible()
    editor.size.setValue(42)
    assert not editor.region()["style"]["autofit"]
    editor.autofit.setChecked(True)
    assert editor.region()["style"]["autofit"]
    editor.align.setCurrentIndex(editor.align.findData("right"))
    editor.select_region(editor.selected)
    assert editor.align.currentData() == "right"
    editor.progress_panel.show()
    editor.position_progress_panel()
    assert not editor.progress_panel.geometry().intersects(
        editor.zoom_overlay.geometry()
    )
    editor.submitting = True
    editor.cancel_job()
    assert editor.cancel_pending
    editor.submitting = False
    dispose_editor(editor)
