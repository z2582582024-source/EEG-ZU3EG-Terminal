"""PySide6 oscilloscope with explicit simulation and validated file provenance."""

from __future__ import annotations

import sys
import os
import argparse
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QFont,
    QMouseEvent,
    QFontDatabase,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .datasource import RealtimeFrameSource
from .dsp import DspConfig
from .electrodes import ELECTRODE_CHANNELS
from .frame import SourceType
from .linux_dma import LinuxDmaError
from .leadoff import (
    ContactState,
    LeadOffPhase,
    LeadOffProtocolError,
    LeadOffReport,
    LeadOffStateMachine,
    SimulatedLeadOffTransport,
    SimulationScenario,
)
from .recording import (
    ContinuousRecordWriter,
    FileReplaySource,
    RecordingError,
    RecordingInfo,
    RecordingWriteError,
    ReplayLoadError,
)
from .processed_packet_v1 import ProcessedDspReplaySource
from .scope import (
    AMPLITUDE_SCALES,
    TIME_WINDOWS_SECONDS,
    OscilloscopeSession,
    ScopeSnapshot,
    WaveformMode,
)
from .simulator import RealtimeSimulatedSource


BACKGROUND = QColor("#0b1020")
PANEL = QColor("#111a2e")
GRID = QColor("#29344f")
TEXT = QColor("#dbeafe")
MUTED = QColor("#94a3b8")
ACCENT = QColor("#38bdf8")
WARNING = QColor("#f59e0b")
COUNT_HALF_RANGES = (600_000.0, 10_000.0, 5_000.0, 20_000.0)
TRACE_COLORS = tuple(
    QColor(color)
    for color in (
        "#38bdf8",
        "#a78bfa",
        "#34d399",
        "#fb7185",
        "#fbbf24",
        "#22d3ee",
        "#c084fc",
        "#4ade80",
        "#60a5fa",
        "#f472b6",
        "#2dd4bf",
        "#f97316",
        "#818cf8",
        "#e879f9",
        "#84cc16",
        "#f43f5e",
    )
)

LEAD_OFF_SCENARIOS = (
    ("正常连接", SimulationScenario.NORMAL),
    ("接触不良", SimulationScenario.POOR_CONTACT),
    ("开路", SimulationScenario.OPEN),
    ("异常低阻（不可区分）", SimulationScenario.ABNORMALLY_LOW),
    ("阈值边界", SimulationScenario.THRESHOLD_BOUNDARY),
    ("噪声抖动", SimulationScenario.NOISY_JITTER),
    ("ADS1 状态缺失", SimulationScenario.ADS1_STATUS_MISSING),
    ("ADS2 状态缺失", SimulationScenario.ADS2_STATUS_MISSING),
    ("公共 REF 异常", SimulationScenario.COMMON_REFERENCE_OFF),
    ("BIAS 未知/不支持", SimulationScenario.BIAS_UNKNOWN),
    ("等待超时", SimulationScenario.TIMEOUT),
    ("寄存器错误", SimulationScenario.REGISTER_ERROR),
    ("恢复失败", SimulationScenario.RESTORE_FAILURE),
)

LEAD_OFF_PHASE_TEXT = {
    LeadOffPhase.IDLE: "空闲",
    LeadOffPhase.SAVE_EEG_CONFIG: "保存 EEG 配置",
    LeadOffPhase.STOP_CONTINUOUS: "停止连续读取",
    LeadOffPhase.CONFIGURE_LEAD_OFF: "配置 lead-off",
    LeadOffPhase.WAIT_STABLE: "等待稳定",
    LeadOffPhase.ACQUIRE_STATUS: "获取状态",
    LeadOffPhase.CLASSIFY_CHANNELS: "通道判定",
    LeadOffPhase.DISABLE_LEAD_OFF: "关闭 lead-off",
    LeadOffPhase.RESTORE_EEG_CONFIG: "恢复 EEG 配置",
    LeadOffPhase.RESUME_CONTINUOUS: "恢复连续读取",
    LeadOffPhase.COMPLETE: "完成",
    LeadOffPhase.FAILED: "失败（已尝试恢复）",
    LeadOffPhase.CANCELLED: "已取消（已尝试恢复）",
}

CONTACT_STATE_TEXT = {
    ContactState.NOT_TESTED: "未检测",
    ContactState.CONNECTED: "连接",
    ContactState.ELECTRODE_OFF: "脱落/开路",
    ContactState.UNSTABLE: "接触不稳",
    ContactState.UNKNOWN: "未知",
    ContactState.UNSUPPORTED: "暂不支持",
}


