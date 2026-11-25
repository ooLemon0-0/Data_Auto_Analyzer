from pathlib import Path

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QSpinBox, QVBoxLayout

from app.desktop.widgets import button, label


class CollectionDialog(QDialog):
    submitted = Signal(object, object, object, int, object)

    def __init__(self, parent, projects, selected, directory):
        super().__init__(parent)
        self.setWindowTitle("数据采集")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(490)
        self.directory = Path(directory)
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 22)
        box.setSpacing(14)
        box.addWidget(label("采集原图", heading=True))

        form = QFormLayout()
        self.project = QComboBox()
        for project in projects:
            self.project.addItem(project.name, project.id)
        if selected:
            self.project.setCurrentIndex(max(0, self.project.findData(selected.id)))
        form.addRow("点位", self.project)

        today = QDate.currentDate()
        self.start = QDateEdit(today.addDays(-1))
        self.end = QDateEdit(today)
        period = QHBoxLayout()
        for editor in (self.start, self.end):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setMaximumDate(today)
        period.addWidget(self.start, 1)
        period.addWidget(label("至"))
        period.addWidget(self.end, 1)
        form.addRow("采集日期", period)

        self.quantity = QSpinBox()
        self.quantity.setRange(1, 2**31 - 1)
        self.quantity.setValue(100)
        self.quantity.setSuffix(" 张")
        form.addRow("随机抽取", self.quantity)

        location = QHBoxLayout()
        self.destination = label(str(self.directory), muted=True)
        self.destination.setWordWrap(True)
        location.addWidget(self.destination, 1)
        location.addWidget(button("选择文件夹", self._select_folder, variant="ghost"))
        form.addRow("保存位置", location)
        box.addLayout(form)

        footer = QHBoxLayout()
        self.feedback = label("", muted=True)
        self.feedback.setWordWrap(True)
        footer.addWidget(self.feedback, 1)
        footer.addWidget(button("取消", self.reject))
        self.start_button = button("开始采集", self._submit, variant="primary", glyph="download", color="#FFFFFF")
        footer.addWidget(self.start_button)
        box.addLayout(footer)
        self.start.dateChanged.connect(self._validate)
        self.end.dateChanged.connect(self._validate)
        self._validate()

    def _select_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "选择原图保存位置", str(self.directory))
        if directory:
            self.directory = Path(directory)
            self.destination.setText(directory)

    def _validate(self):
        valid = self.start.date() <= self.end.date()
        self.start_button.setEnabled(valid and self.project.count() > 0)
        cursor = Qt.CursorShape.PointingHandCursor if self.start_button.isEnabled() else Qt.CursorShape.ForbiddenCursor
        self.start_button.setCursor(cursor)
        self.feedback.setText("" if valid else "结束日期不能早于开始日期")

    def _submit(self):
        if self.start_button.isEnabled():
            self.accept()
            self.submitted.emit(self.project.currentData(), self.start.date().toPython(), self.end.date().toPython(),
                                self.quantity.value(), self.directory)
