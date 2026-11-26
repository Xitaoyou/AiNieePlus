"""Native Qt geometry and mask interaction, always in original-image coordinates."""

import json
import weakref
from io import BytesIO

from PyQt5 import sip
from PyQt5.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)
from qfluentwidgets import isDarkTheme, qconfig

from Shared.manga_lettering import render_text


class RegionItem(QGraphicsRectItem):
    def __init__(self, region, rect_key, canvas):
        rect = region[rect_key]
        super().__init__(0, 0, rect["width"], rect["height"])
        self.region, self.rect_key, self.canvas = region, rect_key, canvas
        self.setPos(rect["x"], rect["y"])
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setFlag(QGraphicsItem.ItemIsMovable, not region.get("geometry_locked"))
        self.setPen(QPen(QColor("#25b9e8"), 2))
        self.setZValue(2)
        self.resizing = False

    def boundingRect(self):
        return super().boundingRect().adjusted(-2, -22, 12, 10)

    def paint(self, painter, option, widget=None):
        if (
            self.canvas.live_layout
            and self.rect_key == "layout_rect"
            and self.region.get("target")
        ):
            key = (
                self.rect().width(),
                self.rect().height(),
                self.region["target"],
                json.dumps(
                    [self.region["style"], self.canvas.punctuation_rules],
                    sort_keys=True,
                ),
                self.region["layout_rect"].get("rotation", 0),
            )
            if getattr(self, "text_key", None) != key:
                tile, self.layout_info = render_text(
                    self.region,
                    key[0],
                    key[1],
                    punctuation_rules=self.canvas.punctuation_rules,
                )
                # 延迟到事件循环：draw_canvas 期间 blockSignals 会吞掉本信号
                QTimer.singleShot(
                    0,
                    lambda canvas_ref=weakref.ref(self.canvas), rid=self.region["id"], info=self.layout_info: (
                        self.notify_layout(canvas_ref, rid, info)
                    ),
                )
                self.setToolTip(
                    "公共替换规则存在无效正则，已跳过，请在文本替换表修正"
                    if self.layout_info.get("invalid_rules")
                    else "字体文件不可用，预览使用内置字体，请重新选择字体"
                    if self.layout_info.get("font_missing")
                    else "拖动移动译文；右下角缩放并自动适应文字；右键更多操作"
                )
                buffer = BytesIO()
                tile.save(buffer, "PNG")
                self.text_pixmap = QPixmap()
                self.text_pixmap.loadFromData(buffer.getvalue())
                self.text_key = key
            painter.drawPixmap(0, 0, self.text_pixmap)
        if self.isSelected():
            painter.setPen(QPen(QColor("#ffbc42"), 2))
        elif self.rect_key == "ocr_rect":
            pen = QPen(QColor("#23c087"), 2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
        elif self.rect_key == "erase_rect":
            pen = QPen(QColor("#e78a4e"), 2)
            pen.setStyle(Qt.DashDotLine)
            painter.setPen(pen)
        else:
            painter.setPen(QPen(QColor("#25b9e8"), 2))
        painter.drawRect(self.rect())
        badge = {"ocr_rect": " 取字", "erase_rect": " 去字"}.get(self.rect_key, "")
        label = str(self.region.get("order", 0) + 1) + badge
        badge_color = painter.pen().color()
        metrics = painter.fontMetrics()
        pill_w = max(18, metrics.horizontalAdvance(label) + 10)
        pill_h = 16
        pill_y = -pill_h - 2 if self.pos().y() >= pill_h + 2 else 2
        pill_rect = QRectF(0, pill_y, pill_w, pill_h)
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(badge_color)
        painter.drawRoundedRect(pill_rect, 4, 4)
        painter.setPen(
            QColor("#1a1a1a")
            if badge_color == QColor("#ffbc42")
            else QColor("#ffffff")
        )
        painter.drawText(pill_rect, Qt.AlignCenter, label)
        painter.restore()
        if self.isSelected():
            painter.fillRect(
                QRectF(self.rect().width() - 8, self.rect().height() - 8, 8, 8),
                QColor("#ffbc42"),
            )

    @staticmethod
    def notify_layout(canvas_ref, region_id, info):
        canvas = canvas_ref()
        if canvas is not None and not sip.isdeleted(canvas):
            canvas.last_layout_info[region_id] = info
            canvas.layoutMeasured.emit(region_id, info)

    def mousePressEvent(self, event):
        self.resizing = (
            not self.region.get("geometry_locked")
            and event.pos().x() > self.rect().width() - 14
            and event.pos().y() > self.rect().height() - 14
        )
        self.old = dict(self.region[self.rect_key])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.resizing:
            width = min(
                max(4, event.pos().x()),
                self.canvas.sceneRect().width() - self.pos().x(),
            )
            height = min(
                max(4, event.pos().y()),
                self.canvas.sceneRect().height() - self.pos().y(),
            )
            self.setRect(0, 0, width, height)
        else:
            super().mouseMoveEvent(event)
            self.setPos(
                max(
                    0,
                    min(
                        self.pos().x(),
                        self.canvas.sceneRect().width() - self.rect().width(),
                    ),
                ),
                max(
                    0,
                    min(
                        self.pos().y(),
                        self.canvas.sceneRect().height() - self.rect().height(),
                    ),
                ),
            )

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        current = {
            **self.old,
            "x": self.pos().x(),
            "y": self.pos().y(),
            "width": self.rect().width(),
            "height": self.rect().height(),
        }
        if current != self.old:
            self.canvas.geometryChanged.emit(self.region["id"], self.rect_key, current)


class MangaCanvas(QGraphicsView):
    regionSelected = pyqtSignal(str)
    geometryChanged = pyqtSignal(str, str, dict)
    boxCreated = pyqtSignal(dict)
    strokeCreated = pyqtSignal(dict)
    deleteRequested = pyqtSignal()
    contextRequested = pyqtSignal(str, object)
    modeRequested = pyqtSignal(int)
    zoomChanged = pyqtSignal(float)
    layoutMeasured = pyqtSignal(str, dict)
    geometryEscapeRequested = pyqtSignal()
    escapeRequested = pyqtSignal()
    panStarted = pyqtSignal()
    panFinished = pyqtSignal()
    cycleRegionRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.scene().selectionChanged.connect(self.selection_changed)
        self.mode, self.rect_key = "select", "layout_rect"
        self.radius, self.lines = 8, []
        self.start = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setObjectName("mangaCanvas")
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.pan_origin = None
        self.space_held = False
        self.last_clicked = ""
        self.box_preview = None
        self.live_layout = False
        self.last_layout_info = {}
        self.punctuation_rules = None
        self.editable = True
        self.persisted_lines = []

    def apply_theme(self, *_):
        background = "#17191c" if isDarkTheme() else "#eef1f6"
        border = "#3b3e43" if isDarkTheme() else "#dfe3eb"
        self.setBackgroundBrush(QColor(background))
        self.setStyleSheet(
            f"QGraphicsView#mangaCanvas {{border:1px solid {border};border-radius:10px;}}"
        )

    def display(self, data, page, selected=None, fit=False):
        # Keep live region objects during selection/drag events. Replacing the
        # scene inside a QGraphicsItem event can invalidate its C++ receiver.
        if getattr(self, "display_page", None) != page["id"]:
            self.scene().clear()
            self.pixmap_item = self.scene().addPixmap(QPixmap())
            self.box_preview = None
            self.lines = []
            self.persisted_lines = []
            self.display_data = None
        self.display_page = page["id"]
        from PyQt5 import sip

        new_pixmap = not getattr(self, "pixmap_item", None) or sip.isdeleted(
            self.pixmap_item
        )
        if new_pixmap:
            self.pixmap_item = self.scene().addPixmap(QPixmap())
        if new_pixmap or getattr(self, "display_data", None) is not data:
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            self.pixmap_item.setPixmap(pixmap)
            self.display_data = data
        pm = self.pixmap_item.pixmap() if not sip.isdeleted(self.pixmap_item) else QPixmap()
        scene_w = pm.width() if pm.width() > 0 else page["width"]
        scene_h = pm.height() if pm.height() > 0 else page["height"]
        self.setSceneRect(0, 0, scene_w, scene_h)
        existing = {
            i.region["id"]: i for i in self.scene().items() if isinstance(i, RegionItem)
        }
        desired = {r["id"] for r in page["regions"]}
        for key, item in existing.items():
            if key not in desired or item.rect_key != self.rect_key:
                self.scene().removeItem(item)
        for region in page["regions"]:
            item = existing.get(region["id"])
            if item is None or item.scene() is None:
                item = RegionItem(region, self.rect_key, self)
                self.scene().addItem(item)
            else:
                item.region = region
                rect = region[self.rect_key]
                item.setPos(rect["x"], rect["y"])
                item.setRect(0, 0, rect["width"], rect["height"])
                item.setFlag(
                    QGraphicsItem.ItemIsMovable, not region.get("geometry_locked")
                )
            item.setVisible(self.editable)
            item.setEnabled(self.editable)
            item.update()
            item.setSelected(
                region["id"] in (selected if isinstance(selected, list) else [selected])
            )
        for line in self.persisted_lines:
            if not sip.isdeleted(line) and line.scene() is self.scene():
                self.scene().removeItem(line)
        self.persisted_lines = []
        if self.mode in {"mask", "eraser"}:
            for region in page["regions"]:
                if region["id"] not in (selected or []):
                    continue
                for stroke in region.get("mask_strokes", []):
                    points = stroke["points"]
                    pen = QPen(
                        QColor(80, 150, 250, 150)
                        if stroke["erase"]
                        else QColor(250, 80, 80, 110),
                        stroke["radius"] * 2,
                    )
                    pen.setCapStyle(Qt.RoundCap)
                    pairs = list(zip(points, points[1:])) or [(points[0], points[0])]
                    for start, end in pairs:
                        line = self.scene().addLine(
                            start[0], start[1], end[0], end[1], pen
                        )
                        line.setZValue(3)
                        self.persisted_lines.append(line)
        if fit:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def selection_changed(self):
        items = [i for i in self.scene().selectedItems() if isinstance(i, RegionItem)]
        if not items:
            self.last_clicked = ""
            self.regionSelected.emit("")
            return
        # 多选时 selectedItems() 的顺序是场景顺序，不是点击顺序；右栏应该
        # 跟着「最后点的那个框」走，否则用户改样式会改到别的框上。
        chosen = next(
            (i for i in items if i.region["id"] == self.last_clicked), items[0]
        )
        self.regionSelected.emit(chosen.region["id"])

    def selected_ids(self):
        return [
            i.region["id"]
            for i in self.scene().selectedItems()
            if isinstance(i, RegionItem)
        ]

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 0.05 < self.transform().m11() * factor < 8:
            self.scale(factor, factor)
            self.zoomChanged.emit(self.transform().m11())

    def contextMenuEvent(self, event):
        item = next(
            (i for i in self.items(event.pos()) if isinstance(i, RegionItem)), None
        )
        if item is not None:
            region_id = item.region["id"]
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
            position = event.globalPos()
            # 延后到事件循环：菜单构建可能触发场景重建，不能在 view 事件栈内
            QTimer.singleShot(
                0,
                lambda rid=region_id, pos=position: self.contextRequested.emit(
                    rid, pos
                ),
            )
            event.accept()
            return
        super().contextMenuEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton or (
            event.button() == Qt.LeftButton and self.space_held
        ):
            self.pan_origin = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            self.panStarted.emit()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.mode in {"add", "mask", "eraser"}:
            self.start = self.mapToScene(event.pos())
            self.points = [(max(0, self.start.x()), max(0, self.start.y()))]
            return
        if event.button() == Qt.LeftButton:
            hit = next(
                (
                    i
                    for i in self.items(event.pos())
                    if isinstance(i, RegionItem)
                ),
                None,
            )
            if hit is not None:
                self.last_clicked = hit.region["id"]
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self.pan_origin is None
            and event.buttons() & Qt.MiddleButton
            and self.mode == "select"
        ):
            # 中键在别的控件里按下后移进画布，同样接管拖动
            self.pan_origin = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            self.panStarted.emit()
            event.accept()
            return
        if self.pan_origin is not None:
            delta = event.pos() - self.pan_origin
            self.pan_origin = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            return
        if self.start is not None and self.mode == "add":
            rect = (
                QRectF(self.start, self.mapToScene(event.pos()))
                .normalized()
                .intersected(self.sceneRect())
            )
            if self.box_preview is None:
                self.box_preview = self.scene().addRect(
                    rect, QPen(QColor("#ffbc42"), 2, Qt.DashLine)
                )
            else:
                self.box_preview.setRect(rect)
            return
        if self.start is not None and self.mode in {"mask", "eraser"}:
            point = self.mapToScene(event.pos())
            point.setX(max(0, min(point.x(), self.sceneRect().width())))
            point.setY(max(0, min(point.y(), self.sceneRect().height())))
            old = self.points[-1]
            self.points.append((point.x(), point.y()))
            pen = QPen(
                QColor(250, 80, 80, 110)
                if self.mode == "mask"
                else QColor(80, 150, 250, 150),
                self.radius * 2,
            )
            pen.setCapStyle(Qt.RoundCap)
            self.lines.append(
                self.scene().addLine(old[0], old[1], point.x(), point.y(), pen)
            )
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.pan_origin is not None:
            self.pan_origin = None
            if self.space_held:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.unsetCursor()
            self.panFinished.emit()
            return
        if self.start is not None:
            if self.box_preview is not None:
                self.scene().removeItem(self.box_preview)
                self.box_preview = None
            point = self.mapToScene(event.pos())
            if self.mode == "add":
                rect = (
                    QRectF(self.start, point).normalized().intersected(self.sceneRect())
                )
                if rect.width() >= 4 and rect.height() >= 4:
                    self.boxCreated.emit(
                        {
                            "x": rect.x(),
                            "y": rect.y(),
                            "width": rect.width(),
                            "height": rect.height(),
                            "rotation": 0,
                        }
                    )
            elif self.mode in {"mask", "eraser"}:
                self.start = None
                self.clear_stroke_preview()
                self.strokeCreated.emit(
                    {
                        "points": self.points,
                        "radius": self.radius,
                        "erase": self.mode == "eraser",
                    }
                )
            self.start = None
            return
        super().mouseReleaseEvent(event)
        self.setDragMode(QGraphicsView.RubberBandDrag)

    def clear_stroke_preview(self):
        from PyQt5 import sip

        for line in self.lines:
            if not sip.isdeleted(line) and line.scene() is self.scene():
                self.scene().removeItem(line)
        self.lines = []

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            if not event.isAutoRepeat():
                self.space_held = True
                if self.pan_origin is None:
                    self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        if event.key() in {Qt.Key_Tab, Qt.Key_Backtab}:
            backward = event.key() == Qt.Key_Backtab or bool(
                event.modifiers() & Qt.ShiftModifier
            )
            self.cycleRegionRequested.emit(-1 if backward else 1)
            event.accept()
            return
        if event.key() in {Qt.Key_V, Qt.Key_B} and not event.modifiers():
            self.modeRequested.emit(0 if event.key() == Qt.Key_V else 1)
            return
        if event.key() == Qt.Key_Escape:
            had_preview = (
                self.start is not None or self.box_preview is not None
            )
            self.start = None
            self.clear_stroke_preview()
            if self.box_preview is not None:
                self.scene().removeItem(self.box_preview)
                self.box_preview = None
            if had_preview:
                return
            if self.rect_key != "layout_rect":
                self.geometryEscapeRequested.emit()
            else:
                self.escapeRequested.emit()
            return
        if event.key() == Qt.Key_Delete:
            self.deleteRequested.emit()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self.space_held = False
            if self.pan_origin is None:
                self.unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)
