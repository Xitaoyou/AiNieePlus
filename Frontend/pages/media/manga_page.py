"""Native editable manga workspace. Network and inference never run on the GUI thread."""

from copy import deepcopy
from functools import wraps
from pathlib import Path
from uuid import uuid4

from PyQt5 import sip
from PyQt5.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPalette
from PyQt5.QtWidgets import (
    QColorDialog,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QTableWidgetItem,
    QUndoCommand,
    QUndoStack,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtWidgets import (
    QLabel as NativeLabel,
)
from qfluentwidgets import (
    CaptionLabel as QLabel,
)
from qfluentwidgets import (
    CheckBox as QCheckBox,
)
from qfluentwidgets import (
    ComboBox as FluentComboBox,
)
from qfluentwidgets import (
    DoubleSpinBox as QDoubleSpinBox,
)
from qfluentwidgets import (
    FluentIcon,
    PrimaryPushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentPushButton,
    isDarkTheme,
    qconfig,
)
from qfluentwidgets import (
    ListWidget as QListWidget,
)
from qfluentwidgets import (
    PlainTextEdit as QPlainTextEdit,
)
from qfluentwidgets import (
    PushButton as QPushButton,
)
from qfluentwidgets import (
    SingleDirectionScrollArea as QScrollArea,
)
from qfluentwidgets import (
    SpinBox as QSpinBox,
)
from qfluentwidgets import (
    TableWidget as QTableWidget,
)

from Frontend.pages.media.manga_canvas import MangaCanvas
from Frontend.pages.media.start_page import MediaStartPage
from Frontend.services.manga_client import MangaClient
from Frontend.services.manga_edits import apply as apply_changes
from Frontend.services.manga_edits import changes
from Frontend.services.manga_merge import merge_edits


def form_update(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        previous = self.loading_form
        self.loading_form = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self.loading_form = previous

    return guarded


class QComboBox(FluentComboBox):
    """Keep the transport IDs in userData, matching Qt's two-argument addItem."""

    def addItem(self, text, userData=None, **kwargs):
        return super().addItem(text, userData=userData, **kwargs)


class Signals(QObject):
    done = pyqtSignal(object, object)


class Background(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action, self.signals = action, Signals()

    def run(self):
        try:
            self.signals.done.emit(self.action(), None)
        except Exception as error:
            self.signals.done.emit(None, str(error))


class EditCommand(QUndoCommand):
    def __init__(self, page, before, after, label):
        super().__init__(label)
        self.page = page
        self.edits = changes(before, after)

    def redo(self):
        apply_changes(self.page.document, self.edits)
        self.page.apply_edit(self.page.document)

    def undo(self):
        apply_changes(self.page.document, self.edits, forward=False)
        self.page.apply_edit(self.page.document)


class ModelDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.setObjectName("mangaModelDialog")
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        self.page = page
        self.setWindowTitle("漫画模型中心")
        self.resize(980, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "核心模型随安装包提供。扩展模型保留原权重版本；legacy 可从 ImageTrans 模型文件夹导入。"
            )
        )
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["模型", "状态", "大小", "用途与建议", "来源许可"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        for label, action in [
            ("下载", self.download),
            ("导入目录", self.import_model),
            ("校验", self.verify),
            ("官方说明", self.link),
            ("刷新", self.refresh),
        ]:
            button = QPushButton(label)
            button.clicked.connect(action)
            bar.addWidget(button)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.refresh()

    def apply_theme(self, *_):
        background = "#202020" if isDarkTheme() else "#f7f9fc"
        self.setStyleSheet(f"QDialog#mangaModelDialog {{background:{background};}}")

    def refresh(self):
        self.page.background(
            lambda: self.page.client.request("GET", "/models"), self.loaded
        )

    def loaded(self, data):
        self.models = data["models"]
        self.table.setRowCount(len(self.models))
        for row, model in enumerate(self.models):
            for col, value in enumerate(
                [
                    model["name"],
                    "已安装"
                    if model["installed"]
                    else "未安装"
                    if model.get("supported", True)
                    else "尚未适配",
                    f"{model['size_mb']} MB",
                    model["advice"],
                    model["license"],
                ]
            ):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def selected(self):
        row = self.table.currentRow()
        return self.models[row] if row >= 0 else None

    def download(self):
        model = self.selected()
        if model:
            self.status.setText("下载中，可在主页面取消任务…")
            self.page.start_request(
                lambda: self.page.client.request(
                    "POST",
                    f"/models/{model['id']}/download",
                    headers={"Prefer": "respond-async"},
                ),
                lambda r: self.refresh(),
            )

    def import_model(self):
        model = self.selected()
        if not model:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "选择模型目录或 ImageTrans 文件夹"
        )
        if folder:
            self.page.start_request(
                lambda: self.page.client.request(
                    "POST",
                    f"/models/{model['id']}/import",
                    json={"path": folder},
                    headers={"Prefer": "respond-async"},
                ),
                lambda r: self.refresh(),
            )

    def verify(self):
        model = self.selected()
        if model:
            self.page.background(
                lambda: self.page.client.request(
                    "POST", f"/models/{model['id']}/verify"
                ),
                lambda r: self.status.setText("校验通过"),
            )

    def link(self):
        model = self.selected()
        if model:
            QDesktopServices.openUrl(QUrl(model["source"]))


class MangaPage(QWidget):
    def __init__(self, parent=None, backend=None):
        super().__init__(parent)
        self.setObjectName("manga_translation_page")
        self.client = MangaClient(backend or parent.config_backend())
        self.document = None
        self.selected = None
        self.page_id = None
        self.image_data = None
        self.image_is_source = True
        self.epoch = 0
        self.saving = False
        self.job_id = None
        self.loading_form = False
        self.preview_pending = False
        self.session = 0
        self.follow_progress = True
        self.syncing = False
        self.undo = QUndoStack(self)
        self.undo.setUndoLimit(100)
        self.pool = QThreadPool.globalInstance()
        self.tasks = set()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(500)
        self.save_timer.timeout.connect(self.save)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.poll_job)
        self.polling = False
        self.submitting = False
        self.dirty = False
        self.save_callbacks = []
        self.build_ui()
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        QTimer.singleShot(0, self.refresh_history)
        QTimer.singleShot(0, self.refresh_providers)

    def apply_theme(self, *_):
        self.setAttribute(Qt.WA_StyledBackground, True)
        background = "#202020" if isDarkTheme() else "#f7f9fc"
        self.setStyleSheet(
            f"QWidget#manga_translation_page {{background:{background};}}"
        )
        palette = self.palette()
        palette.setColor(
            QPalette.WindowText, QColor("#ffffff" if isDarkTheme() else "#202020")
        )
        self.setPalette(palette)

    def build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.workspace_stack = QStackedWidget(self)
        outer.addWidget(self.workspace_stack)
        self.start_page = MediaStartPage("image", self)
        self.start_page.layout().setContentsMargins(24, 24, 24, 20)
        self.start_page.drop_card.title_label.setText("将漫画页面拖到这里")
        self.start_page.drop_card.file_extensions = frozenset(
            {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
        )
        self.start_page.pick_files.clicked.connect(self.import_images)
        self.start_page.pick_folder.clicked.connect(
            lambda: self.import_images(folder=True)
        )
        self.start_page.drop_card.pathsDropped.connect(self.import_paths)
        self.start_page.history_list.itemClicked.connect(
            lambda item: self.open_workspace(item.data(Qt.UserRole))
        )
        self.start_page.history_list.itemActivated.connect(
            lambda item: self.open_workspace(item.data(Qt.UserRole))
        )
        self.start_page.deleteRequested.connect(self.delete_workspace)
        self.workspace_stack.addWidget(self.start_page)
        self.work_page = QWidget(self)
        self.workspace_stack.addWidget(self.work_page)
        root = QVBoxLayout(self.work_page)
        root.setContentsMargins(24, 16, 24, 12)
        root.setSpacing(12)
        heading = QHBoxLayout()
        back = TransparentPushButton(FluentIcon.RETURN, "返回", self.work_page)
        back.clicked.connect(self.back_to_start)
        heading.addWidget(back)
        heading.addWidget(SubtitleLabel("漫画翻译", self.work_page))
        heading.addStretch(1)
        heading.addWidget(QLabel("本地识别  ·  人工校对  ·  精修排版", self.work_page))
        root.addLayout(heading)
        command_card = SimpleCardWidget(self.work_page)
        command_card.setBorderRadius(12)
        commands = QVBoxLayout(command_card)
        commands.setContentsMargins(14, 12, 14, 12)
        commands.setSpacing(10)
        root.addWidget(command_card)
        top = QHBoxLayout()
        commands.addLayout(top)
        self.history = QComboBox()
        self.history.setMinimumWidth(160)
        top.addWidget(self.history)
        self.button(top, "打开", self.open_history)
        self.button(top, "导入图片", self.import_images)
        self.button(top, "导入文件夹", lambda: self.import_images(folder=True))
        self.button(top, "打开工程包", self.import_project)
        self.button(top, "模型中心", lambda: ModelDialog(self).exec())
        top.addStretch(1)
        actions = QHBoxLayout()
        commands.addLayout(actions)
        self.button(actions, "保存", self.save)
        self.button(actions, "导出", self.export)
        self.button(actions, "导出原文", lambda: self.export_translation(True))
        self.button(actions, "导出译文", lambda: self.export_translation(False))
        self.button(actions, "导入译文", self.import_translation)
        self.button(actions, "撤销", self.undo.undo)
        self.button(actions, "重做", self.undo.redo)
        self.button(actions, "取消任务", self.cancel_job)
        actions.addStretch(1)
        stages = QHBoxLayout()
        commands.addLayout(stages)
        self.scope = QComboBox()
        self.scope.addItems(["当前页", "全部页面", "选中文字框"])
        self.scope.setCurrentIndex(1)
        stages.addWidget(self.scope)
        for label, stage in [
            ("检测", "detect"),
            ("OCR", "ocr"),
            ("翻译", "translate"),
            ("排版预览", "render"),
            ("翻译并生成", "pipeline"),
        ]:
            self.button(stages, label, lambda checked=False, s=stage: self.run(s))
        stages = QHBoxLayout()
        commands.addLayout(stages)
        self.precise = QCheckBox("精准去字（整个工程）")
        self.precise.toggled.connect(self.toggle_precise)
        stages.addWidget(self.precise)
        self.button(stages, "跟随处理页", self.resume_follow)
        self.overwrite = QCheckBox("替换已有结果")
        stages.addWidget(self.overwrite)
        self.variant = QComboBox()
        for title, value in [
            ("原图", "source"),
            ("成品图", "render"),
            ("去字图", "clean"),
            ("蒙版", "mask"),
            ("文字层", "layer"),
        ]:
            self.variant.addItem(title, value)
        self.variant.currentIndexChanged.connect(self.load_image)
        stages.addWidget(self.variant)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)
        left = SimpleCardWidget()
        left.setBorderRadius(12)
        left.setMinimumWidth(160)
        left.setMaximumWidth(270)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(StrongBodyLabel("页面", left))
        self.pages = QListWidget()
        left_layout.addWidget(self.pages)
        page_bar = QHBoxLayout()
        left_layout.addLayout(page_bar)
        self.button(page_bar, "↑", lambda: self.move_page(-1))
        self.button(page_bar, "↓", lambda: self.move_page(1))
        self.button(page_bar, "移除页", self.delete_page)
        split.addWidget(left)
        self.pages.currentRowChanged.connect(self.select_page)
        center = SimpleCardWidget()
        center.setBorderRadius(12)
        center_layout = QVBoxLayout(center)
        tools = QHBoxLayout()
        center_layout.addLayout(tools)
        self.tool = QComboBox()
        self.tool.addItems(["选择/移动", "建立OCR框", "蒙版画笔", "蒙版橡皮"])
        self.tool.currentIndexChanged.connect(self.change_tool)
        tools.addWidget(self.tool)
        self.geometry = QComboBox()
        for title, key in [
            ("译文位置", "layout_rect"),
            ("OCR区域", "ocr_rect"),
            ("擦除区域", "erase_rect"),
        ]:
            self.geometry.addItem(title, key)
        self.geometry.currentIndexChanged.connect(self.change_geometry)
        tools.addWidget(self.geometry)
        self.brush = QSpinBox()
        self.brush.setRange(1, 256)
        self.brush.setFixedWidth(90)
        self.brush.setToolTip("蒙版画笔半径")
        self.brush.setValue(8)
        self.brush.valueChanged.connect(lambda v: setattr(self.canvas, "radius", v))
        tools.addWidget(self.brush)
        self.button(
            tools,
            "适应窗口",
            lambda: self.canvas.fitInView(self.canvas.sceneRect(), Qt.KeepAspectRatio),
        )
        self.button(tools, "删除框", self.delete_regions)
        self.canvas = MangaCanvas()
        center_layout.addWidget(self.canvas, 1)
        split.addWidget(center)
        self.canvas.regionSelected.connect(self.select_region)
        self.canvas.geometryChanged.connect(self.geometry_changed, Qt.QueuedConnection)
        self.canvas.boxCreated.connect(self.add_region)
        self.canvas.strokeCreated.connect(self.add_stroke)
        self.canvas.deleteRequested.connect(self.delete_regions)
        scroll = QScrollArea()
        scroll.setMinimumWidth(340)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea {border:none;background:transparent;}")
        right = SimpleCardWidget()
        right.setBorderRadius(12)
        form = QFormLayout(right)
        form.setContentsMargins(16, 16, 16, 16)
        form.setVerticalSpacing(12)
        form.addRow(StrongBodyLabel("识别与排版", right))
        scroll.setWidget(right)
        right.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        split.addWidget(scroll)
        self.detector = QComboBox()
        self.detector.addItems(["manga-panel", "legacy-balloon-yolov4"])
        self.ocr = QComboBox()
        self.ocr.addItems(["mangaocr", "rapidocr"])
        form.addRow("检测模型", self.detector)
        form.addRow("OCR模型", self.ocr)
        self.detector.currentTextChanged.connect(
            lambda v: self.set_setting("detector", v)
        )
        self.ocr.currentTextChanged.connect(lambda v: self.set_setting("ocr", v))
        self.panels = QCheckBox("检测分镜并组织阅读顺序")
        self.panels.setChecked(True)
        form.addRow(self.panels)
        self.panels.toggled.connect(lambda v: self.set_setting("detect_panels", v))
        self.source_language = QComboBox()
        self.source_language.addItems(["ja", "zh", "en", "ko"])
        form.addRow("原文语言", self.source_language)
        self.target_language = QComboBox()
        self.target_language.addItems(["zh-CN", "en", "ja", "ko"])
        form.addRow("目标语言", self.target_language)
        self.source_language.currentTextChanged.connect(
            lambda v: self.set_setting("source_language", v)
        )
        self.target_language.currentTextChanged.connect(
            lambda v: self.set_setting("target_language", v)
        )
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.05, 1)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(0.5)
        form.addRow("检测阈值", self.confidence)
        self.confidence.valueChanged.connect(
            lambda v: self.set_setting("confidence", v)
        )
        self.rtl = QCheckBox("从右向左阅读")
        self.rtl.setChecked(True)
        form.addRow(self.rtl)
        self.rtl.toggled.connect(lambda v: self.set_setting("right_to_left", v))
        self.auto_recognize = QCheckBox("导入后自动检测与 OCR")
        self.auto_recognize.setChecked(True)
        form.addRow(self.auto_recognize)
        self.auto_recognize.toggled.connect(
            lambda v: self.set_setting("auto_recognize", v)
        )
        self.provider = QComboBox()
        self.provider.addItem("使用文本翻译当前接口", None)
        form.addRow("翻译接口", self.provider)
        self.provider.currentIndexChanged.connect(
            lambda: self.set_setting("provider_id", self.provider.currentData())
        )
        self.regions = QTableWidget()
        self.regions.setColumnCount(4)
        self.regions.setHorizontalHeaderLabels(["顺序", "OCR 原文", "译文", "状态"])
        self.regions.setMinimumHeight(150)
        self.regions.setWordWrap(True)
        self.regions.setSelectionBehavior(QTableWidget.SelectRows)
        self.regions.setColumnWidth(0, 60)
        self.regions.setColumnWidth(3, 110)
        self.regions.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.regions.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.regions.cellChanged.connect(self.review_changed)
        vertical_split = QSplitter(Qt.Vertical)
        root.removeWidget(split)
        vertical_split.addWidget(split)
        review = SimpleCardWidget()
        review_layout = QVBoxLayout(review)
        review_layout.addWidget(StrongBodyLabel("阅读顺序 · 点击文字框校对原文与译文"))
        review_layout.addWidget(self.regions)
        vertical_split.addWidget(review)
        vertical_split.setSizes([650, 180])
        root.addWidget(vertical_split, 1)
        self.regions.currentCellChanged.connect(
            lambda row, col, old_row, old_col: self.region_row(row)
        )
        reorder = QHBoxLayout()
        self.button(reorder, "上移框", lambda: self.move_region(-1))
        self.button(reorder, "下移框", lambda: self.move_region(1))
        self.button(reorder, "合并框", self.merge_regions)
        self.button(reorder, "拆分框", self.split_region)
        form.addRow(reorder)
        self.source = QPlainTextEdit()
        self.source.setMaximumHeight(95)
        self.target = QPlainTextEdit()
        self.target.setMaximumHeight(95)
        form.addRow("OCR原文", self.source)
        form.addRow("译文", self.target)
        self.source.textChanged.connect(
            lambda: self.region_value("source", self.source.toPlainText())
        )
        self.target.textChanged.connect(
            lambda: self.region_value("target", self.target.toPlainText())
        )
        self.source_lock = QCheckBox("锁定原文")
        self.target_lock = QCheckBox("锁定译文")
        form.addRow(self.source_lock, self.target_lock)
        self.source_lock.toggled.connect(
            lambda v: self.region_value("source_locked", v)
        )
        self.target_lock.toggled.connect(
            lambda v: self.region_value("target_locked", v)
        )
        self.geometry_lock = QCheckBox("锁定区域")
        form.addRow(self.geometry_lock)
        self.geometry_lock.toggled.connect(
            lambda v: self.region_value("geometry_locked", v)
        )
        self.font = QComboBox()
        self.font.addItem("内置字体", "")
        form.addRow("字体", self.font)
        self.font.currentIndexChanged.connect(
            lambda: self.style_value("font", self.font.currentData() or "")
        )
        self.button(form, "添加字体文件", self.pick_font)
        self.size = QSpinBox()
        self.size.setRange(6, 512)
        self.size.setValue(28)
        form.addRow("字号", self.size)
        self.size.valueChanged.connect(lambda v: self.style_value("size", v))
        self.minimum = QSpinBox()
        self.minimum.setRange(6, 512)
        self.minimum.setValue(10)
        form.addRow("最小字号", self.minimum)
        self.minimum.valueChanged.connect(lambda v: self.style_value("min_size", v))
        self.autofit = QCheckBox("自动适应文字框")
        self.autofit.setChecked(True)
        form.addRow(self.autofit)
        self.autofit.toggled.connect(lambda v: self.style_value("autofit", v))
        self.vertical = QCheckBox("竖排")
        self.bold = QCheckBox("粗体")
        form.addRow(self.vertical, self.bold)
        self.vertical.toggled.connect(lambda v: self.style_value("vertical", v))
        self.bold.toggled.connect(lambda v: self.style_value("bold", v))
        self.spacing = QDoubleSpinBox()
        self.spacing.setRange(0.5, 4)
        self.spacing.setValue(1.2)
        self.spacing.setSingleStep(0.1)
        form.addRow("行距", self.spacing)
        self.spacing.valueChanged.connect(lambda v: self.style_value("line_spacing", v))
        self.charspace = QSpinBox()
        self.charspace.setRange(-10, 50)
        form.addRow("字距", self.charspace)
        self.charspace.valueChanged.connect(
            lambda v: self.style_value("char_spacing", v)
        )
        self.stroke = QSpinBox()
        self.stroke.setRange(0, 30)
        form.addRow("描边宽度", self.stroke)
        self.stroke.valueChanged.connect(lambda v: self.style_value("stroke_width", v))
        colors = QHBoxLayout()
        self.button(colors, "文字颜色", lambda: self.pick_color("color"))
        self.button(colors, "描边颜色", lambda: self.pick_color("stroke_color"))
        form.addRow(colors)
        self.align = QComboBox()
        self.align.addItems(["left", "center", "right"])
        form.addRow("对齐", self.align)
        self.align.currentTextChanged.connect(lambda v: self.style_value("align", v))
        self.rotation = QSpinBox()
        self.rotation.setRange(-180, 180)
        form.addRow("旋转角度", self.rotation)
        self.rotation.valueChanged.connect(self.rotate)
        self.removal = QComboBox()
        for title, value in [
            ("色块遮挡", "fill"),
            ("精准去字（LaMa）", "precise"),
            ("保留原文", "none"),
        ]:
            self.removal.addItem(title, value)
        self.removal.hide()
        self.removal.currentIndexChanged.connect(
            lambda: self.region_value("removal", self.removal.currentData())
        )
        self.button(form, "指定遮挡颜色", lambda: self.pick_color("fill_color", False))
        self.button(
            form, "自动取背景色", lambda: self.region_value("fill_color", "auto")
        )
        self.button(form, "样式应用到本页", lambda: self.apply_style(False))
        self.button(form, "样式应用到全部页", lambda: self.apply_style(True))
        # QFormLayout creates plain QLabel objects from strings. Replace those
        # labels so they participate in Fluent's light/dark text styling too.
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.LabelRole)
            label = item.widget() if item else None
            if isinstance(label, NativeLabel) and not isinstance(label, QLabel):
                replacement = QLabel(label.text(), right)
                form.removeWidget(label)
                label.hide()
                label.deleteLater()
                form.setWidget(row, QFormLayout.LabelRole, replacement)
        split.setSizes([160, 800, 310])
        self.status = QLabel("导入图片后自动检测与OCR，确认后点击“翻译并生成”。")
        self.status.setWordWrap(True)
        self.status.setContentsMargins(24, 6, 24, 8)
        outer.addWidget(self.status)

    @staticmethod
    def button(layout, label, action):
        if label in {"导入图片", "翻译并生成"}:
            button = PrimaryPushButton(label)
        elif isinstance(layout, QFormLayout):
            button = QPushButton(label)
        else:
            button = TransparentPushButton(label)
        button.setMinimumHeight(30)
        button.clicked.connect(action)
        if isinstance(layout, QFormLayout):
            layout.addRow(button)
        else:
            layout.addWidget(button)
        return button

    def background(self, action, callback=lambda r: None):
        task = Background(action)
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            self.tasks.discard(task)
            if error:
                self.status.setText(error)
            else:
                callback(result)

        task.signals.done.connect(done)
        self.pool.start(task)

    def refresh_history(self):
        def loaded(result):
            self.history.clear()
            for doc in result["workspaces"]:
                self.history.addItem(f"{doc['title']} ({doc['pages']}页)", doc["id"])
            self.start_page.set_history(
                [
                    {
                        "workspace_id": d["id"],
                        "project_name": d["title"],
                        "file_count": d["pages"],
                    }
                    for d in result["workspaces"]
                ]
            )

        self.background(lambda: self.client.request("GET"), loaded)

    def refresh_providers(self):
        def loaded(result):
            self.provider.blockSignals(True)
            selected = (self.document or {}).get("settings", {}).get("provider_id")
            self.provider.clear()
            self.provider.addItem("使用文本翻译当前接口", None)
            for key, value in result.get("config", {}).get("platforms", {}).items():
                self.provider.addItem(value.get("name", key), key)
            self.provider.setCurrentIndex(max(0, self.provider.findData(selected)))
            self.provider.blockSignals(False)

        self.background(
            lambda: self.client.backend.request("GET", "/api/config"), loaded
        )

    def open_history(self):
        self.open_workspace(self.history.currentData())

    def open_workspace(self, identifier):
        if self.dirty or self.saving:
            self.save(lambda: self.open_workspace(identifier))
            return
        if identifier:
            self.detach_job()
            session = self.session
            self.background(
                lambda: self.client.request("GET", "/" + identifier),
                lambda r: (
                    self.loaded(r["workspace"]) if session == self.session else None
                ),
            )

    def back_to_start(self):
        if self.dirty or self.saving:
            self.save(self.back_to_start)
            return
        self.detach_job()
        self.workspace_stack.setCurrentWidget(self.start_page)
        self.refresh_history()

    def loaded(self, document):
        self.session += 1
        self.variant.blockSignals(True)
        self.variant.setCurrentIndex(0)
        self.variant.blockSignals(False)
        self.workspace_stack.setCurrentWidget(self.work_page)
        self.save_timer.stop()
        self.dirty = False
        self.image_data = None
        self.document = document
        self.saved_snapshot = deepcopy(document)
        self.selected = None
        self.undo.clear()
        self.page_id = document["pages"][0]["id"] if document["pages"] else None
        self.refresh_pages()
        self.populate_settings()
        self.load_image()
        self.status.setText("工程已加载。检测和 OCR 完成后请先校对，再翻译。")

    def refresh_pages(self):
        self.pages.blockSignals(True)
        self.pages.clear()
        for i, page in enumerate(self.document["pages"]):
            self.pages.addItem(f"{i + 1}. {page['name']} ({len(page['regions'])}框)")
        index = next(
            (
                i
                for i, p in enumerate(self.document["pages"])
                if p["id"] == self.page_id
            ),
            0,
        )
        self.page_id = (
            self.document["pages"][index]["id"] if self.document["pages"] else None
        )
        self.pages.setCurrentRow(index)
        self.pages.blockSignals(False)
        self.refresh_regions()

    @form_update
    def populate_settings(self):
        self.loading_form = True
        s = self.document["settings"]
        self.precise.setChecked(s.get("removal") == "precise")
        self.detector.setCurrentText(s["detector"])
        self.ocr.setCurrentText(s["ocr"])
        self.panels.setChecked(s["detect_panels"])
        self.source_language.setCurrentText(s["source_language"])
        self.target_language.setCurrentText(s["target_language"])
        self.confidence.setValue(s["confidence"])
        self.rtl.setChecked(s["right_to_left"])
        self.auto_recognize.setChecked(s["auto_recognize"])
        if s["provider_id"] and self.provider.findData(s["provider_id"]) < 0:
            self.provider.addItem(s["provider_id"], s["provider_id"])
        self.provider.setCurrentIndex(max(0, self.provider.findData(s["provider_id"])))
        self.loading_form = False

    def page(self):
        return next(
            (
                p
                for p in (self.document or {}).get("pages", [])
                if p["id"] == self.page_id
            ),
            None,
        )

    def region(self):
        return next(
            (
                r
                for r in (self.page() or {}).get("regions", [])
                if r["id"] == self.selected
            ),
            None,
        )

    def select_page(self, index):
        if not self.document or index < 0:
            return
        self.follow_progress = False
        self.page_id = self.document["pages"][index]["id"]
        self.image_data = None
        self.canvas.scene().clear()
        self.selected = None
        self.refresh_regions()
        self.load_image()

    def refresh_regions(self):
        self.regions.blockSignals(True)
        try:
            regions = sorted(
                (self.page() or {}).get("regions", []), key=lambda r: r["order"]
            )
            self.regions.setRowCount(len(regions))
            for row, r in enumerate(regions):
                state = (
                    "已翻译" if r["target"] else "待翻译" if r["source"] else "待识别"
                )
                for col, text in enumerate(
                    [str(row + 1), r["source"], r["target"], state]
                ):
                    item = QTableWidgetItem(text)
                    if col in {0, 3}:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setToolTip(text)
                    self.regions.setItem(row, col, item)
                self.regions.setRowHeight(row, 64)
                if r["id"] == self.selected:
                    self.regions.setCurrentCell(row, 1)
        finally:
            self.regions.blockSignals(False)

    def review_changed(self, row, column):
        if self.loading_form or column not in {1, 2}:
            return
        regions = sorted(
            (self.page() or {}).get("regions", []), key=lambda r: r["order"]
        )
        if 0 <= row < len(regions):
            self.selected = regions[row]["id"]
            self.region_value(
                "source" if column == 1 else "target",
                self.regions.item(row, column).text(),
            )

    def region_row(self, index):
        regions = sorted(
            (self.page() or {}).get("regions", []), key=lambda r: r["order"]
        )
        if 0 <= index < len(regions):
            self.select_region(regions[index]["id"])
            self.draw_canvas()

    @form_update
    def select_region(self, identifier):
        self.selected = identifier
        r = self.region()
        if not r:
            return
        self.loading_form = True
        if self.source.toPlainText() != r["source"]:
            self.source.setPlainText(r["source"])
        if self.target.toPlainText() != r["target"]:
            self.target.setPlainText(r["target"])
        s = r["style"]
        self.source_lock.setChecked(r["source_locked"])
        self.target_lock.setChecked(r["target_locked"])
        self.geometry_lock.setChecked(r["geometry_locked"])
        for widget, key in [
            (self.size, "size"),
            (self.minimum, "min_size"),
            (self.spacing, "line_spacing"),
            (self.charspace, "char_spacing"),
            (self.stroke, "stroke_width"),
        ]:
            widget.setValue(s[key])
        self.vertical.setChecked(s["vertical"])
        self.bold.setChecked(s["bold"])
        self.autofit.setChecked(s["autofit"])
        self.align.setCurrentText(s["align"])
        if s["font"] and self.font.findData(s["font"]) < 0:
            self.font.addItem(Path(s["font"]).name, s["font"])
        self.font.setCurrentIndex(max(0, self.font.findData(s["font"])))
        self.removal.setCurrentIndex(self.removal.findData(r["removal"]))
        self.rotation.setValue(r["layout_rect"]["rotation"])
        self.loading_form = False

    def draw_canvas(self):
        if self.image_data and self.page():
            selected = self.canvas.selected_ids()
            if self.selected not in selected:
                selected = [self.selected] if self.selected else []
            self.canvas.blockSignals(True)
            data = self.image_data
            if (
                self.image_is_source
                and self.variant.currentData() == "render"
                and self.document["settings"].get("removal") != "precise"
            ):
                data = self.fill_preview(data)
            self.canvas.display(data, self.page(), selected)
            self.canvas.blockSignals(False)

    def load_image(self):
        if not self.document or not self.page():
            return
        doc, page, variant = (
            self.document["id"],
            self.page_id,
            self.variant.currentData(),
        )
        ticket = (self.session, doc, page, variant, self.document["revision"])
        self.image_ticket = ticket

        def loaded(data):
            if self.image_ticket != ticket:
                return
            fit = self.image_data is None
            self.image_data = data
            self.draw_canvas()
            if fit:
                self.canvas.fitInView(self.canvas.sceneRect(), Qt.KeepAspectRatio)

        def fetch():
            if variant != "source":
                response = self.client.backend.http.get(
                    f"/api/v2/manga/{doc}/pages/{page}/{variant}", timeout=60
                )
                if response.status_code == 404:
                    return self.client.asset(doc, page, "source"), True
                response.raise_for_status()
                return response.content, False
            return self.client.asset(doc, page, "source"), False

        def ready(result):
            if self.image_ticket != ticket:
                return
            data, pending = result
            self.image_is_source = pending or variant == "source"
            loaded(data)
            if pending and self.document["settings"].get("removal") == "precise":
                self.status.setText(
                    "当前页显示原图与文字框，精准预览尚未生成；可点击排版预览。"
                )

        self.background(fetch, ready)

    def change_tool(self, index):
        self.canvas.mode = ["select", "add", "mask", "eraser"][index]

    def change_geometry(self):
        self.canvas.rect_key = self.geometry.currentData()
        self.draw_canvas()

    def edit(self, label, action):
        if not self.document or self.loading_form:
            return
        before = deepcopy(self.document)
        action()
        after = deepcopy(self.document)
        for page in after["pages"]:
            for order, region in enumerate(
                sorted(page["regions"], key=lambda r: r["order"])
            ):
                region["order"] = order
        if before != after:
            self.document = before
            self.undo.push(EditCommand(self, before, after, label))

    def apply_edit(self, document):
        revision = self.document["revision"]
        self.document = deepcopy(document)
        self.document["revision"] = revision
        old_page = self.page_id
        self.epoch += 1
        self.dirty = True
        self.refresh_pages()
        self.populate_settings()
        self.select_region(self.selected)
        self.draw_canvas()
        self.save_timer.start()
        self.status.setText("有修改，正在自动保存…")
        self.preview_pending = any(
            r.get("target") for r in (self.page() or {}).get("regions", [])
        )
        if old_page != self.page_id:
            self.image_data = None
            self.load_image()

    def set_setting(self, key, value):
        self.edit(
            "修改处理设置", lambda: self.document["settings"].__setitem__(key, value)
        )

    def region_value(self, key, value):
        if self.region():
            self.edit("修改文字框", lambda: self.region().__setitem__(key, value))
            self.preview_pending = True

    def style_value(self, key, value):
        if self.region():
            self.edit(
                "修改排版", lambda: self.region()["style"].__setitem__(key, value)
            )
            self.preview_pending = True

    def geometry_changed(self, identifier, key, rect):
        if not any(
            r["id"] == identifier for r in (self.page() or {}).get("regions", [])
        ):
            return
        self.selected = identifier
        self.edit("调整区域", lambda: self.region().__setitem__(key, rect))
        self.preview_pending = True

    def rotate(self, value):
        if self.region():
            self.edit(
                "旋转译文",
                lambda: self.region()["layout_rect"].__setitem__("rotation", value),
            )
            self.preview_pending = True

    def add_region(self, rect):
        if not self.page():
            return
        region = {
            "id": uuid4().hex,
            "ocr_rect": dict(rect),
            "erase_rect": dict(rect),
            "layout_rect": dict(rect),
            "source": "",
            "target": "",
            "source_locked": False,
            "target_locked": False,
            "geometry_locked": False,
            "order": len(self.page()["regions"]),
            "panel_id": None,
            "confidence": None,
            "removal": self.document["settings"]["removal"],
            "fill_color": "auto",
            "fill_opacity": 1,
            "mask_strokes": [],
            "warnings": [],
            "style": {
                "font": "",
                "size": 28,
                "min_size": 10,
                "autofit": True,
                "vertical": False,
                "bold": False,
                "color": "#000000",
                "stroke_color": "#ffffff",
                "stroke_width": 0,
                "line_spacing": 1.2,
                "char_spacing": 0,
                "padding": 4,
                "align": "center",
            },
        }
        self.selected = region["id"]
        self.edit("建立文字框", lambda: self.page()["regions"].append(region))
        self.select_region(region["id"])

    def delete_regions(self):
        ids = set(
            self.canvas.selected_ids() or ([self.selected] if self.selected else [])
        )
        if ids:
            self.edit(
                "删除文字框",
                lambda: self.page().__setitem__(
                    "regions", [r for r in self.page()["regions"] if r["id"] not in ids]
                ),
            )

    def add_stroke(self, stroke):
        if not self.region():
            self.status.setText("请先选中一个文字框再编辑蒙版")
            return

        def action():
            self.region()["mask_strokes"].append(stroke)
            self.document["settings"]["removal"] = "precise"

        self.edit("编辑文字蒙版", action)
        self.preview_pending = True

    def move_region(self, direction):
        if not self.region():
            return
        values = sorted(self.page()["regions"], key=lambda r: r["order"])
        i = next(i for i, r in enumerate(values) if r["id"] == self.selected)
        j = i + direction
        if 0 <= j < len(values):

            def action():
                values[i], values[j] = values[j], values[i]
                for k, r in enumerate(values):
                    r["order"] = k

            self.edit("调整阅读顺序", action)

    def merge_regions(self):
        ids = set(self.canvas.selected_ids())
        values = (
            [r for r in self.page()["regions"] if r["id"] in ids] if self.page() else []
        )
        if len(values) < 2:
            return

        def action():
            first = min(values, key=lambda r: r["order"])
            for key in ("ocr_rect", "erase_rect", "layout_rect"):
                x = min(r[key]["x"] for r in values)
                y = min(r[key]["y"] for r in values)
                first[key] = {
                    "x": x,
                    "y": y,
                    "width": max(r[key]["x"] + r[key]["width"] for r in values) - x,
                    "height": max(r[key]["y"] + r[key]["height"] for r in values) - y,
                    "rotation": 0,
                }
            first["source"] = "\n".join(
                r["source"] for r in sorted(values, key=lambda r: r["order"])
            )
            first["target"] = "\n".join(
                r["target"] for r in sorted(values, key=lambda r: r["order"])
            )
            self.page()["regions"] = [
                r for r in self.page()["regions"] if r["id"] not in ids or r is first
            ]
            self.selected = first["id"]

        self.edit("合并文字框", action)

    def split_region(self):
        if not self.region():
            return

        def action():
            region = self.region()
            new = deepcopy(region)
            new["id"] = uuid4().hex
            new["source"] = new["target"] = ""
            new["mask_strokes"] = []
            for key in ("ocr_rect", "erase_rect", "layout_rect"):
                region[key]["height"] /= 2
                new[key]["height"] = region[key]["height"]
                new[key]["y"] += new[key]["height"]
            new["order"] = len(self.page()["regions"])
            self.page()["regions"].append(new)

        self.edit("拆分文字框", action)

    def move_page(self, direction):
        if not self.page():
            return
        i = self.document["pages"].index(self.page())
        j = i + direction
        if 0 <= j < len(self.document["pages"]):

            def action():
                self.document["pages"][i], self.document["pages"][j] = (
                    self.document["pages"][j],
                    self.document["pages"][i],
                )

            self.edit("调整页面顺序", action)

    def delete_page(self):
        if self.page():
            self.edit(
                "移除页面",
                lambda: self.document.__setitem__(
                    "pages",
                    [p for p in self.document["pages"] if p["id"] != self.page_id],
                ),
            )

    def pick_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择字体", "", "字体 (*.ttf *.ttc *.otf)"
        )
        if path:
            self.font.addItem(Path(path).name, path)
            self.font.setCurrentIndex(self.font.count() - 1)

    def pick_color(self, key, style=True):
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self.style_value(key, color.name()) if style else self.region_value(
                key, color.name()
            )

    def apply_style(self, all_pages):
        if not self.region():
            return
        style = deepcopy(self.region()["style"])

        def action():
            for page in self.document["pages"] if all_pages else [self.page()]:
                for r in page["regions"]:
                    r["style"] = deepcopy(style)

        self.edit("批量应用样式", action)

    def save(self, callback=None):
        if not self.document:
            return
        if callable(callback):
            self.save_callbacks.append(callback)
        if self.saving:
            self.save_timer.start()
            return
        if not self.dirty:
            self.status.setText("已保存，没有新的修改")
            callbacks, self.save_callbacks = self.save_callbacks, []
            for action in callbacks:
                action()
            return
        self.save_timer.stop()
        self.saving = True
        self.status.setText("正在保存…")
        session = self.session
        base = deepcopy(self.document)
        epoch = self.epoch

        def saved(result):
            self.saving = False
            self.saved_snapshot = deepcopy(result["workspace"])
            self.document = merge_edits(base, self.document, result["workspace"])
            self.status.setText("已自动保存")
            if self.epoch != epoch:
                self.save_timer.start()
                return
            self.dirty = False
            callbacks, self.save_callbacks = self.save_callbacks, []
            for action in callbacks:
                action()
            if (
                not callbacks
                and self.preview_pending
                and not self.job_id
                and not self.submitting
            ):
                self.preview_pending = False
                self.run("render", saved=True, preview=True)

        task = Background(lambda: self.client.save(base))
        self.tasks.add(task)

        def complete(result, error):
            if sip.isdeleted(self):
                return
            self.tasks.discard(task)
            if session != self.session:
                return
            if error:
                self.saving = False
                self.save_callbacks = []
                self.status.setText("保存未完成，修改仍保留在编辑器：" + error)
                if "(409)" in error:
                    self.recover_workspace(self.saved_snapshot)
            else:
                saved(result)

        task.signals.done.connect(complete)
        self.pool.start(task)

    def import_images(self, checked=False, folder=False):
        if self.job_id or self.submitting or not self.prepare_to_leave():
            return
        if folder:
            directory = QFileDialog.getExistingDirectory(
                self, "选择漫画图片目录（含子目录）"
            )
            paths = [directory] if directory else []
        else:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "选择漫画图片",
                "",
                "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
            )
        if not paths:
            return
        self.import_paths(paths)

    def import_paths(self, paths):
        if not paths or self.job_id or self.submitting or not self.prepare_to_leave():
            return
        append = (
            self.document is not None
            and QMessageBox.question(
                self,
                "导入图片",
                "追加到当前工程？选择“否”将新建工程。",
                QMessageBox.Yes | QMessageBox.No,
            )
            == QMessageBox.Yes
        )
        payload = {
            "paths": paths,
            "settings": self.document["settings"]
            if self.document
            else {
                "detector": self.detector.currentText(),
                "ocr": self.ocr.currentText(),
                "source_language": self.source_language.currentText(),
                "target_language": self.target_language.currentText(),
                "confidence": self.confidence.value(),
                "right_to_left": self.rtl.isChecked(),
                "detect_panels": self.panels.isChecked(),
                "auto_recognize": self.auto_recognize.isChecked(),
            },
        }
        if append:
            payload["workspace_id"] = self.document["id"]

        def imported(result):
            self.loaded(result["workspace"])
            self.refresh_history()
            if self.document["settings"].get("auto_recognize"):
                self.follow_progress = True
                self.run("recognize")

        self.start_request(
            lambda: self.client.request(
                "POST", "/import", json=payload, headers={"Prefer": "respond-async"}
            ),
            imported,
        )

    def run(self, stage, saved=False, preview=False):
        if not self.document or self.job_id or self.submitting:
            return
        if not saved:
            self.save(lambda: self.run(stage, True, preview))
            return
        scope = 0 if preview else self.scope.currentIndex()
        pages = [] if scope == 1 or stage == "recognize" else [self.page_id]
        regions = self.canvas.selected_ids() if scope == 2 else []
        if scope == 2 and not regions:
            self.status.setText("请先选中文字框")
            return
        replace = self.overwrite.isChecked()
        if (
            stage == "detect"
            and replace
            and QMessageBox.question(
                self, "替换检测框", "重新检测将替换当前范围的文字框，是否继续？"
            )
            != QMessageBox.Yes
        ):
            return
        base = deepcopy(self.document)

        session = self.session

        def completed(result):
            if session != self.session:
                return
            self.saved_snapshot = deepcopy(result["workspace"])
            self.document = merge_edits(base, self.document, result["workspace"])
            self.refresh_pages()
            self.select_region(self.selected)
            if stage in {"render", "clean", "pipeline"}:
                self.variant.blockSignals(True)
                self.variant.setCurrentIndex(
                    self.variant.findData("clean" if stage == "clean" else "render")
                )
                self.variant.blockSignals(False)
            self.load_image()
            self.show_report(result)
            if self.dirty:
                self.save_timer.start()

        self.start_request(
            lambda: self.client.run(
                base,
                stage,
                pages,
                regions,
                {
                    "replace_regions": replace,
                    "overwrite_source": replace,
                    "retranslate": replace,
                },
            ),
            completed,
        )

    def start_request(self, action, callback):
        if self.job_id or self.submitting:
            self.status.setText("请等待当前任务结束，或先取消")
            return
        self.submitting = True
        self.job_base = deepcopy(self.document)
        session = self.session
        self.status.setText("正在提交任务…")

        def started(result):
            if session != self.session:
                return
            self.submitting = False
            job = result.get("job", {})
            self.job_id = job.get("id") or job.get("job_id")
            self.job_callback = callback
            if self.job_id:
                self.poll_timer.start()
            else:
                callback(result)

        task = Background(action)
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            self.tasks.discard(task)
            self.submitting = False
            if error:
                self.status.setText(error)
            else:
                started(result)

        task.signals.done.connect(done)
        self.pool.start(task)

    def poll_job(self):
        if self.polling or not self.job_id:
            return
        self.polling = True
        identifier = self.job_id

        def loaded(result):
            self.polling = False
            if identifier != self.job_id:
                return
            job = result["job"]
            state = {
                "queued": "排队中",
                "running": "正在处理",
                "succeeded": "已完成",
                "partial": "部分完成",
                "failed": "处理失败",
                "cancelled": "已取消",
                "interrupted": "任务已中断",
            }.get(job["state"], job["state"])
            progress = job.get("progress") or {}
            if job["state"] == "running" and self.document:
                self.sync_progress(progress)
            detail = ""
            if "downloaded_bytes" in progress:
                detail = f"下载 {progress['downloaded_bytes'] / 1024**2:.1f} MB"
            elif "current" in progress:
                label = {
                    "detect": "检测",
                    "ocr": "识别",
                    "translate": "翻译",
                    "clean": "去字",
                    "render": "排版",
                }.get(progress.get("stage"), "")
                detail = (
                    f"{label} {progress['current']} / {progress.get('total', 0)} 页"
                )
            elif "line" in progress:
                detail = f"翻译 {progress['line']} / {progress.get('total_line', 0)} 条"
            self.status.setText(state + (" · " + detail if detail else ""))
            if job["state"] in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "interrupted",
            }:
                self.poll_timer.stop()
                self.job_id = None
                if (job.get("result") and "workspace" in job["result"]) or job[
                    "state"
                ] == "succeeded":
                    self.job_callback(job.get("result") or {})
                    if self.dirty:
                        self.save_timer.start()
                else:
                    self.status.setText(
                        str(job.get("error") or "任务已取消，可重新执行失败阶段")
                    )
                    if self.document:
                        self.recover_workspace(self.job_base)

        task = Background(
            lambda: self.client.backend.request("GET", "/api/jobs/" + identifier)
        )
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            self.tasks.discard(task)
            if error:
                self.polling = False
                self.status.setText(error)
            else:
                loaded(result)

        task.signals.done.connect(done)
        self.pool.start(task)

    def cancel_job(self):
        identifier = self.job_id
        if identifier:
            self.background(
                lambda: self.client.backend.request(
                    "POST", f"/api/jobs/{identifier}/cancel"
                ),
                lambda r: self.status.setText("正在取消，等待当前推理安全结束…"),
            )

    def recover_workspace(self, baseline=None):
        base = deepcopy(baseline or self.saved_snapshot)

        session = self.session

        def recovered(result):
            if (
                session != self.session
                or not self.document
                or self.document["id"] != base["id"]
            ):
                return
            # A failed job may have committed earlier pages. Keep human edits and
            # advance the revision before retrying any save.
            remote = result["workspace"]
            self.saved_snapshot = deepcopy(remote)
            if self.dirty:
                self.document = merge_edits(base, self.document, remote)
            else:
                self.document = remote
            self.refresh_pages()
            self.load_image()
            if self.dirty:
                self.save_timer.start()

        self.background(lambda: self.client.request("GET", "/" + base["id"]), recovered)

    def import_translation(self):
        if not self.document or self.job_id or self.submitting:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 i18next 译文", "", "JSON (*.json)"
        )
        if path:
            self.save(
                lambda: self.start_request(
                    lambda: self.client.request(
                        "POST",
                        f"/{self.document['id']}/translations",
                        json={"path": path},
                        headers={"Prefer": "respond-async"},
                    ),
                    lambda r: self.loaded(r["workspace"]),
                )
            )

    def import_project(self):
        if self.job_id or self.submitting or not self.prepare_to_leave():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开漫画工程包", "", "漫画工程 (*.manga.zip)"
        )
        if path:
            self.start_request(
                lambda: self.client.request(
                    "POST",
                    "/project/import",
                    json={"path": path},
                    headers={"Prefer": "respond-async"},
                ),
                lambda r: (self.loaded(r["workspace"]), self.refresh_history()),
            )

    def show_report(self, result):
        report = result.get("report", {})
        errors = report.get("errors", [])
        warnings = report.get("warnings", [])
        self.status.setText(
            "；".join(str(e.get("message", e)) for e in errors + warnings)
            or "处理完成，请校对后继续"
        )

    def export(self):
        if not self.document:
            return
        labels = ["PNG图片", "JPEG图片", "整批ZIP", "可编辑工程包", "i18next JSON"]
        label, ok = QInputDialog.getItem(self, "导出漫画", "格式", labels, 0, False)
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择新的导出目录")
        if folder:
            mode = ["png", "jpeg", "zip", "project", "i18next"][labels.index(label)]
            self.save(
                lambda: self.start_request(
                    lambda: self.client.request(
                        "POST",
                        f"/{self.document['id']}/export",
                        json={"output_dir": folder, "mode": mode},
                        headers={"Prefer": "respond-async"},
                    ),
                    lambda r: self.status.setText(
                        "已导出：" + ", ".join(r.get("files", []))
                    ),
                )
            )

    def fill_preview(self, data):
        image = QImage.fromData(data).convertToFormat(QImage.Format_ARGB32)
        original = image.copy()
        painter = QPainter(image)
        for region in self.page()["regions"]:
            if not region.get("target", "").strip():
                continue
            r = region["erase_rect"]
            color = region.get("fill_color", "auto")
            if color == "auto":
                x, y = max(0, int(r["x"]) - 1), max(0, int(r["y"]) - 1)
                color = original.pixelColor(x, y)
            else:
                color = QColor(color)
            painter.fillRect(QRectF(r["x"], r["y"], r["width"], r["height"]), color)
        for region in self.page()["regions"]:
            if not region.get("target", "").strip():
                continue
            r, style = region["layout_rect"], region["style"]
            painter.save()
            painter.translate(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
            painter.rotate(r.get("rotation", 0))
            font = QFont()
            font.setPixelSize(style["size"])
            font.setBold(style["bold"])
            painter.setFont(font)
            painter.setPen(QColor(style["color"]))
            painter.drawText(
                QRectF(-r["width"] / 2, -r["height"] / 2, r["width"], r["height"]),
                Qt.AlignCenter | Qt.TextWordWrap,
                region["target"],
            )
            painter.restore()
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    def toggle_precise(self, checked):
        if self.loading_form or not self.document:
            return
        self.set_setting("removal", "precise" if checked else "fill")
        if self.variant.currentData() in {"render", "clean"}:
            self.variant.setCurrentIndex(self.variant.findData("render"))
            self.load_image()
        self.preview_pending = True
        self.status.setText("已启用精准去字" if checked else "已切回色块遮挡")

    def resume_follow(self):
        self.follow_progress = True
        self.status.setText("正在跟随处理页面；手动切页可暂停跟随")

    def sync_progress(self, progress):
        if self.syncing or not self.document:
            return
        self.syncing = True
        session, workspace_id = self.session, self.document["id"]

        def sync():
            try:
                return self.client.request("GET", "/" + workspace_id)
            except Exception:
                return None

        def ready(result):
            self.syncing = False
            if session != self.session or not result:
                return
            remote = result["workspace"]
            if remote["revision"] == self.saved_snapshot["revision"]:
                return
            self.document = merge_edits(self.saved_snapshot, self.document, remote)
            self.saved_snapshot = deepcopy(remote)
            if (
                self.follow_progress
                and progress.get("page_id")
                and self.page_id != progress["page_id"]
            ):
                self.page_id = progress["page_id"]
                self.image_data = None
            self.refresh_pages()
            self.select_region(self.selected)
            self.load_image()

        self.background(sync, ready)

    def detach_job(self):
        self.session += 1
        self.poll_timer.stop()
        self.job_id = None
        self.submitting = False
        self.polling = False
        self.syncing = False
        self.preview_pending = False
        self.save_callbacks = []

    def delete_workspace(self, workspace_id, title):
        if (
            QMessageBox.question(
                self,
                "删除漫画工程",
                f"永久删除“{title}”的工程、识别结果和缓存？原始图片及导出文件会保留。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        if self.job_id and self.document and self.document["id"] == workspace_id:
            self.cancel_job()
            self.detach_job()

        def deleted(_):
            if self.document and self.document["id"] == workspace_id:
                self.document = None
                self.image_data = None
                self.dirty = False
                self.undo.clear()
                self.canvas.scene().clear()
                self.workspace_stack.setCurrentWidget(self.start_page)
            self.refresh_history()
            self.status.setText("工程已永久删除，原始图片和导出文件保留")

        self.status.setText("正在删除工程，等待当前推理安全结束…")
        self.background(
            lambda: self.client.request("DELETE", "/" + workspace_id, timeout=600),
            deleted,
        )

    def export_translation(self, source):
        if not self.document:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "选择原文导出目录" if source else "选择译文导出目录"
        )
        if folder:
            mode = "i18next-source" if source else "i18next-target"
            self.save(
                lambda: self.start_request(
                    lambda: self.client.request(
                        "POST",
                        f"/{self.document['id']}/export",
                        json={"output_dir": folder, "mode": mode},
                        headers={"Prefer": "respond-async"},
                    ),
                    lambda r: self.status.setText(
                        "已导出：" + ", ".join(r.get("files", []))
                    ),
                )
            )

    def prepare_to_leave(self):
        if self.saving or self.dirty:
            self.save()
            self.status.setText("正在保存，请稍后再切换页面")
            return False
        return True

    def prepare_to_close(self):
        if self.job_id or self.submitting:
            self.status.setText("请先取消漫画任务，待当前推理结束后再关闭")
            return False
        return self.prepare_to_leave()

    def cleanup(self):
        self.save_timer.stop()
        self.poll_timer.stop()
