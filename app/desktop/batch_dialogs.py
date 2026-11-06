from __future__ import annotations

import time
from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QDateEdit, QDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QProgressBar, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from app.desktop.widgets import button, label


class ProjectSelectionDialog(QDialog):
    submitted = Signal(object, object, int)

    def __init__(self, parent, projects, *, selected=(), day=None, target=50):
        super().__init__(parent)
        self.setWindowTitle("选择要拉取的数据")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(800, min(760, QApplication.primaryScreen().availableGeometry().height() - 70))
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 22)
        box.setSpacing(14)
        box.addWidget(label("选择点位和日期", heading=True))
        instruction = label("选择采集日期、点位和每点有效样本数。仅下载抽中的图片。", muted=True)
        instruction.setWordWrap(True)
        box.addWidget(instruction)
        options = QHBoxLayout()
        options.setSpacing(14)
        options.addWidget(label("日期"))
        self.day = QDateEdit(day or QDate.currentDate().addDays(-1))
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.day.setCalendarPopup(True)
        self.day.setMaximumDate(QDate.currentDate())
        options.addWidget(self.day, 1)
        options.addWidget(label("每点有效样本"))
        self.target = QSpinBox()
        self.target.setRange(1, 5000)
        self.target.setValue(target)
        self.target.setSuffix(" 张")
        options.addWidget(self.target, 1)
        box.addLayout(options)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索点位名称，例如：2连铸、冷床、72")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet("background: white; border: 1px solid #E3E6EF; border-radius: 9px; padding: 11px;")
        box.addWidget(self.search)
        bulk = QHBoxLayout()
        self.count = label("", muted=True)
        bulk.addWidget(self.count, 1)
        bulk.addWidget(button("全选当前结果", self.select_visible, variant="ghost"))
        bulk.addWidget(button("清空选择", self.clear_selection, variant="ghost"))
        box.addLayout(bulk)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        panel.setObjectName("batchPanel")
        rows = QVBoxLayout(panel)
        rows.setContentsMargins(0, 0, 6, 0)
        rows.setSpacing(8)
        self.checks = {}
        self.rows = {}
        self.names = {p.id: p.name for p in projects}
        selected = {selected} if isinstance(selected, str) else set(selected or ())
        for project in projects:
            row = QFrame()
            row.setObjectName("card")
            line = QHBoxLayout(row)
            line.setContentsMargins(14, 12, 14, 12)
            check = QCheckBox(project.name)
            check.setChecked(project.id in selected)
            check.setToolTip(project.name)
            check.toggled.connect(self._update_count)
            self.checks[project.id], self.rows[project.id] = check, row
            line.addWidget(check, 1)
            rows.addWidget(row)
        rows.addStretch()
        scroll.setWidget(panel)
        box.addWidget(scroll, 1)
        actions = QHBoxLayout()
        self.volume = label("", muted=True)
        self.volume.setWordWrap(True)
        actions.addWidget(self.volume, 1)
        actions.addWidget(button("取消", self.reject))
        self.start_button = button("开始拉取", self._submit, variant="primary", glyph="download", color="#FFFFFF")
        actions.addWidget(self.start_button)
        box.addLayout(actions)
        self.search.textChanged.connect(self._filter)
        self.target.valueChanged.connect(self._update_count)
        self._update_count()

    def selected_ids(self):
        return [key for key, check in self.checks.items() if check.isChecked()]

    def _update_count(self, *_args):
        count = len(self.selected_ids())
        self.count.setText(f"已选 {count} 个点位 / 共 {len(self.checks)} 个")
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(count > 0)
            self.start_button.setText(f"开始拉取（{count} 个点位）")
            self.volume.setText(f"有效样本目标：{count} × {self.target.value()} = {count * self.target.value()} 张；已有缓存直接复用。")

    def _filter(self, text):
        for key, row in self.rows.items():
            row.setVisible(text.strip().lower() in self.names[key].lower())

    def select_visible(self):
        for key, check in self.checks.items():
            if not self.rows[key].isHidden():
                check.setChecked(True)

    def clear_selection(self):
        for check in self.checks.values():
            check.setChecked(False)

    def _submit(self):
        selected = self.selected_ids()
        if selected:
            day, target = self.day.date().toPython(), self.target.value()
            self.accept()
            self.submitted.emit(selected, day, target)


