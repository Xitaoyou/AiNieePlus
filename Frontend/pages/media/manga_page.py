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
    QCursor,
    QDesktopServices,
    QKeySequence,
    QPalette,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QShortcut,
    QSizePolicy,
    QSlider,
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
    BodyLabel,
    ComboBox,
    DropDownPushButton,
    FluentIcon,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PrimarySplitPushButton,
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
# 右栏必须允许横向滚动：qfluentwidgets 的 SingleDirectionScrollArea
# 把横向策略固定成 AlwaysOff，窄栏时内容会被裁掉。
from PyQt5.QtWidgets import QScrollArea as NativeScrollArea

from Frontend.pages.media.manga_canvas import MangaCanvas
from Frontend.pages.media.manga_progress_overlay import MangaProgressOverlay
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
            if not previous:
                self.flush_pending_edits()

    return guarded


# 与文本翻译页（LanguageSettingsCard）保持同一套语言，只保留后端
# map_language_code_to_name 能识别的代码，避免出现「未知语言」提示词。
LANGUAGE_CHOICES = [
    ("日语", "ja"),
    ("简中", "zh-CN"),
    ("繁中", "zh-TW"),
    ("英语", "en"),
    ("韩语", "ko"),
    ("俄语", "ru"),
    ("德语", "de"),
    ("法语", "fr"),
    ("西班牙语", "es"),
    ("印尼语", "id"),
    ("越南语", "vi"),
    ("泰语", "th"),
]


def language_label(code):
    return next((title for title, value in LANGUAGE_CHOICES if value == code), code)


# 弹窗统一配色：qfluentwidgets 只接管它自己那批控件，原生 QLabel / QLineEdit /
# QPlainTextEdit 在暗色主题下仍是系统浅色，白底黑字混进来就很突兀，所以这里
# 统一压一层 QSS，并跟随主题切换重刷。
DIALOG_TONES = {
    "dark": {
        "window": "#202020",
        "surface": "#2b2b2b",
        "text": "#f0f0f0",
        "muted": "#c2c2c2",
        "border": "rgba(255,255,255,0.16)",
        "header": "#272727",
    },
    "light": {
        "window": "#f7f9fc",
        "surface": "#ffffff",
        "text": "#202020",
        "muted": "#5a5a5a",
        "border": "rgba(0,0,0,0.12)",
        "header": "#eef2f8",
    },
}


def dialog_style(name):
    tones = DIALOG_TONES["dark" if isDarkTheme() else "light"]
    return f"""
QDialog#{name} {{background:{tones["window"]};}}
QDialog#{name} QLabel {{color:{tones["text"]};background:transparent;}}
QDialog#{name} QLineEdit, QDialog#{name} QPlainTextEdit, QDialog#{name} QTextEdit {{
    color:{tones["text"]};background:{tones["surface"]};
    border:1px solid {tones["border"]};border-radius:6px;padding:4px 8px;
    selection-background-color:#0f6cbd;selection-color:#ffffff;
}}
QDialog#{name} QTableWidget {{
    color:{tones["text"]};background:{tones["surface"]};
    alternate-background-color:{tones["window"]};
    border:1px solid {tones["border"]};border-radius:8px;
}}
QDialog#{name} QHeaderView::section {{
    color:{tones["muted"]};background:{tones["header"]};
    border:none;border-bottom:1px solid {tones["border"]};
    padding:4px 6px;font-weight:600;
}}
"""


def apply_dialog_theme(dialog, name):
    """给弹窗套统一配色，并在切换明暗主题时自动重刷。"""
    dialog.setObjectName(name)
    dialog.setStyleSheet(dialog_style(name))

    def refresh(*_):
        if sip.isdeleted(dialog):
            return
        dialog.setStyleSheet(dialog_style(name))

    qconfig.themeChanged.connect(refresh)

    def detach(*_):
        try:
            qconfig.themeChanged.disconnect(refresh)
        except (TypeError, RuntimeError):
            pass

    dialog.destroyed.connect(detach)
    return dialog


def dialog_layout(dialog, spacing=12):
    """弹窗正文布局：统一的边距与行距，避免各弹窗疏密不一。"""
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 18, 20, 16)
    layout.setSpacing(spacing)
    return layout


def dialog_note(text):
    """弹窗里的说明文字：自动换行、不撑宽窗口。"""
    note = QLabel(text)
    note.setWordWrap(True)
    note.setMinimumWidth(0)
    return note


class ConfirmBox(MessageBoxBase):
    """和 AiNiee 其它页面一致的确认框：标题 + 正文 + 可选勾选项 + 按钮。"""

    def __init__(self, parent, title, content, checkbox=None,
                 yes="确定", no=None, cancel="取消", default_yes=True):
        super().__init__(parent)
        self.titleLabel = StrongBodyLabel(title, self.widget)
        self.contentLabel = BodyLabel(content, self.widget)
        self.contentLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.contentLabel)
        self.checkbox = None
        if checkbox:
            self.checkbox = QCheckBox(checkbox, self.widget)
            self.viewLayout.addWidget(self.checkbox)
        self.yesButton.setText(yes)
        self.cancelButton.setText(cancel)
        self.widget.setMaximumWidth(520)
        self.widget.setMinimumWidth(360)
        if no:
            self.noButton = QPushButton(no, self.buttonGroup)
            self.noButton.clicked.connect(lambda: self.done(QMessageBox.No))
            self.buttonLayout.insertWidget(
                self.buttonLayout.indexOf(self.cancelButton), self.noButton, 1
            )
        if not default_yes:
            self.yesButton.setDefault(False)

    # MessageBoxBase 的按钮走 accept()/reject()，而调用点沿用 QMessageBox
    # 的 Yes/No/Cancel 语义，这里把退出码映射过去。
    def accept(self):
        self.done(QMessageBox.Yes)

    def reject(self):
        self.done(QMessageBox.Cancel)

    def checked(self):
        return bool(self.checkbox and self.checkbox.isChecked())


class InputBox(MessageBoxBase):
    """取一段文字或从固定选项里挑一项，替代原生 QInputDialog。"""

    def __init__(self, parent, title, prompt, value="", choices=None,
                 yes="确定", cancel="取消"):
        super().__init__(parent)
        self.titleLabel = StrongBodyLabel(title, self.widget)
        self.promptLabel = BodyLabel(prompt, self.widget)
        self.promptLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.promptLabel)
        self.choices = choices
        if choices is None:
            self.field = LineEdit(self.widget)
            self.field.setText(value)
            self.field.selectAll()
        else:
            self.field = ComboBox(self.widget)
            for item in choices:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    self.field.addItem(item[0], userData=item[1])
                else:
                    self.field.addItem(str(item), userData=item)
            index = self.field.findText(value) if value else 0
            self.field.setCurrentIndex(max(0, index))
        self.viewLayout.addWidget(self.field)
        self.yesButton.setText(yes)
        self.cancelButton.setText(cancel)
        self.widget.setMaximumWidth(460)
        self.widget.setMinimumWidth(340)

    def text(self):
        if self.choices is None:
            return self.field.text()
        return self.field.currentText()

    def data(self):
        if self.choices is None:
            return self.field.text()
        return self.field.currentData()

    def showEvent(self, event):
        super().showEvent(event)
        self.field.setFocus()


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


class RegionRetryDialog(QDialog):
    """单框/多框重试：提示已有内容，并可临时更换 OCR / 翻译引擎。"""

    def __init__(self, page, kind, region, region_ids=None):
        super().__init__(page)
        self.page = page
        self.kind = kind
        self.regions = region if isinstance(region, list) else [region]
        region = self.regions[0]
        self.region = region
        self.ids = list(region_ids or [region["id"]])
        self.multi = len(self.ids) > 1
        title = ("重新 OCR " if kind == "ocr" else "重新翻译 ") + (
            f"选中的 {len(self.ids)} 个框" if self.multi else "此框"
        )
        self.setWindowTitle(title)
        self.resize(480, 240)
        layout = dialog_layout(self)
        apply_dialog_theme(self, "mangaRegionRetryDialog")
        field = "source" if kind == "ocr" else "target"
        filled = [r for r in self.regions if r[field].strip()]
        existing = filled[0][field].strip() if filled else ""
        if self.multi:
            text = (
                f"选中的 {len(self.ids)} 个框中有 {len(filled)} 个已有"
                + ("原文" if kind == "ocr" else "译文")
                + "，重试会覆盖这 "
                + str(len(filled))
                + " 个框。"
            )
        else:
            text = (
                ("该框已有原文：" if kind == "ocr" else "该框已有译文：")
                + (existing[:80] + ("…" if len(existing) > 80 else ""))
            )
        for caption in (
            text,
            "可以换一个引擎重试，其他框保持不变。",
        ):
            label = dialog_note(caption)
            layout.addWidget(label)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.engine = QComboBox()
        self.fill_engines()
        form.addRow("引擎", self.engine)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        retry = PrimaryPushButton("重试")
        retry.clicked.connect(self.accept)
        retry.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(retry)
        layout.addLayout(buttons)

    def fill_engines(self):
        catalogue = getattr(self.page, "model_catalogue", None) or []
        current = (
            self.page.document["settings"]["ocr"]
            if self.kind == "ocr"
            else self.page.document["settings"].get("provider_id")
        )
        entries = []
        if self.kind == "ocr":
            for model in catalogue:
                if model.get("role") != "ocr":
                    continue
                kind = model.get("kind", "local")
                usable = (
                    (kind in {"local", "system"} and model.get("installed"))
                    or (kind == "package" and model.get("installed"))
                    or (kind == "api" and model.get("api_ready"))
                )
                if usable:
                    entries.append((model["name"], model["id"]))
        else:
            for index in range(self.page.provider.count()):
                entries.append(
                    (self.page.provider.itemText(index), self.page.provider.itemData(index))
                )
        if not entries:
            entries = [("使用当前设置", None)]
        for name, value in entries:
            self.engine.addItem(name, value)
        index = self.engine.findData(current)
        if index >= 0:
            self.engine.setCurrentIndex(index)

    def engine_id(self):
        return self.engine.currentData()


class ApiConfigDialog(QDialog):
    """配置云 OCR 服务的 API Key / 网关地址 / 模型名。"""

    def __init__(self, page, model):
        super().__init__(page)
        self.page = page
        self.model = model
        self.provider = model.get("api_provider") or ""
        self.setWindowTitle(f"配置 API · {model['name']}")
        self.resize(520, 268)
        layout = dialog_layout(self)
        apply_dialog_theme(self, "mangaApiConfigDialog")
        intro = dialog_note(
            f"{model['advice']}\n\n填写后即可在项目设置里选择该 OCR。"
            "API Key 只保存在本机用户目录。"
        )
        layout.addWidget(intro)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.appid = QLineEdit()
        self.appid.setPlaceholderText("百度填 AppID；腾讯填 SecretId；其他可留空")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-… / 百度 API Key / SecretKey")
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("留空使用官方地址；也可填兼容网关")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText(
            "OpenAI: gpt-4o-mini · Gemini: gemini-2.0-flash · "
            "百度: accurate / network（webimage）· Ollama: llava"
        )
        form.addRow("AppID", self.appid)
        form.addRow("API Key", self.api_key)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名", self.model_name)
        layout.addLayout(form)
        self.status = dialog_note("")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        clear = QPushButton("清除配置")
        clear.clicked.connect(lambda: self.save({}))
        save = PrimaryPushButton("保存")
        save.clicked.connect(self.save_from_form)
        save.setDefault(True)
        buttons.addWidget(clear)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.load_current()

    def load_current(self):
        def loaded(data):
            config = (data.get("configs") or {}).get(self.provider, {})
            self.appid.setText(config.get("appid", ""))
            self.api_key.setText(config.get("api_key", ""))
            self.base_url.setText(config.get("base_url", ""))
            self.model_name.setText(config.get("model", ""))

        self.page.background(
            lambda: self.page.client.request("GET", "/model-apis"), loaded
        )

    def save_from_form(self):
        self.save(
            {
                "appid": self.appid.text().strip(),
                "api_key": self.api_key.text().strip(),
                "base_url": self.base_url.text().strip(),
                "model": self.model_name.text().strip(),
            }
        )

    def save(self, payload):
        body = {"provider": self.provider, **payload}

        def request():
            return self.page.client.request("POST", "/model-apis", json=body)

        def done(result):
            if "models" not in result:
                self.status.setText(str(result.get("error", "保存失败")))
                return
            self.page.refresh_model_catalogue(result["models"])
            if not payload:
                self.status.setText("已清除该服务配置")
                self.reject()
                return
            self.status.setText("已保存，正在验证…")
            self.verify()

        self.page.background(request, done)

    def verify(self):
        def request():
            return self.page.client.request(
                "POST", "/model-apis/verify", json={"provider": self.provider}
            )

        def done(result):
            models = result.get("models")
            if models:
                self.page.refresh_model_catalogue(models)
            if result.get("ok"):
                self.status.setStyleSheet("")
                self.status.setText("验证通过：该 OCR 已加入项目设置可选列表")
                self.accept()
            else:
                message = result.get("message") or result.get("error") or "未知错误"
                self.status.setText("验证失败：" + str(message))
                self.status.setStyleSheet("color:#e74856;")

        self.page.background(request, done)


class ModelDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        apply_dialog_theme(self, "mangaModelDialog")
        self.page = page
        self.setWindowTitle("漫画模型中心")
        # 按屏幕可用区收缩：1366×768 的笔记本上 1180×700 会顶出任务栏，
        # 高度取可用高度减 80 留出标题栏与边距。
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(
            min(1180, max(880, available.width() - 120)),
            min(700, max(480, available.height() - 80)),
        )
        self.setMinimumSize(880, 480)
        layout = dialog_layout(self, spacing=10)
        apply_dialog_theme(self, "mangaModelDialog")
        self.intro = dialog_note(
            "检测负责画框，OCR 负责读字。日漫批量检测可先试 Layout 768；"
            "选中模型可查看完整用途、推荐和安装方式。"
        )
        layout.addWidget(self.intro)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["模型", "类型", "状态", "大小", "用途与推荐", "许可", "操作"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setFixedHeight(34)
        # 说明列自适应剩余宽度，其余列固定，避免横向滚动与挤压
        for column, mode, width in (
            (0, QHeaderView.Interactive, 240),   # 模型
            (1, QHeaderView.Fixed, 120),         # 类型
            (2, QHeaderView.Fixed, 170),         # 状态
            (3, QHeaderView.Interactive, 84),    # 大小
            (4, QHeaderView.Stretch, 0),         # 功能说明
            (5, QHeaderView.Interactive, 130),   # 来源许可
            (6, QHeaderView.Fixed, 100),         # 操作
        ):
            header.setSectionResizeMode(column, mode)
            if width:
                self.table.setColumnWidth(column, width)
        self.table.verticalHeader().setDefaultSectionSize(38)
        layout.addWidget(self.table, 1)
        self.model_details = dialog_note("选中模型查看用途和推荐")
        layout.addWidget(self.model_details)
        self.table.currentCellChanged.connect(self.update_model_details)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        layout.addLayout(bar)
        self.api_button = QPushButton("配置 API")
        self.uninstall_button = QPushButton("卸载模型")
        self.uninstall_button.setEnabled(False)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
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
        self.api_button.clicked.connect(self.configure_api)
        bar.addWidget(self.api_button)
        bar.addWidget(self.uninstall_button)
        self.status = dialog_note("")
        layout.addWidget(self.status)
        self.refresh()

    KIND_LABELS = {
        "detect": "检测模型",
        "ocr": "本地 OCR",
        "package": "pip OCR",
        "api": "云 API OCR",
        "system": "系统 OCR",
        "inpaint": "去字",
    }

    def configure_api(self):
        self.configure_api_for(self.selected())

    def configure_api_for(self, model):
        if not model or model.get("kind") != "api":
            self.status.setText("请选择一个云 API OCR 模型后再配置")
            return
        ApiConfigDialog(self.page, model).exec()
        self.refresh()

    def download_for(self, model):
        if not model or model.get("installed"):
            return
        if model.get("can_uninstall"):
            self.status.setText("请先卸载不完整的模型，再重新下载")
            return
        self.model_request(
            "POST", f"/models/{model['id']}/download",
            f"正在下载 {model['name']}（约 {model['size_mb']} MB）…", "模型已下载并安装",
        )

    def model_request(self, method, path, pending, success, **kwargs):
        if self.page.job_id or self.page.submitting:
            self.status.setText("请等待当前任务结束，或先取消")
            return
        self.status.setText(pending)

        def failed(message):
            if not sip.isdeleted(self):
                self.status.setText(str(message))

        def done(result):
            if not sip.isdeleted(self):
                self.loaded(result)
                self.status.setText(success)
            else:
                self.page.refresh_model_catalogue(result["models"])

        self.page.start_request(
            lambda: self.page.client.request(
                method, path, headers={"Prefer": "respond-async"}, **kwargs
            ),
            done,
            on_error=failed,
        )

    def import_for(self, model):
        if not model:
            return
        self.select_row_for(model)
        self.import_model()

    def select_row_for(self, model):
        for row, item in getattr(self, "row_models", {}).items():
            if item is model:
                self.table.setCurrentCell(row, 0)
                return

    def package_hint(self, model=None):
        module = {"paddle": "paddleocr paddlepaddle", "easyocr": "easyocr", "doctr": "python-doctr"}.get(
            (model or {}).get("backend", ""), "对应依赖"
        )
        self.status.setText(
            f"在 AiNiee 的运行环境执行：pip install {module}，首次使用会自动下载模型权重"
        )

    def apply_theme(self, *_):
        self.setStyleSheet(dialog_style("mangaModelDialog"))

    def refresh(self):
        self.page.background(
            lambda: self.page.client.request("GET", "/models"), self.loaded
        )

    GROUPS = [
        ("漫画版面检测（文字与分镜）", lambda m: m.get("backend") == "layout"),
        (
            "检测模型（按语言选择）",
            lambda m: m.get("role") == "detect" and m.get("backend") != "layout",
        ),
        (
            "本地 OCR（内置 / ONNX 权重）",
            lambda m: m.get("role") == "ocr" and m.get("kind", "local") == "local",
        ),
        ("pip 包 OCR（首次使用时自动下载模型）", lambda m: m.get("kind") == "package"),
        ("云 API OCR（需配置密钥并验证）", lambda m: m.get("kind") == "api"),
        ("系统 OCR（无需下载）", lambda m: m.get("kind") == "system"),
        ("去字模型", lambda m: m.get("role") == "inpaint"),
    ]

    ROLE_FEATURE = {
        "detect": (
            "定位对白气泡与分镜",
            "决定后续识别与排版范围；漏检会少翻，误检会产生空框",
        ),
        "ocr": (
            "把框内文字转成原文",
            "决定原文准确率；日漫竖排优先选支持竖排的引擎",
        ),
        "inpaint": ("擦除原文字并补背景", "影响成品干净程度与导出耗时"),
    }

    def tooltip_for(self, model):
        # 悬停展示：功能定位、体积、建议阈值、语言、特点与对产出的影响
        role = model.get("role", "ocr")
        feature, impact = self.ROLE_FEATURE.get(role, ("", ""))
        kind_label = {
            "local": "本地模型",
            "package": "Python 包",
            "api": "云 API",
            "system": "系统组件",
        }.get(model.get("kind", "local"), "本地模型")
        lines = [model.get("name", model["id"])]
        if feature:
            lines.append(f"{kind_label} · {feature}")
        if model.get("size_mb"):
            lines.append(f"体积约 {model['size_mb']} MB")
        if model.get("default_confidence"):
            lines.append(f"建议阈值 {model['default_confidence']}")
        if model.get("langs"):
            lines.append("语言：" + "、".join(model["langs"]))
        lines.append("")
        lines.append("特点：" + (model.get("advice") or "—"))
        if model.get("recommendation"):
            lines.append(model["recommendation"])
        if impact:
            lines.append("影响：" + impact)
        if model.get("requires_api") and not model.get("api_ready"):
            lines.append("")
            lines.append("尚未配置 API；配置并验证后才会出现在项目设置里")
        elif model.get("api_error"):
            lines.append("")
            lines.append("上次验证：" + str(model["api_error"]))
        return "\n".join(lines)

    def row_state(self, model):
        kind = model.get("kind", "local")
        if not model.get("supported", True):
            return "尚未适配", True
        if kind == "api":
            if not model.get("installed"):
                return "未配置", False
            if not model.get("api_has_key"):
                return "待填写密钥", True
            if not model.get("api_verified"):
                error = model.get("api_error") or ""
                return ("验证失败：" + error[:40]) if error else "待验证", True
            return "已就绪", False
        if kind == "system":
            return ("系统可用" if model.get("installed") else "系统不支持"), False
        if kind == "package":
            return (
                ("依赖已就绪" if model.get("installed") else "需安装 pip 包"),
                not model.get("installed"),
            )
        if model.get("installed"):
            return (
                ("内置随包" if model.get("core") else "已安装"),
                False,
            )
        if model.get("can_uninstall"):
            return "模型不完整 · 请卸载后重装", True
        if model.get("url"):
            return f"需下载 {model['size_mb']} MB", True
        return "需导入目录", True

    def action_for(self, model):
        """每行最右的操作按钮：文本 + 回调（无则禁用）。"""
        kind = model.get("kind", "local")
        if not model.get("supported", True):
            return "未适配", None
        if kind == "api":
            if model.get("api_verified"):
                return "重新配置", self.configure_api_for
            return "配置 API", self.configure_api_for
        if kind == "system":
            return ("可用" if model.get("installed") else "不支持"), None
        if kind == "package":
            if model.get("installed"):
                return "已就绪", None
            return "安装说明", self.package_hint
        if model.get("installed"):
            return "已安装", None
        if model.get("can_uninstall"):
            return "卸载残留", self.uninstall_for
        if model.get("url"):
            return "一键下载", self.download_for
        return "导入目录", self.import_for

    def loaded(self, data):
        selected = self.selected()
        selected_id = selected["id"] if selected else None
        models = list(data.get("models", []))
        self.models = models
        self.table.clearSpans()
        self.table.setRowCount(0)
        rows = []
        for title, predicate in self.GROUPS:
            items = [m for m in models if predicate(m)]
            if not items:
                continue
            items.sort(key=lambda m: (
                m.get("advanced", False),
                m["id"] != "manga-with-layout-768",
                m.get("name", ""),
            ))
            rows.append((title, None))
            rows.extend((None, model) for model in items)
        self.table.setRowCount(len(rows))
        self.row_models = {}
        red = QColor("#e74856")
        for row, (title, model) in enumerate(rows):
            if model is None:
                cell = QTableWidgetItem(title)
                font = cell.font()
                font.setBold(True)
                cell.setFont(font)
                cell.setFlags(Qt.ItemIsEnabled)
                shade = (
                    QColor(255, 255, 255, 18)
                    if isDarkTheme()
                    else QColor(0, 0, 0, 12)
                )
                cell.setBackground(shade)
                self.table.setItem(row, 0, cell)
                # 行跨度必须是 1，否则第 N 个分组会吞掉后面 N 行模型
                self.table.setSpan(row, 0, 1, self.table.columnCount())
                self.table.setRowHeight(row, 30)
                continue
            self.row_models[row] = model
            kind = model.get("kind", "local")
            label = (
                self.KIND_LABELS.get(kind, kind)
                if kind != "local"
                else self.KIND_LABELS.get(model.get("role", ""), model.get("role", ""))
            )
            if model.get("advanced"):
                label += " · 高级"
            state, warn = self.row_state(model)
            size = f"{model['size_mb']} MB" if model.get("size_mb") else "—"
            values = [
                model["name"],
                label,
                state,
                size,
                model.get("recommendation") or model["advice"],
                model["license"],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 2 and warn:
                    item.setForeground(red)
                item.setToolTip(self.tooltip_for(model))
                self.table.setItem(row, col, item)
            if kind == "api" and not model.get("api_ready"):
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is not None and col != 6:
                        cell.setBackground(QColor(231, 72, 86, 24))
            text, callback = self.action_for(model)
            button = QPushButton(text)
            button.setFixedHeight(26)
            button.setMinimumWidth(86)
            if callback is None:
                button.setEnabled(False)
            else:
                button.clicked.connect(
                    lambda checked=False, action=callback, target=model: action(target)
                )
            holder = QWidget(self.table)
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 0, 6, 0)
            holder_layout.addWidget(button)
            self.table.setCellWidget(row, 6, holder)
            self.table.setRowHeight(row, 38)
        pending = [
            m
            for m in models
            if m.get("kind") == "api" and m.get("installed") and not m.get("api_verified")
        ]
        downloadable = [
            m
            for m in models
            if m.get("kind", "local") == "local"
            and not m.get("installed")
            and not m.get("can_uninstall")
            and m.get("url")
            and m.get("supported", True)
        ]
        if pending:
            self.status.setText(
                f"{len(pending)} 个云 API OCR 待验证：点「配置 API」填好密钥并验证后才可选"
            )
        elif downloadable:
            total = sum(m.get("size_mb", 0) for m in downloadable)
            self.status.setText(
                f"{len(downloadable)} 个模型可下载（合计约 {total} MB），点行尾「一键下载」"
            )
        else:
            self.status.setText("")
        self.page.refresh_model_catalogue(models)
        if selected_id:
            for row, model in self.row_models.items():
                if model["id"] == selected_id:
                    self.table.setCurrentCell(row, 0)
                    break
        self.update_model_details()

    def selected(self):
        row = self.table.currentRow()
        return getattr(self, "row_models", {}).get(row)

    def update_model_details(self, *_):
        model = self.selected()
        self.uninstall_button.setEnabled(bool(model and model.get("can_uninstall")))
        if not model:
            self.model_details.setText("选中模型查看用途和推荐")
            return
        details = model.get("advice") or ""
        if model.get("recommendation"):
            details += "\n" + model["recommendation"]
        self.model_details.setText(details)

    def download(self):
        model = self.selected()
        if not model:
            return
        kind = model.get("kind", "local")
        if kind == "api":
            self.configure_api()
            return
        if kind == "package":
            self.status.setText(
                "该 OCR 随 pip 包提供：请先安装对应依赖（pip install），模型会在首次使用时自动下载"
            )
            return
        if kind == "system":
            self.status.setText("系统 OCR 无需下载，直接在项目设置里选择即可")
            return
        if not model.get("supported", True) or not model.get("url"):
            self.status.setText("该模型暂不支持一键下载，请从官方来源获取后使用「导入目录」")
            return
        if model.get("installed"):
            self.status.setText("该模型已安装")
            return
        self.download_for(model)

    def import_model(self):
        model = self.selected()
        if not model:
            return
        if model.get("kind", "local") != "local" or not model.get("supported", True):
            self.status.setText("请选择支持目录导入的本地模型")
            return
        if model.get("installed"):
            self.status.setText("该模型已安装")
            return
        if model.get("can_uninstall"):
            self.status.setText("请先卸载不完整的模型，再重新导入")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "选择模型目录"
        )
        if folder:
            self.model_request(
                "POST", f"/models/{model['id']}/import",
                "正在导入并校验…", "模型已导入", json={"path": folder},
            )

    def uninstall_selected(self):
        self.uninstall_for(self.selected())

    def uninstall_for(self, model):
        if not model or not model.get("can_uninstall"):
            return
        settings = (self.page.document or {}).get("settings", {})
        if model.get("installed") and (
            model["role"] == "detect" and settings.get("detector") == model["id"]
            or model["role"] == "ocr" and settings.get("ocr") == model["id"]
        ):
            self.status.setText("当前工程正在使用此模型，请先在项目设置中切换")
            return
        if self.page.ask(
            "卸载漫画模型",
            f"卸载「{model['name']}」？只删除 AiNiee 中安装的副本。"
            "其他工程如果使用此模型，需要重新安装或切换。",
            yes="卸载",
        ) != QMessageBox.Yes:
            return
        self.model_request(
            "DELETE", f"/models/{model['id']}", "正在卸载…", "模型已卸载",
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
            url = QUrl(model.get("docs_url") or model.get("source", ""))
            if url.scheme() in {"http", "https"} and url.host():
                QDesktopServices.openUrl(url)
            else:
                self.status.setText("该模型没有单独的官方说明链接")


class PunctuationRulesDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.baseline = []
        self.setWindowTitle("自动替换标点 · AiNiee 公共译后替换表")
        apply_dialog_theme(self, "mangaPunctuationDialog")
        self.resize(720, 500)
        layout = dialog_layout(self)
        note = dialog_note(
            "这里直接编辑公共表“漫画标点替换”。启用后，漫画排版和文本翻译共同使用这些规则；其他公共表不受影响。"
        )
        layout.addWidget(note)
        self.enabled = QCheckBox("启用自动替换标点")
        layout.addWidget(self.enabled)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["替换前", "替换后", "正则"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 64)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        page.button(
            buttons,
            "添加规则",
            lambda: self.add_row({"src": "", "dst": "", "regex": False}),
        )
        page.button(buttons, "删除选中规则", self.remove_rows)
        page.button(buttons, "恢复默认规则", self.restore_defaults)
        buttons.addStretch(1)
        page.button(buttons, "保存到公共表", self.save_rules)
        self.status = dialog_note("正在读取规则…")
        layout.addWidget(self.status)
        page.background(lambda: page.client.request("GET", "/punctuation"), self.loaded)

    def apply_theme(self, *_):
        self.setStyleSheet(dialog_style("mangaPunctuationDialog"))

    def add_row(self, rule):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, key in enumerate(("src", "dst")):
            self.table.setItem(row, col, QTableWidgetItem(rule.get(key, "")))
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
        check.setCheckState(Qt.Checked if rule.get("regex") else Qt.Unchecked)
        self.table.setItem(row, 2, check)

    def loaded(self, state):
        if sip.isdeleted(self) or "rows" not in state:
            return
        self.baseline = deepcopy(state["rows"])
        self.enabled.setChecked(state["enabled"])
        self.table.setRowCount(0)
        for row in state["rows"]:
            self.add_row(row)
        self.status.setText(
            "修改、删除或新增规则后点击保存；重复启用不会重复添加公共表。"
        )

    def remove_rows(self):
        for row in sorted(
            {i.row() for i in self.table.selectionModel().selectedRows()}, reverse=True
        ):
            self.table.removeRow(row)

    def restore_defaults(self):
        from Shared.manga_lettering import DEFAULT_PUNCTUATION_RULES

        self.table.setRowCount(0)
        for row in DEFAULT_PUNCTUATION_RULES:
            self.add_row(row)

    def save_rules(self):
        self.table.setCurrentCell(-1, -1)
        rows = []
        for i in range(self.table.rowCount()):
            src = self.table.item(i, 0).text() if self.table.item(i, 0) else ""
            dst = self.table.item(i, 1).text() if self.table.item(i, 1) else ""
            if src:
                rows.append(
                    {
                        "src": src,
                        "dst": dst,
                        "regex": self.table.item(i, 2).checkState() == Qt.Checked,
                    }
                )
        payload = {
            "enabled": self.enabled.isChecked(),
            "rows": rows,
            "baseline": self.baseline,
        }

        def request():
            try:
                return self.page.client.request("POST", "/punctuation", json=payload)
            except Exception as error:
                return {"error": str(error)}

        def saved(state):
            if sip.isdeleted(self):
                return
            if "error" in state:
                self.status.setText("保存失败：" + state["error"])
                return
            self.baseline = deepcopy(state["rows"])
            self.page.apply_punctuation_state(state)
            self.status.setText("已保存到公共译后替换表；编辑器已实时更新。")

        self.page.background(request, saved)


def compose_compare(source_bytes, render_bytes):
    """把原图与成品并排拼成一张对比图；成品缺失时退回原图。"""
    if not render_bytes:
        return source_bytes
    from io import BytesIO

    from PIL import Image

    left = Image.open(BytesIO(source_bytes)).convert("RGB")
    right = Image.open(BytesIO(render_bytes)).convert("RGB")
    if right.size != left.size:
        right = right.resize(left.size, Image.LANCZOS)
    gap = max(4, left.width // 200)
    canvas = Image.new("RGB", (left.width * 2 + gap, left.height), (24, 26, 30))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width + gap, 0))
    buffer = BytesIO()
    canvas.save(buffer, "PNG", compress_level=3)
    return buffer.getvalue()


class ReplaceDialog(QDialog):
    """批量正则查找替换：先预览命中，再一次性写回工程（可撤销）。"""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("批量查找替换")
        apply_dialog_theme(self, "mangaReplaceDialog")
        self.resize(720, 520)
        layout = dialog_layout(self)
        note = dialog_note(
            "按正则表达式批量改写译文或原文。先用「预览」确认命中，再执行替换；"
            "替换结果会作为一次编辑进入撤销栈。"
        )
        layout.addWidget(note)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText(r"例如：\s+ 或 ^…$")
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText("留空表示删除命中内容")
        self.field = QComboBox()
        self.field.addItem("译文", "target")
        self.field.addItem("原文（OCR）", "source")
        self.scope = QComboBox()
        self.scope.addItem("当前页", "page")
        self.scope.addItem("整部漫画", "all")
        self.scope.currentIndexChanged.connect(self.refresh_scope)
        self.ignore_case = QCheckBox("忽略大小写")
        form.addRow("查找（正则）", self.pattern)
        form.addRow("替换为", self.replacement)
        form.addRow("作用字段", self.field)
        form.addRow("范围", self.scope)
        form.addRow(self.ignore_case)
        layout.addLayout(form)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["页", "替换前", "替换后"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 150)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)
        self.status = dialog_note("输入表达式后点击「预览」")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        page.button(buttons, "预览", self.preview)
        apply_button = PrimaryPushButton("全部替换")
        apply_button.clicked.connect(self.apply)
        buttons.addWidget(apply_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh_scope()

    def refresh_scope(self):
        if self.scope.currentData() == "page":
            self.scope.setToolTip("仅替换当前页的文字框")
        else:
            self.scope.setToolTip("替换整部漫画的文字框")

    def payload(self, dry_run):
        page_ids = (
            [self.page.page_id]
            if self.scope.currentData() == "page" and self.page.page_id
            else None
        )
        return {
            "pattern": self.pattern.text(),
            "replacement": self.replacement.text(),
            "ignore_case": self.ignore_case.isChecked(),
            "field": self.field.currentData(),
            "page_ids": page_ids,
            "dry_run": dry_run,
        }

    def send(self, dry_run, callback):
        payload = self.payload(dry_run)
        if not payload["pattern"]:
            self.status.setText("请先填写查找表达式")
            return
        document = self.page.document
        if not document:
            return
        self.status.setText("正在预览…" if dry_run else "正在替换…")
        endpoint = f"/{document['id']}/replace"

        def request():
            try:
                return self.page.client.request("POST", endpoint, json=payload)
            except Exception as error:
                return {"error": str(error)}

        self.page.background(request, callback)

    def preview(self):
        self.send(True, self.preview_done)

    def preview_done(self, result):
        if sip.isdeleted(self):
            return
        if "error" in result:
            self.status.setText("预览失败：" + result["error"])
            return
        samples = result.get("samples") or []
        self.table.setRowCount(len(samples))
        for row, sample in enumerate(samples):
            self.table.setItem(row, 0, QTableWidgetItem(str(sample.get("page", ""))))
            self.table.setItem(row, 1, QTableWidgetItem(sample.get("before", "")))
            self.table.setItem(row, 2, QTableWidgetItem(sample.get("after", "")))
        changed = result.get("changed", 0)
        self.status.setText(
            f"共命中 {changed} 个文字框"
            + (f"，下表显示前 {len(samples)} 条" if changed > len(samples) else "")
            + ("。确认无误后点击「全部替换」。" if changed else "。")
        )

    def apply(self):
        if not self.page.document:
            return

        def ready():
            self.send(False, self.applied)

        self.page.save(ready)

    def applied(self, result):
        if sip.isdeleted(self):
            return
        if "error" in result:
            self.status.setText("替换失败：" + result["error"])
            return
        workspace = result.get("workspace") or {}
        changed = result.get("changed", 0)
        if not changed or not workspace.get("pages"):
            self.status.setText("没有命中需要替换的内容")
            return
        pages = deepcopy(workspace["pages"])
        self.page.edit(
            "批量替换" + ("译文" if self.field.currentData() == "target" else "原文"),
            lambda: self.page.document.__setitem__("pages", pages),
        )
        self.page.refresh_pages()
        self.page.draw_canvas()
        self.status.setText(f"已替换 {changed} 个文字框，可用 Ctrl+Z 撤销")


class CharactersDialog(QDialog):
    """角色设定：把人物名、性格与语气透传给翻译，让对白口吻保持一致。"""

    COLUMNS = ["原名", "译名", "性别", "年龄", "性格", "语气 / 措辞", "补充说明"]

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("角色设定与跨框上下文")
        apply_dialog_theme(self, "mangaCharactersDialog")
        self.resize(860, 520)
        layout = dialog_layout(self)
        note = dialog_note(
            "这些设定会随工程保存，翻译时作为「角色设定」一并提交，"
            "让同一人物在不同文字框里的语气保持一致。原名要与原文用字一致才会命中。"
        )
        layout.addWidget(note)
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.table.horizontalHeader()
        for column, mode in (
            (0, QHeaderView.Stretch),
            (1, QHeaderView.Stretch),
            (2, QHeaderView.Fixed),
            (3, QHeaderView.Fixed),
            (4, QHeaderView.Stretch),
            (5, QHeaderView.Stretch),
            (6, QHeaderView.Stretch),
        ):
            header.setSectionResizeMode(column, mode)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 70)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)
        options = QHBoxLayout()
        options.setSpacing(8)
        self.enabled = QCheckBox("翻译时带上角色设定")
        self.enabled.setToolTip("关闭后只按逐框文本翻译，不提交人物表")
        options.addWidget(self.enabled)
        options.addSpacing(12)
        options.addWidget(QLabel("跨框上下文"))
        self.neighbors = QSpinBox()
        self.neighbors.setRange(0, 5)
        self.neighbors.setToolTip(
            "每个文字框额外带上前后各 N 个框的原文，帮助模型理解对话的承接关系；0 表示不带"
        )
        self.neighbors.setFixedWidth(90)
        options.addWidget(self.neighbors)
        options.addWidget(QLabel("框"))
        options.addStretch(1)
        page.button(options, "添加一行", self.add_row)
        page.button(options, "删除选中行", self.remove_rows)
        layout.addLayout(options)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.status = dialog_note("")
        buttons.addWidget(self.status, 1)
        page.button(buttons, "取消", self.reject)
        save = PrimaryPushButton("保存设定")
        save.clicked.connect(self.save)
        save.setDefault(True)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.load()
        for column in range(len(self.COLUMNS)):
            self.table.resizeColumnToContents(column)
            self.table.setColumnWidth(column, min(max(self.table.columnWidth(column), 70), 260))

    def load(self):
        settings = (self.page.document or {}).get("settings", {})
        self.enabled.setChecked(bool(settings.get("characterization_switch", True)))
        self.neighbors.setValue(int(settings.get("context_neighbors", 1) or 0))
        self.table.setRowCount(0)
        for character in settings.get("characters", []):
            self.add_row(character)

    def add_row(self, character=None):
        character = character or {}
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, key in enumerate(
            (
                "original_name",
                "translated_name",
                "gender",
                "age",
                "personality",
                "speech_style",
                "additional_info",
            )
        ):
            self.table.setItem(row, column, QTableWidgetItem(str(character.get(key, ""))))

    def remove_rows(self):
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def rows(self):
        values = []
        for row in range(self.table.rowCount()):
            record = {}
            for column, key in enumerate(
                (
                    "original_name",
                    "translated_name",
                    "gender",
                    "age",
                    "personality",
                    "speech_style",
                    "additional_info",
                )
            ):
                item = self.table.item(row, column)
                record[key] = (item.text() if item else "").strip()
            if record["original_name"]:
                values.append(record)
        return values

    def save(self):
        page = self.page
        if not page.document:
            return
        records = self.rows()
        switch = self.enabled.isChecked()
        neighbors = self.neighbors.value()

        def action():
            settings = page.document["settings"]
            existing = {c.get("original_name"): c for c in settings.get("characters", [])}
            settings["characters"] = [
                {
                    "id": existing.get(record["original_name"], {}).get("id")
                    or uuid4().hex,
                    **record,
                }
                for record in records
            ]
            settings["characterization_switch"] = switch
            settings["context_neighbors"] = neighbors

        page.edit("更新角色设定", action)
        self.status.setText(f"已保存 {len(records)} 个角色设定")
        self.accept()


