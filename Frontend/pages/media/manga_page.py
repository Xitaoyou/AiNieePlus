"""Native editable manga workspace. Network and inference never run on the GUI thread."""

from copy import deepcopy
from functools import wraps
from pathlib import Path
from uuid import uuid4

from PyQt5 import sip
from PyQt5.QtCore import (
    QEvent,
    QObject,
    QRect,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import (
    QColor,
    QDesktopServices,
    QKeySequence,
    QPalette,
)
from PyQt5.QtWidgets import (
    QColorDialog,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QProgressBar,
    QShortcut,
    QSizePolicy,
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
    Action,
    DropDownPushButton,
    FluentIcon,
    PrimaryPushButton,
    RoundMenu,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
    qconfig,
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
from Frontend.services.manga_edits import changes, value_at
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
    done = pyqtSignal(object, object, object)


class Background(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.setAutoDelete(False)
        self.action, self.signals = action, Signals()

    def run(self):
        try:
            self.signals.done.emit(self, self.action(), None)
        except Exception as error:
            self.signals.done.emit(self, None, str(error))
        finally:
            self.action = None


class EditCommand(QUndoCommand):
    def __init__(self, page, before, after, label, already_applied=False, edits=None):
        super().__init__(label)
        self.page = page
        self.edits = changes(before, after) if edits is None else edits
        self.already_applied = already_applied

    def redo(self):
        if self.already_applied:
            self.already_applied = False
            return
        self.page.note_manual_edits(self.edits)
        apply_changes(self.page.document, self.edits)
        self.page.apply_edit(self.page.document)

    def undo(self):
        self.page.note_manual_edits(self.edits)
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
        self.clean_signature = None
        self.epoch = 0
        self.saving = False
        self.job_id = None
        self.job_manual_paths = set()
        self.loading_form = False
        self.preview_pending = False
        self.session = 0
        self.follow_progress = True
        self.syncing = False
        self.undo = QUndoStack(self)
        self.undo.setUndoLimit(100)
        self.pool = QThreadPool.globalInstance()
        self.tasks = set()
        self.task_callbacks = {}
        self.closed = False
        self.last_callback_error = None
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
        commands = QHBoxLayout(command_card)
        commands.setContentsMargins(10, 8, 10, 8)
        commands.setSpacing(6)
        root.addWidget(command_card)
        self.history = QComboBox()
        self.history.hide()
        self.scope = QComboBox()
        self.scope.addItems(["当前页", "全部页面", "选中文字框"])
        self.scope.setCurrentIndex(1)
        self.scope.hide()
        self.menu_button(
            commands,
            "文件",
            [
                ("导入图片…", self.import_images),
                ("导入文件夹…", lambda: self.import_images(folder=True)),
                ("打开工程包…", self.import_project),
                ("保存工程  Ctrl+S", self.save),
                ("导出图片 / 工程包…", self.export),
                ("导出原文 JSON…", lambda: self.export_translation(True)),
                ("导出译文 JSON…", lambda: self.export_translation(False)),
                ("导入译文 JSON…", self.import_translation),
            ],
        )
        self.menu_button(
            commands,
            "识别",
            [
                ("检测并 OCR · 当前页", lambda: self.run("recognize", scope=0)),
                ("检测并 OCR · 整部漫画", lambda: self.run("recognize", scope=1)),
                ("检测文字框 · 当前页", lambda: self.run("detect", scope=0)),
                ("检测文字框 · 整部漫画", lambda: self.run("detect", scope=1)),
                ("OCR · 选中的框", lambda: self.run("ocr", scope=2)),
                ("OCR · 当前页所有框", lambda: self.run("ocr", scope=0)),
                ("OCR · 整部所有框", lambda: self.run("ocr", scope=1)),
            ],
        )
        self.menu_button(
            commands,
            "翻译",
            [
                ("翻译 · 选中的框", lambda: self.run("translate", scope=2)),
                ("翻译 · 当前页", lambda: self.run("translate", scope=0)),
                ("翻译 · 整部漫画", lambda: self.run("translate", scope=1)),
                ("查看进度与失败明细", self.show_task_details),
                ("重试失败文字框", self.retry_failed),
            ],
        )
        self.menu_button(
            commands,
            "排版",
            [
                ("样式应用到当前页", lambda: self.apply_style(False)),
                ("样式应用到整部漫画", lambda: self.apply_style(True)),
                ("添加字体文件…", self.pick_font),
                ("重新生成当前页精准去字底图", lambda: self.run("render", scope=0)),
            ],
        )
        self.menu_button(
            commands,
            "更多",
            [
                ("模型中心…", lambda: ModelDialog(self).exec()),
                ("恢复跟随处理页面", self.resume_follow),
                ("高级：调整 OCR 取字范围", lambda: self.geometry.setCurrentIndex(1)),
                ("高级：调整去字范围", lambda: self.geometry.setCurrentIndex(2)),
                ("回到译文排版框", lambda: self.geometry.setCurrentIndex(0)),
            ],
        )
        self.icon_button(commands, FluentIcon.SAVE, "保存工程 (Ctrl+S)", self.save)
        self.undo_button = self.icon_button(
            commands, FluentIcon.LEFT_ARROW, "撤销 (Ctrl+Z)", self.undo.undo
        )
        self.redo_button = self.icon_button(
            commands, FluentIcon.RIGHT_ARROW, "重做 (Ctrl+Y)", self.undo.redo
        )
        self.undo.canUndoChanged.connect(self.undo_button.setEnabled)
        self.undo.canRedoChanged.connect(self.redo_button.setEnabled)
        self.undo_button.setEnabled(False)
        self.redo_button.setEnabled(False)
        commands.addStretch(1)
        self.button(commands, "翻译整部", lambda: self.run("translate", scope=1))
        self.cancel_button = self.icon_button(
            commands, FluentIcon.CLOSE, "取消当前任务", self.cancel_job
        )
        self.precise = QCheckBox("精准去字")
        self.precise.setToolTip(
            "整个工程统一：勾选使用 LaMa 修复背景，取消使用色块遮挡；原图始终保留"
        )
        self.precise.toggled.connect(self.toggle_precise)
        self.overwrite = QCheckBox("重做已有识别 / 译文")
        self.overwrite.setToolTip(
            "通常关闭，保留人工校对；开启后可重做当前操作范围的结果"
        )
        self.variant = QComboBox()
        for title, value in [
            ("实时编辑译文", "render"),
            ("查看原图", "source"),
            ("查看去字底图", "clean"),
            ("查看蒙版", "mask"),
            ("查看文字层", "layer"),
        ]:
            self.variant.addItem(title, value)
        self.variant.setCurrentIndex(self.variant.findData("source"))
        self.variant.currentIndexChanged.connect(self.load_image)
        for sequence, callback in [
            ("Ctrl+S", self.save),
            ("Ctrl+Z", self.undo.undo),
            ("Ctrl+Y", self.undo.redo),
            ("Ctrl+Shift+Z", self.undo.redo),
        ]:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
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
        self.tool.hide()
        self.tool.currentIndexChanged.connect(self.change_tool)
        self.tool_buttons = []
        for index, icon, label in [
            (0, FluentIcon.MOVE, "选择 / 移动文字框 (V)"),
            (1, FluentIcon.ADD, "拖动建立文字框 (B)"),
            (2, FluentIcon.BRUSH, "增加去字蒙版"),
            (3, FluentIcon.ERASE_TOOL, "擦除蒙版 / 保护背景"),
        ]:
            button = self.icon_button(
                tools,
                icon,
                label,
                lambda checked=False, i=index: self.tool.setCurrentIndex(i),
            )
            button.setCheckable(True)
            button.setChecked(index == 0)
            self.tool_buttons.append(button)
        self.icon_button(
            tools,
            FluentIcon.DELETE,
            "删除选中文字框 (Delete，可撤销)",
            self.delete_regions,
        )
        self.icon_button(
            tools,
            FluentIcon.FIT_PAGE,
            "适应窗口",
            lambda: self.canvas.fitInView(self.canvas.sceneRect(), Qt.KeepAspectRatio),
        )
        self.geometry = QComboBox()
        for title, key in [
            ("译文排版框", "layout_rect"),
            ("OCR 取字范围", "ocr_rect"),
            ("去字背景范围", "erase_rect"),
        ]:
            self.geometry.addItem(title, key)
        self.geometry.hide()
        self.geometry.currentIndexChanged.connect(self.change_geometry)
        self.geometry_hint = QLabel(
            "拖框移动译文 · 拖右下角自动缩放文字 · 右键更多操作"
        )
        self.geometry_hint.setWordWrap(True)
        self.geometry_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tools.addWidget(self.geometry_hint, 1)
        tools.addStretch(1)
        tools.addWidget(self.variant)
        self.brush = QSpinBox()
        self.brush.setRange(1, 256)
        self.brush.setFixedWidth(120)
        self.brush.setToolTip("蒙版画笔半径")
        self.brush.setValue(8)
        self.brush.hide()
        self.brush.valueChanged.connect(lambda v: setattr(self.canvas, "radius", v))
        tools.addWidget(self.brush)
        self.canvas = MangaCanvas()
        center_layout.addWidget(self.canvas, 1)
        split.addWidget(center)
        self.canvas.regionSelected.connect(self.select_region)
        self.canvas.geometryChanged.connect(self.geometry_changed, Qt.QueuedConnection)
        self.canvas.boxCreated.connect(self.add_region)
        self.canvas.strokeCreated.connect(self.add_stroke)
        self.canvas.deleteRequested.connect(self.delete_regions)
        self.canvas.contextRequested.connect(self.region_context_menu)
        self.canvas.modeRequested.connect(self.tool.setCurrentIndex)
        scroll = QScrollArea()
        scroll.setMinimumWidth(350)
        scroll.setMaximumWidth(480)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea {border:none;background:transparent;}")
        right = SimpleCardWidget()
        right.setBorderRadius(12)
        form = QFormLayout(right)
        form.setContentsMargins(16, 16, 16, 16)
        form.setVerticalSpacing(12)
        form.addRow(StrongBodyLabel("处理设置", right))
        form.addRow(self.precise)
        form.addRow(self.overwrite)
        scroll.setWidget(right)
        right.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        right_split = QSplitter(Qt.Vertical)
        right_split.setMinimumWidth(350)
        right_split.setMaximumWidth(480)
        right_split.addWidget(scroll)
        split.addWidget(right_split)
        outer_form = form
        processing = QWidget(right)
        processing_form = QFormLayout(processing)
        processing_form.setContentsMargins(0, 0, 0, 0)
        processing_toggle = QPushButton("展开识别模型与语言设置")
        processing_toggle.setCheckable(True)
        processing_toggle.toggled.connect(processing.setVisible)
        processing_toggle.toggled.connect(
            lambda checked: processing_toggle.setText(
                "收起识别模型与语言设置" if checked else "展开识别模型与语言设置"
            )
        )
        form.addRow(processing_toggle)
        form.addRow(processing)
        processing.hide()
        form = processing_form
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
        self.regions.setColumnCount(3)
        self.regions.setHorizontalHeaderLabels(["顺序", "OCR 原文 / 译文", "状态"])
        self.regions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.regions.setMinimumHeight(150)
        self.regions.setWordWrap(True)
        self.regions.setSelectionBehavior(QTableWidget.SelectRows)
        self.regions.setColumnWidth(0, 60)
        self.regions.setColumnWidth(2, 80)
        self.regions.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.regions.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.regions.cellChanged.connect(self.review_changed)
        self.regions.currentCellChanged.connect(
            lambda row, col, old_row, old_col: self.region_row(row)
        )
        reorder = QHBoxLayout()
        self.button(reorder, "上移框", lambda: self.move_region(-1))
        self.button(reorder, "下移框", lambda: self.move_region(1))
        self.button(reorder, "合并框", self.merge_regions)
        self.button(reorder, "拆分框", self.split_region)

        form = outer_form
        form.addRow(StrongBodyLabel("文字与排版", right))
        self.source = QPlainTextEdit()
        self.source.setMaximumHeight(95)
        self.target = QPlainTextEdit()
        self.target.setMaximumHeight(95)
        form.addRow("OCR原文", self.source)
        form.addRow("译文", self.target)
        self.source.installEventFilter(self)
        self.target.installEventFilter(self)
        self.source.setUndoRedoEnabled(False)
        self.target.setUndoRedoEnabled(False)
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
        form.addRow("建议字号下限", self.minimum)
        self.minimum.valueChanged.connect(lambda v: self.style_value("min_size", v))
        self.autofit = QCheckBox("字体跟随框大小（字号为上限）")
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
        # Keep the most-used typography controls visible before the long form.
        for widget in (self.font, self.size, self.autofit):
            row, _ = form.getWidgetPosition(widget)
            removed = form.takeRow(row)
            if removed.labelItem and removed.labelItem.widget():
                removed.labelItem.widget().hide()
                removed.labelItem.widget().deleteLater()
        typography = QHBoxLayout()
        typography.addWidget(self.font, 1)
        self.size.setFixedWidth(130)
        self.size.setToolTip(
            "自动适应开启时，这是字号上限；缩小框时文字会自动缩小以完整显示"
        )
        typography.addWidget(self.size)
        row, _ = form.getWidgetPosition(self.source)
        form.insertRow(row, "字体 / 字号", typography)
        form.insertRow(row + 1, self.autofit)
        review = SimpleCardWidget()
        review_layout = QVBoxLayout(review)
        review_layout.addWidget(StrongBodyLabel("阅读顺序 · 原文 / 译文 / 状态"))
        self.regions.setMinimumHeight(180)
        self.regions.setColumnWidth(0, 40)
        self.regions.setColumnWidth(2, 80)
        review_layout.addWidget(self.regions)
        review_layout.addLayout(reorder)
        right_split.addWidget(review)
        right_split.setSizes([500, 260])
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
        split.setSizes([160, 900, 380])
        self.progress_label = QLabel("尚未开始翻译")
        self.progress_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_label, 2)
        progress_row.addWidget(self.progress_bar, 1)
        self.button(progress_row, "进度 / 失败明细", self.show_task_details)
        root.addLayout(progress_row)
        self.last_report = {}
        self.failed_region_ids = []
        self.status = QLabel("导入图片后自动检测与OCR，确认后点击“翻译并生成”。")
        self.status.setWordWrap(True)
        self.status.setContentsMargins(24, 6, 24, 8)
        outer.addWidget(self.status)

    def eventFilter(self, watched, event):
        if watched in (getattr(self, "source", None), getattr(self, "target", None)):
            if event.type() == QEvent.ShortcutOverride and (
                event.matches(QKeySequence.Undo) or event.matches(QKeySequence.Redo)
            ):
                event.ignore()
                return True
            if event.type() == QEvent.KeyPress:
                if event.matches(QKeySequence.Undo):
                    self.undo.undo()
                    return True
                if event.matches(QKeySequence.Redo):
                    self.undo.redo()
                    return True
        return super().eventFilter(watched, event)

    def menu_button(self, layout, label, entries):
        button = DropDownPushButton(label, self)
        menu = RoundMenu(parent=button)
        for title, callback in entries:
            menu.addAction(
                Action(title, triggered=lambda checked=False, action=callback: action())
            )
        button.setMenu(menu)
        layout.addWidget(button)
        return button

    def icon_button(self, layout, icon, label, action):
        button = TransparentToolButton(icon, self)
        button.setFixedSize(32, 32)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def region_context_menu(self, region_id, position):
        self.select_region(region_id)
        menu = RoundMenu(parent=self.canvas)
        for icon, title, callback in [
            (FluentIcon.BROOM, "去除框内原文（可撤销）", self.erase_region),
            (FluentIcon.SYNC, "OCR 识别选中框", lambda: self.run("ocr", scope=2)),
            (FluentIcon.LANGUAGE, "翻译选中框", lambda: self.run("translate", scope=2)),
            (
                FluentIcon.FONT_SIZE,
                "自动适应框大小",
                lambda: self.style_value("autofit", True),
            ),
            (
                FluentIcon.CHECKBOX,
                "锁定 / 解锁文字框",
                lambda: self.region_value(
                    "geometry_locked",
                    not (self.region() or {}).get("geometry_locked", False),
                ),
            ),
            (FluentIcon.DELETE, "删除文字框（可撤销）", self.delete_regions),
            (FluentIcon.LEFT_ARROW, "撤销", self.undo.undo),
            (FluentIcon.RIGHT_ARROW, "重做", self.undo.redo),
        ]:
            menu.addAction(
                Action(
                    icon,
                    title,
                    triggered=lambda checked=False, action=callback: action(),
                )
            )
        menu.exec(position)

    def erase_region(self):
        if self.region():
            self.region_value("erase_only", True)
            self.variant.setCurrentIndex(self.variant.findData("render"))
            self.draw_canvas()

    def note_manual_edits(self, edits):
        if self.job_id or self.submitting:
            self.job_manual_paths.update(path for path, _, _ in edits)

    def record_external_edit(self, base, remote, label, exclude_paths=None):
        manual = self.job_manual_paths if exclude_paths is None else exclude_paths
        edits = [
            edit
            for edit in changes(base, remote)
            if value_at(self.document, edit[0]) == edit[2]
            and not any(
                edit[0][: len(path)] == path or path[: len(edit[0])] == edit[0]
                for path in manual
            )
        ]
        if edits:
            self.undo.push(
                EditCommand(
                    self, base, remote, label, already_applied=True, edits=edits
                )
            )

    def apply_imported_translation(self, result):
        base = deepcopy(self.document)
        self.document = result["workspace"]
        self.saved_snapshot = deepcopy(self.document)
        self.record_external_edit(base, self.document, "导入译文")
        self.refresh_pages()
        self.select_region(self.selected)
        self.draw_canvas()

    def retry_failed(self):
        ids = [
            r["id"]
            for p in (self.document or {}).get("pages", [])
            for r in p["regions"]
            if r.get("translation_state") in {"failed", "cancelled"}
        ]
        if not ids:
            self.status.setText("没有失败的翻译文字框")
            return
        self.run("translate", scope=1, region_ids=ids)

    def show_task_details(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("漫画翻译 · 进度与失败明细")
        dialog.resize(760, 500)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(self.progress_label.text()))
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["页面", "文字框", "状态", "失败原因"])
        rows = [
            (p, r)
            for p in (self.document or {}).get("pages", [])
            for r in p["regions"]
            if r.get("translation_state") != "idle"
        ]
        table.setRowCount(len(rows))
        states = {
            "pending": "等待翻译",
            "succeeded": "成功",
            "failed": "失败",
            "cancelled": "已取消",
            "idle": "未开始",
        }
        for i, (p, r) in enumerate(rows):
            for j, value in enumerate(
                [
                    p["name"],
                    str(r["order"] + 1),
                    states.get(r.get("translation_state"), "未开始"),
                    r.get("translation_error", ""),
                ]
            ):
                table.setItem(i, j, QTableWidgetItem(value))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.resizeColumnsToContents()
        table.cellDoubleClicked.connect(
            lambda row, col: self.locate_region(rows[row][0]["id"], rows[row][1]["id"])
        )
        layout.addWidget(table)
        self.button(
            layout, "重试失败文字框", lambda: (dialog.accept(), self.retry_failed())
        )
        dialog.exec()

    def locate_region(self, page_id, region_id):
        index = next(
            (i for i, p in enumerate(self.document["pages"]) if p["id"] == page_id), -1
        )
        if index >= 0:
            self.pages.setCurrentRow(index)
            self.select_region(region_id)
            self.draw_canvas()

    @staticmethod
    def button(layout, label, action):
        if label in {"导入图片", "翻译并生成", "翻译整部"}:
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

    def connect_task(self, task, callback):
        self.task_callbacks[task] = callback
        task.signals.done.connect(self.task_finished, Qt.QueuedConnection)

    @pyqtSlot(object, object, object)
    def task_finished(self, task, result, error):
        callback = self.task_callbacks.pop(task, None)
        try:
            if not self.closed and callback:
                callback(result, error)
        except Exception as callback_error:
            self.last_callback_error = callback_error
            self.status.setText("界面更新失败，当前编辑已保留：" + str(callback_error))
        finally:
            self.tasks.discard(task)

    def background(self, action, callback=lambda r: None):
        if self.closed:
            return
        task = Background(action)
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            if error:
                self.status.setText(error)
            else:
                callback(result)

        self.connect_task(task, done)
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
        self.variant.setCurrentIndex(self.variant.findData("render"))
        self.variant.blockSignals(False)
        self.workspace_stack.setCurrentWidget(self.work_page)
        self.save_timer.stop()
        self.dirty = False
        self.image_data = None
        self.document = document
        self.saved_snapshot = deepcopy(document)
        self.last_report = {}
        frames = [r for p in document["pages"] for r in p["regions"]]
        done = sum(bool(r.get("target")) for r in frames)
        failed = sum(
            r.get("translation_state") in {"failed", "cancelled"} for r in frames
        )
        self.progress_bar.setRange(0, max(1, len(frames)))
        self.progress_bar.setValue(done)
        self.progress_label.setText(
            f"本工程 · 有译文 {done} 框 · 失败 / 取消 {failed} 框 · 总计 {len(frames)} 框"
        )
        self.selected = None
        self.undo.clear()
        self.page_id = document["pages"][0]["id"] if document["pages"] else None
        self.refresh_pages()
        self.populate_settings()
        self.select_region(None)
        self.load_image()
        self.status.setText(
            "工程已加载：拖动文字框实时排版，右键编辑；识别与翻译操作在顶栏。"
        )

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
                state = {
                    "pending": "翻译中",
                    "succeeded": "成功",
                    "failed": "失败",
                    "cancelled": "取消",
                }.get(r.get("translation_state")) or (
                    "有译文" if r["target"] else "待翻译" if r["source"] else "待识别"
                )
                text = (
                    "原："
                    + (r["source"] or "待识别")
                    + "\n译："
                    + (r["target"] or "待翻译")
                )
                for col, value in enumerate([str(row + 1), text, state]):
                    item = QTableWidgetItem(value)
                    item.setToolTip(r.get("translation_error") or text)
                    if state == "失败":
                        item.setForeground(QColor("#e74856"))
                    self.regions.setItem(row, col, item)
                text_width = max(120, self.regions.columnWidth(1) - 24)
                height = (
                    self.regions.fontMetrics()
                    .boundingRect(QRect(0, 0, text_width, 10000), Qt.TextWordWrap, text)
                    .height()
                    + 24
                )
                self.regions.setRowHeight(row, max(100, min(220, height)))
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
        for widget in (
            self.source,
            self.target,
            self.font,
            self.size,
            self.minimum,
            self.autofit,
            self.vertical,
            self.bold,
            self.spacing,
            self.charspace,
            self.stroke,
            self.align,
            self.rotation,
            self.source_lock,
            self.target_lock,
            self.geometry_lock,
        ):
            widget.setEnabled(bool(r))
        if not r:
            self.source.clear()
            self.target.clear()
            self.source.setPlaceholderText("点击画布或阅读顺序中的文字框")
            self.target.setPlaceholderText("选中框后可编辑译文与排版")
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
            editing = self.variant.currentData() == "render"
            self.canvas.live_layout = editing and self.canvas.rect_key == "layout_rect"
            if editing and self.document["settings"].get("removal") == "fill":
                data = self.fill_preview(data)
            if editing and not self.canvas.live_layout:
                from io import BytesIO

                from PIL import Image

                from Shared.manga_lettering import render_text

                image = Image.open(BytesIO(data)).convert("RGBA")
                for r in self.page()["regions"]:
                    if r.get("target"):
                        tile, _ = render_text(r)
                        image.alpha_composite(
                            tile,
                            (
                                round(r["layout_rect"]["x"]),
                                round(r["layout_rect"]["y"]),
                            ),
                        )
                buffer = BytesIO()
                image.save(buffer, "PNG")
                data = buffer.getvalue()
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
        ticket = (
            self.session,
            doc,
            page,
            variant,
            self.document["revision"],
            self.epoch,
            uuid4().hex,
        )
        self.image_ticket = ticket

        def loaded(data):
            if self.image_ticket != ticket:
                return
            fit = self.image_data is None
            self.image_data = data
            self.draw_canvas()
            if fit:
                self.canvas.fitInView(self.canvas.sceneRect(), Qt.KeepAspectRatio)

        precise = self.document["settings"].get("removal") == "precise"
        signature = self.background_signature()

        def fetch():
            asset = (
                "clean"
                if variant == "render" and precise
                else "source"
                if variant == "render"
                else variant
            )
            if asset != "source":
                response = self.client.backend.http.get(
                    f"/api/v2/manga/{doc}/pages/{page}/{asset}", timeout=60
                )
                if response.status_code == 404:
                    return self.client.asset(doc, page, "source"), True
                response.raise_for_status()
                return response.content, False
            return self.client.asset(doc, page, "source"), variant == "render"

        def ready(result):
            if self.image_ticket != ticket:
                return
            data, pending = result
            self.image_is_source = pending or variant == "source"
            self.clean_signature = (
                signature
                if variant in {"render", "clean"} and precise and not pending
                else None
            )
            loaded(data)
            if pending and self.document["settings"].get("removal") == "precise":
                self.status.setText(
                    "当前页显示原图与文字框，精准预览尚未生成；可点击排版预览。"
                )

        self.background(fetch, ready)

    def change_tool(self, index):
        self.canvas.mode = ["select", "add", "mask", "eraser"][index]
        self.canvas.setCursor(Qt.CrossCursor if index else Qt.ArrowCursor)
        self.brush.setVisible(index in {2, 3})
        for i, button in enumerate(self.tool_buttons):
            button.setChecked(i == index)
        self.draw_canvas()

    def change_geometry(self):
        self.canvas.rect_key = self.geometry.currentData()
        self.geometry_hint.setText(
            {
                "layout_rect": "拖框移动译文 · 拖右下角自动缩放文字 · 右键更多操作",
                "ocr_rect": "高级：仅修改 OCR 从哪里读取原文（不移动译文）",
                "erase_rect": "高级：仅修改去除原文的背景范围（不移动译文）",
            }[self.canvas.rect_key]
        )
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
        self.preview_pending = (
            self.document["settings"].get("removal") == "precise"
            and self.clean_signature != self.background_signature()
            and any(
                r.get("target") or r.get("erase_only")
                for r in (self.page() or {}).get("regions", [])
            )
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

            def action():
                self.region()[key] = value
                if key in {"source", "target"}:
                    self.region()["translation_state"] = "idle"
                    self.region()["translation_error"] = ""

            self.edit("修改文字框", action)

    def style_value(self, key, value):
        if self.region():
            self.edit(
                "修改排版", lambda: self.region()["style"].__setitem__(key, value)
            )

    def geometry_changed(self, identifier, key, rect):
        if not any(
            r["id"] == identifier for r in (self.page() or {}).get("regions", [])
        ):
            return
        self.selected = identifier
        self.edit("调整区域", lambda: self.region().__setitem__(key, rect))

    def rotate(self, value):
        if self.region():
            self.edit(
                "旋转译文",
                lambda: self.region()["layout_rect"].__setitem__("rotation", value),
            )

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
            "erase_only": False,
            "translation_state": "idle",
            "translation_error": "",
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

        self.connect_task(task, complete)
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

    def run(self, stage, saved=False, preview=False, scope=None, region_ids=None):
        if not self.document or self.job_id or self.submitting:
            return
        if not saved:
            self.save(lambda: self.run(stage, True, preview, scope, region_ids))
            return
        scope = 0 if preview else self.scope.currentIndex() if scope is None else scope
        pages = [] if scope == 1 else [self.page_id]
        regions = (
            list(region_ids)
            if region_ids is not None
            else self.canvas.selected_ids()
            if scope == 2
            else []
        )
        if region_ids is not None:
            pages = []
        if scope == 2 and not regions:
            self.status.setText("请先选中文字框")
            return
        replace = self.overwrite.isChecked() or region_ids is not None
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
            self.document = merge_edits(
                self.saved_snapshot, self.document, result["workspace"]
            )
            self.saved_snapshot = deepcopy(result["workspace"])
            self.record_external_edit(
                base,
                result["workspace"],
                {
                    "detect": "检测文字框",
                    "ocr": "OCR 识别",
                    "recognize": "检测并 OCR",
                    "translate": "翻译",
                    "pipeline": "翻译并排版",
                }.get(stage, "处理文字框"),
            )
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
        self.job_manual_paths = set()
        session = self.session
        self.status.setText("正在提交任务…")
        self.progress_label.setText("正在提交任务…")
        self.progress_bar.setRange(0, 0)

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
            self.submitting = False
            if error:
                self.status.setText(error)
            else:
                started(result)

        self.connect_task(task, done)
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
                detail = f"{label} {progress['current']} / {progress.get('total', 0)} {progress.get('unit', '页')}"
            elif "line" in progress:
                detail = f"翻译 {progress['line']} / {progress.get('total_line', 0)} 条"
            self.status.setText(state + (" · " + detail if detail else ""))
            total = progress.get("total", progress.get("total_line", 0))
            current = progress.get("current", progress.get("line", 0))
            if total:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(current)
            self.progress_label.setText(
                state
                + (" · " + detail if detail else "")
                + (
                    f" · 已请求 {progress['total_requests']} 次"
                    if "total_requests" in progress
                    else ""
                )
            )
            if job["state"] in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "interrupted",
            }:
                self.poll_timer.stop()
                self.job_id = None
                self.progress_bar.setRange(0, 100)
                if job["state"] == "succeeded":
                    self.progress_bar.setValue(100)
                else:
                    self.progress_label.setText(state + " · 可查看明细或重试失败文字框")
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
                        self.recover_workspace(record_base=self.job_base)

        task = Background(
            lambda: self.client.backend.request("GET", "/api/jobs/" + identifier)
        )
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            if error:
                self.polling = False
                self.status.setText(error)
            else:
                loaded(result)

        self.connect_task(task, done)
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

    def recover_workspace(self, baseline=None, record_base=None):
        base = deepcopy(baseline or self.saved_snapshot)
        manual_paths = set(self.job_manual_paths)

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
            if record_base:
                self.record_external_edit(
                    record_base, remote, "任务中已完成的修改", manual_paths
                )
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
                    self.apply_imported_translation,
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
        if "translation" in report:
            self.last_report = report["translation"]
            translation = self.last_report
            total, succeeded, failed = (
                translation.get("total", 0),
                translation.get("succeeded", 0),
                translation.get("failed", 0),
            )
            self.failed_region_ids = [
                r["item_id"] for r in translation.get("failed_items", [])
            ]
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(succeeded + translation.get("skipped", 0))
            self.progress_label.setText(
                f"翻译完成 · 成功 {succeeded} 框 · 失败 {failed} 框 · 跳过 {translation.get('skipped', 0)} 框"
                if total
                else "没有需要翻译的原文；请先 OCR，已有译文默认保留"
            )
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

    def background_signature(self):
        import json

        return json.dumps(
            {
                "page": self.page_id,
                "removal": (self.document or {}).get("settings", {}).get("removal"),
                "regions": [
                    {
                        k: r.get(k)
                        for k in (
                            "id",
                            "erase_rect",
                            "mask_strokes",
                            "fill_color",
                            "fill_opacity",
                        )
                    }
                    for r in (self.page() or {}).get("regions", [])
                    if r.get("target") or r.get("erase_only")
                ],
            },
            sort_keys=True,
        )

    def fill_preview(self, data):
        from io import BytesIO
        from statistics import median

        from PIL import Image, ImageColor

        image = Image.open(BytesIO(data)).convert("RGB")
        original = image.copy()
        for region in self.page()["regions"]:
            if not region.get("target", "").strip() and not region.get("erase_only"):
                continue
            r = region["erase_rect"]
            import math

            box = (
                int(r["x"]),
                int(r["y"]),
                math.ceil(r["x"] + r["width"]),
                math.ceil(r["y"] + r["height"]),
            )
            patch = original.crop(box)
            color = region.get("fill_color", "auto")
            if color == "auto":
                pixels = []
                for border in (
                    patch.crop((0, 0, patch.width, 1)),
                    patch.crop((0, patch.height - 1, patch.width, patch.height)),
                    patch.crop((0, 0, 1, patch.height)),
                    patch.crop((patch.width - 1, 0, patch.width, patch.height)),
                ):
                    values = border.tobytes()
                    pixels.extend(zip(values[0::3], values[1::3], values[2::3]))
                color = tuple(int(median(p[i] for p in pixels)) for i in range(3))
            else:
                color = ImageColor.getrgb(color)
            fill = Image.new("RGB", patch.size, color)
            image.paste(
                Image.blend(image.crop(box), fill, region.get("fill_opacity", 1)), box
            )
        output = BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    def toggle_precise(self, checked):
        if self.loading_form or not self.document:
            return
        self.set_setting("removal", "precise" if checked else "fill")
        if self.variant.currentData() in {"render", "clean"}:
            self.variant.setCurrentIndex(self.variant.findData("render"))
            self.load_image()
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
        self.closed = True
        self.task_callbacks.clear()
        self.save_timer.stop()
        self.poll_timer.stop()
