"""Offline check of the installed editor, including fonts and live text layers."""

import os


def check_editor():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from io import BytesIO

    from PIL import Image
    from PyQt5.QtGui import QImage, QPainter
    from PyQt5.QtWidgets import QApplication

    from Frontend.pages.media.manga_canvas import MangaCanvas, RegionItem
    from Frontend.platform.qt_runtime import configure_qt_plugins
    from Shared.manga_lettering import default_font

    configure_qt_plugins()
    app = QApplication.instance() or QApplication([])
    if not default_font():
        raise RuntimeError("Installed editor cannot locate its bundled font")
    rect = {"x": 10, "y": 10, "width": 180, "height": 240, "rotation": 0}
    region = {
        "id": "frame",
        "layout_rect": rect,
        "target": "实时排版与字体缩放检查" * 8,
        "style": {
            "font": "",
            "size": 40,
            "min_size": 10,
            "autofit": True,
            "vertical": True,
            "bold": False,
            "color": "#000000",
            "stroke_color": "#ffffff",
            "stroke_width": 1,
            "line_spacing": 1.2,
            "char_spacing": 0,
            "padding": 4,
            "align": "center",
        },
    }
    canvas = MangaCanvas()
    canvas.live_layout = True
    data = BytesIO()
    Image.new("RGB", (240, 320), "white").save(data, "PNG")
    canvas.display(
        data.getvalue(),
        {"id": "page", "width": 240, "height": 320, "regions": [region]},
    )

    def render():
        image = QImage(240, 320, QImage.Format_ARGB32)
        painter = QPainter(image)
        canvas.scene().render(painter)
        painter.end()

    render()
    item = next(i for i in canvas.scene().items() if isinstance(i, RegionItem))
    size = item.layout_info["font_size"]
    item.setRect(0, 0, 55, 130)
    render()
    if item.layout_info["font_size"] >= size or item.layout_info["overflow"]:
        raise RuntimeError("Installed editor failed the live text resize check")
    canvas.close()
    app.processEvents()


if __name__ == "__main__":
    check_editor()