class StylePresetsDialog(QDialog):
    """样式预设：把常用字体排法存起来，一键套到本页或整部。"""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("样式预设")
        apply_dialog_theme(self, "mangaStylePresetsDialog")
        self.resize(560, 460)
        layout = dialog_layout(self)
        note = dialog_note(
            "预设只保存字体、字号、颜色与对齐等排版参数，不含文字内容。"
            "套用后会覆盖目标文字框的样式，可用 Ctrl+Z 撤销。"
        )
        layout.addWidget(note)
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["预设名称", "说明"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 160)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.status = dialog_note("")
        buttons.addWidget(self.status, 1)
        page.button(buttons, "删除选中", self.remove_current)
        page.button(buttons, "应用到本页", lambda: self.apply(False))
        page.button(buttons, "应用到整部", lambda: self.apply(True))
        page.button(buttons, "关闭", self.reject)
        layout.addLayout(buttons)
        self.load()

    def load(self):
        presets = (self.page.document or {}).get("settings", {}).get("style_presets") or []
        self.table.setRowCount(0)
        for preset in presets:
            style = preset.get("style") or {}
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(str(preset.get("name", "")))
            name.setData(Qt.UserRole, preset.get("id"))
            self.table.setItem(row, 0, name)
            detail = (
                f"{style.get('size', '')} px · "
                f"{'竖排' if style.get('vertical') else '横排'} · "
                f"{'粗体' if style.get('bold') else '常规'} · "
                f"{style.get('font') or '默认字体'}"
            )
            self.table.setItem(row, 1, QTableWidgetItem(detail))
        if presets:
            self.table.selectRow(0)
        self.status.setText(f"共 {len(presets)} 个预设")

    def current(self):
        rows = [index.row() for index in self.table.selectionModel().selectedRows()]
        if not rows:
            return None
        presets = (self.page.document or {}).get("settings", {}).get("style_presets") or []
        row = rows[0]
        name_item = self.table.item(row, 0)
        wanted = name_item.data(Qt.UserRole) if name_item else None
        return next((p for p in presets if p.get("id") == wanted), None)

    def remove_current(self):
        preset = self.current()
        if not preset:
            self.status.setText("请先选中一个预设")
            return
        identifier = preset.get("id")

        def action():
            settings = self.page.document["settings"]
            settings["style_presets"] = [
                p for p in settings.get("style_presets") or [] if p.get("id") != identifier
            ]

        self.page.edit("删除样式预设", action)
        self.load()

    def apply(self, all_pages):
        preset = self.current()
        if not preset:
            self.status.setText("请先选中一个预设")
            return
        style = deepcopy(preset.get("style") or {})

        def action():
            settings = self.page.document["settings"]
            settings["default_style"] = deepcopy(style)
            for page in self.page.document["pages"] if all_pages else [self.page.page()]:
                page["default_style"] = deepcopy(style)
                for region in page["regions"]:
                    region["style"] = deepcopy(style)

        label = "整部" if all_pages else "当前页"
        self.page.edit(f"套用样式预设到{label}", action)
        self.status.setText(f"已套用到{label}「{preset.get('name', '')}」")
        self.page.draw_canvas()


IMAGE_FILTER = (
    "图片与 PDF (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.pdf);;所有文件 (*)"
)


def pick_image_paths(parent):
    """系统原生多选对话框：一次选多张图片 / PDF。文件夹走独立入口。"""
    paths, _ = QFileDialog.getOpenFileNames(
        parent, "导入图片 / PDF", "", IMAGE_FILTER
    )
    return [
        str(Path(p).resolve()) for p in paths if p and Path(p).exists()
    ]


