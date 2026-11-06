from __future__ import annotations

import time
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from app.desktop.theme import app_icon, icon


def label(text="", *, muted=False, heading=False, section=False):
    result = QLabel(text)
    result.setProperty("muted", muted)
    result.setProperty("heading", heading)
    result.setProperty("section", section)
    return result


def button(text, callback=None, *, variant=None, glyph=None, color="#787E90"):
    result = QPushButton(text)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    if variant:
        result.setProperty("variant", variant)
    if glyph:
        result.setIcon(icon(glyph, color))
    if callback:
        result.clicked.connect(callback)
    return result


class ImageViewer(QGraphicsView):
    zoom_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.image = None
        self.pixmap_item = None
        self.rotation, self.zoom = 0, 1.0
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#181C27"))

    def set_image(self, image: QImage | None, *, reset=True):
        self.image = image
        if reset:
            self.rotation, self.zoom = 0, 1.0
        self.redraw()

    def redraw(self):
        self.scene().clear()
        self.pixmap_item = None
        if self.image is not None and not self.image.isNull():
            transformed = self.image.transformed(QTransform().rotate(self.rotation * 90), Qt.TransformationMode.SmoothTransformation)
            self.pixmap_item = self.scene().addPixmap(QPixmap.fromImage(transformed))
            self.scene().setSceneRect(self.pixmap_item.boundingRect())
            self._fit()

    def _fit(self):
        if self.pixmap_item:
            self.resetTransform()
            self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self.scale(self.zoom, self.zoom)
            self.zoom_changed.emit(f"{self.transform().m11() * 100:.0f}%")

    def fit(self):
        self.zoom = 1.0
        self._fit()

    def change_zoom(self, factor):
        self.zoom = max(0.25, min(10.0, self.zoom * factor))
        self._fit()

    def rotate(self):
        self.rotation = (self.rotation + 1) % 4
        self.redraw()

    def wheelEvent(self, event):
        if self.image is not None:
            self.change_zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class ProgressDialog(QDialog):
    """Application-modal job progress, fed by real transport/cache events."""
    exit_requested = Signal()

    def __init__(self, parent=None, *, title="正在准备审核图片", detail="", fetching=True):
        super().__init__(parent)
        self.active = True
        self.fetching = fetching
        self.started = time.monotonic()
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setFixedWidth(510)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 28)
        layout.setSpacing(14)
        heading = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icon("download" if fetching else "cloud", "#6658E8", 30).pixmap(36, 36))
        heading.addWidget(glyph)
        heading.addSpacing(8)
        heading.addWidget(label(title, heading=True), 1)
        layout.addLayout(heading)
        subtitle = label(detail, muted=True)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.steps = []
        if fetching:
            row = QHBoxLayout()
            for text in ("检查连接", "读取记录", "缓存图片", "准备就绪"):
                step = label(text)
                step.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.steps.append(step)
                row.addWidget(step)
            layout.addSpacing(6)
            layout.addLayout(row)
        self.message = label("正在连接数据服务…" if fetching else "正在连接轻推文档…")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        timing = QHBoxLayout()
        self.count = label("请稍候", muted=True)
        self.elapsed = label("已用时 0 秒", muted=True)
        timing.addWidget(self.count)
        timing.addStretch()
        timing.addWidget(self.elapsed)
        layout.addLayout(timing)
        self.warning = label("已保存结果会保留，可以停止当前操作并退出。" if fetching else "如出现验证窗口，请完成扫码；也可以停止当前操作并退出。")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #7D709E; background: #EFECFC; border-radius: 9px; padding: 12px; font-size: 12px;")
        layout.addWidget(self.warning)
        layout.addWidget(button("停止当前操作并退出", self.exit_requested.emit))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self.update_progress({"stage": "network", "message": self.message.text()})

    def _tick(self):
        seconds = int(time.monotonic() - self.started)
        self.elapsed.setText(f"已用时 {seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"已用时 {seconds} 秒")

    def update_progress(self, event):
        stage = event.get("stage")
        step_index = {"network": 0, "records": 1, "sampling": 1, "images": 2, "ready": 3}.get(stage, 0)
        for index, step in enumerate(self.steps):
            color = "#6658E8" if index == step_index else "#39A576" if index < step_index else "#A5AABB"
            step.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {color}; padding: 9px 0;")
        self.message.setText(event.get("message", ""))
        current, total = event.get("current"), event.get("total")
        if current is not None and total is not None and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))
            unit = "张图片" if stage == "images" else "条记录"
            self.count.setText("已完成" if stage == "ready" else f"{current:,} / {total:,} {unit}")
        else:
            self.progress.setRange(0, 0)
            self.count.setText("正在处理，请稍候")

    def finish(self):
        self.active = False
        self.timer.stop()
        self.accept()

    def reject(self):
        if not self.active:
            super().reject()
        else:
            self.exit_requested.emit()

    def closeEvent(self, event):
        if self.active:
            event.ignore()
            self.exit_requested.emit()
        else:
            event.accept()


