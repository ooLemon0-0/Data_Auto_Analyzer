from __future__ import annotations

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QGridLayout, QHeaderView, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.desktop.tasks import CATEGORIES, SORTS, STATUSES, TaskQuery, cache_caption, filter_tasks, review_caption
from app.desktop.widgets import button, label


class TaskBrowserDialog(QDialog):
    query_applied = Signal(object)
    open_task = Signal(str)

    def __init__(self, parent, query, rows):
        super().__init__(parent)
        self.setWindowTitle("查找本地审核任务")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1080, screen.width() - 50), min(690, screen.height() - 60))
        self.rows = rows
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 22)
        box.setSpacing(14)
        box.addWidget(label("本地任务库", heading=True))
        box.addWidget(label("按项目分类查看，支持日期范围、审核状态和排序。筛选不会更改审核记录。", muted=True))
        self.search = QLineEdit(query.search)
        self.search.setPlaceholderText("搜索点位、项目、日期；多个关键词用空格分隔")
        self.search.setClearButtonEnabled(True)
        box.addWidget(self.search)
        filters = QGridLayout()
        filters.setHorizontalSpacing(12)
        self.category, self.status, self.order = QComboBox(), QComboBox(), QComboBox()
        self.category.addItem("全部项目分类", "")
        for category in dict.fromkeys((*CATEGORIES, *(r["category"] for r in rows))):
            self.category.addItem(category, category)
        for value, text in STATUSES: self.status.addItem(text, value)
        for value, text in SORTS: self.order.addItem(text, value)
        for widget, value in ((self.category, query.category), (self.status, query.status), (self.order, query.sort)):
            widget.setCurrentIndex(max(0, widget.findData(value)))
        for column, (title, widget) in enumerate((("分类", self.category), ("状态", self.status), ("分类内排序", self.order))):
            filters.addWidget(label(title, muted=True), 0, column)
            filters.addWidget(widget, 1, column)
        box.addLayout(filters)
        dates = QHBoxLayout()
        self.date_enabled = QCheckBox("限定采集日期")
        self.date_enabled.setChecked(bool(query.start or query.end))
        dates.addWidget(self.date_enabled)
        self.start, self.end = QDateEdit(), QDateEdit()
        for widget, value in ((self.start, query.start), (self.end, query.end)):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            widget.setDate(QDate.fromString(value, "yyyy-MM-dd") if value else QDate.currentDate())
            dates.addWidget(widget, 1)
            if widget is self.start: dates.addWidget(label("至"))
        dates.addWidget(button("重置筛选", self.reset, variant="ghost"))
        box.addLayout(dates)
        self.count = label("", muted=True)
        box.addWidget(self.count)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["采集日期", "项目分类", "点位", "有效样本", "审核 / 同步", "图片缓存"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda *_a: self._open())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        box.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("关闭", self.hide))
        actions.addStretch()
        actions.addWidget(button("应用到左侧列表", self._apply))
        self.open_button = button("打开所选任务", self._open, variant="primary")
        actions.addWidget(self.open_button)
        box.addLayout(actions)
        self.search.textChanged.connect(self.refresh)
        for widget in (self.category, self.status, self.order): widget.currentIndexChanged.connect(self.refresh)
        self.date_enabled.toggled.connect(self.refresh)
        for widget in (self.start, self.end): widget.dateChanged.connect(self.refresh)
        self.refresh()

    def query(self):
        return TaskQuery(self.search.text().strip(), self.category.currentData(),
            self.start.date().toString("yyyy-MM-dd") if self.date_enabled.isChecked() else "",
            self.end.date().toString("yyyy-MM-dd") if self.date_enabled.isChecked() else "",
            self.status.currentData(), self.order.currentData())

    def update_rows(self, rows):
        self.rows = rows
        self.refresh()

    def refresh(self, *_args):
        selected = self.selected_key()
        query = self.query()
        self.start.setEnabled(self.date_enabled.isChecked())
        self.end.setEnabled(self.date_enabled.isChecked())
        rows = filter_tasks(self.rows, query) if not query.start or query.start <= query.end else []
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row["business_date"], row["category"], row["name"], f"{row['valid']} / {row['review_target']}",
                      review_caption(row), cache_caption(row))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row["key"])
                item.setToolTip(value)
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, 48)
            if selected == row["key"]: self.table.selectRow(index)
        self.count.setText("开始日期不能晚于结束日期" if query.start and query.start > query.end else f"找到 {len(rows)} 项 / 共 {len(self.rows)} 项本地任务")
        self._selection_changed()

    def selected_key(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selection_changed(self):
        if hasattr(self, "open_button"):
            self.open_button.setEnabled(self.selected_key() is not None)

    def _apply(self):
        query = self.query()
        if query.start and query.start > query.end:
            return
        self.query_applied.emit(query)
        self.hide()

    def _open(self):
        key = self.selected_key()
        if key:
            self.hide()
            self.open_task.emit(key)

    def reset(self):
        self.search.clear()
        self.category.setCurrentIndex(0)
        self.status.setCurrentIndex(0)
        self.order.setCurrentIndex(0)
        self.date_enabled.setChecked(False)