class ImportDialog(QDialog):
    """导入弹窗：项目名称 + 原文语言 + 目标语言。"""

    LANGUAGE_LABELS = [
        ("日语", "ja"),
        ("中文", "zh-CN"),
        ("英语", "en"),
        ("韩语", "ko"),
    ]

    def __init__(self, parent=None, count=0, default_name=""):
        super().__init__(parent)
        self.setWindowTitle("导入漫画")
        apply_dialog_theme(self, "mangaImportDialog")
        self.resize(440, 300)
        layout = dialog_layout(self)
        layout.addWidget(dialog_note(f"已选择 {count} 个文件，请确认项目信息"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setText(default_name)
        self.name_edit.selectAll()
        self.source_language = QComboBox()
        self.target_language = QComboBox()
        for title, value in self.LANGUAGE_LABELS:
            self.source_language.addItem(title, value)
        for title, value in self.LANGUAGE_LABELS:
            self.target_language.addItem(title, value)
        self.target_language.setCurrentIndex(1)
        form.addRow("项目名称", self.name_edit)
        form.addRow("原文语言", self.source_language)
        form.addRow("目标语言", self.target_language)
        layout.addLayout(form)
        layout.addStretch(1)
        self.append = QCheckBox("追加到当前工程", self)
        self.append.setVisible(bool(getattr(parent, "document", None)))
        layout.addWidget(self.append)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = PrimaryPushButton("开始导入")
        ok.clicked.connect(self.accept)
        ok.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)


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
        # 用户手动缩放后切页/切视图应保持同一比例，而不是每次都跳回"适应窗口"
        self._zoom_percent = 0
        self.source_cache = {}
        self.clean_cache = {}
        self.clean_signature = None
        self.epoch = 0
        self.saving = False
        self.job_id = None
        self.job_manual_paths = set()
        self.loading_form = False
        self.pending_edits = []
        self._canvas_cache_key = None
        self._canvas_cache_data = None
        self._punctuation_loaded = False
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
        self._init_timer = QTimer(self)
        self._init_timer.setSingleShot(True)
        self._init_timer.setInterval(0)
        self._init_timer.timeout.connect(self._run_deferred_init)
        self._init_timer.start()

    def _run_deferred_init(self):
        if self.closed or sip.isdeleted(self):
            return
        self.refresh_history()
        self.refresh_providers()
        self.refresh_fonts()
        self.refresh_model_catalogue()

    def apply_theme(self, *_):
        if sip.isdeleted(self):
            return
        self.setAttribute(Qt.WA_StyledBackground, True)
        dark = isDarkTheme()
        background = "#202020" if dark else "#f7f9fc"
        overlay_bg = "rgba(32, 32, 32, 240)" if dark else "rgba(255, 255, 255, 244)"
        overlay_border = (
            "rgba(255, 255, 255, 0.14)" if dark else "rgba(0, 0, 0, 0.10)"
        )
        overlay_text = "#f3f3f3" if dark else "#1f1f1f"
        self.setStyleSheet(
            f"QWidget#manga_translation_page {{background:{background};}}"
            f"SimpleCardWidget#mangaZoomOverlay, SimpleCardWidget#mangaPanHint {{"
            f"background-color:{overlay_bg};border:1px solid {overlay_border};border-radius:10px;}}"
            f"SimpleCardWidget#mangaZoomOverlay QLabel, SimpleCardWidget#mangaPanHint QLabel {{"
            f"color:{overlay_text};background:transparent;}}"
        )
        palette = self.palette()
        palette.setColor(
            QPalette.WindowText, QColor("#ffffff" if dark else "#202020")
        )
        self.setPalette(palette)
        if hasattr(self, "progress_panel") and not sip.isdeleted(self.progress_panel):
            self.progress_panel.apply_theme()

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
            {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}
        )
        self.start_page.drop_card._title = "将图片、PDF 或文件夹拖到这里"
        self.start_page.drop_card.title_label.setText(self.start_page.drop_card._title)
        self.start_page.pick_files.setText("导入图片 / PDF / 文件夹")
        self.start_page.pick_folder.hide()
        for caption in self.start_page.drop_card.findChildren(QLabel):
            if "PNG" in caption.text():
                caption.setText("PNG · JPG · WebP · PDF · 文件夹")
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

        # ---- 共享控件（先创建，再布局）----
        self.history = QComboBox()
        self.history.hide()
        self.scope = QComboBox()
        self.scope.addItems(["当前页", "全部页面", "选中文字框"])
        self.scope.setCurrentIndex(1)
        self.scope.hide()
        self.detector = QComboBox()
        for model_id in ("manga-panel", "japanese-lines"):
            self.detector.addItem(model_id, model_id)
        self.ocr = QComboBox()
        for model_id in ("mangaocr", "rapidocr"):
            self.ocr.addItem(model_id, model_id)
        self.detector.currentIndexChanged.connect(self.pick_detector)
        self.ocr.currentIndexChanged.connect(
            lambda: not self.loading_form and self.set_setting("ocr", self.ocr.currentData())
        )
        self.model_catalogue = []
        self.source_language = QComboBox()
        for title, code in LANGUAGE_CHOICES:
            self.source_language.addItem(title, code)
        self.target_language = QComboBox()
        for title, code in LANGUAGE_CHOICES:
            self.target_language.addItem(title, code)
        # 默认日语 → 简中，与导入弹窗保持一致
        self.source_language.setCurrentIndex(
            max(0, self.source_language.findData("ja"))
        )
        self.target_language.setCurrentIndex(
            max(0, self.target_language.findData("zh-CN"))
        )
        self.source_language.currentIndexChanged.connect(
            lambda: self.set_setting("source_language", self.source_language.currentData())
        )
        self.target_language.currentIndexChanged.connect(
            lambda: self.set_setting("target_language", self.target_language.currentData())
        )
        self.panels = QCheckBox("检测分镜并组织阅读顺序")
        self.panels.setChecked(True)
        self.panels.toggled.connect(lambda v: self.set_setting("detect_panels", v))
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.05, 1)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(0.25)
        self.confidence.valueChanged.connect(
            lambda v: self.set_setting("confidence", v)
        )
        self.rtl = QCheckBox("从右向左阅读")
        self.rtl.setChecked(True)
        self.rtl.toggled.connect(lambda v: self.set_setting("right_to_left", v))
        self.auto_recognize = QCheckBox("导入后自动检测与 OCR")
        self.auto_recognize.setChecked(False)
        self.auto_recognize.toggled.connect(
            lambda v: self.set_setting("auto_recognize", v)
        )
        self.provider = QComboBox()
        self.provider.addItem("使用文本翻译当前接口", None)
        self.provider.currentIndexChanged.connect(
            lambda: self.set_setting("provider_id", self.provider.currentData())
        )
        self.project_dialog = None
        self.precise = QCheckBox("精准去字")
        self.precise.setToolTip(
            "整个工程统一：勾选使用 LaMa 修复背景，取消使用色块遮挡；原图始终保留"
        )
        self.precise.toggled.connect(self.toggle_precise)
        self.variant = QComboBox()
        for title, value in [
            ("实时编辑", "edit"),
            ("预览成品", "render"),
            ("前后对比", "compare"),
            ("查看原图", "source"),
        ]:
            self.variant.addItem(title, value)
        self.variant.setCurrentIndex(self.variant.findData("source"))
        self.variant.currentIndexChanged.connect(self.change_view)
        self.source = QPlainTextEdit()
        self.source.setMaximumHeight(88)
        self.target = QPlainTextEdit()
        self.target.setMaximumHeight(88)
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
        self.source_lock.setToolTip(
            "锁定后，整部识别 / OCR 与重做识别会跳过此框，保留此处原文"
        )
        self.target_lock = QCheckBox("锁定译文")
        self.target_lock.setToolTip(
            "锁定后，整部翻译与重做翻译会跳过此框，保留此处译文"
        )
        self.source_lock.toggled.connect(
            lambda v: self.region_value("source_locked", v)
        )
        self.target_lock.toggled.connect(
            lambda v: self.region_value("target_locked", v)
        )
        self.font = QComboBox()
        self.font.addItem("内置字体", "")
        self.font.currentIndexChanged.connect(
            lambda: self.style_value("font", self.font.currentData() or "")
        )
        self.size = QSpinBox()
        self.size.setRange(6, 512)
        self.size.setValue(28)
        self.size.setSuffix(" px")
        self.size.setFixedWidth(140)
        self.size.setToolTip(
            "固定字号模式下生效；自动适配模式下字号随框自动调整"
        )
        self.size.valueChanged.connect(lambda v: self.style_value("size", v))
        self.minimum = QSpinBox()
        self.minimum.setRange(6, 512)
        self.minimum.setValue(10)
        self.minimum.valueChanged.connect(lambda v: self.style_value("min_size", v))
        # 字号模式：自动适配 / 固定字号（互斥按钮）
        self.autofit = TransparentPushButton("自动适配", self.work_page)
        self.autofit.setCheckable(True)
        self.autofit.setToolTip("字号随框自动放大缩小")
        self.autofit.toggled.connect(
            lambda checked: checked and self.style_value("autofit", True)
        )
        self.autofit.toggled.connect(self.refresh_font_mode_hint)
        self.fixed_size_button = TransparentPushButton("固定字号", self.work_page)
        self.fixed_size_button.setCheckable(True)
        self.fixed_size_button.setToolTip("手动指定字号；字号输入框中修改")
        self.fixed_size_button.toggled.connect(
            lambda checked: checked and self.style_value("autofit", False)
        )
        self.fixed_size_button.toggled.connect(self.refresh_font_mode_hint)
        self.horizontal_button = TransparentPushButton("横", self.work_page)
        self.horizontal_button.setCheckable(True)
        self.horizontal_button.setToolTip("横排文字")
        self.horizontal_button.clicked.connect(
            lambda: self.style_value("vertical", False)
        )
        self.vertical_button = TransparentPushButton("竖", self.work_page)
        self.vertical_button.setCheckable(True)
        self.vertical_button.setToolTip("竖排文字")
        self.vertical_button.clicked.connect(lambda: self.style_value("vertical", True))
        self.bold_button = TransparentPushButton("B", self.work_page)
        self.bold_button.setCheckable(True)
        self.bold_button.setToolTip("粗体")
        self.bold_button.clicked.connect(
            lambda checked: self.style_value("bold", checked)
        )
        self.color_button = TransparentToolButton(FluentIcon.PALETTE, self.work_page)
        self.color_button.setToolTip("文字颜色")
        self.color_button.clicked.connect(lambda: self.pick_color("color"))
        self.vertical_rtl = QCheckBox("竖排从右往左", self.work_page)
        self.vertical_rtl.setChecked(True)
        self.vertical_rtl.setToolTip(
            "竖排阅读方向：日式漫画默认从右往左；取消勾选则从左往右"
        )
        self.vertical_rtl.toggled.connect(
            lambda v: self.style_value("vertical_rtl", v)
        )
        self.vertical_rtl.hide()
        self.align = QComboBox()
        for title, value in [("左对齐", "left"), ("居中", "center"), ("右对齐", "right")]:
            self.align.addItem(title, value)
        self.align.currentIndexChanged.connect(
            lambda: self.style_value("align", self.align.currentData())
        )
        self.spacing = QDoubleSpinBox()
        self.spacing.setRange(0.5, 4)
        self.spacing.setValue(1.2)
        self.spacing.setSingleStep(0.1)
        self.spacing.valueChanged.connect(
            lambda v: self.style_value("line_spacing", v)
        )
        self.charspace = QSpinBox()
        self.charspace.setRange(-10, 50)
        self.charspace.valueChanged.connect(
            lambda v: self.style_value("char_spacing", v)
        )
        self.stroke = QSpinBox()
        self.stroke.setRange(0, 30)
        self.stroke.valueChanged.connect(lambda v: self.style_value("stroke_width", v))
        self.rotation = QDoubleSpinBox()
        self.rotation.setRange(-180, 180)
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
        self.regions = QTableWidget()
        self.regions.setColumnCount(3)
        self.regions.setHorizontalHeaderLabels(["序号", "内容", "状态"])
        self.regions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.regions.setMinimumHeight(110)
        self.regions.setMaximumHeight(260)
        self.regions.setWordWrap(True)
        self.regions.setSelectionBehavior(QTableWidget.SelectRows)
        self.regions.setColumnWidth(0, 48)
        self.regions.setColumnWidth(2, 76)
        self.regions.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.regions.verticalHeader().setVisible(False)
        self.regions.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.regions.cellChanged.connect(self.review_changed)
        self.regions.currentCellChanged.connect(
            lambda row, col, old_row, old_col: self.region_row(row)
        )
        self.font_mode_hint = QLabel("选择文字框后显示实际字号")
        self.font_mode_hint.setWordWrap(True)

        # ---- 顶栏：返回 + 标题 + 项目摘要（点击修改模型与语言）----
        heading = QHBoxLayout()
        back = TransparentPushButton(FluentIcon.RETURN, "返回", self.work_page)
        back.clicked.connect(self.back_to_start)
        heading.addWidget(back)
        heading.addWidget(SubtitleLabel("漫画翻译", self.work_page))
        heading.addSpacing(16)
        self.project_summary_button = TransparentPushButton(
            FluentIcon.SETTING, "项目设置", self.work_page
        )
        self.project_summary_button.setToolTip("检测模型、语言、阈值与翻译接口")
        self.project_summary_button.clicked.connect(self.open_project_settings)
        heading.addWidget(self.project_summary_button)
        for label, combo in (("检测模型", self.detector), ("OCR 模型", self.ocr)):
            heading.addWidget(QLabel(label, self.work_page))
            combo.setAccessibleName(label)
            combo.setToolTip(f"为当前工程选择{label}")
            combo.setMinimumWidth(105)
            combo.setMaximumWidth(220)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            heading.addWidget(combo, 1)
        heading.addStretch(1)
        root.addLayout(heading)
        self.sync_project_summary()

        # ---- 命令栏：当前页入口在菜单，整部入口在右上角 ----
        command_card = SimpleCardWidget(self.work_page)
        commands = QHBoxLayout(command_card)
        commands.setContentsMargins(10, 8, 10, 8)
        commands.setSpacing(6)
        root.addWidget(command_card)
        self.menu_button(
            commands,
            "文件",
            [
                ("导入图片 / PDF…", self.import_images),
                ("导入文件夹…", lambda: self.import_images(folder=True)),
                ("打开工程包…", self.import_project),
                ("保存工程  Ctrl+S", self.save),
                ("导出成品…", self.export),
            ],
        )
        self.menu_button(
            commands,
            "识别",
            [
                ("检测当前页文字框", lambda: self.ask_detection(0)),
                ("读取当前页原文（OCR）", lambda: self.ask_ocr(0)),
            ],
        )
        translate_menu = self.menu_button(
            commands,
            "翻译",
            [
                ("翻译当前页", lambda: self.run("translate", scope=0)),
                ("角色设定与跨框上下文…", self.open_characters),
                (
                    "术语表（公共）",
                    lambda: self.open_public_table("prompt_dictionary_page"),
                ),
                (
                    "禁翻表（公共）",
                    lambda: self.open_public_table("exclusion_list_page"),
                ),
                (
                    "文本替换（公共）",
                    lambda: self.open_public_table("text_replace_b_page"),
                ),
            ],
        )
        self.menu_button(
            commands,
            "编辑",
            [
                ("撤销", self.undo.undo),
                ("重做", self.undo.redo),
                ("批量查找替换…", self.open_replace),
                ("清除译文…", lambda: self.clear_content("target")),
                ("清除 OCR 原文…", lambda: self.clear_content("source")),
                ("删除文字框…", lambda: self.clear_content("regions")),
            ],
        )
        layout_button = self.menu_button(
            commands,
            "排版",
            [
                ("设为新框默认", self.set_default_style),
                ("保存当前样式为预设…", self.save_style_preset),
                ("样式预设…", self.open_style_presets),
                ("应用到本页已有框", lambda: self.apply_style(False)),
                ("应用到整部已有框", lambda: self.apply_style(True)),
                ("添加字体文件…", self.pick_font),
                ("重新生成当前页精准去字底图", self.regenerate_clean_background),
            ],
        )
        self.menu_button(
            commands,
            "更多",
            [
                ("项目设置…", self.open_project_settings),
                (
                "模型中心…",
                lambda: (ModelDialog(self).exec(), self.refresh_model_catalogue()),
            ),
                ("导出原文 JSON…", lambda: self.export_translation(True)),
                ("导入译文 JSON…", self.import_translation),
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
        one_click = PrimarySplitPushButton("一键翻译", command_card)
        one_menu = RoundMenu(parent=one_click)
        for title, action in [
            ("检测整部文字框", lambda: self.ask_detection(1)),
            ("读取整部原文（OCR）", lambda: self.ask_ocr(1)),
        ]:
            one_menu.addAction(
                Action(
                    title, triggered=lambda checked=False, callback=action: callback()
                )
            )
        one_click.setFlyout(one_menu)
        one_click.clicked.connect(self.one_click_translation)
        one_click.setToolTip("补齐检测与 OCR 后翻译整部")
        commands.addWidget(one_click)
        self.punctuation_action = Action("自动替换标点", self)
        self.punctuation_action.setCheckable(True)
        self.punctuation_action.setToolTip(
            "仅调整排版显示，不修改译文内容：把 …、︙、⋮ 替换为 ...，并规范竖排线条；开启或关闭会立即更新，预览成品与导出一致。"
        )
        self.punctuation_action.toggled.connect(self.set_auto_punctuation)
        layout_button.task_menu.addAction(self.punctuation_action)
        layout_button.task_menu.addAction(
            Action(
                "查看标点替换规则",
                triggered=lambda: PunctuationRulesDialog(self).exec(),
            )
        )
        for sequence, callback in [
            ("Ctrl+S", self.save),
            ("Ctrl+Z", self.undo.undo),
            ("Ctrl+Y", self.undo.redo),
            ("Ctrl+Shift+Z", self.undo.redo),
            ("PageUp", lambda: self.step_page(-1)),
            ("PageDown", lambda: self.step_page(1)),
            ("Ctrl+0", self.fit_canvas),
            ("Ctrl+=", lambda: self.step_zoom(1.15)),
            ("Ctrl++", lambda: self.step_zoom(1.15)),
            ("Ctrl+-", lambda: self.step_zoom(1 / 1.15)),
        ]:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)

        # ---- 三栏布局 ----
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        root.addWidget(split, 1)
        # 左：页面列表（收窄成页码，悬停看文件名）
        left = SimpleCardWidget()
        left.setBorderRadius(12)
        left.setMinimumWidth(64)
        left.setMaximumWidth(270)
        left_layout = QVBoxLayout(left)
        self.page_title = StrongBodyLabel("页面", left)
        left_layout.addWidget(self.page_title)
        self.pages = QListWidget()
        self.pages.setMinimumWidth(0)
        self.pages.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pages.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        left_layout.addWidget(self.pages)
        self.page_bar_widget = QWidget()
        self.page_bar_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        page_bar = QHBoxLayout(self.page_bar_widget)
        page_bar.setContentsMargins(0, 0, 0, 0)
        self.button(page_bar, "↑", lambda: self.move_page(-1))
        self.button(page_bar, "↓", lambda: self.move_page(1))
        remove_page_button = TransparentToolButton(FluentIcon.DELETE, left)
        remove_page_button.setToolTip("移除当前页")
        remove_page_button.setAccessibleName("移除当前页")
        remove_page_button.clicked.connect(self.delete_page)
        page_bar.addWidget(remove_page_button)
        left_layout.addWidget(self.page_bar_widget)
        split.addWidget(left)
        self.pages.currentRowChanged.connect(self.select_page)
        self.page_panel = left
        self.page_panel.installEventFilter(self)
        split.splitterMoved.connect(self.update_page_labels)
        # 中：画布 + 区域编辑横幅
        center = SimpleCardWidget()
        center.setBorderRadius(12)
        center_layout = QVBoxLayout(center)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        center_layout.addLayout(tools)
        self.tool = QComboBox()
        self.tool.addItems(["选择/移动", "建立OCR框", "蒙版画笔", "蒙版橡皮"])
        self.tool.hide()
        self.tool.currentIndexChanged.connect(self.change_tool)
        self.tool_buttons = []
        for index, icon, label in [
            (0, FluentIcon.MOVE, "选择 / 移动 (V)"),
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
            if index == 1:
                self.icon_button(
                    tools,
                    FluentIcon.DELETE,
                    "删除选中文字框 (Delete)",
                    self.delete_regions,
                )
        self.tool_divider = self.divider(tools)
        self.geometry = QComboBox()
        for title, key in [
            ("译文排版框", "layout_rect"),
            ("OCR 取字范围", "ocr_rect"),
            ("去字背景范围", "erase_rect"),
        ]:
            self.geometry.addItem(title, key)
        self.geometry.hide()
        self.geometry.currentIndexChanged.connect(self.change_geometry)
        self.geometry_hint = QLabel("拖框移动 · 滚轮缩放 · 右键更多")
        self.geometry_hint.setWordWrap(False)
        self.geometry_hint.setMinimumWidth(120)
        self.geometry_hint.setMaximumHeight(32)
        self.geometry_hint.setTextInteractionFlags(Qt.NoTextInteraction)
        self.geometry_hint.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Preferred
        )
        tools.addWidget(self.geometry_hint, 1)
        self.geometry_done = TransparentToolButton(FluentIcon.ACCEPT_MEDIUM, center)
        self.geometry_done.setToolTip("完成并返回译文排版 (Esc)")
        self.geometry_done.hide()
        self.geometry_done.clicked.connect(self.exit_geometry_mode)
        tools.addWidget(self.geometry_done)
        self.precise_divider = self.divider(tools)
        self.tool_recognize = TransparentToolButton(FluentIcon.SEARCH, center)
        self.tool_recognize.setAccessibleName("识别")
        self.tool_recognize.setToolTip(
            "识别：无框时检测建框；有框时 OCR 当前页或选中的框。OCR 会更新未锁定的原文。"
        )
        self.tool_recognize.clicked.connect(self.recognize_here)
        tools.addWidget(self.tool_recognize)
        tools.addWidget(self.precise)
        self.brush = QSpinBox()
        self.brush.setRange(1, 256)
        self.brush.setFixedWidth(140)
        self.brush.setToolTip("蒙版画笔半径")
        self.brush.setValue(8)
        self.brush.hide()
        self.brush.valueChanged.connect(lambda v: setattr(self.canvas, "radius", v))
        tools.addWidget(self.brush)
        self.follow_banner = SimpleCardWidget()
        follow_row = QHBoxLayout(self.follow_banner)
        follow_row.addWidget(QLabel("已暂停自动跳页，可自由浏览"), 1)
        self.button(follow_row, "恢复跟随", self.resume_follow)
        center_layout.addWidget(self.follow_banner)
        self.follow_banner.hide()
        self.canvas = MangaCanvas()
        center_layout.addWidget(self.canvas, 1)
        self.canvas.viewport().installEventFilter(self)
        self.canvas.layoutMeasured.connect(self.show_layout_size, Qt.QueuedConnection)
        split.addWidget(center)
        self.canvas.regionSelected.connect(self.select_region)
        self.canvas.geometryChanged.connect(self.geometry_changed, Qt.QueuedConnection)
        self.canvas.boxCreated.connect(self.add_region)
        self.canvas.strokeCreated.connect(self.add_stroke)
        self.canvas.deleteRequested.connect(self.delete_regions)
        self.canvas.contextRequested.connect(self.region_context_menu)
        self.canvas.modeRequested.connect(self.tool.setCurrentIndex)
        self.canvas.geometryEscapeRequested.connect(self.exit_geometry_mode)
        self.canvas.escapeRequested.connect(self.clear_selection)
        self.canvas.cycleRegionRequested.connect(self.cycle_region)
        # 画布右下角统一：视图切换 + 适应窗口 + 缩放
        self.zoom_overlay = SimpleCardWidget(self.canvas.viewport())
        self.zoom_overlay.setObjectName("mangaZoomOverlay")
        self.zoom_overlay.setAttribute(Qt.WA_StyledBackground, True)
        zoom_row = QHBoxLayout(self.zoom_overlay)
        zoom_row.setContentsMargins(8, 4, 8, 4)
        zoom_row.setSpacing(4)
        zoom_row.addWidget(self.variant)
        self.icon_button(zoom_row, FluentIcon.FIT_PAGE, "适应窗口", self.fit_canvas)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(self.ZOOM_MIN, self.ZOOM_MAX)
        self.zoom_slider.setFixedWidth(72)
        self.zoom_slider.setValue(100)
        self.zoom_label = QLabel("100%")
        self.zoom_slider.valueChanged.connect(self.set_zoom)
        self.canvas.zoomChanged.connect(self.update_zoom)
        zoom_row.addWidget(self.zoom_slider)
        zoom_row.addWidget(self.zoom_label)
        self.zoom_overlay.adjustSize()
        self.pan_hint = SimpleCardWidget(self.canvas.viewport())
        self.pan_hint.setObjectName("mangaPanHint")
        self.pan_hint.setAttribute(Qt.WA_StyledBackground, True)
        pan_row = QHBoxLayout(self.pan_hint)
        pan_row.setContentsMargins(10, 6, 10, 6)
        self.pan_hint_label = QLabel("按住空格键或鼠标中键拖动画面")
        pan_row.addWidget(self.pan_hint_label)
        self.pan_hint.adjustSize()
        self.pan_hint.hide()
        self.canvas.panStarted.connect(self.on_pan_started)
        self.canvas.panFinished.connect(self.on_pan_finished)
        # ---- 右栏：文字 / 排版 / 阅读顺序（滚动，阅读顺序默认折叠）----
        # 用原生 QScrollArea：qfluentwidgets 的 SingleDirectionScrollArea
        # 强制横向 AlwaysOff，右栏变窄时内容会被裁掉而不是出现横向滚动条。
        scroll = NativeScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)
        scroll.setMaximumWidth(560)
        scroll.setWidgetResizable(True)
        # 只给 QScrollArea 本身设 background:transparent 不够：viewport 是它的
        # 子 QWidget，会按应用调色板的 Base（永远是白色）自绘底色，暗色主题下
        # 整条右栏就变成白底，上面白色的标签全看不见。子选择器要把 viewport
        # 和卡片一起改成透明，露出页面的主题底色。
        scroll.setStyleSheet(
            "QScrollArea {border:none;background:transparent;}"
            "QScrollArea > QWidget > QWidget {background:transparent;}"
        )
        right_card = SimpleCardWidget()
        right_card.setBorderRadius(12)
        form = QFormLayout(right_card)
        form.setContentsMargins(14, 14, 14, 14)
        form.setVerticalSpacing(9)
        # ==== 文字 ====
        text_header = QHBoxLayout()
        text_header.setContentsMargins(0, 8, 0, 2)
        text_header.addWidget(StrongBodyLabel("文字", right_card))
        text_header.addStretch(1)
        form.addRow(text_header)
        for caption, field, lock in (
            ("原文", self.source, self.source_lock),
            ("译文", self.target, self.target_lock),
        ):
            label = QLabel(caption, right_card)
            # 多行输入框比标签高得多，标签要对齐到首行文字而不是整行居中
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            label.setContentsMargins(0, 4, 0, 0)
            text_row = QVBoxLayout()
            text_row.setSpacing(4)
            text_row.addWidget(field)
            text_row.addWidget(lock)
            form.addRow(label, text_row)
        # ==== 排版 ====
        typography_header = QHBoxLayout()
        typography_header.setContentsMargins(0, 12, 0, 2)
        typography_header.addWidget(StrongBodyLabel("排版", right_card))
        typography_header.addStretch(1)
        form.addRow(typography_header)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        mode_row.addWidget(self.autofit)
        mode_row.addWidget(self.fixed_size_button)
        mode_row.addStretch(1)
        form.addRow(mode_row)
        font_row = QHBoxLayout()
        font_row.setSpacing(6)
        self.font.setMinimumWidth(120)
        font_row.addWidget(self.font, 1)
        font_row.addWidget(self.size)
        form.addRow(font_row)
        form.addRow(self.font_mode_hint)
        common_row = QHBoxLayout()
        common_row.setSpacing(4)
        common_row.addWidget(self.horizontal_button)
        common_row.addWidget(self.vertical_button)
        common_row.addWidget(self.bold_button)
        common_row.addWidget(self.color_button)
        self.align.setMinimumWidth(96)
        common_row.addWidget(self.align, 1)
        form.addRow(common_row)
        form.addRow(self.vertical_rtl)
        style_actions = QHBoxLayout()
        style_actions.setSpacing(6)
        self.button(style_actions, "设为新框默认", self.set_default_style)
        self.button(style_actions, "应用到本页", lambda: self.apply_style(False))
        form.addRow(style_actions)
        advanced = QWidget(right_card)
        advanced_form = QFormLayout(advanced)
        advanced_form.setContentsMargins(0, 4, 0, 4)
        advanced_form.setHorizontalSpacing(12)
        # 子表单沿用主表单的标签列宽，否则字段会整体右移 68px，
        # 展开时看起来像「两列没对齐」。
        LABEL_W = 84
        for caption, field in (
            ("建议字号下限", self.minimum),
            ("行距", self.spacing),
            ("字距", self.charspace),
            ("描边宽度", self.stroke),
            ("旋转角度", self.rotation),
        ):
            label = QLabel(caption, advanced)
            label.setFixedWidth(LABEL_W)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            advanced_form.addRow(label, field)
        stroke_label = QLabel("描边颜色", advanced)
        stroke_label.setFixedWidth(LABEL_W)
        stroke_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        stroke_button = TransparentPushButton("选择颜色…", advanced)
        stroke_button.clicked.connect(lambda: self.pick_color("stroke_color"))
        advanced_form.addRow(stroke_label, stroke_button)
        fill_row = QHBoxLayout()
        self.button(
            fill_row, "指定遮挡颜色", lambda: self.pick_color("fill_color", False)
        )
        self.button(
            fill_row, "自动取背景色", lambda: self.region_value("fill_color", "auto")
        )
        advanced_form.addRow(fill_row)
        advanced.setVisible(False)
        advanced_toggle = QPushButton("高级排版选项")
        advanced_toggle.setCheckable(True)
        advanced_toggle.toggled.connect(advanced.setVisible)
        form.addRow(advanced_toggle)
        form.addRow(advanced)
        # ==== 阅读顺序（默认折叠；点行定位到框）====
        reading_detail = QWidget(right_card)
        reading_layout = QVBoxLayout(reading_detail)
        reading_layout.setContentsMargins(0, 6, 0, 0)
        reading_layout.setSpacing(6)
        reading_layout.addWidget(self.regions)
        reorder = QHBoxLayout()
        self.button(reorder, "上移", lambda: self.move_region(-1))
        self.button(reorder, "下移", lambda: self.move_region(1))
        self.button(reorder, "合并", self.merge_regions)
        self.button(reorder, "拆分", self.split_region)
        self.fix_overflow_button = self.button(
            reorder, "修复溢出", self.fix_overflow
        )
        self.fix_overflow_button.setToolTip(
            "把字号过小或超出文字框的译文改为自动适应，并把排版框扩大 15%"
        )
        reading_layout.addLayout(reorder)
        reading_toggle_row = QHBoxLayout()
        reading_toggle_row.addWidget(StrongBodyLabel("阅读顺序", right_card))
        self.reading_toggle = TransparentToolButton(
            FluentIcon.CARE_RIGHT_SOLID, right_card
        )
        self.reading_toggle.setToolTip("展开 / 折叠本页文字框列表")
        reading_toggle_row.addWidget(self.reading_toggle)
        self.reading_count_label = QLabel("", right_card)
        self.reading_count_label.setObjectName("mangaReadingCount")
        reading_toggle_row.addWidget(self.reading_count_label)
        reading_toggle_row.addStretch(1)
        self.reading_panel = reading_detail
        reading_detail.hide()
        self.reading_toggle.clicked.connect(
            lambda: self.reading_panel.setVisible(self.reading_panel.isHidden())
        )
        form.addRow(reading_toggle_row)
        form.addRow(reading_detail)
        self.reading_toggle_row = reading_toggle_row
        right_card.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(right_card)
        right_holder = QWidget()
        right_holder_layout = QVBoxLayout(right_holder)
        right_holder_layout.setContentsMargins(0, 0, 0, 0)
        right_holder_layout.addWidget(scroll)
        right_holder.setMinimumWidth(360)
        right_holder.setMaximumWidth(560)
        split.addWidget(right_holder)
        # QFormLayout 会把字符串标签建成原生 QLabel，深色主题下前景是纯黑，
        # 落在深色卡面上完全看不清。主表单和「高级排版选项」子表单都要换。
        for target_form, owner in ((form, right_card), (advanced_form, advanced)):
            for row in range(target_form.rowCount()):
                item = target_form.itemAt(row, QFormLayout.LabelRole)
                label = item.widget() if item else None
                if isinstance(label, NativeLabel) and not isinstance(label, QLabel):
                    replacement = QLabel(label.text(), owner)
                    target_form.removeWidget(label)
                    label.hide()
                    label.deleteLater()
                    target_form.setWidget(
                        row, QFormLayout.LabelRole, replacement
                    )
        # 中栏是唯一弹性列：左右给固定意图，剩余全部给画布
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([180, 1, 380])
        # ==== 任务进度面板（画布右下角浮动，统一承载进度与模型/OCR通知）====
        self.progress_panel = MangaProgressOverlay(self.canvas.viewport())
        self.cancel_button = self.progress_panel.cancel_button
        self.cancel_button.clicked.connect(self.cancel_job)
        self.progress_label = self.progress_panel.progress_label
        self.progress_bar = self.progress_panel.progress_bar
        self.progress_panel.detailsRequested.connect(self.show_task_details)
        self.progress_panel.layoutChanged.connect(self.position_progress_panel)
        self.last_report = {}
        self.failed_region_ids = []
        # 状态栏属于工作页：挂到 outer 上的话，起始页底部也会显示
        # 「导入图片…」和「任务明细」，与实际内容不符。
        self.status = QLabel("导入图片、PDF 或文件夹后开始编辑；识别与翻译在顶栏。")
        self.status.setWordWrap(True)
        self.status_dot = QLabel(self.work_page)
        self.status_dot.setFixedSize(8, 8)
        self.set_status_tone("idle")
        log_row = QHBoxLayout()
        log_row.setContentsMargins(2, 4, 2, 0)
        log_row.setSpacing(8)
        log_row.addWidget(self.status_dot, 0, Qt.AlignTop)
        log_row.addWidget(self.status, 1)
        details = TransparentToolButton(FluentIcon.INFO, self.work_page)
        details.setToolTip("任务明细")
        details.clicked.connect(self.show_task_details)
        log_row.addWidget(details)
        root.addLayout(log_row)


    STATUS_TONES = {
        "idle": "#8a8a8a",
        "busy": "#0f6cbd",
        "ok": "#0f7b0f",
        "warn": "#9d5d00",
        "error": "#c42b1c",
    }

    def set_status_tone(self, tone):
        """状态灯：让用户一眼看出上一步是成功、部分成功还是失败。"""
        color = self.STATUS_TONES.get(tone, self.STATUS_TONES["idle"])
        dot = getattr(self, "status_dot", None)
        if dot is not None:
            dot.setStyleSheet(
                f"background-color:{color};border-radius:4px;"
            )

    def pending_counts(self, scope):
        """统计指定范围内：未检测页数、待 OCR 页数。"""
        pages = self.document["pages"] if scope == 1 else [self.page()]
        pages = [p for p in pages if p]
        undetected = sum(1 for p in pages if not p["regions"])
        pending_ocr = sum(
            1
            for p in pages
            if p["regions"] and any(not r["source"].strip() for r in p["regions"])
        )
        return undetected, pending_ocr

    def ask(self, title, message, yes="确定", cancel="取消"):
        """只有「确定 / 取消」的轻确认，统一走 Fluent 弹窗。"""
        box = ConfirmBox(self, title, message, yes=yes, cancel=cancel,
                         default_yes=True)
        return box.exec()

    def ask_choice(self, title, prompt, choices, value=""):
        """从固定选项里挑一项，返回 (选中值, 是否确认)。"""
        box = InputBox(self, title, prompt, value=value, choices=choices)
        # InputBox uses QDialog.accept(); only ConfirmBox returns QMessageBox.Yes.
        if box.exec() != QDialog.Accepted:
            return None, False
        return box.data(), True

    def ask_text(self, title, prompt, value=""):
        """取一段文字，返回 (文本, 是否确认)。"""
        box = InputBox(self, title, prompt, value=value)
        if box.exec() != QDialog.Accepted:
            return "", False
        return box.text(), True

    def ask_yes_no(self, title, message, yes="是", no="否"):
        box = ConfirmBox(self, title, message, yes=yes, no=no, cancel="取消")
        return box.exec()

    def ask_patch(self, title, message, yes="补齐后翻译", no="直接翻译"):
        """三选一任务框（含「否」按钮），确认文案随任务变化时用。"""
        box = ConfirmBox(self, title, message, yes=yes, no=no, cancel="取消")
        return box.exec()

    def confirm_task(self, title, message, checkbox=None):
        """任务确认框：可选内置一次性勾选项。返回 (按钮, 是否勾选)。"""
        box = ConfirmBox(self, title, message, checkbox=checkbox,
                         yes="是", no="否")
        answer = box.exec()
        return answer, box.checked()

    def ask_detection(self, scope):
        if not self.document:
            return
        if scope == 1:
            undetected, pending_ocr = self.pending_counts(1)
            message = f"{undetected} 页未检测，{pending_ocr} 页待 OCR。"
        else:
            message = (
                "当前页尚未检测文字框。"
                if not self.page()["regions"]
                else "当前页已完成检测。"
            )
        answer, replace = self.confirm_task(
            "检测文字框",
            message + "\n\n是否在检测后一并读取原文（OCR）？\n已处理的文字框和原文保持不变。",
            checkbox="重新检测并替换已有文字框",
        )
        if answer == QMessageBox.Cancel:
            return
        self.run(
            "recognize" if answer == QMessageBox.Yes else "detect",
            scope=scope,
            replace=replace,
        )

    def ask_ocr(self, scope):
        if not self.document:
            return
        if scope == 1:
            undetected, pending_ocr = self.pending_counts(1)
            if undetected:
                answer = self.ask(
                    "补齐检测",
                    f"{undetected} 页没有文字框，要先检测这些页面再读取原文吗？已检测页面保持不变。",
                    yes="先检测",
                )
                if answer != QMessageBox.Yes:
                    return
                self.run("recognize", scope=1)
                return
            if pending_ocr:
                answer, replace = self.confirm_task(
                    "读取整部原文（OCR）",
                    f"{pending_ocr} 页待读取原文。是否开始 OCR？\n已有原文保持不变。",
                    checkbox="重新读取并替换已有原文",
                )
                if answer == QMessageBox.Cancel:
                    return
                if answer == QMessageBox.Yes:
                    self.run("ocr", scope=1, replace=replace)
                return
            self.status.setText("整部原文都已读取，无需再次 OCR")
            return
        pages = [self.page()]
        if any(not p["regions"] for p in pages if p):
            answer = self.ask(
                "尚未识别文字框",
                "当前页没有文字框，要先检测再进行 OCR 吗？",
                yes="先检测",
            )
            if answer != QMessageBox.Yes:
                return
            self.run("recognize", scope=scope)
        else:
            self.run("ocr", scope=scope)

    def set_auto_punctuation(self, checked):
        if self.loading_form:
            return

        def request():
            try:
                return self.client.request(
                    "POST", "/punctuation", json={"enabled": checked}
                )
            except Exception as error:
                return {"error": str(error)}

        def ready(state):
            if "error" in state:
                self.punctuation_action.blockSignals(True)
                self.punctuation_action.setChecked(not checked)
                self.punctuation_action.blockSignals(False)
                self.status.setText("公共替换规则保存失败：" + state["error"])
            else:
                self.apply_punctuation_state(state)

        self.background(request, ready)

    def refresh_punctuation(self):
        if self.closed or not self.document:
            return
        session = self.session
        self.background(
            lambda: self.client.request("GET", "/punctuation"),
            lambda state: (
                self.apply_punctuation_state(state) if session == self.session else None
            ),
        )

    def apply_punctuation_state(self, state):
        if "rows" not in state:
            return
        self.punctuation_action.blockSignals(True)
        self.punctuation_action.setChecked(state["enabled"])
        self.punctuation_action.blockSignals(False)
        if not self.document:
            return

        def action():
            self.document["settings"]["punctuation_rules"] = deepcopy(state["rows"])
            self.document["settings"]["punctuation_enabled"] = state["enabled"]
            self.document["settings"]["default_style"]["auto_punctuation"] = state[
                "enabled"
            ]
            for page in self.document["pages"]:
                if page.get("default_style"):
                    page["default_style"]["auto_punctuation"] = state["enabled"]
                for region in page["regions"]:
                    region["style"]["auto_punctuation"] = state["enabled"]

        self.edit("更新公共标点替换", action)

    def showEvent(self, event):
        super().showEvent(event)
        # 只在第一次显示时同步标点表：原来每次 showEvent 都跑一遍，
        # 会把用户刚改的标点规则重新拉回来并压一条撤销记录、把工程标脏。
        if hasattr(self, "document") and not getattr(self, "_punctuation_loaded", False):
            self._punctuation_loaded = True
            self.refresh_punctuation()

    def open_public_table(self, name):
        window = self.window()
        page = getattr(window, name, None)
        if page is None:
            self.status.setText("公共表格需要从 AiNiee 主窗口打开")
            return
        self.save(lambda: window.switchTo(page))

    def clear_selection(self):
        self.canvas.scene().clearSelection()
        self.select_region("")

    def clear_content(self, kind, scope=None):
        if not self.document or not self.page():
            return
        names = {
            "target": "译文",
            "source": "OCR 原文",
            "regions": "文字框（包括框内原文和译文）",
        }
        scopes = ["选中的框", "当前页", "整部漫画"]
        if scope is None:
            scope, ok = self.ask_choice(
                "清除" + names[kind],
                "清除范围",
                scopes,
                "当前页",
            )
            if not ok:
                return
        if scope not in scopes:
            return
        ids = set(
            self.canvas.selected_ids() or ([self.selected] if self.selected else [])
        )
        if scope == "选中的框" and not ids:
            self.status.setText("请先选中文字框")
            return
        if (
            self.ask(
                "确认清除",
                f"清除{scope}的{names[kind]}？可以撤销。",
                yes="删除" if kind == "regions" else "清除",
            )
            != QMessageBox.Yes
        ):
            return

        page_id = self.page_id
        changed = None

        def action():
            nonlocal changed
            changed = 0
            for page in self.document["pages"]:
                if scope != "整部漫画" and page["id"] != page_id:
                    continue
                if kind == "regions":
                    remaining = [
                        r
                        for r in page["regions"]
                        if scope == "选中的框" and r["id"] not in ids
                    ]
                    changed += len(page["regions"]) - len(remaining)
                    page["regions"] = remaining
                else:
                    for region in page["regions"]:
                        if scope == "选中的框" and region["id"] not in ids:
                            continue
                        if (
                            not region[kind]
                            and region["translation_state"] == "idle"
                            and not region["translation_error"]
                        ):
                            continue
                        region[kind] = ""
                        region["translation_state"], region["translation_error"] = (
                            "idle",
                            "",
                        )
                        changed += 1
        self.edit("清除" + names[kind], action)
        if changed is not None:
            self.status.setText(
                f"已处理 {changed} 个文字框（可撤销）"
                if changed
                else f"{scope}没有可清除的{names[kind]}"
            )

    def one_click_translation(self):
        if not self.document:
            return
        undetected, pending_ocr = self.pending_counts(1)
        if undetected or pending_ocr:
            parts = []
            if undetected:
                parts.append(f"{undetected} 页未检测")
            if pending_ocr:
                parts.append(f"{pending_ocr} 页待 OCR")
            answer = self.ask_patch(
                "补齐后翻译",
                "、".join(parts)
                + "。是否补齐后翻译整部？\n已处理的文字框、原文和人工修改保持不变。",
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Yes:
                self.run(
                    "recognize", scope=1, after=lambda: self.run("translate", scope=1)
                )
                return
        self.run("translate", scope=1)

    def update_page_labels(self, *_):
        if not self.document:
            return
        compact = self.page_panel.width() < 150
        self.page_title.setVisible(not compact)
        self.page_bar_widget.setVisible(not compact)
        for i, page in enumerate(self.document["pages"]):
            if i < self.pages.count():
                name = page["name"]
                stem = name.rsplit(".", 1)[0]
                self.pages.item(i).setText(
                    str(i + 1) if compact else f"{i + 1} · {stem}"
                )
                self.pages.item(i).setToolTip(
                    f"{name} · {len(page['regions'])} 个文字框"
                )

    def fit_canvas(self):
        self.canvas.fitInView(self.canvas.sceneRect(), Qt.KeepAspectRatio)
        self._zoom_percent = 0
        self.update_zoom(self.canvas.transform().m11())

    def step_zoom(self, factor):
        if not self.document:
            return
        current = self.canvas.transform().m11() * 100
        self.set_zoom(round(current * factor))
        self.update_zoom(self.canvas.transform().m11())

    def step_page(self, delta):
        if not self.document or not self.document.get("pages"):
            return
        row = self.pages.currentRow()
        if row < 0:
            row = 0
        target = max(0, min(len(self.document["pages"]) - 1, row + delta))
        if target != row:
            self.pages.setCurrentRow(target)

    def cycle_region(self, step):
        page = self.page()
        if not page or not page.get("regions"):
            return
        ordered = sorted(page["regions"], key=lambda r: r.get("order", 0))
        ids = [r["id"] for r in ordered]
        if self.selected in ids:
            idx = (ids.index(self.selected) + step) % len(ids)
        else:
            idx = 0 if step >= 0 else len(ids) - 1
        self.select_region(ids[idx])
        self.draw_canvas()

    ZOOM_MIN = 5
    ZOOM_MAX = 800

    def set_zoom(self, value):
        value = max(self.ZOOM_MIN, min(int(value), self.ZOOM_MAX))
        # 以视口中心为锚点缩放，避免滚动条范围变化导致画面跳动
        anchor = self.canvas.transformationAnchor()
        self.canvas.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.canvas.resetTransform()
        self.canvas.scale(value / 100, value / 100)
        self.canvas.setTransformationAnchor(anchor)
        self.zoom_label.setText(f"{value}%")
        self._zoom_percent = value
        self.maybe_show_pan_hint(value / 100)

    def update_zoom(self, scale):
        percent = max(1, round(scale * 100))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(
            max(self.ZOOM_MIN, min(percent, self.ZOOM_MAX))
        )
        self.zoom_slider.blockSignals(False)
        self.zoom_label.setText(f"{percent}%")
        self.position_zoom_controls()
        self.maybe_show_pan_hint(percent / 100)

    def position_zoom_controls(self):
        self.zoom_overlay.adjustSize()
        viewport = self.canvas.viewport()
        # 画布比浮层还窄时（窄窗口 / 三栏挤压），贴右会溢出到看不见，
        # 所以要双向 clamp；宽度也压到视口内，避免滑块被裁掉。
        self.zoom_overlay.setMaximumWidth(max(120, viewport.width() - 20))
        self.zoom_overlay.adjustSize()
        self.zoom_overlay.move(
            max(0, min(viewport.width() - self.zoom_overlay.width() - 10, viewport.width())),
            max(
                0,
                min(
                    viewport.height() - self.zoom_overlay.height() - 10,
                    viewport.height(),
                ),
            ),
        )
        self.zoom_overlay.raise_()

    def position_pan_hint(self):
        self.pan_hint.adjustSize()
        self.pan_hint.move(
            max(10, (self.canvas.viewport().width() - self.pan_hint.width()) // 2),
            14,
        )
        self.pan_hint.raise_()

    def on_pan_started(self):
        """拖动期间收起提示浮层，避免和光标抢注意力。"""
        self._pan_hint_timer = None
        if getattr(self, "pan_hint", None):
            self.pan_hint.hide()

    def on_pan_finished(self):
        scale = self.canvas.transform().m11()
        self._pan_hint_at = 0.0
        self.maybe_show_pan_hint(max(scale, 0.01))

    def maybe_show_pan_hint(self, scale):
        """画面比“适应窗口”更大时，提示可以用中键拖动。"""
        if not getattr(self, "pan_hint", None) or not self.image_data:
            return
        if getattr(self.canvas, "pan_origin", None) is not None:
            return
        scene = self.canvas.sceneRect()
        viewport = self.canvas.viewport()
        if scene.width() <= 0 or scene.height() <= 0:
            return
        fit = min(
            viewport.width() / scene.width(),
            viewport.height() / scene.height(),
        )
        if scale <= fit * 1.03:
            self.pan_hint.hide()
            return
        import time

        now = time.monotonic()
        if now - getattr(self, "_pan_hint_at", 0.0) < 8:
            return
        self._pan_hint_at = now
        self.pan_hint_label.setText("按住空格键或鼠标中键拖动画面")
        self.pan_hint.show()
        self.position_pan_hint()
        QTimer.singleShot(3200, self.pan_hint.hide)

    def position_progress_panel(self):
        viewport = self.canvas.viewport()
        max_w = min(400, max(240, viewport.width() - 20))
        min_w = min(260, max_w)
        self.progress_panel.setMinimumWidth(min_w)
        self.progress_panel.setMaximumWidth(max_w)
        self.progress_panel.adjustSize()
        self.progress_panel.move(
            max(0, min(viewport.width() - self.progress_panel.width() - 10, viewport.width())),
            max(
                0,
                viewport.height()
                - self.progress_panel.height()
                - self.zoom_overlay.height()
                - 20,
            ),
        )
        self.progress_panel.raise_()

    def refresh_font_mode_hint(self, *_):
        region = self.region()
        if not region:
            self.font_mode_hint.setText("选择文字框后显示实际字号")
            return
        mode = "自动适配" if region["style"]["autofit"] else "固定字号"
        hint = f"{mode}"
        info = self.canvas.last_layout_info.get(region["id"]) if hasattr(
            self.canvas, "last_layout_info"
        ) else None
        if info:
            hint += f"：实际字号 {info['font_size']} px"
        self.font_mode_hint.setText(hint)

    def show_layout_size(self, region_id, info):
        if region_id == self.selected and self.region():
            mode = (
                "自动适配" if self.region()["style"]["autofit"] else "固定字号"
            )
            self.font_mode_hint.setText(
                f"{mode}：实际字号 {info['font_size']} px"
                + (" · 建议扩大文字框" if info.get("below_minimum") else "")
            )

    def change_view(self):
        if not self.document:
            return
        if self.variant.currentData() == "render":
            self.run("render", scope=0)
            return
        if self.variant.currentData() == "compare":
            self.status.setText("前后对比：左侧原图，右侧成品；成品尚未生成时显示原图")
        self.load_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # zoom_overlay 是常驻浮层，改窗口大小后必须跟着重新贴角，
        # 否则它会停在旧位置、和新宽度错位。
        if getattr(self, "zoom_overlay", None) is not None:
            self.position_zoom_controls()
        if not self.progress_panel.isHidden():
            self.position_progress_panel()
        if getattr(self, "pan_hint", None) and not self.pan_hint.isHidden():
            self.position_pan_hint()

    def eventFilter(self, watched, event):
        if (
            hasattr(self, "page_panel")
            and watched is self.page_panel
            and event.type() == QEvent.Resize
        ):
            self.update_page_labels()
        if (
            hasattr(self, "zoom_overlay")
            and watched is self.canvas.viewport()
            and event.type() == QEvent.Resize
        ):
            self.position_zoom_controls()
            if hasattr(self, "progress_panel") and not self.progress_panel.isHidden():
                self.position_progress_panel()
        if (
            hasattr(self, "canvas")
            and watched is self.canvas.viewport()
            and event.type() == QEvent.ContextMenu
        ):
            point = event.pos()
            item = next(
                (
                    i
                    for i in self.canvas.items(point)
                    if type(i).__name__ == "RegionItem"
                ),
                None,
            )
            if item is not None:
                region_id = item.region["id"]
                if not item.isSelected():
                    # 右键未选中的框视为「只操作这一个」，先清掉旧多选，
                    # 否则菜单里的「删除选中的 N 个框」会连坐之前的选中项。
                    self.canvas.scene().clearSelection()
                item.setSelected(True)
                QTimer.singleShot(
                    0,
                    lambda rid=region_id, pos=QCursor.pos(): self.region_context_menu(
                        rid, pos
                    ),
                )
                return True
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
                if event.key() == Qt.Key_Escape:
                    # QPlainTextEdit 自己消费 Esc，页面级 keyPressEvent 收不到；
                    # 在文本框里按 Esc 也要能退出几何模式 / 取消选中。
                    self.exit_geometry_mode()
                    self.clear_selection()
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
        button.task_menu = menu
        layout.addWidget(button)
        return button

    def divider(self, layout, height=18):
        """工具条分组用的竖线：比 QFrame(VLine) 更细、颜色跟随主题。"""
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Plain)
        line.setFixedWidth(1)
        line.setFixedHeight(height)
        tone = "rgba(255,255,255,0.14)" if isDarkTheme() else "rgba(0,0,0,0.12)"
        line.setStyleSheet(f"QFrame {{background:{tone};border:none;}}")
        layout.addWidget(line)
        return line

    def icon_button(self, layout, icon, label, action):
        button = TransparentToolButton(icon, self)
        button.setFixedSize(32, 32)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def with_region(self, region_id, action):
        self.select_region(region_id)
        action()

    def ask_region_ocr(self, region_id):
        region = next(
            (
                r
                for r in (self.page() or {}).get("regions", [])
                if r["id"] == region_id
            ),
            None,
        )
        if not region:
            return
        if region["source"].strip():
            dialog = RegionRetryDialog(self, "ocr", region)
            if dialog.exec() != QDialog.Accepted:
                return
            self.run(
                "ocr",
                scope=0,
                region_ids=[region_id],
                ocr_engine=dialog.engine_id(),
            )
        else:
            self.run("ocr", scope=0, region_ids=[region_id])

    def ask_region_translate(self, region_id):
        region = next(
            (
                r
                for r in (self.page() or {}).get("regions", [])
                if r["id"] == region_id
            ),
            None,
        )
        if not region:
            return
        if region["target"].strip():
            dialog = RegionRetryDialog(self, "translate", region)
            if dialog.exec() != QDialog.Accepted:
                return
            self.run(
                "translate",
                scope=0,
                region_ids=[region_id],
                provider_id=dialog.engine_id(),
            )
        else:
            self.run("translate", scope=0, region_ids=[region_id])

    def region_selection(self, region_id):
        """右键目标 + 当前画布选中：返回要批量操作的框 id 列表。"""
        regions = (self.page() or {}).get("regions", [])
        if not any(r["id"] == region_id for r in regions):
            return []
        selected = set(self.canvas.selected_ids())
        if region_id not in selected:
            selected = {region_id}
        return [r["id"] for r in regions if r["id"] in selected]

    def erase_regions(self, ids):
        ids = set(ids)

        def action():
            for region in self.page()["regions"]:
                if region["id"] in ids:
                    region["erase_only"] = True

        self.edit("去除框内原文", action)
        self.variant.setCurrentIndex(self.variant.findData("edit"))
        self.draw_canvas()

    def clear_region_texts(self, ids, field):
        ids = set(ids)

        def action():
            for region in self.page()["regions"]:
                if region["id"] in ids:
                    region[field] = ""
                    region["translation_state"], region["translation_error"] = (
                        "idle",
                        "",
                    )

        label = "清除译文" if field == "target" else "清除原文"
        self.edit(f"批量{label}", action)
        self.status.setText(f"已清除 {len(ids)} 个框的{label[2:]}（可撤销）")

    def set_style_values(self, ids, key, value):
        ids = set(ids)

        def action():
            for region in self.page()["regions"]:
                if region["id"] in ids:
                    region["style"][key] = value

        self.edit("批量修改排版", action)

    def ask_regions_ocr(self, ids):
        ids = list(ids)
        if not ids:
            return
        regions = [r for r in self.page()["regions"] if r["id"] in set(ids)]
        if any(r["source"].strip() for r in regions):
            dialog = RegionRetryDialog(self, "ocr", regions, ids)
            if dialog.exec() != QDialog.Accepted:
                return
            self.run("ocr", scope=0, region_ids=ids, ocr_engine=dialog.engine_id())
        else:
            self.run("ocr", scope=0, region_ids=ids)

    def ask_regions_translate(self, ids):
        ids = list(ids)
        if not ids:
            return
        regions = [r for r in self.page()["regions"] if r["id"] in set(ids)]
        if any(r["target"].strip() for r in regions):
            dialog = RegionRetryDialog(self, "translate", regions, ids)
            if dialog.exec() != QDialog.Accepted:
                return
            self.run(
                "translate", scope=0, region_ids=ids, provider_id=dialog.engine_id()
            )
        else:
            self.run("translate", scope=0, region_ids=ids)

    def ask_region_ocr(self, region_id):
        self.ask_regions_ocr([region_id])

    def ask_region_translate(self, region_id):
        self.ask_regions_translate([region_id])

    def region_context_menu(self, region_id, position):
        ids = self.region_selection(region_id)
        if not ids:
            return
        batch = len(ids) > 1
        suffix = f"（{len(ids)} 个框）" if batch else ""
        menu = RoundMenu(parent=self.canvas)
        menu.addAction(
            Action(
                FluentIcon.DELETE,
                f"删除此框{suffix}" if not batch else f"删除选中的 {len(ids)} 个框",
                triggered=lambda: self.delete_regions(ids),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.SYNC,
                f"OCR 此框{suffix}" if not batch else f"OCR 选中的 {len(ids)} 个框",
                triggered=lambda: self.ask_regions_ocr(ids),
            )
        )
        menu.addAction(
            Action(
                FluentIcon.LANGUAGE,
                f"翻译此框{suffix}" if not batch else f"翻译选中的 {len(ids)} 个框",
                triggered=lambda: self.ask_regions_translate(ids),
            )
        )
        advanced = RoundMenu("高级", parent=menu)
        advanced.addAction(
            Action(
                FluentIcon.BROOM,
                "去除框内原文" + suffix,
                triggered=lambda: self.erase_regions(ids),
            )
        )
        advanced.addAction(
            Action(
                FluentIcon.CLEAR_SELECTION,
                "清除此框译文" + suffix,
                triggered=lambda: self.clear_region_texts(ids, "target"),
            )
        )
        advanced.addSeparator()
        advanced.addAction(
            Action(
                FluentIcon.CUT,
                "调整取字范围…",
                triggered=lambda: self.with_region(
                    region_id, lambda: self.enter_geometry_mode("ocr_rect")
                ),
            )
        )
        advanced.addAction(
            Action(
                FluentIcon.DELETE,
                "调整去字范围…",
                triggered=lambda: self.with_region(
                    region_id, lambda: self.enter_geometry_mode("erase_rect")
                ),
            )
        )
        advanced.addAction(
            Action(
                FluentIcon.FONT_SIZE,
                "自动适应框大小" + suffix,
                triggered=lambda: self.set_style_values(ids, "autofit", True),
            )
        )
        # qfluentwidgets 的 RoundMenu 需显式挂子菜单
        menu.addAction(advanced.menuAction())
        menu.exec(position)

    def erase_region(self):
        if self.region():
            self.region_value("erase_only", True)
            self.variant.setCurrentIndex(self.variant.findData("edit"))
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
        apply_dialog_theme(dialog, "mangaTaskDetailsDialog")
        dialog.resize(820, 540)
        layout = dialog_layout(dialog, spacing=10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(SubtitleLabel("进度与失败明细", dialog))
        header.addStretch(1)
        layout.addLayout(header)

        rows = []
        pages_list = (self.document or {}).get("pages", [])
        pages = {p["id"]: p for p in pages_list}
        states = {
            "pending": "等待翻译",
            "succeeded": "成功",
            "failed": "失败",
            "cancelled": "已取消",
            "idle": "未开始",
        }
        has_failed_translation = False
        for p in pages_list:
            for r in p["regions"]:
                t_state = r.get("translation_state", "idle")
                warnings = [
                    str(w).strip() for w in (r.get("warnings") or []) if str(w).strip()
                ]
                if t_state in {"failed", "cancelled"}:
                    has_failed_translation = True
                if t_state != "idle" or warnings:
                    detail = r.get("translation_error", "")
                    if warnings:
                        warn_text = "；".join(warnings)
                        detail = f"{detail}；{warn_text}" if detail else warn_text
                    status_text = states.get(t_state, "未开始")
                    if t_state == "idle" and warnings:
                        status_text = "需检查"
                    tone = (
                        "error"
                        if t_state in {"failed", "cancelled"}
                        else "warn"
                        if warnings
                        else "ok"
                        if t_state == "succeeded"
                        else ""
                    )
                    rows.append((p, r, status_text, detail, tone))
        last_report = getattr(self, "last_task_report", {}) or {}
        for error in last_report.get("errors", []):
            page = pages.get(error.get("page_id"), {"id": "", "name": "任务"})
            rows.append(
                (
                    page,
                    {"id": None, "order": -1},
                    "失败",
                    error.get("message", str(error)),
                    "error",
                )
            )
        for page_id in last_report.get("empty_pages", []) or []:
            page = pages.get(page_id, {"id": page_id, "name": str(page_id)})
            rows.append(
                (
                    page,
                    {"id": None, "order": -1},
                    "未检出",
                    "未检出文字框：可降低检测阈值或更换检测模型",
                    "warn",
                )
            )
        for item in last_report.get("fallback_pages", []) or []:
            page = pages.get(item.get("page_id"), {"id": "", "name": "页面"})
            rows.append(
                (
                    page,
                    {"id": None, "order": -1},
                    "已补检",
                    f"由 {item.get('model', '')} 回退到行级检测补出文字框",
                    "warn",
                )
            )
        for item in last_report.get("skipped_pages", []) or []:
            page = pages.get(item.get("page_id"), {"id": "", "name": "页面"})
            rows.append(
                (
                    page,
                    {"id": None, "order": -1},
                    "已跳过",
                    item.get("message", "已有文字框，已跳过检测"),
                    "",
                )
            )

        summary_note = dialog_note(
            f"{self.progress_label.text()}（共 {len(rows)} 条记录）"
        )
        layout.addWidget(summary_note)

        table = QTableWidget(dialog)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["页面", "文字框", "状态", "明细 / 原因"])
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setRowCount(len(rows))
        dark = isDarkTheme()
        tone_colors = {
            "error": QColor("#ff7b7b" if dark else "#c42b1c"),
            "warn": QColor("#ffd369" if dark else "#9d5d00"),
            "ok": QColor("#6ccb5f" if dark else "#0f7b0f"),
        }
        for i, (p, r, status_text, detail_text, tone) in enumerate(rows):
            box_label = str(r["order"] + 1) if r.get("order", -1) >= 0 else "-"
            for j, value in enumerate(
                [p["name"], box_label, status_text, detail_text]
            ):
                item = QTableWidgetItem(value)
                if j == 2 and tone in tone_colors:
                    item.setForeground(tone_colors[tone])
                table.setItem(i, j, item)
        table.resizeColumnsToContents()
        table.cellDoubleClicked.connect(
            lambda row, col: (
                self.locate_region(rows[row][0]["id"], rows[row][1]["id"]),
                dialog.accept(),
            )
        )
        layout.addWidget(table, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(dialog_note("双击任意行可快速定位到对应页面或文字框"), 1)
        retry_btn = PrimaryPushButton("重试失败文字框", dialog)
        retry_btn.setEnabled(has_failed_translation)
        retry_btn.clicked.connect(lambda: (dialog.accept(), self.retry_failed()))
        close_btn = QPushButton("关闭", dialog)
        close_btn.clicked.connect(dialog.accept)
        footer.addWidget(retry_btn)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        dialog.exec()

    def locate_region(self, page_id, region_id):
        index = next(
            (i for i, p in enumerate(self.document["pages"]) if p["id"] == page_id), -1
        )
        if index >= 0:
            self.pages.setCurrentRow(index)
            if region_id:
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

    def refresh_fonts(self):
        def loaded_fonts(result):
            selected_font = (
                self.region()["style"]["font"]
                if self.region()
                else self.font.currentData()
            )
            self.font.blockSignals(True)
            self.font.clear()
            self.font.addItem("内置字体", "")
            for path in result.get("fonts", []):
                self.font.addItem(Path(path).name, path)
            if selected_font and self.font.findData(selected_font) < 0:
                self.font.addItem(Path(selected_font).name, selected_font)
            self.font.setCurrentIndex(max(0, self.font.findData(selected_font or "")))
            self.font.blockSignals(False)

        self.background(lambda: self.client.request("GET", "/fonts"), loaded_fonts)

    def refresh_model_catalogue(self, models=None):
        """缓存模型目录；供模型中心与项目设置的下拉共用。"""

        def loaded(data):
            self.model_catalogue = data.get("models", [])
            self.fill_model_choices()
            self.adopt_detector_threshold()

        if models is None:
            self.background(lambda: self.client.request("GET", "/models"), loaded)
            return
        self.model_catalogue = models
        self.fill_model_choices()
        self.adopt_detector_threshold()

    def adopt_detector_threshold(self):
        """目录到手后把阈值同步到当前检测器的建议值。

        只在没有打开工程时执行：工程里保存的阈值是用户自己的选择，
        打开时不能被目录里的建议值覆盖。
        """
        if self.document or self.job_id:
            return
        value = self.detector_threshold(self.detector.currentData())
        if value is not None:
            self.confidence.setValue(value)

    def detector_threshold(self, model_id):
        model = next(
            (m for m in (self.model_catalogue or []) if m.get("id") == model_id),
            None,
        )
        value = model.get("default_confidence") if model else None
        return float(value) if isinstance(value, (int, float)) else None

    def pick_detector(self, *_):
        if self.loading_form:
            return
        model_id = self.detector.currentData()
        if not model_id:
            return
        value = self.detector_threshold(model_id)
        if not self.document:
            if value is not None:
                self.confidence.setValue(value)
            return

        def action():
            settings = self.document["settings"]
            settings["detector"] = model_id
            if value is not None:
                settings["confidence"] = value

        self.edit("切换检测模型", action)

    def fill_model_choices(self):
        catalogue = getattr(self, "model_catalogue", None) or []
        for combo, role, current in (
            (self.detector, "detect", self.detector.currentData()),
            (self.ocr, "ocr", self.ocr.currentData()),
        ):
            tracks_project = bool(self.document)
            if tracks_project:
                key = "detector" if role == "detect" else "ocr"
                current = self.document["settings"].get(key, current)
            entries = [
                m
                for m in catalogue
                if m.get("role") == role
                and m.get("installed")
                and m.get("supported", True)
                and (
                    m.get("kind", "local") in {"local", "system"}
                    or (m.get("kind") == "api" and m.get("api_ready"))
                    or m.get("kind") == "package"
                )
            ]
            if not entries and not (tracks_project and current):
                continue
            combo.blockSignals(True)
            combo.clear()
            for m in entries:
                combo.addItem(m["name"], m["id"])
            index = combo.findData(current)
            if index < 0 and current and tracks_project:
                combo.addItem(self.unavailable_model_label(current), current)
                index = combo.count() - 1
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def unavailable_model_label(self, model_id):
        model = next(
            (m for m in (self.model_catalogue or []) if m["id"] == model_id), None
        )
        return f"{model['name'] if model else model_id}（不可用）"

    def recognize_here(self):
        """无框时先建框；有框时直接 OCR 选中的框或当前页。"""
        if not self.document:
            self.status.setText("请先导入漫画或打开工程包")
            return
        if self.job_id or self.submitting:
            self.status.setText("已有任务在执行，请等它结束或先取消当前任务")
            self.set_status_tone("warn")
            return
        page = self.page()
        if page is None:
            return
        if not page["regions"]:
            self.run("detect", scope=0)
            return
        engine = self.ocr.currentData()
        selected = set(self.canvas.selected_ids() or ([self.selected] if self.selected else []))
        ids = [
            r["id"]
            for r in page["regions"]
            if r["id"] in selected
        ]
        if ids:
            self.run("ocr", scope=0, region_ids=ids, ocr_engine=engine, replace=True)
            return
        self.run("ocr", scope=0, ocr_engine=engine, replace=True)

    def sync_project_summary(self):
        if not hasattr(self, "project_summary_button"):
            return
        source = self.source_language.currentText()
        target = self.target_language.currentText()
        self.project_summary_button.setText(f"{source} → {target}")
        self.project_summary_button.setToolTip(f"项目设置 · {source} → {target}")

    def open_project_settings(self):
        self.fill_model_choices()
        if self.project_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("项目设置")
            apply_dialog_theme(dialog, "mangaProjectDialog")
            dialog.resize(460, 420)
            layout = dialog_layout(dialog)

            # 语言放最前：导入后最常改的就是「原文语言 → 目标语言」
            languages = QFormLayout()
            languages.setHorizontalSpacing(12)
            languages.setVerticalSpacing(8)
            self.language_swap = TransparentToolButton(FluentIcon.SYNC)
            self.language_swap.setToolTip("对调原文语言与目标语言")
            self.language_swap.clicked.connect(self.swap_languages)
            direction = QWidget()
            direction_row = QHBoxLayout(direction)
            direction_row.setContentsMargins(0, 0, 0, 0)
            direction_row.setSpacing(6)
            direction_row.addWidget(self.source_language, 1)
            direction_row.addWidget(self.language_swap)
            direction_row.addWidget(self.target_language, 1)
            languages.addRow("语言", direction)
            layout.addLayout(languages)
            layout.addWidget(self.divider_row())

            form = QFormLayout()
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(8)
            form.addRow("检测阈值", self.confidence)
            form.addRow(self.panels)
            form.addRow(self.rtl)
            form.addRow(self.auto_recognize)
            form.addRow("翻译接口", self.provider)
            layout.addLayout(form)
            layout.addWidget(dialog_note("语言与识别选项随本工程保存。模型可在编辑器顶栏切换。"))
            layout.addStretch(1)
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            buttons.addStretch(1)
            close = QPushButton("完成")
            close.setDefault(True)
            close.clicked.connect(dialog.accept)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            self.project_dialog = dialog
        self.project_dialog.show()
        self.project_dialog.raise_()
        self.project_dialog.activateWindow()
        self.status.setText("项目设置已更改会自动保存到当前工程")

    def swap_languages(self):
        source = self.source_language.currentData()
        target = self.target_language.currentData()
        if not source or not target:
            return
        self.source_language.blockSignals(True)
        self.target_language.blockSignals(True)
        self.source_language.setCurrentIndex(
            max(0, self.source_language.findData(target))
        )
        self.target_language.setCurrentIndex(
            max(0, self.target_language.findData(source))
        )
        self.source_language.blockSignals(False)
        self.target_language.blockSignals(False)
        self.set_setting("source_language", self.source_language.currentData())
        self.set_setting("target_language", self.target_language.currentData())
        self.sync_project_summary()

    def divider_row(self):
        """弹窗内的横向分隔线。"""
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Plain)
        line.setFixedHeight(1)
        line.setStyleSheet(
            "background:rgba(255,255,255,0.14);"
            if isDarkTheme()
            else "background:rgba(0,0,0,0.12);"
        )
        return line

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
        self.variant.setCurrentIndex(self.variant.findData("edit"))
        self.variant.blockSignals(False)
        self.workspace_stack.setCurrentWidget(self.work_page)
        self.save_timer.stop()
        self.dirty = False
        self.image_data = None
        self.source_cache.clear()
        self.clean_cache.clear()
        self.canvas.scene().clear()
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
        self.progress_panel.hide()
        self.progress_label.setText(
            f"本工程 · 有译文 {done} 框 · 失败 / 取消 {failed} 框 · 总计 {len(frames)} 框"
        )
        self.selected = None
        self.undo.clear()
        self.page_id = document["pages"][0]["id"] if document["pages"] else None
        self.refresh_pages()
        self.populate_settings()
        self.fill_model_choices()
        self.select_region(None)
        self.load_image()
        self.refresh_punctuation()
        self.status.setText(
            "工程已加载：拖动文字框实时排版，右键编辑；识别与翻译操作在顶栏。"
        )

    def refresh_pages(self):
        # 重建列表会清空左栏多选：先记下选中的页，重建后按 id 还原，
        # 否则「Shift 选中若干页 → 删除/导出」在任意刷新后失效。
        keep = {
            self.document["pages"][row]["id"]
            for row in (
                index.row() for index in self.pages.selectionModel().selectedRows()
            )
            if 0 <= row < len(self.document["pages"])
        }
        current = self.pages.currentRow()
        if 0 <= current < len(self.document["pages"]):
            keep.add(self.document["pages"][current]["id"])
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
        for row, page in enumerate(self.document["pages"]):
            if page["id"] in keep:
                item = self.pages.item(row)
                if item is not None:
                    item.setSelected(True)
        self.pages.blockSignals(False)
        # 当前页已被删掉时，选中框的 id 一并作废，避免右栏显示上一页残留内容
        if self.selected and not any(
            r["id"] == self.selected for r in (self.page() or {}).get("regions", [])
        ):
            self.selected = None
        self.refresh_regions()
        self.update_page_labels()

    @form_update
    def populate_settings(self):
        self.loading_form = True
        s = self.document["settings"]
        self.precise.setChecked(s.get("removal") == "precise")
        for combo, value in (
            (self.detector, s["detector"]),
            (self.ocr, s["ocr"]),
        ):
            index = combo.findData(value)
            if index < 0 and value:
                combo.addItem(self.unavailable_model_label(value), value)
                index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))
        self.panels.setChecked(s["detect_panels"])
        for combo, value in (
            (self.source_language, s["source_language"]),
            (self.target_language, s["target_language"]),
        ):
            index = combo.findData(value)
            if index < 0 and value:
                combo.addItem(value, value)
                index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))
        self.confidence.setValue(s["confidence"])
        self.rtl.setChecked(s["right_to_left"])
        self.auto_recognize.setChecked(s["auto_recognize"])
        if s["provider_id"] and self.provider.findData(s["provider_id"]) < 0:
            self.provider.addItem(s["provider_id"], s["provider_id"])
        self.provider.setCurrentIndex(max(0, self.provider.findData(s["provider_id"])))
        self.loading_form = False
        self.sync_project_summary()

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
        if self.job_id and getattr(self, "current_job_stage", None) in {
            "detect",
            "ocr",
            "recognize",
        }:
            self.follow_banner.show()
        self.follow_progress = False
        self.page_id = self.document["pages"][index]["id"]
        if getattr(self, "pan_hint", None):
            self.pan_hint.hide()
        self.image_data = None
        self.canvas.scene().clear()
        self.refresh_regions()
        # 只置 selected=None 不够：右栏仍显示上一页的框内容且控件可用，
        # 用户在新页里打字会被 region_value 当作「没有选中框」丢掉。
        self.select_region(None)
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
                text = " · ".join(
                    part
                    for part in [
                        (r["source"] or "待识别"),
                        (r["target"] or "待翻译") if r["source"] else "",
                    ]
                    if part
                ) + f"（{state}）"
                order = QTableWidgetItem(str(row + 1))
                content = QTableWidgetItem(text)
                content.setToolTip(r.get("translation_error") or text)
                if state == "失败":
                    content.setForeground(QColor("#e74856"))
                # 排版警告（字号过小 / 溢出）以前只在渲染报告里一闪而过，
                # 现在每框都有状态标记，能直接在列表里定位要改的框。
                notes = [str(w) for w in (r.get("warnings") or [])]
                status_text = "需调整" if notes else "-"
                status = QTableWidgetItem(status_text)
                if notes:
                    status.setForeground(QColor("#c42b1c"))
                    status.setToolTip("\n".join(notes))
                status.setTextAlignment(Qt.AlignCenter)
                self.regions.setItem(row, 0, order)
                self.regions.setItem(row, 1, content)
                self.regions.setItem(row, 2, status)
                text_width = max(120, self.regions.columnWidth(1) - 20)
                height = (
                    self.regions.fontMetrics()
                    .boundingRect(QRect(0, 0, text_width, 40), Qt.TextWordWrap, text)
                    .height()
                    + 14
                )
                self.regions.setRowHeight(row, max(34, min(160, height)))
                if r["id"] == self.selected:
                    self.regions.setCurrentCell(row, 1)
        finally:
            self.regions.blockSignals(False)
        if hasattr(self, "reading_count_label"):
            self.reading_count_label.setText(
                f"{len(regions)} 框" if regions else ""
            )

    def review_changed(self, row, column):
        if self.loading_form or column not in {1}:
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
            region_id = regions[index]["id"]
            self.select_region(region_id)
            for item in self.canvas.scene().items():
                from Frontend.pages.media.manga_canvas import RegionItem

                if isinstance(item, RegionItem) and item.region["id"] == region_id:
                    self.canvas.ensureVisible(item.mapToScene(item.rect()).boundingRect())
                    break
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
            self.fixed_size_button,
            self.horizontal_button,
            self.vertical_button,
            self.vertical_rtl,
            self.bold_button,
            self.spacing,
            self.charspace,
            self.stroke,
            self.align,
            self.rotation,
            self.source_lock,
            self.target_lock,
        ):
            widget.setEnabled(bool(r))
        if not r:
            self.vertical_rtl.hide()
            self.source.clear()
            self.target.clear()
            self.font_mode_hint.setText("选择文字框后显示实际字号")
            self.source.setPlaceholderText("点击画布或阅读顺序中的文字框")
            self.target.setPlaceholderText("选中框后可编辑译文与排版")
            return
        self.loading_form = True
        if self.source.toPlainText() != r["source"]:
            self.source.setPlainText(r["source"])
        if self.target.toPlainText() != r["target"]:
            self.target.setPlainText(r["target"])
        s = r["style"]
        # 锁定开关回填时也要屏蔽信号，否则 setChecked 会把用户的锁定状态当成新编辑写回
        for widget, value in (
            (self.source_lock, r["source_locked"]),
            (self.target_lock, r["target_locked"]),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        for widget, key in [
            (self.size, "size"),
            (self.minimum, "min_size"),
            (self.spacing, "line_spacing"),
            (self.charspace, "char_spacing"),
            (self.stroke, "stroke_width"),
        ]:
            widget.setValue(s[key])
        for widget, value in (
            (self.vertical_button, s["vertical"]),
            (self.bold_button, s["bold"]),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        if s["font"] and self.font.findData(s["font"]) < 0:
            self.font.addItem(Path(s["font"]).name, s["font"])
        for widget, value in (
            (self.font, s["font"]),
            (self.removal, r["removal"]),
        ):
            widget.blockSignals(True)
            widget.setCurrentIndex(max(0, widget.findData(value)))
            widget.blockSignals(False)
        self.rotation.blockSignals(True)
        self.rotation.setValue(r["layout_rect"]["rotation"])
        self.rotation.blockSignals(False)
        self.punctuation_action.blockSignals(True)
        self.punctuation_action.setChecked(s.get("auto_punctuation", False))
        self.punctuation_action.blockSignals(False)
        for widget, value in (
            (self.horizontal_button, not s["vertical"]),
            (self.vertical_button, s["vertical"]),
            (self.bold_button, s["bold"]),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        self.align.blockSignals(True)
        self.align.setCurrentIndex(max(0, self.align.findData(s["align"])))
        self.align.blockSignals(False)
        self.vertical_rtl.blockSignals(True)
        self.vertical_rtl.setVisible(bool(s["vertical"]))
        self.vertical_rtl.setChecked(bool(s.get("vertical_rtl", True)))
        self.vertical_rtl.blockSignals(False)
        self.autofit.blockSignals(True)
        self.fixed_size_button.blockSignals(True)
        self.autofit.setChecked(s["autofit"])
        self.fixed_size_button.setChecked(not s["autofit"])
        self.autofit.blockSignals(False)
        self.fixed_size_button.blockSignals(False)
        self.size.setEnabled(not s["autofit"])
        self.loading_form = False

    def draw_canvas(self):
        if self.image_data and self.page():
            selected = self.canvas.selected_ids()
            if self.selected not in selected:
                selected = [self.selected] if self.selected else []
            self.canvas.blockSignals(True)
            data = self.image_data
            editing = self.variant.currentData() == "edit"
            self.canvas.punctuation_rules = self.document["settings"].get(
                "punctuation_rules"
            )
            self.canvas.editable = self.variant.currentData() not in {"render", "compare"}
            self.canvas.live_layout = editing and self.canvas.rect_key == "layout_rect"
            if editing and self.document["settings"].get("removal") == "fill":
                data = self.fill_preview(data)
            if editing and not self.canvas.live_layout:
                from io import BytesIO

                from PIL import Image

                from Shared.manga_lettering import render_text

                # 合成整页要跑 PIL：同一页同一批文字框反复重绘（拖动缩放、
                # 切列表行）时直接复用上一次结果，避免 GUI 线程每帧重算。
                key = (
                    self.page_id,
                    self.epoch,
                    self.canvas.rect_key,
                    id(data),
                    tuple(
                        (r["id"], r.get("target", ""), repr(r["style"]), repr(r["layout_rect"]))
                        for r in self.page()["regions"]
                    ),
                )
                if self._canvas_cache_key == key and self._canvas_cache_data:
                    data = self._canvas_cache_data
                else:
                    image = Image.open(BytesIO(data)).convert("RGBA")
                    for r in self.page()["regions"]:
                        if r.get("target"):
                            tile, _ = render_text(
                                r,
                                punctuation_rules=self.document["settings"].get(
                                    "punctuation_rules"
                                ),
                            )
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
                    self._canvas_cache_key = key
                    self._canvas_cache_data = data
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
            first = self.image_data is None
            self.image_data = data
            self.draw_canvas()
            keep = getattr(self, "_zoom_percent", 0)
            if keep > 0:
                # 切页时画布被清空，选择/视图需要重新套用同一缩放比例
                self.set_zoom(keep)
            elif first:
                self.fit_canvas()

        precise = self.document["settings"].get("removal") == "precise"
        signature = self.background_signature()

        cached_source = self.source_cache.get(page)

        def fetch():
            original = (
                cached_source
                if cached_source is not None
                else self.client.asset(doc, page, "source")
            )
            if variant == "compare":
                # 前后对比：左原图、右成品，一次请求拿两张拼好再交给画布，
                # 避免在 GUI 线程里再跑一遍 PIL 合图。
                response = self.client.backend.http.get(
                    f"/api/v2/manga/{doc}/pages/{page}/render", timeout=60
                )
                if response.status_code == 404:
                    return original, True, original
                response.raise_for_status()
                return (
                    compose_compare(original, response.content),
                    False,
                    original,
                )
            asset = (
                "clean"
                if variant == "edit" and precise
                else "source"
                if variant == "edit"
                else variant
            )
            if asset != "source":
                response = self.client.backend.http.get(
                    f"/api/v2/manga/{doc}/pages/{page}/{asset}", timeout=60
                )
                if response.status_code == 404:
                    return original, True, original
                response.raise_for_status()
                return response.content, False, original
            return original, variant == "edit", original

        def ready(result):
            if self.image_ticket != ticket:
                return
            data, pending, original = result
            self.source_cache[page] = original
            while len(self.source_cache) > 4:
                self.source_cache.pop(next(iter(self.source_cache)))
            if variant in {"edit", "clean"} and precise and not pending:
                self.clean_cache[signature] = data
                while len(self.clean_cache) > 4:
                    self.clean_cache.pop(next(iter(self.clean_cache)))
            self.image_is_source = pending or variant == "source"
            self.clean_signature = (
                signature
                if variant in {"edit", "clean"} and precise and not pending
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

    LABEL_TEXT = {
        "layout_rect": "拖框移动译文 · 右下角缩放 · 右键更多操作",
        "ocr_rect": "正在编辑：OCR 取字范围 · 仅调整读取原文的区域，不动译文（Esc 完成）",
        "erase_rect": "正在编辑：去字背景范围 · 仅调整去除原文的区域，不动译文（Esc 完成）",
    }

    def change_geometry(self):
        self.canvas.rect_key = self.geometry.currentData()
        key = self.canvas.rect_key
        self.geometry_hint.setText(self.LABEL_TEXT[key])
        self.geometry_done.setVisible(key != "layout_rect")
        self.draw_canvas()

    def enter_geometry_mode(self, key):
        # 「建立OCR框」或「蒙版画笔」状态下 canvas.mode 还是 add/mask，拖拽会走到
        # boxCreated 新建一个框，而不是调整当前框的取字/去字范围。
        if self.tool.currentIndex() != 0:
            self.tool.setCurrentIndex(0)
        index = self.geometry.findData(key)
        if index >= 0:
            self.geometry.setCurrentIndex(index)
            self.status.setText(self.LABEL_TEXT[key])

    def exit_geometry_mode(self):
        if self.canvas.rect_key == "layout_rect":
            return
        self.geometry.setCurrentIndex(0)
        self.status.setText("已回到译文排版框编辑")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.document:
            self.exit_geometry_mode()
            self.clear_selection()
            return
        super().keyPressEvent(event)

    def flush_pending_edits(self):
        """回填结束后补做表单加载期间被挡下的编辑。"""
        pending, self.pending_edits = self.pending_edits, []
        for label, action in pending:
            if not self.document or self.loading_form:
                return
            self.edit(label, action)

    def edit(self, label, action):
        if not self.document:
            return
        if self.loading_form:
            # 之前这里是静默 return：导入工程后表单回填期间用户的输入会被吃掉。
            # 现在改为排队，回填结束后立刻执行，避免「打字没反应」。
            self.pending_edits.append((label, action))
            self.status.setText("正在回填界面，稍后自动应用刚才的修改…")
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
        if self.variant.currentData() != "edit":
            self.variant.blockSignals(True)
            self.variant.setCurrentIndex(self.variant.findData("edit"))
            self.variant.blockSignals(False)
            signature = self.background_signature()
            clean = (
                self.clean_cache.get(signature)
                if self.document["settings"].get("removal") == "precise"
                else None
            )
            self.image_data = (
                clean if clean is not None else self.source_cache.get(self.page_id)
            )
            self.image_is_source = clean is None
            self.clean_signature = signature if clean is not None else None
            if self.image_data is not None:
                self.draw_canvas()
            else:
                self.load_image()
        else:
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
            self.canvas.blockSignals(True)
            self.canvas.scene().clear()
            self.canvas.blockSignals(False)
            QTimer.singleShot(0, self.load_image)

    def set_setting(self, key, value):
        self.edit(
            "修改处理设置", lambda: self.document["settings"].__setitem__(key, value)
        )

    def clear_region_text(self, region_id, field):
        target = next(
            (
                r
                for r in (self.page() or {}).get("regions", [])
                if r["id"] == region_id
            ),
            None,
        )
        if not target:
            return

        def action(t=target):
            t[field] = ""
            t["translation_state"], t["translation_error"] = "idle", ""

        self.edit("清除此框" + ("译文" if field == "target" else "原文"), action)
        self.status.setText("已清除此框" + ("译文" if field == "target" else "原文") + "（可撤销）")

    def region_value(self, key, value):
        if self.region():

            def action():
                self.region()[key] = value
                if key in {"source", "target"}:
                    self.region()["translation_state"] = "idle"
                    self.region()["translation_error"] = ""

            self.edit("修改文字框", action)

    def style_value(self, key, value):
        if self.region() and self.variant.currentData() not in ("edit",):
            self.variant.blockSignals(True)
            self.variant.setCurrentIndex(self.variant.findData("edit"))
            self.variant.blockSignals(False)
            self.load_image()
        if self.region():

            def action():
                self.region()["style"][key] = value
                if key == "size":
                    self.region()["style"]["autofit"] = False

            self.edit("修改排版", action)

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
                "vertical_rtl": True,
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
        region["style"] = deepcopy(
            self.page().get("default_style")
            or self.document["settings"].get("default_style")
            or region["style"]
        )
        self.selected = region["id"]
        self.edit("建立文字框", lambda: self.page()["regions"].append(region))
        self.select_region(region["id"])

    def delete_regions(self, ids=None):
        ids = set(
            ids
            if isinstance(ids, (list, tuple, set))
            else self.canvas.selected_ids()
            or ([self.selected] if self.selected else [])
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

    def fix_overflow(self):
        """批量修当前页所有被标了排版警告的框，走撤销栈，可反悔。"""
        if not self.document:
            return
        page = self.page() or {}
        targets = [
            r["id"] for r in page.get("regions", []) if r.get("warnings")
        ]
        if not targets:
            self.status.setText("当前页没有需要调整的溢出文字框")
            return

        def action():
            for region in page["regions"]:
                if region["id"] not in targets:
                    continue
                region["style"]["autofit"] = True
                rect = region["layout_rect"]
                rect["width"] = min(
                    page["width"] - rect["x"], rect["width"] * 1.15
                )
                rect["height"] = min(
                    page["height"] - rect["y"], rect["height"] * 1.15
                )
                region["warnings"] = []

        self.edit("修复溢出文字框", action)
        self.status.setText(f"已调整 {len(targets)} 个溢出文字框，可 Ctrl+Z 撤销")

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
        """删除选中页（支持 Shift/Ctrl 多选），防重入合并连续点击。"""
        if not self.document or not self.document["pages"]:
            return
        rows = sorted({index.row() for index in self.pages.selectedIndexes()})
        if not rows:
            rows = [self.pages.currentRow()]
        ids = [
            self.document["pages"][row]["id"]
            for row in rows
            if 0 <= row < len(self.document["pages"])
        ]
        if not ids:
            return
        if getattr(self, "_deleting_pages", False):
            self._delete_again = True
            return
        self._deleting_pages = True
        try:
            ids = set(ids)
            self.edit(
                "移除页面",
                lambda pending=ids: self.document.__setitem__(
                    "pages",
                    [p for p in self.document["pages"] if p["id"] not in pending],
                ),
            )
            if not self.document["pages"]:
                self.page_id = None
                self.image_data = None
                self.selected = None
                self.canvas.blockSignals(True)
                self.canvas.scene().clear()
                self.canvas.blockSignals(False)
                self.refresh_regions()
                self.status.setText("已删除全部页面，可继续导入新页面")
        finally:
            self._deleting_pages = False
        if getattr(self, "_delete_again", False):
            self._delete_again = False
            QTimer.singleShot(0, self.delete_page)

    def set_default_style(self):
        """把当前选中框的样式设为新框默认；已有框保持不变。"""
        if not self.region():
            self.status.setText("请先选中一个文字框作为样式模板")
            return
        style = deepcopy(self.region()["style"])

        def action():
            self.document["settings"]["default_style"] = deepcopy(style)

        self.edit("设为新框默认样式", action)
        self.status.setText("新建立的文字框将使用该样式；已有框不变")

    def save_style_preset(self):
        """把当前框样式存成命名预设，供整部快速套用。"""
        if not self.region():
            self.status.setText("请先选中一个文字框作为预设模板")
            return
        presets = self.document["settings"].get("style_presets") or []
        name, ok = self.ask_text(
            "保存样式预设",
            "预设名称（可写「旁白」「拟声词」等）",
            f"预设{len(presets) + 1}",
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        style = deepcopy(self.region()["style"])

        def action():
            bucket = self.document["settings"].setdefault("style_presets", [])
            existing = next((p for p in bucket if p["name"] == name), None)
            if existing:
                existing["style"] = deepcopy(style)
            else:
                bucket.append({"id": uuid4().hex, "name": name, "style": deepcopy(style)})

        self.edit("保存样式预设", action)
        self.status.setText(f"已保存样式预设「{name}」")

    def open_style_presets(self):
        if not self.document:
            return
        StylePresetsDialog(self).exec()

    def open_replace(self):
        if not self.document:
            return
        ReplaceDialog(self).exec()

    def open_characters(self):
        if not self.document:
            return
        CharactersDialog(self).exec()

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
            if all_pages:
                self.document["settings"]["default_style"] = deepcopy(style)
            for page in self.document["pages"] if all_pages else [self.page()]:
                page["default_style"] = deepcopy(style)
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
                self.run("clean", saved=True, preview=True)

        task = Background(lambda: self.client.save(base))
        self.tasks.add(task)

        def complete(result, error):
            if sip.isdeleted(self):
                return
            if session != self.session:
                return
            if error:
                self.saving = False
                # 原来这里把 save_callbacks 清空：保存失败后紧跟着的
                # 「保存后执行识别/翻译/导出」永远不会触发，用户看到菜单点了没反应。
                # 现在保留回调，50 ms 后重试保存，直到成功。
                self.status.setText("保存未完成，正在重试：" + error)
                if "(409)" in error:
                    self.recover_workspace(self.saved_snapshot)
                    self.save_callbacks = []
                    return
                self.dirty = True
                self.save_timer.start()
            else:
                saved(result)

        self.connect_task(task, complete)
        self.pool.start(task)

    def import_images(self, checked=False, folder=False):
        if self.job_id or self.submitting or not self.prepare_to_leave():
            return
        if folder:
            path = QFileDialog.getExistingDirectory(self, "导入文件夹")
            if not path:
                return
            self.import_paths([path])
            return
        paths = pick_image_paths(self)
        if paths:
            self.import_paths(paths)
        else:
            self.status.setText("也可拖入图片、PDF 或文件夹到导入页面")

    def import_paths(self, paths):
        if not paths or self.job_id or self.submitting or not self.prepare_to_leave():
            return
        default_name = ""
        if not self.document:
            first = Path(paths[0])
            default_name = first.stem if first.is_file() else first.name
        dialog = ImportDialog(self, len(paths), default_name)
        if dialog.exec() != QDialog.Accepted:
            return
        settings = (
            deepcopy(self.document["settings"])
            if self.document
            else {
                "detector": self.detector.currentData(),
                "ocr": self.ocr.currentData(),
                "source_language": dialog.source_language.currentData(),
                "target_language": dialog.target_language.currentData(),
                "confidence": self.confidence.value(),
                "right_to_left": self.rtl.isChecked(),
                "detect_panels": self.panels.isChecked(),
                "auto_recognize": self.auto_recognize.isChecked(),
            }
        )
        for key in ("source_language", "target_language"):
            settings[key] = getattr(dialog, key).currentData()
        payload = {
            "paths": paths,
            "title": dialog.name_edit.text().strip() or None,
            "settings": settings,
        }
        if self.document and dialog.append.isChecked():
            payload["workspace_id"] = self.document["id"]

        def imported(result):
            self.loaded(result["workspace"])
            self.refresh_history()
            if not any(p["regions"] for p in self.document["pages"]):
                answer = self.ask(
                    "开始识别",
                    "导入完成。要立即检测文字框并读取原文吗？",
                    yes="立即识别",
                )
                if answer == QMessageBox.Yes:
                    self.run("recognize", scope=1)

        self.start_request(
            lambda: self.client.request(
                "POST", "/import", json=payload, headers={"Prefer": "respond-async"}
            ),
            imported,
        )

    def run(
        self,
        stage,
        saved=False,
        preview=False,
        scope=None,
        region_ids=None,
        after=None,
        replace=None,
        ocr_engine=None,
        provider_id=None,
    ):
        if not self.document:
            return
        if self.job_id or self.submitting:
            # 原来这里静默 return：任务在跑时点菜单毫无反应，用户以为界面卡死
            self.status.setText("已有任务在执行，请等它结束或先取消当前任务")
            self.set_status_tone("warn")
            return
        if not saved:
            self.save(
                lambda: self.run(
                    stage,
                    True,
                    preview,
                    scope,
                    region_ids,
                    after,
                    replace,
                    ocr_engine,
                    provider_id,
                )
            )
            return
        scope = 0 if preview else self.scope.currentIndex() if scope is None else scope
        if replace is None:
            replace = region_ids is not None
        pages = [] if scope == 1 else [self.page_id]
        regions = (
            list(region_ids)
            if region_ids is not None
            else self.canvas.selected_ids()
            if scope == 2
            else []
        )
        if region_ids is not None and scope == 1:
            pages = []
        if scope == 2 and not regions:
            self.status.setText("请先选中文字框")
            return
        self.current_job_stage = stage
        self.set_status_tone("busy")
        if stage in {"recognize", "detect", "ocr"}:
            self.follow_progress = True
            self.follow_banner.hide()
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
                    self.variant.findData(
                        "edit" if preview else "clean" if stage == "clean" else "render"
                    )
                )
                self.variant.blockSignals(False)
            self.load_image()
            self.show_report(result)
            if after and not result.get("report", {}).get("errors"):
                self.save(after)
            if self.dirty:
                self.save_timer.start()

        options = {
            "replace_regions": replace,
            "overwrite_source": replace,
            "retranslate": replace,
        }
        if ocr_engine:
            options["ocr_engine"] = ocr_engine
        if provider_id:
            options["provider_id"] = provider_id
        self.start_request(
            lambda: self.client.run(base, stage, pages, regions, options),
            completed,
        )

    def start_request(self, action, callback, on_error=None):
        if self.job_id or self.submitting:
            self.status.setText("请等待当前任务结束，或先取消")
            return
        self.submitting = True
        self.cancel_pending = False
        self.job_base = deepcopy(self.document)
        self.job_manual_paths = set()
        session = self.session
        self.status.setText("正在提交任务…")
        self.progress_panel.start("正在提交任务…")
        self.position_progress_panel()

        def started(result):
            if session != self.session:
                return
            self.submitting = False
            job = result.get("job", {})
            self.job_id = job.get("id") or job.get("job_id")
            self.job_callback = callback
            self.job_error_callback = on_error
            if self.job_id:
                self.poll_timer.start()
                if self.cancel_pending:
                    self.cancel_job()
            else:
                self.progress_panel.hide()
                callback(result)

        task = Background(action)
        self.tasks.add(task)

        def done(result, error):
            if sip.isdeleted(self):
                return
            self.submitting = False
            if error:
                self.progress_panel.hide()
                self.status.setText(error)
                if on_error:
                    on_error(error)
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
            if progress.get("page_id"):
                self.processing_page_id = progress["page_id"]
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
            if progress.get("error_requests"):
                detail += f" · 失败 {progress['error_requests']} 次"
            self.status.setText(state + (" · " + detail if detail else ""))
            model_names = {
                m["id"]: m.get("name", m["id"])
                for m in (getattr(self, "model_catalogue", None) or [])
                if m.get("id")
            }
            page_numbers = (
                {p["id"]: i + 1 for i, p in enumerate(self.document["pages"])}
                if self.document
                else {}
            )
            self.progress_panel.update_progress(
                state,
                progress,
                model_names=model_names,
                page_numbers=page_numbers,
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
                self.follow_banner.hide()
                result = job.get("result") or {}
                if isinstance(result, dict) and "workspace" in result:
                    self.job_callback(result)
                    if self.dirty:
                        self.save_timer.start()
                elif job["state"] == "succeeded" and isinstance(result.get("models"), list):
                    self.progress_panel.hide()
                    self.job_callback(result)
                elif getattr(self, "job_error_callback", None):
                    self.progress_panel.hide()
                    self.job_error_callback(job.get("error") or state)
                else:
                    # 状态是 succeeded 但没带回 workspace：以前会拿 {} 直接回调，
                    # completed() 里 result["workspace"] 抛 KeyError 又被兜住，
                    # 任务成果就这样丢了，界面只显示「界面更新失败」。
                    message = str(
                        job.get("error")
                        or (
                            "任务已结束但未返回工程快照，正在按磁盘内容恢复"
                            if job["state"] == "succeeded"
                            else "任务已取消，可重新执行失败阶段"
                        )
                    )
                    self.status.setText(message)
                    self.progress_panel.finish(
                        "error" if job["state"] == "failed" else "warn",
                        state,
                        message,
                        auto_hide_ms=0,
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
        if self.submitting and not identifier:
            self.cancel_pending = True
            self.status.setText("已请求取消，等待任务提交完成…")
            return
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

    @staticmethod
    def summarize_report(report, pages=None):
        # 逐页提示合并成带计数的摘要，避免同一句话刷满状态栏
        from collections import Counter

        page_index = {
            p["id"]: i + 1 for i, p in enumerate(pages or []) if isinstance(p, dict) and "id" in p
        }
        parts = []
        messages = [str(e.get("message", e)) for e in report.get("errors", [])]
        if messages:
            top, count = Counter(messages).most_common(1)[0]
            parts.append(top + (f"（{count} 处）" if count > 1 else ""))
        empty = report.get("empty_pages") or []
        if empty:
            labels = [str(page_index.get(pid, pid)) for pid in empty[:4]]
            numbers = "、".join(labels)
            suffix = f" 等 {len(empty)}" if len(empty) > 4 else ""
            parts.append(
                f"第 {numbers}{suffix} 页未检出文字框：可降低检测阈值或换检测模型"
            )
        skipped = report.get("skipped_pages") or []
        if skipped:
            parts.append(
                f"{len(skipped)} 页已有文字框，已跳过检测（替换前需先清空该页）"
            )
        fallback = report.get("fallback_pages") or []
        if fallback:
            parts.append(f"{len(fallback)} 页由面板模型回退到行级检测补出文字框")
        warnings = report.get("warnings", [])
        if warnings:
            top, count = Counter(
                str(w.get("message", w)) for w in warnings
            ).most_common(1)[0]
            parts.append(top + (f"（{count} 页）" if count > 1 else ""))
        return "；".join(parts[:3])

    def show_report(self, result):
        report = result.get("report", {})
        self.last_task_report = deepcopy(report)
        pages = (self.document or {}).get("pages", [])
        stage = getattr(self, "current_job_stage", "")
        stage_done_titles = {
            "detect": "检测完成",
            "ocr": "OCR 识别完成",
            "recognize": "检测并 OCR 完成",
            "translate": "翻译完成",
            "clean": "精准去字完成",
            "render": "排版渲染完成",
            "pipeline": "翻译并排版完成",
        }
        title = stage_done_titles.get(stage, "处理完成")
        detail = ""
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
            detail = (
                f"成功 {succeeded} 框 · 失败 {failed} 框 · 跳过 {translation.get('skipped', 0)} 框"
                if total
                else "没有需要翻译的原文；请先 OCR，已有译文默认保留"
            )
        else:
            completed = len(report.get("completed") or [])
            detail = f"已完成 {completed} 页处理" if completed else "处理完成"

        errors = report.get("errors") or []
        warnings = report.get("warnings") or []
        empty = report.get("empty_pages") or []
        fallback = report.get("fallback_pages") or []
        skipped = report.get("skipped_pages") or []
        ocr_empty_count = sum(
            1
            for p in pages
            for r in p.get("regions", [])
            if any("OCR 未识别出文字" in str(w) for w in (r.get("warnings") or []))
        )
        notices = []
        if errors:
            notices.append(f"失败 {len(errors)} 项：{errors[0].get('message', errors[0])}")
        if empty:
            page_index = {p["id"]: i + 1 for i, p in enumerate(pages)}
            labels = "、".join(str(page_index.get(pid, pid)) for pid in empty[:4])
            suffix = f" 等 {len(empty)}" if len(empty) > 4 else ""
            notices.append(f"第 {labels}{suffix} 页未检出文字框（可降低阈值或换检测模型）")
        if fallback:
            notices.append(f"{len(fallback)} 页由面板模型回退到行级检测补出文字框")
        if skipped:
            notices.append(f"{len(skipped)} 页已有文字框，已跳过检测")
        if ocr_empty_count and stage in {"ocr", "recognize"}:
            notices.append(f"{ocr_empty_count} 个文字框 OCR 未识别出文字")
        elif warnings and not errors:
            notices.append(f"{len(warnings)} 条提示：{warnings[0].get('message', warnings[0])}")

        if errors:
            tone = "error"
            self.set_status_tone("error")
        elif warnings or empty or fallback or (ocr_empty_count and stage in {"ocr", "recognize"}):
            tone = "warn"
            self.set_status_tone("warn")
        else:
            tone = "ok"
            self.set_status_tone("ok")

        summary = self.summarize_report(report, pages=pages)
        self.status.setText(summary or "处理完成，请校对后继续")
        self.progress_panel.finish(tone, title, detail, notices=notices)

    def export(self):
        if not self.document:
            return
        labels = [
            "PNG图片",
            "JPEG图片",
            "整批ZIP",
            "CBZ 漫画包（阅读器可直接打开）",
            "PDF 文档",
            "可编辑工程包",
        ]
        modes = ["png", "jpeg", "zip", "cbz", "pdf", "project"]
        label, ok = self.ask_choice(
            "导出漫画", "选择导出格式", labels, labels[0]
        )
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择新的导出目录")
        if folder:
            mode = modes[labels.index(label)]

            def exported(result):
                files = ", ".join(result.get("files", []))
                quality = result.get("quality") or []
                if quality:
                    pages = {item.get("page_name") or item.get("page_id") for item in quality}
                    self.set_status_tone("warn")
                    self.status.setText(
                        f"已导出：{files}　·　{len(quality)} 处文字溢出（{len(pages)} 页），"
                        "可在「修复溢出」里批量处理"
                    )
                else:
                    self.set_status_tone("ok")
                    self.status.setText("已导出：" + files)

            self.save(
                lambda: self.start_request(
                    lambda: self.client.request(
                        "POST",
                        f"/{self.document['id']}/export",
                        json={"output_dir": folder, "mode": mode},
                        headers={"Prefer": "respond-async"},
                    ),
                    exported,
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

        key = (id(data), self.background_signature())
        if getattr(self, "fill_cache_key", None) == key:
            return self.fill_cache_data
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
        self.fill_cache_key, self.fill_cache_data = key, output.getvalue()
        return self.fill_cache_data

    def toggle_precise(self, checked):
        if self.loading_form or not self.document:
            return
        self.set_setting("removal", "precise" if checked else "fill")
        if self.variant.currentData() in {"edit", "render", "clean"}:
            self.variant.setCurrentIndex(self.variant.findData("edit"))
            self.load_image()
        if checked:
            self.status.setText(
                "已启用精准去字：正在生成去字底图，期间显示色块遮挡，可继续编辑或切页"
            )
            signature = self.background_signature()
            if signature not in self.clean_cache:
                self.preview_pending = True
                self.save_timer.start()
        else:
            self.status.setText("已切回色块遮挡")

    def resume_follow(self):
        self.follow_progress = True
        self.follow_banner.hide()
        page_id = getattr(self, "processing_page_id", None)
        if page_id and any(p["id"] == page_id for p in self.document["pages"]):
            self.page_id = page_id
            self.image_data = None
            self.refresh_pages()
            self.load_image()
        self.status.setText("已恢复自动跳到正在识别的页面")

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

    def detach_job(self, cancel=True):
        if cancel and self.job_id:
            # 只清本地状态会让后端继续跑完整轮推理并写 workspace，
            # 前端 revision 之后必然落后，下一次保存就撞 409。
            job = self.job_id
            self.background(
                lambda: self.client.backend.request("POST", f"/api/jobs/{job}/cancel"),
                lambda r: None,
            )
        self.session += 1
        self.poll_timer.stop()
        self.job_id = None
        self.submitting = False
        self.progress_panel.hide()
        self.polling = False
        self.syncing = False
        self.preview_pending = False
        self.save_callbacks = []

    def regenerate_clean_background(self):
        """重新生成当前页精准去字底图（精准去字的重试入口）。"""
        if not self.document:
            return
        if self.document["settings"].get("removal") != "precise":
            self.precise.setChecked(True)
        self.status.setText("正在重新生成当前页去字底图…")
        self.run("clean", scope=0)

    def delete_workspace(self, workspace_id, title):
        if (
            self.ask_yes_no(
                "删除漫画工程",
                f"永久删除“{title}”的工程、识别结果和缓存？原始图片及导出文件会保留。",
                yes="删除", no="保留",
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
        if hasattr(self, "_init_timer"):
            self._init_timer.stop()
        self.task_callbacks.clear()
        for task in list(self.tasks):
            try:
                task.signals.done.disconnect(self.task_finished)
            except (TypeError, RuntimeError):
                pass
        self.tasks.clear()
        self.save_timer.stop()
        self.poll_timer.stop()
        if hasattr(self, "progress_panel") and not sip.isdeleted(self.progress_panel):
            self.progress_panel.cleanup()
        if hasattr(self, "canvas") and not sip.isdeleted(self.canvas):
            try:
                self.canvas.scene().selectionChanged.disconnect(
                    self.canvas.selection_changed
                )
            except (TypeError, RuntimeError):
                pass
            self.canvas.setScene(None)
        for sig, slot in (
            (qconfig.themeChanged, self.apply_theme),
            (qconfig.themeChanged, getattr(self.canvas, "apply_theme", None)),
            (
                qconfig.themeColorChanged,
                getattr(
                    getattr(self.start_page, "drop_card", None),
                    "_refresh_accent",
                    None,
                ),
            ),
        ):
            if slot is None:
                continue
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
