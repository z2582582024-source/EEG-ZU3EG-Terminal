"""Raw-count rolling scope. View clipping/offsets never alter the saved stream."""
from __future__ import annotations
import time

from PySide6.QtCore import QPointF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QGridLayout,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from .protocol import CHANNELS, RATE
from .stream import BUILD, NetworkSession, ReplaySession, SimulatedPeer
import uuid


class Canvas(QWidget):
    def __init__(self):
        super().__init__()
        self.rows, self.selected = [], list(range(16))
        self.seconds, self.half_range, self.center = 5., 8388608., False
        self.view_clipped = 0
        self.setMinimumHeight(420)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d1726"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.selected:
            p.setPen(QColor("white")); p.drawText(20, 30, "Select channels to display"); return
        rows = self.rows[-int(self.seconds*RATE):]
        lane = self.height()/len(self.selected)
        left, width = 95, max(1, self.width()-110)
        self.view_clipped = 0
        for k, channel in enumerate(self.selected):
            cy = (k+.5)*lane
            values = [v[channel] for v in rows]
            offset = sum(values)/len(values) if values and self.center else 0.
            p.setPen(QColor("#536579"))
            p.drawLine(left, int(cy), self.width()-10, int(cy))
            p.setPen(QColor("#d8e8f6"))
            p.drawText(6, int(cy), f"{channel+1} {CHANNELS[channel]}")
            points = QPolygonF()
            scale = max(1., self.seconds*RATE-1)
            for j, value in enumerate(values):
                norm = (value-offset)/self.half_range
                self.view_clipped += abs(norm) > 1
                x = left+width*(j+max(0,self.seconds*RATE-len(values)))/scale
                points.append(QPointF(x, cy-max(-1.,min(1.,norm))*lane*.42))
            p.setClipRect(left, int(k*lane), width, int(lane))
            p.setPen(QPen(QColor("#47dfb3"), 1.1))
            p.drawPolyline(points)
            p.setClipping(False)
        p.end()


