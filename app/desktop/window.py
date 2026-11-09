from __future__ import annotations

from datetime import date
from dataclasses import replace
import json
import logging
from pathlib import Path
from threading import Thread
import time

from PySide6.QtCore import QDate, QObject, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImageReader, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QProgressBar, QScrollArea, QSizePolicy,
    QListWidget, QListWidgetItem, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from app import db
from app.core.config import settings
from app.core.progress import progress_scope
from app.core.cancellation import CancellationToken, WorkCancelled, cancellation_scope
from app.desktop.bootstrap import VERSION, data_directory
from app.desktop.batch_dialogs import DownloadProgressDialog, ProjectSelectionDialog
from app.desktop.downloads import DownloadManager, task_key
from app.desktop.cache_store import CacheStore, format_bytes
from app.desktop.cache_cleanup import CacheMaintenance, CACHE_CHECK_INTERVAL_MS
from app.desktop.cache_dialog import CacheDialog
from app.desktop.image_export import save_original_image, downloads_directory
from app.desktop.task_dialog import TaskBrowserDialog
from app.desktop.tasks import CATEGORIES, TaskQuery, filter_tasks, project_category, review_summaries, task_rows
from app.desktop.network import EasyConnectManager
from app.desktop.sync import DesktopSync
from app.desktop.theme import app_icon, icon
from app.desktop.widgets import ExitDialog, ImageViewer, NoticeDialog, ProgressDialog, button, label
from app.services.review_service import ReviewService


logger = logging.getLogger(__name__)


class JobSignals(QObject):
    finished = Signal(object, object, object)
    progress = Signal(object)
    download_changed = Signal(object)
    network_finished = Signal(object)
    cache_cleaned = Signal(object)
    image_saved = Signal(object, object)


