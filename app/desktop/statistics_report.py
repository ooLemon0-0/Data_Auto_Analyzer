"""Local daily statistics and clipboard formats for WPS/Excel tables."""
from __future__ import annotations

from datetime import date
from html import escape

from app import db


HEADERS = ("日期", "点位", "总计", "识别正确", "识别错误", "遮挡", "连字",
           "其他（空拍，角度错误，破损严重，反光严重）", "识别准确率")
COUNTING_NOTE = ("总计包含所有已审核图片；遮挡、连字留空。模糊与其他合并列入“其他”，"
                 "不计入准确率并自动补抽。准确率 = 识别正确 ÷（识别正确 + 识别错误）。")


def point_name(project) -> str:
    # These are device numbers, not the different camera IDs in parentheses.
    points = {"binxin_billet": "73", "binxin_billet_72": "72", "binxin_billet_74": "74", "binxin_billet_88": "88"}
    return points.get(project.id, project.name)


def daily_statistics(day: date, projects) -> tuple[list[list[str]], int]:
    """Only complete sessions are exported; pending samples never inflate totals."""
    by_id = {p.id: p for p in projects}
    with db.connect() as conn:
        rows = conn.execute("""SELECT s.project_id, s.target_size,
            COALESCE(SUM(q.decision='correct'),0) AS correct,
            COALESCE(SUM(q.decision='incorrect'),0) AS incorrect,
            COALESCE(SUM(q.decision IN ('invalid','other')),0) AS other
            FROM review_sessions s LEFT JOIN review_queue q ON q.session_id=s.id
            WHERE s.business_date=? GROUP BY s.id ORDER BY s.project_id""", (day.isoformat(),)).fetchall()
    result, incomplete = [], 0
    for row in rows:
        valid = row['correct'] + row['incorrect']
        if valid < row['target_size']:
            incomplete += 1
            continue
        project = by_id.get(row['project_id'])
        point = point_name(project) if project else row['project_id']
        result.append([f"{day.year}/{day.month}/{day.day}", point, str(valid + row['other']),
            str(row['correct']), str(row['incorrect']), '', '',
            str(row['other']), f"{row['correct'] / valid * 100:.2f}%" if valid else "—"])
    return sorted(result, key=lambda row: (not row[1].isdigit(), int(row[1]) if row[1].isdigit() else row[1])), incomplete


def clipboard_formats(rows, *, include_headers=True) -> tuple[str, str]:
    table = ([list(HEADERS)] if include_headers else []) + [list(row) for row in rows]
    text = '\r\n'.join('\t'.join(str(cell).replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
                                 for cell in row) for row in table)
    html_rows = []
    for index, row in enumerate(table):
        tag = 'th' if include_headers and index == 0 else 'td'
        html_rows.append('<tr>' + ''.join(f'<{tag} style="border:1px solid #CBD0DC;padding:8px;'
            f'white-space:nowrap">{escape(str(cell))}</{tag}>' for cell in row) + '</tr>')
    html = ('<html><head><meta charset="utf-8"></head><body>'
            '<table border="1" cellspacing="0" cellpadding="8" style="border-collapse:collapse">'
            + ''.join(html_rows) + '</table></body></html>')
    return text, html


def copy_statistics(rows, *, include_headers=True):
    from PySide6.QtCore import QMimeData
    from PySide6.QtWidgets import QApplication
    plain, html = clipboard_formats(rows, include_headers=include_headers)
    mime = QMimeData()
    mime.setText(plain)
    mime.setHtml(html)
    QApplication.clipboard().setMimeData(mime)


from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDateEdit, QDialog,
    QHBoxLayout, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.desktop.widgets import button, label


class StatisticsDialog(QDialog):
    def __init__(self, day: date, projects, parent=None):
        super().__init__(parent)
        self.projects, self.rows = list(projects), []
        self.setWindowTitle('每日统计表')
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1280, available.width() - 40), min(620, available.height() - 60))
        self.setMinimumSize(min(880, available.width() - 40), min(460, available.height() - 60))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading.addWidget(label('每日统计表', heading=True), 1)
        self.day = QDateEdit(QDate(day.year, day.month, day.day))
        self.day.setDisplayFormat('yyyy/M/d')
        self.day.setCalendarPopup(True)
        heading.addWidget(self.day)
        heading.addWidget(button('刷新', self.refresh))
        layout.addLayout(heading)
        self.caption = label('', muted=True)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.table = QTableWidget(0, len(HEADERS))
        display_headers = list(HEADERS)
        display_headers[7] = '其他\n（空拍，角度错误，破损严重，反光严重）'
        self.table.setHorizontalHeaderLabels(display_headers)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setFixedHeight(64)
        self.table.setStyleSheet('QTableWidget { background:white; alternate-background-color:#F8F9FC; '
            'border:1px solid #E3E6EF; border-radius:10px; gridline-color:#E8EBF1; } '
            'QHeaderView::section { background:#F0F1F8; color:#555D73; border:0; '
            'border-right:1px solid #E3E6EF; padding:8px 4px; font-size:12px; font-weight:600; }')
        for i, width in enumerate((112, 190, 62, 80, 80, 62, 62, 306, 104)):
            self.table.setColumnWidth(i, width)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        note = label(COUNTING_NOTE, muted=True)
        note.setWordWrap(True)
        layout.addWidget(note)
        footer = QHBoxLayout()
        self.include_headers = QCheckBox('复制时包含表头')
        self.include_headers.setChecked(True)
        footer.addWidget(self.include_headers)
        footer.addStretch(1)
        self.feedback = label('', muted=True)
        footer.addWidget(self.feedback)
        footer.addWidget(button('关闭', self.close))
        self.copy_button = button('复制整张表格', self.copy, variant='primary')
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)
        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self.copy_shortcut.activated.connect(self.copy)
        self.day.dateChanged.connect(self.refresh)
        self.refresh()

    def set_day(self, day: date):
        self.day.setDate(QDate(day.year, day.month, day.day))
        self.refresh()

    def refresh(self, *_args):
        self.rows, incomplete = daily_statistics(self.day.date().toPython(), self.projects)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignLeft if c == 1 else Qt.AlignmentFlag.AlignHCenter))
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()
        for r in range(len(self.rows)):
            self.table.setRowHeight(r, max(48, self.table.rowHeight(r)))
        self.caption.setText(f'该日期已完成 {len(self.rows)} 个点位，未完成 {incomplete} 个点位。'
            + ('复制后在 WPS 或轻推表格中按 Ctrl+V 粘贴。' if self.rows else '完成审核后，对应记录会显示在这里。'))
        self.copy_button.setEnabled(bool(self.rows))
        self.feedback.clear()

    def copy(self):
        if self.rows:
            copy_statistics(self.rows, include_headers=self.include_headers.isChecked())
            self.feedback.setText(f'已复制 {len(self.rows)} 行，可直接粘贴')