class DownloadProgressDialog(QDialog):
    open_review = Signal(str)
    retry = Signal(str)
    stop = Signal(str)
    stop_all = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("拉取任务与图片缓存")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(860, min(820, QApplication.primaryScreen().availableGeometry().height() - 70))
        self.jobs, self.rows, self.scope = {}, {}, None
        self.show_all = True
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 22)
        box.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(label("拉取任务", heading=True), 1)
        self.scope_button = button("查看全部任务", self._toggle_scope, variant="ghost")
        self.scope_button.hide()
        heading.addWidget(self.scope_button)
        heading.addWidget(button("收起窗口", self.hide, variant="ghost"))
        box.addLayout(heading)
        self.scope_label = label("全部本地任务", muted=True)
        box.addWidget(self.scope_label)
        instruction = label("读记录 → 选样本 → 缓存 / 生成复核图 → 可审核。记录条数与下载张数分开显示；收起窗口会继续运行，停止任务会保留已完成缓存。", muted=True)
        instruction.setWordWrap(True)
        box.addWidget(instruction)
        self.summary = label("尚无任务")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: #6658E8; background: #EEEBFC; padding: 12px; border-radius: 9px;")
        box.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        panel.setObjectName("batchPanel")
        self.listing = QVBoxLayout(panel)
        self.listing.setContentsMargins(0, 0, 6, 0)
        self.listing.setSpacing(12)
        self.listing.addStretch()
        scroll.setWidget(panel)
        box.addWidget(scroll, 1)
        actions = QHBoxLayout()
        self.stop_all_button = button("停止全部后台任务", self.stop_all.emit)
        self.stop_all_button.setStyleSheet("color: #C25C67; border-color: #EED2D6;")
        actions.addWidget(self.stop_all_button)
        actions.addStretch()
        actions.addWidget(button("收起，继续审核", self.hide, variant="primary"))
        box.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    def set_scope(self, keys, day=None, target=None):
        self.scope = set(keys)
        self.show_all = False
        self.batch_description = f"本批次：{day} · {len(self.scope)} 个点位 · 每点 {target} 张有效样本"
        self.scope_label.setText(self.batch_description)
        self.scope_button.show()
        self._refresh_summary()

    def _toggle_scope(self):
        self.show_all = not self.show_all
        self.scope_label.setText("全部任务（包括之前的本地任务）" if self.show_all else self.batch_description)
        self.scope_button.setText("只看本批次" if self.show_all else "查看全部任务")
        self._refresh_summary()

    @staticmethod
    def _stage(job):
        return {"queued": 0, "network": 0, "records": 1, "sampling": 2,
                "images": 3, "rendering": 3, "ready": 4}.get(job.get("stage"), 0)

    @staticmethod
    def _record_count(job):
        current, total = job.get("records_current"), job.get("records_total")
        if current is None:
            return "记录目录：尚未返回数量"
        return f"记录目录：{current:,} / {total:,} 条" if total is not None else f"记录目录：已读取 {current:,} 条"

    def update_job(self, job):
        key = job["key"]
        self.jobs[key] = job
        if key not in self.rows:
            frame = QFrame()
            frame.setObjectName("card")
            box = QVBoxLayout(frame)
            box.setContentsMargins(16, 14, 16, 14)
            box.setSpacing(9)
            top = QHBoxLayout()
            title = label(job["name"])
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            title.setStyleSheet("font-weight: 600;")
            top.addWidget(title, 1)
            badge = label()
            top.addWidget(badge)
            box.addLayout(top)
            meta = label("", muted=True)
            box.addWidget(meta)
            stages, stage_row = [], QHBoxLayout()
            for text in ("1 连接", "2 读记录", "3 选样本", "4 缓存 / 框图", "5 可审核"):
                mark = label(text)
                mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
                stages.append(mark)
                stage_row.addWidget(mark)
            box.addLayout(stage_row)
            counts = QHBoxLayout()
            records, images = label("", muted=True), label("")
            images.setStyleSheet("color: #6658E8; font-weight: 600;")
            counts.addWidget(records, 1)
            counts.addWidget(images)
            box.addLayout(counts)
            progress_caption = label("", muted=True)
            box.addWidget(progress_caption)
            progress = QProgressBar()
            progress.setTextVisible(False)
            box.addWidget(progress)
            message = label("", muted=True)
            message.setTextFormat(Qt.TextFormat.PlainText)
            message.setWordWrap(True)
            box.addWidget(message)
            bottom = QHBoxLayout()
            elapsed = label("", muted=True)
            bottom.addWidget(elapsed, 1)
            stop = button("停止", lambda: self.stop.emit(key))
            resume = button("继续拉取", lambda: self.retry.emit(key))
            review = button("开始审核", lambda: self.open_review.emit(key), variant="primary")
            for action in (stop, resume, review):
                bottom.addWidget(action)
            box.addLayout(bottom)
            self.listing.insertWidget(self.listing.count() - 1, frame)
            self.rows[key] = {"frame": frame, "badge": badge, "meta": meta, "stages": stages, "records": records,
                "images": images, "progress": progress, "message": message, "elapsed": elapsed,
                "progress_caption": progress_caption,
                "stop": stop, "resume": resume, "review": review}
        row = self.rows[key]
        status = job["status"]
        row["meta"].setText(f"{job['business_date']} · 有效样本目标 {job['target']} 张")
        badge_text = {"queued": "排队", "running": "进行中", "stopping": "正在停止", "cancelled": "已停止",
                      "ready": "可审核", "failed": "失败 · 可重试", "paused": "待补齐"}[status]
        color = "#29946A" if status == "ready" else "#C25C67" if status == "failed" else "#A58046" if status in {"cancelled", "stopping", "paused"} else "#6658E8"
        row["badge"].setText(badge_text)
        row["badge"].setStyleSheet(f"color: {color}; padding: 5px 9px; background: #F2F2F8; border-radius: 7px; font-size: 11px;")
        stage = 4 if status == "ready" else self._stage(job)
        for index, mark in enumerate(row["stages"]):
            color = "#29946A" if index < stage or status == "ready" else "#6658E8" if index == stage else "#A6ABBA"
            mark.setStyleSheet(f"font-size: 11px; color: {color}; padding: 5px 0;")
        row["records"].setText(self._record_count(job))
        current, total = job.get("images_ready", 0), job.get("images_total")
        row["images"].setText(f"复核图：{current} / {total} 张" if total is not None else
            f"本地已缓存 {current} 张 · 本次目标 {job['target']} 张" if current else f"复核图：目标 {job['target']} 张，尚未开始缓存")
        progress = row["progress"]
        if total:
            row["progress_caption"].setText("复核图缓存进度（包含端面框渲染）")
            progress.setRange(0, total)
            progress.setValue(current)
        elif job.get("records_total") and status not in {"cancelled", "failed", "paused"}:
            row["progress_caption"].setText("记录目录读取进度；尚未代表图片已缓存")
            progress.setRange(0, job["records_total"])
            progress.setValue(job.get("records_current") or 0)
        elif status == "running":
            row["progress_caption"].setText("等待连接或数据服务返回记录")
            progress.setRange(0, 0)
        else:
            row["progress_caption"].setText("任务已停止" if status in {"cancelled", "stopping"} else "等待任务开始" if status == "queued" else "")
            progress.setRange(0, 1)
            progress.setValue(1 if status == "ready" else 0)
        text = job.get("error") if status == "failed" else job.get("message", "")
        row["message"].setText((text or "")[:240])
        row["message"].setToolTip(text or "")
        row["stop"].setVisible(status in {"queued", "running", "stopping"})
        row["stop"].setEnabled(status != "stopping")
        row["stop"].setText("停止中…" if status == "stopping" else "停止")
        row["resume"].setVisible(status in {"cancelled", "paused", "failed"})
        row["resume"].setText("重试拉取" if status == "failed" else "继续拉取")
        row["review"].setVisible(status == "ready")
        self._refresh_summary()
        self._tick()

    def _refresh_summary(self):
        visible = []
        for key, job in self.jobs.items():
            show = self.show_all or self.scope is None or key in self.scope
            self.rows[key]["frame"].setVisible(show)
            if show:
                visible.append(job)
        counts = {status: sum(j["status"] == status for j in visible) for status in
                  ("ready", "queued", "running", "stopping", "cancelled", "failed", "paused")}
        self.summary.setText(f"{counts['ready']} / {len(visible)} 可审核   ·   {counts['running']} 正在准备   ·   {counts['queued']} 排队\n"
            f"{counts['stopping']} 停止中   ·   {counts['cancelled']} 已停止   ·   {counts['failed'] + counts['paused']} 待重试 / 补齐")
        active = sum(j["status"] in {"queued", "running"} for j in self.jobs.values())
        self.stop_all_button.setEnabled(active > 0)
        self.stop_all_button.setText(f"停止全部后台任务（{active}）" if active else "没有正在拉取的任务")

    def _tick(self):
        now = time.time()
        for key, job in self.jobs.items():
            started = job.get("started_at") or job.get("created_at") or now
            ended = job.get("finished_at") or now
            seconds = max(0, int(ended - started))
            duration = f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"
            text = ("排队 " if job["status"] == "queued" else "已用时 ") + duration
            if job["status"] in {"running", "stopping"}:
                idle = max(0, int(now - (job.get("updated_at") or now)))
                if idle >= 3:
                    text += f" · 最近更新 {idle} 秒前"
            self.rows[key]["elapsed"].setText(text)

    def reject(self):
        self.hide()

    def closeEvent(self, event):
        self.hide()
        event.accept()