class ReviewWindow(QMainWindow):
    def __init__(self, *, check_network: bool = True, defer_restore: bool = False):
        super().__init__()
        self.service = ReviewService()
        db.init_db()
        self.sync = DesktopSync(self.service)
        self.projects = [p for p in settings.projects if p.enabled]
        self.project_by_id = {p.id: p for p in settings.projects}
        self.state = None
        self.active_key, self.pending_open_key = None, None
        self.ready_priority = None
        self.job_items, self.review_marks = {}, {}
        self.task_jobs, self.task_reviews = {}, review_summaries()
        self.cache_dialog, self.task_browser = None, None
        self.selection_dialog = None
        self.restoring = True
        self.network_checking = False
        self.foreground_token, self.exit_dialog = None, None
        self.network_token = CancellationToken()
        self.busy, self.closed = False, False
        self.progress_dialog = None
        self.notice = None
        self.controls, self.decision_buttons, self.shortcuts = [], [], []
        self.preferences = QSettings(str(Path(db.DB_PATH).resolve().parent / "desktop-preferences.ini"), QSettings.Format.IniFormat)
        try:
            self.task_query = TaskQuery(**json.loads(self.preferences.value("task_query", "{}")))
        except (ValueError, TypeError):
            self.task_query = TaskQuery()
        self.task_refresh_timer = QTimer(self)
        self.task_refresh_timer.setSingleShot(True)
        self.task_refresh_timer.setInterval(100)
        self.task_refresh_timer.timeout.connect(self._refresh_task_list)
        self.signals = JobSignals(self)
        self.signals.finished.connect(self._finished)
        self.signals.progress.connect(self._progress)
        self.signals.download_changed.connect(self._download_changed)
        self.signals.network_finished.connect(self._network_checked)
        self.signals.cache_cleaned.connect(self._background_cache_cleaned)
        self.signals.image_saved.connect(self._image_saved)
        self._build()
        self.download_dialog = DownloadProgressDialog(self)
        self.download_dialog.open_review.connect(self._open_task)
        self.download_dialog.retry.connect(self._retry_task)
        self.download_dialog.stop.connect(self._stop_task)
        self.download_dialog.stop_all.connect(self._stop_all_tasks)
        self.downloads = DownloadManager(settings.projects,
            lambda job: self.signals.download_changed.emit(job) if not self.closed else None, service=self.service,
            state_path=Path(db.DB_PATH).parent / "download_tasks.json")
        self.cache_maintenance = CacheMaintenance(CacheStore(settings.storage.data_root), self.downloads,
            finished=lambda result: self.signals.cache_cleaned.emit(result) if not self.closed else None)
        self.cache_timer = QTimer(self)
        self.cache_timer.setInterval(CACHE_CHECK_INTERVAL_MS)
        self.cache_timer.timeout.connect(self._check_cache_limit)
        if not defer_restore:
            self.downloads.restore_sessions()
            self.restoring = False
            QTimer.singleShot(0, self._maybe_open_ready)
        if not check_network:
            self.vpn_status.setText("●  网络尚未检查")
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self._try_sync)
        if not defer_restore:
            self.sync_timer.start(5000)
            self.cache_timer.start()
            QTimer.singleShot(5000, self._check_cache_limit)
        if check_network and not defer_restore:
            QTimer.singleShot(250, self.check_network)

    def finish_startup(self, *, check_network=True):
        self.restoring = False
        self.sync_timer.start(5000)
        self.cache_timer.start()
        QTimer.singleShot(5000, self._check_cache_limit)
        QTimer.singleShot(0, self._maybe_open_ready)
        if check_network:
            QTimer.singleShot(250, self.check_network)

    def _build(self):
        self.setWindowTitle(f"数据抽样审核  ·  {VERSION}")
        self.setWindowIcon(app_icon())
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1450, screen.width() - 48), min(950, screen.height() - 60))
        self.setMinimumSize(min(1080, screen.width()), min(680, screen.height()))
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        workspace = QWidget()
        layout.addWidget(workspace, 1)
        main = QVBoxLayout(workspace)
        main.setContentsMargins(26, 22, 26, 16)
        main.setSpacing(16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(5)
        titles.addWidget(label("图片审核", heading=True))
        self.subtitle = label("未选择审核任务", muted=True)
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.cache_button = button("缓存空间", self.show_cache_manager, glyph="folder")
        self.controls.append(self.cache_button)
        header.addWidget(self.cache_button)
        self.download_status_button = button("下载进度", self.show_download_progress, glyph="download", color="#6658E8")
        self.download_status_button.setEnabled(False)
        header.addWidget(self.download_status_button)
        self.saved_badge = label("本地自动保存")
        self.saved_badge.setStyleSheet("color: #778298; background: #EBEEF5; padding: 7px 12px; border-radius: 8px; font-size: 12px;")
        header.addWidget(self.saved_badge)
        main.addLayout(header)
        main.addLayout(self._statistics())
        main.addWidget(self._viewer_panel(), 1)
        main.addWidget(self._recognition_panel())
        decisions = QHBoxLayout()
        decisions.setSpacing(10)
        for text, decision, glyph, color in (
            ("正确  ·  快捷键 1", "correct", "check", "#27845B"),
            ("错误  ·  快捷键 2", "incorrect", "close", "#D05C68"),
            ("模糊  ·  快捷键 3", "invalid", "skip", "#71798D"),
            ("其他  ·  快捷键 4", "other", "info", "#8D75B5")):
            action = button(text, lambda _checked=False, d=decision: self.decide(d), glyph=glyph, color=color)
            action.setProperty("decision", decision)
            if decision in {'invalid', 'other'}:
                action.setToolTip('独立记录，不计入正确率，并自动补抽')
            action.setMinimumHeight(48)
            decisions.addWidget(action, 1)
            self.decision_buttons.append(action)
        main.addLayout(decisions)
        footer = QHBoxLayout()
        self.status = label("就绪 · 图片和审核结果将自动保存在本机。", muted=True)
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.activity = QProgressBar()
        self.activity.setFixedWidth(65)
        self.activity.setTextVisible(False)
        self.activity.setRange(0, 0)
        self.activity.hide()
        footer.addWidget(self.activity)
        main.addLayout(footer)
        for key, callback in (("1", lambda: self.decide("correct")), ("2", lambda: self.decide("incorrect")),
                              ("3", lambda: self.decide("invalid")), ("4", lambda: self.decide("other")), ("R", self.rotate),
                              ("Left", lambda: self.navigate("previous")), ("Right", lambda: self.navigate("next"))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda cb=callback: self._shortcut(cb))
            self.shortcuts.append(shortcut)
        self._refresh_enabled()

    def _sidebar(self):
        shell = QFrame()
        shell.setObjectName("sidebar")
        shell.setFixedWidth(286)
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        brand = QHBoxLayout()
        brand.setContentsMargins(24, 24, 20, 20)
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(35, 35))
        brand.addWidget(mark)
        name = QVBoxLayout()
        name.setSpacing(1)
        title = label("数据抽样审核")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        name.addWidget(title)
        brand.addLayout(name, 1)
        outer.addLayout(brand)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        panel = QWidget()
        panel.setObjectName("sidebarPanel")
        scroll.setWidget(panel)
        outer.addWidget(scroll, 1)
        box = QVBoxLayout(panel)
        box.setContentsMargins(20, 0, 20, 18)
        box.setSpacing(12)
        nav = label("   图片审核")
        nav.setStyleSheet("background: #F0EDFE; color: #6658E8; border-radius: 9px; padding: 13px 14px; font-weight: 600;")
        box.addWidget(nav)
        box.addSpacing(9)
        self.fetch_button = button("拉取数据 · 选择点位", self.prepare, variant="primary", glyph="download", color="#FFFFFF")
        self.fetch_button.setMinimumHeight(46)
        self.controls.append(self.fetch_button)
        box.addWidget(self.fetch_button)
        task_heading = QHBoxLayout()
        task_heading.addWidget(label("本地审核任务", section=True), 1)
        task_heading.addWidget(button("筛选 / 排序", self.show_task_browser, variant="ghost"))
        box.addLayout(task_heading)
        self.task_search = QLineEdit(self.task_query.search)
        self.task_search.setPlaceholderText("搜索点位或日期")
        self.task_search.setClearButtonEnabled(True)
        self.task_search.textChanged.connect(self._task_filters_changed)
        box.addWidget(self.task_search)
        self.task_category = QComboBox()
        self.task_category.addItem("全部项目分类", "")
        for category in dict.fromkeys((*CATEGORIES, *(project_category(p) for p in settings.projects))):
            self.task_category.addItem(category, category)
        self.task_category.setCurrentIndex(max(0, self.task_category.findData(self.task_query.category)))
        self.task_category.currentIndexChanged.connect(self._task_filters_changed)
        box.addWidget(self.task_category)
        self.task_count = label("尚无任务", muted=True)
        box.addWidget(self.task_count)
        self.task_list = QListWidget()
        self.task_list.setMinimumHeight(200)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.task_list.setSpacing(5)
        self.task_list.setStyleSheet("QListWidget { background: transparent; border: none; outline: 0; } QListWidget::item { border: 1px solid #ECEEF5; border-radius: 9px; padding: 3px; } QListWidget::item:selected { background: #F0EDFE; border-color: #CBC4F7; } QListWidget::item:disabled { border: none; background: transparent; }")
        self.task_list.itemClicked.connect(lambda item: self._open_task(item.data(Qt.ItemDataRole.UserRole)) if item.data(Qt.ItemDataRole.UserRole) else None)
        self.controls.append(self.task_list)
        box.addWidget(self.task_list, 1)
        self.empty_tasks = label("还没有任务，点击上方按钮添加。", muted=True)
        self.empty_tasks.setWordWrap(True)
        box.addWidget(self.empty_tasks)
        line = QFrame()
        line.setObjectName("line")
        box.addWidget(line)
        box.addWidget(label("统计同步", section=True))
        self.auto_upload = QCheckBox("审核完成后自动同步轻推")
        self.auto_upload.setChecked(self.preferences.value("auto_upload", True, type=bool))
        self.auto_upload.toggled.connect(lambda value: self.preferences.setValue("auto_upload", value))
        self.controls.append(self.auto_upload)
        box.addWidget(self.auto_upload)
        self.upload_button = button("立即同步 / 重试", self.upload, glyph="cloud")
        box.addWidget(self.upload_button)
        self.auth_button = button("轻推登录与验证", self.authenticate, variant="ghost", glyph="shield")
        self.controls.append(self.auth_button)
        box.addWidget(self.auth_button)
        box.addStretch(1)
        network = QFrame()
        network.setObjectName("card")
        network_box = QVBoxLayout(network)
        network_box.setContentsMargins(12, 12, 12, 12)
        network_box.setSpacing(4)
        self.vpn_status = label("●  正在检查企业内网")
        self.vpn_status.setStyleSheet("font-size: 12px; color: #8A829D;")
        self.vpn_status.setWordWrap(True)
        network_box.addWidget(self.vpn_status)
        self.vpn_button = button("检查 / 启动 EasyConnect", self.check_network, variant="ghost", glyph="shield")
        self.vpn_button.setStyleSheet("font-size: 11px; padding: 4px 0;")
        self.controls.append(self.vpn_button)
        network_box.addWidget(self.vpn_button)
        box.addWidget(network)
        local = button("本地图片与审核记录", self.open_data, variant="ghost", glyph="folder")
        box.addWidget(local)
        box.addWidget(label(f"桌面版 {VERSION}  ·  Windows", muted=True))
        return shell

    def _statistics(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        self.stats = {}
        for key, title, color in (("progress", "有效审核进度", "#403851"), ("correct", "正确", "#29946A"),
                                  ("incorrect", "错误", "#D5717B"), ("invalid", "无效 / 已补抽", "#8D94A7"),
                                  ("other", "其他/已补抽", "#8D75B5"),
                                  ("accuracy", "识别准确率", "#6658E8")):
            card = QFrame()
            card.setObjectName("stat")
            box = QVBoxLayout(card)
            box.setContentsMargins(12, 12, 12, 12)
            box.setSpacing(6)
            box.addWidget(label(title, muted=True))
            value = label("—" if key == "accuracy" else "0 / 50" if key == "progress" else "0")
            value.setStyleSheet(f"font-family: 'Segoe UI'; font-size: {21 if key == 'accuracy' else 27}px; font-weight: 600; color: {color};")
            self.stats[key] = value
            box.addWidget(value)
            if key == "progress":
                self.review_progress = QProgressBar()
                self.review_progress.setTextVisible(False)
                self.review_progress.setValue(0)
                box.addWidget(self.review_progress)
            row.addWidget(card, 2 if key == "progress" else 1)
        return row

    def _viewer_panel(self):
        panel = QFrame()
        panel.setObjectName("viewer")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("viewerToolbar")
        top = QHBoxLayout(toolbar)
        top.setContentsMargins(16, 9, 12, 9)
        self.item_info = label("现场图片")
        self.item_info.setStyleSheet("color: #C7CFDF; font-size: 12px;")
        top.addWidget(self.item_info, 1)
        self.zoom_label = label("适应窗口")
        self.zoom_label.setStyleSheet("color: #7E8A9F; font-size: 11px;")
        top.addWidget(self.zoom_label)
        for text, glyph, action, hint in (("", "minus", lambda: self.viewer.change_zoom(1/1.2), "缩小"),
            ("", "plus", lambda: self.viewer.change_zoom(1.2), "放大"),
            ("适应", "fit", lambda: self.viewer.fit(), "适应窗口"),
            ("旋转", "rotate", self.rotate, "顺时针旋转 90° · 快捷键 R")):
            tool = button(text, action, variant="tool", glyph=glyph, color="#C7CFDF")
            tool.setToolTip(hint)
            top.addWidget(tool)
        layout.addWidget(toolbar)
        self.image_stack = QStackedWidget()
        self.image_stack.setMinimumHeight(210)
        empty = QFrame()
        empty.setObjectName("imageStage")
        empty_box = QVBoxLayout(empty)
        empty_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(icon("image", "#505A73", 58).pixmap(58, 58))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.addWidget(mark)
        title = label("暂无审核图片")
        title.setStyleSheet("font-size: 19px; font-weight: 500; color: #C7CFDF;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.addWidget(title)
        text = label("点击左侧“拉取数据”，选择点位和日期。")
        text.setStyleSheet("font-size: 12px; color: #79859B;")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_box.addWidget(text)
        self.image_stack.addWidget(empty)
        self.viewer = ImageViewer()
        self.viewer.zoom_changed.connect(self.zoom_label.setText)
        self.viewer.save_requested.connect(self.save_current_image)
        self.image_stack.addWidget(self.viewer)
        layout.addWidget(self.image_stack, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(14, 8, 14, 8)
        self.prev_button = button("上一张", lambda: self.navigate("previous"), variant="tool", glyph="left", color="#C7CFDF")
        self.next_button = button("下一张", lambda: self.navigate("next"), variant="tool", glyph="right", color="#C7CFDF")
        bottom.addWidget(self.prev_button)
        self.image_hint = label("滚轮缩放 · 拖动查看 · 快捷键 R 旋转")
        self.image_hint.setStyleSheet("font-size: 11px; color: #79859B;")
        self.image_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bottom.addWidget(self.image_hint, 1)
        bottom.addWidget(self.next_button)
        layout.addLayout(bottom)
        return panel

    def _recognition_panel(self):
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(18, 12, 18, 12)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(label("OCR 识别结果", muted=True))
        self.recognition = label("等待图片")
        self.recognition.setTextFormat(Qt.TextFormat.PlainText)
        self.recognition.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.recognition.setWordWrap(True)
        self.recognition.setStyleSheet("font-family: 'Cascadia Mono', 'Microsoft YaHei UI'; font-size: 22px; font-weight: 600; letter-spacing: 1px;")
        text.addWidget(self.recognition)
        row.addLayout(text, 1)
        self.review_badge = label("未开始")
        self.review_badge.setStyleSheet("background: #F0EDF9; color: #8A80A1; padding: 7px 12px; border-radius: 7px; font-size: 12px;")
        row.addWidget(self.review_badge)
        return card

    def _current_project(self):
        project_id = self.state["project_id"] if self.state else (self.active_key or "").split(":", 1)[0]
        return self.project_by_id.get(project_id) or (self.projects[0] if self.projects else None)

    def _clear_review(self, target=50):
        self.state, self.active_key = None, None
        self.downloads.protect_review_dates(set())
        self.viewer.set_image(None)
        self.image_stack.setCurrentIndex(0)
        self.recognition.setText("等待图片")
        self.review_badge.setText("未开始")
        self.item_info.setText("现场图片")
        for key, value in self.stats.items():
            value.setText("—" if key == "accuracy" else f"0 / {target}" if key == "progress" else "0")
        self.review_progress.setValue(0)
        self._refresh_enabled()

    def _refresh_enabled(self):
        for control in self.controls:
            control.setEnabled(not self.busy)
        can_review = self.state is not None and not self.busy
        for control in self.decision_buttons:
            control.setEnabled(can_review)
        self.prev_button.setEnabled(can_review and self.state["current_index"] > 0)
        self.next_button.setEnabled(can_review and self.state["current_index"] < self.state["queue_length"] - 1)
        self.upload_button.setEnabled(can_review and self.state["complete"])
        for shortcut in self.shortcuts:
            shortcut.setEnabled(not self.busy)
        self.vpn_button.setEnabled(not self.busy and not self.network_checking)

    def _run(self, work, callback, message, *, modal=None, retry=None):
        if self.busy or self.closed:
            return
        self.busy = True
        token = self.foreground_token = CancellationToken()
        self.status.setText(message)
        self.activity.show()
        self._refresh_enabled()
        if modal:
            self.progress_dialog = ProgressDialog(self, title=modal, detail=message, fetching=modal == "正在准备审核图片")
            self.progress_dialog.exit_requested.connect(self.request_exit)
            self.progress_dialog.show()

        def worker():
            try:
                with progress_scope(self.signals.progress.emit, cancellation_token=token):
                    result = work()
                if not self.closed:
                    self.signals.finished.emit(callback, result, None)
            except Exception as exc:
                if not isinstance(exc, WorkCancelled) and not token.cancelled:
                    logger.exception("Desktop operation failed")
                if not self.closed:
                    self.signals.finished.emit(callback, None, (str(exc), retry))
        Thread(target=worker, daemon=True).start()

    def _progress(self, event):
        if self.progress_dialog:
            self.progress_dialog.update_progress(event)
        if self.cache_dialog:
            self.cache_dialog.update_progress(event)

    def _finished(self, callback, result, error):
        if self.closed:
            return
        cancelled = self.foreground_token is not None and self.foreground_token.cancelled
        self.foreground_token = None
        self.busy = False
        self.activity.hide()
        if self.progress_dialog:
            self.progress_dialog.finish()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None
        self._refresh_enabled()
        if error:
            if self.cache_dialog and self.cache_dialog.cleaning:
                self.cache_dialog.set_cleaning(False)
                self.cache_dialog.refresh()
            if cancelled:
                self.status.setText("当前操作已停止，已保存结果保留。")
                return
            detail, retry = error
            if self.state:
                self.downloads.protect_review_dates({self.state["business_date"]})
                self.active_key = task_key(self.state["project_id"], self.state["business_date"])
                if self.active_key in self.job_items:
                    self.task_list.setCurrentItem(self.job_items[self.active_key][0])
            self.status.setText("操作未完成 · 已完成的审核结果仍保存在本机。")
            self._notice("操作失败", self._friendly_error(detail), detail=detail, retry=retry)
            return
        try:
            callback(result)
        except Exception as exc:
            logger.exception("Desktop UI update failed")
            self._notice("无法显示当前图片", "审核记录已保存，请重新拉取当前任务后继续。", detail=str(exc))
        self._maybe_open_ready()

    @staticmethod
    def _friendly_error(detail):
        if "所选日期" in detail or "图片缓存正在清理" in detail:
            return detail
        if "只有" in detail or "没有可审核" in detail or "没有抓到" in detail:
            return detail
        if any(word in detail.lower() for word in ("vpn", "内网", "connection", "timeout", "timed out", "远程通道")):
            return "数据服务暂时无法连接。请确认 EasyConnect 已登录、企业内网可访问后重试。当前审核进度不会丢失。"
        if any(word in detail for word in ("登录", "扫码", "验证")):
            return "登录或验证尚未完成。请完成轻推验证后重试同步；网站账号问题请联系维护人员。审核结果已保存在本机。"
        return "请稍后重试。已完成的审核结果和已下载图片会保留，可在“详细信息”中查看原因。"

    def _notice(self, title, text, **kwargs):
        self.notice = NoticeDialog(self, title, text, **kwargs)
        self.notice.show()

    def check_network(self):
        if self.network_checking or self.closed:
            return
        project = self._current_project()
        source = project.source if project else {}
        url = source.get("base_url") or source.get("history_url")
        if not url:
            self.vpn_status.setText("●  请先选择已配置数据地址的点位")
            return
        self.network_checking = True
        self.vpn_button.setEnabled(False)
        def work():
            try:
                with cancellation_scope(self.network_token):
                    result = EasyConnectManager().ensure(url)
            except WorkCancelled:
                return
            except Exception:
                logger.exception("Network check failed")
                result = {"available": False, "message": "网络检查未完成，请确认 VPN 已连接"}
            if not self.closed:
                self.signals.network_finished.emit(result)
        Thread(target=work, daemon=True).start()

    def _network_checked(self, result):
        self.network_checking = False
        self.vpn_status.setText("●  " + result["message"])
        color = "#29946A" if result["available"] else "#B1874A"
        self.vpn_status.setStyleSheet(f"color: {color}; font-size: 12px;")
        if not self.state and not self.downloads.has_active():
            self.status.setText("内网已连接 · 可以选择多个点位并发拉取。" if result["available"] else result["message"])
        self._refresh_enabled()

    def _state_with_image(self, state):
        image = None
        if state.get("current"):
            path = self.service.materialize_item_image(state["current"]["item_id"])
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                raise RuntimeError("本地图片无法解码，请重新下载该图片")
        return state, image

    def prepare(self):
        if self.busy:
            return
        self.selection_dialog = ProjectSelectionDialog(self, self.projects,
            selected=self.preferences.value("last_selected", []),
            day=getattr(self, "last_day", None),
            target=self.preferences.value("last_target", 50, type=int))
        self.selection_dialog.submitted.connect(self.start_downloads)
        self.selection_dialog.show()

    def start_downloads(self, project_ids, day, target):
        if self.busy:
            return
        if self.state and self.state["project_id"] in project_ids and self.state["business_date"] == str(day) and self.state["target_size"] != target:
            self.review_marks.pop(task_key(self.state["project_id"], self.state["business_date"]), None)
            self._clear_review(target)
        self.preferences.setValue("last_selected", list(project_ids))
        self.preferences.setValue("last_target", target)
        self.last_day = QDate(day.year, day.month, day.day)
        self.ready_priority = [task_key(pid, day) for pid in project_ids]
        self.download_dialog.set_scope(self.ready_priority, day, target)
        keys = self.downloads.submit(project_ids, day, target)
        self.status.setText(f"正在拉取 {len(keys)} 个点位的数据。")
        self.show_download_progress()
        self._maybe_open_ready()

    def show_download_progress(self):
        if not hasattr(self, "download_dialog"):
            return
        for job in self.downloads.snapshots():
            self.download_dialog.update_job(job)
        self.download_dialog.showNormal()
        self.download_dialog.raise_()
        self.download_dialog.activateWindow()

    def _download_changed(self, job):
        if self.closed:
            return
        key = job["key"]
        self.download_dialog.update_job(job)
        previous = self.task_jobs.get(key)
        self.task_jobs[key] = job
        if key not in self.review_marks and key in self.task_reviews:
            review = self.task_reviews[key]
            self.review_marks[key] = "已同步轻推" if review["uploaded"] else "审核完成" if review["complete"] else f"审核 {review['valid']}/{review['target']}"
        if key in self.job_items:
            self._set_task_detail(job, *self.job_items[key])
        if previous is None or previous["status"] != job["status"]:
            self.task_refresh_timer.start()
        snapshots = self.downloads.snapshots()
        ready = sum(j["status"] == "ready" for j in snapshots)
        active = sum(j["status"] in {"queued", "running", "stopping"} for j in snapshots)
        self.download_status_button.setEnabled(True)
        self.download_status_button.setText(f"下载进度  {ready}/{len(snapshots)}" + (f" · {active} 个后台任务" if active else ""))
        if job["status"] == "ready" and not self.restoring:
            self._maybe_open_ready()
        if previous and previous["status"] in {"queued", "running", "stopping"} and job["status"] not in {"queued", "running", "stopping"}:
            self._check_cache_limit()

    def _set_task_detail(self, job, item, detail):
        key = job["key"]
        status = {"queued": "排队", "running": "准备中", "stopping": "正在停止", "cancelled": "已停止 · 可继续",
                  "ready": self.review_marks.get(key, "可开始审核"), "failed": "失败 · 可重试", "paused": "待补齐缓存"}[job["status"]]
        if job["status"] == "running":
            if job.get("images_total") is not None:
                status = f"复核图 {job.get('images_ready', 0)}/{job['images_total']} 张"
            elif job.get("records_current") is not None:
                status = f"读取记录 {job['records_current']} 条"
            else:
                status = "检查连接" if job.get("stage") == "network" else "等待读取记录"
        detail.setText(f"{job['business_date']}  ·  {status}")
        detail.setStyleSheet(f"font-size: 11px; color: {'#29946A' if job['status'] == 'ready' else '#B1874A' if job['status'] in {'failed','paused'} else '#8A829D'};")
        item.setToolTip(job["message"] + ("\n" + job["error"] if job["error"] else ""))

    def _all_task_rows(self):
        return task_rows(self.downloads.snapshots(), self.project_by_id, self.task_reviews)

    def _task_filters_changed(self, *_args):
        self.task_query = replace(self.task_query, search=self.task_search.text().strip(), category=self.task_category.currentData())
        self.preferences.setValue("task_query", json.dumps(self.task_query.to_dict(), ensure_ascii=False))
        self.task_refresh_timer.start()

    def _refresh_task_list(self):
        if self.closed or not hasattr(self, "downloads"):
            return
        self.task_refresh_timer.stop()
        rows = self._all_task_rows()
        visible = filter_tasks(rows, self.task_query)
        keys = {row["key"] for row in visible}
        scroll = self.task_list.verticalScrollBar().value()
        self.task_list.clear()
        self.job_items = {}
        group = None
        for row in visible + [row for row in rows if row["key"] not in keys]:
            show = row["key"] in keys
            if show and group != row["category"]:
                group = row["category"]
                header = QListWidgetItem(f"{group}  ·  {sum(r['category'] == group for r in visible)} 项")
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setSizeHint(QSize(218, 28))
                self.task_list.addItem(header)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row["key"])
            item.setSizeHint(QSize(218, 103))
            self.task_list.addItem(item)
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(8, 7, 8, 7)
            layout.setSpacing(5)
            name = label(row["name"])
            name.setWordWrap(True)
            name.setStyleSheet("font-size: 12px; font-weight: 500;")
            layout.addWidget(name, 1)
            detail = label("", muted=True)
            detail.setWordWrap(True)
            layout.addWidget(detail)
            self.task_list.setItemWidget(item, widget)
            self.job_items[row["key"]] = item, detail
            self._set_task_detail(row, item, detail)
            item.setHidden(not show)
            if row["key"] == self.active_key: self.task_list.setCurrentItem(item)
        self.task_list.verticalScrollBar().setValue(scroll)
        self.task_count.setText(f"显示 {len(visible)} / {len(rows)} 项" + (f" · {self.task_query.start} 至 {self.task_query.end}" if self.task_query.start else ""))
        self.task_count.setWordWrap(True)
        self.empty_tasks.setText("没有符合条件的任务，请调整筛选。" if rows else "还没有任务，点击上方按钮添加。")
        self.empty_tasks.setVisible(not visible)
        if self.task_browser and self.task_browser.isVisible(): self.task_browser.update_rows(rows)

    def show_task_browser(self):
        if self.task_browser:
            self.task_browser.hide()
            self.task_browser.deleteLater()
        self.task_browser = TaskBrowserDialog(self, self.task_query, self._all_task_rows())
        self.task_browser.query_applied.connect(self._apply_task_query)
        self.task_browser.open_task.connect(self._open_task)
        self.task_browser.show()

    def _apply_task_query(self, query):
        self.task_query = query
        for control in (self.task_search, self.task_category): control.blockSignals(True)
        self.task_search.setText(query.search)
        self.task_category.setCurrentIndex(max(0, self.task_category.findData(query.category)))
        for control in (self.task_search, self.task_category): control.blockSignals(False)
        self.preferences.setValue("task_query", json.dumps(query.to_dict(), ensure_ascii=False))
        self._refresh_task_list()

    def show_cache_manager(self):
        if self.cache_dialog is None:
            self.cache_dialog = CacheDialog(self, CacheStore(settings.storage.data_root),
                lambda: {j['business_date'] for j in self.downloads.snapshots() if j['status'] in {'queued','running','stopping'}})
            self.cache_dialog.clear_requested.connect(self.clear_cached_dates)
        self.cache_dialog.show()
        self.cache_dialog.raise_()
        self.cache_dialog.refresh()

    def _check_cache_limit(self):
        if not self.closed and not self.restoring:
            self.cache_maintenance.start()

    def _background_cache_cleaned(self, _result):
        if self.closed:
            return
        # Task state changes arrive through download_changed. Refresh only an
        # already open cache table; no popups, focus changes or status messages.
        if self.cache_dialog and self.cache_dialog.isVisible() and not self.cache_dialog.loading:
            self.cache_dialog.refresh()

    def clear_cached_dates(self, days):
        if self.busy or self.closed:
            if self.cache_dialog and not self.closed:
                self.cache_dialog.selection.setText("当前操作尚未完成，请稍后再清理缓存。")
            return
        self.cache_dialog.set_cleaning(True)
        def work():
            with self.downloads.cache_cleanup(days):
                return self.cache_dialog.store.clear(days, lambda n, total: self.signals.progress.emit({
                    "stage": "cache_cleanup", "message": f"正在清理图片 {n:,} / {total:,}，审核记录保留…", "current": n, "total": total}))
        self._run(work, self._cache_cleaned, "正在清理所选日期的图片缓存，审核记录保留…")

    def _cache_cleaned(self, result):
        if self.state and self.state["business_date"] in result["dates"]:
            self._clear_review(self.state["target_size"])
        self.cache_dialog.set_cleaning(False)
        self.cache_dialog.refresh()
        self._refresh_task_list()
        self.status.setText(f"已清理 {result['removed']:,} 个图片文件，释放 {format_bytes(result['freed'])}；审核结果保留。"
            + (f" {result['failed']} 个文件暂未删除，可稍后重试。" if result["failed"] else "需要时可在任务中继续拉取。"))

    def _maybe_open_ready(self):
        if self.closed or self.busy or self.restoring:
            return
        if self.cache_dialog and self.cache_dialog.isVisible():
            return
        if self.pending_open_key:
            key, self.pending_open_key = self.pending_open_key, None
            if self.downloads.get(key)["status"] == "ready":
                self._open_task(key)
                return
        if self.state is None:
            job = next((j for j in self.downloads.snapshots() if j["status"] == "ready"
                        and (self.ready_priority is None or j["key"] in self.ready_priority)), None)
            if job:
                self._open_task(job["key"], activate=False)

    def _open_task(self, key, *, activate=True):
        if not key:
            return
        job = self.downloads.get(key)
        if job["status"] != "ready":
            self.status.setText("该任务的图片需要补齐，点击“继续拉取”即可沿用原审核记录。" if job["status"] in {"paused", "cancelled", "failed"}
                                else "该点位还在准备，可先审核其他已就绪的点位。")
            self.show_download_progress()
            return
        if self.busy:
            self.pending_open_key = key
            return
        dates = {job["business_date"]}
        if self.state:
            dates.add(self.state["business_date"])
        if not self.downloads.protect_review_dates(dates):
            self.status.setText("该日期正在清理旧图片缓存，完成后可继续拉取。")
            return
        self.download_dialog.hide()
        if activate:
            self.showNormal()
            self.raise_()
            self.activateWindow()
        if key not in self.job_items:
            self._refresh_task_list()
        self.task_list.setCurrentItem(self.job_items[key][0])
        if self.state and task_key(self.state["project_id"], self.state["business_date"]) == key:
            return
        self.active_key = key
        self._run(lambda: self._state_with_image(self.service.state(job["project_id"], date.fromisoformat(job["business_date"]))),
                  self._apply_state, "正在打开本地审核任务…")

    def _retry_task(self, key):
        job = self.downloads.get(key)
        if job["status"] in {"failed", "paused", "cancelled"}:
            self.downloads.submit([job["project_id"]], date.fromisoformat(job["business_date"]), job["target"])

    def _stop_task(self, key):
        self.downloads.cancel(key)
        self.status.setText("已请求停止该点位，已完成缓存保留。")

    def _stop_all_tasks(self):
        count = self.downloads.cancel()
        self.status.setText(f"已请求停止 {count} 个拉取任务，已完成缓存保留。")

    def _apply_state(self, result):
        previous = (self.state or {}).get("current") or {}
        self.state, image = result
        state, item = self.state, self.state.get("current")
        self.downloads.protect_review_dates({state["business_date"]})
        self.active_key = task_key(state["project_id"], state["business_date"])
        self.task_reviews[self.active_key] = {"valid": state["valid_count"], "target": state["target_size"],
            "complete": state["complete"], "uploaded": state.get("uploaded", False), "updated": ""}
        self.task_reviews.update(review_summaries())
        self.task_refresh_timer.start()
        self.stats["progress"].setText(f"{state['valid_count']} / {state['target_size']}")
        self.review_progress.setMaximum(state["target_size"])
        self.review_progress.setValue(state["valid_count"])
        for key in ("correct", "incorrect", "invalid", "other"):
            self.stats[key].setText(str(state[key]))
        self.stats["accuracy"].setText("—" if state["accuracy"] is None else f"{state['accuracy'] * 100:.1f}%")
        self.subtitle.setText(f"{state['business_date']}  /  {self._current_project().name}")
        if item:
            self.recognition.setText(item["recognition_text"] or "（空识别结果）")
            self.item_info.setText(f"现场图片   ·   {state['current_index'] + 1} / {state['queue_length']}")
            kind = {"boxed": "端面框复核图", "original": "原图 · 无端面框数据", "detail": "局部图 · 无完整原图"}.get(item["metadata"].get("review_image_kind"), "")
            self.image_hint.setText(item["metadata"].get("timestamp", "") + ("   ·   " + kind if kind else "") + "   ·   滚轮缩放 / 拖动查看")
            decision = item["decision"]
            self.review_badge.setText({None: "待审核", "correct": "已标记正确", "incorrect": "已标记错误", "invalid": "模糊/无效 · 已补抽", 'other': '其他 · 已补抽'}.get(decision, "待审核"))
            self.viewer.set_image(image, reset=item["item_id"] != previous.get("item_id"))
            self.image_stack.setCurrentIndex(1)
        self._refresh_enabled()
        self.status.setText("审核完成，统计已保存。" if state["complete"] else "审核结果已保存。")
        self.saved_badge.setText("审核已完成" if state["complete"] else "本地已保存")
        self.review_marks[self.active_key] = "审核完成" if state["complete"] else f"审核 {state['valid_count']}/{state['target_size']}"
        if self.active_key in self.job_items:
            self.job_items[self.active_key][1].setText(f"{state['business_date']}  ·  {self.review_marks[self.active_key]}")
        if self.auto_upload.isChecked() and self._current_project().sink.get("enabled"):
            if self.sync.enqueue(state):
                self.status.setText("审核完成 · 正在准备同步轻推…")
                QTimer.singleShot(100, self._try_sync)

    def decide(self, decision):
        if not self.state or self.busy:
            return
        state = self.state
        self._run(lambda: self._state_with_image(self.service.decide(state["project_id"], date.fromisoformat(state["business_date"]),
            state["current"]["queue_id"], decision)), self._apply_state, "正在保存审核结果并准备下一张…")

    def navigate(self, direction):
        if not self.state or self.busy:
            return
        state = self.state
        self._run(lambda: self._state_with_image(self.service.navigate(state["project_id"], date.fromisoformat(state["business_date"]),
            state["current"]["queue_id"], direction)), self._apply_state, "正在读取本地图片…")

    def upload(self):
        if not self.state or not self.state["complete"] or self.busy:
            return
        self.sync.enqueue(self.state)
        self.sync.retry(self.state["project_id"], self.state["business_date"])
        self._try_sync(force=True)

    def _try_sync(self, *, force=False):
        if self.closed or self.busy or not (force or self.auto_upload.isChecked()):
            return
        if self.notice is not None and self.notice.isVisible():
            return
        selection = getattr(self, "selection_dialog", None)
        if selection is not None and selection.isVisible():
            return
        job = self.sync.due()
        if job:
            def done(result):
                self.status.setText("同步成功 · 已写入轻推并完成回读校验。" if result.get("uploaded") else "统计已变更 · 将同步最新结果。")
                if result.get("uploaded"):
                    self.task_reviews.update(review_summaries())
                    self.task_refresh_timer.start()
                if self.state and self.state["project_id"] == job["project_id"] and self.state["business_date"] == job["business_date"] and result.get("uploaded"):
                    self.state["uploaded"] = True
                    self.saved_badge.setText("已同步轻推")
                    key = task_key(job["project_id"], job["business_date"])
                    if key in self.task_reviews:
                        self.task_reviews[key]["uploaded"] = True
                    self.task_refresh_timer.start()
                    self.review_marks[key] = "已同步轻推"
                    if key in self.job_items:
                        self.job_items[key][1].setText(f"{job['business_date']}  ·  已同步轻推")
            self._run(lambda: self.sync.run(job), done, "正在写入统计并校验。首次使用或登录过期时，请在验证窗口完成扫码。", modal="正在同步轻推")

    def authenticate(self):
        project = self._current_project()
        if not project:
            return
        def done(result):
            self.status.setText("轻推验证完成 · 登录状态已保存在本机。")
            if self.state and self.state["complete"] and self.auto_upload.isChecked():
                QTimer.singleShot(100, self.upload)
        self._run(lambda: self.service.prepare_sink_auth(project.id), done,
                  "请在打开的轻推验证窗口完成扫码或手机确认。验证成功后会自动继续。", modal="正在验证轻推登录")

    def rotate(self):
        if not self.busy:
            self.viewer.rotate()

    def save_current_image(self):
        if self.closed or not self.state or self.viewer.image is None or not self.viewer.save_button.isEnabled():
            return
        state, item = self.state, self.state.get('current') or {}
        project = self._current_project()
        name = project.name if project else state['project_id']
        day = state['business_date']
        source_key = item.get('source_key') or item.get('item_id') or '样本'
        from app.desktop.widgets import SaveOriginalDialog
        if getattr(self, 'save_confirmation', None) is not None and self.save_confirmation.isVisible():
            self.save_confirmation.raise_()
            return
        self.save_confirmation = SaveOriginalDialog(self, name, day, downloads_directory())
        self.save_confirmation.confirmed.connect(lambda: self._save_original(item['item_id'], name, day, source_key))
        self.save_confirmation.show()

    def _save_original(self, item_id, name, day, source_key):
        if self.closed:
            return
        self.viewer.save_button.setEnabled(False)
        self.viewer.save_button.setText('正在保存…')
        def work():
            try:
                with self.downloads.cache_reading([day]):
                    original = self.service.materialize_original_image(item_id)
                    path = save_original_image(original, name, day, source_key)
                error = None
            except Exception as exc:
                logger.exception('Could not export review image')
                path, error = None, str(exc)
            if not self.closed:
                self.signals.image_saved.emit(path, error)
        Thread(target=work, name='image-export', daemon=True).start()

    def _image_saved(self, path, error):
        if self.closed:
            return
        self.viewer.save_button.setEnabled(True)
        self.viewer.save_button.setText('保存原图')
        self.status.setText(f'原图未保存：{error[:200]}' if error else f'原图已保存：{path}')

    def _shortcut(self, callback):
        from PySide6.QtWidgets import QAbstractSpinBox, QLineEdit
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QAbstractSpinBox, QComboBox)) or self.busy:
            return
        callback()

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_directory())))

    def closeEvent(self, event):
        if self.closed:
            event.accept()
            return
        if self.busy or self.downloads.has_active():
            event.ignore()
            self.request_exit()
            return
        self._shutdown()
        event.accept()

    def request_exit(self):
        if self.exit_dialog is not None and self.exit_dialog.isVisible():
            self.exit_dialog.raise_()
            return
        count = sum(j["status"] in {"queued", "running", "stopping"} for j in self.downloads.snapshots())
        self.exit_dialog = ExitDialog(self, count)
        self.exit_dialog.confirmed.connect(self._confirm_exit)
        self.exit_dialog.show()

    def _confirm_exit(self):
        self._shutdown()
        self.close()

    def _shutdown(self):
        if self.closed:
            return
        self.closed = True
        if self.foreground_token is not None:
            self.foreground_token.cancel()
        self.network_token.cancel()
        self.sync_timer.stop()
        self.cache_timer.stop()
        self.cache_maintenance.close()
        self.task_refresh_timer.stop()
        self.downloads.shutdown(cancel=True)
        self.download_dialog.hide()
        from PySide6.QtWidgets import QDialog
        for dialog in self.findChildren(QDialog):
            dialog.hide()
