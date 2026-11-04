"""A passive diagnostic window. Closing this window never stops acquisition."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from eeg_zynq.continuous.protocol import CHANNELS, RATE
from eeg_zynq.continuous.qt import Canvas
from .model import AnnotationJournal, FC4_CHAIN


class DiagnosticWindow(QMainWindow):
    def __init__(self, observer, *, end_seconds=173.108, notes_folder=None):
        super().__init__()
        font_path = Path("C:/Windows/Fonts/msyh.ttc")
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                QApplication.instance().setFont(QFont(families[0], 9))
        self.observer, self.paused, self.failed = observer, False, False
        self.snapshot, self.displayed_end = None, None
        self.journal = AnnotationJournal(dict(observer.source, observe=observer.observe), folder=notes_folder)
        self.setWindowTitle("CONT1 诊断观察 · 无硬件控制权")
        self.resize(1500, 1000)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root)
        self.mode = QLabel(); self.mode.setStyleSheet("font-size:20px;font-weight:bold;color:#b45309")
        layout.addWidget(self.mode)
        warning = QLabel("只读文件观察；关闭此窗不会停止采集。停止采集请使用唯一主控Qt的停止按钮，并等待目标收尾及物理隔离检查点。")
        warning.setWordWrap(True); layout.addWidget(warning)
        row = QHBoxLayout()
        self.window = QComboBox(); self.window.addItems(["2秒", "5秒", "10秒", "30秒"]); self.window.setCurrentIndex(2)
        self.range = QComboBox(); self.range.addItems(["原码全量程 ±8388608", "局部 ±1000000", "局部 ±100000", "局部 ±10000"])
        self.offset = QCheckBox("处理显示：减窗口均值（不改原码/端点）")
        self.pause = QPushButton("暂停绘图（继续观察）"); self.pause.clicked.connect(self.toggle_pause)
        for widget in (self.window, self.range, self.offset, self.pause): row.addWidget(widget)
        layout.addLayout(row)
        seek = QHBoxLayout()
        seek.addWidget(QLabel("窗口结束：设计秒（非逐点实测时间）"))
        self.end = QDoubleSpinBox(); self.end.setDecimals(6); self.end.setRange(0, 400); self.end.setValue(end_seconds)
        self.end.setEnabled(not observer.observe); seek.addWidget(self.end)
        self.common = QPushButton("共同异常 ≈168秒")
        self.fc4 = QPushButton("FC4贴底 306～319秒")
        for button in (self.common, self.fc4): button.setEnabled(not observer.observe); seek.addWidget(button)
        self.common.clicked.connect(lambda: self.seek(173.108, 2))
        self.fc4.clicked.connect(lambda: self.seek(320, 3))
        layout.addLayout(seek)
        channels = QGridLayout(); self.checks = []
        for i, name in enumerate(CHANNELS):
            checkbox = QCheckBox(name); checkbox.setChecked(True)
            self.checks.append(checkbox); channels.addWidget(checkbox, i // 8, i % 8)
        layout.addLayout(channels)
        self.canvas = Canvas(); layout.addWidget(self.canvas, 1)
        self.table = QTableWidget(6, 16); self.table.setMaximumHeight(205)
        self.table.setMinimumHeight(190)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setHorizontalHeaderLabels(CHANNELS)
        self.table.setVerticalHeaderLabels(["芯片输入", "窗口min / max", "窗口均值counts", "最新原码", "负 / 正端点", "端点比例 / 最长段帧"])
        layout.addWidget(self.table)
        mapping = QLabel(FC4_CHAIN + "；FC3=CH5→1P→U1.IN1P。公共REF=FCz；BIAS=AFz（不是AGND）。")
        layout.addWidget(mapping)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.note = QLineEdit(); self.note.setPlaceholderText("动作/观察标签：回放时仅为事后注释；实时观察也不代表精确ADC动作时间")
        mark = QPushButton("记录主机时间标签"); mark.clicked.connect(self.annotate); self.note.returnPressed.connect(self.annotate)
        notes = QHBoxLayout(); notes.addWidget(self.note); notes.addWidget(mark); layout.addLayout(notes)
        self.feedback = QLabel(); layout.addWidget(self.feedback)
        output = QLabel(f"标签另存：{self.journal.folder}\n原始文件只读：{observer.path}")
        output.setWordWrap(True); layout.addWidget(output)
        self.timer = QTimer(self); self.timer.setInterval(250); self.timer.timeout.connect(self.tick)
        self.timer.start(); self.tick()

    def seek(self, end_seconds, index):
        self.window.setCurrentIndex(index); self.end.setValue(end_seconds); self.tick()

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause.setText("继续绘图" if self.paused else "暂停绘图（继续观察）")

    def tick(self):
        if self.failed:
            return
        try:
            seconds = (2, 5, 10, 30)[self.window.currentIndex()]
            state = self.observer.read_window(seconds=seconds, end_seconds=self.end.value())
            self.snapshot = state
            self.mode.setText(state["mode"] + " · " + state["state"])
            if not self.paused:
                self.canvas.rows = state["samples"]
                self.canvas.seconds = seconds
                self.canvas.selected = [i for i, check in enumerate(self.checks) if check.isChecked()]
                self.canvas.half_range = (8388608, 1000000, 100000, 10000)[self.range.currentIndex()]
                self.canvas.center = self.offset.isChecked(); self.canvas.update()
                self.displayed_end = state["end_sequence_exclusive"]
            for i, metric in enumerate(state["quality"]["channels"]):
                values = [f"U{i // 8 + 1}.IN{i % 8 + 1}P", f'{metric["minimum"]} / {metric["maximum"]}',
                          f'{metric["mean"]:.1f}', str(metric["latest"]),
                          f'{metric["negative"]} / {metric["positive"]}',
                          f'{metric["endpoint_ratio"]:.2%} / {metric["longest_endpoint_run"]}']
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip((FC4_CHAIN + "\n" if i == 1 else "") + value)
                    if j in (4, 5) and metric["negative"] + metric["positive"]:
                        from PySide6.QtGui import QColor
                        item.setBackground(QColor("#ffd5d5"))
                    self.table.setItem(j, i, item)
            age = state["fresh_write_age_seconds"]
            self.status.setText(
                f'只统计当前窗口 [{state["start_sequence"]}, {state["end_sequence_exclusive"]})；'
                f'全文件已保存 {state["total_recorded_frames"]}帧；同帧端点最多 '
                f'{state["quality"]["max_simultaneous_endpoints"]}路；待写完整帧字节={state["pending_partial_bytes"]}。\n'
                f'新落盘年龄={"尚未观察到" if age is None else f"{age:.2f}秒"}；'
                f'观察窗跳过显示帧={state["skipped_observer_display_frames"]}（不代表原始记录丢帧）；'
                f'绘图区夹幅={self.canvas.view_clipped}；目标收尾={state["target_state"]}。\n'
                '序号/状态头仅检查所读窗口；DMA/队列/全记录CRC看主控及已有证据。'
                '50Hz污染复用既有分析；差分码不能测接触阻抗或实际共模。')
        except Exception as exc:
            self.failed = True; self.timer.stop()
            self.mode.setText("OBSERVER_FAULT · 仅停止观察，不改变目标")
            self.status.setText(repr(exc))
            self.feedback.setText("保留当前画面；不自动恢复、更换文件或重连。")

    def annotate(self):
        if self.snapshot is None:
            return
        try:
            note = self.journal.add(self.snapshot, self.note.text(), displayed_end=self.displayed_end)
            self.feedback.setText(f'已记 {note["host_shanghai"]} / seq {note["latest_observed_sequence"]} / {note["relation"]}')
            self.note.clear()
        except (ValueError, OSError) as exc:
            self.feedback.setText(f"标签未写入：{exc}；采集控制不受此窗影响")

    def closeEvent(self, event):
        self.timer.stop()
        try:
            self.journal.close()
        except OSError as exc:
            self.feedback.setText(f"观察日志关闭失败：{exc}")
        event.accept()


def run(observer, *, end_seconds):
    app = QApplication.instance() or QApplication([])
    window = DiagnosticWindow(observer, end_seconds=end_seconds)
    window.show()
    return app.exec()
