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
    QColorDialog,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    DropDownPushButton,
    FluentIcon,
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
        layout = QVBoxLayout(self)
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
        layout.addWidget(QLabel(text))
        layout.addWidget(
            QLabel("可以换一个引擎重试，其他框保持不变。")
        )
        form = QFormLayout()
        self.engine = QComboBox()
        self.fill_engines()
        form.addRow("引擎", self.engine)
        layout.addLayout(form)
        buttons = QHBoxLayout()
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
        self.setObjectName("mangaApiConfigDialog")
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"{model['advice']}\n\n填写后即可在项目设置里选择该 OCR。"
                "API Key 只保存在本机用户目录。"
            )
        )
        form = QFormLayout()
        self.appid = QLineEdit()
        self.appid.setPlaceholderText("百度填 AppID；腾讯填 SecretId；其他可留空")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-… / 百度 API Key / SecretKey")
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("留空使用官方地址；也可填兼容网关")
        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("如 gpt-4o-mini / gemini-2.0-flash")
        form.addRow("AppID", self.appid)
        form.addRow("API Key", self.api_key)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名", self.model_name)
        layout.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        clear = QPushButton("清除配置")
        clear.clicked.connect(lambda: self.save({}))
        save = PrimaryPushButton("保存")
        save.clicked.connect(self.save_from_form)
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
        self.setObjectName("mangaModelDialog")
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        self.page = page
        self.setWindowTitle("漫画模型中心")
        self.resize(1120, 620)
        layout = QVBoxLayout(self)
        self.intro = QLabel(
            "检测模型与本地 OCR 按需下载；云 API OCR 选择后在「配置 API」填密钥。"
            "悬停任意模型可查看功能说明。"
        )
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["模型", "类型", "状态", "大小", "功能说明", "来源许可", "操作"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 360)
        self.table.setColumnWidth(6, 96)
        layout.addWidget(self.table, 1)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        self.api_button = QPushButton("配置 API")
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
        self.status = QLabel()
        self.status.setWordWrap(True)
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

    def row_state(self, model):
        kind = model.get("kind", "local")
        if not model.get("supported", True):
            return "尚未适配", True
        if kind == "api":
            if not model.get("installed"):
                return "未启用（点「配置 API」）", False
            if not model.get("api_ready"):
                return "⚠ 需要配置 API", True
            return "已配置", False
        if kind == "system":
            return ("系统可用" if model.get("installed") else "系统不支持"), False
        if kind == "package":
            if model.get("installed"):
                return "依赖已就绪", False
            return "需安装 pip 包", True
        return ("已安装" if model.get("installed") else "未安装"), False

    def configure_api(self):
        self.configure_api_for(self.selected())

    def configure_api_for(self, model):
        if not model or model.get("kind") != "api":
            self.status.setText("请选择一个云 API OCR 模型后再配置")
            return
        ApiConfigDialog(self.page, model).exec()
        self.refresh()

    def download_for(self, model):
        if not model:
            return
        self.status.setText(
            f"正在下载 {model['name']}（{model['size_mb']} MB），可在主页面取消…"
        )
        self.page.start_request(
            lambda: self.page.client.request(
                "POST",
                f"/models/{model['id']}/download",
                headers={"Prefer": "respond-async"},
            ),
            lambda r: self.refresh(),
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
        background = "#202020" if isDarkTheme() else "#f7f9fc"
        self.setStyleSheet(f"QDialog#mangaModelDialog {{background:{background};}}")

    def refresh(self):
        self.page.background(
            lambda: self.page.client.request("GET", "/models"), self.loaded
        )

    GROUPS = [
        ("检测模型（按语言选择）", lambda m: m.get("role") == "detect"),
        (
            "本地 OCR（内置 / ONNX 权重）",
            lambda m: m.get("role") == "ocr" and m.get("kind", "local") == "local",
        ),
        ("pip 包 OCR（首次使用时自动下载模型）", lambda m: m.get("kind") == "package"),
        ("云 API OCR（需配置密钥并验证）", lambda m: m.get("kind") == "api"),
        ("系统 OCR（无需下载）", lambda m: m.get("kind") == "system"),
        ("去字模型", lambda m: m.get("role") == "inpaint"),
    ]

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
        if model.get("url"):
            return "一键下载", self.download_for
        return "导入目录", self.import_for

    def loaded(self, data):
        models = list(data.get("models", []))
        self.models = models
        self.table.clearSpans()
        self.table.setRowCount(0)
        rows = []
        for title, predicate in self.GROUPS:
            items = [m for m in models if predicate(m)]
            if not items:
                continue
            items.sort(key=lambda m: (m.get("advanced", False), m.get("name", "")))
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
                cell.setBackground(QColor(0, 0, 0, 12))
                self.table.setItem(row, 0, cell)
                self.table.setSpan(row, 0, row, self.table.columnCount())
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
                model["advice"],
                model["license"],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 2 and warn:
                    item.setForeground(red)
                tooltip = model["advice"]
                if model.get("langs"):
                    tooltip += "\n支持语言：" + ", ".join(model["langs"])
                if model.get("source"):
                    tooltip += "\n来源：" + model["source"]
                if kind == "api" and not model.get("api_ready"):
                    tooltip += "\n⚠ 需要配置 API 并通过验证后才会出现在项目设置里"
                item.setToolTip(tooltip)
                self.table.setItem(row, col, item)
            if kind == "api" and not model.get("api_ready"):
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is not None and col != 6:
                        cell.setBackground(QColor(231, 72, 86, 24))
            text, callback = self.action_for(model)
            button = QPushButton(text)
            button.setMinimumHeight(26)
            if callback is None:
                button.setEnabled(False)
            else:
                button.clicked.connect(
                    lambda checked=False, action=callback, target=model: action(target)
                )
            self.table.setCellWidget(row, 6, button)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(4, 360)
        self.table.setColumnWidth(6, 96)
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

    def selected(self):
        row = self.table.currentRow()
        return getattr(self, "row_models", {}).get(row)

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
        self.status.setText(f"正在下载 {model['name']}（{model['size_mb']} MB），可在主页面取消…")
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


class PunctuationRulesDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.baseline = []
        self.setWindowTitle("自动替换标点 · AiNiee 公共译后替换表")
        self.setObjectName("mangaPunctuationDialog")
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)
        self.resize(720, 500)
        layout = QVBoxLayout(self)
        note = QLabel(
            "这里直接编辑公共表“漫画标点替换”。启用后，漫画排版和文本翻译共同使用这些规则；其他公共表不受影响。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.enabled = QCheckBox("启用自动替换标点")
        layout.addWidget(self.enabled)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["替换前", "替换后", "正则"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
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
        self.status = QLabel("正在读取规则…")
        layout.addWidget(self.status)
        page.background(lambda: page.client.request("GET", "/punctuation"), self.loaded)

    def apply_theme(self, *_):
        background = "#202020" if isDarkTheme() else "#f7f9fc"
        self.setStyleSheet(
            f"QDialog#mangaPunctuationDialog {{background:{background};}}"
        )

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
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel(f"已选择 {count} 个文件，请确认项目信息"))
        form = QFormLayout()
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
        self.append = QCheckBox("追加到当前工程", self)
        self.append.setVisible(bool(getattr(parent, "document", None)))
        layout.addWidget(self.append)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = PrimaryPushButton("开始导入")
        ok.clicked.connect(self.accept)
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
        self.source_cache = {}
        self.clean_cache = {}
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
        QTimer.singleShot(0, self.refresh_fonts)
        QTimer.singleShot(0, self.refresh_model_catalogue)

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
        for model_id in ("manga-panel", "legacy-balloon-yolov4"):
            self.detector.addItem(model_id, model_id)
        self.ocr = QComboBox()
        for model_id in ("mangaocr", "rapidocr"):
            self.ocr.addItem(model_id, model_id)
        self.detector.currentIndexChanged.connect(
            lambda: self.set_setting("detector", self.detector.currentData())
        )
        self.ocr.currentIndexChanged.connect(
            lambda: self.set_setting("ocr", self.ocr.currentData())
        )
        self.model_catalogue = []
        self.source_language = QComboBox()
        self.source_language.addItems(["ja", "zh", "en", "ko"])
        self.target_language = QComboBox()
        self.target_language.addItems(["zh-CN", "en", "ja", "ko"])
        self.source_language.currentTextChanged.connect(
            lambda v: self.set_setting("source_language", v)
        )
        self.target_language.currentTextChanged.connect(
            lambda v: self.set_setting("target_language", v)
        )
        self.panels = QCheckBox("检测分镜并组织阅读顺序")
        self.panels.setChecked(True)
        self.panels.toggled.connect(lambda v: self.set_setting("detect_panels", v))
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(0.05, 1)
        self.confidence.setSingleStep(0.05)
        self.confidence.setValue(0.5)
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
        # 锁定：原文 / 译文 行内小锁；区域锁定只在右键菜单
        self.source_lock = QCheckBox(self)
        self.source_lock.setToolTip(
            "锁定后，整部识别 / OCR 与重做识别会跳过此框，保留此处原文"
        )
        self.target_lock = QCheckBox(self)
        self.target_lock.setToolTip(
            "锁定后，整部翻译与重做翻译会跳过此框，保留此处译文"
        )
        self.geometry_lock = QCheckBox(self)
        self.source_lock.toggled.connect(
            lambda v: self.region_value("source_locked", v)
        )
        self.target_lock.toggled.connect(
            lambda v: self.region_value("target_locked", v)
        )
        self.geometry_lock.toggled.connect(
            lambda v: self.region_value("geometry_locked", v)
        )
        self.font = QComboBox()
        self.font.addItem("内置字体", "")
        self.font.currentIndexChanged.connect(
            lambda: self.style_value("font", self.font.currentData() or "")
        )
        self.size = QSpinBox()
        self.size.setRange(6, 512)
        self.size.setValue(28)
        self.size.setFixedWidth(120)
        self.size.setToolTip(
            "固定字号模式下生效；自动适配模式下字号随框自动调整"
        )
        self.size.valueChanged.connect(lambda v: self.style_value("size", v))
        self.minimum = QSpinBox()
        self.minimum.setRange(6, 512)
        self.minimum.setValue(10)
        self.minimum.valueChanged.connect(lambda v: self.style_value("min_size", v))
        # 字号模式：自动适配 / 固定字号（互斥按钮）
        self.autofit = TransparentPushButton("自动适配", self)
        self.autofit.setCheckable(True)
        self.autofit.setToolTip("字号随框自动放大缩小")
        self.autofit.toggled.connect(
            lambda checked: checked and self.style_value("autofit", True)
        )
        self.autofit.toggled.connect(self.refresh_font_mode_hint)
        self.fixed_size_button = TransparentPushButton("固定字号", self)
        self.fixed_size_button.setCheckable(True)
        self.fixed_size_button.setToolTip("手动指定字号；字号输入框中修改")
        self.fixed_size_button.toggled.connect(
            lambda checked: checked and self.style_value("autofit", False)
        )
        self.fixed_size_button.toggled.connect(self.refresh_font_mode_hint)
        self.horizontal_button = TransparentPushButton("横", self)
        self.horizontal_button.setCheckable(True)
        self.horizontal_button.setToolTip("横排文字")
        self.horizontal_button.clicked.connect(
            lambda: self.style_value("vertical", False)
        )
        self.vertical_button = TransparentPushButton("竖", self)
        self.vertical_button.setCheckable(True)
        self.vertical_button.setToolTip("竖排文字")
        self.vertical_button.clicked.connect(lambda: self.style_value("vertical", True))
        self.bold_button = TransparentPushButton("B", self)
        self.bold_button.setCheckable(True)
        self.bold_button.setToolTip("粗体")
        self.bold_button.clicked.connect(
            lambda checked: self.style_value("bold", checked)
        )
        self.color_button = TransparentToolButton(FluentIcon.PALETTE, self)
        self.color_button.setToolTip("文字颜色")
        self.color_button.clicked.connect(lambda: self.pick_color("color"))
        self.vertical_rtl = QCheckBox("竖排从右往左", self)
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
        self.rotation = QSpinBox()
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
        self.regions.setColumnCount(2)
        self.regions.setHorizontalHeaderLabels(["序号", "内容"])
        self.regions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.regions.setMinimumHeight(110)
        self.regions.setMaximumHeight(260)
        self.regions.setWordWrap(True)
        self.regions.setSelectionBehavior(QTableWidget.SelectRows)
        self.regions.setColumnWidth(0, 48)
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
            FluentIcon.SETTING, "项目设置", self
        )
        self.project_summary_button.setToolTip("检测模型、语言、阈值与翻译接口")
        self.project_summary_button.clicked.connect(self.open_project_settings)
        heading.addWidget(self.project_summary_button)
        heading.addStretch(1)
        root.addLayout(heading)

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
        one_click = PrimarySplitPushButton("一键翻译", self)
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
        self.cancel_button = self.icon_button(
            commands, FluentIcon.CLOSE, "取消当前任务", self.cancel_job
        )
        for sequence, callback in [
            ("Ctrl+S", self.save),
            ("Ctrl+Z", self.undo.undo),
            ("Ctrl+Y", self.undo.redo),
            ("Ctrl+Shift+Z", self.undo.redo),
        ]:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)

        # ---- 三栏布局 ----
        split = QSplitter(Qt.Horizontal)
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
        self.geometry = QComboBox()
        for title, key in [
            ("译文排版框", "layout_rect"),
            ("OCR 取字范围", "ocr_rect"),
            ("去字背景范围", "erase_rect"),
        ]:
            self.geometry.addItem(title, key)
        self.geometry.hide()
        self.geometry.currentIndexChanged.connect(self.change_geometry)
        self.geometry_hint = QLabel("拖框移动译文 · 右下角缩放 · 右键更多操作")
        self.geometry_hint.setWordWrap(True)
        self.geometry_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tools.addWidget(self.geometry_hint, 1)
        self.geometry_done = TransparentToolButton(FluentIcon.ACCEPT_MEDIUM, self)
        self.geometry_done.setToolTip("完成并返回译文排版 (Esc)")
        self.geometry_done.hide()
        self.geometry_done.clicked.connect(self.exit_geometry_mode)
        tools.addWidget(self.geometry_done)
        tools.addWidget(self.precise)
        self.brush = QSpinBox()
        self.brush.setRange(1, 256)
        self.brush.setFixedWidth(120)
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
        # 画布右下角统一：视图切换 + 适应窗口 + 缩放
        self.zoom_overlay = SimpleCardWidget(self.canvas.viewport())
        zoom_row = QHBoxLayout(self.zoom_overlay)
        zoom_row.setContentsMargins(6, 4, 6, 4)
        zoom_row.setSpacing(6)
        zoom_row.addWidget(self.variant)
        self.icon_button(zoom_row, FluentIcon.FIT_PAGE, "适应窗口", self.fit_canvas)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(self.ZOOM_MIN, self.ZOOM_MAX)
        self.zoom_slider.setFixedWidth(88)
        self.zoom_slider.setValue(100)
        self.zoom_label = QLabel("100%")
        self.zoom_slider.valueChanged.connect(self.set_zoom)
        self.canvas.zoomChanged.connect(self.update_zoom)
        zoom_row.addWidget(self.zoom_slider)
        zoom_row.addWidget(self.zoom_label)
        self.zoom_overlay.adjustSize()
        self.pan_hint = SimpleCardWidget(self.canvas.viewport())
        pan_row = QHBoxLayout(self.pan_hint)
        pan_row.setContentsMargins(10, 6, 10, 6)
        self.pan_hint_label = QLabel("按住鼠标中键拖动画面")
        pan_row.addWidget(self.pan_hint_label)
        self.pan_hint.adjustSize()
        self.pan_hint.hide()
        # ---- 右栏：文字 / 排版 / 阅读顺序（滚动，阅读顺序默认折叠）----
        scroll = QScrollArea()
        scroll.setMinimumWidth(360)
        scroll.setMaximumWidth(560)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea {border:none;background:transparent;}")
        right_card = SimpleCardWidget()
        right_card.setBorderRadius(12)
        form = QFormLayout(right_card)
        form.setContentsMargins(14, 14, 14, 14)
        form.setVerticalSpacing(9)
        # ==== 文字 ====
        text_header = QHBoxLayout()
        text_header.addWidget(StrongBodyLabel("文字", right_card))
        text_header.addStretch(1)
        form.addRow(text_header)
        form.addRow("原文", self.source)
        form.addRow("译文", self.target)
        # ==== 排版 ====
        typography_header = QHBoxLayout()
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
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addRow("建议字号下限", self.minimum)
        advanced_form.addRow("行距", self.spacing)
        advanced_form.addRow("字距", self.charspace)
        advanced_form.addRow("描边宽度", self.stroke)
        self.button(advanced_form, "描边颜色", lambda: self.pick_color("stroke_color"))
        advanced_form.addRow("旋转角度", self.rotation)
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
        # QFormLayout creates plain QLabel objects from strings. Replace those
        # labels so they participate in Fluent's light/dark text styling too.
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.LabelRole)
            label = item.widget() if item else None
            if isinstance(label, NativeLabel) and not isinstance(label, QLabel):
                replacement = QLabel(label.text(), right_card)
                form.removeWidget(label)
                label.hide()
                label.deleteLater()
                form.setWidget(row, QFormLayout.LabelRole, replacement)
        split.setSizes([170, 880, 430])
        # ==== 任务进度面板（画布右下角浮动，随时可取消）====
        self.progress_label = QLabel("尚未开始翻译")
        self.progress_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_panel = SimpleCardWidget(self.canvas.viewport())
        progress_row = QHBoxLayout(self.progress_panel)
        progress_row.setContentsMargins(12, 8, 12, 8)
        progress_row.setSpacing(8)
        progress_row.addWidget(self.progress_label, 1)
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.cancel_button)
        self.progress_panel.hide()
        self.last_report = {}
        self.failed_region_ids = []
        self.status = QLabel("导入图片、PDF 或文件夹后开始编辑；识别与翻译在顶栏。")
        self.status.setWordWrap(True)
        log_row = QHBoxLayout()
        log_row.setContentsMargins(16, 4, 16, 6)
        log_row.addWidget(self.status, 1)
        self.button(log_row, "任务明细", self.show_task_details)
        outer.addLayout(log_row)


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

    def confirm_task(self, title, message, checkbox=None):
        """任务确认框：可选内置一次性勾选项。返回 (按钮, 是否勾选)。"""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
        )
        box.setDefaultButton(QMessageBox.Yes)
        check = None
        if checkbox:
            check = QCheckBox(checkbox, box)
            box.setCheckBox(check)
        answer = box.exec()
        return answer, (check.isChecked() if check else False)

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
                answer = QMessageBox.question(
                    self,
                    "补齐检测",
                    f"{undetected} 页没有文字框，要先检测这些页面再读取原文吗？已检测页面保持不变。",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Yes,
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
            answer = QMessageBox.question(
                self,
                "尚未识别文字框",
                "当前页没有文字框，要先检测再进行 OCR 吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
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
        if hasattr(self, "document"):
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
        if not self.document:
            return
        names = {
            "target": "译文",
            "source": "OCR 原文",
            "regions": "文字框（包括框内原文和译文）",
        }
        if scope is None:
            scope, ok = QInputDialog.getItem(
                self,
                "清除" + names[kind],
                "清除范围",
                ["选中的框", "当前页", "整部漫画"],
                1,
                False,
            )
            if not ok:
                return
        ids = set(
            self.canvas.selected_ids() or ([self.selected] if self.selected else [])
        )
        if scope == "选中的框" and not ids:
            self.status.setText("请先选中文字框")
            return
        if (
            QMessageBox.question(
                self,
                "确认清除",
                f"清除{scope}的{names[kind]}？可以撤销。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

        def action():
            for page in (
                self.document["pages"] if scope == "整部漫画" else [self.page()]
            ):
                if kind == "regions":
                    page["regions"] = [
                        r
                        for r in page["regions"]
                        if scope == "选中的框" and r["id"] not in ids
                    ]
                else:
                    for region in page["regions"]:
                        if scope == "选中的框" and region["id"] not in ids:
                            continue
                        region[kind] = ""
                        region["translation_state"], region["translation_error"] = (
                            "idle",
                            "",
                        )

        self.edit("清除" + names[kind], action)

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
            answer = QMessageBox.question(
                self,
                "补齐后翻译",
                "、".join(parts) + "。是否补齐后翻译整部？\n已处理的文字框、原文和人工修改保持不变。",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
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
        compact = self.page_panel.width() < 110
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
        self.update_zoom(self.canvas.transform().m11())

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
        self.zoom_overlay.move(
            max(0, self.canvas.viewport().width() - self.zoom_overlay.width() - 10),
            max(0, self.canvas.viewport().height() - self.zoom_overlay.height() - 10),
        )
        self.zoom_overlay.raise_()

    def position_pan_hint(self):
        self.pan_hint.adjustSize()
        self.pan_hint.move(
            max(10, (self.canvas.viewport().width() - self.pan_hint.width()) // 2),
            14,
        )
        self.pan_hint.raise_()

    def maybe_show_pan_hint(self, scale):
        """画面比“适应窗口”更大时，提示可以用中键拖动。"""
        if not getattr(self, "pan_hint", None) or not self.image_data:
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
        self.pan_hint_label.setText("按住鼠标中键拖动画面")
        self.pan_hint.show()
        self.position_pan_hint()
        QTimer.singleShot(3200, self.pan_hint.hide)

    def position_progress_panel(self):
        self.progress_panel.adjustSize()
        self.progress_panel.move(
            max(0, self.canvas.viewport().width() - self.progress_panel.width() - 10),
            max(
                0,
                self.canvas.viewport().height()
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
        else:
            self.load_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
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
        pages = {p["id"]: p for p in (self.document or {}).get("pages", [])}
        for error in getattr(self, "last_task_report", {}).get("errors", []):
            page = pages.get(error.get("page_id"), {"id": "", "name": "任务"})
            rows.append(
                (
                    page,
                    {
                        "id": None,
                        "order": -1,
                        "translation_state": "failed",
                        "translation_error": error.get("message", str(error)),
                    },
                )
            )
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
        if models is None:
            def loaded(data):
                self.model_catalogue = data.get("models", [])

            self.background(lambda: self.client.request("GET", "/models"), loaded)
            return
        self.model_catalogue = models
        self.fill_model_choices()

    def fill_model_choices(self):
        catalogue = getattr(self, "model_catalogue", None) or []
        for combo, role, current in (
            (getattr(self, "detector", None), "detect", self.detector.currentText()),
            (getattr(self, "ocr", None), "ocr", self.ocr.currentText()),
        ):
            if combo is None:
                continue
            entries = [
                m
                for m in catalogue
                if m.get("role") == role
                and m.get("installed")
                and (
                    m.get("kind", "local") in {"local", "system"}
                    or (m.get("kind") == "api" and m.get("api_ready"))
                    or m.get("kind") == "package"
                )
            ]
            if not entries:
                continue
            combo.blockSignals(True)
            combo.clear()
            for m in entries:
                combo.addItem(m["name"], m["id"])
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def sync_project_summary(self):
        if not hasattr(self, "project_summary_button"):
            return
        detector = self.detector.currentText()
        ocr = self.ocr.currentText()
        source = self.source_language.currentText()
        target = self.target_language.currentText()
        self.project_summary_button.setText(
            f"气泡检测：{detector}   ·   OCR：{ocr}"
        )
        self.project_summary_button.setToolTip(
            f"气泡检测模型：{detector}\nOCR 模型：{ocr}\n"
            f"语言：{source} → {target} · 阈值 {self.confidence.value():.2f}\n"
            "点击打开项目设置，可修改模型、语言与接口。"
        )

    def open_project_settings(self):
        self.fill_model_choices()
        if self.project_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("项目设置")
            dialog.setObjectName("mangaProjectDialog")
            dialog.resize(420, 320)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("模型、语言与识别选项，随本工程保存。"))
            form = QFormLayout()
            form.addRow("检测模型", self.detector)
            form.addRow("OCR 模型", self.ocr)
            form.addRow("原文语言", self.source_language)
            form.addRow("目标语言", self.target_language)
            form.addRow("检测阈值", self.confidence)
            form.addRow(self.panels)
            form.addRow(self.rtl)
            form.addRow(self.auto_recognize)
            form.addRow("翻译接口", self.provider)
            layout.addLayout(form)
            buttons = QHBoxLayout()
            buttons.addStretch(1)
            close = QPushButton("完成")
            close.setDefault(True)
            close.clicked.connect(dialog.accept)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            self.project_dialog = dialog
        else:
            for box in (self.detector, self.ocr, self.source_language, self.target_language):
                pass
        self.project_dialog.show()
        self.project_dialog.raise_()
        self.project_dialog.activateWindow()
        self.status.setText("项目设置已更改会自动保存到当前工程")

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
                combo.addItem(value, value)
                index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))
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
                self.regions.setItem(row, 0, order)
                self.regions.setItem(row, 1, content)
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
            self.geometry_lock,
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
        self.vertical_button.setChecked(s["vertical"])
        self.bold_button.setChecked(s["bold"])
        if s["font"] and self.font.findData(s["font"]) < 0:
            self.font.addItem(Path(s["font"]).name, s["font"])
        self.font.setCurrentIndex(max(0, self.font.findData(s["font"])))
        self.removal.setCurrentIndex(self.removal.findData(r["removal"]))
        self.rotation.setValue(r["layout_rect"]["rotation"])
        self.punctuation_action.blockSignals(True)
        self.punctuation_action.setChecked(s.get("auto_punctuation", False))
        self.punctuation_action.blockSignals(False)
        self.horizontal_button.setChecked(not s["vertical"])
        self.vertical_button.setChecked(s["vertical"])
        self.bold_button.setChecked(s["bold"])
        self.align.setCurrentIndex(self.align.findData(s["align"]))
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
            self.canvas.editable = self.variant.currentData() != "render"
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
                "detector": self.detector.currentText(),
                "ocr": self.ocr.currentText(),
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
                answer = QMessageBox.question(
                    self,
                    "开始识别",
                    "导入完成。要立即检测文字框并读取原文吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
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
        if not self.document or self.job_id or self.submitting:
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

    def start_request(self, action, callback):
        if self.job_id or self.submitting:
            self.status.setText("请等待当前任务结束，或先取消")
            return
        self.submitting = True
        self.cancel_pending = False
        self.job_base = deepcopy(self.document)
        self.job_manual_paths = set()
        session = self.session
        self.status.setText("正在提交任务…")
        self.progress_label.setText("正在提交任务…")
        self.progress_bar.setRange(0, 0)
        self.cancel_button.show()
        self.progress_panel.show()
        self.position_progress_panel()

        def started(result):
            if session != self.session:
                return
            self.submitting = False
            job = result.get("job", {})
            self.job_id = job.get("id") or job.get("job_id")
            self.job_callback = callback
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
                self.progress_panel.hide()
                self.follow_banner.hide()
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

    def show_report(self, result):
        report = result.get("report", {})
        self.last_task_report = deepcopy(report)
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
        labels = ["PNG图片", "JPEG图片", "整批ZIP", "可编辑工程包"]
        label, ok = QInputDialog.getItem(self, "导出漫画", "格式", labels, 0, False)
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择新的导出目录")
        if folder:
            mode = ["png", "jpeg", "zip", "project"][labels.index(label)]
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

    def detach_job(self):
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
