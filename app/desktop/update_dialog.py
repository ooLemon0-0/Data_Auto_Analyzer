"""A required update gate; only a verified connectivity failure allows offline use."""
from threading import Thread
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QProgressBar, QVBoxLayout
import requests
from app.core.cancellation import CancellationToken
from app.desktop.bootstrap import VERSION
from app.desktop.updates import (CHECK_DEADLINE_SECONDS, UpdateUnavailable, check_for_update, download_update, launch_installer)
from app.desktop.widgets import button, label


class UpdateDialog(QDialog):
    continue_startup = Signal()
    install_started = Signal()
    exit_requested = Signal()
    checked = Signal(int, object, object)
    downloaded = Signal(int, object, object)
    failed = Signal(int, object)
    progress_changed = Signal(int, int, int)
    update_available = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('应用更新')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(550)
        self.token, self.finished, self.release = CancellationToken(), False, None
        self.generation, self.state = 0, 'checking'
        box = QVBoxLayout(self); box.setContentsMargins(28,26,28,24); box.setSpacing(16)
        self.title = label('正在检查更新', heading=True); box.addWidget(self.title)
        self.message = label(f'当前版本 {VERSION}。发现新版后将自动更新，完成后进入程序。', muted=True)
        self.message.setWordWrap(True); box.addWidget(self.message)
        self.notes = QPlainTextEdit(); self.notes.setReadOnly(True); self.notes.setMaximumHeight(150); self.notes.hide(); box.addWidget(self.notes)
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.setRange(0,0); box.addWidget(self.progress)
        actions = QHBoxLayout(); actions.addStretch()
        self.exit_button = button('退出程序', self.exit); actions.addWidget(self.exit_button)
        self.skip_button = button('离线进入', self.skip); self.skip_button.hide(); actions.addWidget(self.skip_button)
        self.update_button = button('重试', self.retry, variant='primary'); self.update_button.hide(); actions.addWidget(self.update_button)
        box.addLayout(actions)
        self.checked.connect(self._checked); self.downloaded.connect(self._downloaded)
        self.failed.connect(self._failed); self.progress_changed.connect(self._progress)
        self.check_timer = QTimer(self); self.check_timer.setSingleShot(True); self.check_timer.timeout.connect(self._check_timeout)

    def begin(self, *, show=True):
        if self.finished: return
        if show: self.show()
        self.generation += 1
        generation = self.generation
        self.token.cancel(); self.token = CancellationToken(); token = self.token
        self.state = 'checking'; self.release = None
        self.title.setText('正在检查更新'); self.message.setText(f'当前版本 {VERSION}。正在验证最新版本…')
        self.skip_button.hide(); self.update_button.hide(); self.notes.hide(); self.progress.setRange(0,0); self.progress.show()
        self.check_timer.start(CHECK_DEADLINE_SECONDS * 1000)
        def work():
            try: result, error = check_for_update(token), None
            except Exception as exc: result, error = None, exc
            if not self.finished: self.checked.emit(generation, result, error)
        QTimer.singleShot(0, lambda: Thread(target=work, daemon=True).start())

    def _checked(self, generation, release, error=None):
        if self.finished or generation != self.generation or self.state != 'checking': return
        self.check_timer.stop()
        if error is not None:
            self._failed(generation, error); return
        if release is None:
            self.finished = True; self.state = 'finished'; self.hide(); self.continue_startup.emit(); return
        self.release = release; self.state = 'required'
        self.update_available.emit(); self.show()
        self.title.setText(f'需要更新到 {release.version}')
        self.message.setText(f'当前 {VERSION} → {release.version}。更新会保留图片、审核结果和登录状态。')
        self.notes.setPlainText(release.notes or '应用更新'); self.notes.show()
        self.skip_button.hide()
        QTimer.singleShot(0, self.start_download)

    def start_download(self):
        if self.finished or self.release is None or self.state == 'downloading': return
        self.generation += 1; generation = self.generation
        self.token.cancel(); self.token = CancellationToken(); token = self.token
        self.state = 'downloading'; release = self.release
        self.skip_button.hide(); self.update_button.hide(); self.notes.hide()
        self.title.setText('正在更新'); self.message.setText('下载并校验后将自动重启。完成更新前不能进入旧版。')
        self.progress.setRange(0,1000); self.progress.setValue(0); self.progress.show()
        def work():
            try:
                path = download_update(release, token, lambda n,total: self.progress_changed.emit(generation,n,total) if not self.finished else None)
                if not self.finished: self.downloaded.emit(generation,path,release)
            except Exception as error:
                if not self.finished: self.failed.emit(generation,error)
        Thread(target=work,daemon=True).start()

    def _progress(self, generation, current, total):
        if not self.finished and generation == self.generation:
            self.progress.setValue(int(current * 1000 / total))
            self.message.setText(f'已下载 {current/1048576:.1f} / {total/1048576:.1f} MB，正在完成更新。')

    def _downloaded(self, generation, path, release):
        if self.finished or generation != self.generation: return
        try: launch_installer(path,release)
        except Exception as error:
            self._failed(generation,RuntimeError('无法替换当前程序，请确认 EXE 所在目录可写后重试。')); return
        self.finished = True; self.state = 'finished'; self.hide(); self.install_started.emit()

    def _failed(self, generation, error):
        if self.finished or generation != self.generation: return
        self.check_timer.stop()
        offline = isinstance(error,(UpdateUnavailable,requests.ConnectionError,requests.Timeout)) and not isinstance(error,requests.exceptions.SSLError)
        self.state = 'offline' if offline else 'error'
        self.title.setText('无法连接更新服务' if offline else '更新未完成')
        self.message.setText(str(error)[:250] + ('\n可以重试，或离线进入。' if offline else '\n请重试或退出程序；此情况不能跳过更新。'))
        self.progress.hide(); self.skip_button.setVisible(offline); self.update_button.show(); self.show(); self.update_available.emit()

    def _check_timeout(self):
        if self.finished or self.state != 'checking': return
        self.token.cancel(); self.generation += 1
        self._failed(self.generation,UpdateUnavailable('更新服务长时间未响应。'))

    def retry(self):
        if self.finished: return
        if self.release is not None: self.state = 'required'; self.start_download()
        else: self.begin()

    def skip(self):
        if self.finished or self.state != 'offline': return
        self.finished = True; self.state = 'finished'; self.token.cancel(); self.hide(); self.continue_startup.emit()

    def exit(self):
        if self.finished: return
        self.finished = True; self.state = 'finished'; self.check_timer.stop(); self.token.cancel(); self.hide(); self.exit_requested.emit()

    def reject(self): self.exit()

    def closeEvent(self,event): self.exit(); event.accept()
