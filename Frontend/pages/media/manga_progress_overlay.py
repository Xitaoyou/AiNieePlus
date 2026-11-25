"""Unified bottom-right progress and notification overlay for the manga editor."""

from PyQt5 import sip
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout
from qfluentwidgets import (
    FluentIcon,
    SimpleCardWidget,
    TransparentToolButton,
    isDarkTheme,
    qconfig,
)

STATUS_TONES = {
    "idle": "#8a8a8a",
    "busy": "#0f6cbd",
    "ok": "#0f7b0f",
    "warn": "#9d5d00",
    "error": "#c42b1c",
}

STAGE_TITLES = {
    "detect": "检测文字框",
    "ocr": "OCR 识别",
    "recognize": "检测与 OCR",
    "translate": "翻译文字框",
    "clean": "精准去字",
    "render": "排版渲染",
    "pipeline": "翻译并排版",
    "download": "下载模型",
}


class MangaProgressOverlay(SimpleCardWidget):
    """Floating bottom-right card showing task progress and model/OCR notices."""

    detailsRequested = pyqtSignal()
    dismissed = pyqtSignal()
    layoutChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mangaProgressOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setBorderRadius(10)
        self._notices = []
        self._tone = "idle"
        self._auto_hide_armed = False
        self._auto_hide_ms = 5000

        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self._on_auto_hide)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        self.tone_dot = QLabel(self)
        self.tone_dot.setFixedSize(8, 8)
        header.addWidget(self.tone_dot, 0, Qt.AlignVCenter)

        self.title_label = QLabel("尚未开始任务", self)
        self.title_label.setWordWrap(False)
        header.addWidget(self.title_label, 1)

        self.cancel_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.cancel_button.setToolTip("取消当前任务")
        header.addWidget(self.cancel_button)

        self.details_button = TransparentToolButton(FluentIcon.INFO, self)
        self.details_button.setToolTip("查看任务明细")
        self.details_button.clicked.connect(self.detailsRequested)
        self.details_button.hide()
        header.addWidget(self.details_button)

        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.setToolTip("关闭提示")
        self.close_button.clicked.connect(self._dismiss)
        self.close_button.hide()
        header.addWidget(self.close_button)

        layout.addLayout(header)

        self.progress_label = QLabel("尚未开始翻译", self)
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.notices_label = QLabel(self)
        self.notices_label.setWordWrap(True)
        self.notices_label.hide()
        layout.addWidget(self.notices_label)

        self.set_tone("idle")
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        self.destroyed.connect(self._detach_theme)
        self.hide()

    def _detach_theme(self, *_):
        try:
            qconfig.themeChanged.disconnect(self.apply_theme)
        except (TypeError, RuntimeError):
            pass

    def cleanup(self):
        self._auto_hide_timer.stop()
        self._auto_hide_armed = False
        self._detach_theme()

    def apply_theme(self, *_):
        if sip.isdeleted(self):
            return
        dark = isDarkTheme()
        if dark:
            bg = "rgba(32, 32, 32, 244)"
            border = "rgba(255, 255, 255, 0.16)"
            title_color = "#f3f3f3"
            text_color = "#d4d4d4"
            notice_color = "#ff9f9f" if self._tone == "error" else "#ffd369"
            bar_bg = "rgba(255, 255, 255, 0.14)"
            bar_chunk = "#4cc2ff"
        else:
            bg = "rgba(255, 255, 255, 248)"
            border = "rgba(0, 0, 0, 0.12)"
            title_color = "#1f1f1f"
            text_color = "#555555"
            notice_color = "#b42318" if self._tone == "error" else "#8a4f00"
            bar_bg = "rgba(0, 0, 0, 0.08)"
            bar_chunk = "#0f6cbd"

        self.setStyleSheet(
            f"SimpleCardWidget#mangaProgressOverlay {{"
            f"background-color: {bg}; border: 1px solid {border}; border-radius: 10px;"
            f"}}"
        )
        self.title_label.setStyleSheet(
            f"color: {title_color}; font-weight: 600; background: transparent;"
        )
        self.progress_label.setStyleSheet(
            f"color: {text_color}; background: transparent;"
        )
        self.notices_label.setStyleSheet(
            f"color: {notice_color}; font-size: 12px; background: transparent;"
        )
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {bar_bg}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {bar_chunk}; border-radius: 3px; }}"
        )

    def set_tone(self, tone):
        self._tone = tone
        color = STATUS_TONES.get(tone, STATUS_TONES["idle"])
        self.tone_dot.setStyleSheet(f"background-color:{color};border-radius:4px;")
        self.apply_theme()

    def _add_notice(self, text):
        text = str(text or "").strip()
        if not text:
            return
        if text not in self._notices:
            self._notices.append(text)
            if len(self._notices) > 3:
                self._notices = self._notices[-3:]
        self._sync_notices()

    def _sync_notices(self):
        if self._notices:
            self.notices_label.setText("\n".join("• " + item for item in self._notices))
            self.notices_label.show()
        else:
            self.notices_label.clear()
            self.notices_label.hide()

    def start(self, title="正在提交任务…", detail=""):
        self._auto_hide_timer.stop()
        self._auto_hide_armed = False
        self._notices = []
        self._sync_notices()
        self.set_tone("busy")
        self.title_label.setText(title)
        self.progress_label.setText(detail or title)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.cancel_button.show()
        self.details_button.hide()
        self.close_button.hide()
        self.show()
        self.layoutChanged.emit()

    def update_progress(
        self,
        state_text,
        progress,
        model_names=None,
        page_numbers=None,
    ):
        progress = progress or {}
        model_names = model_names or {}
        page_numbers = page_numbers or {}

        stage = progress.get("stage", "")
        model_id = progress.get("model", "")
        model_label = model_names.get(model_id, model_id) if model_id else ""

        if "downloaded_bytes" in progress:
            stage_title = STAGE_TITLES["download"]
        else:
            stage_title = STAGE_TITLES.get(stage, state_text or "正在处理")

        if model_label:
            header_title = f"{stage_title} · {model_label}"
        else:
            header_title = stage_title
        self.title_label.setText(header_title)

        page_id = progress.get("page_id")
        page_no = progress.get("page_index") or page_numbers.get(page_id)
        if not page_no and progress.get("current"):
            page_no = progress.get("current")

        region_total = progress.get("region_total", 0) or 0
        region_index = progress.get("region_index", 0) or 0
        total = progress.get("total", progress.get("total_line", 0)) or 0
        current = progress.get("current", progress.get("line", 0)) or 0

        detail = ""
        if "downloaded_bytes" in progress:
            self.progress_bar.setRange(0, 0)
            detail = f"已下载 {progress['downloaded_bytes'] / 1024**2:.1f} MB"
        elif region_total > 0:
            if total <= 1:
                self.progress_bar.setRange(0, region_total)
                self.progress_bar.setValue(min(region_index, region_total))
                page_prefix = f"第 {page_no} 页 · " if page_no else ""
                detail = f"{page_prefix}OCR {region_index} / {region_total} 框"
            else:
                self.progress_bar.setRange(0, total * 100)
                page_pos = max(0, current - 1) * 100
                box_pos = int(region_index * 100 / region_total)
                self.progress_bar.setValue(min(total * 100, page_pos + box_pos))
                detail = (
                    f"第 {current} / {total} 页 · OCR {region_index} / {region_total} 框"
                )
        elif stage == "detect":
            if total <= 1 and current == 0:
                self.progress_bar.setRange(0, 0)
                detail = (
                    f"正在检测第 {page_no} 页…" if page_no else "正在检测当前页…"
                )
            else:
                self.progress_bar.setRange(0, max(1, total))
                self.progress_bar.setValue(min(current, max(1, total)))
                shown = page_no or current
                detail = f"检测第 {shown} / {total} 页"
        elif "current" in progress:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(min(current, max(1, total)))
            label = {
                "detect": "检测",
                "ocr": "识别",
                "translate": "翻译",
                "clean": "去字",
                "render": "排版",
                "recognize": "识别",
            }.get(stage, "")
            detail = f"{label} {current} / {total} {progress.get('unit', '页')}"
        elif "line" in progress:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(min(current, max(1, total)))
            detail = f"翻译 {progress['line']} / {progress.get('total_line', 0)} 条"
        else:
            detail = state_text

        if progress.get("error_requests"):
            detail += f" · 失败 {progress['error_requests']} 次"
        if "total_requests" in progress:
            detail += f" · 已请求 {progress['total_requests']} 次"

        self.progress_label.setText(detail or state_text)

        event = progress.get("event")
        if event == "fallback":
            added = progress.get("added", 0)
            prefix = f"第 {page_no} 页" if page_no else "当前页"
            suffix = f"（+{added} 框）" if added else ""
            self._add_notice(f"{prefix}触发行级回退补检{suffix}")
        elif event == "empty":
            prefix = f"第 {page_no} 页" if page_no else "当前页"
            self._add_notice(f"{prefix}未检出文字框")
        elif stage == "ocr" and progress.get("empty"):
            prefix = f"第 {page_no} 页" if page_no else "当前页"
            self._add_notice(f"{prefix}有文字框 OCR 未识别出文字")

        self.progress_bar.show()
        self.cancel_button.show()
        self.details_button.hide()
        self.close_button.hide()
        if not self.isVisible():
            self.show()
        self.layoutChanged.emit()

    def finish(self, tone, title, detail, notices=None, auto_hide_ms=5000):
        self._auto_hide_timer.stop()
        self._auto_hide_armed = False
        self._auto_hide_ms = auto_hide_ms
        self.set_tone(tone)
        self.title_label.setText(title)
        self.progress_label.setText(detail)
        self.cancel_button.hide()
        self.details_button.show()
        self.close_button.show()

        if tone == "error" and self.progress_bar.maximum() == 0:
            self.progress_bar.hide()
        else:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            if tone == "ok":
                self.progress_bar.setValue(self.progress_bar.maximum())
            self.progress_bar.show()

        if notices is not None:
            self._notices = [str(n).strip() for n in notices if str(n).strip()][:3]
            self._sync_notices()

        self.show()
        self.layoutChanged.emit()

        if tone == "ok" and not self._notices and auto_hide_ms > 0:
            self._auto_hide_armed = True
            self._auto_hide_timer.start(auto_hide_ms)

    def _on_auto_hide(self):
        self._auto_hide_armed = False
        self.hide()

    def _dismiss(self):
        self._auto_hide_timer.stop()
        self._auto_hide_armed = False
        self.hide()
        self.dismissed.emit()

    def enterEvent(self, event):
        if self._auto_hide_timer.isActive():
            self._auto_hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._auto_hide_armed and self.isVisible():
            self._auto_hide_timer.start(min(self._auto_hide_ms, 2000))
        super().leaveEvent(event)
