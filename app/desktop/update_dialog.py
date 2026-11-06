"""Cancelable startup update card. Late worker results cannot reopen it."""
from __future__ import annotations

from threading import Thread
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QProgressBar, QVBoxLayout

from app.core.cancellation import CancellationToken
from app.desktop.bootstrap import VERSION
from app.desktop.updates import check_for_update, download_update, launch_installer
from app.desktop.widgets import button, label


class UpdateDialog(QDialog):
    continue_startup = Signal()
    install_started = Signal()
    checked = Signal(object)
    downloaded = Signal(object, object)
    failed = Signal(str)
    progress_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("检查应用更新")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(550)
        self.token, self.finished, self.release = CancellationToken(), False, None
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 26, 28, 24)
        box.setSpacing(16)
        self.title = label("正在检查更新", heading=True)
        box.addWidget(self.title)
        self.message = label(f"当前版本 {VERSION} · 正在连接 GitHub。连接失败会直接进入工作台，也可以现在跳过。", muted=True)
        self.message.setWordWrap(True)
        box.addWidget(self.message)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(150)
        self.notes.hide()
        box.addWidget(self.notes)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        box.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.addStretch()
        self.skip_button = button("跳过更新", self.skip)
        actions.addWidget(self.skip_button)
        self.update_button = button("立即更新", self.start_download, variant="primary")
        self.update_button.hide()
        actions.addWidget(self.update_button)
        box.addLayout(actions)
        self.checked.connect(self._checked)
        self.downloaded.connect(self._downloaded)
        self.failed.connect(self._failed)
        self.progress_changed.connect(self._progress)
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.timeout.connect(self.skip)

    def begin(self):
        self.show()
        # DNS and third-party transports can ignore their own connect timeout.
        # This UI deadline still lets the workbench start within six seconds.
        self.check_timer.start(6000)
        def work():
            result = check_for_update(self.token)
            if not self.finished:
                self.checked.emit(result)
        QTimer.singleShot(0, lambda: Thread(target=work, daemon=True).start())

    def _checked(self, release):
        if self.finished:
            return
        self.check_timer.stop()
        if release is None:
            self.skip()
            return
        self.release = release
        self.title.setText(f"发现新版本 {release.version}")
        self.message.setText(f"当前 {VERSION} → {release.version} · {release.size / 1048576:.1f} MB\n更新会保留本机图片、审核结果和登录状态。")
        self.progress.hide()
        self.notes.setPlainText(release.notes or "维护更新")
        self.notes.show()
        self.update_button.show()

    def start_download(self):
        if self.finished or self.release is None:
            return
        self.token = CancellationToken()
        self.update_button.hide()
        self.notes.hide()
        self.title.setText("正在下载更新")
        self.message.setText("下载完成并校验后会重启应用。你可以随时跳过，继续使用当前版本。")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        token, release = self.token, self.release
        def work():
            try:
                path = download_update(release, token, lambda n, total: self.progress_changed.emit(n, total) if not self.finished else None)
                if not self.finished:
                    self.downloaded.emit(path, release)
            except Exception as exc:
                if not self.finished:
                    self.failed.emit(str(exc))
        Thread(target=work, daemon=True).start()

    def _progress(self, current, total):
        if not self.finished:
            self.progress.setValue(int(current * 1000 / total))
            self.message.setText(f"已下载 {current / 1048576:.1f} / {total / 1048576:.1f} MB · 完成校验后重启；可以随时跳过。")

    def _downloaded(self, path, release):
        if self.finished:
            return
        try:
            launch_installer(path, release)
        except Exception:
            self._failed("无法替换当前程序，请确认程序位于可写入的文件夹，也可以跳过更新继续使用。")
            return
        self.finished = True
        self.hide()
        self.install_started.emit()

    def _failed(self, message):
        if self.finished:
            return
        self.title.setText("本次更新未完成")
        self.message.setText(message[:300] + "\n已保留当前版本，可以跳过或重试。")
        self.progress.hide()
        self.update_button.setText("重试更新")
        self.update_button.show()

    def skip(self):
        if self.finished:
            return
        self.finished = True
        self.check_timer.stop()
        self.token.cancel()
        self.hide()
        self.continue_startup.emit()

    def reject(self):
        self.skip()

    def closeEvent(self, event):
        self.skip()
        event.accept()
