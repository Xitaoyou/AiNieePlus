"""Native Qt geometry and mask interaction, always in original-image coordinates."""

from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QPen, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QGraphicsRectItem,
    QGraphicsItem,
)
from qfluentwidgets import qconfig, isDarkTheme


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
        self.setPen(QPen(QColor("#ffbc42" if self.isSelected() else "#25b9e8"), 2))
        super().paint(painter, option, widget)
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

    def apply_theme(self, *_):
        background = "#17191c" if isDarkTheme() else "#eef1f6"
        border = "#3b3e43" if isDarkTheme() else "#dfe3eb"
        self.setBackgroundBrush(QColor(background))
        self.setStyleSheet(
            f"QGraphicsView#mangaCanvas {{border:1px solid {border};border-radius:10px;}}"
        )

    def display(self, data, page, selected=None, fit=False):
        self.scene().clear()
        self.box_preview = None
        self.lines = []
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        self.scene().addPixmap(pixmap)
        self.setSceneRect(0, 0, page["width"], page["height"])
        for region in page["regions"]:
            item = RegionItem(region, self.rect_key, self)
            self.scene().addItem(item)
            item.setSelected(
                region["id"] in (selected if isinstance(selected, list) else [selected])
            )
        if fit:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def selection_changed(self):
        items = [i for i in self.scene().selectedItems() if isinstance(i, RegionItem)]
        if items:
            self.regionSelected.emit(items[0].region["id"])

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
                        dict(
                            x=rect.x(),
                            y=rect.y(),
                            width=rect.width(),
                            height=rect.height(),
                            rotation=0,
                        )
                    )
            elif self.mode in {"mask", "eraser"}:
                self.strokeCreated.emit(
                    dict(
                        points=self.points,
                        radius=self.radius,
                        erase=self.mode == "eraser",
                    )
                )
            self.start = None
            return
        super().mouseReleaseEvent(event)
        self.setDragMode(QGraphicsView.RubberBandDrag)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.deleteRequested.emit()
        else:
            super().keyPressEvent(event)
