"""Finite acquisition-time chunk window. Rendering does not pace or fabricate data."""
from __future__ import annotations
import hashlib,time
from pathlib import Path
from PySide6.QtCore import QTimer,Qt,QBuffer,QIODevice
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QTableWidget,QTableWidgetItem
from eeg_zynq.dsp import DspConfig
from eeg_zynq.electrodes import ELECTRODE_CHANNELS
from eeg_zynq.emd1 import DESIGN_RATE_HZ
from eeg_zynq.qt_scope import WaveformCanvas,configure_ui_font
from eeg_zynq.scope import OscilloscopeSession,WaveformMode
from .protocol import STATUS


class ChunkWindow(QMainWindow):
    def __init__(self,source,*,folder=None,start_timer=True):
        configure_ui_font(QApplication.instance());super().__init__()
        self.source=source;self.folder=Path(folder) if folder is not None else None
        self.session=None;self.paused=False;self.event_closed=False
        self.setWindowTitle('P5B 外部输入 Linux → Qt · 8×32分块/单ARM/总256帧 · 非人体')
        self.resize(1420,940)
        root=QWidget(self);self.setCentralWidget(root);layout=QVBoxLayout(root)
        self.source_label=QLabel(source.display_name);layout.addWidget(self.source_label)
        self.rate_label=QLabel('设计244.140625 SPS；分块到达即显示原码，未取得最终收尾前为暂定数据。')
        layout.addWidget(self.rate_label)
        controls=QHBoxLayout();self.pause_button=QPushButton('暂停显示');self.stop_button=QPushButton('停止本次会话')
        self.pause_button.clicked.connect(self.toggle_pause);self.stop_button.clicked.connect(self.stop_session)
        controls.addWidget(self.pause_button);controls.addWidget(self.stop_button)
        self.state_label=QLabel('等待获准会话/真实READY');controls.addWidget(self.state_label,1);layout.addLayout(controls)
        self.canvas=WaveformCanvas(self);self.canvas.base_half_range_counts=10000.;layout.addWidget(self.canvas,1)
        self.table=QTableWidget(2,16);self.table.setMaximumHeight(112)
        self.table.setHorizontalHeaderLabels([c.logical_channel for c in ELECTRODE_CHANNELS]);self.table.setVerticalHeaderLabels(['逻辑映射','原始counts'])
        for index,ch in enumerate(ELECTRODE_CHANNELS):self.table.setItem(0,index,QTableWidgetItem(f'{ch.electrode} / ADC{ch.adc_index}.{ch.adc_input_index}'))
        layout.addWidget(self.table);self.integrity_label=QLabel('外部输入候选；配置标志不等于外部信号验证通过。完整性尚待本次终态；无平滑/插值/补帧/静默丢帧。');self.integrity_label.setWordWrap(True);layout.addWidget(self.integrity_label)
        self.timer=QTimer(self);self.timer.setInterval(20);self.timer.setTimerType(Qt.TimerType.PreciseTimer);self.timer.timeout.connect(self.tick)
        if start_timer:self.timer.start()

    def toggle_pause(self):
        self.paused=not self.paused
        if self.session:self.session.pause() if self.paused else self.session.play()
        self.pause_button.setText('继续显示' if self.paused else '暂停显示');self.tick()

    def stop_session(self):
        if not self.event_closed:self.source.request_cancel('USER_STOP_IN_QT')
        self.pause_button.setEnabled(False);self.stop_button.setEnabled(False);self.tick()

    def render_receipt(self,snapshot,*,prefix):
        # grab() renders the current canvas synchronously; this receipt does not
        # claim a person viewed it. Driver causal ACK separately proves overlap.
        image=self.grab();encoded=QBuffer();encoded.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(encoded,'PNG'):raise OSError('QT_PNG_RENDER_FAILURE')
        payload=bytes(encoded.data());sha=hashlib.sha256(payload).hexdigest()
        if self.folder is not None:
            path=self.folder/('qt_prefix.png' if prefix else 'qt_final.png')
            with path.open('xb') as f:
                if f.write(payload)!=len(payload):raise OSError('SHORT_SCREENSHOT_WRITE')
                f.flush()
        return dict(source_id=self.source.source_id,frames=self.source.consumed,channels=16,last_sequence=snapshot.last_sequence,
            raw=True,rendered=True,qt_visible=self.isVisible(),screenshot_sha256=sha,host_monotonic=time.monotonic(),
            verification='QT_WIDGET_RENDER_NOT_HUMAN_VIEW_CONFIRMATION',provisional=prefix)

    def tick(self):
        try:
            state=self.source.state();snapshot=None
            if self.source.active and self.session is None:
                self.session=OscilloscopeSession(self.source,dsp_config=DspConfig(sample_rate_hz=DESIGN_RATE_HZ))
                self.session.set_visible_channel_count(16);self.session.set_waveform_mode(WaveformMode.RAW);self.session.set_time_window_seconds(2.)
                if self.paused:self.session.pause()
            if self.session and not state['failure'] and not self.source.cancel.is_set():
                self.session.advance(64);snapshot=self.session.snapshot();counters=dict(snapshot.error_counters)
                if any(v for k,v in counters.items() if k not in ('valid_records','records_received')):raise ValueError('QT_FRAME_VALIDATION:'+str(counters))
                self.canvas.set_snapshot(snapshot)
                for index,values in enumerate(snapshot.raw_waveforms):
                    if values:self.table.setItem(1,index,QTableWidgetItem(str(int(values[-1]))))
                state=self.source.state()
            marker='已关闭' if self.event_closed else '已停止' if state['failure'] or self.source.cancel.is_set() else '有限结束/已验证' if self.source.finished else '显示暂停（采集未暂停）' if self.paused else '采集中/分块暂定'
            self.state_label.setText(f'{marker} · 块{state["chunks_received"]}/8 · 帧{state["frames_consumed"]}/256 · 队列{state["queue"]}/{state["capacity"]}')
            if self.source.post_status:
                st=STATUS.unpack(self.source.post_status)
                self.integrity_label.setText(f'CRC32={st[12]:08x} guards={st[10]} core_errors={st[18]} discarded={st[21]} CPU/DMA masks={st[38]:02x}/{st[39]:02x} · 首屏回执时已完成块={st[44]}，paired={st[45]}，采集活跃={st[46]}')
            if state['failure'] or state['stop_reason']:self.integrity_label.setText('停止原因：'+str(state['failure'] or state['stop_reason']))
            if snapshot and self.source.consumed>=32 and not self.source.prefix_ack.is_set():
                self.source.acknowledge_prefix(self.render_receipt(snapshot,prefix=True))
            if snapshot and self.source.finished and not self.source.display_ack.is_set():
                self.source.acknowledge_display(self.render_receipt(snapshot,prefix=False))
        except BaseException as exc:
            self.source.fail('QT_CHUNK_DISPLAY_FAILURE:'+repr(exc));self.state_label.setText('Qt错误，已请求STOP');self.integrity_label.setText(str(exc))

    def mark_event_closed(self):
        self.event_closed=True;self.stop_button.setEnabled(False);self.tick()

    def closeEvent(self,event):
        self.timer.stop()
        if not self.event_closed:self.source.request_cancel('WINDOW_CLOSED_BEFORE_EVENT_CLOSED')
        super().closeEvent(event)
