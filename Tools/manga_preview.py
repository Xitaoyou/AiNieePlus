"""Open the native manga editor against a running local AiNiee backend."""

import argparse
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from Frontend.pages.media.manga_page import MangaPage
from Frontend.services.backend_client import BackendClient


def main():
    from Frontend.platform.qt_runtime import configure_qt_plugins

    configure_qt_plugins()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:33899")
    args = parser.parse_args()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    backend = BackendClient(args.url)
    page = MangaPage(backend=backend)
    page.setWindowTitle("AiNieePlus 漫画翻译 — 开发预览")
    available = app.desktop().availableGeometry()
    page.resize(int(available.width() * 0.94), int(available.height() * 0.9))
    page.show()
    try:
        return app.exec_()
    finally:
        page.cleanup()
        backend.http.close()


if __name__ == "__main__":
    sys.exit(main())
