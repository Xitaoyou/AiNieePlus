"""Native Qt geometry and mask interaction, always in original-image coordinates."""

import json
from io import BytesIO

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
                    lambda rid=self.region["id"], info=self.layout_info: self.canvas.layoutMeasured.emit(
                        rid, info
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
        painter.setPen(QPen(QColor("#ffbc42" if self.isSelected() else "#25b9e8"), 2))
        painter.drawRect(self.rect())
        painter.drawText(
            self.rect().adjusted(4, 0, -4, 0),
            Qt.AlignTop | Qt.AlignLeft,
            str(self.region.get("order", 0) + 1),
        )
        if self.isSelected():
            painter.fillRect(
                QRectF(self.rect().width() - 8, self.rect().height() - 8, 8, 8),
                QColor("#ffbc42"),
            )

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
        self.box_preview = None
        self.live_layout = False
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
        self.setSceneRect(0, 0, page["width"], page["height"])
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
        self.regionSelected.emit(items[0].region["id"] if items else "")

    def selected_ids(self):
        return [
            i.region["id"]
            for i in self.scene().selectedItems()
            if isinstance(i, RegionItem)
        ]

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 0.02 < self.transform().m11() * factor < 30:
            self.scale(factor, factor)
            self.zoomChanged.emit(self.transform().m11())

    def contextMenuEvent(self, event):
        item = next(
            (i for i in self.items(event.pos()) if isinstance(i, RegionItem)), None
        )
        if item is not None:
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
            self.contextRequested.emit(item.region["id"], event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self.pan_origin = event.pos()
            return
        if event.button() == Qt.LeftButton and self.mode in {"add", "mask", "eraser"}:
            self.start = self.mapToScene(event.pos())
            self.points = [(max(0, self.start.x()), max(0, self.start.y()))]
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
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
        if event.key() in {Qt.Key_V, Qt.Key_B} and not event.modifiers():
            self.modeRequested.emit(0 if event.key() == Qt.Key_V else 1)
            return
        if event.key() == Qt.Key_Escape:
            self.start = None
            self.clear_stroke_preview()
            if self.box_preview is not None:
                self.scene().removeItem(self.box_preview)
                self.box_preview = None
            return
        if event.key() == Qt.Key_Delete:
            self.deleteRequested.emit()
        else:
            super().keyPressEvent(event)
