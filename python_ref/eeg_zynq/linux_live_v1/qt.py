"""Independent finite live window reusing the existing raw 16-channel canvas."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QBuffer, QIODevice
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem

from eeg_zynq.dsp import DspConfig
from eeg_zynq.electrodes import ELECTRODE_CHANNELS
from eeg_zynq.emd1 import DESIGN_RATE_HZ
from eeg_zynq.linux_dma import STATUS
from eeg_zynq.qt_scope import WaveformCanvas, configure_ui_font
from eeg_zynq.scope import OscilloscopeSession, WaveformMode


class LiveWindow(QMainWindow):
    def __init__(self, source, *, screenshot_path: Path | None = None, start_timer=True):
        configure_ui_font(QApplication.instance())
        super().__init__()
        self.source, self.session = source, None
        self.screenshot_path = screenshot_path
        self.paused = False
        self.event_closed = False
        self.render_count = 0
        self.pending_receipt = None
        self.setWindowTitle('P5B Linux → Qt · 有限窗口直达 · 非连续/非人体')
        self.resize(1420, 940)
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.source_label = QLabel(source.display_name)
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.rate_label = QLabel('244.140625 SPS为设计采样率；整批安全完成后通过串口到达，非逐帧实时采集。')
        layout.addWidget(self.rate_label)
        controls = QHBoxLayout()
        self.pause_button = QPushButton('暂停显示')
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button = QPushButton('停止本次会话')
        self.stop_button.clicked.connect(self.stop_session)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        self.state_label = QLabel('等待获准会话及真实源READY')
        controls.addWidget(self.state_label, 1)
        layout.addLayout(controls)
        self.canvas = WaveformCanvas(self)
        self.canvas.base_half_range_counts = 10000.0
        layout.addWidget(self.canvas, 1)
        self.table = QTableWidget(2, 16)
        self.table.setHorizontalHeaderLabels([ch.logical_channel for ch in ELECTRODE_CHANNELS])
        self.table.setVerticalHeaderLabels(['逻辑映射', '原始counts'])
        self.table.setMaximumHeight(112)
        for index, channel in enumerate(ELECTRODE_CHANNELS):
            self.table.setItem(0, index, QTableWidgetItem(f'{channel.electrode} / ADC{channel.adc_index}.{channel.adc_input_index}'))
        layout.addWidget(self.table)
        self.integrity_label = QLabel('校验：尚无本次数据；停止原因：无')
        self.integrity_label.setWordWrap(True)
        layout.addWidget(self.integrity_label)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        if start_timer:
            self.timer.start()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.session:
            self.session.pause() if self.paused else self.session.play()
        self.pause_button.setText('继续显示' if self.paused else '暂停显示')
        self.tick()

    def stop_session(self):
        if not self.event_closed:
            self.source.request_cancel('USER_STOP_IN_QT')
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.tick()

    def tick(self):
        try:
            state = self.source.state()
            if self.source.active and self.session is None:
                self.session = OscilloscopeSession(self.source, dsp_config=DspConfig(sample_rate_hz=DESIGN_RATE_HZ))
                self.session.set_visible_channel_count(16)
                self.session.set_waveform_mode(WaveformMode.RAW)
                self.session.set_time_window_seconds(2.0)
                if self.paused:
                    self.session.pause()
            if self.session and not state['failure'] and not self.source.cancel.is_set():
                self.session.advance(64)
                snapshot = self.session.snapshot()
                counters = dict(snapshot.error_counters)
                if any(value for key, value in counters.items() if key not in ('valid_records', 'records_received')):
                    raise ValueError('QT_STREAM_VALIDATION_ERROR:' + str(counters))
                self.canvas.set_snapshot(snapshot)
                for index, values in enumerate(snapshot.raw_waveforms):
                    if values:
                        self.table.setItem(1, index, QTableWidgetItem(str(int(values[-1]))))
                state = self.source.state()
                if self.source.finished and not self.source.display_ack.is_set():
                    self.pending_receipt = dict(source_id=self.source.source_id, last_sequence=snapshot.last_sequence,
                        channels=snapshot.visible_channel_count, frames=state['frames_consumed'],
                        raw=True, design_sample_rate_hz=DESIGN_RATE_HZ, qt_visible=self.isVisible(),
                        host_monotonic=time.monotonic(), counters=counters,
                        rendering='QT_WIDGET_RENDER_RECEIPT_NOT_PROOF_HUMAN_VIEWED_SCREEN')
            status = STATUS.unpack(self.source.post_status) if self.source.post_status else None
            marker = ('会话已关闭' if self.event_closed else '已停止' if state['failure'] or self.source.cancel.is_set() else
                      '有限窗口结束' if self.source.finished else '显示暂停' if self.paused else '等待/接收中')
            self.state_label.setText(f'{marker} · 帧 {state["frames_consumed"]}/256 · 队列 {state["queue"]}/256')
            if status:
                self.integrity_label.setText(f'CRC32={status[12]:08x} · valid={status[6]} · guards={status[10]} · '
                    f'CPU={status[7]} DMA={status[8]} · core_errors={status[18]} · discarded={status[21]} · '
                    f'停止原因：{state["failure"] or state["stop_reason"] or ("FINITE_END_NO_RESTART" if self.source.finished else "无")}')
            elif state['failure'] or state['stop_reason']:
                self.integrity_label.setText('停止原因：' + str(state['failure'] or state['stop_reason']))
            if self.pending_receipt and not self.source.display_ack.is_set():
                image_sha = None
                if self.screenshot_path is not None:
                    if self.screenshot_path.exists():
                        raise FileExistsError('DISPLAY_SCREENSHOT_ALREADY_EXISTS')
                    image = self.grab()  # current canvas, counters and source label
                    encoded = QBuffer()
                    encoded.open(QIODevice.OpenModeFlag.WriteOnly)
                    if not image.save(encoded, 'PNG'):
                        raise OSError('QT_RENDER_PNG_SAVE_FAILED')
                    payload = bytes(encoded.data())
                    with self.screenshot_path.open('xb') as stream:
                        stream.write(payload)
                        stream.flush()
                    image_sha = hashlib.sha256(payload).hexdigest()
                self.pending_receipt['screenshot_sha256'] = image_sha
                self.source.acknowledge_display(self.pending_receipt)
                self.render_count += 1
                self.pending_receipt = None
        except BaseException as exc:
            self.source.fail('QT_DISPLAY_FAILURE:' + repr(exc))
            self.source.request_cancel('QT_DISPLAY_FAILURE')
            self.state_label.setText('Qt错误，已请求停止')
            self.integrity_label.setText(str(exc))

    def mark_event_closed(self):
        self.event_closed = True
        self.stop_button.setEnabled(False)
        self.tick()

    def closeEvent(self, event):
        self.timer.stop()
        if not self.event_closed:
            self.source.request_cancel('WINDOW_CLOSED_BEFORE_AUTHORIZED_EVENT_CLOSED')
        super().closeEvent(event)