class ExitDialog(QDialog):
    confirmed = Signal()

    def __init__(self, parent, active_count=0):
        super().__init__(parent)
        self.setWindowTitle("停止并退出")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(17)
        layout.addWidget(label("停止任务并退出？", heading=True))
        text = (f"还有 {active_count} 个拉取任务未结束。" if active_count else "当前操作尚未完成。")
        text += "退出会中断未完成的请求。已保存的审核结果和已完成图片会保留，下次可以继续。"
        body = label(text)
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(button("继续使用", self.reject))
        self.exit_button = button("停止并退出", self._confirm, variant="primary")
        self.exit_button.setDefault(True)
        buttons.addWidget(self.exit_button)
        layout.addLayout(buttons)

    def _confirm(self):
        self.accept()
        self.confirmed.emit()


class NoticeDialog(QDialog):
    def __init__(self, parent, title, text, *, detail="", retry=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(470)
        self.setMaximumWidth(580)
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 26, 28, 24)
        box.setSpacing(16)
        heading = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icon("info", "#6658E8", 28).pixmap(30, 30))
        heading.addWidget(glyph)
        heading.addWidget(label(title, heading=True), 1)
        box.addLayout(heading)
        body = label(text)
        body.setWordWrap(True)
        body.setStyleSheet("line-height: 1.6; color: #6F768B;")
        box.addWidget(body)
        if detail:
            details = QPlainTextEdit(detail)
            details.setReadOnly(True)
            details.setMaximumHeight(160)
            details.hide()
            toggle = button("查看详细信息", lambda: details.setVisible(not details.isVisible()), variant="ghost")
            box.addWidget(toggle)
            box.addWidget(details)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("知道了" if not retry else "稍后重试", self.accept))
        if retry:
            def retry_now():
                self.accept()
                QTimer.singleShot(0, retry)
            actions.addWidget(button("重新拉取", retry_now, variant="primary"))
        box.addLayout(actions)


class StartupDialog(QDialog):
    exit_requested = Signal()
    skip_update = Signal()
    def __init__(self):
        super().__init__()
        self.setWindowTitle("数据抽样审核")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(500, 310)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("dialogCard")
        shell.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(38, 34, 38, 28)
        box.setSpacing(13)
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(50, 50))
        box.addWidget(mark)
        box.addWidget(label("数据抽样审核", heading=True))
        self.stage = label("正在加载界面")
        box.addWidget(self.stage)
        box.addStretch()
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        box.addWidget(self.bar)
        self.count = label("", muted=True)
        box.addWidget(self.count)
        actions = QHBoxLayout()
        self.skip_button = button("跳过更新", self.skip_update.emit, variant="ghost")
        self.skip_button.hide()
        actions.addWidget(self.skip_button)
        actions.addStretch()
        actions.addWidget(button("退出", self.exit_requested.emit, variant="ghost"))
        box.addLayout(actions)

    def set_stage(self, message, current=None, total=None):
        self.stage.setText(message)
        if total and current is not None:
            self.bar.setRange(0, total)
            self.bar.setValue(current)
            self.count.setText(f"{current:,} / {total:,}")
        else:
            self.bar.setRange(0, 0)
            self.count.clear()

    def reject(self):
        self.exit_requested.emit()

    def finish(self):
        self.accept()
