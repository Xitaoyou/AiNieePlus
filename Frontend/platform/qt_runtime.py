"""Resolve wheel plugins without Qt 5's lossy Windows non-ASCII prefix lookup."""

from pathlib import Path
import sys


def configure_qt_plugins():
    if getattr(sys, "frozen", False):
        return
    import PyQt5
    from PyQt5.QtCore import QCoreApplication

    plugins = Path(PyQt5.__path__[0]) / "Qt5/plugins"
    if plugins.is_dir():
        QCoreApplication.setLibraryPaths([str(plugins)])