def configure_ui_font(app: QApplication) -> str:
    """Select or load a font that can render the Chinese desktop labels."""

    preferred_families = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "DengXian",
        "SimHei",
    )
    selected = next(
        (family for family in preferred_families if QFontDatabase.hasFamily(family)), None
    )
    if selected is None:
        windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
        font_candidates = (
            windows_root / "Fonts" / "NotoSansSC-VF.ttf",
            windows_root / "Fonts" / "msyh.ttc",
            windows_root / "Fonts" / "Deng.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        )
        for path in font_candidates:
            if not path.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id < 0:
                continue
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                selected = families[0]
                break
    selected = selected or app.font().family()
    font = QFont(selected, 9)
    app.setFont(font)
    return selected


class WaveformCanvas(QWidget):
    """Stacked logical-channel traces drawn with Qt only."""

    BASE_HALF_RANGE_COUNTS = 600_000.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("waveformCanvas")
        self.setMinimumSize(720, 460)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._snapshot: ScopeSnapshot | None = None
        self.base_half_range_counts = self.BASE_HALF_RANGE_COUNTS
        self.selected_channel_index: int | None = None
        self._contact_labels = ["未测"] * len(ELECTRODE_CHANNELS)
        self._contact_colors = [MUTED] * len(ELECTRODE_CHANNELS)
        self._contact_scan_handler = None
        self._contact_scan_available = False
        self.setToolTip("模拟源下点击通道名可启动全通道接触状态扫描；真实 lead-off 硬件尚未接入")

    def set_contact_report(self, report: LeadOffReport | None) -> None:
        if report is None:
            self._contact_labels = ["未测"] * len(ELECTRODE_CHANNELS)
            self._contact_colors = [MUTED] * len(ELECTRODE_CHANNELS)
        else:
            labels = {
                ContactState.NOT_TESTED: "未测",
                ContactState.CONNECTED: "SIM连接",
                ContactState.ELECTRODE_OFF: "SIM脱落",
                ContactState.UNSTABLE: "SIM不稳",
                ContactState.UNKNOWN: "SIM未知",
                ContactState.UNSUPPORTED: "暂不支持",
            }
            colors = {
                ContactState.CONNECTED: QColor("#34d399"),
                ContactState.ELECTRODE_OFF: QColor("#f87171"),
                ContactState.UNSTABLE: QColor("#f59e0b"),
            }
            self._contact_labels = [labels[row.state] for row in report.channels]
            self._contact_colors = [colors.get(row.state, MUTED) for row in report.channels]
        self.update()

    def set_contact_scan_handler(self, handler, *, available: bool) -> None:
        self._contact_scan_handler = handler
        self._contact_scan_available = bool(available)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        snapshot = self._snapshot
        if (self._contact_scan_available and self._contact_scan_handler is not None and
                snapshot is not None and event.button() == Qt.MouseButton.LeftButton):
            top, bottom, left = 28.0, 26.0, 92.0
            plot_height = max(1.0, self.height() - top - bottom)
            indices = ((self.selected_channel_index,) if self.selected_channel_index is not None
                       else tuple(range(snapshot.visible_channel_count)))
            row_height = plot_height / len(indices)
            point = event.position()
            if point.x() < left and top <= point.y() < top + plot_height:
                row = min(len(indices) - 1, int((point.y() - top) / row_height))
                self._contact_scan_handler(indices[row])
                event.accept()
                return
        super().mousePressEvent(event)

    def set_snapshot(self, snapshot: ScopeSnapshot) -> None:
        self._snapshot = snapshot
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), BACKGROUND)
        snapshot = self._snapshot
        if snapshot is None:
            self._draw_centered_message(painter, "等待数据")
            return

        waveforms = snapshot.displayed_waveforms
        channel_indices = tuple(range(snapshot.visible_channel_count))
        if self.selected_channel_index is not None:
            channel_indices = (self.selected_channel_index,)
            channels = (snapshot.raw_waveforms if snapshot.waveform_mode is WaveformMode.RAW
                        else snapshot.filtered_waveforms)
            count = max(1, round(snapshot.time_window_seconds * snapshot.sample_rate_hz))
            waveforms = (channels[self.selected_channel_index][-count:],)
        if not waveforms or not waveforms[0]:
            self._draw_centered_message(painter, "等待数据 · 点击继续播放")
            return

        left = 92.0
        right = 16.0
        top = 28.0
        bottom = 26.0
        plot_width = max(1.0, self.width() - left - right)
        plot_height = max(1.0, self.height() - top - bottom)
        row_height = plot_height / len(channel_indices)
        half_range = self.base_half_range_counts / snapshot.amplitude_scale

        painter.setFont(QFont(self.font().family(), 9))
        painter.setPen(QPen(MUTED, 1.0))
        title_mode = "原始波形" if snapshot.waveform_mode is WaveformMode.RAW else "滤波波形"
        painter.drawText(
            QRectF(left, 4.0, plot_width, 20.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{title_mode} · {snapshot.time_window_seconds:g} s · ±{half_range:g} counts（零点固定）",
        )

        for grid_index in range(5):
            x = left + plot_width * grid_index / 4.0
            painter.setPen(QPen(GRID, 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(x, top), QPointF(x, top + plot_height))
            elapsed = -snapshot.time_window_seconds * (1.0 - grid_index / 4.0)
            painter.setPen(MUTED)
            painter.drawText(
                QRectF(x - 28.0, top + plot_height + 3.0, 56.0, 18.0),
                Qt.AlignmentFlag.AlignCenter,
                f"{elapsed:g}s",
            )

        for row_index, (channel_index, values) in enumerate(zip(channel_indices, waveforms)):
            center_y = top + (row_index + 0.5) * row_height
            painter.setPen(QPen(GRID, 1.0))
            painter.drawLine(QPointF(left, center_y), QPointF(left + plot_width, center_y))
            painter.setPen(TEXT)
            painter.drawText(
                QRectF(7.0, center_y - row_height / 2.0, left - 13.0, row_height * 0.52),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{ELECTRODE_CHANNELS[channel_index].logical_channel}  "
                f"{ELECTRODE_CHANNELS[channel_index].electrode}",
            )
            painter.setPen(self._contact_colors[channel_index])
            painter.setFont(QFont(self.font().family(), 8))
            painter.drawText(
                QRectF(7.0, center_y, left - 13.0, row_height / 2.0 - 1.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._contact_labels[channel_index],
            )

            path = QPainterPath()
            active_segment = False
            value_count = len(values)
            for sample_index, sample in enumerate(values):
                if sample is None:
                    active_segment = False
                    continue
                # Preserve the design timebase even before the display window fills.
                x_fraction = 1.0 - (value_count - 1 - sample_index) / (
                    snapshot.time_window_seconds * snapshot.sample_rate_hz
                )
                x = left + x_fraction * plot_width
                normalized = max(-1.0, min(1.0, sample / half_range))
                y = center_y - normalized * row_height * 0.42
                if active_segment:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                    active_segment = True
            painter.setPen(QPen(TRACE_COLORS[channel_index], 1.15))
            painter.drawPath(path)

    def _draw_centered_message(self, painter: QPainter, message: str) -> None:
        painter.setPen(MUTED)
        painter.setFont(QFont(self.font().family(), 12))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)


class SpectrumCanvas(QWidget):
    """Latest one-sided filtered spectrum for one selected logical channel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumCanvas")
        self.setMinimumSize(330, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._snapshot: ScopeSnapshot | None = None
        self._channel_index = 0

    def set_channel_index(self, channel_index: int) -> None:
        if not 0 <= channel_index < len(ELECTRODE_CHANNELS):
            raise ValueError("spectrum channel index must be in range 0..15")
        self._channel_index = channel_index
        self.update()

    def set_snapshot(self, snapshot: ScopeSnapshot) -> None:
        self._snapshot = snapshot
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)
        snapshot = self._snapshot
        spectrum = None
        if snapshot is not None and snapshot.latest_spectrum is not None:
            spectrum = snapshot.latest_spectrum.spectra[self._channel_index]
        if spectrum is None:
            painter.setPen(MUTED)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "等待一个连续 FFT 窗口")
            return

        left = 47.0
        right = 13.0
        top = 28.0
        bottom = 31.0
        width = max(1.0, self.width() - left - right)
        height = max(1.0, self.height() - top - bottom)
        maximum_hz = min(60.0, spectrum.frequencies_hz[-1])
        points = tuple(
            (frequency, amplitude)
            for frequency, amplitude in zip(spectrum.frequencies_hz, spectrum.amplitudes)
            if frequency <= maximum_hz
        )
        maximum_amplitude = max((amplitude for _, amplitude in points), default=1.0)
        maximum_amplitude = max(1.0, maximum_amplitude)

        painter.setFont(QFont(self.font().family(), 9))
        painter.setPen(TEXT)
        channel = ELECTRODE_CHANNELS[self._channel_index]
        painter.drawText(
            QRectF(left, 4.0, width, 20.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{channel.logical_channel} {channel.electrode} · 滤波频谱",
        )
        for grid_index in range(4):
            y = top + height * grid_index / 3.0
            painter.setPen(QPen(GRID, 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
        for frequency_hz in range(0, int(maximum_hz) + 1, 10):
            x = left + frequency_hz / maximum_hz * width
            painter.setPen(QPen(GRID, 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(x, top), QPointF(x, top + height))
            painter.setPen(MUTED)
            painter.drawText(
                QRectF(x - 18.0, top + height + 3.0, 36.0, 17.0),
                Qt.AlignmentFlag.AlignCenter,
                str(frequency_hz),
            )

        path = QPainterPath()
        for point_index, (frequency, amplitude) in enumerate(points):
            x = left + frequency / maximum_hz * width
            y = top + height * (1.0 - amplitude / maximum_amplitude)
            if point_index:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
        painter.setPen(QPen(ACCENT, 1.7))
        painter.drawPath(path)
        painter.setPen(MUTED)
        painter.drawText(QRectF(left, top + height + 18.0, width, 13.0), Qt.AlignmentFlag.AlignCenter, "Hz")
        painter.save()
        painter.translate(14.0, top + height)
        painter.rotate(-90.0)
        painter.drawText(QRectF(0.0, 0.0, height, 18.0), Qt.AlignmentFlag.AlignCenter, "峰值幅度 / counts")
        painter.restore()


COUNTER_LABELS = (
    ("records_received", "接收记录"),
    ("valid_records", "有效记录"),
    ("invalid_records", "无效记录"),
    ("length_errors", "长度错误"),
    ("crc_errors", "CRC 错误"),
    ("format_errors", "格式错误"),
    ("sequence_gap_frames", "序号缺口"),
    ("duplicate_records", "重复记录"),
    ("out_of_order_records", "乱序记录"),
    ("adc1_status_header_errors", "ADC1 状态头"),
    ("adc2_status_header_errors", "ADC2 状态头"),
    ("adc1_timeout_records", "ADC1 超时"),
    ("adc2_timeout_records", "ADC2 超时"),
    ("sync_skew_records", "同步偏差"),
)


class ErrorCounterPanel(QGroupBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("数据与错误计数", parent)
        self.setObjectName("errorCounterPanel")
        layout = QGridLayout(self)
        layout.setContentsMargins(8, 14, 8, 6)
        layout.setVerticalSpacing(1)
        layout.setColumnStretch(0, 1)
        self._value_labels: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(COUNTER_LABELS):
            title_label = QLabel(title)
            title_label.setStyleSheet("color: #94a3b8;")
            value_label = QLabel("0")
            value_label.setObjectName(f"counter_{key}")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(title_label, row, 0)
            layout.addWidget(value_label, row, 1)
            self._value_labels[key] = value_label

    def set_counters(self, counters: dict[str, int] | Sequence[tuple[str, int]]) -> None:
        values = dict(counters)
        for key, label in self._value_labels.items():
            value = values.get(key, 0)
            label.setText(f"{value:,}")
            is_error = key not in ("records_received", "valid_records") and value > 0
            label.setStyleSheet(f"font-weight: 600; color: {'#f59e0b' if is_error else '#dbeafe'};")


class OscilloscopeWindow(QMainWindow):
    """Desktop prototype; the source can later be replaced by a DMA adapter."""

    TIMER_INTERVAL_MS = 40

    def __init__(
        self,
        source: RealtimeFrameSource | None = None,
        *,
        start_timer: bool = True,
        dsp_config: DspConfig | None = None,
    ) -> None:
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            configure_ui_font(app)
        self.setObjectName("oscilloscopeWindow")
        self.setWindowTitle("EEG Zynq · 16 通道示波器原型")
        self.resize(1380, 850)
        active_source = source or RealtimeSimulatedSource()
        dsp_config = dsp_config or (
            DspConfig(sample_rate_hz=active_source.info.sample_rate_hz)
            if isinstance(active_source, FileReplaySource)
            else None
        )
        self.session = OscilloscopeSession(active_source, dsp_config=dsp_config)
        self._replay_source = (
            active_source if isinstance(active_source, FileReplaySource) else None
        )
        self._recorder: ContinuousRecordWriter | None = None
        self._last_recording_info: RecordingInfo | None = None
        self._recording_path: Path | None = None
        self._write_error: str | None = None
        self._storage_message = "未保存或加载文件"
        self._replay_error: str | None = None
        self._frame_budget = 0.0
        self._lead_off_transport: SimulatedLeadOffTransport | None = None
        self._lead_off_machine: LeadOffStateMachine | None = None
        self._lead_off_report: LeadOffReport | None = None
        self._lead_off_session_isolated = False
        self._resume_after_lead_off = False
        self._build_ui()
        self._connect_controls()
        if self._replay_source is not None and self._replay_source.info.is_board_internal_test:
            self._set_import_view_defaults()
        self._timer = QTimer(self)
        self._timer.setInterval(self.TIMER_INTERVAL_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.tick_once)
        self._refresh(self.session.snapshot())
        if start_timer:
            self._timer.start()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("scopeRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 10, 12, 10)
        root_layout.setSpacing(9)

        controls = QFrame()
        controls.setObjectName("controlBar")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(10, 7, 10, 7)
        self.play_button = QPushButton("暂停")
        self.play_button.setObjectName("playPauseButton")
        self.restart_button = QPushButton("重新开始")
        self.restart_button.setObjectName("restartButton")
        self.channel_count_combo = self._combo("channelCountCombo", ("8 路", "16 路"))
        self.waveform_mode_combo = self._combo("waveformModeCombo", ("滤波", "原始"))
        self.time_window_combo = self._combo(
            "timeWindowCombo", tuple(f"{seconds:g} s" for seconds in TIME_WINDOWS_SECONDS)
        )
        self.time_window_combo.setCurrentText("4 s")
        self.amplitude_combo = self._combo(
            "amplitudeCombo", tuple(f"{scale:g}×" for scale in AMPLITUDE_SCALES)
        )
        self.amplitude_combo.setCurrentText("1×")
        self.spectrum_channel_combo = self._combo(
            "spectrumChannelCombo",
            tuple(f"{channel.logical_channel} {channel.electrode}" for channel in ELECTRODE_CHANNELS),
        )
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.restart_button)
        self._add_control(controls_layout, "显示", self.channel_count_combo)
        self._add_control(controls_layout, "波形", self.waveform_mode_combo)
        self._add_control(controls_layout, "时窗", self.time_window_combo)
        self._add_control(controls_layout, "幅值", self.amplitude_combo)
        self._add_control(controls_layout, "频谱通道", self.spectrum_channel_combo)
        controls_layout.addStretch(1)
        self.playback_label = QLabel("● 播放中")
        self.playback_label.setObjectName("playbackStatusLabel")
        controls_layout.addWidget(self.playback_label)
        root_layout.addWidget(controls)

        view_controls = QHBoxLayout()
        self.range_combo = self._combo(
            "countRangeCombo", tuple(f"±{value:g} counts" for value in COUNT_HALF_RANGES)
        )
        self.waveform_channel_combo = self._combo(
            "waveformChannelCombo", ("全部显示通道", *(
                f"{channel.logical_channel} {channel.electrode}" for channel in ELECTRODE_CHANNELS
            ))
        )
        self._add_control(view_controls, "基准量程", self.range_combo)
        self._add_control(view_controls, "波形通道", self.waveform_channel_combo)
        view_controls.addWidget(QLabel("原始模式保留偏置及幅值；不平滑、不插值、不补帧"))
        view_controls.addStretch(1)
        root_layout.addLayout(view_controls)

        storage = QFrame()
        storage.setObjectName("storageBar")
        storage_layout = QGridLayout(storage)
        storage_layout.setContentsMargins(10, 7, 10, 7)
        storage_layout.setColumnStretch(5, 1)
        self.start_recording_button = QPushButton("开始保存")
        self.start_recording_button.setObjectName("startRecordingButton")
        self.stop_recording_button = QPushButton("停止保存")
        self.stop_recording_button.setObjectName("stopRecordingButton")
        self.load_replay_button = QPushButton("加载文件")
        self.load_replay_button.setObjectName("loadReplayButton")
        self.unload_replay_button = QPushButton("停止/卸载")
        self.unload_replay_button.setObjectName("unloadReplayButton")
        self.recording_status_label = QLabel("保存：未开始")
        self.recording_status_label.setObjectName("recordingStatusLabel")
        self.recording_path_label = QLabel("文件：--")
        self.recording_path_label.setObjectName("recordingPathLabel")
        self.recording_path_label.setToolTip("")
        self.replay_status_label = QLabel("回放：未加载")
        self.replay_status_label.setObjectName("replayStatusLabel")
        self.replay_info_label = QLabel("当前帧 0/0 · 00:00.000/00:00.000 · 原始来源 --")
        self.replay_info_label.setObjectName("replayInfoLabel")
        self.replay_progress = QProgressBar()
        self.replay_progress.setObjectName("replayProgressBar")
        self.replay_progress.setRange(0, 10_000)
        self.replay_progress.setValue(0)
        self.replay_progress.setFormat("0.00%")
        storage_layout.addWidget(self.start_recording_button, 0, 0)
        storage_layout.addWidget(self.stop_recording_button, 0, 1)
        storage_layout.addWidget(self.load_replay_button, 0, 2)
        storage_layout.addWidget(self.unload_replay_button, 0, 3)
        storage_layout.addWidget(self.recording_status_label, 0, 4, 1, 2)
        storage_layout.addWidget(self.recording_path_label, 1, 0, 1, 4)
        storage_layout.addWidget(self.replay_status_label, 1, 4)
        storage_layout.addWidget(self.replay_progress, 1, 5)
        storage_layout.addWidget(self.replay_info_label, 2, 0, 1, 6)
        self.replay_origin_label = QLabel()
        self.replay_origin_label.setObjectName("replayOriginLabel")
        self.replay_origin_label.setWordWrap(True)
        self.replay_origin_label.setStyleSheet("font-weight: 600; color: #fbbf24;")
        storage_layout.addWidget(self.replay_origin_label, 3, 0, 1, 6)
        self.timebase_label = QLabel()
        self.timebase_label.setObjectName("replayTimebaseLabel")
        self.timebase_label.setWordWrap(True)
        storage_layout.addWidget(self.timebase_label, 4, 0, 1, 6)
        self.replay_error_label = QLabel()
        self.replay_error_label.setObjectName("replayErrorLabel")
        self.replay_error_label.setWordWrap(True)
        self.replay_error_label.setStyleSheet("color: #f87171;")
        storage_layout.addWidget(self.replay_error_label, 5, 0, 1, 6)
        root_layout.addWidget(storage)

        diagnostic = QGroupBox("ADS1299 lead-off 接触状态")
        diagnostic.setObjectName("leadOffPanel")
        diagnostic_layout = QGridLayout(diagnostic)
        diagnostic_layout.setContentsMargins(10, 11, 10, 7)
        diagnostic_layout.setHorizontalSpacing(10)
        diagnostic_layout.setVerticalSpacing(4)
        self.lead_off_start_button = QPushButton("开始模拟检测")
        self.lead_off_start_button.setObjectName("leadOffStartButton")
        self.lead_off_cancel_button = QPushButton("取消检测")
        self.lead_off_cancel_button.setObjectName("leadOffCancelButton")
        self.lead_off_scenario_combo = self._combo(
            "leadOffScenarioCombo", tuple(label for label, _ in LEAD_OFF_SCENARIOS)
        )
        self.lead_off_mode_label = QLabel("模式：EEG")
        self.lead_off_mode_label.setObjectName("leadOffModeLabel")
        self.lead_off_phase_label = QLabel("阶段：空闲")
        self.lead_off_phase_label.setObjectName("leadOffPhaseLabel")
        self.lead_off_progress = QProgressBar()
        self.lead_off_progress.setObjectName("leadOffProgressBar")
        self.lead_off_progress.setRange(0, 1000)
        self.lead_off_progress.setFormat("0.0%")
        self.lead_off_notice_label = QLabel("软件模拟，非真实阻抗测量；不输出欧姆值")
        self.lead_off_notice_label.setObjectName("leadOffNoticeLabel")
        self.lead_off_notice_label.setStyleSheet("font-weight: 600; color: #f59e0b;")
        diagnostic_layout.addWidget(self.lead_off_start_button, 0, 0)
        diagnostic_layout.addWidget(self.lead_off_cancel_button, 0, 1)
        diagnostic_layout.addWidget(self.lead_off_scenario_combo, 0, 2, 1, 2)
        diagnostic_layout.addWidget(self.lead_off_mode_label, 0, 4)
        diagnostic_layout.addWidget(self.lead_off_phase_label, 0, 5)
        diagnostic_layout.addWidget(self.lead_off_progress, 0, 6)
        diagnostic_layout.addWidget(self.lead_off_notice_label, 0, 7)
        self.lead_off_channel_labels: list[QLabel] = []
        for channel_index, channel in enumerate(ELECTRODE_CHANNELS):
            label = QLabel(f"{channel.logical_channel} {channel.electrode}: 未检测")
            label.setObjectName(f"leadOffChannel{channel.logical_index:02d}")
            self.lead_off_channel_labels.append(label)
            diagnostic_layout.addWidget(label, 1 + channel_index // 8, channel_index % 8)
        self.lead_off_reference_label = QLabel("REF FCz（公共 SRB1）: 未检测")
        self.lead_off_reference_label.setObjectName("leadOffReferenceLabel")
        self.lead_off_bias_label = QLabel("BIAS AFz: 暂不支持")
        self.lead_off_bias_label.setObjectName("leadOffBiasLabel")
        self.lead_off_error_label = QLabel("超时/恢复错误：无")
        self.lead_off_error_label.setObjectName("leadOffErrorLabel")
        diagnostic_layout.addWidget(self.lead_off_reference_label, 3, 0, 1, 2)
        diagnostic_layout.addWidget(self.lead_off_bias_label, 3, 2, 1, 2)
        diagnostic_layout.addWidget(self.lead_off_error_label, 3, 4, 1, 4)
        root_layout.addWidget(diagnostic)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        self.waveform_canvas = WaveformCanvas()
        self.waveform_canvas.set_contact_scan_handler(
            self._start_lead_off_from_channel,
            available=self.session.source.source_type is SourceType.SIMULATED,
        )
        splitter.addWidget(self.waveform_canvas)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(5, 0, 0, 0)
        self.spectrum_canvas = SpectrumCanvas()
        self.counter_panel = ErrorCounterPanel()
        right_layout.addWidget(self.spectrum_canvas, 3)
        right_layout.addWidget(self.counter_panel, 4)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes((950, 400))
        root_layout.addWidget(splitter, 1)

        footer = QFrame()
        footer.setObjectName("statusBarFrame")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(10, 5, 10, 5)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setObjectName("sourceLabel")
        self.sequence_label = QLabel()
        self.sequence_label.setObjectName("sequenceLabel")
        self.assumption_label = QLabel("DSP：250 SPS · HP 0.5 Hz · LP 45 Hz · Notch 50 Hz · raw counts")
        self.assumption_label.setObjectName("dspAssumptionLabel")
        footer_layout.addWidget(self.source_label)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.assumption_label)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.sequence_label)
        root_layout.addWidget(footer)
        self.setCentralWidget(root)
        self.setStyleSheet(
            """
            QMainWindow, QWidget#scopeRoot { background: #070b14; color: #dbeafe; }
            QLabel { color: #dbeafe; }
            QFrame#controlBar, QFrame#storageBar, QFrame#statusBarFrame, QGroupBox {
                background: #111a2e; border: 1px solid #29344f; border-radius: 7px;
            }
            QGroupBox { margin-top: 8px; padding-top: 12px; font-weight: 600; }
            QGroupBox::title {
                subcontrol-origin: margin; left: 9px; padding: 0 4px; color: #cbd5e1;
            }
            QFrame#storageBar QLabel, QFrame#statusBarFrame QLabel { color: #94a3b8; }
            QPushButton, QComboBox {
                background: #17233b; color: #e2e8f0; border: 1px solid #334155;
                border-radius: 5px; min-height: 28px; padding: 0 9px;
            }
            QPushButton:hover, QComboBox:hover { border-color: #38bdf8; }
            QPushButton:disabled { color: #64748b; }
            QPushButton:pressed { background: #0f172a; }
            QComboBox QAbstractItemView { background: #17233b; color: #e2e8f0; }
            QProgressBar {
                border: 1px solid #334155; border-radius: 4px; background: #0f172a;
                color: #dbeafe; text-align: center; min-height: 18px;
            }
            QProgressBar::chunk { background: #0284c7; border-radius: 3px; }
            """
        )

    @staticmethod
    def _combo(object_name: str, values: Sequence[str]) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(object_name)
        combo.addItems(values)
        return combo

    @staticmethod
    def _add_control(layout: QHBoxLayout, label_text: str, widget: QWidget) -> None:
        label = QLabel(label_text)
        label.setStyleSheet("color: #94a3b8;")
        layout.addSpacing(5)
        layout.addWidget(label)
        layout.addWidget(widget)

    def _connect_controls(self) -> None:
        self.play_button.clicked.connect(self.toggle_playback)
        self.restart_button.clicked.connect(self.restart_playback)
        self.channel_count_combo.currentIndexChanged.connect(self._channel_count_changed)
        self.waveform_mode_combo.currentIndexChanged.connect(self._waveform_mode_changed)
        self.time_window_combo.currentIndexChanged.connect(self._time_window_changed)
        self.amplitude_combo.currentIndexChanged.connect(self._amplitude_changed)
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        self.waveform_channel_combo.currentIndexChanged.connect(self._waveform_channel_changed)
        self.spectrum_channel_combo.currentIndexChanged.connect(
            self.spectrum_canvas.set_channel_index
        )
        self.start_recording_button.clicked.connect(self.choose_recording_path)
        self.stop_recording_button.clicked.connect(self.stop_recording_from_ui)
        self.load_replay_button.clicked.connect(self.choose_replay_file)
        self.unload_replay_button.clicked.connect(self.unload_replay)
        self.lead_off_start_button.clicked.connect(self.start_lead_off_simulation)
        self.lead_off_cancel_button.clicked.connect(self.cancel_lead_off)

    def tick_once(self) -> int:
        if self._lead_off_machine is not None and self._lead_off_machine.active:
            self._lead_off_machine.step()
            if self._lead_off_machine.phase.terminal:
                self._finish_lead_off_session()
            self._refresh(self.session.snapshot())
            return 0
        batch_size = self._next_batch_size()
        try:
            processed = self.session.advance(batch_size)
        except RecordingWriteError as exc:
            self._handle_recording_failure(exc)
            processed = 0
        except ReplayLoadError as exc:
            self._set_replay_failure(str(exc))
            self._show_error("文件回放失败", str(exc))
            processed = 0
        except LinuxDmaError as exc:
            self.session.pause()
            self._storage_message = f"有限DMA已STOP：{exc}"
            self._show_error("有限DMA停止", str(exc))
            processed = 0
        if self._replay_source is not None and self._replay_source.progress.finished:
            self.session.pause()
        if self.session.source.source_type is SourceType.ADS1299_DMA and getattr(self.session.source, "finished", False):
            self.session.pause()
        self._refresh(self.session.snapshot())
        return processed

    def _next_batch_size(self) -> int:
        if not self.session.playing:
            return 0
        self._frame_budget += (
            self.session.dsp_config.sample_rate_hz * self.TIMER_INTERVAL_MS / 1000.0
        )
        batch_size = int(self._frame_budget)
        self._frame_budget -= batch_size
        return batch_size

    def toggle_playback(self) -> None:
        if self.session.source.source_type is SourceType.ADS1299_DMA and (
            getattr(self.session.source, "failure", None) or getattr(self.session.source, "finished", False)
        ):
            return
        if self._replay_error is not None:
            self._show_error("回放已停止，请重新加载", self._replay_error)
            return
        if (
            self._replay_source is not None
            and self._replay_source.progress.finished
            and not self.session.playing
        ):
            self.session.restart()
            self._frame_budget = 0.0
        self.session.toggle_playback()
        self._refresh(self.session.snapshot())

    def restart_playback(self) -> None:
        if self.session.source.source_type is SourceType.ADS1299_DMA:
            self._show_error("有限DMA不能重启", "本源只允许一次接收；需要另行授权的新会话。")
            return
        if self._replay_error is not None:
            self._show_error("回放已停止，请重新加载", self._replay_error)
            return
        if self._recorder is not None:
            self.stop_recording_from_ui()
        self.session.restart()
        self.session.play()
        self._frame_budget = 0.0
        self._refresh(self.session.snapshot())

    def choose_recording_path(self) -> None:
        if self._replay_source is not None:
            self._show_error("无法开始保存", "文件回放期间不能保存为新的模拟采集文件。")
            return
        default_name = str(Path.cwd() / "simulation_recording.eegbin")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存模拟 EEG 数据",
            default_name,
            "EEG fixed records (*.eegbin);;All files (*)",
        )
        if not path:
            self._storage_message = "已取消保存"
            self._refresh_storage()
            return
        try:
            self.start_recording(path)
        except RecordingError as exc:
            self._show_error("无法开始保存", str(exc))
        self._refresh_storage()

    def start_recording(self, path: str | Path) -> None:
        if self._recorder is not None:
            raise RecordingError("a recording is already active")
        if self.session.diagnostic_active:
            raise RecordingError("cannot record EEG frames during lead-off diagnostics")
        if self.session.source.source_type is not SourceType.SIMULATED:
            raise RecordingError("only the current SIMULATED source can be recorded")
        recorder = ContinuousRecordWriter(path)
        self._recorder = recorder
        self._last_recording_info = None
        self._recording_path = recorder.data_path
        self._write_error = None
        self._storage_message = "正在保存模拟数据"
        self.session.set_record_sink(recorder.append_record)
        self._refresh_storage()

    def stop_recording(self) -> RecordingInfo | None:
        recorder = self._recorder
        if recorder is None:
            return None
        self.session.set_record_sink(None)
        self._recorder = None
        try:
            info = recorder.close()
        except RecordingWriteError as exc:
            self._write_error = str(exc)
            self._storage_message = f"保存失败：{exc}"
            self._refresh_storage()
            raise
        self._last_recording_info = info
        self._write_error = None
        self._storage_message = "保存完成，文件已正常关闭"
        self._refresh_storage()
        return info

    def stop_recording_from_ui(self) -> None:
        if self._recorder is None:
            self._storage_message = "当前没有正在保存的文件"
            self._refresh_storage()
            return
        try:
            self.stop_recording()
        except RecordingWriteError as exc:
            self._show_error("停止保存失败", str(exc))

    def _handle_recording_failure(self, error: RecordingWriteError) -> None:
        if self._recorder is not None:
            self._recording_path = self._recorder.data_path
        self.session.set_record_sink(None)
        self._recorder = None
        self._write_error = str(error)
        self._storage_message = f"写入错误：{error}"
        self._show_error("保存写入失败", str(error))

    def choose_replay_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载 EEG 记录文件",
            str(Path.cwd()),
            "EEG fixed records (*.eegbin);;All files (*)",
        )
        if not path:
            self._storage_message = "已取消文件加载"
            self._refresh_storage()
            return
        try:
            self.load_replay(path)
        except (RecordingError, ValueError) as exc:
            self._show_error("文件加载失败", str(exc))
        self._refresh(self.session.snapshot())

    def load_replay(self, path: str | Path) -> FileReplaySource:
        if self._recorder is not None:
            raise RecordingError("stop the active recording before loading a replay file")
        if self.session.diagnostic_active:
            raise RecordingError("finish lead-off diagnostics before loading replay data")
        replay = None
        try:
            replay = FileReplaySource(path)
            config = DspConfig(sample_rate_hz=replay.info.sample_rate_hz)
            new_session = OscilloscopeSession(replay, dsp_config=config)
        except (OSError, TypeError, ValueError) as exc:
            if replay is not None:
                replay.close()
            self._set_replay_failure(f"加载 {path} 失败：{exc}")
            self._refresh(self.session.snapshot())
            raise
        new_session.pause()
        self._apply_view_settings(new_session)
        self._close_replay_source()
        self.session = new_session
        self._replay_source = replay
        self._replay_error = None
        if replay.info.is_board_internal_test:
            self._set_import_view_defaults()
        self._frame_budget = 0.0
        self._storage_message = "文件已通过完整校验，等待播放"
        self._write_error = None
        self._refresh(self.session.snapshot())
        return replay

    def unload_replay(self) -> None:
        if self._replay_source is None and self._replay_error is None:
            self._storage_message = "当前没有已加载的回放文件"
            self._refresh_storage()
            return
        self._close_replay_source()
        self._replay_error = None
        self.range_combo.setCurrentIndex(0)
        new_session = OscilloscopeSession(RealtimeSimulatedSource())
        self._apply_view_settings(new_session)
        self.session = new_session
        self._frame_budget = 0.0
        self._storage_message = "回放已停止并卸载，已恢复模拟实时源"
        self._refresh(self.session.snapshot())

    def _close_replay_source(self) -> None:
        if self._replay_source is not None:
            self._replay_source.close()
            self._replay_source = None

    def start_lead_off_simulation(self) -> None:
        if self._lead_off_machine is not None and self._lead_off_machine.active:
            self._show_error("无法开始检测", "已有 lead-off 检测正在运行。")
            return
        if self.session.source.source_type is not SourceType.SIMULATED:
            self._show_error("无法开始检测", "当前来源不允许模拟 lead-off 检测。")
            return
        if self._recorder is not None:
            try:
                self.stop_recording()
            except RecordingWriteError as exc:
                self._show_error("无法开始检测", f"停止并关闭 EEG 保存文件失败：{exc}")
                return
        scenario = LEAD_OFF_SCENARIOS[self.lead_off_scenario_combo.currentIndex()][1]
        self._resume_after_lead_off = self.session.playing
        try:
            self.session.begin_diagnostic_mode()
            self._lead_off_session_isolated = True
            self._lead_off_transport = SimulatedLeadOffTransport(scenario)
            self._lead_off_machine = LeadOffStateMachine(
                self._lead_off_transport, scenario_name=scenario.value
            )
            self._lead_off_report = None
            self._lead_off_machine.start()
        except (RuntimeError, LeadOffProtocolError, OSError, ValueError) as exc:
            if self._lead_off_session_isolated:
                self.session.end_diagnostic_mode()
                self._lead_off_session_isolated = False
                if self._resume_after_lead_off:
                    self.session.play()
            self._lead_off_transport = None
            self._lead_off_machine = None
            self._lead_off_report = None
            self._show_error("无法开始检测", str(exc))
            self._refresh(self.session.snapshot())
            return
        self._frame_budget = 0.0
        self._refresh(self.session.snapshot())

    def _start_lead_off_from_channel(self, channel_index: int) -> None:
        if not 0 <= channel_index < len(ELECTRODE_CHANNELS):
            return
        self.lead_off_notice_label.setText(
            f"CH{channel_index + 1:02d} 已请求全通道接触状态扫描；仅软件模拟，不输出欧姆值"
        )
        self.start_lead_off_simulation()

    def cancel_lead_off(self) -> None:
        if self._lead_off_machine is not None and self._lead_off_machine.active:
            self._lead_off_machine.cancel()
            self._refresh_lead_off()

    def _finish_lead_off_session(self) -> None:
        machine = self._lead_off_machine
        if machine is None or not machine.phase.terminal:
            return
        self._lead_off_report = machine.report
        if self._lead_off_session_isolated:
            self.session.end_diagnostic_mode()
            self._lead_off_session_isolated = False
            if self._resume_after_lead_off:
                self.session.play()
        self._frame_budget = 0.0

    def _apply_view_settings(self, session: OscilloscopeSession) -> None:
        session.set_visible_channel_count(8 if self.channel_count_combo.currentIndex() == 0 else 16)
        session.set_waveform_mode(
            WaveformMode.FILTERED
            if self.waveform_mode_combo.currentIndex() == 0
            else WaveformMode.RAW
        )
        session.set_time_window_seconds(TIME_WINDOWS_SECONDS[self.time_window_combo.currentIndex()])
        session.set_amplitude_scale(AMPLITUDE_SCALES[self.amplitude_combo.currentIndex()])

    def _set_import_view_defaults(self) -> None:
        self.channel_count_combo.setCurrentIndex(1)
        self.waveform_mode_combo.setCurrentIndex(1)
        self.amplitude_combo.setCurrentText("1×")
        self.range_combo.setCurrentIndex(1)
        self.waveform_channel_combo.setCurrentIndex(0)
        self._apply_view_settings(self.session)

    def _set_replay_failure(self, reason: str) -> None:
        self.session.pause()
        self._replay_error = reason
        self._storage_message = f"回放错误（已暂停，需重新加载）：{reason}"

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000.0))
        minutes, remainder = divmod(milliseconds, 60_000)
        whole_seconds, millis = divmod(remainder, 1000)
        return f"{minutes:02d}:{whole_seconds:02d}.{millis:03d}"

    def _refresh_storage(self) -> None:
        self.replay_error_label.setText(self._storage_message if self._replay_error else "")
        self.replay_error_label.setVisible(self._replay_error is not None)
        imported = self._replay_source is not None and self._replay_source.info.is_board_internal_test
        self.replay_origin_label.setVisible(imported)
        self.timebase_label.setVisible(imported)
        if imported:
            info = self._replay_source.info
            signal = "真实DMA板测内部测试信号" if info.is_dma_internal_test else "真实板测内部测试信号"
            self.replay_origin_label.setText(
                f"FILE_REPLAY · {signal} · 非实时、非真人脑电 · "
                f"原事件 {self._replay_source.info.import_event_id}"
            )
            self.timebase_label.setText(
                "设计采样率 244.140625 SPS · 间隔 4096000 ns · 无独立逐帧实测时间；"
                f"首末跨度 {(info.record_count - 1) / info.sample_rate_hz:.6f} s，"
                f"按样本数播放 {info.duration_seconds:.6f} s"
            )
            self.timebase_label.setToolTip(
                "112 字节帧采样率字段编码 244141 mHz = 244.141 SPS；"
                "相对设计采样率高 0.000375 SPS（+1.536 ppm）。"
                "GUI/DSP 使用经严格元数据确认的 244.140625 SPS；时间戳按序号差推导。"
            )
        if self._recorder is not None:
            status = self._recorder.status
            self.recording_status_label.setText(
                f"保存：进行中 · {status.record_count:,} 帧 · "
                f"{self._format_duration(status.duration_seconds)}"
            )
            self.recording_status_label.setStyleSheet("font-weight: 600; color: #f87171;")
            path = status.data_path
        elif self._last_recording_info is not None:
            info = self._last_recording_info
            self.recording_status_label.setText(
                f"保存：已停止 · {info.record_count:,} 帧 · "
                f"{self._format_duration(info.duration_seconds)}"
            )
            self.recording_status_label.setStyleSheet("color: #34d399;")
            path = info.data_path
        else:
            error = f" · 错误：{self._write_error}" if self._write_error else ""
            self.recording_status_label.setText(f"保存：未开始{error}")
            self.recording_status_label.setStyleSheet(
                "color: #f59e0b;" if self._write_error else "color: #94a3b8;"
            )
            path = self._recording_path
        if self._replay_source is not None:
            replay_path = self._replay_source.info.data_path
            self.recording_path_label.setText(f"回放文件：{replay_path.name}")
            self.recording_path_label.setToolTip(str(replay_path))
        else:
            self.recording_path_label.setText(
                f"文件：{path}" if path is not None else f"状态：{self._storage_message}"
            )
            self.recording_path_label.setToolTip(str(path) if path is not None else self._storage_message)

        if self._replay_source is None:
            self.replay_status_label.setText("回放：未加载")
            self.replay_info_label.setText("当前帧 0/0 · 00:00.000/00:00.000 · 原始来源 --")
            self.replay_progress.setValue(0)
            self.replay_progress.setFormat("0.00%")
        else:
            progress = self._replay_source.progress
            if self._replay_error:
                state = "错误（已暂停）"
            elif progress.finished:
                state = "已完成"
            elif self.session.playing:
                state = "播放中"
            elif progress.current_record == 0:
                state = "已加载"
            else:
                state = "已暂停"
            self.replay_status_label.setText(f"回放：{state}")
            percent_value = min(10_000, round(progress.percent * 100.0))
            self.replay_progress.setValue(percent_value)
            self.replay_progress.setFormat(f"{progress.percent:.2f}%")
            sources = ("真实板测内部测试信号" if imported
                       else ", ".join(self._replay_source.info.original_source_names))
            metadata = "含校验元数据" if self._replay_source.info.metadata_present else "无侧车元数据"
            self.replay_info_label.setText(
                f"当前帧 {progress.current_record:,}/{progress.total_records:,} · "
                f"{self._format_duration(progress.elapsed_seconds)}/"
                f"{self._format_duration(progress.total_seconds)} · "
                f"原始来源 {sources} · 当前来源 FILE_REPLAY · {metadata}"
            )
            self.replay_info_label.setToolTip(str(self._replay_source.info.data_path))

        self.start_recording_button.setEnabled(
            self._recorder is None and self._replay_source is None
        )
        self.stop_recording_button.setEnabled(self._recorder is not None)
        self.load_replay_button.setEnabled(self._recorder is None)
        self.unload_replay_button.setEnabled(self._replay_source is not None or self._replay_error is not None)

    def _refresh_lead_off(self) -> None:
        machine = self._lead_off_machine
        active = machine is not None and machine.active
        phase = machine.phase if machine is not None else LeadOffPhase.IDLE
        progress = machine.progress_percent if machine is not None else 0.0
        self.lead_off_mode_label.setText("模式：阻抗检测" if active else "模式：EEG")
        self.lead_off_phase_label.setText(f"阶段：{LEAD_OFF_PHASE_TEXT[phase]}")
        self.lead_off_progress.setValue(round(progress * 10.0))
        self.lead_off_progress.setFormat(f"{progress:.1f}%")
        is_dma = self.session.source.source_type is SourceType.ADS1299_DMA
        self.waveform_canvas.set_contact_scan_handler(
            self._start_lead_off_from_channel,
            available=self.session.source.source_type is SourceType.SIMULATED,
        )
        self.lead_off_start_button.setEnabled(not active and self.session.source.source_type is SourceType.SIMULATED)
        self.lead_off_cancel_button.setEnabled(active)
        self.lead_off_scenario_combo.setEnabled(not active)
        dma_terminal = is_dma and (getattr(self.session.source, "finished", False) or getattr(self.session.source, "failure", None))
        self.play_button.setEnabled(not active and not dma_terminal)
        self.restart_button.setEnabled(not active and not is_dma)
        if is_dma:
            self.start_recording_button.setEnabled(False)
            self.load_replay_button.setEnabled(False)
        if active:
            self.start_recording_button.setEnabled(False)
            self.load_replay_button.setEnabled(False)

        report = self._lead_off_report or (machine.report if machine is not None else None)
        if report is None:
            self.waveform_canvas.set_contact_report(None)
            for label, channel in zip(self.lead_off_channel_labels, ELECTRODE_CHANNELS):
                label.setText(f"{channel.logical_channel} {channel.electrode}: 未检测")
                label.setStyleSheet("color: #94a3b8;")
            self.lead_off_reference_label.setText("REF FCz（公共 SRB1）: 未检测")
            self.lead_off_bias_label.setText("BIAS AFz: 暂不支持")
            self.lead_off_error_label.setText("超时/恢复错误：无")
            return

        self.waveform_canvas.set_contact_report(report)
        for label, result in zip(self.lead_off_channel_labels, report.channels):
            state_text = CONTACT_STATE_TEXT[result.state]
            label.setText(f"{result.logical_channel} {result.electrode}: {state_text}")
            color = {
                ContactState.CONNECTED: "#34d399",
                ContactState.ELECTRODE_OFF: "#f87171",
                ContactState.UNSTABLE: "#f59e0b",
            }.get(result.state, "#94a3b8")
            label.setStyleSheet(f"color: {color};")
        self.lead_off_reference_label.setText(
            f"REF {report.reference.electrode}（公共 SRB1）: "
            f"{CONTACT_STATE_TEXT[report.reference.state]}"
        )
        self.lead_off_bias_label.setText(
            f"BIAS {report.bias.electrode}: {CONTACT_STATE_TEXT[report.bias.state]}"
        )
        errors = []
        if report.primary_error:
            errors.append(report.primary_error)
        errors.extend(report.recovery_errors)
        self.lead_off_error_label.setText(
            "超时/恢复错误：" + ("；".join(errors) if errors else "无")
        )
        self.lead_off_error_label.setToolTip("\n".join(errors))

    def _channel_count_changed(self, index: int) -> None:
        self.session.set_visible_channel_count(8 if index == 0 else 16)
        self._refresh(self.session.snapshot())

    def _waveform_mode_changed(self, index: int) -> None:
        self.session.set_waveform_mode(WaveformMode.FILTERED if index == 0 else WaveformMode.RAW)
        self._refresh(self.session.snapshot())

    def _time_window_changed(self, index: int) -> None:
        self.session.set_time_window_seconds(TIME_WINDOWS_SECONDS[index])
        self._refresh(self.session.snapshot())

    def _amplitude_changed(self, index: int) -> None:
        self.session.set_amplitude_scale(AMPLITUDE_SCALES[index])
        self._refresh(self.session.snapshot())

    def _range_changed(self, index: int) -> None:
        self.waveform_canvas.base_half_range_counts = COUNT_HALF_RANGES[index]
        self.waveform_canvas.update()

    def _waveform_channel_changed(self, index: int) -> None:
        self.waveform_canvas.selected_channel_index = None if index == 0 else index - 1
        self.waveform_canvas.update()

    def _refresh(self, snapshot: ScopeSnapshot) -> None:
        self.waveform_canvas.set_snapshot(snapshot)
        self.spectrum_canvas.set_snapshot(snapshot)
        self.counter_panel.set_counters(dict(snapshot.error_counters))
        self.play_button.setText("暂停" if snapshot.playing else "继续")
        self.playback_label.setText("● 播放中" if snapshot.playing else "Ⅱ 已暂停")
        self.playback_label.setStyleSheet(
            f"font-weight: 600; color: {'#34d399' if snapshot.playing else '#f59e0b'};"
        )
        imported = self._replay_source is not None and self._replay_source.info.is_board_internal_test
        provenance = ("历史板测文件" if imported else
                      "硬件来源" if snapshot.is_real_hardware else "非真实采集")
        self.source_label.setText(
            f"来源：{snapshot.source_type.name} · {provenance}"
        )
        self.source_label.setToolTip(snapshot.source_display_name)
        rate = f"{snapshot.sample_rate_hz:.6f}".rstrip("0").rstrip(".")
        self.assumption_label.setText(
            f"DSP：{rate} SPS · HP 0.5 / LP 45 / Notch 50 Hz · counts"
        )
        sequence = "--" if snapshot.last_sequence is None else f"{snapshot.last_sequence:,}"
        self.sequence_label.setText(f"帧号：{sequence} · {rate} SPS")
        if self._replay_error:
            self.playback_label.setText("错误 · 已暂停")
        elif self._replay_source is not None and self._replay_source.progress.finished:
            self.playback_label.setText("■ 播放结束")
        elif snapshot.source_type is SourceType.ADS1299_DMA:
            self.source_label.setText("来源：ADS1299_DMA · Linux有限内部测试 · 非真人脑电")
            if getattr(self.session.source, "failure", None):
                self.playback_label.setText("有限DMA STOP")
            elif getattr(self.session.source, "finished", False):
                self.playback_label.setText("■ 有限DMA结束")
        self._refresh_storage()
        self._refresh_lead_off()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        if self._lead_off_machine is not None and self._lead_off_machine.active:
            self._lead_off_machine.cancel()
            self._lead_off_machine.run_to_terminal()
            self._finish_lead_off_session()
        if self._recorder is not None:
            try:
                self.stop_recording()
            except RecordingWriteError as exc:
                self._write_error = str(exc)
        self._close_replay_source()
        if self.session.source.source_type is SourceType.ADS1299_DMA:
            self.session.source.close()
        super().closeEvent(event)


def run(source: RealtimeFrameSource | None = None, argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv
    parser = argparse.ArgumentParser(description="EEG scope: SIMULATED or validated FILE_REPLAY")
    parser.add_argument("--replay", type=Path, help="validate and play this file; failure exits without fallback")
    parser.add_argument("--pl-processed", type=Path,
                        help="validate and play an offline EDP1 PL-processing candidate stream")
    args = parser.parse_args(arguments[1:])
    if args.replay is not None and args.pl_processed is not None:
        parser.error("--replay and --pl-processed cannot be combined")
    if args.replay is not None:
        if source is not None:
            parser.error("--replay and a supplied source cannot be combined")
        try:
            source = FileReplaySource(args.replay)
        except (OSError, ValueError) as exc:
            print(f"FILE_REPLAY load failed: {exc}", file=sys.stderr)
            return 2
    if args.pl_processed is not None:
        if source is not None:
            parser.error("--pl-processed and a supplied source cannot be combined")
        try:
            source = ProcessedDspReplaySource(args.pl_processed)
        except (OSError, ValueError) as exc:
            print(f"EDP1 PL replay load failed: {exc}", file=sys.stderr)
            return 2
    app = QApplication.instance() or QApplication([arguments[0]])
    configure_ui_font(app)
    window = OscilloscopeWindow(source)
    window.show()
    return app.exec()
