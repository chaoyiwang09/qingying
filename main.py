"""清映：把视频投到同一局域网里的电视或投影仪。"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QStyledItemDelegate,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from concurrent.futures import ThreadPoolExecutor

from dlna import local_interfaces
from engine import Bridge, Engine
from media import (
    CACHE_DIR,
    SUBTITLE_LANGS,
    Library,
    choose_encoder,
    collect_videos,
    ensure_black,
    ensure_firewall,
    find_subtitle,
    fmt_clock,
    fmt_size,
    probe_duration,
    runtime_dir,
    serve,
    thumb_key,
    thumbs,
)

APP_DIR = runtime_dir()


def user_settings_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path.home() / "AppData" / "Local"
    folder = base / "清映"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "settings.json"


SETTINGS = user_settings_path()

PATH_ROLE = Qt.UserRole
SUB_ROLE = Qt.UserRole + 1
DUR_ROLE = Qt.UserRole + 2
ID_ROLE = Qt.UserRole + 3
SECS_ROLE = Qt.UserRole + 4
SIZE_ROLE = Qt.UserRole + 5

STYLE = """
QWidget {
    color: #1C1917;
    font-size: 13px;
}
QMainWindow, QWidget#root {
    background: #F3F0E8;
}
QFrame#card {
    background: #FFFCF8;
    border: 1px solid #E6E0D4;
    border-radius: 18px;
}
QLabel#title {
    font-size: 22px;
    font-weight: 600;
    color: #1C1917;
    background: transparent;
}
QLabel#quiet, QLabel#hint {
    color: #78716C;
    background: transparent;
}
QLabel#section {
    font-size: 15px;
    font-weight: 600;
    background: transparent;
}
QLabel#pill {
    background: #E7E5E4;
    color: #44403C;
    border-radius: 11px;
    padding: 4px 10px;
}
QLabel#pill[state="on"] {
    background: #E7F3F1;
    color: #115E59;
}
QLabel#pill[state="play"] {
    background: #0F766E;
    color: #F0FDFA;
}
QLabel#pill[state="pause"] {
    background: #FEF3C7;
    color: #92400E;
}
QLabel#pill[state="black"] {
    background: #1C1917;
    color: #FAFAF9;
}
QPushButton {
    background: #FFFCF8;
    border: 1px solid #E4DFD4;
    border-radius: 10px;
    padding: 7px 12px;
    color: #1C1917;
}
QPushButton:hover { background: #F6F1E8; }
QPushButton:pressed { background: #EFEAE1; }
QPushButton:disabled { color: #A8A29E; background: #F5F2EB; }
QPushButton#primary {
    background: #0F766E;
    color: #F0FDFA;
    border: none;
    padding: 8px 18px;
}
QPushButton#primary:hover { background: #0D6660; }
QPushButton#primary:disabled { background: #D6D3D1; color: #FAFAF9; }
QPushButton#small { padding: 5px 10px; border-radius: 8px; }
QFrame#seg {
    background: #E8E2D6;
    border-radius: 12px;
    border: none;
}
QPushButton#seg {
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 6px 14px;
    color: #57534E;
}
QPushButton#seg:checked {
    background: #FFFCF8;
    color: #1C1917;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    background: transparent;
    border: none;
    padding: 0px;
}
QLineEdit, QSpinBox, QComboBox {
    background: #FFFFFF;
    border: 1px solid #E4DFD4;
    border-radius: 10px;
    padding: 6px 10px;
    min-height: 20px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #0F766E;
}
QSlider::groove:horizontal {
    background: #E7E5E4;
    height: 6px;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #0F766E;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #0F766E;
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover {
    background: #115E59;
}
QSlider:disabled::sub-page:horizontal,
QSlider:disabled::handle:horizontal {
    background: #D6D3D1;
}
QFrame#preview {
    background: #1C1917;
    border-radius: 12px;
}
QFrame#preview QLabel {
    color: #FAFAF9;
    background: transparent;
}
QPlainTextEdit {
    background: #FFFCF8;
    border: 1px solid #E6E0D4;
    border-radius: 12px;
    padding: 8px;
    color: #44403C;
}
QScrollBar:vertical {
    width: 10px;
    background: transparent;
    margin: 4px 0;
}
QScrollBar::handle:vertical {
    background: #E4DFD4;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu {
    background: #FFFCF8;
    border: 1px solid #E6E0D4;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: #E7F3F1; }
"""


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_settings() -> dict:
    data = read_json(SETTINGS)
    legacy = read_json(APP_DIR / "settings.json")
    changed = not SETTINGS.exists() and bool(legacy)
    for key, value in legacy.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        save_settings(data)
    return data


def save_settings(data: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def make_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#0F766E"))
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    painter.setBrush(QColor("#F7F4EE"))
    painter.drawRoundedRect(16, 20, 32, 22, 4, 4)
    painter.setBrush(QColor("#0F766E"))
    painter.drawRoundedRect(28, 42, 8, 4, 1, 1)
    painter.end()
    return QIcon(pix)


class DeviceDelegate(QStyledItemDelegate):
    def __init__(self, devices, chosen):
        super().__init__()
        self.devices = devices
        self.chosen = chosen

    def sizeHint(self, option, index):
        return QSize(10, 72)

    def paint(self, painter, option, index):
        items = self.devices()
        slot = index.data(Qt.UserRole)
        if slot is None or slot < 0 or slot >= len(items):
            return
        device = items[slot]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(4, 4, -4, -4)
        active = device.host == self.chosen() and device.control_url == self.chosen_url()
        if active:
            fill, line = QColor("#E7F3F1"), QColor("#0F766E")
        elif option.state & QStyle.State_Selected:
            fill, line = QColor("#F6F1E8"), QColor("#E4DFD4")
        else:
            fill, line = QColor("#FFFCF8"), QColor("#EFEAE1")
        painter.setPen(QPen(line, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(QColor("#1C1917"))
        font = painter.font()
        font.setPixelSize(14)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        name_width = rect.width() - (78 if device.is_xiaomi else 28)
        name = painter.fontMetrics().elidedText(device.name, Qt.ElideRight, name_width)
        painter.drawText(QRect(rect.left() + 14, rect.top() + 12, name_width, 22), Qt.AlignLeft | Qt.AlignVCenter, name)
        painter.setPen(QColor("#78716C"))
        font.setPixelSize(12)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        sub = painter.fontMetrics().elidedText(device.subtitle, Qt.ElideRight, rect.width() - 28)
        painter.drawText(QRect(rect.left() + 14, rect.top() + 36, rect.width() - 28, 18), Qt.AlignLeft | Qt.AlignVCenter, sub)
        if device.is_xiaomi:
            badge = QRect(rect.right() - 52, rect.top() + 14, 40, 20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#CCFBF1"))
            painter.drawRoundedRect(badge, 8, 8)
            painter.setPen(QColor("#115E59"))
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignCenter, "小米")
        painter.restore()

    def chosen_url(self) -> str:
        return getattr(self, "_url", "")


class PlaylistDelegate(QStyledItemDelegate):
    def __init__(self, current_id):
        super().__init__()
        self.current_id = current_id

    def sizeHint(self, option, index):
        return QSize(10, 70)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(4, 3, -4, 3)
        playing = index.data(ID_ROLE) == self.current_id()
        if playing:
            fill, line = QColor("#E7F3F1"), QColor("#B7DDD8")
        elif option.state & QStyle.State_Selected:
            fill, line = QColor("#F6F1E8"), QColor("#E4DFD4")
        else:
            fill, line = QColor("#FFFFFF"), QColor("#F0EBE3")
        painter.setPen(QPen(line, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 12, 12)
        if playing:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#0F766E"))
            painter.drawRoundedRect(QRect(rect.left(), rect.top() + 16, 3, rect.height() - 32), 1, 1)
        painter.setPen(QColor("#A8A29E"))
        font = painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.drawText(QRect(rect.left() + 14, rect.top(), 28, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, str(index.row() + 1))
        name_rect = QRect(rect.left() + 44, rect.top() + 12, rect.width() - 150, 22)
        painter.setPen(QColor("#1C1917"))
        font.setPixelSize(14)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        name = painter.fontMetrics().elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, name)
        painter.setPen(QColor("#78716C"))
        font.setPixelSize(12)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        sub_rect = QRect(rect.left() + 44, rect.top() + 36, rect.width() - 150, 18)
        sub = painter.fontMetrics().elidedText(index.data(SUB_ROLE) or "", Qt.ElideRight, sub_rect.width())
        painter.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter, sub)
        dur = index.data(DUR_ROLE) or ""
        if dur:
            painter.drawText(QRect(rect.right() - 84, rect.top(), 68, rect.height()), Qt.AlignVCenter | Qt.AlignRight, dur)
        painter.restore()


class PlaylistView(QListWidget):
    filesDropped = Signal(list)
    reordered = Signal()
    removeSelected = Signal()

    def __init__(self):
        super().__init__()
        self.current_media = ""
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setSpacing(2)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.filesDropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)
        self.reordered.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.removeSelected.emit()
            return
        super().keyPressEvent(event)


class SeekBar(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._value_at(event))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self.setValue(self._value_at(event))
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _value_at(self, event) -> int:
        point = event.position() if hasattr(event, "position") else event.localPos()
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(point.x()), max(1, self.width()))


class MainWindow(QMainWindow):
    durationReady = Signal(str, float)
    thumbReady = Signal(str, int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("清映")
        self.setWindowIcon(make_icon())
        self.resize(1120, 960)
        self.setMinimumSize(980, 860)
        self.setAcceptDrops(True)
        self.settings = load_settings()
        self.ifaces = local_interfaces()
        self.device_items = []
        self.chosen_host = ""
        self.chosen_url = ""
        self.connected_name = ""
        self.play_mode = "idle"
        self.count_deadline: datetime | None = None
        self.clock_deadline: datetime | None = None
        self.sleep_note = ""
        self._restoring = True
        self._remembered = ""
        self._scrubbing = False
        self._applying_bar = False
        self.track_duration = 0.0
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.durationReady.connect(self.on_duration)
        self.thumbReady.connect(self.on_thumb_ready)
        thumbs.on_ready = lambda key, second, jpg: self.thumbReady.emit(key, second, jpg)

        self.library = Library()
        saved_quality = self.settings.get("quality", "720")
        if saved_quality not in ("480", "720", "1080", "source"):
            saved_quality = "720"
        self.library.quality = saved_quality
        saved_lang = self.settings.get("subtitle_lang", "中文")
        if saved_lang not in SUBTITLE_LANGS:
            saved_lang = "中文"
        self.library.subtitle_lang = saved_lang
        self.httpd, self.port = serve(self.library)
        self.bridge = Bridge()
        self.library.on_log = self.bridge.log.emit
        self.engine = Engine(self.bridge, self.library, self.port, self.ifaces)
        self.bridge.devices.connect(self.on_devices)
        self.bridge.manual_device.connect(self.on_manual_device)
        self.bridge.scan_finished.connect(self.on_scan_finished)
        self.bridge.log.connect(self.append_log)
        self.bridge.status.connect(self.set_status)
        self.bridge.track.connect(self.on_track)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.mode.connect(self.on_mode)
        self.bridge.linked.connect(self.on_linked)
        self.bridge.rate.connect(self.on_rate)
        self.bridge.relocated.connect(self.on_relocated)
        self.build_ui()
        threading.Thread(target=ensure_firewall, daemon=True).start()
        threading.Thread(target=self.prepare_encoder, daemon=True).start()
        self.engine.start()
        threading.Thread(target=self.prepare_black, daemon=True).start()
        self.clock = QTimer(self)
        self.clock.setInterval(500)
        self.clock.timeout.connect(self.on_clock)
        self.clock.start()
        self.restore_session()
        QTimer.singleShot(400, self.start_scan)

    def prepare_encoder(self) -> None:
        encoder = choose_encoder()
        self.library.encoder = encoder
        names = {
            "h264_nvenc": "显卡",
            "h264_qsv": "核显",
            "h264_amf": "显卡",
            "libx264": "处理器",
        }
        self.bridge.log.emit(f"转码使用{names.get(encoder, '处理器')}")

    def prepare_black(self) -> None:
        mp4, jpg = ensure_black(CACHE_DIR)
        self.library.black_mp4 = str(mp4) if mp4 else None
        self.library.black_jpg = str(jpg) if jpg else None
        if mp4:
            self.bridge.log.emit("全黑画面已准备好")
        else:
            self.bridge.log.emit("没能生成全黑画面，到点息屏可能无法使用")

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("清映")
        title.setObjectName("title")
        sub = QLabel("把电脑里的视频，按顺序投到电视或投影仪")
        sub.setObjectName("quiet")
        titles.addWidget(title)
        titles.addWidget(sub)
        header.addLayout(titles)
        header.addStretch(1)
        self.pill = QLabel("未连接")
        self.pill.setObjectName("pill")
        self.pill.setAlignment(Qt.AlignCenter)
        self.ip_label = QLabel(self.ip_text())
        self.ip_label.setObjectName("quiet")
        header.addWidget(self.pill)
        header.addSpacing(12)
        header.addWidget(self.ip_label)
        outer.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self.build_left(), 0)
        body.addWidget(self.build_playlist(), 1)
        outer.addLayout(body, 1)
        outer.addWidget(self.build_transport())

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(128)
        self.log_box.setVisible(False)
        self.log_box.setPlaceholderText("连接和播放详情会写在这里")
        outer.addWidget(self.log_box)

    def build_left(self) -> QWidget:
        wrap = QWidget()
        wrap.setFixedWidth(420)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 16, 16, 14)
        box.setSpacing(10)
        row = QHBoxLayout()
        name = QLabel("电视或投影仪")
        name.setObjectName("section")
        self.scan_btn = QPushButton("搜索中")
        self.scan_btn.setObjectName("small")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self.start_scan)
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(self.scan_btn)
        box.addLayout(row)

        self.device_list = QListWidget()
        self.device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.device_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.device_delegate = DeviceDelegate(lambda: self.device_items, lambda: self.chosen_host)
        self.device_list.setItemDelegate(self.device_delegate)
        self.device_list.itemClicked.connect(self.on_device_clicked)
        box.addWidget(self.device_list, 1)

        self.device_hint = QLabel("正在搜索同一网段里的电视或投影仪…", self.device_list.viewport())
        self.device_hint.setAlignment(Qt.AlignCenter)
        self.device_hint.setWordWrap(True)
        self.device_hint.setObjectName("hint")
        self.device_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.device_list.viewport().installEventFilter(self)

        manual = QHBoxLayout()
        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("手动输入电视或投影仪 IP")
        self.ip_edit.returnPressed.connect(self.connect_manual)
        connect = QPushButton("连接")
        connect.setObjectName("small")
        connect.clicked.connect(self.connect_manual)
        manual.addWidget(self.ip_edit, 1)
        manual.addWidget(connect)
        box.addLayout(manual)
        note = QLabel("有线和 Wi‑Fi 需要在同一台路由器下。搜不到时，请在电视或投影仪里打开投屏。")
        note.setObjectName("quiet")
        note.setWordWrap(True)
        box.addWidget(note)
        layout.addWidget(card, 1)

        sleep = QFrame()
        sleep.setObjectName("card")
        sleep.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        form = QVBoxLayout(sleep)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        sleep_title = QLabel("到点息屏")
        sleep_title.setObjectName("section")
        form.addWidget(sleep_title)

        count_name = QLabel("倒计时")
        count_name.setObjectName("quiet")
        form.addWidget(count_name)
        self.count_hour = QSpinBox()
        self.count_minute = QComboBox()
        self._fill_sleep_time(self.count_hour, self.count_minute, 1)
        self.count_hour.valueChanged.connect(self.on_count_inputs)
        self.count_minute.currentIndexChanged.connect(self.on_count_inputs)
        form.addLayout(self._sleep_time_row(self.count_hour, self.count_minute, "小时"))
        self.count_on, self.count_off, self.count_state = self._sleep_switch("countdown")
        form.addLayout(self._sleep_switch_row(self.count_on, self.count_off, self.count_state))

        clock_name = QLabel("定时")
        clock_name.setObjectName("quiet")
        form.addWidget(clock_name)
        self.clock_hour = QSpinBox()
        self.clock_minute = QComboBox()
        self._fill_sleep_time(self.clock_hour, self.clock_minute, 3)
        self.clock_hour.valueChanged.connect(self.on_clock_inputs)
        self.clock_minute.currentIndexChanged.connect(self.on_clock_inputs)
        form.addLayout(self._sleep_time_row(self.clock_hour, self.clock_minute, "点"))
        self.clock_on, self.clock_off, self.clock_state = self._sleep_switch("clock")
        form.addLayout(self._sleep_switch_row(self.clock_on, self.clock_off, self.clock_state))

        self.sleep_text = QLabel("倒计时和定时互不影响。开启后，到点会停掉影片，并把投影切成全黑。")
        self.sleep_text.setObjectName("quiet")
        self.sleep_text.setWordWrap(True)
        form.addWidget(self.sleep_text)
        self.wake_btn = QPushButton("结束全黑")
        self.wake_btn.setVisible(False)
        self.wake_btn.clicked.connect(self.engine_stop)
        form.addWidget(self.wake_btn)
        layout.addWidget(sleep)
        return wrap

    def build_playlist(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        card.setAcceptDrops(True)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 16, 16, 12)
        box.setSpacing(10)
        row = QHBoxLayout()
        name = QLabel("播放列表")
        name.setObjectName("section")
        self.count_label = QLabel("0 个")
        self.count_label.setObjectName("quiet")
        row.addWidget(name)
        row.addWidget(self.count_label)
        row.addStretch(1)
        add_files = QPushButton("添加视频")
        add_dir = QPushButton("添加文件夹")
        remove = QPushButton("移除")
        clear = QPushButton("清空")
        for button in (add_files, add_dir, remove, clear):
            button.setObjectName("small")
            button.setCursor(Qt.PointingHandCursor)
        add_files.clicked.connect(self.pick_files)
        add_dir.clicked.connect(self.pick_folder)
        remove.clicked.connect(self.remove_selected)
        clear.clicked.connect(self.clear_playlist)
        row.addWidget(add_files)
        row.addWidget(add_dir)
        row.addWidget(remove)
        row.addWidget(clear)
        box.addLayout(row)

        self.playlist = PlaylistView()
        self.playlist.setItemDelegate(PlaylistDelegate(lambda: self.playlist.current_media))
        self.playlist.filesDropped.connect(self.add_paths)
        self.playlist.reordered.connect(self.push_playlist)
        self.playlist.removeSelected.connect(self.remove_selected)
        self.playlist.itemDoubleClicked.connect(lambda item: self.play_row(self.playlist.row(item)))
        self.playlist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.playlist.customContextMenuRequested.connect(self.playlist_menu)
        box.addWidget(self.playlist, 1)

        self.drop_hint = QLabel("把视频或文件夹拖到这里\n文件夹会按文件名顺序排好，也可以再拖动调整", self.playlist.viewport())
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setObjectName("hint")
        self.drop_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.playlist.viewport().installEventFilter(self)
        foot = QLabel("拖动列表可以调整播放顺序。播放时请让这台电脑保持开着。")
        foot.setObjectName("quiet")
        box.addWidget(foot)
        return card

    def build_transport(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)

        quality_row = QHBoxLayout()
        quality_label = QLabel("画质")
        quality_label.setObjectName("quiet")
        quality_row.addWidget(quality_label)
        seg = QFrame()
        seg.setObjectName("seg")
        seg_row = QHBoxLayout(seg)
        seg_row.setContentsMargins(3, 3, 3, 3)
        seg_row.setSpacing(2)
        self.quality_group = QButtonGroup(self)
        self.quality_group.setExclusive(True)
        current = self.library.quality
        for key, text in (("480", "480"), ("720", "720"), ("1080", "1080"), ("source", "原画")):
            button = QPushButton(text)
            button.setObjectName("seg")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(key == current)
            button.setProperty("quality", key)
            self.quality_group.addButton(button)
            seg_row.addWidget(button)
            button.clicked.connect(lambda checked, name=key: self.on_quality(name))
        quality_row.addWidget(seg)
        quality_hint = QLabel("转码后电视和投影仪更好解，播放更顺")
        quality_hint.setObjectName("quiet")
        quality_row.addWidget(quality_hint)
        quality_row.addSpacing(16)
        sub_label = QLabel("字幕")
        sub_label.setObjectName("quiet")
        quality_row.addWidget(sub_label)
        self.sub_lang = QComboBox()
        self.sub_lang.setFixedWidth(112)
        self.sub_lang.addItems(list(SUBTITLE_LANGS))
        lang_index = self.sub_lang.findText(self.library.subtitle_lang)
        if lang_index >= 0:
            self.sub_lang.setCurrentIndex(lang_index)
        self.sub_lang.setToolTip("有中英字幕时优先用中英。否则用这里选的语言，没有就改用中文，再没有用英语。目录里没有字幕文件就不加载。")
        self.sub_lang.currentTextChanged.connect(self.on_subtitle)
        quality_row.addWidget(self.sub_lang)
        quality_row.addStretch(1)
        box.addLayout(quality_row)

        row = QHBoxLayout()
        self.play_btn = QPushButton("播放")
        self.play_btn.setObjectName("primary")
        self.pause_btn = QPushButton("暂停")
        self.prev_btn = QPushButton("上一个")
        self.next_btn = QPushButton("下一个")
        self.stop_btn = QPushButton("停止")
        self.detail_btn = QPushButton("详情")
        for button in (self.play_btn, self.pause_btn, self.prev_btn, self.next_btn, self.stop_btn, self.detail_btn):
            button.setCursor(Qt.PointingHandCursor)
        self.detail_btn.setObjectName("small")
        self.play_btn.clicked.connect(lambda: self.play_row(None))
        self.pause_btn.clicked.connect(lambda: self.engine.command("pause"))
        self.prev_btn.clicked.connect(lambda: self.shift(-1))
        self.next_btn.clicked.connect(lambda: self.shift(1))
        self.stop_btn.clicked.connect(self.engine_stop)
        self.detail_btn.clicked.connect(self.toggle_log)
        self.now = QLabel("还没有在播放")
        self.now.setObjectName("quiet")
        self.now.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self.play_btn)
        row.addWidget(self.pause_btn)
        row.addWidget(self.prev_btn)
        row.addWidget(self.next_btn)
        row.addWidget(self.stop_btn)
        row.addSpacing(8)
        row.addWidget(self.now, 1)
        row.addWidget(self.detail_btn)
        box.addLayout(row)

        pace = QHBoxLayout()
        self.back_btn = QPushButton("后退30秒")
        self.forward_btn = QPushButton("快进30秒")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.forward_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.setToolTip("从当前进度后退 30 秒")
        self.forward_btn.setToolTip("从当前进度快进 30 秒")
        self.back_btn.clicked.connect(lambda: self.engine.command("jump", -30))
        self.forward_btn.clicked.connect(lambda: self.engine.command("jump", 30))
        pace.addWidget(self.back_btn)
        pace.addWidget(self.forward_btn)
        pace.addSpacing(12)
        speed_box = QFrame()
        speed_box.setObjectName("seg")
        speed_row = QHBoxLayout(speed_box)
        speed_row.setContentsMargins(3, 3, 3, 3)
        speed_row.setSpacing(2)
        self.rate_group = QButtonGroup(self)
        self.rate_group.setExclusive(True)
        for value in (1, 2, 3, 5):
            button = QPushButton(f"×{value}")
            button.setObjectName("seg")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(value == 1)
            button.setProperty("rate", value)
            button.setToolTip(f"按 {value} 倍速播放")
            self.rate_group.addButton(button)
            speed_row.addWidget(button)
            button.clicked.connect(lambda checked, rate=value: self.engine.command("rate", rate))
        pace.addWidget(speed_box)
        pace.addStretch(1)
        box.addLayout(pace)

        times = QHBoxLayout()
        self.time_now = QLabel("--:--")
        self.time_end = QLabel("--:--")
        for label in (self.time_now, self.time_end):
            label.setObjectName("quiet")
        self.bar = SeekBar(Qt.Horizontal)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setEnabled(False)
        self.bar.setFixedHeight(22)
        self.bar.setCursor(Qt.PointingHandCursor)
        self.bar.setToolTip("拖动可跳到指定进度，松手时会显示附近的画面。480、720、1080 会从新位置重新开始播")
        self.bar.sliderPressed.connect(self.on_bar_pressed)
        self.bar.sliderReleased.connect(self.on_bar_released)
        self.bar.valueChanged.connect(self.on_bar_changed)
        times.addWidget(self.time_now)
        times.addWidget(self.bar, 1)
        times.addWidget(self.time_end)
        box.addLayout(times)
        self.preview = QFrame(self)
        self.preview.setObjectName("preview")
        self.preview.setFixedSize(208, 150)
        self.preview.hide()
        preview_box = QVBoxLayout(self.preview)
        preview_box.setContentsMargins(8, 8, 8, 6)
        preview_box.setSpacing(4)
        self.preview_image = QLabel()
        self.preview_image.setFixedSize(192, 108)
        self.preview_image.setAlignment(Qt.AlignCenter)
        self.preview_image.setStyleSheet("background:#000000; border-radius:6px; color:#D6D3D1;")
        self.preview_caption = QLabel("--:--")
        self.preview_caption.setAlignment(Qt.AlignCenter)
        preview_box.addWidget(self.preview_image)
        preview_box.addWidget(self.preview_caption)
        self.status = QLabel("准备就绪")
        self.status.setObjectName("quiet")
        box.addWidget(self.status)
        return card

    def chosen_quality(self) -> str:
        button = self.quality_group.checkedButton()
        if button is not None:
            quality = button.property("quality")
            if quality in ("480", "720", "1080", "source"):
                return quality
        return self.library.quality

    def chosen_subtitle(self) -> str:
        if hasattr(self, "sub_lang"):
            lang = self.sub_lang.currentText()
            if lang in SUBTITLE_LANGS:
                return lang
        return self.library.subtitle_lang

    def remember(self) -> None:
        if self._restoring:
            return
        paths = []
        for index in range(self.playlist.count()):
            path = self.playlist.item(index).data(PATH_ROLE)
            if path:
                paths.append(os.path.abspath(path))
        last = ""
        media_id = self.playlist.current_media
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            path = item.data(PATH_ROLE) or ""
            if media_id and item.data(ID_ROLE) == media_id:
                last = os.path.abspath(path)
                break
        if not last:
            current = self.playlist.currentItem()
            if current is not None and current.data(PATH_ROLE):
                last = os.path.abspath(current.data(PATH_ROLE))
        sleep = {
            "countdown": self._sleep_saved("countdown"),
            "clock": self._sleep_saved("clock"),
        }
        snapshot = json.dumps(
            {
                "quality": self.chosen_quality(),
                "subtitle_lang": self.chosen_subtitle(),
                "playlist": paths,
                "last_path": last,
                "sleep": sleep,
            },
            ensure_ascii=False,
        )
        if snapshot == self._remembered:
            return
        self._remembered = snapshot
        self.settings["quality"] = self.chosen_quality()
        self.settings["subtitle_lang"] = self.chosen_subtitle()
        self.settings["playlist"] = paths
        self.settings["last_path"] = last
        self.settings["sleep"] = sleep
        save_settings(self.settings)

    def restore_session(self) -> None:
        self._restoring = True
        try:
            sleep = self._saved_sleep_blocks(self.settings.get("sleep"))
            self._apply_saved_sleep("countdown", sleep["countdown"])
            self._apply_saved_sleep("clock", sleep["clock"])

            saved = [path for path in (self.settings.get("playlist") or []) if isinstance(path, str)]
            existing = [path for path in saved if os.path.isfile(path)]
            missing = len(saved) - len(existing)
            if existing:
                self.add_paths(existing)
            last = self.settings.get("last_path") or ""
            if isinstance(last, str) and last:
                key = os.path.normcase(os.path.abspath(last))
                for row in range(self.playlist.count()):
                    path = self.playlist.item(row).data(PATH_ROLE) or ""
                    try:
                        same = os.path.normcase(os.path.abspath(path)) == key
                    except OSError:
                        same = False
                    if same:
                        self.playlist.setCurrentRow(row)
                        self.playlist.scrollToItem(self.playlist.item(row))
                        break

            self.refresh_sleep_label()
            if existing:
                text = f"已放回上次的 {len(existing)} 个视频"
                if missing:
                    text += f"，有 {missing} 个已经不在原来的位置"
                self.set_status(text)
        finally:
            self._restoring = False
            self.remember()

    def refresh_subtitle_marks(self) -> None:
        lang = self.library.subtitle_lang
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            path = item.data(PATH_ROLE) or ""
            text = item.data(SUB_ROLE) or ""
            text = text.replace("    ·    字幕", "")
            if path and find_subtitle(path, lang):
                text += "    ·    字幕"
            item.setData(SUB_ROLE, text)
        self.playlist.viewport().update()

    def on_subtitle(self, lang: str) -> None:
        if lang not in SUBTITLE_LANGS or lang == self.library.subtitle_lang:
            return
        self.library.subtitle_lang = lang
        self.refresh_subtitle_marks()
        self.remember()
        self.engine.command("subtitle", lang)

    def on_quality(self, quality: str) -> None:
        if quality == self.library.quality:
            return
        self.remember()
        self.engine.command("quality", quality)

    def ip_text(self) -> str:
        if not self.ifaces:
            return "未检测到局域网"
        return "本机  " + "    ".join(iface.ip for iface in self.ifaces)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            if obj is self.playlist.viewport():
                self.drop_hint.setGeometry(self.playlist.viewport().rect().adjusted(24, 0, -24, 0))
            elif obj is self.device_list.viewport():
                self.device_hint.setGeometry(self.device_list.viewport().rect().adjusted(18, 0, -18, 0))
        return super().eventFilter(obj, event)

    def append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"{stamp}  {text}")

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_pill(self, text: str, state: str) -> None:
        self.pill.setText(text)
        self.pill.setProperty("state", state)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

    def start_scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("搜索中")
        self.device_hint.setText("正在搜索同一网段里的电视或投影仪…")
        self.device_hint.setVisible(self.device_list.count() == 0)
        self.engine.command("scan")

    def on_scan_finished(self) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("重新搜索")

    def on_devices(self, devices) -> None:
        self.device_items = list(devices)
        self.rebuild_devices()
        if self.chosen_host and any(
            item.host == self.chosen_host and item.control_url == self.chosen_url for item in self.device_items
        ):
            self.device_hint.setVisible(False)
            self.device_delegate._url = self.chosen_url
            self.device_list.viewport().update()
            return
        if not self.device_items:
            nets = "、".join(iface.label for iface in (self.engine.ifaces or self.ifaces)) or "局域网"
            self.device_hint.setText(f"在 {nets} 里没有找到电视或投影仪。\n请确认它已开机，或在下面填入 IP。")
            self.device_hint.setVisible(True)
            return
        self.device_hint.setVisible(False)
        prefer = self.settings.get("last_host", "")
        index = next((i for i, item in enumerate(self.device_items) if item.host == prefer), -1)
        if index < 0:
            index = next((i for i, item in enumerate(self.device_items) if item.is_xiaomi), -1)
        if index < 0 and len(self.device_items) == 1:
            index = 0
        if index >= 0:
            self.select_device(index)

    def on_manual_device(self, device) -> None:
        for index, old in enumerate(self.device_items):
            if old.host == device.host:
                self.device_items[index] = device
                self.rebuild_devices()
                self.select_device(index)
                return
        self.device_items.insert(0, device)
        self.rebuild_devices()
        self.device_hint.setVisible(False)
        self.select_device(0)

    def rebuild_devices(self) -> None:
        self.device_list.clear()
        for index, _device in enumerate(self.device_items):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, index)
            item.setSizeHint(QSize(10, 72))
            self.device_list.addItem(item)
        self.device_hint.setVisible(self.device_list.count() == 0)

    def on_device_clicked(self, item: QListWidgetItem) -> None:
        slot = item.data(Qt.UserRole)
        if slot is None:
            return
        self.select_device(int(slot))

    def select_device(self, index: int) -> None:
        if index < 0 or index >= len(self.device_items):
            return
        device = self.device_items[index]
        self.chosen_host = device.host
        self.chosen_url = device.control_url
        self.device_delegate._url = device.control_url
        self.device_list.viewport().update()
        self.settings["last_host"] = device.host
        save_settings(self.settings)
        self.engine.command("select", device)

    def connect_manual(self) -> None:
        ip = self.ip_edit.text().strip()
        parts = ip.split(".")
        if len(parts) != 4 or not all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            self.set_status("请输入正确的 IP 地址")
            return
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("连接中")
        self.engine.command("manual", ip)

    def on_linked(self, name: str) -> None:
        self.connected_name = name
        if name and self.play_mode == "idle":
            self.set_pill("已连接", "on")

    def add_paths(self, paths: list[str]) -> None:
        videos = collect_videos(paths)
        existing = {
            os.path.normcase(self.playlist.item(i).data(PATH_ROLE))
            for i in range(self.playlist.count())
        }
        added = 0
        for path in videos:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            existing.add(key)
            media_id = self.library.add(path)
            item = QListWidgetItem(Path(path).name)
            item.setData(PATH_ROLE, path)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            subtitle = "    ·    字幕" if find_subtitle(path, self.library.subtitle_lang) else ""
            item.setData(SUB_ROLE, f"{Path(path).parent.name}    ·    {fmt_size(size)}{subtitle}")
            item.setData(DUR_ROLE, "")
            item.setData(ID_ROLE, media_id)
            item.setData(SECS_ROLE, 0.0)
            item.setData(SIZE_ROLE, int(size))
            item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
            )
            self.playlist.addItem(item)
            added += 1
            future = self.pool.submit(probe_duration, path)
            future.add_done_callback(lambda done, mid=media_id: self.probe_done(mid, done))
        self.refresh_count()
        self.push_playlist()
        if added:
            self.set_status(f"已加入 {added} 个视频")
        elif paths:
            self.set_status("没有发现新的视频")

    def probe_done(self, media_id: str, future) -> None:
        try:
            seconds = future.result()
        except Exception:
            return
        if seconds:
            self.durationReady.emit(media_id, float(seconds))

    def on_duration(self, media_id: str, seconds: float) -> None:
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            if item.data(ID_ROLE) == media_id:
                item.setData(SECS_ROLE, seconds)
                item.setData(DUR_ROLE, fmt_clock(seconds))
                self.playlist.viewport().update()
                break
        self.push_playlist()

    def refresh_count(self) -> None:
        count = self.playlist.count()
        self.count_label.setText(f"{count} 个")
        self.drop_hint.setVisible(count == 0)

    def push_playlist(self) -> None:
        items = []
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            items.append(
                {
                    "id": item.data(ID_ROLE),
                    "path": item.data(PATH_ROLE),
                    "name": item.text(),
                    "duration": float(item.data(SECS_ROLE) or 0),
                    "size": int(item.data(SIZE_ROLE) or 0),
                }
            )
        self.engine.command("playlist", items)
        self.remember()

    def remove_selected(self) -> None:
        rows = sorted({self.playlist.row(item) for item in self.playlist.selectedItems()}, reverse=True)
        if not rows:
            return
        for row in rows:
            item = self.playlist.takeItem(row)
            if item is not None:
                self.library.discard(item.data(ID_ROLE))
        self.refresh_count()
        self.push_playlist()

    def clear_playlist(self) -> None:
        if self.playlist.count() == 0:
            return
        for index in range(self.playlist.count()):
            self.library.discard(self.playlist.item(index).data(ID_ROLE))
        self.playlist.clear()
        self.refresh_count()
        self.push_playlist()

    def playlist_menu(self, pos: QPoint) -> None:
        item = self.playlist.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            play = menu.addAction("从这里播放")
            remove = menu.addAction("移除")
            chosen = menu.exec(self.playlist.mapToGlobal(pos))
            if chosen == play:
                self.play_row(self.playlist.row(item))
            elif chosen == remove:
                self.playlist.setCurrentItem(item)
                item.setSelected(True)
                self.remove_selected()
            return
        clear = menu.addAction("清空列表")
        if menu.exec(self.playlist.mapToGlobal(pos)) == clear:
            self.clear_playlist()

    def pick_files(self) -> None:
        folder = self.settings.get("last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择视频",
            folder,
            "视频 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.m2ts *.mpg *.mpeg);;所有文件 (*.*)",
        )
        if files:
            self.settings["last_dir"] = str(Path(files[0]).parent)
            save_settings(self.settings)
            self.add_paths(files)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", self.settings.get("last_dir", str(Path.home())))
        if folder:
            self.settings["last_dir"] = folder
            save_settings(self.settings)
            self.add_paths([folder])

    def play_row(self, row: int | None) -> None:
        if self.playlist.count() == 0:
            self.set_status("请先把视频拖进来")
            return
        if row is None:
            selected = self.playlist.selectedItems()
            if self.play_mode == "paused" and not selected:
                self.engine.command("resume")
                return
            if selected:
                row = self.playlist.row(selected[0])
            else:
                row = self.row_of(self.playlist.current_media)
                if row < 0:
                    row = 0
        self.push_playlist()
        self.engine.command("play", int(row))

    def shift(self, delta: int) -> None:
        if self.playlist.count() == 0:
            return
        row = self.row_of(self.playlist.current_media)
        if row < 0:
            row = 0 if delta > 0 else self.playlist.count() - 1
        else:
            row = max(0, min(self.playlist.count() - 1, row + delta))
        self.play_row(row)

    def row_of(self, media_id: str) -> int:
        if not media_id:
            return -1
        for index in range(self.playlist.count()):
            if self.playlist.item(index).data(ID_ROLE) == media_id:
                return index
        return -1

    def engine_stop(self) -> None:
        self.engine.command("stop")

    def on_track(self, media_id: str) -> None:
        self.playlist.current_media = media_id
        self.playlist.viewport().update()
        if not media_id:
            self.now.setText("还没有在播放")
            self.setWindowTitle("清映")
            return
        row = self.row_of(media_id)
        if row < 0:
            return
        item = self.playlist.item(row)
        self.now.setText(f"{row + 1}  /  {self.playlist.count()}      {item.text()}")
        self.setWindowTitle(f"清映  ·  {item.text()}")
        self.playlist.scrollToItem(item)
        self.remember()

    def on_rate(self, rate: float) -> None:
        chosen = int(rate)
        for button in self.rate_group.buttons():
            button.setChecked(button.property("rate") == chosen)

    def on_progress(self, pos: float, dur: float) -> None:
        self.track_duration = dur if dur > 0 else 0.0
        if not self._scrubbing:
            self.time_now.setText(fmt_clock(pos if dur or pos else None))
        self.time_end.setText(fmt_clock(dur if dur else None))
        self._applying_bar = True
        if not self._scrubbing:
            if dur > 0:
                self.bar.setValue(int(max(0, min(1000, pos / dur * 1000))))
            else:
                self.bar.setValue(0)
        self.bar.setEnabled(dur > 1 and self.play_mode in ("playing", "paused"))
        self._applying_bar = False

    def on_bar_pressed(self) -> None:
        self._scrubbing = True
        self._show_preview(self._bar_seconds())

    def on_bar_changed(self, value: int) -> None:
        if self._applying_bar or not self._scrubbing or self.track_duration <= 0:
            return
        seconds = value / 1000 * self.track_duration
        self.time_now.setText(fmt_clock(seconds))
        self._show_preview(seconds)

    def on_bar_released(self) -> None:
        self._scrubbing = False
        self.preview.hide()
        if self.track_duration <= 1 or self.play_mode not in ("playing", "paused"):
            return
        target = self.bar.value() / 1000 * self.track_duration
        self.engine.command("seek", target)

    def _bar_seconds(self) -> float:
        if self.track_duration <= 0:
            return 0.0
        return self.bar.value() / 1000 * self.track_duration

    def _current_media(self) -> tuple[str, int]:
        media_id = self.playlist.current_media
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            if item.data(ID_ROLE) == media_id:
                return item.data(PATH_ROLE) or "", int(item.data(SIZE_ROLE) or 0)
        return "", 0

    def _show_preview(self, seconds: float) -> None:
        path, size = self._current_media()
        self.preview_caption.setText(fmt_clock(seconds))
        found = thumbs.request(path, size, seconds) if path and size else None
        if found:
            _slot, jpg = found
            pixmap = QPixmap(jpg)
            if not pixmap.isNull():
                self.preview_image.setText("")
                self.preview_image.setPixmap(
                    pixmap.scaled(192, 108, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                self.preview_image.setPixmap(QPixmap())
                self.preview_image.setText("预览生成中")
        else:
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("预览生成中")
        ratio = 0 if self.bar.maximum() <= 0 else self.bar.value() / self.bar.maximum()
        point = self.bar.mapTo(self, QPoint(int(ratio * max(0, self.bar.width() - 1)), 0))
        left = max(12, min(self.width() - self.preview.width() - 12, point.x() - self.preview.width() // 2))
        top = max(12, point.y() - self.preview.height() - 12)
        self.preview.move(left, top)
        self.preview.show()
        self.preview.raise_()

    def on_thumb_ready(self, key: str, _second: int, _jpg: str) -> None:
        if not self._scrubbing:
            return
        path, size = self._current_media()
        if not path or thumb_key(Path(path).name, size) != key:
            return
        self._show_preview(self._bar_seconds())

    def on_relocated(self, media_id: str, path: str) -> None:
        for index in range(self.playlist.count()):
            item = self.playlist.item(index)
            if item.data(ID_ROLE) != media_id:
                continue
            item.setData(PATH_ROLE, path)
            try:
                item.setData(SIZE_ROLE, int(os.path.getsize(path)))
            except OSError:
                pass
            break
        self.push_playlist()

    def on_mode(self, name: str) -> None:
        self.play_mode = name
        self.wake_btn.setVisible(name == "blackout")
        if name == "playing":
            self.set_pill("播放中", "play")
        elif name == "paused":
            self.set_pill("已暂停", "pause")
        elif name == "waiting":
            self.set_pill("等待硬盘", "pause")
            self.preview.hide()
        elif name == "blackout":
            self.set_pill("全黑", "black")
            self.now.setText("投影已全黑")
            self.setWindowTitle("清映  ·  全黑")
        elif self.connected_name:
            self.set_pill("已连接", "on")
        else:
            self.set_pill("未连接", "")

    def _fill_sleep_time(self, hour: QSpinBox, minute: QComboBox, default_hour: int) -> None:
        hour.setRange(0, 23)
        hour.setValue(default_hour)
        hour.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        hour.setFixedWidth(72)
        minute.addItems(["00", "15", "30", "45"])
        minute.setFixedWidth(72)

    def _sleep_time_row(self, hour: QSpinBox, minute: QComboBox, unit_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        unit = QLabel(unit_text)
        minute_unit = QLabel("分")
        row.addWidget(hour)
        row.addWidget(unit)
        row.addSpacing(8)
        row.addWidget(minute)
        row.addWidget(minute_unit)
        row.addStretch(1)
        return row

    def _sleep_switch(self, kind: str) -> tuple[QPushButton, QPushButton, QLabel]:
        on = QPushButton("开启")
        off = QPushButton("关闭")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button, armed in ((on, True), (off, False)):
            button.setObjectName("seg")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            group.addButton(button)
            button.clicked.connect(lambda checked, turn_on=armed, name=kind: self.on_sleep_toggled(name, turn_on))
        off.setChecked(True)
        label = QLabel("当前：已关闭")
        label.setObjectName("pill")
        return on, off, label

    def _sleep_switch_row(self, on: QPushButton, off: QPushButton, label: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        switch = QFrame()
        switch.setObjectName("seg")
        buttons = QHBoxLayout(switch)
        buttons.setContentsMargins(3, 3, 3, 3)
        buttons.setSpacing(2)
        buttons.addWidget(on)
        buttons.addWidget(off)
        row.addWidget(switch)
        row.addWidget(label)
        row.addStretch(1)
        return row

    def _sleep_widgets(self, kind: str) -> tuple[QSpinBox, QComboBox, QPushButton, QPushButton, QLabel]:
        if kind == "countdown":
            return self.count_hour, self.count_minute, self.count_on, self.count_off, self.count_state
        return self.clock_hour, self.clock_minute, self.clock_on, self.clock_off, self.clock_state

    def _sleep_deadline(self, kind: str) -> datetime | None:
        return self.count_deadline if kind == "countdown" else self.clock_deadline

    def _set_sleep_deadline(self, kind: str, deadline: datetime | None) -> None:
        if kind == "countdown":
            self.count_deadline = deadline
        else:
            self.clock_deadline = deadline

    def _deadline_for(self, kind: str) -> datetime | None:
        now = datetime.now()
        hour_box, minute_box, _, _, _ = self._sleep_widgets(kind)
        hours = hour_box.value()
        minutes = int(minute_box.currentText())
        if kind == "countdown":
            if hours == 0 and minutes == 0:
                return None
            return now + timedelta(hours=hours, minutes=minutes)
        target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _sleep_saved(self, kind: str) -> dict:
        hour_box, minute_box, _, _, _ = self._sleep_widgets(kind)
        deadline = self._sleep_deadline(kind)
        saved = {
            "hour": hour_box.value(),
            "minute": minute_box.currentText(),
            "armed": False,
        }
        if deadline and deadline > datetime.now():
            saved["armed"] = True
            saved["deadline"] = deadline.isoformat(timespec="seconds")
        return saved

    def _saved_sleep_blocks(self, sleep) -> dict:
        empty = {
            "countdown": {"hour": 1, "minute": "00", "armed": False},
            "clock": {"hour": 3, "minute": "00", "armed": False},
        }
        if not isinstance(sleep, dict):
            return empty
        if isinstance(sleep.get("countdown"), dict) or isinstance(sleep.get("clock"), dict):
            empty["countdown"].update(sleep.get("countdown") or {})
            empty["clock"].update(sleep.get("clock") or {})
            return empty
        mode = sleep.get("mode") if sleep.get("mode") in ("countdown", "clock") else "countdown"
        empty[mode] = {
            "hour": sleep.get("hour", empty[mode]["hour"]),
            "minute": sleep.get("minute", "00"),
            "armed": bool(sleep.get("armed")),
            "deadline": sleep.get("deadline"),
        }
        return empty

    def _apply_saved_sleep(self, kind: str, data: dict) -> None:
        hour_box, minute_box, _, _, _ = self._sleep_widgets(kind)
        try:
            hour = int(data.get("hour", 1 if kind == "countdown" else 3))
        except (TypeError, ValueError):
            hour = 1 if kind == "countdown" else 3
        minute = str(data.get("minute", "00"))
        if minute not in ("00", "15", "30", "45"):
            minute = "00"
        hour_box.blockSignals(True)
        minute_box.blockSignals(True)
        hour_box.setValue(max(0, min(23, hour)))
        minute_index = minute_box.findText(minute)
        if minute_index >= 0:
            minute_box.setCurrentIndex(minute_index)
        hour_box.blockSignals(False)
        minute_box.blockSignals(False)
        deadline = None
        raw = data.get("deadline")
        if isinstance(raw, str):
            try:
                deadline = datetime.fromisoformat(raw)
            except ValueError:
                deadline = None
        if data.get("armed"):
            if deadline is None or deadline <= datetime.now():
                deadline = self._deadline_for(kind)
            if deadline and deadline > datetime.now():
                self._set_sleep_deadline(kind, deadline)
                self._mark_sleep_switch(kind, True)
                return
        self._set_sleep_deadline(kind, None)
        self._mark_sleep_switch(kind, False)

    def on_count_inputs(self) -> None:
        self.sleep_note = ""
        if self.count_deadline:
            self.count_deadline = self._deadline_for("countdown")
            if self.count_deadline is None:
                self._mark_sleep_switch("countdown", False)
        self.refresh_sleep_label()
        self.remember()

    def on_clock_inputs(self) -> None:
        if self.clock_deadline:
            self.clock_deadline = self._deadline_for("clock")
        self.refresh_sleep_label()
        self.remember()

    def _mark_sleep_switch(self, kind: str, armed: bool) -> None:
        _, _, on, off, _ = self._sleep_widgets(kind)
        button = on if armed else off
        if button.isChecked():
            return
        on.blockSignals(True)
        off.blockSignals(True)
        button.setChecked(True)
        on.blockSignals(False)
        off.blockSignals(False)

    def _show_sleep_state(self, kind: str) -> None:
        _, _, _, _, label = self._sleep_widgets(kind)
        name = "倒计时" if kind == "countdown" else "定时"
        armed = self._sleep_deadline(kind) is not None
        text = f"当前：已开启 · {name}" if armed else f"当前：已关闭 · {name}"
        state = "on" if armed else ""
        if label.text() == text and label.property("state") == state:
            return
        label.setText(text)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def on_sleep_toggled(self, kind: str, checked: bool) -> None:
        if checked:
            deadline = self._deadline_for(kind)
            if deadline is None:
                self._set_sleep_deadline(kind, None)
                self._mark_sleep_switch(kind, False)
                self.sleep_note = "倒计时要大于 0。"
                self.refresh_sleep_label()
                return
            self.sleep_note = ""
            self._set_sleep_deadline(kind, deadline)
        else:
            self._set_sleep_deadline(kind, None)
        self.refresh_sleep_label()
        self.remember()

    def _sleep_line(self, name: str, deadline: datetime) -> str:
        remain = max(0, int((deadline - datetime.now()).total_seconds()))
        hours, rest = divmod(remain, 3600)
        minutes, seconds = divmod(rest, 60)
        when = deadline.strftime("%H:%M")
        day = "明天 " if deadline.date() != datetime.now().date() else ""
        return f"{name}将在{day}{when} 切成全黑 · 剩余 {hours} 小时 {minutes:02d} 分 {seconds:02d} 秒"

    def refresh_sleep_label(self) -> None:
        self._show_sleep_state("countdown")
        self._show_sleep_state("clock")
        lines = []
        if self.play_mode == "blackout":
            lines.append("投影保持全黑。需要继续看时，点「结束全黑」。")
        if self.sleep_note:
            lines.append(self.sleep_note)
        if self.count_deadline:
            lines.append(self._sleep_line("倒计时", self.count_deadline))
        if self.clock_deadline:
            lines.append(self._sleep_line("定时", self.clock_deadline))
        if not lines:
            lines.append("倒计时和定时互不影响。开启后，到点会停掉影片，并把投影切成全黑。")
        text = "\n".join(lines)
        if self.sleep_text.text() != text:
            self.sleep_text.setText(text)

    def on_clock(self) -> None:
        now = datetime.now()
        fired = False
        for kind in ("countdown", "clock"):
            deadline = self._sleep_deadline(kind)
            if deadline and now >= deadline:
                self._set_sleep_deadline(kind, None)
                self._mark_sleep_switch(kind, False)
                fired = True
        if fired:
            self.sleep_note = ""
            self.refresh_sleep_label()
            self.sleep_text.setText("时间到了，正在把投影切成全黑…")
            self.remember()
            self.engine.command("blackout")
            return
        self.refresh_sleep_label()

    def toggle_log(self) -> None:
        self.log_box.setVisible(not self.log_box.isVisible())
        self.detail_btn.setText("收起" if self.log_box.isVisible() else "详情")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def keyPressEvent(self, event):
        focus = self.focusWidget()
        editing = isinstance(focus, (QLineEdit, QSpinBox, QComboBox, QAbstractSpinBox, QPlainTextEdit))
        if event.key() == Qt.Key_Space and not editing:
            if self.play_mode == "playing":
                self.engine.command("pause")
            elif self.play_mode == "paused":
                self.engine.command("resume")
            else:
                self.play_row(None)
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.position_hints()

    def position_hints(self) -> None:
        self.drop_hint.setGeometry(self.playlist.viewport().rect().adjusted(28, 0, -28, 0))
        self.device_hint.setGeometry(self.device_list.viewport().rect().adjusted(20, 0, -20, 0))

    def closeEvent(self, event):
        self.remember()
        self.clock.stop()
        self.bridge.blockSignals(True)
        try:
            self.engine.command("stop")
        except Exception:
            pass
        self.engine.stop_event.set()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        self.pool.shutdown(wait=False, cancel_futures=True)
        event.accept()


def main() -> None:
    log_path = APP_DIR / "cache" / "qingying.log"

    def excepthook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except Exception:
            pass
        QMessageBox.critical(None, "清映", str(exc))

    sys.excepthook = excepthook
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    family = "Microsoft YaHei UI"
    if family not in QFontDatabase.families():
        family = "Microsoft YaHei"
    app.setFont(QFont(family, 10))
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
