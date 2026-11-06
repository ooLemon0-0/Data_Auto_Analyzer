from __future__ import annotations

from threading import Thread
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QHeaderView, QHBoxLayout, QProgressBar,
    QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.desktop.cache_store import format_bytes
from app.desktop.widgets import button, label


class SizeItem(QTableWidgetItem):
    def __init__(self, text, value):
        super().__init__(text)
        self.value = value

    def __lt__(self, other):
        return self.value < other.value


class CacheCleanupConfirm(QDialog):
    confirmed = Signal()

    def __init__(self, parent, entries):
        super().__init__(parent)
        self.setWindowTitle("确认清理图片缓存")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(540)
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 26, 28, 24)
        box.setSpacing(16)
        box.addWidget(label("清理所选日期的图片？", heading=True))
        preview = "、".join(e.day for e in entries[:12]) + (f" 等 {len(entries)} 个日期" if len(entries) > 12 else "")
        details = label(f"已选 {len(entries)} 个日期，共 {sum(e.files for e in entries):,} 个文件，预计释放 {format_bytes(sum(e.size for e in entries))}。\n\n"
            + preview + "\n\n包含这些日期的所有点位原图和复核图。审核结果、统计及同步记录会保留；之后可重新拉取图片继续审核。")
        details.setWordWrap(True)
        box.addWidget(details)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        self.confirm_button = button("清理这些图片", self._confirm, variant="primary")
        actions.addWidget(self.confirm_button)
        box.addLayout(actions)

    def _confirm(self):
        self.accept()
        self.confirmed.emit()


class CacheDialog(QDialog):
    scanned = Signal(object, object)
    clear_requested = Signal(object)

    def __init__(self, parent, store, active_dates):
        super().__init__(parent)
        self.setWindowTitle("图片缓存管理")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(900, screen.width() - 50), min(650, screen.height() - 60))
        self.store, self.active_dates = store, active_dates
        self.entries, self.loading, self.cleaning = [], False, False
        self.generation = 0
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 22)
        box.setSpacing(14)
        title = QHBoxLayout()
        title.addWidget(label("图片缓存", heading=True), 1)
        self.refresh_button = button("刷新占用", self.refresh, variant="ghost")
        title.addWidget(self.refresh_button)
        box.addLayout(title)
        self.total = label("正在统计本地图片…")
        self.total.setStyleSheet("color: #6658E8; background: #EEEBFC; padding: 14px; border-radius: 10px; font-weight: 600;")
        box.addWidget(self.total)
        hint = label("按采集日期汇总所有点位的原图、复核图和未完成图片文件。只清理程序图片缓存，审核记录、统计、浏览器登录状态均保留。", muted=True)
        hint.setWordWrap(True)
        box.addWidget(hint)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["选择", "采集日期", "点位数", "文件数", "占用空间", "状态"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._selection_changed)
        box.addWidget(self.table, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        box.addWidget(self.progress)
        self.selection = label("尚未选择日期", muted=True)
        self.selection.setWordWrap(True)
        box.addWidget(self.selection)
        actions = QHBoxLayout()
        self.select_all_button = button("全选可清理日期", self.select_all)
        actions.addWidget(self.select_all_button)
        self.clear_selection_button = button("取消选择", self.clear_selection, variant="ghost")
        actions.addWidget(self.clear_selection_button)
        actions.addStretch()
        actions.addWidget(button("关闭", self.hide))
        self.clear_button = button("清理所选缓存", self.confirm_clear, variant="primary")
        self.clear_button.setEnabled(False)
        actions.addWidget(self.clear_button)
        box.addLayout(actions)
        self.scanned.connect(self._scanned)

    def refresh(self):
        if self.cleaning:
            return
        self.loading = True
        self.generation += 1
        generation = self.generation
        self._enabled()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.selection.setText("正在读取磁盘占用…")
        def work():
            try:
                result, error = self.store.scan(), None
            except Exception as exc:
                result, error = [], str(exc)
            self.scanned.emit(generation, (result, error))
        Thread(target=work, daemon=True).start()

    def _scanned(self, generation, payload):
        if generation != self.generation:
            return
        self.entries, error = payload
        self.loading = False
        self.progress.hide()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.entries))
        active = self.active_dates()
        for index, entry in enumerate(self.entries):
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, entry.day)
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable if entry.day not in active else Qt.ItemFlag.NoItemFlags)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, check)
            self.table.setItem(index, 1, QTableWidgetItem(entry.day))
            self.table.setItem(index, 2, SizeItem(str(len(entry.projects)), len(entry.projects)))
            self.table.setItem(index, 3, SizeItem(f"{entry.files:,}", entry.files))
            self.table.setItem(index, 4, SizeItem(format_bytes(entry.size), entry.size))
            status = QTableWidgetItem("正在拉取，请先停止任务" if entry.day in active else "可清理 · 保留审核记录")
            status.setToolTip("\n".join(f"{pid}：{format_bytes(info['size'])}" for pid, info in entry.projects.items()))
            self.table.setItem(index, 5, status)
            self.table.setRowHeight(index, 50)
        # Sorting numeric columns uses their byte / count values, not text.
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)
        self.total.setText(f"图片缓存共 {format_bytes(sum(e.size for e in self.entries))} · {len(self.entries)} 个日期 · {sum(e.files for e in self.entries):,} 个文件")
        self._enabled()
        self._selection_changed()
        if error:
            self.selection.setText("缓存统计未完成，请稍后刷新：" + error[:150])

    def selected_dates(self):
        return [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.table.rowCount())
                if self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def _selection_changed(self, *_args):
        if self.loading or self.cleaning:
            return
        selected = set(self.selected_dates())
        size = sum(e.size for e in self.entries if e.day in selected)
        self.selection.setText(f"已选 {len(selected)} 个日期，预计释放 {format_bytes(size)}" if selected else "勾选日期后批量清理；正在拉取的日期需先停止任务。")
        self.clear_button.setEnabled(bool(selected))

    def select_all(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.flags() & Qt.ItemFlag.ItemIsEnabled: item.setCheckState(Qt.CheckState.Checked)

    def clear_selection(self):
        for row in range(self.table.rowCount()): self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def confirm_clear(self):
        selected = set(self.selected_dates())
        if not selected:
            return
        self.confirmation = CacheCleanupConfirm(self, [e for e in self.entries if e.day in selected])
        self.confirmation.confirmed.connect(lambda: self.clear_requested.emit(sorted(selected)))
        self.confirmation.show()

    def set_cleaning(self, cleaning):
        self.cleaning = cleaning
        self._enabled()
        if cleaning:
            self.progress.setRange(0, 0)
            self.progress.show()
            self.selection.setText("正在清理图片，审核记录保留…")
        else:
            self.progress.hide()

    def update_progress(self, event):
        if self.cleaning and event.get("stage") == "cache_cleanup":
            self.progress.setRange(0, event.get("total") or 1)
            self.progress.setValue(event.get("current") or 0)
            self.selection.setText(event.get("message", "正在清理图片…"))

    def _enabled(self):
        idle = not self.loading and not self.cleaning
        for control in (self.table, self.refresh_button, self.select_all_button, self.clear_selection_button): control.setEnabled(idle)
        self.clear_button.setEnabled(idle and bool(self.selected_dates()))