class Window(QMainWindow):
    def __init__(self, session=None, *, peer=None):
        super().__init__()
        from pathlib import Path
        font = Path('C:/Windows/Fonts/msyh.ttc')
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
            QApplication.instance().setFont(QFont('Microsoft YaHei',9))
        self.session, self.peer, self.paused, self.closing = session, peer, False, False
        self.setWindowTitle("P5B NET1 · 连续采集/网络/Qt独立候选 · 硬件待验证")
        self.resize(1460, 1000)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root)
        self.mode_label = QLabel("DISCONNECTED · 未取得新鲜硬件流")
        self.mode_label.setStyleSheet("font-size:20px;font-weight:bold;color:#b45309")
        layout.addWidget(self.mode_label)
        controls = QHBoxLayout()
        self.sim = QPushButton("本地模拟检查 SIMULATED")
        self.replay = QPushButton("查看24保存数据 FILE_REPLAY")
        self.start_button, self.stop_button = QPushButton("开始采集（一次START）"), QPushButton("停止采集并等待收尾")
        self.pause_button = QPushButton("暂停显示（继续记录）")
        for button in (self.sim, self.replay, self.start_button, self.stop_button, self.pause_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.sim.clicked.connect(self.simulate)
        self.replay.clicked.connect(self.open_event24)
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button.clicked.connect(self.stop_capture)
        self.pause_button.clicked.connect(self.pause)
        view = QHBoxLayout()
        self.range = QComboBox(); self.range.addItems(["全量程 ±8388608 counts", "局部 ±1000000 counts", "局部 ±100000 counts", "局部 ±10000 counts"])
        self.window = QComboBox(); self.window.addItems(["2秒", "5秒", "10秒", "30秒"]); self.window.setCurrentIndex(1)
        self.offset = QCheckBox("窗口均值偏移（仅处理显示；原始记录不变）")
        for widget in (self.range, self.window, self.offset): view.addWidget(widget)
        view.addStretch(); layout.addLayout(view)
        channels = QGridLayout(); self.checks = []
        for i, label in enumerate(CHANNELS):
            checkbox = QCheckBox(f"{i+1} {label}"); checkbox.setChecked(True)
            self.checks.append(checkbox); channels.addWidget(checkbox, i//8, i%8)
        layout.addLayout(channels)
        self.canvas = Canvas(); layout.addWidget(self.canvas, 1)
        self.table = QTableWidget(4,16); self.table.setMaximumHeight(158)
        self.table.setHorizontalHeaderLabels(CHANNELS)
        self.table.setVerticalHeaderLabels(["ADC输入", "最新原码", "全记录均值counts", "原码饱和 数量/比例"])
        for i in range(16): self.table.setItem(0,i,QTableWidgetItem(f"ADC{i//8+1}.{i%8+1}"))
        layout.addWidget(self.table)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.record = QLabel(); self.record.setWordWrap(True); layout.addWidget(self.record)
        self.notice = QLabel("设计244.140625 SPS；采样时间按设计间隔推算，目标时间戳为DMA完成时间。原码饱和与绘图区夹幅分别计数。")
        layout.addWidget(self.notice)
        self.timer = QTimer(self); self.timer.setInterval(50); self.timer.timeout.connect(self.tick); self.timer.start()
        if self.session and isinstance(self.session, NetworkSession): self.session.connect()
        self.tick()

    def attach(self, session, peer=None):
        if self.session:
            return  # One source/session per window. Open a fresh local window to change sources.
        self.session, self.peer = session, peer
        if isinstance(session, NetworkSession): session.connect()

    def simulate(self):
        tag = uuid.uuid4().bytes
        peer = SimulatedPeer(tag,seconds=0)
        self.attach(NetworkSession("127.0.0.1", peer.port, tag, mode=2,seconds=0), peer)

    def open_event24(self):
        self.attach(ReplaySession(BUILD.parent / "p5b_linux_qt_external_v1/cap_adaptation_01/events/axis_dma_ddr_finite_cap_20261018_24/raw_dma.bin"))

    def start_capture(self):
        try:
            if isinstance(self.session, NetworkSession): self.session.start()
        except RuntimeError as exc:
            self.status.setText(str(exc))

    def stop_capture(self):
        if self.session: self.session.stop()

    def pause(self):
        self.paused = not self.paused
        self.pause_button.setText("继续显示" if self.paused else "暂停显示（继续记录）")

    def tick(self):
        self.sim.setEnabled(self.session is None); self.replay.setEnabled(self.session is None)
        if not self.session:
            self.start_button.setEnabled(False); self.stop_button.setEnabled(False)
            self.status.setText("本地主机入口已启动；可选模拟或保存数据。LIVE_STREAM须使用匹配的新范围批准入口。")
            return
        self.session.ui_tick = time.monotonic()
        if hasattr(self.session,'controller_stage'):
            self.notice.setText('控制阶段：'+self.session.controller_stage+' · '+self.session.operator_notice)
        stopfile = self.session.folder / "stop.request" if isinstance(self.session, NetworkSession) else None
        if stopfile and stopfile.exists() and not self.session.done.is_set(): self.session.stop("USER_STOP_COMMAND")
        if isinstance(self.session, ReplaySession) and not self.paused and not self.session.done.is_set():
            self.session.advance()
        state = self.session.snapshot()
        self.mode_label.setText(f'{state["mode"]} · {state["state"]}')
        self.start_button.setEnabled(isinstance(self.session, NetworkSession) and self.session.state == "READY"
            and self.session.input_ready.is_set() and not self.session.start_request.is_set())
        self.stop_button.setEnabled(not self.session.done.is_set())
        if not self.paused:
            self.canvas.rows = state["samples"]
            self.canvas.selected = [i for i, c in enumerate(self.checks) if c.isChecked()]
            self.canvas.seconds = [2,5,10,30][self.window.currentIndex()]
            self.canvas.half_range = [8388608,1000000,100000,10000][self.range.currentIndex()]
            self.canvas.center = self.offset.isChecked(); self.canvas.update()
        if state["samples"]:
            for i, value in enumerate(state["samples"][-1]):
                n = state["frames"]; sat = state["saturation"][i]
                for row, text in [(1,str(value)), (2,f'{state["sums"][i]/n:.1f}'), (3,f'{sat}/{sat/n:.2%}')]:
                    self.table.setItem(row,i,QTableWidgetItem(text))
        status = state["target_status"]
        self.status.setText(f'帧={state["frames"]} 块={state["chunks"]} 序列间隙={state["seq_gaps"]} 数据年龄秒={state["fresh_seconds"]} '
            f'绘图区夹幅点={self.canvas.view_clipped} · 显示{"暂停，采集和记录仍运行" if self.paused else "滚动"}\n'
            f'DMA错误={status.get("dma_errors","未观察")} core错误={status.get("core_errors","未观察")} '
            f'队列高水位={status.get("queue_high_water","未观察")} discarded={status.get("discarded","未观察")} '
            f'停止原因={state["stop_reason"]} 故障={state["failure"]}')
        if isinstance(self.session, ReplaySession):
            self.record.setText(f'只读回放来源：{self.session.path} · 未新录制；不循环播放')
        else:
            self.record.setText(f'原始记录：{state["record_folder"]} · '
                f'{"已封存/检查manifest" if self.session.done.is_set() else "收录启用，显示缓存上限8192帧，落盘总上限32MiB"}')
        controller_done=getattr(self.session,'controller_done',self.session.done)
        if self.closing and self.session.done.is_set() and controller_done.is_set(): self.close()

    def closeEvent(self, event):
        if self.session and (not self.session.done.is_set() or
                not getattr(self.session,'controller_done',self.session.done).is_set()):
            self.closing = True; self.session.stop("QT_EXIT"); event.ignore()
            self.status.setText("窗口关闭已请求停止，等待目标收尾；失联不能视为GPIO已撤销。")
        else:
            self.timer.stop(); event.accept()


def run(session=None, peer=None):
    app = QApplication.instance() or QApplication([])
    window = Window(session, peer=peer); window.show()
    return app.exec()
